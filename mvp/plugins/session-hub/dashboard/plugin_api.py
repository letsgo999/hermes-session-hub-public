from __future__ import annotations

import importlib.util
import ipaddress
import os
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse


def _load_backend_module():
    path = Path(__file__).with_name("sessionhub_backend.py").resolve()
    module_name = "hermes_session_hub_readonly_backend"
    existing = sys.modules.get(module_name)
    if existing is not None and Path(getattr(existing, "__file__", "")).resolve() == path:
        return existing
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Session Hub backend module is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


_backend_module = _load_backend_module()
SessionHubBackend = _backend_module.SessionHubBackend
KanbanHubBackend = _backend_module.KanbanHubBackend
SessionHubError = _backend_module.SessionHubError
SnapshotCache = _backend_module.SnapshotCache
DEFAULT_PAGE_SIZE = _backend_module.DEFAULT_PAGE_SIZE
hub_config = _backend_module.hub_config
# Branch snapshots are written by tools/session_snapshot.py into <syncRoot>/snapshots/<ID>/.
# syncRoot comes from session-hub.config.json ("instances.syncRoot") or HERMES_SYNC_ROOT;
# HERMES_SESSION_HUB_NO_REMOTE=1 disables branch reads. No syncRoot means no branch reads.


def _settings() -> Any:
    return hub_config.current_settings()


def reload_settings() -> Any:
    """Re-read session-hub.config.json and rebind the backend profile allowlists."""
    global _SKILLS_INDEX_CACHE
    settings = hub_config.current_settings(reload=True)
    _backend_module.apply_hub_settings(settings)
    _project_module.set_owner_profiles(_backend_module.LOCAL_PROFILE_NAMES)
    _SKILLS_INDEX_CACHE = None
    return settings


def __getattr__(name: str) -> Any:
    # Profile allowlists are resolved at load time from the settings file; expose the live values.
    if name in {"PROFILE_NAMES", "ALL_PROFILE_NAMES", "REMOTE_INSTANCES", "LOCAL_PROFILE_NAMES"}:
        return getattr(_backend_module, name)
    raise AttributeError(name)


def _load_project_module():
    path = Path(__file__).with_name("project_hub.py").resolve()
    module_name = "hermes_session_project_hub_backend"
    existing = sys.modules.get(module_name)
    if existing is not None and Path(getattr(existing, "__file__", "")).resolve() == path:
        return existing
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Project Hub backend module is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


_project_module = _load_project_module()
ProjectHub = _project_module.ProjectHub
ProjectHubError = _project_module.ProjectHubError
_project_module.set_owner_profiles(_backend_module.LOCAL_PROFILE_NAMES)


