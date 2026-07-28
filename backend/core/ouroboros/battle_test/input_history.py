"""What you typed last time, on both surfaces, from one file.

`ov` had no command history. Not "no search UI" — no history at all: the
`PromptSession` was built without a `history=` argument and the cockpit's
`TextArea` got the bare `InMemoryHistory` prompt_toolkit hands every widget,
which starts empty and dies with the process. Pressing Up recalled nothing.

That matters more than the shortcuts around it, because prompt_toolkit
already ships every readline binding the audit went looking for — `Ctrl+A`,
`Ctrl+E`, `Ctrl+K`, `Ctrl+U`, `Ctrl+W`, `Ctrl+Y`, `Alt+B`, `Alt+F`, `Ctrl+_`
and `Ctrl+R` are all loaded and live. `Ctrl+R` was not missing; it was
searching an empty list. Adding history is what turns the whole cluster on.

One file, both surfaces
-----------------------
The cockpit and the plain client are different widgets, and this codebase has
found the same class of bug in that split more than once — a feature wired to
one surface while the operator types into the other. A goal typed in the
cockpit must be recallable from the fallback and the reverse, so both read
and write ONE `FileHistory`.

Up is not always history
------------------------
The prompt is multi-line now. `Up` inside a paragraph has to move the cursor,
or editing a pasted block is impossible — history is only the right meaning
on the FIRST line. `Buffer.auto_up`/`auto_down` encode exactly that rule, so
they are bound rather than `history_backward`, which would yank a
half-composed paragraph away mid-edit.

What is not stored
------------------
Blank lines, and anything identical to the entry before it — a history full
of one repeated command is one you stop pressing Up on.

The file is the operator's typing, so it is created with owner-only
permissions. Nothing is filtered beyond that: a heuristic that silently
dropped some commands would leave the operator unable to trust that Up shows
what they actually did, which is worse than a file they can delete.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("Ouroboros.InputHistory")

__all__ = ["history_enabled", "history_path", "shared_history",
           "install_history_bindings"]

#: Entries kept. Deep enough that yesterday's goal is still reachable,
#: bounded so the file cannot grow without limit across a long-lived install.
_MAX_ENTRIES = 2000


def history_enabled() -> bool:
    """Default ON. Off, each session starts blank as it did before."""
    return os.environ.get(
        "JARVIS_INPUT_HISTORY_ENABLED", "1",
    ).strip().lower() not in ("0", "false", "no", "off")


def history_path() -> Path:
    """Where the operator's typing lives.

    Under `.jarvis/` beside the other durable state this organism keeps, so
    it is discoverable and deletable in the place an operator already looks.
    """
    override = os.environ.get("JARVIS_INPUT_HISTORY_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    root = os.environ.get("JARVIS_REPO_PATH", "").strip() or "."
    return Path(root).expanduser() / ".jarvis" / "input_history"


def _trim(path: Path, max_entries: int = _MAX_ENTRIES) -> None:
    """Keep the tail. Rewritten atomically so a crash mid-trim cannot leave
    the operator with a truncated or empty history."""
    try:
        if not path.exists():
            return
        lines = path.read_text(errors="replace").splitlines(keepends=True)
        # FileHistory writes a `# timestamp` line then `+`-prefixed content;
        # counting ENTRIES means counting the comment lines that start them.
        starts = [i for i, ln in enumerate(lines) if ln.startswith("#")]
        if len(starts) <= max_entries:
            return
        cut = starts[len(starts) - max_entries]
        tmp = path.with_suffix(".tmp")
        tmp.write_text("".join(lines[cut:]))
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001 — a trim failure must not cost history
        logger.debug("[InputHistory] trim degraded", exc_info=True)


_HISTORY: Any = None


def shared_history() -> Any:
    """The ONE history both surfaces read and write. None when unavailable.

    A module singleton because the two surfaces are constructed in different
    places and neither owns the other; handing each its own `FileHistory` for
    the same path would give two write-behind caches racing on one file.
    """
    global _HISTORY
    if not history_enabled():
        return None
    if _HISTORY is not None:
        return _HISTORY
    try:
        from prompt_toolkit.history import FileHistory

        path = history_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _trim(path)
        if not path.exists():
            path.touch(mode=0o600)
        else:
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        _HISTORY = _DedupedFileHistory(str(path))
        return _HISTORY
    except Exception:  # noqa: BLE001 — a cockpit without history still runs
        logger.debug("[InputHistory] unavailable", exc_info=True)
        return None


def _build_deduped_base() -> Any:
    from prompt_toolkit.history import FileHistory
    return FileHistory


class _DedupedFileHistory:  # pragma: no cover — thin delegation
    """`FileHistory` that refuses blanks and immediate repeats.

    Composition rather than a subclass so the concrete class is resolved at
    construction: importing prompt_toolkit at module scope would make this
    module unimportable in an environment without it, and history is the
    first thing a headless caller does not need.
    """

    def __init__(self, path: str) -> None:
        self._inner = _build_deduped_base()(path)

    def append_string(self, text: str) -> None:
        try:
            candidate = str(text or "")
            if not candidate.strip():
                return
            try:
                last = next(reversed(list(self._inner.get_strings())), None)
            except Exception:  # noqa: BLE001
                last = None
            if candidate == last:
                # A history full of one repeated command is one the operator
                # stops pressing Up on.
                return
            self._inner.append_string(candidate)
        except Exception:  # noqa: BLE001
            logger.debug("[InputHistory] append degraded", exc_info=True)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def install_history_bindings(kb: Any, get_buffer: Any) -> bool:
    """Bind Up/Down to history-aware navigation. NEVER raises.

    `auto_up`/`auto_down`, not `history_backward`/`history_forward`: the
    prompt is multi-line, so `Up` inside a paragraph must move the cursor and
    recall history only from the first line. Binding the raw history verbs
    would yank a half-composed paragraph away mid-edit.
    """
    try:
        if kb is None or not history_enabled():
            return False

        def _nav(up: bool) -> Any:
            def _handler(event: Any) -> None:
                try:
                    buf = get_buffer()
                    if buf is None:
                        return
                    count = getattr(event, "arg", 1) or 1
                    if up:
                        buf.auto_up(count=count)
                    else:
                        buf.auto_down(count=count)
                except Exception:  # noqa: BLE001
                    pass
            return _handler

        kb.add("up")(_nav(True))
        kb.add("down")(_nav(False))
        return True
    except Exception:  # noqa: BLE001
        logger.debug("[InputHistory] bindings degraded", exc_info=True)
        return False


def reset_for_tests() -> None:
    global _HISTORY
    _HISTORY = None
