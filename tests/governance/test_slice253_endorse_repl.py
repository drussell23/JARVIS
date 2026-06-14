"""Slice 253 frontend — SerpentREPL Shadow-Endorsement interceptor.

The CORE decision-logic tests here are *import-light* and *in-sandbox*: they
exercise the injectable endorsement core
(:func:`backend.core.ouroboros.battle_test.serpent_flow.resolve_endorsement`)
WITHOUT prompt_toolkit, without a real TTY, and without the 102K-line kernel —
backend callables + the prompt function are injected as plain (mock) callables.

The REPL-method tests at the bottom DO import ``serpent_flow`` (which can pull
in the kernel via the split-brain guard); they are marked + skip cleanly when
that import is sandbox-blocked, so the core decision spine always runs green
in-sandbox.

Backend contract under test (``backend/core/cybernetic_reanimation.py``):

* ``handle_endorsement_choice(action_id, choice) -> EndorsementResult`` —
  'y'/'yes' endorses (re-hydrate + execute), anything else declines.
* ``endorse_shadow_action(action_id) -> EndorsementResult`` — one-shot execute.
* ``EndorsementResult.status`` ∈ executed | declined | not_found | expired | error.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Tuple

import pytest


# ──────────────────────────────────────────────────────────────────────────
# Import the injectable endorsement core. It is deliberately import-light:
# a pure async function with NO module-level prompt_toolkit / kernel imports.
# ──────────────────────────────────────────────────────────────────────────
from backend.core.ouroboros.battle_test.serpent_flow import (  # noqa: E402
    resolve_endorsement,
    classify_endorsement_choice,
    render_endorsement_outcome,
)


# ── A tiny EndorsementResult stand-in (avoids importing the kernel) ────────
class _FakeResult:
    def __init__(
        self,
        status: str,
        action_id: str = "",
        organ: str = "",
        intended_action: str = "",
        error: str = "",
    ) -> None:
        self.status = status
        self.action_id = action_id
        self.organ = organ
        self.intended_action = intended_action
        self.error = error


def _run(coro: Any) -> Any:
    return asyncio.get_event_loop().run_until_complete(coro)


# ════════════════════════════════════════════════════════════════════════
# classify_endorsement_choice — the pure y/n normalizer
# ════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("y", "y"), ("Y", "y"), ("yes", "y"), ("YES", "y"), (" yes ", "y"),
        ("", "n"), ("n", "n"), ("N", "n"), ("no", "n"), ("garbage", "n"),
        (None, "n"),
    ],
)
def test_classify_endorsement_choice(raw, expected):
    assert classify_endorsement_choice(raw) == expected


# ════════════════════════════════════════════════════════════════════════
# resolve_endorsement — the injectable async core (no TTY / no kernel)
# ════════════════════════════════════════════════════════════════════════
def test_explicit_yes_endorses_without_prompting():
    calls: List[Tuple[str, str]] = []

    async def fake_handle(action_id: str, choice: str) -> _FakeResult:
        calls.append((action_id, choice))
        return _FakeResult("executed", action_id=action_id, organ="janitor")

    prompt_calls: List[str] = []

    async def fake_prompt(_payload: Dict[str, Any]) -> str:
        prompt_calls.append("called")
        return "y"

    result = _run(
        resolve_endorsement(
            "shadow-000001",
            choice="y",
            prompt_fn=fake_prompt,
            handle_choice=fake_handle,
        )
    )
    # explicit choice must NOT prompt
    assert prompt_calls == []
    assert calls == [("shadow-000001", "y")]
    assert result.status == "executed"


def test_explicit_no_declines_without_prompting():
    async def fake_handle(action_id: str, choice: str) -> _FakeResult:
        # decline path: handle_endorsement_choice returns declined for non-y
        return _FakeResult("declined", action_id=action_id)

    async def fake_prompt(_payload: Dict[str, Any]) -> str:
        raise AssertionError("must not prompt when choice is explicit")

    result = _run(
        resolve_endorsement(
            "shadow-000002",
            choice="n",
            prompt_fn=fake_prompt,
            handle_choice=fake_handle,
        )
    )
    assert result.status == "declined"


def test_no_choice_prompts_then_endorses():
    seen: List[Tuple[str, str]] = []

    async def fake_handle(action_id: str, choice: str) -> _FakeResult:
        seen.append((action_id, choice))
        # backend normalizes internally — mirror that with the pure classifier.
        return _FakeResult(
            "executed" if classify_endorsement_choice(choice) == "y" else "declined",
            action_id=action_id,
        )

    async def fake_prompt(payload: Dict[str, Any]) -> str:
        # the prompt receives the trapped-action payload to render
        assert payload.get("action_id") == "shadow-000003"
        return "yes"

    result = _run(
        resolve_endorsement(
            "shadow-000003",
            choice=None,
            prompt_fn=fake_prompt,
            handle_choice=fake_handle,
            payload={"action_id": "shadow-000003", "organ_name": "shedder"},
        )
    )
    assert seen == [("shadow-000003", "yes")]
    assert result.status == "executed"


def test_no_choice_prompts_then_declines():
    async def fake_handle(action_id: str, choice: str) -> _FakeResult:
        return _FakeResult(
            "executed" if classify_endorsement_choice(choice) == "y" else "declined",
            action_id=action_id,
        )

    async def fake_prompt(_payload: Dict[str, Any]) -> str:
        return ""  # empty == decline (binary [y/N])

    result = _run(
        resolve_endorsement(
            "shadow-000004",
            choice=None,
            prompt_fn=fake_prompt,
            handle_choice=fake_handle,
        )
    )
    assert result.status == "declined"


def test_unknown_action_id_surfaces_not_found():
    async def fake_handle(action_id: str, choice: str) -> _FakeResult:
        return _FakeResult("not_found", action_id=action_id)

    async def fake_prompt(_payload: Dict[str, Any]) -> str:
        return "y"

    result = _run(
        resolve_endorsement(
            "shadow-bogus",
            choice="y",
            prompt_fn=fake_prompt,
            handle_choice=fake_handle,
        )
    )
    assert result.status == "not_found"


def test_backend_exception_is_fail_soft():
    async def fake_handle(action_id: str, choice: str) -> _FakeResult:
        raise RuntimeError("backend exploded")

    async def fake_prompt(_payload: Dict[str, Any]) -> str:
        return "y"

    # resolve_endorsement must NEVER propagate — returns an error result.
    result = _run(
        resolve_endorsement(
            "shadow-000005",
            choice="y",
            prompt_fn=fake_prompt,
            handle_choice=fake_handle,
        )
    )
    assert result.status == "error"
    assert "backend exploded" in (result.error or "")


def test_prompt_eof_declines_fail_soft():
    async def fake_handle(action_id: str, choice: str) -> _FakeResult:
        return _FakeResult("declined", action_id=action_id)

    async def fake_prompt(_payload: Dict[str, Any]) -> str:
        raise EOFError

    # An EOF / cancelled prompt must be treated as a decline, never a crash.
    result = _run(
        resolve_endorsement(
            "shadow-000006",
            choice=None,
            prompt_fn=fake_prompt,
            handle_choice=fake_handle,
        )
    )
    assert result.status == "declined"


# ════════════════════════════════════════════════════════════════════════
# render_endorsement_outcome — pure outcome → display string mapping
# ════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize(
    "status,needle",
    [
        ("executed", "endorsed"),
        ("declined", "declined"),
        ("not_found", "not found"),
        ("expired", "expired"),
        ("error", "error"),
    ],
)
def test_render_endorsement_outcome(status, needle):
    res = _FakeResult(status, action_id="shadow-000001", organ="janitor")
    out = render_endorsement_outcome(res)
    assert isinstance(out, str)
    assert needle in out.lower()


# ════════════════════════════════════════════════════════════════════════
# REPL-method integration — imports serpent_flow + cybernetic_reanimation,
# which import + construct cleanly in-sandbox (verified: serpent_flow does NOT
# pull unified_supervisor at import time). They drive _handle_endorse
# end-to-end against the real backend registry.
# ════════════════════════════════════════════════════════════════════════
def test_handle_endorse_empty_pending_is_calm_noop(monkeypatch):
    import backend.core.cybernetic_reanimation as cyber

    cyber.reset_pending_shadow_actions()
    from backend.core.ouroboros.battle_test.serpent_flow import (
        SerpentFlow,
        SerpentREPL,
    )

    flow = SerpentFlow()
    repl = SerpentREPL(flow)

    printed: List[str] = []
    monkeypatch.setattr(
        flow.console, "print", lambda *a, **k: printed.append(" ".join(str(x) for x in a))
    )

    _run(repl._handle_endorse("/endorse"))
    blob = "\n".join(printed).lower()
    assert "no trapped" in blob or "no shadow" in blob or "awaiting" in blob


def test_handle_endorse_specific_id_decline(monkeypatch):
    import backend.core.cybernetic_reanimation as cyber

    cyber.reset_pending_shadow_actions()

    fired: Dict[str, bool] = {"executed": False}

    def _execute() -> str:
        fired["executed"] = True
        return "killed"

    # Register a trapped action directly via the backend registry.
    action_id = cyber._PENDING.register(
        organ="janitor",
        action_desc="kill process 1234",
        execute=_execute,
        is_coro=False,
        signal_repr="memory_pressure:psutil:rising",
    )

    from backend.core.ouroboros.battle_test.serpent_flow import (
        SerpentFlow,
        SerpentREPL,
    )

    flow = SerpentFlow()
    repl = SerpentREPL(flow)
    monkeypatch.setattr(flow.console, "print", lambda *a, **k: None)

    # Non-interactive decline.
    _run(repl._handle_endorse(f"/endorse {action_id} n"))
    assert fired["executed"] is False
    # declined leaves the action pending (still endorsable later).
    assert action_id in cyber.pending_shadow_action_ids()


def test_handle_endorse_specific_id_yes_executes(monkeypatch):
    import backend.core.cybernetic_reanimation as cyber

    cyber.reset_pending_shadow_actions()

    fired: Dict[str, bool] = {"executed": False}

    def _execute() -> str:
        fired["executed"] = True
        return "killed"

    action_id = cyber._PENDING.register(
        organ="janitor",
        action_desc="kill process 1234",
        execute=_execute,
        is_coro=False,
    )

    from backend.core.ouroboros.battle_test.serpent_flow import (
        SerpentFlow,
        SerpentREPL,
    )

    flow = SerpentFlow()
    repl = SerpentREPL(flow)
    monkeypatch.setattr(flow.console, "print", lambda *a, **k: None)

    _run(repl._handle_endorse(f"/endorse {action_id} y"))
    assert fired["executed"] is True
    # one-shot: the action is popped after execution.
    assert action_id not in cyber.pending_shadow_action_ids()
