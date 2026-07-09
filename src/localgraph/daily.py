from __future__ import annotations

import json
import plistlib
import sqlite3
import sys
from pathlib import Path

from .instagram import import_instagram_source, scan_instagram_source
from .paths import Workspace
from .store import (
    active_pending_imports,
    finish_import_run,
    get_source_location,
    has_completed_import_run,
    record_pending_import,
    resolve_pending_imports,
    set_source_location,
    start_import_run,
)


def configure_drive_source(db: sqlite3.Connection, workspace: Workspace, local_path: Path) -> dict[str, object]:
    workspace.root.mkdir(parents=True, exist_ok=True)
    resolved = local_path.expanduser().resolve()
    location_id = set_source_location(
        db,
        source_kind="instagram",
        location_kind="drive-desktop",
        label="default",
        local_path=str(resolved),
    )
    _update_workspace_config(workspace, resolved)
    db.commit()
    return {
        "sourceKind": "instagram",
        "locationKind": "drive-desktop",
        "label": "default",
        "id": location_id,
        "localPath": str(resolved),
        "config": str(workspace.config_path),
    }


def configured_drive_source(db: sqlite3.Connection, workspace: Workspace) -> Path:
    row = get_source_location(db, source_kind="instagram", location_kind="drive-desktop", label="default")
    if row and row["local_path"]:
        return Path(str(row["local_path"])).expanduser().resolve()
    config_path = workspace.config_path
    if config_path.exists():
        config = json.loads(config_path.read_text(encoding="utf-8"))
        drive_path = config.get("imports", {}).get("instagram", {}).get("driveLocalPath")
        if drive_path:
            return Path(str(drive_path)).expanduser().resolve()
    return workspace.instagram_source_dir


def run_daily_instagram_import(
    db: sqlite3.Connection,
    workspace: Workspace,
    *,
    source_path: Path | None = None,
    all_instagram_exports: bool = False,
) -> dict[str, object]:
    source = (source_path or configured_drive_source(db, workspace)).expanduser().resolve()
    run_id = start_import_run(db, run_kind="daily-import", source_kind="instagram")
    try:
        summary = _run_daily_import(db, workspace, run_id, source, all_instagram_exports=all_instagram_exports)
        status = "pending" if summary["pending"] else "completed"
        finish_import_run(db, run_id=run_id, status=status, summary=summary)
        _write_run_log(workspace, run_id, summary)
        db.commit()
        return summary
    except Exception as exc:
        summary = {
            "runId": run_id,
            "runKind": "daily-import",
            "sourceKind": "instagram",
            "sourcePath": str(source),
            "status": "failed",
            "error": str(exc),
        }
        finish_import_run(db, run_id=run_id, status="failed", summary=summary, error_text=str(exc))
        _write_run_log(workspace, run_id, summary)
        db.commit()
        raise


def build_launch_agent(
    workspace: Workspace,
    *,
    label: str = "com.localgraph.daily-import",
    hour: int = 8,
    minute: int = 15,
    python_executable: str | None = None,
) -> bytes:
    program = python_executable or sys.executable
    payload = {
        "Label": label,
        "ProgramArguments": [
            program,
            "-m",
            "localgraph",
            "--root",
            str(workspace.root),
            "daily-import",
        ],
        "StartCalendarInterval": {
            "Hour": hour,
            "Minute": minute,
        },
        "StandardOutPath": str(workspace.run_logs_dir / "daily-import.out.log"),
        "StandardErrorPath": str(workspace.run_logs_dir / "daily-import.err.log"),
        "WorkingDirectory": str(workspace.root),
    }
    return plistlib.dumps(payload, sort_keys=True)


def install_launch_agent(
    workspace: Workspace,
    *,
    output_path: Path | None = None,
    label: str = "com.localgraph.daily-import",
    hour: int = 8,
    minute: int = 15,
) -> dict[str, object]:
    target = output_path or Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = build_launch_agent(workspace, label=label, hour=hour, minute=minute)
    target.write_bytes(payload)
    workspace.scheduler_dir.mkdir(parents=True, exist_ok=True)
    copy_path = workspace.scheduler_dir / f"{label}.plist"
    copy_path.write_bytes(payload)
    return {
        "label": label,
        "path": str(target),
        "workspaceCopy": str(copy_path),
        "hour": hour,
        "minute": minute,
    }


