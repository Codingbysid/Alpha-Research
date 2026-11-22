"""
Automated NOAA MarineCadastre AIS Streamer via Microsoft Azure GeoParquet.
Queries remote daily Parquet files directly over HTTPS with DuckDB HTTPFS + Spatial.
Filters Gulf Coast refining corridor tankers without downloading full multi-gigabyte national files.

Zero manual web forms. Zero disk bloat (< 1 MB local storage used).

Usage:
    PYTHONPATH=. python3 src/ingest/stream_azure_ais.py --start 2024-05-01 --end 2024-07-31
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pandas as pd

OUT_PATH = Path("data/ais/gulf_tanker_daily.parquet")
GULF_BBOX = (-95.8, 28.8, -93.6, 30.2)  # (west, south, east, north)
BASE_URL = "https://ocmgeodatastor1.blob.core.windows.net/marinecadastre/ais2024"


def stream_gulf_ais_range(start_str: str, end_str: str) -> pd.DataFrame:
    west, south, east, north = GULF_BBOX
    start_dt = datetime.strptime(start_str, "%Y-%m-%d").date()
    end_dt = datetime.strptime(end_str, "%Y-%m-%d").date()

    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; INSTALL spatial; LOAD spatial;")
    con.execute("SET enable_http_metadata_cache=true;")
    con.execute("SET http_timeout=30;")

    cur = start_dt
    daily_rows: list[dict] = []

    print(f"[azure-ais] Streaming Gulf Coast AIS pings from {start_str} to {end_str}...")
    while cur <= end_dt:
        date_str = cur.strftime("%Y-%m-%d")
        file_url = f"{BASE_URL}/ais-{date_str}.parquet"

        q = f"""
            SELECT
                CAST(base_date_time AS DATE) AS obs_date,
                COUNT(DISTINCT mmsi) AS tanker_count,
                AVG(CASE WHEN draft > 1.0 AND draft < 35.0 THEN draft END) AS mean_draught_m,
                AVG(CASE WHEN sog IS NOT NULL AND sog < 1.5 THEN 1.0 ELSE 0.0 END) AS slow_dwell_share
            FROM read_parquet('{file_url}')
            WHERE vessel_type BETWEEN 80 AND 89
              AND ST_X(geometry) BETWEEN {west} AND {east}
              AND ST_Y(geometry) BETWEEN {south} AND {north}
            GROUP BY 1
        """
        try:
            res = con.execute(q).df()
            if not res.empty:
                row = res.iloc[0].to_dict()
                daily_rows.append(row)
                print(
                    f"  {date_str} -> Tankers: {int(row['tanker_count']):3d} | "
                    f"Draught: {float(row['mean_draught_m']):.2f}m | "
                    f"Slow Dwell: {float(row['slow_dwell_share']):.1%}"
                )
        except Exception as exc:
            print(f"  {date_str} -> Skipped/Error: {exc}")

        cur += timedelta(days=1)

    con.close()

    if not daily_rows:
        print("[azure-ais] No rows extracted.")
        return pd.DataFrame()

    new_df = pd.DataFrame(daily_rows)
    new_df["trading_date"] = pd.to_datetime(new_df["obs_date"]).dt.normalize()

    # Merge with existing data if present
    if OUT_PATH.exists():
        existing = pd.read_parquet(OUT_PATH)
        combined = pd.concat([existing, new_df], ignore_index=True)
        combined = combined.drop_duplicates(subset=["trading_date"]).sort_values("trading_date").reset_index(drop=True)
    else:
        combined = new_df.sort_values("trading_date").reset_index(drop=True)

    # Recompute rolling 60-day metrics
    mu60 = combined["tanker_count"].rolling(60, min_periods=10).mean()
    sd60 = combined["tanker_count"].rolling(60, min_periods=10).std().replace(0.0, float("nan"))
    combined["tanker_z60"] = (combined["tanker_count"] - mu60) / sd60
    combined["draught_chg_10d"] = combined["mean_draught_m"].diff(10)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    combined.to_parquet(OUT_PATH, index=False)
    print(f"\n[azure-ais] Successfully updated {len(combined):,} total daily AIS observations -> {OUT_PATH}")
    return combined


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stream NOAA Azure AIS Data")
    parser.add_argument("--start", type=str, default="2024-05-01", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", type=str, default="2024-06-30", help="End date YYYY-MM-DD")
    args = parser.parse_args()

    stream_gulf_ais_range(args.start, args.end)
