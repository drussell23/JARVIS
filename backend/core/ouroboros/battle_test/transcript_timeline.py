"""transcript_timeline — the causal join, as a projection of the spine.

What this replaces, and why
---------------------------

PR #35182 (``ouroboros/prd-42-operation-timeline``, opened 2026-05-17)
built exactly the right *read-model*: the three ``OpsDigestObserver``
callbacks arrive at different times for the same ``op_id``, and none of
them alone answers "what happened to that op". Joining apply → verify →
commit into one row is genuinely useful and was correctly identified.

Its *substrate* did not survive the three months it sat unmerged. It
predates :mod:`transcript_spine` and references it zero times, so it
solved persistence and ordering itself: a sixth ref namespace (``r-``),
its own JSONL store at ``.jarvis/operation_timeline.jsonl``, its own
sequence counter, its own retention. That is the shape the spine exists
to abolish — quoting slice 1:

    Nothing knows whether ``t-7`` happened before or after ``n-4``.
    There is no answer to "what happened next", because "next" spans
    namespaces and no structure spans namespaces.

Merging a sixth independent ring would reinstate that defect one slice
after it was solved. So the join survives and the store does not.

A projection owns nothing
-------------------------

There is no state in this module. :func:`build_timeline` reads the
spine's existing milestone records and folds them, per ``op_id``, into
rows. Consequences, all of which are the point:

* **No second namespace.** Rows are keyed by ``op_id`` — which is not a
  ref this module mints but a foreign key the orchestrator already owns.
* **No second store.** Durability is whatever the spine already has;
  this cannot be more or less durable than the transcript it reads.
* **No second order.** ``first_seq`` is a position in the ONE sequence,
  so a row sorts against diffs and tool bodies, not merely against other
  rows.
* **No eviction policy.** When the spine drops a record the row narrows
  with it, and says so (:attr:`TimelineRow.partial`).

Owning only the edges
---------------------

Adopted verbatim from #35182, because it was right: every datum is a
pointer to whatever authority already owns it — ``op_id`` to the
OperationLedger, ``d-N`` to the DiffArchive, ``commit_hash`` to git.
Rows carry references and scalars, never bodies. No diff text, no plan,
no state-machine transitions.

Unobserved is not zero
----------------------

``verify_passed = 0`` means zero tests passed. An op whose VERIFY never
reported is a different fact, and a row that renders both as ``0/0``
would state a result that was never measured. The two are separate here
(:attr:`TimelineRow.has_verify`), for the same reason the advisor
distinguishes a measured lower bound from an unknown.

Authority boundary
------------------

* §1 deterministic — a pure fold over records already in memory
* §7 fail-closed — an unreadable spine yields NO rows, never guessed ones
* §8 observable — every row states its own completeness
* zero authority — structurally incapable of acting on the loop; pinned
  by :func:`register_shipped_invariants`, adopting #35182's AST-pin
  discipline
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.TranscriptTimeline")


__all__ = [
    "MAX_ROWS_ENV_VAR",
    "TimelineRow",
    "build_timeline",
    "read_max_rows",
    "register_shipped_invariants",
    "timeline_for_op",
]


MAX_ROWS_ENV_VAR: str = "JARVIS_TIMELINE_MAX_ROWS"

#: Only a rendering bound. The projection itself is unbounded because the
#: spine already bounds retention — a second cap here would be a second
#: authority for "how much history exists", and the smaller of the two
#: would silently win.
_DEFAULT_MAX_ROWS: int = 200

#: The kind minted by :mod:`transcript_milestones`. Read from that module
#: rather than restated, so a rename carries.
_MILESTONE_KIND: str = "milestone"

#: Sentinel for a count that was never reported, kept distinct from a
#: reported zero.
UNOBSERVED: int = -1


def read_max_rows() -> int:
    """Resolve :data:`MAX_ROWS_ENV_VAR`. NEVER raises."""
    raw = os.environ.get(MAX_ROWS_ENV_VAR, "").strip()
    if not raw:
        return _DEFAULT_MAX_ROWS
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_ROWS
    return parsed if parsed > 0 else _DEFAULT_MAX_ROWS


# ===========================================================================
# The row
# ===========================================================================


@dataclass(frozen=True)
class TimelineRow:
    """One operation's causal story, folded from the spine's records."""

    op_id: str
    #: Position of this op's FIRST record in the one transcript order.
    #: Rows sort by it, so a timeline interleaves correctly with diffs and
    #: tool bodies rather than forming a parallel history.
    first_seq: int
    last_seq: int
    #: The ``m-N`` records that composed this row.
    milestone_refs: Tuple[str, ...] = ()
    #: Foreign keys into the other vocabularies for the same op — ``d-N``
    #: diffs, ``t-N`` tool bodies, ``o-N`` op blocks. Pointers, never
    #: bodies: the stores remain the owners of their content.
    related_refs: Tuple[str, ...] = ()

    apply_mode: str = ""
    apply_files: int = 0
    has_apply: bool = False

    verify_passed: int = UNOBSERVED
    verify_total: int = UNOBSERVED
    #: Carried verbatim from the observer. "4/4 passed" means something
    #: different when the counts are repo-wide health rather than this
    #: op's tests, and dropping the qualifier would overstate evidence.
    verify_scoped_to_op: Optional[bool] = None

    commit_hash: str = ""

    #: True once the spine has evicted ANYTHING — at which point no row
    #: can demonstrate it still holds every record its op produced. A row
    #: that presented as complete here would be the transcript asserting
    #: something it can no longer support.
    partial: bool = False

    @property
    def provably_incomplete(self) -> bool:
        """Stronger than :attr:`partial`, and independent of eviction.

        VERIFY and COMMIT cannot precede the APPLY they describe, so a row
        carrying either without an apply is missing a milestone as a
        matter of causality — knowable without consulting retention at
        all. ``partial`` says "cannot prove whole"; this says "provably
        not whole", and an operator wants the second stated louder.
        """
        return (self.has_verify or self.has_commit) and not self.has_apply

    @property
    def has_verify(self) -> bool:
        """Distinct from ``verify_total == 0``, which is a real result."""
        return self.verify_total is not UNOBSERVED and self.verify_total >= 0

    @property
    def has_commit(self) -> bool:
        return bool(self.commit_hash)

    @property
    def outcome(self) -> str:
        """A coarse label, derived — never stored, so it cannot disagree
        with the fields it summarises."""
        if not self.has_apply:
            return "pending"
        if self.has_commit:
            return "committed"
        if self.has_verify:
            return "verified" if self.verify_passed == self.verify_total \
                else "verify_failed"
        return "applied"

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "op_id": self.op_id,
            "first_seq": self.first_seq,
            "last_seq": self.last_seq,
            "outcome": self.outcome,
            "milestone_refs": list(self.milestone_refs),
            "related_refs": list(self.related_refs),
            "partial": self.partial,
        }
        if self.has_apply:
            out["apply"] = {"mode": self.apply_mode, "files": self.apply_files}
        if self.has_verify:
            out["verify"] = {
                "passed": self.verify_passed,
                "total": self.verify_total,
                "scoped_to_applied_op": self.verify_scoped_to_op,
            }
        if self.has_commit:
            out["commit"] = self.commit_hash
        return out


# ===========================================================================
# The projection
# ===========================================================================


def _resolve_spine(spine: Optional[Any]) -> Optional[Any]:
    if spine is not None:
        return spine
    try:
        from backend.core.ouroboros.battle_test.transcript_spine import (
            get_default_spine,
        )
        return get_default_spine()
    except Exception:  # noqa: BLE001
        return None


def _milestone_kind() -> str:
    """Read the kind from the producer rather than restating it."""
    try:
        from backend.core.ouroboros.battle_test import transcript_milestones
        return getattr(transcript_milestones, "_KIND", _MILESTONE_KIND)
    except Exception:  # noqa: BLE001
        return _MILESTONE_KIND


class _Accumulator:
    """Mutable fold state for one ``op_id``. Never escapes this module —
    callers only ever see the frozen :class:`TimelineRow`."""

    __slots__ = (
        "op_id", "first_seq", "last_seq", "milestone_refs", "related_refs",
        "apply_mode", "apply_files", "has_apply",
        "verify_passed", "verify_total", "verify_scoped", "commit_hash",
    )

    def __init__(self, op_id: str, seq: int) -> None:
        self.op_id = op_id
        self.first_seq = seq
        self.last_seq = seq
        self.milestone_refs: List[str] = []
        self.related_refs: List[str] = []
        self.apply_mode = ""
        self.apply_files = 0
        self.has_apply = False
        self.verify_passed = UNOBSERVED
        self.verify_total = UNOBSERVED
        self.verify_scoped: Optional[bool] = None
        self.commit_hash = ""

    def note_seq(self, seq: int) -> None:
        self.first_seq = min(self.first_seq, seq)
        self.last_seq = max(self.last_seq, seq)

    def merge(self, ref: str, payload: Any) -> None:
        """Latest-write-wins per FIELD, not per row.

        Per-row would let a re-applied op's APPLY erase the VERIFY that
        followed the previous one; per-field keeps every fact until
        something of the SAME kind supersedes it, which is what makes a
        re-applied op read as a continuation rather than a reset.
        """
        self.milestone_refs.append(ref)
        if not isinstance(payload, dict):
            return
        event = str(payload.get("event", "")).strip().lower()
        if event == "apply":
            self.has_apply = True
            self.apply_mode = str(payload.get("mode", "") or "")
            self.apply_files = _as_int(payload.get("files"), 0)
        elif event == "verify":
            self.verify_passed = _as_int(payload.get("passed"), UNOBSERVED)
            self.verify_total = _as_int(payload.get("total"), UNOBSERVED)
            scoped = payload.get("scoped_to_applied_op")
            self.verify_scoped = bool(scoped) if scoped is not None else None
        elif event == "commit":
            self.commit_hash = str(payload.get("commit", "") or "")

    def freeze(self, evicted_through: int) -> TimelineRow:
        return TimelineRow(
            op_id=self.op_id,
            first_seq=self.first_seq,
            last_seq=self.last_seq,
            milestone_refs=tuple(self.milestone_refs),
            related_refs=tuple(self.related_refs),
            apply_mode=self.apply_mode,
            apply_files=self.apply_files,
            has_apply=self.has_apply,
            verify_passed=self.verify_passed,
            verify_total=self.verify_total,
            verify_scoped_to_op=self.verify_scoped,
            commit_hash=self.commit_hash,
            # ANY eviction makes every row unprovable, not just old ones.
            # ``first_seq`` is computed from SURVIVING records, so
            # ``first_seq <= evicted_through`` is unsatisfiable by
            # construction — a predicate that cannot fire, which is worse
            # than none because it reads as a guard. What is actually
            # knowable: once the spine has dropped anything, no row can
            # demonstrate it still holds every record its op produced,
            # because the evidence for that is exactly what was dropped.
            partial=evicted_through > 0,
        )


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def build_timeline(
    spine: Optional[Any] = None,
    *,
    op_id: Optional[str] = None,
    limit: Optional[int] = None,
) -> Tuple[TimelineRow, ...]:
    """Fold the spine's milestone records into per-op rows. NEVER raises.

    Rows are ordered by ``first_seq`` — a position in the ONE transcript
    order, so a timeline interleaves with the other vocabularies instead
    of forming a parallel history.

    ``limit`` bounds RENDERING only, and takes the most recent rows: an
    operator asking for a timeline wants the tail, and silently dropping
    the newest would answer a question nobody asked.
    """
    target = _resolve_spine(spine)
    if target is None:
        return ()

    want = str(op_id).strip() if op_id else ""
    kind = _milestone_kind()

    try:
        records: Sequence[Any] = tuple(target)
    except Exception:  # noqa: BLE001
        logger.debug("[Timeline] spine iteration failed", exc_info=True)
        return ()

    try:
        evicted_through = int(getattr(target, "_evicted_through", 0) or 0)
    except Exception:  # noqa: BLE001
        evicted_through = 0

    acc: Dict[str, _Accumulator] = {}
    for rec in records:
        try:
            rec_op = str(getattr(rec, "op_id", "") or "")
            if not rec_op or (want and rec_op != want):
                continue
            seq = int(getattr(rec, "seq", 0) or 0)
            entry = acc.get(rec_op)
            if entry is None:
                entry = acc[rec_op] = _Accumulator(rec_op, seq)
            else:
                entry.note_seq(seq)

            if getattr(rec, "kind", "") == kind:
                entry.merge(str(getattr(rec, "ref", "")),
                            getattr(rec, "payload", None))
            else:
                # A foreign key into another vocabulary for the same op.
                # This is why the producers pass op_id to record_event:
                # without it the join cannot reach across namespaces, and
                # the spine's shared order buys nothing here.
                entry.related_refs.append(str(getattr(rec, "ref", "")))
        except Exception:  # noqa: BLE001
            logger.debug("[Timeline] record skipped", exc_info=True)
            continue

    rows = sorted(
        (a.freeze(evicted_through) for a in acc.values()),
        key=lambda r: r.first_seq,
    )
    cap = int(limit) if limit is not None else read_max_rows()
    if cap >= 0 and len(rows) > cap:
        rows = rows[-cap:]
    return tuple(rows)


def timeline_for_op(op_id: str, spine: Optional[Any] = None,
                    ) -> Optional[TimelineRow]:
    """The single row for one op, or ``None``. NEVER raises."""
    rows = build_timeline(spine, op_id=op_id, limit=1)
    return rows[0] if rows else None


# ===========================================================================
# Authority invariant — #35182's AST-pin discipline, adopted
# ===========================================================================


def register_shipped_invariants() -> list:
    """Pin that this module can never act on the loop. NEVER raises.

    A read-model with an import of the orchestrator is one refactor away
    from being a writer. The pin makes that a test failure rather than a
    review question — the discipline #35182 established for the same
    reason, kept even though its substrate was not.
    """
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    import ast as _ast

    _FORBIDDEN = (
        "orchestrator", "policy_engine", "iron_gate", "change_engine",
        "candidate_generator", "governed_loop_service", "repair_engine",
    )

    def _validate_zero_authority(tree, _source) -> tuple:
        del _source
        violations = []
        for node in _ast.walk(tree):
            mod = ""
            if isinstance(node, _ast.ImportFrom):
                mod = node.module or ""
            elif isinstance(node, _ast.Import):
                mod = ",".join(a.name for a in node.names)
            if not mod:
                continue
            for banned in _FORBIDDEN:
                if banned in mod:
                    violations.append(
                        f"transcript_timeline imports {banned!r} — a "
                        f"read-model with loop authority is one refactor "
                        f"from being a writer"
                    )
        return tuple(violations)

    #: Structural, not textual. A source grep would trip on this module's
    #: own docstring — which names ``.jarvis/operation_timeline.jsonl`` to
    #: explain what was retired — and on the token list below. A pin that
    #: fails on its own explanation teaches people to delete the pin.
    _STORE_CALLS = frozenset({"open", "mkstemp", "flock_append_line",
                              "write_text", "write_bytes"})
    _STORE_IMPORTS = ("pathlib", "cross_process_jsonl", "transcript_writer",
                      "transcript_log", "durable_io", "shelve", "sqlite3")

    def _validate_no_second_store(tree, _source) -> tuple:
        del _source
        violations = []
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Call):
                fn = node.func
                name = getattr(fn, "id", "") or getattr(fn, "attr", "")
                if name in _STORE_CALLS:
                    violations.append(
                        f"transcript_timeline calls {name}() — the join is a "
                        f"PROJECTION; a private store would re-create the "
                        f"independent-namespace defect the spine abolished, "
                        f"which is why PR #35182 was closed"
                    )
            elif isinstance(node, (_ast.Import, _ast.ImportFrom)):
                mod = (getattr(node, "module", "") or "") + ",".join(
                    a.name for a in node.names
                )
                for banned in _STORE_IMPORTS:
                    if banned in mod:
                        violations.append(
                            f"transcript_timeline imports {banned!r} — a "
                            f"projection must not reach a persistence layer; "
                            f"durability belongs to the spine it reads"
                        )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name="timeline_has_zero_loop_authority",
            target_file=(
                "backend/core/ouroboros/battle_test/transcript_timeline.py"
            ),
            description=(
                "The timeline read-model must never import the orchestrator "
                "or any policy/mutation surface."
            ),
            validate=_validate_zero_authority,
        ),
        ShippedCodeInvariant(
            invariant_name="timeline_owns_no_store",
            target_file=(
                "backend/core/ouroboros/battle_test/transcript_timeline.py"
            ),
            description=(
                "BUG-FIX REGRESSION PIN: the causal join must remain a pure "
                "projection of the spine. Re-introducing a private store or "
                "ref namespace is the #35182 defect returning."
            ),
            validate=_validate_no_second_store,
        ),
    ]
