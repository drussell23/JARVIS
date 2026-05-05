"""DiffArchive — session-scoped audit ring for candidate diffs + outcomes.
==========================================================================

Slice 1 of the **Gap #4 closure arc** (IDE-native diff review).

Root problem
------------

Today's Yellow-tier (NOTIFY_APPLY) flow renders a 5s ``DiffPreviewRenderer``
overlay, then auto-applies. The diff content is gone the moment the
overlay closes. ``dump_full_diff`` (env-gated) writes a flat file but
there's no in-process retrieval and no lifecycle outcomes — once
applied, the operator can't trivially answer "what did O+V change in
op-019d8…?", "did it pass VERIFY?", or "show me the last 5 diffs that
got rejected."

Slice 1 supplies the **substrate**: a thread-safe FIFO ring of
:class:`ArchivedDiff` records with monotonic ``d-N`` references that
NEVER reuse (even after eviction), plus mutating APIs that thread the
APPLY → VERIFY → COMPLETE lifecycle outcomes onto each archived entry.

Architectural reuse
-------------------

* **Monotonic ref pattern** mirrors :class:`BoundedBodyStore` (Gap #2
  Slice 3) — same safety contract: a printed ``d-12`` either resolves
  to the same diff (if resident) or ``None`` (if evicted), but NEVER
  a different diff.
* **Closed-enum + frozen dataclass + schema_version** house style
  (matches ``ArchivedDiff``, ``DiffOutcome``).
* **Module-owned discovery** via ``register_flags()`` and
  ``register_shipped_invariants()`` — picked up automatically by
  ``flag_registry_seed._FLAG_PROVIDER_PACKAGES`` and
  ``shipped_code_invariants._INVARIANT_PROVIDER_PACKAGES``.

Authority boundary
------------------

* §1 deterministic — pure container; no LLM, no I/O, no Console
* §7 fail-closed — every public method has a documented fallback;
  invalid refs / non-string inputs return ``None`` or coerce safely;
  NEVER raises
* §8 observable — :class:`ArchiveSnapshot` projects state for
  ``GET /observability/diff-archive`` (Slice 4 wiring)

What this module does NOT do (deferred to later slices)
--------------------------------------------------------

* Slice 2 — :class:`ReviewBranchManager` produces the ``ouroboros/
  preview/{op-id}`` git artifact the operator reviews in VS Code.
  This module only stores the diff *text* + metadata; the branch is
  separate.
* Slice 3 — orchestrator integration (the call sites that emit
  ``add(...)``, ``mark_applied(...)``, ``mark_verified(...)``).
* Slice 4 — REPL ``/diff`` and ``/expand`` verbs + SSE events.
* Slice 6 — graduation (master flag default-true, AST pins).
"""
from __future__ import annotations

import enum
import logging
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.DiffArchive")


# ===========================================================================
# Schema + env vocabulary
# ===========================================================================


DIFF_ARCHIVE_SCHEMA_VERSION: str = "diff_archive.v1"


ARCHIVE_SIZE_ENV_VAR: str = "JARVIS_DIFF_ARCHIVE_SIZE"


_DEFAULT_ARCHIVE_SIZE: int = 30
_MIN_ARCHIVE_SIZE: int = 1
_MAX_ARCHIVE_SIZE: int = 1_000


REF_PREFIX: str = "d-"


# ===========================================================================
# Closed taxonomy — diff outcome
# ===========================================================================


class DiffOutcome(str, enum.Enum):
    """Closed 5-value lifecycle outcome.

    A diff begins :data:`PENDING` at archive time. Slice 3's
    orchestrator hooks transition it through the lifecycle. Once
    terminal (APPLIED / REJECTED / FAILED), the entry is read-only
    for the rest of its archive lifetime.
    """

    PENDING = "pending"           # archived but not yet acted upon
    APPLIED = "applied"           # operator/timeout accepted; APPLY succeeded
    REJECTED = "rejected"         # operator rejected (REPL or VS Code button)
    SUPERSEDED = "superseded"     # newer candidate replaced this one mid-review
    FAILED = "failed"             # APPLY phase failed at git/disk layer

    @classmethod
    def coerce(cls, raw: object) -> "DiffOutcome":
        """Lenient parse — anything not recognized becomes
        :data:`PENDING`. NEVER raises."""
        if isinstance(raw, cls):
            return raw
        if isinstance(raw, str):
            s = raw.strip().lower()
            for member in cls:
                if member.value == s:
                    return member
        return cls.PENDING

    @property
    def is_terminal(self) -> bool:
        return self in (
            DiffOutcome.APPLIED, DiffOutcome.REJECTED,
            DiffOutcome.SUPERSEDED, DiffOutcome.FAILED,
        )


