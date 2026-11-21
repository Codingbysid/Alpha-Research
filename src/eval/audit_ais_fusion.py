"""
Ingests clipped NOAA MarineCadastre Gulf Coast AIS files (CSV or GeoParquet)
and tests whether tanker arrivals/draught confirm or refute clear-sky thermal spikes.

Usage:
    PYTHONPATH=. python3 -W ignore src/eval/audit_ais_fusion.py
"""

from __future__ import annotations

from pathlib import Path
import zipfile

import duckdb
import numpy as np
import pandas as pd
from scipy.stats import spearmanr

from src.eval.audit_short_q5 import CLOUD_MAX, HOLDING_DAYS, build_panel

RAW_AIS_DIR = Path("data/raw_ais")
DATA_DIR = Path("data")
OUT_DIR = Path("data/ais")
AIS_DAILY_PATH = OUT_DIR / "gulf_tanker_daily.parquet"
AIS_ZONES_PATH = OUT_DIR / "gulf_tanker_zones.parquet"

# Broad Gulf Coast refinery corridor (Sabine/Port Arthur through Galveston/Houston/Freeport)
GULF_BBOX = (-95.8, 28.8, -93.6, 30.2)
GULF_TICKERS = {"VLO", "MPC", "PSX", "PBF", "DK"}


