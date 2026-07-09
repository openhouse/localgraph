from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Any


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def upsert_source_import(
    db: sqlite3.Connection,
    *,
    source_kind: str,
    source_identifier: str,
    source_path: str | None,
    raw_metadata: Mapping[str, Any] | None = None,
) -> int:
    db.execute(
        """
        INSERT INTO source_imports (source_kind, source_identifier, source_path, raw_metadata_json)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(source_identifier) DO UPDATE SET
          source_kind = excluded.source_kind,
          source_path = excluded.source_path,
          raw_metadata_json = excluded.raw_metadata_json,
          imported_at = CURRENT_TIMESTAMP
        """,
        (source_kind, source_identifier, source_path, json_dumps(raw_metadata or {})),
    )
    return _lookup_id(db, "source_imports", "source_identifier", source_identifier)


def upsert_identity(db: sqlite3.Connection, *, stable_key: str, display_name: str, kind: str) -> int:
    db.execute(
        """
        INSERT INTO identities (stable_key, display_name, kind)
        VALUES (?, ?, ?)
        ON CONFLICT(stable_key) DO UPDATE SET
          display_name = excluded.display_name,
          kind = excluded.kind,
          updated_at = CURRENT_TIMESTAMP
        """,
        (stable_key, display_name or "Unknown", kind),
    )
    return _lookup_id(db, "identities", "stable_key", stable_key)


def upsert_account(
    db: sqlite3.Connection,
    *,
    identity_id: int,
    source_kind: str,
    account_key: str,
    display_name: str,
    profile_url: str | None = None,
    raw_metadata: Mapping[str, Any] | None = None,
) -> int:
    db.execute(
        """
        INSERT INTO accounts (identity_id, source_kind, account_key, display_name, profile_url, raw_metadata_json)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_kind, account_key) DO UPDATE SET
          identity_id = excluded.identity_id,
          display_name = excluded.display_name,
          profile_url = excluded.profile_url,
          raw_metadata_json = excluded.raw_metadata_json
        """,
        (identity_id, source_kind, account_key, display_name or "Unknown", profile_url, json_dumps(raw_metadata or {})),
    )
    row = db.execute(
        "SELECT id FROM accounts WHERE source_kind = ? AND account_key = ?",
        (source_kind, account_key),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"account upsert failed: {source_kind}:{account_key}")
    return int(row["id"])


def upsert_thread(
    db: sqlite3.Connection,
    *,
    source_kind: str,
    source_thread_key: str,
    title: str,
    thread_kind: str,
    first_message_at: str | None,
    last_message_at: str | None,
    raw_metadata: Mapping[str, Any] | None = None,
) -> int:
    db.execute(
        """
        INSERT INTO threads (
          source_kind,
          source_thread_key,
          title,
          thread_kind,
          first_message_at,
          last_message_at,
          raw_metadata_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_kind, source_thread_key) DO UPDATE SET
          title = excluded.title,
          thread_kind = excluded.thread_kind,
          first_message_at = COALESCE(
            MIN(COALESCE(threads.first_message_at, excluded.first_message_at), COALESCE(excluded.first_message_at, threads.first_message_at)),
            threads.first_message_at,
            excluded.first_message_at
          ),
          last_message_at = COALESCE(
            MAX(COALESCE(threads.last_message_at, excluded.last_message_at), COALESCE(excluded.last_message_at, threads.last_message_at)),
            threads.last_message_at,
            excluded.last_message_at
          ),
          raw_metadata_json = excluded.raw_metadata_json
        """,
        (
            source_kind,
            source_thread_key,
            title or "Untitled conversation",
            thread_kind,
            first_message_at,
            last_message_at,
            json_dumps(raw_metadata or {}),
        ),
    )
    row = db.execute(
        "SELECT id FROM threads WHERE source_kind = ? AND source_thread_key = ?",
        (source_kind, source_thread_key),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"thread upsert failed: {source_kind}:{source_thread_key}")
    return int(row["id"])


def upsert_thread_participant(
    db: sqlite3.Connection,
    *,
    thread_id: int,
    identity_id: int | None,
    account_id: int | None,
    role: str = "participant",
) -> None:
    db.execute(
        """
        INSERT OR IGNORE INTO thread_participants (thread_id, identity_id, account_id, role)
        VALUES (?, ?, ?, ?)
        """,
        (thread_id, identity_id, account_id, role),
    )


def upsert_message(
    db: sqlite3.Connection,
    *,
    thread_id: int,
    source_message_key: str,
    sender_identity_id: int | None,
    sender_account_id: int | None,
    sent_at: str,
    body_text: str | None,
    body_format: str,
    raw: Mapping[str, Any] | None = None,
) -> int:
    db.execute(
        """
        INSERT INTO messages (
          thread_id,
          source_message_key,
          sender_identity_id,
          sender_account_id,
          sent_at,
          body_text,
          body_format,
          raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(thread_id, source_message_key) DO UPDATE SET
          sender_identity_id = excluded.sender_identity_id,
          sender_account_id = excluded.sender_account_id,
          sent_at = excluded.sent_at,
          body_text = excluded.body_text,
          body_format = excluded.body_format,
          raw_json = excluded.raw_json
        """,
        (thread_id, source_message_key, sender_identity_id, sender_account_id, sent_at, body_text, body_format, json_dumps(raw or {})),
    )
    row = db.execute(
        "SELECT id FROM messages WHERE thread_id = ? AND source_message_key = ?",
        (thread_id, source_message_key),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"message upsert failed: {source_message_key}")
    return int(row["id"])


def upsert_media_object(
    db: sqlite3.Connection,
    *,
    message_id: int,
    object_key: str,
    source_uri: str | None,
    local_path: str | None = None,
    mime_type: str | None = None,
    checksum: str | None = None,
    raw_metadata: Mapping[str, Any] | None = None,
) -> int:
    db.execute(
        """
        INSERT INTO media_objects (
          message_id,
          object_key,
          source_uri,
          local_path,
          mime_type,
          checksum,
          raw_metadata_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(object_key) DO UPDATE SET
          message_id = excluded.message_id,
          source_uri = excluded.source_uri,
          local_path = excluded.local_path,
          mime_type = excluded.mime_type,
          checksum = excluded.checksum,
          raw_metadata_json = excluded.raw_metadata_json
        """,
        (message_id, object_key, source_uri, local_path, mime_type, checksum, json_dumps(raw_metadata or {})),
    )
    return _lookup_id(db, "media_objects", "object_key", object_key)


def upsert_graph_edge(
    db: sqlite3.Connection,
    *,
    from_kind: str,
    from_key: str,
    edge_kind: str,
    to_kind: str,
    to_key: str,
    source: str = "derived",
) -> None:
    db.execute(
        """
        INSERT OR IGNORE INTO graph_edges (from_kind, from_key, edge_kind, to_kind, to_key, source)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (from_kind, from_key, edge_kind, to_kind, to_key, source),
    )


def _lookup_id(db: sqlite3.Connection, table: str, column: str, value: str) -> int:
    row = db.execute(f"SELECT id FROM {table} WHERE {column} = ?", (value,)).fetchone()
    if row is None:
        raise RuntimeError(f"{table} lookup failed: {column}={value}")
    return int(row["id"])
