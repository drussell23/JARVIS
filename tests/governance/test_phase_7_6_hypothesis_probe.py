"""Phase 7.6 — bounded HypothesisProbe runner pins.

Pinned cage:
  * Three independent termination guarantees ALWAYS fire structurally:
      - call cap (MAX_CALLS)
      - wall-clock cap (TIMEOUT_S, time.monotonic-based)
      - diminishing-returns (sha256 fingerprint of consecutive rounds)
  * Read-only tool allowlist constant frozen
  * Master flag default false
  * Null prober is the safe-default (zero cost; terminates inconclusive)
  * Empty hypothesis pre-check
  * Prober exception caught → INCONCLUSIVE_PROBER_ERROR
  * NEVER raises into caller
  * Authority invariants: stdlib only; one-way dep
"""
from __future__ import annotations

import ast
import time
from pathlib import Path
from typing import List, Tuple
from unittest import mock

import pytest

from backend.core.ouroboros.governance.adaptation import (
    hypothesis_probe as probe_mod,
)
from backend.core.ouroboros.governance.adaptation.hypothesis_probe import (
    HypothesisProbe,
    MAX_CALLS_PER_PROBE_DEFAULT,
    MAX_EVIDENCE_CHARS_PER_ROUND,
    MAX_NOTES_CHARS,
    ProbeResult,
    ProbeRoundResult,
    ProbeVerdict,
    READONLY_TOOL_ALLOWLIST,
    TIMEOUT_S_DEFAULT,
    _NullEvidenceProber,
    get_max_calls_per_probe,
    get_timeout_s,
    is_probe_enabled,
)


def _enable(monkeypatch):
    monkeypatch.setenv("JARVIS_HYPOTHESIS_PROBE_ENABLED", "1")


# ---------------------------------------------------------------------------
# Section A — module constants + master flag + verdict enum
# ---------------------------------------------------------------------------


class TestModuleConstants:
    def test_readonly_tool_allowlist_shape(self):
        # Per PRD §9 P7.6: read_file / search_code / get_callers /
        # glob_files / list_dir
        assert READONLY_TOOL_ALLOWLIST == frozenset({
            "read_file", "search_code", "get_callers",
            "glob_files", "list_dir",
        })

    def test_readonly_tool_allowlist_is_frozen(self):
        with pytest.raises(AttributeError):
            READONLY_TOOL_ALLOWLIST.add("write_file")  # type: ignore[attr-defined]

    def test_max_calls_default_is_5(self):
        assert MAX_CALLS_PER_PROBE_DEFAULT == 5

    def test_timeout_s_default_is_30(self):
        assert TIMEOUT_S_DEFAULT == 30.0

    def test_max_evidence_chars_per_round(self):
        assert MAX_EVIDENCE_CHARS_PER_ROUND == 4096

    def test_max_notes_chars(self):
        assert MAX_NOTES_CHARS == 1024

    def test_truthy_constant_shape(self):
        assert probe_mod._TRUTHY == ("1", "true", "yes", "on")


class TestMasterFlag:
    def test_default_false(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_HYPOTHESIS_PROBE_ENABLED", raising=False,
        )
        assert is_probe_enabled() is False

    def test_truthy_variants(self, monkeypatch):
        for v in ("1", "true", "TRUE", "Yes", "ON"):
            monkeypatch.setenv("JARVIS_HYPOTHESIS_PROBE_ENABLED", v)
            assert is_probe_enabled() is True, v

    def test_falsy_variants(self, monkeypatch):
        for v in ("0", "false", "no", "off", "", " "):
            monkeypatch.setenv("JARVIS_HYPOTHESIS_PROBE_ENABLED", v)
            assert is_probe_enabled() is False, v


