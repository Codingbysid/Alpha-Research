"""
Alpaca Paper Trading REST API Client.
Handles live account inspection, short sale execution, and ETF beta-hedge rebalancing.
"""

from __future__ import annotations

import os
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()


class AlpacaExecutionClient:
    """Production-grade Alpaca client for short Q5 refiners & long XLE hedges."""

    def __init__(
        self,
        api_key: str | None = None,
        secret_key: str | None = None,
        base_url: str | None = None,
    ):
        self.api_key = (
            api_key
            or os.getenv("APCA_API_KEY_ID")
            or os.getenv("ALPACA_API_KEY")
        )
        self.secret_key = (
            secret_key
            or os.getenv("APCA_API_SECRET_KEY")
            or os.getenv("ALPACA_SECRET_KEY")
        )
        url = (
            base_url
            or os.getenv("APCA_API_BASE_URL")
            or os.getenv("ALPACA_BASE_URL")
            or "https://paper-api.alpaca.markets"
        )
        # Normalize URL (strip /v2 if present)
        self.base_url = url.rstrip("/").replace("/v2", "")

    @property
    def headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.api_key or "",
            "APCA-API-SECRET-KEY": self.secret_key or "",
            "Content-Type": "application/json",
        }

    def is_configured(self) -> bool:
        """Verifies if API keys are present and authenticated."""
        if not self.api_key or not self.secret_key:
            return False
        try:
            r = requests.get(f"{self.base_url}/v2/account", headers=self.headers, timeout=5)
            return r.status_code == 200
        except Exception:
            return False

    def get_account(self) -> dict[str, Any]:
        """Fetches full account status from Alpaca."""
        r = requests.get(f"{self.base_url}/v2/account", headers=self.headers, timeout=10)
        r.raise_for_status()
        return r.json()

    def get_equity(self) -> float:
        """Returns current portfolio equity in USD."""
        acc = self.get_account()
        return float(acc.get("equity", 100_000.0))

    def get_buying_power(self) -> float:
        """Returns current RegT or margin buying power in USD."""
        acc = self.get_account()
        return float(acc.get("buying_power", 0.0))

    def get_clock(self) -> dict[str, Any]:
        """Checks market calendar and open/close status."""
        r = requests.get(f"{self.base_url}/v2/clock", headers=self.headers, timeout=10)
        r.raise_for_status()
        return r.json()

    def get_open_positions(self) -> list[dict[str, Any]]:
        """Returns all open positions on Alpaca."""
        r = requests.get(f"{self.base_url}/v2/positions", headers=self.headers, timeout=10)
        r.raise_for_status()
        return r.json()

    def get_position(self, symbol: str) -> dict[str, Any] | None:
        """Returns open position for a single symbol, or None if none open."""
        r = requests.get(f"{self.base_url}/v2/positions/{symbol.upper()}", headers=self.headers, timeout=10)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def submit_order(
        self,
        symbol: str,
        qty: int,
        side: str,
        order_type: str = "market",
        time_in_force: str = "day",
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Submits an order to Alpaca.
        To short: side="sell" when position is 0.
        To cover: side="buy" when short.
        To buy hedge: side="buy".
        """
        if qty <= 0:
            raise ValueError(f"Order quantity must be positive, got {qty}")

        payload: dict[str, Any] = {
            "symbol": symbol.upper(),
            "qty": str(qty),
            "side": side.lower(),
            "type": order_type.lower(),
            "time_in_force": time_in_force.lower(),
        }
        if client_order_id:
            # Client order ID max 128 chars, alphanumeric + dashes/underscores
            clean_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in client_order_id)[:128]
            payload["client_order_id"] = clean_id

        r = requests.post(f"{self.base_url}/v2/orders", json=payload, headers=self.headers, timeout=10)
        if r.status_code == 422 and "client_order_id must be unique" in r.text:
            # Order was already submitted previously (idempotent retry)
            print(f"[alpaca] Order {client_order_id} already exists, fetching existing order...")
            existing = requests.get(
                f"{self.base_url}/v2/orders/by_client_order_id?client_order_id={payload['client_order_id']}",
                headers=self.headers,
                timeout=10,
            )
            if existing.status_code == 200:
                return existing.json()

        r.raise_for_status()
        return r.json()

    def close_position(self, symbol: str) -> dict[str, Any]:
        """Liquidates or covers an entire open position at market."""
        r = requests.delete(f"{self.base_url}/v2/positions/{symbol.upper()}", headers=self.headers, timeout=10)
        r.raise_for_status()
        return r.json()

    def execute_short_hedge_pair(
        self,
        ticker: str,
        short_shares: int,
        xle_long_shares: int,
        trade_id: str,
    ) -> dict[str, Any]:
        """
        Executes an OSINT trade pair:
          1. Short stock (sell)
          2. Long XLE (buy)
        """
        results: dict[str, Any] = {}
        
        # 1. Short the refiner
        if short_shares > 0:
            print(f"[alpaca] Submitting SHORT order: {short_shares} shares of {ticker}...")
            short_res = self.submit_order(
                symbol=ticker,
                qty=short_shares,
                side="sell",
                client_order_id=f"{trade_id}_short",
            )
            results["short_order"] = short_res

        # 2. Buy the XLE beta hedge
        if xle_long_shares > 0:
            print(f"[alpaca] Submitting LONG HEDGE order: {xle_long_shares} shares of XLE...")
            xle_res = self.submit_order(
                symbol="XLE",
                qty=xle_long_shares,
                side="buy",
                client_order_id=f"{trade_id}_xle",
            )
            results["hedge_order"] = xle_res

        return results

    def close_short_hedge_pair(
        self,
        ticker: str,
        trade_id: str,
        short_shares: int | None = None,
        xle_long_shares: int | None = None,
    ) -> dict[str, Any]:
        """
        Closes an OSINT trade pair at exit:
          1. Buy to cover the short refiner stock
          2. Sell proportional XLE hedge shares
        """
        results: dict[str, Any] = {}

        # 1. Cover the short stock
        try:
            print(f"[alpaca] Closing short position on {ticker}...")
            if short_shares:
                cover_res = self.submit_order(
                    symbol=ticker,
                    qty=short_shares,
                    side="buy",
                    client_order_id=f"{trade_id}_cover",
                )
            else:
                cover_res = self.close_position(ticker)
            results["cover_order"] = cover_res
        except Exception as e:
            print(f"[alpaca] Warning: Failed to close {ticker}: {e}")
            results["cover_error"] = str(e)

        # 2. Sell proportional XLE hedge shares
        if xle_long_shares and xle_long_shares > 0:
            try:
                print(f"[alpaca] Unwinding XLE hedge: selling {xle_long_shares} shares...")
                unwind_res = self.submit_order(
                    symbol="XLE",
                    qty=xle_long_shares,
                    side="sell",
                    client_order_id=f"{trade_id}_unwind_xle",
                )
                results["unwind_order"] = unwind_res
            except Exception as e:
                print(f"[alpaca] Warning: Failed to unwind XLE hedge: {e}")
                results["unwind_error"] = str(e)

        return results
