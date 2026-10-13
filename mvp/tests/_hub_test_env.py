"""Point Session Hub at the fixture settings before any backend module is imported."""
import os
from pathlib import Path

FIXTURE_CONFIG = Path(__file__).resolve().parent / "fixtures" / "session-hub.test-config.json"
os.environ["HERMES_SESSION_HUB_CONFIG"] = str(FIXTURE_CONFIG)
os.environ.pop("HERMES_SYNC_ROOT", None)
