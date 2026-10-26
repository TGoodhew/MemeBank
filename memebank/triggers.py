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
_LEADING_GREETING = re.compile(rf"^\s*{_GREETING}\b[\s,:;!.-]*", re.I)
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
        # "hey @Meme Bank, random" / "@Meme Bank hi, random": the greeting was for the bot, not part of the ask
        return Trigger("ask", (m.group("rest") if m else _LEADING_GREETING.sub("", rest)).strip())

    m = ASK_WITH_GREETING.match(text) or ASK_WITH_PUNCT.match(text)
    if m:
        return Trigger("ask", strip_mentions(m.group("rest")))
    if is_dm:
        return Trigger("ask", strip_mentions(text))
    return None


@dataclass
class Command:
    name: str  # help | random | stats | get | info | forget | restore | retag | tag | untag | search
    number: int | None = None  # None on a NUMBERED command: "this one", the meme the conversation is about
    query: str = ""
    or_search: bool = False  # just hashtags ("#mondays"): tags for the meme being talked about, else a search


NUMBERED = {"get", "info", "forget", "restore", "retag", "tag", "untag"}


_QUOTED = re.compile(r'"([^"]+)"|“([^”]+)”')
# A hashtag has a letter in it: "#tooReal", "#2020s". "#12" is a meme number.
_HASHTAG = re.compile(r"#(?=[\w'-]*[^\W\d_])[\w'-]+")


def _hashtag_words(tag: str) -> str:
    """'#tooReal' and '#too_real' -> 'too real', like the tags Claude writes. '#tooreal' stays as typed, and so does
    '#iPhone' (it takes two lowercase letters before a capital to start a new word)."""
    return " ".join(re.sub(r"(?<=[a-z]{2})(?=[A-Z])|[_-]+", " ", tag.lstrip("#").strip("'")).split())


def hashtags_as_words(text: str) -> str:
    """'#tooReal memes' -> 'too real memes': hashtags in the form they're stored in, for searching."""
    return _HASHTAG.sub(lambda m: _hashtag_words(m.group()).lower(), text or "")


def split_tags(text: str) -> list[str]:
    """'"Distracted boyfriend" and "Beanie Girl"' -> ['Distracted boyfriend', 'Beanie Girl']. Quoted phrases stay whole
    (so "rock and roll" can be one tag); the rest splits on commas, semicolons, & and the word "and", and each
    hashtag is a tag of its own, whole ("#tooReal #rock-and-roll" -> ['too real', 'rock and roll'])."""
    text = (text or "").replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")  # Slack escapes these three
    pieces = _QUOTED.split(text)  # [outside, quote, quote, outside, quote, quote, outside, ...]
    out: list[str] = []
    for i in range(0, len(pieces), 3):
        # [words, hashtag, words, hashtag, ...]: hashtags come out first, so "and" inside one doesn't split it
        for j, part in enumerate(re.split(f"({_HASHTAG.pattern})", pieces[i])):
            if j % 2:
                out.append(_hashtag_words(part))
            else:
                out += [w.strip(" '\"“”") for w in re.split(r"[,;&]|\band\b", part, flags=re.I)]
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


_THIS = r"(?:(?:this|that)(?:\s+one)?|it)"
_THIS_NOT_IT = r"(?:this|that)(?:\s+one)?"
# Which meme a tag goes on or comes off: "this"/"it", or "#12"/"meme 12"/"12" (one capture group: the number).
# (?![\w'-]) stops "#2020s" or "#12-monkeys" (hashtags) being read as a meme number.
_TARGET = rf"(?:{_THIS}\b|(?:meme\s+)?#?(\d+)(?![\w'-]))"
_A = r"(?:(?:a|an|the|some|these)\s+)"
_WORDS = r"(?:\s*[:,-]?\s*(.+))?"  # the tags, if any
_HASHTAGS = rf"{_HASHTAG.pattern}(?:[\s,]+(?:and\s+)?{_HASHTAG.pattern})*"
_REMOVE = r"(?:untag|remove|drop|delete|take\s+off|take|strip)"


def _number(digits: str | None) -> int | None:
    return int(digits) if digits else None


