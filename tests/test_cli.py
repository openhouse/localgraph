from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from localgraph.cli import main
from localgraph.instagram_accounts import configure_instagram_account
from localgraph.slug import stable_view_name


class CliTests(unittest.TestCase):
    def test_instagram_accounts_status_applies_standard_export_protocol_to_every_profile(self) -> None:
        """Catch one configured profile drifting from the provider export contract."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace_root = Path(tmp) / "graph"
            from localgraph.paths import Workspace

            workspace = Workspace(workspace_root)
            configure_instagram_account(
                workspace,
                account_key="jamieburkart",
                profile_name="jamieburkart",
                owner_display_name="Jamie Burkart",
                owner_kind="person",
                self_names=["Jamie"],
                adopt_legacy=True,
                primary=True,
            )
            configure_instagram_account(
                workspace,
                account_key="nycartc",
                profile_name="nycartc",
                owner_display_name="NYC Artists Coalition",
                owner_kind="organization",
                self_names=["nycartc"],
                reuse_primary_drive=True,
            )

            code, stdout = run_cli(["--root", str(workspace.root), "instagram-accounts"])

            self.assertEqual(code, 0)
            payload = json.loads(stdout)
            protocols = {
                item["account"]["accountKey"]: item["account"]["requiredProviderExportProtocol"]
                for item in payload["accounts"]
            }
            self.assertEqual(set(protocols), {"jamieburkart", "nycartc"})
            for account_key, protocol in protocols.items():
                self.assertEqual(protocol["destination"], "google-drive")
                self.assertEqual(protocol["information"], ["messages"])
                self.assertEqual(
                    protocol["baseline"],
                    {"cadence": "once", "dateRange": "all-time"},
                )
                self.assertEqual(
                    protocol["recurring"],
                    {"cadence": "daily", "dateRange": "all-time", "durationYears": 3},
                )
                self.assertEqual(protocol["exportNamePrefix"], f"instagram-{account_key}-")

    def test_instagram_accounts_status_lists_profiles_and_health_without_message_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace_root = Path(tmp) / "graph"
            from localgraph.paths import Workspace

            workspace = Workspace(workspace_root)
            configure_instagram_account(
                workspace,
                account_key="jamieburkart",
                profile_name="jamieburkart",
                owner_display_name="Jamie Burkart",
                owner_kind="person",
                self_names=["Jamie"],
                adopt_legacy=True,
                primary=True,
            )
            configure_instagram_account(
                workspace,
                account_key="nycartc",
                profile_name="nycartc",
                owner_display_name="NYC Artists' Coalition",
                owner_kind="organization",
                self_names=["nycartc"],
                reuse_primary_drive=True,
            )
            status_path = workspace.root / "state/instagram-accounts/nycartc/sync-status.json"
            status_path.parent.mkdir(parents=True)
            status_path.write_text('{"status":"pending","messageFiles":0}\n', encoding="utf-8")

            code, stdout = run_cli(["--root", str(workspace.root), "instagram-accounts"])

            self.assertEqual(code, 0)
            payload = json.loads(stdout)
            self.assertEqual(payload["primaryAccountKey"], "jamieburkart")
            self.assertEqual([item["account"]["accountKey"] for item in payload["accounts"]], ["jamieburkart", "nycartc"])
            self.assertEqual(payload["accounts"][1]["sync"]["status"], "pending")
            self.assertNotIn("private body", stdout.lower())

    def test_configure_two_instagram_accounts_adopts_primary_and_scopes_private_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "graph"
            code, _ = run_cli(["--root", str(root), "init"])
            self.assertEqual(code, 0)
            code, _ = run_cli(["--root", str(root), "configure-drive-api", "--folder-id", "drive-root"])
            self.assertEqual(code, 0)

            code, _ = run_cli(
                [
                    "--root",
                    str(root),
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
                ]
            )
            self.assertEqual(code, 0)
            code, _ = run_cli(
                [
                    "--root",
                    str(root),
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
                ]
            )
            self.assertEqual(code, 0)

            config = json.loads((root / "localgraph.config.json").read_text(encoding="utf-8"))
            instagram = config["imports"]["instagram"]
            self.assertEqual(instagram["primaryAccountKey"], "jamieburkart")
            self.assertEqual(set(instagram["accounts"]), {"jamieburkart", "nycartc"})
            jamie = instagram["accounts"]["jamieburkart"]
            nycartc = instagram["accounts"]["nycartc"]
            self.assertEqual(jamie["googleDriveCachePath"], "sources/instagram-drive-cache")
            self.assertEqual(jamie["completedExportsRegistryPath"], "state/instagram-drive-completed-exports.json")
            self.assertEqual(nycartc["googleDriveFolderId"], "drive-root")
            self.assertEqual(nycartc["googleDriveTokenPath"], "state/google-drive-token.json")
            self.assertEqual(nycartc["googleDriveCachePath"], "sources/instagram-accounts/nycartc/drive-cache")
            self.assertEqual(nycartc["syncStatusPath"], "state/instagram-accounts/nycartc/sync-status.json")
            self.assertEqual(nycartc["exportNamePrefix"], "instagram-nycartc-")
            self.assertEqual(nycartc["ownerKind"], "organization")

            code, _ = run_cli(
                [
                    "--root",
                    str(root),
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
                ]
            )
            self.assertEqual(code, 0)
            updated = json.loads((root / "localgraph.config.json").read_text(encoding="utf-8"))
            self.assertEqual(
                updated["imports"]["instagram"]["accounts"]["jamieburkart"]["ownerIdentityKey"],
                "person:self",
            )

    def test_init_accepts_repository_eval_and_script_entries(self) -> None:
        """Catch repository-owned eval tooling making the project root fail workspace validation."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "graph"
            root.mkdir()
            for directory in ("docs", "src", "tests", "evals", "scripts"):
                (root / directory).mkdir()
            for file_name in ("README.md", "pyproject.toml", "Makefile"):
                (root / file_name).write_text("fixture\n", encoding="utf-8")

            code, stdout = run_cli(["--root", str(root), "init"])

            self.assertEqual(code, 0)
            payload = json.loads(stdout)
            self.assertTrue(Path(payload["database"]).exists())

    def test_plan_reports_private_and_view_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "graph"
            code, stdout = run_cli(["--root", str(root), "plan"])

            self.assertEqual(code, 0)
            payload = json.loads(stdout)
            self.assertEqual(payload["root"], str(root.resolve()))
            self.assertIn({"name": "sources", "path": str(root.resolve() / "sources")}, payload["privateDirectories"])
            self.assertIn({"name": "people", "path": str(root.resolve() / "views" / "people")}, payload["viewDirectories"])

    def test_init_doctor_and_render_empty_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "graph"

            code, _ = run_cli(["--root", str(root), "init"])
            self.assertEqual(code, 0)
            self.assertTrue((root / "state" / "localgraph.sqlite").exists())
            self.assertTrue((root / "localgraph.config.json").exists())
            self.assertTrue((root / "PRIVATE-DATA-README.md").exists())
            self.assertTrue((root / "views" / "people").is_dir())
            self.assertTrue((root / "sources" / "instagram").is_dir())
            self.assertTrue((root / "sources" / "imessage").is_dir())

            code, _ = run_cli(["--root", str(root), "doctor"])
            self.assertEqual(code, 0)
            code, _ = run_cli(["--root", str(root), "render"])
            self.assertEqual(code, 0)
            self.assertIn("People: 0", (root / "views" / "index.md").read_text(encoding="utf-8"))
            manifest = json.loads((root / "views" / "_system" / "source-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["source"]["totalMessageFiles"], 0)

    def test_render_creates_person_group_and_thread_views(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "graph"
            code, _ = run_cli(["--root", str(root), "init"])
            self.assertEqual(code, 0)
            db_path = root / "state" / "localgraph.sqlite"

            with contextlib.closing(sqlite3.connect(db_path)) as db:
                db.execute(
                    "INSERT INTO identities (stable_key, display_name, kind) VALUES (?, ?, ?)",
                    ("ig:alice", "Alice Example", "person"),
                )
                db.execute(
                    "INSERT INTO identities (stable_key, display_name, kind) VALUES (?, ?, ?)",
                    ("ig:group:residency", "Residency Planning", "group"),
                )
                db.execute(
                    "INSERT INTO threads (source_kind, source_thread_key, title, thread_kind) VALUES (?, ?, ?, ?)",
                    ("instagram", "messages/inbox/alice_123", "Alice Example", "direct"),
                )
                db.commit()

            code, _ = run_cli(["--root", str(root), "render"])
            self.assertEqual(code, 0)
            self.assertTrue((root / "views" / "people" / stable_view_name("Alice Example", "ig:alice") / "index.md").exists())
            self.assertTrue((root / "views" / "groups" / stable_view_name("Residency Planning", "ig:group:residency") / "index.md").exists())
            self.assertTrue((root / "views" / "threads" / "instagram" / stable_view_name("Alice Example", "messages/inbox/alice_123") / "index.md").exists())

    def test_scan_detects_instagram_exports_without_returning_message_bodies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "graph"
            code, _ = run_cli(["--root", str(root), "init"])
            self.assertEqual(code, 0)
            inbox = root / "sources" / "instagram" / "meta-2026" / "instagram-jamieburkart-2026-07-08-6HfoR9UN" / "your_instagram_activity" / "messages" / "inbox" / "alice_123"
            inbox.mkdir(parents=True)
            (inbox / "message_1.json").write_text('{"messages":[{"content":"private body text"}]}', encoding="utf-8")

            code, stdout = run_cli(["--root", str(root), "scan"])
            self.assertEqual(code, 0)
            payload = json.loads(stdout)
            self.assertEqual(payload["totalMessageFiles"], 1)
            self.assertEqual(payload["exports"][0]["name"], "instagram-jamieburkart-2026-07-08-6HfoR9UN")
            self.assertIn("your_instagram_activity/messages/inbox/alice_123", payload["exports"][0]["threadFolders"])
            self.assertNotIn("private body text", stdout)

    def test_view_name_prints_stable_symlink_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "graph"
            code, stdout = run_cli(["--root", str(root), "view-name", "group", "Residency Planning", "instagram:thread:456"])

            self.assertEqual(code, 0)
            payload = json.loads(stdout)
            self.assertEqual(payload["path"], str(root.resolve() / "views" / "groups" / stable_view_name("Residency Planning", "instagram:thread:456")))

    def test_private_directories_are_gitignored(self) -> None:
        gitignore = Path(__file__).resolve().parents[1] / ".gitignore"
        ignored = gitignore.read_text(encoding="utf-8")
        for path in ["/sources/", "/state/", "/objects/", "/views/", "/annotations/", "/exports/", "localgraph.config.json"]:
            self.assertIn(path, ignored)


def run_cli(argv: list[str]) -> tuple[int, str]:
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        code = main(argv)
    return code, stream.getvalue()


if __name__ == "__main__":
    unittest.main()
