"""Slice 129 — SMT encoding of the RRD Recursion-Depth Bound + the first live
cryptographic proof certificate.

Theorem (inductive safety of the recursion ceiling): for ANY configured ceiling
``mx`` in the gate's clamp range [_MIN, _MAX], the gate's allow-decision
(mirrored from ``evaluate_recursion_gate``: ``effective = depth + 1``; HALT iff
``effective > mx``) can never permit a governance op that pushes the applied
chain depth beyond ``mx``. I.e. the invariant ``applied_depth <= mx`` is
preserved by every allowed op — the bound is un-escapable.

PROVED iff Z3 returns ``unsat`` for the negation. A deliberately-loosened
encoding must be REFUTED (``sat``) — the negative control proving the PROVED is
not vacuous. The proof object is hashed into a tamper-evident
``BlueEvidenceLedger`` receipt (the dissertation certificate).
"""
from __future__ import annotations

import os
import pathlib
import tempfile
import unittest

from backend.core.ouroboros.governance.smt_invariant_prover import (
    ProofVerdict,
    SmtSpec,
    attest_invariant,
    is_proof_trustworthy,
    prove,
    z3_available,
)
from backend.core.ouroboros.governance.smt_encodings import (
    recursion_bound_spec,
    _loosened_recursion_bound_spec_for_test,
)


class TestEncoding(unittest.TestCase):
    def test_spec_extracts_live_params_and_links_invariant(self) -> None:
        spec = recursion_bound_spec()
        self.assertIsInstance(spec, SmtSpec)
        self.assertEqual(spec.linked_invariant_name, "recursion_depth_gate")
        # The live ceiling (default 3) is recorded in the spec for the cert.
        from backend.core.ouroboros.governance.recursion_depth_gate import (
            max_recursion_depth,
        )
        self.assertIn(str(max_recursion_depth()), spec.description)
        self.assertIn("check-sat", spec.smt2)

    def test_gate_structure_pin(self) -> None:
        # The encoding mirrors the gate's decision STRUCTURE. If the code's
        # operator/offset drifts, this pin fails → re-derive the encoding so the
        # proof stays faithful.
        src = pathlib.Path(
            "backend/core/ouroboros/governance/recursion_depth_gate.py"
        ).read_text()
        self.assertIn("effective = before + 1", src)
        self.assertIn("if effective > mx", src)


class TestLiveProof(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["JARVIS_SMT_PROVER_ENABLED"] = "1"
        if not z3_available():
            self.skipTest("z3 not provisioned")

    def tearDown(self) -> None:
        os.environ.pop("JARVIS_SMT_PROVER_ENABLED", None)

    def test_recursion_bound_is_proven_live(self) -> None:
        r = prove(recursion_bound_spec())  # default runner = live z3
        self.assertEqual(r.verdict, ProofVerdict.PROVED)
        self.assertTrue(is_proof_trustworthy(r))
        self.assertTrue(r.certificate_sha256)

    def test_loosened_encoding_is_refuted_negative_control(self) -> None:
        # A loosened bound (allows depth+1 == mx+1) MUST be refutable — proves
        # the prover genuinely discriminates and PROVED is not vacuous.
        r = prove(_loosened_recursion_bound_spec_for_test())
        self.assertEqual(r.verdict, ProofVerdict.REFUTED)
        self.assertFalse(is_proof_trustworthy(r))


class TestCertificateGeneration(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["JARVIS_SMT_PROVER_ENABLED"] = "1"
        if not z3_available():
            self.skipTest("z3 not provisioned")

    def tearDown(self) -> None:
        os.environ.pop("JARVIS_SMT_PROVER_ENABLED", None)

    def test_proof_certificate_written_to_blue_ledger(self) -> None:
        import json
        from backend.core.ouroboros.governance.red_blue_matrix import (
            BlueEvidenceLedger,
        )
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "dissertation_evidence.jsonl"
            ledger = BlueEvidenceLedger(path=path)
            result = attest_invariant(recursion_bound_spec(), ledger=ledger)
            self.assertEqual(result.verdict, ProofVerdict.PROVED)
            # The tamper-evident certificate landed in the ledger.
            lines = [
                json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()
            ]
            self.assertEqual(len(lines), 1)
            rec = lines[0]
            self.assertEqual(rec["attack_class"], "smt_invariant_proof")
            self.assertEqual(rec["verdict"], "proved")
            self.assertTrue(rec["blocked"])           # trustworthy PROVED
            self.assertTrue(rec["record_hash"])       # hash-chained certificate
            self.assertIn("recursion_depth_gate", rec["blocked_by"])


if __name__ == "__main__":
    unittest.main()
