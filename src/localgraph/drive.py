from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .facebook import scan_facebook_source
from .facebook_accounts import facebook_account
from .instagram import scan_instagram_source
from .instagram_accounts import instagram_account
from .paths import Workspace


DRIVE_API_BASE_URL = "https://www.googleapis.com/drive/v3"
DRIVE_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
DRIVE_SCOPE_READONLY = "https://www.googleapis.com/auth/drive.readonly"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
COMPLETED_EXPORTS_REGISTRY_NAME = "instagram-drive-completed-exports.json"
DRIVE_LIST_MAX_WORKERS = 12


@dataclass
class DrivePullResult:
    folder_id: str
    cache_path: Path
    manifest_path: Path
    files_seen: int = 0
    folders_seen: int = 0
    downloaded: int = 0
    unchanged: int = 0
    skipped: int = 0
    bytes_downloaded: int = 0
    warnings: list[str] = field(default_factory=list)
    configured_folder_id: str | None = None
    selected_export_id: str | None = None
    selected_export_name: str | None = None
    selected_export_ids: list[str] = field(default_factory=list)
    selected_export_names: list[str] = field(default_factory=list)
    completed_export_paths: list[Path] = field(default_factory=list)

    def to_json(self) -> dict[str, object]:
        result: dict[str, object] = {
            "status": "pulled",
            "folderId": self.folder_id,
            "cachePath": str(self.cache_path),
            "manifestPath": str(self.manifest_path),
            "filesSeen": self.files_seen,
            "foldersSeen": self.folders_seen,
            "downloaded": self.downloaded,
            "unchanged": self.unchanged,
            "skipped": self.skipped,
            "bytesDownloaded": self.bytes_downloaded,
            "warnings": self.warnings,
        }
        if self.configured_folder_id is not None:
            result["configuredFolderId"] = self.configured_folder_id
        if self.selected_export_id is not None:
            result["selectedExportId"] = self.selected_export_id
        if self.selected_export_name is not None:
            result["selectedExportName"] = self.selected_export_name
        if self.selected_export_ids:
            result["selectedExportIds"] = self.selected_export_ids
        if self.selected_export_names:
            result["selectedExportNames"] = self.selected_export_names
        if self.completed_export_paths:
            result["completedExportPaths"] = [str(path) for path in self.completed_export_paths]
        return result


class DriveAPIError(RuntimeError):
    """Raised when Google Drive auth or API access fails."""


def configure_google_drive_api(
    workspace: Workspace,
    *,
    folder_id: str,
    cache_dir: Path | None = None,
    token_path: Path | None = None,
) -> dict[str, object]:
    workspace.ensure_workspace(force=False)
    config = _load_config(workspace)
    instagram = config.setdefault("imports", {}).setdefault("instagram", {})  # type: ignore[union-attr]
    instagram["googleDriveFolderId"] = folder_id
    instagram["googleDriveCachePath"] = str(_workspace_relative_or_absolute(workspace, cache_dir or default_drive_cache_dir(workspace)))
    instagram["googleDriveTokenPath"] = str(_workspace_relative_or_absolute(workspace, token_path or default_drive_token_path(workspace)))
    _write_config(workspace, config)
    return {
        "workspace": str(workspace.root),
        "folderId": folder_id,
        "cachePath": str(_resolve_workspace_path(workspace, Path(str(instagram["googleDriveCachePath"])))),
        "tokenPath": str(_resolve_workspace_path(workspace, Path(str(instagram["googleDriveTokenPath"])))),
        "config": str(workspace.config_path),
    }


