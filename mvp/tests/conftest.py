import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _hub_test_env  # noqa: E402,F401  (sets HERMES_SESSION_HUB_CONFIG before test imports)

_MVP = Path(__file__).resolve().parents[1]
for _path in (str(_MVP.parent), str(_MVP)):
    if _path not in sys.path:
        sys.path.insert(0, _path)
