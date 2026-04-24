"""Tests for F2 Slice 2 — UrgencyRouter envelope_routing_override consumption.

Scope: `memory/project_followup_f2_backlog_urgency_hint_schema.md` Slice 2.
Pins the behavioral contract for the NEW priority-0.5 clause in
``UrgencyRouter.classify`` that honors a ctx pre-stamped from the
envelope's ``routing_override`` via the intake router.

Contract (binding):

- Default unset master flag ``JARVIS_BACKLOG_URGENCY_HINT_ENABLED=false``
  → the priority-0.5 clause is inert even when ctx.provider_route is
  populated with an envelope_routing_override reason. Routing flows to
  priority 1-5 as pre-F2.
- Master flag on + ctx.provider_route is a valid ProviderRoute value +
  ctx.provider_route_reason starts with "envelope_routing_override" →
  UrgencyRouter returns (ProviderRoute(ctx.provider_route),
  "envelope_routing_override:<value>") immediately, bypassing the
  source/urgency/complexity/cross-repo matrix.
- Master flag on + ctx.provider_route set but reason does NOT start
  with "envelope_routing_override" (e.g. harness pre-stamp flag set
  it) → the F2 priority-0.5 clause ignores the ctx; falls through.
  This keeps F2 orthogonal to the pre-existing
  JARVIS_URGENCY_ROUTER_RESPECT_PRE_STAMPED harness knob.
- Invalid provider_route value (not a ProviderRoute enum member) →
  priority-0.5 clause skips + falls through; never raises.

Priority ordering (UrgencyRouter):

    0 (existing) JARVIS_URGENCY_ROUTER_RESPECT_PRE_STAMPED harness knob
    0.5 (F2)     envelope_routing_override (this test file)
    1            IMMEDIATE (critical / voice / cross_repo)
    2            SPECULATIVE (intent_discovery)
    3            BACKGROUND (source-type default)
    4            COMPLEX (heavy_code / multi-file)
    5            STANDARD (default)

The priority-0.5 clause MUST run AFTER the harness priority-0 so that
explicit harness overrides (tests forcing a specific route regardless
of envelope contents) always win. MUST run BEFORE priority 1 so that
F2 bypasses the source=backlog→BACKGROUND trap even for non-critical
urgency.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.core.ouroboros.governance.urgency_router import (
    ProviderRoute,
    UrgencyRouter,
)


def _ctx(
    *,
    urgency: str = "normal",
    source: str = "backlog",
    complexity: str = "moderate",
    target_files: tuple = ("a.py", "b.py", "c.py"),
    cross_repo: bool = False,
    provider_route: str = "",
    provider_route_reason: str = "",
) -> SimpleNamespace:
    """Duck-typed OperationContext stub (matches existing test_urgency_router
    pattern). UrgencyRouter reads all fields via getattr."""
    return SimpleNamespace(
        signal_urgency=urgency,
        signal_source=source,
        task_complexity=complexity,
        target_files=list(target_files),
        cross_repo=cross_repo,
        provider_route=provider_route,
        provider_route_reason=provider_route_reason,
    )


@pytest.fixture
def router() -> UrgencyRouter:
    return UrgencyRouter()


# ---------------------------------------------------------------------------
# (1) Default-off parity — the F2 clause must be inert when flag is off.
# ---------------------------------------------------------------------------


def test_flag_off_envelope_override_ignored_for_backlog_source(
    monkeypatch, router,
):
    """Flag off: even with ctx pre-stamped from envelope, router flows
    to pre-F2 behavior (source=backlog, low urgency → BACKGROUND)."""
    monkeypatch.delenv("JARVIS_BACKLOG_URGENCY_HINT_ENABLED", raising=False)
    ctx = _ctx(
        urgency="normal",
        source="backlog",
        complexity="simple",
        provider_route="standard",
        provider_route_reason="envelope_routing_override:standard",
    )
    route, reason = router.classify(ctx)
    # Pre-F2 path: source=backlog + complexity not in _COMPLEX_COMPLEXITIES →
    # BACKGROUND. Confirms F2 clause did NOT fire.
    assert route is ProviderRoute.BACKGROUND
    assert "background_source:backlog" in reason


def test_flag_off_envelope_override_preserves_critical_immediate(
    monkeypatch, router,
):
    """Flag off + critical urgency still routes IMMEDIATE (priority 1)."""
    monkeypatch.delenv("JARVIS_BACKLOG_URGENCY_HINT_ENABLED", raising=False)
    ctx = _ctx(
        urgency="critical",
        source="backlog",
        provider_route="background",  # envelope says bg — but flag off, ignored
        provider_route_reason="envelope_routing_override:background",
    )
    route, reason = router.classify(ctx)
    assert route is ProviderRoute.IMMEDIATE


# ---------------------------------------------------------------------------
# (2) Flag-on consumption — priority-0.5 clause wins over source default.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("override,expected_route", [
    ("immediate",   ProviderRoute.IMMEDIATE),
    ("standard",    ProviderRoute.STANDARD),
    ("complex",     ProviderRoute.COMPLEX),
    ("background",  ProviderRoute.BACKGROUND),
    ("speculative", ProviderRoute.SPECULATIVE),
])
def test_flag_on_envelope_override_wins(
    monkeypatch, router, override, expected_route,
):
    """Flag on: envelope_routing_override value beats source-type default."""
    monkeypatch.setenv("JARVIS_BACKLOG_URGENCY_HINT_ENABLED", "true")
    ctx = _ctx(
        urgency="normal",
        source="backlog",  # would normally route BACKGROUND
        complexity="simple",
        provider_route=override,
        provider_route_reason=f"envelope_routing_override:{override}",
    )
    route, reason = router.classify(ctx)
    assert route is expected_route
    assert reason == f"envelope_routing_override:{override}"


def test_flag_on_envelope_override_beats_critical_urgency(
    monkeypatch, router,
):
    """F2 priority-0.5 MUST beat priority-1 IMMEDIATE urgency.
    Rationale: if an operator explicitly declared a routing in backlog.json,
    that intent is more specific than the sensor urgency stamping.
    (Alternative design was rejected: having urgency=critical trump
    envelope_routing_override would mean operators cannot route a
    critical-stamped op anywhere except IMMEDIATE.)"""
    monkeypatch.setenv("JARVIS_BACKLOG_URGENCY_HINT_ENABLED", "true")
    ctx = _ctx(
        urgency="critical",
        source="backlog",
        provider_route="standard",
        provider_route_reason="envelope_routing_override:standard",
    )
    route, _ = router.classify(ctx)
    assert route is ProviderRoute.STANDARD


def test_flag_on_envelope_override_beats_cross_repo(monkeypatch, router):
    """F2 clause wins over cross_repo → IMMEDIATE default."""
    monkeypatch.setenv("JARVIS_BACKLOG_URGENCY_HINT_ENABLED", "true")
    ctx = _ctx(
        urgency="normal",
        source="backlog",
        cross_repo=True,
        provider_route="complex",
        provider_route_reason="envelope_routing_override:complex",
    )
    route, _ = router.classify(ctx)
    assert route is ProviderRoute.COMPLEX


# ---------------------------------------------------------------------------
# (3) Orthogonality with existing harness pre-stamp flag.
# ---------------------------------------------------------------------------


def test_f2_flag_off_but_harness_flag_on_still_respects_pre_stamp(
    monkeypatch, router,
):
    """Harness pre-stamp (priority-0) still works when F2 flag is off —
    F2 additions didn't regress the existing flag."""
    monkeypatch.delenv("JARVIS_BACKLOG_URGENCY_HINT_ENABLED", raising=False)
    monkeypatch.setenv("JARVIS_URGENCY_ROUTER_RESPECT_PRE_STAMPED", "true")
    ctx = _ctx(
        urgency="normal",
        source="backlog",
        provider_route="standard",
        provider_route_reason="forced_by_harness",  # not F2 prefix
    )
    route, reason = router.classify(ctx)
    assert route is ProviderRoute.STANDARD
    assert reason == "forced_pre_stamped:standard"


