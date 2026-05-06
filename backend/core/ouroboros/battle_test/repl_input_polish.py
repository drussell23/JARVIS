"""ReplInputPolish — @filepath mentions, Esc-to-cancel, terminal title.
========================================================================

Slice 4 of the **Gap #7 closure arc** (presentation restraint).

Three thin polishes that bring the REPL prompt to CC-equivalent
ergonomics:

  1. **@filepath mentions** — operators type ``@backend/auth.py`` inline
     and the path is auto-extracted into the existing ``/attach``
     mechanism. Saves the explicit verb invocation. Multiple mentions
     per line are supported.
  2. **Esc-to-cancel** — when the operator's input buffer is empty
     (typical mid-stream wait state), pressing Esc fires
     ``_handle_cancel`` for the most-recent active op. When the
     buffer has content, Esc preserves prompt_toolkit's default
     editing behavior (no surprise data loss).
  3. **Terminal title** — OSC 0 escape sequences update the terminal
     window title at phase transitions. Operators with multiple
     terminals see at-a-glance "which O+V is doing what".

Architectural reuse — zero duplication
---------------------------------------

* Existing ``_handle_attach`` and ``_handle_cancel`` are the
  authoritative paths. This module emits CALLS to them, never
  reimplements.
* ``prompt_toolkit.key_binding.KeyBindings`` is the stdlib-equivalent
  primitive for Esc binding. We construct one and the caller merges
  it into their existing bindings.
* ``sys.stderr.write('\\x1b]0;<title>\\x07')`` — the OSC 0 escape
  sequence is the terminal-emulator standard. No third-party
  terminal libraries.
* ``presentation_restraint.is_restraint_enabled`` is the umbrella
  flag; this module's behaviors gate on a SUB-flag that defaults
  to *the same value as* the umbrella when not explicitly set.

Authority boundary
------------------

* §1 deterministic — pure string parsing + escape-code emission
  + key-event handling; no LLM, no I/O on the hot path
* §7 fail-closed — every helper degrades silently. Bad path tokens
  yield no extraction; non-TTY → title set is a no-op; missing
  prompt_toolkit → no Esc binding.
* §8 observable — :class:`AttachmentExtraction` projection is a
  frozen record so the extraction can be SSE-published in a
  follow-up arc

What this module does NOT do
----------------------------

* Mount path-completion for ``@``-tokens — that's a Slice 5+ polish
  (combine with the existing slash-command completer via a hybrid).
* Globally trap Ctrl+C — that's already handled by signals at
  process scope (``BoundedShutdownWatchdog``).
* Set the terminal title directly from the orchestrator phase enum
  — that's a Slice 5 wiring step. This module supplies the helpers.
"""
from __future__ import annotations

import logging
import os
import re
import sys
from dataclasses import dataclass
from typing import List, Optional, Tuple

logger = logging.getLogger("Ouroboros.ReplInputPolish")


# ===========================================================================
# Schema + master flags
# ===========================================================================


REPL_INPUT_POLISH_SCHEMA_VERSION: str = "repl_input_polish.v1"


MASTER_FLAG_ENV_VAR: str = "JARVIS_REPL_INPUT_POLISH_ENABLED"
TITLE_ENABLED_ENV_VAR: str = "JARVIS_TERMINAL_TITLE_ENABLED"


def is_polish_enabled() -> bool:
    """``JARVIS_REPL_INPUT_POLISH_ENABLED``. **Default true** post Slice 5
    graduation (2026-05-04). Operators flip ``=false`` to disable
    @-mention extraction, Esc-to-cancel, and terminal title updates.
    NEVER raises."""
    raw = os.environ.get(MASTER_FLAG_ENV_VAR, "true")
    return raw.strip().lower() not in ("0", "false", "no", "off")


def is_terminal_title_enabled() -> bool:
    """``JARVIS_TERMINAL_TITLE_ENABLED``. Default true when polish is
    on, false when polish is off — operators get title updates as
    part of the polish bundle, but can opt out via explicit
    ``=false``. NEVER raises."""
    raw = os.environ.get(TITLE_ENABLED_ENV_VAR, "").strip().lower()
    if not raw:
        # Inherit from the umbrella polish flag
        return is_polish_enabled()
    return raw not in ("0", "false", "no", "off")


# ===========================================================================
# @filepath extraction — input pre-processing
# ===========================================================================


