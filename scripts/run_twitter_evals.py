from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timezone

from run_facebook_evals import REPO_ROOT, candidate_receipt, iter_tests


SPEC_PATH = REPO_ROOT / "evals" / "twitter-messages.json"


def main() -> int:
    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    discovered = unittest.defaultTestLoader.discover(str(REPO_ROOT / "tests"), pattern="test_*.py")
    indexed = {test.id(): test for test in iter_tests(discovered)}
    requested = [str(case["id"]) for case in spec["cases"]]
    missing = [case_id for case_id in requested if case_id not in indexed]
    if missing:
        print(json.dumps({"suite": spec["suite"], "missingCases": missing}, indent=2))
        return 2
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=2).run(
        unittest.TestSuite(indexed[case_id] for case_id in requested)
    )
    print(json.dumps({
        "schemaVersion": spec["schemaVersion"],
        "suite": spec["suite"],
        "evaluatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "candidate": candidate_receipt(),
        "cases": len(requested),
        "passed": result.testsRun - len(result.failures) - len(result.errors),
        "failures": len(result.failures),
        "errors": len(result.errors),
        "successful": result.wasSuccessful(),
    }, indent=2, sort_keys=True))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
