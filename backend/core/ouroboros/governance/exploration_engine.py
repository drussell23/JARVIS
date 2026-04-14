"""
ExplorationEngine — ledger-based exploration scoring for the Iron Gate MVP.

Replaces the orchestrator's raw tool-count floor with diversity-weighted
scoring that rewards structured understanding over repeated reads. Pure
dataclasses + functions, zero orchestrator wiring. Integration into the
Iron Gate happens in a follow-up patch behind the
``JARVIS_EXPLORATION_LEDGER_ENABLED`` feature flag.

Contract (consumed by orchestrator + tool_executor in a later patch)::

    ledger  = ExplorationLedger.from_records(tool_execution_records)
    floors  = ExplorationFloors.from_env(complexity)
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
from typing import FrozenSet, Iterable, Mapping, Tuple


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

    COMPREHENSION = "comprehension"   # read_file, list_dir
    DISCOVERY     = "discovery"       # search_code, glob_files
    CALL_GRAPH    = "call_graph"      # get_callers
    STRUCTURE     = "structure"       # list_symbols
    HISTORY       = "history"         # git_blame, git_log, git_diff
    UNCATEGORIZED = "uncategorized"


# Venom exploration tools only. Mutators (edit_file, write_file, bash,
# run_tests, ask_human, web_fetch, web_search) are intentionally absent —
# they are not exploration and must not accrue credit.
_TOOL_CATEGORY: Mapping[str, ExplorationCategory] = {
    "read_file":    ExplorationCategory.COMPREHENSION,
    "list_dir":     ExplorationCategory.COMPREHENSION,
    "search_code":  ExplorationCategory.DISCOVERY,
    "glob_files":   ExplorationCategory.DISCOVERY,
    "get_callers":  ExplorationCategory.CALL_GRAPH,
    "list_symbols": ExplorationCategory.STRUCTURE,
    "git_blame":    ExplorationCategory.HISTORY,
    "git_log":      ExplorationCategory.HISTORY,
    "git_diff":     ExplorationCategory.HISTORY,
}


# Base weights. Call-graph and structure tools are worth more than plain
# reads because they make the model reason about interactions rather than
# just fetch bytes. Weights are separate from categories so both can tune
# independently.
_TOOL_WEIGHT: Mapping[str, float] = {
    "read_file":    1.0,
    "list_dir":     0.5,
    "search_code":  1.5,
    "glob_files":   0.5,
    "get_callers":  2.0,
    "list_symbols": 1.5,
    "git_blame":    1.5,
    "git_log":      1.0,
    "git_diff":     1.0,
}

# Duplicate (same tool + same arguments_hash) calls contribute this
# fraction of their base weight. Hard zero — forward progress is measured
# as new work, not repeated fetches of the same thing.
_DUPLICATE_WEIGHT_FACTOR: float = 0.0


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
        """Weighted sum of unique-argument exploration calls.

        Duplicate calls (same tool, same ``arguments_hash``) contribute
        ``base_weight * _DUPLICATE_WEIGHT_FACTOR`` (currently 0). Failed
        calls still accrue full weight — a failed grep still tells the
        model something useful ("X is not used anywhere").
        """
        seen: set = set()
        total = 0.0
        for call in self.calls:
            key = (call.tool_name, call.arguments_hash)
            if key in seen:
                total += call.base_weight * _DUPLICATE_WEIGHT_FACTOR
            else:
                seen.add(key)
                total += call.base_weight
        return round(total, 3)

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
        "min_score":           4.0,
        "min_categories":      2,
        "required_categories": frozenset(),
    },
    "moderate": {
        "min_score":           8.0,
        "min_categories":      3,
        "required_categories": frozenset(),
    },
    "architectural": {
        "min_score":           14.0,
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
    if verdict.score_deficit > 0:
        lines.append(
            "- Widen your exploration: call get_callers on the target symbols, "
            "list_symbols on the target file, search_code for related usages, "
            "or git_blame on hot regions."
        )
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
    "ExplorationLedger",
    "ExplorationVerdict",
    "evaluate_exploration",
    "is_ledger_enabled",
    "render_retry_feedback",
]
