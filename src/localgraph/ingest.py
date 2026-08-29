from __future__ import annotations

import hashlib
import json
import plistlib
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .instagram import detect_export_root, instagram_export_account_key, instagram_message_files
from .paths import Workspace
from .slug import slugify, stable_hash


APPLE_EPOCH_UNIX_SECONDS = 978307200
INSTAGRAM_MEDIA_KEYS = ("photos", "videos", "audio_files", "files", "gifs")


@dataclass
class SourceImportResult:
    source_kind: str
    source_path: str
    status: str = "imported"
    imports: int = 0
    threads: int = 0
    groups: int = 0
    accounts: int = 0
    messages: int = 0
    media: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, object]:
        return {
            "sourceKind": self.source_kind,
            "sourcePath": self.source_path,
            "status": self.status,
            "imports": self.imports,
            "threads": self.threads,
            "groups": self.groups,
            "accounts": self.accounts,
            "messages": self.messages,
            "media": self.media,
            "warnings": self.warnings,
        }


def import_workspace_sources(
    db: sqlite3.Connection,
    workspace: Workspace,
    *,
    instagram_source: Path | None = None,
    imessage_db: Path | None = None,
    import_instagram: bool = True,
    import_imessage: bool = True,
    me_name: str = "Me",
    me_instagram_names: list[str] | None = None,
    me_imessage_handles: list[str] | None = None,
    explicit_instagram: bool = False,
    explicit_imessage: bool = False,
) -> dict[str, object]:
    results: list[SourceImportResult] = []
    if import_instagram:
        results.append(
            import_instagram_source(
                db,
                instagram_source or workspace.instagram_source_dir,
                me_name=me_name,
                me_names=me_instagram_names or [],
                explicit=explicit_instagram,
            )
        )
    if import_imessage:
        results.append(
            import_imessage_chat_db(
                db,
                imessage_db or workspace.imessage_chat_db_path,
                me_name=me_name,
                me_handles=me_imessage_handles or [],
                explicit=explicit_imessage,
            )
        )
    return {
        "sources": [result.to_json() for result in results],
        "totals": {
            "imports": sum(result.imports for result in results),
            "threads": sum(result.threads for result in results),
            "groups": sum(result.groups for result in results),
            "accounts": sum(result.accounts for result in results),
            "messages": sum(result.messages for result in results),
            "media": sum(result.media for result in results),
        },
    }


def clear_instagram_projection(db: sqlite3.Connection) -> dict[str, int]:
    """Remove source-derived Instagram state before rebuilding from completed exports."""
    counts = {
        "sourceImports": int(
            db.execute("SELECT COUNT(*) FROM source_imports WHERE source_kind = 'instagram'").fetchone()[0]
        ),
        "threads": int(db.execute("SELECT COUNT(*) FROM threads WHERE source_kind = 'instagram'").fetchone()[0]),
        "messages": int(
            db.execute(
                "SELECT COUNT(*) FROM messages JOIN threads ON threads.id = messages.thread_id "
                "WHERE threads.source_kind = 'instagram'"
            ).fetchone()[0]
        ),
        "media": int(
            db.execute(
                "SELECT COUNT(*) FROM media_objects JOIN messages ON messages.id = media_objects.message_id "
                "JOIN threads ON threads.id = messages.thread_id WHERE threads.source_kind = 'instagram'"
            ).fetchone()[0]
        ),
        "accounts": int(db.execute("SELECT COUNT(*) FROM accounts WHERE source_kind = 'instagram'").fetchone()[0]),
        "identities": int(
            db.execute(
                "SELECT COUNT(*) FROM identities WHERE stable_key GLOB 'person:instagram:*' "
                "OR stable_key GLOB 'group:instagram:*' OR stable_key GLOB 'organization:instagram:*'"
            ).fetchone()[0]
        ),
    }
    db.execute("DELETE FROM graph_edges WHERE source = 'instagram-import'")
    db.execute("DELETE FROM source_imports WHERE source_kind = 'instagram'")
    db.execute("DELETE FROM threads WHERE source_kind = 'instagram'")
    db.execute("DELETE FROM accounts WHERE source_kind = 'instagram'")
    db.execute(
        "DELETE FROM identities WHERE stable_key GLOB 'person:instagram:*' "
        "OR stable_key GLOB 'group:instagram:*' OR stable_key GLOB 'organization:instagram:*'"
    )
    return counts


