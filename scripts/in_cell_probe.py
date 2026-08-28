"""Runs INSIDE a bulla cell: proves a real Alpaca call over the sealed tunnel.

Stdlib only (the cell binds the host /usr read-only, so system python3 is
available but our venv is not). Opens a CONNECT tunnel through the broker's
Unix socket, does TLS in-cell (keys never reach the broker in plaintext), calls
GET /v2/account, and prints the account number + equity. The whole exchange is
hash-chained into the signed receipt; the seal stays HELD.

Invoked as:  bulla run --work <cell> --egress-allow paper-api.alpaca.markets:443
             --nondeterministic --out r.json -- python3 /work/in_cell_probe.py
"""
import json
import os
import socket
import ssl
import sys

SOCK = "/work/.bulla/egress.sock"
HOST = "paper-api.alpaca.markets"
PORT = 443


def main() -> int:
    key = os.environ.get("ALPACA_API_KEY", "")
    secret = os.environ.get("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        print("PROBE_FAIL: no ALPACA creds in cell env", file=sys.stderr)
        return 2
    if not os.path.exists(SOCK):
        print(f"PROBE_FAIL: no egress socket at {SOCK}", file=sys.stderr)
        return 3

    raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    raw.settimeout(15.0)
    raw.connect(SOCK)
    raw.sendall(f"CONNECT {HOST} {PORT}\n".encode())

    status = b""
    while not status.endswith(b"\n"):
        b = raw.recv(1)
        if not b:
            print("PROBE_FAIL: broker closed before OK", file=sys.stderr)
            return 4
        status += b
    if status.strip() != b"OK":
        print(f"PROBE_FAIL: broker refused: {status!r}", file=sys.stderr)
        return 5

    ctx = ssl.create_default_context()
    tls = ctx.wrap_socket(raw, server_hostname=HOST)
    req = (
        f"GET /v2/account HTTP/1.1\r\n"
        f"Host: {HOST}\r\n"
        f"APCA-API-KEY-ID: {key}\r\n"
        f"APCA-API-SECRET-KEY: {secret}\r\n"
        f"Accept-Encoding: identity\r\n"
        f"Connection: close\r\n\r\n"
    )
    tls.sendall(req.encode())

    resp = b""
    while True:
        chunk = tls.recv(65536)
        if not chunk:
            break
        resp += chunk
    tls.close()

    sep = resp.find(b"\r\n\r\n")
    head = resp[:sep].decode("latin-1")
    body = resp[sep + 4:]
    status_line = head.splitlines()[0]
    # Body may be chunked; grab the JSON object heuristically.
    start, end = body.find(b"{"), body.rfind(b"}")
    acct = {}
    if start >= 0 and end > start:
        try:
            acct = json.loads(body[start:end + 1])
        except ValueError:
            pass
    line = f"PROBE_OK {status_line} | account {acct.get('account_number','?')} " \
           f"equity {acct.get('equity','?')} options_level {acct.get('options_trading_level','?')}"
    print(line)
    try:  # leave a visible artifact in the cell work dir for the demo
        with open("/work/probe_result.txt", "w") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
