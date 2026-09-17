"""Codex adapter: parses Codex JSONL session transcripts.

- User messages / tool calls live in {"type":"response_item","payload":{...}}
  rows; `payload.type` is "message" (role user/assistant) or "function_call".
- Turn boundaries: {"type":"turn_context"} or event_msg payload.type=="task_started".
- Our own auto-continue arrives as a user message starting with "<hook_prompt"
  or containing "[watchdog]".
"""
import json
import os
import sys
from .base import BaseAdapter, cached_rows

NOISE_PREFIXES = ("<environment_context", "<turn_aborted", "# AGENTS.md instructions")

# Protocols live in one place so the hook and the adapters cannot disagree.
_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import watchdog_protocol as protocol  # noqa: E402


class CodexAdapter(BaseAdapter):
    name = "codex"

    @staticmethod
    def _parse(path, n=600):
        if not path or not os.path.exists(path):
            return []
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = list(f)[-n:]
        except OSError:
            return []
        rows = []
        for line in lines:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        return rows

    @classmethod
    def _rows(cls, path, n=600):
        return cached_rows(cls._parse, path, n)

    @staticmethod
    def _user_text(o) -> str:
        p = o.get("payload") or {}
        if o.get("type") != "response_item" or p.get("type") != "message":
            return ""
        if p.get("role") != "user":
            return ""
        return "".join(
            c.get("text", "") for c in (p.get("content") or []) if isinstance(c, dict)
        ).strip()

    def is_noise(self, text: str) -> bool:
        t = text.strip()
        if not t:
            return True
        return t.startswith(NOISE_PREFIXES)

    def last_user_prompt(self, ev: dict):
        last = None
        for o in self._rows(ev.get("transcript_path")):
            text = self._user_text(o)
            if text and not self.is_noise(text):
                last = text
        return last

    def last_turn_tool_activity(self, ev: dict):
        rows = self._rows(ev.get("transcript_path"), 400)
        if not rows:
            return 0, set()
        turn_start = 0
        for i, o in enumerate(rows):
            t = o.get("type")
            p = o.get("payload") or {}
            if t == "turn_context":
                turn_start = i
            elif t == "event_msg" and p.get("type") == "task_started":
                turn_start = i
        n = 0
        names = set()
        for o in rows[turn_start:]:
            p = o.get("payload") or {}
            if o.get("type") == "response_item" and p.get("type") == "function_call":
                n += 1
                nm = p.get("name") or ""
                if nm:
                    names.add(nm)
        return n, names

    def is_watchdog_inject(self, text: str) -> bool:
        t = text.strip()
        return t.startswith("<hook_prompt") or "[watchdog]" in t

    def enabled(self, ev: dict, state: dict, marker: str) -> bool:
        if state.get("activated"):
            return True
        rows = self._rows(ev.get("transcript_path"), 600)
        for o in rows:
            text = self._user_text(o).lower()
            if "watchdog" in text or "看门狗" in text or "没做完自己继续" in text:
                return True
        return False

    def checkpoint_metadata(self, ev: dict) -> dict:
        fc_count, fc_names = self.last_turn_tool_activity(ev)
        msg = (ev.get("last_assistant_message") or "").strip()
        return {
            "session_id": ev.get("session_id"),
            "final_message": msg,
            "tool_calls": fc_count,
            "tool_names": sorted(fc_names),
            "completion_signal": protocol.is_completion_declaration(msg),
            "need_user_signal": protocol.is_need_user_declaration(msg),
        }