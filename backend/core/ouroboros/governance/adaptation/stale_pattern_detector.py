"""Phase 7.9 — stale-pattern sunset signal (closes §3.6.2 vector #4).

Per `OUROBOROS_VENOM_PRD.md` §9 P7.9:

  > Mined SemanticGuardian patterns are additive forever; no signal
  > when a pattern hasn't matched anything in N days.
  > Solution: `StalePatternDetector` runs at adaptation window
  > cadence; for each adapted pattern, check
  > `.jarvis/semantic_guardian_match_history.jsonl` (new) for
  > last-match timestamp. If > 30 days, emit advisory
  > `/adapt sunset-candidate` signal — operator chooses whether to
  > file a Pass B `/order2 amend` to remove.

This module ships the detector primitive: pure stdlib analyzer over
caller-supplied `(adapted_patterns, match_events)` lists. Same
substrate-first composition as Slices 2-5 of Pass C — Slice 6
MetaGovernor wires the data sources at adaptation-window cadence.

## Design constraints (load-bearing)

  * **Advisory only — Pass C cannot REMOVE patterns.** Per Pass C
    §4.1 the universal cage rule is one-way tightening; removal of
    an adapted pattern is loosening and MUST go through Pass B
    `/order2 amend` (operator-authorized). The sunset signal is a
    NOTICE that surfaces "this pattern looks stale, consider
    removing"; the actual decision is operator-only. Allowed in
    `_TIGHTEN_KINDS` because the signal itself is structurally
    conservative — it suggests reducing surface area, never
    expanding it.
  * **Stdlib + adaptation.ledger only.** Same cage discipline as
    the rest of `adaptation/`. Does NOT import semantic_guardian
    (one-way: callers feed `adapted_patterns` from Phase 7.1's
    `adapted_guardian_loader.load_adapted_patterns()`).
  * **Fail-open**: every error path returns an empty list. Reader
    helper for the JSONL match-history file is best-effort
    (missing / oversized / malformed → empty).
  * **Bounded synthesis**: at most MAX_STALE_CANDIDATES_PER_CYCLE
    proposed per call (operator-review surface stays trim).

## Default-off

`JARVIS_ADAPTIVE_STALE_PATTERN_DETECTOR_ENABLED` (default false).

## Match-history JSONL schema

The file `.jarvis/semantic_guardian_match_history.jsonl`:

```jsonl
{"pattern_name": "removed_import_still_referenced", "matched_at_unix": 1714089600.0}
{"pattern_name": "credential_shape_introduced", "matched_at_unix": 1714190000.0}
```

Each row records ONE match event. Multiple rows per pattern are
allowed — the detector reduces to `{pattern_name: max(matched_at_unix)}`
internally. SemanticGuardian writes one row per pattern hit (via
a follow-up wiring; this module is the read-side substrate).

## Sunset proposal shape

  * `proposal_kind = "sunset_candidate"`
  * `proposal_id = "adapt-sunset-<sha256[:24]>"` keyed on
    pattern_name (idempotent — re-running with same stale state
    returns DUPLICATE_PROPOSAL_ID at substrate)
  * `proposed_state_hash = sha256(current_state || "|sunset|" || pattern_name)`
    — deterministically distinct from current_state to satisfy
    the universal default validator's "hash distinct" check.
  * `evidence.summary = "pattern <name> stale: last match Nd ago"`
    — must contain BOTH "stale" AND a numeric-day indicator
    (validator pin).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from backend.core.ouroboros.governance.adaptation.ledger import (
    AdaptationEvidence,
    AdaptationLedger,
    AdaptationProposal,
    AdaptationSurface,
    ProposeResult,
    SurfaceValidator,
    get_surface_validator,
    register_surface_validator,
)

logger = logging.getLogger(__name__)


_TRUTHY = ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


# Per PRD §9 P7.9 default 30 days.
DEFAULT_STALENESS_THRESHOLD_DAYS: int = 30

# Cap on the number of sunset candidates proposed per call (operator-
# review surface stays trim).
MAX_STALE_CANDIDATES_PER_CYCLE: int = 8

# Hard cap on history JSONL file size we'll attempt to load.
MAX_HISTORY_FILE_BYTES: int = 4 * 1024 * 1024

# Hard cap on history JSONL lines processed (defends against
# operator-typo creating a huge file with lines below the byte cap).
MAX_HISTORY_LINES: int = 10_000

# Min observations required for the surface validator to accept a
# sunset proposal. Conservatively low (1) — a single pattern that
# hasn't matched is sufficient evidence; the threshold-days check is
# the load-bearing one.
MIN_OBSERVATIONS_FOR_SUNSET: int = 1


def is_detector_enabled() -> bool:
    """Master flag — ``JARVIS_ADAPTIVE_STALE_PATTERN_DETECTOR_ENABLED``
    (default false until Phase 7.9 graduation)."""
    return os.environ.get(
        "JARVIS_ADAPTIVE_STALE_PATTERN_DETECTOR_ENABLED", "",
    ).strip().lower() in _TRUTHY


def get_staleness_threshold_days() -> int:
    """Env-overridable threshold —
    ``JARVIS_ADAPTATION_STALENESS_THRESHOLD_DAYS``."""
    raw = os.environ.get("JARVIS_ADAPTATION_STALENESS_THRESHOLD_DAYS")
    if raw is None:
        return DEFAULT_STALENESS_THRESHOLD_DAYS
    try:
        v = int(raw)
        return v if v >= 1 else DEFAULT_STALENESS_THRESHOLD_DAYS
    except ValueError:
        return DEFAULT_STALENESS_THRESHOLD_DAYS


def match_history_path() -> Path:
    """Return the JSONL match-history path. Env-overridable via
    ``JARVIS_SEMANTIC_GUARDIAN_MATCH_HISTORY_PATH``."""
    raw = os.environ.get("JARVIS_SEMANTIC_GUARDIAN_MATCH_HISTORY_PATH")
    if raw:
        return Path(raw)
    return Path(".jarvis") / "semantic_guardian_match_history.jsonl"


# ---------------------------------------------------------------------------
# Result shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StalePatternMatchEvent:
    """One match-history row.

    SemanticGuardian writes one of these per pattern hit (via a
    follow-up wiring); the detector reads them to compute per-
    pattern last-match timestamps.
    """

    pattern_name: str
    matched_at_unix: float


@dataclass(frozen=True)
class StalePatternCandidate:
    """One stale-pattern candidate identified by `mine_stale_candidates`."""

    pattern_name: str
    last_match_unix: float  # 0.0 if never matched
    days_since_last_match: int
    summary: str

    def proposal_id(self) -> str:
        # Idempotent — re-running the detector on the same stale state
        # produces the same proposal_id, which AdaptationLedger
        # dedupes (DUPLICATE_PROPOSAL_ID).
        h = hashlib.sha256()
        h.update(b"sunset|")
        h.update(self.pattern_name.encode("utf-8"))
        return f"adapt-sunset-{h.hexdigest()[:24]}"

    def proposed_state_hash(self, current_state_hash: str) -> str:
        # Deterministically distinct from current — satisfies the
        # universal "hash distinct" default check.
        h = hashlib.sha256()
        h.update((current_state_hash or "").encode("utf-8"))
        h.update(b"|sunset|")
        h.update(self.pattern_name.encode("utf-8"))
        return f"sha256:{h.hexdigest()}"


# ---------------------------------------------------------------------------
# Match-history JSONL reader (fail-open)
# ---------------------------------------------------------------------------


def load_match_events(
    history_path: Optional[Path] = None,
) -> List[StalePatternMatchEvent]:
    """Read the match-history JSONL and return a list of events.

    Returns empty list when:
      * File missing
      * File exceeds MAX_HISTORY_FILE_BYTES
      * File unreadable

    Per-line SKIP (logged at debug) when:
      * Malformed JSON / non-mapping / missing pattern_name /
        non-numeric matched_at_unix

    Cap: at most MAX_HISTORY_LINES events returned.

    NEVER raises into the caller.
    """
    path = (
        history_path if history_path is not None
        else match_history_path()
    )
    if not path.exists():
        logger.debug(
            "[StalePatternDetector] no match-history at %s — empty input",
            path,
        )
        return []
    try:
        size = path.stat().st_size
    except OSError as exc:
        logger.warning(
            "[StalePatternDetector] stat failed for %s: %s", path, exc,
        )
        return []
    if size > MAX_HISTORY_FILE_BYTES:
        logger.warning(
            "[StalePatternDetector] %s exceeds MAX_HISTORY_FILE_BYTES=%d "
            "(was %d) — refusing to load",
            path, MAX_HISTORY_FILE_BYTES, size,
        )
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "[StalePatternDetector] read failed for %s: %s", path, exc,
        )
        return []
    out: List[StalePatternMatchEvent] = []
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        if len(out) >= MAX_HISTORY_LINES:
            logger.warning(
                "[StalePatternDetector] %s exceeds MAX_HISTORY_LINES=%d "
                "— truncating", path, MAX_HISTORY_LINES,
            )
            break
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            logger.debug(
                "[StalePatternDetector] %s:%d malformed json — skip",
                path, line_no,
            )
            continue
        if not isinstance(obj, dict):
            continue
        pattern_name = str(obj.get("pattern_name") or "").strip()
        if not pattern_name:
            continue
        raw_matched = obj.get("matched_at_unix")
        if raw_matched is None:
            continue
        try:
            matched_at = float(raw_matched)
        except (TypeError, ValueError):
            continue
        if matched_at < 0:
            continue
        out.append(StalePatternMatchEvent(
            pattern_name=pattern_name,
            matched_at_unix=matched_at,
        ))
    return out


# ---------------------------------------------------------------------------
# Mining: identify stale candidates
# ---------------------------------------------------------------------------


def _last_match_per_pattern(
    events: Iterable[StalePatternMatchEvent],
) -> Dict[str, float]:
    """Reduce events to ``{pattern_name: max(matched_at_unix)}``."""
    out: Dict[str, float] = {}
    for e in events:
        prior = out.get(e.pattern_name)
        if prior is None or e.matched_at_unix > prior:
            out[e.pattern_name] = e.matched_at_unix
    return out


def mine_stale_candidates_from_events(
    adapted_patterns: Sequence[str],
    match_events: Sequence[StalePatternMatchEvent],
    *,
    threshold_days: Optional[int] = None,
    now_unix: Optional[float] = None,
) -> List[StalePatternCandidate]:
    """Identify adapted patterns that haven't matched in
    ``threshold_days`` days.

    Returns a list of `StalePatternCandidate`, capped at
    `MAX_STALE_CANDIDATES_PER_CYCLE`.

    Patterns NOT present in the match history are treated as
    "never matched" (last_match_unix=0.0, days_since=int.max-grade
    sentinel). They qualify as stale only if `threshold_days >= 0`
    AND there are no events for them.

    Sorting: stalest first (highest days_since_last_match), tie-
    broken alphabetically by pattern_name for determinism.
    """
    threshold = (
        threshold_days
        if threshold_days is not None
        else get_staleness_threshold_days()
    )
    now = now_unix if now_unix is not None else time.time()
    last_match = _last_match_per_pattern(match_events)
    threshold_seconds = threshold * 86_400

    candidates: List[StalePatternCandidate] = []
    for pattern in adapted_patterns:
        last = last_match.get(pattern, 0.0)
        if last == 0.0:
            # Never matched — treat as stale immediately if we have
            # any threshold > 0.
            days_since = max(threshold + 1, 365 * 100)
            summary = (
                f"pattern {pattern} stale: never matched "
                f"(threshold={threshold}d)"
            )
        else:
            elapsed = now - last
            if elapsed < threshold_seconds:
                continue
            days_since = int(elapsed // 86_400)
            summary = (
                f"pattern {pattern} stale: last match {days_since}d ago "
                f"(threshold={threshold}d)"
            )
        candidates.append(StalePatternCandidate(
            pattern_name=pattern,
            last_match_unix=last,
            days_since_last_match=days_since,
            summary=summary,
        ))

    # Sort stalest first, tie-break alpha.
    candidates.sort(
        key=lambda c: (-c.days_since_last_match, c.pattern_name),
    )
    if len(candidates) > MAX_STALE_CANDIDATES_PER_CYCLE:
        logger.info(
            "[StalePatternDetector] %d stale candidates (cap=%d) — "
            "truncating to top stalest",
            len(candidates), MAX_STALE_CANDIDATES_PER_CYCLE,
        )
        candidates = candidates[:MAX_STALE_CANDIDATES_PER_CYCLE]
    return candidates


def propose_sunset_candidates_from_events(
    adapted_patterns: Sequence[str],
    match_events: Sequence[StalePatternMatchEvent],
    *,
    current_state_hash: str = "",
    threshold_days: Optional[int] = None,
    now_unix: Optional[float] = None,
    ledger: Optional[AdaptationLedger] = None,
) -> List[ProposeResult]:
    """End-to-end pipeline: mine stale candidates, propose each one
    via `AdaptationLedger.propose()`.

    Returns a list of `ProposeResult` (one per candidate). Empty list
    when:
      * Master flag off
      * No stale candidates identified

    Best-effort — any individual propose failure is captured in the
    returned `ProposeResult` (status reflects substrate verdict);
    no exception escapes.
    """
    if not is_detector_enabled():
        return []
    candidates = mine_stale_candidates_from_events(
        adapted_patterns, match_events,
        threshold_days=threshold_days,
        now_unix=now_unix,
    )
    if not candidates:
        return []
    if ledger is None:
        from backend.core.ouroboros.governance.adaptation.ledger import (
            get_default_ledger,
        )
        ledger = get_default_ledger()
    assert ledger is not None  # for type-checker

    out: List[ProposeResult] = []
    for c in candidates:
        evidence = AdaptationEvidence(
            window_days=(
                threshold_days
                if threshold_days is not None
                else get_staleness_threshold_days()
            ),
            observation_count=MIN_OBSERVATIONS_FOR_SUNSET,
            source_event_ids=(c.pattern_name,),
            summary=c.summary,
        )
        try:
            # Sunset candidate payload: yaml_writer doesn't have a
            # dedicated "sunset" YAML schema (the sunset signal is
            # advisory — Pass C cannot REMOVE patterns; removal
            # requires Pass B /order2 amend). The payload carries the
            # pattern_name + days_since for /adapt show audit
            # rendering. yaml_writer will append into the
            # SEMANTIC_GUARDIAN_PATTERNS file but the actual loader
            # ignores entries with kind="sunset_candidate" (loader
            # only consumes add_pattern). The audit trail is the
            # value here, not behavior change.
            payload = {
                "pattern_name": c.pattern_name,
                "days_since_last_match": c.days_since_last_match,
                "last_match_unix": c.last_match_unix,
                "kind": "sunset_candidate",
            }
            result = ledger.propose(
                proposal_id=c.proposal_id(),
                surface=AdaptationSurface.SEMANTIC_GUARDIAN_PATTERNS,
                proposal_kind="sunset_candidate",
                evidence=evidence,
                current_state_hash=current_state_hash,
                proposed_state_hash=c.proposed_state_hash(
                    current_state_hash,
                ),
                proposed_state_payload=payload,
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning(
                "[StalePatternDetector] propose failed for %s: %s",
                c.pattern_name, exc,
            )
            continue
        out.append(result)
    return out


# ---------------------------------------------------------------------------
# Surface validator (auto-registered at module import)
# ---------------------------------------------------------------------------


def _validate_sunset_proposal_only(
    proposal: AdaptationProposal,
) -> Tuple[bool, str]:
    """Surface validator for sunset-candidate proposals only.

    Required:
      * proposal_kind == "sunset_candidate"
      * proposed_state_hash starts with "sha256:"
      * observation_count >= MIN_OBSERVATIONS_FOR_SUNSET
      * summary contains "stale" (case-insensitive) — defense-in-
        depth against doctored proposals
      * summary contains a numeric-day indicator ("d" or "day") —
        same defense
    """
    if not proposal.proposed_state_hash.startswith("sha256:"):
        return (False, (
            f"sunset_proposed_hash_format:"
            f"{proposal.proposed_state_hash[:32]}"
        ))
    if proposal.evidence.observation_count < MIN_OBSERVATIONS_FOR_SUNSET:
        return (False, (
            f"sunset_observation_count_below_min:"
            f"{proposal.evidence.observation_count}<"
            f"{MIN_OBSERVATIONS_FOR_SUNSET}"
        ))
    summary_lower = (proposal.evidence.summary or "").lower()
    if "stale" not in summary_lower:
        return (False, "sunset_summary_missing_stale_indicator")
    # Look for "day" OR a digit-d pattern (e.g. "30d", "60d") OR
    # the literal "never matched" sentinel.
    has_day_word = "day" in summary_lower
    has_digit_d = any(
        i + 1 < len(summary_lower)
        and summary_lower[i].isdigit()
        and summary_lower[i + 1] == "d"
        for i in range(len(summary_lower))
    )
    has_never = "never matched" in summary_lower
    if not (has_day_word or has_digit_d or has_never):
        return (False, "sunset_summary_missing_day_indicator")
    return (True, "passed_sunset_validator")


# Auto-register the validator at module import. Composes with the
# Slice 2 SemanticGuardianPatterns validator (which handles
# add_pattern proposals) — chain-of-responsibility pattern at
# registration time so neither validator is shadowed.
_VALIDATOR_REGISTERED = False


def _make_chained_validator(
    prior: Optional[SurfaceValidator],
) -> SurfaceValidator:
    """Build a validator that dispatches by proposal_kind:
      * sunset_candidate → our validator
      * else → delegate to the prior validator (Slice 2's add_pattern)
        if registered; else PASS (universal default already handled
        the fundamentals).
    """
    def chained(proposal: AdaptationProposal) -> Tuple[bool, str]:
        if proposal.proposal_kind == "sunset_candidate":
            return _validate_sunset_proposal_only(proposal)
        if prior is not None:
            return prior(proposal)
        return (True, "no_prior_validator_for_kind_pass")
    return chained


def _register_validator_once() -> None:
    global _VALIDATOR_REGISTERED
    if _VALIDATOR_REGISTERED:
        return
    prior = get_surface_validator(
        AdaptationSurface.SEMANTIC_GUARDIAN_PATTERNS,
    )
    register_surface_validator(
        AdaptationSurface.SEMANTIC_GUARDIAN_PATTERNS,
        _make_chained_validator(prior),
    )
    _VALIDATOR_REGISTERED = True


_register_validator_once()


__all__ = [
    "DEFAULT_STALENESS_THRESHOLD_DAYS",
    "MAX_HISTORY_FILE_BYTES",
    "MAX_HISTORY_LINES",
    "MAX_STALE_CANDIDATES_PER_CYCLE",
    "MIN_OBSERVATIONS_FOR_SUNSET",
    "StalePatternCandidate",
    "StalePatternMatchEvent",
    "get_staleness_threshold_days",
    "is_detector_enabled",
    "load_match_events",
    "match_history_path",
    "mine_stale_candidates_from_events",
    "propose_sunset_candidates_from_events",
]
