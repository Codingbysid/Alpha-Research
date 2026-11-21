"""
Point-in-time Information Coefficient (IC) and decay audit for NASA FIRMS features.

Usage:
    PYTHONPATH=. python src/eval/ic_audit.py
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

GOLD_PATH = Path("data/gold/refinery_features.parquet")
MARKET_PATH = Path("data/silver/market_returns.parquet")

FEATURES = ["frp_zscore_30d", "frp_zscore_90d", "frp_yoy_ratio", "days_below_minus2_z"]
TARGETS = ["fwd_ret_1d_excess", "fwd_ret_5d_excess", "fwd_ret_10d_excess"]
CLOUD_MASK_MAX = 40.0


def calc_ic(df: pd.DataFrame, feat: str, target: str) -> dict[str, float]:
    sub = df[[feat, target]].dropna()
    n = len(sub)
    if n < 20 or sub[feat].nunique() < 3:
        return {"n": n, "ic": np.nan, "t_stat": np.nan, "p_val": np.nan}

    ic, p_val = spearmanr(sub[feat], sub[target])
    t_stat = ic * np.sqrt((n - 2) / max(1e-12, 1.0 - ic**2))
    return {"n": n, "ic": round(float(ic), 4), "t_stat": round(float(t_stat), 2), "p_val": round(float(p_val), 4)}


def run_audit() -> None:
    if not GOLD_PATH.exists() or not MARKET_PATH.exists():
        raise FileNotFoundError("Run feature_engine.py and market_data.py first.")

    gold = pd.read_parquet(GOLD_PATH)
    mkt = pd.read_parquet(MARKET_PATH)
    df = gold.merge(mkt, on=["ticker", "trading_date"], how="inner")

    if len(df) < 30:
        print(
            f"[audit] Joined {len(df)} rows — need historical NASA archive backfill "
            "(IDs 854106–854108) before IC statistics are meaningful."
        )
        return

    clear = df[df["cloud_cover_pct_weighted"] <= CLOUD_MASK_MAX].copy()
    print(f"[audit] Total joined rows: {len(df):,} | Clear-sky (<= {CLOUD_MASK_MAX:.0f}% cloud): {len(clear):,}\n")

    # 1. Feature x Horizon IC table (Clear-Sky vs Unfiltered)
    summary_rows = []
    for label, subset in [("ALL_DAYS", df), ("CLEAR_SKY", clear)]:
        for feat in FEATURES:
            for tgt in TARGETS:
                res = calc_ic(subset, feat, tgt)
                summary_rows.append({"filter": label, "feature": feat, "target": tgt, **res})

    print("=== POOLED SPEARMAN IC ===")
    print(pd.DataFrame(summary_rows).to_string(index=False))

    # 2. Annual stability check on primary signal (frp_zscore_90d -> 5d excess)
    clear["year"] = pd.to_datetime(clear["trading_date"]).dt.year
    yr_rows = []
    for yr, grp in clear.groupby("year"):
        res = calc_ic(grp, "frp_zscore_90d", "fwd_ret_5d_excess")
        yr_rows.append({"year": yr, **res})
    print("\n=== ANNUAL IC STABILITY (frp_zscore_90d -> fwd_ret_5d_excess, CLEAR SKY) ===")
    print(pd.DataFrame(yr_rows).to_string(index=False))

    # 3. Tail event audit: do severe clear-sky thermal drops predict negative excess returns?
    tail = clear[clear["frp_zscore_90d"] <= -2.0]
    normal = clear[clear["frp_zscore_90d"] > -2.0]
    print("\n=== SEVERE OUTAGE TAIL (frp_zscore_90d <= -2.0, CLEAR SKY) ===")
    for tgt in TARGETS:
        t_mean = tail[tgt].mean() * 100
        n_mean = normal[tgt].mean() * 100
        hit_neg = (tail[tgt] < 0).mean() * 100 if len(tail) else np.nan
        print(
            f"{tgt:18s} | n={len(tail):4d} | tail mean={t_mean:+.3f}% "
            f"| normal mean={n_mean:+.3f}% | % negative={hit_neg:.1f}%"
        )


if __name__ == "__main__":
    run_audit()
