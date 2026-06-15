"""Tier 0 — Anticipatory Edge-Case Armor: blindspot detectors for SemanticGuardian.

These are the static half of the "Founding-Engineer prescience" upgrade: deterministic,
AST-based, zero-LLM detectors that flag the bug classes that bite at runtime AFTER
`ast.parse` is happy — the exact gap the LiveKernelValidator exists to catch. They are
registered into :mod:`semantic_guardian` and obey its contract + kill switches.

Five detectors:
  * ``frozen_dataclass_mutation``  (hard) — attribute set on a ``@dataclass(frozen=True)``
  * ``namedtuple_attr_assignment`` (hard) — attribute set on a ``typing.NamedTuple``
  * ``pydantic_immutable_set``     (hard) — attribute set on a frozen pydantic model
  * ``type_coercion_blindspot``    (soft) — deref/coerce an ``Optional[...]`` without a guard
  * ``loop_var_rebind_lost``       (soft) — rebind a loop var whose new value is never read

HONEST SCOPE (the candor that keeps this real):
  * Immutability detection is exact for classes defined IN the candidate file, and
    best-effort across files via a *verified* + env-extensible known-frozen-types registry
    (``JARVIS_SEMGUARD_KNOWN_FROZEN_TYPES``). Type-of-name inference is intra-procedural and
    conservative (arg/AnnAssign annotations, direct constructor calls, ``dataclasses.replace``
    propagation). It does not do whole-program type resolution — it favors low false positives.
  * ``type_coercion_blindspot`` is flow-sensitive but RUDIMENTARY: it tracks None-narrowing
    through ``if x is not None:`` / ``if x:`` blocks, early-exit ``if x is None: return/raise``
    guards, ``assert x is not None``, and ``x is not None and <use>`` short-circuits. It is not
    a type checker (no narrowing via helpers, walrus, complex boolean trees) — on any doubt it
    stays silent rather than cry wolf at the Iron Gate.

Each public detector matches the SemanticGuardian detector contract:
    detector(*, file_path, old_content, new_content) -> Optional[Detection]
and returns at most one Detection (aggregating line numbers), or ``None``.
"""
from __future__ import annotations

import ast
import os
from typing import Dict, List, Optional, Set, Tuple

# Detection is imported lazily (inside _make) to avoid a circular import with
# semantic_guardian, which imports these detectors at the bottom of its module.

# Verified-frozen project types (confirmed @dataclass(frozen=True) at read time). The
# orchestrator mutates op_context.ValidationResult cross-file — the canonical case this
# armor exists to catch — so it MUST be seeded here, not discovered in-file.
_SEEDED_KNOWN_FROZEN: Tuple[str, ...] = (
    "ValidationResult",
    "OperationContext",
    "ApprovalDecision",
    "GenerationResult",
    "UserMemory",
    "Detection",
)

_COERCERS: frozenset = frozenset(
    {"int", "float", "len", "str", "bool", "list", "dict", "set", "tuple", "sorted", "sum"}
)
_TERMINALS = (ast.Return, ast.Raise, ast.Continue, ast.Break)


# ---------------------------------------------------------------------------
# Result + parse + Detection adapter
# ---------------------------------------------------------------------------


class _Hit:
    """Internal pre-Detection result (severity/message/lines/snippet)."""

    __slots__ = ("severity", "message", "lines", "snippet")

    def __init__(self, severity: str, message: str, lines: Tuple[int, ...], snippet: str = ""):
        self.severity = severity
        self.message = message
        self.lines = lines
        self.snippet = snippet


def _make(pattern: str, file_path: str, hit: Optional[_Hit]):
    """Adapt an internal _Hit to a semantic_guardian.Detection (lazy import)."""
    if hit is None:
        return None
    from backend.core.ouroboros.governance.semantic_guardian import Detection

    return Detection(
        pattern=pattern,
        severity=hit.severity,
        message=hit.message,
        file_path=file_path,
        lines=hit.lines,
        snippet=hit.snippet,
    )


def _parse(src: str) -> Optional[ast.Module]:
    try:
        return ast.parse(src or "")
    except (SyntaxError, ValueError):
        return None


def _known_frozen_types() -> Set[str]:
    extra = os.environ.get("JARVIS_SEMGUARD_KNOWN_FROZEN_TYPES", "")
    names = set(_SEEDED_KNOWN_FROZEN)
    for tok in extra.replace(",", " ").split():
        tok = tok.strip()
        if tok:
            names.add(tok)
    return names


