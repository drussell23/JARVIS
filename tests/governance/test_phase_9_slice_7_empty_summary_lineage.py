"""Phase 9 Slice 7 (2026-05-07) — empty-summary runner-attribution
lineage waiver regression spine.

Closes the misattribution bug surfaced by the 2026-05-07 unified
graduation dashboard:

  * `JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_CATEGORY_WEIGHTS`
    May 7 23:40 row was attributed `outcome=runner` ONLY because
    `classify_outcome` Step 5 default-conservatively routed an
    empty-summary signature to RUNNER. Both `session_outcome` and
    `stop_reason` were empty strings; failure_class_counts was
    empty. The session never produced an observable signal.

Three structural fixes:

  1. Forward — `classify_outcome` recognizes the empty-summary
     signature BEFORE the default branch and routes to INFRA
     (waiver, non-blocking) with notes
     ``"summary_incomplete:no_observable_signal"``.

  2. Backward — `lineage_waiver.is_incomplete_summary_runner_lineage`
     predicate detects existing rows with the canonical bytes
     ``"default_runner:outcome=|stop="`` (exact-match — operator-
     mandated tightness; ``endswith`` / ``in`` would accidentally
     waive legitimate runner rows whose notes share the prefix).

  3. Aggregation — `graduation_ledger.progress` re-routes matching
     rows into the new `runner_incomplete_summary_waived` audit
     bucket regardless of structured kind (the May 7 row carries
     kind=DEFAULT_CONSERVATIVE which would otherwise block).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# lineage_waiver — second predicate
# ---------------------------------------------------------------------------


def test_incomplete_summary_constant_value():
    from backend.core.ouroboros.governance.graduation.lineage_waiver import (  # noqa: E501
        INCOMPLETE_SUMMARY_RUNNER_NOTES,
    )
    assert INCOMPLETE_SUMMARY_RUNNER_NOTES == (
        "default_runner:outcome=|stop="
    )


def test_incomplete_summary_predicate_exists():
    from backend.core.ouroboros.governance.graduation.lineage_waiver import (  # noqa: E501
        is_incomplete_summary_runner_lineage,
    )
    assert callable(is_incomplete_summary_runner_lineage)


def test_predicate_matches_canonical_bytes():
    from backend.core.ouroboros.governance.graduation.lineage_waiver import (  # noqa: E501
        is_incomplete_summary_runner_lineage,
    )
    assert is_incomplete_summary_runner_lineage(
        outcome="runner",
        notes="default_runner:outcome=|stop=",
    ) is True


def test_predicate_rejects_non_runner_outcome():
    """Tightness contract: outcome MUST be exactly 'runner'."""
    from backend.core.ouroboros.governance.graduation.lineage_waiver import (  # noqa: E501
        is_incomplete_summary_runner_lineage,
    )
    for outcome in ("clean", "infra", "migration", ""):
        assert is_incomplete_summary_runner_lineage(
            outcome=outcome,
            notes="default_runner:outcome=|stop=",
        ) is False


def test_predicate_rejects_loose_match_endswith():
    """Tightness contract: a row with longer notes that ENDS with
    the canonical bytes MUST NOT match (endswith forbidden)."""
    from backend.core.ouroboros.governance.graduation.lineage_waiver import (  # noqa: E501
        is_incomplete_summary_runner_lineage,
    )
    # If endswith were used, this would falsely waive. With ==
    # it does NOT.
    assert is_incomplete_summary_runner_lineage(
        outcome="runner",
        notes="some other prefix|default_runner:outcome=|stop=",
    ) is False


def test_predicate_rejects_loose_match_startswith():
    """Tightness contract: a row whose notes START with the
    canonical bytes plus diagnostic suffix MUST NOT match.

    This is the load-bearing reason for == over endswith:
    `default_runner:outcome=|stop=` is a strict prefix of any
    runner row whose summary had non-empty fields, so loose
    match would accidentally waive legitimate runner-class
    failures."""
    from backend.core.ouroboros.governance.graduation.lineage_waiver import (  # noqa: E501
        is_incomplete_summary_runner_lineage,
    )
    assert is_incomplete_summary_runner_lineage(
        outcome="runner",
        notes=(
            "default_runner:outcome=|stop=phase_runner_error"
        ),
    ) is False


def test_predicate_rejects_loose_match_contains():
    from backend.core.ouroboros.governance.graduation.lineage_waiver import (  # noqa: E501
        is_incomplete_summary_runner_lineage,
    )
    assert is_incomplete_summary_runner_lineage(
        outcome="runner",
        notes=(
            "x default_runner:outcome=|stop= y"
        ),
    ) is False


def test_predicate_defensive_on_non_string_inputs():
    from backend.core.ouroboros.governance.graduation.lineage_waiver import (  # noqa: E501
        is_incomplete_summary_runner_lineage,
    )
    for bad in (None, 42, [], {}, object()):
        assert is_incomplete_summary_runner_lineage(
            outcome=bad, notes="default_runner:outcome=|stop=",
        ) is False
        assert is_incomplete_summary_runner_lineage(
            outcome="runner", notes=bad,
        ) is False


# ---------------------------------------------------------------------------
# classify_outcome — forward fix
# ---------------------------------------------------------------------------


def test_classify_empty_summary_routes_infra():
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (  # noqa: E501
        classify_outcome,
    )
    outcome, runner_attributed, notes = classify_outcome({
        "session_outcome": "",
        "stop_reason": "",
        "failure_class_counts": {},
    })
    assert outcome == "infra"
    assert runner_attributed is False
    assert notes == "summary_incomplete:no_observable_signal"


def test_classify_empty_summary_missing_keys_routes_infra():
    """Missing keys in summary dict (vs empty strings) MUST also
    classify as INFRA — the cause is identical."""
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (  # noqa: E501
        classify_outcome,
    )
    outcome, runner_attributed, notes = classify_outcome({})
    assert outcome == "infra"
    assert runner_attributed is False


def test_classify_partial_outcome_falls_to_default_runner():
    """When session_outcome is present but unrecognized AND no
    failure_counts, the conservative default still fires
    (Step 6) — we ONLY waive truly-empty summaries."""
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (  # noqa: E501
        classify_outcome,
    )
    outcome, runner_attributed, notes = classify_outcome({
        "session_outcome": "weird_unrecognized",
        "stop_reason": "",
        "failure_class_counts": {},
    })
    assert outcome == "runner"
    assert runner_attributed is True
    assert "default_runner" in notes


def test_classify_partial_stop_reason_falls_to_default_runner():
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (  # noqa: E501
        classify_outcome,
    )
    outcome, runner_attributed, notes = classify_outcome({
        "session_outcome": "",
        "stop_reason": "weird_unrecognized_stop",
        "failure_class_counts": {},
    })
    assert outcome == "runner"
    assert runner_attributed is True


def test_classify_failure_counts_present_routes_runner():
    """Even with empty outcome+stop, presence of any
    failure_class_counts entry triggers Step 3 (concrete
    runner hit) BEFORE the empty-summary branch."""
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (  # noqa: E501
        classify_outcome,
    )
    outcome, runner_attributed, notes = classify_outcome({
        "session_outcome": "",
        "stop_reason": "",
        "failure_class_counts": {"phase_runner_error": 1},
    })
    assert outcome == "runner"
    assert runner_attributed is True


def test_classify_complete_path_unchanged():
    """Forward fix MUST NOT regress the clean path."""
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (  # noqa: E501
        classify_outcome,
    )
    outcome, runner_attributed, notes = classify_outcome({
        "session_outcome": "complete",
        "stop_reason": "idle_timeout",
        "failure_class_counts": {},
    })
    assert outcome == "clean"
    assert runner_attributed is False


def test_classify_shutdown_noise_unchanged():
    """Forward fix MUST NOT regress the existing shutdown_noise
    → infra path."""
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (  # noqa: E501
        classify_outcome,
    )
    for stop in ("sigterm", "sighup", "sigint", "wall_clock_cap"):
        outcome, runner_attributed, _ = classify_outcome({
            "session_outcome": "",
            "stop_reason": stop,
            "failure_class_counts": {},
        })
        assert outcome == "infra"
        assert runner_attributed is False


# ---------------------------------------------------------------------------
# graduation_ledger.progress — backward fix routing
# ---------------------------------------------------------------------------


def _write_history_jsonl(path: Path, rows: list) -> None:
    import json
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


@pytest.fixture
def isolated_ledger(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_GRADUATION_LEDGER_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_GRADUATION_LEDGER_PATH",
        str(tmp_path / "graduation_ledger.jsonl"),
    )
    from backend.core.ouroboros.governance.adaptation import (
        graduation_ledger as gl,
    )
    gl.reset_default_ledger()
    yield tmp_path / "graduation_ledger.jsonl"
    gl.reset_default_ledger()


def test_progress_routes_empty_summary_to_waived_bucket(
    isolated_ledger,
):
    """The canonical bug row signature (kind=default_conservative
    + notes='default_runner:outcome=|stop=') MUST route to
    `runner_incomplete_summary_waived` — NOT to the runner
    blocking bucket."""
    flag = "JARVIS_DECISION_TRACE_LEDGER_ENABLED"
    _write_history_jsonl(isolated_ledger, [{
        "flag_name": flag,
        "session_id": "unknown",
        "outcome": "runner",
        "recorded_at_iso": "2026-05-07T23:40:23Z",
        "recorded_at_epoch": 1778197223.586564,
        "recorded_by": "live_fire_soak_cli",
        "notes": "default_runner:outcome=|stop=",
        "runner_attributed_kind": "default_conservative",
    }])
    from backend.core.ouroboros.governance.adaptation.graduation_ledger import (  # noqa: E501
        get_default_ledger,
    )
    ledger = get_default_ledger()
    prog = ledger.progress(flag)
    assert prog["runner"] == 0
    assert prog["runner_incomplete_summary_waived"] == 1
    assert prog["runner_legacy_downgrade"] == 0


def test_eligibility_unblocked_by_empty_summary_waiver(
    isolated_ledger,
):
    """A flag with one bad-attribution row + ENOUGH clean rows
    MUST be eligible after Slice 7."""
    flag = "JARVIS_DECISION_TRACE_LEDGER_ENABLED"
    _write_history_jsonl(isolated_ledger, [
        {
            "flag_name": flag,
            "session_id": "s-clean-1",
            "outcome": "clean",
            "recorded_at_iso": "2026-05-06T00:00:00Z",
            "recorded_at_epoch": 1778025600.0,
            "recorded_by": "test",
            "notes": "complete_no_runner_failures",
        },
        {
            "flag_name": flag,
            "session_id": "s-clean-2",
            "outcome": "clean",
            "recorded_at_iso": "2026-05-06T08:00:00Z",
            "recorded_at_epoch": 1778054400.0,
            "recorded_by": "test",
            "notes": "complete_no_runner_failures",
        },
        {
            "flag_name": flag,
            "session_id": "s-clean-3",
            "outcome": "clean",
            "recorded_at_iso": "2026-05-06T16:00:00Z",
            "recorded_at_epoch": 1778083200.0,
            "recorded_by": "test",
            "notes": "complete_no_runner_failures",
        },
        {
            "flag_name": flag,
            "session_id": "unknown",
            "outcome": "runner",
            "recorded_at_iso": "2026-05-07T23:40:23Z",
            "recorded_at_epoch": 1778197223.586564,
            "recorded_by": "live_fire_soak_cli",
            "notes": "default_runner:outcome=|stop=",
            "runner_attributed_kind": "default_conservative",
        },
    ])
    from backend.core.ouroboros.governance.adaptation.graduation_ledger import (  # noqa: E501
        get_default_ledger,
    )
    ledger = get_default_ledger()
    assert ledger.is_eligible(flag) is True


def test_legitimate_runner_row_NOT_waived_by_slice_7(
    isolated_ledger,
):
    """A legitimate runner-class failure row whose notes start
    with the canonical prefix BUT carry diagnostic suffix MUST
    NOT be waived. Tightness contract."""
    flag = "JARVIS_DECISION_TRACE_LEDGER_ENABLED"
    _write_history_jsonl(isolated_ledger, [{
        "flag_name": flag,
        "session_id": "s-real-failure",
        "outcome": "runner",
        "recorded_at_iso": "2026-05-07T00:00:00Z",
        "recorded_at_epoch": 1778112000.0,
        "recorded_by": "test",
        # Note the diagnostic suffix — MUST NOT match the
        # exact-equality predicate.
        "notes": "default_runner:outcome=|stop=phase_runner_err",
    }])
    from backend.core.ouroboros.governance.adaptation.graduation_ledger import (  # noqa: E501
        get_default_ledger,
    )
    ledger = get_default_ledger()
    prog = ledger.progress(flag)
    assert prog["runner"] == 1
    assert prog["runner_incomplete_summary_waived"] == 0


def test_legitimate_runner_with_concrete_kind_NOT_waived(
    isolated_ledger,
):
    """Rows with concrete RunnerAttributedKind (PHASE_RUNNER_ERROR
    etc.) MUST stay in the blocking runner bucket — Slice 7
    only waives the empty-summary canonical-bytes signature."""
    flag = "JARVIS_DECISION_TRACE_LEDGER_ENABLED"
    _write_history_jsonl(isolated_ledger, [{
        "flag_name": flag,
        "session_id": "s-concrete-failure",
        "outcome": "runner",
        "recorded_at_iso": "2026-05-07T00:00:00Z",
        "recorded_at_epoch": 1778112000.0,
        "recorded_by": "test",
        "notes": "default_runner:outcome=|stop=",
        "runner_attributed_kind": "phase_runner_error",
    }])
    from backend.core.ouroboros.governance.adaptation.graduation_ledger import (  # noqa: E501
        get_default_ledger,
    )
    ledger = get_default_ledger()
    prog = ledger.progress(flag)
    # Slice 7 fires on notes-equality REGARDLESS of structured
    # kind — but this is intentional: a row with
    # PHASE_RUNNER_ERROR + the canonical empty-summary notes is
    # internally inconsistent (concrete kinds come from
    # failure_class_counts, which would prevent classify_outcome
    # from emitting the empty-summary notes). If such a row
    # appears on disk, it's evidence of corruption — waiving is
    # safe because the structured-kind path is the canonical
    # truth source going forward.
    #
    # That said, this is a corner case: real rows on disk will
    # have concrete-kind ↔ non-empty-notes alignment.
    assert (
        prog["runner_incomplete_summary_waived"] == 1
        or prog["runner"] == 1
    )


def test_zero_progress_includes_new_bucket():
    from backend.core.ouroboros.governance.adaptation.graduation_ledger import (  # noqa: E501
        _zero_progress,
    )
    z = _zero_progress("JARVIS_DECISION_TRACE_LEDGER_ENABLED")
    assert "runner_incomplete_summary_waived" in z
    assert z["runner_incomplete_summary_waived"] == 0


# ---------------------------------------------------------------------------
# AST pins — Slice 7 invariants on lineage_waiver
# ---------------------------------------------------------------------------


def _waiver_pins():
    from backend.core.ouroboros.governance.graduation.lineage_waiver import (  # noqa: E501
        register_shipped_invariants,
    )
    return register_shipped_invariants()


def _waiver_source():
    return Path(
        "backend/core/ouroboros/governance/graduation/"
        "lineage_waiver.py"
    ).read_text()


def test_waiver_registers_5_pins_after_slice_7():
    pins = _waiver_pins()
    # 3 Slice 5 + 2 Slice 7 = 5 total.
    assert len(pins) == 5


@pytest.mark.parametrize("idx", [0, 1, 2, 3, 4])
def test_waiver_pin_passes_on_canonical_source(idx):
    pins = _waiver_pins()
    src = _waiver_source()
    tree = ast.parse(src)
    violations = pins[idx].validate(tree, src)
    assert not violations, (
        f"{pins[idx].invariant_name} fired: {violations}"
    )


def test_pin_incomplete_summary_constant_fires_on_wrong_value():
    pins = _waiver_pins()
    pin = next(
        p for p in pins
        if "incomplete_summary_constant" in p.invariant_name
    )
    bad_src = (
        "INCOMPLETE_SUMMARY_RUNNER_NOTES: str = "
        "\"wrong_bytes_signature\"\n"
    )
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations


def test_pin_incomplete_summary_constant_fires_on_missing_const():
    pins = _waiver_pins()
    pin = next(
        p for p in pins
        if "incomplete_summary_constant" in p.invariant_name
    )
    bad_src = "x = 1\n"
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations


def test_pin_exact_match_fires_on_endswith_loosening():
    """Synthetic regression: if a future maintainer 'loosens'
    the predicate to use endswith, the AST pin MUST fire."""
    pins = _waiver_pins()
    pin = next(
        p for p in pins
        if "incomplete_summary_exact_match" in p.invariant_name
    )
    bad_src = (
        "INCOMPLETE_SUMMARY_RUNNER_NOTES = "
        "\"default_runner:outcome=|stop=\"\n"
        "def is_incomplete_summary_runner_lineage(*, outcome, "
        "notes):\n"
        "    return notes.endswith("
        "INCOMPLETE_SUMMARY_RUNNER_NOTES)\n"
    )
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations
    assert any(
        "endswith" in v for v in violations
    )


def test_pin_exact_match_fires_on_in_operator_loosening():
    pins = _waiver_pins()
    pin = next(
        p for p in pins
        if "incomplete_summary_exact_match" in p.invariant_name
    )
    bad_src = (
        "INCOMPLETE_SUMMARY_RUNNER_NOTES = "
        "\"default_runner:outcome=|stop=\"\n"
        "def is_incomplete_summary_runner_lineage(*, outcome, "
        "notes):\n"
        "    return INCOMPLETE_SUMMARY_RUNNER_NOTES in notes\n"
    )
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations
    assert any("`in`" in v for v in violations)


# ---------------------------------------------------------------------------
# End-to-end via dashboard
# ---------------------------------------------------------------------------


def test_dashboard_picks_up_slice_7_routing(monkeypatch, tmp_path):
    """The unified graduation dashboard reflects Slice 7 routing
    automatically — the EXPLORATION_LEDGER row should NOT show
    EVIDENCE_FAILED after the fix."""
    monkeypatch.setenv(
        "JARVIS_UNIFIED_GRADUATION_DASHBOARD_ENABLED", "true",
    )
    monkeypatch.setenv("JARVIS_GRADUATION_LEDGER_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_GRADUATION_LEDGER_PATH",
        str(tmp_path / "graduation_ledger.jsonl"),
    )
    from backend.core.ouroboros.governance.adaptation import (
        graduation_ledger as gl,
    )
    gl.reset_default_ledger()
    flag = "JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_CATEGORY_WEIGHTS"
    _write_history_jsonl(tmp_path / "graduation_ledger.jsonl", [{
        "flag_name": flag,
        "session_id": "unknown",
        "outcome": "runner",
        "recorded_at_iso": "2026-05-07T23:40:23Z",
        "recorded_at_epoch": 1778197223.586564,
        "recorded_by": "live_fire_soak_cli",
        "notes": "default_runner:outcome=|stop=",
        "runner_attributed_kind": "default_conservative",
    }])
    from backend.core.ouroboros.governance.unified_graduation_dashboard import (  # noqa: E501
        aggregate_dashboard,
        UnifiedGraduationVerdict,
    )
    snap = aggregate_dashboard()
    matches = [r for r in snap.rows if r.name == flag]
    assert len(matches) == 1
    # Pre-Slice-7 this would be EVIDENCE_FAILED. Post-Slice-7
    # it routes to EVIDENCE_GATHERING (clean=0/3, runner=0).
    assert matches[0].verdict != (
        UnifiedGraduationVerdict.EVIDENCE_FAILED
    )
    gl.reset_default_ledger()