def import_instagram_source(
    db: sqlite3.Connection,
    source_path: Path,
    *,
    me_name: str = "Me",
    me_names: list[str] | None = None,
    explicit: bool = False,
    account_key: str | None = None,
    owner_identity_key: str = "person:self",
    owner_kind: str = "person",
    commit: bool = True,
) -> SourceImportResult:
    source = source_path.expanduser().resolve()
    result = SourceImportResult("instagram", str(source))
    if not source.exists():
        if explicit:
            raise FileNotFoundError(f"Instagram source does not exist: {source}")
        result.status = "missing"
        return result

    message_files = _instagram_message_files(source)
    if not message_files:
        result.status = "empty"
        return result

    inferred_account_keys = {
        inferred
        for file_path in message_files
        if (inferred := instagram_export_account_key(detect_export_root(source, file_path).name)) is not None
    }
    implicit_account_scope = len(inferred_account_keys) > 1

    self_names = {_person_key(name) for name in [me_name, *(me_names or [])] if name}
    seen_imports: set[str] = set()
    seen_threads: set[str] = set()
    seen_groups: set[str] = set()
    seen_accounts: set[str] = set()
    seen_messages: set[tuple[int, str]] = set()
    seen_media: set[str] = set()
    message_occurrences: dict[tuple[str, str, str], int] = {}

    for file_path in message_files:
        export_root = detect_export_root(source, file_path)
        inferred_account_key = instagram_export_account_key(export_root.name)
        if account_key is not None and inferred_account_key is not None and inferred_account_key != account_key:
            raise ValueError(
                f"Instagram export account mismatch: expected {account_key}, found {inferred_account_key} in {export_root.name}"
            )
        scoped_account_key = account_key or (inferred_account_key if implicit_account_scope else None)
        source_identifier = (
            f"instagram:{scoped_account_key}:{export_root.as_posix()}"
            if scoped_account_key
            else f"instagram:{export_root.as_posix()}"
        )
        if source_identifier not in seen_imports:
            _upsert_source_import(
                db,
                "instagram",
                source_identifier,
                export_root,
                {"source_root": str(source), "export_name": export_root.name},
            )
            seen_imports.add(source_identifier)

        payload = _read_instagram_json(file_path)
        if not isinstance(payload, dict):
            result.warnings.append(f"skipped non-object JSON: {file_path}")
            continue

        relative_thread_key = file_path.parent.relative_to(export_root).as_posix()
        source_thread_key = f"{scoped_account_key}:{relative_thread_key}" if scoped_account_key else relative_thread_key
        raw_participants = payload.get("participants") or []
        participants = _instagram_participants(raw_participants)
        title = _clean_text(payload.get("title")) or _title_from_participants(participants) or file_path.parent.name
        thread_kind = "group" if len(participants) > 2 else "direct"
        thread_id = _upsert_thread(
            db,
            "instagram",
            source_thread_key,
            title,
            thread_kind,
            {
                "export_root": str(export_root),
                "thread_path": relative_thread_key,
                "instagram_account_key": scoped_account_key,
                "raw_title": payload.get("title"),
            },
        )
        seen_threads.add(source_thread_key)

        participant_accounts: dict[str, tuple[int, int, str]] = {}
        for participant in participants:
            identity_id, account_id, stable_key = _upsert_instagram_participant(
                db,
                participant,
                self_names=self_names,
                me_name=me_name,
                owner_identity_key=owner_identity_key,
                owner_kind=owner_kind,
                export_account_key=scoped_account_key,
            )
            participant_accounts[participant] = (identity_id, account_id, stable_key)
            seen_accounts.add(f"instagram:{_person_key(participant)}")
            _upsert_thread_participant(db, thread_id, identity_id, account_id)
            _upsert_edge(
                db,
                "thread",
                _thread_key("instagram", source_thread_key),
                "has_participant",
                "identity",
                stable_key,
                "instagram-import",
            )

        if thread_kind == "group":
            group_key = _group_key("instagram", source_thread_key)
            _upsert_identity(db, group_key, title, "group")
            seen_groups.add(group_key)
            _upsert_edge(
                db,
                "thread",
                _thread_key("instagram", source_thread_key),
                "represents_group",
                "identity",
                group_key,
                "instagram-import",
            )
            for _, _, stable_key in participant_accounts.values():
                _upsert_edge(db, "identity", group_key, "has_participant", "identity", stable_key, "instagram-import")

        messages = payload.get("messages") or []
        if not isinstance(messages, list):
            result.warnings.append(f"skipped non-list messages in {file_path}")
            continue

        for message in messages:
            if not isinstance(message, dict):
                continue
            sender_name = _clean_text(message.get("sender_name")) or "Unknown Instagram Sender"
            sender_account = participant_accounts.get(sender_name)
            if sender_account is None:
                sender_account = _upsert_instagram_participant(
                    db,
                    sender_name,
                    self_names=self_names,
                    me_name=me_name,
                    owner_identity_key=owner_identity_key,
                    owner_kind=owner_kind,
                    export_account_key=scoped_account_key,
                )
                participant_accounts[sender_name] = sender_account
                seen_accounts.add(f"instagram:{_person_key(sender_name)}")
                sender_identity_id, sender_account_id, sender_stable_key = sender_account
                _upsert_thread_participant(db, thread_id, sender_identity_id, sender_account_id)
                _upsert_edge(
                    db,
                    "thread",
                    _thread_key("instagram", source_thread_key),
                    "has_participant",
                    "identity",
                    sender_stable_key,
                    "instagram-import",
                )
                if thread_kind == "group":
                    _upsert_edge(
                        db,
                        "identity",
                        _group_key("instagram", source_thread_key),
                        "has_participant",
                        "identity",
                        sender_stable_key,
                        "instagram-import",
                    )
            else:
                sender_identity_id, sender_account_id, _ = sender_account
            timestamp_ms = _as_int(message.get("timestamp_ms"))
            fingerprint = _instagram_message_fingerprint(message)
            occurrence_key = (export_root.as_posix(), source_thread_key, fingerprint)
            occurrence = message_occurrences.get(occurrence_key, 0)
            message_occurrences[occurrence_key] = occurrence + 1
            source_message_key = _instagram_message_key(message, occurrence=occurrence, fingerprint=fingerprint)
            body_text = _instagram_message_body(message)
            message_id = _upsert_message(
                db,
                thread_id,
                source_message_key,
                sender_identity_id,
                sender_account_id,
                _timestamp_ms_to_iso(timestamp_ms),
                body_text,
                message,
            )
            seen_messages.add((thread_id, source_message_key))
            for media in _instagram_media_objects(export_root, source_thread_key, source_message_key, message):
                media_id = _upsert_media_object(db, message_id, media)
                if media_id is not None:
                    seen_media.add(media["object_key"])

        _refresh_thread_bounds(db, thread_id)

    if commit:
        db.commit()
    result.imports = len(seen_imports)
    result.threads = len(seen_threads)
    result.groups = len(seen_groups)
    result.accounts = len(seen_accounts)
    result.messages = len(seen_messages)
    result.media = len(seen_media)
    return result


