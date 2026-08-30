from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
import sqlite3
import zipfile
import plistlib
import subprocess
import shutil
from datetime import datetime, timezone
from pathlib import Path

from localgraph.cli import main


NOW = datetime(2026, 8, 29, 23, 45, tzinfo=timezone.utc)


class TwitterTests(unittest.TestCase):
    def test_configured_account_is_account_scoped_and_reported_as_export_required(self) -> None:
        """Catch a named X account being omitted from source health or sharing another account's paths."""
        from localgraph.status import build_localgraph_status

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "graph"
            home = base / "home"

            code, stdout = run_cli(
                [
                    "--root",
                    str(root),
                    "configure-twitter-account",
                    "--account",
                    "exampleperson",
                    "--display-name",
                    "Example Person",
                    "--owner-kind",
                    "person",
                    "--self-name",
                    "exampleperson",
                ]
            )

            self.assertEqual(code, 0)
            configured = json.loads(stdout)
            account = configured["account"]
            self.assertEqual(account["accountKey"], "exampleperson")
            self.assertEqual(account["provider"], "x-twitter")
            self.assertEqual(
                account["sourcePath"],
                str(root.resolve() / "sources/twitter-accounts/exampleperson/incoming"),
            )
            self.assertEqual(account["requiredProviderExportProtocol"]["information"], ["account-archive"])
            self.assertEqual(account["requiredProviderExportProtocol"]["localImportInformation"], ["direct-messages"])
            self.assertEqual(account["requiredProviderExportProtocol"]["cadence"], "manual")
            self.assertTrue(Path(account["sourcePath"]).is_dir())

            report = build_localgraph_status(
                workspace=__import__("localgraph.paths", fromlist=["Workspace"]).Workspace(root),
                now=NOW,
                home=home,
                launchctl=lambda _label: (113, "service not found"),
            )

            twitter = report["sources"]["twitter"]
            self.assertEqual(twitter["accountsConfigured"], 1)
            account_status = twitter["accounts"][0]
            self.assertEqual(account_status["accountKey"], "exampleperson")
            self.assertEqual(account_status["syncStatus"], "export-required")
            self.assertEqual(account_status["historyCoverage"], "archive-required")
            self.assertIn("missing-export", {finding["code"] for finding in account_status["findings"]})
            self.assertNotIn("Example Person", json.dumps(report["sources"]["facebook"]))

    def test_sync_imports_direct_messages_from_account_archive_without_echoing_bodies(self) -> None:
        """Catch a valid X archive being counted but not imported into its account-scoped projection."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "graph"
            code, _ = run_cli(
                [
                    "--root",
                    str(root),
                    "configure-twitter-account",
                    "--account",
                    "exampleperson",
                    "--display-name",
                    "Example Person",
                    "--owner-kind",
                    "person",
                    "--self-name",
                    "exampleperson",
                ]
            )
            self.assertEqual(code, 0)
            write_twitter_archive(root, "exampleperson", "100")

            code, stdout = run_cli(["--root", str(root), "twitter-sync", "--no-render"])

            self.assertEqual(code, 0)
            payload = json.loads(stdout)
            sync = payload["twitterAccounts"]["exampleperson"]["sync"]
            self.assertEqual(sync["status"], "local-current")
            self.assertEqual(sync["completedExports"], 1)
            self.assertEqual(sync["messageFiles"], 1)
            self.assertEqual(sync["messages"], 2)
            self.assertEqual(sync["historyCoverage"], "available-direct-messages-through-archive")
            self.assertNotIn("private outbound", stdout)
            self.assertNotIn("private inbound", stdout)
            with contextlib.closing(sqlite3.connect(root / "state/localgraph.sqlite")) as db:
                rows = db.execute(
                    "SELECT t.source_thread_key, m.body_text FROM messages AS m "
                    "JOIN threads AS t ON t.id = m.thread_id WHERE t.source_kind = 'twitter' "
                    "ORDER BY m.sent_at"
                ).fetchall()
            self.assertEqual(rows, [("exampleperson:100-200", "private outbound"), ("exampleperson:100-200", "private inbound")])

    def test_render_creates_account_scoped_twitter_view(self) -> None:
        """Catch imported X threads being rendered only in the global source folder with no account view."""
        from localgraph.paths import Workspace
        from localgraph.status import build_localgraph_status

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "graph"
            run_cli(
                [
                    "--root",
                    str(root),
                    "configure-twitter-account",
                    "--account",
                    "exampleperson",
                    "--display-name",
                    "Example Person",
                    "--owner-kind",
                    "person",
                ]
            )
            write_twitter_archive(root, "exampleperson", "100")

            code, _ = run_cli(["--root", str(root), "twitter-sync"])

            self.assertEqual(code, 0)
            index = root / "views/twitter-accounts/exampleperson/index.md"
            self.assertTrue(index.is_file())
            self.assertIn("Twitter account: @exampleperson", index.read_text(encoding="utf-8"))
            report = build_localgraph_status(
                Workspace(root),
                now=NOW,
                home=base / "home",
                launchctl=lambda _label: (113, "service not found"),
            )
            lifecycle = report["sources"]["twitter"]["accounts"][0]["lifecycle"]
            self.assertTrue(lifecycle["current"])
            self.assertFalse(lifecycle["complete"])

    def test_archive_identity_must_match_the_registered_account(self) -> None:
        """Catch an unrelated account archive being imported under an authorized handle."""
        from localgraph.paths import Workspace
        from localgraph.schema import connect, initialize_schema
        from localgraph.twitter_accounts import configure_twitter_account, twitter_account
        from localgraph.twitter_sync import import_twitter_archive

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "graph"
            workspace = Workspace(root)
            configure_twitter_account(
                workspace,
                account_key="exampleperson",
                display_name="Example Person",
                owner_kind="person",
                self_names=[],
            )
            archive = write_twitter_archive(root, "unrelated", "999")
            with connect(workspace.database_path) as db:
                initialize_schema(db)
                with self.assertRaisesRegex(ValueError, "account mismatch"):
                    import_twitter_archive(db, archive, account=twitter_account(workspace, "exampleperson"))
                self.assertEqual(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)

    def test_group_direct_messages_are_imported_and_counted(self) -> None:
        """Catch the separate group-DM archive file being silently omitted."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "graph"
            run_cli(["--root", str(root), "configure-twitter-account", "--account", "exampleperson", "--display-name", "Example Person", "--owner-kind", "person"])
            archive = write_twitter_archive(root, "exampleperson", "100")
            with zipfile.ZipFile(archive, "a") as bundle:
                bundle.writestr(
                    "data/direct-messages-group.js",
                    'window.YTD.direct_messages_group.part0 = [{"dmConversation":{"conversationId":"group-1",'
                    '"messages":[{"messageCreate":{"id":"g1","senderId":"300",'
                    '"createdAt":"2026-08-28T13:00:00.000Z","text":"private group message"}}]}}]',
                )

            code, stdout = run_cli(["--root", str(root), "twitter-sync", "--no-render"])

            self.assertEqual(code, 0)
            sync = json.loads(stdout)["twitterAccounts"]["exampleperson"]["sync"]
            self.assertEqual(sync["messages"], 3)
            self.assertEqual(sync["messageFiles"], 2)
            with contextlib.closing(sqlite3.connect(root / "state/localgraph.sqlite")) as db:
                row = db.execute("SELECT thread_kind FROM threads WHERE source_kind = 'twitter' AND source_thread_key = 'exampleperson:group-1'").fetchone()
            self.assertEqual(row, ("group",))
            self.assertNotIn("private group message", stdout)

    def test_missing_archive_username_is_not_assumed_to_be_the_registered_account(self) -> None:
        """Catch an unidentified archive borrowing the configured username and bypassing identity checks."""
        from localgraph.twitter_accounts import TwitterAccount
        from localgraph.twitter_sync import _account_identity

        account = TwitterAccount("exampleperson", "Example Person", "person", "person:self", (), Path("incoming"), Path("status"))
        with self.assertRaises(ValueError):
            _account_identity([{"account": {"accountId": "999"}}], account)

    def test_underscore_account_keys_do_not_match_or_erase_other_accounts(self) -> None:
        """Catch SQL wildcard account keys counting or clearing another account's messages."""
        from localgraph.paths import Workspace
        from localgraph.status import _canonical_counts
        from localgraph.twitter_sync import _clear_account_projection
        from localgraph.schema import connect

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "graph"
            for key, own_id in (("example_org", "100"), ("examplexorg", "300")):
                run_cli(["--root", str(root), "configure-twitter-account", "--account", key, "--display-name", key, "--owner-kind", "organization"])
                write_twitter_archive(root, key, own_id)
            code, _ = run_cli(["--root", str(root), "twitter-sync", "--no-render"])
            self.assertEqual(code, 0)
            workspace = Workspace(root)
            self.assertEqual(_canonical_counts(workspace, "twitter", "example_org"), (1, 1))
            with connect(workspace.database_path) as db:
                _clear_account_projection(db, "example_org")
                db.commit()
                rows = db.execute("SELECT source_thread_key FROM threads WHERE source_kind = 'twitter'").fetchall()
                self.assertEqual([row[0] for row in rows], ["examplexorg:300-200"])

    def test_installed_hourly_job_executes_the_account_scoped_sync(self) -> None:
        """Catch a nominal scheduler installation that cannot execute the private runtime."""
        import localgraph.twitter_sync as sync_module
        from localgraph.paths import Workspace
        from localgraph.twitter_accounts import configure_twitter_account

        installer = getattr(sync_module, "install_twitter_sync", None)
        self.assertTrue(callable(installer), "install_twitter_sync is missing")
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            workspace = Workspace(base / "graph")
            configure_twitter_account(workspace, account_key="exampleperson", display_name="Example Person", owner_kind="person", self_names=[])

            installed = installer(workspace, home=base / "home", interval_minutes=60)
            plist = plistlib.loads(Path(installed["plist"]).read_bytes())
            completed = subprocess.run(plist["ProgramArguments"], capture_output=True, text=True, check=False)

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(plist["StartInterval"], 3600)
            self.assertTrue(plist["RunAtLoad"])
            receipt = json.loads((workspace.state_dir / "twitter-accounts/exampleperson/sync-status.json").read_text(encoding="utf-8"))
            self.assertEqual(receipt["status"], "export-required")

    def test_invalid_archive_preserves_last_good_account_and_other_accounts_advance(self) -> None:
        """Catch one invalid archive erasing valid custody or stopping an independent account refresh."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "graph"
            for key, kind in (("exampleperson", "person"), ("exampleorg", "organization")):
                run_cli(["--root", str(root), "configure-twitter-account", "--account", key, "--display-name", key, "--owner-kind", kind])
            good = write_twitter_archive(root, "exampleperson", "100")
            code, first_stdout = run_cli(["--root", str(root), "twitter-sync", "--no-render"])
            self.assertEqual(code, 0)
            last_success = json.loads(first_stdout)["twitterAccounts"]["exampleperson"]["sync"]["lastSuccessfulSyncAt"]
            with zipfile.ZipFile(good, "w") as bundle:
                bundle.writestr("data/account.js", 'window.YTD.account.part0 = [{"account":{"accountId":"999","username":"unrelated"}}]')
                bundle.writestr("data/direct-messages.js", "window.YTD.direct_messages.part0 = []")
            write_twitter_archive(root, "exampleorg", "300")

            code, stdout = run_cli(["--root", str(root), "twitter-sync", "--no-render"])

            self.assertEqual(code, 0)
            payload = json.loads(stdout)
            personal = payload["twitterAccounts"]["exampleperson"]["sync"]
            self.assertEqual(personal["status"], "degraded")
            self.assertEqual(personal["lastSuccessfulSyncAt"], last_success)
            self.assertEqual(personal["lastError"], "archive-validation-failed")
            self.assertEqual(payload["twitterAccounts"]["exampleorg"]["sync"]["status"], "local-current")
            with contextlib.closing(sqlite3.connect(root / "state/localgraph.sqlite")) as db:
                rows = db.execute("SELECT source_thread_key FROM threads WHERE source_kind = 'twitter' ORDER BY source_thread_key").fetchall()
            self.assertEqual(rows, [("exampleorg:300-200",), ("exampleperson:100-200",)])
            self.assertNotIn("private outbound", stdout)

    def test_overlapping_archives_report_unique_canonical_message_counts(self) -> None:
        """Catch cumulative archives doubling message and thread counts in source health."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "graph"
            run_cli(["--root", str(root), "configure-twitter-account", "--account", "exampleperson", "--display-name", "Example Person", "--owner-kind", "person"])
            archive = write_twitter_archive(root, "exampleperson", "100")
            shutil.copyfile(archive, archive.with_name("twitter-exampleperson-2026-08-30.zip"))

            code, stdout = run_cli(["--root", str(root), "twitter-sync", "--no-render"])

            self.assertEqual(code, 0)
            sync = json.loads(stdout)["twitterAccounts"]["exampleperson"]["sync"]
            self.assertEqual(sync["completedExports"], 2)
            self.assertEqual(sync["messages"], 2)
            self.assertEqual(sync["threads"], 1)

    def test_twitter_requested_stage_does_not_imply_archive_delivery(self) -> None:
        """Catch a provider archive request being reported as a delivered or complete archive."""
        from localgraph.paths import Workspace
        from localgraph.status import build_localgraph_status, record_lifecycle_stage
        from localgraph.twitter_accounts import configure_twitter_account

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            workspace = Workspace(base / "graph")
            configure_twitter_account(workspace, account_key="exampleperson", display_name="Example Person", owner_kind="person", self_names=[])
            try:
                record_lifecycle_stage(workspace, source="twitter", account="exampleperson", stage="requested", observed_at="2026-08-29T23:30:00Z", evidence="operator-observed-provider-ui")
            except ValueError as exc:
                self.fail(str(exc))
            report = build_localgraph_status(workspace, now=NOW, home=base / "home", launchctl=lambda _label: (113, "service not found"))
            lifecycle = report["sources"]["twitter"]["accounts"][0]["lifecycle"]
            self.assertEqual(lifecycle["currentStage"], "requested")
            self.assertEqual(lifecycle["stages"]["delivered"]["status"], "pending")
            self.assertFalse(lifecycle["complete"])


