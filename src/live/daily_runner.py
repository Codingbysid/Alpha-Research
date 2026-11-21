"""
Live daily OSINT signal runner & beta-hedged paper-trading ledger.
Runs full ingest -> feature -> regime -> CVaR sizing -> paper ledger MTM/execution.

Usage:
    PYTHONPATH=. python3 -W ignore src/live/daily_runner.py --equity 25000
    PYTHONPATH=. python3 -W ignore src/live/daily_runner.py --skip-ingest
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import yfinance as yf
from dotenv import load_dotenv

from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderSide
from src.live.alpaca_broker import AlpacaPaperBroker
from src.ingest.energy_macro import CRACK_PATH, build_crack_spread
from src.ingest.firms_live import poll_live_firms
from src.ingest.market_data import build_market_panel
from src.risk.regime import build_regimes
from src.risk.sizing import calibrate_merton_params, simulate_merton_unit_returns
from src.transform.feature_engine import build_features

CRACK_Z60_MAX = 1.0

SILVER_PATH = Path("data/silver/facility_daily.parquet")
GOLD_PATH = Path("data/gold/refinery_features.parquet")
MARKET_PATH = Path("data/silver/market_returns.parquet")
REGIME_PATH = Path("data/silver/market_regimes.parquet")
XLE_CACHE_PATH = Path("data/silver/xle_close.parquet")
LEDGER_PARQUET = Path("data/live/paper_ledger.parquet")
LEDGER_CSV = Path("data/live/paper_ledger.csv")

HOLDING_DAYS = 10
CLOUD_MAX = 40.0
COST_RT = 0.0025
MAX_CVAR_PCT = 0.015       # 1.5% equity 95% CVaR cap per trade
MAX_SINGLE_WT = 0.10       # 10% max stock notional per name
MAX_GROSS_WT = 0.60        # 60% max gross short book across all open positions
PRIOR_ALPHA = 10.0

LEDGER_COLS = [
    "trade_id", "status", "entry_date", "exit_date", "holding_days_elapsed",
    "ticker", "yoy_per_pass", "q80_threshold", "cloud_pct", "p_win_est",
    "raw_kelly_est", "unit_cvar_95", "weight_pct", "short_shares", "entry_px",
    "beta_60d", "xle_long_shares", "xle_entry_px", "last_px", "xle_last_px",
    "mtm_pnl_usd", "realized_pnl_usd", "notes",
]


def compute_regime_mult(regime_label: str, p_panic: float, ovx: float) -> float:
    if regime_label != "IDIOSYNCRATIC":
        return 0.0
    if pd.notna(ovx) and ovx < 35.0:
        return 1.0
    if pd.notna(ovx) and p_panic >= 0.50:
        return float(np.clip(1.0 - 0.5 * ((ovx - 35.0) / 10.0), 0.5, 1.0))
    return 1.0


def load_xle_series(refresh: bool = True) -> pd.Series:
    if refresh or not XLE_CACHE_PATH.exists():
        raw = yf.download("XLE", start="2018-01-01", auto_adjust=False, progress=False)
        if isinstance(raw.columns, pd.MultiIndex):
            col = "Adj Close" if "Adj Close" in raw.columns.get_level_values(0) else "Close"
            s = raw[col].iloc[:, 0]
        else:
            col = "Adj Close" if "Adj Close" in raw.columns else "Close"
            s = raw[col]
        df_xle = pd.DataFrame(
            {"trading_date": pd.to_datetime(s.index).tz_localize(None).normalize(), "xle_close": s.values}
        ).dropna()
        XLE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        df_xle.to_parquet(XLE_CACHE_PATH, index=False)
    else:
        df_xle = pd.read_parquet(XLE_CACHE_PATH)
    return df_xle.set_index("trading_date")["xle_close"].sort_index()


def build_live_panel() -> pd.DataFrame:
    fac = pd.read_parquet(SILVER_PATH).copy()
    fac["frp_per_pass"] = np.where(
        fac["satellite_pass_count"] > 0, fac["frp_night_mw"] / fac["satellite_pass_count"], 0.0
    )
    cap = fac[["facility_id", "ticker", "capacity_bpd"]].drop_duplicates()
    cap["w"] = cap["capacity_bpd"] / cap.groupby("ticker")["capacity_bpd"].transform("sum")
    fac = fac.merge(cap[["facility_id", "w"]], on="facility_id", how="left")
    fac["w_frp_pp"] = fac["frp_per_pass"] * fac["w"]

    pp = (
        fac.groupby(["ticker", "trading_date"], as_index=False)["w_frp_pp"]
        .sum()
        .sort_values(["ticker", "trading_date"])
    )

    df = (
        pd.read_parquet(GOLD_PATH)
        .merge(pp, on=["ticker", "trading_date"], how="inner")
        .merge(pd.read_parquet(MARKET_PATH), on=["ticker", "trading_date"], how="inner")
        .merge(
            pd.read_parquet(REGIME_PATH)[["trading_date", "p_macro_panic", "regime_label", "is_tradeable"]],
            on="trading_date",
            how="left",
        )
    )

    frames = []
    for _, g in df.groupby("ticker"):
        g = g.sort_values("trading_date").copy()
        clean = g["w_frp_pp"].where(g["cloud_cover_pct_weighted"] <= 50.0)
        g["yoy_per_pass"] = clean.rolling(5, min_periods=2).mean() / clean.rolling(
            21, min_periods=7
        ).mean().shift(252).replace(0.0, np.nan)
        frames.append(g)
    df = pd.concat(frames, ignore_index=True)

    crack = pd.read_parquet(CRACK_PATH)[["trading_date", "crack_321", "crack_z60"]].sort_values("trading_date")
    df = pd.merge_asof(
        df.sort_values("trading_date"),
        crack,
        on="trading_date",
        direction="backward",
    )
    return df.sort_values(["trading_date", "ticker"]).reset_index(drop=True)


def load_ledger() -> pd.DataFrame:
    if LEDGER_PARQUET.exists():
        return pd.read_parquet(LEDGER_PARQUET)
    return pd.DataFrame(columns=LEDGER_COLS)


def save_ledger(ledger: pd.DataFrame) -> None:
    LEDGER_PARQUET.parent.mkdir(parents=True, exist_ok=True)
    ledger.to_parquet(LEDGER_PARQUET, index=False)
    ledger.to_csv(LEDGER_CSV, index=False)


def send_webhook(text: str) -> None:
    url = os.getenv("ALERT_WEBHOOK_URL", "").strip()
    if not url:
        return
    try:
        requests.post(url, json={"content": text, "text": text}, timeout=10)
    except Exception as exc:
        print(f"[alert][warn] Webhook failed: {exc}")


def _rebalance_xle_hedge(
    broker: AlpacaPaperBroker | None,
    ledger: pd.DataFrame,
    as_of_str: str,
    tag: str,
) -> str:
    """Rebalances Alpaca XLE long position to match total open ledger xle_long_shares."""
    if broker is None or ledger.empty:
        return "ledger-only"

    open_df = ledger[ledger["status"] == "OPEN"]
    target_xle = int(open_df["xle_long_shares"].fillna(0).sum()) if not open_df.empty else 0

    try:
        positions = broker.client.get_all_positions()
        cur_xle = int(
            next((float(p.qty) for p in positions if str(p.symbol).upper() == "XLE"), 0)
        )
    except Exception as exc:
        return f"xle-query-err:{exc}"

    delta = target_xle - cur_xle
    if delta == 0:
        return f"xle-flat:{cur_xle}"

    cid = f"{as_of_str}_{tag}_XLE_{target_xle}"
    if delta > 0:
        res = broker._submit_qty("XLE", delta, OrderSide.BUY, cid)
    else:
        res = broker._submit_qty("XLE", abs(delta), OrderSide.SELL, cid)
    return f"xle:{cur_xle}->{target_xle}({res})"


def _reconcile_positions(
    broker: AlpacaPaperBroker | None,
    ledger: pd.DataFrame,
) -> tuple[bool, list[str]]:
    """Compares OPEN ledger shares (signed) against live Alpaca positions."""
    if broker is None:
        return True, ["LEDGER_ONLY (reconciliation skipped)"]

    expected: dict[str, int] = {}
    if not ledger.empty:
        open_df = ledger[ledger["status"] == "OPEN"]
        for _, row in open_df.iterrows():
            tkr = str(row["ticker"]).upper()
            expected[tkr] = expected.get(tkr, 0) - int(row["short_shares"])
        target_xle = int(open_df["xle_long_shares"].fillna(0).sum())
        if target_xle > 0:
            expected["XLE"] = target_xle

    try:
        alp_positions = broker.client.get_all_positions()
    except Exception as exc:
        return False, [f"SYNC_ERROR: {exc}"]

    live: dict[str, int] = {}
    for p in alp_positions:
        sym = str(p.symbol).upper()
        raw_qty = int(float(p.qty))
        is_short = (
            str(getattr(p, "side", "")).upper() in ("SHORT", "POSITIONSIDE.SHORT")
            or str(getattr(getattr(p, "side", None), "name", "")).upper() == "SHORT"
            or raw_qty < 0
        )
        live[sym] = -abs(raw_qty) if is_short else abs(raw_qty)

    all_symbols = sorted(set(expected.keys()) | set(live.keys()))
    if not all_symbols:
        return True, ["IN_SYNC (flat)"]

    drifts: list[str] = []
    for sym in all_symbols:
        exp_qty = expected.get(sym, 0)
        liv_qty = live.get(sym, 0)
        if exp_qty != liv_qty:
            drifts.append(f"{sym}: expected {exp_qty:+d}, live {liv_qty:+d} (drift {liv_qty - exp_qty:+d})")

    if drifts:
        return False, [f"DRIFT_DETECTED: {'; '.join(drifts)}"]
    return True, ["IN_SYNC (all positions match)"]


def run_daily(base_equity: float = 25_000.0, skip_ingest: bool = False, dry_run: bool = False) -> None:
    load_dotenv()

    broker = AlpacaPaperBroker.from_env()
    broker_status = "ALPACA_PAPER" if broker is not None else "LEDGER_ONLY"

    if broker is not None:
        try:
            acc = broker.client.get_account()
            base_equity = float(acc.equity)
            print(f"[broker] Connected to Alpaca Paper Trading. Account: {acc.account_number} | Live Equity: ${base_equity:,.2f}")
        except Exception as exc:
            print(f"[broker][warn] Could not fetch live equity: {exc}")

    if not skip_ingest:
        print("[live] Step 1/5: Polling NASA FIRMS NRT...")
        poll_live_firms(day_range=2)
        print("[live] Step 2/5: Rebuilding Gold features...")
        build_features()
        print("[live] Step 3/5: Updating market returns...")
        build_market_panel()
        print("[live] Step 4/5: Updating HMM regimes...")
        build_regimes()
        print("[live] Step 5/5: Updating 3-2-1 crack spread...")
        build_crack_spread()

    xle_px_map = load_xle_series(refresh=not skip_ingest)
    df = build_live_panel()

    all_dates = sorted(df["trading_date"].unique())
    as_of_dt = all_dates[-1]
    as_of_str = str(pd.Timestamp(as_of_dt).date())
    embargo_dt = all_dates[-1 - HOLDING_DAYS]

    today_df = df[df["trading_date"] == as_of_dt].copy()
    xle_now = float(xle_px_map.loc[:as_of_dt].iloc[-1])

    # 1. Mark-to-market & auto-close matured OPEN positions
    ledger = load_ledger()
    reconcile_ok, reconcile_msgs = _reconcile_positions(broker, ledger)
    order_errors: list[str] = []
    closed_today: list[str] = []

    if not ledger.empty:
        px_today = today_df.set_index("ticker")["adj_close"].to_dict()
        for idx, tr in ledger[ledger["status"] == "OPEN"].iterrows():
            tkr = tr["ticker"]
            cur_px = float(px_today.get(tkr, tr["last_px"]))
            entry_dt = pd.Timestamp(tr["entry_date"])
            elapsed = sum(1 for d in all_dates if entry_dt < d <= as_of_dt)

            short_pnl = tr["short_shares"] * (tr["entry_px"] - cur_px)
            hedge_pnl = tr["xle_long_shares"] * (xle_now - tr["xle_entry_px"])
            gross_pnl = short_pnl + hedge_pnl
            est_cost = tr["short_shares"] * tr["entry_px"] * COST_RT

            ledger.at[idx, "holding_days_elapsed"] = elapsed
            ledger.at[idx, "last_px"] = round(cur_px, 2)
            ledger.at[idx, "xle_last_px"] = round(xle_now, 2)
            ledger.at[idx, "mtm_pnl_usd"] = round(gross_pnl - est_cost, 2)

            if elapsed >= HOLDING_DAYS:
                close_note = ""
                if broker is not None and not dry_run:
                    try:
                        c_msgs = broker.close_short_pair(
                            trade_id=str(tr["trade_id"]),
                            ticker=tkr,
                            short_shares=int(tr["short_shares"]),
                            xle_release_shares=0,
                        )
                        close_note = f" [{' | '.join(c_msgs)}]"
                    except APIError as exc:
                        err_msg = f"Close failed for {tkr}: {exc}"
                        order_errors.append(err_msg)
                        print(f"[broker][error] {err_msg}")
                        continue
                ledger.at[idx, "status"] = "CLOSED"
                ledger.at[idx, "exit_date"] = as_of_str
                ledger.at[idx, "realized_pnl_usd"] = round(gross_pnl - est_cost, 2)
                closed_today.append(f"{tkr} (${gross_pnl - est_cost:+.2f}){close_note}")

    realized_total = float(ledger.loc[ledger["status"] == "CLOSED", "realized_pnl_usd"].sum()) if not ledger.empty else 0.0
    unrealized_total = float(ledger.loc[ledger["status"] == "OPEN", "mtm_pnl_usd"].sum()) if not ledger.empty else 0.0
    current_equity = base_equity + realized_total + unrealized_total

    # 2. Compute embargoed Q80 threshold & historical telemetry
    hist = df[
        (df["trading_date"] <= embargo_dt)
        & (df["cloud_cover_pct_weighted"] <= CLOUD_MAX)
        & (df["yoy_per_pass"].notna())
        & (df["fwd_ret_10d_excess"].notna())
    ]
    q80 = float(np.quantile(hist["yoy_per_pass"].values, 0.80))
    q5_rets = -hist.loc[hist["yoy_per_pass"] >= q80, "fwd_ret_10d_excess"] - COST_RT
    wins, losses = q5_rets[q5_rets > 0], q5_rets[q5_rets < 0]
    p_win_est = float((len(wins) + PRIOR_ALPHA) / (len(q5_rets) + 2.0 * PRIOR_ALPHA))
    b_est = float(wins.mean() / abs(losses.mean())) if (len(wins) >= 5 and len(losses) >= 5) else 1.0
    raw_kelly_est = p_win_est - (1.0 - p_win_est) / max(b_est, 1e-6)

    # 3. Evaluate today's regime & candidates
    reg_label = str(today_df["regime_label"].iloc[0])
    p_panic = float(today_df["p_macro_panic"].iloc[0]) if pd.notna(today_df["p_macro_panic"].iloc[0]) else 1.0
    ovx_val = float(today_df["ovx_level"].iloc[0]) if pd.notna(today_df["ovx_level"].iloc[0]) else np.nan
    reg_mult = compute_regime_mult(reg_label, p_panic, ovx_val)

    open_tickers = set(ledger.loc[ledger["status"] == "OPEN", "ticker"]) if not ledger.empty else set()
    existing_keys = set(zip(ledger["entry_date"].astype(str), ledger["ticker"])) if not ledger.empty else set()
    open_gross_wt = (
        float((ledger.loc[ledger["status"] == "OPEN", "short_shares"] * ledger.loc[ledger["status"] == "OPEN", "last_px"]).sum())
        / max(current_equity, 1.0)
        if not ledger.empty
        else 0.0
    )

    crack_321 = float(today_df["crack_321"].iloc[0]) if pd.notna(today_df["crack_321"].iloc[0]) else np.nan
    crack_z60 = float(today_df["crack_z60"].iloc[0]) if pd.notna(today_df["crack_z60"].iloc[0]) else np.nan
    crack_ok = pd.notna(crack_z60) and (crack_z60 <= CRACK_Z60_MAX)

    new_entries: list[dict] = []
    if reg_mult > 0 and crack_ok:
        cands = today_df[
            (today_df["cloud_cover_pct_weighted"] <= CLOUD_MAX)
            & (today_df["yoy_per_pass"] >= q80)
        ]
        for _, row in cands.iterrows():
            tkr = str(row["ticker"])
            if tkr in open_tickers or (as_of_str, tkr) in existing_keys:
                continue

            t_hist = df[(df["ticker"] == tkr) & (df["trading_date"] <= as_of_dt)]["ret_1d"]
            params = calibrate_merton_params(t_hist)
            unit_losses = -simulate_merton_unit_returns(params, direction=-1, holding_days=HOLDING_DAYS, n_sims=10_000)
            var_95 = float(np.quantile(unit_losses, 0.95))
            cvar_95 = float(unit_losses[unit_losses >= var_95].mean())

            cvar_wt = MAX_CVAR_PCT / max(cvar_95, 1e-6)
            wt = min(cvar_wt, MAX_SINGLE_WT) * reg_mult
            avail_wt = max(0.0, MAX_GROSS_WT - open_gross_wt)
            wt = min(wt, avail_wt)

            px = float(row["adj_close"])
            beta = float(row["beta_xle_60d"]) if pd.notna(row["beta_xle_60d"]) else 1.0
            short_shares = int((current_equity * wt) // px) if px > 0 else 0
            if short_shares <= 0:
                continue

            stock_notional = short_shares * px
            xle_shares = int(round((stock_notional * beta) / xle_now))
            open_gross_wt += stock_notional / current_equity

            new_entries.append(
                {
                    "trade_id": f"{as_of_str}_{tkr}",
                    "status": "OPEN",
                    "entry_date": as_of_str,
                    "exit_date": "",
                    "holding_days_elapsed": 0,
                    "ticker": tkr,
                    "yoy_per_pass": round(float(row["yoy_per_pass"]), 3),
                    "q80_threshold": round(q80, 3),
                    "cloud_pct": round(float(row["cloud_cover_pct_weighted"]), 1),
                    "p_win_est": round(p_win_est, 3),
                    "raw_kelly_est": round(raw_kelly_est, 3),
                    "unit_cvar_95": round(cvar_95, 4),
                    "weight_pct": round((stock_notional / current_equity) * 100, 2),
                    "short_shares": short_shares,
                    "entry_px": round(px, 2),
                    "beta_60d": round(beta, 3),
                    "xle_long_shares": xle_shares,
                    "xle_entry_px": round(xle_now, 2),
                    "last_px": round(px, 2),
                    "xle_last_px": round(xle_now, 2),
                    "mtm_pnl_usd": round(-stock_notional * COST_RT, 2),
                    "realized_pnl_usd": 0.0,
                    "notes": f"Short Q5 / Long beta*XLE (reg_mult={reg_mult:.2f}, crack_z60={crack_z60:+.2f})",
                }
            )

    accepted_entries: list[dict] = []
    if new_entries:
        if broker is not None and not dry_run:
            for item in new_entries:
                tkr = item["ticker"]
                try:
                    asset = broker.client.get_asset(tkr)
                    if not asset.tradable or not asset.shortable:
                        raise APIError(f"Asset {tkr} not tradable or not shortable (tradable={asset.tradable}, shortable={asset.shortable})")

                    o_msgs = broker.open_short_pair(
                        trade_id=item["trade_id"],
                        ticker=tkr,
                        short_shares=int(item["short_shares"]),
                        xle_delta_shares=0,
                    )
                    item["notes"] = f"{item['notes']} | {' ; '.join(o_msgs)}"
                    accepted_entries.append(item)
                except APIError as exc:
                    err_msg = f"Entry order failed for {tkr}: {exc}"
                    order_errors.append(err_msg)
                    print(f"[broker][error] {err_msg}")
        else:
            accepted_entries = new_entries

        if accepted_entries:
            ledger = pd.concat([ledger, pd.DataFrame(accepted_entries)], ignore_index=True)

    xle_rebal_status = "SKIP"
    if broker is not None and not dry_run and (closed_today or accepted_entries):
        try:
            xle_rebal_status = _rebalance_xle_hedge(broker, ledger, as_of_str, tag="REBAL")
        except APIError as exc:
            xle_rebal_status = f"xle-rebal-err:{exc}"
            order_errors.append(f"XLE rebalance failed: {exc}")
            print(f"[broker][error] XLE rebalance failed: {exc}")

    save_ledger(ledger)

    # 4. Print Desk Blotter
    lines = [
        f"=== OSINT SHORT-Q5 DAILY BLOTTER | AS OF {as_of_str} (run {datetime.now(timezone.utc).strftime('%H:%M UTC')}) ===",
        f"Broker : {broker_status} | XLE Hedge Rebal: {xle_rebal_status}",
        f"Sync   : {' | '.join(reconcile_msgs)}",
        f"Errors : {', '.join(order_errors) if order_errors else 'None'}",
        f"Regime : {reg_label} (p_panic={p_panic:.3f}, OVX={ovx_val:.2f}, reg_mult={reg_mult:.2f})",
        f"Crack  : ${crack_321:.2f}/bbl (z60={crack_z60:+.2f}, gate={'PASS' if crack_ok else 'BLOCK'})",
        f"Equity : ${current_equity:,.2f} (Realized: ${realized_total:+,.2f} | Open MTM: ${unrealized_total:+,.2f})",
        f"Model  : Q80={q80:.3f} | Est p_win={p_win_est:.3f} | b={b_est:.2f} | Raw Kelly={raw_kelly_est:+.3f}",
        "",
        "--- TODAY'S UNIVERSE SNAPSHOT ---",
        today_df[["ticker", "yoy_per_pass", "cloud_cover_pct_weighted", "adj_close", "beta_xle_60d"]]
        .round(3)
        .to_string(index=False),
        "",
        f"Closed Today : {', '.join(closed_today) if closed_today else 'None'}",
        f"New Entries  : {len(accepted_entries)}",
    ]
    if accepted_entries:
        lines.append(
            pd.DataFrame(accepted_entries)[
                ["ticker", "yoy_per_pass", "short_shares", "entry_px", "xle_long_shares", "weight_pct"]
            ].to_string(index=False)
        )

    open_df = ledger[ledger["status"] == "OPEN"] if not ledger.empty else pd.DataFrame()
    lines.append(f"\nOpen Positions ({len(open_df)}):")
    if not open_df.empty:
        lines.append(
            open_df[["entry_date", "ticker", "holding_days_elapsed", "short_shares", "xle_long_shares", "mtm_pnl_usd"]].to_string(index=False)
        )

    if broker is not None:
        try:
            acc = broker.client.get_account()
            lines.append("")
            lines.append("--- ALPACA BROKER INTEGRATION ---")
            lines.append(f"Account ID   : {acc.account_number} (Status: {acc.status})")
            lines.append(f"Live Equity  : ${float(acc.equity):,.2f} | Cash: ${float(acc.cash):,.2f}")
            lines.append(f"Buying Power : ${float(acc.buying_power):,.2f} | Shorting: {'ENABLED' if acc.shorting_enabled else 'DISABLED'}")

            alp_pos = broker.client.get_all_positions()
            lines.append(f"Live Alpaca Positions ({len(alp_pos)}):")
            for p in alp_pos:
                side_str = str(p.side.name).upper()
                qty_str = str(int(float(p.qty)))
                sym_str = str(p.symbol)
                avg_px = float(p.avg_entry_price)
                mkt_val = float(p.market_value)
                unr_pl = float(p.unrealized_pl)
                lines.append(f"  {side_str:5s} {qty_str:>5s} {sym_str:5s} @ ${avg_px:6.2f} (Val: ${mkt_val:,.2f} | PnL: ${unr_pl:+,.2f})")
            if not alp_pos:
                lines.append("  (No open positions on Alpaca)")
        except Exception as exc:
            lines.append(f"  Error fetching Alpaca positions: {exc}")

    report = "\n".join(lines)
    print(report)

    if accepted_entries or closed_today or not reconcile_ok or order_errors:
        send_webhook(report)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--equity", type=float, default=25_000.0)
    parser.add_argument("--skip-ingest", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without routing orders to Alpaca")
    args = parser.parse_args()
    run_daily(base_equity=args.equity, skip_ingest=args.skip_ingest, dry_run=args.dry_run)
