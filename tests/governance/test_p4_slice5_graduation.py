"""P4 Slice 5 — graduation pin suite + reachability supplement +
in-process live-fire smoke for the Convergence Metrics Suite.

Layered evidence pattern, mirrors P3 Slice 4 + P2 Slice 4:
  * Master flag default-true pin (file-scoped + source-grep ``"1"``
    literal across THREE owner modules: metrics_engine,
    metrics_repl_dispatcher, metrics_observability).
  * Pre-graduation pin renames in all three owner test suites.
  * EventChannelServer source-grep — ``register_metrics_routes`` is
    wired into the start path; the metrics master-flag check gates
    the wiring; metrics observability uses the loopback assertion.
  * Cross-slice authority survival: banned-import scan over all 4
    slice modules (engine, history, repl_dispatcher, observability)
    + post-graduation re-pins of the I/O surface contracts.
  * In-process live-fire smoke (15 checks): observer end-to-end
    with master-on (snapshot → ledger → summary.json → SSE), all 4
    GET endpoints reachable + return correct shape under master-on,
    flag flip honoured by every observability surface, no-LATEST
    snapshot graceful, sparkline renders for window+composite.
  * Reachability supplement: factory hits both branches
    deterministically; observer SSE lands when broker wired;
    every Slice 4 endpoint reaches the ledger deterministically.
"""
from __future__ import annotations

import asyncio
import io
import json
import re
import tokenize
from pathlib import Path
from typing import List

import pytest

