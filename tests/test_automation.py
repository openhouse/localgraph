from __future__ import annotations

import contextlib
import io
import json
import os
import plistlib
import tempfile
import unittest
from pathlib import Path

from localgraph.automation import (
    candidate_instagram_drive_sources,
    install_daily_import,
    latest_instagram_export_source,
    launchd_plist,
)
from localgraph.cli import main
from localgraph.paths import Workspace


class AutomationTests(unittest.TestCase):
    def test_daily_import_reads_explicit_google_drive_source_and_records_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "graph"
            drive_source = base / "Library" / "CloudStorage" / "GoogleDrive-jamie@example.com" / "Shared drives" / "Instagram"
            thread = (
                drive_source
                / "meta-new"
                / "instagram-jamie-new"
                / "your_instagram_activity"
                / "messages"
                / "inbox"
                / "alice_123"
            )
            thread.mkdir(parents=True)
            (thread / "message_1.json").write_text(
                json.dumps(
                    {
                        "participants": [{"name": "Jamie"}, {"name": "Alice"}],
                        "title": "Alice",
                        "messages": [
                            {
                                "sender_name": "Alice",
                                "timestamp_ms": 1700000000000,
                                "content": "daily drive hello",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            old_thread = (
                drive_source
                / "meta-old"
                / "instagram-jamie-old"
                / "your_instagram_activity"
                / "messages"
                / "inbox"
                / "bob_456"
            )
            old_thread.mkdir(parents=True)
            (old_thread / "message_1.json").write_text(
                json.dumps(
                    {
                        "participants": [{"name": "Jamie"}, {"name": "Bob"}],
                        "title": "Bob",
                        "messages": [{"sender_name": "Bob", "timestamp_ms": 1600000000000, "content": "old drive hello"}],
                    }
                ),
                encoding="utf-8",
            )
            set_export_mtime(drive_source / "meta-old" / "instagram-jamie-old", 1_600_000_000)
            set_export_mtime(drive_source / "meta-new" / "instagram-jamie-new", 1_700_000_000)

            code, stdout = run_cli(
                [
                    "--root",
                    str(root),
                    "daily-import",
                    "--instagram-drive-source",
                    str(drive_source),
                    "--skip-imessage",
                    "--me",
                    "Jamie",
                    "--me-instagram",
                    "Jamie",
                    "--write-config",
                ]
            )

            self.assertEqual(code, 0)
            payload = json.loads(stdout)
            self.assertEqual(payload["instagram"]["origin"], "explicit")
            self.assertIsNone(payload["instagram"]["importPath"])
            self.assertTrue(payload["instagram"]["bootstrap"])
            self.assertEqual(
                payload["instagram"]["importPaths"],
                [
                    str((drive_source / "meta-old" / "instagram-jamie-old").resolve()),
                    str((drive_source / "meta-new" / "instagram-jamie-new").resolve()),
                ],
            )
            self.assertTrue(payload["instagram"]["latestOnly"])
            self.assertEqual(payload["result"]["totals"]["messages"], 2)
            self.assertEqual(payload["result"]["totals"]["threads"], 2)
            self.assertEqual(payload["result"]["render"]["threads"], 2)
            self.assertTrue((root / "state" / "daily-import-runs.jsonl").exists())
            config = json.loads((root / "localgraph.config.json").read_text(encoding="utf-8"))
            self.assertEqual(config["imports"]["instagram"]["googleDriveLocalPath"], str(drive_source.resolve()))
            transcript = next((root / "views" / "threads" / "instagram").glob("alice*/messages.md")).read_text(
                encoding="utf-8"
            )
            self.assertIn("daily drive hello", transcript)
            bob_transcript = next((root / "views" / "threads" / "instagram").glob("bob*/messages.md")).read_text(encoding="utf-8")
            self.assertIn("old drive hello", bob_transcript)

    def test_configure_drive_records_source_without_scanning_message_bodies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "graph"
            drive_source = Path(tmp) / "Drive" / "Instagram"
            drive_source.mkdir(parents=True)

            code, stdout = run_cli(
                [
                    "--root",
                    str(root),
                    "configure-drive",
                    "--instagram-drive-source",
                    str(drive_source),
                ]
            )

            self.assertEqual(code, 0)
            payload = json.loads(stdout)
            self.assertEqual(payload["instagramGoogleDriveSource"], str(drive_source.resolve()))
            config = json.loads((root / "localgraph.config.json").read_text(encoding="utf-8"))
            self.assertEqual(config["imports"]["instagram"]["googleDriveLocalPath"], str(drive_source.resolve()))

    def test_daily_import_marks_google_drive_collection_pending_when_no_export_is_materialized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "graph"
            drive_source = base / "Library" / "CloudStorage" / "GoogleDrive-jamie@example.com" / "My Drive" / "Instagram"
            drive_source.mkdir(parents=True)

            code, stdout = run_cli(
                [
                    "--root",
                    str(root),
                    "daily-import",
                    "--instagram-drive-source",
                    str(drive_source),
                    "--skip-imessage",
                    "--no-render",
                ]
            )

            self.assertEqual(code, 0)
            payload = json.loads(stdout)
            self.assertIsNone(payload["instagram"]["importPath"])
            self.assertEqual(payload["result"]["sources"][0]["status"], "pending")
            self.assertEqual(payload["result"]["totals"]["messages"], 0)

    def test_candidate_drive_sources_are_shallow_and_predictable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            root = home / "Library" / "CloudStorage" / "GoogleDrive-jamie@example.com"
            (root / "Shared drives" / "Instagram").mkdir(parents=True)
            (root / "My Drive" / "Instagram").mkdir(parents=True)

            candidates = candidate_instagram_drive_sources(home=home)

            self.assertIn(root / "Shared drives" / "Instagram", candidates)
            self.assertIn(root / "My Drive" / "Instagram", candidates)

    def test_latest_instagram_export_source_prefers_newest_shallow_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Instagram"
            older = root / "meta-old" / "instagram-old" / "your_instagram_activity" / "messages" / "inbox"
            newer = root / "meta-new" / "instagram-new" / "your_instagram_activity" / "messages" / "inbox"
            older.mkdir(parents=True)
            newer.mkdir(parents=True)
            set_export_mtime(root / "meta-old" / "instagram-old", 1_600_000_000)
            set_export_mtime(root / "meta-new" / "instagram-new", 1_700_000_000)

            self.assertEqual(latest_instagram_export_source(root), (root / "meta-new" / "instagram-new").resolve())

    def test_install_daily_import_dry_run_reports_paths_without_writing_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp) / "graph")
            home = Path(tmp) / "home"
            drive_source = Path(tmp) / "Drive" / "Instagram"

            result = install_daily_import(
                workspace,
                instagram_drive_source=drive_source,
                skip_imessage=True,
                me_name="Jamie",
                me_instagram_names=["jamieburkart"],
                hour=4,
                minute=5,
                label="com.example.localgraph.test",
                dry_run=True,
                home=home,
            )

            self.assertTrue(result["dryRun"])
            self.assertEqual(result["hour"], 4)
            self.assertEqual(result["minute"], 5)
            self.assertFalse((workspace.state_dir / "bin" / "localgraph-daily-import.sh").exists())
            self.assertFalse((home / "Library" / "LaunchAgents" / "com.example.localgraph.test.plist").exists())

    def test_launchd_plist_contains_daily_calendar_interval(self) -> None:
        workspace = Workspace(Path("/tmp/localgraph-test"))
        plist = launchd_plist(
            label="com.example.localgraph.test",
            script_path=workspace.state_dir / "bin" / "localgraph-daily-import.sh",
            hour=3,
            minute=15,
            workspace=workspace,
        )

        encoded = plistlib.loads(plistlib.dumps(plist))
        self.assertEqual(encoded["Label"], "com.example.localgraph.test")
        self.assertEqual(encoded["StartCalendarInterval"], {"Hour": 3, "Minute": 15})
        self.assertEqual(encoded["ProgramArguments"][0], "/bin/zsh")


def run_cli(argv: list[str]) -> tuple[int, str]:
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        code = main(argv)
    return code, stream.getvalue()


def set_export_mtime(export_root: Path, value: int) -> None:
    for path in [
        export_root,
        export_root / "your_instagram_activity" / "messages",
        export_root / "your_instagram_activity" / "messages" / "inbox",
    ]:
        os.utime(path, (value, value))


if __name__ == "__main__":
    unittest.main()
