"""Operator-facing help derived from the code, not from a parallel table.

45 of the cockpit's verbs showed a blank description. The reflex fix is to
author 45 ``__verb_help__`` strings across 45 modules, which is 45 new places
to drift out of step with behaviour nobody will update. The actual root cause
is the absence of a runtime introspection layer: the information is already
in the source — in docstrings, and in the signatures themselves — and nothing
was reading it.

Two resolvers live here, both pure and both safe on anything:

``extract_operator_section``
    Pulls an ``Operator:`` block out of a docstring. This is the one place a
    maintainer can write a sentence aimed at the person typing the verb rather
    than at the next maintainer, and it wins over everything else.

``derive_usage``
    Builds ``Usage: /verb <required> [optional]`` from the signature when no
    prose exists anywhere. A verb with no description at least tells the
    operator what it takes.

The signature is TRANSLATED, not dumped. A raw Python signature in a palette
is worse than a blank: ``dispatch_search_command(line, ctx=None, *, limit=5)``
answers nothing an operator asked. Two rules do the translation:

  * **Injected parameters are invisible.** ``self``, ``ctx``, ``session`` and
    friends are wiring the dispatcher receives, never something typed. So is
    ``line`` and its synonyms — the raw command text every dispatcher in this
    codebase accepts as its transport. Rendering ``/molt <line>`` would be
    technically accurate and completely useless.
  * **POSIX brackets.** Required positionals become ``<arg>``; anything with a
    default becomes ``[arg]``; ``*args`` becomes ``[arg...]``. That is the
    convention every CLI user already knows, so it needs no legend.

Nothing here raises. A palette that throws while the operator is typing is a
worse failure than a palette with a gap in it.
"""
from __future__ import annotations

import ast
import inspect
import logging
import os
import re
from typing import Any, FrozenSet, List

logger = logging.getLogger("Ouroboros.VerbUsage")

__all__ = [
    "UNDOCUMENTED",
    "derive_usage",
    "extract_operator_section",
    "injected_parameters",
    "mine_subcommands",
]

#: Shown when every resolver comes up empty. Deliberately not a blank and
#: deliberately not a guess: it reads as "nobody has written this yet", which
#: is true and actionable, where invented prose reads as a description that
#: happens to be wrong — and the operator acts on it.
UNDOCUMENTED = "[undocumented]"

#: Parameters a dispatcher RECEIVES rather than ones an operator TYPES.
#:
#: Split into two groups because they are wrong for different reasons, and
#: conflating them hides that from whoever edits this next:
#:
#:   * plumbing — the execution context injected by the router;
#:   * transport — the raw command text itself. Every ``dispatch_<verb>_command``
#:     in this codebase takes the whole line and parses it internally, so the
#:     parameter is an implementation detail of the calling convention. The
#:     operator's actual arguments live INSIDE that string, where no signature
#:     can see them, which is exactly why an authored ``Operator:`` line beats
#:     a derived one and is tried first.
_PLUMBING = (
    "self", "cls", "ctx", "context", "session", "app", "ui", "client",
    "repl", "flow", "console", "loop", "bridge", "state", "registry",
    "logger", "bus", "harness", "orchestrator", "service", "deps",
)
_TRANSPORT = ("line", "raw", "text", "argv", "args", "arg", "command", "cmd",
              "rest", "tail", "input")


def injected_parameters() -> FrozenSet[str]:
    """Names hidden from derived usage.

    Env-extendable via ``JARVIS_VERB_USAGE_HIDE`` (comma-separated) so a new
    injection convention does not require a code change here — the list is a
    property of the calling convention, which is allowed to evolve.
    """
    names = set(_PLUMBING) | set(_TRANSPORT)
    try:
        extra = os.environ.get("JARVIS_VERB_USAGE_HIDE", "") or ""
        names |= {n.strip() for n in extra.split(",") if n.strip()}
    except Exception:  # noqa: BLE001
        pass
    return frozenset(names)


# ---------------------------------------------------------------------------
# 1. authored operator prose
# ---------------------------------------------------------------------------