class VerifyOutcome(str, enum.Enum):
    """Closed 4-value VERIFY-phase outcome.

    Distinct from :class:`DiffOutcome` because a diff can be APPLIED
    and then fail VERIFY (tests broke); operators want to filter on
    that combination specifically.
    """

    PENDING = "pending"          # not yet verified
    PASSED = "passed"            # VERIFY tests passed
    FAILED = "failed"            # VERIFY tests failed (L2 may engage)
    SKIPPED = "skipped"          # VERIFY skipped (no scoped tests)

    @classmethod
    def coerce(cls, raw: object) -> "VerifyOutcome":
        if isinstance(raw, cls):
            return raw
        if isinstance(raw, str):
            s = raw.strip().lower()
            for member in cls:
                if member.value == s:
                    return member
        return cls.PENDING

    @property
    def is_terminal(self) -> bool:
        return self is not VerifyOutcome.PENDING


# ===========================================================================
# Frozen records
# ===========================================================================


@dataclass(frozen=True)
class ArchivedDiff:
    """One archived candidate diff with full lifecycle metadata.

    Frozen + hashable. Lifecycle-state mutations replace-in-place via
    :func:`dataclasses.replace` from inside :meth:`DiffArchive.mark_*`
    methods; the original record is GC'd after replacement.

    Fields
    ------
    * ``ref`` — opaque ``d-N`` handle (the only stable identifier).
    * ``op_id`` — orchestrator op id this diff belongs to. May be
      shared by multiple archived diffs if the same op was retried
      with different candidates.
    * ``risk_tier`` — ``"safe_auto"`` / ``"notify_apply"`` /
      ``"approval_required"`` / ``"blocked"`` (string for forward-
      compat with potential future tiers; use the orchestrator's
      RiskTier enum upstream).
    * ``file_paths`` — repo-relative paths the diff touches.
    * ``diff_text`` — full unified-diff text. May be empty for
      single-file write_file ops where no prior content existed.
    * ``summary`` — 1-line summary (e.g. ``"+12 / -3 in 2 files"``).
    * ``review_branch`` — Slice 2's ``ouroboros/preview/{op-id}``
      branch name; ``None`` until Slice 3 wires the branch creation.
    * ``apply_outcome`` — :class:`DiffOutcome` (PENDING at insert).
    * ``verify_outcome`` — :class:`VerifyOutcome` (PENDING at insert).
    * ``apply_error`` — short reason string when ``apply_outcome``
      is REJECTED / FAILED. Empty otherwise.
    * ``archived_at`` — ``time.monotonic()`` timestamp at insert.
    * ``terminal_at`` — ``time.monotonic()`` timestamp when
      apply_outcome first became terminal; ``0.0`` while pending.
    """

    ref: str
    op_id: str
    risk_tier: str
    file_paths: Tuple[str, ...]
    diff_text: str
    summary: str
    review_branch: Optional[str]
    apply_outcome: DiffOutcome
    verify_outcome: VerifyOutcome
    apply_error: str
    archived_at: float
    terminal_at: float
    schema_version: str = DIFF_ARCHIVE_SCHEMA_VERSION

    # ---- projections -------------------------------------------------

    def to_dict(self, *, include_diff_text: bool = False) -> Dict[str, Any]:
        """Read-only projection. By default omits ``diff_text`` to
        keep SSE / observability payloads bounded — caller passes
        ``include_diff_text=True`` for the ``/expand <ref>``
        recovery path."""
        d: Dict[str, Any] = {
            "ref": self.ref,
            "op_id": self.op_id,
            "risk_tier": self.risk_tier,
            "file_paths": list(self.file_paths),
            "summary": self.summary,
            "review_branch": self.review_branch,
            "apply_outcome": self.apply_outcome.value,
            "verify_outcome": self.verify_outcome.value,
            "apply_error": self.apply_error,
            "archived_at": self.archived_at,
            "terminal_at": self.terminal_at,
            "diff_chars": len(self.diff_text),
            "schema_version": self.schema_version,
        }
        if include_diff_text:
            d["diff_text"] = self.diff_text
        return d


