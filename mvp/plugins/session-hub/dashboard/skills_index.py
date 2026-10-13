from __future__ import annotations

import hashlib
import os
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml


# Fallback profile list when the caller passes none: "default" plus any profiles/<name>/ found.
PROFILE_NAMES: tuple[str, ...] = ("default",)
MAX_SKILL_FILES = 4000
MAX_DIRECTORIES = 12000
MAX_SKILL_BYTES = 512 * 1024
MAX_RETURNED_RESULTS = 1000
# Optional shared skills folder (e.g. a synced drive). None = only Hermes' own skills folders.
CANONICAL_DEFAULT: Path | None = None
ARCHIVE_DIR_NAMES = ("skills-archive", "backup", "backups", "raw", "legacy", "staging")
_ARCHIVE_STATUSES = {"archive", "backup", "raw", "legacy", "staging"}
_QUERY_RE = re.compile(r'"([^"]+)"|(\S+)')
_USE_WHEN_RE = re.compile(r"\bUse when\b[^.\n]*(?:\.|$)", re.IGNORECASE)
_TRIGGER_HEADING_RE = re.compile(r"^#{1,6}\s*(?:triggers?|트리거)\s*$", re.IGNORECASE)
_HEADING_RE = re.compile(r"^#{1,6}\s+")
_STATUS_ORDER = {"active": 0, "archive": 1, "backup": 2, "legacy": 3, "staging": 4, "raw": 5, "canonical-candidate": 6}
_FIELD_WEIGHTS = {"title": 100, "description": 70, "triggers": 50, "tags": 35, "body": 5}


class SkillsIndexError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def resolve_base_hermes_home(explicit: str | Path | None = None) -> Path:
    """Resolve the shared Hermes base, even when HERMES_HOME names one profile."""
    value = explicit or os.environ.get("HERMES_HOME")
    if value:
        home = Path(value).expanduser()
        base = home.parent.parent
        if home.parent.name.casefold() == "profiles" and ((base / "config.yaml").is_file() or (base / "state.db").is_file()):
            return base
        return home
    local = os.environ.get("LOCALAPPDATA")
    return (Path(local) / "hermes") if local else Path.home() / ".hermes"


def detect_profiles(base_home: Path) -> tuple[str, ...]:
    """"default" first, then every profiles/<name>/ folder (with or without a state.db)."""
    found = ["default"]
    root = base_home / "profiles"
    try:
        children = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name.casefold()) if root.is_dir() else []
    except OSError:
        children = []
    found.extend(child.name for child in children if child.name != "default" and re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,63}", child.name))
    return tuple(found)


def default_archive_roots(base_home: Path, canonical_root: Path | None) -> tuple[Path, ...]:
    candidates = [base_home / name for name in ARCHIVE_DIR_NAMES]
    if canonical_root is not None:
        candidates.extend(canonical_root.parent / name for name in ARCHIVE_DIR_NAMES)
    unique: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = os.path.normcase(str(path.absolute()))
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return tuple(unique)


