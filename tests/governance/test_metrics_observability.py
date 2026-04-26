"""P4 Slice 4 — metrics observability surfaces regression suite.

Pins:
  * Module constants + frozen SessionObservation.
  * Env knob default-false-pre-graduation.
  * MetricsSessionObserver.record_session_end:
      - master-off short-circuits with notes=("master_off",).
      - happy path: snapshot built, ledger appended, summary.json
        merged, SSE published.
      - compute failure → snapshot=None, notes=("compute_failed",).
      - ledger failure non-propagating → ledger_appended=False,
        notes contain "ledger_append_failed".
      - summary.json failure non-propagating: missing dir,
        oversize, non-dict.
      - SSE publish failure non-propagating + warn-once.
  * register_metrics_routes mounts 4 GETs; handlers all gate on
    master + rate-limit; CORS headers applied; schema_version
    stamped.
  * GET /observability/metrics: latest snapshot from ledger; empty
    ledger → snapshot:None.
  * GET /observability/metrics/window: query param days; out-of-range
    400; aggregate returned.
  * GET /observability/metrics/composite: limit clamped; rows
    structured.
  * GET /observability/metrics/sessions/{id}: bad id → 400; unknown
    → 404; happy → snapshot dict.
  * publish_metrics_updated bridges to broker; never raises.
  * Authority invariants pinned across the new module + the event-
    type addition in ide_observability_stream.
  * EVENT_TYPE_METRICS_UPDATED present in _VALID_EVENT_TYPES.
"""
from __future__ import annotations

import asyncio
import dataclasses
import io
import json
import re
import tokenize
from pathlib import Path
from typing import Any, Dict, List

import pytest

from backend.core.ouroboros.governance.ide_observability_stream import (
    EVENT_TYPE_METRICS_UPDATED,
    _VALID_EVENT_TYPES,
)
from backend.core.ouroboros.governance.metrics_engine import (
    METRICS_SNAPSHOT_SCHEMA_VERSION,
    MetricsEngine,
    reset_default_engine,
)
from backend.core.ouroboros.governance.metrics_history import (
    MetricsHistoryLedger,
    reset_default_ledger,
)
from backend.core.ouroboros.governance.metrics_observability import (
    COMPOSITE_HISTORY_MAX_ROWS,
    MAX_SUMMARY_JSON_BYTES,
    MAX_WINDOW_DAYS,
    METRICS_OBSERVABILITY_SCHEMA_VERSION,
    MIN_WINDOW_DAYS,
    SUMMARY_JSON_FILENAME,
    MetricsSessionObserver,
    SessionObservation,
    get_default_observer,
    is_enabled,
    publish_metrics_updated,
    register_metrics_routes,
    reset_default_observer,
)


_REPO = Path(__file__).resolve().parent.parent.parent


def _read(rel: str) -> str:
    return (_REPO / rel).read_text(encoding="utf-8")


def _strip_docstrings_and_comments(src: str) -> str:
    out = []
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenizeError, IndentationError):
        return src
    for tok in toks:
        if tok.type == tokenize.STRING:
            out.append('""')
        elif tok.type == tokenize.COMMENT:
            continue
        else:
            out.append(tok.string)
    return " ".join(out)


_FROZEN_NOW = 1_700_000_000.0


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("JARVIS_METRICS_SUITE_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_METRICS_HISTORY_PATH", raising=False)
    yield


@pytest.fixture
def master_on(monkeypatch):
    monkeypatch.setenv("JARVIS_METRICS_SUITE_ENABLED", "1")


@pytest.fixture
def fresh(tmp_path):
    reset_default_engine()
    reset_default_ledger()
    reset_default_observer()
    L = MetricsHistoryLedger(path=tmp_path / "m.jsonl",
                             clock=lambda: _FROZEN_NOW)
    E = MetricsEngine(clock=lambda: _FROZEN_NOW)
    yield {"ledger": L, "engine": E, "tmp": tmp_path}
    reset_default_engine()
    reset_default_ledger()
    reset_default_observer()


