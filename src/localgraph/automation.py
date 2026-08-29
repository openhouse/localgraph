from __future__ import annotations

import contextlib
import fcntl
import json
import os
import plistlib
import shlex
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .drive import (
    DriveAPIError,
    completed_instagram_export_paths,
    configured_drive_cache_dir,
    pull_configured_google_drive_source,
)
from .ingest import (
    SourceImportResult,
    clear_instagram_projection,
    import_imessage_chat_db,
    import_instagram_source,
)
from .instagram import detect_export_root, scan_instagram_source
from .instagram_accounts import InstagramAccount, instagram_account, instagram_accounts
from .paths import Workspace
from .render import render_views
from .schema import connect, initialize_schema
from .slug import stable_hash


DEFAULT_LAUNCHD_LABEL = "com.openhouse.localgraph.daily-import"
DEFAULT_INSTAGRAM_SYNC_LABEL = "com.openhouse.localgraph.instagram-sync"
DEFAULT_INSTAGRAM_SYNC_INTERVAL_MINUTES = 60


@contextlib.contextmanager
def instagram_sync_lock(workspace: Workspace) -> Iterator[bool]:
    lock_path = workspace.state_dir / "instagram-sync.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        lock_path.chmod(0o600)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(f"{os.getpid()}\n")
            handle.flush()
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@dataclass
class DriveSourceResolution:
    path: Path
    origin: str
    warnings: list[str] = field(default_factory=list)
    resolved_export_path: Path | None = None
    resolved_export_paths: list[Path] = field(default_factory=list)

    def to_json(self) -> dict[str, object]:
        result: dict[str, object] = {
            "path": str(self.path),
            "origin": self.origin,
            "warnings": self.warnings,
        }
        if self.resolved_export_path is not None:
            result["resolvedExportPath"] = str(self.resolved_export_path)
        if self.resolved_export_paths:
            result["resolvedExportPaths"] = [str(path) for path in self.resolved_export_paths]
        return result


def configure_google_drive_source(workspace: Workspace, source_path: Path) -> dict[str, object]:
    workspace.ensure_workspace(force=False)
    source = source_path.expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"Google Drive Instagram source does not exist: {source}")
    config = load_config(workspace)
    imports = config.setdefault("imports", {})
    instagram = imports.setdefault("instagram", {})
    instagram["googleDriveLocalPath"] = str(source)
    write_config(workspace, config)
    return {
        "workspace": str(workspace.root),
        "instagramGoogleDriveSource": str(source),
        "config": str(workspace.config_path),
    }


def configure_instagram_baseline(
    workspace: Workspace,
    export_name: str,
    *,
    account_key: str | None = None,
) -> dict[str, object]:
    workspace.ensure_workspace(force=False)
    completed = completed_instagram_export_paths(workspace, account_key=account_key)
    matches = [path for path in completed if path.name == export_name]
    if not matches:
        raise ValueError(f"completed Instagram export is not available locally: {export_name}")
    if len(matches) > 1:
        raise ValueError(f"Instagram baseline export name is ambiguous: {export_name}")
    config = load_config(workspace)
    instagram = config.setdefault("imports", {}).setdefault("instagram", {})  # type: ignore[union-attr]
    if account_key is None:
        target = instagram
    else:
        accounts = instagram.get("accounts") if isinstance(instagram, dict) else None
        target = accounts.get(account_key) if isinstance(accounts, dict) else None
        if not isinstance(target, dict):
            raise ValueError(f"Instagram account is not configured: {account_key}")
    recorded_at = _now_iso()
    target["baselineExportName"] = export_name
    target["baselineRecordedAt"] = recorded_at
    write_config(workspace, config)
    _advance_instagram_baseline_receipts(
        workspace,
        account_key=account_key,
        export_name=export_name,
        recorded_at=recorded_at,
    )
    result: dict[str, object] = {
        "baselineExportName": export_name,
        "baselineExportPath": str(matches[0]),
        "historyCoverage": "complete-through-latest-export",
        "config": str(workspace.config_path),
    }
    if account_key is not None:
        result["accountKey"] = account_key
    return result


def _advance_instagram_baseline_receipts(
    workspace: Workspace,
    *,
    account_key: str | None,
    export_name: str,
    recorded_at: str,
) -> None:
    if account_key is None:
        status_path = workspace.state_dir / "instagram-sync-status.json"
    else:
        status_path = instagram_account(workspace, account_key).sync_status_path
    status = _load_json_path(status_path)
    if status:
        status.update(
            {
                "baselineExportName": export_name,
                "baselineRecordedAt": recorded_at,
                "historyCoverage": "complete-through-latest-export",
            }
        )
        _write_json_private_path(status_path, status)

    if account_key is None:
        return
    aggregate_path = workspace.state_dir / "instagram-accounts-sync-status.json"
    aggregate = _load_json_path(aggregate_path)
    accounts = aggregate.get("accounts") if isinstance(aggregate, dict) else None
    account_status = accounts.get(account_key) if isinstance(accounts, dict) else None
    if isinstance(account_status, dict):
        account_status["historyCoverage"] = "complete-through-latest-export"
        _write_json_private_path(aggregate_path, aggregate)


