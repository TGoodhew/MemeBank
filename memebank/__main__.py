"""Run the bot:  python -m memebank"""
import logging

from slack_bolt.adapter.socket_mode import SocketModeHandler

from .config import Config, load_env, make_store
from .core import MemeBank
from .slack_app import build_app
from .tagger import ClaudeTagger


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_env()
    cfg = Config.from_env()
    missing = [n for n, v in [("SLACK_BOT_TOKEN", cfg.slack_bot_token), ("SLACK_APP_TOKEN", cfg.slack_app_token),
                              ("ANTHROPIC_API_KEY", cfg.anthropic_api_key)] if not v]
    if missing:
        raise SystemExit("Missing settings: " + ", ".join(missing))
    store = make_store(cfg)
    tagger = ClaudeTagger(cfg.anthropic_api_key, cfg.tag_model, cfg.search_model)
    bank = MemeBank(store, tagger, cfg.slack_bot_token, cfg.admin_user_ids, cfg.ack_mode)
    app = build_app(cfg, bank)
    logging.getLogger("memebank").info("Meme Bank open for business (%s storage, %d memes)",
                                       cfg.storage_backend, len(store.all()))
    SocketModeHandler(app, cfg.slack_app_token).start()


if __name__ == "__main__":
    main()
