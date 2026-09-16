#!/usr/bin/env python3
"""Adapter regression tests: codex + claude transcript parsing."""
import importlib.util
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from adapters import load_adapter  # noqa: E402

PASS = 0
FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    ok = got == want
    PASS += ok
    FAIL += not ok
    print(f"{'OK ' if ok else 'FAIL'} {name}: got={got!r} want={want!r}")


def write_tmp(rows):
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return path


# ---- Codex adapter ----
def codex_user(text):
    return {"type": "response_item", "payload": {"type": "message", "role": "user",
            "content": [{"type": "input_text", "text": text}]}}


def codex_fc(name="exec_command"):
    return {"type": "response_item", "payload": {"type": "function_call", "name": name}}


codex = load_adapter("codex")

# 1. last real user prompt (skips noise)
p1 = write_tmp([
    {"type": "response_item", "payload": {"type": "message", "role": "user",
     "content": [{"type": "input_text", "text": "<environment_context>\n ..."}]}},
    codex_user("继续做 P7.5"),
])
check("codex: last_user_prompt skips noise", codex.last_user_prompt({"transcript_path": p1}), "继续做 P7.5")

# 2. watchdog inject detection
check("codex: watchdog inject", codex.is_watchdog_inject("[watchdog] 任务尚未完成"), True)
check("codex: real prompt not inject", codex.is_watchdog_inject("继续做 P7.5"), False)

# 3. tool activity in current turn
p2 = write_tmp([
    {"type": "turn_context", "payload": {"turn_id": "t1"}},
    codex_fc("exec_command"),
    codex_fc("apply_patch"),
])
n, names = codex.last_turn_tool_activity({"transcript_path": p2})
check("codex: tool count", n, 2)
check("codex: tool names", names, {"exec_command", "apply_patch"})

# 4. no tool activity
p3 = write_tmp([{"type": "turn_context", "payload": {}}])
check("codex: no tools", codex.last_turn_tool_activity({"transcript_path": p3}), (0, set()))

# ---- Claude adapter ----
def claude_user(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def claude_tool_use(name="Bash"):
    return {"type": "tool_use", "name": name, "input": {"command": "ls"}}

claude = load_adapter("claude")

# 5. claude: user prompt extraction
p4 = write_tmp([claude_user("继续重构"), {"type": "user", "message": {"role": "user", "content": "<environment_context>"}}])
check("claude: last_user_prompt", claude.last_user_prompt({"transcript_path": p4}), "继续重构")

# 6. claude: tool activity
p5 = write_tmp([claude_tool_use("Bash"), claude_tool_use("Edit")])
n, names = claude.last_turn_tool_activity({"transcript_path": p5})
check("claude: tool count", n, 2)
check("claude: tool names", names, {"Bash", "Edit"})

# 7. claude: inject detection
check("claude: inject", claude.is_watchdog_inject("[watchdog] 继续"), True)
check("claude: not inject", claude.is_watchdog_inject("正常消息"), False)

# 7b. claude: nested tool_use inside assistant content (real transcript shape)
p_aux = write_tmp([
    claude_user("继续做"),
    {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "查一下"},
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}},
        {"type": "tool_use", "id": "t2", "name": "Read", "input": {"file_path": "a.py"}},
    ]}},
])
n, names = claude.last_turn_tool_activity({"transcript_path": p_aux})
check("claude: nested tool_use count", n, 2)
check("claude: nested tool_use names", names, {"Bash", "Read"})

# 7c. claude: Stop hook feedback is noise (our own injection)
p_fb = write_tmp([
    {"type": "user", "message": {"role": "user", "content": "Reply with exactly: watchdog-e2e-ok"}},
    {"type": "user", "message": {"role": "user", "content": "Stop hook feedback:\n[watchdog] The task is not finished; keep working..."}},
])
check("claude: stop hook feedback is noise", claude.last_user_prompt({"transcript_path": p_fb}), "Reply with exactly: watchdog-e2e-ok")

# 8. claude: JSON array transcript
p6 = tempfile.mktemp(suffix=".json")
with open(p6, "w") as f:
    json.dump([claude_user("数组格式"), claude_tool_use("Bash")], f)
check("claude: array transcript user", claude.last_user_prompt({"transcript_path": p6}), "数组格式")

for p in (p1, p2, p3, p4, p5, p6, p_aux, p_fb):
    try:
        os.remove(p)
    except OSError:
        pass

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