def authenticate_google_drive(
    workspace: Workspace,
    *,
    client_secrets_path: Path,
    token_path: Path | None = None,
    open_browser: bool = True,
    port: int = 0,
) -> dict[str, object]:
    workspace.ensure_workspace(force=False)
    client = _load_oauth_client(client_secrets_path)
    token_target = token_path or configured_drive_token_path(workspace)
    token_target.parent.mkdir(parents=True, exist_ok=True)

    verifier = _pkce_verifier()
    challenge = _pkce_challenge(verifier)
    state = secrets.token_urlsafe(24)
    captured: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            if parsed.path != "/oauth2callback":
                self.send_response(404)
                self.end_headers()
                return
            if params.get("state", [""])[0] != state:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"OAuth state mismatch.")
                return
            if "error" in params:
                captured["error"] = params["error"][0]
            if "code" in params:
                captured["code"] = params["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Localgraph Google Drive authorization received. You can close this tab.")

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    redirect_uri = f"http://127.0.0.1:{server.server_port}/oauth2callback"
    auth_url = _authorization_url(client, redirect_uri=redirect_uri, state=state, challenge=challenge)
    print(f"Open this URL to authorize Localgraph Google Drive access:\n{auth_url}", file=sys.stderr)
    if open_browser:
        webbrowser.open(auth_url)
    server.handle_request()
    server.server_close()

    if "error" in captured:
        raise DriveAPIError(f"Google OAuth returned an error: {captured['error']}")
    code = captured.get("code")
    if not code:
        raise DriveAPIError("Google OAuth did not return an authorization code")

    token = _exchange_authorization_code(client, code=code, redirect_uri=redirect_uri, verifier=verifier)
    token_payload = {
        **token,
        "client_id": client["client_id"],
        "client_secret": client.get("client_secret"),
        "token_uri": client["token_uri"],
        "scope": DRIVE_SCOPE_READONLY,
        "expires_at": time.time() + int(token.get("expires_in", 0)),
    }
    _write_json_private(token_target, token_payload)
    return {
        "status": "authorized",
        "tokenPath": str(token_target),
        "scope": DRIVE_SCOPE_READONLY,
    }


def pull_configured_google_drive_source(
    workspace: Workspace,
    *,
    account_key: str | None = None,
) -> DrivePullResult | None:
    if account_key is not None:
        account = instagram_account(workspace, account_key)
        if not account.google_drive_folder_id:
            return None
        return pull_latest_instagram_export(
            workspace,
            container_folder_id=account.google_drive_folder_id,
            cache_dir=account.google_drive_cache_path,
            token_path=account.google_drive_token_path,
            export_name_prefix=account.export_name_prefix,
            registry_path=account.completed_exports_registry_path,
            manifest_path=account.pull_manifest_path,
        )
    config = _load_config(workspace)
    instagram = config.get("imports", {}).get("instagram", {}) if isinstance(config.get("imports"), dict) else {}
    folder_id = instagram.get("googleDriveFolderId") if isinstance(instagram, dict) else None
    if not folder_id:
        return None
    return pull_latest_instagram_export(
        workspace,
        container_folder_id=str(folder_id),
        cache_dir=configured_drive_cache_dir(workspace),
        token_path=configured_drive_token_path(workspace),
    )


def pull_configured_facebook_source(
    workspace: Workspace,
    *,
    account_key: str,
) -> DrivePullResult | None:
    account = facebook_account(workspace, account_key)
    if not account.google_drive_folder_id:
        return None
    return pull_latest_facebook_export(
        workspace,
        container_folder_id=account.google_drive_folder_id,
        cache_dir=account.google_drive_cache_path,
        token_path=account.google_drive_token_path,
        export_name_prefix=account.export_name_prefix,
        registry_path=account.completed_exports_registry_path,
        manifest_path=account.pull_manifest_path,
        allowed_relative_roots=(
            Path("your_facebook_activity/messages"),
            Path("messages"),
        ),
    )


@dataclass(frozen=True)
class InstagramExportFolder:
    folder_id: str
    name: str
    relative_path: Path


