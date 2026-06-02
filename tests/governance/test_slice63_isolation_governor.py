"""Slice 63 — benchmark-isolation sensor gate.

Phase 3 soak (bt-2026-06-02-074655): autonomous sensors (OpportunityMiner
'torch is 7 versions behind', GitHubIssue '#65637') fired ops that competed with
the 2 injected swe_bench instances for the shared Claude budget while DW's
circuit was tripped, draining the $2 cap to $0 before either benchmark instance
could GENERATE — 0 scored rows.

Fix: a single benchmark-isolation gate. When ``JARVIS_BENCHMARK_ISOLATION_MODE``
is set, the intake layer suppresses ALL autonomous sensors so an injected
benchmark (swe_bench_pro is delivered by the harness boot hook — sensor-
INDEPENDENT) owns 100% of the execution + token budget. Composes the existing
``self._sensors`` collection (started via one loop + subscribed by the FS event
bridge from the same list) — no parallel governance manager.

Runbook Phase 2 (provider-aware budget Safe-Halt) is intentionally NOT built in
code: it ALREADY exists as the candidate_generator ``SessionBudgetPreflightRefused``
gate (verified live this run — it refused the over-budget Claude call rather than
burning through), and auto-RAISING a spend cap is a footgun. The soak script's
cap is made operator-overridable instead.
"""
from __future__ import annotations

import pathlib
import re

from backend.core.ouroboros.governance.intake import intake_layer_service as ils


def test_isolation_mode_default_off(monkeypatch):
    monkeypatch.delenv("JARVIS_BENCHMARK_ISOLATION_MODE", raising=False)
    assert ils.benchmark_isolation_mode() is False


def test_isolation_mode_truthy(monkeypatch):
    for v in ("true", "1", "yes", "on", "TRUE", "On"):
        monkeypatch.setenv("JARVIS_BENCHMARK_ISOLATION_MODE", v)
        assert ils.benchmark_isolation_mode() is True, v


def test_isolation_mode_falsy(monkeypatch):
    for v in ("false", "0", "no", "off", ""):
        monkeypatch.setenv("JARVIS_BENCHMARK_ISOLATION_MODE", v)
        assert ils.benchmark_isolation_mode() is False, v


def test_apply_isolation_suppresses_all_when_on(monkeypatch):
    monkeypatch.setenv("JARVIS_BENCHMARK_ISOLATION_MODE", "true")
    sensors = ["miner", "github", "testfail"]
    kept, n = ils.apply_benchmark_isolation(sensors)
    assert kept == [] and n == 3


def test_apply_isolation_noop_when_off(monkeypatch):
    # Default (no benchmark) — every sensor stays registered, byte-identical
    # to pre-Slice-63 behaviour.
    monkeypatch.delenv("JARVIS_BENCHMARK_ISOLATION_MODE", raising=False)
    sensors = ["miner", "github", "testfail"]
    kept, n = ils.apply_benchmark_isolation(sensors)
    assert kept == sensors and n == 0


def test_apply_isolation_empty_list_when_on(monkeypatch):
    monkeypatch.setenv("JARVIS_BENCHMARK_ISOLATION_MODE", "true")
    kept, n = ils.apply_benchmark_isolation([])
    assert kept == [] and n == 0


def test_soak_script_sets_isolation_and_overridable_cap():
    repo = pathlib.Path(__file__).resolve().parents[2]
    src = (repo / "scripts/swe_bench_pro_soak.sh").read_text()
    assert re.search(r"export\s+JARVIS_BENCHMARK_ISOLATION_MODE=true", src), (
        "soak script must enable benchmark isolation so sensor noise can't "
        "dilute the benchmark budget"
    )
    assert re.search(r'COST_CAP="\$\{COST_CAP:-', src), (
        "COST_CAP must be operator-overridable (DW-down -> Claude-only is "
        "expensive; $2 is too thin for a 106-file instance)"
    )
