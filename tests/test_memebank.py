import io
import random

import pytest
from PIL import Image, ImageDraw, ImageFont

from memebank import images
from memebank.core import MemeBank, images_in, same_text
from memebank.search import keyword_rank, search
from memebank.store import LocalStore
from memebank.tagger import Tags
from memebank.triggers import classify, parse_command

BOT = "UBOT123"


# ---------------------------------------------------------------- helpers
def png(seed: int, size=(400, 300)) -> bytes:
    rnd = random.Random(seed)
    im = Image.new("RGB", (16, 12))
    im.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(16 * 12)])
    im = im.resize(size, Image.NEAREST)
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()


def template_meme(caption: str, fmt="PNG") -> bytes:
    """One picture with different top text, like two memes made from the same template."""
    with Image.open(io.BytesIO(png(7, (800, 600)))) as im:
        im = im.convert("RGB")
    ImageDraw.Draw(im).text((40, 20), caption, fill="white", font=ImageFont.load_default(size=44),
                            stroke_width=3, stroke_fill="black")
    out = io.BytesIO()
    im.save(out, fmt)
    return out.getvalue()


class FakeTagger:
    def __init__(self, by_caption=None, picks=None, fail_pick=False):
        self.by_caption = by_caption or {}
        self.picks = picks
        self.fail_pick = fail_pick
        self.tag_calls = 0
        self.pick_calls = []

    def tag(self, image, media_type, caption=""):
        assert media_type in ("image/png", "image/jpeg") and len(image) < 5_000_000
        self.tag_calls += 1
        for key, tags in self.by_caption.items():
            if key in caption.lower():
                return tags
        return Tags("Generic meme", "A meme.", "", ["meme"])

    def pick(self, query, catalogue):
        self.pick_calls.append((query, catalogue))
        if self.fail_pick:
            raise RuntimeError("API down")
        return self.picks(query) if callable(self.picks) else (self.picks or [])


class FakeClient:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def f(**kw):
            self.calls.append((name, kw))
            if name == "chat_getPermalink":
                return {"permalink": f"https://x.slack.com/p{kw['message_ts']}"}
            return {"ok": True}
        return f

    def named(self, name):
        return [kw for n, kw in self.calls if n == name]


def slack_msg(ts, text, file_id=None, user="UTONY"):
    m = {"ts": ts, "text": text, "user": user}
    if file_id:
        m["files"] = [{"id": file_id, "mimetype": "image/png", "url_private_download": f"https://files/{file_id}",
                       "name": "image.png"}]
    return m


@pytest.fixture
def setup(tmp_path):
    blobs = {"F1": png(1), "F2": png(2), "F3": png(3), "F1RESIZED": png(1, (800, 600)),
             "T1": template_meme("SOCIALISM WORKS ON PAPER"), "T2": template_meme("WHEN THE BUILD PASSES"),
             "T1JPEG": template_meme("SOCIALISM WORKS ON PAPER", "JPEG")}
    tagger = FakeTagger(
        by_caption={
            "castro": Tags("Fidel island socialism", "Castro smoking a cigar.", "HAD HIS OWN ISLAND",
                           ["fidel castro", "cuba", "socialism", "cigar"]),
            "baguette": Tags("Baguette diplomacy", "A man in a beret with bread.", "",
                             ["paris", "beret", "baguette"]),
            "template one": Tags("Socialism on paper", "A template meme.", "SOCIALISM WORKS ON PAPER", ["socialism"]),
            "template two": Tags("Build passes", "A template meme.", "WHEN THE BUILD PASSES", ["software"]),
            "repost of one": Tags("Socialism on paper", "A template meme.", "Socialism works on paper!", ["socialism"]),
        },
    )
    store = LocalStore(str(tmp_path))
    bank = MemeBank(store, tagger, "xoxb-test", admin_user_ids=["UADMIN"],
                    fetch=lambda url, token: blobs[url.rsplit("/", 1)[1]])
    return bank, store, tagger, FakeClient()


