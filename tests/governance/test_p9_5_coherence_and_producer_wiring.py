"""Phase 9.5 — Cross-session coherence + Phase 8 producer-wiring spine.

Pins (Part A — coherence harness):
  * Two-session arc returns CoherenceReport with carry-over verdicts
    for each primitive (LSS load / LSS prompt render / UPM survival).
  * Master-flag matrix for LSS enable + prompt-injection enable.
  * NEVER-raises smoke (bad inputs / missing dirs / disabled prims).
  * Authority/cage invariants.

Pins (Part B — producer-wiring hooks):
  * 5 producer entry points (record_decision / record_confidence /
    record_phase_latency / check_breach_and_publish /
    check_flag_changes_and_publish + 1 placeholder
    append_timeline_event + substrate_flag_snapshot).
  * Master-off → no-op behavior (substrate's master flags govern).
  * Master-on → substrate.record() invoked + SSE-bridge publish.
  * NEVER-raises on broken substrate / broken SSE bridge.
  * Authority/cage invariants.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List

import pytest

from backend.core.ouroboros.governance.graduation import (
    cross_session_coherence as _coh,
)
from backend.core.ouroboros.governance.graduation.cross_session_coherence import (  # noqa: E501
    COHERENCE_HARNESS_SCHEMA_VERSION,
    CoherenceReport,
    PrimitiveResult,
    PrimitiveStatus,
    render_results_markdown,
    run_two_session_arc,
    write_results_markdown,
)
from backend.core.ouroboros.governance.observability import (
    decision_trace_ledger as _ledger_mod,
    flag_change_emitter as _flag_mod,
    latency_slo_detector as _slo_mod,
    latent_confidence_ring as _ring_mod,
    phase8_producers as _producers,
)
from backend.core.ouroboros.governance.observability.phase8_producers import (
    append_timeline_event,
    check_breach_and_publish,
    check_flag_changes_and_publish,
    record_confidence,
    record_decision,
    record_phase_latency,
    substrate_flag_snapshot,
)


# ---------------------------------------------------------------------------
# Coherence — fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch: pytest.MonkeyPatch):
    keys = [
        k for k in os.environ.keys()
        if (
            k.startswith("JARVIS_LAST_SESSION_SUMMARY_")
            or k.startswith("JARVIS_DECISION_TRACE_LEDGER_")
            or k.startswith("JARVIS_LATENT_CONFIDENCE_RING_")
            or k.startswith("JARVIS_FLAG_CHANGE_EMITTER_")
            or k.startswith("JARVIS_LATENCY_SLO_DETECTOR_")
            or k.startswith("JARVIS_PHASE8_SSE_BRIDGE_")
        )
    ]
    for k in keys:
        monkeypatch.delenv(k, raising=False)
    _ledger_mod.reset_default_ledger()
    _ring_mod.reset_default_ring()
    _flag_mod.reset_default_monitor()
    _slo_mod.reset_default_detector()
    yield
    _ledger_mod.reset_default_ledger()
    _ring_mod.reset_default_ring()
    _flag_mod.reset_default_monitor()
    _slo_mod.reset_default_detector()


@pytest.fixture
def lss_on(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JARVIS_LAST_SESSION_SUMMARY_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_LAST_SESSION_SUMMARY_PROMPT_INJECTION_ENABLED",
        "true",
    )


# ---------------------------------------------------------------------------
# Coherence — module constants
# ---------------------------------------------------------------------------


def test_coh_schema_version():
    assert COHERENCE_HARNESS_SCHEMA_VERSION == "1.0"


def test_coh_primitive_status_five_values():
    assert {s.value for s in PrimitiveStatus} == {
        "carried_over", "no_carryover",
        "primitive_disabled", "primitive_unavailable",
        "harness_error",
    }


# ---------------------------------------------------------------------------
# Coherence — happy path
# ---------------------------------------------------------------------------


def test_two_session_arc_all_primitives_carry_over(
    lss_on, tmp_path: Path,
):
    user_root = tmp_path / "user_prefs"
    user_root.mkdir()
    report = run_two_session_arc(
        project_root=tmp_path,
        user_preference_root=user_root,
    )
    assert isinstance(report, CoherenceReport)
    assert report.all_applicable_carried_over is True
    assert report.carryover_rate_pct == pytest.approx(100.0)


def test_two_session_arc_lss_load_carries_session_n_id(
    lss_on, tmp_path: Path,
):
    user_root = tmp_path / "user_prefs"
    user_root.mkdir()
    custom_n_id = "bt-coherence-test-N-deadbeef"
    report = run_two_session_arc(
        project_root=tmp_path,
        user_preference_root=user_root,
        session_n_id=custom_n_id,
    )
    by_name = {p.primitive_name: p for p in report.primitives}
    lss = by_name["last_session_summary"]
    assert lss.status == PrimitiveStatus.CARRIED_OVER
    assert lss.marker_signal == custom_n_id


def test_two_session_arc_lss_prompt_renders_session_n_id(
    lss_on, tmp_path: Path,
):
    user_root = tmp_path / "user_prefs"
    user_root.mkdir()
    custom_n_id = "bt-coherence-test-N-renderpin"
    report = run_two_session_arc(
        project_root=tmp_path,
        user_preference_root=user_root,
        session_n_id=custom_n_id,
    )
    by_name = {p.primitive_name: p for p in report.primitives}
    render = by_name["lss_prompt_render"]
    assert render.status == PrimitiveStatus.CARRIED_OVER


def test_two_session_arc_user_pref_marker_survives(
    lss_on, tmp_path: Path,
):
    user_root = tmp_path / "user_prefs"
    user_root.mkdir()
    report = run_two_session_arc(
        project_root=tmp_path,
        user_preference_root=user_root,
        marker_name="custom_carryover_marker_xyz",
    )
    by_name = {p.primitive_name: p for p in report.primitives}
    upm = by_name["user_preference_memory"]
    assert upm.status == PrimitiveStatus.CARRIED_OVER
    assert upm.marker_signal == "custom_carryover_marker_xyz"


# ---------------------------------------------------------------------------
# Coherence — disabled-primitive fallthrough
# ---------------------------------------------------------------------------


def test_two_session_arc_lss_disabled_marks_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """When LSS master flag is OFF, the LSS load primitive is
    classified PRIMITIVE_DISABLED — not NO_CARRYOVER (which would
    falsely accuse the substrate of failing)."""
    monkeypatch.delenv(
        "JARVIS_LAST_SESSION_SUMMARY_ENABLED", raising=False,
    )
    user_root = tmp_path / "user_prefs"
    user_root.mkdir()
    report = run_two_session_arc(
        project_root=tmp_path,
        user_preference_root=user_root,
    )
    by_name = {p.primitive_name: p for p in report.primitives}
    lss = by_name["last_session_summary"]
    assert lss.status == PrimitiveStatus.PRIMITIVE_DISABLED


def test_two_session_arc_prompt_injection_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    monkeypatch.setenv("JARVIS_LAST_SESSION_SUMMARY_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_LAST_SESSION_SUMMARY_PROMPT_INJECTION_ENABLED",
        "false",
    )
    user_root = tmp_path / "user_prefs"
    user_root.mkdir()
    report = run_two_session_arc(
        project_root=tmp_path,
        user_preference_root=user_root,
    )
    by_name = {p.primitive_name: p for p in report.primitives}
    render = by_name["lss_prompt_render"]
    assert render.status == PrimitiveStatus.PRIMITIVE_DISABLED


# ---------------------------------------------------------------------------
# Coherence — markdown writer
# ---------------------------------------------------------------------------


def test_render_results_markdown_includes_header(
    lss_on, tmp_path: Path,
):
    user_root = tmp_path / "user_prefs"
    user_root.mkdir()
    report = run_two_session_arc(
        project_root=tmp_path, user_preference_root=user_root,
    )
    md = render_results_markdown(report)
    assert "Cross-Session Coherence Harness" in md
    assert "Carried-over rate" in md


def test_write_results_markdown_creates_file(
    lss_on, tmp_path: Path,
):
    user_root = tmp_path / "user_prefs"
    user_root.mkdir()
    report = run_two_session_arc(
        project_root=tmp_path, user_preference_root=user_root,
    )
    target = tmp_path / "coherence_RESULTS.md"
    ok = write_results_markdown(report, target)
    assert ok is True
    assert target.exists()


def test_write_results_markdown_unwritable_returns_false(
    lss_on, tmp_path: Path,
):
    user_root = tmp_path / "user_prefs"
    user_root.mkdir()
    report = run_two_session_arc(
        project_root=tmp_path, user_preference_root=user_root,
    )
    bad = Path("/nonexistent_root_xyz_zzz/RESULTS.md")
    ok = write_results_markdown(report, bad)
    assert ok is False


# ---------------------------------------------------------------------------
# Coherence — NEVER-raises
# ---------------------------------------------------------------------------


def test_two_session_arc_with_nonexistent_user_pref_root(
    lss_on, tmp_path: Path,
):
    """User-pref store handles a non-existent root gracefully (it
    creates the directory via .add())."""
    user_root = tmp_path / "does_not_exist_yet"
    # NOT mkdir-ing
    report = run_two_session_arc(
        project_root=tmp_path,
        user_preference_root=user_root,
    )
    # Either CARRIED_OVER (if store auto-creates) or HARNESS_ERROR
    # (if the platform's mkdir failed) — both are acceptable. Not
    # NO_CARRYOVER (that would mean the marker disappeared after
    # being persisted).
    by_name = {p.primitive_name: p for p in report.primitives}
    upm = by_name["user_preference_memory"]
    assert upm.status in {
        PrimitiveStatus.CARRIED_OVER,
        PrimitiveStatus.HARNESS_ERROR,
    }


def test_two_session_arc_returns_report_even_on_setup_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    """Make the sessions root unwritable — _write_summary_json
    returns False — harness returns a report with session_n_setup
    error."""
    # Point project_root at a path that's readable but unwritable.
    user_root = tmp_path / "user_prefs"
    user_root.mkdir()
    # Place project root inside a path that exists but where we
    # can't write — use /dev/null/x which always fails.
    project_root = Path("/dev/null/no-write")
    report = run_two_session_arc(
        project_root=project_root,
        user_preference_root=user_root,
    )
    assert isinstance(report, CoherenceReport)
    # Should have a single session_n_setup error primitive.
    statuses = {p.status for p in report.primitives}
    assert PrimitiveStatus.HARNESS_ERROR in statuses


# ---------------------------------------------------------------------------
# Coherence — authority/cage invariants
# ---------------------------------------------------------------------------


def test_coherence_does_not_import_gate_modules():
    import ast
    import inspect
    src = inspect.getsource(_coh)
    tree = ast.parse(src)
    banned = [
        "orchestrator", "iron_gate", "risk_tier_floor",
        "policy_engine", "candidate_generator",
        "tool_executor", "change_engine",
    ]
    for node in ast.walk(tree):
        names: List[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names = [node.module]
        for mod in names:
            for token in banned:
                assert token not in mod, (
                    f"cross_session_coherence imports {mod!r} "
                    f"(banned token {token!r})"
                )


def test_coherence_top_level_imports_stdlib_only():
    import ast
    import inspect
    src = inspect.getsource(_coh)
    tree = ast.parse(src)
    top_level: List[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            top_level.append(node.module)
    forbidden = {
        "backend.core.ouroboros.governance.last_session_summary",
        "backend.core.ouroboros.governance.user_preference_memory",
    }
    leaked = forbidden & set(top_level)
    assert not leaked, f"hoisted memory primitives: {leaked}"


def test_coherence_public_api_pinned():
    public = sorted(
        n for n in dir(_coh)
        if not n.startswith("_") and (
            callable(getattr(_coh, n)) or n.isupper()
        )
    )
    required = {
        "COHERENCE_HARNESS_SCHEMA_VERSION",
        "CoherenceReport", "PrimitiveResult", "PrimitiveStatus",
        "render_results_markdown",
        "run_two_session_arc",
        "write_results_markdown",
    }
    missing = required - set(public)
    assert not missing, f"public API regression: {missing}"


# ---------------------------------------------------------------------------
# Producer-wiring — fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_substrate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Point substrate paths at tmp_path + reset singletons."""
    monkeypatch.setenv(
        "JARVIS_DECISION_TRACE_LEDGER_PATH",
        str(tmp_path / "decision_trace.jsonl"),
    )
    _ledger_mod.reset_default_ledger()
    _ring_mod.reset_default_ring()
    _flag_mod.reset_default_monitor()
    _slo_mod.reset_default_detector()
    return tmp_path


