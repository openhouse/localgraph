from __future__ import annotations

import hashlib
import plistlib
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .store import (
    json_dumps,
    upsert_account,
    upsert_graph_edge,
    upsert_identity,
    upsert_media_object,
    upsert_message,
    upsert_source_import,
    upsert_thread,
    upsert_thread_participant,
)

APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)


def import_imessage_chat_db(db, chat_db_path: Path, *, limit: int | None = None, immutable: bool = False) -> dict[str, object]:
    source = chat_db_path.expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"iMessage chat database does not exist: {source}")

    source_import_id = upsert_source_import(
        db,
        source_kind="imessage",
        source_identifier=f"imessage:{source}",
        source_path=str(source),
        raw_metadata={"chatDbPath": str(source), "immutable": immutable},
    )
    immutable_flag = "&immutable=1" if immutable else ""
    source_db = sqlite3.connect(f"file:{source}?mode=ro{immutable_flag}", uri=True)
    source_db.row_factory = sqlite3.Row
    try:
        chats = _read_chats(source_db)
        chat_handles = _read_chat_handles(source_db)
        rows = _read_messages(source_db, limit=limit)
        attachments_by_message = _read_attachments(source_db, [int(row["message_rowid"]) for row in rows])
    finally:
        source_db.close()

    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[_thread_key_for_row(row)].append(row)

    me_identity_id = upsert_identity(db, stable_key="imessage:person:me", display_name="Me", kind="person")
    me_account_id = upsert_account(db, identity_id=me_identity_id, source_kind="imessage", account_key="me", display_name="Me")
    seen_people = {"imessage:person:me"}
    seen_groups: set[str] = set()
    imported_messages = 0
    imported_media = 0

    for source_thread_key, thread_rows in sorted(grouped.items()):
        chat_id = thread_rows[0]["chat_id"]
        chat = chats.get(chat_id) if chat_id is not None else None
        handles = list(chat_handles.get(chat_id, [])) if chat_id is not None else []
        for row in thread_rows:
            handle_value = _display_handle(row["handle_id_value"])
            if handle_value and handle_value not in handles:
                handles.append(handle_value)
        handles = _dedupe(handles)
        title = _chat_title(chat, handles)
        thread_kind = "group" if _is_group_chat(chat, handles) else "direct"
        normalized_messages = [_normalize_message(row, attachments_by_message.get(int(row["message_rowid"]), [])) for row in thread_rows]
        normalized_messages.sort(key=lambda item: (item.sent_at, item.source_message_key))
        first_message_at = normalized_messages[0].sent_at if normalized_messages else None
        last_message_at = normalized_messages[-1].sent_at if normalized_messages else None
        thread_id = upsert_thread(
            db,
            source_kind="imessage",
            source_thread_key=source_thread_key,
            title=title,
            thread_kind=thread_kind,
            first_message_at=first_message_at,
            last_message_at=last_message_at,
            raw_metadata={"sourceImportId": source_import_id, "chat": _row_to_dict(chat), "handles": handles},
        )
        thread_key = f"imessage:{source_thread_key}"

        upsert_thread_participant(db, thread_id=thread_id, identity_id=me_identity_id, account_id=me_account_id)
        handle_accounts: dict[str, tuple[int, int]] = {}
        for handle in handles:
            identity_key = f"imessage:person:{_handle_key(handle)}"
            identity_id = upsert_identity(db, stable_key=identity_key, display_name=handle, kind="person")
            account_id = upsert_account(db, identity_id=identity_id, source_kind="imessage", account_key=_handle_key(handle), display_name=handle)
            upsert_thread_participant(db, thread_id=thread_id, identity_id=identity_id, account_id=account_id)
            handle_accounts[handle] = (identity_id, account_id)
            seen_people.add(identity_key)

        if thread_kind == "group":
            group_key = f"imessage:group:{source_thread_key}"
            upsert_identity(db, stable_key=group_key, display_name=title, kind="group")
            upsert_graph_edge(
                db,
                from_kind="identity",
                from_key=group_key,
                edge_kind="contains_thread",
                to_kind="thread",
                to_key=thread_key,
                source="imessage",
            )
            for handle in handles:
                upsert_graph_edge(
                    db,
                    from_kind="identity",
                    from_key=group_key,
                    edge_kind="has_member",
                    to_kind="identity",
                    to_key=f"imessage:person:{_handle_key(handle)}",
                    source="imessage",
                )
            seen_groups.add(group_key)

        for message in normalized_messages:
            if message.is_from_me:
                sender_identity_id, sender_account_id = me_identity_id, me_account_id
            else:
                sender_identity_id, sender_account_id = handle_accounts.get(message.handle, (None, None))
            message_id = upsert_message(
                db,
                thread_id=thread_id,
                source_message_key=message.source_message_key,
                sender_identity_id=sender_identity_id,
                sender_account_id=sender_account_id,
                sent_at=message.sent_at,
                body_text=message.body_text,
                body_format=message.body_format,
                raw=message.raw,
            )
            imported_messages += 1
            for index, attachment in enumerate(message.attachments):
                upsert_media_object(
                    db,
                    message_id=message_id,
                    object_key=f"imessage:{message.source_message_key}:attachment:{index}",
                    source_uri=attachment.get("filename") or attachment.get("transfer_name"),
                    local_path=attachment.get("filename"),
                    mime_type=attachment.get("mime_type"),
                    raw_metadata=attachment,
                )
                imported_media += 1

    db.commit()
    return {
        "sourceKind": "imessage",
        "sourcePath": str(source),
        "limitedToMessages": limit,
        "immutable": immutable,
        "threads": len(grouped),
        "people": len(seen_people),
        "groups": len(seen_groups),
        "messages": imported_messages,
        "mediaObjects": imported_media,
    }


