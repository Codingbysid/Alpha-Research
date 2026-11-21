"""
Shared Silver -> Gold feature engineering engine for NASA FIRMS refinery OSINT.
Guarantees zero train/serve skew and strict point-in-time NYSE alignment.

Usage:
    python src/transform/feature_engine.py
"""

from __future__ import annotations

import time
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import geopandas as gpd
import numpy as np
import pandas as pd
import requests

BRONZE_PATH = Path("data/bronze/firms_detections.parquet")
FACILITIES_PATH = Path("config/facilities.geojson")
CLOUD_CACHE_PATH = Path("data/silver/cloud_cover.parquet")
SILVER_FACILITY_PATH = Path("data/silver/facility_daily.parquet")
GOLD_FEATURES_PATH = Path("data/gold/refinery_features.parquet")

NY_TZ = ZoneInfo("America/New_York")
EQUAL_AREA_CRS = "EPSG:5070"  # NAD83 / Conus Albers (meters)

# Cutoff before 16:00 ET close so live orders can execute without look-ahead bias
MARKET_CUTOFF_HOUR = 15
MARKET_CUTOFF_MINUTE = 45
INGESTION_LATENCY_MINUTES = 90
CLEAR_SKY_CLOUD_THRESHOLD = 30.0  # <= 30% cloud cover counts as unobstructed view


def assign_trading_date_et(
    acq_utc: pd.Series,
    latency_minutes: int = INGESTION_LATENCY_MINUTES,
    cutoff_hour: int = MARKET_CUTOFF_HOUR,
    cutoff_minute: int = MARKET_CUTOFF_MINUTE,
) -> pd.Series:
    """
    Maps UTC satellite acquisition timestamps to the first tradeable US business day
    where the signal is genuinely available prior to the 15:45 ET execution cutoff.
    """
    available_et = (acq_utc + pd.to_timedelta(latency_minutes, unit="m")).dt.tz_convert(NY_TZ)
    cutoff_mins = cutoff_hour * 60 + cutoff_minute
    mins_since_midnight = available_et.dt.hour * 60 + available_et.dt.minute

    is_weekend = available_et.dt.weekday >= 5
    after_cutoff = mins_since_midnight >= cutoff_mins

    base_date = available_et.dt.tz_localize(None).dt.normalize()
    needs_roll = is_weekend | after_cutoff

    # Roll weekend or post-cutoff passes forward to the next business day
    # (Swap BDay for exchange_calendars XNYS before live capital deployment)
    trading_dates = base_date.where(~needs_roll, base_date + pd.offsets.BDay(1))
    return trading_dates


def spatial_join_facilities(
    bronze_df: pd.DataFrame, facilities_gdf: gpd.GeoDataFrame
) -> pd.DataFrame:
    """
    Filters to nominal/high confidence detections and assigns each pixel to at most
    one refinery (nearest centroid within that facility's buffer_km).
    """
    valid = bronze_df[bronze_df["confidence"].isin(["n", "h"])].copy()
    if valid.empty:
        return pd.DataFrame()

    valid = valid.reset_index(drop=True)
    valid["det_id"] = valid.index

    pts_gdf = gpd.GeoDataFrame(
        valid,
        geometry=gpd.points_from_xy(valid["longitude"], valid["latitude"]),
        crs="EPSG:4326",
    ).to_crs(EQUAL_AREA_CRS)

    fac_proj = facilities_gdf.to_crs(EQUAL_AREA_CRS).copy()
    fac_proj["centroid_geom"] = fac_proj.geometry
    fac_proj["geometry"] = fac_proj.geometry.buffer(fac_proj["buffer_km"] * 1000.0)

    joined = gpd.sjoin(
        pts_gdf,
        fac_proj[["facility_id", "ticker", "capacity_bpd", "centroid_geom", "geometry"]],
        how="inner",
        predicate="within",
    )
    if joined.empty:
        return pd.DataFrame()

    # Resolve any overlapping refinery buffers by keeping strictly the nearest centroid
    joined["dist_m"] = joined.geometry.distance(joined["centroid_geom"])
    joined = (
        joined.sort_values("dist_m")
        .drop_duplicates(subset=["det_id"], keep="first")
        .drop(columns=["geometry", "centroid_geom", "index_right", "det_id"])
    )
    return pd.DataFrame(joined)