# ---------------------------------------------------------------- triggers
@pytest.mark.parametrize("text,has_img,dm,expect", [
    ("A meme for the meme bank <@U6UTZEXAB|kevin>", True, False, ("bank", "")),
    ("Did I post this as a meme bank image <@U6UTZEXAB> because if I didn't I should have...", True, False, ("bank", "")),
    ("A meme for the meme bank <@U6UTZEXAB|kevin>", False, False, None),  # link not unfurled yet: not a question
    ("This is why the meme bank is important", False, False, None),
    ("just a cat picture", True, False, None),
    ("Hey meme bank, could you find me the France meme?", False, False, ("ask", "could you find me the France meme?")),
    ("yo memebank what have you got on cats", False, False, ("ask", "what have you got on cats")),
    ("Meme bank: random", False, False, ("ask", "random")),
    ("meme bank is great", False, False, None),
    (f"<@{BOT}> find the socialism one", False, False, ("ask", "find the socialism one")),
    (f"<@{BOT}> here you go", True, False, ("bank", "")),
    ("find the castro one", False, True, ("ask", "find the castro one")),
    ("no words needed", True, True, ("bank", "")),
])
def test_classify(text, has_img, dm, expect):
    t = classify(text, has_img, BOT, is_dm=dm)
    assert (None if t is None else (t.kind, t.rest)) == expect


@pytest.mark.parametrize("rest,name,number,query", [
    ("", "help", None, ""),
    ("help", "help", None, ""),
    ("random", "random", None, ""),
    ("surprise me!", "random", None, ""),
    ("stats", "stats", None, ""),
    ("how many memes do you have?", "stats", None, ""),
    ("#12", "get", 12, ""),
    ("show me #7", "get", 7, ""),
    ("number 3", "get", 3, ""),
    ("forget #4", "forget", 4, ""),
    ("retag 9", "retag", 9, ""),
    ("could you find me the France meme?", "search", None, "the France meme"),
    ("What memes relate to France", "search", None, "What memes relate to France"),
])
def test_parse_command(rest, name, number, query):
    c = parse_command(rest)
    assert (c.name, c.number) == (name, number)
    if query:
        assert c.query == query


# ---------------------------------------------------------------- deposits
def test_deposit_tags_stores_and_acknowledges(setup):
    bank, store, tagger, client = setup
    res = bank.deposit_message(client, "CGEN", slack_msg("100.1", "castro: a meme for the meme bank", "F1"))
    assert [r.status for r in res] == ["new"]
    m = store.get(1)
    assert m.title == "Fidel island socialism" and "cuba" in m.tags
    assert m.permalink == "https://x.slack.com/p100.1" and m.posted_by == "UTONY"
    assert store.get_image(m) == png(1)
    assert client.named("reactions_add")[0]["name"] == "bank"
    reply = client.named("chat_postMessage")[0]
    assert reply["thread_ts"] == "100.1" and "#1: Fidel island socialism" in reply["text"]


def test_same_file_twice_is_banked_once(setup):
    bank, store, tagger, client = setup
    msg = slack_msg("100.1", "a meme for the meme bank", "F1")
    bank.deposit_message(client, "CGEN", msg)
    again = bank.deposit_message(client, "CGEN", msg)
    assert [r.status for r in again] == ["already"] and len(store.all()) == 1 and tagger.tag_calls == 1


def test_repost_of_same_picture_is_caught_as_duplicate(setup):
    bank, store, tagger, client = setup
    bank.deposit_message(client, "CGEN", slack_msg("100.1", "a meme for the meme bank", "F1"))
    res = bank.deposit_message(client, "CGEN", slack_msg("200.1", "This meme needs to be added again to the meme bank", "F1RESIZED"))
    assert res[0].status == "duplicate" and res[0].meme.number == 1 and len(store.all()) == 1
    assert "Already in the vault as *#1" in client.named("chat_postMessage")[-1]["text"]


