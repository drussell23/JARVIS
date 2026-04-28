"""Slice RC.1 — Typed ArtifactBag with per-key write authority.

Per ``OUROBOROS_VENOM_PRD.md`` §24.6.1 (HIGH severity race condition):

  > Cross-runner artifact ordering — ``PhaseResult.artifacts`` is a
  > ``Mapping[str, Any]`` with no ownership semantics. Two phase runners
  > can write to the same key and the orchestrator silently takes the
  > last write. Replace ``ctx.artifacts`` with a typed ``ArtifactBag``
  > that enforces per-key write authority.

This module ships the ``ArtifactBag`` primitive:

  * Per-key write authority: each key is owned by a declared phase.
  * Write attempts from non-owning phases are REJECTED (not silently
    overwritten).
  * The bag is frozen after construction — mutations produce a new bag
    via ``with_entry()`` / ``merge()``.
  * Thread-safe reads: the bag is a frozen dataclass.

## Cage rules (load-bearing)

  * **Stdlib-only import surface.** No governance, no provider, no
    orchestrator imports. This is a leaf module like
    ``determinism_substrate.py``.
  * **Frozen + immutable** — mutations produce new instances.
  * **NEVER raises into the caller** — authority violations return
    ``(False, reason)`` tuples.
  * **Master flag: none** — this is a structural upgrade, always active
    once deployed.

## Integration surface

  ``PhaseResult.artifacts`` transitions from ``Mapping[str, Any]`` to
  ``ArtifactBag``. Phase runners declare ownership at class level via
  ``OWNED_ARTIFACT_KEYS``. The orchestrator's ``_dispatch_phase_runner``
  validates ownership before merging results.
"""
from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Iterator, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ArtifactEntry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArtifactEntry:
    """One entry in the bag — value + write provenance.

    Fields
    ------
    key : str
        The artifact key (e.g. ``"generation_metadata"``,
        ``"validation_summary"``).
    value : Any
        The artifact value. Must be JSON-serializable for §8 audit.
    owner_phase : str
        Phase name that wrote this entry (e.g. ``"GENERATE"``,
        ``"VALIDATE"``). Used for authority checks.
    written_at_epoch : float
        ``time.time()`` when the entry was written. For audit only.
    """

    key: str
    value: Any
    owner_phase: str
    written_at_epoch: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "owner_phase": self.owner_phase,
            "written_at_epoch": self.written_at_epoch,
        }


# ---------------------------------------------------------------------------
# ArtifactBag
# ---------------------------------------------------------------------------


# Well-known artifact keys and their owning phases. Phase runners that
# write to these keys MUST be the declared owner. Keys not in this
# registry are open (any phase can write — backwards compatibility for
# the transition period).
#
# Format: {key_name: frozenset_of_allowed_phase_names}
ARTIFACT_KEY_OWNERSHIP: Dict[str, FrozenSet[str]] = {
    "generation_metadata": frozenset({"GENERATE", "GENERATE_RETRY"}),
    "generation_candidates": frozenset({"GENERATE", "GENERATE_RETRY"}),
    "validation_summary": frozenset({"VALIDATE", "VALIDATE_RETRY"}),
    "validation_diagnostics": frozenset({"VALIDATE", "VALIDATE_RETRY"}),
    "gate_decision": frozenset({"GATE"}),
    "gate_rationale": frozenset({"GATE"}),
    "approval_decision": frozenset({"APPROVE"}),
    "apply_result": frozenset({"APPLY"}),
    "apply_commit_hash": frozenset({"APPLY"}),
    "verify_result": frozenset({"VERIFY", "VISUAL_VERIFY"}),
    "verify_test_output": frozenset({"VERIFY"}),
    "visual_verify_result": frozenset({"VISUAL_VERIFY"}),
    "plan_output": frozenset({"PLAN"}),
    "route_decision": frozenset({"ROUTE"}),
    "classify_result": frozenset({"CLASSIFY"}),
    "context_expansion_summary": frozenset({"CONTEXT_EXPANSION"}),
    # Determinism substrate additions (Slice 1.2)
    "decision_hash": frozenset({
        "CLASSIFY", "ROUTE", "GENERATE", "GENERATE_RETRY",
        "VALIDATE", "VALIDATE_RETRY", "GATE",
    }),
}


