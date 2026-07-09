from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass
class ImportStats:
    source_kind: str
    source_path: str
    imports: int = 0
    people: int = 0
    groups: int = 0
    threads: int = 0
    messages: int = 0
    media_objects: int = 0
    skipped: bool = False
    note: str | None = None

    def to_json(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "sourceKind": self.source_kind,
            "sourcePath": self.source_path,
            "imports": self.imports,
            "people": self.people,
            "groups": self.groups,
            "threads": self.threads,
            "messages": self.messages,
            "mediaObjects": self.media_objects,
            "skipped": self.skipped,
        }
        if self.note:
            payload["note"] = self.note
        return payload


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def set_source_location(
    db: sqlite3.Connection,
    *,
    source_kind: str,
    location_kind: str,
    label: str,
    local_path: str | None,
    options: dict[str, object] | None = None,
) -> int:
    db.execute(
        """
        INSERT INTO source_locations (source_kind, location_kind, label, local_path, options_json)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(source_kind, location_kind, label) DO UPDATE SET
          local_path = excluded.local_path,
          options_json = excluded.options_json,
          updated_at = CURRENT_TIMESTAMP
        """,
        (source_kind, location_kind, label, local_path, json_dumps(options or {})),
    )
    return int(
        db.execute(
            """
            SELECT id FROM source_locations
            WHERE source_kind = ? AND location_kind = ? AND label = ?
            """,
            (source_kind, location_kind, label),
        ).fetchone()["id"]
    )


def get_source_location(
    db: sqlite3.Connection,
    *,
    source_kind: str,
    location_kind: str,
    label: str,
) -> sqlite3.Row | None:
    return db.execute(
        """
        SELECT * FROM source_locations
        WHERE source_kind = ? AND location_kind = ? AND label = ?
        """,
        (source_kind, location_kind, label),
    ).fetchone()


def start_import_run(db: sqlite3.Connection, *, run_kind: str, source_kind: str) -> int:
    cursor = db.execute(
        """
        INSERT INTO import_runs (run_kind, source_kind, status, summary_json)
        VALUES (?, ?, 'started', '{}')
        """,
        (run_kind, source_kind),
    )
    return int(cursor.lastrowid)


def finish_import_run(
    db: sqlite3.Connection,
    *,
    run_id: int,
    status: str,
    summary: dict[str, object] | None = None,
    error_text: str | None = None,
) -> None:
    db.execute(
        """
        UPDATE import_runs
        SET status = ?,
            finished_at = ?,
            summary_json = ?,
            error_text = ?
        WHERE id = ?
        """,
        (status, utc_now(), json_dumps(summary or {}), error_text, run_id),
    )


def has_completed_import_run(db: sqlite3.Connection, *, run_kind: str, source_kind: str) -> bool:
    return bool(
        db.execute(
            """
            SELECT id FROM import_runs
            WHERE run_kind = ? AND source_kind = ? AND status = 'completed'
            LIMIT 1
            """,
            (run_kind, source_kind),
        ).fetchone()
    )


def record_pending_import(
    db: sqlite3.Connection,
    *,
    source_kind: str,
    source_identifier: str,
    source_path: str | None,
    reason: str,
    raw_metadata: dict[str, object] | None = None,
) -> int:
    db.execute(
        """
        INSERT INTO pending_imports (source_kind, source_identifier, source_path, reason, raw_metadata_json)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(source_kind, source_identifier, reason) DO UPDATE SET
          source_path = excluded.source_path,
          detected_at = CURRENT_TIMESTAMP,
          resolved_at = NULL,
          raw_metadata_json = excluded.raw_metadata_json
        """,
        (source_kind, source_identifier, source_path, reason, json_dumps(raw_metadata or {})),
    )
    return int(
        db.execute(
            """
            SELECT id FROM pending_imports
            WHERE source_kind = ? AND source_identifier = ? AND reason = ?
            """,
            (source_kind, source_identifier, reason),
        ).fetchone()["id"]
    )


def resolve_pending_imports(db: sqlite3.Connection, *, source_kind: str, source_identifier: str | None = None) -> int:
    if source_identifier is None:
        cursor = db.execute(
            """
            UPDATE pending_imports
            SET resolved_at = ?
            WHERE source_kind = ? AND resolved_at IS NULL
            """,
            (utc_now(), source_kind),
        )
    else:
        cursor = db.execute(
            """
            UPDATE pending_imports
            SET resolved_at = ?
            WHERE source_kind = ? AND source_identifier = ? AND resolved_at IS NULL
            """,
            (utc_now(), source_kind, source_identifier),
        )
    return int(cursor.rowcount)


def active_pending_imports(db: sqlite3.Connection, *, source_kind: str | None = None) -> list[sqlite3.Row]:
    if source_kind is None:
        return list(db.execute("SELECT * FROM pending_imports WHERE resolved_at IS NULL ORDER BY detected_at DESC").fetchall())
    return list(
        db.execute(
            """
            SELECT * FROM pending_imports
            WHERE source_kind = ? AND resolved_at IS NULL
            ORDER BY detected_at DESC
            """,
            (source_kind,),
        ).fetchall()
    )


