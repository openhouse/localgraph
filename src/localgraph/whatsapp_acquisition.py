"""Deterministic native acquisition; account population is not phone history."""
from __future__ import annotations

import hashlib
import json
import contextlib
import fcntl
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .automation import instagram_sync_lock
from .paths import Workspace
from .whatsapp import _iso, _key, _load, _write, _time, chats, record_export, record_acquisition_failure, run_whatsapp_sync, MAX_ARCHIVE_BYTES

LABEL = "com.openhouse.localgraph.whatsapp-acquire"
DISCOVERY_LABEL = "com.openhouse.localgraph.whatsapp-discover"
SESSION_FAILURES = {"session-unavailable", "identity-unverified", "app-disconnected", "filtered-inventory",
                    "export-control-changed", "export-not-delivered", "invalid-native-driver-result", "insufficient-disk-space"}
SAFE_FAILURES = SESSION_FAILURES | {"export-control-changed", "export-unavailable", "export-failed",
    "export-not-delivered", "ambiguous-download", "inventory-incomplete", "inventory-scroll-stalled",
    "inventory-changed-during-scan", "invalid-native-driver-result", "candidate-changed-during-run",
    "chat-identity-ambiguous", "chat-not-found", "delivery-recovery-failed"}


def safe_failure(error: Exception, fallback: str = "export-failed") -> str:
    return str(error) if str(error) in SAFE_FAILURES else fallback


def clean_title(value: str) -> str:
    return "".join(c for c in value if unicodedata.category(c) != "Cf").strip()


def chat_observation(row) -> tuple[str, str]:
    if isinstance(row, str):
        title, kind = row, "unknown"
    elif isinstance(row, dict):
        title, kind = row.get("title"), row.get("kind")
    else:
        raise ValueError("invalid chat inventory")
    if not isinstance(title, str) or not clean_title(title) or kind not in {"direct", "group", "unknown"}:
        raise ValueError("invalid chat inventory")
    return clean_title(title), kind


def reconcile_inventory(workspace: Workspace, *, account: str, expected_profile: str,
                        observed_profile: str, lists: dict, date_order: str,
                        timezone_name: str) -> dict:
    if not expected_profile or observed_profile != expected_profile:
        raise ValueError("identity-unverified")
    _key(account)
    from zoneinfo import ZoneInfo
    ZoneInfo(timezone_name)
    if date_order not in {"mdy", "dmy"}:
        raise ValueError("explicit date convention required")
    if any(not isinstance(rows, list) for rows in lists.values()):
        raise ValueError("invalid chat inventory")
    complete = set(lists) == {"main", "archived"}
    observed_lists = {name: [chat_observation(row) for row in rows] for name, rows in lists.items()}
    counts = Counter(row for rows in observed_lists.values() for row in rows)
    title_counts = Counter(title for (title, kind), count in counts.items() for _ in range(count))
    def matches(record, title, kind):
        if clean_title(record["title"]) != title:
            return False
        prior_kind = record.get("kind", "unknown")
        return (prior_kind == kind or kind == "unknown" or
                (prior_kind == "unknown" and title_counts[title] == 1))
    now = _iso(datetime.now(timezone.utc))
    path = workspace.state_dir / "whatsapp-acquisition" / account / "inventory.json"
    with instagram_sync_lock(workspace) as acquired:
        if not acquired:
            raise ValueError("Localgraph writer is busy")
        workspace.ensure_workspace(force=False)
        config = _load(workspace.config_path)
        records = config.setdefault("imports", {}).setdefault("whatsapp", {}).setdefault("chats", {})
        previous = _load(path)
        seen_title_counts = Counter(previous.get("seenTitleCounts", {}))
        previous_counts = Counter()
        for entry in previous.get("entries", []):
            previous_counts[entry["title"]] += entry["occurrences"]
        seen_title_counts |= previous_counts | title_counts
        existing = [r for r in records.values() if r["accountKey"] == account]
        by_title = {}
        for r in existing:
            by_title.setdefault(clean_title(r["title"]), []).append(r)
        entries = []
        for (title, kind), occurrences in counts.items():
            bound = [r for r in by_title.get(title, []) if matches(r, title, kind)]
            unknown_collision = title_counts[title] > 1 and (title, "unknown") in counts
            unresolved_loss = seen_title_counts[title] > title_counts[title]
            state = "ambiguous" if occurrences > 1 or len(bound) > 1 or unknown_collision or unresolved_loss else "configured"
            record = bound[0] if len(bound) == 1 else None
            if record and not record["enabled"]:
                state = "excluded"
            if state == "configured" and record is None:
                key = "chat-" + hashlib.sha256((title + "\0" + kind).encode()).hexdigest()[:24]
                record = {"accountKey": account, "chatKey": key, "sourceKey": f"{account}:{key}",
                          "title": title, "viewLabel": title, "kind": kind, "enabled": True,
                          "dateOrder": date_order, "timezone": timezone_name, "acquisitionIntervalHours": 24}
                records[record["sourceKey"]] = record
            if record and record.get("kind") == "unknown" and kind != "unknown" and state == "configured":
                record["kind"] = kind
            entries.append({"title": title, "kind": kind, "occurrences": occurrences, "state": state,
                            "chatKey": record["chatKey"] if record else None,
                            "list": next(k for k, v in observed_lists.items() if (title, kind) in v)})
        missing = sum(r["enabled"] and not any(matches(r, title, kind) for title, kind in counts) for r in existing)
        # An unresolved row still belongs to the observed population even before it has a binding.
        missing = max(missing, sum((seen_title_counts - title_counts).values()))
        result = {"schemaVersion": 1, "accountKey": account, "observedAt": now,
                  "status": "inventoried" if complete else "inventory-incomplete",
                  "inventoryComplete": complete, "populationCovered": False, "historicalComplete": False,
                  "populationScope": "mac-main-and-archived-lists-not-primary-phone",
                  "discoveredChats": sum(counts.values()),
                  "configuredChats": sum(e["state"] == "configured" for e in entries),
                  "ambiguousChats": sum(e["occurrences"] for e in entries if e["state"] == "ambiguous"),
                  "excludedChats": sum(e["occurrences"] for e in entries if e["state"] == "excluded"),
                  "missingPreviouslySeenChats": missing, "seenTitleCounts": dict(seen_title_counts), "entries": entries}
        _write(workspace.config_path, config)
        _write(path, result)
    return result


