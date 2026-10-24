"""ClaudeTagger's requests and answers, against a fake Anthropic client (no network)."""
import json
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from memebank.tagger import FALLBACK_BETA, PICK_SCHEMA, TAG_SCHEMA, ClaudeTagger, TaggerError


class FakeAnthropic:
    def __init__(self, answer=None, stop_reason="end_turn", error=None):
        self.requests = []
        self.answer, self.stop_reason, self.error = answer, stop_reason, error
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.requests.append(kw)
        if self.error:
            raise self.error
        content = [SimpleNamespace(type="thinking", thinking="")]  # thinking comes first, with its text omitted
        if self.answer is not None:
            content.append(SimpleNamespace(type="text", text=json.dumps(self.answer)))
        return SimpleNamespace(stop_reason=self.stop_reason, stop_details=None, content=content)


TAGS = {"title": "Fidel island socialism", "description": "Castro with a cigar.", "text_in_image": "HAD HIS OWN ISLAND",
        "tags": ["Cuba", "#socialism", "cuba", "fidel  castro"]}


def tagger(client, model="claude-sonnet-5-5"):
    return ClaudeTagger("sk-ant-test", tag_model=model, search_model=model, client=client)


def test_tag_asks_for_json_not_a_forced_tool_call():
    client = FakeAnthropic(TAGS)
    t = tagger(client).tag(b"png-bytes", "image/png", caption="a meme for the meme bank")
    req = client.requests[0]
    assert "tool_choice" not in req and "tools" not in req
    assert req["output_config"] == {"format": {"type": "json_schema", "schema": TAG_SCHEMA}}
    assert req["messages"][0]["content"][0]["type"] == "image"
    assert "meme for the meme bank" in req["messages"][0]["content"][1]["text"]
    assert (t.title, t.text_in_image) == ("Fidel island socialism", "HAD HIS OWN ISLAND")
    assert t.tags == ["cuba", "socialism", "fidel castro"]  # cleaned: lower-cased, no '#', deduped


def test_refusal_fallback_only_on_models_that_have_it():
    sonnet, haiku = FakeAnthropic(TAGS), FakeAnthropic(TAGS)
    tagger(sonnet).tag(b"x", "image/png")
    tagger(haiku, "claude-haiku-5-5").tag(b"x", "image/png")
    assert sonnet.requests[0]["fallbacks"] == "default" and sonnet.requests[0]["betas"] == [FALLBACK_BETA]
    assert "fallbacks" not in haiku.requests[0] and "betas" not in haiku.requests[0]


def test_pick_returns_numbers_best_first_at_most_five():
    client = FakeAnthropic({"numbers": [7, 3, 9, 1, 2, 8]})
    assert tagger(client).pick("the france one", ["#7 ...", "#3 ..."]) == [7, 3, 9, 1, 2]
    req = client.requests[0]
    assert req["output_config"]["format"]["schema"] == PICK_SCHEMA and "the france one" in req["messages"][0]["content"]


@pytest.mark.parametrize("client,says", [
    (FakeAnthropic(None, stop_reason="refusal"), "declined"),
    (FakeAnthropic(None, stop_reason="max_tokens"), "cut off"),
    (FakeAnthropic(None), "wasn't readable"),
    (FakeAnthropic(error=anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com"))),
     "couldn't be reached"),
])
def test_failures_become_short_messages_for_the_channel(client, says):
    with pytest.raises(TaggerError, match=says) as e:
        tagger(client).tag(b"x", "image/png")
    assert "invalid_request_error" not in str(e.value) and "{" not in str(e.value)
