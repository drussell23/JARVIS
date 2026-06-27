from __future__ import annotations

import importlib.util
import pathlib

_spec = importlib.util.spec_from_file_location(
    "rwd", pathlib.Path("scripts/run_wedge_diagnostics.py"))
rwd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rwd)


_BB_LINES = [
    "rss=812.0MB free=45.0% cpu=20.0% ctx=1500.0/s swap=0.0MB pageouts=10 disk_free=80.0%",
    "rss=2200.0MB free=8.0% cpu=95.0% ctx=42000.0/s swap=1200.0MB pageouts=999 disk_free=79.0%",
    "rss=-1.0MB free=-1.0% cpu=-1.0% ctx=-1.0/s swap=-1.0MB pageouts=0 disk_free=-1.0%",
]


def test_parse_blackbox_peaks_picks_worst_and_ignores_sentinels():
    p = rwd.parse_blackbox_peaks(_BB_LINES)
    assert p["peak_rss_mb"] == 2200.0
    assert p["min_free_pct"] == 8.0          # -1 sentinel ignored
    assert p["peak_cpu_pct"] == 95.0
    assert p["peak_ctx_rate"] == 42000.0
    assert p["peak_swap_mb"] == 1200.0
    assert p["peak_pageouts"] == 999.0
    assert p["samples"] == 3


def test_parse_blackbox_peaks_empty():
    p = rwd.parse_blackbox_peaks([])
    assert p["peak_rss_mb"] == 0.0
    assert p["min_free_pct"] == 100.0
    assert p["samples"] == 0


def test_summarize_autopsy_detects_rattle_and_peak_rss():
    body = (
        "=== RESOURCE-GOVERNOR PRE-OOM DEATH RATTLE ===\n"
        "--- thread stacks (faulthandler) ---\n"
        "Current thread 0x01 (most recent call first):\n"
        '  File "/x/y.py", line 5 in foo\n'
        "--- process-tree RSS ---\n"
        "4434 2100MB python3.11\n"
        "4500 320MB python3.11\n"
        "=== END DEATH RATTLE ===\n"
    )
    s = rwd.summarize_autopsy(body)
    assert s["rattle_fired"] is True
    assert s["peak_proc_rss_mb"] == 2100
    assert s["n_procs_in_snapshot"] == 2
    assert s["n_stack_frames"] >= 1


def test_summarize_autopsy_empty_means_no_rattle():
    s = rwd.summarize_autopsy("")
    assert s["rattle_fired"] is False
    assert s["peak_proc_rss_mb"] == 0


def test_detect_throttle_markers():
    log = ("[ResourceGovernor] REDLINE crit=True free=7.0% ...\n"
           "memory_pressure_gate.capped_to_3_at_critical\n")
    t = rwd.detect_throttle(log)
    assert t["redline_fired"] is True
    assert t["fanout_capped"] is True
    assert t["stagger_held"] is True   # "[ResourceGovernor]" present


def test_verdict_proven_when_run2_flattens():
    run1 = {
        "peaks": {"peak_rss_mb": 2200.0, "peak_ctx_rate": 42000.0,
                  "min_free_pct": 8.0, "peak_cpu_pct": 95.0, "peak_swap_mb": 1200.0},
        "autopsy": {"rattle_fired": True}, "stop_reason": "resource_governor_redline",
        "throttle": {},
    }
    run2 = {
        "peaks": {"peak_rss_mb": 1400.0, "peak_ctx_rate": 9000.0,
                  "min_free_pct": 30.0, "peak_cpu_pct": 60.0, "peak_swap_mb": 0.0},
        "autopsy": {"rattle_fired": False}, "stop_reason": "wall_clock_cap",
        "throttle": {"fanout_capped": True},
    }
    v = rwd.compute_verdict(run1, run2)
    assert v["conclusive"] is True
    assert "PROVEN" in v["verdict"]
    assert v["rss_drop_mb"] == 800.0
    assert v["throttle_engaged"] is True


def test_verdict_inconclusive_when_run1_never_wedged():
    flat = {"peak_rss_mb": 0.0, "peak_ctx_rate": 0.0, "min_free_pct": 100.0,
            "peak_cpu_pct": 0.0, "peak_swap_mb": 0.0}
    run1 = {"peaks": flat, "autopsy": {"rattle_fired": False},
            "stop_reason": "wall_clock_cap", "throttle": {}}
    run2 = {"peaks": flat, "autopsy": {"rattle_fired": False},
            "stop_reason": "skipped", "throttle": {}}
    v = rwd.compute_verdict(run1, run2)
    assert v["conclusive"] is False
    assert "INCONCLUSIVE" in v["verdict"]


def test_render_matrix_runs_without_error():
    run1 = {
        "peaks": {"peak_rss_mb": 2200.0, "peak_ctx_rate": 42000.0,
                  "min_free_pct": 8.0, "peak_cpu_pct": 95.0, "peak_swap_mb": 1200.0},
        "autopsy": {"rattle_fired": True}, "stop_reason": "resource_governor_redline",
        "throttle": {},
    }
    run2 = {
        "peaks": {"peak_rss_mb": 1400.0, "peak_ctx_rate": 9000.0,
                  "min_free_pct": 30.0, "peak_cpu_pct": 60.0, "peak_swap_mb": 0.0},
        "autopsy": {"rattle_fired": False}, "stop_reason": "wall_clock_cap",
        "throttle": {"fanout_capped": True},
    }
    v = rwd.compute_verdict(run1, run2)
    out = rwd.render_matrix(run1, run2, v)
    assert "VERDICT MATRIX" in out
    assert "RUN 1 (WEDGE)" in out and "RUN 2 (FEAST)" in out
    assert "PROVEN" in out
