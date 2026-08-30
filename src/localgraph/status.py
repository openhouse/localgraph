from __future__ import annotations

import json
import os
import plistlib
import re
import sqlite3
import subprocess
from collections.abc import Callable
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .facebook_accounts import FacebookAccount, facebook_accounts
from .instagram_accounts import InstagramAccount, instagram_accounts
from .paths import Workspace
from .twitter_accounts import TwitterAccount, twitter_accounts
from .whatsapp import source_status as whatsapp_source_status


LAUNCHAGENT_LABELS = {
    "instagram": "com.openhouse.localgraph.instagram-sync",
    "facebook": "com.openhouse.localgraph.facebook-sync",
    "twitter": "com.openhouse.localgraph.twitter-sync",
    "imessage": "com.openhouse.localgraph.imessage-sync",
    "whatsapp": "com.openhouse.localgraph.whatsapp-sync",
}
LIFECYCLE_STAGES = (
    "configured",
    "requested",
    "preparing",
    "delivered",
    "imported",
    "rendered",
    "current",
    "complete",
)
PROVIDER_RECORDED_STAGES = {"requested", "preparing"}
EVIDENCE_KINDS = {"provider-activity-record", "operator-observed-provider-ui"}
DEFAULT_INTERVAL_MINUTES = 60


LaunchctlReader = Callable[[str], tuple[int, str]]


def build_localgraph_status(
    workspace: Workspace,
    *,
    now: datetime | None = None,
    home: Path | None = None,
    launchctl: LaunchctlReader | None = None,
) -> dict[str, object]:
    """Return one body-free health and acceptance report for every source and account."""
    observed_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    home_dir = (home or Path.home()).expanduser()
    launchctl_reader = launchctl or _read_launchctl
    schedulers = {
        source: inspect_launchagent(label, home=home_dir, launchctl=launchctl_reader)
        for source, label in LAUNCHAGENT_LABELS.items()
    }
    sources = {
        "instagram": _instagram_source_status(workspace, schedulers["instagram"], observed_at),
        "facebook": _facebook_source_status(workspace, schedulers["facebook"], observed_at),
        "twitter": _twitter_source_status(workspace, schedulers["twitter"], observed_at),
        "imessage": _imessage_source_status(workspace, schedulers["imessage"], observed_at),
        "whatsapp": whatsapp_source_status(workspace, schedulers["whatsapp"], observed_at,
            acquisition_scheduler=inspect_launchagent("com.openhouse.localgraph.whatsapp-acquire",
                home=home_dir, launchctl=launchctl_reader) if
                _load_json(workspace.config_path).get("imports", {}).get("whatsapp", {}).get("acquisition") else None),
    }
    finding_counts = {"error": 0, "warning": 0, "info": 0}
    for source in sources.values():
        for finding in source["findings"]:  # type: ignore[index]
            severity = str(finding.get("severity") or "info")
            finding_counts[severity] = finding_counts.get(severity, 0) + 1
        for account in source["accounts"]:  # type: ignore[index]
            for finding in account["findings"]:
                severity = str(finding.get("severity") or "info")
                finding_counts[severity] = finding_counts.get(severity, 0) + 1
    if finding_counts["error"]:
        overall = "degraded"
    elif finding_counts["warning"]:
        overall = "attention-required"
    else:
        overall = "healthy"
    return {
        "schemaVersion": 1,
        "generatedAt": _iso(observed_at),
        "workspace": str(workspace.root),
        "status": overall,
        "findingCounts": finding_counts,
        "sources": sources,
        "lifecycleStageOrder": list(LIFECYCLE_STAGES),
    }


