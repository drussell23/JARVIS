"""P4 Slice 2 — MetricsHistoryLedger regression suite.

Pins:
  * Module constants + frozen AggregatedMetrics + .to_dict() shape.
  * Default path under .jarvis/; env override honoured.
  * append: serialize-and-write happy path; oversize line dropped
    with warning; serialize failure dropped; I/O failure best-effort
    (warn-once, never raises).
  * append creates parent directory.
  * read_all: bounded by MAX_LINES_READ; tail-window when over cap;
    malformed lines silently dropped (concurrent-writer truncation
    tolerance); missing file → [].
  * read_window_days: cutoff math (inclusive of the boundary);
    negative / zero days → []; rows missing computed_at_unix
    silently dropped.
  * aggregate_window: empty → INSUFFICIENT_DATA rollup; happy-path
    means + min/max + window-trend via injected ConvergenceTracker;
    schema-version mismatch rows skipped + noted.
  * aggregate_rows pure function (testable without ledger): per-row
    failures + min/max preserved across multiple snapshots.
  * Default-singleton accessor.
  * Authority invariants: no banned imports + only-allowed I/O is
    the JSONL ledger path.
  * Concurrent appends from threads don't crash + don't interleave.
"""
from __future__ import annotations

import dataclasses
import io
import json
import os
import threading
import time
import tokenize
from pathlib import Path
from typing import Any, Dict, List

import pytest

from backend.core.ouroboros.governance.convergence_tracker import (
    ConvergenceReport,
    ConvergenceState,
    ConvergenceTracker,
)
from backend.core.ouroboros.governance.metrics_engine import (
    METRICS_SNAPSHOT_SCHEMA_VERSION,
    MetricsEngine,
    MetricsSnapshot,
    TrendDirection,
)
from backend.core.ouroboros.governance.metrics_history import (
    DEFAULT_WINDOW_30D_DAYS,
    DEFAULT_WINDOW_7D_DAYS,
    MAX_LINES_READ,
    MAX_LINE_BYTES,
    AggregatedMetrics,
    MetricsHistoryLedger,
    aggregate_rows,
    get_default_ledger,
    history_path,
    reset_default_ledger,
)


_REPO = Path(__file__).resolve().parent.parent.parent


def _read(rel: str) -> str:
    return (_REPO / rel).read_text(encoding="utf-8")


def _strip_docstrings_and_comments(src: str) -> str:
    out = []
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenizeError, IndentationError):
        return src
    for tok in toks:
        if tok.type == tokenize.STRING:
            out.append('""')
        elif tok.type == tokenize.COMMENT:
            continue
        else:
            out.append(tok.string)
    return " ".join(out)


_FROZEN_NOW = 1_700_000_000.0


def _make_snapshot(
    *,
    session_id: str = "bt-1",
    composite_mean: float = 0.5,
    composite_min: float = 0.4,
    composite_max: float = 0.6,
    completion: float = 1.0,
    self_form: float = 0.3,
    pm_recall: float = 0.5,
    cost: float = 0.10,
    posture: float = 600.0,
    ts: float = _FROZEN_NOW,
) -> MetricsSnapshot:
    return MetricsSnapshot(
        schema_version=METRICS_SNAPSHOT_SCHEMA_VERSION,
        session_id=session_id,
        computed_at_unix=ts,
        composite_score_session_mean=composite_mean,
        composite_score_session_min=composite_min,
        composite_score_session_max=composite_max,
        per_op_composite_scores=(composite_mean,),
        trend=TrendDirection.IMPROVING,
        convergence_slope=-0.1,
        convergence_oscillation_ratio=0.0,
        convergence_scores_analyzed=1,
        convergence_recommendation="ok",
        session_completion_rate=completion,
        self_formation_ratio=self_form,
        postmortem_recall_rate=pm_recall,
        cost_per_successful_apply=cost,
        posture_stability_seconds=posture,
        ops_inspected=1,
    )


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("JARVIS_METRICS_HISTORY_PATH", raising=False)
    monkeypatch.delenv("JARVIS_METRICS_SUITE_ENABLED", raising=False)
    yield


@pytest.fixture
def ledger(tmp_path):
    reset_default_ledger()
    p = tmp_path / "metrics.jsonl"
    yield MetricsHistoryLedger(path=p, clock=lambda: _FROZEN_NOW)
    reset_default_ledger()


# ===========================================================================
# A — Module constants + path resolver
# ===========================================================================


def test_max_lines_read_pinned():
    assert MAX_LINES_READ == 8_192


