"""Run the bot:  python -m memebank"""
import logging
import threading
import time

from slack_bolt.adapter.socket_mode import SocketModeHandler

from .config import Config, load_env, make_store
from .core import MemeBank
from .slack_app import build_app
from .tagger import ClaudeTagger

log = logging.getLogger("memebank")

# The "gone quiet" alert (deploy.ps1) fires when no "connected=True" heartbeat arrives for 2 hours.
HEARTBEAT_SECONDS = 3600


def heartbeat_line(handler, store) -> str:
    return f"Heartbeat: connected={handler.client.is_connected()}, {len(store.all())} memes"


def heartbeat(handler, store, first_after: float = 60, every: float = HEARTBEAT_SECONDS) -> None:
    time.sleep(first_after)  # give Socket Mode a moment to connect
    while True:
        try:
            log.info(heartbeat_line(handler, store))
        except Exception:  # noqa: BLE001
            log.exception("Heartbeat failed")
        time.sleep(every)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("azure").setLevel(logging.WARNING)  # the storage SDK logs every HTTP request at INFO
    load_env()
    cfg = Config.from_env()
    missing = [n for n, v in [("SLACK_BOT_TOKEN", cfg.slack_bot_token), ("SLACK_APP_TOKEN", cfg.slack_app_token),
                              ("ANTHROPIC_API_KEY", cfg.anthropic_api_key)] if not v]
    if missing:
        raise SystemExit("Missing settings: " + ", ".join(missing))
    store = make_store(cfg)
    tagger = ClaudeTagger(cfg.anthropic_api_key, cfg.tag_model, cfg.search_model)
    bank = MemeBank(store, tagger, cfg.slack_bot_token, cfg.admin_user_ids, cfg.ack_mode, dm_tags=cfg.dm_tags)
    app = build_app(cfg, bank)
    log.info("Meme Bank open for business (%s storage, %d memes)", cfg.storage_backend, len(store.all()))
    handler = SocketModeHandler(app, cfg.slack_app_token)
    threading.Thread(target=heartbeat, args=(handler, store), daemon=True, name="heartbeat").start()
    handler.start()


if __name__ == "__main__":
    main()
