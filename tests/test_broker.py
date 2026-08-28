"""Unit tests for quaestor.broker — httpx.MockTransport only, zero network.

Covers the execute() contract: happy-path fill, cancel/repost with fresh cid and
re-quote, 403 terminal no-retry, 429 honoring Retry-After, partial-fill
accumulation across attempts, and ambiguous-timeout -> lookup-before-resubmit.

If the parallel quaestor.orders slice is not present yet, a spec-faithful stub
(build_order_payload / marketable_limit, per docs/ARCHITECTURE.md) is installed
into sys.modules so broker.py imports cleanly either way.
"""
from __future__ import annotations

import itertools
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import pytest


def _install_orders_stub() -> None:
    """Provide quaestor.orders per its spec signatures if the real slice is absent."""
    try:
        import quaestor.orders  # noqa: F401
        return
    except ImportError:
        pass
    import quaestor
    from quaestor.models import TradeIntent, new_client_order_id

    stub = types.ModuleType("quaestor.orders")

    def build_order_payload(intent: TradeIntent, attempt: int, zk_prefix: str = "") -> dict[str, Any]:
        cid = new_client_order_id(intent.intent_id, attempt, zk_prefix)
        if intent.is_multileg:
            return {
                "order_class": "mleg",
                "qty": str(intent.qty),
                "type": "limit",
                "limit_price": f"{intent.limit_price:.2f}",
                "time_in_force": "day",
                "legs": [leg.to_alpaca() for leg in intent.legs],
                "client_order_id": cid,
            }
        leg = intent.legs[0]
        return {
            "symbol": leg.symbol,
            "qty": str(intent.qty),
            "side": leg.side.value,
            "type": "limit",
            "limit_price": f"{abs(intent.limit_price):.2f}",
            "time_in_force": "day",
            "position_intent": leg.position_intent.value,
            "client_order_id": cid,
        }

    def marketable_limit(intent: TradeIntent, chain: dict[str, Any], policy: dict[str, Any]) -> float:
        quote = chain.get(intent.legs[0].symbol) or {}
        buffer_usd = float(policy.get("execution", {}).get("marketable_buffer_usd", 0.02))
        return round(float(quote.get("ask", intent.limit_price)) + buffer_usd, 2)

    stub.build_order_payload = build_order_payload
    stub.marketable_limit = marketable_limit
    sys.modules["quaestor.orders"] = stub
    setattr(quaestor, "orders", stub)


_install_orders_stub()

import quaestor.broker as broker_mod  # noqa: E402
from quaestor.broker import Broker  # noqa: E402
from quaestor.models import Leg, PositionIntent, Side, Structure, TradeIntent  # noqa: E402

OCC = "SPY260904C00650000"
_RID_COUNTER = itertools.count(1)


# ---------------------------------------------------------------------- helpers

def resp(code: int, body: Any = None, headers: dict[str, str] | None = None) -> httpx.Response:
    all_headers = {"x-request-id": f"rid-{next(_RID_COUNTER)}"}
    if headers:
        all_headers.update(headers)
    if body is None:
        return httpx.Response(code, headers=all_headers)
    return httpx.Response(code, json=body, headers=all_headers)


def order_json(cid: str, status: str, *, oid: str = "ord-1", filled_qty: str = "0",
               avg: str | None = None, qty: str = "1") -> dict[str, Any]:
    return {
        "id": oid,
        "client_order_id": cid,
        "status": status,
        "qty": qty,
        "filled_qty": filled_qty,
        "filled_avg_price": avg,
        "symbol": OCC,
        "time_in_force": "day",
    }


def make_intent(qty: int = 1) -> TradeIntent:
    return TradeIntent(
        underlying="SPY",
        structure=Structure.LONG_CALL,
        legs=[Leg(symbol=OCC, side=Side.BUY, ratio_qty=1,
                  position_intent=PositionIntent.BUY_TO_OPEN)],
        qty=qty,
        limit_price=1.25,
        thesis="unit test",
        max_loss_usd=125.0 * qty,
        expiry="2026-09-04",
    )


