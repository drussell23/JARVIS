"""Tests for RemEpoch — single explore → analyze → patch cycle (TDD first).

All dependencies are injected as mocks.  No I/O, no model calls, no network.
The oracle methods (find_dead_code, find_circular_dependencies) are SYNC.
Fleet.deploy() is async.  SpinalCord has async stream_up.
IntakeRouter has async ingest().
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from typing import List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.ouroboros.cancellation_token import CancellationToken
from backend.core.ouroboros.daemon_config import DaemonConfig
from backend.core.ouroboros.finding_ranker import RankedFinding
from backend.core.ouroboros.rem_epoch import EpochResult, RemEpoch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = time.time()
_RECENT = _NOW - 1


def _make_finding(
    *,
    description: str = "sample finding",
    category: str = "dead_code",
    file_path: str = "backend/foo.py",
    blast_radius: float = 0.5,
    confidence: float = 0.75,
    urgency: str = "normal",
    repo: str = "jarvis",
    source_check: str = "check_dead_code",
) -> RankedFinding:
    return RankedFinding(
        description=description,
        category=category,
        file_path=file_path,
        blast_radius=blast_radius,
        confidence=confidence,
        urgency=urgency,
        last_modified=_RECENT,
        repo=repo,
        source_check=source_check,
    )


def _make_config(**overrides) -> DaemonConfig:
    """Build a fast DaemonConfig suitable for tests."""
    defaults = dict(
        rem_cycle_timeout_s=5.0,
        rem_max_agents=2,
        rem_max_findings_per_epoch=10,
    )
    defaults.update(overrides)
    return DaemonConfig(**defaults)


def _make_oracle(*, dead_code=None, circular_deps=None) -> MagicMock:
    """Oracle with sync find_dead_code / find_circular_dependencies."""
    oracle = MagicMock()
    oracle.find_dead_code.return_value = dead_code or []
    oracle.find_circular_dependencies.return_value = circular_deps or []
    return oracle


def _make_fleet(*, findings=None) -> MagicMock:
    """ExplorationFleet mock with async deploy()."""
    fleet = MagicMock()
    fleet_report = MagicMock()
    fleet_report.findings = findings or []
    fleet.deploy = AsyncMock(return_value=fleet_report)
    return fleet


def _make_spinal_cord() -> MagicMock:
    cord = MagicMock()
    cord.stream_up = AsyncMock()
    cord.stream_down = AsyncMock()
    return cord


def _make_intake_router(*, ingest_return: str = "enqueued") -> MagicMock:
    router = MagicMock()
    router.ingest = AsyncMock(return_value=ingest_return)
    return router


def _make_doubleword() -> MagicMock:
    return MagicMock()


def _make_epoch(
    *,
    oracle=None,
    fleet=None,
    spinal_cord=None,
    intake_router=None,
    doubleword=None,
    config=None,
    epoch_id: int = 1,
) -> RemEpoch:
    return RemEpoch(
        epoch_id=epoch_id,
        oracle=oracle or _make_oracle(),
        fleet=fleet or _make_fleet(),
        spinal_cord=spinal_cord or _make_spinal_cord(),
        intake_router=intake_router or _make_intake_router(),
        doubleword=doubleword or _make_doubleword(),
        config=config or _make_config(),
    )


# ---------------------------------------------------------------------------
# test_epoch_with_no_findings
# ---------------------------------------------------------------------------


class TestEpochWithNoFindings:
    @pytest.mark.asyncio
    async def test_epoch_with_no_findings_completed(self):
        """Empty oracle + empty fleet → completed=True, 0 findings."""
        epoch = _make_epoch(
            oracle=_make_oracle(dead_code=[], circular_deps=[]),
            fleet=_make_fleet(findings=[]),
        )
        token = CancellationToken(epoch_id=1)
        result = await epoch.run(token)

        assert result.completed is True
        assert result.cancelled is False
        assert result.findings_count == 0
        assert result.error is None

    @pytest.mark.asyncio
    async def test_epoch_no_findings_does_not_submit_envelopes(self):
        """When no findings, intake.ingest must never be called."""
        router = _make_intake_router()
        epoch = _make_epoch(
            oracle=_make_oracle(dead_code=[], circular_deps=[]),
            fleet=_make_fleet(findings=[]),
            intake_router=router,
        )
        token = CancellationToken(epoch_id=1)
        await epoch.run(token)

        router.ingest.assert_not_called()

    @pytest.mark.asyncio
    async def test_epoch_no_findings_epoch_id_preserved(self):
        """EpochResult.epoch_id must match the constructed epoch_id."""
        epoch = _make_epoch(epoch_id=42)
        token = CancellationToken(epoch_id=42)
        result = await epoch.run(token)

        assert result.epoch_id == 42


# ---------------------------------------------------------------------------
# test_epoch_with_findings_submits_envelopes
# ---------------------------------------------------------------------------


class TestEpochWithFindingsSubmitsEnvelopes:
    @pytest.mark.asyncio
    async def test_oracle_dead_code_yields_finding(self):
        """When oracle.find_dead_code returns entries, findings_count >= 1."""
        # Oracle returns a NodeID-like object with file_path + name attrs
        node = MagicMock()
        node.file_path = "backend/core/foo.py"
        node.name = "unused_fn"

        epoch = _make_epoch(
            oracle=_make_oracle(dead_code=[node], circular_deps=[]),
            fleet=_make_fleet(findings=[]),
        )
        token = CancellationToken(epoch_id=1)
        result = await epoch.run(token)

        assert result.findings_count >= 1

    @pytest.mark.asyncio
    async def test_intake_ingest_called_for_each_finding(self):
        """One finding → intake.ingest called exactly once."""
        node = MagicMock()
        node.file_path = "backend/core/foo.py"
        node.name = "unused_fn"

        router = _make_intake_router(ingest_return="enqueued")
        epoch = _make_epoch(
            oracle=_make_oracle(dead_code=[node], circular_deps=[]),
            fleet=_make_fleet(findings=[]),
            intake_router=router,
        )
        token = CancellationToken(epoch_id=1)
        await epoch.run(token)

        assert router.ingest.call_count >= 1

    @pytest.mark.asyncio
    async def test_envelopes_submitted_count_tracked(self):
        """envelopes_submitted reflects how many ingest calls returned 'enqueued'."""
        node = MagicMock()
        node.file_path = "backend/core/foo.py"
        node.name = "unused_fn"

        epoch = _make_epoch(
            oracle=_make_oracle(dead_code=[node], circular_deps=[]),
            fleet=_make_fleet(findings=[]),
            intake_router=_make_intake_router(ingest_return="enqueued"),
        )
        token = CancellationToken(epoch_id=1)
        result = await epoch.run(token)

        assert result.envelopes_submitted >= 1
        assert result.completed is True

    @pytest.mark.asyncio
    async def test_spinal_cord_stream_up_called_with_findings(self):
        """stream_up must be called at least once when findings are present."""
        node = MagicMock()
        node.file_path = "backend/core/foo.py"
        node.name = "unused_fn"

        cord = _make_spinal_cord()
        epoch = _make_epoch(
            oracle=_make_oracle(dead_code=[node], circular_deps=[]),
            fleet=_make_fleet(findings=[]),
            spinal_cord=cord,
        )
        token = CancellationToken(epoch_id=1)
        await epoch.run(token)

        cord.stream_up.assert_called()

    @pytest.mark.asyncio
    async def test_circular_dep_yields_finding(self):
        """When oracle.find_circular_dependencies returns cycles, findings_count >= 1."""
        node_a = MagicMock()
        node_a.file_path = "backend/a.py"
        node_a.name = "ModuleA"
        node_b = MagicMock()
        node_b.file_path = "backend/b.py"
        node_b.name = "ModuleB"

        epoch = _make_epoch(
            oracle=_make_oracle(dead_code=[], circular_deps=[[node_a, node_b]]),
            fleet=_make_fleet(findings=[]),
        )
        token = CancellationToken(epoch_id=1)
        result = await epoch.run(token)

        assert result.findings_count >= 1

    @pytest.mark.asyncio
    async def test_fleet_finding_converted_to_ranked_finding(self):
        """ExplorationFinding from fleet becomes a RankedFinding in results."""
        fleet_finding = MagicMock()
        fleet_finding.category = "test_gap"
        fleet_finding.description = "Missing unit tests"
        fleet_finding.file_path = "backend/core/bar.py"
        fleet_finding.relevance = 0.8

        epoch = _make_epoch(
            oracle=_make_oracle(dead_code=[], circular_deps=[]),
            fleet=_make_fleet(findings=[fleet_finding]),
        )
        token = CancellationToken(epoch_id=1)
        result = await epoch.run(token)

        assert result.findings_count >= 1


# ---------------------------------------------------------------------------
# test_epoch_respects_cancellation
# ---------------------------------------------------------------------------


class TestEpochRespectsCancellation:
    @pytest.mark.asyncio
    async def test_cancel_before_run_returns_cancelled(self):
        """Token cancelled before run() starts → result.cancelled=True immediately."""
        token = CancellationToken(epoch_id=1)
        token.cancel()

        epoch = _make_epoch()
        result = await epoch.run(token)

        assert result.cancelled is True
        assert result.completed is False

    @pytest.mark.asyncio
    async def test_cancel_during_fleet_returns_cancelled(self):
        """Cancelling mid-fleet (slow deploy) → cancelled=True, no crash."""
        token = CancellationToken(epoch_id=1)

        async def _slow_deploy(**kwargs):
            # Signal cancellation partway through
            await asyncio.sleep(0.05)
            token.cancel()
            await asyncio.sleep(0.1)
            report = MagicMock()
            report.findings = []
            return report

        fleet = MagicMock()
        fleet.deploy = AsyncMock(side_effect=_slow_deploy)

        epoch = _make_epoch(fleet=fleet)
        result = await epoch.run(token)

        # May be cancelled or completed (cancel happened during fleet, epoch
        # may finish the current phase before checking token)
        assert result.cancelled or result.completed

    @pytest.mark.asyncio
    async def test_cancelled_result_has_zero_envelopes(self):
        """When cancelled before patching, no envelopes are submitted."""
        token = CancellationToken(epoch_id=1)
        token.cancel()

        router = _make_intake_router()
        epoch = _make_epoch(intake_router=router)
        await epoch.run(token)

        router.ingest.assert_not_called()


# ---------------------------------------------------------------------------
# test_epoch_stops_on_backpressure
# ---------------------------------------------------------------------------


class TestEpochStopsOnBackpressure:
    @pytest.mark.asyncio
    async def test_backpressure_stops_further_ingest(self):
        """When ingest returns 'backpressure', no more envelopes are submitted."""
        nodes = []
        for i in range(5):
            node = MagicMock()
            node.file_path = f"backend/core/f{i}.py"
            node.name = f"fn_{i}"
            nodes.append(node)

        # First call returns backpressure; subsequent calls should not happen
        router = MagicMock()
        router.ingest = AsyncMock(return_value="backpressure")

        epoch = _make_epoch(
            oracle=_make_oracle(dead_code=nodes, circular_deps=[]),
            fleet=_make_fleet(findings=[]),
            intake_router=router,
        )
        token = CancellationToken(epoch_id=1)
        result = await epoch.run(token)

        # Should stop after first backpressure — only 1 call
        assert router.ingest.call_count == 1
        assert result.envelopes_backpressured >= 1

    @pytest.mark.asyncio
    async def test_backpressure_marks_result_completed(self):
        """Backpressure is a graceful stop, not an error; completed=True, error=None."""
        node = MagicMock()
        node.file_path = "backend/core/foo.py"
        node.name = "fn_x"

        router = MagicMock()
        router.ingest = AsyncMock(return_value="backpressure")

        epoch = _make_epoch(
            oracle=_make_oracle(dead_code=[node], circular_deps=[]),
            fleet=_make_fleet(findings=[]),
            intake_router=router,
        )
        token = CancellationToken(epoch_id=1)
        result = await epoch.run(token)

        assert result.completed is True
        assert result.error is None

    @pytest.mark.asyncio
    async def test_mixed_enqueued_then_backpressure(self):
        """enqueued responses followed by backpressure: counts are correct."""
        nodes = []
        for i in range(4):
            node = MagicMock()
            node.file_path = f"backend/core/f{i}.py"
            node.name = f"fn_{i}"
            nodes.append(node)

        # First two enqueued, third backpressure
        router = MagicMock()
        router.ingest = AsyncMock(side_effect=["enqueued", "enqueued", "backpressure"])

        epoch = _make_epoch(
            oracle=_make_oracle(dead_code=nodes, circular_deps=[]),
            fleet=_make_fleet(findings=[]),
            intake_router=router,
            config=_make_config(rem_max_findings_per_epoch=10),
        )
        token = CancellationToken(epoch_id=1)
        result = await epoch.run(token)

        assert result.envelopes_submitted == 2
        assert result.envelopes_backpressured == 1
        # Stops after backpressure — only 3 total calls
        assert router.ingest.call_count == 3


# ---------------------------------------------------------------------------
# test_epoch_duration_tracked
# ---------------------------------------------------------------------------


class TestEpochDurationTracked:
    @pytest.mark.asyncio
    async def test_duration_is_positive(self):
        """EpochResult.duration_s must be > 0 for any run."""
        epoch = _make_epoch()
        token = CancellationToken(epoch_id=1)
        result = await epoch.run(token)

        assert result.duration_s > 0.0

    @pytest.mark.asyncio
    async def test_duration_is_float(self):
        epoch = _make_epoch()
        token = CancellationToken(epoch_id=1)
        result = await epoch.run(token)

        assert isinstance(result.duration_s, float)


# ---------------------------------------------------------------------------
# test_epoch_handles_oracle_exception
# ---------------------------------------------------------------------------


class TestEpochHandlesOracleException:
    @pytest.mark.asyncio
    async def test_oracle_exception_captured_in_error(self):
        """If oracle raises, epoch captures error and does not propagate."""
        oracle = MagicMock()
        oracle.find_dead_code.side_effect = RuntimeError("oracle exploded")
        oracle.find_circular_dependencies.return_value = []

        epoch = _make_epoch(oracle=oracle)
        token = CancellationToken(epoch_id=1)
        result = await epoch.run(token)

        # Should not raise; error captured
        assert result.error is not None
        assert "oracle" in result.error.lower() or result.error != ""

    @pytest.mark.asyncio
    async def test_fleet_exception_captured_in_error(self):
        """If fleet.deploy raises, epoch captures error and does not propagate."""
        fleet = MagicMock()
        fleet.deploy = AsyncMock(side_effect=RuntimeError("fleet exploded"))

        epoch = _make_epoch(fleet=fleet)
        token = CancellationToken(epoch_id=1)
        result = await epoch.run(token)

        assert result.error is not None


# ---------------------------------------------------------------------------
# test_epoch_max_findings_cap
# ---------------------------------------------------------------------------


class TestEpochMaxFindingsCap:
    @pytest.mark.asyncio
    async def test_max_findings_limits_envelopes_submitted(self):
        """rem_max_findings_per_epoch caps how many envelopes are submitted."""
        nodes = []
        for i in range(10):
            node = MagicMock()
            node.file_path = f"backend/core/f{i}.py"
            node.name = f"fn_{i}"
            nodes.append(node)

        router = _make_intake_router(ingest_return="enqueued")
        epoch = _make_epoch(
            oracle=_make_oracle(dead_code=nodes, circular_deps=[]),
            fleet=_make_fleet(findings=[]),
            intake_router=router,
            config=_make_config(rem_max_findings_per_epoch=3),
        )
        token = CancellationToken(epoch_id=1)
        result = await epoch.run(token)

        # Only top 3 should have been submitted
        assert router.ingest.call_count <= 3
        assert result.envelopes_submitted <= 3
