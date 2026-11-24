"""
Controlled 2x2 Evaluation for US Steel Sleeve (CLF, NUE, STLD, CMC vs XME):
Grid Dimensions:
  1. Coordinates: Original (config/facilities_industrial.geojson) vs. Verified (audit_evidence_pack/facilities_steel_verified.geojson)
  2. Denominator: All detected acquisition timestamps vs. Nighttime-only detected timestamps

Reports for all 4 cells:
  - Matched detections count (total & per ticker)
  - Q5 triggers count (total & per ticker)
  - Distinct episodes count (actual executed trades under paper single-position rule)
  - Net results per ticker (Win Rate %, Mean Net BPS, Total Net BPS, Net Return)
  - Driver analysis explaining which names or sites account for any sign change.
"""

from __future__ import annotations

import json
from pathlib import Path
import warnings

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import Point

warnings.filterwarnings("ignore")

from src.transform.feature_engine import assign_trading_date_et

AUDIT_DIR = Path("audit_evidence_pack")
BRONZE_IND_PATH = Path("data/industrial/bronze_industrial.parquet")
CLOUD_IND_PATH = Path("data/industrial/cloud_industrial.parquet")
PX_PATH = AUDIT_DIR / "market_prices.parquet"

STEEL_TICKERS = ["CLF", "NUE", "STLD", "CMC"]
CLOUD_MAX = 40.0
HOLDING_DAYS = 10
BETA_WINDOW = 60
COST_RT = 0.0025  # 25 bps
INITIAL_EQUITY = 100_000.0
MAX_SINGLE_WT = 0.10
MAX_GROSS_WT = 0.40


def spatial_join_buffered(bronze_df: pd.DataFrame, fac_gdf: gpd.GeoDataFrame) -> pd.DataFrame:
    """Spatially joins bronze detections to facilities using facility-specific metric buffers."""
    pts = [Point(xy) for xy in zip(bronze_df["longitude"], bronze_df["latitude"])]
    pts_gdf = gpd.GeoDataFrame(bronze_df, geometry=pts, crs="EPSG:4326")

    # Buffer in EPSG:3857 (meters) then convert back to EPSG:4326
    fac_proj = fac_gdf.to_crs(epsg=3857).copy()
    fac_proj["geometry"] = fac_proj.geometry.buffer(fac_proj["buffer_km"] * 1000.0)
    poly_gdf = fac_proj.to_crs(epsg=4326)[["facility_id", "facility_name", "ticker", "capacity_bpd", "geometry"]]

    matched = gpd.sjoin(pts_gdf, poly_gdf, how="inner", predicate="intersects")
    matched = matched.drop(columns=["index_right"]).reset_index(drop=True)
    return pd.DataFrame(matched)


