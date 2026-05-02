"""Time-Travel Debugging Slice 1 — IDE GET routes for the CausalityDAG.

Activates the IDE-side consumer for the navigable session graph
(Causality DAG primitive shipped by Priority #2; GET surface was
structurally orphaned until this slice). Both routes delegate to
``verification.dag_navigation`` handlers (the substrate) which
already check JARVIS_DAG_NAVIGATION_GET_ENABLED + dag_query_enabled()
and NEVER raise.

Coverage:
  * Route registration (both routes mount on the IDEObservabilityRouter)
  * IDE umbrella flag gates the routes (403 on disabled)
  * Rate limit applies (429)
  * URL boundary regex validation (400 on malformed session_id/record_id)
  * Substrate reason_code → HTTP status mapping (5 codes)
  * Successful delegation (substrate returns result → 200 JSON)
  * Substrate raise (defensive fallback → 500 with sentinel reason)
  * Schema-version stamped on every response
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from backend.core.ouroboros.governance.ide_observability import (
    IDE_OBSERVABILITY_SCHEMA_VERSION,
    IDEObservabilityRouter,
    ide_observability_enabled,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(
    path: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    match_info: Optional[Dict[str, str]] = None,
    remote: str = "127.0.0.1",
) -> web.Request:
    headers = headers or {}
    req = make_mocked_request("GET", path, headers=headers)
    if match_info:
        req.match_info.update(match_info)
    req._transport_peername = (remote, 0)  # type: ignore[attr-defined]
    return req


@pytest.fixture(autouse=True)
def _enable_ide_observability(monkeypatch):
    """Most tests assume the umbrella flag is on; tests that
    specifically test the disabled path override this."""
    monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "true")


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


class TestRouteRegistration:
    def test_both_dag_routes_register(self):
        app = web.Application()
        router = IDEObservabilityRouter()
        router.register_routes(app)
        registered_paths = [
            getattr(r, "resource", None) and r.resource.canonical
            for r in app.router.routes()
        ]
        # Both new routes present.
        assert "/observability/dag/{session_id}" in registered_paths
        assert (
            "/observability/dag/{session_id}/{record_id}"
            in registered_paths
        )


# ---------------------------------------------------------------------------
# Umbrella flag + rate limit
# ---------------------------------------------------------------------------


class TestUmbrellaFlagAndRateLimit:
    @pytest.mark.asyncio
    async def test_session_route_returns_403_when_umbrella_disabled(
        self, monkeypatch,
    ):
        monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "false")
        router = IDEObservabilityRouter()
        req = _make_request(
            "/observability/dag/sess-1",
            match_info={"session_id": "sess-1"},
        )
        resp = await router._handle_dag_session(req)
        assert resp.status == 403
        body = json.loads(resp.body.decode("utf-8"))
        assert body["reason_code"] == "ide_observability.disabled"

    @pytest.mark.asyncio
    async def test_record_route_returns_403_when_umbrella_disabled(
        self, monkeypatch,
    ):
        monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "false")
        router = IDEObservabilityRouter()
        req = _make_request(
            "/observability/dag/sess-1/rec-1",
            match_info={"session_id": "sess-1", "record_id": "rec-1"},
        )
        resp = await router._handle_dag_record(req)
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_session_route_rate_limited_returns_429(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_IDE_OBSERVABILITY_RATE_LIMIT_PER_MIN", "2",
        )
        router = IDEObservabilityRouter()
        req = _make_request(
            "/observability/dag/sess-1",
            match_info={"session_id": "sess-1"},
        )
        # Burn 2 then 3rd is throttled.
        for _ in range(2):
            await router._handle_dag_session(req)
        resp = await router._handle_dag_session(req)
        assert resp.status == 429
        body = json.loads(resp.body.decode("utf-8"))
        assert body["reason_code"] == "ide_observability.rate_limited"


# ---------------------------------------------------------------------------
# URL boundary validation
# ---------------------------------------------------------------------------


class TestURLValidation:
    @pytest.mark.asyncio
    async def test_session_route_invalid_session_id_returns_400(self):
        router = IDEObservabilityRouter()
        req = _make_request(
            "/observability/dag/has spaces",
            match_info={"session_id": "has spaces"},
        )
        resp = await router._handle_dag_session(req)
        assert resp.status == 400
        body = json.loads(resp.body.decode("utf-8"))
        assert body["reason_code"] == (
            "ide_observability.invalid_session_id"
        )

    @pytest.mark.asyncio
    async def test_record_route_invalid_session_id_returns_400(self):
        router = IDEObservabilityRouter()
        req = _make_request(
            "/observability/dag/has spaces/rec-1",
            match_info={"session_id": "has spaces", "record_id": "rec-1"},
        )
        resp = await router._handle_dag_record(req)
        assert resp.status == 400
        body = json.loads(resp.body.decode("utf-8"))
        assert body["reason_code"] == (
            "ide_observability.invalid_session_id"
        )

    @pytest.mark.asyncio
    async def test_record_route_invalid_record_id_returns_400(self):
        router = IDEObservabilityRouter()
        req = _make_request(
            "/observability/dag/sess-1/has spaces",
            match_info={"session_id": "sess-1", "record_id": "has spaces"},
        )
        resp = await router._handle_dag_record(req)
        assert resp.status == 400
        body = json.loads(resp.body.decode("utf-8"))
        assert body["reason_code"] == (
            "ide_observability.invalid_record_id"
        )

    @pytest.mark.asyncio
    async def test_record_id_accepts_long_composite_id(self):
        """Phase-capture composite ids include phase + ordinal +
        ulid segments — RECORD_ID regex must accept lengths up to
        256."""
        router = IDEObservabilityRouter()
        long_id = "a" * 250
        req = _make_request(
            f"/observability/dag/sess-1/{long_id}",
            match_info={"session_id": "sess-1", "record_id": long_id},
        )
        with patch(
            "backend.core.ouroboros.governance.verification."
            "dag_navigation.handle_dag_record",
            return_value={
                "error": True,
                "reason_code": "dag_navigation.not_found",
            },
        ):
            resp = await router._handle_dag_record(req)
        # 400 would mean the regex failed; we expect 404 from substrate.
        assert resp.status == 404


# ---------------------------------------------------------------------------
# Reason-code → HTTP status mapping
# ---------------------------------------------------------------------------


class TestReasonCodeMapping:
    def _router(self) -> IDEObservabilityRouter:
        return IDEObservabilityRouter()

    def test_dag_status_for_reason_known_codes(self):
        r = self._router()
        assert r._dag_status_for_reason("dag_navigation.disabled") == 403
        assert r._dag_status_for_reason("dag_query.disabled") == 403
        assert r._dag_status_for_reason("dag_navigation.not_found") == 404
        assert r._dag_status_for_reason("dag_navigation.error") == 500

    def test_dag_status_for_reason_unknown_defaults_to_500(self):
        r = self._router()
        # Defensive — unexpected substrate codes surface as 500
        # rather than silently masking as 200.
        assert r._dag_status_for_reason("totally.unknown") == 500
        assert r._dag_status_for_reason("") == 500
        assert r._dag_status_for_reason(None) == 500  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_session_route_substrate_disabled_returns_403(self):
        router = IDEObservabilityRouter()
        req = _make_request(
            "/observability/dag/sess-1",
            match_info={"session_id": "sess-1"},
        )
        with patch(
            "backend.core.ouroboros.governance.verification."
            "dag_navigation.handle_dag_session",
            return_value={
                "error": True,
                "reason_code": "dag_navigation.disabled",
            },
        ):
            resp = await router._handle_dag_session(req)
        assert resp.status == 403
        body = json.loads(resp.body.decode("utf-8"))
        assert body["reason_code"] == "dag_navigation.disabled"

    @pytest.mark.asyncio
    async def test_session_route_substrate_dag_query_disabled_returns_403(
        self,
    ):
        router = IDEObservabilityRouter()
        req = _make_request(
            "/observability/dag/sess-1",
            match_info={"session_id": "sess-1"},
        )
        with patch(
            "backend.core.ouroboros.governance.verification."
            "dag_navigation.handle_dag_session",
            return_value={
                "error": True,
                "reason_code": "dag_query.disabled",
            },
        ):
            resp = await router._handle_dag_session(req)
        assert resp.status == 403
        body = json.loads(resp.body.decode("utf-8"))
        assert body["reason_code"] == "dag_query.disabled"

    @pytest.mark.asyncio
    async def test_record_route_substrate_not_found_returns_404(self):
        router = IDEObservabilityRouter()
        req = _make_request(
            "/observability/dag/sess-1/missing-rec",
            match_info={"session_id": "sess-1", "record_id": "missing-rec"},
        )
        with patch(
            "backend.core.ouroboros.governance.verification."
            "dag_navigation.handle_dag_record",
            return_value={
                "error": True,
                "reason_code": "dag_navigation.not_found",
            },
        ):
            resp = await router._handle_dag_record(req)
        assert resp.status == 404
        body = json.loads(resp.body.decode("utf-8"))
        assert body["reason_code"] == "dag_navigation.not_found"

    @pytest.mark.asyncio
    async def test_record_route_substrate_error_returns_500(self):
        router = IDEObservabilityRouter()
        req = _make_request(
            "/observability/dag/sess-1/rec-1",
            match_info={"session_id": "sess-1", "record_id": "rec-1"},
        )
        with patch(
            "backend.core.ouroboros.governance.verification."
            "dag_navigation.handle_dag_record",
            return_value={
                "error": True,
                "reason_code": "dag_navigation.error",
            },
        ):
            resp = await router._handle_dag_record(req)
        assert resp.status == 500
        body = json.loads(resp.body.decode("utf-8"))
        assert body["reason_code"] == "dag_navigation.error"


# ---------------------------------------------------------------------------
# Successful delegation
# ---------------------------------------------------------------------------


class TestSuccessfulDelegation:
    @pytest.mark.asyncio
    async def test_session_route_returns_200_on_success(self):
        router = IDEObservabilityRouter()
        req = _make_request(
            "/observability/dag/sess-1",
            match_info={"session_id": "sess-1"},
        )
        with patch(
            "backend.core.ouroboros.governance.verification."
            "dag_navigation.handle_dag_session",
            return_value={
                "session_id": "sess-1",
                "node_count": 12,
                "edge_count": 18,
                "record_ids": ["a", "b", "c"],
            },
        ):
            resp = await router._handle_dag_session(req)
        assert resp.status == 200
        body = json.loads(resp.body.decode("utf-8"))
        assert body["session_id"] == "sess-1"
        assert body["node_count"] == 12
        assert body["edge_count"] == 18
        assert body["record_ids"] == ["a", "b", "c"]
        assert body["schema_version"] == IDE_OBSERVABILITY_SCHEMA_VERSION

    @pytest.mark.asyncio
    async def test_record_route_returns_200_on_success(self):
        router = IDEObservabilityRouter()
        req = _make_request(
            "/observability/dag/sess-1/rec-1",
            match_info={"session_id": "sess-1", "record_id": "rec-1"},
        )
        full_record = {
            "record_id": "rec-1",
            "phase": "GENERATE",
            "session_id": "sess-1",
            "ordinal": 5,
        }
        with patch(
            "backend.core.ouroboros.governance.verification."
            "dag_navigation.handle_dag_record",
            return_value={
                "record_id": "rec-1",
                "record": full_record,
                "parents": ["rec-0"],
                "children": ["rec-2", "rec-3"],
                "counterfactual_branches": [],
                "subgraph_node_count": 4,
            },
        ):
            resp = await router._handle_dag_record(req)
        assert resp.status == 200
        body = json.loads(resp.body.decode("utf-8"))
        assert body["record_id"] == "rec-1"
        assert body["record"] == full_record
        assert body["parents"] == ["rec-0"]
        assert body["children"] == ["rec-2", "rec-3"]
        assert body["counterfactual_branches"] == []
        assert body["subgraph_node_count"] == 4
        assert body["schema_version"] == IDE_OBSERVABILITY_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Defensive degradation
# ---------------------------------------------------------------------------


class TestDefensiveDegradation:
    @pytest.mark.asyncio
    async def test_session_route_substrate_raise_returns_500(self):
        """If the substrate import or call raises (despite its
        NEVER-raise contract), the router catches and returns 500
        with a sentinel reason — the IDE never sees an unhandled
        exception."""
        router = IDEObservabilityRouter()
        req = _make_request(
            "/observability/dag/sess-1",
            match_info={"session_id": "sess-1"},
        )
        with patch(
            "backend.core.ouroboros.governance.verification."
            "dag_navigation.handle_dag_session",
            side_effect=RuntimeError("substrate boom"),
        ):
            resp = await router._handle_dag_session(req)
        assert resp.status == 500
        body = json.loads(resp.body.decode("utf-8"))
        assert body["reason_code"] == (
            "ide_observability.dag_session_error"
        )

    @pytest.mark.asyncio
    async def test_record_route_substrate_raise_returns_500(self):
        router = IDEObservabilityRouter()
        req = _make_request(
            "/observability/dag/sess-1/rec-1",
            match_info={"session_id": "sess-1", "record_id": "rec-1"},
        )
        with patch(
            "backend.core.ouroboros.governance.verification."
            "dag_navigation.handle_dag_record",
            side_effect=RuntimeError("substrate boom"),
        ):
            resp = await router._handle_dag_record(req)
        assert resp.status == 500
        body = json.loads(resp.body.decode("utf-8"))
        assert body["reason_code"] == (
            "ide_observability.dag_record_error"
        )


# ---------------------------------------------------------------------------
# Schema version
# ---------------------------------------------------------------------------


class TestSchemaVersion:
    @pytest.mark.asyncio
    async def test_session_response_carries_schema_version(self):
        router = IDEObservabilityRouter()
        req = _make_request(
            "/observability/dag/sess-1",
            match_info={"session_id": "sess-1"},
        )
        with patch(
            "backend.core.ouroboros.governance.verification."
            "dag_navigation.handle_dag_session",
            return_value={
                "session_id": "sess-1", "node_count": 0,
                "edge_count": 0, "record_ids": [],
            },
        ):
            resp = await router._handle_dag_session(req)
        body = json.loads(resp.body.decode("utf-8"))
        assert body["schema_version"] == IDE_OBSERVABILITY_SCHEMA_VERSION
