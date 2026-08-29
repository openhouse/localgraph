from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .paths import Workspace


ACCOUNT_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def required_provider_export_protocol(export_name_prefix: str) -> dict[str, object]:
    return {
        "destination": "google-drive",
        "information": ["messages"],
        "baseline": {"cadence": "once", "dateRange": "all-time"},
        "recurring": {"cadence": "daily", "durationYears": 3},
        "exportNamePrefix": export_name_prefix,
    }


@dataclass(frozen=True)
class InstagramAccount:
    account_key: str
    profile_name: str
    export_name_prefix: str
    owner_display_name: str
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
    sync_status_path: Path
    baseline_export_name: str | None
    enabled: bool = True

    def to_public_json(self) -> dict[str, object]:
        return {
            "accountKey": self.account_key,
            "profileName": self.profile_name,
            "exportNamePrefix": self.export_name_prefix,
            "ownerDisplayName": self.owner_display_name,
            "ownerKind": self.owner_kind,
            "ownerIdentityKey": self.owner_identity_key,
            "selfNames": list(self.self_names),
            "googleDriveConfigured": bool(self.google_drive_folder_id),
            "googleDriveCachePath": str(self.google_drive_cache_path),
            "completedExportsRegistryPath": str(self.completed_exports_registry_path),
            "currentMirrorPath": str(self.current_mirror_path),
            "completedMirrorPath": str(self.completed_mirror_path),
            "syncStatusPath": str(self.sync_status_path),
            "baselineExportName": self.baseline_export_name,
            "requiredProviderExportProtocol": required_provider_export_protocol(self.export_name_prefix),
            "enabled": self.enabled,
        }


def configure_instagram_account(
    workspace: Workspace,
    *,
    account_key: str,
    profile_name: str,
    owner_display_name: str,
    owner_kind: str,
    self_names: list[str],
    export_name_prefix: str | None = None,
    adopt_legacy: bool = False,
    reuse_primary_drive: bool = False,
    primary: bool = False,
) -> dict[str, object]:
    workspace.ensure_workspace(force=False)
    key = normalize_account_key(account_key)
    profile = normalize_account_key(profile_name)
    if owner_kind not in {"person", "organization"}:
        raise ValueError("--owner-kind must be person or organization")
    config = load_config(workspace)
    imports = config.setdefault("imports", {})
    if not isinstance(imports, dict):
        raise ValueError("imports configuration must be an object")
    instagram = imports.setdefault("instagram", {})
    if not isinstance(instagram, dict):
        raise ValueError("imports.instagram configuration must be an object")
    accounts = instagram.setdefault("accounts", {})
    if not isinstance(accounts, dict):
        raise ValueError("imports.instagram.accounts configuration must be an object")

    primary_key = str(instagram.get("primaryAccountKey") or "") or None
    is_primary = primary or primary_key == key or primary_key is None
    if reuse_primary_drive:
        if not primary_key or not isinstance(accounts.get(primary_key), dict):
            raise ValueError("cannot reuse primary Drive settings before a primary account is configured")
        drive_source = accounts[primary_key]
    elif adopt_legacy:
        drive_source = instagram
    else:
        drive_source = accounts.get(key) if isinstance(accounts.get(key), dict) else {}

    defaults = _account_path_defaults(key, legacy=adopt_legacy)
    record = {
        "accountKey": key,
        "profileName": profile,
        "exportNamePrefix": export_name_prefix or f"instagram-{profile}-",
        "ownerDisplayName": owner_display_name.strip(),
        "ownerKind": owner_kind,
        "ownerIdentityKey": "person:self" if is_primary and owner_kind == "person" else f"{owner_kind}:instagram:{key}",
        "selfNames": _unique_names([profile, owner_display_name, *self_names]),
        "enabled": True,
        **defaults,
    }
    if isinstance(accounts.get(key), dict):
        record = {**accounts[key], **record}
    for field in ("googleDriveFolderId", "googleDriveLocalPath", "googleDriveTokenPath"):
        value = drive_source.get(field) if isinstance(drive_source, dict) else None
        if value:
            record[field] = value
    if adopt_legacy:
        for field in (
            "googleDriveCachePath",
            "baselineExportName",
            "baselineRecordedAt",
        ):
            value = instagram.get(field)
            if value:
                record[field] = value
    accounts[key] = record
    if is_primary:
        instagram["primaryAccountKey"] = key
        if owner_kind == "person":
            record["ownerIdentityKey"] = "person:self"
    _write_config(workspace, config)
    account = instagram_account(workspace, key)
    return {
        "workspace": str(workspace.root),
        "primaryAccountKey": instagram["primaryAccountKey"],
        "account": account.to_public_json(),
        "config": str(workspace.config_path),
    }


def instagram_accounts(workspace: Workspace, *, enabled_only: bool = True) -> list[InstagramAccount]:
    config = load_config(workspace)
    imports = config.get("imports")
    instagram = imports.get("instagram") if isinstance(imports, dict) else None
    records = instagram.get("accounts") if isinstance(instagram, dict) else None
    if not isinstance(records, dict):
        return []
    result: list[InstagramAccount] = []
    for key in sorted(records):
        try:
            account = _account_from_record(workspace, key, records[key])
        except (TypeError, ValueError):
            continue
        if not enabled_only or account.enabled:
            result.append(account)
    return result


def instagram_account(workspace: Workspace, account_key: str) -> InstagramAccount:
    key = normalize_account_key(account_key)
    for account in instagram_accounts(workspace, enabled_only=False):
        if account.account_key == key:
            return account
    raise ValueError(f"Instagram account is not configured: {key}")


