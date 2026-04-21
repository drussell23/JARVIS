"""
IntentTracker + PreservationScorer — Slice 2 of the Context Preservation arc.
=============================================================================

What this module does
---------------------

Replaces "keep last N entries" with **score-ordered preservation**. Two
primitives live here:

* :class:`IntentTracker` — deterministic, per-op extractor of current
  operator intent signals. Consumes dialogue turns + ``ask_human``
  answers + plan approvals, produces a recency-weighted set of files,
  tools, and error terms the operator is *currently* focused on. No
  LLM calls.
* :class:`PreservationScorer` — ranks candidate context chunks against
  the current intent (+ structural importance + recency). Compaction
  passes consult the scorer to decide what survives, what compacts,
  what drops.

Boundaries
----------

* §1 — intent signals are extracted from operator / orchestrator turns,
  not model output. A model turn claiming "I'm focused on auth.py"
  does NOT move the intent; an operator turn saying so does.
  Enforcement: :meth:`ingest_turn` takes an explicit ``source``; only
  ``user`` / ``orchestrator`` source turns shift intent. ``assistant``
  source turns are absorbed for recency tracking only.
* §5 — zero LLM calls. Path extraction uses a compiled regex set;
  tool names come from the known-tool whitelist; error terms from a
  small compiled lexicon. All O(len(text)).
* §7 — fail-closed: scoring produces a total ordering so compaction
  decisions are deterministic. Ties break on recency, then on chunk
  index, never on wall-clock.
* §8 — every scored decision emits structured signal breakdown
  (``ChunkScore.breakdown``) so Slice 4's manifest can show WHY a
  chunk was kept or dropped.

Design pattern
--------------

1. Conversation + ledger events flow into :meth:`ingest_turn` and
   :meth:`ingest_ledger_entry`.
2. The tracker maintains three recency-weighted frequency counters:
   ``mentioned_paths``, ``active_tools``, ``error_terms``.
3. :meth:`current_intent` returns an immutable snapshot.
4. :meth:`PreservationScorer.score` takes a chunk + intent snapshot
   and returns a :class:`ChunkScore` (base + intent + structure +
   pin_bonus + total).
5. :meth:`PreservationScorer.select_preserved` applies a budget
   (char count / chunk count) and returns the winning subset, the
   discarded subset, and a per-chunk decision trail (kept / compacted
   / dropped) suitable for the Slice 4 manifest.
"""
from __future__ import annotations

import enum
import logging
import math
import re
import threading
from dataclasses import dataclass, field
from typing import (
    Any, Callable, Dict, FrozenSet, Iterable, List, Optional, Tuple,
)

logger = logging.getLogger("Ouroboros.ContextIntent")


# ---------------------------------------------------------------------------
# Schema / versioning
# ---------------------------------------------------------------------------

INTENT_TRACKER_SCHEMA_VERSION: str = "context_intent.v1"


# ---------------------------------------------------------------------------
# Recency decay helpers
# ---------------------------------------------------------------------------


# Each turn is "one unit" of recency; signals fade exponentially so that
# five turns ago weighs about a quarter of "right now". Operator-tunable
# per test; the production default is chosen to match CC's subjective
# "still feels current" horizon (~8 turns).
_DEFAULT_DECAY_HALF_LIFE_TURNS: float = 4.0


def _decay_weight(
    turns_since: int, *, half_life: float = _DEFAULT_DECAY_HALF_LIFE_TURNS,
) -> float:
    """Return an exponential decay weight ≤ 1.0 for a signal N turns old."""
    if turns_since <= 0:
        return 1.0
    return math.pow(0.5, turns_since / max(1e-6, half_life))


# ---------------------------------------------------------------------------
# Path / tool / error extraction (deterministic, regex only)
# ---------------------------------------------------------------------------


# Path-like fragment: a sequence of word / slash / dot chars with at least
# one slash OR a word ending in a known suffix. This is intentionally
# narrow — we want "backend/foo.py" and "src/lib/utils.ts" but NOT
# "this.is.not.a.path".
_PATH_RX = re.compile(
    r"""
    (?P<path>
      (?:
        # Absolute or relative with slashes
        [A-Za-z0-9_\-./]+/[A-Za-z0-9_\-./]+
        |
        # Bareword with a known source suffix
        [A-Za-z0-9_\-]+
        \.
        (?:py|pyi|ts|tsx|js|jsx|rs|go|kt|java|c|cc|cpp|h|hpp|md|yaml|yml|json|toml|sh|rb|ex|exs)
      )
    )
    """,
    re.VERBOSE,
)


