"""Acceptance tests for Phase 1 Step 3C — Orchestrator state hoist.

These tests lock down the contract described in
``_governance_state.OrchestratorState`` and the § 4 ``GovernanceStack``
bind contract: when ``JARVIS_UNQUARANTINE_ORCHESTRATOR=true``, the
governed orchestrator's reload-hostile roots (oracle lock, cost
governor, forward-progress detector, session lessons, convergence
counters, RSI trackers, hot reloader, seven attached refs) must
survive ``importlib.reload(orchestrator)`` because they are sourced
from a process-lifetime singleton instead of re-allocated in
``__init__``.

When the flag is off (default during rollout), each
``__init__`` mints a fresh :class:`OrchestratorState` so pre-hoist
behavior is preserved bit-for-bit. Both paths are exercised below.
"""

from __future__ import annotations

import importlib
from unittest.mock import MagicMock

import pytest

from backend.core.ouroboros.governance import _governance_state
from backend.core.ouroboros.governance.orchestrator import (
    GovernedOrchestrator,
    OrchestratorConfig,
)


def _make_orchestrator(tmp_path) -> GovernedOrchestrator:
    """Build a minimal orchestrator for state-identity tests.

    The tests here care only about ``__init__``-time state wiring, not
    actual pipeline behavior, so ``stack``/``generator``/``approval``
    are ``MagicMock`` placeholders. ``tmp_path`` supplies the
    ``project_root`` the hot reloader needs to arm.
    """
    cfg = OrchestratorConfig(project_root=tmp_path)
    return GovernedOrchestrator(
        stack=MagicMock(),
        generator=MagicMock(),
        approval_provider=MagicMock(),
        config=cfg,
    )


