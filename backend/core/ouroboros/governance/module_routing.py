"""module_routing.py — AST-bound hierarchical context router for O+V.

Given an op's target files, injects ONLY the architecture-memory topics
relevant to the module under work.  Routing signal = Oracle dependency graph
(AST-bound), NOT filename string-matching.

Gated default-OFF (``JARVIS_MEMORY_ROUTING_ENABLED``).
Authority-free / advisory: produces prompt text only, fail-silent like
StrategicDirection.  Never imports oracle / semantic_index / source_crawlers
at module level — all three are lazy-imported inside methods to avoid reverse
dependency / import cycles.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, List, Optional, Sequence, Tuple

if TYPE_CHECKING:  # pragma: no cover
    pass

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Feature gate
# ---------------------------------------------------------------------------

_ENV_FLAG = "JARVIS_MEMORY_ROUTING_ENABLED"


def routing_enabled() -> bool:
    """Return True iff ``JARVIS_MEMORY_ROUTING_ENABLED`` is set to a truthy value.

    Default: False (gated default-OFF per spec).
    """
    raw = os.environ.get(_ENV_FLAG, "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Frontmatter parser — simple line scan, no new yaml dependency
# ---------------------------------------------------------------------------

def _parse_modules_frontmatter(content: str) -> List[str]:
    """Extract ``modules:`` list from YAML-ish frontmatter at the top of *content*.

    Parses both compact inline form::

        modules: [a.py, b.py]

    and multi-line form::

        modules:
          - a.py
          - b.py

    Returns an empty list when the frontmatter is absent, empty, or
    unparseable.  Never raises.
    """
    lines = content.splitlines()
    # Only look inside the frontmatter fence (``---`` delimiters) if present.
    in_fence = False
    fence_lines: List[str] = []
    for line in lines[:40]:  # bounded scan — frontmatter is always at top
        stripped = line.strip()
        if stripped == "---":
            if not in_fence:
                in_fence = True
                continue
            else:
                break  # closing fence
        if in_fence:
            fence_lines.append(stripped)
        elif stripped.startswith("modules:"):
            # No fence — treat this line (and the next few) as the only source
            fence_lines = [stripped]
            idx = lines.index(line)
            for follow in lines[idx + 1 : idx + 20]:
                fs = follow.strip()
                if fs.startswith("-"):
                    fence_lines.append(fs)
                elif fs and not fs.startswith(" ") and not fs.startswith("\t"):
                    break  # another key
            break

    modules: List[str] = []
    consuming_modules = False
    for line in fence_lines:
        if line.startswith("modules:"):
            rest = line[len("modules:"):].strip()
            if rest.startswith("[") and rest.endswith("]"):
                # inline list: modules: [a.py, b.py]
                inner = rest[1:-1]
                for item in inner.split(","):
                    item = item.strip().strip("'\"")
                    if item:
                        modules.append(item)
                consuming_modules = False
            elif rest:
                # single value on same line
                modules.append(rest.strip().strip("'\""))
                consuming_modules = False
            else:
                consuming_modules = True
        elif consuming_modules:
            if line.startswith("-"):
                val = line[1:].strip().strip("'\"")
                if val:
                    modules.append(val)
            elif line and not line.startswith(" ") and not line.startswith("\t"):
                consuming_modules = False

    return modules


# ---------------------------------------------------------------------------
# Topic fragment — lightweight container (avoids SnapshotFragment validation
# constraints while preserving the same field surface for consumers)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TopicFragment:
    """A lightweight topic record loaded from ``docs/memory_topics/**/*.md``."""

    source_id: str
    uri: str          # relative path from project_root
    title: str
    summary: str      # first ~500 chars of content
    modules: Tuple[str, ...]  # parsed ``modules:`` frontmatter entries
    content_hash: str


def _hash_content(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def _extract_title(content: str, path: Path) -> str:
    """Extract title from first H1 heading or fall back to the stem."""
    for line in content.splitlines()[:20]:
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return path.stem.replace("_", " ").replace("-", " ").title()


def _load_topic_fragments(topics_dir: Path, project_root: Path) -> List[TopicFragment]:
    """Recursively load all ``.md`` files under *topics_dir* as TopicFragments.

    Returns an empty list if the directory does not exist.  Never raises.
    """
    if not topics_dir.is_dir():
        return []

    fragments: List[TopicFragment] = []
    for md_file in sorted(topics_dir.rglob("*.md")):
        try:
            content = md_file.read_text(encoding="utf-8", errors="replace")
            try:
                uri = str(md_file.relative_to(project_root))
            except ValueError:
                uri = str(md_file)

            stem = md_file.stem
            source_id = f"memory_topic:{stem}"
            title = _extract_title(content, md_file)
            summary = content.strip()[:500]
            modules = tuple(_parse_modules_frontmatter(content))
            content_hash = _hash_content(content)

            fragments.append(
                TopicFragment(
                    source_id=source_id,
                    uri=uri,
                    title=title,
                    summary=summary,
                    modules=modules,
                    content_hash=content_hash,
                )
            )
        except Exception:  # noqa: BLE001 — fail-soft per spec
            logger.debug("[ModuleRouter] skipping topic file %s (read error)", md_file, exc_info=True)

    return fragments


# ---------------------------------------------------------------------------
# Oracle-based related-module extraction (AST-bound signal)
# ---------------------------------------------------------------------------

def _get_oracle_related_modules(target_files: List[str]) -> List[str]:
    """Lazy-import TheOracle and extract related module file-paths via the
    real AST dependency graph (find_nodes_in_file → get_dependents).

    Returns an empty list on any error (fail-soft).  The Oracle is resolved
    via the ``get_oracle()`` factory — the canonical singleton accessor used
    throughout the codebase.  (The previous implementation erroneously
    imported ``Oracle`` / called ``Oracle.get_instance()`` /
    ``compute_blast_radius`` — none of which exist on the real API; the
    ImportError was swallowed, silently defeating the AST signal.)
    """
    related: List[str] = []
    try:
        from backend.core.ouroboros.oracle import get_oracle  # lazy import — real factory

        oracle = get_oracle()
        if oracle is None:
            return []

        seen: set = set()
        for target in target_files:
            try:
                nodes = oracle.find_nodes_in_file(target)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[ModuleRouter] find_nodes_in_file failed for %s", target, exc_info=True
                )
                continue
            for node in nodes:
                try:
                    for dep in oracle.get_dependents(str(node)):
                        fp = getattr(dep, "file_path", None)
                        if fp and fp not in seen:
                            seen.add(fp)
                            related.append(fp)
                except Exception:  # noqa: BLE001
                    continue
    except Exception:  # noqa: BLE001
        logger.debug("[ModuleRouter] Oracle unavailable — skipping AST signal", exc_info=True)

    return related


# ---------------------------------------------------------------------------
# Structural boost: topic × related-module overlap (path-tail match)
# ---------------------------------------------------------------------------

def _path_tail(path: str) -> str:
    """Return the filename (basename) of a path string."""
    return Path(path).name


def _structural_score(
    topic: TopicFragment,
    related_modules: Sequence[str],
    target_files: Sequence[str],
) -> float:
    """Return a [0.0, 1.0] structural score for a topic.

    A topic scores 1.0 when its ``modules:`` frontmatter overlaps (by
    path-tail) with the target files or Oracle-derived related modules.
    """
    if not topic.modules:
        return 0.0

    candidate_tails = {_path_tail(m) for m in topic.modules}
    target_tails = {_path_tail(f) for f in target_files}
    related_tails = {_path_tail(m) for m in related_modules}

    # Direct match with target files: strong signal
    if candidate_tails & target_tails:
        return 1.0

    # Overlap with Oracle-derived related modules: moderate signal
    if candidate_tails & related_tails:
        return 0.6

    return 0.0


# ---------------------------------------------------------------------------
# Semantic ranking via lazy _embedder_factory / _cosine
# ---------------------------------------------------------------------------

def _embed_texts(texts: List[str]) -> Optional[List[List[float]]]:
    """Lazy-import _embedder_factory and embed *texts*.  Returns None on failure."""
    try:
        from backend.core.ouroboros.governance.semantic_index import (  # lazy
            _embedder_factory,
        )
        embedder = _embedder_factory()
        return embedder.embed(texts)
    except Exception:  # noqa: BLE001
        logger.debug("[ModuleRouter] embedder unavailable", exc_info=True)
        return None


def _cosine_score(a: Sequence[float], b: Sequence[float]) -> float:
    """Lazy-import _cosine from semantic_index.  Falls back to 0.0 on failure."""
    try:
        from backend.core.ouroboros.governance.semantic_index import _cosine  # lazy
        return _cosine(a, b)
    except Exception:  # noqa: BLE001
        return 0.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RoutedContext:
    """Result returned by :meth:`ModuleContextRouter.route`.

    Attributes
    ----------
    topics:
        Selected :class:`TopicFragment` instances in ranked order.
    section:
        Rendered ``## Relevant Architecture Memory`` prompt block.
        Empty string when there are no topics to inject.
    """

    topics: Tuple[TopicFragment, ...]
    section: str

    @classmethod
    def empty(cls) -> "RoutedContext":
        return cls(topics=(), section="")


def _render_section(topics: List[TopicFragment]) -> str:
    """Render a ``## Relevant Architecture Memory`` prompt block."""
    if not topics:
        return ""

    lines = ["## Relevant Architecture Memory", ""]
    for topic in topics:
        lines.append(f"### {topic.title}")
        lines.append(f"*Source: {topic.uri}*")
        lines.append("")
        lines.append(topic.summary)
        lines.append("")

    return "\n".join(lines).rstrip()


