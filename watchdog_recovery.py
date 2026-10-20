#!/usr/bin/env python3
"""Recover rate-limited turns outside Stop hooks, using the local Codex daemon.

Run with --daemon, or let the installed SessionStart hook launch it. Only loaded,
enabled, directly addressable root threads are observed. Existing failed turns
are baselined on first sight, so starting this process never revives old work.
"""
import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time

import watchdog as wd
from watchdog_rpc import CodexClient, RpcError

THROTTLE = re.compile(
    r"rate[ _-]?limit(?:[ _-]?exceeded)?|too many requests|"
    r"requests have exceeded the throughput limit", re.I)
PERMANENT = re.compile(
    r"insufficient[_ ]quota|(?:usage|spending|billing|credit|budget) limit|"
    r"quota (?:exceeded|exhausted)|credits? (?:exhausted|depleted)|"
    r"invalid api key|unauthorized|authentication", re.I)
RETRY_AFTER = re.compile(
    r"(?:retry[- ]after\s*[:=]?|(?:try again|retry) (?:in|after))\s*"
    r"(\d+(?:\.\d+)?)\s*(milliseconds?|ms|seconds?|secs?|s|minutes?|mins?|m)?\b", re.I)
PROMPT = (
    "[watchdog] The previous turn failed because of a temporary provider rate "
    "limit. The cooldown has elapsed. Continue the same unfinished task from "
    "its existing progress. First inspect any running command and saved output; "
    "do not repeat completed or still-running operations. Respect newer user "
    "instructions and stop if the task is complete or needs user input."
)


def rate_limited(turn):
    if turn.get("status") != "failed":
        return False
    error = turn.get("error") or {}
    if not isinstance(error, dict):
        return False
    info = error.get("codexErrorInfo")
    text = str(error.get("message") or "") + " " + str(error.get("additionalDetails") or "")
    if PERMANENT.search(text):
        return False
    if isinstance(info, str) and info not in ("rateLimitExceeded", "other"):
        return False
    if info == "rateLimitExceeded":
        return True
    if isinstance(info, dict):
        codes = [v.get("httpStatusCode") for v in info.values() if isinstance(v, dict)]
        if any(code is not None and code != 429 for code in codes):
            return False
        if 429 in codes:
            return True
    return bool(THROTTLE.search(text))


def retry_delay(turn, attempts, base=30, ceiling=300):
    delay = min(ceiling, base * (2 ** min(attempts, 20)))
    error = turn.get("error") or {}
    text = str(error.get("message") or "") + " " + str(error.get("additionalDetails") or "")
    for match in RETRY_AFTER.finditer(text):
        value = float(match[1])
        unit = (match[2] or "s").lower()
        value *= 0.001 if unit.startswith("ms") or unit.startswith("millisecond") else (
            60 if unit.startswith("m") else 1)
        delay = max(delay, value)
    return delay


def snapshot(client, sid):
    thread = client.call("thread/read", {"threadId": sid})["thread"]
    # Don't inspect child agents or change their controlling agent's decisions.
    if thread.get("parentThreadId") or thread.get("canAcceptDirectInput") is not True:
        return thread, {}
    if thread.get("status", {}).get("type") == "notLoaded":
        return thread, {}
    result = client.call("thread/turns/list", {
        "threadId": sid, "limit": 1, "sortDirection": "desc", "itemsView": "notLoaded",
    })
    rows = result.get("data") or []
    return thread, rows[0] if rows else {}


class Recovery:
    def __init__(self, records=None, persist=None, enabled=None, base=30,
                 ceiling=300, maximum=6):
        self.records = records if records is not None else {}
        self.persist = persist or (lambda: None)
        self.enabled = enabled or (lambda thread: wd.session_activated(
            {"session_id": thread["id"], "cwd": thread.get("cwd", "")},
            wd.load_state(thread["id"])))
        self.base, self.ceiling, self.maximum = base, ceiling, maximum

    def save(self):
        if self.persist() is False:
            raise OSError("cannot persist recovery state")

    def observe(self, client, thread, turn, now):
        sid, tid = thread["id"], turn.get("id")
        record = self.records.setdefault(sid, {})
        record["seen_at"] = now
        if not self.enabled(thread) or not tid:
            record.pop("due", None)
            return
        if not record.get("turn"):
            record.update(turn=tid, attempts=0)
            # Never resume a historical failure merely because we started up.
            if turn.get("status") == "failed":
                record["handled"] = tid
        elif record["turn"] != tid:
            attempts = record.get("attempts", 0) if tid == record.get("retry_turn") else 0
            record.clear()
            record.update(turn=tid, attempts=attempts, seen_at=now)
        if thread.get("status", {}).get("type") not in ("idle", "systemError"):
            record.pop("due", None)
            return
        if not rate_limited(turn) or record.get("handled") == tid:
            record.pop("due", None)
            return
        attempts = record.get("attempts", 0)
        if attempts >= self.maximum:
            record["handled"] = tid
            record.pop("due", None)
            wd.log(f"{sid} recovery exhausted after {attempts} retries")
            self.save()
            return
        if "due" not in record:
            delay = retry_delay(turn, attempts, self.base, self.ceiling)
            record["due"] = now + delay
            wd.log(f"{sid} recovery scheduled #{attempts + 1} in {delay:g}s (rate limit)")
            self.save()
            return
        if now < record["due"]:
            return
        # A user or another client may have continued during the cooldown.
        current, latest = snapshot(client, sid)
        queue = client.call("thread/queue/list", {"threadId": sid})
        if (not self.enabled(current) or current.get("canAcceptDirectInput") is not True
                or current.get("status", {}).get("type") not in ("idle", "systemError")
                or latest.get("id") != tid or not rate_limited(latest)
                or queue.get("data") or queue.get("nextCursor")):
            record["handled"] = tid
            record.pop("due", None)
            wd.log(f"{sid} recovery cancelled (session changed or user input queued)")
            self.save()
            return
        # Persist BEFORE sending. An uncertain network result must never cause
        # this same failed turn to receive a second automatic submission.
        record.update(handled=tid, attempts=attempts + 1)
        record.pop("due", None)
        self.save()
        result = client.call("turn/start", {
            "threadId": sid,
            "input": [{"type": "text", "text": PROMPT, "text_elements": []}],
        })
        record["retry_turn"] = result["turn"]["id"]
        self.save()
        wd.log(f"{sid} recovery sent #{attempts + 1}")


