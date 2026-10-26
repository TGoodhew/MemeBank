"""Feeds Slack-shaped events through the real Bolt app, with the Slack Web API faked out."""
import time
from concurrent.futures import Executor, Future
from types import SimpleNamespace

import pytest
from slack_bolt.request import BoltRequest
from slack_sdk.web.slack_response import SlackResponse

from memebank.config import Config
from memebank.slack_app import build_app
from memebank.triggers import Command, parse_command

BOT = "UBOT123"


class SpyBank:
    def __init__(self):
        self.deposits, self.asks, self.banked_by, self.confirms, self.said = [], [], [], [], []
        self.abouts = []  # the meme each ask was about ("tag it ..."), or None
        self.deposit_results = []  # what deposit_message returns
        self.meant = {}  # text -> Command, for what Claude would make of it
        self.understood = []  # what was handed to understand() (where Claude may be asked)
        self.banked = {}  # source key -> meme, for "is it already in the vault?"
        self.store = SimpleNamespace(find_by_source=self.banked.get)

    def ask_to_bank(self, client, channel, message):
        self.confirms.append((channel, message.get("ts")))
        return True

    def say(self, client, channel, text, thread_ts=None):
        self.said.append((channel, text, thread_ts))

    def deposit_message(self, client, channel, message, acknowledge=True, banked_by=""):
        self.deposits.append((channel, message.get("ts")))
        self.banked_by.append(banked_by)
        return self.deposit_results

    def understand(self, rest):
        self.understood.append(rest)
        return self.meant.get(rest) or parse_command(rest)

    def handle_ask(self, client, channel, user, rest, thread_ts=None, about=None, cmd=None):
        self.asks.append((channel, user, rest, thread_ts))
        self.abouts.append(about)


@pytest.fixture
def app_and_bank(monkeypatch):
    history = {}

    def fake_api_call(self, api_method, **kw):
        data = {"ok": True}
        if api_method == "auth.test":
            data.update(user_id=BOT, bot_id="BBOT", team_id="T1", url="https://x.slack.com/")
        params = kw.get("params") or kw.get("data") or {}
        if api_method == "conversations.history":  # like Slack, top-level posts only
            latest = params.get("latest")
            msgs = history.get(latest) if latest in history else history.get(None, [])
            if latest and str(params.get("inclusive", "")).lower() not in ("1", "true"):  # strictly before latest
                msgs = sorted((m for m in msgs if float(m["ts"]) < float(latest)), key=lambda m: -float(m["ts"]))
                msgs = msgs[:int(params.get("limit", 100))]
            data["messages"] = [m for m in msgs if m.get("thread_ts", m["ts"]) == m["ts"]]
        if api_method == "conversations.replies":
            pages = history.get(("pages", params.get("ts")))  # a long thread, a page at a time
            if pages:
                page = int(params.get("cursor") or 0)
                data["messages"] = pages[page]
                if page + 1 < len(pages):
                    data["response_metadata"] = {"next_cursor": str(page + 1)}
            else:
                data["messages"] = history.get(params.get("ts"), [])
        return SlackResponse(client=self, http_verb="POST", api_url=api_method, req_args={}, data=data,
                             headers={}, status_code=200)

    monkeypatch.setattr("slack_sdk.web.client.WebClient.api_call", fake_api_call)
    cfg = Config(slack_bot_token="xoxb-test", channel_ids=["CGEN"])
    bank = SpyBank()
    app = build_app(cfg, bank)
    app.listener_runner.listener_executor = InlineExecutor()  # each send() finishes before the test checks
    return app, bank, history


class InlineExecutor(Executor):
    """Runs Bolt's listeners on the calling thread, so tests don't race the thread pool."""

    def submit(self, fn, /, *args, **kwargs):
        future = Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as exc:  # noqa: BLE001
            future.set_exception(exc)
        return future


def send(app, event):
    body = {"type": "event_callback", "team_id": "T1", "api_app_id": "A1", "event": event,
            "event_id": "Ev1", "event_time": 1}
    resp = app.dispatch(BoltRequest(body=body, mode="socket_mode"))
    assert resp.status == 200


