"""Shared deterministic scoring primitives for cross-op pattern
accumulation arcs (Upgrade 3, M11, future Upgrade 1 + M9).

Decision C2 (M11 Slice 3 scope): extract the four primitives that
:mod:`failure_mode_memory` Slice 3 originated into a shared module
so M11 + future arcs (Upgrade 1 Bounded Epistemic Loop will likely
reuse :func:`recency_weight`; M9 CuriosityGradient may reuse
:func:`weight_score`) compose without copy-pasted code.

The module-name underscore prefix (``_scoring_primitives``) marks
this as **package-internal** — external callers (CLI, IDE
extensions, future plugins) should not depend on this surface;
the appropriate consumer surface for retrieval scoring is each
arc's public retriever (``retrieve_failure_modes`` /
``recall_for_region`` / etc.).

The function names themselves are **public** (no leading
underscore) because they ARE imported across the package by the
sibling arc modules. Established convention from
:mod:`cross_process_jsonl` / :mod:`_governance_state` / similar.

Authority invariants (AST-pinned by companion tests):

  * Imports stdlib ONLY — strongest possible authority floor.
    Any consumer-arc behavioral state lives in the arc itself;
    this module is pure mathematical primitives.
  * NEVER raises out of any function — defensive everywhere;
    callers of these primitives are on the GENERATE prompt-
    construction hot path and CANNOT tolerate exceptions
    propagating up.
  * Pure functions — same inputs always produce same outputs;
    no module-level mutable state.

Each primitive is independently testable + auditable. The
formulas are pinned via direct mathematical comparison (e.g.
``0.5 ** (age_days / halflife_days)`` for recency) so any future
divergence from the literal contract trips immediately.
"""
from __future__ import annotations

import logging
import math
from typing import Callable, Iterable, List, TypeVar

logger = logging.getLogger(__name__)


__all__ = [
    "DEFAULT_WEIGHT_SATURATION_REFERENCE",
    "diversity_dedup",
    "jaccard_similarity",
    "recency_weight",
    "weight_score",
]


# ---------------------------------------------------------------------------
# Recency weight — 0.5 ** (age_days / halflife_days) literal formula
# ---------------------------------------------------------------------------


def recency_weight(
    age_seconds: float, halflife_days: float,
) -> float:
    """``0.5 ** (age_days / halflife_days)``. Clamped to [0, 1].

    Literal parity with :func:`coherence_auditor._recency_weight`
    and :func:`semantic_index._recency_weight` — pinned by test
    so any future divergence from this exact formula trips
    immediately.

    Edge cases (defensive):
      * ``halflife_days <= 0`` -> 1.0 (caller has clamped the
        env knob improperly; treat as "no decay")
      * ``age_seconds < 0`` -> 1.0 (clock skew or test-time
        synthetic-future record; treat as fresh)
      * Any other exception -> 0.0 (degraded fallback; caller
        will still get a number to multiply)

    NEVER raises."""
    try:
        if halflife_days <= 0 or age_seconds < 0:
            return 1.0
        age_days = age_seconds / 86400.0
        return float(0.5 ** (age_days / halflife_days))
    except Exception:  # noqa: BLE001 — defensive
        return 0.0


# ---------------------------------------------------------------------------
# Jaccard similarity over string sets
# ---------------------------------------------------------------------------


def jaccard_similarity(
    a: Iterable[str], b: Iterable[str],
) -> float:
    """``|a ∩ b| / |a ∪ b|`` — 1.0 when both sets are empty
    (degenerate exact-match), 0.0 when union is otherwise empty.

    Edge cases:
      * Both iterables empty -> 1.0 (treat as full match — the
        caller has enum-only signal and no file dimension)
      * Union empty after filtering -> 0.0 (defensive — should
        not happen in practice given the above)
      * Non-iterable input -> 0.0 (defensive fallback)

    Empty-string elements are filtered out before set
    construction — callers passing ``("a.py", "")`` get the same
    result as ``("a.py",)``. NEVER raises."""
    try:
        sa = set(str(x) for x in a if x)
        sb = set(str(x) for x in b if x)
    except (TypeError, ValueError):
        return 0.0
    if not sa and not sb:
        # Both empty — treat as full match (situation alone matched).
        return 1.0
    union = sa | sb
    if not union:
        return 0.0
    inter = sa & sb
    return float(len(inter)) / float(len(union))