def test_f2_flag_on_ignores_non_f2_reason(monkeypatch, router):
    """F2 flag on + ctx.provider_route set but reason NOT F2-prefix →
    F2 priority-0.5 falls through; routing flows via source default.
    Proves F2 does NOT accidentally consume harness / other pre-stamps.
    """
    monkeypatch.setenv("JARVIS_BACKLOG_URGENCY_HINT_ENABLED", "true")
    # Harness flag explicitly OFF — don't let priority-0 interfere
    monkeypatch.delenv(
        "JARVIS_URGENCY_ROUTER_RESPECT_PRE_STAMPED", raising=False,
    )
    ctx = _ctx(
        urgency="normal",
        source="backlog",
        complexity="simple",
        provider_route="immediate",
        provider_route_reason="some_other_reason",  # NOT envelope_routing_override
    )
    route, reason = router.classify(ctx)
    # Falls through to source-default: source=backlog + non-complex → BACKGROUND.
    assert route is ProviderRoute.BACKGROUND
    assert "background_source:backlog" in reason


def test_f2_and_harness_both_on_harness_wins_at_priority_0(
    monkeypatch, router,
):
    """Priority ordering: harness pre-stamp (priority-0) runs BEFORE the
    F2 priority-0.5 clause. When both are active and ctx has an
    envelope_routing_override, harness returns first with forced_pre_stamped.
    """
    monkeypatch.setenv("JARVIS_URGENCY_ROUTER_RESPECT_PRE_STAMPED", "true")
    monkeypatch.setenv("JARVIS_BACKLOG_URGENCY_HINT_ENABLED", "true")
    ctx = _ctx(
        urgency="normal",
        source="backlog",
        provider_route="complex",
        provider_route_reason="envelope_routing_override:complex",
    )
    route, reason = router.classify(ctx)
    assert route is ProviderRoute.COMPLEX
    # Harness returns FIRST with its own reason format.
    assert reason == "forced_pre_stamped:complex"


