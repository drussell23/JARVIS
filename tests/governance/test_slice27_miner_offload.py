"""Slice 27 — OpportunityMiner scan orchestration off-loop.

Soak bt-2026-07-15-214458 (the Slice 26 verdict run) beat the Oracle sweep
death class but left the miner as the dominant lag source: 41
ControlPlaneStarvation events (0.5–2s, peak 4.2s) all attributable to its
scan cycles. Root causes fixed here:
  * every 300s cycle re-scanned ~2.8k files even when NOTHING changed —
    the Slice 11.6.c merkle subtree short-circuit had been accidentally
    stripped by the 8f3be950a1 rewrite (its regression spine
    test_opp_miner_merkle.py failed collection ever since; restored);
  * the per-file loop had guaranteed yield SLOTS but no ADAPTIVE backoff —
    a loop-lag AIMD throttle (the Slice 26 Oracle-sweep shape, same class)
    now contracts cadence + backs off under control-plane lag;
  * Phase-2 candidate selection (full sort over all mined analyses with
    stratification-penalty property math) ran sync on-loop with zero
    awaits — now offloaded via the shared offload_blocking substrate;
  * the ast_compile_helper process pool joined the child_reaper cascade +
    worker_lifeline ppid-drift discipline (the fs-pool pattern).
"""
from __future__ import annotations

import inspect

import backend.core.ouroboros.governance.ast_compile_helper as ACH
import backend.core.ouroboros.governance.event_loop_governance as ELG
from backend.core.ouroboros.governance.intake.sensors import (
    opportunity_miner_sensor as oms,
)


# ── AIMD throttle wiring ─────────────────────────────────────────────


def test_scan_once_wires_aimd_throttle():
    src = inspect.getsource(oms.OpportunityMinerSensor.scan_once)
    assert "_AdaptiveIndexThrottle" in src
    assert "measure_loop_lag_ms" in src
    assert "_miner_backpressure_enabled" in src
    assert "backoff_s" in src


def test_miner_backpressure_env_knobs(monkeypatch):
    monkeypatch.delenv("JARVIS_MINER_BACKPRESSURE_ENABLED", raising=False)
    assert oms._miner_backpressure_enabled() is True
    monkeypatch.setenv("JARVIS_MINER_BACKPRESSURE_ENABLED", "0")
    assert oms._miner_backpressure_enabled() is False

    monkeypatch.setenv("JARVIS_MINER_LAG_PROBE_CEILING", "42")
    assert oms._miner_lag_probe_ceiling() == 42
    monkeypatch.setenv("JARVIS_MINER_LAG_PROBE_CEILING", "junk")
    assert oms._miner_lag_probe_ceiling() == 128

    monkeypatch.setenv("JARVIS_MINER_LAG_PROBE_FLOOR", "3")
    assert oms._miner_lag_probe_floor() == 3

    monkeypatch.setenv("JARVIS_MINER_BACKPRESSURE_LAG_MS", "75")
    assert oms._miner_backpressure_lag_ms() == 75.0
    monkeypatch.delenv("JARVIS_MINER_BACKPRESSURE_LAG_MS", raising=False)
    assert oms._miner_backpressure_lag_ms() == 50.0


async def test_measure_loop_lag_ms_shared_probe():
    lag = await ELG.measure_loop_lag_ms(probe_s=0.001)
    assert isinstance(lag, float) and lag >= 0.0


# ── Phase-2 selection offload ────────────────────────────────────────


def test_selection_sort_offloaded():
    src = inspect.getsource(oms.OpportunityMinerSensor.scan_once)
    assert "await offload_blocking(\n            self._select_diverse_candidates" in src


def test_slice12l_pins_still_hold():
    """The Slice 12L discipline survives Slice 27: scan_once still composes
    BOTH canonical primitives and never bare-walks on-loop."""
    src = inspect.getsource(oms.OpportunityMinerSensor.scan_once)
    assert "cooperative_yield_every_n_async" in src
    assert "offload_blocking" in src
    # The walk goes through the pruned iterator inside offload_blocking.
    # (The no-bare-rglob discipline itself is AST-pinned by the canonical
    # Slice 12L suite — not re-pinned here via fragile string matching.)
    assert "_iter_python_files_pruned" in src


# ── ast_compile_helper pool lifecycle (mandate 4) ────────────────────


def test_ast_pool_registers_child_reaper():
    src = inspect.getsource(ACH._get_pool)
    assert "register_cleanup" in src
    assert "reap_ast_pool_hard" in src
    assert "pool_worker_initializer" in src


def test_reap_ast_pool_hard_never_raises():
    # No pool exists in this test process — must be a clean no-op.
    ACH.reap_ast_pool_hard()


def test_reap_ast_pool_hard_registered_label():
    """The cleanup label matches the reaper-registry convention."""
    src = inspect.getsource(ACH._get_pool)
    assert 'label="ast_helper_pool"' in src


# ── sterile RT stimulus injector ─────────────────────────────────────


def _load_injector():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "inject_rt_stimulus", Path("scripts/inject_rt_stimulus.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_stimulus_inject_revert_lifecycle(tmp_path):
    inj = _load_injector()
    (tmp_path / "tests").mkdir()
    assert inj.inject(tmp_path) == 0
    probe = tmp_path / inj.PROBE_REL
    assert probe.exists() and inj.MARKER in probe.read_text()
    # double-inject refused
    assert inj.inject(tmp_path) == 2
    assert inj.revert(tmp_path) == 0
    assert not probe.exists()
    # revert on clean tree is a no-op success
    assert inj.revert(tmp_path) == 0


def test_stimulus_revert_refuses_foreign_file(tmp_path):
    inj = _load_injector()
    (tmp_path / "tests").mkdir()
    probe = tmp_path / inj.PROBE_REL
    probe.write_text("def test_real(): assert True\n")
    assert inj.revert(tmp_path) == 2
    assert probe.exists()  # untouched


def test_stimulus_probe_is_deterministically_red(tmp_path):
    """The probe body must fail by contract: the sentence_transformer
    estimate is a positive MB count so the == -1 assertion can never pass."""
    inj = _load_injector()
    assert 'COMPONENT_MEMORY_ESTIMATES.get("sentence_transformer") == -1' in inj.PROBE_BODY
    from backend.core.proactive_resource_guard import COMPONENT_MEMORY_ESTIMATES
    assert COMPONENT_MEMORY_ESTIMATES.get("sentence_transformer", 0) > 0


def test_stimulus_probe_imports_real_source_module():
    """Slice 6 attribution contract: the probe imports a real source module
    (direct_import resolution) so the signal carries a source locus."""
    inj = _load_injector()
    assert "from backend.core.proactive_resource_guard import" in inj.PROBE_BODY


def test_stimulus_probe_target_outside_the_cage():
    """Learned live (bt-2026-07-15-223446): an attribution target inside the
    risk engine's self-mod sentinels is BLOCKED pre-GENERATE
    (self_modification_unsanctioned_source) — the probe's imported module
    must sit outside 'ouroboros/governance/' and the kernel/security
    surfaces or the dispatch proof never fires."""
    inj = _load_injector()
    body_import_lines = [
        ln for ln in inj.PROBE_BODY.splitlines()
        if ln.startswith(("from ", "import "))
    ]
    assert body_import_lines, "probe must import a real source module"
    for ln in body_import_lines:
        assert "ouroboros.governance" not in ln
        assert "auth" not in ln