# ---------------------------------------------------------------------------
# Immutability classification
# ---------------------------------------------------------------------------


def _decorator_is_frozen_dataclass(dec: ast.expr) -> bool:
    """True for @dataclass(frozen=True) (with or without other args)."""
    if not isinstance(dec, ast.Call):
        return False
    fn = dec.func
    name = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else "")
    if name != "dataclass":
        return False
    for kw in dec.keywords:
        if kw.arg == "frozen" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
            return True
    return False


def _base_names(cls: ast.ClassDef) -> Set[str]:
    out: Set[str] = set()
    for b in cls.bases:
        if isinstance(b, ast.Name):
            out.add(b.id)
        elif isinstance(b, ast.Attribute):
            out.add(b.attr)
    return out


def _pydantic_is_frozen(cls: ast.ClassDef) -> bool:
    """Detect a frozen pydantic model: model_config=ConfigDict(frozen=True) (v2) or a
    nested ``class Config`` with frozen=True / allow_mutation=False (v1)."""
    for node in cls.body:
        # v2: model_config = ConfigDict(frozen=True) / model_config = {"frozen": True}
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "model_config":
                    v = node.value
                    if isinstance(v, ast.Call):
                        for kw in v.keywords:
                            if kw.arg == "frozen" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                                return True
                    if isinstance(v, ast.Dict):
                        for k, val in zip(v.keys, v.values):
                            if (isinstance(k, ast.Constant) and k.value == "frozen"
                                    and isinstance(val, ast.Constant) and val.value is True):
                                return True
        # v1: class Config: frozen = True  /  allow_mutation = False
        if isinstance(node, ast.ClassDef) and node.name == "Config":
            for sub in node.body:
                if isinstance(sub, ast.Assign):
                    for tgt in sub.targets:
                        if not isinstance(tgt, ast.Name):
                            continue
                        if (tgt.id == "frozen" and isinstance(sub.value, ast.Constant)
                                and sub.value.value is True):
                            return True
                        if (tgt.id == "allow_mutation" and isinstance(sub.value, ast.Constant)
                                and sub.value.value is False):
                            return True
    return False


def _classify_module_classes(module: ast.Module) -> Dict[str, str]:
    """Map class-name -> immutability kind for classes defined in this module.
    kind ∈ {"frozen_dataclass", "namedtuple", "pydantic_frozen"}."""
    kinds: Dict[str, str] = {}
    for node in ast.walk(module):
        if not isinstance(node, ast.ClassDef):
            continue
        if any(_decorator_is_frozen_dataclass(d) for d in node.decorator_list):
            kinds[node.name] = "frozen_dataclass"
            continue
        bases = _base_names(node)
        if "NamedTuple" in bases:
            kinds[node.name] = "namedtuple"
            continue
        if "BaseModel" in bases and _pydantic_is_frozen(node):
            kinds[node.name] = "pydantic_frozen"
    return kinds


def _annotation_type_name(ann: Optional[ast.expr]) -> Optional[str]:
    if isinstance(ann, ast.Name):
        return ann.id
    if isinstance(ann, ast.Attribute):
        return ann.attr
    if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
        return ann.value  # string forward-ref
    return None


def _infer_name_types(func: ast.AST) -> Dict[str, str]:
    """Intra-procedural name -> class-name inference. Conservative: arg annotations,
    AnnAssign annotations, direct constructor calls, and dataclasses.replace propagation.
    Last write wins; a rebind to an unknown shape clears the name."""
    types: Dict[str, str] = {}

    if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
        a = func.args
        for arg in list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs):
            t = _annotation_type_name(arg.annotation)
            if t:
                types[arg.arg] = t

    for node in ast.walk(func):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            t = _annotation_type_name(node.annotation)
            if t:
                types[node.target.id] = t
        elif isinstance(node, ast.Assign):
            tnames = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if not tnames:
                continue
            inferred = _infer_value_type(node.value, types)
            for nm in tnames:
                if inferred:
                    types[nm] = inferred
                else:
                    types.pop(nm, None)  # rebind to unknown shape — stop tracking
    return types