def inspect_launchagent(
    label: str,
    *,
    home: Path,
    launchctl: LaunchctlReader,
) -> dict[str, object]:
    plist_path = home / "Library" / "LaunchAgents" / f"{label}.plist"
    configured_interval: int | None = None
    if plist_path.is_file():
        try:
            payload = plistlib.loads(plist_path.read_bytes())
        except (OSError, plistlib.InvalidFileException):
            payload = {}
        interval = payload.get("StartInterval") if isinstance(payload, dict) else None
        if isinstance(interval, int):
            configured_interval = interval
    return_code, output = launchctl(label)
    loaded = return_code == 0
    runs = _launchctl_integer(output, r"(?m)^\s*runs\s*=\s*(-?\d+)\s*$")
    last_exit = _launchctl_integer(output, r"(?m)^\s*last exit code\s*=\s*(-?\d+)\s*$")
    runtime_interval = _launchctl_integer(output, r"(?m)^\s*run interval\s*=\s*(\d+)\s+seconds\s*$")
    if not plist_path.is_file():
        status = "missing"
    elif not loaded:
        status = "unloaded"
    elif last_exit not in (None, 0):
        status = "failed"
    elif runs in (None, 0):
        status = "never-run"
    else:
        status = "healthy"
    return {
        "label": label,
        "status": status,
        "plistPath": str(plist_path),
        "plistPresent": plist_path.is_file(),
        "loaded": loaded,
        "runs": runs,
        "lastExitCode": last_exit,
        "intervalSeconds": runtime_interval or configured_interval,
    }


def record_lifecycle_stage(
    workspace: Workspace,
    *,
    source: str,
    account: str,
    stage: str,
    observed_at: str,
    evidence: str,
) -> dict[str, object]:
    source_key = source.strip().lower()
    account_key = account.strip().lstrip("@").lower()
    if source_key not in {"instagram", "facebook", "twitter"}:
        raise ValueError("provider lifecycle source must be instagram, facebook, or twitter")
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", account_key):
        raise ValueError("lifecycle account key is invalid")
    if stage not in PROVIDER_RECORDED_STAGES:
        raise ValueError("only requested and preparing provider stages may be recorded manually")
    if evidence not in EVIDENCE_KINDS:
        raise ValueError("unsupported lifecycle evidence kind")
    _parse_time(observed_at)
    path = _lifecycle_path(workspace, source_key, account_key)
    ledger = _load_json(path)
    events = ledger.get("events") if isinstance(ledger.get("events"), list) else []
    retained = [
        event
        for event in events
        if not (isinstance(event, dict) and event.get("stage") == stage)
    ]
    retained.append({"stage": stage, "observedAt": observed_at, "evidence": evidence})
    retained.sort(key=lambda event: (str(event.get("observedAt") or ""), str(event.get("stage") or "")))
    payload = {
        "schemaVersion": 1,
        "source": source_key,
        "accountKey": account_key,
        "events": retained,
    }
    _write_json_private(path, payload)
    return {
        "source": source_key,
        "accountKey": account_key,
        "stage": stage,
        "observedAt": observed_at,
        "evidence": evidence,
        "ledgerPath": str(path),
    }


def _instagram_source_status(
    workspace: Workspace,
    scheduler: dict[str, object],
    now: datetime,
) -> dict[str, object]:
    accounts = instagram_accounts(workspace, enabled_only=False)
    account_reports = [
        _instagram_account_status(workspace, account, scheduler=scheduler, now=now)
        for account in accounts
    ]
    findings = _scheduler_findings(scheduler)
    return _source_report("instagram", scheduler, account_reports, findings)


def _instagram_account_status(
    workspace: Workspace,
    account: InstagramAccount,
    *,
    scheduler: dict[str, object],
    now: datetime,
) -> dict[str, object]:
    sync = _load_json(account.sync_status_path)
    authorization = _authorization_status(
        account.google_drive_token_path,
        configured=bool(account.google_drive_folder_id),
        now=now,
    )
    legacy_primary = account.current_mirror_path == workspace.sources_dir / "instagram-current"
    import_count, thread_count = _canonical_counts(
        workspace,
        "instagram",
        account.account_key,
        allow_legacy=legacy_primary,
    )
    rendered = (workspace.views_dir / "instagram-accounts" / account.account_key / "index.md").is_file()
    if legacy_primary and not rendered:
        rendered = (workspace.views_dir / "threads" / "instagram").is_dir()
    findings = _account_findings(
        source="instagram",
        sync=sync,
        authorization=authorization,
        scheduler=scheduler,
        now=now,
        interval_minutes=_scheduler_interval_minutes(scheduler),
        capability=None,
    )
    lifecycle = _lifecycle(
        workspace,
        source="instagram",
        account_key=account.account_key,
        sync=sync,
        import_count=import_count,
        rendered=rendered,
        findings=findings,
    )
    return {
        "accountKey": account.account_key,
        "profileName": account.profile_name,
        "enabled": account.enabled,
        "health": _health(findings),
        "syncStatus": str(sync.get("status") or "not-checked"),
        "lastSuccessfulSyncAt": sync.get("lastSuccessfulSyncAt"),
        "completedExports": _integer(sync.get("completedExports")),
        "messageFiles": _integer(sync.get("messageFiles")),
        "imports": import_count,
        "threads": thread_count,
        "historyCoverage": str(sync.get("historyCoverage") or "baseline-required"),
        "authorization": authorization,
        "lifecycle": lifecycle,
        "findings": findings,
    }