def pull_latest_instagram_export(
    workspace: Workspace,
    *,
    container_folder_id: str,
    cache_dir: Path | None = None,
    token_path: Path | None = None,
    api_base_url: str | None = None,
    export_name_prefix: str | None = None,
    registry_path: Path | None = None,
    manifest_path: Path | None = None,
) -> DrivePullResult:
    workspace.ensure_workspace(force=False)
    cache_root = cache_dir or configured_drive_cache_dir(workspace)
    token_source = token_path or configured_drive_token_path(workspace)
    token = _load_token(token_source)
    access_token = _valid_access_token(token_source, token)
    base_url = api_base_url or os.environ.get("LOCALGRAPH_DRIVE_API_BASE_URL", DRIVE_API_BASE_URL)
    candidates = _list_instagram_exports(
        base_url,
        access_token,
        container_folder_id,
        export_name_prefix=export_name_prefix,
    )
    selected = candidates[-1]
    registry_target = registry_path or workspace.state_dir / COMPLETED_EXPORTS_REGISTRY_NAME
    manifest_target = manifest_path or workspace.state_dir / "google-drive-pull-manifest.json"
    registry = _load_json(registry_target, default={"exports": {}})
    if not isinstance(registry, dict):
        registry = {"exports": {}}
    entries = registry.get("exports")
    if not isinstance(entries, dict):
        entries = {}
        registry["exports"] = entries

    result = DrivePullResult(
        folder_id=selected.folder_id,
        cache_path=cache_root / selected.relative_path,
        manifest_path=manifest_target,
    )
    for candidate in candidates:
        selected_cache = cache_root / candidate.relative_path
        entry = entries.get(candidate.folder_id)
        if _completed_export_entry_is_valid(cache_root, candidate, entry):
            if not isinstance(entry, dict) or entry.get("status") != "no-message-files":
                result.completed_export_paths.append(selected_cache.resolve())
            else:
                result.skipped += 1
            continue
        pulled = pull_google_drive_folder(
            workspace,
            folder_id=candidate.folder_id,
            cache_dir=selected_cache,
            token_path=token_source,
            api_base_url=base_url,
            manifest_path=manifest_target,
            allowed_relative_roots=(
                Path("your_instagram_activity/messages"),
                Path("messages"),
            ),
        )
        result.files_seen += pulled.files_seen
        result.folders_seen += pulled.folders_seen
        result.downloaded += pulled.downloaded
        result.unchanged += pulled.unchanged
        result.skipped += pulled.skipped
        result.bytes_downloaded += pulled.bytes_downloaded
        result.warnings.extend(pulled.warnings)
        if int(scan_instagram_source(selected_cache)["totalMessageFiles"]) <= 0:
            entries[candidate.folder_id] = {
                "folderId": candidate.folder_id,
                "name": candidate.name,
                "relativePath": candidate.relative_path.as_posix(),
                "status": "no-message-files",
                "checkedAt": _now_iso(),
            }
            registry.update({"containerFolderId": container_folder_id, "updatedAt": _now_iso()})
            _write_json_private(registry_target, registry)
            result.warnings.append(f"skipped Instagram export without message files: {candidate.name}")
            continue
        entries[candidate.folder_id] = {
            "folderId": candidate.folder_id,
            "name": candidate.name,
            "relativePath": candidate.relative_path.as_posix(),
            "status": "completed",
            "completedAt": _now_iso(),
        }
        registry.update({"containerFolderId": container_folder_id, "updatedAt": _now_iso()})
        _write_json_private(registry_target, registry)
        result.completed_export_paths.append(selected_cache.resolve())

    result.configured_folder_id = container_folder_id
    result.selected_export_id = selected.folder_id
    result.selected_export_name = selected.name
    result.selected_export_ids = [candidate.folder_id for candidate in candidates]
    result.selected_export_names = [candidate.name for candidate in candidates]
    return result