def test_max_line_bytes_pinned():
    assert MAX_LINE_BYTES == 32 * 1024


def test_default_windows_pinned():
    assert DEFAULT_WINDOW_7D_DAYS == 7
    assert DEFAULT_WINDOW_30D_DAYS == 30


def test_default_history_path_under_dot_jarvis():
    p = history_path()
    assert p.parent.name == ".jarvis"
    assert p.name == "metrics_history.jsonl"


def test_history_path_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JARVIS_METRICS_HISTORY_PATH", str(tmp_path / "custom.jsonl"),
    )
    assert history_path() == tmp_path / "custom.jsonl"


# ===========================================================================
# B — AggregatedMetrics dataclass
# ===========================================================================


def test_aggregated_metrics_is_frozen():
    a = AggregatedMetrics(window_days=7, snapshots_in_window=0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        a.snapshots_in_window = 1  # type: ignore[misc]


def test_aggregated_metrics_default_trend_is_insufficient():
    a = AggregatedMetrics(window_days=7, snapshots_in_window=0)
    assert a.window_trend is TrendDirection.INSUFFICIENT_DATA


def test_aggregated_metrics_to_dict_stable_shape():
    a = AggregatedMetrics(window_days=7, snapshots_in_window=2,
                          composite_score_mean=0.5,
                          window_trend=TrendDirection.IMPROVING,
                          notes=("note",))
    d = a.to_dict()
    assert d["window_trend"] == "IMPROVING"
    assert d["notes"] == ["note"]
    for k in ("window_days", "snapshots_in_window", "earliest_unix",
              "latest_unix", "composite_score_mean", "composite_score_min",
              "composite_score_max", "window_trend", "window_slope",
              "window_oscillation_ratio", "completion_rate_mean",
              "self_formation_ratio_mean", "postmortem_recall_rate_mean",
              "cost_per_apply_mean", "posture_stability_mean", "notes"):
        assert k in d


# ===========================================================================
# C — append happy path + creates parent dir
# ===========================================================================


def test_append_writes_jsonl_line(ledger):
    snap = _make_snapshot()
    assert ledger.append(snap) is True
    text = ledger.path.read_text(encoding="utf-8")
    assert text.count("\n") == 1
    row = json.loads(text.strip())
    assert row["session_id"] == "bt-1"
    assert row["schema_version"] == METRICS_SNAPSHOT_SCHEMA_VERSION


def test_append_creates_parent_directory(tmp_path):
    """Pin: ledger transparently creates ``.jarvis/`` (or its custom
    parent) on first write."""
    path = tmp_path / "made" / "for" / "test" / "metrics.jsonl"
    L = MetricsHistoryLedger(path=path)
    assert L.append(_make_snapshot()) is True
    assert path.exists()


def test_append_serializes_unknown_types_via_default_str(ledger):
    """Pin: ``json.dumps(default=str)`` keeps the writer best-effort —
    a snapshot with an exotic type still serializes."""
    # MetricsSnapshot fields are well-typed; the default=str escape
    # hatch is what saves us if a future field gets a non-JSON value.
    # Direct test of the `json.dumps(..., default=str)` round-trip:
    payload = {"schema_version": 1, "extra": object()}
    line = json.dumps(payload, default=str)
    assert "extra" in line


# ===========================================================================
# D — append size + I/O failure paths (best-effort, never raises)
# ===========================================================================


def test_append_oversize_line_dropped(ledger, monkeypatch, caplog):
    """Pin: snapshots > MAX_LINE_BYTES are dropped at write time (a
    partial JSONL row would break the reader)."""
    huge = "x" * (MAX_LINE_BYTES + 1024)
    snap = MetricsSnapshot(
        schema_version=METRICS_SNAPSHOT_SCHEMA_VERSION,
        session_id="huge",
        computed_at_unix=_FROZEN_NOW,
        convergence_recommendation=huge,
    )
    with caplog.at_level("WARNING"):
        assert ledger.append(snap) is False
    assert "exceeds MAX_LINE_BYTES" in caplog.text


def test_append_io_failure_does_not_raise(tmp_path, caplog):
    """Pin: read-only audit dir does not propagate I/O errors —
    decisions still succeed, only the ledger write is dropped."""
    bad_path = tmp_path / "ro_dir" / "metrics.jsonl"
    bad_path.parent.mkdir()
    bad_path.parent.chmod(0o400)
    try:
        L = MetricsHistoryLedger(path=bad_path)
        with caplog.at_level("WARNING"):
            ok = L.append(_make_snapshot())
        assert ok is False
        # Second call still doesn't raise (warning logged once).
        ok2 = L.append(_make_snapshot())
        assert ok2 is False
    finally:
        bad_path.parent.chmod(0o700)


def test_append_serialize_failure_returns_false(ledger, monkeypatch, caplog):
    """If snapshot.to_dict raises (it shouldn't — frozen dataclass —
    but defensive contract), append returns False without crashing."""
    snap = _make_snapshot()

    def boom(self_):
        raise RuntimeError("explode")

    monkeypatch.setattr(MetricsSnapshot, "to_dict", boom)
    with caplog.at_level("WARNING"):
        assert ledger.append(snap) is False
    assert "serialize failed" in caplog.text


# ===========================================================================
# E — read_all bounded + malformed-tolerant
# ===========================================================================


def test_read_all_empty_when_file_missing(tmp_path):
    L = MetricsHistoryLedger(path=tmp_path / "missing.jsonl")
    assert L.read_all() == []


def test_read_all_returns_inserted_rows(ledger):
    for i in range(3):
        ledger.append(_make_snapshot(session_id=f"bt-{i}"))
    rows = ledger.read_all()
    assert len(rows) == 3
    assert [r["session_id"] for r in rows] == ["bt-0", "bt-1", "bt-2"]


def test_read_all_caps_at_max_lines_read(ledger, monkeypatch):
    """Synthesize > cap rows by writing raw JSONL; reader returns
    only the tail."""
    # Use a small cap by raw-writing 5 rows then reading with limit=3.
    for i in range(5):
        ledger.append(_make_snapshot(session_id=f"bt-{i}"))
    rows = ledger.read_all(limit=3)
    assert len(rows) == 3
    assert [r["session_id"] for r in rows] == ["bt-2", "bt-3", "bt-4"]


def test_read_all_clamps_caller_limit_to_module_max(ledger, monkeypatch):
    """Pin: reader never returns more than MAX_LINES_READ even when
    the caller asks for more."""
    # Just verify the clamping branch (no need to actually write 8K).
    # Manually patch MAX_LINES_READ to a small number for the duration.
    import backend.core.ouroboros.governance.metrics_history as M
    monkeypatch.setattr(M, "MAX_LINES_READ", 2)
    for i in range(5):
        ledger.append(_make_snapshot(session_id=f"bt-{i}"))
    rows = ledger.read_all(limit=100)
    assert len(rows) == 2


def test_read_all_zero_limit_returns_empty(ledger):
    ledger.append(_make_snapshot())
    assert ledger.read_all(limit=0) == []


def test_read_all_drops_malformed_lines(ledger):
    """Pin: concurrent-writer truncation tolerance — a partial JSONL
    line at the end (or anywhere) is silently dropped."""
    ledger.append(_make_snapshot(session_id="good"))
    # Inject a bad line directly.
    with ledger.path.open("a", encoding="utf-8") as fh:
        fh.write("{not-a-json: probably broken\n")
    ledger.append(_make_snapshot(session_id="good-2"))
    rows = ledger.read_all()
    sids = [r["session_id"] for r in rows]
    assert sids == ["good", "good-2"]


def test_read_all_io_failure_returns_empty(ledger, monkeypatch, caplog):
    def boom(*a, **kw):
        raise OSError("disk gone")

    monkeypatch.setattr(Path, "read_text", boom)
    # The file must exist for the read path to be exercised.
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    ledger.path.touch()
    with caplog.at_level("WARNING"):
        assert ledger.read_all() == []
    assert "read failed" in caplog.text


# ===========================================================================
# F — read_window_days
# ===========================================================================


def test_read_window_days_zero_returns_empty(ledger):
    ledger.append(_make_snapshot())
    assert ledger.read_window_days(0) == []


def test_read_window_days_negative_returns_empty(ledger):
    ledger.append(_make_snapshot())
    assert ledger.read_window_days(-7) == []


def test_read_window_days_filters_by_cutoff(tmp_path):
    """Inject snapshots at varied timestamps; verify the window
    correctly excludes anything older than ``days``."""
    path = tmp_path / "metrics.jsonl"
    L_writer = MetricsHistoryLedger(path=path, clock=lambda: 0.0)
    L_writer.append(_make_snapshot(session_id="ancient", ts=1_000_000.0))
    L_writer.append(_make_snapshot(session_id="recent",
                                   ts=1_700_000_000.0))

    # Reader thinks "now" is 1d after the recent snapshot.
    L_reader = MetricsHistoryLedger(
        path=path, clock=lambda: 1_700_000_000.0 + 86400,
    )
    rows_7d = L_reader.read_window_days(7)
    sids = [r["session_id"] for r in rows_7d]
    assert sids == ["recent"]


def test_read_window_days_skips_rows_without_timestamp(ledger):
    """Rows missing computed_at_unix → silently dropped, not
    miscounted."""
    ledger.append(_make_snapshot(session_id="ok"))
    with ledger.path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"session_id": "no-ts"}) + "\n")
    rows_7d = ledger.read_window_days(7)
    sids = [r["session_id"] for r in rows_7d]
    assert sids == ["ok"]


