"""Tier 2 — Semantic Consolidation Matrix: the neuroplasticity loop's compressor.

The learning half of the Anticipatory Edge-Case Armor. Live-fire failures and Guardian
hard-findings stream in as :class:`Lesson`s. When N *structurally similar* lessons
accumulate, the matrix distills them into a single high-weight CORE_DIRECTIVE persisted to
the UserPreferenceStore (as a STYLE memory — injected into every generation) and purges the
now-redundant episodic records. This keeps episodic memory from bloating the 16 GB
unified-memory footprint / LLM context while *promoting* the lesson from transient to durable.

Manifesto §6 (neuroplasticity): the organism learns its own blindspots from runtime failure
instead of us hard-coding every rule. The LiveKernelValidator is the teacher; this is how the
student writes the lesson down once and forgets the noise.

Design discipline (matches the rest of the engine):
  * Decoupled + duck-typed: ``store`` needs only ``.add(memory_type, name, description, ...)``;
    ``purge`` is any ``Callable[[str], int]`` taking the consolidated cluster fingerprint and
    returning how many backing episodes it retired. No hard import of the heavy store, so it
    is unit-testable with fakes and OFF-inert.
  * Default OFF (``JARVIS_SEMANTIC_CONSOLIDATION_ENABLED``). Fail-soft — ``record`` never
    raises into its caller.
  * Bounded memory: at most ``max_clusters`` fingerprints, each holding at most ``threshold``
    lessons → O(max_clusters × threshold) retained, never unbounded.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Sequence

logger = logging.getLogger("ouroboros.semantic_consolidation")

_ENV_ENABLED = "JARVIS_SEMANTIC_CONSOLIDATION_ENABLED"
_ENV_THRESHOLD = "JARVIS_CONSOLIDATION_THRESHOLD"
_ENV_MAX_CLUSTERS = "JARVIS_CONSOLIDATION_MAX_CLUSTERS"
_TRUTHY = {"1", "true", "yes", "on"}

# Normalization patterns — strip the parts that vary between two instances of the SAME
# structural failure so their fingerprints collide.
_RE_HEXADDR = re.compile(r"0x[0-9a-fA-F]+")
_RE_QUOTED = re.compile(r"""(['"]).*?\1""")
_RE_PATH = re.compile(r"(/[\w.\-]+)+|[A-Za-z]:\\[\\\w.\-]+")
_RE_NUM = re.compile(r"\b\d+\b")
_RE_WS = re.compile(r"\s+")
_RE_LINENO = re.compile(r"line\s+\d+", re.IGNORECASE)


def consolidation_enabled() -> bool:
    return os.environ.get(_ENV_ENABLED, "0").strip().lower() in _TRUTHY


@dataclass(frozen=True)
class Lesson:
    """One recurring-failure signal fed to the matrix.

    kind:        coarse family, e.g. "live_fire" / "guardian".
    signature:   the raw failure text (exception line, message). Fingerprinted internally.
    file_path:   optional file the failure relates to (used for directive path-scoping).
    episode_id:  optional id of the backing episodic record (purged on consolidation).
    """

    signature: str
    kind: str = "live_fire"
    file_path: str = ""
    episode_id: str = ""


def fingerprint(signature: str) -> str:
    """Structural key: lower-cased text with quoted strings, paths, hex addrs, line refs and
    bare numbers elided, so two instances of the same failure shape collapse to one key."""
    s = (signature or "").strip()
    s = _RE_QUOTED.sub("'_'", s)
    s = _RE_LINENO.sub("line _", s)
    s = _RE_HEXADDR.sub("_", s)
    s = _RE_PATH.sub("_", s)
    s = _RE_NUM.sub("_", s)
    s = _RE_WS.sub(" ", s).strip().lower()
    return s


# Principle extraction — recognise known exception families to write a useful directive.
_PRINCIPLES: Sequence = (
    ("frozeninstanceerror",
     "Frozen/immutable instances cannot be mutated. Produce a NEW value with "
     "dataclasses.replace(obj, field=...) (or NamedTuple._replace / pydantic model_copy) "
     "and rebind — never `obj.field = ...`."),
    ("nonetype",
     "Guard Optional values before use: check `if x is not None:` (or narrow with an early "
     "return) before any attribute access, subscript, or coercion."),
    ("modulenotfounderror",
     "Verify the import path and that the symbol exists before referencing it; a patch must "
     "import cleanly under the real interpreter, not just parse."),
    ("importerror",
     "Verify the import path and that the symbol exists before referencing it; a patch must "
     "import cleanly under the real interpreter, not just parse."),
    ("attributeerror",
     "Confirm attribute/method names against the actual class definition before access. Dict "
     "candidates use [] / .get(); only objects use attribute access."),
    ("keyerror",
     "Access dict keys defensively with .get(key, default) unless the key is guaranteed; "
     "validate the shape before indexing."),
    ("typeerror",
     "Check argument counts/types against the real signature before calling; functions that "
     "need arguments must not be invoked argument-free."),
)


_NONE_PRINCIPLE = (
    "Guard Optional values before use: check `if x is not None:` (or narrow with an early "
    "return) before any attribute access, subscript, or coercion."
)


def _principle_for(fp: str, raw: str = "") -> str:
    # None-deref is the most specific runtime hint, but tracebacks QUOTE 'NoneType' so it is
    # normalized out of the fingerprint — detect it from the raw signature, prioritized.
    if "nonetype" in (raw or "").lower() or "nonetype" in fp:
        return _NONE_PRINCIPLE
    for needle, text in _PRINCIPLES:
        if needle in fp:
            return text
    return (f"Recurring failure pattern detected — review and eliminate the shared root "
            f"cause: \"{fp[:160]}\".")


@dataclass
class _Cluster:
    fingerprint: str
    lessons: List[Lesson] = field(default_factory=list)
    consolidated: bool = False


@dataclass(frozen=True)
class ConsolidationResult:
    """What the matrix did when a cluster reached threshold."""

    directive_name: str
    principle: str
    fingerprint: str
    episodes_purged: int
    occurrences: int


class SemanticConsolidationMatrix:
    """Clusters recurring lessons by structural fingerprint and, at threshold, distills a
    single durable CORE_DIRECTIVE while purging the redundant episodes."""

    def __init__(
        self,
        *,
        store: Optional[Any] = None,
        purge: Optional[Callable[[str], int]] = None,
        threshold: Optional[int] = None,
        max_clusters: Optional[int] = None,
        enabled: Optional[bool] = None,
    ) -> None:
        self._store = store
        self._purge = purge
        self._threshold = self._resolve_int(threshold, _ENV_THRESHOLD, 5, lo=2)
        self._max_clusters = self._resolve_int(max_clusters, _ENV_MAX_CLUSTERS, 64, lo=1)
        self._enabled_override = enabled
        self._clusters: "OrderedDict[str, _Cluster]" = OrderedDict()
        # resolve the STYLE memory type lazily so this module imports standalone
        self._style_type = self._resolve_style_type()

    # ---- helpers ----------------------------------------------------------
    @staticmethod
    def _resolve_int(explicit: Optional[int], env: str, default: int, *, lo: int) -> int:
        if explicit is not None:
            return max(lo, int(explicit))
        raw = os.environ.get(env, "")
        try:
            return max(lo, int(raw)) if raw.strip() else default
        except ValueError:
            return default

    @staticmethod
    def _resolve_style_type():
        try:
            from backend.core.ouroboros.governance.user_preference_memory import MemoryType
            return MemoryType.STYLE
        except Exception:  # noqa: BLE001
            return "style"

    def _is_enabled(self) -> bool:
        if self._enabled_override is not None:
            return self._enabled_override
        return consolidation_enabled()

    # ---- public API -------------------------------------------------------
    def record(self, lesson: Lesson) -> Optional[ConsolidationResult]:
        """Ingest a lesson. Returns a ConsolidationResult if this call crossed the threshold
        and produced a directive, else None. Never raises."""
        try:
            if not self._is_enabled():
                return None
            if not lesson or not (lesson.signature or "").strip():
                return None
            fp = fingerprint(lesson.signature)
            if not fp:
                return None

            cluster = self._clusters.get(fp)
            if cluster is None:
                cluster = _Cluster(fingerprint=fp)
                self._clusters[fp] = cluster
                self._evict_if_needed()
            self._clusters.move_to_end(fp)  # LRU freshness

            if cluster.consolidated:
                return None  # already distilled — don't re-fire or re-grow
            if len(cluster.lessons) < self._threshold:
                cluster.lessons.append(lesson)
            if len(cluster.lessons) < self._threshold:
                return None
            return self._consolidate(cluster)
        except Exception:  # noqa: BLE001
            logger.debug("[Consolidation] record failed (fail-soft)", exc_info=True)
            return None

    def cluster_size(self, signature: str) -> int:
        c = self._clusters.get(fingerprint(signature))
        return len(c.lessons) if c else 0

    # ---- internals --------------------------------------------------------
    def _evict_if_needed(self) -> None:
        while len(self._clusters) > self._max_clusters:
            old_fp, _ = self._clusters.popitem(last=False)  # drop least-recently-used
            logger.debug("[Consolidation] evicted cluster %s (cap %d)", old_fp[:40], self._max_clusters)

    def _consolidate(self, cluster: _Cluster) -> Optional[ConsolidationResult]:
        raw_sample = cluster.lessons[0].signature if cluster.lessons else ""
        principle = _principle_for(cluster.fingerprint, raw_sample)
        occurrences = len(cluster.lessons)
        files = tuple(sorted({l.file_path for l in cluster.lessons if l.file_path}))
        short = cluster.fingerprint[:60].strip() or "recurring-failure"
        name = f"core-directive: {short}"
        description = f"Distilled from {occurrences} similar failures — {principle[:120]}"
        content = (
            f"## CORE DIRECTIVE (auto-consolidated)\n\n{principle}\n\n"
            f"Observed {occurrences}× as: `{cluster.fingerprint[:200]}`"
        )

        persisted = False
        if self._store is not None:
            try:
                self._store.add(
                    memory_type=self._style_type,
                    name=name,
                    description=description,
                    content=content,
                    why=f"Recurred {occurrences} times before consolidation.",
                    how_to_apply="Apply whenever generating code in the affected area.",
                    source=f"consolidation:{cluster.fingerprint[:16]}",
                    tags=("core_directive", "consolidated", "live_fire", "edge_case_armor"),
                    paths=files,
                )
                persisted = True
            except Exception:  # noqa: BLE001
                logger.debug("[Consolidation] store.add failed (fail-soft)", exc_info=True)

        purged = 0
        if persisted and self._purge is not None:
            try:
                purged = int(self._purge(cluster.fingerprint) or 0)
            except Exception:  # noqa: BLE001
                logger.debug("[Consolidation] purge failed (fail-soft)", exc_info=True)

        if persisted:
            cluster.consolidated = True
            cluster.lessons.clear()  # free the redundant in-memory copies
            logger.info("[Consolidation] distilled %d×%s → CORE_DIRECTIVE (purged %d episodes)",
                        occurrences, cluster.fingerprint[:40], purged)
            return ConsolidationResult(
                directive_name=name, principle=principle,
                fingerprint=cluster.fingerprint, episodes_purged=purged,
                occurrences=occurrences,
            )
        return None


# ---------------------------------------------------------------------------
# Process-wide lazy singleton (the call site the Tier-2 deployer injects)
# ---------------------------------------------------------------------------

_DEFAULT_MATRIX: Optional["SemanticConsolidationMatrix"] = None
_DEFAULT_MATRIX_LOCK = threading.Lock()


def get_default_matrix(project_root: Optional[Any] = None) -> "SemanticConsolidationMatrix":
    """Return a process-wide :class:`SemanticConsolidationMatrix` wired to the default
    UserPreferenceStore. Lets orchestrator hooks reach the matrix without threading it
    through every constructor (mirrors ``get_default_store``). Fail-soft: if the store can't
    be built, the matrix runs store-less (clusters in memory, persists nothing) — never
    raising into the hot lesson path. Gating is still per-call via the env master switch."""
    global _DEFAULT_MATRIX
    with _DEFAULT_MATRIX_LOCK:
        if _DEFAULT_MATRIX is None:
            store = None
            try:
                from backend.core.ouroboros.governance.user_preference_memory import (
                    get_default_store,
                )
                store = get_default_store(project_root)
            except Exception:  # noqa: BLE001
                logger.debug("[Consolidation] default store unavailable — store-less matrix",
                             exc_info=True)
            _DEFAULT_MATRIX = SemanticConsolidationMatrix(store=store, purge=_default_purge)
        return _DEFAULT_MATRIX


def _default_purge(fp: str) -> int:
    """Integrity-preserving purge: retire episodic recall entries whose summary shares the
    consolidated cluster's fingerprint (RAM cache eviction + append-only supersession
    tombstone — NEVER deletes the tamper-evident chain). Fail-soft → 0."""
    try:
        from backend.core.ouroboros.governance.episodic_core import prune_episodes
        return prune_episodes(
            lambda ep: fingerprint(getattr(ep, "summary", "")) == fp,
            tombstone_label=fp[:60],
        )
    except Exception:  # noqa: BLE001
        logger.debug("[Consolidation] episodic prune unavailable", exc_info=True)
        return 0


def reset_default_matrix() -> None:
    """Clear the process-wide singleton. Primarily for tests."""
    global _DEFAULT_MATRIX
    with _DEFAULT_MATRIX_LOCK:
        _DEFAULT_MATRIX = None
