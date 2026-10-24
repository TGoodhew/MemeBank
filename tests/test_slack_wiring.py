"""Feeds Slack-shaped events through the real Bolt app, with the Slack Web API faked out."""
import time

import pytest
from slack_bolt.request import BoltRequest
from slack_sdk.web.slack_response import SlackResponse

from memebank.config import Config
from memebank.slack_app import build_app

BOT = "UBOT123"


class SpyBank:
    def __init__(self):
        self.deposits, self.asks, self.banked_by = [], [], []

    def deposit_message(self, client, channel, message, acknowledge=True, banked_by=""):
        self.deposits.append((channel, message.get("ts")))
        self.banked_by.append(banked_by)
        return []

    def handle_ask(self, client, channel, user, rest, thread_ts=None):
        self.asks.append((channel, user, rest, thread_ts))


@pytest.fixture
def app_and_bank(monkeypatch):
    history = {}

    def fake_api_call(self, api_method, **kw):
        data = {"ok": True}
        if api_method == "auth.test":
            data.update(user_id=BOT, bot_id="BBOT", team_id="T1", url="https://x.slack.com/")
        params = kw.get("params") or kw.get("data") or {}
        if api_method == "conversations.history":  # like Slack, top-level posts only
            data["messages"] = [m for m in history.get(params.get("latest"), [])
                                if m.get("thread_ts", m["ts"]) == m["ts"]]
        if api_method == "conversations.replies":
            data["messages"] = history.get(params.get("ts"), [])
        return SlackResponse(client=self, http_verb="POST", api_url=api_method, req_args={}, data=data,
                             headers={}, status_code=200)

    monkeypatch.setattr("slack_sdk.web.client.WebClient.api_call", fake_api_call)
    cfg = Config(slack_bot_token="xoxb-test", channel_ids=["CGEN"])
    bank = SpyBank()
    app = build_app(cfg, bank)
    return app, bank, history


def send(app, event):
    body = {"type": "event_callback", "team_id": "T1", "api_app_id": "A1", "event": event,
            "event_id": "Ev1", "event_time": 1}
    resp = app.dispatch(BoltRequest(body=body, mode="socket_mode"))
    assert resp.status == 200


def wait_for(done, timeout=5.0):
    """Bolt runs listeners on a thread pool after acking, so events can finish out of order."""
    deadline = time.time() + timeout
    while not done() and time.time() < deadline:
        time.sleep(0.01)


IMG = [{"id": "F1", "mimetype": "image/png", "url_private_download": "https://files/F1"}]


def test_meme_bank_post_is_deposited(app_and_bank):
    app, bank, _ = app_and_bank
    send(app, {"type": "message", "channel": "CGEN", "channel_type": "channel", "user": "UTONY", "ts": "1.0",
               "text": "A meme for the meme bank <@U6UTZEXAB|kevin>", "files": IMG, "subtype": "file_share"})
    assert bank.deposits == [("CGEN", "1.0")]


def test_question_is_routed_with_thread(app_and_bank):
    app, bank, _ = app_and_bank
    send(app, {"type": "message", "channel": "CGEN", "channel_type": "channel", "user": "UZAK", "ts": "2.0",
               "thread_ts": "1.5", "text": "Hey meme bank, find me the France meme"})
    assert bank.asks == [("CGEN", "UZAK", "find me the France meme", "1.5")]


def test_other_channels_and_bots_are_ignored(app_and_bank):
    app, bank, _ = app_and_bank
    send(app, {"type": "message", "channel": "COTHER", "channel_type": "channel", "user": "UTONY", "ts": "3.0",
               "text": "a meme for the meme bank", "files": IMG})
    send(app, {"type": "message", "channel": "CGEN", "channel_type": "channel", "bot_id": "BX", "ts": "3.1",
               "text": "hey meme bank, help"})
    send(app, {"type": "message", "channel": "CGEN", "channel_type": "channel", "user": "UTONY", "ts": "3.2",
               "text": "ordinary chat"})
    assert bank.deposits == [] and bank.asks == []


def test_dm_is_always_a_question(app_and_bank):
    app, bank, _ = app_and_bank
    send(app, {"type": "message", "channel": "DTONY", "channel_type": "im", "user": "UTONY", "ts": "4.0",
               "text": "castro"})
    assert bank.asks == [("DTONY", "UTONY", "castro", None)]


def test_unfurl_arrives_as_edit_and_is_deposited(app_and_bank):
    app, bank, _ = app_and_bank
    send(app, {"type": "message", "subtype": "message_changed", "channel": "CGEN", "channel_type": "channel",
               "ts": "5.1", "message": {"ts": "5.0", "user": "UTONY",
                                        "text": "A legit disturbing meme for the meme bank <https://x.com/a>",
                                        "attachments": [{"image_url": "https://pbs.twimg.com/a.jpg"}]}})
    assert bank.deposits == [("CGEN", "5.0")]


