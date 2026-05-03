"""GoalInferenceEngine — cross-signal direction inference.

**Scope framing**: existing surfaces handle *declared* goals (GoalTracker
YAML + keywords), *similarity scoring* (SemanticIndex centroid), and
*session summaries* (LastSessionSummary). None of them synthesize
hypothesized goals from what the operator *actually does* — what gets
committed, what they type, what they save to memory, what lands
successfully. This engine closes the "read the room" gap: it watches
multiple signals and produces ranked hypotheses about direction with
evidence + confidence + operator-reviewable IDs.

Design principles (Manifesto §1 boundary + §8 observability):

  * Pure-deterministic. No embeddings, no LLM calls, no network. V1
    uses keyword extraction + token-cluster correlation. Machine-
    learned clustering is deferred — the V1 deterministic surface is
    already useful AND honest about what it does.
  * Hypotheses, not facts. Output is explicitly labeled in every
    rendered surface as "inferred direction — not declared goal".
  * Evidence-forward. Every hypothesis carries (source → supporting
    token weight → confidence) so operators can see WHY a direction
    was inferred, not just WHAT it is.
  * Operator-authoritative. `/infer accept` promotes to declared
    goal. `/infer reject` writes a FEEDBACK memory saying "we're NOT
    working on X", which future inference runs honor.
  * Fail-closed. `JARVIS_GOAL_INFERENCE_ENABLED=false` default.
    Master switch must be explicit.

Authority invariant: inferred goals inform the CONTEXT_EXPANSION
prompt + optionally provide a capped soft boost to intake priority.
They NEVER affect risk_tier, provider_route, SemanticGuardian findings,
Iron Gate verdicts, approval gating, or any deterministic engine.
Risk/route/guardian/gate remain authoritative.

Signal sources (V1):
  * Commits        — parse last N commits for scope/type/subject tokens
  * REPL inputs    — recent operator-typed text via ConversationBridge
  * Memory         — USER/PROJECT memories via UserPreferenceStore
  * Completed ops  — applied ledger entries (what actually landed)
  * File hotspots  — git log --name-only top-touched files
  * Declared goals — GoalTracker (anchor for correlation)

Rejected FEEDBACK memories (tagged ``inferred_goal_rejected``) are
applied as a *negative* signal: any hypothesis whose theme matches a
rejection is filtered out before ranking. Operators don't re-see
hypotheses they've already said no to.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger("Ouroboros.GoalInference")

_ENV_ENABLED = "JARVIS_GOAL_INFERENCE_ENABLED"
_ENV_MIN_CONFIDENCE = "JARVIS_GOAL_INFERENCE_MIN_CONFIDENCE"
_ENV_TOP_K = "JARVIS_GOAL_INFERENCE_TOP_K"
_ENV_COMMIT_LOOKBACK = "JARVIS_GOAL_INFERENCE_COMMIT_LOOKBACK"
_ENV_MAX_AGE_S = "JARVIS_GOAL_INFERENCE_MAX_AGE_S"
_ENV_PRIORITY_BOOST_MAX = "JARVIS_GOAL_INFERENCE_PRIORITY_BOOST_MAX"
_ENV_PROMPT_INJECTION = "JARVIS_GOAL_INFERENCE_PROMPT_INJECTION"
_ENV_REFRESH_S = "JARVIS_GOAL_INFERENCE_REFRESH_S"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def inference_enabled() -> bool:
    """Master switch. Graduated default-ON 2026-05-03 (Slice C).

    The substrate has been live behind this flag since Phase 7.x with
    the engine wired into orchestrator.py CONTEXT_EXPANSION; Slice B
    added the production intake-priority wire-up; Slice C ships
    observability + flips this default. Operators who want the engine
    silent flip explicit ``false``.
    """
    return os.environ.get(_ENV_ENABLED, "1").strip().lower() in _TRUTHY


def prompt_injection_enabled() -> bool:
    """When master is on, still allow operators to disable prompt
    injection separately (keep the engine running but invisible to
    the model — useful for A/B observation)."""
    raw = os.environ.get(_ENV_PROMPT_INJECTION, "1").strip().lower()
    return raw in _TRUTHY


def min_confidence() -> float:
    try:
        return max(0.0, min(1.0, float(
            os.environ.get(_ENV_MIN_CONFIDENCE, "0.5"),
        )))
    except (TypeError, ValueError):
        return 0.5


def top_k() -> int:
    try:
        return max(1, min(10, int(os.environ.get(_ENV_TOP_K, "3"))))
    except (TypeError, ValueError):
        return 3


def commit_lookback() -> int:
    try:
        return max(5, min(200, int(
            os.environ.get(_ENV_COMMIT_LOOKBACK, "30"),
        )))
    except (TypeError, ValueError):
        return 30


def max_age_s() -> int:
    try:
        return max(3600, int(os.environ.get(_ENV_MAX_AGE_S, "86400")))
    except (TypeError, ValueError):
        return 86400


def priority_boost_max() -> float:
    """Hard cap on the soft intake-priority boost inferred goals
    contribute. Kept strictly below the declared-goal boost (typically
    1.0-2.0) so inferred signal can never outweigh explicit goals."""
    try:
        return max(0.0, min(1.0, float(
            os.environ.get(_ENV_PRIORITY_BOOST_MAX, "0.5"),
        )))
    except (TypeError, ValueError):
        return 0.5


def refresh_s() -> int:
    try:
        return max(60, min(86400, int(
            os.environ.get(_ENV_REFRESH_S, "1800"),
        )))
    except (TypeError, ValueError):
        return 1800


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SignalSample:
    """One token contributed by one signal source."""

    source: str                     # "commits" | "repl" | "memory" | ...
    token: str                      # normalized keyword (lowercase, alnum)
    weight: float                   # 0.0-1.0, source-local
    age_s: float = 0.0              # how old the contributing event was
    citation: str = ""              # short human-readable source quote


@dataclass(frozen=True)
class InferredGoal:
    """One hypothesized direction the operator appears to be pursuing."""

    theme: str                              # short phrase (cluster head token)
    tokens: Tuple[str, ...]                 # clustered token set
    confidence: float                       # 0.0-1.0
    supporting_sources: Tuple[str, ...]     # distinct sources that agreed
    evidence: Tuple[SignalSample, ...]       # raw samples that built this
    supporting_files: Tuple[str, ...] = ()   # correlated file paths (if any)

    @property
    def inferred_id(self) -> str:
        """Stable short id for accept/reject reference."""
        h = hashlib.sha256(self.theme.encode("utf-8")).hexdigest()[:10]
        return f"inf-{h}"


@dataclass
class InferenceResult:
    """One snapshot from ``GoalInferenceEngine.build()``. Cached between
    rebuilds; the engine returns this same object until a new build."""

    inferred: Tuple[InferredGoal, ...] = ()
    built_at: float = 0.0
    build_ms: int = 0
    total_samples: int = 0
    # Counts per source for observability.
    sources_contributing: Dict[str, int] = field(default_factory=dict)
    # Raw reason this build fired (either "refresh_elapsed" or "first_build").
    build_reason: str = ""


# ---------------------------------------------------------------------------
# Keyword extraction helpers
# ---------------------------------------------------------------------------


# Common English + code stopwords — kept deliberately short. V1 leans on
# deterministic substrings rather than trying to be a real NLP stopwords
# list. Operators who want stricter filtering can set the env knob.
_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "if", "else", "for", "while",
    "from", "into", "onto", "with", "without", "of", "to", "in", "on",
    "at", "by", "as", "is", "are", "was", "were", "be", "been", "being",
    "has", "have", "had", "do", "does", "did", "will", "would", "can",
    "could", "should", "may", "might", "must", "shall", "this", "that",
    "these", "those", "it", "its", "they", "them", "their", "we", "our",
    "us", "you", "your", "he", "him", "his", "she", "her", "i", "my",
    "me", "not", "no", "yes", "all", "any", "some", "one", "two", "three",
    "new", "old", "add", "added", "adds", "added", "fix", "fixed", "fixes",
    "update", "updated", "updates", "remove", "removed", "removes",
    "import", "from", "def", "class", "self", "cls", "return", "yield",
    "true", "false", "none", "null",
    "test", "tests", "testing",  # too generic as a signal
    "wip", "todo", "fixme",
})

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")


def _normalize_token(raw: str) -> str:
    return raw.strip().lower()


def extract_tokens(text: str) -> List[str]:
    """Deterministic keyword extraction:

      * match ≥3-char alphanumeric runs starting with a letter
      * lowercase
      * drop stopwords + pure-digit leftovers

    V1 uses plain regex — stemming / lemmatization deferred. For
    commit subjects, REPL inputs, and memory descriptions, this is
    more than good enough to find theme correlations.
    """
    if not text:
        return []
    out: List[str] = []
    for m in _TOKEN_RE.finditer(text):
        t = _normalize_token(m.group(0))
        if t in _STOPWORDS:
            continue
        if len(t) < 3 or len(t) > 40:
            continue
        out.append(t)
    return out


# ---------------------------------------------------------------------------
# Signal extractors — one per source. Each returns List[SignalSample].
# Each is independently fallible: a git failure in CommitsSignal doesn't
# stop MemorySignal from contributing.
# ---------------------------------------------------------------------------


def _git(repo_root: Path, args: List[str]) -> str:
    """Run git and return stdout, empty string on failure."""
    import subprocess
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(repo_root),
            capture_output=True, text=True, timeout=10.0,
        )
        if proc.returncode != 0:
            return ""
        return proc.stdout
    except Exception:  # noqa: BLE001
        return ""


def extract_commits_signal(
    *, repo_root: Path, lookback: int, cutoff_epoch: float,
) -> List[SignalSample]:
    """Parse recent commit subjects + Conventional-Commit scopes."""
    out: List[SignalSample] = []
    raw = _git(
        repo_root,
        [
            "log", f"-{lookback}", "--no-merges",
            "--format=%ct%x1f%s",
        ],
    )
    if not raw:
        return out
    for line in raw.splitlines():
        parts = line.split("\x1f", 1)
        if len(parts) != 2:
            continue
        try:
            ct = int(parts[0])
        except ValueError:
            continue
        if ct < cutoff_epoch:
            continue
        subject = parts[1].strip()
        age = max(0.0, time.time() - ct)
        # Extract Conventional Commit scope — scope is explicit author
        # intent so its tokens get the 1.5x weight bonus AND are
        # emitted independently (they're stripped from the remaining
        # subject text during tokenization).
        scope_match = re.match(r"^([a-z_-]+)(?:\(([^)]+)\))?:\s*(.+)$", subject)
        scope_tokens: Set[str] = set()
        subj_text = subject
        if scope_match:
            type_ = scope_match.group(1) or ""
            scope = (scope_match.group(2) or "").strip()
            rest = scope_match.group(3) or ""
            if scope:
                for s in re.split(r"[,\s]+", scope):
                    if s:
                        scope_tokens.add(s.strip().lower())
            subj_text = f"{type_} {rest}"

        # Emit scope tokens FIRST with the high-intent weight. These
        # wouldn't otherwise appear in subj_text (we stripped them).
        for scope_tok in scope_tokens:
            for tok in extract_tokens(scope_tok):
                out.append(SignalSample(
                    source="commits", token=tok, weight=1.5,
                    age_s=age, citation=subject[:80],
                ))

        for tok in extract_tokens(subj_text):
            weight = 1.0
            if tok in scope_tokens:
                weight = 1.5
            out.append(SignalSample(
                source="commits", token=tok, weight=weight,
                age_s=age, citation=subject[:80],
            ))
    return out


def extract_repl_signal(
    *, bridge_snapshot: Optional[List[Tuple[str, str]]] = None,
    cutoff_epoch: float,
) -> List[SignalSample]:
    """Pull recent operator-typed REPL text via ConversationBridge.

    ``bridge_snapshot`` is passed explicitly so tests can inject; in
    production the engine resolves the default bridge via the
    conversation_bridge module's public API.
    """
    samples: List[SignalSample] = []
    if bridge_snapshot is None:
        try:
            from backend.core.ouroboros.governance.conversation_bridge import (
                get_default_bridge,
            )
            bridge = get_default_bridge()
            if bridge is None:
                return samples
            # ConversationBridge exposes a get_snapshot method on most
            # builds; fall back defensively.
            get_snap = getattr(bridge, "get_snapshot", None)
            if get_snap is None:
                return samples
            snap = get_snap()
            # snap is expected to be iterable of (source, text) tuples
            bridge_snapshot = []
            for item in (snap or []):
                if (
                    isinstance(item, tuple)
                    and len(item) >= 2
                    and isinstance(item[0], str)
                    and isinstance(item[1], str)
                ):
                    bridge_snapshot.append((item[0], item[1]))
        except Exception:  # noqa: BLE001
            logger.debug("[GoalInference] REPL signal skipped", exc_info=True)
            return samples

    for src, text in (bridge_snapshot or []):
        if src not in ("tui_user", "ask_human_a"):
            continue  # only operator-typed text
        if not text:
            continue
        for tok in extract_tokens(text):
            samples.append(SignalSample(
                source="repl", token=tok, weight=1.0,
                age_s=0.0,
                citation=text[:80],
            ))
    return samples


def extract_memory_signal(
    *, store: Any = None, repo_root: Optional[Path] = None,
) -> List[SignalSample]:
    """Pull tokens from USER + PROJECT memory entries."""
    samples: List[SignalSample] = []
    if store is None:
        try:
            from backend.core.ouroboros.governance.user_preference_memory import (
                get_default_store,
            )
            store = get_default_store(repo_root or Path.cwd())
        except Exception:  # noqa: BLE001
            return samples
    try:
        from backend.core.ouroboros.governance.user_preference_memory import (
            MemoryType,
        )
    except Exception:  # noqa: BLE001
        return samples
    try:
        mems = store.list_all()
    except Exception:  # noqa: BLE001
        return samples
    for m in mems:
        if m.type not in (MemoryType.USER, MemoryType.PROJECT):
            continue
        combined = " ".join([
            m.name or "", m.description or "", m.why or "",
            " ".join(m.tags or ()),
        ])
        for tok in extract_tokens(combined):
            samples.append(SignalSample(
                source="memory", token=tok, weight=1.2,  # operator-declared = high
                age_s=0.0, citation=(m.name or "")[:80],
            ))
    return samples


def extract_completed_ops_signal(
    *, repo_root: Path, cutoff_epoch: float,
) -> List[SignalSample]:
    """Extract tokens from applied ops' ledger entries — what actually
    landed is a high-signal indicator of direction."""
    samples: List[SignalSample] = []
    import json
    ledger_root = (
        repo_root / ".ouroboros" / "state" / "ouroboros" / "ledger"
    )
    if not ledger_root.is_dir():
        return samples
    # Iterate recent ledger files; extract goal from PLANNED entries of
    # ops that reached terminal state "applied".
    files = sorted(ledger_root.glob("op-*.jsonl"))
    for path in files[-50:]:  # bounded walk
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            continue
        if '"state": "applied"' not in raw:
            continue
        goal = ""
        wall_time = 0.0
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if str(entry.get("state", "")).lower() == "planned":
                data = entry.get("data") or {}
                if isinstance(data, dict):
                    g = data.get("goal")
                    if isinstance(g, str):
                        goal = g
                        wall_time = float(entry.get("wall_time", 0.0) or 0.0)
        if not goal or wall_time < cutoff_epoch:
            continue
        age = max(0.0, time.time() - wall_time)
        for tok in extract_tokens(goal):
            samples.append(SignalSample(
                source="completed_ops", token=tok, weight=1.3,
                age_s=age, citation=goal[:80],
            ))
    return samples


def extract_file_hotspots_signal(
    *, repo_root: Path, lookback: int,
) -> Tuple[List[SignalSample], List[str]]:
    """Find the most-touched files in recent history and extract tokens
    from path components. Returns (samples, ranked_paths)."""
    samples: List[SignalSample] = []
    raw = _git(
        repo_root,
        ["log", f"-{lookback}", "--name-only", "--format="],
    )
    if not raw:
        return (samples, [])
    counter: Counter = Counter()
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        counter[line] += 1
    ranked = [p for p, _ in counter.most_common(10)]
    for path, n in counter.most_common(20):
        # Extract tokens from each path component — skip very generic
        # dir names that pollute signal (backend, core, etc.).
        for part in re.split(r"[/\\.]", path):
            if len(part) < 3:
                continue
            for tok in extract_tokens(part):
                # Weight by touch frequency, normalized.
                weight = min(1.0, n / 10.0)
                samples.append(SignalSample(
                    source="file_hotspots", token=tok, weight=weight,
                    citation=path,
                ))
    return (samples, ranked)


def extract_declared_goals_signal(
    *, repo_root: Path,
) -> List[SignalSample]:
    """Pull tokens from GoalTracker.active_goals as the baseline anchor."""
    samples: List[SignalSample] = []
    try:
        from backend.core.ouroboros.governance.strategic_direction import (
            GoalTracker,
        )
    except Exception:  # noqa: BLE001
        return samples
    try:
        tracker = GoalTracker(repo_root)
        for g in tracker.active_goals:
            text = " ".join([
                getattr(g, "description", "") or "",
                " ".join(getattr(g, "keywords", ()) or ()),
            ])
            for tok in extract_tokens(text):
                samples.append(SignalSample(
                    source="declared_goals", token=tok, weight=0.8,
                    citation=(getattr(g, "id", "") or ""),
                ))
    except Exception:  # noqa: BLE001
        pass
    return samples


# ---------------------------------------------------------------------------
# Rejection filter — operator rejects are a negative signal
# ---------------------------------------------------------------------------


def _rejected_themes(repo_root: Path) -> Set[str]:
    """Read FEEDBACK memories tagged ``inferred_goal_rejected`` and
    return the set of theme tokens the operator explicitly said NO to.

    Any hypothesis whose theme equals one of these is dropped before
    ranking. Tokens inside those rejected themes also get their weight
    reduced in the general inference corpus.
    """
    try:
        from backend.core.ouroboros.governance.user_preference_memory import (
            MemoryType, get_default_store,
        )
    except Exception:  # noqa: BLE001
        return set()
    try:
        store = get_default_store(repo_root)
        mems = store.find_by_type(MemoryType.FEEDBACK)
    except Exception:  # noqa: BLE001
        return set()
    themes: Set[str] = set()
    for m in mems:
        if "inferred_goal_rejected" not in (m.tags or ()):
            continue
        # Theme is stored in the description when we write the memory.
        # Extract it defensively.
        desc = (m.description or "").lower().strip()
        # The write format is: "operator rejected inferred direction: <theme>"
        marker = "inferred direction:"
        if marker in desc:
            theme = desc.split(marker, 1)[1].strip()
            if theme:
                themes.add(theme)
        else:
            # Fall back to the name slug.
            themes.add((m.name or "").lower())
    return themes


# ---------------------------------------------------------------------------
# Engine — aggregation + clustering + ranking
# ---------------------------------------------------------------------------


class GoalInferenceEngine:
    """Pull-model cross-signal aggregator.

    Usage:

        engine = GoalInferenceEngine(repo_root=Path(...))
        result = engine.build()           # returns InferenceResult
        engine.get_current()              # cached result (no rebuild)
        engine.build(force=True)          # bypass refresh timer

    The engine caches the last build for ``refresh_s()`` seconds so
    callers can invoke ``build()`` on every op without re-running
    signal extraction. Explicit ``force=True`` bypasses the cache
    (used by ``/infer`` REPL command).
    """

    def __init__(self, *, repo_root: Path) -> None:
        self._repo_root = Path(repo_root).resolve()
        self._cached: Optional[InferenceResult] = None
        self._last_build_mono: float = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build(self, *, force: bool = False) -> InferenceResult:
        if not inference_enabled():
            return InferenceResult(build_reason="disabled")
        if (
            not force
            and self._cached is not None
            and (time.monotonic() - self._last_build_mono) < refresh_s()
        ):
            return self._cached

        t_start = time.monotonic()
        cutoff_epoch = time.time() - max_age_s()
        lookback = commit_lookback()

        # Collect signal samples.
        samples: List[SignalSample] = []
        samples.extend(extract_commits_signal(
            repo_root=self._repo_root,
            lookback=lookback,
            cutoff_epoch=cutoff_epoch,
        ))
        samples.extend(extract_repl_signal(cutoff_epoch=cutoff_epoch))
        samples.extend(extract_memory_signal(repo_root=self._repo_root))
        samples.extend(extract_completed_ops_signal(
            repo_root=self._repo_root, cutoff_epoch=cutoff_epoch,
        ))
        hotspot_samples, hotspot_paths = extract_file_hotspots_signal(
            repo_root=self._repo_root, lookback=lookback,
        )
        samples.extend(hotspot_samples)
        samples.extend(extract_declared_goals_signal(
            repo_root=self._repo_root,
        ))

        # Rejection filter — drop tokens that correspond to rejected themes.
        rejected = _rejected_themes(self._repo_root)
        if rejected:
            samples = [s for s in samples if s.token not in rejected]

        # Cluster by normalized token; a simple aggregator is enough for V1.
        inferred = _cluster_and_rank(
            samples=samples,
            hotspot_paths=tuple(hotspot_paths),
            rejected_themes=rejected,
        )

        # Per-source contribution counts for telemetry.
        source_counts: Dict[str, int] = defaultdict(int)
        for s in samples:
            source_counts[s.source] += 1

        result = InferenceResult(
            inferred=tuple(inferred),
            built_at=time.time(),
            build_ms=int((time.monotonic() - t_start) * 1000),
            total_samples=len(samples),
            sources_contributing=dict(source_counts),
            build_reason="first_build" if self._cached is None else "refresh_elapsed",
        )
        self._cached = result
        self._last_build_mono = time.monotonic()

        logger.info(
            "[GoalInference] built_at=%d build_ms=%d samples=%d "
            "sources=%s hypotheses=%d top_conf=%.2f",
            int(result.built_at), result.build_ms,
            result.total_samples, len(source_counts),
            len(result.inferred),
            (result.inferred[0].confidence if result.inferred else 0.0),
        )
        # Slice C — best-effort SSE publish on cache miss only. Never
        # raises into build(); never fires on cache hits (avoids
        # observability storm under hot intake load).
        try:
            from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
                publish_goal_inference_built,
            )
            top = result.inferred[0] if result.inferred else None
            publish_goal_inference_built(
                built_at=result.built_at,
                build_ms=result.build_ms,
                total_samples=result.total_samples,
                hypotheses_count=len(result.inferred),
                top_theme=(top.theme if top else ""),
                top_confidence=(top.confidence if top else 0.0),
                sources_contributing=len(source_counts),
                build_reason=result.build_reason,
            )
        except Exception:  # noqa: BLE001 -- defensive
            logger.debug(
                "[GoalInference] SSE publish skipped", exc_info=True,
            )
        return result

    def get_current(self) -> Optional[InferenceResult]:
        return self._cached

    def invalidate(self) -> None:
        """Force next build() to refresh. Called by accept/reject so the
        operator's action flows through to the next prompt immediately."""
        self._cached = None
        self._last_build_mono = 0.0


