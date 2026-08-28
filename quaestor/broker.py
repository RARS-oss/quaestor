"""Execution engine: raw REST calls to the Alpaca paper trading API via httpx.

Role: turn an approved TradeIntent into a filled order with an idempotent,
marketable-limit cancel/repost loop, and expose account/position/order plumbing
for the rest of the agent.

Alpaca facts encoded here (verified 2026-08-28, do not re-derive):
- Auth is via the ``APCA-API-KEY-ID`` / ``APCA-API-SECRET-KEY`` headers against
  ``settings.trading_base`` (must be https://paper-api.alpaca.markets — paper only).
- Paper fills are marketable-at-NBBO-touch with ~10% random partials and zero fees;
  a limit resting inside the spread does NOT fill until the quote crosses. Hence
  execute() always works marketable limits and cancels/reposts unfilled orders.
- Every order carries a unique client_order_id (fresh one per attempt, built by
  quaestor.orders.build_order_payload). After an ambiguous network error the order
  is looked up by client_order_id BEFORE any resubmit — never double-send.
- POST /v2/orders: 403 = insufficient buying power (or wash-trade block) — terminal,
  never retried. 422 = malformed payload — terminal, never retried. 429 = rate
  limit — sleep honoring the Retry-After header, then resubmit the same payload
  (the 429'd request created no order, so the same client_order_id is safe).
- Options TIF is always "day". mleg net limit_price is signed (+debit / -credit);
  re-quotes on repost never flip that sign.
- Wash-trade protection: an opposing open order on the same contract makes a new
  submit 403 — cancel_opposing() clears open orders on the intent's symbols first.
- GET /v2/account returns numeric fields as strings (equity, cash, buying_power,
  options_buying_power, options_approved_level, options_trading_level) — cast here.
- Every response's ``x-request-id`` header is captured (self.request_ids and
  ExecutionReport.request_ids) for the audit trail.
"""
from __future__ import annotations

import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Callable

import httpx

from quaestor import orders
from quaestor.models import AccountSnapshot, ExecutionReport, TradeIntent

if TYPE_CHECKING:  # pragma: no cover — config.py is a sibling slice; duck-typed at runtime
    from quaestor.config import Settings

#: Order statuses after which Alpaca will not fill any more quantity on that order.
TERMINAL_ORDER_STATUSES: frozenset[str] = frozenset(
    {"filled", "canceled", "expired", "rejected", "done_for_day", "stopped", "replaced", "suspended"}
)

_MAX_429_RETRIES = 5          # per submit attempt
_MAX_NETWORK_RETRIES = 3      # per submit attempt, only after confirming order absent
_DEFAULT_TIMEOUT_S = 10.0
_DEFAULT_RETRY_AFTER_S = 1.0
_CANCEL_SETTLE_POLLS = 3      # short polls waiting for a canceled order to reach terminal state


def _num(value: Any, default: float = 0.0) -> float:
    """Cast Alpaca's string/None numerics to float, defaulting on junk."""
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _append_err(existing: str, new: str) -> str:
    return f"{existing}; {new}" if existing else new


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    """Parse Retry-After as seconds; None if absent or not numeric."""
    raw = resp.headers.get("Retry-After", "")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


class BrokerAPIError(Exception):
    """Non-2xx HTTP response from the Alpaca trading API."""

    def __init__(self, status_code: int, message: str, request_id: str = "",
                 retry_after: float | None = None) -> None:
        super().__init__(f"HTTP {status_code}: {message}")
        self.status_code = status_code
        self.message = message
        self.request_id = request_id
        self.retry_after = retry_after