@dataclass(frozen=True)
class NormalizedIMessage:
    source_message_key: str
    sent_at: str
    handle: str
    is_from_me: bool
    body_text: str | None
    body_format: str
    attachments: list[dict[str, Any]]
    raw: dict[str, Any]


def _read_chats(db: sqlite3.Connection) -> dict[int, sqlite3.Row]:
    rows = db.execute(
        """
        SELECT ROWID AS chat_id, guid, chat_identifier, display_name, service_name, room_name
        FROM chat
        """
    ).fetchall()
    return {int(row["chat_id"]): row for row in rows}


def _read_chat_handles(db: sqlite3.Connection) -> dict[int, list[str]]:
    if not _table_exists(db, "chat_handle_join"):
        return {}
    rows = db.execute(
        """
        SELECT chj.chat_id, h.id AS handle_id_value
        FROM chat_handle_join chj
        JOIN handle h ON h.ROWID = chj.handle_id
        ORDER BY chj.chat_id, h.id
        """
    ).fetchall()
    grouped: dict[int, list[str]] = defaultdict(list)
    for row in rows:
        handle = _display_handle(row["handle_id_value"])
        if handle:
            grouped[int(row["chat_id"])].append(handle)
    return {chat_id: _dedupe(values) for chat_id, values in grouped.items()}


def _read_messages(db: sqlite3.Connection, *, limit: int | None = None) -> list[sqlite3.Row]:
    sql = """
        SELECT
          m.ROWID AS message_rowid,
          m.guid AS message_guid,
          m.text,
          m.attributedBody,
          m.date,
          m.is_from_me,
          m.service,
          h.id AS handle_id_value,
          c.ROWID AS chat_id,
          c.guid AS chat_guid,
          c.chat_identifier,
          c.display_name,
          c.service_name,
          c.room_name
        FROM message m
        LEFT JOIN handle h ON h.ROWID = m.handle_id
        LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        LEFT JOIN chat c ON c.ROWID = cmj.chat_id
    """
    if limit is not None and limit > 0:
        sql += " ORDER BY m.ROWID DESC LIMIT ?"
        rows = db.execute(sql, (limit,)).fetchall()
        return sorted(rows, key=lambda row: (row["chat_id"] if row["chat_id"] is not None else -1, row["date"] or 0, row["message_rowid"]))
    sql += " ORDER BY COALESCE(c.ROWID, -1), m.date, m.ROWID"
    return db.execute(sql).fetchall()


