"""Slice 34 — DW Capacity Investigation Substrate.

Tests the 4 substrate modules + probe + adaptive timeout composition
that resolve the v25→v29 capability blocker (DW 397B 100% TIMEOUT).

Substrate (per §48.7):
  * ``dw_capacity_ledger.py`` — per-call JSONL ledger
  * ``dw_per_shape_stats.py`` — rolling p50/p95/p99 aggregator
  * ``dw_adaptive_timeout.py`` — observation-driven timeout
  * ``dw_capacity_probe.py`` — out-of-band probe + hypothesis
    classifier

Test surface (6 AST pins + 20 spine = 26 tests).
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from typing import List
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LEDGER_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "dw_capacity_ledger.py"
)
STATS_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "dw_per_shape_stats.py"
)
TIMEOUT_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "dw_adaptive_timeout.py"
)
PROBE_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "dw_capacity_probe.py"
)
SCRIPT_FILE = REPO_ROOT / "scripts" / "dw_capacity_probe.py"


@pytest.fixture
def tmp_ledger_env(monkeypatch, tmp_path):
    """Isolated ledger per test."""
    p = tmp_path / "ledger.jsonl"
    monkeypatch.setenv("JARVIS_DW_CAPACITY_LEDGER_PATH", str(p))
    monkeypatch.setenv("JARVIS_DW_CAPACITY_LEDGER_ENABLED", "1")
    # Reset module singletons that cache the path
    from backend.core.ouroboros.governance import dw_capacity_ledger as L
    from backend.core.ouroboros.governance import dw_per_shape_stats as S
    L.reset_for_tests()
    S.reset_for_tests()
    yield p
    L.reset_for_tests()
    S.reset_for_tests()


# ──────────────────────────────────────────────────────────────────────
# AST PINS — 6
# ──────────────────────────────────────────────────────────────────────


def test_ast_pin_ledger_substrate_present() -> None:
    """``DWCapacityLedger`` + ``DWCallRecord`` + ``record_call`` async +
    schema versioned. Without these no observation."""
    src = LEDGER_FILE.read_text()
    tree = ast.parse(src, filename=str(LEDGER_FILE))
    found_class = False
    found_record = False
    found_async = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            if node.name == "DWCapacityLedger":
                found_class = True
                for sub in node.body:
                    if (
                        isinstance(sub, ast.AsyncFunctionDef)
                        and sub.name == "record_call"
                    ):
                        found_async = True
            if node.name == "DWCallRecord":
                found_record = True
    assert found_class, "DWCapacityLedger class missing"
    assert found_record, "DWCallRecord dataclass missing"
    assert found_async, "DWCapacityLedger.record_call must be async"
    assert 'LEDGER_SCHEMA_VERSION = "dw_capacity.1"' in src, (
        "schema version constant missing or wrong value"
    )


def test_ast_pin_ledger_composes_existing_flock_writer() -> None:
    """Ledger MUST write via ``cross_process_jsonl.flock_append_line``
    (Slice 33 Arc 2 Phase 2 instrumented surface). NO parallel
    write path — operator binding 'compose existing.'"""
    src = LEDGER_FILE.read_text()
    assert "flock_append_line" in src, (
        "ledger doesn't compose cross_process_jsonl.flock_append_line — "
        "duplicates the write path"
    )
    # AND it must dispatch off-loop
    tree = ast.parse(src, filename=str(LEDGER_FILE))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "record_call"
        ):
            body = ast.unparse(node)
            assert "to_thread" in body or "run_in_executor" in body, (
                "record_call must dispatch off-loop (to_thread/run_in_executor)"
            )
            return
    pytest.fail("record_call not found")


def test_ast_pin_stats_composes_ledger_no_parallel_storage() -> None:
    """``DWPerShapeStats`` MUST read from ``DWCapacityLedger``. NO
    parallel storage."""
    src = STATS_FILE.read_text()
    assert "DWCapacityLedger" in src
    assert "read_recent" in src, (
        "stats must read via DWCapacityLedger.read_recent (no parallel DB)"
    )


def test_ast_pin_adaptive_timeout_default_off() -> None:
    """``JARVIS_DW_ADAPTIVE_TIMEOUT_ENABLED`` default FALSE. Substrate
    ships before behaviour change; v30 graduation flips later."""
    from backend.core.ouroboros.governance.dw_adaptive_timeout import (
        is_enabled, _ENABLED_ENV,
    )
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop(_ENABLED_ENV, None)
        assert is_enabled() is False, "default must be FALSE (substrate-first)"
    for truthy in ("1", "true", "TRUE", "yes", "on"):
        with mock.patch.dict(os.environ, {_ENABLED_ENV: truthy}):
            assert is_enabled() is True


def test_ast_pin_adaptive_timeout_never_below_static_floor() -> None:
    """Adaptive timeout MUST never return less than ``static_floor_s``
    even when master flag is on and observed p99 is lower. This is
    the safety invariant — Slice 27's calibrated math is the floor."""
    src = TIMEOUT_FILE.read_text()
    tree = ast.parse(src, filename=str(TIMEOUT_FILE))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "compute_adaptive_timeout"
        ):
            body = ast.unparse(node)
            assert "max(static_floor_s" in body or "max(static_floor" in body, (
                "compute_adaptive_timeout must use max(static_floor, ...) — "
                "safety floor invariant"
            )
            return
    pytest.fail("compute_adaptive_timeout not located")


