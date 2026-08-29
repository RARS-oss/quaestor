"""Runs INSIDE a bulla cell: executes approved orders over the sealed tunnel.

The decision (strategy + risk) happens OUTSIDE the cell (it needs the venv). The
approved order payloads are handed in via /work/orders.json; this stdlib-only
script places each over the CONNECT tunnel, polls it for the fill window, cancels
if unfilled, and writes /work/results.json. Because it runs inside the cell, the
actual exchange conversation for every order is hash-chained into the signed
receipt — so the whole week's real trading is verifiable, not a demo.

orders.json  = {"orders": [<alpaca order payload>, ...], "poll_seconds": 8,
                "poll_interval": 1.5}
results.json = {"results": [{client_order_id, order_id, status, filled_qty,
                filled_avg_price, request_ids, error}, ...]}
"""
import json
import os
import socket
import ssl
import sys
import time

SOCK = "/work/.bulla/egress.sock"
HOST = "paper-api.alpaca.markets"
_TERMINAL = {"filled", "canceled", "expired", "rejected", "done_for_day", "stopped", "replaced", "suspended"}


def _dechunk(body):
    out = bytearray()
    i = 0
    while i < len(body):
        nl = body.find(b"\r\n", i)
        if nl < 0:
            break
        try:
            size = int(body[i:nl].split(b";", 1)[0].strip(), 16)
        except ValueError:
            break
        if size == 0:
            break
        start = nl + 2
        out += body[start:start + size]
        i = start + size + 2
    return bytes(out)


def _req(method, path, key, secret, body=None):
    """One HTTPS request through the sealed tunnel. Returns (code, json|None, x_request_id)."""
    raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    raw.settimeout(20.0)
    raw.connect(SOCK)
    raw.sendall(f"CONNECT {HOST} 443\n".encode())
    status = b""
    while not status.endswith(b"\n"):
        b = raw.recv(1)
        if not b:
            raise RuntimeError("broker closed before OK")
        status += b
    if status.strip() != b"OK":
        raise RuntimeError(f"broker refused: {status!r}")

    tls = ssl.create_default_context().wrap_socket(raw, server_hostname=HOST)
    payload = json.dumps(body).encode() if body is not None else b""
    lines = [
        f"{method} {path} HTTP/1.1", f"Host: {HOST}",
        f"APCA-API-KEY-ID: {key}", f"APCA-API-SECRET-KEY: {secret}",
        "Accept: application/json", "Accept-Encoding: identity", "Connection: close",
    ]
    if payload:
        lines.append("Content-Type: application/json")
        lines.append(f"Content-Length: {len(payload)}")
    tls.sendall(("\r\n".join(lines) + "\r\n\r\n").encode() + payload)

    resp = b""
    while True:
        chunk = tls.recv(65536)
        if not chunk:
            break
        resp += chunk
    tls.close()

    sep = resp.find(b"\r\n\r\n")
    head_lines = resp[:sep].decode("latin-1").splitlines()
    code = int(head_lines[0].split(" ")[1])
    xrid = ""
    chunked = False
    for h in head_lines[1:]:
        low = h.lower()
        if low.startswith("x-request-id:"):
            xrid = h.split(":", 1)[1].strip()
        if low.startswith("transfer-encoding") and "chunked" in low:
            chunked = True
    body_bytes = resp[sep + 4:]
    if chunked:
        body_bytes = _dechunk(body_bytes)
    parsed = None
    if body_bytes.strip():
        try:
            parsed = json.loads(body_bytes)
        except ValueError:
            s, e = body_bytes.find(b"{"), body_bytes.rfind(b"}")
            if s >= 0 and e > s:
                try:
                    parsed = json.loads(body_bytes[s:e + 1])
                except ValueError:
                    pass
    return code, parsed, xrid


