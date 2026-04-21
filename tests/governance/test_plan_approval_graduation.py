"""Slice 5 graduation pins — problem #7 Plan Mode.

Plan-approval-mode is different from Gap #6 surfaces: the default
stays `false` because turning it on halts every op for human
approval. Graduation here means "the mechanism is proven and
production-ready; the operator chooses when to engage it."

These pins lock the shipped semantics so future refactors don't
accidentally (a) flip the default to true (would break autonomy),
(b) remove the env knob, or (c) regress the authority invariant.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.ouroboros.governance import (
    ide_observability,
    ide_observability_stream,
    plan_approval,
    plan_approval_repl,
)
from backend.core.ouroboros.governance.plan_approval import (
    PlanApprovalController,
    PlanApprovalProviderAdapter,
    needs_approval,
    plan_approval_mode_enabled,
    reset_default_controller,
    should_force_plan_review,
)


# --------------------------------------------------------------------------
# Fixture
# --------------------------------------------------------------------------


_ENV_KEYS = [
    "JARVIS_PLAN_APPROVAL_MODE",
    "JARVIS_PLAN_APPROVAL_TIMEOUT_S",
]


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    reset_default_controller()
    yield
    reset_default_controller()


# --------------------------------------------------------------------------
# 1. Default-stays-false invariant (the KEY graduation choice)
# --------------------------------------------------------------------------


def test_slice5_default_stays_false():
    """Graduation deliberately keeps the default off — turning on
    plan mode halts every op, which is an OPERATOR CHOICE not a
    default posture."""
    assert plan_approval_mode_enabled() is False


def test_slice5_explicit_true_enables(monkeypatch):
    """Explicit opt-in still works identically to pre-graduation."""
    monkeypatch.setenv("JARVIS_PLAN_APPROVAL_MODE", "true")
    assert plan_approval_mode_enabled() is True
    assert should_force_plan_review() is True


def test_slice5_explicit_false_disables(monkeypatch):
    monkeypatch.setenv("JARVIS_PLAN_APPROVAL_MODE", "false")
    assert plan_approval_mode_enabled() is False


def test_slice5_docstring_names_graduation():
    """Docstring bit-rot guard — the module's master-switch docstring
    must reference Slice 5 graduation so future readers know the
    default-false was a conscious choice, not an oversight."""
    doc = plan_approval_mode_enabled.__doc__ or ""
    assert "slice 5" in doc.lower()
    assert "2026-04-21" in doc
    assert "operator choice" in doc.lower()


# --------------------------------------------------------------------------
# 2. Authority invariants (§1 Boundary)
# --------------------------------------------------------------------------


def test_slice5_authority_invariant_plan_approval_module():
    """No gate-module imports in plan_approval.py."""
    src = Path(plan_approval.__file__).read_text()
    forbidden = [
        "from backend.core.ouroboros.governance.orchestrator",
        "from backend.core.ouroboros.governance.tool_executor",
        "from backend.core.ouroboros.governance.iron_gate",
        "from backend.core.ouroboros.governance.risk_tier_floor",
        "from backend.core.ouroboros.governance.semantic_guardian",
        "from backend.core.ouroboros.governance.semantic_firewall",
        "from backend.core.ouroboros.governance.policy_engine",
    ]
    for f in forbidden:
        assert f not in src, "plan_approval imports " + f


def test_slice5_authority_invariant_plan_approval_repl_module():
    """No gate-module imports in plan_approval_repl.py either."""
    src = Path(plan_approval_repl.__file__).read_text()
    forbidden = [
        "from backend.core.ouroboros.governance.orchestrator",
        "from backend.core.ouroboros.governance.tool_executor",
        "from backend.core.ouroboros.governance.iron_gate",
        "from backend.core.ouroboros.governance.risk_tier_floor",
        "from backend.core.ouroboros.governance.semantic_guardian",
        "from backend.core.ouroboros.governance.semantic_firewall",
        "from backend.core.ouroboros.governance.policy_engine",
    ]
    for f in forbidden:
        assert f not in src, "plan_approval_repl imports " + f


# --------------------------------------------------------------------------
# 3. ApprovalProvider adapter surface pinned
# --------------------------------------------------------------------------


def test_slice5_adapter_implements_full_surface():
    """The ApprovalProvider-compatible methods MUST remain available
    — orchestrator's existing plan-approval wiring depends on them."""
    adapter = PlanApprovalProviderAdapter(controller=PlanApprovalController())
    assert callable(getattr(adapter, "request_plan", None))
    assert callable(getattr(adapter, "approve", None))
    assert callable(getattr(adapter, "reject", None))
    assert callable(getattr(adapter, "await_decision", None))
    assert callable(getattr(adapter, "is_plan_request", None))


