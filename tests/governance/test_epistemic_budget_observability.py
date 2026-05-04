"""Upgrade 1 Slice 4 — observability + bridge + REPL tests
(PRD §31.2).

Pins the four Slice 4 surfaces together:

  § 1 — :func:`EpistemicBudget.to_dict` projection
  § 2 — :func:`EpistemicBudgetTracker.snapshot_all`
  § 3 — ``GET /observability/budget[/{op_id}]`` route handlers
  § 4 — ``/budget`` REPL dispatcher
  § 5 — :func:`attach_to_provider_run` + per-round callback +
        SSE publication
  § 6 — Authority floor (no orchestrator/tool_executor imports
        in any Slice 4 module)
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, AsyncMock

import pytest


def _enable(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_EPISTEMIC_BUDGET_ENABLED", "true",
    )


# ---------------------------------------------------------------------------
# § 1 — EpistemicBudget.to_dict projection
# ---------------------------------------------------------------------------


class TestProjection:
    def test_to_dict_has_required_fields(self, monkeypatch):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        t = EpistemicBudgetTracker()
        t.open(
            op_id="op-x", route="standard",
            risk_tier="safe_auto",
        )
        t.note_round_complete("op-x", confidence=0.85)
        budget = t.get("op-x")
        assert budget is not None
        d = budget.to_dict()
        # Required keys
        for key in (
            "schema_version", "op_id", "route", "risk_tier",
            "rounds_consumed", "max_rounds", "rounds_remaining",
            "probe_calls_consumed", "probe_call_cap",
            "probe_calls_remaining", "branch_calls_consumed",
            "sbt_branch_cap", "branch_calls_remaining",
            "confidence_drop_threshold",
            "last_probe_verdict", "last_sbt_verdict",
            "is_route_cost_gated", "is_rounds_exhausted",
            "is_at_or_above_notify_apply",
            "created_at_unix", "last_updated_at_unix",
            "trajectory",
        ):
            assert key in d, f"missing key: {key}"
        # Derived fields computed correctly
        assert d["rounds_consumed"] == 1
        assert (
            d["rounds_remaining"]
            == d["max_rounds"] - d["rounds_consumed"]
        )

    def test_to_dict_handles_no_rounds_consumed(
        self, monkeypatch,
    ):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        t = EpistemicBudgetTracker()
        t.open(
            op_id="op-y", route="standard",
            risk_tier="safe_auto",
        )
        d = t.get("op-y").to_dict()
        assert d["rounds_consumed"] == 0
        assert d["rounds_remaining"] == d["max_rounds"]
        assert d["last_probe_verdict"] is None


# ---------------------------------------------------------------------------
# § 2 — Tracker snapshot_all
# ---------------------------------------------------------------------------


class TestSnapshotAll:
    def test_snapshot_all_empty_tracker(self):
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        t = EpistemicBudgetTracker()
        assert t.snapshot_all() == ()

    def test_snapshot_all_returns_frozen(self, monkeypatch):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudget,
            EpistemicBudgetTracker,
        )
        t = EpistemicBudgetTracker()
        t.open(
            op_id="op-1", route="standard",
            risk_tier="safe_auto",
        )
        t.open(
            op_id="op-2", route="background",
            risk_tier="safe_auto",
        )
        snap = t.snapshot_all()
        assert len(snap) == 2
        assert all(
            isinstance(b, EpistemicBudget) for b in snap
        )
        # Frozen — attempting mutation raises FrozenInstanceError
        with pytest.raises(Exception):
            snap[0].rounds_consumed = 99


# ---------------------------------------------------------------------------
# § 3 — Observability HTTP handlers
# ---------------------------------------------------------------------------


def _make_request(
    *, match_info: Dict[str, str] = None,
    query: Dict[str, str] = None,
):
    """Minimal aiohttp Request mock — match_info + query."""
    req = MagicMock()
    req.match_info = match_info or {}
    req.query = query or {}
    return req


class TestObservabilityHandlers:
    @pytest.mark.asyncio
    async def test_overview_disabled_returns_503(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_EPISTEMIC_BUDGET_ENABLED", "false",
        )
        from backend.core.ouroboros.governance.epistemic_budget_observability import (  # noqa: E501
            _EpistemicBudgetRoutesHandler,
        )
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        h = _EpistemicBudgetRoutesHandler(
            tracker=EpistemicBudgetTracker(),
        )
        resp = await h.handle_overview(_make_request())
        assert resp.status == 503

    @pytest.mark.asyncio
    async def test_overview_returns_tracked_budgets(
        self, monkeypatch,
    ):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget_observability import (  # noqa: E501
            _EpistemicBudgetRoutesHandler,
        )
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        t = EpistemicBudgetTracker()
        t.open(
            op_id="op-A", route="standard",
            risk_tier="safe_auto",
        )
        t.note_round_complete("op-A", confidence=0.7)
        h = _EpistemicBudgetRoutesHandler(tracker=t)
        resp = await h.handle_overview(_make_request())
        assert resp.status == 200
        # aiohttp.web.json_response stores body in `_body` —
        # decode + parse for assertions
        import json
        body = json.loads(resp.body)
        assert body["tracked_count"] == 1
        assert len(body["budgets"]) == 1
        assert body["budgets"][0]["op_id"] == "op-A"
        assert body["budgets"][0]["rounds_consumed"] == 1
        assert (
            body["sse_event_type"] == "budget_action_taken"
        )
        assert "outcome_kinds" in body
        assert "config" in body

    @pytest.mark.asyncio
    async def test_detail_returns_full_trajectory(
        self, monkeypatch,
    ):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget_observability import (  # noqa: E501
            _EpistemicBudgetRoutesHandler,
        )
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        t = EpistemicBudgetTracker()
        t.open(
            op_id="op-B", route="standard",
            risk_tier="safe_auto",
        )
        t.note_round_complete("op-B", confidence=0.9)
        t.note_round_complete("op-B", confidence=0.8)
        t.note_round_complete("op-B", confidence=0.7)
        h = _EpistemicBudgetRoutesHandler(tracker=t)
        resp = await h.handle_detail(
            _make_request(match_info={"op_id": "op-B"}),
        )
        assert resp.status == 200
        import json
        body = json.loads(resp.body)
        assert body["op_id"] == "op-B"
        assert body["rounds_consumed"] == 3
        # Detail endpoint includes per-sample trajectory
        assert "samples" in body["trajectory"]
        assert len(body["trajectory"]["samples"]) == 3

    @pytest.mark.asyncio
    async def test_detail_unknown_op_returns_404(
        self, monkeypatch,
    ):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget_observability import (  # noqa: E501
            _EpistemicBudgetRoutesHandler,
        )
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        h = _EpistemicBudgetRoutesHandler(
            tracker=EpistemicBudgetTracker(),
        )
        resp = await h.handle_detail(
            _make_request(match_info={"op_id": "nonexistent"}),
        )
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_detail_missing_op_id_returns_400(
        self, monkeypatch,
    ):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget_observability import (  # noqa: E501
            _EpistemicBudgetRoutesHandler,
        )
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        h = _EpistemicBudgetRoutesHandler(
            tracker=EpistemicBudgetTracker(),
        )
        resp = await h.handle_detail(
            _make_request(match_info={"op_id": ""}),
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_rate_limit_check_blocks(
        self, monkeypatch,
    ):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget_observability import (  # noqa: E501
            _EpistemicBudgetRoutesHandler,
        )
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        h = _EpistemicBudgetRoutesHandler(
            tracker=EpistemicBudgetTracker(),
            rate_limit_check=lambda req: False,
        )
        resp = await h.handle_overview(_make_request())
        assert resp.status == 429

    def test_register_routes_mounts_two_endpoints(
        self,
    ):
        from backend.core.ouroboros.governance.epistemic_budget_observability import (  # noqa: E501
            register_routes,
        )
        from aiohttp import web
        app = web.Application()
        register_routes(app)
        # Both routes mounted
        routes = list(app.router.routes())
        paths = {
            r.resource.canonical for r in routes
            if r.resource is not None
        }
        assert "/observability/budget" in paths
        # Detail route uses {op_id} pattern
        assert any("/observability/budget/" in p for p in paths)


# ---------------------------------------------------------------------------
# § 4 — /budget REPL dispatcher
# ---------------------------------------------------------------------------


class TestBudgetREPL:
    def test_help_works_when_disabled(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_EPISTEMIC_BUDGET_ENABLED", "false",
        )
        from backend.core.ouroboros.governance.budget_repl import (  # noqa: E501
            dispatch_budget_command,
        )
        res = dispatch_budget_command("/budget help")
        assert res.ok is True
        assert "/budget" in res.text
        assert "PRD §31.2" in res.text

    def test_disabled_returns_friendly_message(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_EPISTEMIC_BUDGET_ENABLED", "false",
        )
        from backend.core.ouroboros.governance.budget_repl import (  # noqa: E501
            dispatch_budget_command,
        )
        res = dispatch_budget_command("/budget status")
        assert res.ok is False
        assert "disabled" in res.text.lower()

    def test_status_no_tracked_ops(self, monkeypatch):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        from backend.core.ouroboros.governance.budget_repl import (  # noqa: E501
            dispatch_budget_command,
        )
        t = EpistemicBudgetTracker()
        res = dispatch_budget_command(
            "/budget status", tracker=t,
        )
        assert res.ok is True
        assert "no ops currently tracked" in res.text

    def test_status_shows_tracked_ops(self, monkeypatch):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        from backend.core.ouroboros.governance.budget_repl import (  # noqa: E501
            dispatch_budget_command,
        )
        t = EpistemicBudgetTracker()
        t.open(
            op_id="op-x", route="standard",
            risk_tier="safe_auto",
        )
        t.note_round_complete("op-x", confidence=0.7)
        res = dispatch_budget_command(
            "/budget status", tracker=t,
        )
        assert res.ok is True
        assert "1 op(s) tracked" in res.text
        assert "route=standard" in res.text
        assert "tier=safe_auto" in res.text

    def test_op_detail(self, monkeypatch):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        from backend.core.ouroboros.governance.budget_repl import (  # noqa: E501
            dispatch_budget_command,
        )
        t = EpistemicBudgetTracker()
        t.open(
            op_id="op-x", route="standard",
            risk_tier="safe_auto",
        )
        res = dispatch_budget_command(
            "/budget op op-x", tracker=t,
        )
        assert res.ok is True
        assert "/budget op op-x" in res.text
        assert "rounds" in res.text

    def test_op_detail_unknown_op(self, monkeypatch):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        from backend.core.ouroboros.governance.budget_repl import (  # noqa: E501
            dispatch_budget_command,
        )
        t = EpistemicBudgetTracker()
        res = dispatch_budget_command(
            "/budget op missing", tracker=t,
        )
        assert res.ok is False
        assert "not tracked" in res.text

    def test_op_missing_arg(self, monkeypatch):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        from backend.core.ouroboros.governance.budget_repl import (  # noqa: E501
            dispatch_budget_command,
        )
        res = dispatch_budget_command(
            "/budget op", tracker=EpistemicBudgetTracker(),
        )
        assert res.ok is False
        assert "missing op_id" in res.text

    def test_config_renders_env_knobs(self, monkeypatch):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.budget_repl import (  # noqa: E501
            dispatch_budget_command,
        )
        res = dispatch_budget_command("/budget config")
        assert res.ok is True
        assert "max_rounds" in res.text
        assert "probe_call_cap" in res.text

    def test_unknown_subcommand(self, monkeypatch):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.budget_repl import (  # noqa: E501
            dispatch_budget_command,
        )
        res = dispatch_budget_command("/budget xyzzy")
        assert res.ok is False
        assert "unknown subcommand" in res.text

    def test_non_budget_line_doesnt_match(self, monkeypatch):
        from backend.core.ouroboros.governance.budget_repl import (  # noqa: E501
            dispatch_budget_command,
        )
        res = dispatch_budget_command("/posture status")
        assert res.matched is False

    def test_register_verbs(self):
        """help_dispatcher auto-discovery — must register
        exactly one verb."""
        from backend.core.ouroboros.governance.budget_repl import (  # noqa: E501
            register_verbs,
        )
        registry = MagicMock()
        n = register_verbs(registry)
        assert n == 1
        registry.register.assert_called_once()
        spec = registry.register.call_args[0][0]
        assert spec.name == "/budget"


# ---------------------------------------------------------------------------
# § 5 — Provider bridge + per-round callback + SSE
# ---------------------------------------------------------------------------


class TestProviderBridge:
    @pytest.mark.asyncio
    async def test_disabled_returns_none_callback(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_EPISTEMIC_BUDGET_ENABLED", "false",
        )
        from backend.core.ouroboros.governance.epistemic_budget_provider_bridge import (  # noqa: E501
            attach_to_provider_run,
        )
        cb = attach_to_provider_run(
            op_id="op-x", route="standard",
            risk_tier="safe_auto",
        )
        assert cb is None

    @pytest.mark.asyncio
    async def test_enabled_returns_callable(
        self, monkeypatch,
    ):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        from backend.core.ouroboros.governance.epistemic_budget_provider_bridge import (  # noqa: E501
            attach_to_provider_run,
        )
        cb = attach_to_provider_run(
            op_id="op-x", route="standard",
            risk_tier="safe_auto",
            tracker=EpistemicBudgetTracker(),
        )
        assert cb is not None
        assert callable(cb)

    @pytest.mark.asyncio
    async def test_callback_increments_rounds_and_returns_false(
        self, monkeypatch,
    ):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        from backend.core.ouroboros.governance.epistemic_budget_provider_bridge import (  # noqa: E501
            attach_to_provider_run,
        )
        t = EpistemicBudgetTracker()
        cb = attach_to_provider_run(
            op_id="op-x", route="standard",
            risk_tier="safe_auto", tracker=t,
        )
        result1 = await cb(0)
        result2 = await cb(1)
        assert result1 is False
        assert result2 is False
        # Tracker state advanced
        b = t.get("op-x")
        assert b.rounds_consumed == 2

    @pytest.mark.asyncio
    async def test_callback_breaks_on_converged(
        self, monkeypatch,
    ):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        from backend.core.ouroboros.governance.epistemic_budget_provider_bridge import (  # noqa: E501
            attach_to_provider_run,
        )
        t = EpistemicBudgetTracker()
        # Pre-mark probe as confirmed → next dispatch returns
        # CONVERGED → break_round_loop=True
        t.open(
            op_id="op-x", route="standard",
            risk_tier="safe_auto",
        )
        t.note_probe_completed("op-x", verdict="confirmed")
        cb = attach_to_provider_run(
            op_id="op-x", route="standard",
            risk_tier="safe_auto", tracker=t,
        )
        result = await cb(0)
        assert result is True

    @pytest.mark.asyncio
    async def test_callback_publishes_sse_on_significant(
        self, monkeypatch,
    ):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        from backend.core.ouroboros.governance import (
            epistemic_budget_provider_bridge as bridge,
        )
        # Mock the publisher
        publish_mock = MagicMock()
        monkeypatch.setattr(
            bridge, "publish_budget_action_event",
            publish_mock,
        )
        t = EpistemicBudgetTracker()
        t.open(
            op_id="op-x", route="standard",
            risk_tier="safe_auto",
        )
        t.note_probe_completed("op-x", verdict="confirmed")
        cb = bridge.attach_to_provider_run(
            op_id="op-x", route="standard",
            risk_tier="safe_auto", tracker=t,
        )
        await cb(0)
        # CONVERGED is significant → publish called
        assert publish_mock.called
        kwargs = publish_mock.call_args.kwargs
        assert kwargs["outcome"] == "converged"
        assert kwargs["op_id"] == "op-x"

    @pytest.mark.asyncio
    async def test_callback_skips_sse_on_within_budget(
        self, monkeypatch,
    ):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        from backend.core.ouroboros.governance import (
            epistemic_budget_provider_bridge as bridge,
        )
        publish_mock = MagicMock()
        monkeypatch.setattr(
            bridge, "publish_budget_action_event",
            publish_mock,
        )
        t = EpistemicBudgetTracker()
        cb = bridge.attach_to_provider_run(
            op_id="op-x", route="standard",
            risk_tier="safe_auto", tracker=t,
        )
        await cb(0)
        # WITHIN_BUDGET is no-op → no publish
        assert not publish_mock.called

    @pytest.mark.asyncio
    async def test_close_op_drops_tracker_entry(
        self, monkeypatch,
    ):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        from backend.core.ouroboros.governance.epistemic_budget_provider_bridge import (  # noqa: E501
            attach_to_provider_run, close_op,
        )
        t = EpistemicBudgetTracker()
        attach_to_provider_run(
            op_id="op-x", route="standard",
            risk_tier="safe_auto", tracker=t,
        )
        assert t.get("op-x") is not None
        close_op(op_id="op-x", tracker=t)
        assert t.get("op-x") is None

    @pytest.mark.asyncio
    async def test_callback_with_probe_runner(
        self, monkeypatch,
    ):
        """End-to-end: confidence drop in trajectory → bridge
        invokes probe runner → tracker records verdict."""
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        from backend.core.ouroboros.governance.epistemic_budget_provider_bridge import (  # noqa: E501
            attach_to_provider_run,
        )
        t = EpistemicBudgetTracker()
        t.open(
            op_id="op-x", route="standard",
            risk_tier="safe_auto",
        )
        t.note_round_complete("op-x", confidence=0.9)
        t.note_round_complete("op-x", confidence=0.5)
        probe = AsyncMock(return_value="confirmed")
        probe_runner = MagicMock()
        probe_runner.run = probe
        cb = attach_to_provider_run(
            op_id="op-x", route="standard",
            risk_tier="safe_auto", tracker=t,
            probe_runner=probe_runner,
        )
        await cb(2)
        # Probe was invoked
        assert probe.called


# ---------------------------------------------------------------------------
# § 6 — Authority floor
# ---------------------------------------------------------------------------


class TestAuthorityInvariants:
    _FORBIDDEN = (
        "from backend.core.ouroboros.governance.orchestrator",
        "from backend.core.ouroboros.governance.iron_gate",
        "from backend.core.ouroboros.governance.candidate_generator",
        "from backend.core.ouroboros.governance.providers",
        "from backend.core.ouroboros.governance.urgency_router",
        "from backend.core.ouroboros.governance.semantic_guardian",
        "from backend.core.ouroboros.governance.tool_executor",
        "from backend.core.ouroboros.governance.change_engine",
        "from backend.core.ouroboros.governance.subagent_scheduler",
        "from backend.core.ouroboros.governance.policy",
        "from backend.core.ouroboros.governance.auto_action_router",
    )

    def _read_module(self, name: str) -> str:
        return (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / f"{name}.py"
        ).read_text(encoding="utf-8")

    def test_observability_module_floor(self):
        source = self._read_module(
            "epistemic_budget_observability",
        )
        for forbidden in self._FORBIDDEN:
            assert forbidden not in source, forbidden
        # Observability is read-only — must NOT import the
        # executor-hook (that's the authority side)
        assert (
            "from backend.core.ouroboros.governance."
            "epistemic_budget_executor_hook"
            not in source
        )

    def test_repl_module_floor(self):
        source = self._read_module("budget_repl")
        for forbidden in self._FORBIDDEN:
            assert forbidden not in source, forbidden
        assert (
            "from backend.core.ouroboros.governance."
            "epistemic_budget_executor_hook"
            not in source
        )

    def test_provider_bridge_module_floor(self):
        """Provider bridge is the authority-touching adapter —
        MAY import executor-hook. Must NOT import providers /
        orchestrator (that would be circular from the call
        site)."""
        source = self._read_module(
            "epistemic_budget_provider_bridge",
        )
        for forbidden in self._FORBIDDEN:
            assert forbidden not in source, forbidden
