"""Capture actual unittest output as reviewable evidence (no network required)."""
import io
import json
import platform
import unittest
from datetime import datetime, timezone
from src.cli import ROOT


def main():
    stream = io.StringIO()
    suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'))
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    summary = {'ran_at': datetime.now(timezone.utc).isoformat(), 'python': platform.python_version(),
               'command': 'python -m scripts.verify', 'tests_run': result.testsRun,
               'failures': len(result.failures), 'errors': len(result.errors),
               'skipped': len(result.skipped), 'successful': result.wasSuccessful(),
               'scope': 'Offline business invariants and HTTP adapter contract tests; not live model quality.'}
    (ROOT / 'examples/test-results.json').write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
    (ROOT / 'examples/test-results.txt').write_text(stream.getvalue(), encoding='utf-8')
    print(stream.getvalue())
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
