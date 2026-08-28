"""httpx transport that routes HTTPS through bulla's in-cell egress tunnel.

This is the cell side of the "sealed live trading" killer feature. Inside a
bulla cell there is NO network namespace access — the only conduit to the
outside is the mediated egress broker's Unix socket at
``/work/.bulla/egress.sock``. This transport makes httpx (and therefore the
whole Alpaca stack) speak to that socket:

  1. connect the Unix socket,
  2. send one line ``CONNECT <host> <port>\n``,
  3. read ``OK\n`` (or a DENIED/ERROR line),
  4. wrap the socket in TLS with SNI = host — **TLS terminates here, in the
     cell**, so API keys in the Authorization headers never reach the broker in
     plaintext; the broker only ever relays (and hashes) ciphertext,
  5. speak HTTP/1.1 with ``Connection: close`` (one request per tunnel, so the
     broker logs exactly one hash-chained call per API request).

The broker allowlists ``(host, port)``, pins the resolved IP (anti-rebinding),
blocks internal targets, and folds the per-direction byte hashes into the
signed receipt's egress chain — while the seal stays HELD (netns still empty).

Only HTTPS on the allowlisted hosts is supported; anything else raises. This
transport is used ONLY when running inside a cell with EGRESS_SOCK set; outside
a cell the agent uses ordinary httpx.
"""
from __future__ import annotations

import os
import socket
import ssl
from typing import Optional

import httpx

DEFAULT_SOCK = "/work/.bulla/egress.sock"
_CONNECT_TIMEOUT_S = 10.0
_IO_TIMEOUT_S = 30.0


class SealError(RuntimeError):
    """The broker refused or failed the tunnel (DENIED/ERROR/allowlist)."""


def egress_socket_path() -> Optional[str]:
    """The broker socket if we are running inside a tunnel-enabled cell, else None."""
    path = os.environ.get("QUAESTOR_EGRESS_SOCK", DEFAULT_SOCK)
    return path if path and os.path.exists(path) else None


def _open_tunnel(host: str, port: int, sock_path: str) -> ssl.SSLSocket:
    """Open one CONNECT tunnel through the broker and hand back a TLS socket."""
    raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    raw.settimeout(_CONNECT_TIMEOUT_S)
    raw.connect(sock_path)
    raw.sendall(f"CONNECT {host} {port}\n".encode("ascii"))

    # Read the single status line (bytes up to the newline) without consuming
    # any TLS bytes — the broker sends TLS only after our OK, and we send TLS
    # only after reading it, so there is nothing to over-read.
    status = bytearray()
    while not status.endswith(b"\n"):
        chunk = raw.recv(1)
        if not chunk:
            raise SealError("broker closed before status line")
        status += chunk
        if len(status) > 256:
            raise SealError("broker status line too long")
    line = status.decode("ascii", "replace").strip()
    if line != "OK":
        raw.close()
        raise SealError(f"broker refused tunnel to {host}:{port}: {line!r}")

    raw.settimeout(_IO_TIMEOUT_S)
    ctx = ssl.create_default_context()
    return ctx.wrap_socket(raw, server_hostname=host)


class SealedHTTPTransport(httpx.BaseTransport):
    """A blocking httpx transport that tunnels every request through the broker.

    One request == one tunnel == one hash-chained broker call. Forces HTTP/1.1
    with Connection: close so the exchange closes the response and the tunnel.
    """

    def __init__(self, sock_path: Optional[str] = None) -> None:
        self._sock_path = sock_path or egress_socket_path() or DEFAULT_SOCK

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.scheme != "https":
            raise SealError(f"sealed transport is HTTPS-only, got {url.scheme}")
        host = url.host
        port = url.port or 443

        tls = _open_tunnel(host, port, self._sock_path)
        try:
            body = request.content or b""
            lines = [f"{request.method} {url.raw_path.decode('ascii')} HTTP/1.1"]
            headers = dict(request.headers)
            headers["host"] = url.netloc.decode("ascii")
            headers["connection"] = "close"
            headers.setdefault("accept-encoding", "identity")
            if body:
                headers["content-length"] = str(len(body))
            for key, value in headers.items():
                lines.append(f"{key}: {value}")
            raw_req = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body
            tls.sendall(raw_req)

            raw_resp = bytearray()
            while True:
                chunk = tls.recv(65536)
                if not chunk:
                    break
                raw_resp += chunk
        finally:
            try:
                tls.close()
            except OSError:
                pass

        status_code, resp_headers, resp_body = _parse_http_response(bytes(raw_resp))
        return httpx.Response(
            status_code=status_code,
            headers=resp_headers,
            content=resp_body,
            request=request,
        )


def _parse_http_response(raw: bytes) -> tuple[int, list[tuple[str, str]], bytes]:
    """Minimal HTTP/1.1 response parser (status line + headers + body).

    Handles Content-Length and chunked transfer-encoding — enough for Alpaca's
    JSON REST responses received over a Connection: close socket."""
    sep = raw.find(b"\r\n\r\n")
    if sep < 0:
        raise SealError("malformed HTTP response (no header terminator)")
    head = raw[:sep].decode("latin-1")
    body = raw[sep + 4:]
    head_lines = head.split("\r\n")
    status_line = head_lines[0]
    parts = status_line.split(" ", 2)
    if len(parts) < 2 or not parts[1].isdigit():
        raise SealError(f"malformed status line: {status_line!r}")
    status_code = int(parts[1])

    headers: list[tuple[str, str]] = []
    chunked = False
    for line in head_lines[1:]:
        if ":" not in line:
            continue
        name, _, value = line.partition(":")
        name = name.strip()
        value = value.strip()
        headers.append((name, value))
        if name.lower() == "transfer-encoding" and "chunked" in value.lower():
            chunked = True

    if chunked:
        body = _dechunk(body)
    # Drop hop-by-hop headers that no longer describe the decoded body.
    headers = [
        (n, v) for (n, v) in headers
        if n.lower() not in ("transfer-encoding", "content-length", "connection")
    ]
    headers.append(("content-length", str(len(body))))
    return status_code, headers, body


def _dechunk(body: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(body):
        nl = body.find(b"\r\n", i)
        if nl < 0:
            break
        size_str = body[i:nl].split(b";", 1)[0].strip()
        try:
            size = int(size_str, 16)
        except ValueError:
            break
        if size == 0:
            break
        start = nl + 2
        out += body[start:start + size]
        i = start + size + 2  # skip the chunk's trailing CRLF
    return bytes(out)
