"""Move 3 Slice 4 — operator surfaces + master-flag graduation.

Pins:
  * ``JARVIS_AUTO_ACTION_ROUTER_ENABLED`` GRADUATED to default-true
    (was false in Slices 1-3). Asymmetric env semantics — explicit
    falsy hot-reverts; empty/unset = graduated default-on.
  * ``ENFORCE`` flag stays default-false (locked off until separate
    later authorization — operator binding).
  * ``EVENT_TYPE_AUTO_ACTION_PROPOSAL_EMITTED`` constant pinned.
  * ``publish_auto_action_proposal_emitted`` fires on actionable
    proposals, skips NO_ACTION, NEVER raises.
  * ``proposal_stats`` aggregates ledger rows correctly + handles
    malformed/empty input cleanly.
  * ``register_auto_action_routes`` mounts 2 GET endpoints on an
    aiohttp app; handler returns 503 when master flag is off,
    schema_version stamped in every response.
  * ``AutoActionShadowObserver`` calls
    ``publish_auto_action_proposal_emitted`` when ledger.append
    succeeds (1:1 SSE↔ledger relationship).
  * ``event_channel.py`` wires ``register_auto_action_routes`` +
    ``install_shadow_observer`` (bytes-pin).
  * ``serpent_flow.py`` SerpentREPL accepts ``/auto-action`` +
    ``/auto-action stats`` + ``/auto-action <op_id>``; help lists
    the new command (bytes-pin).

Authority Invariant
-------------------
Tests import only the modules under test + stdlib + Rich + aiohttp
(for route handler tests).
"""
from __future__ import annotations

import importlib
import io
import json
import pathlib
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _isolate_router_state():
    """Clear all router process-local state between tests."""
    from backend.core.ouroboros.governance.auto_action_router import (
        _verdict_buffer, reset_default_ledger_for_tests,
        clear_op_context_registry, reset_post_postmortem_observer,
    )
    _verdict_buffer.clear()
    reset_default_ledger_for_tests()
    clear_op_context_registry()
    reset_post_postmortem_observer()
    yield
    _verdict_buffer.clear()
    reset_default_ledger_for_tests()
    clear_op_context_registry()
    reset_post_postmortem_observer()


# -----------------------------------------------------------------------
# § A — Graduation pins (the master-flag flip)
# -----------------------------------------------------------------------


def test_master_flag_graduated_default_true(monkeypatch):
    """The headline pin of Slice 4: default flipped to True."""
    monkeypatch.delenv("JARVIS_AUTO_ACTION_ROUTER_ENABLED", raising=False)
    import backend.core.ouroboros.governance.auto_action_router as m
    importlib.reload(m)
    assert m.auto_action_router_enabled() is True


def test_master_flag_explicit_falsy_hot_reverts(monkeypatch):
    """Operator escape hatch — graduated default doesn't lock the
    flag on; explicit false still disables."""
    import backend.core.ouroboros.governance.auto_action_router as m
    for val in ("0", "false", "no", "off"):
        monkeypatch.setenv("JARVIS_AUTO_ACTION_ROUTER_ENABLED", val)
        importlib.reload(m)
        assert m.auto_action_router_enabled() is False


def test_enforce_flag_still_locked_off(monkeypatch):
    """Operator binding: ENFORCE stays default-false even after
    master graduation. Mutation boundary requires separate
    authorization."""
    monkeypatch.delenv("JARVIS_AUTO_ACTION_ENFORCE", raising=False)
    import backend.core.ouroboros.governance.auto_action_router as m
    importlib.reload(m)
    assert m.auto_action_enforce() is False


# -----------------------------------------------------------------------
# § B — SSE event constant + publish helper
# -----------------------------------------------------------------------


def test_sse_event_type_constant_pinned():
    from backend.core.ouroboros.governance.auto_action_router import (
        EVENT_TYPE_AUTO_ACTION_PROPOSAL_EMITTED,
    )
    assert EVENT_TYPE_AUTO_ACTION_PROPOSAL_EMITTED == (
        "auto_action_proposal_emitted"
    )


