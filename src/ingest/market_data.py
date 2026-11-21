"""
Pulls US refiner equities, XLE sector ETF, WTI crude, and OVX volatility,
and computes point-in-time rolling XLE betas + forward excess returns.

Usage:
    PYTHONPATH=. python src/ingest/market_data.py
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

REFINERS = ["VLO", "MPC", "PSX", "DINO", "PBF", "DK", "CVI"]
BENCHMARKS = ["XLE", "CL=F", "^OVX"]
START_DATE = "2018-01-01"
BETA_WINDOW = 60
HORIZONS = (1, 5, 10)
OUT_PATH = Path("data/silver/market_returns.parquet")


def _extract_adj_close(raw: pd.DataFrame, tickers: list[str]) -> pd.DataFrame:
    """Handles both MultiIndex and flat DataFrames across yfinance versions."""
    if isinstance(raw.columns, pd.MultiIndex):
        level0 = raw.columns.get_level_values(0)
        field = "Adj Close" if "Adj Close" in level0 else "Close"
        px = raw[field].copy()
    else:
        field = "Adj Close" if "Adj Close" in raw.columns else "Close"
        px = raw[[field]].rename(columns={field: tickers[0]})
    return px.dropna(how="all")


def build_market_panel(start_date: str = START_DATE) -> pd.DataFrame:
    all_tickers = REFINERS + BENCHMARKS
    raw = yf.download(
        all_tickers,
        start=start_date,
        auto_adjust=False,
        progress=False,
        threads=True,
    )
    px = _extract_adj_close(raw, all_tickers)
    px.index = pd.to_datetime(px.index).tz_localize(None).normalize()
    px = px.sort_index()

    rets = px.pct_change()
    xle_ret = rets["XLE"]
    xle_var = xle_ret.rolling(BETA_WINDOW, min_periods=30).var()

    # Forward benchmark returns from Close(t) -> Close(t + h)
    xle_fwd = {h: px["XLE"].shift(-h) / px["XLE"] - 1.0 for h in HORIZONS}

    rows: list[pd.DataFrame] = []
    for ticker in REFINERS:
        stock_px = px[ticker]
        stock_ret = rets[ticker]

        # Strictly backward-looking beta computed through Close(t)
        beta_60d = (
            stock_ret.rolling(BETA_WINDOW, min_periods=30).cov(xle_ret)
            / xle_var.replace(0.0, np.nan)
        )

        df = pd.DataFrame(
            {
                "trading_date": px.index,
                "ticker": ticker,
                "adj_close": stock_px.values,
                "ret_1d": stock_ret.values,
                "vol_20d_ann": (stock_ret.rolling(20).std() * np.sqrt(252)).values,
                "beta_xle_60d": beta_60d.values,
                "wti_close": px["CL=F"].values if "CL=F" in px else np.nan,
                "ovx_level": px["^OVX"].values if "^OVX" in px else np.nan,
            }
        )

        for h in HORIZONS:
            fwd_raw = stock_px.shift(-h) / stock_px - 1.0
            df[f"fwd_ret_{h}d_raw"] = fwd_raw.values
            df[f"fwd_ret_{h}d_excess"] = (fwd_raw - beta_60d * xle_fwd[h]).values

        rows.append(df)

    panel = (
        pd.concat(rows, ignore_index=True)
        .dropna(subset=["adj_close"])
        .sort_values(["ticker", "trading_date"])
        .reset_index(drop=True)
    )

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    panel.to_parquet(OUT_PATH, index=False)
    print(f"[market] Wrote {len(panel):,} rows ({panel['trading_date'].min().date()} -> {panel['trading_date'].max().date()}) to {OUT_PATH}")
    return panel


if __name__ == "__main__":
    build_market_panel()
