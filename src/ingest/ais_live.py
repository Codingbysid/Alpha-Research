"""
Resilient live AISStream.io collector for Gulf Coast refinery ship channels
(Galveston/Houston/Texas City & Sabine Pass/Port Arthur).
Writes to data/ais/tankers_live.parquet and maintains data/ais/tanker_registry.parquet.

Usage:
    PYTHONPATH=. python3 src/ingest/ais_live.py --minutes 15
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import websockets
from dotenv import load_dotenv

WS_URL = "wss://stream.aisstream.io/v0/stream"
OUT_DIR = Path("data/ais")
LIVE_PINGS_PATH = OUT_DIR / "tankers_live.parquet"
REGISTRY_PATH = OUT_DIR / "tanker_registry.parquet"

# AISStream BoundingBoxes format: [[lat_sw, lon_sw], [lat_ne, lon_ne]]
GULF_REFINERY_CHANNELS = [
    [[29.15, -95.15], [29.80, -94.60]],  # Galveston Bay / Texas City / Houston Ship Channel
    [[29.60, -94.15], [30.10, -93.75]],  # Sabine Pass / Port Arthur
]


def _load_registry() -> dict[str, dict]:
    if not REGISTRY_PATH.exists():
        return {}
    df = pd.read_parquet(REGISTRY_PATH)
    return {str(r["mmsi"]): r.to_dict() for _, r in df.iterrows()}


def _save_registry(reg: dict[str, dict]) -> None:
    if not reg:
        return
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(list(reg.values())).to_parquet(REGISTRY_PATH, index=False)


async def _stream_until(deadline: float, api_key: str, reg: dict[str, dict], records: list[dict]) -> None:
    sub_msg = {
        "APIKey": api_key,
        "BoundingBoxes": GULF_REFINERY_CHANNELS,
        "FilterMessageTypes": ["PositionReport", "ShipStaticData"],
    }

    async with websockets.connect(
        WS_URL,
        compression="deflate",
        ping_interval=20,
        ping_timeout=20,
        max_size=2**22,
    ) as ws:
        await ws.send(json.dumps(sub_msg))

        while asyncio.get_running_loop().time() < deadline:
            remaining = deadline - asyncio.get_running_loop().time()
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=min(10.0, max(0.5, remaining)))
            except asyncio.TimeoutError:
                continue

            payload = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
            if "error" in payload:
                raise RuntimeError(f"AISStream error: {payload['error']}")

            msg_type = payload.get("MessageType")
            meta = payload.get("MetaData", {})
            mmsi = str(meta.get("MMSI") or meta.get("UserID") or "").strip()
            if not mmsi:
                continue

            if msg_type == "ShipStaticData":
                sd = payload.get("Message", {}).get("ShipStaticData", {})
                vtype = int(sd.get("Type") or 0)
                reg[mmsi] = {
                    "mmsi": mmsi,
                    "ship_name": str(sd.get("Name") or meta.get("ShipName") or "").strip(),
                    "vessel_type": vtype,
                    "draught_m": sd.get("MaximumStaticDraught"),
                    "destination": str(sd.get("Destination") or "").strip(),
                    "updated_utc": datetime.now(timezone.utc).isoformat(),
                }

            elif msg_type == "PositionReport":
                pr = payload.get("Message", {}).get("PositionReport", {})
                known = reg.get(mmsi)
                # Skip if already identified as a non-tanker (type known and not in 80..89)
                if known is not None and known.get("vessel_type") and not (80 <= known["vessel_type"] <= 89):
                    continue

                records.append(
                    {
                        "ts_utc": datetime.now(timezone.utc).isoformat(),
                        "mmsi": mmsi,
                        "ship_name": known.get("ship_name", "") if known else str(meta.get("ShipName") or "").strip(),
                        "vessel_type": known.get("vessel_type") if known else None,
                        "draught_m": known.get("draught_m") if known else None,
                        "destination": known.get("destination", "") if known else "",
                        "lat": pr.get("Latitude") or meta.get("latitude"),
                        "lon": pr.get("Longitude") or meta.get("longitude"),
                        "sog_knots": pr.get("Sog"),
                        "cog_deg": pr.get("Cog"),
                        "nav_status": pr.get("NavigationalStatus"),
                    }
                )


async def collect_tankers(duration_minutes: int = 15) -> None:
    load_dotenv()
    api_key = os.getenv("AISSTREAM_API_KEY", "").strip()
    if not api_key:
        raise EnvironmentError("Add AISSTREAM_API_KEY to .env (free at https://aisstream.io)")

    reg = _load_registry()
    records: list[dict] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + duration_minutes * 60
    attempt = 0

    print(f"[ais-live] Collecting Gulf Coast tanker pings for {duration_minutes}m...")
    while loop.time() < deadline:
        try:
            await _stream_until(deadline, api_key, reg, records)
            attempt = 0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            attempt += 1
            backoff = min(60.0, (2 ** min(attempt, 5))) + random.uniform(0.0, 1.5)
            print(f"[ais-live][warn] Stream interrupted ({exc}); reconnecting in {backoff:.1f}s...")
            await asyncio.sleep(min(backoff, max(0.0, deadline - loop.time())))

    _save_registry(reg)
    if not records:
        print("[ais-live] Window finished with 0 position pings.")
        return

    df_new = pd.DataFrame(records)
    for idx, r in df_new.iterrows():
        m = str(r["mmsi"])
        if m in reg:
            k = reg[m]
            if pd.isna(r["vessel_type"]) and k.get("vessel_type"):
                df_new.at[idx, "vessel_type"] = k["vessel_type"]
            if not r["ship_name"] and k.get("ship_name"):
                df_new.at[idx, "ship_name"] = k["ship_name"]
            if pd.isna(r["draught_m"]) and k.get("draught_m"):
                df_new.at[idx, "draught_m"] = k["draught_m"]
            if not r["destination"] and k.get("destination"):
                df_new.at[idx, "destination"] = k["destination"]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if LIVE_PINGS_PATH.exists():
        df_all = pd.concat([pd.read_parquet(LIVE_PINGS_PATH), df_new], ignore_index=True)
    else:
        df_all = df_new
    df_all.to_parquet(LIVE_PINGS_PATH, index=False)
    print(
        f"[ais-live] Saved {len(records):,} new pings ({df_new['mmsi'].nunique()} vessels) -> "
        f"{LIVE_PINGS_PATH} (total pings: {len(df_all):,}, known registry vessels: {len(reg)})"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--minutes", type=int, default=15)
    args = parser.parse_args()
    asyncio.run(collect_tankers(args.minutes))