def build_steel_features(
    bronze_df: pd.DataFrame,
    fac_gdf: gpd.GeoDataFrame,
    denominator_mode: str,  # "all_passes" or "night_only"
) -> tuple[pd.DataFrame, int, dict[str, int]]:
    """Builds daily steel features for the given coordinates and denominator mode."""
    matched = spatial_join_buffered(bronze_df, fac_gdf)
    total_matched = len(matched)
    ticker_matched = matched.groupby("ticker").size().to_dict()

    matched["acq_datetime_utc"] = pd.to_datetime(
        matched["acq_date"] + " " + matched["acq_time"].astype(str).str.zfill(4),
        format="%Y-%m-%d %H%M",
        utc=True,
    )
    matched["trading_date"] = assign_trading_date_et(matched["acq_datetime_utc"])
    matched["frp_night"] = np.where(matched["daynight"] == "N", matched["frp"], 0.0)

    if denominator_mode == "all_passes":
        matched["pass_token"] = matched["acq_datetime_utc"]
    else:
        matched["pass_token"] = matched["acq_datetime_utc"].where(matched["daynight"] == "N")

    fac_agg = matched.groupby(["facility_id", "trading_date"], as_index=False).agg(
        frp_night_mw=("frp_night", "sum"),
        satellite_pass_count=("pass_token", "nunique"),
    )
    fac_agg["frp_per_pass"] = np.where(
        fac_agg["satellite_pass_count"] > 0,
        fac_agg["frp_night_mw"] / fac_agg["satellite_pass_count"],
        0.0,
    )

    # Spine over all business days
    start_dt = pd.Timestamp("2018-01-01")
    end_dt = pd.Timestamp("2026-11-23")
    bdays = pd.bdate_range(start=start_dt, end=end_dt)

    spine = pd.MultiIndex.from_product(
        [fac_gdf["facility_id"].unique(), bdays],
        names=["facility_id", "trading_date"],
    ).to_frame().reset_index(drop=True)

    fac_daily = (
        spine.merge(fac_agg, on=["facility_id", "trading_date"], how="left")
        .merge(
            fac_gdf[["facility_id", "ticker", "capacity_bpd"]],
            on="facility_id",
            how="left",
        )
        .fillna({"frp_night_mw": 0.0, "satellite_pass_count": 0, "frp_per_pass": 0.0})
        .sort_values(["facility_id", "trading_date"])
        .reset_index(drop=True)
    )

    # 365-day (252 bdays) YoY
    fac_daily["frp_per_pass_365d"] = fac_daily.groupby("facility_id")["frp_per_pass"].shift(252)

    # Ticker-level capacity aggregation
    fac_daily["frp_per_pass_wt"] = fac_daily["frp_per_pass"] * fac_daily["capacity_bpd"]
    fac_daily["frp_per_pass_365d_wt"] = fac_daily["frp_per_pass_365d"] * fac_daily["capacity_bpd"]

    ticker_agg = (
        fac_daily.groupby(["ticker", "trading_date"], as_index=False)
        .agg(
            frp_per_pass_sum=("frp_per_pass_wt", "sum"),
            frp_per_pass_365d_sum=("frp_per_pass_365d_wt", "sum"),
            capacity_sum=("capacity_bpd", "sum"),
        )
    )
    ticker_agg["frp_per_pass_mean"] = ticker_agg["frp_per_pass_sum"] / ticker_agg["capacity_sum"]
    ticker_agg["frp_per_pass_365d_mean"] = ticker_agg["frp_per_pass_365d_sum"] / ticker_agg["capacity_sum"]
    ticker_agg["yoy_per_pass"] = (ticker_agg["frp_per_pass_sum"] + 0.1) / (ticker_agg["frp_per_pass_365d_sum"] + 0.1)

    # Merge cloud cover
    cloud_df = pd.read_parquet(CLOUD_IND_PATH)
    cloud_fac = cloud_df.merge(
        fac_gdf[["facility_id", "ticker", "capacity_bpd"]],
        on="facility_id",
        how="inner",
    )
    cloud_fac["cloud_wt"] = cloud_fac["cloud_cover_pct"] * cloud_fac["capacity_bpd"]
    cloud_ticker = (
        cloud_fac.groupby(["ticker", "trading_date"], as_index=False)
        .agg(cloud_wt_sum=("cloud_wt", "sum"), cap_sum=("capacity_bpd", "sum"))
    )
    cloud_ticker["cloud_cover_pct_weighted"] = cloud_ticker["cloud_wt_sum"] / cloud_ticker["cap_sum"]

    ticker_agg = ticker_agg.merge(
        cloud_ticker[["ticker", "trading_date", "cloud_cover_pct_weighted"]],
        on=["ticker", "trading_date"],
        how="left",
    )
    return ticker_agg, total_matched, ticker_matched


