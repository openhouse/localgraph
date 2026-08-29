from __future__ import annotations

import contextlib
import io
import json
import plistlib
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from localgraph.cli import main
from localgraph.instagram_accounts import configure_instagram_account
from localgraph.paths import Workspace
from localgraph.schema import connect, initialize_schema


NOW = datetime(2026, 8, 29, 22, 0, tzinfo=timezone.utc)


class SourceHealthTests(unittest.TestCase):
    def test_recorded_instagram_baseline_immediately_advances_status_acceptance(self) -> None:
        """Baseline acceptance cannot leave status stale until another network sync."""
        from localgraph.automation import configure_instagram_baseline
        from localgraph.status import build_localgraph_status

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            workspace = Workspace(base / "graph")
            home = base / "home"
            configure_instagram_account(
                workspace,
                account_key="nycartc",
                profile_name="nycartc",
                owner_display_name="NYC Artists Coalition",
                owner_kind="organization",
                self_names=["nycartc"],
                adopt_legacy=True,
                primary=True,
            )
            export_name = "instagram-nycartc-2026-08-29-baseline"
            export = workspace.sources_dir / "instagram-drive-cache/meta-html" / export_name
            message = export / "your_instagram_activity/messages/inbox/artist_123/message_1.html"
            message.parent.mkdir(parents=True)
            message.write_text(
                '<h1>Example Artist</h1><div class="_a6-g"><h2 class="_a6-h">Example Artist</h2>'
                '<div class="_a6-p">Hello</div><div class="_a6-o">Aug 29, 2026 3:45 PM</div></div>',
                encoding="utf-8",
            )
            registry = workspace.state_dir / "instagram-drive-completed-exports.json"
            registry.write_text(
                json.dumps(
                    {
                        "exports": {
                            "baseline-id": {
                                "status": "completed",
                                "relativePath": f"meta-html/{export_name}",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            (workspace.state_dir / "instagram-sync-status.json").write_text(
                json.dumps(
                    {
                        "status": "current",
                        "lastSuccessfulSyncAt": "2026-08-29T21:30:00Z",
                        "completedExports": 1,
                        "messageFiles": 1,
                        "historyCoverage": "baseline-required",
                    }
                ),
                encoding="utf-8",
            )

            configure_instagram_baseline(workspace, export_name, account_key="nycartc")
            report = build_localgraph_status(
                workspace,
                now=NOW,
                home=home,
                launchctl=lambda _label: (113, "service not found"),
            )

            account = report["sources"]["instagram"]["accounts"][0]
            self.assertEqual(account["historyCoverage"], "complete-through-latest-export")
            self.assertTrue(account["lifecycle"]["complete"])
            self.assertEqual(account["lifecycle"]["stages"]["complete"]["baselineExportName"], export_name)

    def test_status_detects_failed_scheduler_expired_auth_stale_sync_missing_export_and_empty_snapshot(self) -> None:
        """Catch a nominally configured source concealing several independent acceptance failures."""
        from localgraph.status import build_localgraph_status

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            workspace = Workspace(base / "graph")
            home = base / "home"
            configure_instagram_account(
                workspace,
                account_key="example",
                profile_name="example",
                owner_display_name="Example",
                owner_kind="person",
                self_names=["Example"],
                adopt_legacy=True,
                primary=True,
            )
            config = json.loads(workspace.config_path.read_text(encoding="utf-8"))
            account = config["imports"]["instagram"]["accounts"]["example"]
            account["googleDriveFolderId"] = "drive-container"
            workspace.config_path.write_text(f"{json.dumps(config, indent=2)}\n", encoding="utf-8")
            configure_instagram_account(
                workspace,
                account_key="missing",
                profile_name="missing",
                owner_display_name="Missing Export",
                owner_kind="organization",
                self_names=["missing"],
                reuse_primary_drive=True,
            )
            token = workspace.state_dir / "google-drive-token.json"
            token.write_text(
                json.dumps({"access_token": "secret-body", "expires_at": 1_700_000_000}),
                encoding="utf-8",
            )
            status = workspace.state_dir / "instagram-sync-status.json"
            status.write_text(
                json.dumps(
                    {
                        "status": "current",
                        "checkedAt": "2026-08-29T16:00:00Z",
                        "lastSuccessfulSyncAt": "2026-08-29T16:00:00Z",
                        "completedExports": 1,
                        "messageFiles": 0,
                        "historyCoverage": "baseline-required",
                    }
                ),
                encoding="utf-8",
            )
            plist = home / "Library/LaunchAgents/com.openhouse.localgraph.instagram-sync.plist"
            plist.parent.mkdir(parents=True)
            plist.write_bytes(
                plistlib.dumps(
                    {
                        "Label": "com.openhouse.localgraph.instagram-sync",
                        "StartInterval": 3600,
                        "RunAtLoad": True,
                    }
                )
            )

            def launchctl(label: str) -> tuple[int, str]:
                if label.endswith("instagram-sync"):
                    return 0, "state = not running\nruns = 4\nlast exit code = 78\nrun interval = 3600 seconds\n"
                return 113, "service not found"

            report = build_localgraph_status(workspace, now=NOW, home=home, launchctl=launchctl)

            instagram = report["sources"]["instagram"]
            finding_codes = {finding["code"] for finding in instagram["findings"]}
            account_report = instagram["accounts"][0]
            account_codes = {finding["code"] for finding in account_report["findings"]}
            self.assertEqual(instagram["scheduler"]["status"], "failed")
            self.assertIn("launchagent-failed", finding_codes)
            self.assertIn("stale-sync", account_codes)
            self.assertIn("authorization-expired", account_codes)
            self.assertIn("unexpected-empty-snapshot", account_codes)
            missing_account = next(item for item in instagram["accounts"] if item["accountKey"] == "missing")
            self.assertIn("missing-export", {finding["code"] for finding in missing_account["findings"]})
            self.assertFalse(account_report["lifecycle"]["complete"])
            self.assertNotIn("secret-body", json.dumps(report))

    def test_status_lifecycle_keeps_subsequent_packets_current_but_incomplete_without_baseline(self) -> None:
        """Catch incremental packets being mislabeled as all-time history."""
        from localgraph.status import build_localgraph_status, record_lifecycle_stage

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            workspace = Workspace(base / "graph")
            home = base / "home"
            configure_instagram_account(
                workspace,
                account_key="nycartc",
                profile_name="nycartc",
                owner_display_name="NYC Artists Coalition",
                owner_kind="organization",
                self_names=["nycartc"],
                adopt_legacy=True,
                primary=True,
            )
            record_lifecycle_stage(
                workspace,
                source="instagram",
                account="nycartc",
                stage="requested",
                observed_at="2026-08-29T14:00:00Z",
                evidence="provider-activity-record",
            )
            record_lifecycle_stage(
                workspace,
                source="instagram",
                account="nycartc",
                stage="preparing",
                observed_at="2026-08-29T14:05:00Z",
                evidence="provider-activity-record",
            )
            status_path = workspace.state_dir / "instagram-sync-status.json"
            status_path.write_text(
                json.dumps(
                    {
                        "status": "current",
                        "checkedAt": "2026-08-29T21:30:00Z",
                        "lastSuccessfulSyncAt": "2026-08-29T21:30:00Z",
                        "completedExports": 2,
                        "messageFiles": 3,
                        "historyCoverage": "baseline-required",
                    }
                ),
                encoding="utf-8",
            )
            with connect(workspace.database_path) as db:
                initialize_schema(db)
                db.execute(
                    "INSERT INTO source_imports (source_kind, source_identifier, source_path, raw_metadata_json) "
                    "VALUES (?, ?, ?, ?)",
                    ("instagram", "instagram:nycartc:packet-2", "/private/packet-2", "{}"),
                )
                db.execute(
                    "INSERT INTO threads (source_kind, source_thread_key, title, thread_kind) VALUES (?, ?, ?, ?)",
                    ("instagram", "nycartc:messages/inbox/example", "Example", "direct"),
                )
                db.commit()
            rendered = workspace.views_dir / "instagram-accounts/nycartc"
            rendered.mkdir(parents=True)
            (rendered / "index.md").write_text("# body-free fixture\n", encoding="utf-8")
            plist = home / "Library/LaunchAgents/com.openhouse.localgraph.instagram-sync.plist"
            plist.parent.mkdir(parents=True)
            plist.write_bytes(plistlib.dumps({"Label": "com.openhouse.localgraph.instagram-sync", "StartInterval": 3600}))

            report = build_localgraph_status(
                workspace,
                now=NOW,
                home=home,
                launchctl=lambda _label: (0, "runs = 2\nlast exit code = 0\nrun interval = 3600 seconds\n"),
            )

            account = report["sources"]["instagram"]["accounts"][0]
            lifecycle = account["lifecycle"]
            self.assertEqual(lifecycle["currentStage"], "current")
            self.assertTrue(lifecycle["current"])
            self.assertFalse(lifecycle["complete"])
            self.assertEqual(lifecycle["stages"]["requested"]["observedAt"], "2026-08-29T14:00:00Z")
            self.assertEqual(lifecycle["stages"]["delivered"]["packetCount"], 2)
            self.assertEqual(lifecycle["stages"]["imported"]["importCount"], 1)
            self.assertEqual(account["historyCoverage"], "baseline-required")
            self.assertIn(
                "historical-completeness-not-established",
                {finding["code"] for finding in account["findings"]},
            )

    def test_status_cli_lists_every_source_and_account_without_correspondence_bodies(self) -> None:
        """Catch a new source bypassing the one body-free operator entry point."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp) / "graph")
            configure_instagram_account(
                workspace,
                account_key="example",
                profile_name="example",
                owner_display_name="Example",
                owner_kind="person",
                self_names=["Example"],
                adopt_legacy=True,
                primary=True,
            )
            workspace.imessage_sync_status_path.write_text(
                json.dumps({"status": "blocked", "lastError": "private body marker"}),
                encoding="utf-8",
            )

            code, stdout = run_cli(["--root", str(workspace.root), "status"])

            self.assertEqual(code, 0)
            payload = json.loads(stdout)
            self.assertEqual(set(payload["sources"]), {"instagram", "facebook", "imessage"})
            self.assertEqual(payload["sources"]["instagram"]["accounts"][0]["accountKey"], "example")
            self.assertEqual(payload["sources"]["imessage"]["accounts"][0]["accountKey"], "local-macos-messages")
            self.assertNotIn("private body marker", stdout)


def run_cli(arguments: list[str]) -> tuple[int, str]:
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        code = main(arguments)
    return code, stream.getvalue()


if __name__ == "__main__":
    unittest.main()
