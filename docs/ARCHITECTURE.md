# 架构说明

## 触发链

看门狗注册三个 hook 事件,共用同一个入口 `watchdog.py`(从 stdin 读 JSON):

| 事件 | 作用 | 输出 |
|---|---|---|
| `Stop` | 回合结束,判断继续还是停 | `{"decision":"block","reason":...}` 续推 / `{"continue":true}` 停 |
| `PreCompact` | 上下文即将被压缩,落检查点 | 无(不需要决策) |
| `SessionStart` | 压缩后重新会话,注入恢复上下文 | 纯文本 stdout(上下文类事件的官方载体) |

```
turn ends ──> Stop ──> continue? ──> yes ──> block, keep going
                          │
                          └── no ──> turn ends
                                        │
context fills ──> PreCompact ──> checkpoint(goal + next step)
                                        │
                             SessionStart(source=compact) ──> inject context ──> resume
```

## 决策顺序(优先级从高到低)

1. **声明协议**:整行声明 `任务完成`/`『任务完成』`/`Task Complete`(可跟标点与简短说明)→ 停;
   整行声明 `需要用户`/`『需要用户』`/`Need User` → 停(等待用户)。
   进度叙述(`已完成 3/7 个文件,还剩 4 个`、`任务完成度约 60%,继续推进`)**不算声明**,
   看门狗会继续推进 —— 见下方「声明协议为什么要独占一行」。
2. **突发预算**:连续 `CODEX_WATCHDOG_MAX`(60)次自动续推、期间无真实用户输入 → 停(防失控)。
   实际上限取 `min(CODEX_WATCHDOG_MAX, CODEX_WATCHDOG_HOST_CAP)`,因为宿主可能有自己的拦截上限。
3. **重复消息**:最终消息与上次完全相同 → 停(无进展)。
4. **短请求需用户**:长度 ≤160 且是明确的请求句(`请你提供...`/`等待你的指示`)-> 停。
5. **工具证据**:本次 turn 内存在 `function_call` → 继续推(还在干活)。
6. **安静回合**:连续 `CODEX_WATCHDOG_QUIET`(3)轮无工具调用 → 停(打转)。
7. **经典完成词**:安静回合 + 命中完成短语(`全部完成`/`all done`...) → 停。
8. 其余 → 继续推,并重置安静计数。

## 关于 `stop_hook_active`

两个宿主都会在 Stop 输入里传 `stop_hook_active`:当这次 Stop **本身**是由上一次 block
产生的续推时,它为 true。

它**不是**停止条件 —— 它恰好说明「上一轮续推生效了」,如果据此停止,自动续推在第一轮就会结束。
它的两个真实用途:

1. **漂移检测**:宿主说已经在续推,而我们的计数是 0(状态被 TTL 清理、状态目录被清空),
   说明计数丢了。此时按宿主信号把计数同步到 ≥1,避免不知不觉越过宿主的 block 上限。
2. **信号记录**:计入 `host_continuations`,区分「宿主接管的轮次」和「我们主动发起的轮次」。

宿主上限由 `CODEX_WATCHDOG_HOST_CAP` 处理,不由这个字段触发停止。

## 声明协议为什么要独占一行

声明检测最初写成「匹配行首的 `任务完成|已完成|...`」,结果 `已完成初步分析,下面开始实现`
这类最常见的进度叙述会被当成「任务已结束」,而这条判定优先级高于工具证据,于是看门狗
在任务中途就放手了 —— 恰好是它要解决的问题本身。

现在的判据是:`watchdog_protocol.py` 里要求声明标记**独占一行**(行尾或标点 + 简短说明),
且标记之后不能出现 `度/率/部分/初步/继续/接下来/还/剩/remains/next/...` 这类反证词。
`watchdog_protocol.py` 是唯一事实来源,引擎与两个 adapter 都从这里导入,避免词表漂移。

## 预算重置

- 最后一条**真实用户消息**(非 `[watchdog]` 注入、非 `<environment_context>`/`<turn_aborted>` 噪声)出现 → 预算归零。
- 空闲超过 `CODEX_WATCHDOG_RESET`(1800s)→ 预算归零。

## 状态与日志

- `/tmp/codex-watchdog/<session_id>.json` — 每个会话的 `count / last_continue_at / last_msg / quiet_turns / activated / status / updated_at / reason / goal / next_step / host_continued`
- `/tmp/codex-watchdog.log` — 追加式日志,记录每次决策
- `/tmp/codex-watchdog/events.jsonl` — 结构化事件流(JSONL)
- `/tmp/codex-watchdog/checkpoints/<session_id>.json` — 轻量检查点
- `/tmp/codex-watchdog/metrics.json` — 由事件流派生的聚合指标

