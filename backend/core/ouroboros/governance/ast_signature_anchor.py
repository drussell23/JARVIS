"""AST-Signature Anchor — inject a target module's EXACT public API into the
generation prompt so the model cannot hallucinate interfaces.

The local coder model reliably produces structurally-valid code but invents
function signatures, argument counts, and return shapes when writing against a
module it has only skimmed (measured 2026-09-06: writing a test for
``model_physics.py`` it guessed ``parse_model_physics("model_a", 100, 200)`` and
``effective_ceiling(100, 50) == 150.0`` when the real API is
``parse_model_physics(payload) -> Optional[ModelPhysics]`` and a two-arg
``effective_ceiling(physics, configured_ceiling) -> int``). Reading the whole
286-line source did NOT fix it; a distilled, authoritative signature block does —
short, unmissable, and stating the contract the candidate MUST honour.

Pure, deterministic, stdlib-only (``ast``). NEVER raises — every edge (syntax
error, exotic decorators, missing type hints, unreadable file) degrades to a
weaker-but-valid signature or an empty string, never a crash. Master switch
``JARVIS_AST_SIGNATURE_ANCHOR_ENABLED`` (default TRUE); OFF ⇒ empty block ⇒
byte-identical legacy prompt.
"""
from __future__ import annotations

import ast
import copy
import os
import logging
import re
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.SigAnchor")

_ENV_ENABLED = "JARVIS_AST_SIGNATURE_ANCHOR_ENABLED"
_ENV_MAX_MODULES = "JARVIS_AST_SIGNATURE_ANCHOR_MAX_MODULES"
_ENV_MAX_CHARS = "JARVIS_AST_SIGNATURE_ANCHOR_MAX_CHARS"
#: Per-symbol docstring excerpt cap (chars). The docstring IS the semantic
#: contract a signature cannot carry — ``payload: Any`` says nothing, while
#: the docstring says "an Ollama /api/show payload, architecture-prefixed
#: keys under model_info, None when any load-bearing field is missing".
#: Measured 2026-09-07: with signatures alone the model built flat
#: ``{"context_length": 2048, "num_layers": 32}`` payloads and every test
#: failed on ``assert None is not None``.
_ENV_DOC_CHARS = "JARVIS_AST_SIGNATURE_ANCHOR_DOC_CHARS"
#: Ceiling for the ADAPTIVE per-symbol excerpt: the module's remaining char
#: budget is spread evenly across its public symbols, floored at
#: ``_DEFAULT_DOC_CHARS`` and capped here — a module with few symbols keeps
#: its full contracts, a module with dozens degrades gracefully to the floor.
_ENV_DOC_CHARS_MAX = "JARVIS_AST_SIGNATURE_ANCHOR_DOC_CHARS_MAX"
_DEFAULT_MAX_MODULES = 4
_DEFAULT_MAX_CHARS = 6000
_DEFAULT_DOC_CHARS = 420
_DEFAULT_DOC_CHARS_MAX = 1200
#: Data-access pattern: how many ``x.get('k')`` / ``x['k']`` / helper('k')
#: expressions a def may list, and the per-expression length cap.
_ENV_ACCESS_ITEMS = "JARVIS_AST_SIGNATURE_ANCHOR_ACCESS_ITEMS"
_DEFAULT_ACCESS_ITEMS = 24
_ACCESS_EXPR_MAX_CHARS = 80
_ACCESS_METHODS = frozenset({"get", "pop", "setdefault", "getlist", "getattr"})
#: Derived-semantics lines (# where / # returns / # returns None if).
_CONTRACT_EXPR_MAX_CHARS = 90
_CONTRACT_SHAPE_MAX_CHARS = 600
_CONTRACT_MAX_ITEMS = 8
_SENTENCE_END_RE = re.compile(r"[.!?](?:\s|$)")

_PY_PATH_RE = re.compile(r"[A-Za-z0-9_./\\-]+\.py")


