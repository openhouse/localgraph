from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .instagram_accounts import instagram_accounts
from .paths import Workspace


ACCOUNT_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def required_provider_export_protocol(
    account_type: str,
    export_name_prefix: str,
    export_capability_status: str | None = None,
) -> dict[str, object]:
    verified = account_type == "profile" or export_capability_status == "verified-supported"
    unsupported = export_capability_status == "verified-unsupported"
    if verified:
        support = "verified-in-accounts-center" if account_type == "profile" else "verified-for-this-page"
    elif unsupported:
        support = "verified-unsupported-for-this-page"
    else:
        support = "provider-verification-required"
    return {
        "providerSurface": "meta-accounts-center" if account_type == "profile" else "facebook-page-settings",
        "destination": "google-drive" if verified else "provider-destination-verification-required",
        "information": ["messages"],
        "baseline": {"cadence": "once", "dateRange": "all-time", "support": support},
        "recurring": {
            "cadence": "daily",
            "dateRange": "all-time",
            "durationYears": 3,
            "support": support,
        },
        "exportNamePrefix": export_name_prefix,
    }


@dataclass(frozen=True)
class FacebookAccount:
    account_key: str
    display_name: str
    account_type: str
    provider_state: str
    export_name_prefix: str
    owner_kind: str
    owner_identity_key: str
    self_names: tuple[str, ...]
    google_drive_folder_id: str | None
    google_drive_local_path: Path | None
    google_drive_cache_path: Path
    google_drive_token_path: Path
    completed_exports_registry_path: Path
    pull_manifest_path: Path
    current_mirror_path: Path
    completed_mirror_path: Path
    source_path: Path
    sync_status_path: Path
    baseline_export_name: str | None
    export_capability_status: str
    export_capability_provider_surface: str | None
    export_capability_verified_at: str | None
    enabled: bool = True

    @property
    def sync_eligible(self) -> bool:
        if not self.enabled or self.provider_state != "active":
            return False
        if self.account_type == "page":
            return self.export_capability_status == "verified-supported"
        return self.export_capability_status != "verified-unsupported"

    def to_public_json(self) -> dict[str, object]:
        return {
            "accountKey": self.account_key,
            "displayName": self.display_name,
            "accountType": self.account_type,
            "providerState": self.provider_state,
            "exportNamePrefix": self.export_name_prefix,
            "ownerKind": self.owner_kind,
            "ownerIdentityKey": self.owner_identity_key,
            "selfNames": list(self.self_names),
            "googleDriveConfigured": bool(self.google_drive_folder_id),
            "googleDriveCachePath": str(self.google_drive_cache_path),
            "currentMirrorPath": str(self.current_mirror_path),
            "sourcePath": str(self.source_path),
            "syncStatusPath": str(self.sync_status_path),
            "baselineExportName": self.baseline_export_name,
            "exportCapability": {
                "status": self.export_capability_status,
                "providerSurface": self.export_capability_provider_surface,
                "verifiedAt": self.export_capability_verified_at,
            },
            "syncEligible": self.sync_eligible,
            "requiredProviderExportProtocol": required_provider_export_protocol(
                self.account_type,
                self.export_name_prefix,
                self.export_capability_status,
            ),
            "enabled": self.enabled,
        }


