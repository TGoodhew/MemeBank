"""Reading settings from the environment and from .env files."""
import os

import pytest

from memebank.config import Config, load_env, make_store

KEYS = ["SLACK_BOT_TOKEN", "STORAGE_BACKEND", "ACK_MODE", "MEME_CHANNEL_IDS", "BANK_REACTIONS", "ADMIN_USER_IDS",
        "DM_TAGS", "AZURE_STORAGE_CONNECTION_STRING", "AZURE_STORAGE_ACCOUNT", "TAG_MODEL"]


@pytest.fixture
def clean_env(monkeypatch):
    for k in KEYS:
        monkeypatch.setenv(k, "")  # registered with monkeypatch, so anything load_env sets is undone afterwards
        monkeypatch.delenv(k)


def test_lists_are_split_and_trimmed_and_words_lower_cased(clean_env, monkeypatch):
    monkeypatch.setenv("MEME_CHANNEL_IDS", " C6UTZF23Z, CTWO ,,")
    monkeypatch.setenv("STORAGE_BACKEND", "Azure")
    monkeypatch.setenv("ACK_MODE", "Reaction")
    cfg = Config.from_env()
    assert cfg.channel_ids == ["C6UTZF23Z", "CTWO"] and cfg.storage_backend == "azure" and cfg.ack_mode == "reaction"


def test_defaults(clean_env):
    cfg = Config.from_env()
    assert (cfg.bank_reactions, cfg.channel_ids, cfg.ack_mode, cfg.storage_backend, cfg.dm_tags, cfg.tag_model) == \
        (["bank"], [], "thread", "local", True, "claude-sonnet-5-5")


@pytest.mark.parametrize("value,on", [("on", True), ("", True), ("OFF", False), ("false", False), ("0", False), ("no", False)])
def test_dm_tags_switch(clean_env, monkeypatch, value, on):
    monkeypatch.setenv("DM_TAGS", value)
    assert Config.from_env().dm_tags is on


def test_a_notepad_bom_doesnt_hide_the_first_setting(clean_env, tmp_path):
    env = tmp_path / ".env"
    env.write_bytes("SLACK_BOT_TOKEN=xoxb-from-file\nACK_MODE=reaction\n".encode("utf-8-sig"))  # starts with a BOM
    load_env(str(env))
    assert os.environ["SLACK_BOT_TOKEN"] == "xoxb-from-file" and Config.from_env().ack_mode == "reaction"


def test_an_env_file_given_by_path_overrides_the_environment(clean_env, monkeypatch, tmp_path):
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    env = tmp_path / ".env.azure"
    env.write_text("STORAGE_BACKEND=azure\n")
    load_env(str(env))
    assert Config.from_env().storage_backend == "azure"  # what the backfill's --env-file relies on


def test_azure_storage_with_neither_an_account_nor_a_connection_string_says_so(clean_env):
    with pytest.raises(SystemExit, match="AZURE_STORAGE_ACCOUNT"):
        make_store(Config(storage_backend="azure"))


def test_an_account_name_means_signing_in_by_identity_not_by_key(clean_env, monkeypatch):
    made = []
    monkeypatch.setattr("memebank.store.AzureStore", lambda *a, **kw: made.append((a, kw)) or "store")
    monkeypatch.setenv("STORAGE_BACKEND", "azure")
    monkeypatch.setenv("AZURE_STORAGE_ACCOUNT", "memebankgn9hc64r")
    monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", "UseDevelopmentStorage=true")  # the account wins
    assert make_store(Config.from_env()) == "store"
    assert made == [((), {"table_name": "memes", "container": "memes", "account": "memebankgn9hc64r"})]


def test_the_store_builds_its_clients_from_the_account_with_the_credential(monkeypatch):
    import azure.data.tables
    import azure.storage.blob

    from memebank.store import AzureStore

    seen = {}

    class Tables:
        def __init__(self, endpoint, credential):
            seen["tables"] = (endpoint, credential)

        def create_table_if_not_exists(self, name):
            return name

    class Blobs:
        def __init__(self, account_url, credential):
            seen["blobs"] = (account_url, credential)

        def get_container_client(self, name):
            return type("Container", (), {"create_container": lambda self: None})()

    monkeypatch.setattr(azure.data.tables, "TableServiceClient", Tables)
    monkeypatch.setattr(azure.storage.blob, "BlobServiceClient", Blobs)
    store = AzureStore(account="memebankgn9hc64r", credential="cred")
    assert seen == {"tables": ("https://memebankgn9hc64r.table.core.windows.net", "cred"),
                    "blobs": ("https://memebankgn9hc64r.blob.core.windows.net", "cred")}
    assert store.table == "memes"