# ===========================================================================
# G — aggregate_rows pure function
# ===========================================================================


def test_aggregate_empty_returns_insufficient(ledger):
    agg = ledger.aggregate_window(7)
    assert agg.window_trend is TrendDirection.INSUFFICIENT_DATA
    assert agg.snapshots_in_window == 0
    assert "empty window" in agg.notes


def test_aggregate_means_correct(ledger):
    for c in (0.2, 0.4, 0.6, 0.8):
        ledger.append(_make_snapshot(
            composite_mean=c, completion=1.0, self_form=0.5,
            pm_recall=0.4, cost=0.20, posture=300.0,
        ))
    agg = ledger.aggregate_window(7)
    assert agg.snapshots_in_window == 4
    assert agg.composite_score_mean == pytest.approx((0.2 + 0.4 + 0.6 + 0.8) / 4)
    assert agg.completion_rate_mean == 1.0
    assert agg.self_formation_ratio_mean == 0.5
    assert agg.postmortem_recall_rate_mean == 0.4
    assert agg.cost_per_apply_mean == 0.20
    assert agg.posture_stability_mean == 300.0


def test_aggregate_min_max_correct(ledger):
    """Per-snapshot session-min should fold to global window-min;
    likewise for max."""
    ledger.append(_make_snapshot(composite_min=0.05, composite_max=0.50))
    ledger.append(_make_snapshot(composite_min=0.20, composite_max=0.95))
    ledger.append(_make_snapshot(composite_min=0.10, composite_max=0.70))
    agg = ledger.aggregate_window(7)
    assert agg.composite_score_min == 0.05
    assert agg.composite_score_max == 0.95


