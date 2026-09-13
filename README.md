<div align="center">

# Codex Watchdog

**让 Codex 在任务未完成时自动继续 — Auto-continue Codex when tasks aren't done yet**

[![CI](https://github.com/tanlysgy/codex-watchdog/actions/workflows/ci.yml/badge.svg)](https://github.com/tanlysgy/codex-watchdog/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)]()
[![GitHub stars](https://img.shields.io/github/stars/tanlysgy/codex-watchdog?style=social)](https://github.com/tanlysgy/codex-watchdog)

</div>

> Codex often wraps up a turn with a progress summary before the task is truly finished,
> forcing you to type `continue` over and over. This project plugs a **Stop hook** into Codex
> that checks whether work is really done — if not, it auto-continues until the task is complete.

---

## 中文版 (Chinese)

让 Codex 在任务未完成时自动继续,而不是停在半路等你手动推。

### 原理 (How it works)

Codex 每回合结束会触发 `Stop` hook → 运行 `watchdog.py` → 脚本基于以下信号决定继续或停止:

| 信号 | 行为 |
|---|---|
| 本回合调用了工具(`exec_command`/`apply_patch`...) | 继续(还在干活) |
| 模型声明 `『任务完成』` / `任务完成` | 停 |
| 模型声明 `『需要用户』` / `需要用户:` | 停(等你输入) |
| 连续 3 回合无工具调用 | 停(原地打转) |
| 同一句最终消息重复 | 停(无进展) |
| 短请求("请你提供 API key") | 停(真在等你) |
| 连续 60 次自动续推、无用户输入 | 停(兜底) |
| 空闲超过 30 分钟 | 重置预算 |

### 安装

```bash
git clone https://github.com/tanlysgy/codex-watchdog
cd codex-watchdog
bash install.sh
```

然后在 Codex CLI 里运行 `/hooks`,给 `watchdog.py` 这条点"信任"。

### 验证

```bash
python3 watchdog_test.py    # 回归测试(11 项)
tail /tmp/codex-watchdog.log  # 看到 continue #1 即生效
```

---

## English

Auto-continue your Codex CLI when a turn ends before the task is complete — no more
repeatedly typing `continue`.

### How it works

A Codex **Stop hook** runs after every turn. `watchdog.py` reads the situation and decides:

| Signal | Action |
|---|---|
| Turn actually called tools (`exec_command` / `apply_patch` ...) | **Continue** (agent is working) |
| Agent declares `『任务完成』` / `『Task Complete』` / `任务完成` | **Stop** |
| Agent declares `『需要用户』` / `『Need User』` | **Stop** (blocked on you) |
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

Then run `/hooks` inside Codex CLI and **trust** the `watchdog.py` hook entry.

### Verify

```bash
python3 watchdog_test.py    # 11 regression tests
tail /tmp/codex-watchdog.log  # if you see "continue #1" it's working
```

### Uninstall

```bash
rm ~/.codex/watchdog.py ~/.codex/watchdog.enabled
# remove the [[hooks.Stop]] block from ~/.codex/config.toml
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

Log tail meanings:
- `declared done` / `done` — task genuinely finished
- `needs user` / `declared need-user` — blocked on you
- `repeated final message` — spinning in place
- `N quiet turns in a row` — no tools called for N turns
- `burst exhausted` — 60 auto-continues, no user nudged

### Cross‑agent compatibility

**Yes — this works with any CLI agent that supports Stop hooks.**  
Tested with Codex. Designed to be agent-agnostic:

- **Claude Code**: supports `ClaudeCodeStop` in its [hooks system](https://docs.anthropic.com/en/docs/claude-code/hooks). Adapt the event name and config path — same `watchdog.py` logic.
- **Gemini CLI**: `PreToolUse` / `PostToolUse` hooks available; a Stop-equivalent event is in preview.
- **Cursor / Continue.dev (Sidecar)**: do not expose lifecycle hooks — incompatible without additional tooling.

The core logic (`watchdog.py`) is pure Python, zero dependencies, and reads/produces JSON on
stdin/stdout — any shell with `python3` can run it.

---

### License

MIT &mdash; feel free to fork, adapt, and share.
