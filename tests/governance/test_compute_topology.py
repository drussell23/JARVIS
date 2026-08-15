"""Regression spine for compute_topology + measured compute-class admission.

These tests assert BEHAVIOUR under substituted probes, not the spelling of
the module. Every probe stage is monkeypatched at its seam so the suite runs
identically on a 16 GB M1, a 32 GB discrete-GPU host, and a CI container with
no accelerator at all — which is the whole point of the module under test.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from backend.core.ouroboros.governance import compute_topology as ct

GIB = 1024 ** 3


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    """Master switch on, singleton clean, every knob at its default."""
    monkeypatch.setenv("JARVIS_COMPUTE_TOPOLOGY_ENABLED", "1")
    for knob in (
        "JARVIS_COMPUTE_TOPOLOGY_CLASS_BYTES",
        "JARVIS_COMPUTE_TOPOLOGY_HEADROOM_FRACTION",
        "JARVIS_COMPUTE_TOPOLOGY_UNIFIED_BUDGET_FRACTION",
        "JARVIS_COMPUTE_TOPOLOGY_FREE_TTL_S",
        "JARVIS_COMPUTE_TOPOLOGY_IDENTITY_REPIN_S",
        "JARVIS_COMPUTE_TOPOLOGY_LOCAL_ALIASES",
    ):
        monkeypatch.delenv(knob, raising=False)
    ct.reset_default_resolver()
    yield
    ct.reset_default_resolver()


def _discrete(total_gib=32, free_gib=30, name="NVIDIA GeForce RTX 5090"):
    return ct.AcceleratorProbe(
        topology=ct.MemoryTopology.DISCRETE,
        total_bytes=total_gib * GIB, free_bytes=free_gib * GIB,
        device_name=name, device_count=1, source="torch_cuda",
    )


def _silence_cascade(monkeypatch, *, torch=None, smi=None, unified=None,
                     ram=(0, 0)):
    """Pin every cascade stage. None means the stage DECLINES."""
    monkeypatch.setattr(ct, "_probe_torch_cuda", lambda: torch)
    async def _smi():
        return smi
    monkeypatch.setattr(ct, "_probe_nvidia_smi_async", _smi)
    monkeypatch.setattr(ct, "_probe_unified", lambda: unified)
    monkeypatch.setattr(ct, "_system_ram_from_canonical_gate", lambda: ram)


# ---------------------------------------------------------------------------
# The defect this module exists to close
# ---------------------------------------------------------------------------


def test_discrete_host_is_not_judged_by_system_ram(monkeypatch):
    """THE regression: 64 GB of host RAM must not authorize a 40 GiB load
    onto a 32 GiB card. This is the exact misjudgement the old path made."""
    _silence_cascade(monkeypatch, torch=_discrete(32, 30), ram=(64 * GIB, 60 * GIB))
    decision = asyncio.run(ct.fits(40 * GIB))
    assert decision.fits is False
    assert decision.reason_code == "exceeds_accelerator"
    assert decision.spill_bytes > 0


def test_unified_host_defers_to_the_canonical_ram_gate(monkeypatch):
    """Under unified memory the system-RAM reading IS the accelerator
    reading — and it must arrive via memory_pressure_gate, not a second probe."""
    calls = []

    def _ram():
        calls.append(1)
        return 16 * GIB, 8 * GIB

    monkeypatch.setattr(ct, "_probe_torch_cuda", lambda: None)
    async def _smi():
        return None
    monkeypatch.setattr(ct, "_probe_nvidia_smi_async", _smi)
    monkeypatch.setattr(ct, "_system_ram_from_canonical_gate", _ram)
    monkeypatch.setattr(ct.sys, "platform", "darwin")
    monkeypatch.setattr("platform.machine", lambda: "arm64")

    reading = asyncio.run(ct.resolve())
    assert reading.topology is ct.MemoryTopology.UNIFIED
    assert calls, "unified probe must consume the canonical RAM gate"
    # 75% budget fraction of 16 GiB, not the nameplate.
    assert reading.total_bytes == int(16 * GIB * 0.75)
    assert reading.free_is_measured is False


# ---------------------------------------------------------------------------
# Epistemic posture: unknown is not zero, and not "plenty"
# ---------------------------------------------------------------------------


def test_unknown_topology_refuses_rather_than_inventing(monkeypatch):
    _silence_cascade(monkeypatch, ram=(0, 0))
    reading = asyncio.run(ct.resolve())
    assert reading.topology is ct.MemoryTopology.UNKNOWN
    assert reading.measured is False
    decision = asyncio.run(ct.fits(1 * GIB))
    assert decision.fits is False
    assert decision.reason_code == "unknown_topology"


def test_no_accelerator_is_a_measurement_not_an_absence(monkeypatch):
    """NONE and UNKNOWN must stay distinguishable: one is a reading."""
    _silence_cascade(monkeypatch, ram=(32 * GIB, 24 * GIB))
    reading = asyncio.run(ct.resolve())
    assert reading.topology is ct.MemoryTopology.NONE
    assert reading.measured is True


def test_disabled_is_status_quo_not_degraded(monkeypatch):
    monkeypatch.setenv("JARVIS_COMPUTE_TOPOLOGY_ENABLED", "0")
    ct.reset_default_resolver()
    reading = asyncio.run(ct.resolve())
    assert reading.enabled is False
    assert reading.topology is ct.MemoryTopology.UNKNOWN


# ---------------------------------------------------------------------------
# Cascade discipline
# ---------------------------------------------------------------------------


def test_failed_stage_is_skipped_not_read_as_zero_capacity(monkeypatch):
    """A broken driver must not masquerade as a machine with no GPU."""
    broken = ct.AcceleratorProbe(
        topology=ct.MemoryTopology.UNKNOWN, total_bytes=0, free_bytes=0,
        device_name="", device_count=0, source="torch_cuda",
        ok=False, error="driver init failed",
    )
    _silence_cascade(monkeypatch, torch=broken, smi=_discrete(32, 31),
                     ram=(64 * GIB, 50 * GIB))
    reading = asyncio.run(ct.resolve())
    assert reading.topology is ct.MemoryTopology.DISCRETE
    assert reading.total_bytes == 32 * GIB
    assert any("driver init failed" in n for n in reading.notes)


def test_multi_gpu_resolves_largest_single_device_never_the_sum(monkeypatch):
    """A model must fit on ONE device; summing would authorize an
    impossible load."""
    text = "24576, 24000, NVIDIA L4\n32768, 32000, NVIDIA RTX 5090\n"
    parsed = ct._parse_nvidia_smi(text)
    assert parsed is not None
    free_b, total_b, name, count = parsed
    assert total_b == 32768 * 1024 * 1024
    assert count == 2
    assert "5090" in name


def test_unparseable_smi_rows_are_skipped_individually():
    text = "garbage\n32768, 32000, RTX 5090\n, , \n"
    parsed = ct._parse_nvidia_smi(text)
    assert parsed is not None
    assert parsed[1] == 32768 * 1024 * 1024


def test_smi_with_no_usable_rows_returns_none():
    assert ct._parse_nvidia_smi("garbage\n\n") is None


# ---------------------------------------------------------------------------
# Requirement interpretation — bytes, not ordinals
# ---------------------------------------------------------------------------


def test_legacy_class_names_resolve_to_byte_requirements():
    assert ct.bytes_for_requirement(min_compute_class="gpu_l4") == 24 * GIB
    assert ct.bytes_for_requirement(min_compute_class="gpu_t4") == 16 * GIB
    assert ct.bytes_for_requirement(min_compute_class="cpu") == 0


def test_explicit_vram_requirement_outranks_the_legacy_name():
    got = ct.bytes_for_requirement(min_compute_class="gpu_t4", min_vram_gb=27)
    assert got == 27 * GIB


def test_unknown_class_name_states_no_requirement_rather_than_denying():
    assert ct.bytes_for_requirement(min_compute_class="gpu_from_2031") == 0


def test_class_table_is_overridable(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_COMPUTE_TOPOLOGY_CLASS_BYTES", '{"gpu_l4": 48}',
    )
    assert ct.bytes_for_requirement(min_compute_class="gpu_l4") == 48 * GIB


def test_malformed_override_entry_does_not_void_the_table(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_COMPUTE_TOPOLOGY_CLASS_BYTES", '{"gpu_l4": "nonsense"}',
    )
    assert ct.bytes_for_requirement(min_compute_class="gpu_t4") == 16 * GIB


def test_resolved_class_names_the_measurement_not_the_nearest_rung(monkeypatch):
    """A 32 GiB consumer card must not be relabelled 'gpu_v100' merely
    because that legacy rung sits at 32 GiB."""
    _silence_cascade(monkeypatch, torch=_discrete(32, 30))
    reading = asyncio.run(ct.resolve())
    assert reading.resolved_class == "gpu_32gib"


# ---------------------------------------------------------------------------
# Headroom + spill
# ---------------------------------------------------------------------------


def test_headroom_is_withheld_from_every_fit(monkeypatch):
    _silence_cascade(monkeypatch, torch=_discrete(32, 30))
    reading = asyncio.run(ct.resolve())
    assert reading.usable_bytes == int(30 * GIB * 0.9)


def test_spill_can_rescue_a_fit_only_on_discrete(monkeypatch):
    _silence_cascade(monkeypatch, torch=_discrete(32, 30),
                     ram=(64 * GIB, 48 * GIB))
    decision = asyncio.run(ct.fits(40 * GIB, allow_spill=True))
    assert decision.fits is True
    assert decision.reason_code == "fits_with_spill"
    assert decision.spill_bytes > 0


def test_spill_cannot_rescue_a_fit_under_unified(monkeypatch):
    """There is nowhere for it to spill TO — the pool is already counted."""
    monkeypatch.setattr(ct, "_probe_torch_cuda", lambda: None)
    async def _smi():
        return None
    monkeypatch.setattr(ct, "_probe_nvidia_smi_async", _smi)
    monkeypatch.setattr(ct, "_system_ram_from_canonical_gate",
                        lambda: (16 * GIB, 12 * GIB))
    monkeypatch.setattr(ct.sys, "platform", "darwin")
    monkeypatch.setattr("platform.machine", lambda: "arm64")
    decision = asyncio.run(ct.fits(40 * GIB, allow_spill=True))
    assert decision.fits is False
    assert decision.reason_code == "no_spill_pool"


def test_spill_beyond_host_memory_is_refused(monkeypatch):
    _silence_cascade(monkeypatch, torch=_discrete(32, 30),
                     ram=(64 * GIB, 4 * GIB))
    decision = asyncio.run(ct.fits(120 * GIB, allow_spill=True))
    assert decision.fits is False
    assert decision.reason_code == "exceeds_host_memory"


# ---------------------------------------------------------------------------
# Caching: two clocks
# ---------------------------------------------------------------------------


def test_identity_is_pinned_and_only_free_bytes_re_probe(monkeypatch):
    """Device enumeration must not re-run on every question."""
    identity_calls = {"n": 0}

    def _torch():
        identity_calls["n"] += 1
        return _discrete(32, 30 - identity_calls["n"])

    _silence_cascade(monkeypatch, torch=None, ram=(64 * GIB, 60 * GIB))
    monkeypatch.setattr(ct, "_probe_torch_cuda", _torch)
    monkeypatch.setenv("JARVIS_COMPUTE_TOPOLOGY_FREE_TTL_S", "0")
    ct.reset_default_resolver()

    async def _drive():
        r1 = await ct.resolve()
        r2 = await ct.resolve()
        return r1, r2

    r1, r2 = asyncio.run(_drive())
    assert r1.total_bytes == r2.total_bytes == 32 * GIB
    # Second call refreshed the dynamic dimension only.
    assert identity_calls["n"] >= 2
    assert r2.free_bytes <= r1.free_bytes


def test_failed_free_refresh_keeps_the_host_resolved(monkeypatch):
    """A transient probe failure must not demote an already-known host."""
    state = {"n": 0}

    def _torch():
        state["n"] += 1
        if state["n"] == 1:
            return _discrete(32, 30)
        return ct.AcceleratorProbe(
            topology=ct.MemoryTopology.UNKNOWN, total_bytes=0, free_bytes=0,
            device_name="", device_count=0, source="torch_cuda",
            ok=False, error="transient",
        )

    _silence_cascade(monkeypatch, ram=(64 * GIB, 60 * GIB))
    monkeypatch.setattr(ct, "_probe_torch_cuda", _torch)
    monkeypatch.setenv("JARVIS_COMPUTE_TOPOLOGY_FREE_TTL_S", "0")
    ct.reset_default_resolver()

    async def _drive():
        await ct.resolve()
        return await ct.resolve()

    reading = asyncio.run(_drive())
    assert reading.topology is ct.MemoryTopology.DISCRETE
    assert reading.free_bytes == 30 * GIB


def test_concurrent_resolves_share_one_cascade(monkeypatch):
    calls = {"n": 0}

    async def _slow_smi():
        calls["n"] += 1
        await asyncio.sleep(0.01)
        return _discrete(32, 30, name="shared")

    monkeypatch.setattr(ct, "_probe_torch_cuda", lambda: None)
    monkeypatch.setattr(ct, "_probe_nvidia_smi_async", _slow_smi)
    monkeypatch.setattr(ct, "_system_ram_from_canonical_gate",
                        lambda: (64 * GIB, 60 * GIB))
    ct.reset_default_resolver()

    async def _drive():
        return await asyncio.gather(*(ct.resolve() for _ in range(8)))

    readings = asyncio.run(_drive())
    assert all(r.total_bytes == 32 * GIB for r in readings)
    assert calls["n"] == 1, "single-flight must collapse concurrent probes"


def test_sync_facade_refuses_to_block_a_running_loop(monkeypatch):
    """Blocking an event loop to answer a capability question is the
    failure class this codebase has spent slices removing."""
    _silence_cascade(monkeypatch, torch=_discrete(32, 30))

    async def _drive():
        return ct.resolve_sync()

    reading = asyncio.run(_drive())
    assert reading.degraded is True
    assert reading.topology is ct.MemoryTopology.UNKNOWN


def test_sync_facade_serves_the_cache_after_prewarm(monkeypatch):
    _silence_cascade(monkeypatch, torch=_discrete(32, 30))

    async def _warm():
        await ct.prewarm()

    asyncio.run(_warm())
    reading = ct.resolve_sync()
    assert reading.topology is ct.MemoryTopology.DISCRETE
    assert reading.total_bytes == 32 * GIB


# ---------------------------------------------------------------------------
# Locality — the remote-VM trap
# ---------------------------------------------------------------------------


def test_remote_capability_is_not_this_host():
    assert ct.describes_this_host({"host": "jarvis-brain-gcp-us-central1"}) is False


def test_absent_host_field_is_not_proof_of_locality():
    assert ct.describes_this_host({}) is False
    assert ct.describes_this_host(None) is False


def test_loopback_capability_is_this_host():
    assert ct.describes_this_host({"host": "127.0.0.1"}) is True
    assert ct.describes_this_host({"host": "localhost"}) is True


def test_local_aliases_are_extensible(monkeypatch):
    monkeypatch.setenv("JARVIS_COMPUTE_TOPOLOGY_LOCAL_ALIASES", "my-rig")
    assert ct.describes_this_host({"host": "my-rig"}) is True


# ---------------------------------------------------------------------------
# Admission integration
# ---------------------------------------------------------------------------


def test_admission_admits_a_32gib_host_for_an_l4_class_brain(monkeypatch):
    """The headline: a card no legacy rung can name passes an L4
    requirement, because 32 GiB > 24 GiB."""
    from backend.core.ouroboros.governance.governed_loop_service import (
        _check_compute_admission,
    )
    _silence_cascade(monkeypatch, torch=_discrete(32, 31))
    asyncio.run(ct.prewarm())
    _check_compute_admission(
        {"min_compute_class": "gpu_l4"},
        {"compute_class": "cpu", "host": "localhost"},
    )


def test_admission_denies_when_the_measured_host_is_too_small(monkeypatch):
    from backend.core.ouroboros.governance.governed_loop_service import (
        ComputeClassMismatch,
        _check_compute_admission,
    )
    _silence_cascade(monkeypatch, torch=_discrete(8, 7))
    asyncio.run(ct.prewarm())
    with pytest.raises(ComputeClassMismatch):
        _check_compute_admission(
            {"min_vram_gb": 27},
            {"compute_class": "gpu_a100", "host": "localhost"},
        )


def test_admission_uses_ordinal_path_for_a_remote_brain(monkeypatch):
    """A local 32 GiB card must NOT authorize a route to an undersized
    remote VM — the wrong-resource error in new clothes."""
    from backend.core.ouroboros.governance.governed_loop_service import (
        ComputeClassMismatch,
        _check_compute_admission,
    )
    _silence_cascade(monkeypatch, torch=_discrete(32, 31))
    asyncio.run(ct.prewarm())
    with pytest.raises(ComputeClassMismatch):
        _check_compute_admission(
            {"min_compute_class": "gpu_a100"},
            {"compute_class": "gpu_t4", "host": "jarvis-brain-gcp"},
        )


def test_admission_legacy_behaviour_is_unchanged_when_disabled(monkeypatch):
    from backend.core.ouroboros.governance.governed_loop_service import (
        ComputeClassMismatch,
        _check_compute_admission,
    )
    monkeypatch.setenv("JARVIS_COMPUTE_TOPOLOGY_ENABLED", "0")
    ct.reset_default_resolver()
    _check_compute_admission(
        {"min_compute_class": "gpu_t4"}, {"compute_class": "gpu_l4"},
    )
    with pytest.raises(ComputeClassMismatch):
        _check_compute_admission(
            {"min_compute_class": "gpu_a100"}, {"compute_class": "gpu_t4"},
        )


def test_admission_survives_a_module_level_fault(monkeypatch):
    """A measurement fault must fall back, never deny."""
    from backend.core.ouroboros.governance import governed_loop_service as gls

    def _boom(*_a, **_k):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(ct, "describes_this_host", _boom)
    gls._check_compute_admission(
        {"min_compute_class": "gpu_t4"}, {"compute_class": "gpu_l4"},
    )


# ---------------------------------------------------------------------------
# Observability + authority invariant
# ---------------------------------------------------------------------------


def test_prewarm_is_bounded_and_never_raises(monkeypatch):
    """A wedged driver costs the budget and nothing more — boot is never
    held open by a capability probe (§2 Progressive Awakening)."""
    async def _hang():
        await asyncio.sleep(30)
        return _discrete(32, 30)

    monkeypatch.setattr(ct, "_probe_torch_cuda", lambda: None)
    monkeypatch.setattr(ct, "_probe_nvidia_smi_async", _hang)
    monkeypatch.setattr(ct, "_system_ram_from_canonical_gate",
                        lambda: (64 * GIB, 60 * GIB))
    monkeypatch.setenv("JARVIS_COMPUTE_TOPOLOGY_PREWARM_BUDGET_S", "0.5")
    ct.reset_default_resolver()

    reading = asyncio.run(ct.prewarm())
    assert reading.degraded is True
    assert reading.measured is False


def test_boot_prewarms_topology_so_admission_is_not_inert():
    """THE inertness pin. ``_check_compute_admission`` runs inside a running
    loop, and ``resolve_sync`` refuses to block one. Without a boot prewarm
    the measured path can never engage — present, tested, never reached.
    This asserts the boot path still contains that call."""
    import inspect
    from backend.core.ouroboros.governance import governed_loop_service as gls

    src = inspect.getsource(gls)
    assert "compute_topology as _ct_boot" in src
    assert "_ct_boot.prewarm()" in src
    # And it must be awaited BEFORE the admission question is asked.
    assert src.index("_ct_boot.prewarm()") < src.index(
        "_check_compute_admission(_boot_brain_cfg, cap)",
    )


def test_measured_path_is_reachable_from_an_async_caller(monkeypatch):
    """Behavioural counterpart to the source pin above: after a prewarm,
    a caller inside a running loop gets the MEASURED verdict, not a
    degraded fallback."""
    from backend.core.ouroboros.governance.governed_loop_service import (
        ComputeClassMismatch,
        _check_compute_admission,
    )
    _silence_cascade(monkeypatch, torch=_discrete(8, 7))

    async def _boot_then_admit():
        await ct.prewarm()
        _check_compute_admission(
            {"min_vram_gb": 27}, {"compute_class": "gpu_a100", "host": "localhost"},
        )

    with pytest.raises(ComputeClassMismatch):
        asyncio.run(_boot_then_admit())


def test_snapshot_is_serialisable_and_carries_provenance(monkeypatch):
    import json
    _silence_cascade(monkeypatch, torch=_discrete(32, 30))
    snap = asyncio.run(ct.snapshot())
    json.dumps(snap)
    assert snap["source"] == "torch_cuda"
    assert snap["topology"] == "discrete"
    assert snap["schema_version"] == ct.COMPUTE_TOPOLOGY_SCHEMA_VERSION
    assert "free_age_s" in snap


def test_authority_invariant_no_forbidden_imports():
    """§1 — this module measures; it must never reach into governance."""
    import pathlib
    src = pathlib.Path(ct.__file__).read_text(encoding="utf-8")
    for banned in ("orchestrator", "iron_gate", "risk_tier", "change_engine",
                   "candidate_generator", "policy"):
        assert f"import {banned}" not in src
        assert f"from backend.core.ouroboros.governance.{banned}" not in src


def test_only_governance_import_is_the_canonical_ram_gate():
    """DRY, structurally pinned: system RAM has exactly one authority."""
    import pathlib
    import re
    src = pathlib.Path(ct.__file__).read_text(encoding="utf-8")
    imports = set(re.findall(
        r"from backend\.core\.ouroboros\.governance import (\w+)", src,
    )) | set(re.findall(
        r"from backend\.core\.ouroboros\.governance\.(\w+) import", src,
    ))
    assert imports == {"memory_pressure_gate"}, imports


# ---------------------------------------------------------------------------
# Driver-CLI discovery across launch contexts (the WSL2 / stripped-PATH class)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_smi_cache():
    ct.reset_nvidia_smi_cache()
    yield
    ct.reset_nvidia_smi_cache()


def _fake_bin(tmp_path, name="nvidia-smi", mode=0o755, kind="file"):
    p = tmp_path / name
    if kind == "dir":
        p.mkdir()
    elif kind == "deadlink":
        p.symlink_to(tmp_path / "does-not-exist")
    else:
        p.write_text("#!/bin/sh\necho stub\n")
        p.chmod(mode)
    return p


def test_path_is_searched_first_because_it_is_operator_intent(monkeypatch, tmp_path):
    """An operator who put a binary on PATH has been explicit; a fallback
    directory must never outrank that."""
    path_dir = tmp_path / "on_path"
    path_dir.mkdir()
    on_path = _fake_bin(path_dir)
    monkeypatch.setattr(ct.shutil, "which", lambda n: str(on_path))
    monkeypatch.setenv("JARVIS_COMPUTE_TOPOLOGY_SMI_DIRS", str(tmp_path))
    assert ct.resolve_nvidia_smi() == str(on_path)


def test_stripped_path_still_finds_the_driver_in_a_search_dir(monkeypatch, tmp_path):
    """THE regression: a systemd unit / cron job / launchd daemon starts with
    a sanitized PATH. The host is identical; the answer must not differ."""
    binary = _fake_bin(tmp_path)
    monkeypatch.setattr(ct.shutil, "which", lambda n: None)
    monkeypatch.setenv("JARVIS_COMPUTE_TOPOLOGY_SMI_DIRS", str(tmp_path))
    assert ct.resolve_nvidia_smi() == str(binary.resolve())


def test_wsl_stub_directory_is_searched_by_default():
    """WSL2 projects the driver into /usr/lib/wsl/lib from the login profile,
    not from the kernel — it must be a default candidate, not an env opt-in."""
    assert "/usr/lib/wsl/lib" in ct.nvidia_smi_search_dirs()


def test_windows_interop_path_is_ranked_last():
    """It answers correctly from inside WSL2 and crosses 9p to do it — it
    must never outrank a native binary."""
    dirs = ct.nvidia_smi_search_dirs()
    assert dirs[-1].startswith("/mnt/c"), dirs


def test_a_directory_named_like_the_binary_is_not_a_hit(monkeypatch, tmp_path):
    _fake_bin(tmp_path, kind="dir")
    monkeypatch.setattr(ct.shutil, "which", lambda n: None)
    monkeypatch.setenv("JARVIS_COMPUTE_TOPOLOGY_SMI_DIRS", str(tmp_path))
    assert ct.resolve_nvidia_smi() is None


def test_a_dangling_symlink_is_not_a_hit(monkeypatch, tmp_path):
    _fake_bin(tmp_path, kind="deadlink")
    monkeypatch.setattr(ct.shutil, "which", lambda n: None)
    monkeypatch.setenv("JARVIS_COMPUTE_TOPOLOGY_SMI_DIRS", str(tmp_path))
    assert ct.resolve_nvidia_smi() is None


def test_a_present_but_non_executable_file_is_not_a_hit(monkeypatch, tmp_path):
    _fake_bin(tmp_path, mode=0o644)
    monkeypatch.setattr(ct.shutil, "which", lambda n: None)
    monkeypatch.setenv("JARVIS_COMPUTE_TOPOLOGY_SMI_DIRS", str(tmp_path))
    assert ct.resolve_nvidia_smi() is None


def test_windows_exe_suffix_is_a_candidate_name():
    assert "nvidia-smi.exe" in ct.nvidia_smi_binary_names()


def test_search_dirs_are_fully_overridable(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_COMPUTE_TOPOLOGY_SMI_DIRS",
                       os.pathsep.join([str(tmp_path), "/nope"]))
    assert ct.nvidia_smi_search_dirs() == (str(tmp_path), "/nope")


def test_empty_override_entries_are_skipped(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_COMPUTE_TOPOLOGY_SMI_DIRS",
                       f"{os.pathsep}  {os.pathsep}{tmp_path}")
    assert ct.nvidia_smi_search_dirs() == (str(tmp_path),)


def test_a_deleted_binary_invalidates_the_cache(monkeypatch, tmp_path):
    """A package upgrade can delete the resolved path under a long-lived
    daemon; a stale hit would fail every probe naming the wrong cause."""
    binary = _fake_bin(tmp_path)
    monkeypatch.setattr(ct.shutil, "which", lambda n: None)
    monkeypatch.setenv("JARVIS_COMPUTE_TOPOLOGY_SMI_DIRS", str(tmp_path))
    assert ct.resolve_nvidia_smi() == str(binary.resolve())
    binary.unlink()
    assert ct.resolve_nvidia_smi() is None


def test_discovery_is_cached_not_re_walked(monkeypatch, tmp_path):
    calls = {"n": 0}
    real = ct._resolve_nvidia_smi_uncached

    def _counting():
        calls["n"] += 1
        return real()

    binary = _fake_bin(tmp_path)
    monkeypatch.setattr(ct.shutil, "which", lambda n: None)
    monkeypatch.setenv("JARVIS_COMPUTE_TOPOLOGY_SMI_DIRS", str(tmp_path))
    monkeypatch.setattr(ct, "_resolve_nvidia_smi_uncached", _counting)
    for _ in range(5):
        assert ct.resolve_nvidia_smi() == str(binary.resolve())
    assert calls["n"] == 1


def test_absent_driver_is_cached_as_absent(monkeypatch):
    calls = {"n": 0}

    def _counting():
        calls["n"] += 1
        return None

    monkeypatch.setattr(ct.shutil, "which", lambda n: None)
    monkeypatch.setenv("JARVIS_COMPUTE_TOPOLOGY_SMI_DIRS", "/nonexistent-xyz")
    monkeypatch.setattr(ct, "_resolve_nvidia_smi_uncached", _counting)
    for _ in range(3):
        assert ct.resolve_nvidia_smi() is None
    assert calls["n"] == 1, "a negative result must not re-walk the filesystem"


def test_resolution_never_raises_on_a_hostile_path(monkeypatch):
    """Inputs that can ACTUALLY arrive. A null byte is not among them —
    ``os.environ`` rejects one at assignment, so that scenario is
    unreachable rather than merely unhandled."""
    monkeypatch.setattr(ct.shutil, "which", lambda n: None)
    hostile = os.pathsep.join([
        "/proc/self/mem",          # a device-ish file, not a directory
        "/root/forbidden",         # permission denied for a normal user
        "relative/not/absolute",   # never resolves
        "x" * 4096,                # over PATH_MAX
    ])
    monkeypatch.setenv("JARVIS_COMPUTE_TOPOLOGY_SMI_DIRS", hostile)
    assert ct.resolve_nvidia_smi() is None


def test_discovery_runs_off_loop(monkeypatch, tmp_path):
    """Discovery stats candidate dirs, one of which may be a 9p or network
    mount where a stat blocks for seconds — it must not sit on the loop."""
    import inspect
    src = inspect.getsource(ct._probe_nvidia_smi_async)
    assert "to_thread(resolve_nvidia_smi)" in src


def test_the_binary_is_never_shell_invoked():
    """An operator-supplied search path must not become an injection
    surface."""
    import inspect
    src = inspect.getsource(ct._probe_nvidia_smi_async)
    assert "create_subprocess_shell" not in src
    assert "shell=True" not in src
