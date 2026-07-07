"""A minimal RFC 6455 WebSocket client, standard library only.

Covers exactly what the realtime transcription session needs: TLS connect,
the upgrade handshake, masked text frames out, text frames in (including
fragmented ones), ping/pong, and clean close. Not a general-purpose client.
"""

from __future__ import annotations

import base64
import os
import socket
import ssl
import struct
import threading
from urllib.parse import urlparse

_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WsError(RuntimeError):
    pass


class WsClosed(WsError):
    pass


class WebSocket:
    def __init__(self, url: str, headers: dict[str, str], timeout: float = 30.0) -> None:
        u = urlparse(url)
        if u.scheme != "wss":
            raise WsError(f"only wss:// is supported, got {u.scheme}")
        host, port = u.hostname or "", u.port or 443
        raw = socket.create_connection((host, port), timeout=timeout)
        ctx = ssl.create_default_context()
        self.sock = ctx.wrap_socket(raw, server_hostname=host)
        self.sock.settimeout(timeout)
        self._send_lock = threading.Lock()
        self._buf = b""

        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path + (f"?{u.query}" if u.query else "")
        lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {host}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        lines += [f"{k}: {v}" for k, v in headers.items()]
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())

        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WsError("connection closed during handshake")
            response += chunk
        head, _, rest = response.partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0].decode(errors="replace")
        if " 101 " not in f"{status} ":
            body = rest[:500].decode(errors="replace")
            raise WsError(f"handshake failed: {status} {body}")
        self._buf = rest

    # --- receiving --------------------------------------------------------

    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WsClosed("connection closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _read_frame(self) -> tuple[int, bytes]:
        b1, b2 = self._read_exact(2)
        fin, opcode = b1 & 0x80, b1 & 0x0F
        length = b2 & 0x7F
        if length == 126:
            (length,) = struct.unpack(">H", self._read_exact(2))
        elif length == 127:
            (length,) = struct.unpack(">Q", self._read_exact(8))
        if b2 & 0x80:  # masked server frame: not expected, but handle it
            mask = self._read_exact(4)
            data = bytes(c ^ mask[i % 4] for i, c in enumerate(self._read_exact(length)))
        else:
            data = self._read_exact(length)
        return (opcode if fin else -opcode), data

    def recv_text(self) -> str:
        """Next complete text message; transparently answers pings."""
        message = b""
        fragmented = False
        while True:
            opcode, data = self._read_frame()
            fin, op = opcode > 0, abs(opcode)
            if op == 0x9:  # ping
                self._send_frame(0xA, data)
                continue
            if op == 0xA:  # pong
                continue
            if op == 0x8:
                raise WsClosed(f"server closed: {data[2:].decode(errors='replace')}")
            if op in (0x1, 0x2) or (op == 0x0 and fragmented):
                message += data
                fragmented = not fin
                if fin:
                    return message.decode(errors="replace")
            else:
                raise WsError(f"unexpected frame opcode {op}")

    # --- sending -----------------------------------------------------------

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        masked = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        with self._send_lock:
            self.sock.sendall(header + mask + masked)

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode())

    def close(self) -> None:
        try:
            self._send_frame(0x8, struct.pack(">H", 1000))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
