"""The meme bank itself: deposits (bank an image post) and withdrawals (answer "hey meme bank, ...")."""
from __future__ import annotations

import collections
import difflib
import hashlib
import logging
import random
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from . import images
from .search import search
from .store import Meme
from .tagger import Tags, clean_tags
from .triggers import NUMBERED, Command, hashtags_as_words, parse_command, split_tags

log = logging.getLogger(__name__)

DUPLICATE_DISTANCE = 6  # dHash bits; reposts and re-screenshots land well inside this
# ...but so do different memes on the same template, so a near picture is only a repost if its text matches too
TEXT_MATCH = 0.9
MAX_EXTRA_TAGS = 100  # tags people can add to one meme ("tag #12 ...")
CONFIRM_EVENT = "memebank_confirm_deposit"  # metadata on the "want me to bank it?" DM

HELP = """*The Meme Bank* :bank:
*Deposit:* post an image saying it's for the bank ("a meme for the meme bank", "store this in the meme bank", or :bank:), react to any image post with :bank:, or DM me an image. I'll keep a copy and tag it. (Just mentioning the meme bank with a picture gets you a DM asking if you meant it.)
*Withdraw:* start a message with "hey meme bank," then ask:
• `hey meme bank, find me the France meme`
• `hey meme bank, what have you got on Mondays?`
• `hey meme bank, #12`  ·  `random`  ·  `stats`
• `hey meme bank, tag #12 names, in-jokes` or `add #tooReal to #12` (anyone; `untag #12 word` for the poster or a bank manager)  ·  `retag #12` (Claude again)
• `hey meme bank, forget #12` (asks a bank manager to remove it; they decide, and `restore #12` undoes it)
*In a meme's thread*, "this" or "it" is that meme: `@Meme Bank add a tag #tooReal`  ·  `remove the tag cabin trip`  ·  `retag this`  ·  `forget this`
(`#12` is a meme's number and `#tooReal` is a tag. For a tag that's all digits, quote it: `tag #12 "2020"`.)
You can also @mention me, or DM me a question without the "hey meme bank"."""


@dataclass
class ImageRef:
    key: str
    url: str
    slack_auth: bool
    name: str = ""


