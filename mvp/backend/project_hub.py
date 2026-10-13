"""Development alias for ``plugins/session-hub/dashboard/project_hub.py`` (single source of truth)."""
from pathlib import Path as _Path

__file__ = str((_Path(__file__).resolve().parents[1] / "plugins" / "session-hub" / "dashboard" / "project_hub.py"))
exec(compile(_Path(__file__).read_text(encoding="utf-8"), __file__, "exec"), globals())