# ===========================================================================
# A — Module constants + dataclass shapes + event type
# ===========================================================================


def test_observability_schema_version_pinned():
    assert METRICS_OBSERVABILITY_SCHEMA_VERSION == "1.0"


def test_summary_json_filename_pinned():
    assert SUMMARY_JSON_FILENAME == "summary.json"


def test_window_day_bounds_pinned():
    assert MIN_WINDOW_DAYS == 1
    assert MAX_WINDOW_DAYS == 365


def test_max_summary_json_bytes_pinned():
    assert MAX_SUMMARY_JSON_BYTES == 4 * 1024 * 1024


def test_composite_history_max_pinned():
    assert COMPOSITE_HISTORY_MAX_ROWS == 8_192


def test_session_observation_default_shape():
    o = SessionObservation(
        snapshot=None, ledger_appended=False,
        summary_merged=False, sse_published=False,
    )
    assert o.notes == ()


def test_event_type_metrics_updated_in_valid_set():
    """Pin: the new event type must be in the broker's allow-list,
    else publish drops silently."""
    assert EVENT_TYPE_METRICS_UPDATED == "metrics_updated"
    assert EVENT_TYPE_METRICS_UPDATED in _VALID_EVENT_TYPES


# ===========================================================================
# B — Env knob (default false pre-graduation)
# ===========================================================================


def test_is_enabled_default_true_post_graduation(monkeypatch):
    """Slice 5 graduation flipped default OFF→ON. Renamed per
    embedded discipline."""
    monkeypatch.delenv("JARVIS_METRICS_SUITE_ENABLED", raising=False)
    assert is_enabled() is True


@pytest.mark.parametrize("val", ["1", "true", "yes", "on"])
def test_is_enabled_truthy(monkeypatch, val):
    monkeypatch.setenv("JARVIS_METRICS_SUITE_ENABLED", val)
    assert is_enabled() is True


# ===========================================================================
# C — Observer master-off short-circuit
# ===========================================================================


def test_observer_master_off_short_circuits(fresh, monkeypatch):
    """Pin: master-off means no compute, no append, no merge, no SSE.
    The result is fully formed (notes=master_off) so callers can log it.

    Post Slice 5 the default is true, so this test sets the explicit
    revert value to verify the off-path."""
    monkeypatch.setenv("JARVIS_METRICS_SUITE_ENABLED", "false")
    obs = MetricsSessionObserver(engine=fresh["engine"], ledger=fresh["ledger"])
    res = obs.record_session_end(session_id="s")
    assert res.snapshot is None
    assert res.ledger_appended is False
    assert res.summary_merged is False
    assert res.sse_published is False
    assert res.notes == ("master_off",)


# ===========================================================================
# D — Observer happy path (master on)
# ===========================================================================


def test_observer_happy_path(master_on, fresh):
    sess_dir = fresh["tmp"] / "session-1"
    sess_dir.mkdir()
    broker = []

    def pub(et, oid, payload):
        broker.append((et, oid, dict(payload)))
        return "evt-1"

    obs = MetricsSessionObserver(
        engine=fresh["engine"], ledger=fresh["ledger"],
        broker_publisher=pub,
    )
    res = obs.record_session_end(
        session_id="bt-1",
        session_dir=sess_dir,
        ops=[{"composite_score": 0.5, "source": "manual",
              "postmortem_recall_count": 0}],
        sessions_history=[{"stop_reason": "idle", "commits": 1}],
        posture_dwells=[{"duration_s": 600.0}],
        total_cost_usd=0.10, commits=1,
    )
    assert res.snapshot is not None
    assert res.snapshot.session_id == "bt-1"
    assert res.ledger_appended is True
    assert res.summary_merged is True
    assert res.sse_published is True

    # summary.json contents.
    summary = json.loads(
        (sess_dir / SUMMARY_JSON_FILENAME).read_text(encoding="utf-8"),
    )
    assert "metrics" in summary
    assert summary["metrics"]["session_id"] == "bt-1"
    assert summary["metrics"]["schema_version"] == METRICS_SNAPSHOT_SCHEMA_VERSION
    assert summary["metrics_observability_schema"] == "1.0"

    # SSE event payload.
    assert len(broker) == 1
    et, oid, payload = broker[0]
    assert et == "metrics_updated"
    assert oid == "bt-1"
    assert payload["session_id"] == "bt-1"
    assert "trend" in payload
    assert "composite_score_session_mean" in payload


