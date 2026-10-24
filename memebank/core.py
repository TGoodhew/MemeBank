"""The meme bank itself: deposits (bank an image post) and withdrawals (answer "hey meme bank, ...")."""
from __future__ import annotations

import collections
import hashlib
import logging
import random
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from . import images
from .search import search
from .store import Meme
from .tagger import Tags
from .triggers import Command, parse_command

log = logging.getLogger(__name__)

DUPLICATE_DISTANCE = 6  # dHash bits; reposts and re-screenshots land well inside this

HELP = """*The Meme Bank* :bank:
*Deposit:* post an image with "meme for the meme bank" in the message (or react to any image post with :bank:). I'll keep a copy and tag it.
*Withdraw:* start a message with "hey meme bank," then ask:
• `hey meme bank, find me the France meme`
• `hey meme bank, what have you got on socialism?`
• `hey meme bank, #12`  ·  `random`  ·  `stats`
• `hey meme bank, forget #12` (your own deposits)
You can also @mention me or DM me."""


@dataclass
class ImageRef:
    key: str
    url: str
    slack_auth: bool
    name: str = ""


@dataclass
class Deposit:
    meme: Meme
    status: str  # "new" | "already" | "duplicate"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ts_to_iso(ts: str) -> str:
    try:
        return datetime.fromtimestamp(float(ts), timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return ""


def images_in(message: dict, channel: str) -> list[ImageRef]:
    refs = []
    for f in message.get("files") or []:
        if str(f.get("mimetype", "")).startswith("image/") and f.get("mode") != "tombstone":
            url = f.get("url_private_download") or f.get("url_private")
            if url:
                refs.append(ImageRef(f"slack:{f['id']}", url, True, f.get("name", "")))
    for i, a in enumerate(message.get("attachments") or []):  # link unfurls, e.g. an x.com post with a picture
        url = a.get("image_url")
        if url:
            refs.append(ImageRef(f"unfurl:{channel}:{message.get('ts')}:{i}", url, False, a.get("title", "")))
    return refs


class MemeBank:
    def __init__(self, store, tagger, bot_token: str = "", admin_user_ids=(), ack_mode: str = "thread", fetch=None):
        self.store = store
        self.tagger = tagger
        self.bot_token = bot_token
        self.admins = set(admin_user_ids)
        self.ack_mode = ack_mode
        self._fetch = fetch or images.fetch
        self._deposit_lock = threading.Lock()  # one deposit at a time keeps numbering and dedup simple

    # ------------------------------------------------------------------ deposits
    def deposit_bytes(self, data: bytes, source_key: str, caption: str = "", posted_by: str = "",
                      posted_at: str = "", channel_id: str = "", message_ts: str = "", permalink: str = "") -> Deposit:
        with self._deposit_lock:
            existing = self.store.find_by_source(source_key)
            if existing:
                return Deposit(existing, "already")
            content_type = images.sniff(data)
            h = images.dhash(data)
            near = [(images.hamming(h, m.dhash), m) for m in self.store.all() if m.dhash]
            near = [x for x in near if x[0] <= DUPLICATE_DISTANCE]
            if near:
                return Deposit(min(near, key=lambda x: x[0])[1], "duplicate")
            small, small_type = images.for_claude(data)
            tags: Tags = self.tagger.tag(small, small_type, caption)
            number = self.store.next_number()
            meme = Meme(
                number=number, source_key=source_key, title=tags.title, description=tags.description,
                text_in_image=tags.text_in_image, tags=tags.tags, dhash=h, content_type=content_type,
                blob_name=f"{number:06d}{images.EXT.get(content_type, '.png')}", posted_by=posted_by,
                posted_at=posted_at or _now(), channel_id=channel_id, message_ts=message_ts,
                permalink=permalink, caption=caption, banked_at=_now(),
            )
            self.store.put(meme, data)
            log.info("Banked #%s %s", meme.number, meme.title)
            return Deposit(meme, "new")

    def deposit_message(self, client, channel: str, message: dict, acknowledge: bool = True) -> list[Deposit]:
        refs = images_in(message, channel)
        if not refs:
            return []
        ts = message.get("ts", "")
        thread = message.get("thread_ts") or ts  # replies go in the post's thread, or the thread it's already in
        caption = message.get("text", "")
        permalink = ""
        try:
            permalink = client.chat_getPermalink(channel=channel, message_ts=ts)["permalink"]
        except Exception:  # noqa: BLE001
            pass
        results = []
        for ref in refs:
            try:
                if self.store.find_by_source(ref.key):  # skip the download for repeats (edits, retries)
                    results.append(Deposit(self.store.find_by_source(ref.key), "already"))
                    continue
                data = self._fetch(ref.url, self.bot_token if ref.slack_auth else None)
                results.append(self.deposit_bytes(
                    data, ref.key, caption=caption, posted_by=message.get("user", ""),
                    posted_at=_ts_to_iso(ts), channel_id=channel, message_ts=ts, permalink=permalink))
            except Exception as e:  # noqa: BLE001
                log.exception("Couldn't bank %s", ref.key)
                if acknowledge:
                    self._say(client, channel, f":warning: Couldn't bank that one: {e}", thread_ts=thread)
        if acknowledge:
            self._acknowledge(client, channel, ts, results, thread)
        return results

    def _acknowledge(self, client, channel: str, ts: str, results: list[Deposit], thread: str = "") -> None:
        fresh = [r for r in results if r.status == "new"]
        dupes = [r for r in results if r.status == "duplicate"]
        if fresh or dupes:
            try:
                client.reactions_add(channel=channel, timestamp=ts, name="bank")
            except Exception:  # noqa: BLE001  (already_reacted etc.)
                pass
        if self.ack_mode != "thread":
            return
        lines = []
        for r in fresh:
            lines.append(f"Banked as *#{r.meme.number}: {r.meme.title}*\n_{', '.join(r.meme.tags[:12])}_")
        for r in dupes:
            lines.append(f"Already in the vault as *#{r.meme.number}: {r.meme.title}*. The meme bank never forgets.")
        if lines:
            self._say(client, channel, "\n".join(lines), thread_ts=thread or ts)

    # ------------------------------------------------------------------ withdrawals
    def handle_ask(self, client, channel: str, user: str, rest: str, thread_ts: str | None = None) -> Command:
        cmd = parse_command(rest)
        memes = self.store.all()
        if cmd.name == "help":
            self._say(client, channel, HELP, thread_ts)
        elif not memes:
            self._say(client, channel, "The vault is empty. Post something \"for the meme bank\" to make the first deposit.", thread_ts)
        elif cmd.name == "random":
            self._post_meme(client, channel, random.choice(memes), thread_ts, "Dealer's choice:")
        elif cmd.name == "stats":
            self._say(client, channel, self._stats(memes), thread_ts)
        elif cmd.name in ("get", "forget", "retag"):
            meme = self.store.get(cmd.number)
            if not meme:
                self._say(client, channel, f"There's no #{cmd.number} in the vault.", thread_ts)
            elif cmd.name == "get":
                self._post_meme(client, channel, meme, thread_ts)
            elif cmd.name == "forget":
                if user and (user == meme.posted_by or user in self.admins):
                    self.store.delete(meme)
                    self._say(client, channel, f"Withdrawn: #{meme.number} {meme.title} is gone from the vault.", thread_ts)
                else:
                    self._say(client, channel, f"Only whoever deposited #{meme.number} (or a bank manager) can withdraw it.", thread_ts)
            else:
                self._retag(client, channel, meme, thread_ts)
        else:
            self._search(client, channel, cmd.query, memes, thread_ts)
        return cmd

    def _search(self, client, channel, query, memes, thread_ts):
        hits = search(query, memes, self.tagger)
        if not hits:
            self._say(client, channel, f"Nothing in the vault for “{query}”. Sounds like a deposit opportunity.", thread_ts)
            return
        others = ""
        if len(hits) > 1:
            others = "\nAlso in the vault: " + "  ·  ".join(f"#{m.number} {m.title}" for m in hits[1:])
        self._post_meme(client, channel, hits[0], thread_ts, extra=others)

    def _retag(self, client, channel, meme: Meme, thread_ts):
        data = self.store.get_image(meme)
        small, small_type = images.for_claude(data)
        t = self.tagger.tag(small, small_type, meme.caption)
        meme.title, meme.description, meme.text_in_image, meme.tags = t.title, t.description, t.text_in_image, t.tags
        self.store.put(meme, None)
        self._say(client, channel, f"Re-tagged *#{meme.number}: {meme.title}*\n_{', '.join(meme.tags[:12])}_", thread_ts)

    @staticmethod
    def _stats(memes: list[Meme]) -> str:
        counts = collections.Counter(t for m in memes for t in m.tags)
        top = ", ".join(f"{t} ({n})" for t, n in counts.most_common(10))
        newest = memes[-1]
        return (f":bank: *{len(memes)} memes* on deposit.\nNewest: #{newest.number} {newest.title}\n"
                f"Most common tags: {top}")

    def _post_meme(self, client, channel, meme: Meme, thread_ts=None, lead: str = "", extra: str = ""):
        who = f" deposited by <@{meme.posted_by}>" if meme.posted_by.startswith("U") else ""
        comment = f"{lead + ' ' if lead else ''}*#{meme.number}: {meme.title}*{who}{extra}"
        try:
            data = self.store.get_image(meme)
            client.files_upload_v2(channel=channel, thread_ts=thread_ts, file=data,
                                   filename=meme.blob_name, title=meme.title, initial_comment=comment)
        except Exception:  # noqa: BLE001
            log.exception("Upload failed for #%s, falling back to a link", meme.number)
            self._say(client, channel, comment + (f"\n{meme.permalink}" if meme.permalink else ""), thread_ts)

    @staticmethod
    def _say(client, channel, text, thread_ts=None):
        try:
            client.chat_postMessage(channel=channel, text=text, thread_ts=thread_ts, unfurl_links=False)
        except Exception:  # noqa: BLE001
            log.exception("Couldn't post to %s", channel)


def local_source_key(data: bytes) -> str:
    return "local:" + hashlib.sha256(data).hexdigest()
