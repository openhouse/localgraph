from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import plistlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import localgraph.automation as automation
from localgraph.automation import (
    candidate_instagram_drive_sources,
    install_daily_import,
    install_instagram_sync,
    latest_instagram_export_source,
    launchd_plist,
    resolve_instagram_import_sources,
)
from localgraph.cli import main
from localgraph.paths import Workspace


class AutomationTests(unittest.TestCase):
    def test_pending_instagram_account_can_be_registered_without_stalling_active_accounts(self) -> None:
        """Catch onboarding an account without an export blocking already operational accounts."""
        from localgraph.instagram_accounts import instagram_accounts

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp) / "graph")
            configure_test_accounts(workspace)
            for key, owner, source in (
                ("jamieburkart", "Jamie", workspace.sources_dir / "instagram-current"),
                ("nycartc", "nycartc", workspace.sources_dir / "instagram-accounts/nycartc/current"),
            ):
                write_instagram_packet(source / f"instagram-{key}-2026-08-29-current", owner=owner, correspondent="Example Person", content="example")
            try:
                code, _ = run_cli(["--root", str(workspace.root), "configure-instagram-account", "--account", "examplepending", "--profile-name", "examplepending", "--owner-display-name", "Example Pending", "--owner-kind", "organization", "--disabled"])
            except SystemExit as exc:
                self.fail(f"paused account registration was rejected: {exc}")
            self.assertEqual(code, 0)
            self.assertEqual(len(instagram_accounts(workspace, enabled_only=False)), 3)
            self.assertEqual(len(instagram_accounts(workspace)), 2)
            code, stdout = run_cli(["--root", str(workspace.root), "instagram-sync", "--no-render"])
            self.assertEqual(code, 0)
            payload = json.loads(stdout)
            self.assertEqual(payload["instagramSync"]["status"], "current")
            self.assertEqual(payload["instagramSync"]["accountsReady"], 2)

    def test_multi_account_launchagent_reads_accounts_from_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp) / "graph")
            home = Path(tmp) / "home"
            configure_test_accounts(workspace)

            result = install_instagram_sync(
                workspace,
                interval_minutes=60,
                me_name="Jamie",
                me_instagram_names=["stale-hard-coded-account"],
                label="com.example.localgraph.instagram-sync",
                home=home,
            )

            script = Path(result["script"]).read_text(encoding="utf-8")
            self.assertIn("instagram-sync", script)
            self.assertNotIn("--me-instagram", script)
            self.assertNotIn("stale-hard-coded-account", script)

    def test_multi_account_sync_preserves_projection_until_every_account_has_a_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp) / "graph")
            seed = workspace.sources_dir / "seed" / "instagram-jamieburkart-2026-08-28-seed"
            write_instagram_packet(seed, owner="Jamie", correspondent="Existing Person", content="existing")
            code, _ = run_cli(
                [
                    "--root",
                    str(workspace.root),
                    "import",
                    "--instagram-source",
                    str(seed),
                    "--skip-imessage",
                    "--me-instagram",
                    "Jamie",
                ]
            )
            self.assertEqual(code, 0)
            configure_test_accounts(workspace)
            jamie_export = workspace.sources_dir / "instagram-current" / "instagram-jamieburkart-2026-08-29-current"
            write_instagram_packet(jamie_export, owner="Jamie", correspondent="New Person", content="new")

            code, stdout = run_cli(["--root", str(workspace.root), "instagram-sync", "--no-render"])

            self.assertEqual(code, 0)
            payload = json.loads(stdout)
            self.assertEqual(payload["instagramSync"]["status"], "pending")
            self.assertEqual(payload["instagramAccounts"]["jamieburkart"]["sync"]["status"], "local-current")
            self.assertEqual(payload["instagramAccounts"]["nycartc"]["sync"]["status"], "pending")
            with contextlib.closing(sqlite3.connect(workspace.database_path)) as db:
                messages = db.execute(
                    "SELECT COUNT(*) FROM messages JOIN threads ON threads.id = messages.thread_id "
                    "WHERE threads.source_kind = 'instagram'"
                ).fetchone()[0]
            self.assertEqual(messages, 1, "a pending second account must not clear the last complete projection")
            aggregate = json.loads((workspace.state_dir / "instagram-accounts-sync-status.json").read_text(encoding="utf-8"))
            self.assertEqual(aggregate["status"], "pending")

    def test_multi_account_sync_atomically_rebuilds_accounts_identities_status_and_views(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp) / "graph")
            configure_test_accounts(workspace)
            jamie_export = workspace.sources_dir / "instagram-current" / "instagram-jamieburkart-2026-08-29-current"
            nycartc_export = (
                workspace.sources_dir
                / "instagram-accounts"
                / "nycartc"
                / "current"
                / "instagram-nycartc-2026-08-29-current"
            )
            write_instagram_packet(jamie_export, owner="Jamie", correspondent="Shared Person", content="personal")
            write_instagram_packet(nycartc_export, owner="nycartc", correspondent="Shared Person", content="coalition")

            code, stdout = run_cli(["--root", str(workspace.root), "instagram-sync"])

            self.assertEqual(code, 0)
            payload = json.loads(stdout)
            self.assertEqual(payload["instagramSync"]["status"], "current")
            self.assertEqual(payload["instagramSync"]["accountsConfigured"], 2)
            self.assertEqual(payload["result"]["totals"]["threads"], 2)
            self.assertEqual(payload["result"]["totals"]["messages"], 2)
            with contextlib.closing(sqlite3.connect(workspace.database_path)) as db:
                thread_keys = [
                    row[0]
                    for row in db.execute(
                        "SELECT source_thread_key FROM threads WHERE source_kind = 'instagram' ORDER BY source_thread_key"
                    )
                ]
                identity_keys = {
                    row[0]
                    for row in db.execute(
                        "SELECT stable_key FROM identities WHERE stable_key IN ('person:self', 'organization:instagram:nycartc')"
                    )
                }
            self.assertEqual(
                thread_keys,
                [
                    "jamieburkart:your_instagram_activity/messages/inbox/shared-person_123",
                    "nycartc:your_instagram_activity/messages/inbox/shared-person_123",
                ],
            )
            self.assertEqual(identity_keys, {"person:self", "organization:instagram:nycartc"})
            self.assertTrue((workspace.views_dir / "instagram-accounts" / "jamieburkart" / "threads").is_dir())
            self.assertTrue((workspace.views_dir / "instagram-accounts" / "nycartc" / "threads").is_dir())

    def test_instagram_sync_lock_allows_only_one_workspace_writer(self) -> None:
        """Catch manual and launchd sync runs racing on the same private cache files."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp) / "graph")
            workspace.ensure_workspace(force=False)
            lock = getattr(automation, "instagram_sync_lock", None)
            self.assertTrue(callable(lock), "instagram_sync_lock is missing")

            with lock(workspace) as first_acquired:
                with lock(workspace) as second_acquired:
                    self.assertTrue(first_acquired)
                    self.assertFalse(second_acquired)
            with lock(workspace) as acquired_after_release:
                self.assertTrue(acquired_after_release)

    def test_instagram_sync_command_skips_when_workspace_lock_is_held(self) -> None:
        """Catch the CLI bypassing the single-writer lock used by launchd and manual runs."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp) / "graph")
            workspace.ensure_workspace(force=False)
            lock_path = workspace.state_dir / "instagram-sync.lock"
            with lock_path.open("a+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                code, stdout = run_cli(
                    ["--root", str(workspace.root), "instagram-sync", "--no-render"]
                )

            self.assertEqual(code, 0)
            self.assertEqual(json.loads(stdout)["instagramSync"]["status"], "skipped-concurrent")

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

    def test_exact_export_root_ignores_nested_spotlight_duplicates(self) -> None:
        """Catch macOS metadata indexing splitting one export into two import roots."""
        with tempfile.TemporaryDirectory() as tmp:
            export = Path(tmp) / "instagram-jamie-2026-08-25-latest"
            nested = export / "your_instagram_activity"
            (nested / "messages" / "inbox" / "alice_123").mkdir(parents=True)

            with mock.patch(
                "localgraph.automation._indexed_instagram_export_sources",
                return_value=[export.resolve(), nested.resolve()],
            ):
                sources = resolve_instagram_import_sources(export, all_materialized_exports=True)

            self.assertEqual(sources, [export.resolve()])

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

    def test_instagram_sync_launchagent_runs_hourly_and_at_login(self) -> None:
        """Catch a freshness job regressing to a once-daily or login-disabled schedule."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp) / "graph")
            home = Path(tmp) / "home"

            result = install_instagram_sync(
                workspace,
                interval_minutes=60,
                me_name="Jamie",
                me_instagram_names=["jamieburkart"],
                label="com.example.localgraph.instagram-sync",
                home=home,
            )

            plist = plistlib.loads(Path(result["plist"]).read_bytes())
            script = Path(result["script"]).read_text(encoding="utf-8")
            support = home / "Library" / "Application Support" / "Localgraph"
            self.assertEqual(plist["StartInterval"], 3600)
            self.assertTrue(plist["RunAtLoad"])
            self.assertEqual(plist["ProcessType"], "Background")
            self.assertNotIn("StartCalendarInterval", plist)
            self.assertEqual(plist["WorkingDirectory"], str(support))
            self.assertEqual(Path(result["script"]).parent, support / "bin")
            self.assertTrue((support / "runtime" / "localgraph" / "automation.py").exists())
            self.assertIn(str(support / "runtime"), script)
            self.assertIn("instagram-sync", script)
            self.assertNotIn("--instagram-drive-source", script)

    def test_instagram_sync_installer_rejects_a_removable_volume_workspace(self) -> None:
        """Catch launchd jobs being installed where macOS background privacy blocks the workspace."""
        workspace = Workspace(Path("/Volumes/External/Localgraph"))

        with self.assertRaisesRegex(ValueError, "Application Support"):
            install_instagram_sync(workspace, dry_run=True, home=Path("/tmp/localgraph-home"))


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


def configure_test_accounts(workspace: Workspace) -> None:
    for argv in (
        [
            "configure-instagram-account",
            "--account",
            "jamieburkart",
            "--profile-name",
            "jamieburkart",
            "--owner-display-name",
            "Jamie Burkart",
            "--owner-kind",
            "person",
            "--self-name",
            "Jamie",
            "--adopt-legacy",
            "--primary",
        ],
        [
            "configure-instagram-account",
            "--account",
            "nycartc",
            "--profile-name",
            "nycartc",
            "--owner-display-name",
            "NYC Artists' Coalition",
            "--owner-kind",
            "organization",
            "--self-name",
            "nycartc",
            "--reuse-primary-drive",
        ],
    ):
        code, _ = run_cli(["--root", str(workspace.root), *argv])
        if code != 0:
            raise AssertionError(f"failed to configure test Instagram account: {argv}")


def write_instagram_packet(export_root: Path, *, owner: str, correspondent: str, content: str) -> None:
    thread = (
        export_root
        / "your_instagram_activity"
        / "messages"
        / "inbox"
        / "shared-person_123"
    )
    thread.mkdir(parents=True)
    (thread / "message_1.json").write_text(
        json.dumps(
            {
                "participants": [{"name": owner}, {"name": correspondent}],
                "title": correspondent,
                "messages": [
                    {
                        "sender_name": correspondent,
                        "timestamp_ms": 1700000000000,
                        "content": content,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()
