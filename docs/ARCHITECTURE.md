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

- `/tmp/codex-watchdog/<session_id>.json` — 每个会话的 `count / last_continue_at / last_msg / quiet_turns / activated`
- `/tmp/codex-watchdog.log` — 追加式日志,记录每次决策

## 可移植性

- 纯 Python 标准库,无第三方依赖。
- 仅依赖 `~/.codex/watchdog.enabled` 启用标记(通过 `expanduser` 解析)。
- Windows 下 `/tmp` 需改为 `%TEMP%`,`command` 需指向本地 python。