class TestEnvOverrides:
    def test_max_calls_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_HYPOTHESIS_PROBE_MAX_CALLS", raising=False,
        )
        assert get_max_calls_per_probe() == MAX_CALLS_PER_PROBE_DEFAULT

    def test_max_calls_env_override(self, monkeypatch):
        monkeypatch.setenv("JARVIS_HYPOTHESIS_PROBE_MAX_CALLS", "3")
        assert get_max_calls_per_probe() == 3

    def test_max_calls_invalid_falls_back(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_HYPOTHESIS_PROBE_MAX_CALLS", "not-an-int",
        )
        assert get_max_calls_per_probe() == MAX_CALLS_PER_PROBE_DEFAULT

    def test_max_calls_zero_falls_back(self, monkeypatch):
        monkeypatch.setenv("JARVIS_HYPOTHESIS_PROBE_MAX_CALLS", "0")
        assert get_max_calls_per_probe() == MAX_CALLS_PER_PROBE_DEFAULT

    def test_timeout_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_HYPOTHESIS_PROBE_TIMEOUT_S", raising=False,
        )
        assert get_timeout_s() == TIMEOUT_S_DEFAULT

    def test_timeout_env_override(self, monkeypatch):
        monkeypatch.setenv("JARVIS_HYPOTHESIS_PROBE_TIMEOUT_S", "5.5")
        assert get_timeout_s() == 5.5

    def test_timeout_invalid_falls_back(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_HYPOTHESIS_PROBE_TIMEOUT_S", "abc",
        )
        assert get_timeout_s() == TIMEOUT_S_DEFAULT

    def test_timeout_zero_falls_back(self, monkeypatch):
        monkeypatch.setenv("JARVIS_HYPOTHESIS_PROBE_TIMEOUT_S", "0")
        assert get_timeout_s() == TIMEOUT_S_DEFAULT


class TestVerdictEnum:
    def test_all_verdict_values_present(self):
        # Pin the full set so adding/removing a verdict is intentional.
        assert {v.value for v in ProbeVerdict} == {
            "confirmed", "refuted",
            "inconclusive_budget", "inconclusive_timeout",
            "inconclusive_diminishing", "inconclusive_prober_error",
            "skipped_master_off", "skipped_no_prober",
            "skipped_empty_hypothesis",
        }


# ---------------------------------------------------------------------------
# Section B — pre-checks (skip paths)
# ---------------------------------------------------------------------------


class TestPreChecks:
    def test_master_off_skipped(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_HYPOTHESIS_PROBE_ENABLED", raising=False,
        )
        p = HypothesisProbe(prober=_NullEvidenceProber())
        result = p.test("claim", "expected_outcome")
        assert result.verdict == ProbeVerdict.SKIPPED_MASTER_OFF
        assert result.rounds == 0
        assert result.is_skipped

    def test_empty_claim_skipped(self, monkeypatch):
        _enable(monkeypatch)
        p = HypothesisProbe(prober=_NullEvidenceProber())
        result = p.test("", "expected_outcome")
        assert result.verdict == ProbeVerdict.SKIPPED_EMPTY_HYPOTHESIS

    def test_whitespace_claim_skipped(self, monkeypatch):
        _enable(monkeypatch)
        p = HypothesisProbe(prober=_NullEvidenceProber())
        result = p.test("   ", "expected_outcome")
        assert result.verdict == ProbeVerdict.SKIPPED_EMPTY_HYPOTHESIS

    def test_empty_expected_outcome_skipped(self, monkeypatch):
        _enable(monkeypatch)
        p = HypothesisProbe(prober=_NullEvidenceProber())
        result = p.test("claim", "")
        assert result.verdict == ProbeVerdict.SKIPPED_EMPTY_HYPOTHESIS

    def test_no_prober_skipped(self, monkeypatch):
        _enable(monkeypatch)
        p = HypothesisProbe(prober=None)
        result = p.test("claim", "expected_outcome")
        assert result.verdict == ProbeVerdict.SKIPPED_NO_PROBER


# ---------------------------------------------------------------------------
# Section C — Null prober safety
# ---------------------------------------------------------------------------


class TestNullProber:
    def test_null_prober_returns_continue(self):
        np = _NullEvidenceProber()
        r = np.probe("claim", "expected", ())
        assert r.verdict_signal == "continue"
        assert r.evidence == ""
        assert r.notes == "null_prober"

    def test_null_prober_terminates_diminishing(self, monkeypatch):
        # Two rounds with empty evidence → identical fingerprint →
        # INCONCLUSIVE_DIMINISHING. Proves a misconfigured caller
        # using the Null sentinel cannot accidentally invoke a model.
        _enable(monkeypatch)
        p = HypothesisProbe(prober=_NullEvidenceProber())
        result = p.test("claim", "expected_outcome")
        assert result.verdict == ProbeVerdict.INCONCLUSIVE_DIMINISHING
        assert result.rounds == 2  # round 1 + round 2 with same hash


