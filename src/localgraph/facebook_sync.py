from __future__ import annotations

import json
import os
import plistlib
import shlex
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from .facebook import scan_facebook_source
from .facebook_accounts import FacebookAccount, facebook_account, facebook_accounts
from .drive import DriveAPIError, pull_configured_facebook_source
from .ingest import SourceImportResult, import_facebook_source
from .paths import Workspace
from .render import render_views
from .schema import connect, initialize_schema


DEFAULT_FACEBOOK_SYNC_LABEL = "com.openhouse.localgraph.facebook-sync"
DEFAULT_FACEBOOK_SYNC_INTERVAL_MINUTES = 60


def configure_facebook_baseline(
    workspace: Workspace,
    *,
    account_key: str,
    export_name: str,
) -> dict[str, object]:
    workspace.ensure_workspace(force=False)
    account = facebook_account(workspace, account_key)
    if account.account_type == "page" and account.export_capability_status != "verified-supported":
        raise ValueError(
            f"Facebook Page export capability must be individually verified before recording a baseline: {account.account_key}"
        )
    matches = [source for source in _account_sources(account) if source.name == export_name]
    if not matches:
        raise ValueError(f"completed Facebook export is not available locally for {account.account_key}: {export_name}")
    if len(matches) > 1:
        raise ValueError(f"Facebook baseline export name is ambiguous for {account.account_key}: {export_name}")
    config = json.loads(workspace.config_path.read_text(encoding="utf-8"))
    record = config["imports"]["facebook"]["accounts"][account.account_key]
    recorded_at = _now_iso()
    record["baselineExportName"] = export_name
    record["baselineRecordedAt"] = recorded_at
    temporary = workspace.config_path.with_name(f".{workspace.config_path.name}.{os.getpid()}.tmp")
    temporary.write_text(f"{json.dumps(config, indent=2, sort_keys=True)}\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, workspace.config_path)
    status = _load_json(account.sync_status_path)
    if status:
        status.update(
            {
                "baselineExportName": export_name,
                "baselineRecordedAt": recorded_at,
                "historyCoverage": "complete-through-latest-export",
            }
        )
        _write_json_private(account.sync_status_path, status)
    return {
        "accountKey": account.account_key,
        "baselineExportName": export_name,
        "baselineExportPath": str(matches[0]),
        "historyCoverage": "complete-through-latest-export",
        "config": str(workspace.config_path),
    }


def run_facebook_sync(workspace: Workspace, *, render: bool = True) -> dict[str, object]:
    """Import every locally materialized Facebook account independently."""
    workspace.ensure_workspace(force=False)
    accounts = facebook_accounts(workspace, enabled_only=False)
    checked_at = _now_iso()
    source_results: list[SourceImportResult] = []
    account_payload: dict[str, object] = {}

    with connect(workspace.database_path) as db:
        initialize_schema(db)
        for account in accounts:
            drive_pull: dict[str, object] | None = None
            drive_error: str | None = None
            if account.sync_eligible and account.google_drive_folder_id:
                try:
                    pulled = pull_configured_facebook_source(workspace, account_key=account.account_key)
                    if pulled is not None:
                        drive_pull = pulled.to_json()
                except DriveAPIError as exc:
                    drive_error = str(exc)
            sources = _account_sources(account)
            if not account.sync_eligible:
                result = SourceImportResult("facebook", str(account.source_path), status="held")
                result.warnings.append(
                    "Facebook account is not eligible for synchronization until its own export capability is verified"
                )
                source_results.append(result)
                status = _account_status(
                    account,
                    sources,
                    checked_at=checked_at,
                    status=(
                        "verification-required"
                        if account.account_type == "page" and account.export_capability_status == "unverified"
                        else "not-eligible"
                    ),
                    drive_error=None,
                )
            elif sources:
                _clear_facebook_account_projection(db, account.account_key)
                imported_for_account: list[SourceImportResult] = []
                try:
                    for source in sources:
                        imported = import_facebook_source(
                            db,
                            source,
                            account_key=account.account_key,
                            owner_display_name=account.display_name,
                            owner_kind=account.owner_kind,
                            owner_identity_key=account.owner_identity_key,
                            self_names=list(account.self_names),
                            explicit=True,
                            commit=False,
                        )
                        if imported.status != "imported":
                            raise ValueError(
                                f"refusing to replace Facebook account projection with an empty packet: {account.account_key}"
                            )
                        imported_for_account.append(imported)
                    db.commit()
                except Exception:
                    db.rollback()
                    raise
                source_results.extend(imported_for_account)
                status = _account_status(
                    account,
                    sources,
                    checked_at=checked_at,
                    status="degraded" if drive_error else "local-current",
                    drive_error=drive_error,
                )
            else:
                result = SourceImportResult("facebook", str(account.source_path), status="pending")
                result.warnings.append("no materialized Facebook message export is available for this account")
                source_results.append(result)
                status = _account_status(
                    account,
                    [],
                    checked_at=checked_at,
                    status="pending",
                    drive_error=drive_error,
                )
            _write_json_private(account.sync_status_path, status)
            account_payload[account.account_key] = {
                "account": account.to_public_json(),
                "sync": status,
                "googleDrivePull": drive_pull
                or {"status": "error" if drive_error else "not-configured", "error": drive_error},
            }

        rendered: dict[str, int] | None = None
        if render:
            rendered = render_views(db, workspace)

    statuses = [str(item["sync"]["status"]) for item in account_payload.values()]  # type: ignore[index]
    eligible = [account for account in accounts if account.sync_eligible]
    if not statuses:
        aggregate_status = "not-configured"
    elif "degraded" in statuses:
        aggregate_status = "degraded"
    elif "pending" in statuses:
        aggregate_status = "pending"
    elif any(status == "verification-required" for status in statuses):
        aggregate_status = "verification-required"
    else:
        aggregate_status = "current"
    aggregate = {
        "schemaVersion": 1,
        "status": aggregate_status,
        "checkedAt": checked_at,
        "accountsConfigured": len(accounts),
        "accountsEligible": len(eligible),
        "accountsReady": sum(
            str(account_payload[account.account_key]["sync"]["status"]) in {"local-current", "current", "degraded"}  # type: ignore[index]
            for account in eligible
        ),
        "accounts": {
            key: {
                "status": value["sync"]["status"],  # type: ignore[index]
                "historyCoverage": value["sync"]["historyCoverage"],  # type: ignore[index]
                "messageFiles": value["sync"]["messageFiles"],  # type: ignore[index]
            }
            for key, value in account_payload.items()
        },
    }
    _write_json_private(workspace.state_dir / "facebook-accounts-sync-status.json", aggregate)
    result = _combined_result(source_results)
    if rendered is not None:
        result["render"] = rendered
    return {
        "startedAt": checked_at,
        "workspace": str(workspace.root),
        "facebookAccounts": account_payload,
        "facebookSync": aggregate,
        "result": result,
    }


def install_facebook_sync(
    workspace: Workspace,
    *,
    interval_minutes: int = DEFAULT_FACEBOOK_SYNC_INTERVAL_MINUTES,
    label: str = DEFAULT_FACEBOOK_SYNC_LABEL,
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
    script_path = support_dir / "bin" / "localgraph-facebook-sync.sh"
    log_dir = support_dir / "logs"
    plist_path = home_dir / "Library" / "LaunchAgents" / f"{label}.plist"
    script = facebook_sync_script(
        workspace,
        runtime_dir=runtime_dir,
        log_path=log_dir / "facebook-sync.log",
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


def facebook_sync_script(workspace: Workspace, *, runtime_dir: Path, log_path: Path) -> str:
    args = [
        sys.executable,
        "-m",
        "localgraph",
        "--root",
        str(workspace.root),
        "facebook-sync",
    ]
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


def _account_sources(account: FacebookAccount) -> list[Path]:
    candidates = [account.current_mirror_path, account.source_path, account.google_drive_cache_path]
    if account.google_drive_local_path is not None:
        candidates.append(account.google_drive_local_path)
    sources: list[Path] = []
    for candidate in candidates:
        scan = scan_facebook_source(candidate)
        for item in scan["exports"]:  # type: ignore[union-attr]
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            path = item.get("path")
            if not isinstance(name, str) or not isinstance(path, str):
                continue
            if not name.startswith(account.export_name_prefix):
                continue
            resolved = Path(path).resolve()
            if resolved not in sources:
                sources.append(resolved)
    return sorted(sources, key=lambda path: (path.name, path.as_posix()))


def _clear_facebook_account_projection(db: sqlite3.Connection, account_key: str) -> None:
    db.execute(
        "DELETE FROM source_imports WHERE source_kind = 'facebook' AND source_identifier LIKE ?",
        (f"facebook:{account_key}:%",),
    )
    db.execute(
        "DELETE FROM threads WHERE source_kind = 'facebook' AND source_thread_key LIKE ?",
        (f"{account_key}:%",),
    )
    db.execute("DELETE FROM graph_edges WHERE source = 'facebook-import' AND from_key LIKE ?", (f"%{account_key}:%",))


def _account_status(
    account: FacebookAccount,
    sources: list[Path],
    *,
    checked_at: str,
    status: str,
    drive_error: str | None = None,
) -> dict[str, object]:
    exports: list[str] = []
    message_files = 0
    for source in sources:
        scan = scan_facebook_source(source)
        message_files += int(scan["totalMessageFiles"])
        exports.extend(
            str(item["name"])
            for item in scan["exports"]  # type: ignore[union-attr]
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        )
    previous = _load_json(account.sync_status_path)
    baseline_present = account.baseline_export_name in exports if account.baseline_export_name else False
    return {
        "schemaVersion": 1,
        "accountKey": account.account_key,
        "accountType": account.account_type,
        "providerState": account.provider_state,
        "exportCapability": {
            "status": account.export_capability_status,
            "providerSurface": account.export_capability_provider_surface,
            "verifiedAt": account.export_capability_verified_at,
        },
        "syncEligible": account.sync_eligible,
        "status": status,
        "checkedAt": checked_at,
        "lastSuccessfulSyncAt": checked_at if status in {"local-current", "degraded"} else previous.get("lastSuccessfulSyncAt"),
        "messageFiles": message_files,
        "completedExports": len(set(exports)),
        "historyCoverage": "complete-through-latest-export" if baseline_present else "baseline-required",
        "baselineExportName": account.baseline_export_name,
        "localSourcePath": str(account.source_path),
        "lastError": drive_error,
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
        "StandardOutPath": str(log_dir / "facebook-sync.stdout.log"),
        "StandardErrorPath": str(log_dir / "facebook-sync.stderr.log"),
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
