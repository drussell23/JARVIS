"""Phase 8.1 — Decision causal-trace ledger.

Per `OUROBOROS_VENOM_PRD.md` §3.6.4 + §9 Phase 8.1:

  > SerpentFlow + replay.html + 41 SSE events + 10+ JSONL ledgers
  > gives "what happened", not "why it happened in this specific
  > causal order". Phase 8 closes this gap.

This module ships the append-only decision-trace primitive:
``.jarvis/decision_trace.jsonl`` rows of the form
``{op_id, phase, decision, factors, weights, ts}``.

Every autonomic decision (route assignment, risk-tier promotion,
exploration verdict, plan approval, etc.) emits one row at the
moment of decision. State reconstruction is then a reduce over
the rows scoped to one op_id.

## Design constraints (load-bearing)

  * **Append-only**: never rewrite, never truncate. The ledger
    is the audit trail.
  * **Bounded**: per-row size cap + per-file size cap +
    per-call-rate cap. Defends against an op spamming the ledger.
  * **Stdlib + adaptation._file_lock import surface only.**
    Same cage discipline as the rest of `governance/observability/`.
    The ledger reuses Phase 7.8's flock for cross-process safety.
  * **Fail-open**: every error path is swallowed + logged once.
    Decision-tracing failure NEVER blocks the autonomic decision
    itself.
  * **Default-off**: ``JARVIS_DECISION_TRACE_LEDGER_ENABLED``
    (default false until graduation).

## Schema

```jsonl
{"schema_version": "1",
 "op_id": "op-abc",
 "phase": "ROUTE",
 "decision": "STANDARD",
 "factors": {"urgency": "normal", "task_complexity": "moderate",
             "source": "TestFailureSensor"},
 "weights": {"urgency": 1.0, "complexity": 0.5},
 "rationale": "Default cascade for normal urgency...",
 "ts_iso": "2026-04-26T...",
 "ts_epoch": 1714128000.0}
```

Each row is one TUPLE of (decision-point, factors-considered,
weights-applied, terminal-decision). Decision points are operator-
defined strings (not constrained by an enum at this layer — Phase 8
is read-side observability; it doesn't constrain the upstream
producers).
"""
from __future__ import annotations

import enum
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


_TRUTHY = ("1", "true", "yes", "on")


# Hard caps (bounded sizes — defends against runaway producers).
MAX_LEDGER_FILE_BYTES: int = 16 * 1024 * 1024
MAX_ROW_BYTES: int = 16 * 1024
MAX_RATIONALE_CHARS: int = 1_000
MAX_FACTORS_KEYS: int = 32
MAX_WEIGHTS_KEYS: int = 32
MAX_RECORDS_LOADED: int = 100_000

# Per-op call rate cap (defends against an op emitting hundreds of
# decisions in a tight loop).
MAX_RECORDS_PER_OP: int = 200


SCHEMA_VERSION: str = "2"
SCHEMA_VERSIONS_READABLE: Tuple[str, ...] = ("1", "2")


def is_ledger_enabled() -> bool:
    """Master flag — ``JARVIS_DECISION_TRACE_LEDGER_ENABLED``
    (default false)."""
    return os.environ.get(
        "JARVIS_DECISION_TRACE_LEDGER_ENABLED", "",
    ).strip().lower() in _TRUTHY


def ledger_path() -> Path:
    raw = os.environ.get("JARVIS_DECISION_TRACE_LEDGER_PATH")
    if raw:
        return Path(raw)
    return Path(".jarvis") / "decision_trace.jsonl"