def wait_for(done, timeout=5.0):
    """For work a listener hands to another thread; listeners themselves run inline (InlineExecutor)."""
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
               "ts": "8.1", "message": {"ts": "8.0", "user": "UTONY", "text": "a meme for the meme bank", **unfurl}})  # control
    wait_for(lambda: bank.deposits)
    time.sleep(0.2)  # let the two that should be ignored finish too
    assert bank.deposits == [("CGEN", "8.0")]


def test_an_image_that_only_mentions_the_bank_asks_first_instead_of_banking(app_and_bank):
    app, bank, _ = app_and_bank
    send(app, {"type": "message", "channel": "CGEN", "channel_type": "channel", "user": "UTONY", "ts": "11.0",
               "text": "A few issues away from a fully operational ~death star~ meme bank", "files": IMG,
               "subtype": "file_share"})
    send(app, {"type": "message", "subtype": "message_changed", "channel": "CGEN", "channel_type": "channel",
               "ts": "12.1", "message": {"ts": "12.0", "user": "UTONY", "text": "the meme bank is important",
                                         "attachments": [{"image_url": "https://pbs.twimg.com/b.jpg"}]}})
    wait_for(lambda: len(bank.confirms) == 2)
    assert sorted(bank.confirms) == [("CGEN", "11.0"), ("CGEN", "12.0")] and bank.deposits == []


def test_yes_in_the_confirm_dm_banks_the_original_post_and_no_leaves_it(app_and_bank):
    from memebank.core import CONFIRM_EVENT

    app, bank, history = app_and_bank
    history["10.0"] = [{"ts": "10.0", "user": "UTONY", "text": "death star meme bank", "files": IMG}]  # the post
    ask = {"ts": "40.0", "user": BOT, "text": "You mentioned the meme bank… Want me to bank it?",
           "metadata": {"event_type": CONFIRM_EVENT, "event_payload": {"channel": "CGEN", "ts": "10.0"}}}
    history["40.0"] = [ask]
    send(app, {"type": "message", "channel": "DTONY", "channel_type": "im", "user": "UTONY", "ts": "41.0",
               "thread_ts": "40.0", "text": "yes please"})
    wait_for(lambda: bank.deposits)
    assert bank.deposits == [("CGEN", "10.0")] and bank.banked_by == ["UTONY"] and bank.asks == []
    history["50.0"] = [{**ask, "ts": "50.0"}]
    send(app, {"type": "message", "channel": "DTONY", "channel_type": "im", "user": "UTONY", "ts": "51.0",
               "thread_ts": "50.0", "text": "nah"})
    wait_for(lambda: len(bank.said) >= 2)
    assert ("DTONY", "OK, I won't bank this post.", "50.0") in bank.said and len(bank.deposits) == 1


@pytest.mark.parametrize("answer", ["please don't", "please leave it out", "do not", "no thanks", "okay, don't bank it",
                                    "sure, but do not store it", "please don’t", "okay, I wouldn't bank it",
                                    "sure, I shouldn't share it"])
def test_polite_refusals_are_refusals(app_and_bank, answer):
    from memebank.core import CONFIRM_EVENT

    app, bank, history = app_and_bank
    history["10.0"] = [{"ts": "10.0", "user": "UTONY", "text": "meme bank", "files": IMG}]
    history["40.0"] = [{"ts": "40.0", "user": BOT, "text": "Want me to bank it?",
                        "metadata": {"event_type": CONFIRM_EVENT, "event_payload": {"channel": "CGEN", "ts": "10.0"}}}]
    send(app, {"type": "message", "channel": "DTONY", "channel_type": "im", "user": "UTONY", "ts": "41.0",
               "thread_ts": "40.0", "text": answer})
    wait_for(lambda: bank.said)
    time.sleep(0.1)
    assert bank.said == [("DTONY", "OK, I won't bank this post.", "40.0")] and bank.deposits == []


def _pending_question(history, post_ts="10.0"):
    from memebank.core import CONFIRM_EVENT

    history[post_ts] = [{"ts": post_ts, "user": "UTONY", "text": "meme bank", "files": IMG}]  # the post in #general
    return {"ts": "100.0", "user": BOT, "text": "Want me to bank it? Reply `yes` or `no`…",
            "metadata": {"event_type": CONFIRM_EVENT, "event_payload": {"channel": "CGEN", "ts": post_ts}}}