# ---------------------------------------------------------------------------
# (4) Safety — invalid / empty provider_route values.
# ---------------------------------------------------------------------------


def test_flag_on_empty_provider_route_falls_through(monkeypatch, router):
    """Flag on + ctx.provider_route empty string → F2 clause skips."""
    monkeypatch.setenv("JARVIS_BACKLOG_URGENCY_HINT_ENABLED", "true")
    ctx = _ctx(
        urgency="normal",
        source="backlog",
        complexity="simple",
        provider_route="",
        provider_route_reason="envelope_routing_override:",
    )
    route, _ = router.classify(ctx)
    assert route is ProviderRoute.BACKGROUND


def test_flag_on_invalid_provider_route_falls_through(monkeypatch, router):
    """Flag on + ctx.provider_route is bogus → F2 clause skips without raising."""
    monkeypatch.setenv("JARVIS_BACKLOG_URGENCY_HINT_ENABLED", "true")
    ctx = _ctx(
        urgency="normal",
        source="backlog",
        complexity="simple",
        provider_route="NOT_A_ROUTE",
        provider_route_reason="envelope_routing_override:NOT_A_ROUTE",
    )
    route, reason = router.classify(ctx)
    # Falls through: invalid F2 value silently drops back to source default.
    assert route is ProviderRoute.BACKGROUND
    assert "background_source:backlog" in reason


def test_flag_on_provider_route_case_insensitive(monkeypatch, router):
    """Intake router stamps lowercase; but guard against accidental case."""
    monkeypatch.setenv("JARVIS_BACKLOG_URGENCY_HINT_ENABLED", "true")
    ctx = _ctx(
        urgency="normal",
        source="backlog",
        provider_route="STANDARD",  # uppercase
        provider_route_reason="envelope_routing_override:STANDARD",
    )
    route, _ = router.classify(ctx)
    assert route is ProviderRoute.STANDARD


# ---------------------------------------------------------------------------
# (5) Pre-F2 parity — nothing pre-stamped, no flag → byte-identical.
# ---------------------------------------------------------------------------


def test_nothing_pre_stamped_flag_off_byte_identical_to_pre_f2(
    monkeypatch, router,
):
    """No ctx.provider_route set, no flag → pre-F2 routing, proves
    F2 added no latent behavior for default-off default-unset sessions."""
    monkeypatch.delenv("JARVIS_BACKLOG_URGENCY_HINT_ENABLED", raising=False)
    ctx = _ctx(urgency="normal", source="backlog", complexity="simple")
    route, reason = router.classify(ctx)
    assert route is ProviderRoute.BACKGROUND
    assert "background_source:backlog" in reason


def test_nothing_pre_stamped_flag_on_byte_identical_to_pre_f2(
    monkeypatch, router,
):
    """Flag on but no ctx.provider_route → F2 clause inert; pre-F2 routing.
    Proves the flag alone doesn't change routing — only envelope override
    does."""
    monkeypatch.setenv("JARVIS_BACKLOG_URGENCY_HINT_ENABLED", "true")
    ctx = _ctx(urgency="normal", source="backlog", complexity="simple")
    route, reason = router.classify(ctx)
    assert route is ProviderRoute.BACKGROUND
    assert "background_source:backlog" in reason


# ---------------------------------------------------------------------------
# (6) Wave 3 (6) Slice 5a use-case — the specific scenario F2 unblocks.
# ---------------------------------------------------------------------------


def test_wave3_slice5a_forced_reach_seed_with_f2_reaches_standard(
    monkeypatch, router,
):
    """The exact live scenario that was blocked without F2:
    source=backlog + critical urgency + 3 target files →
    pre-F2: IMMEDIATE (priority-1 urgency wins; still Claude-direct, no
             multi-file fan-out because IMMEDIATE is not STANDARD/COMPLEX).
    With F2 flag on + routing_hint=standard stamped on envelope:
    → STANDARD (reaches post-GENERATE parallel_dispatch seam with
      multi-file candidate → [ParallelDispatch] eligibility fires).
    """
    monkeypatch.setenv("JARVIS_BACKLOG_URGENCY_HINT_ENABLED", "true")
    ctx = _ctx(
        urgency="critical",  # F3 stamped this
        source="backlog",
        complexity="simple",
        target_files=(
            "backend/core/ouroboros/architect/__init__.py",
            "backend/core/tui/__init__.py",
            "backend/core/umf/__init__.py",
        ),
        provider_route="standard",
        provider_route_reason="envelope_routing_override:standard",
    )
    route, reason = router.classify(ctx)
    assert route is ProviderRoute.STANDARD, (
        f"forced-reach seed must route STANDARD with F2; got {route} ({reason})"
    )
    assert reason == "envelope_routing_override:standard"
