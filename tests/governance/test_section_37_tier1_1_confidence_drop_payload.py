"""§37 Tier 1 #1 — Confidence-drop SSE producer payload enrichment.

The substrate (`confidence_sse_producer.py` + DW provider wiring at
`doubleword_provider.py:1365-1390`) was structurally complete but
the SSE payload omitted the producer's transition context fields
(``prior_verdict`` + ``consecutive_below``). Operators downstream
saw "BELOW_FLOOR fired" but couldn't distinguish:

  * **Fresh OK→BELOW collapse** (sudden — model degraded
    discontinuously; warrants immediate route escalation)
  * **APPROACHING→BELOW progression** (predicted — early-warning
    fired previously; confirms the slide; warrants posture nudge
    not panic)

This test spine pins the v2.83 closure: producer threads
``prior_verdict`` + ``consecutive_below`` from ``TransitionResult``
into the canonical ``publish_confidence_*`` helpers, which surface
them in the SSE payload via ``_build_confidence_payload``.
Backward-compat preserved — legacy callers omit the new kwargs and
get default values that don't break consumers reading the existing
schema fields.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.verification import (
    confidence_observability,
    confidence_sse_producer,
)


_REPO_ROOT = Path(__file__).resolve().parents[2]


# -----------------------------------------------------------------
# Payload schema — _build_confidence_payload must emit
# transition-context fields
# -----------------------------------------------------------------


def test_build_confidence_payload_includes_prior_verdict():
    """Payload schema MUST carry ``prior_verdict`` field — load-
    bearing for operator transition-context analysis."""
    payload = confidence_observability._build_confidence_payload(
        verdict="below_floor",
        prior_verdict="ok",
        consecutive_below=1,
        op_id="op-test",
    )
    assert "prior_verdict" in payload
    assert payload["prior_verdict"] == "ok"


def test_build_confidence_payload_includes_consecutive_below():
    payload = confidence_observability._build_confidence_payload(
        verdict="below_floor",
        prior_verdict="approaching_floor",
        consecutive_below=3,
        op_id="op-test",
    )
    assert "consecutive_below" in payload
    assert payload["consecutive_below"] == 3


def test_build_confidence_payload_legacy_callers_get_defaults():
    """Legacy callers (no transition-context kwargs) get defaults
    that preserve schema — empty string for prior_verdict, 0 for
    consecutive_below — never KeyError-able."""
    payload = confidence_observability._build_confidence_payload(
        verdict="below_floor",
        op_id="op-legacy",
    )
    assert payload["prior_verdict"] == ""
    assert payload["consecutive_below"] == 0


def test_build_confidence_payload_handles_enum_prior_verdict():
    """Producer passes ConfidenceVerdict.<NAME>.value (a str), but
    the helper must also handle a raw enum permissively."""
    from backend.core.ouroboros.governance.verification.confidence_monitor import (  # noqa: E501
        ConfidenceVerdict,
    )
    payload = confidence_observability._build_confidence_payload(
        verdict=ConfidenceVerdict.BELOW_FLOOR,
        prior_verdict=ConfidenceVerdict.APPROACHING_FLOOR,
    )
    assert payload["prior_verdict"] == "approaching_floor"


def test_build_confidence_payload_handles_none_prior_verdict():
    payload = confidence_observability._build_confidence_payload(
        verdict="below_floor",
        prior_verdict=None,
    )
    assert payload["prior_verdict"] == ""


# -----------------------------------------------------------------
# Publish helpers accept the new kwargs (signature pin)
# -----------------------------------------------------------------


def test_publish_drop_event_accepts_prior_verdict_kwarg():
    sig = inspect.signature(
        confidence_observability.publish_confidence_drop_event,
    )
    assert "prior_verdict" in sig.parameters
    assert "consecutive_below" in sig.parameters


def test_publish_approaching_event_accepts_prior_verdict_kwarg():
    sig = inspect.signature(
        confidence_observability.publish_confidence_approaching_event,
    )
    assert "prior_verdict" in sig.parameters
    assert "consecutive_below" in sig.parameters


# -----------------------------------------------------------------
# AST pin — producer threads transition context to publishers
# -----------------------------------------------------------------


def test_producer_threads_prior_verdict_to_drop_publisher():
    """Bytes-pin: when ``FireDecision.FIRED_DROP`` fires, the
    publish block MUST pass ``prior_verdict=prior.value`` to
    ``_safe_publish_drop``. Drift here silently strips operator
    context. Anchored on the publish block (which follows the
    'Publish OUTSIDE the lock' comment) — the stats-counting
    block also branches on FIRED_DROP but doesn't publish."""
    src = Path(
        inspect.getfile(confidence_sse_producer)
    ).read_text(encoding="utf-8")
    # Anchor on the publish-block region — the only one that
    # invokes ``_safe_publish_drop``.
    publish_region_match = re.search(
        r"_safe_publish_drop\(.*?\)",
        src, re.DOTALL,
    )
    assert publish_region_match, "_safe_publish_drop call not found"
    region = publish_region_match.group(0)
    assert "prior_verdict=prior.value" in region, (
        "_safe_publish_drop call must pass prior_verdict=prior.value"
    )
    assert "consecutive_below=consecutive_below_snapshot" in region, (
        "_safe_publish_drop call must pass "
        "consecutive_below=consecutive_below_snapshot"
    )


