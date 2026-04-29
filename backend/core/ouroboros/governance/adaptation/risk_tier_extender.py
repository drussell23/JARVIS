"""RR Pass C Slice 4b — Risk-tier ladder extension on novel attack surfaces.

Per `memory/project_reverse_russian_doll_pass_c.md` §8.2:

  > Analyzes POSTMORTEM events for `failure_class` values that
  > don't fit cleanly into the current 4-tier ladder. For each
  > unknown class with `count >= JARVIS_ADAPTATION_TIER_THRESHOLD`
  > (default 5), determine the appropriate ladder slot (heuristic:
  > severity score from `blast_radius` field) → propose insertion
  > between two existing tiers.

This is the second sub-surface of Slice 4 (paired with
per_order_mutation_budget.py). Per §8.3 the extension is strictly
additive: inserting a new tier between two existing ones tightens
(an op that previously got tier X might now match the new tier
between X and X+1). Existing tier behavior is preserved for ops
not matching the new class.

## Why insertion (not replacement)

Replacement loosens — that's a Pass B amendment. Insertion is
strictly a tightening operation: an op that previously matched
tier X may now match the new tier_X' between X and X+1 (which is
strictly more strict than X), but no op that didn't match X can
suddenly match the new tier. The ladder grows; nothing on it is
removed.

## Activation path (Slice 6 wires this)

Approved tier extensions land in `.jarvis/adapted_risk_tiers.yaml`.
At boot, `risk_tier_floor.py` reads this file and merges new tiers
into the canonical ladder enum. (This module ships the proposer;
risk_tier_floor wiring is a follow-up.)

## Authority surface

  * Pure function over caller-supplied `PostmortemEventLite` lists.
    NOTE: same input shape as Slice 2's miner — Slice 6 MetaGovernor
    will share the postmortem reader across both surfaces.
  * Writes via `AdaptationLedger.propose()` only.
  * No subprocess, no env mutation, no network.
  * Stdlib-only (plus the Slice 1 substrate).
  * Auto-registers a per-surface validator at module-import.

## Default-off

`JARVIS_ADAPTIVE_RISK_TIER_LADDER_ENABLED` (default false).
"""
from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from typing import (
    Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple,
)

from backend.core.ouroboros.governance.adaptation.ledger import (
    AdaptationEvidence,
    AdaptationLedger,
    AdaptationProposal,
    AdaptationSurface,
    ProposeResult,
    ProposeStatus,
    register_surface_validator,
)

logger = logging.getLogger(__name__)


_TRUTHY = ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


# Per §8.5 default 5.
DEFAULT_TIER_THRESHOLD: int = 5

# Adaptation window in days.
DEFAULT_WINDOW_DAYS: int = 7

# The current 4-tier ladder, in increasing strictness order. Caller
# can override via `current_tiers=` kwarg if the ladder evolves.
DEFAULT_LADDER: Tuple[str, ...] = (
    "SAFE_AUTO",
    "NOTIFY_APPLY",
    "APPROVAL_REQUIRED",
    "BLOCKED",
)

# Known failure_class → tier mapping. Caller can override. Failure
# classes NOT in this set are "novel" and candidates for ladder
# extension when their count crosses threshold.
DEFAULT_KNOWN_FAILURE_CLASSES: FrozenSet[str] = frozenset({
    "infra",            # not policy-relevant — handled outside the ladder
    "test",             # NOTIFY_APPLY band
    "code",             # NOTIFY_APPLY band
    "approval_denied",  # APPROVAL_REQUIRED band
    "blocked",          # BLOCKED band
})

# Bound on synthesized tier name length.
MAX_TIER_NAME_CHARS: int = 64


def get_tier_threshold() -> int:
    raw = os.environ.get("JARVIS_ADAPTATION_TIER_THRESHOLD")
    if raw is None:
        return DEFAULT_TIER_THRESHOLD
    try:
        v = int(raw)
        return v if v >= 1 else DEFAULT_TIER_THRESHOLD
    except ValueError:
        return DEFAULT_TIER_THRESHOLD


