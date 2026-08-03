"""Four declared tiers must produce four behaviours.

`capability_registry` declares SAFE_AUTO / NOTIFY_APPLY / APPROVAL_REQUIRED /
BLOCKED, mirroring `risk_engine.RiskTier`. The router asked exactly one
question — `iron_gate_required`, which is False only for SAFE_AUTO — so
NOTIFY_APPLY and APPROVAL_REQUIRED were byte-identical, and BLOCKED was
presented to the operator as a prompt they could approve.

A word that cannot change what happens is not a word; worse, it is a word an
author will reach for believing it means something.
"""
from __future__ import annotations

import pytest

from backend.system_control.capability_registry import (
    CapabilityDef, Tier,
)
from backend.system_control.capability_router import (
    CapabilityRouter, Outcome,
)


def _cap(name, tier, **kw):
    return CapabilityDef(name=name, description=f"{name} does a thing",
                         tier=tier, **kw)


class _Target:
    def __init__(self):
        self.ran = []

    async def safe(self):
        self.ran.append("safe")
        return (True, "read something")

    async def notify(self):
        self.ran.append("notify")
        return (True, "🔒 Locking the screen now, Derek.")

    async def approve(self):
        self.ran.append("approve")
        return (True, "did the guarded thing")

    async def forbidden(self):
        self.ran.append("forbidden")
        return (True, "should never happen")


class _Registry:
    def __init__(self, defs):
        self._d = {d.name: d for d in defs}

    def get(self, name):
        return self._d.get(name)


class _Provider:
    """Records that somebody was ASKED. That is the whole assertion."""

    def __init__(self):
        self.asked = []

    async def request(self, ctx):
        self.asked.append(getattr(ctx, "capability", ""))
        return "rid-1"


@pytest.fixture()
def rig(monkeypatch):
    monkeypatch.delenv("JARVIS_MIN_RISK_TIER", raising=False)
    monkeypatch.delenv("JARVIS_PARANOIA_MODE", raising=False)
    monkeypatch.delenv("JARVIS_AUTO_APPLY_QUIET_HOURS", raising=False)
    target, provider = _Target(), _Provider()
    reg = _Registry([
        _cap("safe", Tier.SAFE_AUTO.value),
        _cap("notify", Tier.NOTIFY_APPLY.value),
        _cap("approve", Tier.APPROVAL_REQUIRED.value),
        _cap("forbidden", Tier.BLOCKED.value),
    ])
    return (CapabilityRouter(registry=reg, provider=provider, target=target),
            target, provider)


# ── The four behaviours ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_safe_auto_just_runs(rig):
    router, target, provider = rig
    out = await router.route("safe")
    assert out.outcome == Outcome.EXECUTED.value
    assert target.ran == ["safe"] and provider.asked == []
    assert out.notified is False


@pytest.mark.asyncio
async def test_notify_apply_runs_without_asking(rig):
    """The behaviour that did not exist. Before this, declaring NOTIFY_APPLY
    got you APPROVAL_REQUIRED and a Touch ID prompt."""
    router, target, provider = rig
    out = await router.route("notify")
    assert out.outcome == Outcome.EXECUTED.value
    assert target.ran == ["notify"]
    assert provider.asked == [], "NOTIFY_APPLY must not ask anybody"


@pytest.mark.asyncio
async def test_notify_apply_is_never_silent(rig):
    """Running without asking is only acceptable because it is announced.
    `notified` is what every narrating surface reads."""
    router, _, _ = rig
    out = await router.route("notify")
    assert out.notified is True
    assert out.tier == Tier.NOTIFY_APPLY.value
    assert "WITHOUT CONSENT" in out.context_note


@pytest.mark.asyncio
async def test_approval_required_still_suspends(rig):
    router, target, provider = rig
    out = await router.route("approve")
    assert out.outcome == Outcome.SUSPENDED.value
    assert provider.asked == ["approve"]
    assert target.ran == [], "it must not run before the human answers"


@pytest.mark.asyncio
async def test_blocked_is_refused_without_asking_anyone(rig):
    """A prompt is an invitation to approve. BLOCKED is not a question.

    Before this, BLOCKED fell into the consent path — so the one tier that
    means 'never' was rendered to the operator as something they could say
    yes to.
    """
    router, target, provider = rig
    out = await router.route("forbidden")
    assert out.outcome == Outcome.DENIED.value
    assert provider.asked == [], "BLOCKED must never reach an operator"
    assert target.ran == []
    assert "BLOCKED" in out.detail


