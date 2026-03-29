"""
Tests for FeatureSynthesisEngine — Doubleword 397B integration (Task 3)
=========================================================================

Coverage:
- test_synthesis_calls_doubleword: mock prompt_only returns valid JSON with 1
  gap; verify the model hypothesis appears in the synthesize() result.
- test_synthesis_falls_back_on_doubleword_failure: prompt_only raises; verify
  tier0 hypotheses are still returned.
- test_synthesis_without_doubleword: doubleword=None; verify tier0 only.
- test_parse_doubleword_response_valid: unit-test the parser directly.
- test_parse_doubleword_response_empty: empty string → empty list, no raise.
- test_parse_doubleword_response_invalid_json: bad JSON → empty list, no raise.
- test_parse_doubleword_response_missing_gaps_key: JSON without "gaps" → empty.
- test_parse_doubleword_response_skips_malformed_items: malformed items
  in "gaps" are skipped; valid items still returned.
- test_dedup_deterministic_wins: model hypothesis with same fingerprint as
  tier0 hypothesis is dropped in favour of the deterministic entry.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.ouroboros.roadmap.hypothesis import FeatureHypothesis
from backend.core.ouroboros.roadmap.snapshot import RoadmapSnapshot, SnapshotFragment
from backend.core.ouroboros.roadmap.synthesis_engine import (
    FeatureSynthesisEngine,
    SynthesisConfig,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fragment(source_id: str = "spec:test", summary: str = "test summary") -> SnapshotFragment:
    return SnapshotFragment(
        source_id=source_id,
        uri="docs/test.md",
        tier=0,
        content_hash=hashlib.sha256(summary.encode()).hexdigest(),
        fetched_at=time.time(),
        mtime=time.time(),
        title="Test Spec",
        summary=summary,
        fragment_type="spec",
    )


def _make_snapshot(summary: str = "analytics agent spec") -> RoadmapSnapshot:
    fragment = _make_fragment(summary=summary)
    return RoadmapSnapshot.create(fragments=(fragment,))


def _make_hypothesis(snapshot_hash: str = "abc123", provenance: str = "deterministic") -> FeatureHypothesis:
    return FeatureHypothesis.new(
        description="Missing agent: analytics",
        evidence_fragments=("spec:test",),
        gap_type="missing_capability",
        confidence=0.85,
        confidence_rule_id="spec_symbol_miss",
        urgency="medium",
        suggested_scope="new-agent",
        suggested_repos=(),
        provenance=provenance,
        synthesized_for_snapshot_hash=snapshot_hash,
        synthesis_input_fingerprint="fp_" + snapshot_hash[:8],
    )


def _make_mock_cache(
    get_if_valid_return: Optional[List[FeatureHypothesis]] = None,
    load_return: Optional[List[FeatureHypothesis]] = None,
) -> MagicMock:
    cache = MagicMock()
    cache.get_if_valid.return_value = get_if_valid_return
    cache.load.return_value = load_return if load_return is not None else []
    cache.save.return_value = None
    return cache


def _make_mock_oracle() -> MagicMock:
    oracle = MagicMock()
    oracle.find_nodes_by_name.return_value = []
    return oracle


def _make_doubleword_mock(response_json: Optional[dict] = None, raises: Optional[Exception] = None) -> MagicMock:
    """Return a mock with a prompt_only coroutine.

    If *raises* is provided the coroutine raises that exception.
    Otherwise it returns the JSON-serialised *response_json* (or empty string).
    """
    dw = MagicMock()
    if raises is not None:
        dw.prompt_only = AsyncMock(side_effect=raises)
    else:
        raw = json.dumps(response_json) if response_json is not None else ""
        dw.prompt_only = AsyncMock(return_value=raw)
    return dw


def _valid_gap_payload(description: str = "Wiring gap in analytics agent") -> dict:
    return {
        "gaps": [
            {
                "description": description,
                "evidence_fragments": ["spec:analytics"],
                "gap_type": "incomplete_wiring",
                "confidence": 0.78,
                "urgency": "high",
                "suggested_scope": "wire-existing",
                "suggested_repos": ["JARVIS-AI-Agent"],
            }
        ]
    }


def _make_engine(
    cache: Optional[MagicMock] = None,
    oracle: Optional[MagicMock] = None,
    doubleword: Optional[MagicMock] = None,
    config: Optional[SynthesisConfig] = None,
) -> FeatureSynthesisEngine:
    return FeatureSynthesisEngine(
        oracle=oracle or _make_mock_oracle(),
        doubleword=doubleword,
        cache=cache or _make_mock_cache(),
        config=config or SynthesisConfig(),
    )


# ---------------------------------------------------------------------------
# test_synthesis_calls_doubleword
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_synthesis_calls_doubleword():
    """When doubleword is provided and returns a valid JSON payload with 1 gap,
    the resulting model hypothesis appears in synthesize()'s output.
    """
    snapshot = _make_snapshot()
    cache = _make_mock_cache(get_if_valid_return=None, load_return=[])
    dw = _make_doubleword_mock(response_json=_valid_gap_payload())

    tier0_hint = _make_hypothesis(snapshot.content_hash)

    with patch(
        "backend.core.ouroboros.roadmap.synthesis_engine.generate_tier0_hints",
        return_value=[tier0_hint],
    ):
        engine = _make_engine(cache=cache, doubleword=dw)
        result = await engine.synthesize(snapshot)

    # prompt_only must have been called exactly once
    dw.prompt_only.assert_awaited_once()

    # The model hypothesis should be in the result (unique fingerprint from tier0)
    model_hyps = [h for h in result if h.provenance == "model:doubleword-397b"]
    assert len(model_hyps) == 1, "Expected exactly 1 model hypothesis"
    assert model_hyps[0].gap_type == "incomplete_wiring"
    assert model_hyps[0].confidence_rule_id == "model_inference"
    assert model_hyps[0].urgency == "high"
    assert model_hyps[0].suggested_repos == ("JARVIS-AI-Agent",)


# ---------------------------------------------------------------------------
# test_synthesis_falls_back_on_doubleword_failure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_synthesis_falls_back_on_doubleword_failure():
    """When prompt_only raises, synthesize() falls back gracefully and still
    returns the tier0 hypotheses.
    """
    snapshot = _make_snapshot()
    cache = _make_mock_cache(get_if_valid_return=None, load_return=[])
    dw = _make_doubleword_mock(raises=RuntimeError("network error"))

    tier0_hint = _make_hypothesis(snapshot.content_hash)

    with patch(
        "backend.core.ouroboros.roadmap.synthesis_engine.generate_tier0_hints",
        return_value=[tier0_hint],
    ):
        engine = _make_engine(cache=cache, doubleword=dw)
        result = await engine.synthesize(snapshot)

    # Tier 0 still present
    assert len(result) == 1
    assert result[0].provenance == "deterministic"

    # No model hypotheses
    model_hyps = [h for h in result if h.provenance.startswith("model:")]
    assert model_hyps == []


# ---------------------------------------------------------------------------
# test_synthesis_without_doubleword
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_synthesis_without_doubleword():
    """When doubleword=None, synthesize() returns tier0 hypotheses only."""
    snapshot = _make_snapshot()
    cache = _make_mock_cache(get_if_valid_return=None, load_return=[])

    tier0_hint = _make_hypothesis(snapshot.content_hash)

    with patch(
        "backend.core.ouroboros.roadmap.synthesis_engine.generate_tier0_hints",
        return_value=[tier0_hint],
    ):
        engine = _make_engine(cache=cache, doubleword=None)
        result = await engine.synthesize(snapshot)

    assert len(result) == 1
    assert result[0].provenance == "deterministic"


# ---------------------------------------------------------------------------
# _parse_doubleword_response — unit tests
# ---------------------------------------------------------------------------

def test_parse_doubleword_response_valid():
    """Valid JSON with 1 gap produces 1 FeatureHypothesis with correct fields."""
    snapshot = _make_snapshot()
    engine = _make_engine()

    payload = _valid_gap_payload("Analytics wiring gap")
    result = engine._parse_doubleword_response(json.dumps(payload), snapshot)

    assert len(result) == 1
    h = result[0]
    assert h.description == "Analytics wiring gap"
    assert h.provenance == "model:doubleword-397b"
    assert h.confidence_rule_id == "model_inference"
    assert h.gap_type == "incomplete_wiring"
    assert h.synthesized_for_snapshot_hash == snapshot.content_hash


def test_parse_doubleword_response_empty():
    """Empty string returns an empty list without raising."""
    snapshot = _make_snapshot()
    engine = _make_engine()
    result = engine._parse_doubleword_response("", snapshot)
    assert result == []


def test_parse_doubleword_response_invalid_json():
    """Non-JSON response returns an empty list without raising."""
    snapshot = _make_snapshot()
    engine = _make_engine()
    result = engine._parse_doubleword_response("not valid json {{", snapshot)
    assert result == []


def test_parse_doubleword_response_missing_gaps_key():
    """JSON that lacks the 'gaps' array returns an empty list."""
    snapshot = _make_snapshot()
    engine = _make_engine()
    result = engine._parse_doubleword_response(json.dumps({"hypotheses": []}), snapshot)
    assert result == []


def test_parse_doubleword_response_skips_malformed_items():
    """Items missing required 'description' or with an invalid gap_type are
    skipped; remaining valid items are still returned.
    """
    snapshot = _make_snapshot()
    engine = _make_engine()

    payload = {
        "gaps": [
            # Valid item
            {
                "description": "Valid gap",
                "evidence_fragments": [],
                "gap_type": "missing_capability",
                "confidence": 0.9,
                "urgency": "high",
                "suggested_scope": "new-agent",
                "suggested_repos": [],
            },
            # Missing 'description' key — should be skipped
            {
                "evidence_fragments": [],
                "gap_type": "incomplete_wiring",
                "confidence": 0.5,
                "urgency": "low",
                "suggested_scope": "refactor",
                "suggested_repos": [],
            },
            # Invalid gap_type — FeatureHypothesis.__post_init__ raises ValueError
            {
                "description": "Bad gap type",
                "evidence_fragments": [],
                "gap_type": "unknown_type",
                "confidence": 0.5,
                "urgency": "low",
                "suggested_scope": "refactor",
                "suggested_repos": [],
            },
        ]
    }

    result = engine._parse_doubleword_response(json.dumps(payload), snapshot)

    assert len(result) == 1
    assert result[0].description == "Valid gap"
    assert result[0].provenance == "model:doubleword-397b"


# ---------------------------------------------------------------------------
# test_dedup_deterministic_wins
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dedup_deterministic_wins():
    """When tier0 and model return the same logical hypothesis (same fingerprint),
    the deterministic entry wins in the merged output.
    """
    snapshot = _make_snapshot()
    cache = _make_mock_cache(get_if_valid_return=None, load_return=[])

    # Build a model response whose description + evidence + gap_type will
    # produce the same fingerprint as the tier0 hint below.
    description = "Missing agent: analytics"
    evidence = ("spec:test",)
    gap_type = "missing_capability"

    tier0_hint = FeatureHypothesis.new(
        description=description,
        evidence_fragments=evidence,
        gap_type=gap_type,
        confidence=0.85,
        confidence_rule_id="spec_symbol_miss",
        urgency="medium",
        suggested_scope="new-agent",
        suggested_repos=(),
        provenance="deterministic",
        synthesized_for_snapshot_hash=snapshot.content_hash,
        synthesis_input_fingerprint="fp_t0",
    )

    # Model payload with the exact same description/evidence/gap_type → same fingerprint
    dw_payload = {
        "gaps": [
            {
                "description": description,
                "evidence_fragments": list(evidence),
                "gap_type": gap_type,
                "confidence": 0.60,
                "urgency": "low",
                "suggested_scope": "refactor",
                "suggested_repos": [],
            }
        ]
    }
    dw = _make_doubleword_mock(response_json=dw_payload)

    with patch(
        "backend.core.ouroboros.roadmap.synthesis_engine.generate_tier0_hints",
        return_value=[tier0_hint],
    ):
        engine = _make_engine(cache=cache, doubleword=dw)
        result = await engine.synthesize(snapshot)

    # Only one entry in the result (deduped)
    assert len(result) == 1
    # The deterministic entry wins
    assert result[0].provenance == "deterministic"
    assert result[0].confidence_rule_id == "spec_symbol_miss"
