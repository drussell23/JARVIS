"""What the memory corpus IS, and whether each topic is still true.

Two questions this answers, and one place they get answered.

Which files are the corpus
--------------------------
Not "every ``.md`` under a directory". The corpus is what the REPOSITORY
declares, and the difference is not cosmetic. This repo lives under a synced
``~/Documents``; the sync client littered ` 2`-suffixed conflict directories
inside ``docs/memory_topics/``, so a tree walk finds 764 files where git
tracks 383. The extra 381 are snapshots — same titles, same ``modules:``
frontmatter, older bodies.

``module_routing`` walked the tree and hash-deduplicated. That caught the
byte-identical copies and left every snapshot that had DIVERGED, which is
precisely the dangerous set: a stale twin of a real topic, scoring almost
identically on structure, competing for the same three slots in a GENERATE
prompt. The organism was being told an old version of what it decided.

Asking git generalises past iCloud for free — editor backups, a vendored
second checkout, stray downloads — because "ignored" is the repository
stating that a file is not part of itself. No pattern list to maintain, and
nothing to update the next time a tool invents a new suffix.

When git cannot answer, this says so rather than pretending. A walk-fallback
listing is returned with :attr:`CorpusProvenance.WALK_FALLBACK` stamped on
it, so a consumer can report a degraded corpus instead of a confident wrong
one. An absent VCS must not read as an empty mind, and it must not read as a
verified one either.

Whether a topic is still true
-----------------------------
Claude Code stamps a ``modified`` timestamp so staleness is visible. That is
better than nothing and it measures the wrong thing: a topic written six
months ago about a module nobody has touched since is not stale, and a topic
written last week about a module rewritten yesterday is.

O+V can do better because its topics already declare ``modules:``. So
staleness here is REFERENTIAL — the topic's own last-commit time against the
last-commit time of the modules it claims to describe. A topic older than its
subject has DRIFTED; a topic whose subject no longer exists is ORPHANED. Both
are facts about content, not about a filesystem.

UNKNOWN is a third answer, and it is load-bearing
--------------------------------------------------
When history cannot decide — the scan window did not reach either commit, git
is unavailable, the file was never committed — the answer is
:attr:`Drift.UNKNOWN`, and unknown is neither penalised nor rendered as
fresh. This is the same discipline the blast-radius gutter keeps: an
unmeasured value that renders as ``0`` is a fabrication, and a fabrication
that looks measured is worse than a blank.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import enum
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.MemoryCorpus")

MEMORY_CORPUS_SCHEMA_VERSION: str = "memory_corpus.1"

__all__ = [
    "MEMORY_CORPUS_SCHEMA_VERSION",
    "CorpusListing",
    "CorpusProvenance",
    "Drift",
    "DriftReading",
    "corpus_listing",
    "corpus_listing_sync",
    "drift_for",
    "drift_readings_sync",
    "corpus_authority_enabled",
    "reset_caches_for_tests",
    "staleness_enabled",
    "staleness_penalty",
]


# ---------------------------------------------------------------------------
# Gates and knobs — every one env-tunable, none hardcoded at a call site
# ---------------------------------------------------------------------------


def _flag(name: str, default: str = "1") -> bool:
    """Truthiness of an env flag. NEVER raises."""
    try:
        return os.environ.get(name, default).strip().lower() not in (
            "0", "false", "no", "off", "")
    except Exception:  # noqa: BLE001
        return default not in ("0", "false", "no", "off", "")


def _num(name: str, default: float, lo: float, hi: float) -> float:
    """A clamped numeric env knob. NEVER raises."""
    try:
        return min(hi, max(lo, float(os.environ.get(name, "").strip() or default)))
    except Exception:  # noqa: BLE001
        return default


def corpus_authority_enabled() -> bool:
    """``JARVIS_MEMORY_CORPUS_AUTHORITY`` (default true).

    OFF restores the legacy tree walk byte-for-byte, ghosts included. The
    rollback exists because a repo that genuinely keeps untracked topics —
    a scratch corpus, a submodule layout git does not list — would lose them
    silently otherwise, and silence is the failure mode this module exists to
    end.
    """
    return _flag("JARVIS_MEMORY_CORPUS_AUTHORITY", "1")


def staleness_enabled() -> bool:
    """``JARVIS_MEMORY_STALENESS_ENABLED`` (default true)."""
    return _flag("JARVIS_MEMORY_STALENESS_ENABLED", "1")


def staleness_penalty() -> float:
    """Rank multiplier applied to a DRIFTED topic. ``JARVIS_MEMORY_DRIFT_PENALTY``.

    A multiplier rather than an exclusion, deliberately. A drifted topic is
    still the best record of an intention even when the code moved on — the
    right response is to prefer a current one when both are available, not to
    forget the old decision. Default 0.6; ``1.0`` disables the effect while
    leaving the reading visible.
    """
    return _num("JARVIS_MEMORY_DRIFT_PENALTY", 0.6, 0.0, 1.0)


def _git_timeout_s() -> float:
    return _num("JARVIS_MEMORY_CORPUS_GIT_TIMEOUT_S", 15.0, 1.0, 120.0)


def _corpus_ttl_s() -> float:
    return _num("JARVIS_MEMORY_CORPUS_TTL_S", 120.0, 0.0, 3600.0)


def _scan_commits() -> int:
    """How far back the single history pass walks. ``JARVIS_MEMORY_STALENESS_SCAN_COMMITS``.

    Bounded because the alternative is unbounded: this repo has ~9,800
    commits and a full ``--name-only`` dump of all of them is megabytes of
    pipe for a ranking hint.

    The bound costs less than it looks. Walking newest-first and keeping the
    FIRST sighting of each path yields exactly "when was this last touched",
    and a path never seen inside the window is simply older than the window.
    That still decides the comparison whenever ONE side is inside it — a
    module edited last week versus a topic git has not seen in 4,000 commits
    is decidably drifted. Only both-outside is genuinely undecidable, and
    that case reports UNKNOWN.
    """
    return int(_num("JARVIS_MEMORY_STALENESS_SCAN_COMMITS", 4000, 200, 100000))


def _max_topic_bytes() -> int:
    """Ceiling on a single topic file read. ``JARVIS_MEMORY_TOPIC_MAX_BYTES``."""
    return int(_num("JARVIS_MEMORY_TOPIC_MAX_BYTES", 262144, 4096, 8388608))


# ---------------------------------------------------------------------------
# Corpus listing
# ---------------------------------------------------------------------------


class CorpusProvenance(str, enum.Enum):
    """How the corpus listing was obtained — reported, never inferred.

    A consumer that shows a topic count owes the operator this alongside it.
    ``383 topics (git-tracked)`` and ``684 topics (walk fallback)`` are
    different claims with different confidence, and a bare number states the
    stronger one regardless of which is true.
    """

    GIT_TRACKED = "git_tracked"
    WALK_FALLBACK = "walk_fallback"
    ABSENT = "absent"


@dataclass(frozen=True)
class CorpusListing:
    """The corpus, plus how confidently we know it is the corpus."""

    paths: Tuple[Path, ...]
    provenance: CorpusProvenance
    #: Files present on disk that the authority disowned. Counted, not
    #: discarded quietly: a dedup or a filter that leaves no trace is
    #: indistinguishable from a loader that never saw the files, and the
    #: difference matters the day someone asks why a topic they wrote is not
    #: in a prompt.
    excluded: int
    detail: str
    scanned_at: float

    @property
    def size(self) -> int:
        return len(self.paths)

    @property
    def degraded(self) -> bool:
        """True when the listing is a fallback rather than the authority."""
        return self.provenance is CorpusProvenance.WALK_FALLBACK

    @classmethod
    def absent(cls, detail: str = "topics directory not found") -> "CorpusListing":
        return cls(paths=(), provenance=CorpusProvenance.ABSENT,
                   excluded=0, detail=detail, scanned_at=time.time())


def _run_git(args: Sequence[str], cwd: Path) -> Optional[str]:
    """``git <args>`` stdout, or None when git cannot answer. NEVER raises.

    None is distinct from ``""``: an empty stdout is git successfully saying
    "nothing", and None is git not saying anything. Collapsing them would
    turn a missing binary into a verified-empty corpus.
    """
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True,
            timeout=_git_timeout_s(), check=False,
        )
        if proc.returncode != 0:
            return None
        return proc.stdout.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        logger.debug("[MemoryCorpus] git %s unavailable", args[:1], exc_info=True)
        return None


def _walk(topics_dir: Path) -> List[Path]:
    """Every ``.md`` under *topics_dir*, deterministically ordered.

    ``rglob`` follows neither symlinked directories on 3.9-3.12 nor recurses
    into them, so the classic walk-loop is not reachable here; the try/except
    covers permission faults on individual subtrees instead.
    """
    try:
        return sorted(p for p in topics_dir.rglob("*.md") if p.is_file())
    except Exception:  # noqa: BLE001
        logger.debug("[MemoryCorpus] tree walk degraded", exc_info=True)
        return []


def corpus_listing_sync(
    project_root: Path,
    topics_dir: Optional[Path] = None,
) -> CorpusListing:
    """The corpus for *project_root*. Blocking. NEVER raises.

    Module-level and picklable so :func:`corpus_listing` can hand it to the
    shared offload pool without capturing caller state.

    Falls back to the tree walk on every path where git declines to answer —
    no repo, no binary, a timeout, a corpus that lives outside the work tree
    — and stamps the fallback so the caller can say which one happened.
    """
    try:
        root = Path(project_root)
        tdir = Path(topics_dir) if topics_dir is not None else (
            root / "docs" / "memory_topics")
        if not tdir.is_dir():
            return CorpusListing.absent(f"{tdir} is not a directory")

        on_disk = _walk(tdir)

        if not corpus_authority_enabled():
            return CorpusListing(
                paths=tuple(on_disk), provenance=CorpusProvenance.WALK_FALLBACK,
                excluded=0, detail="corpus authority disabled by flag",
                scanned_at=time.time(),
            )

        try:
            rel = tdir.relative_to(root).as_posix()
        except ValueError:
            rel = str(tdir)

        out = _run_git(["ls-files", "-z", "--", rel], root)
        if out is None:
            return CorpusListing(
                paths=tuple(on_disk), provenance=CorpusProvenance.WALK_FALLBACK,
                excluded=0,
                detail="git could not list the tree; corpus is unverified",
                scanned_at=time.time(),
            )

        tracked: List[Path] = []
        for name in out.split("\0"):
            if not name.endswith(".md"):
                continue
            path = root / name
            # ls-files reports the INDEX, which can name a file the working
            # tree no longer holds (mid-rebase, a deleted-but-unstaged
            # topic). Reading it would fail per-file anyway; dropping it here
            # keeps the count honest.
            if path.is_file():
                tracked.append(path)

        if not tracked and on_disk:
            # Git answered, and answered "none", while files clearly exist —
            # a corpus outside the work tree, or a path git resolved
            # differently than we did. Trusting the empty answer would make
            # the organism forget everything on the strength of a path bug.
            return CorpusListing(
                paths=tuple(on_disk), provenance=CorpusProvenance.WALK_FALLBACK,
                excluded=0,
                detail=(f"git tracks no topic under {rel!r} but "
                        f"{len(on_disk)} exist on disk; using the tree"),
                scanned_at=time.time(),
            )

        tracked.sort()
        excluded = max(0, len(on_disk) - len(tracked))
        return CorpusListing(
            paths=tuple(tracked), provenance=CorpusProvenance.GIT_TRACKED,
            excluded=excluded,
            detail=(f"{excluded} untracked file(s) on disk excluded"
                    if excluded else "no untracked topics on disk"),
            scanned_at=time.time(),
        )
    except Exception:  # noqa: BLE001
        logger.debug("[MemoryCorpus] listing degraded", exc_info=True)
        return CorpusListing.absent("corpus listing raised")


#: ``(root, topics_dir)`` -> listing. Guarded by ``_cache_lock``.
_listing_cache: Dict[Tuple[str, str], CorpusListing] = {}
_cache_lock = threading.Lock()


async def corpus_listing(
    project_root: Path,
    topics_dir: Optional[Path] = None,
    *,
    force: bool = False,
) -> CorpusListing:
    """:func:`corpus_listing_sync`, off the event loop and TTL-memoised.

    The scan spawns a subprocess and stats hundreds of files; on the async
    path that belongs in the shared ``advisor-blast`` pool, not on the loop
    that is also servicing a full-screen Application.

    Fail-soft: an offload failure degrades to a synchronous call rather than
    to an empty corpus — the wrong answer here is forgetting, and forgetting
    is worse than a brief stall.
    """
    key = (str(project_root), str(topics_dir or ""))
    ttl = _corpus_ttl_s()
    if not force and ttl > 0:
        with _cache_lock:
            hit = _listing_cache.get(key)
        if hit is not None and (time.time() - hit.scanned_at) < ttl:
            return hit

    listing: Optional[CorpusListing] = None
    try:
        from backend.core.ouroboros.governance.cooperative_fs_io import (
            offload, is_offload_error,
        )
        result = await offload(
            corpus_listing_sync, Path(project_root),
            Path(topics_dir) if topics_dir is not None else None,
            cpu_bound=False,
        )
        if not is_offload_error(result):
            listing = result
    except Exception:  # noqa: BLE001
        logger.debug("[MemoryCorpus] offload unavailable", exc_info=True)

    if listing is None:
        listing = corpus_listing_sync(project_root, topics_dir)

    with _cache_lock:
        _listing_cache[key] = listing
    return listing


# ---------------------------------------------------------------------------
# Referential staleness
# ---------------------------------------------------------------------------


class Drift(str, enum.Enum):
    """Whether a topic still describes the code it claims to describe."""

    FRESH = "fresh"
    DRIFTED = "drifted"
    ORPHANED = "orphaned"
    UNBOUND = "unbound"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class DriftReading:
    """One topic's staleness verdict, with the evidence that produced it."""

    drift: Drift
    #: Newest commit epoch touching the topic file itself, or None when the
    #: scan window never saw it.
    topic_ct: Optional[int]
    #: Newest commit epoch across every module the topic declares.
    subject_ct: Optional[int]
    #: The declared module whose change is newest — the reason for a DRIFTED
    #: verdict, named so the operator does not have to guess which one moved.
    newest_subject: str
    #: Declared modules that no longer exist in the tree.
    missing: Tuple[str, ...]

    @property
    def rank_multiplier(self) -> float:
        """The factor a ranker should apply. UNKNOWN is never penalised.

        An unmeasured topic ranks exactly as it would have before staleness
        existed. Penalising on absence of evidence would quietly bury every
        topic older than the scan window, which is a rewrite of the corpus
        disguised as a heuristic.
        """
        if self.drift is Drift.DRIFTED:
            return staleness_penalty()
        return 1.0

    def describe(self) -> str:
        """A short human phrase. NEVER raises."""
        if self.drift is Drift.DRIFTED and self.newest_subject:
            return f"drifted (subject {self.newest_subject} moved since)"
        if self.drift is Drift.ORPHANED and self.missing:
            return f"orphaned ({len(self.missing)} declared module(s) gone)"
        return self.drift.value