def test_publish_skips_no_action():
    """NO_ACTION proposals never trigger a publish — operator
    surfaces should see 1:1 SSE↔actionable-ledger-row."""
    from backend.core.ouroboros.governance.auto_action_router import (
        publish_auto_action_proposal_emitted, AdvisoryAction,
        AdvisoryActionType,
    )
    no_action = AdvisoryAction(
        action_type=AdvisoryActionType.NO_ACTION,
        reason_code="no_signal",
        evidence="",
    )
    result = publish_auto_action_proposal_emitted(no_action)
    assert result is None


def test_publish_calls_broker_on_actionable_proposal():
    """An actionable proposal calls the SSE broker.publish with
    the constant event type + structured payload."""
    from backend.core.ouroboros.governance.auto_action_router import (
        publish_auto_action_proposal_emitted, AdvisoryAction,
        AdvisoryActionType, EVENT_TYPE_AUTO_ACTION_PROPOSAL_EMITTED,
    )
    captured = []

    class _FakeBroker:
        def publish(self, *, event_type, op_id, payload):
            captured.append((event_type, op_id, payload))
            return "frame-id-123"

    with patch(
        "backend.core.ouroboros.governance.ide_observability_stream"
        ".get_default_broker",
        return_value=_FakeBroker(),
    ):
        action = AdvisoryAction(
            action_type=AdvisoryActionType.DEMOTE_RISK_TIER,
            reason_code="op_family_failure_rate_safe_auto",
            evidence="3/3 failed",
            target_op_family="doc_staleness",
            proposed_risk_tier="notify_apply",
            op_id="op-abc",
        )
        result = publish_auto_action_proposal_emitted(action)
    assert result == "frame-id-123"
    assert len(captured) == 1
    event_type, op_id, payload = captured[0]
    assert event_type == EVENT_TYPE_AUTO_ACTION_PROPOSAL_EMITTED
    assert op_id == "op-abc"
    assert payload["action_type"] == "demote_risk_tier"
    assert payload["target_op_family"] == "doc_staleness"
    assert payload["proposed_risk_tier"] == "notify_apply"
    assert "schema_version" in payload
    assert "wall_ts" in payload


def test_publish_swallows_broker_failure():
    """Broker failure must not propagate — SSE is best-effort."""
    from backend.core.ouroboros.governance.auto_action_router import (
        publish_auto_action_proposal_emitted, AdvisoryAction,
        AdvisoryActionType,
    )
    with patch(
        "backend.core.ouroboros.governance.ide_observability_stream"
        ".get_default_broker",
        side_effect=RuntimeError("broker died"),
    ):
        action = AdvisoryAction(
            action_type=AdvisoryActionType.DEFER_OP_FAMILY,
            reason_code="x", evidence="x",
        )
        # Must not raise
        result = publish_auto_action_proposal_emitted(action)
    assert result is None


# -----------------------------------------------------------------------
# § C — proposal_stats aggregator
# -----------------------------------------------------------------------


def test_proposal_stats_empty():
    from backend.core.ouroboros.governance.auto_action_router import (
        proposal_stats,
    )
    s = proposal_stats(())
    assert s["total"] == 0
    assert s["by_action_type"] == {}
    assert s["by_op_family"] == {}
    assert s["by_category"] == {}


def test_proposal_stats_aggregates_correctly():
    from backend.core.ouroboros.governance.auto_action_router import (
        proposal_stats,
    )
    rows = [
        {
            "action_type": "demote_risk_tier",
            "target_op_family": "doc_staleness",
            "target_category": "",
        },
        {
            "action_type": "demote_risk_tier",
            "target_op_family": "doc_staleness",
            "target_category": "",
        },
        {
            "action_type": "defer_op_family",
            "target_op_family": "github_issue",
            "target_category": "",
        },
        {
            "action_type": "raise_exploration_floor",
            "target_op_family": "",
            "target_category": "read_file",
        },
    ]
    s = proposal_stats(rows)
    assert s["total"] == 4
    assert s["by_action_type"]["demote_risk_tier"] == 2
    assert s["by_action_type"]["defer_op_family"] == 1
    assert s["by_action_type"]["raise_exploration_floor"] == 1
    assert s["by_op_family"]["doc_staleness"] == 2
    assert s["by_op_family"]["github_issue"] == 1
    assert s["by_category"]["read_file"] == 1


def test_proposal_stats_skips_malformed_rows():
    from backend.core.ouroboros.governance.auto_action_router import (
        proposal_stats,
    )
    rows = [
        "not a dict",
        None,
        {"action_type": "defer_op_family"},
        42,
    ]
    s = proposal_stats(rows)
    assert s["total"] == 1  # only the valid dict counts
    assert s["by_action_type"] == {"defer_op_family": 1}