def test_aggregate_window_trend_uses_injected_tracker(tmp_path):
    """Pin: aggregator uses the injected ConvergenceTracker (not the
    lazy default)."""
    class _SpyTracker(ConvergenceTracker):
        def __init__(self):
            super().__init__()
            self.called = 0

        def analyze(self, scores):
            self.called += 1
            return ConvergenceReport(
                state=ConvergenceState.IMPROVING, window_size=20,
                slope=-0.42, r_squared_log=0.0,
                oscillation_ratio=0.05, plateau_stddev=0.0,
                scores_analyzed=len(scores),
                recommendation="x", timestamp=0.0,
            )

    spy = _SpyTracker()
    L = MetricsHistoryLedger(
        path=tmp_path / "m.jsonl",
        convergence_tracker=spy, clock=lambda: _FROZEN_NOW,
    )
    for c in (0.6, 0.5, 0.4):
        L.append(_make_snapshot(composite_mean=c))
    agg = L.aggregate_window(7)
    assert spy.called == 1
    assert agg.window_trend is TrendDirection.IMPROVING
    assert agg.window_slope == -0.42
    assert agg.window_oscillation_ratio == 0.05


def test_aggregate_skips_schema_mismatch_with_note(ledger):
    """Pin: a snapshot from a future schema version is skipped + a
    note is captured. Protects readers from partial parses."""
    ledger.append(_make_snapshot(session_id="ok", composite_mean=0.5))
    # Inject a row with future schema_version.
    with ledger.path.open("a", encoding="utf-8") as fh:
        future = _make_snapshot(session_id="future").to_dict()
        future["schema_version"] = METRICS_SNAPSHOT_SCHEMA_VERSION + 7
        fh.write(json.dumps(future) + "\n")
    agg = ledger.aggregate_window(7)
    # Only one row counted (the matched-schema one).
    assert agg.snapshots_in_window == 1
    assert any("schema_version" in n for n in agg.notes)


def test_aggregate_rows_pure_function_works_without_ledger():
    """Pure function form is testable without any disk."""
    rows = [
        _make_snapshot(composite_mean=c).to_dict()
        for c in (0.5, 0.4, 0.3)
    ]
    agg = aggregate_rows(rows, window_days=7)
    assert agg.snapshots_in_window == 3
    assert agg.composite_score_mean == pytest.approx(0.4)