def configure_facebook_account(
    workspace: Workspace,
    *,
    account_key: str,
    display_name: str,
    account_type: str,
    provider_state: str,
    self_names: list[str],
    export_name_prefix: str | None = None,
    reuse_instagram_drive: bool = False,
    enabled: bool = True,
) -> dict[str, object]:
    workspace.ensure_workspace(force=False)
    key = normalize_account_key(account_key)
    if account_type not in {"profile", "page"}:
        raise ValueError("--account-type must be profile or page")
    if provider_state not in {"active", "deactivated", "unknown"}:
        raise ValueError("--provider-state must be active, deactivated, or unknown")
    clean_display_name = display_name.strip()
    if not clean_display_name:
        raise ValueError("--display-name must not be empty")

    config = load_config(workspace)
    imports = config.setdefault("imports", {})
    if not isinstance(imports, dict):
        raise ValueError("imports configuration must be an object")
    facebook = imports.setdefault("facebook", {})
    if not isinstance(facebook, dict):
        raise ValueError("imports.facebook configuration must be an object")
    records = facebook.setdefault("accounts", {})
    if not isinstance(records, dict):
        raise ValueError("imports.facebook.accounts configuration must be an object")
    exclusions = facebook.setdefault("excludedAccounts", {})
    if not isinstance(exclusions, dict):
        raise ValueError("imports.facebook.excludedAccounts configuration must be an object")
    if key in exclusions:
        raise ValueError(f"Facebook account is protected by a privacy exclusion: {key}")

    primary_key = str(facebook.get("primaryProfileAccountKey") or "") or None
    is_primary_profile = account_type == "profile" and (primary_key is None or primary_key == key)
    defaults = _account_path_defaults(key)
    record = {
        **(records.get(key) if isinstance(records.get(key), dict) else {}),
        "accountKey": key,
        "displayName": clean_display_name,
        "accountType": account_type,
        "providerState": provider_state,
        "exportNamePrefix": export_name_prefix or f"facebook-{key}-",
        "ownerKind": "person" if account_type == "profile" else "organization",
        "ownerIdentityKey": "person:self" if is_primary_profile else f"organization:facebook:{key}",
        "selfNames": _unique_names([clean_display_name, *self_names]),
        "enabled": enabled,
        **defaults,
    }
    if "exportCapability" not in record:
        record["exportCapability"] = {
            "status": "verified-supported" if account_type == "profile" else "unverified",
            "providerSurface": "meta-accounts-center" if account_type == "profile" else "facebook-page-settings",
            "verifiedAt": None,
        }
    if account_type == "profile" and not is_primary_profile:
        record["ownerIdentityKey"] = f"person:facebook:{key}"
    if reuse_instagram_drive:
        instagram = instagram_accounts(workspace)
        if not instagram:
            raise ValueError("cannot reuse Instagram Drive settings before an Instagram account is configured")
        drive = instagram[0]
        if drive.google_drive_folder_id:
            record["googleDriveFolderId"] = drive.google_drive_folder_id
        if drive.google_drive_local_path is not None:
            record["googleDriveLocalPath"] = str(drive.google_drive_local_path)
        record["googleDriveTokenPath"] = str(drive.google_drive_token_path)
    records[key] = record
    if is_primary_profile:
        facebook["primaryProfileAccountKey"] = key
    _write_config(workspace, config)
    account = facebook_account(workspace, key)
    return {
        "workspace": str(workspace.root),
        "primaryProfileAccountKey": facebook.get("primaryProfileAccountKey"),
        "account": account.to_public_json(),
        "config": str(workspace.config_path),
    }


def verify_facebook_export_capability(
    workspace: Workspace,
    *,
    account_key: str,
    capability: str,
    provider_surface: str,
    observed_at: str,
) -> dict[str, object]:
    """Record Page-specific provider capability evidence before synchronization is allowed."""
    workspace.ensure_workspace(force=False)
    key = normalize_account_key(account_key)
    if capability not in {"supported", "unsupported"}:
        raise ValueError("Facebook export capability must be supported or unsupported")
    surface = provider_surface.strip()
    if not surface:
        raise ValueError("Facebook export capability requires a provider surface")
    _validate_observed_at(observed_at)
    config = load_config(workspace)
    facebook = config.get("imports", {}).get("facebook", {})
    records = facebook.get("accounts") if isinstance(facebook, dict) else None
    record = records.get(key) if isinstance(records, dict) else None
    if not isinstance(record, dict):
        raise ValueError(f"Facebook account is not configured: {key}")
    if str(record.get("accountType") or "page") != "page":
        raise ValueError("individual export capability verification is only required for Facebook Pages")
    export_capability = {
        "status": f"verified-{capability}",
        "providerSurface": surface,
        "verifiedAt": observed_at.strip(),
    }
    record["exportCapability"] = export_capability
    _write_config(workspace, config)
    return {
        "workspace": str(workspace.root),
        "accountKey": key,
        "exportCapability": export_capability,
        "syncEligible": capability == "supported" and str(record.get("providerState") or "unknown") == "active",
        "config": str(workspace.config_path),
    }


def exclude_facebook_account(
    workspace: Workspace,
    *,
    account_key: str,
    reason: str = "privacy-exclusion",
) -> dict[str, object]:
    """Remove an account from active synchronization and prevent re-enrollment."""
    workspace.ensure_workspace(force=False)
    key = normalize_account_key(account_key)
    config = load_config(workspace)
    imports = config.setdefault("imports", {})
    if not isinstance(imports, dict):
        raise ValueError("imports configuration must be an object")
    facebook = imports.setdefault("facebook", {})
    if not isinstance(facebook, dict):
        raise ValueError("imports.facebook configuration must be an object")
    records = facebook.setdefault("accounts", {})
    exclusions = facebook.setdefault("excludedAccounts", {})
    if not isinstance(records, dict) or not isinstance(exclusions, dict):
        raise ValueError("Facebook account configuration must be an object")
    records.pop(key, None)
    exclusions[key] = {
        "reason": reason.strip() or "privacy-exclusion",
        "excludedAt": _now_iso(),
    }
    if facebook.get("primaryProfileAccountKey") == key:
        facebook.pop("primaryProfileAccountKey", None)
    _write_config(workspace, config)
    status_path = _resolve_path(workspace, str(_account_path_defaults(key)["syncStatusPath"]))
    status_path.unlink(missing_ok=True)
    _scrub_aggregate_status(workspace, key, active_accounts=len(records))
    return {
        "workspace": str(workspace.root),
        "excluded": True,
        "activeAccounts": len(records),
        "excludedAccounts": len(exclusions),
        "config": str(workspace.config_path),
    }


