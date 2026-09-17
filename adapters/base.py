"""Abstract adapter contract. Subclass and implement the four callbacks."""


class BaseAdapter:
    """Minimal interface every agent adapter must satisfy.

    All methods get the raw hook event dict (parsed from stdin JSON) and/or
    the per-session watchdog state dict.
    """

    name = "base"

    def last_user_prompt(self, ev: dict):
        """Return the text of the most recent real user message, or None."""
        raise NotImplementedError

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
        """
        return {
            "session_id": ev.get("session_id"),
            "final_message": (ev.get("last_assistant_message") or "").strip(),
            "tool_calls": 0,
            "tool_names": [],
            "completion_signal": False,
            "need_user_signal": False,
        }