_UNKNOWN_READING = DriftReading(
    drift=Drift.UNKNOWN, topic_ct=None, subject_ct=None,
    newest_subject="", missing=(),
)


def _last_touch_map(root: Path) -> Optional[Dict[str, int]]:
    """``repo-relative path -> newest commit epoch``, in ONE git invocation.

    The naive shape of this is ``git log -1`` per file, which for 383 topics
    plus the ~1,000 modules they declare is ~1,400 process spawns to compute
    a ranking hint. This walks history once, newest-first, and keeps the
    first sighting of each path — the same answer, one subprocess.

    Returns None when git cannot answer, which the caller must render as
    UNKNOWN rather than as an empty map (an empty map would mark every topic
    orphaned).
    """
    out = _run_git(
        ["log", f"-n{_scan_commits()}", "--name-only", "--no-merges",
         "--format=%x01%ct", "--", "."],
        root,
    )
    if out is None:
        return None

    touched: Dict[str, int] = {}
    current = 0
    for line in out.split("\n"):
        if line.startswith("\x01"):
            try:
                current = int(line[1:].strip() or 0)
            except ValueError:
                current = 0
            continue
        name = line.strip()
        # First sighting wins: history is newest-first, so the first time a
        # path appears is the last time it changed.
        if name and current and name not in touched:
            touched[name] = current
    return touched


