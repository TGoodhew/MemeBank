"""Bank image files from your computer (e.g. memes that have aged out of Slack's history).

    python -m scripts.import_local --env-file .env.azure "$env:USERPROFILE\\Downloads\\image.png" --date 2026-07-27 --caption "This meme needs to be added again to the meme bank"

Several files can be given at once; --date/--caption then apply to all of them.
"""
from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone

from memebank.config import Config, load_env, make_store
from memebank.core import MemeBank, local_source_key
from memebank.tagger import ClaudeTagger


def date_to_posted_at(date: str) -> str:
    """--date YYYY-MM-DD as the ISO timestamp stored in posted_at (UTC midnight); "" if not given."""
    return datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc).isoformat() if date else ""


def import_files(bank, paths: list[str], caption: str = "", posted_by: str = "", posted_at: str = "") -> list:
    results = []
    for path in paths:
        with open(path, "rb") as f:
            data = f.read()
        results.append((path, bank.deposit_bytes(data, local_source_key(data), caption=caption,
                                                 posted_by=posted_by, posted_at=posted_at)))
    return results


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("files", nargs="+")
    p.add_argument("--caption", default="", help="what was said when it was posted (helps tagging)")
    p.add_argument("--posted-by", default="", help="Slack user ID (U...) or a name")
    p.add_argument("--date", default="", help="YYYY-MM-DD it was originally posted")
    p.add_argument("--env-file", default=None, help="settings file (default .env); the deploy script writes .env.azure")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("azure").setLevel(logging.WARNING)  # the storage SDK logs every HTTP request at INFO

    load_env(args.env_file)
    cfg = Config.from_env()
    bank = MemeBank(make_store(cfg), ClaudeTagger(cfg.anthropic_api_key, cfg.tag_model, cfg.search_model))
    for path, d in import_files(bank, args.files, args.caption, args.posted_by, date_to_posted_at(args.date)):
        print(f"{d.status:9} #{d.meme.number} {d.meme.title}  <- {path}")
        if d.status == "new":
            print("          tags: " + ", ".join(d.meme.tags))


if __name__ == "__main__":
    main()
