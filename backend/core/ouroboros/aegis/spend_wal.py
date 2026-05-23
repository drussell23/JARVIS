"""Spend WAL — Aegis-owned append-only audit ledger.

Composes ``governance/cross_process_jsonl.flock_append_line`` verbatim
(per binding correction "leverage the existing cross_process_jsonl for
atomic WAL appending to ensure the spend ledger is mathematically
sealed"). No new locking primitive. No new file-handle discipline.

Posture:

  * **Aegis-owned.** WAL path lives under ``.jarvis/aegis/`` which
    Slice 2 will add to the JARVIS-side ``FORBIDDEN_PATH`` list.
    From JARVIS's perspective, the WAL is unreachable.
  * **Authoritative.** Every lease that admits and every reconciliation
    that updates the in-memory state machine writes here. Crash
    recovery replays the WAL.
  * **Append-only.** No update, no delete, no rotation in Slice 1.
    Operator-paced rotation is a future arc (composes Anti-Venom
    corpus-rotation patterns).
  * **NEVER raises.** flock_append_line is contract-bound to return
    False on any failure; we surface that to the caller as a bool.

Schema v1 (one JSON object per line):

    {
      "kind": "admit" | "reconcile" | "boot",
      "schema_version": "aegis_spend_wal.1",
      "ts": <unix epoch float>,
      "lease_nonce": "<str>" | null,
      "op_id": "<str>" | null,
      "route": "<str>" | null,
      "estimated_cost_usd": <float> | null,
      "actual_cost_usd": <float> | null,
      "reserve_cost_usd": <float> | null,
      "detail": "<str>" | null
    }

"boot" rows mark the daemon's lifecycle (boot + clean-shutdown). They
let crash recovery distinguish "WAL is from previous session" vs.
"WAL is mid-session" without parsing every preceding row.
"""
from __future__ import annotations

import asyncio
import enum
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.core.ouroboros.governance.cross_process_jsonl import flock_append_line

logger = logging.getLogger(__name__)


SPEND_WAL_SCHEMA_VERSION: str = "aegis_spend_wal.1"


class SpendEntryKind(str, enum.Enum):
    """Closed 3-value taxonomy of WAL row kinds in Slice 1."""

    BOOT = "boot"
    ADMIT = "admit"
    RECONCILE = "reconcile"


@dataclass(frozen=True)
class SpendEntry:
    """Single WAL row. Frozen + §33.5 to_dict/from_dict.

    Optional fields are ``None`` when not applicable to the entry's
    kind (e.g., BOOT has no lease_nonce). Use the convenience
    constructors below.
    """

    kind: SpendEntryKind
    ts: float
    lease_nonce: Optional[str] = None
    op_id: Optional[str] = None
    route: Optional[str] = None
    estimated_cost_usd: Optional[float] = None
    actual_cost_usd: Optional[float] = None
    reserve_cost_usd: Optional[float] = None
    detail: Optional[str] = None
    schema_version: str = SPEND_WAL_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind.value,
            "schema_version": self.schema_version,
            "ts": self.ts,
            "lease_nonce": self.lease_nonce,
            "op_id": self.op_id,
            "route": self.route,
            "estimated_cost_usd": self.estimated_cost_usd,
            "actual_cost_usd": self.actual_cost_usd,
            "reserve_cost_usd": self.reserve_cost_usd,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SpendEntry":
        kind_raw = str(d["kind"])
        try:
            kind = SpendEntryKind(kind_raw)
        except ValueError as exc:
            raise ValueError(f"unknown spend entry kind: {kind_raw!r}") from exc
        return cls(
            kind=kind,
            ts=float(d["ts"]),
            lease_nonce=_optional_str(d.get("lease_nonce")),
            op_id=_optional_str(d.get("op_id")),
            route=_optional_str(d.get("route")),
            estimated_cost_usd=_optional_float(d.get("estimated_cost_usd")),
            actual_cost_usd=_optional_float(d.get("actual_cost_usd")),
            reserve_cost_usd=_optional_float(d.get("reserve_cost_usd")),
            detail=_optional_str(d.get("detail")),
            schema_version=str(d.get("schema_version", SPEND_WAL_SCHEMA_VERSION)),
        )