#: ``root -> (head_sha, map)``. Keyed on HEAD because that is exactly what
#: invalidates the answer: a corpus whose repo has not moved cannot have
#: drifted since we last looked, however long ago that was.
_touch_cache: Dict[str, Tuple[str, Optional[Dict[str, int]]]] = {}


def _touch_disk_cache(root: Path) -> Path:
    return root / ".jarvis" / "memory_touch_map.json"


def _touch_load_disk(root: Path, head: str) -> Optional[Dict[str, int]]:
    """The persisted map IFF it was built at *head*. NEVER raises.

    The in-process memo makes the second call in a session free; this makes
    the FIRST call in a session free, which is the one that lands on a real
    op's CONTEXT_EXPANSION. A daemon that restarts hourly would otherwise pay
    the 2.4s history walk every time for an answer that cannot have changed.

    Keyed on HEAD, so the cache is not "recent" — it is exactly correct or
    it is discarded. A TTL would have to guess how fast the repo moves.
    """
    try:
        import json
        path = _touch_disk_cache(root)
        if not head or not path.is_file():
            return None
        with open(path, "r", encoding="utf-8") as handle:
            blob = json.load(handle)
        if not isinstance(blob, dict) or blob.get("head") != head:
            return None
        raw = blob.get("touched")
        if not isinstance(raw, dict):
            return None
        return {str(k): int(v) for k, v in raw.items()}
    except Exception:  # noqa: BLE001
        logger.debug("[MemoryCorpus] touch cache read degraded", exc_info=True)
        return None


