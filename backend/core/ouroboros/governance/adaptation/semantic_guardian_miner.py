"""RR Pass C Slice 2 — SemanticGuardian POSTMORTEM-mined patterns.

Per `memory/project_reverse_russian_doll_pass_c.md` §6:

  > A scheduled background analyzer that runs at the adaptation
  > window cadence. For each POSTMORTEM event in the window:
  >   1. Extract `root_cause`, `failure_class`, `error_type`, and
  >      the specific `code_snippet_excerpt` field if present.
  >   2. Group events by `(root_cause, failure_class)` tuple.
  >   3. For each group with `count >= JARVIS_ADAPTATION_PATTERN_
  >      THRESHOLD`: synthesize a candidate detector via
  >      longest-common-substring (regex case), verify the
  >      candidate doesn't already match any existing
  >      SemanticGuardian pattern, emit AdaptationProposal.

This module is the **miner**: deterministic, stdlib-only, zero-LLM.
It does NOT activate detectors — approved patterns flow through the
operator-only `/adapt approve` REPL (Slice 6) before they land in
`.jarvis/adapted_guardian_patterns.yaml` (which `SemanticGuardian`
loads at next boot, additively, alongside its hand-written set).

## Why deterministic, not LLM-based

Per §6.2: pattern synthesis uses **longest common substring**. This
is a stdlib operation. An LLM-based synthesizer would be more
powerful but breaches the §4.4 zero-LLM-in-cage invariant. If LCS
proves too narrow, the operator can extend the synthesizer module
via a normal Pass B Order-2 amendment — it IS governance code.

## Per-method composition with Slice 1

This module imports the Slice 1 substrate (`AdaptationLedger` +
`AdaptationProposal` + `AdaptationSurface` + `register_surface_
validator`). It registers a per-surface validator at module-import
that enforces:

  1. proposal_kind == "add_pattern" (else not additive)
  2. proposed_state_hash != current_state_hash (substrate already
     enforces this; redundant by design)
  3. evidence.observation_count >= JARVIS_ADAPTATION_PATTERN_THRESHOLD
     (the threshold the miner used MUST be at least the cage's floor)

The substrate's universal `validate_monotonic_tightening()` runs
the default check FIRST (kind in allowlist, hashes distinct), then
this validator. Both must pass.

## Authority surface

  * Read-only over caller-supplied event list. The miner does NOT
    open postmortem files itself — that's caller-responsibility
    (Slice 6 MetaGovernor will wire the source). Keeps the miner
    pure + unit-testable.
  * Writes via `AdaptationLedger.propose()` — the substrate handles
    the actual JSONL append + monotonic-tightening check.
  * No subprocess, no env mutation, no network.
  * Stdlib-only (plus the Slice 1 substrate).
  * Not imported by orchestrator / iron_gate / change_engine /
    candidate_generator / risk_tier_floor / semantic_guardian /
    semantic_firewall / scoped_tool_backend.

## Default-off

`JARVIS_ADAPTIVE_SEMANTIC_GUARDIAN_ENABLED` (default false). When
off, `mine_patterns_from_events()` short-circuits to an empty list +
the surface validator is registered but has no effect (substrate
gates everything on `JARVIS_ADAPTATION_LEDGER_ENABLED` first).
"""
from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

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
# Constants (env-overridable)
# ---------------------------------------------------------------------------


# Minimum group size to propose a new detector. Per §6.4 default 3:
# small enough to surface real patterns; large enough to filter
# one-off operational noise.
DEFAULT_PATTERN_THRESHOLD: int = 3

# Adaptation analysis window in days. Per §4.3 default 7. Shared
# across all Slice 2-5 mining surfaces.
DEFAULT_WINDOW_DAYS: int = 7

# Hard cap on excerpts considered per group (bounded LCS cost).
MAX_EXCERPTS_PER_GROUP: int = 32

# Hard cap on synthesized pattern length. Defends against an LCS
# producing a multi-KB regex that nobody can review.
MAX_SYNTHESIZED_PATTERN_CHARS: int = 256