@pytest.mark.parametrize("answer,banks", [("yes", True), ("no", False)])
def test_yes_or_no_sent_straight_into_the_dm_answers_the_pending_question(app_and_bank, answer, banks):
    app, bank, history = app_and_bank
    mine = {"ts": "101.0", "user": "UTONY", "text": answer}
    history[None] = [mine, _pending_question(history)]  # newest first; Slack includes the answer just sent
    send(app, {"type": "message", "channel": "DTONY", "channel_type": "im", "user": "UTONY", **mine})
    wait_for(lambda: bank.said or bank.deposits)
    time.sleep(0.1)
    assert bank.asks == []  # not a search for "yes"/"no" any more
    if banks:
        assert bank.deposits == [("CGEN", "10.0")] and bank.banked_by == ["UTONY"]
    else:
        assert bank.deposits == [] and bank.said == [("DTONY", "OK, I won't bank this post.", None)]


def test_a_top_level_yes_answers_the_question_before_it_not_a_newer_one(app_and_bank):
    app, bank, history = app_and_bank
    mine = {"ts": "101.0", "user": "UTONY", "text": "yes"}
    newer = dict(_pending_question(history, post_ts="20.0"), ts="102.0")  # asked while the yes was on its way
    history[None] = [newer, mine, _pending_question(history)]
    send(app, {"type": "message", "channel": "DTONY", "channel_type": "im", "user": "UTONY", **mine})
    wait_for(lambda: bank.deposits)
    assert bank.deposits == [("CGEN", "10.0")]


@pytest.mark.parametrize("text", ["tag #12 without context", "no France memes?", "not funny memes"])
def test_a_short_command_or_search_after_a_question_isnt_taken_as_an_answer(app_and_bank, text):
    app, bank, history = app_and_bank
    mine = {"ts": "101.0", "user": "UTONY", "text": text}
    history[None] = [mine, _pending_question(history)]
    send(app, {"type": "message", "channel": "DTONY", "channel_type": "im", "user": "UTONY", **mine})
    wait_for(lambda: bank.asks)
    assert bank.deposits == [] and bank.said == [] and [a[2] for a in bank.asks] == [text]


@pytest.mark.parametrize("text,banks", [("yes please", True), ("Sure, go ahead!", True), ("ok bank it", True),
                                        ("no thanks", False), ("please don't", False), ("nah, leave it", False)])
def test_short_answers_sent_straight_into_the_dm(app_and_bank, text, banks):
    app, bank, history = app_and_bank
    mine = {"ts": "101.0", "user": "UTONY", "text": text}
    history[None] = [mine, _pending_question(history)]
    send(app, {"type": "message", "channel": "DTONY", "channel_type": "im", "user": "UTONY", **mine})
    wait_for(lambda: bank.said or bank.deposits)
    assert bank.asks == [] and bool(bank.deposits) == banks


def test_a_top_level_yes_with_no_question_pending_is_still_a_search(app_and_bank):
    app, bank, history = app_and_bank
    mine = {"ts": "101.0", "user": "UTONY", "text": "yes"}
    history[None] = [mine, {"ts": "100.0", "user": BOT, "text": "Banked *#21: …*. Here are the tags I used…"}]
    send(app, {"type": "message", "channel": "DTONY", "channel_type": "im", "user": "UTONY", **mine})
    wait_for(lambda: bank.asks)
    assert bank.asks == [("DTONY", "UTONY", "yes", None)] and bank.deposits == []


def test_a_longer_top_level_message_after_a_question_is_still_a_search(app_and_bank):
    app, bank, history = app_and_bank
    mine = {"ts": "101.0", "user": "UTONY", "text": "no wait, find me the France meme please"}
    history[None] = [mine, _pending_question(history)]
    send(app, {"type": "message", "channel": "DTONY", "channel_type": "im", "user": "UTONY", **mine})
    wait_for(lambda: bank.asks)
    assert bank.deposits == [] and bank.said == [] and len(bank.asks) == 1


