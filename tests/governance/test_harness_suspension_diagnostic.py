"""
Task #94 spine — WallClockWatchdog suspension diagnostic.

Session bt-2026-05-14-075335 surfaced an observability gap: a session
suspended by macOS (process paused while laptop slept for ~6 hours)
fires stop_reason=wall_clock_cap identical to a clean full-runtime
cap-hit. A graduation/Bar A claim from such a session is evidence-
invalid, but currently nothing in summary.json distinguishes the two.

Task #94 (operator-approved 2026-05-14) adds:

  * WARN log at WallClockWatchdog fire-time when monotonic/wall ratio
    falls below JARVIS_HARNESS_SUSPENSION_WARN_RATIO (default 0.5).
  * Additive ``suspension_likely: bool`` + ``suspension_ratio: float``
    fields in summary.json (schema_version unchanged — pre-1.1c
    consumers parse cleanly because fields only emit when set).
  * Threading through both the clean ``_generate_report`` path AND
    the ``_atexit_fallback_write`` partial-summary path so the
    diagnostic survives signal-driven shutdowns.

No behavior change to WHEN the watchdog fires (effective = max(
monotonic, wall_clock) per Ticket A1 Guard 2 stays unchanged) — pure
diagnostic emission.

This spine pins:

  * The ratio formula: ``elapsed_monotonic / elapsed_wall``.
  * The default threshold: 0.5 (env-tunable
    JARVIS_HARNESS_SUSPENSION_WARN_RATIO).
  * Pure-diagnostic invariant: detection branch sets internal state
    AND emits WARN, but does NOT change the existing stop_reason
    classification logic (still ``wall_clock_cap`` per Ticket A1).
  * Schema additive: summary.json's ``suspension_likely`` /
    ``suspension_ratio`` fields are emitted ONLY when non-None so
    legacy v1.1a + v1.1b summary consumers parse the file without
    changes.
  * FlagRegistry seed present.

Tests use AST inspection + monkeypatched clocks where applicable,
following the existing test_harness_partial_shutdown.py pattern.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest


_HARNESS_SRC = (
    Path(__file__).parents[2]
    / "backend" / "core" / "ouroboros" / "battle_test" / "harness.py"
)
_RECORDER_SRC = (
    Path(__file__).parents[2]
    / "backend" / "core" / "ouroboros" / "battle_test" / "session_recorder.py"
)
_SEED_SRC = (
    Path(__file__).parents[2]
    / "backend" / "core" / "ouroboros" / "governance"
    / "flag_registry_seed.py"
)


# ---------------------------------------------------------------------------
# AST pins — wiring in place
# ---------------------------------------------------------------------------


def test_ast_pin_harness_computes_suspension_ratio():
    """WallClockWatchdog firing path MUST compute the monotonic/wall
    ratio (not just emit a generic skew warning earlier in the loop —
    that one is the legacy clock-skew detector at line ~4780)."""
    src = _HARNESS_SRC.read_text(encoding="utf-8")
    assert "JARVIS_HARNESS_SUSPENSION_WARN_RATIO" in src, (
        "WallClockWatchdog firing path MUST consult "
        "JARVIS_HARNESS_SUSPENSION_WARN_RATIO env knob"
    )
    assert "_susp_ratio = max(0.0, min(1.0, _fired_monotonic / _fired_wall))" in src, (
        "Firing path MUST compute the monotonic/wall ratio with "
        "clamp to [0, 1]"
    )
    assert "self._suspension_likely = _susp_likely" in src, (
        "Firing path MUST stamp self._suspension_likely so "
        "save_summary can surface it"
    )
    assert "self._suspension_ratio = _susp_ratio" in src, (
        "Firing path MUST stamp self._suspension_ratio for "
        "structured PRD evidence"
    )


def test_ast_pin_harness_emits_suspension_warn_log():
    """The diagnostic WARN log MUST include the explicit graduation-
    invalid messaging operator-approved 2026-05-14."""
    src = _HARNESS_SRC.read_text(encoding="utf-8")
    assert "SUSPENSION LIKELY" in src, (
        "Firing path MUST emit a SUSPENSION LIKELY WARN log line"
    )
    assert "Graduation / Bar A claims from this session are" in src, (
        "WARN message MUST explicitly cite Bar A graduation invalidity"
    )
    assert "INVALID unless re-run under caffeinate" in src, (
        "WARN message MUST cite caffeinate as the remediation per "
        "operator binding"
    )


def test_ast_pin_save_summary_accepts_new_fields():
    """SessionRecorder.save_summary MUST accept suspension_likely +
    suspension_ratio as new optional kwargs."""
    src = _RECORDER_SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    save_summary_fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "save_summary":
            save_summary_fn = node
            break
    assert save_summary_fn is not None, (
        "session_recorder.py MUST expose save_summary"
    )
    kwonly_names = [a.arg for a in save_summary_fn.args.kwonlyargs] + \
                   [a.arg for a in save_summary_fn.args.args]
    assert "suspension_likely" in kwonly_names, (
        "save_summary MUST accept suspension_likely kwarg (Task #94)"
    )
    assert "suspension_ratio" in kwonly_names, (
        "save_summary MUST accept suspension_ratio kwarg (Task #94)"
    )


def test_ast_pin_save_summary_emits_additive_fields():
    """The new fields MUST be emitted only when non-None — preserves
    legacy summary.json consumer compatibility (v1.1a + v1.1b)."""
    src = _RECORDER_SRC.read_text(encoding="utf-8")
    assert 'summary["suspension_likely"]' in src, (
        "save_summary MUST emit summary['suspension_likely']"
    )
    assert 'summary["suspension_ratio"]' in src, (
        "save_summary MUST emit summary['suspension_ratio']"
    )
    # Both gated on `is not None` to preserve additive discipline
    assert "if suspension_likely is not None:" in src, (
        "summary['suspension_likely'] emission MUST be gated on "
        "is-not-None for additive schema discipline"
    )
    assert "if suspension_ratio is not None:" in src, (
        "summary['suspension_ratio'] emission MUST be gated on "
        "is-not-None for additive schema discipline"
    )


def test_ast_pin_atexit_fallback_threads_suspension_fields():
    """Partial-summary path (signal-driven shutdown) MUST also carry
    the suspension diagnostic — if SIGTERM arrives after suspension
    detection but before clean shutdown, the partial summary should
    still surface the warning."""
    src = _HARNESS_SRC.read_text(encoding="utf-8")
    # Look for the atexit fallback's save_summary call
    # (the second of two save_summary calls in harness.py)
    atexit_section = src[src.find("_atexit_fallback_write"):src.find("_generate_report")]
    if not atexit_section:
        # Falling back to defensive lookup
        atexit_section = src
    assert "suspension_likely=getattr(self," in atexit_section, (
        "Atexit fallback MUST thread suspension_likely via getattr "
        "(defensive — the field may not exist if shutdown fires "
        "before init completes)"
    )
    assert "suspension_ratio=getattr(self," in atexit_section, (
        "Atexit fallback MUST thread suspension_ratio via getattr"
    )


def test_ast_pin_stop_reason_unchanged():
    """LOAD-BEARING invariant: the existing stop_reason classification
    MUST be unchanged.  Task #94 is pure diagnostic — it does not
    change ``stop_reason`` to a new value or remove the existing
    ``wall_clock_cap`` stamping, because that would break existing
    summary.json consumers (audit tooling, LSS, etc.).
    """
    src = _HARNESS_SRC.read_text(encoding="utf-8")
    # The legacy stop_reason="wall_clock_cap" stamp must still appear
    # at the firing path AND must NOT be conditioned on suspension state
    assert 'self._stop_reason = "wall_clock_cap"' in src, (
        "WallClockWatchdog firing path MUST still stamp "
        "stop_reason='wall_clock_cap' (legacy contract preserved)"
    )
    # Negative pin — there must NOT be a stop_reason like
    # 'wall_clock_cap_under_suspension' that would change consumer
    # contracts (operator binding 2026-05-14: don't change stop_reason)
    assert "wall_clock_cap_under_suspension" not in src, (
        "Task #94 MUST NOT introduce a new stop_reason variant — "
        "operator binding: 'avoid breaking consumers; additive over "
        "modifying'"
    )


# ---------------------------------------------------------------------------
# FlagRegistry seed
# ---------------------------------------------------------------------------


def test_seed_has_suspension_warn_ratio_flag():
    src = _SEED_SRC.read_text(encoding="utf-8")
    assert "JARVIS_HARNESS_SUSPENSION_WARN_RATIO" in src
    idx = src.find("JARVIS_HARNESS_SUSPENSION_WARN_RATIO")
    window = src[idx:idx + 1500]
    assert "default=0.5" in window, (
        "Default threshold MUST be 0.5 per operator binding"
    )
    assert "Category.TUNING" in window, (
        "Should be Category.TUNING (operator-tunable observability)"
    )
    assert "harness.py" in window, (
        "Source file MUST point at harness.py"
    )


# ---------------------------------------------------------------------------
# Behavioral decision-table — ratio math + threshold honoring
# ---------------------------------------------------------------------------


def _compute_suspension_verdict(
    monotonic_s: float, wall_s: float, threshold: float,
) -> tuple[bool, float]:
    """Mirrors the harness's firing-path detection logic exactly.

    Returns ``(suspension_likely, ratio)``.  Used in spine tests so
    the decision branch is deterministic without spinning up the full
    harness.  Tracks any future divergence between this and the live
    code via the AST pin above.
    """
    if wall_s > 0.0:
        ratio = max(0.0, min(1.0, monotonic_s / wall_s))
    else:
        # Defensive: wall=0 means session didn't run; treat as not
        # suspended (firing under zero wall shouldn't happen but the
        # production code guards against it too).
        return False, 0.0
    suspension_likely = ratio < threshold
    return suspension_likely, ratio


@pytest.mark.parametrize("monotonic,wall,thresh,expected_likely,expected_ratio", [
    # Clean cap-hit: monotonic ~= wall (process was running the whole time)
    (2400.0, 2400.0, 0.5, False, 1.0),
    # Mild overshoot in wall (NTP jump 5s): still clean
    (2400.0, 2405.0, 0.5, False, pytest.approx(0.998, abs=0.01)),
    # Session-075335 reality: 68s mono, 7238s wall → suspended
    (68.0, 7238.0, 0.5, True, pytest.approx(0.00939, abs=0.001)),
    # Borderline at threshold: ratio = exactly 0.5 → NOT suspended
    # (strict-less-than, not less-equal)
    (1200.0, 2400.0, 0.5, False, 0.5),
    # Just below threshold: 49% monotonic
    (1175.0, 2400.0, 0.5, True, pytest.approx(0.4896, abs=0.001)),
    # Operator-tuned tighter threshold (0.9 → only clean runs pass)
    (2400.0, 2500.0, 0.9, False, pytest.approx(0.96, abs=0.01)),
    (2400.0, 2700.0, 0.9, True, pytest.approx(0.889, abs=0.01)),
    # Operator-tuned looser threshold (0.1 → only severe suspension warns)
    (300.0, 2400.0, 0.1, False, 0.125),
    (200.0, 2400.0, 0.1, True, pytest.approx(0.0833, abs=0.001)),
])
def test_suspension_verdict_decision_table(
    monotonic, wall, thresh, expected_likely, expected_ratio,
):
    likely, ratio = _compute_suspension_verdict(monotonic, wall, thresh)
    assert likely is expected_likely, (
        f"verdict mismatch: mono={monotonic} wall={wall} thresh={thresh} → "
        f"expected likely={expected_likely}, got {likely} (ratio={ratio})"
    )
    assert ratio == expected_ratio


def test_zero_wall_does_not_emit_warning():
    """Defensive case: wall_clock=0 (impossible in practice but defensive
    test) must NOT crash and must NOT mark suspension."""
    likely, ratio = _compute_suspension_verdict(0.0, 0.0, 0.5)
    assert likely is False
    assert ratio == 0.0


def test_ratio_clamped_to_unit_interval():
    """If wall < monotonic (impossible in practice — would mean monotonic
    advanced faster than wall, which can't happen), the ratio is
    clamped to 1.0 so it doesn't pretend to "anti-suspension"."""
    # Simulate monotonic > wall (clock weirdness)
    likely, ratio = _compute_suspension_verdict(2500.0, 2400.0, 0.5)
    assert ratio == 1.0  # clamped
    assert likely is False  # 1.0 > 0.5, no suspension


# ---------------------------------------------------------------------------
# Schema-additive contract — summary.json round-trip
# ---------------------------------------------------------------------------


def test_summary_json_additive_fields_round_trip(tmp_path: Path):
    """End-to-end: save_summary writes the new fields when set + the
    resulting JSON is parseable by stdlib json (no schema breakage)."""
    from backend.core.ouroboros.battle_test.session_recorder import (
        SessionRecorder,
    )

    sr = SessionRecorder(session_id="test-task-94")
    summary_path = sr.save_summary(
        output_dir=tmp_path,
        stop_reason="wall_clock_cap",
        duration_s=2430.0,
        cost_total=0.0,
        cost_breakdown={},
        branch_stats={"commits": 0, "files_changed": 0,
                       "insertions": 0, "deletions": 0},
        convergence_state="INSUFFICIENT_DATA",
        convergence_slope=0.0,
        convergence_r2=0.0,
        suspension_likely=True,
        suspension_ratio=0.05,
    )
    parsed = json.loads(summary_path.read_text())
    assert parsed["suspension_likely"] is True
    assert parsed["suspension_ratio"] == 0.05
    # Legacy fields still present
    assert parsed["stop_reason"] == "wall_clock_cap"
    assert "schema_version" in parsed


def test_summary_json_omits_none_fields(tmp_path: Path):
    """When suspension_likely/ratio are not passed, the fields MUST
    NOT appear in summary.json — preserves legacy parseability."""
    from backend.core.ouroboros.battle_test.session_recorder import (
        SessionRecorder,
    )

    sr = SessionRecorder(session_id="test-task-94-omit")
    summary_path = sr.save_summary(
        output_dir=tmp_path,
        stop_reason="wall_clock_cap",
        duration_s=2400.0,
        cost_total=0.0,
        cost_breakdown={},
        branch_stats={"commits": 0, "files_changed": 0,
                       "insertions": 0, "deletions": 0},
        convergence_state="INSUFFICIENT_DATA",
        convergence_slope=0.0,
        convergence_r2=0.0,
        # suspension_likely / suspension_ratio intentionally omitted
    )
    parsed = json.loads(summary_path.read_text())
    assert "suspension_likely" not in parsed, (
        "Legacy callers (no suspension args) MUST NOT trigger field "
        "emission — preserves v1.1a/v1.1b consumer parseability"
    )
    assert "suspension_ratio" not in parsed
    # Legacy stop_reason unchanged
    assert parsed["stop_reason"] == "wall_clock_cap"