def download_snapshot(folder: Path) -> dict:
    return {p.name: (p.stat().st_ino, p.stat().st_size, p.stat().st_mtime_ns)
            for p in folder.iterdir() if p.is_file() and not p.is_symlink()}


def new_download(folder: Path, before: dict) -> Path:
    after = download_snapshot(folder)
    # Never take an overwritten prior file or infer provenance from its filename.
    candidates = [folder / name for name in after if name not in before and name.endswith(".zip")]
    if len(candidates) > 1:
        raise ValueError("ambiguous-download")
    if not candidates:
        raise ValueError("export-not-delivered")
    return candidates[0]


def parse_driver_result(raw: str, operation: str) -> dict:
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("operation") != operation or value.get("status") != "ok":
            raise ValueError
        if operation in {"inventory", "resolve"} and "pages" in value:
            if not isinstance(value["pages"], dict):
                raise ValueError
            value["lists"] = {key: merge_pages(scan) for key, scan in value.pop("pages").items()}
        if operation in {"inventory", "resolve"} and (not isinstance(value.get("lists"), dict)
                                         or not isinstance(value.get("profile"), str)):
            raise ValueError
        if operation == "export" and (not isinstance(value.get("title"), str)
                                      or not isinstance(value.get("mediaRequested"), bool)
                                      or value.get("downloadObserved") is not True):
            raise ValueError
        return value
    except (ValueError, TypeError):
        raise ValueError("invalid-native-driver-result") from None


