"""
Live NASA FIRMS Area API poller for US refinery corridors.

Usage:
    python src/ingest/firms_live.py --day-range 2
"""

from __future__ import annotations

import argparse
import io
import os
import time
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

from src.ingest.firms_archive import (
    BRONZE_PATH,
    FACILITIES_PATH,
    load_facility_bboxes,
    normalize_firms_df,
    upsert_bronze_parquet,
)

FIRMS_AREA_URL = (
    "https://firms.modaps.eosdis.nasa.gov/api/area/csv/"
    "{map_key}/{source}/{west:.4f},{south:.4f},{east:.4f},{north:.4f}/{day_range}"
)

# NRT endpoints include URT (<60s) and RT (<60m) detections across the US & Canada
DEFAULT_SOURCES = ("VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT")


def fetch_area_csv(
    map_key: str,
    source: str,
    bbox: tuple[float, float, float, float],
    day_range: int = 2,
    timeout: int = 30,
) -> pd.DataFrame:
    west, south, east, north = bbox
    url = FIRMS_AREA_URL.format(
        map_key=map_key,
        source=source,
        west=west,
        south=south,
        east=east,
        north=north,
        day_range=day_range,
    )
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()

    text = resp.text.strip()
    if not text:
        return pd.DataFrame()

    # NASA FIRMS returns plain-text error messages with HTTP 200 on invalid keys/limits
    first_line = text.splitlines()[0].lower()
    if "latitude" not in first_line:
        raise RuntimeError(f"NASA FIRMS API error for {source}: {text[:200]}")

    return pd.read_csv(io.StringIO(text), dtype={"acq_time": str})


def poll_live_firms(
    day_range: int = 2,
    sources: tuple[str, ...] = DEFAULT_SOURCES,
    facilities_path: Path = FACILITIES_PATH,
    bronze_path: Path | None = None,
    sector: str | None = None,
    log_prefix: str = "live",
) -> tuple[int, bool]:
    load_dotenv()
    map_key = os.getenv("NASA_FIRMS_MAP_KEY", "").strip()
    if not map_key or map_key == "your_32_char_key_here":
        raise EnvironmentError(
            "Missing NASA_FIRMS_MAP_KEY in .env. Register free at "
            "https://firms.modaps.eosdis.nasa.gov/api/map_key"
        )

    if not 1 <= day_range <= 5:
        raise ValueError("NASA FIRMS Area API requires day_range between 1 and 5.")

    dest_bronze = bronze_path if bronze_path is not None else BRONZE_PATH
    bboxes = load_facility_bboxes(
        facilities_path=facilities_path, pad_deg=0.06, sector=sector
    )
    frames: list[pd.DataFrame] = []
    requests_ok = 0
    requests_failed = 0
    sector_note = f" sector={sector}" if sector else ""

    for source in sources:
        for idx, bbox in enumerate(bboxes, start=1):
            try:
                raw_df = fetch_area_csv(
                    map_key=map_key,
                    source=source,
                    bbox=bbox,
                    day_range=day_range,
                )
                requests_ok += 1
                if not raw_df.empty:
                    frames.append(
                        normalize_firms_df(
                            raw_df, source_feed=source, processing_mode="NRT"
                        )
                    )
            except Exception as exc:
                requests_failed += 1
                print(f"[{log_prefix}][warn] {source} bbox #{idx} failed: {exc}")
            time.sleep(0.15)  # Polite pacing

    feed_ok = (requests_failed == 0) and (requests_ok > 0)
    if requests_failed > 0:
        print(
            f"[{log_prefix}][error] FEED_ERROR: FIRMS poll encountered {requests_failed} failed/timeout requests "
            f"(succeeded={requests_ok},{sector_note} facilities={len(bboxes)})."
        )

    if not frames:
        if not feed_ok:
            print(
                f"[{log_prefix}][error] FEED_ERROR: Poll failed due to API errors; "
                "suppressing new thermal detections."
            )
            return 0, False
        print(
            f"[{log_prefix}] Poll succeeded; zero new thermal detections across "
            f"{len(bboxes)} facilities{sector_note}."
        )
        return 0, True

    batch_df = pd.concat(frames, ignore_index=True)
    print(
        f"[{log_prefix}] Pulled {len(batch_df):,} raw detections across {len(sources)} sensors "
        f"({len(bboxes)} facilities{sector_note}, feed_ok={feed_ok}) -> {dest_bronze}"
    )
    return upsert_bronze_parquet(batch_df, bronze_path=dest_bronze), feed_ok


def main() -> None:
    parser = argparse.ArgumentParser(description="Poll live NASA FIRMS VIIRS detections.")
    parser.add_argument(
        "--day-range",
        type=int,
        default=2,
        help="Number of trailing days to query (1 to 5).",
    )
    args = parser.parse_args()
    poll_live_firms(day_range=args.day_range)


if __name__ == "__main__":
    main()