def import_imessage_chat_db(
    db: sqlite3.Connection,
    chat_db_path: Path,
    *,
    me_name: str = "Me",
    me_handles: list[str] | None = None,
    explicit: bool = False,
) -> SourceImportResult:
    source = chat_db_path.expanduser().resolve()
    result = SourceImportResult("imessage", str(source))
    if not source.exists():
        if explicit:
            raise FileNotFoundError(f"iMessage chat database does not exist: {source}")
        result.status = "missing"
        return result

    _upsert_source_import(db, "imessage", f"imessage:{source.as_posix()}", source, {"kind": "apple-chat-db"})
    result.imports = 1

    me_identity_id, me_account_id, me_stable_key = _upsert_self_identity(db, me_name, "imessage:self")
    me_handle_keys = {_handle_key(handle) for handle in (me_handles or [])}
    source_db = _connect_readonly_sqlite(source)
    source_db.row_factory = sqlite3.Row
    try:
        chats = _load_imessage_chats(source_db)
        participants = _load_imessage_participants(source_db)
        attachments = _load_imessage_attachments(source_db)
        message_columns = _table_columns(source_db, "message")
        attributed_expr = "m.attributedBody" if "attributedBody" in message_columns else "NULL"
        rows = source_db.execute(
            f"""
            SELECT
              c.ROWID AS chat_rowid,
              c.guid AS chat_guid,
              c.chat_identifier AS chat_identifier,
              c.display_name AS chat_display_name,
              c.service_name AS chat_service_name,
              m.ROWID AS message_rowid,
              m.guid AS message_guid,
              m.text AS text,
              {attributed_expr} AS attributed_body,
              m.date AS date,
              m.is_from_me AS is_from_me,
              m.handle_id AS handle_id,
              m.service AS message_service,
              h.id AS sender_handle,
              h.service AS sender_service
            FROM chat AS c
            JOIN chat_message_join AS cmj ON cmj.chat_id = c.ROWID
            JOIN message AS m ON m.ROWID = cmj.message_id
            LEFT JOIN handle AS h ON h.ROWID = m.handle_id
            ORDER BY c.ROWID, m.date, m.ROWID
            """
        )

        seen_threads: set[str] = set()
        seen_groups: set[str] = set()
        seen_accounts: set[str] = set()
        seen_messages: set[tuple[int, str]] = set()
        seen_media: set[str] = set()
        thread_cache: dict[int, tuple[int, str, str]] = {}
        participant_account_cache: dict[tuple[int, str], tuple[int, int, str]] = {}

        for row in rows:
            chat_rowid = int(row["chat_rowid"])
            if chat_rowid not in thread_cache:
                chat = chats[chat_rowid]
                chat_participants = participants.get(chat_rowid, [])
                thread_kind = "group" if len(chat_participants) > 1 or bool(chat["display_name"]) else "direct"
                thread_title = _imessage_thread_title(chat, chat_participants, me_name)
                source_thread_key = str(chat["guid"] or f"chat:{chat_rowid}")
                thread_id = _upsert_thread(
                    db,
                    "imessage",
                    source_thread_key,
                    thread_title,
                    thread_kind,
                    {
                        "chat_rowid": chat_rowid,
                        "chat_identifier": chat["chat_identifier"],
                        "service_name": chat["service_name"],
                    },
                )
                seen_threads.add(source_thread_key)
                _upsert_thread_participant(db, thread_id, me_identity_id, me_account_id, role="self")
                _upsert_edge(
                    db,
                    "thread",
                    _thread_key("imessage", source_thread_key),
                    "has_participant",
                    "identity",
                    me_stable_key,
                    "imessage-import",
                )
                for handle in chat_participants:
                    identity_id, account_id, stable_key = _upsert_imessage_handle(
                        db,
                        str(handle["id"] or ""),
                        str(handle["service"] or "iMessage"),
                        me_identity_id=me_identity_id,
                        me_stable_key=me_stable_key,
                        me_handle_keys=me_handle_keys,
                    )
                    participant_account_cache[(chat_rowid, str(handle["ROWID"]))] = (identity_id, account_id, stable_key)
                    seen_accounts.add(f"imessage:{_handle_key(str(handle['id'] or ''))}")
                    _upsert_thread_participant(db, thread_id, identity_id, account_id)
                    _upsert_edge(
                        db,
                        "thread",
                        _thread_key("imessage", source_thread_key),
                        "has_participant",
                        "identity",
                        stable_key,
                        "imessage-import",
                    )
                if thread_kind == "group":
                    group_key = _group_key("imessage", source_thread_key)
                    _upsert_identity(db, group_key, thread_title, "group")
                    seen_groups.add(group_key)
                    _upsert_edge(
                        db,
                        "thread",
                        _thread_key("imessage", source_thread_key),
                        "represents_group",
                        "identity",
                        group_key,
                        "imessage-import",
                    )
                    _upsert_edge(db, "identity", group_key, "has_participant", "identity", me_stable_key, "imessage-import")
                    for handle in chat_participants:
                        cached = participant_account_cache.get((chat_rowid, str(handle["ROWID"])))
                        if cached is None:
                            continue
                        _, _, stable_key = cached
                        _upsert_edge(db, "identity", group_key, "has_participant", "identity", stable_key, "imessage-import")
                thread_cache[chat_rowid] = (thread_id, source_thread_key, thread_kind)

            thread_id, source_thread_key, thread_kind = thread_cache[chat_rowid]
            if int(row["is_from_me"] or 0) == 1:
                sender_identity_id, sender_account_id = me_identity_id, me_account_id
            else:
                handle_id = row["handle_id"]
                cached = participant_account_cache.get((chat_rowid, str(handle_id)))
                if cached is None:
                    sender_identity_id, sender_account_id, sender_stable_key = _upsert_imessage_handle(
                        db,
                        str(row["sender_handle"] or "unknown"),
                        str(row["sender_service"] or row["message_service"] or "iMessage"),
                        me_identity_id=me_identity_id,
                        me_stable_key=me_stable_key,
                        me_handle_keys=me_handle_keys,
                    )
                    seen_accounts.add(f"imessage:{_handle_key(str(row['sender_handle'] or 'unknown'))}")
                    _upsert_thread_participant(db, thread_id, sender_identity_id, sender_account_id)
                    _upsert_edge(
                        db,
                        "thread",
                        _thread_key("imessage", source_thread_key),
                        "has_participant",
                        "identity",
                        sender_stable_key,
                        "imessage-import",
                    )
                    if thread_kind == "group":
                        _upsert_edge(
                            db,
                            "identity",
                            _group_key("imessage", source_thread_key),
                            "has_participant",
                            "identity",
                            sender_stable_key,
                            "imessage-import",
                        )
                else:
                    sender_identity_id, sender_account_id, _ = cached

            source_message_key = str(row["message_guid"] or f"message:{row['message_rowid']}")
            body_text = row["text"] if row["text"] is not None else _decode_attributed_body(row["attributed_body"])
            raw_message = {
                "message_rowid": row["message_rowid"],
                "guid": row["message_guid"],
                "handle_id": row["handle_id"],
                "is_from_me": row["is_from_me"],
                "service": row["message_service"],
                "date": row["date"],
            }
            message_id = _upsert_message(
                db,
                thread_id,
                source_message_key,
                sender_identity_id,
                sender_account_id,
                _imessage_date_to_iso(row["date"]),
                body_text,
                raw_message,
            )
            seen_messages.add((thread_id, source_message_key))
            for attachment in attachments.get(int(row["message_rowid"]), []):
                media = {
                    "object_key": f"imessage:{source_message_key}:{attachment['guid'] or attachment['ROWID']}",
                    "source_uri": attachment["filename"],
                    "local_path": attachment["filename"],
                    "mime_type": attachment["mime_type"] or attachment["uti"],
                    "checksum": None,
                    "raw_metadata": {key: attachment[key] for key in attachment.keys()},
                }
                media_id = _upsert_media_object(db, message_id, media)
                if media_id is not None:
                    seen_media.add(media["object_key"])

        for thread_id, _, _ in thread_cache.values():
            _refresh_thread_bounds(db, thread_id)
    finally:
        source_db.close()

    db.commit()
    result.threads = len(seen_threads)
    result.groups = len(seen_groups)
    result.accounts = len(seen_accounts) + 1
    result.messages = len(seen_messages)
    result.media = len(seen_media)
    return result


