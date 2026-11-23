"""
True Daily Marked-to-Market (MTM) Portfolio Engine & T+1 Replay.
Allocates 1/10th of capital to each of the 10 staggered books, marks all open positions
(Refiners Short Q5 Conviction vs XLE + Steel Long Q5 Flat vs XME) to market DAILY using adj_close,
deducts 25 bps round-trip transaction costs on entry/exit, and computes from that single daily net return series:
- Daily Annualized Sharpe Ratio (daily * sqrt(252))
- Annualized CAGR (%), Total Cumulative Net Return (%), Maximum Drawdown (%)
- Profit Factor
- Factor-Regression Alpha (annualized intercept vs SPY, XLE, XME) with Newey-West HAC t-stat and p-value.
- Full timing sensitivity check: T+0 (MOC Close) vs T+1 (Next-Session Close).

Usage:
    PYTHONPATH=. python3 -W ignore src/eval/audit_daily_mtm.py
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import statsmodels.api as sm
import yfinance as yf

from src.eval.audit_model_upgrades import (
    CLOUD_MAX,
    EQ_WT_LEG,
    MAX_GROSS_WT,
    REFINER_TICKERS,
    STEEL_TICKERS,
    load_datasets,
)

INITIAL_PORTFOLIO_CAPITAL = 100_000.0
NUM_BOOKS = 10
HOLDING_DAYS = 10
COST_RT = 0.0025  # 25 bps round-trip (borrow + execution slippage + fees)


@dataclass
class Position:
    ticker: str
    sleeve: str  # 'REFINER' or 'STEEL'
    side: str  # 'SHORT' or 'LONG'
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    notional: float  # Dollar capital allocated to this position
    beta: float  # Rolling 60d beta to benchmark ETF
    hedge_ticker: str  # 'XLE' or 'XME'
    entry_cost_paid: bool = False
    exit_cost_paid: bool = False


def load_factor_prices(all_dates: list[pd.Timestamp]) -> pd.DataFrame:
    """Loads daily adjusted close prices for all stocks, hedge ETFs, and SPY."""
    symbols = REFINER_TICKERS + ["XLE"] + STEEL_TICKERS + ["XME", "SPY"]
    raw = yf.download(symbols, start="2017-06-01", auto_adjust=False, progress=False)
    field = "Adj Close" if "Adj Close" in raw.columns.get_level_values(0) else "Close"
    px = raw[field].copy()
    px.index = pd.to_datetime(px.index).tz_localize(None).normalize()
    px = px.sort_index().ffill().dropna(how="all")
    return px


def run_daily_mtm_simulation(
    ref_df: pd.DataFrame,
    steel_df: pd.DataFrame,
    all_dates: list[pd.Timestamp],
    px_matrix: pd.DataFrame,
    execution_lag: int = 0,  # 0 for T+0 Close, 1 for T+1 Close
    size_mult: float = 1.0,
    num_books: int = NUM_BOOKS,
    entry_stride: int | None = None,
) -> dict:
    """
    Executes a true daily marked-to-market backtest across 10 staggered books.
    Returns daily portfolio value, daily return series, and performance metrics.
    """
    date_to_idx = {d: i for i, d in enumerate(all_dates)}
    n_days = len(all_dates)

    # Equal capital split across staggered books; size_mult scales each ticket notional.
    n_books = max(1, int(num_books))
    book_cap = {b: INITIAL_PORTFOLIO_CAPITAL / n_books for b in range(n_books)}
    book_positions: dict[int, list[Position]] = {b: [] for b in range(n_books)}

    # Precompute causal quantiles
    q_cache_ref: dict[pd.Timestamp, tuple[float, float] | None] = {}
    q_cache_steel: dict[pd.Timestamp, float | None] = {}

    def get_ref_cutoffs(dt: pd.Timestamp) -> tuple[float, float] | None:
        if dt in q_cache_ref:
            return q_cache_ref[dt]
        idx = date_to_idx[dt]
        if idx < 10:
            q_cache_ref[dt] = None
            return None
        hist_end_dt = all_dates[idx - 10]
        hist = ref_df[
            (ref_df["trading_date"] <= hist_end_dt)
            & (ref_df["cloud_cover_pct_weighted"] <= CLOUD_MAX)
            & (ref_df["yoy_per_pass"].notna())
        ]
        if len(hist) < 40:
            q_cache_ref[dt] = None
            return None
        q80 = float(np.quantile(hist["yoy_per_pass"].values, 0.80))
        q95 = float(np.quantile(hist["yoy_per_pass"].values, 0.95))
        q_cache_ref[dt] = (q80, q95)
        return q_cache_ref[dt]

    def get_steel_cutoffs(dt: pd.Timestamp) -> float | None:
        if dt in q_cache_steel:
            return q_cache_steel[dt]
        idx = date_to_idx[dt]
        if idx < 10:
            q_cache_steel[dt] = None
            return None
        hist_end_dt = all_dates[idx - 10]
        hist = steel_df[
            (steel_df["trading_date"] <= hist_end_dt)
            & (steel_df["cloud_cover_pct_weighted"] <= CLOUD_MAX)
            & (steel_df["yoy_per_pass"].notna())
        ]
        if len(hist) < 40:
            q_cache_steel[dt] = None
            return None
        q80 = float(np.quantile(hist["yoy_per_pass"].values, 0.80))
        q_cache_steel[dt] = q80
        return q_cache_steel[dt]

    daily_records: list[dict] = []
    total_trades_entered = 0

    # Step through every single trading day
    for t_idx, dt in enumerate(all_dates):
        # 1. Check if any staggered book enters new positions today
        # For execution_lag == 0, signal is generated and entered on dt
        # For execution_lag == 1, signal generated on dt-1 enters on dt
        sig_idx = t_idx - execution_lag

        if sig_idx >= 0:
            sig_dt = all_dates[sig_idx]
            # Determine which book enters on sig_dt
            entering_book = sig_idx % n_books
            stride = n_books if entry_stride is None else max(1, int(entry_stride))
            allow_entry = (sig_idx % stride) == 0 if entry_stride is not None else True

            # Check if this book has capacity and candidate signals
            ref_cuts = get_ref_cutoffs(sig_dt) if allow_entry else None
            steel_cut = get_steel_cutoffs(sig_dt) if allow_entry else None

            new_positions: list[Position] = []

            # A. Refiners Short Q5 Candidates
            if ref_cuts is not None:
                q80, q95 = ref_cuts
                ref_cands = ref_df[
                    (ref_df["trading_date"] == sig_dt)
                    & (ref_df["cloud_cover_pct_weighted"] <= CLOUD_MAX)
                    & (ref_df["yoy_per_pass"] >= q80)
                    & (ref_df["regime_label"] == "IDIOSYNCRATIC")
                    & (ref_df["crack_ok"].fillna(False))
                ].copy()

                if not ref_cands.empty:
                    denom = max(1e-6, q95 - q80)
                    mults = np.clip((ref_cands["yoy_per_pass"].values - q80) / denom, 0.5, 1.5)
                    raw_w = EQ_WT_LEG * mults
                    sc = min(1.0, MAX_GROSS_WT / max(1e-6, np.sum(raw_w)))
                    w_ref = raw_w * sc

                    # Book capital currently available
                    b_equity = max(0.0, book_cap[entering_book])
                    if b_equity <= 0:
                        ref_cands = ref_cands.iloc[0:0]
                    for w_i, (_, r) in zip(w_ref, ref_cands.iterrows()):
                        exit_idx = min(t_idx + HOLDING_DAYS, n_days - 1)
                        exit_dt = all_dates[exit_idx]
                        pos = Position(
                            ticker=r["ticker"],
                            sleeve="REFINER",
                            side="SHORT",
                            entry_date=dt,
                            exit_date=exit_dt,
                            notional=b_equity * w_i * 0.5 * size_mult,  # 50% sleeve allocation
                            beta=float(r["beta_60d"]) if pd.notna(r["beta_60d"]) else 1.0,
                            hedge_ticker="XLE",
                        )
                        new_positions.append(pos)
                        total_trades_entered += 1

            # B. Steel Long Q5 Candidates
            if steel_cut is not None:
                q80 = steel_cut
                steel_cands = steel_df[
                    (steel_df["trading_date"] == sig_dt)
                    & (steel_df["cloud_cover_pct_weighted"] <= CLOUD_MAX)
                    & (steel_df["yoy_per_pass"] >= q80)
                ].copy()

                if not steel_cands.empty:
                    raw_w = np.full(len(steel_cands), EQ_WT_LEG)
                    sc = min(1.0, MAX_GROSS_WT / max(1e-6, np.sum(raw_w)))
                    w_steel = raw_w * sc

                    b_equity = max(0.0, book_cap[entering_book])
                    if b_equity <= 0:
                        steel_cands = steel_cands.iloc[0:0]
                    for w_i, (_, r) in zip(w_steel, steel_cands.iterrows()):
                        exit_idx = min(t_idx + HOLDING_DAYS, n_days - 1)
                        exit_dt = all_dates[exit_idx]
                        pos = Position(
                            ticker=r["ticker"],
                            sleeve="STEEL",
                            side="LONG",
                            entry_date=dt,
                            exit_date=exit_dt,
                            notional=b_equity * w_i * 0.5 * size_mult,  # 50% sleeve allocation
                            beta=float(r["beta_60d"]) if pd.notna(r["beta_60d"]) else 1.0,
                            hedge_ticker="XME",
                        )
                        new_positions.append(pos)
                        total_trades_entered += 1

            book_positions[entering_book].extend(new_positions)

        # 2. Mark all active positions to market on day dt
        if t_idx == 0:
            # First day baseline
            daily_records.append(
                {
                    "trading_date": dt,
                    "portfolio_equity": INITIAL_PORTFOLIO_CAPITAL,
                    "daily_ret": 0.0,
                    "daily_pnl": 0.0,
                    "active_positions": 0,
                    "gross_notional": 0.0,
                }
            )
            continue

        prev_dt = all_dates[t_idx - 1]
        daily_pnl = 0.0
        active_count = 0
        daily_gross = 0.0

        # Mark positions to market across each of the 10 books
        for b in range(n_books):
            surviving_positions: list[Position] = []
            for pos in book_positions[b]:
                # Check if position was already open yesterday
                # Positions entered today (dt) will generate PnL starting tomorrow
                if pos.entry_date >= dt:
                    surviving_positions.append(pos)
                    continue

                active_count += 1
                # Daily asset returns from prev_dt to dt
                px_cur = px_matrix.loc[dt, pos.ticker]
                px_prev = px_matrix.loc[prev_dt, pos.ticker]
                ret_stock = (px_cur / px_prev - 1.0) if px_prev > 0 else 0.0

                h_cur = px_matrix.loc[dt, pos.hedge_ticker]
                h_prev = px_matrix.loc[prev_dt, pos.hedge_ticker]
                ret_hedge = (h_cur / h_prev - 1.0) if h_prev > 0 else 0.0

                # Hedged position daily return
                if pos.side == "SHORT":
                    # Short stock, Long beta * XLE
                    pos_daily_ret = -ret_stock + pos.beta * ret_hedge
                else:
                    # Long stock, Short beta * XME
                    pos_daily_ret = +ret_stock - pos.beta * ret_hedge

                # Dollar PnL
                dollar_pnl = pos.notional * pos_daily_ret

                # Transaction costs: 12.5 bps on first day, 12.5 bps on exit day
                if not pos.entry_cost_paid:
                    dollar_pnl -= pos.notional * (COST_RT / 2.0)
                    pos.entry_cost_paid = True

                if dt >= pos.exit_date and not pos.exit_cost_paid:
                    dollar_pnl -= pos.notional * (COST_RT / 2.0)
                    pos.exit_cost_paid = True

                daily_pnl += dollar_pnl
                book_cap[b] += dollar_pnl
                daily_gross += abs(pos.notional)

                # Keep position if still active
                if dt < pos.exit_date:
                    surviving_positions.append(pos)

            book_positions[b] = surviving_positions

        prev_equity = daily_records[-1]["portfolio_equity"]
        if prev_equity <= 0:
            current_equity = 0.0
            daily_ret = 0.0
            daily_pnl = 0.0
            for b in range(n_books):
                book_positions[b] = []
                book_cap[b] = 0.0
        else:
            current_equity = prev_equity + daily_pnl
            daily_ret = daily_pnl / prev_equity
            if current_equity <= 0:
                current_equity = 0.0
                daily_ret = -1.0
                for b in range(n_books):
                    book_positions[b] = []
                    book_cap[b] = 0.0

        daily_records.append(
            {
                "trading_date": dt,
                "portfolio_equity": current_equity,
                "daily_ret": daily_ret,
                "daily_pnl": daily_pnl,
                "active_positions": active_count,
                "gross_notional": daily_gross,
            }
        )

    res_df = pd.DataFrame(daily_records)
    res_df["trading_date"] = pd.to_datetime(res_df["trading_date"])
    res_df = res_df.set_index("trading_date")

    # Metrics calculation
    d_rets = res_df["daily_ret"].iloc[1:]  # drop day 0
    mean_d = float(d_rets.mean())
    sd_d = float(d_rets.std(ddof=1))
    daily_sharpe = (mean_d / sd_d * np.sqrt(252.0)) if sd_d > 0 else 0.0

    eq_series = res_df["portfolio_equity"].values
    final_equity = eq_series[-1]
    cum_ret = (final_equity / INITIAL_PORTFOLIO_CAPITAL - 1.0) * 100.0

    n_years = len(d_rets) / 252.0
    cagr = ((final_equity / INITIAL_PORTFOLIO_CAPITAL) ** (1.0 / n_years) - 1.0) * 100.0 if n_years > 0 else 0.0

    # Drawdown
    peaks = np.maximum.accumulate(eq_series)
    dds = (peaks - eq_series) / peaks
    max_dd = float(np.max(dds)) * 100.0

    # Profit factor
    gross_win = float(d_rets[d_rets > 0].sum())
    gross_loss = float(abs(d_rets[d_rets < 0].sum()))
    pf = gross_win / gross_loss if gross_loss > 0 else np.nan

    # Factor regression with Newey-West HAC (skipped if the path ruined / degenerate)
    alpha_ann = alpha_t = alpha_p = beta_spy = beta_xle = beta_xme = float("nan")
    try:
        spy_ret = px_matrix["SPY"].pct_change().loc[d_rets.index].fillna(0.0)
        xle_ret = px_matrix["XLE"].pct_change().loc[d_rets.index].fillna(0.0)
        xme_ret = px_matrix["XME"].pct_change().loc[d_rets.index].fillna(0.0)
        X = pd.DataFrame({"const": 1.0, "SPY": spy_ret, "XLE": xle_ret, "XME": xme_ret})
        ols = sm.OLS(d_rets, X).fit(cov_type="HAC", cov_kwds={"maxlags": 10})
        alpha_daily = float(ols.params["const"])
        alpha_ann = alpha_daily * 252.0 * 100.0
        alpha_t = float(ols.tvalues["const"])
        alpha_p = float(ols.pvalues["const"])
        beta_spy = float(ols.params["SPY"])
        beta_xle = float(ols.params["XLE"])
        beta_xme = float(ols.params["XME"])
    except Exception:
        pass

    return {
        "daily_df": res_df,
        "sharpe": daily_sharpe,
        "cum_ret": cum_ret,
        "cagr": cagr,
        "max_dd": max_dd,
        "profit_factor": pf,
        "alpha_ann": alpha_ann,
        "alpha_t": alpha_t,
        "alpha_p": alpha_p,
        "beta_spy": beta_spy,
        "beta_xle": beta_xle,
        "beta_xme": beta_xme,
        "trades_count": total_trades_entered,
    }


def main() -> None:
    print("=" * 100)
    print("TRUE DAILY MARKED-TO-MARKET (MTM) PORTFOLIO AUDIT & TIMING SENSITIVITY")
    print("10-Book Staggered Ensemble | Refiners Short Q5 Conviction + Steel Long Q5 Flat (2018–2026)")
    print("=" * 100)

    ref_df, steel_df, all_dates = load_datasets()
    print(f"[mtm] Loaded {len(all_dates):,} common trading dates ({all_dates[0].date()} to {all_dates[-1].date()})")

    print("[mtm] Fetching SPY and factor prices...")
    px_matrix = load_factor_prices(all_dates)

    # 1. Run T+0 MOC Execution
    print("\n[mtm] Running T+0 Execution (Market-on-Close same-day entry)...")
    res_t0 = run_daily_mtm_simulation(ref_df, steel_df, all_dates, px_matrix, execution_lag=0)

    # 2. Run T+1 Next-Session Execution
    print("[mtm] Running T+1 Execution (Next-day Close entry sensitivity check)...")
    res_t1 = run_daily_mtm_simulation(ref_df, steel_df, all_dates, px_matrix, execution_lag=1)

    # Print Summary Comparison Table
    print("\n" + "=" * 100)
    print("TABLE 1: TRUE DAILY MARKED-TO-MARKET (MTM) PERFORMANCE & FACTOR ALPHA")
    print("=" * 100)
    header = (
        f"{'Execution Mode':18s} | {'Sharpe':7s} | {'Cum Ret':8s} | {'CAGR':7s} | "
        f"{'Max DD':7s} | {'PF':5s} | {'NW Alpha (ann)':15s} | {'NW t-stat':10s} | {'p-value':8s} | {'Beta SPY':8s}"
    )
    print(header)
    print("-" * len(header))

    modes = [("T+0 (Same-Day MOC)", res_t0), ("T+1 (Next-Day MOC)", res_t1)]
    for name, r in modes:
        print(
            f"{name:18s} | {r['sharpe']:+6.2f}  | {r['cum_ret']:+7.2f}% | {r['cagr']:+6.2f}% | "
            f"{r['max_dd']:6.2f}% | {r['profit_factor']:5.2f} | {r['alpha_ann']:+13.2f}%  | "
            f"{r['alpha_t']:+9.2f}  | {r['alpha_p']:8.4f} | {r['beta_spy']:+8.2f}"
        )

    print("-" * len(header))
    print(f"\nFactor Betas under T+0: SPY={res_t0['beta_spy']:+.3f}, XLE={res_t0['beta_xle']:+.3f}, XME={res_t0['beta_xme']:+.3f}")
    print(f"Factor Betas under T+1: SPY={res_t1['beta_spy']:+.3f}, XLE={res_t1['beta_xle']:+.3f}, XME={res_t1['beta_xme']:+.3f}")
    print("=" * 100)


if __name__ == "__main__":
    main()
