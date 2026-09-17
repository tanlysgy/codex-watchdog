# 架构说明

## 触发链

Codex 每回合结束会触发 `Stop` hook → 执行 `watchdog.py`(从 stdin 读 JSON)→ 脚本决定返回 `{"decision":"block","reason":...}`(续推)或 `{"continue":true}`(停)。

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

- `/tmp/codex-watchdog/<session_id>.json` — 每个会话的 `count / last_continue_at / last_msg / quiet_turns / activated / status / updated_at / reason`
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

事件类型:`task_started`、`continue`、`blocked`、`completed`、`stalled`。
会话首次出现时写 `task_started`;每次续推写 `continue`;每次进入终态写对应事件。

### 轻量检查点(Checkpoint)

`checkpoints/<session_id>.json` 保存最近一次快照:

- 最近 assistant message
- 当前 TaskState
- continue 次数
- quiet_turns
- 最近工具统计(tool_calls / tool_names)
- 决策原因

触发条件:每次状态变化立即保存;每 `CODEX_WATCHDOG_CHECKPOINT`(默认 5)次续推保存一次。
不保存完整 transcript,不生成 LLM 摘要。

### 指标(Metrics)

`metrics.json` 由事件流派生,分两层:每个会话一行 `sessions.<sid>`,外加 `totals` 汇总:

- `continues` — 该会话续推次数
- `completed` / `blocked` / `stalled` — 停止原因分布
- `quiet_turn_hits` — 安静回合命中次数
- `stalled_recovered` — STALLED 后恢复续推次数
- `completed_then_continued` — COMPLETED 后又续推次数
- `blocked_then_continued` — BLOCKED 后又续推次数

聚合是**增量**的:每个事件只更新当前会话的计数器(O(1)),再合并进全局文件。
早期版本在每次 `emit_event()` 里重读重解析整个 `events.jsonl`(O(n²)),而且把
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

`BaseAdapter` 提供 `checkpoint_metadata(ev)` 返回标准化快照:

- `session_id`
- `final_message`
- `tool_calls` / `tool_names`
- `completion_signal` / `need_user_signal`

引擎在每次 Stop hook 里调用它,结果直接写进检查点(`write_checkpoint`),
所以检查点里的 `tool_calls`/`completion_signal` 就是 adapter 视角的真实值。
`codex.py` 与 `claude.py` 均已实现;自定义 adapter 只需实现该方法。

两个 adapter 都从 `watchdog_protocol.py` 取声明判定,因此**不会**与引擎分叉。

## 可移植性

- 纯 Python 标准库,无第三方依赖。
- 仅依赖 `~/.codex/watchdog.enabled` 启用标记(通过 `expanduser` 解析)。
- 状态根目录:POSIX 下保持 `/tmp/codex-watchdog`(兼容既有安装与文档);
  Windows 下回退到 `tempfile.gettempdir()`,可用 `CODEX_WATCHDOG_STATE_DIR` 覆盖。
- hook 入口为 `~/.codex/watchdog.py`,同级需一并安装 `watchdog_protocol.py` 与 `adapters/`
  (install.sh 已处理)。
