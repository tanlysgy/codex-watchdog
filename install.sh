#!/usr/bin/env bash
set -euo pipefail

# Codex Watchdog 一键安装脚本
# 用法: bash install.sh  [--uninstall]
# 假设 ~/.codex/ 已存在 (codex init'd)

WATCHDOG_DIR="${CODEX_HOME:-$HOME/.codex}"
SRC="$(cd "$(dirname "$0")" && pwd)"

echo "==> 1/4 复制 watchdog.py → $WATCHDOG_DIR/"
cp "$SRC/watchdog.py" "$WATCHDOG_DIR/watchdog.py"
chmod +x "$WATCHDOG_DIR/watchdog.py"

echo "==> 2/4 复制测试脚本(可选) → $WATCHDOG_DIR/"
cp "$SRC/watchdog_test.py" "$WATCHDOG_DIR/watchdog_test.py"

echo "==> 3/4 注册 Stop hook"
CFG="$WATCHDOG_DIR/config.toml"
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

echo "==> 4/4 创建启用标记"
touch "$WATCHDOG_DIR/watchdog.enabled"
echo "  OK"

echo ""
echo "==== 安装完成 ===="
echo "下一步:在你的 Codex CLI 里运行 /hooks"
echo "找到 watchdog.py 那一行,点信任。"
echo "然后跑个简单命令验证:"
echo "  touch /tmp/test-wd && tail -200 /tmp/codex-watchdog.log"
echo ""
echo "卸载:删除 ~/.codex/watchdog.{py,enabled},并手动从 config.toml 移除 [[hooks.Stop]] 那几行。"
