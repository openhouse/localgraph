from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
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

    def test_import_instagram_messages_creates_people_groups_and_transcripts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "graph"
            code, _ = run_cli(["--root", str(root), "init"])
            self.assertEqual(code, 0)
            direct = root / "sources" / "instagram" / "export-a" / "messages" / "inbox" / "alice_123"
            group = root / "sources" / "instagram" / "export-a" / "messages" / "inbox" / "residency_456"
            direct.mkdir(parents=True)
            group.mkdir(parents=True)
            (direct / "message_1.json").write_text(
                json.dumps(
                    {
                        "participants": [{"name": "Alice Example"}, {"name": "Jamie"}],
                        "title": "Alice Example",
                        "messages": [
                            {"sender_name": "Alice Example", "timestamp_ms": 1700000000000, "content": "Hello from Instagram"},
                            {"sender_name": "Jamie", "timestamp_ms": 1700000001000, "content": "Hi Alice"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (group / "message_1.json").write_text(
                json.dumps(
                    {
                        "participants": [{"name": "Alice Example"}, {"name": "Bob Example"}, {"name": "Jamie"}],
                        "title": "Residency Planning",
                        "messages": [
                            {
                                "sender_name": "Bob Example",
                                "timestamp_ms": 1700000002000,
                                "content": "Group note",
                                "photos": [{"uri": "media/photo_001.jpg", "mime_type": "image/jpeg"}],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            code, stdout = run_cli(["--root", str(root), "import", "--skip-imessage", "--render"])
            self.assertEqual(code, 0)
            payload = json.loads(stdout)
            self.assertEqual(payload["totals"]["messages"], 3)
            self.assertEqual(payload["totals"]["groups"], 1)

            code, _ = run_cli(["--root", str(root), "render"])
            self.assertEqual(code, 0)
            manifest = json.loads((root / "views" / "_system" / "source-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["counts"]["messages"], 3)
            transcripts = list((root / "views" / "threads" / "instagram").glob("*/messages.md"))
            self.assertTrue(any("Hello from Instagram" in path.read_text(encoding="utf-8") for path in transcripts))
            self.assertTrue(any("Group note" in path.read_text(encoding="utf-8") for path in transcripts))

            with contextlib.closing(sqlite3.connect(root / "state" / "localgraph.sqlite")) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM identities WHERE kind = 'person'").fetchone()[0], 3)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM identities WHERE kind = 'group'").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM media_objects").fetchone()[0], 1)

    def test_import_imessage_chat_db_creates_people_group_and_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "graph"
            chat_db = Path(tmp) / "chat.db"
            build_imessage_fixture(chat_db)

            code, _ = run_cli(["--root", str(root), "init"])
            self.assertEqual(code, 0)
            code, stdout = run_cli(["--root", str(root), "import", "--skip-instagram", "--imessage-db", str(chat_db), "--render"])
            self.assertEqual(code, 0)
            payload = json.loads(stdout)
            self.assertEqual(payload["totals"]["messages"], 2)
            self.assertEqual(payload["totals"]["groups"], 1)

            code, _ = run_cli(["--root", str(root), "render"])
            self.assertEqual(code, 0)
            transcripts = list((root / "views" / "threads" / "imessage").glob("*/messages.md"))
            self.assertEqual(len(transcripts), 1)
            rendered = transcripts[0].read_text(encoding="utf-8")
            self.assertIn("Hello from Messages", rendered)
            self.assertIn("Reply from Messages", rendered)

            with contextlib.closing(sqlite3.connect(root / "state" / "localgraph.sqlite")) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM identities WHERE kind = 'person'").fetchone()[0], 3)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM identities WHERE kind = 'group'").fetchone()[0], 1)


def run_cli(argv: list[str]) -> tuple[int, str]:
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        code = main(argv)
    return code, stream.getvalue()


def build_imessage_fixture(path: Path) -> None:
    apple_epoch = datetime(2001, 1, 1, tzinfo=timezone.utc)
    first = int((datetime(2026, 1, 1, tzinfo=timezone.utc) - apple_epoch).total_seconds() * 1_000_000_000)
    second = first + 1_000_000_000
    with contextlib.closing(sqlite3.connect(path)) as db:
        db.executescript(
            """
            CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT, service TEXT);
            CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, guid TEXT, chat_identifier TEXT, display_name TEXT, service_name TEXT, room_name TEXT);
            CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
            CREATE TABLE message (
              ROWID INTEGER PRIMARY KEY,
              guid TEXT,
              text TEXT,
              attributedBody BLOB,
              date INTEGER,
              is_from_me INTEGER,
              service TEXT,
              handle_id INTEGER
            );
            CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
            """
        )
        db.execute("INSERT INTO handle (ROWID, id, service) VALUES (1, ?, 'iMessage')", ("+15555550100",))
        db.execute("INSERT INTO handle (ROWID, id, service) VALUES (2, ?, 'iMessage')", ("bob@example.com",))
        db.execute(
            "INSERT INTO chat (ROWID, guid, chat_identifier, display_name, service_name, room_name) VALUES (1, ?, ?, ?, 'iMessage', ?)",
            ("iMessage;+;chat-guid-1", "chat-guid-1", "Project Thread", "room-guid-1"),
        )
        db.execute("INSERT INTO chat_handle_join (chat_id, handle_id) VALUES (1, 1)")
        db.execute("INSERT INTO chat_handle_join (chat_id, handle_id) VALUES (1, 2)")
        db.execute(
            "INSERT INTO message (ROWID, guid, text, attributedBody, date, is_from_me, service, handle_id) VALUES (1, ?, ?, NULL, ?, 1, 'iMessage', NULL)",
            ("msg-1", "Hello from Messages", first),
        )
        db.execute(
            "INSERT INTO message (ROWID, guid, text, attributedBody, date, is_from_me, service, handle_id) VALUES (2, ?, ?, NULL, ?, 0, 'iMessage', 1)",
            ("msg-2", "Reply from Messages", second),
        )
        db.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 1)")
        db.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 2)")
        db.commit()


if __name__ == "__main__":
    unittest.main()
