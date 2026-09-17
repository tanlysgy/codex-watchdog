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
  1. Declared protocol: a line that is exactly 任务完成/『任务完成』/Task Complete
     -> stop; 需要用户/『需要用户』/Need User -> stop (blocked on user).
  2. Burst budget: N auto-continues with no real user input -> stop.
  3. Repeated identical final message -> stop (no progress).
  4. Short genuine need-user request -> stop.
  5. Tool evidence: current turn actually called tools -> continue.
  6. Quiet turns: 3 tool-less turns in a row -> stop (spinning).
  7. Classic done phrases (on a quiet turn) -> stop.

Runtime v1.1 observability (all optional, zero new dependencies):
  - TaskState: every session carries a status (RUNNING / BLOCKED / COMPLETED /
    STALLED) persisted in the session state file.
  - Event Log: a JSONL stream of task_started / continue / blocked / completed /
    stalled events at /tmp/codex-watchdog/events.jsonl.
  - Checkpoint: a lightweight snapshot (last message, state, counters) written
    every N continues and on every state change, under
    /tmp/codex-watchdog/checkpoints/.
  - Metrics: per-session and global aggregates derived from the event log at
    /tmp/codex-watchdog/metrics.json.

State: /tmp/codex-watchdog/<session_id>.json
Log:   /tmp/codex-watchdog.log
"""
import json
import os
import sys
import time

# Make `adapters` importable whether run from the repo or from ~/.codex.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import watchdog_protocol as protocol  # noqa: E402
from adapters import load_adapter  # noqa: E402


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    """Parse an int env var without ever raising.

    A hook must never crash: a typo in a config value degrades to the default
    (and is logged) instead of aborting the turn.
    """
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        val = int(str(raw).strip())
    except ValueError:
        sys.stderr.write(f"watchdog: ignoring invalid {name}={raw!r}; using {default}\n")
        return default
    if val < minimum:
        sys.stderr.write(f"watchdog: ignoring out-of-range {name}={val}; using {default}\n")
        return default
    return val


def _env_float(name: str, default: float, minimum: float = 1.0) -> float:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        val = float(str(raw).strip())
    except ValueError:
        sys.stderr.write(f"watchdog: ignoring invalid {name}={raw!r}; using {default}\n")
        return default
    if val < minimum:
        sys.stderr.write(f"watchdog: ignoring out-of-range {name}={val}; using {default}\n")
        return default
    return val


# Portable default state root. On POSIX we keep the historical /tmp paths so
# existing installs, docs and `tail /tmp/codex-watchdog.log` keep working; only
# platforms without /tmp (Windows) fall back to the platform temp dir.
def _default_state_dir() -> str:
    if os.name != "nt" and os.path.isdir("/tmp"):
        return "/tmp/codex-watchdog"
    import tempfile
    return os.path.join(tempfile.gettempdir(), "codex-watchdog")


def _default_log_file() -> str:
    if os.name != "nt" and os.path.isdir("/tmp"):
        return "/tmp/codex-watchdog.log"
    import tempfile
    return os.path.join(tempfile.gettempdir(), "codex-watchdog.log")


STATE_DIR = os.environ.get("CODEX_WATCHDOG_STATE_DIR") or _default_state_dir()
LOG_FILE = os.environ.get("CODEX_WATCHDOG_LOG") or _default_log_file()
EVENTS_FILE = os.path.join(STATE_DIR, "events.jsonl")
METRICS_FILE = os.path.join(STATE_DIR, "metrics.json")
CHECKPOINT_DIR = os.path.join(STATE_DIR, "checkpoints")
BURST_MAX = _env_int("CODEX_WATCHDOG_MAX", 60)
RESET_AFTER_SEC = _env_float("CODEX_WATCHDOG_RESET", 1800.0)
QUIET_TURNS_MAX = _env_int("CODEX_WATCHDOG_QUIET", 3)
CHECKPOINT_EVERY = _env_int("CODEX_WATCHDOG_CHECKPOINT", 5)
EVENTS_MAX_BYTES = _env_int("CODEX_WATCHDOG_EVENTS_MAX", 2_000_000)
STATE_TTL_SEC = _env_float("CODEX_WATCHDOG_TTL", 7 * 86400.0)
ADAPTER_NAME = os.environ.get("CODEX_WATCHDOG_ADAPTER", "codex")

# --- TaskState ---
RUNNING = "RUNNING"
BLOCKED = "BLOCKED"
COMPLETED = "COMPLETED"
STALLED = "STALLED"

# How many consecutive auto-continues a host will tolerate before it overrides
# the hook itself. Claude Code ends the turn after 8 consecutive blocks
# (CLAUDE_CODE_STOP_HOOK_BLOCK_CAP, default 8); Codex has no such host cap, so
# there we rely on BURST_MAX alone. Knowing this keeps our own budget honest:
# a budget above the host cap is unreachable on that host.
HOST_BLOCK_CAP = _env_int("CODEX_WATCHDOG_HOST_CAP", 8 if ADAPTER_NAME == "claude" else 0,
                          minimum=0)

try:
    ADAPTER = load_adapter(ADAPTER_NAME)
except Exception as exc:  # pragma: no cover - fallback so hooks never crash
    sys.stderr.write(f"watchdog: failed to load adapter {ADAPTER_NAME!r}: {exc}\n")
    ADAPTER = None

DONE = protocol.DONE
NEED_USER = protocol.NEED_USER
RHETORICAL = protocol.RHETORICAL
PROMPT_FOR_INPUT = protocol.PROMPT_FOR_INPUT


def log(msg: str) -> None:
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
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
    path = os.path.join(STATE_DIR, f"{sid}.json")
    tmp = f"{path}.tmp"
    # Atomic replace: a concurrent Stop hook (or a crash) must never leave a
    # half-written state file behind, or the next run silently loses the budget.
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def session_activated(ev: dict, state: dict) -> bool:
    if os.environ.get("CODEX_WATCHDOG") == "0":
        return False
    if os.environ.get("CODEX_WATCHDOG") == "1":
        return True
    # Global marker (~/.codex/watchdog.enabled) == watchdog installed & enabled
    # for every session. Created by install.sh; checked first so upgrades of an
    # already-enabled install keep working even when the transcript carries no
    # wake word (the wake-word fallback below is per-session convenience).
    marker = os.path.expanduser("~/.codex/watchdog.enabled")
    if os.path.exists(marker):
        return True
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


def first_user_prompt(ev: dict):
    if ADAPTER is None or not hasattr(ADAPTER, "first_user_prompt"):
        return None
    return ADAPTER.first_user_prompt(ev)


def last_assistant_text(ev: dict):
    """Transcript fallback for the last assistant message.

    The Stop hook is handed `last_assistant_message` directly, but
    context-lifecycle events (PreCompact) may not carry it, and a checkpoint
    written at compaction time is worthless without "where were we".
    """
    if ADAPTER is None or not hasattr(ADAPTER, "last_assistant_text"):
        return None
    return ADAPTER.last_assistant_text(ev)


def turn_metadata(ev: dict) -> dict:
    """Normalized turn snapshot from the active adapter (checkpoint metadata)."""
    if ADAPTER is None or not hasattr(ADAPTER, "checkpoint_metadata"):
        return {}
    try:
        return ADAPTER.checkpoint_metadata(ev)
    except Exception:  # pragma: no cover - metadata is best-effort
        return {}


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


# --- Structured Event Log (JSONL) -------------------------------------------

def read_events(sid: str = None) -> list:
    events = []
    try:
        with open(EVENTS_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if sid is None or row.get("session_id") == sid:
                    events.append(row)
    except OSError:
        pass
    return events


def rotate_events_if_needed() -> None:
    """Keep the event log bounded: move the current file aside when too big."""
    try:
        if os.path.getsize(EVENTS_FILE) < EVENTS_MAX_BYTES:
            return
    except OSError:
        return
    try:
        os.replace(EVENTS_FILE, EVENTS_FILE + ".1")
    except OSError:
        pass


def emit_event(sid: str, event: str, status: str, reason: str,
               continue_count: int, metrics: dict = None) -> None:
    ev = {
        "timestamp": time.time(),
        "session_id": sid,
        "event": event,
        "state": status,
        "reason": reason,
        "continue_count": continue_count,
    }
    os.makedirs(STATE_DIR, exist_ok=True)
    try:
        with open(EVENTS_FILE, "a") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except OSError:
        return
    if metrics is not None:
        fold_metric(metrics, event, reason)
    rotate_events_if_needed()


# --- Metrics (derived from this session's event log slice) -------------------

def fold_metric(m: dict, event: str, reason: str) -> None:
    """Update this session's counters with one event (incremental, O(1))."""
    if event == "continue":
        m["continues"] = m.get("continues", 0) + 1
    elif event in ("completed", "blocked", "stalled"):
        key = f"{event}_count"
        m[key] = m.get(key, 0) + 1
        if event == "stalled" and "quiet" in (reason or "").lower():
            m["quiet_turn_hits"] = m.get("quiet_turn_hits", 0) + 1
    elif event == "precompact":
        m["compactions"] = m.get("compactions", 0) + 1
    elif event == "resume":
        m["resumes"] = m.get("resumes", 0) + 1
    prev = m.get("last_event")
    if event == "continue":
        if prev == "stalled":
            m["stalled_recovered"] = m.get("stalled_recovered", 0) + 1
        elif prev == "completed":
            m["completed_then_continued"] = m.get("completed_then_continued", 0) + 1
        elif prev == "blocked":
            m["blocked_then_continued"] = m.get("blocked_then_continued", 0) + 1
    m["last_event"] = event