class TerminalOrderError(Exception):
    """Execution cannot proceed for this intent (403/422/unrecoverable ambiguity)."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class Broker:
    """Thin, auditable REST client for Alpaca paper trading + the execute() loop."""

    def __init__(self, settings: "Settings", transport: httpx.BaseTransport | None = None) -> None:
        self.settings = settings
        self.request_ids: list[str] = []       # every x-request-id ever seen, in order
        self.last_request_id: str = ""
        headers = {
            "APCA-API-KEY-ID": settings.api_key,
            "APCA-API-SECRET-KEY": settings.api_secret,
            "accept": "application/json",
        }
        self._client = httpx.Client(
            base_url=str(settings.trading_base).rstrip("/"),
            headers=headers,
            timeout=_DEFAULT_TIMEOUT_S,
            transport=transport,
        )

    # ------------------------------------------------------------------ plumbing

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "Broker":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None,
                 json_body: dict[str, Any] | None = None,
                 allow: frozenset[int] | set[int] = frozenset()) -> httpx.Response:
        """One HTTP call. Captures x-request-id; raises BrokerAPIError on unexpected 4xx/5xx.

        Idempotent methods (GET/DELETE) transparently retry once on 429 honoring
        Retry-After; POST 429s propagate so execute() controls the backoff.
        """
        retried_429 = False
        while True:
            resp = self._client.request(method, path, params=params, json=json_body)
            rid = resp.headers.get("x-request-id", "")
            if rid:
                self.request_ids.append(rid)
            self.last_request_id = rid
            if resp.status_code == 429 and method in ("GET", "DELETE") and not retried_429:
                retried_429 = True
                time.sleep(_retry_after_seconds(resp) or _DEFAULT_RETRY_AFTER_S)
                continue
            if resp.status_code >= 400 and resp.status_code not in allow:
                try:
                    body = resp.json()
                    message = str(body.get("message") or body) if isinstance(body, dict) else str(body)
                except ValueError:
                    message = resp.text[:300]
                raise BrokerAPIError(resp.status_code, message, request_id=rid,
                                     retry_after=_retry_after_seconds(resp))
            return resp

    # ------------------------------------------------------------------ account / positions

    def account_snapshot(self) -> AccountSnapshot:
        """GET /v2/account + GET /v2/positions. All numeric account fields arrive as strings."""
        acct: dict[str, Any] = self._request("GET", "/v2/account").json()
        positions = self.positions()
        return AccountSnapshot(
            equity=_num(acct.get("equity")),
            cash=_num(acct.get("cash")),
            buying_power=_num(acct.get("buying_power")),
            options_buying_power=_num(acct.get("options_buying_power")),
            options_approved_level=int(_num(acct.get("options_approved_level"))),
            options_trading_level=int(_num(acct.get("options_trading_level"))),
            positions=positions,
        )

    def positions(self) -> list[dict[str, Any]]:
        """GET /v2/positions — raw position dicts (market_value etc. are strings)."""
        data = self._request("GET", "/v2/positions").json()
        return list(data) if isinstance(data, list) else []

    def open_orders(self) -> list[dict[str, Any]]:
        """GET /v2/orders?status=open — includes mleg parents with their legs."""
        data = self._request("GET", "/v2/orders", params={"status": "open", "limit": "500"}).json()
        return list(data) if isinstance(data, list) else []

    # ------------------------------------------------------------------ orders

    def submit(self, payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """POST /v2/orders -> (order dict, x-request-id). Raises BrokerAPIError on 4xx/5xx."""
        resp = self._request("POST", "/v2/orders", json_body=payload)
        return resp.json(), self.last_request_id

    def get_by_client_id(self, cid: str) -> dict[str, Any] | None:
        """GET /v2/orders:by_client_order_id — None if the order does not exist (404)."""
        resp = self._request("GET", "/v2/orders:by_client_order_id",
                             params={"client_order_id": cid}, allow={404})
        if resp.status_code == 404:
            return None
        return resp.json()

    def cancel(self, order_id: str) -> None:
        """DELETE /v2/orders/{id}. 404/422 tolerated (already gone / already terminal)."""
        self._request("DELETE", f"/v2/orders/{order_id}", allow={404, 422})

    def close_position(self, symbol_or_id: str) -> dict[str, Any]:
        """DELETE /v2/positions/{symbol_or_id} -> the closing order Alpaca created."""
        return self._request("DELETE", f"/v2/positions/{symbol_or_id}").json()

    def cancel_opposing(self, symbols: list[str]) -> list[str]:
        """Wash-trade guard: cancel every open order touching any of these contracts.

        Alpaca 403s a new order when an opposing open order exists on the same
        symbol; canceling any working order on the intent's contracts first is the
        safe superset. Returns the canceled order ids.
        """
        targets = {s for s in symbols if s}
        if not targets:
            return []
        canceled: list[str] = []
        for order in self.open_orders():
            order_symbols: set[str] = set()
            sym = order.get("symbol")
            if sym:
                order_symbols.add(str(sym))
            for leg in order.get("legs") or []:
                leg_sym = leg.get("symbol") if isinstance(leg, dict) else None
                if leg_sym:
                    order_symbols.add(str(leg_sym))
            if order_symbols & targets:
                oid = str(order.get("id") or "")
                if oid:
                    self.cancel(oid)
                    canceled.append(oid)
        return canceled

    # ------------------------------------------------------------------ execute loop

    def execute(self, intent: TradeIntent, chain: dict[str, Any], policy: dict[str, Any],
                zk_prefix: str = "",
                chain_refresh: Callable[[], dict[str, Any]] | None = None) -> ExecutionReport:
        """Idempotent marketable-limit execution loop for one approved intent.

        attempt 0..max_reposts: build payload with a fresh client_order_id per
        attempt -> submit (429: sleep Retry-After then resubmit same cid; ambiguous
        network error: get_by_client_id BEFORE any resubmit; 403/422: terminal) ->
        poll the order every order_poll_interval_s until a terminal status or
        repost_after_s elapses -> if still unfilled: cancel, accumulate any partial
        fill, re-quote from chain_refresh() (or the original chain when None) and
        resubmit the remaining qty as attempt+1.
        """
        exec_cfg = policy.get("execution", {}) if isinstance(policy, dict) else {}
        repost_after_s = float(exec_cfg.get("repost_after_s", 8.0))
        poll_s = float(exec_cfg.get("order_poll_interval_s", 1.5))
        max_reposts = int(exec_cfg.get("max_reposts", 5))

        report = ExecutionReport(intent_id=intent.intent_id, client_order_id="")
        rid_start = len(self.request_ids)
        total_filled = 0.0
        fill_notional = 0.0                 # sum(filled_qty * filled_avg_price) across attempts
        current_chain: dict[str, Any] = chain
        work = replace(intent)              # working copy; caller's intent is never mutated
        last_order: dict[str, Any] = {}

        try:
            self.cancel_opposing(sorted({leg.symbol for leg in intent.legs}))
        except (BrokerAPIError, httpx.HTTPError) as exc:
            report.error = _append_err(report.error, f"wash-guard: {exc}")

        attempt = 0
        while attempt <= max_reposts:
            remaining = intent.qty - int(total_filled)
            if remaining <= 0:
                break
            if attempt > 0:
                current_chain = self._maybe_refresh_chain(chain_refresh, current_chain)
                work = replace(work, qty=remaining,
                               limit_price=self._requote(work, current_chain, policy))
            else:
                work = replace(work, qty=remaining)

            try:
                payload = orders.build_order_payload(work, attempt, zk_prefix)
            except ValueError as exc:
                report.status = "error"
                report.error = _append_err(report.error, f"payload: {exc}")
                break
            cid = str(payload.get("client_order_id", ""))
            report.client_order_id = cid
            report.attempts = attempt + 1

            try:
                order = self._submit_with_recovery(payload)
            except TerminalOrderError as exc:
                report.status = "rejected" if exc.status_code in (403, 422) else "error"
                report.error = _append_err(report.error, f"{exc.status_code}: {exc.message}")
                break

            report.order_id = str(order.get("id") or report.order_id)
            order = self._poll_until_terminal_or(cid, order, repost_after_s, poll_s)
            last_order = order
            status = str(order.get("status", ""))
            filled_here = _num(order.get("filled_qty"))
            avg_here = _num(order.get("filled_avg_price"))

            if status == "filled":
                total_filled += filled_here
                fill_notional += filled_here * avg_here
                report.status = "filled"
                break
            if status == "rejected":
                report.status = "rejected"
                report.error = _append_err(
                    report.error, str(order.get("reject_reason") or "order rejected"))
                break
            if status in ("canceled", "expired", "done_for_day", "stopped", "replaced", "suspended"):
                total_filled += filled_here
                fill_notional += filled_here * avg_here
                attempt += 1
                continue

            # Still working (new / accepted / pending_new / partially_filled).
            if attempt >= max_reposts:
                # Out of reposts: leave the marketable day order working, report honestly.
                total_filled += filled_here
                fill_notional += filled_here * avg_here
                report.status = "partially_filled" if total_filled > 0 else (status or "accepted")
                break

            oid = str(order.get("id") or "")
            try:
                if oid:
                    self.cancel(oid)
            except (BrokerAPIError, httpx.HTTPError) as exc:
                report.error = _append_err(report.error, f"cancel: {exc}")
            final = self._settled_state(cid, order, poll_s)
            last_order = final
            fq = _num(final.get("filled_qty"))
            fill_notional += fq * _num(final.get("filled_avg_price"))
            total_filled += fq
            if intent.qty - int(total_filled) <= 0:
                report.status = "filled"
                break
            attempt += 1

        if not report.status:
            if total_filled >= intent.qty:
                report.status = "filled"
            elif total_filled > 0:
                report.status = "partially_filled"
            else:
                report.status = "canceled"
        report.filled_qty = total_filled
        report.filled_avg_price = (fill_notional / total_filled) if total_filled > 0 else 0.0
        report.raw = last_order
        report.request_ids = list(self.request_ids[rid_start:])
        return report

    # ------------------------------------------------------------------ execute helpers

    def _submit_with_recovery(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Submit once, idempotently, or raise TerminalOrderError.

        429 -> sleep honoring Retry-After and resubmit the same payload (no order
        was created). Ambiguous network error -> look the order up by
        client_order_id FIRST; only when confirmed absent (404) is the same
        payload resubmitted. 403/422 -> terminal, no retry.
        """
        cid = str(payload.get("client_order_id", ""))
        rate_tries = 0
        net_tries = 0
        while True:
            try:
                order, _rid = self.submit(payload)
                return order
            except BrokerAPIError as exc:
                if exc.status_code == 429:
                    rate_tries += 1
                    if rate_tries > _MAX_429_RETRIES:
                        raise TerminalOrderError(429, "rate limited: retries exhausted") from exc
                    delay = exc.retry_after if exc.retry_after is not None \
                        else min(_DEFAULT_RETRY_AFTER_S * rate_tries, 10.0)
                    time.sleep(delay)
                    continue
                if exc.status_code in (403, 422):
                    raise TerminalOrderError(exc.status_code, exc.message) from exc
                # Unexpected status (e.g. 500): the order may or may not exist — check.
                existing = self._lookup_or_terminal(cid, f"http {exc.status_code}")
                if existing is not None:
                    return existing
                raise TerminalOrderError(exc.status_code,
                                         f"http {exc.status_code}: {exc.message}") from exc
            except httpx.TransportError as exc:
                # Ambiguous: request may have reached Alpaca. Look up BEFORE resubmit.
                existing = self._lookup_or_terminal(cid, f"network error ({exc!r})")
                if existing is not None:
                    return existing
                net_tries += 1
                if net_tries > _MAX_NETWORK_RETRIES:
                    raise TerminalOrderError(0, f"network error, order absent: {exc}") from exc
                continue  # confirmed absent — safe to resubmit the same client_order_id

    def _lookup_or_terminal(self, cid: str, context: str) -> dict[str, Any] | None:
        """get_by_client_id; None means CONFIRMED absent (404). If the lookup itself
        fails, ambiguity is unresolved — refuse to resubmit (never double-send)."""
        try:
            return self.get_by_client_id(cid)
        except (BrokerAPIError, httpx.HTTPError) as exc:
            raise TerminalOrderError(
                0, f"ambiguous submit after {context}; lookup failed: {exc}") from exc

    def _poll_until_terminal_or(self, cid: str, order: dict[str, Any],
                                window_s: float, poll_s: float) -> dict[str, Any]:
        """Poll the order by client_order_id until a terminal status or window_s elapses."""
        deadline = time.monotonic() + window_s
        current = order
        while True:
            if str(current.get("status", "")) in TERMINAL_ORDER_STATUSES:
                return current
            if time.monotonic() >= deadline:
                return current
            time.sleep(poll_s)
            try:
                fetched = self.get_by_client_id(cid)
            except (BrokerAPIError, httpx.HTTPError):
                fetched = None
            if fetched is not None:
                current = fetched

    def _settled_state(self, cid: str, fallback: dict[str, Any], poll_s: float) -> dict[str, Any]:
        """After a cancel, briefly re-poll so the final filled_qty (fills can land
        while the cancel is in flight) is captured before the next attempt."""
        current = fallback
        for _ in range(_CANCEL_SETTLE_POLLS):
            try:
                fetched = self.get_by_client_id(cid)
            except (BrokerAPIError, httpx.HTTPError):
                fetched = None
            if fetched is not None:
                current = fetched
            if str(current.get("status", "")) in TERMINAL_ORDER_STATUSES:
                return current
            time.sleep(poll_s)
        return current

    @staticmethod
    def _maybe_refresh_chain(chain_refresh: Callable[[], dict[str, Any]] | None,
                             current: dict[str, Any]) -> dict[str, Any]:
        """Fresh chain snapshot for re-quoting; fall back to the last one on any failure."""
        if chain_refresh is None:
            return current
        try:
            fresh = chain_refresh()
        except Exception:  # noqa: BLE001 — user callback; a bad refresh must not kill execution
            return current
        return fresh if fresh else current

    def _requote(self, work: TradeIntent, chain: dict[str, Any],
                 policy: dict[str, Any]) -> float:
        """New marketable limit from the fresh chain touch. NEVER flips the sign of
        the net price (+debit/-credit is structural, not a market opinion)."""
        try:
            fresh = float(orders.marketable_limit(work, chain, policy))
        except Exception:  # noqa: BLE001 — stale/missing quotes: keep the old price
            return work.limit_price
        if work.limit_price != 0 and (fresh > 0) != (work.limit_price > 0):
            return work.limit_price
        return round(fresh, 2)
