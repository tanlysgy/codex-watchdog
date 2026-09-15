#!/usr/bin/env python3
"""Watchdog regression tests (evidence-driven version).

Run: python3 watchdog_test.py   (from the repo, or after install to ~/.codex)
Uses only /tmp state; restores ~/.codex/watchdog.enabled afterwards.
"""
import importlib.util
import io
import json
import os
import sys
import tempfile

# Prefer the watchdog.py next to this test file (repo checkout),
# fall back to the installed copy in ~/.codex.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_WD = os.path.join(_HERE, "watchdog.py")
_INSTALLED_WD = os.path.expanduser("~/.codex/watchdog.py")
WD = _REPO_WD if os.path.exists(_REPO_WD) else _INSTALLED_WD
ENABLED = os.path.expanduser("~/.codex/watchdog.enabled")

spec = importlib.util.spec_from_file_location("wd", WD)
wd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wd)

PASS = 0
FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    ok = got == want
    PASS += ok
    FAIL += not ok
    print(f"{'OK ' if ok else 'FAIL'} {name}: got={got!r} want={want!r}")


def user_msg(text):
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        },
    }


def tool_call(name="exec_command"):
    return {
        "type": "response_item",
        "payload": {"type": "function_call", "id": "fc_x", "name": name,
                    "arguments": "{}"},
    }


def turn_start():
    return {"type": "turn_context", "payload": {"turn_id": "t1"}}


def run_main(transcript, last_msg, seed=None, sid="sess-ev", keep_state=False):
    tp = tempfile.mktemp(suffix=".jsonl")
    with open(tp, "w") as f:
        for line in transcript:
            f.write(json.dumps(line) + "\n")
    ev = {
        "session_id": sid,
        "hook_event_name": "Stop",
        "transcript_path": tp,
        "cwd": "/tmp",
        "last_assistant_message": last_msg,
        "permission_mode": "default",
        "stop_hook_active": True,
    }
    sp = os.path.join(wd.STATE_DIR, f"{sid}.json")
    if seed is None:
        seed = {"count": 0, "last_continue_at": None, "last_msg": None,
                "quiet_turns": 0, "activated": True}
    wd.save_state(sid, seed)
    old_in, old_out = sys.stdin, sys.stdout
    sys.stdin = io.StringIO(json.dumps(ev))
    out = io.StringIO()
    sys.stdout = out
    try:
        wd.main()
    finally:
        sys.stdin, sys.stdout = old_in, old_out
    res = out.getvalue().strip()
    if not keep_state:
        try:
            os.remove(sp)
        except OSError:
            pass
    return json.loads(res) if res else {}