def _instagram_message_files(source: Path) -> list[Path]:
    return instagram_message_files(source)


def _read_instagram_json(file_path: Path) -> Any:
    raw = file_path.read_bytes()
    try:
        return json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        return json.loads(raw.decode("latin-1"))


def _instagram_participants(raw_participants: object) -> list[str]:
    participants: list[str] = []
    if isinstance(raw_participants, list):
        for item in raw_participants:
            name = item.get("name") if isinstance(item, dict) else item
            clean = _clean_text(name)
            if clean and clean not in participants:
                participants.append(clean)
    return participants


def _instagram_message_body(message: dict[str, object]) -> str | None:
    content = _clean_text(message.get("content"))
    if content:
        return content
    share = message.get("share")
    if isinstance(share, dict):
        link = _clean_text(share.get("link"))
        text = _clean_text(share.get("share_text"))
        if link and text:
            return f"{text}\n{link}"
        return link or text
    return None


def _instagram_media_objects(
    export_root: Path,
    source_thread_key: str,
    source_message_key: str,
    message: dict[str, object],
) -> list[dict[str, object]]:
    media: list[dict[str, object]] = []
    for key in INSTAGRAM_MEDIA_KEYS:
        items = message.get(key)
        if not isinstance(items, list):
            continue
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            uri = _clean_text(item.get("uri"))
            if not uri:
                continue
            object_key = f"instagram:{stable_hash(f'{source_thread_key}:{source_message_key}:{key}:{index}:{uri}', length=24)}"
            media.append(
                {
                    "object_key": object_key,
                    "source_uri": uri,
                    "local_path": str((export_root / uri).resolve()) if not Path(uri).is_absolute() else uri,
                    "mime_type": item.get("mime_type"),
                    "checksum": None,
                    "raw_metadata": {"kind": key, **item},
                }
            )
    return media


