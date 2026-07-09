from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .slug import slugify
from .store import (
    ImportStats,
    link_thread_participant,
    upsert_account,
    upsert_graph_edge,
    upsert_identity,
    upsert_media_object,
    upsert_message,
    upsert_source_import,
    upsert_thread,
)

APPLE_EPOCH_UNIX_SECONDS = 978_307_200


@dataclass(frozen=True)
class IMessageHandle:
    rowid: int
    account_key: str
    display_name: str
    service: str | None


@dataclass
class IMessageThread:
    rowid: int
    source_thread_key: str
    title: str
    handle_ids: set[int] = field(default_factory=set)
    messages: list[sqlite3.Row] = field(default_factory=list)


def import_imessage_source(db: sqlite3.Connection, source_path: Path) -> dict[str, object]:
    source = source_path.expanduser().resolve()
    stats = ImportStats(source_kind="imessage", source_path=str(source))
    chat_db_paths = _discover_chat_databases(source)
    if not chat_db_paths:
        stats.skipped = True
        stats.note = "no chat.db or SQLite database found"
        return stats.to_json()

    for chat_db_path in chat_db_paths:
        _import_chat_database(db, chat_db_path, stats)

    db.commit()
    return stats.to_json()


def _discover_chat_databases(source: Path) -> list[Path]:
    if source.is_file():
        return [source]
    if not source.exists():
        return []
    preferred = source / "chat.db"
    if preferred.exists():
        return [preferred]
    return sorted(path for path in source.iterdir() if path.is_file() and path.suffix.lower() in {".db", ".sqlite", ".sqlite3"})