# Known tool names we care about for intent. Slice 2 v1 enumerates the
# Venom built-ins; future slices can widen via env.
_KNOWN_TOOL_NAMES: FrozenSet[str] = frozenset({
    "read_file", "search_code", "edit_file", "write_file", "delete_file",
    "bash", "run_tests", "web_fetch", "web_search",
    "get_callers", "glob_files", "list_dir", "list_symbols",
    "git_log", "git_diff", "git_blame", "apply_patch",
    "monitor", "task_create", "task_update", "task_complete",
    "ask_human", "delegate_to_agent", "dispatch_subagent",
})


# A conservative lexicon of error / investigation terms that when present
# on a chunk indicate "we were debugging X". Matches are whole-word +
# case-insensitive.
_ERROR_TERMS: FrozenSet[str] = frozenset({
    "error", "exception", "traceback", "failed", "failure", "panic",
    "segfault", "timeout", "deadlock", "crash", "regression", "bug",
    "broken", "fails", "importerror", "attributeerror", "keyerror",
    "typeerror", "valueerror", "assertionerror", "nameerror",
    "syntaxerror", "runtimeerror",
})

_ERROR_TERMS_RX = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in _ERROR_TERMS) + r")\b",
    re.IGNORECASE,
)

# Tool names → compiled alternation for whole-word matching
_TOOL_NAMES_RX = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in _KNOWN_TOOL_NAMES) + r")\b",
)


def extract_paths(text: str) -> List[str]:
    """Return the list of path-like tokens in *text*, in order, deduplicated."""
    if not text:
        return []
    seen: List[str] = []
    out_set = set()
    for m in _PATH_RX.finditer(text):
        p = m.group("path")
        # Filter out common-English false positives that slip past the
        # regex: paths that are pure punctuation / too short / look like
        # email local-parts or URLs-without-domains are rejected.
        if len(p) < 4:
            continue
        if p.startswith(".") and "/" not in p:
            continue
        if p in out_set:
            continue
        out_set.add(p)
        seen.append(p)
    return seen


def extract_tool_mentions(text: str) -> List[str]:
    """Return tool names from the known whitelist mentioned in *text*."""
    if not text:
        return []
    seen: List[str] = []
    out_set = set()
    for m in _TOOL_NAMES_RX.finditer(text):
        name = m.group(1)
        if name in out_set:
            continue
        out_set.add(name)
        seen.append(name)
    return seen


def extract_error_terms(text: str) -> List[str]:
    """Return error-lexicon terms present in *text*, lower-cased + deduplicated."""
    if not text:
        return []
    seen: List[str] = []
    out_set = set()
    for m in _ERROR_TERMS_RX.finditer(text):
        term = m.group(1).lower()
        if term in out_set:
            continue
        out_set.add(term)
        seen.append(term)
    return seen


# ---------------------------------------------------------------------------
# TurnSource — who the turn came from
# ---------------------------------------------------------------------------


class TurnSource(str, enum.Enum):
    USER = "user"
    ASSISTANT = "assistant"
    ORCHESTRATOR = "orchestrator"
    TOOL = "tool"


# §1 Boundary: only these sources are trusted to shift intent.
_AUTHORITATIVE_SOURCES: FrozenSet[TurnSource] = frozenset({
    TurnSource.USER, TurnSource.ORCHESTRATOR,
})


# ---------------------------------------------------------------------------
# Intent snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntentSnapshot:
    """Immutable view of the tracker's state at a point in time."""

    op_id: str
    turn_count: int
    recent_paths: Tuple[str, ...]      # top-N by weighted frequency
    recent_tools: Tuple[str, ...]
    recent_error_terms: Tuple[str, ...]
    weighted_path_scores: Dict[str, float] = field(default_factory=dict)
    weighted_tool_scores: Dict[str, float] = field(default_factory=dict)
    weighted_error_scores: Dict[str, float] = field(default_factory=dict)
    schema_version: str = INTENT_TRACKER_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# IntentTracker
# ---------------------------------------------------------------------------


