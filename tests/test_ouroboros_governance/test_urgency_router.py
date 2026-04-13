"""Regression tests for UrgencyRouter source → route mapping.

Backstory:
    In ``bt-2026-04-13-011909`` the Claude-only cost line hit $0.534 on
    3 stalled ops while DW took $0.002 — the inverse of the §5 cascade
    intent. Root cause traced to seven sensors copy-pasting
    ``source="runtime_health"`` onto their envelopes because the
    ``IntentEnvelope._VALID_SOURCES`` whitelist did not yet include
    ``todo_scanner`` / ``doc_staleness`` / ``github_issue`` etc. The
    UrgencyRouter then faithfully stamped every TodoScanner item as
    ``IMMEDIATE`` via ``high_urgency_immediate_source:runtime_health``,
    which skipped DW and routed the entire budget into Claude.

    This test file locks down the fix so that:
      - Each sensor's true source label routes to the *intended* tier.
      - High-urgency TODOs / doc staleness stay on DW (BACKGROUND).
      - Critical urgency still wins over source (safety net).
      - The `_IMMEDIATE_SOURCES` set stays tight — adding anything to
        it requires a deliberate test update, not a silent regression.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.core.ouroboros.governance.urgency_router import (
    ProviderRoute,
    UrgencyRouter,
    _IMMEDIATE_SOURCES,
    _BACKGROUND_SOURCES,
    _SPECULATIVE_SOURCES,
)


def _ctx(
    *,
    urgency: str = "normal",
    source: str = "",
    complexity: str = "moderate",
    target_files: tuple = ("backend/core/foo.py",),
    cross_repo: bool = False,
) -> SimpleNamespace:
    """Duck-typed OperationContext stub.

    UrgencyRouter only reads via ``getattr``, so a SimpleNamespace with
    the same field names is sufficient. Keeping the stub tiny makes the
    intent of each test obvious at a glance.
    """
    return SimpleNamespace(
        signal_urgency=urgency,
        signal_source=source,
        task_complexity=complexity,
        target_files=list(target_files),
        cross_repo=cross_repo,
    )


@pytest.fixture
def router() -> UrgencyRouter:
    return UrgencyRouter()


# ---------------------------------------------------------------------------
# Regression guard — the exact bug that burned $0.53 in bt-2026-04-13-011909
# ---------------------------------------------------------------------------


class TestBattleTest011909Regression:
    """Ops that burned the Claude budget must now route to DW."""

    def test_todo_scanner_high_urgency_routes_background_not_immediate(
        self, router: UrgencyRouter,
    ) -> None:
        """op-019d846f pattern: TODO at verify_gate.py, urgency=high.

        Pre-fix: stamped `source="runtime_health"` → IMMEDIATE → Claude.
        Post-fix: stamped `source="todo_scanner"` → BACKGROUND → DW.
        """
        ctx = _ctx(urgency="high", source="todo_scanner", complexity="simple")
        route, reason = router.classify(ctx)
        assert route is ProviderRoute.BACKGROUND, (
            f"TodoScanner high-urgency must route BACKGROUND to stay off "
            f"the Claude tier; got {route} ({reason})"
        )
        assert "todo_scanner" in reason

    def test_doc_staleness_high_urgency_routes_background(
        self, router: UrgencyRouter,
    ) -> None:
        """DocStaleness was the second-worst offender — same fix."""
        ctx = _ctx(urgency="high", source="doc_staleness", complexity="simple")
        route, _reason = router.classify(ctx)
        assert route is ProviderRoute.BACKGROUND

    def test_github_issue_non_critical_routes_standard_not_immediate(
        self, router: UrgencyRouter,
    ) -> None:
        """GitHub issue ops are moderate-priority by default.

        The regression had `github_issue_sensor` emitting
        `source="runtime_health"`, which tripped the
        `high_urgency_immediate_source` rule. With the correct source,
        non-critical issues must fall through to STANDARD so DW gets
        first shot at them.
        """
        ctx = _ctx(urgency="high", source="github_issue", complexity="moderate")
        route, reason = router.classify(ctx)
        assert route is ProviderRoute.STANDARD, (
            f"High-urgency github_issue must go STANDARD (DW primary); "
            f"got {route} ({reason})"
        )


# ---------------------------------------------------------------------------
# Source → route affinity contract
# ---------------------------------------------------------------------------


class TestSourceAffinityTables:
    """Lock down the contents of the three source affinity frozensets.

    Any future edit to ``_IMMEDIATE_SOURCES`` must break one of these
    tests on purpose — the cost blast radius of silently adding a
    source here is too high to leave unguarded.
    """

    def test_immediate_sources_tight(self) -> None:
        """Only three sources are allowed to imply IMMEDIATE routing.

        test_failure, voice_human, runtime_health — each represents
        a human-in-the-loop or live-runtime critical path where the
        Claude premium is justified. Anything else that wants
        IMMEDIATE must hit the `urgency == critical` short-circuit
        on the real OperationContext, not this set.
        """
        assert _IMMEDIATE_SOURCES == frozenset({
            "test_failure",
            "voice_human",
            "runtime_health",
        })

    def test_background_sources_include_cost_optimized_scanners(self) -> None:
        """BACKGROUND set must cover the CLAUDE.md-listed scanners."""
        assert "todo_scanner" in _BACKGROUND_SOURCES
        assert "doc_staleness" in _BACKGROUND_SOURCES
        assert "ai_miner" in _BACKGROUND_SOURCES
        assert "exploration" in _BACKGROUND_SOURCES
        assert "backlog" in _BACKGROUND_SOURCES
        assert "architecture" in _BACKGROUND_SOURCES

    def test_speculative_sources_contains_intent_discovery(self) -> None:
        assert "intent_discovery" in _SPECULATIVE_SOURCES

    def test_immediate_and_background_are_disjoint(self) -> None:
        """No source may be in both sets — would cause nondeterministic routing.

        The classifier hits IMMEDIATE checks first, so a mispairing
        would silently strand the BACKGROUND intent.
        """
        assert _IMMEDIATE_SOURCES.isdisjoint(_BACKGROUND_SOURCES)


# ---------------------------------------------------------------------------
# Urgency priority order
# ---------------------------------------------------------------------------


class TestUrgencyPriority:
    """Urgency=critical must always win — safety net."""

    @pytest.mark.parametrize("source", [
        "todo_scanner",
        "doc_staleness",
        "github_issue",
        "ai_miner",
        "exploration",
        "backlog",
        "performance_regression",
    ])
    def test_critical_urgency_overrides_any_source(
        self, router: UrgencyRouter, source: str,
    ) -> None:
        """Even BACKGROUND-eligible sources escalate when critical.

        A battle-test seed may mark a TODO as critical; we honor that
        over the default cost-optimization route.
        """
        ctx = _ctx(urgency="critical", source=source, complexity="simple")
        route, reason = router.classify(ctx)
        assert route is ProviderRoute.IMMEDIATE
        assert "critical_urgency" in reason

    def test_voice_command_always_immediate(self, router: UrgencyRouter) -> None:
        """Human waiting on voice reply — IMMEDIATE regardless of urgency."""
        ctx = _ctx(urgency="normal", source="voice_human", complexity="simple")
        route, reason = router.classify(ctx)
        assert route is ProviderRoute.IMMEDIATE
        assert "voice_command" in reason

    def test_runtime_health_high_urgency_routes_immediate(
        self, router: UrgencyRouter,
    ) -> None:
        """Keep the existing path alive — runtime_health is legitimately urgent."""
        ctx = _ctx(urgency="high", source="runtime_health", complexity="simple")
        route, _reason = router.classify(ctx)
        assert route is ProviderRoute.IMMEDIATE

    def test_test_failure_high_urgency_routes_immediate(
        self, router: UrgencyRouter,
    ) -> None:
        ctx = _ctx(urgency="high", source="test_failure", complexity="simple")
        route, _reason = router.classify(ctx)
        assert route is ProviderRoute.IMMEDIATE


# ---------------------------------------------------------------------------
# Default / fall-through behavior
# ---------------------------------------------------------------------------


class TestDefaultFallThrough:
    """Sources with no affinity entry must fall through to STANDARD."""

    def test_unknown_source_normal_urgency_routes_standard(
        self, router: UrgencyRouter,
    ) -> None:
        ctx = _ctx(urgency="normal", source="someone_new", complexity="moderate")
        route, _reason = router.classify(ctx)
        assert route is ProviderRoute.STANDARD

    def test_empty_source_normal_urgency_routes_standard(
        self, router: UrgencyRouter,
    ) -> None:
        ctx = _ctx(urgency="normal", source="", complexity="simple")
        route, _reason = router.classify(ctx)
        assert route is ProviderRoute.STANDARD

    def test_cross_repo_forces_immediate(self, router: UrgencyRouter) -> None:
        """cross_repo field short-circuits to IMMEDIATE even for background sources."""
        ctx = _ctx(
            urgency="normal",
            source="todo_scanner",
            complexity="simple",
            cross_repo=True,
        )
        route, reason = router.classify(ctx)
        assert route is ProviderRoute.IMMEDIATE
        assert "cross_repo" in reason

    def test_complex_task_routes_complex(self, router: UrgencyRouter) -> None:
        ctx = _ctx(
            urgency="normal",
            source="",
            complexity="heavy_code",
            target_files=("a.py", "b.py"),
        )
        route, _reason = router.classify(ctx)
        assert route is ProviderRoute.COMPLEX

    def test_multi_file_moderate_routes_complex(self, router: UrgencyRouter) -> None:
        ctx = _ctx(
            urgency="normal",
            source="",
            complexity="moderate",
            target_files=("a.py", "b.py", "c.py"),
        )
        route, _reason = router.classify(ctx)
        assert route is ProviderRoute.COMPLEX


# ---------------------------------------------------------------------------
# Envelope schema ↔ router coverage
# ---------------------------------------------------------------------------


class TestEnvelopeSourceSchemaCoverage:
    """Every sensor-declared source must route to *some* known tier.

    Without this test, a future sensor author could add a new source
    to `IntentEnvelope._VALID_SOURCES`, wire a sensor to emit it, and
    accidentally inherit the STANDARD fall-through — producing a
    semi-silent drift from the intended CLAUDE.md routing plan.
    """

    def test_every_valid_envelope_source_routes_to_expected_tier(
        self, router: UrgencyRouter,
    ) -> None:
        from backend.core.ouroboros.governance.intake.intent_envelope import (
            _VALID_SOURCES,
        )

        # Each source's *intended* route at normal urgency, moderate
        # complexity, single-file scope. Must be kept in sync with
        # UrgencyRouter's affinity tables.
        expected: dict[str, ProviderRoute] = {
            "test_failure": ProviderRoute.STANDARD,       # high urgency → IMMEDIATE; normal → STANDARD
            "voice_human": ProviderRoute.IMMEDIATE,       # always IMMEDIATE
            "runtime_health": ProviderRoute.STANDARD,     # normal urgency falls through
            "ai_miner": ProviderRoute.BACKGROUND,
            "exploration": ProviderRoute.BACKGROUND,
            "backlog": ProviderRoute.BACKGROUND,
            "architecture": ProviderRoute.BACKGROUND,
            "todo_scanner": ProviderRoute.BACKGROUND,
            "doc_staleness": ProviderRoute.BACKGROUND,
            "capability_gap": ProviderRoute.STANDARD,     # default fall-through
            "roadmap": ProviderRoute.STANDARD,            # default fall-through
            "cu_execution": ProviderRoute.STANDARD,       # default fall-through
            "intent_discovery": ProviderRoute.SPECULATIVE,
            "github_issue": ProviderRoute.STANDARD,
            "performance_regression": ProviderRoute.STANDARD,
            "cross_repo_drift": ProviderRoute.STANDARD,
            "security_advisory": ProviderRoute.STANDARD,
            "web_intelligence": ProviderRoute.STANDARD,
        }

        missing = _VALID_SOURCES - expected.keys()
        assert not missing, (
            f"_VALID_SOURCES contains entries without an expected-route "
            f"mapping: {sorted(missing)}. Add them here and pick a tier "
            f"on purpose — silent defaulting is how we got the "
            f"bt-2026-04-13-011909 regression."
        )

        for source in _VALID_SOURCES:
            ctx = _ctx(urgency="normal", source=source, complexity="moderate")
            route, reason = router.classify(ctx)
            assert route is expected[source], (
                f"source={source!r} routed to {route} ({reason}), "
                f"expected {expected[source]}"
            )
