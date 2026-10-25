"""Container-only core regression entry; no production connectors exist here."""
import json
import sys
import unittest


def main():
    suite = unittest.TestSuite([unittest.defaultTestLoader.discover("tests", pattern=pattern)
                              for pattern in ["test_runtime.py", "test_offline_network.py", "test_adapters.py", "test_bundles.py", "test_codex.py", "test_resident.py"]])
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=1).run(suite)
    print(json.dumps({"tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
                      "skipped": len(result.skipped), "production_enabled": False, "model_calls": 0}))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