def socket_path():
    return os.environ.get("CODEX_WATCHDOG_SOCKET") or os.path.join(
        os.path.expanduser(os.environ.get("CODEX_HOME", "~/.codex")),
        "app-server-control", "app-server-control.sock")


def recovery_enabled():
    return (os.name == "posix" and wd.ADAPTER_NAME == "codex"
            and os.environ.get("CODEX_WATCHDOG") != "0"
            and os.environ.get("CODEX_WATCHDOG_RECOVERY") != "0"
            and not os.path.exists(os.path.join(wd.STATE_DIR, "recovery.disabled")))


def recovery_paths():
    # Separate custom CODEX_HOME installations can have separate daemons.
    key = hashlib.sha256(os.path.abspath(socket_path()).encode()).hexdigest()[:16]
    root = os.path.join(wd.STATE_DIR, "recovery")
    wd._ensure_private_dir(root)
    return os.path.join(root, key + ".lock"), os.path.join(root, key + ".json")


def ensure_background():
    """Detach quickly; the worker holds a singleton lock and owns no hook pipes."""
    if not recovery_enabled() or not os.path.exists(socket_path()):
        return
    import fcntl
    lock_path, _ = recovery_paths()
    with open(lock_path, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        # Pass the acquired lock across exec to avoid concurrent hook launches.
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--daemon", "--lock-fd", str(lock.fileno())],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, pass_fds=(lock.fileno(),),
        )


def run(lock_fd=None):
    if not recovery_enabled():
        return
    import fcntl
    lock_path, path = recovery_paths()
    lock = os.fdopen(lock_fd, "a") if lock_fd is not None else open(
        lock_path, "a")
    with lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        try:
            with open(path, encoding="utf-8") as f:
                records = json.load(f)
            if not isinstance(records, dict):
                records = {}
            records = {k: v for k, v in records.items() if isinstance(v, dict)
                       and all(isinstance(v.get(key, 0), (int, float))
                               and math.isfinite(v.get(key, 0)) and v.get(key, 0) >= 0
                               for key in ("seen_at", "attempts", "due"))}
        except (OSError, ValueError):
            records = {}
        persist = lambda: wd._atomic_json_write(path, records)
        recovery = Recovery(records, persist,
                            base=wd._env_float("CODEX_WATCHDOG_RETRY_BASE", 30),
                            ceiling=wd._env_float("CODEX_WATCHDOG_RETRY_CEILING", 300),
                            maximum=wd._env_int("CODEX_WATCHDOG_RETRY_MAX", 6))
        if not math.isfinite(recovery.base):
            recovery.base = 30
        if not math.isfinite(recovery.ceiling):
            recovery.ceiling = 300
        wd.log("recovery supervisor started (local Codex daemon)")
        client = None
        try:
            while recovery_enabled() and os.path.exists(os.path.abspath(__file__)):
                try:
                    if client is None:
                        client = CodexClient(socket_path())
                    ids, cursor = [], None
                    while True:
                        page = client.call("thread/loaded/list", {"limit": 100, "cursor": cursor})
                        ids.extend(page.get("data", []))
                        cursor = page.get("nextCursor")
                        if not cursor:
                            break
                    for sid in ids:
                        try:
                            thread, turn = snapshot(client, sid)
                            recovery.observe(client, thread, turn, time.time())
                        except RpcError as exc:
                            # Legacy/unavailable threads don't support every
                            # method. Never fall back to resuming them elsewhere.
                            if exc.code not in (-32600, -32601):
                                wd.log(f"{sid} recovery RPC rejected: {exc}")
                            continue
                    for sid in list(records):
                        if sid not in ids or records[sid].get("seen_at", 0) < time.time() - wd.STATE_TTL_SEC:
                            del records[sid]
                    persist()
                    time.sleep(5)
                except (OSError, ValueError, RpcError, KeyError, TypeError):
                    if client is not None:
                        client.close()
                        client = None
                    wd.log("recovery control socket unavailable; retrying in 30s")
                    time.sleep(30)
        finally:
            if client is not None:
                client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--daemon", action="store_true", help="run the recovery supervisor")
    parser.add_argument("--start", action="store_true", help="start in the background if needed")
    parser.add_argument("--disable", action="store_true", help="disable recovery, including a running supervisor")
    parser.add_argument("--enable", action="store_true", help="re-enable recovery and start if available")
    parser.add_argument("--lock-fd", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.disable:
        wd._ensure_private_dir(wd.STATE_DIR)
        with open(os.path.join(wd.STATE_DIR, "recovery.disabled"), "a"):
            pass
    elif args.enable:
        try:
            os.remove(os.path.join(wd.STATE_DIR, "recovery.disabled"))
        except FileNotFoundError:
            pass
        ensure_background()
    elif args.start:
        ensure_background()
    elif args.daemon:
        run(args.lock_fd)
    else:
        parser.print_help()
