from __future__ import annotations

import json
import os
import plistlib
import re
import shlex
import shutil
import sqlite3
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .ingest import SourceImportResult
from .paths import Workspace
from .render import render_views
from .schema import connect, initialize_schema
from .twitter_accounts import TwitterAccount, twitter_accounts


def run_twitter_sync(workspace: Workspace, *, render: bool = True) -> dict[str, object]:
    """Import cumulative X/Twitter account archives from account-scoped incoming directories."""
    workspace.ensure_workspace(force=False)
    checked_at = _now_iso()
    account_payload: dict[str, object] = {}
    source_results: list[SourceImportResult] = []
    with connect(workspace.database_path) as db:
        initialize_schema(db)
        for account in twitter_accounts(workspace, enabled_only=False):
            archives = _account_archives(account)
            if not account.enabled:
                status = _account_status(account, checked_at=checked_at, status="not-eligible")
            elif not archives:
                result = SourceImportResult("twitter", str(account.source_path), status="pending")
                result.warnings.append("no X/Twitter account archive is available for this account")
                source_results.append(result)
                status = _account_status(account, checked_at=checked_at, status="export-required")
            else:
                _clear_account_projection(db, account.account_key)
                imported: list[SourceImportResult] = []
                try:
                    for archive in archives:
                        result = import_twitter_archive(db, archive, account=account, commit=False)
                        if result.status != "imported":
                            raise ValueError(f"refusing to replace Twitter account projection with an empty archive: {account.account_key}")
                        imported.append(result)
                    db.commit()
                except (OSError, ValueError, zipfile.BadZipFile):
                    db.rollback()
                    previous = _load_json(account.sync_status_path)
                    status = {
                        **_account_status(account, checked_at=checked_at, status="degraded"),
                        **previous,
                        "status": "degraded",
                        "checkedAt": checked_at,
                        "lastError": "archive-validation-failed",
                    }
                    failed = SourceImportResult("twitter", str(account.source_path), status="failed")
                    failed.warnings.append("archive validation failed; last-known-good account projection was preserved")
                    source_results.append(failed)
                else:
                    source_results.extend(imported)
                    canonical_messages, canonical_threads = _account_canonical_counts(db, account.account_key)
                    status = _account_status(
                        account,
                        checked_at=checked_at,
                        status="local-current",
                        archives=archives,
                        message_files=sum(_archive_message_file_count(archive) for archive in archives),
                        messages=canonical_messages,
                        threads=canonical_threads,
                    )
            _write_json_private(account.sync_status_path, status)
            account_payload[account.account_key] = {"account": account.to_public_json(), "sync": status}
        rendered: dict[str, int] | None = None
        if render:
            rendered = render_views(db, workspace)
    statuses = [str(value["sync"]["status"]) for value in account_payload.values()]  # type: ignore[index]
    aggregate_status = "not-configured" if not statuses else (
        "degraded" if "degraded" in statuses else ("export-required" if "export-required" in statuses else "current")
    )
    aggregate = {
        "schemaVersion": 1,
        "status": aggregate_status,
        "checkedAt": checked_at,
        "accountsConfigured": len(account_payload),
        "accountsReady": sum(status == "local-current" for status in statuses),
        "accounts": {
            key: {
                "status": value["sync"]["status"],  # type: ignore[index]
                "historyCoverage": value["sync"]["historyCoverage"],  # type: ignore[index]
                "messages": value["sync"]["messages"],  # type: ignore[index]
            }
            for key, value in account_payload.items()
        },
    }
    _write_json_private(workspace.state_dir / "twitter-accounts-sync-status.json", aggregate)
    payload: dict[str, object] = {
        "startedAt": checked_at,
        "workspace": str(workspace.root),
        "twitterAccounts": account_payload,
        "twitterSync": aggregate,
        "result": _combined_result(source_results),
    }
    if rendered is not None:
        payload["render"] = rendered
    return payload