def pull_latest_facebook_export(
    workspace: Workspace,
    *,
    container_folder_id: str,
    cache_dir: Path,
    token_path: Path,
    export_name_prefix: str,
    registry_path: Path,
    manifest_path: Path,
    allowed_relative_roots: tuple[Path, ...],
    api_base_url: str | None = None,
) -> DrivePullResult:
    """Pull all completed Facebook message packets matching one account prefix."""
    workspace.ensure_workspace(force=False)
    token = _load_token(token_path)
    access_token = _valid_access_token(token_path, token)
    base_url = api_base_url or os.environ.get("LOCALGRAPH_DRIVE_API_BASE_URL", DRIVE_API_BASE_URL)
    candidates = _list_instagram_exports(
        base_url,
        access_token,
        container_folder_id,
        export_name_prefix=export_name_prefix,
    )
    selected = candidates[-1]
    registry = _load_json(registry_path, default={"exports": {}})
    if not isinstance(registry, dict):
        registry = {"exports": {}}
    entries = registry.get("exports")
    if not isinstance(entries, dict):
        entries = {}
        registry["exports"] = entries

    result = DrivePullResult(
        folder_id=selected.folder_id,
        cache_path=cache_dir / selected.relative_path,
        manifest_path=manifest_path,
    )
    for candidate in candidates:
        selected_cache = cache_dir / candidate.relative_path
        entry = entries.get(candidate.folder_id)
        if _completed_facebook_export_entry_is_valid(cache_dir, candidate, entry):
            if not isinstance(entry, dict) or entry.get("status") != "no-message-files":
                result.completed_export_paths.append(selected_cache.resolve())
            else:
                result.skipped += 1
            continue
        pulled = pull_google_drive_folder(
            workspace,
            folder_id=candidate.folder_id,
            cache_dir=selected_cache,
            token_path=token_path,
            api_base_url=base_url,
            manifest_path=manifest_path,
            allowed_relative_roots=allowed_relative_roots,
        )
        result.files_seen += pulled.files_seen
        result.folders_seen += pulled.folders_seen
        result.downloaded += pulled.downloaded
        result.unchanged += pulled.unchanged
        result.skipped += pulled.skipped
        result.bytes_downloaded += pulled.bytes_downloaded
        result.warnings.extend(pulled.warnings)
        if int(scan_facebook_source(selected_cache)["totalMessageFiles"]) <= 0:
            entries[candidate.folder_id] = {
                "folderId": candidate.folder_id,
                "name": candidate.name,
                "relativePath": candidate.relative_path.as_posix(),
                "status": "no-message-files",
                "checkedAt": _now_iso(),
            }
            result.warnings.append(f"skipped Facebook export without message files: {candidate.name}")
        else:
            entries[candidate.folder_id] = {
                "folderId": candidate.folder_id,
                "name": candidate.name,
                "relativePath": candidate.relative_path.as_posix(),
                "status": "completed",
                "completedAt": _now_iso(),
            }
            result.completed_export_paths.append(selected_cache.resolve())
        registry.update({"containerFolderId": container_folder_id, "updatedAt": _now_iso()})
        _write_json_private(registry_path, registry)

    result.configured_folder_id = container_folder_id
    result.selected_export_id = selected.folder_id
    result.selected_export_name = selected.name
    result.selected_export_ids = [candidate.folder_id for candidate in candidates]
    result.selected_export_names = [candidate.name for candidate in candidates]
    return result


def _completed_facebook_export_entry_is_valid(
    cache_root: Path,
    candidate: InstagramExportFolder,
    entry: object,
) -> bool:
    if not isinstance(entry, dict):
        return False
    if entry.get("relativePath") != candidate.relative_path.as_posix():
        return False
    if entry.get("status") == "no-message-files":
        return True
    export = cache_root / candidate.relative_path
    return int(scan_facebook_source(export)["totalMessageFiles"]) > 0


def _select_latest_instagram_export(
    base_url: str,
    access_token: str,
    container_folder_id: str,
    export_name_prefix: str | None = None,
) -> InstagramExportFolder:
    return _list_instagram_exports(
        base_url,
        access_token,
        container_folder_id,
        export_name_prefix=export_name_prefix,
    )[-1]


def _list_instagram_exports(
    base_url: str,
    access_token: str,
    container_folder_id: str,
    *,
    export_name_prefix: str | None = None,
) -> list[InstagramExportFolder]:
    children = _list_drive_children(base_url, access_token, container_folder_id)
    candidates: list[InstagramExportFolder] = []
    meta_folders: list[tuple[str, Path]] = []
    for item in children:
        if str(item.get("mimeType") or "") != DRIVE_FOLDER_MIME_TYPE:
            continue
        name = str(item.get("name") or "")
        folder_id = str(item.get("id") or "")
        if _matches_instagram_export_name(name, export_name_prefix) and folder_id:
            candidates.append(InstagramExportFolder(folder_id, name, Path(_safe_drive_name(name))))
            continue
        if not name.startswith("meta-") or not folder_id:
            continue
        meta_folders.append((folder_id, Path(_safe_drive_name(name))))
    for _, relative_meta, exports in _list_drive_children_for_folders(
        base_url,
        lambda: access_token,
        meta_folders,
    ):
        for export in exports:
            export_name = str(export.get("name") or "")
            export_id = str(export.get("id") or "")
            if (
                str(export.get("mimeType") or "") == DRIVE_FOLDER_MIME_TYPE
                and _matches_instagram_export_name(export_name, export_name_prefix)
                and export_id
            ):
                candidates.append(
                    InstagramExportFolder(
                        export_id,
                        export_name,
                        relative_meta / _safe_drive_name(export_name),
                    )
                )
    if not candidates:
        raise DriveAPIError(
            "configured Google Drive container has no matching dated Meta exports for the configured account prefix"
        )
    return sorted(
        candidates,
        key=lambda candidate: (_instagram_export_name_key(candidate.name), candidate.relative_path.as_posix()),
    )


