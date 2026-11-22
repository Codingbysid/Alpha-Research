"""
Optional Alpaca Paper Trading execution adapter for Short Q5 + Long XLE hedge pairs.
Stays dormant unless ALPACA_API_KEY and ALPACA_SECRET_KEY are set in the environment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

NY_TZ = ZoneInfo("America/New_York")
CASH_CLOSE = (16, 0)  # 16:00 ET; after this the next executable session is tomorrow


def _as_ny_date(dt: datetime | date | str) -> str:
    if isinstance(dt, date) and not isinstance(dt, datetime):
        return dt.isoformat()
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    if getattr(dt, "tzinfo", None) is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(NY_TZ).date().isoformat()


def ny_calendar_session_date(now: datetime | None = None) -> str:
    """NYSE weekday fallback when the Alpaca clock is unavailable. Ignores holidays."""
    now = now or datetime.now(NY_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=NY_TZ)
    else:
        now = now.astimezone(NY_TZ)
    d = now.date()
    weekday = now.weekday()
    after_close = (now.hour, now.minute) >= CASH_CLOSE
    if weekday >= 5:
        d = d + timedelta(days=(7 - weekday))  # Sat/Sun -> Monday
    elif after_close:
        d = d + timedelta(days=1)
        if d.weekday() >= 5:
            d = d + timedelta(days=(7 - d.weekday()))
    return d.isoformat()


def resolve_expected_session(broker: "AlpacaPaperBroker | None") -> str:
    """Date of the US cash session we are allowed to open into."""
    if broker is not None:
        return broker.expected_session_date()
    return ny_calendar_session_date()


@dataclass
class AlpacaPaperBroker:
    client: TradingClient

    @classmethod
    def from_env(cls) -> AlpacaPaperBroker | None:
        key = (os.getenv("ALPACA_API_KEY") or os.getenv("APCA_API_KEY_ID") or "").strip()
        secret = (os.getenv("ALPACA_SECRET_KEY") or os.getenv("APCA_API_SECRET_KEY") or "").strip()
        if not key or not secret:
            return None
        return cls(client=TradingClient(key, secret, paper=True))

    def is_market_open(self) -> bool:
        """Returns True if the U.S. equities market is currently open for trading."""
        try:
            clock = self.client.get_clock()
            return bool(clock.is_open)
        except Exception as exc:
            print(f"[broker][warn] Could not fetch market clock: {exc}")
            return False

    def expected_session_date(self) -> str:
        """
        NY date of the session we may open positions into:
        today's date while the cash market is open, otherwise next_open.
        """
        try:
            clock = self.client.get_clock()
            raw = clock.timestamp if bool(clock.is_open) else clock.next_open
            return _as_ny_date(raw)
        except Exception as exc:
            print(f"[broker][warn] Could not resolve session date: {exc}")
            return ny_calendar_session_date()

    def _submit_qty(
        self,
        symbol: str,
        qty: int,
        side: OrderSide,
        client_order_id: str,
        tif: TimeInForce = TimeInForce.DAY,
    ) -> str:
        if qty <= 0:
            return "SKIP_ZERO_QTY"
        try:
            existing = self.client.get_order_by_client_id(client_order_id)
            return f"EXISTS:{existing.id}"
        except APIError:
            pass

        req = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side,
            time_in_force=tif,
            client_order_id=client_order_id,
        )
        order = self.client.submit_order(order_data=req)
        return f"SUBMITTED:{order.id}({order.status})"

    def open_short_pair(
        self,
        trade_id: str,
        ticker: str,
        short_shares: int,
        xle_delta_shares: int = 0,
        tif: TimeInForce = TimeInForce.DAY,
    ) -> list[str]:
        msgs: list[str] = []
        if short_shares > 0:
            s_msg = self._submit_qty(
                symbol=ticker,
                qty=short_shares,
                side=OrderSide.SELL,
                client_order_id=f"{trade_id}_SHORT",
                tif=tif,
            )
            msgs.append(f"SHORT {short_shares} {ticker} -> {s_msg}")

        if xle_delta_shares > 0:
            h_msg = self._submit_qty(
                symbol="XLE",
                qty=xle_delta_shares,
                side=OrderSide.BUY,
                client_order_id=f"{trade_id}_HEDGE_BUY",
                tif=tif,
            )
            msgs.append(f"BUY {xle_delta_shares} XLE -> {h_msg}")
        return msgs

    def close_short_pair(
        self,
        trade_id: str,
        ticker: str,
        short_shares: int,
        xle_release_shares: int = 0,
        tif: TimeInForce = TimeInForce.DAY,
    ) -> list[str]:
        msgs: list[str] = []
        if short_shares > 0:
            c_msg = self._submit_qty(
                symbol=ticker,
                qty=short_shares,
                side=OrderSide.BUY,
                client_order_id=f"{trade_id}_CLOSE_SHORT",
                tif=tif,
            )
            msgs.append(f"COVER {short_shares} {ticker} -> {c_msg}")

        if xle_release_shares > 0:
            s_msg = self._submit_qty(
                symbol="XLE",
                qty=xle_release_shares,
                side=OrderSide.SELL,
                client_order_id=f"{trade_id}_HEDGE_SELL",
                tif=tif,
            )
            msgs.append(f"SELL {xle_release_shares} XLE -> {s_msg}")
        return msgs

    def open_long_pair(
        self,
        trade_id: str,
        ticker: str,
        long_shares: int,
        tif: TimeInForce = TimeInForce.DAY,
    ) -> list[str]:
        msgs: list[str] = []
        if long_shares > 0:
            l_msg = self._submit_qty(
                symbol=ticker,
                qty=long_shares,
                side=OrderSide.BUY,
                client_order_id=f"{trade_id}_LONG",
                tif=tif,
            )
            msgs.append(f"BUY {long_shares} {ticker} -> {l_msg}")
        return msgs

    def close_long_pair(
        self,
        trade_id: str,
        ticker: str,
        long_shares: int,
        tif: TimeInForce = TimeInForce.DAY,
    ) -> list[str]:
        msgs: list[str] = []
        if long_shares > 0:
            s_msg = self._submit_qty(
                symbol=ticker,
                qty=long_shares,
                side=OrderSide.SELL,
                client_order_id=f"{trade_id}_CLOSE_LONG",
                tif=tif,
            )
            msgs.append(f"SELL {long_shares} {ticker} -> {s_msg}")
        return msgs

    def rebalance_hedge_etf(
        self,
        symbol: str,
        target_signed_shares: int,
        as_of_str: str,
        tag: str,
    ) -> str:
        """
        Rebalances live Alpaca position in `symbol` to match `target_signed_shares`.
        Positive target_signed_shares = Long (e.g. XLE for Short Refiners).
        Negative target_signed_shares = Short (e.g. XME for Long Steel).
        """
        sym = symbol.upper()
        try:
            positions = self.client.get_all_positions()
            cur_signed_shares = 0
            for p in positions:
                if str(p.symbol).upper() == sym:
                    raw_qty = int(float(p.qty))
                    is_short = (
                        str(getattr(p, "side", "")).upper() in ("SHORT", "POSITIONSIDE.SHORT")
                        or str(getattr(getattr(p, "side", None), "name", "")).upper() == "SHORT"
                        or raw_qty < 0
                    )
                    cur_signed_shares = -abs(raw_qty) if is_short else abs(raw_qty)
                    break
        except Exception as exc:
            return f"{sym.lower()}-query-err:{exc}"

        delta = target_signed_shares - cur_signed_shares
        if delta == 0:
            return f"{sym.lower()}-flat:{cur_signed_shares}"

        cid = f"{as_of_str}_{tag}_{sym}_{target_signed_shares}_{abs(delta)}"
        if delta > 0:
            res = self._submit_qty(sym, abs(delta), OrderSide.BUY, cid)
        else:
            res = self._submit_qty(sym, abs(delta), OrderSide.SELL, cid)
        return f"{sym.lower()}:{cur_signed_shares}->{target_signed_shares}({res})"

