"""Slice 253 — end-to-end endorsement integration proof.

Drives the SerpentREPL injectable endorsement core (`resolve_endorsement`)
against the REAL merged backend (`backend.core.cybernetic_reanimation`, which is
standalone-importable — it never imports the split-brain-guarded
`unified_supervisor`). Proves the CLI interceptor triggers the core's in-process
re-hydration (closure execution) on endorse, and is inert on decline.
"""
import pytest

import backend.core.cybernetic_reanimation as cr
from backend.core.ouroboros.battle_test.serpent_flow import (
    classify_endorsement_choice,
    resolve_endorsement,
)


def _register(execute, *, organ="SelfHealingOrchestrator", action="restart jarvis-prime",
              signal="component_degraded"):
    return cr._PENDING.register(
        organ=organ, action_desc=action, execute=execute, is_coro=False,
        signal_repr=signal,
    )


@pytest.mark.asyncio
async def test_endorse_executes_the_inprocess_closure():
    cr.reset_pending_shadow_actions()
    ran = {}
    aid = _register(lambda: ran.setdefault("a", True))
    payload = {"action_id": aid, "organ_name": "SelfHealingOrchestrator",
               "intended_action": "restart jarvis-prime",
               "triggering_signal": "component_degraded"}
    result = await resolve_endorsement(
        aid, prompt_fn=lambda _p: "y",
        handle_choice=cr.handle_endorsement_choice, payload=payload,
    )
    assert result.status == "executed"
    assert ran.get("a") is True                       # the closure actually ran
    assert cr.pending_shadow_action_count() == 0       # one-shot: entry popped


@pytest.mark.asyncio
async def test_decline_leaves_the_closure_unrun():
    cr.reset_pending_shadow_actions()
    ran = {}
    aid = _register(lambda: ran.setdefault("a", True), organ="LoadSheddingController",
                    action="shed request", signal="resource_pressure")
    payload = {"action_id": aid, "organ_name": "LoadSheddingController",
               "intended_action": "shed request", "triggering_signal": "resource_pressure"}
    result = await resolve_endorsement(
        aid, prompt_fn=lambda _p: "n",
        handle_choice=cr.handle_endorsement_choice, payload=payload,
    )
    assert result.status == "declined"
    assert ran.get("a") is None                        # closure NOT executed


@pytest.mark.asyncio
async def test_headless_no_tty_defaults_to_decline():
    """A no-TTY prompt that returns empty MUST decline — never auto-endorse a
    trapped kill unattended (that would defeat the Shadow shield)."""
    cr.reset_pending_shadow_actions()
    ran = {}
    aid = _register(lambda: ran.setdefault("a", True))
    payload = {"action_id": aid, "organ_name": "SelfHealingOrchestrator",
               "intended_action": "restart jarvis-prime",
               "triggering_signal": "component_degraded"}
    result = await resolve_endorsement(
        aid, prompt_fn=lambda _p: "",   # headless / empty input
        handle_choice=cr.handle_endorsement_choice, payload=payload,
    )
    assert result.status == "declined"
    assert ran.get("a") is None


def test_classify_choice_safe_default():
    assert classify_endorsement_choice("y") == "y"
    assert classify_endorsement_choice("Y") == "y"
    assert classify_endorsement_choice("yes") == "y"
    for raw in ("", "n", "no", "x", None):
        assert classify_endorsement_choice(raw) == "n"