def import_twitter_archive(
    db: sqlite3.Connection,
    archive_path: Path,
    *,
    account: TwitterAccount,
    commit: bool = True,
) -> SourceImportResult:
    archive = archive_path.expanduser().resolve()
    result = SourceImportResult("twitter", str(archive))
    if not archive.is_file():
        result.status = "missing"
        return result
    with zipfile.ZipFile(archive) as bundle:
        names = set(bundle.namelist())
        message_names = _message_member_names(names)
        if not message_names:
            result.status = "empty"
            return result
        account_payload = _read_archive_js(bundle, _member_name(names, "data/account.js"))
        payloads = [(_read_archive_js(bundle, name), "direct-messages-group" in Path(name).name) for name in message_names]
    own_id, own_username, own_display = _account_identity(account_payload, account)
    source_identifier = f"twitter:{account.account_key}:{archive.name}"
    db.execute(
        "INSERT INTO source_imports (source_kind, source_identifier, source_path, raw_metadata_json) "
        "VALUES (?, ?, ?, ?) ON CONFLICT(source_identifier) DO UPDATE SET source_path = excluded.source_path, "
        "imported_at = CURRENT_TIMESTAMP, raw_metadata_json = excluded.raw_metadata_json",
        ("twitter", source_identifier, str(archive), json.dumps({"archive_name": archive.name})),
    )
    seen_threads: set[str] = set()
    seen_messages: set[str] = set()
    participant_ids: set[str] = set()
    conversations = [
        (item, is_group)
        for payload, is_group in payloads
        if isinstance(payload, list)
        for item in payload
    ]
    for item, is_group in conversations:
        conversation = item.get("dmConversation") if isinstance(item, dict) else None
        if not isinstance(conversation, dict):
            continue
        conversation_id = str(conversation.get("conversationId") or "").strip()
        if not conversation_id:
            continue
        messages = conversation.get("messages")
        if not isinstance(messages, list):
            continue
        ids = {own_id} if is_group else {part for part in conversation_id.split("-") if part.isdigit()}
        for raw in messages:
            create = raw.get("messageCreate") if isinstance(raw, dict) else None
            if isinstance(create, dict):
                ids.update(str(create.get(key) or "") for key in ("senderId", "recipientId"))
        ids.discard("")
        participant_ids.update(ids)
        participant_names = [own_display if participant_id == own_id else f"Twitter user {participant_id}" for participant_id in sorted(ids)]
        title = next((name for name in participant_names if name != own_display), own_display)
        thread_kind = "group" if is_group or len(ids) > 2 else "direct"
        source_thread_key = f"{account.account_key}:{conversation_id}"
        db.execute(
            "INSERT INTO threads (source_kind, source_thread_key, title, thread_kind, raw_metadata_json) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(source_kind, source_thread_key) DO UPDATE SET "
            "title = excluded.title, thread_kind = excluded.thread_kind, raw_metadata_json = excluded.raw_metadata_json",
            ("twitter", source_thread_key, title, thread_kind, json.dumps({"archive": archive.name, "conversation_id": conversation_id})),
        )
        thread_id = int(db.execute("SELECT id FROM threads WHERE source_kind = 'twitter' AND source_thread_key = ?", (source_thread_key,)).fetchone()[0])
        seen_threads.add(source_thread_key)
        participant_accounts: dict[str, tuple[int, int]] = {}
        for participant_id in sorted(ids):
            display = own_display if participant_id == own_id else f"Twitter user {participant_id}"
            identity_key = account.owner_identity_key if participant_id == own_id else f"person:twitter:{participant_id}"
            kind = account.owner_kind if participant_id == own_id else "person"
            identity_id = _upsert_identity(db, identity_key, display, kind)
            account_id = _upsert_account(db, participant_id, display, identity_id, own_username if participant_id == own_id else None)
            participant_accounts[participant_id] = (identity_id, account_id)
            db.execute(
                "INSERT OR IGNORE INTO thread_participants (thread_id, identity_id, account_id, role) VALUES (?, ?, ?, 'participant')",
                (thread_id, identity_id, account_id),
            )
        for raw in messages:
            create = raw.get("messageCreate") if isinstance(raw, dict) else None
            if not isinstance(create, dict):
                continue
            message_id = str(create.get("id") or "").strip()
            sender_id = str(create.get("senderId") or "").strip()
            sent_at = str(create.get("createdAt") or "").strip()
            if not message_id or not sender_id or not sent_at:
                continue
            sender = participant_accounts.get(sender_id)
            if sender is None:
                continue
            body = create.get("text")
            db.execute(
                "INSERT INTO messages (thread_id, source_message_key, sender_identity_id, sender_account_id, sent_at, body_text, raw_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(thread_id, source_message_key) DO UPDATE SET "
                "sender_identity_id = excluded.sender_identity_id, sender_account_id = excluded.sender_account_id, "
                "sent_at = excluded.sent_at, body_text = excluded.body_text, raw_json = excluded.raw_json",
                (thread_id, message_id, sender[0], sender[1], sent_at, str(body) if body is not None else None, json.dumps(create)),
            )
            seen_messages.add(message_id)
        bounds = db.execute("SELECT MIN(sent_at), MAX(sent_at) FROM messages WHERE thread_id = ?", (thread_id,)).fetchone()
        db.execute("UPDATE threads SET first_message_at = ?, last_message_at = ? WHERE id = ?", (bounds[0], bounds[1], thread_id))
    if not seen_messages:
        result.status = "empty"
        return result
    if commit:
        db.commit()
    result.imports = 1
    result.threads = len(seen_threads)
    result.accounts = len(participant_ids)
    result.messages = len(seen_messages)
    return result