def _infer_value_type(value: ast.expr, known: Dict[str, str]) -> Optional[str]:
    """ClassName(...) -> ClassName; dataclasses.replace(x, ...) -> type-of-x."""
    if isinstance(value, ast.Call):
        fn = value.func
        if isinstance(fn, ast.Name):
            if fn.id == "replace" and value.args:
                first = value.args[0]
                return known.get(first.id) if isinstance(first, ast.Name) else None
            if fn.id and fn.id[:1].isupper():
                return fn.id
        elif isinstance(fn, ast.Attribute):
            if fn.attr == "replace" and value.args:  # dataclasses.replace(x, ...)
                first = value.args[0]
                return known.get(first.id) if isinstance(first, ast.Name) else None
            if fn.attr and fn.attr[:1].isupper():
                return fn.attr
    return None


def _immutable_attr_violations(module: ast.Module, want_kind: str) -> List[int]:
    """Line numbers of `x.attr = ...` / `x.attr op= ...` where x's inferred type is an
    immutable class of ``want_kind`` (in-file class OR known-frozen registry → frozen_dataclass)."""
    local_kinds = _classify_module_classes(module)
    known_frozen = _known_frozen_types()
    lines: List[int] = []

    # module-level code + every function/method scope (each has its own name types)
    scopes: List[ast.AST] = [module]
    scopes += [n for n in ast.walk(module) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]

    seen: Set[int] = set()
    for scope in scopes:
        types = _infer_name_types(scope)
        for node in ast.walk(scope):
            if isinstance(node, (ast.Assign,)):
                tgts = node.targets
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                tgts = [node.target]
            else:
                continue
            for tgt in tgts:
                if not (isinstance(tgt, ast.Attribute) and isinstance(tgt.value, ast.Name)):
                    continue
                tname = types.get(tgt.value.id)
                if not tname:
                    continue
                kind = local_kinds.get(tname)
                if kind is None and tname in known_frozen:
                    kind = "frozen_dataclass"
                if kind == want_kind and node.lineno not in seen:
                    seen.add(node.lineno)
                    lines.append(node.lineno)
    return sorted(lines)


def _immutable_detector(want_kind: str, label: str, new_content: str) -> Optional[_Hit]:
    module = _parse(new_content)
    if module is None:
        return None
    lines = _immutable_attr_violations(module, want_kind)
    if not lines:
        return None
    return _Hit(
        severity="hard",
        message=(f"attribute assignment to a {label} instance — this raises at runtime "
                 f"(use dataclasses.replace / _replace / model_copy to produce a new value)"),
        lines=tuple(lines),
    )


# ---------------------------------------------------------------------------
# Optional-guard flow analysis (type_coercion_blindspot)
# ---------------------------------------------------------------------------


def _is_optional_annotation(ann: Optional[ast.expr]) -> bool:
    """True for Optional[...] or X | None (or None | X)."""
    if ann is None:
        return False
    if isinstance(ann, ast.Subscript):
        base = ann.value
        nm = base.id if isinstance(base, ast.Name) else (base.attr if isinstance(base, ast.Attribute) else "")
        return nm == "Optional"
    if isinstance(ann, ast.BinOp) and isinstance(ann.op, ast.BitOr):
        for side in (ann.left, ann.right):
            if isinstance(side, ast.Constant) and side.value is None:
                return True
    if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
        s = ann.value.replace(" ", "")
        return s.startswith("Optional[") or s.endswith("|None") or s.startswith("None|")
    return False


def _narrowed_name(test: ast.expr) -> Optional[Tuple[str, bool]]:
    """Return (name, narrows_true) where narrows_true means the THEN branch makes name
    non-None. `x is not None` / `x` -> (x, True); `x is None` -> (x, False)."""
    if isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.left, ast.Name):
        op = test.ops[0]
        comp = test.comparators[0]
        if isinstance(comp, ast.Constant) and comp.value is None:
            if isinstance(op, ast.IsNot):
                return (test.left.id, True)
            if isinstance(op, ast.Is):
                return (test.left.id, False)
    if isinstance(test, ast.Name):
        return (test.id, True)  # `if x:` narrows x truthy in the body
    return None


def _block_terminates(stmts: List[ast.stmt]) -> bool:
    return bool(stmts) and isinstance(stmts[-1], _TERMINALS)


