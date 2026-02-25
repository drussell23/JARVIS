"""Ordering guarantee invariants (Invariants 8-9)."""
import json


class TestLamportMonotonic:
    """Within a trace, Lamport sequences must be monotonically increasing."""

    def test_monotonic_sequence(self, tmp_path):
        from tests.adversarial.invariant_checks import check_lamport_monotonic
        from backend.core.trace_envelope import TraceEnvelopeFactory
        factory = TraceEnvelopeFactory(
            repo="jarvis", boot_id="b1", runtime_epoch_id="e1",
            node_id="n1", producer_version="v1",
        )
        boot = factory.create_root(component="supervisor", operation="boot_start")
        phase = factory.create_child(
            parent=boot, component="supervisor", operation="phase_enter",
        )
        exit_env = factory.create_event_from(phase)
        events = [
            {"event_type": "boot_start", "envelope": boot.to_dict()},
            {"event_type": "phase_enter", "phase": "preflight", "envelope": phase.to_dict()},
            {"event_type": "phase_exit", "phase": "preflight", "envelope": exit_env.to_dict()},
        ]
        _write_events(tmp_path, events)
        violations = check_lamport_monotonic(tmp_path / "lifecycle")
        assert violations == []

    def test_detects_non_monotonic(self, tmp_path):
        from tests.adversarial.invariant_checks import check_lamport_monotonic
        # Manually craft events with out-of-order Lamport sequences
        events = [
            {
                "event_type": "boot_start",
                "envelope": {
                    "trace_id": "t1", "event_id": "e1", "sequence": 5,
                    "ts_wall_utc": 1000.0,
                },
            },
            {
                "event_type": "phase_enter",
                "envelope": {
                    "trace_id": "t1", "event_id": "e2", "sequence": 3,  # Non-monotonic!
                    "ts_wall_utc": 1001.0,
                },
            },
        ]
        _write_events(tmp_path, events)
        violations = check_lamport_monotonic(tmp_path / "lifecycle")
        assert len(violations) > 0
        assert "non-monotonic" in violations[0].lower() or "Non-monotonic" in violations[0]


class TestCausalityDAGAcyclic:
    """The causality graph must be a DAG (no cycles)."""

    def test_valid_dag(self, tmp_path):
        from tests.adversarial.invariant_checks import check_causality_acyclic
        from backend.core.trace_envelope import TraceEnvelopeFactory
        factory = TraceEnvelopeFactory(
            repo="jarvis", boot_id="b1", runtime_epoch_id="e1",
            node_id="n1", producer_version="v1",
        )
        boot = factory.create_root(component="supervisor", operation="boot_start")
        phase = factory.create_child(
            parent=boot, component="supervisor", operation="phase_enter",
            caused_by_event_id=boot.event_id,
        )
        events = [
            {"event_type": "boot_start", "envelope": boot.to_dict()},
            {"event_type": "phase_enter", "envelope": phase.to_dict()},
        ]
        _write_events(tmp_path, events)
        violations = check_causality_acyclic(tmp_path / "lifecycle")
        assert violations == []

    def test_detects_self_cycle(self, tmp_path):
        from tests.adversarial.invariant_checks import check_causality_acyclic
        events = [
            {
                "event_type": "broken",
                "envelope": {
                    "trace_id": "t1", "event_id": "e1",
                    "caused_by_event_id": "e1",  # Self-reference!
                },
            },
        ]
        _write_events(tmp_path, events)
        violations = check_causality_acyclic(tmp_path / "lifecycle")
        assert len(violations) > 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_events(tmp_path, events):
    lifecycle_dir = tmp_path / "lifecycle"
    lifecycle_dir.mkdir(parents=True, exist_ok=True)
    with open(lifecycle_dir / "test_epoch.jsonl", "w") as f:
        for event in events:
            f.write(json.dumps(event) + "\n")
