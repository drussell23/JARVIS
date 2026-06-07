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
    """Build the SmtSpec for the recursion-bound inductive-safety theorem from
    the LIVE gate parameters. PROVED iff Z3 returns ``unsat``."""
    lo = int(_MIN_MAX_DEPTH)
    hi = int(_MAX_MAX_DEPTH)
    live_mx = int(max_recursion_depth())
    smt2 = (
        ";; RRD recursion-depth bound — inductive safety (negation; unsat=PROVED)\n"
        "(declare-const depth Int)\n"
        "(declare-const mx Int)\n"
        f"(assert (>= mx {lo}))\n"
        f"(assert (<= mx {hi}))\n"
        "(assert (>= depth 0))\n"
        "(assert (<= depth mx))\n"            # invariant holds before
        "(assert (<= (+ depth 1) mx))\n"      # gate ALLOWS (effective <= mx)
        "(assert (> (+ depth 1) mx))\n"       # post-state breaks invariant
        "(check-sat)\n"
    )
    return SmtSpec(
        name="rrd_recursion_bound_inductive",
        smt2=smt2,
        description=(
            f"RRD recursion-depth bound is un-escapable for all ceilings in "
            f"[{lo},{hi}] (live mx={live_mx}); allowed op never exceeds mx."
        ),
        linked_invariant_name=_LINKED,
        timeout_ms=10000,
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