def _matches_instagram_export_name(name: str, export_name_prefix: str | None) -> bool:
    if export_name_prefix is None:
        return name.startswith("instagram-")
    if not name.startswith(export_name_prefix):
        return False
    return re.match(r"^20\d{2}-\d{2}-\d{2}(?:-|$)", name[len(export_name_prefix) :]) is not None


def completed_instagram_export_paths(workspace: Workspace, *, account_key: str | None = None) -> list[Path]:
    if account_key is not None:
        account = instagram_account(workspace, account_key)
        cache_root = account.google_drive_cache_path.resolve()
        registry_path = account.completed_exports_registry_path
    else:
        cache_root = configured_drive_cache_dir(workspace).resolve()
        registry_path = workspace.state_dir / COMPLETED_EXPORTS_REGISTRY_NAME
    registry = _load_json(registry_path, default={"exports": {}})
    entries = registry.get("exports") if isinstance(registry, dict) else None
    if not isinstance(entries, dict):
        return []
    completed: list[Path] = []
    for entry in entries.values():
        if not isinstance(entry, dict):
            continue
        if entry.get("status") == "no-message-files":
            continue
        relative = entry.get("relativePath")
        if not isinstance(relative, str):
            continue
        candidate = (cache_root / relative).resolve()
        try:
            candidate.relative_to(cache_root)
        except ValueError:
            continue
        if int(scan_instagram_source(candidate)["totalMessageFiles"]) > 0:
            completed.append(candidate)
    return sorted(set(completed), key=lambda path: (_instagram_export_name_key(path.name), path.as_posix()))


def _completed_export_entry_is_valid(
    cache_root: Path,
    candidate: InstagramExportFolder,
    entry: object,
) -> bool:
    if not isinstance(entry, dict):
        return False
    if entry.get("relativePath") != candidate.relative_path.as_posix():
        return False
    if entry.get("status") == "no-message-files":
        return True
    export = cache_root / candidate.relative_path
    return int(scan_instagram_source(export)["totalMessageFiles"]) > 0


def _instagram_export_name_key(name: str) -> tuple[int, int, int]:
    match = re.search(r"-(20\d{2})-(\d{2})-(\d{2})(?:-|$)", name)
    if match is None:
        return (0, 0, 0)
    return tuple(int(value) for value in match.groups())  # type: ignore[return-value]


