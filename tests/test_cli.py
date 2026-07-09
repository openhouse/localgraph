from __future__ import annotations

import contextlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path

from localgraph.cli import main


class CliTests(unittest.TestCase):
    def test_init_doctor_and_render_empty_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "graph"

            self.assertEqual(run_cli(["--root", str(root), "init"]), 0)
            self.assertTrue((root / "state" / "localgraph.sqlite").exists())
            self.assertTrue((root / "PRIVATE-DATA-README.md").exists())

            self.assertEqual(run_cli(["--root", str(root), "doctor"]), 0)
            self.assertEqual(run_cli(["--root", str(root), "render"]), 0)
            self.assertIn("People: 0", (root / "views" / "index.md").read_text(encoding="utf-8"))

    def test_render_creates_person_group_and_thread_views(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "graph"
            self.assertEqual(run_cli(["--root", str(root), "init"]), 0)
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

            self.assertEqual(run_cli(["--root", str(root), "render"]), 0)
            self.assertTrue((root / "views" / "people" / "alice-example--igalice" / "index.md").exists())
            self.assertTrue((root / "views" / "groups" / "residency-planning--esidency" / "index.md").exists())
            self.assertTrue((root / "views" / "threads" / "instagram" / "alice-example--alice123" / "index.md").exists())

    def test_private_directories_are_gitignored(self) -> None:
        gitignore = Path(__file__).resolve().parents[1] / ".gitignore"
        ignored = gitignore.read_text(encoding="utf-8")
        for path in ["/sources/", "/state/", "/objects/", "/views/", "/annotations/"]:
            self.assertIn(path, ignored)


def run_cli(argv: list[str]) -> int:
    with contextlib.redirect_stdout(io.StringIO()):
        return main(argv)


if __name__ == "__main__":
    unittest.main()