def session_metrics(sid: str) -> dict:
    """Per-session metrics for the current state (derived from the event log)."""
    events = read_events(sid)
    m = {}
    for e in events:
        fold_metric(m, e.get("event"), e.get("reason") or "")
    return m


def update_metrics(sid: str, metrics: dict) -> None:
    """Merge this session's metrics into the global metrics file.

    Only the per-session slice is aggregated (not the whole log re-parsed for
    every event), so the cost stays flat as the event log grows.
    """
    try:
        with open(METRICS_FILE) as f:
            doc = json.load(f)
        if not isinstance(doc, dict):
            doc = {}
    except (OSError, ValueError):
        doc = {}
    sessions = doc.get("sessions")
    if not isinstance(sessions, dict):
        sessions = {}
    sessions[sid] = {
        "continues": metrics.get("continues", 0),
        "completed": metrics.get("completed_count", 0),
        "blocked": metrics.get("blocked_count", 0),
        "stalled": metrics.get("stalled_count", 0),
        "quiet_turn_hits": metrics.get("quiet_turn_hits", 0),
        "stalled_recovered": metrics.get("stalled_recovered", 0),
        "completed_then_continued": metrics.get("completed_then_continued", 0),
        "blocked_then_continued": metrics.get("blocked_then_continued", 0),
        "compactions": metrics.get("compactions", 0),
        "resumes": metrics.get("resumes", 0),
        "host_continuations": metrics.get("host_continuations", 0),
        "last_event": metrics.get("last_event"),
        "updated_at": time.strftime("%F %T"),
    }
    doc["sessions"] = sessions
    doc["totals"] = {
        "sessions": len(sessions),
        "continues": sum(s.get("continues", 0) for s in sessions.values()),
        "average_continue_rounds": round(
            sum(s.get("continues", 0) for s in sessions.values())
            / max(1, sum(1 for s in sessions.values() if s.get("continues", 0))), 2),
        "stop_reason_distribution": {
            "completed": sum(s.get("completed", 0) for s in sessions.values()),
            "blocked": sum(s.get("blocked", 0) for s in sessions.values()),
            "stalled": sum(s.get("stalled", 0) for s in sessions.values()),
        },
        "quiet_turn_hits": sum(s.get("quiet_turn_hits", 0) for s in sessions.values()),
        "stalled_recovered": sum(s.get("stalled_recovered", 0) for s in sessions.values()),
        "completed_then_continued": sum(
            s.get("completed_then_continued", 0) for s in sessions.values()),
        "blocked_then_continued": sum(
            s.get("blocked_then_continued", 0) for s in sessions.values()),
        "compactions": sum(s.get("compactions", 0) for s in sessions.values()),
        "resumes": sum(s.get("resumes", 0) for s in sessions.values()),
        "host_continuations": sum(
            s.get("host_continuations", 0) for s in sessions.values()),
    }
    doc["updated_at"] = time.strftime("%F %T")
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(METRICS_FILE, "w") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


