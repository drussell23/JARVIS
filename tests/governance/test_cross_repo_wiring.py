"""Tests for cross_repo_wiring (G1 GENERATE block + G2 APPLY sandbox gate).

Covers:
  * build_blast_context_block returns the rendered block + fires the visualizer
    on a cross-repo ctx (fake oracle/registry);
  * build_blast_context_block returns "" when master-disabled (byte-identical);
  * build_blast_context_block returns "" when ctx is not cross-repo;
  * run_apply_sandbox_gate returns a fracture verdict on a fracture (fake gate);
  * run_apply_sandbox_gate is a no-op PASS sentinel when master-disabled.
"""
from __future__ import annotations

import asyncio
import types

import pytest

import backend.core.ouroboros.governance.cross_repo_master_flag as master_flag
import backend.core.ouroboros.governance.multi_repo.cross_repo_wiring as wiring
import backend.core.ouroboros.governance.saga.trinity_integration_gate as gate_mod
from backend.core.ouroboros.governance.saga.trinity_integration_gate import SandboxVerdict


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _FakeNode:
    def __init__(self, repo, file_path, name):
        self.repo = repo
        self.file_path = file_path
        self.name = name


class _FakeReport:
    def __init__(self, direct, trans=()):
        self.directly_affected = set(direct)
        self.transitively_affected = set(trans)


class _FakeOracle:
    def __init__(self, target_node, report):
        self._target = target_node
        self._report = report
        self.compute_calls = 0

    def find_nodes_in_file(self, path):
        return [self._target]

    def find_nodes_by_name(self, name):
        return [self._target]

    def compute_blast_radius(self, node_id, max_depth=None):
        self.compute_calls += 1
        return self._report


class _FakeRegistry:
    def __init__(self, sources):
        # sources: {(repo, file): "src"}
        self._sources = sources

    async def read_file(self, repo, path):
        return self._sources.get((repo, path), "def caller_a():\n    return 1\n")


class _Ctx:
    def __init__(self, *, cross_repo, target_files=("reactor/telemetry.py",),
                 target_symbol="TelemetryAdapter.emit"):
        self.cross_repo = cross_repo
        self.target_files = target_files
        self.target_symbol = target_symbol


def _arm(monkeypatch, armed: bool):
    """Force cross_repo_mutation_enabled() to a fixed value in BOTH the source
    module and the wiring module (imports are lazy from the source module)."""
    monkeypatch.setattr(master_flag, "cross_repo_mutation_enabled", lambda: armed)


# --------------------------------------------------------------------------- #
# G1 -- build_blast_context_block
# --------------------------------------------------------------------------- #
def test_blast_block_returns_block_and_fires_visualizer(monkeypatch, caplog):
    _arm(monkeypatch, True)
    target = _FakeNode("reactor", "reactor/telemetry.py", "TelemetryAdapter.emit")
    dependent = _FakeNode("jarvis", "backend/a/foo.py", "caller_a")
    oracle = _FakeOracle(target, _FakeReport(direct=[dependent]))
    registry = _FakeRegistry({})

    import logging
    caplog.set_level(logging.INFO, logger="Ouroboros.CrossRepoWiring")

    ctx = _Ctx(cross_repo=True)
    block = asyncio.run(
        wiring.build_blast_context_block(ctx, oracle=oracle, registry=registry)
    )
    # Rendered prompt block present, names the cross-repo dependent.
    assert "CROSS-REPO BLAST RADIUS" in block
    assert "caller_a" in block
    # The oracle blast-radius graph was consulted.
    assert oracle.compute_calls == 1
    # The visualizer fired to the operator surface (logger.info).
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "CROSS-REPO BLAST RADIUS" in joined
    assert "depended on by:" in joined


def test_blast_block_empty_when_master_disabled(monkeypatch):
    _arm(monkeypatch, False)
    target = _FakeNode("reactor", "reactor/telemetry.py", "TelemetryAdapter.emit")
    dependent = _FakeNode("jarvis", "backend/a/foo.py", "caller_a")
    oracle = _FakeOracle(target, _FakeReport(direct=[dependent]))
    ctx = _Ctx(cross_repo=True)
    block = asyncio.run(
        wiring.build_blast_context_block(ctx, oracle=oracle, registry=_FakeRegistry({}))
    )
    assert block == ""
    # Master OFF -> the oracle was never even consulted (byte-identical).
    assert oracle.compute_calls == 0


def test_blast_block_empty_when_not_cross_repo(monkeypatch):
    _arm(monkeypatch, True)
    target = _FakeNode("reactor", "reactor/telemetry.py", "TelemetryAdapter.emit")
    oracle = _FakeOracle(target, _FakeReport(direct=[]))
    ctx = _Ctx(cross_repo=False)
    block = asyncio.run(
        wiring.build_blast_context_block(ctx, oracle=oracle, registry=_FakeRegistry({}))
    )
    assert block == ""
    assert oracle.compute_calls == 0


# --------------------------------------------------------------------------- #
# G2 -- run_apply_sandbox_gate
# --------------------------------------------------------------------------- #
def test_sandbox_gate_returns_fracture_on_fracture(monkeypatch):
    _arm(monkeypatch, True)

    async def _fake_gate(*, candidate_root, op_id, runner=None):
        return SandboxVerdict(
            passed=False,
            fracture=True,
            reason="handshake_failed",
            air_gapped=True,
            handshake_ok=False,
            containers=("jarvis",),
        )

    monkeypatch.setattr(gate_mod, "run_trinity_sandbox_gate", _fake_gate)

    verdict = asyncio.run(
        wiring.run_apply_sandbox_gate(
            _Ctx(cross_repo=True), candidate_root="/tmp/cand", op_id="op-1"
        )
    )
    assert verdict.fracture is True
    assert verdict.passed is False
    assert verdict.reason == "handshake_failed"


def test_sandbox_gate_passes_through_pass_verdict(monkeypatch):
    _arm(monkeypatch, True)

    async def _fake_gate(*, candidate_root, op_id, runner=None):
        return SandboxVerdict(
            passed=True, fracture=False, reason="handshake_ok_air_gapped",
            air_gapped=True, handshake_ok=True, containers=("jarvis",),
        )

    monkeypatch.setattr(gate_mod, "run_trinity_sandbox_gate", _fake_gate)
    verdict = asyncio.run(
        wiring.run_apply_sandbox_gate(
            _Ctx(cross_repo=True), candidate_root="/tmp/cand", op_id="op-2"
        )
    )
    assert verdict.fracture is False
    assert verdict.passed is True


def test_sandbox_gate_noop_when_master_disabled(monkeypatch):
    _arm(monkeypatch, False)

    called = {"n": 0}

    async def _fake_gate(*, candidate_root, op_id, runner=None):
        called["n"] += 1
        return SandboxVerdict(False, True, "should-not-run", False, False, ())

    monkeypatch.setattr(gate_mod, "run_trinity_sandbox_gate", _fake_gate)
    verdict = asyncio.run(
        wiring.run_apply_sandbox_gate(
            _Ctx(cross_repo=True), candidate_root="/tmp/cand", op_id="op-3"
        )
    )
    # No-op PASS sentinel, the real gate was never invoked (byte-identical).
    assert verdict.passed is True
    assert verdict.fracture is False
    assert verdict.reason == "master_disabled"
    assert called["n"] == 0
