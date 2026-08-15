"""Read a module's CODE, never its prose. One probe, four former copies.

Structural assertions in this suite ask questions like "does the watchdog
touch the op-ledger", "does the store call ``os.access``", "does the offload
live at one seam". A substring search over source cannot tell an explanation
from a use, and these modules explain themselves at length — deliberately
NAMING the thing they avoid:

    # No `to_thread` here any more: `_audit` owns the offload …
    ``os.access`` answers a question about permission bits, and the failures
    that matter here are not permission bits …

Both read as violations to ``in``. That mistake has cost this suite tests on
five separate occasions, each time fixed locally, each time re-appearing in
the next file — four private ``_code_of`` helpers with three different
signatures and two different notions of what counts as code.

``ast.unparse`` is the fix that generalises: it renders the parsed tree, so
comments are gone by construction rather than by a rule someone has to
remember, and docstrings are dropped explicitly below. One of the four copies
used ``ast.get_source_segment``, which preserves comments and therefore still
had the original bug.

Python 3.9+.
"""
from __future__ import annotations

import ast
import inspect
import textwrap
from typing import Any, List


def _strip_docstring(body: List[ast.stmt]) -> List[ast.stmt]:
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(getattr(body[0], "value", None), ast.Constant)
            and isinstance(body[0].value.value, str)):
        return body[1:]
    return body


def code_of(target: Any, *names: str) -> str:
    """Executable code of ``target``, with docstrings and comments removed.

    ``target`` may be a module, a class, or a single function — the three
    shapes the private copies each supported one of. ``names`` filters to
    particular functions or methods; with none given, every function in the
    target contributes.

    NEVER raises: an unreadable target yields an empty string, which fails an
    ``in`` assertion loudly rather than passing a ``not in`` one silently.
    """
    try:
        source = textwrap.dedent(inspect.getsource(target))
        tree = ast.parse(source)
    except (OSError, TypeError, SyntaxError, ValueError):
        return ""

    funcs = [n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if names:
        funcs = [n for n in funcs if n.name in names]
    elif inspect.isfunction(target) or inspect.ismethod(target):
        # A single function: its own body, not every nested helper twice.
        funcs = funcs[:1]

    out: List[str] = []
    for node in funcs:
        for stmt in _strip_docstring(list(node.body)):
            try:
                out.append(ast.unparse(stmt))
            except Exception:  # noqa: BLE001
                continue
    if not funcs and not names:
        # A module with no functions still has module-level code worth
        # reading — an import, a constant, a registration call.
        for stmt in _strip_docstring(list(tree.body)):
            try:
                out.append(ast.unparse(stmt))
            except Exception:  # noqa: BLE001
                continue
    return "\n".join(out)


__all__ = ["code_of"]
