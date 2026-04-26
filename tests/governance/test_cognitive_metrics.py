"""Phase 4 P3 Slice 1 — CognitiveMetrics wrapper + REPL regression suite.

Pins the un-stranding of OraclePreScorer + VindicationReflector under
the new CognitiveMetricsService wrapper. Slice 2 wires the orchestrator;
this slice ships the wrapper + REPL + ledger + tests.

Sections:
    (A) Master flag — env reader, default false (graduates in Slice 2)
    (B) CognitiveMetricRecord dataclass — frozen + ledger_dict shape
    (C) score_pre_apply — happy path, ledger persistence, neutral fallback,
        flag-off no-write
    (D) reflect_post_apply — same coverage shape
    (E) load_records — empty / populated / malformed lines tolerated
    (F) stats() — counts, means, gate/advisory aggregations
    (G) Default-singleton accessor — None when off, lazy construct,
        set_default_service injection, reset
    (H) REPL — routing / help / stats / list / show / pre-scores /
        vindications / unknown / parse error / no-service
    (I) Authority invariants — no banned imports + side-effect surface
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

import pytest

from backend.core.ouroboros.governance.cognitive_metrics import (
    COGNITIVE_METRICS_SCHEMA_VERSION,
    DEFAULT_LEDGER_FILENAME,
    CognitiveMetricRecord,
    CognitiveMetricsService,
    get_default_service,
    is_enabled,
    reset_default_service,
    set_default_service,
)
from backend.core.ouroboros.governance.cognitive_metrics_repl import (
    CognitiveDispatchResult,
    dispatch_cognitive_command as REPL,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("JARVIS_COGNITIVE_METRICS_ENABLED", raising=False)
    reset_default_service()
    yield
    reset_default_service()


@pytest.fixture
def stub_oracle():
    o = MagicMock()
    o.compute_blast_radius.return_value = MagicMock(
        risk_level="LOW", total_affected=5,
    )
    o.get_dependencies.return_value = []
    o.get_dependents.return_value = []
    return o


@pytest.fixture
def service(stub_oracle, tmp_path):
    return CognitiveMetricsService(
        oracle=stub_oracle, project_root=tmp_path,
    )


def _enable(monkeypatch):
    monkeypatch.setenv("JARVIS_COGNITIVE_METRICS_ENABLED", "true")


# ---------------------------------------------------------------------------
# (A) Master flag
# ---------------------------------------------------------------------------


def test_master_flag_default_true_post_graduation(monkeypatch):
    """JARVIS_COGNITIVE_METRICS_ENABLED defaults True post-graduation
    (P3 Slice 2, 2026-04-26). Hot-revert: set env to "false".

    If this test fails AND P3 has been intentionally rolled back: rename
    to test_master_flag_default_false (and flip the assertion + the
    source-grep pin) per the same discipline P0/P0.5/P1/P1.5 used."""
    monkeypatch.delenv("JARVIS_COGNITIVE_METRICS_ENABLED", raising=False)
    assert is_enabled() is True


def test_master_flag_explicit_false_hot_revert(monkeypatch):
    """Hot-revert path post-graduation."""
    monkeypatch.setenv("JARVIS_COGNITIVE_METRICS_ENABLED", "false")
    assert is_enabled() is False


def test_master_flag_explicit_true(monkeypatch):
    _enable(monkeypatch)
    assert is_enabled() is True


# ---------------------------------------------------------------------------
# (B) CognitiveMetricRecord
# ---------------------------------------------------------------------------


def test_record_is_frozen():
    r = CognitiveMetricRecord(
        schema_version=COGNITIVE_METRICS_SCHEMA_VERSION,
        op_id="op-1", kind="pre_score", target_files=("a.py",),
        pre_score=0.5, pre_score_gate="NORMAL",
    )
    with pytest.raises(Exception):
        r.pre_score = 0.9  # type: ignore[misc]


def test_record_to_ledger_dict_jsonable():
    r = CognitiveMetricRecord(
        schema_version=COGNITIVE_METRICS_SCHEMA_VERSION,
        op_id="op-1", kind="pre_score", target_files=("a.py", "b.py"),
        pre_score=0.5, pre_score_gate="NORMAL",
        subsignals={"blast_radius": 0.5, "coupling": 0.2},
    )
    d = r.to_ledger_dict()
    json.dumps(d)  # must serialize cleanly
    assert d["target_files"] == ["a.py", "b.py"]


def test_schema_version_pinned():
    assert COGNITIVE_METRICS_SCHEMA_VERSION == "cognitive_metrics.1"


def test_default_ledger_filename_pinned():
    assert DEFAULT_LEDGER_FILENAME == "cognitive_metrics.jsonl"


# ---------------------------------------------------------------------------
# (C) score_pre_apply
# ---------------------------------------------------------------------------


def test_score_pre_apply_returns_real_result(monkeypatch, service):
    _enable(monkeypatch)
    result = service.score_pre_apply(
        op_id="op-1", target_files=["a.py"],
        max_complexity=10, has_tests=True,
    )
    assert 0.0 <= result.pre_score <= 1.0
    assert result.gate in ("FAST_TRACK", "NORMAL", "WARN")


def test_score_pre_apply_persists_when_flag_on(monkeypatch, service):
    _enable(monkeypatch)
    service.score_pre_apply(
        op_id="op-1", target_files=["a.py"],
        max_complexity=10, has_tests=True,
    )
    assert service.ledger_path.exists()
    rows = service.load_records()
    assert len(rows) == 1
    assert rows[0].kind == "pre_score"
    assert rows[0].op_id == "op-1"


def test_score_pre_apply_no_persist_when_flag_off(monkeypatch, service):
    """Hot-revert: flag off → no ledger touch (byte-for-byte pre-Slice-1)."""
    monkeypatch.setenv("JARVIS_COGNITIVE_METRICS_ENABLED", "false")
    service.score_pre_apply(
        op_id="op-1", target_files=["a.py"], max_complexity=5,
    )
    assert not service.ledger_path.exists()


def test_score_pre_apply_oracle_failure_returns_neutral(
    monkeypatch, tmp_path,
):
    """Defensive: oracle that raises → underlying scorer returns neutral
    PreScoreResult, wrapper still returns + persists it."""
    _enable(monkeypatch)
    bad_oracle = MagicMock()
    bad_oracle.compute_blast_radius.side_effect = RuntimeError("oracle down")
    svc = CognitiveMetricsService(oracle=bad_oracle, project_root=tmp_path)
    result = svc.score_pre_apply(
        op_id="op-1", target_files=["a.py"],
    )
    assert result.gate == "NORMAL"
    assert result.pre_score == 0.5  # neutral fallback


# ---------------------------------------------------------------------------
# (D) reflect_post_apply
# ---------------------------------------------------------------------------


def test_reflect_returns_real_result(monkeypatch, service):
    _enable(monkeypatch)
    result = service.reflect_post_apply(
        op_id="op-1", target_files=["a.py"],
        coupling_after=0, blast_radius_after=0,
        complexity_after=10, complexity_before=15,
    )
    assert -1.0 <= result.vindication_score <= 1.0
    assert result.advisory in (
        "vindicating", "neutral", "concerning", "warning",
    )


def test_reflect_persists_when_flag_on(monkeypatch, service):
    _enable(monkeypatch)
    service.reflect_post_apply(
        op_id="op-1", target_files=["a.py"],
        coupling_after=0, blast_radius_after=0,
        complexity_after=10, complexity_before=15,
    )
    rows = service.load_records()
    assert len(rows) == 1
    assert rows[0].kind == "vindication"


def test_reflect_no_persist_when_flag_off(monkeypatch, service):
    monkeypatch.setenv("JARVIS_COGNITIVE_METRICS_ENABLED", "false")
    service.reflect_post_apply(
        op_id="op-1", target_files=["a.py"],
        coupling_after=0, blast_radius_after=0,
        complexity_after=10, complexity_before=15,
    )
    assert not service.ledger_path.exists()


def test_reflect_oracle_failure_returns_neutral(
    monkeypatch, tmp_path,
):
    _enable(monkeypatch)
    bad_oracle = MagicMock()
    bad_oracle.get_dependencies.side_effect = RuntimeError("oracle down")
    svc = CognitiveMetricsService(oracle=bad_oracle, project_root=tmp_path)
    result = svc.reflect_post_apply(
        op_id="op-1", target_files=["a.py"],
        coupling_after=0, blast_radius_after=0,
        complexity_after=0, complexity_before=0,
    )
    assert result.advisory == "neutral"
    assert result.vindication_score == 0.0


# ---------------------------------------------------------------------------
# (E) load_records
# ---------------------------------------------------------------------------


def test_load_records_empty(service):
    assert service.load_records() == []


def test_load_records_tolerates_malformed_line(service):
    service.ledger_path.parent.mkdir(parents=True, exist_ok=True)
    valid = json.dumps({
        "schema_version": COGNITIVE_METRICS_SCHEMA_VERSION,
        "op_id": "op-good", "kind": "pre_score",
        "target_files": ["a.py"], "pre_score": 0.4,
        "pre_score_gate": "NORMAL",
    })
    service.ledger_path.write_text(
        "this is not json\n" + valid + "\n{still bad}\n"
    )
    rows = service.load_records()
    assert len(rows) == 1
    assert rows[0].op_id == "op-good"


def test_load_records_skips_unknown_kind(service):
    service.ledger_path.parent.mkdir(parents=True, exist_ok=True)
    bad = json.dumps({
        "schema_version": COGNITIVE_METRICS_SCHEMA_VERSION,
        "op_id": "op-1", "kind": "alien", "target_files": [],
    })
    good = json.dumps({
        "schema_version": COGNITIVE_METRICS_SCHEMA_VERSION,
        "op_id": "op-2", "kind": "pre_score", "target_files": ["a.py"],
        "pre_score": 0.3, "pre_score_gate": "FAST_TRACK",
    })
    service.ledger_path.write_text(bad + "\n" + good + "\n")
    rows = service.load_records()
    assert len(rows) == 1
    assert rows[0].op_id == "op-2"


# ---------------------------------------------------------------------------
# (F) stats
# ---------------------------------------------------------------------------


def test_stats_empty_ledger(service):
    s = service.stats()
    assert s["total"] == 0
    assert s["pre_score_count"] == 0
    assert s["vindication_count"] == 0
    assert s["mean_pre_score"] is None
    assert s["mean_vindication_score"] is None
    assert s["gate_counts"] == {}
    assert s["advisory_counts"] == {}


def test_stats_populated(monkeypatch, service):
    _enable(monkeypatch)
    service.score_pre_apply("op-1", ["a.py"], max_complexity=5)
    service.score_pre_apply("op-2", ["b.py"], max_complexity=20)
    service.reflect_post_apply(
        "op-1", ["a.py"], coupling_after=0, blast_radius_after=0,
        complexity_after=5, complexity_before=10,
    )
    s = service.stats()
    assert s["total"] == 3
    assert s["pre_score_count"] == 2
    assert s["vindication_count"] == 1
    assert s["mean_pre_score"] is not None
    assert s["mean_vindication_score"] is not None
    assert sum(s["gate_counts"].values()) == 2


# ---------------------------------------------------------------------------
# (G) Default-singleton accessor
# ---------------------------------------------------------------------------


def test_get_default_service_none_when_master_off(
    monkeypatch, stub_oracle, tmp_path,
):
    """Hot-revert path post-graduation: explicit false → accessor None."""
    monkeypatch.setenv("JARVIS_COGNITIVE_METRICS_ENABLED", "false")
    assert get_default_service(oracle=stub_oracle, project_root=tmp_path) is None


def test_get_default_service_lazy_construct(monkeypatch, stub_oracle, tmp_path):
    _enable(monkeypatch)
    a = get_default_service(oracle=stub_oracle, project_root=tmp_path)
    b = get_default_service(oracle=stub_oracle, project_root=tmp_path)
    assert a is not None and a is b


def test_get_default_service_returns_none_without_oracle(monkeypatch):
    """When uninitialised + no oracle supplied, returns None (orchestrator
    boot in Slice 2 will inject via set_default_service)."""
    _enable(monkeypatch)
    reset_default_service()
    assert get_default_service() is None


def test_set_default_service_injection(monkeypatch, service):
    _enable(monkeypatch)
    set_default_service(service)
    got = get_default_service()
    assert got is service


def test_reset_default_service_drops_singleton(
    monkeypatch, stub_oracle, tmp_path,
):
    _enable(monkeypatch)
    a = get_default_service(oracle=stub_oracle, project_root=tmp_path)
    reset_default_service()
    b = get_default_service(oracle=stub_oracle, project_root=tmp_path)
    assert a is not b


# ---------------------------------------------------------------------------
# (H) REPL
# ---------------------------------------------------------------------------


def test_repl_unrelated_line_unmatched(tmp_path):
    r = REPL("/posture explain", project_root=tmp_path)
    assert r.matched is False


def test_repl_help(tmp_path):
    r = REPL("/cognitive help", project_root=tmp_path)
    assert r.ok is True
    assert "stats" in r.text and "show" in r.text


def test_repl_stats_no_service_when_master_off(monkeypatch, tmp_path):
    """Hot-revert path post-graduation: explicit false → REPL says
    not initialised."""
    monkeypatch.setenv("JARVIS_COGNITIVE_METRICS_ENABLED", "false")
    r = REPL("/cognitive stats", project_root=tmp_path)
    assert r.ok is False
    assert "not initialised" in r.text


def test_repl_stats_with_injected_service(monkeypatch, service):
    _enable(monkeypatch)
    service.score_pre_apply("op-1", ["a.py"], max_complexity=5)
    r = REPL("/cognitive stats", service=service)
    assert r.ok is True
    assert "total rows" in r.text
    assert "pre_score count:      1" in r.text


def test_repl_no_args_alias_for_stats(monkeypatch, service):
    _enable(monkeypatch)
    r = REPL("/cognitive", service=service)
    assert r.ok is True
    assert "Cognitive metrics ledger stats" in r.text


def test_repl_list(monkeypatch, service):
    _enable(monkeypatch)
    service.score_pre_apply("op-aaa", ["a.py"], max_complexity=5)
    service.score_pre_apply("op-bbb", ["b.py"], max_complexity=10)
    r = REPL("/cognitive list", service=service)
    assert r.ok is True
    assert "op-aaa" in r.text and "op-bbb" in r.text


def test_repl_list_limit(monkeypatch, service):
    _enable(monkeypatch)
    for i in range(5):
        service.score_pre_apply(f"op-{i}", ["a.py"], max_complexity=5)
    r = REPL("/cognitive list --limit 2", service=service)
    # Only 2 op rows shown.
    assert sum(1 for line in r.text.splitlines() if "pre_score" in line) == 2


def test_repl_show_unknown_op(monkeypatch, service):
    _enable(monkeypatch)
    r = REPL("/cognitive show nonexistent", service=service)
    assert "no rows" in r.text


def test_repl_show_op_with_records(monkeypatch, service):
    _enable(monkeypatch)
    service.score_pre_apply("op-target", ["a.py"], max_complexity=5)
    service.reflect_post_apply(
        "op-target", ["a.py"],
        coupling_after=0, blast_radius_after=0,
        complexity_after=5, complexity_before=10,
    )
    r = REPL("/cognitive show op-target", service=service)
    assert "op-target" in r.text
    assert "pre_score" in r.text
    assert "vindication" in r.text


def test_repl_show_missing_op_arg(monkeypatch, service):
    _enable(monkeypatch)
    r = REPL("/cognitive show", service=service)
    assert r.ok is False
    assert "missing" in r.text.lower()


def test_repl_pre_scores_filter(monkeypatch, service):
    _enable(monkeypatch)
    service.score_pre_apply("op-pre", ["a.py"], max_complexity=5)
    service.reflect_post_apply(
        "op-vind", ["a.py"],
        coupling_after=0, blast_radius_after=0,
        complexity_after=0, complexity_before=0,
    )
    r = REPL("/cognitive pre-scores", service=service)
    assert "op-pre" in r.text
    assert "op-vind" not in r.text


def test_repl_vindications_filter(monkeypatch, service):
    _enable(monkeypatch)
    service.score_pre_apply("op-pre", ["a.py"], max_complexity=5)
    service.reflect_post_apply(
        "op-vind", ["a.py"],
        coupling_after=0, blast_radius_after=0,
        complexity_after=0, complexity_before=0,
    )
    r = REPL("/cognitive vindications", service=service)
    assert "op-vind" in r.text
    assert "op-pre" not in r.text


def test_repl_unknown_subcommand(monkeypatch, service):
    _enable(monkeypatch)
    r = REPL("/cognitive floof", service=service)
    assert r.ok is False
    assert "unknown" in r.text.lower()


def test_repl_parse_error(tmp_path):
    r = REPL('/cognitive show "unclosed', project_root=tmp_path)
    assert r.matched is True
    assert "parse error" in r.text


def test_repl_dispatch_result_contract(tmp_path):
    """Mirror of HypothesisDispatchResult shape — SerpentREPL uniform."""
    r = REPL("/posture explain", project_root=tmp_path)
    assert isinstance(r, CognitiveDispatchResult)
    assert hasattr(r, "ok") and hasattr(r, "text") and hasattr(r, "matched")


# ---------------------------------------------------------------------------
# (I) Authority invariants
# ---------------------------------------------------------------------------


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


def test_cognitive_metrics_no_authority_imports():
    src = (
        Path(__file__).resolve().parent.parent.parent
        / "backend/core/ouroboros/governance/cognitive_metrics.py"
    ).read_text(encoding="utf-8")
    for imp in _BANNED:
        assert imp not in src, f"banned import: {imp}"


def test_cognitive_metrics_repl_no_authority_imports():
    src = (
        Path(__file__).resolve().parent.parent.parent
        / "backend/core/ouroboros/governance/cognitive_metrics_repl.py"
    ).read_text(encoding="utf-8")
    for imp in _BANNED:
        assert imp not in src, f"banned import: {imp}"


def test_cognitive_metrics_only_writes_jsonl():
    """Pin: wrapper does only file I/O via JSONL append. No subprocess /
    env mutation / system calls."""
    src = (
        Path(__file__).resolve().parent.parent.parent
        / "backend/core/ouroboros/governance/cognitive_metrics.py"
    ).read_text(encoding="utf-8")
    forbidden = [
        "subprocess.",
        "os.environ[",
        "os." + "system(",
    ]
    for c in forbidden:
        assert c not in src, f"unexpected coupling: {c}"


def test_cognitive_metrics_repl_is_read_only_on_service():
    """REPL never calls score_pre_apply / reflect_post_apply / set_default_service.
    Only the orchestrator (Slice 2) writes."""
    src = (
        Path(__file__).resolve().parent.parent.parent
        / "backend/core/ouroboros/governance/cognitive_metrics_repl.py"
    ).read_text(encoding="utf-8")
    forbidden_calls = [
        "service.score_pre_apply(",
        "service.reflect_post_apply(",
        "resolved.score_pre_apply(",
        "resolved.reflect_post_apply(",
        "set_default_service(",
    ]
    for c in forbidden_calls:
        assert c not in src, f"REPL must not call {c}"
