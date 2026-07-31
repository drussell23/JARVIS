"""One key, two completion sources, and nobody deciding between them.

The prompt shows grey ghost text — ``wha`` offers ``what is O+V?`` — and Tab
does nothing. The suggestion is right there and the obvious key refuses it.

Why, precisely
--------------
``PromptSession`` is built with BOTH a ``completer`` and an ``auto_suggest``.
Those are two independent completion sources, and prompt_toolkit gives them
different keys: Tab drives the COMPLETER, while ``→`` / ``c-e`` accept the
SUGGESTION (``load_auto_suggest_bindings``). The verb completer only fires on
``/`` and the mention completer only on ``@``, so on prose like ``wha`` the
completer correctly yields nothing — and Tab, bound to the completer alone,
correctly does nothing.

Every piece behaved as designed. What was missing is the piece nobody wrote:
an arbiter that decides which source Tab means RIGHT NOW.

So the fix is not "bind Tab to accept the suggestion". That would trade one
broken half for the other — Tab would stop completing ``/mem`` into
``/memory``, which is the case it currently gets right. The fix is to make
Tab a DISPATCHER over a priority ladder, where each rung is a question with a
cheap, synchronous answer.

The ladder
----------
1. **A completion menu is open** → navigate it. Tab must never steal a
   keystroke from a menu the operator can see.
2. **The cursor is in completion territory** (``/verb`` / ``@path``) → run
   the completer. Contextual beats historical: if the operator is typing a
   verb, the verb list is the better answer even when history also matches.
3. **A suggestion exists and the cursor is at the very end** → accept it.
   This is the rung that was missing.
4. **The buffer is empty** → open the palette. Tab on a blank prompt is the
   cheapest discoverability affordance a CLI has.
5. **Nothing to offer** → indent, because in a multi-line prompt that is what
   Tab has always meant.

Why the ladder is not "ask the completer"
------------------------------------------
The obvious implementation — ask the completer for candidates, and fall
through to the suggestion when it returns none — cannot be written here. The
completer is a ``ThreadedCompleter``: asking it is asynchronous by
construction, and a key handler that waits on it would block the very
keystroke it is servicing. Priming the verb registry walks packages, which is
exactly why it was threaded in the first place.

So rung 2 decides by CONTEXT instead, and the context predicate
(``repl_completion.completion_would_trigger``) is the SAME function the
completers themselves branch on. Not a copy of their rule — the rule. Two
definitions of "is this completion territory" would drift the first time a
trigger character was added, and the symptom would be this exact bug again.

Remappable, not hardcoded
--------------------------
Every binding goes through ``keymap.bind_action``, so Tab is a DEFAULT rather
than a constant and ``.jarvis/keybindings.json`` can move it. The actions
appear in ``/keys`` like every other.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional, Tuple

logger = logging.getLogger("Ouroboros.CompletionArbiter")

COMPLETION_ARBITER_SCHEMA_VERSION: str = "completion_arbiter.1"

#: Action ids. Registered in the keymap catalog so `/keys` lists them and
#: `.jarvis/keybindings.json` can remap them.
ACTION_SMART_COMPLETE = "chat:smart_complete"
ACTION_ACCEPT_WORD = "chat:accept_suggestion_word"

__all__ = [
    "COMPLETION_ARBITER_SCHEMA_VERSION",
    "ACTION_ACCEPT_WORD",
    "ACTION_SMART_COMPLETE",
    "arbiter_enabled",
    "install_completion_arbiter",
    "resolve_tab_action",
]


def arbiter_enabled() -> bool:
    """``JARVIS_COMPLETION_ARBITER_ENABLED`` (default true).

    OFF restores prompt_toolkit's stock behaviour exactly: Tab drives the
    completer, ``→`` accepts the suggestion. The rollback is the old bug, so
    it exists for diagnosis rather than for use.
    """
    try:
        return os.environ.get(
            "JARVIS_COMPLETION_ARBITER_ENABLED", "1",
        ).strip().lower() not in ("0", "false", "no", "off", "")
    except Exception:  # noqa: BLE001
        return True


def _indent() -> str:
    """What Tab inserts when it has nothing to complete.

    ``JARVIS_COMPLETION_TAB_INDENT`` — spaces, because a literal tab in a
    goal that later reaches a prompt is invisible whitespace nobody can see
    or diff.
    """
    try:
        raw = os.environ.get("JARVIS_COMPLETION_TAB_INDENT", "").strip()
        if raw.isdigit():
            return " " * max(0, min(16, int(raw)))
    except Exception:  # noqa: BLE001
        pass
    return "  "


def _would_trigger_completion(text_before_cursor: str) -> bool:
    """Whether the completer owns this position. NEVER raises.

    Delegates to the completion module so the predicate has exactly one
    definition. Fail-CLOSED to False: if the rule cannot be consulted, the
    arbiter falls through to the suggestion, which is the behaviour the
    operator is asking for. Guessing True would restore the dead Tab.
    """
    try:
        from backend.core.ouroboros.battle_test.repl_completion import (
            completion_would_trigger,
        )
        return bool(completion_would_trigger(text_before_cursor))
    except Exception:  # noqa: BLE001
        logger.debug("[CompletionArbiter] trigger predicate unavailable",
                     exc_info=True)
        return False


def resolve_tab_action(
    *,
    menu_open: bool,
    text_before_cursor: str,
    has_suggestion: bool,
    cursor_at_end: bool,
) -> str:
    """The ladder, as a pure function. NEVER raises.

    Extracted from the key handler so the DECISION is testable without a
    terminal, a running application, or prompt_toolkit at all. The handler
    below is then only plumbing, and a regression in the priority order
    fails a unit test rather than an operator's muscle memory.

    Returns one of ``menu`` / ``complete`` / ``accept`` / ``indent``.
    """
    try:
        if menu_open:
            return "menu"
        text = str(text_before_cursor or "")
        if _would_trigger_completion(text):
            return "complete"
        # The suggestion is only real at the very end of the buffer:
        # prompt_toolkit renders it there and nowhere else, so accepting it
        # from mid-line would splice history into the middle of a sentence.
        if has_suggestion and cursor_at_end:
            return "accept"
        if not text.strip():
            return "complete"
        return "indent"
    except Exception:  # noqa: BLE001
        return "indent"


def _suggestion_text(buff: Any) -> str:
    """The ghost text Tab should accept, or ``""``. NEVER raises.

    Reads ``buffer.suggestion`` first, then — and this is the part that
    matters — falls back to asking the buffer's own ``AutoSuggest``
    synchronously.

    ``Buffer.suggestion`` is populated by an ASYNC task that runs after the
    text changes. A human typing at human speed always wins that race, sees
    the grey text, and presses Tab. A paste, a fast typist, or a busy event
    loop does not: Tab arrives while ``suggestion`` is still ``None``, the
    ladder concludes there is nothing to accept, and Tab inserts an indent —
    which then also corrupts the prefix the pending suggestion was about to
    match. A timing-dependent key is a key that works until it matters.

    ``AutoSuggestFromHistory.get_suggestion`` is a synchronous backward walk
    over history and is exactly what the async task would have called, so
    asking it directly is not extra work — it is the SAME work, done now
    instead of maybe-already. ``ThreadedAutoSuggest`` keeps the synchronous
    method and only adds an async variant, so the wrapper is transparent
    here.
    """
    try:
        suggestion = getattr(buff, "suggestion", None)
        text = str(getattr(suggestion, "text", "") or "")
        if text:
            return text
        auto = getattr(buff, "auto_suggest", None)
        if auto is None:
            return ""
        resolved = auto.get_suggestion(buff, buff.document)
        return str(getattr(resolved, "text", "") or "")
    except Exception:  # noqa: BLE001
        logger.debug("[CompletionArbiter] suggestion resolution degraded",
                     exc_info=True)
        return ""


def _first_word(text: str) -> str:
    """Leading whitespace plus the first word of *text*. Pure.

    Word-wise acceptance takes the separator WITH the word, so repeated
    presses walk forward one word at a time instead of stalling on the space
    between them.
    """
    if not text:
        return ""
    i = 0
    while i < len(text) and text[i].isspace():
        i += 1
    while i < len(text) and not text[i].isspace():
        i += 1
    return text[:i]


def install_completion_arbiter(
    kb: Any,
    *,
    context: str = "Chat",
) -> bool:
    """Bind the arbiter into *kb*. Returns True when bound. NEVER raises.

    Idempotent per KeyBindings object: a surface that mounts twice (attach,
    detach, re-attach) would otherwise stack handlers and accept a suggestion
    twice on one keypress.
    """
    if kb is None or not arbiter_enabled():
        return False
    try:
        if getattr(kb, "_ov_completion_arbiter_installed", False):
            return True

        def _smart_complete(event: Any) -> None:
            """Tab. NEVER raises — a key handler that throws kills the app."""
            try:
                buff = event.current_buffer
                doc = buff.document
                decision = resolve_tab_action(
                    menu_open=bool(getattr(buff, "complete_state", None)),
                    text_before_cursor=doc.text_before_cursor,
                    has_suggestion=bool(_suggestion_text(buff)),
                    cursor_at_end=bool(
                        getattr(doc, "is_cursor_at_the_end", False)),
                )
                if decision == "menu":
                    buff.complete_next()
                elif decision == "complete":
                    # select_first=False so the menu OPENS without silently
                    # committing to its first row — Tab again picks.
                    buff.start_completion(select_first=False)
                elif decision == "accept":
                    buff.insert_text(_suggestion_text(buff))
                else:
                    buff.insert_text(_indent())
            except Exception:  # noqa: BLE001
                logger.debug("[CompletionArbiter] tab handler degraded",
                             exc_info=True)

        def _accept_word(event: Any) -> None:
            """Accept ONE word of the suggestion. NEVER raises.

            The half-measure that makes ghost text usable for long
            suggestions: take the useful prefix and keep typing, instead of
            accepting a whole remembered line and deleting the tail.
            """
            try:
                buff = event.current_buffer
                word = _first_word(_suggestion_text(buff))
                if word:
                    buff.insert_text(word)
            except Exception:  # noqa: BLE001
                logger.debug("[CompletionArbiter] word-accept degraded",
                             exc_info=True)

        from backend.core.ouroboros.battle_test.keymap import bind_action

        bound = bind_action(
            kb, ACTION_SMART_COMPLETE, ("tab",), _smart_complete,
            context=context,
            description=("Complete: menu → verb/mention completion → accept "
                         "the ghost-text suggestion → indent"),
            # eager, so Tab resolves immediately rather than waiting to see
            # whether it is the first key of a longer sequence.
            eager=True,
        )
        bind_action(
            # ONE space-joined chord, not two keys. `bind_action` treats each
            # element of `default_keys` as an ALTERNATIVE binding, so
            # ("escape", "f") binds bare `escape` AND bare `f` — hijacking
            # the letter f mid-word and the Escape key the overlay arbiter
            # depends on. Chords are single strings; the tuple is the list of
            # alternatives.
            kb, ACTION_ACCEPT_WORD, ("escape f",), _accept_word,
            context=context,
            description="Accept one word of the ghost-text suggestion",
        )
        try:
            setattr(kb, "_ov_completion_arbiter_installed", True)
        except Exception:  # noqa: BLE001
            pass
        logger.debug("[CompletionArbiter] installed (bound=%s)", bound)
        return bound
    except Exception:  # noqa: BLE001
        logger.debug("[CompletionArbiter] install degraded", exc_info=True)
        return False
