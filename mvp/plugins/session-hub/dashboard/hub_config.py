"""Session Hub settings: Hermes home, profile auto-detection and optional branch instances.

Nothing here is specific to one installation. Every value comes from (highest first):

1. environment variables (``HERMES_SESSION_HUB_CONFIG``, ``HERMES_HOME``, ...),
2. the JSON settings file ``<hermes home>/session-hub/session-hub.config.json``,
3. auto-detection / built-in defaults.

The settings file is optional. Without it the hub lists every local Hermes profile it finds
(``default`` plus each ``profiles/<name>/`` that has a ``state.db``) and shows no branches.

Example (see ``session-hub.config.example.json`` next to the installer)::

    {
      "contract": "session-hub-config-v1",
      "profiles": "auto",
      "profileLabels": {"default": "Lead"},
      "instances": {
        "enabled": true,
        "localLabel": "본사",
        "remoteGroupLabel": "지사",
        "syncRoot": "D:/Sync/hermes-hub",
        "remotes": {"A": {"label": "Branch A", "ttlMinutes": 30}}
      }
    }
"""
from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
from typing import Any, Mapping

CONFIG_CONTRACT = "session-hub-config-v1"
CONFIG_FILE_NAME = "session-hub.config.json"
PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,63}$")
INSTANCE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,16}$")
RESERVED_INSTANCE_IDS = frozenset({"HQ"})
MAX_PROFILES = 32
MAX_REMOTES = 16
DEFAULT_LOCAL_LABEL = "본사"
DEFAULT_REMOTE_GROUP_LABEL = "지사"
DEFAULT_REMOTE_TTL_SECONDS = 24 * 60 * 60
MAX_LABEL_CHARS = 40


def resolve_hermes_home(explicit: str | Path | None = None) -> Path:
    """Shared Hermes base folder, even when HERMES_HOME names a single profile folder."""
    value = explicit or os.environ.get("HERMES_HOME")
    if value:
        home = Path(value).expanduser()
        if is_profile_folder(home):
            return home.parent.parent
        return home
    local = os.environ.get("LOCALAPPDATA")
    return (Path(local) / "hermes") if local else Path.home() / ".hermes"


def is_profile_folder(path: Path) -> bool:
    """True for <hermes home>/profiles/<name> (the base must look like a Hermes home)."""
    if path.parent.name.casefold() != "profiles":
        return False
    base = path.parent.parent
    try:
        return (base / "config.yaml").is_file() or (base / "state.db").is_file()
    except OSError:
        return False


def config_path(hermes_home: Path | None = None) -> Path:
    override = os.environ.get("HERMES_SESSION_HUB_CONFIG")
    if override:
        return Path(override).expanduser()
    return (hermes_home or resolve_hermes_home()) / "session-hub" / CONFIG_FILE_NAME


def load_config_file(path: Path) -> dict[str, Any]:
    """Read the optional settings file. A missing or malformed file means "all defaults"."""
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def detect_local_profiles(hermes_home: Path) -> tuple[str, ...]:
    """``default`` first, then every ``profiles/<name>`` folder that holds a Hermes ``state.db``."""
    found: list[str] = ["default"]
    profiles_root = hermes_home / "profiles"
    try:
        children = sorted(profiles_root.iterdir(), key=lambda p: p.name.casefold()) if profiles_root.is_dir() else []
    except OSError:
        children = []
    for child in children:
        name = child.name
        if name == "default" or not PROFILE_NAME_RE.fullmatch(name):
            continue
        try:
            if (child / "state.db").is_file():
                found.append(name)
        except OSError:
            continue
        if len(found) >= MAX_PROFILES:
            break
    return tuple(found)


