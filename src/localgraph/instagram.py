from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .slug import slugify
from .store import (
    ImportStats,
    json_dumps,
    link_thread_participant,
    upsert_account,
    upsert_graph_edge,
    upsert_identity,
    upsert_media_object,
    upsert_message,
    upsert_source_import,
    upsert_thread,
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


@dataclass
class InstagramThreadImport:
    source_thread_key: str
    export_root: Path
    thread_folder: str
    message_files: set[str] = field(default_factory=set)
    participants: dict[str, str] = field(default_factory=dict)
    messages: list[dict[str, object]] = field(default_factory=list)


def import_instagram_source(
    db: sqlite3.Connection,
    source_path: Path,
    *,
    selected_export_paths: list[Path] | None = None,
) -> dict[str, object]:
    source = source_path.expanduser().resolve()
    stats = ImportStats(source_kind="instagram", source_path=str(source))
    if not source.exists():
        stats.skipped = True
        stats.note = "source path does not exist"
        return stats.to_json()

    selected_exports = {path.expanduser().resolve() for path in selected_export_paths or []}
    threads: dict[str, InstagramThreadImport] = {}
    export_roots: set[Path] = set()
    message_files = sorted(path for path in source.rglob("message_*.json") if path.is_file())
    for file_path in message_files:
        export_root = detect_export_root(source, file_path)
        if selected_exports and export_root.resolve() not in selected_exports:
            continue
        export_roots.add(export_root)
        relative_file = file_path.relative_to(export_root).as_posix()
        thread_folder = str(Path(relative_file).parent).replace("\\", "/")
        thread = threads.get(thread_folder)
        if thread is None:
            thread = InstagramThreadImport(
                source_thread_key=thread_folder,
                export_root=export_root,
                thread_folder=thread_folder,
            )
            threads[thread_folder] = thread
        thread.message_files.add(relative_file)

        payload = json.loads(file_path.read_text(encoding="utf-8"))
        for participant in payload.get("participants", []):
            name = _decode_instagram_text(participant.get("name"))
            if name:
                thread.participants[_participant_key(name)] = name
        for index, message in enumerate(payload.get("messages", [])):
            if not isinstance(message, dict):
                continue
            sender = _decode_instagram_text(message.get("sender_name"))
            if sender:
                thread.participants[_participant_key(sender)] = sender
            thread.messages.append(
                {
                    "file": relative_file,
                    "fileIndex": index,
                    "message": message,
                }
            )

    for export_root in sorted(export_roots):
        relative_export = export_root.relative_to(source).as_posix() if export_root != source else "."
        upsert_source_import(
            db,
            source_kind="instagram",
            source_identifier=f"instagram:{relative_export}",
            source_path=str(export_root),
            raw_metadata={"relativePath": relative_export},
        )
        stats.imports += 1

    account_cache: dict[str, tuple[int, int, str]] = {}
    group_count = 0
    for thread in threads.values():
        participants = list(thread.participants.values())
        title = _thread_title(thread.thread_folder, participants)
        thread_kind = "group" if len(participants) > 2 else "direct"
        ordered_messages = sorted(
            thread.messages,
            key=lambda item: (
                _instagram_timestamp_ms(item["message"]),
                str(item["file"]),
                int(item["fileIndex"]),
            ),
        )
        sent_times = [_instagram_sent_at(item["message"]) for item in ordered_messages]
        thread_id = upsert_thread(
            db,
            source_kind="instagram",
            source_thread_key=thread.source_thread_key,
            title=title,
            thread_kind=thread_kind,
            first_message_at=sent_times[0] if sent_times else None,
            last_message_at=sent_times[-1] if sent_times else None,
            raw_metadata={
                "exportRoot": str(thread.export_root),
                "threadFolder": thread.thread_folder,
                "messageFiles": sorted(thread.message_files),
                "participants": participants,
            },
        )
        stats.threads += 1

        participant_refs: dict[str, tuple[int, int, str]] = {}
        for name in participants:
            key = _participant_key(name)
            ref = account_cache.get(key)
            if ref is None:
                identity_key = f"person:instagram:{slugify(name)}--{_short_hash(key)}"
                identity_id = upsert_identity(db, stable_key=identity_key, display_name=name, kind="person")
                account_id = upsert_account(
                    db,
                    identity_id=identity_id,
                    source_kind="instagram",
                    account_key=key,
                    display_name=name,
                    raw_metadata={"name": name},
                )
                ref = (identity_id, account_id, identity_key)
                account_cache[key] = ref
                stats.people += 1
            participant_refs[key] = ref
            link_thread_participant(db, thread_id=thread_id, identity_id=ref[0], account_id=ref[1])
            upsert_graph_edge(
                db,
                from_kind="thread",
                from_key=f"instagram:{thread.source_thread_key}",
                edge_kind="participant",
                to_kind="identity",
                to_key=ref[2],
            )

        if thread_kind == "group":
            group_key = f"group:instagram:{thread.source_thread_key}"
            group_id = upsert_identity(db, stable_key=group_key, display_name=title, kind="group")
            group_count += 1
            for identity_id, _, identity_key in participant_refs.values():
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
                    from_kind="thread",
                    from_key=f"instagram:{thread.source_thread_key}",
                    edge_kind="represents",
                    to_kind="identity",
                    to_key=group_key,
                )
            if group_id:
                pass

        occurrence_counts: dict[str, int] = {}
        for item in ordered_messages:
            raw_message = item["message"]
            sender = _decode_instagram_text(raw_message.get("sender_name"))
            sender_ref = participant_refs.get(_participant_key(sender)) if sender else None
            body_text = _instagram_body(raw_message)
            base_key = _instagram_message_base_key(raw_message)
            occurrence_counts[base_key] = occurrence_counts.get(base_key, 0) + 1
            source_message_key = f"{base_key}:{occurrence_counts[base_key]}"
            message_id = upsert_message(
                db,
                thread_id=thread_id,
                source_message_key=source_message_key,
                sender_identity_id=sender_ref[0] if sender_ref else None,
                sender_account_id=sender_ref[1] if sender_ref else None,
                sent_at=_instagram_sent_at(raw_message),
                body_text=body_text,
                raw_json={
                    "file": item["file"],
                    "fileIndex": item["fileIndex"],
                    "message": _decode_instagram_value(raw_message),
                },
            )
            stats.messages += 1
            for media_index, media in enumerate(_instagram_media_items(raw_message)):
                object_key = f"instagram:{thread.source_thread_key}:{source_message_key}:{media_index}"
                upsert_media_object(
                    db,
                    message_id=message_id,
                    object_key=object_key,
                    source_uri=media.get("uri"),
                    local_path=media.get("uri"),
                    mime_type=media.get("mimeType"),
                    raw_metadata=media,
                )
                stats.media_objects += 1

    stats.groups = group_count
    db.commit()
    return stats.to_json()


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


