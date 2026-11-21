"""
Final pre-production audit for Pure Beta-Hedged Short Q5 (Short Stock / Long beta*XLE):
- Causal embargoed q80 threshold across 10 staggered non-overlapping sub-books
- 25 bps round-trip cost (short borrow + XLE hedge execution)
- Immediate MOC Close(t) vs 1-Day Delayed Close(t+1) execution test
- Year-by-year and Ticker-by-ticker attribution + Half-Kelly / Merton CVaR stack

Usage:
    PYTHONPATH=. python3 -W ignore src/eval/audit_short_q5.py
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from src.risk.sizing import calibrate_merton_params, simulate_merton_unit_returns

SILVER_PATH = Path("data/silver/facility_daily.parquet")
GOLD_PATH = Path("data/gold/refinery_features.parquet")
MARKET_PATH = Path("data/silver/market_returns.parquet")
REGIME_PATH = Path("data/silver/market_regimes.parquet")

HOLDING_DAYS = 10
CLOUD_MAX = 40.0
COST_RT = 0.0025           # 25 bps RT (short leg + XLE hedge + borrow)
MIN_TRAIN_Q5 = 20
PRIOR_ALPHA = 10.0         # Beta(10, 10) empirical-Bayes shrinkage
INITIAL_EQUITY = 25_000.0
EQ_WT_LEG = 0.10           # 10% notional per trade for equal-weight baseline
KELLY_MULT = 0.50          # Half-Kelly (capped by 1.5% 95% CVaR & 15% max weight)
MAX_SINGLE_WT = 0.15
MAX_GROSS_WT = 0.60


def compute_regime_mult(regime_label: str, p_panic: float, ovx: float) -> float:
    if regime_label != "IDIOSYNCRATIC":
        return 0.0
    if pd.notna(ovx) and ovx < 35.0:
        return 1.0
    if pd.notna(ovx) and p_panic >= 0.50:
        return float(np.clip(1.0 - 0.5 * ((ovx - 35.0) / 10.0), 0.5, 1.0))
    return 1.0


def build_panel() -> pd.DataFrame:
    fac = pd.read_parquet(SILVER_PATH).copy()
    fac["frp_per_pass"] = np.where(
        fac["satellite_pass_count"] > 0,
        fac["frp_night_mw"] / fac["satellite_pass_count"],
        0.0,
    )
    cap = fac[["facility_id", "ticker", "capacity_bpd"]].drop_duplicates()
    cap["w"] = cap["capacity_bpd"] / cap.groupby("ticker")["capacity_bpd"].transform("sum")
    fac = fac.merge(cap[["facility_id", "w"]], on="facility_id", how="left")
    fac["w_frp_pp"] = fac["frp_per_pass"] * fac["w"]

    pp = (
        fac.groupby(["ticker", "trading_date"], as_index=False)["w_frp_pp"]
        .sum()
        .sort_values(["ticker", "trading_date"])
    )

    df = (
        pd.read_parquet(GOLD_PATH)
        .merge(pp, on=["ticker", "trading_date"], how="inner")
        .merge(pd.read_parquet(MARKET_PATH), on=["ticker", "trading_date"], how="inner")
        .merge(
            pd.read_parquet(REGIME_PATH)[
                ["trading_date", "p_macro_panic", "regime_label", "is_tradeable"]
            ],
            on="trading_date",
            how="left",
        )
    )

    def _add_feats(g: pd.DataFrame) -> pd.DataFrame:
        g = g.sort_values("trading_date").copy()
        clean = g["w_frp_pp"].where(g["cloud_cover_pct_weighted"] <= 50.0)
        g["yoy_per_pass"] = clean.rolling(5, min_periods=2).mean() / clean.rolling(
            21, min_periods=7
        ).mean().shift(252).replace(0.0, np.nan)
        # Immediate MOC Close(t)->Close(t+10) vs 1-day delayed Close(t+1)->Close(t+11)
        g["fwd_ret_10d_lag1"] = g["fwd_ret_10d_excess"].shift(-1)
        return g

    return (
        df.groupby("ticker", group_keys=False)
        .apply(_add_feats)
        .sort_values(["trading_date", "ticker"])
        .reset_index(drop=True)
    )


def run_mode(df: pd.DataFrame, ret_col: str, mode_label: str) -> pd.DataFrame:
    all_dates = sorted(df["trading_date"].unique())
    date_to_idx = {d: i for i, d in enumerate(all_dates)}
    mkt_by_ticker = {
        t: grp.sort_values("trading_date").reset_index(drop=True)
        for t, grp in df.groupby("ticker")
    }

    embargo_shift = HOLDING_DAYS + (1 if ret_col.endswith("lag1") else 0)
    stats_cache: dict[pd.Timestamp, tuple[float, float, float] | None] = {}
    cvar_cache: dict[tuple[str, pd.Timestamp], float] = {}

    def get_causal_q5(dt: pd.Timestamp) -> tuple[float, float, float] | None:
        if dt in stats_cache:
            return stats_cache[dt]
        idx = date_to_idx[dt]
        if idx < embargo_shift:
            stats_cache[dt] = None
            return None
        embargo_dt = all_dates[idx - embargo_shift]

        hist = df[
            (df["trading_date"] <= embargo_dt)
            & (df["cloud_cover_pct_weighted"] <= CLOUD_MAX)
            & (df["yoy_per_pass"].notna())
            & (df[ret_col].notna())
        ]
        if len(hist) < 80:
            stats_cache[dt] = None
            return None

        q80 = float(np.quantile(hist["yoy_per_pass"].values, 0.80))
        q5_rets = -hist.loc[hist["yoy_per_pass"] >= q80, ret_col] - COST_RT
        if len(q5_rets) < MIN_TRAIN_Q5:
            stats_cache[dt] = None
            return None

        wins = q5_rets[q5_rets > 0]
        losses = q5_rets[q5_rets < 0]
        p_win = (len(wins) + PRIOR_ALPHA) / (len(q5_rets) + 2.0 * PRIOR_ALPHA)
        b_rat = float(wins.mean() / abs(losses.mean())) if (len(wins) >= 5 and len(losses) >= 5) else 1.0
        stats_cache[dt] = (q80, float(p_win), b_rat)
        return stats_cache[dt]

    def get_unit_cvar(tkr: str, dt: pd.Timestamp) -> float:
        key = (tkr, dt)
        if key not in cvar_cache:
            t_hist = mkt_by_ticker[tkr]
            t_hist = t_hist[t_hist["trading_date"] <= dt]
            params = calibrate_merton_params(t_hist["ret_1d"])
            unit_rets = simulate_merton_unit_returns(
                params, direction=-1, holding_days=HOLDING_DAYS, n_sims=4_000, seed=42
            )
            losses = -unit_rets
            var_95 = float(np.quantile(losses, 0.95))
            cvar_cache[key] = float(losses[losses >= var_95].mean())
        return cvar_cache[key]

    book_rows = []
    all_trades = []

    for offset in range(HOLDING_DAYS):
        step_dates = all_dates[offset::HOLDING_DAYS]
        eq_raw, peak_raw, dd_raw = INITIAL_EQUITY, INITIAL_EQUITY, 0.0
        eq_q, peak_q, dd_q = INITIAL_EQUITY, INITIAL_EQUITY, 0.0
        raw_step_r, q_step_r = [], []
        raw_pnls, q_pnls = [], []

        for dt in step_dates:
            if date_to_idx[dt] + embargo_shift >= len(all_dates):
                continue

            st = get_causal_q5(dt)
            if st is None:
                raw_step_r.append(0.0)
                q_step_r.append(0.0)
                continue
            q80, p_win, b_rat = st

            cands = df[
                (df["trading_date"] == dt)
                & (df["cloud_cover_pct_weighted"] <= CLOUD_MAX)
                & (df["regime_label"] == "IDIOSYNCRATIC")
                & (df["yoy_per_pass"] >= q80)
                & (df[ret_col].notna())
            ]
            if cands.empty:
                raw_step_r.append(0.0)
                q_step_r.append(0.0)
                continue

            # Equal-Weight Short Q5
            raw_scale = min(1.0, MAX_GROSS_WT / (len(cands) * EQ_WT_LEG))
            step_pnl_raw = 0.0
            for _, row in cands.iterrows():
                nr = -float(row[ret_col]) - COST_RT
                pnl = eq_raw * (EQ_WT_LEG * raw_scale) * nr
                step_pnl_raw += pnl
                raw_pnls.append(pnl)
                all_trades.append(
                    {
                        "offset": offset,
                        "date": dt,
                        "year": pd.Timestamp(dt).year,
                        "ticker": row["ticker"],
                        "net_ret": nr,
                        "p_win": p_win,
                        "b_rat": b_rat,
                    }
                )
            raw_step_r.append(step_pnl_raw / eq_raw)
            eq_raw += step_pnl_raw
            peak_raw = max(peak_raw, eq_raw)
            dd_raw = max(dd_raw, (peak_raw - eq_raw) / peak_raw)

            # Quant Stack (Half-Kelly + Merton 95% CVaR)
            raw_kelly = p_win - (1.0 - p_win) / max(b_rat, 1e-6)
            q_orders = []
            if raw_kelly > 0:
                for _, row in cands.iterrows():
                    reg_mult = compute_regime_mult(
                        str(row["regime_label"]),
                        float(row["p_macro_panic"]) if pd.notna(row["p_macro_panic"]) else 1.0,
                        float(row["ovx_level"]) if pd.notna(row["ovx_level"]) else np.nan,
                    )
                    k_wt = raw_kelly * KELLY_MULT * reg_mult
                    cvar_cap = 0.015 / max(get_unit_cvar(row["ticker"], dt), 1e-6)
                    wt = min(k_wt, cvar_cap, MAX_SINGLE_WT)
                    if wt > 0:
                        q_orders.append((row, wt))

            if q_orders:
                q_scale = min(1.0, MAX_GROSS_WT / sum(w for _, w in q_orders))
                step_pnl_q = 0.0
                for row, wt in q_orders:
                    nr = -float(row[ret_col]) - COST_RT
                    pnl = eq_q * (wt * q_scale) * nr
                    step_pnl_q += pnl
                    q_pnls.append(pnl)
                q_step_r.append(step_pnl_q / eq_q)
                eq_q += step_pnl_q
                peak_q = max(peak_q, eq_q)
                dd_q = max(dd_q, (peak_q - eq_q) / peak_q)
            else:
                q_step_r.append(0.0)

        def _m(eq: float, pnls: list[float], sr: list[float], dd: float) -> tuple:
            ret = (eq / INITIAL_EQUITY - 1.0) * 100
            ex_b = ((eq - max(pnls)) / INITIAL_EQUITY - 1.0) * 100 if pnls else 0.0
            a = np.array(sr)
            sh = (a.mean() / a.std(ddof=1) * np.sqrt(252.0 / HOLDING_DAYS)) if a.std(ddof=1) > 0 else 0.0
            gp, gl = sum(p for p in pnls if p > 0), abs(sum(p for p in pnls if p < 0))
            pf = gp / gl if gl > 0 else np.nan
            return round(ret, 2), round(ex_b, 2), round(float(sh), 2), round(float(pf), 2), round(dd * 100, 2)

        rr, reb, rsh, rpf, rdd = _m(eq_raw, raw_pnls, raw_step_r, dd_raw)
        qr, qeb, qsh, qpf, qdd = _m(eq_q, q_pnls, q_step_r, dd_q)
        book_rows.append(
            {
                "off": offset,
                "n": len(raw_pnls),
                "ew_net%": rr,
                "ew_exB%": reb,
                "ew_sh": rsh,
                "ew_pf": rpf,
                "ew_dd%": rdd,
                "q_n": len(q_pnls),
                "q_net%": qr,
                "q_exB%": qeb,
                "q_sh": qsh,
                "q_pf": qpf,
                "q_dd%": qdd,
            }
        )

    res = pd.DataFrame(book_rows)
    print(f"=== PURE SHORT Q5 ({mode_label} | NET OF {COST_RT*1e4:.0f} BPS RT) ===")
    print(res.to_string(index=False))
    print(
        f"  [EW Mean   ] Net={res['ew_net%'].mean():+.2f}% (ex-best {res['ew_exB%'].mean():+.2f}%) "
        f"| Sharpe={res['ew_sh'].mean():.2f} | PF={res['ew_pf'].mean():.2f} | DD={res['ew_dd%'].mean():.2f}% | PosBooks={(res['ew_net%']>0).sum()}/10"
    )
    print(
        f"  [Quant Mean] Net={res['q_net%'].mean():+.2f}% (ex-best {res['q_exB%'].mean():+.2f}%) "
        f"| Sharpe={res['q_sh'].mean():.2f} | PF={res['q_pf'].mean():.2f} | DD={res['q_dd%'].mean():.2f}% | PosBooks={(res['q_net%']>0).sum()}/10\n"
    )
    return pd.DataFrame(all_trades)


def main() -> None:
    df = build_panel()
    t_moc = run_mode(df, "fwd_ret_10d_excess", "MOC Close(t) -> Close(t+10)")
    run_mode(df, "fwd_ret_10d_lag1", "1-DAY LAG Close(t+1) -> Close(t+11)")

    if not t_moc.empty:
        # Deduplicate across staggered offsets? No, each date appears in exactly 1 offset!
        print("=== SHORT Q5 ATTRIBUTION BY YEAR (NET OF 25 BPS, MOC) ===")
        yr = t_moc.groupby("year")["net_ret"].agg(
            n="count",
            mean_bps=lambda s: round(s.mean() * 1e4, 1),
            wr_pct=lambda s: round((s > 0).mean() * 100, 1),
        )
        print(yr.to_string())

        print("\n=== SHORT Q5 ATTRIBUTION BY TICKER (NET OF 25 BPS, MOC) ===")
        tk = t_moc.groupby("ticker")["net_ret"].agg(
            n="count",
            mean_bps=lambda s: round(s.mean() * 1e4, 1),
            wr_pct=lambda s: round((s > 0).mean() * 100, 1),
        )
        print(tk.to_string())


if __name__ == "__main__":
    main()