def run_steel_backtest(
    panel_df: pd.DataFrame,
    px_matrix: pd.DataFrame,
    xme_series: pd.Series,
    all_dates: list[pd.Timestamp],
) -> tuple[dict, pd.DataFrame]:
    """Runs T+1 single-position daily MTM replay for steel."""
    date_to_idx = {d: i for i, d in enumerate(all_dates)}
    n_days = len(all_dates)

    q_cache: dict[pd.Timestamp, float | None] = {}

    def get_q80(dt: pd.Timestamp) -> float | None:
        if dt in q_cache:
            return q_cache[dt]
        idx = date_to_idx[dt]
        if idx < 10:
            q_cache[dt] = None
            return None
        hist_dt = all_dates[idx - 10]
        hist = panel_df[
            (panel_df["trading_date"] <= hist_dt)
            & (panel_df["cloud_cover_pct_weighted"] <= CLOUD_MAX)
            & (panel_df["yoy_per_pass"].notna())
        ]
        if len(hist) < 40:
            q_cache[dt] = None
            return None
        q_cache[dt] = float(np.quantile(hist["yoy_per_pass"].values, 0.80))
        return q_cache[dt]

    # Pre-calculate rolling 60-day betas vs XME
    rets = px_matrix.pct_change()
    xme_ret = rets["XME"]
    beta_map: dict[tuple[pd.Timestamp, str], float] = {}
    for t_idx, dt in enumerate(all_dates):
        if t_idx < 30:
            for tkr in STEEL_TICKERS:
                beta_map[(dt, tkr)] = 1.0
            continue
        hist_dts = all_dates[max(0, t_idx - 59) : t_idx + 1]
        xme_h = xme_ret.loc[hist_dts]
        var_xme = float(np.var(xme_h.dropna()))
        for tkr in STEEL_TICKERS:
            stk_h = rets.loc[hist_dts, tkr]
            if len(stk_h.dropna()) >= 20 and var_xme > 1e-6:
                cov = float(np.cov(stk_h.dropna(), xme_h.dropna())[0, 1])
                beta_map[(dt, tkr)] = float(np.clip(cov / var_xme, 0.2, 3.0))
            else:
                beta_map[(dt, tkr)] = 1.0

    # Simulate single position per ticker
    # Track open positions: ticker -> dict(entry_dt, exit_dt, entry_px, xme_entry_px, shares, xme_shares, notional, weight)
    open_positions: dict[str, dict] = {}
    trade_records: list[dict] = []
    q5_trigger_counts: dict[str, int] = {t: 0 for t in STEEL_TICKERS}

    daily_pnl: list[float] = []
    daily_gross_exposure: list[float] = []

    for t_idx, dt in enumerate(all_dates):
        cur_day_pnl = 0.0

        # 1. MTM on existing open positions
        closed_this_day = []
        for tkr, pos in list(open_positions.items()):
            cur_px = px_matrix.loc[dt, tkr]
            cur_xme = xme_series.loc[dt]
            prev_dt = all_dates[t_idx - 1]
            prev_px = px_matrix.loc[prev_dt, tkr]
            prev_xme = xme_series.loc[prev_dt]

            # Long stock + Short XME
            stk_chg = pos["shares"] * (cur_px - prev_px)
            xme_chg = pos["xme_shares"] * (cur_xme - prev_xme)  # xme_shares is negative
            day_mtm = stk_chg + xme_chg
            cur_day_pnl += day_mtm

            if dt >= pos["exit_dt"]:
                # Exit today: pay exit friction
                exit_cost = pos["shares"] * cur_px * (COST_RT / 2.0)
                cur_day_pnl -= exit_cost
                closed_this_day.append(tkr)

                total_gross_pnl = pos["shares"] * (cur_px - pos["entry_px"]) + pos["xme_shares"] * (cur_xme - pos["xme_entry_px"])
                total_cost = pos["notional"] * COST_RT
                net_pnl = total_gross_pnl - total_cost
                net_bps = (net_pnl / pos["notional"]) * 10_000.0

                trade_records.append(
                    {
                        "ticker": tkr,
                        "sig_dt": pos["sig_dt"],
                        "entry_dt": pos["entry_dt"],
                        "exit_dt": dt,
                        "notional": pos["notional"],
                        "shares": pos["shares"],
                        "entry_px": pos["entry_px"],
                        "exit_px": cur_px,
                        "xme_entry_px": pos["xme_entry_px"],
                        "xme_exit_px": cur_xme,
                        "beta": pos["beta"],
                        "gross_pnl": total_gross_pnl,
                        "cost": total_cost,
                        "net_pnl": net_pnl,
                        "net_bps": net_bps,
                        "win": net_pnl > 0,
                    }
                )

        for tkr in closed_this_day:
            del open_positions[tkr]

        # 2. T+1 Entries from signal at t_idx - 1
        sig_idx = t_idx - 1
        if sig_idx >= 0:
            sig_dt = all_dates[sig_idx]
            q80 = get_q80(sig_dt)
            if q80 is not None:
                day_sig = panel_df[panel_df["trading_date"] == sig_dt]
                for _, r in day_sig.iterrows():
                    tkr = str(r["ticker"])
                    if tkr not in STEEL_TICKERS:
                        continue
                    is_clear = r["cloud_cover_pct_weighted"] <= CLOUD_MAX
                    yoy = r["yoy_per_pass"]
                    if is_clear and yoy >= q80:
                        q5_trigger_counts[tkr] += 1
                        # If ticker not already open, enter at close today
                        if tkr not in open_positions:
                            open_wt = sum(p["notional"] for p in open_positions.values()) / INITIAL_EQUITY
                            avail_wt = max(0.0, MAX_GROSS_WT - open_wt)
                            wt = min(MAX_SINGLE_WT, avail_wt)
                            if wt > 0:
                                entry_px = px_matrix.loc[dt, tkr]
                                xme_entry_px = xme_series.loc[dt]
                                notional = INITIAL_EQUITY * wt
                                shares = int(notional // entry_px) if entry_px > 0 else 0
                                if shares > 0:
                                    beta = beta_map.get((sig_dt, tkr), 1.0)
                                    xme_shares = -int(round((shares * entry_px * beta) / xme_entry_px))
                                    exit_idx = min(t_idx + HOLDING_DAYS, n_days - 1)
                                    exit_dt = all_dates[exit_idx]
                                    # Entry cost
                                    entry_cost = shares * entry_px * (COST_RT / 2.0)
                                    cur_day_pnl -= entry_cost

                                    open_positions[tkr] = {
                                        "sig_dt": sig_dt,
                                        "entry_dt": dt,
                                        "exit_dt": exit_dt,
                                        "shares": shares,
                                        "xme_shares": xme_shares,
                                        "entry_px": entry_px,
                                        "xme_entry_px": xme_entry_px,
                                        "notional": shares * entry_px,
                                        "beta": beta,
                                        "weight": wt,
                                    }

        daily_pnl.append(cur_day_pnl)
        current_gross = sum(p["notional"] for p in open_positions.values()) / INITIAL_EQUITY
        daily_gross_exposure.append(current_gross)

    pnl_s = pd.Series(daily_pnl, index=all_dates)
    daily_rets = pnl_s / INITIAL_EQUITY
    sharpe = float(np.sqrt(252) * daily_rets.mean() / max(1e-6, daily_rets.std()))
    cum_net_ret = float(daily_rets.sum() * 100.0)

    # Max Drawdown
    cum_wealth = 1.0 + daily_rets.cumsum()
    peak = cum_wealth.cummax()
    dd = (cum_wealth - peak) / peak
    max_dd = float(abs(dd.min()) * 100.0)

    trades_df = pd.DataFrame(trade_records)

    summary = {
        "sharpe": sharpe,
        "cum_net_ret_pct": cum_net_ret,
        "max_dd_pct": max_dd,
        "avg_gross_pct": float(np.mean(daily_gross_exposure) * 100.0),
        "total_episodes": len(trades_df),
        "total_q5_triggers": sum(q5_trigger_counts.values()),
        "q5_triggers_by_ticker": q5_trigger_counts,
        "daily_pnl_series": pnl_s,
    }
    return summary, trades_df


def main():
    print("=" * 100)
    print("CONTROLLED 2x2 EVALUATION: US STEEL SLEEVE (CLF, NUE, STLD, CMC vs XME)")
    print("=" * 100)

    bronze_df = pd.read_parquet(BRONZE_IND_PATH)
    px_matrix = pd.read_parquet(PX_PATH)
    xme_series = px_matrix["XME"]

    # Calendar from 2018-04-01 to 2026-11-23
    all_dates = sorted(
        d for d in px_matrix.index if pd.Timestamp("2018-04-01") <= d <= pd.Timestamp("2026-11-23")
    )
    px_matrix = px_matrix.loc[all_dates]
    xme_series = xme_series.loc[all_dates]

    orig_gdf = gpd.read_file(AUDIT_DIR / "facilities_steel_original.geojson")
    ver_gdf = gpd.read_file(AUDIT_DIR / "facilities_steel_verified.geojson")

    configurations = [
        ("Orig_Coord__All_Pass", orig_gdf, "all_passes"),
        ("Orig_Coord__Night_Pass", orig_gdf, "night_only"),
        ("Verif_Coord__All_Pass", ver_gdf, "all_passes"),
        ("Verif_Coord__Night_Pass", ver_gdf, "night_only"),
    ]

    results: dict[str, dict] = {}
    all_trades: dict[str, pd.DataFrame] = {}

    for name, fac_gdf, denom_mode in configurations:
        print(f"\nEvaluating Variant: {name}...")
        panel, tot_matched, tkr_matched = build_steel_features(bronze_df, fac_gdf, denom_mode)
        summary, trades_df = run_steel_backtest(panel, px_matrix, xme_series, all_dates)
        summary["total_matched"] = tot_matched
        summary["ticker_matched"] = tkr_matched
        results[name] = summary
        all_trades[name] = trades_df

    # Print 2x2 Comparison Tables
    print("\n" + "=" * 110)
    print("TABLE 2A: 2x2 SUMMARY COMPARISON (PORTFOLIO-LEVEL METRICS)")
    print("=" * 110)
    hdr = f"{'Variant':26s} | {'Matched Dets':12s} | {'Q5 Triggers':11s} | {'Episodes':8s} | {'Sharpe':7s} | {'Cum Ret (%)':11s} | {'Max DD (%)':10s} | {'Avg Gross':9s}"
    print(hdr)
    print("-" * len(hdr))
    for name in configurations:
        cfg_name = name[0]
        s = results[cfg_name]
        print(
            f"{cfg_name:26s} | {s['total_matched']:12,d} | {s['total_q5_triggers']:11d} | "
            f"{s['total_episodes']:8d} | {s['sharpe']:+7.2f} | {s['cum_net_ret_pct']:+11.2f}% | "
            f"{s['max_dd_pct']:10.2f}% | {s['avg_gross_pct']:8.1f}%"
        )

    print("\n" + "=" * 110)
    print("TABLE 2B: TICKER-BY-TICKER BREAKDOWN ACROSS ALL 4 VARIANTS")
    print("=" * 110)
    t_hdr = f"{'Variant':26s} | {'Ticker':6s} | {'Matched':7s} | {'Triggers':8s} | {'Episodes':8s} | {'Win Rate':8s} | {'Mean BPS':9s} | {'Total BPS':10s}"
    print(t_hdr)
    print("-" * len(t_hdr))

    for name in configurations:
        cfg_name = name[0]
        s = results[cfg_name]
        tdf = all_trades[cfg_name]
        for tkr in STEEL_TICKERS:
            matched_cnt = s["ticker_matched"].get(tkr, 0)
            trig_cnt = s["q5_triggers_by_ticker"].get(tkr, 0)
            sub = tdf[tdf["ticker"] == tkr]
            episodes = len(sub)
            if episodes > 0:
                wr = (sub["win"]).mean() * 100.0
                mean_bps = sub["net_bps"].mean()
                tot_bps = sub["net_bps"].sum()
            else:
                wr, mean_bps, tot_bps = 0.0, 0.0, 0.0
            print(
                f"{cfg_name:26s} | {tkr:6s} | {matched_cnt:7,d} | {trig_cnt:8d} | {episodes:8d} | "
                f"{wr:7.1f}% | {mean_bps:+9.1f} | {tot_bps:+10.1f}"
            )
        print("-" * len(t_hdr))

    # Save summary dictionary to JSON for report integration
    serializable = {}
    for k, v in results.items():
        serializable[k] = {
            "sharpe": v["sharpe"],
            "cum_net_ret_pct": v["cum_net_ret_pct"],
            "max_dd_pct": v["max_dd_pct"],
            "avg_gross_pct": v["avg_gross_pct"],
            "total_episodes": v["total_episodes"],
            "total_q5_triggers": v["total_q5_triggers"],
            "total_matched": v["total_matched"],
            "ticker_matched": v["ticker_matched"],
            "q5_triggers_by_ticker": v["q5_triggers_by_ticker"],
        }
    with open(AUDIT_DIR / "steel_2x2_results.json", "w") as f:
        json.dump(serializable, f, indent=2)

    # Save trades per variant
    for k, df in all_trades.items():
        df.to_parquet(AUDIT_DIR / f"trades_steel_{k}.parquet", index=False)

    print("\nSaved steel 2x2 audit artifacts to audit_evidence_pack/")


if __name__ == "__main__":
    main()