def test_ast_pin_probe_no_orchestrator_coupling() -> None:
    """Probe MUST NOT import orchestrator / sensor / intake modules —
    operator binding §48.7.2: 'isolate the variable.'"""
    src = PROBE_FILE.read_text()
    tree = ast.parse(src, filename=str(PROBE_FILE))
    forbidden_prefixes = (
        "backend.core.ouroboros.governance.orchestrator",
        "backend.core.ouroboros.governance.candidate_generator",
        "backend.core.ouroboros.governance.intake",
        "backend.core.ouroboros.governance.tool_executor",
        "backend.core.ouroboros.battle_test",
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module and any(
                node.module.startswith(p) for p in forbidden_prefixes
            ):
                pytest.fail(
                    f"probe imports forbidden coupling: {node.module}"
                )


# ──────────────────────────────────────────────────────────────────────
# Spine — 20
# ──────────────────────────────────────────────────────────────────────


def test_spine_ledger_record_call_writes_jsonl(tmp_ledger_env) -> None:
    from backend.core.ouroboros.governance.dw_capacity_ledger import (
        DWCallRecord, DWCapacityLedger,
    )

    async def run():
        ledger = DWCapacityLedger(path=tmp_ledger_env)
        ok = await ledger.record_call(DWCallRecord(
            timestamp_unix=time.time(),
            model_id="test/foo",
            route="standard",
            prompt_chars=100,
            outcome="ok",
            total_elapsed_ms=42.0,
            cost_usd=0.001,
        ))
        assert ok is True
        # File should exist with one line
        assert tmp_ledger_env.exists()
        lines = tmp_ledger_env.read_text().strip().split("\n")
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["model_id"] == "test/foo"
        assert parsed["outcome"] == "ok"
        assert parsed["schema_version"] == "dw_capacity.1"

    asyncio.run(run())


def test_spine_ledger_disabled_master_skips_write(monkeypatch, tmp_path) -> None:
    """Master flag OFF → record_call returns False without touching disk."""
    p = tmp_path / "ledger.jsonl"
    monkeypatch.setenv("JARVIS_DW_CAPACITY_LEDGER_PATH", str(p))
    monkeypatch.setenv("JARVIS_DW_CAPACITY_LEDGER_ENABLED", "0")
    from backend.core.ouroboros.governance import dw_capacity_ledger as L
    L.reset_for_tests()
    from backend.core.ouroboros.governance.dw_capacity_ledger import (
        DWCallRecord, DWCapacityLedger,
    )

    async def run():
        ledger = DWCapacityLedger(path=p)
        ok = await ledger.record_call(DWCallRecord(model_id="x"))
        assert ok is False
        assert not p.exists()

    asyncio.run(run())


def test_spine_ledger_read_recent_returns_records(tmp_ledger_env) -> None:
    from backend.core.ouroboros.governance.dw_capacity_ledger import (
        DWCallRecord, DWCapacityLedger,
    )

    async def run():
        ledger = DWCapacityLedger(path=tmp_ledger_env)
        for i in range(5):
            await ledger.record_call(DWCallRecord(
                timestamp_unix=time.time() + i,
                model_id=f"test/m{i}",
                route="standard",
                prompt_chars=100,
                outcome="ok",
                total_elapsed_ms=float(i * 10),
            ))
        recs = ledger.read_recent(limit=10)
        assert len(recs) == 5
        # Order preserved (oldest first)
        assert recs[0].model_id == "test/m0"
        assert recs[-1].model_id == "test/m4"

    asyncio.run(run())


def test_spine_ledger_read_recent_skips_malformed_lines(tmp_ledger_env) -> None:
    """Garbage lines mixed with valid JSONL — read_recent must skip
    malformed without raising."""
    # Write garbage + valid mix
    tmp_ledger_env.write_text(
        '{"model_id":"valid","outcome":"ok","total_elapsed_ms":1.0}\n'
        'not-json-at-all\n'
        '{"truncated": "json\n'
        '{"model_id":"v2","outcome":"timeout","total_elapsed_ms":2.0}\n'
    )
    from backend.core.ouroboros.governance.dw_capacity_ledger import (
        DWCapacityLedger,
    )
    ledger = DWCapacityLedger(path=tmp_ledger_env)
    recs = ledger.read_recent(limit=100)
    assert len(recs) == 2
    assert {r.model_id for r in recs} == {"valid", "v2"}


def test_spine_ledger_record_call_never_raises_on_internal_error(
    tmp_ledger_env, monkeypatch,
) -> None:
    """Force flock_append_line to raise; record_call must swallow."""
    from backend.core.ouroboros.governance import cross_process_jsonl
    monkeypatch.setattr(
        cross_process_jsonl, "flock_append_line",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("simulated")),
    )
    from backend.core.ouroboros.governance.dw_capacity_ledger import (
        DWCallRecord, DWCapacityLedger,
    )

    async def run():
        ledger = DWCapacityLedger(path=tmp_ledger_env)
        ok = await ledger.record_call(DWCallRecord(model_id="x"))
        assert ok is False  # but no raise

    asyncio.run(run())


