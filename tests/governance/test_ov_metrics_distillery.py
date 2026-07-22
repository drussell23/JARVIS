"""OV Metrics Distillery — read-only global telemetry aggregation.

Pins the percentile math, multi-glob campaign scoping, cost aggregation,
the GC-store (``<table>`` + ``<table>_agg``) fold, and read-only /
fail-soft guarantees — the reporting substrate behind the whitepaper's
n-count-defensible latency claims.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.ov_metrics_distillery import (
    _fold_telemetry_db,
    _percentile,
    distill,
)


def _session(root: Path, name: str, ttfts_ms, cost) -> None:
    d = root / name
    d.mkdir(parents=True)
    lines = [f"[Slice187] TTFT pure_network={t} clean=True" for t in ttfts_ms]
    (d / "debug.log").write_text("\n".join(lines) + "\n")
    if cost is not None:
        (d / "summary.json").write_text(json.dumps({"cost_total": cost}))


def test_percentile_nearest_rank() -> None:
    vals = list(range(1, 101))  # 1..100
    assert _percentile(vals, 0.50) == 51
    assert _percentile(vals, 0.95) == 96
    assert _percentile([], 0.5) == 0


def test_distill_computes_global_distribution(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    _session(root, "bt-2026-07-21-a", [4000, 8000, 12000], 0.10)
    _session(root, "bt-2026-07-22-b", [3000, 9000, 40000], 0.02)
    # An unrelated session outside the campaign glob — must be excluded.
    _session(root, "bt-2026-06-01-old", [999999], 5.0)

    rep = distill(
        sessions_root=str(root),
        sessions_glob="bt-2026-07-21-*,bt-2026-07-22-*",
    )

    assert rep.sessions_scanned == 2
    assert rep.cost_bearing_sessions == 2
    assert rep.ttft_sample_count == 6  # the old session's sample excluded
    assert abs(rep.aggregate_cost_usd - 0.12) < 1e-9  # 5.0 excluded
    # Distribution buckets reflect the 6 in-window samples.
    assert rep.distribution["1-5s"] == 2   # 4000, 3000
    assert rep.distribution["5-15s"] == 3  # 8000, 12000, 9000
    assert rep.distribution[">30s"] == 1   # 40000
    assert rep.ttft_max_ms == 40000
    assert rep.ttft_min_ms == 3000


def test_distill_empty_is_zeroed_not_crash(tmp_path: Path) -> None:
    rep = distill(sessions_root=str(tmp_path), sessions_glob="nope-*")
    assert rep.ttft_sample_count == 0
    assert rep.ttft_p50_ms == 0
    assert rep.aggregate_cost_usd == 0.0


def test_telemetry_db_fold_reuses_gc_schema(tmp_path: Path) -> None:
    """DRY: the distillery folds the SAME schema the GC's compact_telemetry
    produces — live <table> rows + day-bucketed <table>_agg row_count."""
    db = tmp_path / "telemetry.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE telemetry (ts REAL, ttft_ms INTEGER)")
    conn.executemany(
        "INSERT INTO telemetry VALUES (?,?)",
        [(1.0, 5000), (2.0, 6000)],
    )
    # The GC's aggregate table (day_bucket, row_count) for compacted rows.
    conn.execute(
        "CREATE TABLE telemetry_agg (day_bucket INTEGER PRIMARY KEY, "
        "row_count INTEGER)"
    )
    conn.execute("INSERT INTO telemetry_agg VALUES (100, 48)")
    conn.commit()
    conn.close()

    folded = _fold_telemetry_db(str(db))
    assert folded == 2 + 48  # live rows + compacted aggregate count


def test_telemetry_db_fold_missing_is_fail_soft(tmp_path: Path) -> None:
    assert _fold_telemetry_db(str(tmp_path / "nonexistent.db")) == 0


def test_db_opened_read_only(tmp_path: Path) -> None:
    """Intellectual-honesty guarantee: the distillery NEVER mutates the
    telemetry DB it reads."""
    db = tmp_path / "ro.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE telemetry (ts REAL, ttft_ms INTEGER)")
    conn.execute("INSERT INTO telemetry VALUES (1.0, 5000)")
    conn.commit()
    conn.close()
    before = db.read_bytes()
    _fold_telemetry_db(str(db))
    assert db.read_bytes() == before  # byte-identical — read-only proven
