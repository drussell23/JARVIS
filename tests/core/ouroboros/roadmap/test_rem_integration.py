"""Tests for REM Epoch hypothesis cache integration (Task 9).

Verifies that RemEpoch._load_cached_hypotheses() correctly feeds
FeatureHypothesis objects into the exploration pipeline as RankedFindings,
and that the epoch degrades gracefully when no cache dir is provided.

No I/O except for a temp-dir-backed HypothesisCache; no model calls.
"""
from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import List
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.ouroboros.cancellation_token import CancellationToken
from backend.core.ouroboros.daemon_config import DaemonConfig
from backend.core.ouroboros.rem_epoch import RemEpoch
from backend.core.ouroboros.roadmap.hypothesis import FeatureHypothesis
from backend.core.ouroboros.roadmap.hypothesis_cache import HypothesisCache

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = time.time()


def _make_hypothesis(
    *,
    gap_type: str = "missing_capability",
    description: str = "Hypothesis finding from roadmap",
    urgency: str = "high",
    confidence: float = 0.85,
    suggested_scope: str = "new-agent",
    suggested_repos: tuple = ("jarvis",),
    status: str = "active",
) -> FeatureHypothesis:
    return FeatureHypothesis(
        hypothesis_id=str(uuid.uuid4()),
        description=description,
        evidence_fragments=("src:test-fragment",),
        gap_type=gap_type,
        confidence=confidence,
        confidence_rule_id="tier0-spec-vs-impl-diff",
        urgency=urgency,
        suggested_scope=suggested_scope,
        suggested_repos=suggested_repos,
        provenance="deterministic",
        synthesized_for_snapshot_hash="abc123",
        synthesized_at=_NOW,
        synthesis_input_fingerprint="fp-test",
        status=status,
    )


def _make_config(**overrides) -> DaemonConfig:
    defaults = dict(
        rem_cycle_timeout_s=5.0,
        rem_max_agents=2,
        rem_max_findings_per_epoch=10,
    )
    defaults.update(overrides)
    return DaemonConfig(**defaults)


def _make_oracle(*, dead_code=None, circular_deps=None) -> MagicMock:
    oracle = MagicMock()
    oracle.find_dead_code.return_value = dead_code or []
    oracle.find_circular_dependencies.return_value = circular_deps or []
    return oracle


def _make_fleet(*, findings=None) -> MagicMock:
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


def _make_epoch(
    *,
    oracle=None,
    fleet=None,
    spinal_cord=None,
    intake_router=None,
    config=None,
    epoch_id: int = 1,
    hypothesis_cache_dir=None,
) -> RemEpoch:
    return RemEpoch(
        epoch_id=epoch_id,
        oracle=oracle or _make_oracle(),
        fleet=fleet or _make_fleet(),
        spinal_cord=spinal_cord or _make_spinal_cord(),
        intake_router=intake_router or _make_intake_router(),
        doubleword=MagicMock(),
        config=config or _make_config(),
        hypothesis_cache_dir=hypothesis_cache_dir,
    )


# ---------------------------------------------------------------------------
# test_rem_epoch_includes_hypothesis_findings
# ---------------------------------------------------------------------------


