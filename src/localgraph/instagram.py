from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
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


@dataclass
class InstagramExportScan:
    name: str
    path: str
    relative_path: str
    message_files: int = 0
    thread_folders: set[str] = field(default_factory=set)
    latest_modified_time: str | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "name": self.name,
            "path": self.path,
            "relativePath": self.relative_path,
            "messageFiles": self.message_files,
            "threadFolders": sorted(self.thread_folders),
            "latestModifiedTime": self.latest_modified_time,
        }


def scan_instagram_source(source_path: Path) -> dict[str, object]:
    source = source_path.expanduser().resolve()
    exports: dict[Path, InstagramExportScan] = {}
    if not source.exists():
        return {"sourceKind": "instagram", "sourcePath": str(source), "exports": [], "totalMessageFiles": 0}

    message_files = sorted(path for path in source.rglob("message_*.json") if path.is_file())
    for file_path in message_files:
        export_root = detect_export_root(source, file_path)
        relative_file = file_path.relative_to(export_root).as_posix()
        thread_folder = str(Path(relative_file).parent).replace("\\", "/")
        item = exports.get(export_root)
        if item is None:
            relative_export = export_root.relative_to(source).as_posix() if export_root != source else "."
            item = InstagramExportScan(
                name=export_root.name,
                path=str(export_root),
                relative_path=relative_export,
            )
            exports[export_root] = item
        item.message_files += 1
        item.thread_folders.add(thread_folder)
        modified = _iso_mtime(file_path)
        item.latest_modified_time = max(filter(None, [item.latest_modified_time, modified]))

    return {
        "sourceKind": "instagram",
        "sourcePath": str(source),
        "exports": [item.to_json() for _, item in sorted(exports.items(), key=lambda pair: pair[1].relative_path)],
        "totalMessageFiles": len(message_files),
    }


def detect_export_root(source_path: Path, file_path: Path) -> Path:
    parts = file_path.relative_to(source_path).parts
    if "your_instagram_activity" in parts:
        index = parts.index("your_instagram_activity")
        if index == 0:
            return source_path
        if index > 0:
            return source_path.joinpath(*parts[:index])
    if "messages" in parts:
        index = parts.index("messages")
        if index == 0:
            return source_path
        if index > 0:
            return source_path.joinpath(*parts[:index])
    return source_path


def _iso_mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def import_instagram_source(db, source_path: Path) -> dict[str, object]:
    scan = scan_instagram_source(source_path)
    totals = {
        "sourceKind": "instagram",
        "sourcePath": scan["sourcePath"],
        "exports": len(scan["exports"]),
        "threads": 0,
        "people": 0,
        "groups": 0,
        "messages": 0,
        "mediaObjects": 0,
        "warnings": [],
    }
    seen_people: set[str] = set()
    seen_groups: set[str] = set()

    for export in scan["exports"]:
        export_path = Path(str(export["path"]))
        source_import_id = upsert_source_import(
            db,
            source_kind="instagram",
            source_identifier=f"instagram:{export_path.resolve()}",
            source_path=str(export_path),
            raw_metadata=export,
        )
        for folder in discover_conversation_folders(export_path):
            result = import_instagram_conversation(db, export_path=export_path, folder=folder, source_import_id=source_import_id)
            totals["threads"] = int(totals["threads"]) + 1
            totals["messages"] = int(totals["messages"]) + int(result["messages"])
            totals["mediaObjects"] = int(totals["mediaObjects"]) + int(result["mediaObjects"])
            for key in result["people"]:
                seen_people.add(str(key))
            if result["group"] is not None:
                seen_groups.add(str(result["group"]))

    totals["people"] = len(seen_people)
    totals["groups"] = len(seen_groups)
    db.commit()
    return totals


def discover_conversation_folders(export_root: Path) -> list[Path]:
    folders = {
        path.parent
        for path in export_root.rglob("message_*.json")
        if path.is_file() and _looks_like_message_path(path.relative_to(export_root))
    }
    return sorted(folders)