def qty_of(payload: dict[str, Any]) -> int:
    return int(float(payload["qty"]))


# ---------------------------------------------------------------------- fixtures

@pytest.fixture
def policy() -> dict[str, Any]:
    """Tiny intervals so the repost loop expires in milliseconds — no slow tests."""
    return {
        "execution": {
            "time_in_force": "day",
            "marketable_buffer_usd": 0.02,
            "mleg_buffer_usd": 0.05,
            "repost_after_s": 0.05,
            "max_reposts": 2,
            "order_poll_interval_s": 0.005,
        }
    }


@pytest.fixture
def settings() -> SimpleNamespace:
    return SimpleNamespace(
        api_key="test-key-id",
        api_secret="test-secret",
        trading_base="https://paper-api.alpaca.markets",
        paper=True,
    )


@pytest.fixture
def chain() -> dict[str, Any]:
    return {OCC: {"bid": 1.20, "ask": 1.24, "mid": 1.22, "iv": 0.14, "delta": 0.45}}


def make_broker(settings: SimpleNamespace, handler) -> Broker:
    return Broker(settings, transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------- tests

def test_submit_filled_happy_path(policy, settings, chain):
    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/v2/orders":
            return resp(200, [])  # wash-trade guard: nothing open
        if request.method == "POST" and path == "/v2/orders":
            payload = json.loads(request.content)
            posted.append(payload)
            return resp(200, order_json(payload["client_order_id"], "accepted",
                                        qty=payload["qty"]))
        if request.method == "GET" and path == "/v2/orders:by_client_order_id":
            cid = request.url.params["client_order_id"]
            return resp(200, order_json(cid, "filled", filled_qty="1", avg="1.23"))
        raise AssertionError(f"unexpected call {request.method} {path}")

    broker = make_broker(settings, handler)
    report = broker.execute(make_intent(), chain, policy)

    assert report.status == "filled"
    assert report.attempts == 1
    assert len(posted) == 1
    assert report.client_order_id == posted[0]["client_order_id"]
    assert report.client_order_id.endswith("-a0")
    assert report.filled_qty == pytest.approx(1.0)
    assert report.filled_avg_price == pytest.approx(1.23)
    assert posted[0]["time_in_force"] == "day"
    assert report.request_ids, "x-request-id must be captured from every response"
    assert report.error == ""


def test_repost_after_no_fill_cancels_and_requotes(policy, settings, chain, monkeypatch):
    monkeypatch.setattr(broker_mod.orders, "marketable_limit",
                        lambda intent, ch, pol: 1.31)
    refresh_calls = {"n": 0}

    def chain_refresh() -> dict[str, Any]:
        refresh_calls["n"] += 1
        return chain

    posted: list[dict[str, Any]] = []
    canceled: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/v2/orders":
            return resp(200, [])
        if request.method == "POST" and path == "/v2/orders":
            payload = json.loads(request.content)
            posted.append(payload)
            return resp(200, order_json(payload["client_order_id"], "accepted",
                                        oid=f"ord-{len(posted)}", qty=payload["qty"]))
        if request.method == "DELETE" and path.startswith("/v2/orders/"):
            canceled.append(path.rsplit("/", 1)[-1])
            return resp(204)
        if request.method == "GET" and path == "/v2/orders:by_client_order_id":
            cid = request.url.params["client_order_id"]
            if cid.endswith("-a0"):
                status = "canceled" if canceled else "accepted"  # never fills
                return resp(200, order_json(cid, status, oid="ord-1"))
            return resp(200, order_json(cid, "filled", oid="ord-2",
                                        filled_qty="1", avg="1.30"))
        raise AssertionError(f"unexpected call {request.method} {path}")

    broker = make_broker(settings, handler)
    report = broker.execute(make_intent(), chain, policy, chain_refresh=chain_refresh)

    assert report.status == "filled"
    assert report.attempts == 2
    assert canceled == ["ord-1"], "the unfilled first order must be canceled before repost"
    assert len(posted) == 2
    assert posted[0]["client_order_id"].endswith("-a0")
    assert posted[1]["client_order_id"].endswith("-a1")
    assert posted[0]["client_order_id"] != posted[1]["client_order_id"]
    assert float(posted[1]["limit_price"]) == pytest.approx(1.31), \
        "second attempt must carry the fresh marketable limit"
    assert refresh_calls["n"] == 1, "repost must re-quote from the chain_refresh callback"
    assert report.filled_qty == pytest.approx(1.0)


def test_403_buying_power_is_terminal_no_retry(policy, settings, chain):
    post_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/v2/orders":
            return resp(200, [])
        if request.method == "POST" and path == "/v2/orders":
            post_count["n"] += 1
            return resp(403, {"code": 40310000,
                              "message": "insufficient options buying power"})
        raise AssertionError(f"unexpected call {request.method} {path}")

    broker = make_broker(settings, handler)
    report = broker.execute(make_intent(), chain, policy)

    assert report.status == "rejected"
    assert "403" in report.error
    assert "buying power" in report.error
    assert post_count["n"] == 1, "403 must never be retried"
    assert report.filled_qty == 0.0


def test_422_bad_payload_is_terminal_no_retry(policy, settings, chain):
    post_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/v2/orders":
            return resp(200, [])
        if request.method == "POST" and path == "/v2/orders":
            post_count["n"] += 1
            return resp(422, {"code": 42210000, "message": "invalid limit_price"})
        raise AssertionError(f"unexpected call {request.method} {path}")

    broker = make_broker(settings, handler)
    report = broker.execute(make_intent(), chain, policy)

    assert report.status == "rejected"
    assert "422" in report.error and "limit_price" in report.error
    assert post_count["n"] == 1, "422 must never be retried"


def test_429_honors_retry_after_then_succeeds(policy, settings, chain, monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(broker_mod.time, "sleep", lambda s: sleeps.append(float(s)))
    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/v2/orders":
            return resp(200, [])
        if request.method == "POST" and path == "/v2/orders":
            payload = json.loads(request.content)
            posted.append(payload)
            if len(posted) == 1:
                return resp(429, {"message": "too many requests"},
                            headers={"Retry-After": "7"})
            return resp(200, order_json(payload["client_order_id"], "filled",
                                        filled_qty="1", avg="1.24", qty=payload["qty"]))
        raise AssertionError(f"unexpected call {request.method} {path}")

    broker = make_broker(settings, handler)
    report = broker.execute(make_intent(), chain, policy)

    assert 7.0 in sleeps, "must sleep for exactly the Retry-After header value"
    assert report.status == "filled"
    assert len(posted) == 2
    assert posted[0]["client_order_id"] == posted[1]["client_order_id"], \
        "429 created no order, so the SAME client_order_id is resubmitted (same attempt)"
    assert report.attempts == 1


def test_partial_fill_accumulates_across_reposts(policy, settings, chain):
    posted: list[dict[str, Any]] = []
    canceled: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/v2/orders":
            return resp(200, [])
        if request.method == "POST" and path == "/v2/orders":
            payload = json.loads(request.content)
            posted.append(payload)
            return resp(200, order_json(payload["client_order_id"], "accepted",
                                        oid=f"ord-{len(posted)}", qty=payload["qty"]))
        if request.method == "DELETE" and path.startswith("/v2/orders/"):
            canceled.append(path.rsplit("/", 1)[-1])
            return resp(204)
        if request.method == "GET" and path == "/v2/orders:by_client_order_id":
            cid = request.url.params["client_order_id"]
            if cid.endswith("-a0"):
                status = "canceled" if canceled else "partially_filled"
                return resp(200, order_json(cid, status, oid="ord-1",
                                            filled_qty="1", avg="1.20", qty="3"))
            return resp(200, order_json(cid, "filled", oid="ord-2",
                                        filled_qty="2", avg="1.26", qty="2"))
        raise AssertionError(f"unexpected call {request.method} {path}")

    broker = make_broker(settings, handler)
    report = broker.execute(make_intent(qty=3), chain, policy)

    assert report.status == "filled"
    assert report.attempts == 2
    assert canceled == ["ord-1"]
    assert qty_of(posted[0]) == 3
    assert qty_of(posted[1]) == 2, "repost must submit only the unfilled remainder"
    assert report.filled_qty == pytest.approx(3.0), "1 partial + 2 on repost"
    # weighted average: (1*1.20 + 2*1.26) / 3 = 1.24
    assert report.filled_avg_price == pytest.approx(1.24)


def test_ambiguous_timeout_looks_up_before_resubmit(policy, settings, chain):
    post_count = {"n": 0}
    lookups = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/v2/orders":
            return resp(200, [])
        if request.method == "POST" and path == "/v2/orders":
            post_count["n"] += 1
            # The request "reached" Alpaca but the response was lost.
            raise httpx.ConnectTimeout("simulated timeout", request=request)
        if request.method == "GET" and path == "/v2/orders:by_client_order_id":
            lookups["n"] += 1
            cid = request.url.params["client_order_id"]
            if lookups["n"] == 1:
                return resp(200, order_json(cid, "accepted"))  # it DOES exist
            return resp(200, order_json(cid, "filled", filled_qty="1", avg="1.25"))
        raise AssertionError(f"unexpected call {request.method} {path}")

    broker = make_broker(settings, handler)
    report = broker.execute(make_intent(), chain, policy)

    assert post_count["n"] == 1, \
        "after an ambiguous network error the order must be looked up, NOT resubmitted"
    assert lookups["n"] >= 1
    assert report.status == "filled"
    assert report.attempts == 1
    assert report.filled_qty == pytest.approx(1.0)


def test_account_snapshot_casts_string_fields(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/v2/account":
            return resp(200, {
                "equity": "100000.25",
                "cash": "50000",
                "buying_power": "200000.5",
                "options_buying_power": "75000.5",
                "options_approved_level": "3",
                "options_trading_level": "3",
            })
        if request.method == "GET" and path == "/v2/positions":
            return resp(200, [{"symbol": OCC, "asset_class": "us_option",
                               "market_value": "-1250.0", "qty": "1"}])
        raise AssertionError(f"unexpected call {request.method} {path}")

    broker = make_broker(settings, handler)
    snap = broker.account_snapshot()

    assert snap.equity == pytest.approx(100000.25)
    assert snap.cash == pytest.approx(50000.0)
    assert snap.buying_power == pytest.approx(200000.5)
    assert snap.options_buying_power == pytest.approx(75000.5)
    assert snap.options_approved_level == 3
    assert isinstance(snap.options_approved_level, int)
    assert snap.options_trading_level == 3
    assert isinstance(snap.options_trading_level, int)
    assert snap.positions[0]["asset_class"] == "us_option"
    assert broker.request_ids, "account calls must also capture x-request-id"


def test_cancel_opposing_clears_open_orders_on_symbols(settings):
    canceled: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/v2/orders":
            assert request.url.params["status"] == "open"
            return resp(200, [
                {"id": "o1", "symbol": OCC, "side": "sell", "legs": None},
                {"id": "o2", "symbol": "QQQ260904P00470000", "side": "buy", "legs": None},
                {"id": "o3", "symbol": None,
                 "legs": [{"symbol": OCC}, {"symbol": "SPY260904C00655000"}]},
            ])
        if request.method == "DELETE" and path.startswith("/v2/orders/"):
            canceled.append(path.rsplit("/", 1)[-1])
            return resp(204)
        raise AssertionError(f"unexpected call {request.method} {path}")

    broker = make_broker(settings, handler)
    out = broker.cancel_opposing([OCC])

    assert set(canceled) == {"o1", "o3"}, "orders touching the contract (incl. mleg legs) go"
    assert set(out) == {"o1", "o3"}
