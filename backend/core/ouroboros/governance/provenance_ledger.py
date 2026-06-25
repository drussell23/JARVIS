"""Unified Provenance Ledger -- tamper-evident origin graph per IntentEnvelope.
================================================================================

Every :class:`IntentEnvelope` that arrives at the intake router is stamped with
a hash-chained :class:`ProvenanceRecord` binding *op_id -> origin -> ingest_ts*.
The records are cryptographically chained (``record_hash = sha256(prev_hash +
canonical(payload))``) so a forged, dropped, or reordered record breaks the
chain, which :meth:`ProvenanceLedger.verify_chain` detects.

Why this exists (Run #17)
-------------------------
The A1 GraduationAuditor's ``A1_DISPATCH_PROVEN`` check expected the 5-hop
sequence ``emit -> ingest -> dequeue -> submit -> accept``. But the ``emit`` hop
is emitted ONLY by ``roadmap_orchestrator`` (source="roadmap"); a sensor op
(TestFailure / OpportunityMiner / ...) ingests WITHOUT an emit BY DESIGN. The
auditor wrongly failed an emit-less sensor op ``missing_or_out_of_order:emit``.

The fix is provenance-awareness: this ledger records each envelope's ORIGIN
(its :class:`SignalSource`) at ingestion, classified into an
:class:`OriginClass`. The auditor then traverses the provenance to validate the
pipeline THAT origin actually produces -- no hardcoded op-id -> pipeline map.

Design constraints
------------------
- **Reuse, no new crypto**: the hash-chain idiom is the SAME one used by
  ``command_node/biometric_audit_ledger`` (sha256 of prev-link + canonical
  payload, GENESIS sentinel). Imported here.
- **Origin from the enum, not op-ids**: :func:`classify_origin` maps a source
  token to an :class:`OriginClass` via :class:`SignalSource` enum membership +
  the ROADMAP-emits-only invariant -- NEVER a hardcoded if/elif of op-ids.
- **Fail-soft**: a ledger error NEVER blocks ingestion. Every public entry is
  wrapped so a stamp failure logs loudly but returns gracefully.
- **Gated**: ``JARVIS_PROVENANCE_LEDGER_ENABLED`` (default ``"true"``). OFF is
  byte-identical (no stamp, no log, no record).
- **Append-only + bounded**: an in-memory ring (newest-wins) caps growth on
  long soaks; the chain head is preserved so verification stays valid.
"""
from __future__ import annotations

import collections
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Deque, Dict, List, Optional

# Reuse the EXISTING hash-chain idiom (no new crypto scheme): the GENESIS
# sentinel + the ``sha256(prev_hash + canonical(payload))`` chain link shape
# from command_node/biometric_audit_ledger. We canonicalize OUR payload fields
# the same way (compact json.dumps, sort_keys) -- the construction is identical,
# only the field set differs (provenance fields vs biometric fields).
from backend.core.ouroboros.governance.command_node.biometric_audit_ledger import (
    GENESIS_HASH,
)
from backend.core.ouroboros.governance.intent.signals import SignalSource

logger = logging.getLogger(__name__)

_ENV_ENABLED = "JARVIS_PROVENANCE_LEDGER_ENABLED"

# Bounded ring: cap records held in memory on a long soak. Newest-wins FIFO
# eviction. The CHAIN HEAD (last record_hash) is tracked separately so the
# chain continues correctly even after the oldest records are evicted.
_LEDGER_MAX_DEFAULT = 4096


def ledger_enabled() -> bool:
    """Return True unless ``JARVIS_PROVENANCE_LEDGER_ENABLED`` is explicitly
    falsy. Default ON; OFF is byte-identical (no stamp emitted)."""
    val = (os.environ.get(_ENV_ENABLED, "true") or "").strip().lower()
    return val not in {"0", "false", "no", "off"}


# ===========================================================================
# OriginClass -- the pipeline a source's ops actually produce
# ===========================================================================


class OriginClass(str, Enum):
    """The pipeline-shape class an envelope's origin produces.

    * ``ROADMAP``  -- RoadmapOrchestrator ops EMIT (the ``emit`` hop fires);
      their pipeline is ``emit -> ingest -> dequeue -> submit -> accept``.
    * ``SENSOR``   -- every autonomous sensor (TestFailure / OpportunityMiner /
      ...) ingests an envelope WITHOUT a prior emit; their pipeline is
      ``ingest -> dequeue -> submit -> accept`` (NO emit -- this is VALID).
    * ``UNKNOWN``  -- a source the enum does not recognize. The auditor grades
      these UNVERIFIABLE (honest non-pass, never a fake-pass / bypass).
    """

    ROADMAP = "roadmap"
    SENSOR = "sensor"
    UNKNOWN = "unknown"


