"""Slice 3 regression spine — GET /observability/flags|verbs + SSE typo/registered.

Pins carried into Slice 4 graduation.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

import pytest

from backend.core.ouroboros.governance.flag_registry import (
    Category,
    FlagRegistry,
    FlagSpec,
    FlagType,
    Relevance,
    ensure_seeded,
    get_default_registry,
    reset_default_registry,
)
from backend.core.ouroboros.governance.help_dispatcher import (
    reset_default_verb_registry,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in list(os.environ):
        if (key.startswith("JARVIS_FLAG_REGISTRY") or
                key.startswith("JARVIS_HELP_DISPATCHER") or
                key.startswith("JARVIS_FLAG_TYPO") or
                key.startswith("JARVIS_IDE_")):
            monkeypatch.delenv(key, raising=False)
    reset_default_registry()
    reset_default_verb_registry()
    yield
    reset_default_registry()
    reset_default_verb_registry()


@pytest.fixture
def seeded_with_surface_enabled(monkeypatch) -> FlagRegistry:
    monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "true")
    monkeypatch.setenv("JARVIS_FLAG_REGISTRY_ENABLED", "true")
    return ensure_seeded()


def _make_request(
    query: Dict[str, str] = None,
    match_info: Dict[str, str] = None,
    origin: str = "http://localhost:1234",
):
    return SimpleNamespace(
        remote="127.0.0.1",
        headers={"Origin": origin},
        query=query or {},
        match_info=match_info or {},
    )


# ---------------------------------------------------------------------------
# GET /observability/flags
# ---------------------------------------------------------------------------


class TestFlagsList:

    @pytest.mark.asyncio
    async def test_403_when_ide_observability_off(self, monkeypatch):
        # Explicit false on ide_observability kills the surface even if
        # flag_registry master is on
        monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "false")
        monkeypatch.setenv("JARVIS_FLAG_REGISTRY_ENABLED", "true")
        from backend.core.ouroboros.governance.ide_observability import (
            IDEObservabilityRouter,
        )
        router = IDEObservabilityRouter()
        resp = await router._handle_flags_list(_make_request())
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_403_when_flag_registry_master_off(self, monkeypatch):
        monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "true")
        # master not set → default false Slice 1-3
        from backend.core.ouroboros.governance.ide_observability import (
            IDEObservabilityRouter,
        )
        router = IDEObservabilityRouter()
        resp = await router._handle_flags_list(_make_request())
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_200_returns_seeded_flags(self, seeded_with_surface_enabled):
        from backend.core.ouroboros.governance.ide_observability import (
            IDEObservabilityRouter,
        )
        router = IDEObservabilityRouter()
        resp = await router._handle_flags_list(_make_request())
        assert resp.status == 200
        payload = json.loads(resp.body)
        assert payload["schema_version"] == "1.0"
        assert payload["count"] >= 40
        assert any(
            f["name"] == "JARVIS_DIRECTION_INFERRER_ENABLED"
            for f in payload["flags"]
        )

    @pytest.mark.asyncio
    async def test_category_filter(self, seeded_with_surface_enabled):
        from backend.core.ouroboros.governance.ide_observability import (
            IDEObservabilityRouter,
        )
        router = IDEObservabilityRouter()
        resp = await router._handle_flags_list(
            _make_request(query={"category": "safety"}),
        )
        assert resp.status == 200
        payload = json.loads(resp.body)
        assert payload["count"] > 0
        assert all(f["category"] == "safety" for f in payload["flags"])

    @pytest.mark.asyncio
    async def test_malformed_category_returns_400(self, seeded_with_surface_enabled):
        from backend.core.ouroboros.governance.ide_observability import (
            IDEObservabilityRouter,
        )
        router = IDEObservabilityRouter()
        resp = await router._handle_flags_list(
            _make_request(query={"category": "made_up"}),
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_posture_filter(self, seeded_with_surface_enabled):
        from backend.core.ouroboros.governance.ide_observability import (
            IDEObservabilityRouter,
        )
        router = IDEObservabilityRouter()
        resp = await router._handle_flags_list(
            _make_request(query={"posture": "HARDEN"}),
        )
        assert resp.status == 200
        payload = json.loads(resp.body)
        # All returned flags must have HARDEN in posture_relevance
        for f in payload["flags"]:
            assert "HARDEN" in f["posture_relevance"]

    @pytest.mark.asyncio
    async def test_search_filter(self, seeded_with_surface_enabled):
        from backend.core.ouroboros.governance.ide_observability import (
            IDEObservabilityRouter,
        )
        router = IDEObservabilityRouter()
        resp = await router._handle_flags_list(
            _make_request(query={"search": "observer"}),
        )
        assert resp.status == 200
        payload = json.loads(resp.body)
        assert payload["count"] > 0
        for f in payload["flags"]:
            assert "observer" in f["name"].lower() or "observer" in f["description"].lower()

    @pytest.mark.asyncio
    async def test_limit_clamp(self, seeded_with_surface_enabled):
        from backend.core.ouroboros.governance.ide_observability import (
            IDEObservabilityRouter,
        )
        router = IDEObservabilityRouter()
        resp = await router._handle_flags_list(
            _make_request(query={"limit": "5"}),
        )
        payload = json.loads(resp.body)
        assert payload["count"] <= 5
        assert payload["limit"] == 5

    @pytest.mark.asyncio
    async def test_limit_malformed_returns_400(self, seeded_with_surface_enabled):
        from backend.core.ouroboros.governance.ide_observability import (
            IDEObservabilityRouter,
        )
        router = IDEObservabilityRouter()
        resp = await router._handle_flags_list(
            _make_request(query={"limit": "banana"}),
        )
        assert resp.status == 400


# ---------------------------------------------------------------------------
# GET /observability/flags/{name}
# ---------------------------------------------------------------------------


class TestFlagDetail:

    @pytest.mark.asyncio
    async def test_happy_path(self, seeded_with_surface_enabled):
        from backend.core.ouroboros.governance.ide_observability import (
            IDEObservabilityRouter,
        )
        router = IDEObservabilityRouter()
        resp = await router._handle_flag_detail(
            _make_request(match_info={"name": "JARVIS_DIRECTION_INFERRER_ENABLED"}),
        )
        assert resp.status == 200
        payload = json.loads(resp.body)
        assert payload["name"] == "JARVIS_DIRECTION_INFERRER_ENABLED"
        assert payload["type"] == "bool"
        assert payload["category"] == "safety"

    @pytest.mark.asyncio
    async def test_includes_current_env_value(
        self, seeded_with_surface_enabled, monkeypatch,
    ):
        monkeypatch.setenv("JARVIS_POSTURE_OBSERVER_INTERVAL_S", "600")
        from backend.core.ouroboros.governance.ide_observability import (
            IDEObservabilityRouter,
        )
        router = IDEObservabilityRouter()
        resp = await router._handle_flag_detail(
            _make_request(match_info={"name": "JARVIS_POSTURE_OBSERVER_INTERVAL_S"}),
        )
        payload = json.loads(resp.body)
        assert payload["current_env_value"] == "600"

    @pytest.mark.asyncio
    async def test_unknown_flag_404_with_suggestions(self, seeded_with_surface_enabled):
        from backend.core.ouroboros.governance.ide_observability import (
            IDEObservabilityRouter,
        )
        router = IDEObservabilityRouter()
        resp = await router._handle_flag_detail(
            _make_request(
                match_info={"name": "JARVIS_POSTURE_OBSERVR_INTERVAL_S"},
            ),
        )
        assert resp.status == 404
        payload = json.loads(resp.body)
        assert payload["reason_code"] == "flags.unknown"
        assert payload["suggestions"]

    @pytest.mark.asyncio
    async def test_malformed_name_400(self, seeded_with_surface_enabled):
        from backend.core.ouroboros.governance.ide_observability import (
            IDEObservabilityRouter,
        )
        router = IDEObservabilityRouter()
        # No JARVIS_ prefix, or has bad chars
        for bad_name in ("PATH", "JARVIS_BAD/NAME", "jarvis_lower"):
            resp = await router._handle_flag_detail(
                _make_request(match_info={"name": bad_name}),
            )
            assert resp.status == 400, bad_name


# ---------------------------------------------------------------------------
# GET /observability/flags/unregistered
# ---------------------------------------------------------------------------


class TestFlagsUnregistered:

    @pytest.mark.asyncio
    async def test_403_when_master_off(self, monkeypatch):
        monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "true")
        monkeypatch.setenv("JARVIS_FLAG_REGISTRY_ENABLED", "false")
        from backend.core.ouroboros.governance.ide_observability import (
            IDEObservabilityRouter,
        )
        router = IDEObservabilityRouter()
        resp = await router._handle_flags_unregistered(_make_request())
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_200_empty_when_no_typos(self, seeded_with_surface_enabled):
        from backend.core.ouroboros.governance.ide_observability import (
            IDEObservabilityRouter,
        )
        router = IDEObservabilityRouter()
        resp = await router._handle_flags_unregistered(_make_request())
        assert resp.status == 200
        payload = json.loads(resp.body)
        assert isinstance(payload["unregistered"], list)

    @pytest.mark.asyncio
    async def test_surfaces_typo_with_suggestion(
        self, seeded_with_surface_enabled, monkeypatch,
    ):
        monkeypatch.setenv("JARVIS_POSTURE_OBSERVR_INTERVAL_S", "600")
        from backend.core.ouroboros.governance.ide_observability import (
            IDEObservabilityRouter,
        )
        router = IDEObservabilityRouter()
        resp = await router._handle_flags_unregistered(_make_request())
        payload = json.loads(resp.body)
        names = [u["name"] for u in payload["unregistered"]]
        assert "JARVIS_POSTURE_OBSERVR_INTERVAL_S" in names
        entry = next(
            u for u in payload["unregistered"]
            if u["name"] == "JARVIS_POSTURE_OBSERVR_INTERVAL_S"
        )
        assert entry["suggestions"]


# ---------------------------------------------------------------------------
# GET /observability/verbs
# ---------------------------------------------------------------------------


class TestVerbsGet:

    @pytest.mark.asyncio
    async def test_happy_returns_seeded_verbs(self, seeded_with_surface_enabled):
        from backend.core.ouroboros.governance.ide_observability import (
            IDEObservabilityRouter,
        )
        router = IDEObservabilityRouter()
        resp = await router._handle_verbs_list(_make_request())
        assert resp.status == 200
        payload = json.loads(resp.body)
        assert payload["count"] >= 7
        names = [v["name"] for v in payload["verbs"]]
        for expected in ("/help", "/posture", "/recover", "/session",
                         "/cost", "/plan", "/layout"):
            assert expected in names


# ---------------------------------------------------------------------------
# SSE events
# ---------------------------------------------------------------------------


class TestSSEEvents:

    def test_event_types_in_whitelist(self):
        from backend.core.ouroboros.governance.ide_observability_stream import (
            EVENT_TYPE_FLAG_TYPO_DETECTED,
            EVENT_TYPE_FLAG_REGISTERED,
            _VALID_EVENT_TYPES,
        )
        assert EVENT_TYPE_FLAG_TYPO_DETECTED in _VALID_EVENT_TYPES
        assert EVENT_TYPE_FLAG_REGISTERED in _VALID_EVENT_TYPES

    def test_publish_flag_typo_disabled_returns_none(self, monkeypatch):
        monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "false")
        from backend.core.ouroboros.governance.ide_observability_stream import (
            publish_flag_typo_event, reset_default_broker,
        )
        reset_default_broker()
        assert publish_flag_typo_event("JARVIS_X", "JARVIS_Y", 1) is None

    def test_publish_flag_typo_enabled_emits_event(self, monkeypatch):
        monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "true")
        from backend.core.ouroboros.governance.ide_observability_stream import (
            EVENT_TYPE_FLAG_TYPO_DETECTED,
            get_default_broker, publish_flag_typo_event, reset_default_broker,
        )
        reset_default_broker()
        broker = get_default_broker()
        before = broker.published_count
        eid = publish_flag_typo_event("JARVIS_POSTUR", "JARVIS_POSTURE", 1)
        assert eid is not None
        assert broker.published_count == before + 1
        latest = list(broker._history)[-1]
        assert latest.event_type == EVENT_TYPE_FLAG_TYPO_DETECTED
        assert latest.payload["env_name"] == "JARVIS_POSTUR"
        assert latest.payload["closest_match"] == "JARVIS_POSTURE"

    def test_publish_flag_registered(self, monkeypatch):
        monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "true")
        from backend.core.ouroboros.governance.ide_observability_stream import (
            EVENT_TYPE_FLAG_REGISTERED,
            get_default_broker, publish_flag_registered_event,
            reset_default_broker,
        )
        reset_default_broker()
        broker = get_default_broker()
        eid = publish_flag_registered_event(
            "JARVIS_NEW", "safety", "test.py",
        )
        assert eid is not None
        latest = list(broker._history)[-1]
        assert latest.event_type == EVENT_TYPE_FLAG_REGISTERED
        assert latest.payload["name"] == "JARVIS_NEW"

    def test_bridge_publishes_on_new_registration(self, monkeypatch):
        monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "true")
        from backend.core.ouroboros.governance.ide_observability_stream import (
            EVENT_TYPE_FLAG_REGISTERED,
            bridge_flag_registry_to_broker,
            get_default_broker, reset_default_broker,
        )
        reset_default_broker()
        broker = get_default_broker()

        registry = FlagRegistry()
        bridge_flag_registry_to_broker(registry=registry)

        spec = FlagSpec(
            name="JARVIS_BRIDGE_TEST", type=FlagType.BOOL, default=False,
            description="bridge test", category=Category.EXPERIMENTAL,
            source_file="test.py",
        )
        before = broker.published_count
        registry.register(spec)
        assert broker.published_count == before + 1

    def test_bridge_does_not_fire_on_override(self, monkeypatch):
        monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "true")
        from backend.core.ouroboros.governance.ide_observability_stream import (
            bridge_flag_registry_to_broker,
            get_default_broker, reset_default_broker,
        )
        reset_default_broker()
        broker = get_default_broker()

        registry = FlagRegistry()
        spec = FlagSpec(
            name="JARVIS_O", type=FlagType.BOOL, default=False,
            description="override test", category=Category.EXPERIMENTAL,
            source_file="t.py",
        )
        registry.register(spec)  # pre-bridge initial registration
        bridge_flag_registry_to_broker(registry=registry)

        before = broker.published_count
        registry.register(spec)  # override — should NOT fire
        assert broker.published_count == before

    def test_report_typos_publishes_sse(self, monkeypatch):
        monkeypatch.setenv("JARVIS_FLAG_REGISTRY_ENABLED", "true")
        monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "true")
        from backend.core.ouroboros.governance.ide_observability_stream import (
            EVENT_TYPE_FLAG_TYPO_DETECTED,
            get_default_broker, reset_default_broker,
        )
        reset_default_broker()
        broker = get_default_broker()

        reset_default_registry()
        registry = ensure_seeded()
        monkeypatch.setenv("JARVIS_POSTURE_OBSERVR_INTERVAL_S", "600")
        before = broker.published_count
        emitted = registry.report_typos()
        assert emitted
        # Broker should have grown
        assert broker.published_count > before
        # The frame type should be FLAG_TYPO_DETECTED
        types = [e.event_type for e in broker._history]
        assert EVENT_TYPE_FLAG_TYPO_DETECTED in types


# ---------------------------------------------------------------------------
# Authority invariant
# ---------------------------------------------------------------------------


class TestAuthorityInvariant:

    @pytest.mark.parametrize("relpath", [
        "backend/core/ouroboros/governance/flag_registry.py",
        "backend/core/ouroboros/governance/flag_registry_seed.py",
        "backend/core/ouroboros/governance/help_dispatcher.py",
    ])
    def test_arc_files_authority_free(self, relpath: str):
        repo_root = Path(subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        ).stdout.strip())
        src = (repo_root / relpath).read_text(encoding="utf-8")
        forbidden = (
            "orchestrator", "policy", "iron_gate", "risk_tier",
            "change_engine", "candidate_generator",
        )
        bad = []
        for line in src.splitlines():
            if line.startswith(("from ", "import ")):
                for f in forbidden:
                    if f".{f}" in line:
                        bad.append(line)
        assert not bad, f"{relpath} authority violations: {bad}"
