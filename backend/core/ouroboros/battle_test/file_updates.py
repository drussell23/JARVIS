"""File updates in the transcript: one rule for every renderer.

An op's product is the change it makes to files. Two per-op renderers exist
(``ClaudeStyleTransport``, the default, and ``SerpentTransport`` behind
``JARVIS_RENDER_MODE=serpent``), and each had its own idea of how a change
appears. The default one ignored the APPLY diff entirely, so on the
production cockpit a landing showed as ``✓ … · 0.0s`` and no code at all.

This module is the single answer, and both renderers ask it:

* :func:`display_path`: a file as the operator knows it, relative to the work
  tree it lives in. APPLY writes into an autonomous worktree under
  ``.worktrees/``, whose absolute path names a workspace nobody has heard of.
* :func:`split_unified_diff`: a patch, one section per file, so a multi-file
  preview never shows every file's changes under each file.
* :class:`FileUpdates`: per op, each file renders ONCE. The pipeline names a
  target before its change exists, then sends the diff, then names the files
  again at completion; only the diff renders, and a file whose diff never
  arrived gets one header at completion rather than vanishing.
* :func:`compose_update`: the block itself, composed by ``tool_render_view``
  as an ``edit_file`` result. That is the composer every tool block uses, so a
  diff here is Claude Code's format (gutter, syntax, add/remove bands, one
  ``/expand`` ref) and the same format as an edit made by a tool call.

NEVER raises.
"""
from __future__ import annotations

import os
import subprocess
import threading
from typing import Any, Dict, List, Optional, Set, Tuple


def display_path(file_path: str) -> str:
    """*file_path* relative to the nearest ancestor holding ``.git`` (a
    directory for a checkout, a FILE for a linked worktree). Asked of the
    filesystem, not of configuration, so it is right for the operator tree,
    the autonomous workspace and any L3 unit alike. Relative input is
    returned unchanged."""
    text = str(file_path or "")
    if not os.path.isabs(text):
        return text
    try:
        real = os.path.realpath(text)
        here = os.path.dirname(real)
        while True:
            if os.path.exists(os.path.join(here, ".git")):
                return os.path.relpath(real, here)
            parent = os.path.dirname(here)
            if parent == here:
                break
            here = parent
    except Exception:  # noqa: BLE001
        pass
    return text


def split_unified_diff(diff_text: str) -> List[Tuple[str, str]]:
    """A unified patch as ``(path, section)`` per file.

    A section starts at ``diff --git``, or at a ``--- `` header not already
    part of one. A header is ``--- `` IMMEDIATELY followed by ``+++ ``: a
    removed line whose text begins ``-- `` (a SQL comment) also reads
    ``--- …`` and must stay in its hunk. The path is the NEW side; a deletion
    (``+++ /dev/null``) is named by its old side. Text with no ``@@`` hunk is
    not a diff (a candidate's full content travels on the same preview field)
    and yields ``[]``.
    """
    try:
        text = str(diff_text or "")
        if "@@" not in text:
            return []
        lines_in = text.split("\n")
        sections: List[List[str]] = []
        current: Optional[List[str]] = None
        for i, line in enumerate(lines_in):
            header = (line.startswith("--- ") and i + 1 < len(lines_in)
                      and lines_in[i + 1].startswith("+++ "))
            # A `diff --git` section owns the `---/+++` pair that follows it.
            owned = bool(current) and current[0].startswith("diff --git ") \
                and not any(l.startswith("+++ ") for l in current)
            if line.startswith("diff --git ") or (header and not owned):
                current = [line]
                sections.append(current)
            elif current is not None:
                current.append(line)
        out: List[Tuple[str, str]] = []
        for lines in sections:
            old = new = ""
            for line in lines:
                if line.startswith("--- ") and not old:
                    old = line[4:].split("\t")[0].strip()
                elif line.startswith("+++ ") and not new:
                    new = line[4:].split("\t")[0].strip()
            path = new if new and new != "/dev/null" else old
            if path.startswith(("a/", "b/")):
                path = path[2:]
            body = "\n".join(lines)
            if path and "@@" in body:
                out.append((path, body))
        return out
    except Exception:  # noqa: BLE001
        return []


