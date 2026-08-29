from __future__ import annotations

import contextlib
import json
import os
import plistlib
import shlex
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from .ingest import clear_imessage_projection, import_imessage_chat_db
from .paths import Workspace
from .render import render_views
from .schema import connect, initialize_schema


DEFAULT_IMESSAGE_SYNC_LABEL = "com.openhouse.localgraph.imessage-sync"
DEFAULT_IMESSAGE_SYNC_INTERVAL_MINUTES = 60
REQUIRED_CHAT_TABLES = {"chat", "message", "chat_message_join"}


def run_imessage_sync(
    workspace: Workspace,
    *,
    live_db_path: Path | None = None,
    me_name: str = "Me",
    me_handles: list[str] | None = None,
    render: bool = True,
) -> dict[str, object]:
    workspace.ensure_workspace(force=False)
    checked_at = _now_iso()
    source = _configured_live_db(workspace, live_db_path)
    previous = _load_json(workspace.imessage_sync_status_path)
    snapshot_path = workspace.imessage_chat_db_path
    previous_snapshot = snapshot_path.with_name(f".{snapshot_path.name}.{os.getpid()}.last-known-good")
    previous_snapshot.unlink(missing_ok=True)
    if snapshot_path.exists():
        os.link(snapshot_path, previous_snapshot)
    try:
        snapshot = snapshot_imessage_database(source, snapshot_path)
        with connect(workspace.database_path) as db:
            initialize_schema(db)
            replacement = clear_imessage_projection(db)
            try:
                imported = import_imessage_chat_db(
                    db,
                    workspace.imessage_chat_db_path,
                    me_name=me_name,
                    me_handles=me_handles or [],
                    explicit=True,
                    commit=False,
                )
                if replacement["messages"] > 0 and imported.messages == 0:
                    raise ValueError("refusing to replace a non-empty iMessage projection with an empty snapshot")
                latest_message_at = db.execute(
                    "SELECT MAX(last_message_at) FROM threads WHERE source_kind = 'imessage'"
                ).fetchone()[0]
                rendered = render_views(db, workspace) if render else None
                db.commit()
            except Exception:
                db.rollback()
                raise
        status = {
            "schemaVersion": 1,
            "status": "current",
            "checkedAt": checked_at,
            "lastSuccessfulSyncAt": checked_at,
            "sourceModifiedAt": snapshot["sourceModifiedAt"],
            "snapshotCreatedAt": snapshot["createdAt"],
            "snapshotBytes": snapshot["bytes"],
            "snapshotPath": str(workspace.imessage_chat_db_path),
            "checkIntervalMinutes": DEFAULT_IMESSAGE_SYNC_INTERVAL_MINUTES,
            "historyCoverage": "complete-through-snapshot",
            "imports": imported.imports,
            "threads": imported.threads,
            "messages": imported.messages,
            "media": imported.media,
            "latestMessageAt": latest_message_at,
            "lastError": None,
        }
        _write_json_private(workspace.imessage_sync_status_path, status)
        previous_snapshot.unlink(missing_ok=True)
        result: dict[str, object] = {
            "startedAt": checked_at,
            "workspace": str(workspace.root),
            "snapshot": snapshot,
            "projectionReplacement": replacement,
            "imessageSync": status,
            "result": {
                "sources": [imported.to_json()],
                "totals": {
                    "imports": imported.imports,
                    "threads": imported.threads,
                    "groups": imported.groups,
                    "accounts": imported.accounts,
                    "messages": imported.messages,
                    "media": imported.media,
                },
            },
        }
        if rendered is not None:
            result["render"] = rendered
        return result
    except Exception as exc:
        if previous_snapshot.exists():
            os.replace(previous_snapshot, snapshot_path)
        has_last_known_good = workspace.imessage_chat_db_path.exists() and bool(previous.get("lastSuccessfulSyncAt"))
        failure = {
            **previous,
            "schemaVersion": 1,
            "status": "degraded" if has_last_known_good else "blocked",
            "checkedAt": checked_at,
            "lastSuccessfulSyncAt": previous.get("lastSuccessfulSyncAt"),
            "checkIntervalMinutes": DEFAULT_IMESSAGE_SYNC_INTERVAL_MINUTES,
            "historyCoverage": previous.get("historyCoverage") or "snapshot-required",
            "lastError": str(exc),
        }
        _write_json_private(workspace.imessage_sync_status_path, failure)
        if isinstance(exc, FileNotFoundError):
            raise
        raise ValueError(f"iMessage sync failed: {exc}") from exc


def snapshot_imessage_database(source: Path, destination: Path) -> dict[str, object]:
    source = source.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"live iMessage chat database does not exist: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    temporary.unlink(missing_ok=True)
    source_uri = f"file:{quote(source.as_posix(), safe='/')}?mode=ro"
    try:
        try:
            source_db_connection = sqlite3.connect(source_uri, uri=True, timeout=30)
        except sqlite3.OperationalError as exc:
            if "unable to open database file" not in str(exc).lower():
                raise
            raise PermissionError(
                f"macOS denied access to {source}; grant Full Disk Access to "
                f"{sys.executable} in System Settings > Privacy & Security > Full Disk Access"
            ) from exc
        with contextlib.closing(source_db_connection) as source_db:
            source_db.execute("PRAGMA query_only = ON")
            with contextlib.closing(sqlite3.connect(temporary)) as target_db:
                source_db.backup(target_db)
                target_db.commit()
        temporary.chmod(0o600)
        with contextlib.closing(
            sqlite3.connect(f"file:{quote(temporary.as_posix(), safe='/')}?mode=ro", uri=True)
        ) as copied:
            tables = {
                str(row[0])
                for row in copied.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            missing = REQUIRED_CHAT_TABLES - tables
            if missing:
                raise ValueError(f"iMessage snapshot is missing required tables: {', '.join(sorted(missing))}")
            integrity = copied.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise ValueError(f"iMessage snapshot integrity check failed: {integrity}")
        os.replace(temporary, destination)
        destination.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)
    created_at = _now_iso()
    source_mtime = max(
        path.stat().st_mtime
        for path in (source, source.with_name(f"{source.name}-wal"), source.with_name(f"{source.name}-shm"))
        if path.exists()
    )
    return {
        "method": "sqlite-online-backup",
        "path": str(destination),
        "createdAt": created_at,
        "sourceModifiedAt": _timestamp_iso(source_mtime),
        "bytes": destination.stat().st_size,
    }