class _CoercionScan:
    """Collects unguarded deref/coercion lines. Recurses through expressions honoring
    `and` short-circuit narrowing (in `x is not None and x.f`, x is safe for the right
    operand) and consults the CURRENT tracked set (so a reassigned name stops tracking)."""

    def __init__(self) -> None:
        self.lines: Set[int] = set()

    def check_expr(self, node: Optional[ast.expr], tracked: Set[str], guarded: Set[str]) -> None:
        if node is None:
            return
        self._visit(node, tracked, set(guarded))

    def _visit(self, node: ast.AST, tracked: Set[str], guarded: Set[str]) -> None:
        if isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                g = set(guarded)
                for v in node.values:
                    self._visit(v, tracked, g)
                    nn = _narrowed_name(v)
                    if nn and nn[1]:
                        g.add(nn[0])          # left operand narrows the rest of the `and`
            else:                              # Or: no fact carries between operands
                for v in node.values:
                    self._visit(v, tracked, guarded)
            return
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            self._flag(node.value.id, node.lineno, tracked, guarded)
        elif isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
            self._flag(node.value.id, node.lineno, tracked, guarded)
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
              and node.func.id in _COERCERS):
            for a in node.args:
                if isinstance(a, ast.Name):
                    self._flag(a.id, node.lineno, tracked, guarded)
        for child in ast.iter_child_nodes(node):
            self._visit(child, tracked, guarded)

    def _flag(self, name: str, lineno: int, tracked: Set[str], guarded: Set[str]) -> None:
        if name in tracked and name not in guarded:
            self.lines.add(lineno)


def _process_block(stmts: List[ast.stmt], tracked: Set[str], guarded: Set[str],
                   scan: _CoercionScan) -> None:
    """Walk a statement list sequentially, threading None-narrowing facts forward."""
    guarded = set(guarded)
    tracked = set(tracked)
    for stmt in stmts:
        if isinstance(stmt, ast.If):
            scan.check_expr(stmt.test, tracked, guarded)
            nn = _narrowed_name(stmt.test)
            then_guard, else_guard = set(guarded), set(guarded)
            if nn:
                name, narrows_true = nn
                (then_guard if narrows_true else else_guard).add(name)
            _process_block(stmt.body, tracked, then_guard, scan)
            _process_block(stmt.orelse, tracked, else_guard, scan)
            # early-exit narrowing: rest of THIS block learns the surviving fact
            if nn:
                name, narrows_true = nn
                if not narrows_true and _block_terminates(stmt.body):
                    guarded.add(name)          # `if x is None: return` -> x safe after
                elif narrows_true and _block_terminates(stmt.orelse):
                    guarded.add(name)          # `if x is not None: ... else: return`
        elif isinstance(stmt, ast.Assert):
            scan.check_expr(stmt.test, tracked, guarded)
            nn = _narrowed_name(stmt.test)
            if nn and nn[1]:
                guarded.add(nn[0])
        elif isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            # check the RHS / value first under current facts
            val = getattr(stmt, "value", None)
            scan.check_expr(val, tracked, guarded)
            # a rebind of a tracked name clears its tracking + guard for the rest
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            for t in targets:
                if isinstance(t, ast.Name):
                    tracked.discard(t.id)
                    guarded.discard(t.id)
                else:
                    scan.check_expr(t, tracked, guarded)  # x.attr / x[i] target is itself a deref
        elif isinstance(stmt, (ast.For, ast.AsyncFor)):
            scan.check_expr(stmt.iter, tracked, guarded)
            _process_block(stmt.body, tracked, guarded, scan)
            _process_block(stmt.orelse, tracked, guarded, scan)
        elif isinstance(stmt, ast.While):
            scan.check_expr(stmt.test, tracked, guarded)
            nn = _narrowed_name(stmt.test)
            body_guard = set(guarded)
            if nn and nn[1]:
                body_guard.add(nn[0])
            _process_block(stmt.body, tracked, body_guard, scan)
            _process_block(stmt.orelse, tracked, guarded, scan)
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            for item in stmt.items:
                scan.check_expr(item.context_expr, tracked, guarded)
            _process_block(stmt.body, tracked, guarded, scan)
        elif isinstance(stmt, ast.Try):
            _process_block(stmt.body, tracked, guarded, scan)
            for h in stmt.handlers:
                _process_block(h.body, tracked, guarded, scan)
            _process_block(stmt.orelse, tracked, guarded, scan)
            _process_block(stmt.finalbody, tracked, guarded, scan)
        elif isinstance(stmt, (ast.Return, ast.Expr, ast.Raise)):
            scan.check_expr(getattr(stmt, "value", None) or getattr(stmt, "exc", None), tracked, guarded)
        # nested function/class defs: handled separately as their own scopes