def facebook_accounts(workspace: Workspace, *, enabled_only: bool = True) -> list[FacebookAccount]:
    config = load_config(workspace)
    imports = config.get("imports")
    facebook = imports.get("facebook") if isinstance(imports, dict) else None
    records = facebook.get("accounts") if isinstance(facebook, dict) else None
    if not isinstance(records, dict):
        return []
    result: list[FacebookAccount] = []
    for key in sorted(records):
        try:
            account = _account_from_record(workspace, key, records[key])
        except (TypeError, ValueError):
            continue
        if not enabled_only or account.enabled:
            result.append(account)
    return result


def facebook_account(workspace: Workspace, account_key: str) -> FacebookAccount:
    key = normalize_account_key(account_key)
    for account in facebook_accounts(workspace, enabled_only=False):
        if account.account_key == key:
            return account
    raise ValueError(f"Facebook account is not configured: {key}")


def facebook_accounts_status(workspace: Workspace) -> dict[str, object]:
    accounts = facebook_accounts(workspace, enabled_only=False)
    config = load_config(workspace)
    imports = config.get("imports")
    facebook = imports.get("facebook") if isinstance(imports, dict) else None
    primary_key = facebook.get("primaryProfileAccountKey") if isinstance(facebook, dict) else None
    exclusions = facebook.get("excludedAccounts") if isinstance(facebook, dict) else None
    items: list[dict[str, object]] = []
    for account in accounts:
        status: dict[str, object] = {"status": "not-checked"}
        if account.sync_status_path.exists():
            try:
                loaded = json.loads(account.sync_status_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                loaded = {}
            if isinstance(loaded, dict) and loaded:
                status = loaded
        items.append({"account": account.to_public_json(), "sync": status})
    aggregate_path = workspace.state_dir / "facebook-accounts-sync-status.json"
    aggregate: dict[str, object] = {"status": "not-checked"}
    if aggregate_path.exists():
        try:
            loaded_aggregate = json.loads(aggregate_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded_aggregate = {}
        if isinstance(loaded_aggregate, dict) and loaded_aggregate:
            aggregate = loaded_aggregate
    return {
        "workspace": str(workspace.root),
        "primaryProfileAccountKey": primary_key,
        "excludedAccounts": len(exclusions) if isinstance(exclusions, dict) else 0,
        "accounts": items,
        "aggregate": aggregate,
    }


def load_config(workspace: Workspace) -> dict[str, Any]:
    if not workspace.config_path.exists():
        return {}
    payload = json.loads(workspace.config_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("localgraph configuration must be a JSON object")
    return payload


def normalize_account_key(value: str) -> str:
    key = value.strip().lstrip("@").lower()
    if not ACCOUNT_KEY_PATTERN.fullmatch(key):
        raise ValueError("Facebook account key must contain only letters, digits, dot, underscore, or hyphen")
    return key


def _account_path_defaults(key: str) -> dict[str, object]:
    base = f"facebook-accounts/{key}"
    return {
        "sourcePath": f"sources/{base}/incoming",
        "googleDriveCachePath": f"sources/{base}/drive-cache",
        "completedExportsRegistryPath": f"state/{base}/completed-exports.json",
        "pullManifestPath": f"state/{base}/pull-manifest.json",
        "currentMirrorPath": f"sources/{base}/current",
        "completedMirrorPath": f"sources/{base}/completed-exports",
        "syncStatusPath": f"state/{base}/sync-status.json",
    }


def _account_from_record(workspace: Workspace, key: str, value: object) -> FacebookAccount:
    if not isinstance(value, dict):
        raise TypeError("Facebook account configuration must be an object")
    defaults = _account_path_defaults(key)
    account_type = str(value.get("accountType") or "page")
    if account_type not in {"profile", "page"}:
        raise ValueError("Facebook account type must be profile or page")
    self_names = value.get("selfNames")
    if not isinstance(self_names, list):
        self_names = []
    local_path = value.get("googleDriveLocalPath")
    capability = value.get("exportCapability")
    if not isinstance(capability, dict):
        capability = {}
    capability_status = str(
        capability.get("status")
        or ("verified-supported" if account_type == "profile" else "unverified")
    )
    if capability_status not in {"unverified", "verified-supported", "verified-unsupported"}:
        capability_status = "unverified"
    return FacebookAccount(
        account_key=normalize_account_key(str(value.get("accountKey") or key)),
        display_name=str(value.get("displayName") or key),
        account_type=account_type,
        provider_state=str(value.get("providerState") or "unknown"),
        export_name_prefix=str(value.get("exportNamePrefix") or f"facebook-{key}-"),
        owner_kind="person" if account_type == "profile" else "organization",
        owner_identity_key=str(
            value.get("ownerIdentityKey")
            or (f"person:facebook:{key}" if account_type == "profile" else f"organization:facebook:{key}")
        ),
        self_names=tuple(_unique_names([str(name) for name in self_names])),
        google_drive_folder_id=str(value["googleDriveFolderId"]) if value.get("googleDriveFolderId") else None,
        google_drive_local_path=_resolve_path(workspace, str(local_path)) if local_path else None,
        google_drive_cache_path=_resolve_path(workspace, str(value.get("googleDriveCachePath") or defaults["googleDriveCachePath"])),
        google_drive_token_path=_resolve_path(workspace, str(value.get("googleDriveTokenPath") or "state/google-drive-token.json")),
        completed_exports_registry_path=_resolve_path(workspace, str(value.get("completedExportsRegistryPath") or defaults["completedExportsRegistryPath"])),
        pull_manifest_path=_resolve_path(workspace, str(value.get("pullManifestPath") or defaults["pullManifestPath"])),
        current_mirror_path=_resolve_path(workspace, str(value.get("currentMirrorPath") or defaults["currentMirrorPath"])),
        completed_mirror_path=_resolve_path(workspace, str(value.get("completedMirrorPath") or defaults["completedMirrorPath"])),
        source_path=_resolve_path(workspace, str(value.get("sourcePath") or defaults["sourcePath"])),
        sync_status_path=_resolve_path(workspace, str(value.get("syncStatusPath") or defaults["syncStatusPath"])),
        baseline_export_name=str(value["baselineExportName"]) if value.get("baselineExportName") else None,
        export_capability_status=capability_status,
        export_capability_provider_surface=(
            str(capability["providerSurface"])
            if capability.get("providerSurface")
            else ("meta-accounts-center" if account_type == "profile" else "facebook-page-settings")
        ),
        export_capability_verified_at=(
            str(capability["verifiedAt"]) if capability.get("verifiedAt") else None
        ),
        enabled=bool(value.get("enabled", True)),
    )


def _resolve_path(workspace: Workspace, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else workspace.root / path


def _unique_names(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        name = value.strip()
        folded = name.casefold()
        if name and folded not in seen:
            seen.add(folded)
            result.append(name)
    return result


def _write_config(workspace: Workspace, config: dict[str, object]) -> None:
    temporary = workspace.config_path.with_name(f".{workspace.config_path.name}.{os.getpid()}.tmp")
    temporary.write_text(f"{json.dumps(config, indent=2, sort_keys=True)}\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, workspace.config_path)


def _scrub_aggregate_status(workspace: Workspace, key: str, *, active_accounts: int) -> None:
    path = workspace.state_dir / "facebook-accounts-sync-status.json"
    payload: dict[str, object] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded = {}
        if isinstance(loaded, dict):
            payload = loaded
    accounts = payload.get("accounts")
    if not isinstance(accounts, dict):
        accounts = {}
    accounts.pop(key, None)
    statuses = [str(value.get("status")) for value in accounts.values() if isinstance(value, dict)]
    if not statuses:
        aggregate_status = "not-configured"
    elif "pending" in statuses:
        aggregate_status = "pending"
    elif "degraded" in statuses:
        aggregate_status = "degraded"
    else:
        aggregate_status = "current"
    payload.update(
        {
            "schemaVersion": int(payload.get("schemaVersion") or 1),
            "status": aggregate_status,
            "accountsConfigured": active_accounts,
            "accountsReady": sum(status != "pending" for status in statuses),
            "accounts": accounts,
        }
    )
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(f"{json.dumps(payload, indent=2, sort_keys=True)}\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_observed_at(value: str) -> None:
    from datetime import datetime

    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Facebook export capability observation time must be ISO 8601") from exc
    if parsed.tzinfo is None:
        raise ValueError("Facebook export capability observation time must include a timezone")
