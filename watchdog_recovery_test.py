"""Offline recovery tests: fake clock and a local fake Codex WebSocket server."""
import base64
import copy
import hashlib
import json
import os
import socket
import struct
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import watchdog_recovery as recovery
from watchdog_rpc import CodexClient, RpcError


MESSAGE = ("rate limit exceeded: Requests have exceeded the throughput limit on "
           "your Provisioned-Managed deployment. If you continue to exceed your "
           "limit, consider increasing the number of provisioned throughput units deployed.")


def turn(tid="t1", status="failed", message=MESSAGE, info="rateLimitExceeded"):
    return {"id": tid, "status": status, "items": [],
            "error": {"message": message, "codexErrorInfo": info} if status == "failed" else None}


def thread(status="idle"):
    return {"id": "s1", "cwd": "/tmp", "status": {"type": status},
            "canAcceptDirectInput": True}


class FakeClient:
    def __init__(self):
        self.thread = thread()
        self.turn = turn()
        self.calls = []
        self.queue = []
        self.fail_start = False

    def call(self, method, params):
        self.calls.append((method, params))
        if method == "thread/read":
            return {"thread": copy.deepcopy(self.thread)}
        if method == "thread/turns/list":
            return {"data": [copy.deepcopy(self.turn)]}
        if method == "thread/queue/list":
            return {"data": self.queue}
        if method == "turn/start":
            if self.fail_start:
                raise ConnectionError("response lost after submission")
            return {"turn": turn("retry-" + self.turn["id"], "inProgress")}
        raise AssertionError(method)

    def starts(self):
        return [params for method, params in self.calls if method == "turn/start"]


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.log = patch.object(recovery.wd, "log")
        self.log.start()
        self.addCleanup(self.log.stop)
        self.client = FakeClient()
        self.engine = recovery.Recovery(enabled=lambda t: True)
        self.engine.observe(self.client, thread("active"), turn(status="inProgress"), 0)

    def observe(self, when):
        self.engine.observe(self.client, self.client.thread, self.client.turn, when)

    def test_failure_without_stop_hook_retries_once_after_cooldown(self):
        self.observe(1)
        self.observe(30)
        self.assertEqual(self.client.starts(), [])
        self.observe(31)
        self.observe(100)
        self.assertEqual(len(self.client.starts()), 1)
        params = self.client.starts()[0]
        self.assertEqual(set(params), {"threadId", "input"})
        self.assertIn("still-running operations", params["input"][0]["text"])
        self.assertEqual(self.engine.records["s1"]["attempts"], 1)

    def test_exponential_backoff_and_budget_survive_restart(self):
        clock = 1
        for expected_delay in [30, 60, 120, 240, 300, 300]:
            self.observe(clock)
            self.assertEqual(self.engine.records["s1"]["due"], clock + expected_delay)
            self.engine = recovery.Recovery(copy.deepcopy(self.engine.records), enabled=lambda t: True)
            self.observe(clock + expected_delay)
            self.client.turn = turn("retry-" + self.client.turn["id"])
            clock += expected_delay + 1
        self.observe(clock)
        self.observe(clock + 10000)
        self.assertEqual(len(self.client.starts()), 6)
        self.assertEqual(self.engine.records["s1"]["handled"], self.client.turn["id"])

    def test_new_user_turn_resets_budget(self):
        self.observe(1)
        self.observe(31)
        self.client.turn = turn("new-user", "inProgress")
        self.client.thread = thread("active")
        self.observe(40)
        self.assertEqual(self.engine.records["s1"]["attempts"], 0)
        self.client.turn = turn("new-user")
        self.client.thread = thread()
        self.observe(50)
        self.assertEqual(self.engine.records["s1"]["due"], 80)

    def test_initial_historical_failure_is_not_resumed(self):
        engine = recovery.Recovery(enabled=lambda t: True)
        for when in [0, 100, 1000]:
            engine.observe(self.client, thread(), turn(), when)
        self.assertFalse(self.client.starts())

    def test_final_recheck_cancels_changed_turn_or_active_session(self):
        for change in ("active", "new-turn", "interrupted", "queued", "disabled"):
            with self.subTest(change=change):
                self.client = FakeClient()
                self.engine = recovery.Recovery(enabled=lambda t: True)
                self.engine.observe(self.client, thread("active"), turn(status="inProgress"), 0)
                self.observe(1)
                old_thread, old_turn = thread(), turn()
                if change == "active":
                    self.client.thread = thread("active")
                elif change == "new-turn":
                    self.client.turn = turn("new")
                elif change == "interrupted":
                    self.client.turn = turn(status="interrupted")
                elif change == "queued":
                    self.client.queue = [{"id": "user-input"}]
                else:
                    self.client.thread["canAcceptDirectInput"] = False
                self.engine.observe(self.client, old_thread, old_turn, 31)
                self.assertFalse(self.client.starts())
                self.assertNotIn("due", self.engine.records["s1"])

    def test_disabled_during_cooldown(self):
        self.observe(1)
        self.engine.enabled = lambda t: False
        self.observe(31)
        self.assertFalse(self.client.starts())

    def test_failed_persistence_prevents_submission(self):
        self.observe(1)
        self.engine.persist = lambda: False
        with self.assertRaises(OSError):
            self.observe(31)
        self.assertFalse(self.client.starts())

    def test_lost_response_is_not_submitted_twice(self):
        self.observe(1)
        self.client.fail_start = True
        with self.assertRaises(ConnectionError):
            self.observe(31)
        self.engine = recovery.Recovery(copy.deepcopy(self.engine.records), enabled=lambda t: True)
        self.observe(100)
        self.assertEqual(len(self.client.starts()), 1)

    def test_classification_uses_failed_turn_not_tool_or_assistant_text(self):
        for info in ["rateLimitExceeded", "other", None,
                     {"responseTooManyFailedAttempts": {"httpStatusCode": 429}}]:
            with self.subTest(info=info):
                self.assertTrue(recovery.rate_limited(turn(info=info)))
        for status in ["inProgress", "completed", "interrupted"]:
            self.assertFalse(recovery.rate_limited(turn(status=status)))
        for info in ["usageLimitExceeded", "unauthorized", "sessionBudgetExceeded",
                     {"httpConnectionFailed": {"httpStatusCode": 401}}]:
            self.assertFalse(recovery.rate_limited(turn(info=info)))
        for message in ["rate limit exceeded: insufficient_quota", "billing limit reached",
                        "API key authentication failed", "quota exhausted"]:
            self.assertFalse(recovery.rate_limited(turn(message=message)))
        self.assertFalse(recovery.rate_limited(turn(message="unit test failed", info="other")))

    def test_retry_after_is_minimum_even_above_ceiling(self):
        for text, delay in [("Retry-After: 900", 900), ("retry in 2 minutes", 120),
                            ("retry after 45000 ms", 45), ("try again in 10s", 30)]:
            with self.subTest(text=text):
                self.assertEqual(recovery.retry_delay(turn(message=text), 0), delay)

    def test_snapshot_skips_child_or_unloaded_threads(self):
        for field, value in [("parentThreadId", "parent"), ("canAcceptDirectInput", False),
                             ("status", {"type": "notLoaded"})]:
            client = FakeClient()
            client.thread[field] = value
            _, latest = recovery.snapshot(client, "s1")
            self.assertEqual(latest, {})
            self.assertEqual(len(client.calls), 1)

    def test_disabled_launcher_does_not_create_state_or_process(self):
        with patch.dict(os.environ, {"CODEX_WATCHDOG_RECOVERY": "0"}), \
                patch.object(recovery.subprocess, "Popen") as popen, \
                patch.object(recovery, "recovery_paths") as paths:
            recovery.ensure_background()
            popen.assert_not_called()
            paths.assert_not_called()

    def test_session_start_launches_recovery_without_changing_hook_output(self):
        import io
        event = {"hook_event_name": "SessionStart", "session_id": "s1"}
        with patch.object(recovery.wd.sys, "stdin", io.StringIO(json.dumps(event))), \
                patch.object(recovery.wd.sys, "stdout", io.StringIO()) as output, \
                patch.object(recovery.wd, "session_activated", return_value=True), \
                patch.object(recovery.wd, "load_state", return_value={}), \
                patch.object(recovery.wd, "emit_resume_context") as resume, \
                patch.object(recovery, "ensure_background") as launch:
            recovery.wd.main()
            launch.assert_called_once()
            resume.assert_called_once_with(event, "s1")
            self.assertEqual(output.getvalue(), "")

    def test_installer_copies_recovery_and_preserves_unrelated_hooks(self):
        with tempfile.TemporaryDirectory(prefix="wd-install-") as root:
            dest = os.path.join(root, "custom codex")
            os.mkdir(dest)
            config = os.path.join(dest, "hooks.json")
            unrelated = {"hooks": [{"type": "command", "command": "echo unrelated"}]}
            with open(config, "w") as f:
                json.dump({"hooks": {"SessionStart": [unrelated]}}, f)
            env = dict(os.environ, CODEX_HOME=dest, CODEX_WATCHDOG_RECOVERY="0")
            script = os.path.join(os.path.dirname(__file__), "install.sh")
            for _ in range(2):
                subprocess.run(["bash", script], env=env, check=True, capture_output=True)
            with open(config) as f:
                hooks = json.load(f)["hooks"]
            self.assertIn(unrelated, hooks["SessionStart"])
            self.assertEqual(len(hooks["SessionStart"]), 2)
            for name in ["watchdog_recovery.py", "watchdog_rpc.py"]:
                self.assertTrue(os.path.isfile(os.path.join(dest, name)))
            subprocess.run(["bash", script, "--uninstall"], env=env, check=True, capture_output=True)
            self.assertFalse(os.path.exists(os.path.join(dest, "watchdog_recovery.py")))