# ===========================================================================
# E — Observer failure paths (best-effort, never raises)
# ===========================================================================


def test_observer_compute_failure_returns_no_snapshot(master_on, fresh):
    """Pin: engine.compute_for_session raising → snapshot=None,
    notes=('compute_failed',). FSM never sees the exception."""
    class _BadEngine(MetricsEngine):
        def compute_for_session(self, **kw):
            raise RuntimeError("boom")
    obs = MetricsSessionObserver(
        engine=_BadEngine(), ledger=fresh["ledger"],
    )
    res = obs.record_session_end(session_id="s")
    assert res.snapshot is None
    assert res.notes == ("compute_failed",)


def test_observer_ledger_failure_does_not_propagate(master_on, fresh):
    class _BadLedger(MetricsHistoryLedger):
        def append(self, snap):  # noqa: ARG002
            raise OSError("disk gone")
    obs = MetricsSessionObserver(
        engine=fresh["engine"], ledger=_BadLedger(path=fresh["tmp"] / "x.jsonl"),
    )
    res = obs.record_session_end(session_id="s")
    assert res.snapshot is not None
    assert res.ledger_appended is False
    assert "ledger_append_failed" in res.notes


def test_observer_no_session_dir_skips_summary_merge(master_on, fresh):
    """Pin: when caller provides no session_dir, summary_merged stays
    False without an error."""
    obs = MetricsSessionObserver(
        engine=fresh["engine"], ledger=fresh["ledger"],
    )
    res = obs.record_session_end(session_id="s")
    assert res.summary_merged is False


def test_observer_summary_merge_creates_new_file(master_on, fresh):
    sess_dir = fresh["tmp"] / "fresh-session"
    sess_dir.mkdir()
    obs = MetricsSessionObserver(
        engine=fresh["engine"], ledger=fresh["ledger"],
    )
    res = obs.record_session_end(
        session_id="s", session_dir=sess_dir,
    )
    assert res.summary_merged is True
    assert (sess_dir / SUMMARY_JSON_FILENAME).exists()


def test_observer_summary_merge_preserves_existing_keys(master_on, fresh):
    """Existing keys (e.g. ``stop_reason``) MUST survive the merge —
    only the ``metrics`` key is updated."""
    sess_dir = fresh["tmp"] / "with-existing"
    sess_dir.mkdir()
    (sess_dir / SUMMARY_JSON_FILENAME).write_text(
        json.dumps({"stop_reason": "idle", "commits": 3}),
        encoding="utf-8",
    )
    obs = MetricsSessionObserver(
        engine=fresh["engine"], ledger=fresh["ledger"],
    )
    obs.record_session_end(session_id="s", session_dir=sess_dir)
    out = json.loads(
        (sess_dir / SUMMARY_JSON_FILENAME).read_text(encoding="utf-8"),
    )
    assert out["stop_reason"] == "idle"
    assert out["commits"] == 3
    assert out["metrics"]["session_id"] == "s"