def run_cli(arguments: list[str]) -> tuple[int, str]:
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        try:
            code = main(arguments)
        except SystemExit as exc:
            raise AssertionError(f"Localgraph rejected the requested command: {arguments[2]}") from exc
    return code, stream.getvalue()


def write_twitter_archive(root: Path, account: str, own_id: str) -> Path:
    archive = root / f"sources/twitter-accounts/{account}/incoming/twitter-{account}-2026-08-29.zip"
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(
            "data/account.js",
            f'window.YTD.account.part0 = [{{"account":{{"accountId":"{own_id}","username":"{account}",'
            f'"accountDisplayName":"Example Person"}}}}]',
        )
        bundle.writestr(
            "data/direct-messages.js",
            f'window.YTD.direct_messages.part0 = [{{"dmConversation":{{"conversationId":"{own_id}-200",'
            f'"messages":[{{"messageCreate":{{"id":"m1","senderId":"{own_id}","recipientId":"200",'
            '"createdAt":"2026-08-28T12:00:00.000Z","text":"private outbound"}},'
            f'{{"messageCreate":{{"id":"m2","senderId":"200","recipientId":"{own_id}",'
            '"createdAt":"2026-08-28T12:01:00.000Z","text":"private inbound"}}]}}]',
        )
    return archive


if __name__ == "__main__":
    unittest.main()
