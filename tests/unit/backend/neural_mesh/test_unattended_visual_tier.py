"""An autonomous poll may not seize the operator's desktop — or their ears.

WHAT WAS MEASURED
-----------------
The 60-second email-triage poll, with its Google API tier dead on this
machine, escalated to the Computer Use visual tier on every tick:
`google_workspace_agent` foregrounded Chrome via Yabai (LAUNCHED_APP — it
OPENS one when none exists), switched the operator's macOS Space (4 -> 5,
84 times in one boot), screenshotted Gmail, and spoke "Checking your inbox
now." into an empty room. The operator experienced it as "a Chrome window
keeps opening".

The provenance was PRESENT the entire time: agent_runtime wraps the cycle
in `execution_budget(request_kind=RequestKind.AUTONOMOUS)` (v284.0, built
exactly so payload flags need not be trusted). No seam consulted it. The
fix is one canonical predicate — `execution_context.is_unattended_request`
— consulted at every door into the visual tier, before the cached fast
paths, plus one declared-consent key at the one caller that can run
UNSTAMPED (agent_runtime's ImportError degradation).
"""
from __future__ import annotations

import asyncio

import pytest

# The agent reads the SHORT module identity (`core.execution_context`) —
# matching its stamper, agent_runtime. These tests stamp through the same
# identity the production pairing uses; the dual-root hazard is pinned
# explicitly below.
import sys
from pathlib import Path

