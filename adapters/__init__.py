"""Adapter registry for agent-specific transcript parsing.

Each adapter translates an agent's hook input / transcript JSON into the
canonical view the watchdog engine needs:

  - last_user_prompt(ev) -> str | None        (most recent real user prompt)
  - last_turn_tool_activity(ev) -> (int, set[str])  (tool calls in current turn)
  - is_watchdog_inject(text) -> bool          (our own auto-continue?)
  - enabled(ev, state, marker) -> bool        (is watchdog active here?)
"""
from .codex import CodexAdapter
from .claude import ClaudeAdapter

_ADAPTERS = {
    "codex": CodexAdapter,
    "claude": ClaudeAdapter,
}


def load_adapter(name: str):
    """Return an adapter instance for `name` (defaults to Codex)."""
    cls = _ADAPTERS.get(name or "codex")
    if cls is None:
        raise ValueError(f"unknown adapter {name!r}; known: {sorted(_ADAPTERS)}")
    return cls()
