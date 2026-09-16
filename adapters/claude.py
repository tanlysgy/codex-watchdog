"""Claude Code adapter.

Claude Code's Stop hook input shares the canonical fields with Codex:
  - session_id, transcript_path, cwd
  - last_assistant_message, stop_hook_active

The transcript is a JSON file (array of messages, possibly JSONL in some
builds). Each entry is either:
  - {"type":"user","message":{"role":"user","content":"..."}}
  - {"type":"assistant","message":{"role":"assistant","content":[...]}}
  - {"type":"tool_use","name":"Bash","input":{...},...} (assistant-issued)
  - {"type":"tool_result","tool_use_id":"...","content":"..."} (tool output)

We defensively scan whatever shape the file actually has: rows can be
objects with "type"/"message" fields, or raw {"role": ..., "content": ...}
objects, and tool use may appear as "tool_use" rows or as content blocks.

Privacy note: we only read the LAST N lines and never write anything there.
"""
import json
import os
from .base import BaseAdapter

NOISE_PREFIXES = ("<environment_context", "<turn_aborted", "# AGENTS.md instructions", "Stop hook feedback:")


def _content_tool_uses(content):
    """Count tool_use blocks nested in a Claude content list.

    Real Claude Code transcripts nest tool calls inside assistant
    message content blocks ({"type":"tool_use","name":...}) rather than
    emitting standalone tool_use rows; the Stop hook payload also
    sometimes carries them there. Count any nested tool_use blocks so
    tool activity isn't misread as a quiet turn.
    """
    if isinstance(content, str):
        return 0, set()
    if isinstance(content, dict):
        b = content.get("type") == "tool_use"
        if b:
            nm = content.get("name") or ""
            return 1, {nm} if nm else set()
        return 0, set()
    if isinstance(content, list):
        n = 0
        names = set()
        for c in content:
            if isinstance(c, dict):
                if c.get("type") == "tool_use":
                    n += 1
                    nm = c.get("name") or ""
                    if nm:
                        names.add(nm)
                else:
                    sub_n, sub_names = _content_tool_uses(c)
                    n += sub_n
                    names |= sub_names
        return n, names
    return 0, set()


def _content_to_text(content) -> str:
    """Extract plain text from a Claude content block, whatever its shape."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, str):
                parts.append(c)
            elif isinstance(c, dict):
                if c.get("type") == "text":
                    parts.append(c.get("text", ""))
                elif c.get("text"):
                    parts.append(c.get("text"))
        return "\n".join(parts)
    if isinstance(content, dict):
        return str(content.get("text") or content.get("content") or "")
    return str(content)


class ClaudeAdapter(BaseAdapter):
    name = "claude"

    @staticmethod
    def _rows(path, n=600):
        if not path or not os.path.exists(path):
            return []
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                raw = f.read()
        except OSError:
            return []
        if raw.lstrip().startswith("["):
            # JSON array transcript
            try:
                arr = json.loads(raw)
                return arr[-n:] if isinstance(arr, list) else []
            except ValueError:
                pass
        # JSONL fallback
        rows = []
        for line in raw.splitlines()[-n:]:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        return rows

    @staticmethod
    def _entry_role(o) -> str:
        """Return 'user' / 'assistant' / 'tool_use' / 'tool_result' / ''."""
        if not isinstance(o, dict):
            return ""
        t = o.get("type", "")
        if t in ("user", "assistant", "tool_use", "tool_result"):
            return t
        # unwrap {"message": {"role": ...}} or {"message": {"content": [...]}}
        m = o.get("message")
        if isinstance(m, dict):
            r = m.get("role", "")
            if r:
                return r
            # some builds nest rows as {"message": {"content": [tool_use...]}}
            if isinstance(m.get("content"), (list, dict)):
                return "assistant"
        return ""

    @staticmethod
    def _entry_text(o) -> str:
        if not isinstance(o, dict):
            return ""
        m = o.get("message")
        if isinstance(m, dict):
            return _content_to_text(m.get("content"))
        if "content" in o:
            return _content_to_text(o.get("content"))
        t = o.get("type")
        if t == "tool_use":
            return ""
        return ""

    def is_noise(self, text: str) -> bool:
        t = text.strip()
        if not t:
            return True
        return t.startswith(NOISE_PREFIXES)

    def last_user_prompt(self, ev: dict):
        last = None
        for o in self._rows(ev.get("transcript_path")):
            role = self._entry_role(o)
            text = self._entry_text(o)
            if role == "user" and text and not self.is_noise(text):
                last = text
        return last

    def last_turn_tool_activity(self, ev: dict):
        rows = self._rows(ev.get("transcript_path"), 400)
        if not rows:
            return 0, set()
        # Turn = everything after the last user/assistant boundary; Claude
        # transcripts don't have explicit turn markers, so we approximate by
        # taking the tail of the file (current turn) — usually fine.
        n = 0
        names = set()
        for o in rows[-80:]:
            t = o.get("type")
            if t == "tool_use":
                n += 1
                nm = o.get("name") or ""
                if nm:
                    names.add(nm)
            elif t == "tool_result":
                n += 1
            elif t == "assistant":
                # nested tool calls in content blocks
                m = o.get("message") or {}
                content = m.get("content") if isinstance(m, dict) else o.get("content")
                in_n, in_names = _content_tool_uses(content)
                n += in_n
                names |= in_names
        return n, names

    def is_watchdog_inject(self, text: str) -> bool:
        t = text.strip()
        return "[watchdog]" in t or t.startswith("<hook_prompt")

    def enabled(self, ev: dict, state: dict, marker: str) -> bool:
        if state.get("activated"):
            return True
        # Claude: ~/.claude/settings.json holds user hooks; we use the same
        # marker-file convention as Codex if present.
        if os.path.exists(marker):
            return True
        rows = self._rows(ev.get("transcript_path"), 600)
        for o in rows:
            text = self._entry_text(o).lower()
            if "watchdog" in text or "看门狗" in text:
                return True
        return False