@dataclass(frozen=True)
class ArchiveSnapshot:
    """Read-only projection of the archive's state."""

    capacity: int
    size: int
    next_seq: int
    pending_count: int
    applied_count: int
    rejected_count: int
    failed_count: int
    schema_version: str = DIFF_ARCHIVE_SCHEMA_VERSION

    @property
    def utilization(self) -> float:
        if self.capacity <= 0:
            return 0.0
        return min(1.0, self.size / self.capacity)


# ===========================================================================
# Helpers
# ===========================================================================


def _read_capacity_from_env() -> int:
    raw = os.environ.get(ARCHIVE_SIZE_ENV_VAR, "").strip()
    if not raw:
        return _DEFAULT_ARCHIVE_SIZE
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        logger.debug(
            "[DiffArchive] %s=%r is not an int; using default %d",
            ARCHIVE_SIZE_ENV_VAR, raw, _DEFAULT_ARCHIVE_SIZE,
        )
        return _DEFAULT_ARCHIVE_SIZE
    if parsed < _MIN_ARCHIVE_SIZE:
        return _MIN_ARCHIVE_SIZE
    if parsed > _MAX_ARCHIVE_SIZE:
        return _MAX_ARCHIVE_SIZE
    return parsed


def _safe_str(raw: object) -> str:
    if raw is None:
        return ""
    try:
        return str(raw)
    except Exception:  # noqa: BLE001
        return ""


def _safe_int(raw: object, default: int = 0) -> int:
    if isinstance(raw, bool):
        return default
    if isinstance(raw, int):
        return raw
    return default


def _safe_path_tuple(raw: object) -> Tuple[str, ...]:
    """Coerce iterable-of-strings into a frozen tuple of paths."""
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,)
    # Defensive: only iterate if the value declares ``__iter__``.
    # Anything else (int, dict, custom non-iterable object) yields ().
    if not hasattr(raw, "__iter__"):
        return ()
    try:
        return tuple(_safe_str(x) for x in raw if _safe_str(x))  # type: ignore[union-attr]
    except TypeError:
        return ()


# ===========================================================================
# DiffArchive — the ring
# ===========================================================================


