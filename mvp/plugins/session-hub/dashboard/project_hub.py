from __future__ import annotations

import contextlib
import math
from datetime import datetime
import hashlib
import json
import sqlite3
import re
import time
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping
from urllib.parse import quote


# Projects come from session-hub.config.json ("projects": [{"id", "name", "goal", "aliases"}]).
# Without a list, projects are derived from the Kanban board itself: each distinct task
# project_id and each distinct workspace folder name becomes one project.
MAX_PROJECTS = 50
_PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def _slug(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return text[:64]


def normalize_projects(raw: Any) -> tuple[dict[str, Any], ...] | None:
    """Validate a configured project list; None means "derive from Kanban"."""
    if not isinstance(raw, (list, tuple)):
        return None
    projects: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "").strip()[:120]
        project_id = str(item.get("id") or "").strip().lower() or _slug(name)
        if not name or not _PROJECT_ID_RE.fullmatch(project_id) or project_id in seen:
            continue
        aliases = item.get("aliases") if isinstance(item.get("aliases"), (list, tuple)) else ()
        projects.append({
            "id": project_id,
            "name": name,
            "goal": str(item.get("goal") or "").strip()[:300],
            "aliases": tuple(str(alias)[:80] for alias in aliases if isinstance(alias, str) and alias.strip())[:10],
        })
        seen.add(project_id)
        if len(projects) >= MAX_PROJECTS:
            break
    return tuple(projects) if projects else None


def _workspace_folder(path: str) -> str:
    parts = [part for part in re.split(r"[\\/]+", path or "") if part and not part.endswith(":")]
    return parts[-1] if parts else ""