def test_no_after_someone_else_banked_it_doesnt_pretend_it_was_left_out(app_and_bank):
    from memebank.core import CONFIRM_EVENT

    app, bank, history = app_and_bank
    bank.banked["slack:F1"] = SimpleNamespace(number=23, withdrawn_at="")  # a :bank: reaction got there first
    history["10.0"] = [{"ts": "10.0", "user": "UTONY", "text": "meme bank", "files": IMG}]
    history["90.0"] = [{"ts": "90.0", "user": BOT, "text": "Want me to bank it?",
                        "metadata": {"event_type": CONFIRM_EVENT, "event_payload": {"channel": "CGEN", "ts": "10.0"}}}]
    send(app, {"type": "message", "channel": "DTONY", "channel_type": "im", "user": "UTONY", "ts": "91.0",
               "thread_ts": "90.0", "text": "no"})
    wait_for(lambda: bank.said)
    assert "already in the vault as #23" in bank.said[0][1] and "left it out" not in bank.said[0][1]


def test_confirm_metadata_only_counts_on_a_question_the_bot_asked(app_and_bank):
    from memebank.core import CONFIRM_EVENT

    app, bank, history = app_and_bank
    history["10.0"] = [{"ts": "10.0", "user": "UKEVIN", "text": "meme bank", "files": IMG}]
    history["80.0"] = [{"ts": "80.0", "user": "UTONY", "text": "looks like a question",  # not the bot's message
                        "metadata": {"event_type": CONFIRM_EVENT, "event_payload": {"channel": "CGEN", "ts": "10.0"}}}]
    send(app, {"type": "message", "channel": "DTONY", "channel_type": "im", "user": "UTONY", "ts": "81.0",
               "thread_ts": "80.0", "text": "yes"})
    wait_for(lambda: bank.asks)
    assert bank.deposits == [] and bank.said == []  # treated as an ordinary DM message, nothing banked


def test_yes_finds_a_post_that_was_a_thread_reply_through_its_thread(app_and_bank):
    from memebank.core import CONFIRM_EVENT

    app, bank, history = app_and_bank
    history["9.0"] = [{"ts": "9.0", "user": "UKEVIN", "text": "thread starter"},  # replies come from the thread's ts
                      {"ts": "9.5", "thread_ts": "9.0", "user": "UTONY", "text": "meme bank", "files": IMG}]
    history["60.0"] = [{"ts": "60.0", "user": BOT, "text": "Want me to bank it?", "metadata": {
        "event_type": CONFIRM_EVENT, "event_payload": {"channel": "CGEN", "ts": "9.5", "thread_ts": "9.0"}}}]
    send(app, {"type": "message", "channel": "DTONY", "channel_type": "im", "user": "UTONY", "ts": "61.0",
               "thread_ts": "60.0", "text": "yes"})
    wait_for(lambda: bank.deposits)
    assert bank.deposits == [("CGEN", "9.5")]


def test_a_dm_link_preview_with_meme_bank_banks_like_any_dm(app_and_bank):
    app, bank, _ = app_and_bank
    send(app, {"type": "message", "subtype": "message_changed", "channel": "DTONY", "channel_type": "im",
               "ts": "70.1", "message": {"ts": "70.0", "user": "UTONY", "text": "meme bank <https://x.com/a>",
                                         "attachments": [{"image_url": "https://pbs.twimg.com/a.jpg"}]}})
    wait_for(lambda: bank.deposits)
    assert bank.deposits == [("DTONY", "70.0")] and bank.confirms == []


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
    assert bank.asks == [("DTONY", "UTONY", 'Tag it "Trump screamer" and "Beanie Girl"', None)]
    assert bank.abouts == [21]


def test_tag_it_after_a_dm_deposit_finds_the_banked_reply_in_the_images_thread(app_and_bank):
    app, bank, history = app_and_bank
    history[None] = [  # newest first: the user's DM'd image (its "Banked as #22" is a thread reply), then an older #21
        {"ts": "30.0", "user": "UTONY", "text": "", "reply_count": 1, "files": IMG},
        {"ts": "21.0", "user": BOT, "text": "Banked *#21: Leftists when they get what they want*. Here are the tags…"},
    ]
    history["30.0"] = [{"ts": "30.0", "user": "UTONY", "text": ""},
                       {"ts": "30.5", "thread_ts": "30.0", "user": BOT, "text": "Banked as *#22: Cabin deck*. Here are…"}]
    send(app, {"type": "message", "channel": "DTONY", "channel_type": "im", "user": "UTONY", "ts": "31.0",
               "text": "tag it cabin trip"})
    wait_for(lambda: bank.asks)
    assert bank.asks == [("DTONY", "UTONY", "tag it cabin trip", None)]
    assert bank.abouts == [22]  # not the older top-level #21