class ModuleContextRouter:
    """AST-bound memory context router.

    Usage::

        router = ModuleContextRouter(project_root=Path("/path/to/repo"))
        ctx = router.route(
            target_files=["backend/core/ouroboros/governance/orchestrator.py"],
            query="refactor the PLAN phase timeout handling",
        )
        if ctx.section:
            prompt += "\\n\\n" + ctx.section

    All I/O is fail-soft.  If the Oracle, embedder, or topics directory is
    unavailable the router returns an empty :class:`RoutedContext` without
    raising.

    Gated: returns empty when :func:`routing_enabled` is False.
    """

    def __init__(
        self,
        project_root: Path,
        *,
        topics_dir: Optional[Path] = None,
    ) -> None:
        self._project_root = project_root
        self._topics_dir = (
            topics_dir if topics_dir is not None
            else project_root / "docs" / "memory_topics"
        )

    # ------------------------------------------------------------------
    # Public method
    # ------------------------------------------------------------------

    def route(
        self,
        target_files: List[str],
        query: str,
        *,
        max_topics: int = 3,
        token_budget: int = 2000,
    ) -> RoutedContext:
        """Select and render the most relevant memory topics for this op.

        Parameters
        ----------
        target_files:
            The op's target file paths (relative or absolute).
        query:
            The op description / intent string used for semantic ranking.
        max_topics:
            Maximum number of topics to include (default 3).
        token_budget:
            Approximate character budget for topic summaries (chars / 4 ≈
            tokens).  Topics are dropped once this budget is exhausted.

        Returns
        -------
        RoutedContext
            Selected topics + rendered prompt section.  Returns an empty
            context when the flag is off, no topics exist, or any error
            occurs.
        """
        if not routing_enabled():
            return RoutedContext.empty()

        try:
            return self._route_impl(target_files, query, max_topics, token_budget)
        except Exception:  # noqa: BLE001 — advisory path, never break pipeline
            logger.warning("[ModuleRouter] route() failed — returning empty context", exc_info=True)
            return RoutedContext.empty()

    # ------------------------------------------------------------------
    # Internal implementation
    # ------------------------------------------------------------------

    def _route_impl(
        self,
        target_files: List[str],
        query: str,
        max_topics: int,
        token_budget: int,
    ) -> RoutedContext:
        # 1. Load topic fragments
        all_topics = _load_topic_fragments(self._topics_dir, self._project_root)
        if not all_topics:
            return RoutedContext.empty()

        # 2. AST-bound candidate set via Oracle (fail-soft → empty)
        related_modules = _get_oracle_related_modules(target_files)

        # 3. Compute structural scores
        structural: List[Tuple[float, TopicFragment]] = [
            (_structural_score(t, related_modules, target_files), t)
            for t in all_topics
        ]

        # 4. Semantic ranking via embedder
        sem_scores: List[float] = self._semantic_scores(query, all_topics)

        # 5. Combine structural + semantic: structural_boost * 0.5 + cosine * 0.5
        #    (structural dominates when present; semantic is the tiebreaker)
        _STRUCT_WEIGHT = 0.5
        _SEM_WEIGHT = 0.5

        combined: List[Tuple[float, TopicFragment]] = []
        for i, topic in enumerate(all_topics):
            struct_s = structural[i][0]
            sem_s = sem_scores[i] if i < len(sem_scores) else 0.0
            score = struct_s * _STRUCT_WEIGHT + sem_s * _SEM_WEIGHT
            combined.append((score, topic))

        # Sort descending by score, then by title for determinism
        combined.sort(key=lambda x: (-x[0], x[1].title))

        # 6. Apply max_topics + token_budget
        selected: List[TopicFragment] = []
        char_used = 0
        for score, topic in combined:
            if len(selected) >= max_topics:
                break
            topic_chars = len(topic.summary)
            if char_used + topic_chars > token_budget:
                # Skip if it would blow the budget, unless nothing selected yet
                if selected:
                    continue
            selected.append(topic)
            char_used += topic_chars

        if not selected:
            return RoutedContext.empty()

        section = _render_section(selected)
        return RoutedContext(topics=tuple(selected), section=section)

    def _semantic_scores(
        self,
        query: str,
        topics: List[TopicFragment],
    ) -> List[float]:
        """Return per-topic cosine scores against the query.

        Falls back to uniform 0.0 scores if the embedder is unavailable so
        only the structural signal governs ranking.
        """
        zero = [0.0] * len(topics)
        if not topics or not query.strip():
            return zero

        try:
            summaries = [t.summary for t in topics]
            texts = [query] + summaries
            vecs = _embed_texts(texts)
            if vecs is None or len(vecs) < 2:
                return zero

            query_vec = vecs[0]
            scores = [
                _cosine_score(query_vec, vecs[i + 1])
                for i in range(len(topics))
            ]
            return scores
        except Exception:  # noqa: BLE001
            logger.debug("[ModuleRouter] semantic scoring failed", exc_info=True)
            return zero