def run_daily_import(
    workspace: Workspace,
    *,
    instagram_drive_source: Path | None = None,
    imessage_db: Path | None = None,
    me_name: str = "Me",
    me_instagram_names: list[str] | None = None,
    me_imessage_handles: list[str] | None = None,
    skip_instagram: bool = False,
    skip_imessage: bool = False,
    render: bool = True,
    write_config_on_discovery: bool = False,
    latest_instagram_only: bool = True,
    replace_instagram_snapshot: bool = False,
) -> dict[str, object]:
    workspace.ensure_workspace(force=False)
    configured_accounts = instagram_accounts(workspace)
    if configured_accounts and not skip_instagram and instagram_drive_source is None:
        return run_multi_account_instagram_sync(
            workspace,
            accounts=configured_accounts,
            imessage_db=imessage_db,
            me_name=me_name,
            me_imessage_handles=me_imessage_handles or [],
            skip_imessage=skip_imessage,
            render=render,
        )
    drive_pull: dict[str, object] | None = None
    drive_pull_error: str | None = None
    resolution: DriveSourceResolution | None = None
    if not skip_instagram:
        try:
            pull_result = pull_configured_google_drive_source(workspace)
            if pull_result is not None:
                drive_pull = pull_result.to_json()
                latest_export = resolve_latest_instagram_export_source(pull_result.cache_path)
                if latest_export is not None:
                    current = instagram_current_mirror_path(workspace)
                    for completed_export in pull_result.completed_export_paths or [latest_export]:
                        current = activate_instagram_current_mirror(workspace, completed_export)
                    resolution = DriveSourceResolution(
                        current,
                        "google-drive-api-current",
                        resolved_export_path=current.resolve(),
                        resolved_export_paths=pull_result.completed_export_paths or [latest_export],
                    )
                else:
                    resolution = DriveSourceResolution(
                        pull_result.cache_path.expanduser().resolve(),
                        "google-drive-api",
                    )
        except DriveAPIError as exc:
            drive_pull_error = str(exc)
            current = valid_instagram_current_mirror(workspace)
            completed_exports = completed_instagram_export_paths(workspace)
            if current is not None:
                resolution = DriveSourceResolution(
                    current,
                    "google-drive-last-known-good",
                    ["authenticated Google Drive pull failed; using the last completed local mirror"],
                    resolved_export_path=current.resolve(),
                    resolved_export_paths=completed_exports or [current.resolve()],
                )
            else:
                cache_candidate = configured_drive_cache_dir(workspace)
                if cache_candidate.exists() and int(scan_instagram_source(cache_candidate)["totalMessageFiles"]) > 0:
                    resolution = DriveSourceResolution(
                        cache_candidate.expanduser().resolve(),
                        "google-drive-cache",
                        ["authenticated Google Drive pull failed; using the existing private cache"],
                    )

    if resolution is None:
        resolution = resolve_instagram_drive_source(workspace, explicit=instagram_drive_source)
    if write_config_on_discovery and resolution.origin in {"explicit", "discovered"}:
        configure_google_drive_source(workspace, resolution.path)

    instagram_import_sources: list[Path] = []
    instagram_pending_warning: str | None = None
    snapshot_replacement: dict[str, int] | None = None

    imessage_path = imessage_db.expanduser().resolve() if imessage_db else workspace.imessage_chat_db_path
    with connect(workspace.database_path) as db:
        initialize_schema(db)
        bootstrap_instagram = False
        if not skip_instagram:
            bootstrap_instagram = (not latest_instagram_only) or (
                not replace_instagram_snapshot and not _has_instagram_imports(db)
            )
            if resolution.resolved_export_paths:
                instagram_import_sources = [path.resolve() for path in resolution.resolved_export_paths]
            else:
                instagram_import_sources = resolve_instagram_import_sources(
                    resolution.path,
                    all_materialized_exports=bootstrap_instagram,
                )
            if not instagram_import_sources:
                instagram_pending_warning = (
                    "no materialized Instagram export found under the Google Drive source; "
                    "Drive may still be syncing or the folder may be online-only"
                )

        source_results: list[SourceImportResult] = []
        if not skip_instagram:
            if instagram_pending_warning is not None:
                source_results.append(
                    SourceImportResult(
                        "instagram",
                        str(resolution.path),
                        status="pending",
                        warnings=[instagram_pending_warning],
                    )
                )
            else:
                if replace_instagram_snapshot:
                    snapshot_replacement = clear_instagram_projection(db)
                for source in instagram_import_sources:
                    imported = import_instagram_source(
                        db,
                        source,
                        me_name=me_name,
                        me_names=me_instagram_names or [],
                        explicit=instagram_drive_source is not None,
                    )
                    if replace_instagram_snapshot and (imported.status != "imported" or imported.messages <= 0):
                        raise ValueError("refusing to replace Instagram projection with an empty snapshot")
                    source_results.append(imported)
        if not skip_imessage:
            source_results.append(
                import_imessage_chat_db(
                    db,
                    imessage_path,
                    me_name=me_name,
                    me_handles=me_imessage_handles or [],
                    explicit=imessage_db is not None,
                )
            )
        result = _combined_import_result(source_results)
        if render:
            source_scan = combined_instagram_source_scan(instagram_import_sources) if instagram_import_sources else None
            result["render"] = render_views(db, workspace, source_scan=source_scan)

    started_at = _now_iso()
    sync_status = instagram_sync_status(
        workspace,
        checked_at=started_at,
        resolution=resolution,
        drive_pull=drive_pull,
        drive_pull_error=drive_pull_error,
        import_sources=instagram_import_sources,
        pending=instagram_pending_warning is not None,
    )
    summary = {
        "startedAt": started_at,
        "workspace": str(workspace.root),
        "instagram": {
            **resolution.to_json(),
            "bootstrap": bootstrap_instagram,
            "importPath": str(instagram_import_sources[0]) if len(instagram_import_sources) == 1 else None,
            "importPaths": [str(source) for source in instagram_import_sources],
            "latestOnly": latest_instagram_only,
            "snapshotReplacement": snapshot_replacement,
        },
        "googleDrivePull": drive_pull or {
            "status": "error" if drive_pull_error else "not-configured",
            "error": drive_pull_error,
        },
        "instagramSync": sync_status,
        "result": result,
    }
    write_instagram_sync_status(workspace, sync_status)
    run_log = append_daily_run_log(workspace, summary)
    summary["runLog"] = str(run_log)
    return summary