def _instagram_message_fingerprint(message: dict[str, object]) -> str:
    stable_payload = json.dumps(message, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(stable_payload.encode("utf-8")).hexdigest()


def _instagram_message_key(
    message: dict[str, object],
    *,
    occurrence: int,
    fingerprint: str | None = None,
) -> str:
    timestamp = str(message.get("timestamp_ms") or "")
    digest = fingerprint or _instagram_message_fingerprint(message)
    return f"v2:{timestamp}:{digest[:24]}:{occurrence}"


def _upsert_instagram_participant(
    db: sqlite3.Connection,
    name: str,
    *,
    self_names: set[str],
    me_name: str,
    owner_identity_key: str = "person:self",
    owner_kind: str = "person",
    export_account_key: str | None = None,
) -> tuple[int, int, str]:
    clean_name = _clean_text(name) or "Unknown Instagram User"
    person_key = _person_key(clean_name)
    if person_key in self_names:
        identity_id, stable_key = _upsert_identity(db, owner_identity_key, me_name, owner_kind)
    else:
        stable_key = f"person:instagram:{person_key}"
        identity_id, stable_key = _upsert_identity(db, stable_key, clean_name, "person")
    account_key = person_key
    account_id = _upsert_account(
        db,
        identity_id,
        "instagram",
        account_key,
        clean_name,
        None,
        {"name": clean_name, "observedInExportAccount": export_account_key},
    )
    return identity_id, account_id, stable_key


def _upsert_imessage_handle(
    db: sqlite3.Connection,
    handle: str,
    service: str,
    *,
    me_identity_id: int,
    me_stable_key: str,
    me_handle_keys: set[str],
) -> tuple[int, int, str]:
    clean_handle = handle.strip() or "unknown"
    account_key = _handle_key(clean_handle)
    display_name = clean_handle
    if account_key in me_handle_keys:
        identity_id, stable_key = me_identity_id, me_stable_key
    else:
        stable_key = f"person:imessage:{account_key}"
        identity_id, stable_key = _upsert_identity(db, stable_key, display_name, "person")
    account_id = _upsert_account(
        db,
        identity_id,
        "imessage",
        account_key,
        display_name,
        None,
        {"handle": clean_handle, "service": service},
    )
    return identity_id, account_id, stable_key


def _upsert_self_identity(db: sqlite3.Connection, me_name: str, account_key: str) -> tuple[int, int, str]:
    identity_id, stable_key = _upsert_identity(db, "person:self", me_name or "Me", "person")
    account_id = _upsert_account(db, identity_id, "self", account_key, me_name or "Me", None, {})
    return identity_id, account_id, stable_key


def _upsert_source_import(
    db: sqlite3.Connection,
    source_kind: str,
    source_identifier: str,
    source_path: Path,
    metadata: dict[str, object],
) -> int:
    db.execute(
        """
        INSERT INTO source_imports (source_kind, source_identifier, source_path, raw_metadata_json)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(source_identifier) DO UPDATE SET
          source_kind=excluded.source_kind,
          source_path=excluded.source_path,
          raw_metadata_json=excluded.raw_metadata_json,
          imported_at=CURRENT_TIMESTAMP
        """,
        (source_kind, source_identifier, str(source_path), _json(metadata)),
    )
    return int(db.execute("SELECT id FROM source_imports WHERE source_identifier = ?", (source_identifier,)).fetchone()["id"])


def _upsert_identity(db: sqlite3.Connection, stable_key: str, display_name: str, kind: str) -> tuple[int, str]:
    db.execute(
        """
        INSERT INTO identities (stable_key, display_name, kind)
        VALUES (?, ?, ?)
        ON CONFLICT(stable_key) DO UPDATE SET
          display_name=excluded.display_name,
          kind=excluded.kind,
          updated_at=CURRENT_TIMESTAMP
        """,
        (stable_key, display_name, kind),
    )
    row = db.execute("SELECT id, stable_key FROM identities WHERE stable_key = ?", (stable_key,)).fetchone()
    return int(row["id"]), str(row["stable_key"])


def _upsert_account(
    db: sqlite3.Connection,
    identity_id: int,
    source_kind: str,
    account_key: str,
    display_name: str,
    profile_url: str | None,
    metadata: dict[str, object],
) -> int:
    db.execute(
        """
        INSERT INTO accounts (identity_id, source_kind, account_key, display_name, profile_url, raw_metadata_json)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_kind, account_key) DO UPDATE SET
          identity_id=excluded.identity_id,
          display_name=excluded.display_name,
          profile_url=excluded.profile_url,
          raw_metadata_json=excluded.raw_metadata_json
        """,
        (identity_id, source_kind, account_key, display_name, profile_url, _json(metadata)),
    )
    row = db.execute(
        "SELECT id FROM accounts WHERE source_kind = ? AND account_key = ?",
        (source_kind, account_key),
    ).fetchone()
    return int(row["id"])


def _upsert_thread(
    db: sqlite3.Connection,
    source_kind: str,
    source_thread_key: str,
    title: str,
    thread_kind: str,
    metadata: dict[str, object],
) -> int:
    db.execute(
        """
        INSERT INTO threads (source_kind, source_thread_key, title, thread_kind, raw_metadata_json)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(source_kind, source_thread_key) DO UPDATE SET
          title=excluded.title,
          thread_kind=excluded.thread_kind,
          raw_metadata_json=excluded.raw_metadata_json
        """,
        (source_kind, source_thread_key, title, thread_kind, _json(metadata)),
    )
    row = db.execute(
        "SELECT id FROM threads WHERE source_kind = ? AND source_thread_key = ?",
        (source_kind, source_thread_key),
    ).fetchone()
    return int(row["id"])


def _upsert_thread_participant(
    db: sqlite3.Connection,
    thread_id: int,
    identity_id: int,
    account_id: int,
    *,
    role: str = "participant",
) -> None:
    db.execute(
        """
        INSERT OR IGNORE INTO thread_participants (thread_id, identity_id, account_id, role)
        VALUES (?, ?, ?, ?)
        """,
        (thread_id, identity_id, account_id, role),
    )


def _upsert_message(
    db: sqlite3.Connection,
    thread_id: int,
    source_message_key: str,
    sender_identity_id: int | None,
    sender_account_id: int | None,
    sent_at: str,
    body_text: str | None,
    raw: dict[str, object],
) -> int:
    db.execute(
        """
        INSERT INTO messages (
          thread_id, source_message_key, sender_identity_id, sender_account_id,
          sent_at, body_text, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(thread_id, source_message_key) DO UPDATE SET
          sender_identity_id=excluded.sender_identity_id,
          sender_account_id=excluded.sender_account_id,
          sent_at=excluded.sent_at,
          body_text=excluded.body_text,
          raw_json=excluded.raw_json
        """,
        (thread_id, source_message_key, sender_identity_id, sender_account_id, sent_at, body_text, _json(raw)),
    )
    row = db.execute(
        "SELECT id FROM messages WHERE thread_id = ? AND source_message_key = ?",
        (thread_id, source_message_key),
    ).fetchone()
    return int(row["id"])


def _upsert_media_object(db: sqlite3.Connection, message_id: int, media: dict[str, object]) -> int | None:
    object_key = str(media["object_key"])
    db.execute(
        """
        INSERT INTO media_objects (
          message_id, object_key, source_uri, local_path, mime_type, checksum, raw_metadata_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(object_key) DO UPDATE SET
          message_id=excluded.message_id,
          source_uri=excluded.source_uri,
          local_path=excluded.local_path,
          mime_type=excluded.mime_type,
          checksum=excluded.checksum,
          raw_metadata_json=excluded.raw_metadata_json
        """,
        (
            message_id,
            object_key,
            media.get("source_uri"),
            media.get("local_path"),
            media.get("mime_type"),
            media.get("checksum"),
            _json(media.get("raw_metadata") or {}),
        ),
    )
    row = db.execute("SELECT id FROM media_objects WHERE object_key = ?", (object_key,)).fetchone()
    return int(row["id"]) if row else None


def _upsert_edge(
    db: sqlite3.Connection,
    from_kind: str,
    from_key: str,
    edge_kind: str,
    to_kind: str,
    to_key: str,
    source: str,
) -> None:
    db.execute(
        """
        INSERT OR IGNORE INTO graph_edges (from_kind, from_key, edge_kind, to_kind, to_key, source)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (from_kind, from_key, edge_kind, to_kind, to_key, source),
    )


def _refresh_thread_bounds(db: sqlite3.Connection, thread_id: int) -> None:
    row = db.execute(
        "SELECT MIN(sent_at) AS first_at, MAX(sent_at) AS last_at FROM messages WHERE thread_id = ?",
        (thread_id,),
    ).fetchone()
    db.execute(
        "UPDATE threads SET first_message_at = ?, last_message_at = ? WHERE id = ?",
        (row["first_at"], row["last_at"], thread_id),
    )


def _load_imessage_chats(source_db: sqlite3.Connection) -> dict[int, sqlite3.Row]:
    return {
        int(row["ROWID"]): row
        for row in source_db.execute(
            "SELECT ROWID, guid, chat_identifier, display_name, service_name FROM chat ORDER BY ROWID"
        )
    }


def _load_imessage_participants(source_db: sqlite3.Connection) -> dict[int, list[sqlite3.Row]]:
    participants: dict[int, list[sqlite3.Row]] = {}
    if not _table_exists(source_db, "chat_handle_join"):
        return participants
    for row in source_db.execute(
        """
        SELECT chj.chat_id, h.ROWID, h.id, h.service
        FROM chat_handle_join AS chj
        JOIN handle AS h ON h.ROWID = chj.handle_id
        ORDER BY chj.chat_id, h.id
        """
    ):
        participants.setdefault(int(row["chat_id"]), []).append(row)
    return participants


def _load_imessage_attachments(source_db: sqlite3.Connection) -> dict[int, list[sqlite3.Row]]:
    attachments: dict[int, list[sqlite3.Row]] = {}
    if not (_table_exists(source_db, "message_attachment_join") and _table_exists(source_db, "attachment")):
        return attachments
    attachment_columns = _table_columns(source_db, "attachment")
    optional = {
        "mime_type": "a.mime_type" if "mime_type" in attachment_columns else "NULL",
        "uti": "a.uti" if "uti" in attachment_columns else "NULL",
        "total_bytes": "a.total_bytes" if "total_bytes" in attachment_columns else "NULL",
    }
    for row in source_db.execute(
        f"""
        SELECT
          maj.message_id,
          a.ROWID,
          a.guid,
          a.filename,
          {optional["mime_type"]} AS mime_type,
          {optional["uti"]} AS uti,
          {optional["total_bytes"]} AS total_bytes
        FROM message_attachment_join AS maj
        JOIN attachment AS a ON a.ROWID = maj.attachment_id
        ORDER BY maj.message_id, a.ROWID
        """
    ):
        attachments.setdefault(int(row["message_id"]), []).append(row)
    return attachments


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    return bool(db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (name,)).fetchone())


def _table_columns(db: sqlite3.Connection, name: str) -> set[str]:
    return {str(row["name"]) for row in db.execute(f"PRAGMA table_info({name})")}


def _connect_readonly_sqlite(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path), safe='/')}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _imessage_thread_title(chat: sqlite3.Row, participants: list[sqlite3.Row], me_name: str) -> str:
    display_name = _clean_text(chat["display_name"])
    if display_name:
        return display_name
    names = [_clean_text(row["id"]) for row in participants if _clean_text(row["id"])]
    if not names:
        return _clean_text(chat["chat_identifier"]) or me_name or "iMessage"
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:4]) + ("..." if len(names) > 4 else "")


def _decode_attributed_body(blob: bytes | None) -> str | None:
    if not blob:
        return None
    try:
        value = plistlib.loads(blob)
    except Exception:
        value = None
    if value is not None:
        strings = [text for text in _walk_strings(value) if _looks_like_message_text(text)]
        if strings:
            return max(strings, key=len)
    for encoding in ("utf-8", "utf-16le"):
        try:
            decoded = blob.decode(encoding, errors="ignore")
        except Exception:
            continue
        candidates = re.findall(r"[\t\n\r -~]{3,}", decoded)
        candidates = [candidate.strip() for candidate in candidates if _looks_like_message_text(candidate)]
        if candidates:
            return max(candidates, key=len)
    return None


def _walk_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        strings: list[str] = []
        for item in value.values():
            strings.extend(_walk_strings(item))
        return strings
    if isinstance(value, (list, tuple, set)):
        strings = []
        for item in value:
            strings.extend(_walk_strings(item))
        return strings
    return []


def _looks_like_message_text(value: str) -> bool:
    text = value.strip()
    if len(text) < 1:
        return False
    blocked_prefixes = ("NS.", "NSMutable", "NSString", "NSNumber", "__kIM")
    return not any(text.startswith(prefix) for prefix in blocked_prefixes)


def _imessage_date_to_iso(value: object) -> str:
    raw = _as_int(value)
    if raw is None:
        return _unix_to_iso(0)
    if abs(raw) > 1_000_000_000_000:
        seconds = APPLE_EPOCH_UNIX_SECONDS + raw / 1_000_000_000
    elif raw > 1_500_000_000:
        seconds = raw
    else:
        seconds = APPLE_EPOCH_UNIX_SECONDS + raw
    return _unix_to_iso(seconds)


def _timestamp_ms_to_iso(value: int | None) -> str:
    if value is None:
        return _unix_to_iso(0)
    return _unix_to_iso(value / 1000)


def _unix_to_iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _title_from_participants(participants: list[str]) -> str | None:
    if not participants:
        return None
    return ", ".join(participants[:4]) + ("..." if len(participants) > 4 else "")


def _clean_text(value: object) -> str:
    if value is None:
        return ""
    text = str(value)
    try:
        decoded = text.encode("latin-1").decode("utf-8")
        if decoded.count("\ufffd") <= text.count("\ufffd"):
            text = decoded
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return re.sub(r"\s+", " ", text).strip()


def _person_key(value: str) -> str:
    return slugify(_clean_text(value)).lower()


def _handle_key(value: str) -> str:
    return re.sub(r"\s+", "", value.strip().lower()) or "unknown"


def _thread_key(source_kind: str, source_thread_key: str) -> str:
    return f"thread:{source_kind}:{source_thread_key}"


def _group_key(source_kind: str, source_thread_key: str) -> str:
    return f"group:{source_kind}:{stable_hash(source_thread_key, length=16)}"


def _as_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