# ---------------------------------------------------------------------------
# Weight score — bounded, log-scale (saturates near reference)
# ---------------------------------------------------------------------------


# Reference floor for log-scale weight saturation. With reference=10,
# weight=2 yields ~0.46, weight=10 saturates near 1.0. Compresses
# the long tail so a single 50-recurrence outlier doesn't dominate
# diverse medium-weight matches. Pinned by tests.
DEFAULT_WEIGHT_SATURATION_REFERENCE: int = 10


def weight_score(
    weight: int, *, reference: int = DEFAULT_WEIGHT_SATURATION_REFERENCE,
) -> float:
    """Bounded, non-linear weight scoring. ``log1p(weight) /
    log1p(reference)`` capped at 1.0.

    Linear weight would let one 50-recurrence outlier dominate the
    top-K; log1p compresses the tail so multiple medium-weight
    matches can still surface. The reference parameter is
    caller-tunable so each arc can pick saturation appropriate to
    its weight semantics (Upgrade 3 uses default 10 — a "very
    recurrent" failure; M11 may use lower for outcomes since a
    single APPLIED_VERIFIED is already strong signal).

    Edge cases:
      * ``weight <= 0`` -> 0.0
      * ``reference <= 0`` -> 0.0 (caller misconfigured; fail
        soft so the multiplicative pipeline degrades to 0)

    NEVER raises."""
    try:
        w = max(0, int(weight))
        ref = max(0, int(reference))
        if ref <= 0:
            return 0.0
        denom = math.log1p(ref)
        if denom <= 0:
            return 0.0
        return min(1.0, math.log1p(w) / denom)
    except Exception:  # noqa: BLE001 — defensive
        return 0.0


# ---------------------------------------------------------------------------
# Diversity dedup — preserve at-most-one per key in primary set
# ---------------------------------------------------------------------------


T = TypeVar("T")


def diversity_dedup(
    matches: Iterable[T],
    *,
    top_k: int,
    key_fn: Callable[[T], str],
) -> tuple:
    """Walk ``matches`` in score-descending order (caller pre-
    sorts); preserve at most one per ``key_fn`` value until
    ``top_k`` filled. If pool exhausted before ``top_k`` filled,
    fall through and accept duplicate-key matches in score order.

    Generalized from the Upgrade 3 retriever (which hardcoded
    ``record.attempted_action_kind`` as the key). M11 uses
    ``record.outcome_kind.value`` to enforce balanced-palette
    diversity (one VERIFIED + one REVERTED + one REJECTED rather
    than three VERIFIED). Future arcs may key on cluster_id /
    severity / etc.

    Edge cases:
      * ``top_k <= 0`` -> empty tuple
      * Empty matches -> empty tuple
      * key_fn raises on a match -> that match is treated as
        having an empty-string key (degraded; never blocks
        retrieval)

    NEVER raises."""
    if top_k < 1:
        return tuple()
    primary: List[T] = []
    seen_keys: set = set()
    overflow: List[T] = []
    try:
        for m in matches:
            try:
                key = str(key_fn(m)).strip().lower()
            except Exception:  # noqa: BLE001 — defensive per-match
                key = ""
            if key not in seen_keys:
                primary.append(m)
                seen_keys.add(key)
                if len(primary) >= top_k:
                    break
            else:
                overflow.append(m)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[_scoring_primitives] diversity_dedup raised: %s",
            exc,
        )
        return tuple(primary[:top_k])
    if len(primary) >= top_k:
        return tuple(primary[:top_k])
    remaining = top_k - len(primary)
    return tuple(primary + overflow[:remaining])