@dataclass
class InstagramAccountPreparation:
    account: InstagramAccount
    status: str
    origin: str
    sources: list[Path] = field(default_factory=list)
    drive_pull: dict[str, object] | None = None
    error: str | None = None

    @property
    def ready(self) -> bool:
        return bool(self.sources)


def run_multi_account_instagram_sync(
    workspace: Workspace,
    *,
    accounts: list[InstagramAccount],
    imessage_db: Path | None,
    me_name: str,
    me_imessage_handles: list[str],
    skip_imessage: bool,
    render: bool,
) -> dict[str, object]:
    checked_at = _now_iso()
    preparations = [_prepare_instagram_account(workspace, account) for account in accounts]
    all_ready = all(preparation.ready for preparation in preparations)
    source_results: list[SourceImportResult] = []
    snapshot_replacement: dict[str, int] | None = None
    rendered: dict[str, int] | None = None

    with connect(workspace.database_path) as db:
        initialize_schema(db)
        if all_ready:
            snapshot_replacement = clear_instagram_projection(db)
            try:
                for preparation in preparations:
                    account = preparation.account
                    for source in preparation.sources:
                        source_results.append(
                            import_instagram_source(
                                db,
                                source,
                                me_name=account.owner_display_name,
                                me_names=list(account.self_names),
                                explicit=True,
                                account_key=account.account_key,
                                owner_identity_key=account.owner_identity_key,
                                owner_kind=account.owner_kind,
                                commit=False,
                            )
                        )
                if any(result.status != "imported" or result.messages <= 0 for result in source_results):
                    raise ValueError("refusing to replace Instagram projection with an empty account snapshot")
                db.commit()
            except Exception:
                db.rollback()
                raise
            if not skip_imessage:
                source_results.append(
                    import_imessage_chat_db(
                        db,
                        imessage_db.expanduser().resolve() if imessage_db else workspace.imessage_chat_db_path,
                        me_name=me_name,
                        me_handles=me_imessage_handles,
                        explicit=imessage_db is not None,
                    )
                )
            if render:
                source_scan = combined_instagram_source_scan(
                    [source for preparation in preparations for source in preparation.sources]
                )
                rendered = render_views(db, workspace, source_scan=source_scan)
        else:
            for preparation in preparations:
                source_results.append(
                    SourceImportResult(
                        "instagram",
                        str(preparation.account.current_mirror_path),
                        status=preparation.status if preparation.status == "pending" else "held",
                        warnings=[preparation.error] if preparation.error else [],
                    )
                )

    account_payload: dict[str, object] = {}
    for preparation in preparations:
        status = _instagram_account_sync_status(preparation, checked_at=checked_at)
        _write_json_private_path(preparation.account.sync_status_path, status)
        account_payload[preparation.account.account_key] = {
            "account": preparation.account.to_public_json(),
            "sync": status,
            "googleDrivePull": preparation.drive_pull
            or {"status": "error" if preparation.error else "not-configured", "error": preparation.error},
        }

    statuses = [str(item["sync"]["status"]) for item in account_payload.values()]  # type: ignore[index]
    if any(status == "pending" for status in statuses):
        aggregate_status = "pending"
    elif any(status == "degraded" for status in statuses):
        aggregate_status = "degraded"
    else:
        aggregate_status = "current"
    aggregate = {
        "schemaVersion": 2,
        "status": aggregate_status,
        "checkedAt": checked_at,
        "accountsConfigured": len(preparations),
        "accountsReady": sum(preparation.ready for preparation in preparations),
        "projectionUpdated": all_ready,
        "accounts": {
            key: {
                "status": value["sync"]["status"],  # type: ignore[index]
                "historyCoverage": value["sync"]["historyCoverage"],  # type: ignore[index]
                "messageFiles": value["sync"]["messageFiles"],  # type: ignore[index]
            }
            for key, value in account_payload.items()
        },
    }
    _write_json_private_path(workspace.state_dir / "instagram-accounts-sync-status.json", aggregate)
    result = _combined_import_result(source_results)
    if rendered is not None:
        result["render"] = rendered
    summary = {
        "startedAt": checked_at,
        "workspace": str(workspace.root),
        "instagramAccounts": account_payload,
        "instagramSync": aggregate,
        "instagram": {
            "mode": "multi-account",
            "snapshotReplacement": snapshot_replacement,
            "importPaths": [
                str(source) for preparation in preparations for source in preparation.sources
            ],
        },
        "result": result,
    }
    run_log = append_daily_run_log(workspace, summary)
    summary["runLog"] = str(run_log)
    return summary