# ---------------------------------------------------------------------------
# Section D — verdict signals (CONFIRMED / REFUTED)
# ---------------------------------------------------------------------------


class _ScriptedProber:
    """Test prober that returns a scripted sequence of round results."""

    def __init__(self, sequence: List[ProbeRoundResult]):
        self._sequence = list(sequence)
        self._index = 0
        self.calls: List[Tuple[str, str, Tuple[str, ...]]] = []

    def probe(self, claim, expected_outcome, prior_evidence):
        self.calls.append((claim, expected_outcome, prior_evidence))
        if self._index >= len(self._sequence):
            # Default: continue with diminishing-returns stub.
            return ProbeRoundResult("continue", "", "")
        r = self._sequence[self._index]
        self._index += 1
        return r


class TestVerdictSignals:
    def test_confirmed_terminates_immediately(self, monkeypatch):
        _enable(monkeypatch)
        prober = _ScriptedProber([
            ProbeRoundResult(
                "confirmed", "X is in foo.py:42", "found",
            ),
        ])
        p = HypothesisProbe(prober=prober)
        result = p.test("X causes Y", "X visible at foo.py")
        assert result.verdict == ProbeVerdict.CONFIRMED
        assert result.rounds == 1
        assert result.is_confirmed
        assert result.final_evidence == "X is in foo.py:42"

    def test_refuted_terminates_immediately(self, monkeypatch):
        _enable(monkeypatch)
        prober = _ScriptedProber([
            ProbeRoundResult(
                "refuted", "X is NOT in foo.py", "checked",
            ),
        ])
        p = HypothesisProbe(prober=prober)
        result = p.test("X causes Y", "X visible at foo.py")
        assert result.verdict == ProbeVerdict.REFUTED
        assert result.is_refuted

    def test_confirmed_with_empty_evidence_does_not_terminate(
        self, monkeypatch,
    ):
        # Defense-in-depth: a "confirmed" signal with NO evidence is
        # not a real confirmation — fall through to next round /
        # diminishing-returns.
        _enable(monkeypatch)
        prober = _ScriptedProber([
            ProbeRoundResult("confirmed", "", "no-evidence"),
            ProbeRoundResult("confirmed", "", "no-evidence"),
        ])
        p = HypothesisProbe(prober=prober)
        result = p.test("claim", "expected")
        # Both rounds same empty fingerprint → DIMINISHING.
        assert result.verdict == ProbeVerdict.INCONCLUSIVE_DIMINISHING

    def test_refuted_with_empty_evidence_does_not_terminate(
        self, monkeypatch,
    ):
        _enable(monkeypatch)
        prober = _ScriptedProber([
            ProbeRoundResult("refuted", "", ""),
            ProbeRoundResult("refuted", "", ""),
        ])
        p = HypothesisProbe(prober=prober)
        result = p.test("claim", "expected")
        assert result.verdict == ProbeVerdict.INCONCLUSIVE_DIMINISHING

    def test_continue_then_confirmed(self, monkeypatch):
        _enable(monkeypatch)
        prober = _ScriptedProber([
            ProbeRoundResult("continue", "round1 evidence", ""),
            ProbeRoundResult("continue", "round2 different", ""),
            ProbeRoundResult("confirmed", "found at last", ""),
        ])
        p = HypothesisProbe(prober=prober)
        result = p.test("claim", "expected")
        assert result.verdict == ProbeVerdict.CONFIRMED
        assert result.rounds == 3

    def test_unknown_signal_treated_as_continue(self, monkeypatch):
        _enable(monkeypatch)
        prober = _ScriptedProber([
            ProbeRoundResult("garbled", "round1", ""),
            ProbeRoundResult("garbled", "round2 different", ""),
            ProbeRoundResult("confirmed", "found", ""),
        ])
        p = HypothesisProbe(prober=prober)
        result = p.test("claim", "expected")
        assert result.verdict == ProbeVerdict.CONFIRMED


# ---------------------------------------------------------------------------
# Section E — termination guarantee 1: call cap
# ---------------------------------------------------------------------------


