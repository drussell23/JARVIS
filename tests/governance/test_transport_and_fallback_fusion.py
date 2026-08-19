"""Network variance must not become inference telemetry, and a working
fallback must not read as an outage.

Two defects this closes, both introduced by the bridge itself:

  1. A dry paid lane resolved to BLOCKED even when the sovereign host was
     serving. Once BACKGROUND can run locally, an exhausted card is a COST
     condition, not a capability one — and a badge that cries outage while the
     organism works fine teaches the operator to stop reading it.
  2. The steady-state stall deadline is derived from model physics and
     computes to ~2.0s on a fast host. Over a Tailscale DERP relay a 2s gap
     between chunks is legitimate — WireGuard over TCP bunches packets — so
     the watchdog would sever healthy streams, write timeout penalties into
     the ledger for a host that was fine, and fail over on hotel wifi.
"""
from __future__ import annotations

import dataclasses

import pytest

from backend.core.ouroboros.governance import capability_state as cs
from backend.core.ouroboros.governance import inference_gateway as ig
from backend.core.ouroboros.governance import local_inference_director as lid
from backend.core.ouroboros.governance import transport_profile as tp

REMOTE = "http://100.64.1.20:11434"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    for k in ("JARVIS_REMOTE_INFERENCE_ENDPOINT", "JARVIS_TRANSPORT_PROFILE_ENABLED",
              "JARVIS_TRANSPORT_VARIANCE_K", "JARVIS_TRANSPORT_MIN_FLOOR_S",
              "JARVIS_CAPABILITY_TTL_S"):
        monkeypatch.delenv(k, raising=False)
    cs.reset_for_tests(); ig.reset_for_tests(); tp.reset_for_tests()
    yield
    cs.reset_for_tests(); ig.reset_for_tests(); tp.reset_for_tests()


def _ev(monkeypatch, *, dry, remote):
    e = cs.CapabilityEvaluator()
    monkeypatch.setattr(cs.CapabilityEvaluator, "_read_lanes",
                        staticmethod(lambda: (dry, "doubleword", True)))
    monkeypatch.setattr(cs.CapabilityEvaluator, "_read_ops",
                        staticmethod(lambda: (0, 0, 0, True)))
    monkeypatch.setattr(cs.CapabilityEvaluator, "_read_remote",
                        staticmethod(lambda: (remote, REMOTE, True)))
    return e