## Runtime v1.1:状态机 / 事件流 / 检查点 / 指标

### 任务状态机(TaskState)

每个会话在状态文件中维护一个 `status`,取值:

| 状态 | 含义 | 进入条件 |
|---|---|---|
| `RUNNING` | 正常推进 | 每次自动续推 |
| `BLOCKED` | 等待用户 | 声明 `需要用户`/`Need User`,或短请求需用户、抛可选后续 |
| `COMPLETED` | 任务结束 | 声明 `任务完成`/`Task Complete`,或安静回合 + 完成短语 |
| `STALLED` | 无进展 | 突发预算耗尽、重复最终消息、连续安静回合 |

状态迁移统一由 `stop()`(进入终态)与 `keep_going()`(进入 RUNNING)两个入口完成,
每次迁移都会写 `status / updated_at / reason` 到状态文件。

### 结构化事件流(Event Log)

`events.jsonl` 每行一个事件,字段:`timestamp / session_id / event / state / reason / continue_count`
(`timestamp` 为 Unix 秒,便于机器处理;人类可读时间在检查点与日志里)。

事件类型:`task_started`、`continue`、`blocked`、`completed`、`stalled`,
以及上下文生命周期事件 `precompact`、`resume`。
会话首次出现时写 `task_started`;每次续推写 `continue`;每次进入终态写对应事件。

### 轻量检查点(Checkpoint)

`checkpoints/<session_id>.json` 保存最近一次快照:

- 最近 assistant message
- 当前 TaskState
- continue 次数
- quiet_turns
- 最近工具统计(tool_calls / tool_names,来自 adapter 的 `checkpoint_metadata`)
- 决策原因

触发条件:每次状态变化立即保存;每 `CODEX_WATCHDOG_CHECKPOINT`(默认 5)次续推保存一次。
不保存完整 transcript,不生成 LLM 摘要。

### Resume Engine:跨越上下文压缩

长任务最终会撞上上下文窗口。**Stop hook 里读不到窗口占用**——上下文用量不在 hook 契约里
(相关功能请求被官方关闭为 not planned)。但两个宿主都暴露了压缩事件,所以看门狗把
「上下文快满了」当作**事件**而不是数字:

| 事件 | 做什么 |
|---|---|
| `PreCompact` | 写检查点:`goal`(用户最近一次真实请求)+ `next_step`(当前进度)+ 状态与计数,并标记 `for_resume: true` |
| `SessionStart(source=compact)` | 把该检查点作为上下文打印回会话,让压缩后的 agent 接着做而不是从头做 |

设计要点:

- **`goal` 取用户最近一次真实请求**,不是第一条。长会话可连续包含多个任务,
  压缩后把旧任务当目标会把 agent 赶回已完成的工作。因此每次 `PreCompact` 都用
  `last_user_prompt` 刷新(仅当与上次不同才记日志),`[watchdog]` 注入不计入。
  首次(还没有真实用户消息)才退回 `first_user_prompt`。
- **`next_step` 优先用 hook 的 `last_assistant_message`**;`PreCompact` 不一定带这个字段,
  此时回退到读 transcript(adapter 的 `last_assistant_text`),否则检查点里
  「我们进行到哪了」会是空的。
- **状态不为空**:`PreCompact` 可能早于该会话的第一个 `Stop` 触发,此时状态文件里没有
  `status`;检查点写入 `RUNNING` 作为兜底,避免恢复文本出现 `recorded status: None`。
- **指向性的一次性**:注入后把 `for_resume` 置为 false。普通 `resume`/`startup` 的
  `SessionStart` 不重放,避免过期指令污染后续会话。
- **输出用纯文本 stdout**,因为这是官方文档里上下文类事件(Claude 的
  `UserPromptSubmit`/`SessionStart`、Codex 的 `SessionStart` 等)的载体;
  避免去猜 `hookSpecificOutput` 的确切结构。
- **统一启用开关**:三个事件都走 `session_activated()`。关闭或未启用的会话不读
  transcript、不写盘,连状态目录都不创建 —— 一个被关掉的工具继续记录用户输入是不可接受的。
  为此 `maybe_cleanup()` 在状态目录不存在时直接返回。

边界:它注入的是**目标与下一步**,不是工作内容的摘要。真正恢复工作内容必须由 agent
自己写进检查点,而不是由 hook 去推断。

### 指标(Metrics)

