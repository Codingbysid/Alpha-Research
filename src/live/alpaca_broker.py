"""
Optional Alpaca Paper Trading execution adapter for Short Q5 + Long XLE hedge pairs.
Stays dormant unless ALPACA_API_KEY and ALPACA_SECRET_KEY are set in the environment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest


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
