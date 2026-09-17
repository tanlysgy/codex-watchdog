"""Abstract adapter contract. Subclass and implement the four callbacks.

A Stop hook is a short-lived process that asks several questions about the same
transcript (last user prompt, tool activity, checkpoint metadata). Re-reading and
re-parsing the file for each question is pure waste, so adapters share one
per-process row cache via `cached_rows`.
"""

# path -> parsed rows, scoped to this short-lived process.
_ROW_CACHE = {}


def cached_rows(fetch, path, n=600):
    """Parse a transcript at most once per path within this process.

    Different callers ask for different windows (600 lines for the user prompt,
    400 for tool activity). A smaller request is served by slicing an already
    parsed larger window instead of re-reading the file.
    """
    entry = _ROW_CACHE.get(path)
    if entry is not None and entry[0] >= n:
        return entry[1][-n:] if entry[0] != n else entry[1]
    rows = fetch(path, n)
    _ROW_CACHE[path] = (n, rows)
    return rows


class BaseAdapter:
    """Minimal interface every agent adapter must satisfy.

    All methods get the raw hook event dict (parsed from stdin JSON) and/or
    the per-session watchdog state dict.
    """

    name = "base"

    def last_user_prompt(self, ev: dict):
        """Return the text of the most recent real user message, or None."""
        raise NotImplementedError

    def first_user_prompt(self, ev: dict):
        """Return the text of the session's first real user message, or None.

        Used as the task "goal" when a checkpoint is written for resume.
        """
        return None

    def last_assistant_text(self, ev: dict):
        """Return the transcript's last assistant message, or None.

        Context-lifecycle events (PreCompact) may arrive without the
        `last_assistant_message` field the Stop hook carries, so the transcript
        is the fallback source for "where were we".
        """
        return None

    def last_turn_tool_activity(self, ev: dict):
        """Return (count, set_of_tool_names) for the current turn."""
        raise NotImplementedError

    def is_watchdog_inject(self, text: str) -> bool:
        """True if `text` is our own auto-continue injection."""
        raise NotImplementedError

    def enabled(self, ev: dict, state: dict, marker: str) -> bool:
        """Decide whether the watchdog is active for this session."""
        raise NotImplementedError

    def checkpoint_metadata(self, ev: dict) -> dict:
        """Return a normalized snapshot of the current turn for checkpoints.

        Standardized keys (all optional, missing -> None/empty):
          - session_id
          - final_message
          - tool_calls (int)
          - tool_names (list)
          - completion_signal (bool)  -- agent declared the task done
          - need_user_signal (bool)   -- agent declared it needs the user

        The engine calls this on every Stop hook, so implementations should stay
        cheap (the shared row cache makes the transcript read essentially free).
        """
        return {
            "session_id": ev.get("session_id"),
            "final_message": (ev.get("last_assistant_message") or "").strip(),
            "tool_calls": 0,
            "tool_names": [],
            "completion_signal": False,
            "need_user_signal": False,
        }