def import_instagram_conversation(db, *, export_path: Path, folder: Path, source_import_id: int) -> dict[str, object]:
    message_files = sorted(folder.glob("message_*.json"), key=_message_file_number)
    chunks: list[tuple[Path, dict[str, Any]]] = []
    for file_path in message_files:
        try:
            chunks.append((file_path, json.loads(file_path.read_text(encoding="utf-8"))))
        except json.JSONDecodeError:
            continue

    first = chunks[0][1] if chunks else {}
    source_thread_key = folder.relative_to(export_path).as_posix()
    participants = _participants(first)
    messages = []
    for file_path, chunk in chunks:
        for raw_message in chunk.get("messages", []) if isinstance(chunk.get("messages"), list) else []:
            message = _normalize_instagram_message(raw_message, source_thread_key, file_path)
            messages.append(message)
            if message.sender_name and message.sender_name not in participants:
                participants.append(message.sender_name)
    messages.sort(key=lambda item: (item.sent_at, item.source_message_key))

    title = _decode_meta_string(first.get("title")) or _title_from_participants(participants) or folder.name
    thread_kind = "group" if len(participants) > 2 else "direct" if participants else "unknown"
    first_message_at = messages[0].sent_at if messages else None
    last_message_at = messages[-1].sent_at if messages else None
    thread_id = upsert_thread(
        db,
        source_kind="instagram",
        source_thread_key=source_thread_key,
        title=title,
        thread_kind=thread_kind,
        first_message_at=first_message_at,
        last_message_at=last_message_at,
        raw_metadata={
            "sourceImportId": source_import_id,
            "sourceFolder": str(folder),
            "sourcePaths": [str(path) for path in message_files],
            "title": first.get("title"),
            "participants": first.get("participants"),
        },
    )
    thread_key = f"instagram:{source_thread_key}"

    people: dict[str, tuple[int, int]] = {}
    for participant in participants:
        identity_key = f"instagram:person:{_account_key(participant)}"
        identity_id = upsert_identity(db, stable_key=identity_key, display_name=participant, kind="person")
        account_id = upsert_account(
            db,
            identity_id=identity_id,
            source_kind="instagram",
            account_key=_account_key(participant),
            display_name=participant,
        )
        upsert_thread_participant(db, thread_id=thread_id, identity_id=identity_id, account_id=account_id)
        people[participant] = (identity_id, account_id)

    group_key = None
    if thread_kind == "group":
        group_key = f"instagram:group:{source_thread_key}"
        group_id = upsert_identity(db, stable_key=group_key, display_name=title, kind="group")
        upsert_graph_edge(
            db,
            from_kind="identity",
            from_key=group_key,
            edge_kind="contains_thread",
            to_kind="thread",
            to_key=thread_key,
            source="instagram",
        )
        for participant, (identity_id, _) in people.items():
            upsert_graph_edge(
                db,
                from_kind="identity",
                from_key=group_key,
                edge_kind="has_member",
                to_kind="identity",
                to_key=f"instagram:person:{_account_key(participant)}",
                source="instagram",
            )
        _ = group_id

    media_count = 0
    for message in messages:
        identity_id, account_id = people.get(message.sender_name, (None, None))
        message_id = upsert_message(
            db,
            thread_id=thread_id,
            source_message_key=message.source_message_key,
            sender_identity_id=identity_id,
            sender_account_id=account_id,
            sent_at=message.sent_at,
            body_text=message.body_text,
            body_format=message.body_format,
            raw=message.raw,
        )
        for index, attachment in enumerate(message.attachments):
            object_key = f"instagram:{message.source_message_key}:media:{index}"
            upsert_media_object(
                db,
                message_id=message_id,
                object_key=object_key,
                source_uri=attachment.get("source_uri"),
                local_path=_attachment_local_path(folder, attachment.get("source_uri")),
                mime_type=attachment.get("mime_type"),
                raw_metadata=attachment,
            )
            media_count += 1

    return {"people": [f"instagram:person:{_account_key(name)}" for name in participants], "group": group_key, "messages": len(messages), "mediaObjects": media_count}


@dataclass(frozen=True)
class NormalizedInstagramMessage:
    source_message_key: str
    sent_at: str
    sender_name: str
    body_text: str | None
    body_format: str
    attachments: list[dict[str, Any]]
    raw: dict[str, Any]


def _normalize_instagram_message(raw: dict[str, Any], source_thread_key: str, source_path: Path) -> NormalizedInstagramMessage:
    timestamp_ms = int(raw.get("timestamp_ms") or 0)
    sender_name = _decode_meta_string(raw.get("sender_name")) or "Unknown"
    content = _decode_meta_string(raw.get("content"))
    attachments = _collect_attachments(raw)
    share = raw.get("share") if isinstance(raw.get("share"), dict) else None
    body_text = content
    body_format = "plain"
    if body_text is None and share:
        body_text = _decode_meta_string(share.get("link") or share.get("href") or share.get("share_text") or share.get("text"))
        body_format = "share"
    if body_text is None and _is_unavailable(raw):
        body_text = "<Message unavailable>"
        body_format = "unavailable"
    if body_text is None and attachments:
        body_text = "<Media message>"
        body_format = "media"
    if body_text is None and (raw.get("call_duration") or raw.get("missed") or "call" in str(raw.get("type", "")).lower()):
        body_text = "<Call event>"
        body_format = "call"
    sent_at = _instagram_timestamp(timestamp_ms)
    key_material = {
        "sourceThreadKey": source_thread_key,
        "timestampMs": timestamp_ms,
        "senderName": sender_name,
        "content": raw.get("content"),
        "attachments": attachments,
        "share": share,
        "type": raw.get("type"),
        "sourcePath": str(source_path.name),
    }
    return NormalizedInstagramMessage(
        source_message_key=_sha256(key_material),
        sent_at=sent_at,
        sender_name=sender_name,
        body_text=body_text,
        body_format=body_format,
        attachments=attachments,
        raw=_json_safe(raw),
    )


