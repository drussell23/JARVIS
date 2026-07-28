"""When Enter means "another line" instead of "go".

A goal worth giving an autonomous organism is often longer than one line — a
paragraph of context, a pasted traceback, a list of constraints. The prompt
accepted exactly one line, so those had to be flattened by hand.

And it was not only a limit on typing. With `multiline=False`, a PASTED block
loses its newlines: an operator pasting a stack trace got it silently
collapsed into one line, which is data loss rather than inconvenience.

Enter still submits
-------------------
The obvious fix — turn multiline on — breaks submission: Enter starts
inserting newlines and there is no longer a key that means "go". So the
buffer is conditionally multiline. Enter submits UNLESS the text is visibly
unfinished, and "unfinished" is decided by the same rules every REPL and shell
already taught operators:

  * a trailing backslash — the shell's own continuation mark;
  * an unclosed code fence — pasting half a ``` block and having it submit is
    the most annoying possible outcome;
  * an unbalanced bracket — a half-typed dict or call.

Alt+Enter always inserts a newline, for prose that ends in none of those.

Why not "Enter always inserts, Ctrl+D submits"
-----------------------------------------------
Because the common case is one line. Making every short goal cost two
keystrokes to save the rare long one is the wrong trade, and it breaks the
muscle memory of every operator who has ever used a prompt.

Detection is SYNTACTIC and cheap — no parsing, no language awareness. A rule
that guesses wrong holds the operator's input hostage, so every ambiguous case
resolves to SUBMIT: the escape hatch (Alt+Enter) is always available, whereas
a prompt that refuses to submit has no escape at all.
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("Ouroboros.InputContinuation")

__all__ = ["multiline_enabled", "wants_continuation",
           "strip_continuations", "install_newline_binding",
           "continuation_filter"]

_OPENERS = {"(": ")", "[": "]", "{": "}"}
_CLOSERS = {v: k for k, v in _OPENERS.items()}


def multiline_enabled() -> bool:
    """Default ON. A prompt that silently eats a pasted traceback is worse."""
    return os.environ.get(
        "JARVIS_INPUT_MULTILINE_ENABLED", "1",
    ).strip().lower() not in ("0", "false", "no", "off")


def _fence_is_open(text: str) -> bool:
    """An odd number of ``` fences means one is still open."""
    return text.count("```") % 2 == 1


def _brackets_unbalanced(text: str) -> bool:
    """Is a bracket left open?

    Quoted spans are skipped, so a lone `(` inside a string — `"a (smiley"` —
    does not trap the operator. Depth is clamped at zero: a stray closer is
    someone's prose, not a reason to demand more input.
    """
    depth = 0
    quote = ""
    escaped = False
    for ch in text:
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if quote:
            if ch == quote:
                quote = ""
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            continue
        if ch in _OPENERS:
            depth += 1
        elif ch in _CLOSERS:
            depth = max(0, depth - 1)
    return depth > 0


def wants_continuation(text: Any) -> bool:
    """Should Enter add a line rather than submit? NEVER raises.

    False for anything ambiguous. A prompt that refuses to submit has no
    escape hatch; one that submits early can be retyped, and Alt+Enter is
    always there for a deliberate newline.
    """
    try:
        if not multiline_enabled():
            return False
        raw = str(text or "")
        if not raw.strip():
            # Enter on an empty buffer submits (a no-op), rather than
            # silently growing blank lines the operator cannot see.
            return False
        # A trailing backslash is the shell's continuation mark and the one
        # signal operators reach for without being told. Checked on the raw
        # line-end so trailing spaces do not defeat it.
        if raw.rstrip(" \t").endswith("\\"):
            return True
        if _fence_is_open(raw):
            return True
        # Bracket balance counts ONLY once the operator is already composing
        # something multi-line. On a first line it is prose far more often
        # than code — "the smiley is (: nice" would otherwise trap them in a
        # prompt that will not submit, which is the exact hostage-taking this
        # module's rules exist to avoid. By the second line the intent is
        # structural and balance is a real signal.
        if "\n" not in raw:
            return False
        return _brackets_unbalanced(raw)
    except Exception:  # noqa: BLE001 — a stuck prompt is the worst outcome
        logger.debug("[InputContinuation] degraded", exc_info=True)
        return False


def strip_continuations(text: Any) -> str:
    """Remove the backslashes that only meant "keep going".

    The daemon should receive the goal, not the typing mechanics. A fence or a
    bracket is CONTENT and survives untouched; a trailing `\\` is punctuation
    for the prompt and would otherwise reach the model as noise.
    """
    try:
        lines = str(text or "").split("\n")
        out = []
        for line in lines:
            stripped = line.rstrip(" \t")
            out.append(stripped[:-1].rstrip() if stripped.endswith("\\")
                       else line)
        return "\n".join(out)
    except Exception:  # noqa: BLE001
        return str(text or "")


def install_newline_binding(kb: Any) -> bool:
    """Bind Alt+Enter to "insert a newline, whatever the rule thinks".

    Load-bearing rather than a convenience. Every ambiguous case in
    `wants_continuation` resolves to SUBMIT, and that is only safe BECAUSE a
    deliberate newline is always one keystroke away. prompt_toolkit ships no
    `escape enter` binding of its own — verified against the installed
    library, not assumed — so without this the escape hatch the rules above
    promise does not exist.

    NEVER raises: a cockpit whose Enter key works is worth more than one that
    refused to start over a binding.
    """
    try:
        if kb is None or not multiline_enabled():
            return False

        @kb.add("escape", "enter")
        def _newline(event: Any) -> None:
            try:
                event.current_buffer.insert_text("\n")
            except Exception:  # noqa: BLE001
                pass

        return True
    except Exception:  # noqa: BLE001
        logger.debug("[InputContinuation] newline binding degraded",
                     exc_info=True)
        return False


def continuation_filter(get_text: Any) -> Any:
    """A `Buffer.multiline` filter over *get_text*. NEVER raises.

    Bound to ONE buffer's text rather than reading `get_app().current_buffer`.
    The focused buffer is usually the prompt, but "usually" is how a rule ends
    up consulting the wrong text the moment focus moves to a lane or a menu —
    and a filter that reads the wrong buffer fails in the direction of a
    prompt that will not submit.

    Binding it also makes the rule testable with no running Application, which
    is what caught this: the get_app() version silently returned False for
    every case under test and looked like it worked.

    Degrades to a filter that always says "submit", never to a bare bool —
    `is_multiline` CALLS this value on every keystroke.
    """
    from prompt_toolkit.filters import Condition

    @Condition
    def _cond() -> bool:
        try:
            return wants_continuation(get_text())
        except Exception:  # noqa: BLE001
            return False

    return _cond