def test_spine_ledger_aggregate_by_model_shape(tmp_ledger_env) -> None:
    from backend.core.ouroboros.governance.dw_capacity_ledger import (
        DWCallRecord, DWCapacityLedger,
    )

    async def run():
        ledger = DWCapacityLedger(path=tmp_ledger_env)
        # 10 records: 8 timeouts + 2 success on 397B/standard/20K
        for i in range(10):
            await ledger.record_call(DWCallRecord(
                timestamp_unix=time.time(),
                model_id="Qwen/Qwen3.5-397B-A17B-FP8",
                route="standard",
                prompt_chars=20480,
                outcome="ok" if i < 2 else "timeout",
                total_elapsed_ms=1000.0 if i < 2 else 75000.0,
            ))
        agg = ledger.aggregate_by_model_shape(window=100, prompt_chars_bucket=5000)
        assert len(agg) == 1
        key = list(agg.keys())[0]
        assert "Qwen/Qwen3.5-397B-A17B-FP8" in key
        assert agg[key]["count"] == 10
        assert agg[key]["success_rate"] == 0.2

    asyncio.run(run())


def test_spine_ledger_error_detail_truncated(tmp_ledger_env) -> None:
    from backend.core.ouroboros.governance.dw_capacity_ledger import (
        DWCallRecord, DWCapacityLedger,
    )

    async def run():
        ledger = DWCapacityLedger(path=tmp_ledger_env)
        long_detail = "x" * 10_000
        await ledger.record_call(DWCallRecord(
            model_id="x", outcome="error",
            error_detail=long_detail,
        ))
        recs = ledger.read_recent(1)
        assert len(recs) == 1
        # Truncated to 256 chars + "...[truncated]" suffix
        assert len(recs[0].error_detail) <= 300
        assert recs[0].error_detail.endswith("[truncated]")

    asyncio.run(run())