class IntentTracker:
    """Per-op deterministic intent extractor.

    Maintains three independent recency-weighted scores:
        mentioned_paths: Dict[str, float]
        active_tools:     Dict[str, float]
        error_terms:      Dict[str, float]

    Turn counter advances monotonically on every authoritative ingest;
    older signals decay as new turns arrive. Non-authoritative turns
    (assistant / tool) do NOT advance the counter but may still
    contribute to ``recent_tools`` (observed usage, not operator intent).
    """

    def __init__(
        self,
        op_id: str,
        *,
        half_life_turns: Optional[float] = None,
        top_n: int = 10,
    ) -> None:
        if not op_id:
            raise ValueError("op_id must be non-empty")
        self._op_id = op_id
        self._half_life = half_life_turns or _DEFAULT_DECAY_HALF_LIFE_TURNS
        self._top_n = max(1, top_n)
        self._lock = threading.Lock()

        # signal → (weight_sum, most_recent_turn_seen)
        # On ingest we decay weight_sum by (turn_now - most_recent_turn_seen)
        # BEFORE adding +1.0; this makes the stored value always the
        # weight if we pretend the signal was last seen now.
        self._paths: Dict[str, Tuple[float, int]] = {}
        self._tools: Dict[str, Tuple[float, int]] = {}
        self._errors: Dict[str, Tuple[float, int]] = {}
        self._turn_count: int = 0

    # --- write path ------------------------------------------------------

    def ingest_turn(
        self,
        text: str,
        *,
        source: TurnSource = TurnSource.USER,
    ) -> None:
        """Absorb one conversation turn.

        Only ``USER`` / ``ORCHESTRATOR`` sources advance the intent
        clock and move the authoritative counters. ``ASSISTANT`` /
        ``TOOL`` turns are ignored for intent shifts (§1); callers who
        want to also preserve tool observations should use
        :meth:`ingest_ledger_entry` instead.
        """
        if not text:
            return
        with self._lock:
            if source in _AUTHORITATIVE_SOURCES:
                self._turn_count += 1
            # Only authoritative turns mutate intent.
            if source not in _AUTHORITATIVE_SOURCES:
                return
            now_turn = self._turn_count
            for p in extract_paths(text):
                self._bump(self._paths, p, now_turn)
            for t in extract_tool_mentions(text):
                self._bump(self._tools, t, now_turn)
            for term in extract_error_terms(text):
                self._bump(self._errors, term, now_turn)

    def ingest_ledger_entry(self, projection: Dict[str, Any]) -> None:
        """Absorb a :class:`ContextLedger` entry projection.

        Different from :meth:`ingest_turn` — ledger entries are facts
        the agent observed (not operator intent) but carry signal we
        want to preserve. A ``file_read`` entry strengthens the
        ``mentioned_paths`` score for that file; a ``tool_call`` entry
        strengthens the ``active_tools`` score. These updates do NOT
        advance the turn clock (the operator's clock runs separately).
        """
        kind = projection.get("kind", "")
        if not kind:
            return
        with self._lock:
            now_turn = self._turn_count
            if kind == "file_read":
                p = projection.get("file_path", "")
                if p:
                    self._bump(self._paths, p, now_turn)
            elif kind == "tool_call":
                t = projection.get("tool", "")
                if t:
                    self._bump(self._tools, t, now_turn)
            elif kind == "error":
                msg = projection.get("message", "") or ""
                for term in extract_error_terms(msg):
                    self._bump(self._errors, term, now_turn)
                where = projection.get("where", "") or ""
                for path in extract_paths(where):
                    self._bump(self._paths, path, now_turn)
            elif kind == "question":
                for p in projection.get("related_paths", []) or []:
                    self._bump(self._paths, p, now_turn)
                for t in projection.get("related_tools", []) or []:
                    self._bump(self._tools, t, now_turn)
            elif kind == "decision":
                for p in projection.get("approved_paths", []) or []:
                    self._bump(self._paths, p, now_turn)

    # --- read path -------------------------------------------------------

    def current_intent(self) -> IntentSnapshot:
        with self._lock:
            paths = self._top_decayed(self._paths)
            tools = self._top_decayed(self._tools)
            errors = self._top_decayed(self._errors)
        return IntentSnapshot(
            op_id=self._op_id,
            turn_count=self._turn_count,
            recent_paths=tuple(paths.keys()),
            recent_tools=tuple(tools.keys()),
            recent_error_terms=tuple(errors.keys()),
            weighted_path_scores=paths,
            weighted_tool_scores=tools,
            weighted_error_scores=errors,
        )

    @property
    def op_id(self) -> str:
        return self._op_id

    @property
    def turn_count(self) -> int:
        with self._lock:
            return self._turn_count

    # --- internals -------------------------------------------------------

    def _bump(
        self,
        store: Dict[str, Tuple[float, int]],
        key: str,
        now_turn: int,
    ) -> None:
        cur_weight, last_turn = store.get(key, (0.0, now_turn))
        decayed = cur_weight * _decay_weight(
            now_turn - last_turn, half_life=self._half_life,
        )
        store[key] = (decayed + 1.0, now_turn)

    def _top_decayed(
        self, store: Dict[str, Tuple[float, int]],
    ) -> Dict[str, float]:
        """Apply decay-to-now and return the top N by current weight."""
        now = self._turn_count
        out: Dict[str, float] = {}
        for k, (w, last_turn) in store.items():
            out[k] = w * _decay_weight(
                now - last_turn, half_life=self._half_life,
            )
        # Drop signals whose decayed weight has fallen below a tiny
        # floor — a signal 20+ turns stale is functionally dead.
        cleaned = {k: v for k, v in out.items() if v >= 0.01}
        top = dict(
            sorted(cleaned.items(), key=lambda kv: kv[1], reverse=True)[: self._top_n]
        )
        return top


