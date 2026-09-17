"""Shared protocol detection: completion / need-user declarations.

Single source of truth used by both the engine (watchdog.py) and the adapters,
so the declaration vocabulary cannot drift between them.

Named `watchdog_protocol` (not `protocol`) so it cannot be shadowed by — or
shadow — an unrelated module of that very common name elsewhere on sys.path.

Design note on false positives
------------------------------
Declarations are *protocol* markers, so they only count when they form a line
of their own (optionally with a short explanation after punctuation):

  - `任务完成`                        -> declaration
  - `任务完成:所有改动已提交并推送。`   -> declaration
  - `已完成 3/7 个文件,还剩 4 个,继续` -> progress narration, NOT a declaration
  - `任务完成度约 60%,继续推进`        -> progress narration, NOT a declaration

Requiring the marker to start the line and be either followed by line end or by
punctuation keeps ordinary progress summaries from being read as "done" — the
failure mode that made the watchdog stop mid-task.
"""
import re

# --- Declared protocol markers (the model is asked to use these) ---
DONE_MARKER = (
    r"任务完成|任务全部完成|全部完成|已完成|done|"
    r"task\s+(?:is\s+)?(?:complete|done|completed)|task\s+completed|"
    r"all\s+(?:tasks|work)\s+(?:complete|done|finished)|fully\s+complete"
)
NEED_USER_MARKER = (
    r"需要用户|需要你|need\s+user|needs\s+user|need\s+your\s+input|"
    r"need\s+your\s+decision|waiting\s+for\s+(?:your|you)|"
    r"blocked\s+on\s+(?:you|your|user)|awaiting\s+(?:your|user)|等待用户"
)

_OPEN_Q = r"[『「【\"'“”]?"
_CLOSE_Q = r"[」』】\"'”’]?"
_PUNCT = r"[:：。.!！,，、;；\-—]"

_LINE_DONE = re.compile(
    r"^[^\S\n]*" + _OPEN_Q + r"[^\S\n]*(?P<marker>" + DONE_MARKER + r")"
    + _CLOSE_Q + r"[^\S\n]*(?:" + _PUNCT + r".*|\s*)$",
    re.I,
)
_LINE_NEED_USER = re.compile(
    r"^[^\S\n]*" + _OPEN_Q + r"[^\S\n]*(?:" + NEED_USER_MARKER + r")"
    + _CLOSE_Q + r"[^\S\n]*(?:" + _PUNCT + r".*|\s*)$",
    re.I,
)

# Text after the marker that contradicts it: 任务完成,继续推进 / 已完成,部分待补充
CONTRAST = re.compile(
    r"(?:度|率|百分比|部分|初步|基本|大致|开始|继续|接下来|下一步|接着|然后|还|剩|"
    r"remains?|remaining|partial|partially|initial|next|then|continue|\d+\s*%)",
    re.I,
)


def is_completion_declaration(text: str) -> bool:
    """True when `text` contains an explicit, unqualified 'task complete' line."""
    for line in (text or "").splitlines():
        m = _LINE_DONE.match(line)
        if not m:
            continue
        if CONTRAST.search(line[m.end("marker"):]):
            continue
        return True
    return False


def is_need_user_declaration(text: str) -> bool:
    """True when `text` contains an explicit 'I need the user' line."""
    for line in (text or "").splitlines():
        if _LINE_NEED_USER.match(line):
            return True
    return False


# --- Classic DONE phrases (fallback when the agent did NOT use the protocol) ---
DONE = re.compile(
    r"(任务(已?全部)?完成|全部完成|已经完成|已完成|完成收尾|全部做好|"
    r"没有更多(工作|任务|事项)|无(需|须)(再|任何)(工作|修改|任务|事项)|"
    r"all done|task (is )?(complete|done)|fully (complete|done)|"
    r"no (more|further|remaining) (work|tasks|steps)|nothing (left|more|else)|"
    r"finished|搞定了|行了|收工|就此结束|到此为止|已完成所有|没有其他工作)",
    re.I,
)

# --- Genuine short requests for user input ---
NEED_USER = re.compile(
    r"(请(您|你)?(提供|输入|确认|决定|告诉我|给出|选择|设置|说明|回复|告知)|"
    r"需要(您|你|用户)?(提供|输入|确认|批准|授权|告知|选择|决定|告诉我)|"
    r"(等待|等)(您|你|用户)(的)?(输入|确认|决定|指示|消息|答复|要求|选择)|"
    r"please (provide|enter|confirm|decide|tell|give|choose|explain)|"
    r"waiting for (your|you|user)|need (your|you|user) (input|confirmation|decision|approval)|"
    r"asked for (your|user) (input|confirmation|decision)|"
    r"needs? (your|user) (input|confirmation|decision|approval))",
    re.I,
)

RHETORICAL = re.compile(r"[吗么呢]+\s*[?？!!。]?\s*(不(需要|用|必)|无需|不必|我自己|我来|我可以|我会|算了)")

# The agent is handing control back to the user (offering options, asking a
# question, asking for a decision). Auto-continuing past a genuine question is
# annoying and can even modify the repo without consent, so we stop and wait.
PROMPT_FOR_INPUT = re.compile(
    r"(要不要|需不需要|是否(需要|要)|你(想|觉得|希望|要不要)(怎么|如何|用|选|做)?|"
    r"你(来)?(选|决定|拍板|拿主意|确认一下?|说了算)|"
    r"等(你|您|您来|你来)(选|决定|确认|拍板|拿主意|输入|回复|答复|告诉|选择|指示|消息)|"
    r"请(你|您)?(选|决定|确认|拍板|输入|回复|答复|告诉|选择|指示|告知|提供)|"
    r"(\?|？|吗|呢)$|"
    r"which (option|one|approach)|what do you (want|prefer|think)|"
    r"(do|would) you (want|like|prefer)|your (call|choice|decision)|"
    r"(waiting|wait) for (your|you) (input|decision|choice|answer|confirmation)|"
    r"let me know (if|what|how|whether)|tell me (if|what|how|whether))",
    re.I,
)