def test_spine_stats_returns_none_below_min_samples(
    tmp_ledger_env, monkeypatch,
) -> None:
    """Insufficient samples → None (caller falls through to static)."""
    monkeypatch.setenv("JARVIS_DW_PER_SHAPE_MIN_SAMPLES", "20")
    from backend.core.ouroboros.governance.dw_capacity_ledger import (
        DWCallRecord, DWCapacityLedger,
    )
    from backend.core.ouroboros.governance.dw_per_shape_stats import (
        DWPerShapeStats,
    )

    async def run():
        ledger = DWCapacityLedger(path=tmp_ledger_env)
        # Only 5 records — below min_samples=20
        for i in range(5):
            await ledger.record_call(DWCallRecord(
                model_id="x", route="standard", prompt_chars=100,
                outcome="ok", total_elapsed_ms=10.0,
            ))
        stats = DWPerShapeStats(ledger=ledger)
        result = stats.stats_for_shape(
            model_id="x", route="standard", prompt_chars=100,
        )
        assert result is None  # insufficient samples

    asyncio.run(run())


def test_spine_stats_computes_correct_percentiles(tmp_ledger_env) -> None:
    from backend.core.ouroboros.governance.dw_capacity_ledger import (
        DWCallRecord, DWCapacityLedger,
    )
    from backend.core.ouroboros.governance.dw_per_shape_stats import (
        DWPerShapeStats,
    )

    async def run():
        ledger = DWCapacityLedger(path=tmp_ledger_env)
        # 100 records, latency = 0..9900 in 100ms steps
        for i in range(100):
            await ledger.record_call(DWCallRecord(
                model_id="x", route="standard", prompt_chars=100,
                outcome="ok", total_elapsed_ms=float(i * 100),
            ))
        stats = DWPerShapeStats(ledger=ledger)
        result = stats.stats_for_shape(
            model_id="x", route="standard", prompt_chars=100,
        )
        assert result is not None
        assert result.sample_count == 100
        assert result.success_rate == 1.0
        # Nearest-rank: p50 idx ≈ 49, p95 idx ≈ 94, p99 idx ≈ 98
        assert 4500 <= result.p50_ms <= 5500
        assert 9000 <= result.p95_ms <= 9500
        assert 9700 <= result.p99_ms <= 9900

    asyncio.run(run())


def test_spine_stats_cache_ttl_honored(
    tmp_ledger_env, monkeypatch,
) -> None:
    """Cache rebuild only every TTL — verify second call within TTL
    sees same data even if file changes."""
    monkeypatch.setenv("JARVIS_DW_PER_SHAPE_MIN_SAMPLES", "1")
    monkeypatch.setenv("JARVIS_DW_PER_SHAPE_CACHE_TTL_S", "60")
    from backend.core.ouroboros.governance.dw_capacity_ledger import (
        DWCallRecord, DWCapacityLedger,
    )
    from backend.core.ouroboros.governance.dw_per_shape_stats import (
        DWPerShapeStats,
    )

    async def run():
        ledger = DWCapacityLedger(path=tmp_ledger_env)
        await ledger.record_call(DWCallRecord(
            model_id="x", route="standard", prompt_chars=100,
            outcome="ok", total_elapsed_ms=100.0,
        ))
        stats = DWPerShapeStats(ledger=ledger)
        r1 = stats.stats_for_shape(
            model_id="x", route="standard", prompt_chars=100,
        )
        # Add more — cache should NOT see them yet (TTL=60s)
        for _ in range(10):
            await ledger.record_call(DWCallRecord(
                model_id="x", route="standard", prompt_chars=100,
                outcome="ok", total_elapsed_ms=5000.0,
            ))
        r2 = stats.stats_for_shape(
            model_id="x", route="standard", prompt_chars=100,
        )
        # Cached — same as r1
        assert r2.sample_count == r1.sample_count
        # invalidate → fresh read
        stats.invalidate()
        r3 = stats.stats_for_shape(
            model_id="x", route="standard", prompt_chars=100,
        )
        assert r3.sample_count == 11

    asyncio.run(run())