def merge_pages(scan: dict) -> list:
    if not isinstance(scan, dict) or scan.get("topReached") is not True or scan.get("bottomReached") is not True or not scan.get("pages"):
        raise ValueError("inventory-incomplete")
    pages = scan["pages"]
    if not isinstance(pages, list):
        raise ValueError("inventory-incomplete")
    if any(not isinstance(page, list) for page in pages):
        raise ValueError("inventory-incomplete")
    for page in pages:
        for row in page:
            chat_observation(row)
    merged = list(pages[0])
    for page in pages[1:]:
        if not page:
            raise ValueError("inventory-incomplete")
        overlaps = [n for n in range(1, min(len(merged), len(page)) + 1)
                    if [chat_observation(r)[0] for r in merged[-n:]] == [chat_observation(r)[0] for r in page[:n]]]
        if len(overlaps) != 1:
            raise ValueError("inventory-incomplete")
        overlap = overlaps[0]
        for offset in range(overlap):
            prior, incoming = merged[len(merged) - overlap + offset], page[offset]
            _, old_kind = chat_observation(prior)
            _, new_kind = chat_observation(incoming)
            if old_kind != new_kind and "unknown" not in {old_kind, new_kind}:
                raise ValueError("inventory-changed-during-scan")
            if old_kind == "unknown" and new_kind != "unknown":
                merged[len(merged) - overlap + offset] = incoming
        merged.extend(page[overlap:])
    return merged


def candidate_hash() -> str:
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.iterdir()):
        if path.suffix in {".py", ".applescript"}:
            digest.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def policy_hash(policy: dict) -> str:
    fields = ("accountKey", "expectedProfile", "dateOrder", "timezone", "scope", "driver")
    return hashlib.sha256(json.dumps({k: policy.get(k) for k in fields}, sort_keys=True).encode()).hexdigest()


def binding_hash(record: dict) -> str:
    return hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()


def assert_authority(workspace: Workspace, record: dict, expected_policy: str) -> None:
    current_policy = _load(workspace.config_path).get("imports", {}).get("whatsapp", {}).get("acquisition", {})
    current = next((r for r in chats(workspace) if r["sourceKey"] == record["sourceKey"]), {})
    if policy_hash(current_policy) != expected_policy or binding_hash(current) != binding_hash(record) or not current.get("enabled"):
        raise ValueError("identity-unverified")


def resolve_binding(driver, profile: str, record: dict) -> dict:
    observed = parse_driver_result(json.dumps(driver("resolve", profile, record["title"])), "resolve")
    if observed["profile"] != profile:
        raise ValueError("identity-unverified")
    lists = observed["lists"]
    if set(lists) != {"main", "archived"} or any(not isinstance(rows, list) for rows in lists.values()):
        raise ValueError("inventory-incomplete")
    matches = []
    unknown = False
    for list_name, rows in lists.items():
        for row in rows:
            title, kind = chat_observation(row)
            if title != clean_title(record["title"]):
                continue
            unknown |= kind == "unknown"
            if record.get("kind", "unknown") in {kind, "unknown"}:
                matches.append({"list": list_name, "title": title, "kind": kind, "chatKey": record["chatKey"]})
    if unknown or len(matches) > 1:
        raise ValueError("chat-identity-ambiguous")
    if not matches:
        raise ValueError("chat-not-found")
    return matches[0]


def recover_delivery(workspace: Workspace, record: dict, job: dict) -> bool:
    root = workspace.sources_dir / "whatsapp" / record["accountKey"] / record["chatKey"]
    digest = job.get("sha256", "")
    if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
        return False
    if not any(r.get("sha256") == digest and r.get("recordedAt") == job.get("receiptRecordedAt")
               and r.get("sourceKey") == record["sourceKey"] and r.get("origin") == "mac-native"
               for r in (_load(p) for p in (root / "receipts").glob("*.json"))):
        return False
    archive = root / "archives" / f"{digest}.zip"
    if not archive.is_file() or hashlib.sha256(archive.read_bytes()).hexdigest() != digest:
        return False
    synced = run_whatsapp_sync(workspace)
    return any(r.get("chatKey") == record["chatKey"] and r.get("accountKey") == record["accountKey"]
               and r["status"] == "local-current" for r in synced["chats"])


def desktop_available(*, snapshot: dict | None = None, uid: int | None = None) -> bool:
    if snapshot is None:
        try:
            result = subprocess.run(["/usr/sbin/ioreg", "-n", "Root", "-d", "1", "-a"],
                                    capture_output=True, timeout=10, check=True)
            snapshot = plistlib.loads(result.stdout)
        except (OSError, ValueError, subprocess.SubprocessError, plistlib.InvalidFileException):
            return False
    if (not isinstance(snapshot, dict) or snapshot.get("IOConsoleLocked") is not False or
            not isinstance(snapshot.get("IOConsoleUsers"), list)):
        return False
    return any(isinstance(row, dict) and row.get("kCGSSessionOnConsoleKey") is True and
               row.get("kCGSSessionUserIDKey") == (os.getuid() if uid is None else uid) and
               row.get("CGSSessionScreenIsLocked") is not True for row in snapshot.get("IOConsoleUsers", []))