@pytest.fixture
def all_substrates_on(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JARVIS_DECISION_TRACE_LEDGER_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_LATENT_CONFIDENCE_RING_ENABLED", "true",
    )
    monkeypatch.setenv("JARVIS_FLAG_CHANGE_EMITTER_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_LATENCY_SLO_DETECTOR_ENABLED", "true",
    )
    monkeypatch.setenv("JARVIS_MULTI_OP_TIMELINE_ENABLED", "true")


# ---------------------------------------------------------------------------
# Producer-wiring — substrate_flag_snapshot
# ---------------------------------------------------------------------------


def test_substrate_flag_snapshot_default_all_false():
    snap = substrate_flag_snapshot()
    assert snap == {
        "decision_trace_ledger": False,
        "latent_confidence_ring": False,
        "flag_change_emitter": False,
        "latency_slo_detector": False,
        "multi_op_timeline": False,
    }


def test_substrate_flag_snapshot_all_on(all_substrates_on):
    snap = substrate_flag_snapshot()
    assert all(snap.values())


def test_substrate_flag_snapshot_partial(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("JARVIS_DECISION_TRACE_LEDGER_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_LATENT_CONFIDENCE_RING_ENABLED", "false",
    )
    snap = substrate_flag_snapshot()
    assert snap["decision_trace_ledger"] is True
    assert snap["latent_confidence_ring"] is False


# ---------------------------------------------------------------------------
# Producer-wiring — record_decision
# ---------------------------------------------------------------------------


def test_record_decision_master_off_returns_false(
    isolated_substrate,
):
    ok = record_decision(
        op_id="op-1", phase="ROUTE", decision="STANDARD",
    )
    assert ok is False


def test_record_decision_master_on_records(
    isolated_substrate, all_substrates_on,
):
    ok = record_decision(
        op_id="op-1", phase="ROUTE", decision="STANDARD",
        rationale="default cascade",
    )
    assert ok is True
    # Verify the row landed.
    ledger = _ledger_mod.get_default_ledger()
    rows = ledger.reconstruct_op("op-1")
    assert len(rows) == 1
    assert rows[0].decision == "STANDARD"


def test_record_decision_with_factors_and_weights(
    isolated_substrate, all_substrates_on,
):
    ok = record_decision(
        op_id="op-2", phase="GATE", decision="GREEN",
        factors={"risk": "low", "complexity": "moderate"},
        weights={"risk": 1.0, "complexity": 0.5},
        rationale="all clear",
    )
    assert ok is True
    ledger = _ledger_mod.get_default_ledger()
    rows = ledger.reconstruct_op("op-2")
    assert rows[0].factors == {"risk": "low", "complexity": "moderate"}
    assert rows[0].weights == {"risk": 1.0, "complexity": 0.5}


# ---------------------------------------------------------------------------
# Producer-wiring — record_confidence
# ---------------------------------------------------------------------------


def test_record_confidence_master_off_returns_false(
    isolated_substrate,
):
    ok = record_confidence(
        classifier_name="route", confidence=0.7,
        threshold=0.5, outcome="STANDARD",
    )
    assert ok is False


def test_record_confidence_master_on_records(
    isolated_substrate, all_substrates_on,
):
    ok = record_confidence(
        classifier_name="route", confidence=0.7,
        threshold=0.5, outcome="STANDARD",
    )
    assert ok is True
    ring = _ring_mod.get_default_ring()
    events = ring.recent(n=5)
    assert len(events) == 1
    assert events[0].classifier_name == "route"
    assert events[0].confidence == pytest.approx(0.7)


# ---------------------------------------------------------------------------
# Producer-wiring — record_phase_latency + breach
# ---------------------------------------------------------------------------


def test_record_phase_latency_master_off_returns_false(
    isolated_substrate,
):
    ok = record_phase_latency("ROUTE", 0.123)
    assert ok is False


def test_record_phase_latency_master_on_records(
    isolated_substrate, all_substrates_on,
):
    ok = record_phase_latency("ROUTE", 0.123)
    assert ok is True


def test_check_breach_no_samples_returns_false(
    isolated_substrate, all_substrates_on,
):
    ok = check_breach_and_publish("ROUTE")
    assert ok is False


def test_check_breach_publishes_when_p95_exceeds_slo(
    isolated_substrate, all_substrates_on,
):
    detector = _slo_mod.get_default_detector()
    detector.set_slo("ROUTE", 0.05)
    for _ in range(25):
        record_phase_latency("ROUTE", 0.50)
    ok = check_breach_and_publish("ROUTE")
    assert ok is True


# ---------------------------------------------------------------------------
# Producer-wiring — flag-change publish
# ---------------------------------------------------------------------------


def test_check_flag_changes_master_off_returns_zero(
    isolated_substrate,
):
    n = check_flag_changes_and_publish()
    assert n == 0


def test_check_flag_changes_master_on_publishes_deltas(
    isolated_substrate, all_substrates_on,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("JARVIS_NEW_FLAG_FOR_TEST", "value1")
    n = check_flag_changes_and_publish()
    assert n >= 1


# ---------------------------------------------------------------------------
# Producer-wiring — append_timeline_event placeholder
# ---------------------------------------------------------------------------


def test_append_timeline_event_returns_false_placeholder():
    ok = append_timeline_event(
        op_id="op-x", event_type="custom",
        payload={"k": "v"},
    )
    assert ok is False  # placeholder for future write-side registry


# ---------------------------------------------------------------------------
# Producer-wiring — NEVER-raises smoke
# ---------------------------------------------------------------------------


def test_record_decision_never_raises_on_empty_inputs(
    isolated_substrate,
):
    ok = record_decision(op_id="", phase="", decision="")
    assert ok is False


def test_record_confidence_never_raises_on_non_numeric(
    isolated_substrate, all_substrates_on,
):
    """Non-numeric confidence/threshold — substrate's record()
    handles + returns (False, ...). Producer wraps + returns False."""
    ok = record_confidence(
        classifier_name="x",
        confidence="not a number",  # type: ignore[arg-type]
        threshold=0.5, outcome="X",
    )
    assert ok is False


def test_check_breach_never_raises_on_unknown_phase(
    isolated_substrate, all_substrates_on,
):
    ok = check_breach_and_publish("UNKNOWN_PHASE_XYZ")
    assert ok is False


def test_record_decision_never_raises_when_substrate_module_broken(
    isolated_substrate, all_substrates_on,
    monkeypatch: pytest.MonkeyPatch,
):
    """Force the substrate module to raise on import."""
    import sys
    target = (
        "backend.core.ouroboros.governance.observability."
        "decision_trace_ledger"
    )
    real = sys.modules.pop(target, None)

    class _Boom:
        def __getattr__(self, name):
            raise ImportError("simulated")

    sys.modules[target] = _Boom()  # type: ignore[assignment]
    try:
        ok = record_decision(
            op_id="op-broken", phase="ROUTE", decision="X",
        )
        assert ok is False
    finally:
        if real is not None:
            sys.modules[target] = real


# ---------------------------------------------------------------------------
# Producer-wiring — authority/cage invariants
# ---------------------------------------------------------------------------


def test_producers_does_not_import_gate_modules():
    import ast
    import inspect
    src = inspect.getsource(_producers)
    tree = ast.parse(src)
    banned = [
        "orchestrator", "iron_gate", "risk_tier_floor",
        "policy_engine", "candidate_generator",
        "tool_executor", "change_engine",
    ]
    for node in ast.walk(tree):
        names: List[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names = [node.module]
        for mod in names:
            for token in banned:
                assert token not in mod, (
                    f"phase8_producers imports {mod!r} "
                    f"(banned token {token!r})"
                )


def test_producers_top_level_imports_stdlib_only():
    """All substrate + SSE-bridge imports are LAZY inside helpers."""
    import ast
    import inspect
    src = inspect.getsource(_producers)
    tree = ast.parse(src)
    top_level: List[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            top_level.append(node.module)
    forbidden = {
        "backend.core.ouroboros.governance.observability."
        "decision_trace_ledger",
        "backend.core.ouroboros.governance.observability."
        "latent_confidence_ring",
        "backend.core.ouroboros.governance.observability."
        "flag_change_emitter",
        "backend.core.ouroboros.governance.observability."
        "latency_slo_detector",
        "backend.core.ouroboros.governance.observability."
        "multi_op_timeline",
        "backend.core.ouroboros.governance.observability."
        "sse_bridge",
    }
    leaked = forbidden & set(top_level)
    assert not leaked, f"hoisted substrate/bridge imports: {leaked}"


def test_producers_public_api_pinned():
    public = sorted(
        n for n in dir(_producers)
        if not n.startswith("_") and callable(getattr(_producers, n))
    )
    required = {
        "append_timeline_event",
        "check_breach_and_publish",
        "check_flag_changes_and_publish",
        "record_confidence",
        "record_decision",
        "record_phase_latency",
        "substrate_flag_snapshot",
    }
    missing = required - set(public)
    assert not missing, f"public API regression: {missing}"


def test_producers_no_secret_leakage():
    text = repr(vars(_producers))
    for needle in ("sk-", "ghp_", "AKIA", "BEGIN PRIVATE KEY"):
        assert needle not in text