@dataclass
class Deposit:
    meme: Meme
    status: str  # "new" | "already" | "duplicate" | "withdrawn" (this very post was banked, then forgotten)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def same_text(a: str, b: str) -> bool:
    """Whether two images' transcribed text is the same, allowing for case, punctuation and small misreads."""
    a, b = (" ".join(re.sub(r"[\W_]+", " ", (s or "").casefold()).split()) for s in (a, b))  # keeps any script's letters
    if not a or not b:
        return a == b  # two text-free pictures go on the hash alone; text on only one of them means different memes
    # autojunk would ignore common characters in text over 200 long, so one misread could sink a long quote
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() >= TEXT_MATCH


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
    def __init__(self, store, tagger, bot_token: str = "", admin_user_ids=(), ack_mode: str = "thread", fetch=None,
                 dm_tags: bool = True):
        self.store = store
        self.tagger = tagger
        self.bot_token = bot_token
        self.admins = set(admin_user_ids)
        self.ack_mode = ack_mode
        self.dm_tags = dm_tags  # after a new deposit, DM whoever banked it the tags and how to add more
        self._fetch = fetch or images.fetch
        self._deposit_lock = threading.Lock()  # one deposit at a time keeps numbering and dedup simple
        self._asked: set[tuple[str, str]] = set()  # posts we've already asked their poster about (ask_to_bank)
        self._asked_lock = threading.Lock()

    # ------------------------------------------------------------------ deposits
    def deposit_bytes(self, data: bytes, source_key: str, caption: str = "", posted_by: str = "",
                      posted_at: str = "", channel_id: str = "", message_ts: str = "", permalink: str = "") -> Deposit:
        with self._deposit_lock:
            existing = self.store.find_by_source(source_key)
            if existing:
                return Deposit(existing, "withdrawn" if existing.withdrawn_at else "already")
            content_type = images.sniff(data)
            h = images.dhash(data)
            near = [(images.hamming(h, m.dhash), m) for m in self.store.all() if m.dhash]
            near = sorted((x for x in near if x[0] <= DUPLICATE_DISTANCE), key=lambda x: x[0])
            small, small_type = images.for_claude(data)
            tags: Tags = self.tagger.tag(small, small_type, caption)
            for _, m in near:  # closest picture first
                if same_text(tags.text_in_image, m.text_in_image):
                    return Deposit(m, "duplicate")
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

    def deposit_message(self, client, channel: str, message: dict, acknowledge: bool = True,
                        banked_by: str = "") -> list[Deposit]:
        """banked_by: who asked for it (the poster, or whoever added the :bank: reaction)."""
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
                found = self.store.find_by_source(ref.key)
                if found:  # skip the download for repeats (edits, retries)
                    results.append(Deposit(found, "withdrawn" if found.withdrawn_at else "already"))
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
            in_dm = channel.startswith("D")
            self._acknowledge(client, channel, ts, results, thread, in_dm)
            if self.dm_tags and not in_dm:  # a DM deposit gets the same hint in its own thread instead
                for r in results:
                    if r.status == "new":
                        self._dm_tags(client, banked_by or message.get("user", ""), r.meme)
        return results

    def ask_to_bank(self, client, channel: str, message: dict) -> bool:
        """An image that only *mentions* the meme bank: DM the poster to check instead of banking it. The DM carries
        the post's channel and ts as message metadata, so a yes or no (in its thread, or sent straight into the DM)
        knows which post it's about."""
        user, ts = message.get("user", ""), message.get("ts", "")
        if not user.startswith(("U", "W")):
            return False
        with self._asked_lock:  # a post and its link preview can arrive at once, on different Bolt threads
            if (channel, ts) in self._asked:
                return False
            self._asked.add((channel, ts))
        try:
            banked = any(self.store.find_by_source(r.key) for r in images_in(message, channel))
        except Exception:  # noqa: BLE001  (storage unreachable: don't ask now, but let a later event try)
            log.exception("Couldn't check whether %s/%s is banked", channel, ts)
            with self._asked_lock:
                self._asked.discard((channel, ts))
            return False
        if banked:
            return False  # already in the vault, nothing to ask (and no need to ask later)
        payload = {"channel": channel, "ts": ts}
        if message.get("thread_ts") and message["thread_ts"] != ts:
            payload["thread_ts"] = message["thread_ts"]  # a reply in a thread: where to look it up
        post = "your post"
        try:
            post = f"<{client.chat_getPermalink(channel=channel, message_ts=ts)['permalink']}|your post>"
        except Exception:  # noqa: BLE001
            pass
        try:
            client.chat_postMessage(
                channel=user, unfurl_links=False,
                text=(f"You mentioned the meme bank with a picture in <#{channel}> ({post}). Want me to bank it? "
                      f"Reply `yes` or `no`, or react :bank: on the post. Otherwise I'll leave it be."),
                metadata={"event_type": CONFIRM_EVENT, "event_payload": payload})
        except Exception:  # noqa: BLE001
            log.exception("Couldn't ask %s about %s/%s", user, channel, ts)
            with self._asked_lock:
                self._asked.discard((channel, ts))  # it never arrived, so a later event can try again
            return False
        return True

    def _tag_hint(self, meme: Meme) -> str:
        return (f"Here are the tags I used:\n_{', '.join(meme.all_tags)}_\n"
                f"Want to add others? Reply here with `tag it cabin trip, team lunch` (or just the words, in this thread), "
                f"or from anywhere: `hey meme bank, tag #{meme.number} names, in-jokes` "
                f"(or `hey meme bank, tag it …` in the meme's thread)")

    def _dm_tags(self, client, user: str, meme: Meme) -> None:
        if user.startswith(("U", "W")):  # a Slack person, not a bot or a local import
            self._say(client, user, f"Banked *#{meme.number}: {meme.title}*. {self._tag_hint(meme)}")

    def _acknowledge(self, client, channel: str, ts: str, results: list[Deposit], thread: str = "",
                     in_dm: bool = False) -> None:
        fresh = [r for r in results if r.status == "new"]
        dupes = [r for r in results if r.status == "duplicate"]
        if fresh or dupes:
            try:
                client.reactions_add(channel=channel, timestamp=ts, name="bank")
            except Exception:  # noqa: BLE001  (already_reacted etc.)
                pass
        hint_in_dm = in_dm and self.dm_tags
        quiet = self.ack_mode != "thread"  # react only... except a DM deposit, whose thread is the tag hint's home
        if quiet and not hint_in_dm:
            return
        lines = []
        for r in fresh:
            if hint_in_dm:
                lines.append(f"Banked as *#{r.meme.number}: {r.meme.title}*. {self._tag_hint(r.meme)}")
            else:
                lines.append(f"Banked as *#{r.meme.number}: {r.meme.title}*\n_{', '.join(r.meme.all_tags[:12])}_")
        for r in [] if quiet else dupes:
            lines.append(f"Already in the vault as *#{r.meme.number}: {r.meme.title}*. The meme bank never forgets.")
        for r in [] if quiet else results:
            if r.status == "withdrawn":
                lines.append(f"*#{r.meme.number}: {r.meme.title}* was withdrawn from the vault. "
                             f"`hey meme bank, restore #{r.meme.number}` brings it back.")
        if lines:
            self._say(client, channel, "\n".join(lines), thread_ts=thread or ts)

    # ------------------------------------------------------------------ withdrawals
    def handle_ask(self, client, channel: str, user: str, rest: str, thread_ts: str | None = None,
                   about: int | None = None) -> Command:
        """about: the meme the conversation is about (the meme in this thread, or a DM's last deposit), which is what
        "tag it ..." or "forget this" without a number mean."""
        cmd = parse_command(rest)
        if cmd.name in NUMBERED and cmd.number is None:
            cmd.number = about
            if about is None and cmd.or_search:  # "hey meme bank, #mondays" with no meme in sight
                cmd = Command("search", query=hashtags_as_words(cmd.query))
        memes = self.store.all()
        if cmd.name == "help":
            self._say(client, channel, HELP, thread_ts)
        elif cmd.name in NUMBERED and cmd.number is None:  # "tag it ..." with no meme to point at
            example = f"{cmd.name} #12 words, more words" if cmd.name in ("tag", "untag") else f"{cmd.name} #12"
            self._say(client, channel, f"Which meme? Reply in its thread, or say `hey meme bank, {example}`.", thread_ts)
        elif cmd.name in NUMBERED:
            self._numbered(client, channel, user, cmd, thread_ts)
        elif not memes:
            self._say(client, channel, "The vault is empty. Post something \"for the meme bank\" to make the first deposit.", thread_ts)
        elif cmd.name == "random":
            self._post_meme(client, channel, random.choice(memes), thread_ts, "Dealer's choice:")
        elif cmd.name == "stats":
            self._say(client, channel, self._stats(memes), thread_ts)
        else:
            self._search(client, channel, cmd.query, memes, thread_ts)
        return cmd

    def _numbered(self, client, channel, user, cmd: Command, thread_ts):
        """Commands about one meme by number. Withdrawn memes only answer to "restore"."""
        meme = self.store.get(cmd.number, include_withdrawn=True)
        n = cmd.number
        allowed = bool(user) and meme is not None and (user == meme.posted_by or user in self.admins)
        if not meme:
            self._say(client, channel, f"There's no #{n} in the vault.", thread_ts)
        elif cmd.name == "restore":  # removing and restoring memes is the bank managers' call
            if not meme.withdrawn_at:
                self._say(client, channel, f"#{n} {meme.title} is already in the vault.", thread_ts)
            elif user in self.admins:
                meme.withdrawn_at = ""
                self.store.put(meme, None)
                self._say(client, channel, f"Restored: #{n} {meme.title} is back in the vault.", thread_ts)
            else:
                self._say(client, channel, f"Only a bank manager can restore #{n}.", thread_ts)
        elif meme.withdrawn_at:
            self._say(client, channel, f"#{n} was withdrawn from the vault. A bank manager can `restore #{n}`.", thread_ts)
        elif cmd.name == "get":
            self._post_meme(client, channel, meme, thread_ts)
        elif cmd.name == "forget":
            if user in self.admins:
                meme.withdrawn_at = _now()
                self.store.put(meme, None)  # hidden, not deleted: the row and image stay for "restore"
                self._say(client, channel, f"Withdrawn: #{n} {meme.title} is out of the vault. "
                                           f"`hey meme bank, restore #{n}` puts it back.", thread_ts)
            elif not self.admins:
                self._say(client, channel, f"There's no bank manager set up to remove #{n} (ADMIN_USER_IDS).", thread_ts)
            else:  # everyone else asks; a bank manager decides
                where = f" from <#{channel}>" if channel.startswith(("C", "G")) else ""
                told = [self._say(client, admin, f"<@{user}> asked to remove *#{n}: {meme.title}*{where}. "
                                                 f"`hey meme bank, forget #{n}` removes it; otherwise nothing changes.")
                        for admin in self.admins]
                if any(told):
                    self._say(client, channel, f"I've asked a bank manager to remove #{n} {meme.title}.", thread_ts)
                else:
                    self._say(client, channel, f"I couldn't reach a bank manager just now, so #{n} is unchanged. "
                                               f"Try `hey meme bank, forget #{n}` again in a bit.", thread_ts)
        elif cmd.name == "tag":  # anyone can add tags
            words = clean_tags(split_tags(cmd.query))
            new = [t for t in words if t not in meme.all_tags]
            had = [t for t in words if t in meme.all_tags]
            added = new[:max(0, MAX_EXTRA_TAGS - len(meme.extra_tags))]
            full = f" ({len(new) - len(added)} skipped: #{n} has the most added tags it can hold)" if len(added) < len(new) else ""
            already = f" (already had: {', '.join(had)})" if had else ""
            if not words:
                self._say(client, channel, f"Give me some words to tag #{n} with, comma separated.", thread_ts)
            elif added:
                meme.extra_tags = meme.extra_tags + added  # already cleaned and new, so no re-clean (it would cap at 40)
                self.store.put(meme, None)
                self._say(client, channel, f"Tagged *#{n}: {meme.title}* with _{', '.join(added)}_{already}{full}\n"
                                           f"All tags: _{', '.join(meme.all_tags)}_", thread_ts)
            elif new:
                self._say(client, channel, f"#{n} can't take more added tags{full}.", thread_ts)
            else:
                self._say(client, channel, f"#{n} already has those tags.", thread_ts)
        elif cmd.name == "untag":
            words = clean_tags(split_tags(cmd.query))
            # spaces don't count, so "#tooreal" takes off "too real" (what "#tooReal" was stored as) and vice versa
            unspaced = {w.replace(" ", "") for w in words}
            removed = [t for t in meme.all_tags if t.replace(" ", "") in unspaced]
            if not allowed:
                self._say(client, channel, f"Only whoever deposited #{n} (or a bank manager) can remove its tags.", thread_ts)
            elif not words:
                self._say(client, channel, f"Which tag should I remove from #{n}? e.g. `untag #{n} cabin trip`", thread_ts)
            elif not removed:
                self._say(client, channel, f"#{n} doesn't have {', '.join(words)}.", thread_ts)
            else:
                meme.tags = [t for t in meme.tags if t not in removed]  # a retag may bring Claude's back
                meme.extra_tags = [t for t in meme.extra_tags if t not in removed]
                self.store.put(meme, None)
                self._say(client, channel, f"Removed _{', '.join(removed)}_ from *#{n}: {meme.title}*\n"
                                           f"All tags: _{', '.join(meme.all_tags)}_", thread_ts)
        else:
            self._retag(client, channel, meme, thread_ts)

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
        kept = f"\n(kept the tags people added: {', '.join(meme.extra_tags)})" if meme.extra_tags else ""
        self._say(client, channel, f"Re-tagged *#{meme.number}: {meme.title}*\n_{', '.join(meme.tags[:12])}_{kept}", thread_ts)

    @staticmethod
    def _stats(memes: list[Meme]) -> str:
        counts = collections.Counter(t for m in memes for t in m.all_tags)
        top = ", ".join(f"{t} ({n})" for t, n in counts.most_common(10))
        newest = memes[-1]
        return (f":bank: *{len(memes)} memes* on deposit.\nNewest: #{newest.number} {newest.title}\n"
                f"Most common tags: {top}")

    def _post_meme(self, client, channel, meme: Meme, thread_ts=None, lead: str = "", extra: str = ""):
        who = f" deposited by <@{meme.posted_by}>" if meme.posted_by.startswith("U") else ""
        n = meme.number
        how = (f"\n_#{n} · add tags: `hey meme bank, tag #{n} …` (or in its thread, `hey meme bank, tag it …`)"
               f" · ask to remove: `hey meme bank, forget #{n}`_")
        comment = f"{lead + ' ' if lead else ''}*#{n}: {meme.title}*{who}{extra}{how}"
        try:
            data = self.store.get_image(meme)
            client.files_upload_v2(channel=channel, thread_ts=thread_ts, file=data,
                                   filename=meme.blob_name, title=meme.title, initial_comment=comment)
        except Exception:  # noqa: BLE001
            log.exception("Upload failed for #%s, falling back to a link", meme.number)
            self._say(client, channel, comment + (f"\n{meme.permalink}" if meme.permalink else ""), thread_ts)

    def say(self, client, channel, text, thread_ts=None) -> bool:
        return self._say(client, channel, text, thread_ts)

    @staticmethod
    def _say(client, channel, text, thread_ts=None) -> bool:
        """Post a message; False (and logged) if Slack refused it."""
        try:
            client.chat_postMessage(channel=channel, text=text, thread_ts=thread_ts, unfurl_links=False)
            return True
        except Exception:  # noqa: BLE001
            log.exception("Couldn't post to %s", channel)
            return False


def local_source_key(data: bytes) -> str:
    return "local:" + hashlib.sha256(data).hexdigest()