`metrics.json` 由事件流派生,分两层:每个会话一行 `sessions.<sid>`,外加 `totals` 汇总:

- `continues` — 该会话续推次数
- `completed` / `blocked` / `stalled` — 停止原因分布
- `quiet_turn_hits` — 安静回合命中次数
- `stalled_recovered` — STALLED 后恢复续推次数
- `completed_then_continued` — COMPLETED 后又续推次数
- `blocked_then_continued` — BLOCKED 后又续推次数
- `compactions` / `resumes` — 压缩与恢复次数
- `host_continuations` — 宿主报告「已在续推」的轮次

**计数必须落成事件。** 指标每次 hook 调用都从事件流重建,任何只存在内存里的计数器
都会被重置 —— `host_continuations` 曾因此永远停在 1,改成写 `host_continue` 事件后
才正确累加。该事件描述的是**同一轮**(前面那次 block 造成的续推),所以它不更新
`last_event`,否则会静默破坏 `stalled_recovered` / `completed_then_continued` 这类
依赖「上一个事件」的恢复计数。

聚合是**增量**的:每个事件只更新当前会话的计数器(O(1)),再合并进全局文件。
早期版本在每次 `emit_event()` 里重读重解析整个 `events.jsonl`(O(n2)),而且把
不同会话混在一起平均,现在两者都已修正。

### 有界性(Bounded state)

- `events.jsonl` 超过 `CODEX_WATCHDOG_EVENTS_MAX`(默认 2 MB)→ 轮转为 `events.jsonl.1`。
- 会话状态与检查点空闲超过 `CODEX_WATCHDOG_TTL`(默认 7 天)→ 清理(每小时最多一次)。
- 状态文件用临时文件 + `os.replace` 原子写入,崩溃不会留下半个状态文件而丢掉预算。

### 宿主上限(Host block cap)

Codex 与 Claude Code 都会在 hook 输入里传 `stop_hook_active`。Claude Code 在连续
8 次 `decision:"block"` 后会**自己结束回合**(`CLAUDE_CODE_STOP_HOOK_BLOCK_CAP`,默认 8);
Codex 没有对应的宿主上限。因此:

- `CODEX_WATCHDOG_HOST_CAP` 在 Claude 下默认 8,实际预算取 `min(MAX, HOST_CAP)`,
  避免设置一个永远达不到的 60 并导致 `STALLED` 状态不可达。
- Codex 下默认 0(不启用),完全依赖 `CODEX_WATCHDOG_MAX` 自我约束 —— 这一点很重要,
  因为 Codex 侧没有兜底。

### 无崩溃保证(Never crash the hook)

`CODEX_WATCHDOG_*` 环境变量解析失败(非法值、空串、越界)只会在 stderr 提示并退回默认值。
hook 崩溃会直接破坏用户的一次回合,这比配置写错严重得多。

## Adapter 扩展点

`BaseAdapter` 提供:

| 方法 | 用途 |
|---|---|
| `checkpoint_metadata(ev)` | 标准化回合快照:`session_id` / `final_message` / `tool_calls` / `tool_names` / `completion_signal` / `need_user_signal` |
| `first_user_prompt(ev)` | 会话第一条真实用户消息,仅在还没有真实用户消息时作为 `goal` 兜底 |
| `last_assistant_text(ev)` | transcript 里的最后一条 assistant 消息,`PreCompact` 缺字段时的回退 |

引擎在每次 Stop hook 里调用 `checkpoint_metadata`,结果直接写进检查点,
所以检查点里的 `tool_calls`/`completion_signal` 就是 adapter 视角的真实值。
`codex.py` 与 `claude.py` 均已实现;自定义 adapter 只需实现这些方法。

两个 adapter 都从 `watchdog_protocol.py` 取声明判定,因此**不会**与引擎分叉。
transcript 在单次进程内只解析一次(`adapters/base.py` 的 `cached_rows`),
三个问题共享同一份已解析行,较小的窗口从较大窗口切片得到。

## 可移植性

- 纯 Python 标准库,无第三方依赖。
- 仅依赖 `~/.codex/watchdog.enabled` 启用标记(通过 `expanduser` 解析)。
- 状态根目录:POSIX 下保持 `/tmp/codex-watchdog`(兼容既有安装与文档);
  Windows 下回退到 `tempfile.gettempdir()`,可用 `CODEX_WATCHDOG_STATE_DIR` 覆盖。
- hook 入口为 `~/.codex/watchdog.py`,同级需一并安装 `watchdog_protocol.py` 与 `adapters/`
  (install.sh 已处理)。