def _touch_store_disk(root: Path, head: str, touched: Dict[str, int]) -> None:
    """Persist *touched* under *head*, atomically. NEVER raises."""
    try:
        import json
        if not head or not touched:
            return
        path = _touch_disk_cache(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"schema": MEMORY_CORPUS_SCHEMA_VERSION,
                       "head": head, "touched": touched}, handle)
        # Atomic rename: a reader must never observe a half-written map and
        # conclude the repo has no history.
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        logger.debug("[MemoryCorpus] touch cache write degraded", exc_info=True)


#: Stands in for HEAD when git cannot name one — a repo with no commits, or
#: no git at all. Without it, an empty sha never matches the cached key and
#: every single call re-walks history, which is the opposite of a memo.
_NO_HEAD = "\x00no-head"


def _touch_map_cached(root: Path) -> Optional[Dict[str, int]]:
    """:func:`_last_touch_map` memoised on HEAD, in-process and on disk.

    NEVER raises.
    """
    try:
        head = (_run_git(["rev-parse", "HEAD"], root) or "").strip() or _NO_HEAD
        with _cache_lock:
            hit = _touch_cache.get(str(root))
        if hit is not None and hit[0] == head:
            return hit[1]

        if head != _NO_HEAD:
            persisted = _touch_load_disk(root, head)
            if persisted is not None:
                with _cache_lock:
                    _touch_cache[str(root)] = (head, persisted)
                return persisted

        fresh = _last_touch_map(root)
        with _cache_lock:
            _touch_cache[str(root)] = (head, fresh)
        if fresh and head != _NO_HEAD:
            _touch_store_disk(root, head, fresh)
        return fresh
    except Exception:  # noqa: BLE001
        logger.debug("[MemoryCorpus] touch map degraded", exc_info=True)
        return None