def recv_exact(sock, size):
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise EOFError()
        data.extend(chunk)
    return bytes(data)


def read_frame(sock):
    first, second = recv_exact(sock, 2)
    if not second & 128:
        raise AssertionError("client frames must be masked")
    size = second & 127
    if size == 126:
        size = struct.unpack("!H", recv_exact(sock, 2))[0]
    elif size == 127:
        size = struct.unpack("!Q", recv_exact(sock, 8))[0]
    mask = recv_exact(sock, 4)
    data = recv_exact(sock, size)
    return first & 15, bytes(b ^ mask[i % 4] for i, b in enumerate(data))


def frame(data, opcode=1, final=True):
    size = len(data)
    first = opcode | (128 if final else 0)
    if size < 126:
        return bytes([first, size]) + data
    if size < 65536:
        return bytes([first, 126]) + struct.pack("!H", size) + data
    return bytes([first, 127]) + struct.pack("!Q", size) + data


@unittest.skipUnless(hasattr(socket, "AF_UNIX"), "Unix control socket required")
class TransportTests(unittest.TestCase):
    def test_real_unix_websocket_recovery_without_a_stop_event(self):
        fake = FakeClient()
        errors, received = [], []
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "control.sock")
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(listener.close)
            listener.bind(path)
            listener.listen(1)

            def serve():
                try:
                    conn, _ = listener.accept()
                    with conn:
                        conn.settimeout(3)
                        header = b""
                        while not header.endswith(b"\r\n\r\n"):
                            header += recv_exact(conn, 1)
                        fields = dict(line.split(b": ", 1) for line in header.split(b"\r\n") if b": " in line)
                        accept = base64.b64encode(hashlib.sha1(fields[b"Sec-WebSocket-Key"] +
                            b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
                        conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                                     b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")
                        while True:
                            opcode, body = read_frame(conn)
                            if opcode == 10:
                                self.assertEqual(body, b"ping")
                                continue
                            request = json.loads(body)
                            received.append(request)
                            if "id" not in request:
                                continue
                            result = {} if request["method"] == "initialize" else fake.call(
                                request["method"], request["params"])
                            data = json.dumps({"id": request["id"], "result": result}).encode()
                            # Fragmented messages with interleaved ping and unrelated notification.
                            conn.sendall(frame(b'{"method":"ignored"}') +
                                         frame(data[:10], final=False) + frame(b"ping", opcode=9) +
                                         frame(data[10:], opcode=0))
                except EOFError:
                    pass
                except Exception as exc:
                    errors.append(exc)

            server = threading.Thread(target=serve, daemon=True)
            server.start()
            client = CodexClient(path, timeout=3)
            try:
                with patch.object(recovery.wd, "log"):
                    engine = recovery.Recovery(enabled=lambda t: True)
                    engine.observe(client, thread("active"), turn(status="inProgress"), 0)
                    engine.observe(client, thread(), turn(), 1)
                    engine.observe(client, thread(), turn(), 31)
                self.assertEqual(len(fake.starts()), 1)
            finally:
                client.close()
            server.join(4)
            self.assertFalse(server.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(received[0]["method"], "initialize")
            self.assertEqual(received[1]["method"], "initialized")

    def test_transport_handles_extended_lengths_and_rejects_oversize(self):
        for size in [300, 70000]:
            left, right = socket.socketpair()
            with left, right:
                client = CodexClient.__new__(CodexClient)
                client.sock = left
                body = b"x" * size
                writer = threading.Thread(target=lambda: client._send(body), daemon=True)
                writer.start()
                self.assertEqual(read_frame(right), (1, body))
                writer.join(2)
        left, right = socket.socketpair()
        with left, right:
            client = CodexClient.__new__(CodexClient)
            client.sock = left
            right.sendall(bytes([129, 127]) + struct.pack("!Q", CodexClient.MAX_MESSAGE + 1))
            with self.assertRaises(RpcError):
                client._receive(recovery.time.monotonic() + 1)


if __name__ == "__main__":
    unittest.main()