def _fetch_open_meteo_cloud(
    lat: float, lon: float, start_date: date, end_date: date
) -> pd.DataFrame:
    """
    Fetches hourly UTC cloud_cover (%) from Open-Meteo, automatically stitching the
    Historical Archive API (older than 5 days) and Forecast API (recent 5 days).
    """
    today = date.today()
    archive_cutoff = today - timedelta(days=5)
    frames: list[pd.DataFrame] = []

    if start_date <= archive_cutoff:
        arch_end = min(end_date, archive_cutoff)
        resp = requests.get(
            "https://archive-api.open-meteo.com/v1/archive",
            params={
                "latitude": round(lat, 4),
                "longitude": round(lon, 4),
                "start_date": start_date.isoformat(),
                "end_date": arch_end.isoformat(),
                "hourly": "cloud_cover",
                "timezone": "UTC",
            },
            timeout=90,
        )
        resp.raise_for_status()
        payload = resp.json().get("hourly", {})
        if payload.get("time"):
            frames.append(
                pd.DataFrame(
                    {
                        "time_utc": pd.to_datetime(payload["time"], utc=True),
                        "cloud_cover_pct": payload["cloud_cover"],
                    }
                )
            )
        time.sleep(0.2)

    if end_date > archive_cutoff:
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": round(lat, 4),
                "longitude": round(lon, 4),
                "hourly": "cloud_cover",
                "past_days": 6,
                "forecast_days": 1,
                "timezone": "UTC",
            },
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json().get("hourly", {})
        if payload.get("time"):
            frames.append(
                pd.DataFrame(
                    {
                        "time_utc": pd.to_datetime(payload["time"], utc=True),
                        "cloud_cover_pct": payload["cloud_cover"],
                    }
                )
            )
        time.sleep(0.2)

    if not frames:
        return pd.DataFrame(columns=["trading_date", "cloud_cover_pct"])

    hourly = (
        pd.concat(frames, ignore_index=True)
        .drop_duplicates(subset=["time_utc"], keep="last")
        .dropna(subset=["cloud_cover_pct"])
    )

    # US nocturnal VIIRS overpasses (~01:30 local) fall inside 05:00-11:00 UTC
    night_window = hourly[hourly["time_utc"].dt.hour.between(5, 11)].copy()
    night_window["trading_date"] = assign_trading_date_et(night_window["time_utc"])

    daily_cloud = (
        night_window.groupby("trading_date", as_index=False)["cloud_cover_pct"]
        .mean()
        .round(2)
    )
    return daily_cloud


def get_cloud_cover_panel(
    facilities_gdf: gpd.GeoDataFrame,
    start_date: date,
    end_date: date,
    cache_path: Path = CLOUD_CACHE_PATH,
) -> pd.DataFrame:
    """Loads cached daily cloud cover and fetches only missing date spans."""
    cached = pd.read_parquet(cache_path) if cache_path.exists() else pd.DataFrame()
    existing_max = (
        pd.to_datetime(cached["trading_date"]).max().date() if not cached.empty else None
    )
    existing_min = (
        pd.to_datetime(cached["trading_date"]).min().date() if not cached.empty else None
    )

    # Refetch if cache doesn't cover requested window
    needs_fetch = (
        cached.empty
        or existing_min is None
        or existing_max is None
        or start_date < existing_min
        or end_date > existing_max
    )

    if not needs_fetch:
        return cached

    fetch_start = start_date if cached.empty else min(start_date, existing_max - timedelta(days=2))
    print(f"[weather] Fetching Open-Meteo cloud cover from {fetch_start} to {end_date}...")

    rows: list[pd.DataFrame] = []
    for _, fac in facilities_gdf.iterrows():
        lon, lat = fac.geometry.x, fac.geometry.y
        df_fac = _fetch_open_meteo_cloud(lat, lon, fetch_start, end_date)
        df_fac["facility_id"] = fac["facility_id"]
        rows.append(df_fac)

    fresh = pd.concat(rows, ignore_index=True)
    combined = (
        pd.concat([cached, fresh], ignore_index=True)
        .drop_duplicates(subset=["facility_id", "trading_date"], keep="last")
        .sort_values(["facility_id", "trading_date"])
    )

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    combined.to_parquet(cache_path, index=False)
    return combined