def _facebook_source_status(
    workspace: Workspace,
    scheduler: dict[str, object],
    now: datetime,
) -> dict[str, object]:
    accounts = facebook_accounts(workspace, enabled_only=False)
    reports = [
        _facebook_account_status(workspace, account, scheduler=scheduler, now=now)
        for account in accounts
    ]
    findings = _scheduler_findings(scheduler)
    return _source_report("facebook", scheduler, reports, findings)


def _facebook_account_status(
    workspace: Workspace,
    account: FacebookAccount,
    *,
    scheduler: dict[str, object],
    now: datetime,
) -> dict[str, object]:
    sync = _load_json(account.sync_status_path)
    authorization = _authorization_status(
        account.google_drive_token_path,
        configured=bool(account.google_drive_folder_id),
        now=now,
    )
    import_count, thread_count = _canonical_counts(workspace, "facebook", account.account_key)
    rendered = (workspace.views_dir / "facebook-accounts" / account.account_key / "index.md").is_file()
    capability = account.export_capability_status
    findings = _account_findings(
        source="facebook",
        sync=sync,
        authorization=authorization,
        scheduler=scheduler,
        now=now,
        interval_minutes=_scheduler_interval_minutes(scheduler),
        capability=capability if account.account_type == "page" else None,
    )
    lifecycle = _lifecycle(
        workspace,
        source="facebook",
        account_key=account.account_key,
        sync=sync,
        import_count=import_count,
        rendered=rendered,
        findings=findings,
    )
    return {
        "accountKey": account.account_key,
        "displayName": account.display_name,
        "accountType": account.account_type,
        "providerState": account.provider_state,
        "enabled": account.enabled,
        "syncEligible": account.sync_eligible,
        "exportCapability": {
            "status": account.export_capability_status,
            "providerSurface": account.export_capability_provider_surface,
            "verifiedAt": account.export_capability_verified_at,
        },
        "health": _health(findings),
        "syncStatus": str(sync.get("status") or "not-checked"),
        "lastSuccessfulSyncAt": sync.get("lastSuccessfulSyncAt"),
        "completedExports": _integer(sync.get("completedExports")),
        "messageFiles": _integer(sync.get("messageFiles")),
        "imports": import_count,
        "threads": thread_count,
        "historyCoverage": str(sync.get("historyCoverage") or "baseline-required"),
        "authorization": authorization,
        "lifecycle": lifecycle,
        "findings": findings,
    }


def _imessage_source_status(
    workspace: Workspace,
    scheduler: dict[str, object],
    now: datetime,
) -> dict[str, object]:
    sync = _load_json(workspace.imessage_sync_status_path)
    import_count, thread_count = _canonical_counts(workspace, "imessage", None)
    rendered = (workspace.views_dir / "threads" / "imessage").is_dir()
    authorization = {"status": "not-required", "accessTokenExpired": False}
    findings = _account_findings(
        source="imessage",
        sync=sync,
        authorization=authorization,
        scheduler=scheduler,
        now=now,
        interval_minutes=_scheduler_interval_minutes(
            scheduler,
            fallback=_integer(sync.get("checkIntervalMinutes")) or DEFAULT_INTERVAL_MINUTES,
        ),
        capability=None,
    )
    lifecycle = _lifecycle(
        workspace,
        source="imessage",
        account_key="local-macos-messages",
        sync=sync,
        import_count=import_count,
        rendered=rendered,
        findings=findings,
    )
    account = {
        "accountKey": "local-macos-messages",
        "enabled": True,
        "health": _health(findings),
        "syncStatus": str(sync.get("status") or "not-checked"),
        "lastSuccessfulSyncAt": sync.get("lastSuccessfulSyncAt"),
        "snapshotBytes": _integer(sync.get("snapshotBytes")),
        "messages": _integer(sync.get("messages")),
        "imports": import_count,
        "threads": thread_count,
        "historyCoverage": str(sync.get("historyCoverage") or "snapshot-required"),
        "authorization": authorization,
        "lifecycle": lifecycle,
        "findings": findings,
    }
    return _source_report("imessage", scheduler, [account], _scheduler_findings(scheduler))