class TestCallCapGuarantee:
    def test_call_cap_default_is_5(self, monkeypatch):
        # Prober returns "continue" with NEW evidence each round so
        # diminishing-returns DOESN'T fire — proves call cap fires.
        _enable(monkeypatch)
        prober = _ScriptedProber([
            ProbeRoundResult("continue", f"r{i}", "")
            for i in range(20)
        ])
        p = HypothesisProbe(prober=prober)
        result = p.test("claim", "expected")
        assert result.verdict == ProbeVerdict.INCONCLUSIVE_BUDGET
        assert result.rounds == MAX_CALLS_PER_PROBE_DEFAULT

    def test_call_cap_constructor_override(self, monkeypatch):
        _enable(monkeypatch)
        prober = _ScriptedProber([
            ProbeRoundResult("continue", f"r{i}", "")
            for i in range(20)
        ])
        p = HypothesisProbe(prober=prober, max_calls=3)
        result = p.test("claim", "expected")
        assert result.verdict == ProbeVerdict.INCONCLUSIVE_BUDGET
        assert result.rounds == 3

    def test_call_cap_env_override(self, monkeypatch):
        _enable(monkeypatch)
        monkeypatch.setenv("JARVIS_HYPOTHESIS_PROBE_MAX_CALLS", "2")
        prober = _ScriptedProber([
            ProbeRoundResult("continue", f"r{i}", "")
            for i in range(20)
        ])
        # No constructor override → env wins.
        p = HypothesisProbe(prober=prober)
        result = p.test("claim", "expected")
        assert result.verdict == ProbeVerdict.INCONCLUSIVE_BUDGET
        assert result.rounds == 2


# ---------------------------------------------------------------------------
# Section F — termination guarantee 2: wall-clock cap
# ---------------------------------------------------------------------------


class TestWallClockGuarantee:
    def test_timeout_terminates(self, monkeypatch):
        _enable(monkeypatch)
        # Patch time.monotonic to advance past timeout on second call.
        clock = [0.0]
        real_monotonic = time.monotonic

        def fake_monotonic():
            return clock[0]

        prober = _ScriptedProber([
            ProbeRoundResult("continue", "r1", ""),
            ProbeRoundResult("continue", "r2", ""),
            ProbeRoundResult("continue", "r3", ""),
        ])
        p = HypothesisProbe(prober=prober, max_calls=10, timeout_s=5.0)
        with mock.patch.object(probe_mod.time, "monotonic", side_effect=lambda: clock[0]):
            # Round 1: time=0, OK; round 2: time=10 > 5 → terminate.
            def advance(*a, **k):
                clock[0] += 10.0
                return ProbeRoundResult("continue", f"r{int(clock[0])}", "")
            prober2 = mock.Mock()
            prober2.probe.side_effect = advance
            p2 = HypothesisProbe(prober=prober2, max_calls=10, timeout_s=5.0)
            result = p2.test("claim", "expected")
        assert result.verdict == ProbeVerdict.INCONCLUSIVE_TIMEOUT

    def test_timeout_uses_monotonic_not_wall_clock(self):
        # Pin via source — runner reads time.monotonic, not time.time
        # (wall clock changes mid-probe shouldn't affect cap).
        source = Path(probe_mod.__file__).read_text()
        assert "time.monotonic()" in source
        # Wall-clock time.time should NOT appear in the runner body.
        # (Allow it in unrelated code if any; tighter test:
        # diff of usage counts.)
        # Simpler: assert the runner uses monotonic.
        assert source.count("time.monotonic()") >= 2  # start + at least 1 check


# ---------------------------------------------------------------------------
# Section G — termination guarantee 3: diminishing returns
# ---------------------------------------------------------------------------