# -----------------------------------------------------------------------
# § D — register_auto_action_routes (aiohttp surface)
# -----------------------------------------------------------------------


def _build_test_app(ledger=None):
    """Minimal aiohttp Application for route mounting tests."""
    from aiohttp import web
    from backend.core.ouroboros.governance.auto_action_router import (
        register_auto_action_routes,
    )
    app = web.Application()
    register_auto_action_routes(app, ledger=ledger)
    return app


def test_routes_mount_two_endpoints():
    from aiohttp import web
    app = _build_test_app()
    routes = [
        r for r in app.router.routes()
        if r.method == "GET"
    ]
    paths = [r.resource.canonical for r in routes]
    assert "/observability/auto-action" in paths
    assert "/observability/auto-action/stats" in paths


def _fake_request(query: dict = None):
    """Build a duck-typed request object for direct handler invocation
    without binding a real socket (sandbox-friendly). The route
    handlers only read ``request.query`` so this is sufficient."""
    from types import SimpleNamespace
    return SimpleNamespace(query=query or {})


@pytest.mark.asyncio
async def test_routes_recent_handler_returns_rows(tmp_path, monkeypatch):
    monkeypatch.delenv("JARVIS_AUTO_ACTION_ROUTER_ENABLED", raising=False)
    from backend.core.ouroboros.governance.auto_action_router import (
        AutoActionProposalLedger, AdvisoryAction, AdvisoryActionType,
        _AutoActionRoutesHandler,
    )
    ledger = AutoActionProposalLedger(path=tmp_path / "ledger.jsonl")
    ledger.append(AdvisoryAction(
        action_type=AdvisoryActionType.DEMOTE_RISK_TIER,
        reason_code="x", evidence="x", op_id="op-1",
    ))
    ledger.append(AdvisoryAction(
        action_type=AdvisoryActionType.DEFER_OP_FAMILY,
        reason_code="y", evidence="y", op_id="op-2",
    ))
    handler = _AutoActionRoutesHandler(ledger=ledger)
    resp = await handler.handle_recent(_fake_request())
    assert resp.status == 200
    data = json.loads(resp.body.decode("utf-8"))
    assert data["count"] == 2
    assert data["limit"] == 100
    assert data["schema_version"] == "auto_action_router.1"
    assert data["rows"][0]["op_id"] == "op-1"
    assert data["rows"][1]["op_id"] == "op-2"


@pytest.mark.asyncio
async def test_routes_recent_returns_503_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_AUTO_ACTION_ROUTER_ENABLED", "0")
    import backend.core.ouroboros.governance.auto_action_router as m
    importlib.reload(m)
    handler = m._AutoActionRoutesHandler(
        ledger=m.AutoActionProposalLedger(path=tmp_path / "ledger.jsonl")
    )
    resp = await handler.handle_recent(_fake_request())
    assert resp.status == 503
    data = json.loads(resp.body.decode("utf-8"))
    assert data["error"] == "disabled"


@pytest.mark.asyncio
async def test_routes_stats_aggregates(tmp_path, monkeypatch):
    monkeypatch.delenv("JARVIS_AUTO_ACTION_ROUTER_ENABLED", raising=False)
    from backend.core.ouroboros.governance.auto_action_router import (
        AutoActionProposalLedger, AdvisoryAction, AdvisoryActionType,
        _AutoActionRoutesHandler,
    )
    ledger = AutoActionProposalLedger(path=tmp_path / "ledger.jsonl")
    for _ in range(3):
        ledger.append(AdvisoryAction(
            action_type=AdvisoryActionType.DEMOTE_RISK_TIER,
            reason_code="x", evidence="x",
            target_op_family="doc_staleness",
        ))
    ledger.append(AdvisoryAction(
        action_type=AdvisoryActionType.DEFER_OP_FAMILY,
        reason_code="y", evidence="y",
        target_op_family="github_issue",
    ))
    handler = _AutoActionRoutesHandler(ledger=ledger)
    resp = await handler.handle_stats(_fake_request())
    assert resp.status == 200
    data = json.loads(resp.body.decode("utf-8"))
    assert data["total"] == 4
    assert data["by_action_type"]["demote_risk_tier"] == 3
    assert data["by_action_type"]["defer_op_family"] == 1


