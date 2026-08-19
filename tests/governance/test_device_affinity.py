"""Asymmetric GPUs are not interchangeable lanes.

A 32GB RTX 5090 beside a 24GB card, each hosting its own model. A queue that
treats them as equal is wrong in both directions: a long-context BACKGROUND op
on the 24GB card OOMs or silently truncates its window (a QUALITY failure that
looks like the model getting dumber), and a trivial SPECULATIVE op on the 32GB
card occupies the only device that could have taken the next heavy one.

The invariant these tests defend: **capacity is a constraint, affinity is a
preference, and a preference may never override a constraint.**
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance import device_affinity as da
from backend.core.ouroboros.governance import local_model_admission as lma
from backend.core.ouroboros.governance.compute_topology import DeviceReading

GIB = 1024 ** 3


@pytest.fixture(autouse=True)
def _kv(monkeypatch):
    # Qwen3.8-27B's measured rate, so the numbers below are realistic.
    monkeypatch.setenv("JARVIS_KV_BYTES_PER_TOKEN", "64000")
    monkeypatch.delenv("JARVIS_DEVICE_AFFINITY_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_KV_BYTES_PER_TOKEN_BY_MODEL", raising=False)
    yield


def _dev(i, name, total, free, uuid=""):
    return DeviceReading(index=i, name=name, total_bytes=total,
                         free_bytes=free, uuid=uuid or f"GPU-{i}")


BIG = _dev(0, "RTX 5090", 32 * GIB, 30 * GIB)
SMALL = _dev(1, "RTX 4090", 24 * GIB, 22 * GIB)
PAIR = [BIG, SMALL]
W = 18 * GIB          # ~a 27B model at Q4


class TestAffinitySteersByRoute:
    def test_light_triage_vacates_the_big_card(self):
        """A SPECULATIVE op that fits anywhere should not occupy the only
        device capable of taking the next heavy one."""
        sel = da.select_device(PAIR, ctx_tokens=2000, weight_bytes=W,
                               route="speculative")
        assert sel.device.name == "RTX 4090"

    @pytest.mark.parametrize("route", ["background", "complex", "standard",
                                       "immediate"])
    def test_heavy_routes_prefer_the_roomiest_device(self, route):
        sel = da.select_device(PAIR, ctx_tokens=2000, weight_bytes=W,
                               route=route)
        assert sel.device.name == "RTX 5090"

    def test_no_route_is_capacity_only(self):
        sel = da.select_device(PAIR, ctx_tokens=2000, weight_bytes=W)
        assert sel.reason == "most_free"

    def test_the_master_flag_off_is_capacity_only(self, monkeypatch):
        monkeypatch.setenv("JARVIS_DEVICE_AFFINITY_ENABLED", "0")
        sel = da.select_device(PAIR, ctx_tokens=2000, weight_bytes=W,
                               route="speculative")
        assert sel.reason == "most_free"
        assert sel.device.name == "RTX 5090"


class TestCapacityOverridesPreference:
    def test_a_light_op_too_big_for_the_small_card_still_runs(self):
        """Deferring work a present device could do would be policy defeating
        the system it exists to tune."""
        sel = da.select_device(PAIR, ctx_tokens=100_000, weight_bytes=W,
                               route="speculative")
        assert sel.device.name == "RTX 5090"
        assert sel.fallback_from_preference is True

    def test_the_displacement_is_visible_on_a_two_gpu_host(self):
        """Regression pin. `fallback_from_preference` was originally computed
        only in the multi-candidate path -- but with exactly two GPUs,
        excluding one leaves one, so that branch was never reached and the
        flag could never be True on the very configuration it was written
        for. The one event an operator wants to see was invisible."""
        sel = da.select_device(PAIR, ctx_tokens=100_000, weight_bytes=W,
                               route="speculative")
        assert sel.reason == "only_candidate"
        assert sel.fallback_from_preference is True

    def test_an_undisplaced_op_does_not_claim_it_was(self):
        sel = da.select_device(PAIR, ctx_tokens=2000, weight_bytes=W,
                               route="speculative")
        assert sel.fallback_from_preference is False


class TestTheDocumentedEdgeCases:
    def test_1_no_devices_declines_rather_than_guessing(self):
        """An empty list means "we could not see", never "there is nothing"."""
        sel = da.select_device([], ctx_tokens=1000, weight_bytes=W)
        assert sel.device is None and sel.reason == "not_enumerated"

    def test_2_a_single_device_still_gets_the_capacity_check(self):
        ok = da.select_device([SMALL], ctx_tokens=2000, weight_bytes=W,
                              route="background")
        assert ok.device is SMALL
        too_big = da.select_device([SMALL], ctx_tokens=200_000,
                                   weight_bytes=W, route="background")
        assert too_big.device is None

    def test_4_nothing_fits_returns_an_actionable_ceiling(self):
        sel = da.select_device(PAIR, ctx_tokens=400_000, weight_bytes=W,
                               route="background")
        assert sel.device is None and sel.reason == "no_device_fits"
        assert sel.max_ctx_tokens > 0
        # and the ceiling must actually fit
        assert da.select_device(PAIR, ctx_tokens=sel.max_ctx_tokens,
                                weight_bytes=W,
                                route="background").device is not None

    def test_5_ties_are_broken_deterministically_by_index(self):
        """Two identical cards must not flap between ops: a stable assignment
        keeps each device's model resident instead of thrashing loads, and a
        cold model load measured ~30s on this project's own hardware."""
        twins = [_dev(0, "A", 24 * GIB, 20 * GIB),
                 _dev(1, "B", 24 * GIB, 20 * GIB)]
        picks = {da.select_device(twins, ctx_tokens=2000, weight_bytes=W,
                                  route="background").device.index
                 for _ in range(25)}
        assert picks == {0}
        assert {da.select_device(list(reversed(twins)), ctx_tokens=2000,
                                 weight_bytes=W,
                                 route="background").device.index
                for _ in range(25)} == {0}

    def test_6_zero_context_is_not_treated_as_zero_kv(self):
        """A zero-KV assumption admits everything -- the optimistic direction,
        and therefore the wrong one."""
        assert da.estimate_kv_bytes(0) == 0
        sel = da.select_device(PAIR, ctx_tokens=0, weight_bytes=W,
                               route="background")
        assert sel.kv_bytes == 0   # caller must pass its real window

    def test_7_kv_is_sized_against_the_reachable_ceiling_not_the_prompt(self):
        """Venom accumulates tool results into the same window until
        compaction fires. An op sized against its FIRST prompt fits at round 1
        and OOMs at round 7 -- after the exploration calls the Iron Gate
        demanded have already been spent."""
        grown = da.estimate_kv_bytes(10_000, grow=True)
        raw = da.estimate_kv_bytes(10_000, grow=False)
        assert grown > raw
        assert grown == int(10_000 * da.context_growth_factor()) * 64000