def anchor_enabled() -> bool:
    raw = os.environ.get(_ENV_ENABLED, "true").strip().lower()
    return raw not in ("false", "0", "no", "off")


def _int_env(name: str, default: int) -> int:
    try:
        v = int(os.environ.get(name, "").strip())
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def _sig_line(node) -> str:
    """One-line signature for a def/async def — name, params (with whatever type
    hints exist), and return annotation if present. Falls back to bare-name
    reconstruction if ``ast.unparse`` is unavailable or raises."""
    is_async = isinstance(node, ast.AsyncFunctionDef)
    prefix = "async def " if is_async else "def "
    try:
        clone = copy.copy(node)
        clone.body = [ast.Pass()]
        clone.decorator_list = []
        ast.fix_missing_locations(clone)
        head = ast.unparse(clone).split("\n", 1)[0].rstrip()
        return head + " ..."
    except Exception:  # noqa: BLE001 — degrade to bare-name reconstruction
        try:
            a = node.args
            names: List[str] = [
                arg.arg for arg in
                list(getattr(a, "posonlyargs", []) or []) + list(a.args)
            ]
            if a.vararg:
                names.append("*" + a.vararg.arg)
            names += [arg.arg for arg in a.kwonlyargs]
            if a.kwarg:
                names.append("**" + a.kwarg.arg)
            return f"{prefix}{node.name}({', '.join(names)}): ..."
        except Exception:  # noqa: BLE001
            return f"{prefix}{node.name}(...): ..."


def _is_public(name: str) -> bool:
    return not name.startswith("_")


def _doc_excerpt(node, max_chars: int) -> str:
    """Single-line excerpt of *node*'s docstring, bounded to *max_chars* and
    cut at the last sentence boundary (else the last word) before the cap.
    ``""`` when there is no docstring. Triple double-quotes are neutralised so
    the excerpt can be re-embedded as a docstring in the anchor block. NEVER
    raises."""
    try:
        raw = ast.get_docstring(node, clean=True)
    except Exception:  # noqa: BLE001 — exotic node/body shapes
        return ""
    if not raw:
        return ""
    text = " ".join(raw.split()).replace('"""', "'''").replace("\\", "/")
    if len(text) <= max_chars:
        return text
    head = text[:max_chars]
    cut = -1
    for m in _SENTENCE_END_RE.finditer(head):
        cut = m.end()
    if cut < max_chars // 3:
        sp = head.rfind(" ")
        cut = sp if sp >= max_chars // 3 else max_chars
    return head[:cut].rstrip() + " …"


def _has_key_literal(expr) -> bool:
    """True when *expr* contains a non-empty string constant — the marker of
    a literal key (``'model_info'``, ``arch + '.' + name``)."""
    return any(
        isinstance(c, ast.Constant) and isinstance(c.value, str) and c.value
        for c in ast.walk(expr)
    )