# ---------------------------------------------------------------------------
# Row shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DecisionRow:
    """One causal-trace row. Frozen — append-only history.

    v2 fields (§24.5.1 Merkle DAG extension):
      * ``predecessor_ids`` — Merkle DAG edges to causally-prior rows.
      * ``payload_hash`` — sha256 of canonical JSON (content-addressing).
      * ``decision_tier`` — noise-budget tier (§24.5.2).
      * ``decision_hash_digest`` — from ``DecisionHash`` (Slice 1.2).
    """

    op_id: str
    phase: str
    decision: str
    factors: Dict[str, Any]
    weights: Dict[str, float]
    rationale: str
    ts_iso: str
    ts_epoch: float
    # v2 Merkle DAG fields (§24.5.1)
    predecessor_ids: Tuple[str, ...] = ()
    payload_hash: str = ""
    decision_tier: str = "NORMAL"  # CRITICAL | HIGH | NORMAL | LOW
    decision_hash_digest: str = ""  # from DecisionHash (Slice 1.2)

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "op_id": self.op_id,
            "phase": self.phase,
            "decision": self.decision,
            "factors": self.factors,
            "weights": self.weights,
            "rationale": self.rationale,
            "ts_iso": self.ts_iso,
            "ts_epoch": self.ts_epoch,
        }
        # v2 fields — always emitted for v2 rows.
        if self.predecessor_ids:
            d["predecessor_ids"] = list(self.predecessor_ids)
        if self.payload_hash:
            d["payload_hash"] = self.payload_hash
        if self.decision_tier and self.decision_tier != "NORMAL":
            d["decision_tier"] = self.decision_tier
        if self.decision_hash_digest:
            d["decision_hash_digest"] = self.decision_hash_digest
        return d


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _truncate_dict(
    d: Optional[Dict[str, Any]], max_keys: int,
) -> Dict[str, Any]:
    if not isinstance(d, dict):
        return {}
    if len(d) <= max_keys:
        return dict(d)
    # Truncate to first N keys (operator can adjust if N matters).
    keys = list(d.keys())[:max_keys]
    return {k: d[k] for k in keys}


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