def pull_google_drive_folder(
    workspace: Workspace,
    *,
    folder_id: str,
    cache_dir: Path | None = None,
    token_path: Path | None = None,
    api_base_url: str | None = None,
    allowed_relative_roots: tuple[Path, ...] | None = None,
    manifest_path: Path | None = None,
) -> DrivePullResult:
    workspace.ensure_workspace(force=False)
    cache_root = cache_dir or configured_drive_cache_dir(workspace)
    token_source = token_path or configured_drive_token_path(workspace)
    cache_root.mkdir(parents=True, exist_ok=True)
    manifest_target = manifest_path or workspace.state_dir / "google-drive-pull-manifest.json"
    manifest = _load_json(manifest_target, default={"files": {}})
    if "files" not in manifest or not isinstance(manifest["files"], dict):
        manifest["files"] = {}

    token = _load_token(token_source)
    base_url = api_base_url or os.environ.get("LOCALGRAPH_DRIVE_API_BASE_URL", DRIVE_API_BASE_URL)
    result = DrivePullResult(folder_id=folder_id, cache_path=cache_root, manifest_path=manifest_target)
    used_paths: dict[Path, str] = {}

    def current_access_token() -> str:
        return _valid_access_token(token_source, token)

    visited_folder_ids = {folder_id}
    frontier = [(folder_id, Path())]
    while frontier:
        next_frontier: list[tuple[str, Path]] = []
        for _, relative_dir, children in _list_drive_children_for_folders(
            base_url,
            current_access_token,
            frontier,
        ):
            for item in children:
                name = str(item.get("name") or item.get("id") or "unnamed")
                item_id = str(item.get("id") or "")
                mime_type = str(item.get("mimeType") or "")
                relative_path = relative_dir / _safe_drive_name(name)
                if mime_type == DRIVE_FOLDER_MIME_TYPE:
                    if allowed_relative_roots is not None and not _path_intersects_allowed_roots(
                        relative_path,
                        allowed_relative_roots,
                    ):
                        result.skipped += 1
                        continue
                    if not item_id or item_id in visited_folder_ids:
                        result.skipped += 1
                        continue
                    visited_folder_ids.add(item_id)
                    result.folders_seen += 1
                    next_frontier.append((item_id, relative_path))
                    continue
                if allowed_relative_roots is not None and not any(
                    relative_path.is_relative_to(root) for root in allowed_relative_roots
                ):
                    result.skipped += 1
                    continue
                if mime_type.startswith("application/vnd.google-apps."):
                    result.skipped += 1
                    result.warnings.append(f"skipped unsupported Google Workspace file: {relative_dir / name}")
                    continue
                capabilities = item.get("capabilities")
                if isinstance(capabilities, dict) and capabilities.get("canDownload") is False:
                    result.skipped += 1
                    result.warnings.append(f"skipped non-downloadable file: {relative_dir / name}")
                    continue
                result.files_seen += 1
                target = _unique_target(cache_root, relative_path, item_id, used_paths)
                manifest_entry = manifest["files"].get(item_id, {})  # type: ignore[index]
                if _is_unchanged(target, item, manifest_entry):
                    result.unchanged += 1
                    continue
                downloaded = _download_drive_file(
                    base_url,
                    current_access_token(),
                    item_id,
                    target,
                )
                result.downloaded += 1
                result.bytes_downloaded += downloaded
                manifest["files"][item_id] = {
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "mimeType": item.get("mimeType"),
                    "modifiedTime": item.get("modifiedTime"),
                    "size": item.get("size"),
                    "md5Checksum": item.get("md5Checksum"),
                    "relativePath": target.relative_to(cache_root).as_posix(),
                    "downloadedAt": _now_iso(),
                }
        frontier = next_frontier
    manifest.update({"folderId": folder_id, "cachePath": str(cache_root), "updatedAt": _now_iso()})
    _write_json_private(manifest_target, manifest)
    return result


def _path_intersects_allowed_roots(path: Path, allowed_roots: tuple[Path, ...]) -> bool:
    return any(path.is_relative_to(root) or root.is_relative_to(path) for root in allowed_roots)


def configured_drive_cache_dir(workspace: Workspace) -> Path:
    config = _load_config(workspace)
    value = (
        config.get("imports", {})
        .get("instagram", {})
        .get("googleDriveCachePath")
    )
    return _resolve_workspace_path(workspace, Path(str(value))) if value else default_drive_cache_dir(workspace)


def configured_drive_token_path(workspace: Workspace) -> Path:
    config = _load_config(workspace)
    value = (
        config.get("imports", {})
        .get("instagram", {})
        .get("googleDriveTokenPath")
    )
    return _resolve_workspace_path(workspace, Path(str(value))) if value else default_drive_token_path(workspace)


def default_drive_cache_dir(workspace: Workspace) -> Path:
    return workspace.sources_dir / "instagram-drive-cache"


def default_drive_token_path(workspace: Workspace) -> Path:
    return workspace.state_dir / "google-drive-token.json"