def _twitter_source_status(
    workspace: Workspace,
    scheduler: dict[str, object],
    now: datetime,
) -> dict[str, object]:
    reports = [
        _twitter_account_status(workspace, account, scheduler=scheduler, now=now)
        for account in twitter_accounts(workspace, enabled_only=False)
    ]
    return _source_report("twitter", scheduler, reports, _scheduler_findings(scheduler))


def _twitter_account_status(
    workspace: Workspace,
    account: TwitterAccount,
    *,
    scheduler: dict[str, object],
    now: datetime,
) -> dict[str, object]:
    sync = _load_json(account.sync_status_path) or {
        "accountKey": account.account_key,
        "status": "export-required",
        "completedExports": 0,
        "messageFiles": 0,
        "historyCoverage": "archive-required",
    }
    authorization = {"status": "not-required", "accessTokenExpired": False}
    import_count, thread_count = _canonical_counts(workspace, "twitter", account.account_key)
    rendered = (workspace.views_dir / "twitter-accounts" / account.account_key / "index.md").is_file()
    findings = _account_findings(
        source="twitter",
        sync=sync,
        authorization=authorization,
        scheduler=scheduler,
        now=now,
        interval_minutes=_scheduler_interval_minutes(scheduler),
        capability=None,
    )
    lifecycle = _lifecycle(
        workspace,
        source="twitter",
        account_key=account.account_key,
        sync=sync,
        import_count=import_count,
        rendered=rendered,
        findings=findings,
    )
    return {
        "accountKey": account.account_key,
        "displayName": account.display_name,
        "provider": "x-twitter",
        "enabled": account.enabled,
        "health": _health(findings),
        "syncStatus": str(sync.get("status") or "export-required"),
        "lastSuccessfulSyncAt": sync.get("lastSuccessfulSyncAt"),
        "completedExports": _integer(sync.get("completedExports")),
        "messageFiles": _integer(sync.get("messageFiles")),
        "imports": import_count,
        "threads": thread_count,
        "historyCoverage": str(sync.get("historyCoverage") or "archive-required"),
        "authorization": authorization,
        "providerCadence": "manual",
        "lifecycle": lifecycle,
        "findings": findings,
    }


def _source_report(
    source: str,
    scheduler: dict[str, object],
    accounts: list[dict[str, object]],
    findings: list[dict[str, str]],
) -> dict[str, object]:
    account_health = [str(account["health"]) for account in accounts]
    combined = [*findings]
    for account in accounts:
        combined.extend(account["findings"])  # type: ignore[arg-type]
    return {
        "source": source,
        "health": _health(combined),
        "scheduler": scheduler,
        "accountsConfigured": len(accounts),
        "accounts": accounts,
        "findings": findings,
        "accountHealth": {
            "healthy": account_health.count("healthy"),
            "attention-required": account_health.count("attention-required"),
            "degraded": account_health.count("degraded"),
        },
    }


def _scheduler_findings(scheduler: dict[str, object]) -> list[dict[str, str]]:
    status = str(scheduler.get("status") or "missing")
    if status == "healthy":
        return []
    severity = "error" if status in {"failed", "missing", "unloaded"} else "warning"
    code = {
        "failed": "launchagent-failed",
        "missing": "launchagent-missing",
        "unloaded": "launchagent-unloaded",
        "never-run": "launchagent-never-run",
    }.get(status, "launchagent-unhealthy")
    return [{"code": code, "severity": severity}]


