import io
import json
import random

import pytest
from PIL import Image, ImageDraw, ImageFont

from memebank import images
from memebank.core import MemeBank, images_in, same_text
from memebank.search import keyword_rank, search
from memebank.store import LocalStore
from memebank.tagger import Tags
from memebank.triggers import classify, parse_command, split_tags

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
    ("restore #4", "restore", 4, ""),
    ("undo forget 4", "restore", 4, ""),
    ("bring back meme #4", "restore", 4, ""),
    ("retag 9", "retag", 9, ""),
    ("tag #12 ilhan omar, squad", "tag", 12, "ilhan omar, squad"),
    ("add tags to #3: Cabin Trip", "tag", 3, "Cabin Trip"),
    ("tag meme 5 with kevin", "tag", 5, "kevin"),
    ("untag #12 squad", "untag", 12, "squad"),
    ("tag #12", "tag", 12, ""),  # not "tag #1 with 2"
    ("untag #12", "untag", 12, ""),
    ('Tag it "Trump screamer" and "Beanie Girl"', "tag", None, '"Trump screamer" and "Beanie Girl"'),
    ("tag this one with cabin trip", "tag", None, "cabin trip"),
    ("add tags: kevin", "tag", None, "kevin"),
    ("tag #12 without context", "tag", 12, "without context"),
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
    assert client.named("reactions_add")
    assert [kw["channel"] for kw in client.named("chat_postMessage")] == ["UTONY"]  # nothing in the channel; the tag DM still goes


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


def test_forget_hides_and_restore_brings_back(setup):
    bank, store, tagger, client = setup
    _fill(bank, client)
    say = lambda: client.named("chat_postMessage")[-1]["text"]  # noqa: E731
    bank.handle_ask(client, "CGEN", "UTONY", "forget #1")
    assert "restore #1" in say()
    hidden = store.get(1, include_withdrawn=True)
    assert hidden.withdrawn_at and store.get_image(hidden) == png(1)  # still stored, image and all
    bank.handle_ask(client, "CGEN", "U", "stats")
    assert "*1 memes*" in say()
    bank.handle_ask(client, "CGEN", "U", "#1")
    assert "was withdrawn" in say() and not client.named("files_upload_v2")
    bank.handle_ask(client, "CGEN", "UKEVIN", "restore #1")
    assert "Only whoever deposited" in say() and store.get(1) is None
    bank.handle_ask(client, "CGEN", "UTONY", "restore #1")
    assert "back in the vault" in say() and store.get(1).title == "Fidel island socialism"
    bank.handle_ask(client, "CGEN", "UTONY", "restore #1")
    assert "already in the vault" in say()


def test_restore_works_even_when_everything_was_forgotten(setup):
    bank, store, tagger, client = setup
    _fill(bank, client)
    bank.handle_ask(client, "CGEN", "UADMIN", "forget #1")
    bank.handle_ask(client, "CGEN", "UADMIN", "forget #2")
    bank.handle_ask(client, "CGEN", "UADMIN", "restore #2")
    assert [m.number for m in store.all()] == [2]


def test_forgotten_numbers_are_never_reused(setup):
    bank, store, tagger, client = setup
    _fill(bank, client)
    bank.handle_ask(client, "CGEN", "UKEVIN", "forget #2")
    bank.deposit_message(client, "CGEN", slack_msg("3.0", "meme bank", "F3"))
    assert [m.number for m in store.all()] == [1, 3]


def test_reacting_to_a_forgotten_post_offers_restore_instead_of_a_copy(setup):
    bank, store, tagger, client = setup
    msg = slack_msg("100.1", "a meme for the meme bank", "F1")
    bank.deposit_message(client, "CGEN", msg)
    bank.handle_ask(client, "CGEN", "UTONY", "forget #1")
    res = bank.deposit_message(client, "CGEN", msg)
    assert [r.status for r in res] == ["withdrawn"] and len(store.all(include_withdrawn=True)) == 1
    assert "restore #1" in client.named("chat_postMessage")[-1]["text"]


def test_export_writes_catalogue_and_images_including_withdrawn(setup, tmp_path):
    from scripts.export import export

    bank, store, tagger, client = setup
    _fill(bank, client)
    bank.handle_ask(client, "CGEN", "UTONY", "forget #1")
    out = tmp_path / "backup"
    assert export(store, str(out)) == {"memes": 2, "withdrawn": 1, "downloaded": 2, "kept": 0}
    rows = json.loads((out / "memes.json").read_text(encoding="utf-8"))
    assert [(r["number"], bool(r["withdrawn_at"])) for r in rows] == [(1, True), (2, False)]
    assert (out / "images" / rows[1]["blob_name"]).read_bytes() == png(2)
    assert export(store, str(out))["kept"] == 2  # a re-run only fetches what's new