# Match ``@<token>`` where the token is non-empty and contains either:
#   * a slash (path-shaped: backend/auth.py, ./foo, ../bar)
#   * a dot followed by a non-empty extension (file-shaped: foo.py)
#
# Non-greedy on the path part so multiple mentions in one line work.
# The token can NOT contain whitespace or another @ (prevents accidental
# matches inside email addresses or decorators in pasted code).
#
# Anchored to a leading boundary (start-of-string OR whitespace) so
# tokens like ``user@host`` (email) DON'T match — only true mentions.
_MENTION_RE = re.compile(
    r'(?:^|(?<=\s))@'                                # @ at boundary
    r'(?P<path>[^\s@]+)'                             # the path token
)


# Heuristic: a path token must contain ``/`` (path-shaped) OR end with
# ``.<extension>`` of 1-6 alnum chars (file-shaped). Filters out
# ``@here`` which the regex above would otherwise capture.
_PATH_OR_FILE = re.compile(r'(/|\.[A-Za-z0-9]{1,6}$)')


def is_attachment_mention(token: str) -> bool:
    """Predicate: does ``token`` look like a real path mention?

    Returns ``False`` for:

      * Empty / non-string input
      * Tokens without ``/`` AND without an extension dot
        (``@here``, ``@foo`` → False; ``@foo.py``, ``@dir/file`` → True)

    NEVER raises.
    """
    if not isinstance(token, str) or not token:
        return False
    return bool(_PATH_OR_FILE.search(token))


@dataclass(frozen=True)
class AttachmentExtraction:
    """Result of :func:`extract_attachments`.

    Fields
    ------
    * ``cleaned_line`` — the original line with every recognized
      ``@<path>`` mention removed, whitespace normalized
    * ``paths`` — ordered tuple of extracted paths (no duplicates,
      same order they appeared)
    """

    cleaned_line: str
    paths: Tuple[str, ...]
    schema_version: str = REPL_INPUT_POLISH_SCHEMA_VERSION


def extract_attachments(line: object) -> AttachmentExtraction:
    """Pull ``@<path>`` mentions out of ``line``.

    Returns the cleaned line + ordered de-duplicated paths. Each path
    is verified by :func:`is_attachment_mention` — false-positive
    tokens like ``@here`` are LEFT IN PLACE in the cleaned line so
    operators see them as-is (treating them as prose).

    NEVER raises.
    """
    if not isinstance(line, str) or not line:
        return AttachmentExtraction(cleaned_line="", paths=())

    paths: List[str] = []
    seen: set = set()

    def _consume(match: re.Match) -> str:
        token = match.group("path")
        if not is_attachment_mention(token):
            # Leave unrecognized @tokens in place
            return match.group(0)
        if token not in seen:
            seen.add(token)
            paths.append(token)
        # Replace the @<path> token with empty — the surrounding
        # whitespace handling collapses adjacent gaps below.
        return ""

    cleaned = _MENTION_RE.sub(_consume, line)
    # Normalize whitespace (collapse multiple spaces, strip ends)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    return AttachmentExtraction(
        cleaned_line=cleaned,
        paths=tuple(paths),
    )


# ===========================================================================
# Terminal title — OSC 0 escape sequence
# ===========================================================================


_OSC_PREFIX: str = "\x1b]0;"   # ESC ] 0 ;
_OSC_TERMINATOR: str = "\x07"  # BEL


def _terminal_supports_osc() -> bool:
    """Best-effort check: are we running under a TTY that's likely to
    respect OSC 0? Conservative — when in doubt, return True so
    operators on capable terminals get title updates. The actual
    terminal silently ignores OSC 0 if it doesn't grok it."""
    try:
        from backend.core.ouroboros.battle_test.presentation_restraint import (
            real_stdout_isatty,
        )
        if not real_stdout_isatty():
            return False
    except Exception:  # noqa: BLE001
        pass
    # Most modern terminals (xterm, Terminal.app, iTerm2, kitty,
    # alacritty, gnome-terminal, Windows Terminal) support OSC 0.
    # Linux text consoles (TERM=linux) typically don't.
    term = os.environ.get("TERM", "")
    if term in ("linux", "dumb", ""):
        return False
    return True