# Manners around a command: "can you please forget #12", "#12 please", "show #12, thanks!"
_POLITE_START = re.compile(r"^(?:(?:(?:can|could|would|will)\s+you|(?:i'?d|i\s+would)\s+like\s+you\s+to|kindly|please|pls"
                           r"|plz)[\s,]+)+", re.I)
_POLITE_END = re.compile(r"(?:[\s,]+(?:please|pls|plz|thanks|thank\s+you|thx|ty|ta|cheers|:pray:|🙏)"
                         r"(?:\s+(?:so\s+much|a\s+lot|very\s+much))?[\s!.]*)+$", re.I)


def parse_command(rest: str) -> Command:
    # Slack sends "#name" as a link when a channel of that name exists: it's still the tag "name" here
    rest = re.sub(r"<#[A-Z0-9]+\|([^>|]+)>", r"#\1", rest or "")
    raw = rest.strip().rstrip("?!. ")
    # A command is the same command with or without the manners. A search keeps them, though, so "please clap" or
    # "thank you memes" are still what gets searched for.
    plain = _POLITE_END.sub("", _POLITE_START.sub("", raw)).strip().rstrip("?!.,: ")
    if plain and plain != raw:
        cmd = _parse(plain)
        if cmd.name != "search":
            return cmd
    return _parse(raw)


def _any_number(m: re.Match) -> int | None:
    return _number(next((g for g in m.groups() if g), None))


