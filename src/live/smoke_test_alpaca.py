"""
Safe Alpaca Paper smoke test:
1) Verifies account status & shortability for all 11 dual-sleeve stocks + XLE + XME
2) Submits a far-off-market $1.00 limit buy on XLE, checks idempotency, and cancels it

Usage:
    PYTHONPATH=. python3 src/live/smoke_test_alpaca.py
"""

from __future__ import annotations

from datetime import datetime, timezone

from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import LimitOrderRequest
from dotenv import load_dotenv

from src.live.alpaca_broker import AlpacaPaperBroker

UNIVERSE = ["VLO", "MPC", "PSX", "DINO", "PBF", "DK", "CVI", "XLE", "CLF", "NUE", "STLD", "CMC", "XME"]


def main() -> None:
    load_dotenv()
    broker = AlpacaPaperBroker.from_env()
    if broker is None:
        raise EnvironmentError("Set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env first.")

    acc = broker.client.get_account()
    print(f"[smoke] Account={acc.account_number} | Status={acc.status} | Equity=${float(acc.equity):,.2f} | BP=${float(acc.buying_power):,.2f}")

    print("\n=== ASSET SHORTABILITY CHECK ===")
    for sym in UNIVERSE:
        a = broker.client.get_asset(sym)
        print(f"  {sym:4s} | tradable={str(a.tradable):5s} | shortable={str(a.shortable):5s} | etb={str(a.easy_to_borrow):5s}")

    cid = f"SMOKE_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
    req = LimitOrderRequest(
        symbol="XLE",
        qty=1,
        side=OrderSide.BUY,
        limit_price=1.00,
        time_in_force=TimeInForce.DAY,
        client_order_id=cid,
    )
    order = broker.client.submit_order(order_data=req)
    print(f"\n[smoke] Submitted test order: id={order.id} | cid={cid} | status={order.status}")

    # Verify idempotency lookup works
    fetched = broker.client.get_order_by_client_id(cid)
    assert str(fetched.id) == str(order.id), "client_order_id lookup mismatch!"

    # Cancel immediately
    broker.client.cancel_order_by_id(order.id)
    print(f"[smoke] Canceled test order {order.id} cleanly. Smoke test PASSED.")


if __name__ == "__main__":
    main()
