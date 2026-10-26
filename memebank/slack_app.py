"""Slack wiring (Bolt, Socket Mode: the bot dials out to Slack, so it needs no public web address)."""
from __future__ import annotations

import logging
import re

from slack_bolt import App

from .core import CONFIRM_EVENT, MemeBank, images_in
from .triggers import NEGATION_RE, NUMBERED, classify, parse_command

log = logging.getLogger(__name__)

BANKED_RE = re.compile(r"Banked (?:as )?\*#(\d+)")  # the bot's own "Banked as *#12: ..." messages
# How the bot names a meme in anything it says about one: "Banked as *#12: ...", a posted meme ("*#12: ...*"),
# "Already in the vault as *#12: ...", "Tagged *#12: ...".
MEME_REF_RE = re.compile(r"\*#(\d+):")
MAX_THREAD_PAGES = 20  # 200 replies a page: enough for any thread people still reply in
# Answers to "want me to bank it?". "please" alone isn't consent ("please don't"), and a no is checked first.
YES_RE = re.compile(r"\s*(?:please\s+)?(?:y|yes|yep|yeah|yup|sure|ok|okay|bank it|store it|do it|go ahead)\b", re.I)
# A refusal anywhere in the reply wins: "okay, don't bank it" and "sure, but do not store it" are noes.
NO_RE = re.compile(r"^\s*n\b|\b(?:no|nope|nah|not|don'?t|do not|never|leave it|skip it|stop)\b", re.I)
# Sent straight into the DM (not in the question's thread), a reply only counts as an answer if every word in it is
# one of these, so "tag #12 without context" or "no France memes?" stay a command or a search.
ANSWER_WORDS = {"y", "yes", "yep", "yeah", "yup", "sure", "ok", "okay", "please", "bank", "store", "do", "it", "go",
                "ahead", "n", "no", "nope", "nah", "not", "don't", "dont", "never", "leave", "skip", "stop", "thanks",
                "thank", "you", "ta", "cheers"}

IGNORED_SUBTYPES = {"bot_message", "message_deleted", "channel_join", "channel_leave", "channel_topic",
                    "channel_purpose", "channel_name", "pinned_item", "unpinned_item", "tombstone"}


