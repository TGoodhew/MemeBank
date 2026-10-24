"""Where memes live: Azure (Table Storage for the catalogue + Blob Storage for images) or a local folder."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field, fields


@dataclass
class Meme:
    number: int
    source_key: str  # slack:<file id> | unfurl:<channel>:<ts>:<n> | local:<sha256>
    title: str
    description: str
    text_in_image: str
    tags: list[str] = field(default_factory=list)
    dhash: str = ""
    content_type: str = "image/png"
    blob_name: str = ""
    posted_by: str = ""  # Slack user id, or a name for local imports
    posted_at: str = ""  # ISO timestamp of the original post
    channel_id: str = ""
    message_ts: str = ""
    permalink: str = ""
    caption: str = ""  # what the poster wrote alongside it
    banked_at: str = ""
    withdrawn_at: str = ""  # set by "forget"; the row and image stay, so "restore" can undo it
    extra_tags: list[str] = field(default_factory=list)  # added by people ("tag #12 ..."); a retag keeps these

    @property
    def all_tags(self) -> list[str]:
        return list(dict.fromkeys(self.tags + self.extra_tags))

    def catalogue_line(self) -> str:
        bits = [f"#{self.number}", self.title, "tags: " + ", ".join(self.all_tags), self.description]
        if self.text_in_image:
            bits.append("text: " + self.text_in_image[:200].replace("\n", " "))
        if self.caption:
            bits.append("poster said: " + self.caption[:150].replace("\n", " "))
        return " | ".join(bits)


_FIELDS = [f.name for f in fields(Meme)]


class LocalStore:
    """SQLite + a folder of images. For trying the bot on your own PC and for tests."""

    def __init__(self, folder: str):
        self.folder = folder
        os.makedirs(os.path.join(folder, "images"), exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(os.path.join(folder, "memebank.db"), check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS memes (number INTEGER PRIMARY KEY, source_key TEXT UNIQUE, data TEXT)")
        self.db.commit()

    def next_number(self) -> int:
        with self._lock:
            (n,) = self.db.execute("SELECT COALESCE(MAX(number), 0) + 1 FROM memes").fetchone()
            return n

    def put(self, meme: Meme, image: bytes | None) -> None:
        with self._lock:
            if image is not None:
                with open(os.path.join(self.folder, "images", meme.blob_name), "wb") as f:
                    f.write(image)
            self.db.execute(
                "INSERT OR REPLACE INTO memes (number, source_key, data) VALUES (?, ?, ?)",
                (meme.number, meme.source_key, json.dumps(asdict(meme))),
            )
            self.db.commit()

    def all(self, include_withdrawn: bool = False) -> list[Meme]:
        with self._lock:
            rows = self.db.execute("SELECT data FROM memes ORDER BY number").fetchall()
        memes = [Meme(**json.loads(r[0])) for r in rows]
        return memes if include_withdrawn else [m for m in memes if not m.withdrawn_at]

    def get(self, number: int, include_withdrawn: bool = False) -> Meme | None:
        with self._lock:
            row = self.db.execute("SELECT data FROM memes WHERE number = ?", (number,)).fetchone()
        meme = Meme(**json.loads(row[0])) if row else None
        return meme if meme and (include_withdrawn or not meme.withdrawn_at) else None

    def find_by_source(self, key: str) -> Meme | None:  # withdrawn ones too, so a re-react doesn't bank a copy
        with self._lock:
            row = self.db.execute("SELECT data FROM memes WHERE source_key = ?", (key,)).fetchone()
        return Meme(**json.loads(row[0])) if row else None

    def get_image(self, meme: Meme) -> bytes:
        with open(os.path.join(self.folder, "images", meme.blob_name), "rb") as f:
            return f.read()

    def delete(self, meme: Meme) -> None:
        with self._lock:
            self.db.execute("DELETE FROM memes WHERE number = ?", (meme.number,))
            self.db.commit()
            try:
                os.remove(os.path.join(self.folder, "images", meme.blob_name))
            except FileNotFoundError:
                pass


class AzureStore:
    """Catalogue rows in an Azure Table, images in a Blob container, both in one storage account."""

    PARTITION = "meme"
    CACHE_SECONDS = 60  # pick up memes added by the backfill/import scripts running elsewhere

    def __init__(self, connection_string: str, table_name: str = "memes", container: str = "memes"):
        from azure.core.exceptions import ResourceExistsError
        from azure.data.tables import TableServiceClient
        from azure.storage.blob import BlobServiceClient

        self.table = TableServiceClient.from_connection_string(connection_string).create_table_if_not_exists(table_name)
        self.blobs = BlobServiceClient.from_connection_string(connection_string).get_container_client(container)
        try:
            self.blobs.create_container()
        except ResourceExistsError:
            pass
        self._lock = threading.RLock()
        self._cache: list[Meme] | None = None
        self._cache_at = 0.0

    # --- numbering: a counter row updated with optimistic concurrency so two processes can't share a number
    def next_number(self) -> int:
        from azure.core import MatchConditions
        from azure.core.exceptions import ResourceExistsError, ResourceModifiedError, ResourceNotFoundError
        from azure.data.tables import UpdateMode

        for _ in range(20):
            try:
                row = self.table.get_entity("meta", "counter")
            except ResourceNotFoundError:
                start = max((m.number for m in self.all(refresh=True, include_withdrawn=True)), default=0)
                try:
                    self.table.create_entity({"PartitionKey": "meta", "RowKey": "counter", "value": start})
                except ResourceExistsError:
                    pass
                continue
            n = int(row["value"]) + 1
            try:
                self.table.update_entity(
                    {"PartitionKey": "meta", "RowKey": "counter", "value": n},
                    mode=UpdateMode.REPLACE, etag=row.metadata["etag"], match_condition=MatchConditions.IfNotModified,
                )
                return n
            except ResourceModifiedError:
                time.sleep(0.2)
        raise RuntimeError("Couldn't allocate a meme number")

    @classmethod
    def _to_entity(cls, m: Meme) -> dict:
        d = asdict(m)
        d["tags"] = json.dumps(m.tags)
        d["extra_tags"] = json.dumps(m.extra_tags)
        d.update(PartitionKey=cls.PARTITION, RowKey=f"{m.number:07d}")
        return d

    @staticmethod
    def _from_entity(e) -> Meme:
        d = {k: e.get(k) for k in _FIELDS if k in e}
        d["tags"] = json.loads(d.get("tags") or "[]")
        if "extra_tags" in d:  # rows from before manual tags don't have the column
            d["extra_tags"] = json.loads(d["extra_tags"] or "[]")
        d["number"] = int(d["number"])
        return Meme(**{k: ("" if v is None else v) for k, v in d.items()})

    def put(self, meme: Meme, image: bytes | None) -> None:
        from azure.data.tables import UpdateMode
        from azure.storage.blob import ContentSettings

        if image is not None:
            self.blobs.upload_blob(meme.blob_name, image, overwrite=True,
                                   content_settings=ContentSettings(content_type=meme.content_type))
        self.table.upsert_entity(self._to_entity(meme), mode=UpdateMode.REPLACE)
        with self._lock:
            self._cache = None

    def all(self, refresh: bool = False, include_withdrawn: bool = False) -> list[Meme]:
        with self._lock:
            if refresh or self._cache is None or time.time() - self._cache_at > self.CACHE_SECONDS:
                rows = self.table.query_entities(f"PartitionKey eq '{self.PARTITION}'")
                self._cache = sorted((self._from_entity(r) for r in rows), key=lambda m: m.number)
                self._cache_at = time.time()
            return [m for m in self._cache if include_withdrawn or not m.withdrawn_at]

    def get(self, number: int, include_withdrawn: bool = False) -> Meme | None:
        return next((m for m in self.all(include_withdrawn=include_withdrawn) if m.number == number), None) or \
            next((m for m in self.all(refresh=True, include_withdrawn=include_withdrawn) if m.number == number), None)

    def find_by_source(self, key: str) -> Meme | None:  # withdrawn ones too, so a re-react doesn't bank a copy
        return next((m for m in self.all(refresh=True, include_withdrawn=True) if m.source_key == key), None)

    def get_image(self, meme: Meme) -> bytes:
        return self.blobs.download_blob(meme.blob_name).readall()

    def delete(self, meme: Meme) -> None:
        from azure.core.exceptions import ResourceNotFoundError

        self.table.delete_entity(self.PARTITION, f"{meme.number:07d}")
        try:
            self.blobs.delete_blob(meme.blob_name)
        except ResourceNotFoundError:
            pass
        with self._lock:
            self._cache = None