def upsert_source_import(
    db: sqlite3.Connection,
    *,
    source_kind: str,
    source_identifier: str,
    source_path: str,
    raw_metadata: dict[str, object] | None = None,
) -> int:
    db.execute(
        """
        INSERT INTO source_imports (source_kind, source_identifier, source_path, raw_metadata_json)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(source_identifier) DO UPDATE SET
          source_kind = excluded.source_kind,
          source_path = excluded.source_path,
          imported_at = CURRENT_TIMESTAMP,
          raw_metadata_json = excluded.raw_metadata_json
        """,
        (source_kind, source_identifier, source_path, json_dumps(raw_metadata or {})),
    )
    return int(db.execute("SELECT id FROM source_imports WHERE source_identifier = ?", (source_identifier,)).fetchone()["id"])


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
        (stable_key, display_name, kind),
    )
    return int(db.execute("SELECT id FROM identities WHERE stable_key = ?", (stable_key,)).fetchone()["id"])


def upsert_account(
    db: sqlite3.Connection,
    *,
    identity_id: int | None,
    source_kind: str,
    account_key: str,
    display_name: str,
    profile_url: str | None = None,
    raw_metadata: dict[str, object] | None = None,
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
        (identity_id, source_kind, account_key, display_name, profile_url, json_dumps(raw_metadata or {})),
    )
    return int(
        db.execute(
            "SELECT id FROM accounts WHERE source_kind = ? AND account_key = ?",
            (source_kind, account_key),
        ).fetchone()["id"]
    )


def upsert_thread(
    db: sqlite3.Connection,
    *,
    source_kind: str,
    source_thread_key: str,
    title: str,
    thread_kind: str,
    first_message_at: str | None,
    last_message_at: str | None,
    raw_metadata: dict[str, object] | None = None,
) -> int:
    db.execute(
        """
        INSERT INTO threads (
          source_kind, source_thread_key, title, thread_kind, first_message_at, last_message_at, raw_metadata_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_kind, source_thread_key) DO UPDATE SET
          title = excluded.title,
          thread_kind = excluded.thread_kind,
          first_message_at = excluded.first_message_at,
          last_message_at = excluded.last_message_at,
          raw_metadata_json = excluded.raw_metadata_json
        """,
        (
            source_kind,
            source_thread_key,
            title,
            thread_kind,
            first_message_at,
            last_message_at,
            json_dumps(raw_metadata or {}),
        ),
    )
    return int(
        db.execute(
            "SELECT id FROM threads WHERE source_kind = ? AND source_thread_key = ?",
            (source_kind, source_thread_key),
        ).fetchone()["id"]
    )


def link_thread_participant(
    db: sqlite3.Connection,
    *,
    thread_id: int,
    identity_id: int | None,
    account_id: int,
    role: str = "participant",
) -> None:
    db.execute(
        """
        INSERT OR IGNORE INTO thread_participants (thread_id, identity_id, account_id, role)
        VALUES (?, ?, ?, ?)
        """,
        (thread_id, identity_id, account_id, role),
    )
    db.execute(
        """
        UPDATE thread_participants
        SET identity_id = ?
        WHERE thread_id = ? AND account_id = ? AND role = ?
        """,
        (identity_id, thread_id, account_id, role),
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
    body_format: str = "plain",
    raw_json: dict[str, object] | None = None,
) -> int:
    db.execute(
        """
        INSERT INTO messages (
          thread_id, source_message_key, sender_identity_id, sender_account_id,
          sent_at, body_text, body_format, raw_json
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
        (
            thread_id,
            source_message_key,
            sender_identity_id,
            sender_account_id,
            sent_at,
            body_text,
            body_format,
            json_dumps(raw_json or {}),
        ),
    )
    return int(
        db.execute(
            "SELECT id FROM messages WHERE thread_id = ? AND source_message_key = ?",
            (thread_id, source_message_key),
        ).fetchone()["id"]
    )


def upsert_media_object(
    db: sqlite3.Connection,
    *,
    message_id: int,
    object_key: str,
    source_uri: str | None,
    local_path: str | None,
    mime_type: str | None,
    checksum: str | None = None,
    raw_metadata: dict[str, object] | None = None,
) -> int:
    db.execute(
        """
        INSERT INTO media_objects (
          message_id, object_key, source_uri, local_path, mime_type, checksum, raw_metadata_json
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
    return int(db.execute("SELECT id FROM media_objects WHERE object_key = ?", (object_key,)).fetchone()["id"])


def upsert_graph_edge(
    db: sqlite3.Connection,
    *,
    from_kind: str,
    from_key: str,
    edge_kind: str,
    to_kind: str,
    to_key: str,
    source: str = "import",
) -> None:
    db.execute(
        """
        INSERT OR IGNORE INTO graph_edges (from_kind, from_key, edge_kind, to_kind, to_key, source)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (from_kind, from_key, edge_kind, to_kind, to_key, source),
    )