def git_diff(file_path: str, repo: str) -> str:
    """The working-tree diff for one file, including an untracked one (as
    all-additions). Bounded per call by ``JARVIS_FILE_UPDATE_GIT_TIMEOUT_S``.
    "" when there is nothing or it cannot be read."""
    try:
        timeout = max(0.1, float(os.environ.get("JARVIS_FILE_UPDATE_GIT_TIMEOUT_S", "") or 2.0))
    except ValueError:
        timeout = 2.0
    # READ-ONLY, and it must not take the index lock. Plain `git diff`
    # refreshes the index opportunistically, which writes `.git/index.lock`;
    # a call killed at the timeout mid-refresh left that lock behind and
    # blocked every later commit in the repository (AutoCommit and the
    # operator's own) until someone deleted it by hand.
    git = ["git", "--no-optional-locks"]
    candidates = [git + ["diff", "--", file_path],
                  git + ["diff", "--cached", "--", file_path]]
    try:
        tracked = subprocess.run(
            git + ["ls-files", "--error-unmatch", "--", file_path],
            cwd=repo, capture_output=True, text=True, timeout=timeout,
        ).returncode == 0
        if not tracked and os.path.isfile(os.path.join(repo, file_path)):
            # `--no-index` renders ANY existing file as all-additions, so it
            # is asked only for a file git does not track.
            candidates.append(git + ["diff", "--no-index", "--", os.devnull, file_path])
    except Exception:  # noqa: BLE001
        pass
    for args in candidates:
        try:
            out = subprocess.run(args, cwd=repo, capture_output=True,
                                 text=True, timeout=timeout).stdout.strip()
            if out:
                return out
        except Exception:  # noqa: BLE001
            continue
    return ""


def compose_update(path: str, diff_text: str, *, op_id: str = "",
                   palette: Any = None) -> Any:
    """One file's change as a composed tool block, or ``None``."""
    try:
        from backend.core.ouroboros.battle_test.tool_render_view import (
            compose_if_enabled, store_for_view,
        )
        return compose_if_enabled("edit_file", path, diff_text, op_id=op_id,
                                  palette=palette, store=store_for_view())
    except Exception:  # noqa: BLE001
        return None


class FileUpdates:
    """Which files an op has shown, and which are still waiting for a diff."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._shown: Dict[str, Set[str]] = {}
        self._pending: Dict[str, Set[str]] = {}

    def claim(self, op_id: str, file_path: str, diff_text: str) -> Optional[str]:
        """The display path to render *diff_text* under, or ``None`` when
        this op already showed that file, or there is no diff yet (the file
        is then remembered as pending)."""
        shown_path = display_path(file_path)
        with self._lock:
            if not diff_text:
                if shown_path not in self._shown.get(op_id, set()):
                    self._pending.setdefault(op_id, set()).add(shown_path)
                return None
            shown = self._shown.setdefault(op_id, set())
            if shown_path in shown:
                return None
            shown.add(shown_path)
            self._pending.get(op_id, set()).discard(shown_path)
            return shown_path

    def unresolved(self, op_id: str) -> List[str]:
        """Files announced for *op_id* whose diff never arrived, now counted
        as shown (the caller renders their headers)."""
        with self._lock:
            waiting = sorted(self._pending.pop(op_id, set()))
            self._shown.setdefault(op_id, set()).update(waiting)
            return waiting

    def changed(self, op_id: str) -> Set[str]:
        with self._lock:
            return set(self._shown.get(op_id, set())) | set(self._pending.get(op_id, set()))

    def close(self, op_id: str) -> None:
        with self._lock:
            self._shown.pop(op_id, None)
            self._pending.pop(op_id, None)


__all__ = ["FileUpdates", "compose_update", "display_path", "git_diff",
           "split_unified_diff"]