def _prepare_instagram_account(
    workspace: Workspace,
    account: InstagramAccount,
) -> InstagramAccountPreparation:
    pull_payload: dict[str, object] | None = None
    error: str | None = None
    if account.google_drive_folder_id:
        try:
            pulled = pull_configured_google_drive_source(workspace, account_key=account.account_key)
            if pulled is not None:
                pull_payload = pulled.to_json()
                for completed_export in pulled.completed_export_paths:
                    activate_instagram_account_current_mirror(account, completed_export)
        except DriveAPIError as exc:
            error = str(exc)
    current = valid_instagram_account_current_mirror(account)
    if current is not None:
        if error:
            status, origin = "degraded", "google-drive-last-known-good"
        elif pull_payload is not None:
            status, origin = "current", "google-drive-api-current"
        else:
            status, origin = "local-current", "local-current"
        return InstagramAccountPreparation(
            account,
            status,
            origin,
            [current],
            drive_pull=pull_payload,
            error=error,
        )
    if account.google_drive_local_path is not None:
        sources = [
            source
            for source in resolve_instagram_import_sources(
                account.google_drive_local_path,
                all_materialized_exports=True,
            )
            if source.name.startswith(account.export_name_prefix)
        ]
        if sources:
            return InstagramAccountPreparation(account, "local-current", "configured-local", sources)
    return InstagramAccountPreparation(account, "pending", "not-materialized", error=error)


def _instagram_account_sync_status(
    preparation: InstagramAccountPreparation,
    *,
    checked_at: str,
) -> dict[str, object]:
    account = preparation.account
    exports: list[str] = []
    message_files = 0
    for source in preparation.sources:
        scan = scan_instagram_source(source)
        message_files += int(scan["totalMessageFiles"])
        exports.extend(
            str(item["name"])
            for item in scan["exports"]  # type: ignore[union-attr]
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        )
    baseline_present = account.baseline_export_name in exports if account.baseline_export_name else False
    previous = _load_json_path(account.sync_status_path)
    last_success = checked_at if preparation.status in {"current", "local-current"} else previous.get("lastSuccessfulSyncAt")
    return {
        "schemaVersion": 2,
        "accountKey": account.account_key,
        "profileName": account.profile_name,
        "status": preparation.status,
        "checkedAt": checked_at,
        "lastSuccessfulSyncAt": last_success,
        "origin": preparation.origin,
        "localMirrorPath": str(account.current_mirror_path) if preparation.ready else None,
        "messageFiles": message_files,
        "completedExports": len(set(exports)),
        "historyCoverage": "complete-through-latest-export" if baseline_present else "baseline-required",
        "baselineExportName": account.baseline_export_name,
        "pullStatus": preparation.drive_pull.get("status") if preparation.drive_pull else ("error" if preparation.error else "not-configured"),
        "lastError": preparation.error,
    }


def resolve_instagram_drive_source(workspace: Workspace, *, explicit: Path | None = None) -> DriveSourceResolution:
    warnings: list[str] = []
    if explicit is not None:
        source = explicit.expanduser().resolve()
        if not source.exists():
            raise FileNotFoundError(f"Google Drive Instagram source does not exist: {source}")
        return DriveSourceResolution(source, "explicit")

    configured = configured_instagram_drive_source(workspace)
    if configured is not None:
        if configured.exists():
            return DriveSourceResolution(configured, "configured")
        warnings.append(f"configured Google Drive source is missing: {configured}")

    for candidate in candidate_instagram_drive_sources():
        if candidate.exists():
            return DriveSourceResolution(candidate.resolve(), "discovered", warnings)

    fallback = workspace.instagram_source_dir
    warnings.append("no Google Drive Instagram source found; falling back to workspace sources/instagram")
    return DriveSourceResolution(fallback, "workspace-fallback", warnings)