def test_failed_export_leaves_no_stub_and_a_retry_completes_it(setup, tmp_path, monkeypatch):
    from scripts.export import export

    bank, store, tagger, client = setup
    _fill(bank, client)
    real = store.get_image
    monkeypatch.setattr(store, "get_image", lambda m: (_ for _ in ()).throw(OSError("network")) if m.number == 2 else real(m))
    out = tmp_path / "backup"  # not tmp_path itself: that's where the LocalStore keeps its own images
    with pytest.raises(OSError):
        export(store, str(out))
    assert sorted(p.name for p in (out / "images").iterdir()) == [store.get(1).blob_name]  # no empty #2, no .part
    monkeypatch.setattr(store, "get_image", real)
    assert export(store, str(out)) == {"memes": 2, "withdrawn": 0, "downloaded": 1, "kept": 1}


def test_anyone_can_add_tags_and_they_survive_a_retag(setup):
    bank, store, tagger, client = setup
    _fill(bank, client)
    say = lambda: client.named("chat_postMessage")[-1]["text"]  # noqa: E731
    bank.handle_ask(client, "CGEN", "UZAK", "tag #1 Ilhan Omar, #Squad, cuba")  # cuba is already one of Claude's
    assert "with _ilhan omar, squad_" in say()
    m = store.get(1)
    assert m.extra_tags == ["ilhan omar", "squad"] and m.all_tags[-2:] == ["ilhan omar", "squad"]
    bank.handle_ask(client, "CGEN", "UZAK", "tag #1 squad")
    assert "already has those tags" in say()
    bank.handle_ask(client, "CGEN", "UTONY", "retag #1")
    assert "kept the tags people added: ilhan omar, squad" in say() and store.get(1).extra_tags == ["ilhan omar", "squad"]
    assert search("the ilhan omar one", store.all(), tagger=None)[0].number == 1  # keyword fallback sees them
    assert "ilhan omar" in store.get(1).catalogue_line()  # and so does Claude's pick


@pytest.mark.parametrize("text,tags", [
    ('"Trump screamer" and "Beanie Girl"', ["Trump screamer", "Beanie Girl"]),
    ("thomas sowell quote card, moral superiority", ["thomas sowell quote card", "moral superiority"]),
    ('cabin, "rock and roll", kevin & zak', ["cabin", "rock and roll", "kevin", "zak"]),
    ("“curly quotes” and plain", ["curly quotes", "plain"]),
    ("brandon; candy", ["brandon", "candy"]),
    ("kevin &amp; zak", ["kevin", "zak"]),  # Slack sends a typed & as &amp;
    ('"mac &amp; cheese", &lt;3', ["mac & cheese", "<3"]),
    ("", []),
])
def test_split_tags(text, tags):
    assert split_tags(text) == tags


def test_tag_reply_names_the_tags_that_were_already_there(setup):
    bank, store, tagger, client = setup
    _fill(bank, client)
    bank.handle_ask(client, "CGEN", "UZAK", 'tag #1 "Trump screamer" and cuba')  # cuba is one of Claude's
    text = client.named("chat_postMessage")[-1]["text"]
    assert "with _trump screamer_ (already had: cuba)" in text and store.get(1).extra_tags == ["trump screamer"]


def test_tag_it_without_a_number_outside_a_dm_asks_which_meme(setup):
    bank, store, tagger, client = setup
    _fill(bank, client)
    bank.handle_ask(client, "CGEN", "UZAK", "tag it cabin trip")
    assert "Which meme?" in client.named("chat_postMessage")[-1]["text"]
    assert not store.get(1).extra_tags and not store.get(2).extra_tags


def test_tag_with_no_words_asks_for_some_and_changes_nothing(setup):
    bank, store, tagger, client = setup
    _fill(bank, client)
    before = (store.get(1).all_tags, store.get(2).all_tags)
    bank.handle_ask(client, "CGEN", "UTONY", "tag #2")
    assert "Give me some words to tag #2" in client.named("chat_postMessage")[-1]["text"]
    bank.handle_ask(client, "CGEN", "UTONY", "untag #1")
    assert "Which tag should I remove from #1" in client.named("chat_postMessage")[-1]["text"]
    assert (store.get(1).all_tags, store.get(2).all_tags) == before