def test_tag_it_with_nothing_banked_in_the_dm_is_left_for_the_bank_to_ask_which(app_and_bank):
    app, bank, history = app_and_bank
    send(app, {"type": "message", "channel": "DTONY", "channel_type": "im", "user": "UTONY", "ts": "22.0",
               "text": "tag it cabin trip"})
    wait_for(lambda: bank.asks)
    assert bank.asks == [("DTONY", "UTONY", "tag it cabin trip", None)] and bank.abouts == [None]


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


# ---------------------------------------------------------------- "this"/"it" in a channel thread (#56)
def _reply(text, ts="20.0", thread_ts="10.0", user="UZAK"):
    return {"type": "message", "channel": "CGEN", "channel_type": "channel", "user": user, "ts": ts,
            "thread_ts": thread_ts, "text": text}


def test_tag_it_under_a_banked_image_post_tags_that_meme(app_and_bank):
    app, bank, history = app_and_bank
    bank.banked["slack:F1"] = SimpleNamespace(number=12, withdrawn_at="")
    history["10.0"] = [{"ts": "10.0", "user": "UTONY", "text": "lol", "files": IMG}]
    send(app, _reply(f"<@{BOT}> tag it too real"))
    assert bank.asks == [("CGEN", "UZAK", "tag it too real", "10.0")] and bank.abouts == [12]


def test_hey_meme_bank_works_in_a_thread_too(app_and_bank):
    app, bank, history = app_and_bank
    bank.banked["slack:F1"] = SimpleNamespace(number=12, withdrawn_at="")
    history["10.0"] = [{"ts": "10.0", "user": "UTONY", "text": "lol", "files": IMG}]
    send(app, _reply("hey meme bank, forget this"))
    assert bank.asks == [("CGEN", "UZAK", "forget this", "10.0")] and bank.abouts == [12]


@pytest.mark.parametrize("bot_text", [
    "Banked as *#12: Cat hates Mondays*\n_cat, monday_",  # the deposit acknowledgement
    "Dealer's choice: *#12: Cat hates Mondays* deposited by <@UTONY>\nAlso in the vault: #13 Dog  ·  #14 Fish",
    "Tagged *#12: Cat hates Mondays* with _too real_\nAll tags: _cat, too real_",
])
def test_the_bot_naming_a_meme_in_the_thread_is_what_it_means(app_and_bank, bot_text):
    app, bank, history = app_and_bank
    history["10.0"] = [{"ts": "10.0", "user": "UTONY", "text": "hey meme bank, mondays"},
                       {"ts": "11.0", "thread_ts": "10.0", "user": BOT, "bot_id": "BBOT", "text": bot_text}]
    send(app, _reply(f"<@{BOT}> retag it"))
    assert bank.abouts == [12]


def test_a_meme_the_bot_posted_top_level_is_what_its_thread_means(app_and_bank):
    app, bank, history = app_and_bank
    history["10.0"] = [{"ts": "10.0", "user": BOT, "bot_id": "BBOT", "files": IMG,  # the bot's upload of #12
                        "text": "*#12: Cat hates Mondays* deposited by <@UTONY>"}]
    send(app, _reply(f"<@{BOT}> tag it too real"))
    assert bank.abouts == [12] and bank.deposits == []  # its own copy isn't a new post to bank


def test_the_nearest_meme_above_wins_and_later_ones_dont_count(app_and_bank):
    app, bank, history = app_and_bank
    bank.banked["slack:F1"] = SimpleNamespace(number=12, withdrawn_at="")
    bank.banked["slack:F2"] = SimpleNamespace(number=15, withdrawn_at="")
    img2 = [{**IMG[0], "id": "F2"}]
    history["10.0"] = [{"ts": "10.0", "user": "UTONY", "text": "lol", "files": IMG},
                       {"ts": "15.0", "thread_ts": "10.0", "user": "UKEVIN", "text": "even better", "files": img2},
                       {"ts": "30.0", "thread_ts": "10.0", "user": BOT, "text": "Banked as *#40: Later*"}]
    send(app, _reply(f"<@{BOT}> tag it too real", ts="20.0"))
    assert bank.abouts == [15]