def get_window_days() -> int:
    raw = os.environ.get("JARVIS_ADAPTATION_WINDOW_DAYS")
    if raw is None:
        return DEFAULT_WINDOW_DAYS
    try:
        v = int(raw)
        return v if v >= 1 else DEFAULT_WINDOW_DAYS
    except ValueError:
        return DEFAULT_WINDOW_DAYS


def is_enabled() -> bool:
    """Master flag — ``JARVIS_ADAPTIVE_RISK_TIER_LADDER_ENABLED``
    (default ``true`` — graduated in Move 1 Pass C cadence 2026-04-29).

    Asymmetric env semantics — empty/whitespace = unset = graduated
    default-true; explicit truthy enables; explicit falsy hot-reverts."""
    raw = os.environ.get(
        "JARVIS_ADAPTIVE_RISK_TIER_LADDER_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated default (Move 1 Pass C cadence)
    return raw in _TRUTHY


# ---------------------------------------------------------------------------
# Event input shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PostmortemEventLite:
    """Minimal shape — same fields as Slice 2's miner consumes,
    plus `blast_radius` for the severity heuristic.

    The `blast_radius` field is a 0.0–1.0 score the orchestrator
    computes per failure (number of files touched / verify-ops
    impacted / etc.). Higher = more severe → higher tier slot.
    """

    op_id: str
    failure_class: str
    blast_radius: float = 0.0
    timestamp_unix: float = 0.0


@dataclass(frozen=True)
class MinedTierExtension:
    """Pre-substrate result. The extender produces these; then
    `propose_tier_extensions_from_events()` lifts each into an
    `AdaptationProposal`."""

    failure_class: str
    proposed_tier_name: str   # synthesized name e.g. "NOTIFY_APPLY_HARDENED"
    insert_after_tier: str    # the existing tier this slots ABOVE
    insert_before_tier: str   # the existing tier this slots BELOW
    avg_blast_radius: float
    occurrence_count: int
    source_event_ids: Tuple[str, ...]
    summary: str

    def proposal_id(self) -> str:
        h = hashlib.sha256()
        h.update(self.failure_class.encode("utf-8"))
        h.update(b"|")
        h.update(self.proposed_tier_name.encode("utf-8"))
        return f"adapt-rt-{h.hexdigest()[:24]}"

    def proposed_state_hash(self, current_state_hash: str) -> str:
        h = hashlib.sha256()
        h.update((current_state_hash or "").encode("utf-8"))
        h.update(b"|+|")
        h.update(self.proposed_tier_name.encode("utf-8"))
        h.update(b":")
        h.update(self.insert_after_tier.encode("utf-8"))
        return f"sha256:{h.hexdigest()}"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _filter_window(
    events: Iterable[PostmortemEventLite],
    *,
    now_unix: float,
    window_days: int,
) -> List[PostmortemEventLite]:
    if window_days <= 0:
        return list(events)
    cutoff = now_unix - (window_days * 86_400)
    return [
        e for e in events
        if e.timestamp_unix == 0.0 or e.timestamp_unix >= cutoff
    ]


def _group_novel_failure_classes(
    events: Sequence[PostmortemEventLite],
    *,
    known_classes: FrozenSet[str],
) -> Dict[str, List[PostmortemEventLite]]:
    """Group events by `failure_class`, retaining ONLY classes that
    are not in `known_classes`. Empty failure_class is skipped."""
    out: Dict[str, List[PostmortemEventLite]] = {}
    for e in events:
        fc = (e.failure_class or "").strip()
        if not fc:
            continue
        if fc in known_classes:
            continue
        out.setdefault(fc, []).append(e)
    return out


def _classify_blast_radius_band(
    avg_blast_radius: float,
) -> Tuple[str, str, str]:
    """Heuristic per §8.2: severity score from `blast_radius` →
    insertion slot.

    Returns (insert_after, insert_before, suffix_label):
      * 0.0–0.25 → after SAFE_AUTO, before NOTIFY_APPLY (suffix HARDENED)
      * 0.25–0.5 → after NOTIFY_APPLY, before APPROVAL_REQUIRED (HARDENED)
      * 0.5–0.75 → after APPROVAL_REQUIRED, before BLOCKED (HARDENED)
      * 0.75+ → after APPROVAL_REQUIRED, before BLOCKED (CRITICAL)
        (BLOCKED is already terminal; we never propose inserting
        above BLOCKED — that would be a non-actionable tier.)
    """
    if avg_blast_radius < 0.25:
        return ("SAFE_AUTO", "NOTIFY_APPLY", "HARDENED")
    if avg_blast_radius < 0.5:
        return ("NOTIFY_APPLY", "APPROVAL_REQUIRED", "HARDENED")
    if avg_blast_radius < 0.75:
        return ("APPROVAL_REQUIRED", "BLOCKED", "HARDENED")
    return ("APPROVAL_REQUIRED", "BLOCKED", "CRITICAL")


def _synthesize_tier_name(
    failure_class: str,
    insert_after: str,
    suffix_label: str,
) -> str:
    """Synthesize a deterministic tier name. Format:
    ``<insert_after>_<SUFFIX_LABEL>_<FAILURE_CLASS>`` truncated to
    MAX_TIER_NAME_CHARS, all uppercase."""
    fc_token = failure_class.upper().replace(" ", "_").replace("-", "_")
    # Strip non-alphanumeric chars (deterministic name sanitization)
    fc_token = "".join(
        ch if ch.isalnum() or ch == "_" else "_"
        for ch in fc_token
    )
    name = f"{insert_after}_{suffix_label}_{fc_token}"
    return name[:MAX_TIER_NAME_CHARS]


# ---------------------------------------------------------------------------
# Public mining pipeline
# ---------------------------------------------------------------------------


def mine_tier_extensions_from_events(
    events: Iterable[PostmortemEventLite],
    *,
    known_classes: FrozenSet[str] = DEFAULT_KNOWN_FAILURE_CLASSES,
    threshold: Optional[int] = None,
    window_days: Optional[int] = None,
    now_unix: float = 0.0,
) -> List[MinedTierExtension]:
    """Pure function. Returns one MinedTierExtension per novel
    failure_class whose count >= threshold."""
    th = threshold if threshold is not None else get_tier_threshold()
    wd = window_days if window_days is not None else get_window_days()
    in_window = _filter_window(events, now_unix=now_unix, window_days=wd)
    grouped = _group_novel_failure_classes(
        in_window, known_classes=known_classes,
    )
    out: List[MinedTierExtension] = []
    for fc, evts in grouped.items():
        if len(evts) < th:
            continue
        avg_br = sum(e.blast_radius for e in evts) / len(evts)
        insert_after, insert_before, suffix = _classify_blast_radius_band(
            avg_br,
        )
        tier_name = _synthesize_tier_name(fc, insert_after, suffix)
        ev_ids = tuple(e.op_id for e in evts if e.op_id)
        summary = (
            f"Novel failure_class={fc!r} observed in {len(evts)} ops "
            f"in last {wd}d window (avg blast_radius={avg_br:.2f}). "
            f"Proposed new tier {tier_name!r} → insert between "
            f"{insert_after} and {insert_before}."
        )
        out.append(MinedTierExtension(
            failure_class=fc,
            proposed_tier_name=tier_name,
            insert_after_tier=insert_after,
            insert_before_tier=insert_before,
            avg_blast_radius=avg_br,
            occurrence_count=len(evts),
            source_event_ids=ev_ids,
            summary=summary,
        ))
    return out


def propose_tier_extensions_from_events(
    events: Iterable[PostmortemEventLite],
    *,
    ledger: AdaptationLedger,
    known_classes: FrozenSet[str] = DEFAULT_KNOWN_FAILURE_CLASSES,
    current_state_hash: str = "",
    threshold: Optional[int] = None,
    window_days: Optional[int] = None,
    now_unix: float = 0.0,
) -> List[ProposeResult]:
    """End-to-end."""
    if not is_enabled():
        return []
    candidates = mine_tier_extensions_from_events(
        events,
        known_classes=known_classes,
        threshold=threshold,
        window_days=window_days,
        now_unix=now_unix,
    )
    wd = window_days if window_days is not None else get_window_days()
    results: List[ProposeResult] = []
    for c in candidates:
        evidence = AdaptationEvidence(
            window_days=wd,
            observation_count=c.occurrence_count,
            source_event_ids=c.source_event_ids,
            summary=c.summary,
        )
        proposed_hash = c.proposed_state_hash(current_state_hash)
        # Mining-surface payload (Item #2 yaml_writer schema):
        # `tiers: [{tier_name, insert_after, failure_class, ...prov}]`.
        # Loader validates tier_name + insert_after match [A-Z0-9_]+
        # charset (Slice 4b miner already produces uppercase output).
        # Provenance auto-enriched by yaml_writer.
        payload = {
            "tier_name": c.proposed_tier_name,
            "insert_after": c.insert_after_tier,
            "failure_class": c.failure_class,
        }
        res = ledger.propose(
            proposal_id=c.proposal_id(),
            surface=AdaptationSurface.RISK_TIER_FLOOR_TIERS,
            proposal_kind="add_tier",
            evidence=evidence,
            current_state_hash=current_state_hash or "sha256:initial",
            proposed_state_hash=proposed_hash,
            proposed_state_payload=payload,
        )
        results.append(res)
        if res.status is ProposeStatus.OK:
            logger.info(
                "[RiskTierExtender] proposed new tier=%s "
                "(failure_class=%s, between %s and %s) proposal_id=%s",
                c.proposed_tier_name, c.failure_class,
                c.insert_after_tier, c.insert_before_tier,
                res.proposal_id,
            )
    return results


# ---------------------------------------------------------------------------
# Surface validator
# ---------------------------------------------------------------------------


def _risk_tier_validator(
    proposal: AdaptationProposal,
) -> Tuple[bool, str]:
    """Per-surface validator (Pass C §4.1).

    Asserts:
      * proposal_kind MUST be "add_tier" (the universal allowlist
        also includes add_tier; this surface enforces it strictly).
      * proposed_state_hash sha256-prefixed.
      * observation_count >= cage's threshold floor.
      * Summary mentions "insert" or "between" (sanity check on
        direction indicator — defends against doctored proposals).
    """
    if proposal.proposal_kind != "add_tier":
        return (
            False,
            f"risk_tier_kind_must_be_add_tier:{proposal.proposal_kind}",
        )
    if not proposal.proposed_state_hash.startswith("sha256:"):
        return (
            False,
            f"risk_tier_proposed_hash_format:{proposal.proposed_state_hash[:32]}",
        )
    th = get_tier_threshold()
    if proposal.evidence.observation_count < th:
        return (
            False,
            f"risk_tier_observation_count_below_threshold:"
            f"{proposal.evidence.observation_count} < {th}",
        )
    summary = proposal.evidence.summary
    if "insert between" not in summary and "between" not in summary:
        return (
            False,
            "risk_tier_summary_missing_insert_indicator",
        )
    return (True, "risk_tier_extension_ok")


def install_surface_validator() -> None:
    register_surface_validator(
        AdaptationSurface.RISK_TIER_FLOOR_TIERS,
        _risk_tier_validator,
    )


install_surface_validator()


__all__ = [
    "DEFAULT_KNOWN_FAILURE_CLASSES",
    "DEFAULT_LADDER",
    "DEFAULT_TIER_THRESHOLD",
    "DEFAULT_WINDOW_DAYS",
    "MAX_TIER_NAME_CHARS",
    "MinedTierExtension",
    "PostmortemEventLite",
    "get_tier_threshold",
    "get_window_days",
    "install_surface_validator",
    "is_enabled",
    "mine_tier_extensions_from_events",
    "propose_tier_extensions_from_events",
]