class TestKvEstimation:
    def test_kv_is_linear_in_context(self):
        a = da.estimate_kv_bytes(8_000, grow=False)
        b = da.estimate_kv_bytes(32_000, grow=False)
        assert b == a * 4

    def test_a_per_model_rate_overrides_the_pessimistic_default(self,
                                                                monkeypatch):
        monkeypatch.setenv("JARVIS_KV_BYTES_PER_TOKEN_BY_MODEL",
                           "qwen3.8-27b=64000,other=1000")
        assert da.kv_bytes_per_token("qwen3.8-27b") == 64000
        assert da.kv_bytes_per_token("other") == 1000
        assert da.kv_bytes_per_token("unlisted") == 64000  # env default here

    def test_a_malformed_override_does_not_take_the_gate_down(self,
                                                              monkeypatch):
        monkeypatch.setenv("JARVIS_KV_BYTES_PER_TOKEN_BY_MODEL", "garbage,,x=")
        assert da.kv_bytes_per_token("anything") > 0

    def test_the_growth_factor_is_clamped(self, monkeypatch):
        for raw, lo, hi in (("0.1", 1.0, 4.0), ("99", 1.0, 4.0),
                            ("nonsense", 1.0, 4.0)):
            monkeypatch.setenv("JARVIS_DEVICE_AFFINITY_CTX_GROWTH", raw)
            assert lo <= da.context_growth_factor() <= hi


class TestAdmissionUsesTheSelectedDevice:
    def _reading(self, devices):
        from backend.core.ouroboros.governance import compute_topology as ct
        return ct.ComputeReading(
            topology=ct.MemoryTopology.DISCRETE,
            total_bytes=max(d.total_bytes for d in devices),
            free_bytes=max(d.free_bytes for d in devices),
            device_name=devices[0].name, device_count=len(devices),
            source="test", resolved_class="test", enabled=True,
            probed_at=0.0, free_probed_at=0.0, devices=tuple(devices))

    def test_a_context_no_device_can_hold_is_deferred_with_a_ceiling(
            self, monkeypatch):
        monkeypatch.setattr(lma, "_read_accelerator",
                            lambda: self._reading(PAIR))
        d = lma.assess(400_000, weight_bytes=W, model_id="m",
                       route="background")
        assert d.action == lma.Admission.DEFER.value
        assert d.bound == "accelerator_device"
        assert "largest context that fits" in d.reason

    def test_a_context_that_fits_is_judged_against_that_device(self,
                                                              monkeypatch):
        monkeypatch.setattr(lma, "_read_accelerator",
                            lambda: self._reading(PAIR))
        d = lma.assess(4_000, weight_bytes=W, model_id="m", route="background")
        assert d.action == lma.Admission.ADMIT.value

    def test_pooled_readings_skip_per_device_selection(self, monkeypatch):
        """One model spanning several devices has no per-device question to
        ask -- the pooled bound is the right one."""
        monkeypatch.setenv("JARVIS_LOCAL_ACCEL_SHARDING", "1")
        monkeypatch.setattr(lma, "_read_accelerator",
                            lambda: self._reading(PAIR))
        d = lma.assess(4_000, weight_bytes=W, model_id="m", route="background")
        assert d.bound != "accelerator_device"

    def test_the_route_argument_is_optional(self, monkeypatch):
        """Every existing caller passes no route and must keep working."""
        monkeypatch.setattr(lma, "_read_accelerator",
                            lambda: self._reading(PAIR))
        assert lma.assess(4_000, weight_bytes=W, model_id="m") is not None
