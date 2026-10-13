from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import _hub_test_env  # noqa: F401  (fixture settings before any backend import)

    mvp_root = Path(__file__).resolve().parents[1]
    repo_root = mvp_root.parent
    os.chdir(repo_root)
    for path in (str(repo_root), str(mvp_root)):
        if path not in sys.path:
            sys.path.insert(0, path)
    suite = unittest.defaultTestLoader.discover(str(mvp_root / "tests"))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
