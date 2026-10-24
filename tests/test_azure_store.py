"""AzureStore against Azurite (the local Azure Storage emulator). Skipped unless AZURITE=1.

    npx azurite --silent --inMemoryPersistence --skipApiVersionCheck --loose   then (in another terminal)
    AZURITE=1 pytest tests/test_azure_store.py
"""
import os
import threading
import uuid

import pytest

from memebank.store import AzureStore, Meme

pytestmark = pytest.mark.skipif(os.environ.get("AZURITE") != "1", reason="set AZURITE=1 with azurite running")

CONN = ("DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;"
        "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;"
        "BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;TableEndpoint=http://127.0.0.1:10002/devstoreaccount1;")


@pytest.fixture
def store():
    suffix = uuid.uuid4().hex[:8]
    return AzureStore(CONN, table_name=f"memes{suffix}", container=f"memes-{suffix}")


def meme(n, key):
    return Meme(number=n, source_key=key, title=f"Meme {n}", description="d", text_in_image="",
                tags=["cuba", "fidel castro"], dhash="0f" * 8, content_type="image/png", blob_name=f"{n:06d}.png",
                posted_by="UTONY", caption="a meme for the meme bank")


def test_round_trip_and_delete(store):
    n = store.next_number()
    assert n == 1
    store.put(meme(n, "slack:F1"), b"\x89PNG fake")
    got = store.get(1)
    assert got.tags == ["cuba", "fidel castro"] and got.caption == "a meme for the meme bank"
    assert store.find_by_source("slack:F1").number == 1 and store.find_by_source("slack:nope") is None
    assert store.get_image(got) == b"\x89PNG fake"
    got.title = "Retitled"
    got.extra_tags = ["ilhan omar", "squad"]
    store.put(got, None)
    assert store.get(1).title == "Retitled" and store.get_image(store.get(1)) == b"\x89PNG fake"
    assert store.all(refresh=True)[0].extra_tags == ["ilhan omar", "squad"]
    store.delete(got)
    assert store.all(refresh=True) == []


def test_withdrawn_memes_are_hidden_but_kept(store):
    store.put(meme(store.next_number(), "slack:F1"), b"one")
    store.put(meme(store.next_number(), "slack:F2"), b"two")
    m = store.get(1)
    m.withdrawn_at = "2026-10-24T18:00:00+00:00"
    store.put(m, None)
    assert [x.number for x in store.all(refresh=True)] == [2] and store.get(1) is None
    assert store.get(1, include_withdrawn=True).withdrawn_at and store.find_by_source("slack:F1").number == 1
    assert store.get_image(store.get(1, include_withdrawn=True)) == b"one"
    assert store.next_number() == 3  # never reused


def test_memes_added_by_another_process_show_up_after_the_cache_expires(store):
    backfill = AzureStore(CONN, table_name=store.table.table_name, container=store.blobs.container_name)
    depositor = AzureStore(CONN, table_name=store.table.table_name, container=store.blobs.container_name)
    assert store.all() == [] and depositor.all() == []  # both now cached
    backfill.put(meme(backfill.next_number(), "slack:F9"), b"nine")
    assert store.all() == []  # still the cached view...
    # ...but deposits check fresh, so no double-banking. (A separate store, so `store` keeps its stale cache.)
    assert depositor.find_by_source("slack:F9").number == 1
    assert store.all() == []
    store._cache_at -= AzureStore.CACHE_SECONDS + 1  # a minute later
    assert [m.number for m in store.all()] == [1]


def test_numbers_are_unique_under_concurrency(store):
    other = AzureStore(CONN, table_name=store.table.table_name, container=store.blobs.container_name)
    got, lock = [], threading.Lock()

    def grab(s):
        for _ in range(10):
            n = s.next_number()
            with lock:
                got.append(n)

    ts = [threading.Thread(target=grab, args=(s,)) for s in (store, other, store, other)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(got) == list(range(1, 41))