# -----------------------------------------------------------------------
# § E — Shadow observer fires SSE on actionable proposal
# -----------------------------------------------------------------------


def test_shadow_observer_publishes_sse_on_ledger_append(tmp_path, monkeypatch):
    """When ledger.append succeeds (returns True), the observer
    must call publish_auto_action_proposal_emitted. NO_ACTION
    cases skip both ledger AND publish."""
    monkeypatch.setenv("JARVIS_AUTO_ACTION_ROUTER_ENABLED", "1")
    from backend.core.ouroboros.governance.auto_action_router import (
        AutoActionShadowObserver, AutoActionProposalLedger,
        register_op_context, lookup_op_context, RecentOpOutcome,
    )
    register_op_context(
        "op-trigger",
        op_family="doc_staleness",
        risk_tier="SAFE_AUTO",
        route="background",
        posture="EXPLORE",
    )
    ledger = AutoActionProposalLedger(path=tmp_path / "ledger.jsonl")
    obs = AutoActionShadowObserver(
        ledger=ledger,
        ctx_lookup=lookup_op_context,
    )
    fake_outcomes = tuple(
        RecentOpOutcome(
            op_id=f"op-{i}", op_family="doc_staleness",
            success=False, risk_tier="SAFE_AUTO",
        )
        for i in range(3)
    )
    publish_calls = []

    def _capture(action):
        publish_calls.append(action)
        return "frame-x"

    with patch(
        "backend.core.ouroboros.governance.auto_action_router"
        ".recent_postmortem_outcomes",
        return_value=fake_outcomes,
    ), patch(
        "backend.core.ouroboros.governance.auto_action_router"
        ".publish_auto_action_proposal_emitted",
        side_effect=_capture,
    ):
        obs.on_terminal_postmortem_persisted(
            op_id="op-trigger", terminal_phase="VERIFY",
            has_blocking_failures=True,
        )
    # Ledger row written + SSE published exactly once
    assert len(ledger.read_recent()) == 1
    assert len(publish_calls) == 1
    assert publish_calls[0].action_type.value == "demote_risk_tier"
    assert publish_calls[0].op_id == "op-trigger"


def test_shadow_observer_no_publish_on_no_action(tmp_path, monkeypatch):
    """No signal → NO_ACTION → ledger skipped → SSE skipped."""
    monkeypatch.setenv("JARVIS_AUTO_ACTION_ROUTER_ENABLED", "1")
    from backend.core.ouroboros.governance.auto_action_router import (
        AutoActionShadowObserver, AutoActionProposalLedger,
    )
    ledger = AutoActionProposalLedger(path=tmp_path / "ledger.jsonl")
    obs = AutoActionShadowObserver(ledger=ledger)
    publish_calls = []
    with patch(
        "backend.core.ouroboros.governance.auto_action_router"
        ".recent_postmortem_outcomes",
        return_value=(),
    ), patch(
        "backend.core.ouroboros.governance.auto_action_router"
        ".publish_auto_action_proposal_emitted",
        side_effect=lambda a: publish_calls.append(a),
    ):
        obs.on_terminal_postmortem_persisted(
            op_id="op-clean", terminal_phase="VERIFY",
            has_blocking_failures=False,
        )
    assert len(ledger.read_recent()) == 0
    assert len(publish_calls) == 0


# -----------------------------------------------------------------------
# § F — Bytes pins on event_channel + serpent_flow wirings
# -----------------------------------------------------------------------


def test_event_channel_wires_auto_action_routes():
    """event_channel.py must call register_auto_action_routes +
    install_shadow_observer when the master flag is on."""
    src = pathlib.Path(
        "backend/core/ouroboros/governance/event_channel.py"
    ).read_text()
    assert "register_auto_action_routes" in src
    assert "install_shadow_observer" in src
    # Master flag check before mounting
    assert "auto_action_router_enabled" in src


def test_serpent_flow_dispatches_auto_action():
    """SerpentREPL accepts /auto-action with no arg, with stats
    subcommand, and with arbitrary op_id arg."""
    src = pathlib.Path(
        "backend/core/ouroboros/battle_test/serpent_flow.py"
    ).read_text()
    # Bare command
    assert (
        'line == "auto-action"' in src
        or 'line == "/auto-action"' in src
    )
    # Subcommand routing
    assert 'line.startswith("/auto-action ")' in src or (
        'line.startswith("auto-action ")' in src
    )
    # Handler defined
    assert "def _print_auto_action(self" in src


