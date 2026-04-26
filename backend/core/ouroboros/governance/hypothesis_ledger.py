"""P1.5 Slice 1 — HypothesisLedger primitive.

Per OUROBOROS_VENOM_PRD.md §9 Phase 2 P1.5 ("Hypothesis ledger"):

  > Self-formed goals need to be testable. Without an explicit
  > hypothesis structure, the system can't measure whether its
  > self-direction is yielding insight.
  > Solution: every self-formed goal is paired with a hypothesis
  > ("I think X causes Y; if I do Z, I expect W"). After the goal
  > completes, automated check: did W happen?

This slice ships the primitive (data model + JSONL persistence) plus
the operator-visible ``/hypothesis ledger`` REPL surface. Slice 2
will wire the SelfGoalFormationEngine to emit a paired Hypothesis on
every ProposalDraft and add the auto-validator that fills
``actual_outcome`` + ``validated`` after the op completes.

Storage model: append-only JSONL with **last-write-wins per hypothesis_id**.
Every state change (create / record_outcome / mark_validated) writes a
fresh row; ``load_all`` returns one record per hypothesis_id (the latest).
That keeps the file ledger frozen-dataclass-friendly while preserving
audit history (operators can `tail -f` the file to watch state evolve).

Authority invariants (PRD §12.2):
  * **No authority imports** — orchestrator / policy / iron_gate /
    risk_tier / change_engine / candidate_generator / gate /
    semantic_guardian. Pinned by
    ``test_hypothesis_ledger_no_authority_imports``.
  * **No FSM mutation** — read-only data primitive + append-only file
    write. Never invokes a model, never enqueues an op.
  * **Best-effort** — malformed lines / missing files / write failures
    return empty / False, never raise.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Schema version frozen for the JSONL on-disk format. Future bumps need
# additive migration semantics + this constant + the source-grep pin
# updated together.
HYPOTHESIS_SCHEMA_VERSION: str = "hypothesis_ledger.1"

# Default ledger filename — sits alongside the SelfGoalFormationEngine
# proposals ledger so operators can audit them together.
DEFAULT_LEDGER_FILENAME: str = "hypothesis_ledger.jsonl"


@dataclass(frozen=True)
class Hypothesis:
    """One falsifiable claim paired with a self-formed goal.

    Lifecycle:
      1. Created with ``actual_outcome=None``, ``validated=None``
         (open hypothesis — engine just proposed it).
      2. After op completes, the validator records the actual outcome
         via ``HypothesisLedger.record_outcome``. ``validated`` becomes
         True / False / None depending on the validator's check.

    Attributes
    ----------
    hypothesis_id:
        Stable sha256[:12] of ``(op_id + claim + created_unix)``. Used
        as the dedup + lookup key.
    op_id:
        The op_id this hypothesis is paired with — comes from the
        SelfGoalFormationEngine proposal.
    claim:
        The model's hypothesis statement: "I think X causes Y".
        Free-form text, capped at 500 chars on disk.
    expected_outcome:
        The falsifiable predicate: "if I do Z, I expect W". The
        validator compares this against actual outcome. Capped at
        300 chars on disk.
    actual_outcome:
        Filled after the op completes. ``None`` until then.
    validated:
        True (predicate held), False (it didn't), or None (not yet
        evaluated OR validator couldn't decide). The summary.json
        counter only counts unambiguous True/False.
    proposed_signature_hash:
        Optional link to the ProposalDraft that triggered this
        hypothesis. Lets operators correlate ledgers.
    created_unix:
        Wall-clock timestamp at creation.
    validated_unix:
        Wall-clock timestamp when the validator reached a decision.
        ``None`` while still open.
    schema_version:
        Frozen at module-level constant. Pinned by tests.
    """

    hypothesis_id: str
    op_id: str
    claim: str
    expected_outcome: str
    actual_outcome: Optional[str] = None
    validated: Optional[bool] = None
    proposed_signature_hash: Optional[str] = None
    created_unix: float = 0.0
    validated_unix: Optional[float] = None
    schema_version: str = HYPOTHESIS_SCHEMA_VERSION

    def is_open(self) -> bool:
        """True when validation hasn't completed (actual_outcome is None)."""
        return self.actual_outcome is None

    def is_validated(self) -> bool:
        """True only when validator confirmed the predicate held."""
        return self.validated is True

    def is_invalidated(self) -> bool:
        """True only when validator confirmed the predicate did NOT hold."""
        return self.validated is False

    def to_ledger_dict(self) -> Dict[str, Any]:
        return asdict(self)


def make_hypothesis_id(
    op_id: str, claim: str, created_unix: float,
) -> str:
    """Deterministic sha256[:12] of (op_id + claim + ts).

    Same input → same id. Used to dedup append-only ledger rows back
    into one logical hypothesis."""
    raw = f"{op_id}|{claim}|{created_unix:.6f}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


class HypothesisLedger:
    """Append-only JSONL store of ``Hypothesis`` rows with last-write-wins
    semantics per ``hypothesis_id``.

    Parameters
    ----------
    project_root:
        Repository root. Default ledger sits at
        ``project_root/.jarvis/hypothesis_ledger.jsonl``.
    ledger_path:
        Optional explicit path override.
    """

    def __init__(
        self,
        project_root: Path,
        ledger_path: Optional[Path] = None,
    ) -> None:
        self._root = Path(project_root).resolve()
        self._ledger_path = (
            Path(ledger_path).resolve()
            if ledger_path is not None
            else self._root / ".jarvis" / DEFAULT_LEDGER_FILENAME
        )

    @property
    def ledger_path(self) -> Path:
        return self._ledger_path

    # ---- public API ----

    def append(self, hypothesis: Hypothesis) -> bool:
        """Append one Hypothesis row to the JSONL ledger. Best-effort.

        Returns True on successful write, False otherwise. Caller is
        expected to mint the ``hypothesis_id`` via ``make_hypothesis_id``;
        any rows sharing an id collapse on read via last-write-wins."""
        try:
            self._ledger_path.parent.mkdir(parents=True, exist_ok=True)
            with self._ledger_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(hypothesis.to_ledger_dict()) + "\n")
            return True
        except OSError:
            logger.debug(
                "[HypothesisLedger] append failed: %s", self._ledger_path,
                exc_info=True,
            )
            return False

    def load_all(self) -> List[Hypothesis]:
        """Return one Hypothesis per hypothesis_id (the latest row per id).

        Order is insertion order of the FIRST row per id (so newly-created
        hypotheses always appear in chronological order even if they
        receive later state-update rows). Tolerates malformed lines."""
        if not self._ledger_path.exists():
            return []
        try:
            text = self._ledger_path.read_text(encoding="utf-8")
        except OSError:
            return []

        first_seen_order: Dict[str, int] = {}
        latest_by_id: Dict[str, Hypothesis] = {}
        for idx, line in enumerate(text.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(d, dict):
                continue
            hid = str(d.get("hypothesis_id", "")).strip()
            if not hid:
                continue
            if hid not in first_seen_order:
                first_seen_order[hid] = idx
            latest_by_id[hid] = self._row_to_hypothesis(d)

        # Sort by first-seen order (chronological).
        return [
            latest_by_id[hid]
            for hid in sorted(first_seen_order, key=first_seen_order.__getitem__)
        ]

    def find_by_id(self, hypothesis_id: str) -> Optional[Hypothesis]:
        """Return the latest record for an id, or None if absent."""
        target = hypothesis_id.strip().lower()
        for h in self.load_all():
            if h.hypothesis_id.lower() == target:
                return h
        return None

    def find_open(self) -> List[Hypothesis]:
        """All hypotheses still awaiting validation (actual_outcome=None)."""
        return [h for h in self.load_all() if h.is_open()]

    def find_validated(self) -> List[Hypothesis]:
        return [h for h in self.load_all() if h.is_validated()]

    def find_invalidated(self) -> List[Hypothesis]:
        return [h for h in self.load_all() if h.is_invalidated()]

    def record_outcome(
        self,
        hypothesis_id: str,
        actual_outcome: str,
        validated: Optional[bool],
    ) -> bool:
        """Append a state-update row that records the actual outcome +
        validator's decision. Returns True on success.

        Last-write-wins semantics on read mean this row overrides earlier
        rows for the same id."""
        existing = self.find_by_id(hypothesis_id)
        if existing is None:
            return False
        updated = Hypothesis(
            hypothesis_id=existing.hypothesis_id,
            op_id=existing.op_id,
            claim=existing.claim,
            expected_outcome=existing.expected_outcome,
            actual_outcome=str(actual_outcome)[:500],
            validated=validated,
            proposed_signature_hash=existing.proposed_signature_hash,
            created_unix=existing.created_unix,
            validated_unix=time.time(),
            schema_version=existing.schema_version,
        )
        return self.append(updated)

    def stats(self) -> Dict[str, int]:
        """Return validated/invalidated/open counts for summary.json wiring
        in Slice 2."""
        all_h = self.load_all()
        return {
            "total": len(all_h),
            "open": sum(1 for h in all_h if h.is_open()),
            "validated": sum(1 for h in all_h if h.is_validated()),
            "invalidated": sum(1 for h in all_h if h.is_invalidated()),
        }

    # ---- internals ----

    @staticmethod
    def _row_to_hypothesis(d: Dict[str, Any]) -> Hypothesis:
        validated = d.get("validated")
        # JSON null → None; True/False stay; anything else → None
        if validated not in (True, False, None):
            validated = None
        return Hypothesis(
            hypothesis_id=str(d.get("hypothesis_id", "")),
            op_id=str(d.get("op_id", "")),
            claim=str(d.get("claim", ""))[:500],
            expected_outcome=str(d.get("expected_outcome", ""))[:300],
            actual_outcome=(
                str(d.get("actual_outcome"))[:500]
                if d.get("actual_outcome") is not None else None
            ),
            validated=validated,
            proposed_signature_hash=(
                str(d.get("proposed_signature_hash"))
                if d.get("proposed_signature_hash") is not None else None
            ),
            created_unix=float(d.get("created_unix", 0.0) or 0.0),
            validated_unix=(
                float(d.get("validated_unix"))
                if d.get("validated_unix") is not None else None
            ),
            schema_version=str(
                d.get("schema_version", HYPOTHESIS_SCHEMA_VERSION)
            ),
        )


# ---------------------------------------------------------------------------
# Default-singleton accessor (mirrors PostmortemRecallService /
# SelfGoalFormationEngine pattern)
# ---------------------------------------------------------------------------


_default_ledger: Optional[HypothesisLedger] = None


def get_default_ledger(
    project_root: Optional[Path] = None,
) -> HypothesisLedger:
    """Return the process-wide HypothesisLedger.

    Unlike the engine accessor, the ledger doesn't have a master flag —
    it's always available so the REPL surface works even when the engine
    is hot-reverted (operator may want to inspect prior decisions)."""
    global _default_ledger
    if _default_ledger is None:
        root = Path(project_root) if project_root else Path.cwd()
        _default_ledger = HypothesisLedger(project_root=root)
    return _default_ledger


def reset_default_ledger() -> None:
    """Reset the singleton — for tests and config reload."""
    global _default_ledger
    _default_ledger = None


__all__ = [
    "DEFAULT_LEDGER_FILENAME",
    "HYPOTHESIS_SCHEMA_VERSION",
    "Hypothesis",
    "HypothesisLedger",
    "get_default_ledger",
    "make_hypothesis_id",
    "reset_default_ledger",
]
