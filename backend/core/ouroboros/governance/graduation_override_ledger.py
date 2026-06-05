"""Slice 96 — durable env-override ledger + boot-time applier.

The sanctioned, cage-respecting delivery channel for an autonomous
graduation. The :class:`AutonomousGraduationEngine` (Module 1) decides
that a built-but-dormant §33.1 subsystem has EARNED its master-flag
flip (FALSE→TRUE). It MUST NOT edit source defaults — editing
``flag_registry`` / ``*_seed.py`` source lands inside
``backend/core/ouroboros/governance/`` and trips the governance
boundary gate + AST hash-cap (→ APPROVAL_REQUIRED). Instead, the flip
is delivered as an **immutable receipt** appended to a durable JSONL,
and a **boot-time applier** injects the authorized flags into
``os.environ`` — env vars are OS-level, external to the cage, and
honor the hash-cap signatures perfectly (they don't mutate any
hashed source byte).

## The tiered-boundary invariant (LOAD-BEARING)

ONLY ``STANDARD``-tier graduations are ever written here. ``SAFETY``-
tier graduations (auto-activating a kill-switch / gate is itself a
governance self-modification — the §1 zero-order-doll invariant) are
routed to an operator advisory, NEVER an override. This is enforced
STRUCTURALLY: :func:`record_graduation` REFUSES any decision whose
``tier`` is not ``STANDARD``. Therefore a SAFETY flag can never reach
the override ledger, and the boot applier (which reads ONLY this
ledger) structurally cannot auto-activate a safety capability.

## Authority posture (§33.1)

* Default-off: the applier is gated by ``JARVIS_GRADUATION_OVERRIDE_-
  APPLY_ENABLED`` OR ``JARVIS_AUTONOMOUS_GRADUATION_ENGINE_ENABLED``
  (both default-FALSE) — dormant by default.
* Append-only, immutable receipts. Never edits or deletes a row.
* Best-effort: every public function NEVER raises.
* Authority-asymmetry: imports stdlib only at module load; the
  canonical ``cross_process_jsonl`` substrate is lazy-imported. NO
  orchestrator / iron_gate / policy / providers / candidate_generator
  / urgency_router / change_engine / semantic_guardian /
  auto_committer / risk_tier_floor imports anywhere.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


GRADUATION_OVERRIDE_LEDGER_SCHEMA_VERSION: str = "graduation_override.1"

_TRUTHY = ("1", "true", "yes", "on")

# The ONLY tier permitted to reach this ledger. SAFETY-tier flips are
# structurally excluded (they route to operator advisories).
_PERMITTED_TIER: str = "standard"

# Defensive caps.
MAX_LEDGER_FILE_BYTES: int = 4 * 1024 * 1024
MAX_RECORDS_LOADED: int = 50_000


# ---------------------------------------------------------------------------
# Master / sub gates
# ---------------------------------------------------------------------------


def _env_truthy(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUTHY


def engine_master_enabled() -> bool:
    """``JARVIS_AUTONOMOUS_GRADUATION_ENGINE_ENABLED`` (default
    FALSE)."""
    return _env_truthy("JARVIS_AUTONOMOUS_GRADUATION_ENGINE_ENABLED")


def apply_enabled() -> bool:
    """The boot applier is dormant unless EITHER the engine master OR
    its own apply sub-gate is on. Both default FALSE."""
    return (
        engine_master_enabled()
        or _env_truthy("JARVIS_GRADUATION_OVERRIDE_APPLY_ENABLED")
    )


def ledger_path() -> Path:
    """Append-only override-ledger path. Env-overridable via
    ``JARVIS_GRADUATION_OVERRIDE_LEDGER_PATH``; defaults to
    ``.jarvis/graduation_overrides.jsonl`` under cwd."""
    raw = os.environ.get("JARVIS_GRADUATION_OVERRIDE_LEDGER_PATH")
    if raw:
        return Path(raw)
    return Path(".jarvis") / "graduation_overrides.jsonl"


# ---------------------------------------------------------------------------
# Receipt record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OverrideRecord:
    """One immutable graduation receipt. Frozen — append-only."""

    flag_name: str
    authorized_true: bool
    tier: str
    receipt_id: str
    evidence: Dict[str, Any]
    evidence_sha256: str
    decided_at_unix: float
    decided_at_iso: str
    authorized_by: str = "autonomous_graduation_engine"
    schema_version: str = GRADUATION_OVERRIDE_LEDGER_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "flag_name": self.flag_name,
            "authorized_true": bool(self.authorized_true),
            "tier": self.tier,
            "receipt_id": self.receipt_id,
            "evidence": self.evidence,
            "evidence_sha256": self.evidence_sha256,
            "decided_at_unix": float(self.decided_at_unix),
            "decided_at_iso": self.decided_at_iso,
            "authorized_by": self.authorized_by,
        }


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Write — record_graduation
# ---------------------------------------------------------------------------


def record_graduation(decision: Any, *, now_unix: Optional[float] = None) -> bool:
    """Append an immutable receipt for an AUTO_FLIP decision.

    STRUCTURAL TIER GATE (load-bearing): refuses any decision whose
    ``tier`` is not STANDARD, regardless of disposition. A SAFETY flag
    can therefore NEVER reach this ledger — even a forged decision with
    a SAFETY tier + an AUTO_FLIP disposition is rejected here.

    Also refuses non-AUTO_FLIP dispositions (advisories / holds /
    disabled never produce an override).

    Returns True on a successful append, False otherwise. NEVER
    raises."""
    try:
        flag_name = getattr(decision, "flag_name", None)
        tier = getattr(decision, "tier", None)
        disposition = getattr(decision, "disposition", None)
        if not isinstance(flag_name, str) or not flag_name.strip():
            return False
        tier_val = getattr(tier, "value", tier)
        disp_val = getattr(disposition, "value", disposition)
        # STRUCTURAL TIER GATE — the load-bearing safety boundary.
        if tier_val != _PERMITTED_TIER:
            logger.info(
                "[GraduationOverride] refusing non-STANDARD tier=%s "
                "flag=%s (safety/governance flips route to advisory)",
                tier_val, flag_name,
            )
            return False
        # Only AUTO_FLIP decisions yield overrides.
        if disp_val != "auto_flip":
            return False
        evidence = getattr(decision, "evidence", {}) or {}
        if not isinstance(evidence, dict):
            evidence = {"_raw": str(evidence)}
        evidence_sha = getattr(decision, "evidence_sha256", "") or ""
        ts = float(now_unix) if now_unix is not None else time.time()
        record = OverrideRecord(
            flag_name=flag_name.strip(),
            authorized_true=True,
            tier=_PERMITTED_TIER,
            receipt_id=uuid.uuid4().hex,
            evidence=evidence,
            evidence_sha256=str(evidence_sha),
            decided_at_unix=ts,
            decided_at_iso=_utc_now_iso(),
            authorized_by="autonomous_graduation_engine",
        )
        return _append_record(record)
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.debug("[GraduationOverride] record failed: %s", exc)
        return False


def _append_record(record: OverrideRecord) -> bool:
    try:
        line = json.dumps(record.to_dict(), separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        logger.debug("[GraduationOverride] serialize failed: %s", exc)
        return False
    path = ledger_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.debug("[GraduationOverride] mkdir failed: %s", exc)
        return False
    try:
        from backend.core.ouroboros.governance.cross_process_jsonl import (
            flock_append_line,
        )
    except ImportError:
        # Stdlib fallback — never raises.
        try:
            with path.open("a", encoding="utf-8") as f:
                f.write(line)
                f.write("\n")
            return True
        except OSError as exc:
            logger.debug("[GraduationOverride] append failed: %s", exc)
            return False
    ok = bool(flock_append_line(path, line))
    if ok:
        logger.info(
            "[GraduationOverride] recorded flag=%s tier=%s receipt=%s",
            record.flag_name, record.tier, record.receipt_id,
        )
    return ok


# ---------------------------------------------------------------------------
# Read — all_overrides
# ---------------------------------------------------------------------------


def all_overrides() -> Tuple[OverrideRecord, ...]:
    """Read every receipt. Bounded + fail-open — NEVER raises."""
    path = ledger_path()
    if not path.exists():
        return ()
    try:
        size = path.stat().st_size
    except OSError:
        return ()
    if size > MAX_LEDGER_FILE_BYTES:
        logger.warning(
            "[GraduationOverride] %s exceeds cap (%d bytes) — refusing",
            path, size,
        )
        return ()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ()
    out: list = []
    for raw_line in text.splitlines():
        if len(out) >= MAX_RECORDS_LOADED:
            break
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        flag = obj.get("flag_name")
        if not isinstance(flag, str) or not flag:
            continue
        # Defensive read-side tier filter — a row that somehow carries
        # a non-STANDARD tier is dropped on read too (defense in depth).
        tier = str(obj.get("tier") or "")
        if tier != _PERMITTED_TIER:
            continue
        ev = obj.get("evidence")
        if not isinstance(ev, dict):
            ev = {}
        try:
            out.append(OverrideRecord(
                flag_name=flag,
                authorized_true=bool(obj.get("authorized_true", False)),
                tier=tier,
                receipt_id=str(obj.get("receipt_id") or ""),
                evidence=ev,
                evidence_sha256=str(obj.get("evidence_sha256") or ""),
                decided_at_unix=float(obj.get("decided_at_unix") or 0.0),
                decided_at_iso=str(obj.get("decided_at_iso") or ""),
                authorized_by=str(
                    obj.get("authorized_by")
                    or "autonomous_graduation_engine"
                ),
                schema_version=str(
                    obj.get("schema_version")
                    or GRADUATION_OVERRIDE_LEDGER_SCHEMA_VERSION
                ),
            ))
        except (TypeError, ValueError):
            continue
    return tuple(out)


# ---------------------------------------------------------------------------
# Boot applier — apply_overrides_to_environ
# ---------------------------------------------------------------------------


def apply_overrides_to_environ(
    environ: Optional[Any] = None,
) -> Tuple[str, ...]:
    """The BOOT applier. Reads the override ledger and, for each
    authorized STANDARD-tier flag, sets ``environ[flag] = "true"`` —
    but ONLY if the flag is not already present in ``environ`` (operator
    env-precedence wins — an explicit operator setting is NEVER
    overridden).

    Returns the tuple of flag names actually applied this call.

    SAFETY-tier flags are STRUCTURALLY absent from the ledger (they go
    to advisories), so this applier cannot auto-activate any safety
    capability — there is no code path here that reads a SAFETY flag.

    Gated by :func:`apply_enabled` (default-FALSE). Pure except the
    environ writes. NEVER raises."""
    target = environ if environ is not None else os.environ
    try:
        if not apply_enabled():
            return ()
        applied: list = []
        seen: set = set()
        for rec in all_overrides():
            if not rec.authorized_true:
                continue
            if rec.tier != _PERMITTED_TIER:  # defense in depth
                continue
            flag = rec.flag_name
            if flag in seen:
                continue
            seen.add(flag)
            # Env-precedence: never overwrite an operator-set value.
            try:
                already_present = flag in target
            except TypeError:
                already_present = False
            if already_present:
                continue
            try:
                target[flag] = "true"
            except Exception:  # noqa: BLE001 — defensive
                continue
            applied.append(flag)
        if applied:
            logger.info(
                "[GraduationOverride] boot applier injected %d flag(s): %s",
                len(applied), ", ".join(applied),
            )
        return tuple(applied)
    except Exception as exc:  # noqa: BLE001 — best-effort boot path
        logger.debug("[GraduationOverride] applier failed: %s", exc)
        return ()


__all__ = [
    "GRADUATION_OVERRIDE_LEDGER_SCHEMA_VERSION",
    "OverrideRecord",
    "all_overrides",
    "apply_enabled",
    "apply_overrides_to_environ",
    "engine_master_enabled",
    "ledger_path",
    "record_graduation",
]