def test_same_template_with_different_text_is_a_new_meme(setup):
    bank, store, tagger, client = setup
    assert images.hamming(images.dhash(template_meme("A")), images.dhash(template_meme("B"))) <= 6  # looks like a repost
    bank.deposit_message(client, "CGEN", slack_msg("100.1", "template one: a meme for the meme bank", "T1"))
    res = bank.deposit_message(client, "CGEN", slack_msg("200.1", "template two: a meme for the meme bank", "T2"))
    assert res[0].status == "new" and [m.title for m in store.all()] == ["Socialism on paper", "Build passes"]


def test_repost_with_text_is_caught_despite_small_transcription_differences(setup):
    bank, store, tagger, client = setup
    bank.deposit_message(client, "CGEN", slack_msg("100.1", "template one: a meme for the meme bank", "T1"))
    res = bank.deposit_message(client, "CGEN", slack_msg("200.1", "repost of one for the meme bank", "T1JPEG"))
    assert res[0].status == "duplicate" and res[0].meme.number == 1 and len(store.all()) == 1


@pytest.mark.parametrize("a,b,same", [
    ("", "", True),
    ("SOCIALISM WORKS\nON PAPER", "socialism works on paper.", True),
    ("It is usually futile to try to talk facts and analysis to people", "It is usualy futile to try to talk facts and analysis to people", True),
    ("SOCIALISM WORKS ON PAPER", "WHEN THE BUILD PASSES", False),
    ("SOCIALISM", "CAPITALISM", False),
    ("SOCIALISM WORKS ON PAPER", "", False),
    ("今天不上班", "明天去旅行", False),  # non-Latin text isn't stripped away
    ("今天不上班", "", False),
    ("ÇA MARCHE SUR LE PAPIER", "ça marche sur le papier.", True),  # case folds for accented letters too
    (" ".join(["build passed all checks for today"] * 8),
     "bui1d" + " ".join(["build passed all checks for today"] * 8)[5:], True),  # long text, one misread
])
def test_same_text(a, b, same):
    assert same_text(a, b) is same


def test_deposit_from_a_thread_reply_is_acknowledged_in_that_thread(setup):
    bank, store, tagger, client = setup
    bank.deposit_message(client, "CGEN", {**slack_msg("100.2", "a meme for the meme bank", "F1"), "thread_ts": "100.1"})
    assert client.named("reactions_add")[0]["timestamp"] == "100.2"  # the reaction goes on the reply itself
    assert client.named("chat_postMessage")[0]["thread_ts"] == "100.1"


def test_different_pictures_get_sequential_numbers(setup):
    bank, store, *_ , client = setup
    for i, f in enumerate(["F1", "F2", "F3"]):
        bank.deposit_message(client, "CGEN", slack_msg(f"{i}.0", "meme bank", f))
    assert [m.number for m in store.all()] == [1, 2, 3]


def test_unfurl_image_is_found():
    msg = {"ts": "5.5", "text": "A legit disturbing meme for the meme bank <https://x.com/a/status/1>",
           "attachments": [{"image_url": "https://pbs.twimg.com/a.jpg", "title": "post"}]}
    refs = images_in(msg, "CGEN")
    assert refs[0].key == "unfurl:CGEN:5.5:0" and refs[0].slack_auth is False


def test_reaction_only_mode_posts_no_thread_reply(setup):
    bank, store, tagger, client = setup
    bank.ack_mode = "reaction"
    bank.deposit_message(client, "CGEN", slack_msg("1.0", "meme bank", "F1"))
    assert client.named("reactions_add") and not client.named("chat_postMessage")


# ---------------------------------------------------------------- withdrawals
def _fill(bank, client):
    bank.deposit_message(client, "CGEN", slack_msg("1.0", "castro meme bank", "F1", user="UTONY"))
    bank.deposit_message(client, "CGEN", slack_msg("2.0", "baguette meme bank", "F2", user="UKEVIN"))
    client.calls.clear()


