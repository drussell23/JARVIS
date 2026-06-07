"""Slice 130 — deterministic AST→SMT extraction closes the faithfulness gap.

Slice 129 hand-mirrored the gate's decision structure (offset +1, comparator >)
into the SMT. Slice 130 EXTRACTS that structure from the live
``recursion_depth_gate.py`` AST and compiles it to Z3 dynamically — so the proof
is over the actual source, not a human-maintained abstraction. If a developer
alters the gate logic, the extractor re-derives a different formula and the proof
re-runs (a loosened bound → REFUTED; the closed loop).

Scope (honest): this is a DETERMINISTIC extractor for the recursion-gate
decision PATTERN (``effective = chain + <int>``; ``if effective <cmp> <mx-expr>:
HALT``), NOT a general Python→SMT compiler (undecidable in general). Anything it
does not recognize → FAIL-CLOSED (extraction error → never a false PROVED).
"""
from __future__ import annotations

import os
import pathlib
import unittest

from backend.core.ouroboros.governance.ast_to_smt_extractor import (
    ExtractedGateLogic,
    extract_recursion_gate_logic,
    compile_recursion_bound_smt,
    recursion_bound_spec_from_source,
)
from backend.core.ouroboros.governance.smt_invariant_prover import (
    ProofVerdict,
    prove,
    z3_available,
)


_LIVE_SRC = pathlib.Path(
    "backend/core/ouroboros/governance/recursion_depth_gate.py"
).read_text()


def _mutate(src: str, old: str, new: str) -> str:
    assert old in src, f"anchor not found for mutation: {old!r}"
    return src.replace(old, new, 1)


class TestExtraction(unittest.TestCase):
    def test_extracts_live_decision_logic(self) -> None:
        logic = extract_recursion_gate_logic(_LIVE_SRC)
        self.assertIsInstance(logic, ExtractedGateLogic)
        self.assertTrue(logic.ok, logic.error)
        self.assertEqual(logic.lhs_offset, 1)        # effective = before + 1
        self.assertEqual(logic.comparator, ">")      # if effective > mx
        self.assertEqual(logic.rhs_offset, 0)        # compared against mx (+0)
        self.assertEqual(logic.clamp_lo, 1)
        self.assertEqual(logic.clamp_hi, 16)

    def test_compiled_smt_reflects_extracted_params(self) -> None:
        logic = extract_recursion_gate_logic(_LIVE_SRC)
        smt2 = compile_recursion_bound_smt(logic)
        self.assertIn("check-sat", smt2)
        self.assertIn("(+ before 1)", smt2)          # the extracted offset

    def test_fail_closed_on_unrecognized_source(self) -> None:
        # No evaluate_recursion_gate → extraction must FAIL (not silently pass).
        logic = extract_recursion_gate_logic("def unrelated():\n    return 1\n")
        self.assertFalse(logic.ok)
        self.assertTrue(logic.error)


class TestClosedLoopProof(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["JARVIS_SMT_PROVER_ENABLED"] = "1"
        if not z3_available():
            self.skipTest("z3 not provisioned")

    def tearDown(self) -> None:
        os.environ.pop("JARVIS_SMT_PROVER_ENABLED", None)

    def test_live_source_proves(self) -> None:
        spec = recursion_bound_spec_from_source(_LIVE_SRC)
        r = prove(spec)
        self.assertEqual(r.verdict, ProofVerdict.PROVED)
        self.assertTrue(r.certificate_sha256)

    def test_loosened_mutation_is_refuted(self) -> None:
        # THE CLOSED LOOP: a developer loosens the bound. The extractor must
        # catch it (re-derive the formula) and Z3 must REFUTE it.
        mutated = _mutate(_LIVE_SRC, "if effective > mx:", "if effective > mx + 1:")
        spec = recursion_bound_spec_from_source(mutated)
        r = prove(spec)
        self.assertEqual(r.verdict, ProofVerdict.REFUTED)

    def test_stricter_mutation_still_proves(self) -> None:
        # A STRICTER bound (>= instead of >) is still safe → PROVED.
        mutated = _mutate(_LIVE_SRC, "if effective > mx:", "if effective >= mx:")
        spec = recursion_bound_spec_from_source(mutated)
        r = prove(spec)
        self.assertEqual(r.verdict, ProofVerdict.PROVED)

    def test_offset_mutation_changes_formula(self) -> None:
        # Changing the offset (+1 → +2) re-derives a different SMT formula.
        live = compile_recursion_bound_smt(extract_recursion_gate_logic(_LIVE_SRC))
        mutated_src = _mutate(_LIVE_SRC, "effective = before + 1", "effective = before + 2")
        mutated = compile_recursion_bound_smt(extract_recursion_gate_logic(mutated_src))
        self.assertNotEqual(live, mutated)
        self.assertIn("(+ before 2)", mutated)


class TestFailClosedProof(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["JARVIS_SMT_PROVER_ENABLED"] = "1"

    def tearDown(self) -> None:
        os.environ.pop("JARVIS_SMT_PROVER_ENABLED", None)

    def test_unextractable_source_never_proves(self) -> None:
        # Fail-closed: an unrecognized source must NOT yield PROVED.
        spec = recursion_bound_spec_from_source("def nope():\n    return 0\n")
        r = prove(spec)
        self.assertNotEqual(r.verdict, ProofVerdict.PROVED)


if __name__ == "__main__":
    unittest.main()
