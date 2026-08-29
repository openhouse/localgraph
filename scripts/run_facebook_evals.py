from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = REPO_ROOT / "evals" / "facebook-messages.json"


def iter_tests(suite: unittest.TestSuite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from iter_tests(item)
        else:
            yield item


def git_output(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def candidate_receipt() -> dict[str, object]:
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    ).stdout
    digest = hashlib.sha256()
    for raw_path in sorted(path for path in listed.split(b"\0") if path):
        path = REPO_ROOT / raw_path.decode("utf-8")
        digest.update(raw_path)
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    status = git_output("status", "--short")
    return {
        "head": git_output("rev-parse", "HEAD"),
        "dirty": bool(status),
        "treeSha256": digest.hexdigest(),
    }


def main() -> int:
    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    discovered = unittest.defaultTestLoader.discover(str(REPO_ROOT / "tests"), pattern="test_*.py")
    indexed = {test.id(): test for test in iter_tests(discovered)}
    requested = [str(case["id"]) for case in spec["cases"]]
    missing = [case_id for case_id in requested if case_id not in indexed]
    if missing:
        print(json.dumps({"suite": spec["suite"], "missingCases": missing}, indent=2))
        return 2

    suite = unittest.TestSuite(indexed[case_id] for case_id in requested)
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=2).run(suite)
    receipt = {
        "schemaVersion": spec["schemaVersion"],
        "suite": spec["suite"],
        "evaluatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "candidate": candidate_receipt(),
        "cases": len(requested),
        "passed": result.testsRun - len(result.failures) - len(result.errors),
        "failures": len(result.failures),
        "errors": len(result.errors),
        "successful": result.wasSuccessful(),
    }
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
