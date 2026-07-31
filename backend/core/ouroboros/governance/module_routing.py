"""module_routing.py — AST-bound hierarchical context router for O+V.

Given an op's target files, injects ONLY the architecture-memory topics
relevant to the module under work.  Routing signal = Oracle dependency graph
(AST-bound), NOT filename string-matching.

Gated default-ON (``JARVIS_MEMORY_ROUTING_ENABLED``, graduated 2026-07-31).
Authority-free / advisory: produces prompt text only, fail-silent like
StrategicDirection.  Never imports oracle / semantic_index / source_crawlers
at module level — all three are lazy-imported inside methods to avoid reverse
dependency / import cycles.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import dataclasses
import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Tuple

if TYPE_CHECKING:  # pragma: no cover
    pass

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Feature gate
# ---------------------------------------------------------------------------

_ENV_FLAG = "JARVIS_MEMORY_ROUTING_ENABLED"


def routing_enabled() -> bool:
    """Whether architecture-memory routing is active. NEVER raises.

    Default: **True** (graduated 2026-07-31).

    THE definition of this flag. `memory_surface._routing_row` used to read
    the same env var with its own default of ``"1"`` while this defaulted to
    OFF — so ``/memory`` reported ``routing: on`` for the entire period
    routing was silently disabled. One knob, two answers, and the surface
    built to tell an operator the truth was the one asserting the falsehood.

    Graduated on evidence, not on age: soak bt-2026-07-31-185316 showed a
    real op routing 3 topics from a 387-topic git-tracked corpus into a
    GENERATE prompt, with the admission ledger recording
    ``considered=387 admitted=3``. The path is fail-soft end to end — any
    error returns an empty context and the pipeline continues — so the
    downside of default-ON is a wasted corpus scan, while the downside of
    default-OFF was an entire subsystem that looked alive and was not.

    ``JARVIS_MEMORY_ROUTING_ENABLED=0`` restores the previous behaviour
    exactly: `route()` returns empty before any I/O.
    """
    raw = os.environ.get(_ENV_FLAG, "").strip().lower()
    if not raw:
        return True
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
    #: Referential staleness verdict from :mod:`memory_corpus` — whether the
    #: modules this topic DECLARES have moved since it was written. Defaults
    #: to ``"unknown"``, which is never penalised: absence of evidence must
    #: not quietly re-rank the corpus.
    drift: str = "unknown"


def _hash_content(text: str) -> str:
    """SHA-256 of the NORMALISED payload, truncated. Pure. NEVER raises.

    Normalisation is what makes this an identity rather than a checksum.
    Two copies of one topic differ in ways that are not content: CRLF from a
    Windows checkout, a trailing newline an editor added, indentation
    whitespace a sync client rewrote. Hashing the raw bytes would call those
    different documents and defeat the deduplication they are supposed to
    enable.

    Deliberately does NOT strip interior blank lines or normalise case: two
    topics that differ only in emphasis or paragraphing ARE different
    documents, and a hash aggressive enough to merge them would silently drop
    real memory. Normalise transport artefacts; preserve authorship.
    """
    normalised = "\n".join(
        line.rstrip() for line in str(text or "").replace("\r\n", "\n")
                                                 .replace("\r", "\n")
                                                 .split("\n")
    ).strip()
    return hashlib.sha256(
        normalised.encode("utf-8", errors="replace")).hexdigest()[:16]


def _extract_title(content: str, path: Path) -> str:
    """Extract title from first H1 heading or fall back to the stem."""
    for line in content.splitlines()[:20]:
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return path.stem.replace("_", " ").replace("-", " ").title()


def _load_topic_fragments_worker(
    topics_dir_str: str, project_root_str: str,
) -> Tuple[List[TopicFragment], Any]:
    """Module-level worker for :func:`_load_topic_fragments`.

    Dispatched into the shared ``advisor-blast`` thread pool via
    ``cooperative_fs_io.offload`` (fs-hot-tier Batch 3, row 21). Lifted
    to module level so the offload trampoline doesn't capture any
    caller-local state. NEVER raises — a single unreadable/malformed
    topic file is skipped, matching the original per-file fail-soft
    semantics.

    Returns ``(fragments, listing)``. The listing travels with the
    fragments because the ADMISSION record needs to state which corpus
    was offered and how confidently it was known, and recomputing that
    downstream would mean scanning twice and could disagree with itself
    if the tree moved in between.

    The corpus comes from :mod:`memory_corpus`, not from ``rglob``. This
    loader used to walk the working tree and inject whatever the
    filesystem held — which, in a repo living under a synced
    ``~/Documents``, meant 764 files where git tracks 383. Hash dedup
    caught the byte-identical conflict copies and left 301 DIVERGED ones
    standing as first-class topics: same title, same ``modules:``, older
    body, competing for the same three slots in every GENERATE prompt.

    Asking the repository what it declares fixes that cause and every
    sibling cause at once — editor backups, a vendored second checkout,
    stray downloads — because "ignored" is the repo stating a file is not
    part of itself. The hash dedup below stays as the second line: it
    catches duplication the VCS cannot see, such as one topic
    copy-pasted into two tracked paths.
    """
    topics_dir = Path(topics_dir_str)
    project_root = Path(project_root_str)

    from backend.core.ouroboros.governance.memory_corpus import (
        corpus_listing_sync, drift_readings_sync, read_topic_text,
    )

    listing = corpus_listing_sync(project_root, topics_dir)
    if not listing.paths:
        return [], listing

    fragments: List[TopicFragment] = []
    #: content_hash -> the URI that claimed it first. Identity is the PAYLOAD,
    #: never the path.
    #
    #: This loader walked `rglob("*.md")` and injected whatever the filesystem
    #: held, so an iCloud conflict copy (`battle_test 2/`, from this repo
    #: living under a synced `~/Documents`) was loaded as a SECOND topic —
    #: every duplicated fragment paying full tokens in a GENERATE prompt to
    #: say what the first one already said.
    #
    #: Filtering the name would have fixed that one cause and no other.
    #: Hashing the payload immunises against every cause at once — sync
    #: copies, symlink loops, a hand copy-paste, a vendored second checkout —
    #: because none of them can change what the document IS. `_hash_content`
    #: was already computed here for every fragment and simply never
    #: consulted; the deduplication was one comparison away the whole time.
    #:
    #: FIRST path wins, and `sorted()` above makes "first" deterministic: the
    #: same corpus yields the same surviving URI on every boot, so a topic's
    #: recorded provenance does not shuffle between runs.
    seen_hashes: Dict[str, str] = {}
    duplicates = 0
    for md_file in listing.paths:
        try:
            content = read_topic_text(md_file)
            if not content:
                continue
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

            first = seen_hashes.get(content_hash)
            if first is not None:
                # Silently, per the mandate — but COUNTED. A dedup that
                # leaves no trace is indistinguishable from a loader that
                # never saw the file, and the difference matters the day
                # someone asks why a topic they wrote is not in a prompt.
                duplicates += 1
                logger.debug(
                    "[ModuleRouter] duplicate payload %s == %s (hash %s)",
                    uri, first, content_hash,
                )
                continue
            seen_hashes[content_hash] = uri

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

    # Grade referential staleness in ONE history pass over the whole batch.
    # Per-topic grading would be ~1,400 `git log -1` spawns for this corpus;
    # `drift_readings_sync` walks history once and answers all of them.
    try:
        readings = drift_readings_sync(
            project_root, [(f.uri, f.modules) for f in fragments])
        fragments = [
            dataclasses.replace(
                f, drift=readings[f.uri].drift.value) if f.uri in readings else f
            for f in fragments
        ]
    except Exception:  # noqa: BLE001 — advisory signal; corpus stands without it
        logger.debug("[ModuleRouter] drift grading skipped", exc_info=True)

    logger.info(
        "[ModuleRouter] corpus %d topics [%s] (%d untracked excluded, "
        "%d duplicate payloads dropped)",
        len(fragments), listing.provenance.value, listing.excluded, duplicates,
    )
    return fragments, listing


async def _load_topic_fragments(
    topics_dir: Path, project_root: Path,
) -> Tuple[List[TopicFragment], Any]:
    """Load the declared topic corpus as ``(fragments, listing)``. NEVER raises.

    fs-hot-tier Batch 3 (row 21): the corpus scan + per-file read is
    dispatched off the asyncio loop via
    ``cooperative_fs_io.offload(cpu_bound=False)`` — thread pool (read
    + light parse is IO-bound). Fail-soft: an ``OffloadError``
    degrades to an empty corpus carrying an ABSENT listing, so the
    admission record can still say WHY nothing was offered rather than
    reporting an empty corpus as a verified fact.
    """
    from backend.core.ouroboros.governance.memory_corpus import CorpusListing

    if not topics_dir.is_dir():
        return [], CorpusListing.absent(f"{topics_dir} is not a directory")

    from backend.core.ouroboros.governance.cooperative_fs_io import (
        offload,
        is_offload_error,
    )
    result = await offload(
        _load_topic_fragments_worker,
        str(topics_dir), str(project_root),
        cpu_bound=False,
    )
    if is_offload_error(result):
        logger.debug(
            "[ModuleRouter] _load_topic_fragments offload failed — "
            "degrading to empty corpus",
        )
        return [], CorpusListing.absent("corpus scan offload failed")
    return result


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
# Module-level embedding cache: content_hash → vector
# Keyed by content_hash (str, 16-hex chars) → list[float].
# Persisted to .jarvis/memory_topics_emb.npz; loaded lazily on first use
# per project_root.  Fail-soft: any I/O error → skip cache, embed live.
# ---------------------------------------------------------------------------

_emb_cache: dict[str, List[float]] = {}
_emb_cache_loaded_roots: set[str] = set()
_PREFILTER_K = 24  # lexical fallback candidate count


def _emb_cache_path(project_root: Path) -> Path:
    return project_root / ".jarvis" / "memory_topics_emb.npz"


def _load_emb_cache_from_disk(project_root: Path) -> None:
    """Populate _emb_cache from .jarvis/memory_topics_emb.npz. Fail-soft."""
    try:
        import numpy as np  # noqa: PLC0415 — optional dep
    except Exception:  # noqa: BLE001
        return
    path = _emb_cache_path(project_root)
    if not path.exists():
        return
    try:
        # SECURITY: allow_pickle is required only because _persist_emb_cache
        # writes hashes as an object-dtype string array.  This .npz is a
        # HOST-LOCAL, SELF-WRITTEN cache under the repo's own .jarvis/
        # (written exclusively by this process) — NOT an untrusted external
        # source.  An attacker who could overwrite it would already have local
        # write/code-exec on the host, so this adds no new attack surface.
        # Mirrors the identical justification in semantic_index._load_from_cache.
        data = np.load(path, allow_pickle=True)
        hashes = list(data["hashes"])
        vectors = data["vectors"]
        for i, h in enumerate(hashes):
            key = str(h)
            if key not in _emb_cache:
                _emb_cache[key] = [float(x) for x in vectors[i]]
    except Exception:  # noqa: BLE001
        logger.debug("[ModuleRouter] emb cache load failed", exc_info=True)


def _persist_emb_cache(project_root: Path) -> None:
    """Write _emb_cache to .jarvis/memory_topics_emb.npz. Fail-soft."""
    if not _emb_cache:
        return
    try:
        import numpy as np  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return
    try:
        cache_dir = project_root / ".jarvis"
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = _emb_cache_path(project_root)
        hashes = list(_emb_cache.keys())
        vecs = [_emb_cache[h] for h in hashes]
        vectors = np.array(vecs, dtype="float32")
        np.savez(path, hashes=np.array(hashes, dtype=object), vectors=vectors)
    except Exception:  # noqa: BLE001
        logger.debug("[ModuleRouter] emb cache persist failed", exc_info=True)


def _embed_texts_cached(
    texts_with_hashes: List[Tuple[str, str]],
    project_root: Path,
) -> Optional[List[List[float]]]:
    """Embed texts using the module-level cache; persist new embeddings.

    Parameters
    ----------
    texts_with_hashes:
        List of (text, content_hash) pairs.  Hashes are used as cache keys.
    project_root:
        Used to locate the .jarvis/memory_topics_emb.npz cache file.

    Returns
    -------
    List of vectors (one per input text), or None on total failure.
    Fail-soft: returns None if any live embedding call fails.
    """
    global _emb_cache, _emb_cache_loaded_roots  # noqa: PLW0603

    root_key = str(project_root)
    if root_key not in _emb_cache_loaded_roots:
        _load_emb_cache_from_disk(project_root)
        _emb_cache_loaded_roots.add(root_key)

    results: List[Optional[List[float]]] = [None] * len(texts_with_hashes)
    uncached_indices: List[int] = []
    uncached_texts: List[str] = []

    for i, (text, hash_) in enumerate(texts_with_hashes):
        if hash_ in _emb_cache:
            results[i] = _emb_cache[hash_]
        else:
            uncached_indices.append(i)
            uncached_texts.append(text)

    if uncached_texts:
        new_vecs = _embed_texts(uncached_texts)
        if new_vecs is None:
            return None  # fail-soft: don't return partial
        for j, idx in enumerate(uncached_indices):
            vec = new_vecs[j]
            results[idx] = vec
            _, hash_ = texts_with_hashes[idx]
            _emb_cache[hash_] = vec
        _persist_emb_cache(project_root)

    if any(r is None for r in results):
        return None
    return results  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Candidate-first narrowing: path-tail intersection → lexical pre-filter
# ---------------------------------------------------------------------------

_TOKEN_SPLIT_RE = re.compile(r"[^a-zA-Z0-9]+")


def _lexical_prefilter(
    query: str,
    topics: List["TopicFragment"],
    k: int,
) -> List["TopicFragment"]:
    """Return top-k topics ranked by token-overlap with *query*.

    Splits query and topic (title + summary) on non-alphanumeric boundaries,
    lower-cases, and counts intersection size.  O(N) — no embedding required.
    Falls back to the first-k topics when the query is empty.
    """
    if not topics:
        return []
    query_tokens = set(_TOKEN_SPLIT_RE.split(query.lower())) - {""}
    if not query_tokens:
        return topics[:k]
    scored: List[Tuple[int, "TopicFragment"]] = []
    for topic in topics:
        text = (topic.title + " " + topic.summary).lower()
        topic_tokens = set(_TOKEN_SPLIT_RE.split(text)) - {""}
        overlap = len(query_tokens & topic_tokens)
        scored.append((overlap, topic))
    scored.sort(key=lambda x: -x[0])
    return [t for _, t in scored[:k]]


def _candidate_topics(
    all_topics: List["TopicFragment"],
    target_files: Sequence[str],
    related_modules: Sequence[str],
    query: str,
    prefilter_k: int = _PREFILTER_K,
) -> List["TopicFragment"]:
    """Return the narrowed topic set for semantic embedding.

    Two-stage narrowing:

    1. **Path-tail intersection**: topics whose ``modules:`` path-tails overlap
       with any target-file tail or Oracle-derived related-module tail.  If
       non-empty, return only those candidates (precise + cheap).

    2. **Lexical pre-filter** (fallback): if no path-tail match, pick the top
       ``prefilter_k`` topics by query-vs-(title+summary) token overlap.
       Guarantees at most ``prefilter_k`` topics reach the embedder.

    In both cases the number of topics embedded is O(candidates), NOT O(all).
    """
    target_tails = {_path_tail(f) for f in target_files}
    related_tails = {_path_tail(m) for m in related_modules}
    all_tails = target_tails | related_tails

    candidates: List["TopicFragment"] = []
    for topic in all_topics:
        topic_tails = {_path_tail(m) for m in topic.modules}
        if topic_tails & all_tails:
            candidates.append(topic)

    if candidates:
        return candidates

    # Lexical fallback — no path match
    return _lexical_prefilter(query, all_topics, prefilter_k)


# ---------------------------------------------------------------------------
# Structural boost: topic × related-module overlap (path-tail match)
# ---------------------------------------------------------------------------

def _path_tail(path: str) -> str:
    """Return the filename (basename) of a path string."""
    return Path(path).name


_utility_armed = False


def _arm_utility_listener_once() -> None:
    """Subscribe the utility store to VERIFY telemetry. NEVER raises."""
    global _utility_armed  # noqa: PLW0603
    if _utility_armed:
        return
    try:
        from backend.core.ouroboros.governance.memory_utility import (
            arm_outcome_listener, utility_enabled,
        )
        if utility_enabled():
            _utility_armed = arm_outcome_listener()
    except Exception:  # noqa: BLE001
        logger.debug("[ModuleRouter] utility listener not armed", exc_info=True)


def _utility_multiplier(content_hash: str) -> float:
    """Outcome-learned rank weight for a topic. NEVER raises.

    Lazy-imported and fail-open at 1.0, so a missing or broken utility store
    costs the ranker exactly nothing — the closed loop is an improvement on
    the open one, never a dependency of it.
    """
    try:
        from backend.core.ouroboros.governance.memory_utility import (
            utility_for,
        )
        return utility_for(content_hash)
    except Exception:  # noqa: BLE001
        return 1.0


def _drift_multiplier(drift: str) -> float:
    """Rank weight for a staleness verdict. NEVER raises.

    Only ``drifted`` is penalised. ``unknown`` — history could not decide —
    weighs exactly 1.0, so a topic older than the scan window ranks precisely
    as it did before staleness existed. Penalising on absence of evidence
    would bury the oldest half of the corpus under a heuristic that never
    measured it, which is a rewrite disguised as a hint.
    """
    try:
        if str(drift) != "drifted":
            return 1.0
        from backend.core.ouroboros.governance.memory_corpus import (
            staleness_penalty,
        )
        return staleness_penalty()
    except Exception:  # noqa: BLE001
        return 1.0


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
    #: The :class:`memory_admission.AdmissionRecord` this pass filed, when
    #: the ledger is on. Carried so a caller can log the corpus provenance it
    #: actually got instead of asserting one — the difference between
    #: "3 topics injected" and "3 of 383 git-tracked, 2 cut by budget".
    record: Any = None

    @classmethod
    def empty(cls) -> "RoutedContext":
        return cls(topics=(), section="", record=None)


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

    async def route(
        self,
        target_files: List[str],
        query: str,
        *,
        max_topics: int = 3,
        token_budget: int = 2000,
        op_id: str = "",
        consumer: str = "main",
        exclude_hashes: Sequence[str] = (),
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
        op_id:
            The operation this routing pass serves.  Recorded on the
            admission ledger so ``/memory context`` can answer "what did
            THIS op load".
        consumer:
            Who is asking — ``main`` or one of the EXPLORE / REVIEW / PLAN /
            GENERAL subagents.  Claude Code deliberately does not inherit
            conversation memory into subagents; O+V had never made that
            choice either way, which meant four call sites were making it by
            accident.  Naming the consumer turns an accident into something
            readable off the ledger.
        exclude_hashes:
            Content hashes to withhold from selection.  Used by the REVIEW
            subagent's ``COMPLEMENT`` scope: a reviewer handed the same
            topics the author had inherits the author's blind spot and
            cannot catch a mistake the memory itself caused.  Excluded
            topics are still RECORDED, with a distinct reason — "you were
            deliberately not shown this" and "this ranked low" are different
            facts about the same absence.

        Returns
        -------
        RoutedContext
            Selected topics + rendered prompt section.  Returns an empty
            context when the flag is off, no topics exist, or any error
            occurs.
        """
        if not routing_enabled():
            return RoutedContext.empty()

        # Arm the outcome listener at the first real route. Deliberately here
        # rather than at import or at boot: the subscription is only
        # meaningful once memory is actually being injected, and this is the
        # one code path that proves it is. A boot-time hook would have to
        # guess, and a flag flipped mid-session would leave it unarmed —
        # which is precisely the wired-but-inert failure this codebase keeps
        # rediscovering. Idempotent, so paying it per-call is free.
        _arm_utility_listener_once()

        try:
            return await self._route_impl(
                target_files, query, max_topics, token_budget, op_id, consumer,
                tuple(exclude_hashes or ()))
        except Exception:  # noqa: BLE001 — advisory path, never break pipeline
            logger.warning("[ModuleRouter] route() failed — returning empty context", exc_info=True)
            return RoutedContext.empty()

    # ------------------------------------------------------------------
    # Internal implementation
    # ------------------------------------------------------------------

    async def _route_impl(
        self,
        target_files: List[str],
        query: str,
        max_topics: int,
        token_budget: int,
        op_id: str = "",
        consumer: str = "main",
        exclude_hashes: Tuple[str, ...] = (),
    ) -> RoutedContext:
        # 1. Load the DECLARED topic corpus (git-tracked, drift-graded)
        all_topics, listing = await _load_topic_fragments(
            self._topics_dir, self._project_root)
        if not all_topics:
            # An empty corpus is still a routing pass, and saying WHY it was
            # empty is the whole point of the ledger. "nothing loaded because
            # the topics dir is missing" and "nothing loaded because nothing
            # scored" are different diagnoses that render identically as an
            # absent record.
            self._file_record(
                op_id, consumer, rows=[], listing=listing,
                token_budget=token_budget, query=query,
                target_files=target_files,
            )
            return RoutedContext.empty()

        # 2. AST-bound candidate set via Oracle (fail-soft → empty)
        related_modules = _get_oracle_related_modules(target_files)

        # 3. Compute structural scores (cheap — over ALL topics)
        structural_map: dict[str, float] = {
            t.source_id: _structural_score(t, related_modules, target_files)
            for t in all_topics
        }

        # 4. Candidate-first narrowing: only embed the relevant subset.
        #    Path-tail intersection → candidates; fallback to lexical prefilter.
        #    This bounds the embedder to O(candidates), NOT O(all_topics).
        embed_topics = _candidate_topics(
            all_topics, target_files, related_modules, query
        )

        # 5. Semantic ranking via embedder + persisted cache (only on candidates)
        sem_scores_list: List[float] = self._semantic_scores(query, embed_topics)
        sem_map: dict[str, float] = {
            embed_topics[i].source_id: sem_scores_list[i]
            for i in range(len(embed_topics))
        }

        # 6. Combine structural + semantic.
        #    Topics outside the candidate set receive sem_score = 0.0 but
        #    retain their structural score — strong structural matches still
        #    surface even when the embedder was not run on them.
        _STRUCT_WEIGHT = 0.5
        _SEM_WEIGHT = 0.5

        combined: List[Tuple[float, TopicFragment]] = []
        for topic in all_topics:
            struct_s = structural_map.get(topic.source_id, 0.0)
            sem_s = sem_map.get(topic.source_id, 0.0)
            base = struct_s * _STRUCT_WEIGHT + sem_s * _SEM_WEIGHT
            # Referential staleness is a WEIGHT, never a filter. A topic whose
            # subject has moved is still the best surviving record of why that
            # subject looks the way it does; the right response is to prefer a
            # current one when both are available, not to forget the decision.
            # UNKNOWN multiplies by 1.0 — absence of evidence must not re-rank
            # the corpus.
            # Two independent, multiplicative advisories on top of the
            # measured relevance signals. Both default to 1.0 when they have
            # nothing to say, so a cold corpus ranks exactly as it did before
            # either existed.
            combined.append((
                base
                * _drift_multiplier(topic.drift)
                * _utility_multiplier(topic.content_hash),
                topic,
            ))

        # Sort descending by score, then by title for determinism
        combined.sort(key=lambda x: (-x[0], x[1].title))

        # 7. Apply max_topics + token_budget, recording every outcome.
        #    Each topic leaves a row whether or not it got in: "these three
        #    loaded" is inferable from the prompt, while "this one lost to
        #    budget by 200 characters" is the fact that explains a bad
        #    generation, and it exists nowhere else.
        selected: List[TopicFragment] = []
        rows: List[Any] = []
        char_used = 0
        excluded = set(exclude_hashes or ())
        for score, topic in combined:
            chars = len(topic.summary)
            if topic.content_hash in excluded:
                # Withheld on purpose, and recorded as such. Folding this
                # into "ranked below cutoff" would erase the only evidence
                # that a scoping POLICY acted — leaving an operator to
                # conclude the ranker simply disliked the topic.
                rows.append(self._row(topic, score, chars, admitted=False,
                                      why="scope_excluded",
                                      structural=structural_map))
                continue
            if len(selected) >= max_topics:
                rows.append(self._row(topic, score, chars, admitted=False,
                                      why="max_topics_reached",
                                      structural=structural_map))
                continue
            if char_used + chars > token_budget and selected:
                rows.append(self._row(topic, score, chars, admitted=False,
                                      why="budget_exhausted",
                                      structural=structural_map))
                continue
            selected.append(topic)
            char_used += chars
            rows.append(self._row(topic, score, chars, admitted=True, why="",
                                  structural=structural_map))

        record = self._file_record(
            op_id, consumer, rows=rows, listing=listing,
            token_budget=token_budget, query=query, target_files=target_files,
        )

        if not selected:
            return RoutedContext(topics=(), section="", record=record)

        section = _render_section(selected)
        return RoutedContext(
            topics=tuple(selected), section=section, record=record)

    # ------------------------------------------------------------------
    # Admission bookkeeping
    # ------------------------------------------------------------------

    @staticmethod
    def _row(
        topic: TopicFragment,
        score: float,
        chars: int,
        *,
        admitted: bool,
        why: str,
        structural: Dict[str, float],
    ) -> Any:
        """One :class:`AdmissionRow`. NEVER raises — returns None on failure.

        Reason attribution mirrors ``context_manifest.reason_for_keep``:
        exactly one structured code per row, strongest signal first, with the
        score breakdown carried alongside for the detail a code cannot hold.
        """
        try:
            from backend.core.ouroboros.governance.memory_admission import (
                AdmissionDecision, AdmissionReason, AdmissionRow,
            )
            struct = structural.get(topic.source_id, 0.0)
            if admitted:
                if struct >= 1.0:
                    reason = AdmissionReason.STRUCTURAL_TARGET
                elif struct > 0.0:
                    reason = AdmissionReason.STRUCTURAL_RELATED
                else:
                    reason = AdmissionReason.SEMANTIC
                decision = AdmissionDecision.ADMITTED
            else:
                # An orphaned topic that also lost is reported as orphaned:
                # "it ranked low" invites raising max_topics, while "its
                # declared modules no longer exist" invites deleting it.
                if why == "scope_excluded":
                    reason = AdmissionReason.SCOPE_EXCLUDED
                elif topic.drift == "orphaned":
                    reason = AdmissionReason.ORPHANED_SUBJECT
                elif why == "budget_exhausted":
                    reason = AdmissionReason.BUDGET_EXHAUSTED
                elif why == "max_topics_reached":
                    reason = AdmissionReason.MAX_TOPICS_REACHED
                else:
                    reason = AdmissionReason.RANK_BELOW_CUTOFF
                decision = AdmissionDecision.WITHHELD
            return AdmissionRow(
                source_id=topic.source_id, uri=topic.uri,
                content_hash=topic.content_hash, decision=decision,
                reason=reason, score=float(score), chars=int(chars),
                drift=topic.drift,
                breakdown=(("structural", round(struct, 4)),
                           ("drift_weight", _drift_multiplier(topic.drift))),
            )
        except Exception:  # noqa: BLE001
            return None

    def _file_record(
        self,
        op_id: str,
        consumer: str,
        *,
        rows: Sequence[Any],
        listing: Any,
        token_budget: int,
        query: str,
        target_files: Sequence[str],
    ) -> Any:
        """File the pass with the admission ledger. NEVER raises.

        Fail-soft in both directions: a broken ledger must not cost the op
        its memory, and a routing pass that produced nothing must still leave
        a record saying so.
        """
        try:
            from backend.core.ouroboros.governance.memory_admission import (
                AdmissionRecord, MemoryConsumer, record_admission,
            )
            return record_admission(AdmissionRecord.of(
                op_id=op_id or "unattributed",
                consumer=MemoryConsumer.coerce(consumer),
                rows=[r for r in rows if r is not None],
                corpus_size=getattr(listing, "size", 0),
                corpus_provenance=getattr(
                    getattr(listing, "provenance", None), "value", "unknown"),
                corpus_excluded=getattr(listing, "excluded", 0),
                char_budget=int(token_budget),
                query=query,
                target_files=target_files,
                extra={"corpus_detail": getattr(listing, "detail", "")},
            ))
        except Exception:  # noqa: BLE001
            logger.debug("[ModuleRouter] admission record skipped", exc_info=True)
            return None

    def _semantic_scores(
        self,
        query: str,
        topics: List[TopicFragment],
    ) -> List[float]:
        """Return per-topic cosine scores against the query.

        Uses the module-level embedding cache (keyed on content_hash) so
        repeated calls with the same topics embed zero new texts.  Falls back
        to uniform 0.0 scores if the embedder is unavailable so only the
        structural signal governs ranking.
        """
        zero = [0.0] * len(topics)
        if not topics or not query.strip():
            return zero

        try:
            query_hash = _hash_content(query)
            texts_with_hashes: List[Tuple[str, str]] = (
                [(query, query_hash)]
                + [(t.summary, t.content_hash) for t in topics]
            )
            vecs = _embed_texts_cached(texts_with_hashes, self._project_root)
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
