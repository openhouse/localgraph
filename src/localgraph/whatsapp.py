"""Private, explicitly bound WhatsApp exports; native acquisition is a separate job.

TXT exports do not carry authoritative account/chat/message IDs. Operator binding,
explicit date convention, immutable packets and conservative coverage are mandatory.
"""
from __future__ import annotations

import hashlib
import io
import json
import mimetypes
import os
import plistlib
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import uuid
import zipfile
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo
from urllib.parse import quote

from .automation import instagram_sync_lock
from .paths import Workspace
from .render import _write_thread_view
from .schema import connect, initialize_schema
from .slug import stable_view_name


LABEL = "com.openhouse.localgraph.whatsapp-sync"
MAX_ARCHIVE_BYTES = 2 * 1024**3
MAX_TEXT_BYTES = 64 * 1024**2
HEADER = re.compile(r"^[\u200e\u200f\ufeff]*(?:\[(?P<bracket>\d{1,2}/\d{1,2}/\d{2,4},? .+?)\] |(?P<android>\d{1,2}/\d{1,2}/\d{2,4},? .+?) - )(?P<rest>.*)$")
ATTACHED = re.compile(r"<attached: ([^>]+)>")


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("WhatsApp acquisition time requires a timezone")
    return parsed.astimezone(timezone.utc)