def test_more_than_forty_added_tags_are_all_kept_up_to_the_limit(setup):
    from memebank.core import MAX_EXTRA_TAGS

    bank, store, tagger, client = setup
    _fill(bank, client)
    for start in range(0, MAX_EXTRA_TAGS, 25):
        bank.handle_ask(client, "CGEN", "UZAK", "tag #1 " + ", ".join(f"word{i}" for i in range(start, start + 25)))
    assert len(store.get(1).extra_tags) == MAX_EXTRA_TAGS and "word99" in store.get(1).all_tags
    bank.handle_ask(client, "CGEN", "UZAK", "tag #1 one more")
    assert "can't take more added tags (1 skipped" in client.named("chat_postMessage")[-1]["text"]


def test_reaction_only_mode_still_gives_a_dm_deposit_its_tag_hint(setup):
    bank, store, tagger, client = setup
    bank.ack_mode = "reaction"
    bank.deposit_message(client, "DTONY", slack_msg("100.1", "", "F1", user="UTONY"))
    posts = client.named("chat_postMessage")
    assert len(posts) == 1 and posts[0]["channel"] == "DTONY" and "Here are the tags I used" in posts[0]["text"]


def test_only_poster_or_admin_can_untag(setup):
    bank, store, tagger, client = setup
    _fill(bank, client)
    bank.handle_ask(client, "CGEN", "UZAK", "tag #1 squad")
    bank.handle_ask(client, "CGEN", "UZAK", "untag #1 squad")
    assert "Only whoever deposited" in client.named("chat_postMessage")[-1]["text"] and "squad" in store.get(1).all_tags
    bank.handle_ask(client, "CGEN", "UTONY", "untag #1 squad, cigar")
    assert "squad" not in store.get(1).all_tags and "cigar" not in store.get(1).all_tags


def test_new_deposit_dms_the_depositor_the_tags_and_how_to_add_more(setup):
    bank, store, tagger, client = setup
    bank.deposit_message(client, "CGEN", slack_msg("100.1", "castro: a meme for the meme bank", "F1", user="UTONY"))
    dm = [kw for kw in client.named("chat_postMessage") if kw["channel"] == "UTONY"]
    assert len(dm) == 1 and "Banked *#1: Fidel island socialism*" in dm[0]["text"]
    assert "fidel castro, cuba, socialism, cigar" in dm[0]["text"] and "tag #1" in dm[0]["text"]
    client.calls.clear()
    res = bank.deposit_message(client, "CGEN", slack_msg("200.1", "castro again, meme bank", "F1RESIZED", user="UTONY"))
    assert res[0].status == "duplicate"
    assert not [kw for kw in client.named("chat_postMessage") if kw["channel"] == "UTONY"]  # duplicates: no DM


def test_reaction_deposit_dms_whoever_reacted(setup):
    bank, store, tagger, client = setup
    bank.deposit_message(client, "CGEN", slack_msg("100.1", "lol", "F1", user="UKEVIN"), banked_by="UTONY")
    assert [kw["channel"] for kw in client.named("chat_postMessage")] == ["CGEN", "UTONY"]


def test_no_tag_dm_for_backfill_or_when_turned_off(setup):
    bank, store, tagger, client = setup
    bank.deposit_message(client, "CGEN", slack_msg("1.0", "meme bank", "F1"), acknowledge=False)
    bank.dm_tags = False
    bank.deposit_message(client, "CGEN", slack_msg("2.0", "meme bank", "F2"))
    assert [kw["channel"] for kw in client.named("chat_postMessage")] == ["CGEN"]  # just the thread reply for #2


def test_dm_deposit_gets_the_tag_hint_in_its_own_thread(setup):
    bank, store, tagger, client = setup
    bank.deposit_message(client, "DTONY", slack_msg("100.1", "", "F1", user="UTONY"))
    posts = client.named("chat_postMessage")
    assert len(posts) == 1 and posts[0]["channel"] == "DTONY" and posts[0]["thread_ts"] == "100.1"
    assert "Here are the tags I used" in posts[0]["text"] and "tag #1" in posts[0]["text"]


def test_empty_vault_and_stats_and_help(setup):
    bank, store, tagger, client = setup
    bank.handle_ask(client, "CGEN", "U", "random")
    assert "vault is empty" in client.named("chat_postMessage")[-1]["text"]
    _fill(bank, client)
    bank.handle_ask(client, "CGEN", "U", "stats")
    assert "*2 memes*" in client.named("chat_postMessage")[-1]["text"]
    bank.handle_ask(client, "CGEN", "U", "help")
    help_text = client.named("chat_postMessage")[-1]["text"]
    assert "Deposit" in help_text and "retag #12" in help_text and "DM me an image" in help_text


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
