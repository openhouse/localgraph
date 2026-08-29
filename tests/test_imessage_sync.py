from __future__ import annotations

import contextlib
import fcntl
import gc
import hashlib
import importlib
import io
import json
import plistlib
import sqlite3
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest import mock

from localgraph.automation import instagram_sync_lock
from localgraph.cli import main
from localgraph.paths import Workspace
from test_ingest import create_imessage_fixture


class IMessageSyncTests(unittest.TestCase):
    def test_snapshot_reports_actionable_full_disk_access_failure(self) -> None:
        """Catch launchd privacy denial surfacing as an opaque SQLite error."""
        sync = self._sync_module()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            live_db = base / "Messages" / "chat.db"
            destination = base / "snapshot" / "chat.db"
            live_db.parent.mkdir(parents=True)
            create_imessage_fixture(live_db)

            with mock.patch.object(
                sync.sqlite3,
                "connect",
                side_effect=sqlite3.OperationalError("unable to open database file"),
            ):
                with self.assertRaisesRegex(PermissionError, "Full Disk Access") as raised:
                    sync.snapshot_imessage_database(live_db, destination)

            message = str(raised.exception)
            self.assertIn(str(live_db), message)
            self.assertIn(sync.sys.executable, message)
            self.assertFalse(destination.exists())

    def test_snapshot_closes_every_sqlite_connection_without_resource_warnings(self) -> None:
        """Catch hourly snapshots leaking database descriptors until launchd runs fail."""
        sync = self._sync_module()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            live_db = base / "Messages" / "chat.db"
            destination = base / "snapshot" / "chat.db"
            live_db.parent.mkdir(parents=True)
            create_imessage_fixture(live_db)

            with warnings.catch_warnings(record=True) as captured:
                warnings.simplefilter("always", ResourceWarning)
                sync.snapshot_imessage_database(live_db, destination)
                gc.collect()

            resource_warnings = [item for item in captured if issubclass(item.category, ResourceWarning)]
            self.assertEqual(resource_warnings, [])

    def test_sync_snapshots_uncheckpointed_wal_messages_and_reports_body_free_freshness(self) -> None:
        """Catch a live SQLite copy ignoring messages that exist only in chat.db-wal."""
        sync = self._sync_module()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            workspace = Workspace(base / "graph")
            live_db = base / "Messages" / "chat.db"
            live_db.parent.mkdir(parents=True)
            create_imessage_fixture(live_db)

            with contextlib.closing(sqlite3.connect(live_db)) as writer:
                writer.execute("PRAGMA journal_mode = WAL")
                writer.execute("PRAGMA wal_autocheckpoint = 0")
                writer.execute(
                    "INSERT INTO message (ROWID, guid, text, attributedBody, date, is_from_me, handle_id, service) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (4, "msg-wal", "private wal body", None, 700_000_003_000_000_000, 0, 1, "iMessage"),
                )
                writer.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (?, ?)", (1, 4))
                writer.commit()

                summary = sync.run_imessage_sync(
                    workspace,
                    live_db_path=live_db,
                    me_name="Jamie",
                    me_handles=["jamie@example.com"],
                    render=False,
                )

                self.assertTrue(live_db.with_name("chat.db-wal").exists())
                self.assertEqual(summary["imessageSync"]["status"], "current")
                self.assertEqual(summary["imessageSync"]["messages"], 4)
                self.assertEqual(summary["imessageSync"]["historyCoverage"], "complete-through-snapshot")
                self.assertEqual(summary["imessageSync"]["checkIntervalMinutes"], 60)
                self.assertEqual(summary["snapshot"]["method"], "sqlite-online-backup")
                self.assertEqual(
                    summary["result"]["totals"],
                    {
                        "imports": 1,
                        "threads": 2,
                        "groups": 1,
                        "accounts": 3,
                        "messages": 4,
                        "media": 1,
                    },
                )
                self.assertNotIn("private wal body", json.dumps(summary))

            snapshot = workspace.imessage_chat_db_path
            self.assertEqual(snapshot.stat().st_mode & 0o777, 0o600)
            with contextlib.closing(sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)) as copied:
                self.assertEqual(copied.execute("SELECT COUNT(*) FROM message").fetchone()[0], 4)

    def test_sync_rebuilds_imessage_projection_so_source_deletions_propagate(self) -> None:
        """Catch scheduled refreshes only appending and leaving deleted source messages behind."""
        sync = self._sync_module()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            workspace = Workspace(base / "graph")
            live_db = base / "Messages" / "chat.db"
            live_db.parent.mkdir(parents=True)
            create_imessage_fixture(live_db)

            first = sync.run_imessage_sync(workspace, live_db_path=live_db, render=False)
            self.assertEqual(first["imessageSync"]["messages"], 3)

            with contextlib.closing(sqlite3.connect(live_db)) as source:
                source.execute("DELETE FROM chat_message_join WHERE message_id = 3")
                source.execute("DELETE FROM message WHERE ROWID = 3")
                source.commit()

            second = sync.run_imessage_sync(workspace, live_db_path=live_db, render=False)

            self.assertEqual(second["projectionReplacement"]["messages"], 3)
            self.assertEqual(second["imessageSync"]["messages"], 2)
            with contextlib.closing(sqlite3.connect(workspace.database_path)) as canonical:
                count = canonical.execute(
                    "SELECT COUNT(*) FROM messages JOIN threads ON threads.id = messages.thread_id "
                    "WHERE threads.source_kind = 'imessage'"
                ).fetchone()[0]
                stale_identities = canonical.execute(
                    "SELECT COUNT(*) FROM identities WHERE stable_key = 'person:imessage:bob@example.com' "
                    "OR stable_key GLOB 'group:imessage:*'"
                ).fetchone()[0]
            self.assertEqual(count, 2)
            self.assertEqual(stale_identities, 0)

    def test_failed_snapshot_preserves_last_known_good_snapshot_projection_and_success_time(self) -> None:
        """Catch a missing or privacy-blocked live database erasing verified local custody."""
        sync = self._sync_module()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            workspace = Workspace(base / "graph")
            live_db = base / "Messages" / "chat.db"
            live_db.parent.mkdir(parents=True)
            create_imessage_fixture(live_db)
            first = sync.run_imessage_sync(workspace, live_db_path=live_db, render=False)
            first_success = first["imessageSync"]["lastSuccessfulSyncAt"]
            snapshot_hash = hashlib.sha256(workspace.imessage_chat_db_path.read_bytes()).hexdigest()

            with self.assertRaises(FileNotFoundError):
                sync.run_imessage_sync(
                    workspace,
                    live_db_path=base / "Missing" / "chat.db",
                    render=False,
                )

            status = json.loads(workspace.imessage_sync_status_path.read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "degraded")
            self.assertEqual(status["lastSuccessfulSyncAt"], first_success)
            self.assertIsNotNone(status["lastError"])
            self.assertEqual(hashlib.sha256(workspace.imessage_chat_db_path.read_bytes()).hexdigest(), snapshot_hash)
            with contextlib.closing(sqlite3.connect(workspace.database_path)) as canonical:
                count = canonical.execute(
                    "SELECT COUNT(*) FROM messages JOIN threads ON threads.id = messages.thread_id "
                    "WHERE threads.source_kind = 'imessage'"
                ).fetchone()[0]
            self.assertEqual(count, 3)

    def test_empty_candidate_restores_last_known_good_snapshot_and_projection(self) -> None:
        """Catch a valid-schema but empty live snapshot replacing populated local custody."""
        sync = self._sync_module()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            workspace = Workspace(base / "graph")
            live_db = base / "Messages" / "chat.db"
            live_db.parent.mkdir(parents=True)
            create_imessage_fixture(live_db)
            sync.run_imessage_sync(workspace, live_db_path=live_db, render=False)
            snapshot_hash = hashlib.sha256(workspace.imessage_chat_db_path.read_bytes()).hexdigest()

            with contextlib.closing(sqlite3.connect(live_db)) as source:
                source.execute("DELETE FROM chat_message_join")
                source.execute("DELETE FROM message")
                source.commit()

            with self.assertRaisesRegex(ValueError, "empty snapshot"):
                sync.run_imessage_sync(workspace, live_db_path=live_db, render=False)

            self.assertEqual(hashlib.sha256(workspace.imessage_chat_db_path.read_bytes()).hexdigest(), snapshot_hash)
            with contextlib.closing(sqlite3.connect(workspace.database_path)) as canonical:
                count = canonical.execute(
                    "SELECT COUNT(*) FROM messages JOIN threads ON threads.id = messages.thread_id "
                    "WHERE threads.source_kind = 'imessage'"
                ).fetchone()[0]
            self.assertEqual(count, 3)

    def test_render_failure_rolls_back_candidate_projection_and_snapshot(self) -> None:
        """Catch a failed directory render leaving canonical state ahead of restored custody."""
        sync = self._sync_module()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            workspace = Workspace(base / "graph")
            live_db = base / "Messages" / "chat.db"
            live_db.parent.mkdir(parents=True)
            create_imessage_fixture(live_db)
            first = sync.run_imessage_sync(workspace, live_db_path=live_db, render=False)
            first_success = first["imessageSync"]["lastSuccessfulSyncAt"]
            snapshot_hash = hashlib.sha256(workspace.imessage_chat_db_path.read_bytes()).hexdigest()

            with contextlib.closing(sqlite3.connect(live_db)) as source:
                source.execute(
                    "INSERT INTO message (ROWID, guid, text, attributedBody, date, is_from_me, handle_id, service) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (4, "msg-render", "candidate body", None, 700_000_003_000_000_000, 0, 1, "iMessage"),
                )
                source.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (?, ?)", (1, 4))
                source.commit()

            with mock.patch.object(sync, "render_views", side_effect=RuntimeError("render failed")):
                with self.assertRaisesRegex(ValueError, "render failed"):
                    sync.run_imessage_sync(workspace, live_db_path=live_db, render=True)

            status = json.loads(workspace.imessage_sync_status_path.read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "degraded")
            self.assertEqual(status["lastSuccessfulSyncAt"], first_success)
            self.assertEqual(hashlib.sha256(workspace.imessage_chat_db_path.read_bytes()).hexdigest(), snapshot_hash)
            with contextlib.closing(sqlite3.connect(workspace.database_path)) as canonical:
                count = canonical.execute(
                    "SELECT COUNT(*) FROM messages JOIN threads ON threads.id = messages.thread_id "
                    "WHERE threads.source_kind = 'imessage'"
                ).fetchone()[0]
            self.assertEqual(count, 3)

    def test_imessage_sync_command_uses_the_shared_workspace_writer_lock(self) -> None:
        """Catch manual and scheduled iMessage refreshes racing Instagram or Facebook writers."""
        self._sync_module()
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp) / "graph")
            workspace.ensure_workspace(force=False)

            with instagram_sync_lock(workspace) as acquired:
                self.assertTrue(acquired)
                code, stdout, _ = run_cli(
                    ["--root", str(workspace.root), "imessage-sync", "--no-render"]
                )

            self.assertEqual(code, 0)
            payload = json.loads(stdout)
            self.assertEqual(payload["imessageSync"]["status"], "skipped-concurrent")

    def test_imessage_sync_launchagent_runs_hourly_at_login_from_application_support(self) -> None:
        """Catch iMessage freshness regressing to daily checks or a removable-volume runtime."""
        sync = self._sync_module()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            workspace = Workspace(base / "graph")
            home = base / "home"

            result = sync.install_imessage_sync(
                workspace,
                interval_minutes=60,
                label="com.example.localgraph.imessage-sync",
                home=home,
            )

            plist = plistlib.loads(Path(result["plist"]).read_bytes())
            script = Path(result["script"])
            script_text = script.read_text(encoding="utf-8")
            support = home / "Library" / "Application Support" / "Localgraph"
            self.assertEqual(plist["StartInterval"], 3600)
            self.assertTrue(plist["RunAtLoad"])
            self.assertEqual(plist["ProcessType"], "Background")
            self.assertEqual(plist["WorkingDirectory"], str(support))
            self.assertEqual(plist["StandardOutPath"], str(support / "logs" / "imessage-sync.stdout.log"))
            self.assertEqual(plist["StandardErrorPath"], str(support / "logs" / "imessage-sync.stderr.log"))
            self.assertIn("imessage-sync", script_text)
            self.assertNotIn("--me-imessage", script_text)
            self.assertTrue((support / "runtime" / "localgraph" / "imessage_sync.py").exists())
            self.assertEqual(script.stat().st_mode & 0o777, 0o700)

    def test_imessage_status_reports_counts_and_failure_state_without_message_content(self) -> None:
        """Catch operator health output leaking private message bodies or hiding source failure."""
        sync = self._sync_module()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            workspace = Workspace(base / "graph")
            live_db = base / "Messages" / "chat.db"
            live_db.parent.mkdir(parents=True)
            create_imessage_fixture(live_db)
            sync.run_imessage_sync(workspace, live_db_path=live_db, render=False)

            status = sync.imessage_status(workspace)
            serialized = json.dumps(status)

            self.assertEqual(status["sync"]["status"], "current")
            self.assertEqual(status["sync"]["threads"], 2)
            self.assertEqual(status["sync"]["messages"], 3)
            self.assertIn("nextCheckWithinMinutes", status["freshness"])
            self.assertNotIn("hi from imessage", serialized)
            self.assertNotIn("reply from self", serialized)
            self.assertEqual(workspace.imessage_sync_status_path.stat().st_mode & 0o777, 0o600)

    def _sync_module(self):
        try:
            return importlib.import_module("localgraph.imessage_sync")
        except ModuleNotFoundError:
            self.fail("localgraph.imessage_sync is missing")


def run_cli(argv: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


if __name__ == "__main__":
    unittest.main()
