"""Slice 128 Phase 2 (Phase 0 substrate) — SMT/Z3 invariant prover.

Converts an engineering assertion into a machine-checked theorem with a
tamper-evident certificate — WITHOUT replacing the existing deterministic AST
checks (``ShippedCodeInvariant``). The prover is:

  * **Out-of-process** — Z3 runs in a subprocess (a Z3 hang/crash cannot wedge
    the engine; bounded by a timeout). The solver runner is injectable so the
    verdict logic is testable without z3 installed.
  * **Import-guarded** — never ``import z3`` at module top; availability is
    probed lazily. z3 is absent in CI → the live path is UNAVAILABLE.
  * **Default-FALSE** — ``JARVIS_SMT_PROVER_ENABLED`` master off → UNAVAILABLE.
  * **Fail-closed** — only ``PROVED`` (Z3 ``unsat`` of the negation) is
    trustworthy; UNKNOWN / UNAVAILABLE / ERROR / REFUTED are NOT.
  * **Composes** ``BlueEvidenceLedger`` (records the proof as a hash-chained
    receipt) — the cryptographic certificate.
"""
from __future__ import annotations

import ast
import os
import pathlib
import unittest

from backend.core.ouroboros.governance.smt_invariant_prover import (
    ProofResult,
    ProofVerdict,
    SmtSpec,
    SolverRun,
    attest_invariant,
    is_proof_trustworthy,
    prove,
    smt_prover_enabled,
    z3_available,
)


def _runner_returning(status: str):
    def _r(smt2: str, timeout_ms: int) -> SolverRun:
        return SolverRun(status=status, raw_output=f"{status}\n")
    return _r


_TAUTOLOGY = SmtSpec(
    name="x_eq_x",
    # negation of (x == x) is unsat → invariant PROVED.
    smt2="(declare-const x Int)\n(assert (not (= x x)))\n(check-sat)\n",
    description="trivial: x == x always holds",
    linked_invariant_name="demo_tautology",
)


class TestVerdictTaxonomy(unittest.TestCase):
    def test_closed_five_values(self) -> None:
        names = {m.name for m in ProofVerdict}
        self.assertEqual(
            names,
            {"PROVED", "REFUTED", "UNKNOWN", "UNAVAILABLE", "ERROR"},
        )

    def test_str_enum(self) -> None:
        for m in ProofVerdict:
            self.assertIsInstance(m, str)


class TestGatesAndAvailability(unittest.TestCase):
    def setUp(self) -> None:
        self._prev = os.environ.get("JARVIS_SMT_PROVER_ENABLED")

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("JARVIS_SMT_PROVER_ENABLED", None)
        else:
            os.environ["JARVIS_SMT_PROVER_ENABLED"] = self._prev

    def test_master_default_false(self) -> None:
        os.environ.pop("JARVIS_SMT_PROVER_ENABLED", None)
        self.assertFalse(smt_prover_enabled())

    def test_z3_available_returns_bool_never_raises(self) -> None:
        self.assertIsInstance(z3_available(), bool)

    def test_master_off_is_unavailable(self) -> None:
        os.environ.pop("JARVIS_SMT_PROVER_ENABLED", None)
        # Even with a runner that would PROVE, master-off short-circuits.
        r = prove(_TAUTOLOGY, runner=_runner_returning("unsat"))
        self.assertEqual(r.verdict, ProofVerdict.UNAVAILABLE)

    def test_default_runner_unavailable_when_no_z3(self) -> None:
        # z3 is absent in CI; master on + default runner → UNAVAILABLE
        # (fail-closed, never a false PROVED).
        os.environ["JARVIS_SMT_PROVER_ENABLED"] = "1"
        if z3_available():
            self.skipTest("z3 present in this environment")
        r = prove(_TAUTOLOGY)
        self.assertEqual(r.verdict, ProofVerdict.UNAVAILABLE)


