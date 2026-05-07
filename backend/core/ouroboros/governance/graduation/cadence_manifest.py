"""Phase 9 cadence manifest — single source of truth for the
Live-Fire Graduation Soak schedule.

Closes the cadence-observability gap surfaced 2026-05-06: cron
fired but macOS denied execution before Python ran (EPERM /
operator policy / TCC); the failure was invisible to
``.jarvis/live_fire_graduation_history.jsonl`` because that
file is only appended by the harness AFTER subprocess invoke.
The manifest is the upstream witness that lets a separate
detector (Slice 3 ``cadence_status``) answer "did the schedule
fire and die before Python?"

Operator binding 2026-05-06 (verbatim):

  > "Drive thresholds from installer-written metadata (e.g.
  > schedule string or next-run hint written at --install time
  > from the existing CRON_SCHEDULE / launchd StartInterval —
  > single source of truth, no magic 86400 in random modules."

This module ships:

  * :class:`CadenceManifest` — frozen §33.5 versioned artifact
    (schema_version + symmetric to_dict / from_dict). Captures
    ``schedule_kind`` (cron|launchd), ``schedule_string`` (raw
    crontab line OR launchd StartInterval seconds), the
    derived ``interval_hint_s`` floor, ``installed_at_iso``,
    and ``installer_version``.
  * :func:`derive_interval_hint_s` — pure-function cron-spec
    parser supporting the patterns the installer actually
    emits: ``0 */N * * *`` / ``0 H1,H2,H3 * * *`` / ``M */N * * *``.
    Returns the WORST-CASE gap (max consecutive interval over
    24h) so the overdue detector errs on patient. NEVER raises.
  * :func:`manifest_path` — canonical location
    ``.jarvis/cadence_manifest.json`` (env-overridable).
  * :func:`write_manifest` / :func:`read_manifest` — atomic
    write + defensive read; both NEVER raise.

Architectural locks:

  * **Authority asymmetry** — pure stdlib substrate; no
    orchestrator / iron_gate / policy / providers / candidate_
    generator imports (AST-pinned).
  * **Single source of truth** — the only knower of the
    cadence interval is this module's manifest read; consumers
    compose :func:`read_manifest` instead of guessing magic
    numbers (no module elsewhere may hardcode ``86400`` /
    ``43200`` / ``28800`` for cadence purposes — convention,
    not yet AST-pinned globally).
  * **Versioned-artifact-contract (§33.5)** — manifest carries
    explicit ``schema_version``; readers tolerate legacy.
  * **NEVER raises** across all public surfaces.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


CADENCE_MANIFEST_SCHEMA_VERSION: str = "cadence_manifest.1"


# Floor — refuse to derive an interval below 60s (would imply
# every-minute scheduling which is operator-unintended for soaks).
_INTERVAL_HINT_MIN_S: int = 60

# Ceiling — refuse to derive an interval above 7 days (would
# imply effectively-no-cadence; operator likely meant a less
# permissive schedule).
_INTERVAL_HINT_MAX_S: int = 7 * 24 * 3600


# ---------------------------------------------------------------------------
# Manifest path resolution
# ---------------------------------------------------------------------------


def manifest_path() -> Path:
    """Canonical path. Env-overridable for tests:
    ``JARVIS_CADENCE_MANIFEST_PATH``."""
    raw = os.environ.get("JARVIS_CADENCE_MANIFEST_PATH", "")
    if raw:
        return Path(raw)
    return Path(".jarvis") / "cadence_manifest.json"


# ---------------------------------------------------------------------------
# Versioned manifest artifact (§33.5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CadenceManifest:
    """Frozen cadence manifest. Adopts §33.5
    Versioned-Artifact-Contract."""

    schema_version: str
    schedule_kind: str  # "cron" | "launchd"
    schedule_string: str  # raw crontab line OR str(StartInterval)
    interval_hint_s: int  # worst-case gap derived from schedule
    installed_at_iso: str
    installed_at_epoch: float
    installer_version: str
    # Free-form extras (e.g. cost_cap_usd, wall_cap_s, timeout_s)
    # so the manifest is a complete forensic witness without
    # requiring schema bumps for additive operator metadata.
    extras: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "schedule_kind": self.schedule_kind,
            "schedule_string": self.schedule_string,
            "interval_hint_s": int(self.interval_hint_s),
            "installed_at_iso": self.installed_at_iso,
            "installed_at_epoch": float(
                self.installed_at_epoch,
            ),
            "installer_version": self.installer_version,
            "extras": dict(self.extras),
        }

    @classmethod
    def from_dict(
        cls, payload: Dict[str, Any],
    ) -> Optional["CadenceManifest"]:
        """Defensive deserialize. Returns None on malformed
        input rather than raising."""
        try:
            if not isinstance(payload, dict):
                return None
            kind = str(payload.get("schedule_kind") or "")
            if kind not in ("cron", "launchd"):
                return None
            return cls(
                schema_version=str(
                    payload.get("schema_version")
                    or CADENCE_MANIFEST_SCHEMA_VERSION,
                ),
                schedule_kind=kind,
                schedule_string=str(
                    payload.get("schedule_string") or "",
                ),
                interval_hint_s=int(
                    payload.get("interval_hint_s") or 0,
                ),
                installed_at_iso=str(
                    payload.get("installed_at_iso") or "",
                ),
                installed_at_epoch=float(
                    payload.get("installed_at_epoch") or 0.0,
                ),
                installer_version=str(
                    payload.get("installer_version") or "",
                ),
                extras=(
                    dict(payload.get("extras") or {})
                    if isinstance(
                        payload.get("extras"), dict,
                    )
                    else {}
                ),
            )
        except Exception:  # noqa: BLE001 — defensive
            return None


# ---------------------------------------------------------------------------
# Cron-spec parser — pure function, NEVER raises
# ---------------------------------------------------------------------------


def _parse_field(field: str, lo: int, hi: int) -> Optional[List[int]]:
    """Parse one cron field into the sorted list of trigger
    values within [lo, hi]. Supports ``*`` / ``*/N`` /
    comma-separated lists / single literal. Returns None on any
    parse failure or unsupported pattern."""
    try:
        f = field.strip()
        if not f:
            return None
        if f == "*":
            return list(range(lo, hi + 1))
        # */N — every N within [lo, hi]
        if f.startswith("*/"):
            n_raw = f[2:]
            n = int(n_raw)
            if n <= 0:
                return None
            return list(range(lo, hi + 1, n))
        # comma-separated literals: 6,14,22
        if "," in f:
            out: List[int] = []
            for tok in f.split(","):
                v = int(tok.strip())
                if v < lo or v > hi:
                    return None
                out.append(v)
            return sorted(set(out))
        # single literal
        v = int(f)
        if v < lo or v > hi:
            return None
        return [v]
    except (TypeError, ValueError):
        return None


def derive_interval_hint_s(schedule_string: str) -> int:
    """Pure-function. Parse a cron 5-field expression and
    return the WORST-CASE consecutive-fire gap in seconds.

    The hint is intentionally conservative — the overdue
    detector uses it as a floor to avoid false positives on
    legitimate-but-uneven schedules like ``0 6,14,22 * * *``
    (8/8/8h gaps; hint = 8h = 28800s).

    Defaults / clamps:
      * blank / unparseable → 0 (caller treats as "unknown
        cadence" + uses a conservative override)
      * derived value <60s → 60s (floor; sub-minute soaks
        are operator-unintended)
      * derived value >7 days → 7 days (ceiling; longer
        means effectively no cadence)

    NEVER raises.
    """
    try:
        s = (schedule_string or "").strip()
        if not s:
            return 0
        # Strip a leading user-name field if present (system
        # crontabs sometimes carry it). Heuristic: 6 fields
        # where field[0] is non-digit non-* and not a list →
        # drop it.
        parts = s.split()
        if len(parts) == 6 and not (
            parts[0][0].isdigit()
            or parts[0].startswith("*")
        ):
            parts = parts[1:]
        if len(parts) != 5:
            return 0
        minutes = _parse_field(parts[0], 0, 59)
        hours = _parse_field(parts[1], 0, 23)
        # Skip dom / month / dow — for the worst-case-gap
        # heuristic we assume daily. Nonstandard schedules
        # (specific days only) over-estimate cadence; that's
        # the desired conservative direction.
        if minutes is None or hours is None:
            return 0
        # Compute every (hour, minute) trigger within a 24h
        # day, then derive the max consecutive gap.
        triggers: List[int] = []
        for h in hours:
            for m in minutes:
                triggers.append(h * 3600 + m * 60)
        triggers.sort()
        if not triggers:
            return 0
        if len(triggers) == 1:
            # Single fire/day → gap = 24h.
            return _clamp_interval_hint(24 * 3600)
        # Compute gaps between consecutive triggers + the
        # wrap-around gap (last trigger of day → first
        # trigger of next day).
        gaps: List[int] = []
        for i in range(1, len(triggers)):
            gaps.append(triggers[i] - triggers[i - 1])
        gaps.append(
            (24 * 3600 - triggers[-1]) + triggers[0],
        )
        worst = max(gaps)
        return _clamp_interval_hint(worst)
    except Exception:  # noqa: BLE001 — defensive
        return 0


def _clamp_interval_hint(value: int) -> int:
    if value < _INTERVAL_HINT_MIN_S:
        return _INTERVAL_HINT_MIN_S
    if value > _INTERVAL_HINT_MAX_S:
        return _INTERVAL_HINT_MAX_S
    return int(value)


def derive_interval_hint_s_from_launchd_interval(
    start_interval_s: int,
) -> int:
    """Launchd StartInterval is already seconds. Apply the
    same clamp + min-floor as the cron derivation."""
    try:
        v = int(start_interval_s)
        if v <= 0:
            return 0
        return _clamp_interval_hint(v)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------


def write_manifest(
    *,
    schedule_kind: str,
    schedule_string: str,
    installer_version: str = "1.0",
    extras: Optional[Dict[str, Any]] = None,
    path: Optional[Path] = None,
    interval_hint_s: Optional[int] = None,
) -> Tuple[bool, str]:
    """Atomic write. Returns ``(ok, detail)``. NEVER raises.

    ``interval_hint_s`` may be passed explicitly (e.g. launchd
    integration), otherwise the function derives it from
    ``schedule_string``.

    Atomic semantics: write to ``<path>.tmp`` + os.replace.
    """
    try:
        kind = (schedule_kind or "").strip().lower()
        if kind not in ("cron", "launchd"):
            return (False, f"unknown_schedule_kind:{kind!r}")
        target = Path(path) if path is not None else manifest_path()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return (False, f"mkdir_failed:{exc}")
        if interval_hint_s is None:
            if kind == "cron":
                hint = derive_interval_hint_s(schedule_string)
            else:
                # launchd schedule_string is the StartInterval
                # decimal as string.
                try:
                    hint = (
                        derive_interval_hint_s_from_launchd_interval(
                            int(schedule_string),
                        )
                    )
                except (TypeError, ValueError):
                    hint = 0
        else:
            hint = _clamp_interval_hint(int(interval_hint_s))
        now_iso = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ",
        )
        import time as _t
        manifest = CadenceManifest(
            schema_version=CADENCE_MANIFEST_SCHEMA_VERSION,
            schedule_kind=kind,
            schedule_string=str(schedule_string or ""),
            interval_hint_s=hint,
            installed_at_iso=now_iso,
            installed_at_epoch=_t.time(),
            installer_version=str(installer_version or "1.0"),
            extras=dict(extras or {}),
        )
        try:
            payload = json.dumps(
                manifest.to_dict(),
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError) as exc:
            return (False, f"serialize_failed:{exc}")
        tmp = target.with_suffix(target.suffix + ".tmp")
        try:
            tmp.write_text(payload + "\n", encoding="utf-8")
            os.replace(str(tmp), str(target))
        except OSError as exc:
            return (False, f"write_failed:{exc}")
        logger.info(
            "[CadenceManifest] wrote kind=%s interval_hint=%ds path=%s",
            kind, hint, target,
        )
        return (True, "ok")
    except Exception as exc:  # noqa: BLE001 — defensive
        return (False, f"unexpected:{exc}")


def read_manifest(
    *, path: Optional[Path] = None,
) -> Optional[CadenceManifest]:
    """Defensive read. Returns None on missing / malformed.
    NEVER raises."""
    try:
        target = Path(path) if path is not None else manifest_path()
        if not target.exists():
            return None
        try:
            text = target.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return None
        return CadenceManifest.from_dict(payload)
    except Exception:  # noqa: BLE001 — defensive
        return None


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins:

      1. ``cadence_manifest_authority_asymmetry`` — substrate
         purity. Forbids orchestrator+iron_gate+policy+providers+
         candidate_generator+urgency_router+change_engine+
         semantic_guardian imports.
      2. ``cadence_manifest_versioned_artifact_compliance`` —
         CadenceManifest carries `schema_version` field +
         exposes `to_dict` / `from_dict` (§33.5 contract).
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/graduation/"
        "cadence_manifest.py"
    )

    def _validate_authority_asymmetry(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        forbidden = (
            "orchestrator", "iron_gate", "policy", "providers",
            "candidate_generator", "urgency_router",
            "change_engine", "semantic_guardian",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for f in forbidden:
                    if f in module:
                        violations.append(
                            f"cadence_manifest.py MUST NOT "
                            f"import {module!r}"
                        )
        return tuple(violations)

    def _validate_versioned_artifact_compliance(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """CadenceManifest must carry schema_version + expose
        to_dict + from_dict per §33.5."""
        violations: list = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "CadenceManifest"
            ):
                method_names = {
                    sub.name
                    for sub in node.body
                    if isinstance(
                        sub, (ast.FunctionDef, ast.AsyncFunctionDef),
                    )
                }
                field_names = {
                    sub.target.id
                    for sub in node.body
                    if (
                        isinstance(sub, ast.AnnAssign)
                        and isinstance(sub.target, ast.Name)
                    )
                }
                if "schema_version" not in field_names:
                    violations.append(
                        "CadenceManifest MUST declare "
                        "schema_version field (§33.5)"
                    )
                if "to_dict" not in method_names:
                    violations.append(
                        "CadenceManifest MUST expose to_dict "
                        "(§33.5)"
                    )
                if "from_dict" not in method_names:
                    violations.append(
                        "CadenceManifest MUST expose from_dict "
                        "(§33.5)"
                    )
                return tuple(violations)
        violations.append(
            "CadenceManifest class definition missing"
        )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "cadence_manifest_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Cadence Slice 1 — substrate purity."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "cadence_manifest_versioned_artifact_compliance"
            ),
            target_file=target,
            description=(
                "Cadence Slice 1 — §33.5 versioned-artifact "
                "contract compliance."
            ),
            validate=_validate_versioned_artifact_compliance,
        ),
    ]


__all__ = [
    "CADENCE_MANIFEST_SCHEMA_VERSION",
    "CadenceManifest",
    "derive_interval_hint_s",
    "derive_interval_hint_s_from_launchd_interval",
    "manifest_path",
    "read_manifest",
    "register_shipped_invariants",
    "write_manifest",
]
