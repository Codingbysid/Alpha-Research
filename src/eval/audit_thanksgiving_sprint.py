"""
Audit Thanksgiving Sprint:
1. Table 1 — Exact Refiner-Only T+1 Baseline Reconciliation:
   - (a) Overlapping 10-Book Simulator (research baseline)
   - (b) Live Single-Position Rule (skip ticker if already open)
   - At 25 bps and 75 bps RT: Episodes, T+1 Sharpe, CAGR, Max DD, Avg Gross Exposure (%), NW Alpha t-stat.

2. Table 2 — Residual-Vol Haircut Sizing (Causal 60-Day XLE-Hedged Vol):
   - Scaling w_i^* = w_i * min(1, median(sigma_j,t) / sigma_i,t)
   - Compare Core, Residual-Vol Haircut, and Uniformly Scaled Core (matched to exact same gross exposure)
   - T+1 Sharpe, CAGR, Max DD, Worst Overnight Loss, Worst 5% Daily Loss (CVaR 5%), paired block-bootstrap p-value.

3. Table 3 — Earnings-Window Repricing (Li & Venkatachalam 2022):
   - Quarterly earnings dates via yfinance for all 7 refiners.
   - Flagged Outage Quarters (gated flare episode strictly ending >= 2 trading days before earnings) vs. Unflagged Quarters.
   - XLE-hedged return from T-1 close to T+1 close (short direction): N, Mean bps, Win Rate %, two-sample t-stat & p-value.

Usage:
    PYTHONPATH=. python3 -W ignore src/eval/audit_thanksgiving_sprint.py
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import warnings

import numpy as np
import pandas as pd
from scipy import stats
import statsmodels.api as sm
import yfinance as yf

warnings.filterwarnings("ignore")

from src.eval.audit_daily_mtm import load_factor_prices
from src.eval.audit_model_upgrades import (
    CLOUD_MAX,
    EQ_WT_LEG,
    MAX_GROSS_WT,
    REFINER_TICKERS,
    load_datasets,
)

INITIAL_PORTFOLIO_CAPITAL = 100_000.0
NUM_BOOKS = 10
HOLDING_DAYS = 10
MAX_SINGLE_WT = 0.10


@dataclass
class Position:
    ticker: str
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    notional: float
    beta: float
    entry_cost_paid: bool = False
    exit_cost_paid: bool = False


# ==============================================================================
# 1. Trailing 60-Day Residual Volatility Engine
# ==============================================================================

def precompute_residual_vol_matrix(
    all_dates: list[pd.Timestamp],
    px_matrix: pd.DataFrame,
    ref_df: pd.DataFrame,
) -> dict[pd.Timestamp, dict[str, float]]:
    """
    Computes causal trailing 60d volatility of daily XLE-hedged returns for each refiner:
        e_{i,tau} = r_{i,tau} - beta_{i,t} * r_{XLE,tau} for tau in [t-59, t].
    Returns dict: date -> {ticker: sigma_{i,t}}.
    """
    rets = px_matrix.pct_change()
    xle_ret = rets["XLE"]
    vol_cache: dict[pd.Timestamp, dict[str, float]] = {}

    # Pre-index betas from ref_df
    beta_lookup = (
        ref_df.set_index(["trading_date", "ticker"])["beta_xle_60d"].to_dict()
    )

    for t_idx, dt in enumerate(all_dates):
        if t_idx < 60:
            vol_cache[dt] = {tkr: 0.02 for tkr in REFINER_TICKERS}
            continue

        hist_dts = all_dates[t_idx - 59 : t_idx + 1]
        xle_hist = xle_ret.loc[hist_dts]
        day_vols = {}

        for tkr in REFINER_TICKERS:
            stk_hist = rets.loc[hist_dts, tkr]
            beta_val = beta_lookup.get((dt, tkr), np.nan)
            if pd.isna(beta_val) or beta_val <= 0.0:
                cov = float(np.cov(stk_hist.dropna(), xle_hist.dropna())[0, 1])
                var_xle = float(np.var(xle_hist.dropna()))
                beta_val = cov / max(1e-6, var_xle)
                beta_val = float(np.clip(beta_val, 0.2, 3.0))

            resid = stk_hist - beta_val * xle_hist
            sigma = float(resid.std(ddof=1))
            day_vols[tkr] = max(sigma, 1e-4)

        vol_cache[dt] = day_vols

    return vol_cache


# ==============================================================================
# 2. Daily MTM Simulation Engine (Refiners-Only, Supports 10-Book & Live Rules)
# ==============================================================================

def run_refiners_simulation(
    ref_df: pd.DataFrame,
    all_dates: list[pd.Timestamp],
    px_matrix: pd.DataFrame,
    vol_cache: dict[pd.Timestamp, dict[str, float]],
    mode: str = "10_book",  # "10_book" or "live_single"
    cost_rt: float = 0.0025,
    haircut_mode: str = "none",  # "none", "haircut", "uniform"
    uniform_scale: float = 1.0,
) -> dict:
    date_to_idx = {d: i for i, d in enumerate(all_dates)}
    n_days = len(all_dates)

    # Precompute causal cutoffs
    q_cache: dict[pd.Timestamp, tuple[float, float] | None] = {}

    def get_cutoffs(dt: pd.Timestamp) -> tuple[float, float] | None:
        if dt in q_cache:
            return q_cache[dt]
        idx = date_to_idx[dt]
        if idx < 10:
            q_cache[dt] = None
            return None
        hist_end = all_dates[idx - 10]
        hist = ref_df[
            (ref_df["trading_date"] <= hist_end)
            & (ref_df["cloud_cover_pct_weighted"] <= CLOUD_MAX)
            & (ref_df["yoy_per_pass"].notna())
        ]
        if len(hist) < 40:
            q_cache[dt] = None
            return None
        q80 = float(np.quantile(hist["yoy_per_pass"].values, 0.80))
        q95 = float(np.quantile(hist["yoy_per_pass"].values, 0.95))
        q_cache[dt] = (q80, q95)
        return q_cache[dt]

    # Portfolio state
    if mode == "10_book":
        n_books = NUM_BOOKS
        book_positions: dict[int, list[Position]] = {b: [] for b in range(n_books)}
    else:
        n_books = 1
        live_positions: list[Position] = []

    daily_records: list[dict] = []
    total_trades_entered = 0
    trade_log: list[dict] = []

    for t_idx, dt in enumerate(all_dates):
        # T+1 execution: signal observed at t_idx - 1, executed at close of t_idx
        sig_idx = t_idx - 1

        if sig_idx >= 0:
            sig_dt = all_dates[sig_idx]
            cuts = get_cutoffs(sig_dt)

            if cuts is not None:
                q80, q95 = cuts
                cands = ref_df[
                    (ref_df["trading_date"] == sig_dt)
                    & (ref_df["cloud_cover_pct_weighted"] <= CLOUD_MAX)
                    & (ref_df["yoy_per_pass"] >= q80)
                    & (ref_df["regime_label"] == "IDIOSYNCRATIC")
                    & (ref_df["crack_ok"].fillna(False))
                ].copy()

                if not cands.empty:
                    denom = max(1e-6, q95 - q80)
                    mults = np.clip((cands["yoy_per_pass"].values - q80) / denom, 0.5, 1.5)
                    raw_w = EQ_WT_LEG * mults
                    sc = min(1.0, MAX_GROSS_WT / max(1e-6, np.sum(raw_w)))
                    w_ref = np.minimum(raw_w * sc, MAX_SINGLE_WT)

                    # Compute residual vol haircut if enabled
                    day_vols = vol_cache.get(sig_dt, {})
                    med_sigma = float(np.median(list(day_vols.values()))) if day_vols else 0.02

                    if mode == "10_book":
                        entering_book = sig_idx % NUM_BOOKS
                        b_cap = INITIAL_PORTFOLIO_CAPITAL / NUM_BOOKS
                        for w_i, (_, r) in zip(w_ref, cands.iterrows()):
                            tkr = r["ticker"]
                            exit_idx = min(t_idx + HOLDING_DAYS, n_days - 1)
                            exit_dt = all_dates[exit_idx]

                            sig_i = day_vols.get(tkr, med_sigma)
                            if haircut_mode == "haircut":
                                h_mult = min(1.0, med_sigma / sig_i)
                            elif haircut_mode == "uniform":
                                h_mult = uniform_scale
                            else:
                                h_mult = 1.0

                            notional = b_cap * w_i * h_mult
                            pos = Position(
                                ticker=tkr,
                                entry_date=dt,
                                exit_date=exit_dt,
                                notional=notional,
                                beta=float(r["beta_xle_60d"]) if pd.notna(r["beta_xle_60d"]) else 1.0,
                            )
                            book_positions[entering_book].append(pos)
                            total_trades_entered += 1
                            trade_log.append(
                                {
                                    "sig_dt": sig_dt,
                                    "entry_dt": dt,
                                    "exit_dt": exit_dt,
                                    "ticker": tkr,
                                    "notional": notional,
                                    "book": entering_book,
                                }
                            )
                    else:
                        # Live Single-Position Rule: skip ticker if already has an open position
                        currently_open_tickers = {
                            p.ticker for p in live_positions if p.exit_date > dt and p.entry_date <= dt
                        }
                        for w_i, (_, r) in zip(w_ref, cands.iterrows()):
                            tkr = r["ticker"]
                            if tkr in currently_open_tickers:
                                continue  # Skip ticker already open

                            exit_idx = min(t_idx + HOLDING_DAYS, n_days - 1)
                            exit_dt = all_dates[exit_idx]

                            sig_i = day_vols.get(tkr, med_sigma)
                            if haircut_mode == "haircut":
                                h_mult = min(1.0, med_sigma / sig_i)
                            elif haircut_mode == "uniform":
                                h_mult = uniform_scale
                            else:
                                h_mult = 1.0

                            notional = INITIAL_PORTFOLIO_CAPITAL * w_i * h_mult
                            pos = Position(
                                ticker=tkr,
                                entry_date=dt,
                                exit_date=exit_dt,
                                notional=notional,
                                beta=float(r["beta_xle_60d"]) if pd.notna(r["beta_xle_60d"]) else 1.0,
                            )
                            live_positions.append(pos)
                            currently_open_tickers.add(tkr)
                            total_trades_entered += 1
                            trade_log.append(
                                {
                                    "sig_dt": sig_dt,
                                    "entry_dt": dt,
                                    "exit_dt": exit_dt,
                                    "ticker": tkr,
                                    "notional": notional,
                                    "book": 0,
                                }
                            )

        # 2. Mark positions to market on day dt
        if t_idx == 0:
            daily_records.append(
                {
                    "trading_date": dt,
                    "daily_pnl": 0.0,
                    "daily_ret": 0.0,
                    "gross_notional": 0.0,
                }
            )
            continue

        prev_dt = all_dates[t_idx - 1]
        daily_pnl = 0.0
        daily_gross = 0.0

        all_active_positions = (
            [p for b in range(n_books) for p in book_positions[b]]
            if mode == "10_book"
            else live_positions
        )

        for pos in all_active_positions:
            if pos.entry_date >= dt:
                continue

            px_cur = px_matrix.loc[dt, pos.ticker]
            px_prev = px_matrix.loc[prev_dt, pos.ticker]
            ret_stock = (px_cur / px_prev - 1.0) if px_prev > 0 else 0.0

            xle_cur = px_matrix.loc[dt, "XLE"]
            xle_prev = px_matrix.loc[prev_dt, "XLE"]
            ret_xle = (xle_cur / xle_prev - 1.0) if xle_prev > 0 else 0.0

            # Short stock, Long beta * XLE
            pos_ret = -ret_stock + pos.beta * ret_xle
            pnl = pos.notional * pos_ret

            # Friction
            if not pos.entry_cost_paid:
                pnl -= pos.notional * (cost_rt / 2.0)
                pos.entry_cost_paid = True

            if dt >= pos.exit_date and not pos.exit_cost_paid:
                pnl -= pos.notional * (cost_rt / 2.0)
                pos.exit_cost_paid = True

            daily_pnl += pnl
            daily_gross += abs(pos.notional)

        # Prune expired positions
        if mode == "10_book":
            for b in range(n_books):
                book_positions[b] = [p for p in book_positions[b] if p.exit_date > dt or not p.exit_cost_paid]
        else:
            live_positions = [p for p in live_positions if p.exit_date > dt or not p.exit_cost_paid]

        # Constant-notional daily return
        daily_ret = daily_pnl / INITIAL_PORTFOLIO_CAPITAL

        daily_records.append(
            {
                "trading_date": dt,
                "daily_pnl": daily_pnl,
                "daily_ret": daily_ret,
                "gross_notional": daily_gross,
            }
        )

    res_df = pd.DataFrame(daily_records).set_index("trading_date")
    d_rets = res_df["daily_ret"].iloc[1:]

    # Compute stats
    mean_d = float(d_rets.mean())
    sd_d = float(d_rets.std(ddof=1))
    sharpe = (mean_d / sd_d * np.sqrt(252.0)) if sd_d > 0 else 0.0

    cum_pnl = float(res_df["daily_pnl"].sum())
    cum_ret = cum_pnl / INITIAL_PORTFOLIO_CAPITAL
    n_years = len(d_rets) / 252.0
    cagr = ((1.0 + cum_ret) ** (1.0 / n_years) - 1.0) * 100.0 if (1.0 + cum_ret > 0 and n_years > 0) else 0.0

    # Drawdown on constant-notional equity
    equity_curve = INITIAL_PORTFOLIO_CAPITAL + res_df["daily_pnl"].cumsum().values
    peaks = np.maximum.accumulate(equity_curve)
    dds = (peaks - equity_curve) / peaks
    max_dd = float(np.max(dds)) * 100.0

    avg_gross_pct = float(res_df["gross_notional"].mean() / INITIAL_PORTFOLIO_CAPITAL * 100.0)

    # Worst Overnight & CVaR 5%
    worst_overnight_pct = float(d_rets.min() * 100.0)
    q05 = np.percentile(d_rets.values, 5.0)
    cvar_05_pct = float(d_rets[d_rets <= q05].mean() * 100.0)

    # Newey-West Alpha t-stat vs SPY & XLE
    spy_rets = px_matrix["SPY"].pct_change().loc[d_rets.index].fillna(0.0)
    xle_rets = px_matrix["XLE"].pct_change().loc[d_rets.index].fillna(0.0)
    X = pd.DataFrame({"const": 1.0, "SPY": spy_rets, "XLE": xle_rets})
    nw_t = np.nan
    try:
        model = sm.OLS(d_rets, X).fit(cov_type="HAC", cov_kwds={"maxlags": 10})
        nw_t = float(model.tvalues["const"])
    except Exception:
        pass

    # Count distinct episodes: group triggers by ticker and cluster contiguous/overlapping dates
    episodes_count = 0
    t_df = pd.DataFrame(trade_log)
    if not t_df.empty:
        if mode == "live_single":
            episodes_count = len(t_df)
        else:
            # For 10-book simulator, an episode is a contiguous sequence of signals <= 5 days apart
            for _, grp in t_df.groupby("ticker"):
                dts = sorted(grp["sig_dt"].unique())
                if not dts:
                    continue
                eps = 1
                for i in range(1, len(dts)):
                    gap = (pd.Timestamp(dts[i]) - pd.Timestamp(dts[i - 1])).days
                    if gap > 7:
                        eps += 1
                episodes_count += eps

    return {
        "sharpe": sharpe,
        "cagr": cagr,
        "max_dd": max_dd,
        "avg_gross_pct": avg_gross_pct,
        "nw_alpha_t": nw_t,
        "distinct_episodes": episodes_count,
        "total_trades": total_trades_entered,
        "worst_overnight_pct": worst_overnight_pct,
        "cvar_05_pct": cvar_05_pct,
        "daily_rets": d_rets,
    }


# ==============================================================================
# 3. Bootstrap Significance for Table 2
# ==============================================================================

def calc_paired_bootstrap_p(
    d_rets_target: pd.Series,
    d_rets_base: pd.Series,
    block_len: int = 10,
    n_draws: int = 2000,
) -> float:
    """Computes block-bootstrap p-value for H0: Sharpe_target <= Sharpe_base."""
    r1 = d_rets_target.values
    r0 = d_rets_base.values
    n = len(r1)
    n_blocks = int(np.ceil(n / block_len))
    rng = np.random.default_rng(42)

    diffs = np.empty(n_draws, dtype=float)
    for b in range(n_draws):
        starts = rng.integers(0, n - block_len + 1, size=n_blocks)
        idx = np.concatenate([np.arange(s, s + block_len) for s in starts])[:n]

        sample1, sample0 = r1[idx], r0[idx]
        sd1, sd0 = np.std(sample1, ddof=1), np.std(sample0, ddof=1)
        sh1 = (np.mean(sample1) / sd1 * np.sqrt(252.0)) if sd1 > 0 else 0.0
        sh0 = (np.mean(sample0) / sd0 * np.sqrt(252.0)) if sd0 > 0 else 0.0
        diffs[b] = sh1 - sh0

    # One-sided p-value: fraction of draws where target does NOT outperform base
    p_val = float(np.mean(diffs <= 0.0))
    return p_val


# ==============================================================================
# 4. Table 3: Earnings-Window Repricing Engine (Li & Venkatachalam 2022)
# ==============================================================================

def run_earnings_study(
    ref_df: pd.DataFrame,
    all_dates: list[pd.Timestamp],
    px_matrix: pd.DataFrame,
) -> pd.DataFrame:
    """
    Evaluates short-direction XLE-hedged returns over [T-1 close, T+1 close]
    for Flagged Outage Quarters vs. Unflagged Quarters across all 7 refiners.
    """
    date_to_idx = {pd.Timestamp(d).normalize(): i for i, d in enumerate(all_dates)}
    n_days = len(all_dates)

    # 1. Identify all causal gated Short Q5 flare triggers
    triggers = []
    for idx in range(10, n_days):
        dt = all_dates[idx]
        hist_end = all_dates[idx - 10]
        hist = ref_df[
            (ref_df["trading_date"] <= hist_end)
            & (ref_df["cloud_cover_pct_weighted"] <= CLOUD_MAX)
            & (ref_df["yoy_per_pass"].notna())
        ]
        if len(hist) < 40:
            continue
        q80 = float(np.quantile(hist["yoy_per_pass"].values, 0.80))
        cands = ref_df[
            (ref_df["trading_date"] == dt)
            & (ref_df["cloud_cover_pct_weighted"] <= CLOUD_MAX)
            & (ref_df["yoy_per_pass"] >= q80)
            & (ref_df["regime_label"] == "IDIOSYNCRATIC")
            & (ref_df["crack_ok"].fillna(False))
        ]
        for _, r in cands.iterrows():
            # In T+1 execution, holding ends on idx + 1 + 10
            exit_dt = all_dates[min(idx + 1 + HOLDING_DAYS, n_days - 1)]
            triggers.append(
                {
                    "ticker": r["ticker"],
                    "sig_date": dt,
                    "exit_date": exit_dt,
                }
            )
    trig_df = pd.DataFrame(triggers)

    # 2. Iterate through each issuer's earnings announcements
    quarter_records: list[dict] = []

    for tkr in REFINER_TICKERS:
        t = yf.Ticker(tkr)
        ed = t.get_earnings_dates(limit=40)
        if ed is None or ed.empty:
            continue

        # Filter to 2018–2026
        ed = ed[(ed.index >= "2018-01-01") & (ed.index <= "2026-11-23")].sort_index()
        ed_list = list(ed.index)
        tkr_trigs = trig_df[trig_df["ticker"] == tkr]

        for i in range(len(ed_list)):
            cur_ed = ed_list[i]
            prev_ed = ed_list[i - 1] if i > 0 else (cur_ed - pd.Timedelta(days=90))

            cur_dt = pd.Timestamp(cur_ed).tz_localize(None)
            hour = cur_dt.hour
            cur_ed_date = cur_dt.normalize()
            prev_ed_date = pd.Timestamp(prev_ed).tz_localize(None).normalize()

            # Find matching trading day index for earnings announcement
            cur_idx = None
            for step in range(5):
                cand_dt = cur_ed_date - pd.Timedelta(days=step)
                if cand_dt in date_to_idx:
                    cur_idx = date_to_idx[cand_dt]
                    break

            if cur_idx is None or cur_idx < 2 or cur_idx >= n_days - 2:
                continue

            # Determine event study window [T-1, T+1]
            if hour < 12:
                # BMO (Before Market Open): T_0 is cur_idx, T_{-1} is cur_idx - 1, T_{+1} is cur_idx + 1
                t_minus_1_idx = cur_idx - 1
                t_plus_1_idx = cur_idx + 1
            else:
                # AMC (After Market Close): T_{-1} is cur_idx, T_{+1} is cur_idx + 2
                t_minus_1_idx = cur_idx
                t_plus_1_idx = cur_idx + 2

            t_minus_1_dt = all_dates[t_minus_1_idx]
            t_plus_1_dt = all_dates[t_plus_1_idx]

            # Price returns over earnings window [T-1 close, T+1 close]
            px_m1 = px_matrix.loc[t_minus_1_dt, tkr]
            px_p1 = px_matrix.loc[t_plus_1_dt, tkr]
            xle_m1 = px_matrix.loc[t_minus_1_dt, "XLE"]
            xle_p1 = px_matrix.loc[t_plus_1_dt, "XLE"]

            if px_m1 <= 0 or px_p1 <= 0 or xle_m1 <= 0 or xle_p1 <= 0:
                continue

            ret_stock = px_p1 / px_m1 - 1.0
            ret_xle = xle_p1 / xle_m1 - 1.0

            # 60d trailing beta at T-1
            beta_val = np.nan
            hist_d = ref_df[(ref_df["trading_date"] == t_minus_1_dt) & (ref_df["ticker"] == tkr)]
            if not hist_d.empty and pd.notna(hist_d["beta_xle_60d"].iloc[0]):
                beta_val = float(hist_d["beta_xle_60d"].iloc[0])
            else:
                beta_val = 1.0

            # Hedged return in SHORT direction: -R_stock + beta * R_XLE
            hedged_short_ret = -ret_stock + beta_val * ret_xle
            hedged_short_bps = hedged_short_ret * 10_000.0

            # Flagging check: Outage episode during quarter strictly ending >= 2 trading days before earnings
            # Strictly ending >= 2 days before T_0 means exit_date <= all_dates[cur_idx - 2]
            cutoff_dt = all_dates[cur_idx - 2]
            matching_episodes = tkr_trigs[
                (tkr_trigs["sig_date"] > prev_ed_date) & (tkr_trigs["exit_date"] <= cutoff_dt)
            ]
            is_flagged = not matching_episodes.empty

            quarter_records.append(
                {
                    "ticker": tkr,
                    "earnings_dt": cur_ed,
                    "is_flagged": is_flagged,
                    "hedged_short_bps": hedged_short_bps,
                    "is_win": hedged_short_bps > 0.0,
                    "ret_stock": ret_stock,
                    "ret_xle": ret_xle,
                    "beta": beta_val,
                }
            )

    return pd.DataFrame(quarter_records)


# ==============================================================================
# Main Runner & Table Formatting
# ==============================================================================

def main() -> None:
    print("=" * 105)
    print("AUDIT THANKSGIVING SPRINT: 7-REFINER UNIVERSE (2018–2026, T+1 EXECUTION)")
    print("=" * 105)

    print("\n[ingest] Loading datasets and historical prices...")
    r_df, s_df, all_dates = load_datasets()
    px_matrix = load_factor_prices(all_dates)

    print("[ingest] Precomputing trailing 60d XLE-hedged residual volatility matrix...")
    vol_cache = precompute_residual_vol_matrix(all_dates, px_matrix, r_df)

    # --------------------------------------------------------------------------
    # TABLE 1: Exact Refiner-Only T+1 Baseline Reconciliation
    # --------------------------------------------------------------------------
    print("\n[sim] Running Table 1 simulations across entry rules and transaction friction...")

    sim_10b_25 = run_refiners_simulation(
        r_df, all_dates, px_matrix, vol_cache, mode="10_book", cost_rt=0.0025
    )
    sim_10b_75 = run_refiners_simulation(
        r_df, all_dates, px_matrix, vol_cache, mode="10_book", cost_rt=0.0075
    )
    sim_live_25 = run_refiners_simulation(
        r_df, all_dates, px_matrix, vol_cache, mode="live_single", cost_rt=0.0025
    )
    sim_live_75 = run_refiners_simulation(
        r_df, all_dates, px_matrix, vol_cache, mode="live_single", cost_rt=0.0075
    )

    print("\n" + "=" * 105)
    print("TABLE 1: EXACT REFINER-ONLY T+1 BASELINE RECONCILIATION (2018–2026)")
    print("=" * 105)
    header_t1 = (
        f"{'Entry Rule & Friction':<32} | {'Episodes':<9} | {'T+1 Sharpe':<10} | {'CAGR':<8} | "
        f"{'Max DD':<8} | {'Avg Gross':<10} | {'NW Alpha t':<10}"
    )
    print(header_t1)
    print("-" * 105)

    rows_t1 = [
        (
            "(a) 10-Book Simulator (25 bps)",
            sim_10b_25["distinct_episodes"],
            f"{sim_10b_25['sharpe']:+.2f}",
            f"{sim_10b_25['cagr']:+.2f}%",
            f"{sim_10b_25['max_dd']:.2f}%",
            f"{sim_10b_25['avg_gross_pct']:.1f}%",
            f"{sim_10b_25['nw_alpha_t']:+.2f}",
        ),
        (
            "(a) 10-Book Simulator (75 bps)",
            sim_10b_75["distinct_episodes"],
            f"{sim_10b_75['sharpe']:+.2f}",
            f"{sim_10b_75['cagr']:+.2f}%",
            f"{sim_10b_75['max_dd']:.2f}%",
            f"{sim_10b_75['avg_gross_pct']:.1f}%",
            f"{sim_10b_75['nw_alpha_t']:+.2f}",
        ),
        (
            "(b) Live Single-Position (25 bps)",
            sim_live_25["distinct_episodes"],
            f"{sim_live_25['sharpe']:+.2f}",
            f"{sim_live_25['cagr']:+.2f}%",
            f"{sim_live_25['max_dd']:.2f}%",
            f"{sim_live_25['avg_gross_pct']:.1f}%",
            f"{sim_live_25['nw_alpha_t']:+.2f}",
        ),
        (
            "(b) Live Single-Position (75 bps)",
            sim_live_75["distinct_episodes"],
            f"{sim_live_75['sharpe']:+.2f}",
            f"{sim_live_75['cagr']:+.2f}%",
            f"{sim_live_75['max_dd']:.2f}%",
            f"{sim_live_75['avg_gross_pct']:.1f}%",
            f"{sim_live_75['nw_alpha_t']:+.2f}",
        ),
    ]
    for r in rows_t1:
        print(f"{r[0]:<32} | {r[1]:<9} | {r[2]:<10} | {r[3]:<8} | {r[4]:<8} | {r[5]:<10} | {r[6]:<10}")
    print("=" * 105)

    # --------------------------------------------------------------------------
    # TABLE 2: Residual-Vol Haircut Sizing
    # --------------------------------------------------------------------------
    print("\n[sim] Running Table 2 Residual-Vol Haircut sizing simulations...")

    sim_core = sim_10b_25
    sim_haircut = run_refiners_simulation(
        r_df, all_dates, px_matrix, vol_cache, mode="10_book", cost_rt=0.0025, haircut_mode="haircut"
    )

    # Match uniform core gross exposure to haircut gross exposure exactly
    scale_ratio = sim_haircut["avg_gross_pct"] / max(1e-4, sim_core["avg_gross_pct"])
    sim_uniform = run_refiners_simulation(
        r_df,
        all_dates,
        px_matrix,
        vol_cache,
        mode="10_book",
        cost_rt=0.0025,
        haircut_mode="uniform",
        uniform_scale=scale_ratio,
    )

    # Block bootstrap p-values
    p_vs_core = calc_paired_bootstrap_p(sim_haircut["daily_rets"], sim_core["daily_rets"], block_len=10)
    p_vs_uniform = calc_paired_bootstrap_p(sim_haircut["daily_rets"], sim_uniform["daily_rets"], block_len=10)

    print("\n" + "=" * 115)
    print("TABLE 2: RESIDUAL-VOL HAIRCUT SIZING COMPARISON (REFINERS-ONLY, 25 BPS RT)")
    print("=" * 115)
    header_t2 = (
        f"{'Model Variation':<28} | {'T+1 Sharpe':<10} | {'CAGR':<8} | {'Max DD':<8} | "
        f"{'Worst Night':<11} | {'CVaR 5%':<9} | {'Avg Gross':<10} | {'Bootstrap p':<11}"
    )
    print(header_t2)
    print("-" * 115)

    rows_t2 = [
        (
            "Core Baseline",
            f"{sim_core['sharpe']:+.2f}",
            f"{sim_core['cagr']:+.2f}%",
            f"{sim_core['max_dd']:.2f}%",
            f"{sim_core['worst_overnight_pct']:+.2f}%",
            f"{sim_core['cvar_05_pct']:+.2f}%",
            f"{sim_core['avg_gross_pct']:.1f}%",
            "—",
        ),
        (
            "Residual-Vol Haircut",
            f"{sim_haircut['sharpe']:+.2f}",
            f"{sim_haircut['cagr']:+.2f}%",
            f"{sim_haircut['max_dd']:.2f}%",
            f"{sim_haircut['worst_overnight_pct']:+.2f}%",
            f"{sim_haircut['cvar_05_pct']:+.2f}%",
            f"{sim_haircut['avg_gross_pct']:.1f}%",
            f"p = {p_vs_core:.3f}",
        ),
        (
            "Uniformly Scaled Core",
            f"{sim_uniform['sharpe']:+.2f}",
            f"{sim_uniform['cagr']:+.2f}%",
            f"{sim_uniform['max_dd']:.2f}%",
            f"{sim_uniform['worst_overnight_pct']:+.2f}%",
            f"{sim_uniform['cvar_05_pct']:+.2f}%",
            f"{sim_uniform['avg_gross_pct']:.1f}%",
            f"p = {p_vs_uniform:.3f}",
        ),
    ]
    for r in rows_t2:
        print(f"{r[0]:<28} | {r[1]:<10} | {r[2]:<8} | {r[3]:<8} | {r[4]:<11} | {r[5]:<9} | {r[6]:<10} | {r[7]:<11}")
    print("=" * 115)

    # --------------------------------------------------------------------------
    # TABLE 3: Earnings-Window Repricing
    # --------------------------------------------------------------------------
    print("\n[sim] Evaluating Table 3 quarterly earnings repricing study (Li & Venkatachalam 2022)...")
    ea_df = run_earnings_study(r_df, all_dates, px_matrix)

    flagged = ea_df[ea_df["is_flagged"]]
    unflagged = ea_df[~ea_df["is_flagged"]]

    n_f, n_u = len(flagged), len(unflagged)
    mean_bps_f = float(flagged["hedged_short_bps"].mean()) if n_f > 0 else 0.0
    mean_bps_u = float(unflagged["hedged_short_bps"].mean()) if n_u > 0 else 0.0

    win_rate_f = float(flagged["is_win"].mean() * 100.0) if n_f > 0 else 0.0
    win_rate_u = float(unflagged["is_win"].mean() * 100.0) if n_u > 0 else 0.0

    t_stat, p_val = stats.ttest_ind(
        flagged["hedged_short_bps"], unflagged["hedged_short_bps"], equal_var=False
    )

    print("\n" + "=" * 90)
    print("TABLE 3: EARNINGS-WINDOW REPRICING STUDY (LI & VENKATACHALAM 2022)")
    print("Window: [T-1 Close -> T+1 Close] | Direction: Short Target / Long Beta*XLE")
    print("=" * 90)
    header_t3 = f"{'Quarter Category':<25} | {'Quarters (N)':<14} | {'Mean Hedged BPS':<16} | {'Win Rate (Short)':<18}"
    print(header_t3)
    print("-" * 90)
    print(f"{'Flagged Outage Quarters':<25} | {n_f:<14} | {mean_bps_f:+7.1f} bps      | {win_rate_f:5.1f}%")
    print(f"{'Unflagged Quarters':<25} | {n_u:<14} | {mean_bps_u:+7.1f} bps      | {win_rate_u:5.1f}%")
    print(f"{'Spread (Flagged - Unflag)':<25} | {n_f + n_u:<14} | {mean_bps_f - mean_bps_u:+7.1f} bps      | {win_rate_f - win_rate_u:+5.1f}%")
    print("-" * 90)
    print(f"Two-Sample Welch's t-statistic : {t_stat:+.2f}")
    print(f"Two-Sample p-value (two-sided) : {p_val:.4f} {'(p < 0.05)' if p_val < 0.05 else ''}")
    print("=" * 90 + "\n")


if __name__ == "__main__":
    main()