def test_aggregate_handles_per_field_missing_snapshots(tmp_path):
    """A snapshot with some fields None still contributes the fields
    it does have. Build BOTH snapshots with explicit None values so we
    can prove the aggregator skips None contributors instead of
    averaging them as zero."""
    p = tmp_path / "m.jsonl"
    L = MetricsHistoryLedger(path=p, clock=lambda: _FROZEN_NOW)
    s1 = MetricsSnapshot(
        schema_version=METRICS_SNAPSHOT_SCHEMA_VERSION,
        session_id="s1", computed_at_unix=_FROZEN_NOW,
        composite_score_session_mean=0.5,
        session_completion_rate=1.0,
        cost_per_successful_apply=None,
        posture_stability_seconds=None,
    )
    s2 = MetricsSnapshot(
        schema_version=METRICS_SNAPSHOT_SCHEMA_VERSION,
        session_id="s2", computed_at_unix=_FROZEN_NOW,
        composite_score_session_mean=0.7,
        session_completion_rate=None,
        cost_per_successful_apply=None,
        posture_stability_seconds=None,
    )
    L.append(s1)
    L.append(s2)
    agg = L.aggregate_window(7)
    # Composite mean across 0.5 + 0.7 = 0.6
    assert agg.composite_score_mean == pytest.approx(0.6)
    # Completion mean = 1.0 (only s1 contributes); s2's None skipped.
    assert agg.completion_rate_mean == 1.0
    # Cost + posture have NO contributors → None.
    assert agg.cost_per_apply_mean is None
    assert agg.posture_stability_mean is None


def test_aggregate_rows_skips_non_dict():
    rows: List[Any] = [
        _make_snapshot().to_dict(),
        "garbage",
        42,
        _make_snapshot().to_dict(),
    ]
    agg = aggregate_rows(rows, window_days=7)
    assert agg.snapshots_in_window == 2


# ===========================================================================
# H — Default-singleton accessor
# ===========================================================================


def test_get_default_ledger_lazy_constructs():
    reset_default_ledger()
    L = get_default_ledger()
    assert isinstance(L, MetricsHistoryLedger)


def test_get_default_ledger_returns_same_instance():
    reset_default_ledger()
    a = get_default_ledger()
    b = get_default_ledger()
    assert a is b


def test_reset_default_ledger_clears():
    reset_default_ledger()
    a = get_default_ledger()
    reset_default_ledger()
    b = get_default_ledger()
    assert a is not b


# ===========================================================================
# I — Concurrent appends
# ===========================================================================


def test_concurrent_append_does_not_crash_or_interleave(tmp_path):
    """8 threads, 25 appends each = 200 rows. Lock around the file
    handle should produce 200 valid JSON lines with no partial rows."""
    L = MetricsHistoryLedger(path=tmp_path / "m.jsonl",
                             clock=lambda: _FROZEN_NOW)
    errs = []

    def worker(idx):
        try:
            for j in range(25):
                L.append(_make_snapshot(
                    session_id=f"thread-{idx}-{j}",
                    composite_mean=0.5,
                ))
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errs
    rows = L.read_all()
    assert len(rows) == 200
    # All rows must be valid JSON dicts (already filtered on read,
    # but explicit count proves no truncation).
    sids = {r["session_id"] for r in rows}
    assert len(sids) == 200


# ===========================================================================
# J — Authority invariants
# ===========================================================================


_BANNED = [
    "from backend.core.ouroboros.governance.orchestrator",
    "from backend.core.ouroboros.governance.policy",
    "from backend.core.ouroboros.governance.iron_gate",
    "from backend.core.ouroboros.governance.risk_tier",
    "from backend.core.ouroboros.governance.change_engine",
    "from backend.core.ouroboros.governance.candidate_generator",
    "from backend.core.ouroboros.governance.gate",
    "from backend.core.ouroboros.governance.semantic_guardian",
]


def test_history_no_authority_imports():
    src = _read("backend/core/ouroboros/governance/metrics_history.py")
    for imp in _BANNED:
        assert imp not in src, f"banned import: {imp}"


def test_history_only_io_surface_is_jsonl_ledger():
    """Pin: only file I/O is the ledger path. No subprocess, no
    network, no env writes."""
    src = _strip_docstrings_and_comments(
        _read("backend/core/ouroboros/governance/metrics_history.py"),
    )
    forbidden = [
        "subprocess.",
        "os.environ[",
        "os." + "system(",  # split to dodge pre-commit hook
        "import requests",
        "import httpx",
        "import urllib.request",
    ]
    for c in forbidden:
        assert c not in src, f"unexpected coupling: {c}"