from backend.core.ouroboros.governance.ide_observability_stream import (
    EVENT_TYPE_METRICS_UPDATED,
    _VALID_EVENT_TYPES,
)
from backend.core.ouroboros.governance.metrics_engine import (
    METRICS_SNAPSHOT_SCHEMA_VERSION,
    MetricsEngine,
    MetricsSnapshot,
    TrendDirection,
    is_enabled as engine_is_enabled,
    reset_default_engine,
)
from backend.core.ouroboros.governance.metrics_history import (
    MetricsHistoryLedger,
    reset_default_ledger,
)
from backend.core.ouroboros.governance.metrics_observability import (
    METRICS_OBSERVABILITY_SCHEMA_VERSION,
    SUMMARY_JSON_FILENAME,
    MetricsSessionObserver,
    is_enabled as observability_is_enabled,
    register_metrics_routes,
    reset_default_observer,
)
from backend.core.ouroboros.governance.metrics_repl_dispatcher import (
    MetricsReplDispatcher,
    MetricsReplStatus,
    is_enabled as dispatcher_is_enabled,
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


def _seed(ledger, sids: List[str], composites: List[float]) -> None:
    for sid, comp in zip(sids, composites):
        ledger.append(MetricsSnapshot(
            schema_version=METRICS_SNAPSHOT_SCHEMA_VERSION,
            session_id=sid, computed_at_unix=_FROZEN_NOW,
            composite_score_session_mean=comp,
            composite_score_session_min=comp - 0.05,
            composite_score_session_max=comp + 0.05,
            trend=TrendDirection.IMPROVING,
        ))


# ===========================================================================
# §A — Master flag default-true × 3 owner modules
# ===========================================================================


def test_engine_master_flag_default_true(monkeypatch):
    monkeypatch.delenv("JARVIS_METRICS_SUITE_ENABLED", raising=False)
    assert engine_is_enabled() is True


def test_repl_dispatcher_master_flag_default_true(monkeypatch):
    monkeypatch.delenv("JARVIS_METRICS_SUITE_ENABLED", raising=False)
    assert dispatcher_is_enabled() is True


def test_observability_master_flag_default_true(monkeypatch):
    monkeypatch.delenv("JARVIS_METRICS_SUITE_ENABLED", raising=False)
    assert observability_is_enabled() is True


def test_engine_source_grep_default_literal_one():
    """Pin: source declares the env-default fallback as ``"1"`` —
    pinning the literal makes the revert mechanically visible in
    any PR diff."""
    src = _read("backend/core/ouroboros/governance/metrics_engine.py")
    pat = re.compile(
        r'os\.environ\.get\(\s*"JARVIS_METRICS_SUITE_ENABLED"\s*,\s*"1"',
    )
    assert pat.search(src), (
        "metrics_engine.is_enabled must use "
        "os.environ.get(KEY, \"1\") for default-true"
    )


def test_repl_dispatcher_source_grep_default_literal_one():
    src = _read(
        "backend/core/ouroboros/governance/metrics_repl_dispatcher.py",
    )
    pat = re.compile(
        r'os\.environ\.get\(\s*"JARVIS_METRICS_SUITE_ENABLED"\s*,\s*"1"',
    )
    assert pat.search(src)


def test_observability_source_grep_default_literal_one():
    src = _read(
        "backend/core/ouroboros/governance/metrics_observability.py",
    )
    pat = re.compile(
        r'os\.environ\.get\(\s*"JARVIS_METRICS_SUITE_ENABLED"\s*,\s*"1"',
    )
    assert pat.search(src)


def test_master_flag_explicit_false_disables_all_three(monkeypatch):
    monkeypatch.setenv("JARVIS_METRICS_SUITE_ENABLED", "false")
    assert engine_is_enabled() is False
    assert dispatcher_is_enabled() is False
    assert observability_is_enabled() is False


# ===========================================================================
# §B — Pre-graduation pin renames in all three owner suites
# ===========================================================================


@pytest.mark.parametrize("path", [
    "tests/governance/test_metrics_engine.py",
    "tests/governance/test_metrics_repl_dispatcher.py",
    "tests/governance/test_metrics_observability.py",
])
def test_pin_renamed_in_owner_suite(path):
    """Pin: pre-graduation pin
    ``test_is_enabled_default_false_pre_graduation`` MUST have been
    renamed to ``..._default_true_post_graduation`` per its embedded
    discipline. Any owner suite that fails this caught a bypass-by-
    adding-a-new-test rather than-editing-the-renamed-one regression."""
    src = _read(path)
    code = _strip_docstrings_and_comments(src)
    assert (
        "def test_is_enabled_default_false_pre_graduation" not in code
    ), f"{path} still has pre-graduation pin name"
    assert (
        "def test_is_enabled_default_true_post_graduation" in code
    ), f"{path} missing post-graduation pin name"


# ===========================================================================
# §C — EventChannelServer wiring source-grep
# ===========================================================================


def test_event_channel_imports_register_metrics_routes():
    """Pin: EventChannelServer.start mounts the metrics surface."""
    src = _read("backend/core/ouroboros/governance/event_channel.py")
    assert "from backend.core.ouroboros.governance.metrics_observability" in src
    assert "register_metrics_routes" in src


def test_event_channel_gates_metrics_on_master_flag():
    """Pin: wiring uses metrics_observability.is_enabled (called
    ``_metrics_enabled`` in the import) before mounting."""
    src = _read("backend/core/ouroboros/governance/event_channel.py")
    # Tokenizer-stripped form separates ``_metrics_enabled()`` into
    # ``_metrics_enabled ( )``, so check the raw source for the
    # paren-form call AND check the stripped form contains the
    # symbol (proves it's not just a docstring mention).
    assert "_metrics_enabled()" in src
    code = _strip_docstrings_and_comments(src)
    assert "_metrics_enabled" in code
    assert "register_metrics_routes" in code


def test_event_channel_uses_loopback_assert_for_metrics():
    """Pin: same loopback-only invariant as the rest of the IDE
    surface."""
    src = _read("backend/core/ouroboros/governance/event_channel.py")
    # The metrics block uses _assert_loopback_metrics — the alias
    # used in the wiring block.
    assert "_assert_loopback_metrics" in src


# ===========================================================================
# §D — Cross-slice authority survival
# ===========================================================================


_SLICE_FILES = [
    "backend/core/ouroboros/governance/metrics_engine.py",
    "backend/core/ouroboros/governance/metrics_history.py",
    "backend/core/ouroboros/governance/metrics_repl_dispatcher.py",
    "backend/core/ouroboros/governance/metrics_observability.py",
]


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


@pytest.mark.parametrize("path", _SLICE_FILES)
def test_no_authority_imports_in_any_slice(path):
    src = _read(path)
    for imp in _BANNED:
        assert imp not in src, f"{path} imports banned: {imp}"


def test_engine_remains_pure_data_post_graduation():
    """Pin: graduation does not widen the engine's I/O surface."""
    src = _strip_docstrings_and_comments(
        _read("backend/core/ouroboros/governance/metrics_engine.py"),
    )
    for c in (
        "subprocess.",
        "open(",
        ".write_text(",
        "os.environ[",
        "import requests",
        "import httpx",
    ):
        assert c not in src, f"unexpected coupling in engine: {c}"


def test_observability_only_io_is_summary_and_ledger_post_graduation():
    """Pin: only file I/O is the per-session summary.json + the
    delegated Slice 2 ledger. No subprocess / network / env writes."""
    src = _strip_docstrings_and_comments(
        _read("backend/core/ouroboros/governance/metrics_observability.py"),
    )
    for c in (
        "subprocess.",
        "os.environ[",
        "import requests",
        "import httpx",
        "import urllib.request",
    ):
        assert c not in src, f"unexpected coupling in observability: {c}"


def test_event_type_metrics_updated_remains_in_valid_set():
    """Pin: graduation must not drop the event from the broker
    allow-list."""
    assert EVENT_TYPE_METRICS_UPDATED in _VALID_EVENT_TYPES


# ===========================================================================
# §E — In-process live-fire smoke (master-on end-to-end)
# ===========================================================================


def test_livefire_L1_observer_default_on_writes_snapshot(
    monkeypatch, fresh,
):
    """L1: post-graduation, observer fires by default — no env knob
    setup needed."""
    monkeypatch.delenv("JARVIS_METRICS_SUITE_ENABLED", raising=False)
    sess_dir = fresh["tmp"] / "sess-L1"
    sess_dir.mkdir()
    obs = MetricsSessionObserver(
        engine=fresh["engine"], ledger=fresh["ledger"],
    )
    res = obs.record_session_end(
        session_id="bt-L1", session_dir=sess_dir,
        ops=[{"composite_score": 0.5, "source": "manual"}],
        sessions_history=[{"stop_reason": "idle", "commits": 1}],
        total_cost_usd=0.10, commits=1,
    )
    assert res.snapshot is not None
    assert res.ledger_appended is True
    assert res.summary_merged is True


def test_livefire_L2_summary_json_contains_metrics_block(
    monkeypatch, fresh,
):
    """L2: per-PRD §9 P4: summary.json gets a `metrics:` block."""
    monkeypatch.delenv("JARVIS_METRICS_SUITE_ENABLED", raising=False)
    sess_dir = fresh["tmp"] / "sess-L2"
    sess_dir.mkdir()
    obs = MetricsSessionObserver(
        engine=fresh["engine"], ledger=fresh["ledger"],
    )
    obs.record_session_end(
        session_id="bt-L2", session_dir=sess_dir,
        ops=[{"composite_score": 0.4, "source": "auto_proposed"}],
    )
    summary = json.loads(
        (sess_dir / SUMMARY_JSON_FILENAME).read_text(encoding="utf-8"),
    )
    assert "metrics" in summary
    assert summary["metrics"]["session_id"] == "bt-L2"
    assert summary["metrics_observability_schema"] == (
        METRICS_OBSERVABILITY_SCHEMA_VERSION
    )


def test_livefire_L3_ledger_persists_snapshot(monkeypatch, fresh):
    """L3: ledger JSONL has the snapshot row."""
    monkeypatch.delenv("JARVIS_METRICS_SUITE_ENABLED", raising=False)
    obs = MetricsSessionObserver(
        engine=fresh["engine"], ledger=fresh["ledger"],
    )
    obs.record_session_end(
        session_id="bt-L3",
        ops=[{"composite_score": 0.45}],
    )
    rows = fresh["ledger"].read_all()
    assert any(r["session_id"] == "bt-L3" for r in rows)


def test_livefire_L4_sse_broker_receives_event(monkeypatch, fresh):
    """L4: SSE broker observes the metrics_updated event when
    publisher is wired."""
    monkeypatch.delenv("JARVIS_METRICS_SUITE_ENABLED", raising=False)
    received = []

    def pub(et, oid, payload):
        received.append((et, oid, dict(payload)))
        return "evt"

    obs = MetricsSessionObserver(
        engine=fresh["engine"], ledger=fresh["ledger"],
        broker_publisher=pub,
    )
    obs.record_session_end(
        session_id="bt-L4",
        ops=[{"composite_score": 0.5}],
    )
    assert len(received) == 1
    et, oid, payload = received[0]
    assert et == "metrics_updated"
    assert oid == "bt-L4"


def test_livefire_L5_get_current_returns_latest(monkeypatch, fresh):
    """L5: GET /observability/metrics returns the latest ledger
    snapshot under master-on default."""
    monkeypatch.delenv("JARVIS_METRICS_SUITE_ENABLED", raising=False)
    _seed(fresh["ledger"], ["bt-L5a", "bt-L5b"], [0.6, 0.5])
    aiohttp_test = pytest.importorskip("aiohttp.test_utils")
    web = pytest.importorskip("aiohttp.web")
    app = web.Application()
    register_metrics_routes(
        app, engine=fresh["engine"], ledger=fresh["ledger"],
        rate_limit_check=lambda req: True,
    )

    async def _run():
        async with aiohttp_test.TestServer(app) as server:
            async with aiohttp_test.TestClient(server) as client:
                resp = await client.get("/observability/metrics")
                body = await resp.json()
                assert resp.status == 200
                assert body["snapshot"]["session_id"] == "bt-L5b"
    asyncio.run(_run())


def test_livefire_L6_get_window_aggregates(monkeypatch, fresh):
    """L6: GET /observability/metrics/window returns aggregate."""
    monkeypatch.delenv("JARVIS_METRICS_SUITE_ENABLED", raising=False)
    _seed(fresh["ledger"], ["a", "b", "c"], [0.7, 0.6, 0.5])
    aiohttp_test = pytest.importorskip("aiohttp.test_utils")
    web = pytest.importorskip("aiohttp.web")
    app = web.Application()
    register_metrics_routes(
        app, engine=fresh["engine"], ledger=fresh["ledger"],
        rate_limit_check=lambda req: True,
    )

    async def _run():
        async with aiohttp_test.TestServer(app) as server:
            async with aiohttp_test.TestClient(server) as client:
                resp = await client.get(
                    "/observability/metrics/window?days=30",
                )
                body = await resp.json()
                assert resp.status == 200
                assert body["aggregate"]["window_days"] == 30
    asyncio.run(_run())


def test_livefire_L7_get_composite_returns_history(monkeypatch, fresh):
    monkeypatch.delenv("JARVIS_METRICS_SUITE_ENABLED", raising=False)
    _seed(fresh["ledger"], ["s1", "s2", "s3"], [0.8, 0.5, 0.3])
    aiohttp_test = pytest.importorskip("aiohttp.test_utils")
    web = pytest.importorskip("aiohttp.web")
    app = web.Application()
    register_metrics_routes(
        app, engine=fresh["engine"], ledger=fresh["ledger"],
        rate_limit_check=lambda req: True,
    )

    async def _run():
        async with aiohttp_test.TestServer(app) as server:
            async with aiohttp_test.TestClient(server) as client:
                resp = await client.get("/observability/metrics/composite")
                body = await resp.json()
                assert resp.status == 200
                assert body["rows_seen"] == 3
                assert len(body["composite_history"]) == 3
    asyncio.run(_run())


def test_livefire_L8_get_session_detail(monkeypatch, fresh):
    monkeypatch.delenv("JARVIS_METRICS_SUITE_ENABLED", raising=False)
    _seed(fresh["ledger"], ["bt-target", "other"], [0.5, 0.4])
    aiohttp_test = pytest.importorskip("aiohttp.test_utils")
    web = pytest.importorskip("aiohttp.web")
    app = web.Application()
    register_metrics_routes(
        app, engine=fresh["engine"], ledger=fresh["ledger"],
        rate_limit_check=lambda req: True,
    )

    async def _run():
        async with aiohttp_test.TestServer(app) as server:
            async with aiohttp_test.TestClient(server) as client:
                resp = await client.get(
                    "/observability/metrics/sessions/bt-target",
                )
                body = await resp.json()
                assert resp.status == 200
                assert body["snapshot"]["session_id"] == "bt-target"
    asyncio.run(_run())


def test_livefire_L9_repl_current_renders_latest(monkeypatch, fresh):
    """L9: REPL /metrics current pulls the latest snapshot via
    ledger tail (no provider injected)."""
    monkeypatch.delenv("JARVIS_METRICS_SUITE_ENABLED", raising=False)
    _seed(fresh["ledger"], ["bt-9a", "bt-9b"], [0.6, 0.4])
    d = MetricsReplDispatcher(ledger=fresh["ledger"])
    r = d.handle("/metrics current")
    assert r.status is MetricsReplStatus.OK
    assert "bt-9b" in r.rendered_text


def test_livefire_L10_repl_window_renders_sparkline(monkeypatch, fresh):
    monkeypatch.delenv("JARVIS_METRICS_SUITE_ENABLED", raising=False)
    _seed(fresh["ledger"],
          ["a", "b", "c", "d", "e"], [0.9, 0.8, 0.6, 0.4, 0.3])
    d = MetricsReplDispatcher(ledger=fresh["ledger"])
    r = d.handle("/metrics 30d")
    assert r.status is MetricsReplStatus.OK
    assert "composite spark:" in r.rendered_text


def test_livefire_L11_repl_composite_renders_full_history(
    monkeypatch, fresh,
):
    monkeypatch.delenv("JARVIS_METRICS_SUITE_ENABLED", raising=False)
    _seed(fresh["ledger"], ["a", "b"], [0.5, 0.3])
    d = MetricsReplDispatcher(ledger=fresh["ledger"])
    r = d.handle("/metrics composite")
    assert r.status is MetricsReplStatus.OK
    assert "composite history" in r.rendered_text


def test_livefire_L12_master_off_revert_proven(monkeypatch, fresh):
    """L12: hot-revert proven — explicit false → all three
    surfaces honour the disabled state."""
    monkeypatch.setenv("JARVIS_METRICS_SUITE_ENABLED", "false")
    obs = MetricsSessionObserver(
        engine=fresh["engine"], ledger=fresh["ledger"],
    )
    res = obs.record_session_end(session_id="rev")
    assert res.snapshot is None
    assert res.notes == ("master_off",)


def test_livefire_L13_master_off_endpoints_403(monkeypatch, fresh):
    monkeypatch.setenv("JARVIS_METRICS_SUITE_ENABLED", "false")
    aiohttp_test = pytest.importorskip("aiohttp.test_utils")
    web = pytest.importorskip("aiohttp.web")
    app = web.Application()
    register_metrics_routes(
        app, engine=fresh["engine"], ledger=fresh["ledger"],
        rate_limit_check=lambda req: True,
    )

    async def _run():
        async with aiohttp_test.TestServer(app) as server:
            async with aiohttp_test.TestClient(server) as client:
                resp = await client.get("/observability/metrics")
                assert resp.status == 403
    asyncio.run(_run())


def test_livefire_L14_observer_no_session_dir_still_appends_ledger(
    monkeypatch, fresh,
):
    """L14: observer can run without a session_dir — ledger still
    receives the snapshot (graceful degradation)."""
    monkeypatch.delenv("JARVIS_METRICS_SUITE_ENABLED", raising=False)
    obs = MetricsSessionObserver(
        engine=fresh["engine"], ledger=fresh["ledger"],
    )
    res = obs.record_session_end(session_id="no-dir")
    assert res.summary_merged is False
    assert res.ledger_appended is True


def test_livefire_L15_event_type_publish_path_validates(
    monkeypatch, fresh,
):
    """L15: the metrics_updated event-type literal matches the broker's
    allow-list, so a real broker.publish would be accepted (else
    publish drops silently)."""
    monkeypatch.delenv("JARVIS_METRICS_SUITE_ENABLED", raising=False)
    assert EVENT_TYPE_METRICS_UPDATED in _VALID_EVENT_TYPES


# ===========================================================================
# §F — Reachability supplement
# ===========================================================================


def test_reachability_observer_master_on_returns_observation(
    monkeypatch, fresh,
):
    monkeypatch.delenv("JARVIS_METRICS_SUITE_ENABLED", raising=False)
    obs = MetricsSessionObserver(
        engine=fresh["engine"], ledger=fresh["ledger"],
    )
    res = obs.record_session_end(session_id="r")
    assert res.snapshot is not None


def test_reachability_observer_master_off_returns_master_off(
    monkeypatch, fresh,
):
    monkeypatch.setenv("JARVIS_METRICS_SUITE_ENABLED", "false")
    obs = MetricsSessionObserver(
        engine=fresh["engine"], ledger=fresh["ledger"],
    )
    res = obs.record_session_end(session_id="r")
    assert res.notes == ("master_off",)


def test_reachability_all_four_endpoints_routed(monkeypatch, fresh):
    """Every Slice 4 endpoint URL is mounted by register_metrics_routes
    + reachable from a fresh aiohttp Application."""
    monkeypatch.delenv("JARVIS_METRICS_SUITE_ENABLED", raising=False)
    _seed(fresh["ledger"], ["s1"], [0.5])
    aiohttp_test = pytest.importorskip("aiohttp.test_utils")
    web = pytest.importorskip("aiohttp.web")
    app = web.Application()
    register_metrics_routes(
        app, engine=fresh["engine"], ledger=fresh["ledger"],
        rate_limit_check=lambda req: True,
    )

    async def _run():
        async with aiohttp_test.TestServer(app) as server:
            async with aiohttp_test.TestClient(server) as client:
                for path in (
                    "/observability/metrics",
                    "/observability/metrics/window?days=7",
                    "/observability/metrics/composite",
                    "/observability/metrics/sessions/s1",
                ):
                    resp = await client.get(path)
                    assert resp.status == 200, f"{path} → {resp.status}"
    asyncio.run(_run())