def imessage_status(workspace: Workspace) -> dict[str, object]:
    sync = _load_json(workspace.imessage_sync_status_path)
    if not sync:
        sync = {
            "schemaVersion": 1,
            "status": "not-checked",
            "lastSuccessfulSyncAt": None,
            "historyCoverage": "snapshot-required",
            "lastError": None,
        }
    return {
        "workspace": str(workspace.root),
        "sync": sync,
        "freshness": {
            "cadence": "hourly",
            "nextCheckWithinMinutes": DEFAULT_IMESSAGE_SYNC_INTERVAL_MINUTES,
            "source": "macos-messages-read-only-snapshot",
        },
    }


def install_imessage_sync(
    workspace: Workspace,
    *,
    interval_minutes: int = DEFAULT_IMESSAGE_SYNC_INTERVAL_MINUTES,
    label: str = DEFAULT_IMESSAGE_SYNC_LABEL,
    live_db_path: Path | None = None,
    me_name: str = "Me",
    dry_run: bool = False,
    home: Path | None = None,
) -> dict[str, object]:
    if not (5 <= interval_minutes <= 1440):
        raise ValueError("--interval-minutes must be between 5 and 1440")
    workspace_root = workspace.root.expanduser().resolve()
    if len(workspace_root.parts) > 1 and workspace_root.parts[1] == "Volumes":
        raise ValueError(
            "macOS launchd cannot reliably read a removable-volume workspace; "
            "use a workspace under ~/Library/Application Support/Localgraph"
        )
    workspace.ensure_workspace(force=False)
    home_dir = (home or Path.home()).expanduser()
    support_dir = home_dir / "Library" / "Application Support" / "Localgraph"
    runtime_dir = support_dir / "runtime"
    runtime_package = runtime_dir / "localgraph"
    script_path = support_dir / "bin" / "localgraph-imessage-sync.sh"
    log_dir = support_dir / "logs"
    plist_path = home_dir / "Library" / "LaunchAgents" / f"{label}.plist"
    script = imessage_sync_script(
        workspace,
        runtime_dir=runtime_dir,
        log_path=log_dir / "imessage-sync.log",
        live_db_path=live_db_path,
        me_name=me_name,
    )
    plist = _interval_launchd_plist(
        label=label,
        script_path=script_path,
        interval_seconds=interval_minutes * 60,
        working_directory=support_dir,
        log_dir=log_dir,
    )
    if not dry_run:
        support_dir.mkdir(parents=True, exist_ok=True)
        support_dir.chmod(0o700)
        shutil.copytree(
            Path(__file__).resolve().parent,
            runtime_package,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
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
        "bootstrapCommand": f"launchctl bootstrap gui/$(id -u) {shlex.quote(str(plist_path))}",
        "kickstartCommand": f"launchctl kickstart -k gui/$(id -u)/{label}",
        "runCommand": str(script_path),
    }


def imessage_sync_script(
    workspace: Workspace,
    *,
    runtime_dir: Path,
    log_path: Path,
    live_db_path: Path | None,
    me_name: str,
) -> str:
    args = [
        sys.executable,
        "-m",
        "localgraph",
        "--root",
        str(workspace.root),
        "imessage-sync",
        "--me",
        me_name,
    ]
    if live_db_path is not None:
        args.extend(["--source-db", str(live_db_path.expanduser().resolve())])
    command = " ".join(shlex.quote(part) for part in args)
    return "\n".join(
        [
            "#!/bin/zsh",
            "set -euo pipefail",
            f"export PYTHONPATH={shlex.quote(str(runtime_dir))}:${{PYTHONPATH:-}}",
            f"mkdir -p {shlex.quote(str(workspace.state_dir))} {shlex.quote(str(log_path.parent))}",
            f"{command} >> {shlex.quote(str(log_path))} 2>&1",
            "",
        ]
    )


def _configured_live_db(workspace: Workspace, explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit.expanduser().resolve()
    config = _load_json(workspace.config_path)
    imports = config.get("imports")
    imessage = imports.get("imessage") if isinstance(imports, dict) else None
    configured = imessage.get("defaultMacPath") if isinstance(imessage, dict) else None
    return Path(str(configured or "~/Library/Messages/chat.db")).expanduser().resolve()


def _interval_launchd_plist(
    *,
    label: str,
    script_path: Path,
    interval_seconds: int,
    working_directory: Path,
    log_dir: Path,
) -> dict[str, object]:
    return {
        "Label": label,
        "ProgramArguments": ["/bin/zsh", str(script_path)],
        "StartInterval": interval_seconds,
        "RunAtLoad": True,
        "ProcessType": "Background",
        "ThrottleInterval": 60,
        "StandardOutPath": str(log_dir / "imessage-sync.stdout.log"),
        "StandardErrorPath": str(log_dir / "imessage-sync.stderr.log"),
        "WorkingDirectory": str(working_directory),
    }


def _load_json(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json_private(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(f"{json.dumps(payload, indent=2, sort_keys=True)}\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _timestamp_iso(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace("+00:00", "Z")