def test_search_uses_claude_pick_and_posts_image(setup):
    bank, store, tagger, client = setup
    _fill(bank, client)
    tagger.picks = lambda q: [2] if "france" in q.lower() else []
    bank.handle_ask(client, "CGEN", "UZAK", "What memes relate to France")
    up = client.named("files_upload_v2")[0]
    assert up["file"] == png(2) and "#2: Baguette diplomacy" in up["initial_comment"] and up["thread_ts"] is None
    assert any("#2 | Baguette diplomacy" in line for line in tagger.pick_calls[0][1])


def test_search_falls_back_to_keywords_when_claude_fails(setup):
    bank, store, tagger, client = setup
    _fill(bank, client)
    tagger.fail_pick = True
    bank.handle_ask(client, "CGEN", "UZAK", "find me the socialism meme")
    assert "#1: Fidel island socialism" in client.named("files_upload_v2")[0]["initial_comment"]


def test_search_with_no_match_says_so(setup):
    bank, store, tagger, client = setup
    _fill(bank, client)
    tagger.picks = []
    bank.handle_ask(client, "CGEN", "UZAK", "penguins")
    assert "Nothing in the vault" in client.named("chat_postMessage")[0]["text"]


def test_reply_goes_into_the_thread_it_was_asked_in(setup):
    bank, store, tagger, client = setup
    _fill(bank, client)
    bank.handle_ask(client, "CGEN", "UZAK", "#1", thread_ts="9.9")
    assert client.named("files_upload_v2")[0]["thread_ts"] == "9.9"


def test_forget_only_by_poster_or_admin(setup):
    bank, store, tagger, client = setup
    _fill(bank, client)
    bank.handle_ask(client, "CGEN", "UKEVIN", "forget #1")
    assert store.get(1) is not None and "Only whoever deposited" in client.named("chat_postMessage")[-1]["text"]
    bank.handle_ask(client, "CGEN", "UTONY", "forget #1")
    assert store.get(1) is None
    bank.handle_ask(client, "CGEN", "UADMIN", "forget #2")
    assert store.all() == []


def test_empty_vault_and_stats_and_help(setup):
    bank, store, tagger, client = setup
    bank.handle_ask(client, "CGEN", "U", "random")
    assert "vault is empty" in client.named("chat_postMessage")[-1]["text"]
    _fill(bank, client)
    bank.handle_ask(client, "CGEN", "U", "stats")
    assert "*2 memes*" in client.named("chat_postMessage")[-1]["text"]
    bank.handle_ask(client, "CGEN", "U", "help")
    assert "Deposit" in client.named("chat_postMessage")[-1]["text"]


def test_upload_failure_falls_back_to_permalink(setup):
    bank, store, tagger, client = setup
    _fill(bank, client)

    def boom(**kw):
        raise RuntimeError("missing_scope")
    client.files_upload_v2 = boom
    bank.handle_ask(client, "CGEN", "U", "#1")
    assert "https://x.slack.com/p1.0" in client.named("chat_postMessage")[-1]["text"]


# ---------------------------------------------------------------- images & keyword ranking
def test_dhash_near_for_resize_far_for_different():
    assert images.hamming(images.dhash(png(1)), images.dhash(png(1, (900, 700)))) <= 6
    assert images.hamming(images.dhash(png(1)), images.dhash(png(2))) > 10


def test_for_claude_shrinks_big_images():
    big = io.BytesIO()
    Image.new("RGB", (5000, 3000), (200, 10, 10)).save(big, "PNG")
    small, mt = images.for_claude(big.getvalue())
    with Image.open(io.BytesIO(small)) as im:
        assert max(im.size) <= images.MAX_EDGE and mt in ("image/png", "image/jpeg")


def test_keyword_rank_prefers_tags_and_multiword_tags(setup):
    bank, store, tagger, client = setup
    _fill(bank, client)
    ranked = keyword_rank("the fidel castro one", store.all())
    assert ranked[0][1].number == 1 and ranked[0][0] > ranked[1][0]
    assert search("cigars", store.all(), tagger=None)[0].number == 1  # plural folded
