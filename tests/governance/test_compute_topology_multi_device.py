"""Two GPUs are two capacities, and only the serving stack knows which counts.

The gap this closes is REPRESENTATIONAL, not a bug. `compute_topology`
deliberately collapsed multi-GPU to the largest single device, with a sound
rationale: a model must fit on one device unless the serving stack shards it,
and summing would authorize a load that cannot physically land.

That reasoning is correct and stays the default. What was missing is the
ability to EXPRESS the other fact — that two devices exist and a sharding
runtime could pool them — so a consumer that knows its stack can ask.

Concretely: an RTX 5090 (32GiB) beside a 24GiB card is 32GiB to a stack that
does not shard and 56GiB to one that does. Reporting only one of those numbers
is wrong for somebody.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance import compute_topology as ct
from backend.core.ouroboros.governance import local_model_admission as lma

_MIB = 1024 * 1024
_GIB = 1024 * _MIB

#: The dual-GPU host this work exists for.
_SMI_DUAL = (
    "32768, 31000, NVIDIA GeForce RTX 5090\n"
    "24576, 23000, NVIDIA GeForce RTX 4090\n"
)


def _reading(devices, **kw):
    base = dict(
        topology=ct.MemoryTopology.DISCRETE,
        total_bytes=max((d.total_bytes for d in devices), default=0),
        free_bytes=max((d.free_bytes for d in devices), default=0),
        device_name=devices[0].name if devices else "",
        device_count=len(devices), source="test", resolved_class="test",
        enabled=True, probed_at=0.0, free_probed_at=0.0, devices=tuple(devices),
    )
    base.update(kw)
    return ct.ComputeReading(**base)


class TestTheParserKeepsEveryDevice:
    def test_both_cards_survive_the_parse(self):
        devices = ct._parse_nvidia_smi_devices(_SMI_DUAL)
        assert len(devices) == 2
        assert devices[0].total_bytes == 32768 * _MIB
        assert devices[1].total_bytes == 24576 * _MIB
        assert [d.index for d in devices] == [0, 1]

    def test_the_collapsed_view_is_unchanged(self):
        """The existing contract: largest SINGLE device, not the sum."""
        free, total, name, count = ct._parse_nvidia_smi(_SMI_DUAL)
        assert total == 32768 * _MIB      # not 57344
        assert count == 2
        assert "5090" in name

    def test_one_malformed_row_does_not_void_the_others(self):
        text = "32768, 31000, RTX 5090\ngarbage\n24576, 23000, RTX 4090\n"
        assert len(ct._parse_nvidia_smi_devices(text)) == 2

    def test_unparseable_input_is_still_none(self):
        assert ct._parse_nvidia_smi("garbage\n\n") is None
        assert ct._parse_nvidia_smi_devices("garbage\n\n") == ()


class TestBothCapacitiesAreExpressible:
    def test_the_aggregate_is_the_sum_and_the_collapsed_view_is_not(self):
        r = _reading(ct._parse_nvidia_smi_devices(_SMI_DUAL))
        assert r.free_bytes == 31000 * _MIB              # largest single
        assert r.aggregate_free_bytes == 54000 * _MIB    # pooled
        assert r.aggregate_total_bytes == 57344 * _MIB
        assert r.is_multi_device is True

    def test_no_devices_enumerated_is_not_multi_device(self):
        """Empty means 'could not enumerate', never 'one device' — and an
        aggregate over devices never read would be a fabrication."""
        r = _reading([], device_count=4)   # a count without an enumeration
        assert r.is_multi_device is False
        assert r.aggregate_free_bytes == 0

    def test_a_single_device_aggregate_equals_itself(self):
        d = ct.DeviceReading(index=0, name="one", total_bytes=32 * _GIB,
                             free_bytes=30 * _GIB)
        r = _reading([d])
        assert r.is_multi_device is False
        assert r.aggregate_free_bytes == r.free_bytes


class TestPoolingRequiresADeclaration:
    def test_the_default_stays_conservative(self):
        """Undeclared, the answer is the single-device bound — the number
        that holds no matter what the serving stack does."""
        r = _reading(ct._parse_nvidia_smi_devices(_SMI_DUAL))
        assert r.shardable_usable_bytes(sharding=False) == r.usable_bytes

    def test_declaring_sharding_unlocks_the_pool(self):
        r = _reading(ct._parse_nvidia_smi_devices(_SMI_DUAL))
        pooled = r.shardable_usable_bytes(sharding=True)
        assert pooled > r.usable_bytes
        assert pooled <= r.aggregate_free_bytes   # headroom still reserved

    def test_declaring_sharding_cannot_invent_a_second_card(self):
        """The flag is not a capacity multiplier. On a single-device host it
        must change nothing."""
        d = ct.DeviceReading(index=0, name="one", total_bytes=32 * _GIB,
                             free_bytes=30 * _GIB)
        r = _reading([d])
        assert r.shardable_usable_bytes(sharding=True) == r.usable_bytes

    def test_headroom_is_still_reserved_when_pooled(self):
        r = _reading(ct._parse_nvidia_smi_devices(_SMI_DUAL))
        assert r.shardable_usable_bytes(sharding=True) < r.aggregate_free_bytes


class TestTheAdmissionDeclaration:
    def test_it_defaults_off(self, monkeypatch):
        """Getting this wrong optimistically is the expensive direction: it
        admits a model that then fails to load, having passed the gate whose
        job was to prevent exactly that."""
        monkeypatch.delenv("JARVIS_LOCAL_ACCEL_SHARDING", raising=False)
        assert lma.accelerator_sharding_declared() is False

    @pytest.mark.parametrize("raw,expected",
                             [("1", True), ("true", True), ("ON", True),
                              ("0", False), ("no", False), ("", False),
                              ("garbage", False)])
    def test_it_reads_the_flag(self, monkeypatch, raw, expected):
        monkeypatch.setenv("JARVIS_LOCAL_ACCEL_SHARDING", raw)
        assert lma.accelerator_sharding_declared() is expected

    def test_the_admission_path_asks_the_reading_not_the_flag_alone(self):
        """Regression pin: the consult must go through
        `shardable_usable_bytes`, which re-checks `is_multi_device`. Reading
        the flag and summing directly would let a declaration on a
        single-GPU host fabricate capacity."""
        import ast
        import inspect
        import textwrap
        src = textwrap.dedent(inspect.getsource(lma))
        tree = ast.parse(src)
        # Both spellings: a direct `.attr` and the defensive
        # `getattr(reading, "attr", ...)` the module actually uses, so the
        # pin cannot be defeated by switching between them.
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        names |= {
            n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        }
        assert "shardable_usable_bytes" in names
        assert "aggregate_free_bytes" not in names, (
            "admission must not sum device memory itself")


class TestTheObservabilitySurface:
    def test_the_dict_carries_both_numbers(self):
        d = _reading(ct._parse_nvidia_smi_devices(_SMI_DUAL)).to_dict()
        assert d["is_multi_device"] is True
        assert d["free_bytes"] != d["aggregate_free_bytes"]
        assert len(d["devices"]) == 2
        assert d["devices"][0]["name"].endswith("5090")
