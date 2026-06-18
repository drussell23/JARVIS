"""Tests driving the Sovereign Chaos Injection & Verification Fabric end-to-end.

Proves the deterministic cross-repo self-healing loop converges over a mirrored workspace:
fault ingestion → fault coordinates → cross-repo scope promotion → structural gate → topo multi-file
apply — at zero production risk (canonical source untouched).
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from tests.governance.chaos_simulation_fabric import (
    ChaosSimulationFabric,
    ChaosWorkspace,
    FaultSeeder,
    run_stateful_l2_soak,
)


class TestWorkspaceNonDestructive:
    def test_mirror_writes_only_scratchpad(self) -> None:
        ws = ChaosWorkspace()
        try:
            p = ws.mirror("jarvis", "app.py", "def f(): pass\n")
            assert ws.root in p.parents          # lives under the ephemeral root
            assert str(p).startswith(tempfile.gettempdir())
            assert p.read_text().startswith("def f")
        finally:
            ws.cleanup()
            assert not ws.root.exists()           # fully ephemeral


class TestFaultSeeder:
    def test_interface_break(self, tmp_path: Path) -> None:
        f = tmp_path / "api.py"
        f.write_text("def compute(payload, mode):\n    return payload\n")
        FaultSeeder.interface_contract_break(f, "compute")
        assert "def compute(payload):" in f.read_text()

    def test_call_chain_severance(self, tmp_path: Path) -> None:
        f = tmp_path / "m.py"
        f.write_text("def shared_entry(x):\n    return x\n")
        FaultSeeder.call_chain_severance(f, "shared_entry")
        assert "_DISABLED_shared_entry" in f.read_text()


class TestFabricConvergence:
    @pytest.mark.asyncio
    async def test_interface_break_converges(self) -> None:
        fab = ChaosSimulationFabric()
        try:
            report = await fab.run(fault="interface_contract_break")
            print("\n" + report.render())
            assert report.stages.get("phase1_mirror") is True
            assert report.stages.get("phase2_fault") is True
            assert report.stages.get("phase3a_fault_coords") is True
            assert report.stages.get("phase3b_promotion") is True
            assert report.stages.get("phase3c_structural_gate") is True
            assert report.stages.get("phase3d_multifile_apply") is True
            assert report.converged is True
        finally:
            fab.workspace.cleanup()

    @pytest.mark.asyncio
    async def test_promotion_elevates_to_reactor(self) -> None:
        fab = ChaosSimulationFabric()
        try:
            report = await fab.run()
            assert "reactor" in report.detail["phase3b_promotion"]["elevated_scope"]
            assert report.detail["phase3b_promotion"]["boundary_edges"] >= 1
        finally:
            fab.workspace.cleanup()

    @pytest.mark.asyncio
    async def test_multifile_topo_order_reactor_first(self) -> None:
        fab = ChaosSimulationFabric()
        try:
            report = await fab.run()
            order = report.detail["phase3d_multifile_apply"]["apply_order"]
            # dependency-first: reactor endpoint applied before the jarvis caller
            assert order[0] == "reactor:reactor_core/api.py"
        finally:
            fab.workspace.cleanup()

    @pytest.mark.asyncio
    async def test_call_chain_severance_path(self) -> None:
        fab = ChaosSimulationFabric()
        try:
            report = await fab.run(fault="call_chain_severance")
            assert report.converged is True
        finally:
            fab.workspace.cleanup()


class TestStatefulAdversarialSoak:
    """Live-behavioral proof: a stateful mock provider drives the REAL RepairEngine._run_inner
    through stagnation → escalation → velocity → convergence. The CONTRAST is the proof."""

    @pytest.mark.asyncio
    async def test_escape_on_converges_through_stagnation(self) -> None:
        tel = await run_stateful_l2_soak(escape_enabled=True)
        # escalation ladder carried the staged stagnation all the way to convergence
        assert tel["terminal"] == "L2_CONVERGED"
        assert tel["converged"] is True
        assert tel["iterations"] >= 5

    @pytest.mark.asyncio
    async def test_escape_off_stops_at_no_progress(self) -> None:
        tel = await run_stateful_l2_soak(escape_enabled=False)
        # same staged stagnation, but without escape the flat early-stop fires
        assert tel["terminal"] == "L2_STOPPED"
        assert tel["stop_reason"] == "no_progress_streak"
        assert tel["converged"] is False

    @pytest.mark.asyncio
    async def test_escape_is_the_decisive_factor(self) -> None:
        on = await run_stateful_l2_soak(escape_enabled=True)
        off = await run_stateful_l2_soak(escape_enabled=False)
        # identical scenario; the ONLY difference is the flag → ON converges, OFF stops
        assert on["converged"] is True and off["converged"] is False
