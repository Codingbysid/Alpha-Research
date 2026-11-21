"""
Ingests daily 3-2-1 Crack Spread (yfinance: CL=F, RB=F, HO=F) and
EIA API v2 Weekly Refinery Utilization & Crude Stocks with strict PIT release dates.

Usage:
    PYTHONPATH=. python3 src/ingest/energy_macro.py
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import yfinance as yf
from dotenv import load_dotenv

START_DATE = "2018-01-01"
CRACK_PATH = Path("data/silver/crack_spread.parquet")
EIA_PATH = Path("data/silver/eia_weekly.parquet")

EIA_BASE = "https://api.eia.gov/v2"
EIA_QUERIES = [
    (
        "petroleum/pnp/wiup/data/",
        {
            "WPULEUS3": "util_us_pct",
            "W_NA_YUP_R20_PER": "util_padd2_pct",
            "W_NA_YUP_R30_PER": "util_padd3_pct",
        },
        1.0,
    ),
    (
        "petroleum/stoc/wstk/data/",
        {
            "WCESTUS1": "crude_us_mbbl",
            "W_EPC0_SAX_YCUOK_MBBL": "crude_cushing_mbbl",
        },
        1.0 / 1000.0,  # EIA returns thousand barrels -> convert to million barrels
    ),
]


def _extract_close(raw: pd.DataFrame) -> pd.DataFrame:
    if isinstance(raw.columns, pd.MultiIndex):
        lvl0 = raw.columns.get_level_values(0)
        field = "Close" if "Close" in lvl0 else "Adj Close"
        px = raw[field].copy()
    else:
        field = "Close" if "Close" in raw.columns else "Adj Close"
        px = raw[[field]].copy()
    px.index = pd.to_datetime(px.index).tz_localize(None).normalize()
    return px.sort_index().ffill()


def build_crack_spread(start_date: str = START_DATE) -> pd.DataFrame:
    raw = yf.download(["CL=F", "RB=F", "HO=F"], start=start_date, auto_adjust=False, progress=False)
    px = _extract_close(raw).dropna(subset=["CL=F", "RB=F", "HO=F"])

    # 3 barrels crude -> 2 barrels RBOB (42 gal/bbl) + 1 barrel HO (42 gal/bbl)
    df = pd.DataFrame(index=px.index)
    df["wti_cl"] = px["CL=F"]
    df["rbob_gal"] = px["RB=F"]
    df["ho_gal"] = px["HO=F"]
    df["crack_321"] = 28.0 * px["RB=F"] + 14.0 * px["HO=F"] - px["CL=F"]

    df["crack_chg_5d"] = df["crack_321"].diff(5)
    df["crack_chg_20d"] = df["crack_321"].diff(20)
    mu60 = df["crack_321"].rolling(60, min_periods=20).mean()
    sd60 = df["crack_321"].rolling(60, min_periods=20).std().replace(0.0, np.nan)
    df["crack_z60"] = (df["crack_321"] - mu60) / sd60
    df["crack_above_ma20"] = df["crack_321"] > df["crack_321"].rolling(20, min_periods=10).mean()

    df = df.reset_index().rename(columns={"Date": "trading_date", "index": "trading_date"})
    CRACK_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(CRACK_PATH, index=False)
    print(
        f"[crack] Saved {len(df):,} rows ({df['trading_date'].min().date()} -> "
        f"{df['trading_date'].max().date()}) | Latest 3-2-1=${df['crack_321'].iloc[-1]:.2f}/bbl "
        f"(z60={df['crack_z60'].iloc[-1]:+.2f}) to {CRACK_PATH}"
    )
    return df


def _fetch_eia_route(api_key: str, route: str, series_map: dict[str, str], scale: float) -> pd.DataFrame:
    params: list[tuple[str, str]] = [
        ("api_key", api_key),
        ("frequency", "weekly"),
        ("data[0]", "value"),
        ("start", START_DATE),
        ("sort[0][column]", "period"),
        ("sort[0][direction]", "asc"),
        ("length", "5000"),
    ]
    for s_id in series_map:
        params.append(("facets[series][]", s_id))

    resp = requests.get(f"{EIA_BASE}/{route}", params=params, timeout=30)
    resp.raise_for_status()
    rows = resp.json().get("response", {}).get("data", [])
    if not rows:
        return pd.DataFrame()

    raw = pd.DataFrame(rows)
    raw["period"] = pd.to_datetime(raw["period"]).dt.normalize()
    raw["value"] = pd.to_numeric(raw["value"], errors="coerce") * scale
    piv = raw.pivot_table(index="period", columns="series", values="value", aggfunc="last")
    return piv.rename(columns=series_map)


def build_eia_weekly() -> pd.DataFrame | None:
    load_dotenv()
    api_key = os.getenv("EIA_API_KEY", "").strip()
    if not api_key:
        print("[eia] EIA_API_KEY not found in .env — skipping EIA weekly ingest.")
        return None

    frames = [_fetch_eia_route(api_key, route, smap, sc) for route, smap, sc in EIA_QUERIES]
    frames = [f for f in frames if not f.empty]
    if not frames:
        raise RuntimeError("EIA API returned zero rows across all series.")

    eia = pd.concat(frames, axis=1).sort_index().reset_index().rename(columns={"period": "week_ending_friday"})

    # EIA WPSR covers week ending Friday and publishes the next Wednesday at 10:30 AM ET
    eia["release_date"] = eia["week_ending_friday"] + pd.Timedelta(days=5)

    for col in ("util_us_pct", "util_padd2_pct", "util_padd3_pct", "crude_us_mbbl", "crude_cushing_mbbl"):
        if col in eia.columns:
            eia[f"{col}_wow"] = eia[col].diff(1)
            # Surprise proxy: actual WoW change minus trailing 4-week mean change
            eia[f"{col}_surprise"] = eia[f"{col}_wow"] - eia[f"{col}_wow"].rolling(4).mean().shift(1)

    EIA_PATH.parent.mkdir(parents=True, exist_ok=True)
    eia.to_parquet(EIA_PATH, index=False)
    print(
        f"[eia] Saved {len(eia):,} weekly reports ({eia['week_ending_friday'].min().date()} -> "
        f"{eia['week_ending_friday'].max().date()}) to {EIA_PATH}"
    )
    return eia


if __name__ == "__main__":
    build_crack_spread()
    build_eia_weekly()
