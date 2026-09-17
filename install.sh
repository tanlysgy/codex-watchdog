#!/usr/bin/env bash
set -euo pipefail

# Codex Watchdog 一键安装脚本
# 用法:
#   bash install.sh              # 安装到 Codex (Linux/macOS)
#   bash install.sh --agent claude   # 安装到 Claude Code
#   bash install.sh --uninstall
# 假设目标目录已存在 (Codex: ~/.codex, Claude: ~/.claude)

AGENT="codex"
for arg in "$@"; do
  case "$arg" in
    --agent) AGENT="codex" ;;  # 由下一个 --agent= 覆盖;兼容旧用法
    --agent=*) AGENT="${arg#--agent=}" ;;
    --uninstall) UNINSTALL=1 ;;
  esac
done

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
  rm -rf "$DEST/adapters"
  echo "  已删除。请手动从 $DEST/hooks.json(Codex)或 $DEST/settings.json(Claude)移除 watchdog 条目。"
  exit 0
fi

mkdir -p "$DEST"
echo "==> 1/5 复制 watchdog.py + watchdog_protocol.py → $DEST/"
cp "$SRC/watchdog.py" "$DEST/watchdog.py"
cp "$SRC/watchdog_protocol.py" "$DEST/watchdog_protocol.py"
chmod +x "$DEST/watchdog.py"

echo "==> 2/5 复制 adapters/ → $DEST/"
cp -r "$SRC/adapters" "$DEST/"

echo "==> 3/5 复制测试脚本(可选) → $DEST/"
cp "$SRC/watchdog_test.py" "$DEST/watchdog_test.py"

if [ "$AGENT" = "claude" ]; then
  TARGET="$DEST/settings.json"; FNAME="Claude Code"; CMD="CODEX_WATCHDOG_ADAPTER=claude python3 ~/.claude/watchdog.py"
else
  TARGET="$DEST/hooks.json"; FNAME="Codex"; CMD="python3 ~/.codex/watchdog.py"
fi

echo "==> 4/5 注册 $FNAME hooks(Stop + PreCompact + SessionStart)→ $TARGET =="
# Idempotent: registers each missing event, leaves existing entries alone, so
# upgrading an install that only has Stop adds the context-lifecycle events.
python3 - "$TARGET" "$CMD" <<'PY'
import json, os, sys

path, command = sys.argv[1], sys.argv[2]
data = {}
if os.path.exists(path):
    try:
        with open(path) as f:
            data = json.load(f)
    except ValueError:
        data = {}
if not isinstance(data, dict):
    data = {}

hooks = data.setdefault("hooks", {})
entry = {"type": "command", "command": command, "timeout": 30}
# One "hooks" wrapper per event; the matcher is left empty (match everything)
# because the watchdog filters on trigger/source itself.
wanted = ("Stop", "PreCompact", "SessionStart")
added = []
for event in wanted:
    existing = hooks.setdefault(event, [])
    if any("watchdog" in json.dumps(e) for e in existing):
        continue
    existing.append({"matcher": "", "hooks": [dict(entry)]})
    added.append(event)

os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
with open(path, "w") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
    f.write("\n")
print("  已注册: " + (", ".join(added) if added else "(全部已存在,未改动)"))
PY

echo "==> 5/5 创建启用标记"
touch "$DEST/watchdog.enabled"
echo "  OK"

echo "==== 安装完成($AGENT)===="
echo "下一步:在 Codex 里运行 /hooks 并信任 watchdog.py;"
echo "      在 Claude Code 里首次触发 hook 时选 Trust & run。"
echo "验证:tail /tmp/codex-watchdog.log (看到 continue #1 即生效)"
echo ""
