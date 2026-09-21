"""Repository state — a fingerprint of the worktree, and the indexes it keys.

## What this exists for

Soak bt-2026-09-20-183259: Sentinel discovery cost 750 s across 12 passes —
more than all 122 model generations (597 s) — re-deriving the same eight
candidates from a repository that had not changed. Profiled, the cause was not
"discovery is expensive". It was one line in ``collect_anchor_sources``:

    for p in root.rglob(stem + ".py"):

a FULL repository walk, per test target, per goal, per pass — 205 walks and
1,046,115 path-selector calls for 49 goals, descending into ``.worktrees/``
(whole copies of the repository) and filtering only afterwards. 2.5 s alone;
55 s when a background test census was walking the same disk.

Two fixes, and the order matters. A cache laid over that walk would have hidden
an O(goals x tree) algorithm behind a hit rate. So:

1. :class:`ModuleIndex` — ONE pruned walk per tree state, then every
   "where is ``<stem>.py``" is a dictionary lookup.
2. :func:`worktree_fingerprint` — so that work which is a PURE FUNCTION OF THE
   TREE is not redone while the tree stands still.

## The fingerprint is git's, not ours

Git already is the Merkle tree. ``HEAD^{tree}`` is a content hash of every
tracked file (16 ms here); ``git status --porcelain`` is the delta from it
(39 ms), answered from git's own stat cache. A hand-rolled Merkle walk would
re-read the tree to rediscover what the index already knows. The composite is

    SHA-256( tree-hash || for each RELEVANT dirty path: status, path, size, mtime_ns )

Porcelain alone says a file is modified, not HOW — a second edit to an
already-dirty file leaves it byte-identical — so each dirty path contributes its
stat. "Relevant" is a predicate, because the worktree also holds ledgers and
traces that churn every second and that no tree-pure computation reads;
fingerprinting them would make the cache key change on every pass and turn the
whole thing into an expensive no-op.

## What may be cached against it — and what must never be

Tree-pure results only: the module index, covering-test stems, AST-derived
import verdicts (together with :func:`environment_fingerprint`, because a
verdict also depends on what is installed, and a package can be installed while
the daemon runs). NEVER the candidate list itself: selection depends on
cooldowns, on what has landed and on the clock, and a cached candidate would
re-dispatch the goal that just failed.

An empty fingerprint means "unknown" (not a git checkout, git unavailable, a
timeout). Unknown is never equal to unknown: callers recompute. NEVER raises.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import logging
import os
import subprocess
import sysconfig
import threading
from pathlib import Path
from typing import Callable, Dict, Iterator, Optional, Tuple

logger = logging.getLogger(__name__)

_ENV_GIT_TIMEOUT_S = "JARVIS_REPO_STATE_GIT_TIMEOUT_S"
_DEFAULT_GIT_TIMEOUT_S = 20.0

#: Suffixes whose change can alter a tree-pure answer: Python sources, and the
#: manifests the import verdict reads (requirements, pytest/pyproject config).
_RELEVANT_SUFFIXES = (".py", ".pyi", ".txt", ".ini", ".toml", ".cfg")


def _git_timeout_s() -> float:
    try:
        value = float(os.environ.get(_ENV_GIT_TIMEOUT_S, "") or 0)
        return value if value > 0 else _DEFAULT_GIT_TIMEOUT_S
    except (TypeError, ValueError):
        return _DEFAULT_GIT_TIMEOUT_S


def default_relevance(path: str) -> bool:
    """Whether a change to *path* can alter a tree-pure discovery answer."""
    return path.endswith(_RELEVANT_SUFFIXES)


def _git(root: Path, *args: str) -> Optional[bytes]:
    try:
        done = subprocess.run(
            ["git", "-C", str(root), *args],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, timeout=_git_timeout_s(), check=False,
        )
        return done.stdout if done.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def worktree_fingerprint_sync(
    repo_root: Path, *, relevant: Callable[[str], bool] = default_relevance,
) -> str:
    """Composite SHA-256 of the worktree, or ``""`` when it cannot be known."""
    try:
        root = Path(repo_root)
        tree = _git(root, "rev-parse", "HEAD^{tree}")
        status = _git(
            root, "status", "--porcelain=v1", "-z", "--untracked-files=all",
        )
        if tree is None or status is None:
            return ""
        digest = hashlib.sha256()
        digest.update(tree.strip())
        entries = [e for e in status.split(b"\0") if e]
        index = 0
        while index < len(entries):
            entry = entries[index].decode("utf-8", errors="replace")
            code, path = entry[:2], entry[3:]
            # A rename/copy is followed by its ORIGIN path as the next record.
            index += 2 if code[:1] in ("R", "C") else 1
            if not relevant(path):
                continue
            digest.update(b"\0" + code.encode() + b"\0" + path.encode("utf-8", "replace"))
            try:
                st = (root / path).stat()
                digest.update(f"\0{st.st_size}\0{st.st_mtime_ns}".encode())
            except OSError:
                digest.update(b"\0absent")
        return digest.hexdigest()
    except Exception:  # noqa: BLE001
        logger.debug("[RepoState] fingerprint degraded", exc_info=True)
        return ""


async def worktree_fingerprint(
    repo_root: Path, *, relevant: Callable[[str], bool] = default_relevance,
) -> str:
    """:func:`worktree_fingerprint_sync`, off the event loop."""
    return await asyncio.to_thread(
        worktree_fingerprint_sync, repo_root, relevant=relevant,
    )


def environment_fingerprint() -> str:
    """Changes when a distribution is installed or removed.

    ``pip``/``uv`` add and remove entries in ``site-packages``, which moves that
    directory's mtime; so does dropping a ``.pth`` file. A stat, not a walk of
    ``importlib.metadata`` — this is asked every pass.
    """
    try:
        digest = hashlib.sha256()
        paths = sysconfig.get_paths()
        for key in ("purelib", "platlib"):
            location = paths.get(key) or ""
            try:
                digest.update(f"{location}\0{os.stat(location).st_mtime_ns}\0".encode())
            except OSError:
                digest.update(f"{location}\0absent\0".encode())
        return digest.hexdigest()
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# Pinning — one fingerprint for the duration of a pass
# ---------------------------------------------------------------------------

_PINNED: "contextvars.ContextVar[Optional[Tuple[str, str]]]" = contextvars.ContextVar(
    "repo_state_pinned", default=None,
)


@contextlib.contextmanager
def pinned(repo_root: Path, fingerprint: str) -> Iterator[None]:
    """Hold *fingerprint* as THE state of *repo_root* for this context.

    A pass asks "where is this module" once per goal. Re-deriving the
    fingerprint each time would cost ~55 ms x goals; a TTL would be a guess
    about how long a tree stays still. A pass instead takes ONE reading and
    works against it — which is also the only self-consistent thing to do, as
    every answer in the pass then describes the same tree. ``asyncio.to_thread``
    copies the context, so the pin follows the work off the loop.
    """
    token = _PINNED.set((str(Path(repo_root).resolve()), fingerprint))
    try:
        yield
    finally:
        _PINNED.reset(token)


def current_fingerprint(repo_root: Path) -> str:
    """The pinned fingerprint for *repo_root*, else a fresh reading."""
    held = _PINNED.get()
    if held is not None and held[0] == str(Path(repo_root).resolve()):
        return held[1]
    return worktree_fingerprint_sync(repo_root)


# ---------------------------------------------------------------------------
# Module index — one walk per tree state
# ---------------------------------------------------------------------------


def _prunable(name: str) -> bool:
    try:
        from backend.core.ouroboros.governance.environment_integrity import (  # noqa: PLC0415
            _prunable as _shared,
        )
        return _shared(name)
    except Exception:  # noqa: BLE001
        return name.startswith(".")


class ModuleIndex:
    """``filename -> paths`` for every Python file under a root, from ONE walk.

    Pruning happens DURING the walk (``dirnames[:] = ...``), so ``.worktrees``,
    ``.git``, virtualenvs and ``node_modules`` are never descended into — the
    ``rglob`` this replaces visited all of them and discarded the hits after.
    """

    def __init__(self, repo_root: Path) -> None:
        self.root = Path(repo_root)
        table: Dict[str, list] = {}
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if not _prunable(d) and d != "__pycache__"]
            for name in filenames:
                if name.endswith(".py"):
                    table.setdefault(name, []).append(Path(dirpath) / name)
        self._table: Dict[str, Tuple[Path, ...]] = {
            name: tuple(sorted(paths, key=lambda p: (len(str(p)), str(p))))
            for name, paths in table.items()
        }

    def find(self, filename: str) -> Tuple[Path, ...]:
        """Every file called *filename*, shortest path first."""
        return self._table.get(filename, ())

    def __len__(self) -> int:
        return len(self._table)


_index_lock = threading.Lock()
_index_held: Optional[Tuple[str, str, ModuleIndex]] = None


def module_index(repo_root: Path) -> ModuleIndex:
    """The index for *repo_root*'s current state; rebuilt only when it moves.

    With an UNKNOWN fingerprint nothing is retained: unknown is never equal to
    unknown, so each call pays for its own walk — still one walk, not 205.
    """
    global _index_held
    root = str(Path(repo_root).resolve())
    fingerprint = current_fingerprint(repo_root)
    if fingerprint:
        with _index_lock:
            held = _index_held
        if held is not None and held[0] == root and held[1] == fingerprint:
            return held[2]
    built = ModuleIndex(Path(repo_root))
    if fingerprint:
        with _index_lock:
            _index_held = (root, fingerprint, built)
    return built


def reset_for_tests() -> None:
    global _index_held
    with _index_lock:
        _index_held = None


__all__ = [
    "ModuleIndex",
    "current_fingerprint",
    "default_relevance",
    "environment_fingerprint",
    "module_index",
    "pinned",
    "reset_for_tests",
    "worktree_fingerprint",
    "worktree_fingerprint_sync",
]