def test_spine_adaptive_timeout_default_off_returns_static(
    monkeypatch,
) -> None:
    """Master flag OFF → static_floor_s returned unchanged regardless
    of stats."""
    monkeypatch.delenv("JARVIS_DW_ADAPTIVE_TIMEOUT_ENABLED", raising=False)
    from backend.core.ouroboros.governance.dw_adaptive_timeout import (
        compute_adaptive_timeout,
    )

    class FakeStats:
        def stats_for_shape(self, **kw):
            from backend.core.ouroboros.governance.dw_per_shape_stats import (
                ShapeStats,
            )
            return ShapeStats(
                model_id="x", route="standard", prompt_bucket=0,
                sample_count=100, success_rate=0.5,
                p50_ms=1000, p95_ms=5000, p99_ms=10000,
                ttft_p50_ms=500, last_observed_unix=time.time(),
            )

    t = compute_adaptive_timeout(
        model_id="x", route="standard", prompt_chars=100,
        static_floor_s=42.0, stats=FakeStats(),
    )
    assert t == 42.0  # static returned, no adaptive math


def test_spine_adaptive_timeout_master_on_raises_to_p99_safety(
    monkeypatch,
) -> None:
    """Master ON + stats present → max(static, p99×safety_factor)."""
    monkeypatch.setenv("JARVIS_DW_ADAPTIVE_TIMEOUT_ENABLED", "1")
    monkeypatch.setenv("JARVIS_DW_ADAPTIVE_TIMEOUT_SAFETY_FACTOR", "1.5")
    from backend.core.ouroboros.governance.dw_adaptive_timeout import (
        compute_adaptive_timeout,
    )
    from backend.core.ouroboros.governance.dw_per_shape_stats import (
        ShapeStats,
    )

    class FakeStats:
        def stats_for_shape(self, **kw):
            return ShapeStats(
                model_id="x", route="standard", prompt_bucket=0,
                sample_count=100, success_rate=0.5,
                p50_ms=1000, p95_ms=50000, p99_ms=80000,  # 80s p99
                ttft_p50_ms=500, last_observed_unix=time.time(),
            )

    # static=75s, p99=80s × 1.5 = 120s → max = 120s
    t = compute_adaptive_timeout(
        model_id="x", route="standard", prompt_chars=100,
        static_floor_s=75.0, stats=FakeStats(),
    )
    assert abs(t - 120.0) < 0.01


def test_spine_adaptive_timeout_never_below_static_safety(monkeypatch) -> None:
    """Even when observed p99 is LOW, static_floor is the floor."""
    monkeypatch.setenv("JARVIS_DW_ADAPTIVE_TIMEOUT_ENABLED", "1")
    from backend.core.ouroboros.governance.dw_adaptive_timeout import (
        compute_adaptive_timeout,
    )
    from backend.core.ouroboros.governance.dw_per_shape_stats import (
        ShapeStats,
    )

    class FakeStats:
        def stats_for_shape(self, **kw):
            return ShapeStats(
                model_id="x", route="standard", prompt_bucket=0,
                sample_count=100, success_rate=1.0,
                p50_ms=10, p95_ms=20, p99_ms=30,  # super fast
                ttft_p50_ms=5, last_observed_unix=time.time(),
            )

    t = compute_adaptive_timeout(
        model_id="x", route="standard", prompt_chars=100,
        static_floor_s=75.0, stats=FakeStats(),
    )
    assert t == 75.0  # static is the floor