def _epoch(value: Any) -> int | None:
    """Kanban timestamp as epoch seconds; None for empty/unparseable. Hermes 0.22 lets outside writers
    store text stamps, so one odd card must not fail the whole board. Like Hermes' kanban_db._to_epoch:
    int/float, integer text, then ISO-8601 (a stamp without a zone is local time, as in Hermes).
    Also accepts decimal text (e.g. "1792022500.5"), which SQLite may hand back for an INTEGER column."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if math.isfinite(value) and abs(value) < 1e13 else None
    text = str(value).strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        try:
            return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
        except (ValueError, OSError, OverflowError):
            return None
    return int(number) if math.isfinite(number) and abs(number) < 1e13 else None


def derive_projects(tasks: Iterable[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    """One project per distinct task project_id / workspace folder, most recently active first."""
    latest: dict[str, tuple[int, str]] = {}
    for task in tasks:
        stamp = _epoch(task.get("created_at")) or 0
        for name in (str(task.get("project_id") or "").strip(), _workspace_folder(str(task.get("workspace_path") or ""))):
            project_id = _slug(name)
            if not name or not project_id or not _PROJECT_ID_RE.fullmatch(project_id):
                continue
            if project_id not in latest or stamp > latest[project_id][0]:
                latest[project_id] = (stamp, name)
    ordered = sorted(latest.items(), key=lambda row: (-row[1][0], row[0]))[:MAX_PROJECTS]
    return tuple({"id": project_id, "name": name, "goal": "", "aliases": ()} for project_id, (_stamp, name) in ordered)


OVERRIDE_FIELDS = frozenset({"priority", "dueAt", "ownerProfile", "nextAction", "pinned", "status"})
PROJECT_STATUSES = frozenset({"계획", "진행", "승인대기", "차단", "일시중지", "완료", "보관"})
REGISTRY_TABLES = (
    "projects",
    "project_links",
    "logical_work_items",
    "logical_work_members",
    "derived_session_insights",
    "project_overrides",
    "action_candidates",
    "audit_events",
)


class ProjectHubError(RuntimeError):
    pass


# Allowed owner profiles for overrides. The plugin loader sets this to the detected local
# profiles (set_owner_profiles); None accepts any well-formed profile name.
OWNER_PROFILES: frozenset[str] | None = None
_OWNER_PROFILE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _owner_profiles() -> frozenset[str] | None:
    if OWNER_PROFILES is not None:
        return OWNER_PROFILES
    # Fall back to the profiles Session Hub detected on this PC (hub_config, when loaded).
    import sys

    config = sys.modules.get("hermes_session_hub_config")
    try:
        return frozenset(config.current_settings().local_profiles) if config is not None else None
    except Exception:  # noqa: BLE001 - settings are advisory here
        return None


def set_owner_profiles(profiles: Any) -> None:
    global OWNER_PROFILES
    OWNER_PROFILES = frozenset(str(name) for name in profiles) if profiles is not None else None


def _valid_override(field: str, value: Any) -> bool:
    """Type-check explicit overrides so one bad PATCH cannot break every later read."""
    if field == "priority":
        return value is None or (isinstance(value, int) and not isinstance(value, bool) and -1000 <= value <= 1000)
    if field == "dueAt":
        return value is None or (isinstance(value, int) and not isinstance(value, bool) and value >= 0)
    if field == "ownerProfile":
        if value is None:
            return True
        if not isinstance(value, str) or not _OWNER_PROFILE_RE.fullmatch(value):
            return False
        allowed = _owner_profiles()
        return allowed is None or value in allowed
    if field == "nextAction":
        return value is None or (isinstance(value, str) and len(value) <= 500)
    if field == "pinned":
        return isinstance(value, bool)
    if field == "status":
        return value in PROJECT_STATUSES
    return False


def _canonical(value: str | None) -> str:
    return "".join(ch.lower() for ch in (value or "") if ch.isalnum())


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class ProjectHub:
    """Project registry with a writable local DB and strictly read-only sources."""

    def __init__(
        self,
        registry_path: str | Path,
        kanban_path: str | Path,
        *,
        now_fn=time.time,
        projects: Any = None,
    ) -> None:
        self.configured_projects = normalize_projects(projects)
        self._projects: tuple[dict[str, Any], ...] = self.configured_projects or ()
        self.registry_path = Path(registry_path)
        self.kanban_path = Path(kanban_path)
        self.now_fn = now_fn
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.registry_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    @contextlib.contextmanager
    def _source(self) -> Iterator[sqlite3.Connection]:
        if not self.kanban_path.is_file():
            raise ProjectHubError("kanban_db_unavailable")
        # Percent-encode the path so '#', '?' or '%' in a folder name cannot change which file SQLite opens.
        uri = f"file:{quote(self.kanban_path.resolve().as_posix(), safe='/:')}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=3.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        try:
            yield conn
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects(
                  id TEXT PRIMARY KEY, name TEXT NOT NULL, goal TEXT NOT NULL,
                  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS project_links(
                  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL,
                  source_type TEXT NOT NULL, source_id TEXT NOT NULL,
                  owner_profile TEXT, stored_session_id TEXT, evidence TEXT NOT NULL,
                  confidence REAL NOT NULL, explicit INTEGER NOT NULL DEFAULT 0,
                  created_at INTEGER NOT NULL,
                  UNIQUE(project_id, source_type, source_id),
                  FOREIGN KEY(project_id) REFERENCES projects(id)
                );
                CREATE TABLE IF NOT EXISTS logical_work_items(
                  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, title TEXT NOT NULL,
                  current_status TEXT, current_owner TEXT, next_action TEXT,
                  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                  FOREIGN KEY(project_id) REFERENCES projects(id)
                );
                CREATE TABLE IF NOT EXISTS logical_work_members(
                  logical_work_id TEXT NOT NULL, source_type TEXT NOT NULL,
                  source_id TEXT NOT NULL, relation TEXT NOT NULL,
                  PRIMARY KEY(logical_work_id, source_type, source_id),
                  FOREIGN KEY(logical_work_id) REFERENCES logical_work_items(id)
                );
                CREATE TABLE IF NOT EXISTS derived_session_insights(
                  project_id TEXT NOT NULL, owner_profile TEXT NOT NULL,
                  stored_session_id TEXT NOT NULL, insight_json TEXT NOT NULL,
                  updated_at INTEGER NOT NULL,
                  PRIMARY KEY(project_id, owner_profile, stored_session_id)
                );
                CREATE TABLE IF NOT EXISTS project_overrides(
                  project_id TEXT NOT NULL, field TEXT NOT NULL, value_json TEXT NOT NULL,
                  updated_at INTEGER NOT NULL,
                  PRIMARY KEY(project_id, field),
                  FOREIGN KEY(project_id) REFERENCES projects(id)
                );
                CREATE TABLE IF NOT EXISTS action_candidates(
                  id INTEGER PRIMARY KEY AUTOINCREMENT, source_type TEXT NOT NULL,
                  source_id TEXT NOT NULL, candidate_project_ids TEXT NOT NULL,
                  reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'unclassified',
                  created_at INTEGER NOT NULL,
                  UNIQUE(source_type, source_id)
                );
                CREATE TABLE IF NOT EXISTS audit_events(
                  id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL,
                  project_id TEXT, payload_json TEXT NOT NULL, created_at INTEGER NOT NULL
                );
                """
            )

    def refresh(self) -> dict[str, Any]:
        now = int(self.now_fn())
        tasks, edges = self._read_tasks_and_edges()
        self._projects = self.configured_projects or derive_projects(tasks)
        with self._connect() as conn:
            for project in self._projects:
                conn.execute(
                    """INSERT INTO projects(id,name,goal,created_at,updated_at) VALUES (?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET name=excluded.name,goal=excluded.goal,updated_at=excluded.updated_at""",
                    (project["id"], project["name"], project["goal"], now, now),
                )
            conn.execute("DELETE FROM action_candidates WHERE status='unclassified'")
            deterministic: dict[str, str] = {}
            for task in tasks:
                project_id, evidence = self._deterministic_project(task)
                if project_id:
                    deterministic[task["id"]] = project_id
                    conn.execute(
                        """INSERT INTO project_links(
                        project_id,source_type,source_id,owner_profile,stored_session_id,evidence,confidence,explicit,created_at
                        ) VALUES (?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(project_id,source_type,source_id) DO UPDATE SET
                        owner_profile=excluded.owner_profile,stored_session_id=excluded.stored_session_id,
                        evidence=excluded.evidence,confidence=excluded.confidence""",
                        (
                            project_id,
                            "kanban",
                            task["id"],
                            task.get("assignee"),
                            task.get("session_id"),
                            evidence,
                            1.0,
                            1 if evidence == "explicit_project_id" else 0,
                            now,
                        ),
                    )
                else:
                    candidates = self._candidate_projects(task)
                    if candidates:
                        conn.execute(
                            """INSERT INTO action_candidates(source_type,source_id,candidate_project_ids,reason,status,created_at)
                            VALUES (?,?,?,?,?,?)
                            ON CONFLICT(source_type,source_id) DO UPDATE SET
                            candidate_project_ids=excluded.candidate_project_ids,reason=excluded.reason,status='unclassified'""",
                            ("kanban", task["id"], _json(candidates), "title_or_body_similarity_only", "unclassified", now),
                        )
            # Links are derived from the source; drop links whose task moved to another project or disappeared.
            for row in conn.execute("SELECT id,project_id,source_id FROM project_links WHERE source_type='kanban'").fetchall():
                if deterministic.get(row["source_id"]) != row["project_id"]:
                    conn.execute("DELETE FROM project_links WHERE id=?", (row["id"],))
            self._rebuild_logical_work(conn, tasks, edges, deterministic, now)
            conn.execute(
                "INSERT INTO audit_events(event_type,project_id,payload_json,created_at) VALUES (?,?,?,?)",
                ("source_refresh", None, _json({"tasks": len(tasks), "links": len(deterministic), "readonly": True}), now),
            )
        return self.portfolio()

    def _read_tasks_and_edges(self) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
        with self._source() as conn:
            if conn.execute("PRAGMA query_only").fetchone()[0] != 1:
                raise ProjectHubError("source_not_readonly")
            columns = {row[1] for row in conn.execute("PRAGMA table_info('tasks')")}
            required = {"id", "title", "status", "workspace_path"}
            if not required.issubset(columns):
                raise ProjectHubError("kanban_schema_unsupported")
            wanted = [
                "id", "title", "body", "assignee", "status", "priority", "created_at",
                "started_at", "completed_at", "workspace_path", "result", "session_id", "project_id",
            ]
            select = [name if name in columns else f"NULL AS {name}" for name in wanted]
            tasks = [dict(row) for row in conn.execute(f"SELECT {','.join(select)} FROM tasks ORDER BY created_at,id")]
            for task in tasks:  # outside writers may store text stamps (Hermes 0.22 accepts them)
                task["created_at"] = _epoch(task.get("created_at")) or 0
                task["started_at"] = _epoch(task.get("started_at"))
                task["completed_at"] = _epoch(task.get("completed_at"))
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            edges: list[tuple[str, str]] = []
            if "task_links" in tables:
                link_cols = {row[1] for row in conn.execute("PRAGMA table_info('task_links')")}
                if {"parent_id", "child_id"}.issubset(link_cols):
                    edges = [(row[0], row[1]) for row in conn.execute("SELECT parent_id,child_id FROM task_links")]
        return tasks, edges

    def _deterministic_project(self, task: Mapping[str, Any]) -> tuple[str | None, str]:
        explicit = _canonical(str(task.get("project_id") or ""))
        workspace = _canonical(str(task.get("workspace_path") or ""))
        for project in self._projects:
            ids = {_canonical(project["id"]), _canonical(project["name"])}
            if explicit and explicit in ids:
                return project["id"], "explicit_project_id"
            if workspace and any(alias in workspace for alias in ids):
                return project["id"], "workspace_path"
        return None, ""

    def _candidate_projects(self, task: Mapping[str, Any]) -> list[str]:
        haystack = _canonical(f"{task.get('title') or ''} {task.get('body') or ''}")
        candidates = []
        for project in self._projects:
            aliases = {
                _canonical(project["name"]),
                _canonical(project["id"]),
                *(_canonical(a) for a in project["aliases"]),
            }
            if any(alias and alias in haystack for alias in aliases):
                candidates.append(project["id"])
        return candidates

    def _rebuild_logical_work(
        self,
        conn: sqlite3.Connection,
        tasks: list[dict[str, Any]],
        edges: list[tuple[str, str]],
        deterministic: dict[str, str],
        now: int,
    ) -> None:
        conn.execute("DELETE FROM logical_work_members")
        conn.execute("DELETE FROM logical_work_items")
        by_id = {task["id"]: task for task in tasks}
        adjacency: dict[str, set[str]] = {task_id: set() for task_id in deterministic}
        for parent, child in edges:
            if parent in deterministic and child in deterministic and deterministic[parent] == deterministic[child]:
                adjacency[parent].add(child)
                adjacency[child].add(parent)
        visited: set[str] = set()
        for task_id in sorted(deterministic):
            if task_id in visited:
                continue
            stack = [task_id]
            members: list[str] = []
            while stack:
                current = stack.pop()
                if current in visited:
                    continue
                visited.add(current)
                members.append(current)
                stack.extend(sorted(adjacency.get(current, ()), reverse=True))
            members.sort()
            project_id = deterministic[task_id]
            digest = hashlib.sha256((project_id + "\0" + "\0".join(members)).encode("utf-8")).hexdigest()[:16]
            logical_id = f"lw_{digest}"
            current = max((by_id[mid] for mid in members), key=lambda row: (row.get("created_at") or 0, row["id"]))
            conn.execute(
                """INSERT INTO logical_work_items(id,project_id,title,current_status,current_owner,next_action,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?)""",
                (
                    logical_id,
                    project_id,
                    current.get("title") or current["id"],
                    current.get("status"),
                    current.get("assignee"),
                    current.get("title") if current.get("status") not in {"done", "archived", "cancelled"} else None,
                    now,
                    now,
                ),
            )
            for index, member_id in enumerate(members):
                conn.execute(
                    "INSERT INTO logical_work_members(logical_work_id,source_type,source_id,relation) VALUES (?,?,?,?)",
                    (logical_id, "kanban", member_id, "current" if member_id == current["id"] else "history"),
                )

    def portfolio(self) -> dict[str, Any]:
        project_ids = [project["id"] for project in self._projects]
        if not project_ids:
            with self._connect() as conn:
                project_ids = [row["id"] for row in conn.execute("SELECT id FROM projects ORDER BY updated_at DESC, id")]
        # No projects yet (empty Kanban, nothing configured) is a valid, empty portfolio — not an error.
        projects = [self._project_summary(project_id) for project_id in project_ids]
        with self._connect() as conn:
            links = [self._link_dict(row) for row in conn.execute("SELECT * FROM project_links ORDER BY project_id,source_id")]
            candidates = [
                {"sourceType": row["source_type"], "sourceId": row["source_id"], "candidateProjectIds": json.loads(row["candidate_project_ids"]), "reason": row["reason"]}
                for row in conn.execute("SELECT * FROM action_candidates WHERE status='unclassified' ORDER BY source_id")
            ]
        actions = [
            {"projectId": project["id"], "status": project["effective"]["status"], "nextAction": project["effective"].get("nextAction"), "priority": project["effective"]["priority"]}
            for project in projects
            if project["effective"]["status"] not in {"완료", "보관"}
        ]
        actions.sort(key=lambda row: (-int(row["priority"] or 0), row["projectId"]))
        return {
            "contract": "project-hub-v1",
            "projects": projects,
            "actions": actions,
            "links": links,
            "unclassified": candidates,
            "sourceReadonly": True,
            "refreshedAt": int(self.now_fn()),
        }

    def _project_summary(self, project_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if row is None:
                raise ProjectHubError("unknown_project")
            override_rows = conn.execute("SELECT field,value_json FROM project_overrides WHERE project_id=?", (project_id,)).fetchall()
            overrides = {}
            for item in override_rows:
                try:
                    value = json.loads(item["value_json"])
                except (TypeError, ValueError):
                    continue
                if _valid_override(item["field"], value):
                    overrides[item["field"]] = value
            work = conn.execute("SELECT current_status,current_owner,next_action FROM logical_work_items WHERE project_id=?", (project_id,)).fetchall()
            priorities = conn.execute("SELECT source_id FROM project_links WHERE project_id=?", (project_id,)).fetchall()
        statuses = [item["current_status"] for item in work if item["current_status"]]
        if "blocked" in statuses:
            status = "차단"
        elif any(value in {"running", "ready", "todo", "review", "triage"} for value in statuses):
            status = "진행"
        elif statuses and all(value in {"done", "archived", "cancelled"} for value in statuses):
            status = "완료"
        else:
            status = "계획"
        effective = {
            "status": status,
            "priority": len(priorities),
            "ownerProfile": next((item["current_owner"] for item in work if item["current_owner"]), None),
            "nextAction": next((item["next_action"] for item in work if item["next_action"]), None),
            "dueAt": None,
            "pinned": False,
            **overrides,
        }
        return {"id": row["id"], "name": row["name"], "goal": row["goal"], "effective": effective, "linkedCount": len(priorities)}

    def project_detail(self, project_id: str) -> dict[str, Any]:
        summary = self._project_summary(project_id)
        with self._connect() as conn:
            links = [self._link_dict(row) for row in conn.execute("SELECT * FROM project_links WHERE project_id=? ORDER BY source_id", (project_id,))]
            work_rows = conn.execute("SELECT * FROM logical_work_items WHERE project_id=? ORDER BY id", (project_id,)).fetchall()
            logical = []
            for row in work_rows:
                members = [item[0] for item in conn.execute("SELECT source_id FROM logical_work_members WHERE logical_work_id=? ORDER BY source_id", (row["id"],))]
                logical.append({"id": row["id"], "title": row["title"], "status": row["current_status"], "ownerProfile": row["current_owner"], "nextAction": row["next_action"], "memberIds": members})
        linked_ids = [link["sourceId"] for link in links if link["sourceType"] == "kanban"]
        tasks = self._read_tasks(linked_ids)
        return {**summary, "links": links, "logicalWork": logical, "tasks": tasks}

    def _read_tasks(self, ids: list[str]) -> list[dict[str, Any]]:
        if not ids:
            return []
        tasks, _ = self._read_tasks_and_edges()
        wanted = set(ids)
        return [
            {
                "id": row["id"], "title": row.get("title"), "status": row.get("status"),
                "ownerProfile": row.get("assignee"), "priority": row.get("priority"),
                "sessionId": row.get("session_id"), "result": row.get("result"),
            }
            for row in tasks if row["id"] in wanted
        ]

    @staticmethod
    def _link_dict(row: sqlite3.Row) -> dict[str, Any]:
        source_session = None
        if row["stored_session_id"] and row["owner_profile"]:
            source_session = {"ownerProfile": row["owner_profile"], "storedSessionId": row["stored_session_id"]}
        return {
            "projectId": row["project_id"], "sourceType": row["source_type"], "sourceId": row["source_id"],
            "sourceSession": source_session, "evidence": row["evidence"], "explicit": bool(row["explicit"]),
        }

    def update_override(self, project_id: str, changes: Mapping[str, Any]) -> dict[str, Any]:
        unknown = set(changes) - OVERRIDE_FIELDS
        if unknown or not changes:
            raise ProjectHubError("invalid_override_fields")
        if "status" in changes and changes["status"] not in PROJECT_STATUSES:
            raise ProjectHubError("invalid_project_status")
        for field, value in changes.items():
            if not _valid_override(field, value):
                raise ProjectHubError(f"invalid_override_{field}")
        now = int(self.now_fn())
        with self._connect() as conn:
            if conn.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is None:
                raise ProjectHubError("unknown_project")
            for field, value in changes.items():
                conn.execute(
                    """INSERT INTO project_overrides(project_id,field,value_json,updated_at) VALUES (?,?,?,?)
                    ON CONFLICT(project_id,field) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at""",
                    (project_id, field, _json(value), now),
                )
            conn.execute(
                "INSERT INTO audit_events(event_type,project_id,payload_json,created_at) VALUES (?,?,?,?)",
                ("override_update", project_id, _json(dict(changes)), now),
            )
        return self.project_detail(project_id)

    def export_snapshot(self) -> dict[str, Any]:
        with self._connect() as conn:
            tables = {}
            for table in REGISTRY_TABLES:
                rows = conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                tables[table] = [dict(row) for row in rows]
        return {
            "contract": "project-hub-registry-snapshot-v1",
            "exportedAt": int(self.now_fn()),
            "tables": tables,
            "auditEvents": tables["audit_events"],
        }

    def restore_snapshot(self, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        if snapshot.get("contract") != "project-hub-registry-snapshot-v1" or not isinstance(snapshot.get("tables"), Mapping):
            raise ProjectHubError("invalid_snapshot")
        tables = snapshot["tables"]
        if set(tables) != set(REGISTRY_TABLES):
            raise ProjectHubError("invalid_snapshot_tables")
        try:
            with self._connect() as conn:
                # Column names come from the live schema, never from the request body (prevents SQL injection).
                allowed = {
                    table: [row["name"] for row in conn.execute(f"PRAGMA table_info('{table}')")]
                    for table in REGISTRY_TABLES
                }
                for table in REGISTRY_TABLES:
                    rows = tables[table]
                    if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows):
                        raise ProjectHubError("invalid_snapshot_rows")
                    for row in rows:
                        if not set(row).issubset(allowed[table]):
                            raise ProjectHubError("invalid_snapshot_columns")
                for table in reversed(REGISTRY_TABLES):
                    conn.execute(f"DELETE FROM {table}")
                for table in REGISTRY_TABLES:
                    for row in tables[table]:
                        columns = [col for col in allowed[table] if col in row]
                        placeholders = ",".join("?" for _ in columns)
                        conn.execute(
                            f"INSERT INTO {table}({','.join(columns)}) VALUES ({placeholders})",
                            tuple(row[col] for col in columns),
                        )
                conn.execute(
                    "INSERT INTO audit_events(event_type,project_id,payload_json,created_at) VALUES (?,?,?,?)",
                    ("snapshot_restore", None, _json({"exportedAt": snapshot.get("exportedAt")}), int(self.now_fn())),
                )
        except sqlite3.Error as exc:
            raise ProjectHubError("invalid_snapshot_rows") from exc
        return {"ok": True, "contract": "project-hub-registry-snapshot-v1"}