def test_edits_by_bots_or_without_meme_bank_text_arent_deposited(app_and_bank):
    app, bank, _ = app_and_bank
    unfurl = {"attachments": [{"image_url": "https://pbs.twimg.com/a.jpg"}]}
    send(app, {"type": "message", "subtype": "message_changed", "channel": "CGEN", "channel_type": "channel",
               "ts": "6.1", "message": {"ts": "6.0", "bot_id": "BX", "text": "for the meme bank", **unfurl}})
    send(app, {"type": "message", "subtype": "message_changed", "channel": "CGEN", "channel_type": "channel",
               "ts": "7.1", "message": {"ts": "7.0", "user": "UTONY", "text": "look at this", **unfurl}})
    send(app, {"type": "message", "subtype": "message_changed", "channel": "CGEN", "channel_type": "channel",
               "ts": "8.1", "message": {"ts": "8.0", "user": "UTONY", "text": "meme bank", **unfurl}})  # control
    wait_for(lambda: bank.deposits)
    time.sleep(0.2)  # let the two that should be ignored finish too
    assert bank.deposits == [("CGEN", "8.0")]


def test_bank_reaction_deposits_the_message(app_and_bank):
    app, bank, history = app_and_bank
    history["6.0"] = [{"ts": "6.0", "user": "UKEVIN", "text": "lol", "files": IMG}]
    send(app, {"type": "reaction_added", "user": "UTONY", "reaction": "bank",
               "item": {"type": "message", "channel": "CGEN", "ts": "6.0"}})
    send(app, {"type": "reaction_added", "user": "UTONY", "reaction": "joy",
               "item": {"type": "message", "channel": "CGEN", "ts": "6.0"}})
    assert bank.deposits == [("CGEN", "6.0")]


def test_bank_reaction_credits_whoever_reacted(app_and_bank):
    app, bank, history = app_and_bank
    history["6.0"] = [{"ts": "6.0", "user": "UKEVIN", "text": "lol", "files": IMG}]
    send(app, {"type": "reaction_added", "user": "UTONY", "reaction": "bank",
               "item": {"type": "message", "channel": "CGEN", "ts": "6.0"}})
    assert bank.banked_by == ["UTONY"]


def test_tag_it_straight_into_the_dm_tags_the_last_meme_banked_there(app_and_bank):
    app, bank, history = app_and_bank
    history[None] = [  # the DM's recent history (fetched without "latest"), newest first like Slack
        {"ts": "21.0", "user": BOT, "text": "Banked *#21: Leftists when they get what they want*. Here are the tags…"},
        {"ts": "20.0", "user": BOT, "text": "Banked *#20: Catholic France vs atheist France*. Here are the tags…"},
    ]
    send(app, {"type": "message", "channel": "DTONY", "channel_type": "im", "user": "UTONY", "ts": "22.0",
               "text": 'Tag it "Trump screamer" and "Beanie Girl"'})
    wait_for(lambda: bank.asks)
    assert bank.asks == [("DTONY", "UTONY", 'tag #21 "Trump screamer" and "Beanie Girl"', None)]


def test_tag_it_with_nothing_banked_in_the_dm_is_left_for_the_bank_to_ask_which(app_and_bank):
    app, bank, history = app_and_bank
    send(app, {"type": "message", "channel": "DTONY", "channel_type": "im", "user": "UTONY", "ts": "22.0",
               "text": "tag it cabin trip"})
    wait_for(lambda: bank.asks)
    assert bank.asks == [("DTONY", "UTONY", "tag it cabin trip", None)]


def test_plain_words_replied_under_the_banked_dm_become_tags(app_and_bank):
    app, bank, history = app_and_bank
    history["9.0"] = [{"ts": "9.0", "user": BOT, "text": "Banked *#7: Castro cigar*. Here are the tags I used: ..."}]
    send(app, {"type": "message", "channel": "DTONY", "channel_type": "im", "user": "UTONY", "ts": "9.5",
               "thread_ts": "9.0", "text": "ilhan omar, squad"})
    send(app, {"type": "message", "channel": "DTONY", "channel_type": "im", "user": "UTONY", "ts": "9.6",
               "thread_ts": "9.0", "text": "#3"})  # an actual command in that thread still works as one
    send(app, {"type": "message", "channel": "DTONY", "channel_type": "im", "user": "UTONY", "ts": "9.7",
               "text": "ilhan omar"})  # not in a thread: an ordinary DM search
    wait_for(lambda: len(bank.asks) == 3)
    assert sorted(bank.asks, key=str) == sorted([("DTONY", "UTONY", "tag #7 ilhan omar, squad", "9.0"),
                                                 ("DTONY", "UTONY", "#3", "9.0"),
                                                 ("DTONY", "UTONY", "ilhan omar", None)], key=str)


def test_bank_reaction_on_a_thread_reply_deposits_it(app_and_bank):
    app, bank, history = app_and_bank
    history["7.1"] = [{"ts": "7.1", "thread_ts": "7.0", "user": "UKEVIN", "text": "lol", "files": IMG}]
    send(app, {"type": "reaction_added", "user": "UTONY", "reaction": "bank",
               "item": {"type": "message", "channel": "CGEN", "ts": "7.1"}})
    assert bank.deposits == [("CGEN", "7.1")]
