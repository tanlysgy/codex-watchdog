<div align="center">

# Codex Watchdog

**English | [中文](README.zh-CN.md)**

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

脚本会注册 `~/.codex/hooks.json` 的 `Stop` hook,并创建全局启用标记
`~/.codex/watchdog.enabled`(所有会话都启用)。然后在 Codex CLI 里运行
`/hooks`,给 `watchdog.py` 这条点"信任"。

> 升级已有安装?重跑 `bash install.sh` 即可——启用标记会对**所有**会话生效,
> 包括老会话,不再需要会话里出现"看门狗"等唤醒词(转录唤醒词扫描仍保留,
> 作为无标记会话的兜底)。

### 验证

```bash
python3 watchdog_test.py    # 回归测试(11 项)
tail /tmp/codex-watchdog.log  # 看到 continue #1 即生效
```

---

### 跨 agent 复用

核心引擎与 agent 无关,transcript 解析放在 `adapters/`,通过 `CODEX_WATCHDOG_ADAPTER` 选择:

| Agent | Adapter | 安装 |
|---|---|---|
| Codex | `adapters/codex.py`(默认) | `bash install.sh` |
| Claude Code | `adapters/claude.py` | `bash install.sh --agent claude` |

- **Codex**:注册为 `~/.codex/hooks.json` 的 `Stop` command hook。
- **Claude Code**:注册为 `~/.claude/settings.json` 的 `Stop` command hook,
  详细见 [docs/CLAUDE.md](docs/CLAUDE.md)。Claude 的 Stop hook 与 Codex 同一套
  JSON 契约:`decision:"block"` + `reason` 让它继续干。
- **Gemini CLI**:有 `PreToolUse` / `PostToolUse`,Stop 类事件尚在预览。
- **Cursor / Continue.dev**:不暴露生命周期 hook,无法直接复用。

`watchdog.py` 是纯 Python、零依赖,从 stdin 读 JSON / 往 stdout 写 JSON,
任何带 Stop hook 的 agent 加个小 adapter 都能复用。

### 卸载

```bash
bash install.sh --uninstall
# 或手动移除 ~/.codex/hooks.json 里的 watchdog 条目与 ~/.codex/watchdog.*
```

### 同类项目对比与已知局限

调研时找到的两个最像的项目:

| 项目 | 方案 | 与我们差异 |
|---|---|---|
| [flowing-water1/codex-watchdog](https://github.com/flowing-water1/codex-watchdog) | Node.js 代理,插在 Codex TUI 与 `app-server` 之间 | 在 Stop hook **之外**主动监控,能恢复瞬时故障(429/502/503/504)、上下文耗尽、额度限制——比 hook 能触及的更深 |
| [kur114/codex-auto-continue-hook](https://github.com/kur114/codex-auto-continue-hook) | Stop hook + 关键词匹配 | 思路相同,但关键词硬匹配脆弱;我们用证据驱动(工具调用 + `任务完成`/`需要用户` 协议 + 静默/预算兜底)更抗误判 |

**已知局限**:我们运行在 Stop hook 内部,只能在一回合正常结束并触发 hook 时介入。
如果连接中断但回合没有正常收尾(没有触发 Stop 事件),看门狗无法自己重启它——这类
故障需要进程外的监督器(如 `flowing-water1/codex-watchdog` 的方案)。

### License

MIT