class TestDiminishingReturnsGuarantee:
    def test_identical_evidence_terminates_at_round_2(self, monkeypatch):
        _enable(monkeypatch)
        prober = _ScriptedProber([
            ProbeRoundResult("continue", "same", ""),
            ProbeRoundResult("continue", "same", ""),
        ])
        p = HypothesisProbe(prober=prober)
        result = p.test("claim", "expected")
        assert result.verdict == ProbeVerdict.INCONCLUSIVE_DIMINISHING
        assert result.rounds == 2

    def test_different_then_repeat_terminates(self, monkeypatch):
        _enable(monkeypatch)
        # round1=A, round2=B (different), round3=B (repeat) → terminate
        _enable(monkeypatch)
        prober = _ScriptedProber([
            ProbeRoundResult("continue", "A", ""),
            ProbeRoundResult("continue", "B", ""),
            ProbeRoundResult("continue", "B", ""),
        ])
        p = HypothesisProbe(prober=prober)
        result = p.test("claim", "expected")
        assert result.verdict == ProbeVerdict.INCONCLUSIVE_DIMINISHING
        assert result.rounds == 3

    def test_alternating_evidence_does_not_diminish(self, monkeypatch):
        # A B A B → no consecutive duplicates; diminishing-returns
        # does NOT fire (only consecutive). Eventually call cap fires.
        _enable(monkeypatch)
        prober = _ScriptedProber([
            ProbeRoundResult("continue", "A", ""),
            ProbeRoundResult("continue", "B", ""),
            ProbeRoundResult("continue", "A", ""),
            ProbeRoundResult("continue", "B", ""),
            ProbeRoundResult("continue", "A", ""),
        ])
        p = HypothesisProbe(prober=prober, max_calls=5)
        result = p.test("claim", "expected")
        assert result.verdict == ProbeVerdict.INCONCLUSIVE_BUDGET

    def test_evidence_hashes_returned_in_order(self, monkeypatch):
        _enable(monkeypatch)
        prober = _ScriptedProber([
            ProbeRoundResult("continue", "A", ""),
            ProbeRoundResult("continue", "A", ""),
        ])
        p = HypothesisProbe(prober=prober)
        result = p.test("claim", "expected")
        assert len(result.evidence_hashes) == 2
        assert all(h.startswith("sha256:") for h in result.evidence_hashes)
        # Both rounds have identical evidence — both hashes equal.
        assert result.evidence_hashes[0] == result.evidence_hashes[1]


# ---------------------------------------------------------------------------
# Section H — prober exception handling
# ---------------------------------------------------------------------------


class _RaisingProber:
    def probe(self, claim, expected_outcome, prior_evidence):
        raise RuntimeError("simulated prober failure")


class TestProberExceptionHandling:
    def test_prober_raise_caught(self, monkeypatch):
        _enable(monkeypatch)
        p = HypothesisProbe(prober=_RaisingProber())
        result = p.test("claim", "expected")
        assert result.verdict == ProbeVerdict.INCONCLUSIVE_PROBER_ERROR
        assert "RuntimeError" in result.notes
        assert "simulated prober failure" in result.notes

    def test_prober_raise_after_some_evidence(self, monkeypatch):
        _enable(monkeypatch)
        # Wrap a prober that succeeds once then raises.
        class Mixed:
            def __init__(self):
                self._n = 0

            def probe(self, claim, expected_outcome, prior_evidence):
                self._n += 1
                if self._n == 1:
                    return ProbeRoundResult("continue", "round1", "")
                raise ValueError("boom")

        p = HypothesisProbe(prober=Mixed())
        result = p.test("claim", "expected")
        assert result.verdict == ProbeVerdict.INCONCLUSIVE_PROBER_ERROR
        assert result.rounds == 1  # 1 successful round before raise


# ---------------------------------------------------------------------------
# Section I — bounded sizes (defense-in-depth)
# ---------------------------------------------------------------------------


class TestBoundedSizes:
    def test_evidence_truncated_at_max(self, monkeypatch):
        _enable(monkeypatch)
        big_evidence = "X" * (MAX_EVIDENCE_CHARS_PER_ROUND + 1000)
        prober = _ScriptedProber([
            ProbeRoundResult("confirmed", big_evidence, ""),
        ])
        p = HypothesisProbe(prober=prober)
        result = p.test("claim", "expected")
        assert result.verdict == ProbeVerdict.CONFIRMED
        assert len(result.final_evidence) <= MAX_EVIDENCE_CHARS_PER_ROUND
        assert "...(truncated)" in result.final_evidence

    def test_notes_truncated_at_max(self, monkeypatch):
        _enable(monkeypatch)
        big_notes = "Y" * (MAX_NOTES_CHARS + 500)
        prober = _ScriptedProber([
            ProbeRoundResult("confirmed", "evidence", big_notes),
        ])
        p = HypothesisProbe(prober=prober)
        result = p.test("claim", "expected")
        assert len(result.notes) <= MAX_NOTES_CHARS

    def test_call_cap_with_huge_evidence_still_caps_calls(
        self, monkeypatch,
    ):
        # Pin: even with huge evidence, the call cap still fires
        # (sanity check that truncation doesn't reset the call counter).
        # Put the differentiating index FIRST so post-truncation each
        # round still has a distinct fingerprint (otherwise diminishing-
        # returns fires on identical truncated suffixes).
        _enable(monkeypatch)
        big = "Z" * 10000
        prober = _ScriptedProber([
            ProbeRoundResult("continue", f"R{i:02d}_" + big, "")
            for i in range(20)
        ])
        p = HypothesisProbe(prober=prober, max_calls=3)
        result = p.test("claim", "expected")
        assert result.verdict == ProbeVerdict.INCONCLUSIVE_BUDGET
        assert result.rounds == 3