# Floor on synthesized pattern length. Sub-3-char patterns would
# match anything (e.g., "if" / "{" / "()") and produce useless
# false positives.
MIN_SYNTHESIZED_PATTERN_CHARS: int = 8

# Floor on shared substring length when synthesizing. Below this,
# the LCS is structurally noise (every Python file has "def" and
# "return" — those are not detector patterns).
MIN_LCS_LENGTH: int = 8


def get_pattern_threshold() -> int:
    """Threshold env-overridable via JARVIS_ADAPTATION_PATTERN_THRESHOLD."""
    raw = os.environ.get("JARVIS_ADAPTATION_PATTERN_THRESHOLD")
    if raw is None:
        return DEFAULT_PATTERN_THRESHOLD
    try:
        v = int(raw)
        return v if v >= 1 else DEFAULT_PATTERN_THRESHOLD
    except ValueError:
        return DEFAULT_PATTERN_THRESHOLD


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
    """Master flag — ``JARVIS_ADAPTIVE_SEMANTIC_GUARDIAN_ENABLED``
    (default ``true`` — graduated in Move 1 Pass C cadence 2026-04-29).

    Asymmetric env semantics — empty/whitespace = unset = graduated
    default-true; explicit truthy enables; explicit falsy hot-reverts."""
    raw = os.environ.get(
        "JARVIS_ADAPTIVE_SEMANTIC_GUARDIAN_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated default (Move 1 Pass C cadence)
    return raw in _TRUTHY


# ---------------------------------------------------------------------------
# Event input shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PostmortemEventLite:
    """Minimal shape the miner consumes. Caller (Slice 6 MetaGovernor
    or test) supplies a list of these. The miner does NOT read
    postmortem files itself — that's deliberate per the §6 design.

    Field semantics (all bounded strings):
      * `op_id`: identifies the source op for evidence.source_event_ids.
      * `root_cause` + `failure_class`: the grouping key.
      * `error_type`: refined sub-class (e.g. ``ValueError`` /
        ``KeyError``) — included in synthesized pattern context.
      * `code_snippet_excerpt`: the structural hint LCS operates on.
        When empty, the event is grouped but contributes nothing
        to pattern synthesis.
      * `timestamp_unix`: window filter input.
    """

    op_id: str
    root_cause: str
    failure_class: str
    error_type: str = ""
    code_snippet_excerpt: str = ""
    timestamp_unix: float = 0.0


@dataclass(frozen=True)
class MinedPatternProposal:
    """Internal pre-substrate result. The miner produces these; then
    `propose_patterns_from_events()` lifts each into an
    `AdaptationProposal` via the ledger."""

    group_key: Tuple[str, str]   # (root_cause, failure_class)
    proposed_pattern: str         # synthesized regex string
    excerpt_count: int            # group size that justified the proposal
    source_event_ids: Tuple[str, ...]
    summary: str

    def proposal_id(self) -> str:
        """Stable id keyed on the group + pattern content. Re-mining
        the same group with the same pattern yields the same id —
        the substrate's DUPLICATE_PROPOSAL_ID gate then kicks in
        for idempotency."""
        h = hashlib.sha256()
        h.update(self.group_key[0].encode("utf-8"))
        h.update(b"|")
        h.update(self.group_key[1].encode("utf-8"))
        h.update(b"|")
        h.update(self.proposed_pattern.encode("utf-8"))
        return f"adapt-sg-{h.hexdigest()[:24]}"

    def proposed_state_hash(self, current_state_hash: str) -> str:
        """sha256(current + new_pattern). Substrate compares hashes
        for distinctness; this keeps the proposed hash deterministic."""
        h = hashlib.sha256()
        h.update((current_state_hash or "").encode("utf-8"))
        h.update(b"|+|")
        h.update(self.proposed_pattern.encode("utf-8"))
        return f"sha256:{h.hexdigest()}"


# ---------------------------------------------------------------------------
# Internal helpers — deterministic synthesizers
# ---------------------------------------------------------------------------


def _filter_window(
    events: Iterable[PostmortemEventLite],
    *,
    now_unix: float,
    window_days: int,
) -> List[PostmortemEventLite]:
    """Keep events with `timestamp_unix >= now_unix - window_days*86400`.
    Events with `timestamp_unix == 0.0` are kept (back-compat: tests
    + boot-time scans without a clock can still mine)."""
    if window_days <= 0:
        return list(events)
    cutoff = now_unix - (window_days * 86_400)
    out: List[PostmortemEventLite] = []
    for e in events:
        if e.timestamp_unix == 0.0 or e.timestamp_unix >= cutoff:
            out.append(e)
    return out


def _group_by_signature(
    events: Sequence[PostmortemEventLite],
) -> Dict[Tuple[str, str], List[PostmortemEventLite]]:
    """Group events by `(root_cause, failure_class)`. Empty
    root_cause / failure_class are skipped (no signal)."""
    groups: Dict[Tuple[str, str], List[PostmortemEventLite]] = {}
    for e in events:
        rc = (e.root_cause or "").strip()
        fc = (e.failure_class or "").strip()
        if not rc or not fc:
            continue
        groups.setdefault((rc, fc), []).append(e)
    return groups


def _longest_common_substring(strings: Sequence[str]) -> str:
    """Return the longest substring shared by every input string.
    Stdlib-only. Returns empty string on no commonality or empty
    input. Bounded by the shortest input (typical excerpt is a few
    hundred chars; LCS over 32 such strings is microseconds)."""
    cleaned = [s for s in strings if s]
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return cleaned[0][:MAX_SYNTHESIZED_PATTERN_CHARS]
    # Start from the shortest as the substring source — every
    # candidate substring of the answer must be a substring of the
    # shortest input.
    cleaned.sort(key=len)
    base = cleaned[0]
    others = cleaned[1:]
    longest = ""
    n = len(base)
    # Bound the inner loop by the cap so a multi-KB excerpt doesn't
    # turn into an O(n^3) walk.
    cap = min(n, MAX_SYNTHESIZED_PATTERN_CHARS)
    for i in range(n):
        for j in range(i + len(longest) + 1, min(n, i + cap) + 1):
            candidate = base[i:j]
            if all(candidate in other for other in others):
                if len(candidate) > len(longest):
                    longest = candidate
            else:
                break
    return longest


def _synthesize_pattern_from_excerpts(
    excerpts: Sequence[str],
) -> str:
    """Return the synthesized detector pattern for a group, OR empty
    string when no usable pattern can be synthesized.

    Deterministic LCS pipeline:
      1. Take up to MAX_EXCERPTS_PER_GROUP.
      2. Compute longest common substring.
      3. Strip leading/trailing whitespace.
      4. Reject if shorter than MIN_LCS_LENGTH (would match anything).
      5. Reject if shorter than MIN_SYNTHESIZED_PATTERN_CHARS after
         strip.
      6. Truncate at MAX_SYNTHESIZED_PATTERN_CHARS.
    """
    bounded = [
        e for e in excerpts[:MAX_EXCERPTS_PER_GROUP]
        if e and e.strip()
    ]
    if not bounded:
        return ""
    lcs = _longest_common_substring(bounded)
    lcs = lcs.strip()
    if len(lcs) < MIN_LCS_LENGTH:
        return ""
    if len(lcs) < MIN_SYNTHESIZED_PATTERN_CHARS:
        return ""
    return lcs[:MAX_SYNTHESIZED_PATTERN_CHARS]


def _pattern_already_exists(
    candidate: str,
    existing_patterns: Sequence[str],
) -> bool:
    """True iff `candidate` is structurally redundant with an
    existing detector pattern.

    Conservative check — we treat the candidate as redundant if:
      * It is a substring of any existing pattern (the existing one
        already catches a superset), OR
      * Any existing pattern is a substring of the candidate (the
        candidate would shadow / duplicate the existing one).

    This prevents the LCS from proposing patterns that any current
    detector already trips on. Per §6.3: adapted patterns are
    additive, never substitutive — a candidate that overlaps an
    existing pattern is not added (let the existing one handle it)."""
    if not candidate:
        return True
    for p in existing_patterns:
        if not p:
            continue
        if candidate in p or p in candidate:
            return True
    return False


# ---------------------------------------------------------------------------
# Public mining pipeline
# ---------------------------------------------------------------------------


def mine_patterns_from_events(
    events: Iterable[PostmortemEventLite],
    *,
    existing_patterns: Sequence[str] = (),
    threshold: Optional[int] = None,
    window_days: Optional[int] = None,
    now_unix: float = 0.0,
) -> List[MinedPatternProposal]:
    """Group events by `(root_cause, failure_class)`, synthesize a
    detector candidate per group whose count >= threshold, and
    return the candidates that don't already shadow existing
    patterns.

    Pure function. Master-flag-disabled callers can short-circuit
    via `is_enabled()` before calling — this function does NOT
    short-circuit on the env (kept testable). The substrate +
    factory layer enforce the master flag.
    """
    th = threshold if threshold is not None else get_pattern_threshold()
    wd = window_days if window_days is not None else get_window_days()

    in_window = _filter_window(
        events, now_unix=now_unix, window_days=wd,
    )
    groups = _group_by_signature(in_window)
    out: List[MinedPatternProposal] = []
    for (rc, fc), group_events in groups.items():
        if len(group_events) < th:
            continue
        excerpts = [e.code_snippet_excerpt for e in group_events]
        pattern = _synthesize_pattern_from_excerpts(excerpts)
        if not pattern:
            continue
        if _pattern_already_exists(pattern, existing_patterns):
            logger.debug(
                "[SemanticGuardianMiner] candidate pattern shadows "
                "existing detector — skip group=(%s, %s)",
                rc[:60], fc[:60],
            )
            continue
        ev_ids = tuple(e.op_id for e in group_events if e.op_id)
        summary = (
            f"Mined from {len(group_events)} POSTMORTEM events grouped "
            f"by (root_cause={rc[:80]!r}, failure_class={fc[:40]!r}). "
            f"Longest-common-substring detector: {pattern[:80]!r}"
        )
        out.append(MinedPatternProposal(
            group_key=(rc, fc),
            proposed_pattern=pattern,
            excerpt_count=len(group_events),
            source_event_ids=ev_ids,
            summary=summary,
        ))
    return out


def propose_patterns_from_events(
    events: Iterable[PostmortemEventLite],
    *,
    ledger: AdaptationLedger,
    existing_patterns: Sequence[str] = (),
    current_state_hash: str = "",
    threshold: Optional[int] = None,
    window_days: Optional[int] = None,
    now_unix: float = 0.0,
) -> List[ProposeResult]:
    """End-to-end: mine candidates → submit each through the Slice 1
    AdaptationLedger. Returns one ProposeResult per attempted
    submission (so callers can log OK / DUPLICATE / WOULD_LOOSEN /
    etc per group).

    Master flag check happens here BEFORE any work — when the env
    is off, returns an empty list.
    """
    if not is_enabled():
        return []

    candidates = mine_patterns_from_events(
        events,
        existing_patterns=existing_patterns,
        threshold=threshold,
        window_days=window_days,
        now_unix=now_unix,
    )
    wd = window_days if window_days is not None else get_window_days()

    results: List[ProposeResult] = []
    for c in candidates:
        evidence = AdaptationEvidence(
            window_days=wd,
            observation_count=c.excerpt_count,
            source_event_ids=c.source_event_ids,
            summary=c.summary,
        )
        proposed_hash = c.proposed_state_hash(current_state_hash)
        # Mining-surface payload (closes the producer-side gap end-to-
        # end with Item #2 yaml_writer + Item #3 bridges). Shape MUST
        # match yaml_writer's SEMANTIC_GUARDIAN_PATTERNS schema:
        # `patterns: [{name, regex, severity, message, ...prov}]`.
        # Provenance fields (proposal_id / approved_at / approved_by)
        # are added by yaml_writer's `_enrich_with_provenance()`.
        payload = {
            "name": f"adapted_{c.group_key[0]}_{c.group_key[1]}"[:240],
            "regex": c.proposed_pattern,
            "severity": "warn",
            "message": (
                f"Adapted SemanticGuardian pattern from POSTMORTEM "
                f"cluster ({c.group_key[0]}, {c.group_key[1]}); "
                f"observations={c.excerpt_count}"
            )[:240],
        }
        res = ledger.propose(
            proposal_id=c.proposal_id(),
            surface=AdaptationSurface.SEMANTIC_GUARDIAN_PATTERNS,
            proposal_kind="add_pattern",
            evidence=evidence,
            current_state_hash=current_state_hash or "sha256:initial",
            proposed_state_hash=proposed_hash,
            proposed_state_payload=payload,
        )
        results.append(res)
        if res.status is ProposeStatus.OK:
            logger.info(
                "[SemanticGuardianMiner] proposed pattern for group="
                "(%s, %s) observations=%d proposal_id=%s",
                c.group_key[0][:60], c.group_key[1][:60],
                c.excerpt_count, res.proposal_id,
            )
        elif res.status is ProposeStatus.DUPLICATE_PROPOSAL_ID:
            logger.debug(
                "[SemanticGuardianMiner] duplicate proposal_id — "
                "miner is idempotent; skip group=(%s, %s)",
                c.group_key[0][:60], c.group_key[1][:60],
            )
        else:
            logger.info(
                "[SemanticGuardianMiner] propose returned %s for "
                "group=(%s, %s) detail=%s",
                res.status.value,
                c.group_key[0][:60], c.group_key[1][:60],
                res.detail,
            )
    return results


# ---------------------------------------------------------------------------
# Surface validator — enforces "additive only" semantic
# ---------------------------------------------------------------------------


def _semantic_guardian_validator(
    proposal: AdaptationProposal,
) -> Tuple[bool, str]:
    """Per-surface validator (Pass C §4.1 surface-specific layer).

    Asserts the proposal is genuinely additive:
      * proposal_kind MUST be "add_pattern" (substrate already
        checks the universal allowlist; this is the strict-form
        check for THIS surface).
      * proposed_state_hash MUST start with "sha256:" (the
        miner-synthesized hash format) — defends against a
        non-standard caller emitting a malformed hash.
      * observation_count MUST be >= the cage's threshold floor
        (the miner uses the env threshold; an attacker who built
        their own proposal under the threshold would be blocked).
    """
    if proposal.proposal_kind != "add_pattern":
        return (False, f"semantic_guardian_kind_must_be_add_pattern:{proposal.proposal_kind}")
    if not proposal.proposed_state_hash.startswith("sha256:"):
        return (False, f"semantic_guardian_proposed_hash_format:{proposal.proposed_state_hash[:32]}")
    th = get_pattern_threshold()
    if proposal.evidence.observation_count < th:
        return (
            False,
            f"semantic_guardian_observation_count_below_threshold:"
            f"{proposal.evidence.observation_count} < {th}",
        )
    return (True, "semantic_guardian_additive_ok")


def install_surface_validator() -> None:
    """Register the surface validator with the Slice 1 substrate.

    Idempotent — re-registration is a no-op (last-write-wins). Safe
    to call from module-import or from tests.
    """
    register_surface_validator(
        AdaptationSurface.SEMANTIC_GUARDIAN_PATTERNS,
        _semantic_guardian_validator,
    )


# Auto-register at module import. Tests can call
# `reset_surface_validators()` to undo.
install_surface_validator()


__all__ = [
    "DEFAULT_PATTERN_THRESHOLD",
    "DEFAULT_WINDOW_DAYS",
    "MAX_EXCERPTS_PER_GROUP",
    "MAX_SYNTHESIZED_PATTERN_CHARS",
    "MIN_LCS_LENGTH",
    "MIN_SYNTHESIZED_PATTERN_CHARS",
    "MinedPatternProposal",
    "PostmortemEventLite",
    "get_pattern_threshold",
    "get_window_days",
    "install_surface_validator",
    "is_enabled",
    "mine_patterns_from_events",
    "propose_patterns_from_events",
]