def _access_lines(node, indent: str) -> List[str]:
    """The data-access pattern of a def: every ``x.get('k')`` / ``x['k']`` /
    ``'k' in x`` / nested-helper call carrying a string-literal key,
    unparsed verbatim in source order. This is the INPUT SHAPE the docstring
    describes in prose — the exact key vocabulary and nesting the function
    reads (measured 2026-09-07: with prose alone the model invented
    ``qwen2.kv_heads`` for the real ``qwen2.attention.head_count_kv`` and
    dropped the ``model_info`` nesting). Bounded by
    ``JARVIS_AST_SIGNATURE_ANCHOR_ACCESS_ITEMS``; NEVER raises."""
    max_items = _int_env(_ENV_ACCESS_ITEMS, _DEFAULT_ACCESS_ITEMS)
    try:
        helpers = {
            n.name for n in ast.walk(node)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n is not node
        }
        found: List[Tuple[int, int, str]] = []
        seen: set = set()
        for sub in ast.walk(node):
            expr = None
            if isinstance(sub, ast.Call):
                f = sub.func
                if sub.args and _has_key_literal(sub.args[0]) and (
                    (isinstance(f, ast.Attribute) and f.attr in _ACCESS_METHODS)
                    or (isinstance(f, ast.Name) and f.id in helpers)
                ):
                    expr = sub
            elif isinstance(sub, ast.Subscript) and _has_key_literal(sub.slice):
                expr = sub
            elif (
                isinstance(sub, ast.Compare) and len(sub.ops) == 1
                and isinstance(sub.ops[0], (ast.In, ast.NotIn))
                and _has_key_literal(sub.left)
            ):
                expr = sub
            if expr is None:
                continue
            try:
                s = ast.unparse(expr)
            except Exception:  # noqa: BLE001
                continue
            if len(s) > _ACCESS_EXPR_MAX_CHARS or s in seen:
                continue
            seen.add(s)
            found.append((getattr(expr, "lineno", 0), getattr(expr, "col_offset", 0), s))
        if not found:
            return []
        found.sort()
        items = [s for _, _, s in found[:max_items]]
        return [indent + "    # reads: " + "; ".join(items)]
    except Exception:  # noqa: BLE001 — additive, never fatal
        return []


def _local_assignments(node) -> dict:
    """``name -> [value exprs in source order]`` for single-Name assignments in
    the def's own body (nested defs excluded)."""
    out: dict = {}
    nested = {
        id(n) for n in ast.walk(node)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) and n is not node
    }
    for sub in ast.walk(node):
        if id(sub) in nested:
            continue
        if isinstance(sub, ast.Assign) and len(sub.targets) == 1 and isinstance(sub.targets[0], ast.Name):
            out.setdefault(sub.targets[0].id, []).append(sub.value)
        elif isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name) and sub.value is not None:
            out.setdefault(sub.target.id, []).append(sub.value)
    return out


def _names_in(expr) -> set:
    return {n.id for n in ast.walk(expr) if isinstance(n, ast.Name)}


def _substitute(expr, assigns: dict, depth: int):
    """Inline single-assignment locals into *expr* (depth-bounded), skipping
    self-referential rebinds (``x = x or y`` keeps the earlier definition)."""
    if depth <= 0:
        return expr

    class _Sub(ast.NodeTransformer):
        def visit_Name(self, n):  # noqa: N802 — ast visitor API
            if isinstance(n.ctx, ast.Load) and n.id in assigns:
                defs = [v for v in assigns[n.id] if n.id not in _names_in(v)]
                # Exactly one real definition — otherwise the value is
                # branch-dependent and the NAME is the honest rendering.
                if len(defs) == 1:
                    try:
                        if len(ast.unparse(defs[0])) <= _CONTRACT_EXPR_MAX_CHARS:
                            return _substitute(copy.deepcopy(defs[0]), assigns, depth - 1)
                    except Exception:  # noqa: BLE001
                        pass
            return n

    return _Sub().visit(copy.deepcopy(expr))


def _own_body_nodes(node):
    """Every AST node of *node*'s body EXCLUDING nested def/lambda subtrees."""
    skip: set = set()
    for n in ast.walk(node):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) and n is not node:
            skip.update(id(x) for x in ast.walk(n))
    for n in ast.walk(node):
        if id(n) not in skip:
            yield n


