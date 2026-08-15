"""repl_verb_cage — a ``*_repl`` module IS its verb, structurally.

THE DEFECT THIS CLOSES
----------------------
Slash verbs were mounted by hand: a module exported ``dispatch_x_command``,
and somebody remembered to add an ``if line.startswith("/x")`` branch to the
REPL. Five governance modules honour the ``*_repl`` contract; three had a
branch. ``/reach`` and ``/why`` were importable, tested, documented — and
untypeable. Their own docstrings claimed they were "auto-discovered through
the naming cage", which described a mechanism that did not exist.

That is the unmounted-feature class in its purest form, and the reason it
survives review is that every test passes: the dispatcher is correct, the
renderer is correct, and nothing anywhere asserts that a human can reach
them. So the fix is not another branch. It is to make the mount structural,
so that shipping an unmounted verb stops being possible.

THE CONTRACT
------------
A module ``governance/<verb>_repl.py`` claims ``/<verb>`` by exporting BOTH:

* ``__verb_help__`` — a dict-literal ``{verb: one-line description}``
* ``dispatch_<verb>_command(line) -> result`` with ``.text`` / ``.ok``

Both, because either alone is ambiguous: a dispatcher with no help is a
verb nobody can discover, and help with no dispatcher is a promise with no
mechanism. The module name is the verb — there is no mapping table to drift
out of step with the filesystem, and renaming the file renames the verb.

DISCOVERY READS SOURCE, NOT MODULES
-----------------------------------
The scan is ``ast`` over the package directory: ~0.16s for 68 candidates and
**zero imports**, against ~0.47s and 68 import side effects for the obvious
alternative. Descriptions come from ``ast.literal_eval`` of the dict literal,
so the palette is fully populated before a single module is loaded.

Dispatch then imports exactly ONE module — the one whose name matches what
the operator typed. A verb nobody uses costs nothing.

ORDERING IS DELIBERATE
----------------------
The cage runs AFTER the REPL's explicit ladder and BEFORE its unknown-verb
handler. Verbs with a hand-written branch keep byte-identical behaviour;
only lines that currently fall through reach the cage. Mounting cannot
change what already works.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import ast
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("Ouroboros.ReplVerbCage")

REPL_VERB_CAGE_SCHEMA_VERSION: str = "repl_verb_cage.1"

_SUFFIX = "_repl"


def cage_enabled() -> bool:
    """Master switch. Default TRUE — an unmounted verb is the defect."""
    return (os.environ.get("JARVIS_REPL_VERB_CAGE_ENABLED", "true")
            or "").strip().lower() not in ("0", "false", "no", "off")


@dataclass(frozen=True)
class ModuleVerb:
    """One verb a module claims by name."""

    verb: str
    module: str
    description: str

    @property
    def slash_form(self) -> str:
        return f"/{self.verb}"

    @property
    def dispatch_name(self) -> str:
        return f"dispatch_{self.verb}_command"

    def to_dict(self) -> Dict[str, Any]:
        return {"verb": self.verb, "module": self.module,
                "description": self.description,
                "slash_form": self.slash_form}


_cache: Optional[Tuple[ModuleVerb, ...]] = None
_cache_lock = threading.Lock()


def _package_dir() -> Optional[Path]:
    """This package's directory, without importing its members."""
    try:
        return Path(__file__).resolve().parent
    except Exception:  # noqa: BLE001
        return None