def _collect_attachments(raw: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for field, kind in (
        ("photos", "photo"),
        ("videos", "video"),
        ("audio_files", "audio"),
        ("files", "file"),
        ("gifs", "gif"),
    ):
        for item in raw.get(field, []) if isinstance(raw.get(field), list) else []:
            out.append(_attachment(kind, item))
    if isinstance(raw.get("sticker"), dict):
        out.append(_attachment("sticker", raw["sticker"]))
    return out


def _attachment(kind: str, item: dict[str, Any]) -> dict[str, Any]:
    uri = _decode_meta_string(item.get("uri") or item.get("href") or item.get("src"))
    return {
        "kind": kind,
        "source_uri": uri,
        "original_filename": Path(uri).name if uri else None,
        "mime_type": _decode_meta_string(item.get("mime_type")),
        "raw": _json_safe(item),
    }


def _participants(first_chunk: dict[str, Any]) -> list[str]:
    raw = first_chunk.get("participants")
    names = []
    if isinstance(raw, list):
        for participant in raw:
            if isinstance(participant, dict):
                name = _decode_meta_string(participant.get("name"))
                if name:
                    names.append(name)
    return _dedupe(names)


def _looks_like_message_path(path: Path) -> bool:
    parts = path.parts
    return "messages" in parts or any(part in {"inbox", "archived_threads", "message_requests", "filtered_threads"} for part in parts)


def _title_from_participants(participants: list[str]) -> str | None:
    if not participants:
        return None
    return ", ".join(participants[:4]) + ("..." if len(participants) > 4 else "")


def _account_key(display_name: str) -> str:
    key = unicodedata.normalize("NFKD", display_name)
    key = "".join(ch for ch in key if not unicodedata.combining(ch))
    key = re.sub(r"[^a-z0-9]+", "-", key.lower()).strip("-")
    return key or _sha256(display_name)[:12]


def _instagram_timestamp(timestamp_ms: int) -> str:
    if timestamp_ms <= 0:
        return datetime.fromtimestamp(0, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _message_file_number(path: Path) -> int:
    match = re.search(r"message_(\d+)\.json$", path.name)
    return int(match.group(1)) if match else 0


def _attachment_local_path(folder: Path, source_uri: str | None) -> str | None:
    if not source_uri:
        return None
    candidate = (folder / source_uri).resolve()
    try:
        candidate.relative_to(folder.resolve())
    except ValueError:
        return None
    return str(candidate) if candidate.exists() else None


def _is_unavailable(raw: dict[str, Any]) -> bool:
    if raw.get("is_unsent") or raw.get("is_deleted") or raw.get("deleted") or raw.get("is_unavailable"):
        return True
    allowed = {"sender_name", "timestamp_ms", "is_geoblocked_for_viewer", "is_unsent_image_by_messenger_kid_parent"}
    return bool(raw.get("sender_name") and raw.get("timestamp_ms") and set(raw) <= allowed)


def _decode_meta_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = unicodedata.normalize("NFC", value)
    if not re.search(r"[ÃÂâðÐÑÌï�\u0080-\u009f]", normalized):
        return normalized
    best = normalized
    best_score = _mojibake_score(best)
    current = normalized
    for _ in range(2):
        try:
            repaired = unicodedata.normalize("NFC", current.encode("latin1").decode("utf-8"))
        except UnicodeError:
            break
        score = _mojibake_score(repaired)
        if score < best_score:
            best = repaired
            best_score = score
            current = repaired
    return best


def _mojibake_score(value: str) -> int:
    return (
        len(re.findall(r"(?:Ã.|Â.|â.|ð.|Ð.|Ñ.|Ì.|ï.)", value)) * 8
        + len(re.findall(r"[\u0080-\u009f]", value)) * 12
        + value.count("�") * 25
    )


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


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        return json.loads(json.dumps(value, default=str))