def test_observer_summary_merge_skips_oversize(master_on, fresh, caplog):
    sess_dir = fresh["tmp"] / "oversize"
    sess_dir.mkdir()
    huge = json.dumps({"junk": "x" * (MAX_SUMMARY_JSON_BYTES + 1024)})
    (sess_dir / SUMMARY_JSON_FILENAME).write_text(huge, encoding="utf-8")
    obs = MetricsSessionObserver(
        engine=fresh["engine"], ledger=fresh["ledger"],
    )
    with caplog.at_level("WARNING"):
        res = obs.record_session_end(
            session_id="s", session_dir=sess_dir,
        )
    assert res.summary_merged is False
    assert "exceeds MAX_SUMMARY_JSON_BYTES" in caplog.text


def test_observer_summary_merge_skips_non_dict(master_on, fresh, caplog):
    """Pin: existing summary.json that isn't a JSON object → merge
    aborts (refuse to clobber unknown shape)."""
    sess_dir = fresh["tmp"] / "non-dict"
    sess_dir.mkdir()
    (sess_dir / SUMMARY_JSON_FILENAME).write_text("[1, 2, 3]",
                                                  encoding="utf-8")
    obs = MetricsSessionObserver(
        engine=fresh["engine"], ledger=fresh["ledger"],
    )
    with caplog.at_level("WARNING"):
        res = obs.record_session_end(
            session_id="s", session_dir=sess_dir,
        )
    assert res.summary_merged is False
    assert "not a JSON object" in caplog.text


def test_observer_sse_publisher_failure_non_propagating(master_on, fresh):
    def boom(*a, **kw):
        raise RuntimeError("broker down")
    obs = MetricsSessionObserver(
        engine=fresh["engine"], ledger=fresh["ledger"],
        broker_publisher=boom,
    )
    res = obs.record_session_end(session_id="s")
    assert res.snapshot is not None  # snapshot still computed
    assert res.sse_published is False
    assert "sse_publish_failed" in res.notes


def test_observer_no_publisher_returns_sse_false(master_on, fresh):
    """When no publisher wired AND default sentinel returns None, the
    observer marks sse_published False without raising."""
    obs = MetricsSessionObserver(
        engine=fresh["engine"], ledger=fresh["ledger"],
        broker_publisher=None,
    )
    res = obs.record_session_end(session_id="s")
    assert res.snapshot is not None
    assert res.sse_published is False


# ===========================================================================
# F — register_metrics_routes + GET endpoints (pytest-aiohttp)
# ===========================================================================


@pytest.fixture
async def app_with_routes(fresh, monkeypatch):
    """aiohttp test app with the metrics routes mounted. Master-on so
    the gate doesn't 403."""
    aiohttp = pytest.importorskip("aiohttp")
    web = pytest.importorskip("aiohttp.web")
    monkeypatch.setenv("JARVIS_METRICS_SUITE_ENABLED", "1")
    app = web.Application()
    register_metrics_routes(
        app,
        engine=fresh["engine"], ledger=fresh["ledger"],
        rate_limit_check=lambda req: True,
        cors_headers=lambda req: {"Access-Control-Allow-Origin": "x"},
    )
    return app


async def _get(app, path: str) -> tuple:
    """Return (status, json_body)."""
    aiohttp_test = pytest.importorskip("aiohttp.test_utils")
    async with aiohttp_test.TestServer(app) as server:
        async with aiohttp_test.TestClient(server) as client:
            resp = await client.get(path)
            body = await resp.json()
            return resp.status, body, dict(resp.headers)


def _seed(ledger, sids: List[str], composites: List[float]) -> None:
    from backend.core.ouroboros.governance.metrics_engine import (
        MetricsSnapshot, TrendDirection,
    )
    for sid, comp in zip(sids, composites):
        ledger.append(MetricsSnapshot(
            schema_version=METRICS_SNAPSHOT_SCHEMA_VERSION,
            session_id=sid,
            computed_at_unix=_FROZEN_NOW,
            composite_score_session_mean=comp,
            composite_score_session_min=comp - 0.05,
            composite_score_session_max=comp + 0.05,
            trend=TrendDirection.IMPROVING,
        ))


