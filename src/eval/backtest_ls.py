"""
Embargoed Long/Short backtest (Causal Q2-Q3 Long / Q5 Short) with 15 bps round-trip costs,
Empirical-Bayes Kelly + Merton Jump-Diffusion 95% CVaR sizing, and strict In-Sample vs
Out-of-Sample ticker validation across 10 staggered non-overlapping sub-books.

Usage:
    PYTHONPATH=. python3 -W ignore src/eval/backtest_ls.py
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

IN_SAMPLE = {"VLO", "MPC", "PSX"}
OUT_OF_SAMPLE = {"DINO", "PBF", "DK", "CVI"}

HOLDING_DAYS = 10
CLOUD_MAX = 40.0
COST_RT = 0.0015           # 15 bps round-trip slippage / borrow per trade
MIN_TRAIN_OBS = 60
PRIOR_ALPHA = 10.0         # Beta(10, 10) shrinkage toward 50% hit rate
INITIAL_EQUITY = 25_000.0
EQ_WT_LEG = 0.08           # 8% per leg for equal-weight raw benchmark
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

    def _add_yoy(g: pd.DataFrame) -> pd.DataFrame:
        g = g.sort_values("trading_date").copy()
        clean = g["w_frp_pp"].where(g["cloud_cover_pct_weighted"] <= 50.0)
        g["yoy_per_pass"] = clean.rolling(5, min_periods=2).mean() / clean.rolling(
            21, min_periods=7
        ).mean().shift(252).replace(0.0, np.nan)
        return g

    df = df.groupby("ticker", group_keys=False).apply(_add_yoy)
    return df.sort_values(["trading_date", "ticker"]).reset_index(drop=True)


def print_quintiles(df: pd.DataFrame, label: str) -> None:
    pool = df[
        (df["cloud_cover_pct_weighted"] <= CLOUD_MAX)
        & (df["regime_label"] == "IDIOSYNCRATIC")
        & (df["yoy_per_pass"].notna())
        & (df["fwd_ret_10d_excess"].notna())
    ].copy()
    if len(pool) < 50:
        print(f"[{label}] Insufficient clear-sky observations ({len(pool)}).\n")
        return

    pool["yoy_q"] = pd.qcut(
        pool["yoy_per_pass"], 5, labels=["Q1_LOW", "Q2", "Q3", "Q4", "Q5_HIGH"], duplicates="drop"
    )
    qtab = pool.groupby("yoy_q", observed=False)["fwd_ret_10d_excess"].agg(
        n="count",
        mean_fwd_pct=lambda s: round(s.mean() * 100, 3),
        pct_pos=lambda s: round((s > 0).mean() * 100, 1),
    )
    q23 = pool[pool["yoy_q"].isin(["Q2", "Q3"])]["fwd_ret_10d_excess"].mean() * 100
    q5 = pool[pool["yoy_q"] == "Q5_HIGH"]["fwd_ret_10d_excess"].mean() * 100
    print(f"=== QUINTILES: {label} (Clear Sky + Idiosyncratic, 10d Fwd Excess) ===")
    print(qtab.to_string())
    print(f"  -> Gross Q2-Q3 vs Q5 spread: {q23 - q5:+.3f}% ({(q23 - q5)*100:+.0f} bps)\n")


def evaluate_split(df_full: pd.DataFrame, tickers: set[str], label: str) -> None:
    df = df_full[df_full["ticker"].isin(tickers)].copy()
    if df.empty:
        print(f"[{label}] No data found for tickers {sorted(tickers)}.\n")
        return

    print_quintiles(df, label)

    all_dates = sorted(df["trading_date"].unique())
    date_to_idx = {d: i for i, d in enumerate(all_dates)}
    mkt_by_ticker = {
        t: grp.sort_values("trading_date").reset_index(drop=True)
        for t, grp in df.groupby("ticker")
    }

    # Cache embargoed quantiles + Empirical-Bayes stats per date
    stats_cache: dict[pd.Timestamp, dict | None] = {}
    cvar_cache: dict[tuple[str, int, pd.Timestamp], float] = {}

    def get_causal_stats(dt: pd.Timestamp) -> dict | None:
        if dt in stats_cache:
            return stats_cache[dt]

        idx = date_to_idx[dt]
        if idx < HOLDING_DAYS:
            stats_cache[dt] = None
            return None
        embargo_dt = all_dates[idx - HOLDING_DAYS]

        hist = df[
            (df["trading_date"] <= embargo_dt)
            & (df["cloud_cover_pct_weighted"] <= CLOUD_MAX)
            & (df["yoy_per_pass"].notna())
            & (df["fwd_ret_10d_excess"].notna())
        ]
        if len(hist) < MIN_TRAIN_OBS:
            stats_cache[dt] = None
            return None

        q20, q60, q80 = np.quantile(hist["yoy_per_pass"].values, [0.20, 0.60, 0.80])

        long_r = hist.loc[
            (hist["yoy_per_pass"] >= q20) & (hist["yoy_per_pass"] <= q60), "fwd_ret_10d_excess"
        ] - COST_RT
        short_r = -hist.loc[hist["yoy_per_pass"] >= q80, "fwd_ret_10d_excess"] - COST_RT

        def _leg_edge(rets: pd.Series) -> tuple[float, float]:
            if len(rets) < 10:
                return 0.50, 1.0
            wins = rets[rets > 0]
            losses = rets[rets < 0]
            p_win = (len(wins) + PRIOR_ALPHA) / (len(rets) + 2.0 * PRIOR_ALPHA)
            b_rat = float(wins.mean() / abs(losses.mean())) if (len(wins) >= 3 and len(losses) >= 3) else 1.0
            return float(p_win), b_rat

        p_long, b_long = _leg_edge(long_r)
        p_short, b_short = _leg_edge(short_r)

        stats_cache[dt] = {
            "q20": q20,
            "q60": q60,
            "q80": q80,
            "p_long": p_long,
            "b_long": b_long,
            "p_short": p_short,
            "b_short": b_short,
        }
        return stats_cache[dt]

    def get_unit_cvar(tkr: str, direction: int, dt: pd.Timestamp) -> float:
        key = (tkr, direction, dt)
        if key not in cvar_cache:
            t_hist = mkt_by_ticker[tkr]
            t_hist = t_hist[t_hist["trading_date"] <= dt]
            params = calibrate_merton_params(t_hist["ret_1d"])
            unit_rets = simulate_merton_unit_returns(
                params, direction=direction, holding_days=HOLDING_DAYS, n_sims=4_000, seed=42
            )
            losses = -unit_rets
            var_95 = float(np.quantile(losses, 0.95))
            cvar_cache[key] = float(losses[losses >= var_95].mean())
        return cvar_cache[key]

    book_rows = []
    all_raw_trades = []
    all_q_trades = []

    for offset in range(HOLDING_DAYS):
        step_dates = all_dates[offset::HOLDING_DAYS]

        eq_raw, peak_raw, dd_raw = INITIAL_EQUITY, INITIAL_EQUITY, 0.0
        eq_q, peak_q, dd_q = INITIAL_EQUITY, INITIAL_EQUITY, 0.0
        raw_step_rets: list[float] = []
        q_step_rets: list[float] = []
        raw_pnls: list[float] = []
        q_pnls: list[float] = []

        for dt in step_dates:
            if date_to_idx[dt] + HOLDING_DAYS >= len(all_dates):
                continue

            st = get_causal_stats(dt)
            if st is None:
                raw_step_rets.append(0.0)
                q_step_rets.append(0.0)
                continue

            cands = df[
                (df["trading_date"] == dt)
                & (df["cloud_cover_pct_weighted"] <= CLOUD_MAX)
                & (df["regime_label"] == "IDIOSYNCRATIC")
                & (df["yoy_per_pass"].notna())
                & (df["fwd_ret_10d_excess"].notna())
            ]
            if cands.empty:
                raw_step_rets.append(0.0)
                q_step_rets.append(0.0)
                continue

            raw_orders = []
            q_orders = []
            for _, row in cands.iterrows():
                yoy = float(row["yoy_per_pass"])
                if st["q20"] <= yoy <= st["q60"]:
                    direction = 1
                    p_win, b_rat = st["p_long"], st["b_long"]
                elif yoy >= st["q80"]:
                    direction = -1
                    p_win, b_rat = st["p_short"], st["b_short"]
                else:
                    continue

                net_ret = direction * float(row["fwd_ret_10d_excess"]) - COST_RT
                raw_orders.append((row["ticker"], direction, net_ret, EQ_WT_LEG))

                raw_kelly = p_win - (1.0 - p_win) / max(b_rat, 1e-6)
                if raw_kelly <= 0:
                    continue
                reg_mult = compute_regime_mult(
                    str(row["regime_label"]),
                    float(row["p_macro_panic"]) if pd.notna(row["p_macro_panic"]) else 1.0,
                    float(row["ovx_level"]) if pd.notna(row["ovx_level"]) else np.nan,
                )
                kelly_wt = raw_kelly * 0.25 * reg_mult
                cvar_95 = get_unit_cvar(row["ticker"], direction, dt)
                cvar_cap = 0.015 / max(cvar_95, 1e-6)
                wt = min(kelly_wt, cvar_cap, MAX_SINGLE_WT)
                if wt > 0:
                    q_orders.append((row["ticker"], direction, net_ret, wt))

            # Execute Equal-Weight Book
            if raw_orders:
                g_wt = sum(o[3] for o in raw_orders)
                sc = min(1.0, MAX_GROSS_WT / g_wt)
                step_pnl = 0.0
                for tkr, d, nr, wt in raw_orders:
                    pnl = eq_raw * (wt * sc) * nr
                    step_pnl += pnl
                    raw_pnls.append(pnl)
                    all_raw_trades.append({"dir": d, "net_ret": nr, "pnl": pnl})
                raw_step_rets.append(step_pnl / eq_raw)
                eq_raw += step_pnl
                peak_raw = max(peak_raw, eq_raw)
                dd_raw = max(dd_raw, (peak_raw - eq_raw) / peak_raw)
            else:
                raw_step_rets.append(0.0)

            # Execute Full Quant Stack Book
            if q_orders:
                g_wt = sum(o[3] for o in q_orders)
                sc = min(1.0, MAX_GROSS_WT / g_wt)
                step_pnl = 0.0
                for tkr, d, nr, wt in q_orders:
                    pnl = eq_q * (wt * sc) * nr
                    step_pnl += pnl
                    q_pnls.append(pnl)
                    all_q_trades.append({"dir": d, "net_ret": nr, "pnl": pnl})
                q_step_rets.append(step_pnl / eq_q)
                eq_q += step_pnl
                peak_q = max(peak_q, eq_q)
                dd_q = max(dd_q, (peak_q - eq_q) / peak_q)
            else:
                q_step_rets.append(0.0)

        def _metrics(eq: float, pnls: list[float], step_r: list[float], dd: float) -> tuple:
            ret_pct = (eq / INITIAL_EQUITY - 1.0) * 100
            ex_best = ((eq - max(pnls)) / INITIAL_EQUITY - 1.0) * 100 if pnls else 0.0
            sr = np.array(step_r)
            sharpe = (sr.mean() / sr.std(ddof=1) * np.sqrt(252.0 / HOLDING_DAYS)) if sr.std(ddof=1) > 0 else 0.0
            gp = sum(p for p in pnls if p > 0)
            gl = abs(sum(p for p in pnls if p < 0))
            pf = gp / gl if gl > 0 else np.nan
            return round(ret_pct, 2), round(ex_best, 2), round(float(sharpe), 2), round(float(pf), 2), round(dd * 100, 2)

        r_ret, r_ex, r_sh, r_pf, r_dd = _metrics(eq_raw, raw_pnls, raw_step_rets, dd_raw)
        q_ret, q_ex, q_sh, q_pf, q_dd = _metrics(eq_q, q_pnls, q_step_rets, dd_q)
        book_rows.append(
            {
                "off": offset,
                "raw_n": len(raw_pnls),
                "raw_net%": r_ret,
                "raw_exB%": r_ex,
                "raw_sh": r_sh,
                "raw_dd%": r_dd,
                "q_n": len(q_pnls),
                "q_net%": q_ret,
                "q_exB%": q_ex,
                "q_sh": q_sh,
                "q_pf": q_pf,
                "q_dd%": q_dd,
            }
        )

    res = pd.DataFrame(book_rows)
    print(f"=== STAGGERED L/S BACKTEST: {label} (NET OF {COST_RT*1e4:.0f} BPS RT COSTS) ===")
    print(res.to_string(index=False))

    rtdf = pd.DataFrame(all_raw_trades)
    if not rtdf.empty:
        l_sub = rtdf[rtdf["dir"] == 1]
        s_sub = rtdf[rtdf["dir"] == -1]
        print(
            f"  [Raw L/S Mean] Net={res['raw_net%'].mean():+.2f}% (ex-best {res['raw_exB%'].mean():+.2f}%) "
            f"| Sharpe={res['raw_sh'].mean():.2f} | DD={res['raw_dd%'].mean():.2f}% | AvgN={res['raw_n'].mean():.1f}"
        )
        print(
            f"  [Leg Split   ] Long Q2-Q3: n={len(l_sub)} avg={l_sub['net_ret'].mean()*1e4:+.1f} bps WR={(l_sub['net_ret']>0).mean()*100:.1f}% "
            f"| Short Q5: n={len(s_sub)} avg={s_sub['net_ret'].mean()*1e4:+.1f} bps WR={(s_sub['net_ret']>0).mean()*100:.1f}%"
        )
    print(
        f"  [Quant Stack ] Net={res['q_net%'].mean():+.2f}% (ex-best {res['q_exB%'].mean():+.2f}%) "
        f"| Sharpe={res['q_sh'].mean():.2f} | PF={res['q_pf'].mean():.2f} | DD={res['q_dd%'].mean():.2f}% | AvgN={res['q_n'].mean():.1f}\n"
    )


def main() -> None:
    df = build_panel()
    evaluate_split(df, IN_SAMPLE, "IN-SAMPLE (VLO, MPC, PSX)")
    evaluate_split(df, OUT_OF_SAMPLE, "OUT-OF-SAMPLE (DINO, PBF, DK, CVI)")
    evaluate_split(df, IN_SAMPLE | OUT_OF_SAMPLE, "FULL UNIVERSE (ALL 7 REFINERS)")


if __name__ == "__main__":
    main()
