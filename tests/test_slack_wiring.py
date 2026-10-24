"""Feeds Slack-shaped events through the real Bolt app, with the Slack Web API faked out."""
import pytest
from slack_bolt.request import BoltRequest
from slack_sdk.web.slack_response import SlackResponse

from memebank.config import Config
from memebank.slack_app import build_app

BOT = "UBOT123"


class SpyBank:
    def __init__(self):
        self.deposits, self.asks = [], []

    def deposit_message(self, client, channel, message, acknowledge=True):
        self.deposits.append((channel, message.get("ts")))
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


def test_bank_reaction_deposits_the_message(app_and_bank):
    app, bank, history = app_and_bank
    history["6.0"] = [{"ts": "6.0", "user": "UKEVIN", "text": "lol", "files": IMG}]
    send(app, {"type": "reaction_added", "user": "UTONY", "reaction": "bank",
               "item": {"type": "message", "channel": "CGEN", "ts": "6.0"}})
    send(app, {"type": "reaction_added", "user": "UTONY", "reaction": "joy",
               "item": {"type": "message", "channel": "CGEN", "ts": "6.0"}})
    assert bank.deposits == [("CGEN", "6.0")]


def test_bank_reaction_on_a_thread_reply_deposits_it(app_and_bank):
    app, bank, history = app_and_bank
    history["7.1"] = [{"ts": "7.1", "thread_ts": "7.0", "user": "UKEVIN", "text": "lol", "files": IMG}]
    send(app, {"type": "reaction_added", "user": "UTONY", "reaction": "bank",
               "item": {"type": "message", "channel": "CGEN", "ts": "7.1"}})
    assert bank.deposits == [("CGEN", "7.1")]