def primary_instagram_account(workspace: Workspace) -> InstagramAccount | None:
    config = load_config(workspace)
    imports = config.get("imports")
    instagram = imports.get("instagram") if isinstance(imports, dict) else None
    primary_key = instagram.get("primaryAccountKey") if isinstance(instagram, dict) else None
    if isinstance(primary_key, str):
        try:
            return instagram_account(workspace, primary_key)
        except ValueError:
            return None
    accounts = instagram_accounts(workspace)
    return accounts[0] if accounts else None


def instagram_accounts_status(workspace: Workspace) -> dict[str, object]:
    accounts = instagram_accounts(workspace, enabled_only=False)
    primary = primary_instagram_account(workspace)
    items: list[dict[str, object]] = []
    for account in accounts:
        status: dict[str, object] = {}
        if account.sync_status_path.exists():
            try:
                loaded = json.loads(account.sync_status_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                loaded = {}
            if isinstance(loaded, dict):
                status = loaded
        items.append({"account": account.to_public_json(), "sync": status or {"status": "not-checked"}})
    aggregate_path = workspace.state_dir / "instagram-accounts-sync-status.json"
    aggregate: dict[str, object] = {}
    if aggregate_path.exists():
        try:
            loaded_aggregate = json.loads(aggregate_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded_aggregate = {}
        if isinstance(loaded_aggregate, dict):
            aggregate = loaded_aggregate
    return {
        "workspace": str(workspace.root),
        "primaryAccountKey": primary.account_key if primary is not None else None,
        "accounts": items,
        "aggregate": aggregate or {"status": "not-checked"},
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
        raise ValueError("Instagram account key must contain only letters, digits, dot, underscore, or hyphen")
    return key


def _account_path_defaults(key: str, *, legacy: bool) -> dict[str, object]:
    if legacy:
        return {
            "googleDriveCachePath": "sources/instagram-drive-cache",
            "completedExportsRegistryPath": "state/instagram-drive-completed-exports.json",
            "pullManifestPath": "state/google-drive-pull-manifest.json",
            "currentMirrorPath": "sources/instagram-current",
            "completedMirrorPath": "sources/instagram-completed-exports",
            "syncStatusPath": "state/instagram-sync-status.json",
        }
    base = f"instagram-accounts/{key}"
    return {
        "googleDriveCachePath": f"sources/{base}/drive-cache",
        "completedExportsRegistryPath": f"state/{base}/completed-exports.json",
        "pullManifestPath": f"state/{base}/pull-manifest.json",
        "currentMirrorPath": f"sources/{base}/current",
        "completedMirrorPath": f"sources/{base}/completed-exports",
        "syncStatusPath": f"state/{base}/sync-status.json",
    }


def _account_from_record(workspace: Workspace, key: str, value: object) -> InstagramAccount:
    if not isinstance(value, dict):
        raise TypeError("Instagram account configuration must be an object")
    defaults = _account_path_defaults(key, legacy=False)
    self_names = value.get("selfNames")
    if not isinstance(self_names, list):
        self_names = []
    local_path = value.get("googleDriveLocalPath")
    return InstagramAccount(
        account_key=normalize_account_key(str(value.get("accountKey") or key)),
        profile_name=normalize_account_key(str(value.get("profileName") or key)),
        export_name_prefix=str(value.get("exportNamePrefix") or f"instagram-{key}-"),
        owner_display_name=str(value.get("ownerDisplayName") or value.get("profileName") or key),
        owner_kind=str(value.get("ownerKind") or "person"),
        owner_identity_key=str(value.get("ownerIdentityKey") or f"person:instagram:{key}"),
        self_names=tuple(_unique_names([str(name) for name in self_names])),
        google_drive_folder_id=str(value["googleDriveFolderId"]) if value.get("googleDriveFolderId") else None,
        google_drive_local_path=_resolve_path(workspace, str(local_path)) if local_path else None,
        google_drive_cache_path=_resolve_path(workspace, str(value.get("googleDriveCachePath") or defaults["googleDriveCachePath"])),
        google_drive_token_path=_resolve_path(workspace, str(value.get("googleDriveTokenPath") or "state/google-drive-token.json")),
        completed_exports_registry_path=_resolve_path(workspace, str(value.get("completedExportsRegistryPath") or defaults["completedExportsRegistryPath"])),
        pull_manifest_path=_resolve_path(workspace, str(value.get("pullManifestPath") or defaults["pullManifestPath"])),
        current_mirror_path=_resolve_path(workspace, str(value.get("currentMirrorPath") or defaults["currentMirrorPath"])),
        completed_mirror_path=_resolve_path(workspace, str(value.get("completedMirrorPath") or defaults["completedMirrorPath"])),
        sync_status_path=_resolve_path(workspace, str(value.get("syncStatusPath") or defaults["syncStatusPath"])),
        baseline_export_name=str(value["baselineExportName"]) if value.get("baselineExportName") else None,
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
        key = name.casefold()
        if name and key not in seen:
            seen.add(key)
            result.append(name)
    return result


def _write_config(workspace: Workspace, config: dict[str, object]) -> None:
    temporary = workspace.config_path.with_name(f".{workspace.config_path.name}.{os.getpid()}.tmp")
    temporary.write_text(f"{json.dumps(config, indent=2, sort_keys=True)}\n", encoding="utf-8")
    os.replace(temporary, workspace.config_path)