def _execute_one(payload, poll_seconds, poll_interval, key, secret):
    cid = payload.get("client_order_id", "")
    result = {"client_order_id": cid, "order_id": "", "status": "", "filled_qty": 0.0,
              "filled_avg_price": 0.0, "request_ids": [], "error": ""}
    code, order, rid = _req("POST", "/v2/orders", key, secret, payload)
    if rid:
        result["request_ids"].append(rid)
    if code not in (200, 201) or not order:
        result["status"] = "rejected" if code in (403, 422) else "error"
        result["error"] = f"submit http {code}: {json.dumps(order)[:200] if order else ''}"
        return result
    oid = order.get("id", "")
    result["order_id"] = oid
    result["status"] = str(order.get("status", ""))

    deadline = time.time() + poll_seconds
    while time.time() < deadline:
        if result["status"] in _TERMINAL:
            break
        time.sleep(poll_interval)
        c2, cur, rid2 = _req(
            "GET", f"/v2/orders:by_client_order_id?client_order_id={cid}", key, secret)
        if rid2:
            result["request_ids"].append(rid2)
        _absorb_snapshot(result, c2, cur)

    if result["status"] not in _TERMINAL and oid:
        # Cancel is async and may race a late fill (a DELETE can 422 precisely
        # because the order just filled). Never derive the final status from the
        # stale pre-cancel snapshot — re-read the real terminal state after DELETE
        # so the sealed receipt can never disagree with the account.
        try:
            _, _, rid3 = _req("DELETE", f"/v2/orders/{oid}", key, secret)
            if rid3:
                result["request_ids"].append(rid3)
        except Exception as exc:  # a raise must not corrupt the record
            result["error"] = (result["error"] + f"; cancel: {exc!r}").strip("; ")
        try:
            c4, fin, rid4 = _req(
                "GET", f"/v2/orders:by_client_order_id?client_order_id={cid}", key, secret)
            if rid4:
                result["request_ids"].append(rid4)
            _absorb_snapshot(result, c4, fin)
        except Exception as exc:
            result["status"] = "unknown_working"  # ambiguous — never claim canceled
            result["error"] = (result["error"] + f"; verify: {exc!r}").strip("; ")
        if result["status"] not in _TERMINAL:
            result["status"] = "canceled" if result["filled_qty"] == 0 else "partially_filled"
    return result


def _absorb_snapshot(result, code, snap) -> None:
    """Fold a GET-order response into result — ONLY when it is a real order body
    (HTTP 200 + an id), and keep filled_qty monotonic so a transient error/stale
    read (404/429/5xx JSON envelopes are truthy but carry no filled_qty) can never
    erase an observed fill."""
    if code != 200 or not snap or not snap.get("id"):
        return
    result["status"] = str(snap.get("status", result["status"]))
    nq = float(snap.get("filled_qty") or 0)
    if nq >= result["filled_qty"]:
        result["filled_qty"] = nq
        result["filled_avg_price"] = float(snap.get("filled_avg_price") or 0)


def main() -> int:
    key = os.environ.get("ALPACA_API_KEY", "")
    secret = os.environ.get("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        print("EXEC_FAIL: no creds", file=sys.stderr)
        return 2
    if not os.path.exists(SOCK):
        print(f"EXEC_FAIL: no egress socket at {SOCK}", file=sys.stderr)
        return 3
    try:
        spec = json.load(open("/work/orders.json"))
    except (OSError, ValueError) as exc:
        print(f"EXEC_FAIL: bad orders.json: {exc}", file=sys.stderr)
        return 4

    poll_seconds = float(spec.get("poll_seconds", 8))
    poll_interval = float(spec.get("poll_interval", 1.5))
    results = []
    for payload in spec.get("orders", []):
        try:
            results.append(_execute_one(payload, poll_seconds, poll_interval, key, secret))
        except Exception as exc:  # one bad order must not lose the rest
            results.append({"client_order_id": payload.get("client_order_id", ""),
                            "status": "error", "error": repr(exc), "request_ids": [],
                            "filled_qty": 0.0, "filled_avg_price": 0.0, "order_id": ""})

    with open("/work/results.json", "w") as fh:
        json.dump({"results": results}, fh)
    summary = " | ".join(f"{r['client_order_id'][:16]}:{r['status']}" for r in results)
    print(f"EXEC_OK {len(results)} order(s) | {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
