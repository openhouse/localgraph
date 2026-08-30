"""Resumable acquisition contracts; provider observations and ZIPs are synthetic."""
import json
import inspect
import contextlib
import io
import shutil
import subprocess
import plistlib
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from localgraph import whatsapp_acquisition as acq
from localgraph.paths import Workspace
from localgraph.whatsapp import _load, _write, configure_chat, run_whatsapp_sync


class WhatsAppRefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Workspace(Path(self.tmp.name) / "graph")
        self.downloads = Path(self.tmp.name) / "downloads"
        self.downloads.mkdir()
        acq.configure_acquisition(self.ws, account="self", expected_profile="Self",
                                  date_order="mdy", timezone_name="UTC")
        for key, title in (("first", "First"), ("second", "Second")):
            configure_chat(self.ws, account_key="self", chat_key=key, title=title,
                           kind="direct", date_order="mdy", timezone_name="UTC")
        self.exports = []

    def driver(self, operation, profile, *args):
        self.assertEqual(profile, "Self")
        if operation == "resolve":
            title, = args
            return {"operation": "resolve", "status": "ok", "profile": profile,
                    "lists": {"main": [{"title": title, "kind": "direct"}], "archived": []}}
        self.assertEqual(operation, "export", "refresh must not depend on discovery")
        list_name, title, kind = args
        self.assertEqual((list_name, kind), ("main", "direct"))
        self.exports.append(title)
        with zipfile.ZipFile(self.downloads / f"WhatsApp Chat - {title} ({len(self.exports)}).zip", "w") as archive:
            archive.writestr("_chat.txt", "[8/29/26, 10:00 AM] Synthetic: fixture\n")
        return {"operation": "export", "status": "ok", "title": title, "kind": kind,
                "downloadObserved": True, "mediaRequested": False}

    def refresh(self, **kwargs):
        self.assertIn("refresh_only", inspect.signature(acq.run_acquisition).parameters,
                      "independent resumable refresh is missing")
        return acq.run_acquisition(self.ws, downloads=self.downloads, driver=kwargs.pop("driver", self.driver),
                                   poll_seconds=0.01, refresh_only=True, resume=True, **kwargs)

    def test_refresh_bypasses_failed_discovery_without_laundering_population_health(self):
        """Catch a population failure blocking verified bindings or being erased by refresh."""
        _write(self.ws.state_dir / "whatsapp-acquisition/self/discovery.json",
               {"status": "degraded", "error": "inventory-scroll-stalled"})
        result = self.refresh()
        self.assertEqual(result["refreshStatus"], "current")
        self.assertEqual(result["acceptedChatKeys"], ["first", "second"])
        self.assertEqual(result["population"]["discoveryError"], "inventory-scroll-stalled")
        self.assertFalse(result["population"]["populationCovered"])
        self.assertEqual([r["messages"] for r in run_whatsapp_sync(self.ws)["chats"]], [1, 1])
        from localgraph.status import build_localgraph_status
        account = build_localgraph_status(self.ws, home=Path(self.tmp.name), launchctl=lambda _: (113, ""))["sources"]["whatsapp"]["accounts"][0]
        self.assertIn("native-population-discovery-failed", {f["code"] for f in account["findings"]})

    def test_interrupted_refresh_checkpoints_each_chat_and_resumes_without_reexport(self):
        """Catch a later interruption losing earlier imports or duplicating completed work."""
        def interrupted(operation, profile, *args):
            if operation == "resolve" and args == ("Second",):
                raise KeyboardInterrupt
            return self.driver(operation, profile, *args)
        with self.assertRaises(KeyboardInterrupt):
            self.refresh(driver=interrupted)
        queue = _load(self.ws.state_dir / "whatsapp-acquisition/self/refresh-queue.json")
        self.assertEqual(queue["jobs"]["first"]["status"], "current")
        self.assertEqual([r["messages"] for r in run_whatsapp_sync(self.ws)["chats"] if r["chatKey"] == "first"], [1])
        result = self.refresh()
        self.assertEqual(self.exports, ["First", "Second"])
        self.assertEqual(result["skippedCurrentChatKeys"], ["first"])
        self.assertEqual(result["acceptedChatKeys"], ["second"])

    def test_refresh_batch_limit_and_failed_job_do_not_starve_pending_chats(self):
        """Catch a broken first job consuming every bounded refresh run."""
        def broken(operation, profile, *args):
            if operation == "export" and args[1] == "First":
                raise ValueError("export-unavailable")
            return self.driver(operation, profile, *args)
        first = self.refresh(driver=broken, max_chats=1)
        self.assertEqual(first["chats"][0]["chatKey"], "first")
        second = self.refresh(driver=broken, max_chats=1)
        self.assertEqual(second["acceptedChatKeys"], ["second"])
        self.assertEqual(self.exports, ["Second"])

    def test_resume_rechecks_binding_and_disable_before_any_native_action(self):
        """Catch saved queue entries retaining authority after configuration changes."""
        self.refresh()
        configure_chat(self.ws, account_key="self", chat_key="first", title="Renamed",
                       kind="direct", date_order="mdy", timezone_name="UTC")
        configure_chat(self.ws, account_key="self", chat_key="second", title="Second",
                       kind="direct", date_order="mdy", timezone_name="UTC", enabled=False)
        result = self.refresh()
        self.assertEqual(self.exports, ["First", "Second", "Renamed"])
        self.assertEqual(result["acceptedChatKeys"], ["first"])

    def test_binding_revoked_during_resolution_prevents_export(self):
        """Catch an identity read retaining export authority after a concurrent disable."""
        def revoked(operation, profile, *args):
            self.assertEqual(operation, "resolve", "revoked binding reached an export action")
            response = self.driver(operation, profile, *args)
            configure_chat(self.ws, account_key="self", chat_key="first", title="First",
                           kind="direct", date_order="mdy", timezone_name="UTC", enabled=False)
            return response
        result = self.refresh(driver=revoked, chat_keys=["first"])
        self.assertEqual(self.exports, [])
        self.assertEqual(result["error"], "identity-unverified")

    def test_policy_change_during_export_cannot_be_custodied_or_reported_current(self):
        """Catch in-flight exports being imported under a changed account policy."""
        def changed(operation, profile, *args):
            response = self.driver(operation, profile, *args)
            if operation == "export":
                acq.configure_acquisition(self.ws, account="self", expected_profile="Different",
                                          date_order="mdy", timezone_name="UTC")
            return response
        result = self.refresh(driver=changed, chat_keys=["first"])
        self.assertEqual(result["acceptedChatKeys"], [])
        self.assertEqual(result["refreshStatus"], "degraded")
        self.assertEqual(list((self.ws.sources_dir / "whatsapp/self/first/receipts").glob("*.json")), [])

    def test_delivery_rechecks_binding_inside_the_writer_lock(self):
        """Catch a same-title kind change between the last caller check and custody write."""
        real_delivery = acq.record_export
        def changed_before_lock(*args, **kwargs):
            configure_chat(self.ws, account_key="self", chat_key="first", title="First",
                           kind="group", date_order="mdy", timezone_name="UTC")
            return real_delivery(*args, **kwargs)
        with patch.object(acq, "record_export", side_effect=changed_before_lock):
            result = self.refresh(chat_keys=["first"])
        self.assertEqual(result["acceptedChatKeys"], [])
        self.assertEqual(result["error"], "identity-unverified")
        self.assertEqual(list((self.ws.sources_dir / "whatsapp/self/first/receipts").glob("*.json")), [])

    def test_final_canonical_validation_overrides_an_earlier_queue_checkpoint(self):
        """Catch a later custody failure leaving an earlier chat falsely accepted/current."""
        def tampered(operation, profile, *args):
            if operation == "resolve" and args == ("Second",):
                archive, = (self.ws.sources_dir / "whatsapp/self/first/archives").glob("*.zip")
                archive.write_bytes(b"broken synthetic custody")
            return self.driver(operation, profile, *args)
        result = self.refresh(driver=tampered)
        self.assertEqual(result["acceptedChatKeys"], ["second"])
        self.assertEqual(result["refreshStatus"], "degraded")

    def test_delivered_checkpoint_resumes_import_without_another_native_export(self):
        """Catch re-exporting an already custodied ZIP after interruption before import."""
        real_sync = acq.run_whatsapp_sync
        with patch.object(acq, "run_whatsapp_sync", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.refresh(chat_keys=["first"])
        self.assertEqual(self.exports, ["First"])
        result = self.refresh(chat_keys=["first"], driver=lambda *args: self.fail("custodied delivery must resume locally"))
        self.assertEqual(result["resumedDeliveredChatKeys"], ["first"])
        self.assertEqual(result["acceptedChatKeys"], [], "recovery is not a second native export for acceptance")
        self.assertEqual(result["refreshStatus"], "current")
        self.assertEqual(real_sync(self.ws)["chats"][0]["messages"], 1)

    def test_wrong_account_in_resolver_stops_all_following_actions(self):
        """Catch treating an account mismatch as one recoverable missing conversation."""
        calls = []
        def wrong(operation, profile, *args):
            calls.append(operation)
            return {"operation": "resolve", "status": "ok", "profile": "Other",
                    "lists": {"main": [], "archived": []}}
        result = self.refresh(driver=wrong)
        self.assertEqual(calls, ["resolve"])
        self.assertEqual(result["error"], "identity-unverified")
        self.assertEqual(result["acceptedChatKeys"], [])

    def test_lock_at_native_exit_is_reported_as_session_unavailable(self):
        """Catch a locking desktop being misreported as changed native controls."""
        from unittest.mock import Mock
        process = Mock(returncode=1)
        process.communicate.return_value = ("", "export-control-changed")
        with patch.object(acq, "desktop_available", side_effect=[True, False]), patch.object(acq.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(ValueError, "session-unavailable"):
                acq.native_driver("inventory", "Self")

    def test_discovery_does_not_erase_refresh_failure_and_due_check_avoids_native_work(self):
        """Catch discovery laundering refresh failures or needlessly recontrolling the UI."""
        path = self.ws.state_dir / "whatsapp-acquisition/self/acquisition.json"
        _write(path, {"status": "degraded", "error": "export-not-delivered"})
        def inventory(operation, profile, *args):
            self.assertEqual(operation, "inventory")
            return {"operation": operation, "status": "ok", "profile": profile,
                    "lists": {"main": ["First", "Second"], "archived": []}}
        acq.run_acquisition(self.ws, inventory_only=True, driver=inventory)
        self.assertEqual(_load(path).get("error"), "export-not-delivered")
        self.assertIn("discovery_if_due", inspect.signature(acq.run_acquisition).parameters)
        result = acq.run_acquisition(self.ws, inventory_only=True, discovery_if_due=True,
                                    driver=lambda *args: self.fail("fresh discovery should skip native control"))
        self.assertEqual(result["status"], "skipped-current-discovery")

    def test_unified_status_reports_the_independent_discovery_scheduler(self):
        """Catch a missing discovery job disappearing behind a healthy refresh job."""
        from localgraph.status import build_localgraph_status
        source = build_localgraph_status(self.ws, home=Path(self.tmp.name), launchctl=lambda _: (113, ""))["sources"]["whatsapp"]
        self.assertIn("discovery", source["scheduler"], "discovery scheduler health is missing")
        self.assertEqual(source["scheduler"]["discovery"]["status"], "missing")
        self.assertIn("discovery-launchagent-missing", {f["code"] for f in source["accounts"][0]["findings"]})

    def test_installed_schedulers_dispatch_independent_discovery_and_resumable_refresh(self):
        """Catch scheduling the coupled legacy command instead of two independent jobs."""
        from localgraph.whatsapp import _iso
        from datetime import datetime, timezone
        receipt = {"nativeDriver": True, "candidateSha256": acq.candidate_hash(),
                   "policySha256": acq.policy_hash(_load(self.ws.config_path)["imports"]["whatsapp"]["acquisition"]),
                   "finishedAt": _iso(datetime.now(timezone.utc)), "acceptedChatKeys": ["first", "second"]}
        journal = self.ws.state_dir / "whatsapp-acquisition/self/acquisition-runs"
        _write(journal / "one.json", receipt)
        _write(journal / "two.json", receipt)
        installed = acq.install_acquisition(self.ws, home=Path(self.tmp.name) / "home")
        self.assertIn("discoveryPlist", installed, "independent discovery scheduler is missing")
        # Substitute only the child-command boundary, then execute both real installed wrappers.
        (Path(installed["runtime"]) / "localgraph/__main__.py").write_text("import json,sys\nprint(json.dumps(sys.argv[1:]))\n")
        for key, command in (("plist", "whatsapp-refresh"), ("discoveryPlist", "whatsapp-discover")):
            payload = plistlib.loads(Path(installed[key]).read_bytes())
            self.assertTrue(payload["RunAtLoad"])
            self.assertGreater(payload["StartInterval"], 0)
            ran = subprocess.run(payload["ProgramArguments"], capture_output=True, text=True, check=True)
            args = json.loads(ran.stdout)
            self.assertEqual(args[:3], ["--root", str(self.ws.root), command])
            self.assertIn("--max-chats" if command == "whatsapp-refresh" else "--if-due", args)

    def test_delivered_recovery_does_not_accept_a_tampered_archive(self):
        """Catch a resumable checkpoint bypassing immutable-custody verification."""
        with patch.object(acq, "run_whatsapp_sync", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.refresh(chat_keys=["first"])
        archive, = (self.ws.sources_dir / "whatsapp/self/first/archives").glob("*.zip")
        archive.write_bytes(b"broken synthetic custody")
        result = self.refresh(chat_keys=["first"], driver=lambda *args: self.fail("corrupt custody requires attention, not silent re-export"))
        self.assertEqual(result["resumedDeliveredChatKeys"], [])
        self.assertEqual(result["refreshStatus"], "degraded")

    def test_resolver_rejects_ambiguous_or_other_account_before_export(self):
        """Catch stale title-only bindings selecting the first matching row."""
        def ambiguous(operation, profile, *args):
            self.assertEqual(operation, "resolve")
            return {"operation": "resolve", "status": "ok", "profile": profile,
                    "lists": {"main": [{"title": args[0], "kind": "direct"}] * 2, "archived": []}}
        result = self.refresh(driver=ambiguous)
        self.assertEqual(result["acceptedChatKeys"], [])
        self.assertEqual({r["error"] for r in result["chats"]}, {"chat-identity-ambiguous"})
        self.assertEqual(self.exports, [])

    def test_unknown_cached_kind_can_merge_with_observed_kind_but_conflict_cannot(self):
        """Catch virtualized offscreen metadata discarding a visible identity observation."""
        a = {"title": "A", "kind": "unknown"}
        unknown = {"title": "B", "kind": "unknown"}
        known = {"title": "B", "kind": "direct"}
        c = {"title": "C", "kind": "unknown"}
        scan = {"topReached": True, "bottomReached": True, "pages": [[a, unknown], [known, c]]}
        try:
            merged = acq.merge_pages(scan)
        except ValueError:
            self.fail("visible identity evidence cannot merge with a prefetched unknown row")
        self.assertEqual(merged, [a, known, c])
        scan["pages"] = [[a, known], [{"title": "B", "kind": "group"}, c]]
        with self.assertRaises(ValueError):
            acq.merge_pages(scan)

    @unittest.skipUnless(shutil.which("osacompile"), "native AppleScript requires macOS")
    def test_native_discovery_does_not_request_menus_for_unrelated_or_cached_rows(self):
        """Catch coupling passive inventory to every chat's interactive context menu."""
        compiled = Path(self.tmp.name) / "driver.scpt"
        result = subprocess.run(["/usr/bin/osacompile", "-o", str(compiled),
            str(Path(acq.__file__).with_name("whatsapp_native.applescript"))], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        probe = 'use scripting additions\nset driver to (load script (POSIX file ' + json.dumps(str(compiled)) + '))\n'
        probe += '''return {driver's shouldInspectRow("A", "", true), driver's shouldInspectRow("A", "B", true), driver's shouldInspectRow("A", "A", false), driver's shouldInspectRow("A", "A", true)}'''
        result = subprocess.run(["/usr/bin/osascript", "-e", probe], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "false, false, false, true")

    def test_refresh_cli_exposes_independent_discovery_and_bounded_force_acceptance(self):
        """Catch the operator command accidentally running full discovery before refresh."""
        from localgraph.cli import build_parser
        try:
            args = build_parser().parse_args(["whatsapp-refresh", "--chat", "first", "--max-chats", "2", "--force"])
        except SystemExit:
            self.fail("independent refresh CLI is missing")
        self.assertEqual(args.chat, ["first"])
        self.assertEqual(args.max_chats, 2)
        self.assertTrue(args.force)
        self.assertEqual(build_parser().parse_args(["whatsapp-discover"]).command, "whatsapp-discover")

    def test_refresh_cli_exit_code_distinguishes_pending_population_from_failed_export(self):
        """Catch a healthy bounded refresh being reported as a failed scheduled job."""
        from localgraph.cli import main
        output = io.StringIO()
        with patch.object(acq, "native_driver", side_effect=self.driver), contextlib.redirect_stdout(output):
            code = main(["--root", str(self.ws.root), "whatsapp-refresh", "--max-chats", "1",
                         "--downloads", str(self.downloads)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["refreshStatus"], "pending")
        self.assertFalse(json.loads(output.getvalue())["population"]["populationCovered"])
        def failed(operation, profile, *args):
            if operation == "export":
                raise ValueError("export-unavailable")
            return self.driver(operation, profile, *args)
        output = io.StringIO()
        with patch.object(acq, "native_driver", side_effect=failed), contextlib.redirect_stdout(output):
            code = main(["--root", str(self.ws.root), "whatsapp-refresh", "--max-chats", "1",
                         "--downloads", str(self.downloads)])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue())["refreshStatus"], "degraded")


if __name__ == "__main__":
    unittest.main()