def main():
    inj = user_msg("[watchdog] 任务尚未完成,请继续执行")
    env = user_msg("<environment_context>\n  <current_date>2026-09-13</current_date>")
    aborted = user_msg("<turn_aborted>\nThe previous turn was interrupted")

    # ---- evidence-driven: tool activity means keep going even if text sounds done ----
    # transcript: turn_start + tool_call (current turn has activity)
    r = run_main([inj, turn_start(), tool_call()], "任务看起来差不多了,我再核对一遍输出。", sid="s1")
    check("tool evidence overrides done-ish text -> block", r.get("decision"), "block")

    # ---- quiet turn + done phrase -> stop ----
    r = run_main([inj, turn_start()], "全部完成,没有更多工作了。", sid="s2")
    check("quiet turn + done phrase -> continue(stop)", r.get("continue"), True)

    # ---- declared protocol: 任务完成 -> stop even mid-work ----
    r = run_main([turn_start()], "任务完成:所有改动已提交并推送。", sid="s3")
    check("declared 任务完成 -> continue(stop)", r.get("continue"), True)

    # ---- declared protocol: 需要用户 -> stop ----
    r = run_main([turn_start()], "需要用户:请提供部署环境的 SSH 凭据。", sid="s4")
    check("declared 需要用户 -> continue(stop)", r.get("continue"), True)

    # ---- 3 quiet turns in a row -> stop ----
    seed5 = {"count": 0, "last_continue_at": None, "last_msg": None,
             "quiet_turns": 2, "activated": True}
    r = run_main([turn_start()], "我再想一想接下来怎么做", sid="s5", seed=seed5)
    check("3 quiet turns -> continue(stop)", r.get("continue"), True)

    # ---- quiet turn counter increments (tool turn resets it) ----
    seed6 = {"count": 0, "last_continue_at": None, "last_msg": None,
             "quiet_turns": 0, "activated": True}
    r = run_main([turn_start()], "继续干,下一步是写 release-check", sid="s6", seed=seed6, keep_state=True)
    st6 = wd.load_state("s6")
    check("quiet turn increments counter", st6.get("quiet_turns"), 1)
    _ = wd.load_state("s6")  # noqa

    seed7 = {"count": 0, "last_continue_at": None, "last_msg": None,
             "quiet_turns": 2, "activated": True}
    r = run_main([turn_start(), tool_call("apply_patch")], "补丁打好了,继续", sid="s7", seed=seed7, keep_state=True)
    st7 = wd.load_state("s7")
    check("tool turn resets quiet counter", st7.get("quiet_turns"), 0)
    for sid_ in ("s6", "s7"):
        try:
            os.remove(os.path.join(wd.STATE_DIR, f"{sid_}.json"))
        except OSError:
            pass

    # ---- classic guards still work ----
    r = run_main([turn_start()], "请你提供 API key 后才能继续。", sid="s8")
    check("short need-user -> continue(stop)", r.get("continue"), True)

    r = run_main([turn_start()], "遇到构建错误,我已修复,继续验证。", sid="s9")
    check("narrated error -> block", r.get("decision"), "block")

    r = run_main([turn_start()], "继续", sid="s10",
                 seed={"count": wd.BURST_MAX, "last_continue_at": None,
                       "last_msg": "旧", "quiet_turns": 0, "activated": True})
    check("burst max -> continue(stop)", r.get("continue"), True)

    # ---- noise user messages don't reset burst ----
    r = run_main([env, aborted, inj, turn_start()], "继续干活,还没完", sid="s11")
    check("noise+inject only -> block", r.get("decision"), "block")

    # ---- global enabled marker activates sessions even without wake words ----
    had_marker = os.path.exists(ENABLED)
    if not had_marker:
        os.makedirs(os.path.dirname(ENABLED), exist_ok=True)
        open(ENABLED, "a").close()
    r = run_main([turn_start(), tool_call("apply_patch")], "继续 P8 收尾", sid="s12",
                 seed={"count": 0, "last_continue_at": None, "last_msg": None,
                       "quiet_turns": 0, "activated": False})
    check("global marker activates watchdog (no wake word) -> block", r.get("decision"), "block")

    # ---- without marker and without wake word -> watchdog stays silent ----
    backup = None
    if os.path.exists(ENABLED):
        backup = ENABLED + ".bak"
        os.replace(ENABLED, backup)
    r = run_main([turn_start(), tool_call()], "继续干活,还没完", sid="s13",
                 seed={"count": 0, "last_continue_at": None, "last_msg": None,
                       "quiet_turns": 0, "activated": False})
    check("no marker + no wake word -> silent skip", r, {})
    if backup is not None:
        os.replace(backup, ENABLED)

    # ---- connection lost mid-response -> auto-continue ----
    r = run_main([turn_start()], "API Error: Connection lost mid-response. The response above may be incomplete.", sid="s14",
                 seed={"count": 0, "last_continue_at": None, "last_msg": None,
                       "quiet_turns": 0, "activated": True})
    check("connection lost -> block (auto-continue)", r.get("decision"), "block")

    print(f"\n{PASS} passed, {FAIL} failed")
    if not os.path.exists(ENABLED):
        os.makedirs(os.path.dirname(ENABLED), exist_ok=True)
        open(ENABLED, "a").close()
        print("(restored watchdog.enabled)")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
