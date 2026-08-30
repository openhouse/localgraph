from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timezone

from run_facebook_evals import REPO_ROOT, candidate_receipt, iter_tests


def main() -> int:
    spec = json.loads((REPO_ROOT / "evals/whatsapp-messages.json").read_text())
    tests = unittest.defaultTestLoader.discover(str(REPO_ROOT / "tests"), pattern="test_whatsapp.py")
    indexed = {test.id(): test for test in iter_tests(tests)}
    missing = [name for name in spec["cases"] if name not in indexed]
    if missing:
        print(json.dumps({"missingCases": missing}))
        return 2
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=2).run(unittest.TestSuite(indexed[name] for name in spec["cases"]))
    print(json.dumps({"schemaVersion": 1, "suite": spec["suite"], "candidate": candidate_receipt(),
                      "evaluatedAt": datetime.now(timezone.utc).isoformat(), "cases": len(spec["cases"]),
                      "passed": result.testsRun - len(result.failures) - len(result.errors),
                      "failures": len(result.failures), "errors": len(result.errors),
                      "successful": result.wasSuccessful()}, indent=2, sort_keys=True))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
