"""
ExplorationEngine — ledger-based exploration scoring for the Iron Gate MVP.

Replaces the orchestrator's raw tool-count floor with diversity-weighted
scoring that rewards structured understanding over repeated reads. Pure
dataclasses + functions, zero orchestrator wiring. Integration into the
Iron Gate happens in a follow-up patch behind the
``JARVIS_EXPLORATION_LEDGER_ENABLED`` feature flag.

Contract (consumed by orchestrator + tool_executor in a later patch)::

    ledger  = ExplorationLedger.from_records(tool_execution_records)
    floors  = ExplorationFloors.from_env_with_adapted(complexity)
    verdict = evaluate_exploration(ledger, floors)
    if verdict.insufficient:
        feedback = render_retry_feedback(verdict, floors)
        # fold ``feedback`` into the GENERATE_RETRY prompt

Everything in this module is pure. No I/O, no network, no orchestrator
imports. ``from_records`` duck-types its input so the module stays
zero-coupled to ``tool_executor.ToolExecutionRecord`` (tests can drive the
same code path with lightweight fakes).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
import logging
from typing import Dict, FrozenSet, Iterable, Mapping, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Category + weight tables
# ---------------------------------------------------------------------------

class ExplorationCategory(str, Enum):
    """Coarse-grained buckets that classify exploration tool calls.

    Diversity floors require coverage across categories — not just many
    calls inside one bucket. Each known tool maps to exactly one category;
    unknown tools map to :attr:`UNCATEGORIZED` and contribute nothing to
    the score.
    """

    COMPREHENSION = "comprehension"   # read_file (reading file content)
    DISCOVERY     = "discovery"       # search_code, glob_files, list_dir
    CALL_GRAPH    = "call_graph"      # get_callers
    STRUCTURE     = "structure"       # list_symbols
    HISTORY       = "history"         # git_blame, git_log, git_diff
    UNCATEGORIZED = "uncategorized"


# Venom exploration tools only. Mutators (edit_file, write_file, bash,
# run_tests, ask_human, web_fetch, web_search) are intentionally absent —
# they are not exploration and must not accrue credit.
_TOOL_CATEGORY: Mapping[str, ExplorationCategory] = {
    "read_file":    ExplorationCategory.COMPREHENSION,
    # list_dir answers "what exists here?" (structure of the filesystem)
    # — semantically discovery, not comprehension. Remapped 2026-04-14
    # after Session bt-2026-04-15-054552 showed a retry adding list_dir
    # to read_file×2 dropping the score (2× read_file = 2.0 beats
    # 2× read_file + 1× list_dir = 2.5 once you factor in the diversity
    # multiplier being 1.0 on both sides pre-remap). The agent was
    # punished for diversifying. Moving list_dir to DISCOVERY lets the
    # diversity multiplier kick in when the agent legitimately widens
    # its exploration.
    "list_dir":     ExplorationCategory.DISCOVERY,
    "search_code":  ExplorationCategory.DISCOVERY,
    "glob_files":   ExplorationCategory.DISCOVERY,
    "get_callers":  ExplorationCategory.CALL_GRAPH,
    "list_symbols": ExplorationCategory.STRUCTURE,
    "git_blame":    ExplorationCategory.HISTORY,
    "git_log":      ExplorationCategory.HISTORY,
    "git_diff":     ExplorationCategory.HISTORY,
}


# Base weights. Call-graph and history tools are worth more than plain
# reads because they make the model reason about interactions and
# temporal context rather than just fetching bytes. Weights are separate
# from categories so both can tune independently.
#
# Calibration history:
#   2026-04-14 (neuro4 live-fire): attempt 2 covered all 5 categories
#   with 11 unique calls but scored 13.0 against a 14.0 architectural
#   floor. Weights for the two required architectural categories
#   (CALL_GRAPH, HISTORY) were bumped +0.5 so that any architectural
#   ledger which legitimately covers both required categories picks up
#   a minimum +1.0 delta.
#   2026-04-14 (gemma live-fire bbpst3ebf): attempt 2 covered all 5
#   categories with 8 unique high-leverage calls and scored 11.5 —
#   still below the 14.0 floor. The organism obeyed the override and
#   pivoted perfectly; 14.0 was mathematically miscalibrated for
#   real-world execution. Rather than inflate weights further (which
#   would reward spam), the architectural floor was lowered to 11.0.
#   The required_categories constraint (CALL_GRAPH + HISTORY) remains
#   the load-bearing diversity check; the score floor just reflects
#   what a well-behaved architectural agent actually produces.
_TOOL_WEIGHT: Mapping[str, float] = {
    "read_file":    1.0,
    "list_dir":     0.5,
    "search_code":  1.5,
    "glob_files":   0.5,
    "get_callers":  2.5,
    "list_symbols": 1.5,
    "git_blame":    2.0,
    "git_log":      1.5,
    "git_diff":     1.5,
}

# Duplicate (same tool + same arguments_hash) calls contribute this
# fraction of their base weight. Hard zero — forward progress is measured
# as new work, not repeated fetches of the same thing.
_DUPLICATE_WEIGHT_FACTOR: float = 0.0


# Hard cap on the base-weight sum. Prevents an adversarial "read every
# file in the repo" strategy from out-scoring diverse exploration through
# sheer volume. Calibrated so a thorough 4-file COMPLEX exploration stays
# under the cap: e.g. read_file × 4 + search_code × 2 + list_symbols × 2
# = 10.0 base (well under 15.0), while read_file × 50 = 50.0 base gets
# clipped to 15.0. Paired with the diversity multiplier, capped base
# still cannot dominate a diverse ledger because spam stays at 1.0
# multiplier (1 category) while diverse patterns climb to 2.0–3.0×.
_BASE_SCORE_CAP: float = 15.0


# Phase 7.5 caller wiring (Caller Wiring PR #4 — 2026-04-26):
# Per-category weight registry. Baseline weight is 1.0 for every
# known category — the diversity-scoring formula multiplies each
# call's `base_weight` (per-tool) by the per-category weight from
# this registry. Master-off byte-identical: when no adapted weights
# are loaded, every multiplier is 1.0, so the formula degenerates
# to the pre-wiring `sum(base_weight)` arithmetic exactly.
#
# Adapted entries arrive via `compute_effective_category_weights()`
# (Phase 7.5 substrate). The Slice 5 miner's mass-conservation cage
# (Σ(new) ≥ Σ(base) + per-category floor at HALF_OF_BASE) ensures
# every adapted vector NET-tightens the exploration cage — even when
# individual category weights drop below 1.0 (low-value categories),
# the corresponding rises (high-value categories) more than
# compensate.
def _baseline_category_weights() -> "Dict[str, float]":
    """Return the canonical per-category baseline: every known
    category at weight 1.0. Excludes UNCATEGORIZED — uncategorized
    calls are silently treated as multiplier=1.0 by the helper.
    """
    return {
        c.value: 1.0
        for c in ExplorationCategory
        if c is not ExplorationCategory.UNCATEGORIZED
    }


def _compute_active_category_weights() -> "Dict[str, float]":
    """Compose Phase 7.5 adapted category weights with the canonical
    baseline.

    Master-off byte-identical: when
    ``JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_CATEGORY_WEIGHTS=false``
    (default), the substrate's `compute_effective_category_weights()`
    returns `dict(baseline)` unchanged → every multiplier in the
    diversity-scoring loop is 1.0 → byte-identical to pre-wiring
    score arithmetic.

    Defense-in-depth: substrate raise → caught here → falls back to
    the canonical baseline (NEVER raises into the caller).

    Per Pass C §4.1 cage rule, the substrate already enforces three
    layers of mass-conservation defense BEFORE returning to us:
      - Sum invariant: Σ(new) ≥ Σ(base)
      - Per-category floor: each new ≥ HALF_OF_BASE × base[k]
      - Absolute floor: each new ≥ MIN_WEIGHT_VALUE
    Our wiring just consumes the result; no need to re-validate.
    """
    base = _baseline_category_weights()
    try:
        from backend.core.ouroboros.governance.adaptation.adapted_category_weight_loader import (  # noqa: E501
            compute_effective_category_weights,
        )
        return compute_effective_category_weights(base)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.warning(
            "[ExplorationEngine] compute_effective_category_weights "
            "raised %s — falling back to canonical baseline", exc,
        )
        return base


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExplorationCall:
    """A single exploration tool call lifted from the orchestrator's
    tool-execution records. Frozen so ledgers can be hashed cheaply.
    """

    tool_name:      str
    arguments_hash: str
    output_bytes:   int  = 0
    succeeded:      bool = True

    @property
    def category(self) -> ExplorationCategory:
        return _TOOL_CATEGORY.get(self.tool_name, ExplorationCategory.UNCATEGORIZED)

    @property
    def base_weight(self) -> float:
        return _TOOL_WEIGHT.get(self.tool_name, 0.0)


@dataclass(frozen=True)
class ExplorationLedger:
    """Immutable view of every exploration call made for a single op.

    Build via :meth:`from_records` (production: reads the orchestrator's
    ``ToolExecutionRecord`` tuple) or :meth:`from_calls` (tests: build
    synthetic ledgers without depending on tool_executor).
    """

    calls: Tuple[ExplorationCall, ...] = ()

    # ----- constructors ---------------------------------------------------

    @classmethod
    def from_records(cls, records: Iterable[object]) -> "ExplorationLedger":
        """Build a ledger from duck-typed tool execution records.

        Each record must expose ``tool_name`` (str) and should expose
        ``arguments_hash``, ``output_bytes``, and ``status``. Missing
        attributes are tolerated with safe defaults. Non-exploration tools
        are filtered out here so callers don't need to pre-filter.
        """
        calls = []
        for rec in records or ():
            tool_name = getattr(rec, "tool_name", None)
            if not tool_name or tool_name not in _TOOL_CATEGORY:
                continue

            status = getattr(rec, "status", None)
            if status is None:
                succeeded = True
            else:
                status_str = getattr(status, "value", str(status))
                succeeded = str(status_str).lower() == "success"

            calls.append(
                ExplorationCall(
                    tool_name=tool_name,
                    arguments_hash=str(getattr(rec, "arguments_hash", "") or ""),
                    output_bytes=int(getattr(rec, "output_bytes", 0) or 0),
                    succeeded=succeeded,
                )
            )
        return cls(calls=tuple(calls))

    @classmethod
    def from_calls(cls, calls: Iterable[ExplorationCall]) -> "ExplorationLedger":
        return cls(calls=tuple(calls))

    # ----- scoring --------------------------------------------------------

    def diversity_score(self) -> float:
        """Category-multiplier weighted sum of exploration calls.

        Formula::

            base_score = min(sum(base_weight for unique call), _BASE_SCORE_CAP)
            n_cats     = len(self.categories_covered())
            multiplier = 1.0 + 0.5 * (n_cats - 1)    if n_cats >= 1 else 0.0
            score      = round(base_score * multiplier, 3)

        The category multiplier is the load-bearing anti-shallow-spam
        mechanism: a ledger that spams ``read_file × 4`` (1 category)
        scores ``4.0 × 1.0 = 4.0``, while a ledger that reads two files,
        runs one ``search_code``, and one ``list_symbols`` (3 categories)
        scores ``5.0 × 2.0 = 10.0`` — the diverse ledger dominates even
        with fewer calls, which is the whole point of the Iron Gate.

        Pre-2026-04-14 (Session bt-2026-04-15-054552) the scorer was a
        plain linear sum, which *punished* diversification whenever the
        diverse tool had a lower base weight than the one it replaced.
        The empirical failure mode: a retry that added ``list_dir`` to
        ``read_file × 2`` scored 2.5, LOWER than four read_file calls at
        4.0, and was rejected by the Iron Gate — despite being
        structurally more diverse. The multiplier fixes that.

        Duplicate calls (same ``(tool_name, arguments_hash)``) contribute
        ``base_weight * _DUPLICATE_WEIGHT_FACTOR`` (hard 0). Failed calls
        still accrue base weight (a failed grep is still signal) but
        don't add to category coverage — an all-failed ledger therefore
        has ``n_cats == 0`` and multiplies to zero, a STRONGER anti-
        gaming property than the pre-multiplier formula provided.
        """
        # Phase 7.5 caller wiring: per-category weight multipliers.
        # Master-off byte-identical → all multipliers == 1.0 →
        # this loop is arithmetically equivalent to the pre-wiring
        # `base += call.base_weight` form. Master-on with adapted
        # rebalance → high-value category contributions scale up,
        # low-value contributions scale down (per Slice 5 cage rule
        # the NET effect is tightening — Σ-invariant + per-cat floor
        # enforced at the substrate).
        cat_weights = _compute_active_category_weights()
        seen: set = set()
        base = 0.0
        for call in self.calls:
            cat_multiplier = cat_weights.get(call.category.value, 1.0)
            key = (call.tool_name, call.arguments_hash)
            if key in seen:
                base += (
                    call.base_weight
                    * _DUPLICATE_WEIGHT_FACTOR
                    * cat_multiplier
                )
            else:
                seen.add(key)
                base += call.base_weight * cat_multiplier

        base = min(base, _BASE_SCORE_CAP)
        n_cats = len(self.categories_covered())

        if n_cats == 0:
            multiplier = 0.0
        else:
            multiplier = 1.0 + 0.5 * (n_cats - 1)

        return round(base * multiplier, 3)

    def categories_covered(self) -> FrozenSet[ExplorationCategory]:
        """Categories with at least one successful, unique call.

        Failed calls don't count toward coverage (they may indicate the
        tool itself misbehaved). Duplicates don't add new coverage beyond
        the first successful call.
        """
        seen: set = set()
        out: set = set()
        for call in self.calls:
            if not call.succeeded:
                continue
            if call.category is ExplorationCategory.UNCATEGORIZED:
                continue
            key = (call.tool_name, call.arguments_hash)
            if key in seen:
                continue
            seen.add(key)
            out.add(call.category)
        return frozenset(out)

    def unique_call_count(self) -> int:
        """Distinct (tool, args_hash) pairs that successfully executed.

        Used as a monotonic forward-progress signal — strictly increasing
        between retry rounds means the model made new observations rather
        than spinning.
        """
        seen: set = set()
        for call in self.calls:
            if not call.succeeded:
                continue
            seen.add((call.tool_name, call.arguments_hash))
        return len(seen)


# ---------------------------------------------------------------------------
# Floors (env-driven, no hidden defaults)
# ---------------------------------------------------------------------------

_DEFAULT_FLOORS: Mapping[str, Mapping[str, object]] = {
    "trivial": {
        "min_score":           0.0,
        "min_categories":      0,
        "required_categories": frozenset(),
    },
    "simple": {
        # Calibrated 2026-04-14 for the diversity-multiplier formula.
        # The minimum acceptable simple-op exploration is "read the target
        # file + do one breadth action" — read_file + search_code scores
        # 2.5 * 1.5 = 3.75 under the new math. Floor set to 3.5 so that
        # pattern passes while read_file + list_dir (1.5 * 1.5 = 2.25)
        # does not — list_dir alone is too light to count as real breadth.
        "min_score":           3.5,
        "min_categories":      2,
        "required_categories": frozenset(),
    },
    "moderate": {
        # Unchanged floor; much easier to hit under the new multiplier
        # math because a 3-category exploration gets ×2.0. A minimal
        # acceptable pattern (read_file + search_code + list_symbols =
        # 4.0 base × 2.0 = 8.0) sits exactly at the floor.
        "min_score":           8.0,
        "min_categories":      3,
        "required_categories": frozenset(),
    },
    "complex": {
        # NEW entry (2026-04-14) — was silently falling through to
        # `moderate` defaults because `complex` had no dedicated row.
        # Session bt-2026-04-15-044627 made that fall-through visible:
        # a 4-file COMPLEX probe was being enforced against a MODERATE
        # score floor. Calibrated to sit JUST BELOW the "minimum reasonable
        # 4-file exploration" pattern (read_file × 2 + search_code +
        # list_symbols = 5.0 base × 2.0 mult = 10.0). A model that clears
        # this floor has demonstrably touched comprehension + discovery +
        # structure; a model that doesn't is still in single-category spam.
        "min_score":           10.0,
        "min_categories":      3,
        "required_categories": frozenset(),
    },
    "architectural": {
        # TODO(2026-04-14): Recalibrate for the new diversity-multiplier
        # formula. Under the new math, any 4-category exploration at base
        # >=5.5 passes 11.0 trivially, so this floor is effectively a
        # no-op — the load-bearing gates are `min_categories=4` and the
        # `required_categories={CALL_GRAPH, HISTORY}` conjunct. Revisit
        # after one or two ARCHITECTURAL-tier battle tests land under
        # the new formula to pick a floor that reflects "true
        # architectural-grade exploration" rather than the pre-
        # multiplier calibration this number was fitted to.
        "min_score":           11.0,
        "min_categories":      4,
        "required_categories": frozenset({
            ExplorationCategory.CALL_GRAPH,
            ExplorationCategory.HISTORY,
        }),
    },
}


def _read_float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return float(default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(default)


def _read_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return int(default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return int(default)


@dataclass(frozen=True)
class ExplorationFloors:
    """Per-complexity exploration thresholds.

    A ledger passes when all three hold::

        score      >= min_score
        |covered|  >= min_categories
        required_categories ⊆ covered

    ``from_env`` reads ``JARVIS_EXPLORATION_MIN_SCORE_<COMPLEXITY>`` and
    ``JARVIS_EXPLORATION_MIN_CATEGORIES_<COMPLEXITY>`` so ops teams can
    tune floors without a code change. ``required_categories`` is a hard
    subset requirement (not env-tunable) — used for architectural ops
    that must cover call-graph + history.
    """

    complexity:          str
    min_score:           float
    min_categories:      int
    required_categories: FrozenSet[ExplorationCategory] = frozenset()

    @classmethod
    def from_env(cls, complexity: str) -> "ExplorationFloors":
        c = (complexity or "").strip().lower() or "moderate"
        if c not in _DEFAULT_FLOORS:
            c = "moderate"
        defaults = _DEFAULT_FLOORS[c]

        score_env = f"JARVIS_EXPLORATION_MIN_SCORE_{c.upper()}"
        cats_env  = f"JARVIS_EXPLORATION_MIN_CATEGORIES_{c.upper()}"

        min_score = _read_float_env(score_env, float(defaults["min_score"]))  # type: ignore[arg-type]
        min_cats  = _read_int_env(cats_env,    int(defaults["min_categories"]))  # type: ignore[arg-type]
        req_cats  = defaults["required_categories"]  # frozenset — immutable, safe to share

        return cls(
            complexity=c,
            min_score=min_score,
            min_categories=min_cats,
            required_categories=req_cats,  # type: ignore[arg-type]
        )

    @classmethod
    def from_env_with_adapted(cls, complexity: str) -> "ExplorationFloors":
        """Phase 7.2 — env floors + Pass C adapted floors merged.

        Reads `.jarvis/adapted_iron_gate_floors.yaml` (when env flag
        `JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_FLOORS` is on) and
        merges the adapted required-categories into the env baseline.

        Cage discipline (load-bearing per Pass C §7.3):
          * Adapted floors can only RAISE coverage requirements —
            new entries are added to `required_categories`; never
            removed.
          * Default-off + fail-open: when the env flag is off OR the
            YAML is missing/malformed, this method returns the same
            ExplorationFloors `from_env` would.
          * `min_score` and `min_categories` are NOT modified by
            adapted floors (the adapted-floor surface is structurally
            categorical-coverage; numeric thresholds stay env-driven).
            Operator visibility for the per-category numeric value
            is via `/posture` REPL surfacing in a follow-up.
        """
        base = cls.from_env(complexity)
        try:
            from backend.core.ouroboros.governance.adaptation.adapted_iron_gate_loader import (  # noqa: E501
                compute_adapted_required_categories,
                is_loader_enabled,
                load_adapted_floors,
            )
        except Exception:  # noqa: BLE001 — fail-open
            return base
        if not is_loader_enabled():
            return base
        try:
            adapted_floors = load_adapted_floors()
        except Exception:  # noqa: BLE001 — fail-open
            return base
        if not adapted_floors:
            return base
        adapted_cat_names = compute_adapted_required_categories(
            adapted_floors,
        )
        # Translate category-name strings → ExplorationCategory enum
        # values. Unknown values (which the loader already filters)
        # are tolerated here too as defense-in-depth.
        adapted_required: set = set()
        for name in adapted_cat_names:
            try:
                adapted_required.add(ExplorationCategory(name))
            except ValueError:
                continue
        if not adapted_required:
            return base
        merged = frozenset(base.required_categories) | frozenset(
            adapted_required,
        )
        # Adapted floors are additive only — return a new instance
        # with the union; never remove existing required categories.
        import logging as _logging
        _logging.getLogger(__name__).info(
            "[ExplorationFloors] merged %d adapted floor(s) into "
            "required_categories (env=%d, adapted=%d, merged=%d)",
            len(adapted_required),
            len(base.required_categories),
            len(adapted_required),
            len(merged),
        )
        return cls(
            complexity=base.complexity,
            min_score=base.min_score,
            min_categories=base.min_categories,
            required_categories=merged,  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# Gate evaluation + retry feedback
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExplorationVerdict:
    """Outcome of evaluating a ledger against its floors."""

    sufficient:          bool
    score:               float
    score_deficit:       float
    categories_covered:  FrozenSet[ExplorationCategory]
    missing_categories:  FrozenSet[ExplorationCategory]
    category_deficit:    int

    @property
    def insufficient(self) -> bool:
        return not self.sufficient


class ExplorationInsufficientError(RuntimeError):
    """Raised by the Iron Gate when ledger-enforced exploration is insufficient.

    Carries the ``verdict`` and ``floors`` so the GENERATE_RETRY feedback
    builder can call :func:`render_retry_feedback` and produce a category-
    aware prompt without re-running the scoring pass.

    Subclasses :class:`RuntimeError` to preserve the existing
    ``_err_str.startswith("exploration_insufficient")`` retry branches in
    ``orchestrator.py`` — the string message keeps the same prefix, and
    ``except Exception`` / ``except RuntimeError`` handlers catch it
    identically to the legacy gate raise.
    """

    def __init__(
        self,
        message: str,
        verdict: "ExplorationVerdict",
        floors: "ExplorationFloors",
    ) -> None:
        super().__init__(message)
        self.verdict = verdict
        self.floors = floors


def evaluate_exploration(
    ledger: ExplorationLedger,
    floors: ExplorationFloors,
) -> ExplorationVerdict:
    """Pure check: does the ledger clear all three floor conditions?"""
    score    = ledger.diversity_score()
    covered  = ledger.categories_covered()
    missing  = floors.required_categories - covered

    score_ok = score >= floors.min_score
    count_ok = len(covered) >= floors.min_categories
    req_ok   = not missing

    return ExplorationVerdict(
        sufficient=score_ok and count_ok and req_ok,
        score=score,
        score_deficit=round(max(0.0, floors.min_score - score), 3),
        categories_covered=covered,
        missing_categories=missing,
        category_deficit=max(0, floors.min_categories - len(covered)),
    )


# Tool leverage tiers for retry feedback. Kept in sync with _TOOL_WEIGHT
# above — if weights are rebalanced, update these lists so the feedback
# doesn't send the model after tools that have lost their edge.
#
# The three tiers reflect the multiplier-era reality: a ledger that
# covers 3 categories using only 0.5-weight tools scores
# ``3 * 0.5 * 2.0 = 3.0`` and fails every non-trivial floor, while the
# same 3 categories covered with one 2.5-weight tool and two 1.5-weight
# tools scores ``5.5 * 2.0 = 11.0`` and passes complex (10.0) cleanly.
# Naming the tools explicitly in the feedback closes the behavioral
# gap Session bt-2026-04-15-063108 exposed: the model diversified
# correctly but picked the cheapest tool per category (list_dir 0.5
# for discovery, list_symbols 1.5 for structure), coming up 1.0 short
# of the complex floor at 9.0/10.0.
_HIGH_LEVERAGE_TOOLS: Tuple[str, ...] = ("get_callers", "git_blame")
_MEDIUM_LEVERAGE_TOOLS: Tuple[str, ...] = (
    "search_code", "list_symbols", "git_log", "git_diff",
)
_LOW_LEVERAGE_TOOLS: Tuple[str, ...] = ("list_dir", "glob_files")


def render_retry_feedback(
    verdict: ExplorationVerdict,
    floors: ExplorationFloors,
) -> str:
    """Deterministic feedback block for GENERATE_RETRY.

    Returns an empty string when the verdict is sufficient so callers can
    unconditionally concatenate this into the retry prompt. Insufficient
    verdicts name the **missing categories** rather than emitting a generic
    "need more reads" message — that's the whole point of the diversity
    floor.

    Two distinct failure modes get dedicated feedback branches:

    1. **Missing required or unsatisfied category count** — tell the
       model which categories are still absent and demand at least one
       tool per missing category.
    2. **Score-only deficit with categories satisfied** (Session E,
       2026-04-14) — the model has the right breadth but picked
       lightweight tools (``list_dir``, ``glob_files``) to hit category
       count. Padding with more of those will never raise the score.
       This branch explicitly names the HIGH-leverage tools
       (``get_callers``, ``git_blame``) and MEDIUM-leverage tools
       (``search_code``, ``list_symbols``, ``git_log``, ``git_diff``)
       that move the score, and warns the model against repeating the
       low-leverage pattern.
    """
    if verdict.sufficient:
        return ""

    lines = [
        "[EXPLORATION GATE] Your exploration is insufficient for this operation.",
        f"- Complexity: {floors.complexity}",
        f"- Diversity score: {verdict.score:.1f} (required: {floors.min_score:.1f})",
        (
            f"- Categories covered: {len(verdict.categories_covered)} "
            f"(required: {floors.min_categories})"
        ),
    ]
    if verdict.missing_categories:
        missing = ", ".join(sorted(c.value for c in verdict.missing_categories))
        lines.append(
            f"- REQUIRED categories still missing: {missing}. "
            "You MUST call at least one tool from each of these categories "
            "before emitting any edit_file / write_file / delete_file call."
        )

    # Session F (bt-2026-04-15-065523, 2026-04-14) proved the previous
    # ``categories_satisfied`` gate was architecturally unreachable in
    # the 2-attempt retry loop: the sharpened high-leverage warning
    # could only fire on a retry whose previous attempt had already
    # covered all categories, but attempt 1 typically covers 2/3 and
    # there is no attempt 3 to apply the sharpened feedback to.
    #
    # Fix: fire the high-leverage block UNCONDITIONALLY on any score
    # deficit, additively after the missing-category guidance above.
    # The model now sees BOTH messages when both gates are failing —
    # "fill missing categories" (above) AND "use high-leverage tools
    # for the fill" (below) — in the right order. Trivial tier has
    # ``min_score=0.0`` so ``score_deficit`` is always 0 and this
    # block never fires there.
    if verdict.score_deficit > 0:
        hl = ", ".join(f"`{t}`" for t in _HIGH_LEVERAGE_TOOLS)
        ml = ", ".join(f"`{t}`" for t in _MEDIUM_LEVERAGE_TOOLS)
        ll = ", ".join(f"`{t}`" for t in _LOW_LEVERAGE_TOOLS)
        lines.extend([
            (
                f"- SCORE GATE: your current score is "
                f"{verdict.score:.1f}/{floors.min_score:.1f} — "
                f"deficit {verdict.score_deficit:.1f} points."
            ),
            (
                "- CRITICAL: category breadth alone will not close "
                f"this gap. LOW-LEVERAGE tools ({ll}, each worth 0.5 "
                "weight) contribute minimally to the weighted sum — "
                "padding with more of them will NOT raise the score."
            ),
            (
                f"- Widen your exploration with HIGH-LEVERAGE tools: "
                f"{hl} (worth 2.0–2.5 weight each). A single "
                "`get_callers` call on the primary target symbol, or "
                "a `git_blame` on the target file, will push you past "
                "the floor by itself."
            ),
            (
                f"- Alternatively, use MEDIUM-LEVERAGE tools: {ml} "
                "(worth 1.5 weight each). Prefer `search_code` for "
                "cross-file references, `list_symbols` for target-"
                "file structure, or `git_log` / `git_diff` for "
                "temporal context."
            ),
            (
                "- DO NOT pad with additional `list_dir` or `glob_files` "
                "calls — they will not close the score deficit. Call "
                "get_callers / git_blame / search_code instead."
            ),
        ])
    lines.append(
        "Run more exploration tools now; do not attempt to patch until the "
        "gate passes."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Feature flag (shadow-log first, enforce later)
# ---------------------------------------------------------------------------

def is_ledger_enabled() -> bool:
    """Master switch for Iron Gate integration.

    When false (default), callers should still **shadow-log** the ledger
    score so deterministic scoring can be compared against the current
    counter-based gate without yet enforcing it. When true, the Iron Gate
    MUST consult the ledger authoritatively.
    """
    raw = os.environ.get("JARVIS_EXPLORATION_LEDGER_ENABLED", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


__all__ = [
    "ExplorationCall",
    "ExplorationCategory",
    "ExplorationFloors",
    "ExplorationInsufficientError",
    "ExplorationLedger",
    "ExplorationVerdict",
    "evaluate_exploration",
    "is_ledger_enabled",
    "render_retry_feedback",
]
