#!/usr/bin/env bash
set -euo pipefail

# Codex Watchdog 一键安装脚本
# 用法:
#   bash install.sh              # 安装到 Codex (Linux/macOS)
#   bash install.sh --agent claude   # 安装到 Claude Code
#   bash install.sh --uninstall
# 假设目标目录已存在 (Codex: ~/.codex, Claude: ~/.claude)

AGENT="codex"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --agent)
      if [ "$#" -lt 2 ]; then
        echo "错误: --agent 需要 codex 或 claude" >&2
        exit 2
      fi
      AGENT="$2"
      shift 2
      continue
      ;;
    --agent=*) AGENT="${1#--agent=}" ;;
    --uninstall) UNINSTALL=1 ;;
    *)
      echo "错误: 未知参数 $1" >&2
      exit 2
      ;;
  esac
  shift
done

if [ "$AGENT" != "codex" ] && [ "$AGENT" != "claude" ]; then
  echo "错误: agent 必须是 codex 或 claude" >&2
  exit 2
fi

OUT_DIR=""
DEST=""
if [ "$AGENT" = "claude" ]; then
  DEST="${CLAUDE_HOME:-$HOME/.claude}"
else
  OUT_DIR="${CODEX_HOME:-$HOME/.codex}"
  DEST="$OUT_DIR"
fi
SRC="$(cd "$(dirname "$0")" && pwd)"

if [ -n "${UNINSTALL:-}" ]; then
  echo "==> 卸载:删除 $DEST/watchdog.* 与 adapters/ =="
  rm -f "$DEST/watchdog.py" "$DEST/watchdog_protocol.py" "$DEST/watchdog_test.py" "$DEST/watchdog.enabled"
  rm -f "$DEST/watchdog_recovery.py" "$DEST/watchdog_rpc.py"
  rm -rf "$DEST/adapters"
  echo "  已删除。请手动从 $DEST/hooks.json(Codex)或 $DEST/settings.json(Claude)移除 watchdog 条目。"
  exit 0
fi

mkdir -p "$DEST"
DEST="$(cd "$DEST" && pwd)"
echo "==> 1/5 复制 watchdog.py + watchdog_protocol.py → $DEST/"
cp "$SRC/watchdog.py" "$DEST/watchdog.py"
cp "$SRC/watchdog_protocol.py" "$DEST/watchdog_protocol.py"
cp "$SRC/watchdog_recovery.py" "$DEST/watchdog_recovery.py"
cp "$SRC/watchdog_rpc.py" "$DEST/watchdog_rpc.py"
chmod +x "$DEST/watchdog.py"

echo "==> 2/5 复制 adapters/ → $DEST/"
cp -r "$SRC/adapters" "$DEST/"

echo "==> 3/5 复制测试脚本(可选) → $DEST/"
cp "$SRC/watchdog_test.py" "$DEST/watchdog_test.py"

if [ "$AGENT" = "claude" ]; then
  TARGET="$DEST/settings.json"; FNAME="Claude Code"
  CMD="CODEX_WATCHDOG_ADAPTER=claude python3 $(printf '%q' "$DEST/watchdog.py")"
else
  TARGET="$DEST/hooks.json"; FNAME="Codex"
  CMD="python3 $(printf '%q' "$DEST/watchdog.py")"
fi

echo "==> 4/5 注册 $FNAME hooks(Stop + PreCompact + SessionStart)→ $TARGET =="
# Idempotent: registers each missing event and refreshes existing watchdog
# entries, while leaving unrelated hooks alone.
python3 - "$TARGET" "$CMD" <<'PY'
import json, os, stat, sys, tempfile

path, command = os.path.realpath(sys.argv[1]), sys.argv[2]
data = {}
if os.path.exists(path):
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"错误:无法读取现有配置 {path}: {exc}")
if not isinstance(data, dict):
    raise SystemExit(f"错误:现有配置 {path} 不是 JSON 对象,已停止以避免覆盖")

hooks = data.get("hooks")
if "hooks" not in data:
    hooks = {}
    data["hooks"] = hooks
elif not isinstance(hooks, dict):
    raise SystemExit(f"错误:现有配置中的 hooks 不是对象,已停止以避免覆盖")
entry = {"type": "command", "command": command, "timeout": 30}
# One "hooks" wrapper per event; the matcher is left empty (match everything)
# because the watchdog filters on trigger/source itself.
wanted = ("Stop", "PreCompact", "SessionStart")
added = []
for event in wanted:
    existing = hooks.get(event)
    if event not in hooks:
        existing = []
        hooks[event] = existing
    elif not isinstance(existing, list):
        raise SystemExit(f"错误:现有 {event} 配置不是数组,已停止以避免覆盖")
    found = False
    for wrapper in existing:
        if not isinstance(wrapper, dict):
            continue
        nested = wrapper.get("hooks")
        if not isinstance(nested, list):
            continue
        refreshed = False
        for hook in nested:
            if isinstance(hook, dict) and "watchdog.py" in str(hook.get("command", "")):
                hook.update(entry)
                refreshed = True
        found = found or refreshed
    if found:
        added.append(event + "(updated)")
    else:
        existing.append({"matcher": "", "hooks": [dict(entry)]})
        added.append(event)

directory = os.path.dirname(path) or "."
os.makedirs(directory, exist_ok=True)
tmp_path = None
try:
    fd, tmp_path = tempfile.mkstemp(prefix=".hooks-", suffix=".tmp", dir=directory)
    if os.path.exists(path):
        os.fchmod(fd, stat.S_IMODE(os.stat(path).st_mode))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp_path, path)
except Exception:
    if tmp_path:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
    raise
print("  已注册: " + (", ".join(added) if added else "(全部已存在,未改动)"))
PY

echo "==> 5/5 创建启用标记"
touch "$DEST/watchdog.enabled"
echo "  OK"
if [ "$AGENT" = "codex" ]; then
  CODEX_HOME="$DEST" python3 "$DEST/watchdog_recovery.py" --start
fi

echo "==== 安装完成($AGENT)===="
echo "下一步:在 Codex 里运行 /hooks 并信任 watchdog.py;"
echo "      在 Claude Code 里首次触发 hook 时选 Trust & run。"
echo "验证:tail /tmp/codex-watchdog.log (看到 continue #1 即生效)"
echo ""