def _read_declaration(path: Path, verb: str) -> Optional[str]:
    """The description this file declares for ``verb``, or None.

    Returns None unless the module honours BOTH halves of the contract.
    Parsing rather than importing: discovery must never run a module's
    top-level code, or a broken verb takes the whole palette down with it.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, ValueError):
        return None

    help_map: Optional[Dict[str, Any]] = None
    has_dispatch = False
    want = f"dispatch_{verb}_command"

    # Module level only. A nested `__verb_help__` or a dispatch defined
    # inside a function is not an export, and treating it as one would
    # advertise a verb that `getattr` cannot then find.
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if stmt.name == want:
                has_dispatch = True
        elif isinstance(stmt, ast.Assign):
            if not any(getattr(t, "id", None) == "__verb_help__"
                       for t in stmt.targets):
                continue
            try:
                value = ast.literal_eval(stmt.value)
            except (ValueError, SyntaxError, TypeError):
                continue
            if isinstance(value, dict):
                help_map = value

    if not has_dispatch or not help_map:
        return None
    text = help_map.get(verb)
    return str(text) if isinstance(text, str) and text.strip() else ""


def discover(*, force: bool = False) -> Tuple[ModuleVerb, ...]:
    """Every verb the package claims by name. Cached. NEVER raises.

    The result is a fact about the filesystem, and the filesystem does not
    change under a running REPL, so it is computed once. ``force`` exists
    for tests, which do move files.
    """
    global _cache
    if not cage_enabled():
        return ()
    with _cache_lock:
        if _cache is not None and not force:
            return _cache
    found: list = []
    root = _package_dir()
    if root is not None:
        try:
            candidates = sorted(root.glob(f"*{_SUFFIX}.py"))
        except OSError:
            candidates = []
        for path in candidates:
            verb = path.stem[: -len(_SUFFIX)]
            if not verb or verb.startswith("_"):
                continue
            description = _read_declaration(path, verb)
            if description is None:
                continue
            found.append(ModuleVerb(
                verb=verb,
                module=f"{__package__}.{path.stem}",
                description=description,
            ))
    result = tuple(found)
    with _cache_lock:
        _cache = result
    logger.debug("[VerbCage] %d module verbs: %s",
                 len(result), ", ".join(v.slash_form for v in result))
    return result


def reset_cache() -> None:
    global _cache
    with _cache_lock:
        _cache = None


def find(line: str) -> Optional[ModuleVerb]:
    """The verb a typed line claims, or None. NEVER raises."""
    try:
        word = (line or "").strip().split(None, 1)[0]
    except IndexError:
        return None
    if not word.startswith("/"):
        return None
    name = word[1:].strip().lower()
    if not name:
        return None
    return next((v for v in discover() if v.verb == name), None)


def dispatch(line: str) -> Optional[Any]:
    """Route a typed line to its module. ``None`` means "not mine".

    ``None`` and a falsy result are deliberately different: the caller must
    be able to fall through to its unknown-verb handler without swallowing a
    verb that ran and legitimately answered "no".

    Imports exactly the one module the operator named, at the moment they
    name it. An import failure returns a REFUSAL rather than None, because
    ``/why`` existing-but-broken and ``/why`` not existing call for different
    operator responses, and collapsing them into "did you mean…" hides a bug.
    """
    spec = find(line)
    if spec is None:
        return None
    try:
        import importlib

        module = importlib.import_module(spec.module)
        handler = getattr(module, spec.dispatch_name, None)
        if not callable(handler):
            # Discovery proved the source declares it, so absence here means
            # the module rebinds or deletes it at import — worth saying out
            # loud rather than silently degrading to "unknown verb".
            return _Refusal(
                f"{spec.slash_form} is declared by {spec.module} but "
                f"{spec.dispatch_name} is not callable at runtime")
        return handler(line)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[VerbCage] %s dispatch failed", spec.slash_form,
                     exc_info=True)
        return _Refusal(
            f"{spec.slash_form} failed to load ({type(exc).__name__}: {exc})")


@dataclass(frozen=True)
class _Refusal:
    """A mounted verb that could not run — never a silent fall-through."""

    text: str
    ok: bool = False
    matched: bool = True


__all__ = [
    "REPL_VERB_CAGE_SCHEMA_VERSION",
    "ModuleVerb",
    "cage_enabled",
    "discover",
    "dispatch",
    "find",
    "reset_cache",
]