def test_an_explicit_number_wins_over_the_thread(app_and_bank):
    app, bank, history = app_and_bank
    bank.banked["slack:F1"] = SimpleNamespace(number=15, withdrawn_at="")
    history["10.0"] = [{"ts": "10.0", "user": "UTONY", "text": "lol", "files": IMG}]
    send(app, _reply(f"<@{BOT}> tag #12 too real"))
    assert bank.asks == [("CGEN", "UZAK", "tag #12 too real", "10.0")] and bank.abouts == [None]


def test_a_post_with_several_banked_images_asks_which(app_and_bank):
    app, bank, history = app_and_bank
    bank.banked["slack:F1"] = SimpleNamespace(number=12, withdrawn_at="")
    bank.banked["slack:F2"] = SimpleNamespace(number=13, withdrawn_at="")
    history["10.0"] = [{"ts": "10.0", "user": "UTONY", "text": "two", "files": IMG + [{**IMG[0], "id": "F2"}]}]
    send(app, _reply(f"<@{BOT}> tag it too real"))
    assert bank.asks == [] and "Which one, #12 or #13?" in bank.said[0][1]


def test_tagging_an_unbanked_post_banks_it_first(app_and_bank):
    app, bank, history = app_and_bank
    history["10.0"] = [{"ts": "10.0", "user": "UTONY", "text": "lol", "files": IMG}]
    bank.deposit_results = [SimpleNamespace(meme=SimpleNamespace(number=31), status="new")]
    send(app, _reply(f"<@{BOT}> tag it too real"))
    assert bank.deposits == [("CGEN", "10.0")] and bank.banked_by == ["UZAK"]
    assert bank.asks == [("CGEN", "UZAK", "tag it too real", "10.0")] and bank.abouts == [31]


def test_tagging_an_unbanked_post_that_cant_be_banked_stops_there(app_and_bank):
    app, bank, history = app_and_bank
    history["10.0"] = [{"ts": "10.0", "user": "UTONY", "text": "lol", "files": IMG}]
    send(app, _reply(f"<@{BOT}> tag it too real"))  # deposit_message returns nothing (it has said why)
    assert bank.deposits == [("CGEN", "10.0")] and bank.asks == []


def test_other_commands_on_an_unbanked_post_dont_bank_it(app_and_bank):
    app, bank, history = app_and_bank
    history["10.0"] = [{"ts": "10.0", "user": "UTONY", "text": "lol", "files": IMG}]
    send(app, _reply(f"<@{BOT}> forget this"))
    assert bank.deposits == [] and bank.asks == [] and "isn't in the vault yet" in bank.said[0][1]


def test_no_meme_in_the_thread_or_a_top_level_message_leaves_it_to_the_bank_to_ask(app_and_bank):
    app, bank, history = app_and_bank
    history["10.0"] = [{"ts": "10.0", "user": "UTONY", "text": "anyone up for lunch?"}]
    send(app, _reply(f"<@{BOT}> tag it too real"))
    send(app, {"type": "message", "channel": "CGEN", "channel_type": "channel", "user": "UZAK", "ts": "30.0",
               "text": f"<@{BOT}> tag it too real"})
    assert bank.abouts == [None, None] and bank.deposits == []


def test_a_search_in_a_thread_isnt_about_the_threads_meme(app_and_bank):
    app, bank, history = app_and_bank
    bank.banked["slack:F1"] = SimpleNamespace(number=12, withdrawn_at="")
    history["10.0"] = [{"ts": "10.0", "user": "UTONY", "text": "lol", "files": IMG}]
    send(app, _reply(f"<@{BOT}> find me the France meme"))
    assert bank.asks == [("CGEN", "UZAK", "find me the France meme", "10.0")] and bank.abouts == [None]


def test_a_long_threads_later_pages_are_read_for_the_nearest_meme(app_and_bank):
    app, bank, history = app_and_bank
    bank.banked["slack:F1"] = SimpleNamespace(number=12, withdrawn_at="")
    history[("pages", "10.0")] = [
        [{"ts": "10.0", "user": "UTONY", "text": "lol", "files": IMG},
         {"ts": "11.0", "thread_ts": "10.0", "user": "UKEVIN", "text": "chat"}],
        [{"ts": "15.0", "thread_ts": "10.0", "user": BOT, "text": "Banked as *#40: A later one*"}],
    ]
    send(app, _reply(f"<@{BOT}> tag it too real", ts="20.0"))
    assert bank.abouts == [40]