def test_spine_adaptive_timeout_capped(monkeypatch) -> None:
    """Adaptive timeout MUST be bounded by JARVIS_DW_ADAPTIVE_TIMEOUT_CAP_S."""
    monkeypatch.setenv("JARVIS_DW_ADAPTIVE_TIMEOUT_ENABLED", "1")
    monkeypatch.setenv("JARVIS_DW_ADAPTIVE_TIMEOUT_CAP_S", "300")
    from backend.core.ouroboros.governance.dw_adaptive_timeout import (
        compute_adaptive_timeout,
    )
    from backend.core.ouroboros.governance.dw_per_shape_stats import (
        ShapeStats,
    )

    class FakeStats:
        def stats_for_shape(self, **kw):
            return ShapeStats(
                model_id="x", route="standard", prompt_bucket=0,
                sample_count=100, success_rate=0.0,
                p50_ms=300000, p95_ms=500000, p99_ms=900000,  # 900s p99
                ttft_p50_ms=0, last_observed_unix=time.time(),
            )

    t = compute_adaptive_timeout(
        model_id="x", route="standard", prompt_chars=100,
        static_floor_s=75.0, stats=FakeStats(),
    )
    assert t == 300.0  # capped


def test_spine_adaptive_timeout_fails_closed_on_stats_error(monkeypatch) -> None:
    """Stats raises → adaptive returns static_floor (never raises)."""
    monkeypatch.setenv("JARVIS_DW_ADAPTIVE_TIMEOUT_ENABLED", "1")
    from backend.core.ouroboros.governance.dw_adaptive_timeout import (
        compute_adaptive_timeout,
    )

    class RaisingStats:
        def stats_for_shape(self, **kw):
            raise RuntimeError("simulated")

    t = compute_adaptive_timeout(
        model_id="x", route="standard", prompt_chars=100,
        static_floor_s=42.0, stats=RaisingStats(),
    )
    assert t == 42.0


def test_spine_probe_runs_trials_against_fake_provider(tmp_ledger_env) -> None:
    """End-to-end probe with a fake provider — verify trials run +
    ledger records the trials."""
    from backend.core.ouroboros.governance.dw_capacity_ledger import (
        DWCapacityLedger,
    )
    from backend.core.ouroboros.governance.dw_capacity_probe import (
        DWCapacityProbe,
    )

    class FakeProvider:
        def __init__(self):
            self.calls: List[str] = []

        async def prompt_only(self, prompt: str, *, model_id: str = "") -> str:
            self.calls.append(prompt[:20])
            await asyncio.sleep(0.001)
            return "def compute_sum(): return 0"

    async def run():
        ledger = DWCapacityLedger(path=tmp_ledger_env)
        provider = FakeProvider()
        probe = DWCapacityProbe(provider=provider, ledger=ledger)
        results = await probe.probe(
            model_id="fake/m",
            prompt_sizes=[1000, 5000],
            trials_per_size=3,
        )
        assert len(results) == 2
        assert results[0].trials_run == 3
        assert results[0].successes == 3
        assert results[0].timeouts == 0
        # Ledger should have 6 records (2 sizes × 3 trials)
        recs = ledger.read_recent(20)
        assert len(recs) == 6
        for r in recs:
            assert r.route == "probe"
            assert r.outcome == "ok"

    asyncio.run(run())


def test_spine_probe_captures_timeouts(tmp_ledger_env) -> None:
    """Timeout outcomes correctly classified."""
    from backend.core.ouroboros.governance.dw_capacity_ledger import (
        DWCapacityLedger,
    )
    from backend.core.ouroboros.governance.dw_capacity_probe import (
        DWCapacityProbe,
    )

    class SlowProvider:
        async def prompt_only(self, prompt: str, *, model_id: str = "") -> str:
            await asyncio.sleep(2.0)  # > timeout_per_call_s
            return "x"

    async def run():
        ledger = DWCapacityLedger(path=tmp_ledger_env)
        provider = SlowProvider()
        probe = DWCapacityProbe(provider=provider, ledger=ledger)
        results = await probe.probe(
            model_id="fake/slow",
            prompt_sizes=[1000],
            trials_per_size=2,
            timeout_per_call_s=0.1,
        )
        assert results[0].timeouts == 2
        assert results[0].successes == 0
        recs = ledger.read_recent(10)
        for r in recs:
            assert r.outcome == "timeout"
            assert r.error_class == "TimeoutError"

    asyncio.run(run())


