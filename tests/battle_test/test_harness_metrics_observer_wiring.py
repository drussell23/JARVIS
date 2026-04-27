"""P4 Slice 5 deferred follow-up — harness MetricsSessionObserver wiring test.

Pins the structural integration of `MetricsSessionObserver.record_session_end`
into the harness's `_generate_report` session-end path. This is the
deferred-follow-up wiring that completes Phase 4 P4 (the observer was
registered + reachable via REPL/IDE GET after Slice 5 graduation, but the
harness did not call it on session-end; this PR closes that gap).

Authority invariants pinned:

  * The wiring is invoked in `_generate_report` AFTER `summary.json`
    is written (so the observer can MERGE its `metrics` block into the
    existing file via read-modify-write) and BEFORE the session_replay
    builder runs (so replay.html sees the merged content).
  * The wiring is purely additive — same try/except shape as the
    sibling session_replay block. No existing behavior changes.
  * Master flag JARVIS_METRICS_SUITE_ENABLED was already graduated
    in Slice 5 (default true); the new wiring does NOT change that.
    The observer's master-off short-circuit still gates any compute.
  * Best-effort by construction — ImportError + bare-Exception both
    swallowed so an observer crash NEVER crashes the report.
  * The wiring uses the singleton `get_default_observer()` so any
    other call site (REPL, future SSE consumer) sees the same
    instance state (broker_warned / summary_warned dedup flags).
"""
from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parent.parent.parent
_HARNESS = (
    _REPO / "backend" / "core" / "ouroboros" / "battle_test"
    / "harness.py"
)


def _read() -> str:
    return _HARNESS.read_text(encoding="utf-8")


# ===========================================================================
# A — Wiring presence (source-grep)
# ===========================================================================


def test_harness_imports_metrics_observability():
    """Pin: harness.py imports `get_default_observer` from
    `metrics_observability` via function-local import (mirrors the
    sibling session_replay pattern — keeps static import surface
    minimum + degrades cleanly if module absent)."""
    src = _read()
    assert (
        "from backend.core.ouroboros.governance.metrics_observability import"
        in src
    ), "harness.py must import from metrics_observability"
    assert "get_default_observer" in src, (
        "harness.py must reference get_default_observer (singleton "
        "consumer pattern)"
    )


def test_harness_calls_record_session_end():
    """Pin: the observer's `record_session_end` is the call site."""
    src = _read()
    assert "record_session_end(" in src, (
        "harness must call observer.record_session_end(...)"
    )


def test_harness_calls_observer_with_expected_kwargs():
    """Pin: the wiring passes session_id + session_dir + ops +
    total_cost_usd + commits as kwargs (the observer's documented
    contract)."""
    src = _read()
    call_idx = src.find("record_session_end(")
    assert call_idx > 0
    # Take a 1500-char window starting at the call site.
    block = src[call_idx:call_idx + 1500]
    for kw in ("session_id", "session_dir", "ops", "total_cost_usd",
               "commits"):
        assert re.search(rf"\b{kw}\s*=", block), (
            f"observer call must pass {kw} as kwarg"
        )


def test_harness_reads_recorder_operations_field():
    """Pin: ops are sourced from the SessionRecorder's _operations
    list. Uses getattr with empty-list default so the wiring is
    resilient if the recorder shape changes."""
    src = _read()
    assert re.search(
        r'getattr\(\s*self\._session_recorder\s*,\s*"_operations"',
        src,
    ), "wiring must read self._session_recorder._operations via getattr"


def test_harness_passes_branch_stats_commits():
    """Pin: commits count flows from branch_stats (already computed
    earlier in _generate_report)."""
    src = _read()
    # branch_stats.get("commits", 0) appears in the wiring block
    call_idx = src.find("record_session_end(")
    block = src[call_idx:call_idx + 1500]
    assert re.search(r'branch_stats\.get\(\s*"commits"\s*,', block), (
        "wiring must pass branch_stats commits to observer"
    )


def test_harness_passes_total_cost_from_cost_tracker():
    """Pin: total_cost_usd flows from self._cost_tracker.total_spent
    (same field source as the existing summary.json write)."""
    src = _read()
    call_idx = src.find("record_session_end(")
    block = src[call_idx:call_idx + 1500]
    assert "self._cost_tracker.total_spent" in block, (
        "wiring must pass cost_tracker.total_spent to observer"
    )