# --- Lightweight Checkpoint -------------------------------------------------

def write_checkpoint(sid: str, state: dict, msg: str, fc_count: int,
                     fc_names: set, reason: str, metadata: dict = None,
                     extra: dict = None) -> None:
    """Snapshot the turn so a later run (or a human) can resume from it."""
    md = metadata if metadata is not None else {}
    tool_calls = md.get("tool_calls", fc_count)
    tool_names = md.get("tool_names") or sorted(fc_names)
    cp = {
        "timestamp": time.time(),
        "time": time.strftime("%F %T"),
        "session_id": sid,
        "status": state.get("status"),
        "continue_count": state.get("count", 0),
        "quiet_turns": state.get("quiet_turns", 0),
        "last_message": msg,
        "tool_calls": tool_calls,
        "tool_names": tool_names,
        "completion_signal": md.get("completion_signal"),
        "need_user_signal": md.get("need_user_signal"),
        "reason": reason,
    }
    if extra:
        cp.update(extra)
    os.makedirs(CHECKPOINT_DIR, exist_ok=True)
    path = os.path.join(CHECKPOINT_DIR, f"{sid}.json")
    try:
        with open(path, "w") as f:
            json.dump(cp, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


# --- Resume Engine: checkpoints across compaction ---------------------------
#
# Nothing inside a Stop hook can read how full the context window is (context
# usage is not in the hook contract; repeated feature requests were closed as
# not planned). The supported primitives are event-driven instead: PreCompact
# fires before the window is summarised away, and SessionStart(source=compact)
# fires afterwards. So we treat "context almost full" as an *event*, not a
# number:
#
#   PreCompact      -> write a checkpoint holding the goal + next step + state
#   SessionStart    -> re-inject that checkpoint as context, so the post-compact
#                      agent still knows what it was doing

def load_checkpoint(sid: str):
    path = os.path.join(CHECKPOINT_DIR, f"{sid}.json")
    try:
        with open(path) as f:
            cp = json.load(f)
        return cp if isinstance(cp, dict) else None
    except (OSError, ValueError):
        return None


def write_precompact_checkpoint(ev: dict, sid: str) -> None:
    """Save a resume pointer before the host summarises the context away.

    Unlike a Stop-time checkpoint (a snapshot of a finished turn), this one is
    written while the turn is still alive, and carries the goal and next step so
    a post-compaction session can pick the work back up.
    """
    state = load_state(sid)
    msg = (ev.get("last_assistant_message") or "").strip()
    if not msg:
        # PreCompact need not carry the message; fall back to the transcript.
        msg = (last_assistant_text(ev) or "").strip()
    fc_count, fc_names = last_turn_tool_activity(ev)
    metadata = turn_metadata(ev)
    metrics = session_metrics(sid)
    # The session's first real user prompt is the best available statement of
    # what the task actually is, so keep it as the resume "goal".
    goal = state.get("goal")
    if not goal:
        prompt = first_user_prompt(ev)
        if prompt:
            goal = prompt.strip()[:500]
            state["goal"] = goal
    state["next_step"] = (msg or state.get("last_msg") or "").strip()[:500]
    # NOTE: don't bump `compactions` here — emit_event folds it into metrics.
    emit_event(sid, "precompact", state.get("status") or RUNNING,
               f"compaction ({ev.get('trigger') or 'unknown'})",
               state.get("count", 0), metrics)
    write_checkpoint(
        sid, state, msg or state.get("last_msg") or "", fc_count, fc_names,
        f"precompact ({ev.get('trigger') or 'unknown'})", metadata,
        extra={
            "trigger": ev.get("trigger"),
            "goal": goal,
            "next_step": state.get("next_step"),
            "compactions": metrics.get("compactions", 0),
            "for_resume": True,
        },
    )
    save_state(sid, state)
    update_metrics(sid, metrics)
    log(f"{sid} checkpoint written before compaction ({ev.get('trigger')})")


def resume_context(ev: dict, sid: str) -> str:
    """Build the context to re-inject after a compaction ('' = nothing to say)."""
    cp = load_checkpoint(sid)
    if not cp or not cp.get("for_resume"):
        return ""
    parts = ["[watchdog] This session was compacted mid-task. Resume it:"]
    if cp.get("goal"):
        parts.append(f"- goal: {cp['goal']}")
    if cp.get("next_step"):
        parts.append(f"- next step: {cp['next_step']}")
    if cp.get("last_message"):
        parts.append(f"- last progress note: {cp['last_message'][:400]}")
    parts.append(
        f"- recorded status: {cp.get('status')} "
        f"(auto-continues so far: {cp.get('continue_count', 0)})"
    )
    parts.append(
        "Continue from the next step instead of restarting. Work that already "
        "succeeded does not need to be redone; re-check anything whose result "
        "you cannot see any more. Say 『Task Complete』 when it is really done, "
        "or 『Need User』 if you are blocked on the user."
    )
    return "\n".join(parts)


def emit_resume_context(ev: dict, sid: str) -> None:
    """SessionStart hook: re-inject the pre-compaction checkpoint as context.

    Plain stdout is the documented carrier for context-only events on both
    hosts, so we print it rather than describing a hookSpecificOutput shape we
    would have to guess at.
    """
    state = load_state(sid)
    if not session_activated(ev, state):
        return
    cp = load_checkpoint(sid)
    source = ev.get("source")
    # Only inject when there is a resume pointer, and only once per compaction:
    # a later plain `resume`/`startup` must not replay stale instructions.
    if not cp or not cp.get("for_resume"):
        return
    if source not in (None, "", "compact"):
        return
    text = resume_context(ev, sid)
    if not text:
        return
    metrics = session_metrics(sid)
    emit_event(sid, "resume", state.get("status") or RUNNING,
               f"context re-injected after {source or 'start'}", state.get("count", 0),
               metrics)
    update_metrics(sid, metrics)
    # Consume the pointer so the next SessionStart does not repeat it.
    cp["for_resume"] = False
    try:
        with open(os.path.join(CHECKPOINT_DIR, f"{sid}.json"), "w") as f:
            json.dump(cp, f, ensure_ascii=False, indent=2)
    except OSError:
        pass
    log(f"{sid} resume context emitted (source={source})")
    print(text)


# --- Housekeeping -----------------------------------------------------------

def cleanup_stale_state() -> None:
    """Drop session state/checkpoints that have not been touched for STATE_TTL.

    /tmp is not garbage collected for us, and every session leaves a state file
    plus a checkpoint behind forever.
    """
    cutoff = now() - STATE_TTL_SEC
    try:
        names = os.listdir(STATE_DIR)
    except OSError:
        return
    for name in names:
        if not name.endswith(".json") or name in ("metrics.json",):
            continue
        path = os.path.join(STATE_DIR, name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
        except OSError:
            continue
    try:
        names = os.listdir(CHECKPOINT_DIR)
    except OSError:
        return
    for name in names:
        path = os.path.join(CHECKPOINT_DIR, name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
        except OSError:
            continue


def maybe_cleanup() -> None:
    """Run housekeeping at most once an hour, recorded by a stamp file."""
    stamp = os.path.join(STATE_DIR, ".last-cleanup")
    try:
        if os.path.getmtime(stamp) > now() - 3600:
            return
    except OSError:
        pass
    cleanup_stale_state()
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(stamp, "w") as f:
            f.write(str(now()))
    except OSError:
        pass


# --- Terminal / continue transitions ---------------------------------------

def stop(sid: str, state: dict, status: str, reason: str, msg: str,
         fc_count: int, fc_names: set, metrics: dict, metadata: dict = None) -> None:
    state["status"] = status
    state["updated_at"] = now()
    state["reason"] = reason
    emit_event(sid, status.lower(), status, reason, state.get("count", 0), metrics)
    write_checkpoint(sid, state, msg, fc_count, fc_names, reason, metadata)
    update_metrics(sid, metrics)
    save_state(sid, state)
    log(f"{sid} {status}: {reason}")
    print(json.dumps({"continue": True}))


def keep_going(sid: str, state: dict, msg: str, fc_count: int, fc_names: set,
               reason: str, metrics: dict, metadata: dict = None) -> None:
    state["count"] = state.get("count", 0) + 1
    state["last_continue_at"] = now()
    state["last_msg"] = msg
    state["status"] = RUNNING
    state["updated_at"] = now()
    state["reason"] = reason
    emit_event(sid, "continue", RUNNING, reason, state["count"], metrics)
    if state["count"] % CHECKPOINT_EVERY == 0:
        write_checkpoint(sid, state, msg, fc_count, fc_names, reason, metadata)
    update_metrics(sid, metrics)
    save_state(sid, state)
    log(f"{sid} continue #{state['count']} (tools={fc_count}): {msg[:60]!r}")
    print(json.dumps({"decision": "block", "reason": reason}))


def main() -> None:
    try:
        ev = json.load(sys.stdin)
    except ValueError:
        return
    hook_event = ev.get("hook_event_name") or ev.get("hookEventName") or ""
    # Accept both Codex ("Stop") and Claude Code ("Stop") event names.
    if hook_event in ("Stop", "ClaudeCodeStop"):
        return handle_stop(ev)
    # Context-lifecycle events: the only supported way to survive a compaction.
    if hook_event == "PreCompact":
        return write_precompact_checkpoint(ev, ev.get("session_id") or "unknown")
    if hook_event == "SessionStart":
        return emit_resume_context(ev, ev.get("session_id") or "unknown")


def handle_stop(ev: dict) -> None:
    sid = ev.get("session_id") or "unknown"
    msg = (ev.get("last_assistant_message") or "").strip()
    state = load_state(sid)

    if not session_activated(ev, state):
        return
    state["activated"] = True

    if should_reset_burst(state, ev):
        state["count"] = 0
        state["last_msg"] = None

    fc_count, fc_names = last_turn_tool_activity(ev)
    metadata = turn_metadata(ev)
    metrics = session_metrics(sid)

    # Host signals: `stop_hook_active` is true when this turn is already a
    # continuation produced by a previous block. Claude Code gives up after 8
    # consecutive blocks; Codex has no cap of its own and relies on the hook to
    # bound itself, so our own budget stays the primary guard there.
    host_continued = bool(ev.get("stop_hook_active"))

    # First time we see this session -> record task_started.
    if state.get("status") is None:
        state["status"] = RUNNING
        emit_event(sid, "task_started", RUNNING, "session started", 0, metrics)

    # ---- 1. declared protocol (highest priority) ----
    if protocol.is_completion_declaration(msg):
        return stop(sid, state, COMPLETED, "declared done", msg,
                    fc_count, fc_names, metrics, metadata)
    if protocol.is_need_user_declaration(msg):
        return stop(sid, state, BLOCKED, "declared need-user", msg,
                    fc_count, fc_names, metrics, metadata)

    # ---- 2. burst guard ----
    # The effective ceiling is the smaller of our budget and what the host will
    # actually honour (Claude Code stops accepting blocks after 8).
    effective_max = BURST_MAX
    if HOST_BLOCK_CAP:
        effective_max = min(effective_max, HOST_BLOCK_CAP)
    if state.get("count", 0) >= effective_max:
        reason = "burst exhausted"
        if HOST_BLOCK_CAP and effective_max == HOST_BLOCK_CAP < BURST_MAX:
            reason = f"host block cap reached ({HOST_BLOCK_CAP})"
        return stop(sid, state, STALLED, reason, msg,
                    fc_count, fc_names, metrics, metadata)

    # Note on `stop_hook_active`: it is true when this turn is already a
    # continuation produced by a previous block. Blocking again is exactly how
    # repeated continuations work, so it is deliberately NOT a stop condition —
    # treating it as one would end auto-continue after the very first round.
    # It is used two ways instead:
    #   1. as a drift check — if the host says we already continued but our own
    #      counter is 0, our state was lost (TTL cleanup, wiped state dir), and
    #      continuing blindly would sail past the host's block cap unnoticed;
    #   2. as a recorded signal, so `host_continuations` distinguishes rounds the
    #      host carried from rounds we initiated.
    state["host_continued"] = host_continued
    if host_continued:
        metrics["host_continuations"] = metrics.get("host_continuations", 0) + 1
        if state.get("count", 0) == 0:
            log(f"{sid} host reports an active continuation but our counter is 0; "
                f"re-syncing budget")
            state["count"] = 1

    # ---- 3. repeated final message ----
    if state.get("last_msg") and msg and msg == state["last_msg"]:
        return stop(sid, state, STALLED, "repeated final message", msg,
                    fc_count, fc_names, metrics, metadata)

    # ---- 4. short genuine need-user request ----
    if len(msg) <= 160 and NEED_USER.search(msg) and not RHETORICAL.search(msg):
        return stop(sid, state, BLOCKED, "needs user", msg,
                    fc_count, fc_names, metrics, metadata)

    # ---- 4b. agent offered options / asked for a decision -> stop & wait ----
    if len(msg) <= 400 and PROMPT_FOR_INPUT.search(msg):
        return stop(sid, state, BLOCKED, "prompt-for-input", msg,
                    fc_count, fc_names, metrics, metadata)

    # ---- 5. tool evidence for the just-finished turn ----
    if fc_count <= 0:
        state["quiet_turns"] = state.get("quiet_turns", 0) + 1
    else:
        state["quiet_turns"] = 0

    # ---- 6. fallback DONE (only stop if this was a quiet turn) ----
    if state.get("quiet_turns", 0) >= 1 and DONE.search(msg):
        return stop(sid, state, COMPLETED, "done (quiet turn + done phrase)",
                    msg, fc_count, fc_names, metrics, metadata)

    # ---- 7. too many quiet turns in a row -> stop (spinning) ----
    if state.get("quiet_turns", 0) >= QUIET_TURNS_MAX:
        return stop(sid, state, STALLED, "quiet turns", msg,
                    fc_count, fc_names, metrics, metadata)

    # ---- 8. otherwise keep going ----
    last_prompt = last_user_prompt(ev) or ""
    reason_en = (
        "[watchdog] The task is not finished; keep working on the same task and "
        "do not stop after a progress summary. When it is truly complete, say "
        "『Task Complete』 and stop. When you need input, a choice, credentials, or "
        "the user to decide on an optional step, say 『Need User』 and stop with the "
        "question or options — do not keep acting or do optional work on your own. "
        "Otherwise keep going."
    )
    reason_zh = (
        "[watchdog] 任务尚未完成,请继续执行刚才的任务,不要输出阶段总结就停下。"
        "如果任务已经真正全部完成,请直接说『任务完成』并结束;"
        "如果必须由用户提供信息、选择或凭据,或你想请用户做主/抛可选后续,请直接说『需要用户』并停下列出问题或选项,"
        "不要继续行动,也不要自己动手做可选事项;"
        "否则请继续执行,不要输出阶段总结就停下。"
    )
    # Prefer Chinese when the session speaks Chinese or language is unknown.
    if any('\u4e00' <= ch <= '\u9fff' for ch in last_prompt):
        reason = reason_zh
    else:
        reason = reason_en
    return keep_going(sid, state, msg, fc_count, fc_names, reason, metrics, metadata)


def _run() -> None:
    try:
        main()
    finally:
        maybe_cleanup()


if __name__ == "__main__":
    _run()