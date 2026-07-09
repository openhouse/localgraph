from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from localgraph.cli import main
from localgraph.slug import stable_view_name


class CliTests(unittest.TestCase):
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

            with sqlite3.connect(db_path) as db:
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
