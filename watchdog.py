#!/usr/bin/env python3
"""Codex Watchdog: auto-continue unfinished tasks.

Registered as a Stop hook (Codex) or ClaudeCodeStop (Claude Code). Every time
a turn ends, the agent runs this script. If the task looks unfinished, we tell
the agent to keep going (decision=block); when the task is done, waiting on
the user, or the burst budget is exhausted, we let the turn finish normally.

Agent-agnostic via adapters/:
  - adapters/codex.py  -> Codex JSONL transcripts (default)
  - adapters/claude.py -> Claude Code transcripts
  - adapters/<name>.py -> your own

Select with env CODEX_WATCHDOG_ADAPTER=claude (or codex).

Evidence-driven finish detection:
  1. Declared protocol: message starts with 任务完成/『任务完成』/done -> stop;
     需要用户/『需要用户』/need user -> stop (blocked on user).
  2. Burst budget: N auto-continues with no real user input -> stop.
  3. Repeated identical final message -> stop (no progress).
  4. Short genuine need-user request -> stop.
  5. Tool evidence: current turn actually called tools -> continue.
  6. Quiet turns: 3 tool-less turns in a row -> stop (spinning).
  7. Classic done phrases (on a quiet turn) -> stop.

State: /tmp/codex-watchdog/<session_id>.json
Log:   /tmp/codex-watchdog.log
"""
import json
import os
import re
import sys
import time

# Make `adapters` importable whether run from the repo or from ~/.codex.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from adapters import load_adapter  # noqa: E402

STATE_DIR = "/tmp/codex-watchdog"
LOG_FILE = "/tmp/codex-watchdog.log"
BURST_MAX = int(os.environ.get("CODEX_WATCHDOG_MAX", "60"))
RESET_AFTER_SEC = float(os.environ.get("CODEX_WATCHDOG_RESET", "1800"))
QUIET_TURNS_MAX = int(os.environ.get("CODEX_WATCHDOG_QUIET", "3"))
ADAPTER_NAME = os.environ.get("CODEX_WATCHDOG_ADAPTER", "codex")

try:
    ADAPTER = load_adapter(ADAPTER_NAME)
except Exception as exc:  # pragma: no cover - fallback so hooks never crash
    sys.stderr.write(f"watchdog: failed to load adapter {ADAPTER_NAME!r}: {exc}\n")
    ADAPTER = None

# --- Explicit declarations the model is asked to make ---
DECL_DONE = re.compile(
    r"^\s*[『「【\"''“”]?\s*(任务完成|全部完成|已完成|完成。|done\.|done$|任务全部完成)", re.I | re.M)
DECL_NEED_USER = re.compile(
    r"^\s*[『「【\"''“”]?\s*(需要用户|需要你|need user|needs user|blocked|waiting for you|等待用户)", re.I | re.M)

# --- Classic DONE phrases (fallback when the agent did NOT use the protocol) ---
DONE = re.compile(
    r"(任务(已?全部)?完成|全部完成|已经完成|已完成|完成收尾|全部做好|"
    r"没有更多(工作|任务|事项)|无(需|须)(再|任何)(工作|修改|任务|事项)|"
    r"all done|task (is )?(complete|done)|fully (complete|done)|"
    r"no (more|further|remaining) (work|tasks|steps)|nothing (left|more|else)|"
    r"finished|搞定了|行了|收工|就此结束|到此为止|已完成所有|没有其他工作)",
    re.I,
)

# --- Genuine short requests for user input ---
NEED_USER = re.compile(
    r"(请(您|你)?(提供|输入|确认|决定|告诉我|给出|选择|设置|说明|回复|告知)|"
    r"需要(您|你|用户)?(提供|输入|确认|批准|授权|告知|选择|决定|告诉我)|"
    r"(等待|等)(您|你|用户)(的)?(输入|确认|决定|指示|消息|答复|要求|选择)|"
    r"please (provide|enter|confirm|decide|tell|give|choose|explain)|"
    r"waiting for (your|you|user)|need (your|you|user) (input|confirmation|decision|approval)|"
    r"asked for (your|user) (input|confirmation|decision)|"
    r"needs? (your|user) (input|confirmation|decision|approval))",
    re.I,
)

RHETORICAL = re.compile(r"[吗么呢]+\s*[??!!。]?\s*(不(需要|用|必)|无需|不必|我自己|我来|我可以|我会|算了)")


def log(msg: str) -> None:
    try:
        with open(LOG_FILE, "a") as f:
            f.write(f"{time.strftime('%F %T')} {msg}\n")
    except OSError:
        pass


def now() -> float:
    return time.time()


def load_state(sid: str) -> dict:
    p = os.path.join(STATE_DIR, f"{sid}.json")
    try:
        with open(p) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"count": 0, "last_continue_at": None, "last_msg": None,
                "quiet_turns": 0, "activated": False}


