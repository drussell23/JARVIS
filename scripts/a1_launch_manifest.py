"""A1 Launch Manifest -- deterministic, schema-validated, fail-closed config artifact.

Single authoritative source for the three A1-critical flags:
  * model                 -- the DW primary pin
  * native_tool_forcing   -- DW native tool-call format (required for Iron Gate)
  * epistemic_feedback    -- Provider Quarantine / Cryo-DLQ escalation path

Lifecycle:
  build_manifest()        -- pure/deterministic dict; no I/O, no timestamps in core
  write_manifest()        -- serialize to disk as pretty JSON (adds generated_for tag)
  load_and_validate()     -- parse + schema check; FAIL-CLOSED on any error
  apply_manifest()        -- single authoritative apply point into an env dict

The ``generated_for`` timestamp is written OUTSIDE the validated core so pure
round-trip tests (build -> write -> load -> validate) are deterministic.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

SCHEMA_VERSION = "a1-launch.1"
REQUIRED_KEYS = {"schema_version", "model", "native_tool_forcing", "epistemic_feedback", "failover_lifecycle", "file_isolation"}


class A1ManifestError(Exception):
    """Raised by load_and_validate on any manifest failure. FAIL-CLOSED."""


def build_manifest(
    *,
    model: str,
    native_tool_forcing: bool,
    epistemic_feedback: bool,
    failover_lifecycle: bool = False,
    file_isolation: bool = False,
    seed: Optional[int] = None,
    cost_cap: Optional[float] = None,
    max_wall_seconds: Optional[int] = None,
    extra_flags: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build manifest dict. Pure/deterministic. No timestamps in validated core."""
    core: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "model": model,
        "native_tool_forcing": native_tool_forcing,
        "epistemic_feedback": epistemic_feedback,
        "failover_lifecycle": failover_lifecycle,
        "file_isolation": file_isolation,
    }
    if seed is not None:
        core["seed"] = seed
    if cost_cap is not None:
        core["cost_cap"] = cost_cap
    if max_wall_seconds is not None:
        core["max_wall_seconds"] = max_wall_seconds
    if extra_flags:
        core["extra_flags"] = extra_flags
    # generated_for tag is OUTSIDE the validated core -- added by write_manifest.
    return core


def write_manifest(path: "str | Path", manifest: Dict[str, Any]) -> None:
    """Write manifest to path as pretty JSON, adding generated_for tag outside the core."""
    import datetime
    out = dict(manifest)
    out["generated_for"] = datetime.datetime.utcnow().isoformat() + "Z"
    Path(path).write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")


def load_and_validate(path: "str | Path") -> Dict[str, Any]:
    """Load and validate manifest. Raises A1ManifestError on any failure. FAIL-CLOSED."""
    p = Path(path)
    if not p.exists():
        raise A1ManifestError(f"A1 launch manifest not found: {p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise A1ManifestError(f"A1 launch manifest unparseable: {exc}") from exc
    if data.get("schema_version") != SCHEMA_VERSION:
        raise A1ManifestError(
            f"A1 launch manifest wrong schema_version: got {data.get('schema_version')!r},"
            f" expected {SCHEMA_VERSION!r}"
        )
    missing = REQUIRED_KEYS - set(data.keys())
    if missing:
        raise A1ManifestError(
            f"A1 launch manifest missing required keys: {sorted(missing)}"
        )
    return data


def apply_manifest(manifest: Dict[str, Any], env: Dict[str, str]) -> Dict[str, str]:
    """Forcefully write manifest config into env dict. Returns mutated env.
    Single authoritative apply point -- compose_env must call this."""
    env["JARVIS_DW_PRIMARY_OVERRIDE"] = manifest["model"]
    if manifest.get("native_tool_forcing"):
        env["JARVIS_DW_NATIVE_TOOL_FORCING_ENABLED"] = "true"
    if manifest.get("epistemic_feedback"):
        env["JARVIS_EPISTEMIC_FEEDBACK_ENABLED"] = "true"
    # Deterministic failover-lifecycle pin: always written (true/false), never absent.
    # Prevents the shell-var-propagation gap that let JARVIS_FAILOVER_LIFECYCLE_ENABLED
    # default to "true" on the node and spawn J-Prime mid-soak.
    env["JARVIS_FAILOVER_LIFECYCLE_ENABLED"] = "true" if manifest.get("failover_lifecycle") else "false"
    # Deterministic file-isolation pin: always written (true/false), never absent.
    # On an ephemeral cloud node there is no operator checkout to protect, so both
    # isolation flags MUST be false -> autonomous writes land in repo_root ->
    # durable commit (written=True), fixing the fsm_classify_to_applied A1 blocker.
    # Mirrors the failover_lifecycle pin: BOTH flags written deterministically.
    env["JARVIS_FILE_ISOLATION_ENABLED"] = "true" if manifest.get("file_isolation") else "false"
    env["JARVIS_DETERMINISTIC_ISOLATION_LOCK_ENABLED"] = "true" if manifest.get("file_isolation") else "false"
    if manifest.get("seed") is not None:
        env["JARVIS_CHAOS_SEED"] = str(manifest["seed"])
    if manifest.get("cost_cap") is not None:
        env["OUROBOROS_BATTLE_COST_CAP"] = str(manifest["cost_cap"])
    if manifest.get("max_wall_seconds") is not None:
        env["OUROBOROS_BATTLE_MAX_WALL_SECONDS"] = str(manifest["max_wall_seconds"])
    for k, v in (manifest.get("extra_flags") or {}).items():
        env[k] = str(v)
    return env
