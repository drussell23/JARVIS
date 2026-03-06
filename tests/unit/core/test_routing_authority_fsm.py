"""Tests for RoutingAuthorityFSM -- fail-closed routing authority state machine.

Disease 10 -- Startup Sequencing, Task 3.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Dict

import pytest

from backend.core.routing_authority_fsm import (
    AuthorityState,
    RoutingAuthorityFSM,
    TransitionResult,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _passing_guards() -> Dict[str, bool]:
    return {
        "core_ready_passed": True,
        "contracts_valid": True,
        "invariants_clean": True,
        "hybrid_router_ready": True,
        "lease_or_local_ready": True,
        "readiness_contract_passed": True,
        "no_in_flight_requests": True,
    }


def _begin_guards(*, override: Dict[str, bool] | None = None) -> Dict[str, bool]:
    """Return the 3 guards needed for begin_handoff, optionally overridden."""
    guards = {
        "core_ready_passed": True,
        "contracts_valid": True,
        "invariants_clean": True,
    }
    if override:
        guards.update(override)
    return guards


def _complete_guards(*, override: Dict[str, bool] | None = None) -> Dict[str, bool]:
    """Return the 6 guards needed for complete_handoff, optionally overridden."""
    guards = {
        "contracts_valid": True,
        "invariants_clean": True,
        "hybrid_router_ready": True,
        "lease_or_local_ready": True,
        "readiness_contract_passed": True,
        "no_in_flight_requests": True,
    }
    if override:
        guards.update(override)
    return guards


# ---------------------------------------------------------------------------
# TestFSMInitialState
# ---------------------------------------------------------------------------


class TestFSMInitialState:
    """Verify the FSM starts in a safe default state."""

    def test_starts_in_boot_policy_active(self):
        fsm = RoutingAuthorityFSM()
        assert fsm.state == AuthorityState.BOOT_POLICY_ACTIVE

    def test_authority_holder_is_boot_policy(self):
        fsm = RoutingAuthorityFSM()
        assert fsm.authority_holder == "boot_policy"

    def test_transition_log_is_empty(self):
        fsm = RoutingAuthorityFSM()
        assert fsm.transition_log == []


# ---------------------------------------------------------------------------
# TestBootToHandoff
# ---------------------------------------------------------------------------


class TestBootToHandoff:
    """Verify begin_handoff transitions."""

    def test_begin_handoff_succeeds_with_all_guards(self):
        fsm = RoutingAuthorityFSM()
        result = fsm.begin_handoff(_begin_guards())

        assert result.success is True
        assert result.from_state == AuthorityState.BOOT_POLICY_ACTIVE.value
        assert result.to_state == AuthorityState.HANDOFF_PENDING.value
        assert result.failed_guard is None
        assert fsm.state == AuthorityState.HANDOFF_PENDING
        assert fsm.authority_holder == "handoff_controller"

    def test_begin_handoff_fails_on_unmet_guard(self):
        fsm = RoutingAuthorityFSM()
        result = fsm.begin_handoff(_begin_guards(override={"core_ready_passed": False}))

        assert result.success is False
        assert result.from_state == AuthorityState.BOOT_POLICY_ACTIVE.value
        assert result.to_state == AuthorityState.BOOT_POLICY_ACTIVE.value
        assert result.failed_guard == "core_ready_passed"
        assert fsm.state == AuthorityState.BOOT_POLICY_ACTIVE
        assert fsm.authority_holder == "boot_policy"

    def test_begin_handoff_from_wrong_state_fails(self):
        fsm = RoutingAuthorityFSM()
        # Move to HANDOFF_PENDING first
        result1 = fsm.begin_handoff(_begin_guards())
        assert result1.success is True

        # Attempting begin_handoff again from HANDOFF_PENDING should fail
        result2 = fsm.begin_handoff(_begin_guards())
        assert result2.success is False
        assert result2.from_state == AuthorityState.HANDOFF_PENDING.value
        assert result2.to_state == AuthorityState.HANDOFF_PENDING.value


# ---------------------------------------------------------------------------
# TestHandoffToHybrid
# ---------------------------------------------------------------------------


class TestHandoffToHybrid:
    """Verify complete_handoff transitions."""

    @pytest.fixture()
    def fsm_in_handoff(self) -> RoutingAuthorityFSM:
        """Return an FSM already in HANDOFF_PENDING state."""
        fsm = RoutingAuthorityFSM()
        result = fsm.begin_handoff(_begin_guards())
        assert result.success is True
        return fsm

    def test_complete_handoff_succeeds(self, fsm_in_handoff: RoutingAuthorityFSM):
        result = fsm_in_handoff.complete_handoff(_complete_guards())

        assert result.success is True
        assert result.from_state == AuthorityState.HANDOFF_PENDING.value
        assert result.to_state == AuthorityState.HYBRID_ACTIVE.value
        assert result.failed_guard is None
        assert fsm_in_handoff.state == AuthorityState.HYBRID_ACTIVE
        assert fsm_in_handoff.authority_holder == "hybrid_router"

    def test_complete_handoff_fails_on_unmet_guard(self, fsm_in_handoff: RoutingAuthorityFSM):
        result = fsm_in_handoff.complete_handoff(
            _complete_guards(override={"hybrid_router_ready": False})
        )

        assert result.success is False
        assert result.from_state == AuthorityState.HANDOFF_PENDING.value
        assert result.to_state == AuthorityState.HANDOFF_FAILED.value
        assert result.failed_guard == "hybrid_router_ready"
        assert fsm_in_handoff.state == AuthorityState.HANDOFF_FAILED
        assert fsm_in_handoff.authority_holder == "handoff_controller"

    def test_handoff_failed_auto_rollback(self, fsm_in_handoff: RoutingAuthorityFSM):
        # Drive to HANDOFF_FAILED
        result = fsm_in_handoff.complete_handoff(
            _complete_guards(override={"hybrid_router_ready": False})
        )
        assert result.success is False
        assert fsm_in_handoff.state == AuthorityState.HANDOFF_FAILED

        # Rollback from HANDOFF_FAILED -> BOOT_POLICY_ACTIVE
        rb = fsm_in_handoff.rollback("auto recovery after handoff failure")
        assert rb.success is True
        assert rb.to_state == AuthorityState.BOOT_POLICY_ACTIVE.value
        assert fsm_in_handoff.state == AuthorityState.BOOT_POLICY_ACTIVE
        assert fsm_in_handoff.authority_holder == "boot_policy"


# ---------------------------------------------------------------------------
# TestCatastrophicRollback
# ---------------------------------------------------------------------------


class TestCatastrophicRollback:
    """Verify rollback from various states."""

    @pytest.fixture()
    def fsm_hybrid(self) -> RoutingAuthorityFSM:
        """Return an FSM already in HYBRID_ACTIVE state."""
        fsm = RoutingAuthorityFSM()
        fsm.begin_handoff(_begin_guards())
        fsm.complete_handoff(_complete_guards())
        assert fsm.state == AuthorityState.HYBRID_ACTIVE
        return fsm

    def test_rollback_from_hybrid_on_lease_loss(self, fsm_hybrid: RoutingAuthorityFSM):
        result = fsm_hybrid.rollback("lease lost")
        assert result.success is True
        assert result.to_state == AuthorityState.BOOT_POLICY_ACTIVE.value
        assert fsm_hybrid.state == AuthorityState.BOOT_POLICY_ACTIVE
        assert fsm_hybrid.authority_holder == "boot_policy"

    def test_rollback_records_cause(self, fsm_hybrid: RoutingAuthorityFSM):
        fsm_hybrid.rollback("gcp_vm_preempted")
        log = fsm_hybrid.transition_log
        # Find the rollback entry (last one)
        rollback_entry = log[-1]
        assert rollback_entry["cause"] == "gcp_vm_preempted"

    def test_rollback_from_boot_policy_is_noop(self):
        fsm = RoutingAuthorityFSM()
        result = fsm.rollback("nothing to rollback")
        assert result.success is True
        assert result.from_state == AuthorityState.BOOT_POLICY_ACTIVE.value
        assert result.to_state == AuthorityState.BOOT_POLICY_ACTIVE.value
        assert fsm.state == AuthorityState.BOOT_POLICY_ACTIVE


# ---------------------------------------------------------------------------
# TestGuardEvaluationOrder
# ---------------------------------------------------------------------------


class TestGuardEvaluationOrder:
    """Verify guards are checked in deterministic priority order."""

    def test_guards_evaluated_in_deterministic_order(self):
        """When multiple guards fail, the first in priority order is reported."""
        fsm = RoutingAuthorityFSM()
        # All begin_handoff guards fail. Priority: core_ready_passed first.
        result = fsm.begin_handoff({
            "core_ready_passed": False,
            "contracts_valid": False,
            "invariants_clean": False,
        })
        assert result.success is False
        assert result.failed_guard == "core_ready_passed"

    def test_complete_handoff_guards_evaluated_in_deterministic_order(self):
        """When multiple complete_handoff guards fail, the first in priority is reported."""
        fsm = RoutingAuthorityFSM()
        fsm.begin_handoff(_begin_guards())

        # All complete_handoff guards fail. Priority: contracts_valid first.
        result = fsm.complete_handoff({
            "contracts_valid": False,
            "invariants_clean": False,
            "hybrid_router_ready": False,
            "lease_or_local_ready": False,
            "readiness_contract_passed": False,
            "no_in_flight_requests": False,
        })
        assert result.success is False
        assert result.failed_guard == "contracts_valid"


# ---------------------------------------------------------------------------
# TestTokenUniqueness
# ---------------------------------------------------------------------------


class TestTokenUniqueness:
    """Verify only one authority holder at a time."""

    def test_only_one_authority_token(self):
        fsm = RoutingAuthorityFSM()
        assert fsm.is_authority("boot_policy") is True
        assert fsm.is_authority("handoff_controller") is False

        fsm.begin_handoff(_begin_guards())
        assert fsm.is_authority("boot_policy") is False
        assert fsm.is_authority("handoff_controller") is True
        assert fsm.is_authority("hybrid_router") is False


# ---------------------------------------------------------------------------
# TestTransitionLog
# ---------------------------------------------------------------------------


class TestTransitionLog:
    """Verify transition log records and isolation."""

    def test_transitions_are_logged(self):
        fsm = RoutingAuthorityFSM()
        fsm.begin_handoff(_begin_guards())

        log = fsm.transition_log
        assert len(log) == 1
        entry = log[0]
        assert entry["from_state"] == AuthorityState.BOOT_POLICY_ACTIVE.value
        assert entry["to_state"] == AuthorityState.HANDOFF_PENDING.value

    def test_transition_log_is_a_copy(self):
        fsm = RoutingAuthorityFSM()
        fsm.begin_handoff(_begin_guards())

        log = fsm.transition_log
        assert len(log) == 1
        log.clear()
        # Internal log should be unaffected
        assert len(fsm.transition_log) == 1


# ---------------------------------------------------------------------------
# TestJournalPersistence
# ---------------------------------------------------------------------------


class TestJournalPersistence:
    """Verify journal write and recovery."""

    @pytest.fixture()
    def journal_dir(self):
        """Create a writable temp directory for journal tests."""
        base = os.environ.get("TMPDIR", "/private/tmp/claude-501")
        d = tempfile.mkdtemp(prefix="fsm_journal_", dir=base)
        p = Path(d)
        yield p
        shutil.rmtree(str(p), ignore_errors=True)

    def test_journal_write(self, journal_dir):
        journal_file = journal_dir / "fsm_journal.jsonl"
        fsm = RoutingAuthorityFSM(journal_path=str(journal_file))

        fsm.begin_handoff(_begin_guards())

        assert journal_file.exists()
        lines = journal_file.read_text().strip().split("\n")
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["from_state"] == AuthorityState.BOOT_POLICY_ACTIVE.value
        assert entry["to_state"] == AuthorityState.HANDOFF_PENDING.value

    def test_journal_recovery(self, journal_dir):
        """FSM recovers to BOOT_POLICY_ACTIVE when journal has HANDOFF_PENDING."""
        journal_file = journal_dir / "fsm_journal.jsonl"
        # Write a journal entry simulating a crash in HANDOFF_PENDING
        entry = {
            "from_state": AuthorityState.BOOT_POLICY_ACTIVE.value,
            "to_state": AuthorityState.HANDOFF_PENDING.value,
            "timestamp": 12345.0,
            "cause": "",
            "failed_guard": None,
        }
        journal_file.write_text(json.dumps(entry) + "\n")

        # Create FSM with existing journal -- should recover to BOOT_POLICY_ACTIVE
        fsm = RoutingAuthorityFSM(journal_path=str(journal_file))
        assert fsm.state == AuthorityState.BOOT_POLICY_ACTIVE
        assert fsm.authority_holder == "boot_policy"

    def test_journal_recovery_from_handoff_failed(self, journal_dir):
        """FSM recovers to BOOT_POLICY_ACTIVE when journal has HANDOFF_FAILED."""
        journal_file = journal_dir / "fsm_journal.jsonl"
        entry = {
            "from_state": AuthorityState.HANDOFF_PENDING.value,
            "to_state": AuthorityState.HANDOFF_FAILED.value,
            "timestamp": 12345.0,
            "cause": "",
            "failed_guard": "hybrid_router_ready",
        }
        journal_file.write_text(json.dumps(entry) + "\n")

        fsm = RoutingAuthorityFSM(journal_path=str(journal_file))
        assert fsm.state == AuthorityState.BOOT_POLICY_ACTIVE
        assert fsm.authority_holder == "boot_policy"
