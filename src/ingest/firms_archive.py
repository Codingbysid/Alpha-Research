"""
Ingests bulk NASA FIRMS Archive (Standard Processing) VIIRS CSVs into Bronze Parquet.

Usage:
    python src/ingest/firms_archive.py --archive-dir data/raw_archives
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd

BRONZE_PATH = Path("data/bronze/firms_detections.parquet")
FACILITIES_PATH = Path("config/facilities.geojson")

CANONICAL_COLUMNS = [
    "latitude",
    "longitude",
    "bright_ti4",
    "bright_ti5",
    "scan",
    "track",
    "acq_date",
    "acq_time",
    "satellite",
    "instrument",
    "confidence",
    "version",
    "frp",
    "daynight",
    "source_feed",
    "processing_mode",
    "ingested_at_utc",
]


def load_facility_bboxes(
    facilities_path: Path = FACILITIES_PATH, pad_deg: float = 0.08
) -> list[tuple[float, float, float, float]]:
    """Returns (west, south, east, north) bounding boxes around each facility."""
    with facilities_path.open("r", encoding="utf-8") as f:
        gj = json.load(f)

    bboxes: list[tuple[float, float, float, float]] = []
    for feat in gj["features"]:
        lon, lat = feat["geometry"]["coordinates"]
        bboxes.append((lon - pad_deg, lat - pad_deg, lon + pad_deg, lat + pad_deg))
    return bboxes


def normalize_firms_df(
    df: pd.DataFrame, source_feed: str, processing_mode: str
) -> pd.DataFrame:
    """Enforces identical Bronze schema across both Archive (SP) and Live (NRT) adapters."""
    if df.empty:
        return pd.DataFrame(columns=CANONICAL_COLUMNS)

    out = df.copy()
    out.columns = [c.strip().lower() for c in out.columns]

    # Ensure required numeric columns exist and are properly typed
    for col in ("latitude", "longitude", "bright_ti4", "bright_ti5", "scan", "track", "frp"):
        if col not in out.columns:
            out[col] = pd.NA
        out[col] = pd.to_numeric(out[col], errors="coerce")

    out["acq_date"] = pd.to_datetime(out["acq_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    # Critical: FIRMS acq_time can be parsed as int (e.g. 45 -> "0045")
    out["acq_time"] = (
        out["acq_time"]
        .astype(str)
        .str.replace(r"\.0$", "", regex=True)
        .str.strip()
        .str.zfill(4)
    )

    for col in ("satellite", "instrument", "version", "daynight"):
        out[col] = out.get(col, pd.Series("", index=out.index)).astype(str).str.strip()

    out["confidence"] = (
        out.get("confidence", pd.Series("", index=out.index))
        .astype(str)
        .str.strip()
        .str.lower()
    )
    out["daynight"] = out["daynight"].str.upper()
    out["source_feed"] = source_feed
    out["processing_mode"] = processing_mode
    out["ingested_at_utc"] = datetime.now(timezone.utc).isoformat()

    out = out.dropna(subset=["latitude", "longitude", "acq_date", "frp"])
    return out[CANONICAL_COLUMNS]


def filter_to_facility_bboxes(
    df: pd.DataFrame, bboxes: list[tuple[float, float, float, float]]
) -> pd.DataFrame:
    """Fast vectorized pre-filter to drop wildfire pixels far outside refinery corridors."""
    if df.empty:
        return df

    mask = pd.Series(False, index=df.index)
    for west, south, east, north in bboxes:
        mask |= (
            (df["longitude"] >= west)
            & (df["longitude"] <= east)
            & (df["latitude"] >= south)
            & (df["latitude"] <= north)
        )
    return df.loc[mask].copy()


def upsert_bronze_parquet(new_df: pd.DataFrame, bronze_path: Path = BRONZE_PATH) -> int:
    """
    Merges new detections into Bronze Parquet with deterministic deduplication.
    Standard Processing (SP) rows always take priority over Near Real-Time (NRT) rows.
    """
    if new_df.empty:
        print("[bronze] No new rows to upsert.")
        return 0

    bronze_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = bronze_path.with_suffix(".tmp.parquet")

    con = duckdb.connect()
    con.register("new_batch", new_df)

    if bronze_path.exists():
        union_sql = f"""
            SELECT * FROM read_parquet('{bronze_path.as_posix()}')
            UNION ALL
            SELECT * FROM new_batch
        """
    else:
        union_sql = "SELECT * FROM new_batch"

    dedup_sql = f"""
        COPY (
            WITH combined AS ({union_sql}),
            ranked AS (
                SELECT
                    *,
                    ROW_NUMBER() OVER (
                        PARTITION BY
                            satellite,
                            acq_date,
                            acq_time,
                            ROUND(latitude, 5),
                            ROUND(longitude, 5)
                        ORDER BY
                            CASE WHEN processing_mode = 'SP' THEN 0 ELSE 1 END ASC,
                            ingested_at_utc DESC
                    ) AS rn
                FROM combined
            )
            SELECT {', '.join(CANONICAL_COLUMNS)}
            FROM ranked
            WHERE rn = 1
            ORDER BY acq_date, acq_time, satellite
        ) TO '{tmp_path.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """
    con.execute(dedup_sql)
    total_rows = con.execute(
        f"SELECT COUNT(*) FROM read_parquet('{tmp_path.as_posix()}')"
    ).fetchone()[0]
    con.close()

    os.replace(tmp_path, bronze_path)
    print(f"[bronze] Upsert complete. Total deduplicated rows in {bronze_path}: {total_rows:,}")
    return int(total_rows)


def ingest_archive_dir(archive_dir: Path, chunksize: int = 250_000) -> None:
    csv_files = sorted(archive_dir.glob("*.csv"))
    if not csv_files:
        raise FileNotFoundError(
            f"No CSV files found in {archive_dir}. Download bulk VIIRS SP CSVs from "
            "https://firms.modaps.eosdis.nasa.gov/download/ first."
        )

    bboxes = load_facility_bboxes()
    kept_frames: list[pd.DataFrame] = []

    for csv_path in csv_files:
        print(f"[archive] Scanning {csv_path.name}...")
        source_feed = csv_path.stem.upper()
        for chunk in pd.read_csv(csv_path, dtype={"acq_time": str}, chunksize=chunksize):
            chunk.columns = [c.strip().lower() for c in chunk.columns]
            filtered = filter_to_facility_bboxes(chunk, bboxes)
            if not filtered.empty:
                kept_frames.append(
                    normalize_firms_df(filtered, source_feed=source_feed, processing_mode="SP")
                )

    if not kept_frames:
        print("[archive] No detections intersected refinery bounding boxes.")
        return

    combined = pd.concat(kept_frames, ignore_index=True)
    print(f"[archive] Extracted {len(combined):,} refinery-corridor detections.")
    upsert_bronze_parquet(combined)


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest bulk NASA FIRMS Archive CSVs.")
    parser.add_argument(
        "--archive-dir",
        type=Path,
        default=Path("data/raw_archives"),
        help="Directory containing downloaded NASA FIRMS VIIRS CSV files.",
    )
    args = parser.parse_args()
    ingest_archive_dir(args.archive_dir)


if __name__ == "__main__":
    main()