def configure_acquisition(workspace: Workspace, *, account: str, expected_profile: str,
                          date_order: str, timezone_name: str) -> dict:
    _key(account)
    from zoneinfo import ZoneInfo
    ZoneInfo(timezone_name)
    if not re.fullmatch(r"[A-Za-z0-9._-]+", expected_profile) or date_order not in {"mdy", "dmy"}:
        raise ValueError("invalid native acquisition policy")
    with instagram_sync_lock(workspace) as acquired:
        if not acquired:
            raise ValueError("Localgraph writer is busy")
        workspace.ensure_workspace(force=False)
        config = _load(workspace.config_path)
        policy = {"accountKey": account, "expectedProfile": expected_profile,
                  "dateOrder": date_order, "timezone": timezone_name, "scope": "all-mac-chats",
                  "driver": "applescript", "configuredAt": _iso(datetime.now(timezone.utc))}
        config.setdefault("imports", {}).setdefault("whatsapp", {})["acquisition"] = policy
        _write(workspace.config_path, config)
    return {k: v for k, v in policy.items() if k != "expectedProfile"}


def native_driver(operation: str, profile: str, *args: str) -> dict:
    if not desktop_available():
        raise ValueError("session-unavailable")
    try:
        process = subprocess.Popen(["/usr/bin/osascript", str(Path(__file__).with_name("whatsapp_native.applescript")),
                                   operation, profile, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except OSError:
        raise ValueError("session-unavailable") from None
    deadline = time.monotonic() + 600
    try:
        while True:
            try:
                stdout, stderr = process.communicate(timeout=2)
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() >= deadline or not desktop_available():
                    raise ValueError("session-unavailable") from None
    except BaseException:
        process.kill()
        process.communicate()
        raise
    if not desktop_available():
        raise ValueError("session-unavailable")
    if process.returncode:
        for code in sorted(SAFE_FAILURES):
            if code in stderr:
                raise ValueError(code)
        raise ValueError("export-control-changed")
    return parse_driver_result(stdout, operation)


@contextlib.contextmanager
def acquisition_lock(workspace: Workspace):
    workspace.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (workspace.state_dir / "whatsapp-acquisition.lock").open("a+") as handle:
        os.chmod(handle.name, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def population_status(workspace: Workspace, account: str, now: datetime | None = None, current_keys: set | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    if current_keys is None:
        # Reuse the canonical-count, custody, freshness and render checks, not only cached state.
        from .whatsapp import source_status
        report = source_status(workspace, {}, now)
        for account_report in report["accounts"]:
            if account_report["accountKey"] == account:
                return account_report["population"]
        current_keys = set()
    inventory = _load(workspace.state_dir / "whatsapp-acquisition" / account / "inventory.json")
    current = 0
    for entry in inventory.get("entries", []):
        if entry["state"] != "configured":
            continue
        current += entry["chatKey"] in current_keys
    result = {k: v for k, v in inventory.items() if k not in {"entries", "seenTitleCounts"}}
    fresh = bool(inventory.get("observedAt") and 0 <= (now - _time(inventory["observedAt"])).total_seconds() <= 48 * 3600)
    last_run = _load(workspace.state_dir / "whatsapp-acquisition" / account / "acquisition.json")
    discovery = _load(workspace.state_dir / "whatsapp-acquisition" / account / "discovery.json")
    inventory_failed = bool(last_run.get("error"))
    discovery_failed = bool(discovery.get("error"))
    result.update({"currentChats": current, "inventoryFresh": fresh,
                   "lastRunStatus": last_run.get("status"),
                   "refreshStatus": last_run.get("refreshStatus"),
                   "discoveryStatus": discovery.get("status"),
                   "discoveryError": safe_failure(ValueError(discovery["error"]), "discovery-failed") if discovery_failed else None,
                   "lastError": safe_failure(ValueError(last_run["error"]), "acquisition-failed") if inventory_failed else None,
                   "populationCovered": bool(not inventory_failed and not discovery_failed and fresh and inventory.get("inventoryComplete") and
                       current > 0 and current == inventory.get("discoveredChats") and
                       not inventory.get("missingPreviouslySeenChats")), "historicalComplete": False})
    return result


def run_acquisition(workspace: Workspace, *, downloads: Path | None = None, driver=None,
                    poll_seconds: float = 0.5, download_timeout: float = 120,
                    inventory_only: bool = False, chat_keys: list[str] | None = None,
                    refresh_only: bool = False, resume: bool = False, max_chats: int | None = None,
                    discovery_if_due: bool = False) -> dict:
    if inventory_only and refresh_only or max_chats is not None and (isinstance(max_chats, bool) or max_chats < 1):
        raise ValueError("invalid acquisition mode or batch limit")
    driver = driver or native_driver
    downloads = downloads or Path.home() / "Downloads"
    policy = _load(workspace.config_path).get("imports", {}).get("whatsapp", {}).get("acquisition", {})
    if policy.get("scope") != "all-mac-chats":
        raise ValueError("explicit all-chat acquisition policy required")
    account, profile = policy["accountKey"], policy["expectedProfile"]
    run_path = workspace.state_dir / "whatsapp-acquisition" / account / "acquisition.json"
    with acquisition_lock(workspace) as acquired:
        if not acquired:
            return {"status": "skipped-concurrent"}
        started = _iso(datetime.now(timezone.utc))
        execution_candidate = candidate_hash()
        execution_policy = policy_hash(policy)
        if inventory_only and discovery_if_due:
            previous_discovery = _load(run_path.parent / "discovery.json")
            try:
                age = (datetime.now(timezone.utc) - _time(previous_discovery["checkedAt"])).total_seconds()
                if (previous_discovery.get("status") == "inventoried" and not previous_discovery.get("error")
                        and previous_discovery.get("candidateSha256") == execution_candidate
                        and previous_discovery.get("policySha256") == execution_policy and 0 <= age < 24 * 3600):
                    return {"status": "skipped-current-discovery", "population": population_status(workspace, account)}
            except (ValueError, KeyError, TypeError):
                pass
        results = []
        accepted_keys = []
        skipped_keys = []
        recovered_keys = []
        stopped = None
        if not refresh_only:
            try:
                observed = parse_driver_result(json.dumps(driver("inventory", profile)), "inventory")
                inventory = reconcile_inventory(workspace, account=account, expected_profile=profile,
                    observed_profile=observed["profile"], lists=observed["lists"], date_order=policy["dateOrder"],
                    timezone_name=policy["timezone"])
                _write(run_path.parent / "discovery.json", {"status": inventory["status"], "checkedAt": started,
                    "candidateSha256": execution_candidate, "policySha256": execution_policy})
            except (ValueError, OSError) as exc:
                result = {"status": "degraded", "startedAt": started, "finishedAt": _iso(datetime.now(timezone.utc)),
                          "error": safe_failure(exc, "inventory-or-identity-unverified"), "driver": "applescript"}
                _write(run_path.parent / "discovery.json", result)
                if not inventory_only:
                    _write(run_path, result)
                return result
        else:
            inventory = _load(run_path.parent / "inventory.json")
        if not refresh_only and (not inventory["inventoryComplete"] or inventory_only):
            result = {"status": "inventoried" if inventory["inventoryComplete"] else "degraded",
                      "population": population_status(workspace, account), "driver": "applescript"}
            if not inventory_only:
                _write(run_path, result)
            return result
        _write(run_path, {"status": "acquiring", "startedAt": started, "driver": "applescript"})
        bindings = {r["chatKey"]: r for r in chats(workspace) if r["accountKey"] == account}
        queue_path = run_path.parent / "refresh-queue.json"
        queue = _load(queue_path) if refresh_only else {}
        jobs = queue.setdefault("jobs", {})
        entries = ([{"chatKey": key, "state": "configured"} for key in bindings] if refresh_only else inventory["entries"])
        if refresh_only:
            entries.sort(key=lambda e: (jobs.get(e["chatKey"], {}).get("attemptedAt", ""), e["chatKey"]))
        from .whatsapp import source_status
        health = source_status(workspace, {}, datetime.now(timezone.utc)) if refresh_only else {"accounts": []}
        current_keys = {r["chatKey"] for a in health["accounts"] if a["accountKey"] == account
                        for r in a["chats"] if r["lifecycle"]["current"]}
        for entry in entries:
            if entry["state"] != "configured" or (chat_keys and entry["chatKey"] not in chat_keys):
                continue
            key = entry["chatKey"]
            if not bindings[key]["enabled"]:
                continue
            original_binding = binding_hash(bindings[key])
            job = jobs.get(key, {})
            same_job = (job.get("bindingSha256") == original_binding and job.get("policySha256") == execution_policy
                        and job.get("candidateSha256") == execution_candidate)
            if refresh_only and resume and same_job and job.get("status") == "current" and key in current_keys:
                try:
                    age = (datetime.now(timezone.utc) - _time(job["finishedAt"])).total_seconds()
                    if 0 <= age < bindings[key]["acquisitionIntervalHours"] * 3600:
                        skipped_keys.append(key)
                        continue
                except (ValueError, KeyError, TypeError):
                    pass
            if max_chats is not None and len(results) >= max_chats:
                continue
            if refresh_only and resume and same_job and job.get("status") == "delivered":
                try:
                    recovered = recover_delivery(workspace, bindings[key], job)
                except (OSError, ValueError, TypeError):
                    recovered = False
                if recovered:
                    job.update({"status": "current", "finishedAt": job["exportedAt"]})
                    recovered_keys.append(key)
                    results.append({"chatKey": key, "status": "resumed-delivery"})
                else:
                    job.update({"error": "delivery-recovery-failed", "attemptedAt": _iso(datetime.now(timezone.utc))})
                    results.append({"chatKey": key, "status": "failed", "error": "delivery-recovery-failed"})
                _write(queue_path, queue)
                continue
            try:
                assert_authority(workspace, bindings[key], execution_policy)
                # Leave room for download, immutable custody, media staging and a reserve.
                if any(shutil.disk_usage(p).free < 10 * 1024**3 + 3 * MAX_ARCHIVE_BYTES
                       for p in (downloads, workspace.root)):
                    raise ValueError("insufficient-disk-space")
                if refresh_only:
                    jobs[key] = {"status": "acquiring", "attemptedAt": _iso(datetime.now(timezone.utc)),
                                 "bindingSha256": original_binding, "policySha256": execution_policy,
                                 "candidateSha256": execution_candidate}
                    _write(queue_path, queue)
                    entry = resolve_binding(driver, profile, bindings[key])
                assert_authority(workspace, bindings[key], execution_policy)
                before = download_snapshot(downloads)
                selection = [entry["list"], entry["title"]]
                if entry.get("kind", "unknown") != "unknown":
                    selection.append(entry["kind"])
                response = driver("export", profile, *selection)
                parse_driver_result(json.dumps(response), "export")
                if clean_title(response["title"]) != entry["title"]:
                    raise ValueError("identity-unverified")
                if entry.get("kind", "unknown") != "unknown" and response.get("kind") != entry["kind"]:
                    raise ValueError("identity-unverified")
                assert_authority(workspace, bindings[key], execution_policy)
                deadline, previous, stable = time.monotonic() + download_timeout, None, 0
                while True:
                    try:
                        archive = new_download(downloads, before)
                        signature = (archive.stat().st_size, archive.stat().st_mtime_ns)
                        stable = stable + 1 if signature == previous else 0
                        previous = signature
                        if stable >= 2:
                            break
                    except ValueError as exc:
                        if str(exc) != "export-not-delivered":
                            raise
                    if time.monotonic() >= deadline:
                        raise ValueError("export-not-delivered")
                    time.sleep(poll_seconds)
                filename = clean_title(archive.name)
                expected = "WhatsApp Chat - " + entry["title"]
                if not re.fullmatch(re.escape(expected) + r"(?: \(\d+\))?\.zip", filename):
                    raise ValueError("identity-unverified")
                receipt = record_export(workspace, account_key=account, chat_key=key, archive=archive,
                    observed_title=bindings[key]["title"],
                    exported_at=_iso(datetime.now(timezone.utc)), media_requested=response["mediaRequested"],
                    expected_binding=bindings[key], expected_policy=policy)
                results.append({"chatKey": key, "status": "delivered", "sha256": receipt["sha256"]})
                # Commit each successful chat before moving to another native operation.
                if refresh_only:
                    jobs[key].update({"status": "delivered", "sha256": receipt["sha256"],
                                      "receiptRecordedAt": receipt["recordedAt"], "exportedAt": receipt["exportedAt"]})
                    _write(queue_path, queue)
                    synced = run_whatsapp_sync(workspace)
                    if any(r.get("chatKey") == key and r["status"] == "local-current" for r in synced["chats"]):
                        accepted_keys.append(key)
                        jobs[key].update({"status": "current", "finishedAt": _iso(datetime.now(timezone.utc))})
                    _write(queue_path, queue)
            except (ValueError, OSError) as exc:
                reason = safe_failure(exc)
                try:
                    record_acquisition_failure(workspace, account_key=account, chat_key=key,
                        reason=reason if reason in {"session-unavailable", "identity-unverified", "app-disconnected", "export-control-changed"} else "export-failed")
                except ValueError:
                    # A concurrent disable must not be undone to record a failure.
                    pass
                results.append({"chatKey": key, "status": "failed", "error": reason})
                if refresh_only:
                    jobs.setdefault(key, {}).update({"status": "failed", "error": reason,
                                                    "attemptedAt": _iso(datetime.now(timezone.utc))})
                    _write(queue_path, queue)
                if reason in SESSION_FAILURES:
                    stopped = reason
                    break
        sync = run_whatsapp_sync(workspace)
        if refresh_only:
            final_health = source_status(workspace, {}, datetime.now(timezone.utc))
            current_keys = {r["chatKey"] for a in final_health["accounts"] if a["accountKey"] == account
                            for r in a["chats"] if r["lifecycle"]["current"]}
            accepted_keys = [key for key in accepted_keys if key in current_keys]
            skipped_keys = [key for key in skipped_keys if key in current_keys]
        if stopped:
            _write(run_path, {"status": "degraded", "error": stopped, "startedAt": started})
        population = population_status(workspace, account)
        result = {"status": "local-current" if population["populationCovered"] and sync["status"] == "local-current" else "degraded",
                  "driver": "applescript", "startedAt": started, "finishedAt": _iso(datetime.now(timezone.utc)),
                  "chats": results, "population": population,
                  "driverSha256": hashlib.sha256(Path(__file__).with_name("whatsapp_native.applescript").read_bytes()).hexdigest()}
        result["nativeDriver"] = driver is native_driver
        result["candidateSha256"] = execution_candidate
        result["policySha256"] = execution_policy
        if stopped:
            result["error"] = stopped
        result["acceptedChatKeys"] = accepted_keys if refresh_only else [r["chatKey"] for r in results if r["status"] == "delivered"
            and any(s.get("chatKey") == r["chatKey"] and s["status"] == "local-current" for s in sync["chats"])]
        if refresh_only:
            eligible = {r["chatKey"] for r in bindings.values() if r["enabled"] and (not chat_keys or r["chatKey"] in chat_keys)}
            result["skippedCurrentChatKeys"] = skipped_keys
            result["resumedDeliveredChatKeys"] = recovered_keys
            completed = set(accepted_keys + skipped_keys + recovered_keys) & current_keys
            result["refreshStatus"] = "current" if eligible and eligible <= completed else "pending"
            result["remainingChats"] = len(eligible - completed)
            if any(r["status"] == "failed" for r in results):
                result["refreshStatus"] = "degraded"
        if execution_candidate != candidate_hash():
            result.update({"status": "degraded", "error": "candidate-changed-during-run", "acceptedChatKeys": []})
        current_policy = _load(workspace.config_path).get("imports", {}).get("whatsapp", {}).get("acquisition", {})
        if execution_policy != policy_hash(current_policy):
            result.update({"status": "degraded", "error": "identity-unverified", "acceptedChatKeys": []})
        if result.get("error"):
            result["population"]["populationCovered"] = False
            if refresh_only:
                result["refreshStatus"] = "degraded"
        _write(run_path, result)
        _write(run_path.parent / "acquisition-runs" / f"{uuid.uuid4().hex}.json", result)
        return result


def install_acquisition(workspace: Workspace, *, home: Path | None = None, hour: int = 9, dry_run: bool = False) -> dict:
    if not 0 <= hour <= 23 or workspace.root.resolve().parts[1:2] == ("Volumes",):
        raise ValueError("native acquisition requires an internal workspace and hour 0..23")
    policy = _load(workspace.config_path).get("imports", {}).get("whatsapp", {}).get("acquisition", {})
    account = policy.get("accountKey", "unconfigured")
    candidate = candidate_hash()
    accepted = Counter()
    for path in (workspace.state_dir / "whatsapp-acquisition" / account / "acquisition-runs").glob("*.json"):
        receipt = _load(path)
        if (receipt.get("nativeDriver") is True and not receipt.get("error") and
                receipt.get("candidateSha256") == candidate and receipt.get("policySha256") == policy_hash(policy)):
            if 0 <= (datetime.now(timezone.utc) - _time(receipt["finishedAt"])).total_seconds() <= 48 * 3600:
                accepted.update(set(receipt.get("acceptedChatKeys", [])))
    if sum(n >= 2 for n in accepted.values()) < 2:
        raise ValueError("acceptance-required: two fresh exact-candidate native runs of at least two chats")
    support = (home or Path.home()) / "Library/Application Support/Localgraph"
    runtime, logs = support / "runtime", support / "logs"
    script = support / "bin/localgraph-whatsapp-acquire.sh"
    plist = (home or Path.home()) / "Library/LaunchAgents" / f"{LABEL}.plist"
    discovery_script = support / "bin/localgraph-whatsapp-discover.sh"
    discovery_plist = plist.with_name(f"{DISCOVERY_LABEL}.plist")
    def wrapper(arguments):
        command = " ".join(shlex.quote(p) for p in [sys.executable, "-m", "localgraph", "--root", str(workspace.root), *arguments])
        return f"#!/bin/zsh\nset -euo pipefail\nexport PYTHONPATH={shlex.quote(str(runtime))}\nexport PYTHONDONTWRITEBYTECODE=1\numask 077\n{command}\n"
    script_text = wrapper(["whatsapp-refresh", "--max-chats", "10"])
    payload = {"Label": LABEL, "ProgramArguments": ["/bin/zsh", str(script)], "RunAtLoad": True,
               "StartCalendarInterval": {"Hour": hour, "Minute": 0}, "StartInterval": 1800, "ThrottleInterval": 60,
               "WorkingDirectory": str(support), "ProcessType": "Interactive",
               "StandardOutPath": str(logs / "whatsapp-acquire.stdout.log"),
               "StandardErrorPath": str(logs / "whatsapp-acquire.stderr.log")}
    discovery_payload = {**payload, "Label": DISCOVERY_LABEL, "ProgramArguments": ["/bin/zsh", str(discovery_script)],
        "StartCalendarInterval": {"Hour": hour, "Minute": 15}, "StartInterval": 3600,
        "StandardOutPath": str(logs / "whatsapp-discover.stdout.log"),
        "StandardErrorPath": str(logs / "whatsapp-discover.stderr.log")}
    if not dry_run:
        with acquisition_lock(workspace) as acquired:
            if not acquired:
                raise ValueError("native acquisition busy")
            with instagram_sync_lock(workspace) as writer:
                if not writer:
                    raise ValueError("Localgraph writer is busy")
                for folder in (runtime, logs, script.parent, plist.parent):
                    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copytree(Path(__file__).parent, runtime / "localgraph", dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                script.write_text(script_text)
                script.chmod(0o700)
                plist.write_bytes(plistlib.dumps(payload))
                discovery_script.write_text(wrapper(["whatsapp-discover", "--if-due"]))
                discovery_script.chmod(0o700)
                discovery_plist.write_bytes(plistlib.dumps(discovery_payload))
    return {"label": LABEL, "script": str(script), "runtime": str(runtime), "plist": str(plist),
            "discoveryLabel": DISCOVERY_LABEL, "discoveryScript": str(discovery_script), "discoveryPlist": str(discovery_plist),
            "hour": hour, "dryRun": dry_run, "candidateSha256": candidate}
