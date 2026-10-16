from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def generate_compatibility_matrix() -> dict[str, Any]:
    return {
        "contract": "hermes-session-hub-compat-v2",
        "hermesDesktopVersion": "0.22.2",
        "baselineHermesDesktopVersion": "0.21.7",
        "verifiedHermesDesktopVersions": ["0.21.7", "0.22.1", "0.22.2"],
        "supportedSchemaVersions": [26, 27, 28, 29, 30, 31],
        "liveSchemaVersion": 31,
        "pluginSdk": "plain-esm-desktop-plugin",
        "selectedDataPath": "strict_ro_fallback",
        "fallbackReason": "Hermes 0.21.7, 0.22.1 and 0.22.2 all use state.db schema 31; required columns are feature-detected. The official persisted-session list does not provide the bounded cross-profile message search/previews and strict SQLite no-write proof required by this MVP.",
        "capabilities": {
            "routePage": "pass",
            "sidebarNav": "pass",
            "paletteCommand": "pass",
            "openSessionInPlace": "pass",
            "profileAwareRequest": "pass",
            "reactQueryRefresh": "pass",
            "sessionRpcList": "available_not_equivalent",
            "sessionRpcSearchMessages": "insufficient",
            "strictNoWriteRpc": "insufficient",
        },
        "fallbackGuards": [
            "profile allowlist from auto-detection or session-hub.config.json",
            "SQLite URI mode=ro",
            "PRAGMA query_only=ON",
            "SELECT and read-only PRAGMA only",
            "stale or malformed schema fails closed",
            "no raw transcript, title, session, chat, user id, db path, or search term logging",
        ],
        "liveDesktopE2E": "not_run",
    }


def write_compatibility_matrix(path: str | Path) -> dict[str, Any]:
    matrix = generate_compatibility_matrix()
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(matrix, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return matrix
