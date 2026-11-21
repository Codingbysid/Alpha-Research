"""
2018-2026 VIIRS Thermal Sensor-Visibility and 10-Day Rank IC Audit
for 5 Candidate Expansion Stocks:
- Energy Sleeve (Short Q5 vs XLE): PARR, CLMT, LNG
- Metals Sleeve (Long Q5 vs XME): CMC, CENX

Usage:
    PYTHONPATH=. python3 -W ignore src/eval/audit_expansion_candidates.py
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import geopandas as gpd
import numpy as np
import pandas as pd
import requests
import yfinance as yf
from scipy.stats import spearmanr
from shapely.geometry import Point

ARCHIVE_DIR = Path("data/raw_archives")
FACILITIES_PATH = Path("config/facilities_expansion_test.geojson")
OUT_DIR = Path("data/industrial")
BRONZE_PATH = OUT_DIR / "bronze_expansion.parquet"
CLOUD_PATH = OUT_DIR / "cloud_expansion.parquet"

HOLDING_DAYS = 10
CLOUD_MAX = 40.0
BETA_WINDOW = 60


def load_expansion_bboxes(pad_deg: float = 0.08) -> list[tuple[float, float, float, float]]:
    with FACILITIES_PATH.open("r", encoding="utf-8") as f:
        gj = json.load(f)
    bboxes = []
    for feat in gj["features"]:
        lon, lat = feat["geometry"]["coordinates"]
        bboxes.append((lon - pad_deg, lat - pad_deg, lon + pad_deg, lat + pad_deg))
    return bboxes


def ingest_expansion_bronze() -> pd.DataFrame:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if BRONZE_PATH.exists():
        print(f"[bronze] Loading cached {BRONZE_PATH}...")
        return pd.read_parquet(BRONZE_PATH)

    files = sorted(ARCHIVE_DIR.glob("fire_archive_*.csv")) + sorted(ARCHIVE_DIR.glob("fire_nrt_*.csv"))
    if not files:
        raise FileNotFoundError(f"No VIIRS CSVs in {ARCHIVE_DIR}")

    bboxes = load_expansion_bboxes()
    clauses = [
        f"(CAST(latitude AS DOUBLE) BETWEEN {s:.4f} AND {n:.4f} AND CAST(longitude AS DOUBLE) BETWEEN {w:.4f} AND {e:.4f})"
        for (w, s, e, n) in bboxes
    ]
    spatial_filter = " OR ".join(clauses)

    con = duckdb.connect()
    parts = []
    for fpath in files:
        print(f"[bronze] Filtering {fpath.name}...")
        q = f"""
            SELECT
                TRY_CAST(latitude AS DOUBLE) AS latitude,
                TRY_CAST(longitude AS DOUBLE) AS longitude,
                TRY_CAST(frp AS DOUBLE) AS frp,
                acq_date,
                acq_time,
                satellite,
                daynight
            FROM read_csv_auto('{fpath.as_posix()}', all_varchar=True)
            WHERE ({spatial_filter})
              AND daynight = 'N'
        """
        df_part = con.execute(q).df()
        if not df_part.empty:
            parts.append(df_part)
    con.close()

    raw = pd.concat(parts, ignore_index=True)
    raw["acq_time_clean"] = raw["acq_time"].astype(str).str.zfill(4)
    raw["utc_dt"] = pd.to_datetime(
        raw["acq_date"].astype(str) + " " + raw["acq_time_clean"],
        format="%Y-%m-%d %H%M",
        utc=True,
    )
    raw["trading_date"] = (raw["utc_dt"] - pd.Timedelta(hours=4)).dt.normalize().dt.tz_localize(None)

    raw.to_parquet(BRONZE_PATH, index=False)
    print(f"[bronze] Saved {len(raw):,} expansion detections -> {BRONZE_PATH}")
    return raw


def get_cloud_cover_expansion(fac_gdf: gpd.GeoDataFrame) -> pd.DataFrame:
    if CLOUD_PATH.exists():
        return pd.read_parquet(CLOUD_PATH)

    coords = fac_gdf[["facility_id", "geometry"]].copy()
    coords["lat"] = coords.geometry.y.round(4)
    coords["lon"] = coords.geometry.x.round(4)

    frames = []
    for _, r in coords.iterrows():
        fac_id = r["facility_id"]
        lat, lon = r["lat"], r["lon"]
        print(f"[cloud] Fetching Open-Meteo for {fac_id} ({lat}, {lon})...")
        url = "https://archive-api.open-meteo.com/v1/archive"
        params = {
            "latitude": lat,
            "longitude": lon,
            "start_date": "2018-01-01",
            "end_date": "2026-11-20",
            "hourly": "cloud_cover",
            "timezone": "America/New_York",
        }
        resp = requests.get(url, params=params, timeout=30)
        data = resp.json()
        if "hourly" in data:
            cdf = pd.DataFrame(data["hourly"])
            cdf["time"] = pd.to_datetime(cdf["time"])
            # Nighttime 21:00 - 05:00 ET
            night = cdf[cdf["time"].dt.hour.isin([21, 22, 23, 0, 1, 2, 3, 4, 5])].copy()
            night["trading_date"] = night["time"].dt.normalize()
            daily_c = (
                night.groupby("trading_date", as_index=False)["cloud_cover"]
                .mean()
                .rename(columns={"cloud_cover": "cloud_cover_pct"})
            )
            daily_c["facility_id"] = fac_id
            frames.append(daily_c)

    res = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if not res.empty:
        res.to_parquet(CLOUD_PATH, index=False)
    return res


def run_audit() -> None:
    fac_gdf = gpd.read_file(FACILITIES_PATH)
    bronze = ingest_expansion_bronze()

    # Spatial join to facilities
    pts = [Point(xy) for xy in zip(bronze["longitude"], bronze["latitude"])]
    b_gdf = gpd.GeoDataFrame(bronze, geometry=pts, crs="EPSG:4326").to_crs("EPSG:3857")
    f_proj = fac_gdf.to_crs("EPSG:3857").copy()
    f_proj["geom_buf"] = f_proj.geometry.buffer(f_proj["buffer_km"] * 1000.0)
    f_buf = gpd.GeoDataFrame(f_proj.drop(columns=["geometry"]), geometry="geom_buf", crs="EPSG:3857")

    joined = gpd.sjoin(b_gdf, f_buf, how="inner", predicate="within")
    print(f"\n[spatial] Matched {len(joined):,} detections to expansion facilities.")

    # Detection breakdown by ticker
    print("\n=== SENSOR VISIBILITY & DETECTION COUNTS (2018-2026) ===")
    det_sum = joined.groupby(["ticker", "facility_name", "sector"]).agg(
        n_detections=("frp", "count"),
        total_frp=("frp", "sum"),
        mean_frp=("frp", "mean"),
        max_frp=("frp", "max"),
    ).reset_index()
    print(det_sum.to_string(index=False))

    # Daily aggregation
    daily_fac = (
        joined.groupby(["facility_id", "ticker", "sector", "hedge_etf", "capacity_bpd", "trading_date"], as_index=False)
        .agg(
            frp_night_mw=("frp", "sum"),
            satellite_pass_count=("satellite", "nunique"),
        )
    )
    daily_fac["frp_per_pass"] = np.where(
        daily_fac["satellite_pass_count"] > 0,
        daily_fac["frp_night_mw"] / daily_fac["satellite_pass_count"],
        0.0,
    )

    # Cloud cover
    cloud = get_cloud_cover_expansion(fac_gdf)
    if not cloud.empty:
        daily_fac = daily_fac.merge(cloud, on=["facility_id", "trading_date"], how="left")
    else:
        daily_fac["cloud_cover_pct"] = 0.0

    # Capacity weighting per ticker
    cap = daily_fac[["facility_id", "ticker", "capacity_bpd"]].drop_duplicates()
    cap["w"] = cap["capacity_bpd"] / cap.groupby("ticker")["capacity_bpd"].transform("sum")
    daily_fac = daily_fac.merge(cap[["facility_id", "w"]], on="facility_id", how="left")
    daily_fac["w_frp_pp"] = daily_fac["frp_per_pass"] * daily_fac["w"]

    ticker_daily = (
        daily_fac.groupby(["ticker", "sector", "hedge_etf", "trading_date"], as_index=False)
        .agg(
            w_frp_pp=("w_frp_pp", "sum"),
            cloud_pct=("cloud_cover_pct", "mean"),
        )
    )

    # YoY 5d vs 21d ratio
    frames = []
    for tkr, g in ticker_daily.groupby("ticker"):
        g = g.sort_values("trading_date").copy()
        clean = g["w_frp_pp"].where(g["cloud_pct"] <= CLOUD_MAX)
        g["yoy_per_pass"] = clean.rolling(5, min_periods=2).mean() / clean.rolling(
            21, min_periods=7
        ).mean().shift(252).replace(0.0, np.nan)
        frames.append(g)
    ticker_df = pd.concat(frames, ignore_index=True)

    # Download Market Data
    all_tickers = sorted(ticker_df["ticker"].unique().tolist())
    symbols = all_tickers + ["XLE", "XME"]
    print(f"\n[market] Downloading price history for {symbols}...")
    raw = yf.download(symbols, start="2018-01-01", auto_adjust=False, progress=False)
    lvl0 = raw.columns.get_level_values(0)
    field = "Adj Close" if "Adj Close" in lvl0 else "Close"
    px = raw[field].copy()
    px.index = pd.to_datetime(px.index).tz_localize(None).normalize()
    px = px.sort_index()
    rets = px.pct_change()

    # Beta hedge to respective ETF
    mkt_rows = []
    for tkr in all_tickers:
        if tkr not in px.columns:
            continue
        etf = ticker_df.loc[ticker_df["ticker"] == tkr, "hedge_etf"].iloc[0]
        beta = rets[tkr].rolling(BETA_WINDOW, min_periods=30).cov(rets[etf]) / rets[etf].rolling(
            BETA_WINDOW, min_periods=30
        ).var().replace(0.0, np.nan)
        fwd_s = px[tkr].shift(-HOLDING_DAYS) / px[tkr] - 1.0
        fwd_e = px[etf].shift(-HOLDING_DAYS) / px[etf] - 1.0
        mkt_rows.append(
            pd.DataFrame(
                {
                    "trading_date": px.index,
                    "ticker": tkr,
                    "beta": beta.values,
                    "fwd_ret_10d_excess": (fwd_s - beta * fwd_e).values,
                }
            )
        )
    mkt_df = pd.concat(mkt_rows, ignore_index=True).dropna(subset=["fwd_ret_10d_excess"])

    audit_df = ticker_df.merge(mkt_df, on=["ticker", "trading_date"], how="inner")

    # Evaluate Non-Overlapping 10d Rank IC
    all_dates = sorted(audit_df["trading_date"].unique())
    no_df = audit_df[
        audit_df["trading_date"].isin(set(all_dates[::HOLDING_DAYS]))
        & (audit_df["cloud_pct"] <= CLOUD_MAX)
        & audit_df["yoy_per_pass"].notna()
        & audit_df["fwd_ret_10d_excess"].notna()
    ]

    print("\n=== EXPANSION CANDIDATE AUDIT (NON-OVERLAPPING 10-DAY IC & EXCESS RETURN) ===")
    print(f"{'Ticker':6s} | {'Sector':8s} | {'Hedge':5s} | {'n (10d)':7s} | {'Spearman IC':11s} | {'t-stat':6s} | {'p-val':6s} | {'Expected Sign':13s} | {'Sign Match?':11s}")
    print("-" * 88)

    results = []
    for tkr in all_tickers:
        sub = no_df[no_df["ticker"] == tkr]
        sec = ticker_df.loc[ticker_df["ticker"] == tkr, "sector"].iloc[0]
        etf = ticker_df.loc[ticker_df["ticker"] == tkr, "hedge_etf"].iloc[0]
        exp_sign = "NEGATIVE (-)" if sec in ("REFINING", "LNG") else "POSITIVE (+)"

        if len(sub) >= 15:
            ic, p = spearmanr(sub["yoy_per_pass"], sub["fwd_ret_10d_excess"])
            t = ic * np.sqrt((len(sub) - 2) / max(1e-12, 1.0 - ic**2))
            sign_match = "YES (PASS)" if (ic < 0 if exp_sign.startswith("NEG") else ic > 0) else "NO (FAIL)"
            print(f"{tkr:6s} | {sec:8s} | {etf:5s} | {len(sub):7d} | {ic:+11.4f} | {t:+6.2f} | {p:6.4f} | {exp_sign:13s} | {sign_match:11s}")
            results.append({"ticker": tkr, "sector": sec, "hedge": etf, "n": len(sub), "ic": ic, "t": t, "p": p, "pass": sign_match.startswith("YES")})
        else:
            print(f"{tkr:6s} | {sec:8s} | {etf:5s} | {len(sub):7d} | {'INSUFFICIENT DATA (n < 15)':35s}")

    # Summary of Q5 returns (top 20% anomaly)
    print("\n=== Q5 EXTREME THERMAL ANOMALY 10-DAY EXCESS RETURN ===")
    for tkr in all_tickers:
        sub_all = audit_df[(audit_df["ticker"] == tkr) & (audit_df["cloud_pct"] <= CLOUD_MAX) & audit_df["yoy_per_pass"].notna()]
        if len(sub_all) < 30:
            continue
        q80 = sub_all["yoy_per_pass"].quantile(0.80)
        q5 = sub_all[sub_all["yoy_per_pass"] >= q80]
        q1_4 = sub_all[sub_all["yoy_per_pass"] < q80]
        q5_ret = q5["fwd_ret_10d_excess"].mean() * 100.0
        q14_ret = q1_4["fwd_ret_10d_excess"].mean() * 100.0
        sec = ticker_df.loc[ticker_df["ticker"] == tkr, "sector"].iloc[0]
        action = "SHORT Q5" if sec in ("REFINING", "LNG") else "LONG Q5"
        edge = -q5_ret if action == "SHORT Q5" else q5_ret
        print(f"  {tkr:5s} ({sec:8s}) | Q5 n={len(q5):3d} | Q5 10d Ret={q5_ret:+6.2f}% | Rest Ret={q14_ret:+6.2f}% | {action} Edge={edge:+6.2f}%")


if __name__ == "__main__":
    run_audit()
