"""Library contract — what the INSTALLED package actually says, read from disk.

## The gap this bridges

The exemplar injector shows the model a verified-passing test for a module like
its subject. For FastAPI subjects there is nothing to show: this repository has
no passing FastAPI test. So the model writes ``assert "x" in response`` against
a ``JSONResponse`` — ``TypeError: argument of type 'JSONResponse' is not
iterable`` — nine attempts in a row, because how a response is read is a fact
about a LIBRARY, and it was answering from memory.

The fact is on disk. ``site-packages/starlette/responses.py`` says a
``Response`` sets ``self.body`` and that ``JSONResponse.render`` returns bytes.
So when no exemplar covers a third-party package the subject depends on, its
contract is extracted from the installed source and put in the prompt: exact
for THIS version, not recalled for some other.

## Nothing is imported, and nothing is listed

* PURE AST + FILESYSTEM. ``find_spec`` is asked only about a TOP-LEVEL name,
  which locates without importing; every submodule is then found by walking
  directories. No third-party code runs in the daemon at PLAN time.
* NO FRAMEWORK MAP. What to show is derived from the subject:
    1. the names it imports from the package (``JSONResponse``, ``APIRouter``)
       — the things its test must construct and inspect; and
    2. the package's TESTING entry points, found structurally: top-level
       modules of the package whose name contains ``test`` (``testclient``,
       ``testing``, ``test_utils`` — the convention FastAPI, Flask, Click,
       aiohttp and Django all follow).
* RE-EXPORTS ARE FOLLOWED, across packages, by AST. ``fastapi/testclient.py``
  is one line — ``from starlette.testclient import TestClient`` — and
  ``fastapi.responses.JSONResponse`` is starlette's. A visited set bounds the
  walk; there is no depth constant.

## What a contract contains

For a class: bases, its ``__init__`` signature, the INSTANCE ATTRIBUTES that
``__init__`` assigns (``self.body`` appears in no signature, and is precisely
what the failing tests needed), the first paragraph of its docstring, and the
signatures of its public methods — one hop up its base classes when the base is
defined in the same module. ``Annotated[T, Doc(...)]`` renders as ``T``: the
metadata is documentation for humans, and FastAPI's runs to pages per parameter.

Blocks are atomic and added in priority order until the budget is spent.
NEVER raises; an unreadable or exotic package yields an empty contract.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import logging
import sysconfig
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Locating installed source without importing it
# ---------------------------------------------------------------------------


def _site_roots() -> Tuple[Path, ...]:
    paths = sysconfig.get_paths()
    roots = []
    for key in ("purelib", "platlib"):
        try:
            roots.append(Path(paths[key]).resolve())
        except Exception:  # noqa: BLE001
            continue
    return tuple(dict.fromkeys(roots))


def third_party_root(top: str) -> Optional[Path]:
    """Package directory (or module file) of installed top-level *top*, or
    ``None`` if it is not third-party. ``find_spec`` on a top-level name
    locates it WITHOUT importing it."""
    try:
        if not top or not top.isidentifier():
            return None
        spec = importlib.util.find_spec(top)
        if spec is None:
            return None
        if spec.submodule_search_locations:
            candidate = Path(list(spec.submodule_search_locations)[0]).resolve()
        elif spec.origin and spec.origin.endswith(".py"):
            candidate = Path(spec.origin).resolve()
        else:
            return None
        if any(root == candidate or root in candidate.parents for root in _site_roots()):
            return candidate
        return None
    except Exception:  # noqa: BLE001
        return None


def module_file(dotted: str) -> Optional[Path]:
    """Source file for installed module *dotted*, by walking directories."""
    parts = [p for p in (dotted or "").split(".") if p]
    if not parts:
        return None
    root = third_party_root(parts[0])
    if root is None:
        return None
    if root.is_file():
        return root if len(parts) == 1 else None
    cur = root
    for index, part in enumerate(parts[1:], start=1):
        last = index == len(parts) - 1
        if (cur / part).is_dir():
            cur = cur / part
            continue
        if last and (cur / f"{part}.py").is_file():
            return cur / f"{part}.py"
        return None
    init = cur / "__init__.py"
    return init if init.is_file() else None


def _parse(path: Path) -> Optional[ast.Module]:
    try:
        return ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return None


def _absolute(importer: str, importer_is_package: bool, node: ast.ImportFrom) -> str:
    """Dotted name of the module an ``ImportFrom`` refers to."""
    if not node.level:
        return node.module or ""
    base = importer.split(".")
    if not importer_is_package:
        base = base[:-1]
    base = base[: len(base) - (node.level - 1)] if node.level > 1 else base
    return ".".join(base + ([node.module] if node.module else []))


def resolve_name(
    dotted_module: str, name: str, visited: Optional[Set[Tuple[str, str]]] = None,
) -> Optional[Tuple[str, ast.AST, ast.Module]]:
    """``(module, defining node, that module's tree)`` for *name*, following
    ``from X import name [as alias]`` re-exports across packages."""
    visited = visited if visited is not None else set()
    key = (dotted_module, name)
    if key in visited:
        return None
    visited.add(key)
    path = module_file(dotted_module)
    if path is None:
        return None
    tree = _parse(path)
    if tree is None:
        return None
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return dotted_module, node, tree
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        for alias in node.names:
            if (alias.asname or alias.name) != name:
                continue
            origin = _absolute(dotted_module, path.name == "__init__.py", node)
            found = resolve_name(origin, alias.name, visited)
            if found is not None:
                return found
    return None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _annotation(node: Optional[ast.AST]) -> str:
    if node is None:
        return ""
    try:
        # Annotated[T, *metadata] -> T. The metadata documents the parameter
        # for a human; FastAPI's Doc(...) runs to pages per argument.
        if (
            isinstance(node, ast.Subscript)
            and getattr(node.value, "id", getattr(node.value, "attr", "")) == "Annotated"
        ):
            inner = node.slice
            if isinstance(inner, ast.Tuple) and inner.elts:
                return _annotation(inner.elts[0])
        return ast.unparse(node)
    except Exception:  # noqa: BLE001
        return ""


def _default(node: Optional[ast.AST]) -> str:
    if node is None:
        return ""
    try:
        text = ast.unparse(node)
    except Exception:  # noqa: BLE001
        return "..."
    # A default that is itself a page of code is not part of the contract.
    return text if "\n" not in text and text.count("(") <= 1 else "..."


def signature(node: ast.AST) -> str:
    args = node.args  # type: ignore[attr-defined]
    rendered: List[str] = []
    positional = list(args.posonlyargs) + list(args.args)
    defaults = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)

    def one(arg: ast.arg, default: Optional[ast.AST]) -> str:
        text = arg.arg
        ann = _annotation(arg.annotation)
        if ann:
            text += f": {ann}"
        dflt = _default(default)
        if dflt:
            text += f" = {dflt}" if ann else f"={dflt}"
        return text

    for index, (arg, default) in enumerate(zip(positional, defaults)):
        rendered.append(one(arg, default))
        if args.posonlyargs and index == len(args.posonlyargs) - 1:
            rendered.append("/")
    if args.vararg:
        rendered.append("*" + args.vararg.arg)
    elif args.kwonlyargs:
        rendered.append("*")
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        rendered.append(one(arg, default))
    if args.kwarg:
        rendered.append("**" + args.kwarg.arg)
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    returns = _annotation(getattr(node, "returns", None))
    return f"{prefix} {node.name}({', '.join(rendered)})" + (f" -> {returns}" if returns else "")  # type: ignore[attr-defined]


def _first_paragraph(node: ast.AST) -> str:
    try:
        doc = ast.get_docstring(node) or ""  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001
        return ""
    return " ".join(doc.strip().split("\n\n")[0].split())


def _instance_attributes(init: ast.AST) -> List[str]:
    """Names ``__init__`` assigns on ``self`` — the part of a class's surface
    that no signature shows."""
    seen: List[str] = []
    for node in ast.walk(init):
        targets: Sequence[ast.AST] = ()
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = (node.target,)
        for target in targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name) and target.value.id == "self"
                and not target.attr.startswith("_") and target.attr not in seen
            ):
                seen.append(target.attr)
    return seen


def render_class(node: ast.ClassDef, tree: ast.Module, dotted: str) -> str:
    local = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
    chain: List[ast.ClassDef] = [node]
    for base in node.bases:  # one hop, same module: where `body` usually lives
        name = getattr(base, "id", None)
        if name in local and local[name] is not node:
            chain.append(local[name])
    bases = ", ".join(_annotation(b) for b in node.bases)
    lines = [f"class {node.name}({bases}):   # {dotted}" if bases else f"class {node.name}:   # {dotted}"]
    doc = _first_paragraph(node)
    if doc:
        lines.append(f'    """{doc}"""')
    attributes: List[str] = []
    methods: Dict[str, str] = {}
    for cls in chain:
        for item in cls.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if item.name == "__init__":
                for attr in _instance_attributes(item):
                    if attr not in attributes:
                        attributes.append(attr)
            public = not item.name.startswith("_") or item.name in ("__init__", "__call__")
            if public and item.name not in methods:
                origin = "" if cls is node else f"   # inherited from {cls.name}"
                methods[item.name] = f"    {signature(item)}: ...{origin}"
    if attributes:
        lines.append(f"    # instance attributes set by __init__: {', '.join(attributes)}")
    lines.extend(methods.values())
    if len(lines) == 1:
        lines.append("    ...")
    return "\n".join(lines)


def render_index(node: ast.ClassDef, dotted: str, tree: Optional[ast.Module] = None) -> str:
    """A class the subject never touches directly — a testing entry point,
    say — as an INDEX: what it is, how it is built, what it exposes. Its
    method SIGNATURES are not shown; the model needs to know `client.get(url)`
    exists, not the eleven httpx parameter types behind it. On the live probe
    the full TestClient rendering alone was 1,400 tokens of parameter types."""
    bases = ", ".join(_annotation(b) for b in node.bases)
    head = f"class {node.name}({bases}):   # {dotted}" if bases else f"class {node.name}:   # {dotted}"
    lines = [head]
    doc = _first_paragraph(node)
    if doc:
        lines.append(f'    """{doc}"""')
    # Construction may be inherited (JSONResponse has no __init__ of its
    # own); climb one same-module base for it, as the full render does.
    chain: List[ast.ClassDef] = [node]
    if tree is not None:
        local = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
        for base in node.bases:
            name = getattr(base, "id", None)
            if name in local and local[name] is not node:
                chain.append(local[name])
    methods: List[str] = []
    attrs: List[str] = []
    init_line = ""
    for cls in chain:
        for item in cls.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if item.name == "__init__":
                # The SIGNATURE is the nearest __init__; the ATTRIBUTES are
                # gathered up the chain -- starlette's JSONResponse.__init__
                # sets nothing itself and delegates to Response.__init__,
                # which is where `body` lives.
                if not init_line:
                    origin = "" if cls is node else f"   # inherited from {cls.name}"
                    init_line = f"    {signature(item)}: ...{origin}"
                for attr in _instance_attributes(item):
                    if attr not in attrs:
                        attrs.append(attr)
            elif not item.name.startswith("_") and item.name + "()" not in methods:
                methods.append(item.name + "()")
    if init_line:
        lines.append(init_line)
    if attrs:
        lines.append(f"    # instance attributes set by __init__: {', '.join(attrs)}")
    if methods:
        lines.append(f"    # methods: {', '.join(methods)}")
    return "\n".join(lines)


def produced_types(subject_source: str) -> List[str]:
    """Names the subject RETURNS or RAISES as constructed objects — what a
    test of it will receive and must inspect. `return JSONResponse(...)`,
    `raise HTTPException(...)`, `return await X(...)`. These come first: they
    are the objects the failing assertions were written against."""
    out: List[str] = []
    try:
        tree = ast.parse(subject_source)
    except Exception:  # noqa: BLE001
        return out
    for node in ast.walk(tree):
        value = None
        if isinstance(node, (ast.Return, ast.Raise)):
            value = getattr(node, "value", None) or getattr(node, "exc", None)
        if isinstance(value, ast.Await):
            value = value.value
        if isinstance(value, ast.Call):
            fn = value.func
            name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
            if name and name not in out:
                out.append(name)
    return out


_TYPE_IN_MESSAGE = re.compile(r"'([A-Za-z_][A-Za-z0-9_]*)'|\b([A-Z][A-Za-z0-9_]*)\(\)")


def type_names_in_error(text: str) -> List[str]:
    """Candidate type names an exception message quotes. No error taxonomy:
    every quoted identifier is a candidate, and the caller keeps only those
    that resolve to an installed library type — which is what makes the
    extraction safe without a list of error shapes."""
    found: List[str] = []
    for a, b in _TYPE_IN_MESSAGE.findall(text or ""):
        name = a or b
        if name and name not in found and not name.islower():
            found.append(name)
    return found


def _import_origins(sources: Sequence[str]) -> Dict[str, str]:
    """``name -> dotted module`` for every third-party ``from M import name``
    across *sources*, plus ``M`` for every ``import M``."""
    origins: Dict[str, str] = {}
    for source in sources:
        try:
            tree = ast.parse(source)
        except Exception:  # noqa: BLE001
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                if third_party_root(node.module.split(".")[0]) is None:
                    continue
                for alias in node.names:
                    if alias.name != "*":
                        origins.setdefault(alias.asname or alias.name, node.module + "\0" + alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if third_party_root(alias.name.split(".")[0]) is not None:
                        origins.setdefault(alias.asname or alias.name, alias.name)
    return origins


def contract_for_type(name: str, sources: Sequence[str]) -> str:
    """The full contract of ONE library type, located through what *sources*
    import. ``""`` when *name* is not a library type those sources know."""
    try:
        origins = _import_origins(sources)
        target = origins.get(name)
        found = None
        if target and "\0" in target:
            module, imported = target.split("\0", 1)
            found = resolve_name(module, imported)
        else:
            # Attribute form: `fastapi.responses.JSONResponse` with `import fastapi`.
            for alias, module in origins.items():
                if "\0" in module:
                    continue
                found = resolve_name(module, name)
                if found is not None:
                    break
        if found is None:
            return ""
        origin, node, tree = found
        return render(node, tree, origin)
    except Exception:  # noqa: BLE001
        logger.debug("[LibraryContract] type contract degraded", exc_info=True)
        return ""


def render(node: ast.AST, tree: ast.Module, dotted: str) -> str:
    if isinstance(node, ast.ClassDef):
        return render_class(node, tree, dotted)
    doc = _first_paragraph(node)
    head = f"{signature(node)}:   # {dotted}"
    return head + (f'\n    """{doc}"""' if doc else "") + "\n    ..."


# ---------------------------------------------------------------------------
# What to show for a subject
# ---------------------------------------------------------------------------


def imported_names(subject_source: str, top: str) -> List[Tuple[str, str]]:
    """``(module, name)`` for everything the subject imports from *top*."""
    out: List[Tuple[str, str]] = []
    try:
        tree = ast.parse(subject_source)
    except Exception:  # noqa: BLE001
        return out
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and not node.level and node.module:
            if node.module.split(".")[0] == top:
                for alias in node.names:
                    if alias.name != "*" and (node.module, alias.name) not in out:
                        out.append((node.module, alias.name))
    return out


def testing_modules(top: str) -> List[str]:
    """Top-level modules of package *top* that are its TESTING entry points,
    recognised by the convention — ``test`` in the module name — that FastAPI,
    Starlette, Flask, Click, aiohttp and Django all follow."""
    root = third_party_root(top)
    if root is None or not root.is_dir():
        return []
    found = []
    for entry in sorted(root.iterdir()):
        stem = entry.stem if entry.is_file() else entry.name
        if stem.startswith("_") or "test" not in stem.lower():
            continue
        if (entry.is_file() and entry.suffix == ".py") or (entry / "__init__.py").is_file():
            found.append(f"{top}.{stem}")
    return found


def _public_names(dotted: str) -> List[str]:
    path = module_file(dotted)
    tree = _parse(path) if path is not None else None
    if tree is None:
        return []
    names: List[str] = []
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                names.append(node.name)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                # `from x import Name as Name` is the explicit re-export idiom.
                if alias.asname and alias.asname == alias.name and not alias.name.startswith("_"):
                    names.append(alias.name)
    return list(dict.fromkeys(names))


def contract_for(
    subject_source: str,
    tops: Sequence[str],
    budget_tokens: int,
    *,
    estimate: Optional[Callable[[str], int]] = None,
) -> str:
    """Contract blocks for third-party *tops*, most important first, within
    *budget_tokens*. ``""`` when there is nothing installed to read."""
    try:
        if estimate is None:
            from backend.core.ouroboros.governance.ast_signature_pruner import (  # noqa: PLC0415
                _estimate as estimate,
            )
        produced = set(produced_types(subject_source))
        imported: List[Tuple[str, str]] = []
        for top in tops:
            if third_party_root(top) is None:
                continue
            imported.extend(imported_names(subject_source, top))
        # 1. what the subject RETURNS or RAISES -- the objects a test receives;
        # 2. everything else it imports from the package;
        # 3. the package's testing entry points, as an index.
        wanted: List[Tuple[str, str, bool]] = (
            [(m, n, True) for m, n in imported if n in produced]
            + [(m, n, False) for m, n in imported if n not in produced]
        )
        for top in tops:
            if third_party_root(top) is None:
                continue
            for module in testing_modules(top):
                wanted.extend((module, name, False) for name in _public_names(module))
        blocks: List[str] = []
        shown: Set[Tuple[str, str]] = set()
        used = 0
        for module, name, referenced in wanted:
            found = resolve_name(module, name)
            if found is None:
                continue
            origin, node, tree = found
            if (origin, name) in shown:
                continue
            via = "" if origin == module else f"   (imported as {module}.{name})"
            # FULL contract only for what the subject PRODUCES -- the objects a
            # test receives and inspects. A class the subject merely uses
            # (a router it registers handlers on, a base model it subclasses)
            # gets the index: enough to construct it, not the forty-parameter
            # constructor a test will never call. Functions are short; full.
            full = (name in produced) or not isinstance(node, ast.ClassDef)
            block = (render(node, tree, origin) if full else render_index(node, origin, tree)) + via
            cost = estimate(block)
            if used + cost > budget_tokens:
                continue
            shown.add((origin, name))
            blocks.append(block)
            used += cost
        return "\n\n".join(blocks)
    except Exception:  # noqa: BLE001
        logger.debug("[LibraryContract] extraction degraded", exc_info=True)
        return ""


__all__ = [
    "contract_for",
    "contract_for_type",
    "produced_types",
    "render_index",
    "type_names_in_error",
    "imported_names",
    "module_file",
    "render",
    "resolve_name",
    "signature",
    "testing_modules",
    "third_party_root",
]
