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

    def test_import_instagram_messages_creates_people_group_thread_and_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "graph"
            code, _ = run_cli(["--root", str(root), "init"])
            self.assertEqual(code, 0)
            inbox = root / "sources" / "instagram" / "meta-2026" / "instagram-jamieburkart-2026-07-08" / "your_instagram_activity" / "messages" / "inbox" / "project_group_123"
            inbox.mkdir(parents=True)
            (inbox / "message_1.json").write_text(
                json.dumps(
                    {
                        "participants": [{"name": "Jamie"}, {"name": "Alice Example"}, {"name": "Bob Example"}],
                        "messages": [
                            {
                                "sender_name": "Alice Example",
                                "timestamp_ms": 1_788_752_000_000,
                                "content": "Dinner at 7?",
                            },
                            {
                                "sender_name": "Bob Example",
                                "timestamp_ms": 1_788_752_060_000,
                                "photos": [{"uri": "messages/inbox/project_group_123/photos/photo.jpg"}],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            code, stdout = run_cli(["--root", str(root), "import", "instagram"])
            self.assertEqual(code, 0)
            payload = json.loads(stdout)
            self.assertEqual(payload["results"]["instagram"]["messages"], 2)
            self.assertEqual(payload["results"]["instagram"]["groups"], 1)
            code, _ = run_cli(["--root", str(root), "import", "instagram"])
            self.assertEqual(code, 0)
            with sqlite3.connect(root / "state" / "localgraph.sqlite") as db:
                message_count = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            self.assertEqual(message_count, 2)

            code, stdout = run_cli(["--root", str(root), "render"])
            self.assertEqual(code, 0)
            render_payload = json.loads(stdout)
            self.assertEqual(render_payload["messages"], 2)

            thread_key = "your_instagram_activity/messages/inbox/project_group_123"
            title = "Jamie, Alice Example, Bob Example"
            thread_dir = root / "views" / "threads" / "instagram" / stable_view_name(title, thread_key)
            transcript = (thread_dir / "messages.md").read_text(encoding="utf-8")
            self.assertIn("Dinner at 7?", transcript)
            self.assertIn("[Photo]", transcript)

    def test_import_imessage_chat_db_creates_people_group_thread_and_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "graph"
            code, _ = run_cli(["--root", str(root), "init"])
            self.assertEqual(code, 0)
            chat_db = root / "sources" / "imessage" / "chat.db"
            create_imessage_fixture(chat_db)

            code, stdout = run_cli(["--root", str(root), "import", "imessage"])
            self.assertEqual(code, 0)
            payload = json.loads(stdout)
            self.assertEqual(payload["results"]["imessage"]["messages"], 3)
            self.assertEqual(payload["results"]["imessage"]["groups"], 1)
            code, _ = run_cli(["--root", str(root), "import", "imessage"])
            self.assertEqual(code, 0)
            with sqlite3.connect(root / "state" / "localgraph.sqlite") as db:
                message_count = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            self.assertEqual(message_count, 3)

            code, stdout = run_cli(["--root", str(root), "render"])
            self.assertEqual(code, 0)
            render_payload = json.loads(stdout)
            self.assertEqual(render_payload["messages"], 3)

            thread_key = "iMessage;-;project-group"
            thread_dir = root / "views" / "threads" / "imessage" / stable_view_name("Project Group", thread_key)
            transcript = (thread_dir / "messages.md").read_text(encoding="utf-8")
            self.assertIn("Hello from Alice", transcript)
            self.assertIn("Hi both", transcript)
            self.assertIn("Photo from Bob", transcript)

            group_dir = root / "views" / "groups" / stable_view_name("Project Group", f"group:imessage:{thread_key}")
            group_index = (group_dir / "index.md").read_text(encoding="utf-8")
            self.assertIn("Alice Example", group_index)
            self.assertIn("Bob Example", group_index)
            self.assertIn("Me", group_index)

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


def create_imessage_fixture(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE handle (
              ROWID INTEGER PRIMARY KEY,
              id TEXT,
              service TEXT,
              uncanonicalized_id TEXT
            );
            CREATE TABLE chat (
              ROWID INTEGER PRIMARY KEY,
              guid TEXT,
              chat_identifier TEXT,
              display_name TEXT,
              room_name TEXT,
              service_name TEXT,
              style INTEGER
            );
            CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
            CREATE TABLE message (
              ROWID INTEGER PRIMARY KEY,
              guid TEXT,
              text TEXT,
              attributedBody BLOB,
              handle_id INTEGER,
              date INTEGER,
              is_from_me INTEGER,
              service TEXT,
              cache_has_attachments INTEGER
            );
            CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
            CREATE TABLE attachment (
              ROWID INTEGER PRIMARY KEY,
              guid TEXT,
              filename TEXT,
              mime_type TEXT,
              transfer_name TEXT,
              total_bytes INTEGER
            );
            CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER);
            """
        )
        db.execute("INSERT INTO handle (ROWID, id, service) VALUES (?, ?, ?)", (1, "Alice Example", "iMessage"))
        db.execute("INSERT INTO handle (ROWID, id, service) VALUES (?, ?, ?)", (2, "Bob Example", "iMessage"))
        db.execute(
            "INSERT INTO chat (ROWID, guid, chat_identifier, display_name, service_name, style) VALUES (?, ?, ?, ?, ?, ?)",
            (10, "iMessage;-;project-group", "chat-project-group", "Project Group", "iMessage", 45),
        )
        db.execute("INSERT INTO chat_handle_join (chat_id, handle_id) VALUES (?, ?)", (10, 1))
        db.execute("INSERT INTO chat_handle_join (chat_id, handle_id) VALUES (?, ?)", (10, 2))
        messages = [
            (100, "imsg-100", "Hello from Alice", None, 1, apple_ns("2026-09-04T12:00:00Z"), 0, "iMessage", 0),
            (101, "imsg-101", "Hi both", None, 0, apple_ns("2026-09-04T12:01:00Z"), 1, "iMessage", 0),
            (102, "imsg-102", "Photo from Bob", None, 2, apple_ns("2026-09-04T12:02:00Z"), 0, "iMessage", 1),
        ]
        db.executemany(
            """
            INSERT INTO message (
              ROWID, guid, text, attributedBody, handle_id, date, is_from_me, service, cache_has_attachments
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            messages,
        )
        for message_id in [100, 101, 102]:
            db.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (?, ?)", (10, message_id))
        db.execute(
            "INSERT INTO attachment (ROWID, guid, filename, mime_type, transfer_name, total_bytes) VALUES (?, ?, ?, ?, ?, ?)",
            (200, "attachment-200", "~/Library/Messages/Attachments/photo.jpg", "image/jpeg", "photo.jpg", 12_345),
        )
        db.execute("INSERT INTO message_attachment_join (message_id, attachment_id) VALUES (?, ?)", (102, 200))


def apple_ns(value: str) -> int:
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    return int((timestamp - 978_307_200) * 1_000_000_000)


if __name__ == "__main__":
    unittest.main()
