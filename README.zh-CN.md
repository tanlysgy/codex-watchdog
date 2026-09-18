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

> 给被监控 agent 的约定:一回合真正做完就声明 `任务完成`;需要用户输入、选择、
> 凭据,或想做可选事项时需要用户拍板,就声明 `需要用户` 并停下。看门狗会立刻尊重
> 这些声明。

| 信号 | 行为 |
|---|---|
| 本回合调用了工具(`exec_command`/`apply_patch`...) | 继续(还在干活) |
| 模型声明 `『任务完成』` / `任务完成` | 停 |
| 模型声明 `『需要用户』` / `需要用户:` | 停(等你输入) |
| 连续 3 回合无工具调用 | 停(原地打转) |
| 同一句最终消息重复 | 停(无进展) |
| 短请求("请你提供 API key") | 停(真在等你) |
| agent 抛可选后续或提问("要不要我把 X 也做了?") | 停(等你拍板) |
| 连续 60 次自动续推、无用户输入 | 停(兜底) |
| 空闲超过 30 分钟 | 重置预算 |

声明只在**独占一行**时才成立(可跟标点与简短说明)。像
`已完成 3/7 个文件,还剩 4 个` 或 `任务完成度约 60%,继续推进` 这类进度叙述
**不算**完成声明,看门狗会继续推进。

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
python3 watchdog_test.py    # 回归测试(112 项)
python3 adapters_test.py    # adapter 测试(46 项)
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

### 可观测性与运行时有界性

看门狗为每个会话维护一个轻量任务状态机(`RUNNING` / `BLOCKED` / `COMPLETED` /
`STALLED`),并写入结构化事件流与检查点:

- 事件流:`/tmp/codex-watchdog/events.jsonl`(`task_started` / `continue` / `blocked` /
  `completed` / `stalled` / `precompact` / `resume`)
- 检查点:`/tmp/codex-watchdog/checkpoints/<session_id>.json`
- 指标:`/tmp/codex-watchdog/metrics.json`(按会话 + 总计,增量聚合)

运行时有界,不会无限增长:事件流超过 `CODEX_WATCHDOG_EVENTS_MAX`(默认 2 MB)会轮转;
空闲超过 `CODEX_WATCHDOG_TTL`(默认 7 天)的会话状态与检查点会被清理;状态文件原子写入,
崩溃不会破坏续推预算。环境变量写错(非法值、空串、越界)只会退回默认值,不会让 hook 崩溃。

### 上下文压缩后自动续接(Resume Engine)

长任务最终会撞上上下文窗口。Stop hook 里读不到窗口占用,但两个宿主都暴露了压缩事件,
所以看门狗把「上下文快满了」当作**事件**处理:

| 事件 | 做什么 |
|---|---|
| `PreCompact` | 写检查点:目标(用户最近一次真实请求)+ 下一步 + 状态计数 |
| `SessionStart(source=compact)` | 把该检查点作为上下文打印回会话,压缩后接着做而不是重来 |

注入示例:

```
[watchdog] This session was compacted mid-task. Resume it:
- goal: 把 README 的对比表更新一下
- next step: 改完表格,接着验证链接
- recorded status: RUNNING (auto-continues so far: 3)
Continue from the next step instead of restarting. ...
```

目标跟着**当前**任务走,而不是会话的第一个任务:长会话里可以连续有多个请求,压缩后把旧任务
当成目标会把 agent 赶回已经做完的工作。`[watchdog]` 注入不会被当成目标。

恢复指向只生效一次,普通 `resume`/`startup` 不会重放,避免过期指令污染后续会话。

所有事件(含 `PreCompact` / `SessionStart`)都遵守与 `Stop` 相同的启用开关。看门狗关闭
或该会话从未启用时,不读 transcript、不写任何东西 —— 关闭状态下连状态目录都不会创建。

`bash install.sh` 会同时注册 `Stop` / `PreCompact` / `SessionStart`,并且是幂等的:
只装过 Stop 的老安装重跑一次即可补上新事件,已有条目不会被改动。

这刻意不是完整 resume:注入的是目标与下一步,不是工作内容的摘要。恢复工作内容得由 agent
自己写进检查点,而不是由 hook 推断。

**注意宿主上限**:Claude Code 在连续 8 次 `decision:"block"` 后会自己结束回合
(`CLAUDE_CODE_STOP_HOOK_BLOCK_CAP`),所以在其上默认取 `min(60, 8)`;Codex 没有这个上限,
完全依赖 `CODEX_WATCHDOG_MAX` 自我约束。详见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

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
