from __future__ import annotations

import importlib
import importlib.util
import json
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from localgraph.paths import Workspace


NOW = datetime(2026, 8, 30, 12, tzinfo=timezone.utc)
TEXT = "[8/29/26, 10:00:00 AM] Alice: first\ncontinued: line\n[8/29/26, 10:01:00 AM] Me: same\n[8/29/26, 10:01:00 AM] Me: same\n"


class WhatsAppTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("localgraph.whatsapp"), "WhatsApp connector is missing")
        self.wa = importlib.import_module("localgraph.whatsapp")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.ws = Workspace(self.base / "graph")
        self.wa.configure_chat(self.ws, account_key="self", chat_key="friends", title="Friends", kind="group", date_order="mdy", timezone_name="America/New_York")

    def archive(self, name="export.zip", text=TEXT, extra=None):
        path = self.base / name
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("_chat.txt", text)
            for key, value in (extra or {}).items():
                z.writestr(key, value)
        return path

    def deliver(self, path=None, chat="friends", title="Friends", when=NOW, origin="mac-native"):
        return self.wa.record_export(self.ws, account_key="self", chat_key=chat, archive=path or self.archive(), observed_title=title, exported_at=when.isoformat(), media_requested=True, origin=origin)

    def sync(self):
        return self.wa.run_whatsapp_sync(self.ws, now=NOW)

    def rows(self):
        with closing(sqlite3.connect(self.ws.database_path)) as db:
            return db.execute("SELECT body_text,sent_at FROM messages ORDER BY id").fetchall()

    def test_multiline_timezone_and_repeated_identical_messages(self):
        """Catch multiline loss, incorrect zone conversion, and dedupe collapsing genuine repeats."""
        self.deliver()
        result = self.sync()
        self.assertEqual(self.rows(), [("first\ncontinued: line", "2026-08-29T14:00:00Z"), ("same", "2026-08-29T14:01:00Z"), ("same", "2026-08-29T14:01:00Z")])
        self.assertEqual(result["chats"][0]["messages"], 3)
        self.assertNotIn("continued: line", json.dumps(result))

    def test_overlap_shorter_exports_and_repeat_acquisition_preserve_history(self):
        """Catch destructive snapshot replacement, duplicate overlap, and lost acquisition receipts."""
        self.deliver()
        self.sync()
        self.deliver(self.archive("short.zip", "[8/29/26, 10:01:00 AM] Me: same\n[8/29/26, 10:02:00 AM] Alice: newer\n"))
        self.deliver(self.archive(), when=NOW + timedelta(minutes=1))
        result = self.sync()
        self.assertEqual(len(self.rows()), 4)
        self.assertEqual(result["chats"][0]["exports"], 2)
        self.assertEqual(len(list((self.ws.sources_dir / "whatsapp/self/friends/receipts").glob("*.json"))), 3)

    def test_delivered_archive_has_verified_private_custody(self):
        """Catch input being referenced instead of copied, or custody not binding exact bytes."""
        receipt = self.deliver()
        self.assertEqual(len(receipt["sha256"]), 64)
        self.assertEqual(Path(receipt["archivePath"]).stat().st_mode & 0o777, 0o600)
        self.archive().unlink()
        self.assertEqual(self.sync()["chats"][0]["messages"], 3)

    def test_wrong_chat_binding_and_unqualified_time_are_rejected(self):
        """Catch accidental cross-chat ingestion or invented acquisition timezone."""
        with self.assertRaises(ValueError):
            self.deliver(title="Other chat")
        with self.assertRaises(ValueError):
            self.deliver(when=datetime(2026, 8, 30))
        self.assertEqual(list((self.ws.sources_dir / "whatsapp/self/friends").glob("receipts/*.json")), [])

    def test_invalid_zip_paths_symlinks_and_multiple_transcripts_are_rejected(self):
        """Catch untrusted ZIP traversal and ambiguous transcript selection."""
        for extra in ({"../escape": b"x"}, {"/absolute": b"x"}, {"other/_chat.txt": TEXT}, {"a\\b": b"x"}):
            with self.subTest(extra=list(extra)):
                with self.assertRaises(ValueError):
                    self.deliver(self.archive(extra=extra))
        path = self.archive()
        with zipfile.ZipFile(path, "a") as z:
            info = zipfile.ZipInfo("link")
            info.create_system = 3
            info.external_attr = 0o120777 << 16
            z.writestr(info, "/etc/passwd")
        with self.assertRaises(ValueError):
            self.deliver(path)

    def test_malformed_and_empty_exports_never_replace_good_projection(self):
        """Catch empty, corrupt, or unsupported input silently erasing accepted history."""
        self.deliver()
        self.sync()
        before = self.rows()
        for content in ("", "not a WhatsApp export", "[13/50/26, 10:00 AM] Alice: invalid"):
            with self.assertRaises(ValueError):
                self.deliver(self.archive("bad.zip", content))
        self.assertEqual(self.rows(), before)

    def test_custody_tampering_preserves_last_good_and_other_chats_advance(self):
        """Catch one bad chat blocking another or silently accepting modified archives."""
        receipt = self.deliver()
        self.sync()
        Path(receipt["archivePath"]).write_bytes(b"corrupted")
        self.wa.configure_chat(self.ws, account_key="self", chat_key="direct", title="Direct", kind="direct", date_order="mdy", timezone_name="America/New_York")
        self.deliver(chat="direct", title="Direct")
        result = self.sync()
        by_key = {r["chatKey"]: r for r in result["chats"]}
        self.assertEqual(by_key["friends"]["status"], "degraded")
        self.assertEqual(by_key["direct"]["messages"], 3)
        self.assertEqual(len(self.rows()), 6)

    def test_media_is_copied_to_content_addressed_custody_and_missing_is_reported(self):
        """Catch attachments only counted but not preserved, or omitted media called complete."""
        text = "[8/29/26, 10:00 AM] Alice: <attached: photo.jpg>\n[8/29/26, 10:01 AM] Alice: <attached: gone.jpg>\n"
        self.deliver(self.archive(text=text, extra={"photo.jpg": b"image bytes"}))
        row = self.sync()["chats"][0]
        self.assertEqual(row["mediaFiles"], 1)
        self.assertEqual(row["missingMedia"], 1)
        with closing(sqlite3.connect(self.ws.database_path)) as db:
            path = db.execute("SELECT local_path FROM media_objects WHERE local_path IS NOT NULL").fetchone()[0]
        self.assertEqual(Path(path).read_bytes(), b"image bytes")

    def test_dmy_android_system_events_unicode_and_dst_ambiguity(self):
        """Catch locale guessing, system-event loss, or silently choosing an ambiguous DST instant."""
        parsed = self.wa.parse_transcript("29/08/2026, 14:32 - Alice: hello\n29/08/2026, 14:33 - Alice left\n", date_order="dmy", timezone_name="UTC")
        self.assertEqual(parsed[0]["sentAt"], "2026-08-29T14:32:00Z")
        self.assertIsNone(parsed[1]["sender"])
        parsed = self.wa.parse_transcript("\u200e[8/29/26, 8:56:12\u202fPM] Alice: héllo\n", date_order="mdy", timezone_name="America/New_York")
        self.assertEqual(parsed[0]["body"], "héllo")
        with self.assertRaises(ValueError):
            self.wa.parse_transcript("[11/1/26, 1:30 AM] Alice: ambiguous\n", date_order="mdy", timezone_name="America/New_York")

    def test_render_failure_rolls_back_database_and_preserves_transcript(self):
        """Catch canonical state advancing after a failed render, or a good view being destroyed."""
        self.deliver()
        old = self.sync()["chats"][0]
        path = Path(old["viewPath"]) / "messages.md"
        before = path.read_bytes()
        self.deliver(self.archive("new.zip", TEXT + "[8/29/26, 10:03 AM] Me: new\n"))
        with patch("localgraph.whatsapp._write_thread_view", side_effect=OSError("synthetic disk failure")):
            result = self.sync()
        self.assertEqual(result["chats"][0]["status"], "degraded")
        self.assertEqual(len(self.rows()), 3)
        self.assertEqual(path.read_bytes(), before)

    def test_reimport_does_not_make_old_acquisition_fresh_or_history_complete(self):
        """Catch the hourly importer laundering an old export into a fresh/full-history claim."""
        self.deliver(self.archive(text="[8/20/26, 10:00 AM] Alice: old\n"), when=NOW - timedelta(days=3))
        self.sync()
        from localgraph.status import build_localgraph_status
        report = build_localgraph_status(self.ws, now=NOW, home=self.base, launchctl=lambda _: (113, ""))
        chat = report["sources"]["whatsapp"]["accounts"][0]["chats"][0]
        codes = {f["code"] for f in chat["findings"]}
        self.assertIn("stale-acquisition", codes)
        self.assertFalse(chat["lifecycle"]["complete"])
        self.assertFalse(chat["lifecycle"]["current"])

    def test_renamed_chat_retains_path_and_unknown_chat_is_not_imported(self):
        """Catch display-name changes breaking links or unregistered chats entering custody."""
        self.deliver()
        old = self.sync()["chats"][0]["viewPath"]
        self.wa.configure_chat(self.ws, account_key="self", chat_key="friends", title="Renamed", kind="group", date_order="mdy", timezone_name="America/New_York")
        self.deliver(title="Renamed")
        self.assertEqual(self.sync()["chats"][0]["viewPath"], old)
        with self.assertRaises(ValueError):
            self.deliver(chat="unapproved")

    def test_historical_adoption_does_not_claim_current_native_acquisition(self):
        """Catch old downloaded files with fresh filesystem timestamps being called newly exported."""
        self.deliver(origin="historical-local")
        row = self.sync()["chats"][0]
        self.assertIsNone(row["lastNativeExportAt"])
        self.assertEqual(row["historyCoverage"], "available-export-history-unverified")

    def test_shared_lock_prevents_concurrent_import(self):
        """Catch the new connector bypassing the existing workspace writer lock."""
        from localgraph.automation import instagram_sync_lock
        self.deliver()
        with instagram_sync_lock(self.ws) as acquired:
            self.assertTrue(acquired)
            self.assertEqual(self.sync()["status"], "skipped-concurrent")

    def test_installed_job_imports_using_its_runtime(self):
        """Catch installing a watcher that invokes the wrong command or missing runtime."""
        import subprocess
        self.deliver()
        install = self.wa.install_whatsapp_sync(self.ws, home=self.base / "home")
        result = subprocess.run(["/bin/zsh", install["script"]], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.rows()), 3)

    def test_missing_prior_receipts_degrade_without_losing_last_success(self):
        """Catch removed custody being reclassified as ordinary first-export waiting."""
        self.deliver()
        previous = self.sync()["chats"][0]
        for path in (self.ws.sources_dir / "whatsapp/self/friends/receipts").glob("*.json"):
            path.unlink()
        result = self.sync()["chats"][0]
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["lastSuccessfulSyncAt"], previous["lastSuccessfulSyncAt"])
        self.assertEqual(len(self.rows()), 3)

    def test_delivery_precedes_import_in_lifecycle_and_canonical_loss_is_detected(self):
        """Catch delivery being inferred only from stale import receipts or lost canonical rows hidden."""
        from localgraph.status import build_localgraph_status
        def report():
            return build_localgraph_status(self.ws, now=NOW, home=self.base, launchctl=lambda _: (113, ""))["sources"]["whatsapp"]["accounts"][0]["chats"][0]
        self.deliver()
        self.assertEqual(report()["lifecycle"]["currentStage"], "delivered")
        self.sync()
        with closing(sqlite3.connect(self.ws.database_path)) as db:
            db.execute("DELETE FROM messages")
            db.commit()
        self.assertIn("canonical-count-mismatch", {r["code"] for r in report()["findings"]})
        self.assertFalse(report()["lifecycle"]["current"])

    def test_changed_render_is_detected_and_native_failure_cannot_be_hidden_by_import(self):
        """Catch stale/corrupted views or a failing acquisition job still appearing current."""
        from localgraph.status import build_localgraph_status
        self.deliver()
        row = self.sync()["chats"][0]
        (Path(row["viewPath"]) / "messages.md").write_text("truncated")
        self.wa.record_acquisition_failure(self.ws, account_key="self", chat_key="friends", reason="app-disconnected")
        report = build_localgraph_status(self.ws, now=NOW, home=self.base, launchctl=lambda _: (113, ""))
        codes = {f["code"] for f in report["sources"]["whatsapp"]["accounts"][0]["chats"][0]["findings"]}
        self.assertIn("render-checksum-mismatch", codes)
        self.assertIn("native-acquisition-failed", codes)

    def test_malformed_timestamp_line_is_not_silently_folded_into_previous_message(self):
        """Catch unsupported timestamp-looking records turning into apparently complete text."""
        with self.assertRaises(ValueError):
            self.wa.parse_transcript(TEXT + "[2026-08-29, 10:00] Alice: unsupported date\n", date_order="mdy", timezone_name="UTC")

    def test_native_acquisition_cannot_be_backdated_before_export_messages(self):
        """Catch future-dated source records being accepted as a trustworthy current export."""
        with self.assertRaises(ValueError):
            self.deliver(self.archive(text="[9/1/26, 10:00 AM] Alice: future\n"))

    def test_generated_view_symlink_cannot_redirect_writes_outside_the_chat(self):
        """Catch an existing generated-file symlink redirecting a staged render to another file."""
        self.deliver()
        row = self.sync()["chats"][0]
        sentinel = self.base / "do-not-change.txt"
        sentinel.write_text("keep")
        view = Path(row["viewPath"]) / "messages.md"
        view.unlink()
        view.symlink_to(sentinel)
        self.sync()
        self.assertEqual(sentinel.read_text(), "keep")
        self.assertFalse(view.is_symlink())

    def test_partial_receipt_loss_is_detected(self):
        """Catch disappearing historical custody hidden by the continued presence of a newer packet."""
        self.deliver()
        self.deliver(self.archive("next.zip", TEXT + "[8/29/26, 10:03 AM] Me: later\n"))
        self.sync()
        next((self.ws.sources_dir / "whatsapp/self/friends/receipts").glob("*.json")).unlink()
        self.assertEqual(self.sync()["chats"][0]["status"], "degraded")

    def test_status_reads_committed_wal_and_pending_delivery_blocks_current(self):
        """Catch ignoring committed WAL state and calling an unimported new packet current."""
        from localgraph.status import build_localgraph_status
        self.deliver()
        self.sync()
        def report():
            return build_localgraph_status(self.ws, now=NOW, home=self.base, launchctl=lambda _: (113, ""))["sources"]["whatsapp"]["accounts"][0]["chats"][0]
        with closing(sqlite3.connect(self.ws.database_path)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA wal_autocheckpoint=0")
            db.execute("DELETE FROM messages")
            db.commit()
            self.assertEqual(report()["canonicalMessages"], 0)
        self.sync()
        self.deliver(self.archive("next.zip", TEXT + "[8/29/26, 10:03 AM] Me: more\n"))
        self.assertFalse(report()["lifecycle"]["current"])
        self.assertIn("unimported-export", {f["code"] for f in report()["findings"]})

    def test_corrupt_receipt_is_a_health_error_not_a_status_crash(self):
        """Catch a malformed receipt preventing every source from appearing in unified status."""
        from localgraph.status import build_localgraph_status
        self.deliver()
        self.sync()
        next((self.ws.sources_dir / "whatsapp/self/friends/receipts").glob("*.json")).write_text("{")
        report = build_localgraph_status(self.ws, now=NOW, home=self.base, launchctl=lambda _: (113, ""))
        chat = report["sources"]["whatsapp"]["accounts"][0]["chats"][0]
        self.assertIn("whatsapp-metadata-invalid", {f["code"] for f in chat["findings"]})
        self.assertFalse(chat["lifecycle"]["current"])