def classify_origin(source: Any) -> OriginClass:
    """Map an envelope ``source`` (string or :class:`SignalSource`) to an
    :class:`OriginClass`.

    Derivation is from the :class:`SignalSource` enum + the ROADMAP-emits-only
    invariant -- NOT a hardcoded if/elif of specific op-ids:

      * The source token is resolved against ``SignalSource`` membership. An
        unrecognized token -> ``OriginClass.UNKNOWN``.
      * ``SignalSource.ROADMAP`` is the ONLY origin whose pipeline begins with
        an ``emit`` hop (RoadmapOrchestrator emits the strategic GOAL). It maps
        to ``OriginClass.ROADMAP``.
      * Every OTHER recognized source is an autonomous sensor that ingests
        without a prior emit -> ``OriginClass.SENSOR``.

    Pure; NEVER raises."""
    try:
        if isinstance(source, SignalSource):
            member: Optional[SignalSource] = source
        else:
            token = str(source).strip()
            if not token:
                return OriginClass.UNKNOWN
            try:
                member = SignalSource(token)
            except ValueError:
                return OriginClass.UNKNOWN
        # The ROADMAP origin is the sole emit-producing source. Every other
        # recognized SignalSource is a sensor (ingest without emit).
        if member is SignalSource.ROADMAP:
            return OriginClass.ROADMAP
        return OriginClass.SENSOR
    except Exception:  # noqa: BLE001 -- classification must never raise
        return OriginClass.UNKNOWN


# ===========================================================================
# ProvenanceRecord -- one tamper-evident chain link per envelope
# ===========================================================================

# The ordered, canonical payload fields hashed into the chain. Order is
# load-bearing (it is part of the canonicalization). ``prev_hash`` and
# ``record_hash`` are chain metadata, NOT part of the hashed payload -- the
# SAME discipline as biometric_audit_ledger._PAYLOAD_FIELDS.
_PAYLOAD_FIELDS = (
    "op_id",
    "origin",
    "origin_class",
    "ingested_ts",
)


@dataclass(frozen=True)
class ProvenanceRecord:
    """One immutable, hash-chained provenance link for an envelope.

    ``record_hash = sha256(prev_hash + canonical({op_id, origin, origin_class,
    ingested_ts}))`` -- so a forged / dropped / reordered record breaks the
    chain (detected by :meth:`ProvenanceLedger.verify_chain`)."""

    op_id: str
    origin: str              # the raw source token (e.g. "test_failure")
    origin_class: str        # OriginClass value (e.g. "sensor")
    ingested_ts: float       # wall-clock ingest time
    prev_hash: str
    record_hash: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "op_id": self.op_id,
            "origin": self.origin,
            "origin_class": self.origin_class,
            "ingested_ts": self.ingested_ts,
            "prev_hash": self.prev_hash,
            "record_hash": self.record_hash,
        }


def _canonical_payload(record: Dict[str, Any]) -> str:
    """Deterministic canonical serialization of the hashed payload -- mirrors
    biometric_audit_ledger._canonical_payload exactly (project onto the ordered
    fields, compact separators, sort_keys) so the hash is stable + independent
    of dict insertion order. ``prev_hash`` / ``record_hash`` are NOT hashed."""
    ordered = {k: record.get(k) for k in _PAYLOAD_FIELDS}
    return json.dumps(ordered, separators=(",", ":"), sort_keys=True)


def compute_provenance_hash(prev_hash: str, payload: Dict[str, Any]) -> str:
    """``sha256(prev_hash + canonical(payload))`` -- the chain link. Same
    construction as biometric_audit_ledger.compute_record_hash, applied to the
    provenance payload fields."""
    h = hashlib.sha256()
    h.update((prev_hash or GENESIS_HASH).encode("utf-8"))
    h.update(_canonical_payload(payload).encode("utf-8"))
    return h.hexdigest()


# ===========================================================================
# ProvenanceLedger -- append-only, bounded, hash-chained
# ===========================================================================


