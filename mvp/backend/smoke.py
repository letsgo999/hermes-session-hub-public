from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .sessionhub import PROFILE_NAMES, SessionHubBackend, hub_config


def default_profile_dbs() -> dict[str, Path]:
    root = hub_config.current_settings().hermes_home
    return {
        profile: (root / "state.db") if profile == "default" else (root / "profiles" / profile / "state.db")
        for profile in PROFILE_NAMES
    }


def run_aggregate_smoke(
    db_by_profile: dict[str, str | Path] | None = None,
    *,
    iterations: int = 1,
    include_items: bool = False,
) -> dict[str, Any]:
    is_live_default = db_by_profile is None
    paths = {profile: Path(path) for profile, path in (db_by_profile or default_profile_dbs()).items()}
    start = _composite_sha(paths)
    backend = SessionHubBackend(paths)
    aggregate = {
        "profiles": len(PROFILE_NAMES),
        "iterationsOk": 0,
        "desktopRows": 0,
        "telegramRows": 0,
        "items": [] if include_items else None,
    }
    for _ in range(iterations):
        desktop = backend.list_sessions(tab="desktop", page_size=100)
        telegram = backend.list_sessions(tab="telegram", page_size=100)
        aggregate["iterationsOk"] += 1
        aggregate["desktopRows"] = desktop["page"]["totalKnown"]
        aggregate["telegramRows"] = telegram["page"]["totalKnown"]
        if include_items:
            aggregate["items"] = desktop["items"][:3] + telegram["items"][:3]
    write_control_evidence = {
        "authorizerWriteAttemptsObserved": 0,
        "authorizerWriteOpsAllowed": 0,
        "writeProbeRejected": True,
        "profilesProbed": 0,
    }
    for profile in PROFILE_NAMES:
        measured = backend.measure_authorizer_write_denial(profile)
        write_control_evidence["authorizerWriteAttemptsObserved"] += measured["authorizerWriteAttemptsObserved"]
        write_control_evidence["authorizerWriteOpsAllowed"] += measured["authorizerWriteOpsAllowed"]
        write_control_evidence["writeProbeRejected"] = (
            write_control_evidence["writeProbeRejected"] and measured["writeProbeRejected"]
        )
        write_control_evidence["profilesProbed"] += 1
    end = _composite_sha(paths)
    if not include_items:
        aggregate.pop("items", None)
    observed_drift = start != end
    return {
        "contract": "aggregate-only; no raw ids, titles, messages, user/chat ids, or paths",
        "profiles": list(PROFILE_NAMES),
        "iterations": iterations,
        "startSha256": start,
        "endSha256": end,
        "observedDbDrift": observed_drift,
        "hashAssessment": _hash_assessment(
            is_live_default=is_live_default,
            observed_drift=observed_drift,
        ),
        "writeControlEvidence": write_control_evidence,
        "readonlyControls": {
            "sqliteUriMode": "ro",
            "pragmaQueryOnly": True,
            "authorizerDeniesWrites": True,
            "sqlClass": "SELECT/PRAGMA-only",
        },
        "aggregate": aggregate,
    }


def _hash_assessment(*, is_live_default: bool, observed_drift: bool) -> str:
    if is_live_default:
        return "inconclusive_external_live_db_drift" if observed_drift else "unchanged_during_live_observation_window"
    return "unexpected_injected_fixture_drift" if observed_drift else "unchanged_during_isolated_fixture_smoke"


def write_smoke(path: str | Path, *, iterations: int = 3) -> dict[str, Any]:
    result = run_aggregate_smoke(iterations=iterations, include_items=False)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def _composite_sha(paths: dict[str, Path]) -> str:
    digest = hashlib.sha256()
    for profile in PROFILE_NAMES:
      path = paths[profile]
      digest.update(profile.encode("utf-8"))
      digest.update(hashlib.sha256(path.read_bytes()).hexdigest().encode("ascii"))
    return digest.hexdigest()
