"""
End-to-End Integration Tests — 397B Model Wiring + DaemonNarrator
==================================================================

Validates the complete pipeline from FeatureSynthesisEngine (with Doubleword
397B model) through to DaemonNarrator voice output.

Tests
-----
- test_synthesis_with_doubleword_produces_hypotheses
    Mock doubleword.prompt_only returns JSON with 2 gaps; verify the model
    hypotheses appear in the synthesize() result with provenance starting
    with "model:".

- test_narrator_speaks_on_synthesis_complete
    DaemonNarrator.on_event("synthesis.complete", {"hypothesis_count": 3})
    triggers say_fn with a message containing "3".

- test_narrator_speaks_on_saga_complete
    DaemonNarrator.on_event("saga.complete", {"title": "WhatsApp Agent"})
    triggers say_fn with a message containing "WhatsApp".

- test_full_flow_synthesis_plus_narrator
    FeatureSynthesisEngine constructed with a narrator kwarg; mock Doubleword
    returns gaps; after synthesize() the narrator's say_fn has been called
    once with a synthesis.complete message.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.ouroboros.daemon_narrator import DaemonNarrator
from backend.core.ouroboros.roadmap.hypothesis import FeatureHypothesis
from backend.core.ouroboros.roadmap.hypothesis_cache import HypothesisCache
from backend.core.ouroboros.roadmap.snapshot import RoadmapSnapshot, SnapshotFragment
from backend.core.ouroboros.roadmap.synthesis_engine import (
    FeatureSynthesisEngine,
    SynthesisConfig,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_fragment(
    source_id: str = "spec:integration-test",
    summary: str = "integration test summary",
) -> SnapshotFragment:
    return SnapshotFragment(
        source_id=source_id,
        uri="docs/integration_test.md",
        tier=0,
        content_hash=hashlib.sha256(summary.encode()).hexdigest(),
        fetched_at=time.time(),
        mtime=time.time(),
        title="Integration Test Spec",
        summary=summary,
        fragment_type="spec",
    )


def _make_snapshot(summary: str = "model wiring integration spec") -> RoadmapSnapshot:
    fragment = _make_fragment(summary=summary)
    return RoadmapSnapshot.create(fragments=(fragment,))


def _make_mock_oracle() -> MagicMock:
    """Oracle that always reports no existing symbols — every reference is a gap."""
    oracle = MagicMock()
    oracle.find_nodes_by_name.return_value = []
    return oracle


def _make_doubleword_mock(
    response_json: Optional[dict] = None,
    raises: Optional[Exception] = None,
) -> MagicMock:
    """Return a mock doubleword client with an async prompt_only coroutine.

    If *raises* is given the coroutine raises that exception.
    Otherwise it returns JSON-serialised *response_json* (or empty string).
    """
    dw = MagicMock()
    if raises is not None:
        dw.prompt_only = AsyncMock(side_effect=raises)
    else:
        raw = json.dumps(response_json) if response_json is not None else ""
        dw.prompt_only = AsyncMock(return_value=raw)
    return dw


def _two_gap_payload() -> dict:
    """A Doubleword response with 2 syntactically valid gaps."""
    return {
        "gaps": [
            {
                "description": "Missing WhatsApp agent integration",
                "evidence_fragments": ["spec:whatsapp"],
                "gap_type": "missing_capability",
                "confidence": 0.82,
                "urgency": "high",
                "suggested_scope": "new-agent",
                "suggested_repos": ["JARVIS-AI-Agent"],
            },
            {
                "description": "Incomplete wiring for calendar sync provider",
                "evidence_fragments": ["spec:calendar"],
                "gap_type": "incomplete_wiring",
                "confidence": 0.70,
                "urgency": "medium",
                "suggested_scope": "wire-existing",
                "suggested_repos": [],
            },
        ]
    }


def _make_cache_mock(
    get_if_valid_return: Optional[List[FeatureHypothesis]] = None,
    load_return: Optional[List[FeatureHypothesis]] = None,
) -> MagicMock:
    cache = MagicMock()
    cache.get_if_valid.return_value = get_if_valid_return
    cache.load.return_value = load_return if load_return is not None else []
    cache.save.return_value = None
    return cache


def _make_say_fn() -> AsyncMock:
    """Return an async mock for DaemonNarrator.say_fn that records calls."""
    return AsyncMock(return_value=True)


# ---------------------------------------------------------------------------
# test_synthesis_with_doubleword_produces_hypotheses
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_synthesis_with_doubleword_produces_hypotheses(tmp_path: Path) -> None:
    """Mock Doubleword returns JSON with 2 gaps; both model hypotheses appear in
    synthesize() output with provenance starting with "model:".

    Tier 0 is patched to return no hints so that all hypotheses in the result
    come exclusively from the Doubleword mock, making assertions unambiguous.
    """
    snapshot = _make_snapshot()
    cache = HypothesisCache(cache_dir=tmp_path / "cache")
    dw = _make_doubleword_mock(response_json=_two_gap_payload())

    engine = FeatureSynthesisEngine(
        oracle=_make_mock_oracle(),
        doubleword=dw,
        cache=cache,
        config=SynthesisConfig(min_interval_s=0),
    )

    # Patch Tier 0 to return nothing so model hypotheses are unambiguous
    with patch(
        "backend.core.ouroboros.roadmap.synthesis_engine.generate_tier0_hints",
        return_value=[],
    ):
        result = await engine.synthesize(snapshot, force=True)

    # prompt_only must have been awaited exactly once
    dw.prompt_only.assert_awaited_once()

    # Both model hypotheses must be present
    model_hyps = [h for h in result if h.provenance.startswith("model:")]
    assert len(model_hyps) == 2, (
        f"Expected 2 model hypotheses, got {len(model_hyps)}: "
        f"{[h.description for h in model_hyps]}"
    )

    descriptions = [h.description for h in model_hyps]
    assert any("WhatsApp" in d for d in descriptions), (
        "Expected a WhatsApp hypothesis"
    )
    assert any("calendar" in d.lower() for d in descriptions), (
        "Expected a calendar hypothesis"
    )

    # Every model hypothesis must have the doubleword provenance prefix
    for h in model_hyps:
        assert h.provenance.startswith("model:"), (
            f"provenance {h.provenance!r} does not start with 'model:'"
        )
        assert h.confidence_rule_id == "model_inference"


# ---------------------------------------------------------------------------
# test_narrator_speaks_on_synthesis_complete
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_narrator_speaks_on_synthesis_complete() -> None:
    """DaemonNarrator.on_event('synthesis.complete', {'hypothesis_count': 3}) calls
    say_fn once and the spoken message contains '3'.
    """
    say = _make_say_fn()
    narrator = DaemonNarrator(say_fn=say, rate_limit_s=0.0)

    await narrator.on_event("synthesis.complete", {"hypothesis_count": 3})

    say.assert_called_once()
    message: str = say.call_args[0][0]
    assert "3" in message, f"Expected '3' in message, got: {message!r}"


# ---------------------------------------------------------------------------
# test_narrator_speaks_on_saga_complete
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_narrator_speaks_on_saga_complete() -> None:
    """DaemonNarrator.on_event('saga.complete', {'title': 'WhatsApp Agent'}) calls
    say_fn once and the spoken message contains 'WhatsApp'.
    """
    say = _make_say_fn()
    narrator = DaemonNarrator(say_fn=say, rate_limit_s=0.0)

    await narrator.on_event("saga.complete", {"title": "WhatsApp Agent"})

    say.assert_called_once()
    message: str = say.call_args[0][0]
    assert "WhatsApp" in message, f"Expected 'WhatsApp' in message, got: {message!r}"


# ---------------------------------------------------------------------------
# test_full_flow_synthesis_plus_narrator
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_full_flow_synthesis_plus_narrator(tmp_path: Path) -> None:
    """Full pipeline: FeatureSynthesisEngine with narrator param; after synthesize()
    the narrator's say_fn is called once with a synthesis.complete message that
    reflects the actual hypothesis count returned by the model mock.
    """
    snapshot = _make_snapshot()
    cache = HypothesisCache(cache_dir=tmp_path / "cache")
    dw = _make_doubleword_mock(response_json=_two_gap_payload())

    say = _make_say_fn()
    narrator = DaemonNarrator(say_fn=say, rate_limit_s=0.0)

    engine = FeatureSynthesisEngine(
        oracle=_make_mock_oracle(),
        doubleword=dw,
        cache=cache,
        config=SynthesisConfig(min_interval_s=0),
        narrator=narrator,
    )

    # Tier 0 returns nothing so count comes entirely from model gaps
    with patch(
        "backend.core.ouroboros.roadmap.synthesis_engine.generate_tier0_hints",
        return_value=[],
    ):
        result = await engine.synthesize(snapshot, force=True)

    # Narrator must have fired exactly once (synthesis.complete)
    say.assert_called_once()

    # The spoken message must mention the hypothesis count
    message: str = say.call_args[0][0]
    hypothesis_count = len(result)
    assert str(hypothesis_count) in message, (
        f"Expected '{hypothesis_count}' in narrator message, got: {message!r}"
    )

    # say_fn call contract — source and skip_dedup kwargs are forwarded
    _, kwargs = say.call_args
    assert kwargs.get("source") == "ouroboros_narrator"
    assert kwargs.get("skip_dedup") is True
