"""Deciding whether a message is a deposit ("a meme for the meme bank") or a withdrawal ("hey meme bank, ...")."""
from __future__ import annotations

import re
from dataclasses import dataclass

BANK_RE = re.compile(r"\bmeme[\s-]?bank\b", re.I)

_GREETING = r"(?:hey|hi|hello|yo|oi|ok|okay|g'?day|dear)"
# "hey meme bank, find me the France meme" / "yo memebank what have you got on cats"
ASK_WITH_GREETING = re.compile(
    rf"^\s*{_GREETING}[\s,]+(?:the\s+)?meme[\s-]?bank\b[\s,:;!.?-]*(?P<rest>.*)$", re.I | re.S
)
# "Meme bank, France" / "meme bank: random" (needs the punctuation so "meme bank is great" isn't a question)
ASK_WITH_PUNCT = re.compile(r"^\s*(?:the\s+)?meme[\s-]?bank\s*[,:!?]\s*(?P<rest>.*)$", re.I | re.S)
MENTION_RE = re.compile(r"<@([A-Z0-9]+)(?:\|[^>]*)?>")


@dataclass
class Trigger:
    kind: str  # "bank" or "ask"
    rest: str = ""


def strip_mentions(text: str) -> str:
    return re.sub(r"\s+", " ", MENTION_RE.sub(" ", text or "")).strip()


def classify(text: str, has_images: bool, bot_user_id: str | None = None, is_dm: bool = False) -> Trigger | None:
    text = text or ""
    mentioned = bool(bot_user_id) and f"<@{bot_user_id}" in text

    if has_images and (BANK_RE.search(text) or mentioned):
        return Trigger("bank")
    if has_images:
        return Trigger("bank") if is_dm else None

    if mentioned:
        rest = strip_mentions(text.replace(f"<@{bot_user_id}>", " "))
        m = ASK_WITH_GREETING.match(rest) or ASK_WITH_PUNCT.match(rest)
        return Trigger("ask", (m.group("rest") if m else rest).strip())

    m = ASK_WITH_GREETING.match(text) or ASK_WITH_PUNCT.match(text)
    if m:
        return Trigger("ask", strip_mentions(m.group("rest")))
    if is_dm:
        return Trigger("ask", strip_mentions(text))
    return None


@dataclass
class Command:
    name: str  # help | random | stats | get | forget | restore | retag | tag | untag | search
    number: int | None = None
    query: str = ""


_QUOTED = re.compile(r'"([^"]+)"|“([^”]+)”')


def split_tags(text: str) -> list[str]:
    """'"Trump screamer" and "Beanie Girl"' -> ['Trump screamer', 'Beanie Girl']. Quoted phrases stay whole
    (so "rock and roll" can be one tag); the rest splits on commas, semicolons, & and the word "and"."""
    pieces = _QUOTED.split(text or "")  # [outside, quote, quote, outside, quote, quote, outside, ...]
    out: list[str] = []
    for i in range(0, len(pieces), 3):
        out += [w.strip(" '\"“”") for w in re.split(r"[,;&]|\band\b", pieces[i], flags=re.I)]
        if i + 2 < len(pieces):
            out.append(pieces[i + 1] or pieces[i + 2])
    return [t.strip() for t in out if t and t.strip()]


_FILLER = re.compile(
    r"^(?:(?:can|could|would|will)\s+you\s+)?(?:please\s+)?"
    r"(?:(?:find|show|get|give|fetch|dig\s+up|pull\s+up|bring\s+up|post|grab)\s+(?:me\s+|us\s+)?)?",
    re.I,
)


def parse_command(rest: str) -> Command:
    raw = (rest or "").strip().rstrip("?!. ")
    low = raw.lower()
    if low in ("", "help", "commands", "what can you do", "how do you work", "how does this work"):
        return Command("help")
    if re.fullmatch(r"(?:give\s+me\s+|show\s+me\s+)?(?:a\s+)?(?:random(?:\s+(?:one|meme))?|surprise\s+me|dealer'?s\s+choice|anything)", low):
        return Command("random")
    if re.fullmatch(r"(?:stats|statistics|status|balance|how\s+many(?:\s+memes)?.*|what'?s\s+in\s+the\s+(?:bank|vault))", low):
        return Command("stats")
    m = re.fullmatch(r"(?:forget|delete|remove|withdraw)\s+(?:meme\s+)?#?(\d+)", low)
    if m:
        return Command("forget", int(m.group(1)))
    m = re.fullmatch(r"(?:restore|unforget|undelete|un-?withdraw|undo\s+forget|bring\s+back)\s+(?:meme\s+)?#?(\d+)", low)
    if m:
        return Command("restore", int(m.group(1)))
    m = re.fullmatch(r"(?:retag|re-tag|redo)\s+(?:meme\s+)?#?(\d+)", low)
    if m:
        return Command("retag", int(m.group(1)))
    # "tag #12 ilhan omar, squad": the words keep their own text (comma separated) in query. (?!\d) stops
    # "tag #12" (no words) being read as "tag #1 with 2".
    m = re.fullmatch(r"(?:add\s+)?tags?\s+(?:to\s+)?(?:meme\s+)?#?(\d+)(?!\d)(?:\s+with\b)?(?:\s*[:,-]?\s*(.+))?",
                     raw, re.I | re.S)
    if m:
        return Command("tag", int(m.group(1)), query=(m.group(2) or "").strip())
    m = re.fullmatch(r"(?:untag|remove\s+tags?)\s+(?:meme\s+)?#?(\d+)(?!\d)(?:\s*[:,-]?\s*(.+))?", raw, re.I | re.S)
    if m:
        return Command("untag", int(m.group(1)), query=(m.group(2) or "").strip())
    # "Tag it "Trump screamer" and "Beanie Girl"": no number, so number=None. In a DM the bot fills in the meme
    # from its last "Banked #N" message there; anywhere else it asks which meme.
    m = re.fullmatch(r"(?:please\s+)?(?:tag\s+(?:it|this(?:\s+one)?|that(?:\s+one)?)|add\s+tags?)"
                     r"(?:\s+(?:with|as)\b)?(?:\s*[:,-]?\s*(.+))?", raw, re.I | re.S)
    if m:
        return Command("tag", None, query=(m.group(1) or "").strip())
    m = re.fullmatch(r"(?:(?:show|get|post)\s+(?:me\s+)?)?(?:meme\s+)?(?:#|number\s+|no\.?\s*)(\d+)", low)
    if m:
        return Command("get", int(m.group(1)))
    query = _FILLER.sub("", raw).strip() or raw
    return Command("search", query=query)