def _resolve_declared(root: Path, declared: str) -> Optional[str]:
    """A ``modules:`` entry resolved to a repo-relative path, or None.

    Entries are written both fully-qualified
    (``backend/core/ouroboros/governance/orchestrator.py``) and bare
    (``orchestrator.py``) — the frontmatter in this repo mixes both inside a
    single list. A bare name is resolved only when it is unambiguous; a name
    matching several files is left unresolved rather than guessed, because a
    wrong resolution produces a confident staleness verdict about the wrong
    module.
    """
    try:
        cand = str(declared or "").strip().strip("'\"")
        if not cand:
            return None
        direct = root / cand
        if direct.is_file():
            return Path(cand).as_posix()
        if "/" in cand:
            return None  # qualified and absent — the caller reads that as missing
        matches = _bare_name_index(root).get(cand, ())
        return matches[0] if len(matches) == 1 else None
    except Exception:  # noqa: BLE001
        return None


_name_index_cache: Dict[str, Dict[str, Tuple[str, ...]]] = {}


def _bare_name_index(root: Path) -> Dict[str, Tuple[str, ...]]:
    """``basename -> tracked paths``. Built once per root from git. NEVER raises."""
    with _cache_lock:
        hit = _name_index_cache.get(str(root))
    if hit is not None:
        return hit
    index: Dict[str, List[str]] = {}
    out = _run_git(["ls-files", "-z", "--", "*.py"], root)
    if out:
        for name in out.split("\0"):
            if name:
                index.setdefault(Path(name).name, []).append(name)
    frozen = {k: tuple(v) for k, v in index.items()}
    with _cache_lock:
        _name_index_cache[str(root)] = frozen
    return frozen