# ── The operator's standing floor ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_paranoia_mode_takes_notify_apply_back(rig, monkeypatch):
    """The counterweight to NOTIFY_APPLY actually applying.

    The operator can revoke it globally — at 2am, before a demo — without
    editing a decorator, using the floor module that already existed.
    """
    router, target, provider = rig
    monkeypatch.setenv("JARVIS_MIN_RISK_TIER", "approval_required")
    out = await router.route("notify")
    assert out.outcome == Outcome.SUSPENDED.value
    assert provider.asked == ["notify"] and target.ran == []
    assert "risk floor" in out.detail


@pytest.mark.asyncio
async def test_the_floor_only_ever_raises(rig, monkeypatch):
    """A floor BELOW the declared tier changes nothing — it is a floor, not a
    ceiling, and it must never make a capability more permissive."""
    router, _, provider = rig
    monkeypatch.setenv("JARVIS_MIN_RISK_TIER", "safe_auto")
    assert (await router.route("approve")).outcome == Outcome.SUSPENDED.value
    assert provider.asked == ["approve"]


@pytest.mark.asyncio
async def test_a_broken_floor_falls_back_to_the_declared_tier(rig, monkeypatch):
    """Fail toward what the method's author chose. A floor subsystem that
    cannot answer must not be able to make anything MORE permissive."""
    import backend.system_control.capability_router as CR

    def _boom(*a, **k):
        raise RuntimeError("floor is down")

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.risk_tier_floor.apply_floor_to_name",
        _boom)
    router, target, provider = rig
    assert (await router.route("approve")).outcome == Outcome.SUSPENDED.value
    assert (await router.route("notify")).outcome == Outcome.EXECUTED.value


@pytest.mark.asyncio
async def test_the_floor_can_be_rolled_back(rig, monkeypatch):
    monkeypatch.setenv("JARVIS_CAPABILITY_RISK_FLOOR_ENABLED", "false")
    monkeypatch.setenv("JARVIS_MIN_RISK_TIER", "approval_required")
    router, target, _ = rig
    assert (await router.route("notify")).outcome == Outcome.EXECUTED.value


# ── The registry's own vocabulary ───────────────────────────────────────────

@pytest.mark.parametrize("tier,governed,consent,notifies,blocked", [
    (Tier.SAFE_AUTO.value, False, False, False, False),
    (Tier.NOTIFY_APPLY.value, True, False, True, False),
    (Tier.APPROVAL_REQUIRED.value, True, True, False, False),
    (Tier.BLOCKED.value, True, True, False, True),
])
def test_the_four_tiers_answer_four_different_ways(
        tier, governed, consent, notifies, blocked):
    d = _cap("x", tier)
    assert d.iron_gate_required is governed
    assert d.requires_consent is consent
    assert d.notifies is notifies
    assert d.blocked is blocked


def test_governed_and_needs_consent_are_not_the_same_question():
    """The conflation itself, pinned. Both were one property for as long as
    there was only one of them."""
    n = _cap("x", Tier.NOTIFY_APPLY.value)
    assert n.iron_gate_required and not n.requires_consent


def test_an_unclassified_capability_still_needs_consent():
    """The registry's inversion survives the split: silence means
    APPROVAL_REQUIRED, which means a human is asked."""
    assert CapabilityDef(name="mystery", description="").requires_consent


# ── The live capability ─────────────────────────────────────────────────────

def test_lock_screen_is_instant_and_unlock_screen_is_not():
    """The operator's deliberate choice, pinned so a refactor cannot quietly
    reverse it — in either direction."""
    from backend.system_control.capability_registry import (
        get_capability_registry,
    )
    reg = get_capability_registry()
    lock = reg.get("lock_screen")
    if lock is None:
        pytest.skip("controller unimportable in this environment")
    assert lock.tier == Tier.NOTIFY_APPLY.value
    assert lock.requires_consent is False, "locking must not wait for Touch ID"
    assert lock.notifies is True, "and must never happen silently"
    # Unlocking is the opposite decision and must stay that way.
    assert reg.get("unlock_screen").requires_consent is True