def configured_instagram_drive_source(workspace: Workspace) -> Path | None:
    config = load_config(workspace)
    value = (
        config.get("imports", {})
        .get("instagram", {})
        .get("googleDriveLocalPath")
    )
    if not value:
        return None
    return Path(str(value)).expanduser().resolve()


def candidate_instagram_drive_sources(home: Path | None = None) -> list[Path]:
    candidates: list[Path] = []
    for root in google_drive_roots(home=home):
        candidates.extend(
            [
                root / "Shared drives" / "Instagram",
                root / "My Drive" / "Instagram",
            ]
        )
    return candidates


def latest_instagram_export_source(source_path: Path) -> Path:
    return resolve_latest_instagram_export_source(source_path) or source_path.expanduser().resolve()


def resolve_latest_instagram_export_source(source_path: Path) -> Path | None:
    sources = resolve_instagram_import_sources(source_path, all_materialized_exports=False)
    return sources[0] if sources else None


def resolve_instagram_import_sources(source_path: Path, *, all_materialized_exports: bool) -> list[Path]:
    source = source_path.expanduser().resolve()
    candidates = _materialized_instagram_export_sources(source)

    if candidates:
        sorted_candidates = sorted(set(candidates), key=_path_freshness_key)
        if all_materialized_exports:
            return sorted_candidates
        return [sorted_candidates[-1]]

    if _is_google_drive_path(source):
        return []
    return [source]


def instagram_current_mirror_path(workspace: Workspace) -> Path:
    return workspace.sources_dir / "instagram-current"


def instagram_completed_mirror_path(workspace: Workspace) -> Path:
    return workspace.sources_dir / "instagram-completed-exports"


def activate_instagram_current_mirror(workspace: Workspace, export_source: Path) -> Path:
    export = export_source.expanduser().resolve()
    message_files = int(scan_instagram_source(export)["totalMessageFiles"])
    if message_files <= 0:
        raise ValueError(f"refusing to publish an Instagram mirror without message files: {export}")
    current = instagram_current_mirror_path(workspace)
    if current.exists() and not current.is_symlink():
        raise ValueError(f"refusing to replace a non-symlink Instagram mirror path: {current}")
    current.parent.mkdir(parents=True, exist_ok=True)
    completed = instagram_completed_mirror_path(workspace)
    if completed.exists() and not completed.is_dir():
        raise ValueError(f"refusing to replace a non-directory Instagram completed mirror: {completed}")
    completed.mkdir(parents=True, exist_ok=True)
    if current.is_symlink() and current.exists():
        previous = current.resolve()
        if previous != completed.resolve() and int(scan_instagram_source(previous)["totalMessageFiles"]) > 0:
            _publish_completed_export_link(completed, previous)
    _publish_completed_export_link(completed, export)
    temporary = current.with_name(f".{current.name}.{os.getpid()}.tmp")
    if temporary.is_symlink() or temporary.exists():
        temporary.unlink()
    target = Path(os.path.relpath(completed, start=current.parent))
    temporary.symlink_to(target, target_is_directory=True)
    os.replace(temporary, current)
    return current


def _publish_completed_export_link(completed: Path, export: Path) -> Path:
    link = completed / export.name
    if link.is_symlink() and link.resolve() == export:
        return link
    if link.exists() or link.is_symlink():
        link = completed / f"{export.name}--{stable_hash(str(export), length=10)}"
        if link.is_symlink() and link.resolve() == export:
            return link
        if link.exists() or link.is_symlink():
            raise ValueError(f"refusing to replace an existing completed Instagram export link: {link}")
    temporary = completed / f".{link.name}.{os.getpid()}.tmp"
    if temporary.exists() or temporary.is_symlink():
        temporary.unlink()
    temporary.symlink_to(Path(os.path.relpath(export, start=completed)), target_is_directory=True)
    os.replace(temporary, link)
    return link


def valid_instagram_current_mirror(workspace: Workspace) -> Path | None:
    current = instagram_current_mirror_path(workspace)
    if not current.is_symlink() or not current.exists():
        return None
    if int(scan_instagram_source(current)["totalMessageFiles"]) <= 0:
        return None
    return current


def activate_instagram_account_current_mirror(account: InstagramAccount, export_source: Path) -> Path:
    export = export_source.expanduser().resolve()
    if int(scan_instagram_source(export)["totalMessageFiles"]) <= 0:
        raise ValueError(f"refusing to publish an Instagram mirror without message files: {export}")
    current = account.current_mirror_path
    completed = account.completed_mirror_path
    if current.exists() and not current.is_symlink():
        raise ValueError(f"refusing to replace a non-symlink Instagram account mirror path: {current}")
    if completed.exists() and not completed.is_dir():
        raise ValueError(f"refusing to replace a non-directory Instagram account completed mirror: {completed}")
    current.parent.mkdir(parents=True, exist_ok=True)
    completed.mkdir(parents=True, exist_ok=True)
    if current.is_symlink() and current.exists():
        previous = current.resolve()
        if previous != completed.resolve() and int(scan_instagram_source(previous)["totalMessageFiles"]) > 0:
            _publish_completed_export_link(completed, previous)
    _publish_completed_export_link(completed, export)
    temporary = current.with_name(f".{current.name}.{os.getpid()}.tmp")
    if temporary.is_symlink() or temporary.exists():
        temporary.unlink()
    temporary.symlink_to(Path(os.path.relpath(completed, start=current.parent)), target_is_directory=True)
    os.replace(temporary, current)
    return current