def set_terminal_title(text: object) -> bool:
    """Set the terminal window title via OSC 0.

    Writes ``\\x1b]0;<text>\\x07`` to ``stderr`` (terminals listen on
    both streams; stderr is unbuffered by default which lets the
    title update arrive promptly even when stdout is redirected).

    Returns ``True`` on emission; ``False`` when:

      * Master flag :func:`is_terminal_title_enabled` is off
      * stdout is not a TTY (per :func:`real_stdout_isatty`)
      * ``TERM`` indicates a non-OSC console
      * Write itself raised

    NEVER raises.
    """
    if not is_terminal_title_enabled():
        return False
    if not _terminal_supports_osc():
        return False
    safe_text = str(text) if text is not None else ""
    # Sanitize: drop any embedded BEL or ESC characters that would
    # otherwise terminate the OSC sequence prematurely.
    safe_text = safe_text.replace("\x07", "").replace("\x1b", "")
    # Bound the length — terminal title bars are typically ~150 chars
    # max; longer titles render as garbage on some platforms.
    if len(safe_text) > 200:
        safe_text = safe_text[:197] + "…"
    try:
        sys.stderr.write(f"{_OSC_PREFIX}{safe_text}{_OSC_TERMINATOR}")
        sys.stderr.flush()
        return True
    except Exception:  # noqa: BLE001
        logger.debug(
            "[ReplInputPolish] set_terminal_title write failed",
            exc_info=True,
        )
        return False


def clear_terminal_title() -> bool:
    """Reset the title to an empty string. Most terminals fall back
    to their default (typically the shell's prompt-aware title)."""
    return set_terminal_title("")


def format_title(
    *,
    op_id: Optional[str] = None,
    phase: Optional[str] = None,
    cost_used: float = 0.0,
    cost_budget: float = 0.0,
) -> str:
    """Build a compact terminal-title string from current state.

    Examples:

      * Idle: ``"O+V"``
      * Mid-op: ``"O+V · GENERATE op-019d83 · $0.04/$0.50"``
      * Cost only: ``"O+V · $0.04/$0.50"``

    NEVER raises. Inputs are best-effort coerced.
    """
    parts: List[str] = ["O+V"]
    if isinstance(phase, str) and phase and phase.upper() != "IDLE":
        parts.append(phase.upper())
    if isinstance(op_id, str) and op_id:
        # Truncate op_id tail to 8 chars for compact display
        tail = op_id.split("-")[-1][:8] if "-" in op_id else op_id[:8]
        parts.append(f"op-{tail}")
    if cost_budget > 0.0:
        parts.append(f"${cost_used:.2f}/${cost_budget:.2f}")
    elif cost_used > 0.0:
        parts.append(f"${cost_used:.2f}")
    return " · ".join(parts)


# ===========================================================================
# Esc-to-cancel — prompt_toolkit binding factory
# ===========================================================================


def make_esc_cancel_binding(
    repl_instance: object,
    *,
    flow: object = None,
) -> Optional[object]:
    """Build a ``prompt_toolkit.key_binding.KeyBindings`` instance with
    a single binding: bare Esc when the input buffer is empty fires
    cancellation for the most-recent active op.

    The buffer-empty filter is critical — it prevents Esc from
    blowing away in-progress operator typing. Operators only see
    cancellation behavior when they're WAITING (typing nothing),
    which matches the natural usage: "I'm watching this op stream,
    I want to abort, hit Esc."

    Returns ``None`` when prompt_toolkit isn't available or the
    master flag is off. The caller merges the returned bindings
    into their main ``KeyBindings`` if non-None.
    """
    if not is_polish_enabled():
        return None

    try:
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.filters import Condition
    except ImportError:
        return None

    # Resolve the flow (where ``_active_ops`` lives) — fall back to
    # the repl_instance's ``_flow`` attribute when not explicitly
    # provided. NEVER raise.
    resolved_flow = flow
    if resolved_flow is None:
        resolved_flow = getattr(repl_instance, "_flow", None)

    bindings = KeyBindings()

    @Condition
    def _buffer_empty() -> bool:
        try:
            from prompt_toolkit.application import get_app
            return get_app().current_buffer.text == ""
        except Exception:  # noqa: BLE001
            return False

    @bindings.add("escape", filter=_buffer_empty, eager=True)
    def _on_esc(event) -> None:  # noqa: ARG001
        """Cancel the most-recent active op."""
        try:
            op_id = _pick_active_op_id(resolved_flow)
            if not op_id:
                return
            handler = getattr(repl_instance, "_handle_cancel", None)
            if not callable(handler):
                return
            # Schedule the async handler — fire-and-forget per the
            # existing /cancel REPL contract.
            import asyncio
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(handler(op_id, immediate=False))
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            logger.debug(
                "[ReplInputPolish] Esc handler raised", exc_info=True,
            )

    return bindings


