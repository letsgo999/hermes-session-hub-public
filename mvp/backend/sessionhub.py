"""Development alias for the plugin backend (single source of truth).

The installable plugin ships ``plugins/session-hub/dashboard/sessionhub_backend.py``. This module
executes that exact file so tests, previews and smoke tools never drift from what is installed.
"""
from pathlib import Path as _Path

__file__ = str((_Path(__file__).resolve().parents[1] / "plugins" / "session-hub" / "dashboard" / "sessionhub_backend.py"))
exec(compile(_Path(__file__).read_text(encoding="utf-8"), __file__, "exec"), globals())
