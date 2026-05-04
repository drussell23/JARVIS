"""Slice 5b C — Generative Quorum observer + observability tests.

Mirrors ``tests/governance/test_coherence_observability.py`` and
``test_confidence_probe_graduation.py`` for the register_*_routes
idiom: isolated handler tests + the structural event_channel mount
pin + observer recorder/reader tests + adaptive-stats tests +
runner-wire-up structural pin.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest


# ---------------------------------------------------------------------------
# § 1 — register_quorum_routes mounts five endpoints
# ---------------------------------------------------------------------------


class TestObservabilityRoutes:
    def test_register_routes_mounts_five_endpoints(self):
        from aiohttp import web
        from backend.core.ouroboros.governance.verification.generative_quorum_observability import (  # noqa: E501
            register_quorum_routes,
        )
        app = web.Application()
        register_quorum_routes(app)
        paths = {
            r.url_for().path
            for resource in app.router.resources()
            for r in resource
        }
        assert "/observability/quorum" in paths
        assert "/observability/quorum/config" in paths
        assert "/observability/quorum/history" in paths
        assert "/observability/quorum/stats" in paths
        assert "/observability/quorum/outcomes" in paths

    def test_routes_safe_to_mount_with_master_off(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_GENERATIVE_QUORUM_ENABLED", "false",
        )
        from aiohttp import web
        from backend.core.ouroboros.governance.verification.generative_quorum_observability import (  # noqa: E501
            register_quorum_routes,
        )
        app = web.Application()
        register_quorum_routes(app)


# ---------------------------------------------------------------------------
# § 2 — Per-handler 503/200 master-flag gate contract
# ---------------------------------------------------------------------------


class TestHandlerGate:
    @pytest.mark.asyncio
    async def test_overview_503_when_master_off(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_GENERATIVE_QUORUM_ENABLED", "false",
        )
        from backend.core.ouroboros.governance.verification.generative_quorum_observability import (  # noqa: E501
            _QuorumRoutesHandler,
        )
        h = _QuorumRoutesHandler()
        r = SimpleNamespace(query={})
        response = await h.handle_overview(r)
        assert response.status == 503

    @pytest.mark.asyncio
    async def test_history_503_when_master_off(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_GENERATIVE_QUORUM_ENABLED", "false",
        )
        from backend.core.ouroboros.governance.verification.generative_quorum_observability import (  # noqa: E501
            _QuorumRoutesHandler,
        )
        h = _QuorumRoutesHandler()
        r = SimpleNamespace(query={})
        response = await h.handle_history(r)
        assert response.status == 503

    @pytest.mark.asyncio
    async def test_stats_503_when_master_off(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_GENERATIVE_QUORUM_ENABLED", "false",
        )
        from backend.core.ouroboros.governance.verification.generative_quorum_observability import (  # noqa: E501
            _QuorumRoutesHandler,
        )
        h = _QuorumRoutesHandler()
        r = SimpleNamespace(query={})
        response = await h.handle_stats(r)
        assert response.status == 503

    @pytest.mark.asyncio
    async def test_outcomes_503_when_master_off(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_GENERATIVE_QUORUM_ENABLED", "false",
        )
        from backend.core.ouroboros.governance.verification.generative_quorum_observability import (  # noqa: E501
            _QuorumRoutesHandler,
        )
        h = _QuorumRoutesHandler()
        r = SimpleNamespace(query={})
        response = await h.handle_outcomes(r)
        assert response.status == 503

    @pytest.mark.asyncio
    async def test_overview_200_when_master_on(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_GENERATIVE_QUORUM_ENABLED", "true",
        )
        from backend.core.ouroboros.governance.verification.generative_quorum_observability import (  # noqa: E501
            _QuorumRoutesHandler,
        )
        h = _QuorumRoutesHandler()
        r = SimpleNamespace(query={})
        response = await h.handle_overview(r)
        assert response.status == 200


# ---------------------------------------------------------------------------
# § 3 — Overview payload shape
# ---------------------------------------------------------------------------


class TestOverviewPayload:
    @pytest.mark.asyncio
    async def test_overview_payload_shape(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_GENERATIVE_QUORUM_ENABLED", "true",
        )
        from backend.core.ouroboros.governance.verification.generative_quorum_observability import (  # noqa: E501
            _QuorumRoutesHandler,
        )
        h = _QuorumRoutesHandler()
        r = SimpleNamespace(query={})
        response = await h.handle_overview(r)
        body = json.loads(response.body)
        assert "schemas" in body
        assert "primitive" in body["schemas"]
        assert "runner" in body["schemas"]
        assert "gate" in body["schemas"]
        assert "observer" in body["schemas"]
        assert "flags" in body
        assert body["flags"]["quorum_enabled"] is True
        assert "quorum_config" in body
        assert "observer_config" in body
        assert "history_size" in body
        assert "recent_stats" in body
        assert "sse_event_type" in body
        assert "outcome_kinds" in body

    @pytest.mark.asyncio
    async def test_outcome_kinds_match_enum_dynamically(
        self, monkeypatch,
    ):
        """Drift-safe: probe the enum + assert overview surface
        size matches — never quote literal outcome strings."""
        monkeypatch.setenv(
            "JARVIS_GENERATIVE_QUORUM_ENABLED", "true",
        )
        from backend.core.ouroboros.governance.verification.generative_quorum import (  # noqa: E501
            ConsensusOutcome,
        )
        from backend.core.ouroboros.governance.verification.generative_quorum_observability import (  # noqa: E501
            _QuorumRoutesHandler,
        )
        h = _QuorumRoutesHandler()
        r = SimpleNamespace(query={})
        response = await h.handle_overview(r)
        body = json.loads(response.body)
        assert (
            len(body["outcome_kinds"]) == len(ConsensusOutcome)
        )


# ---------------------------------------------------------------------------
# § 4 — Query-param parsing
# ---------------------------------------------------------------------------


class TestQueryParamParsing:
    def test_limit_default(self):
        from backend.core.ouroboros.governance.verification.generative_quorum_observability import (  # noqa: E501
            _parse_limit,
        )
        assert _parse_limit(SimpleNamespace(query={})) == 50

    def test_limit_clamps_to_max(self):
        from backend.core.ouroboros.governance.verification.generative_quorum_observability import (  # noqa: E501
            _parse_limit,
        )
        assert _parse_limit(
            SimpleNamespace(query={"limit": "999999"}),
        ) == 1000

    def test_limit_clamps_to_min(self):
        from backend.core.ouroboros.governance.verification.generative_quorum_observability import (  # noqa: E501
            _parse_limit,
        )
        assert _parse_limit(
            SimpleNamespace(query={"limit": "0"}),
        ) == 1

    def test_since_ts_negative_floors_to_zero(self):
        from backend.core.ouroboros.governance.verification.generative_quorum_observability import (  # noqa: E501
            _parse_since_ts,
        )
        assert _parse_since_ts(
            SimpleNamespace(query={"since_ts": "-100"}),
        ) == 0.0


# ---------------------------------------------------------------------------
# § 5 — Empty-history endpoints return 200 with empty arrays
# ---------------------------------------------------------------------------


class TestEmptyHistoryEndpoints:
    @pytest.mark.asyncio
    async def test_history_empty_200(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv(
            "JARVIS_GENERATIVE_QUORUM_ENABLED", "true",
        )
        monkeypatch.setenv(
            "JARVIS_QUORUM_HISTORY_DIR", str(tmp_path),
        )
        from backend.core.ouroboros.governance.verification.generative_quorum_observability import (  # noqa: E501
            _QuorumRoutesHandler,
        )
        h = _QuorumRoutesHandler()
        r = SimpleNamespace(query={})
        response = await h.handle_history(r)
        assert response.status == 200
        body = json.loads(response.body)
        assert body["count"] == 0
        assert body["records"] == []

    @pytest.mark.asyncio
    async def test_stats_empty_200(self, monkeypatch, tmp_path):
        monkeypatch.setenv(
            "JARVIS_GENERATIVE_QUORUM_ENABLED", "true",
        )
        monkeypatch.setenv(
            "JARVIS_QUORUM_HISTORY_DIR", str(tmp_path),
        )
        from backend.core.ouroboros.governance.verification.generative_quorum_observability import (  # noqa: E501
            _QuorumRoutesHandler,
        )
        h = _QuorumRoutesHandler()
        r = SimpleNamespace(query={})
        response = await h.handle_stats(r)
        assert response.status == 200
        body = json.loads(response.body)
        assert body["stats"]["sample_size"] == 0


# ---------------------------------------------------------------------------
# § 6 — Recorder roundtrip — write a real run, read it back, stats
# ---------------------------------------------------------------------------


def _make_quorum_run_result(
    outcome_value: str,
    *,
    agreement: int = 3,
    distinct: int = 1,
    total: int = 3,
    elapsed: float = 0.123,
    failed_ids: tuple = (),
    signature: str = "deadbeef",
    accepted: str = "roll-0",
    detail: str = "ok",
):
    """Build a synthetic QuorumRunResult that round-trips through
    QuorumRunResult.to_dict — uses the real frozen dataclasses, no
    mocks of internal contracts."""
    from backend.core.ouroboros.governance.verification.generative_quorum import (  # noqa: E501
        CandidateRoll,
        ConsensusOutcome,
        ConsensusVerdict,
    )
    from backend.core.ouroboros.governance.verification.generative_quorum_runner import (  # noqa: E501
        QuorumRunResult,
    )
    verdict = ConsensusVerdict(
        outcome=ConsensusOutcome(outcome_value),
        agreement_count=agreement,
        distinct_count=distinct,
        total_rolls=total,
        canonical_signature=signature,
        accepted_roll_id=accepted,
        detail=detail,
    )
    rolls = tuple(
        CandidateRoll(
            roll_id=f"roll-{i}",
            candidate_diff="...",
            ast_signature=signature,
            cost_estimate_usd=0.0,
            seed=i,
        )
        for i in range(total)
    )
    return QuorumRunResult(
        verdict=verdict,
        rolls=rolls,
        failed_roll_ids=tuple(failed_ids),
        elapsed_seconds=elapsed,
    )


class TestRecorderRoundtrip:
    def test_record_then_read(self, monkeypatch, tmp_path):
        monkeypatch.setenv(
            "JARVIS_GENERATIVE_QUORUM_ENABLED", "true",
        )
        monkeypatch.setenv(
            "JARVIS_QUORUM_OBSERVER_ENABLED", "true",
        )
        monkeypatch.setenv(
            "JARVIS_QUORUM_HISTORY_DIR", str(tmp_path),
        )
        from backend.core.ouroboros.governance.verification.generative_quorum_observer import (  # noqa: E501
            RecordOutcome,
            read_quorum_history,
            record_quorum_run,
        )
        result = _make_quorum_run_result("consensus")
        outcome = record_quorum_run(result, op_id="op-test-001")
        assert outcome is RecordOutcome.OK
        history = read_quorum_history()
        assert len(history) == 1
        assert history[0].op_id == "op-test-001"

    def test_record_disabled_outcome_rejected(
        self, monkeypatch, tmp_path,
    ):
        """DISABLED outcomes must NOT be persisted (zero noise
        floor when consensus is master-off — runs that never fired
        carry no signal)."""
        monkeypatch.setenv(
            "JARVIS_GENERATIVE_QUORUM_ENABLED", "true",
        )
        monkeypatch.setenv(
            "JARVIS_QUORUM_HISTORY_DIR", str(tmp_path),
        )
        from backend.core.ouroboros.governance.verification.generative_quorum_observer import (  # noqa: E501
            RecordOutcome,
            record_quorum_run,
        )
        result = _make_quorum_run_result(
            "disabled",
            agreement=0, distinct=0, total=0,
            signature="", accepted="",
            detail="master flag off",
        )
        outcome = record_quorum_run(result, op_id="op-disabled")
        assert outcome is RecordOutcome.REJECTED

    def test_record_disabled_when_master_off(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv(
            "JARVIS_GENERATIVE_QUORUM_ENABLED", "false",
        )
        monkeypatch.setenv(
            "JARVIS_QUORUM_HISTORY_DIR", str(tmp_path),
        )
        from backend.core.ouroboros.governance.verification.generative_quorum_observer import (  # noqa: E501
            RecordOutcome,
            record_quorum_run,
        )
        result = _make_quorum_run_result("consensus")
        outcome = record_quorum_run(result)
        assert outcome is RecordOutcome.DISABLED

    def test_record_disabled_when_observer_off(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv(
            "JARVIS_GENERATIVE_QUORUM_ENABLED", "true",
        )
        monkeypatch.setenv(
            "JARVIS_QUORUM_OBSERVER_ENABLED", "false",
        )
        monkeypatch.setenv(
            "JARVIS_QUORUM_HISTORY_DIR", str(tmp_path),
        )
        from backend.core.ouroboros.governance.verification.generative_quorum_observer import (  # noqa: E501
            RecordOutcome,
            record_quorum_run,
        )
        result = _make_quorum_run_result("consensus")
        outcome = record_quorum_run(result)
        assert outcome is RecordOutcome.DISABLED

    def test_record_rejected_for_garbage_input(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv(
            "JARVIS_GENERATIVE_QUORUM_ENABLED", "true",
        )
        monkeypatch.setenv(
            "JARVIS_QUORUM_HISTORY_DIR", str(tmp_path),
        )
        from backend.core.ouroboros.governance.verification.generative_quorum_observer import (  # noqa: E501
            RecordOutcome,
            record_quorum_run,
        )
        outcome = record_quorum_run("not a result")  # type: ignore[arg-type]
        assert outcome is RecordOutcome.REJECTED


# ---------------------------------------------------------------------------
# § 7 — Adaptive stats — derived insights over a real history
# ---------------------------------------------------------------------------


class TestAdaptiveStats:
    def test_stability_score_all_consensus(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv(
            "JARVIS_GENERATIVE_QUORUM_ENABLED", "true",
        )
        monkeypatch.setenv(
            "JARVIS_QUORUM_HISTORY_DIR", str(tmp_path),
        )
        from backend.core.ouroboros.governance.verification.generative_quorum_observer import (  # noqa: E501
            compute_recent_quorum_stats,
            record_quorum_run,
        )
        for i in range(5):
            record_quorum_run(
                _make_quorum_run_result("consensus"),
                op_id=f"op-{i}",
            )
        stats = compute_recent_quorum_stats()
        assert stats.sample_size == 5
        assert stats.stability_score == 1.0
        assert stats.actionable_score == 1.0
        assert stats.outcome_distribution.get("consensus") == 5

    def test_stability_score_mixed_outcomes(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv(
            "JARVIS_GENERATIVE_QUORUM_ENABLED", "true",
        )
        monkeypatch.setenv(
            "JARVIS_QUORUM_HISTORY_DIR", str(tmp_path),
        )
        from backend.core.ouroboros.governance.verification.generative_quorum_observer import (  # noqa: E501
            compute_recent_quorum_stats,
            record_quorum_run,
        )
        # 2 consensus + 1 majority + 1 disagreement → stability =
        # 2/4 = 0.5; actionable = (2+1)/4 = 0.75
        record_quorum_run(
            _make_quorum_run_result("consensus"),
            op_id="op-1",
        )
        record_quorum_run(
            _make_quorum_run_result("consensus"),
            op_id="op-2",
        )
        record_quorum_run(
            _make_quorum_run_result(
                "majority_consensus",
                agreement=2, distinct=2,
            ),
            op_id="op-3",
        )
        record_quorum_run(
            _make_quorum_run_result(
                "disagreement",
                agreement=1, distinct=3,
            ),
            op_id="op-4",
        )
        stats = compute_recent_quorum_stats()
        assert stats.sample_size == 4
        assert stats.stability_score == 0.5
        assert stats.actionable_score == 0.75

    def test_failed_roll_fraction(self, monkeypatch, tmp_path):
        monkeypatch.setenv(
            "JARVIS_GENERATIVE_QUORUM_ENABLED", "true",
        )
        monkeypatch.setenv(
            "JARVIS_QUORUM_HISTORY_DIR", str(tmp_path),
        )
        from backend.core.ouroboros.governance.verification.generative_quorum_observer import (  # noqa: E501
            compute_recent_quorum_stats,
            record_quorum_run,
        )
        # 1/3 rolls failed → 0.333...
        record_quorum_run(
            _make_quorum_run_result(
                "majority_consensus",
                agreement=2, distinct=2, total=3,
                failed_ids=("roll-2",),
            ),
        )
        stats = compute_recent_quorum_stats()
        assert abs(stats.avg_failed_roll_fraction - 1.0 / 3.0) < 1e-6


# ---------------------------------------------------------------------------
# § 8 — Authority invariants
# ---------------------------------------------------------------------------


class TestAuthorityInvariants:
    def test_observability_authority(self):
        path = (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "verification" / "generative_quorum_observability.py"
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
                f"generative_quorum_observability must NOT import "
                f"{forbidden_path}"
            )

    def test_observer_authority(self):
        path = (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "verification" / "generative_quorum_observer.py"
        )
        source = path.read_text(encoding="utf-8")
        forbidden = [
            "from backend.core.ouroboros.governance.orchestrator",
            "from backend.core.ouroboros.governance.iron_gate",
            "from backend.core.ouroboros.governance.providers",
            "from backend.core.ouroboros.governance.semantic_guardian",
            "from backend.core.ouroboros.governance.tool_executor",
            "from backend.core.ouroboros.governance.change_engine",
            "from backend.core.ouroboros.governance.policy",
        ]
        for forbidden_path in forbidden:
            assert forbidden_path not in source, (
                f"generative_quorum_observer must NOT import "
                f"{forbidden_path}"
            )


# ---------------------------------------------------------------------------
# § 9 — Structural pins: event_channel mount + runner wire-up
# ---------------------------------------------------------------------------


class TestStructuralPins:
    def test_event_channel_imports_quorum_module(self):
        """Slice 5b C — pin the event_channel mount."""
        path = (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "event_channel.py"
        )
        source = path.read_text(encoding="utf-8")
        assert "register_quorum_routes" in source, (
            "event_channel must mount the quorum GET routes "
            "(Slice 5b C)"
        )
        assert "Move 6 Slice 5b" in source, (
            "event_channel must mark the wiring with the slice "
            "comment for traceability"
        )

    def test_runner_wires_recorder(self):
        """Slice 5b C — pin the runner's recorder call so a future
        refactor cannot silently drop persistence."""
        path = (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "verification" / "generative_quorum_runner.py"
        )
        source = path.read_text(encoding="utf-8")
        assert "record_quorum_run" in source, (
            "generative_quorum_runner must call record_quorum_run "
            "after publish_quorum_outcome (Slice 5b C)"
        )
        assert "Slice 5b C" in source, (
            "runner must mark the recorder wiring with the slice "
            "comment for traceability"
        )
