"""Phase 9 Slice 7c (2026-05-07) — composite stop_reason +
incomplete_kill outcome regression spine.

Surfaced via active monitoring of the first cron-fired soak in
repo history (`bt-2026-05-08-022312`, May 8 03:10 UTC). Soak
hit the 40min wall-clock cap and wrote:

  * `session_outcome="incomplete_kill"`
  * `stop_reason="wall_clock_cap+atexit_fallback"` (composite —
    the harness's atexit fallback path appends `+atexit_fallback`
    to the original signal name per PRD §32.7 Battle Test
    partial-shutdown insurance)

Pre-Slice-7c `classify_outcome` Step 4:
  * Used exact set membership on `_SHUTDOWN_NOISE_STOP_REASONS`
    (no composite-prefix split) → composite reason didn't match.
  * Did NOT recognize `incomplete_kill` outcome as INFRA signal.
  * Both gaps → row classified as `runner` → Phase 9 cadence
    blocked for `JARVIS_COMMAND_BUS_BRIDGE_ENABLED`.

Slice 7c structural fixes:
  * Forward (live_fire_soak): canonical `_SHUTDOWN_NOISE_STOP_-
    REASONS` frozenset + `_INCOMPLETE_OUTCOME_VALUES` frozenset +
    `_is_shutdown_noise_stop` helper (composite-prefix split on
    `+`) → composite reasons + incomplete_kill route to INFRA.
  * Backward (lineage_waiver): `is_pre_slice_7c_shutdown_-
    misclassification` predicate + `_DEFAULT_RUNNER_NOTES_RE`
    parser → existing on-disk rows route to
    `runner_incomplete_summary_waived` audit bucket.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Test isolation
# ---------------------------------------------------------------------------


def _write_history_jsonl(path: Path, rows: list) -> None:
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


# ---------------------------------------------------------------------------
# Forward fix — _is_shutdown_noise_stop helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stop_reason,expected",
    [
        # Uncomposed — exact-match on canonical set.
        ("sigterm", True),
        ("sighup", True),
        ("sigint", True),
        # Layer 8 revision (v2.96, 2026-05-10): wall_clock_cap is
        # harness-intended (CLAUDE.md clean-bar equivalence to
        # idle_timeout) — NOT shutdown-noise. False now.
        ("wall_clock_cap", False),
        ("harness_idle_timeout", True),
        # Composite from May 8 soak — UNDER LAYER 8 also False
        # because the head segment `wall_clock_cap` is no longer
        # noise. The Layer 6 atexit fallback firing AFTER a
        # harness-intended wall-clock cap is a clean termination.
        ("wall_clock_cap+atexit_fallback", False),
        ("sigterm+drain_timeout", True),
        ("sigint+something", True),
        # Truly unknown.
        ("unknown_reason", False),
        ("provider_died", False),
        # Empty / None defensive.
        ("", False),
        (None, False),
        (42, False),
    ],
)
def test_is_shutdown_noise_stop(stop_reason, expected):
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (  # noqa: E501
        _is_shutdown_noise_stop,
    )
    assert _is_shutdown_noise_stop(stop_reason) is expected


def test_canonical_shutdown_noise_set_complete():
    """Layer 8 revision (v2.96, 2026-05-10): the canonical set
    is now 4 entries (was 5). ``wall_clock_cap`` REMOVED —
    harness-intended terminations classify as clean per
    CLAUDE.md battle-test footnote (clean-bar equivalence to
    idle_timeout)."""
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (  # noqa: E501
        _SHUTDOWN_NOISE_STOP_REASONS,
    )
    assert _SHUTDOWN_NOISE_STOP_REASONS == frozenset({
        "sigterm",
        "sighup",
        "sigint",
        "harness_idle_timeout",
    }), (
        "Layer 8 (v2.96) revised the canonical noise set: "
        "wall_clock_cap is removed (harness-intended, "
        "clean-bar-equivalent to idle_timeout). Drift here "
        "regresses to the pre-Layer-8 misclassification of "
        "bt-2026-05-10-221432 (soak #3) as outcome=infra."
    )


def test_incomplete_outcome_values_includes_incomplete_kill():
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (  # noqa: E501
        _INCOMPLETE_OUTCOME_VALUES,
    )
    assert "incomplete_kill" in _INCOMPLETE_OUTCOME_VALUES


# ---------------------------------------------------------------------------
# Forward fix — classify_outcome
# ---------------------------------------------------------------------------


def test_classify_composite_wall_clock_cap_legacy_incomplete_kill_routes_infra():
    """LEGACY pre-Layer-8 path: when the cause→outcome adapter
    emitted ``session_outcome=incomplete_kill`` for a wall-clock-
    cap-triggered atexit fallback (the bt-2026-05-10-221432
    soak #3 signature), the row classified as INFRA via the
    ``_INCOMPLETE_OUTCOME_VALUES`` predicate.

    Layer 8 (v2.96, 2026-05-10) fixes the adapter to emit
    ``session_outcome=complete`` for WALL_CLOCK_CAP, so NEW
    soaks under the post-Layer-8 harness emit the
    ``test_classify_composite_wall_clock_cap_layer8_routes_clean``
    signature below. HISTORICAL rows with the legacy
    ``incomplete_kill`` marker still classify as INFRA per
    this test — the legacy-marker semantics are preserved.
    """
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (  # noqa: E501
        classify_outcome,
    )
    outcome, runner_attributed, notes = classify_outcome({
        "session_outcome": "incomplete_kill",
        "stop_reason": "wall_clock_cap+atexit_fallback",
        "failure_class_counts": {},
    })
    assert outcome == "infra"
    assert runner_attributed is False
    assert "incomplete_outcome:incomplete_kill" in notes


def test_classify_composite_wall_clock_cap_layer8_routes_clean():
    """Layer 8 (v2.96, 2026-05-10) — FORWARD-FIX path.

    Under the post-Layer-8 harness, when wall_clock_cap fires
    AND the atexit fallback writes a partial summary (because
    the clean shutdown path was slow OR wedged), the cause→
    outcome adapter stamps ``session_outcome=complete`` (not
    ``incomplete_kill``). The composite ``stop_reason``
    ``wall_clock_cap+atexit_fallback`` is preserved for
    forensics — but the classifier sees a clean outcome AND a
    non-noise stop_reason (Layer 8 removed wall_clock_cap from
    the noise set), so the row classifies as CLEAN.

    This pins the load-bearing Layer 8 invariant: a future
    Phase 9 soak that hits wall_clock_cap under sleep (Layer 7
    dual-clock authority) + slow shutdown (Layer 6 atexit
    fallback) lands on the clean evidence ladder, matching
    CLAUDE.md's clean-bar-equivalence footnote."""
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (  # noqa: E501
        classify_outcome,
    )
    outcome, runner_attributed, notes = classify_outcome({
        "session_outcome": "complete",
        "stop_reason": "wall_clock_cap+atexit_fallback",
        "failure_class_counts": {},
    })
    assert outcome == "clean", (
        f"Layer 8: wall_clock_cap+atexit_fallback with "
        f"session_outcome=complete MUST classify as clean. "
        f"Got outcome={outcome!r} notes={notes!r}"
    )
    assert runner_attributed is False


def test_classify_incomplete_kill_alone_routes_infra():
    """Just `incomplete_kill` outcome (no stop_reason) MUST
    still route to INFRA — the outcome itself is the load-
    bearing signal."""
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (  # noqa: E501
        classify_outcome,
    )
    outcome, runner_attributed, _ = classify_outcome({
        "session_outcome": "incomplete_kill",
        "stop_reason": "",
        "failure_class_counts": {},
    })
    assert outcome == "infra"
    assert runner_attributed is False


def test_classify_composite_sigterm_routes_infra():
    """Composite `sigterm+drain_timeout` MUST also route to
    INFRA via the same prefix-split logic.

    Note: session_outcome="" (not "complete") because real
    sigterm-killed sessions don't reach the canonical
    "complete" terminal state; Step 1 clean-path is reserved
    for that. Sigterm-killed sessions have empty
    session_outcome OR `incomplete_kill`."""
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (  # noqa: E501
        classify_outcome,
    )
    outcome, runner_attributed, _ = classify_outcome({
        "session_outcome": "",
        "stop_reason": "sigterm+drain_timeout",
        "failure_class_counts": {},
    })
    assert outcome == "infra"
    assert runner_attributed is False


def test_classify_uncomposed_shutdown_unchanged():
    """Pre-Slice-7c known-good path: uncomposed external-signal
    stop_reasons still route to INFRA — Slice 7c was additive.

    Layer 8 revision (v2.96, 2026-05-10) — REMOVED
    ``wall_clock_cap`` from the loop (it's harness-intended,
    not external; classifies as clean under Layer 8). The
    remaining 4 entries are external-signal causes (the harness
    did NOT intend the termination) — still INFRA."""
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (  # noqa: E501
        classify_outcome,
    )
    for stop in (
        "sigterm",
        "sighup",
        "sigint",
        # wall_clock_cap REMOVED in Layer 8 — see
        # test_classify_uncomposed_wall_clock_cap_routes_clean
        # below for the post-Layer-8 expectation.
        "harness_idle_timeout",
    ):
        outcome, runner_attributed, _ = classify_outcome({
            "session_outcome": "",
            "stop_reason": stop,
            "failure_class_counts": {},
        })
        assert outcome == "infra", (
            f"external-signal stop_reason {stop!r} MUST still "
            f"route to infra (harness did not intend the "
            f"termination)"
        )
        assert runner_attributed is False


def test_classify_uncomposed_wall_clock_cap_routes_clean():
    """Layer 8 (v2.96, 2026-05-10): uncomposed
    ``stop_reason=wall_clock_cap`` (no atexit suffix — the clean
    shutdown path completed) classifies as CLEAN, mirroring
    ``idle_timeout`` which has always been clean per CLAUDE.md
    clean-bar-equivalence footnote."""
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (  # noqa: E501
        classify_outcome,
    )
    outcome, runner_attributed, _ = classify_outcome({
        "session_outcome": "complete",
        "stop_reason": "wall_clock_cap",
        "failure_class_counts": {},
    })
    assert outcome == "clean", (
        "Layer 8: uncomposed wall_clock_cap with "
        "session_outcome=complete MUST classify as clean — "
        "mirrors idle_timeout (the harness-intended clean stop)."
    )
    assert runner_attributed is False


def test_classify_unknown_stop_still_defaults_runner():
    """Pre-Slice-7c default-conservative path preserved:
    truly-unknown stop_reasons still route to RUNNER (Slice 7c
    adds shutdown-noise composite recognition; doesn't
    weaken default-conservative)."""
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (  # noqa: E501
        classify_outcome,
    )
    outcome, runner_attributed, _ = classify_outcome({
        "session_outcome": "weird_value",
        "stop_reason": "totally_random",
        "failure_class_counts": {},
    })
    assert outcome == "runner"
    assert runner_attributed is True


def test_classify_complete_path_unchanged_by_slice_7c():
    """Clean-path regression guard."""
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (  # noqa: E501
        classify_outcome,
    )
    outcome, runner_attributed, _ = classify_outcome({
        "session_outcome": "complete",
        "stop_reason": "idle_timeout",
        "failure_class_counts": {},
    })
    assert outcome == "clean"
    assert runner_attributed is False


# ---------------------------------------------------------------------------
# Backward fix — lineage_waiver predicate
# ---------------------------------------------------------------------------


def test_pre_slice_7c_predicate_matches_canonical_signature():
    """The exact bytes signature from May 8 03:10 row MUST
    be detected by the backward-fix predicate."""
    from backend.core.ouroboros.governance.graduation.lineage_waiver import (  # noqa: E501
        is_pre_slice_7c_shutdown_misclassification,
    )
    assert is_pre_slice_7c_shutdown_misclassification(
        outcome="runner",
        notes=(
            "default_runner:outcome=incomplete_kill|"
            "stop=wall_clock_cap+atexit_fallback"
        ),
    ) is True


def test_pre_slice_7c_predicate_matches_composite_sigterm():
    from backend.core.ouroboros.governance.graduation.lineage_waiver import (  # noqa: E501
        is_pre_slice_7c_shutdown_misclassification,
    )
    assert is_pre_slice_7c_shutdown_misclassification(
        outcome="runner",
        notes=(
            "default_runner:outcome=complete|"
            "stop=sigterm+drain"
        ),
    ) is True


def test_pre_slice_7c_predicate_rejects_non_runner_outcome():
    from backend.core.ouroboros.governance.graduation.lineage_waiver import (  # noqa: E501
        is_pre_slice_7c_shutdown_misclassification,
    )
    for outcome in ("clean", "infra", "migration", ""):
        assert is_pre_slice_7c_shutdown_misclassification(
            outcome=outcome,
            notes=(
                "default_runner:outcome=incomplete_kill|"
                "stop=wall_clock_cap"
            ),
        ) is False


def test_pre_slice_7c_predicate_rejects_unrelated_runner_row():
    """A legitimate runner-class failure with NON-shutdown stop
    + non-incomplete outcome MUST NOT be waived."""
    from backend.core.ouroboros.governance.graduation.lineage_waiver import (  # noqa: E501
        is_pre_slice_7c_shutdown_misclassification,
    )
    assert is_pre_slice_7c_shutdown_misclassification(
        outcome="runner",
        notes=(
            "default_runner:outcome=weird|"
            "stop=totally_random"
        ),
    ) is False


def test_pre_slice_7c_predicate_rejects_non_default_runner_notes():
    """Notes that don't match the `default_runner:` shape MUST
    NOT be parsed by this predicate."""
    from backend.core.ouroboros.governance.graduation.lineage_waiver import (  # noqa: E501
        is_pre_slice_7c_shutdown_misclassification,
    )
    assert is_pre_slice_7c_shutdown_misclassification(
        outcome="runner",
        notes=(
            "runner_classes:['phase_runner_error']"
        ),
    ) is False


def test_pre_slice_7c_predicate_defensive_on_non_string():
    from backend.core.ouroboros.governance.graduation.lineage_waiver import (  # noqa: E501
        is_pre_slice_7c_shutdown_misclassification,
    )
    for bad in (None, 42, [], {}):
        assert is_pre_slice_7c_shutdown_misclassification(
            outcome=bad,
            notes="default_runner:outcome=incomplete_kill|stop=wall_clock_cap",
        ) is False
        assert is_pre_slice_7c_shutdown_misclassification(
            outcome="runner",
            notes=bad,
        ) is False


# ---------------------------------------------------------------------------
# Aggregation — graduation_ledger.progress routing
# ---------------------------------------------------------------------------


def test_progress_routes_pre_slice_7c_row_to_waived_bucket(
    isolated_ledger,
):
    """The actual May 8 03:10 row signature MUST route to
    `runner_incomplete_summary_waived` (NOT to runner)."""
    flag = "JARVIS_DECISION_TRACE_LEDGER_ENABLED"
    _write_history_jsonl(isolated_ledger, [{
        "flag_name": flag,
        "session_id": "bt-2026-05-08-022312",
        "outcome": "runner",
        "recorded_at_iso": "2026-05-08T03:10:08Z",
        "recorded_at_epoch": 1778209808.292107,
        "recorded_by": "live_fire_soak_cli",
        "notes": (
            "default_runner:outcome=incomplete_kill|"
            "stop=wall_clock_cap+atexit_fallback"
        ),
        "runner_attributed_kind": "default_conservative",
    }])
    from backend.core.ouroboros.governance.adaptation.graduation_ledger import (  # noqa: E501
        get_default_ledger,
    )
    ledger = get_default_ledger()
    prog = ledger.progress(flag)
    assert prog["runner"] == 0
    assert prog["runner_incomplete_summary_waived"] == 1


def test_eligibility_unblocked_by_slice_7c_waiver(isolated_ledger):
    """3 clean + 1 pre-Slice-7c misattribution row → eligible."""
    flag = "JARVIS_DECISION_TRACE_LEDGER_ENABLED"
    _write_history_jsonl(isolated_ledger, [
        {
            "flag_name": flag,
            "session_id": f"s-clean-{i}",
            "outcome": "clean",
            "recorded_at_iso": "2026-05-06T00:00:00Z",
            "recorded_at_epoch": 1778025600.0,
            "recorded_by": "test",
            "notes": "complete_no_runner_failures",
        }
        for i in range(3)
    ] + [
        {
            "flag_name": flag,
            "session_id": "bt-2026-05-08-022312",
            "outcome": "runner",
            "recorded_at_iso": "2026-05-08T03:10:08Z",
            "recorded_at_epoch": 1778209808.0,
            "recorded_by": "live_fire_soak_cli",
            "notes": (
                "default_runner:outcome=incomplete_kill|"
                "stop=wall_clock_cap+atexit_fallback"
            ),
            "runner_attributed_kind": "default_conservative",
        },
    ])
    from backend.core.ouroboros.governance.adaptation.graduation_ledger import (  # noqa: E501
        get_default_ledger,
    )
    ledger = get_default_ledger()
    assert ledger.is_eligible(flag) is True


def test_legitimate_runner_NOT_waived_by_slice_7c(isolated_ledger):
    """A row with a real runner-class failure MUST NOT be
    waived by Slice 7c — the predicate is tight."""
    flag = "JARVIS_DECISION_TRACE_LEDGER_ENABLED"
    _write_history_jsonl(isolated_ledger, [{
        "flag_name": flag,
        "session_id": "s-real-failure",
        "outcome": "runner",
        "recorded_at_iso": "2026-05-08T00:00:00Z",
        "recorded_at_epoch": 1778198400.0,
        "recorded_by": "test",
        "notes": "runner_classes:['phase_runner_error']",
        "runner_attributed_kind": "phase_runner_error",
    }])
    from backend.core.ouroboros.governance.adaptation.graduation_ledger import (  # noqa: E501
        get_default_ledger,
    )
    ledger = get_default_ledger()
    prog = ledger.progress(flag)
    assert prog["runner"] == 1
    assert prog["runner_incomplete_summary_waived"] == 0
