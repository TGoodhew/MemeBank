"""Slack wiring (Bolt, Socket Mode: the bot dials out to Slack, so it needs no public web address)."""
from __future__ import annotations

import logging
import re

from slack_bolt import App

from .core import MemeBank, images_in
from .triggers import BANK_RE, classify, parse_command

log = logging.getLogger(__name__)

BANKED_RE = re.compile(r"Banked (?:as )?\*#(\d+)")  # the bot's own "Banked as *#12: ..." messages

IGNORED_SUBTYPES = {"bot_message", "message_deleted", "channel_join", "channel_leave", "channel_topic",
                    "channel_purpose", "channel_name", "pinned_item", "unpinned_item", "tombstone"}


def build_app(cfg, bank: MemeBank) -> App:
    app = App(token=cfg.slack_bot_token)
    bot_user_id = app.client.auth_test()["user_id"]
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
            if msg.get("bot_id") or not msg.get("attachments"):
                return
            if BANK_RE.search(msg.get("text", "")) and images_in(msg, channel):
                bank.deposit_message(client, channel, msg)
            return

        if subtype in IGNORED_SUBTYPES or event.get("bot_id") or event.get("user") == bot_user_id:
            return

        text = event.get("text", "")
        trig = classify(text, bool(images_in(event, channel)), bot_user_id, is_dm=channel_type == "im")
        if trig is None:
            return
        thread_ts = event.get("thread_ts")
        if trig.kind == "bank":
            bank.deposit_message(client, channel, event)
            return
        rest = trig.rest
        cmd = parse_command(rest)
        in_thread = bool(thread_ts) and thread_ts != event.get("ts")
        if channel_type == "im" and in_thread and cmd.name == "search":
            # Plain words replied under the bot's "Banked #12" DM are tags for #12, not a search.
            number = banked_number_in_thread(client, channel, thread_ts)
            if number:
                rest = f"tag #{number} {rest}"
        elif channel_type == "im" and cmd.name == "tag" and cmd.number is None:
            # "Tag it ..." straight into the DM: the meme is the one the bot last said it banked here.
            number = banked_number_in_thread(client, channel, thread_ts) if in_thread else latest_banked_in_dm(client, channel)
            if number:
                rest = f"tag #{number} {cmd.query}"
        bank.handle_ask(client, channel, event.get("user", ""), rest, thread_ts=thread_ts)

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
        try:
            msgs = client.conversations_history(channel=channel, limit=20).get("messages", [])
        except Exception:  # noqa: BLE001
            log.exception("Couldn't read the DM")
            return None
        found = banked_numbers(msgs)  # newest first
        return found[0] if found else None

    @app.event("reaction_added")
    def on_reaction(event, client):
        if event.get("reaction") not in cfg.bank_reactions or event.get("user") == bot_user_id:
            return
        item = event.get("item") or {}
        channel, ts = item.get("channel", ""), item.get("ts", "")
        if item.get("type") != "message" or not channel_ok(channel):
            return
        try:
            resp = client.conversations_history(channel=channel, latest=ts, oldest=ts, inclusive=True, limit=1)
            msgs = [m for m in resp.get("messages", []) if m.get("ts") == ts]
            if not msgs:  # history only has top-level posts; a reply in a thread has to be looked up as one
                resp = client.conversations_replies(channel=channel, ts=ts, latest=ts, oldest=ts, inclusive=True, limit=10)
                msgs = [m for m in resp.get("messages", []) if m.get("ts") == ts]
        except Exception:  # noqa: BLE001
            log.exception("Couldn't load the reacted-to message")
            return
        if msgs and not msgs[0].get("bot_id"):
            bank.deposit_message(client, channel, msgs[0], banked_by=event.get("user", ""))

    @app.event("app_mention")
    def _ignore_mentions():  # mentions are handled as ordinary messages above
        pass

    return app
