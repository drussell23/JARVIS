"""Spine — DW adaptive heavy-probe TTFT + _stream_with_resilience
exception-retrieval hygiene.

Root (v18 bt-2026-05-16-175621): the static 30s heavy-probe TTFT
ceiling false-negative-graded the 35B–397B general DW models
(`done_before_content @ ttft_ms=30000`), collapsing the
``standard``-route DW catalog → Claude single point of failure →
Anthropic transient 5xx → total exhaustion. Plus an
`asyncio.leak: Task exception was never retrieved` on the Claude
stream-resilience task.

Pins:
  * **Adaptive TTFT** — `_adaptive_probe_timeout_s` invariants:
    Floor (None/≤0/tiny params → base, byte-identical), Ceiling
    (never above `_ttft_max_s`), Monotonic (non-decreasing in
    params), no hardcoded seconds (env-tunable; small-model
    behaviour unchanged).
  * **Threading** — probe()/_do_probe()/run_cycle accept the param;
    run_cycle maps model_id→param via the supplied map.
  * **Wiring (AST)** — dw_discovery_runner builds param_by_id via the
    canonical `parse_parameter_count` and passes it to run_cycle
    (leverage existing parser, no new data path).
  * **Leak hygiene (AST)** — providers.py attaches a done-callback
    that calls `.exception()` (retrieve, not await) on the stream
    task; the deliberate hard-kill visibility log is preserved.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import dw_heavy_probe as hp


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for v in (
        "JARVIS_TOPOLOGY_HEAVY_PROBE_TIMEOUT_S",
        "JARVIS_HEAVY_PROBE_TTFT_REF_PARAMS_B",
        "JARVIS_HEAVY_PROBE_TTFT_MAX_S",
    ):
        monkeypatch.delenv(v, raising=False)


# ---------------------------------------------------------------------------
# Adaptive TTFT invariants
# ---------------------------------------------------------------------------


def test_floor_none_and_tiny_params_equal_base():
    base = hp._probe_timeout_s()  # default 30
    assert hp._adaptive_probe_timeout_s(None) == base
    assert hp._adaptive_probe_timeout_s(0.0) == base
    assert hp._adaptive_probe_timeout_s(-5.0) == base
    # 4B model, ref=14 → multiplier clamps to 1.0 → base (strict)
    assert hp._adaptive_probe_timeout_s(4.0) == base


def test_large_model_scales_up_but_capped():
    base = hp._probe_timeout_s()       # 30
    cap = hp._ttft_max_s()             # 300
    # 397B / ref 14 ≈ 28.4× → 30*28.4 ≈ 852 → capped at 300
    out = hp._adaptive_probe_timeout_s(397.0)
    assert out == cap
    assert base < out <= cap
    # 35B / 14 = 2.5 → 30*2.5 = 75 (below cap, scaled)
    mid = hp._adaptive_probe_timeout_s(35.0)
    assert mid == pytest.approx(75.0)
    assert base < mid < cap


def test_monotonic_non_decreasing_in_params():
    vals = [
        hp._adaptive_probe_timeout_s(p)
        for p in (None, 1, 4, 14, 35, 70, 397, 1000)
    ]
    for a, b in zip(vals, vals[1:]):
        assert b >= a, f"non-monotonic: {vals}"
    assert vals[-1] > vals[0]


def test_ceiling_never_exceeded_even_absurd_params(monkeypatch):
    monkeypatch.setenv("JARVIS_HEAVY_PROBE_TTFT_MAX_S", "120")
    assert hp._adaptive_probe_timeout_s(99999.0) == 120.0


def test_env_tunable_no_hardcoded_seconds(monkeypatch):
    monkeypatch.setenv("JARVIS_TOPOLOGY_HEAVY_PROBE_TIMEOUT_S", "10")
    monkeypatch.setenv("JARVIS_HEAVY_PROBE_TTFT_REF_PARAMS_B", "7")
    monkeypatch.setenv("JARVIS_HEAVY_PROBE_TTFT_MAX_S", "500")
    # base=10, ref=7 → 70B/7=10× → 10*10=100
    assert hp._adaptive_probe_timeout_s(70.0) == pytest.approx(100.0)
    # tiny still floors to base=10
    assert hp._adaptive_probe_timeout_s(3.0) == 10.0


def test_invalid_env_falls_back_to_documented_defaults(monkeypatch):
    monkeypatch.setenv("JARVIS_HEAVY_PROBE_TTFT_REF_PARAMS_B", "garbage")
    monkeypatch.setenv("JARVIS_HEAVY_PROBE_TTFT_MAX_S", "nope")
    assert hp._ttft_ref_params_b() == 14.0
    assert hp._ttft_max_s() == 300.0
    # still functional
    assert hp._adaptive_probe_timeout_s(397.0) == 300.0


# ---------------------------------------------------------------------------
# Threading: probe / _do_probe / run_cycle accept the param
# ---------------------------------------------------------------------------


def test_signatures_accept_parameter_count_b():
    import inspect
    assert "parameter_count_b" in inspect.signature(
        hp.HeavyProber.probe
    ).parameters
    assert "parameter_count_b" in inspect.signature(
        hp.HeavyProber._do_probe
    ).parameters
    assert "param_by_id" in inspect.signature(
        hp.HeavyProbeScheduler.run_cycle
    ).parameters
    # Defaults preserve byte-identical legacy behaviour
    assert inspect.signature(
        hp.HeavyProber.probe
    ).parameters["parameter_count_b"].default is None
    assert inspect.signature(
        hp.HeavyProbeScheduler.run_cycle
    ).parameters["param_by_id"].default is None


# ---------------------------------------------------------------------------
# AST — discovery-runner wiring uses the canonical parser, no new path
# ---------------------------------------------------------------------------


def test_discovery_runner_wires_param_by_id_via_canonical_parser():
    src = (
        Path(hp.__file__).parents[0] / "dw_discovery_runner.py"
    ).read_text(encoding="utf-8")
    assert "parse_parameter_count" in src, (
        "must derive params from the canonical model-id parser"
    )
    assert "param_by_id=" in src, (
        "must pass param_by_id into run_cycle"
    )
    # parser call precedes the run_cycle invocation (source order)
    p = src.index("parse_parameter_count(")
    rc = src.index("run_cycle(", p)
    assert p < rc


# ---------------------------------------------------------------------------
# AST — providers.py leak hygiene
# ---------------------------------------------------------------------------


def test_providers_stream_task_exception_retrieved():
    src = (
        Path(hp.__file__).parents[0] / "providers.py"
    ).read_text(encoding="utf-8")
    # done-callback retrieves the exception (not await) on _stream_task
    assert "_stream_task.add_done_callback(" in src, (
        "stream task must have a done-callback to retrieve its "
        "exception (suppress GC 'never retrieved' warning)"
    )
    assert "_retrieve_stream_exc" in src
    # The callback calls .exception() (retrieval), and is placed
    # BEFORE the asyncio.wait so it covers the hard-kill pending path.
    cb = src.index("def _retrieve_stream_exc")
    seg = src[cb:cb + 320]
    assert "_t.exception()" in seg
    add = src.index("_stream_task.add_done_callback(")
    wait = src.index("await asyncio.wait(", add)
    assert add < wait, (
        "callback must be attached before the wait so it also "
        "covers the deliberate hard-kill severance path"
    )
    # The deliberate hard-kill visibility log is preserved.
    assert "HARD-KILL claude stream after" in src