def _list_drive_children(base_url: str, access_token: str, parent_id: str) -> list[dict[str, object]]:
    files: list[dict[str, object]] = []
    page_token: str | None = None
    while True:
        params = {
            "q": f"'{_drive_query_literal(parent_id)}' in parents and trashed = false",
            "pageSize": "1000",
            "fields": "nextPageToken, files(id, name, mimeType, modifiedTime, size, md5Checksum, capabilities/canDownload, shortcutDetails)",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
            "spaces": "drive",
        }
        if page_token:
            params["pageToken"] = page_token
        payload = _drive_json_request("GET", _url(base_url, "files", params), access_token)
        batch = payload.get("files", [])
        if not isinstance(batch, list):
            raise DriveAPIError("Drive API files.list response did not contain a files list")
        files.extend(item for item in batch if isinstance(item, dict))
        page_token = payload.get("nextPageToken") if isinstance(payload.get("nextPageToken"), str) else None
        if not page_token:
            return files


def _list_drive_children_for_folders(
    base_url: str,
    access_token_for_request: Callable[[], str],
    folders: list[tuple[str, Path]],
) -> list[tuple[str, Path, list[dict[str, object]]]]:
    if not folders:
        return []
    workers = min(DRIVE_LIST_MAX_WORKERS, len(folders))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="localgraph-drive-list") as executor:
        futures = [
            executor.submit(
                _list_drive_children,
                base_url,
                access_token_for_request(),
                folder_id,
            )
            for folder_id, _ in folders
        ]
        return [
            (folder_id, relative_path, future.result())
            for (folder_id, relative_path), future in zip(folders, futures, strict=True)
        ]