def _key(value: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", value) or value in {".", ".."}:
        raise ValueError("invalid WhatsApp local key")
    return value


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("invalid WhatsApp state")
    return value


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            os.chmod(temporary, 0o600)
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def chats(workspace: Workspace) -> list[dict]:
    records = _load(workspace.config_path).get("imports", {}).get("whatsapp", {}).get("chats", {})
    if not isinstance(records, dict):
        raise ValueError("invalid WhatsApp registry")
    return [dict(record) for _, record in sorted(records.items())]


def _chat(workspace: Workspace, account: str, chat: str) -> dict:
    identity = f"{_key(account)}:{_key(chat)}"
    for record in chats(workspace):
        if record["sourceKey"] == identity and record["enabled"]:
            return record
    raise ValueError("WhatsApp chat is not explicitly enabled")


def _root(workspace: Workspace, record: dict) -> Path:
    return workspace.sources_dir / "whatsapp" / _key(record["accountKey"]) / _key(record["chatKey"])


def _status_path(workspace: Workspace, record: dict) -> Path:
    return workspace.state_dir / "whatsapp" / record["accountKey"] / f"{record['chatKey']}.json"


def configure_chat(workspace: Workspace, *, account_key: str, chat_key: str, title: str,
                   kind: str, date_order: str, timezone_name: str, enabled: bool = True) -> dict:
    account_key, chat_key = _key(account_key), _key(chat_key)
    if kind not in {"direct", "group", "unknown"} or date_order not in {"mdy", "dmy"} or not title.strip():
        raise ValueError("invalid WhatsApp chat configuration")
    ZoneInfo(timezone_name)
    with instagram_sync_lock(workspace) as acquired:
        if not acquired:
            raise ValueError("Localgraph writer is busy")
        workspace.ensure_workspace(force=False)
        config = _load(workspace.config_path)
        records = config.setdefault("imports", {}).setdefault("whatsapp", {}).setdefault("chats", {})
        source_key = f"{account_key}:{chat_key}"
        old = records.get(source_key, {})
        # Parsing interpretation is immutable once custody exists. Never silently reinterpret history.
        if old and (old["dateOrder"], old["timezone"]) != (date_order, timezone_name):
            raise ValueError("date convention changes require explicit migration")
        record = {"accountKey": account_key, "chatKey": chat_key, "sourceKey": source_key,
                  "title": title.strip(), "viewLabel": old.get("viewLabel", title.strip()),
                  "kind": kind, "dateOrder": date_order, "timezone": timezone_name,
                  "enabled": enabled, "acquisitionIntervalHours": 24}
        records[source_key] = record
        _write(workspace.config_path, config)
        _root(workspace, record).mkdir(parents=True, exist_ok=True, mode=0o700)
    return record


def parse_transcript(text: str, *, date_order: str, timezone_name: str) -> list[dict]:
    if date_order not in {"mdy", "dmy"}:
        raise ValueError("explicit WhatsApp date convention required")
    zone = ZoneInfo(timezone_name)
    result: list[dict] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        match = HEADER.match(line)
        if not match:
            if re.match(r"^[\u200e\u200f\ufeff]*\[?\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}[, ]", line):
                raise ValueError("unsupported timestamp-looking WhatsApp record")
            if not result:
                if not line.strip():
                    continue
                raise ValueError("unsupported WhatsApp transcript header")
            result[-1]["body"] += "\n" + line
            continue
        raw_time = match["bracket"] or match["android"]
        normalized = re.sub(r"\s+", " ", raw_time.replace(",", "")).strip()
        prefix = "%m/%d/" if date_order == "mdy" else "%d/%m/"
        local = None
        for year in ("%y", "%Y"):
            for clock in ("%I:%M:%S %p", "%I:%M %p", "%H:%M:%S", "%H:%M"):
                try:
                    local = datetime.strptime(normalized, prefix + year + " " + clock)
                    break
                except ValueError:
                    continue
            if local is not None:
                break
        if local is None:
            raise ValueError("invalid WhatsApp timestamp")
        aware = local.replace(tzinfo=zone)
        if aware.utcoffset() != local.replace(tzinfo=zone, fold=1).utcoffset():
            raise ValueError("ambiguous or nonexistent WhatsApp local timestamp")
        if aware.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != local:
            raise ValueError("nonexistent WhatsApp local timestamp")
        rest = match["rest"].lstrip("\u200e\u200f")
        sender, separator, body = rest.partition(": ")
        result.append({"sentAt": _iso(aware), "sourceTimestamp": raw_time,
                       "sender": sender if separator else None, "body": body if separator else rest,
                       "sourceLine": line_number})
    if not result:
        raise ValueError("empty WhatsApp transcript")
    return result


def _read_bundle(data: bytes, record: dict) -> tuple[list[dict], dict[str, bytes]]:
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ValueError("WhatsApp archive exceeds size limit")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as bundle:
            infos = bundle.infolist()
            names = [entry.filename for entry in infos]
            if len(infos) > 100000 or len(set(names)) != len(names) or sum(i.file_size for i in infos) > MAX_ARCHIVE_BYTES:
                raise ValueError("invalid WhatsApp archive size or duplicate members")
            for entry in infos:
                path = PurePosixPath(entry.filename)
                if path.is_absolute() or ".." in path.parts or "\\" in entry.filename or stat.S_ISLNK(entry.external_attr >> 16):
                    raise ValueError("unsafe WhatsApp archive member")
            transcripts = [i for i in infos if PurePosixPath(i.filename).name == "_chat.txt"]
            if len(transcripts) != 1 or transcripts[0].file_size > MAX_TEXT_BYTES:
                raise ValueError("one bounded _chat.txt is required")
            messages = parse_transcript(bundle.read(transcripts[0]).decode("utf-8-sig"),
                                        date_order=record["dateOrder"], timezone_name=record["timezone"])
            media = {i.filename: bundle.read(i) for i in infos
                     if not i.is_dir() and i != transcripts[0] and not i.filename.startswith("__MACOSX/")}
            return messages, media
    except (zipfile.BadZipFile, UnicodeError, RuntimeError, NotImplementedError) as exc:
        raise ValueError("invalid WhatsApp archive") from exc


def record_export(workspace: Workspace, *, account_key: str, chat_key: str, archive: Path,
                  observed_title: str, exported_at: str, media_requested: bool,
                  origin: str = "mac-native", expected_binding: dict | None = None,
                  expected_policy: dict | None = None) -> dict:
    observed = _time(exported_at)
    if origin not in {"mac-native", "phone-export", "historical-local"}:
        raise ValueError("unsupported WhatsApp acquisition origin")
    if observed > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise ValueError("WhatsApp acquisition time is in the future")
    with instagram_sync_lock(workspace) as acquired:
        if not acquired:
            raise ValueError("Localgraph writer is busy")
        record = _chat(workspace, account_key, chat_key)
        if expected_binding is not None and record != expected_binding:
            raise ValueError("identity-unverified")
        if expected_policy is not None and _load(workspace.config_path).get("imports", {}).get("whatsapp", {}).get("acquisition") != expected_policy:
            raise ValueError("identity-unverified")
        if observed_title != record["title"]:
            raise ValueError("WhatsApp chat title does not match the explicit binding")
        archive = Path(archive)
        if archive.stat().st_size > MAX_ARCHIVE_BYTES:
            raise ValueError("WhatsApp archive exceeds size limit")
        data = archive.read_bytes()
        messages, media = _read_bundle(data, record)
        if _time(max(m["sentAt"] for m in messages)) > observed + timedelta(minutes=5):
            raise ValueError("WhatsApp messages postdate the declared export time")
        digest = hashlib.sha256(data).hexdigest()
        root = _root(workspace, record)
        target = root / "archives" / f"{digest}.zip"
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise ValueError("existing WhatsApp custody checksum mismatch")
        else:
            with target.open("xb") as handle:
                os.chmod(target, 0o600)
                handle.write(data)
        receipt = {"schemaVersion": 1, "accountKey": account_key, "chatKey": chat_key,
                   "sourceKey": record["sourceKey"], "sha256": digest, "bytes": len(data),
                   "archivePath": str(target), "exportedAt": _iso(observed),
                   "recordedAt": _iso(datetime.now(timezone.utc)), "origin": origin,
                   "bindingEvidence": "operator-observed-chat-title", "mediaRequested": media_requested,
                   "messages": len(messages), "mediaFiles": len(media),
                   "firstMessageAt": min(m["sentAt"] for m in messages),
                   "lastMessageAt": max(m["sentAt"] for m in messages)}
        _write(root / "receipts" / f"{uuid.uuid4().hex}.json", receipt)
        return receipt


def record_acquisition_failure(workspace: Workspace, *, account_key: str, chat_key: str, reason: str) -> dict:
    if reason not in {"session-unavailable", "app-disconnected", "export-control-changed", "export-failed", "identity-unverified"}:
        raise ValueError("unsupported body-free WhatsApp failure code")
    record = _chat(workspace, account_key, chat_key)
    result = {"checkedAt": _iso(datetime.now(timezone.utc)), "error": reason}
    _write(_root(workspace, record) / "acquisition-failure.json", result)
    return result


def _identity(db: sqlite3.Connection, key: str, display: str, kind: str) -> int:
    db.execute("INSERT INTO identities(stable_key,display_name,kind) VALUES(?,?,?) ON CONFLICT(stable_key) DO NOTHING", (key, display, kind))
    return int(db.execute("SELECT id FROM identities WHERE stable_key=?", (key,)).fetchone()[0])


def _import_chat(db: sqlite3.Connection, workspace: Workspace, record: dict, now: datetime) -> dict:
    root = _root(workspace, record)
    receipts = [_load(path) for path in sorted((root / "receipts").glob("*.json"))]
    if not receipts:
        if _load(_status_path(workspace, record)).get("exports"):
            raise ValueError("previous WhatsApp delivery custody is missing")
        return {"accountKey": record["accountKey"], "chatKey": record["chatKey"], "status": "export-required",
                "checkedAt": _iso(now), "messages": 0, "exports": 0, "lastNativeExportAt": None,
                "historyCoverage": "baseline-required"}
    packets: dict[str, tuple[list[dict], dict[str, bytes]]] = {}
    for receipt in receipts:
        digest = receipt["sha256"]
        if receipt["sourceKey"] != record["sourceKey"] or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("WhatsApp receipt binding mismatch")
        if digest not in packets:
            data = (root / "archives" / f"{digest}.zip").read_bytes()
            if hashlib.sha256(data).hexdigest() != digest:
                raise ValueError("WhatsApp custody checksum mismatch")
            packets[digest] = _read_bundle(data, record)
    source_key = record["sourceKey"]
    previous_packets = {r[0].rsplit(":", 1)[-1] for r in db.execute(
        "SELECT source_identifier FROM source_imports WHERE source_kind='whatsapp' AND source_identifier GLOB ?",
        (f"whatsapp:{source_key}:*",))}
    if not previous_packets.issubset(packets):
        raise ValueError("previous WhatsApp packet receipts are missing")
    db.execute("INSERT INTO threads(source_kind,source_thread_key,title,thread_kind,raw_metadata_json) VALUES('whatsapp',?,?,?,?) ON CONFLICT(source_kind,source_thread_key) DO NOTHING",
               (source_key, record["viewLabel"], record["kind"], json.dumps({"identityAssurance": "operator-bound-export", "historyCoverage": "available-export-history-unverified"})))
    thread_id = int(db.execute("SELECT id FROM threads WHERE source_kind='whatsapp' AND source_thread_key=?", (source_key,)).fetchone()[0])
    group_key = "group:whatsapp:" + source_key
    if record["kind"] == "group":
        _identity(db, group_key, record["viewLabel"], "group")
        db.execute("INSERT OR IGNORE INTO graph_edges(from_kind,from_key,edge_kind,to_kind,to_key,source) VALUES('thread',?,'represents_group','identity',?,'whatsapp')", ("thread:whatsapp:" + source_key, group_key))
    available_media: dict[str, tuple[str, str]] = {}
    for digest, (messages, media) in packets.items():
        packet_media = {}
        for name, data in media.items():
            checksum = hashlib.sha256(data).hexdigest()
            target = workspace.objects_dir / "whatsapp" / checksum
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not target.exists():
                with target.open("xb") as handle:
                    os.chmod(target, 0o600)
                    handle.write(data)
            elif hashlib.sha256(target.read_bytes()).hexdigest() != checksum:
                raise ValueError("WhatsApp media custody checksum mismatch")
            packet_media[name] = (str(target), checksum)
            available_media[checksum] = (str(target), name)
        db.execute("INSERT INTO source_imports(source_kind,source_identifier,source_path,raw_metadata_json) VALUES('whatsapp',?,?,?) ON CONFLICT(source_identifier) DO NOTHING",
                   (f"whatsapp:{source_key}:{digest}", str(root / "archives" / f"{digest}.zip"), json.dumps({"sha256": digest, "accountKey": record["accountKey"], "chatKey": record["chatKey"]})))
        occurrences: Counter[str] = Counter()
        for message in messages:
            fingerprint = hashlib.sha256(json.dumps([message["sentAt"], message["sender"], message["body"]], ensure_ascii=False).encode()).hexdigest()
            occurrences[fingerprint] += 1
            message_key = f"{fingerprint}:{occurrences[fingerprint]}"
            identity_id = account_id = None
            if message["sender"] is not None:
                # A display name is not a global identity. Keep it scoped to this chat.
                sender_key = source_key + ":" + hashlib.sha256(message["sender"].encode()).hexdigest()[:24]
                identity_id = _identity(db, "person:whatsapp:" + sender_key, message["sender"], "person")
                db.execute("INSERT INTO accounts(identity_id,source_kind,account_key,display_name) VALUES(?,'whatsapp',?,?) ON CONFLICT(source_kind,account_key) DO NOTHING", (identity_id, sender_key, message["sender"]))
                account_id = int(db.execute("SELECT id FROM accounts WHERE source_kind='whatsapp' AND account_key=?", (sender_key,)).fetchone()[0])
                db.execute("INSERT OR IGNORE INTO thread_participants(thread_id,identity_id,account_id) VALUES(?,?,?)", (thread_id, identity_id, account_id))
            raw = {"archiveSha256": digest, "sourceLine": message["sourceLine"], "sourceTimestamp": message["sourceTimestamp"], "identityMethod": "content-occurrence-not-provider-id"}
            db.execute("INSERT INTO messages(thread_id,source_message_key,sender_identity_id,sender_account_id,sent_at,body_text,raw_json) VALUES(?,?,?,?,?,?,?) ON CONFLICT(thread_id,source_message_key) DO NOTHING", (thread_id, message_key, identity_id, account_id, message["sentAt"], message["body"], json.dumps(raw)))
            message_id = int(db.execute("SELECT id FROM messages WHERE thread_id=? AND source_message_key=?", (thread_id, message_key)).fetchone()[0])
            for name in ATTACHED.findall(message["body"]):
                match = packet_media.get(name)
                object_key = f"whatsapp:{source_key}:{message_key}:{hashlib.sha256(name.encode()).hexdigest()}"
                db.execute("INSERT INTO media_objects(message_id,object_key,source_uri,local_path,mime_type,checksum) VALUES(?,?,?,?,?,?) ON CONFLICT(object_key) DO UPDATE SET local_path=COALESCE(excluded.local_path,media_objects.local_path),checksum=COALESCE(excluded.checksum,media_objects.checksum)",
                           (message_id, object_key, name, match[0] if match else None, mimetypes.guess_type(name)[0], match[1] if match else None))
    count, first, last = db.execute("SELECT COUNT(*),MIN(sent_at),MAX(sent_at) FROM messages WHERE thread_id=?", (thread_id,)).fetchone()
    db.execute("UPDATE threads SET first_message_at=?,last_message_at=? WHERE id=?", (first, last, thread_id))
    missing = db.execute("SELECT COUNT(*) FROM media_objects mo JOIN messages m ON m.id=mo.message_id WHERE m.thread_id=? AND mo.local_path IS NULL", (thread_id,)).fetchone()[0]
    omitted = db.execute("SELECT COUNT(*) FROM messages WHERE thread_id=? AND (body_text LIKE '%omitted%' OR body_text LIKE '%not included%')", (thread_id,)).fetchone()[0]
    row = db.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
    view = workspace.views_dir / "threads" / "whatsapp" / stable_view_name(record["viewLabel"], source_key)
    result = {"accountKey": record["accountKey"], "chatKey": record["chatKey"], "status": "local-current",
              "checkedAt": _iso(now), "lastSuccessfulSyncAt": _iso(now),
              "lastNativeExportAt": max((r["exportedAt"] for r in receipts if r["origin"] == "mac-native"), default=None),
              "lastExportAt": max(r["exportedAt"] for r in receipts), "exports": len(packets), "messages": count,
              "firstMessageAt": first, "lastMessageAt": last, "mediaFiles": len(available_media),
              "missingMedia": missing, "omittedMediaMarkers": omitted,
              "historyCoverage": "available-export-history-unverified", "viewPath": str(view), "lastError": None}
    # Prepare before publishing. Preserve user-added files in an existing transcript directory.
    with tempfile.TemporaryDirectory(prefix="whatsapp-render-", dir=workspace.state_dir) as temporary:
        candidate, backup = Path(temporary) / "candidate", Path(temporary) / "previous"
        if view.is_symlink():
            raise ValueError("WhatsApp generated view must not be a symlink")
        if view.exists():
            shutil.copytree(view, candidate, symlinks=True)
        for name in ("index.md", "messages.md"):
            target = candidate / name
            if target.is_symlink():
                target.unlink()
        _write_thread_view(db, candidate, row)
        result["renderSha256"] = hashlib.sha256((candidate / "messages.md").read_bytes()).hexdigest()
        _write(candidate / "coverage.json", result)
        _write(candidate / "media-manifest.json", {"objects": [{"sha256": h, "path": p, "name": n} for h, (p, n) in sorted(available_media.items())]})
        for file in candidate.iterdir():
            if file.is_file() and not file.is_symlink():
                file.chmod(0o600)
        candidate.chmod(0o700)
        view.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if view.exists():
            os.replace(view, backup)
        try:
            os.replace(candidate, view)
            db.commit()
        except BaseException:
            db.rollback()
            if view.exists():
                os.replace(view, Path(temporary) / "failed")
            if backup.exists():
                os.replace(backup, view)
            raise
    return result


def run_whatsapp_sync(workspace: Workspace, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    with instagram_sync_lock(workspace) as acquired:
        if not acquired:
            return {"status": "skipped-concurrent", "chats": []}
        workspace.ensure_workspace(force=False)
        results = []
        with connect(workspace.database_path) as db:
            initialize_schema(db)
            for record in chats(workspace):
                if not record["enabled"]:
                    continue
                try:
                    result = _import_chat(db, workspace, record, now)
                except (OSError, ValueError, KeyError, sqlite3.Error, zipfile.BadZipFile):
                    db.rollback()
                    result = {**_load(_status_path(workspace, record)), "accountKey": record["accountKey"],
                              "chatKey": record["chatKey"], "status": "degraded", "checkedAt": _iso(now),
                              "lastError": "archive-validation-or-render-failed"}
                _write(_status_path(workspace, record), result)
                results.append(result)
        return {"status": "degraded" if any(r["status"] == "degraded" for r in results) else ("local-current" if results and all(r["status"] == "local-current" for r in results) else "export-required"), "chats": results}


def source_status(workspace: Workspace, scheduler: dict, now: datetime, acquisition_scheduler: dict | None = None,
                  discovery_scheduler: dict | None = None) -> dict:
    from .status import _health, _scheduler_findings, _source_report
    by_account: dict[str, list[dict]] = {}
    policy = _load(workspace.config_path).get("imports", {}).get("whatsapp", {}).get("acquisition", {})
    if policy.get("accountKey"):
        by_account[policy["accountKey"]] = []
    for record in chats(workspace):
        metadata_invalid = False
        try:
            sync = _load(_status_path(workspace, record))
        except (OSError, ValueError):
            sync, metadata_invalid = {}, True
        for field in ("lastNativeExportAt", "lastSuccessfulSyncAt", "lastExportAt"):
            if sync.get(field) is not None:
                try:
                    if _time(sync[field]) > now + timedelta(minutes=5):
                        raise ValueError("future state timestamp")
                except (ValueError, AttributeError, TypeError):
                    sync.pop(field, None)
                    metadata_invalid = True
        try:
            receipts = [_load(p) for p in (_root(workspace, record) / "receipts").glob("*.json")]
            if any(not re.fullmatch(r"[a-f0-9]{64}", str(r.get("sha256", ""))) or r.get("sourceKey") != record["sourceKey"] for r in receipts):
                raise ValueError("invalid receipt")
        except (OSError, ValueError):
            receipts, metadata_invalid = [], True
        delivered = {r["sha256"] for r in receipts if (_root(workspace, record) / "archives" / f"{r['sha256']}.zip").is_file()}
        canonical_count = 0
        if workspace.database_path.exists():
            # A live WAL is part of canonical state. Never use immutable=1 here.
            uri = "file:" + quote(str(workspace.database_path), safe="/") + "?mode=ro"
            try:
                with closing(sqlite3.connect(uri, uri=True)) as db:
                    canonical_count = db.execute("SELECT COUNT(*) FROM messages m JOIN threads t ON t.id=m.thread_id WHERE t.source_kind='whatsapp' AND t.source_thread_key=?", (record["sourceKey"],)).fetchone()[0]
            except sqlite3.Error:
                canonical_count = -1
        findings = []
        def finding(code: str, severity: str = "warning") -> None:
            findings.append({"code": code, "severity": severity})
        if metadata_invalid:
            finding("whatsapp-metadata-invalid", "error")
        if record["enabled"]:
            if not delivered:
                finding("missing-export")
            if canonical_count != sync.get("messages", 0):
                finding("canonical-count-mismatch", "error")
            if len(delivered) > sync.get("exports", 0):
                finding("unimported-export")
            if len(delivered) < sync.get("exports", 0):
                finding("archive-custody-incomplete", "error")
            if sync.get("status") == "degraded":
                finding("whatsapp-import-failed", "error")
            exported = sync.get("lastNativeExportAt")
            if not exported or now - _time(exported) > timedelta(hours=record["acquisitionIntervalHours"] * 2):
                finding("stale-acquisition")
            imported = sync.get("lastSuccessfulSyncAt")
            if not imported or now - _time(imported) > timedelta(hours=2):
                finding("stale-sync")
            if sync.get("exports") and not sync.get("messages"):
                finding("unexpected-empty-snapshot", "error")
            if sync.get("missingMedia") or sync.get("omittedMediaMarkers"):
                finding("media-coverage-incomplete")
            finding("historical-completeness-not-established")
            try:
                failure = _load(_root(workspace, record) / "acquisition-failure.json")
                if failure:
                    _time(failure["checkedAt"])
            except (OSError, ValueError, KeyError, AttributeError, TypeError):
                failure = {}
                finding("whatsapp-metadata-invalid", "error")
            if failure and (not exported or _time(failure["checkedAt"]) > _time(exported)):
                finding("native-acquisition-failed", "error")
        rendered = bool(sync.get("viewPath") and (Path(sync["viewPath"]) / "messages.md").is_file())
        if sync.get("messages") and not rendered:
            finding("missing-rendered-view", "error")
        if rendered and sync.get("renderSha256") != hashlib.sha256((Path(sync["viewPath"]) / "messages.md").read_bytes()).hexdigest():
            finding("render-checksum-mismatch", "error")
        current = bool(record["enabled"] and delivered and rendered and canonical_count > 0 and not any(f["severity"] == "error" or f["code"] in {"stale-acquisition", "stale-sync", "unimported-export"} for f in findings))
        lifecycle = {"current": current, "complete": False, "currentStage": "current" if current else ("rendered" if rendered and canonical_count > 0 else ("imported" if canonical_count > 0 else ("delivered" if delivered else "configured"))),
                     "stages": {"configured": "evidenced", "requested": "not-recorded", "preparing": "not-recorded", "delivered": "evidenced" if delivered else "pending", "imported": "evidenced" if canonical_count > 0 else "pending", "rendered": "evidenced" if rendered else "pending", "current": "evidenced" if current else "pending", "complete": "history-unverified"}}
        by_account.setdefault(record["accountKey"], []).append({**sync, "chatKey": record["chatKey"], "enabled": record["enabled"], "deliveredExports": len(delivered), "canonicalMessages": canonical_count, "findings": findings, "lifecycle": lifecycle})
    accounts = []
    for key, records in by_account.items():
        findings = [f for r in records for f in r["findings"]]
        from .whatsapp_acquisition import population_status
        try:
            population = population_status(workspace, key, now=now,
                current_keys={r["chatKey"] for r in records if r["lifecycle"]["current"]})
        except (ValueError, OSError, KeyError, TypeError, AttributeError):
            population = {"populationCovered": False, "historicalComplete": False, "status": "metadata-invalid"}
        if not population.get("populationCovered"):
            findings.append({"code": "whatsapp-population-incomplete", "severity": "warning"})
        if population.get("lastError"):
            findings.append({"code": "native-population-acquisition-failed", "severity": "error"})
        if population.get("discoveryError"):
            findings.append({"code": "native-population-discovery-failed", "severity": "error"})
        accounts.append({"accountKey": key, "health": _health(findings), "findings": findings,
                         "population": population, "chats": records})
    for role, native_scheduler in (("acquisition", acquisition_scheduler), ("discovery", discovery_scheduler)):
        if native_scheduler is None:
            continue
        scheduler = {**scheduler, role: native_scheduler}
        for account in accounts:
            extra = _scheduler_findings(native_scheduler)
            account["findings"].extend({**f, "code": role + "-" + f["code"]} for f in extra)
            account["health"] = _health(account["findings"])
    return _source_report("whatsapp", scheduler, accounts, _scheduler_findings(scheduler) if accounts else [])


def install_whatsapp_sync(workspace: Workspace, *, interval_minutes: int = 60, home: Path | None = None, dry_run: bool = False) -> dict:
    if not 5 <= interval_minutes <= 1440 or workspace.root.resolve().parts[1:2] == ("Volumes",):
        raise ValueError("hourly WhatsApp watcher requires an internal workspace and interval 5..1440")
    support = (home or Path.home()) / "Library/Application Support/Localgraph"
    runtime = support / "runtime"
    script = support / "bin/localgraph-whatsapp-sync.sh"
    logs = support / "logs"
    plist = (home or Path.home()) / "Library/LaunchAgents" / f"{LABEL}.plist"
    import shlex
    command = " ".join(shlex.quote(p) for p in [sys.executable, "-m", "localgraph", "--root", str(workspace.root), "whatsapp-sync"])
    script_text = f"#!/bin/zsh\nset -euo pipefail\nexport PYTHONPATH={shlex.quote(str(runtime))}\nexport PYTHONDONTWRITEBYTECODE=1\numask 077\n{command} >> {shlex.quote(str(logs / 'whatsapp-sync.log'))} 2>&1\n"
    payload = {"Label": LABEL, "ProgramArguments": ["/bin/zsh", str(script)], "RunAtLoad": True, "StartInterval": interval_minutes * 60, "ThrottleInterval": 60, "WorkingDirectory": str(support), "ProcessType": "Background", "StandardOutPath": str(logs / "whatsapp-sync.stdout.log"), "StandardErrorPath": str(logs / "whatsapp-sync.stderr.log")}
    if not dry_run:
        with instagram_sync_lock(workspace) as acquired:
            if not acquired:
                raise ValueError("Localgraph writer is busy")
            for folder in (support, runtime, script.parent, logs, plist.parent):
                folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copytree(Path(__file__).parent, runtime / "localgraph", dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            script.write_text(script_text, encoding="utf-8")
            script.chmod(0o700)
            plist.write_bytes(plistlib.dumps(payload))
    return {"label": LABEL, "script": str(script), "runtime": str(runtime), "plist": str(plist), "intervalMinutes": interval_minutes, "dryRun": dry_run}
