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


def run_with_prompt(user_prompt, last_msg, sid="lang-reason"):
    tp = tempfile.mktemp(suffix=".jsonl")
    with open(tp, "w") as f:
        f.write(json.dumps(user_msg(user_prompt)) + "\n")
        f.write(json.dumps(turn_start()) + "\n")
    ev = {
        "session_id": sid,
        "hook_event_name": "Stop",
        "transcript_path": tp,
        "cwd": "/tmp",
        "last_assistant_message": last_msg,
    }
    sp = os.path.join(wd.STATE_DIR, f"{sid}.json")
    wd.save_state(sid, {"count": 0, "last_continue_at": None, "last_msg": None,
                        "quiet_turns": 0, "activated": True})
    old_in, old_out = sys.stdin, sys.stdout
    sys.stdin = io.StringIO(json.dumps(ev))
    out = io.StringIO()
    sys.stdout = out
    try:
        wd.main()
        res = json.loads(out.getvalue().strip() or "{}")
    finally:
        sys.stdin, sys.stdout = old_in, old_out
    try:
        os.remove(sp)
    except OSError:
        pass
    return res


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

    # ---- agent offered optional follow-up / asked for decision -> stop ----
    r = run_main([turn_start(), tool_call()], "要不要我把对比写进 README?", sid="s15",
                 seed={"count": 0, "last_continue_at": None, "last_msg": None,
                       "quiet_turns": 0, "activated": True})
    check("agent offers optional next step -> continue(stop)", r.get("continue"), True)

    # ---- English protocol declarations are honored at protocol level ----
    r = run_main([turn_start(), tool_call()], "Task Complete.", sid="s16",
                 seed={"count": 0, "last_continue_at": None, "last_msg": None,
                       "quiet_turns": 0, "activated": True})
    check("english protocol Task Complete (with tools) -> continue(stop)", r.get("continue"), True)

    r = run_main([turn_start(), tool_call()], "Need User: please provide the API key.", sid="s17",
                 seed={"count": 0, "last_continue_at": None, "last_msg": None,
                       "quiet_turns": 0, "activated": True})
    check("english protocol Need User -> continue(stop)", r.get("continue"), True)

    # ---- bilingual reason follows session language (Chinese vs English) ----
    r_zh = run_with_prompt("继续做 P7.5,中文会话", "还没做完,继续", sid="s18")
    r_en = run_with_prompt("continue with the task", "not done yet, keep going", sid="s19")
    check("reason follows zh session", "任务尚未完成" in (r_zh.get("reason") or ""), True)
    check("reason follows en session", "The task is not finished" in (r_en.get("reason") or ""), True)

    # ================= Runtime v1.1: TaskState / Event Log / Checkpoint / Metrics =================
    # Use an isolated state dir so we don't pollute /tmp/codex-watchdog.
    import tempfile as _tf
    _tmp = _tf.mkdtemp(prefix="wd-test-")
    _old_state_dir = wd.STATE_DIR
    _old_events = wd.EVENTS_FILE
    _old_metrics = wd.METRICS_FILE
    _old_cp = wd.CHECKPOINT_DIR
    wd.STATE_DIR = _tmp
    wd.EVENTS_FILE = os.path.join(_tmp, "events.jsonl")
    wd.METRICS_FILE = os.path.join(_tmp, "metrics.json")
    wd.CHECKPOINT_DIR = os.path.join(_tmp, "checkpoints")

    def run_v11(last_msg, sid, seed=None, transcript=None):
        return run_main(transcript or [turn_start()], last_msg, sid=sid, seed=seed,
                        keep_state=True)

    # ---- task_started emitted on first sight of a session ----
    run_v11("继续干活", "v11-start")
    evs = wd.read_events()
    check("task_started event emitted", any(e.get("event") == "task_started" for e in evs), True)

    # ---- continue -> RUNNING state persisted ----
    run_v11("继续干活", "v11-run")
    st = wd.load_state("v11-run")
    check("continue sets status RUNNING", st.get("status"), wd.RUNNING)
    check("continue sets updated_at", st.get("updated_at") is not None, True)

    # ---- declared done -> COMPLETED ----
    run_v11("任务完成:全部搞定", "v11-done")
    st = wd.load_state("v11-done")
    check("declared done -> COMPLETED", st.get("status"), wd.COMPLETED)
    evs = wd.read_events()
    check("completed event emitted", any(e.get("event") == "completed" for e in evs), True)

    # ---- declared need-user -> BLOCKED ----
    run_v11("需要用户:请提供 API key", "v11-blocked")
    st = wd.load_state("v11-blocked")
    check("declared need-user -> BLOCKED", st.get("status"), wd.BLOCKED)
    evs = wd.read_events()
    check("blocked event emitted", any(e.get("event") == "blocked" for e in evs), True)

    # ---- repeated message -> STALLED ----
    seed_rep = {"count": 1, "last_continue_at": None, "last_msg": "同一个消息",
                "quiet_turns": 0, "activated": True}
    run_v11("同一个消息", "v11-stall", seed=seed_rep)
    st = wd.load_state("v11-stall")
    check("repeated message -> STALLED", st.get("status"), wd.STALLED)
    evs = wd.read_events()
    check("stalled event emitted", any(e.get("event") == "stalled" for e in evs), True)

    # ---- checkpoint written on state change (stop paths) ----
    cp_path = os.path.join(wd.CHECKPOINT_DIR, "v11-done.json")
    check("checkpoint file created on state change", os.path.exists(cp_path), True)
    with open(cp_path) as f:
        cp = json.load(f)
    check("checkpoint has status", cp.get("status"), wd.COMPLETED)
    check("checkpoint has last_message", cp.get("last_message"), "任务完成:全部搞定")

    # ---- checkpoint written every CHECKPOINT_EVERY continues ----
    seed_cp = {"count": wd.CHECKPOINT_EVERY - 1, "last_continue_at": None,
               "last_msg": None, "quiet_turns": 0, "activated": True}
    run_v11("继续干活", "v11-cp", seed=seed_cp)
    cp_path = os.path.join(wd.CHECKPOINT_DIR, "v11-cp.json")
    check("checkpoint written at continue multiple", os.path.exists(cp_path), True)
    with open(cp_path) as f:
        cp = json.load(f)
    check("checkpoint continue_count", cp.get("continue_count"), wd.CHECKPOINT_EVERY)

    # ---- metrics derived from event log ----
    wd.update_metrics()
    with open(wd.METRICS_FILE) as f:
        m = json.load(f)
    check("metrics total_continues > 0", m.get("total_continues", 0) > 0, True)
    check("metrics has stop_reason_distribution", "stop_reason_distribution" in m, True)
    check("metrics has stalled_recovered", "stalled_recovered" in m, True)
    check("metrics has completed_then_continued", "completed_then_continued" in m, True)
    check("metrics has blocked_then_continued", "blocked_then_continued" in m, True)

    # ---- restore state dir ----
    wd.STATE_DIR = _old_state_dir
    wd.EVENTS_FILE = _old_events
    wd.METRICS_FILE = _old_metrics
    wd.CHECKPOINT_DIR = _old_cp
    import shutil as _sh
    _sh.rmtree(_tmp, ignore_errors=True)

    print(f"\n{PASS} passed, {FAIL} failed")
    if not os.path.exists(ENABLED):
        os.makedirs(os.path.dirname(ENABLED), exist_ok=True)
        open(ENABLED, "a").close()
        print("(restored watchdog.enabled)")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
