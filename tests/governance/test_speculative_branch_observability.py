"""Slice 5b D — SBT (speculative branch tree) observability tests.

Mirrors the Slice 5b A/B/C/D-CIGW test structure. Producer side
(SBTObserver + recorder + history reader + comparator) is already
graduated and tested by ``test_priority_4_speculative_branch_*``;
this file pins the new HTTP surface only.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest


# ---------------------------------------------------------------------------
# § 1 — register_sbt_routes mounts five endpoints
# ---------------------------------------------------------------------------


class TestObservabilityRoutes:
    def test_register_routes_mounts_five_endpoints(self):
        from aiohttp import web
        from backend.core.ouroboros.governance.verification.speculative_branch_observability import (  # noqa: E501
            register_sbt_routes,
        )
        app = web.Application()
        register_sbt_routes(app)
        paths = {
            r.url_for().path
            for resource in app.router.resources()
            for r in resource
        }
        assert "/observability/sbt" in paths
        assert "/observability/sbt/config" in paths
        assert "/observability/sbt/history" in paths
        assert "/observability/sbt/stats" in paths
        assert "/observability/sbt/outcomes" in paths

    def test_routes_safe_to_mount_with_master_off(
        self, monkeypatch,
    ):
        monkeypatch.setenv("JARVIS_SBT_ENABLED", "false")
        from aiohttp import web
        from backend.core.ouroboros.governance.verification.speculative_branch_observability import (  # noqa: E501
            register_sbt_routes,
        )
        app = web.Application()
        register_sbt_routes(app)


# ---------------------------------------------------------------------------
# § 2 — Per-handler 503/200 master-flag gate contract
# ---------------------------------------------------------------------------


class TestHandlerGate:
    @pytest.mark.asyncio
    async def test_overview_503_when_master_off(
        self, monkeypatch,
    ):
        monkeypatch.setenv("JARVIS_SBT_ENABLED", "false")
        from backend.core.ouroboros.governance.verification.speculative_branch_observability import (  # noqa: E501
            _SBTRoutesHandler,
        )
        h = _SBTRoutesHandler()
        response = await h.handle_overview(
            SimpleNamespace(query={}),
        )
        assert response.status == 503

    @pytest.mark.asyncio
    async def test_history_503_when_master_off(
        self, monkeypatch,
    ):
        monkeypatch.setenv("JARVIS_SBT_ENABLED", "false")
        from backend.core.ouroboros.governance.verification.speculative_branch_observability import (  # noqa: E501
            _SBTRoutesHandler,
        )
        h = _SBTRoutesHandler()
        response = await h.handle_history(
            SimpleNamespace(query={}),
        )
        assert response.status == 503

    @pytest.mark.asyncio
    async def test_stats_503_when_master_off(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SBT_ENABLED", "false")
        from backend.core.ouroboros.governance.verification.speculative_branch_observability import (  # noqa: E501
            _SBTRoutesHandler,
        )
        h = _SBTRoutesHandler()
        response = await h.handle_stats(
            SimpleNamespace(query={}),
        )
        assert response.status == 503

    @pytest.mark.asyncio
    async def test_outcomes_503_when_master_off(
        self, monkeypatch,
    ):
        monkeypatch.setenv("JARVIS_SBT_ENABLED", "false")
        from backend.core.ouroboros.governance.verification.speculative_branch_observability import (  # noqa: E501
            _SBTRoutesHandler,
        )
        h = _SBTRoutesHandler()
        response = await h.handle_outcomes(
            SimpleNamespace(query={}),
        )
        assert response.status == 503

    @pytest.mark.asyncio
    async def test_overview_200_when_master_on(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SBT_ENABLED", "true")
        from backend.core.ouroboros.governance.verification.speculative_branch_observability import (  # noqa: E501
            _SBTRoutesHandler,
        )
        h = _SBTRoutesHandler()
        response = await h.handle_overview(
            SimpleNamespace(query={}),
        )
        assert response.status == 200


# ---------------------------------------------------------------------------
# § 3 — Overview payload shape
# ---------------------------------------------------------------------------


class TestOverviewPayload:
    @pytest.mark.asyncio
    async def test_overview_payload_shape(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SBT_ENABLED", "true")
        from backend.core.ouroboros.governance.verification.speculative_branch_observability import (  # noqa: E501
            _SBTRoutesHandler,
        )
        h = _SBTRoutesHandler()
        response = await h.handle_overview(
            SimpleNamespace(query={}),
        )
        body = json.loads(response.body)
        assert "schemas" in body
        assert "primitive" in body["schemas"]
        assert "runner" in body["schemas"]
        assert "comparator" in body["schemas"]
        assert "observer" in body["schemas"]
        assert "flags" in body
        assert body["flags"]["sbt_enabled"] is True
        assert "observer_config" in body
        assert "history_size" in body
        assert "recent_stats" in body
        assert "sse_event_types" in body
        assert "tree_complete" in body["sse_event_types"]
        assert "baseline_updated" in body["sse_event_types"]
        assert "verdict_kinds" in body
        assert "baseline_qualities" in body

    @pytest.mark.asyncio
    async def test_enum_sizes_match_dynamically(self, monkeypatch):
        """Drift-safe: probe the closed enums + assert overview
        surface size matches."""
        monkeypatch.setenv("JARVIS_SBT_ENABLED", "true")
        from backend.core.ouroboros.governance.verification.speculative_branch import (  # noqa: E501
            TreeVerdict,
        )
        from backend.core.ouroboros.governance.verification.speculative_branch_comparator import (  # noqa: E501
            SBTBaselineQuality,
        )
        from backend.core.ouroboros.governance.verification.speculative_branch_observability import (  # noqa: E501
            _SBTRoutesHandler,
        )
        h = _SBTRoutesHandler()
        response = await h.handle_overview(
            SimpleNamespace(query={}),
        )
        body = json.loads(response.body)
        assert (
            len(body["verdict_kinds"]) == len(TreeVerdict)
        )
        assert (
            len(body["baseline_qualities"])
            == len(SBTBaselineQuality)
        )


# ---------------------------------------------------------------------------
# § 4 — Limit clamping
# ---------------------------------------------------------------------------


class TestQueryParamParsing:
    def test_limit_default(self):
        from backend.core.ouroboros.governance.verification.speculative_branch_observability import (  # noqa: E501
            _parse_limit,
        )
        assert _parse_limit(SimpleNamespace(query={})) == 50

    def test_limit_clamps_to_max(self):
        from backend.core.ouroboros.governance.verification.speculative_branch_observability import (  # noqa: E501
            _parse_limit,
        )
        assert _parse_limit(
            SimpleNamespace(query={"limit": "999999"}),
        ) == 1000

    def test_limit_clamps_to_min(self):
        from backend.core.ouroboros.governance.verification.speculative_branch_observability import (  # noqa: E501
            _parse_limit,
        )
        assert _parse_limit(
            SimpleNamespace(query={"limit": "0"}),
        ) == 1


# ---------------------------------------------------------------------------
# § 5 — Empty-history endpoints return 200
# ---------------------------------------------------------------------------


class TestEmptyHistoryEndpoints:
    @pytest.mark.asyncio
    async def test_history_empty_200(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv("JARVIS_SBT_ENABLED", "true")
        monkeypatch.setenv(
            "JARVIS_SBT_HISTORY_DIR", str(tmp_path),
        )
        from backend.core.ouroboros.governance.verification.speculative_branch_observability import (  # noqa: E501
            _SBTRoutesHandler,
        )
        h = _SBTRoutesHandler()
        response = await h.handle_history(
            SimpleNamespace(query={}),
        )
        assert response.status == 200
        body = json.loads(response.body)
        assert body["count"] == 0
        assert body["records"] == []


# ---------------------------------------------------------------------------
# § 6 — Authority invariants
# ---------------------------------------------------------------------------


class TestAuthorityInvariants:
    def test_observability_authority(self):
        path = (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "verification"
            / "speculative_branch_observability.py"
        )
        source = path.read_text(encoding="utf-8")
        forbidden = [
            "from backend.core.ouroboros.governance.orchestrator",
            "from backend.core.ouroboros.governance.iron_gate",
            "from backend.core.ouroboros.governance.candidate_generator",
            "from backend.core.ouroboros.governance.providers",
            "from backend.core.ouroboros.governance.urgency_router",
            "from backend.core.ouroboros.governance.semantic_guardian",
            "from backend.core.ouroboros.governance.tool_executor",
            "from backend.core.ouroboros.governance.change_engine",
            "from backend.core.ouroboros.governance.subagent_scheduler",
            "from backend.core.ouroboros.governance.auto_action_router",
            "from backend.core.ouroboros.governance.policy",
        ]
        for forbidden_path in forbidden:
            assert forbidden_path not in source, (
                f"speculative_branch_observability must NOT "
                f"import {forbidden_path}"
            )


# ---------------------------------------------------------------------------
# § 7 — event_channel mount pin
# ---------------------------------------------------------------------------


class TestEventChannelMount:
    def test_event_channel_imports_sbt_module(self):
        """Slice 5b D — pin the SBT mount."""
        path = (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "event_channel.py"
        )
        source = path.read_text(encoding="utf-8")
        assert "register_sbt_routes" in source, (
            "event_channel must mount the SBT GET routes"
        )
        assert "Priority #4 Slice 5b" in source, (
            "event_channel must mark the wiring with the slice "
            "comment for traceability"
        )