_BACKEND = str(Path(__file__).resolve().parents[4] / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.execution_context import (  # noqa: E402
    UNATTENDED_REQUEST_KINDS,
    RequestKind,
    execution_budget,
    is_unattended_request,
)
from backend.neural_mesh.agents.google_workspace_agent import (  # noqa: E402
    UnifiedWorkspaceExecutor,
)


# ---------------------------------------------------------------------------
# The canonical predicate
# ---------------------------------------------------------------------------

class TestTheAttendancePredicate:

    def test_no_context_reads_as_attended(self):
        """Load-bearing polarity: interactive command paths mostly run
        OUTSIDE any budget. Fail-closed-on-None would break every human
        visual flow to guard callers that already carry the stamp."""
        assert is_unattended_request() is False

    @pytest.mark.asyncio
    async def test_the_autonomous_stamp_reads_as_unattended(self):
        """The exact stamp the email-triage poll carries."""
        async with execution_budget(
            owner="test", timeout=5.0, request_kind=RequestKind.AUTONOMOUS,
        ):
            assert is_unattended_request() is True
        assert is_unattended_request() is False, "stamp leaked past the budget"

    @pytest.mark.asyncio
    async def test_runtime_reads_as_attended(self):
        """RUNTIME is what interactive paths run under when they DO carry a
        budget — refusing it would degrade the human's flow."""
        async with execution_budget(
            owner="test", timeout=5.0, request_kind=RequestKind.RUNTIME,
        ):
            assert is_unattended_request() is False

    def test_every_request_kind_has_a_declared_attendance(self):
        """A new RequestKind member must be classified in the same diff that
        creates it — this is the assertion that forces the conversation."""
        for kind in RequestKind:
            assert (kind in UNATTENDED_REQUEST_KINDS) == (
                kind is not RequestKind.RUNTIME
            ), (
                f"{kind} attendance changed or was never declared — decide "
                f"it in UNATTENDED_REQUEST_KINDS, next to the enum"
            )

    def test_the_dual_root_pairing_is_pinned(self):
        """`core.execution_context` and `backend.core.execution_context` are
        TWO module instances with TWO contextvars. The stamper this fix
        pairs with (agent_runtime) and the readers (workspace agent) both
        use the SHORT identity; this test documents the hazard so a future
        'cleanup' normalizing the agent's import to the long form doesn't
        silently disconnect it from its stamper."""
        import core.execution_context as short
        import backend.core.execution_context as long_form
        # Both exist and both expose the predicate...
        assert hasattr(short, "is_unattended_request")
        assert hasattr(long_form, "is_unattended_request")
        # ...and the agent's source binds the short identity, like its stamper.
        import inspect
        import backend.neural_mesh.agents.google_workspace_agent as gwa
        src = inspect.getsource(gwa)
        assert "from core.execution_context import is_unattended_request" in src


# ---------------------------------------------------------------------------
# The doors
# ---------------------------------------------------------------------------

def _executor() -> UnifiedWorkspaceExecutor:
    ex = UnifiedWorkspaceExecutor.__new__(UnifiedWorkspaceExecutor)
    ex._spatial_awareness = None
    ex._computer_use = None
    ex._visual_refusals_logged = set()
    ex._lock = asyncio.Lock()
    return ex


class TestTheVisualTierRefusesUnattendedRequests:

    @pytest.mark.asyncio
    async def test_visual_tooling_door_refuses_before_the_cache(self):
        """The cache check must come SECOND: an interactive command warming
        `_computer_use` must not open the door for the next poll."""
        ex = _executor()
        ex._computer_use = object()      # cache warm — the dangerous state
        async with execution_budget(
            owner="test", timeout=5.0, request_kind=RequestKind.AUTONOMOUS,
        ):
            assert await ex._ensure_visual_tooling() is False

    @pytest.mark.asyncio
    async def test_spatial_awareness_door_refuses_before_the_cache(self):
        ex = _executor()
        ex._spatial_awareness = {"switch_to_app": object()}
        async with execution_budget(
            owner="test", timeout=5.0, request_kind=RequestKind.AUTONOMOUS,
        ):
            assert await ex._ensure_spatial_awareness() is False

    @pytest.mark.asyncio
    async def test_the_switch_seam_backstop_never_reaches_yabai(self):
        """The one seam every visual path crosses. Even a caller that
        bypasses both _ensure_* doors cannot move the operator's Space."""
        ex = _executor()
        called = {"n": 0}

        async def _switch(app, narrate=True):
            called["n"] += 1

        ex._spatial_awareness = {"switch_to_app": _switch}
        async with execution_budget(
            owner="test", timeout=5.0, request_kind=RequestKind.AUTONOMOUS,
        ):
            ok = await ex._switch_to_app_with_spatial_awareness("Google Chrome")
        assert ok is False
        assert called["n"] == 0, "an unattended request reached the desktop"

    @pytest.mark.asyncio
    async def test_attended_requests_pass_the_doors_unchanged(self):
        """The gate must be invisible to the human path — cache-warm tooling
        stays available with no context and under RUNTIME."""
        ex = _executor()
        ex._computer_use = object()
        assert await ex._ensure_visual_tooling() is True
        async with execution_budget(
            owner="test", timeout=5.0, request_kind=RequestKind.RUNTIME,
        ):
            assert await ex._ensure_visual_tooling() is True

    @pytest.mark.asyncio
    async def test_refusal_logs_INFO_once_per_door_then_debug(self, caplog):
        """This fires on every 60s poll tick; only the first occurrence per
        door is a diagnostic."""
        import logging
        ex = _executor()
        ex._computer_use = object()
        with caplog.at_level(logging.INFO):
            async with execution_budget(
                owner="test", timeout=5.0,
                request_kind=RequestKind.AUTONOMOUS,
            ):
                await ex._ensure_visual_tooling()
                await ex._ensure_visual_tooling()
        infos = [r for r in caplog.records
                 if r.levelno == logging.INFO and "REFUSED" in r.getMessage()]
        assert len(infos) == 1, f"expected exactly one INFO refusal, got {len(infos)}"


# ---------------------------------------------------------------------------
# The caller that can run unstamped
# ---------------------------------------------------------------------------

class TestTheTriagePollDeclaresItself:

    def test_fetch_unread_payload_carries_explicit_no_visual_consent(self):
        """agent_runtime degrades to running the cycle UNSTAMPED when the
        execution_context import fails — and an unstamped context reads as
        attended. The runner is the one caller that knows structurally that
        no human asked, so it declares it in the payload, covering the
        degraded path under ANY module identity."""
        import inspect
        from backend.autonomy.email_triage import runner as triage_runner
        src = inspect.getsource(triage_runner.EmailTriageRunner._fetch_unread)
        assert '"allow_visual_fallback": False' in src, (
            "the poll's payload no longer refuses the visual tier — the "
            "ImportError degradation path is unguarded again")
