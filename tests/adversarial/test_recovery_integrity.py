"""Recovery integrity invariants (Invariants 4-5)."""
import json


class TestNoDuplicateSideEffects:
    """No two events with the same idempotency_key should both succeed."""

    def test_clean_events_no_duplicates(self, tmp_path):
        from tests.adversarial.invariant_checks import check_no_duplicate_side_effects
        from backend.core.trace_envelope import TraceEnvelopeFactory
        factory = TraceEnvelopeFactory(
            repo="jarvis", boot_id="b1", runtime_epoch_id="e1",
            node_id="n1", producer_version="v1",
        )
        env1 = factory.create_root(
            component="vm_manager", operation="terminate_vm",
            idempotency_key="terminate:vm-001:nonce-abc",
        )
        env2 = factory.create_root(
            component="vm_manager", operation="terminate_vm",
            idempotency_key="terminate:vm-002:nonce-def",
        )
        events = [
            {"event_type": "action", "to_state": "success", "envelope": env1.to_dict()},
            {"event_type": "action", "to_state": "success", "envelope": env2.to_dict()},
        ]
        _write_events(tmp_path, events)
        violations = check_no_duplicate_side_effects(tmp_path / "lifecycle")
        assert violations == []

    def test_detects_duplicate_idempotency_key(self, tmp_path):
        from tests.adversarial.invariant_checks import check_no_duplicate_side_effects
        from backend.core.trace_envelope import TraceEnvelopeFactory
        factory = TraceEnvelopeFactory(
            repo="jarvis", boot_id="b1", runtime_epoch_id="e1",
            node_id="n1", producer_version="v1",
        )
        env1 = factory.create_root(
            component="vm_manager", operation="terminate_vm",
            idempotency_key="terminate:vm-001:nonce-abc",
        )
        env2 = factory.create_root(
            component="vm_manager", operation="terminate_vm",
            idempotency_key="terminate:vm-001:nonce-abc",  # Same key!
        )
        events = [
            {"event_type": "action", "to_state": "success", "envelope": env1.to_dict()},
            {"event_type": "action", "to_state": "success", "envelope": env2.to_dict()},
        ]
        _write_events(tmp_path, events)
        violations = check_no_duplicate_side_effects(tmp_path / "lifecycle")
        assert len(violations) > 0
        assert "terminate:vm-001:nonce-abc" in violations[0]


class TestCausalChainIntegrity:
    """Every caused_by_event_id must reference an event seen earlier."""

    def test_valid_chain(self, tmp_path):
        from tests.adversarial.invariant_checks import check_causal_chain_integrity
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
            {"event_type": "phase_enter", "phase": "preflight", "envelope": phase.to_dict()},
        ]
        _write_events(tmp_path, events)
        violations = check_causal_chain_integrity(tmp_path / "lifecycle")
        assert violations == []

    def test_detects_broken_chain(self, tmp_path):
        from tests.adversarial.invariant_checks import check_causal_chain_integrity
        from backend.core.trace_envelope import TraceEnvelopeFactory
        factory = TraceEnvelopeFactory(
            repo="jarvis", boot_id="b1", runtime_epoch_id="e1",
            node_id="n1", producer_version="v1",
        )
        boot = factory.create_root(component="supervisor", operation="boot_start")
        phase = factory.create_child(
            parent=boot, component="supervisor", operation="phase_enter",
            caused_by_event_id="nonexistent-event-id",
        )
        events = [
            {"event_type": "boot_start", "envelope": boot.to_dict()},
            {"event_type": "phase_enter", "phase": "preflight", "envelope": phase.to_dict()},
        ]
        _write_events(tmp_path, events)
        violations = check_causal_chain_integrity(tmp_path / "lifecycle")
        assert len(violations) > 0
        assert "nonexistent-event-id" in violations[0]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_events(tmp_path, events):
    lifecycle_dir = tmp_path / "lifecycle"
    lifecycle_dir.mkdir(parents=True, exist_ok=True)
    with open(lifecycle_dir / "test_epoch.jsonl", "w") as f:
        for event in events:
            f.write(json.dumps(event) + "\n")