# ===========================================================================
# B — Authority invariants (call-site ordering + best-effort)
# ===========================================================================


def test_wiring_runs_after_summary_json_write():
    """The observer MERGES into summary.json via read-modify-write,
    so it must run AFTER the recorder's save_summary call."""
    src = _read()
    # The recorder save call uses `self._session_recorder.save_summary(`
    # which appears multiple times (atexit fallback + clean path).
    # Find the clean path one inside _generate_report.
    gen_idx = src.find("async def _generate_report")
    assert gen_idx > 0
    save_idx = src.find("self._session_recorder.save_summary(", gen_idx)
    record_idx = src.find("record_session_end(", gen_idx)
    assert save_idx > 0 and record_idx > 0
    assert record_idx > save_idx, (
        "MetricsObserver wiring must run AFTER session_recorder."
        f"save_summary (save@{save_idx} record@{record_idx})"
    )


def test_wiring_runs_before_session_replay_builder():
    """The session_replay builder consumes the MERGED summary.json
    (with the metrics block landed), so it must run AFTER the
    observer wiring."""
    src = _read()
    gen_idx = src.find("async def _generate_report")
    assert gen_idx > 0
    record_idx = src.find("record_session_end(", gen_idx)
    replay_idx = src.find("SessionReplayBuilder", gen_idx)
    assert record_idx > 0 and replay_idx > 0
    assert record_idx < replay_idx, (
        "MetricsObserver wiring must run BEFORE SessionReplayBuilder "
        f"(record@{record_idx} replay@{replay_idx})"
    )


def test_wiring_uses_defensive_try_except_pattern():
    """Pin: the wiring is wrapped in try / except ImportError /
    except Exception — same best-effort shape as the session_replay
    block. An observer failure must NOT propagate."""
    src = _read()
    marker = "Phase 4 P4 Slice 5 follow-up: MetricsSessionObserver"
    idx = src.find(marker)
    assert idx > 0, "wiring section comment marker missing"
    block = src[idx:idx + 3500]
    assert "try:" in block, "wiring must be in a try block"
    assert "except ImportError:" in block, (
        "wiring must catch ImportError (defensive — module may be "
        "absent in some test envs)"
    )
    assert "except Exception" in block, (
        "wiring must catch bare Exception (best-effort contract)"
    )


def test_wiring_logs_observation_summary():
    """Pin: structured telemetry log surfaces ledger_appended +
    summary_merged + sse_published flags. Operators need this to
    debug observer state without rummaging through the JSONL."""
    src = _read()
    marker = "Phase 4 P4 Slice 5 follow-up: MetricsSessionObserver"
    idx = src.find(marker)
    block = src[idx:idx + 3500]
    assert "MetricsObserver" in block and "snapshot recorded" in block, (
        "wiring must log when a snapshot was recorded (operator "
        "visibility)"
    )
    for flag in ("ledger_appended", "summary_merged", "sse_published"):
        assert flag in block, (
            f"wiring telemetry must surface observer.{flag} flag"
        )


def test_wiring_uses_singleton_observer():
    """Pin: wiring uses get_default_observer() (singleton) rather
    than constructing a fresh `MetricsSessionObserver()`. Singleton
    pattern preserves the warned-once dedup state across multiple
    session-ends in a single process (rare but possible — long-lived
    daemon)."""
    src = _read()
    marker = "Phase 4 P4 Slice 5 follow-up: MetricsSessionObserver"
    idx = src.find(marker)
    block = src[idx:idx + 3500]
    assert "get_default_observer()" in block, (
        "wiring must use the get_default_observer() singleton"
    )
    assert "MetricsSessionObserver(" not in block, (
        "wiring should NOT construct a fresh observer instance — use "
        "the singleton to share warned-once dedup state"
    )


# ===========================================================================
# C — Observer contract sanity (smoke)
# ===========================================================================


def test_observer_module_exports_expected_surface():
    """Pin the observer's public surface that the wiring depends on.
    If the signature changes, this test fails and the wiring must be
    updated in lockstep."""
    from backend.core.ouroboros.governance.metrics_observability import (
        MetricsSessionObserver,
        get_default_observer,
    )
    import inspect
    sig = inspect.signature(MetricsSessionObserver.record_session_end)
    expected_kwargs = {
        "session_id", "session_dir", "ops", "sessions_history",
        "posture_dwells", "total_cost_usd", "commits",
    }
    actual_kwargs = set(sig.parameters.keys())
    assert expected_kwargs.issubset(actual_kwargs), (
        f"observer signature missing expected kwargs: "
        f"{expected_kwargs - actual_kwargs}"
    )
    # And the singleton accessor exists.
    inst = get_default_observer()
    assert isinstance(inst, MetricsSessionObserver)