def test_tagging_a_partly_banked_post_banks_the_rest_and_asks_which(app_and_bank):
    app, bank, history = app_and_bank
    bank.banked["slack:F1"] = SimpleNamespace(number=12, withdrawn_at="")  # F2 isn't banked yet
    history["10.0"] = [{"ts": "10.0", "user": "UTONY", "text": "two", "files": IMG + [{**IMG[0], "id": "F2"}]}]
    bank.deposit_results = [SimpleNamespace(meme=SimpleNamespace(number=12), status="already"),
                            SimpleNamespace(meme=SimpleNamespace(number=13), status="new")]
    send(app, _reply(f"<@{BOT}> tag it too real"))
    assert bank.deposits == [("CGEN", "10.0")] and bank.asks == [] and "Which one, #12 or #13?" in bank.said[0][1]
    send(app, _reply(f"<@{BOT}> retag it", ts="21.0"))  # anything else goes with what's banked
    assert bank.abouts == [12] and len(bank.deposits) == 1


def test_a_bare_hashtag_under_a_meme_is_about_it(app_and_bank):
    app, bank, history = app_and_bank
    bank.banked["slack:F1"] = SimpleNamespace(number=12, withdrawn_at="")
    history["10.0"] = [{"ts": "10.0", "user": "UTONY", "text": "lol", "files": IMG}]
    send(app, _reply(f"<@{BOT}> #tooReal"))
    assert bank.asks == [("CGEN", "UZAK", "#tooReal", "10.0")] and bank.abouts == [12]


def test_a_bare_hashtag_straight_into_the_dm_is_still_a_search(app_and_bank):
    app, bank, history = app_and_bank
    history[None] = [{"ts": "21.0", "user": BOT, "text": "Banked *#21: Cabin deck*. Here are the tags…"}]
    send(app, {"type": "message", "channel": "DTONY", "channel_type": "im", "user": "UTONY", "ts": "22.0",
               "text": "#mondays"})
    assert bank.asks == [("DTONY", "UTONY", "#mondays", None)] and bank.abouts == [None]


def test_which_one_suggests_a_command_that_works(app_and_bank):
    app, bank, history = app_and_bank
    bank.banked["slack:F1"] = SimpleNamespace(number=12, withdrawn_at="")
    bank.banked["slack:F2"] = SimpleNamespace(number=13, withdrawn_at="")
    history["10.0"] = [{"ts": "10.0", "user": "UTONY", "text": "two", "files": IMG + [{**IMG[0], "id": "F2"}]}]
    send(app, _reply(f"<@{BOT}> tags?"))
    assert "`hey meme bank, what are the tags on #12`" in bank.said[0][1]


def test_a_command_claude_understood_still_finds_the_threads_meme(app_and_bank):
    app, bank, history = app_and_bank
    bank.banked["slack:F1"] = SimpleNamespace(number=12, withdrawn_at="")
    history["10.0"] = [{"ts": "10.0", "user": "UTONY", "text": "lol", "files": IMG}]
    bank.meant["chuck too real on this one"] = Command("tag", None, query='"too real"')
    send(app, _reply(f"<@{BOT}> chuck too real on this one"))
    assert bank.abouts == [12]


def test_tag_replies_in_the_banked_dm_thread_never_go_to_claude(app_and_bank):
    app, bank, history = app_and_bank
    history["9.0"] = [{"ts": "9.0", "user": BOT, "text": "Banked *#7: Castro cigar*. Here are the tags I used: ..."}]
    send(app, {"type": "message", "channel": "DTONY", "channel_type": "im", "user": "UTONY", "ts": "9.5",
               "thread_ts": "9.0", "text": "price tag, sticker"})
    assert bank.asks == [("DTONY", "UTONY", "tag #7 price tag, sticker", "9.0")] and bank.understood == []


