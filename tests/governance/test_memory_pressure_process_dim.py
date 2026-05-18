"""P5 Arc C Slices 5d/5e — process-tree dimension in MemoryPressureGate.

Pins:
  * flag-OFF → byte-identical legacy free-%-only path (additive
    fields None, no reason suffix, snapshot.process_tree.enabled=False)
  * usage-vs-cap level mapping (Amendment B) across the matrix
  * strictest-wins composition in BOTH directions
  * fail-open: a probe glitch never clamps (watchdog stays the hard
    stop; the gate only ever advises)
  * GRADUATION (5e): process-dim HIGH clamps fan-out while system
    free-% is OK — the gate relieves pressure BEFORE the watchdog
    must terminate (no soak needed; synthetic ramp is deterministic)
  * AST: gate composes the shared probe (no second impl); gate is
    NOT merged with the watchdog (no os._exit / watchdog import)
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import memory_pressure_gate as G
from backend.core.ouroboros.governance import process_tree_probe
from backend.core.ouroboros.governance.memory_pressure_gate import (
    MemoryPressureGate,
    MemoryProbe,
    PressureLevel,
)

_GATE_SRC = (
    Path(__file__).resolve().parents[2]
    / "backend/core/ouroboros/governance/memory_pressure_gate.py"
)
_DIM_FLAG = "JARVIS_MEMORY_PRESSURE_PROCESS_DIM_ENABLED"

_TOTAL_BYTES = 1000 * 1024 * 1024  # 1000 MB host (controlled)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in (
        _DIM_FLAG, "JARVIS_MEMORY_PRESSURE_PROCESS_FRACTION",
        "JARVIS_MEMORY_PRESSURE_PROCESS_WARN_FRAC",
        "JARVIS_MEMORY_PRESSURE_PROCESS_HIGH_FRAC",
        "JARVIS_MEMORY_PRESSURE_PROCESS_CRITICAL_FRAC",
    ):
        monkeypatch.delenv(k, raising=False)
    yield


def _gate(free_pct: float) -> MemoryPressureGate:
    return MemoryPressureGate(probe_fn=lambda: MemoryProbe(
        free_pct=free_pct, total_bytes=_TOTAL_BYTES,
        available_bytes=int(_TOTAL_BYTES * free_pct / 100.0),
        source="stub", ok=True,
    ))


def _stub_process(monkeypatch, rss_mb):
    """Cap = 1000MB * 0.5 = 500MB (PROCESS_FRACTION=0.5)."""
    monkeypatch.setenv(_DIM_FLAG, "true")
    monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_PROCESS_FRACTION", "0.5")
    monkeypatch.setattr(
        process_tree_probe, "probe_process_tree_rss_mb",
        lambda: rss_mb,
    )

    class _VM:
        total = _TOTAL_BYTES

    import psutil
    monkeypatch.setattr(psutil, "virtual_memory", lambda: _VM())


# ---------------------------------------------------------------------------
# Flag-off: byte-identical legacy
# ---------------------------------------------------------------------------

def test_flag_off_byte_identical_legacy():
    d = _gate(15.0).can_fanout(8)  # free_pct 15 → HIGH (free-only)
    assert d.level is PressureLevel.HIGH
    assert d.reason_code == "memory_pressure_gate.capped_to_3_at_high"
    assert d.process_level is None
    assert d.process_rss_mb is None and d.process_cap_mb is None
    assert d.dominant_dimension == "free_pct"
    snap = _gate(15.0).snapshot()
    assert snap["process_tree"]["enabled"] is False
    assert snap["process_tree"]["level"] is None


# ---------------------------------------------------------------------------
# Usage-vs-cap level matrix (cap = 500 MB)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rss_mb,expected", [
    (400.0, PressureLevel.OK),        # 0.80 < 0.85
    (430.0, PressureLevel.WARN),      # 0.86 >= 0.85
    (470.0, PressureLevel.HIGH),      # 0.94 >= 0.92
    (495.0, PressureLevel.CRITICAL),  # 0.99 >= 0.98
])
def test_process_dim_usage_vs_cap_levels(monkeypatch, rss_mb, expected):
    _stub_process(monkeypatch, rss_mb)
    lvl, rss, cap = _gate(100.0)._process_tree_dim()
    assert lvl is expected
    assert rss == rss_mb and cap == pytest.approx(500.0)


# ---------------------------------------------------------------------------
# Strictest-wins (both directions)
# ---------------------------------------------------------------------------

def test_strictest_wins_process_escalates(monkeypatch):
    _stub_process(monkeypatch, 470.0)          # process HIGH
    d = _gate(100.0).can_fanout(8)             # free OK
    assert d.level is PressureLevel.HIGH
    assert d.dominant_dimension == "process_tree"
    assert d.reason_code.endswith("_via_process_tree")
    assert d.process_level == "high"
    assert d.n_allowed == G.high_fanout_cap()  # clamped


def test_strictest_wins_free_dominates(monkeypatch):
    _stub_process(monkeypatch, 430.0)          # process WARN
    d = _gate(5.0).can_fanout(8)               # free CRITICAL
    assert d.level is PressureLevel.CRITICAL
    assert d.dominant_dimension == "free_pct"
    assert not d.reason_code.endswith("_via_process_tree")


def test_fail_open_on_probe_error(monkeypatch):
    monkeypatch.setenv(_DIM_FLAG, "true")

    def _boom():
        raise RuntimeError("probe glitch")

    monkeypatch.setattr(
        process_tree_probe, "probe_process_tree_rss_mb", _boom,
    )
    lvl, rss, cap = _gate(100.0)._process_tree_dim()
    assert lvl is PressureLevel.OK and rss is None and cap is None
    # free OK + process fail-open OK → no clamp at all
    d = _gate(100.0).can_fanout(8)
    assert d.n_allowed == 8 and d.dominant_dimension == "free_pct"


# ---------------------------------------------------------------------------
# 5e GRADUATION PROOF — process-dim HIGH clamps fan-out while free-% OK
# ---------------------------------------------------------------------------

def test_graduation_process_high_clamps_fanout_while_free_ok(monkeypatch):
    _stub_process(monkeypatch, 470.0)          # process HIGH
    d = _gate(100.0).can_fanout(8)             # system free-% perfect
    # The gate caught process-tree pressure the free-% probe is blind
    # to, and CLAMPED new fan-out (8 → high cap) BEFORE any hard stop.
    assert d.level is PressureLevel.HIGH
    assert 1 <= d.n_allowed < 8                 # clamped, still progresses
    assert d.allowed is True                    # advisory, never blocks
    assert d.dominant_dimension == "process_tree"
    # Gate is advisory only — it returns a decision, never terminates.
    assert isinstance(d, G.FanoutDecision)


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------

def test_ast_pin_gate_composes_shared_probe_no_second_impl():
    src = _GATE_SRC.read_text()
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_process_tree_dim"
    )
    body = ast.unparse(fn)
    assert "probe_process_tree_rss_mb" in body, "must compose shared probe"
    assert "children(recursive" not in body, "no inlined psutil tree-sum"
    assert "getrusage" not in body, "no inlined resource probe"
    # Flag-off short-circuit must be the FIRST gate (byte-identical).
    first = next(
        s for s in fn.body if not isinstance(s, ast.Expr)
    )
    assert "process_dim_enabled" in ast.unparse(first), (
        "_process_tree_dim must short-circuit on the master flag first"
    )


def test_ast_pin_gate_not_merged_with_watchdog():
    # Real "not merged" invariant: the gate must not TERMINATE and
    # must not depend on the harness/watchdog module. (Naming the
    # watchdog in a docstring to document the separation is desired,
    # so we test imports/calls, never the bare word.)
    src = _GATE_SRC.read_text()
    assert "os._exit" not in src, "gate must never terminate (≠ watchdog)"
    assert "_fire_process_memory_cap" not in src, "no watchdog fire path"
    tree = ast.parse(src)
    for node in ast.walk(tree):
        mod = None
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
        elif isinstance(node, ast.Import):
            mod = ",".join(a.name for a in node.names)
        if mod and "battle_test" in mod:
            pytest.fail(
                f"gate must not import the harness/watchdog module "
                f"({mod}) — gate is advisory, watchdog is authority"
            )