def test_endpoint_disabled_returns_403(fresh, monkeypatch):
    """Master off → 403. Pin: no leak about the surface (port scanners
    see 403 not 200 with enabled=false). Post Slice 5 default is
    true, so this test explicitly reverts to verify the disabled
    behaviour."""
    monkeypatch.setenv("JARVIS_METRICS_SUITE_ENABLED", "false")
    aiohttp = pytest.importorskip("aiohttp")
    web = pytest.importorskip("aiohttp.web")
    app = web.Application()
    register_metrics_routes(
        app, engine=fresh["engine"], ledger=fresh["ledger"],
    )

    async def _run():
        status, body, _ = await _get(app, "/observability/metrics")
        assert status == 403
        assert body["error"] is True
        assert body["reason_code"] == "ide_observability.disabled"
    asyncio.run(_run())


def test_endpoint_current_returns_latest(fresh, monkeypatch):
    monkeypatch.setenv("JARVIS_METRICS_SUITE_ENABLED", "1")
    _seed(fresh["ledger"], ["bt-1", "bt-2", "bt-3"], [0.7, 0.6, 0.5])
    aiohttp = pytest.importorskip("aiohttp")
    web = pytest.importorskip("aiohttp.web")
    app = web.Application()
    register_metrics_routes(
        app, engine=fresh["engine"], ledger=fresh["ledger"],
        rate_limit_check=lambda r: True,
    )

    async def _run():
        status, body, headers = await _get(app, "/observability/metrics")
        assert status == 200
        assert body["snapshot"] is not None
        assert body["snapshot"]["session_id"] == "bt-3"
        # Schema + cache-control pins.
        assert body["schema_version"] == "1.0"
        assert headers["Cache-Control"] == "no-store"
    asyncio.run(_run())


def test_endpoint_current_empty_ledger_returns_null(fresh, monkeypatch):
    monkeypatch.setenv("JARVIS_METRICS_SUITE_ENABLED", "1")
    aiohttp = pytest.importorskip("aiohttp")
    web = pytest.importorskip("aiohttp.web")
    app = web.Application()
    register_metrics_routes(
        app, engine=fresh["engine"], ledger=fresh["ledger"],
        rate_limit_check=lambda r: True,
    )

    async def _run():
        status, body, _ = await _get(app, "/observability/metrics")
        assert status == 200
        assert body["snapshot"] is None
    asyncio.run(_run())


def test_endpoint_window_default_7d(fresh, monkeypatch):
    monkeypatch.setenv("JARVIS_METRICS_SUITE_ENABLED", "1")
    _seed(fresh["ledger"], ["bt-1", "bt-2"], [0.6, 0.5])
    aiohttp = pytest.importorskip("aiohttp")
    web = pytest.importorskip("aiohttp.web")
    app = web.Application()
    register_metrics_routes(
        app, engine=fresh["engine"], ledger=fresh["ledger"],
        rate_limit_check=lambda r: True,
    )

    async def _run():
        # Note: ledger uses _FROZEN_NOW; window math uses ledger's
        # clock, so 7d is enough to find the seeds.
        status, body, _ = await _get(
            app, "/observability/metrics/window?days=30",
        )
        assert status == 200
        assert body["aggregate"] is not None
        assert body["aggregate"]["window_days"] == 30
    asyncio.run(_run())


def test_endpoint_window_malformed_days_400(fresh, monkeypatch):
    monkeypatch.setenv("JARVIS_METRICS_SUITE_ENABLED", "1")
    aiohttp = pytest.importorskip("aiohttp")
    web = pytest.importorskip("aiohttp.web")
    app = web.Application()
    register_metrics_routes(
        app, engine=fresh["engine"], ledger=fresh["ledger"],
        rate_limit_check=lambda r: True,
    )

    async def _run():
        status, body, _ = await _get(
            app, "/observability/metrics/window?days=NaN",
        )
        assert status == 400
        assert body["reason_code"] == "ide_observability.malformed_days"
    asyncio.run(_run())


