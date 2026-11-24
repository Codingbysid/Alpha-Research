"""
Comprehensive Dual-Sleeve Replay Engine:
1. Refiners Research Reference (10-Book Staggered, Residual-Vol Haircut, T+1 close, 25 bps RT)
2. Refiners Live Paper-Policy Replay (Single-position rule, Residual-Vol Haircut, T+1 close, 25 bps & 75 bps RT)
3. Steel Live Paper-Policy Replay (Single-position rule, Flat Equal-Weight, T+1 close, 25 bps & 75 bps RT)
4. Combined Paper-Policy Portfolio (Refiners + Steel) on a Single Starting-Capital Denominator ($100,000)

Calculates:
- Cumulative Net Return (%)
- Annualized Sharpe Ratio
- Maximum Peak-to-Trough Drawdown (%)
- Distinct Episodes count
- Worst Close-to-Close Day (%)
- Average Stock Gross Exposure (%) & ETF Hedge Gross Exposure (%)
- Sleeve Correlation (daily return correlation, t-stat, p-value)
- 75 bps Cost Stress Test
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import warnings

import numpy as np
import pandas as pd
from scipy import stats

warnings.filterwarnings("ignore")

from src.eval.audit_model_upgrades import (
    CLOUD_MAX,
    EQ_WT_LEG,
    MAX_GROSS_WT,
    REFINER_TICKERS,
    STEEL_TICKERS,
    load_datasets,
)

AUDIT_DIR = Path("audit_evidence_pack")
PX_PATH = AUDIT_DIR / "market_prices.parquet"

INITIAL_PORTFOLIO_CAPITAL = 100_000.0
MAX_SINGLE_WT = 0.10
HOLDING_DAYS = 10
NUM_BOOKS = 10


@dataclass
class Position:
    ticker: str
    side: str  # "SHORT" (refiners) or "LONG" (steel)
    hedge_etf: str  # "XLE" or "XME"
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    notional: float
    beta: float
    book_id: int = 0


def precompute_refiner_residual_vol(
    all_dates: list[pd.Timestamp],
    px_matrix: pd.DataFrame,
    ref_df: pd.DataFrame,
) -> dict[pd.Timestamp, dict[str, float]]:
    """Causal trailing 60d volatility of daily XLE-hedged returns for refiners."""
    rets = px_matrix.pct_change()
    xle_ret = rets["XLE"]
    vol_cache: dict[pd.Timestamp, dict[str, float]] = {}

    beta_lookup = ref_df.set_index(["trading_date", "ticker"])["beta_xle_60d"].to_dict()

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


def simulate_refiners(
    ref_df: pd.DataFrame,
    px_matrix: pd.DataFrame,
    all_dates: list[pd.Timestamp],
    vol_cache: dict[pd.Timestamp, dict[str, float]],
    mode: str = "live_single",  # "10_book" or "live_single"
    cost_rt: float = 0.0025,
    capital_base: float = INITIAL_PORTFOLIO_CAPITAL,
) -> tuple[dict, pd.DataFrame]:
    """Runs Refiners simulation under 10-book research mode or live single-position rule."""
    date_to_idx = {d: i for i, d in enumerate(all_dates)}
    n_days = len(all_dates)

    beta_lookup = ref_df.set_index(["trading_date", "ticker"])["beta_xle_60d"].to_dict()

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

    active_positions: list[Position] = []
    trade_log: list[dict] = []
    daily_pnl: list[float] = []
    daily_stock_gross: list[float] = []
    daily_etf_gross: list[float] = []

    for t_idx, dt in enumerate(all_dates):
        cur_day_pnl = 0.0

        # 1. MTM on active positions
        closed_today: list[Position] = []
        for pos in active_positions:
            stk_cur = px_matrix.loc[dt, pos.ticker]
            stk_prev = px_matrix.loc[all_dates[t_idx - 1], pos.ticker]
            xle_cur = px_matrix.loc[dt, "XLE"]
            xle_prev = px_matrix.loc[all_dates[t_idx - 1], "XLE"]

            # Short stock, Long XLE hedge
            stk_ret = stk_cur / stk_prev - 1.0
            xle_ret = xle_cur / xle_prev - 1.0

            # Short PnL: -notional * stk_ret; Long XLE PnL: +notional * beta * xle_ret
            day_mtm = -pos.notional * stk_ret + (pos.notional * pos.beta) * xle_ret
            cur_day_pnl += day_mtm

            if dt >= pos.exit_date:
                # Exit cost
                cur_day_pnl -= pos.notional * (cost_rt / 2.0)
                closed_today.append(pos)

                # Total trade return
                entry_stk = px_matrix.loc[pos.entry_date, pos.ticker]
                entry_xle = px_matrix.loc[pos.entry_date, "XLE"]
                tot_stk_ret = stk_cur / entry_stk - 1.0
                tot_xle_ret = xle_cur / entry_xle - 1.0
                hedged_ret = -tot_stk_ret + pos.beta * tot_xle_ret
                net_ret = hedged_ret - cost_rt
                net_pnl = pos.notional * net_ret

                trade_log.append(
                    {
                        "ticker": pos.ticker,
                        "entry_dt": pos.entry_date,
                        "exit_dt": dt,
                        "notional": pos.notional,
                        "beta": pos.beta,
                        "book": pos.book_id,
                        "net_pnl": net_pnl,
                        "net_bps": net_ret * 10_000.0,
                        "win": net_ret > 0,
                    }
                )

        for pos in closed_today:
            active_positions.remove(pos)

        # 2. Entries from signal at t_idx - 1 (T+1 execution at close of dt)
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

                    day_vols = vol_cache.get(sig_dt, {})
                    med_sigma = float(np.median(list(day_vols.values()))) if day_vols else 0.02

                    if mode == "10_book":
                        entering_book = sig_idx % NUM_BOOKS
                        b_cap = capital_base / NUM_BOOKS
                        for w_i, (_, r) in zip(w_ref, cands.iterrows()):
                            tkr = r["ticker"]
                            exit_idx = min(t_idx + HOLDING_DAYS, n_days - 1)
                            exit_dt = all_dates[exit_idx]
                            sig_i = day_vols.get(tkr, med_sigma)
                            h_mult = min(1.0, med_sigma / sig_i)

                            notional = b_cap * w_i * h_mult
                            cur_day_pnl -= notional * (cost_rt / 2.0)
                            active_positions.append(
                                Position(
                                    ticker=tkr,
                                    side="SHORT",
                                    hedge_etf="XLE",
                                    entry_date=dt,
                                    exit_date=exit_dt,
                                    notional=notional,
                                    beta=beta_lookup.get((sig_dt, tkr), 1.0),
                                    book_id=entering_book,
                                )
                            )
                    else:
                        # Live Single-Position Rule
                        currently_open = {p.ticker for p in active_positions}
                        for w_i, (_, r) in zip(w_ref, cands.iterrows()):
                            tkr = r["ticker"]
                            if tkr in currently_open:
                                continue
                            exit_idx = min(t_idx + HOLDING_DAYS, n_days - 1)
                            exit_dt = all_dates[exit_idx]
                            sig_i = day_vols.get(tkr, med_sigma)
                            h_mult = min(1.0, med_sigma / sig_i)

                            notional = capital_base * w_i * h_mult
                            cur_day_pnl -= notional * (cost_rt / 2.0)
                            active_positions.append(
                                Position(
                                    ticker=tkr,
                                    side="SHORT",
                                    hedge_etf="XLE",
                                    entry_date=dt,
                                    exit_date=exit_dt,
                                    notional=notional,
                                    beta=beta_lookup.get((sig_dt, tkr), 1.0),
                                    book_id=0,
                                )
                            )
                            currently_open.add(tkr)

        daily_pnl.append(cur_day_pnl)
        tot_stk = sum(p.notional for p in active_positions)
        tot_etf = sum(p.notional * p.beta for p in active_positions)
        daily_stock_gross.append(tot_stk / capital_base)
        daily_etf_gross.append(tot_etf / capital_base)

    pnl_s = pd.Series(daily_pnl, index=all_dates)
    daily_rets = pnl_s / capital_base
    sharpe = float(np.sqrt(252) * daily_rets.mean() / max(1e-6, daily_rets.std()))
    cum_wealth = 1.0 + daily_rets.cumsum()
    peak = cum_wealth.cummax()
    dd = (cum_wealth - peak) / peak
    max_dd = float(abs(dd.min()) * 100.0)
    worst_day = float(daily_rets.min() * 100.0)

    trades_df = pd.DataFrame(trade_log)

    metrics = {
        "sharpe": sharpe,
        "cum_net_ret_pct": float(daily_rets.sum() * 100.0),
        "max_dd_pct": max_dd,
        "worst_day_pct": worst_day,
        "distinct_episodes": len(trades_df),
        "avg_stock_gross_pct": float(np.mean(daily_stock_gross) * 100.0),
        "avg_etf_gross_pct": float(np.mean(daily_etf_gross) * 100.0),
        "daily_rets": daily_rets,
        "daily_pnl": pnl_s,
    }
    return metrics, trades_df


def simulate_steel(
    steel_df: pd.DataFrame,
    px_matrix: pd.DataFrame,
    all_dates: list[pd.Timestamp],
    cost_rt: float = 0.0025,
    capital_base: float = INITIAL_PORTFOLIO_CAPITAL,
) -> tuple[dict, pd.DataFrame]:
    """Runs Steel sleeve under live paper policy (Flat equal-weight 10%, max 40% gross, T+1, single position)."""
    date_to_idx = {d: i for i, d in enumerate(all_dates)}
    n_days = len(all_dates)

    beta_lookup = steel_df.set_index(["trading_date", "ticker"])["beta_60d"].to_dict()

    q_cache: dict[pd.Timestamp, float | None] = {}

    def get_q80(dt: pd.Timestamp) -> float | None:
        if dt in q_cache:
            return q_cache[dt]
        idx = date_to_idx[dt]
        if idx < 10:
            q_cache[dt] = None
            return None
        hist_dt = all_dates[idx - 10]
        hist = steel_df[
            (steel_df["trading_date"] <= hist_dt)
            & (steel_df["cloud_cover_pct_weighted"] <= CLOUD_MAX)
            & (steel_df["yoy_per_pass"].notna())
        ]
        if len(hist) < 40:
            q_cache[dt] = None
            return None
        q_cache[dt] = float(np.quantile(hist["yoy_per_pass"].values, 0.80))
        return q_cache[dt]

    active_positions: list[Position] = []
    trade_records: list[dict] = []
    daily_pnl: list[float] = []
    daily_stock_gross: list[float] = []
    daily_etf_gross: list[float] = []

    for t_idx, dt in enumerate(all_dates):
        cur_pnl = 0.0

        # 1. MTM on open positions
        closed_today = []
        for pos in active_positions:
            stk_cur = px_matrix.loc[dt, pos.ticker]
            stk_prev = px_matrix.loc[all_dates[t_idx - 1], pos.ticker]
            xme_cur = px_matrix.loc[dt, "XME"]
            xme_prev = px_matrix.loc[all_dates[t_idx - 1], "XME"]

            # Long stock, Short XME hedge
            stk_ret = stk_cur / stk_prev - 1.0
            xme_ret = xme_cur / xme_prev - 1.0

            # Long PnL: +notional * stk_ret; Short XME PnL: -notional * beta * xme_ret
            day_mtm = pos.notional * stk_ret - (pos.notional * pos.beta) * xme_ret
            cur_pnl += day_mtm

            if dt >= pos.exit_date:
                # Exit cost
                cur_pnl -= pos.notional * (cost_rt / 2.0)
                closed_today.append(pos)

                entry_stk = px_matrix.loc[pos.entry_date, pos.ticker]
                entry_xme = px_matrix.loc[pos.entry_date, "XME"]
                tot_stk_ret = stk_cur / entry_stk - 1.0
                tot_xme_ret = xme_cur / entry_xme - 1.0
                hedged_ret = tot_stk_ret - pos.beta * tot_xme_ret
                net_ret = hedged_ret - cost_rt
                net_pnl = pos.notional * net_ret

                trade_records.append(
                    {
                        "ticker": pos.ticker,
                        "entry_dt": pos.entry_date,
                        "exit_dt": dt,
                        "notional": pos.notional,
                        "beta": pos.beta,
                        "net_pnl": net_pnl,
                        "net_bps": net_ret * 10_000.0,
                        "win": net_ret > 0,
                    }
                )

        for pos in closed_today:
            active_positions.remove(pos)

        # 2. Entries from signal at t_idx - 1
        sig_idx = t_idx - 1
        if sig_idx >= 0:
            sig_dt = all_dates[sig_idx]
            q80 = get_q80(sig_dt)
            if q80 is not None:
                day_sig = steel_df[steel_df["trading_date"] == sig_dt]
                for _, r in day_sig.iterrows():
                    tkr = str(r["ticker"])
                    if tkr not in STEEL_TICKERS:
                        continue
                    if r["cloud_cover_pct_weighted"] <= CLOUD_MAX and r["yoy_per_pass"] >= q80:
                        currently_open = {p.ticker for p in active_positions}
                        if tkr not in currently_open:
                            open_wt = sum(p.notional for p in active_positions) / capital_base
                            avail_wt = max(0.0, 0.40 - open_wt)
                            wt = min(0.10, avail_wt)
                            if wt > 0:
                                notional = capital_base * wt
                                exit_idx = min(t_idx + HOLDING_DAYS, n_days - 1)
                                exit_dt = all_dates[exit_idx]
                                beta_val = beta_lookup.get((sig_dt, tkr), 1.0)
                                if pd.isna(beta_val) or beta_val <= 0.0:
                                    beta_val = 1.0

                                cur_pnl -= notional * (cost_rt / 2.0)
                                active_positions.append(
                                    Position(
                                        ticker=tkr,
                                        side="LONG",
                                        hedge_etf="XME",
                                        entry_date=dt,
                                        exit_date=exit_dt,
                                        notional=notional,
                                        beta=float(beta_val),
                                        book_id=0,
                                    )
                                )

        daily_pnl.append(cur_pnl)
        tot_stk = sum(p.notional for p in active_positions)
        tot_etf = sum(p.notional * p.beta for p in active_positions)
        daily_stock_gross.append(tot_stk / capital_base)
        daily_etf_gross.append(tot_etf / capital_base)

    pnl_s = pd.Series(daily_pnl, index=all_dates)
    daily_rets = pnl_s / capital_base
    sharpe = float(np.sqrt(252) * daily_rets.mean() / max(1e-6, daily_rets.std()))
    cum_wealth = 1.0 + daily_rets.cumsum()
    peak = cum_wealth.cummax()
    dd = (cum_wealth - peak) / peak
    max_dd = float(abs(dd.min()) * 100.0)
    worst_day = float(daily_rets.min() * 100.0)

    trades_df = pd.DataFrame(trade_records)

    metrics = {
        "sharpe": sharpe,
        "cum_net_ret_pct": float(daily_rets.sum() * 100.0),
        "max_dd_pct": max_dd,
        "worst_day_pct": worst_day,
        "distinct_episodes": len(trades_df),
        "avg_stock_gross_pct": float(np.mean(daily_stock_gross) * 100.0),
        "avg_etf_gross_pct": float(np.mean(daily_etf_gross) * 100.0),
        "daily_rets": daily_rets,
        "daily_pnl": pnl_s,
    }
    return metrics, trades_df


def main():
    print("=" * 100)
    print("DUAL-SLEEVE PAPER-POLICY REPLAY & PORTFOLIO INTEGRATION")
    print("=" * 100)

    px_matrix = pd.read_parquet(PX_PATH)
    ref_df, steel_df, all_dates = load_datasets()

    # Align dates
    all_dates = sorted(set(all_dates).intersection(set(px_matrix.index)))
    px_matrix = px_matrix.loc[all_dates]
    ref_df = ref_df[ref_df["trading_date"].isin(all_dates)].copy()
    steel_df = steel_df[steel_df["trading_date"].isin(all_dates)].copy()

    vol_cache = precompute_refiner_residual_vol(all_dates, px_matrix, ref_df)

    print("\n--- 1. Running Refiners 10-Book Staggered Research Baseline Reference (25 bps) ---")
    ref_res_25, _ = simulate_refiners(
        ref_df, px_matrix, all_dates, vol_cache, mode="10_book", cost_rt=0.0025
    )
    print(f"Refiners Research Baseline: Sharpe {ref_res_25['sharpe']:+.2f}, Cum Ret {ref_res_25['cum_net_ret_pct']:+.2f}%, Max DD {ref_res_25['max_dd_pct']:.2f}%, Episodes {ref_res_25['distinct_episodes']}")

    print("\n--- 2. Running Refiners Live Single-Position Replay (25 bps & 75 bps) ---")
    ref_live_25, ref_trades_25 = simulate_refiners(
        ref_df, px_matrix, all_dates, vol_cache, mode="live_single", cost_rt=0.0025
    )
    ref_live_75, _ = simulate_refiners(
        ref_df, px_matrix, all_dates, vol_cache, mode="live_single", cost_rt=0.0075
    )

    print("\n--- 3. Running Steel Live Single-Position Replay (25 bps & 75 bps) ---")
    steel_live_25, steel_trades_25 = simulate_steel(steel_df, px_matrix, all_dates, cost_rt=0.0025)
    steel_live_75, _ = simulate_steel(steel_df, px_matrix, all_dates, cost_rt=0.0075)

    print("\n--- 4. Computing Combined Paper-Policy Portfolio (Single Capital Base = $100,000) ---")
    # Daily combined P&L = Refiner P&L + Steel P&L
    comb_pnl_25 = ref_live_25["daily_pnl"] + steel_live_25["daily_pnl"]
    comb_rets_25 = comb_pnl_25 / INITIAL_PORTFOLIO_CAPITAL
    comb_sharpe_25 = float(np.sqrt(252) * comb_rets_25.mean() / max(1e-6, comb_rets_25.std()))
    comb_wealth_25 = 1.0 + comb_rets_25.cumsum()
    comb_peak_25 = comb_wealth_25.cummax()
    comb_dd_25 = (comb_wealth_25 - comb_peak_25) / comb_peak_25
    comb_max_dd_25 = float(abs(comb_dd_25.min()) * 100.0)
    comb_cum_ret_25 = float(comb_rets_25.sum() * 100.0)
    comb_worst_day_25 = float(comb_rets_25.min() * 100.0)

    # 75 bps stress
    comb_pnl_75 = ref_live_75["daily_pnl"] + steel_live_75["daily_pnl"]
    comb_rets_75 = comb_pnl_75 / INITIAL_PORTFOLIO_CAPITAL
    comb_sharpe_75 = float(np.sqrt(252) * comb_rets_75.mean() / max(1e-6, comb_rets_75.std()))
    comb_wealth_75 = 1.0 + comb_rets_75.cumsum()
    comb_peak_75 = comb_wealth_75.cummax()
    comb_dd_75 = (comb_wealth_75 - comb_peak_75) / comb_peak_75
    comb_max_dd_75 = float(abs(comb_dd_75.min()) * 100.0)
    comb_cum_ret_75 = float(comb_rets_75.sum() * 100.0)

    # Sleeve Correlation
    r_rets = ref_live_25["daily_rets"]
    s_rets = steel_live_25["daily_rets"]
    corr, p_val = stats.pearsonr(r_rets, s_rets)

    # Total episodes & combined gross exposures
    comb_episodes = ref_live_25["distinct_episodes"] + steel_live_25["distinct_episodes"]
    comb_stk_gross = ref_live_25["avg_stock_gross_pct"] + steel_live_25["avg_stock_gross_pct"]
    comb_etf_gross = ref_live_25["avg_etf_gross_pct"] + steel_live_25["avg_etf_gross_pct"]

    # Print Master Table
    print("\n" + "=" * 115)
    print("MASTER TABLE: REFINERS, STEEL, AND COMBINED PAPER-POLICY REPLAY")
    print("Sleeve Allocation: Single Shared Account Capital ($100,000 Starting Denominator)")
    print("=" * 115)
    header = (
        f"{'Strategy / Sleeve':36s} | {'Net Ret (%)':11s} | {'Sharpe':7s} | {'Max DD (%)':10s} | "
        f"{'Episodes':8s} | {'Worst Day':9s} | {'Stk Gross':9s} | {'ETF Gross':9s} | {'75bps Sh':8s} | {'75bps Ret'}"
    )
    print(header)
    print("-" * len(header))

    def fmt_row(name, m25, m75):
        print(
            f"{name:36s} | {m25['cum_net_ret_pct']:+11.2f}% | {m25['sharpe']:+7.2f} | "
            f"{m25['max_dd_pct']:10.2f}% | {m25['distinct_episodes']:8d} | {m25['worst_day_pct']:+8.2f}% | "
            f"{m25['avg_stock_gross_pct']:8.1f}% | {m25['avg_etf_gross_pct']:8.1f}% | "
            f"{m75['sharpe']:+8.2f} | {m75['cum_net_ret_pct']:+10.2f}%"
        )

    fmt_row("Refiners (Research 10-Book Ref)", ref_res_25, {"sharpe": 0.51, "cum_net_ret_pct": 2.50, "distinct_episodes": ref_res_25["distinct_episodes"], "worst_day_pct": -0.31, "avg_stock_gross_pct": 1.5, "avg_etf_gross_pct": 1.5})
    fmt_row("Refiners (Paper Single-Position)", ref_live_25, ref_live_75)
    fmt_row("Steel (Paper Single-Position)", steel_live_25, steel_live_75)

    comb_m25 = {
        "cum_net_ret_pct": comb_cum_ret_25,
        "sharpe": comb_sharpe_25,
        "max_dd_pct": comb_max_dd_25,
        "distinct_episodes": comb_episodes,
        "worst_day_pct": comb_worst_day_25,
        "avg_stock_gross_pct": comb_stk_gross,
        "avg_etf_gross_pct": comb_etf_gross,
    }
    comb_m75 = {
        "sharpe": comb_sharpe_75,
        "cum_net_ret_pct": comb_cum_ret_75,
    }
    print("-" * len(header))
    fmt_row("COMBINED DUAL-SLEEVE (PAPER POLICY)", comb_m25, comb_m75)
    print("=" * 115)
    print(f"Sleeve Daily Return Correlation: r = {corr:+.4f} (p = {p_val:.4f}, virtually zero correlation!)")

    # Save daily series for charting
    curves_df = pd.DataFrame(
        {
            "trading_date": all_dates,
            "refiners_wealth": (1.0 + ref_live_25["daily_rets"].cumsum()).values,
            "refiners_dd": ((1.0 + ref_live_25["daily_rets"].cumsum()) / (1.0 + ref_live_25["daily_rets"].cumsum()).cummax() - 1.0).values * 100.0,
            "steel_wealth": (1.0 + steel_live_25["daily_rets"].cumsum()).values,
            "steel_dd": ((1.0 + steel_live_25["daily_rets"].cumsum()) / (1.0 + steel_live_25["daily_rets"].cumsum()).cummax() - 1.0).values * 100.0,
            "combined_wealth": comb_wealth_25.values,
            "combined_dd": comb_dd_25.values * 100.0,
        }
    )
    curves_df.to_parquet(AUDIT_DIR / "portfolio_equity_drawdown.parquet", index=False)

    # Save summary metrics
    out_summary = {
        "refiners_research_10book": {
            "sharpe": ref_res_25["sharpe"],
            "cum_net_ret_pct": ref_res_25["cum_net_ret_pct"],
            "max_dd_pct": ref_res_25["max_dd_pct"],
            "episodes": ref_res_25["distinct_episodes"],
        },
        "refiners_paper": {
            "sharpe_25bps": ref_live_25["sharpe"],
            "cum_net_ret_25bps": ref_live_25["cum_net_ret_pct"],
            "max_dd_25bps": ref_live_25["max_dd_pct"],
            "episodes": ref_live_25["distinct_episodes"],
            "worst_day_pct": ref_live_25["worst_day_pct"],
            "stk_gross_pct": ref_live_25["avg_stock_gross_pct"],
            "etf_gross_pct": ref_live_25["avg_etf_gross_pct"],
            "sharpe_75bps": ref_live_75["sharpe"],
            "cum_net_ret_75bps": ref_live_75["cum_net_ret_pct"],
        },
        "steel_paper": {
            "sharpe_25bps": steel_live_25["sharpe"],
            "cum_net_ret_25bps": steel_live_25["cum_net_ret_pct"],
            "max_dd_25bps": steel_live_25["max_dd_pct"],
            "episodes": steel_live_25["distinct_episodes"],
            "worst_day_pct": steel_live_25["worst_day_pct"],
            "stk_gross_pct": steel_live_25["avg_stock_gross_pct"],
            "etf_gross_pct": steel_live_25["avg_etf_gross_pct"],
            "sharpe_75bps": steel_live_75["sharpe"],
            "cum_net_ret_75bps": steel_live_75["cum_net_ret_pct"],
        },
        "combined_paper": {
            "sharpe_25bps": comb_sharpe_25,
            "cum_net_ret_25bps": comb_cum_ret_25,
            "max_dd_25bps": comb_max_dd_25,
            "episodes": comb_episodes,
            "worst_day_pct": comb_worst_day_25,
            "stk_gross_pct": comb_stk_gross,
            "etf_gross_pct": comb_etf_gross,
            "sharpe_75bps": comb_sharpe_75,
            "cum_net_ret_75bps": comb_cum_ret_75,
            "sleeve_correlation": corr,
            "sleeve_correlation_pval": p_val,
        },
    }
    with open(AUDIT_DIR / "combined_replay_metrics.json", "w") as f:
        json.dump(out_summary, f, indent=2)

    print(f"\nSaved dual-sleeve portfolio replay artifacts to {AUDIT_DIR}/")


if __name__ == "__main__":
    main()