def _download_drive_file(base_url: str, access_token: str, file_id: str, target: Path) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.download")
    request = urllib.request.Request(
        _url(base_url, f"files/{urllib.parse.quote(file_id, safe='')}", {"alt": "media", "supportsAllDrives": "true"}),
        headers={"Authorization": f"Bearer {access_token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response, tmp.open("wb") as handle:
            total = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
                total += len(chunk)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise DriveAPIError(f"Drive API download failed for {file_id}: HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise DriveAPIError(f"Drive API download failed for {file_id}: {exc}") from exc
    tmp.replace(target)
    return total


def _valid_access_token(token_path: Path, token: dict[str, object]) -> str:
    access_token = token.get("access_token")
    expires_at = float(token.get("expires_at") or 0)
    if isinstance(access_token, str) and expires_at > time.time() + 60:
        return access_token
    refresh_token = token.get("refresh_token")
    if not isinstance(refresh_token, str):
        raise DriveAPIError(f"Google Drive token is expired and has no refresh token: {token_path}")
    refreshed = _refresh_access_token(token, refresh_token)
    token.update(refreshed)
    token["expires_at"] = time.time() + int(refreshed.get("expires_in", 0))
    _write_json_private(token_path, token)
    new_access_token = token.get("access_token")
    if not isinstance(new_access_token, str):
        raise DriveAPIError("Google token refresh did not return an access token")
    return new_access_token


def _refresh_access_token(token: dict[str, object], refresh_token: str) -> dict[str, object]:
    client_id = token.get("client_id")
    token_uri = str(token.get("token_uri") or os.environ.get("LOCALGRAPH_GOOGLE_TOKEN_URL") or GOOGLE_TOKEN_URL)
    if not isinstance(client_id, str):
        raise DriveAPIError("Google Drive token file is missing client_id")
    data = {
        "client_id": client_id,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    client_secret = token.get("client_secret")
    if isinstance(client_secret, str) and client_secret:
        data["client_secret"] = client_secret
    return _token_request(token_uri, data)


def _exchange_authorization_code(
    client: dict[str, str],
    *,
    code: str,
    redirect_uri: str,
    verifier: str,
) -> dict[str, object]:
    data = {
        "client_id": client["client_id"],
        "code": code,
        "code_verifier": verifier,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }
    if client.get("client_secret"):
        data["client_secret"] = client["client_secret"]
    return _token_request(client["token_uri"], data)


def _token_request(token_uri: str, data: dict[str, str]) -> dict[str, object]:
    body = urllib.parse.urlencode(data).encode("utf-8")
    request = urllib.request.Request(
        token_uri,
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise DriveAPIError(f"Google token request failed: HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise DriveAPIError(f"Google token request failed: {exc}") from exc


def _drive_json_request(method: str, url: str, access_token: str) -> dict[str, object]:
    request = urllib.request.Request(url, method=method, headers={"Authorization": f"Bearer {access_token}"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise DriveAPIError(f"Drive API request failed: HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise DriveAPIError(f"Drive API request failed: {exc}") from exc


def _load_oauth_client(path: Path) -> dict[str, str]:
    payload = json.loads(path.expanduser().read_text(encoding="utf-8"))
    client = payload.get("installed") or payload.get("web")
    if not isinstance(client, dict):
        raise DriveAPIError("OAuth client secrets file must contain an installed or web client")
    client_id = client.get("client_id")
    if not isinstance(client_id, str):
        raise DriveAPIError("OAuth client secrets file is missing client_id")
    return {
        "client_id": client_id,
        "client_secret": str(client.get("client_secret") or ""),
        "auth_uri": str(client.get("auth_uri") or GOOGLE_AUTH_URL),
        "token_uri": str(client.get("token_uri") or GOOGLE_TOKEN_URL),
    }


def _authorization_url(client: dict[str, str], *, redirect_uri: str, state: str, challenge: str) -> str:
    params = {
        "access_type": "offline",
        "client_id": client["client_id"],
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "consent",
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": DRIVE_SCOPE_READONLY,
        "state": state,
    }
    return f"{client['auth_uri']}?{urllib.parse.urlencode(params)}"


def _load_token(path: Path) -> dict[str, object]:
    if not path.exists():
        raise DriveAPIError(f"Google Drive token does not exist. Run drive-auth first: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise DriveAPIError(f"Google Drive token is not a JSON object: {path}")
    return payload


def _is_unchanged(target: Path, item: dict[str, object], manifest_entry: object) -> bool:
    if not target.exists():
        return False
    if isinstance(manifest_entry, dict) and (
        manifest_entry.get("modifiedTime") == item.get("modifiedTime")
        and manifest_entry.get("size") == item.get("size")
        and manifest_entry.get("md5Checksum") == item.get("md5Checksum")
    ):
        return True
    expected_size = item.get("size")
    expected_md5 = item.get("md5Checksum")
    if not isinstance(expected_size, str) or not isinstance(expected_md5, str):
        return False
    if str(target.stat().st_size) != expected_size:
        return False
    digest = hashlib.md5()  # noqa: S324 - Google Drive supplies MD5 as file-integrity metadata
    with target.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest() == expected_md5


def _unique_target(cache_root: Path, relative_path: Path, file_id: str, used_paths: dict[Path, str]) -> Path:
    target = cache_root / relative_path
    owner = used_paths.get(target)
    if owner is None or owner == file_id:
        used_paths[target] = file_id
        return target
    suffix = target.suffix
    stem = target.name[: -len(suffix)] if suffix else target.name
    deduped = target.with_name(f"{stem}--{file_id[:8]}{suffix}")
    used_paths[deduped] = file_id
    return deduped


def _safe_drive_name(value: str) -> str:
    clean = value.replace("/", "_").replace("\0", "")
    if clean == ".":
        return "_"
    if clean == "..":
        return "__"
    return clean or "unnamed"


def _url(base_url: str, path: str, params: dict[str, str]) -> str:
    base = base_url.rstrip("/")
    return f"{base}/{path}?{urllib.parse.urlencode(params)}"


def _drive_query_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _pkce_verifier() -> str:
    return base64.urlsafe_b64encode(os.urandom(64)).rstrip(b"=").decode("ascii")


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _workspace_relative_or_absolute(workspace: Workspace, path: Path) -> str:
    resolved = path.expanduser()
    try:
        return resolved.resolve().relative_to(workspace.root.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def _resolve_workspace_path(workspace: Workspace, path: Path) -> Path:
    expanded = path.expanduser()
    return expanded if expanded.is_absolute() else workspace.root / expanded


def _load_config(workspace: Workspace) -> dict[str, object]:
    if not workspace.config_path.exists():
        return {}
    return json.loads(workspace.config_path.read_text(encoding="utf-8"))


def _write_config(workspace: Workspace, config: dict[str, object]) -> None:
    workspace.config_path.write_text(f"{json.dumps(config, indent=2, sort_keys=True)}\n", encoding="utf-8")


def _load_json(path: Path, *, default: object) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json_private(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{json.dumps(payload, indent=2, sort_keys=True)}\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _now_iso() -> str:
    return datetime_now_utc()


def datetime_now_utc() -> str:
    from datetime import datetime, timezone

    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
