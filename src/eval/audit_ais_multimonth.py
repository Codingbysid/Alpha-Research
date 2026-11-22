"""
Multi-month Gulf Coast AIS + VIIRS Thermal Sensor Fusion Audit.
Reads all .csv, .csv.zst, and .parquet files in data/raw_ais/ via DuckDB,
builds data/ais/gulf_tanker_daily.parquet, and evaluates non-overlapping 10d IC
and the 2x2 Thermal x Maritime Dwell matrix across regimes.

Usage:
    PYTHONPATH=. python3 -W ignore src/eval/audit_ais_multimonth.py
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
from scipy.stats import spearmanr

from src.eval.audit_short_q5 import CLOUD_MAX, HOLDING_DAYS, build_panel

RAW_AIS_DIR = Path("data/raw_ais")
OUT_PATH = Path("data/ais/gulf_tanker_daily.parquet")
GULF_BBOX = (-95.8, 28.8, -93.6, 30.2)
GULF_TICKERS = {"VLO", "MPC", "PSX", "PBF", "DK"}


def _resolve_cols(con: duckdb.DuckDBPyConnection, src_expr: str) -> dict[str, str]:
    cols = {c.lower(): c for c in con.execute(f"SELECT * FROM {src_expr} LIMIT 0").df().columns}

    def pick(candidates: list[str]) -> str:
        for k in candidates:
            if k in cols:
                return f'"{cols[k]}"'
        raise KeyError(f"None of {candidates} found in {list(cols.keys())}")

    return {
        "mmsi": pick(["mmsi"]),
        "lat": pick(["latitude", "lat"]),
        "lon": pick(["longitude", "lon"]),
        "sog": pick(["sog"]),
        "draft": pick(["draft", "draught"]),
        "vtype": pick(["vessel_type", "vesseltype"]),
        "ts": pick(["base_date_time", "basedatetime", "timestamp"]),
    }


def build_multimonth_ais() -> pd.DataFrame:
    files = sorted(
        list(RAW_AIS_DIR.glob("*.csv"))
        + list(RAW_AIS_DIR.glob("*.csv.zst"))
        + list(RAW_AIS_DIR.glob("*.parquet"))
    )
    if not files:
        raise FileNotFoundError(f"No AIS files found in {RAW_AIS_DIR}.")

    west, south, east, north = GULF_BBOX
    con = duckdb.connect()
    parts: list[pd.DataFrame] = []

    for fpath in files:
        print(f"[ais] Scanning {fpath.name}...")
        src_expr = (
            f"read_parquet('{fpath.as_posix()}')"
            if fpath.suffix.lower() == ".parquet"
            else f"read_csv_auto('{fpath.as_posix()}', all_varchar=True)"
        )
        
        # Check if already pre-aggregated daily parquet
        col_names = [c.lower() for c in con.execute(f"SELECT * FROM {src_expr} LIMIT 0").df().columns]
        if "tanker_count" in col_names and "obs_date" in col_names:
            df_part = con.execute(f"SELECT obs_date, tanker_count, mean_draught_m, slow_dwell_share FROM {src_expr}").df()
            if not df_part.empty:
                parts.append(df_part)
            continue

        c = _resolve_cols(con, src_expr)
        q = f"""
            SELECT
                CAST(TRY_CAST({c['ts']} AS TIMESTAMP) AS DATE) AS obs_date,
                COUNT(DISTINCT CAST({c['mmsi']} AS VARCHAR)) AS tanker_count,
                AVG(CASE WHEN TRY_CAST({c['draft']} AS DOUBLE) > 1.0
                          AND TRY_CAST({c['draft']} AS DOUBLE) < 35.0
                         THEN TRY_CAST({c['draft']} AS DOUBLE) END) AS mean_draught_m,
                AVG(CASE WHEN TRY_CAST({c['sog']} AS DOUBLE) IS NOT NULL
                          AND TRY_CAST({c['sog']} AS DOUBLE) < 1.5
                         THEN 1.0 ELSE 0.0 END) AS slow_dwell_share
            FROM {src_expr}
            WHERE TRY_CAST({c['vtype']} AS INTEGER) BETWEEN 80 AND 89
              AND TRY_CAST({c['lon']} AS DOUBLE) BETWEEN {west} AND {east}
              AND TRY_CAST({c['lat']} AS DOUBLE) BETWEEN {south} AND {north}
              AND TRY_CAST({c['ts']} AS TIMESTAMP) IS NOT NULL
            GROUP BY 1
        """
        df_part = con.execute(q).df()
        if not df_part.empty:
            parts.append(df_part)

    con.close()
    daily = (
        pd.concat(parts, ignore_index=True)
        .groupby("obs_date", as_index=False)
        .agg(
            tanker_count=("tanker_count", "max"),
            mean_draught_m=("mean_draught_m", "mean"),
            slow_dwell_share=("slow_dwell_share", "mean"),
        )
        .sort_values("obs_date")
        .reset_index(drop=True)
    )
    # Drop truncated boundary days (< 50 active tankers)
    daily = daily[daily["tanker_count"] >= 50].copy()
    daily["trading_date"] = pd.to_datetime(daily["obs_date"]).dt.normalize()

    mu60 = daily["tanker_count"].rolling(60, min_periods=10).mean()
    sd60 = daily["tanker_count"].rolling(60, min_periods=10).std().replace(0.0, np.nan)
    daily["tanker_z60"] = (daily["tanker_count"] - mu60) / sd60
    daily["draught_chg_10d"] = daily["mean_draught_m"].diff(10)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    daily.to_parquet(OUT_PATH, index=False)
    print(
        f"[ais] Built {len(daily):,} Gulf tanker days "
        f"({daily['trading_date'].min().date()} -> {daily['trading_date'].max().date()}) -> {OUT_PATH}"
    )
    return daily


def run_audit(ais_df: pd.DataFrame) -> None:
    panel = build_panel()
    gulf = panel[
        panel["ticker"].isin(GULF_TICKERS)
        & panel["yoy_per_pass"].notna()
        & panel["fwd_ret_10d_excess"].notna()
    ].copy()

    m = gulf.merge(
        ais_df[["trading_date", "tanker_count", "tanker_z60", "mean_draught_m", "draught_chg_10d", "slow_dwell_share"]],
        on="trading_date",
        how="inner",
    )
    print(f"\n[audit] Merged Gulf rows: {len(m):,} across {m['trading_date'].nunique()} trading days")
    print(f"[audit] Regimes: {m['regime_label'].value_counts().to_dict()}")

    # 1. Non-overlapping 10d IC
    all_dates = sorted(m["trading_date"].unique())
    no_m = m[m["trading_date"].isin(set(all_dates[::HOLDING_DAYS]))]
    print("\n=== HONEST NON-OVERLAPPING 10d IC (ALL MERGED DAYS) ===")
    for col in ("tanker_z60", "slow_dwell_share", "mean_draught_m", "draught_chg_10d", "yoy_per_pass"):
        sub = no_m[[col, "fwd_ret_10d_excess"]].dropna()
        if len(sub) >= 15:
            ic, p = spearmanr(sub[col], sub["fwd_ret_10d_excess"])
            t = ic * np.sqrt((len(sub) - 2) / max(1e-12, 1.0 - ic**2))
            print(f"  {col:18s} | n={len(sub):3d} | IC={ic:+.4f} | t={t:+.2f} | p={p:.4f}")

    # 2. 2x2 Matrix split by Regime (Clear Sky <= 40%)
    cs = m[m["cloud_cover_pct_weighted"] <= CLOUD_MAX].copy()
    for reg_name in ["ALL", "IDIOSYNCRATIC", "MACRO_PANIC"]:
        sub_r = cs if reg_name == "ALL" else cs[cs["regime_label"] == reg_name]
        if len(sub_r) < 20:
            continue
        med_yoy = sub_r["yoy_per_pass"].median()
        med_dwl = sub_r["slow_dwell_share"].median()
        print(f"\n=== 2x2 THERMAL x DWELL MATRIX ({reg_name} | CLEAR SKY, N={len(sub_r)}) ===")
        for t_cond, t_lbl in [(sub_r["yoy_per_pass"] >= med_yoy, "HIGH THERMAL"), (sub_r["yoy_per_pass"] < med_yoy, "LOW THERMAL")]:
            for d_cond, d_lbl in [(sub_r["slow_dwell_share"] >= med_dwl, "HIGH DWELL"), (sub_r["slow_dwell_share"] < med_dwl, "LOW DWELL")]:
                cell = sub_r[t_cond & d_cond]["fwd_ret_10d_excess"]
                if not cell.empty:
                    print(
                        f"  {t_lbl:12s} + {d_lbl:10s} | n={len(cell):3d} "
                        f"| 10d Mean={cell.mean()*100:+6.2f}% | Short WR={(cell < 0).mean()*100:5.1f}%"
                    )


if __name__ == "__main__":
    ais = build_multimonth_ais()
    run_audit(ais)