def _load_external_ai_module():
    path = Path(__file__).with_name("external_ai_status.py").resolve()
    module_name = "hermes_session_external_ai_status"
    existing = sys.modules.get(module_name)
    if existing is not None and Path(getattr(existing, "__file__", "")).resolve() == path:
        return existing
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("External AI status module is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


_external_ai_module = _load_external_ai_module()
ExternalAIStatusReader = _external_ai_module.ExternalAIStatusReader
ExternalAIStatusError = _external_ai_module.ExternalAIStatusError


def _load_ai_sessions_module():
    path = Path(__file__).with_name("ai_sessions.py").resolve()
    module_name = "hermes_session_hub_ai_sessions"
    existing = sys.modules.get(module_name)
    if existing is not None and Path(getattr(existing, "__file__", "")).resolve() == path:
        return existing
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("AI sessions module is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


_ai_sessions_module = _load_ai_sessions_module()
AISessionsReader = _ai_sessions_module.AISessionsReader
AISessionsError = _ai_sessions_module.AISessionsError
_TEST_AI_SESSIONS: tuple[Path, dict[str, Path]] | None = None


def configure_ai_sessions_for_tests(cache_path: str | Path | None, sources: Mapping[str, str | Path] | None = None) -> None:
    """Point the Codex/Claude tab at fixture data in isolated tests; never touches real app data."""
    global _TEST_AI_SESSIONS
    _TEST_AI_SESSIONS = None if cache_path is None else (Path(cache_path), {k: Path(v) for k, v in (sources or {}).items()})


def _ai_sessions_paths() -> tuple[Path, dict[str, Path] | None]:
    if _TEST_AI_SESSIONS is not None:
        return _TEST_AI_SESSIONS
    return _ai_sessions_module.default_cache_path(), None


def _load_skills_module():
    path = Path(__file__).with_name("skills_index.py").resolve()
    module_name = "hermes_session_hub_skills_index"
    existing = sys.modules.get(module_name)
    if existing is not None and Path(getattr(existing, "__file__", "")).resolve() == path:
        return existing
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Session Hub skills index module is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


_skills_module = _load_skills_module()
SkillsIndex = _skills_module.SkillsIndex

_TEST_DB_PATHS: dict[str, Path] | None = None
_TEST_KANBAN_PATHS: dict[str, Path] | None = None
_TEST_PROJECT_PATHS: tuple[Path, Path] | None = None
_TEST_SNAPSHOTS: tuple[dict[str, Path], Path | None] | None = None
_TEST_SNAPSHOTS_DISABLED = False
_TEST_EXTERNAL_AI_ROOT: Path | None = None
_TEST_SKILLS_CONFIG: tuple[Path, Path, tuple[Path, ...]] | None = None
_SKILLS_INDEX_CACHE: Any | None = None
router = APIRouter()


def _default_profile_dbs() -> dict[str, Path]:
    root = _settings().hermes_home
    return {
        profile: (root / "state.db") if profile == "default" else (root / "profiles" / profile / "state.db")
        for profile in _backend_module.LOCAL_PROFILE_NAMES
    }


def configure_backend_for_tests(paths: Mapping[str, str | Path] | None) -> None:
    """Inject fixture paths in an isolated loader test; never writes config."""
    global _TEST_DB_PATHS
    _TEST_DB_PATHS = None if paths is None else {name: Path(path) for name, path in paths.items()}


def configure_kanban_for_tests(paths: Mapping[str, str | Path] | None) -> None:
    """Inject read-only board fixtures in isolated tests; never writes config."""
    global _TEST_KANBAN_PATHS
    _TEST_KANBAN_PATHS = None if paths is None else {name: Path(path) for name, path in paths.items()}


def configure_project_hub_for_tests(registry_path: str | Path, kanban_path: str | Path) -> None:
    global _TEST_PROJECT_PATHS
    _TEST_PROJECT_PATHS = (Path(registry_path), Path(kanban_path))


def configure_external_ai_for_tests(jobs_root: str | Path | None) -> None:
    global _TEST_EXTERNAL_AI_ROOT
    _TEST_EXTERNAL_AI_ROOT = Path(jobs_root) if jobs_root is not None else None


def configure_skills_for_tests(
    hermes_home: str | Path, canonical_root: str | Path, archive_roots: tuple[str | Path, ...] = ()
) -> None:
    """Inject isolated SKILL.md roots and reset the memory cache."""
    global _TEST_SKILLS_CONFIG, _SKILLS_INDEX_CACHE
    _TEST_SKILLS_CONFIG = (
        Path(hermes_home),
        Path(canonical_root),
        tuple(Path(path) for path in archive_roots),
    )
    _SKILLS_INDEX_CACHE = None


def configure_snapshots_for_tests(
    snapshot_dirs: Mapping[str, str | Path] | None, cache_dir: str | Path | None = None
) -> None:
    """Inject branch snapshot folders in isolated tests. ``None`` restores discovery; ``{}`` disables remote reads."""
    global _TEST_SNAPSHOTS, _TEST_SNAPSHOTS_DISABLED
    if snapshot_dirs is None:
        _TEST_SNAPSHOTS, _TEST_SNAPSHOTS_DISABLED = None, False
        return
    _TEST_SNAPSHOTS_DISABLED = not snapshot_dirs
    _TEST_SNAPSHOTS = ({name: Path(path) for name, path in snapshot_dirs.items()}, Path(cache_dir) if cache_dir else None)


def _default_snapshot_dirs() -> tuple[dict[str, Path], Path | None]:
    settings = _settings()
    if os.environ.get("HERMES_SESSION_HUB_NO_REMOTE") or not settings.instances_enabled:
        return {}, None
    sync_root = settings.sync_root
    if sync_root is None:
        return {}, None
    # When the sync folder is not mounted (or a branch has never exported) that branch reports
    # "unknown"; it is never faked as an empty instance.
    dirs = {instance: sync_root / "snapshots" / instance for instance in _backend_module.REMOTE_INSTANCES}
    cache = settings.hermes_home / "session-hub" / "snapshot-cache"
    return dirs, cache


def _snapshots() -> Any:
    if _TEST_SNAPSHOTS_DISABLED:
        return None
    dirs, cache = _TEST_SNAPSHOTS or _default_snapshot_dirs()
    if not dirs:
        return None
    return SnapshotCache(dirs, cache)


def _backend() -> Any:
    return SessionHubBackend(_TEST_DB_PATHS or _default_profile_dbs(), _snapshots())


def _default_kanban_dbs() -> dict[str, Path]:
    root = _settings().hermes_home
    candidates: list[tuple[str, Path]] = []
    explicit = os.environ.get("HERMES_KANBAN_DB")
    if explicit:
        candidates.append((os.environ.get("HERMES_KANBAN_BOARD") or "current", Path(explicit)))
    candidates.append(("default", root / "kanban.db"))
    boards_root = root / "kanban" / "boards"
    if boards_root.is_dir():
        candidates.extend((folder.name, folder / "kanban.db") for folder in sorted(boards_root.iterdir()) if folder.is_dir())
    discovered: dict[str, Path] = {}
    seen: set[Path] = set()
    for board, path in candidates:
        if not path.is_file():
            continue
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        discovered[board] = resolved
    if not discovered:
        raise SessionHubError("kanban_db_unavailable")
    return discovered


def _kanban_backend() -> Any:
    return KanbanHubBackend(
        _TEST_KANBAN_PATHS or _default_kanban_dbs(),
        _TEST_DB_PATHS or _default_profile_dbs(),
        _snapshots(),
    )


def _history_date_from(payload: Mapping[str, Any]) -> int | None:
    history_scope = payload.get("historyScope", "30d")
    days = {"7d": 7, "14d": 14, "30d": 30}
    if history_scope not in {*days, "full", "custom"}:
        raise SessionHubError("bad_history_scope")
    date_from = payload.get("dateFrom")
    if history_scope == "full":
        return None
    if date_from is not None:
        try:
            return int(date_from)
        except (TypeError, ValueError):
            raise SessionHubError("bad_date_from") from None
    if history_scope == "custom":
        return None
    return int(time.time()) - (days[history_scope] * 24 * 60 * 60)


def _default_project_paths() -> tuple[Path, Path]:
    settings = _settings()
    root = settings.hermes_home
    registry = root / "session-hub" / "project-registry.sqlite3"
    explicit = os.environ.get("HERMES_KANBAN_DB")
    # Precedence: explicit env override, then the board named by "projectBoard" in
    # session-hub.config.json, then the "current" board, then the root Kanban DB.
    candidates = [
        Path(explicit) if explicit else None,
        (root / "kanban" / "boards" / settings.project_board / "kanban.db") if settings.project_board else None,
        root / "kanban" / "boards" / "current" / "kanban.db",
        root / "kanban.db",
    ]
    kanban = next((path for path in candidates if path is not None and path.is_file()), None)
    if kanban is None:
        raise ProjectHubError("kanban_db_unavailable")
    return registry, kanban


def _project_hub() -> Any:
    registry, kanban = _TEST_PROJECT_PATHS or _default_project_paths()
    return ProjectHub(registry, kanban, projects=_settings().projects)


def _external_ai_reader() -> Any:
    root = _TEST_EXTERNAL_AI_ROOT or _settings().external_ai_jobs
    return ExternalAIStatusReader(root)


def _skills_index() -> Any:
    global _SKILLS_INDEX_CACHE
    if _SKILLS_INDEX_CACHE is None:
        if _TEST_SKILLS_CONFIG is None:
            settings = _settings()
            _SKILLS_INDEX_CACHE = SkillsIndex(
                hermes_home=settings.hermes_home,
                canonical_root=settings.skills_root,
                # Auto mode scans skills in every profile folder, including profiles that have no
                # state.db yet; an explicit profiles list in the settings file still wins.
                profiles=_backend_module.LOCAL_PROFILE_NAMES if settings.profiles_mode == "explicit" else None,
            )
        else:
            home, canonical, archives = _TEST_SKILLS_CONFIG
            _SKILLS_INDEX_CACHE = SkillsIndex(
                hermes_home=home,
                canonical_root=canonical,
                archive_roots=archives,
            )
    return _SKILLS_INDEX_CACHE


def _is_loopback_host(value: Any) -> bool:
    host = str(value or "").strip().strip("[]").casefold()
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def _native_owner_request(request: Request) -> bool:
    """Hermes 0.22+: Desktop calls the gateway-hosted API with a one-request owner ticket.

    Hermes redeems that ticket itself (``x-hermes-gateway-ticket``, minted on the owner-only
    control socket) and stamps ``request.state.native_http_principal`` only for a loopback socket
    peer with no Origin header. The listener may still report ``auth_required`` True when
    ``dashboard.public_url`` names a public host, so this verified principal, not the gate flag,
    is the local-owner signal there. The socket peer is re-checked as defence in depth.
    """
    grant = getattr(request.state, "native_http_principal", None)
    if not isinstance(grant, Mapping) or not grant:
        return False
    peer = request.scope.get("hermes.gateway_socket_peer")
    if not peer or not _is_loopback_host(peer[0]):
        return False
    return _is_loopback_host(getattr(request.app.state, "bound_host", ""))


def _legacy_loopback_request(request: Request) -> bool:
    """Hermes 0.21 (and Desktop-spawned backends): ungated loopback bind + loopback client."""
    if getattr(request.app.state, "auth_required", None) is not False:
        return False
    if not _is_loopback_host(getattr(request.app.state, "bound_host", "")):
        return False
    client = request.client
    return client is not None and _is_loopback_host(client.host)


def _is_plugin_host_request(request: Request) -> bool:
    """``plugins.isolation: host`` replays the request into a bare app in a child process."""
    state = request.app.state
    return (request.url.hostname == "plugin-host"
            and getattr(state, "bound_host", None) is None
            and getattr(state, "auth_required", None) is None)


def _local_desktop_denial(request: Request) -> Optional[str]:
    """None for this PC's own Desktop, else the error code. Never trusts caller-supplied headers."""
    if _native_owner_request(request) or _legacy_loopback_request(request):
        return None
    if _is_plugin_host_request(request):
        return "plugin_host_isolation"
    return "local_desktop_required"


def _is_local_desktop_request(request: Request) -> bool:
    return _local_desktop_denial(request) is None


def _local_desktop_error(request: Request) -> Optional[JSONResponse]:
    code = _local_desktop_denial(request)
    return None if code is None else _json({"ok": False, "error": code}, 403)


def _skills_payload(body: Mapping[str, Any]) -> tuple[str, bool]:
    query = body.get("query", "")
    include_archive = body.get("includeArchive", False)
    if not isinstance(query, str) or len(query) > 2000 or not isinstance(include_archive, bool):
        raise ValueError("bad_skills_filter")
    return query, include_archive


def _json(payload: dict[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status_code, headers={"Cache-Control": "no-store"})


def _error(exc: SessionHubError) -> JSONResponse:
    return _json({"ok": False, "error": exc.code}, 400)


@router.get("/health")
def health() -> JSONResponse:
    return _json({
        "ok": True,
        "contract": "session-hub-local-mvp",
        "profiles": list(_backend_module.PROFILE_NAMES),
        "allProfiles": list(_backend_module.LOCAL_PROFILE_NAMES) + _effective_remote_profiles(),
        "defaultPageSize": DEFAULT_PAGE_SIZE,
        "readonly": True,
    })


@router.get("/config")
def hub_config_payload() -> JSONResponse:
    """Profile checkboxes and branch labels for the desktop UI (no filesystem paths)."""
    payload = _settings().public_payload()
    remote = _effective_remote_profiles()
    payload["localProfiles"] = list(_backend_module.LOCAL_PROFILE_NAMES)
    payload["remoteProfiles"] = remote
    payload["remoteInstances"] = {
        key: value for key, value in payload.get("remoteInstances", {}).items() if f"{key}/default" in remote
    }
    payload["profileLabels"] = {
        key: value for key, value in payload.get("profileLabels", {}).items() if "/" not in key or key in remote
    }
    return _json(payload)


def _effective_remote_profiles() -> list[str]:
    """Branch profiles that can actually be read (configured, enabled and with a sync folder)."""
    if _TEST_SNAPSHOTS_DISABLED:
        return []
    dirs, _cache = _TEST_SNAPSHOTS or _default_snapshot_dirs()
    return [f"{instance}/default" for instance in _backend_module.REMOTE_INSTANCES if instance in dirs]


@router.get("/instances")
def instances() -> JSONResponse:
    """Freshness of every instance: HQ (local) plus each branch snapshot (fresh/stale/unknown/damaged/unsupported)."""
    try:
        return _json({"contract": "session-hub-instances-v1", "instances": _backend().instance_status()})
    except SessionHubError as exc:
        return _error(exc)


@router.post("/sessions")
def sessions(body: dict[str, Any]) -> JSONResponse:
    try:
        payload = body or {}
        date_from = _history_date_from(payload)
        result = _backend().list_sessions(
            tab=payload.get("tab", "all"),
            query=payload.get("query", ""),
            profiles=payload.get("profiles"),
            date_from=date_from,
            date_to=payload.get("dateTo"),
            telegram_types=payload.get("telegramTypes"),
            page_size=payload.get("pageSize", DEFAULT_PAGE_SIZE),
            cursor=payload.get("cursor"),
        )
        return _json(result)
    except SessionHubError as exc:
        return _error(exc)


@router.post("/kanban")
def kanban_cards(body: dict[str, Any]) -> JSONResponse:
    try:
        payload = body or {}
        result = _kanban_backend().list_cards(
            query=payload.get("query", ""),
            assignees=payload.get("profiles"),
            statuses=payload.get("statuses"),
            date_from=_history_date_from(payload),
            date_to=payload.get("dateTo"),
            page_size=payload.get("pageSize", DEFAULT_PAGE_SIZE),
            cursor=payload.get("cursor"),
        )
        return _json(result)
    except SessionHubError as exc:
        return _error(exc)
    except (TypeError, ValueError):
        return _json({"ok": False, "error": "bad_filter"}, 400)


@router.post("/external-ai/jobs")
def external_ai_jobs(body: dict[str, Any]) -> JSONResponse:
    try:
        payload = body or {}
        return _json(_external_ai_reader().list_jobs(
            query=payload.get("query", ""),
            statuses=payload.get("statuses"),
            page_size=payload.get("pageSize", DEFAULT_PAGE_SIZE),
            cursor=payload.get("cursor"),
        ))
    except ExternalAIStatusError as exc:
        return _json({"ok": False, "error": exc.code}, 400)


@router.post("/ai-sessions")
def ai_sessions(body: dict[str, Any]) -> JSONResponse:
    try:
        payload = body or {}
        cache_path, _sources = _ai_sessions_paths()
        return _json(AISessionsReader(cache_path).list_sessions(
            query=payload.get("query", ""),
            sources=payload.get("sources"),
            include_archived=bool(payload.get("includeArchived", False)),
            page_size=payload.get("pageSize", DEFAULT_PAGE_SIZE),
            cursor=payload.get("cursor"),
        ))
    except AISessionsError as exc:
        return _json({"ok": False, "error": exc.code}, 400)


@router.post("/ai-sessions/refresh")
def ai_sessions_refresh(request: Request, body: dict[str, Any]) -> JSONResponse:
    denied = _local_desktop_error(request)
    if denied is not None:
        return denied
    try:
        cache_path, sources = _ai_sessions_paths()
        result = _ai_sessions_module.collect(cache_path, sources)
        return _json({"ok": True, "collectedAt": result["collectedAt"], "sources": result["sources"], "count": len(result["items"])})
    except AISessionsError as exc:
        return _json({"ok": False, "error": exc.code}, 400)
    except OSError:
        return _json({"ok": False, "error": "ai_sessions_collect_failed"}, 500)


@router.post("/skills/search")
def skills_search(request: Request, body: dict[str, Any]) -> JSONResponse:
    denied = _local_desktop_error(request)
    if denied is not None:
        return denied
    try:
        query, include_archive = _skills_payload(body or {})
        return _json(_skills_index().search(query, include_archive=include_archive))
    except (TypeError, ValueError):
        return _json({"ok": False, "error": "bad_skills_filter"}, 400)


@router.post("/skills/refresh")
def skills_refresh(request: Request, body: dict[str, Any]) -> JSONResponse:
    denied = _local_desktop_error(request)
    if denied is not None:
        return denied
    try:
        query, include_archive = _skills_payload(body or {})
        return _json(_skills_index().refresh(query, include_archive=include_archive))
    except (TypeError, ValueError):
        return _json({"ok": False, "error": "bad_skills_filter"}, 400)


@router.get("/kanban/{board}/{task_id}")
def kanban_card(board: str, task_id: str) -> JSONResponse:
    try:
        return _json(_kanban_backend().get_card(board, task_id))
    except SessionHubError as exc:
        return _error(exc)


@router.post("/revalidate")
def revalidate(body: dict[str, Any]) -> JSONResponse:
    try:
        payload = body or {}
        result = _backend().revalidate_owner(
            str(payload.get("ownerProfile", "")),
            str(payload.get("storedSessionId", "")),
        )
        return _json(result)
    except SessionHubError as exc:
        return _error(exc)


@router.post("/projects/refresh")
def refresh_projects() -> JSONResponse:
    try:
        return _json(_project_hub().refresh())
    except ProjectHubError as exc:
        return _json({"ok": False, "error": str(exc)}, 400)


@router.get("/projects")
def projects() -> JSONResponse:
    try:
        hub = _project_hub()
        try:
            portfolio = hub.portfolio()
        except ProjectHubError:
            return _json(hub.refresh())
        # First open on a new install: the registry is still empty, so derive projects once.
        return _json(portfolio if portfolio.get("projects") else hub.refresh())
    except ProjectHubError as exc:
        return _json({"ok": False, "error": str(exc)}, 400)


@router.get("/projects/{project_id}")
def project_detail(project_id: str) -> JSONResponse:
    try:
        return _json(_project_hub().project_detail(project_id))
    except ProjectHubError as exc:
        return _json({"ok": False, "error": str(exc)}, 400)


@router.patch("/projects/{project_id}/overrides")
def update_project(project_id: str, body: dict[str, Any]) -> JSONResponse:
    try:
        return _json(_project_hub().update_override(project_id, body or {}))
    except ProjectHubError as exc:
        return _json({"ok": False, "error": str(exc)}, 400)


@router.get("/registry/export")
def export_registry() -> JSONResponse:
    try:
        return _json(_project_hub().export_snapshot())
    except ProjectHubError as exc:
        return _json({"ok": False, "error": str(exc)}, 400)


@router.post("/registry/restore")
def restore_registry(body: dict[str, Any]) -> JSONResponse:
    try:
        return _json(_project_hub().restore_snapshot(body or {}))
    except ProjectHubError as exc:
        return _json({"ok": False, "error": str(exc)}, 400)


@router.api_route(
    "/{unmatched_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    include_in_schema=False,
)
def not_found(unmatched_path: str) -> JSONResponse:
    return _json({"ok": False, "error": "not_found"}, 404)