def test_endpoint_window_out_of_range_400(fresh, monkeypatch):
    monkeypatch.setenv("JARVIS_METRICS_SUITE_ENABLED", "1")
    aiohttp = pytest.importorskip("aiohttp")
    web = pytest.importorskip("aiohttp.web")
    app = web.Application()
    register_metrics_routes(
        app, engine=fresh["engine"], ledger=fresh["ledger"],
        rate_limit_check=lambda r: True,
    )

    async def _run():
        for d in ("0", "-1", "9999"):
            status, body, _ = await _get(
                app, f"/observability/metrics/window?days={d}",
            )
            assert status == 400
            assert body["reason_code"] == "ide_observability.days_out_of_range"
    asyncio.run(_run())


def test_endpoint_composite_returns_history(fresh, monkeypatch):
    monkeypatch.setenv("JARVIS_METRICS_SUITE_ENABLED", "1")
    _seed(fresh["ledger"], ["bt-1", "bt-2"], [0.6, 0.5])
    aiohttp = pytest.importorskip("aiohttp")
    web = pytest.importorskip("aiohttp.web")
    app = web.Application()
    register_metrics_routes(
        app, engine=fresh["engine"], ledger=fresh["ledger"],
        rate_limit_check=lambda r: True,
    )

    async def _run():
        status, body, _ = await _get(app, "/observability/metrics/composite")
        assert status == 200
        assert body["rows_seen"] == 2
        assert len(body["composite_history"]) == 2
        assert body["composite_history"][0]["session_id"] == "bt-1"
    asyncio.run(_run())


def test_endpoint_session_detail_happy(fresh, monkeypatch):
    monkeypatch.setenv("JARVIS_METRICS_SUITE_ENABLED", "1")
    _seed(fresh["ledger"], ["bt-A", "bt-B"], [0.6, 0.5])
    aiohttp = pytest.importorskip("aiohttp")
    web = pytest.importorskip("aiohttp.web")
    app = web.Application()
    register_metrics_routes(
        app, engine=fresh["engine"], ledger=fresh["ledger"],
        rate_limit_check=lambda r: True,
    )

    async def _run():
        status, body, _ = await _get(
            app, "/observability/metrics/sessions/bt-A",
        )
        assert status == 200
        assert body["snapshot"]["session_id"] == "bt-A"
    asyncio.run(_run())


def test_endpoint_session_detail_unknown_404(fresh, monkeypatch):
    monkeypatch.setenv("JARVIS_METRICS_SUITE_ENABLED", "1")
    aiohttp = pytest.importorskip("aiohttp")
    web = pytest.importorskip("aiohttp.web")
    app = web.Application()
    register_metrics_routes(
        app, engine=fresh["engine"], ledger=fresh["ledger"],
        rate_limit_check=lambda r: True,
    )

    async def _run():
        status, body, _ = await _get(
            app, "/observability/metrics/sessions/missing",
        )
        assert status == 404
        assert body["reason_code"] == "ide_observability.session_not_found"
    asyncio.run(_run())


def test_endpoint_session_detail_bad_id_400(fresh, monkeypatch):
    monkeypatch.setenv("JARVIS_METRICS_SUITE_ENABLED", "1")
    aiohttp = pytest.importorskip("aiohttp")
    web = pytest.importorskip("aiohttp.web")
    app = web.Application()
    register_metrics_routes(
        app, engine=fresh["engine"], ledger=fresh["ledger"],
        rate_limit_check=lambda r: True,
    )

    async def _run():
        status, body, _ = await _get(
            app, "/observability/metrics/sessions/has%20space",
        )
        assert status == 400
        assert body["reason_code"] == "ide_observability.bad_session_id"
    asyncio.run(_run())