def _read_attachments(db: sqlite3.Connection, message_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    if not _table_exists(db, "attachment") or not _table_exists(db, "message_attachment_join"):
        return {}
    if not message_ids:
        return {}
    placeholders = ",".join("?" for _ in message_ids)
    rows = db.execute(
        f"""
        SELECT
          maj.message_id,
          a.ROWID AS attachment_id,
          a.filename,
          a.mime_type,
          a.transfer_name,
          a.total_bytes
        FROM message_attachment_join maj
        JOIN attachment a ON a.ROWID = maj.attachment_id
        WHERE maj.message_id IN ({placeholders})
        ORDER BY maj.message_id, a.ROWID
        """,
        message_ids,
    ).fetchall()
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[int(row["message_id"])].append(_row_to_dict(row))
    return grouped


def _normalize_message(row: sqlite3.Row, attachments: list[dict[str, Any]]) -> NormalizedIMessage:
    handle = _display_handle(row["handle_id_value"])
    text = row["text"] or _extract_attributed_body(row["attributedBody"])
    body_format = "plain" if text else "media" if attachments else "empty"
    sent_at = _apple_timestamp(row["date"])
    key = row["message_guid"] or _sha256({"rowid": row["message_rowid"], "sentAt": sent_at, "handle": handle, "text": text})
    raw = {
        "messageRowId": row["message_rowid"],
        "guid": row["message_guid"],
        "date": row["date"],
        "isFromMe": bool(row["is_from_me"]),
        "service": row["service"],
        "handle": handle,
        "chatId": row["chat_id"],
        "chatGuid": row["chat_guid"],
        "hasAttributedBody": row["attributedBody"] is not None,
    }
    return NormalizedIMessage(
        source_message_key=str(key),
        sent_at=sent_at,
        handle=handle,
        is_from_me=bool(row["is_from_me"]),
        body_text=text or ("<Attachment>" if attachments else None),
        body_format=body_format,
        attachments=attachments,
        raw=raw,
    )


def _thread_key_for_row(row: sqlite3.Row) -> str:
    if row["chat_guid"]:
        return str(row["chat_guid"])
    handle = _display_handle(row["handle_id_value"]) or "unknown"
    return f"direct:{_handle_key(handle)}"


def _chat_title(chat: sqlite3.Row | None, handles: list[str]) -> str:
    if chat is not None:
        for field in ("display_name", "chat_identifier", "room_name", "guid"):
            value = chat[field]
            if value:
                return str(value)
    if handles:
        return ", ".join(handles[:4]) + ("..." if len(handles) > 4 else "")
    return "Untitled iMessage conversation"


def _is_group_chat(chat: sqlite3.Row | None, handles: list[str]) -> bool:
    if len(handles) > 1:
        return True
    if chat is None:
        return False
    return bool(chat["room_name"] or (chat["display_name"] and len(handles) != 1))


def _display_handle(value: Any) -> str:
    return str(value).strip() if value else ""


def _handle_key(value: str) -> str:
    normalized = value.strip().lower()
    if not normalized:
        return "unknown"
    return re.sub(r"[^a-z0-9@+._-]+", "-", normalized).strip("-") or _sha256(value)[:12]


def _apple_timestamp(value: Any) -> str:
    try:
        raw = int(value or 0)
    except (TypeError, ValueError):
        raw = 0
    if raw > 10**14:
        seconds = raw / 1_000_000_000
    elif raw > 10**11:
        seconds = raw / 1_000
    else:
        seconds = raw
    return (APPLE_EPOCH + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _extract_attributed_body(value: Any) -> str | None:
    if value is None:
        return None
    data = bytes(value)
    try:
        plist = plistlib.loads(data)
    except Exception:
        plist = None
    strings: list[str] = []
    if plist is not None:
        _collect_plist_strings(plist, strings)
    if not strings:
        strings.extend(_scan_blob_strings(data))
    cleaned = [text.strip() for text in strings if _looks_like_message_text(text)]
    if not cleaned:
        return None
    return max(cleaned, key=len)


def _collect_plist_strings(value: Any, out: list[str]) -> None:
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, bytes):
        out.extend(_scan_blob_strings(value))
    elif isinstance(value, dict):
        for item in value.values():
            _collect_plist_strings(item, out)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_plist_strings(item, out)


def _scan_blob_strings(data: bytes) -> list[str]:
    strings: list[str] = []
    for encoding in ("utf-8", "utf-16le", "utf-16be"):
        decoded = data.decode(encoding, errors="ignore")
        strings.extend(re.findall(r"[\w\s.,!?@#$%&*()+=:/;'\"-]{3,}", decoded))
    return strings


def _looks_like_message_text(value: str) -> bool:
    if not value or value.startswith("$") or value in {"NSString", "NSDictionary", "NSNumber", "NSObject"}:
        return False
    return any(ch.isalnum() for ch in value)


def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    return bool(db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def _dedupe(values: list[str]) -> list[str]:
    out = []
    seen = set()
    for value in values:
        if value not in seen:
            out.append(value)
            seen.add(value)
    return out


def _sha256(value: Any) -> str:
    text = value if isinstance(value, str) else json_dumps(value)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
