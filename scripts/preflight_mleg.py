"""Preflight: prove our mleg payload builder is accepted by the real paper API.

Submits a 1-lot deep-OTM SPY call debit vertical at a $0.05 limit (won't fill),
prints the API's acceptance, then cancels it immediately. Run on the DEV paper
account only. Safe on weekends: order queues, we cancel before it ever trades.

Usage (WSL):  ~/hack/venv/bin/python scripts/preflight_mleg.py
"""
from __future__ import annotations

import sys
import time

from quaestor.broker import Broker
from quaestor.config import load_settings
from quaestor.models import Leg, PositionIntent, Side, Structure, TradeIntent
from quaestor.orders import build_order_payload, occ_parse


def main() -> int:
    settings = load_settings()
    broker = Broker(settings)

    acct = broker.account_snapshot()
    print(f"account ok: equity=${acct.equity:,.0f} options L{acct.options_trading_level}")

    # Discover next-Friday SPY calls, take two adjacent far-OTM strikes.
    import httpx

    r = httpx.get(
        f"{settings.trading_base}/v2/options/contracts",
        headers={
            "APCA-API-KEY-ID": settings.api_key,
            "APCA-API-SECRET-KEY": settings.api_secret,
        },
        params={
            "underlying_symbols": "SPY",
            "type": "call",
            "expiration_date_gte": "2026-09-03",
            "expiration_date_lte": "2026-09-04",
            "limit": 300,
        },
        timeout=30,
    )
    r.raise_for_status()
    contracts = r.json().get("option_contracts", [])
    if len(contracts) < 2:
        print("not enough contracts returned", file=sys.stderr)
        return 1
    strikes = sorted(
        (float(c["strike_price"]), c["symbol"]) for c in contracts if c.get("tradable", True)
    )
    hi = strikes[-2:]  # two highest (deepest OTM) adjacent strikes
    buy_sym, sell_sym = hi[0][1], hi[1][1]
    print(f"legs: buy {buy_sym} (K={hi[0][0]}) / sell {sell_sym} (K={hi[1][0]})")

    intent = TradeIntent(
        underlying="SPY",
        structure=Structure.VERTICAL_DEBIT,
        legs=[
            Leg(buy_sym, Side.BUY, 1, PositionIntent.BUY_TO_OPEN),
            Leg(sell_sym, Side.SELL, 1, PositionIntent.SELL_TO_OPEN),
        ],
        qty=1,
        limit_price=0.05,
        thesis="preflight mleg validation — cancel immediately",
        max_loss_usd=5.0,
        expiry=str(occ_parse(buy_sym)["expiry"]),
    )
    payload = build_order_payload(intent, attempt=1)
    print("payload:", payload)

    order, req_id = broker.submit(payload)
    print(f"SUBMIT OK  id={order.get('id')}  status={order.get('status')}  x-request-id={req_id}")
    print(f"  order_class={order.get('order_class')}  legs={len(order.get('legs') or [])}")

    time.sleep(1.0)
    broker.cancel(order["id"])
    time.sleep(1.0)
    after = broker.get_by_client_id(payload["client_order_id"])
    print(f"CANCEL OK  status={after.get('status') if after else 'gone'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