def _optional_names_for(func: ast.AST) -> Set[str]:
    names: Set[str] = set()
    if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
        a = func.args
        for arg in list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs):
            if _is_optional_annotation(arg.annotation):
                names.add(arg.arg)
    for node in ast.walk(func):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if _is_optional_annotation(node.annotation):
                names.add(node.target.id)
    return names


def _coercion_blindspot(new_content: str) -> Optional[_Hit]:
    module = _parse(new_content)
    if module is None:
        return None
    all_lines: Set[int] = set()
    for func in ast.walk(module):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        tracked = _optional_names_for(func)
        if not tracked:
            continue
        scan = _CoercionScan()
        _process_block(func.body, tracked, set(), scan)
        all_lines |= scan.lines
    if not all_lines:
        return None
    return _Hit(
        severity="soft",
        message=("Optional-typed value dereferenced/coerced without a None-guard — "
                 "guard with `if x is not None:` (or narrow before use) to avoid a "
                 "runtime AttributeError/TypeError"),
        lines=tuple(sorted(all_lines)),
    )


# ---------------------------------------------------------------------------
# loop_var_rebind_lost
# ---------------------------------------------------------------------------


def _loop_var_rebind_lost(new_content: str) -> Optional[_Hit]:
    """Flag `for v in ...: ... v = <expr>` where v is reassigned but the new value is never
    read afterward in the loop body — a classic no-op 'fix' (the rebind is discarded next
    iteration). Conservative: only the simple single-target case, value-not-read-after."""
    module = _parse(new_content)
    if module is None:
        return None
    lines: List[int] = []
    for loop in ast.walk(module):
        if not isinstance(loop, (ast.For, ast.AsyncFor)) or not isinstance(loop.target, ast.Name):
            continue
        var = loop.target.id
        body = loop.body
        for idx, stmt in enumerate(body):
            if not isinstance(stmt, ast.Assign):
                continue
            if not (len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name)
                    and stmt.targets[0].id == var):
                continue
            # is `var` read anywhere AFTER this statement in the remaining body?
            read_after = False
            for later in body[idx + 1:]:
                for n in ast.walk(later):
                    if isinstance(n, ast.Name) and n.id == var and isinstance(n.ctx, ast.Load):
                        read_after = True
                        break
                if read_after:
                    break
            if not read_after:
                lines.append(stmt.lineno)
    if not lines:
        return None
    return _Hit(
        severity="soft",
        message=("loop variable reassigned but the new value is never read before the next "
                 "iteration — the rebind is discarded (likely a no-op fix; mutate a "
                 "collection or accumulate instead)"),
        lines=tuple(sorted(set(lines))),
    )


# ---------------------------------------------------------------------------
# Public detectors (SemanticGuardian contract)
# ---------------------------------------------------------------------------


def pat_frozen_dataclass_mutation(*, file_path: str, old_content: str, new_content: str):
    return _make("frozen_dataclass_mutation", file_path,
                 _immutable_detector("frozen_dataclass", "frozen dataclass", new_content))


def pat_namedtuple_attr_assignment(*, file_path: str, old_content: str, new_content: str):
    return _make("namedtuple_attr_assignment", file_path,
                 _immutable_detector("namedtuple", "NamedTuple", new_content))


def pat_pydantic_immutable_set(*, file_path: str, old_content: str, new_content: str):
    return _make("pydantic_immutable_set", file_path,
                 _immutable_detector("pydantic_frozen", "frozen pydantic model", new_content))


def pat_type_coercion_blindspot(*, file_path: str, old_content: str, new_content: str):
    return _make("type_coercion_blindspot", file_path, _coercion_blindspot(new_content))


def pat_loop_var_rebind_lost(*, file_path: str, old_content: str, new_content: str):
    return _make("loop_var_rebind_lost", file_path, _loop_var_rebind_lost(new_content))


# Registry export consumed by semantic_guardian (name -> detector).
BLINDSPOT_PATTERNS: Dict[str, object] = {
    "frozen_dataclass_mutation": pat_frozen_dataclass_mutation,
    "namedtuple_attr_assignment": pat_namedtuple_attr_assignment,
    "pydantic_immutable_set": pat_pydantic_immutable_set,
    "type_coercion_blindspot": pat_type_coercion_blindspot,
    "loop_var_rebind_lost": pat_loop_var_rebind_lost,
}
