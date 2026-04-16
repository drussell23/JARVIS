"""SemanticIndex — local, bounded, non-authoritative semantic goal inference.

Moves O+V from *goal declaration* (4 YAML goals + git-theme histogram +
keyword matching) to *goal inference*: a recency-weighted semantic
centroid over recent commits + active goals + recent conversation, and
cosine similarity between new intake signals and that centroid.

The score feeds two surfaces only:
  1. Intake-time priority bias — capped at ``BOOST_MAX=1`` so it remains
     strictly subordinate to ``goal_alignment_boost`` (=2).
  2. CONTEXT_EXPANSION prompt subsection — top-K nearest-neighbor corpus
     items rendered as untrusted context (no raw scores leaked).

Authority invariant (mirrors ConversationBridge §9):
  The output of this module is consumed **only** by the intake priority
  formula and by StrategicDirection at CONTEXT_EXPANSION. It has **zero**
  authority over Iron Gate, UrgencyRouter, risk-tier escalation, policy
  engine, FORBIDDEN_PATH matching, ToolExecutor protected-path checks,
  or approval gating.

Manifesto alignment:
  * §1 (Boundary Principle) — soft semantic prior, not execution authority
  * §4 (Privacy Shield / Data Sovereignty) — local embedder, no external API
  * §5 (Tier 1-ish interpretation, NOT Tier -1 Semantic Firewall — v5
    reconciliation is a separate track)
  * §8 (Observability) — hashes + counts + shapes, never raw vectors

Design references:
  * §12.3 — POSTMORTEM excluded from centroid by default (failure-gravity
    avoidance); surfaced as separate "### Recent friction / closures"
    prompt subsection instead.
  * §12.4 — Conversation turns included in centroid with shorter 3-day
    halflife (vs 14-day for commits/goals).
  * §12.5 — Boot + interval refresh only in V1; HEAD-change debounce V1.1.

Dependency direction (beef #3):
  semantic_index.py -->  conversation_bridge.py  (snapshot reader)
                    -->  strategic_direction.py  (GoalTracker reader)
  ConversationBridge must NOT import this module — enforced by placing
  the bridge-reading code here, never the reverse.
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from backend.core.secure_logging import sanitize_for_log

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Env configuration
# ---------------------------------------------------------------------------


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, int(raw))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, float(raw))
    except (TypeError, ValueError):
        return default


def _is_enabled() -> bool:
    """Master switch. Off → no import, no disk I/O, no fastembed touch."""
    return _env_bool("JARVIS_SEMANTIC_INFERENCE_ENABLED", False)


def _prompt_injection_enabled() -> bool:
    """Sub-gate for the CONTEXT_EXPANSION prompt subsection (§12.1)."""
    return _env_bool("JARVIS_SEMANTIC_PROMPT_INJECTION_ENABLED", True)


def _embedder_name() -> str:
    return os.environ.get("JARVIS_SEMANTIC_EMBEDDER", "fastembed").strip().lower()


def _halflife_days() -> float:
    return _env_float("JARVIS_SEMANTIC_HALFLIFE_DAYS", 14.0, minimum=0.1)


def _conversation_halflife_days() -> float:
    return _env_float("JARVIS_SEMANTIC_CONVERSATION_HALFLIFE_DAYS", 3.0, minimum=0.1)


def _max_items() -> int:
    return max(1, _env_int("JARVIS_SEMANTIC_MAX_ITEMS", 50, minimum=1))


def _refresh_s() -> float:
    return float(max(1, _env_int("JARVIS_SEMANTIC_REFRESH_S", 3600, minimum=1)))


def _boost_max() -> int:
    return max(0, _env_int("JARVIS_SEMANTIC_ALIGNMENT_BOOST_MAX", 1, minimum=0))


def _prompt_top_k() -> int:
    return max(0, _env_int("JARVIS_SEMANTIC_PROMPT_TOP_K", 3, minimum=0))


def _postmortem_in_centroid() -> bool:
    """§12.3: default false — postmortems are prompt-only (failure gravity)."""
    return _env_bool("JARVIS_SEMANTIC_POSTMORTEM_IN_CENTROID", False)


def _cache_enabled() -> bool:
    return _env_bool("JARVIS_SEMANTIC_INDEX_PERSIST", True)


def _git_log_limit() -> int:
    return max(1, _env_int("JARVIS_SEMANTIC_GIT_LOG_N", 30, minimum=1))


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

# Corpus sources — the string labels go into logs + into cache files.
SOURCE_GIT_COMMIT = "git_commit"
SOURCE_GOAL = "goal"
SOURCE_CONVERSATION = "conversation"
SOURCE_POSTMORTEM = "postmortem"

_CENTROID_DIM_PLACEHOLDER = 384  # bge-small-en-v1.5 dim; real dim set at embed time


@dataclass(frozen=True)
class CorpusItem:
    """One item in the semantic corpus. Immutable after assembly.

    ``halflife_days`` per-item so conversation items decay faster than
    commits/goals (§12.4). ``ts`` is a Unix epoch; recency weight at
    scoring time is ``0.5 ** (age_days / halflife_days)``.
    """

    text: str
    source: str  # SOURCE_* constant
    ts: float
    halflife_days: float = 14.0


@dataclass
class IndexStats:
    """Counters snapshot. Never contains content or vectors."""

    built_at: float = 0.0
    corpus_n: int = 0
    build_ms: float = 0.0
    centroid_hash8: str = ""
    refreshes: int = 0
    signals_scored: int = 0
    embed_failures: int = 0
    by_source: Dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Pre-embed sanitizer (beef #2)
# ---------------------------------------------------------------------------


def _sanitize_corpus_text(text: str, max_len: int = 512) -> str:
    """Strip control chars + cap length before embedding.

    Also applies the ConversationBridge secret-shape redaction — git
    commit messages aren't inherently safe (a developer may paste a
    token into a commit subject). Delegates to the bridge's redaction
    to avoid duplicating the regex set.
    """
    if not isinstance(text, str) or not text:
        return ""
    cleaned = sanitize_for_log(text, max_len=max_len)
    if not cleaned:
        return ""
    # Apply the bridge's secret-shape redaction. Local import to respect
    # the dependency direction rule (beef #3): semantic_index imports
    # from bridge, never the reverse.
    try:
        from backend.core.ouroboros.governance.conversation_bridge import (
            _redact_secrets,
        )
        cleaned, _ = _redact_secrets(cleaned)
    except Exception:
        pass
    return cleaned


# ---------------------------------------------------------------------------
# Embedder — lazy fastembed import with graceful disable
# ---------------------------------------------------------------------------


class _Embedder:
    """Wraps fastembed's TextEmbedding with a master-off no-import contract.

    Construction does NOT import fastembed. The first call to
    :meth:`embed` lazily imports. If the import fails (package not
    installed), the embedder silently transitions to disabled — callers
    see ``None`` returns and can short-circuit.
    """

    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5") -> None:
        self._model_name = model_name
        self._model: Optional[Any] = None
        self._disabled: bool = False
        self._lock = threading.Lock()

    @property
    def disabled(self) -> bool:
        return self._disabled

    @property
    def model_name(self) -> str:
        return self._model_name

    def _lazy_init(self) -> bool:
        """Import fastembed on first use. Returns True if ready."""
        if self._model is not None:
            return True
        if self._disabled:
            return False
        with self._lock:
            if self._model is not None:
                return True
            if self._disabled:
                return False
            try:
                from fastembed import TextEmbedding  # type: ignore[import-not-found]
                self._model = TextEmbedding(model_name=self._model_name)
                logger.info(
                    "[SemanticIndex] fastembed loaded: model=%s",
                    self._model_name,
                )
                return True
            except Exception as exc:
                self._disabled = True
                logger.warning(
                    "[SemanticIndex] fastembed unavailable (%s) — "
                    "semantic inference disabled until dep installed",
                    exc.__class__.__name__,
                )
                return False

    def embed(self, texts: Sequence[str]) -> Optional[List[List[float]]]:
        """Return one vector per input text, or ``None`` when disabled.

        Vectors are returned as plain Python lists (not NumPy arrays) so
        the rest of the module has no hard NumPy dependency at type
        level. Cosine arithmetic below uses the lists directly.
        """
        if not texts:
            return []
        if not self._lazy_init():
            return None
        try:
            # fastembed's embed() returns a generator of numpy arrays.
            out = list(self._model.embed(list(texts)))  # type: ignore[union-attr]
            return [list(map(float, v)) for v in out]
        except Exception:
            logger.debug("[SemanticIndex] embed() failed", exc_info=True)
            return None


# ---------------------------------------------------------------------------
# Vector math — inlined to avoid NumPy dep at module level
# ---------------------------------------------------------------------------


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Plain-Python cosine similarity. Returns 0.0 on zero-norm."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


def _weighted_centroid(
    vectors: Sequence[Sequence[float]],
    weights: Sequence[float],
) -> List[float]:
    """Compute Σ(w_i · v_i) / Σ(w_i). Returns empty list on empty input."""
    if not vectors or not weights or len(vectors) != len(weights):
        return []
    total = sum(max(0.0, w) for w in weights)
    if total <= 0.0:
        return []
    dim = len(vectors[0])
    acc = [0.0] * dim
    for v, w in zip(vectors, weights):
        if w <= 0.0 or len(v) != dim:
            continue
        for i, x in enumerate(v):
            acc[i] += x * w
    return [x / total for x in acc]


def _recency_weight(age_s: float, halflife_days: float) -> float:
    """0.5 ** (age_days / halflife). Clamped to [0, 1]."""
    if halflife_days <= 0 or age_s < 0:
        return 1.0
    age_days = age_s / 86400.0
    return 0.5 ** (age_days / halflife_days)


# ---------------------------------------------------------------------------
# Corpus assembler (deterministic, zero model inference)
# ---------------------------------------------------------------------------


def _assemble_corpus(
    project_root: Path,
    *,
    git_limit: int,
    max_items: int,
) -> List[CorpusItem]:
    """Pull from git / GoalTracker / ConversationBridge, sanitize, cap."""
    items: List[CorpusItem] = []
    now = time.time()
    halflife_default = _halflife_days()
    halflife_conv = _conversation_halflife_days()
    include_pm_in_centroid = _postmortem_in_centroid()

    # --- Git commits (subject lines) ---
    try:
        result = subprocess.run(
            ["git", "log", f"-{git_limit}", "--pretty=format:%ct|%s"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if "|" not in line:
                    continue
                ts_s, subj = line.split("|", 1)
                try:
                    ts = float(ts_s)
                except ValueError:
                    continue
                cleaned = _sanitize_corpus_text(subj)
                if cleaned:
                    items.append(CorpusItem(
                        text=cleaned, source=SOURCE_GIT_COMMIT,
                        ts=ts, halflife_days=halflife_default,
                    ))
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        logger.debug("[SemanticIndex] git log unavailable", exc_info=True)

    # --- GoalTracker active goals ---
    try:
        from backend.core.ouroboros.governance.strategic_direction import (
            GoalTracker,
        )
        tracker = GoalTracker(project_root)
        for goal in tracker.active_goals:
            desc = f"{goal.description} — keywords: {' '.join(goal.keywords[:5])}"
            cleaned = _sanitize_corpus_text(desc)
            if cleaned:
                items.append(CorpusItem(
                    text=cleaned, source=SOURCE_GOAL,
                    ts=goal.updated_at or now,
                    halflife_days=halflife_default,
                ))
    except Exception:
        logger.debug("[SemanticIndex] GoalTracker unavailable", exc_info=True)

    # --- ConversationBridge recent turns (shorter halflife §12.4) ---
    try:
        from backend.core.ouroboros.governance.conversation_bridge import (
            get_default_bridge,
            SOURCE_POSTMORTEM as BRIDGE_POSTMORTEM,
        )
        bridge = get_default_bridge()
        for turn in bridge.snapshot():
            cleaned = _sanitize_corpus_text(turn.text)
            if not cleaned:
                continue
            if turn.source == BRIDGE_POSTMORTEM:
                # §12.3: postmortem default-excluded from centroid. Still
                # captured here so the prompt subsection renderer can
                # find them later — but only under the centroid-include
                # env override do they get the "conversation" halflife
                # that makes them centroid-material.
                if not include_pm_in_centroid:
                    items.append(CorpusItem(
                        text=cleaned, source=SOURCE_POSTMORTEM,
                        ts=turn.ts, halflife_days=halflife_conv,
                    ))
                    continue
                # Override path: treat as conversation-rate contributor.
                items.append(CorpusItem(
                    text=cleaned, source=SOURCE_POSTMORTEM,
                    ts=turn.ts, halflife_days=halflife_conv,
                ))
            else:
                items.append(CorpusItem(
                    text=cleaned, source=SOURCE_CONVERSATION,
                    ts=turn.ts, halflife_days=halflife_conv,
                ))
    except Exception:
        logger.debug("[SemanticIndex] ConversationBridge unavailable", exc_info=True)

    # Cap total (most recent wins — sort by ts descending, trim).
    items.sort(key=lambda it: it.ts, reverse=True)
    return items[:max_items]


# ---------------------------------------------------------------------------
# SemanticIndex
# ---------------------------------------------------------------------------


class SemanticIndex:
    """Local, bounded semantic goal inference over recent work.

    Lifecycle:
      * ``build()`` — assemble corpus, embed, compute centroid. Idempotent.
        Safe to call from multiple threads.
      * ``score(text)`` — embed ``text`` and cosine against centroid.
      * ``boost_for(text)`` — convenience: ``score → clamp(0, BOOST_MAX)``.
      * ``format_prompt_sections()`` — subsection pair for StrategicDirection.

    Disabled states (any produces no-op behavior, no disk I/O):
      * Master switch off (``JARVIS_SEMANTIC_INFERENCE_ENABLED=false``)
      * fastembed import fails on first embed
      * Corpus empty (no git history, no goals, no conversation)
    """

    def __init__(self, project_root: Path) -> None:
        self._root = Path(project_root).resolve()
        self._embedder = _Embedder()
        self._lock = threading.RLock()
        self._stats = IndexStats()
        self._corpus: List[CorpusItem] = []
        self._corpus_centroid_members: List[CorpusItem] = []  # subset eligible for centroid
        self._vectors: List[List[float]] = []
        self._centroid_vectors_subset: List[List[float]] = []  # matches centroid-members
        self._centroid: List[float] = []
        self._built_at: float = 0.0

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def build(self, *, force: bool = False) -> bool:
        """Rebuild the corpus + centroid. Returns True if built/refreshed.

        Honors the refresh interval unless ``force=True``. Never raises —
        failures log at DEBUG and leave the prior index (if any) in place.
        """
        if not _is_enabled():
            return False
        now = time.time()
        if not force and self._built_at > 0:
            if (now - self._built_at) < _refresh_s():
                return False
        t0 = time.monotonic()
        try:
            items = _assemble_corpus(
                self._root,
                git_limit=_git_log_limit(),
                max_items=_max_items(),
            )
            if not items:
                with self._lock:
                    self._corpus = []
                    self._vectors = []
                    self._centroid = []
                    self._built_at = now
                    self._stats.built_at = now
                    self._stats.corpus_n = 0
                    self._stats.build_ms = (time.monotonic() - t0) * 1000.0
                    self._stats.centroid_hash8 = ""
                    self._stats.refreshes += 1
                    self._stats.by_source = {}
                return True

            texts = [it.text for it in items]
            vectors = self._embedder.embed(texts)
            if vectors is None or len(vectors) != len(items):
                # Embedder disabled — keep prior state, but mark a stat bump.
                with self._lock:
                    self._stats.embed_failures += 1
                return False

            # Centroid membership rule (§12.3 default):
            # Include: git_commit, goal, conversation.
            # Exclude: postmortem (unless override env).
            include_pm = _postmortem_in_centroid()
            centroid_members: List[CorpusItem] = []
            centroid_vectors: List[List[float]] = []
            for it, vec in zip(items, vectors):
                if it.source == SOURCE_POSTMORTEM and not include_pm:
                    continue
                centroid_members.append(it)
                centroid_vectors.append(vec)

            weights: List[float] = []
            for it in centroid_members:
                age_s = max(0.0, now - it.ts)
                weights.append(_recency_weight(age_s, it.halflife_days))

            centroid = _weighted_centroid(centroid_vectors, weights)
            hash8 = ""
            if centroid:
                hash_src = ",".join(f"{x:.6f}" for x in centroid[:16])
                hash8 = hashlib.sha256(hash_src.encode("utf-8")).hexdigest()[:8]

            by_source: Dict[str, int] = {}
            for it in items:
                by_source[it.source] = by_source.get(it.source, 0) + 1

            with self._lock:
                self._corpus = items
                self._vectors = vectors
                self._corpus_centroid_members = centroid_members
                self._centroid_vectors_subset = centroid_vectors
                self._centroid = centroid
                self._built_at = now
                self._stats.built_at = now
                self._stats.corpus_n = len(items)
                self._stats.build_ms = (time.monotonic() - t0) * 1000.0
                self._stats.centroid_hash8 = hash8
                self._stats.refreshes += 1
                self._stats.by_source = by_source

            logger.info(
                "[SemanticIndex] built_at=%.0f corpus_n=%d embedder=%s "
                "centroid_hash8=%s halflife_days=%.1f build_ms=%.0f",
                now, len(items),
                f"fastembed-{self._embedder.model_name.split('/')[-1]}",
                hash8, _halflife_days(), self._stats.build_ms,
            )

            if _cache_enabled():
                self._persist_cache_safe()
            return True
        except Exception:
            logger.debug("[SemanticIndex] build failed", exc_info=True)
            return False

    def _persist_cache_safe(self) -> None:
        """Best-effort cache to .jarvis/semantic_index.npz."""
        try:
            import numpy as np  # optional; fastembed pulls it in transitively
        except Exception:
            return
        try:
            cache_dir = self._root / ".jarvis"
            cache_dir.mkdir(parents=True, exist_ok=True)
            path = cache_dir / "semantic_index.npz"
            vecs = np.array(self._vectors, dtype="float32") if self._vectors else np.zeros((0, 0), dtype="float32")
            centroid = np.array(self._centroid, dtype="float32") if self._centroid else np.zeros((0,), dtype="float32")
            texts = np.array([it.text for it in self._corpus], dtype=object)
            sources = np.array([it.source for it in self._corpus], dtype=object)
            tss = np.array([it.ts for it in self._corpus], dtype="float64")
            halflives = np.array([it.halflife_days for it in self._corpus], dtype="float32")
            np.savez(
                path,
                vectors=vecs, centroid=centroid,
                texts=texts, sources=sources,
                ts=tss, halflives=halflives,
                built_at=np.array([self._built_at], dtype="float64"),
            )
        except Exception:
            logger.debug("[SemanticIndex] cache write failed", exc_info=True)

    # ------------------------------------------------------------------
    # Score
    # ------------------------------------------------------------------

    def score(self, text: str) -> float:
        """Cosine similarity of ``text`` against the active centroid.

        Returns 0.0 when disabled, no centroid, or embedder fails.
        Range is [-1, 1] but for intake callers we clamp to [0, 1] at
        :meth:`boost_for` — negative values mean "orthogonal to active
        theme" and shouldn't produce negative priority boost.
        """
        if not _is_enabled():
            return 0.0
        with self._lock:
            centroid = list(self._centroid)
        if not centroid:
            return 0.0
        cleaned = _sanitize_corpus_text(text)
        if not cleaned:
            return 0.0
        vec = self._embedder.embed([cleaned])
        if not vec:
            return 0.0
        sim = _cosine(vec[0], centroid)
        with self._lock:
            self._stats.signals_scored += 1
        return sim

    def boost_for(self, text: str) -> int:
        """Clamp the cosine score to a non-negative integer priority boost.

        Stays strictly subordinate to ``goal_alignment_boost`` because
        ``BOOST_MAX=1`` by default (§12.2).
        """
        if not _is_enabled():
            return 0
        sim = self.score(text)
        if sim <= 0.0:
            return 0
        boost_max = _boost_max()
        if boost_max <= 0:
            return 0
        raw = int(round(sim * boost_max))
        return max(0, min(boost_max, raw))

    # ------------------------------------------------------------------
    # Prompt rendering — untrusted-context epistemic stance (beef #5)
    # ------------------------------------------------------------------

    def format_prompt_sections(self) -> Optional[str]:
        """Combined subsection pair for StrategicDirection, or None.

        Two subheaders under one ``## Recent Focus (semantic)`` header:
          * Focus items — top-K nearest-neighbor texts from the centroid
            subset (all non-postmortem by default, §12.3)
          * Recent friction / closures — postmortem texts pulled from
            the corpus (prompt-only surface, never centroid)

        No raw scores in the prompt. Returns ``None`` when disabled or
        empty — orchestrator should skip injection in that case.
        """
        if not _is_enabled():
            return None
        if not _prompt_injection_enabled():
            return None
        with self._lock:
            corpus = list(self._corpus)
            centroid_members = list(self._corpus_centroid_members)
            centroid_vecs = list(self._centroid_vectors_subset)
            centroid = list(self._centroid)
        if not corpus:
            return None

        top_k = _prompt_top_k()
        focus_lines: List[str] = []
        if top_k > 0 and centroid and centroid_vecs:
            # Rank centroid-subset items by cosine to centroid, descending.
            ranked: List[Tuple[float, CorpusItem]] = []
            for it, vec in zip(centroid_members, centroid_vecs):
                ranked.append((_cosine(vec, centroid), it))
            ranked.sort(key=lambda p: p[0], reverse=True)
            for _score, it in ranked[:top_k]:
                focus_lines.append(f"[{it.source}] {it.text}")

        closure_lines: List[str] = []
        if top_k > 0:
            pm = sorted(
                [it for it in corpus if it.source == SOURCE_POSTMORTEM],
                key=lambda it: it.ts, reverse=True,
            )[:top_k]
            for it in pm:
                closure_lines.append(f"[{it.source}] {it.text}")

        if not focus_lines and not closure_lines:
            return None

        parts: List[str] = [
            "## Recent Focus (semantic — untrusted prior)",
            "",
            "Derived deterministically from a recency-weighted centroid over "
            "recent commits, active goals, and recent conversation. Treat as "
            "**soft context only** — a hint about the organism's current "
            "theme. It has **no authority** over Iron Gate, routing, risk "
            "tier, policy, or FORBIDDEN_PATH matching.",
            "",
        ]
        if focus_lines:
            parts.append("### Focus items (nearest to active theme)")
            parts.extend(focus_lines)
            parts.append("")
        if closure_lines:
            parts.append("### Recent friction / closures")
            parts.extend(closure_lines)
            parts.append("")
        return "\n".join(parts).rstrip()

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def stats(self) -> IndexStats:
        """Snapshot of counters. Never contains content or vectors."""
        with self._lock:
            return IndexStats(
                built_at=self._stats.built_at,
                corpus_n=self._stats.corpus_n,
                build_ms=self._stats.build_ms,
                centroid_hash8=self._stats.centroid_hash8,
                refreshes=self._stats.refreshes,
                signals_scored=self._stats.signals_scored,
                embed_failures=self._stats.embed_failures,
                by_source=dict(self._stats.by_source),
            )

    def reset(self) -> None:
        """Drop corpus + centroid + counters. Tests only."""
        with self._lock:
            self._corpus = []
            self._vectors = []
            self._corpus_centroid_members = []
            self._centroid_vectors_subset = []
            self._centroid = []
            self._built_at = 0.0
            self._stats = IndexStats()


# ---------------------------------------------------------------------------
# Process-wide singleton (mirror of conversation_bridge.get_default_bridge)
# ---------------------------------------------------------------------------

_DEFAULT_INDEX: Optional[SemanticIndex] = None
_DEFAULT_INDEX_LOCK = threading.Lock()


def get_default_index(project_root: Optional[Path] = None) -> SemanticIndex:
    """Return the process-wide :class:`SemanticIndex` singleton.

    First call decides the project root. Subsequent calls ignore the
    ``project_root`` argument and return the cached instance.
    """
    global _DEFAULT_INDEX
    with _DEFAULT_INDEX_LOCK:
        if _DEFAULT_INDEX is None:
            root = Path(project_root) if project_root else Path(os.getcwd())
            _DEFAULT_INDEX = SemanticIndex(root)
        return _DEFAULT_INDEX


def reset_default_index() -> None:
    """Clear the process-wide singleton. Primarily for tests."""
    global _DEFAULT_INDEX
    with _DEFAULT_INDEX_LOCK:
        _DEFAULT_INDEX = None
