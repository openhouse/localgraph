from __future__ import annotations

import contextlib
import io
import json
import os
import re
import tempfile
import time
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from localgraph.cli import main
from localgraph.drive import configure_google_drive_api, pull_google_drive_folder
from localgraph.paths import Workspace


def folder(file_id: str, name: str) -> dict[str, object]:
    return {
        "id": file_id,
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "modifiedTime": "2026-07-09T12:00:00.000Z",
    }


def message_payload() -> bytes:
    return json.dumps(
        {
            "participants": [{"name": "Jamie"}, {"name": "Alice"}],
            "title": "Alice",
            "messages": [
                {
                    "sender_name": "Alice",
                    "timestamp_ms": 1700000000000,
                    "content": "hello from drive api",
                }
            ],
        }
    ).encode("utf-8")


class DrivePullTests(unittest.TestCase):
    def test_configure_drive_api_records_private_cache_and_token_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp) / "graph")

            result = configure_google_drive_api(workspace, folder_id="drive-folder-123")

            self.assertEqual(result["folderId"], "drive-folder-123")
            config = json.loads(workspace.config_path.read_text(encoding="utf-8"))
            instagram = config["imports"]["instagram"]
            self.assertEqual(instagram["googleDriveFolderId"], "drive-folder-123")
            self.assertEqual(instagram["googleDriveCachePath"], "sources/instagram-drive-cache")
            self.assertEqual(instagram["googleDriveTokenPath"], "state/google-drive-token.json")

    def test_drive_pull_downloads_recursive_folder_into_private_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, fake_drive_api():
            workspace = Workspace(Path(tmp) / "graph")
            token_path = workspace.state_dir / "google-drive-token.json"
            write_token(token_path)

            result = pull_google_drive_folder(
                workspace,
                folder_id="root",
                token_path=token_path,
                api_base_url=FAKE_DRIVE_BASE_URL,
            )

            downloaded = workspace.sources_dir / "instagram-drive-cache" / "meta-2026" / "instagram-jamie-2026-07-09" / "your_instagram_activity" / "messages" / "inbox" / "alice_123" / "message_1.json"
            self.assertTrue(downloaded.exists())
            self.assertIn("hello from drive api", downloaded.read_text(encoding="utf-8"))
            self.assertEqual(result.files_seen, 1)
            self.assertEqual(result.downloaded, 1)
            manifest = json.loads((workspace.state_dir / "google-drive-pull-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["files"]["file-message"]["relativePath"], downloaded.relative_to(result.cache_path).as_posix())

    def test_daily_import_pulls_drive_api_cache_before_importing_instagram(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, fake_drive_api():
            root = Path(tmp) / "graph"
            workspace = Workspace(root)
            workspace.ensure_workspace(force=False)
            write_token(workspace.state_dir / "google-drive-token.json")
            code, _ = run_cli(["--root", str(root), "configure-drive-api", "--folder-id", "root"])
            self.assertEqual(code, 0)

            with patched_env("LOCALGRAPH_DRIVE_API_BASE_URL", FAKE_DRIVE_BASE_URL):
                code, stdout = run_cli(
                    [
                        "--root",
                        str(root),
                        "daily-import",
                        "--skip-imessage",
                        "--no-render",
                        "--me",
                        "Jamie",
                        "--me-instagram",
                        "Jamie",
                    ]
                )

            self.assertEqual(code, 0)
            payload = json.loads(stdout)
            self.assertEqual(payload["googleDrivePull"]["status"], "pulled")
            self.assertEqual(payload["googleDrivePull"]["downloaded"], 1)
            self.assertEqual(payload["result"]["totals"]["messages"], 1)
            self.assertEqual(payload["result"]["totals"]["threads"], 1)
            self.assertIn("instagram-drive-cache", payload["instagram"]["importPaths"][0])

    def test_daily_import_prefers_configured_drive_api_when_explicit_local_source_is_empty(self) -> None:
        """Catch schedulers pinning an empty Drive Desktop path and bypassing the API pull."""
        with tempfile.TemporaryDirectory() as tmp, fake_drive_api():
            root = Path(tmp) / "graph"
            workspace = Workspace(root)
            workspace.ensure_workspace(force=False)
            empty_drive_source = (
                Path(tmp)
                / "Library"
                / "CloudStorage"
                / "GoogleDrive-jamie@example.com"
                / "Shared drives"
                / "Instagram"
            )
            empty_drive_source.mkdir(parents=True)
            write_token(workspace.state_dir / "google-drive-token.json")
            code, _ = run_cli(["--root", str(root), "configure-drive-api", "--folder-id", "root"])
            self.assertEqual(code, 0)

            with patched_env("LOCALGRAPH_DRIVE_API_BASE_URL", FAKE_DRIVE_BASE_URL):
                code, stdout = run_cli(
                    [
                        "--root",
                        str(root),
                        "daily-import",
                        "--instagram-drive-source",
                        str(empty_drive_source),
                        "--skip-imessage",
                        "--no-render",
                        "--me",
                        "Jamie",
                        "--me-instagram",
                        "Jamie",
                    ]
                )

            self.assertEqual(code, 0)
            payload = json.loads(stdout)
            self.assertEqual(payload["googleDrivePull"]["status"], "pulled")
            self.assertEqual(payload["instagram"]["origin"], "google-drive-api-current")
            self.assertEqual(payload["result"]["totals"]["messages"], 1)
            self.assertIn("instagram-drive-cache", payload["instagram"]["importPaths"][0])

    def test_daily_import_uses_existing_private_cache_when_drive_pull_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "graph"
            workspace = Workspace(root)
            workspace.ensure_workspace(force=False)
            cache_file = (
                workspace.sources_dir
                / "instagram-drive-cache"
                / "meta-2026"
                / "instagram-jamie-2026-07-09"
                / "your_instagram_activity"
                / "messages"
                / "inbox"
                / "alice_123"
                / "message_1.json"
            )
            cache_file.parent.mkdir(parents=True)
            cache_file.write_bytes(message_payload())
            code, _ = run_cli(["--root", str(root), "configure-drive-api", "--folder-id", "root"])
            self.assertEqual(code, 0)

            code, stdout = run_cli(
                [
                    "--root",
                    str(root),
                    "daily-import",
                    "--skip-imessage",
                    "--no-render",
                    "--me",
                    "Jamie",
                    "--me-instagram",
                    "Jamie",
                ]
            )

            self.assertEqual(code, 0)
            payload = json.loads(stdout)
            self.assertEqual(payload["googleDrivePull"]["status"], "error")
            self.assertIn("Google Drive token does not exist", payload["googleDrivePull"]["error"])
            self.assertEqual(payload["result"]["totals"]["messages"], 1)
            self.assertIn("instagram-drive-cache", payload["instagram"]["importPaths"][0])

    def test_successful_drive_pull_advances_the_current_instagram_mirror(self) -> None:
        """Catch completed provider pulls that never publish a stable current directory."""
        with tempfile.TemporaryDirectory() as tmp, fake_drive_api():
            root = Path(tmp) / "graph"
            workspace = Workspace(root)
            workspace.ensure_workspace(force=False)
            write_token(workspace.state_dir / "google-drive-token.json")
            code, _ = run_cli(["--root", str(root), "configure-drive-api", "--folder-id", "root"])
            self.assertEqual(code, 0)

            with patched_env("LOCALGRAPH_DRIVE_API_BASE_URL", FAKE_DRIVE_BASE_URL):
                code, stdout = run_cli(
                    [
                        "--root",
                        str(root),
                        "instagram-sync",
                        "--no-render",
                        "--me",
                        "Jamie",
                        "--me-instagram",
                        "Jamie",
                    ]
                )

            self.assertEqual(code, 0)
            payload = json.loads(stdout)
            current = workspace.sources_dir / "instagram-current"
            self.assertTrue(current.is_symlink())
            self.assertEqual(current.resolve(), Path(payload["instagram"]["resolvedExportPath"]))
            self.assertEqual(Path(payload["instagram"]["path"]).resolve(), current.resolve())
            self.assertEqual(payload["instagram"]["origin"], "google-drive-api-current")
            self.assertEqual(payload["instagramSync"]["status"], "current")
            self.assertEqual(payload["instagramSync"]["messageFiles"], 1)
            status = json.loads((workspace.state_dir / "instagram-sync-status.json").read_text(encoding="utf-8"))
            self.assertEqual(Path(status["localMirrorPath"]).resolve(), current.resolve())
            self.assertEqual(status["status"], "current")

    def test_configured_sync_selects_only_the_latest_export_from_a_drive_container(self) -> None:
        """Catch a stable container configuration downloading unrelated or older Drive trees."""
        with tempfile.TemporaryDirectory() as tmp, fake_drive_api(latest_container_children, latest_container_payloads):
            root = Path(tmp) / "graph"
            workspace = Workspace(root)
            workspace.ensure_workspace(force=False)
            write_token(workspace.state_dir / "google-drive-token.json")
            code, _ = run_cli(["--root", str(root), "configure-drive-api", "--folder-id", "root"])
            self.assertEqual(code, 0)

            with patched_env("LOCALGRAPH_DRIVE_API_BASE_URL", FAKE_DRIVE_BASE_URL):
                code, stdout = run_cli(
                    [
                        "--root",
                        str(root),
                        "instagram-sync",
                        "--no-render",
                        "--me",
                        "Jamie",
                        "--me-instagram",
                        "Jamie",
                    ]
                )

            self.assertEqual(code, 0)
            payload = json.loads(stdout)
            self.assertEqual(payload["googleDrivePull"]["selectedExportName"], "instagram-jamie-2026-08-25-latest")
            self.assertEqual(payload["googleDrivePull"]["downloaded"], 1)
            self.assertIn("instagram-jamie-2026-08-25-latest", payload["instagram"]["resolvedExportPath"])
            old_export = workspace.sources_dir / "instagram-drive-cache" / "meta-2026-Jul-01-00-00-00"
            self.assertFalse(old_export.exists())

    def test_failed_drive_pull_keeps_the_last_known_good_current_mirror(self) -> None:
        """Catch a partial newer cache folder replacing the last completed provider snapshot."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "graph"
            workspace = Workspace(root)
            workspace.ensure_workspace(force=False)
            cache = workspace.sources_dir / "instagram-drive-cache"
            old_file = (
                cache
                / "meta-2026"
                / "instagram-jamie-2026-07-09"
                / "your_instagram_activity"
                / "messages"
                / "inbox"
                / "alice_123"
                / "message_1.json"
            )
            old_file.parent.mkdir(parents=True)
            old_file.write_bytes(message_payload())
            partial_new = (
                cache
                / "meta-2026"
                / "instagram-jamie-2026-07-10"
                / "your_instagram_activity"
                / "messages"
                / "inbox"
                / "partial_456"
                / "message_1.json"
            )
            partial_new.parent.mkdir(parents=True)
            partial_new.write_text(
                json.dumps(
                    {
                        "participants": [{"name": "Jamie"}, {"name": "Partial"}],
                        "title": "Partial",
                        "messages": [
                            {
                                "sender_name": "Partial",
                                "timestamp_ms": 1800000000000,
                                "content": "must not import a partial provider pull",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            current = workspace.sources_dir / "instagram-current"
            current.symlink_to(old_file.parents[4], target_is_directory=True)
            code, _ = run_cli(["--root", str(root), "configure-drive-api", "--folder-id", "root"])
            self.assertEqual(code, 0)

            code, stdout = run_cli(
                [
                    "--root",
                    str(root),
                    "instagram-sync",
                    "--no-render",
                    "--me",
                    "Jamie",
                    "--me-instagram",
                    "Jamie",
                ]
            )

            self.assertEqual(code, 0)
            payload = json.loads(stdout)
            self.assertEqual(current.resolve(), old_file.parents[4].resolve())
            self.assertEqual(payload["instagram"]["origin"], "google-drive-last-known-good")
            self.assertEqual(payload["instagramSync"]["status"], "degraded")
            self.assertEqual(payload["result"]["totals"]["messages"], 1)
            self.assertEqual(payload["result"]["totals"]["threads"], 1)


FAKE_DRIVE_BASE_URL = "https://drive.test/drive/v3"


@contextlib.contextmanager
def fake_drive_api(children_factory=None, payload_factory=None):
    def urlopen(request, timeout=0):  # type: ignore[no-untyped-def]
        return fake_urlopen(request, timeout=timeout, children_factory=children_factory, payload_factory=payload_factory)

    with mock.patch("localgraph.drive.urllib.request.urlopen", side_effect=urlopen):
        yield


def fake_urlopen(request, timeout=0, *, children_factory=None, payload_factory=None):  # type: ignore[no-untyped-def]
    url = request.full_url if hasattr(request, "full_url") else str(request)
    parsed = urllib.parse.urlparse(url)
    if parsed.path == "/drive/v3/files":
        params = urllib.parse.parse_qs(parsed.query)
        q = params.get("q", [""])[0]
        match = re.search(r"'([^']+)' in parents", q)
        parent = match.group(1) if match else ""
        children = children_factory() if children_factory is not None else fake_children()
        return FakeResponse(json.dumps({"files": children.get(parent, [])}).encode("utf-8"))
    file_id = parsed.path.rsplit("/", 1)[-1]
    payloads = payload_factory() if payload_factory is not None else {"file-message": message_payload()}
    if file_id in payloads:
        return FakeResponse(payloads[file_id])
    raise AssertionError(f"unexpected fake Drive URL: {url}")


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.offset = 0

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        return None

    def read(self, size: int | None = None) -> bytes:
        if size is None or size < 0:
            chunk = self.body[self.offset :]
            self.offset = len(self.body)
            return chunk
        chunk = self.body[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk


def fake_children() -> dict[str, list[dict[str, object]]]:
    return {
        "root": [folder("folder-meta", "meta-2026")],
        "folder-meta": [folder("folder-export", "instagram-jamie-2026-07-09")],
        "folder-export": [folder("folder-activity", "your_instagram_activity")],
        "folder-activity": [folder("folder-messages", "messages")],
        "folder-messages": [folder("folder-inbox", "inbox")],
        "folder-inbox": [folder("folder-thread", "alice_123")],
        "folder-thread": [
            {
                "id": "file-message",
                "name": "message_1.json",
                "mimeType": "application/json",
                "modifiedTime": "2026-07-09T12:00:00.000Z",
                "size": str(len(message_payload())),
                "md5Checksum": "fixture-md5",
                "capabilities": {"canDownload": True},
            }
        ],
    }


def latest_container_children() -> dict[str, list[dict[str, object]]]:
    return {
        "root": [
            folder("folder-unrelated", "Unrelated private folder"),
            folder("folder-meta-old", "meta-2026-Jul-01-00-00-00"),
            folder("folder-meta-new", "meta-2026-Aug-19-12-26-26"),
        ],
        "folder-meta-old": [folder("folder-export-old", "instagram-jamie-2026-08-24-old")],
        "folder-meta-new": [folder("folder-export-latest", "instagram-jamie-2026-08-25-latest")],
        "folder-export-latest": [folder("folder-activity-latest", "your_instagram_activity")],
        "folder-activity-latest": [folder("folder-messages-latest", "messages")],
        "folder-messages-latest": [folder("folder-inbox-latest", "inbox")],
        "folder-inbox-latest": [folder("folder-thread-latest", "alice_123")],
        "folder-thread-latest": [
            {
                "id": "file-latest",
                "name": "message_1.json",
                "mimeType": "application/json",
                "modifiedTime": "2026-08-25T19:04:43.973Z",
                "size": str(len(message_payload())),
                "md5Checksum": "latest-fixture-md5",
                "capabilities": {"canDownload": True},
            }
        ],
    }


def latest_container_payloads() -> dict[str, bytes]:
    return {"file-latest": message_payload()}


def write_token(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"access_token": "test-token", "expires_at": time.time() + 3600}), encoding="utf-8")


@contextlib.contextmanager
def patched_env(key: str, value: str):
    original = os.environ.get(key)
    os.environ[key] = value
    try:
        yield
    finally:
        if original is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = original


def run_cli(argv: list[str]) -> tuple[int, str]:
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        code = main(argv)
    return code, stream.getvalue()


if __name__ == "__main__":
    unittest.main()
