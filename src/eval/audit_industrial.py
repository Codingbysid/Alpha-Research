"""
Isolated OSINT thermal audit for US Steel (XME-hedged) and Petrochemicals (XLB-hedged).
Stores all artifacts in data/industrial/ to keep the live refiner pipeline untouched.

Usage:
    PYTHONPATH=. python3 -W ignore src/eval/audit_industrial.py
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import yfinance as yf
from scipy.stats import spearmanr

from src.ingest.firms_archive import (
    filter_to_facility_bboxes,
    load_facility_bboxes,
    normalize_firms_df,
)
from src.transform.feature_engine import (
    assign_trading_date_et,
    get_cloud_cover_panel,
    spatial_join_facilities,
)

FACILITIES_PATH = Path("config/facilities_industrial.geojson")
ARCHIVE_DIR = Path("data/raw_archives")
OUT_DIR = Path("data/industrial")
BRONZE_IND_PATH = OUT_DIR / "bronze_industrial.parquet"
CLOUD_IND_PATH = OUT_DIR / "cloud_industrial.parquet"

HOLDING_DAYS = 10
CLOUD_MAX = 40.0
BETA_WINDOW = 60


def ingest_industrial_bronze() -> pd.DataFrame:
    if BRONZE_IND_PATH.exists():
        df = pd.read_parquet(BRONZE_IND_PATH)
        print(f"[ind-bronze] Loaded cached {len(df):,} rows from {BRONZE_IND_PATH}")
        return df

    csv_files = sorted(ARCHIVE_DIR.glob("*.csv"))
    bboxes = load_facility_bboxes(FACILITIES_PATH, pad_deg=0.08)
    kept: list[pd.DataFrame] = []

    for csv_path in csv_files:
        print(f"[ind-bronze] Scanning {csv_path.name}...")
        mode = "NRT" if "nrt" in csv_path.stem.lower() else "SP"
        for chunk in pd.read_csv(csv_path, dtype={"acq_time": str}, chunksize=250_000):
            chunk.columns = [c.strip().lower() for c in chunk.columns]
            filt = filter_to_facility_bboxes(chunk, bboxes)
            if not filt.empty:
                kept.append(normalize_firms_df(filt, source_feed=csv_path.stem.upper(), processing_mode=mode))

    if not kept:
        raise ValueError("Zero detections matched industrial facility bounding boxes.")

    df = (
        pd.concat(kept, ignore_index=True)
        .sort_values("processing_mode")  # NRT vs SP -> keep SP last/first deterministically
        .drop_duplicates(subset=["satellite", "acq_date", "acq_time", "latitude", "longitude"], keep="last")
        .reset_index(drop=True)
    )
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(BRONZE_IND_PATH, index=False)
    print(f"[ind-bronze] Saved {len(df):,} deduplicated industrial detections to {BRONZE_IND_PATH}")
    return df


def build_industrial_features(bronze_df: pd.DataFrame, fac_gdf: gpd.GeoDataFrame) -> pd.DataFrame:
    matched = spatial_join_facilities(bronze_df, fac_gdf)
    if matched.empty:
        raise ValueError("Zero nominal/high confidence detections matched industrial polygons.")

    matched["acq_datetime_utc"] = pd.to_datetime(
        matched["acq_date"] + " " + matched["acq_time"], format="%Y-%m-%d %H%M", utc=True
    )
    matched["trading_date"] = assign_trading_date_et(matched["acq_datetime_utc"])
    matched["frp_night"] = np.where(matched["daynight"] == "N", matched["frp"], 0.0)

    fac_agg = matched.groupby(["facility_id", "trading_date"], as_index=False).agg(
        frp_night_mw=("frp_night", "sum"),
        satellite_pass_count=("acq_datetime_utc", "nunique"),
    )
    fac_agg["frp_per_pass"] = np.where(
        fac_agg["satellite_pass_count"] > 0,
        fac_agg["frp_night_mw"] / fac_agg["satellite_pass_count"],
        0.0,
    )

    start_dt = fac_agg["trading_date"].min().date()
    end_dt = fac_agg["trading_date"].max().date()
    bdays = pd.bdate_range(start=start_dt, end=end_dt)

    spine = pd.MultiIndex.from_product(
        [fac_gdf["facility_id"].tolist(), bdays], names=["facility_id", "trading_date"]
    ).to_frame(index=False)

    meta = fac_gdf[["facility_id", "ticker", "sector", "hedge_etf", "capacity_bpd"]].copy()
    meta["cap_w"] = meta["capacity_bpd"] / meta.groupby("ticker")["capacity_bpd"].transform("sum")
    cloud_df = get_cloud_cover_panel(fac_gdf, start_dt, end_dt, cache_path=CLOUD_IND_PATH)

    silver = (
        spine.merge(meta, on="facility_id", how="left")
        .merge(fac_agg, on=["facility_id", "trading_date"], how="left")
        .merge(cloud_df, on=["facility_id", "trading_date"], how="left")
    )
    silver["frp_per_pass"] = silver["frp_per_pass"].fillna(0.0)
    silver["w_frp_pp"] = silver["frp_per_pass"] * silver["cap_w"]
    silver["w_cloud"] = silver["cloud_cover_pct"] * silver["cap_w"]

    gold = (
        silver.groupby(["ticker", "sector", "hedge_etf", "trading_date"], as_index=False)
        .agg(
            w_frp_pp=("w_frp_pp", "sum"),
            cloud_cover_pct_weighted=("w_cloud", "sum"),
            active_days=("frp_per_pass", lambda s: int((s > 0).any())),
        )
        .sort_values(["ticker", "trading_date"])
        .reset_index(drop=True)
    )

    frames = []
    for _, g in gold.groupby("ticker"):
        g = g.sort_values("trading_date").copy()
        clean = g["w_frp_pp"].where(g["cloud_cover_pct_weighted"] <= 50.0)
        g["yoy_per_pass"] = clean.rolling(5, min_periods=2).mean() / clean.rolling(
            21, min_periods=7
        ).mean().shift(252).replace(0.0, np.nan)
        frames.append(g)

    return pd.concat(frames, ignore_index=True).sort_values(["ticker", "trading_date"]).reset_index(drop=True)


def fetch_sector_returns(meta_df: pd.DataFrame) -> pd.DataFrame:
    tickers = sorted(meta_df["ticker"].unique().tolist())
    etfs = sorted(meta_df["hedge_etf"].unique().tolist())
    raw = yf.download(tickers + etfs, start="2018-01-01", auto_adjust=False, progress=False)

    lvl0 = raw.columns.get_level_values(0)
    field = "Adj Close" if "Adj Close" in lvl0 else "Close"
    px = raw[field].copy()
    px.index = pd.to_datetime(px.index).tz_localize(None).normalize()
    px = px.sort_index()
    rets = px.pct_change()

    tkr_to_etf = meta_df.drop_duplicates("ticker").set_index("ticker")["hedge_etf"].to_dict()
    rows = []
    for tkr, etf in tkr_to_etf.items():
        if tkr not in px.columns or etf not in px.columns:
            continue
        s_px, s_ret = px[tkr], rets[tkr]
        e_px, e_ret = px[etf], rets[etf]
        beta = s_ret.rolling(BETA_WINDOW, min_periods=30).cov(e_ret) / e_ret.rolling(
            BETA_WINDOW, min_periods=30
        ).var().replace(0.0, np.nan)
        fwd_s = s_px.shift(-HOLDING_DAYS) / s_px - 1.0
        fwd_e = e_px.shift(-HOLDING_DAYS) / e_px - 1.0
        rows.append(
            pd.DataFrame(
                {
                    "trading_date": px.index,
                    "ticker": tkr,
                    "fwd_ret_10d_excess": (fwd_s - beta * fwd_e).values,
                }
            )
        )
    return pd.concat(rows, ignore_index=True).dropna(subset=["fwd_ret_10d_excess"])


def main() -> None:
    fac_gdf = gpd.read_file(FACILITIES_PATH)
    bronze = ingest_industrial_bronze()
    gold = build_industrial_features(bronze, fac_gdf)
    mkt = fetch_sector_returns(fac_gdf[["ticker", "hedge_etf"]])
    df = gold.merge(mkt, on=["ticker", "trading_date"], how="inner")

    # 1. Sensor visibility check per ticker (critical for enclosed EAF steel mills)
    print("\n=== SENSOR VISIBILITY CHECK (ACTIVE THERMAL DAYS BY TICKER) ===")
    vis = df.groupby(["sector", "ticker"]).agg(
        total_days=("trading_date", "count"),
        pct_active_days=("active_days", lambda s: round(s.mean() * 100, 1)),
        valid_yoy_days=("yoy_per_pass", lambda s: int(s.notna().sum())),
    )
    print(vis.to_string())

    # 2. Separate Sector Audits (Non-overlapping 10d IC + Clear-Sky Quintiles)
    all_dates = sorted(df["trading_date"].unique())
    no_dates = set(all_dates[::HOLDING_DAYS])

    for sector in sorted(df["sector"].unique()):
        sub = df[
            (df["sector"] == sector)
            & (df["cloud_cover_pct_weighted"] <= CLOUD_MAX)
            & (df["yoy_per_pass"].notna())
            & (df["fwd_ret_10d_excess"].notna())
        ].copy()

        no_sub = sub[sub["trading_date"].isin(no_dates)]
        if len(no_sub) >= 20:
            ic, p_val = spearmanr(no_sub["yoy_per_pass"], no_sub["fwd_ret_10d_excess"])
            t_stat = ic * np.sqrt((len(no_sub) - 2) / max(1e-12, 1.0 - ic**2))
            ic_str = f"IC={ic:+.4f} (t={t_stat:+.2f}, p={p_val:.4f}, n={len(no_sub)})"
        else:
            ic_str = f"Insufficient non-overlapping obs (n={len(no_sub)})"

        print(f"\n=== {sector} | NON-OVERLAPPING 10d SPEARMAN IC: {ic_str} ===")
        if len(sub) >= 50:
            sub["yoy_q"] = pd.qcut(
                sub["yoy_per_pass"], 5, labels=["Q1_LOW", "Q2", "Q3", "Q4", "Q5_HIGH"], duplicates="drop"
            )
            qtab = sub.groupby("yoy_q", observed=False)["fwd_ret_10d_excess"].agg(
                n="count",
                mean_fwd_pct=lambda s: round(s.mean() * 100, 3),
                pct_pos=lambda s: round((s > 0).mean() * 100, 1),
            )
            print(qtab.to_string())


if __name__ == "__main__":
    main()
