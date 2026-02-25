"""Startup determinism invariants (Invariants 1-3)."""
import json


class TestNoOrphanLifecyclePhases:
    """Every phase_enter has a matching phase_exit or phase_fail."""

    def test_with_clean_startup(self, tmp_path):
        from tests.adversarial.invariant_checks import check_no_orphan_phases
        events = _make_clean_startup_events()
        _write_events(tmp_path, events)
        violations = check_no_orphan_phases(tmp_path / "lifecycle")
        assert violations == []

    def test_detects_orphan(self, tmp_path):
        from tests.adversarial.invariant_checks import check_no_orphan_phases
        events = _make_clean_startup_events()
        # Remove the phase_exit for preflight
        events = [
            e for e in events
            if not (e.get("event_type") == "phase_exit" and e.get("phase") == "preflight")
        ]
        _write_events(tmp_path, events)
        violations = check_no_orphan_phases(tmp_path / "lifecycle")
        assert len(violations) > 0
        assert "preflight" in violations[0]


class TestStartupPhaseDAGConsistency:
    """Phase transitions respect declared dependency DAG."""

    def test_correct_order(self, tmp_path):
        from tests.adversarial.invariant_checks import check_phase_dag_consistency
        events = _make_clean_startup_events()
        _write_events(tmp_path, events)
        violations = check_phase_dag_consistency(tmp_path / "lifecycle")
        assert violations == []

    def test_detects_out_of_order(self, tmp_path):
        from tests.adversarial.invariant_checks import check_phase_dag_consistency
        from backend.core.trace_envelope import TraceEnvelopeFactory
        factory = TraceEnvelopeFactory(
            repo="jarvis", boot_id="b1", runtime_epoch_id="e1",
            node_id="n1", producer_version="v1",
        )
        boot = factory.create_root(component="supervisor", operation="boot_start")
        # Jump straight to "backend" without completing earlier phases
        enter_env = factory.create_child(
            parent=boot, component="supervisor", operation="phase_backend",
        )
        events = [
            {"event_type": "boot_start", "envelope": boot.to_dict()},
            {"event_type": "phase_enter", "phase": "backend", "envelope": enter_env.to_dict()},
        ]
        _write_events(tmp_path, events)
        violations = check_phase_dag_consistency(tmp_path / "lifecycle")
        assert len(violations) > 0


class TestDeterministicReplay:
    """Same event stream produces identical final state twice."""

    def test_replay_determinism(self, tmp_path):
        from tests.adversarial.replay_engine import ReplayEngine
        events = _make_clean_startup_events()
        _write_events(tmp_path, events)
        engine = ReplayEngine()
        engine.load_streams(tmp_path / "lifecycle", tmp_path / "decisions")
        result1 = engine.replay()
        result2 = engine.replay()
        assert result1.final_state == result2.final_state
        assert result1.events_processed == result2.events_processed


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_clean_startup_events():
    """Create a valid startup event sequence for testing."""
    from backend.core.trace_envelope import TraceEnvelopeFactory
    factory = TraceEnvelopeFactory(
        repo="jarvis", boot_id="b1", runtime_epoch_id="e1",
        node_id="n1", producer_version="v1",
    )
    boot = factory.create_root(component="supervisor", operation="boot_start")
    phases = [
        "clean_slate", "preflight", "resources", "backend",
        "intelligence", "trinity", "enterprise",
    ]
    events = [{"event_type": "boot_start", "envelope": boot.to_dict()}]
    prev_event_id = boot.event_id
    for phase in phases:
        enter_env = factory.create_child(
            parent=boot, component="supervisor",
            operation=f"phase_{phase}", caused_by_event_id=prev_event_id,
        )
        events.append({
            "event_type": "phase_enter",
            "phase": phase,
            "envelope": enter_env.to_dict(),
        })
        exit_env = factory.create_event_from(enter_env)
        events.append({
            "event_type": "phase_exit",
            "phase": phase,
            "to_state": "success",
            "envelope": exit_env.to_dict(),
        })
        prev_event_id = exit_env.event_id

    complete = factory.create_child(
        parent=boot, component="supervisor", operation="boot_complete",
    )
    events.append({"event_type": "boot_complete", "envelope": complete.to_dict()})
    return events


def _write_events(tmp_path, events):
    lifecycle_dir = tmp_path / "lifecycle"
    lifecycle_dir.mkdir(parents=True, exist_ok=True)
    decisions_dir = tmp_path / "decisions"
    decisions_dir.mkdir(parents=True, exist_ok=True)
    with open(lifecycle_dir / "test_epoch.jsonl", "w") as f:
        for event in events:
            f.write(json.dumps(event) + "\n")