def _import_chat_database(db: sqlite3.Connection, chat_db_path: Path, stats: ImportStats) -> None:
    source = sqlite3.connect(f"file:{chat_db_path}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    try:
        if not _table_exists(source, "message") or not _table_exists(source, "handle"):
            stats.note = "one or more iMessage databases did not contain message and handle tables"
            return

        source_identifier = f"imessage:{chat_db_path}"
        upsert_source_import(
            db,
            source_kind="imessage",
            source_identifier=source_identifier,
            source_path=str(chat_db_path),
            raw_metadata={"path": str(chat_db_path)},
        )
        stats.imports += 1

        handles = _load_handles(source)
        chat_handle_ids = _load_chat_handle_ids(source)
        messages_by_chat = _load_messages_by_chat(source)
        attachments_by_message = _load_attachments_by_message(source)
        chats = _load_chats(source)

        me_identity_id, me_account_id, me_identity_key = _ensure_me(db)
        stats.people += 1
        account_cache: dict[int, tuple[int, int, str]] = {}

        for chat_id, messages in messages_by_chat.items():
            chat = chats.get(chat_id)
            thread = _build_thread(chat_id, chat, chat_handle_ids.get(chat_id, set()), messages, handles)
            first_message_at, last_message_at = _thread_bounds(messages)
            thread_kind = "group" if len(thread.handle_ids) > 1 else "direct"
            thread_id = upsert_thread(
                db,
                source_kind="imessage",
                source_thread_key=thread.source_thread_key,
                title=thread.title,
                thread_kind=thread_kind,
                first_message_at=first_message_at,
                last_message_at=last_message_at,
                raw_metadata={
                    "chatId": chat_id,
                    "chat": _row_to_dict(chat) if chat else None,
                    "database": str(chat_db_path),
                },
            )
            stats.threads += 1

            link_thread_participant(db, thread_id=thread_id, identity_id=me_identity_id, account_id=me_account_id, role="self")
            upsert_graph_edge(
                db,
                from_kind="thread",
                from_key=f"imessage:{thread.source_thread_key}",
                edge_kind="participant",
                to_kind="identity",
                to_key=me_identity_key,
            )

            participant_refs: dict[int, tuple[int, int, str]] = {}
            for handle_id in sorted(thread.handle_ids):
                handle = handles.get(handle_id)
                if handle is None:
                    continue
                ref = account_cache.get(handle_id)
                if ref is None:
                    ref = _ensure_handle(db, handle)
                    account_cache[handle_id] = ref
                    stats.people += 1
                participant_refs[handle_id] = ref
                link_thread_participant(db, thread_id=thread_id, identity_id=ref[0], account_id=ref[1])
                upsert_graph_edge(
                    db,
                    from_kind="thread",
                    from_key=f"imessage:{thread.source_thread_key}",
                    edge_kind="participant",
                    to_kind="identity",
                    to_key=ref[2],
                )

            group_key = None
            if thread_kind == "group":
                group_key = f"group:imessage:{thread.source_thread_key}"
                upsert_identity(db, stable_key=group_key, display_name=thread.title, kind="group")
                stats.groups += 1
                for _, _, identity_key in participant_refs.values():
                    upsert_graph_edge(
                        db,
                        from_kind="identity",
                        from_key=group_key,
                        edge_kind="member",
                        to_kind="identity",
                        to_key=identity_key,
                    )
                upsert_graph_edge(
                    db,
                    from_kind="identity",
                    from_key=group_key,
                    edge_kind="member",
                    to_kind="identity",
                    to_key=me_identity_key,
                )
                upsert_graph_edge(
                    db,
                    from_kind="thread",
                    from_key=f"imessage:{thread.source_thread_key}",
                    edge_kind="represents",
                    to_kind="identity",
                    to_key=group_key,
                )

            for message in sorted(messages, key=lambda row: (_row_value(row, "date") or 0, _row_value(row, "message_id") or 0)):
                sender_identity_id: int | None
                sender_account_id: int | None
                if int(_row_value(message, "is_from_me") or 0):
                    sender_identity_id, sender_account_id = me_identity_id, me_account_id
                else:
                    sender_ref = participant_refs.get(int(_row_value(message, "handle_id") or 0))
                    sender_identity_id = sender_ref[0] if sender_ref else None
                    sender_account_id = sender_ref[1] if sender_ref else None

                message_key = _message_key(message)
                message_id = upsert_message(
                    db,
                    thread_id=thread_id,
                    source_message_key=message_key,
                    sender_identity_id=sender_identity_id,
                    sender_account_id=sender_account_id,
                    sent_at=_apple_date_to_iso(_row_value(message, "date")),
                    body_text=_message_body(message, attachments_by_message.get(int(_row_value(message, "message_id")), [])),
                    raw_json={
                        "database": str(chat_db_path),
                        "message": _row_to_dict(message, skip={"attributedBody"}),
                        "groupIdentity": group_key,
                    },
                )
                stats.messages += 1
                for attachment_index, attachment in enumerate(attachments_by_message.get(int(_row_value(message, "message_id")), [])):
                    filename = _row_value(attachment, "filename")
                    mime_type = _row_value(attachment, "mime_type")
                    object_key = f"imessage:{message_key}:{attachment_index}:{_short_hash(str(filename or attachment_index))}"
                    upsert_media_object(
                        db,
                        message_id=message_id,
                        object_key=object_key,
                        source_uri=str(filename) if filename else None,
                        local_path=str(filename) if filename else None,
                        mime_type=str(mime_type) if mime_type else None,
                        raw_metadata=_row_to_dict(attachment),
                    )
                    stats.media_objects += 1
    finally:
        source.close()


def _load_handles(db: sqlite3.Connection) -> dict[int, IMessageHandle]:
    columns = _columns(db, "handle")
    select = _select_clause(
        columns,
        {
            "ROWID": "rowid",
            "id": "id",
            "service": "service",
            "uncanonicalized_id": "uncanonicalized_id",
        },
        table="handle",
    )
    handles: dict[int, IMessageHandle] = {}
    for row in db.execute(f"SELECT {select} FROM handle"):
        handle_id = int(row["rowid"])
        account_key = str(row["id"] or row["uncanonicalized_id"] or f"handle:{handle_id}")
        handles[handle_id] = IMessageHandle(
            rowid=handle_id,
            account_key=account_key,
            display_name=account_key,
            service=str(row["service"]) if row["service"] else None,
        )
    return handles


def _load_chats(db: sqlite3.Connection) -> dict[int, sqlite3.Row]:
    if not _table_exists(db, "chat"):
        return {}
    columns = _columns(db, "chat")
    select = _select_clause(
        columns,
        {
            "ROWID": "chat_id",
            "guid": "guid",
            "chat_identifier": "chat_identifier",
            "display_name": "display_name",
            "room_name": "room_name",
            "service_name": "service_name",
            "style": "style",
        },
        table="chat",
    )
    return {int(row["chat_id"]): row for row in db.execute(f"SELECT {select} FROM chat")}


def _load_chat_handle_ids(db: sqlite3.Connection) -> dict[int, set[int]]:
    if not _table_exists(db, "chat_handle_join"):
        return {}
    out: dict[int, set[int]] = {}
    for row in db.execute("SELECT chat_id, handle_id FROM chat_handle_join"):
        out.setdefault(int(row["chat_id"]), set()).add(int(row["handle_id"]))
    return out


def _load_messages_by_chat(db: sqlite3.Connection) -> dict[int, list[sqlite3.Row]]:
    columns = _columns(db, "message")
    select = _select_clause(
        columns,
        {
            "ROWID": "message_id",
            "guid": "guid",
            "text": "text",
            "attributedBody": "attributedBody",
            "handle_id": "handle_id",
            "date": "date",
            "is_from_me": "is_from_me",
            "service": "service",
            "cache_has_attachments": "cache_has_attachments",
        },
        table="message",
    )
    if _table_exists(db, "chat_message_join"):
        rows = db.execute(
            f"""
            SELECT cmj.chat_id AS chat_id, {select}
            FROM message
            JOIN chat_message_join cmj ON cmj.message_id = message.ROWID
            """
        )
    else:
        rows = db.execute(f"SELECT COALESCE(handle_id, 0) AS chat_id, {select} FROM message")
    out: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        out.setdefault(int(row["chat_id"]), []).append(row)
    return out


def _load_attachments_by_message(db: sqlite3.Connection) -> dict[int, list[sqlite3.Row]]:
    if not _table_exists(db, "attachment") or not _table_exists(db, "message_attachment_join"):
        return {}
    columns = _columns(db, "attachment")
    select = _select_clause(
        columns,
        {
            "ROWID": "attachment_id",
            "guid": "guid",
            "filename": "filename",
            "mime_type": "mime_type",
            "transfer_name": "transfer_name",
            "total_bytes": "total_bytes",
        },
        table="attachment",
    )
    out: dict[int, list[sqlite3.Row]] = {}
    for row in db.execute(
        f"""
        SELECT maj.message_id AS message_id, {select}
        FROM attachment
        JOIN message_attachment_join maj ON maj.attachment_id = attachment.ROWID
        """
    ):
        out.setdefault(int(row["message_id"]), []).append(row)
    return out


def _build_thread(
    chat_id: int,
    chat: sqlite3.Row | None,
    chat_handle_ids: set[int],
    messages: list[sqlite3.Row],
    handles: dict[int, IMessageHandle],
) -> IMessageThread:
    handle_ids = set(chat_handle_ids)
    for message in messages:
        handle_id = _row_value(message, "handle_id")
        if handle_id:
            handle_ids.add(int(handle_id))
    source_thread_key = str(_row_value(chat, "guid") or _row_value(chat, "chat_identifier") or f"chat:{chat_id}")
    title = _chat_title(chat, handle_ids, handles)
    return IMessageThread(rowid=chat_id, source_thread_key=source_thread_key, title=title, handle_ids=handle_ids, messages=messages)


def _chat_title(chat: sqlite3.Row | None, handle_ids: set[int], handles: dict[int, IMessageHandle]) -> str:
    for column in ("display_name", "room_name"):
        value = _row_value(chat, column)
        if value:
            return str(value)
    names = [handles[handle_id].display_name for handle_id in sorted(handle_ids) if handle_id in handles]
    if names:
        if len(names) <= 3:
            return ", ".join(names)
        return f"{', '.join(names[:3])} +{len(names) - 3}"
    identifier = _row_value(chat, "chat_identifier")
    return str(identifier) if identifier else "iMessage Thread"


def _ensure_me(db: sqlite3.Connection) -> tuple[int, int, str]:
    identity_key = "person:imessage:me"
    identity_id = upsert_identity(db, stable_key=identity_key, display_name="Me", kind="person")
    account_id = upsert_account(
        db,
        identity_id=identity_id,
        source_kind="imessage",
        account_key="me",
        display_name="Me",
        raw_metadata={"isFromMe": True},
    )
    return identity_id, account_id, identity_key


def _ensure_handle(db: sqlite3.Connection, handle: IMessageHandle) -> tuple[int, int, str]:
    identity_key = f"person:imessage:{slugify(handle.account_key)}--{_short_hash(handle.account_key)}"
    identity_id = upsert_identity(db, stable_key=identity_key, display_name=handle.display_name, kind="person")
    account_id = upsert_account(
        db,
        identity_id=identity_id,
        source_kind="imessage",
        account_key=handle.account_key,
        display_name=handle.display_name,
        raw_metadata={"service": handle.service, "rowid": handle.rowid},
    )
    return identity_id, account_id, identity_key


def _thread_bounds(messages: list[sqlite3.Row]) -> tuple[str | None, str | None]:
    if not messages:
        return None, None
    ordered = sorted(messages, key=lambda row: (_row_value(row, "date") or 0, _row_value(row, "message_id") or 0))
    return _apple_date_to_iso(_row_value(ordered[0], "date")), _apple_date_to_iso(_row_value(ordered[-1], "date"))


def _message_key(message: sqlite3.Row) -> str:
    guid = _row_value(message, "guid")
    if guid:
        return str(guid)
    return f"message:{_row_value(message, 'message_id')}"


def _message_body(message: sqlite3.Row, attachments: list[sqlite3.Row]) -> str | None:
    text = _row_value(message, "text")
    if text:
        return str(text)
    attributed = _row_value(message, "attributedBody")
    extracted = _extract_attributed_body_text(attributed)
    if extracted:
        return extracted
    if attachments:
        return "\n".join("[Attachment]" for _ in attachments)
    return None


def _extract_attributed_body_text(value: object) -> str | None:
    if not isinstance(value, bytes) or not value:
        return None
    candidates: list[str] = []
    for encoding in ("utf-8", "utf-16"):
        try:
            decoded = value.decode(encoding, errors="ignore")
        except LookupError:
            continue
        cleaned = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]+", " ", decoded)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if cleaned:
            candidates.append(cleaned)
    if not candidates:
        return None
    best = max(candidates, key=len)
    marker = "NSString"
    if marker in best:
        best = best.split(marker, 1)[-1].strip()
    return best[:4000] or None


