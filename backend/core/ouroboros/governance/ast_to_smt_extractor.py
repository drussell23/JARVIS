"""Slice 130 — deterministic AST→SMT extraction for the recursion-depth bound.

Closes the Slice 129 faithfulness gap: instead of hand-mirroring the gate's
decision structure into the SMT, this reads the LIVE ``recursion_depth_gate.py``
AST, extracts the decision logic, and compiles it into Z3 linear-arithmetic
constraints **dynamically**. If a developer alters the gate, the extractor
re-derives a different formula and the proof re-runs — a loosened bound becomes
``REFUTED`` (the closed loop the dissertation requires).

**Scope (honest).** This is a DETERMINISTIC extractor for the recursion-gate
decision PATTERN::

    effective = <chain_var> + <int_offset>          # the +1 step
    if effective <cmp> <mx_var>[ + <int>]:          # the HALT comparison
        ... RecursionVerdict.HALT ...

It is NOT a general Python→SMT compiler (that is undecidable). Anything it does
not recognize → **FAIL-CLOSED**: ``extract_recursion_gate_logic`` returns
``ok=False`` and ``recursion_bound_spec_from_source`` emits a spec that can never
prove (so a malformed/unexpected source yields a NON-``PROVED`` verdict, never a
false certificate).

The safety property proved is fixed (the *meaning* of the bound): an ALLOWED
governance op never pushes the applied chain depth past ``mx`` (``effective <=
mx``). The HALT predicate that must enforce it is EXTRACTED — so the proof
certifies the live code's predicate, not an abstraction.
"""

from __future__ import annotations

import ast
import dataclasses
from typing import Optional

from backend.core.ouroboros.governance.smt_invariant_prover import SmtSpec

_GATE_FUNC = "evaluate_recursion_gate"
_LINKED = "recursion_depth_gate"

# AST comparison node → SMT-LIB2 operator (linear arithmetic).
_AST_CMP_TO_SMT = {
    ast.Gt: ">",
    ast.GtE: ">=",
    ast.Lt: "<",
    ast.LtE: "<=",
    ast.Eq: "=",
}


@dataclasses.dataclass(frozen=True)
class ExtractedGateLogic:
    """The recursion-gate decision parameters extracted from the live AST.

    HALT iff ``(chain + lhs_offset) <comparator> (mx + rhs_offset)``."""

    ok: bool
    lhs_offset: int = 0
    comparator: str = ""
    rhs_offset: int = 0
    clamp_lo: int = 0
    clamp_hi: int = 0
    error: str = ""


def _module_int_const(tree: ast.Module, name: str) -> Optional[int]:
    """Find a module-level ``name = <int>`` assignment. None if absent."""
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if (
                    isinstance(tgt, ast.Name) and tgt.id == name
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, int)
                ):
                    return int(node.value.value)
    return None


def _name_plus_int(node: ast.AST) -> Optional[tuple]:
    """If ``node`` is ``Name + <int>`` (or just ``Name``), return (name, offset).
    None otherwise."""
    if isinstance(node, ast.Name):
        return (node.id, 0)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        if (
            isinstance(node.left, ast.Name)
            and isinstance(node.right, ast.Constant)
            and isinstance(node.right.value, int)
        ):
            return (node.left.id, int(node.right.value))
        if (
            isinstance(node.right, ast.Name)
            and isinstance(node.left, ast.Constant)
            and isinstance(node.left.value, int)
        ):
            return (node.right.id, int(node.left.value))
    return None


def _body_has_halt(body) -> bool:
    """True if an ``if`` body references the HALT verdict (Attribute ``.HALT``
    or a bare ``HALT`` name)."""
    for n in ast.walk(ast.Module(body=list(body), type_ignores=[])):
        if isinstance(n, ast.Attribute) and n.attr == "HALT":
            return True
        if isinstance(n, ast.Name) and n.id == "HALT":
            return True
    return False


