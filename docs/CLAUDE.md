# 在 Claude Code 中使用看门狗

Claude Code 的 Stop hook 与 Codex 语义一致:`decision:"block"` + `reason` 会让 Claude 继续工作。
核心 `watchdog.py` 无需修改,只需:

1. 选择 claude adapter(默认按 env 切换)
2. 把脚本注册到 Claude Code 的 hook 配置

## 1. 安装脚本

```bash
# 克隆仓库(或从 Codex 那边拷 watchdog.py + adapters/)
git clone https://github.com/tanlysgy/codex-watchdog
cd codex-watchdog
# 复制到 ~/.claude/ 或任意路径
mkdir -p ~/.claude
cp -r watchdog.py adapters ~/.claude/
touch ~/.claude/watchdog.enabled
```

## 2. 注册 hook

编辑 `~/.claude/settings.json`,加入:

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "CODEX_WATCHDOG_ADAPTER=claude python3 ~/.claude/watchdog.py",
            "timeout": 30
          }
        ]
      }
    ]
  }
}
```

或者在项目级 `.claude/settings.json` 中配置(只对当前项目生效)。

> 注意:Claude Code 的 hook 配置里命令通过 shell 执行,环境变量前缀写法在大多数
> shell 下可用。若你的 shell 不认 `KEY=val cmd`,请写成
> `python3 ~/.claude/watchdog.py` 并在脚本里设默认,或在 `settings.json` 的同级
> `.env`/shell profile 中 export `CODEX_WATCHDOG_ADAPTER=claude`。

## 3. 信任/权限

Claude Code 的 command hooks 默认需要显式授权/信任。首次运行 Stop hook 时会弹出
确认(Trust & run),选确认。若要跳过所有确认(危险),用 `--dangerously-skip-permissions`
仅在你信任本机脚本时使用。

## 4. 验证

给 Claude 一个简单任务,然后:

```bash
tail /tmp/codex-watchdog.log
```

看到 `continue #N (tools=…)` 即生效。日志文件与 Codex 共用同一个,状态也按
session_id 区分,互不干扰。

## 状态隔离

- 状态:`/tmp/codex-watchdog/<claude_session_id>.json`
- 与 Codex 会话共用目录,但 session_id 前缀不同,天然隔离。

## 卸载

```bash
rm -rf ~/.claude/watchdog.py ~/.claude/adapters ~/.claude/watchdog.enabled
# 并从 settings.json 移除 Stop 的 hook 条目
```