def _apple_date_to_iso(value: object) -> str:
    try:
        raw = int(value) if value is not None else 0
    except (TypeError, ValueError):
        raw = 0
    if not raw:
        return "1970-01-01T00:00:00Z"
    magnitude = abs(raw)
    if magnitude > 10_000_000_000_000_000:
        seconds = raw / 1_000_000_000
    elif magnitude > 10_000_000_000_000:
        seconds = raw / 1_000_000
    elif magnitude > 10_000_000_000:
        seconds = raw / 1_000
    else:
        seconds = raw
    unix_seconds = APPLE_EPOCH_UNIX_SECONDS + seconds
    return datetime.fromtimestamp(unix_seconds, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    return bool(db.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone())


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in db.execute(f"PRAGMA table_info({table})")}


def _select_clause(columns: set[str], requested: dict[str, str], *, table: str | None = None) -> str:
    selected = []
    for source_name, alias in requested.items():
        if source_name == "ROWID" or source_name in columns:
            if source_name == "ROWID":
                column = f"{table}.ROWID" if table else "ROWID"
            else:
                column = f"{table}.{source_name}" if table else source_name
            selected.append(f"{column} AS {alias}")
        else:
            selected.append(f"NULL AS {alias}")
    return ", ".join(selected)


def _row_value(row: sqlite3.Row | None, key: str) -> Any:
    if row is None:
        return None
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def _row_to_dict(row: sqlite3.Row | None, *, skip: set[str] | None = None) -> dict[str, object] | None:
    if row is None:
        return None
    ignored = skip or set()
    return {key: row[key] for key in row.keys() if key not in ignored}


def _short_hash(value: str, length: int = 10) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]