def extract_recursion_gate_logic(source: str) -> ExtractedGateLogic:
    """Parse ``source`` and extract the recursion-gate decision pattern.
    FAIL-CLOSED: any deviation from the recognized shape → ``ok=False``.
    NEVER raises."""
    def _fail(msg: str) -> ExtractedGateLogic:
        return ExtractedGateLogic(ok=False, error=msg)

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return _fail(f"syntax_error: {exc}")

    clamp_lo = _module_int_const(tree, "_MIN_MAX_DEPTH")
    clamp_hi = _module_int_const(tree, "_MAX_MAX_DEPTH")
    if clamp_lo is None or clamp_hi is None:
        return _fail("clamp constants _MIN_MAX_DEPTH/_MAX_MAX_DEPTH not found")

    func = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == _GATE_FUNC:
            func = node
            break
    if func is None:
        return _fail(f"function {_GATE_FUNC} not found")

    # 1. effective = <chain_var> + <int_offset>
    effective_var = None
    lhs_offset = None
    for n in ast.walk(func):
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(
            n.targets[0], ast.Name
        ):
            np = _name_plus_int(n.value)
            if np is not None and n.targets[0].id == "effective":
                effective_var = n.targets[0].id
                lhs_offset = np[1]
                break
    if effective_var is None or lhs_offset is None:
        return _fail("could not extract 'effective = chain + <int>'")

    # 2. mx var (assigned from max_recursion_depth())
    mx_var = None
    for n in ast.walk(func):
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(
            n.targets[0], ast.Name
        ) and isinstance(n.value, ast.Call) and isinstance(
            n.value.func, ast.Name
        ) and n.value.func.id == "max_recursion_depth":
            mx_var = n.targets[0].id
            break
    if mx_var is None:
        return _fail("could not locate mx = max_recursion_depth()")

    # 3. if effective <cmp> (mx [+ int]):  with HALT in the body
    comparator = None
    rhs_offset = None
    for n in ast.walk(func):
        if not isinstance(n, ast.If):
            continue
        test = n.test
        if not isinstance(test, ast.Compare) or len(test.ops) != 1:
            continue
        if not (isinstance(test.left, ast.Name) and test.left.id == effective_var):
            continue
        rhs = _name_plus_int(test.comparators[0])
        if rhs is None or rhs[0] != mx_var:
            continue
        smt_op = _AST_CMP_TO_SMT.get(type(test.ops[0]))
        if smt_op is None:
            return _fail(f"unsupported comparator {type(test.ops[0]).__name__}")
        if not _body_has_halt(n.body):
            continue
        comparator = smt_op
        rhs_offset = rhs[1]
        break
    if comparator is None or rhs_offset is None:
        return _fail("could not extract the HALT comparison")

    return ExtractedGateLogic(
        ok=True, lhs_offset=lhs_offset, comparator=comparator,
        rhs_offset=rhs_offset, clamp_lo=clamp_lo, clamp_hi=clamp_hi,
    )


def compile_recursion_bound_smt(logic: ExtractedGateLogic) -> str:
    """Compile extracted logic into the SMT-LIB2 inductive-safety negation
    (unsat = PROVED). Raises ``ValueError`` on not-``ok`` logic (callers that
    want fail-closed use ``recursion_bound_spec_from_source``)."""
    if not logic.ok:
        raise ValueError(f"cannot compile not-ok logic: {logic.error}")
    eff = f"(+ before {logic.lhs_offset})"
    rhs = f"(+ mx {logic.rhs_offset})"
    halt = f"({logic.comparator} {eff} {rhs})"   # extracted HALT predicate
    return (
        ";; AST-extracted recursion bound — negation of inductive safety "
        "(unsat=PROVED)\n"
        ";; HALT iff (chain + lhs_offset) <cmp> (mx + rhs_offset); allow=not HALT;\n"
        ";; safety target: an ALLOWED op keeps effective <= mx.\n"
        "(declare-const before Int)\n"
        "(declare-const mx Int)\n"
        f"(assert (>= mx {logic.clamp_lo}))\n"
        f"(assert (<= mx {logic.clamp_hi}))\n"
        "(assert (>= before 0))\n"
        f"(assert (not {halt}))\n"          # the op is ALLOWED (HALT false)
        f"(assert (> {eff} mx))\n"          # ...yet the applied depth exceeds mx
        "(check-sat)\n"
    )


def recursion_bound_spec_from_source(source: Optional[str] = None) -> SmtSpec:
    """Build the SmtSpec from the LIVE recursion_depth_gate source (or a supplied
    ``source`` for drift tests). FAIL-CLOSED: if extraction fails, return a spec
    that is satisfiable (→ a NON-PROVED verdict) so an unrecognized source never
    yields a false proof certificate. NEVER raises."""
    if source is None:
        try:
            import pathlib
            source = pathlib.Path(
                "backend/core/ouroboros/governance/recursion_depth_gate.py"
            ).read_text()
        except Exception as exc:  # noqa: BLE001
            source = ""
            _read_err = str(exc)
        else:
            _read_err = ""
    else:
        _read_err = ""

    logic = extract_recursion_gate_logic(source or "")
    if not logic.ok:
        # Fail-closed: an always-satisfiable formula → REFUTED/non-PROVED.
        return SmtSpec(
            name="rrd_recursion_bound_ast_extraction_failed",
            smt2="(declare-const fail Bool)\n(assert fail)\n(check-sat)\n",
            description=(
                f"EXTRACTION_FAILED (fail-closed, never PROVED): "
                f"{logic.error or _read_err}"
            ),
            linked_invariant_name=_LINKED,
            timeout_ms=10000,
        )

    return SmtSpec(
        name="rrd_recursion_bound_ast_extracted",
        smt2=compile_recursion_bound_smt(logic),
        description=(
            f"AST-extracted RRD bound: HALT iff (chain+{logic.lhs_offset}) "
            f"{logic.comparator} (mx+{logic.rhs_offset}); clamp "
            f"[{logic.clamp_lo},{logic.clamp_hi}]; allowed op never exceeds mx."
        ),
        linked_invariant_name=_LINKED,
        timeout_ms=10000,
    )


__all__ = [
    "ExtractedGateLogic",
    "extract_recursion_gate_logic",
    "compile_recursion_bound_smt",
    "recursion_bound_spec_from_source",
]