class ProvenanceLedger:
    """In-memory, append-only, hash-chained provenance ledger.

    Bounded: holds at most ``max_records`` records (newest-wins FIFO). The
    chain head (last ``record_hash``) is tracked separately, so appends
    continue the chain even after the oldest records are evicted, and
    :meth:`verify_chain` validates the retained window from its first record.

    Fail-soft: :meth:`append` NEVER raises -- a hash/append error logs loudly
    and returns the computed record (or None) without blocking the caller."""

    def __init__(self, *, max_records: Optional[int] = None) -> None:
        if max_records is None:
            raw = (os.environ.get("JARVIS_PROVENANCE_LEDGER_MAX", "") or "").strip()
            try:
                max_records = int(raw) if raw else _LEDGER_MAX_DEFAULT
            except ValueError:
                max_records = _LEDGER_MAX_DEFAULT
        self.max_records = max(1, int(max_records))
        self._records: Deque[ProvenanceRecord] = collections.deque(
            maxlen=self.max_records
        )
        # The hash of the most-recent appended record -- the chain head. Stays
        # valid across eviction (we never re-genesis on eviction).
        self._head_hash: str = GENESIS_HASH

    @property
    def head_hash(self) -> str:
        return self._head_hash

    def append(
        self,
        *,
        op_id: str,
        origin: Any,
        ingested_ts: Optional[float] = None,
    ) -> Optional[ProvenanceRecord]:
        """Append a hash-chained provenance record for an envelope. Returns the
        record (with its hash) or None on failure. NEVER raises."""
        try:
            origin_token = (
                origin.value if isinstance(origin, SignalSource) else str(origin)
            )
            origin_class = classify_origin(origin).value
            ts = float(ingested_ts) if ingested_ts is not None else time.time()
            payload = {
                "op_id": str(op_id),
                "origin": origin_token,
                "origin_class": origin_class,
                "ingested_ts": ts,
            }
            prev = self._head_hash
            record_hash = compute_provenance_hash(prev, payload)
            record = ProvenanceRecord(
                op_id=str(op_id),
                origin=origin_token,
                origin_class=origin_class,
                ingested_ts=ts,
                prev_hash=prev,
                record_hash=record_hash,
            )
            self._records.append(record)
            self._head_hash = record_hash
            return record
        except Exception:  # noqa: BLE001 -- a ledger error NEVER blocks ingest
            logger.warning(
                "[Provenance] append failed for op=%s -- ingestion continues",
                op_id,
                exc_info=True,
            )
            return None

    def verify_chain(self) -> bool:
        """Recompute the retained window's chain; True iff every link is intact
        (tamper-evident). Empty ledger -> True (vacuously valid). Any hash
        mismatch / broken prev-link -> False. NEVER raises."""
        try:
            records = list(self._records)
            if not records:
                return True
            prev = records[0].prev_hash
            for rec in records:
                if rec.prev_hash != prev:
                    return False
                expected = compute_provenance_hash(prev, rec.to_dict())
                if rec.record_hash != expected:
                    return False
                prev = rec.record_hash
            return True
        except Exception:  # noqa: BLE001 -- any error -> NOT verified
            logger.warning("[Provenance] verify_chain failed", exc_info=True)
            return False

    def records(self) -> List[ProvenanceRecord]:
        """Read-only snapshot of the retained records (oldest-first)."""
        return list(self._records)

    def latest_for_op(self, op_id: str) -> Optional[ProvenanceRecord]:
        """Most-recent retained record for ``op_id`` (or None)."""
        for rec in reversed(self._records):
            if rec.op_id == op_id:
                return rec
        return None

    def clear(self) -> None:
        """Reset the ledger to genesis (test hook + boot reset)."""
        self._records.clear()
        self._head_hash = GENESIS_HASH


# ===========================================================================
# Process-default ledger + the ingestion stamp entry point
# ===========================================================================

_DEFAULT_LEDGER: Optional[ProvenanceLedger] = None


def get_default_ledger() -> ProvenanceLedger:
    global _DEFAULT_LEDGER
    if _DEFAULT_LEDGER is None:
        _DEFAULT_LEDGER = ProvenanceLedger()
    return _DEFAULT_LEDGER


def reset_default_ledger() -> None:
    """Test hook + boot reset for the process-default ledger."""
    global _DEFAULT_LEDGER
    _DEFAULT_LEDGER = None


def stamp_provenance(
    op_id: str,
    origin: Any,
    *,
    ledger: Optional[ProvenanceLedger] = None,
    ingested_ts: Optional[float] = None,
) -> Optional[ProvenanceRecord]:
    """Stamp a hash-chained provenance record at INGESTION and emit a
    structured ``[Provenance] op=.. origin=.. origin_class=.. chain_ok=..``
    line at WARNING level (so it survives ``silent_boot``).

    Gated by ``JARVIS_PROVENANCE_LEDGER_ENABLED`` (default ON): when disabled
    this is a silent no-op (byte-identical to no instrumentation).

    Fail-soft: a ledger error NEVER blocks ingestion -- returns None and logs.
    NEVER raises into the caller (the intake hot path)."""
    if not ledger_enabled():
        return None
    try:
        led = ledger if ledger is not None else get_default_ledger()
        record = led.append(op_id=op_id, origin=origin, ingested_ts=ingested_ts)
        if record is None:
            # append already logged loudly; emit the structured line anyway so
            # the auditor sees a (failed) stamp attempt.
            logger.warning(
                "[Provenance] op=%s origin=%s origin_class=%s chain_ok=False",
                op_id,
                origin.value if isinstance(origin, SignalSource) else origin,
                classify_origin(origin).value,
            )
            return None
        chain_ok = led.verify_chain()
        logger.warning(
            "[Provenance] op=%s origin=%s origin_class=%s chain_ok=%s",
            record.op_id,
            record.origin,
            record.origin_class,
            chain_ok,
        )
        return record
    except Exception:  # noqa: BLE001 -- provenance NEVER blocks ingestion
        logger.warning(
            "[Provenance] stamp failed for op=%s -- ingestion continues",
            op_id,
            exc_info=True,
        )
        return None


__all__ = [
    "GENESIS_HASH",
    "OriginClass",
    "ProvenanceLedger",
    "ProvenanceRecord",
    "classify_origin",
    "compute_provenance_hash",
    "get_default_ledger",
    "ledger_enabled",
    "reset_default_ledger",
    "stamp_provenance",
]
