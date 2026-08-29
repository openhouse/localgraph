from __future__ import annotations

import contextlib
import io
import json
import plistlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from localgraph.cli import main


class FacebookTests(unittest.TestCase):
    def test_scan_facebook_source_finds_message_categories_without_reading_bodies(self) -> None:
        from localgraph.facebook import scan_facebook_source

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "facebook-example-person-2026-08-29-export"
            for category in ("inbox", "archived_threads", "message_requests"):
                thread = source / "your_facebook_activity" / "messages" / category / f"shared_{category}"
                thread.mkdir(parents=True)
                (thread / "message_1.json").write_text(
                    json.dumps(
                        {
                            "participants": [{"name": "Example Person"}, {"name": "Shared Person"}],
                            "messages": [{"sender_name": "Shared Person", "content": "private body"}],
                        }
                    ),
                    encoding="utf-8",
                )

            result = scan_facebook_source(source)

            self.assertEqual(result["sourceKind"], "facebook")
            self.assertEqual(result["totalMessageFiles"], 3)
            folders = result["exports"][0]["threadFolders"]
            self.assertEqual(
                folders,
                [
                    "your_facebook_activity/messages/archived_threads/shared_archived_threads",
                    "your_facebook_activity/messages/inbox/shared_inbox",
                    "your_facebook_activity/messages/message_requests/shared_message_requests",
                ],
            )
            self.assertNotIn("private body", json.dumps(result))

    def test_two_facebook_accounts_keep_threads_and_owner_identities_distinct(self) -> None:
        from localgraph.ingest import import_facebook_source
        from localgraph.schema import connect, initialize_schema

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exports = {
                "example-person": ("Example Person", "person", "person:self", "personal message"),
                "example-page": (
                    "Example Page",
                    "organization",
                    "organization:facebook:example-page",
                    "page message",
                ),
            }
            with connect(root / "localgraph.sqlite") as db:
                initialize_schema(db)
                for account_key, (owner, owner_kind, identity_key, content) in exports.items():
                    source = root / f"facebook-{account_key}-2026-08-29-export"
                    thread = source / "your_facebook_activity" / "messages" / "inbox" / "shared_123"
                    thread.mkdir(parents=True)
                    (thread / "message_1.json").write_text(
                        json.dumps(
                            {
                                "participants": [{"name": owner}, {"name": "Shared Person"}],
                                "title": "Shared Person",
                                "messages": [
                                    {
                                        "sender_name": owner,
                                        "timestamp_ms": 1700000000000,
                                        "content": content,
                                    }
                                ],
                            }
                        ),
                        encoding="utf-8",
                    )
                    result = import_facebook_source(
                        db,
                        source,
                        account_key=account_key,
                        owner_display_name=owner,
                        owner_kind=owner_kind,
                        owner_identity_key=identity_key,
                        self_names=[owner],
                    )
                    self.assertEqual(result.messages, 1)

                thread_keys = [
                    row[0]
                    for row in db.execute(
                        "SELECT source_thread_key FROM threads WHERE source_kind = 'facebook' ORDER BY source_thread_key"
                    )
                ]
                owners = {
                    row[0]
                    for row in db.execute(
                        "SELECT stable_key FROM identities WHERE stable_key IN "
                        "('person:self', 'organization:facebook:example-page')"
                    )
                }
                messages = db.execute(
                    "SELECT COUNT(*) FROM messages JOIN threads ON threads.id = messages.thread_id "
                    "WHERE threads.source_kind = 'facebook'"
                ).fetchone()[0]

            self.assertEqual(
                thread_keys,
                [
                    "example-page:your_facebook_activity/messages/inbox/shared_123",
                    "example-person:your_facebook_activity/messages/inbox/shared_123",
                ],
            )
            self.assertEqual(owners, {"person:self", "organization:facebook:example-page"})
            self.assertEqual(messages, 2)

    def test_facebook_registry_reports_profile_and_page_protocols_without_private_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "graph"
            code, _ = run_cli(["--root", str(root), "init"])
            self.assertEqual(code, 0)
            profile = [
                "--root",
                str(root),
                "configure-facebook-account",
                "--account",
                "example-person",
                "--display-name",
                "Example Person",
                "--account-type",
                "profile",
                "--self-name",
                "Example Person",
            ]
            page = [
                "--root",
                str(root),
                "configure-facebook-account",
                "--account",
                "example-page",
                "--display-name",
                "Example Page",
                "--account-type",
                "page",
                "--provider-state",
                "active",
                "--self-name",
                "Example Page",
            ]
            self.assertEqual(run_cli(profile)[0], 0)
            self.assertEqual(run_cli(page)[0], 0)

            code, stdout = run_cli(["--root", str(root), "facebook-accounts"])

            self.assertEqual(code, 0)
            payload = json.loads(stdout)
            accounts = {item["account"]["accountKey"]: item["account"] for item in payload["accounts"]}
            self.assertEqual(set(accounts), {"example-person", "example-page"})
            self.assertEqual(accounts["example-person"]["ownerIdentityKey"], "person:self")
            self.assertEqual(
                accounts["example-person"]["requiredProviderExportProtocol"]["recurring"],
                {
                    "cadence": "daily",
                    "dateRange": "all-time",
                    "durationYears": 3,
                    "support": "verified-in-accounts-center",
                },
            )
            self.assertEqual(accounts["example-page"]["ownerKind"], "organization")
            self.assertEqual(
                accounts["example-page"]["requiredProviderExportProtocol"]["providerSurface"],
                "facebook-page-settings",
            )
            self.assertEqual(
                accounts["example-page"]["requiredProviderExportProtocol"]["recurring"]["support"],
                "provider-verification-required",
            )
            self.assertNotIn("providerId", stdout)
            self.assertNotIn("private body", stdout.lower())

    def test_facebook_sync_updates_ready_accounts_without_erasing_pending_pages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "graph"
            self.assertEqual(run_cli(["--root", str(root), "init"])[0], 0)
            configure_facebook_fixture(root, "example-person", "Example Person", "profile")
            configure_facebook_fixture(root, "example-page", "Example Page", "page")
            write_facebook_packet(
                root / "sources/facebook-accounts/example-person/incoming/facebook-example-person-2026-08-29-export",
                owner="Example Person",
                content="personal message",
            )

            code, stdout = run_cli(["--root", str(root), "facebook-sync"])

            self.assertEqual(code, 0)
            first = json.loads(stdout)
            self.assertEqual(first["facebookSync"]["status"], "pending")
            self.assertEqual(first["facebookAccounts"]["example-person"]["sync"]["status"], "local-current")
            self.assertEqual(first["facebookAccounts"]["example-page"]["sync"]["status"], "pending")
            self.assertEqual(first["result"]["totals"]["messages"], 1)
            with contextlib.closing(sqlite3.connect(root / "state/localgraph.sqlite")) as db:
                self.assertEqual(
                    db.execute(
                        "SELECT COUNT(*) FROM messages JOIN threads ON threads.id = messages.thread_id "
                        "WHERE threads.source_kind = 'facebook'"
                    ).fetchone()[0],
                    1,
                )

            write_facebook_packet(
                root / "sources/facebook-accounts/example-page/incoming/facebook-example-page-2026-08-29-export",
                owner="Example Page",
                content="page message",
            )
            code, stdout = run_cli(["--root", str(root), "facebook-sync"])

            self.assertEqual(code, 0)
            second = json.loads(stdout)
            self.assertEqual(second["facebookSync"]["status"], "current")
            self.assertEqual(second["result"]["totals"]["messages"], 2)
            self.assertTrue((root / "views/facebook-accounts/example-person/threads").is_dir())
            self.assertTrue((root / "views/facebook-accounts/example-page/threads").is_dir())

    def test_facebook_sync_launchagent_runs_hourly_from_private_registry(self) -> None:
        from localgraph.facebook_sync import install_facebook_sync
        from localgraph.paths import Workspace

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp) / "graph")
            home = Path(tmp) / "home"
            configure_facebook_fixture(workspace.root, "example-person", "Example Person", "profile")

            result = install_facebook_sync(
                workspace,
                interval_minutes=60,
                label="com.example.localgraph.facebook-sync",
                home=home,
            )

            plist = plistlib.loads(Path(result["plist"]).read_bytes())
            script = Path(result["script"]).read_text(encoding="utf-8")
            self.assertEqual(plist["StartInterval"], 3600)
            self.assertTrue(plist["RunAtLoad"])
            self.assertIn("facebook-sync", script)
            self.assertNotIn("example-person", script)
            self.assertTrue((home / "Library/Application Support/Localgraph/runtime/localgraph/facebook_sync.py").exists())

    def test_configured_facebook_pull_uses_messages_only_scoped_paths_and_prefix(self) -> None:
        from localgraph.drive import pull_configured_facebook_source
        from localgraph.facebook_accounts import configure_facebook_account
        from localgraph.instagram_accounts import configure_instagram_account
        from localgraph.paths import Workspace

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp) / "graph")
            configure_instagram_account(
                workspace,
                account_key="example-instagram",
                profile_name="example-instagram",
                owner_display_name="Example Person",
                owner_kind="person",
                self_names=["Example Person"],
                adopt_legacy=True,
                primary=True,
            )
            config = json.loads(workspace.config_path.read_text(encoding="utf-8"))
            instagram = config["imports"]["instagram"]
            instagram["accounts"]["example-instagram"]["googleDriveFolderId"] = "drive-root"
            workspace.config_path.write_text(f"{json.dumps(config, indent=2)}\n", encoding="utf-8")
            configure_facebook_account(
                workspace,
                account_key="example-person",
                display_name="Example Person",
                account_type="profile",
                provider_state="active",
                self_names=["Example Person"],
                reuse_instagram_drive=True,
            )

            sentinel = object()
            with mock.patch("localgraph.drive.pull_latest_facebook_export", return_value=sentinel) as pull:
                result = pull_configured_facebook_source(workspace, account_key="example-person")

            self.assertIs(result, sentinel)
            kwargs = pull.call_args.kwargs
            self.assertEqual(kwargs["container_folder_id"], "drive-root")
            self.assertEqual(kwargs["export_name_prefix"], "facebook-example-person-")
            self.assertEqual(kwargs["cache_dir"], workspace.root / "sources/facebook-accounts/example-person/drive-cache")
            self.assertEqual(
                kwargs["allowed_relative_roots"],
                (Path("your_facebook_activity/messages"), Path("messages")),
            )

    def test_facebook_sync_pulls_authenticated_drive_before_import(self) -> None:
        from localgraph.drive import DrivePullResult
        from localgraph.facebook_accounts import configure_facebook_account
        from localgraph.instagram_accounts import configure_instagram_account
        from localgraph.paths import Workspace

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp) / "graph")
            configure_instagram_account(
                workspace,
                account_key="example-instagram",
                profile_name="example-instagram",
                owner_display_name="Example Person",
                owner_kind="person",
                self_names=["Example Person"],
                adopt_legacy=True,
                primary=True,
            )
            config = json.loads(workspace.config_path.read_text(encoding="utf-8"))
            config["imports"]["instagram"]["accounts"]["example-instagram"]["googleDriveFolderId"] = "drive-root"
            workspace.config_path.write_text(f"{json.dumps(config, indent=2)}\n", encoding="utf-8")
            configure_facebook_account(
                workspace,
                account_key="example-person",
                display_name="Example Person",
                account_type="profile",
                provider_state="active",
                self_names=["Example Person"],
                reuse_instagram_drive=True,
            )
            cache = workspace.root / "sources/facebook-accounts/example-person/drive-cache"
            export = cache / "facebook-example-person-2026-08-29-export"

            def materialize(_workspace: Workspace, *, account_key: str):
                self.assertEqual(account_key, "example-person")
                write_facebook_packet(export, owner="Example Person", content="drive message")
                return DrivePullResult(
                    folder_id="export-id",
                    cache_path=export,
                    manifest_path=workspace.root / "state/facebook-accounts/example-person/pull-manifest.json",
                    downloaded=1,
                    completed_export_paths=[export],
                )

            with mock.patch("localgraph.facebook_sync.pull_configured_facebook_source", side_effect=materialize) as pull:
                code, stdout = run_cli(["--root", str(workspace.root), "facebook-sync", "--no-render"])

            self.assertEqual(code, 0)
            self.assertEqual(pull.call_count, 1)
            payload = json.loads(stdout)
            self.assertEqual(payload["facebookAccounts"]["example-person"]["googleDrivePull"]["downloaded"], 1)
            self.assertEqual(payload["result"]["totals"]["messages"], 1)

    def test_facebook_history_requires_account_specific_completed_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "graph"
            self.assertEqual(run_cli(["--root", str(root), "init"])[0], 0)
            configure_facebook_fixture(root, "example-person", "Example Person", "profile")
            export_name = "facebook-example-person-2026-08-29-baseline"
            write_facebook_packet(
                root / f"sources/facebook-accounts/example-person/incoming/{export_name}",
                owner="Example Person",
                content="historical message",
            )
            code, stdout = run_cli(["--root", str(root), "facebook-sync", "--no-render"])
            self.assertEqual(code, 0)
            self.assertEqual(
                json.loads(stdout)["facebookAccounts"]["example-person"]["sync"]["historyCoverage"],
                "baseline-required",
            )

            code, stdout = run_cli(
                [
                    "--root",
                    str(root),
                    "configure-facebook-baseline",
                    "--account",
                    "example-person",
                    "--export-name",
                    export_name,
                ]
            )

            self.assertEqual(code, 0)
            self.assertEqual(json.loads(stdout)["historyCoverage"], "complete-through-latest-export")
            code, stdout = run_cli(["--root", str(root), "facebook-sync", "--no-render"])
            self.assertEqual(code, 0)
            self.assertEqual(
                json.loads(stdout)["facebookAccounts"]["example-person"]["sync"]["historyCoverage"],
                "complete-through-latest-export",
            )

    def test_shared_drive_discovery_error_is_source_neutral(self) -> None:
        from localgraph.drive import DriveAPIError, _list_instagram_exports

        with mock.patch("localgraph.drive._list_drive_children", return_value=[]):
            with self.assertRaises(DriveAPIError) as raised:
                _list_instagram_exports(
                    "https://drive.example/drive/v3",
                    "token",
                    "root",
                    export_name_prefix="facebook-example-person-",
                )

        self.assertIn("Meta exports", str(raised.exception))
        self.assertNotIn("instagram", str(raised.exception).lower())


def run_cli(arguments: list[str]) -> tuple[int, str]:
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        code = main(arguments)
    return code, stream.getvalue()


def configure_facebook_fixture(root: Path, key: str, display_name: str, account_type: str) -> None:
    code, _ = run_cli(
        [
            "--root",
            str(root),
            "configure-facebook-account",
            "--account",
            key,
            "--display-name",
            display_name,
            "--account-type",
            account_type,
            "--self-name",
            display_name,
        ]
    )
    if code != 0:
        raise AssertionError(f"failed to configure Facebook fixture: {key}")


def write_facebook_packet(export_root: Path, *, owner: str, content: str) -> None:
    thread = export_root / "your_facebook_activity/messages/inbox/shared-person_123"
    thread.mkdir(parents=True)
    (thread / "message_1.json").write_text(
        json.dumps(
            {
                "participants": [{"name": owner}, {"name": "Shared Person"}],
                "title": "Shared Person",
                "messages": [
                    {
                        "sender_name": owner,
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
