<div align="center">

# Codex Watchdog

**Auto-continue Codex when tasks aren't done yet — 让 Codex 在任务未完成时自动继续**

**[English](README.md) | [中文](README.zh-CN.md)**

[![CI](https://github.com/tanlysgy/codex-watchdog/actions/workflows/ci.yml/badge.svg)](https://github.com/tanlysgy/codex-watchdog/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)]()
[![GitHub stars](https://img.shields.io/github/stars/tanlysgy/codex-watchdog?style=social)](https://github.com/tanlysgy/codex-watchdog)

</div>

Auto-continue your Codex CLI when a turn ends before the task is complete — no more
repeatedly typing `continue`.

### How it works

A Codex **Stop hook** runs after every turn. `watchdog.py` reads the situation and decides:

> Protocol for the watched agent: when a turn really is finished, say `任务完成`
> (or `Task Complete`); when you need the user — input, a choice, credentials, or
> approval for an optional step — say `需要用户` (or `Need User`) and stop. The
> watchdog observes these declarations and respects them immediately.

| Signal | Action |
|---|---|
| Turn actually called tools (`exec_command` / `apply_patch` ...) | **Continue** (agent is working) |
| Agent declares `『任务完成』` / `Task Complete` | **Stop** |
| Agent declares `『需要用户』` / `Need User` | **Stop** (blocked on you) |
| No tool calls for 3 consecutive turns | **Stop** (spinning) |
| Same final message repeated verbatim | **Stop** (no progress) |
| Short need-user sentence ("please provide the API key") | **Stop** |
| Agent offers optional follow-ups or asks a question ("Want me to...?") | **Stop** (wait for you) |
| 60 auto-continues in a row with no real user input | **Stop** (burst guard) |
| Idle > 30 minutes | Reset burst budget |

A declaration only counts when it forms a line of its own (optionally followed by
punctuation and a short explanation). Progress narration such as
`已完成 3/7 个文件,还剩 4 个` or `任务完成度约 60%,继续推进` is **not** read as
"done" — the watchdog keeps that turn going.

### Install

```bash
git clone https://github.com/tanlysgy/codex-watchdog
cd codex-watchdog
bash install.sh
```

That registers a `Stop` hook in `~/.codex/hooks.json` and creates the global
enable marker `~/.codex/watchdog.enabled` (every session is watched). Then run
`/hooks` inside Codex CLI and **trust** the `watchdog.py` hook entry.

> Upgrading an existing install? Re-run `bash install.sh` — the enable marker
> makes the watchdog active for every session, old ones included, so there is
> no need for a per-session "wake word" anymore (the transcript wake-word scan
> remains as a fallback for sessions started without the marker).

### Verify

```bash
python3 watchdog_test.py    # 88 regression tests
python3 adapters_test.py    # 46 adapter tests
tail /tmp/codex-watchdog.log  # if you see "continue #1" it's working
```

### Uninstall

```bash
bash install.sh --uninstall
# or remove the watchdog entry from ~/.codex/hooks.json and ~/.codex/watchdog.*
```

### Configuration (env vars)

| Variable | Default | Meaning |
|---|---|---|
| `CODEX_WATCHDOG_MAX` | `60` | Burst auto-continue limit |
| `CODEX_WATCHDOG_RESET` | `1800` | Idle seconds before budget reset |
| `CODEX_WATCHDOG_QUIET` | `3` | Consecutive tool-less turns before spinning detection |
| `CODEX_WATCHDOG_CHECKPOINT` | `5` | Continues between checkpoints |
| `CODEX_WATCHDOG_STATE_DIR` | `/tmp/codex-watchdog` | Override state/event/checkpoint/metrics root |
| `CODEX_WATCHDOG_LOG` | `/tmp/codex-watchdog.log` | Override the plain-text log path |
| `CODEX_WATCHDOG_EVENTS_MAX` | `2000000` | Rotate `events.jsonl` once it exceeds this many bytes |
| `CODEX_WATCHDOG_TTL` | `604800` | Seconds before idle session state/checkpoints are cleaned up |
| `CODEX_WATCHDOG_HOST_CAP` | `8` on Claude, `0` on Codex | Consecutive blocks the host tolerates before it overrides the hook |

Invalid or out-of-range values are ignored (with a message on stderr) rather than
crashing the hook — a typo in a config value must never break your agent's turn.

**About `CODEX_WATCHDOG_HOST_CAP`:** Claude Code ends the turn itself after 8
consecutive `decision:"block"` responses (`CLAUDE_CODE_STOP_HOOK_BLOCK_CAP`,
default 8), so a budget larger than 8 is simply unreachable there and the
watchdog stops at the real ceiling instead. Codex has no equivalent host cap, so
the value defaults to `0` (disabled) and `CODEX_WATCHDOG_MAX` is the only limit.

### State & Logs

- State: `/tmp/codex-watchdog/<session_id>.json`
- Log: `/tmp/codex-watchdog.log`
- Event log (JSONL): `/tmp/codex-watchdog/events.jsonl`
- Checkpoints: `/tmp/codex-watchdog/checkpoints/<session_id>.json`
- Metrics: `/tmp/codex-watchdog/metrics.json`

### Runtime v1.1: TaskState, Event Log, Checkpoint, Metrics

The watchdog tracks a lightweight **task state machine** per session and records
observability artifacts — all optional, zero new dependencies, and fully
backward-compatible with existing installs.

**TaskState** — every session carries a `status` in its state file:

| State | Meaning |
|---|---|
| `RUNNING` | Task is progressing (auto-continue active) |
| `BLOCKED` | Waiting on the user (declared `需要用户`/`Need User`, or a genuine need-user request) |
| `COMPLETED` | Task finished (declared `任务完成`/`Task Complete`, or done phrase on a quiet turn) |
| `STALLED` | No progress: burst exhausted, repeated final message, or too many quiet turns |

**Event Log** — a JSONL stream at `events.jsonl` with one line per transition:
`task_started`, `continue`, `blocked`, `completed`, `stalled`, plus the
context-lifecycle `precompact` and `resume`. Each event carries `timestamp`,
`session_id`, `state`, `reason`, and `continue_count`.

**Checkpoint** — a lightweight snapshot (last message, state, counters, tool
stats) written to `checkpoints/<session_id>.json` on every state change and every
`CODEX_WATCHDOG_CHECKPOINT` (default 5) continues. It does **not** store the full
transcript or generate LLM summaries.

**Metrics** — `metrics.json` is derived from the event log and split two ways: a
per-session row and a `totals` roll-up. Reported: total continues, average
continue rounds, stop-reason distribution, quiet-turn hits, recovery counters
(`stalled_recovered`, `completed_then_continued`, `blocked_then_continued`), and
run-lifecycle counts (`compactions`, `resumes`, `host_continuations`).

**Bounded by design** — the event log rotates to `events.jsonl.1` once it exceeds
`CODEX_WATCHDOG_EVENTS_MAX`, and session state plus checkpoints idle for longer
than `CODEX_WATCHDOG_TTL` are cleaned up (at most once an hour). State files are
written atomically, so a crash can't corrupt the burst budget.

### Resume engine: surviving context compaction

A long auto-continued task eventually hits the context window. Nothing inside a
Stop hook can read how full the window is — context usage is not part of the hook
contract — but both Codex and Claude Code expose the compaction events, so the
watchdog treats "context almost full" as an **event**, not a number:

| Hook | What the watchdog does |
|---|---|
| `PreCompact` | Writes a checkpoint holding the task goal (the session's first real user prompt) and the next step, plus current state and counters |
| `SessionStart` (`source: compact`) | Prints that checkpoint back as context, so the post-compaction agent resumes instead of restarting |

Example of what gets re-injected:

```
[watchdog] This session was compacted mid-task. Resume it:
- goal: 把 README 的对比表更新一下
- next step: 改完表格,接着验证链接
- recorded status: RUNNING (auto-continues so far: 3)
Continue from the next step instead of restarting. ...
```

The pointer is consumed after one injection and is **not** replayed on a plain
`resume` or `startup` SessionStart, so stale instructions don't leak into a later
session.

`install.sh` registers all three events (`Stop`, `PreCompact`, `SessionStart`) and
is idempotent — upgrading a Stop-only install adds the new events and leaves the
existing entry alone. Re-run `bash install.sh`, then re-trust via `/hooks`.

This is deliberately not a full resume engine: it re-injects the goal and next
step, not a reconstruction of the work. Recovering the work itself has to come
from a checkpoint the agent writes, not from something a hook can infer.

### Cross-agent compatibility

**Yes — works with Codex and Claude Code out of the box.** The engine is agent-agnostic:
transcript parsing lives in `adapters/` and is selected via `CODEX_WATCHDOG_ADAPTER`:

| Agent | Adapter | Install |
|---|---|---|
| Codex | `adapters/codex.py` (default) | `bash install.sh` |
| Claude Code | `adapters/claude.py` | `bash install.sh --agent claude` |

- **Codex**: registered as `Stop`, `PreCompact` and `SessionStart` command hooks in
  `~/.codex/hooks.json`.
- **Claude Code**: registered as the same three events in `~/.claude/settings.json`
  (see [docs/CLAUDE.md](docs/CLAUDE.md)). Claude's Stop hook shares the same JSON
  contract: `decision:"block"` + `reason` keeps it working.
- **Gemini CLI**: `PreToolUse` / `PostToolUse` hooks available; a Stop-equivalent event
  is in preview.
- **Cursor / Continue.dev (Sidecar)**: do not expose lifecycle hooks — incompatible
  without additional tooling.

The core logic (`watchdog.py`) is pure Python, zero dependencies, and reads/produces JSON on
stdin/stdout — any agent with a Stop hook and a small adapter can reuse it.

### Comparison & known limitations

Similar projects we found while evaluating this one:

| Project | Approach | Difference from us |
|---|---|---|
| [flowing-water1/codex-watchdog](https://github.com/flowing-water1/codex-watchdog) | Node.js proxy between the Codex TUI and `app-server` | Watches **outside** the Stop hook; recovers transient failures (429/502/503/504), context exhaustion, and usage limits — deeper than a hook can reach |
| [kur114/codex-auto-continue-hook](https://github.com/kur114/codex-auto-continue-hook) | Stop hook + keyword matching | Same hook-based idea, but keyword matching is brittle; we use evidence-driven detection (tool activity, `任务完成`/`需要用户` protocol, quiet-turn + burst guards) |

**Known limitation**: we run inside the Stop hook, so we can only act when a turn
actually ends and triggers the hook. If a connection drops mid-response and the
turn never completes cleanly (no `Stop` event), the watchdog cannot restart it —
that class of failure needs an out-of-process supervisor like
`flowing-water1/codex-watchdog`.

### License

MIT &mdash; feel free to fork, adapt, and share.