def _contract_lines(node, indent: str) -> List[str]:
    """Derived semantics the docstring and access pattern cannot state:
    ``# where``   nested single-return helpers, inlined (``field(name) = ...``);
    ``# returns`` the constructed return value with locals substituted, so a
                  consumer sees the FORMULA behind each field;
    ``# returns None if`` every guard that short-circuits to ``None``.
    Measured 2026-09-07: with keys alone the model still asserted
    ``kv_bytes_per_token == 2`` against a value that is
    ``block_count * kv_heads * (key_len + val_len) * kv_cache_dtype_bytes()``.
    Bounded; NEVER raises."""
    lines: List[str] = []
    try:
        pad = indent + "    # "
        # -- helpers ---------------------------------------------------
        helpers: List[str] = []
        for n in ast.walk(node):
            if (
                isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n is not node
                and len(n.body) == 1 and isinstance(n.body[0], ast.Return) and n.body[0].value is not None
            ):
                try:
                    args = ", ".join(a.arg for a in n.args.args)
                    s = f"{n.name}({args}) = {ast.unparse(n.body[0].value)}"
                except Exception:  # noqa: BLE001
                    continue
                if len(s) <= _CONTRACT_EXPR_MAX_CHARS:
                    helpers.append(s)
        if helpers:
            lines.append(pad + "where: " + "; ".join(helpers[:_CONTRACT_MAX_ITEMS]))
        # -- return shape ----------------------------------------------
        assigns = _local_assignments(node)
        none_guards: List[str] = []
        shapes: List[str] = []
        for sub in _own_body_nodes(node):
            if isinstance(sub, ast.If):
                body = sub.body
                if (
                    len(body) == 1 and isinstance(body[0], ast.Return)
                    and isinstance(body[0].value, ast.Constant) and body[0].value.value is None
                ):
                    try:
                        g = ast.unparse(sub.test)
                    except Exception:  # noqa: BLE001
                        continue
                    if len(g) <= _CONTRACT_EXPR_MAX_CHARS and g not in none_guards:
                        none_guards.append(g)
            elif isinstance(sub, ast.Return) and isinstance(sub.value, (ast.Call, ast.Dict, ast.Tuple)):
                try:
                    s = ast.unparse(_substitute(sub.value, assigns, 2))
                except Exception:  # noqa: BLE001
                    continue
                if s not in shapes:
                    shapes.append(s)
        for s in shapes[:2]:
            lines.append(pad + "returns: " + s[: _CONTRACT_SHAPE_MAX_CHARS])
        if none_guards:
            lines.append(pad + "returns None if: " + "; ".join(none_guards[:_CONTRACT_MAX_ITEMS]))
    except Exception:  # noqa: BLE001 — additive, never fatal
        return lines
    return lines


def _access_of(expr):
    """``(base, key)`` when *expr* is ``base.get(key)``-style or ``base[key]``."""
    if (
        isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute)
        and expr.func.attr in _ACCESS_METHODS and expr.args
    ):
        return expr.func.value, expr.args[0]
    if isinstance(expr, ast.Subscript):
        return expr.value, expr.slice
    return None


