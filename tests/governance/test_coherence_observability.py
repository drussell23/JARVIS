"""Slice 5b B — Coherence Observability tests.

Mirrors ``tests/governance/test_confidence_probe_graduation.py`` /
``tests/governance/test_invariant_drift_graduation.py`` for the
register_*_routes idiom: isolated handler tests + the structural
event_channel mount pin.

Authority invariants (this test file imports stdlib + pytest +
aiohttp + the verification.coherence_* modules ONLY) — pinned by
``test_authority_invariants_via_module_imports``."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest


# ---------------------------------------------------------------------------
# § 1 — register_coherence_routes mounts five endpoints
# ---------------------------------------------------------------------------


class TestObservabilityRoutes:
    def test_register_routes_mounts_five_endpoints(self):
        from aiohttp import web
        from backend.core.ouroboros.governance.verification.coherence_observability import (  # noqa: E501
            register_coherence_routes,
        )
        app = web.Application()
        register_coherence_routes(app)
        paths = {
            r.url_for().path
            for resource in app.router.resources()
            for r in resource
        }
        assert "/observability/coherence" in paths
        assert "/observability/coherence/config" in paths
        assert "/observability/coherence/audits" in paths
        assert "/observability/coherence/advisories" in paths
        assert "/observability/coherence/stats" in paths

    def test_routes_safe_to_mount_with_master_off(
        self, monkeypatch,
    ):
        """Mount must succeed even when the master flag is off —
        per-request _gate() does the live-toggle check."""
        monkeypatch.setenv(
            "JARVIS_COHERENCE_AUDITOR_ENABLED", "false",
        )
        from aiohttp import web
        from backend.core.ouroboros.governance.verification.coherence_observability import (  # noqa: E501
            register_coherence_routes,
        )
        app = web.Application()
        register_coherence_routes(app)


# ---------------------------------------------------------------------------
# § 2 — Per-handler 503-when-disabled / 200-when-enabled contract
# ---------------------------------------------------------------------------


class TestHandlerGate:
    @pytest.mark.asyncio
    async def test_overview_returns_503_when_master_off(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_COHERENCE_AUDITOR_ENABLED", "false",
        )
        from backend.core.ouroboros.governance.verification.coherence_observability import (  # noqa: E501
            _CoherenceRoutesHandler,
        )
        handler = _CoherenceRoutesHandler()
        request = SimpleNamespace(query={})
        response = await handler.handle_overview(request)
        assert response.status == 503

    @pytest.mark.asyncio
    async def test_config_returns_503_when_master_off(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_COHERENCE_AUDITOR_ENABLED", "false",
        )
        from backend.core.ouroboros.governance.verification.coherence_observability import (  # noqa: E501
            _CoherenceRoutesHandler,
        )
        handler = _CoherenceRoutesHandler()
        request = SimpleNamespace(query={})
        response = await handler.handle_config(request)
        assert response.status == 503

    @pytest.mark.asyncio
    async def test_audits_returns_503_when_master_off(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_COHERENCE_AUDITOR_ENABLED", "false",
        )
        from backend.core.ouroboros.governance.verification.coherence_observability import (  # noqa: E501
            _CoherenceRoutesHandler,
        )
        handler = _CoherenceRoutesHandler()
        request = SimpleNamespace(query={})
        response = await handler.handle_audits(request)
        assert response.status == 503

    @pytest.mark.asyncio
    async def test_advisories_returns_503_when_master_off(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_COHERENCE_AUDITOR_ENABLED", "false",
        )
        from backend.core.ouroboros.governance.verification.coherence_observability import (  # noqa: E501
            _CoherenceRoutesHandler,
        )
        handler = _CoherenceRoutesHandler()
        request = SimpleNamespace(query={})
        response = await handler.handle_advisories(request)
        assert response.status == 503

    @pytest.mark.asyncio
    async def test_stats_returns_503_when_master_off(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_COHERENCE_AUDITOR_ENABLED", "false",
        )
        from backend.core.ouroboros.governance.verification.coherence_observability import (  # noqa: E501
            _CoherenceRoutesHandler,
        )
        handler = _CoherenceRoutesHandler()
        request = SimpleNamespace(query={})
        response = await handler.handle_stats(request)
        assert response.status == 503

    @pytest.mark.asyncio
    async def test_overview_returns_200_when_master_on(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_COHERENCE_AUDITOR_ENABLED", "true",
        )
        from backend.core.ouroboros.governance.verification.coherence_observability import (  # noqa: E501
            _CoherenceRoutesHandler,
        )
        handler = _CoherenceRoutesHandler()
        request = SimpleNamespace(query={})
        response = await handler.handle_overview(request)
        assert response.status == 200


# ---------------------------------------------------------------------------
# § 3 — Overview payload shape: schemas, flags, budget, cadence,
# advisory, observer_snapshot, sse_event_type, drift_kinds
# ---------------------------------------------------------------------------


class TestOverviewPayload:
    @pytest.mark.asyncio
    async def test_overview_payload_shape(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_COHERENCE_AUDITOR_ENABLED", "true",
        )
        from backend.core.ouroboros.governance.verification.coherence_observability import (  # noqa: E501
            _CoherenceRoutesHandler,
        )
        handler = _CoherenceRoutesHandler()
        request = SimpleNamespace(query={})
        response = await handler.handle_overview(request)
        body = json.loads(response.body)
        assert "schemas" in body
        assert "auditor" in body["schemas"]
        assert "observer" in body["schemas"]
        assert "window_store" in body["schemas"]
        assert "action_bridge" in body["schemas"]
        assert "flags" in body
        assert body["flags"]["auditor_enabled"] is True
        assert "budget" in body
        assert "cadence" in body
        assert "window" in body
        assert "advisory" in body
        assert "observer_snapshot" in body
        assert "sse_event_type" in body
        assert "drift_kinds" in body
        # Drift-kind enum surfaced — should be exactly the closed
        # 6-value enum from coherence_auditor.BehavioralDriftKind.
        assert len(body["drift_kinds"]) == 6


# ---------------------------------------------------------------------------
# § 4 — Query-param parsing: limit clamping, since_ts floor,
# drift_kind enum match
# ---------------------------------------------------------------------------


class TestQueryParamParsing:
    def test_limit_default(self):
        from backend.core.ouroboros.governance.verification.coherence_observability import (  # noqa: E501
            _parse_limit,
        )
        request = SimpleNamespace(query={})
        assert _parse_limit(request) == 50

    def test_limit_clamps_to_max(self):
        from backend.core.ouroboros.governance.verification.coherence_observability import (  # noqa: E501
            _parse_limit,
        )
        request = SimpleNamespace(query={"limit": "999999"})
        assert _parse_limit(request) == 1000

    def test_limit_clamps_to_min(self):
        from backend.core.ouroboros.governance.verification.coherence_observability import (  # noqa: E501
            _parse_limit,
        )
        request = SimpleNamespace(query={"limit": "0"})
        assert _parse_limit(request) == 1

    def test_limit_garbage_yields_default(self):
        from backend.core.ouroboros.governance.verification.coherence_observability import (  # noqa: E501
            _parse_limit,
        )
        request = SimpleNamespace(query={"limit": "abc"})
        assert _parse_limit(request) == 50

    def test_since_ts_default(self):
        from backend.core.ouroboros.governance.verification.coherence_observability import (  # noqa: E501
            _parse_since_ts,
        )
        request = SimpleNamespace(query={})
        assert _parse_since_ts(request) == 0.0

    def test_since_ts_negative_floors_to_zero(self):
        from backend.core.ouroboros.governance.verification.coherence_observability import (  # noqa: E501
            _parse_since_ts,
        )
        request = SimpleNamespace(query={"since_ts": "-100"})
        assert _parse_since_ts(request) == 0.0

    def test_since_ts_garbage_yields_zero(self):
        from backend.core.ouroboros.governance.verification.coherence_observability import (  # noqa: E501
            _parse_since_ts,
        )
        request = SimpleNamespace(query={"since_ts": "xyz"})
        assert _parse_since_ts(request) == 0.0

    def test_drift_kind_unknown_yields_none(self):
        from backend.core.ouroboros.governance.verification.coherence_observability import (  # noqa: E501
            _parse_drift_kind,
        )
        request = SimpleNamespace(query={"drift_kind": "no_such"})
        assert _parse_drift_kind(request) is None

    def test_drift_kind_case_insensitive_match(self):
        from backend.core.ouroboros.governance.verification.coherence_auditor import (  # noqa: E501
            BehavioralDriftKind,
        )
        from backend.core.ouroboros.governance.verification.coherence_observability import (  # noqa: E501
            _parse_drift_kind,
        )
        # Probe the actual closed-enum vocabulary — never hardcode
        # the literal strings (defensive against future enum
        # additions).
        first_kind = next(iter(BehavioralDriftKind))
        upper_token = first_kind.value.upper()
        request = SimpleNamespace(query={"drift_kind": upper_token})
        assert _parse_drift_kind(request) is first_kind


# ---------------------------------------------------------------------------
# § 5 — Audits + advisories endpoints return 200 with empty arrays
# when no history exists yet
# ---------------------------------------------------------------------------


class TestEmptyHistoryEndpoints:
    @pytest.mark.asyncio
    async def test_audits_empty_history_200(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv(
            "JARVIS_COHERENCE_AUDITOR_ENABLED", "true",
        )
        # Redirect the audit JSONL to an empty tmp file so the
        # reader returns READ_EMPTY cleanly.
        monkeypatch.setenv(
            "JARVIS_COHERENCE_BASE_DIR", str(tmp_path),
        )
        from backend.core.ouroboros.governance.verification.coherence_observability import (  # noqa: E501
            _CoherenceRoutesHandler,
        )
        handler = _CoherenceRoutesHandler()
        request = SimpleNamespace(query={})
        response = await handler.handle_audits(request)
        assert response.status == 200
        body = json.loads(response.body)
        assert body["count"] == 0
        assert body["verdicts"] == []

    @pytest.mark.asyncio
    async def test_advisories_empty_history_200(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv(
            "JARVIS_COHERENCE_AUDITOR_ENABLED", "true",
        )
        monkeypatch.setenv(
            "JARVIS_COHERENCE_ADVISORY_PATH",
            str(tmp_path / "advisories.jsonl"),
        )
        from backend.core.ouroboros.governance.verification.coherence_observability import (  # noqa: E501
            _CoherenceRoutesHandler,
        )
        handler = _CoherenceRoutesHandler()
        request = SimpleNamespace(query={})
        response = await handler.handle_advisories(request)
        assert response.status == 200
        body = json.loads(response.body)
        assert body["count"] == 0
        assert body["advisories"] == []


# ---------------------------------------------------------------------------
# § 6 — Stats payload exposes observer counter snapshot keys
# ---------------------------------------------------------------------------


class TestStatsPayload:
    @pytest.mark.asyncio
    async def test_stats_exposes_observer_snapshot_keys(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_COHERENCE_AUDITOR_ENABLED", "true",
        )
        from backend.core.ouroboros.governance.verification.coherence_observability import (  # noqa: E501
            _CoherenceRoutesHandler,
        )
        handler = _CoherenceRoutesHandler()
        request = SimpleNamespace(query={})
        response = await handler.handle_stats(request)
        assert response.status == 200
        body = json.loads(response.body)
        snap = body.get("observer_snapshot", {})
        # CoherenceObserver.snapshot() exposes these counters per
        # its docstring contract — pin the keys so a refactor that
        # renames them trips this test.
        assert "cycles_total" in snap
        assert "cycles_coherent" in snap
        assert "cycles_drift_emitted" in snap
        assert "consecutive_failures" in snap


# ---------------------------------------------------------------------------
# § 7 — Authority invariants — module imports stdlib + aiohttp +
# verification.coherence_* ONLY
# ---------------------------------------------------------------------------


class TestAuthorityInvariants:
    def test_authority_invariants_via_module_imports(self):
        """Read the module source and assert it does not import
        any orchestrator / policy / iron-gate / generator surface.
        Mirrors the equivalent pin on
        confidence_probe_observability."""
        path = (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "verification" / "coherence_observability.py"
        )
        source = path.read_text(encoding="utf-8")
        forbidden = [
            "from backend.core.ouroboros.governance.orchestrator",
            "from backend.core.ouroboros.governance.iron_gate",
            "from backend.core.ouroboros.governance.candidate_generator",
            "from backend.core.ouroboros.governance.providers",
            "from backend.core.ouroboros.governance.doubleword_provider",
            "from backend.core.ouroboros.governance.urgency_router",
            "from backend.core.ouroboros.governance.semantic_guardian",
            "from backend.core.ouroboros.governance.semantic_firewall",
            "from backend.core.ouroboros.governance.tool_executor",
            "from backend.core.ouroboros.governance.change_engine",
            "from backend.core.ouroboros.governance.subagent_scheduler",
            "from backend.core.ouroboros.governance.auto_action_router",
            "from backend.core.ouroboros.governance.policy",
        ]
        for module_path in forbidden:
            assert module_path not in source, (
                f"coherence_observability must NOT import "
                f"{module_path} — read-only authority invariant"
            )


# ---------------------------------------------------------------------------
# § 8 — event_channel mount pin (Slice 5b B)
# ---------------------------------------------------------------------------


class TestEventChannelMount:
    def test_event_channel_imports_coherence_module(self):
        """Slice 5b B — pin the event_channel mount so a future
        refactor cannot silently drop the wiring. Mirrors the
        Move 4 / Move 5 pattern in
        test_invariant_drift_graduation.py and
        test_confidence_probe_graduation.py."""
        path = (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "event_channel.py"
        )
        source = path.read_text(encoding="utf-8")
        assert (
            "register_coherence_routes" in source
        ), (
            "event_channel must mount the coherence GET routes "
            "(Slice 5b B)"
        )
        assert (
            "Priority #1 Slice 5b" in source
        ), (
            "event_channel must mark the wiring with the slice "
            "comment for traceability"
        )