# ---------------------------------------------------------------------------
# PreservationScorer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChunkCandidate:
    """A chunk of prompt/dialogue the scorer may preserve or drop.

    ``chunk_id`` is whatever the caller uses to identify the chunk in
    its own index (a list position, a UUID, a message id). The scorer
    never mutates it — it's echoed back in :class:`ChunkScore` for the
    caller's bookkeeping.

    ``index_in_sequence`` is the chunk's position in the conversation
    ordering (0 = oldest). The scorer uses this to compute a base
    recency weight; ties break on it so output ordering is stable.
    """

    chunk_id: str
    text: str
    index_in_sequence: int
    role: str = ""     # "user" / "assistant" / "tool" / "system" / ...
    pinned: bool = False


class ChunkDecision(str, enum.Enum):
    KEEP = "keep"
    COMPACT = "compact"
    DROP = "drop"


@dataclass(frozen=True)
class ChunkScore:
    """Detailed score for one :class:`ChunkCandidate`.

    ``breakdown`` lists each signal's contribution; Slice 4's manifest
    projects this verbatim so operators can see WHY a chunk was kept.
    """

    chunk_id: str
    index_in_sequence: int
    base: float           # recency baseline
    intent: float         # intent-match boost
    structural: float     # protected role / structural importance boost
    pin_bonus: float      # infinity if pinned
    total: float
    decision: Optional[ChunkDecision] = None
    breakdown: Tuple[Tuple[str, float], ...] = ()


@dataclass(frozen=True)
class PreservationResult:
    """Full output of :meth:`PreservationScorer.select_preserved`."""

    kept: Tuple[ChunkScore, ...]
    compacted: Tuple[ChunkScore, ...]   # fed into the compactor summary
    dropped: Tuple[ChunkScore, ...]     # dropped entirely (budget overflow)
    total_chars_before: int
    total_chars_after: int


