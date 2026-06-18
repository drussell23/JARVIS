"""Tests for the Active Forensic Inoculation Engine + the OperationAdvisor flag alias."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, List

import pytest

from backend.core.ouroboros.governance.forensic_inoculation import (
    ForensicInoculationEngine,
    inoculation_enabled,
)


class _Graph:
    def find_nodes_in_file(self, f: str):
        return [SimpleNamespace(__str__=lambda s: "jarvis:mod.py:public_fn")] if f == "mod.py" else []
    # _interface uses str(n).split(":")[-1]; provide a node whose str() works:


class _Node:
    def __str__(self): return "jarvis:mod.py:public_fn"


class _GraphReal:
    def find_nodes_in_file(self, f: str):
        return [_Node()] if f == "mod.py" else []


def _adv(decision: str, volatility: float = 0.9):
    return SimpleNamespace(
        decision=SimpleNamespace(value=decision), git_volatility=volatility,
    )


def _git_ok():
    calls: List[Any] = []
    def _run(args):
        calls.append(args)
        return 0, "", ""
    return _run, calls


# --------------------------------------------------------------------------- gating
class TestGating:
    def test_default_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("JARVIS_FORENSIC_INOCULATION_ENABLED", raising=False)
        assert inoculation_enabled() is False

    @pytest.mark.asyncio
    async def test_disabled_no_trigger(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        monkeypatch.delenv("JARVIS_FORENSIC_INOCULATION_ENABLED", raising=False)
        eng = ForensicInoculationEngine(tmp_path, graph=_GraphReal(), git_runner=_git_ok()[0],
                                        probe_runner=lambda m, s: (True, ""))
        r = await eng.inoculate(_adv("block"), ("mod.py",), "op1")
        assert r.triggered is False


# --------------------------------------------------------------------------- trigger conditions
class TestTriggerConditions:
    @pytest.mark.asyncio
    async def test_recommend_not_triggered(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        monkeypatch.setenv("JARVIS_FORENSIC_INOCULATION_ENABLED", "true")
        eng = ForensicInoculationEngine(tmp_path, graph=_GraphReal(), git_runner=_git_ok()[0],
                                        probe_runner=lambda m, s: (True, ""))
        r = await eng.inoculate(_adv("recommend"), ("mod.py",), "op1")
        assert r.triggered is False

    @pytest.mark.asyncio
    async def test_low_volatility_not_hotspot(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        monkeypatch.setenv("JARVIS_FORENSIC_INOCULATION_ENABLED", "true")
        eng = ForensicInoculationEngine(tmp_path, graph=_GraphReal(), git_runner=_git_ok()[0],
                                        probe_runner=lambda m, s: (True, ""))
        r = await eng.inoculate(_adv("block", volatility=0.1), ("mod.py",), "op1")
        assert r.triggered is False  # not a hot-spot


# --------------------------------------------------------------------------- the three phases
class TestPhases:
    @pytest.mark.asyncio
    async def test_phase1_nondestructive_branch(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        monkeypatch.setenv("JARVIS_FORENSIC_INOCULATION_ENABLED", "true")
        run, calls = _git_ok()
        eng = ForensicInoculationEngine(tmp_path, graph=_GraphReal(), git_runner=run,
                                        probe_runner=lambda m, s: (True, ""))
        r = await eng.inoculate(_adv("caution"), ("mod.py",), "op-XYZ/123")
        assert r.forensic_branch == "forensic/saga_op-XYZ-123"
        # NON-destructive: only `git branch`, never `checkout`
        assert calls and calls[0][0] == "branch"
        assert not any("checkout" in c for c in calls)

    @pytest.mark.asyncio
    async def test_phase2_baseline_pass(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        monkeypatch.setenv("JARVIS_FORENSIC_INOCULATION_ENABLED", "true")
        eng = ForensicInoculationEngine(tmp_path, graph=_GraphReal(), git_runner=_git_ok()[0],
                                        probe_runner=lambda m, s: (True, "ok"))
        r = await eng.inoculate(_adv("block"), ("mod.py",), "op1")
        assert r.baseline_passed is True and r.locked is False and r.constraint_clause == ""

    @pytest.mark.asyncio
    async def test_phase3_baseline_fail_locks_and_constrains(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        monkeypatch.setenv("JARVIS_FORENSIC_INOCULATION_ENABLED", "true")
        eng = ForensicInoculationEngine(
            tmp_path, graph=_GraphReal(), git_runner=_git_ok()[0],
            probe_runner=lambda m, s: (False, "ImportError: cannot import name 'public_fn'"),
        )
        r = await eng.inoculate(_adv("block"), ("mod.py",), "op1")
        assert r.baseline_passed is False and r.locked is True
        assert "GATE LOCKED" in r.constraint_clause
        assert "ImportError" in r.constraint_clause
        assert "mod.py" in r.constraint_clause

    @pytest.mark.asyncio
    async def test_interface_mapped_from_graph(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        monkeypatch.setenv("JARVIS_FORENSIC_INOCULATION_ENABLED", "true")
        seen = {}
        def _probe(module, symbols):
            seen["module"] = module; seen["symbols"] = symbols
            return True, ""
        eng = ForensicInoculationEngine(tmp_path, graph=_GraphReal(), git_runner=_git_ok()[0],
                                        probe_runner=_probe)
        await eng.inoculate(_adv("caution"), ("mod.py",), "op1")
        assert seen["module"] == "mod"  # mod.py → dotted module
        assert "public_fn" in seen["symbols"]  # public symbol from the Oracle graph

    @pytest.mark.asyncio
    async def test_failsoft_git_failure(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        monkeypatch.setenv("JARVIS_FORENSIC_INOCULATION_ENABLED", "true")
        def _bad_git(args):
            return 1, "", "fatal: not a git repo"
        eng = ForensicInoculationEngine(tmp_path, graph=_GraphReal(), git_runner=_bad_git,
                                        probe_runner=lambda m, s: (True, ""))
        r = await eng.inoculate(_adv("block"), ("mod.py",), "op1")
        # branch failed but the engine still ran the probe (fail-soft), no raise
        assert r.forensic_branch == "" and r.baseline_passed is True


# --------------------------------------------------------------------------- advisor flag alias
class TestAdvisorAlias:
    def test_operation_advisor_alias_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import importlib
        monkeypatch.setenv("JARVIS_OPERATION_ADVISOR_ENABLED", "false")
        monkeypatch.setenv("JARVIS_ADVISOR_ENABLED", "true")
        import backend.core.ouroboros.governance.operation_advisor as oa
        importlib.reload(oa)
        assert oa._ENABLED is False  # alias overrides canonical
        # restore default for other tests
        monkeypatch.delenv("JARVIS_OPERATION_ADVISOR_ENABLED", raising=False)
        importlib.reload(oa)

    def test_canonical_default_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import importlib
        monkeypatch.delenv("JARVIS_OPERATION_ADVISOR_ENABLED", raising=False)
        monkeypatch.delenv("JARVIS_ADVISOR_ENABLED", raising=False)
        import backend.core.ouroboros.governance.operation_advisor as oa
        importlib.reload(oa)
        assert oa._ENABLED is True