class DiffArchive:
    """Thread-safe bounded FIFO of :class:`ArchivedDiff` records.

    Eviction policy
    ---------------
    Drop-oldest on overflow. Eviction is **outcome-blind** — we don't
    privilege APPLIED entries over REJECTED ones. Operators who need
    long-term diff history should run periodic ``/diff list rejected``
    snapshots into a downstream ledger; the archive is for hot state.

    Reference allocation
    --------------------
    ``d-1``, ``d-2``, ... — monotonic counter, NEVER reset. Identical
    safety contract to :class:`BoundedBodyStore` (Gap #2 Slice 3): a
    ref printed at time t1 either resolves to the same diff or
    ``None``, never a different diff.

    Lifecycle mutations
    -------------------
    :meth:`mark_applied` / :meth:`mark_verified` use
    :func:`dataclasses.replace` to produce an updated frozen record,
    then replace-in-place at the existing ``ref`` key (preserving
    insertion order so eviction stays predictable).

    Thread safety
    -------------
    Single :class:`threading.RLock` serializes all operations.
    Reentrant so listeners reading via :meth:`snapshot` inside an
    on-change observer don't self-deadlock.
    """

    def __init__(self, *, capacity: Optional[int] = None) -> None:
        if capacity is None:
            cap = _read_capacity_from_env()
        else:
            cap = max(
                _MIN_ARCHIVE_SIZE,
                min(_MAX_ARCHIVE_SIZE, _safe_int(capacity, _DEFAULT_ARCHIVE_SIZE)),
            )
        self._capacity: int = cap
        self._items: "OrderedDict[str, ArchivedDiff]" = OrderedDict()
        self._next_seq: int = 1
        self._lock = threading.RLock()

    # ---- introspection -----------------------------------------------

    @property
    def capacity(self) -> int:
        return self._capacity

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def snapshot(self) -> ArchiveSnapshot:
        """Cheap read-only projection. NEVER raises."""
        with self._lock:
            pending = applied = rejected = failed = 0
            for entry in self._items.values():
                outcome = entry.apply_outcome
                if outcome is DiffOutcome.PENDING:
                    pending += 1
                elif outcome is DiffOutcome.APPLIED:
                    applied += 1
                elif outcome is DiffOutcome.REJECTED:
                    rejected += 1
                elif outcome is DiffOutcome.FAILED:
                    failed += 1
            return ArchiveSnapshot(
                capacity=self._capacity,
                size=len(self._items),
                next_seq=self._next_seq,
                pending_count=pending,
                applied_count=applied,
                rejected_count=rejected,
                failed_count=failed,
            )

    # ---- mutating API -------------------------------------------------

    def add(
        self,
        *,
        op_id: object,
        risk_tier: object,
        file_paths: object,
        diff_text: object,
        summary: object = "",
        review_branch: object = None,
    ) -> ArchivedDiff:
        """Archive a candidate diff. Returns the :class:`ArchivedDiff`
        with stable ``ref``. NEVER raises.

        Initial ``apply_outcome`` is :data:`DiffOutcome.PENDING` and
        ``verify_outcome`` is :data:`VerifyOutcome.PENDING`; Slice 3
        transitions them via :meth:`mark_applied` / :meth:`mark_verified`.

        Capacity eviction kicks in *after* insertion so the most
        recent diff always wins.
        """
        op_safe = _safe_str(op_id)
        tier_safe = _safe_str(risk_tier).lower() or "unknown"
        paths_safe = _safe_path_tuple(file_paths)
        diff_safe = _safe_str(diff_text)
        summary_safe = _safe_str(summary)
        branch_safe = _safe_str(review_branch) or None

        with self._lock:
            ref = f"{REF_PREFIX}{self._next_seq}"
            self._next_seq += 1
            entry = ArchivedDiff(
                ref=ref,
                op_id=op_safe,
                risk_tier=tier_safe,
                file_paths=paths_safe,
                diff_text=diff_safe,
                summary=summary_safe,
                review_branch=branch_safe,
                apply_outcome=DiffOutcome.PENDING,
                verify_outcome=VerifyOutcome.PENDING,
                apply_error="",
                archived_at=time.monotonic(),
                terminal_at=0.0,
            )
            self._items[ref] = entry
            while len(self._items) > self._capacity:
                self._items.popitem(last=False)
            return entry

    def mark_applied(
        self,
        ref: object,
        outcome: object,
        *,
        error: object = "",
    ) -> Optional[ArchivedDiff]:
        """Transition the apply_outcome. Returns the updated record,
        or ``None`` if the ref is unknown / evicted.

        Idempotent for terminal outcomes — calling ``mark_applied(d-12,
        APPLIED)`` twice returns the same record both times. Once
        terminal, the outcome is **frozen**: subsequent calls with a
        different outcome are silently ignored (operators can inspect
        via the ``apply_error`` field's "frozen" suffix). NEVER raises.
        """
        if not isinstance(ref, str):
            return None
        outcome_enum = DiffOutcome.coerce(outcome)
        error_safe = _safe_str(error)
        with self._lock:
            current = self._items.get(ref)
            if current is None:
                return None
            # Once terminal, freeze. (Idempotent re-calls with the
            # same outcome are a no-op; calls with a different
            # outcome are dropped — log only.)
            if current.apply_outcome.is_terminal:
                if current.apply_outcome is not outcome_enum:
                    logger.debug(
                        "[DiffArchive] mark_applied(%s, %s) ignored — "
                        "already terminal as %s",
                        ref, outcome_enum.value,
                        current.apply_outcome.value,
                    )
                return current
            updated = replace(
                current,
                apply_outcome=outcome_enum,
                apply_error=error_safe,
                terminal_at=(
                    time.monotonic()
                    if outcome_enum.is_terminal
                    else current.terminal_at
                ),
            )
            self._items[ref] = updated
            return updated

    def mark_verified(
        self,
        ref: object,
        outcome: object,
    ) -> Optional[ArchivedDiff]:
        """Transition the verify_outcome. Returns the updated record,
        or ``None`` if ref is unknown / evicted. NEVER raises.

        Once verify_outcome is terminal it is **frozen** (matches
        :meth:`mark_applied` semantics).
        """
        if not isinstance(ref, str):
            return None
        outcome_enum = VerifyOutcome.coerce(outcome)
        with self._lock:
            current = self._items.get(ref)
            if current is None:
                return None
            if current.verify_outcome.is_terminal:
                if current.verify_outcome is not outcome_enum:
                    logger.debug(
                        "[DiffArchive] mark_verified(%s, %s) ignored — "
                        "already terminal as %s",
                        ref, outcome_enum.value,
                        current.verify_outcome.value,
                    )
                return current
            updated = replace(current, verify_outcome=outcome_enum)
            self._items[ref] = updated
            return updated

    def attach_review_branch(
        self, ref: object, branch_name: object,
    ) -> Optional[ArchivedDiff]:
        """Stamp the Slice 2 review branch name onto an entry.
        Returns the updated record, or ``None`` for unknown ref.
        Once a branch is attached, subsequent calls overwrite (the
        last-known branch wins — supports re-attempt after a
        branch-collision FAILED outcome). NEVER raises."""
        if not isinstance(ref, str):
            return None
        branch_safe = _safe_str(branch_name) or None
        with self._lock:
            current = self._items.get(ref)
            if current is None:
                return None
            updated = replace(current, review_branch=branch_safe)
            self._items[ref] = updated
            return updated

    def clear(self) -> None:
        """Drop all archived diffs. Counter is NOT reset (see class
        docstring on monotonic refs)."""
        with self._lock:
            self._items.clear()

    # ---- query API ---------------------------------------------------

    def lookup(self, ref: object) -> Optional[ArchivedDiff]:
        """Resolve a ref. Returns ``None`` for unknown / evicted /
        malformed input. NEVER raises."""
        if not isinstance(ref, str):
            return None
        with self._lock:
            return self._items.get(ref)

    def list_recent(
        self, limit: int = 10,
    ) -> Tuple[ArchivedDiff, ...]:
        """Newest → oldest, capped by ``limit``. ``limit <= 0``
        returns empty tuple."""
        if not isinstance(limit, int) or limit <= 0:
            return ()
        with self._lock:
            # OrderedDict iteration is oldest → newest; reverse for
            # "most recent first" presentation.
            entries = list(self._items.values())
        entries.reverse()
        return tuple(entries[:limit])

    def find_by_op_id(self, op_id: object) -> Tuple[ArchivedDiff, ...]:
        """All entries (oldest → newest) matching ``op_id``. Empty
        tuple for unknown / non-string."""
        if not isinstance(op_id, str) or not op_id:
            return ()
        with self._lock:
            return tuple(
                e for e in self._items.values() if e.op_id == op_id
            )

    def find_by_file(self, path: object) -> Tuple[ArchivedDiff, ...]:
        """All entries whose ``file_paths`` contains ``path`` (exact
        match). Operators who want glob support should expand client-
        side."""
        if not isinstance(path, str) or not path:
            return ()
        with self._lock:
            return tuple(
                e for e in self._items.values() if path in e.file_paths
            )

    def find_by_outcome(
        self, *, apply: object = None, verify: object = None,
    ) -> Tuple[ArchivedDiff, ...]:
        """Filter by terminal outcomes. ``None`` means "any". Both
        ``None`` returns the full archive (oldest → newest)."""
        apply_enum = (
            DiffOutcome.coerce(apply) if apply is not None else None
        )
        verify_enum = (
            VerifyOutcome.coerce(verify) if verify is not None else None
        )
        with self._lock:
            entries = list(self._items.values())
        out: List[ArchivedDiff] = []
        for e in entries:
            if apply_enum is not None and e.apply_outcome is not apply_enum:
                continue
            if verify_enum is not None and e.verify_outcome is not verify_enum:
                continue
            out.append(e)
        return tuple(out)

    def all_refs(self) -> Tuple[str, ...]:
        """All currently-resident refs, oldest → newest."""
        with self._lock:
            return tuple(self._items.keys())


# ===========================================================================
# Module singleton — same pattern as BoundedBodyStore
# ===========================================================================


_default_archive: Optional[DiffArchive] = None
_singleton_lock = threading.Lock()


def get_default_archive() -> DiffArchive:
    """Return the process-wide default archive (constructed lazily)."""
    global _default_archive
    with _singleton_lock:
        if _default_archive is None:
            _default_archive = DiffArchive()
        return _default_archive


def reset_default_archive_for_tests() -> None:
    """Test isolation hook."""
    global _default_archive
    with _singleton_lock:
        _default_archive = None


__all__ = [
    "ARCHIVE_SIZE_ENV_VAR",
    "DIFF_ARCHIVE_SCHEMA_VERSION",
    "REF_PREFIX",
    "ArchivedDiff",
    "ArchiveSnapshot",
    "DiffArchive",
    "DiffOutcome",
    "VerifyOutcome",
    "get_default_archive",
    "reset_default_archive_for_tests",
]
