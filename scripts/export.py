"""Copy the whole bank to a folder on your computer: the catalogue as JSON plus every image.

    python -m scripts.export --env-file .env.azure --out memebank-backup

Withdrawn ("forgotten") memes are included and marked, so the folder is a complete backup. Images already in
the folder are skipped, so re-running it only downloads what's new.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import asdict

from memebank.config import Config, load_env, make_store


def export(store, out: str) -> dict:
    os.makedirs(os.path.join(out, "images"), exist_ok=True)
    memes = store.all(include_withdrawn=True)
    counts = {"memes": len(memes), "withdrawn": sum(1 for m in memes if m.withdrawn_at), "downloaded": 0, "kept": 0}
    for m in memes:
        path = os.path.join(out, "images", m.blob_name)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            counts["kept"] += 1
            continue
        data = store.get_image(m)
        with open(path + ".part", "wb") as f:  # renamed only once complete, so a failed run can't leave a stub
            f.write(data)
        os.replace(path + ".part", path)
        counts["downloaded"] += 1
    with open(os.path.join(out, "memes.json"), "w", encoding="utf-8") as f:
        json.dump([asdict(m) for m in memes], f, ensure_ascii=False, indent=2)
    return counts


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="memebank-backup", help="folder to write to (default memebank-backup)")
    p.add_argument("--env-file", default=None, help="settings file (default .env); the deploy script writes .env.azure")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("azure").setLevel(logging.WARNING)  # the storage SDK logs every HTTP request at INFO

    load_env(args.env_file)
    c = export(make_store(Config.from_env()), args.out)
    print(f"{c['memes']} memes ({c['withdrawn']} withdrawn) -> {os.path.abspath(args.out)}: "
          f"{c['downloaded']} images downloaded, {c['kept']} already there")


if __name__ == "__main__":
    main()
