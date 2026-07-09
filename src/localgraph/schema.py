from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Iterator
import sqlite3
from pathlib import Path


@contextmanager
def connect(path: Path) -> Iterator[sqlite3.Connection]:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA journal_mode = WAL")
    try:
        yield db
        db.commit()
    finally:
        db.close()


def initialize_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS source_imports (
          id INTEGER PRIMARY KEY,
          source_kind TEXT NOT NULL,
          source_identifier TEXT NOT NULL UNIQUE,
          source_path TEXT,
          imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          raw_metadata_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS source_locations (
          id INTEGER PRIMARY KEY,
          source_kind TEXT NOT NULL,
          location_kind TEXT NOT NULL,
          label TEXT NOT NULL,
          local_path TEXT,
          options_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          UNIQUE(source_kind, location_kind, label)
        );

        CREATE TABLE IF NOT EXISTS import_runs (
          id INTEGER PRIMARY KEY,
          run_kind TEXT NOT NULL,
          source_kind TEXT NOT NULL,
          status TEXT NOT NULL CHECK (status IN ('started', 'completed', 'pending', 'failed')),
          started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          finished_at TEXT,
          summary_json TEXT NOT NULL DEFAULT '{}',
          error_text TEXT
        );

        CREATE TABLE IF NOT EXISTS pending_imports (
          id INTEGER PRIMARY KEY,
          source_kind TEXT NOT NULL,
          source_identifier TEXT NOT NULL,
          source_path TEXT,
          reason TEXT NOT NULL,
          detected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          resolved_at TEXT,
          raw_metadata_json TEXT NOT NULL DEFAULT '{}',
          UNIQUE(source_kind, source_identifier, reason)
        );

        CREATE TABLE IF NOT EXISTS identities (
          id INTEGER PRIMARY KEY,
          stable_key TEXT NOT NULL UNIQUE,
          display_name TEXT NOT NULL,
          kind TEXT NOT NULL CHECK (kind IN ('person', 'group', 'organization', 'unknown')),
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS accounts (
          id INTEGER PRIMARY KEY,
          identity_id INTEGER REFERENCES identities(id) ON DELETE SET NULL,
          source_kind TEXT NOT NULL,
          account_key TEXT NOT NULL,
          display_name TEXT NOT NULL,
          profile_url TEXT,
          raw_metadata_json TEXT NOT NULL DEFAULT '{}',
          UNIQUE(source_kind, account_key)
        );

        CREATE TABLE IF NOT EXISTS threads (
          id INTEGER PRIMARY KEY,
          source_kind TEXT NOT NULL,
          source_thread_key TEXT NOT NULL,
          title TEXT NOT NULL,
          thread_kind TEXT NOT NULL CHECK (thread_kind IN ('direct', 'group', 'channel', 'unknown')),
          first_message_at TEXT,
          last_message_at TEXT,
          raw_metadata_json TEXT NOT NULL DEFAULT '{}',
          UNIQUE(source_kind, source_thread_key)
        );

        CREATE TABLE IF NOT EXISTS thread_participants (
          thread_id INTEGER NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
          identity_id INTEGER REFERENCES identities(id) ON DELETE SET NULL,
          account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
          role TEXT NOT NULL DEFAULT 'participant',
          PRIMARY KEY(thread_id, account_id, role)
        );

        CREATE TABLE IF NOT EXISTS messages (
          id INTEGER PRIMARY KEY,
          thread_id INTEGER NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
          source_message_key TEXT NOT NULL,
          sender_identity_id INTEGER REFERENCES identities(id) ON DELETE SET NULL,
          sender_account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
          sent_at TEXT NOT NULL,
          body_text TEXT,
          body_format TEXT NOT NULL DEFAULT 'plain',
          raw_json TEXT NOT NULL DEFAULT '{}',
          UNIQUE(thread_id, source_message_key)
        );

        CREATE TABLE IF NOT EXISTS media_objects (
          id INTEGER PRIMARY KEY,
          message_id INTEGER REFERENCES messages(id) ON DELETE CASCADE,
          object_key TEXT NOT NULL UNIQUE,
          source_uri TEXT,
          local_path TEXT,
          mime_type TEXT,
          checksum TEXT,
          raw_metadata_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS annotations (
          id INTEGER PRIMARY KEY,
          target_kind TEXT NOT NULL,
          target_key TEXT NOT NULL,
          body_markdown TEXT NOT NULL,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS graph_edges (
          id INTEGER PRIMARY KEY,
          from_kind TEXT NOT NULL,
          from_key TEXT NOT NULL,
          edge_kind TEXT NOT NULL,
          to_kind TEXT NOT NULL,
          to_key TEXT NOT NULL,
          source TEXT NOT NULL DEFAULT 'derived',
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          UNIQUE(from_kind, from_key, edge_kind, to_kind, to_key, source)
        );
        """
    )
    db.commit()
