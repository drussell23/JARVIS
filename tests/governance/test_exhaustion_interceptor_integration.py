"""Phase 3.3 Task 3 — integration matrix for exhaustion interceptor.

Covers end-to-end interceptor behaviour: topology-aware pruning, flag gating,
and zero-duplication guard (no new recovery loop). Stubs are used because
constructing a full CandidateGenerator requires live provider credentials.
"""
from __future__ import annotations

import dataclasses
import pytest


@dataclasses.dataclass
class _Ctx:
    op_id: str = "opX"
    target_files: tuple = ("hub.py", "leaf1.py", "leaf2.py", "leaf3.py")


class _Resp:
    def __init__(self, c: str) -> None:
        self.content = c


class _Jprime:
    def __init__(self) -> None:
        self.seen: tuple = ()
        self.calls: int = 0

    async def health_probe(self) -> bool:
        return True

    async def generate(self, context: object, deadline: object) -> _Resp:
        self.calls += 1
        self.seen = tuple(getattr(context, "target_files", ()))
        return _Resp("LOCAL")


class _Backend:
    """Minimal graph backend stub: hub.py has high degree, leaves peripheral."""

    _map: dict = {
        "hub.py": ["h"],
        "leaf1.py": ["l1"],
        "leaf2.py": ["l2"],
        "leaf3.py": ["l3"],
    }

    def nodes_in_file(self, f: str) -> list:
        return self._map.get(f, [])

    def successor_keys(self, k: str) -> list:
        return ["x"] * 9 if k == "h" else []

    def predecessor_keys(self, k: str) -> list:
        return ["y"] * 9 if k == "h" else []


@pytest.mark.asyncio
async def test_inflight_exhaustion_absorbed_with_topological_prune(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Induced all_providers_exhausted -> local absorbs; massive context pruned by
    topology weight (hub kept, peripheral leaves discarded); loop not crashed."""
    monkeypatch.setenv("JARVIS_JPRIME_LASTRESORT_ENABLED", "true")
    from backend.core.ouroboros.governance.exhaustion_interceptor import (
        should_intercept,
        execute_local_last_resort,
    )

    jp = _Jprime()
    exc = RuntimeError("all_providers_exhausted:fallback_failed")
    assert should_intercept(exc, jprime=jp) is True

    big_tokens = {
        "hub.py": 700,
        "leaf1.py": 700,
        "leaf2.py": 700,
        "leaf3.py": 700,
    }
    res = await execute_local_last_resort(
        jprime=jp,
        context=_Ctx(),
        deadline=None,
        graph_backend=_Backend(),
        broker=None,
        file_tokens=big_tokens,
        ceiling_tokens=1000,
        original_exc=exc,
    )
    assert res.content == "LOCAL"          # loop absorbed, not crashed
    assert "hub.py" in jp.seen            # central node retained
    assert len(jp.seen) < 4              # peripheral leaves pruned


@pytest.mark.asyncio
async def test_disabled_flag_does_not_intercept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the feature flag is off, should_intercept returns False."""
    monkeypatch.setenv("JARVIS_JPRIME_LASTRESORT_ENABLED", "false")
    from backend.core.ouroboros.governance.exhaustion_interceptor import should_intercept

    assert should_intercept(RuntimeError("all_providers_exhausted"), jprime=_Jprime()) is False


def test_no_new_recovery_loop_added() -> None:
    """Recovery stays with the EXISTING dw_transport_recovery / FSM -- the interceptor
    module must NOT spawn its own background probe loop (zero-duplication guard)."""
    import backend.core.ouroboros.governance.exhaustion_interceptor as ei

    src = open(ei.__file__).read()
    # No asyncio task-spawning (would create a hidden probe loop)
    assert "create_task" not in src
    assert "ensure_future" not in src
    # No infinite loop (would be a hidden polling loop)
    assert "while True" not in src
    # dw_transport_recovery may appear in the docstring as a reference to what we
    # are NOT duplicating -- that is intentional and safe. What must NOT appear is
    # an actual import or call to it. Guard: no 'import dw_transport_recovery' or
    # 'dw_transport_recovery(' (functional invocation).
    assert "import dw_transport_recovery" not in src
    assert "dw_transport_recovery(" not in src