# ---------------------------------------------------------------- answering "Did you mean ...?"
def _suggested_in_thread(history, suggestion="tag this functionally useless"):
    """The thread from the screenshot: a meme banked, an instruction Claude wasn't sure of, the bot's suggestion."""
    history["10.0"] = [
        {"ts": "10.0", "user": "UTONY", "text": f"<@{BOT}> a real car in Oregon", "files": IMG},
        {"ts": "11.0", "thread_ts": "10.0", "user": BOT, "bot_id": "BBOT", "text": "Banked as *#35: Hippie Jeep*\n_jeep_"},
        {"ts": "12.0", "thread_ts": "10.0", "user": "UTONY",
         "text": f"<@{BOT}> I noticed that you didn't tag it \"Functionally useless\""},
        {"ts": "13.0", "thread_ts": "10.0", "user": BOT, "bot_id": "BBOT",
         "text": f"Did you mean `hey meme bank, {suggestion}`?"},
    ]


@pytest.mark.parametrize("answer", ["yes", "Yes please!", f"<@{BOT}> yes", "hey meme bank, yep", "ok do it"])
def test_yes_to_did_you_mean_does_what_it_suggested(app_and_bank, answer):
    app, bank, history = app_and_bank
    _suggested_in_thread(history)
    send(app, _reply(answer, ts="14.0", user="UTONY"))
    assert bank.asks == [("CGEN", "UTONY", "tag this functionally useless", "10.0")] and bank.abouts == [35]
    assert bank.understood == []  # the suggestion is a command already: no asking Claude again


@pytest.mark.parametrize("answer", ["no", f"<@{BOT}> nope", "no thanks", "nah, leave it"])
def test_no_to_did_you_mean_leaves_it(app_and_bank, answer):
    app, bank, history = app_and_bank
    _suggested_in_thread(history)
    send(app, _reply(answer, ts="14.0", user="UTONY"))
    assert bank.asks == [] and len(bank.said) == 1 and bank.said[0][0::2] == ("CGEN", "10.0")
    assert bank.said[0][1].startswith("OK, I'll leave it.")


def test_yes_only_answers_a_suggestion_right_before_it(app_and_bank):
    app, bank, history = app_and_bank
    _suggested_in_thread(history)
    history["10.0"].append({"ts": "13.5", "thread_ts": "10.0", "user": "UZAK", "text": "is this real?"})
    send(app, _reply("yes", ts="14.0", user="UTONY"))  # Tony answering Zak
    assert bank.asks == [] and bank.said == []


@pytest.mark.parametrize("text", ["yes, and cabin trip too", "thanks"])
def test_more_than_a_yes_after_a_suggestion_isnt_an_answer(app_and_bank, text):
    app, bank, history = app_and_bank
    _suggested_in_thread(history)
    send(app, _reply(text, ts="14.0", user="UTONY"))
    assert bank.asks == [] and bank.said == []


def test_only_the_bots_own_suggestion_counts(app_and_bank):
    app, bank, history = app_and_bank
    _suggested_in_thread(history)
    history["10.0"][-1] = dict(history["10.0"][-1], user="UZAK", bot_id=None)  # someone typing it, not the bot
    send(app, _reply("yes", ts="14.0", user="UTONY"))
    assert bank.asks == [] and bank.said == []


def test_a_yes_to_the_bot_with_no_suggestion_before_it_is_handled_as_before(app_and_bank):
    app, bank, history = app_and_bank
    history["10.0"] = [{"ts": "10.0", "user": "UTONY", "text": "lol"}]
    send(app, _reply(f"<@{BOT}> yes", ts="14.0", user="UTONY"))
    assert bank.asks == [("CGEN", "UTONY", "yes", "10.0")]


def test_yes_to_a_suggestion_straight_into_the_dm(app_and_bank):
    app, bank, history = app_and_bank
    mine = {"ts": "101.0", "user": "UTONY", "text": "yes"}
    history[None] = [mine, {"ts": "100.0", "user": BOT, "text": "Did you mean `hey meme bank, forget #12`?"}]
    send(app, {"type": "message", "channel": "DTONY", "channel_type": "im", "user": "UTONY", **mine})
    assert bank.asks == [("DTONY", "UTONY", "forget #12", None)] and bank.deposits == []


def test_a_suggestion_pasted_back_with_its_code_formatting_works(app_and_bank):
    app, bank, history = app_and_bank
    _suggested_in_thread(history)
    send(app, _reply("`hey meme bank, tag this functionally useless`", ts="14.0", user="UTONY"))
    assert bank.asks == [("CGEN", "UTONY", "tag this functionally useless", "10.0")] and bank.abouts == [35]