def test_serpent_flow_help_lists_auto_action():
    src = pathlib.Path(
        "backend/core/ouroboros/battle_test/serpent_flow.py"
    ).read_text()
    fn_idx = src.find("def _print_help(self)")
    end_idx = src.find("\n    def ", fn_idx + 1)
    body = src[fn_idx:end_idx if end_idx > fn_idx else fn_idx + 5000]
    assert "/auto-action" in body


# -----------------------------------------------------------------------
# § G — REPL behavioral test
# -----------------------------------------------------------------------


def _make_flow_repl():
    from rich.console import Console
    from backend.core.ouroboros.battle_test.serpent_flow import (
        SerpentFlow, SerpentREPL,
    )
    flow = SerpentFlow(
        session_id="bt-slice4-test",
        cost_cap_usd=2.50,
        idle_timeout_s=3600.0,
    )
    buf = io.StringIO()
    flow.console = Console(
        file=buf, force_terminal=False, width=120, color_system=None,
    )
    return flow, SerpentREPL(flow=flow), buf


def test_repl_auto_action_empty_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "JARVIS_AUTO_ACTION_LEDGER_PATH", str(tmp_path / "ledger.jsonl"),
    )
    from backend.core.ouroboros.governance.auto_action_router import (
        reset_default_ledger_for_tests,
    )
    reset_default_ledger_for_tests()
    flow, repl, buf = _make_flow_repl()
    repl._print_auto_action()
    out = buf.getvalue()
    assert "Auto-Action proposals" in out
    assert "No advisory proposals yet" in out


def test_repl_auto_action_with_rows(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "JARVIS_AUTO_ACTION_LEDGER_PATH", str(tmp_path / "ledger.jsonl"),
    )
    from backend.core.ouroboros.governance.auto_action_router import (
        reset_default_ledger_for_tests, get_default_ledger,
        AdvisoryAction, AdvisoryActionType,
    )
    reset_default_ledger_for_tests()
    ledger = get_default_ledger()
    ledger.append(AdvisoryAction(
        action_type=AdvisoryActionType.DEMOTE_RISK_TIER,
        reason_code="op_family_failure_rate_safe_auto",
        evidence="3/3 failed in doc_staleness family",
        target_op_family="doc_staleness",
        proposed_risk_tier="notify_apply",
        op_id="op-abc-123",
    ))
    flow, repl, buf = _make_flow_repl()
    repl._print_auto_action()
    out = buf.getvalue()
    assert "Auto-Action proposals" in out
    assert "demote_risk_tier" in out
    assert "doc_staleness" in out
    # No box-drawing glyphs — inline output per UI Slice 5/6
    for glyph in ("╭", "╰", "╮", "╯", "┌", "└", "┐", "┘"):
        assert glyph not in out


def test_repl_auto_action_stats_subcommand(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "JARVIS_AUTO_ACTION_LEDGER_PATH", str(tmp_path / "ledger.jsonl"),
    )
    from backend.core.ouroboros.governance.auto_action_router import (
        reset_default_ledger_for_tests, get_default_ledger,
        AdvisoryAction, AdvisoryActionType,
    )
    reset_default_ledger_for_tests()
    ledger = get_default_ledger()
    for _ in range(3):
        ledger.append(AdvisoryAction(
            action_type=AdvisoryActionType.DEMOTE_RISK_TIER,
            reason_code="x", evidence="x",
            target_op_family="doc_staleness",
        ))
    flow, repl, buf = _make_flow_repl()
    repl._print_auto_action(arg="stats")
    out = buf.getvalue()
    assert "Auto-Action Stats" in out
    assert "demote_risk_tier" in out
    assert "doc_staleness" in out


# -----------------------------------------------------------------------
# § H — Authority invariant
# -----------------------------------------------------------------------


def test_test_module_authority():
    src = pathlib.Path(__file__).read_text()
    forbidden = (
        "phase_runners", "iron_gate", "change_engine",
        "candidate_generator", "providers", "orchestrator",
    )
    for tok in forbidden:
        assert (
            f"from backend.core.ouroboros.governance.{tok}" not in src
        ), f"forbidden: {tok}"
