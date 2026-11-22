"""SQLite persistence.

One connection per request; writes use ``BEGIN IMMEDIATE`` so that the
progress check and the segment insert form a single serialised transaction.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

DEFAULT_DB_PATH = os.environ.get("HANDOVER_DB", "handover.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS trails (
    id          TEXT PRIMARY KEY,
    name        TEXT,
    created_at  TEXT NOT NULL,
    error_limit TEXT NOT NULL,
    point_limit INTEGER NOT NULL,
    samples     TEXT NOT NULL,
    zones       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS segments (
    trail_id             TEXT NOT NULL REFERENCES trails(id),
    seq                  INTEGER NOT NULL,
    start_index          INTEGER NOT NULL,
    end_index            INTEGER NOT NULL,
    observed_progress    INTEGER NOT NULL,
    required_pins        TEXT NOT NULL,
    points               TEXT NOT NULL,
    point_count          INTEGER NOT NULL,
    new_point_count      INTEGER NOT NULL,
    total_points_used    INTEGER NOT NULL,
    remaining_minimum_points INTEGER NOT NULL,
    remaining_new_points INTEGER NOT NULL,
    minimum_total_points INTEGER NOT NULL,
    point_limit          INTEGER NOT NULL,
    created_at           TEXT NOT NULL,
    PRIMARY KEY (trail_id, seq)
);

-- Issued segments are archival: no update, no delete, ever.
CREATE TRIGGER IF NOT EXISTS segments_no_update
BEFORE UPDATE ON segments
BEGIN
    SELECT RAISE(ABORT, 'issued segments are immutable');
END;

CREATE TRIGGER IF NOT EXISTS segments_no_delete
BEFORE DELETE ON segments
BEGIN
    SELECT RAISE(ABORT, 'issued segments are immutable');
END;
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_trail_id() -> str:
    return uuid.uuid4().hex


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def write_transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")


def encode(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
