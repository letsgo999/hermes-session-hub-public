# Session Hub 0.7.1 — installable package

Hermes Desktop 0.21.x and 0.22.x plugin (verified on 0.21.7; 0.22.1 and 0.22.2 checked against Hermes source).

## Install (Windows)

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1 -EnableCollector
```

Options: `-WriteConfig` (starter settings file), `-NoEnable` (skip `hermes plugins enable session-hub`),
`-CollectorMinutes 15`, `-UpdateProfileCopies` (also refresh `profiles\<name>\plugins\session-hub`),
`-HermesHome <path>`, `-DryRun`.

Then restart Hermes and enable **Session Hub** in Desktop → Capabilities → Plugins.

## Contents

- `plugins/session-hub/plugin.yaml`, `__init__.py` — plugin manifest
- `plugins/session-hub/dashboard/` — read-only FastAPI namespace (`plugin_api.py`), settings and profile auto-detection (`hub_config.py`), session/Kanban reader (`sessionhub_backend.py`), project registry (`project_hub.py`), skills index, Codex/Claude collector (`ai_sessions.py`, run `python ai_sessions.py --collect`)
- `plugins/session-hub/desktop/plugin.js` — Desktop page (Project Hub, Desktop/Telegram/Kanban/코덱스·클로드/Skills tabs)
- `install.ps1`, `uninstall.ps1`, `session-hub.config.example.json`
- `manifest.json` — sha256 ledger of every file in this package

## Settings

Optional `%LOCALAPPDATA%\hermes\session-hub\session-hub.config.json` (copy `session-hub.config.example.json`).
Without it every local profile (`default` + `profiles\<name>\state.db`) is listed and no branch PCs are read.
Codex/Claude cloud-session summaries: `%LOCALAPPDATA%\hermes\session-hub\ai-sessions.config.json` → `{"reportsDir": "<folder>"}`.

## Rollback

`.\uninstall.ps1` disables the plugin, removes the collector task and moves the plugin folder to
`%LOCALAPPDATA%\hermes\session-hub\backups\`. Hermes `state.db` and Kanban DBs are never modified;
the plugin's own `project-registry.sqlite3` can be restored from its JSON export.