def test_observer_returns_session_observation_dataclass():
    """Pin: observer returns SessionObservation with the four flag
    fields the wiring reads (snapshot, ledger_appended,
    summary_merged, sse_published, notes)."""
    from backend.core.ouroboros.governance.metrics_observability import (
        SessionObservation,
    )
    import dataclasses
    fields = {f.name for f in dataclasses.fields(SessionObservation)}
    for required in ("snapshot", "ledger_appended", "summary_merged",
                     "sse_published", "notes"):
        assert required in fields, (
            f"SessionObservation missing field {required}"
        )


def test_observer_master_off_short_circuits(monkeypatch, tmp_path):
    """Integration smoke: with the metrics master flag off, the
    observer returns notes=('master_off',) with snapshot=None. The
    wiring's `if _metrics_observation.snapshot is not None` branch
    then logs the master_off path and no-ops everything else."""
    monkeypatch.setenv("JARVIS_METRICS_SUITE_ENABLED", "0")
    # Reset singletons so the env override is honored on next call.
    from backend.core.ouroboros.governance import metrics_observability
    metrics_observability._default_observer = None  # type: ignore[attr-defined]
    obs = metrics_observability.get_default_observer()
    out = obs.record_session_end(
        session_id="bt-test-master-off",
        session_dir=tmp_path,
        ops=(),
        total_cost_usd=0.0,
        commits=0,
    )
    assert out.snapshot is None
    assert "master_off" in out.notes
    # Reset for other tests.
    metrics_observability._default_observer = None  # type: ignore[attr-defined]


def test_observer_with_minimal_inputs_does_not_raise(tmp_path, monkeypatch):
    """Smoke: observer accepts the harness's actual call shape (empty
    ops + zero cost + zero commits) and returns a SessionObservation
    without raising. Pins that the harness's defensive defaults
    (no ops, no cost) don't trip the engine's invariants."""
    monkeypatch.setenv("JARVIS_METRICS_SUITE_ENABLED", "1")
    from backend.core.ouroboros.governance import metrics_observability
    metrics_observability._default_observer = None  # type: ignore[attr-defined]
    obs = metrics_observability.get_default_observer()
    out = obs.record_session_end(
        session_id="bt-test-minimal",
        session_dir=tmp_path,
        ops=(),
        sessions_history=(),
        posture_dwells=(),
        total_cost_usd=0.0,
        commits=0,
    )
    # Snapshot should be produced (even with empty ops the engine
    # builds a valid skeleton snapshot).
    assert out.snapshot is not None
    metrics_observability._default_observer = None  # type: ignore[attr-defined]


# ===========================================================================
# D — Master flag default preserved (graduation invariant)
# ===========================================================================


def test_metrics_master_flag_still_default_true_post_wiring(monkeypatch):
    """Pin: the metrics master flag is still default true (graduated
    in Phase 4 P4 Slice 5). This wiring follow-up does NOT change
    that. Hot-revert (set env to false) still works because the
    observer short-circuits inside record_session_end."""
    monkeypatch.delenv("JARVIS_METRICS_SUITE_ENABLED", raising=False)
    from backend.core.ouroboros.governance.metrics_observability import (
        is_enabled,
    )
    assert is_enabled() is True


# ===========================================================================
# E — SessionRecorder field name pinned (the wiring depends on it)
# ===========================================================================


def test_session_recorder_has_operations_attribute():
    """The wiring reads `self._session_recorder._operations` (private
    field). If a future refactor renames this, this test fails and
    the wiring must be updated. Smoke-test by instantiating a fresh
    recorder + asserting the field exists + is a list."""
    from backend.core.ouroboros.battle_test.session_recorder import (
        SessionRecorder,
    )
    rec = SessionRecorder(session_id="bt-test-shape")
    assert hasattr(rec, "_operations"), (
        "SessionRecorder must expose _operations — wiring depends on this"
    )
    assert isinstance(getattr(rec, "_operations"), list), (
        "_operations must be a list (observer expects Sequence)"
    )
