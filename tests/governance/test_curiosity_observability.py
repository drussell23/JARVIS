"""M9 Slice 4 — observability + REPL + SSE tests (PRD §30.5.1).

Pins:
  § 1 — `EpistemicBudget` SSE event type + publisher
  § 2 — HTTP route handlers (overview / region detail / 503/429/400)
  § 3 — `register_routes` mounts both endpoints
  § 4 — `/curiosity` REPL dispatcher (all 5 subcommands)
  § 5 — `/curiosity reset` is the SOLE mutation surface
        (read-only contract for the rest)
  § 6 — `register_verbs` auto-discovery
  § 7 — Authority floor (no orchestrator/iron_gate/governor imports)
  § 8 — Public exports
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _enable(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JARVIS_CURIOSITY_GRADIENT_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_CURIOSITY_HISTORY_DIR", str(tmp_path / "cur"),
    )
    monkeypatch.setenv("JARVIS_CURIOSITY_MIN_SAMPLES", "3")


# ---------------------------------------------------------------------------
# § 1 — SSE event type + publisher
# ---------------------------------------------------------------------------


class TestSSEEventVocabulary:
    def test_event_type_curiosity_changed_constant(self):
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_CURIOSITY_CHANGED,
        )
        assert EVENT_TYPE_CURIOSITY_CHANGED == "curiosity_changed"

    def test_publish_curiosity_event_payload_shape(
        self, monkeypatch,
    ):
        """When stream is disabled (default), publisher returns
        None silently. Verify the function exists + accepts the
        expected kwargs without raising."""
        monkeypatch.delenv(
            "JARVIS_IDE_STREAM_ENABLED", raising=False,
        )
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            publish_curiosity_event,
        )
        result = publish_curiosity_event(
            cluster_id="backend",
            transition_kind="threshold_crossed",
            magnitude=0.75,
            confidence=0.8,
            dominant_source="logprob_entropy",
            decay_reason="none",
            samples_count=10,
        )
        # Stream disabled → None; never raises
        assert result is None or isinstance(result, str)

    def test_publish_curiosity_event_with_extra_telemetry(
        self,
    ):
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            publish_curiosity_event,
        )
        # Extra telemetry passes through without exception
        result = publish_curiosity_event(
            cluster_id="x",
            transition_kind="decay_applied",
            magnitude=0.0,
            confidence=0.0,
            dominant_source="disabled",
            decay_reason="stale_focus",
            samples_count=0,
            extra_telemetry={"prior_magnitude": 0.9},
        )
        assert result is None or isinstance(result, str)


# ---------------------------------------------------------------------------
# § 2 — HTTP route handlers
# ---------------------------------------------------------------------------


def _make_request(*, match_info=None, query=None):
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
            "JARVIS_CURIOSITY_GRADIENT_ENABLED", "false",
        )
        from backend.core.ouroboros.governance.curiosity_observability import (  # noqa: E501
            _CuriosityRoutesHandler,
        )
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            CuriosityCollector,
        )
        h = _CuriosityRoutesHandler(
            collector=CuriosityCollector(),
        )
        resp = await h.handle_overview(_make_request())
        assert resp.status == 503

    @pytest.mark.asyncio
    async def test_overview_returns_sorted_scores(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.curiosity_observability import (  # noqa: E501
            _CuriosityRoutesHandler,
        )
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            CuriosityCollector,
        )
        c = CuriosityCollector()
        now = time.time()
        # Three clusters with distinct magnitudes
        for _ in range(5):
            c.record_logprob_entropy(
                "high", 0.9, at_unix=now,
            )
        for _ in range(5):
            c.record_logprob_entropy(
                "mid", 0.5, at_unix=now,
            )
        for _ in range(5):
            c.record_logprob_entropy(
                "low", 0.1, at_unix=now,
            )
        h = _CuriosityRoutesHandler(collector=c)
        resp = await h.handle_overview(_make_request())
        assert resp.status == 200
        body = json.loads(resp.body)
        assert body["tracked_count"] == 3
        scores = body["scores"]
        assert len(scores) == 3
        # Sorted by magnitude descending
        assert scores[0]["cluster_id"] == "high"
        assert scores[1]["cluster_id"] == "mid"
        assert scores[2]["cluster_id"] == "low"
        assert (
            body["sse_event_type"] == "curiosity_changed"
        )
        assert "source_kinds" in body
        assert "decay_reasons" in body
        assert "config" in body

    @pytest.mark.asyncio
    async def test_overview_respects_limit_param(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.curiosity_observability import (  # noqa: E501
            _CuriosityRoutesHandler,
        )
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            CuriosityCollector,
        )
        c = CuriosityCollector()
        now = time.time()
        for letter in "abcdef":
            for _ in range(5):
                c.record_logprob_entropy(
                    letter, 0.5, at_unix=now,
                )
        h = _CuriosityRoutesHandler(collector=c)
        resp = await h.handle_overview(
            _make_request(query={"limit": "3"}),
        )
        body = json.loads(resp.body)
        assert body["tracked_count"] == 6
        assert len(body["scores"]) == 3

    @pytest.mark.asyncio
    async def test_region_detail_returns_observations(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.curiosity_observability import (  # noqa: E501
            _CuriosityRoutesHandler,
        )
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            CuriosityCollector,
        )
        c = CuriosityCollector()
        now = time.time()
        for i in range(5):
            c.record_logprob_entropy(
                "x", 0.6, op_id=f"op-{i}",
                at_unix=now + i * 0.001,
            )
        h = _CuriosityRoutesHandler(collector=c)
        resp = await h.handle_region_detail(
            _make_request(match_info={"cluster_id": "x"}),
        )
        assert resp.status == 200
        body = json.loads(resp.body)
        assert body["cluster_id"] == "x"
        assert "observations" in body
        assert body["observations_count"] == 5
        assert (
            body["sse_event_type"] == "curiosity_changed"
        )

    @pytest.mark.asyncio
    async def test_region_detail_missing_id_returns_400(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.curiosity_observability import (  # noqa: E501
            _CuriosityRoutesHandler,
        )
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            CuriosityCollector,
        )
        h = _CuriosityRoutesHandler(
            collector=CuriosityCollector(),
        )
        resp = await h.handle_region_detail(
            _make_request(match_info={"cluster_id": ""}),
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_rate_limit_check_blocks(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.curiosity_observability import (  # noqa: E501
            _CuriosityRoutesHandler,
        )
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            CuriosityCollector,
        )
        h = _CuriosityRoutesHandler(
            collector=CuriosityCollector(),
            rate_limit_check=lambda req: False,
        )
        resp = await h.handle_overview(_make_request())
        assert resp.status == 429


# ---------------------------------------------------------------------------
# § 3 — register_routes mounts both endpoints
# ---------------------------------------------------------------------------


class TestRegisterRoutes:
    def test_register_routes_mounts_two_endpoints(self):
        from aiohttp import web
        from backend.core.ouroboros.governance.curiosity_observability import (  # noqa: E501
            register_routes,
        )
        app = web.Application()
        register_routes(app)
        paths = {
            r.resource.canonical for r in app.router.routes()
            if r.resource is not None
        }
        assert "/observability/curiosity" in paths
        assert any(
            "/observability/curiosity/region/" in p
            for p in paths
        )


# ---------------------------------------------------------------------------
# § 4 — /curiosity REPL dispatcher
# ---------------------------------------------------------------------------


class TestCuriosityREPL:
    def test_help_works_when_disabled(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CURIOSITY_GRADIENT_ENABLED", "false",
        )
        from backend.core.ouroboros.governance.curiosity_repl import (  # noqa: E501
            dispatch_curiosity_command,
        )
        res = dispatch_curiosity_command("/curiosity help")
        assert res.ok is True
        assert "/curiosity" in res.text
        assert "PRD §30.5.1" in res.text

    def test_disabled_returns_friendly_message(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_CURIOSITY_GRADIENT_ENABLED", "false",
        )
        from backend.core.ouroboros.governance.curiosity_repl import (  # noqa: E501
            dispatch_curiosity_command,
        )
        res = dispatch_curiosity_command("/curiosity top")
        assert res.ok is False
        assert "disabled" in res.text.lower()

    def test_top_no_clusters_tracked(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            CuriosityCollector,
        )
        from backend.core.ouroboros.governance.curiosity_repl import (  # noqa: E501
            dispatch_curiosity_command,
        )
        c = CuriosityCollector()
        res = dispatch_curiosity_command(
            "/curiosity top", collector=c,
        )
        assert res.ok is True
        assert "no clusters" in res.text.lower()

    def test_top_shows_tracked_clusters(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            CuriosityCollector,
        )
        from backend.core.ouroboros.governance.curiosity_repl import (  # noqa: E501
            dispatch_curiosity_command,
        )
        c = CuriosityCollector()
        now = time.time()
        for _ in range(5):
            c.record_logprob_entropy(
                "alpha", 0.9, at_unix=now,
            )
        for _ in range(5):
            c.record_logprob_entropy(
                "beta", 0.4, at_unix=now,
            )
        res = dispatch_curiosity_command(
            "/curiosity top", collector=c,
        )
        assert res.ok is True
        assert "alpha" in res.text
        assert "beta" in res.text
        # Higher-magnitude alpha appears before beta
        assert res.text.index("alpha") < res.text.index("beta")

    def test_region_detail(self, monkeypatch, tmp_path):
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            CuriosityCollector,
        )
        from backend.core.ouroboros.governance.curiosity_repl import (  # noqa: E501
            dispatch_curiosity_command,
        )
        c = CuriosityCollector()
        now = time.time()
        for _ in range(5):
            c.record_logprob_entropy(
                "x", 0.7, at_unix=now,
            )
        res = dispatch_curiosity_command(
            "/curiosity region x", collector=c,
        )
        assert res.ok is True
        assert "/curiosity region x" in res.text
        assert "magnitude" in res.text
        assert "source_breakdown" in res.text

    def test_region_missing_arg(self, monkeypatch, tmp_path):
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            CuriosityCollector,
        )
        from backend.core.ouroboros.governance.curiosity_repl import (  # noqa: E501
            dispatch_curiosity_command,
        )
        res = dispatch_curiosity_command(
            "/curiosity region",
            collector=CuriosityCollector(),
        )
        assert res.ok is False
        assert "missing cluster_id" in res.text

    def test_config_renders_env_knobs(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.curiosity_repl import (  # noqa: E501
            dispatch_curiosity_command,
        )
        res = dispatch_curiosity_command("/curiosity config")
        assert res.ok is True
        assert "halflife_days" in res.text
        assert "multiplier_ceiling" in res.text
        assert "weight_logprob" in res.text

    def test_unknown_subcommand(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.curiosity_repl import (  # noqa: E501
            dispatch_curiosity_command,
        )
        res = dispatch_curiosity_command("/curiosity xyzzy")
        assert res.ok is False
        assert "unknown subcommand" in res.text

    def test_non_curiosity_line_doesnt_match(self):
        from backend.core.ouroboros.governance.curiosity_repl import (  # noqa: E501
            dispatch_curiosity_command,
        )
        res = dispatch_curiosity_command("/posture status")
        assert res.matched is False

    def test_default_subcommand_is_top(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            CuriosityCollector,
        )
        from backend.core.ouroboros.governance.curiosity_repl import (  # noqa: E501
            dispatch_curiosity_command,
        )
        res_default = dispatch_curiosity_command(
            "/curiosity",
            collector=CuriosityCollector(),
        )
        res_top = dispatch_curiosity_command(
            "/curiosity top",
            collector=CuriosityCollector(),
        )
        # Both produce the "no clusters tracked" empty message
        assert res_default.ok == res_top.ok
        # Both reference /curiosity top
        assert "top" in res_default.text or (
            "no clusters" in res_default.text.lower()
        )


# ---------------------------------------------------------------------------
# § 5 — /curiosity reset is the SOLE mutation surface
# ---------------------------------------------------------------------------


class TestResetMutationSurface:
    def test_reset_marks_cluster(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            CuriosityCollector,
        )
        from backend.core.ouroboros.governance.curiosity_gradient import (  # noqa: E501
            CuriosityDecayReason,
        )
        from backend.core.ouroboros.governance.curiosity_repl import (  # noqa: E501
            dispatch_curiosity_command,
        )
        c = CuriosityCollector()
        now = time.time()
        for _ in range(5):
            c.record_logprob_entropy(
                "x", 0.9, at_unix=now,
            )
        # Call reset
        res = dispatch_curiosity_command(
            "/curiosity reset x", collector=c,
        )
        assert res.ok is True
        assert "OPERATOR_RESET" in res.text
        # Verify decay engaged on next score query
        score = c.score_for_cluster("x")
        assert score.decay_reason is (
            CuriosityDecayReason.OPERATOR_RESET
        )

    def test_reset_missing_arg(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
            CuriosityCollector,
        )
        from backend.core.ouroboros.governance.curiosity_repl import (  # noqa: E501
            dispatch_curiosity_command,
        )
        res = dispatch_curiosity_command(
            "/curiosity reset",
            collector=CuriosityCollector(),
        )
        assert res.ok is False
        assert "missing cluster_id" in res.text


# ---------------------------------------------------------------------------
# § 6 — register_verbs auto-discovery
# ---------------------------------------------------------------------------


class TestRegisterVerbs:
    def test_register_verbs_returns_one(self):
        from backend.core.ouroboros.governance.curiosity_repl import (  # noqa: E501
            register_verbs,
        )
        registry = MagicMock()
        n = register_verbs(registry)
        assert n == 1
        registry.register.assert_called_once()
        spec = registry.register.call_args[0][0]
        assert spec.name == "/curiosity"


# ---------------------------------------------------------------------------
# § 7 — Authority floor
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
        "from backend.core.ouroboros.governance.strategic_direction",
        "from backend.core.ouroboros.governance.sensor_governor",
    )

    def _read_module(self, name: str) -> str:
        return (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / f"{name}.py"
        ).read_text(encoding="utf-8")

    def test_observability_module_floor(self):
        source = self._read_module(
            "curiosity_observability",
        )
        for forbidden in self._FORBIDDEN:
            assert forbidden not in source, forbidden

    def test_observability_is_read_only(self):
        """HTTP routes module must not call any mutation
        surface — the only mutation surface is /curiosity reset
        in the REPL module."""
        source = self._read_module(
            "curiosity_observability",
        )
        forbidden_calls = (
            ".reset_cluster(",
            ".record_logprob_entropy(",
            ".record_prophecy_error(",
            ".record_recurrence_drift(",
        )
        for fcall in forbidden_calls:
            assert fcall not in source, (
                f"curiosity_observability.py is read-only — "
                f"found mutation call {fcall}"
            )

    def test_repl_module_floor(self):
        source = self._read_module("curiosity_repl")
        for forbidden in self._FORBIDDEN:
            assert forbidden not in source, forbidden

    def test_repl_only_mutation_is_reset(self):
        """The /curiosity reset subcommand is the SOLE
        mutation surface — REPL module must not call
        record_* methods."""
        source = self._read_module("curiosity_repl")
        forbidden_calls = (
            ".record_logprob_entropy(",
            ".record_prophecy_error(",
            ".record_recurrence_drift(",
        )
        for fcall in forbidden_calls:
            assert fcall not in source, (
                f"curiosity_repl.py records nothing — found "
                f"unexpected mutation call {fcall}"
            )
        # reset_cluster IS allowed (the operator-explicit surface)
        assert ".reset_cluster(" in source


# ---------------------------------------------------------------------------
# § 8 — Public exports
# ---------------------------------------------------------------------------


class TestPublicExports:
    def test_observability_exports(self):
        from backend.core.ouroboros.governance import (
            curiosity_observability as co,
        )
        assert sorted(co.__all__) == ["register_routes"]

    def test_repl_exports(self):
        from backend.core.ouroboros.governance import (
            curiosity_repl as cr,
        )
        expected = sorted([
            "CuriosityReplDispatchResult",
            "dispatch_curiosity_command",
            "register_verbs",
            "reset_default_collector_for_tests",
            "set_default_collector",
        ])
        assert sorted(cr.__all__) == expected