class PreservationScorer:
    """Scores chunks against the current intent + selects a preserved subset.

    Default weights (override via constructor for tests):
        base_recency_weight:      10.0 at newest, decays
        intent_path_weight:       5.0 per matching path (weighted by intent score)
        intent_tool_weight:       3.0 per matching tool
        intent_error_weight:      4.0 per matching error term
        structural_role_weights:  {"system": 8.0, "user": 6.0, "assistant": 2.0,
                                   "tool": 1.0}
        pin_bonus:                math.inf  (pinned chunks always survive)
    """

    def __init__(
        self,
        *,
        base_recency_weight: float = 10.0,
        base_recency_half_life: float = 6.0,
        intent_path_weight: float = 5.0,
        intent_tool_weight: float = 3.0,
        intent_error_weight: float = 4.0,
        structural_role_weights: Optional[Dict[str, float]] = None,
        pin_bonus: float = math.inf,
    ) -> None:
        self._base_w = base_recency_weight
        self._base_half_life = base_recency_half_life
        self._path_w = intent_path_weight
        self._tool_w = intent_tool_weight
        self._error_w = intent_error_weight
        self._role_w = structural_role_weights or {
            "system": 8.0, "user": 6.0, "assistant": 2.0, "tool": 1.0,
            "error": 5.0,
        }
        self._pin_bonus = pin_bonus

    # --- scoring ---------------------------------------------------------

    def score(
        self,
        chunk: ChunkCandidate,
        intent: IntentSnapshot,
        *,
        newest_index: int,
    ) -> ChunkScore:
        """Produce a :class:`ChunkScore` for one chunk."""
        # Base recency: newest chunk gets full weight; older chunks decay.
        turns_since = max(0, newest_index - chunk.index_in_sequence)
        base = self._base_w * _decay_weight(
            turns_since, half_life=self._base_half_life,
        )

        # Intent match: paths + tools + error terms
        paths_in_chunk = set(extract_paths(chunk.text))
        tools_in_chunk = set(extract_tool_mentions(chunk.text))
        errors_in_chunk = set(extract_error_terms(chunk.text))

        intent_score = 0.0
        path_hits: List[Tuple[str, float]] = []
        for p in paths_in_chunk:
            weight = intent.weighted_path_scores.get(p, 0.0)
            if weight > 0.0:
                contribution = self._path_w * weight
                intent_score += contribution
                path_hits.append((p, contribution))
        tool_hits: List[Tuple[str, float]] = []
        for t in tools_in_chunk:
            weight = intent.weighted_tool_scores.get(t, 0.0)
            if weight > 0.0:
                contribution = self._tool_w * weight
                intent_score += contribution
                tool_hits.append((t, contribution))
        error_hits: List[Tuple[str, float]] = []
        for term in errors_in_chunk:
            weight = intent.weighted_error_scores.get(term, 0.0)
            if weight > 0.0:
                contribution = self._error_w * weight
                intent_score += contribution
                error_hits.append((term, contribution))

        # Structural importance: by role (system > user > assistant > tool).
        structural = self._role_w.get(chunk.role, 0.0)

        # Pin — infinite bonus.
        pin = self._pin_bonus if chunk.pinned else 0.0

        total = base + intent_score + structural + pin

        breakdown: List[Tuple[str, float]] = [
            ("base_recency", base),
            ("structural_role", structural),
        ]
        for k, v in path_hits:
            breakdown.append((f"intent_path:{k}", v))
        for k, v in tool_hits:
            breakdown.append((f"intent_tool:{k}", v))
        for k, v in error_hits:
            breakdown.append((f"intent_error:{k}", v))
        if pin:
            breakdown.append(("pin_bonus", pin))

        return ChunkScore(
            chunk_id=chunk.chunk_id,
            index_in_sequence=chunk.index_in_sequence,
            base=base,
            intent=intent_score,
            structural=structural,
            pin_bonus=pin,
            total=total,
            breakdown=tuple(breakdown),
        )

    # --- selection -------------------------------------------------------

    def select_preserved(
        self,
        candidates: Iterable[ChunkCandidate],
        intent: IntentSnapshot,
        *,
        max_chars: Optional[int] = None,
        max_chunks: Optional[int] = None,
        keep_ratio: float = 0.5,
    ) -> PreservationResult:
        """Apply budget + score to pick winners / compaction pool / drops.

        Algorithm:
          1. Score every candidate.
          2. Sort by ``total`` descending, breaking ties on recency
             (index_in_sequence descending), then on chunk_id for
             determinism.
          3. Walk the sorted list; accumulate into ``kept`` until the
             budget (chars or chunks) is exhausted.
          4. Pinned chunks are ALWAYS kept, regardless of budget.
          5. The next ``keep_ratio`` fraction (by chunks) goes to
             ``compacted``; the rest to ``dropped``.

        Returns a :class:`PreservationResult` with full decision
        breakdowns so the Slice 4 manifest can show exactly what
        happened to each chunk.
        """
        candidate_list = list(candidates)
        if not candidate_list:
            return PreservationResult(
                kept=(), compacted=(), dropped=(),
                total_chars_before=0, total_chars_after=0,
            )
        newest = max(c.index_in_sequence for c in candidate_list)
        scored: List[ChunkScore] = [
            self.score(c, intent, newest_index=newest)
            for c in candidate_list
        ]
        chunk_by_id: Dict[str, ChunkCandidate] = {
            c.chunk_id: c for c in candidate_list
        }

        # Determinism: sort DESC by total, tie-break DESC by index_in_sequence,
        # then ASC by chunk_id.
        def _key(s: ChunkScore) -> Tuple[float, int, str]:
            return (-s.total, -s.index_in_sequence, s.chunk_id)

        scored.sort(key=_key)

        total_chars_before = sum(
            len(chunk_by_id[s.chunk_id].text) for s in scored
        )

        kept: List[ChunkScore] = []
        chars_used = 0
        chunks_used = 0

        # Pass 1: all pinned chunks automatically kept (regardless of budget)
        pinned_ids = {
            c.chunk_id for c in candidate_list if c.pinned
        }
        remaining: List[ChunkScore] = []
        for s in scored:
            if s.chunk_id in pinned_ids:
                kept.append(ChunkScore(
                    **{**s.__dict__, "decision": ChunkDecision.KEEP},
                ))
                chars_used += len(chunk_by_id[s.chunk_id].text)
                chunks_used += 1
            else:
                remaining.append(s)

        # Pass 2: fill budget with top-scored non-pinned chunks
        for s in remaining:
            chunk_len = len(chunk_by_id[s.chunk_id].text)
            if max_chunks is not None and chunks_used >= max_chunks:
                remaining = remaining[remaining.index(s):]
                break
            if max_chars is not None and chars_used + chunk_len > max_chars:
                remaining = remaining[remaining.index(s):]
                break
            kept.append(ChunkScore(
                **{**s.__dict__, "decision": ChunkDecision.KEEP},
            ))
            chars_used += chunk_len
            chunks_used += 1
        else:
            remaining = []

        # Pass 3: partition the leftover into compacted / dropped
        cut = int(round(len(remaining) * max(0.0, min(1.0, keep_ratio))))
        compacted_scores = remaining[:cut]
        dropped_scores = remaining[cut:]
        compacted = tuple(
            ChunkScore(**{**s.__dict__, "decision": ChunkDecision.COMPACT})
            for s in compacted_scores
        )
        dropped = tuple(
            ChunkScore(**{**s.__dict__, "decision": ChunkDecision.DROP})
            for s in dropped_scores
        )

        # Re-sort kept / compacted / dropped by index_in_sequence for
        # callers that want chronological ordering.
        kept.sort(key=lambda s: s.index_in_sequence)

        return PreservationResult(
            kept=tuple(kept),
            compacted=compacted,
            dropped=dropped,
            total_chars_before=total_chars_before,
            total_chars_after=chars_used,
        )


