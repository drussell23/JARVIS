"""The stall deadline is derived, the stall is recorded, and the loop survives.

Two defects this closes, both invisible until the LAN bridge forced the
streaming path:

  1. **One constant for two different physics.** `_complete_streaming` applied
     a single 30s budget to BOTH the wait before the first chunk (which
     measures PREFILL and grows with prompt size) and every wait after it
     (which measures the inter-token gap and is roughly constant). At 220
     tok/s that hides a wedged peer for ~6,600 normal gaps; on a 64K prompt it
     can declare a healthy prefill "wedged".
  2. **A stall taught the ledger nothing.** `record()` fires only on a clean
     finish, so a host that wedges every op kept its optimistic EWMA forever
     and the ThroughputGovernor kept sizing lanes for a machine that was
     actively failing.
"""
from __future__ import annotations

import ast
import inspect
import os
import tempfile
import textwrap

import pytest

from backend.core.ouroboros.governance import inference_gateway as ig
from backend.core.ouroboros.governance import local_inference_director as lid


@pytest.fixture(autouse=True)
def _ledger(monkeypatch):
    monkeypatch.setenv("JARVIS_LATENCY_LEDGER_PATH",
                       os.path.join(tempfile.mkdtemp(), "l.json"))
    for k in ("JARVIS_LOCAL_INTER_TOKEN_ADAPTIVE_ENABLED",
              "JARVIS_LOCAL_INTER_TOKEN_STALL_MULTIPLE",
              "JARVIS_LOCAL_INTER_TOKEN_FLOOR_S",
              "JARVIS_LOCAL_INTER_TOKEN_TIMEOUT_S"):
        monkeypatch.delenv(k, raising=False)
    yield


def _profiler(**over):
    return lid.LatencyProfiler(lid.LocalConfig.from_env())


def _warm(p, *, ttft_ms, total_ms, out_tokens, n=8):
    for _ in range(n):
        p.record(ttft_ms=ttft_ms, total_ms=total_ms, output_tokens=out_tokens)
    return p


class TestTheBudgetIsDerived:
    def test_a_cold_profiler_is_byte_identical_legacy(self):
        """No measurement, no derivation. Guessing from an empty window would
        be worse than the constant it replaces."""
        static = lid._inter_token_timeout_s()
        assert _profiler().inter_token_budget_s() == (static, static)

    def test_a_fast_host_tightens_the_steady_deadline(self):
        """The defect: at 220 tok/s the normal gap is ~4.5ms, so a 30s budget
        gives a wedged peer thousands of normal gaps before anyone notices."""
        p = _warm(_profiler(), ttft_ms=180.0, total_ms=1000.0, out_tokens=200)
        first, steady = p.inter_token_budget_s()
        assert steady < lid._inter_token_timeout_s()
        assert steady >= lid._inter_token_floor_s()

    def test_a_large_prefill_loosens_the_first_token_deadline(self):
        """The mirror defect: the static budget was applied to the prefill
        wait too, so a 64K prompt whose first byte legitimately takes 40s was
        declared wedged while working normally."""
        p = _warm(_profiler(), ttft_ms=9000.0, total_ms=40000.0, out_tokens=100)
        first, _steady = p.inter_token_budget_s()
        assert first > lid._inter_token_timeout_s()

    def test_the_steady_deadline_can_only_tighten(self):
        """Capped at the static value: this path may detect a wedge SOONER,
        never later. Loosening it would weaken the guard it replaces."""
        for ttft, total, out in ((50.0, 200.0, 100), (9000.0, 90000.0, 20)):
            p = _warm(_profiler(), ttft_ms=ttft, total_ms=total, out_tokens=out)
            assert p.inter_token_budget_s()[1] <= lid._inter_token_timeout_s()

    def test_the_first_deadline_can_only_loosen(self):
        for ttft, total, out in ((50.0, 200.0, 100), (9000.0, 90000.0, 20)):
            p = _warm(_profiler(), ttft_ms=ttft, total_ms=total, out_tokens=out)
            assert p.inter_token_budget_s()[0] >= lid._inter_token_timeout_s()

    def test_a_floor_stops_jitter_being_read_as_a_wedge(self, monkeypatch):
        """Below the floor, ordinary LAN jitter and server-side chunk batching
        are indistinguishable from a stopped peer -- and a false positive
        aborts a real generation."""
        monkeypatch.setenv("JARVIS_LOCAL_INTER_TOKEN_FLOOR_S", "1.5")
        p = _warm(_profiler(), ttft_ms=5.0, total_ms=50.0, out_tokens=500)
        assert p.inter_token_budget_s()[1] >= 1.5

    def test_the_master_flag_off_restores_one_constant(self, monkeypatch):
        monkeypatch.setenv("JARVIS_LOCAL_INTER_TOKEN_ADAPTIVE_ENABLED", "0")
        p = _warm(_profiler(), ttft_ms=180.0, total_ms=1000.0, out_tokens=200)
        static = lid._inter_token_timeout_s()
        assert p.inter_token_budget_s() == (static, static)

    def test_no_hardcoded_network_parameter_survives(self, monkeypatch):
        """Every term is tunable; none is a literal in the decision path."""
        monkeypatch.setenv("JARVIS_LOCAL_INTER_TOKEN_TIMEOUT_S", "45")
        monkeypatch.setenv("JARVIS_LOCAL_INTER_TOKEN_STALL_MULTIPLE", "4")
        monkeypatch.setenv("JARVIS_LOCAL_INTER_TOKEN_FLOOR_S", "0.5")
        p = _warm(_profiler(), ttft_ms=100.0, total_ms=2000.0, out_tokens=100)
        first, steady = p.inter_token_budget_s()
        assert first >= 45.0 and 0.5 <= steady <= 45.0

    def test_a_broken_profiler_still_arms_a_watchdog(self, monkeypatch):
        """A watchdog that fails to arm is worse than a loose one."""
        p = _profiler()
        monkeypatch.setattr(type(p), "_mean",
                            staticmethod(lambda _xs: (_ for _ in ()).throw(
                                RuntimeError("boom"))))
        static = lid._inter_token_timeout_s()
        assert p.inter_token_budget_s() == (static, static)


