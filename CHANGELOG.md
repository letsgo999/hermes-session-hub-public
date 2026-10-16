# Changelog

## 0.7.1 — 2026-10-16 (Hermes 0.22 compatibility)

- Skills search/refresh and the 코덱스·클로드 Refresh button work again on Hermes 0.22. Hermes 0.22 serves
  Desktop's API from the gateway process and authenticates it with a one-request owner ticket; when
  `dashboard.public_url` is set, that listener reports `auth_required: true`, so 0.7.0's local-Desktop check
  returned 403 `local_desktop_required`. The check now also accepts Hermes' verified native owner
  (`request.state.native_http_principal`, loopback socket peer). Remote dashboard users stay blocked.
- `plugins.isolation: host` (new in 0.22) now gets an explicit `plugin_host_isolation` error instead of the
  generic 403. The Skills tab and the 코덱스·클로드 Refresh button explain both refusals in Korean.
- Skills index scans every profile folder in auto mode, including profiles without a `state.db` yet.
- Kanban cards and Project Hub refresh tolerate text timestamps (numeric strings, ISO-8601 — Hermes 0.22
  accepts these from outside writers) instead of failing the whole board; a card's latest run is chosen by
  parsed time, not SQLite's int-before-text ordering.

## 0.7.0 — 2026-10-14 (first public release)

- Profiles are auto-detected (`default` + every `profiles/<name>/state.db`) instead of a fixed list;
  the Desktop page draws its checkboxes from the new `GET /config` endpoint.
- New optional settings file `session-hub.config.json`: explicit profile list, profile labels,
  branch instances (`instances.remotes`, `syncRoot`, on by default), shared skills folder, Project Hub board.
- Project Hub projects come from `projects` in the settings file, or are derived from Kanban
  (`project_id` and workspace folder names) instead of three built-in pilot projects.
- A local profile that cannot be read (stale schema, missing/corrupt DB) is skipped and reported as
  "읽지 못한 프로필" instead of failing the whole list; it still fails closed when every selected
  local profile fails. Kanban "remote" detection no longer assumes fixed branch IDs.
- `GET /config` only advertises branch profiles that can actually be read; the Desktop page retries
  loading it while the Python half is starting.
- Removed installation-specific defaults (drive paths, board names, profile nicknames).
  Without settings no branch folder is read and no external skills folder is indexed.
- `install.ps1` / `uninstall.ps1`: staged copy with backup, `hermes plugins enable`, optional Codex/Claude
  collector scheduled task (Register-ScheduledTask, honours `-HermesHome`), optional starter settings, optional refresh of per-profile plugin copies.
- `tools/session_snapshot.py` (branch exporter) takes the sync folder from `--sync-root` / `HERMES_SYNC_ROOT`.

## 0.6.1 — 2026-10-13

- Accept Hermes `state.db` schema 26 and newer after column checks (schema 31 broke search).
- '외부 AI' tab replaced by '코덱스·클로드': Codex desktop, Claude desktop and Claude Code sessions,
  plus optional cloud-session summaries (`ai-session-report-v1`).
