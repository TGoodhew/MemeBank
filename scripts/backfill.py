"""Bank the meme-bank posts already in a channel.

    python -m scripts.backfill --env-file .env.azure --channel C6UTZF23Z [--since 2026-07-01] [--dry-run] [--ack]

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
from memebank.triggers import BANK_RE


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--channel", required=True, help="channel ID, e.g. C6UTZF23Z")
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
    oldest = "0"
    if args.since:
        oldest = str(datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())

    messages, cursor = [], None
    while True:
        resp = client.conversations_history(channel=args.channel, oldest=oldest, limit=200, cursor=cursor)
        messages += resp["messages"]
        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    candidates = [m for m in reversed(messages)  # oldest first, so numbers follow posting order
                  if not m.get("bot_id") and BANK_RE.search(m.get("text", "")) and images_in(m, args.channel)]
    print(f"{len(messages)} messages read, {len(candidates)} meme-bank posts with images")

    if args.dry_run:
        for m in candidates:
            when = datetime.fromtimestamp(float(m["ts"]), timezone.utc).strftime("%Y-%m-%d")
            print(f"  {when}  {m.get('text', '')[:70]}")
        return

    bank = MemeBank(make_store(cfg), ClaudeTagger(cfg.anthropic_api_key, cfg.tag_model, cfg.search_model),
                    cfg.slack_bot_token, cfg.admin_user_ids, ack_mode=cfg.ack_mode)
    counts = {"new": 0, "already": 0, "duplicate": 0, "withdrawn": 0}
    for m in candidates:
        for d in bank.deposit_message(client, args.channel, m, acknowledge=args.ack):
            counts[d.status] += 1
            print(f"  {d.status:9} #{d.meme.number} {d.meme.title}")
    print(f"Done: {counts['new']} banked, {counts['already']} already in, {counts['duplicate']} duplicates skipped, "
          f"{counts['withdrawn']} withdrawn (left out)")


if __name__ == "__main__":
    main()
