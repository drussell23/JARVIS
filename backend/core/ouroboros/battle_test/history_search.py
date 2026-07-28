"""Ctrl+R history search for the cockpit — rendered by the palette it
already has.

The bipartite cockpit cannot use prompt_toolkit's reverse-i-search: that
machinery belongs to ``PromptSession``'s search toolbar, and the cockpit
is a hand-built Application. Rebuilding readline's UI there would mean a
second overlay system fighting the palette Float for the same rows.

So history search IS a completion source. Ctrl+R arms a controller and
opens the completion menu; a gated completer feeds it history entries
(most recent first, substring-filtered by whatever is typed); the
page-style palette renders them exactly like verbs; typing refines the
match live because prompt_toolkit re-runs completers while a menu is
open; Enter accepts into the buffer; Esc closes. Ctrl+R again cycles to
the next-older match — readline muscle memory intact. One surface, one
renderer, zero new widgets.

The controller disarms itself the moment the menu closes (via the
buffer's own ``on_completions_changed`` event), so ordinary typing never
sees a history candidate.

Env: ``JARVIS_HISTORY_SEARCH_ENABLED`` (default true),
``JARVIS_HISTORY_SEARCH_MAX`` (menu cap, default 32).

NEVER raises into the key-dispatch or completion paths.
"""
from __future__ import annotations

import logging
import os
from typing import Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

HISTORY_SEARCH_SCHEMA_VERSION: str = "history_search.1"

MASTER_FLAG_ENV_VAR: str = "JARVIS_HISTORY_SEARCH_ENABLED"
MAX_RESULTS_ENV_VAR: str = "JARVIS_HISTORY_SEARCH_MAX"

#: The action id — remappable via keybindings.json like everything else.
HISTORY_SEARCH_ACTION: str = "history:search"
HISTORY_SEARCH_DEFAULT_KEYS: Tuple[str, ...] = ("ctrl+r",)


def is_history_search_enabled() -> bool:
    """Master flag — default true. NEVER raises."""
    raw = os.environ.get(MASTER_FLAG_ENV_VAR, "true")
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _max_results() -> int:
    try:
        return max(1, int(os.environ.get(MAX_RESULTS_ENV_VAR, "32")))
    except (TypeError, ValueError):
        return 32


class HistorySearchController:
    """The armed/disarmed latch between the keystroke and the completer.

    Armed by the Ctrl+R handler, disarmed automatically when the
    completion menu closes — subscribed to the buffer's OWN
    ``on_completions_changed`` event rather than to any key, so every
    way a menu can die (Esc, accept, text cleared, focus lost) disarms
    it without this module having to enumerate them."""

    def __init__(self) -> None:
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    def arm(self) -> None:
        self._active = True

    def disarm(self) -> None:
        self._active = False

    def watch(self, buffer: Any) -> None:
        """Subscribe the auto-disarm to *buffer*. NEVER raises."""
        try:
            def _on_change(buf: Any) -> None:
                try:
                    if buf.complete_state is None:
                        self._active = False
                except Exception:  # noqa: BLE001
                    self._active = False

            buffer.on_completions_changed += _on_change
        except Exception:  # noqa: BLE001
            logger.debug("[HistorySearch] watch degraded", exc_info=True)


def _history_strings(history: Any) -> List[str]:
    """All history entries, oldest→newest. Prefers the in-memory cache;
    falls back to the storage read for a history nothing has loaded
    yet. NEVER raises."""
    try:
        strings = list(history.get_strings())
        if strings:
            return strings
    except Exception:  # noqa: BLE001
        pass
    try:
        # Storage yields newest-first; normalize to oldest→newest.
        return list(reversed(list(history.load_history_strings())))
    except Exception:  # noqa: BLE001
        return []


def _completer_base() -> Any:
    """prompt_toolkit's `Completer`, or `object` if it cannot be imported.

    Resolved at class-creation time rather than imported at module scope so
    this file stays importable without prompt_toolkit — the history ranking
    below is pure logic and is tested without a terminal.
    """
    try:
        from prompt_toolkit.completion import Completer
        return Completer
    except Exception:  # noqa: BLE001
        return object