def _input_shape_lines(node, indent: str) -> List[str]:
    """Synthesised INPUT SHAPE of a def's parameters, resolved statically
    from its access pattern: ``info = payload.get('model_info')`` makes
    ``info`` the container at ``payload['model_info']``; a key built as
    ``arch + '.' + name`` renders with ``arch`` as ``<general.architecture>``
    (the key it was read from) and ``name`` as each literal a nested helper
    was called with. The result is a literal skeleton the consumer can copy:
    ``payload = {'model_info': {'general.architecture': ...,
    '<general.architecture>.context_length': ...}}`` — nesting and flat
    dotted keys SHOWN, not described (measured 2026-09-07: told in prose,
    the model still nested 'general.architecture' and put prefixed keys
    beside model_info instead of inside it). Bounded; NEVER raises."""
    try:
        a = node.args
        params = [p.arg for p in list(getattr(a, "posonlyargs", []) or []) + list(a.args) + list(a.kwonlyargs)]
        if not params:
            return []
        assigns = _local_assignments(node)
        helpers = {
            n.name: n for n in ast.walk(node)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n is not node
        }
        # helper param -> literal strings it is called with (own body only)
        bindings: dict = {}
        for c in _own_body_nodes(node):
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id in helpers:
                hp = [p.arg for p in helpers[c.func.id].args.args]
                for p, arg in zip(hp, c.args):
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        bindings.setdefault(c.func.id, {}).setdefault(p, []).append(arg.value)
        # node id -> enclosing helper name (for param binding lookup)
        owner: dict = {}
        for hname, h in helpers.items():
            for x in ast.walk(h):
                owner[id(x)] = hname

        def _first_access(expr):
            for n in ast.walk(expr):
                acc = _access_of(n)
                if acc:
                    return acc
            return None

        def _resolve_path(expr, depth: int = 0) -> Optional[List[str]]:
            """Container path (keys from the parameter) for a base expr."""
            if depth > 6 or not isinstance(expr, ast.Name):
                return None
            if expr.id in params:
                return []
            defs = [v for v in assigns.get(expr.id, []) if expr.id not in _names_in(v)]
            if len(defs) != 1:
                return None
            acc = _first_access(defs[0])
            if not acc:
                return None
            base_path = _resolve_path(acc[0], depth + 1)
            if base_path is None:
                return None
            keys = _render_key(acc[1], None, depth + 1)
            return base_path + [keys[0]] if keys else None

        def _render_key(expr, hname, depth: int = 0) -> List[str]:
            if depth > 6:
                return []
            if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
                return [expr.value]
            if isinstance(expr, ast.Name):
                bound = bindings.get(hname or "", {}).get(expr.id)
                if bound:
                    return list(dict.fromkeys(bound))[:_CONTRACT_MAX_ITEMS * 2]
                path = _resolve_path(expr, depth + 1)
                return ["<" + path[-1] + ">"] if path else []
            if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
                left = _render_key(expr.left, hname, depth + 1)
                right = _render_key(expr.right, hname, depth + 1)
                return [l + r for l in left for r in right][:_CONTRACT_MAX_ITEMS * 2]
            return []

        tree: dict = {}
        root_name: Optional[str] = None
        # Source order (ast.walk is breadth-first): the first key a function
        # reads is the first key the skeleton shows.
        ordered = sorted(
            (n for n in ast.walk(node) if _access_of(n)),
            key=lambda n: (getattr(n, "lineno", 0), getattr(n, "col_offset", 0)),
        )
        for n in ordered:
            acc = _access_of(n)
            base_path = _resolve_path(acc[0])
            if base_path is None:
                continue
            # the parameter this access ultimately reads from
            cur = acc[0]
            while isinstance(cur, ast.Name) and cur.id not in params:
                defs = [v for v in assigns.get(cur.id, []) if cur.id not in _names_in(v)]
                fa = _first_access(defs[0]) if len(defs) == 1 else None
                if not fa:
                    break
                cur = fa[0]
            pname = cur.id if isinstance(cur, ast.Name) and cur.id in params else None
            if pname is None or (root_name is not None and pname != root_name):
                continue
            root_name = pname
            for key in _render_key(acc[1], owner.get(id(n))):
                sub = tree
                for k in base_path:
                    sub = sub.setdefault(k, {})
                sub.setdefault(key, {})
        if not tree or root_name is None:
            return []

        def _render(t: dict) -> str:
            return "{" + ", ".join(
                f"{k!r}: {_render(v) if v else '...'}" for k, v in t.items()
            ) + "}"

        text = f"{root_name} = {_render(tree)}"
        if len(text) > _CONTRACT_SHAPE_MAX_CHARS:
            text = text[:_CONTRACT_SHAPE_MAX_CHARS] + " …"
        return [indent + "    # input shape: " + text]
    except Exception:  # noqa: BLE001 — additive, never fatal
        return []