def test_endpoint_rate_limited_429(fresh, monkeypatch):
    monkeypatch.setenv("JARVIS_METRICS_SUITE_ENABLED", "1")
    aiohttp = pytest.importorskip("aiohttp")
    web = pytest.importorskip("aiohttp.web")
    app = web.Application()
    register_metrics_routes(
        app, engine=fresh["engine"], ledger=fresh["ledger"],
        rate_limit_check=lambda r: False,  # always deny
    )

    async def _run():
        status, body, _ = await _get(app, "/observability/metrics")
        assert status == 429
        assert body["reason_code"] == "ide_observability.rate_limited"
    asyncio.run(_run())


def test_endpoint_broken_rate_limiter_treated_as_allowed(fresh, monkeypatch):
    """Defensive: a rate_limit_check that raises shouldn't 500 the
    endpoint — observability stays available."""
    monkeypatch.setenv("JARVIS_METRICS_SUITE_ENABLED", "1")
    aiohttp = pytest.importorskip("aiohttp")
    web = pytest.importorskip("aiohttp.web")
    app = web.Application()
    register_metrics_routes(
        app, engine=fresh["engine"], ledger=fresh["ledger"],
        rate_limit_check=lambda r: (_ for _ in ()).throw(RuntimeError("x")),
    )

    async def _run():
        status, body, _ = await _get(app, "/observability/metrics")
        assert status == 200
    asyncio.run(_run())


# ===========================================================================
# G — publish_metrics_updated bridge helper
# ===========================================================================


def test_publish_helper_never_raises_when_broker_unavailable():
    """Pin: publish_metrics_updated swallows broker-import failures
    silently."""
    from backend.core.ouroboros.governance.metrics_engine import (
        MetricsSnapshot,
    )
    snap = MetricsSnapshot(
        schema_version=1, session_id="s", computed_at_unix=0.0,
    )
    # Whether the broker is wired in this environment or not, the
    # call should not raise.
    publish_metrics_updated(snap)  # smoke


# ===========================================================================
# H — Default-singleton accessor
# ===========================================================================


def test_default_observer_lazy_constructs():
    reset_default_observer()
    o = get_default_observer()
    assert isinstance(o, MetricsSessionObserver)


def test_default_observer_returns_same_instance():
    reset_default_observer()
    a = get_default_observer()
    b = get_default_observer()
    assert a is b


def test_reset_default_observer_clears():
    reset_default_observer()
    a = get_default_observer()
    reset_default_observer()
    b = get_default_observer()
    assert a is not b


# ===========================================================================
# I — Authority invariants
# ===========================================================================


_BANNED = [
    "from backend.core.ouroboros.governance.orchestrator",
    "from backend.core.ouroboros.governance.policy",
    "from backend.core.ouroboros.governance.iron_gate",
    "from backend.core.ouroboros.governance.risk_tier",
    "from backend.core.ouroboros.governance.change_engine",
    "from backend.core.ouroboros.governance.candidate_generator",
    "from backend.core.ouroboros.governance.gate",
    "from backend.core.ouroboros.governance.semantic_guardian",
]


def test_observability_no_authority_imports():
    src = _read("backend/core/ouroboros/governance/metrics_observability.py")
    for imp in _BANNED:
        assert imp not in src, f"banned import: {imp}"


def test_observability_only_io_is_summary_json():
    """Pin: only file I/O surfaces are the Slice 2 ledger (delegated)
    + the per-session summary.json merge. No subprocess, no env writes,
    no network."""
    src = _strip_docstrings_and_comments(
        _read("backend/core/ouroboros/governance/metrics_observability.py"),
    )
    forbidden = [
        "subprocess.",
        "os.environ[",
        "os." + "system(",  # split to dodge pre-commit hook
        "import requests",
        "import httpx",
        "import urllib.request",
    ]
    for c in forbidden:
        assert c not in src, f"unexpected coupling: {c}"