#: ``Operator:`` at the start of a line, any indentation, any case. The value
#: may continue onto following lines until a blank line or the next section.
_OPERATOR_RE = re.compile(r"^[ \t]*operator[ \t]*:[ \t]*(.*)$", re.IGNORECASE)
#: A following line that starts a NEW docstring section ends the block.
_SECTION_RE = re.compile(r"^[ \t]*[A-Za-z][A-Za-z ]{0,20}:[ \t]*$")


def extract_operator_section(doc: Any) -> str:
    """Return the ``Operator:`` block of a docstring, or ``""``.

    Accepts anything at all. ``__doc__`` is ``None`` on a great many callables
    — C builtins, ``functools.partial``, anything run under ``python -OO`` —
    and the whole point of this layer is that it runs over callables nobody
    curated, so a type check is not enough: the argument is coerced.

    Continuation lines are joined so a maintainer can wrap naturally without
    the palette showing a truncated half-sentence.
    """
    try:
        text = doc if isinstance(doc, str) else ""
        if not text.strip():
            return ""
        collected: List[str] = []
        capturing = False
        for raw_line in text.splitlines():
            if not capturing:
                match = _OPERATOR_RE.match(raw_line)
                if match:
                    capturing = True
                    head = match.group(1).strip()
                    if head:
                        collected.append(head)
                continue
            stripped = raw_line.strip()
            if not stripped or _SECTION_RE.match(raw_line):
                break
            collected.append(stripped)
        return " ".join(collected).strip()
    except Exception:  # noqa: BLE001 — never break a palette over a docstring
        logger.debug("[VerbUsage] operator-section parse degraded",
                     exc_info=True)
        return ""


# ---------------------------------------------------------------------------
# 2. usage derived from the signature
# ---------------------------------------------------------------------------

def _unwrap(fn: Any) -> Any:
    """See through decorators and partials to the function actually called.

    Without this a decorated dispatcher reports ``(*args, **kwargs)`` — the
    wrapper's signature — which is both useless and confidently wrong.
    """
    try:
        import functools
        seen = 0
        while isinstance(fn, functools.partial) and seen < 8:
            fn = fn.func
            seen += 1
        return inspect.unwrap(fn)
    except Exception:  # noqa: BLE001
        return fn


def derive_usage(fn: Any, verb: str) -> str:
    """``Usage: /verb <required> [optional]``, or ``""`` if nothing to say.

    Empty rather than ``Usage: /verb`` when every parameter is injected: a
    usage line listing no arguments tells the operator nothing they did not
    already know from typing the verb, and it would crowd out the honest
    ``[undocumented]`` marker that says help is genuinely missing.
    """
    try:
        target = _unwrap(fn)
        try:
            sig = inspect.signature(target)
        except (TypeError, ValueError):
            # Builtins and C extensions have no introspectable signature.
            return ""
        hidden = injected_parameters()
        parts: List[str] = []
        for name, param in sig.parameters.items():
            if name in hidden or name.startswith("_"):
                continue
            kind = param.kind
            if kind is inspect.Parameter.VAR_KEYWORD:
                continue                      # **kwargs is never typed
            if kind is inspect.Parameter.VAR_POSITIONAL:
                parts.append(f"[{name}...]")
                continue
            if param.default is inspect.Parameter.empty:
                # Keyword-only with no default IS required, and POSIX renders
                # required the same way regardless of how Python passes it.
                parts.append(f"<{name}>")
            else:
                parts.append(f"[{name}]")
        if not parts:
            return ""
        slug = str(verb or "").lstrip("/")
        return f"Usage: /{slug} " + " ".join(parts)
    except Exception:  # noqa: BLE001
        logger.debug("[VerbUsage] usage derivation degraded", exc_info=True)
        return ""


# ---------------------------------------------------------------------------
# 3. the subcommand vocabulary, mined from the dispatcher's own body
# ---------------------------------------------------------------------------

#: Variables that plausibly hold a parsed token. A comparison against one of
#: these is a dispatch decision; a comparison against ``self.mode`` or
#: ``response.status`` is internal logic that happens to use a string.
_TOKEN_NAMES = frozenset({
    "sub", "subcmd", "subcommand", "cmd", "command", "verb", "action",
    "mode", "op", "kind", "what", "which", "head", "first", "token",
    "arg", "argument", "choice", "target", "part", "word", "tail", "rest",
})