def _run_daily_import(
    db: sqlite3.Connection,
    workspace: Workspace,
    run_id: int,
    source: Path,
    *,
    all_instagram_exports: bool,
) -> dict[str, object]:
    if not source.exists():
        record_pending_import(
            db,
            source_kind="instagram",
            source_identifier="drive-desktop:default",
            source_path=str(source),
            reason="source path missing",
        )
        return _pending_summary(run_id, source, "source path missing")

    scan = scan_instagram_source(source)
    exports = list(scan["exports"])  # type: ignore[index]
    if not exports:
        candidates = _candidate_export_dirs(source)
        if candidates:
            for candidate in candidates:
                record_pending_import(
                    db,
                    source_kind="instagram",
                    source_identifier=f"instagram:{candidate.relative_to(source).as_posix()}",
                    source_path=str(candidate),
                    reason="export not materialized",
                )
            reason = "candidate exports are not materialized"
        else:
            record_pending_import(
                db,
                source_kind="instagram",
                source_identifier="drive-desktop:default",
                source_path=str(source),
                reason="no materialized message files",
            )
            reason = "no materialized message files"
        return _pending_summary(run_id, source, reason)

    bootstrap = not has_completed_import_run(db, run_kind="daily-import", source_kind="instagram")
    if all_instagram_exports:
        selected_exports = exports
        mode = "full"
    elif bootstrap:
        selected_exports = exports
        mode = "bootstrap"
    else:
        selected_exports = [_newest_export(exports)]
        mode = "incremental"

    selected_paths = [Path(str(item["path"])) for item in selected_exports]
    result = import_instagram_source(db, source, selected_export_paths=selected_paths)
    resolve_pending_imports(db, source_kind="instagram")
    selected = [
        {
            "name": item["name"],
            "relativePath": item["relativePath"],
            "path": item["path"],
            "messageFiles": item["messageFiles"],
            "latestModifiedTime": item["latestModifiedTime"],
        }
        for item in selected_exports
    ]
    pending = [_row_to_dict(row) for row in active_pending_imports(db, source_kind="instagram")]
    return {
        "runId": run_id,
        "runKind": "daily-import",
        "sourceKind": "instagram",
        "status": "completed",
        "mode": mode,
        "sourcePath": str(source),
        "selectedExports": selected,
        "scan": scan,
        "result": result,
        "pending": pending,
    }


def _pending_summary(run_id: int, source: Path, reason: str) -> dict[str, object]:
    return {
        "runId": run_id,
        "runKind": "daily-import",
        "sourceKind": "instagram",
        "status": "pending",
        "mode": "pending",
        "sourcePath": str(source),
        "selectedExports": [],
        "reason": reason,
        "pending": [{"sourceKind": "instagram", "sourcePath": str(source), "reason": reason}],
    }


def _newest_export(exports: list[object]) -> dict[str, object]:
    return max(
        (dict(item) for item in exports),
        key=lambda item: (str(item.get("latestModifiedTime") or ""), str(item.get("relativePath") or "")),
    )


def _candidate_export_dirs(source: Path) -> list[Path]:
    if not source.exists():
        return []
    candidates: list[Path] = []
    for path in source.rglob("*"):
        if not path.is_dir():
            continue
        if path.name.startswith("instagram-") or (path / "your_instagram_activity").exists() or (path / "messages").exists():
            candidates.append(path)
    return sorted(set(candidates))


def _write_workspace_config(workspace: Workspace, config: dict[str, object]) -> None:
    workspace.config_path.write_text(f"{json.dumps(config, indent=2, sort_keys=True)}\n", encoding="utf-8")


def _update_workspace_config(workspace: Workspace, drive_path: Path) -> None:
    if workspace.config_path.exists():
        config = json.loads(workspace.config_path.read_text(encoding="utf-8"))
    else:
        config = {
            "formatVersion": 1,
            "root": str(workspace.root),
            "directories": {},
            "views": {},
            "imports": {},
        }
    imports = config.setdefault("imports", {})
    if not isinstance(imports, dict):
        imports = {}
        config["imports"] = imports
    instagram = imports.setdefault("instagram", {})
    if not isinstance(instagram, dict):
        instagram = {}
        imports["instagram"] = instagram
    instagram["driveLocalPath"] = str(drive_path)
    _write_workspace_config(workspace, config)


def _write_run_log(workspace: Workspace, run_id: int, summary: dict[str, object]) -> None:
    workspace.run_logs_dir.mkdir(parents=True, exist_ok=True)
    path = workspace.run_logs_dir / f"daily-import-{run_id}.json"
    path.write_text(f"{json.dumps(summary, indent=2, sort_keys=True)}\n", encoding="utf-8")


def _row_to_dict(row: sqlite3.Row) -> dict[str, object]:
    return {key: row[key] for key in row.keys()}
