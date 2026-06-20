"""Sovereign Exception Taxonomy — FSM-exhaustion must not be mislabeled transport.

Cloud soak 2026-06-20: a single
``all_providers_exhausted:fallback_skipped:no_fallback_configured`` RuntimeError was
classified ``LIVE_TRANSPORT`` on all 16 DW models, severing the whole lane +
corrupting surface-health. These tests pin the carve-out: such an OUR-side FSM
exhaustion is ``FSM_EXHAUSTED`` (weight 0.0, sever-immune), never a vendor rupture.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.dw_fault_taxonomy import (
    is_fsm_exhaustion,
    is_generation_timeout,
    is_internal_fault,
)
from backend.core.ouroboros.governance.topology_sentinel import (
    FailureSource,
    failure_weight,
)


@pytest.mark.parametrize("msg", [
    "all_providers_exhausted:fallback_skipped:no_fallback_configured",
    "sentinel_dispatch_no_fallback:nvidia/NVIDIA-Nemotron:live_transport",
    "fallback_skipped:no_fallback_configured",
    "background_dw_blocked_by_topology:dw_severed_queued:all_models_open",
    "speculative_deferred:dw_severed_queued:x",
])
def test_fsm_exhaustion_markers_match(msg):
    assert is_fsm_exhaustion(RuntimeError(msg)) is True


@pytest.mark.parametrize("msg", [
    "Connection reset by peer",
    "Server disconnected",
    "TimeoutError",
    "stream stalled mid-response",
])
def test_genuine_transport_errors_do_not_match(msg):
    assert is_fsm_exhaustion(RuntimeError(msg)) is False


def test_fsm_exhaustion_is_segregated_from_other_taxonomies():
    e = RuntimeError("all_providers_exhausted:fallback_skipped:no_fallback_configured")
    assert is_fsm_exhaustion(e) is True
    assert is_internal_fault(e) is False      # not a python logic bug
    assert is_generation_timeout(e) is False  # not a tool-loop budget timeout


def test_fsm_exhaustion_never_raises():
    class _Bad(Exception):
        def __str__(self):
            raise ValueError("boom")
    assert is_fsm_exhaustion(_Bad()) is False


def test_fsm_exhausted_enum_exists_and_is_sever_immune():
    assert FailureSource.FSM_EXHAUSTED.value == "fsm_exhausted"
    # weight 0.0 → never contributes to the weighted-streak that severs the lane
    assert failure_weight(FailureSource.FSM_EXHAUSTED) == 0.0
    # and it is NOT the transport source the degrade/sever consumers act on
    assert FailureSource.FSM_EXHAUSTED is not FailureSource.LIVE_TRANSPORT


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
