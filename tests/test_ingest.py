from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from localgraph.cli import main


class IngestTests(unittest.TestCase):
    def test_import_instagram_messages_people_group_and_media(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "graph"
            instagram = root / "sources" / "instagram" / "meta" / "instagram-jamie" / "your_instagram_activity" / "messages" / "inbox"
            direct = instagram / "alice_123"
            group = instagram / "residency_456"
            direct.mkdir(parents=True)
            group.mkdir(parents=True)
            (direct / "message_1.json").write_text(
                json.dumps(
                    {
                        "participants": [{"name": "Jamie"}, {"name": "Alice"}],
                        "title": "Alice",
                        "messages": [
                            {
                                "sender_name": "Alice",
                                "timestamp_ms": 1700000000000,
                                "content": "hello from instagram",
                                "photos": [{"uri": "messages/inbox/alice_123/photos/0.jpg"}],
                            },
                            {
                                "sender_name": "Jamie",
                                "timestamp_ms": 1700000001000,
                                "content": "reply from me",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (group / "message_1.json").write_text(
                json.dumps(
                    {
                        "participants": [{"name": "Jamie"}, {"name": "Alice"}, {"name": "Bob"}],
                        "title": "Residency Planning",
                        "messages": [
                            {
                                "sender_name": "Bob",
                                "timestamp_ms": 1700000010000,
                                "content": "group note",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            code, stdout = run_cli(
                [
                    "--root",
                    str(root),
                    "import",
                    "--skip-imessage",
                    "--me",
                    "Jamie",
                    "--me-instagram",
                    "Jamie",
                    "--render",
                ]
            )

            self.assertEqual(code, 0)
            payload = json.loads(stdout)
            self.assertEqual(payload["totals"]["threads"], 2)
            self.assertEqual(payload["totals"]["groups"], 1)
            self.assertEqual(payload["totals"]["messages"], 3)
            self.assertEqual(payload["totals"]["media"], 1)
            self.assertTrue((root / "views" / "threads" / "instagram").is_dir())
            transcript = next((root / "views" / "threads" / "instagram").glob("alice*/messages.md")).read_text(encoding="utf-8")
            self.assertIn("hello from instagram", transcript)
            self.assertIn("[1 media attachment]", transcript)
            group_view = next((root / "views" / "groups").glob("residency-planning*/index.md")).read_text(encoding="utf-8")
            self.assertIn("Alice", group_view)
            self.assertIn("Bob", group_view)
            self.assertIn("Residency Planning (instagram, group", group_view)

    def test_import_imessage_chat_db_direct_group_and_attachment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "graph"
            chat_db = root / "sources" / "imessage" / "chat.db"
            chat_db.parent.mkdir(parents=True)
            create_imessage_fixture(chat_db)

            code, stdout = run_cli(
                [
                    "--root",
                    str(root),
                    "import",
                    "--skip-instagram",
                    "--me",
                    "Jamie",
                    "--me-imessage",
                    "jamie@example.com",
                    "--render",
                ]
            )

            self.assertEqual(code, 0)
            payload = json.loads(stdout)
            self.assertEqual(payload["totals"]["threads"], 2)
            self.assertEqual(payload["totals"]["groups"], 1)
            self.assertEqual(payload["totals"]["messages"], 3)
            self.assertEqual(payload["totals"]["media"], 1)
            transcript = next((root / "views" / "threads" / "imessage").glob("alice-example-com*/messages.md")).read_text(
                encoding="utf-8"
            )
            self.assertIn("hi from imessage", transcript)
            self.assertIn("reply from self", transcript)
            group_view = next((root / "views" / "groups").glob("family-chat*/index.md")).read_text(encoding="utf-8")
            self.assertIn("alice@example.com", group_view)
            self.assertIn("bob@example.com", group_view)
            self.assertIn("Family Chat (imessage, group", group_view)


def create_imessage_fixture(path: Path) -> None:
    apple_date = 700_000_000_000_000_000
    with contextlib.closing(sqlite3.connect(path)) as db:
        db.executescript(
            """
            CREATE TABLE handle (
              ROWID INTEGER PRIMARY KEY,
              id TEXT,
              service TEXT
            );
            CREATE TABLE chat (
              ROWID INTEGER PRIMARY KEY,
              guid TEXT,
              chat_identifier TEXT,
              display_name TEXT,
              service_name TEXT
            );
            CREATE TABLE chat_handle_join (
              chat_id INTEGER,
              handle_id INTEGER
            );
            CREATE TABLE message (
              ROWID INTEGER PRIMARY KEY,
              guid TEXT,
              text TEXT,
              attributedBody BLOB,
              date INTEGER,
              is_from_me INTEGER,
              handle_id INTEGER,
              service TEXT
            );
            CREATE TABLE chat_message_join (
              chat_id INTEGER,
              message_id INTEGER
            );
            CREATE TABLE attachment (
              ROWID INTEGER PRIMARY KEY,
              guid TEXT,
              filename TEXT,
              mime_type TEXT,
              uti TEXT,
              total_bytes INTEGER
            );
            CREATE TABLE message_attachment_join (
              message_id INTEGER,
              attachment_id INTEGER
            );
            """
        )
        db.executemany(
            "INSERT INTO handle (ROWID, id, service) VALUES (?, ?, ?)",
            [
                (1, "alice@example.com", "iMessage"),
                (2, "bob@example.com", "iMessage"),
            ],
        )
        db.executemany(
            "INSERT INTO chat (ROWID, guid, chat_identifier, display_name, service_name) VALUES (?, ?, ?, ?, ?)",
            [
                (1, "iMessage;-;alice@example.com", "alice@example.com", None, "iMessage"),
                (2, "chat-family", "chat-family", "Family Chat", "iMessage"),
            ],
        )
        db.executemany(
            "INSERT INTO chat_handle_join (chat_id, handle_id) VALUES (?, ?)",
            [(1, 1), (2, 1), (2, 2)],
        )
        db.executemany(
            "INSERT INTO message (ROWID, guid, text, attributedBody, date, is_from_me, handle_id, service) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (1, "msg-1", "hi from imessage", None, apple_date, 0, 1, "iMessage"),
                (2, "msg-2", "reply from self", None, apple_date + 1_000_000_000, 1, None, "iMessage"),
                (3, "msg-3", "group imessage", None, apple_date + 2_000_000_000, 0, 2, "iMessage"),
            ],
        )
        db.executemany(
            "INSERT INTO chat_message_join (chat_id, message_id) VALUES (?, ?)",
            [(1, 1), (1, 2), (2, 3)],
        )
        db.execute(
            "INSERT INTO attachment (ROWID, guid, filename, mime_type, uti, total_bytes) VALUES (?, ?, ?, ?, ?, ?)",
            (1, "att-1", "/tmp/photo.jpg", "image/jpeg", "public.jpeg", 10),
        )
        db.execute("INSERT INTO message_attachment_join (message_id, attachment_id) VALUES (?, ?)", (3, 1))
        db.commit()


def run_cli(argv: list[str]) -> tuple[int, str]:
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        code = main(argv)
    return code, stream.getvalue()


if __name__ == "__main__":
    unittest.main()
