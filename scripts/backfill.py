"""Bank the meme-bank posts already in a channel.

    python -m scripts.backfill --env-file .env.azure --channel C0123456789 [--since 2026-07-01] [--dry-run] [--ack]

Uses the same settings as the bot (.env, or the file given with --env-file). The bot must already be in the channel.
Only messages Slack still shows can be read (90 days on the free plan).
"""
from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone

from slack_sdk import WebClient

from memebank.config import Config, load_env, make_store
from memebank.core import MemeBank, images_in
from memebank.tagger import ClaudeTagger
from memebank.triggers import BANK_RE, says_deposit


def since_to_oldest(since: str) -> str:
    """--since YYYY-MM-DD (UTC midnight) as the Slack "oldest" timestamp; "0" means everything."""
    return str(datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()) if since else "0"


def read_history(client, channel: str, oldest: str = "0") -> list[dict]:
    messages, cursor = [], None
    while True:
        resp = client.conversations_history(channel=channel, oldest=oldest, limit=200, cursor=cursor)
        messages += resp["messages"]
        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return messages


def candidates(messages: list[dict], channel: str, wanted=says_deposit) -> list[dict]:
    """People's posts with an image that say they're for the meme bank, oldest first so numbers follow posting
    order. Same rule as the bot: "a meme for the meme bank", "store this in the meme bank", :bank:."""
    return [m for m in reversed(messages)  # Slack returns newest first
            if not m.get("bot_id") and wanted(m.get("text", "")) and images_in(m, channel)]


def mentions_only(messages: list[dict], channel: str) -> list[dict]:
    """Image posts that only *mention* the meme bank. Not banked; listed so they can be banked by :bank: reaction."""
    mentioned = candidates(messages, channel, lambda text: bool(BANK_RE.search(text)))
    return [m for m in mentioned if not says_deposit(m.get("text", ""))]


def bank_all(bank, client, channel: str, posts: list[dict], ack: bool = False) -> dict:
    counts = {"new": 0, "already": 0, "duplicate": 0, "withdrawn": 0}
    for m in posts:
        for d in bank.deposit_message(client, channel, m, acknowledge=ack):
            counts[d.status] += 1
            print(f"  {d.status:9} #{d.meme.number} {d.meme.title}")
    return counts


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--channel", required=True, help="channel ID, e.g. C0123456789")
    p.add_argument("--since", default="", help="YYYY-MM-DD; default: everything Slack still shows")
    p.add_argument("--dry-run", action="store_true", help="list what would be banked, change nothing")
    p.add_argument("--ack", action="store_true", help="react/reply on the old posts (off by default, to keep the channel quiet)")
    p.add_argument("--env-file", default=None, help="settings file (default .env); the deploy script writes .env.azure")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("azure").setLevel(logging.WARNING)  # the storage SDK logs every HTTP request at INFO

    load_env(args.env_file)
    cfg = Config.from_env()
    client = WebClient(token=cfg.slack_bot_token)
    messages = read_history(client, args.channel, since_to_oldest(args.since))
    posts = candidates(messages, args.channel)
    maybe = mentions_only(messages, args.channel)
    print(f"{len(messages)} messages read, {len(posts)} meme-bank posts with images")

    def listing(msgs):
        for m in msgs:
            when = datetime.fromtimestamp(float(m["ts"]), timezone.utc).strftime("%Y-%m-%d")
            print(f"  {when}  {m.get('text', '')[:70]}")

    if maybe:
        print(f"{len(maybe)} image posts only mention the meme bank, so they're left out (react :bank: to bank one):")
        listing(maybe)
    if args.dry_run:
        print("Would bank:")
        listing(posts)
        return

    bank = MemeBank(make_store(cfg), ClaudeTagger(cfg.anthropic_api_key, cfg.tag_model, cfg.search_model),
                    cfg.slack_bot_token, cfg.admin_user_ids, ack_mode=cfg.ack_mode)
    counts = bank_all(bank, client, args.channel, posts, ack=args.ack)
    print(f"Done: {counts['new']} banked, {counts['already']} already in, {counts['duplicate']} duplicates skipped, "
          f"{counts['withdrawn']} withdrawn (left out)")


if __name__ == "__main__":
    main()