def build_gulf_tanker_daily(raw_dir: Path = RAW_AIS_DIR) -> tuple[pd.DataFrame, pd.DataFrame]:
    raw_dir.mkdir(parents=True, exist_ok=True)
    
    # Auto-unzip any zips in raw_dir or DATA_DIR
    for zpath in list(raw_dir.glob("*.zip")) + list(DATA_DIR.glob("*AIS*.zip")):
        print(f"[ais-hist] Unzipping {zpath.name}...")
        with zipfile.ZipFile(zpath, "r") as z:
            z.extractall(raw_dir)

    # Check for CSVs in raw_dir or data/
    files = sorted(list(raw_dir.glob("*.csv")) + list(raw_dir.glob("*.parquet")))
    if not files:
        # Check if user placed CSV directly in data/
        for p in DATA_DIR.glob("*.csv"):
            if "paper_ledger" not in p.name and "fire_" not in p.name:
                files.append(p)
                
    if not files:
        raise FileNotFoundError(
            f"No CSV, Parquet, or ZIP files found in {raw_dir} or {DATA_DIR}."
        )

    west, south, east, north = GULF_BBOX
    con = duckdb.connect()
    daily_frames: list[pd.DataFrame] = []
    zone_frames: list[pd.DataFrame] = []

    for fpath in files:
        src_expr = (
            f"read_csv_auto('{fpath.as_posix()}', all_varchar=True)"
            if fpath.suffix.lower() == ".csv"
            else f"read_parquet('{fpath.as_posix()}')"
        )
        
        # Dynamically inspect column names
        cols = [c.lower() for c in con.execute(f"SELECT * FROM {src_expr} LIMIT 0").df().columns]
        mmsi_c = next((c for c in cols if "mmsi" in c), "mmsi")
        lat_c = next((c for c in cols if "lat" in c), "latitude")
        lon_c = next((c for c in cols if "lon" in c), "longitude")
        time_c = next((c for c in cols if any(k in c for k in ["time", "date", "timestamp"])), "base_date_time")
        type_c = next((c for c in cols if any(k in c for k in ["vessel_type", "vesseltype", "cargo", "type"])), "vessel_type")
        draft_c = next((c for c in cols if any(k in c for k in ["draft", "draught"])), "draft")
        sog_c = next((c for c in cols if "sog" in c or "speed" in c), "sog")

        query_daily = f"""
            WITH raw AS (
                SELECT * FROM {src_expr}
            ),
            norm AS (
                SELECT
                    CAST({mmsi_c} AS VARCHAR) AS v_mmsi,
                    TRY_CAST({lat_c} AS DOUBLE) AS v_lat,
                    TRY_CAST({lon_c} AS DOUBLE) AS v_lon,
                    TRY_CAST({sog_c} AS DOUBLE) AS v_sog,
                    TRY_CAST({draft_c} AS DOUBLE) AS v_draft,
                    TRY_CAST({type_c} AS INTEGER) AS v_type,
                    TRY_CAST({time_c} AS TIMESTAMP) AS v_ts
                FROM raw
            ),
            filtered AS (
                SELECT *
                FROM norm
                WHERE v_type BETWEEN 80 AND 89
                  AND v_lon BETWEEN {west} AND {east}
                  AND v_lat BETWEEN {south} AND {north}
                  AND v_ts IS NOT NULL
            )
            SELECT
                CAST(v_ts AS DATE) AS obs_date,
                COUNT(DISTINCT v_mmsi) AS tanker_count,
                AVG(CASE WHEN v_draft > 1.0 AND v_draft < 35.0 THEN v_draft END) AS mean_draught_m,
                AVG(CASE WHEN v_sog IS NOT NULL AND v_sog < 1.5 THEN 1.0 ELSE 0.0 END) AS slow_dwell_share
            FROM filtered
            GROUP BY 1
            ORDER BY 1
        """
        p_daily = con.execute(query_daily).df()
        if not p_daily.empty:
            daily_frames.append(p_daily)

        query_zones = f"""
            WITH raw AS (
                SELECT * FROM {src_expr}
            ),
            norm AS (
                SELECT
                    CAST({mmsi_c} AS VARCHAR) AS v_mmsi,
                    TRY_CAST({lat_c} AS DOUBLE) AS v_lat,
                    TRY_CAST({lon_c} AS DOUBLE) AS v_lon,
                    TRY_CAST({sog_c} AS DOUBLE) AS v_sog,
                    TRY_CAST({draft_c} AS DOUBLE) AS v_draft,
                    TRY_CAST({type_c} AS INTEGER) AS v_type,
                    TRY_CAST({time_c} AS TIMESTAMP) AS v_ts
                FROM raw
            ),
            zoned AS (
                SELECT *,
                    CASE 
                        WHEN v_lon BETWEEN -94.2 AND -93.7 AND v_lat BETWEEN 29.6 AND 30.1 THEN 'PORT_ARTHUR'
                        WHEN v_lon BETWEEN -95.4 AND -94.6 AND v_lat BETWEEN 29.2 AND 29.8 THEN 'HOUSTON_GALVESTON'
                        WHEN v_lon BETWEEN -95.6 AND -95.1 AND v_lat BETWEEN 28.8 AND 29.2 THEN 'FREEPORT'
                        ELSE 'OFFSHORE_APPROACH'
                    END AS port_zone
                FROM norm
                WHERE v_type BETWEEN 80 AND 89 AND v_ts IS NOT NULL
            )
            SELECT
                CAST(v_ts AS DATE) AS obs_date,
                port_zone,
                COUNT(DISTINCT v_mmsi) AS tanker_count,
                AVG(CASE WHEN v_draft > 1.0 AND v_draft < 35.0 THEN v_draft END) AS avg_draft,
                AVG(CASE WHEN v_sog IS NOT NULL AND v_sog < 1.5 THEN 1.0 ELSE 0.0 END) AS slow_dwell_pct
            FROM zoned
            GROUP BY 1, 2
            ORDER BY 1, 2
        """
        p_zone = con.execute(query_zones).df()
        if not p_zone.empty:
            zone_frames.append(p_zone)

    con.close()
    if not daily_frames:
        raise ValueError("No tanker rows (VesselType 80-89) matched the Gulf Coast bounding box.")

    daily = (
        pd.concat(daily_frames, ignore_index=True)
        .groupby("obs_date", as_index=False)
        .agg(
            tanker_count=("tanker_count", "sum"),
            mean_draught_m=("mean_draught_m", "mean"),
            slow_dwell_share=("slow_dwell_share", "mean"),
        )
        .sort_values("obs_date")
        .reset_index(drop=True)
    )
    daily["trading_date"] = pd.to_datetime(daily["obs_date"]).dt.normalize()
    
    # Drop boundary days with truncated observation hours (<50 tanker pings)
    daily = daily[daily["tanker_count"] >= 50].reset_index(drop=True)

    if len(daily) < 60:
        mu = daily["tanker_count"].expanding(min_periods=3).mean()
        sd = daily["tanker_count"].expanding(min_periods=3).std().replace(0.0, np.nan)
        daily["tanker_z60"] = ((daily["tanker_count"] - mu) / sd).fillna(0.0)
        daily["draught_chg_10d"] = daily["mean_draught_m"].diff(5).bfill()
    else:
        mu60 = daily["tanker_count"].rolling(60, min_periods=15).mean()
        sd60 = daily["tanker_count"].rolling(60, min_periods=15).std().replace(0.0, np.nan)
        daily["tanker_z60"] = (daily["tanker_count"] - mu60) / sd60
        daily["draught_chg_10d"] = daily["mean_draught_m"].diff(10)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    daily.to_parquet(AIS_DAILY_PATH, index=False)
    
    zones = pd.concat(zone_frames, ignore_index=True)
    zones["trading_date"] = pd.to_datetime(zones["obs_date"]).dt.normalize()
    zones = zones[zones["trading_date"].isin(daily["trading_date"])].reset_index(drop=True)
    zones.to_parquet(AIS_ZONES_PATH, index=False)

    print(
        f"[ais-hist] Processed {len(daily):,} Gulf tanker days "
        f"({daily['trading_date'].min().date()} -> {daily['trading_date'].max().date()}) | Saved to {OUT_DIR}"
    )
    return daily, zones