def _clean_label(value: Any, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    text = " ".join(value.split())[:MAX_LABEL_CHARS]
    return text or fallback


def _explicit_profiles(value: Any) -> tuple[str, ...] | None:
    if not isinstance(value, list):
        return None
    names: list[str] = []
    for item in value:
        if isinstance(item, str) and PROFILE_NAME_RE.fullmatch(item) and item not in names:
            names.append(item)
    if not names:
        return None
    if "default" in names:
        names.remove("default")
    return tuple(["default", *names][:MAX_PROFILES])


def _remotes(value: Any) -> dict[str, dict[str, Any]]:
    remotes: dict[str, dict[str, Any]] = {}
    if not isinstance(value, Mapping):
        return remotes
    for instance, meta in value.items():
        if not isinstance(instance, str) or not INSTANCE_ID_RE.fullmatch(instance) or instance in RESERVED_INSTANCE_IDS:
            continue
        meta = meta if isinstance(meta, Mapping) else {}
        remotes[instance] = {"label": _clean_label(meta.get("label"), instance), "ttlSeconds": _ttl_seconds(meta)}
        if len(remotes) >= MAX_REMOTES:
            break
    return remotes


MAX_TTL_SECONDS = 365 * 24 * 60 * 60


def _ttl_seconds(meta: Mapping[str, Any]) -> int:
    for key, factor in (("ttlSeconds", 1), ("ttlMinutes", 60)):
        value = meta.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if isinstance(value, float) and not math.isfinite(value):
            continue
        return int(min(max(value * factor, 60), MAX_TTL_SECONDS))
    return DEFAULT_REMOTE_TTL_SECONDS


def _optional_path(value: Any) -> Path | None:
    if isinstance(value, str) and value.strip():
        return Path(os.path.expandvars(value.strip())).expanduser()
    return None


class HubSettings:
    """Resolved, validated settings. Build with :func:`load_settings`."""

    def __init__(self, data: Mapping[str, Any] | None = None, *, hermes_home: Path | None = None, source: Path | None = None) -> None:
        data = dict(data or {})
        self.source = source
        self.hermes_home = resolve_hermes_home(data.get("hermesHome") if isinstance(data.get("hermesHome"), str) else None) if hermes_home is None else Path(hermes_home)
        explicit = _explicit_profiles(data.get("profiles"))
        self.profiles_mode = "explicit" if explicit else "auto"
        self.local_profiles: tuple[str, ...] = explicit or detect_local_profiles(self.hermes_home)
        labels = data.get("profileLabels") if isinstance(data.get("profileLabels"), Mapping) else {}
        self.profile_labels: dict[str, str] = {
            key: _clean_label(value, key) for key, value in labels.items()
            if isinstance(key, str) and len(key) <= 80 and isinstance(value, str) and value.strip()
        }
        instances = data.get("instances") if isinstance(data.get("instances"), Mapping) else {}
        enabled = instances.get("enabled", True)
        self.instances_enabled = bool(enabled) if isinstance(enabled, bool) else True
        self.local_label = _clean_label(instances.get("localLabel"), DEFAULT_LOCAL_LABEL)
        self.remote_group_label = _clean_label(instances.get("remoteGroupLabel"), DEFAULT_REMOTE_GROUP_LABEL)
        env_sync = os.environ.get("HERMES_SYNC_ROOT")
        self.sync_root = _optional_path(env_sync) if env_sync else _optional_path(instances.get("syncRoot"))
        self.remote_instances: dict[str, dict[str, Any]] = _remotes(instances.get("remotes")) if self.instances_enabled else {}
        for instance, meta in self.remote_instances.items():
            self.profile_labels.setdefault(f"{instance}/default", f"{meta['label']} ({instance})")
        env_skills = os.environ.get("HERMES_SESSION_HUB_SKILLS_ROOT")
        self.skills_root = _optional_path(env_skills) if env_skills else _optional_path(data.get("skillsRoot"))
        env_jobs = os.environ.get("HERMES_EXTERNAL_AI_JOBS")
        self.external_ai_jobs = (
            _optional_path(env_jobs) if env_jobs else _optional_path(data.get("externalAiJobsDir"))
        ) or self.hermes_home / "session-hub" / "external-ai" / "jobs"
        projects = data.get("projects")
        self.projects: list[dict[str, Any]] | None = [item for item in projects if isinstance(item, Mapping)] if isinstance(projects, list) else None
        board = data.get("projectBoard")
        self.project_board = board if isinstance(board, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", board) else None

    @property
    def remote_profiles(self) -> tuple[str, ...]:
        return tuple(f"{instance}/default" for instance in self.remote_instances)

    def public_payload(self) -> dict[str, Any]:
        """What the desktop UI needs to draw the profile checkboxes. Contains no paths."""
        return {
            "contract": CONFIG_CONTRACT,
            "profilesMode": self.profiles_mode,
            "localProfiles": list(self.local_profiles),
            "remoteProfiles": list(self.remote_profiles),
            "profileLabels": dict(self.profile_labels),
            "instancesEnabled": self.instances_enabled,
            "localLabel": self.local_label,
            "remoteGroupLabel": self.remote_group_label,
            "remoteInstances": {key: {"label": meta["label"]} for key, meta in self.remote_instances.items()},
        }


_CACHED: HubSettings | None = None


def load_settings(path: Path | None = None) -> HubSettings:
    home = resolve_hermes_home()
    source = path or config_path(home)
    data = load_config_file(source)
    explicit_home = data.get("hermesHome") if isinstance(data.get("hermesHome"), str) and data.get("hermesHome").strip() else None
    resolved_home = resolve_hermes_home(explicit_home) if explicit_home and not os.environ.get("HERMES_HOME") else home
    try:
        return HubSettings(data, hermes_home=resolved_home, source=source)
    except Exception:  # noqa: BLE001 - a malformed settings file must never stop the plugin from loading
        return HubSettings({}, hermes_home=home, source=source)


def current_settings(*, reload: bool = False) -> HubSettings:
    global _CACHED
    if _CACHED is None or reload:
        _CACHED = load_settings()
    return _CACHED


def set_settings_for_tests(settings: HubSettings | None) -> None:
    global _CACHED
    _CACHED = settings
