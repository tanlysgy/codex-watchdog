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

| Signal | Action |
|---|---|
| Turn actually called tools (`exec_command` / `apply_patch` ...) | **Continue** (agent is working) |
| Agent declares `『任务完成』` / `Task Complete` | **Stop** |
| Agent declares `『需要用户』` / `Need User` | **Stop** (blocked on you) |
| No tool calls for 3 consecutive turns | **Stop** (spinning) |
| Same final message repeated verbatim | **Stop** (no progress) |
| Short need-user sentence ("please provide the API key") | **Stop** |
| 60 auto-continues in a row with no real user input | **Stop** (burst guard) |
| Idle > 30 minutes | Reset burst budget |

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
python3 watchdog_test.py    # 11 regression tests
python3 adapters_test.py    # adapter tests
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

### State & Logs

- State: `/tmp/codex-watchdog/<session_id>.json`
- Log: `/tmp/codex-watchdog.log`

### Cross-agent compatibility

**Yes — works with Codex and Claude Code out of the box.** The engine is agent-agnostic:
transcript parsing lives in `adapters/` and is selected via `CODEX_WATCHDOG_ADAPTER`:

| Agent | Adapter | Install |
|---|---|---|
| Codex | `adapters/codex.py` (default) | `bash install.sh` |
| Claude Code | `adapters/claude.py` | `bash install.sh --agent claude` |

- **Codex**: registered as a `Stop` command hook in `~/.codex/hooks.json`.
- **Claude Code**: registered as a `Stop` command hook in `~/.claude/settings.json`
  (see [docs/CLAUDE.md](docs/CLAUDE.md)). Claude's Stop hook shares the same JSON
  contract: `decision:"block"` + `reason` keeps it working.
- **Gemini CLI**: `PreToolUse` / `PostToolUse` hooks available; a Stop-equivalent event
  is in preview.
- **Cursor / Continue.dev (Sidecar)**: do not expose lifecycle hooks — incompatible
  without additional tooling.

The core logic (`watchdog.py`) is pure Python, zero dependencies, and reads/produces JSON on
stdin/stdout — any agent with a Stop hook and a small adapter can reuse it.

### License

MIT &mdash; feel free to fork, adapt, and share.