class TestOrchestratorUnquarantine:
    """Phase 1 Step 3C: state hoist behind ``JARVIS_UNQUARANTINE_ORCHESTRATOR``.

    Every test starts from a freshly-reset singleton so leaks from a
    sibling test case cannot mask a drift bug.
    """

    def _reset_singleton(self) -> None:
        """Clear ``_orchestrator_state`` + ``_bound_orchestrator``.

        ``reset_for_tests()`` is the authoritative test helper — it
        owns the full set of state roots and is extended whenever a
        new hoist phase lands, so this one call is sufficient.
        """
        _governance_state.reset_for_tests()

    def test_flag_off_mints_fresh_state_per_instance(self, monkeypatch, tmp_path):
        """Default path: two orchestrators get independent state roots.

        Asserts pre-hoist behavior is preserved when the
        un-quarantine flag is off — mutating counters or session
        lessons on one instance must not bleed into a sibling.
        """
        monkeypatch.delenv("JARVIS_UNQUARANTINE_ORCHESTRATOR", raising=False)
        self._reset_singleton()

        orch_a = _make_orchestrator(tmp_path)
        orch_b = _make_orchestrator(tmp_path)

        assert orch_a._state is not orch_b._state
        assert orch_a._state.counters is not orch_b._state.counters
        assert orch_a._state.oracle_update_lock is not orch_b._state.oracle_update_lock
        assert orch_a._state.cost_governor is not orch_b._state.cost_governor
        assert orch_a._state.forward_progress is not orch_b._state.forward_progress
        assert orch_a._state.session_lessons is not orch_b._state.session_lessons

        orch_a._ops_before_lesson = 7
        orch_a._session_lessons.append(("hint", "lesson-a"))
        assert orch_b._ops_before_lesson == 0
        assert orch_b._session_lessons == []

    def test_flag_on_shares_singleton_state(self, monkeypatch, tmp_path):
        """Un-quarantine path: two orchestrators share the singleton roots.

        Counter writes and session-lessons appends on one instance
        must be visible on the next ``__init__`` because both read
        from the same :class:`OrchestratorState` singleton.
        """
        monkeypatch.setenv("JARVIS_UNQUARANTINE_ORCHESTRATOR", "true")
        self._reset_singleton()

        orch_a = _make_orchestrator(tmp_path)
        orch_a._ops_before_lesson = 12
        orch_a._ops_after_lesson_success = 5
        orch_a._session_lessons.append(("hint", "keep-going"))

        orch_b = _make_orchestrator(tmp_path)

        assert orch_b._state is orch_a._state
        assert orch_b._state.counters is orch_a._state.counters
        assert orch_b._state.session_lessons is orch_a._state.session_lessons
        assert orch_b._ops_before_lesson == 12
        assert orch_b._ops_after_lesson_success == 5
        assert orch_b._session_lessons == [("hint", "keep-going")]

    def test_flag_on_attached_refs_survive_new_instance(
        self, monkeypatch, tmp_path,
    ):
        """§ 4 attached refs flow through the singleton.

        Calling ``set_reasoning_bridge`` / ``set_critique_engine`` /
        etc. on one instance must be visible on the next instance
        without re-running the harness wiring pass — that is the
        whole point of the § 4 "don't let harness attach rot" fix.
        """
        monkeypatch.setenv("JARVIS_UNQUARANTINE_ORCHESTRATOR", "true")
        self._reset_singleton()

        orch_a = _make_orchestrator(tmp_path)
        _bridge = MagicMock(name="bridge")
        _narrator = MagicMock(name="narrator")
        _engine = MagicMock(name="critique")
        orch_a.set_reasoning_bridge(_bridge)
        orch_a.set_reasoning_narrator(_narrator)
        orch_a.set_critique_engine(_engine)

        orch_b = _make_orchestrator(tmp_path)

        assert orch_b._reasoning_bridge is _bridge
        assert orch_b._reasoning_narrator is _narrator
        assert orch_b._critique_engine is _engine

    def test_flag_on_counter_increment_reaches_singleton(
        self, monkeypatch, tmp_path,
    ):
        """``self._ops_before_lesson += 1`` must mutate the singleton.

        Regression guard for the shadowing hazard: if the property/
        setter pair is missing, ``+=`` on the instance would plant a
        real instance attribute on the left-hand side and silently
        drift away from the ``OrchestratorState.counters`` container.
        """
        monkeypatch.setenv("JARVIS_UNQUARANTINE_ORCHESTRATOR", "true")
        self._reset_singleton()

        orch = _make_orchestrator(tmp_path)
        orch._ops_before_lesson += 1
        orch._ops_before_lesson += 1
        orch._ops_after_lesson += 5

        singleton = _governance_state.get_orchestrator_state()
        assert singleton.counters.ops_before_lesson == 2
        assert singleton.counters.ops_after_lesson == 5

    def test_flag_on_session_lessons_slice_rebind_persists(
        self, monkeypatch, tmp_path,
    ):
        """``self._session_lessons = self._session_lessons[-N:]`` must persist.

        The property/setter pair routes the rebind into
        ``self._state.session_lessons`` so a truncation on one
        instance is visible to the next. A plain instance attribute
        would re-point the current instance at a local list and the
        singleton would silently hold the old, untruncated container.
        """
        monkeypatch.setenv("JARVIS_UNQUARANTINE_ORCHESTRATOR", "true")
        self._reset_singleton()

        orch_a = _make_orchestrator(tmp_path)
        for i in range(25):
            orch_a._session_lessons.append(("hint", f"lesson-{i}"))
        # Truncate — this is the exact slice-rebind pattern the
        # orchestrator uses to enforce ``session_lessons_max``.
        orch_a._session_lessons = orch_a._session_lessons[-5:]

        orch_b = _make_orchestrator(tmp_path)
        assert len(orch_b._session_lessons) == 5
        assert orch_b._session_lessons[0][1] == "lesson-20"
        assert orch_b._session_lessons[-1][1] == "lesson-24"

    @pytest.mark.asyncio
    async def test_importlib_reload_preserves_state(self, monkeypatch, tmp_path):
        """Acceptance test for Phase 1 Step 3C.

        Mutate the state (counters, lessons, attached refs), reload
        ``orchestrator`` with ``importlib.reload()``, then build a
        fresh orchestrator. Because the un-quarantine flag routes
        every field through :class:`OrchestratorState`, the new
        instance must observe the prior counters, lessons, and refs.

        This is the exact failure mode the 3C hoist exists to prevent:
        a successful O+V self-modification of ``orchestrator.py``
        would ``importlib.reload`` the module, drop all of the
        __init__-allocated primitives, and silently reset the
        oracle lock / session lessons / hot-reload subscription.
        """
        monkeypatch.setenv("JARVIS_UNQUARANTINE_ORCHESTRATOR", "true")
        self._reset_singleton()

        from backend.core.ouroboros.governance import orchestrator as _orch_mod

        orch_before = _make_orchestrator(tmp_path)
        orch_before._ops_before_lesson = 9
        orch_before._ops_after_lesson_success = 3
        orch_before._session_lessons.append(("hint", "keep-going"))
        _bridge = MagicMock(name="bridge")
        orch_before.set_reasoning_bridge(_bridge)

        # Capture the actual state identity so we can confirm it's
        # the *same* object after reload, not a lookalike.
        _state_before = orch_before._state

        # The reload operation the whole hoist exists to support.
        reloaded = importlib.reload(_orch_mod)

        orch_after = reloaded.GovernedOrchestrator(
            stack=MagicMock(),
            generator=MagicMock(),
            approval_provider=MagicMock(),
            config=OrchestratorConfig(project_root=tmp_path),
        )

        assert orch_after._state is _state_before
        assert orch_after._ops_before_lesson == 9
        assert orch_after._ops_after_lesson_success == 3
        assert orch_after._session_lessons == [("hint", "keep-going")]
        assert orch_after._reasoning_bridge is _bridge

    def test_bind_contract_atomic_swap(self, monkeypatch, tmp_path):
        """§ 4 ``bind_orchestrator`` routes through ``_bound_orchestrator``.

        ``GovernanceStack.bind_orchestrator`` must update both the
        legacy dataclass slot AND the process-lifetime bind, and
        ``orchestrator_ref`` must read the latest bind on every
        access. Passing ``None`` clears both.
        """
        monkeypatch.setenv("JARVIS_UNQUARANTINE_ORCHESTRATOR", "true")
        self._reset_singleton()

        from backend.core.ouroboros.governance.integration import GovernanceStack

        # Minimal stack — we're exercising bind/ref, not can_write.
        stack = GovernanceStack.__new__(GovernanceStack)
        stack.orchestrator = None

        orch = _make_orchestrator(tmp_path)
        stack.bind_orchestrator(orch)

        assert stack.orchestrator is orch
        assert stack.orchestrator_ref is orch
        assert _governance_state.get_bound_orchestrator() is orch

        stack.bind_orchestrator(None)
        assert stack.orchestrator is None
        assert _governance_state.get_bound_orchestrator() is None
        # With the bind cleared, ``orchestrator_ref`` falls back to the
        # legacy dataclass slot (also None) — documenting the fallback
        # chain so a future refactor can't break it silently.
        assert stack.orchestrator_ref is None

    def test_bind_contract_survives_importlib_reload(
        self, monkeypatch, tmp_path,
    ):
        """The whole point of the bind contract.

        Bind an orchestrator, ``importlib.reload(orchestrator)``,
        build a new orchestrator, rebind, and assert the stack's
        ``orchestrator_ref`` routes to the new instance *without*
        losing the state singleton underneath.
        """
        monkeypatch.setenv("JARVIS_UNQUARANTINE_ORCHESTRATOR", "true")
        self._reset_singleton()

        from backend.core.ouroboros.governance import orchestrator as _orch_mod
        from backend.core.ouroboros.governance.integration import GovernanceStack

        stack = GovernanceStack.__new__(GovernanceStack)
        stack.orchestrator = None

        orch_before = _make_orchestrator(tmp_path)
        stack.bind_orchestrator(orch_before)
        _state_before = orch_before._state
        orch_before._ops_before_lesson = 4

        reloaded = importlib.reload(_orch_mod)
        orch_after = reloaded.GovernedOrchestrator(
            stack=MagicMock(),
            generator=MagicMock(),
            approval_provider=MagicMock(),
            config=OrchestratorConfig(project_root=tmp_path),
        )
        stack.bind_orchestrator(orch_after)

        assert stack.orchestrator_ref is orch_after
        assert stack.orchestrator_ref is not orch_before
        # State singleton still lives — new instance rebinds into the
        # same ``OrchestratorState`` so counters from the pre-reload
        # era are still readable.
        assert orch_after._state is _state_before
        assert orch_after._ops_before_lesson == 4
