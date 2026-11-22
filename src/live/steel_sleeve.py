"""
Modular Steel Long Q5 OSINT Sleeve (CLF, NUE, STLD, CMC vs. XME).
Operates alongside the Refiner Short Q5 engine without modifying refiner state.
Persists to data/live/steel_ledger.parquet and data/live/steel_ledger.csv.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import requests
import yfinance as yf
from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderSide

from src.eval.audit_industrial import (
    BETA_WINDOW,
    CLOUD_IND_PATH,
    CLOUD_MAX,
    FACILITIES_PATH,
    HOLDING_DAYS,
    build_industrial_features,
)
from src.ingest.firms_live import poll_live_firms
from src.live.alpaca_broker import AlpacaPaperBroker

STEEL_TICKERS = ("CLF", "NUE", "STLD", "CMC")
STEEL_SECTOR = "STEEL"
STEEL_FIRMS_DAY_RANGE = 3

STEEL_LEDGER_PARQUET = Path("data/live/steel_ledger.parquet")
STEEL_LEDGER_CSV = Path("data/live/steel_ledger.csv")
BRONZE_IND_PATH = Path("data/industrial/bronze_industrial.parquet")
XME_CACHE_PATH = Path("data/silver/xme_close.parquet")

COST_RT = 0.0025            # 25 bps round-trip execution & borrow cost
MAX_SINGLE_WT = 0.10        # 10% max stock notional per name
MAX_GROSS_WT = 0.40         # 40% max gross long steel sleeve cap
MOM_WINDOW = 20             # 20-day XME momentum window

STEEL_LEDGER_COLS = [
    "trade_id", "status", "entry_date", "exit_date", "holding_days_elapsed",
    "ticker", "yoy_per_pass", "q80_threshold", "cloud_pct", "weight_pct",
    "long_shares", "entry_px", "beta_60d", "xme_short_shares", "xme_entry_px",
    "last_px", "xme_last_px", "mtm_pnl_usd", "realized_pnl_usd", "notes",
]


def load_steel_ledger() -> pd.DataFrame:
    if STEEL_LEDGER_PARQUET.exists():
        return pd.read_parquet(STEEL_LEDGER_PARQUET)
    return pd.DataFrame(columns=STEEL_LEDGER_COLS)


def save_steel_ledger(ledger: pd.DataFrame) -> None:
    STEEL_LEDGER_PARQUET.parent.mkdir(parents=True, exist_ok=True)
    ledger.to_parquet(STEEL_LEDGER_PARQUET, index=False)
    ledger.to_csv(STEEL_LEDGER_CSV, index=False)


def load_xme_series(refresh: bool = True) -> pd.Series:
    if refresh or not XME_CACHE_PATH.exists():
        raw = yf.download("XME", start="2018-01-01", auto_adjust=False, progress=False)
        if isinstance(raw.columns, pd.MultiIndex):
            col = "Adj Close" if "Adj Close" in raw.columns.get_level_values(0) else "Close"
            s = raw[col].iloc[:, 0]
        else:
            col = "Adj Close" if "Adj Close" in raw.columns else "Close"
            s = raw[col]
        df_xme = pd.DataFrame(
            {"trading_date": pd.to_datetime(s.index).tz_localize(None).normalize(), "xme_close": s.values}
        ).dropna()
        XME_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        df_xme.to_parquet(XME_CACHE_PATH, index=False)
    else:
        df_xme = pd.read_parquet(XME_CACHE_PATH)
    return df_xme.set_index("trading_date")["xme_close"].sort_index()


def build_steel_panel(fac_gdf: gpd.GeoDataFrame) -> tuple[pd.DataFrame, pd.Series]:
    steel_gdf = fac_gdf[fac_gdf["sector"] == "STEEL"].copy()
    bronze = pd.read_parquet(BRONZE_IND_PATH)
    gold = build_industrial_features(bronze, steel_gdf)

    tickers = sorted(steel_gdf["ticker"].unique().tolist())
    raw = yf.download(tickers + ["XME"], start="2018-01-01", auto_adjust=False, progress=False)
    lvl0 = raw.columns.get_level_values(0)
    field = "Adj Close" if "Adj Close" in lvl0 else "Close"
    px = raw[field].copy()
    px.index = pd.to_datetime(px.index).tz_localize(None).normalize()
    px = px.sort_index()
    rets = px.pct_change()

    xme_px = px["XME"]
    xme_mom20 = xme_px / xme_px.shift(MOM_WINDOW) - 1.0

    mkt_rows = []
    for tkr in tickers:
        if tkr not in px.columns:
            continue
        beta = rets[tkr].rolling(BETA_WINDOW, min_periods=30).cov(rets["XME"]) / rets["XME"].rolling(
            BETA_WINDOW, min_periods=30
        ).var().replace(0.0, np.nan)
        mkt_rows.append(
            pd.DataFrame(
                {
                    "trading_date": px.index,
                    "ticker": tkr,
                    "adj_close": px[tkr].values,
                    "beta_xme_60d": beta.values,
                    "xme_mom20": xme_mom20.values,
                }
            )
        )
    mkt_df = pd.concat(mkt_rows, ignore_index=True).dropna(subset=["adj_close"])
    df = gold.merge(mkt_df, on=["ticker", "trading_date"], how="inner")
    return df.sort_values(["trading_date", "ticker"]).reset_index(drop=True), xme_px


def run_steel_sleeve(
    broker: AlpacaPaperBroker | None,
    base_equity: float = 25_000.0,
    skip_ingest: bool = False,
    dry_run: bool = False,
    feed_ok: bool = True,
) -> dict:
    order_errors: list[str] = []
    if not skip_ingest:
        print(
            f"[steel] Polling NASA FIRMS NRT for {STEEL_SECTOR} facilities "
            f"({', '.join(STEEL_TICKERS)}, last {STEEL_FIRMS_DAY_RANGE} days)..."
        )
        try:
            n_rows, feed_ok = poll_live_firms(
                day_range=STEEL_FIRMS_DAY_RANGE,
                facilities_path=FACILITIES_PATH,
                bronze_path=BRONZE_IND_PATH,
                sector=STEEL_SECTOR,
                log_prefix="steel",
            )
            print(
                f"[steel] FIRMS upsert complete (rows_in_bronze={n_rows:,}, feed_ok={feed_ok})."
            )
        except Exception as exc:
            feed_ok = False
            print(f"[steel][error] FEED_ERROR: NASA FIRMS steel poll failed: {exc}")
        if not feed_ok:
            err_msg = (
                "FEED_ERROR: NASA FIRMS steel polling incomplete/timeout; "
                "suppressing new entries."
            )
            order_errors.append(err_msg)
            print(f"[steel][error] {err_msg}")

    fac_gdf = gpd.read_file(FACILITIES_PATH)
    df, xme_px_map = build_steel_panel(fac_gdf)

    all_dates = sorted(df["trading_date"].unique())
    as_of_dt = all_dates[-1]
    as_of_str = str(pd.Timestamp(as_of_dt).date())
    embargo_dt = all_dates[-1 - HOLDING_DAYS]

    today_df = df[df["trading_date"] == as_of_dt].copy()
    xme_now = float(xme_px_map.loc[:as_of_dt].iloc[-1])
    xme_mom20_today = float(today_df["xme_mom20"].iloc[0]) if pd.notna(today_df["xme_mom20"].iloc[0]) else 0.0
    mom_ok = xme_mom20_today > 0.0

    ledger = load_steel_ledger()
    closed_today: list[str] = []

    # Check if market is open before routing live orders
    market_open = broker.is_market_open() if broker is not None else False

    # 1. Mark-to-market & auto-close matured OPEN positions
    if not ledger.empty:
        px_today = today_df.set_index("ticker")["adj_close"].to_dict()
        for idx, tr in ledger[ledger["status"] == "OPEN"].iterrows():
            tkr = str(tr["ticker"])
            cur_px = float(px_today.get(tkr, tr["last_px"]))
            entry_dt = pd.Timestamp(tr["entry_date"])
            elapsed = sum(1 for d in all_dates if entry_dt < d <= as_of_dt)

            long_pnl = tr["long_shares"] * (cur_px - tr["entry_px"])
            # xme_short_shares is negative: (-qty) * (cur - entry)
            hedge_pnl = tr["xme_short_shares"] * (xme_now - tr["xme_entry_px"])
            gross_pnl = long_pnl + hedge_pnl
            est_cost = tr["long_shares"] * tr["entry_px"] * COST_RT

            ledger.at[idx, "holding_days_elapsed"] = elapsed
            ledger.at[idx, "last_px"] = round(cur_px, 2)
            ledger.at[idx, "xme_last_px"] = round(xme_now, 2)
            ledger.at[idx, "mtm_pnl_usd"] = round(gross_pnl - est_cost, 2)

            if elapsed >= HOLDING_DAYS:
                close_note = ""
                if broker is not None and not dry_run and market_open:
                    try:
                        c_msgs = broker.close_long_pair(
                            trade_id=str(tr["trade_id"]),
                            ticker=tkr,
                            long_shares=int(tr["long_shares"]),
                        )
                        close_note = f" [{' | '.join(c_msgs)}]"
                    except APIError as exc:
                        err_msg = f"Steel close failed for {tkr}: {exc}"
                        order_errors.append(err_msg)
                        print(f"[steel][error] {err_msg}")
                        continue
                elif broker is not None and not dry_run and not market_open:
                    close_note = " [MARKET_CLOSED: defer close]"
                    continue

                ledger.at[idx, "status"] = "CLOSED"
                ledger.at[idx, "exit_date"] = as_of_str
                ledger.at[idx, "realized_pnl_usd"] = round(gross_pnl - est_cost, 2)
                closed_today.append(f"{tkr} (${gross_pnl - est_cost:+.2f}){close_note}")

    realized_total = float(ledger.loc[ledger["status"] == "CLOSED", "realized_pnl_usd"].sum()) if not ledger.empty else 0.0
    unrealized_total = float(ledger.loc[ledger["status"] == "OPEN", "mtm_pnl_usd"].sum()) if not ledger.empty else 0.0
    # Live broker equity already reflects account P&L; do not double-count
    current_equity = base_equity if broker is not None else (base_equity + realized_total + unrealized_total)

    # 2. Compute embargoed Q80 threshold
    hist = df[
        (df["trading_date"] <= embargo_dt)
        & (df["cloud_cover_pct_weighted"] <= CLOUD_MAX)
        & (df["yoy_per_pass"].notna())
    ]
    q80 = float(np.quantile(hist["yoy_per_pass"].values, 0.80)) if not hist.empty else 1.20

    # 3. Evaluate today's candidates
    open_tickers = set(ledger.loc[ledger["status"] == "OPEN", "ticker"]) if not ledger.empty else set()
    existing_keys = set(zip(ledger["entry_date"].astype(str), ledger["ticker"])) if not ledger.empty else set()
    open_gross_wt = (
        float((ledger.loc[ledger["status"] == "OPEN", "long_shares"] * ledger.loc[ledger["status"] == "OPEN", "last_px"]).sum())
        / max(current_equity, 1.0)
        if not ledger.empty
        else 0.0
    )

    new_entries: list[dict] = []
    cands = (
        today_df[
            (today_df["cloud_cover_pct_weighted"] <= CLOUD_MAX)
            & (today_df["yoy_per_pass"] >= q80)
        ]
        if feed_ok
        else today_df.iloc[0:0]
    )
    for _, row in cands.iterrows():
        tkr = str(row["ticker"])
        if tkr in open_tickers or (as_of_str, tkr) in existing_keys:
            continue

        avail_wt = max(0.0, MAX_GROSS_WT - open_gross_wt)
        wt = min(MAX_SINGLE_WT, avail_wt)
        if wt <= 0.0:
            continue

        px = float(row["adj_close"])
        beta = float(row["beta_xme_60d"]) if pd.notna(row["beta_xme_60d"]) else 1.0
        long_shares = int((current_equity * wt) // px) if px > 0 else 0
        if long_shares <= 0:
            continue

        stock_notional = long_shares * px
        xme_shares = int(round((stock_notional * beta) / xme_now))
        open_gross_wt += stock_notional / current_equity

        new_entries.append(
            {
                "trade_id": f"STEEL_{as_of_str}_{tkr}",
                "status": "OPEN",
                "entry_date": as_of_str,
                "exit_date": "",
                "holding_days_elapsed": 0,
                "ticker": tkr,
                "yoy_per_pass": round(float(row["yoy_per_pass"]), 3),
                "q80_threshold": round(q80, 3),
                "cloud_pct": round(float(row["cloud_cover_pct_weighted"]), 1),
                "weight_pct": round((stock_notional / current_equity) * 100, 2),
                "long_shares": long_shares,
                "entry_px": round(px, 2),
                "beta_60d": round(beta, 3),
                "xme_short_shares": -xme_shares,  # Negative quantity for short hedge
                "xme_entry_px": round(xme_now, 2),
                "last_px": round(px, 2),
                "xme_last_px": round(xme_now, 2),
                "mtm_pnl_usd": round(-stock_notional * COST_RT, 2),
                "realized_pnl_usd": 0.0,
                "notes": f"Long Q5 Steel / Short beta*XME (unfiltered, xme_mom20={xme_mom20_today:+.2%})",
            }
        )

    accepted_entries: list[dict] = []
    if new_entries:
        if broker is not None and not dry_run and market_open:
            for item in new_entries:
                tkr = item["ticker"]
                try:
                    asset = broker.client.get_asset(tkr)
                    if not asset.tradable:
                        raise APIError(f"Steel asset {tkr} not tradable")

                    o_msgs = broker.open_long_pair(
                        trade_id=item["trade_id"],
                        ticker=tkr,
                        long_shares=int(item["long_shares"]),
                    )
                    item["notes"] = f"{item['notes']} | {' ; '.join(o_msgs)}"
                    accepted_entries.append(item)
                except APIError as exc:
                    err_msg = f"Steel entry failed for {tkr}: {exc}"
                    order_errors.append(err_msg)
                    print(f"[steel][error] {err_msg}")
        elif broker is not None and not dry_run and not market_open:
            print("[steel] Market closed; skipping live entry orders.")
        else:
            accepted_entries = new_entries

        if accepted_entries:
            ledger = pd.concat([ledger, pd.DataFrame(accepted_entries)], ignore_index=True)

    xme_rebal_status = "SKIP"
    open_df = ledger[ledger["status"] == "OPEN"] if not ledger.empty else pd.DataFrame()
    target_xme = int(open_df["xme_short_shares"].fillna(0).sum()) if not open_df.empty else 0

    if broker is not None and not dry_run and market_open and (closed_today or accepted_entries):
        try:
            xme_rebal_status = broker.rebalance_hedge_etf("XME", target_xme, as_of_str, tag="STEEL_REBAL")
        except APIError as exc:
            xme_rebal_status = f"xme-rebal-err:{exc}"
            order_errors.append(f"XME rebalance failed: {exc}")
            print(f"[steel][error] XME rebalance failed: {exc}")

    if not dry_run:
        save_steel_ledger(ledger)

    # 4. Print Sleeve Blotter
    lines = [
        f"\n=== STEEL LONG-Q5 SLEEVE BLOTTER | AS OF {as_of_str} ===",
        f"Signal : Steel signal_as_of_date = {as_of_str} | Feed: {'OK' if feed_ok else 'FEED_ERROR (New entries suppressed)'}",
        f"Gate   : UNFILTERED (Flat Equal-Weight) | XME 20d Mom = {xme_mom20_today:+.2%} | Market: {'OPEN' if market_open else 'CLOSED'}",
        f"Model  : Q80 = {q80:.3f} | XME Price = ${xme_now:.2f} | Target XME Short = {target_xme} shares ({xme_rebal_status})",
        f"Sleeve : Realized = ${realized_total:+,.2f} | Open MTM = ${unrealized_total:+,.2f} | Gross Wt = {open_gross_wt:.1%}",
        f"Closed : {', '.join(closed_today) if closed_today else 'None'}",
        f"New    : {len(accepted_entries)} entries",
    ]
    if accepted_entries:
        lines.append(
            pd.DataFrame(accepted_entries)[
                ["ticker", "yoy_per_pass", "long_shares", "entry_px", "xme_short_shares", "weight_pct"]
            ].to_string(index=False)
        )
    if not open_df.empty:
        lines.append(f"\nOpen Steel Positions ({len(open_df)}):")
        lines.append(
            open_df[["entry_date", "ticker", "holding_days_elapsed", "long_shares", "xme_short_shares", "mtm_pnl_usd"]].to_string(index=False)
        )
    print("\n".join(lines))

    return {
        "as_of_str": as_of_str,
        "mom_ok": mom_ok,
        "xme_mom20": xme_mom20_today,
        "accepted_entries": accepted_entries,
        "closed_today": closed_today,
        "target_xme_short": target_xme,
        "order_errors": order_errors,
    }