class TestAWorkingFallbackIsNotAnOutage:
    def test_dry_lane_with_a_serving_host_is_degraded(self, monkeypatch):
        r = _ev(monkeypatch, dry=True, remote="serving").evaluate()
        assert r.state is cs.Capability.DEGRADED
        assert "running local" in r.reason

    def test_dry_lane_with_no_remote_still_stops_dispatch(self, monkeypatch):
        """Asserts the SEMANTIC, not the enum member.

        This read `is cs.Capability.BLOCKED` and broke the moment UNFUNDED was
        added — a legitimate refinement, since money is the one blocker the
        organism cannot clear itself. `is_blocking` is the property that
        actually matters and survives the next refinement too.
        """
        r = _ev(monkeypatch, dry=True, remote="absent").evaluate()
        assert r.state.is_blocking and not r.state.can_work
        assert r.state is cs.Capability.UNFUNDED

    def test_dry_lane_with_an_unreachable_remote_stops_dispatch(self, monkeypatch):
        r = _ev(monkeypatch, dry=True, remote="unreachable").evaluate()
        assert r.state.is_blocking and not r.state.can_work
        assert "unreachable" in r.reason

    def test_a_configured_but_untried_host_counts_as_a_lane(self, monkeypatch):
        """Never-contacted is UNVERIFIED, not broken. Reporting BLOCKED would
        send the operator to buy credits they do not need."""
        monkeypatch.setenv("JARVIS_REMOTE_INFERENCE_ENDPOINT", REMOTE)
        assert cs.CapabilityEvaluator._read_remote()[0] == "serving"

    def test_the_capability_read_does_not_touch_the_network(self):
        """It runs on the render path. A badge that blocked the UI thread on a
        LAN round-trip would be a worse defect than the one it replaces.

        AST, not substring — for the third time in this codebase. Good code
        NAMES the thing it deliberately avoids, so `_read_remote`'s docstring
        says "does NOT call resident_models()" and a text search finds the
        explanation and fails a function that is in fact clean. A structural
        test over source must parse it.
        """
        import ast
        import inspect
        import textwrap
        tree = ast.parse(textwrap.dedent(
            inspect.getsource(cs.CapabilityEvaluator._read_remote)))
        called = {n.func.attr for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        assert "resident_models" not in called, "network call on the render path"
        assert "snapshot" in called


class TestBurstAwareTransportBudget:
    def test_a_relay_switch_widens_the_budget_within_a_few_chunks(self):
        """Mid-stream direct -> DERP. The budget must expand fast enough that
        the NEXT chunk is not judged by a LAN-derived deadline."""
        p = tp.profile_for("x")
        for _ in range(20):
            p.observe(0.4)
        before = p.reading().budget_ms
        for _ in range(6):
            p.observe(90.0)
        after = p.reading().budget_ms
        assert after > before * 20

    def test_recovery_contracts_slowly(self):
        """Asymmetric on purpose. Too loose delays detection — recoverable.
        Too tight severs a healthy stream and poisons the ledger for a host
        that was fine."""
        p = tp.profile_for("y")
        for _ in range(10):
            p.observe(90.0)
        peak = p.reading().srtt_ms
        for _ in range(10):
            p.observe(0.4)
        assert p.reading().srtt_ms > peak * 0.05   # still elevated

    def test_bursting_inflates_variance_not_just_the_mean(self):
        """Packet bunching is a VARIANCE event: 5 chunks instantly, then a
        long pause. `rttvar` is the term that must absorb it."""
        p = tp.profile_for("z")
        for _ in range(12):
            p.observe(5.0)
        calm = p.reading()
        for gap in (1.0, 1.0, 1.0, 1.0, 400.0) * 3:
            p.observe(gap)
        bursty = p.reading()
        assert bursty.rttvar_ms > calm.rttvar_ms * 10
        assert bursty.budget_ms > calm.budget_ms * 5

    def test_an_unmeasured_transport_contributes_nothing(self):
        assert tp.profile_for("never-seen").floor_s() == 0.0

    def test_the_floor_never_drops_below_its_minimum(self, monkeypatch):
        monkeypatch.setenv("JARVIS_TRANSPORT_MIN_FLOOR_S", "0.75")
        p = tp.profile_for("fast")
        for _ in range(20):
            p.observe(0.1)
        assert p.floor_s() >= 0.75

    def test_disabled_contributes_nothing(self, monkeypatch):
        monkeypatch.setenv("JARVIS_TRANSPORT_PROFILE_ENABLED", "0")
        p = tp.profile_for("w")
        for _ in range(20):
            p.observe(50.0)
        assert p.floor_s() == 0.0

    def test_it_never_raises_on_hostile_samples(self):
        p = tp.profile_for("hostile")
        for bad in (-1.0, float("nan"), float("inf"), 0.0):
            p.observe(bad)
        assert isinstance(p.reading().budget_ms, float)


class TestTransportClassKeyDimension:
    def test_two_transports_do_not_share_a_ledger_key(self):
        """Same silicon, two networks. Blending them would size lanes for a
        hotel-wifi session from home-LAN measurements."""
        cfg = dataclasses.replace(lid.LocalConfig.from_env(),
                                  model_name="m", num_ctx=8192)
        p = tp.profile_for(REMOTE)
        for _ in range(10):
            p.observe(0.4)
        near = lid.physics_key(cfg, endpoint=REMOTE)
        for _ in range(15):
            p.observe(150.0)
        far = lid.physics_key(cfg, endpoint=REMOTE)
        assert near != far
        assert near.endswith("m@8192") and far.endswith("m@8192")

    def test_an_unmeasured_transport_omits_the_dimension(self):
        """Empty rather than a guess: an unknown transport must not become a
        bucket that later real measurements are mixed into."""
        cfg = dataclasses.replace(lid.LocalConfig.from_env(),
                                  model_name="m", num_ctx=8192)
        tp.reset_for_tests()
        assert lid.physics_key(cfg, endpoint=REMOTE).count("@") == 2

    def test_the_class_has_hysteresis_at_boundaries(self):
        """A boundary-hugging RTT that flipped the class would shred one
        host's physics across two keys — the conflation the hardware axis
        exists to prevent, one level down."""
        p = tp.profile_for("edge")
        for _ in range(20):
            p.observe(14.0)          # just inside "lan" (bound 15.0)
        first = p.reading().transport_class
        for _ in range(3):
            p.observe(16.0)          # just over the boundary
        assert p.reading().transport_class == first


class TestTailnetBinding:
    def test_the_profile_binds_to_the_tailnet_not_the_world(self):
        """Ollama has NO AUTHENTICATION. `0.0.0.0` exposes model execution to
        whatever network the machine is on, including a café LAN."""
        import io as _io
        text = _io.open("deploy/local_tier_windows.env", encoding="utf-8").read()
        active = [l.strip() for l in text.splitlines()
                  if l.strip().startswith("OLLAMA_HOST=")]
        assert active, "no OLLAMA_HOST setting"
        assert not any(l.endswith("0.0.0.0:11434") for l in active)
        assert any("100." in l for l in active)

    def test_the_client_endpoint_is_a_tailnet_address(self):
        import io as _io
        text = _io.open("deploy/local_tier_windows.env", encoding="utf-8").read()
        line = next(l for l in text.splitlines()
                    if l.startswith("JARVIS_REMOTE_INFERENCE_ENDPOINT="))
        assert "100." in line