# --------------------------------------------------------------------------
# 4. Event-type vocabulary pinned
# --------------------------------------------------------------------------


def test_slice5_sse_event_types_pinned():
    """All four plan_* event types must remain in the SSE vocabulary
    so existing IDE clients that subscribe to them don't break."""
    expected = {
        ide_observability_stream.EVENT_TYPE_PLAN_PENDING: "plan_pending",
        ide_observability_stream.EVENT_TYPE_PLAN_APPROVED: "plan_approved",
        ide_observability_stream.EVENT_TYPE_PLAN_REJECTED: "plan_rejected",
        ide_observability_stream.EVENT_TYPE_PLAN_EXPIRED: "plan_expired",
    }
    for const, wire_value in expected.items():
        assert const == wire_value
    # All four are in the _VALID_EVENT_TYPES whitelist.
    valid = ide_observability_stream._VALID_EVENT_TYPES
    for wire_value in expected.values():
        assert wire_value in valid


# --------------------------------------------------------------------------
# 5. REPL command surface pinned
# --------------------------------------------------------------------------


def test_slice5_repl_subcommands_registered():
    """The six operator-facing /plan subcommands must all be
    registered in the dispatcher. Removing one would silently break
    the REPL UX."""
    expected = {"mode", "pending", "show", "approve", "reject", "history"}
    registered = set(plan_approval_repl._HANDLERS.keys())
    assert expected.issubset(registered), (
        "missing: " + str(expected - registered)
    )


# --------------------------------------------------------------------------
# 6. IDE GET routes pinned
# --------------------------------------------------------------------------


def test_slice5_ide_plan_routes_registered():
    """IDE observability must register /observability/plans and
    /observability/plans/{op_id} in its register_routes call."""
    from unittest.mock import MagicMock
    from backend.core.ouroboros.governance.ide_observability import (
        IDEObservabilityRouter,
    )
    app = MagicMock()
    app.router = MagicMock()
    app.router.add_get = MagicMock()
    IDEObservabilityRouter().register_routes(app)
    paths = [c.args[0] for c in app.router.add_get.call_args_list]
    assert "/observability/plans" in paths
    assert "/observability/plans/{op_id}" in paths


# --------------------------------------------------------------------------
# 7. Full revert matrix (explicit off / explicit on / unset)
# --------------------------------------------------------------------------


def test_slice5_full_revert_matrix(monkeypatch):
    # Unset (default) → disabled.
    monkeypatch.delenv("JARVIS_PLAN_APPROVAL_MODE", raising=False)
    assert plan_approval_mode_enabled() is False
    # Explicit true → enabled.
    monkeypatch.setenv("JARVIS_PLAN_APPROVAL_MODE", "true")
    assert plan_approval_mode_enabled() is True
    # Explicit false → disabled.
    monkeypatch.setenv("JARVIS_PLAN_APPROVAL_MODE", "false")
    assert plan_approval_mode_enabled() is False
    # needs_approval mirrors.
    assert needs_approval() is False


def test_slice5_per_op_override_still_wins(monkeypatch):
    """Per-op override via ctx.plan_approval_override continues to
    work post-graduation (forces true / false regardless of env)."""
    monkeypatch.setenv("JARVIS_PLAN_APPROVAL_MODE", "false")
    from types import SimpleNamespace
    ctx_force_on = SimpleNamespace(plan_approval_override=True)
    assert needs_approval(ctx_force_on) is True
    monkeypatch.setenv("JARVIS_PLAN_APPROVAL_MODE", "true")
    ctx_force_off = SimpleNamespace(plan_approval_override=False)
    assert needs_approval(ctx_force_off) is False