def test_spine_probe_never_raises_on_provider_error(tmp_ledger_env) -> None:
    """Provider raises → trial captures error, probe continues."""
    from backend.core.ouroboros.governance.dw_capacity_ledger import (
        DWCapacityLedger,
    )
    from backend.core.ouroboros.governance.dw_capacity_probe import (
        DWCapacityProbe,
    )

    class BrokenProvider:
        async def prompt_only(self, prompt: str, *, model_id: str = "") -> str:
            raise ValueError("provider exploded")

    async def run():
        ledger = DWCapacityLedger(path=tmp_ledger_env)
        provider = BrokenProvider()
        probe = DWCapacityProbe(provider=provider, ledger=ledger)
        results = await probe.probe(
            model_id="x", prompt_sizes=[1000], trials_per_size=2,
        )
        assert results[0].other_failures == 2
        recs = ledger.read_recent(10)
        assert all(r.error_class == "ValueError" for r in recs)

    asyncio.run(run())


def test_spine_hypothesis_classifier_size_correlated_failure() -> None:
    """Small succeeds + large fails → hypothesis (c) prompt complexity."""
    from backend.core.ouroboros.governance.dw_capacity_probe import (
        ProbeResult, classify_probe_results,
    )
    results = [
        ProbeResult(
            model_id="x", target_size=1024, trials_run=10,
            successes=10, timeouts=0, other_failures=0,
            p50_ms=300, p95_ms=500, p99_ms=600,
            max_ms=700, min_ms=200, avg_response_chars=400,
        ),
        ProbeResult(
            model_id="x", target_size=51200, trials_run=10,
            successes=0, timeouts=10, other_failures=0,
            p50_ms=60000, p95_ms=60000, p99_ms=60000,
            max_ms=60000, min_ms=60000, avg_response_chars=0,
        ),
    ]
    v = classify_probe_results(results)
    assert v["hypothesis"] == "c"
    assert v["confidence"] >= 0.7
    assert "prompt" in v["reasoning"].lower() or "size" in v["reasoning"].lower()


def test_spine_hypothesis_classifier_all_succeed_flags_harness() -> None:
    """All sizes succeed → not an endpoint issue; harness is variable."""
    from backend.core.ouroboros.governance.dw_capacity_probe import (
        ProbeResult, classify_probe_results,
    )
    results = [
        ProbeResult(
            model_id="x", target_size=s, trials_run=10,
            successes=10, timeouts=0, other_failures=0,
            p50_ms=500, p95_ms=800, p99_ms=1000,
            max_ms=1100, min_ms=300, avg_response_chars=400,
        )
        for s in (1024, 5120, 20480, 51200)
    ]
    v = classify_probe_results(results)
    assert v["hypothesis"] == "harness_variable"
    assert v["confidence"] >= 0.7


def test_spine_hypothesis_classifier_all_fail_fast_flags_network() -> None:
    """All sizes fail with sub-5s latency → hypothesis (d) network/auth."""
    from backend.core.ouroboros.governance.dw_capacity_probe import (
        ProbeResult, classify_probe_results,
    )
    results = [
        ProbeResult(
            model_id="x", target_size=s, trials_run=10,
            successes=0, timeouts=0, other_failures=10,
            p50_ms=2000, p95_ms=4000, p99_ms=4500,
            max_ms=4800, min_ms=1000, avg_response_chars=0,
        )
        for s in (1024, 5120)
    ]
    v = classify_probe_results(results)
    assert v["hypothesis"] == "d"