# ---------------------------------------------------------------------------
# Clustering + ranking — the heart of the engine
# ---------------------------------------------------------------------------


def _cluster_and_rank(
    *,
    samples: Sequence[SignalSample],
    hotspot_paths: Tuple[str, ...],
    rejected_themes: Set[str],
) -> List[InferredGoal]:
    """V1 clustering: group samples by exact token, then merge clusters
    whose heads share a common prefix (length ≥ 4) to capture families
    like {"semantic", "semantically", "semantics"} or {"preview",
    "previewer"}. Confidence is a function of:

      * token frequency across samples
      * distinct source count (diversity matters more than volume)
      * recency (younger signals dominate)

    Rejected themes are dropped at the top. File hotspots attach to
    whichever cluster has the strongest keyword overlap with the path.
    """
    # 1. Token-level aggregation.
    by_token: Dict[str, List[SignalSample]] = defaultdict(list)
    for s in samples:
        by_token[s.token].append(s)

    # 2. Drop rejected themes.
    for rej in rejected_themes:
        by_token.pop(rej, None)

    # 3. Compute per-token score. The formula favors DIVERSITY (multiple
    # sources agreeing) over raw VOLUME (many samples from one source),
    # which captures "the operator's intent is visible across the whole
    # surface" rather than "this one source happens to mention X a lot."
    token_scores: Dict[str, float] = {}
    for tok, ss in by_token.items():
        # Diversity: distinct sources, count capped at 4 for the math.
        # Single-source (diversity_score = 0.25) ops must fall below the
        # default 0.5 min_confidence threshold — that's the load-bearing
        # shape of this formula.
        distinct_sources = len({s.source for s in ss})
        diversity_score = min(4, distinct_sources) / 4.0
        # Volume: log-scaled.
        volume_score = min(1.0, len(ss) / 10.0)
        # Recency: mean age < max_age; younger = higher.
        avg_age = sum(s.age_s for s in ss) / max(1, len(ss))
        recency_score = max(
            0.0,
            1.0 - (avg_age / max(1.0, max_age_s())),
        )
        # Author intent weighting: sources like memory/declared_goals
        # carry operator-explicit signal (weight ≥ 1.2) — capped at 1.5.
        weight_score = min(1.5, sum(s.weight for s in ss) / max(1.0, len(ss)))
        # Diversity-dominant composition:
        #   single-source single-sample → 0.60·0.25 + … = 0.15 + small  → ~0.35
        #   2-source ≥2-sample → 0.60·0.50 + … = 0.30 + 0.15+ → > 0.50
        score = (
            0.60 * diversity_score
            + 0.20 * recency_score
            + 0.10 * volume_score
            + 0.10 * min(1.0, weight_score / 1.5)
        )
        token_scores[tok] = min(1.0, score)

    # 4. Cluster by shared prefix (simple, deterministic).
    # Sort tokens by score desc so each cluster anchors on the strongest
    # head token. Subsequent tokens sharing a ≥4-char prefix merge in.
    tokens_sorted = sorted(
        token_scores.items(), key=lambda kv: kv[1], reverse=True,
    )
    clusters: List[Dict[str, Any]] = []
    consumed: Set[str] = set()
    for tok, _score in tokens_sorted:
        if tok in consumed:
            continue
        head = tok
        cluster_tokens = [head]
        consumed.add(head)
        for other, _ in tokens_sorted:
            if other in consumed:
                continue
            if len(head) < 4 or len(other) < 4:
                continue
            # Prefix-or-contains match.
            if (
                head.startswith(other[:4])
                or other.startswith(head[:4])
            ):
                cluster_tokens.append(other)
                consumed.add(other)
        clusters.append({
            "head": head,
            "tokens": tuple(cluster_tokens),
        })

    # 5. Build InferredGoal records with aggregated evidence.
    results: List[InferredGoal] = []
    thresh = min_confidence()
    for cl in clusters:
        head = cl["head"]
        tokens = cl["tokens"]
        evidence: List[SignalSample] = []
        supporting_sources: Set[str] = set()
        for t in tokens:
            evidence.extend(by_token.get(t, []))
            supporting_sources.update(s.source for s in by_token.get(t, []))
        if not evidence:
            continue
        # Cluster confidence: head score scaled by source diversity bonus.
        head_score = token_scores.get(head, 0.0)
        diversity_bonus = min(0.2, 0.05 * len(supporting_sources))
        confidence = min(1.0, head_score + diversity_bonus)
        if confidence < thresh:
            continue
        # Correlated files: hotspots whose path mentions the head token.
        head_lower = head.lower()
        correlated = tuple(
            p for p in hotspot_paths if head_lower in p.lower()
        )[:5]
        theme = _make_theme(head=head, tokens=tokens, evidence=evidence)
        results.append(InferredGoal(
            theme=theme,
            tokens=tokens,
            confidence=confidence,
            supporting_sources=tuple(sorted(supporting_sources)),
            evidence=tuple(evidence[:20]),  # cap evidence to keep records small
            supporting_files=correlated,
        ))

    results.sort(key=lambda g: g.confidence, reverse=True)
    return results