def test_producer_threads_prior_verdict_to_approaching_publisher():
    src = Path(
        inspect.getfile(confidence_sse_producer)
    ).read_text(encoding="utf-8")
    publish_region_match = re.search(
        r"_safe_publish_approaching\(.*?\)",
        src, re.DOTALL,
    )
    assert publish_region_match, (
        "_safe_publish_approaching call not found"
    )
    region = publish_region_match.group(0)
    assert "prior_verdict=prior.value" in region
    assert "consecutive_below=consecutive_below_snapshot" in region


# -----------------------------------------------------------------
# End-to-end: producer fires drop → publisher receives transition
# context → SSE payload carries it
# -----------------------------------------------------------------


@pytest.fixture
def _producer_master_on(monkeypatch):
    monkeypatch.setenv("JARVIS_CONFIDENCE_SSE_PRODUCER_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CONFIDENCE_OBSERVABILITY_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_IDE_OBSERVABILITY_STREAM_ENABLED", "true",
    )
    confidence_sse_producer.reset_default_tracker_for_tests()
    yield
    confidence_sse_producer.reset_default_tracker_for_tests()


def test_producer_fresh_drop_threads_prior_ok(
    _producer_master_on, monkeypatch,
):
    """OK → BELOW_FLOOR transition (fresh drop): publisher receives
    prior_verdict='ok'."""
    from backend.core.ouroboros.governance.verification.confidence_monitor import (  # noqa: E501
        ConfidenceVerdict,
    )
    captured: list[dict] = []

    def _capture_drop(**kwargs):
        captured.append(dict(kwargs))
        return "frame-fake"

    # Inject capturing publisher into the singleton tracker
    tracker = confidence_sse_producer.get_default_tracker()
    monkeypatch.setattr(
        tracker, "_safe_publish_drop", _capture_drop,
    )

    # Fresh op: prior=OK by default
    result = tracker.observe_verdict(
        op_id="op-fresh",
        verdict=ConfidenceVerdict.BELOW_FLOOR,
        rolling_margin=-0.5,
        floor=0.1,
    )
    assert result is not None
    assert result.prior_verdict == "ok"
    assert result.current_verdict == "below_floor"
    assert len(captured) == 1
    assert captured[0]["prior_verdict"] == "ok"
    assert captured[0]["consecutive_below"] == 1


def test_producer_progression_drop_threads_prior_approaching(
    _producer_master_on, monkeypatch,
):
    """APPROACHING_FLOOR → BELOW_FLOOR transition (progression):
    publisher receives prior_verdict='approaching_floor'."""
    from backend.core.ouroboros.governance.verification.confidence_monitor import (  # noqa: E501
        ConfidenceVerdict,
    )
    captured: list[dict] = []

    def _capture_drop(**kwargs):
        captured.append(dict(kwargs))
        return "frame-fake"

    def _capture_approaching(**kwargs):
        return "frame-approaching"

    tracker = confidence_sse_producer.get_default_tracker()
    monkeypatch.setattr(tracker, "_safe_publish_drop", _capture_drop)
    monkeypatch.setattr(
        tracker, "_safe_publish_approaching", _capture_approaching,
    )

    # Step 1: OK → APPROACHING (fires approaching)
    tracker.observe_verdict(
        op_id="op-progress",
        verdict=ConfidenceVerdict.APPROACHING_FLOOR,
        rolling_margin=0.05,
        floor=0.1,
    )
    # Step 2: APPROACHING → BELOW (fires drop with prior=APPROACHING)
    # Use now= to bypass the rate-limit gate set by step 1's emission.
    import time as _time
    result = tracker.observe_verdict(
        op_id="op-progress",
        verdict=ConfidenceVerdict.BELOW_FLOOR,
        rolling_margin=-0.3,
        floor=0.1,
        now=_time.time() + 100.0,
    )
    assert result is not None
    assert result.prior_verdict == "approaching_floor"
    assert result.current_verdict == "below_floor"
    assert len(captured) == 1
    assert captured[0]["prior_verdict"] == "approaching_floor"
    assert captured[0]["consecutive_below"] == 1


# -----------------------------------------------------------------
# Provenance pin — the v2.83 enhancement is documented in source
# -----------------------------------------------------------------


def test_observability_documents_v2_83_enhancement():
    """Bytes-pin: v2.83 docstring citation in
    confidence_observability for both publishers + the payload
    builder. Without the citation, future readers won't know why
    the additive kwargs exist."""
    src = Path(
        inspect.getfile(confidence_observability)
    ).read_text(encoding="utf-8")
    occurrences = src.count("§37 Tier 1 #1")
    assert occurrences >= 3, (
        f"Expected ≥3 §37 Tier 1 #1 citations in "
        f"confidence_observability (payload builder + drop publisher "
        f"+ approaching publisher); found {occurrences}"
    )


def test_producer_documents_v2_83_threading():
    src = Path(
        inspect.getfile(confidence_sse_producer)
    ).read_text(encoding="utf-8")
    assert "§37 Tier 1 #1" in src, (
        "confidence_sse_producer must cite §37 Tier 1 #1 (v2.83) "
        "at the threading site"
    )