def valid_instagram_account_current_mirror(account: InstagramAccount) -> Path | None:
    current = account.current_mirror_path
    if not current.exists():
        return None
    if int(scan_instagram_source(current)["totalMessageFiles"]) <= 0:
        return None
    return current


def _materialized_instagram_export_sources(source: Path) -> list[Path]:
    if _looks_like_instagram_export(source):
        return [source.expanduser().resolve()]
    candidates = [*(_indexed_instagram_export_sources(source)), *(_subprocess_shallow_instagram_export_sources(source))]
    return sorted(set(candidates), key=_path_freshness_key)


def _looks_like_instagram_export(path: Path) -> bool:
    return any(
        (path / relative).is_dir()
        for relative in (
            "your_instagram_activity/messages/inbox",
            "your_instagram_activity/messages/message_requests",
            "messages/inbox",
            "messages/message_requests",
        )
    )


def google_drive_roots(home: Path | None = None) -> list[Path]:
    base = (home or Path.home()).expanduser()
    roots: list[Path] = []
    cloud_storage = base / "Library" / "CloudStorage"
    if cloud_storage.exists():
        roots.extend(sorted(path for path in cloud_storage.glob("GoogleDrive-*") if path.is_dir()))
    legacy = base / "Google Drive"
    if legacy.is_dir():
        roots.append(legacy)
    return roots


def install_daily_import(
    workspace: Workspace,
    *,
    hour: int = 3,
    minute: int = 15,
    label: str = DEFAULT_LAUNCHD_LABEL,
    instagram_drive_source: Path | None = None,
    skip_imessage: bool = False,
    me_name: str = "Me",
    me_instagram_names: list[str] | None = None,
    me_imessage_handles: list[str] | None = None,
    dry_run: bool = False,
    home: Path | None = None,
) -> dict[str, object]:
    if not (0 <= hour <= 23):
        raise ValueError("--hour must be between 0 and 23")
    if not (0 <= minute <= 59):
        raise ValueError("--minute must be between 0 and 59")
    workspace.ensure_workspace(force=False)
    home_dir = (home or Path.home()).expanduser()
    script_path = workspace.state_dir / "bin" / "localgraph-daily-import.sh"
    plist_path = home_dir / "Library" / "LaunchAgents" / f"{label}.plist"
    script = daily_import_script(
        workspace,
        instagram_drive_source=instagram_drive_source,
        skip_imessage=skip_imessage,
        me_name=me_name,
        me_instagram_names=me_instagram_names or [],
        me_imessage_handles=me_imessage_handles or [],
    )
    plist = launchd_plist(label=label, script_path=script_path, hour=hour, minute=minute, workspace=workspace)

    if not dry_run:
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text(script, encoding="utf-8")
        script_path.chmod(0o755)
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        plist_path.write_bytes(plistlib.dumps(plist, sort_keys=True))

    return {
        "label": label,
        "script": str(script_path),
        "plist": str(plist_path),
        "hour": hour,
        "minute": minute,
        "dryRun": dry_run,
        "loadCommand": f"launchctl load {shlex.quote(str(plist_path))}",
        "runCommand": f"{shlex.quote(str(script_path))}",
    }