class TestTheStallTeachesTheLedger:
    def test_the_streaming_path_penalises_before_it_raises(self):
        """Structural: `record()` fires only on a clean finish, so without an
        explicit penalty the EWMA never learns that this host wedges."""
        src = textwrap.dedent(
            inspect.getsource(lid.LocalPrimeClient._complete_streaming))
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)):
                continue
            if getattr(node.exc.func, "id", "") != "InterTokenStall":
                continue
            enclosing = [
                n for n in ast.walk(tree)
                if isinstance(n, (ast.If, ast.Try))
                and any(c is node for c in ast.walk(n))
            ]
            calls = {
                c.func.attr for n in enclosing for c in ast.walk(n)
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
            }
            assert "record_timeout_penalty" in calls
            return
        raise AssertionError("no InterTokenStall raise found")

    def test_a_penalty_raises_the_estimate_on_the_path_the_bridge_uses(self):
        """The behavioural half: a penalised profiler must ask for more time,
        which is what makes the governor throttle lanes on a degraded host.

        Exercised with `num_ctx` SET, because that is the branch a negotiated
        remote endpoint takes -- and it is the branch that consults the EWMA.
        """
        import dataclasses
        cfg = dataclasses.replace(lid.LocalConfig.from_env(), num_ctx=16384)
        p = _warm(lid.LatencyProfiler(cfg), ttft_ms=180.0, total_ms=1000.0,
                  out_tokens=200)
        before = p.adaptive_timeout_ms(prompt_tokens=8000)
        p.record_timeout_penalty(90_000.0)
        assert p.adaptive_timeout_ms(prompt_tokens=8000) > before

    def test_the_survival_path_is_deliberately_unmoved_by_a_penalty(self):
        """Documents a PRE-EXISTING design choice rather than asserting a bug.

        `adaptive_timeout_ms` has two branches, and the survival/CPU branch
        (no negotiated `num_ctx`) is explicitly "BYTE-IDENTICAL legacy -- no
        EWMA escalation". So the penalty is recorded there but not consulted.
        That is fortunate rather than accidental for this work: the LAN bridge
        negotiates `num_ctx`, so the host whose stalls must move the estimate
        is exactly the host whose branch reads it. Pinned so a future change
        to either branch has to confront the asymmetry deliberately.
        """
        import dataclasses
        cfg = dataclasses.replace(lid.LocalConfig.from_env(), num_ctx=None)
        p = _warm(lid.LatencyProfiler(cfg), ttft_ms=180.0, total_ms=1000.0,
                  out_tokens=200)
        before = p.adaptive_timeout_ms(prompt_tokens=8000)
        p.record_timeout_penalty(90_000.0)
        assert p.adaptive_timeout_ms(prompt_tokens=8000) == before

    def test_the_penalty_is_fail_soft(self):
        """Telemetry must never mask the stall it is describing.

        AST rather than a character window: the call sits inside a long
        explanatory comment block, and a substring search over a fixed slice
        found the comment instead of the code.
        """
        src = textwrap.dedent(
            inspect.getsource(lid.LocalPrimeClient._complete_streaming))
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            calls = {
                c.func.attr for c in ast.walk(node)
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
            }
            if "record_timeout_penalty" in calls and node.handlers:
                return
        raise AssertionError("record_timeout_penalty is not inside a try/except")


class TestTheDegradationEvent:
    def test_it_reuses_an_existing_event_type(self):
        """New SSE types must be in the canonical `_VALID_EVENT_TYPES` or they
        are SILENTLY DROPPED -- a degradation signal that degrades silently."""
        src = inspect.getsource(ig.InferenceGateway._publish_degraded)
        assert "publish_provider_state_changed" in src
        from backend.core.ouroboros.governance import (
            ide_observability_stream as s,
        )
        assert "provider_state_changed" in s._VALID_EVENT_TYPES

    def test_it_is_marked_non_fatal_and_names_the_fallback(self):
        src = inspect.getsource(ig.InferenceGateway._publish_degraded)
        assert '"fatal": False' in src and "local_triage" in src

    def test_publishing_cannot_turn_a_handled_fault_into_an_unhandled_one(self):
        """The op has already been re-routed by the time this runs."""
        g = ig.InferenceGateway()
        t = ig.GatewayTarget(base_url="http://h:1", model_name="m",
                             scope="remote", state=ig.HostState.DEGRADED,
                             reason="t")
        g._publish_degraded(t, RuntimeError("x"))   # must not raise