class HistoryCompleter(_completer_base()):  # type: ignore[misc]
    """Feeds history entries into the completion menu while armed.

    INHERITS `Completer`, and that is load-bearing rather than tidy. A
    completer is not consumed through `get_completions`: prompt_toolkit's
    async completion path calls `get_completions_async`, which the base class
    supplies by wrapping the sync method. A duck-typed class implementing
    only `get_completions` therefore satisfies every static reading of the
    protocol and raises `AttributeError` on the first keystroke —

        Exception 'HistoryCompleter' object has no attribute
        'get_completions_async'

    repeated per keypress, because the failure is inside a coroutine the
    event loop restarts. Tests that call `get_completions` directly cannot
    see it; only prompt_toolkit calling it the way prompt_toolkit does.

    Ranking: substring hits, most recent first; entries whose START
    matches the typed text rank above mere containment. Duplicates
    collapse to their most recent occurrence. A multi-line entry
    displays as its first line + ``…`` but ACCEPTS as the full text —
    the continuation rules already know how to hold it."""

    def __init__(
        self,
        controller: HistorySearchController,
        history: Any,
        *,
        max_results: Optional[int] = None,
    ) -> None:
        self._controller = controller
        self._history = history
        self._max = max_results

    def get_completions(self, document: Any, _event: Any = None):
        if not self._controller.active:
            return
        try:
            from prompt_toolkit.completion import Completion
        except ImportError:
            return
        try:
            needle = (document.text_before_cursor or "").lower()
            cap = self._max if self._max is not None else _max_results()
            seen: set = set()
            ranked: List[Tuple[int, str]] = []
            # newest first — the whole point of a history search
            for entry in reversed(_history_strings(self._history)):
                if not entry or entry in seen:
                    continue
                seen.add(entry)
                low = entry.lower()
                if needle and needle not in low:
                    continue
                rank = 0 if (not needle or low.startswith(needle)) else 1
                ranked.append((rank, entry))
                if len(ranked) >= cap * 2:
                    break
            ranked.sort(key=lambda pair: pair[0])
            replace = -len(document.text_before_cursor or "")
            for _, entry in ranked[:cap]:
                first_line = entry.splitlines()[0] if entry else entry
                display = (
                    first_line + " …" if "\n" in entry else first_line
                )
                yield Completion(
                    text=entry,
                    start_position=replace,
                    display=display,
                    display_meta="history",
                )
        except Exception:  # noqa: BLE001 — never break typing
            logger.debug("[HistorySearch] completion degraded",
                         exc_info=True)
            return


def build_history_search(
    history: Any,
) -> Tuple[Optional[HistorySearchController], Optional[HistoryCompleter]]:
    """Controller + completer pair, or ``(None, None)`` when disabled /
    no history to search. NEVER raises."""
    try:
        if history is None or not is_history_search_enabled():
            return None, None
        controller = HistorySearchController()
        return controller, HistoryCompleter(controller, history)
    except Exception:  # noqa: BLE001
        return None, None


def install_history_search(
    kb: Any,
    controller: HistorySearchController,
    *,
    context: str = "Chat",
) -> bool:
    """Bind the remappable ``history:search`` action into *kb*.

    First press arms + opens the menu; further presses cycle to the
    next-older match (readline muscle memory). Operates on
    ``event.current_buffer`` so one binding serves any focused prompt.
    NEVER raises; returns True when bound."""
    try:
        def _search(event: Any) -> None:
            try:
                buf = event.current_buffer
                if controller.active and buf.complete_state is not None:
                    buf.complete_next()
                    return
                controller.arm()
                buf.start_completion(select_first=False)
            except Exception:  # noqa: BLE001
                controller.disarm()

        from backend.core.ouroboros.battle_test.keymap import bind_action
        return bind_action(
            kb, HISTORY_SEARCH_ACTION, HISTORY_SEARCH_DEFAULT_KEYS,
            _search, context=context,
            description="search prompt history (again: next older match)",
        )
    except Exception:  # noqa: BLE001
        logger.debug("[HistorySearch] install degraded", exc_info=True)
        return False


def merge_history_completer(
    base_completer: Any,
    history_completer: Optional[HistoryCompleter],
) -> Any:
    """Compose the gated history source with a surface's completer.
    Either side may be None. NEVER raises — falls back to whichever
    half exists."""
    if history_completer is None:
        return base_completer
    if base_completer is None:
        return history_completer
    try:
        from prompt_toolkit.completion import merge_completers
        # History first: while ARMED it owns the menu; while disarmed it
        # yields nothing and the verb palette behaves as before.
        return merge_completers([history_completer, base_completer])
    except Exception:  # noqa: BLE001
        return base_completer


__all__ = [
    "HISTORY_SEARCH_ACTION",
    "HISTORY_SEARCH_DEFAULT_KEYS",
    "HISTORY_SEARCH_SCHEMA_VERSION",
    "HistoryCompleter",
    "HistorySearchController",
    "MASTER_FLAG_ENV_VAR",
    "MAX_RESULTS_ENV_VAR",
    "build_history_search",
    "install_history_search",
    "is_history_search_enabled",
    "merge_history_completer",
]
