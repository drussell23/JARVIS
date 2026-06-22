"""Advisor integration tests for the Sovereign Call-Graph Risk Matrix (C1).

Proves the sharpening: a symbol-scoped op with few callers measures
call-graph blast BELOW threshold (not BLOCKED), while the SAME file without
a scope falls through to the file-level import scan (file-level high →
BLOCKED). Also proves OFF byte-identical (master off → file-level) and
fail-soft (oracle absent / unresolved → file-level).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, List

import pytest

from backend.core.ouroboros.governance import operation_advisor
from backend.core.ouroboros.governance.operation_advisor import (
    ADVISOR_CALLGRAPH_BLAST_ENABLED_ENV_VAR,
    ADVISOR_ORACLE_BLAST_ENABLED_ENV_VAR,
    OperationAdvisor,
    _BLAST_RADIUS_CACHE_SHARED,
    _advisor_callgraph_blast_enabled,
    extract_scoped_symbols,
    set_active_oracle,
)


@pytest.fixture(autouse=True)
def _reset_advisor_module_state():
    _BLAST_RADIUS_CACHE_SHARED.clear()
    set_active_oracle(None)
    prev_cg = os.environ.pop(ADVISOR_CALLGRAPH_BLAST_ENABLED_ENV_VAR, None)
    prev_oracle = os.environ.pop(ADVISOR_ORACLE_BLAST_ENABLED_ENV_VAR, None)
    yield
    _BLAST_RADIUS_CACHE_SHARED.clear()
    set_active_oracle(None)
    if prev_cg is not None:
        os.environ[ADVISOR_CALLGRAPH_BLAST_ENABLED_ENV_VAR] = prev_cg
    if prev_oracle is not None:
        os.environ[ADVISOR_ORACLE_BLAST_ENABLED_ENV_VAR] = prev_oracle


# ---------------------------------------------------------------------------
# Fake oracle
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FakeNode:
    name: str
    file_path: str

    def __str__(self) -> str:
        return f"jarvis:{self.file_path}:{self.name}"


class FakeOracle:
    def __init__(self, name_index, callers_map):
        self._name_index = name_index
        self._callers_map = callers_map

    def find_nodes_by_name(self, name, fuzzy=False):
        return list(self._name_index.get(name, []))

    def get_callers(self, node):
        return list(self._callers_map.get(str(node), []))


def _few_caller_oracle():
    target = FakeNode("tiny_helper", "semantic_index.py")
    callers = [FakeNode("c1", "a.py"), FakeNode("c2", "b.py")]
    return FakeOracle(
        name_index={"tiny_helper": [target]},
        callers_map={str(target): callers},
    )


def _hub_oracle():
    hub = FakeNode("hub", "semantic_index.py")
    callers = [FakeNode(f"c{i}", f"f{i}.py") for i in range(40)]
    return FakeOracle(
        name_index={"hub": [hub]},
        callers_map={str(hub): callers},
    )


# ---------------------------------------------------------------------------
# Master flag
# ---------------------------------------------------------------------------


def test_callgraph_master_defaults_off():
    assert ADVISOR_CALLGRAPH_BLAST_ENABLED_ENV_VAR not in os.environ
    assert _advisor_callgraph_blast_enabled() is False


# ---------------------------------------------------------------------------
# extract_scoped_symbols evidence parsing
# ---------------------------------------------------------------------------


def test_extract_scoped_symbols_happy():
    js = '{"scoped_symbols": ["x.py::A", "y.py::B"], "other": 1}'
    assert extract_scoped_symbols(js) == ("x.py::A", "y.py::B")


@pytest.mark.parametrize("js", [
    "", "not json", "[]", "null", '{"scoped_symbols": "notalist"}',
    '{"scoped_symbols": [1, 2]}', '{"scoped_symbols": ["", "  "]}',
    '{"no_key": true}',
])
def test_extract_scoped_symbols_degrades_to_empty(js):
    assert extract_scoped_symbols(js) == ()


# ---------------------------------------------------------------------------
# _maybe_symbol_blast_radius gating
# ---------------------------------------------------------------------------


def _advisor(tmp_path):
    return OperationAdvisor(tmp_path)


def test_no_scope_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv(ADVISOR_CALLGRAPH_BLAST_ENABLED_ENV_VAR, "true")
    monkeypatch.setenv(ADVISOR_ORACLE_BLAST_ENABLED_ENV_VAR, "true")
    set_active_oracle(_few_caller_oracle())
    adv = _advisor(tmp_path)
    assert adv._maybe_symbol_blast_radius(()) is None


def test_callgraph_master_off_returns_none(tmp_path, monkeypatch):
    # oracle master ON but call-graph master OFF → file-level (None).
    monkeypatch.setenv(ADVISOR_ORACLE_BLAST_ENABLED_ENV_VAR, "true")
    set_active_oracle(_few_caller_oracle())
    adv = _advisor(tmp_path)
    assert adv._maybe_symbol_blast_radius(
        ("semantic_index.py::tiny_helper",)
    ) is None


def test_oracle_master_off_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv(ADVISOR_CALLGRAPH_BLAST_ENABLED_ENV_VAR, "true")
    set_active_oracle(_few_caller_oracle())
    adv = _advisor(tmp_path)
    assert adv._maybe_symbol_blast_radius(
        ("semantic_index.py::tiny_helper",)
    ) is None


def test_no_oracle_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv(ADVISOR_CALLGRAPH_BLAST_ENABLED_ENV_VAR, "true")
    monkeypatch.setenv(ADVISOR_ORACLE_BLAST_ENABLED_ENV_VAR, "true")
    set_active_oracle(None)
    adv = _advisor(tmp_path)
    assert adv._maybe_symbol_blast_radius(
        ("semantic_index.py::tiny_helper",)
    ) is None


def test_few_callers_returns_low_count(tmp_path, monkeypatch):
    monkeypatch.setenv(ADVISOR_CALLGRAPH_BLAST_ENABLED_ENV_VAR, "true")
    monkeypatch.setenv(ADVISOR_ORACLE_BLAST_ENABLED_ENV_VAR, "true")
    set_active_oracle(_few_caller_oracle())
    adv = _advisor(tmp_path)
    assert adv._maybe_symbol_blast_radius(
        ("semantic_index.py::tiny_helper",)
    ) == 2


def test_oracle_raises_is_fail_soft(tmp_path, monkeypatch):
    monkeypatch.setenv(ADVISOR_CALLGRAPH_BLAST_ENABLED_ENV_VAR, "true")
    monkeypatch.setenv(ADVISOR_ORACLE_BLAST_ENABLED_ENV_VAR, "true")

    class Boom:
        def find_nodes_by_name(self, name, fuzzy=False):
            raise RuntimeError("boom")

        def get_callers(self, node):
            raise RuntimeError("boom")

    set_active_oracle(Boom())
    adv = _advisor(tmp_path)
    # never raises, returns None → file-level fallback
    assert adv._maybe_symbol_blast_radius(("x.py::y",)) is None


# ---------------------------------------------------------------------------
# _compute_blast_radius integration — scoped vs file-level
# ---------------------------------------------------------------------------


def test_compute_blast_radius_uses_callgraph_when_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv(ADVISOR_CALLGRAPH_BLAST_ENABLED_ENV_VAR, "true")
    monkeypatch.setenv(ADVISOR_ORACLE_BLAST_ENABLED_ENV_VAR, "true")
    set_active_oracle(_few_caller_oracle())
    adv = _advisor(tmp_path)
    # With a scope, blast = call-graph count (2), NOT a file scan.
    radius = adv._compute_blast_radius(
        ("semantic_index.py",),
        scoped_symbols=("semantic_index.py::tiny_helper",),
    )
    assert radius == 2


def test_compute_blast_radius_file_level_when_unscoped(tmp_path, monkeypatch):
    # No scope → call-graph path inert → file-level scan runs. On an empty
    # tmp tree the file-level scan resolves to 0 (no importers).
    monkeypatch.setenv(ADVISOR_CALLGRAPH_BLAST_ENABLED_ENV_VAR, "true")
    monkeypatch.setenv(ADVISOR_ORACLE_BLAST_ENABLED_ENV_VAR, "true")
    set_active_oracle(_few_caller_oracle())
    adv = _advisor(tmp_path)
    radius = adv._compute_blast_radius(("semantic_index.py",))
    assert radius == 0  # file-level on empty tree; call-graph not consulted


# ---------------------------------------------------------------------------
# advise() end-to-end — the C1 proof
# ---------------------------------------------------------------------------


def _BLAST_WARN():
    from backend.core.ouroboros.governance.operation_advisor import (
        _BLAST_RADIUS_WARN,
    )
    return _BLAST_RADIUS_WARN


def test_advise_symbol_scoped_few_callers_low_blast(tmp_path, monkeypatch):
    """C1 end-to-end: a symbol-scoped op with few callers measures
    call-graph blast BELOW the warn threshold → no high-blast reason."""
    monkeypatch.setenv(ADVISOR_CALLGRAPH_BLAST_ENABLED_ENV_VAR, "true")
    monkeypatch.setenv(ADVISOR_ORACLE_BLAST_ENABLED_ENV_VAR, "true")
    set_active_oracle(_few_caller_oracle())
    adv = _advisor(tmp_path)
    advisory = adv.advise(
        ("semantic_index.py",),
        "edit tiny_helper",
        "op-c1",
        scoped_symbols=("semantic_index.py::tiny_helper",),
    )
    assert advisory.blast_radius == 2
    assert advisory.blast_radius < _BLAST_WARN()
    assert not any("blast radius" in r.lower() for r in advisory.reasons)


def test_advise_hub_symbol_stays_high_blast(tmp_path, monkeypatch):
    """Invariant I1: a widely-called symbol still measures high — the
    gate is not weakened by the call-graph path."""
    monkeypatch.setenv(ADVISOR_CALLGRAPH_BLAST_ENABLED_ENV_VAR, "true")
    monkeypatch.setenv(ADVISOR_ORACLE_BLAST_ENABLED_ENV_VAR, "true")
    set_active_oracle(_hub_oracle())
    adv = _advisor(tmp_path)
    advisory = adv.advise(
        ("semantic_index.py",),
        "edit hub",
        "op-hub",
        scoped_symbols=("semantic_index.py::hub",),
    )
    assert advisory.blast_radius == 40
    assert advisory.blast_radius >= _BLAST_WARN()
    assert any("blast radius" in r.lower() for r in advisory.reasons)


def test_advise_reads_scope_from_evidence(tmp_path, monkeypatch):
    """The scope rides ctx.intake_evidence_json (the production wire)."""
    monkeypatch.setenv(ADVISOR_CALLGRAPH_BLAST_ENABLED_ENV_VAR, "true")
    monkeypatch.setenv(ADVISOR_ORACLE_BLAST_ENABLED_ENV_VAR, "true")
    set_active_oracle(_few_caller_oracle())
    adv = _advisor(tmp_path)
    advisory = adv.advise(
        ("semantic_index.py",),
        "edit tiny_helper",
        "op-ev",
        intake_evidence_json='{"scoped_symbols": ["semantic_index.py::tiny_helper"]}',
    )
    assert advisory.blast_radius == 2


def test_advise_off_byte_identical_when_master_off(tmp_path, monkeypatch):
    """OFF byte-identical: call-graph master OFF → file-level scan even
    with a scope present + a hub oracle that WOULD measure high."""
    # call-graph master intentionally NOT set; oracle master on.
    monkeypatch.setenv(ADVISOR_ORACLE_BLAST_ENABLED_ENV_VAR, "true")
    set_active_oracle(_hub_oracle())
    adv = _advisor(tmp_path)
    advisory = adv.advise(
        ("semantic_index.py",),
        "edit hub",
        "op-off",
        scoped_symbols=("semantic_index.py::hub",),
    )
    # File-level on empty tmp tree → 0, proving the call-graph path was
    # NOT taken (a hub would have measured 40).
    assert advisory.blast_radius == 0


@pytest.mark.asyncio
async def test_advise_async_threads_scope(tmp_path, monkeypatch):
    monkeypatch.setenv(ADVISOR_CALLGRAPH_BLAST_ENABLED_ENV_VAR, "true")
    monkeypatch.setenv(ADVISOR_ORACLE_BLAST_ENABLED_ENV_VAR, "true")
    set_active_oracle(_few_caller_oracle())
    adv = _advisor(tmp_path)
    advisory = await adv.advise_async(
        ("semantic_index.py",),
        "edit tiny_helper",
        "op-async",
        scoped_symbols=("semantic_index.py::tiny_helper",),
    )
    assert advisory.blast_radius == 2