def audit_fusion(tanker_df: pd.DataFrame, zones_df: pd.DataFrame) -> None:
    panel = build_panel()
    dt_min = tanker_df["trading_date"].min()
    dt_max = tanker_df["trading_date"].max()

    gulf = panel[
        panel["ticker"].isin(GULF_TICKERS)
        & panel["fwd_ret_10d_excess"].notna()
        & (panel["trading_date"] >= dt_min)
        & (panel["trading_date"] <= dt_max)
    ].copy()

    if gulf.empty:
        print(f"[audit-ais] No Gulf refiner records found between {dt_min.date()} and {dt_max.date()}.")
        return

    t_cols = tanker_df[
        ["trading_date", "tanker_count", "tanker_z60", "mean_draught_m", "draught_chg_10d", "slow_dwell_share"]
    ].sort_values("trading_date")

    m = pd.merge(gulf, t_cols, on="trading_date", how="inner")

    print("\n" + "=" * 80)
    print(f"       GULF COAST TANKER AIS SENSOR FUSION AUDIT ({dt_min.date()} -> {dt_max.date()})")
    print("=" * 80)
    print(f"Total Gulf Refiner-Days Merged: {len(m):,} across {m['trading_date'].nunique()} trading days")
    print(f"Active Refiners Evaluated:      {sorted(m['ticker'].unique())}")
    print(f"Market Regimes in Period:       {dict(m['regime_label'].value_counts())}")

    # 1. Overall Correlations
    print("\n" + "-" * 80)
    print("1. OVERALL CORRELATION: MARITIME SENSORS vs REFINER FORWARD EXCESS RETURNS")
    print("-" * 80)
    print(f"{'Metric':20s} | {'Target':18s} | {'N':4s} | {'Spearman IC':11s} | {'t-stat':7s} | {'p-value'}")
    print("-" * 75)
    for col, lbl in [
        ("tanker_count", "Tanker Volume"),
        ("tanker_z60", "Tanker Z-Score"),
        ("mean_draught_m", "Mean Draught (m)"),
        ("slow_dwell_share", "Slow Dwell Share (<1.5kt)"),
        ("yoy_per_pass", "Thermal YoY Flaring"),
    ]:
        for tgt in ("fwd_ret_10d_excess", "fwd_ret_5d_excess"):
            sub = m[[col, tgt]].dropna()
            if len(sub) >= 5:
                ic, p = spearmanr(sub[col], sub[tgt])
                t_stat = ic * np.sqrt((len(sub) - 2) / max(1e-12, 1.0 - ic**2))
                print(f"{lbl:20s} | {tgt:18s} | {len(sub):4d} | {ic:+11.4f} | {t_stat:+7.2f} | {p:.4f}")

    # 2. Clear Sky Subset
    cs = m[m["cloud_cover_pct_weighted"] <= CLOUD_MAX]
    print("\n" + "-" * 80)
    print(f"2. CLEAR-SKY CORRELATION (Cloud Cover <= {CLOUD_MAX}%, N={len(cs)})")
    print("-" * 80)
    for col, lbl in [
        ("tanker_z60", "Tanker Z-Score"),
        ("slow_dwell_share", "Slow Dwell Share"),
        ("mean_draught_m", "Mean Draught (m)"),
        ("yoy_per_pass", "Thermal YoY Flaring"),
    ]:
        sub = cs[[col, "fwd_ret_10d_excess"]].dropna()
        if len(sub) >= 5:
            ic, p = spearmanr(sub[col], sub["fwd_ret_10d_excess"])
            t_stat = ic * np.sqrt((len(sub) - 2) / max(1e-12, 1.0 - ic**2))
            print(f"{lbl:20s} vs 10d Excess Ret | N={len(sub):3d} | IC={ic:+11.4f} | t={t_stat:+7.2f} | p={p:.4f}")

    # 3. 2x2 Factor Matrix
    print("\n" + "-" * 80)
    print("3. SENSOR FUSION MATRIX: THERMAL SPIKES x MARITIME DOCK BOTTLENECKS")
    print("-" * 80)
    med_yoy = m["yoy_per_pass"].median()
    med_dwell = m["slow_dwell_share"].median()
    
    for t_cond, t_name in [(m["yoy_per_pass"] >= med_yoy, "HIGH THERMAL"), (m["yoy_per_pass"] < med_yoy, "LOW THERMAL")]:
        for d_cond, d_name in [(m["slow_dwell_share"] >= med_dwell, "HIGH DWELL (Bottleneck)"), (m["slow_dwell_share"] < med_dwell, "LOW DWELL (Fluid Flow)")]:
            sub = m[t_cond & d_cond]
            r10 = sub["fwd_ret_10d_excess"]
            r5 = sub["fwd_ret_5d_excess"]
            print(
                f"  {t_name:12s} + {d_name:24s} | N={len(sub):2d} | "
                f"10d Excess={r10.mean()*100:+6.2f}% | 5d Excess={r5.mean()*100:+6.2f}% | "
                f"Short WR={(r10 < 0).mean()*100:4.1f}%"
            )

    # 4. Refiner Specific Port Zones
    print("\n" + "-" * 80)
    print("4. REFINER-SPECIFIC LOCAL DOCK CORRELATIONS")
    print("-" * 80)
    zone_mapping = {
        "VLO": ("PORT_ARTHUR", "Valero Port Arthur / Lucas Terminal"),
        "MPC": ("HOUSTON_GALVESTON", "Marathon Galveston Bay / Texas City"),
        "PSX": ("HOUSTON_GALVESTON", "Phillips 66 Sweeny / Houston"),
    }
    for ticker, (zone, desc) in zone_mapping.items():
        t_sub = gulf[gulf["ticker"] == ticker]
        z_sub = zones_df[zones_df["port_zone"] == zone]
        merged_tz = pd.merge(t_sub, z_sub, on="trading_date")
        
        ic_cnt, _ = spearmanr(merged_tz["tanker_count"], merged_tz["fwd_ret_10d_excess"])
        ic_drf, p_drf = spearmanr(merged_tz["avg_draft"], merged_tz["fwd_ret_10d_excess"])
        ic_dwl, p_dwl = spearmanr(merged_tz["slow_dwell_pct"], merged_tz["fwd_ret_10d_excess"])
        
        print(f"  {ticker} -> {zone} ({desc}) [N={len(merged_tz)} days]:")
        print(f"     Average Draught vs 10d Ret: IC={ic_drf:+.4f} (p={p_drf:.4f})")
        print(f"     Slow Dwell %    vs 10d Ret: IC={ic_dwl:+.4f} (p={p_dwl:.4f})")
        print(f"     Tanker Count    vs 10d Ret: IC={ic_cnt:+.4f}")

    # 5. Event Level Breakdown
    print("\n" + "-" * 80)
    print("5. EVENT AUDIT: TOP CLEAR-SKY THERMAL FLARE-UPS IN JUNE 2026")
    print("-" * 80)
    q70 = cs["yoy_per_pass"].quantile(0.65)
    spikes = cs[cs["yoy_per_pass"] >= q70].sort_values("yoy_per_pass", ascending=False)
    
    print(
        f"{'Date':10s} | {'Ticker':6s} | {'YoY Pass':8s} | {'Tankers':7s} | {'Draught':7s} | "
        f"{'Slow Dwell':10s} | {'10d Excess':11s} | {'Maritime Verdict'}"
    )
    print("-" * 90)
    for _, r in spikes.head(12).iterrows():
        ret = r["fwd_ret_10d_excess"]
        dwell = r["slow_dwell_share"]
        verdict = (
            "CONFIRMED BOTTLENECK (High Dwell)" if dwell >= med_dwell else "NORMAL TRANSIT (Fluid)"
        )
        print(
            f"{r['trading_date'].strftime('%Y-%m-%d')} | {r['ticker']:6s} | {r['yoy_per_pass']:8.2f} | "
            f"{r['tanker_count']:7.0f} | {r['mean_draught_m']:6.2f}m | {dwell*100:9.1f}% | "
            f"{ret*100:+10.2f}% | {verdict}"
        )
    print("=" * 80 + "\n")


if __name__ == "__main__":
    daily_df, zone_df = build_gulf_tanker_daily()
    audit_fusion(daily_df, zone_df)