def save_state(sid: str, state: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(os.path.join(STATE_DIR, f"{sid}.json"), "w") as f:
        json.dump(state, f)


def session_activated(ev: dict, state: dict) -> bool:
    if os.environ.get("CODEX_WATCHDOG") == "0":
        return False
    if os.environ.get("CODEX_WATCHDOG") == "1":
        return True
    marker = os.path.expanduser("~/.codex/watchdog.enabled")
    # Marker next to the session cwd works for any agent.
    if os.path.exists(os.path.join(ev.get("cwd", ""), ".codex-watchdog")):
        return True
    if os.environ.get("CODEX_WATCHDOG_ADAPTER") in ("claude",):
        marker = os.path.expanduser("~/.claude/watchdog.enabled")
        if os.path.exists(marker):
            return True
    if ADAPTER is not None and ADAPTER.enabled(ev, state, marker):
        return True
    return False


def last_user_prompt(ev: dict):
    if ADAPTER is None:
        return None
    return ADAPTER.last_user_prompt(ev)


def last_turn_tool_activity(ev: dict):
    if ADAPTER is None:
        return 0, set()
    return ADAPTER.last_turn_tool_activity(ev)


def is_watchdog_inject(text: str) -> bool:
    if ADAPTER is None:
        return "[watchdog]" in (text or "")
    return ADAPTER.is_watchdog_inject(text)


def should_reset_burst(state: dict, ev: dict) -> bool:
    sid = ev.get("session_id") or "unknown"
    last_prompt = last_user_prompt(ev)
    if last_prompt is not None and not is_watchdog_inject(last_prompt):
        if state.get("count", 0) > 0:
            log(f"{sid} last user message is real (not watchdog); burst reset")
        return True
    last_t = state.get("last_continue_at")
    if last_t is not None and (now() - last_t) > RESET_AFTER_SEC:
        log(f"{sid} idle >{int(RESET_AFTER_SEC)}s; burst reset")
        return True
    return False


def main() -> None:
    try:
        ev = json.load(sys.stdin)
    except ValueError:
        return
    hook_event = ev.get("hook_event_name") or ev.get("hookEventName") or ""
    # Accept both Codex ("Stop") and Claude Code ("Stop") event names.
    if hook_event not in ("Stop", "ClaudeCodeStop"):
        return

    sid = ev.get("session_id") or "unknown"
    msg = (ev.get("last_assistant_message") or "").strip()
    state = load_state(sid)

    if not session_activated(ev, state):
        return
    state["activated"] = True

    if should_reset_burst(state, ev):
        state["count"] = 0
        state["last_msg"] = None

    # ---- 1. declared protocol (highest priority) ----
    if DECL_DONE.search(msg):
        log(f"{sid} declared done: {msg[:80]!r}; stopping")
        save_state(sid, state)
        print(json.dumps({"continue": True}))
        return
    if DECL_NEED_USER.search(msg):
        log(f"{sid} declared need-user: {msg[:80]!r}; stopping")
        save_state(sid, state)
        print(json.dumps({"continue": True}))
        return

    # ---- 2. burst guard ----
    if state.get("count", 0) >= BURST_MAX:
        log(f"{sid} burst exhausted ({BURST_MAX} auto-continues, no user input); stopping")
        save_state(sid, state)
        print(json.dumps({"continue": True}))
        return

    # ---- 3. repeated final message ----
    if state.get("last_msg") and msg and msg == state["last_msg"]:
        log(f"{sid} repeated final message; stopping")
        save_state(sid, state)
        print(json.dumps({"continue": True}))
        return

    # ---- 4. short genuine need-user request ----
    if len(msg) <= 160 and NEED_USER.search(msg) and not RHETORICAL.search(msg):
        log(f"{sid} needs user: {msg[:80]!r}; stopping")
        save_state(sid, state)
        print(json.dumps({"continue": True}))
        return

    # ---- 5. tool evidence for the just-finished turn ----
    fc_count, fc_names = last_turn_tool_activity(ev)
    if fc_count <= 0:
        state["quiet_turns"] = state.get("quiet_turns", 0) + 1
    else:
        state["quiet_turns"] = 0

    # ---- 6. fallback DONE (only stop if this was a quiet turn) ----
    if state.get("quiet_turns", 0) >= 1 and DONE.search(msg):
        log(f"{sid} done (quiet turn + done phrase): {msg[:80]!r}; stopping")
        save_state(sid, state)
        print(json.dumps({"continue": True}))
        return

    # ---- 7. too many quiet turns in a row -> stop (spinning) ----
    if state.get("quiet_turns", 0) >= QUIET_TURNS_MAX:
        log(f"{sid} {QUIET_TURNS_MAX} quiet turns in a row (no tool activity); stopping")
        save_state(sid, state)
        print(json.dumps({"continue": True}))
        return

    # ---- 8. otherwise keep going ----
    state["count"] = state.get("count", 0) + 1
    state["last_continue_at"] = now()
    state["last_msg"] = msg
    save_state(sid, state)
    reason = (
        "[watchdog] 任务尚未完成,请继续执行刚才的任务,不要输出阶段总结就停下。"
        "如果任务已经真正全部完成,请直接说『任务完成』并结束;"
        "如果遇到必须由用户提供信息、选择或凭据才能继续的情况,请直接说『需要用户』并停下列出问题。"
        "否则请继续执行,不要输出阶段总结就停下。"
    )
    log(f"{sid} continue #{state['count']} (tools={fc_count}): {msg[:60]!r}")
    print(json.dumps({"decision": "block", "reason": reason}))


if __name__ == "__main__":
    main()
