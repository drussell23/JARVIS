"""Phase 8 surface wiring Slice 2 — SSE bridge regression spine.

Covers the 5 new event types in the broker's frozen vocabulary, the
5 publish helpers, masking discipline, master + per-event flag
matrix, payload bounds, NEVER-raises contract, and the cage
authority invariants.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import pytest

from backend.core.ouroboros.governance.observability import sse_bridge
from backend.core.ouroboros.governance.observability.sse_bridge import (
    MAX_PAYLOAD_KEYS,
    MAX_PAYLOAD_STRING_CHARS,
    MAX_RATIONALE_CHARS,
    is_bridge_enabled,
    is_confidence_drop_detected_enabled,
    is_confidence_observed_enabled,
    is_decision_recorded_enabled,
    is_flag_changed_enabled,
    is_slo_breached_enabled,
    publish_confidence_drop_detected,
    publish_confidence_observed,
    publish_decision_recorded,
    publish_flag_change_event,
    publish_flag_changed,
    publish_slo_breached,
)
from backend.core.ouroboros.governance import (
    ide_observability_stream as _stream_mod,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class _FakeBroker:
    """Captures publish() calls for assertions."""

    def __init__(self, raise_on_publish: bool = False) -> None:
        self.calls: List[Dict[str, Any]] = []
        self._raise = raise_on_publish
        self._counter = 0

    def publish(
        self,
        event_type: str,
        op_id: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        if self._raise:
            raise RuntimeError("simulated broker failure")
        self._counter += 1
        self.calls.append({
            "event_type": event_type,
            "op_id": op_id,
            "payload": dict(payload or {}),
        })
        return f"evt-{self._counter:08x}"


@pytest.fixture
def fake_broker(monkeypatch: pytest.MonkeyPatch):
    """Inject a fake broker via monkey-patching the lazy import path."""
    fake = _FakeBroker()
    # The bridge does `from ide_observability_stream import
    # get_default_broker` at call time; patch the source.
    monkeypatch.setattr(
        _stream_mod, "get_default_broker", lambda: fake,
    )
    return fake


@pytest.fixture
def raising_broker(monkeypatch: pytest.MonkeyPatch):
    fake = _FakeBroker(raise_on_publish=True)
    monkeypatch.setattr(
        _stream_mod, "get_default_broker", lambda: fake,
    )
    return fake


@pytest.fixture(autouse=True)
def _reset_bridge_env(monkeypatch: pytest.MonkeyPatch):
    """Clean Phase-8 SSE bridge env per test."""
    keys = [
        k for k in os.environ.keys()
        if k.startswith("JARVIS_PHASE8_SSE_BRIDGE_")
    ]
    for k in keys:
        monkeypatch.delenv(k, raising=False)
    yield


@pytest.fixture
def bridge_on(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(
        "JARVIS_PHASE8_SSE_BRIDGE_ENABLED", "true",
    )


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------


def test_payload_caps_sane():
    assert MAX_PAYLOAD_KEYS >= 8
    assert MAX_PAYLOAD_KEYS <= 64
    assert MAX_PAYLOAD_STRING_CHARS >= 256
    assert MAX_RATIONALE_CHARS >= 100


def test_event_types_in_broker_vocabulary():
    """The 5 Phase 8 event types must be in
    ``_VALID_EVENT_TYPES`` so the broker's strict allowlist accepts
    them. If a future refactor removes one of these, the
    corresponding bridge silently fails."""
    assert "decision_recorded" in _stream_mod._VALID_EVENT_TYPES
    assert "confidence_observed" in _stream_mod._VALID_EVENT_TYPES
    assert (
        "confidence_drop_detected"
        in _stream_mod._VALID_EVENT_TYPES
    )
    assert "slo_breached" in _stream_mod._VALID_EVENT_TYPES
    assert "flag_changed" in _stream_mod._VALID_EVENT_TYPES


def test_event_type_constants_match():
    """The named constants in ide_observability_stream must equal
    the string literals the bridge module emits — pin the
    contract."""
    assert (
        _stream_mod.EVENT_TYPE_DECISION_RECORDED == "decision_recorded"
    )
    assert (
        _stream_mod.EVENT_TYPE_CONFIDENCE_OBSERVED
        == "confidence_observed"
    )
    assert (
        _stream_mod.EVENT_TYPE_CONFIDENCE_DROP_DETECTED
        == "confidence_drop_detected"
    )
    assert _stream_mod.EVENT_TYPE_SLO_BREACHED == "slo_breached"
    assert _stream_mod.EVENT_TYPE_FLAG_CHANGED == "flag_changed"


# ---------------------------------------------------------------------------
# Master flag matrix
# ---------------------------------------------------------------------------


def test_bridge_default_off():
    assert is_bridge_enabled() is False


@pytest.mark.parametrize("val", ["true", "1", "yes", "on", "TRUE"])
def test_bridge_truthy(monkeypatch: pytest.MonkeyPatch, val: str):
    monkeypatch.setenv("JARVIS_PHASE8_SSE_BRIDGE_ENABLED", val)
    assert is_bridge_enabled() is True


@pytest.mark.parametrize("val", ["false", "0", "no", "off", ""])
def test_bridge_falsy(monkeypatch: pytest.MonkeyPatch, val: str):
    monkeypatch.setenv("JARVIS_PHASE8_SSE_BRIDGE_ENABLED", val)
    assert is_bridge_enabled() is False


# ---------------------------------------------------------------------------
# Per-event sub-flag matrix (default true; explicit false silences)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("checker,env", [
    (
        is_decision_recorded_enabled,
        "JARVIS_PHASE8_SSE_BRIDGE_DECISION_RECORDED",
    ),
    (
        is_confidence_observed_enabled,
        "JARVIS_PHASE8_SSE_BRIDGE_CONFIDENCE_OBSERVED",
    ),
    (
        is_confidence_drop_detected_enabled,
        "JARVIS_PHASE8_SSE_BRIDGE_CONFIDENCE_DROP_DETECTED",
    ),
    (
        is_slo_breached_enabled,
        "JARVIS_PHASE8_SSE_BRIDGE_SLO_BREACHED",
    ),
    (
        is_flag_changed_enabled,
        "JARVIS_PHASE8_SSE_BRIDGE_FLAG_CHANGED",
    ),
])
def test_per_event_flag_default_true(
    monkeypatch: pytest.MonkeyPatch, checker, env: str,
):
    monkeypatch.delenv(env, raising=False)
    assert checker() is True


@pytest.mark.parametrize("checker,env", [
    (
        is_decision_recorded_enabled,
        "JARVIS_PHASE8_SSE_BRIDGE_DECISION_RECORDED",
    ),
    (
        is_confidence_observed_enabled,
        "JARVIS_PHASE8_SSE_BRIDGE_CONFIDENCE_OBSERVED",
    ),
    (
        is_confidence_drop_detected_enabled,
        "JARVIS_PHASE8_SSE_BRIDGE_CONFIDENCE_DROP_DETECTED",
    ),
    (
        is_slo_breached_enabled,
        "JARVIS_PHASE8_SSE_BRIDGE_SLO_BREACHED",
    ),
    (
        is_flag_changed_enabled,
        "JARVIS_PHASE8_SSE_BRIDGE_FLAG_CHANGED",
    ),
])
def test_per_event_flag_explicit_false(
    monkeypatch: pytest.MonkeyPatch, checker, env: str,
):
    monkeypatch.setenv(env, "false")
    assert checker() is False


# ---------------------------------------------------------------------------
# Master-off: every helper is a no-op (returns None, no broker call)
# ---------------------------------------------------------------------------


def test_master_off_decision_no_op(fake_broker: _FakeBroker):
    result = publish_decision_recorded(
        op_id="op-1", phase="ROUTE", decision="STANDARD",
    )
    assert result is None
    assert fake_broker.calls == []


def test_master_off_confidence_no_op(fake_broker: _FakeBroker):
    result = publish_confidence_observed(
        classifier_name="clf", confidence=0.5,
        threshold=0.5, outcome="X",
    )
    assert result is None
    assert fake_broker.calls == []


def test_master_off_drop_detected_no_op(fake_broker: _FakeBroker):
    result = publish_confidence_drop_detected(
        classifier_name="clf", drop_pct=30.0,
        recent_mean=0.4, prior_mean=0.6, window_size=10,
    )
    assert result is None
    assert fake_broker.calls == []


def test_master_off_slo_no_op(fake_broker: _FakeBroker):
    result = publish_slo_breached(
        phase="ROUTE", p95_s=1.5, slo_s=1.0, sample_count=25,
    )
    assert result is None
    assert fake_broker.calls == []


def test_master_off_flag_changed_no_op(fake_broker: _FakeBroker):
    result = publish_flag_changed(
        flag_name="JARVIS_X", prev_value="a", next_value="b",
        is_changed=True,
    )
    assert result is None
    assert fake_broker.calls == []


# ---------------------------------------------------------------------------
# Sub-flag-off: silences only that one event
# ---------------------------------------------------------------------------


def test_decision_sub_flag_off_silences_only_that(
    bridge_on, fake_broker: _FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(
        "JARVIS_PHASE8_SSE_BRIDGE_DECISION_RECORDED", "false",
    )
    assert publish_decision_recorded(
        op_id="op-1", phase="ROUTE", decision="X",
    ) is None
    # Sibling helpers still publish.
    eid = publish_confidence_observed(
        classifier_name="clf", confidence=0.5,
        threshold=0.5, outcome="X",
    )
    assert eid is not None
    assert any(
        c["event_type"] == "confidence_observed"
        for c in fake_broker.calls
    )


# ---------------------------------------------------------------------------
# Decision recorded — happy path + bounds
# ---------------------------------------------------------------------------


def test_decision_recorded_publishes(
    bridge_on, fake_broker: _FakeBroker,
):
    eid = publish_decision_recorded(
        op_id="op-abc", phase="ROUTE", decision="STANDARD",
        rationale="default cascade",
    )
    assert eid is not None
    assert len(fake_broker.calls) == 1
    call = fake_broker.calls[0]
    assert call["event_type"] == "decision_recorded"
    assert call["op_id"] == "op-abc"
    assert call["payload"]["phase"] == "ROUTE"
    assert call["payload"]["decision"] == "STANDARD"
    assert call["payload"]["rationale"] == "default cascade"


def test_decision_recorded_truncates_rationale(
    bridge_on, fake_broker: _FakeBroker,
):
    long = "x" * (MAX_RATIONALE_CHARS + 100)
    publish_decision_recorded(
        op_id="op-abc", phase="ROUTE", decision="X",
        rationale=long,
    )
    payload = fake_broker.calls[0]["payload"]
    assert len(payload["rationale"]) <= MAX_RATIONALE_CHARS


def test_decision_recorded_handles_empty_strings(
    bridge_on, fake_broker: _FakeBroker,
):
    eid = publish_decision_recorded(
        op_id="op-x", phase="", decision="",
    )
    assert eid is not None  # publish still goes (cage decides if useful)


# ---------------------------------------------------------------------------
# Confidence observed
# ---------------------------------------------------------------------------


def test_confidence_observed_publishes_below_threshold_flag(
    bridge_on, fake_broker: _FakeBroker,
):
    publish_confidence_observed(
        classifier_name="clf", confidence=0.3,
        threshold=0.5, outcome="X", op_id="op-c",
    )
    p = fake_broker.calls[0]["payload"]
    assert p["below_threshold"] is True
    assert p["confidence"] == 0.3
    assert p["threshold"] == 0.5
    assert fake_broker.calls[0]["op_id"] == "op-c"


def test_confidence_observed_above_threshold(
    bridge_on, fake_broker: _FakeBroker,
):
    publish_confidence_observed(
        classifier_name="clf", confidence=0.9,
        threshold=0.5, outcome="X",
    )
    p = fake_broker.calls[0]["payload"]
    assert p["below_threshold"] is False


def test_confidence_observed_skips_non_numeric(
    bridge_on, fake_broker: _FakeBroker,
):
    eid = publish_confidence_observed(
        classifier_name="clf",
        confidence="not a float",  # type: ignore[arg-type]
        threshold=0.5, outcome="X",
    )
    assert eid is None
    assert fake_broker.calls == []


def test_confidence_observed_optional_op_id_default_empty(
    bridge_on, fake_broker: _FakeBroker,
):
    publish_confidence_observed(
        classifier_name="clf", confidence=0.5,
        threshold=0.5, outcome="X",
    )
    assert fake_broker.calls[0]["op_id"] == ""


# ---------------------------------------------------------------------------
# Confidence drop detected
# ---------------------------------------------------------------------------


def test_confidence_drop_detected_publishes(
    bridge_on, fake_broker: _FakeBroker,
):
    publish_confidence_drop_detected(
        classifier_name="route_classifier",
        drop_pct=35.5, recent_mean=0.55,
        prior_mean=0.85, window_size=10,
    )
    p = fake_broker.calls[0]["payload"]
    assert p["classifier_name"] == "route_classifier"
    assert p["drop_pct"] == 35.5
    assert p["recent_mean"] == 0.55
    assert p["prior_mean"] == 0.85
    assert p["window_size"] == 10


def test_confidence_drop_detected_skips_non_numeric(
    bridge_on, fake_broker: _FakeBroker,
):
    eid = publish_confidence_drop_detected(
        classifier_name="x",
        drop_pct="bad",  # type: ignore[arg-type]
        recent_mean=0.0, prior_mean=0.0, window_size=0,
    )
    assert eid is None
    assert fake_broker.calls == []


# ---------------------------------------------------------------------------
# SLO breach
# ---------------------------------------------------------------------------


def test_slo_breached_publishes_with_overshoot(
    bridge_on, fake_broker: _FakeBroker,
):
    publish_slo_breached(
        phase="GENERATE", p95_s=1.5, slo_s=1.0, sample_count=25,
    )
    p = fake_broker.calls[0]["payload"]
    assert p["phase"] == "GENERATE"
    assert p["p95_s"] == 1.5
    assert p["slo_s"] == 1.0
    assert p["overshoot_s"] == pytest.approx(0.5)
    assert p["overshoot_pct"] == pytest.approx(50.0)
    assert p["sample_count"] == 25


def test_slo_breached_handles_zero_slo(
    bridge_on, fake_broker: _FakeBroker,
):
    """When slo_s is 0, overshoot_pct must NOT divide by zero."""
    publish_slo_breached(
        phase="X", p95_s=0.5, slo_s=0.0, sample_count=20,
    )
    p = fake_broker.calls[0]["payload"]
    assert p["overshoot_pct"] == 0.0


def test_slo_breached_skips_non_numeric(
    bridge_on, fake_broker: _FakeBroker,
):
    eid = publish_slo_breached(
        phase="X",
        p95_s="oops",  # type: ignore[arg-type]
        slo_s=1.0, sample_count=20,
    )
    assert eid is None
    assert fake_broker.calls == []


# ---------------------------------------------------------------------------
# Flag changed — masking discipline
# ---------------------------------------------------------------------------


def test_flag_changed_masks_values(
    bridge_on, fake_broker: _FakeBroker,
):
    """Raw env values MUST NEVER appear in the SSE payload."""
    publish_flag_changed(
        flag_name="JARVIS_API_KEY",
        prev_value="OLD_SECRET_TOKEN",
        next_value="NEW_SECRET_TOKEN",
        is_changed=True,
    )
    p = fake_broker.calls[0]["payload"]
    assert p["prev_value"] == "<set>"
    assert p["next_value"] == "<set>"
    assert "OLD_SECRET_TOKEN" not in str(p)
    assert "NEW_SECRET_TOKEN" not in str(p)


def test_flag_changed_preserves_none_values(
    bridge_on, fake_broker: _FakeBroker,
):
    """None signals add/remove — must NOT be masked to "<empty>"
    (that would conflate with empty-string values)."""
    # Removed: prev=set, next=None.
    publish_flag_changed(
        flag_name="JARVIS_X",
        prev_value="anything",
        next_value=None,
        is_removed=True,
    )
    p = fake_broker.calls[0]["payload"]
    assert p["prev_value"] == "<set>"
    assert p["next_value"] is None
    # Added: prev=None, next=set.
    fake_broker.calls.clear()
    publish_flag_changed(
        flag_name="JARVIS_Y",
        prev_value=None,
        next_value="anything",
        is_added=True,
    )
    p = fake_broker.calls[0]["payload"]
    assert p["prev_value"] is None
    assert p["next_value"] == "<set>"


def test_flag_changed_distinguishes_set_vs_empty(
    bridge_on, fake_broker: _FakeBroker,
):
    publish_flag_changed(
        flag_name="JARVIS_X",
        prev_value="value",
        next_value="",
        is_changed=True,
    )
    p = fake_broker.calls[0]["payload"]
    assert p["prev_value"] == "<set>"
    assert p["next_value"] == "<empty>"


def test_flag_change_event_wrapper(
    bridge_on, fake_broker: _FakeBroker,
):
    """Convenience wrapper accepts a FlagChangeEvent dataclass."""
    from backend.core.ouroboros.governance.observability.flag_change_emitter import (  # noqa: E501
        FlagChangeEvent,
    )
    ev = FlagChangeEvent(
        flag_name="JARVIS_NEW",
        prev_value=None,
        next_value="secret_value",
        ts_epoch=123.0,
    )
    eid = publish_flag_change_event(ev)
    assert eid is not None
    p = fake_broker.calls[0]["payload"]
    assert p["flag_name"] == "JARVIS_NEW"
    assert p["next_value"] == "<set>"
    assert p["is_added"] is True
    assert "secret_value" not in str(p)


def test_flag_change_event_wrapper_handles_bad_object(
    bridge_on, fake_broker: _FakeBroker,
):
    """Bad input must NOT raise — defensive contract."""
    eid = publish_flag_change_event(object())  # arbitrary
    # Either None or a string event_id; never raise.
    assert eid is None or isinstance(eid, str)


# ---------------------------------------------------------------------------
# NEVER-raises contract
# ---------------------------------------------------------------------------


def test_broker_exception_swallowed_decision(
    bridge_on, raising_broker,
):
    """Broker raises → bridge returns None, never propagates."""
    eid = publish_decision_recorded(
        op_id="op-1", phase="ROUTE", decision="X",
    )
    assert eid is None


def test_broker_exception_swallowed_confidence(
    bridge_on, raising_broker,
):
    eid = publish_confidence_observed(
        classifier_name="clf", confidence=0.5,
        threshold=0.5, outcome="X",
    )
    assert eid is None


def test_broker_exception_swallowed_slo(
    bridge_on, raising_broker,
):
    eid = publish_slo_breached(
        phase="ROUTE", p95_s=1.0, slo_s=0.5, sample_count=25,
    )
    assert eid is None


def test_broker_exception_swallowed_flag(
    bridge_on, raising_broker,
):
    eid = publish_flag_changed(
        flag_name="JARVIS_X", prev_value=None, next_value="y",
        is_added=True,
    )
    assert eid is None


# ---------------------------------------------------------------------------
# Payload bounds
# ---------------------------------------------------------------------------


def test_payload_bound_truncates_long_strings(
    bridge_on, fake_broker: _FakeBroker,
):
    """Internal ``_bound_payload`` truncates string values per
    MAX_PAYLOAD_STRING_CHARS."""
    long = "x" * (MAX_PAYLOAD_STRING_CHARS + 500)
    bounded = sse_bridge._bound_payload({"big": long})
    assert len(bounded["big"]) <= MAX_PAYLOAD_STRING_CHARS


def test_payload_bound_caps_key_count(
    bridge_on, fake_broker: _FakeBroker,
):
    big = {f"k{i}": i for i in range(MAX_PAYLOAD_KEYS + 5)}
    bounded = sse_bridge._bound_payload(big)
    assert len(bounded) <= MAX_PAYLOAD_KEYS


def test_payload_bound_passes_through_non_strings(
    bridge_on, fake_broker: _FakeBroker,
):
    bounded = sse_bridge._bound_payload(
        {"n": 42, "f": 3.14, "b": True, "lst": [1, 2, 3]},
    )
    assert bounded["n"] == 42
    assert bounded["f"] == 3.14
    assert bounded["b"] is True
    assert bounded["lst"] == [1, 2, 3]


# ---------------------------------------------------------------------------
# Broker-import-failure resilience
# ---------------------------------------------------------------------------


def test_broker_import_failure_returns_none(
    bridge_on, monkeypatch: pytest.MonkeyPatch,
):
    """Force a broker-import failure inside the lazy path."""
    import sys
    real_module = sys.modules.get(
        "backend.core.ouroboros.governance.ide_observability_stream",
    )
    # Inject a stub that raises on attribute access.

    class _Boom:
        def __getattr__(self, name: str) -> Any:
            raise ImportError("simulated import failure")

    sys.modules[
        "backend.core.ouroboros.governance.ide_observability_stream"
    ] = _Boom()  # type: ignore[assignment]
    try:
        eid = publish_decision_recorded(
            op_id="op-1", phase="X", decision="Y",
        )
        assert eid is None
    finally:
        if real_module is not None:
            sys.modules[
                "backend.core.ouroboros.governance."
                "ide_observability_stream"
            ] = real_module


# ---------------------------------------------------------------------------
# Authority / cage invariants
# ---------------------------------------------------------------------------


def test_does_not_import_gate_modules():
    import ast
    import inspect
    src = inspect.getsource(sse_bridge)
    tree = ast.parse(src)
    banned = [
        "orchestrator", "iron_gate", "risk_tier_floor",
        "semantic_guardian", "policy_engine",
        "candidate_generator", "tool_executor", "change_engine",
    ]
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names = [node.module]
        for mod in names:
            for token in banned:
                assert token not in mod, (
                    f"sse_bridge imports {mod!r} containing banned "
                    f"token {token!r}"
                )


def test_top_level_imports_are_stdlib_only():
    """Top-level imports must be stdlib + this module's own logger.
    The broker + substrate are imported lazily inside helpers."""
    import ast
    import inspect
    src = inspect.getsource(sse_bridge)
    tree = ast.parse(src)
    top_level: List[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            top_level.append(node.module)
    forbidden = {
        "backend.core.ouroboros.governance.ide_observability_stream",
        "backend.core.ouroboros.governance.observability."
        "decision_trace_ledger",
        "backend.core.ouroboros.governance.observability."
        "latent_confidence_ring",
        "backend.core.ouroboros.governance.observability."
        "flag_change_emitter",
        "backend.core.ouroboros.governance.observability."
        "latency_slo_detector",
    }
    leaked = forbidden & set(top_level)
    assert not leaked, (
        f"sse_bridge hoisted lazy modules to top level: {leaked!r}"
    )


def test_no_secret_leakage_in_module_constants():
    text = repr(vars(sse_bridge))
    for needle in ("sk-", "ghp_", "AKIA", "BEGIN PRIVATE KEY"):
        assert needle not in text


def test_publish_helper_count_pinned_at_six():
    """Bit-rot guard: any added publish helper must update this pin."""
    public_publishers = [
        name for name in dir(sse_bridge)
        if name.startswith("publish_")
        and callable(getattr(sse_bridge, name))
    ]
    assert sorted(public_publishers) == [
        "publish_confidence_drop_detected",
        "publish_confidence_observed",
        "publish_decision_recorded",
        "publish_flag_change_event",
        "publish_flag_changed",
        "publish_slo_breached",
    ]


def test_event_type_count_pinned_at_five():
    """The 5 Phase 8 event types — a 6th would update both broker
    vocab and this pin."""
    phase8_events = [
        e for e in _stream_mod._VALID_EVENT_TYPES
        if e in {
            "decision_recorded", "confidence_observed",
            "confidence_drop_detected", "slo_breached",
            "flag_changed",
        }
    ]
    assert len(phase8_events) == 5