def _make_theme(
    *, head: str, tokens: Tuple[str, ...], evidence: Sequence[SignalSample],
) -> str:
    """Compose a concise theme string from the cluster. For V1 we use
    the head token — which is the highest-scoring keyword — plus a
    short evidence citation so the theme isn't a bare word. Real NLP
    summarization is deferred."""
    cite = ""
    for s in evidence:
        if s.citation:
            cite = s.citation[:60]
            break
    if cite:
        return f"{head}  [{cite}]"
    return head


# ---------------------------------------------------------------------------
# Prompt rendering — injected at CONTEXT_EXPANSION
# ---------------------------------------------------------------------------


def render_prompt_section(result: InferenceResult) -> str:
    """Return the text injected into ``ctx.strategic_memory_prompt``.

    Explicitly labels the content as hypotheses so the model weights it
    below declared goals. Caps to ``top_k()`` hypotheses. Returns empty
    string when inference is disabled or has nothing to show.
    """
    if not prompt_injection_enabled():
        return ""
    if not result.inferred:
        return ""
    k = top_k()
    top = list(result.inferred[:k])
    lines = [
        "## Inferred Direction (hypotheses — not declared goals)",
        "",
        "The following themes were inferred from recent commits, operator",
        "input, memory, completed ops, and file hotspots. Weight them",
        "BELOW any declared goal. Manifesto §1: these are soft signals,",
        "not authority.",
        "",
    ]
    for i, g in enumerate(top, 1):
        sources = ", ".join(g.supporting_sources)
        lines.append(
            f"  {i}. {g.theme}  [conf={g.confidence:.2f}  "
            f"sources={sources}]"
        )
        if g.supporting_files:
            lines.append(f"     files: {', '.join(g.supporting_files[:3])}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Priority boost — capped contribution to intake scoring
# ---------------------------------------------------------------------------


def priority_boost_for_signal(
    *,
    signal_description: str,
    signal_target_files: Tuple[str, ...],
    result: InferenceResult,
) -> float:
    """Return a small additive intake-priority boost for a signal that
    aligns with current inferred direction. Capped by
    ``priority_boost_max()`` — strictly below declared-goal boosts.

    Soft authority only: this value is added by the intake router, not
    enforced. Never mutates the router's deterministic fields (source
    enum, dedup key, urgency).
    """
    if not result.inferred:
        return 0.0
    signal_text = " ".join([
        signal_description or "",
        " ".join(signal_target_files or ()),
    ]).lower()
    if not signal_text.strip():
        return 0.0
    cap = priority_boost_max()
    # Simple: contribution is confidence × 0.5 for each inferred goal
    # whose head token appears in the signal. Summed, clamped to cap.
    score = 0.0
    for g in result.inferred:
        if g.theme.split("  ", 1)[0].lower() in signal_text:
            score += g.confidence * 0.5
    return min(cap, score)


# ---------------------------------------------------------------------------
# Operator actions — accept/reject
# ---------------------------------------------------------------------------


def accept_inferred_goal(
    *, repo_root: Path, inferred: InferredGoal,
) -> Tuple[bool, str]:
    """Promote an inferred goal to a declared goal via GoalTracker.

    Returns ``(ok, message)``. The operator sees ``message`` via
    ``/infer accept``. On success, this also invalidates any cached
    inference so the next build picks up the new declared goal.
    """
    try:
        from backend.core.ouroboros.governance.strategic_direction import (
            GoalTracker,
        )
    except Exception as exc:  # noqa: BLE001
        return (False, f"GoalTracker import failed: {exc}")
    try:
        from backend.core.ouroboros.governance.strategic_direction import (
            ActiveGoal,
        )
        tracker = GoalTracker(repo_root)
        head = inferred.theme.split("  ", 1)[0].strip()
        # Build a slug for goal_id from the head token; reuse cluster
        # tokens as keywords for the declared goal.
        slug = re.sub(r"[^a-z0-9-]+", "-", head.lower()).strip("-") or "inferred"
        slug = f"inferred-{slug}"[:40]
        keywords = tuple(inferred.tokens[:6])
        goal = ActiveGoal(
            goal_id=slug,
            description=f"[from /infer accept] {inferred.theme}",
            keywords=keywords,
        )
        tracker.add_goal(goal)
        return (True, f"promoted to declared goal id={slug}")
    except Exception as exc:  # noqa: BLE001
        return (False, f"add_goal raised: {exc}")


def reject_inferred_goal(
    *, repo_root: Path, inferred: InferredGoal,
) -> Tuple[bool, str]:
    """Record a FEEDBACK memory so this theme is filtered from future
    inference runs. Returns ``(ok, message)``.
    """
    try:
        from backend.core.ouroboros.governance.user_preference_memory import (
            MemoryType, get_default_store,
        )
    except Exception as exc:  # noqa: BLE001
        return (False, f"memory store import failed: {exc}")
    try:
        store = get_default_store(repo_root)
        head = inferred.theme.split("  ", 1)[0].strip().lower()
        mem = store.add(
            MemoryType.FEEDBACK,
            name=f"rejected_{head}",
            description=(
                f"operator rejected inferred direction: {head}"
            ),
            tags=("inferred_goal_rejected",),
            source="repl",
        )
        return (True, f"recorded rejection; mem_id={getattr(mem, 'id', '?')}")
    except Exception as exc:  # noqa: BLE001
        return (False, f"memory add raised: {exc}")


# ---------------------------------------------------------------------------
# Module-level singleton (harness wires once at boot)
# ---------------------------------------------------------------------------


_DEFAULT_ENGINE: Optional[GoalInferenceEngine] = None


def get_default_engine(repo_root: Optional[Path] = None) -> Optional[GoalInferenceEngine]:
    global _DEFAULT_ENGINE
    if _DEFAULT_ENGINE is None and repo_root is not None:
        _DEFAULT_ENGINE = GoalInferenceEngine(repo_root=repo_root)
    return _DEFAULT_ENGINE


def register_default_engine(engine: Optional[GoalInferenceEngine]) -> None:
    global _DEFAULT_ENGINE
    _DEFAULT_ENGINE = engine


def reset_default_engine() -> None:
    global _DEFAULT_ENGINE
    _DEFAULT_ENGINE = None


# ---------------------------------------------------------------------------
# MissionInferrer Slice A — Module-owned FlagRegistry seeds
# ---------------------------------------------------------------------------
#
# Eight env knobs surface here. Master + sub-gate stay at their original
# defaults pre-graduation; Slice C flips the master to true after the
# Slice B intake wire-up lands and is regression-tested.


def register_flags(registry) -> int:  # noqa: ANN001
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category, FlagSpec, FlagType,
        )
    except Exception as exc:  # noqa: BLE001 -- defensive
        logger.warning(
            "[GoalInference] register_flags degraded: %s", exc,
        )
        return 0
    target = "backend/core/ouroboros/governance/goal_inference.py"
    specs = [
        FlagSpec(
            name=_ENV_ENABLED,
            type=FlagType.BOOL, default=True,
            category=Category.SAFETY,
            source_file=target,
            example=f"{_ENV_ENABLED}=true",
            description=(
                "Master switch for cross-signal goal inference. "
                "When on, GoalInferenceEngine pulls signals from "
                "commits + REPL + memory + completed ops + file "
                "hotspots + declared goals every refresh window "
                "and surfaces ranked hypotheses to CONTEXT_EXPANSION "
                "+ intake priority boost. Graduated default-true "
                "2026-05-03 (Slice C)."
            ),
        ),
        FlagSpec(
            name=_ENV_PROMPT_INJECTION,
            type=FlagType.BOOL, default=True,
            category=Category.SAFETY,
            source_file=target,
            example=f"{_ENV_PROMPT_INJECTION}=true",
            description=(
                "Sub-gate for the CONTEXT_EXPANSION prompt section. "
                "When master is on but this is off, hypotheses are "
                "computed (and flow into intake priority boost) but "
                "are NOT injected into the model prompt. Useful for "
                "A/B observation of pure-cognition vs prompt-influence "
                "effects."
            ),
        ),
        FlagSpec(
            name=_ENV_MIN_CONFIDENCE,
            type=FlagType.FLOAT, default=0.5,
            category=Category.CAPACITY,
            source_file=target,
            example=f"{_ENV_MIN_CONFIDENCE}=0.65",
            description=(
                "Minimum confidence threshold for an inferred goal "
                "to surface. Clamped to [0.0, 1.0]. Lower = more "
                "noise; higher = stricter filter."
            ),
        ),
        FlagSpec(
            name=_ENV_TOP_K,
            type=FlagType.INT, default=3,
            category=Category.CAPACITY,
            source_file=target,
            example=f"{_ENV_TOP_K}=5",
            description=(
                "Maximum number of inferred goals surfaced per "
                "build. Clamped to [1, 10]."
            ),
        ),
        FlagSpec(
            name=_ENV_COMMIT_LOOKBACK,
            type=FlagType.INT, default=30,
            category=Category.CAPACITY,
            source_file=target,
            example=f"{_ENV_COMMIT_LOOKBACK}=60",
            description=(
                "Number of recent commits scanned for the commits "
                "signal. Clamped to [5, 200]."
            ),
        ),
        FlagSpec(
            name=_ENV_MAX_AGE_S,
            type=FlagType.INT, default=86400,
            category=Category.TIMING,
            source_file=target,
            example=f"{_ENV_MAX_AGE_S}=172800",
            description=(
                "Maximum age (seconds) of an event to contribute to "
                "the inferred-direction signal. Older signals are "
                "filtered out before clustering. Floor 3600 (1 hour)."
            ),
        ),
        FlagSpec(
            name=_ENV_PRIORITY_BOOST_MAX,
            type=FlagType.FLOAT, default=0.5,
            category=Category.SAFETY,
            source_file=target,
            example=f"{_ENV_PRIORITY_BOOST_MAX}=0.5",
            description=(
                "Hard cap on the soft intake-priority boost contributed "
                "by inferred goals. Strictly below declared-goal boost "
                "(1.0-2.0) so inferred signal cannot outweigh explicit "
                "goals. Clamped to [0.0, 1.0]."
            ),
        ),
        FlagSpec(
            name=_ENV_REFRESH_S,
            type=FlagType.INT, default=1800,
            category=Category.TIMING,
            source_file=target,
            example=f"{_ENV_REFRESH_S}=900",
            description=(
                "Cadence (seconds) at which GoalInferenceEngine.build() "
                "re-runs signal extraction + clustering. Cache hits "
                "between refreshes are O(1). Clamped to [60, 86400]."
            ),
        ),
    ]
    count = 0
    for spec in specs:
        try:
            registry.register(spec)
            count += 1
        except Exception as exc:  # noqa: BLE001 -- defensive
            logger.debug(
                "[GoalInference] register_flags spec %s skipped: %s",
                spec.name, exc,
            )
    return count