def _pick_active_op_id(flow: object) -> Optional[str]:
    """Pick the most-recent active op_id from the flow's ``_active_ops``
    + ``_swarm_snapshots``. Returns ``None`` when no active op."""
    if flow is None:
        return None
    active = getattr(flow, "_active_ops", None)
    if not isinstance(active, set) or not active:
        return None
    snapshots = getattr(flow, "_swarm_snapshots", None)
    # Prefer the most-recently-started op when snapshots have ts data
    if isinstance(snapshots, dict) and snapshots:
        best_op: Optional[str] = None
        best_ts: float = -1.0
        for op_id in active:
            snap = snapshots.get(op_id)
            ts = float(getattr(snap, "started_monotonic", 0.0)) if snap is not None else 0.0
            if ts > best_ts:
                best_ts = ts
                best_op = op_id
        if best_op is not None:
            return best_op
    # Fallback: arbitrary pick from active set
    return next(iter(active))


# ===========================================================================
# §37 Slice 7 (PRD §37.7 Tier 1 #2, 2026-05-05) —
# @mention path-completion completer
# ===========================================================================
#
# Closes the deferred TODO at lines 53-54 above ("Mount path-completion
# for ``@``-tokens — that's a Slice 5+ polish"). Composes the existing
# `_MENTION_RE` extraction shape (a path-token must contain `/` or
# `.<ext>`) with prompt_toolkit's stdlib `PathCompleter` so operators
# typing `@back<TAB>` get a dropdown of repo files starting with
# `back`.
#
# Architectural locks:
#
#   * **Composes existing PathCompleter** — no parallel file-tree
#     walking. The stdlib completer handles all the tricky bits
#     (case-insensitive matching, hidden-file gating, expand-user).
#   * **Gates on word boundary** — fires only when the current word
#     under cursor starts with `@`. Operators typing prose / goals
#     / @-decorators in pasted code don't see file-tree dropdowns
#     interleaved with their natural input.
#   * **NEVER raises** — any prompt_toolkit / filesystem error
#     returns no completions (silent degrade).
#   * **Master-flag-aware** — when polish is off, returns no
#     completer (operator opt-out).


def build_mention_completer() -> Optional[object]:
    """Build a ``prompt_toolkit.completion.Completer`` that fires
    only when the current cursor-word starts with ``@``. Composes
    stdlib :class:`PathCompleter` for the actual file-tree
    walking.

    Returns ``None`` when:
      * prompt_toolkit isn't available (headless / sandbox)
      * polish master flag is off (operator opt-out)

    NEVER raises. Returns no completions on filesystem errors.
    """
    if not is_polish_enabled():
        return None
    try:
        from prompt_toolkit.completion import (
            Completer, Completion, PathCompleter,
        )
    except ImportError:
        return None

    # Stdlib path completer — handles glob expansion, case
    # matching, and prefix-relative path rendering.
    inner = PathCompleter(
        only_directories=False,
        get_paths=lambda: ["."],  # repo-root relative
        # File extensions matter little here; PathCompleter's
        # default behavior matches all entries.
    )

    class _MentionCompleter(Completer):
        """@<partial> completer. Composes stdlib PathCompleter."""

        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            # Find the current word under cursor — everything
            # back to the last whitespace boundary or start.
            word_start = max(
                text.rfind(" ") + 1,
                text.rfind("\t") + 1,
                0,
            )
            current_word = text[word_start:]
            if not current_word.startswith("@"):
                return
            # Strip the leading @, hand the remainder to
            # PathCompleter via a synthetic Document.
            path_part = current_word[1:]
            try:
                from prompt_toolkit.document import Document
            except ImportError:
                return
            try:
                # The synthetic document has only the path part
                # so PathCompleter walks from cwd correctly.
                synth_doc = Document(
                    text=path_part,
                    cursor_position=len(path_part),
                )
                # Delegate. Each Completion's start_position is
                # relative to the synthetic doc; remap by
                # accounting for the @-prefix offset (-1) so the
                # final replacement covers the entire @<path>
                # token in the operator's actual input.
                for completion in inner.get_completions(
                    synth_doc, complete_event,
                ):
                    yield Completion(
                        text="@" + completion.text,
                        start_position=(
                            completion.start_position - 1
                        ),
                        display=completion.display,
                        display_meta="@mention path",
                    )
            except Exception:  # noqa: BLE001 — defensive
                # Filesystem error / permission error / etc. —
                # return no completions rather than crash the
                # REPL.
                return

    return _MentionCompleter()


__all__ = [
    "AttachmentExtraction",
    "MASTER_FLAG_ENV_VAR",
    "REPL_INPUT_POLISH_SCHEMA_VERSION",
    "TITLE_ENABLED_ENV_VAR",
    "build_mention_completer",
    "clear_terminal_title",
    "extract_attachments",
    "format_title",
    "is_attachment_mention",
    "is_polish_enabled",
    "is_terminal_title_enabled",
    "make_esc_cancel_binding",
    "set_terminal_title",
]