def _consecutive_true_streak(flag: pd.Series) -> pd.Series:
    """Vectorized streak counter for consecutive True observations."""
    groups = (~flag).cumsum()
    return flag.groupby(groups).cumsum().astype(int)


def build_features(
    bronze_path: Path = BRONZE_PATH,
    facilities_path: Path = FACILITIES_PATH,
    gold_path: Path = GOLD_FEATURES_PATH,
) -> pd.DataFrame:
    if not bronze_path.exists():
        raise FileNotFoundError(
            f"{bronze_path} not found. Run firms_archive.py or firms_live.py first."
        )

    bronze_df = pd.read_parquet(bronze_path)
    facilities_gdf = gpd.read_file(facilities_path)

    matched = spatial_join_facilities(bronze_df, facilities_gdf)
    if matched.empty:
        raise ValueError("Zero nominal/high confidence detections matched facility polygons.")

    # Parse acquisition UTC timestamp and map strictly to point-in-time US trading_date
    matched["acq_datetime_utc"] = pd.to_datetime(
        matched["acq_date"] + " " + matched["acq_time"],
        format="%Y-%m-%d %H%M",
        utc=True,
    )
    matched["trading_date"] = assign_trading_date_et(matched["acq_datetime_utc"])
    matched["frp_night"] = np.where(matched["daynight"] == "N", matched["frp"], 0.0)

    # Aggregate raw detections to (facility_id, trading_date)
    fac_agg = (
        matched.groupby(["facility_id", "trading_date"], as_index=False)
        .agg(
            frp_total_mw=("frp", "sum"),
            frp_night_mw=("frp_night", "sum"),
            hotspot_count=("frp", "size"),
            max_bright_ti4=("bright_ti4", "max"),
            satellite_pass_count=("acq_datetime_utc", "nunique"),
            has_nrt=("processing_mode", lambda s: (s == "NRT").any()),
        )
    )

    # Build a complete business-day spine so zero-heat days are explicitly preserved
    start_dt = fac_agg["trading_date"].min().date()
    end_dt = fac_agg["trading_date"].max().date()
    bdays = pd.bdate_range(start=start_dt, end=end_dt)

    spine = pd.MultiIndex.from_product(
        [facilities_gdf["facility_id"].tolist(), bdays],
        names=["facility_id", "trading_date"],
    ).to_frame(index=False)

    fac_meta = facilities_gdf[["facility_id", "ticker", "capacity_bpd"]].copy()
    cloud_df = get_cloud_cover_panel(facilities_gdf, start_dt, end_dt)

    silver = (
        spine.merge(fac_meta, on="facility_id", how="left")
        .merge(fac_agg, on=["facility_id", "trading_date"], how="left")
        .merge(cloud_df, on=["facility_id", "trading_date"], how="left")
    )

    for col in ("frp_total_mw", "frp_night_mw", "hotspot_count", "satellite_pass_count"):
        silver[col] = silver[col].fillna(0.0)
    silver["has_nrt"] = silver["has_nrt"].fillna(False)
    silver["is_clear_sky"] = silver["cloud_cover_pct"] <= CLEAR_SKY_CLOUD_THRESHOLD
    silver["is_clear_sky_zero"] = (silver["frp_night_mw"] == 0.0) & silver["is_clear_sky"]

    SILVER_FACILITY_PATH.parent.mkdir(parents=True, exist_ok=True)
    silver.to_parquet(SILVER_FACILITY_PATH, index=False)

    # Capacity-weighted aggregation to (ticker, trading_date)
    ticker_cap = fac_meta.groupby("ticker")["capacity_bpd"].transform("sum")
    fac_meta["cap_weight"] = fac_meta["capacity_bpd"] / ticker_cap
    silver = silver.merge(fac_meta[["facility_id", "cap_weight"]], on="facility_id", how="left")

    silver["w_frp_night"] = silver["frp_night_mw"] * silver["cap_weight"]
    silver["w_cloud"] = silver["cloud_cover_pct"] * silver["cap_weight"]
    silver["w_clear_zero"] = silver["is_clear_sky_zero"].astype(float) * silver["cap_weight"]

    gold = (
        silver.groupby(["ticker", "trading_date"], as_index=False)
        .agg(
            frp_total_mw=("frp_total_mw", "sum"),
            frp_night_mw=("frp_night_mw", "sum"),
            frp_cap_weighted=("w_frp_night", "sum"),
            hotspot_count=("hotspot_count", "sum"),
            max_bright_ti4=("max_bright_ti4", "max"),
            satellite_pass_count=("satellite_pass_count", "sum"),
            cloud_cover_pct_weighted=("w_cloud", "sum"),
            clear_sky_zero_cap_share=("w_clear_zero", "sum"),
            has_nrt=("has_nrt", "any"),
        )
        .sort_values(["ticker", "trading_date"])
        .reset_index(drop=True)
    )

    gold["processing_mode"] = np.where(gold["has_nrt"], "NRT", "SP")
    gold = gold.drop(columns=["has_nrt"])

    # Compute strictly backward-looking rolling dynamics per ticker.
    # Mask heavily overcast days (>50% weighted cloud cover) out of baseline stats
    # so storm-induced zeroes do not artificially depress rolling mean/std.
    def _add_rolling_features(grp: pd.DataFrame) -> pd.DataFrame:
        g = grp.copy()
        clean_signal = g["frp_cap_weighted"].where(g["cloud_cover_pct_weighted"] <= 50.0)

        for win in (30, 90):
            roll_mean = clean_signal.rolling(win, min_periods=max(10, win // 3)).mean()
            roll_std = clean_signal.rolling(win, min_periods=max(10, win // 3)).std()
            g[f"frp_zscore_{win}d"] = (g["frp_cap_weighted"] - roll_mean) / roll_std.replace(
                0.0, np.nan
            )

        # Year-over-year ratio (252 trading days) removes seasonal fuel-blend turnarounds
        yoy_base = clean_signal.rolling(21, min_periods=7).mean().shift(252)
        cur_smooth = clean_signal.rolling(5, min_periods=2).mean()
        g["frp_yoy_ratio"] = cur_smooth / yoy_base.replace(0.0, np.nan)

        # Outage persistence counter (only increments on reasonably clear skies)
        severe_drop = (g["frp_zscore_90d"] <= -2.0) & (g["cloud_cover_pct_weighted"] <= 40.0)
        g["days_below_minus2_z"] = _consecutive_true_streak(severe_drop.fillna(False))
        return g

    gold = gold.groupby("ticker", group_keys=False).apply(_add_rolling_features)

    gold_path.parent.mkdir(parents=True, exist_ok=True)
    gold.to_parquet(gold_path, index=False)
    print(f"[gold] Wrote {len(gold):,} rows across {gold['ticker'].nunique()} tickers to {gold_path}")
    return gold


if __name__ == "__main__":
    df_gold = build_features()
    print(df_gold.tail(9).to_string(index=False))