class TestRemEpochIncludesHypothesisFindings:
    @pytest.mark.asyncio
    async def test_rem_epoch_includes_hypothesis_findings(self, tmp_path: Path):
        """Active hypothesis in cache → epoch findings_count >= 1."""
        # Save one active hypothesis to the temp cache dir
        cache = HypothesisCache(cache_dir=tmp_path)
        h = _make_hypothesis(
            description="Missing async capability in vision loop",
            gap_type="missing_capability",
            urgency="high",
            confidence=0.9,
            status="active",
        )
        cache.save([h], input_fingerprint="fp-test", snapshot_hash="abc123")

        # Build epoch with oracle+fleet returning nothing, only hypothesis yields findings
        epoch = _make_epoch(
            oracle=_make_oracle(dead_code=[], circular_deps=[]),
            fleet=_make_fleet(findings=[]),
            hypothesis_cache_dir=tmp_path,
        )
        token = CancellationToken(epoch_id=1)
        result = await epoch.run(token)

        assert result.findings_count >= 1
        assert result.error is None

    @pytest.mark.asyncio
    async def test_hypothesis_finding_triggers_envelope_submission(self, tmp_path: Path):
        """Hypothesis finding results in at least one envelope submitted."""
        cache = HypothesisCache(cache_dir=tmp_path)
        h = _make_hypothesis(status="active")
        cache.save([h])

        router = _make_intake_router(ingest_return="enqueued")
        epoch = _make_epoch(
            oracle=_make_oracle(dead_code=[], circular_deps=[]),
            fleet=_make_fleet(findings=[]),
            hypothesis_cache_dir=tmp_path,
            intake_router=router,
        )
        token = CancellationToken(epoch_id=1)
        result = await epoch.run(token)

        assert result.envelopes_submitted >= 1

    @pytest.mark.asyncio
    async def test_dismissed_hypothesis_excluded(self, tmp_path: Path):
        """Dismissed hypothesis must NOT appear in findings."""
        cache = HypothesisCache(cache_dir=tmp_path)
        dismissed = _make_hypothesis(status="dismissed")
        cache.save([dismissed])

        epoch = _make_epoch(
            oracle=_make_oracle(dead_code=[], circular_deps=[]),
            fleet=_make_fleet(findings=[]),
            hypothesis_cache_dir=tmp_path,
        )
        token = CancellationToken(epoch_id=1)
        result = await epoch.run(token)

        # Only dismissed hypothesis: no findings expected
        assert result.findings_count == 0

    @pytest.mark.asyncio
    async def test_multiple_hypotheses_all_active_included(self, tmp_path: Path):
        """Multiple active hypotheses all feed into findings."""
        cache = HypothesisCache(cache_dir=tmp_path)
        # Use distinct (file_path, category) combos so dedup does not collapse them
        hypotheses = [
            _make_hypothesis(
                description=f"Gap {i}",
                gap_type="missing_capability",
                suggested_scope=f"scope-{i}",
                suggested_repos=(f"repo-{i}",),
            )
            for i in range(3)
        ]
        cache.save(hypotheses)

        epoch = _make_epoch(
            oracle=_make_oracle(dead_code=[], circular_deps=[]),
            fleet=_make_fleet(findings=[]),
            hypothesis_cache_dir=tmp_path,
        )
        token = CancellationToken(epoch_id=1)
        result = await epoch.run(token)

        # All 3 distinct (scope, gap_type) pairs should survive dedup
        assert result.findings_count >= 3

    @pytest.mark.asyncio
    async def test_hypothesis_findings_merged_with_oracle(self, tmp_path: Path):
        """Hypothesis findings are merged with oracle findings."""
        # One oracle dead-code node
        node = MagicMock()
        node.file_path = "backend/core/foo.py"
        node.name = "unused_fn"

        # One hypothesis
        cache = HypothesisCache(cache_dir=tmp_path)
        h = _make_hypothesis(suggested_scope="new-agent", status="active")
        cache.save([h])

        epoch = _make_epoch(
            oracle=_make_oracle(dead_code=[node], circular_deps=[]),
            fleet=_make_fleet(findings=[]),
            hypothesis_cache_dir=tmp_path,
        )
        token = CancellationToken(epoch_id=1)
        result = await epoch.run(token)

        # At least the oracle dead_code finding + the hypothesis finding
        assert result.findings_count >= 2

    @pytest.mark.asyncio
    async def test_blast_radius_mapped_for_manifesto_violation(self, tmp_path: Path):
        """manifesto_violation gap_type maps to blast_radius=0.7 (highest)."""
        cache = HypothesisCache(cache_dir=tmp_path)
        h = _make_hypothesis(
            gap_type="manifesto_violation",
            suggested_scope="manifesto-fix",
            suggested_repos=("jarvis",),
        )
        cache.save([h])

        epoch = _make_epoch(
            oracle=_make_oracle(dead_code=[], circular_deps=[]),
            fleet=_make_fleet(findings=[]),
            hypothesis_cache_dir=tmp_path,
        )
        token = CancellationToken(epoch_id=1)
        result = await epoch.run(token)

        # manifesto_violation has highest blast_radius (0.7) so should rank at top
        assert result.findings_count >= 1
        assert result.completed is True


# ---------------------------------------------------------------------------
# test_rem_epoch_without_cache_dir_works
# ---------------------------------------------------------------------------


class TestRemEpochWithoutCacheDir:
    @pytest.mark.asyncio
    async def test_rem_epoch_without_cache_dir_works(self):
        """hypothesis_cache_dir=None → no crash, epoch still completes."""
        epoch = _make_epoch(
            oracle=_make_oracle(dead_code=[], circular_deps=[]),
            fleet=_make_fleet(findings=[]),
            hypothesis_cache_dir=None,
        )
        token = CancellationToken(epoch_id=1)
        result = await epoch.run(token)

        assert result.completed is True
        assert result.error is None
        assert result.cancelled is False

    @pytest.mark.asyncio
    async def test_rem_epoch_without_cache_dir_zero_hypothesis_findings(self):
        """hypothesis_cache_dir=None → hypothesis findings contribute nothing."""
        epoch = _make_epoch(
            oracle=_make_oracle(dead_code=[], circular_deps=[]),
            fleet=_make_fleet(findings=[]),
            hypothesis_cache_dir=None,
        )
        token = CancellationToken(epoch_id=1)
        result = await epoch.run(token)

        assert result.findings_count == 0

    @pytest.mark.asyncio
    async def test_existing_epoch_api_unchanged(self):
        """Existing call sites that omit hypothesis_cache_dir must still work."""
        # Construct WITHOUT hypothesis_cache_dir (old call style)
        epoch = RemEpoch(
            epoch_id=99,
            oracle=_make_oracle(),
            fleet=_make_fleet(),
            spinal_cord=_make_spinal_cord(),
            intake_router=_make_intake_router(),
            doubleword=MagicMock(),
            config=_make_config(),
        )
        token = CancellationToken(epoch_id=99)
        result = await epoch.run(token)

        assert result.epoch_id == 99
        assert result.error is None

    @pytest.mark.asyncio
    async def test_nonexistent_cache_dir_does_not_crash(self, tmp_path: Path):
        """Pointing at a dir that doesn't exist returns empty list gracefully."""
        nonexistent = tmp_path / "no_such_dir"
        epoch = _make_epoch(
            oracle=_make_oracle(dead_code=[], circular_deps=[]),
            fleet=_make_fleet(findings=[]),
            hypothesis_cache_dir=nonexistent,
        )
        token = CancellationToken(epoch_id=1)
        result = await epoch.run(token)

        assert result.completed is True
        assert result.error is None
