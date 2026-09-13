# Codex Watchdog

让 Codex 在任务未完成时自动继续(`continue`),而不是停在半路等你手动推。

> 背景:Codex 偶尔会在任务没做完时就结束回合(尤其上下文压缩后),需要你反复输入 `continue`。
> 本项目在 **Stop hook** 的机制上做一个"看门狗":每回合结束自动检查,没干完就自动续推,干完了/卡住了/在打转就停。

## 它怎么判断"干完没"

不是猜一句话,而是证据驱动:

| 信号 | 行为 |
|---|---|
| 本回合真实调用了工具(`exec_command`/`apply_patch`...) | **继续推**(还在干活) |
| 模型明确说 `『任务完成』` / `任务完成` | **停** |
| 模型明确说 `『需要用户』` / `需要用户:` | **停**(等你提供信息/凭据) |
| 连续 3 个回合没调用任何工具 | **停**(在原地打转) |
| 同一句最终消息重复出现 | **停**(无进展) |
| 消息是明确的短请求("请你提供 API key") | **停**(真在等你) |
| 连续 60 次自动续推、期间无真实用户输入 | **停**(兜底防失控) |
| 空闲超过 30 分钟 | 重置续推预算 |

## 安装

### 方式一:一键脚本(Linux/macOS)

```bash
git clone https://github.com/tanlysgy/codex-watchdog
cd codex-watchdog
bash install.sh
```

`install.sh` 会:
1. 复制 `watchdog.py` → `~/.codex/`
2. 注册 `[[hooks.Stop]]` 到 `~/.codex/config.toml`
3. 创建 `~/.codex/watchdog.enabled` 启用标记

### 方式二:手动

把 `watchdog.py` 放到 `~/.codex/`,在 `~/.codex/config.toml` 加:

```toml
[[hooks.Stop]]
[[hooks.Stop.hooks]]
type = "command"
command = "python3 ~/.codex/watchdog.py"
timeout = 30
```

然后 `touch ~/.codex/watchdog.enabled`

### 信任 hook(必做)

Codex 对非托管 hook 有信任机制,未信任会被**静默跳过**。在 Codex CLI/TUI 里运行:

```
/hooks
```

找到 `watchdog.py` 那条,给它信任。

## 验证

跑一个简单任务,然后:

```bash
tail /tmp/codex-watchdog.log
```

看到 `continue #1` 就说明生效了。

回归测试:

```bash
python3 watchdog_test.py
```

## 配置(环境变量)

| 变量 | 默认 | 含义 |
|---|---|---|
| `CODEX_WATCHDOG_MAX` | `60` | 单次突发自动续推上限 |
| `CODEX_WATCHDOG_RESET` | `1800` | 空闲多少秒后重置预算 |
| `CODEX_WATCHDOG_QUIET` | `3` | 连续多少回合无工具调用则判定打转 |

## 状态与日志

- 状态:`/tmp/codex-watchdog/<session_id>.json`
- 日志:`/tmp/codex-watchdog.log`

末尾几行含义:
- `declared done` / `done` — 模型声明/确认真完成,正常停
- `needs user` / `declared need-user` — 在等你输入,正常停
- `repeated final message` — 原地重复,正常停
- `N quiet turns in a row` — 连续没动手,正常停
- `burst exhausted` — 连续 60 次无输入,兜底停

## 卸载

```bash
rm ~/.codex/watchdog.py ~/.codex/watchdog.enabled
# 并从 ~/.codex/config.toml 移除 [[hooks.Stop]] 那几行
```

## License

MIT
