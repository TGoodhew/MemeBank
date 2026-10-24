"""The backfill and import scripts, with Slack and Claude faked out."""
import sys
from datetime import datetime, timezone

import pytest

from memebank.core import MemeBank
from memebank.store import LocalStore
from scripts import backfill, import_local
from test_memebank import FakeClient, FakeTagger, png, slack_msg


class HistoryClient(FakeClient):
    """conversations_history in pages of two, newest first, like Slack."""

    def __init__(self, messages):
        super().__init__()
        self.messages = messages  # newest first

    def conversations_history(self, channel, oldest="0", limit=200, cursor=None):
        self.calls.append(("conversations_history", {"channel": channel, "oldest": oldest, "cursor": cursor}))
        start = int(cursor or 0)
        page = self.messages[start:start + 2]
        more = start + 2 < len(self.messages)
        return {"messages": page, "response_metadata": {"next_cursor": str(start + 2) if more else ""}}


@pytest.fixture
def bank(tmp_path):
    blobs = {"F1": png(1), "F2": png(2), "F3": png(3), "F1COPY": png(1, (800, 600))}
    return MemeBank(LocalStore(str(tmp_path)), FakeTagger(), "xoxb-test",
                    fetch=lambda url, token: blobs[url.rsplit("/", 1)[1]])


# ---------------------------------------------------------------- backfill
def test_read_history_follows_every_page():
    msgs = [{"ts": f"{t}.0"} for t in range(5, 0, -1)]
    client = HistoryClient(msgs)
    assert backfill.read_history(client, "CGEN", "123.0") == msgs
    calls = client.named("conversations_history")
    assert [c["cursor"] for c in calls] == [None, "2", "4"] and {c["oldest"] for c in calls} == {"123.0"}


def test_candidates_are_peoples_meme_bank_posts_with_images_oldest_first():
    newest_first = [
        slack_msg("5.0", "a meme for the meme bank", "F3"),
        {**slack_msg("4.0", "meme bank", "F2"), "bot_id": "B1"},     # the bot's own post
        slack_msg("3.0", "just a picture", "F2"),                     # no "meme bank"
        slack_msg("2.0", "the meme bank is great"),                   # no image
        slack_msg("1.5", "This needs to be added again to the meme bank", "F1"),
        slack_msg("1.0", "Did I post this as a meme bank image", "F1"),  # only mentions it
    ]
    assert [m["ts"] for m in backfill.candidates(newest_first, "CGEN")] == ["1.5", "5.0"]
    assert [m["ts"] for m in backfill.mentions_only(newest_first, "CGEN")] == ["1.0"]  # listed, not banked


def test_since_is_utc_midnight_and_blank_means_everything():
    assert backfill.since_to_oldest("") == "0"
    assert float(backfill.since_to_oldest("2026-07-01")) == datetime(2026, 7, 1, tzinfo=timezone.utc).timestamp()


def test_bank_all_numbers_in_order_counts_duplicates_and_stays_quiet(bank, capsys):
    client = FakeClient()
    posts = [slack_msg("1.0", "meme bank", "F1"), slack_msg("2.0", "meme bank", "F2"),
             slack_msg("3.0", "meme bank again", "F1COPY")]
    counts = backfill.bank_all(bank, client, "CGEN", posts)
    assert counts == {"new": 2, "already": 0, "duplicate": 1, "withdrawn": 0}
    assert [m.message_ts for m in bank.store.all()] == ["1.0", "2.0"]
    assert not client.named("chat_postMessage") and not client.named("reactions_add")  # no --ack: nothing posted
    assert backfill.bank_all(bank, client, "CGEN", posts[:1]) == {"new": 0, "already": 1, "duplicate": 0, "withdrawn": 0}
    assert "duplicate #1" in capsys.readouterr().out


def test_ack_replies_on_the_old_posts(bank):
    client = FakeClient()
    backfill.bank_all(bank, client, "CGEN", [slack_msg("1.0", "meme bank", "F1")], ack=True)
    assert client.named("reactions_add") and client.named("chat_postMessage")[0]["thread_ts"] == "1.0"


def test_dry_run_lists_and_changes_nothing(tmp_path, monkeypatch, capsys):
    for k in ("SLACK_BOT_TOKEN", "STORAGE_BACKEND", "LOCAL_DATA_DIR"):
        monkeypatch.setenv(k, "")  # so what --env-file loads is undone after the test
    env = tmp_path / "test.env"
    env.write_text(f"SLACK_BOT_TOKEN=xoxb-test\nSTORAGE_BACKEND=local\nLOCAL_DATA_DIR={tmp_path / 'data'}\n")
    client = HistoryClient([slack_msg("2.0", "meme bank", "F2"), slack_msg("1.0", "a meme for the meme bank", "F1")])
    monkeypatch.setattr(backfill, "WebClient", lambda token: client)
    monkeypatch.setattr(backfill, "make_store", lambda cfg: pytest.fail("a dry run mustn't open the store"))
    monkeypatch.setattr(sys, "argv", ["backfill", "--channel", "CGEN", "--dry-run", "--env-file", str(env)])
    backfill.main()
    out = capsys.readouterr().out
    assert "2 messages read, 1 meme-bank posts with images" in out and "1970-01-01  a meme for the meme bank" in out
    assert "1 image posts only mention the meme bank, so they're left out" in out and "1970-01-01  meme bank" in out
    assert not (tmp_path / "data").exists()


# ---------------------------------------------------------------- import_local
def test_date_becomes_utc_midnight_posted_at():
    assert import_local.date_to_posted_at("2026-07-27") == "2026-07-27T00:00:00+00:00"
    assert import_local.date_to_posted_at("") == ""


def test_importing_the_same_file_twice_banks_it_once(bank, tmp_path):
    f = tmp_path / "rescued.png"
    f.write_bytes(png(3))
    first = list(import_local.import_files(bank, [str(f)], caption="for the meme bank", posted_by="U01D6UG5KFH",
                                           posted_at="2026-07-27T00:00:00+00:00"))
    again = list(import_local.import_files(bank, [str(f)]))
    assert [d.status for _, d in first + again] == ["new", "already"]
    m = bank.store.get(1)
    assert (m.posted_at, m.posted_by, m.caption) == ("2026-07-27T00:00:00+00:00", "U01D6UG5KFH", "for the meme bank")
    assert m.source_key.startswith("local:")


def test_import_reports_each_file_as_it_goes_so_a_later_failure_doesnt_hide_earlier_ones(bank, tmp_path, monkeypatch, capsys):
    good = tmp_path / "good.png"
    good.write_bytes(png(1))
    for k in ("STORAGE_BACKEND", "LOCAL_DATA_DIR"):
        monkeypatch.setenv(k, "")
    env = tmp_path / "test.env"
    env.write_text(f"STORAGE_BACKEND=local\nLOCAL_DATA_DIR={tmp_path / 'data'}\n")
    monkeypatch.setattr(import_local, "ClaudeTagger", lambda *a: FakeTagger())
    monkeypatch.setattr(sys, "argv", ["import_local", str(good), str(tmp_path / "missing.png"), "--env-file", str(env)])
    with pytest.raises(FileNotFoundError):
        import_local.main()
    assert "new       #1" in capsys.readouterr().out  # printed before the second file failed
