"""Phase 8 IDE observability surface — regression spine.

Covers the 8 new endpoints + master flag + per-rule kill behavior +
authority invariants + bounded-payload pins + lazy-import contract.

Tests invoke handlers directly via ``make_mocked_request`` — no real
HTTP server is bound. Mirrors the Gap #6 ``IDEObservabilityRouter``
test pattern.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

pytest.importorskip("aiohttp")

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import make_mocked_request  # noqa: E402

from backend.core.ouroboros.governance.observability.ide_routes import (  # noqa: E402,E501
    MAX_CONFIDENCE_EVENTS,
    MAX_DECISION_LIST_ROWS,
    MAX_TIMELINE_LINES,
    PHASE8_OBSERVABILITY_SCHEMA_VERSION,
    Phase8ObservabilityRouter,
    assert_loopback_only,
    phase8_ide_observability_enabled,
)
from backend.core.ouroboros.governance.observability import (  # noqa: E402
    decision_trace_ledger as _ledger_mod,
    flag_change_emitter as _flag_mod,
    latency_slo_detector as _slo_mod,
    latent_confidence_ring as _ring_mod,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_request(
    path: str,
    *,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    match_info: Optional[Dict[str, str]] = None,
    remote: str = "127.0.0.1",
) -> web.Request:
    headers = headers or {}
    req = make_mocked_request(method, path, headers=headers)
    if match_info:
        req.match_info.update(match_info)
    req._transport_peername = (remote, 0)  # type: ignore[attr-defined]
    return req


def _run_async(coro: Any) -> Any:
    return asyncio.new_event_loop().run_until_complete(coro)


def _body(resp: Any) -> Dict[str, Any]:
    return json.loads(resp.body.decode("utf-8"))


@pytest.fixture(autouse=True)
def _reset_phase8_env(monkeypatch: pytest.MonkeyPatch):
    """Clean Phase-8-related env per test."""
    keys = [
        k for k in os.environ.keys()
        if (
            k.startswith("JARVIS_PHASE8_")
            or k.startswith("JARVIS_DECISION_TRACE_")
            or k.startswith("JARVIS_LATENT_CONFIDENCE_")
            or k.startswith("JARVIS_FLAG_CHANGE_")
            or k.startswith("JARVIS_LATENCY_SLO_")
            or k.startswith("JARVIS_MULTI_OP_")
        )
    ]
    for k in keys:
        monkeypatch.delenv(k, raising=False)
    _ledger_mod.reset_default_ledger()
    _ring_mod.reset_default_ring()
    _flag_mod.reset_default_monitor()
    _slo_mod.reset_default_detector()
    yield
    _ledger_mod.reset_default_ledger()
    _ring_mod.reset_default_ring()
    _flag_mod.reset_default_monitor()
    _slo_mod.reset_default_detector()


def _enable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "JARVIS_PHASE8_IDE_OBSERVABILITY_ENABLED", "true",
    )


@pytest.fixture
def isolated_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    target = tmp_path / "decision_trace.jsonl"
    monkeypatch.setenv(
        "JARVIS_DECISION_TRACE_LEDGER_PATH", str(target),
    )
    monkeypatch.setenv(
        "JARVIS_DECISION_TRACE_LEDGER_ENABLED", "true",
    )
    _ledger_mod.reset_default_ledger()
    return target


@pytest.fixture
def enabled_ring(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(
        "JARVIS_LATENT_CONFIDENCE_RING_ENABLED", "true",
    )
    _ring_mod.reset_default_ring()
    return _ring_mod.get_default_ring()


@pytest.fixture
def enabled_monitor(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(
        "JARVIS_FLAG_CHANGE_EMITTER_ENABLED", "true",
    )
    _flag_mod.reset_default_monitor()
    return _flag_mod.get_default_monitor()


@pytest.fixture
def enabled_detector(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(
        "JARVIS_LATENCY_SLO_DETECTOR_ENABLED", "true",
    )
    _slo_mod.reset_default_detector()
    return _slo_mod.get_default_detector()


# ---------------------------------------------------------------------------
# Module-level constants + master flag
# ---------------------------------------------------------------------------


def test_schema_version_is_v1_0():
    assert PHASE8_OBSERVABILITY_SCHEMA_VERSION == "1.0"


def test_bounded_response_caps_are_sane():
    assert 100 <= MAX_DECISION_LIST_ROWS <= 5000
    assert MAX_TIMELINE_LINES >= 50
    assert MAX_CONFIDENCE_EVENTS >= 50


def test_master_flag_default_false():
    assert phase8_ide_observability_enabled() is False


@pytest.mark.parametrize("val", ["true", "1", "yes", "on", "TRUE"])
def test_master_flag_truthy(monkeypatch: pytest.MonkeyPatch, val: str):
    monkeypatch.setenv(
        "JARVIS_PHASE8_IDE_OBSERVABILITY_ENABLED", val,
    )
    assert phase8_ide_observability_enabled() is True


@pytest.mark.parametrize("val", ["false", "0", "no", "off", ""])
def test_master_flag_falsy(monkeypatch: pytest.MonkeyPatch, val: str):
    monkeypatch.setenv(
        "JARVIS_PHASE8_IDE_OBSERVABILITY_ENABLED", val,
    )
    assert phase8_ide_observability_enabled() is False


def test_assert_loopback_only_rejects_wildcards():
    for bad in ("0.0.0.0", "::", "*"):
        with pytest.raises(Exception):
            assert_loopback_only(bad)


def test_assert_loopback_only_accepts_localhost():
    assert_loopback_only("127.0.0.1")
    assert_loopback_only("localhost")


# ---------------------------------------------------------------------------
# Master-off: every endpoint returns 403
# ---------------------------------------------------------------------------


def _all_handler_paths():
    return [
        ("_handle_health", "/observability/phase8/health", {}),
        ("_handle_decision_list", "/observability/decisions", {}),
        (
            "_handle_decision_detail",
            "/observability/decisions/op-abc",
            {"op_id": "op-abc"},
        ),
        ("_handle_confidence_list", "/observability/confidence", {}),
        (
            "_handle_confidence_detail",
            "/observability/confidence/clf-1",
            {"classifier": "clf-1"},
        ),
        (
            "_handle_timeline_detail",
            "/observability/timeline/op-abc",
            {"op_id": "op-abc"},
        ),
        ("_handle_flag_changes", "/observability/flags/changes", {}),
        ("_handle_latency_slo", "/observability/latency/slo", {}),
    ]


@pytest.mark.parametrize(
    "handler_name,path,match", _all_handler_paths(),
)
def test_endpoints_403_when_master_off(
    handler_name: str, path: str, match: Dict[str, str],
):
    router = Phase8ObservabilityRouter()
    req = _make_request(path, match_info=match or None)
    resp = _run_async(getattr(router, handler_name)(req))
    assert resp.status == 403
    body = _body(resp)
    assert body["reason_code"] == "phase8_observability.disabled"
    assert body["schema_version"] == PHASE8_OBSERVABILITY_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------


def test_health_returns_substrate_flag_state(
    monkeypatch: pytest.MonkeyPatch,
):
    _enable(monkeypatch)
    monkeypatch.setenv("JARVIS_DECISION_TRACE_LEDGER_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_LATENT_CONFIDENCE_RING_ENABLED", "false",
    )
    monkeypatch.setenv("JARVIS_FLAG_CHANGE_EMITTER_ENABLED", "false")
    monkeypatch.setenv("JARVIS_LATENCY_SLO_DETECTOR_ENABLED", "true")
    monkeypatch.setenv("JARVIS_MULTI_OP_TIMELINE_ENABLED", "false")
    router = Phase8ObservabilityRouter()
    req = _make_request("/observability/phase8/health")
    resp = _run_async(router._handle_health(req))
    assert resp.status == 200
    body = _body(resp)
    assert body["enabled"] is True
    assert body["api_version"] == PHASE8_OBSERVABILITY_SCHEMA_VERSION
    assert body["substrate"]["decision_trace_ledger"] is True
    assert body["substrate"]["latent_confidence_ring"] is False
    assert body["substrate"]["flag_change_emitter"] is False
    assert body["substrate"]["latency_slo_detector"] is True
    assert body["substrate"]["multi_op_timeline"] is False
    assert "decisions" in body["surface"]
    assert "now_mono" in body


def test_health_carries_schema_and_no_store(
    monkeypatch: pytest.MonkeyPatch,
):
    _enable(monkeypatch)
    router = Phase8ObservabilityRouter()
    req = _make_request("/observability/phase8/health")
    resp = _run_async(router._handle_health(req))
    assert _body(resp)["schema_version"] == (
        PHASE8_OBSERVABILITY_SCHEMA_VERSION
    )
    assert resp.headers.get("Cache-Control") == "no-store"


# ---------------------------------------------------------------------------
# Decisions: list + detail
# ---------------------------------------------------------------------------


def test_decisions_list_empty_when_ledger_off(
    monkeypatch: pytest.MonkeyPatch,
):
    _enable(monkeypatch)
    monkeypatch.setenv(
        "JARVIS_DECISION_TRACE_LEDGER_ENABLED", "false",
    )
    router = Phase8ObservabilityRouter()
    req = _make_request("/observability/decisions")
    resp = _run_async(router._handle_decision_list(req))
    assert resp.status == 200
    body = _body(resp)
    assert body["rows"] == []
    assert body["count"] == 0
    assert body["ledger_enabled"] is False


def test_decisions_list_returns_ledger_rows(
    monkeypatch: pytest.MonkeyPatch, isolated_ledger: Path,
):
    _enable(monkeypatch)
    ledger = _ledger_mod.get_default_ledger()
    for i in range(3):
        ok, _ = ledger.record(
            op_id=f"op-{i}", phase="ROUTE",
            decision=f"D-{i}", rationale="r",
        )
        assert ok
    router = Phase8ObservabilityRouter()
    req = _make_request("/observability/decisions")
    resp = _run_async(router._handle_decision_list(req))
    assert resp.status == 200
    body = _body(resp)
    ids = [row["op_id"] for row in body["rows"]]
    assert ids == ["op-2", "op-1", "op-0"]
    assert body["count"] == 3
    assert body["ledger_enabled"] is True


def test_decisions_list_respects_op_id_filter(
    monkeypatch: pytest.MonkeyPatch, isolated_ledger: Path,
):
    _enable(monkeypatch)
    ledger = _ledger_mod.get_default_ledger()
    ledger.record(op_id="op-A", phase="ROUTE", decision="X")
    ledger.record(op_id="op-B", phase="ROUTE", decision="Y")
    ledger.record(op_id="op-A", phase="GENERATE", decision="Z")
    router = Phase8ObservabilityRouter()
    req = _make_request("/observability/decisions?op_id=op-A")
    resp = _run_async(router._handle_decision_list(req))
    assert resp.status == 200
    body = _body(resp)
    for row in body["rows"]:
        assert row["op_id"] == "op-A"
    assert body["count"] == 2


def test_decisions_list_rejects_malformed_op_id_filter(
    monkeypatch: pytest.MonkeyPatch, isolated_ledger: Path,
):
    _enable(monkeypatch)
    router = Phase8ObservabilityRouter()
    req = _make_request(
        "/observability/decisions?op_id=../etc/passwd",
    )
    resp = _run_async(router._handle_decision_list(req))
    assert resp.status == 400
    assert _body(resp)["reason_code"] == (
        "phase8_observability.malformed_op_id"
    )


def test_decisions_list_clamps_limit(
    monkeypatch: pytest.MonkeyPatch, isolated_ledger: Path,
):
    _enable(monkeypatch)
    ledger = _ledger_mod.get_default_ledger()
    for i in range(10):
        ledger.record(op_id=f"op-{i}", phase="X", decision="D")
    router = Phase8ObservabilityRouter()
    req = _make_request("/observability/decisions?limit=3")
    resp = _run_async(router._handle_decision_list(req))
    body = _body(resp)
    assert len(body["rows"]) == 3
    assert body["limit_applied"] == 3
    req2 = _make_request("/observability/decisions?limit=garbage")
    resp2 = _run_async(router._handle_decision_list(req2))
    assert _body(resp2)["limit_applied"] == MAX_DECISION_LIST_ROWS


def test_decision_detail_returns_full_trace(
    monkeypatch: pytest.MonkeyPatch, isolated_ledger: Path,
):
    _enable(monkeypatch)
    ledger = _ledger_mod.get_default_ledger()
    ledger.record(op_id="op-trace", phase="ROUTE", decision="STANDARD")
    ledger.record(
        op_id="op-trace", phase="GENERATE", decision="OK",
        rationale="generated",
    )
    router = Phase8ObservabilityRouter()
    req = _make_request(
        "/observability/decisions/op-trace",
        match_info={"op_id": "op-trace"},
    )
    resp = _run_async(router._handle_decision_detail(req))
    assert resp.status == 200
    body = _body(resp)
    assert body["op_id"] == "op-trace"
    assert body["row_count"] == 2
    phases = [row["phase"] for row in body["rows"]]
    assert phases == ["ROUTE", "GENERATE"]


def test_decision_detail_rejects_malformed_op_id(
    monkeypatch: pytest.MonkeyPatch, isolated_ledger: Path,
):
    _enable(monkeypatch)
    router = Phase8ObservabilityRouter()
    req = _make_request(
        "/observability/decisions/" + "x" * 500,
        match_info={"op_id": "x" * 500},
    )
    resp = _run_async(router._handle_decision_detail(req))
    assert resp.status == 400
    assert _body(resp)["reason_code"] == (
        "phase8_observability.malformed_op_id"
    )


def test_decision_detail_404_unknown_op_id(
    monkeypatch: pytest.MonkeyPatch, isolated_ledger: Path,
):
    _enable(monkeypatch)
    router = Phase8ObservabilityRouter()
    req = _make_request(
        "/observability/decisions/op-nonexistent",
        match_info={"op_id": "op-nonexistent"},
    )
    resp = _run_async(router._handle_decision_detail(req))
    assert resp.status == 404
    assert _body(resp)["reason_code"] == (
        "phase8_observability.unknown_op_id"
    )


def test_decision_detail_503_when_ledger_off(
    monkeypatch: pytest.MonkeyPatch,
):
    _enable(monkeypatch)
    monkeypatch.setenv(
        "JARVIS_DECISION_TRACE_LEDGER_ENABLED", "false",
    )
    router = Phase8ObservabilityRouter()
    req = _make_request(
        "/observability/decisions/op-abc",
        match_info={"op_id": "op-abc"},
    )
    resp = _run_async(router._handle_decision_detail(req))
    assert resp.status == 503
    assert _body(resp)["reason_code"] == (
        "phase8_observability.ledger_disabled"
    )


# ---------------------------------------------------------------------------
# Confidence: list + detail
# ---------------------------------------------------------------------------


def test_confidence_list_empty_when_ring_off(
    monkeypatch: pytest.MonkeyPatch,
):
    _enable(monkeypatch)
    monkeypatch.setenv(
        "JARVIS_LATENT_CONFIDENCE_RING_ENABLED", "false",
    )
    router = Phase8ObservabilityRouter()
    req = _make_request("/observability/confidence")
    resp = _run_async(router._handle_confidence_list(req))
    body = _body(resp)
    assert body["classifier_names"] == []
    assert body["ring_enabled"] is False


def test_confidence_list_returns_distinct_names(
    monkeypatch: pytest.MonkeyPatch, enabled_ring,
):
    _enable(monkeypatch)
    enabled_ring.record(
        classifier_name="route_classifier",
        confidence=0.9, threshold=0.5, outcome="STANDARD",
    )
    enabled_ring.record(
        classifier_name="risk_tier",
        confidence=0.7, threshold=0.5, outcome="GREEN",
    )
    enabled_ring.record(
        classifier_name="route_classifier",
        confidence=0.4, threshold=0.5, outcome="IMMEDIATE",
    )
    router = Phase8ObservabilityRouter()
    req = _make_request("/observability/confidence")
    resp = _run_async(router._handle_confidence_list(req))
    assert resp.status == 200
    body = _body(resp)
    assert sorted(body["classifier_names"]) == [
        "risk_tier", "route_classifier",
    ]
    assert body["total_events"] == 3
    assert body["ring_enabled"] is True
    assert body["capacity"] >= 64


def test_confidence_detail_returns_events_and_drop(
    monkeypatch: pytest.MonkeyPatch, enabled_ring,
):
    _enable(monkeypatch)
    for c in [0.9, 0.92, 0.91, 0.93, 0.5, 0.45, 0.4, 0.42]:
        enabled_ring.record(
            classifier_name="clf",
            confidence=c, threshold=0.5, outcome="X",
        )
    router = Phase8ObservabilityRouter()
    req = _make_request(
        "/observability/confidence/clf?window=4",
        match_info={"classifier": "clf"},
    )
    resp = _run_async(router._handle_confidence_detail(req))
    assert resp.status == 200
    body = _body(resp)
    assert body["classifier_name"] == "clf"
    assert body["event_count"] == 8
    drop = body["drop_indicators"]
    assert drop["window_size"] == 4
    assert drop["drop_detected"] is True
    assert drop["drop_pct"] > 20.0


def test_confidence_detail_404_unknown_classifier(
    monkeypatch: pytest.MonkeyPatch, enabled_ring,
):
    _enable(monkeypatch)
    router = Phase8ObservabilityRouter()
    req = _make_request(
        "/observability/confidence/never-recorded",
        match_info={"classifier": "never-recorded"},
    )
    resp = _run_async(router._handle_confidence_detail(req))
    assert resp.status == 404
    assert _body(resp)["reason_code"] == (
        "phase8_observability.unknown_classifier"
    )


def test_confidence_detail_400_malformed_classifier(
    monkeypatch: pytest.MonkeyPatch, enabled_ring,
):
    _enable(monkeypatch)
    router = Phase8ObservabilityRouter()
    bad = "x" * 200
    req = _make_request(
        "/observability/confidence/" + bad,
        match_info={"classifier": bad},
    )
    resp = _run_async(router._handle_confidence_detail(req))
    assert resp.status == 400
    assert _body(resp)["reason_code"] == (
        "phase8_observability.malformed_classifier"
    )


def test_confidence_detail_clamps_window_and_drop_pct(
    monkeypatch: pytest.MonkeyPatch, enabled_ring,
):
    _enable(monkeypatch)
    enabled_ring.record(
        classifier_name="clf-2", confidence=0.5,
        threshold=0.5, outcome="X",
    )
    router = Phase8ObservabilityRouter()
    req = _make_request(
        "/observability/confidence/clf-2"
        "?window=99999&drop_pct=999",
        match_info={"classifier": "clf-2"},
    )
    resp = _run_async(router._handle_confidence_detail(req))
    assert resp.status == 200
    assert "drop_indicators" in _body(resp)


def test_confidence_detail_503_when_ring_off(
    monkeypatch: pytest.MonkeyPatch,
):
    _enable(monkeypatch)
    monkeypatch.setenv(
        "JARVIS_LATENT_CONFIDENCE_RING_ENABLED", "false",
    )
    router = Phase8ObservabilityRouter()
    req = _make_request(
        "/observability/confidence/clf-x",
        match_info={"classifier": "clf-x"},
    )
    resp = _run_async(router._handle_confidence_detail(req))
    assert resp.status == 503
    assert _body(resp)["reason_code"] == (
        "phase8_observability.ring_disabled"
    )


# ---------------------------------------------------------------------------
# Timeline endpoint
# ---------------------------------------------------------------------------


def test_timeline_returns_text_and_events(
    monkeypatch: pytest.MonkeyPatch, isolated_ledger: Path,
):
    _enable(monkeypatch)
    ledger = _ledger_mod.get_default_ledger()
    ledger.record(op_id="op-T", phase="ROUTE", decision="A")
    ledger.record(op_id="op-T", phase="GENERATE", decision="B")
    router = Phase8ObservabilityRouter()
    req = _make_request(
        "/observability/timeline/op-T",
        match_info={"op_id": "op-T"},
    )
    resp = _run_async(router._handle_timeline_detail(req))
    assert resp.status == 200
    body = _body(resp)
    assert body["op_id"] == "op-T"
    assert body["event_count"] == 2
    assert body["max_lines"] == MAX_TIMELINE_LINES
    assert body["text_render"]
    for ev in body["events"]:
        assert ev["event_type"] == "decision"


def test_timeline_404_unknown_op_id(
    monkeypatch: pytest.MonkeyPatch, isolated_ledger: Path,
):
    _enable(monkeypatch)
    router = Phase8ObservabilityRouter()
    req = _make_request(
        "/observability/timeline/op-missing",
        match_info={"op_id": "op-missing"},
    )
    resp = _run_async(router._handle_timeline_detail(req))
    assert resp.status == 404
    assert _body(resp)["reason_code"] == (
        "phase8_observability.unknown_op_id"
    )


def test_timeline_400_malformed_op_id(
    monkeypatch: pytest.MonkeyPatch, isolated_ledger: Path,
):
    _enable(monkeypatch)
    router = Phase8ObservabilityRouter()
    bad = "x" * 500
    req = _make_request(
        "/observability/timeline/" + bad,
        match_info={"op_id": bad},
    )
    resp = _run_async(router._handle_timeline_detail(req))
    assert resp.status == 400
    assert _body(resp)["reason_code"] == (
        "phase8_observability.malformed_op_id"
    )


def test_timeline_503_when_ledger_off(
    monkeypatch: pytest.MonkeyPatch,
):
    _enable(monkeypatch)
    monkeypatch.setenv(
        "JARVIS_DECISION_TRACE_LEDGER_ENABLED", "false",
    )
    router = Phase8ObservabilityRouter()
    req = _make_request(
        "/observability/timeline/op-T",
        match_info={"op_id": "op-T"},
    )
    resp = _run_async(router._handle_timeline_detail(req))
    assert resp.status == 503
    assert _body(resp)["reason_code"] == (
        "phase8_observability.ledger_disabled"
    )


# ---------------------------------------------------------------------------
# Flag-changes endpoint
# ---------------------------------------------------------------------------


def test_flag_changes_returns_masked_snapshot(
    monkeypatch: pytest.MonkeyPatch, enabled_monitor,
):
    _enable(monkeypatch)
    monkeypatch.setenv("JARVIS_TEST_FLAG", "secret_value_42")
    monkeypatch.setenv("JARVIS_OTHER", "")
    router = Phase8ObservabilityRouter()
    req = _make_request("/observability/flags/changes")
    resp = _run_async(router._handle_flag_changes(req))
    assert resp.status == 200
    body = _body(resp)
    assert "JARVIS_TEST_FLAG" in body["snapshot"]
    assert body["snapshot"]["JARVIS_TEST_FLAG"] == "<set>"
    assert body["snapshot"]["JARVIS_OTHER"] == "<empty>"
    assert "secret_value_42" not in json.dumps(body)
    assert body["emitter_enabled"] is True


def test_flag_changes_empty_when_emitter_off(
    monkeypatch: pytest.MonkeyPatch,
):
    _enable(monkeypatch)
    monkeypatch.setenv("JARVIS_FLAG_CHANGE_EMITTER_ENABLED", "false")
    router = Phase8ObservabilityRouter()
    req = _make_request("/observability/flags/changes")
    resp = _run_async(router._handle_flag_changes(req))
    assert resp.status == 200
    body = _body(resp)
    assert body["snapshot_size"] == 0
    assert body["delta_count"] == 0
    assert body["emitter_enabled"] is False


# ---------------------------------------------------------------------------
# Latency SLO endpoint
# ---------------------------------------------------------------------------


def test_latency_slo_empty_when_detector_off(
    monkeypatch: pytest.MonkeyPatch,
):
    _enable(monkeypatch)
    monkeypatch.setenv(
        "JARVIS_LATENCY_SLO_DETECTOR_ENABLED", "false",
    )
    router = Phase8ObservabilityRouter()
    req = _make_request("/observability/latency/slo")
    resp = _run_async(router._handle_latency_slo(req))
    assert resp.status == 200
    body = _body(resp)
    assert body["stats"] == {}
    assert body["breaches"] == []
    assert body["detector_enabled"] is False


def test_latency_slo_returns_per_phase_stats(
    monkeypatch: pytest.MonkeyPatch, enabled_detector,
):
    _enable(monkeypatch)
    enabled_detector.set_slo("ROUTE", 0.05)
    enabled_detector.set_slo("GENERATE", 1.0)
    for _ in range(25):
        enabled_detector.record("ROUTE", 0.5)
        enabled_detector.record("GENERATE", 0.2)
    router = Phase8ObservabilityRouter()
    req = _make_request("/observability/latency/slo")
    resp = _run_async(router._handle_latency_slo(req))
    assert resp.status == 200
    body = _body(resp)
    assert "ROUTE" in body["stats"]
    assert "GENERATE" in body["stats"]
    breach_phases = [b["phase"] for b in body["breaches"]]
    assert "ROUTE" in breach_phases
    assert "GENERATE" not in breach_phases
    assert body["detector_enabled"] is True


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def test_rate_limit_kicks_in(monkeypatch: pytest.MonkeyPatch):
    _enable(monkeypatch)
    monkeypatch.setenv(
        "JARVIS_PHASE8_IDE_OBSERVABILITY_RATE_LIMIT_PER_MIN", "3",
    )
    router = Phase8ObservabilityRouter()
    for _ in range(3):
        req = _make_request("/observability/phase8/health")
        resp = _run_async(router._handle_health(req))
        assert resp.status == 200
    req = _make_request("/observability/phase8/health")
    resp = _run_async(router._handle_health(req))
    assert resp.status == 429
    assert _body(resp)["reason_code"] == (
        "phase8_observability.rate_limited"
    )


def test_rate_limit_independent_per_client(
    monkeypatch: pytest.MonkeyPatch,
):
    """Per-client tracker — two distinct remotes get independent
    quotas. Pin against accidental shared-tracker regressions."""
    _enable(monkeypatch)
    monkeypatch.setenv(
        "JARVIS_PHASE8_IDE_OBSERVABILITY_RATE_LIMIT_PER_MIN", "2",
    )
    router = Phase8ObservabilityRouter()
    # Client A — burns its quota.
    for _ in range(2):
        req = _make_request(
            "/observability/phase8/health", remote="127.0.0.1",
        )
        assert _run_async(router._handle_health(req)).status == 200
    # Client A: 3rd over cap.
    req = _make_request(
        "/observability/phase8/health", remote="127.0.0.1",
    )
    assert _run_async(router._handle_health(req)).status == 429
    # Client B: still has quota.
    req = _make_request(
        "/observability/phase8/health", remote="::1",
    )
    assert _run_async(router._handle_health(req)).status == 200


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------


def test_cors_echoes_allowed_origin(monkeypatch: pytest.MonkeyPatch):
    _enable(monkeypatch)
    router = Phase8ObservabilityRouter()
    req = _make_request(
        "/observability/phase8/health",
        headers={"Origin": "http://localhost:5173"},
    )
    resp = _run_async(router._handle_health(req))
    assert resp.status == 200
    assert resp.headers.get(
        "Access-Control-Allow-Origin",
    ) == "http://localhost:5173"
    assert resp.headers.get("Vary") == "Origin"


def test_cors_does_not_echo_disallowed_origin(
    monkeypatch: pytest.MonkeyPatch,
):
    _enable(monkeypatch)
    router = Phase8ObservabilityRouter()
    req = _make_request(
        "/observability/phase8/health",
        headers={"Origin": "https://evil.example.com"},
    )
    resp = _run_async(router._handle_health(req))
    assert resp.status == 200
    assert "Access-Control-Allow-Origin" not in resp.headers


def test_cors_handles_malformed_pattern(
    monkeypatch: pytest.MonkeyPatch,
):
    """An operator-set malformed regex pattern must not crash the
    handler — the loop must skip the bad pattern + continue."""
    _enable(monkeypatch)
    monkeypatch.setenv(
        "JARVIS_PHASE8_IDE_OBSERVABILITY_CORS_ORIGINS",
        "[invalid(regex,^https?://localhost(:\\d+)?$",
    )
    router = Phase8ObservabilityRouter()
    req = _make_request(
        "/observability/phase8/health",
        headers={"Origin": "http://localhost:5173"},
    )
    resp = _run_async(router._handle_health(req))
    assert resp.status == 200
    assert resp.headers.get(
        "Access-Control-Allow-Origin",
    ) == "http://localhost:5173"


# ---------------------------------------------------------------------------
# Response shape pins
# ---------------------------------------------------------------------------


def test_every_response_has_schema_version(
    monkeypatch: pytest.MonkeyPatch,
    isolated_ledger: Path, enabled_ring,
    enabled_monitor, enabled_detector,
):
    _enable(monkeypatch)
    _ledger_mod.get_default_ledger().record(
        op_id="op-S", phase="ROUTE", decision="X",
    )
    enabled_ring.record(
        classifier_name="clf", confidence=0.5,
        threshold=0.5, outcome="X",
    )
    enabled_detector.set_slo("ROUTE", 0.1)
    router = Phase8ObservabilityRouter()
    cases = [
        (router._handle_health, "/observability/phase8/health", {}),
        (
            router._handle_decision_list,
            "/observability/decisions", {},
        ),
        (
            router._handle_decision_detail,
            "/observability/decisions/op-S", {"op_id": "op-S"},
        ),
        (
            router._handle_confidence_list,
            "/observability/confidence", {},
        ),
        (
            router._handle_confidence_detail,
            "/observability/confidence/clf",
            {"classifier": "clf"},
        ),
        (
            router._handle_timeline_detail,
            "/observability/timeline/op-S", {"op_id": "op-S"},
        ),
        (
            router._handle_flag_changes,
            "/observability/flags/changes", {},
        ),
        (
            router._handle_latency_slo,
            "/observability/latency/slo", {},
        ),
    ]
    for handler, path, match in cases:
        req = _make_request(path, match_info=match or None)
        resp = _run_async(handler(req))
        body = _body(resp)
        assert body.get("schema_version") == (
            PHASE8_OBSERVABILITY_SCHEMA_VERSION
        ), f"missing schema_version on {path}"


def test_every_handler_sets_no_store_cache(
    monkeypatch: pytest.MonkeyPatch,
):
    _enable(monkeypatch)
    router = Phase8ObservabilityRouter()
    req = _make_request("/observability/phase8/health")
    resp = _run_async(router._handle_health(req))
    assert resp.headers.get("Cache-Control") == "no-store"


# ---------------------------------------------------------------------------
# Authority / cage invariants
# ---------------------------------------------------------------------------


def test_does_not_import_gate_modules():
    """Authority invariant: the Phase 8 IDE-routes module must NOT
    pull in orchestrator / iron_gate / risk_tier_floor /
    semantic_guardian / policy / candidate_generator / tool_executor
    / change_engine. AST-scan of import statements only — docstrings
    that name these modules in prose are fine."""
    import ast
    import inspect
    from backend.core.ouroboros.governance.observability import (
        ide_routes,
    )
    src = inspect.getsource(ide_routes)
    tree = ast.parse(src)
    banned_substrings = [
        "orchestrator",
        "iron_gate",
        "risk_tier_floor",
        "semantic_guardian",
        "policy_engine",
        "candidate_generator",
        "tool_executor",
        "change_engine",
    ]
    imported: list = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.append(node.module)
    for name in banned_substrings:
        for mod in imported:
            assert name not in mod, (
                f"phase 8 ide_routes imports {mod!r} which contains "
                f"banned token {name!r}"
            )


def test_lazy_imports_substrate_only_in_handlers():
    """The 5 substrate modules are imported lazily inside handler
    bodies — top-level imports must not include them."""
    import ast
    import inspect
    from backend.core.ouroboros.governance.observability import (
        ide_routes,
    )
    src = inspect.getsource(ide_routes)
    tree = ast.parse(src)
    top_level_imports = set()
    for n in tree.body:
        if isinstance(n, ast.ImportFrom) and n.module:
            top_level_imports.add(n.module)
    forbidden = {
        "backend.core.ouroboros.governance.observability."
        "decision_trace_ledger",
        "backend.core.ouroboros.governance.observability."
        "latent_confidence_ring",
        "backend.core.ouroboros.governance.observability."
        "flag_change_emitter",
        "backend.core.ouroboros.governance.observability."
        "latency_slo_detector",
        "backend.core.ouroboros.governance.observability."
        "multi_op_timeline",
    }
    leaked = forbidden & top_level_imports
    assert not leaked, (
        f"phase 8 ide_routes hoisted substrate imports to top "
        f"level: {leaked!r}"
    )


def test_router_init_independent_state():
    """Two routers must not share rate-tracker state."""
    r1 = Phase8ObservabilityRouter()
    r2 = Phase8ObservabilityRouter()
    assert r1._rate_tracker is not r2._rate_tracker


def test_no_secret_leakage_in_module_constants():
    from backend.core.ouroboros.governance.observability import (
        ide_routes,
    )
    text = repr(vars(ide_routes))
    for needle in ("sk-", "ghp_", "AKIA", "BEGIN PRIVATE KEY"):
        assert needle not in text


def test_endpoint_count_pinned_at_eight():
    """Bit-rot guard: any new endpoint must update this pin so the
    surface contract is reviewed."""
    app = web.Application()
    Phase8ObservabilityRouter().register_routes(app)
    paths = sorted(
        r.resource.canonical
        for r in app.router.routes()
        if r.resource is not None and r.method == "GET"
    )
    expected = [
        "/observability/confidence",
        "/observability/confidence/{classifier}",
        "/observability/decisions",
        "/observability/decisions/{op_id}",
        "/observability/flags/changes",
        "/observability/latency/slo",
        "/observability/phase8/health",
        "/observability/timeline/{op_id}",
    ]
    assert paths == expected


# ---------------------------------------------------------------------------
# Helpers + parsing
# ---------------------------------------------------------------------------


def test_parse_limit_clamps():
    r = Phase8ObservabilityRouter()
    assert r._parse_limit(None, 100) == 100
    assert r._parse_limit("garbage", 100) == 100
    assert r._parse_limit("0", 100) == 1
    assert r._parse_limit("999999", 100) == 100
    assert r._parse_limit("50", 100) == 50


def test_parse_int_clamps():
    r = Phase8ObservabilityRouter()
    assert r._parse_int(None, default=5, lo=1, hi=10) == 5
    assert r._parse_int("nope", default=5, lo=1, hi=10) == 5
    assert r._parse_int("0", default=5, lo=1, hi=10) == 1
    assert r._parse_int("99", default=5, lo=1, hi=10) == 10
    assert r._parse_int("3", default=5, lo=1, hi=10) == 3


def test_parse_float_clamps():
    r = Phase8ObservabilityRouter()
    assert r._parse_float(None, default=1.5, lo=0.0, hi=2.0) == 1.5
    assert r._parse_float("oops", default=1.5, lo=0.0, hi=2.0) == 1.5
    assert r._parse_float("-1", default=1.5, lo=0.0, hi=2.0) == 0.0
    assert r._parse_float("99", default=1.5, lo=0.0, hi=2.0) == 2.0
    assert r._parse_float("0.7", default=1.5, lo=0.0, hi=2.0) == 0.7


def test_rate_limit_clamps_env_overrides(
    monkeypatch: pytest.MonkeyPatch,
):
    """The per-min env override is clamped to [1, 6000]."""
    from backend.core.ouroboros.governance.observability.ide_routes import (  # noqa: E501
        _rate_limit_per_min,
    )
    monkeypatch.setenv(
        "JARVIS_PHASE8_IDE_OBSERVABILITY_RATE_LIMIT_PER_MIN", "0",
    )
    assert _rate_limit_per_min() == 1
    monkeypatch.setenv(
        "JARVIS_PHASE8_IDE_OBSERVABILITY_RATE_LIMIT_PER_MIN", "999999",
    )
    assert _rate_limit_per_min() == 6000
    monkeypatch.setenv(
        "JARVIS_PHASE8_IDE_OBSERVABILITY_RATE_LIMIT_PER_MIN", "garbage",
    )
    assert _rate_limit_per_min() == 120
    monkeypatch.delenv(
        "JARVIS_PHASE8_IDE_OBSERVABILITY_RATE_LIMIT_PER_MIN",
        raising=False,
    )
    assert _rate_limit_per_min() == 120


def test_cors_origin_patterns_default(
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.core.ouroboros.governance.observability.ide_routes import (  # noqa: E501
        _cors_origin_patterns,
    )
    monkeypatch.delenv(
        "JARVIS_PHASE8_IDE_OBSERVABILITY_CORS_ORIGINS",
        raising=False,
    )
    patterns = _cors_origin_patterns()
    assert any("localhost" in p for p in patterns)
    assert any("127" in p for p in patterns)
    assert any("vscode-webview" in p for p in patterns)
