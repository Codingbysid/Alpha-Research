"""
Embargoed walk-forward backtest across 10 staggered non-overlapping sub-books (offsets 0-9).
Separates raw signal quality (equal-weight) from Platt + Fractional Kelly + Merton CVaR sizing.

Usage:
    PYTHONPATH=. python3 -W ignore src/eval/backtest.py
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression

from src.risk.sizing import calibrate_merton_params, simulate_merton_unit_returns

SILVER_PATH = Path("data/silver/facility_daily.parquet")
GOLD_PATH = Path("data/gold/refinery_features.parquet")
MARKET_PATH = Path("data/silver/market_returns.parquet")
REGIME_PATH = Path("data/silver/market_regimes.parquet")

HOLDING_DAYS = 10
CLOUD_MAX = 40.0
YOY_MIN_THRESHOLD = 1.10
MIN_TRAIN_OBS = 40
INITIAL_EQUITY = 25_000.0
EQ_WT_NOTIONAL = 0.10      # 10% fixed notional per trade for raw signal benchmark
MAX_GROSS_WEIGHT = 0.45    # Max simultaneous exposure across VLO/MPC/PSX


def compute_regime_mult(regime_label: str, p_panic: float, ovx: float) -> float:
    """Consistent with regime.py's OVX=45 floor; never zeroes out IDIOSYNCRATIC days."""
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

    gold = pd.read_parquet(GOLD_PATH)
    mkt = pd.read_parquet(MARKET_PATH)
    reg = pd.read_parquet(REGIME_PATH)

    df = (
        gold.merge(pp, on=["ticker", "trading_date"], how="inner")
        .merge(mkt, on=["ticker", "trading_date"], how="inner")
        .merge(
            reg[["trading_date", "p_macro_panic", "regime_label", "is_tradeable"]],
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
        g["log_w_frp_pp"] = np.log1p(g["w_frp_pp"].clip(lower=0.0))
        g["short_ret_10d"] = -g["fwd_ret_10d_excess"]
        g["short_win"] = (g["short_ret_10d"] > 0).astype(int)
        return g

    df = df.groupby("ticker", group_keys=False).apply(_add_feats)
    return df.sort_values(["trading_date", "ticker"]).reset_index(drop=True)


def print_funnel(df: pd.DataFrame) -> None:
    base = df[df["yoy_per_pass"].notna() & df["short_ret_10d"].notna()]
    g1 = base[base["cloud_cover_pct_weighted"] <= CLOUD_MAX]
    g2 = g1[g1["yoy_per_pass"] >= YOY_MIN_THRESHOLD]
    g3 = g2[g2["regime_label"] == "IDIOSYNCRATIC"]

    print("=== CANDIDATE FUNNEL (ALL DATES, BEFORE 10-DAY STEP SUBSAMPLING) ===")
    print(f"  1. Valid yoy_per_pass & 10d label : {len(base):5d}")
    print(f"  2. Clear sky (cloud <= {CLOUD_MAX:.0f}%)      : {len(g1):5d} ({len(g1)/max(1,len(base))*100:4.1f}%)")
    print(f"  3. Signal (yoy >= {YOY_MIN_THRESHOLD:.2f})          : {len(g2):5d} ({len(g2)/max(1,len(g1))*100:4.1f}%)")
    print(f"  4. Regime == IDIOSYNCRATIC        : {len(g3):5d} ({len(g3)/max(1,len(g2))*100:4.1f}%)")
    print(
        f"     -> Raw pool short mean={g3['short_ret_10d'].mean()*100:+.3f}% "
        f"| hit rate={(g3['short_ret_10d'] > 0).mean()*100:.1f}%\n"
    )

    # Check both tails (low YoY vs high YoY) inside Clear Sky + Idiosyncratic
    pool = g1[g1["regime_label"] == "IDIOSYNCRATIC"].copy()
    if len(pool) >= 50:
        pool["yoy_q"] = pd.qcut(pool["yoy_per_pass"], 5, labels=["Q1_LOW", "Q2", "Q3", "Q4", "Q5_HIGH"])
        qtab = pool.groupby("yoy_q", observed=False)["fwd_ret_10d_excess"].agg(
            n="count",
            mean_fwd_pct=lambda s: round(s.mean() * 100, 3),
            pct_neg=lambda s: round((s < 0).mean() * 100, 1),
        )
        print("=== CLEAR-SKY + IDIOSYNCRATIC QUINTILES (10d FWD EXCESS RETURN) ===")
        print(qtab.to_string())
        print()


def run_backtest() -> None:
    df = build_panel()
    print_funnel(df)

    all_dates = sorted(df["trading_date"].unique())
    date_to_idx = {d: i for i, d in enumerate(all_dates)}
    mkt_by_ticker = {
        t: grp.sort_values("trading_date").reset_index(drop=True)
        for t, grp in df.groupby("ticker")
    }

    # Cache embargoed Platt model + payoff ratio per date across all 10 offsets
    model_cache: dict[pd.Timestamp, tuple[LogisticRegression, float] | None] = {}
    cvar_cache: dict[tuple[str, pd.Timestamp], float] = {}

    def get_model(dt: pd.Timestamp) -> tuple[LogisticRegression, float] | None:
        if dt in model_cache:
            return model_cache[dt]

        idx = date_to_idx[dt]
        if idx < HOLDING_DAYS:
            model_cache[dt] = None
            return None
        embargo_dt = all_dates[idx - HOLDING_DAYS]

        hist = df[
            (df["trading_date"] <= embargo_dt)
            & (df["cloud_cover_pct_weighted"] <= CLOUD_MAX)
            & (df["yoy_per_pass"].notna())
            & (df["short_ret_10d"].notna())
        ]
        if len(hist) < MIN_TRAIN_OBS or hist["short_win"].nunique() < 2:
            model_cache[dt] = None
            return None

        clf = LogisticRegression(C=1.0, max_iter=500)
        clf.fit(hist[["yoy_per_pass", "log_w_frp_pp"]].fillna(0.0).values, hist["short_win"].values)

        # Estimate payoff ratio b from past triggered candidates (fallback to all clear-sky)
        trig = hist[hist["yoy_per_pass"] >= YOY_MIN_THRESHOLD]
        b_src = trig if len(trig) >= 20 else hist
        wins = b_src.loc[b_src["short_ret_10d"] > 0, "short_ret_10d"]
        losses = b_src.loc[b_src["short_ret_10d"] < 0, "short_ret_10d"]
        if len(wins) < 5 or len(losses) < 5:
            model_cache[dt] = None
            return None

        b_ratio = float(wins.mean() / abs(losses.mean()))
        model_cache[dt] = (clf, b_ratio)
        return model_cache[dt]

    def get_unit_cvar(tkr: str, dt: pd.Timestamp) -> float:
        key = (tkr, dt)
        if key not in cvar_cache:
            t_hist = mkt_by_ticker[tkr]
            t_hist = t_hist[t_hist["trading_date"] <= dt]
            params = calibrate_merton_params(t_hist["ret_1d"])
            unit_rets = simulate_merton_unit_returns(
                params, direction=-1, holding_days=HOLDING_DAYS, n_sims=5_000, seed=42
            )
            losses = -unit_rets
            var_95 = float(np.quantile(losses, 0.95))
            cvar_cache[key] = float(losses[losses >= var_95].mean())
        return cvar_cache[key]

    book_rows = []
    all_quant_trades = []
    kelly_blocked_total = 0

    for offset in range(HOLDING_DAYS):
        step_dates = all_dates[offset::HOLDING_DAYS]

        eq_raw = INITIAL_EQUITY
        peak_raw, dd_raw = INITIAL_EQUITY, 0.0
        raw_rets: list[float] = []

        eq_q = INITIAL_EQUITY
        peak_q, dd_q = INITIAL_EQUITY, 0.0
        q_trades: list[dict] = []

        for dt in step_dates:
            if date_to_idx[dt] + HOLDING_DAYS >= len(all_dates):
                continue

            cands = df[
                (df["trading_date"] == dt)
                & (df["cloud_cover_pct_weighted"] <= CLOUD_MAX)
                & (df["yoy_per_pass"] >= YOY_MIN_THRESHOLD)
                & (df["regime_label"] == "IDIOSYNCRATIC")
                & (df["short_ret_10d"].notna())
            ]
            if cands.empty:
                continue

            # 1. Raw Signal Equal-Weight Book (10% per name, no Kelly/CVaR)
            raw_pnl_step = 0.0
            for _, row in cands.iterrows():
                r = float(row["short_ret_10d"])
                raw_rets.append(r)
                raw_pnl_step += eq_raw * EQ_WT_NOTIONAL * r
            eq_raw += raw_pnl_step
            peak_raw = max(peak_raw, eq_raw)
            dd_raw = max(dd_raw, (peak_raw - eq_raw) / peak_raw)

            # 2. Full Quant Stack Book (Embargoed Platt + Quarter-Kelly + Merton CVaR)
            fitted = get_model(dt)
            if fitted is None:
                continue
            clf, b_ratio = fitted

            step_orders = []
            for _, row in cands.iterrows():
                x_cur = np.array([[float(row["yoy_per_pass"]), float(row["log_w_frp_pp"])]])
                p_win = float(clf.predict_proba(x_cur)[0, 1])
                raw_kelly = p_win - (1.0 - p_win) / b_ratio
                if raw_kelly <= 0:
                    kelly_blocked_total += 1
                    continue

                reg_mult = compute_regime_mult(
                    str(row["regime_label"]),
                    float(row["p_macro_panic"]) if pd.notna(row["p_macro_panic"]) else 1.0,
                    float(row["ovx_level"]) if pd.notna(row["ovx_level"]) else np.nan,
                )
                kelly_wt = raw_kelly * 0.25 * reg_mult
                cvar_95 = get_unit_cvar(row["ticker"], dt)
                cvar_cap = 0.015 / max(cvar_95, 1e-6)
                wt = min(kelly_wt, cvar_cap, 0.15)
                if wt > 0:
                    step_orders.append((row, p_win, b_ratio, wt))

            if not step_orders:
                continue

            gross_wt = sum(o[3] for o in step_orders)
            scale = min(1.0, MAX_GROSS_WEIGHT / gross_wt)
            pnl_step = 0.0

            for row, p_win, b_rat, wt in step_orders:
                eff_wt = wt * scale
                r = float(row["short_ret_10d"])
                pnl = eq_q * eff_wt * r
                pnl_step += pnl
                rec = {
                    "offset": offset,
                    "entry_date": dt,
                    "ticker": row["ticker"],
                    "yoy_per_pass": round(float(row["yoy_per_pass"]), 3),
                    "p_win": round(p_win, 3),
                    "b_ratio": round(b_rat, 2),
                    "weight": round(eff_wt, 4),
                    "short_ret_10d": r,
                    "pnl_usd": round(pnl, 2),
                }
                q_trades.append(rec)
                all_quant_trades.append(rec)

            eq_q += pnl_step
            peak_q = max(peak_q, eq_q)
            dd_q = max(dd_q, (peak_q - eq_q) / peak_q)

        q_df = pd.DataFrame(q_trades)
        q_ret_pct = (eq_q / INITIAL_EQUITY - 1.0) * 100
        # Best-trade removal check
        if not q_df.empty:
            best_pnl = q_df["pnl_usd"].max()
            ex_best_pct = ((eq_q - best_pnl) / INITIAL_EQUITY - 1.0) * 100
            q_wr = (q_df["short_ret_10d"] > 0).mean() * 100
            q_wt = q_df["weight"].mean() * 100
        else:
            ex_best_pct, q_wr, q_wt = 0.0, np.nan, 0.0

        book_rows.append(
            {
                "offset": offset,
                "raw_n": len(raw_rets),
                "raw_ret%": round((eq_raw / INITIAL_EQUITY - 1.0) * 100, 2),
                "raw_wr%": round(np.mean([r > 0 for r in raw_rets]) * 100, 1) if raw_rets else np.nan,
                "raw_dd%": round(dd_raw * 100, 2),
                "q_n": len(q_df),
                "q_ret%": round(q_ret_pct, 2),
                "q_ex_best%": round(ex_best_pct, 2),
                "q_wr%": round(q_wr, 1),
                "q_dd%": round(dd_q * 100, 2),
                "q_avg_wt%": round(q_wt, 2),
            }
        )

    res = pd.DataFrame(book_rows)
    print("=== 10 STAGGERED NON-OVERLAPPING SUB-BOOKS (EMBARGOED, OFFSETS 0-9) ===")
    print(res.to_string(index=False))
    print(f"\n[summary] Mean Raw Signal Return : {res['raw_ret%'].mean():+.2f}% | Mean Raw WR: {res['raw_wr%'].mean():.1f}% | Avg Trades/Book: {res['raw_n'].mean():.1f}")
    print(f"[summary] Mean Quant Stack Return: {res['q_ret%'].mean():+.2f}% (ex-best: {res['q_ex_best%'].mean():+.2f}%) | Mean WR: {res['q_wr%'].mean():.1f}% | Mean DD: {res['q_dd%'].mean():.2f}%")
    print(f"[summary] Candidates blocked by negative Kelly across all 10 books: {kelly_blocked_total}")


if __name__ == "__main__":
    run_backtest()