@dataclass(frozen=True)
class ArtifactBag:
    """Typed, authority-enforced artifact bag.

    Replaces ``Mapping[str, Any]`` on ``PhaseResult.artifacts`` to
    prevent cross-runner write collisions (§24.6.1).

    The bag is frozen — all mutations produce new instances via
    ``with_entry()`` or ``merge()``.

    Usage::

        bag = ArtifactBag.empty()
        ok, bag_or_reason = bag.with_entry(
            key="generation_metadata",
            value={"model": "claude"},
            writer_phase="GENERATE",
        )
        if ok:
            assert bag_or_reason["generation_metadata"].value == {"model": "claude"}
    """

    _entries: Tuple[ArtifactEntry, ...] = ()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def empty(cls) -> "ArtifactBag":
        """Create an empty bag."""
        return cls(_entries=())

    @classmethod
    def from_mapping(
        cls,
        mapping: Mapping[str, Any],
        *,
        writer_phase: str,
        ts_epoch: float = 0.0,
    ) -> "ArtifactBag":
        """Create a bag from a legacy ``Mapping[str, Any]``.

        All keys are attributed to ``writer_phase``. Used during the
        transition period when not all callers have migrated to
        ``ArtifactBag``.
        """
        import time as _time
        ts = ts_epoch if ts_epoch > 0 else _time.time()
        entries = tuple(
            ArtifactEntry(
                key=str(k),
                value=v,
                owner_phase=writer_phase,
                written_at_epoch=ts,
            )
            for k, v in mapping.items()
        )
        return cls(_entries=entries)

    # ------------------------------------------------------------------
    # Read API (Mapping-like)
    # ------------------------------------------------------------------

    def __contains__(self, key: str) -> bool:
        return any(e.key == key for e in self._entries)

    def __getitem__(self, key: str) -> ArtifactEntry:
        for e in self._entries:
            if e.key == key:
                return e
        raise KeyError(key)

    def get(
        self, key: str, default: Any = None,
    ) -> Optional[ArtifactEntry]:
        """Get an entry by key, or ``default`` if not found."""
        for e in self._entries:
            if e.key == key:
                return e
        return default

    def get_value(self, key: str, default: Any = None) -> Any:
        """Get the VALUE of an entry by key (unwraps ``ArtifactEntry``)."""
        entry = self.get(key)
        if entry is not None:
            return entry.value
        return default

    def keys(self) -> Tuple[str, ...]:
        return tuple(e.key for e in self._entries)

    def values(self) -> Tuple[ArtifactEntry, ...]:
        return self._entries

    def items(self) -> Tuple[Tuple[str, ArtifactEntry], ...]:
        return tuple((e.key, e) for e in self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[str]:
        return iter(e.key for e in self._entries)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to a plain dict (value-only) for JSON serialization."""
        return {e.key: e.value for e in self._entries}

    def to_audit_dict(self) -> Dict[str, Dict[str, Any]]:
        """Convert to a full audit dict (includes provenance)."""
        return {e.key: e.to_dict() for e in self._entries}

    # ------------------------------------------------------------------
    # Write API (returns new bags)
    # ------------------------------------------------------------------

    def with_entry(
        self,
        *,
        key: str,
        value: Any,
        writer_phase: str,
        ts_epoch: float = 0.0,
    ) -> Tuple[bool, "ArtifactBag"]:
        """Add or update an entry. Returns ``(success, new_bag)``.

        Authority check:
          * If ``key`` is in ``ARTIFACT_KEY_OWNERSHIP``, the
            ``writer_phase`` must be in the allowed set.
          * If the key already exists with a DIFFERENT owner, the
            write is rejected.

        On rejection, returns ``(False, self)`` (unchanged bag).
        On success, returns ``(True, new_bag)``.
        """
        import time as _time
        ts = ts_epoch if ts_epoch > 0 else _time.time()

        # Authority check against the registry.
        allowed = ARTIFACT_KEY_OWNERSHIP.get(key)
        if allowed is not None:
            if writer_phase not in allowed:
                logger.warning(
                    "[ArtifactBag] REJECTED write to %r by phase %s "
                    "(allowed: %s)",
                    key, writer_phase, sorted(allowed),
                )
                return (False, self)

        # Check for existing entry with different owner.
        existing = self.get(key)
        if existing is not None and existing.owner_phase != writer_phase:
            # If the existing key is in the open registry (not in
            # ARTIFACT_KEY_OWNERSHIP), we allow the overwrite for
            # backwards compatibility but log a warning.
            if key in ARTIFACT_KEY_OWNERSHIP:
                logger.warning(
                    "[ArtifactBag] REJECTED overwrite of %r: owned by "
                    "%s, writer is %s",
                    key, existing.owner_phase, writer_phase,
                )
                return (False, self)
            else:
                logger.info(
                    "[ArtifactBag] overwriting unregistered key %r: "
                    "was %s, now %s (migration-compat)",
                    key, existing.owner_phase, writer_phase,
                )

        new_entry = ArtifactEntry(
            key=key,
            value=value,
            owner_phase=writer_phase,
            written_at_epoch=ts,
        )

        # Replace existing or append.
        new_entries = tuple(
            new_entry if e.key == key else e
            for e in self._entries
        )
        if key not in self:
            new_entries = self._entries + (new_entry,)

        return (True, ArtifactBag(_entries=new_entries))

    def merge(
        self,
        other: "ArtifactBag",
    ) -> Tuple[bool, "ArtifactBag", Tuple[str, ...]]:
        """Merge ``other`` into this bag. Returns
        ``(all_ok, new_bag, rejected_keys)``.

        Each entry in ``other`` is merged via ``with_entry()``.
        Rejected entries are collected; the merge continues past
        rejections (partial merge is allowed).
        """
        result = self
        rejected: list[str] = []
        for entry in other._entries:
            ok, result = result.with_entry(
                key=entry.key,
                value=entry.value,
                writer_phase=entry.owner_phase,
                ts_epoch=entry.written_at_epoch,
            )
            if not ok:
                rejected.append(entry.key)
        return (not rejected, result, tuple(rejected))


__all__ = [
    "ARTIFACT_KEY_OWNERSHIP",
    "ArtifactBag",
    "ArtifactEntry",
]
