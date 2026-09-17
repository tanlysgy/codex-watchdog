# 架构说明

## 触发链

Codex 每回合结束会触发 `Stop` hook → 执行 `watchdog.py`(从 stdin 读 JSON)→ 脚本决定返回 `{"decision":"block","reason":...}`(续推)或 `{"continue":true}`(停)。

## 决策顺序(优先级从高到低)

1. **声明协议**:消息以 `任务完成`/`『任务完成』` 开头 → 停;以 `需要用户`/`『需要用户』` 开头 → 停。
2. **突发预算**:连续 `CODEX_WATCHDOG_MAX`(60)次自动续推、期间无真实用户输入 → 停(防失控)。
3. **重复消息**:最终消息与上次完全相同 → 停(无进展)。
4. **短请求需用户**:长度 ≤160 且是明确的请求句(`请你提供…`/`等待你的指示`)-> 停。
5. **工具证据**:本次 turn 内存在 `function_call` → 继续推(还在干活)。
6. **安静回合**:连续 `CODEX_WATCHDOG_QUIET`(3)轮无工具调用 → 停(打转)。
7. **经典完成词**:安静回合 + 命中完成短语(`全部完成`/`all done`…) → 停。
8. 其余 → 继续推,并重置安静计数。

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

`events.jsonl` 每行一个事件,字段:`timestamp / session_id / event / state / reason / continue_count`。

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

`metrics.json` 由事件流派生,不维护第二套存储:

- `total_continues` — 总续推次数
- `average_continue_rounds` — 平均续推轮数
- `stop_reason_distribution` — 停止原因分布(completed/blocked/stalled)
- `quiet_turn_hits` — 安静回合命中次数
- `stalled_recovered` — STALLED 后恢复续推次数
- `completed_then_continued` — COMPLETED 后又续推次数
- `blocked_then_continued` — BLOCKED 后又续推次数

### Adapter 扩展点

`BaseAdapter` 新增 `checkpoint_metadata(ev)` 返回标准化快照:

- `session_id`
- `final_message`
- `tool_calls` / `tool_names`
- `completion_signal` / `need_user_signal`

`codex.py` 与 `claude.py` 均已实现;自定义 adapter 只需实现该方法即可获得一致的检查点元数据。

## 可移植性

- 纯 Python 标准库,无第三方依赖。
- 仅依赖 `~/.codex/watchdog.enabled` 启用标记(通过 `expanduser` 解析)。
- Windows 下 `/tmp` 需改为 `%TEMP%`,`command` 需指向本地 python。