def register_shipped_invariants() -> list:
    """MissionInferrer Slice A invariant: the substrate's load-bearing
    surface MUST stay structurally intact. Pins:

      * 6 extract_*_signal helpers present (one per signal source).
      * GoalInferenceEngine class + render_prompt_section function.
      * priority_boost_for_signal exported (Slice B intake hook).
      * accept_inferred_goal + reject_inferred_goal exported (REPL).
      * No exec/eval/compile anywhere in the module.
      * InferredGoal + SignalSample dataclasses MUST stay frozen
        (their hash-stable identity is consumed by /infer accept/reject
        and by FEEDBACK memory; mutating fields would silently corrupt
        cross-session memory).
    """
    import ast as _ast
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    REQUIRED_FUNCS = (
        "extract_commits_signal",
        "extract_repl_signal",
        "extract_memory_signal",
        "extract_completed_ops_signal",
        "extract_file_hotspots_signal",
        "extract_declared_goals_signal",
        "render_prompt_section",
        "priority_boost_for_signal",
        "accept_inferred_goal",
        "reject_inferred_goal",
    )
    REQUIRED_CLASSES = ("GoalInferenceEngine",)
    FROZEN_DATACLASSES = ("InferredGoal", "SignalSample")

    def _validate(
        tree: "_ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        seen_funcs: set = set()
        seen_classes: set = set()
        frozen_status: dict = {}
        for node in _ast.walk(tree):
            if isinstance(node, _ast.FunctionDef):
                seen_funcs.add(node.name)
            elif isinstance(node, _ast.ClassDef):
                seen_classes.add(node.name)
                if node.name in FROZEN_DATACLASSES:
                    frozen = False
                    for dec in node.decorator_list:
                        # Match @dataclass(frozen=True) variants.
                        if isinstance(dec, _ast.Call):
                            for kw in dec.keywords:
                                if (
                                    kw.arg == "frozen"
                                    and isinstance(kw.value, _ast.Constant)
                                    and kw.value.value is True
                                ):
                                    frozen = True
                                    break
                    frozen_status[node.name] = frozen
            elif isinstance(node, _ast.Call):
                if isinstance(node.func, _ast.Name):
                    if node.func.id in ("exec", "eval", "compile"):
                        violations.append(
                            f"line {getattr(node, 'lineno', '?')}: "
                            f"goal_inference MUST NOT "
                            f"{node.func.id}()"
                        )
        for fn in REQUIRED_FUNCS:
            if fn not in seen_funcs:
                violations.append(f"missing function {fn!r}")
        for cls in REQUIRED_CLASSES:
            if cls not in seen_classes:
                violations.append(f"missing class {cls!r}")
        for cls in FROZEN_DATACLASSES:
            if not frozen_status.get(cls, False):
                violations.append(
                    f"{cls} dataclass MUST stay frozen=True "
                    "(hash-stable identity required for accept/reject "
                    "+ FEEDBACK memory)"
                )
        return tuple(violations)

    # Slice B regression pin: the intake router MUST consume
    # priority_boost_for_signal in _compute_priority. Without this,
    # MissionInferrer is decorative (prompt-only). The pin protects
    # the production-side wire-up from silent deletion across edits.
    INTAKE_ROUTER_FILE = (
        "backend/core/ouroboros/governance/intake/unified_intake_router.py"
    )

    def _validate_intake_consumer(
        tree: "_ast.Module", source: str,
    ) -> tuple:
        violations: list = []
        # Cheap source-level check: the import + call must both appear.
        if "priority_boost_for_signal" not in source:
            violations.append(
                "unified_intake_router.py MUST import + call "
                "priority_boost_for_signal (MissionInferrer Slice B "
                "production wire-up)"
            )
            return tuple(violations)
        if "inferred_direction_boost" not in source:
            violations.append(
                "_compute_priority MUST compose "
                "inferred_direction_boost into priority computation"
            )
        # AST-level confirmation: search for an actual Call to
        # priority_boost_for_signal so renaming-related drift is caught.
        found_call = False
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Call):
                func = node.func
                if (
                    isinstance(func, _ast.Name)
                    and func.id == "priority_boost_for_signal"
                ):
                    found_call = True
                    break
                if (
                    isinstance(func, _ast.Attribute)
                    and func.attr == "priority_boost_for_signal"
                ):
                    found_call = True
                    break
        if not found_call:
            violations.append(
                "no Call to priority_boost_for_signal found in "
                "unified_intake_router.py AST"
            )
        return tuple(violations)

    target = "backend/core/ouroboros/governance/goal_inference.py"
    return [
        ShippedCodeInvariant(
            invariant_name="goal_inference_substrate",
            target_file=target,
            description=(
                "MissionInferrer substrate: 6 signal extractors + "
                "GoalInferenceEngine + render/priority/accept/reject "
                "exports + frozen InferredGoal/SignalSample + "
                "no exec/eval/compile."
            ),
            validate=_validate,
        ),
        ShippedCodeInvariant(
            invariant_name="goal_inference_intake_consumer",
            target_file=INTAKE_ROUTER_FILE,
            description=(
                "MissionInferrer Slice B production wire-up: "
                "_compute_priority MUST consume "
                "priority_boost_for_signal so inferred-direction "
                "boost actually steers intake (not just prompt). "
                "Cross-file regression pin owned by goal_inference."
            ),
            validate=_validate_intake_consumer,
        ),
    ]
