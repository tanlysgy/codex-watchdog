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
  rm -f "$DEST/watchdog.py" "$DEST/watchdog_test.py" "$DEST/watchdog.enabled"
  rm -rf "$DEST/adapters"
  echo "  已删除。请手动从 $DEST/config.toml 移除 [[hooks.Stop]] 块(或 settings.json 的 Stop hook)。"
  exit 0
fi

mkdir -p "$DEST"
echo "==> 1/5 复制 watchdog.py → $DEST/"
cp "$SRC/watchdog.py" "$DEST/watchdog.py"
chmod +x "$DEST/watchdog.py"

echo "==> 2/5 复制 adapters/ → $DEST/"
cp -r "$SRC/adapters" "$DEST/"

echo "==> 3/5 复制测试脚本(可选) → $DEST/"
cp "$SRC/watchdog_test.py" "$DEST/watchdog_test.py"

if [ "$AGENT" = "claude" ]; then
  echo "==> 4/5 注册 Claude Code Stop hook → $DEST/settings.json =="
  SETTINGS="$DEST/settings.json"
  if grep -q 'watchdog' "$SETTINGS" 2>/dev/null; then
    echo "  (已存在,跳过)"
  else
    # 保留原 settings.json 内容,合并 hooks.Stop
    python3 - "$SETTINGS" <<'PY'
import json, os, sys
p = sys.argv[1]
data = {}
if os.path.exists(p):
    try:
        with open(p) as f:
            data = json.load(f)
    except ValueError:
        data = {}
hooks = data.setdefault("hooks", {})
entry = {
    "type": "command",
    "command": "CODEX_WATCHDOG_ADAPTER=claude python3 ~/.claude/watchdog.py",
    "timeout": 30,
}
hooks.setdefault("Stop", []).append({"hooks": [entry]})
with open(p, "w") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
    f.write("\n")
PY
    echo "  OK"
  fi
  echo "==> 5/5 创建启用标记"
  touch "$DEST/watchdog.enabled"
  echo "  OK"
else
  echo "==> 4/5 注册 Codex Stop hook → $DEST/config.toml =="
  CFG="$DEST/config.toml"
  if grep -q 'watchdog' "$CFG" 2>/dev/null; then
    echo "  (已存在,跳过)"
  else
    cat >> "$CFG" <<'EOF'

# Codex Watchdog: auto-continue unfinished tasks (see ~/.codex/watchdog.py)
[[hooks.Stop]]
[[hooks.Stop.hooks]]
type = "command"
command = "python3 ~/.codex/watchdog.py"
timeout = 30
EOF
    echo "  OK"
  fi
  echo "==> 5/5 创建启用标记"
  touch "$DEST/watchdog.enabled"
  echo "  OK"
fi

echo ""
echo "==== 安装完成($AGENT)===="
echo "下一步:在 Codex 里运行 /hooks 并信任 watchdog.py;"
echo "      在 Claude Code 里首次触发 Stop hook 时选 Trust & run。"
echo "验证:tail /tmp/codex-watchdog.log (看到 continue #1 即生效)"
echo ""
