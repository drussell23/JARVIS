"""Slice 129 — SMT encodings of JARVIS safety rules (the formal theorems).

The first machine-checked invariant: the **RRD Recursion-Depth Bound**
(``recursion_depth_gate``). The decision rule, mirrored from
``evaluate_recursion_gate``::

    effective = chain_depth_before + 1
    verdict   = HALT if effective > mx else ALLOWED     # mx = max_recursion_depth()

**Theorem (inductive safety / un-escapability):** for ANY ceiling ``mx`` in the
gate's clamp range ``[_MIN_MAX_DEPTH, _MAX_MAX_DEPTH]``, no governance op the
gate ALLOWS can push the applied chain depth past ``mx``. Formally the negation
is UNSAT::

    ∃ depth, mx :  _MIN ≤ mx ≤ _MAX
               ∧  0 ≤ depth ≤ mx                (invariant holds before)
               ∧  (depth + 1) ≤ mx              (the gate ALLOWS the op)
               ∧  (depth + 1) > mx              (post-state breaks the invariant)

No such state exists → Z3 ``unsat`` → PROVED. The parameters (clamp bounds, the
live ceiling) are extracted from the actual gate module — if they drift the
proof tracks them. The decision STRUCTURE (offset ``+1``, comparator ``>``) is
mirrored from the code and anchored by a bytes-pin test
(``test_gate_structure_pin``); full verified-extraction from the AST is future
work and is NOT claimed here.

This is **additive** — it does not replace the deterministic
``recursion_depth_gate`` runtime check; it certifies that check's logic is
sound and writes a tamper-evident certificate to the ``BlueEvidenceLedger``.
"""

from __future__ import annotations

from backend.core.ouroboros.governance.recursion_depth_gate import (
    _MAX_MAX_DEPTH,
    _MIN_MAX_DEPTH,
    max_recursion_depth,
)
from backend.core.ouroboros.governance.smt_invariant_prover import SmtSpec

_LINKED = "recursion_depth_gate"


def recursion_bound_spec() -> SmtSpec:
    """Canonical recursion-bound theorem spec.

    **Slice 130**: this now DELEGATES to the deterministic AST extractor
    (``ast_to_smt_extractor.recursion_bound_spec_from_source``) — the SMT is
    compiled from the LIVE ``recursion_depth_gate.py`` AST, NOT a hand-written
    formula. The live ceiling is appended to the description for the certificate.
    Fail-closed: if extraction fails the delegate returns a non-provable spec.
    """
    from backend.core.ouroboros.governance.ast_to_smt_extractor import (
        recursion_bound_spec_from_source,
    )
    spec = recursion_bound_spec_from_source(None)  # None → live source
    # Stamp the live ceiling into the description (certificate continuity).
    return SmtSpec(
        name=spec.name,
        smt2=spec.smt2,
        description=f"{spec.description} (live mx={int(max_recursion_depth())})",
        linked_invariant_name=spec.linked_invariant_name,
        timeout_ms=spec.timeout_ms,
    )


def _loosened_recursion_bound_spec_for_test() -> SmtSpec:
    """NEGATIVE CONTROL (tests only): an off-by-one loosened allow-condition
    (``effective <= mx + 1``) that DOES admit a bound violation, so Z3 returns
    ``sat`` → REFUTED. Proves the prover genuinely discriminates — the real
    theorem's PROVED is not vacuous."""
    lo = int(_MIN_MAX_DEPTH)
    hi = int(_MAX_MAX_DEPTH)
    smt2 = (
        ";; NEGATIVE CONTROL — loosened bound, expect sat=REFUTED\n"
        "(declare-const depth Int)\n"
        "(declare-const mx Int)\n"
        f"(assert (>= mx {lo}))\n"
        f"(assert (<= mx {hi}))\n"
        "(assert (>= depth 0))\n"
        "(assert (<= depth mx))\n"
        "(assert (<= (+ depth 1) (+ mx 1)))\n"   # LOOSENED allow (off-by-one)
        "(assert (> (+ depth 1) mx))\n"
        "(check-sat)\n"
    )
    return SmtSpec(
        name="rrd_recursion_bound_loosened_control",
        smt2=smt2,
        description="negative control: loosened bound must be REFUTED",
        linked_invariant_name=_LINKED,
        timeout_ms=10000,
    )


__all__ = ["recursion_bound_spec"]