def install_instagram_sync(
    workspace: Workspace,
    *,
    interval_minutes: int = DEFAULT_INSTAGRAM_SYNC_INTERVAL_MINUTES,
    label: str = DEFAULT_INSTAGRAM_SYNC_LABEL,
    me_name: str = "Me",
    me_instagram_names: list[str] | None = None,
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
    script_path = support_dir / "bin" / "localgraph-instagram-sync.sh"
    log_dir = support_dir / "logs"
    plist_path = home_dir / "Library" / "LaunchAgents" / f"{label}.plist"
    script = instagram_sync_script(
        workspace,
        runtime_dir=runtime_dir,
        log_path=log_dir / "instagram-sync.log",
        me_name=me_name,
        me_instagram_names=[] if instagram_accounts(workspace) else (me_instagram_names or []),
    )
    plist = interval_launchd_plist(
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
        "runCommand": f"{shlex.quote(str(script_path))}",
    }


def daily_import_script(
    workspace: Workspace,
    *,
    instagram_drive_source: Path | None,
    skip_imessage: bool,
    me_name: str,
    me_instagram_names: list[str],
    me_imessage_handles: list[str],
) -> str:
    repo_root = Path(__file__).resolve().parents[2]
    python_path = repo_root / "src"
    args = [
        sys.executable,
        "-m",
        "localgraph",
        "--root",
        str(workspace.root),
        "daily-import",
        "--me",
        me_name,
        "--write-config",
    ]
    if instagram_drive_source is not None:
        args.extend(["--instagram-drive-source", str(instagram_drive_source.expanduser().resolve())])
    if skip_imessage:
        args.append("--skip-imessage")
    for value in me_instagram_names:
        args.extend(["--me-instagram", value])
    for value in me_imessage_handles:
        args.extend(["--me-imessage", value])

    exports = []
    if (repo_root / "pyproject.toml").exists():
        exports.append(f"export PYTHONPATH={shlex.quote(str(python_path))}:${{PYTHONPATH:-}}")
    command = " ".join(shlex.quote(part) for part in args)
    log_path = workspace.state_dir / "daily-import.launchd.log"
    return "\n".join(
        [
            "#!/bin/zsh",
            "set -euo pipefail",
            *exports,
            f"mkdir -p {shlex.quote(str(workspace.state_dir))}",
            f"{command} >> {shlex.quote(str(log_path))} 2>&1",
            "",
        ]
    )


def instagram_sync_script(
    workspace: Workspace,
    *,
    runtime_dir: Path,
    log_path: Path,
    me_name: str,
    me_instagram_names: list[str],
) -> str:
    args = [
        sys.executable,
        "-m",
        "localgraph",
        "--root",
        str(workspace.root),
        "instagram-sync",
        "--me",
        me_name,
    ]
    for value in me_instagram_names:
        args.extend(["--me-instagram", value])

    exports = [f"export PYTHONPATH={shlex.quote(str(runtime_dir))}:${{PYTHONPATH:-}}"]
    command = " ".join(shlex.quote(part) for part in args)
    return "\n".join(
        [
            "#!/bin/zsh",
            "set -euo pipefail",
            *exports,
            f"mkdir -p {shlex.quote(str(workspace.state_dir))} {shlex.quote(str(log_path.parent))}",
            f"{command} >> {shlex.quote(str(log_path))} 2>&1",
            "",
        ]
    )


def launchd_plist(*, label: str, script_path: Path, hour: int, minute: int, workspace: Workspace) -> dict[str, object]:
    log_path = workspace.state_dir / "daily-import.launchd.stdout.log"
    error_log_path = workspace.state_dir / "daily-import.launchd.stderr.log"
    return {
        "Label": label,
        "ProgramArguments": ["/bin/zsh", str(script_path)],
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "RunAtLoad": False,
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(error_log_path),
        "WorkingDirectory": str(workspace.root),
    }


def interval_launchd_plist(
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
        "StandardOutPath": str(log_dir / "instagram-sync.stdout.log"),
        "StandardErrorPath": str(log_dir / "instagram-sync.stderr.log"),
        "WorkingDirectory": str(working_directory),
    }


def append_daily_run_log(workspace: Workspace, summary: dict[str, object]) -> Path:
    path = workspace.state_dir / "daily-import-runs.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{json.dumps(summary, ensure_ascii=False, sort_keys=True, default=str)}\n")
    return path


def instagram_sync_status(
    workspace: Workspace,
    *,
    checked_at: str,
    resolution: DriveSourceResolution,
    drive_pull: dict[str, object] | None,
    drive_pull_error: str | None,
    import_sources: list[Path],
    pending: bool,
) -> dict[str, object]:
    previous = load_instagram_sync_status(workspace)
    current = valid_instagram_current_mirror(workspace)
    message_files = sum(int(scan_instagram_source(source)["totalMessageFiles"]) for source in import_sources)
    instagram_config = load_config(workspace).get("imports", {}).get("instagram", {})
    baseline_export_name = (
        instagram_config.get("baselineExportName") if isinstance(instagram_config, dict) else None
    )
    baseline_present = isinstance(baseline_export_name, str) and any(
        source.name == baseline_export_name for source in import_sources
    )
    if resolution.origin == "google-drive-api-current" and not pending:
        status = "current"
        last_successful_sync_at: object = checked_at
    elif resolution.origin == "google-drive-last-known-good":
        status = "degraded"
        last_successful_sync_at = previous.get("lastSuccessfulSyncAt")
    elif pending:
        status = "pending"
        last_successful_sync_at = previous.get("lastSuccessfulSyncAt")
    else:
        status = "local-fallback"
        last_successful_sync_at = previous.get("lastSuccessfulSyncAt")
    return {
        "schemaVersion": 1,
        "status": status,
        "checkedAt": checked_at,
        "lastSuccessfulSyncAt": last_successful_sync_at,
        "localMirrorPath": str(current) if current is not None else None,
        "resolvedExportPath": str(current.resolve()) if current is not None else None,
        "messageFiles": message_files,
        "completedExports": len(import_sources),
        "historyCoverage": "complete-through-latest-export" if baseline_present else "baseline-required",
        "baselineExportName": baseline_export_name,
        "origin": resolution.origin,
        "pullStatus": drive_pull.get("status") if drive_pull is not None else ("error" if drive_pull_error else "not-configured"),
        "lastError": drive_pull_error,
    }