def _account_findings(
    *,
    source: str,
    sync: dict[str, object],
    authorization: dict[str, object],
    scheduler: dict[str, object],
    now: datetime,
    interval_minutes: int,
    capability: str | None,
) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    if capability == "unverified":
        findings.append({"code": "provider-export-capability-unverified", "severity": "warning"})
    elif capability == "verified-unsupported":
        findings.append({"code": "provider-export-capability-unsupported", "severity": "warning"})
    auth_status = str(authorization.get("status") or "not-required")
    if auth_status == "expired":
        findings.append({"code": "authorization-expired", "severity": "error"})
    elif auth_status in {"missing", "invalid"}:
        findings.append({"code": f"authorization-{auth_status}", "severity": "error"})
    sync_status = str(sync.get("status") or "not-checked")
    if sync_status in {"blocked", "degraded", "failed"} or sync.get("lastError"):
        findings.append({"code": "sync-failed", "severity": "error"})
    last_success = _optional_time(sync.get("lastSuccessfulSyncAt"))
    scheduler_installed = str(scheduler.get("status")) not in {"missing", "unloaded"}
    if last_success is not None and scheduler_installed:
        stale_after = timedelta(minutes=max(interval_minutes * 2, interval_minutes + 15))
        if now - last_success > stale_after:
            findings.append({"code": "stale-sync", "severity": "error"})
    completed = _integer(sync.get("completedExports"))
    if source in {"instagram", "facebook", "twitter"}:
        if completed == 0:
            findings.append({"code": "missing-export", "severity": "warning"})
        if completed > 0 and _integer(sync.get("messageFiles")) == 0:
            findings.append({"code": "unexpected-empty-snapshot", "severity": "error"})
        default_coverage = "archive-required" if source == "twitter" else "baseline-required"
        if str(sync.get("historyCoverage") or default_coverage) != "complete-through-latest-export":
            findings.append({"code": "historical-completeness-not-established", "severity": "warning"})
    elif source == "imessage":
        if sync_status == "current" and (
            _integer(sync.get("snapshotBytes")) == 0 or _integer(sync.get("messages")) == 0
        ):
            findings.append({"code": "unexpected-empty-snapshot", "severity": "error"})
        if not sync:
            findings.append({"code": "missing-snapshot", "severity": "warning"})
    return _deduplicate_findings(findings)


def _authorization_status(path: Path, *, configured: bool, now: datetime) -> dict[str, object]:
    if not configured:
        return {"status": "not-configured", "accessTokenExpired": False}
    if not path.is_file():
        return {"status": "missing", "accessTokenExpired": False}
    token = _load_json(path)
    if not token:
        return {"status": "invalid", "accessTokenExpired": False}
    expires_at = token.get("expires_at")
    expired = isinstance(expires_at, (int, float)) and float(expires_at) <= now.timestamp()
    refreshable = bool(token.get("refresh_token"))
    if expired and not refreshable:
        status = "expired"
    elif expired:
        status = "refreshable"
    else:
        status = "current"
    return {
        "status": status,
        "accessTokenExpired": expired,
        "refreshable": refreshable,
        "scope": str(token.get("scope") or "unknown"),
        "tokenPath": str(path),
    }


def _lifecycle(
    workspace: Workspace,
    *,
    source: str,
    account_key: str,
    sync: dict[str, object],
    import_count: int,
    rendered: bool,
    findings: list[dict[str, str]],
) -> dict[str, object]:
    ledger = _load_json(_lifecycle_path(workspace, source, account_key))
    events = ledger.get("events") if isinstance(ledger.get("events"), list) else []
    stages: dict[str, dict[str, object]] = {
        stage: {"status": "pending"} for stage in LIFECYCLE_STAGES
    }
    stages["configured"] = {"status": "evidenced", "evidence": "local-config"}
    for event in events:
        if not isinstance(event, dict):
            continue
        stage = str(event.get("stage") or "")
        if stage in PROVIDER_RECORDED_STAGES:
            stages[stage] = {
                "status": "evidenced",
                "observedAt": event.get("observedAt"),
                "evidence": event.get("evidence"),
            }
    completed_exports = _integer(sync.get("completedExports"))
    if import_count > 0:
        stages["imported"] = {
            "status": "evidenced",
            "evidence": "canonical-database",
            "importCount": import_count,
        }
    if rendered:
        stages["rendered"] = {"status": "evidenced", "evidence": "filesystem-view"}
    finding_codes = {finding["code"] for finding in findings}
    delivered = completed_exports > 0
    if source == "imessage":
        delivered = _integer(sync.get("snapshotBytes")) > 0
    if delivered:
        stages["delivered"] = {
            "status": "evidenced",
            "observedAt": sync.get("lastSuccessfulSyncAt") or sync.get("checkedAt"),
            "evidence": "local-completed-packet" if source != "imessage" else "local-snapshot",
            "packetCount": completed_exports if source != "imessage" else 1,
        }
    current = str(sync.get("status") or "") in {"current", "local-current"} and not {
        "stale-sync",
        "sync-failed",
        "unexpected-empty-snapshot",
    }.intersection(finding_codes) and delivered and import_count > 0 and rendered
    if current:
        stages["current"] = {
            "status": "evidenced",
            "observedAt": sync.get("lastSuccessfulSyncAt"),
            "evidence": "fresh-success-receipt",
        }
    coverage = str(sync.get("historyCoverage") or "")
    complete = coverage in {"complete-through-latest-export", "complete-through-snapshot"}
    if complete:
        stages["complete"] = {
            "status": "evidenced",
            "evidence": "account-specific-baseline" if source != "imessage" else "validated-local-snapshot",
            "baselineExportName": sync.get("baselineExportName"),
        }
    if current and complete:
        current_stage = "complete"
    elif current:
        current_stage = "current"
    else:
        current_stage = "configured"
        for stage in LIFECYCLE_STAGES[1:6]:
            if stages[stage]["status"] == "evidenced":
                current_stage = stage
    return {
        "currentStage": current_stage,
        "current": current,
        "complete": complete,
        "stages": stages,
    }


