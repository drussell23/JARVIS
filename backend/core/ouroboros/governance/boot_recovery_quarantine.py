"""Slice 123 Phase 1 — Boot-Recovery Provenance Quarantine.

On boot the GovernedLoopService replays orphaned APPLIED ops from the durable
ledger. Ops that lack provenance (`target_file` / `rollback_hash`) cannot be
safely rolled back, so they are escalated to `manual_intervention_required`
(correct fail-safe). But historically those unvouched ops *stay in the ledger
and re-surface on every boot*, flooding the intake (observed: 81 in one boot)
and starving fresh, Layer-4-eligible work.

This module adds an automated triage step: when an op trips
`boot_recovery_missing_provenance`, its raw payload is migrated to an isolated
`.jarvis/quarantine/` directory and recorded, so the escalation still happens
(auditability preserved) but the cruft is sequestered out of the hot path
instead of re-clogging it.

Design invariants:
  • NEVER raises — quarantine is best-effort; a write failure must not break the
    recovery loop (the op still escalates exactly as before).
  • ADDITIVE — does not replace the existing escalation/postmortem; it sequesters
    the payload alongside it. Default-off (§33.1); byte-identical when disabled.
  • No deletion of source-of-truth — the ledger entry is untouched here; the
    quarantine dir is a SIDE record the operator can inspect or purge.

Master switch: ``JARVIS_BOOT_RECOVERY_QUARANTINE_ENABLED`` (default **false**).
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_ENV_MASTER = "JARVIS_BOOT_RECOVERY_QUARANTINE_ENABLED"
_ENV_DIR = "JARVIS_QUARANTINE_DIR"
_DEFAULT_DIR = ".jarvis/quarantine"


def quarantine_enabled() -> bool:
    return os.getenv(_ENV_MASTER, "false").strip().lower() in ("1", "true", "yes", "on")


def quarantine_dir() -> Path:
    return Path(os.getenv(_ENV_DIR, _DEFAULT_DIR))


def _safe_op_id(op_id: str) -> str:
    # Filename hygiene — keep it filesystem-safe without losing identity.
    return "".join(c if (c.isalnum() or c in "-_.") else "_" for c in str(op_id))[:120]


def quarantine_op(
    op_id: str,
    payload: Dict[str, Any],
    reason: str,
    *,
    now: Optional[float] = None,
) -> Optional[str]:
    """Migrate one unvouched recovery op's raw payload to the quarantine dir.

    Returns the written path, or ``None`` if quarantine is disabled or the write
    fails (logged). NEVER raises — the caller's recovery escalation proceeds
    regardless.
    """
    if not quarantine_enabled():
        return None
    try:
        ts = now if now is not None else time.time()
        d = quarantine_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{int(ts)}_{_safe_op_id(op_id)}.json"
        record = {
            "op_id": op_id,
            "reason": reason,
            "quarantined_at": ts,
            "schema_version": "quarantine.1",
            "payload": _jsonable(payload),
        }
        path.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
        logger.warning(
            "[BootRecoveryQuarantine] op=%s reason=%s → sequestered at %s "
            "(escalation still emitted; payload off the hot path)",
            op_id, reason, path,
        )
        return str(path)
    except Exception as exc:  # noqa: BLE001 - best-effort; never break recovery
        logger.debug("[BootRecoveryQuarantine] quarantine swallowed for op=%s: %s", op_id, exc)
        return None


def _jsonable(obj: Any) -> Any:
    """Best-effort coerce to a JSON-serializable shape (the payload may hold
    enums / dataclasses); falls back to repr."""
    try:
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        if isinstance(obj, dict):
            return {str(k): _jsonable(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [_jsonable(v) for v in obj]
        return repr(obj)


def list_quarantined(limit: int = 100) -> List[Dict[str, Any]]:
    """Read-only: recent quarantined op records (for observability)."""
    out: List[Dict[str, Any]] = []
    try:
        d = quarantine_dir()
        if not d.exists():
            return out
        files = sorted(d.glob("*.json"), key=lambda p: p.name, reverse=True)[:limit]
        for f in files:
            try:
                out.append(json.loads(f.read_text(encoding="utf-8")))
            except Exception:  # noqa: BLE001
                out.append({"file": f.name, "error": "unreadable"})
    except Exception as exc:  # noqa: BLE001
        logger.debug("[BootRecoveryQuarantine] list swallowed: %s", exc)
    return out


__all__ = ["quarantine_enabled", "quarantine_dir", "quarantine_op", "list_quarantined"]