class SkillsIndex:
    """Bounded, read-only SKILL.md index with an in-memory cache."""

    def __init__(
        self,
        *,
        hermes_home: str | Path | None = None,
        canonical_root: str | Path | None = None,
        archive_roots: Iterable[str | Path] | None = None,
        max_files: int = MAX_SKILL_FILES,
        max_directories: int = MAX_DIRECTORIES,
        max_file_bytes: int = MAX_SKILL_BYTES,
        profiles: Iterable[str] | None = None,
    ) -> None:
        self.base_home = resolve_base_hermes_home(hermes_home)
        self.canonical_root = Path(canonical_root) if canonical_root is not None else CANONICAL_DEFAULT
        self.profiles = tuple(profiles) if profiles is not None else detect_profiles(self.base_home)
        self.archive_roots = tuple(Path(path) for path in archive_roots) if archive_roots is not None else default_archive_roots(self.base_home, self.canonical_root)
        self.max_files = max_files
        self.max_directories = max_directories
        self.max_file_bytes = max_file_bytes
        self._cache: dict[bool, tuple[list[dict[str, Any]], list[dict[str, Any]], int]] = {}

    def search(self, query: str = "", *, include_archive: bool = False, force_refresh: bool = False) -> dict[str, Any]:
        records, sources, refreshed_at = self._snapshot(include_archive, force_refresh=force_refresh)
        units = self._query_units(query)
        grouped = self._group(records)
        items: list[dict[str, Any]] = []
        for item in grouped:
            if not units and not item["runtimeCopies"]:
                if not include_archive or not any(copy["status"] in _ARCHIVE_STATUSES for copy in item["otherCopies"]):
                    continue
            scored = self._score(item, units)
            if scored is None:
                continue
            score, reason, snippet = scored
            item["score"] = score
            item["matchReason"] = reason
            item["snippet"] = snippet
            items.append(item)
        items.sort(key=lambda row: (-row["score"], row["title"].casefold(), row["id"]))
        total = len(items)
        return {
            "contract": "session-hub-skills-v1",
            "readonly": True,
            "query": str(query or ""),
            "includeArchive": bool(include_archive),
            "refreshedAt": refreshed_at,
            "items": items[:MAX_RETURNED_RESULTS],
            "page": {"returned": min(total, MAX_RETURNED_RESULTS), "totalKnown": total},
            "sources": sources,
            "truncated": total > MAX_RETURNED_RESULTS,
        }

    def refresh(self, query: str = "", *, include_archive: bool = False) -> dict[str, Any]:
        return self.search(query, include_archive=include_archive, force_refresh=True)

    def _snapshot(self, include_archive: bool, *, force_refresh: bool) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
        key = bool(include_archive)
        if not force_refresh and key in self._cache:
            return self._cache[key]
        records: list[dict[str, Any]] = []
        sources: list[dict[str, Any]] = []
        remaining_files = self.max_files
        remaining_directories = self.max_directories
        roots: list[tuple[Path, str, str | None]] = [(self.base_home / "skills", "active", "default")]
        roots.extend((self.base_home / "profiles" / profile / "skills", "active", profile) for profile in self.profiles if profile != "default")
        if self.canonical_root is not None:
            roots.append((self.canonical_root, "canonical", None))
        if include_archive:
            roots.extend((root, self._archive_kind(root), None) for root in self.archive_roots)

        seen_roots: set[str] = set()
        for root, kind, profile in roots:
            root_key = os.path.normcase(str(root.absolute()))
            if root_key in seen_roots:
                continue
            seen_roots.add(root_key)
            files, issues, directories, status = self._walk(root, remaining_files, remaining_directories)
            remaining_files -= len(files)
            remaining_directories -= directories
            source = {
                "kind": "canonical" if kind == "canonical" else kind,
                "profile": profile,
                "absolutePath": str(root.absolute()),
                "status": status,
                "skillFiles": len(files),
                "issues": issues,
            }
            sources.append(source)
            for path in files:
                record, read_issue = self._read_record(path, kind=kind, profile=profile)
                if record is not None:
                    records.append(record)
                if read_issue:
                    source["issues"] = sorted(set([*source["issues"], read_issue]))
                    if source["status"] == "ok":
                        source["status"] = "partial"
            if remaining_files <= 0 or remaining_directories <= 0:
                sources.append({
                    "kind": "limit",
                    "profile": None,
                    "absolutePath": None,
                    "status": "truncated",
                    "skillFiles": 0,
                    "issues": ["scan_limit_reached"],
                })
                break
        refreshed_at = int(time.time())
        snapshot = (records, sources, refreshed_at)
        self._cache[key] = snapshot
        return snapshot

    def _walk(self, root: Path, file_budget: int, directory_budget: int) -> tuple[list[Path], list[str], int, str]:
        if not root.exists():
            return [], ["source_not_found"], 0, "missing"
        if not root.is_dir():
            return [], ["source_not_directory"], 0, "error"
        try:
            resolved_root = root.resolve(strict=True)
        except OSError:
            return [], ["source_unresolvable"], 0, "error"

        files: list[Path] = []
        issues: list[str] = []
        stack = [resolved_root]
        seen_dirs: set[str] = set()
        directories = 0
        truncated = False
        while stack:
            current = stack.pop()
            current_key = os.path.normcase(str(current))
            if current_key in seen_dirs:
                continue
            seen_dirs.add(current_key)
            directories += 1
            if directories > directory_budget:
                truncated = True
                break
            try:
                entries = sorted(os.scandir(current), key=lambda entry: entry.name.casefold(), reverse=True)
            except OSError:
                issues.append("directory_unreadable")
                continue
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        child = Path(entry.path).resolve(strict=True)
                        if child.is_relative_to(resolved_root):
                            stack.append(child)
                        else:
                            issues.append("directory_escape_blocked")
                    elif entry.is_file(follow_symlinks=False) and entry.name.casefold() == "skill.md":
                        target = Path(entry.path).resolve(strict=True)
                        if not target.is_relative_to(resolved_root):
                            issues.append("file_escape_blocked")
                            continue
                        if len(files) >= file_budget:
                            truncated = True
                            break
                        files.append(target)
                except OSError:
                    issues.append("entry_unreadable")
            if truncated:
                break
        files.sort(key=lambda path: str(path).casefold())
        status = "truncated" if truncated else ("partial" if issues else "ok")
        return files, sorted(set(issues)), directories, status

    def _read_record(self, path: Path, *, kind: str, profile: str | None) -> tuple[dict[str, Any] | None, str | None]:
        try:
            size = path.stat().st_size
            if size > self.max_file_bytes:
                return None, "skill_file_too_large"
            raw = path.read_bytes()
        except OSError:
            return None, "skill_file_unreadable"
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeError:
            return None, "skill_file_not_utf8"
        frontmatter, body, parse_error = self._parse_frontmatter(text)
        title = str(frontmatter.get("name") or self._first_heading(body) or path.parent.name).strip()[:200]
        description = str(frontmatter.get("description") or "").strip()[:1000]
        tags_value = frontmatter.get("tags") or []
        if isinstance(tags_value, str):
            tags = [part.strip() for part in re.split(r"[,;]", tags_value) if part.strip()]
        elif isinstance(tags_value, (list, tuple, set)):
            tags = [str(value).strip() for value in tags_value if str(value).strip()]
        else:
            tags = []
        triggers = self._extract_triggers(description, body, frontmatter)
        digest = hashlib.sha256(raw).hexdigest()
        return {
            "logicalKey": (title or path.parent.name).casefold(),
            "title": title or path.parent.name,
            "description": description,
            "tags": sorted(set(tags), key=str.casefold),
            "triggers": triggers,
            "body": body[: self.max_file_bytes],
            "absolutePath": str(path),
            "profile": profile,
            "kind": kind,
            "status": "active" if kind == "active" else ("canonical-candidate" if kind == "canonical" else kind),
            "sha256": digest,
            "canonicalExplicit": self._explicit_canonical(frontmatter, path),
            "parseError": parse_error,
        }, None

    @staticmethod
    def _parse_frontmatter(text: str) -> tuple[Mapping[str, Any], str, str | None]:
        if not text.startswith("---"):
            return {}, text, None
        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            return {}, text, None
        try:
            end = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
        except StopIteration:
            return {}, text, "frontmatter_unclosed"
        raw_yaml = "\n".join(lines[1:end])
        try:
            loaded = yaml.safe_load(raw_yaml) or {}
            if not isinstance(loaded, Mapping):
                return {}, "\n".join(lines[end + 1 :]), "frontmatter_not_mapping"
            return loaded, "\n".join(lines[end + 1 :]), None
        except yaml.YAMLError:
            return {}, "\n".join(lines[end + 1 :]), "frontmatter_invalid"

    @staticmethod
    def _first_heading(body: str) -> str:
        for line in body.splitlines():
            if line.startswith("#"):
                return line.lstrip("#").strip()
        return ""

    @staticmethod
    def _extract_triggers(description: str, body: str, frontmatter: Mapping[str, Any]) -> str:
        explicit = frontmatter.get("trigger") or frontmatter.get("triggers")
        parts: list[str] = []
        if explicit:
            if isinstance(explicit, (list, tuple)):
                parts.extend(str(value).strip() for value in explicit if str(value).strip())
            else:
                parts.append(str(explicit).strip())
        use_when = _USE_WHEN_RE.search(description)
        if use_when:
            parts.append(use_when.group(0).strip())
        lines = body.splitlines()
        for index, line in enumerate(lines):
            if not _TRIGGER_HEADING_RE.match(line.strip()):
                continue
            section: list[str] = []
            for candidate in lines[index + 1 :]:
                if _HEADING_RE.match(candidate.strip()):
                    break
                if candidate.strip():
                    section.append(candidate.strip().lstrip("-* "))
                if len(" ".join(section)) >= 600:
                    break
            if section:
                parts.append(" ".join(section))
            break
        return " ".join(dict.fromkeys(part for part in parts if part))[:1000]

    @staticmethod
    def _explicit_canonical(frontmatter: Mapping[str, Any], path: Path) -> str | None:
        value = frontmatter.get("canonical")
        if value is True or (isinstance(value, str) and value.strip().casefold() in {"true", "yes", "canonical"}):
            return "frontmatter:canonical"
        declared = frontmatter.get("canonical_path") or frontmatter.get("canonicalPath")
        if declared:
            try:
                if Path(str(declared)).expanduser().resolve() == path.resolve():
                    return "frontmatter:canonical_path"
            except OSError:
                pass
        provenance = frontmatter.get("provenance") or frontmatter.get("org_provenance")
        if provenance and "canonical" in str(provenance).casefold():
            return "frontmatter:provenance"
        return None

    @staticmethod
    def _archive_kind(root: Path) -> str:
        lowered = root.name.casefold()
        if "backup" in lowered:
            return "backup"
        if "legacy" in lowered:
            return "legacy"
        if "staging" in lowered:
            return "staging"
        if lowered == "raw":
            return "raw"
        return "archive"

    def _group(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for record in records:
            groups[record["logicalKey"]].append(record)
        items: list[dict[str, Any]] = []
        for key in sorted(groups):
            copies = sorted(groups[key], key=lambda row: (row["status"], row["profile"] or "", row["absolutePath"].casefold()))
            preferred = next((row for row in copies if row["kind"] == "active"), copies[0])
            active_hashes = {row["sha256"] for row in copies if row["kind"] == "active"}
            candidates = [row for row in copies if row["kind"] == "canonical"]
            explicit = next((row for row in candidates if row["canonicalExplicit"]), None)
            matched = next((row for row in candidates if row["sha256"] in active_hashes), None)
            active_explicit = next((row for row in copies if row["kind"] == "active" and row["canonicalExplicit"]), None)
            canonical_row = explicit or matched or active_explicit
            if canonical_row is not None:
                evidence = canonical_row["canonicalExplicit"] or "sha256_match_active"
                canonical = {
                    "status": "verified",
                    "absolutePath": canonical_row["absolutePath"],
                    "paths": [row["absolutePath"] for row in candidates if row["sha256"] == canonical_row["sha256"]],
                    "evidence": evidence,
                }
            elif candidates:
                canonical = {
                    "status": "unverified",
                    "absolutePath": candidates[0]["absolutePath"],
                    "paths": [row["absolutePath"] for row in candidates],
                    "evidence": "candidate_only",
                }
            else:
                canonical = {"status": "unverified", "absolutePath": None, "paths": [], "evidence": "no_registered_canonical"}
            runtime = [self._copy_payload(row) for row in copies if row["kind"] == "active"]
            other = [self._copy_payload(row) for row in copies if row["kind"] != "active"]
            deployed = sorted({row["profile"] for row in copies if row["kind"] == "active" and row["profile"]}, key=lambda profile: (self.profiles.index(profile) if profile in self.profiles else len(self.profiles), profile))
            statuses = sorted({row["status"] for row in copies if row["kind"] != "canonical" or not runtime}, key=lambda status: (_STATUS_ORDER.get(status, 99), status))
            searchable = {
                "title": " ".join(row["title"] for row in copies),
                "description": " ".join(row["description"] for row in copies),
                "tags": " ".join(tag for row in copies for tag in row["tags"]),
                "triggers": " ".join(row["triggers"] for row in copies),
                "body": "\n".join(row["body"] for row in copies),
            }
            items.append({
                "id": key,
                "title": preferred["title"],
                "description": preferred["description"],
                "tags": sorted({tag for row in copies for tag in row["tags"]}, key=str.casefold),
                "triggers": preferred["triggers"],
                "statuses": statuses,
                "deployedProfiles": deployed,
                "canonical": canonical,
                "runtimeCopies": runtime,
                "otherCopies": other,
                "parseWarnings": sorted({row["parseError"] for row in copies if row["parseError"]}),
                "_searchable": searchable,
            })
        return items

    @staticmethod
    def _copy_payload(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "absolutePath": row["absolutePath"],
            "profile": row["profile"],
            "status": row["status"],
        }

    @staticmethod
    def _query_units(query: str) -> list[tuple[str, bool]]:
        units: list[tuple[str, bool]] = []
        for match in _QUERY_RE.finditer(str(query or "")):
            phrase, token = match.groups()
            value = (phrase if phrase is not None else token).strip().casefold()
            if value:
                units.append((value, phrase is not None))
        return units

    def _score(self, item: dict[str, Any], units: list[tuple[str, bool]]) -> tuple[int, str, str] | None:
        searchable = item.pop("_searchable")
        folded = {field: value.casefold() for field, value in searchable.items()}
        if not units:
            return 0, "전체 활성/정본 후보 목록", item["description"] or item["triggers"] or item["title"]
        score = 0
        matched_fields: list[str] = []
        best_snippet = ""
        best_weight = -1
        for value, quoted in units:
            candidates = [field for field, text in folded.items() if value in text]
            if not candidates:
                return None
            field = max(candidates, key=lambda name: _FIELD_WEIGHTS[name])
            weight = _FIELD_WEIGHTS[field] + (10 if quoted else 0)
            score += weight
            matched_fields.append(field)
            snippet_weight = weight + (200 if quoted else 0)
            if snippet_weight > best_weight:
                best_weight = snippet_weight
                best_snippet = self._snippet(searchable[field], value)
        reason = " · ".join(dict.fromkeys(matched_fields))
        return score, reason, best_snippet or item["description"] or item["title"]

    @staticmethod
    def _snippet(text: str, needle: str, limit: int = 220) -> str:
        compact = " ".join(str(text).split())
        if not compact:
            return ""
        folded = compact.casefold()
        index = folded.find(needle)
        if index < 0:
            return compact[:limit]
        start = max(0, index - 60)
        end = min(len(compact), index + len(needle) + 120)
        value = compact[start:end]
        if start:
            value = "…" + value
        if end < len(compact):
            value += "…"
        return value[: limit + 2]
