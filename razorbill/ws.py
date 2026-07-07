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
        self._fragments = b""

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

    def set_timeout(self, seconds: float) -> None:
        """Receive timeout after the handshake; short values make recv_text
        double as a frequent tick for callers that poll between messages."""
        self.sock.settimeout(seconds)

    # --- receiving --------------------------------------------------------

    def _fill(self, n: int) -> None:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WsClosed("connection closed")
            self._buf += chunk

    def _read_frame(self) -> tuple[int, bytes]:
        """Parse one frame, consuming nothing until it is fully buffered, so
        a receive timeout mid-frame leaves the stream intact for retry."""
        self._fill(2)
        b1, b2 = self._buf[0], self._buf[1]
        length = b2 & 0x7F
        header = 2
        if length == 126:
            self._fill(4)
            (length,) = struct.unpack(">H", self._buf[2:4])
            header = 4
        elif length == 127:
            self._fill(10)
            (length,) = struct.unpack(">Q", self._buf[2:10])
            header = 10
        if b2 & 0x80:  # masked server frame: not expected, but handle it
            self._fill(header + 4 + length)
            mask = self._buf[header:header + 4]
            raw = self._buf[header + 4:header + 4 + length]
            data = bytes(c ^ mask[i % 4] for i, c in enumerate(raw))
            self._buf = self._buf[header + 4 + length:]
        else:
            self._fill(header + length)
            data = self._buf[header:header + length]
            self._buf = self._buf[header + length:]
        fin, opcode = b1 & 0x80, b1 & 0x0F
        return (opcode if fin else -opcode), data

    def recv_text(self) -> str:
        """Next complete text message; transparently answers pings.

        Fragment state lives on the instance, so a timeout between the
        fragments of one message resumes cleanly on the next call.
        """
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
            if op in (0x1, 0x2) or (op == 0x0 and self._fragments):
                self._fragments += data
                if fin:
                    message, self._fragments = self._fragments, b""
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