def _account_archives(account: TwitterAccount) -> list[Path]:
    if not account.source_path.is_dir():
        return []
    return sorted(path for path in account.source_path.iterdir() if path.is_file() and path.suffix.lower() == ".zip")


def _read_archive_js(bundle: zipfile.ZipFile, name: str | None) -> object:
    if name is None:
        return []
    text = bundle.read(name).decode("utf-8-sig")
    payload = text.split("=", 1)[1] if "=" in text else text
    return json.loads(payload.strip().removesuffix(";"))


def _member_name(names: set[str], suffix: str) -> str | None:
    matches = sorted(name for name in names if name == suffix or name.endswith(f"/{suffix}"))
    return matches[0] if len(matches) == 1 else None


def _message_member_names(names: set[str]) -> list[str]:
    return sorted(
        name
        for name in names
        if re.fullmatch(r"direct-messages(?:-group)?(?:-part\d+)?\.js", Path(name).name)
        and (name.startswith("data/") or "/data/" in name)
    )


def _archive_message_file_count(archive: Path) -> int:
    with zipfile.ZipFile(archive) as bundle:
        return len(_message_member_names(set(bundle.namelist())))


def _account_identity(payload: object, account: TwitterAccount) -> tuple[str, str, str]:
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        raw = payload[0].get("account")
        if isinstance(raw, dict):
            account_id = str(raw.get("accountId") or "").strip()
            username = str(raw.get("username") or "").strip().lstrip("@")
            display = str(raw.get("accountDisplayName") or account.display_name).strip()
            if username.casefold() != account.account_key.casefold():
                raise ValueError(f"X/Twitter archive account mismatch: expected {account.account_key}, found {username}")
            if account_id:
                return account_id, username, display
    raise ValueError(f"X/Twitter archive does not identify the exporting account: {account.account_key}")


def _upsert_identity(db: sqlite3.Connection, key: str, display: str, kind: str) -> int:
    db.execute(
        "INSERT INTO identities (stable_key, display_name, kind) VALUES (?, ?, ?) "
        "ON CONFLICT(stable_key) DO UPDATE SET display_name = excluded.display_name, kind = excluded.kind, updated_at = CURRENT_TIMESTAMP",
        (key, display, kind),
    )
    return int(db.execute("SELECT id FROM identities WHERE stable_key = ?", (key,)).fetchone()[0])


def _upsert_account(db: sqlite3.Connection, key: str, display: str, identity_id: int, username: str | None) -> int:
    profile_url = f"https://x.com/{username}" if username else None
    db.execute(
        "INSERT INTO accounts (identity_id, source_kind, account_key, display_name, profile_url) VALUES (?, 'twitter', ?, ?, ?) "
        "ON CONFLICT(source_kind, account_key) DO UPDATE SET identity_id = excluded.identity_id, "
        "display_name = excluded.display_name, profile_url = COALESCE(excluded.profile_url, accounts.profile_url)",
        (identity_id, key, display, profile_url),
    )
    return int(db.execute("SELECT id FROM accounts WHERE source_kind = 'twitter' AND account_key = ?", (key,)).fetchone()[0])


def _clear_account_projection(db: sqlite3.Connection, account_key: str) -> None:
    db.execute("DELETE FROM source_imports WHERE source_kind = 'twitter' AND source_identifier GLOB ?", (f"twitter:{account_key}:*",))
    db.execute("DELETE FROM threads WHERE source_kind = 'twitter' AND source_thread_key GLOB ?", (f"{account_key}:*",))