class TestProveVerdicts(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["JARVIS_SMT_PROVER_ENABLED"] = "1"

    def tearDown(self) -> None:
        os.environ.pop("JARVIS_SMT_PROVER_ENABLED", None)

    def test_unsat_is_proved(self) -> None:
        r = prove(_TAUTOLOGY, runner=_runner_returning("unsat"))
        self.assertEqual(r.verdict, ProofVerdict.PROVED)
        self.assertTrue(r.certificate_sha256)
        self.assertTrue(is_proof_trustworthy(r))

    def test_sat_is_refuted(self) -> None:
        r = prove(_TAUTOLOGY, runner=_runner_returning("sat"))
        self.assertEqual(r.verdict, ProofVerdict.REFUTED)
        self.assertFalse(is_proof_trustworthy(r))

    def test_unknown_fails_closed(self) -> None:
        r = prove(_TAUTOLOGY, runner=_runner_returning("unknown"))
        self.assertEqual(r.verdict, ProofVerdict.UNKNOWN)
        self.assertFalse(is_proof_trustworthy(r))

    def test_runner_exception_is_error_failclosed(self) -> None:
        def _boom(smt2: str, timeout_ms: int) -> SolverRun:
            raise RuntimeError("z3 exploded")
        r = prove(_TAUTOLOGY, runner=_boom)
        self.assertEqual(r.verdict, ProofVerdict.ERROR)
        self.assertFalse(is_proof_trustworthy(r))

    def test_timeout_status_fails_closed(self) -> None:
        r = prove(_TAUTOLOGY, runner=_runner_returning("timeout"))
        self.assertIn(r.verdict, (ProofVerdict.UNKNOWN, ProofVerdict.ERROR))
        self.assertFalse(is_proof_trustworthy(r))

    def test_certificate_is_deterministic(self) -> None:
        a = prove(_TAUTOLOGY, runner=_runner_returning("unsat"))
        b = prove(_TAUTOLOGY, runner=_runner_returning("unsat"))
        self.assertEqual(a.certificate_sha256, b.certificate_sha256)

    def test_proof_result_frozen(self) -> None:
        r = prove(_TAUTOLOGY, runner=_runner_returning("unsat"))
        self.assertIsInstance(r, ProofResult)
        with self.assertRaises(Exception):
            r.verdict = ProofVerdict.REFUTED  # type: ignore[misc]


class TestLedgerComposition(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["JARVIS_SMT_PROVER_ENABLED"] = "1"

    def tearDown(self) -> None:
        os.environ.pop("JARVIS_SMT_PROVER_ENABLED", None)

    def test_attest_records_to_blue_ledger(self) -> None:
        import tempfile
        from backend.core.ouroboros.governance.red_blue_matrix import (
            BlueEvidenceLedger,
        )
        with tempfile.TemporaryDirectory() as d:
            ledger = BlueEvidenceLedger(path=pathlib.Path(d) / "ev.jsonl")
            r = attest_invariant(
                _TAUTOLOGY, ledger=ledger,
                runner=_runner_returning("unsat"),
            )
            self.assertEqual(r.verdict, ProofVerdict.PROVED)
            # A hash-chained receipt was appended (the certificate).
            text = (pathlib.Path(d) / "ev.jsonl").read_text()
            self.assertIn("smt", text)
            self.assertIn("proved", text)


class TestImportGuardPin(unittest.TestCase):
    """The module MUST NOT import z3 at top level (import-guarded) — z3 is an
    optional heavy dep and the engine must boot without it."""

    def test_no_top_level_z3_import(self) -> None:
        src = pathlib.Path(
            "backend/core/ouroboros/governance/smt_invariant_prover.py"
        ).read_text()
        tree = ast.parse(src)
        for node in tree.body:  # module-level only
            if isinstance(node, ast.Import):
                for a in node.names:
                    self.assertNotEqual(a.name.split(".")[0], "z3")
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    self.assertNotEqual(node.module.split(".")[0], "z3")


if __name__ == "__main__":
    unittest.main()