def build_app(cfg, bank: MemeBank) -> App:
    app = App(token=cfg.slack_bot_token)
    auth = app.client.auth_test()
    bot_user_id, bot_id = auth["user_id"], auth.get("bot_id")
    allowed = set(cfg.channel_ids)

    def channel_ok(channel: str, channel_type: str = "") -> bool:
        return channel_type == "im" or not allowed or channel in allowed

    @app.event("message")
    def on_message(event, client):
        subtype = event.get("subtype")
        channel = event.get("channel", "")
        channel_type = event.get("channel_type", "")
        if not channel_ok(channel, channel_type):
            return

        if subtype == "message_changed":
            # Link previews (unfurls) arrive as an edit a moment after the post; bank their picture then.
            msg = event.get("message") or {}
            if msg.get("bot_id") or not msg.get("attachments") or not images_in(msg, channel):
                return
            trig = classify(msg.get("text", ""), True, bot_user_id, is_dm=channel_type == "im")
            if trig and trig.kind == "bank":
                bank.deposit_message(client, channel, msg)
            elif trig and trig.kind == "confirm":
                bank.ask_to_bank(client, channel, msg)
            return

        if subtype in IGNORED_SUBTYPES or event.get("bot_id") or event.get("user") == bot_user_id:
            return

        text = event.get("text", "")
        thread_ts = event.get("thread_ts")
        in_thread = bool(thread_ts) and thread_ts != event.get("ts")
        if channel_type == "im" and not images_in(event, channel) and answer_confirm(client, event, in_thread):
            return
        trig = classify(text, bool(images_in(event, channel)), bot_user_id, is_dm=channel_type == "im")
        if trig is None:
            return
        if trig.kind == "bank":
            bank.deposit_message(client, channel, event)
            return
        if trig.kind == "confirm":
            bank.ask_to_bank(client, channel, event)
            return
        rest = trig.rest
        user = event.get("user", "")
        cmd = parse_command(rest)
        about = None  # the meme "it"/"this" means
        if channel_type == "im" and in_thread and cmd.name == "search":
            # Plain words replied under the bot's "Banked #12" DM are tags for #12, not a search.
            number = banked_number_in_thread(client, channel, thread_ts)
            if number:
                rest = f"tag #{number} {rest}"
        elif channel_type == "im" and cmd.name in NUMBERED and cmd.number is None:
            # "Tag it ..." straight into the DM: the meme is the one the bot last said it banked here. A bare "#mondays"
            # sent straight into the DM is a search, though, as it always was.
            if in_thread:
                about = banked_number_in_thread(client, channel, thread_ts)
            elif not cmd.or_search:
                about = latest_banked_in_dm(client, channel)
        elif in_thread and cmd.name in NUMBERED and cmd.number is None:
            # "@Meme Bank tag it too real" replied under a meme in the channel
            numbers, unbanked = meme_in_thread(client, channel, thread_ts, event["ts"])
            if unbanked is not None and (cmd.name == "tag" or not numbers):  # (else: what's banked is what "it" can be)
                if cmd.name != "tag":
                    bank.say(client, channel, "That one isn't in the vault yet. React :bank: on it to bank it.", thread_ts)
                    return
                # Anyone can bank a post with a :bank: reaction, so asking to tag it banks it the same way.
                # deposit_message replies with how that went, so a failure needs nothing more here.
                numbers = list(dict.fromkeys(r.meme.number for r in
                                             bank.deposit_message(client, channel, unbanked, banked_by=user)))
                if not numbers:
                    return
            if len(numbers) > 1:
                which = " or ".join(f"#{n}" for n in numbers)
                bank.say(client, channel, f"Which one, {which}? Say `hey meme bank, {cmd.name} #{numbers[0]} …`",
                         thread_ts)
                return
            about = numbers[0] if numbers else None
        bank.handle_ask(client, channel, user, rest, thread_ts=thread_ts, about=about)

    def answer_confirm(client, event, in_thread: bool) -> bool:
        """An answer to a "want me to bank it?" DM: a reply in its thread, or a yes/no sent straight into the DM
        right after that question. True if it was one (and has been dealt with)."""
        channel, text = event["channel"], event.get("text", "")
        plain = text.replace("’", "'").replace("‘", "'")
        no = NO_RE.search(plain) or NEGATION_RE.search(plain)  # "okay, I wouldn't bank it" is a no too
        yes = not no and YES_RE.match(plain)
        if not (yes or no) or (not in_thread and not set(re.findall(r"[\w']+", plain.lower())) <= ANSWER_WORDS):
            return False
        try:
            if in_thread:
                msgs = client.conversations_replies(channel=channel, ts=event["thread_ts"], limit=1,
                                                    include_all_metadata=True).get("messages", [])
            else:  # the message just before this answer (not a newer question that arrived since)
                msgs = client.conversations_history(channel=channel, latest=event["ts"], inclusive=False, limit=1,
                                                    include_all_metadata=True).get("messages", [])
        except Exception:  # noqa: BLE001
            log.exception("Couldn't read the DM")
            return False
        root = msgs[0] if msgs else {}
        thread_ts = event["thread_ts"] if in_thread else None  # answer where they answered
        if root.get("user") != bot_user_id:
            return False  # only trust the question if the bot itself asked it
        meta = root.get("metadata") or {}
        if meta.get("event_type") != CONFIRM_EVENT:
            return False
        where = meta.get("event_payload") or {}
        if no:  # but someone may have banked it since (a :bank: reaction), so don't claim it's left out if it isn't
            post = fetch_message(client, where.get("channel", ""), where.get("ts", ""), where.get("thread_ts"))
            refs = images_in(post, where["channel"]) if post else []
            found = [m for m in (bank.store.find_by_source(r.key) for r in refs) if m and not m.withdrawn_at]
            if found:
                n = found[0].number
                bank.say(client, channel, f"OK, I won't bank it. It's already in the vault as #{n}, though; only a bank "
                                          f"manager can remove it (`hey meme bank, forget #{n}` asks one).", thread_ts)
            else:  # about this post only: a resized copy banked another way is a different source (#42)
                bank.say(client, channel, "OK, I won't bank this post.", thread_ts)
            return True
        post = fetch_message(client, where.get("channel", ""), where.get("ts", ""), where.get("thread_ts"))
        if not post:
            bank.say(client, channel, "I couldn't find that post any more, so there's nothing to bank.", thread_ts)
            return True
        results = bank.deposit_message(client, where["channel"], post, banked_by=event.get("user", ""))
        done = [r for r in results if r.status in ("new", "duplicate", "already")]
        bank.say(client, channel, (f"Done: it's #{done[0].meme.number} in the vault." if done
                                   else "I couldn't bank that one (see the post's thread)."), thread_ts)
        return True

    def fetch_message(client, channel: str, ts: str, thread_ts: str | None = None) -> dict | None:
        """One message by channel and ts, whether it's a top-level post or a reply in a thread (pass the thread's ts
        when it's known; Slack also finds a reply from its own ts, which is what a reaction gives us)."""
        if not (channel and ts):
            return None
        try:
            msgs = []
            if not thread_ts:
                resp = client.conversations_history(channel=channel, latest=ts, oldest=ts, inclusive=True, limit=1)
                msgs = [m for m in resp.get("messages", []) if m.get("ts") == ts]
            if not msgs:  # history only has top-level posts; a reply in a thread has to be looked up as one
                resp = client.conversations_replies(channel=channel, ts=thread_ts or ts, latest=ts, oldest=ts,
                                                    inclusive=True, limit=10)
                msgs = [m for m in resp.get("messages", []) if m.get("ts") == ts]
        except Exception:  # noqa: BLE001
            log.exception("Couldn't load message %s/%s", channel, ts)
            return None
        return msgs[0] if msgs and not msgs[0].get("bot_id") else None

    def banked_numbers(msgs: list[dict]) -> list[int]:
        return [int(m.group(1)) for msg in msgs if msg.get("user") == bot_user_id or msg.get("bot_id")
                for m in [BANKED_RE.search(msg.get("text", ""))] if m]

    def banked_number_in_thread(client, channel: str, thread_ts: str) -> int | None:
        try:
            msgs = client.conversations_replies(channel=channel, ts=thread_ts, limit=50).get("messages", [])
        except Exception:  # noqa: BLE001
            log.exception("Couldn't read the DM thread")
            return None
        found = banked_numbers(msgs)  # oldest first
        return found[-1] if found else None

    def latest_banked_in_dm(client, channel: str) -> int | None:
        """The newest "Banked #N" the bot posted in this DM: top-level (after a channel deposit) or in the thread of
        an image sent straight to the bot (a DM deposit), which the DM's history alone doesn't include."""
        try:
            msgs = client.conversations_history(channel=channel, limit=20).get("messages", [])
            threads = [m for m in msgs if m.get("reply_count") and not m.get("bot_id")][:5]
            for t in threads:
                msgs += client.conversations_replies(channel=channel, ts=t["ts"], limit=20).get("messages", [])
        except Exception:  # noqa: BLE001
            log.exception("Couldn't read the DM")
            return None
        banked = [(float(msg["ts"]), n) for msg in msgs for n in banked_numbers([msg])]
        return max(banked)[1] if banked else None

    def meme_in_thread(client, channel: str, thread_ts: str, before: str) -> tuple[list[int], dict | None]:
        """The meme a channel thread is about: the nearest message above `before` that's an image post (the thread's
        own, or a reply) or the bot naming a meme ("Banked as *#12: ...", a meme it posted, "Tagged *#12: ...").
        Returns (the banked numbers, the post if any of its images isn't banked yet); ([], None) if there's none."""
        try:
            msgs, cursor = [], None
            for _ in range(MAX_THREAD_PAGES):  # replies come oldest first, so the nearest ones can be pages in
                resp = client.conversations_replies(channel=channel, ts=thread_ts, latest=before, inclusive=False,
                                                    limit=200, cursor=cursor)
                msgs += resp.get("messages", [])
                cursor = (resp.get("response_metadata") or {}).get("next_cursor")
                if not cursor:
                    break
            for msg in sorted(msgs, key=lambda m: float(m.get("ts", 0)), reverse=True):  # nearest first
                if float(msg.get("ts", 0)) >= float(before):
                    continue
                if msg.get("user") == bot_user_id or (bot_id and msg.get("bot_id") == bot_id):
                    numbers = [int(n) for n in MEME_REF_RE.findall(msg.get("text", ""))]
                    if numbers:
                        return list(dict.fromkeys(numbers)), None
                elif not msg.get("bot_id") and images_in(msg, channel):
                    found = [bank.store.find_by_source(r.key) for r in images_in(msg, channel)]
                    numbers = list(dict.fromkeys(m.number for m in found if m))
                    return numbers, (None if all(found) else msg)
        except Exception:  # noqa: BLE001
            log.exception("Couldn't work out the meme in thread %s/%s", channel, thread_ts)
        return [], None

    @app.event("reaction_added")
    def on_reaction(event, client):
        if event.get("reaction") not in cfg.bank_reactions or event.get("user") == bot_user_id:
            return
        item = event.get("item") or {}
        channel, ts = item.get("channel", ""), item.get("ts", "")
        if item.get("type") != "message" or not channel_ok(channel):
            return
        msg = fetch_message(client, channel, ts)
        if msg:
            bank.deposit_message(client, channel, msg, banked_by=event.get("user", ""))

    @app.event("app_mention")
    def _ignore_mentions():  # mentions are handled as ordinary messages above
        pass

    return app