# ---------------------------------------------------------------------------
# Registry (per-op singletons)
# ---------------------------------------------------------------------------


class IntentTrackerRegistry:
    """Per-op tracker lookup with bounded retention. Same pattern as
    :class:`ContextLedgerRegistry`."""

    def __init__(self, *, max_ops: int = 64) -> None:
        self._lock = threading.Lock()
        self._trackers: Dict[str, IntentTracker] = {}
        self._max_ops = max(4, max_ops)

    def get_or_create(self, op_id: str) -> IntentTracker:
        if not op_id:
            raise ValueError("op_id must be non-empty")
        with self._lock:
            tracker = self._trackers.get(op_id)
            if tracker is not None:
                return tracker
            if len(self._trackers) >= self._max_ops:
                oldest = next(iter(self._trackers))
                self._trackers.pop(oldest)
            fresh = IntentTracker(op_id)
            self._trackers[op_id] = fresh
        return fresh

    def get(self, op_id: str) -> Optional[IntentTracker]:
        with self._lock:
            return self._trackers.get(op_id)

    def drop(self, op_id: str) -> bool:
        with self._lock:
            return self._trackers.pop(op_id, None) is not None

    def reset(self) -> None:
        with self._lock:
            self._trackers.clear()


_default_tracker_registry: Optional[IntentTrackerRegistry] = None
_tracker_registry_lock = threading.Lock()


def get_default_tracker_registry() -> IntentTrackerRegistry:
    global _default_tracker_registry
    with _tracker_registry_lock:
        if _default_tracker_registry is None:
            _default_tracker_registry = IntentTrackerRegistry()
        return _default_tracker_registry


def reset_default_tracker_registry() -> None:
    global _default_tracker_registry
    with _tracker_registry_lock:
        if _default_tracker_registry is not None:
            _default_tracker_registry.reset()
        _default_tracker_registry = None


def intent_tracker_for(op_id: str) -> IntentTracker:
    return get_default_tracker_registry().get_or_create(op_id)


__all__ = [
    "ChunkCandidate",
    "ChunkDecision",
    "ChunkScore",
    "INTENT_TRACKER_SCHEMA_VERSION",
    "IntentSnapshot",
    "IntentTracker",
    "IntentTrackerRegistry",
    "PreservationResult",
    "PreservationScorer",
    "TurnSource",
    "extract_error_terms",
    "extract_paths",
    "extract_tool_mentions",
    "get_default_tracker_registry",
    "intent_tracker_for",
    "reset_default_tracker_registry",
]

_ = Callable  # silence unused-import guard