def _parse(raw: str) -> Command:
    low = raw.lower()
    if not low or _HELP_RE.fullmatch(low):
        return Command("help")
    if re.fullmatch(r"(?:give\s+me\s+|show\s+me\s+)?(?:a\s+)?(?:random(?:\s+(?:one|meme))?|surprise\s+me|dealer'?s\s+choice|anything)", low):
        return Command("random")
    if re.fullmatch(r"(?:(?:show|give)\s+(?:me\s+|us\s+)?(?:the\s+)?)?"
                    r"(?:stats|statistics|status|balance|how\s+many(?:\s+memes)?.*|what'?s\s+in\s+the\s+(?:bank|vault))", low):
        return Command("stats")
    # "#12", or "this"/"that one"/"it" for the meme being talked about (a thread's meme, or a DM's last deposit).
    # "forget it" alone is left out: it usually means "never mind". "delete it", "remove it" and "forget it from the
    # bank" don't.
    m = re.fullmatch(rf"(?:forget\s+(?:{_THIS_NOT_IT}|it(?=\s+from))|(?:delete|remove|withdraw)\s+{_THIS}"
                     r"|(?:forget|delete|remove|withdraw)\s+(?:meme\s+)?#?(\d+))"
                     r"(?:\s+from\s+(?:the\s+|our\s+)?(?:meme[\s-]?bank|bank|vault))?", low)
    if m:
        return Command("forget", _number(m.group(1)))
    m = re.fullmatch(r"(?:restore|unforget|undelete|un-?withdraw|undo\s+forget|bring\s+back)\s+"
                     rf"(?:{_THIS}|(?:meme\s+)?#?(\d+))|bring\s+(?:{_THIS}|(?:meme\s+)?#?(\d+))\s+back", low)
    if m:
        return Command("restore", _any_number(m))
    m = re.fullmatch(rf"(?:retag|re-tag|redo)\s+(?:{_THIS}|(?:meme\s+)?#?(\d+))"
                     rf"|(?:redo|refresh)\s+(?:the\s+|its\s+)?tags(?:\s+(?:on|for)\s+{_TARGET})?", low)
    if m:
        return Command("retag", _any_number(m))
    # "what number is this?" · "what's this one's number" · "what are the tags on #12?" · "show tags for this" · "tags?"
    m = re.fullmatch(rf"(?:what|which)\s+(?:number|one|meme)\s+is\s+{_THIS}"
                     rf"|what(?:'s|\s+is)\s+(?:its|(?:this|that)(?:\s+one)?'s)\s+number"
                     rf"|(?:(?:what\s+are|show(?:\s+me)?|list|get)\s+)?(?:the\s+|its\s+|(?:this|that)(?:\s+one)?'s\s+)?"
                     rf"tags(?:\s+(?:on|for|of)\s+{_TARGET})?"
                     rf"|(?:how|what)(?:'s|\s+is)\s+{_THIS}\s+tagged(?:\s+(?:with|as))?", low)
    if m:
        return Command("info", _any_number(m))
    # Tags: the words keep their own text (comma separated, or hashtags) in query. number=None is "this one": the bot
    # fills in the meme the thread or DM is about, or asks which. (?![\w'-]) stops "tag #12" (no words) being read as
    # "tag #1 with 2", and "tag #12-monkeys" (a hashtag) being read as meme #12.
    flags = re.I | re.S
    # A meme named at the end wins over a number the tag starts with: "add a tag 12 monkeys to #7" is for #7, and
    # "remove the tag day off from #12" takes "day off" off #12. (?!tags?\s) leaves "add tags to #3: x" for below.
    m = re.fullmatch(rf"add\s+{_A}?(?:tags?\s+)?(?!tags?\s)(.+?)\s+(?:to|on)\s+{_TARGET}", raw, flags)
    if m:
        return Command("tag", _number(m.group(2)), query=m.group(1).strip())
    m = re.fullmatch(rf"{_REMOVE}\s+{_A}?(?:tags?\s+)?(?!tags?\s)(.+?)(?:\s+tags?)?\s+(?:from|off(?:\s+of)?)\s+{_TARGET}",
                     raw, flags)
    if m:
        return Command("untag", _number(m.group(2)), query=m.group(1).strip())
    # "tag #12 dave, cabin trip" · "add tags to #3: x" · "tag #12 as #tooReal"
    m = re.fullmatch(rf"(?:add\s+{_A}?)?tags?\s+(?:to\s+)?(?:meme\s+)?#?(\d+)(?![\w'-])(?:\s+(?:with|as)\b)?{_WORDS}",
                     raw, flags)
    if m:
        return Command("tag", int(m.group(1)), query=(m.group(2) or "").strip())
    m = re.fullmatch(rf"(?:untag|remove\s+tags?)\s+(?:meme\s+)?#?(\d+)(?![\w'-]){_WORDS}", raw, flags)  # "untag #12 squad"
    if m:
        return Command("untag", int(m.group(1)), query=(m.group(2) or "").strip())
    # "untag this laser tag" · "untag tag team" · "untag cabin trip": everything after "untag" (and "this") is the tag
    m = re.fullmatch(rf"untag\s+(?:{_TARGET})?{_WORDS}", raw, flags)
    if m:
        return Command("untag", _number(m.group(1)), query=(m.group(2) or "").strip())
    # "remove the tag too real" · "drop the #tooReal tag" · "take the tag cabin trip off" · "take #tooReal off"
    for pattern in (rf"take\s+{_A}?tags?\s+(.+?)\s+off",  # "take ... off" before "take ...", so "off" isn't a tag
                    rf"{_REMOVE}\s+{_A}?tags?\b(?:\s+(.+))?",
                    rf"{_REMOVE}\s+{_A}?(.+?)\s+tags?",
                    rf"{_REMOVE}\s+({_HASHTAGS})(?:\s+off)?"):
        m = re.fullmatch(pattern, raw, flags)
        if m:
            return Command("untag", None, query=(m.group(1) or "").strip())
    # "Tag it "Distracted boyfriend" and "Beanie Girl"" · "add a tag #tooReal" · "tag this as too real" ·
    # "tag #tooReal" · "add #tooReal"
    m = re.fullmatch(rf"(?:please\s+)?(?:tag\s+{_THIS}\b|tag\b|add\s+{_A}?tags?\b)(?:\s+(?:with|as|called)\b)?{_WORDS}",
                     raw, flags)
    if m:
        return Command("tag", None, query=(m.group(1) or "").strip())
    m = re.fullmatch(rf"add\s+({_HASHTAGS})", raw, flags)
    if m:
        return Command("tag", None, query=m.group(1))
    if re.fullmatch(_HASHTAGS, raw):  # just "#tooReal": a tag in a meme's thread, a search anywhere else
        return Command("tag", None, query=raw, or_search=True)
    m = re.fullmatch(r"(?:(?:show|get|post)\s+(?:me\s+)?)?(?:meme\s+)?(?:#|number\s+|no\.?\s*)(\d+)", low)
    if m:
        return Command("get", int(m.group(1)))
    query = _FILLER.sub("", raw).strip() or raw
    return Command("search", query=query)
