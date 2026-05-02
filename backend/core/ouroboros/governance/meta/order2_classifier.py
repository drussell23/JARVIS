"""RR Pass B Slice 2 — Order-2 manifest classifier (pure function).

Per ``memory/project_reverse_russian_doll_pass_b.md`` §4.2:

  > The classifier hook lives at GATE phase, in
  > ``phase_runners/gate_runner.py``. Pseudocode::
  >
  >     def classify_order2(candidate, manifest: Order2Manifest) -> bool:
  >         for change in candidate.iter_changes():  # multi-file aware
  >             for entry in manifest.entries:
  >                 if change.repo == entry.repo and \\
  >                    fnmatch(change.path, entry.path_glob):
  >                     return True
  >         return False
  >
  > If ``classify_order2(...)`` returns ``True``, GATE forces
  > ``risk_tier = ORDER_2_GOVERNANCE`` after the existing risk-tier-floor
  > composition (so a ``JARVIS_PARANOIA_MODE=1`` override cannot
  > accidentally lower an Order-2 op below itself).

This module is the **pure-function classifier**. Slice 2 ships the
function only; Slice 2b (or Slice 5 MetaPhaseRunner) wires the call
site in ``gate_runner.py``. Splitting the wiring from the function
keeps the cage-touching change isolated to a separate, smaller PR.

Authority invariants (Pass B §3.4):
  * No imports of orchestrator / policy / iron_gate / risk_tier_floor
    / change_engine / candidate_generator / gate / semantic_guardian
    / semantic_firewall / scoped_tool_backend.
  * Allowed: ``meta.order2_manifest`` (own-package primitive) +
    ``risk_engine.RiskTier`` (the new enum value).
  * Pure data — no I/O, no subprocess, no env mutation.
  * The classifier is **observability** until the GATE wiring lands.
    Calling it from a test or a future caller is safe; the function
    returns a boolean + does not mutate any state.

Default-off behind ``JARVIS_ORDER2_RISK_CLASS_ENABLED``. When off,
:func:`apply_order2_floor` returns its input tier unchanged
regardless of manifest match — preserves the hot-revert even when
the manifest is loaded.
"""
from __future__ import annotations

import logging
import os
from typing import Optional, Sequence

from backend.core.ouroboros.governance.meta.order2_manifest import (
    ManifestLoadStatus,
    Order2Manifest,
    get_default_manifest,
)
from backend.core.ouroboros.governance.risk_engine import RiskTier

logger = logging.getLogger(__name__)


_TRUTHY = ("1", "true", "yes", "on")


def is_enabled() -> bool:
    """Master flag — ``JARVIS_ORDER2_RISK_CLASS_ENABLED`` (default
    TRUE post Q4 Priority #3 graduation, 2026-05-02).

    Operator-authorized graduation: when an op's target_files match
    a manifest entry, the risk-tier floor elevates to
    ``ORDER_2_GOVERNANCE`` (above ``BLOCKED``). This DOES NOT
    permit Order-2 mutations — the only path is operator approval
    via ``/order2 amend`` (gated by ``JARVIS_ORDER2_REPL_ENABLED``,
    still default-false). What graduating Slice 2 enables: the
    classifier elevates governance-path ops to the highest tier;
    autonomous attempts to modify governance code are structurally
    blocked at the gate.

    Note: there are TWO independent flags in the Pass B revert
    matrix:
      1. ``JARVIS_ORDER2_MANIFEST_LOADED`` (Slice 1) — when off, the
         manifest is empty so :func:`classify_order2_match` returns
         False before this function is even called.
      2. ``JARVIS_ORDER2_RISK_CLASS_ENABLED`` (Slice 2, this module)
         — when off, this function returns input tier unchanged
         even if the manifest is loaded AND classifier matches.

    Either flag off → no behaviour change. Both must be on for the
    Order-2 risk class to fire. Hot-revert: single env knob
    (``JARVIS_ORDER2_RISK_CLASS_ENABLED=false``)."""
    raw = os.environ.get(
        "JARVIS_ORDER2_RISK_CLASS_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated 2026-05-02
    return raw in _TRUTHY


# ---------------------------------------------------------------------------
# Pure classifier
# ---------------------------------------------------------------------------


def classify_order2_match(
    target_files: Sequence[str],
    repo: str = "jarvis",
    manifest: Optional[Order2Manifest] = None,
) -> bool:
    """Return True iff any path in ``target_files`` matches a manifest
    entry for ``repo``.

    Pure data. No state mutation, no I/O. Multi-file aware: the GATE
    classifier-hook (Pass B §4.2) iterates a candidate's full file
    set so a multi-file op touching even ONE governance path is
    classified Order-2.

    Defensive contract:
      * Empty / None ``target_files`` → False (no files = no match).
      * Manifest with status != LOADED → False (no enforcement when
        the manifest itself is missing/disabled/malformed).
      * The function NEVER raises — every ``Order2Manifest`` operation
        is already best-effort by Slice 1 contract.
    """
    if not target_files:
        return False
    m = manifest if manifest is not None else get_default_manifest()
    if m.status is not ManifestLoadStatus.LOADED:
        return False
    for path in target_files:
        if not isinstance(path, str) or not path:
            continue
        if m.matches(repo, path):
            return True
    return False


# ---------------------------------------------------------------------------
# Risk-floor application
# ---------------------------------------------------------------------------


def apply_order2_floor(
    current_tier: RiskTier,
    target_files: Sequence[str],
    repo: str = "jarvis",
    *,
    manifest: Optional[Order2Manifest] = None,
) -> RiskTier:
    """Apply the Order-2 risk floor to ``current_tier``.

    Returns ``RiskTier.ORDER_2_GOVERNANCE`` when:
      1. ``JARVIS_ORDER2_RISK_CLASS_ENABLED`` is truthy (master flag on).
      2. ``classify_order2_match(...)`` returns True for the candidate.

    Else returns ``current_tier`` unchanged.

    Per Pass B §4.2: this composition runs **after** the existing
    ``risk_tier_floor`` composition (the ``JARVIS_MIN_RISK_TIER`` /
    ``JARVIS_PARANOIA_MODE`` / quiet-hours stack). Order-2 always wins
    when its conditions are met — no other knob can lower an Order-2
    op below itself. This is the "strictest wins, with Order-2
    strictly above all" property.

    Slice 2b (or Slice 5 MetaPhaseRunner) wires this call into
    ``phase_runners/gate_runner.py``; this module ships the function
    only."""
    if not is_enabled():
        return current_tier
    if classify_order2_match(target_files, repo, manifest):
        # Telemetry: pinned to make Slice 2 graduation evidence
        # observable in session logs.
        logger.info(
            "[Order2RiskClass] target_files=%d repo=%s -> "
            "ORDER_2_GOVERNANCE (was %s)",
            len(target_files), repo, current_tier.name,
        )
        return RiskTier.ORDER_2_GOVERNANCE
    return current_tier


__all__ = [
    "apply_order2_floor",
    "classify_order2_match",
    "is_enabled",
]