def _canonical_counts(
    workspace: Workspace,
    source: str,
    account_key: str | None,
    *,
    allow_legacy: bool = False,
) -> tuple[int, int]:
    if not workspace.database_path.is_file():
        return 0, 0
    try:
        database_uri = f"file:{quote(workspace.database_path.as_posix(), safe='/')}?mode=ro&immutable=1"
        with closing(sqlite3.connect(database_uri, uri=True)) as db:
            if account_key is None:
                imports = db.execute(
                    "SELECT COUNT(*) FROM source_imports WHERE source_kind = ?",
                    (source,),
                ).fetchone()[0]
                threads = db.execute(
                    "SELECT COUNT(*) FROM threads WHERE source_kind = ?",
                    (source,),
                ).fetchone()[0]
            else:
                imports = db.execute(
                    "SELECT COUNT(*) FROM source_imports WHERE source_kind = ? AND source_identifier GLOB ?",
                    (source, f"{source}:{account_key}:*"),
                ).fetchone()[0]
                threads = db.execute(
                    "SELECT COUNT(*) FROM threads WHERE source_kind = ? AND source_thread_key GLOB ?",
                    (source, f"{account_key}:*"),
                ).fetchone()[0]
                if allow_legacy and imports == 0 and threads == 0:
                    imports = db.execute(
                        "SELECT COUNT(*) FROM source_imports WHERE source_kind = ?",
                        (source,),
                    ).fetchone()[0]
                    threads = db.execute(
                        "SELECT COUNT(*) FROM threads WHERE source_kind = ?",
                        (source,),
                    ).fetchone()[0]
    except sqlite3.Error:
        return 0, 0
    return int(imports), int(threads)


def _read_launchctl(label: str) -> tuple[int, str]:
    try:
        result = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 127, ""
    return result.returncode, result.stdout


def _launchctl_integer(output: str, pattern: str) -> int | None:
    match = re.search(pattern, output)
    return int(match.group(1)) if match else None


def _scheduler_interval_minutes(
    scheduler: dict[str, object],
    *,
    fallback: int = DEFAULT_INTERVAL_MINUTES,
) -> int:
    seconds = _integer(scheduler.get("intervalSeconds"))
    return max(1, seconds // 60) if seconds else fallback


def _health(findings: list[dict[str, str]]) -> str:
    severities = {finding.get("severity") for finding in findings}
    if "error" in severities:
        return "degraded"
    if "warning" in severities:
        return "attention-required"
    return "healthy"


def _deduplicate_findings(findings: list[dict[str, str]]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for finding in findings:
        code = finding["code"]
        if code not in seen:
            seen.add(code)
            result.append(finding)
    return result


def _lifecycle_path(workspace: Workspace, source: str, account: str) -> Path:
    return workspace.state_dir / "lifecycle" / source / f"{account}.json"


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
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


def _integer(value: object) -> int:
    return int(value) if isinstance(value, (int, float)) else 0


def _optional_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return _parse_time(value)
    except ValueError:
        return None


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("lifecycle timestamps must include a timezone")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