# ---------------------------------------------------------------------------
# Section J — ProbeResult convenience
# ---------------------------------------------------------------------------


class TestProbeResult:
    def test_is_confirmed_property(self):
        r = ProbeResult(
            verdict=ProbeVerdict.CONFIRMED, rounds=1, elapsed_s=0.1,
        )
        assert r.is_confirmed
        assert not r.is_refuted
        assert not r.is_inconclusive
        assert not r.is_skipped

    def test_is_refuted_property(self):
        r = ProbeResult(
            verdict=ProbeVerdict.REFUTED, rounds=1, elapsed_s=0.1,
        )
        assert r.is_refuted

    def test_is_inconclusive_covers_all_4_terminations(self):
        for v in (
            ProbeVerdict.INCONCLUSIVE_BUDGET,
            ProbeVerdict.INCONCLUSIVE_TIMEOUT,
            ProbeVerdict.INCONCLUSIVE_DIMINISHING,
            ProbeVerdict.INCONCLUSIVE_PROBER_ERROR,
        ):
            r = ProbeResult(verdict=v, rounds=1, elapsed_s=0.1)
            assert r.is_inconclusive, v

    def test_is_skipped_covers_all_3_skips(self):
        for v in (
            ProbeVerdict.SKIPPED_MASTER_OFF,
            ProbeVerdict.SKIPPED_NO_PROBER,
            ProbeVerdict.SKIPPED_EMPTY_HYPOTHESIS,
        ):
            r = ProbeResult(verdict=v, rounds=0, elapsed_s=0.0)
            assert r.is_skipped, v

    def test_frozen(self):
        r = ProbeResult(
            verdict=ProbeVerdict.CONFIRMED, rounds=1, elapsed_s=0.1,
        )
        with pytest.raises(Exception):
            r.rounds = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Section K — authority invariants
# ---------------------------------------------------------------------------


_PROBE_PATH = Path(probe_mod.__file__)


class TestAuthorityInvariants:
    def test_no_banned_governance_imports(self):
        """Probe runner must NOT import HypothesisLedger,
        tool_executor, or any orchestrator/Venom module — one-way
        dependency rule.
        """
        source = _PROBE_PATH.read_text()
        tree = ast.parse(source)
        banned_substrings = (
            "hypothesis_ledger",
            "tool_executor",
            "scoped_tool_backend",
            "general_driver",
            "exploration_engine",
            "semantic_guardian",
            "orchestrator",
            "phase_runners",
            "gate_runner",
            "risk_tier_floor",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for banned in banned_substrings:
                    assert banned not in node.module, (
                        f"banned import: {node.module}"
                    )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    for banned in banned_substrings:
                        assert banned not in alias.name, (
                            f"banned import: {alias.name}"
                        )

    def test_only_stdlib(self):
        """Top-level imports must be stdlib only."""
        source = _PROBE_PATH.read_text()
        tree = ast.parse(source)
        stdlib_prefixes = (
            "__future__", "enum", "hashlib", "logging", "os",
            "time", "dataclasses", "typing",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("backend."):
                    pytest.fail(
                        f"unexpected backend import: {node.module}"
                    )
                else:
                    assert any(
                        node.module.startswith(p) for p in stdlib_prefixes
                    ), f"unexpected import: {node.module}"

    def test_no_subprocess_or_network_tokens(self):
        source = _PROBE_PATH.read_text()
        for token in (
            "subprocess", "requests", "urllib", "socket",
            "http.client", "asyncio.create_subprocess",
            "anthropic",
        ):
            assert token not in source, f"banned token: {token}"

    def test_runner_uses_monotonic_clock(self):
        # Defense-in-depth: clock can't be tampered with mid-probe.
        source = _PROBE_PATH.read_text()
        assert "time.monotonic()" in source
        # time.time should NOT be used for the timeout cap.
        # (Tighter check: the runner body specifically uses monotonic.)
        assert "time.time()" not in source
