"""Acquisition contracts use synthetic UI observations, never private messages."""
from __future__ import annotations

import importlib
import importlib.util
import tempfile
import unittest
import json
import zipfile
import shutil
from pathlib import Path

from localgraph.paths import Workspace


class WhatsAppAcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("localgraph.whatsapp_acquisition"),
                             "deterministic WhatsApp acquisition is missing")
        self.acq = importlib.import_module("localgraph.whatsapp_acquisition")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Workspace(Path(self.tmp.name) / "graph")

    def test_inventory_requires_both_lists_and_never_claims_phone_completeness(self):
        """Catch accepting a partial/filtered list as the complete account population."""
        report = self.acq.reconcile_inventory(self.ws, account="self", expected_profile="Self",
            observed_profile="Self", lists={"main": ["Friends"]}, date_order="mdy", timezone_name="UTC")
        self.assertFalse(report["populationCovered"])
        self.assertEqual(report["status"], "inventory-incomplete")
        self.assertFalse(report["historicalComplete"])

    def reconcile(self, lists, profile="Self"):
        return self.acq.reconcile_inventory(self.ws, account="self", expected_profile="Self",
            observed_profile=profile, lists=lists, date_order="mdy", timezone_name="UTC")

    def test_inventory_enrolls_main_and_archived_without_claiming_delivery(self):
        """Catch archived chats being omitted or discovery counted as successful export."""
        report = self.reconcile({"main": ["Friends"], "archived": ["Old group"]})
        self.assertEqual(report["discoveredChats"], 2)
        self.assertEqual(report["configuredChats"], 2)
        self.assertTrue(report["inventoryComplete"])
        self.assertFalse(report["populationCovered"])
        from localgraph.whatsapp import chats
        self.assertEqual({r["title"] for r in chats(self.ws)}, {"Friends", "Old group"})

    def test_wrong_account_cannot_enroll_or_advance_inventory(self):
        """Catch profile mismatch broadening access or producing a success receipt."""
        with self.assertRaisesRegex(ValueError, "identity-unverified"):
            self.reconcile({"main": ["Friends"], "archived": []}, profile="Other")
        self.assertFalse(self.ws.config_path.exists())

    def test_duplicate_titles_and_disappeared_chats_remain_unresolved(self):
        """Catch title collisions merging people and missing chats being silently dropped."""
        self.reconcile({"main": ["Friends", "Retired"], "archived": []})
        report = self.reconcile({"main": ["Friends", "Twins", "Twins"], "archived": []})
        self.assertEqual(report["ambiguousChats"], 2)
        self.assertEqual(report["missingPreviouslySeenChats"], 1)
        self.assertFalse(report["populationCovered"])
        from localgraph.whatsapp import chats
        self.assertEqual({r["title"] for r in chats(self.ws)}, {"Friends", "Retired"})

    def test_configured_bindings_keep_existing_keys_paths_and_kind(self):
        """Catch automatic discovery forking a previously bound conversation."""
        from localgraph.whatsapp import configure_chat, chats
        configure_chat(self.ws, account_key="self", chat_key="custom", title="Friends",
                       kind="group", date_order="mdy", timezone_name="UTC")
        self.reconcile({"main": ["Friends"], "archived": []})
        row, = chats(self.ws)
        self.assertEqual((row["chatKey"], row["kind"]), ("custom", "group"))

    def test_disabled_chat_is_not_silently_reenabled_by_population_discovery(self):
        """Catch all-chat enrollment overriding an explicit privacy exclusion."""
        from localgraph.whatsapp import configure_chat, chats
        configure_chat(self.ws, account_key="self", chat_key="excluded", title="Excluded",
                       kind="direct", date_order="mdy", timezone_name="UTC", enabled=False)
        result = self.reconcile({"main": ["Excluded"], "archived": []})
        self.assertEqual(result["excludedChats"], 1)
        self.assertFalse(chats(self.ws)[0]["enabled"])

    def test_acquisition_does_not_accept_existing_or_ambiguous_downloads(self):
        """Catch reusing an old export or arbitrarily binding simultaneous downloads."""
        folder = Path(self.tmp.name) / "downloads"
        folder.mkdir()
        (folder / "old.zip").write_bytes(b"old")
        before = self.acq.download_snapshot(folder)
        with self.assertRaisesRegex(ValueError, "export-not-delivered"):
            self.acq.new_download(folder, before)
        (folder / "one.zip").write_bytes(b"one")
        self.assertEqual(self.acq.new_download(folder, before).name, "one.zip")
        (folder / "two.zip").write_bytes(b"two")
        with self.assertRaisesRegex(ValueError, "ambiguous-download"):
            self.acq.new_download(folder, before)

    def test_identity_and_ui_protocol_reject_malformed_results_without_bodies(self):
        """Catch accepting arbitrary script stdout as a verified inventory or export."""
        for raw in ("private message", "{}", '{"status":"ok"}', '[1]'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.acq.parse_driver_result(raw, "inventory")

    @unittest.skipUnless(shutil.which("osascript"), "native AppleScript requires macOS")
    def test_native_script_rejects_unknown_operation_before_touching_ui(self):
        """Catch a permissive driver defaulting unexpected input to real UI actions."""
        import subprocess
        script = Path(self.acq.__file__).with_name("whatsapp_native.applescript")
        self.assertTrue(script.exists(), "native AppleScript driver is missing")
        result = subprocess.run(["/usr/bin/osascript", str(script), "invalid-operation"],
                                capture_output=True, text=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsupported-operation", result.stderr)

    def test_full_run_materializes_every_discovered_chat_and_repeat_is_idempotent(self):
        """Catch only acquiring preexisting bindings or counting exports without import/render."""
        self.assertTrue(callable(getattr(self.acq, "run_acquisition", None)), "acquisition runner is missing")
        self.acq.configure_acquisition(self.ws, account="self", expected_profile="Self",
                                       date_order="mdy", timezone_name="UTC")
        downloads = Path(self.tmp.name) / "downloads"
        downloads.mkdir()
        counter = 0
        def driver(operation, profile, *args):
            nonlocal counter
            self.assertEqual(profile, "Self")
            if operation == "inventory":
                return {"operation": operation, "status": "ok", "profile": "Self",
                        "lists": {"main": ["Friends"], "archived": ["Old group"]}}
            list_name, title = args
            self.assertEqual(list_name, "main" if title == "Friends" else "archived")
            counter += 1
            with zipfile.ZipFile(downloads / f"WhatsApp Chat - {title} ({counter}).zip", "w") as z:
                z.writestr("_chat.txt", "[8/29/26, 10:00 AM] Synthetic: fixture\n")
            return {"operation": operation, "status": "ok", "title": title, "mediaRequested": False, "downloadObserved": True}
        for _ in range(2):
            result = self.acq.run_acquisition(self.ws, downloads=downloads, driver=driver, poll_seconds=0.01)
            self.assertEqual(result["status"], "local-current")
            self.assertEqual(result["population"]["currentChats"], 2)
            self.assertTrue(result["population"]["populationCovered"])
            self.assertFalse(result["population"]["historicalComplete"])
            self.assertNotIn("fixture", json.dumps(result))
        from localgraph.whatsapp import run_whatsapp_sync
        self.assertEqual([r["messages"] for r in run_whatsapp_sync(self.ws)["chats"]], [1, 1])

    def test_failed_native_export_does_not_stop_independent_chat_or_leak_error(self):
        """Catch one unsupported chat halting all acquisition or logging provider text."""
        self.assertTrue(callable(getattr(self.acq, "run_acquisition", None)), "acquisition runner is missing")
        self.acq.configure_acquisition(self.ws, account="self", expected_profile="Self",
                                       date_order="mdy", timezone_name="UTC")
        downloads = Path(self.tmp.name) / "downloads"
        downloads.mkdir()
        def driver(operation, profile, *args):
            if operation == "inventory":
                return {"operation": operation, "status": "ok", "profile": "Self",
                        "lists": {"main": ["Broken", "Good"], "archived": []}}
            _, title = args
            if title == "Broken":
                raise ValueError("private contents must not be logged")
            with zipfile.ZipFile(downloads / "WhatsApp Chat - Good.zip", "w") as z:
                z.writestr("_chat.txt", "[8/29/26, 10:00 AM] Synthetic: fixture\n")
            return {"operation": "export", "status": "ok", "title": title, "mediaRequested": True, "downloadObserved": True}
        result = self.acq.run_acquisition(self.ws, downloads=downloads, driver=driver, poll_seconds=0.01)
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["population"]["currentChats"], 1)
        self.assertFalse(result["population"]["populationCovered"])
        self.assertNotIn("private contents", json.dumps(result))

    def test_installer_requires_verified_acquisition_before_scheduling(self):
        """Catch enabling a new persistent export driver before its live acceptance."""
        self.assertTrue(callable(getattr(self.acq, "install_acquisition", None)), "installer is missing")
        with self.assertRaisesRegex(ValueError, "acceptance-required"):
            self.acq.install_acquisition(self.ws, home=Path(self.tmp.name) / "home")

    def test_cli_explicitly_configures_full_population_and_inventory_is_in_status(self):
        """Catch shipping a library-only feature or hiding missing population evidence."""
        from localgraph.cli import build_parser
        parser = build_parser()
        self.assertIn("configure-whatsapp-acquisition", parser.format_help(), "public acquisition command is missing")
        args = parser.parse_args(["configure-whatsapp-acquisition", "--account", "self", "--expected-profile", "Self",
                                  "--date-order", "mdy", "--timezone", "UTC", "--all-chats"])
        self.assertTrue(args.all_chats)
        self.acq.configure_acquisition(self.ws, account="self", expected_profile="Self",
                                       date_order="mdy", timezone_name="UTC")
        self.reconcile({"main": ["Friends"], "archived": []})
        from localgraph.status import build_localgraph_status
        report = build_localgraph_status(self.ws, home=Path(self.tmp.name), launchctl=lambda _: (113, ""))
        account = report["sources"]["whatsapp"]["accounts"][0]
        self.assertIn("population", account, "unified health omits chat population")
        self.assertFalse(account["population"]["populationCovered"])
        self.assertIn("whatsapp-population-incomplete", {r["code"] for r in account["findings"]})

    def test_lock_refuses_concurrent_native_acquisition(self):
        """Catch two GUI drivers selecting and misbinding each other's exports."""
        self.acq.configure_acquisition(self.ws, account="self", expected_profile="Self",
                                       date_order="mdy", timezone_name="UTC")
        with self.acq.acquisition_lock(self.ws) as acquired:
            self.assertTrue(acquired)
            result = self.acq.run_acquisition(self.ws, driver=lambda *args: self.fail("second driver ran"))
        self.assertEqual(result["status"], "skipped-concurrent")

    def test_installer_accepts_only_repeated_exact_candidate_native_receipts(self):
        """Catch stale or single-run evidence authorizing a persistent replacement."""
        self.assertTrue(callable(getattr(self.acq, "candidate_hash", None)), "candidate acceptance binding is missing")
        from localgraph.whatsapp import _write, _iso, _load
        from datetime import datetime, timezone
        self.acq.configure_acquisition(self.ws, account="self", expected_profile="Self",
                                       date_order="mdy", timezone_name="UTC")
        journal = self.ws.state_dir / "whatsapp-acquisition/self/acquisition-runs"
        receipt = {"nativeDriver": True, "candidateSha256": self.acq.candidate_hash(),
                   "policySha256": self.acq.policy_hash(_load(self.ws.config_path)["imports"]["whatsapp"]["acquisition"]),
                   "finishedAt": _iso(datetime.now(timezone.utc)),
                   "acceptedChatKeys": ["friends", "old-group"]}
        _write(journal / "one.json", receipt)
        with self.assertRaisesRegex(ValueError, "acceptance-required"):
            self.acq.install_acquisition(self.ws, home=Path(self.tmp.name) / "home")
        _write(journal / "two.json", {**receipt, "candidateSha256": "old-candidate"})
        with self.assertRaisesRegex(ValueError, "acceptance-required"):
            self.acq.install_acquisition(self.ws, home=Path(self.tmp.name) / "home")
        _write(journal / "two.json", receipt)
        installed = self.acq.install_acquisition(self.ws, home=Path(self.tmp.name) / "home")
        import plistlib
        payload = plistlib.loads(Path(installed["plist"]).read_bytes())
        self.assertEqual(payload["StartCalendarInterval"], {"Hour": 9, "Minute": 0})
        self.assertTrue((Path(installed["runtime"]) / "localgraph/whatsapp_native.applescript").is_file())
        self.assertEqual(Path(installed["script"]).stat().st_mode & 0o777, 0o700)

    def test_virtualized_pages_require_overlap_and_both_boundaries(self):
        """Catch one visible screen, skipped pages, or unproven scroll endpoints counted as all chats."""
        self.assertTrue(callable(getattr(self.acq, "merge_pages", None)), "paged chat discovery is missing")
        self.assertEqual(self.acq.merge_pages({"topReached": True, "bottomReached": True,
            "pages": [["A", "B", "C"], ["C", "D", "E"], ["D", "E", "F"]]}),
            ["A", "B", "C", "D", "E", "F"])
        for scan in ({"topReached": False, "bottomReached": True, "pages": [["A"]]},
                     {"topReached": True, "bottomReached": False, "pages": [["A"]]},
                     {"topReached": True, "bottomReached": True, "pages": [["A"], ["B"]]}):
            with self.subTest(scan=scan), self.assertRaisesRegex(ValueError, "inventory-incomplete"):
                self.acq.merge_pages(scan)

    def test_population_status_does_not_hide_a_configured_account_without_inventory(self):
        """Catch a newly authorized account disappearing from unified status until first discovery."""
        self.acq.configure_acquisition(self.ws, account="self", expected_profile="Self",
                                       date_order="mdy", timezone_name="UTC")
        from localgraph.status import build_localgraph_status
        report = build_localgraph_status(self.ws, home=Path(self.tmp.name), launchctl=lambda _: (113, ""))
        accounts = report["sources"]["whatsapp"]["accounts"]
        self.assertEqual(len(accounts), 1)
        self.assertFalse(accounts[0]["population"]["populationCovered"])

    def test_paged_protocol_rejects_invented_boundaries_and_ambiguous_overlap(self):
        """Catch truthy non-booleans or duplicate labels hiding skipped conversations."""
        with self.assertRaisesRegex(ValueError, "inventory-incomplete"):
            self.acq.merge_pages({"topReached": "false", "bottomReached": True, "pages": [["A"]]})
        with self.assertRaisesRegex(ValueError, "inventory-incomplete"):
            self.acq.merge_pages({"topReached": True, "bottomReached": True,
                                  "pages": [["A", "B", "B"], ["B", "B", "C"]]})

    def test_inventory_cannot_overwrite_a_chat_named_inventory(self):
        """Catch account-ledger filenames colliding with valid per-chat status keys."""
        from localgraph.whatsapp import configure_chat, _write, _load
        configure_chat(self.ws, account_key="self", chat_key="inventory", title="Friends",
                       kind="direct", date_order="mdy", timezone_name="UTC")
        path = self.ws.state_dir / "whatsapp/self/inventory.json"
        _write(path, {"status": "local-current", "messages": 10})
        self.reconcile({"main": ["Friends"], "archived": []})
        self.assertEqual(_load(path), {"status": "local-current", "messages": 10})

    def test_malformed_page_metadata_is_a_body_free_validation_error(self):
        """Catch malformed native protocol crashing outside the controlled failure path."""
        for pages in ("private text", None, [], {"main": None}, {"main": {"pages": 4}}):
            with self.subTest(pages=pages), self.assertRaises(ValueError):
                self.acq.parse_driver_result(json.dumps({"operation": "inventory", "status": "ok",
                                                        "profile": "Self", "pages": pages}), "inventory")

    def test_locked_or_other_user_session_never_authorizes_native_control(self):
        """Catch background jobs manipulating a locked or different user's desktop."""
        self.assertTrue(callable(getattr(self.acq, "desktop_available", None)), "desktop preflight is missing")
        snapshot = {"IOConsoleLocked": False, "IOConsoleUsers": [
            {"kCGSSessionOnConsoleKey": True, "kCGSSessionUserIDKey": 123}]}
        self.assertTrue(self.acq.desktop_available(snapshot=snapshot, uid=123))
        self.assertFalse(self.acq.desktop_available(snapshot={**snapshot, "IOConsoleLocked": True}, uid=123))
        self.assertFalse(self.acq.desktop_available(snapshot=snapshot, uid=456))
        self.assertFalse(self.acq.desktop_available(snapshot={}, uid=123))
        for malformed in (None, 3, "private text", {}):
            self.assertFalse(self.acq.desktop_available(snapshot={**snapshot, "IOConsoleUsers": malformed}, uid=123))

    def test_inventory_failure_cannot_be_hidden_by_previous_population_receipt(self):
        """Catch failed discovery being laundered by an older still-fresh inventory."""
        self.reconcile({"main": ["Friends"], "archived": []})
        from localgraph.whatsapp import _write, chats
        key = chats(self.ws)[0]["chatKey"]
        _write(self.ws.state_dir / "whatsapp-acquisition/self/acquisition.json",
               {"status": "degraded", "error": "inventory-or-identity-unverified"})
        result = self.acq.population_status(self.ws, "self", current_keys={key})
        self.assertFalse(result["populationCovered"])

    def test_malformed_acquisition_times_degrade_status_without_crashing_other_chats(self):
        """Catch malformed native timestamps taking down the unified health command."""
        self.reconcile({"main": ["Friends", "Other"], "archived": []})
        from localgraph.whatsapp import _write, chats
        key = chats(self.ws)[0]["chatKey"]
        _write(self.ws.state_dir / "whatsapp/self" / f"{key}.json", {"lastNativeExportAt": "invalid"})
        _write(self.ws.sources_dir / "whatsapp/self" / key / "acquisition-failure.json", {"checkedAt": 7, "error": "export-failed"})
        from localgraph.status import build_localgraph_status
        report = build_localgraph_status(self.ws, home=Path(self.tmp.name), launchctl=lambda _: (113, ""))
        account = report["sources"]["whatsapp"]["accounts"][0]
        self.assertEqual(len(account["chats"]), 2)
        self.assertIn("whatsapp-metadata-invalid", {f["code"] for f in account["findings"]})

    def test_account_or_session_failure_stops_further_native_actions(self):
        """Catch a global safety failure being treated as an isolated unsupported chat."""
        for reason in ("session-unavailable", "identity-unverified", "app-disconnected",
                       "export-control-changed", "export-not-delivered"):
            with self.subTest(reason=reason):
                self.acq.configure_acquisition(self.ws, account="self", expected_profile="Self",
                                               date_order="mdy", timezone_name="UTC")
                downloads = Path(self.tmp.name) / "downloads"
                downloads.mkdir(exist_ok=True)
                attempted = []
                def driver(operation, profile, *args):
                    if operation == "inventory":
                        return {"operation": operation, "status": "ok", "profile": profile,
                                "lists": {"main": ["First", "Second"], "archived": []}}
                    attempted.append(args[-1])
                    raise ValueError(reason)
                result = self.acq.run_acquisition(self.ws, downloads=downloads, driver=driver)
                self.assertEqual(attempted, ["First"])
                self.assertEqual(result["error"], reason)
                self.assertEqual(result["chats"][0]["error"], reason)

    def test_inventory_reports_safe_failure_reason_without_provider_text(self):
        """Catch hiding actionable locked-desktop failures behind a generic error."""
        self.acq.configure_acquisition(self.ws, account="self", expected_profile="Self",
                                       date_order="mdy", timezone_name="UTC")
        def driver(*args):
            raise ValueError("session-unavailable")
        result = self.acq.run_acquisition(self.ws, driver=driver)
        self.assertEqual(result["error"], "session-unavailable")

    def test_future_inventory_timestamp_cannot_establish_fresh_population(self):
        """Catch future-dated evidence remaining fresh indefinitely."""
        from localgraph.whatsapp import _write, chats
        report = self.reconcile({"main": ["Friends"], "archived": []})
        _write(self.ws.state_dir / "whatsapp-acquisition/self/inventory.json",
               {**report, "observedAt": "2099-01-01T00:00:00Z"})
        result = self.acq.population_status(self.ws, "self", current_keys={chats(self.ws)[0]["chatKey"]})
        self.assertFalse(result["inventoryFresh"])
        self.assertFalse(result["populationCovered"])

    def test_changed_binding_during_export_cannot_relabel_new_archive(self):
        """Catch concurrent configuration changes laundering an old UI selection into a new title."""
        from localgraph.whatsapp import configure_chat, chats
        self.acq.configure_acquisition(self.ws, account="self", expected_profile="Self",
                                       date_order="mdy", timezone_name="UTC")
        downloads = Path(self.tmp.name) / "downloads"
        downloads.mkdir()
        def driver(operation, profile, *args):
            if operation == "inventory":
                return {"operation": operation, "status": "ok", "profile": profile,
                        "lists": {"main": ["Original"], "archived": []}}
            key = chats(self.ws)[0]["chatKey"]
            configure_chat(self.ws, account_key="self", chat_key=key, title="Different",
                           kind="group", date_order="mdy", timezone_name="UTC")
            with zipfile.ZipFile(downloads / "WhatsApp Chat - Original.zip", "w") as z:
                z.writestr("_chat.txt", "[8/29/26, 10:00 AM] Synthetic: fixture\n")
            return {"operation": operation, "status": "ok", "title": "Original", "mediaRequested": False, "downloadObserved": True}
        result = self.acq.run_acquisition(self.ws, downloads=downloads, driver=driver, poll_seconds=0.01)
        self.assertEqual(result["acceptedChatKeys"], [])
        self.assertFalse(list(self.ws.sources_dir.glob("whatsapp/*/*/archives/*.zip")))

    def test_install_acceptance_cannot_cross_account_policy_change(self):
        """Catch old live receipts enabling automation after an account identity change."""
        from localgraph.whatsapp import _write, _load, _iso
        from datetime import datetime, timezone
        self.assertTrue(callable(getattr(self.acq, "policy_hash", None)), "policy acceptance binding is missing")
        self.acq.configure_acquisition(self.ws, account="self", expected_profile="Self",
                                       date_order="mdy", timezone_name="UTC")
        policy = _load(self.ws.config_path)["imports"]["whatsapp"]["acquisition"]
        journal = self.ws.state_dir / "whatsapp-acquisition/self/acquisition-runs"
        receipt = {"nativeDriver": True, "candidateSha256": self.acq.candidate_hash(),
                   "policySha256": self.acq.policy_hash(policy),
                   "finishedAt": _iso(datetime.now(timezone.utc)), "acceptedChatKeys": ["one", "two"]}
        for name in ("first", "second"):
            _write(journal / f"{name}.json", receipt)
        self.acq.configure_acquisition(self.ws, account="self", expected_profile="Other",
                                       date_order="mdy", timezone_name="UTC")
        with self.assertRaisesRegex(ValueError, "acceptance-required"):
            self.acq.install_acquisition(self.ws, home=Path(self.tmp.name) / "home")

    def test_unified_status_reports_latest_inventory_failure_separately(self):
        """Catch a failed acquisition being hidden in an undifferentiated coverage warning."""
        self.acq.configure_acquisition(self.ws, account="self", expected_profile="Self",
                                       date_order="mdy", timezone_name="UTC")
        self.reconcile({"main": ["Friends"], "archived": []})
        from localgraph.whatsapp import _write
        _write(self.ws.state_dir / "whatsapp-acquisition/self/acquisition.json",
               {"status": "degraded", "error": "session-unavailable"})
        from localgraph.status import build_localgraph_status
        report = build_localgraph_status(self.ws, home=Path(self.tmp.name), launchctl=lambda _: (113, ""))
        account = report["sources"]["whatsapp"]["accounts"][0]
        self.assertEqual(account["population"]["lastError"], "session-unavailable")
        self.assertIn("native-population-acquisition-failed", {f["code"] for f in account["findings"]})

    def test_native_driver_terminates_when_desktop_locks_during_operation(self):
        """Catch a long-running UI script continuing after its session becomes unavailable."""
        from unittest.mock import Mock, patch
        import subprocess
        process = Mock()
        process.communicate.side_effect = [subprocess.TimeoutExpired("osascript", 2), ("", "")]
        with patch.object(self.acq, "desktop_available", side_effect=[True, False]), \
             patch.object(self.acq.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(ValueError, "session-unavailable"):
                self.acq.native_driver("inventory", "Self")
        process.kill.assert_called_once()
        self.assertEqual(process.communicate.call_count, 2)

    def test_malformed_inventory_timestamp_does_not_crash_unified_status(self):
        """Catch a corrupt inventory ledger taking every other source's status offline."""
        report = self.reconcile({"main": ["Friends"], "archived": []})
        from localgraph.whatsapp import _write
        _write(self.ws.state_dir / "whatsapp-acquisition/self/inventory.json", {**report, "observedAt": 123})
        from localgraph.status import build_localgraph_status
        status = build_localgraph_status(self.ws, home=Path(self.tmp.name), launchctl=lambda _: (113, ""))
        self.assertFalse(status["sources"]["whatsapp"]["accounts"][0]["population"]["populationCovered"])

    def test_low_disk_space_stops_before_requesting_an_export(self):
        """Catch automatic exports consuming the last available local storage."""
        from unittest.mock import patch
        from types import SimpleNamespace
        self.acq.configure_acquisition(self.ws, account="self", expected_profile="Self",
                                       date_order="mdy", timezone_name="UTC")
        downloads = Path(self.tmp.name) / "downloads"
        downloads.mkdir()
        actions = []
        def driver(operation, profile, *args):
            if operation == "inventory":
                return {"operation": operation, "status": "ok", "profile": profile,
                        "lists": {"main": ["Friends"], "archived": []}}
            actions.append(operation)
            raise ValueError("unexpected export")
        with patch.object(self.acq.shutil, "disk_usage", return_value=SimpleNamespace(free=1)):
            result = self.acq.run_acquisition(self.ws, downloads=downloads, driver=driver)
        self.assertEqual(actions, [])
        self.assertEqual(result["error"], "insufficient-disk-space")
        self.assertFalse(list(self.ws.sources_dir.glob("whatsapp/*/*/archives/*.zip")))

    def test_export_protocol_requires_observed_native_completion(self):
        """Catch treating an export click or media choice as completed native delivery."""
        for observed in (None, False, "true", 1):
            with self.subTest(observed=observed), self.assertRaises(ValueError):
                self.acq.parse_driver_result(json.dumps({"operation": "export", "status": "ok",
                    "title": "Friends", "mediaRequested": False, "downloadObserved": observed}), "export")

    def test_chat_kind_disambiguates_a_contact_and_group_with_the_same_title(self):
        """Catch conflating a known direct conversation with a same-named group."""
        from localgraph.whatsapp import configure_chat, chats
        configure_chat(self.ws, account_key="self", chat_key="existing", title="Same name",
                       kind="direct", date_order="mdy", timezone_name="UTC")
        try:
            report = self.reconcile({"main": [{"title": "Same name", "kind": "direct"},
                                              {"title": "Same name", "kind": "group"}], "archived": []})
        except ValueError:
            self.fail("verified native chat-kind evidence is not accepted")
        self.assertEqual(report["configuredChats"], 2)
        self.assertEqual(report["ambiguousChats"], 0)
        direct, = [r for r in chats(self.ws) if r["kind"] == "direct"]
        self.assertEqual(direct["chatKey"], "existing")
        self.assertEqual(len({r["chatKey"] for r in chats(self.ws)}), 2)

    def test_same_kind_or_unknown_kind_collisions_remain_ambiguous(self):
        """Catch kind evidence being overextended to distinguish two groups or an unknown chat."""
        for kinds in (("group", "group"), ("direct", "unknown")):
            try:
                report = self.reconcile({"main": [{"title": "Same", "kind": k} for k in kinds], "archived": []})
            except ValueError:
                self.fail("native chat-kind observations are not supported")
            self.assertEqual(report["configuredChats"], 0)
            self.assertEqual(report["ambiguousChats"], 2)

    def test_disappeared_unresolved_chats_remain_in_the_population_ledger(self):
        """Catch a duplicate shrinking to one row being promoted to a verified identity."""
        self.reconcile({"main": ["Twins", "Twins", "Stable"], "archived": []})
        for _ in range(2):
            report = self.reconcile({"main": ["Twins", "Stable"], "archived": []})
            self.assertEqual(report["missingPreviouslySeenChats"], 1)
            self.assertEqual(report["configuredChats"], 1)
            self.assertEqual(report["ambiguousChats"], 1)
        report = self.reconcile({"main": ["Stable"], "archived": []})
        self.assertEqual(report["missingPreviouslySeenChats"], 2)
        public = self.acq.population_status(self.ws, "self", current_keys=set())
        self.assertNotIn("Twins", json.dumps(public))
        report = self.reconcile({"main": [{"title": "Twins", "kind": "direct"},
                                          {"title": "Twins", "kind": "group"},
                                          "Stable"], "archived": []})
        self.assertEqual(report["missingPreviouslySeenChats"], 0)
        self.assertEqual(report["configuredChats"], 3)

    def test_paged_kind_evidence_preserves_overlap_without_conflating_chats(self):
        """Catch paginated typed identities becoming strings or losing a same-title group."""
        a = {"title": "Same", "kind": "direct"}
        b = {"title": "Same", "kind": "group"}
        c = {"title": "Other", "kind": "direct"}
        try:
            result = self.acq.merge_pages({"topReached": True, "bottomReached": True, "pages": [[a, b], [b, c]]})
        except ValueError:
            self.fail("typed native inventory pages are not supported")
        self.assertEqual(result, [a, b, c])

    @unittest.skipUnless(shutil.which("osacompile"), "native AppleScript requires macOS")
    def test_native_confirmation_guard_uses_observed_alert_fields(self):
        """Catch reading generic AX descriptions instead of the actual AppKit alert labels."""
        import subprocess
        compiled = Path(self.tmp.name) / "driver.scpt"
        result = subprocess.run(["/usr/bin/osacompile", "-o", str(compiled),
            str(Path(self.acq.__file__).with_name("whatsapp_native.applescript"))], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        probe = 'use scripting additions\nset driver to (load script (POSIX file ' + json.dumps(str(compiled)) + '))\n'
        probe += '''return {driver's isExportConfirmation("‎Exported chat was saved to Downloads Folder", "‎OK", "action-button--999"), driver's isExportConfirmation("text", "button", "action-button--999"), driver's isExportConfirmation("Delete chat?", "OK", "action-button--999"), driver's isExportConfirmation("Exported chat was saved to Downloads Folder", "OK", "other-button")}'''
        result = subprocess.run(["/usr/bin/osascript", "-e", probe], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "true, false, false, false")

    def test_typed_native_acquisition_keeps_same_named_conversations_separate(self):
        """Catch losing the kind qualifier between inventory, native selection and cumulative import."""
        self.acq.configure_acquisition(self.ws, account="self", expected_profile="Self",
                                       date_order="mdy", timezone_name="UTC")
        downloads = Path(self.tmp.name) / "downloads"
        downloads.mkdir()
        counter = 0
        def driver(operation, profile, *args):
            nonlocal counter
            if operation == "inventory":
                return {"operation": operation, "status": "ok", "profile": profile,
                        "lists": {"main": [{"title": "Same", "kind": "direct"},
                                            {"title": "Same", "kind": "group"}], "archived": []}}
            list_name, title, kind = args
            self.assertEqual((list_name, title), ("main", "Same"))
            self.assertIn(kind, {"direct", "group"})
            counter += 1
            with zipfile.ZipFile(downloads / f"WhatsApp Chat - Same ({counter}).zip", "w") as z:
                z.writestr("_chat.txt", f"[8/29/26, 10:00 AM] Synthetic: {kind} fixture\n")
            return {"operation": operation, "status": "ok", "title": title, "kind": kind,
                    "mediaRequested": False, "downloadObserved": True}
        for _ in range(2):
            result = self.acq.run_acquisition(self.ws, downloads=downloads, driver=driver, poll_seconds=0.01)
            self.assertEqual(result["status"], "local-current")
            self.assertEqual(len(result["acceptedChatKeys"]), 2)
        from localgraph.whatsapp import run_whatsapp_sync
        self.assertEqual([r["messages"] for r in run_whatsapp_sync(self.ws)["chats"]], [1, 1])

    @unittest.skipUnless(shutil.which("osacompile"), "native AppleScript requires macOS")
    def test_native_inventory_excludes_offscreen_cached_rows(self):
        """Catch requesting menus on UIKit's cached cells outside the visible list viewport."""
        import subprocess
        compiled = Path(self.tmp.name) / "driver.scpt"
        result = subprocess.run(["/usr/bin/osacompile", "-o", str(compiled),
            str(Path(self.acq.__file__).with_name("whatsapp_native.applescript"))], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        probe = 'use scripting additions\nset driver to (load script (POSIX file ' + json.dumps(str(compiled)) + '))\n'
        probe += '''set viewport to driver's chatViewport({100, -73}, {375, 1055}, {100, -73}, {375, 92})
return {driver's isCenterVisible({100, -116}, {375, 69}, item 1 of viewport, item 2 of viewport), driver's isCenterVisible({100, -47}, {375, 69}, item 1 of viewport, item 2 of viewport), driver's isCenterVisible({100, 22}, {375, 69}, item 1 of viewport, item 2 of viewport), driver's isCenterVisible({100, 1000}, {375, 69}, item 1 of viewport, item 2 of viewport), driver's isCenterVisible({100, 0}, {0, 0}, item 1 of viewport, item 2 of viewport)}'''
        result = subprocess.run(["/usr/bin/osascript", "-e", probe], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "false, false, true, false, false")


if __name__ == "__main__":
    unittest.main()