def _def_lines(node, indent: str, doc_chars: int) -> List[str]:
    """Signature line for a def, expanded with its contract when one exists:
    ``def f(...) -> T:`` / docstring excerpt / ``# reads:`` access pattern /
    ``# input shape:`` synthesised parameter skeleton /
    ``# where / returns / returns None if`` derived semantics / ``...``.
    Without any of those the legacy one-liner ``def f(...) -> T: ...`` is
    emitted."""
    sig = _sig_line(node)
    doc = _doc_excerpt(node, doc_chars) if doc_chars > 0 else ""
    extra = (
        _access_lines(node, indent) + _input_shape_lines(node, indent)
        + _contract_lines(node, indent)
    )
    if (not doc and not extra) or not sig.endswith(" ..."):
        return [indent + sig]
    lines = [indent + sig[: -len(" ...")]]
    if doc:
        lines.append(indent + '    """' + doc + '"""')
    lines.extend(extra)
    lines.append(indent + "    ...")
    return lines


def _field_lines(cls_node, indent: str) -> List[str]:
    """Public annotated class-body fields (``name: T``) — the data contract
    of dataclasses / attrs / plain annotated classes. NEVER raises."""
    out: List[str] = []
    try:
        for item in cls_node.body:
            if not isinstance(item, ast.AnnAssign) or not isinstance(item.target, ast.Name):
                continue
            if not _is_public(item.target.id):
                continue
            try:
                ann = ast.unparse(item.annotation)
            except Exception:  # noqa: BLE001
                ann = "Any"
            out.append(f"{indent}{item.target.id}: {ann}")
    except Exception:  # noqa: BLE001 — partial extraction beats a crash
        pass
    return out


def _public_doc_nodes(tree) -> List[object]:
    """Module, public top-level defs, public classes and their public /
    __init__ methods — every node whose docstring the anchor may carry."""
    nodes: List[object] = [tree]
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _is_public(node.name):
                nodes.append(node)
        elif isinstance(node, ast.ClassDef) and _is_public(node.name):
            nodes.append(node)
            nodes.extend(
                m for m in node.body
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                and (_is_public(m.name) or m.name == "__init__")
            )
    return nodes


def _waterfill_level(lengths: Sequence[int], budget: int, ceiling: int) -> int:
    """Highest per-item cap such that ``sum(min(len, cap)) <= budget``: short
    docstrings stay whole and their unused share flows to the long ones.
    Returns ``ceiling`` when everything fits."""
    remaining = max(0, budget)
    level = ceiling
    pending = sorted(int(x) for x in lengths if x > 0)
    for i, ln in enumerate(pending):
        share = remaining // (len(pending) - i)
        take = min(ln, share, ceiling)
        if ln > share or ln > ceiling:
            level = min(level, share, ceiling)
        remaining -= take
    return max(0, level)


def _adaptive_doc_chars(tree, budget: Optional[int], skeleton_len: int = 0) -> int:
    """Per-symbol excerpt cap: the env floor when no budget is known;
    otherwise the water-fill level of every public docstring inside
    ``budget - skeleton_len`` (the chars left after signatures), floored at
    the env floor and capped at the env ceiling."""
    floor = _int_env(_ENV_DOC_CHARS, _DEFAULT_DOC_CHARS)
    if budget is None or budget <= 0:
        return floor
    ceiling = max(floor, _int_env(_ENV_DOC_CHARS_MAX, _DEFAULT_DOC_CHARS_MAX))
    lengths: List[int] = []
    for node in _public_doc_nodes(tree):
        try:
            doc = ast.get_docstring(node, clean=True) or ""
        except Exception:  # noqa: BLE001
            doc = ""
        if doc:
            # +12: the quotes / indent / newline each embedded excerpt costs.
            lengths.append(len(" ".join(doc.split())) + 12)
    if not lengths:
        return floor
    level = _waterfill_level(lengths, budget - skeleton_len, ceiling)
    return max(floor, min(ceiling, level))


