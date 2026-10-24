"""The heartbeat line the "gone quiet" alert looks for."""
from types import SimpleNamespace

from memebank.__main__ import heartbeat_line
from memebank.store import LocalStore, Meme


def handler(connected: bool):
    return SimpleNamespace(client=SimpleNamespace(is_connected=lambda: connected))


def test_heartbeat_line_says_whether_slack_is_connected(tmp_path):
    store = LocalStore(str(tmp_path))
    store.put(Meme(1, "slack:F1", "A", "d", "", blob_name="000001.png"), b"x")
    # deploy.ps1's alert query matches this exact text
    assert heartbeat_line(handler(True), store) == "Heartbeat: connected=True, 1 memes"
    assert "connected=True" not in heartbeat_line(handler(False), store)
