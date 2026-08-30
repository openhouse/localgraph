from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .paths import Workspace


ACCOUNT_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


@dataclass(frozen=True)
class TwitterAccount:
    account_key: str
    display_name: str
    owner_kind: str
    owner_identity_key: str
    self_names: tuple[str, ...]
    source_path: Path
    sync_status_path: Path
    enabled: bool = True

    def to_public_json(self) -> dict[str, object]:
        return {
            "accountKey": self.account_key,
            "displayName": self.display_name,
            "provider": "x-twitter",
            "ownerKind": self.owner_kind,
            "ownerIdentityKey": self.owner_identity_key,
            "selfNames": list(self.self_names),
            "sourcePath": str(self.source_path),
            "syncStatusPath": str(self.sync_status_path),
            "requiredProviderExportProtocol": {
                "providerSurface": "x-settings-download-your-data",
                "information": ["account-archive"],
                "localImportInformation": ["direct-messages"],
                "dateRange": "all-available",
                "cadence": "manual",
                "delivery": "downloaded-account-archive",
                "automaticRecurrence": "not-configured",
            },
            "enabled": self.enabled,
        }


def configure_twitter_account(
    workspace: Workspace,
    *,
    account_key: str,
    display_name: str,
    owner_kind: str,
    self_names: list[str],
    enabled: bool = True,
) -> dict[str, object]:
    workspace.ensure_workspace(force=False)
    key = normalize_account_key(account_key)
    if owner_kind not in {"person", "organization"}:
        raise ValueError("--owner-kind must be person or organization")
    name = display_name.strip()
    if not name:
        raise ValueError("--display-name must not be empty")
    config = _load_config(workspace)
    imports = config.setdefault("imports", {})
    if not isinstance(imports, dict):
        raise ValueError("imports configuration must be an object")
    twitter = imports.setdefault("twitter", {})
    if not isinstance(twitter, dict):
        raise ValueError("imports.twitter configuration must be an object")
    records = twitter.setdefault("accounts", {})
    if not isinstance(records, dict):
        raise ValueError("imports.twitter.accounts configuration must be an object")
    primary_key = str(twitter.get("primaryAccountKey") or "") or None
    is_primary = owner_kind == "person" and (primary_key is None or primary_key == key)
    record = {
        "accountKey": key,
        "displayName": name,
        "ownerKind": owner_kind,
        "ownerIdentityKey": "person:self" if is_primary else f"{owner_kind}:twitter:{key}",
        "selfNames": _unique_names([key, name, *self_names]),
        "sourcePath": f"sources/twitter-accounts/{key}/incoming",
        "syncStatusPath": f"state/twitter-accounts/{key}/sync-status.json",
        "enabled": enabled,
    }
    records[key] = record
    if is_primary:
        twitter["primaryAccountKey"] = key
    _write_config(workspace, config)
    account = twitter_account(workspace, key)
    account.source_path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return {
        "workspace": str(workspace.root),
        "primaryAccountKey": twitter.get("primaryAccountKey"),
        "account": account.to_public_json(),
        "config": str(workspace.config_path),
    }


def twitter_account(workspace: Workspace, account_key: str) -> TwitterAccount:
    key = normalize_account_key(account_key)
    matches = [account for account in twitter_accounts(workspace, enabled_only=False) if account.account_key == key]
    if not matches:
        raise ValueError(f"Twitter account is not configured: {key}")
    return matches[0]


def twitter_accounts(workspace: Workspace, *, enabled_only: bool = True) -> list[TwitterAccount]:
    config = _load_config(workspace)
    imports = config.get("imports") if isinstance(config, dict) else None
    twitter = imports.get("twitter") if isinstance(imports, dict) else None
    records = twitter.get("accounts") if isinstance(twitter, dict) else None
    if not isinstance(records, dict):
        return []
    result: list[TwitterAccount] = []
    for raw_key, raw_record in sorted(records.items()):
        if not isinstance(raw_record, dict):
            continue
        key = normalize_account_key(str(raw_record.get("accountKey") or raw_key))
        enabled = bool(raw_record.get("enabled", True))
        if enabled_only and not enabled:
            continue
        result.append(
            TwitterAccount(
                account_key=key,
                display_name=str(raw_record.get("displayName") or key),
                owner_kind=str(raw_record.get("ownerKind") or "organization"),
                owner_identity_key=str(raw_record.get("ownerIdentityKey") or f"organization:twitter:{key}"),
                self_names=tuple(str(name) for name in raw_record.get("selfNames", []) if str(name).strip()),
                source_path=_resolve(workspace, raw_record.get("sourcePath"), f"sources/twitter-accounts/{key}/incoming"),
                sync_status_path=_resolve(workspace, raw_record.get("syncStatusPath"), f"state/twitter-accounts/{key}/sync-status.json"),
                enabled=enabled,
            )
        )
    return result


def twitter_accounts_status(workspace: Workspace) -> dict[str, object]:
    accounts = twitter_accounts(workspace, enabled_only=False)
    payload = []
    for account in accounts:
        sync = _load_json(account.sync_status_path)
        payload.append(
            {
                "account": account.to_public_json(),
                "sync": sync
                or {
                    "accountKey": account.account_key,
                    "status": "export-required",
                    "completedExports": 0,
                    "messageFiles": 0,
                    "historyCoverage": "archive-required",
                },
            }
        )
    return {
        "workspace": str(workspace.root),
        "accounts": payload,
        "accountsConfigured": len(accounts),
    }


def normalize_account_key(value: str) -> str:
    key = value.strip().lstrip("@").lower()
    if not ACCOUNT_KEY_PATTERN.fullmatch(key):
        raise ValueError("Twitter account key must contain only lowercase letters, numbers, dots, underscores, or hyphens")
    return key


def _resolve(workspace: Workspace, value: object, default: str) -> Path:
    path = Path(str(value or default)).expanduser()
    return path if path.is_absolute() else workspace.root / path


def _unique_names(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = value.strip().lstrip("@")
        if clean and clean.casefold() not in seen:
            seen.add(clean.casefold())
            result.append(clean)
    return result


def _load_config(workspace: Workspace) -> dict[str, Any]:
    if not workspace.config_path.is_file():
        return {}
    payload = json.loads(workspace.config_path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_config(workspace: Workspace, config: dict[str, Any]) -> None:
    temporary = workspace.config_path.with_name(f".{workspace.config_path.name}.{os.getpid()}.tmp")
    temporary.write_text(f"{json.dumps(config, indent=2, sort_keys=True)}\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, workspace.config_path)
