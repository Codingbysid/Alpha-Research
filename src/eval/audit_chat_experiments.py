"""
Isolated Research Experiments:
1. Experiment A: Facility-Level Robust Z-Score vs. Ticker YoY Ratio
   - Evaluates z_{f,t} on log(1 + x_{f,t}) using trailing 252d clear-sky median/MAD with a 10d embargo.
   - Aggregates to ticker level using one-sided distress: S_{i,t} = sum_f w_{f,i} * max(0, z_{f,t}).
   - Compares Rank IC, Daily MTM Sharpe, CAGR, Max DD, and Leave-One-Ticker-Out stability (specifically PBF).

2. Experiment B: Peer-Refiner Basket Hedge vs. XLE Hedge
   - Replaces the XLE sector hedge with an equal-weighted basket of unaffected peer refiners.
   - Compares Daily MTM Sharpe, Annualized Residual Volatility, CAGR, Max DD, Profit Factor, and Newey-West alpha t-stat.

Usage:
    PYTHONPATH=. python3 -W ignore src/eval/audit_chat_experiments.py
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import spearmanr
import statsmodels.api as sm
import yfinance as yf

from src.eval.audit_daily_mtm import (
    INITIAL_PORTFOLIO_CAPITAL,
    NUM_BOOKS,
    HOLDING_DAYS,
    COST_RT,
    load_factor_prices,
)
from src.eval.audit_model_upgrades import (
    CLOUD_MAX,
    EQ_WT_LEG,
    MAX_GROSS_WT,
    REFINER_TICKERS,
    load_datasets,
)

CRACK_PATH = Path("data/silver/crack_spread.parquet")
FACILITY_PATH = Path("data/silver/facility_daily.parquet")


# ==============================================================================
# Helper: Compute Facility-Level Robust Z-Score & Aggregate to Ticker
# ==============================================================================

def compute_facility_robust_z(fac_df: pd.DataFrame) -> pd.DataFrame:
    """Computes rolling 252d median/MAD z-score with 10d embargo on log(1 + pass_frp)."""
    def _calc_fac(g: pd.DataFrame) -> pd.DataFrame:
        g = g.sort_values("trading_date").copy()
        cs = g["cloud_cover_pct"] <= CLOUD_MAX
        # Nighttime FRP per detected nighttime pass
        x = np.where(cs & (g["satellite_pass_count"] > 0), g["frp_night_mw"] / g["satellite_pass_count"], np.nan)
        x = np.where(cs & (g["frp_night_mw"] == 0.0), 0.0, x)
        log_x = pd.Series(np.log1p(x), index=g.index)

        # 252d rolling with 10d embargo
        shifted = log_x.shift(10)
        roll_med = shifted.rolling(252, min_periods=30).median()

        def _mad(arr: np.ndarray) -> float:
            v = arr[~np.isnan(arr)]
            if len(v) < 30:
                return np.nan
            m = np.median(v)
            return float(np.median(np.abs(v - m)))

        roll_mad = shifted.rolling(252, min_periods=30).apply(_mad, raw=True)
        g["z_score"] = (log_x - roll_med) / (1.4826 * roll_mad + 0.15)
        g["z_pos"] = np.maximum(0.0, g["z_score"])
        return g

    fac_scored = fac_df.groupby("facility_id", group_keys=False).apply(_calc_fac)

    # Point-in-time capacity weights per ticker
    cap = fac_scored[["facility_id", "ticker", "capacity_bpd"]].drop_duplicates()
    cap["w"] = cap["capacity_bpd"] / cap.groupby("ticker")["capacity_bpd"].transform("sum")
    fac_scored = fac_scored.merge(cap[["facility_id", "w"]], on="facility_id", how="left")
    fac_scored["w_z_pos"] = fac_scored["z_pos"] * fac_scored["w"]

    ticker_scores = (
        fac_scored.groupby(["ticker", "trading_date"], as_index=False)["w_z_pos"]
        .sum()
        .rename(columns={"w_z_pos": "s_rob"})
    )
    return ticker_scores


# ==============================================================================
# Daily MTM Simulator for Refiners Sleeve (Supports Custom Signals & Hedges)
# ==============================================================================

@dataclass
class RefinerPosition:
    ticker: str
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    notional: float
    beta: float
    hedge_mode: str  # 'XLE' or 'PEER_BASKET'
    peer_tickers: list[str]
    entry_cost_paid: bool = False
    exit_cost_paid: bool = False


def run_refiners_daily_mtm(
    df: pd.DataFrame,
    signal_col: str,
    hedge_mode: str,  # 'XLE' or 'PEER_BASKET'
    all_dates: list[pd.Timestamp],
    px_matrix: pd.DataFrame,
    excluded_ticker: str | None = None,
) -> dict:
    date_to_idx = {d: i for i, d in enumerate(all_dates)}
    n_days = len(all_dates)

    book_cap = {b: INITIAL_PORTFOLIO_CAPITAL / NUM_BOOKS for b in range(NUM_BOOKS)}
    book_positions: dict[int, list[RefinerPosition]] = {b: [] for b in range(NUM_BOOKS)}

    # Precalculate rolling betas of each refiner to peer basket and XLE
    rets = px_matrix.pct_change()
    peer_betas: dict[tuple[str, str], pd.Series] = {}  # ticker -> rolling beta

    # Precalculate causal quantiles
    q_cache: dict[pd.Timestamp, tuple[float, float] | None] = {}

    def get_cutoffs(dt: pd.Timestamp) -> tuple[float, float] | None:
        if dt in q_cache:
            return q_cache[dt]
        idx = date_to_idx[dt]
        if idx < 10:
            q_cache[dt] = None
            return None
        hist_end_dt = all_dates[idx - 10]
        hist = df[
            (df["trading_date"] <= hist_end_dt)
            & (df["cloud_cover_pct_weighted"] <= CLOUD_MAX)
            & (df[signal_col].notna())
        ]
        if len(hist) < 40:
            q_cache[dt] = None
            return None
        q80 = float(np.quantile(hist[signal_col].values, 0.80))
        q95 = float(np.quantile(hist[signal_col].values, 0.95))
        q_cache[dt] = (q80, q95)
        return q_cache[dt]

    daily_records: list[dict] = []
    trade_count = 0

    for t_idx, dt in enumerate(all_dates):
        # 1. Staggered Book Entry on dt
        entering_book = t_idx % NUM_BOOKS
        cuts = get_cutoffs(dt)

        if cuts is not None:
            q80, q95 = cuts
            cond = (
                (df["trading_date"] == dt)
                & (df["cloud_cover_pct_weighted"] <= CLOUD_MAX)
                & (df[signal_col] >= q80)
                & (df["regime_label"] == "IDIOSYNCRATIC")
                & (df["crack_ok"].fillna(False))
            )
            if excluded_ticker is not None:
                cond &= df["ticker"] != excluded_ticker

            cands = df[cond].copy()
            if not cands.empty:
                denom = max(1e-6, q95 - q80)
                mults = np.clip((cands[signal_col].values - q80) / denom, 0.5, 1.5)
                raw_w = EQ_WT_LEG * mults
                sc = min(1.0, MAX_GROSS_WT / max(1e-6, np.sum(raw_w)))
                w_ref = raw_w * sc

                b_equity = book_cap[entering_book]
                triggered_tickers = set(cands["ticker"].tolist())

                for w_i, (_, r) in zip(w_ref, cands.iterrows()):
                    tkr = r["ticker"]
                    exit_idx = min(t_idx + HOLDING_DAYS, n_days - 1)
                    exit_dt = all_dates[exit_idx]

                    if hedge_mode == "XLE":
                        beta_val = float(r["beta_60d"]) if pd.notna(r["beta_60d"]) else 1.0
                        peer_list: list[str] = []
                    else:
                        # Peer basket: unaffected other refiners
                        avail_peers = [p for p in REFINER_TICKERS if p != tkr and p not in triggered_tickers]
                        if not avail_peers:
                            avail_peers = [p for p in REFINER_TICKERS if p != tkr]
                        peer_list = avail_peers

                        # Compute trailing 60d beta to peer basket
                        hist_start_idx = max(0, t_idx - 60)
                        hist_dts = all_dates[hist_start_idx:t_idx]
                        if len(hist_dts) >= 20:
                            peer_ret_hist = rets.loc[hist_dts, peer_list].mean(axis=1)
                            stk_ret_hist = rets.loc[hist_dts, tkr]
                            cov_sp = float(np.cov(stk_ret_hist, peer_ret_hist)[0, 1])
                            var_p = float(np.var(peer_ret_hist))
                            beta_val = cov_sp / max(1e-6, var_p)
                        else:
                            beta_val = 1.0

                    pos = RefinerPosition(
                        ticker=tkr,
                        entry_date=dt,
                        exit_date=exit_dt,
                        notional=b_equity * w_i,
                        beta=float(np.clip(beta_val, 0.2, 3.0)),
                        hedge_mode=hedge_mode,
                        peer_tickers=peer_list,
                    )
                    book_positions[entering_book].append(pos)
                    trade_count += 1

        # 2. Mark to Market
        if t_idx == 0:
            daily_records.append(
                {
                    "trading_date": dt,
                    "portfolio_equity": INITIAL_PORTFOLIO_CAPITAL,
                    "daily_ret": 0.0,
                    "daily_pnl": 0.0,
                }
            )
            continue

        prev_dt = all_dates[t_idx - 1]
        daily_pnl = 0.0

        for b in range(NUM_BOOKS):
            surviving: list[RefinerPosition] = []
            for pos in book_positions[b]:
                if pos.entry_date >= dt:
                    surviving.append(pos)
                    continue

                px_cur = px_matrix.loc[dt, pos.ticker]
                px_prev = px_matrix.loc[prev_dt, pos.ticker]
                ret_stk = (px_cur / px_prev - 1.0) if px_prev > 0 else 0.0

                if pos.hedge_mode == "XLE":
                    h_cur = px_matrix.loc[dt, "XLE"]
                    h_prev = px_matrix.loc[prev_dt, "XLE"]
                    ret_hedge = (h_cur / h_prev - 1.0) if h_prev > 0 else 0.0
                else:
                    # Peer basket return
                    p_rets = [
                        (px_matrix.loc[dt, p] / px_matrix.loc[prev_dt, p] - 1.0)
                        for p in pos.peer_tickers
                        if px_matrix.loc[prev_dt, p] > 0
                    ]
                    ret_hedge = float(np.mean(p_rets)) if p_rets else 0.0

                # Short stock, Long beta * hedge
                pos_ret = -ret_stk + pos.beta * ret_hedge
                dollar_pnl = pos.notional * pos_ret

                if not pos.entry_cost_paid:
                    dollar_pnl -= pos.notional * (COST_RT / 2.0)
                    pos.entry_cost_paid = True

                if dt >= pos.exit_date and not pos.exit_cost_paid:
                    dollar_pnl -= pos.notional * (COST_RT / 2.0)
                    pos.exit_cost_paid = True

                daily_pnl += dollar_pnl
                book_cap[b] += dollar_pnl

                if dt < pos.exit_date:
                    surviving.append(pos)

            book_positions[b] = surviving

        prev_eq = daily_records[-1]["portfolio_equity"]
        cur_eq = prev_eq + daily_pnl
        d_ret = daily_pnl / prev_eq if prev_eq > 0 else 0.0

        daily_records.append(
            {
                "trading_date": dt,
                "portfolio_equity": cur_eq,
                "daily_ret": d_ret,
                "daily_pnl": daily_pnl,
            }
        )

    res_df = pd.DataFrame(daily_records).set_index("trading_date")
    d_rets = res_df["daily_ret"].iloc[1:]

    mean_d = float(d_rets.mean())
    sd_d = float(d_rets.std(ddof=1))
    sharpe = (mean_d / sd_d * np.sqrt(252.0)) if sd_d > 0 else 0.0
    ann_vol = sd_d * np.sqrt(252.0) * 100.0

    final_eq = res_df["portfolio_equity"].iloc[-1]
    cum_ret = (final_eq / INITIAL_PORTFOLIO_CAPITAL - 1.0) * 100.0
    n_years = len(d_rets) / 252.0
    cagr = ((final_eq / INITIAL_PORTFOLIO_CAPITAL) ** (1.0 / n_years) - 1.0) * 100.0 if n_years > 0 else 0.0

    eq_series = res_df["portfolio_equity"].values
    peaks = np.maximum.accumulate(eq_series)
    max_dd = float(np.max((peaks - eq_series) / peaks)) * 100.0

    win_sum = float(d_rets[d_rets > 0].sum())
    loss_sum = float(abs(d_rets[d_rets < 0].sum()))
    pf = win_sum / loss_sum if loss_sum > 0 else np.nan

    # Newey-West Alpha vs SPY and XLE
    spy_ret = px_matrix["SPY"].pct_change().loc[d_rets.index].fillna(0.0)
    xle_ret = px_matrix["XLE"].pct_change().loc[d_rets.index].fillna(0.0)
    X = pd.DataFrame({"const": 1.0, "SPY": spy_ret, "XLE": xle_ret})
    ols = sm.OLS(d_rets, X).fit(cov_type="HAC", cov_kwds={"maxlags": 10})

    alpha_ann = float(ols.params["const"]) * 252.0 * 100.0
    alpha_t = float(ols.tvalues["const"])
    alpha_p = float(ols.pvalues["const"])

    return {
        "sharpe": sharpe,
        "ann_vol": ann_vol,
        "cum_ret": cum_ret,
        "cagr": cagr,
        "max_dd": max_dd,
        "profit_factor": pf,
        "alpha_ann": alpha_ann,
        "alpha_t": alpha_t,
        "alpha_p": alpha_p,
        "trades_count": trade_count,
        "daily_df": res_df,
    }


def main() -> None:
    print("=" * 105)
    print("HIGH-ROI MODEL UPGRADE EXPERIMENTS (REFINERS SHORT SLEEVE, 2018–2026)")
    print("=" * 105)

    ref_df, _, all_dates = load_datasets()
    fac_df = pd.read_parquet(FACILITY_PATH)
    px_matrix = load_factor_prices(all_dates)

    # 1. Compute Facility Robust Z-Scores
    print("\n[experiment] Computing Facility-Level Robust Z-Scores (rolling 252d MAD, 10d embargo)...")
    rob_scores = compute_facility_robust_z(fac_df)
    ref_df_aug = ref_df.merge(rob_scores, on=["ticker", "trading_date"], how="left")

    # ==========================================================================
    # EXPERIMENT A: Facility-Level Robust Z-Score vs. Ticker YoY Ratio
    # ==========================================================================
    print("\n" + "=" * 105)
    print("EXPERIMENT A: FACILITY-LEVEL ROBUST Z-SCORE vs. CURRENT YoY RATIO")
    print("=" * 105)

    # A1. Non-overlapping 10d Rank IC
    all_dates_set = set(all_dates[::10])
    no_m = ref_df_aug[ref_df_aug["trading_date"].isin(all_dates_set)].copy()
    no_clear = no_m[(no_m["cloud_cover_pct_weighted"] <= CLOUD_MAX) & (no_m["regime_label"] == "IDIOSYNCRATIC")]

    sub_yoy = no_clear.dropna(subset=["yoy_per_pass", "fwd_ret_10d_excess"])
    sub_rob = no_clear.dropna(subset=["s_rob", "fwd_ret_10d_excess"])

    ic_yoy, p_yoy = spearmanr(sub_yoy["yoy_per_pass"], sub_yoy["fwd_ret_10d_excess"])
    t_yoy = ic_yoy * np.sqrt((len(sub_yoy) - 2) / max(1e-12, 1.0 - ic_yoy**2))

    ic_rob, p_rob = spearmanr(sub_rob["s_rob"], sub_rob["fwd_ret_10d_excess"])
    t_rob = ic_rob * np.sqrt((len(sub_rob) - 2) / max(1e-12, 1.0 - ic_rob**2))

    print(f"Rank IC Comparison (Non-Overlapping 10d, Clear-Sky Idiosyncratic):")
    print(f"  Current Ticker YoY Ratio    : IC = {ic_yoy:+.4f} | t = {t_yoy:+.2f} | p = {p_yoy:.4f} (N={len(sub_yoy)})")
    print(f"  Facility Robust Z-Score     : IC = {ic_rob:+.4f} | t = {t_rob:+.2f} | p = {p_rob:.4f} (N={len(sub_rob)})")

    # A2. Daily MTM Simulation Comparison
    sim_yoy = run_refiners_daily_mtm(ref_df_aug, "yoy_per_pass", "XLE", all_dates, px_matrix)
    sim_rob = run_refiners_daily_mtm(ref_df_aug, "s_rob", "XLE", all_dates, px_matrix)

    print("\n--- Daily MTM Performance Table (Full 7-Refiner Universe, XLE Hedge) ---")
    header_a = f"{'Signal Metric':28s} | {'Sharpe':7s} | {'Cum Ret':8s} | {'CAGR':7s} | {'Max DD':7s} | {'PF':5s} | {'Trades':6s} | {'NW Alpha (ann)':15s} | {'NW t-stat'}"
    print(header_a)
    print("-" * len(header_a))
    print(
        f"{'Current Ticker YoY Ratio':28s} | {sim_yoy['sharpe']:+6.2f}  | {sim_yoy['cum_ret']:+7.2f}% | "
        f"{sim_yoy['cagr']:+6.2f}% | {sim_yoy['max_dd']:6.2f}% | {sim_yoy['profit_factor']:5.2f} | "
        f"{sim_yoy['trades_count']:6d} | {sim_yoy['alpha_ann']:+13.2f}%  | {sim_yoy['alpha_t']:+8.2f}"
    )
    print(
        f"{'Facility Robust Z-Score':28s} | {sim_rob['sharpe']:+6.2f}  | {sim_rob['cum_ret']:+7.2f}% | "
        f"{sim_rob['cagr']:+6.2f}% | {sim_rob['max_dd']:6.2f}% | {sim_rob['profit_factor']:5.2f} | "
        f"{sim_rob['trades_count']:6d} | {sim_rob['alpha_ann']:+13.2f}%  | {sim_rob['alpha_t']:+8.2f}"
    )

    # A3. Leave-One-Ticker-Out Stability (focusing on PBF)
    print("\n--- Leave-One-Ticker-Out Stability (Dropping PBF vs. Keeping PBF) ---")
    sim_yoy_nopbf = run_refiners_daily_mtm(ref_df_aug, "yoy_per_pass", "XLE", all_dates, px_matrix, excluded_ticker="PBF")
    sim_rob_nopbf = run_refiners_daily_mtm(ref_df_aug, "s_rob", "XLE", all_dates, px_matrix, excluded_ticker="PBF")
    print(f"  YoY Ratio with PBF      : Sharpe = {sim_yoy['sharpe']:+.2f} | Cum Ret = {sim_yoy['cum_ret']:+.2f}% | Max DD = {sim_yoy['max_dd']:.2f}%")
    print(f"  YoY Ratio without PBF   : Sharpe = {sim_yoy_nopbf['sharpe']:+.2f} | Cum Ret = {sim_yoy_nopbf['cum_ret']:+.2f}% | Max DD = {sim_yoy_nopbf['max_dd']:.2f}%")
    print(f"  Robust Z with PBF       : Sharpe = {sim_rob['sharpe']:+.2f} | Cum Ret = {sim_rob['cum_ret']:+.2f}% | Max DD = {sim_rob['max_dd']:.2f}%")
    print(f"  Robust Z without PBF    : Sharpe = {sim_rob_nopbf['sharpe']:+.2f} | Cum Ret = {sim_rob_nopbf['cum_ret']:+.2f}% | Max DD = {sim_rob_nopbf['max_dd']:.2f}%")

    # ==========================================================================
    # EXPERIMENT B: Peer-Refiner Basket Hedge vs. XLE Hedge
    # ==========================================================================
    print("\n" + "=" * 105)
    print("EXPERIMENT B: PEER-REFINER BASKET HEDGE vs. XLE SECTOR HEDGE")
    print("=" * 105)

    sim_peer = run_refiners_daily_mtm(ref_df_aug, "yoy_per_pass", "PEER_BASKET", all_dates, px_matrix)

    header_b = (
        f"{'Hedge Structure':26s} | {'Sharpe':7s} | {'Resid Vol':9s} | {'Cum Ret':8s} | "
        f"{'CAGR':7s} | {'Max DD':7s} | {'PF':5s} | {'NW Alpha (ann)':15s} | {'NW t-stat'}"
    )
    print(header_b)
    print("-" * len(header_b))
    print(
        f"{'XLE Sector ETF Hedge':26s} | {sim_yoy['sharpe']:+6.2f}  | {sim_yoy['ann_vol']:7.2f}%  | "
        f"{sim_yoy['cum_ret']:+7.2f}% | {sim_yoy['cagr']:+6.2f}% | {sim_yoy['max_dd']:6.2f}% | "
        f"{sim_yoy['profit_factor']:5.2f} | {sim_yoy['alpha_ann']:+13.2f}%  | {sim_yoy['alpha_t']:+8.2f}"
    )
    print(
        f"{'Peer-Refiner Basket Hedge':26s} | {sim_peer['sharpe']:+6.2f}  | {sim_peer['ann_vol']:7.2f}%  | "
        f"{sim_peer['cum_ret']:+7.2f}% | {sim_peer['cagr']:+6.2f}% | {sim_peer['max_dd']:6.2f}% | "
        f"{sim_peer['profit_factor']:5.2f} | {sim_peer['alpha_ann']:+13.2f}%  | {sim_peer['alpha_t']:+8.2f}"
    )
    print("-" * len(header_b))

    vol_reduc = (1.0 - sim_peer["ann_vol"] / sim_yoy["ann_vol"]) * 100.0
    print(f"\n[finding] Peer-Refiner Basket reduces portfolio residual volatility by {vol_reduc:+.1f}%.")
    print("=" * 105)


if __name__ == "__main__":
    main()