def _optional_str(v: Any) -> Optional[str]:
    return None if v is None else str(v)


def _optional_float(v: Any) -> Optional[float]:
    return None if v is None else float(v)


# ---------------------------------------------------------------------------
# Convenience constructors
# ---------------------------------------------------------------------------


def boot_entry(*, ts: float, detail: str) -> SpendEntry:
    return SpendEntry(kind=SpendEntryKind.BOOT, ts=ts, detail=detail)


def admit_entry(
    *,
    ts: float,
    lease_nonce: str,
    op_id: str,
    route: str,
    estimated_cost_usd: float,
    reserve_cost_usd: float,
) -> SpendEntry:
    return SpendEntry(
        kind=SpendEntryKind.ADMIT,
        ts=ts,
        lease_nonce=lease_nonce,
        op_id=op_id,
        route=route,
        estimated_cost_usd=estimated_cost_usd,
        reserve_cost_usd=reserve_cost_usd,
    )


def reconcile_entry(
    *,
    ts: float,
    lease_nonce: str,
    op_id: str,
    route: str,
    actual_cost_usd: float,
    reserve_cost_usd: float,
) -> SpendEntry:
    return SpendEntry(
        kind=SpendEntryKind.RECONCILE,
        ts=ts,
        lease_nonce=lease_nonce,
        op_id=op_id,
        route=route,
        actual_cost_usd=actual_cost_usd,
        reserve_cost_usd=reserve_cost_usd,
    )


# ---------------------------------------------------------------------------
# Append + replay
# ---------------------------------------------------------------------------


def append_entry_sync(wal_path: Path, entry: SpendEntry) -> bool:
    """Append ``entry`` to ``wal_path`` under flock. Returns True on
    success, False on any failure (lock timeout, write error, etc.).
    NEVER raises."""
    line = json.dumps(entry.to_dict(), separators=(",", ":"), sort_keys=True)
    return flock_append_line(Path(wal_path), line)


async def append_entry(wal_path: Path, entry: SpendEntry) -> bool:
    """Async wrapper. flock_append_line is sync-blocking but its
    critical section is microsecond-scale; we route it through a
    worker thread anyway so a busy filesystem cannot stall the event
    loop. Returns True on success, False on any failure."""
    return await asyncio.to_thread(append_entry_sync, wal_path, entry)


def replay_wal(wal_path: Path) -> List[SpendEntry]:
    """Read ``wal_path`` line-by-line and reconstruct entries.

    Malformed lines (JSON parse error, missing required field, unknown
    kind) are SKIPPED with a DEBUG log. WAL recovery is best-effort —
    a corrupt tail (e.g., from SIGKILL mid-write) must not strand the
    daemon at boot. Caller (BudgetStateMachine) tolerates partial
    state by treating unknown spend as un-debited (fail-safe-by-loss).

    Returns entries in WAL order. Empty WAL or missing file returns
    empty list (not an error — first-ever boot)."""
    target = Path(wal_path)
    if not target.exists():
        return []

    entries: List[SpendEntry] = []
    try:
        with target.open("r", encoding="utf-8") as fh:
            for lineno, raw in enumerate(fh, start=1):
                raw = raw.rstrip("\n")
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError as exc:
                    logger.debug(
                        "[AegisSpendWAL] skip malformed line %d: %s",
                        lineno, exc,
                    )
                    continue
                if not isinstance(obj, dict):
                    logger.debug(
                        "[AegisSpendWAL] skip non-object line %d", lineno,
                    )
                    continue
                try:
                    entries.append(SpendEntry.from_dict(obj))
                except (KeyError, ValueError, TypeError) as exc:
                    logger.debug(
                        "[AegisSpendWAL] skip unparseable entry line %d: %s",
                        lineno, exc,
                    )
                    continue
    except OSError as exc:
        logger.debug("[AegisSpendWAL] read failed at %s: %s", target, exc)
        return entries

    return entries


__all__ = [
    "SPEND_WAL_SCHEMA_VERSION",
    "SpendEntry",
    "SpendEntryKind",
    "admit_entry",
    "append_entry",
    "append_entry_sync",
    "boot_entry",
    "reconcile_entry",
    "replay_wal",
]