def extract_public_api(
    source: str,
    module_import_path: str = "",
    doc_chars: Optional[int] = None,
    budget: Optional[int] = None,
) -> str:
    """Compact authoritative block for the PUBLIC top-level API of *source* —
    public functions and public classes (annotated fields, public methods +
    __init__), each carrying a bounded docstring excerpt: the semantic
    contract (accepted input shape, return semantics) that a bare signature
    cannot express. ``doc_chars`` bounds each excerpt (``None`` → env
    ``JARVIS_AST_SIGNATURE_ANCHOR_DOC_CHARS``, default 420, raised adaptively
    toward ``budget / public symbols`` when a char ``budget`` is given, capped
    by ``JARVIS_AST_SIGNATURE_ANCHOR_DOC_CHARS_MAX``). ``""`` when there
    is no extractable public API or the source cannot be parsed. NEVER
    raises."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return ""
    if doc_chars is None:
        skeleton = extract_public_api(source, module_import_path, doc_chars=0)
        doc_chars = _adaptive_doc_chars(tree, budget, len(skeleton))
    lines: List[str] = []
    try:
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if _is_public(node.name):
                    lines.extend(_def_lines(node, "", doc_chars))
            elif isinstance(node, ast.ClassDef):
                if not _is_public(node.name):
                    continue
                try:
                    bases = ", ".join(ast.unparse(b) for b in node.bases)
                except Exception:  # noqa: BLE001
                    bases = ""
                header = f"class {node.name}" + (f"({bases})" if bases else "") + ":"
                body: List[str] = []
                cls_doc = _doc_excerpt(node, doc_chars) if doc_chars > 0 else ""
                if cls_doc:
                    body.append('    """' + cls_doc + '"""')
                body.extend(_field_lines(node, "    "))
                for m in node.body:
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                        _is_public(m.name) or m.name == "__init__"
                    ):
                        body.extend(_def_lines(m, "    ", doc_chars))
                lines.append(header)
                lines.extend(body if body else ["    ..."])
    except Exception:  # noqa: BLE001 — partial extraction beats a crash
        pass
    if not lines:
        return ""
    head = [f"# {module_import_path or 'module'}"]
    try:
        mod_doc = _doc_excerpt(tree, doc_chars) if doc_chars > 0 else ""
    except Exception:  # noqa: BLE001
        mod_doc = ""
    if mod_doc:
        head.append('"""' + mod_doc + '"""')
    return "\n".join(head + lines)


def _import_label(path: Path, repo_root: Path) -> str:
    try:
        rel = path.resolve().relative_to(Path(repo_root).resolve())
        return str(rel).replace(os.sep, "/")
    except Exception:  # noqa: BLE001
        return path.name


def _module_stem_from_test(rel_path: str) -> Optional[str]:
    name = Path(rel_path).name
    if not name.endswith(".py"):
        return None
    stem = name[:-3]
    if stem.startswith("test_"):
        return stem[len("test_"):]
    if stem.endswith("_test"):
        return stem[: -len("_test")]
    return None


def _norm(p: str) -> str:
    return str(p).replace("\\", "/")


def collect_anchor_sources(
    target_files: Sequence[str],
    description: str,
    repo_root: os.PathLike,
) -> List[Tuple[str, Path]]:
    """Resolve the source modules whose API the candidate must honour, in order:
      1. existing TARGET files that are Python source (modifications must preserve
         the current API);
      2. repo-relative ``*.py`` paths named in the op DESCRIPTION that exist
         (operator/roadmap goals name the module-under-test explicitly);
      3. for a ``test_*.py`` / ``*_test.py`` target, the source module it tests,
         found by a bounded, non-test search under the repo.
    De-duplicated, order-preserving. Bounded. NEVER raises."""
    root = Path(repo_root)
    out: List[Tuple[str, Path]] = []
    seen = set()

    def _add(p: Path) -> None:
        try:
            rp = p.resolve()
        except Exception:  # noqa: BLE001
            return
        key = str(rp)
        if key in seen or not rp.is_file() or rp.suffix != ".py":
            return
        seen.add(key)
        out.append((_import_label(rp, root), rp))

    try:
        # (1) existing target source files
        for tf in target_files or ():
            _add(root / str(tf))
        # (2) .py paths named in the description
        for m in _PY_PATH_RE.findall(description or ""):
            cand = root / m.lstrip("./")
            if cand.is_file():
                _add(cand)
        # (3) module-under-test for test_* targets (bounded search)
        for tf in target_files or ():
            stem = _module_stem_from_test(str(tf))
            if not stem:
                continue
            found: List[Path] = []
            try:
                for p in root.rglob(stem + ".py"):
                    sp = _norm(str(p))
                    nm = p.name
                    # Exclude the worktree tree, any tests/ or test/ DIRECTORY
                    # component, and test FILES themselves — but NOT a source
                    # module that merely lives under a dir whose name contains
                    # 'test_' (e.g. a pytest tmp dir or a 'test_project/' repo).
                    if "/.worktrees/" in sp or "/tests/" in sp or "/test/" in sp:
                        continue
                    if nm.startswith("test_") or nm.endswith("_test.py"):
                        continue
                    found.append(p)
                    if len(found) >= 32:  # hard cap: never walk unbounded
                        break
            except Exception:  # noqa: BLE001
                found = []
            found.sort(key=lambda p: len(str(p)))
            for p in found[:1]:
                _add(p)
    except Exception:  # noqa: BLE001
        pass
    return out


def build_signature_anchor(
    target_files: Sequence[str],
    description: str,
    repo_root: os.PathLike,
) -> str:
    """The prompt block: authoritative public API signatures for the modules the
    candidate must call. Empty string when disabled, nothing resolves, or every
    source is unreadable/API-less — in which case the prompt is byte-identical to
    the pre-anchor legacy. NEVER raises."""
    try:
        if not anchor_enabled():
            return ""
        sources = collect_anchor_sources(target_files, description, repo_root)
        if not sources:
            return ""
        max_modules = _int_env(_ENV_MAX_MODULES, _DEFAULT_MAX_MODULES)
        max_chars = _int_env(_ENV_MAX_CHARS, _DEFAULT_MAX_CHARS)
        blocks: List[str] = []
        used = 0
        for label, path in sources[:max_modules]:
            try:
                src = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            block = extract_public_api(src, label, budget=max_chars - used)
            if not block:
                continue
            if used + len(block) > max_chars:
                block = block[: max(0, max_chars - used)]
            blocks.append(block)
            used += len(block)
            if used >= max_chars:
                break
        if not blocks:
            return ""
        body = "\n\n".join(blocks)
        try:
            logger.info(
                "[SigAnchor] injected %d chars for %d module(s): %s",
                len(body), len(blocks),
                ", ".join(lbl for lbl, _ in sources[:max_modules]),
            )
        except Exception:  # noqa: BLE001
            pass
        return (
            "## AUTHORITATIVE API SIGNATURES (ground truth — use EXACTLY)\n\n"
            "The signatures below are parsed from the real, current source on "
            "disk. Any function/method you call from these modules MUST match "
            "these signatures EXACTLY — the same name, argument names/order/"
            "count, and return shape. Do NOT invent parameters, overloads, or "
            "return types. If a needed capability is absent here, it does not "
            "exist — do not assume it. The `# reads:` / `# where:` / "
            "`# returns:` / `# returns None if:` lines are extracted from the "
            "function bodies: quoted dotted names such as 'general.architecture' "
            "are LITERAL flat keys (never nested sub-dicts), helper calls "
            "compose keys exactly as shown, `# input shape:` is the literal "
            "nesting to reproduce (copy it, fill the `...` leaves), and "
            "returned fields hold exactly the formulas shown — derive every "
            "expected value from them.\n\n"
            "```python\n" + body + "\n```"
        )
    except Exception:  # noqa: BLE001 — the anchor is additive, never fatal
        return ""