@dataclass
class DecisionTraceLedger:
    """Append-only JSONL of autonomic decisions. Best-effort —
    NEVER raises into the caller."""

    path: Path = field(default_factory=ledger_path)
    _per_op_count: Dict[str, int] = field(default_factory=dict)

    def record(
        self,
        *,
        op_id: str,
        phase: str,
        decision: str,
        factors: Optional[Dict[str, Any]] = None,
        weights: Optional[Dict[str, float]] = None,
        rationale: str = "",
        predecessor_ids: Optional[Tuple[str, ...]] = None,
        decision_tier: str = "NORMAL",
        decision_hash_digest: str = "",
    ) -> Tuple[bool, str]:
        """Append one decision row. Returns ``(ok, detail)``.

        Pre-checks:
          1. Master flag off → (False, "master_off")
          2. op_id empty → (False, "empty_op_id")
          3. phase empty → (False, "empty_phase")
          4. decision empty → (False, "empty_decision")
          5. per-op rate cap hit → (False, "rate_cap_exhausted")

        NEVER raises.
        """
        if not is_ledger_enabled():
            return (False, "master_off")
        op = (op_id or "").strip()
        if not op:
            return (False, "empty_op_id")
        ph = (phase or "").strip()
        if not ph:
            return (False, "empty_phase")
        dec = (decision or "").strip()
        if not dec:
            return (False, "empty_decision")
        # Per-op rate cap.
        current_count = self._per_op_count.get(op, 0)
        if current_count >= MAX_RECORDS_PER_OP:
            return (False, "rate_cap_exhausted")
        row = DecisionRow(
            op_id=op,
            phase=ph,
            decision=dec,
            factors=_truncate_dict(factors, MAX_FACTORS_KEYS),
            weights=_truncate_dict(weights, MAX_WEIGHTS_KEYS),
            rationale=(rationale or "")[:MAX_RATIONALE_CHARS],
            ts_iso=_utc_now_iso(),
            ts_epoch=time.time(),
            predecessor_ids=tuple(
                str(p) for p in (predecessor_ids or ())
            ),
            decision_tier=(
                (decision_tier or "NORMAL").strip().upper()
            ),
            decision_hash_digest=(
                (decision_hash_digest or "").strip()
            ),
        )
        # Compute payload_hash as content-addressed hash of the
        # row's canonical JSON (§24.5.1). Uses the determinism
        # substrate's canonical serializer for architecture stability.
        row_dict_for_hash = row.to_dict()
        # Remove payload_hash from input (chicken-and-egg).
        row_dict_for_hash.pop("payload_hash", None)
        try:
            from backend.core.ouroboros.governance.observability.determinism_substrate import (  # noqa: E501
                canonical_hash,
            )
            computed_hash = canonical_hash(row_dict_for_hash)
        except Exception:  # noqa: BLE001 — defensive
            computed_hash = ""
        # Replace the row with payload_hash populated.
        row = DecisionRow(
            op_id=row.op_id,
            phase=row.phase,
            decision=row.decision,
            factors=row.factors,
            weights=row.weights,
            rationale=row.rationale,
            ts_iso=row.ts_iso,
            ts_epoch=row.ts_epoch,
            predecessor_ids=row.predecessor_ids,
            payload_hash=computed_hash,
            decision_tier=row.decision_tier,
            decision_hash_digest=row.decision_hash_digest,
        )
        try:
            line = json.dumps(row.to_dict(), separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            return (False, f"serialize_failed:{exc}")
        if len(line.encode("utf-8")) > MAX_ROW_BYTES:
            return (False, f"row_oversize:{len(line)}>max={MAX_ROW_BYTES}")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return (False, f"mkdir_failed:{exc}")
        try:
            with self.path.open("a", encoding="utf-8") as f:
                from backend.core.ouroboros.governance.adaptation._file_lock import (  # noqa: E501
                    flock_exclusive,
                )
                with flock_exclusive(f.fileno()):
                    f.write(line)
                    f.write("\n")
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except OSError:
                        pass
        except OSError as exc:
            return (False, f"append_failed:{exc}")
        self._per_op_count[op] = current_count + 1
        return (True, "ok")

    def reconstruct_op(self, op_id: str) -> List[DecisionRow]:
        """Return all decision rows for one op, in chronological
        (file) order. Used for state-reconstruction queries."""
        if not is_ledger_enabled():
            return []
        if not self.path.exists():
            return []
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size > MAX_LEDGER_FILE_BYTES:
            logger.warning(
                "[DecisionTraceLedger] %s exceeds MAX_LEDGER_FILE_BYTES=%d "
                "(was %d) — refusing to load",
                self.path, MAX_LEDGER_FILE_BYTES, size,
            )
            return []
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return []
        out: List[DecisionRow] = []
        for line in text.splitlines():
            if len(out) >= MAX_RECORDS_LOADED:
                break
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            if str(obj.get("op_id") or "") != op_id:
                continue
            # Parse v2 fields with v1 fallback.
            raw_pred = obj.get("predecessor_ids")
            pred_ids: Tuple[str, ...] = ()
            if isinstance(raw_pred, list):
                pred_ids = tuple(str(p) for p in raw_pred)
            out.append(DecisionRow(
                op_id=str(obj.get("op_id") or ""),
                phase=str(obj.get("phase") or ""),
                decision=str(obj.get("decision") or ""),
                factors=obj.get("factors") if isinstance(obj.get("factors"), dict) else {},
                weights=obj.get("weights") if isinstance(obj.get("weights"), dict) else {},
                rationale=str(obj.get("rationale") or ""),
                ts_iso=str(obj.get("ts_iso") or ""),
                ts_epoch=float(obj.get("ts_epoch") or 0.0),
                predecessor_ids=pred_ids,
                payload_hash=str(obj.get("payload_hash") or ""),
                decision_tier=str(obj.get("decision_tier") or "NORMAL"),
                decision_hash_digest=str(obj.get("decision_hash_digest") or ""),
            ))
        return out


_DEFAULT_LEDGER: Optional[DecisionTraceLedger] = None


def get_default_ledger() -> DecisionTraceLedger:
    global _DEFAULT_LEDGER
    if _DEFAULT_LEDGER is None:
        _DEFAULT_LEDGER = DecisionTraceLedger()
    return _DEFAULT_LEDGER


def reset_default_ledger() -> None:
    global _DEFAULT_LEDGER
    _DEFAULT_LEDGER = None


__all__ = [
    "DecisionRow",
    "DecisionTraceLedger",
    "MAX_FACTORS_KEYS",
    "MAX_LEDGER_FILE_BYTES",
    "MAX_RATIONALE_CHARS",
    "MAX_RECORDS_LOADED",
    "MAX_RECORDS_PER_OP",
    "MAX_ROW_BYTES",
    "MAX_WEIGHTS_KEYS",
    "SCHEMA_VERSION",
    "get_default_ledger",
    "is_ledger_enabled",
    "ledger_path",
    "reset_default_ledger",
]
