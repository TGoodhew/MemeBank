"""Deciding whether a message is a deposit ("a meme for the meme bank") or a withdrawal ("hey meme bank, ...")."""
from __future__ import annotations

import re
from dataclasses import dataclass

BANK_RE = re.compile(r"\bmeme[\s-]?bank\b", re.I)
# Saying it's a deposit: "a meme for the meme bank", "store this in the meme bank", "needs to be added again to
# the meme bank", "meme bank deposit", or the :bank: emoji. "in/to the meme bank" needs a verb that puts something
# there, so "is this already in the meme bank?" isn't one. Merely mentioning the meme bank asks first ("confirm").
_PUT = (r"(?:add|adding|added|put|putting|store|storing|stored|save|saving|saved|stick|file|filed|deposit|deposited"
        r"|bank|banked|throw|toss|send|goes|go|going)")
DEPOSIT_RE = re.compile(
    rf"\b{_PUT}\b(?:\s+[^\s.!?]+){{0,3}}?\s+(?:in|into|to|onto)\s+(?:the\s+|our\s+)?meme[\s-]?bank\b"
    r"|\bfor\s+(?:the\s+|our\s+)?meme[\s-]?bank\b"
    r"|\bmeme[\s-]?bank\s+(?:deposit|submission|entry)\b"
    r"|:bank:",
    re.I,
)

# Any negation at all (every n't contraction, "not", "never", "no"...), and sentences that ask rather than tell. The
# rule is deliberately cautious: when in doubt it isn't a deposit, and the poster just gets asked.
NEGATION_RE = re.compile(r"\b(?:\w+n't|not|never|no|nope|nobody|nothing|without|dont|doesnt|didnt|wont|wouldnt"
                         r"|shouldnt|cant|isnt|arent|wasnt)\b", re.I)
_QUESTION_START = re.compile(r"^\W*(?:did|does|do|has|have|had|is|was|were|are|am|should|shall|can|could|would|will"
                             r"|may|might|must|why|when|where|who|whom|what|which|how|wonder)\b", re.I)


def says_deposit(text: str) -> bool:
    """Deposit wording in a sentence that tells rather than asks and has no negation anywhere in it. "Please don't add
    this to the meme bank", "A meme for the meme bank, but don't bank it yet" and "Did I add this to the meme bank"
    (with or without the ?) aren't deposits: they go to the ask-first path."""
    text = (text or "").replace("’", "'").replace("‘", "'")  # phones type curly apostrophes
    if ":bank:" in text:
        return True  # the emoji is as explicit as the :bank: reaction, whatever the words around it say
    text = re.sub(r"<[^>]*>|https?://\S+", " ", text)  # links (<url|label>, <@user>) have dots that aren't full stops
    text = re.sub(r"\s+", " ", text)  # a line break isn't the end of a sentence ("This is not\nfor the meme bank")
    for sentence in re.findall(r"[^.!?]+[.!?]*", text):  # each sentence on its own: "Don't add that. Store this ..."
        if not DEPOSIT_RE.search(sentence):
            continue
        if sentence.rstrip().endswith("?") or _QUESTION_START.search(sentence):
            continue  # asks about the meme bank
        if NEGATION_RE.search(sentence):
            continue  # negated somewhere in the same sentence
        return True
    return False


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
    kind: str  # "bank", "confirm" (an image that only mentions the meme bank: ask the poster first) or "ask"
    rest: str = ""


def strip_mentions(text: str) -> str:
    return re.sub(r"\s+", " ", MENTION_RE.sub(" ", text or "")).strip()


def classify(text: str, has_images: bool, bot_user_id: str | None = None, is_dm: bool = False) -> Trigger | None:
    text = text or ""
    mentioned = bool(bot_user_id) and f"<@{bot_user_id}" in text

    if has_images and (says_deposit(text) or mentioned or is_dm):
        return Trigger("bank")
    if has_images:
        return Trigger("confirm") if BANK_RE.search(text) else None

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
    text = (text or "").replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")  # Slack escapes these three
    pieces = _QUOTED.split(text)  # [outside, quote, quote, outside, quote, quote, outside, ...]
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


# Questions about the bot itself, however they're put: "can you explain how you work to everyone?", "how do I use
# you", "what can you do". "help" alone counts, but "help me find the France meme" is a search.
_ME = r"(?:you|yourself|the\s+(?:meme[\s-]?bank|bot))"
_SELF = r"(?:you|this|it|the\s+(?:meme[\s-]?bank|bot))"  # "how ___ works"
_PEOPLE = r"(?:me|us|everyone|everybody|people|all|the\s+(?:channel|group|team|gang))"
_HELP_RE = re.compile(
    r"(?:(?:so|ok|okay|and|then|well)[\s,]+)?(?:(?:can|could|would|will)\s+you\s+)?(?:please\s+)?(?:"
    r"help|commands|usage|instructions|menu"
    rf"|(?:explain|describe|introduce|tell\s+{_PEOPLE})(?:\s+(?:to\s+)?{_PEOPLE})?\s+"
    rf"(?:how\s+{_SELF}\s+works?|how\s+to\s+use\s+{_SELF}|(?:about\s+)?{_ME}|what\s+you\s+do)"
    rf"|how\s+(?:do|does|can|should)\s+{_SELF}\s+work"
    rf"|how\s+(?:do|does|can|should)\s+(?:i|we|you|people|everyone)\s+use\s+{_SELF}"
    rf"|how\s+{_SELF}\s+works?|how\s+to\s+use\s+{_SELF}"
    r"|what\s+(?:can|do|does)\s+(?:you|it|the\s+meme[\s-]?bank)\s+do"
    r"|what\s+(?:are\s+)?(?:your|the)\s+commands|(?:who|what)\s+are\s+you"
    rf")(?:\s+(?:to|for|with)\s+{_PEOPLE}|\s+(?:again|please|here|now))*",
    re.I,
)


def parse_command(rest: str) -> Command:
    raw = (rest or "").strip().rstrip("?!. ")
    low = raw.lower()
    if not low or _HELP_RE.fullmatch(low):
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
