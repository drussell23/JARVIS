"""
bytecode_hygiene - remove only the bytecode the import system cannot check.

A running kernel must not delete its own ``__pycache__``. CPython already
guarantees fresh code from it: every ``.pyc`` there records its source's
mtime and size and is recompiled on import when they no longer match, and a
``__pycache__`` entry is never imported without its ``.py``. Wiping those
directories bought nothing, cost a recompile of the whole tree, and raced the
live interpreter writing new entries -- ``[WinError 145] The directory is not
empty`` on ``backend/core/__pycache__`` (boot 2026-10-15) was that race.

What CAN serve stale code:
  * a legacy ``foo.pyc`` beside where ``foo.py`` would live (not in
    ``__pycache__``) -- imported *sourcelessly* once ``foo.py`` is deleted or
    renamed, i.e. a module that no longer exists keeps importing. CPython 3
    never writes these, so all are removed.
What is merely dead weight:
  * a ``__pycache__/foo.<tag>.pyc`` whose ``foo.py`` is gone -- unreachable
    by import, removed as an orphan.

Live ``__pycache__`` entries (source present) are never touched, and a file
another interpreter holds open is skipped, not forced (the next run retries).
The one answer for every cleaner: process_cleanup_manager's code-change
cleanup and the kernel's boot-time cache hygiene both call it. Blocking (a
directory walk): call it off the event loop.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)

_SOURCE_SUFFIXES = (".py", ".pyw")


@dataclass
class PruneResult:
    removed: int = 0
    bytes_freed: int = 0
    skipped: List[str] = field(default_factory=list)  # in use / not ours to force


def _unlink(path: Path, result: PruneResult) -> None:
    try:
        size = path.stat().st_size
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        # Another interpreter may hold it open on Windows; it is not ours to
        # force. Next run retries.
        logger.debug("Stale bytecode not removed (%s): %s", path, exc)
        result.skipped.append(str(path))
        return
    result.removed += 1
    result.bytes_freed += size
    logger.debug("Removed stale bytecode: %s", path)


def prune_unvalidatable_bytecode(root: Path) -> PruneResult:
    """Prune *root*'s bytecode the import system cannot validate. NEVER raises."""
    result = PruneResult()
    try:
        for current, dirs, files in os.walk(root):
            here = Path(current)
            if here.name == "__pycache__":
                source_dir = here.parent
                for name in files:
                    if not name.endswith(".pyc"):
                        continue
                    # "<module>.<cache_tag>[.opt-N].pyc" -- module names
                    # contain no dots, so the stem is the first segment.
                    stem = name.split(".", 1)[0]
                    if any((source_dir / f"{stem}{suffix}").exists() for suffix in _SOURCE_SUFFIXES):
                        continue
                    _unlink(here / name, result)
                dirs[:] = []
                continue
            for name in files:
                if name.endswith(".pyc"):
                    _unlink(here / name, result)
    except Exception as exc:  # noqa: BLE001
        logger.error("Error during stale bytecode prune of %s: %s", root, exc)
    return result


__all__ = ["PruneResult", "prune_unvalidatable_bytecode"]
