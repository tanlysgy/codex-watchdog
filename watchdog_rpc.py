"""Small JSON-RPC client for Codex's local Unix WebSocket control socket.

Only local sockets are supported. No credentials, network listeners, thread
resumes, or model requests are involved in opening this connection.
"""
import base64
import hashlib
import json
import os
import socket
import struct
import time


class RpcError(RuntimeError):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class CodexClient:
    MAX_MESSAGE = 16 * 1024 * 1024

    def __init__(self, path, timeout=10):
        self.timeout = timeout
        self.sequence = 0
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        try:
            self.sock.connect(path)
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            self.sock.sendall((
                "GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                "Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
                f"Sec-WebSocket-Key: {key}\r\n\r\n"
            ).encode("ascii"))
            header = bytearray()
            while not header.endswith(b"\r\n\r\n"):
                header.extend(self._read(1))
                if len(header) > 16384:
                    raise RpcError("oversized WebSocket handshake")
            lines = header.decode("ascii").split("\r\n")
            fields = {k.lower(): v.strip() for k, v in
                      (line.split(":", 1) for line in lines[1:] if ":" in line)}
            accept = base64.b64encode(hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
            ).digest()).decode("ascii")
            if (lines[0].split()[1] != "101"
                    or fields.get("sec-websocket-accept") != accept
                    or fields.get("upgrade", "").lower() != "websocket"):
                raise RpcError("invalid WebSocket handshake")
            self.call("initialize", {
                "clientInfo": {"name": "codex_watchdog", "version": "1.2"},
                "capabilities": {"experimentalApi": True},
            })
            self._send(json.dumps({"method": "initialized"}).encode())
        except Exception:
            self.close()
            raise

    def close(self):
        self.sock.close()

    def _read(self, length):
        data = bytearray()
        while len(data) < length:
            chunk = self.sock.recv(length - len(data))
            if not chunk:
                raise ConnectionError("Codex control socket closed")
            data.extend(chunk)
        return bytes(data)

    def _send(self, data, opcode=1):
        mask = os.urandom(4)
        size = len(data)
        if size < 126:
            header = bytes([0x80 | opcode, 0x80 | size])
        elif size < 65536:
            header = bytes([0x80 | opcode, 0xFE]) + struct.pack("!H", size)
        else:
            header = bytes([0x80 | opcode, 0xFF]) + struct.pack("!Q", size)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(header + mask + masked)

    def _receive(self, deadline):
        message = bytearray()
        fragmented = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Codex RPC timed out")
            self.sock.settimeout(remaining)
            first, second = self._read(2)
            opcode, final = first & 15, bool(first & 128)
            if first & 0x70 or second & 0x80:
                raise RpcError("unsupported WebSocket frame")
            size = second & 127
            if size == 126:
                size = struct.unpack("!H", self._read(2))[0]
            elif size == 127:
                size = struct.unpack("!Q", self._read(8))[0]
            if size + len(message) > self.MAX_MESSAGE:
                raise RpcError("oversized WebSocket message")
            if opcode >= 8 and (not final or size > 125):
                raise RpcError("invalid WebSocket control frame")
            data = self._read(size)
            if opcode == 8:
                raise ConnectionError("Codex closed WebSocket")
            if opcode == 9:
                self._send(data, opcode=10)
                continue
            if opcode == 10:
                continue
            if opcode not in (0, 1) or (opcode == 0) != fragmented:
                raise RpcError("unexpected WebSocket data frame")
            message.extend(data)
            if final:
                return json.loads(message)
            fragmented = True

    def call(self, method, params):
        self.sequence += 1
        request_id = self.sequence
        self.sock.settimeout(self.timeout)
        self._send(json.dumps({"id": request_id, "method": method,
                               "params": params}).encode())
        deadline = time.monotonic() + self.timeout
        while True:
            reply = self._receive(deadline)
            if reply.get("id") != request_id or "method" in reply:
                # This read-only observer doesn't subscribe or answer approvals.
                continue
            if "error" in reply:
                code = reply['error'].get('code')
                raise RpcError(f"{method}: {code}", code=code)
            return reply.get("result", {})
