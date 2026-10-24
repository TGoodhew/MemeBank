"""Finding memes: Claude picks by meaning; a plain keyword match is the fallback if Claude is unavailable."""
from __future__ import annotations

import logging
import re

from .store import Meme

log = logging.getLogger(__name__)

STOP = set("""a an the of to for and or in on at by with about from me us my our you your it its this that these
those is are was were be find show get give fetch post grab please could would can will any some one ones meme
memes thing things related relate relates relating regarding re something stuff what which who got have has""".split())

MAX_CATALOGUE = 800  # beyond this, send Claude the best keyword candidates rather than everything


def _words(s: str) -> list[str]:
    out = []
    for w in re.findall(r"[a-z0-9']+", (s or "").lower()):
        w = w.strip("'")
        if len(w) > 1 and w not in STOP:
            out.append(w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w)
    return out


def keyword_rank(query: str, memes: list[Meme]) -> list[tuple[int, Meme]]:
    q = _words(query)
    ql = (query or "").lower()
    scored = []
    for m in memes:
        tag_words = set(_words(" ".join(m.all_tags)))
        title = set(_words(m.title))
        body = set(_words(" ".join([m.description, m.text_in_image, m.caption])))
        score = sum(3 for t in m.all_tags if " " in t and t in ql)  # multi-word tags like "thomas sowell"
        for w in q:
            score += 3 if w in tag_words else 0
            score += 2 if w in title else 0
            score += 1 if w in body else 0
        scored.append((score, m))
    scored.sort(key=lambda sm: (-sm[0], -sm[1].number))
    return scored


def search(query: str, memes: list[Meme], tagger=None, limit: int = 5) -> list[Meme]:
    if not memes:
        return []
    ranked = keyword_rank(query, memes)
    keyword_hits = [m for s, m in ranked if s > 0]
    if tagger is not None:
        pool = memes if len(memes) <= MAX_CATALOGUE else [m for _, m in ranked[:MAX_CATALOGUE]]
        try:
            picked = tagger.pick(query, [m.catalogue_line() for m in pool])
            by_num = {m.number: m for m in memes}
            results = [by_num[n] for n in dict.fromkeys(picked) if n in by_num]
            if results:
                return results[:limit]
            if not ranked or ranked[0][0] < 3:  # Claude says nothing fits and no strong keyword hit either
                return []
        except Exception:  # noqa: BLE001
            log.exception("Claude search failed; falling back to keywords")
    return keyword_hits[:limit]