def _account_canonical_counts(db: sqlite3.Connection, account_key: str) -> tuple[int, int]:
    prefix = f"{account_key}:*"
    messages = db.execute(
        "SELECT COUNT(*) FROM messages AS m JOIN threads AS t ON t.id = m.thread_id "
        "WHERE t.source_kind = 'twitter' AND t.source_thread_key GLOB ?",
        (prefix,),
    ).fetchone()[0]
    threads = db.execute(
        "SELECT COUNT(*) FROM threads WHERE source_kind = 'twitter' AND source_thread_key GLOB ?",
        (prefix,),
    ).fetchone()[0]
    return int(messages), int(threads)


def _account_status(
    account: TwitterAccount,
    *,
    checked_at: str,
    status: str,
    archives: list[Path] | None = None,
    message_files: int = 0,
    messages: int = 0,
    threads: int = 0,
) -> dict[str, object]:
    completed = len(archives or [])
    return {
        "schemaVersion": 1,
        "accountKey": account.account_key,
        "status": status,
        "checkedAt": checked_at,
        "lastSuccessfulSyncAt": checked_at if status == "local-current" else None,
        "completedExports": completed,
        "messageFiles": message_files,
        "messages": messages,
        "threads": threads,
        "historyCoverage": "available-direct-messages-through-archive" if completed else "archive-required",
        "providerCadence": "manual",
        "lastError": None,
    }


def _combined_result(results: list[SourceImportResult]) -> dict[str, object]:
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


def _write_json_private(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(f"{json.dumps(payload, indent=2, sort_keys=True)}\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)


def _load_json(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def install_twitter_sync(
    workspace: Workspace,
    *,
    interval_minutes: int = 60,
    label: str = "com.openhouse.localgraph.twitter-sync",
    dry_run: bool = False,
    home: Path | None = None,
) -> dict[str, object]:
    if not (5 <= interval_minutes <= 1440):
        raise ValueError("--interval-minutes must be between 5 and 1440")
    workspace_root = workspace.root.expanduser().resolve()
    if len(workspace_root.parts) > 1 and workspace_root.parts[1] == "Volumes":
        raise ValueError("macOS launchd requires a workspace under ~/Library/Application Support/Localgraph")
    workspace.ensure_workspace(force=False)
    home_dir = (home or Path.home()).expanduser()
    support_dir = home_dir / "Library" / "Application Support" / "Localgraph"
    runtime_dir = support_dir / "runtime"
    script_path = support_dir / "bin" / "localgraph-twitter-sync.sh"
    log_dir = support_dir / "logs"
    plist_path = home_dir / "Library" / "LaunchAgents" / f"{label}.plist"
    command = " ".join(shlex.quote(part) for part in [sys.executable, "-m", "localgraph", "--root", str(workspace.root), "twitter-sync"])
    script = "\n".join([
        "#!/bin/zsh",
        "set -euo pipefail",
        f"export PYTHONPATH={shlex.quote(str(runtime_dir))}:${{PYTHONPATH:-}}",
        f"mkdir -p {shlex.quote(str(workspace.state_dir))} {shlex.quote(str(log_dir))}",
        f"{command} >> {shlex.quote(str(log_dir / 'twitter-sync.log'))} 2>&1",
        "",
    ])
    plist = {
        "Label": label,
        "ProgramArguments": ["/bin/zsh", str(script_path)],
        "StartInterval": interval_minutes * 60,
        "RunAtLoad": True,
        "ProcessType": "Background",
        "ThrottleInterval": 60,
        "StandardOutPath": str(log_dir / "twitter-sync.stdout.log"),
        "StandardErrorPath": str(log_dir / "twitter-sync.stderr.log"),
        "WorkingDirectory": str(support_dir),
    }
    if not dry_run:
        support_dir.mkdir(parents=True, exist_ok=True)
        support_dir.chmod(0o700)
        shutil.copytree(Path(__file__).resolve().parent, runtime_dir / "localgraph", dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text(script, encoding="utf-8")
        script_path.chmod(0o700)
        log_dir.mkdir(parents=True, exist_ok=True)
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        plist_path.write_bytes(plistlib.dumps(plist, sort_keys=True))
    return {
        "label": label,
        "script": str(script_path),
        "runtime": str(runtime_dir),
        "workspace": str(workspace.root),
        "plist": str(plist_path),
        "intervalMinutes": interval_minutes,
        "runAtLoad": True,
        "dryRun": dry_run,
    }