def _decode_instagram_value(value: Any) -> Any:
    if isinstance(value, str):
        return _decode_instagram_text(value)
    if isinstance(value, list):
        return [_decode_instagram_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _decode_instagram_value(item) for key, item in value.items()}
    return value


def _decode_instagram_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    try:
        return text.encode("latin-1").decode("utf-8")
    except UnicodeError:
        return text


def _participant_key(name: str) -> str:
    return _decode_instagram_text(name).strip().casefold()


def _short_hash(value: str, length: int = 10) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def _thread_title(thread_folder: str, participants: list[str]) -> str:
    if participants:
        if len(participants) <= 3:
            return ", ".join(participants)
        return f"{', '.join(participants[:3])} +{len(participants) - 3}"
    fallback = Path(thread_folder).name.replace("_", " ").strip()
    return fallback or "Instagram Thread"


def _instagram_timestamp_ms(message: object) -> int:
    if not isinstance(message, dict):
        return 0
    value = message.get("timestamp_ms") or message.get("timestamp") or 0
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return 0
    if timestamp and timestamp < 10_000_000_000:
        timestamp *= 1000
    return timestamp


def _instagram_sent_at(message: object) -> str:
    timestamp_ms = _instagram_timestamp_ms(message)
    if not timestamp_ms:
        return "1970-01-01T00:00:00Z"
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _instagram_body(message: dict[str, object]) -> str | None:
    content = _decode_instagram_text(message.get("content")).strip()
    parts = [content] if content else []
    if "share" in message:
        share = message.get("share")
        if isinstance(share, dict):
            link = _decode_instagram_text(share.get("link"))
            if link:
                parts.append(f"[Shared link: {link}]")
    media_labels = []
    for media in _instagram_media_items(message):
        label = media.get("label")
        if label:
            media_labels.append(f"[{label}]")
    parts.extend(media_labels)
    return "\n".join(parts) if parts else None


def _instagram_media_items(message: dict[str, object]) -> list[dict[str, object]]:
    fields = {
        "photos": ("Photo", "image"),
        "videos": ("Video", "video"),
        "audio_files": ("Audio", "audio"),
        "files": ("File", None),
        "gifs": ("GIF", "image/gif"),
    }
    out: list[dict[str, object]] = []
    for field, (label, default_mime) in fields.items():
        values = message.get(field)
        if not isinstance(values, list):
            continue
        for value in values:
            if not isinstance(value, dict):
                continue
            decoded = _decode_instagram_value(value)
            uri = decoded.get("uri") if isinstance(decoded, dict) else None
            out.append(
                {
                    "label": label,
                    "field": field,
                    "uri": str(uri) if uri else None,
                    "mimeType": default_mime,
                    "raw": decoded,
                }
            )
    return out


def _instagram_message_base_key(message: dict[str, object]) -> str:
    timestamp = _instagram_timestamp_ms(message)
    sender = _participant_key(_decode_instagram_text(message.get("sender_name")))
    body = _instagram_body(message) or ""
    raw_fingerprint = json_dumps(
        {
            "timestamp": timestamp,
            "sender": sender,
            "body": body,
            "media": _instagram_media_items(message),
        }
    )
    return f"{timestamp}:{_short_hash(raw_fingerprint, 16)}"
