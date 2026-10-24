"""Slack wiring (Bolt, Socket Mode: the bot dials out to Slack, so it needs no public web address)."""
from __future__ import annotations

import logging

from slack_bolt import App

from .core import MemeBank, images_in
from .triggers import BANK_RE, classify

log = logging.getLogger(__name__)

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
        if trig.kind == "bank":
            bank.deposit_message(client, channel, event)
        else:
            bank.handle_ask(client, channel, event.get("user", ""), trig.rest, thread_ts=event.get("thread_ts"))

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
        except Exception:  # noqa: BLE001
            log.exception("Couldn't load the reacted-to message")
            return
        if msgs and not msgs[0].get("bot_id"):
            bank.deposit_message(client, channel, msgs[0])

    @app.event("app_mention")
    def _ignore_mentions():  # mentions are handled as ordinary messages above
        pass

    return app
