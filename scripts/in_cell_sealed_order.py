"""Runs INSIDE a bulla cell: a REAL options order lifecycle over the sealed tunnel.

Stronger than in_cell_probe.py — this seals an actual order, not just a read:
  1. GET /v2/account            (confirm paper + options level)
  2. GET /v2/options/contracts  (discover a far-OTM SPY call, won't fill)
  3. POST /v2/orders            (place a 1-lot, deep-OTM, $0.01 limit — non-marketable)
  4. DELETE /v2/orders/{id}     (cancel immediately)

Every request is one CONNECT tunnel == one hash-chained call in the signed
receipt. TLS terminates in-cell (keys never reach the broker). Stdlib only (the
cell has system python3 but not our venv). The order is deliberately unfillable
and cancelled at once — safe to run any time, even on the dev account.

Invoked by scripts/demo_sealed_trade.sh.
"""
import json
import os
import socket
import ssl
import sys
import uuid

SOCK = "/work/.bulla/egress.sock"
TRADING = "paper-api.alpaca.markets"


def _req(host, method, path, key, secret, body=None):
    """One HTTPS request through the sealed tunnel. Returns (status, json|None, raw)."""
    raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    raw.settimeout(20.0)
    raw.connect(SOCK)
    raw.sendall(f"CONNECT {host} 443\n".encode())
    status = b""
    while not status.endswith(b"\n"):
        b = raw.recv(1)
        if not b:
            raise RuntimeError("broker closed before OK")
        status += b
    if status.strip() != b"OK":
        raise RuntimeError(f"broker refused {host}: {status!r}")

    tls = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
    payload = json.dumps(body).encode() if body is not None else b""
    lines = [
        f"{method} {path} HTTP/1.1",
        f"Host: {host}",
        f"APCA-API-KEY-ID: {key}",
        f"APCA-API-SECRET-KEY: {secret}",
        "Accept: application/json",
        "Accept-Encoding: identity",
        "Connection: close",
    ]
    if payload:
        lines.append("Content-Type: application/json")
        lines.append(f"Content-Length: {len(payload)}")
    req = ("\r\n".join(lines) + "\r\n\r\n").encode() + payload
    tls.sendall(req)

    resp = b""
    while True:
        chunk = tls.recv(65536)
        if not chunk:
            break
        resp += chunk
    tls.close()

    sep = resp.find(b"\r\n\r\n")
    head = resp[:sep].decode("latin-1")
    body_bytes = resp[sep + 4:]
    header_lines = head.splitlines()
    code = int(header_lines[0].split(" ")[1])
    chunked = any(
        h.lower().startswith("transfer-encoding") and "chunked" in h.lower()
        for h in header_lines[1:]
    )
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
    return code, parsed, resp


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


def main() -> int:
    key = os.environ.get("ALPACA_API_KEY", "")
    secret = os.environ.get("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        print("SEALED_FAIL: no creds in cell env", file=sys.stderr)
        return 2
    if not os.path.exists(SOCK):
        print(f"SEALED_FAIL: no egress socket at {SOCK}", file=sys.stderr)
        return 3

    out = []

    code, acct, _ = _req(TRADING, "GET", "/v2/account", key, secret)
    if code != 200 or not acct:
        print(f"SEALED_FAIL: account {code}", file=sys.stderr)
        return 4
    out.append(f"account {acct.get('account_number')} equity {acct.get('equity')} "
               f"L{acct.get('options_trading_level')}")

    # Discover a far-OTM SPY call next Friday (deep OTM -> a $0.01 limit never fills).
    code, contracts, _ = _req(
        TRADING, "GET",
        "/v2/options/contracts?underlying_symbols=SPY&type=call"
        "&expiration_date_gte=2026-09-03&expiration_date_lte=2026-09-04&limit=200",
        key, secret,
    )
    rows = (contracts or {}).get("option_contracts", []) if contracts else []
    if not rows:
        print("SEALED_FAIL: no contracts", file=sys.stderr)
        return 5
    far = max(rows, key=lambda c: float(c.get("strike_price", 0)))
    sym = far["symbol"]
    out.append(f"contract {sym} strike {far.get('strike_price')}")

    cid = "sealed-" + uuid.uuid4().hex[:12]
    order_body = {
        "symbol": sym, "qty": "1", "side": "buy", "type": "limit",
        "limit_price": "0.01", "time_in_force": "day",
        "position_intent": "buy_to_open", "client_order_id": cid,
    }
    code, order, _ = _req(TRADING, "POST", "/v2/orders", key, secret, order_body)
    if code not in (200, 201) or not order:
        print(f"SEALED_FAIL: submit {code} {order}", file=sys.stderr)
        return 6
    oid = order.get("id", "")
    out.append(f"ORDER PLACED {sym} id {oid[:8]}… status {order.get('status')}")

    code, canceled, _ = _req(TRADING, "DELETE", f"/v2/orders/{oid}", key, secret)
    out.append(f"ORDER CANCELED http {code}")

    line = "SEALED_OK | " + " | ".join(out)
    print(line)
    try:
        with open("/work/sealed_order_result.txt", "w") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