#: Strings that are never a subcommand even when compared against a token.
_NOT_SUBCOMMANDS = frozenset({
    "true", "false", "none", "null", "nan", "y", "n", "0", "1",
    "utf-8", "utf8", "ascii", "r", "w", "a", "rb", "wb",
})

#: Never show more than this many; a palette line is one line.
_MAX_SUBCOMMANDS = 8


def _is_token_target(node: Any) -> bool:
    """True when *node* looks like a parsed command token.

    ``sub``, ``parts[1]``, ``tokens[0].lower()`` — all yes. ``self.mode`` and
    ``resp.status`` — no: an attribute is object state, and mining it produces
    confident nonsense like ``/breadcrumbs completed | failed``.
    """
    try:
        while isinstance(node, ast.Call):        # unwrap .lower()/.strip()
            node = node.func
        if isinstance(node, ast.Attribute):
            if node.attr in ("lower", "upper", "strip", "casefold"):
                return _is_token_target(node.value)
            return False
        if isinstance(node, ast.Subscript):
            return _is_token_target(node.value)
        if isinstance(node, ast.Name):
            return node.id.lower().lstrip("_").rstrip("0123456789") in _TOKEN_NAMES
        return False
    except Exception:  # noqa: BLE001
        return False


def _literal_strings(node: Any) -> List[str]:
    out: List[str] = []
    for item in getattr(node, "elts", []) if isinstance(
        node, (ast.Tuple, ast.List, ast.Set)
    ) else []:
        if isinstance(item, ast.Constant) and isinstance(item.value, str):
            out.append(item.value)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        out.append(node.value)
    return out


def mine_subcommands(fn: Any) -> List[str]:
    """The subcommands a dispatcher actually accepts, read from its body.

    This exists because signature introspection is nearly blind here. Every
    ``dispatch_<verb>_command`` takes the whole line as one string and parses
    it internally, so the signature says ``(line)`` and the real vocabulary —
    ``status``, ``history``, ``explain`` — lives in the comparisons the body
    makes against the parsed token. That vocabulary is what an operator wants
    from a palette, and it was sitting unread in the source.

    Deliberately conservative, in the direction of saying nothing:

      * only comparisons against a token-shaped variable count, so object
        state (``self.mode == "fast"``) is ignored;
      * a single literal is not a vocabulary — one hit is far more likely to
        be an internal flag check than a subcommand, so two are required;
      * order of first appearance is preserved, because dispatchers are
        written with the common case first and that is a better ordering than
        alphabetical.

    Returns ``[]`` for anything it cannot read — a C builtin, a lambda, a
    function whose source is unavailable (``exec``, a REPL definition, a
    stripped ``.pyc``). NEVER raises.
    """
    try:
        import textwrap
        target = _unwrap(fn)
        try:
            src = textwrap.dedent(inspect.getsource(target))
        except (OSError, TypeError):
            return []
        try:
            tree = ast.parse(src)
        except SyntaxError:
            # A decorated nested def can dedent to something unparseable.
            return []

        found: List[str] = []

        def _add(values: List[str]) -> None:
            for value in values:
                low = value.strip().lower()
                if not low or low in _NOT_SUBCOMMANDS:
                    continue
                if " " in low or len(low) > 16 or low.startswith(("-", "/")):
                    continue
                if not low[0].isalpha():
                    continue
                if low not in found:
                    found.append(low)

        for node in ast.walk(tree):
            if isinstance(node, ast.Compare) and _is_token_target(node.left):
                for op, comparator in zip(node.ops, node.comparators):
                    if isinstance(op, (ast.Eq, ast.In)):
                        _add(_literal_strings(comparator))
            # ``if sub in DISPATCH`` where DISPATCH is a literal dict.
            elif isinstance(node, ast.Dict) and node.keys:
                keys = [k for k in node.keys
                        if isinstance(k, ast.Constant)
                        and isinstance(k.value, str)]
                if len(keys) >= 2 and len(keys) == len(node.keys):
                    _add([k.value for k in keys])

        return found[:_MAX_SUBCOMMANDS] if len(found) >= 2 else []
    except Exception:  # noqa: BLE001
        logger.debug("[VerbUsage] subcommand mining degraded", exc_info=True)
        return []
