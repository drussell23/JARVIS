"""Boundary propagation invariants (Invariants 6-7)."""
class TestCriticalBoundariesCarryEnvelope:
    """All events should have a non-empty envelope with trace_id."""

    def test_all_events_have_envelope(self):
        from tests.adversarial.invariant_checks import check_critical_boundaries_have_envelope
        from backend.core.trace_envelope import TraceEnvelopeFactory
        factory = TraceEnvelopeFactory(
            repo="jarvis", boot_id="b1", runtime_epoch_id="e1",
            node_id="n1", producer_version="v1",
        )
        boot = factory.create_root(component="supervisor", operation="boot_start")
        phase = factory.create_child(
            parent=boot, component="supervisor", operation="phase_enter",
        )
        events = [
            {"event_type": "boot_start", "envelope": boot.to_dict()},
            {"event_type": "phase_enter", "phase": "preflight", "envelope": phase.to_dict()},
        ]
        violations = check_critical_boundaries_have_envelope(events)
        assert violations == []

    def test_detects_missing_envelope(self):
        from tests.adversarial.invariant_checks import check_critical_boundaries_have_envelope
        events = [
            {"event_type": "boot_start", "envelope": {"trace_id": "t1"}},
            {"event_type": "phase_enter", "phase": "preflight"},  # No envelope!
        ]
        violations = check_critical_boundaries_have_envelope(events)
        assert len(violations) > 0

    def test_detects_empty_trace_id(self):
        from tests.adversarial.invariant_checks import check_critical_boundaries_have_envelope
        events = [
            {"event_type": "boot_start", "envelope": {"trace_id": ""}},
        ]
        violations = check_critical_boundaries_have_envelope(events)
        assert len(violations) > 0


class TestCrossRepoEnvelopeRoundTrip:
    """Envelope round-trip preserves all fields."""

    def test_root_envelope_round_trip(self):
        from tests.adversarial.invariant_checks import check_envelope_round_trip
        from backend.core.trace_envelope import TraceEnvelopeFactory
        factory = TraceEnvelopeFactory(
            repo="jarvis", boot_id="b1", runtime_epoch_id="e1",
            node_id="n1", producer_version="v1",
        )
        env = factory.create_root(component="supervisor", operation="boot_start")
        violations = check_envelope_round_trip(env.to_dict())
        assert violations == []

    def test_child_envelope_round_trip(self):
        from tests.adversarial.invariant_checks import check_envelope_round_trip
        from backend.core.trace_envelope import TraceEnvelopeFactory
        factory = TraceEnvelopeFactory(
            repo="jarvis", boot_id="b1", runtime_epoch_id="e1",
            node_id="n1", producer_version="v1",
        )
        root = factory.create_root(component="supervisor", operation="boot_start")
        child = factory.create_child(
            parent=root, component="prime", operation="model_load",
            caused_by_event_id=root.event_id,
            idempotency_key="load:q8:nonce-abc",
        )
        violations = check_envelope_round_trip(child.to_dict())
        assert violations == []
