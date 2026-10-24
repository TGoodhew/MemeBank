"""Settings, read from environment variables (or a .env file when running locally)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

def load_env(path: str | None = None) -> None:
    """Read settings from a .env file for local runs; in Azure they come from the container app."""
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover
        return
    load_dotenv(path or ".env", override=bool(path), encoding="utf-8-sig")  # tolerate a Notepad BOM


def _list(name: str, default: str = "") -> list[str]:
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


@dataclass
class Config:
    slack_bot_token: str = ""
    slack_app_token: str = ""
    anthropic_api_key: str = ""
    tag_model: str = "claude-sonnet-5-5"
    search_model: str = "claude-sonnet-5-5"
    storage_backend: str = "local"  # "azure" or "local"
    azure_connection_string: str = ""  # Azurite and tests; Azure itself signs in by identity (azure_storage_account)
    azure_storage_account: str = ""
    blob_container: str = "memes"
    table_name: str = "memes"
    local_dir: str = "./memebank-data"
    # Channels where posts get banked and questions get answered. Empty = any channel the bot is in.
    channel_ids: list[str] = field(default_factory=list)
    # Reacting with one of these emoji to an image post banks it, whatever the text says.
    bank_reactions: list[str] = field(default_factory=lambda: ["bank"])
    # Users allowed to "forget" any meme (everyone can forget memes they posted).
    admin_user_ids: list[str] = field(default_factory=list)
    # "thread" = react + short thread reply with the tags; "reaction" = react only.
    ack_mode: str = "thread"
    # DM whoever banked a meme its full tag list and how to add more ("off" to stop).
    dm_tags: bool = True

    @classmethod
    def from_env(cls) -> "Config":
        e = os.environ.get
        return cls(
            slack_bot_token=e("SLACK_BOT_TOKEN", ""),
            slack_app_token=e("SLACK_APP_TOKEN", ""),
            anthropic_api_key=e("ANTHROPIC_API_KEY", ""),
            tag_model=e("TAG_MODEL", cls.tag_model),
            search_model=e("SEARCH_MODEL", cls.search_model),
            storage_backend=e("STORAGE_BACKEND", "local").lower(),
            azure_connection_string=e("AZURE_STORAGE_CONNECTION_STRING", ""),
            azure_storage_account=e("AZURE_STORAGE_ACCOUNT", ""),
            blob_container=e("BLOB_CONTAINER", "memes"),
            table_name=e("TABLE_NAME", "memes"),
            local_dir=e("LOCAL_DATA_DIR", "./memebank-data"),
            channel_ids=_list("MEME_CHANNEL_IDS"),
            bank_reactions=_list("BANK_REACTIONS", "bank"),
            admin_user_ids=_list("ADMIN_USER_IDS"),
            ack_mode=e("ACK_MODE", "thread").lower(),
            dm_tags=e("DM_TAGS", "on").strip().lower() not in ("off", "false", "0", "no"),
        )


def make_store(cfg: Config):
    from .store import AzureStore, LocalStore

    if cfg.storage_backend == "azure":
        if cfg.azure_storage_account:  # sign in as the managed identity (in Azure) or your az login (on a PC)
            return AzureStore(table_name=cfg.table_name, container=cfg.blob_container, account=cfg.azure_storage_account)
        if not cfg.azure_connection_string:
            raise SystemExit("STORAGE_BACKEND=azure needs AZURE_STORAGE_ACCOUNT (or AZURE_STORAGE_CONNECTION_STRING)")
        return AzureStore(cfg.azure_connection_string, cfg.table_name, cfg.blob_container)
    return LocalStore(cfg.local_dir)