def drift_for(
    root: Path,
    topic_rel: str,
    declared_modules: Sequence[str],
    touch: Optional[Dict[str, int]],
) -> DriftReading:
    """One topic's :class:`DriftReading`. Pure given *touch*. NEVER raises.

    Split from the batch entry point so a caller holding a warm touch-map can
    grade one topic without re-walking history, and so the decision logic is
    directly testable against a hand-built map.
    """
    try:
        if not staleness_enabled() or touch is None:
            return _UNKNOWN_READING
        if not declared_modules:
            return DriftReading(Drift.UNBOUND, touch.get(topic_rel),
                                None, "", ())

        missing: List[str] = []
        subject_ct: Optional[int] = None
        newest_subject = ""
        for declared in declared_modules:
            resolved = _resolve_declared(root, declared)
            if resolved is None:
                missing.append(str(declared))
                continue
            ct = touch.get(resolved)
            if ct is not None and (subject_ct is None or ct > subject_ct):
                subject_ct, newest_subject = ct, resolved

        topic_ct = touch.get(topic_rel)

        # Orphaned outranks drifted: a topic pointing at modules that no
        # longer exist is describing a shape of the codebase that is gone,
        # which is a stronger statement than "one of its subjects moved".
        if missing and len(missing) == len(declared_modules):
            return DriftReading(Drift.ORPHANED, topic_ct, subject_ct,
                                newest_subject, tuple(missing))

        if topic_ct is None and subject_ct is None:
            # Neither side reached the scan window. Undecidable, and saying
            # so is the whole point of the enum having five members.
            return DriftReading(Drift.UNKNOWN, None, None, "", tuple(missing))
        if subject_ct is None:
            # Topic inside the window, subject outside it: the subject has
            # not changed in at least that many commits. Fresh, decidably.
            return DriftReading(Drift.FRESH, topic_ct, None, "", tuple(missing))
        if topic_ct is None:
            # Subject inside the window, topic outside: the code moved more
            # recently than the note about it, by strictly more than the
            # window. Drifted, decidably — this is the case the bounded scan
            # was designed to keep answerable.
            return DriftReading(Drift.DRIFTED, None, subject_ct,
                                newest_subject, tuple(missing))

        if subject_ct > topic_ct:
            return DriftReading(Drift.DRIFTED, topic_ct, subject_ct,
                                newest_subject, tuple(missing))
        return DriftReading(Drift.FRESH, topic_ct, subject_ct,
                            newest_subject, tuple(missing))
    except Exception:  # noqa: BLE001
        logger.debug("[MemoryCorpus] drift grading degraded", exc_info=True)
        return _UNKNOWN_READING


def drift_readings_sync(
    project_root: Path,
    topics: Sequence[Tuple[str, Sequence[str]]],
) -> Dict[str, DriftReading]:
    """``{topic_rel: DriftReading}`` for a batch. Blocking. NEVER raises.

    Takes ``(topic_rel, declared_modules)`` pairs rather than file paths so
    the caller — which has already parsed frontmatter — does not pay to have
    it parsed twice.
    """
    try:
        root = Path(project_root)
        if not staleness_enabled():
            return {rel: _UNKNOWN_READING for rel, _ in topics}
        touch = _touch_map_cached(root)
        return {rel: drift_for(root, rel, mods, touch) for rel, mods in topics}
    except Exception:  # noqa: BLE001
        logger.debug("[MemoryCorpus] batch drift degraded", exc_info=True)
        return {rel: _UNKNOWN_READING for rel, _ in topics}


def read_topic_text(path: Path) -> str:
    """A topic body, size-capped. NEVER raises.

    The cap is not paranoia about disk: an oversized file reaching the
    summariser costs prompt budget in the one place the corpus is supposed to
    be saving it.
    """
    try:
        limit = _max_topic_bytes()
        with open(path, "rb") as handle:
            raw = handle.read(limit + 1)
        if len(raw) > limit:
            raw = raw[:limit]
        return raw.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""


def reset_caches_for_tests() -> None:
    """Drop every memo. Test-only."""
    with _cache_lock:
        _listing_cache.clear()
        _touch_cache.clear()
        _name_index_cache.clear()