def load_instagram_sync_status(workspace: Workspace) -> dict[str, object]:
    path = workspace.state_dir / "instagram-sync-status.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_instagram_sync_status(workspace: Workspace, status: dict[str, object]) -> Path:
    path = workspace.state_dir / "instagram-sync-status.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(f"{json.dumps(status, indent=2, sort_keys=True)}\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)
    return path


def _load_json_path(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json_private_path(path: Path, payload: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(f"{json.dumps(payload, indent=2, sort_keys=True)}\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)
    return path


def load_config(workspace: Workspace) -> dict[str, object]:
    if not workspace.config_path.exists():
        return {}
    return json.loads(workspace.config_path.read_text(encoding="utf-8"))


def write_config(workspace: Workspace, config: dict[str, object]) -> None:
    workspace.config_path.write_text(f"{json.dumps(config, indent=2, sort_keys=True)}\n", encoding="utf-8")


def combined_instagram_source_scan(sources: list[Path]) -> dict[str, object]:
    scans = [scan_instagram_source(source) for source in sources]
    exports: list[object] = []
    total_message_files = 0
    for scan in scans:
        exports.extend(scan["exports"])  # type: ignore[arg-type]
        total_message_files += int(scan["totalMessageFiles"])
    return {
        "sourceKind": "instagram",
        "sourcePath": ", ".join(str(source) for source in sources),
        "exports": exports,
        "totalMessageFiles": total_message_files,
    }


def _combined_import_result(results: list[SourceImportResult]) -> dict[str, object]:
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


def _has_instagram_imports(db: sqlite3.Connection) -> bool:
    row = db.execute("SELECT COUNT(*) AS count FROM source_imports WHERE source_kind = 'instagram'").fetchone()
    return bool(row and int(row["count"]) > 0)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _indexed_instagram_export_sources(source: Path) -> list[Path]:
    mdfind = Path("/usr/bin/mdfind")
    if not mdfind.exists():
        return []
    query = 'kMDItemFSName == "message_1.json" || kMDItemFSName == "message.json"'
    try:
        completed = subprocess.run(
            [str(mdfind), "-onlyin", str(source), query],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if completed.returncode != 0:
        return []

    roots: set[Path] = set()
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue
        file_path = Path(line.strip())
        try:
            roots.add(detect_export_root(source, file_path).resolve())
        except ValueError:
            continue
    return sorted(roots)


def _subprocess_shallow_instagram_export_sources(source: Path) -> list[Path]:
    script = r"""
import json
import sys
from pathlib import Path

source = Path(sys.argv[1])
known_message_dirs = (
    "your_instagram_activity/messages/inbox",
    "your_instagram_activity/messages/message_requests",
    "messages/inbox",
    "messages/message_requests",
)

def looks_like_export(path):
    return any((path / relative).is_dir() for relative in known_message_dirs)

def iter_dirs(path):
    try:
        return sorted(child for child in path.iterdir() if child.is_dir())
    except OSError:
        return []

roots = []
if looks_like_export(source):
    roots.append(str(source))
else:
    for child in iter_dirs(source):
        if looks_like_export(child):
            roots.append(str(child))
        else:
            for grandchild in iter_dirs(child):
                if looks_like_export(grandchild):
                    roots.append(str(grandchild))
print(json.dumps(roots))
"""
    try:
        completed = subprocess.run(
            [sys.executable, "-c", script, str(source)],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if completed.returncode != 0:
        return []
    try:
        values = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return []
    if not isinstance(values, list):
        return []
    return sorted(Path(str(value)).resolve() for value in values)


def _is_google_drive_path(path: Path) -> bool:
    return any(part.startswith("GoogleDrive-") for part in path.parts)


def _path_freshness_key(path: Path) -> tuple[float, str]:
    dated_key = _path_date_key(path)
    if dated_key:
        return (dated_key, path.as_posix())
    mtimes: list[float] = []
    for candidate in (
        path,
        path / "your_instagram_activity" / "messages",
        path / "messages",
    ):
        try:
            mtimes.append(candidate.stat().st_mtime)
        except OSError:
            continue
    return (max(mtimes) if mtimes else 0.0, path.name)


def _path_date_key(path: Path) -> float:
    month_numbers = {
        "jan": 1,
        "feb": 2,
        "mar": 3,
        "apr": 4,
        "may": 5,
        "jun": 6,
        "jul": 7,
        "aug": 8,
        "sep": 9,
        "oct": 10,
        "nov": 11,
        "dec": 12,
    }
    for name in reversed(path.parts):
        pieces = name.split("-")
        if name.startswith("instagram-") and len(pieces) >= 5:
            try:
                return float(f"{int(pieces[-4]):04d}{int(pieces[-3]):02d}{int(pieces[-2]):02d}")
            except ValueError:
                continue
        if name.startswith("meta-") and len(pieces) >= 7:
            month = month_numbers.get(pieces[2].lower())
            if month is None:
                continue
            try:
                return float(
                    f"{int(pieces[1]):04d}{month:02d}{int(pieces[3]):02d}"
                    f"{int(pieces[4]):02d}{int(pieces[5]):02d}{int(pieces[6]):02d}"
                )
            except ValueError:
                continue
    return 0.0
