"""
Stress-tests whether the frp_yoy_ratio signal survives:
1) Per-pass normalization (removes the March 2023 NOAA-21 3rd-satellite jump)
2) Non-overlapping 10-day return blocks (removes autocorrelation t-stat inflation)
3) Year-by-year stability

Usage:
    PYTHONPATH=. python src/eval/stress_test_yoy.py
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

SILVER = pd.read_parquet("data/silver/facility_daily.parquet")
GOLD = pd.read_parquet("data/gold/refinery_features.parquet")
MKT = pd.read_parquet("data/silver/market_returns.parquet")


def ic_stats(sub: pd.DataFrame, feat: str, tgt: str) -> tuple[int, float, float, float]:
    s = sub[[feat, tgt]].dropna()
    n = len(s)
    if n < 15 or s[feat].nunique() < 3:
        return n, np.nan, np.nan, np.nan
    r, p = spearmanr(s[feat], s[tgt])
    t = r * np.sqrt((n - 2) / max(1e-12, 1.0 - r**2))
    return n, round(float(r), 4), round(float(t), 2), round(float(p), 4)


def main() -> None:
    # Rebuild per-pass normalized FRP from Silver so adding NOAA-21 doesn't inflate totals
    fac = SILVER.copy()
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

    df = GOLD.merge(pp, on=["ticker", "trading_date"], how="inner").merge(
        MKT, on=["ticker", "trading_date"], how="inner"
    )

    def _add_norm_feats(g: pd.DataFrame) -> pd.DataFrame:
        g = g.sort_values("trading_date").copy()
        clean = g["w_frp_pp"].where(g["cloud_cover_pct_weighted"] <= 50.0)
        log_c = np.log1p(clean)
        g["yoy_per_pass"] = clean.rolling(5, min_periods=2).mean() / clean.rolling(
            21, min_periods=7
        ).mean().shift(252).replace(0.0, np.nan)
        g["log_pp_z90"] = (np.log1p(g["w_frp_pp"]) - log_c.rolling(90, min_periods=30).mean()) / (
            log_c.rolling(90, min_periods=30).std().replace(0.0, np.nan)
        )
        return g

    df = df.groupby("ticker", group_keys=False).apply(_add_norm_feats)
    clear = df[df["cloud_cover_pct_weighted"] <= 40.0].copy()
    clear["year"] = pd.to_datetime(clear["trading_date"]).dt.year

    # 1. Annual comparison: Raw YoY vs Per-Pass YoY (10d excess, Clear Sky)
    print("=== ANNUAL IC (10d excess, CLEAR SKY): RAW vs PER-PASS YoY ===")
    rows = []
    for yr, grp in clear.groupby("year"):
        n1, r1, t1, p1 = ic_stats(grp, "frp_yoy_ratio", "fwd_ret_10d_excess")
        n2, r2, t2, p2 = ic_stats(grp, "yoy_per_pass", "fwd_ret_10d_excess")
        rows.append({"year": yr, "n": n2, "raw_ic": r1, "pp_ic": r2, "pp_t": t2, "pp_p": p2})
    print(pd.DataFrame(rows).to_string(index=False))

    # 2. Honest Non-Overlapping 10-Day Test (every 10th trading date)
    all_dates = sorted(df["trading_date"].unique())
    non_overlap_dates = set(all_dates[::10])
    no_clear = clear[clear["trading_date"].isin(non_overlap_dates)]

    print("\n=== HONEST NON-OVERLAPPING 10-DAY IC (CLEAR SKY) ===")
    for feat in ["frp_yoy_ratio", "yoy_per_pass", "log_pp_z90"]:
        n, r, t, p = ic_stats(no_clear, feat, "fwd_ret_10d_excess")
        print(f"{feat:16s} | n={n:4d} | IC={r:+.4f} | t={t:+.2f} | p={p:.4f}")


if __name__ == "__main__":
    main()
