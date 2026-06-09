"""Slice 185 — strict-type sovereignty + adaptive latency matrix.

Phase 1: the _effective_model NameError is killed (resolved in scope).
Phase 2: a Python logical error (NameError/TypeError/...) bypasses the vendor lane, crashes
         loud, and NEVER touches the surface-health vendor ledger.
Phase 3: the latency governor elects batch when RT p95 mathematically crushes batch TTFT —
         dynamically, no hardcoded preference; reverts to RT if DW's latency recovers.
Phase 4: the corrupted DW learned-state is wiped on an opt-in clean boot.
"""
from __future__ import annotations

import importlib.util
import json
import os
import unittest

from backend.core.ouroboros.governance.dw_fault_taxonomy import is_internal_fault
from backend.core.ouroboros.governance import doubleword_provider as DW
from backend.core.ouroboros.governance.dw_ledger_wipe import (
    wipe_corrupted_dw_ledgers,
    dw_ledger_wipe_enabled,
)


def _gen_src():
    spec = importlib.util.find_spec("backend.core.ouroboros.governance.candidate_generator")
    with open(spec.origin) as fh:
        return fh.read()


class TestPhase1NameErrorKilled(unittest.TestCase):
    def test_line_resolves_effective_model_in_scope(self):
        with open(importlib.util.find_spec(
            "backend.core.ouroboros.governance.doubleword_provider").origin) as fh:
            src = fh.read()
        # the bug line must now resolve the model natively, not reference a free var
        self.assertIn("model_id=self._resolve_effective_model(context)", src)
        self.assertNotIn("model_id=_effective_model)", src)  # the exact phantom is gone


class TestPhase2ExceptionSegregation(unittest.TestCase):
    def test_python_logic_errors_are_internal(self):
        for exc in (NameError("x"), TypeError("x"), AttributeError("x"),
                    KeyError("x"), IndexError("x")):
            self.assertTrue(is_internal_fault(exc), exc)

    def test_vendor_faults_are_not_internal(self):
        self.assertFalse(is_internal_fault(json.JSONDecodeError("bad", "doc", 0)))  # vendor payload
        self.assertFalse(is_internal_fault(ConnectionError("network")))
        self.assertFalse(is_internal_fault(TimeoutError("slow")))

        class _Infra(Exception):
            status_code = 503
        self.assertFalse(is_internal_fault(_Infra()))  # structured vendor error

    def test_value_error_without_status_is_internal_with_is_not(self):
        self.assertTrue(is_internal_fault(ValueError("our bad math")))
        ve = ValueError("vendor said no")
        ve.status_code = 400  # type: ignore[attr-defined]
        self.assertFalse(is_internal_fault(ve))

    def test_classifier_reraises_internal_before_ledger(self):
        src = _gen_src()
        self.assertIn("is_internal_fault", src)
        self.assertIn("INTERNAL_FAULT", src)
        # the internal-fault re-raise must precede the vendor LIVE_TRANSPORT classification
        i_reraise = src.find("_s185_internal(exc)")
        i_classify = src.find("failure_source = FailureSource.LIVE_TRANSPORT")
        self.assertTrue(0 < i_reraise < i_classify)
        # and the guard must actually re-raise (crash loud), not classify
        self.assertIn("raise exc", src[i_reraise:i_classify])


class TestPhase3LatencyGovernor(unittest.TestCase):
    def setUp(self):
        from backend.core.ouroboros.governance import dw_latency_tracker as LT
        LT.reset_default_tracker()
        self._LT = LT

    def tearDown(self):
        self._LT.reset_default_tracker()
        for k in ("JARVIS_DW_LATENCY_BATCH_MULT", "JARVIS_DW_BATCH_TTFT_S"):
            os.environ.pop(k, None)

    def test_batch_elected_when_rt_p95_crushes_batch(self):
        t = self._LT.get_default_tracker()
        for _ in range(5):
            t.record_success(66.0)  # the v31 RT reality: 66s
        os.environ["JARVIS_DW_BATCH_TTFT_S"] = "8"
        self.assertTrue(DW._dw_latency_favors_batch())  # 66 > 8*3

    def test_rt_kept_when_latency_recovers(self):
        t = self._LT.get_default_tracker()
        for _ in range(5):
            t.record_success(5.0)  # DW fixed their RT — fast now
        os.environ["JARVIS_DW_BATCH_TTFT_S"] = "8"
        self.assertFalse(DW._dw_latency_favors_batch())  # 5 < 8*3 → revert to RT, zero config

    def test_no_sample_defers(self):
        self.assertFalse(DW._dw_latency_favors_batch())  # no RT data → let other gates decide


class TestPhase4LedgerWipe(unittest.TestCase):
    def test_wipe_removes_corrupted_state_when_enabled(self, ):
        import tempfile
        d = tempfile.mkdtemp()
        sh = os.path.join(d, "dw_surface_health.json")
        cal = os.path.join(d, "dw_threshold_calibration_deepseek.json")
        for p in (sh, cal):
            with open(p, "w") as fh:
                fh.write("{}")
        os.environ["JARVIS_DW_LEDGER_WIPE_ON_BOOT"] = "1"
        try:
            rep = wipe_corrupted_dw_ledgers(state_dir=d)
            self.assertTrue(rep["enabled"])
            self.assertEqual(len(rep["wiped"]), 2)
            self.assertFalse(os.path.exists(sh))
            self.assertFalse(os.path.exists(cal))
        finally:
            os.environ.pop("JARVIS_DW_LEDGER_WIPE_ON_BOOT", None)

    def test_wipe_noop_when_disabled(self):
        os.environ.pop("JARVIS_DW_LEDGER_WIPE_ON_BOOT", None)
        import tempfile
        d = tempfile.mkdtemp()
        sh = os.path.join(d, "dw_surface_health.json")
        with open(sh, "w") as fh:
            fh.write("{}")
        rep = wipe_corrupted_dw_ledgers(state_dir=d)
        self.assertFalse(rep["enabled"])
        self.assertEqual(rep["wiped"], [])
        self.assertTrue(os.path.exists(sh))  # untouched when disabled


if __name__ == "__main__":
    unittest.main()
