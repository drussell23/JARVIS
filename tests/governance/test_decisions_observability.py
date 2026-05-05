"""Upgrade 2 Slice 3 — /decisions REPL + observability tests
(PRD §31.3).

Pins:
  § 1 — `decisions_reader` shared primitives
  § 2 — HTTP route handlers (overview / session detail / 503/429/400/404)
  § 3 — `register_routes` mounts both endpoints
  § 4 — `/decisions` REPL dispatcher (all 5 subcommands)
  § 5 — `register_verbs` auto-discovery
  § 6 — Authority floor (no orchestrator/iron_gate imports anywhere)
  § 7 — Read-only contract (no mutation calls in observability/REPL)
  § 8 — Public exports
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_ledger(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JARVIS_DETERMINISM_LEDGER_DIR", str(tmp_path),
    )
    monkeypatch.setenv(
        "JARVIS_DETERMINISM_LEDGER_ENABLED", "true",
    )


def _write_session(tmp_path, sid, n=5):
    d = tmp_path / sid
    d.mkdir(parents=True, exist_ok=True)
    rows = []
    for i in range(n):
        rows.append({
            "record_id": f"{sid}-{i}", "session_id": sid,
            "op_id": f"op-{i}", "phase": "ROUTE",
            "kind": (
                "route_selection" if i % 2 == 0
                else "gate_pass"
            ),
            "ordinal": i, "inputs_hash": "h",
            "output_repr": '{}',
            "monotonic_ts": float(i), "wall_ts": float(i),
            "schema_version": "decision_record.1",
        })
    path = d / "decisions.jsonl"
    path.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n",
    )
    return path


def _make_request(*, match_info=None, query=None):
    req = MagicMock()
    req.match_info = match_info or {}
    req.query = query or {}
    return req


# ---------------------------------------------------------------------------
# § 1 — decisions_reader primitives
# ---------------------------------------------------------------------------


class TestDecisionsReader:
    def test_list_available_sessions_empty(
        self, monkeypatch, tmp_path,
    ):
        _setup_ledger(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.determinism.decisions_reader import (  # noqa: E501
            list_available_sessions,
        )
        assert list_available_sessions() == ()

    def test_list_available_sessions_orders_by_mtime_desc(
        self, monkeypatch, tmp_path,
    ):
        _setup_ledger(monkeypatch, tmp_path)
        _write_session(tmp_path, "old-session", 2)
        time.sleep(0.05)
        _write_session(tmp_path, "new-session", 2)
        from backend.core.ouroboros.governance.determinism.decisions_reader import (  # noqa: E501
            list_available_sessions,
        )
        sessions = list_available_sessions()
        assert len(sessions) == 2
        # Newest first
        assert sessions[0].session_id == "new-session"
        assert sessions[1].session_id == "old-session"

    def test_read_records_returns_tail(
        self, monkeypatch, tmp_path,
    ):
        _setup_ledger(monkeypatch, tmp_path)
        _write_session(tmp_path, "x", 10)
        from backend.core.ouroboros.governance.determinism.decisions_reader import (  # noqa: E501
            read_records_for_session,
        )
        result = read_records_for_session("x", limit=3)
        assert result.total_records_in_file == 10
        assert len(result.records) == 3
        # Tail returned (most recent)
        assert result.records[-1]["record_id"] == "x-9"

    def test_read_records_kind_filter(
        self, monkeypatch, tmp_path,
    ):
        _setup_ledger(monkeypatch, tmp_path)
        _write_session(tmp_path, "x", 10)
        from backend.core.ouroboros.governance.determinism.decisions_reader import (  # noqa: E501
            read_records_for_session,
        )
        result = read_records_for_session(
            "x", kind_filter="gate_pass",
        )
        # Only odd-indexed rows are gate_pass
        assert all(
            r["kind"] == "gate_pass" for r in result.records
        )
        assert len(result.records) == 5

    def test_read_records_missing_session_returns_diagnostic(
        self, monkeypatch, tmp_path,
    ):
        _setup_ledger(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.determinism.decisions_reader import (  # noqa: E501
            read_records_for_session,
        )
        result = read_records_for_session("does-not-exist")
        assert result.records == ()
        assert any(
            "not found" in d for d in result.diagnostics
        )

    def test_aggregate_kinds(self, monkeypatch, tmp_path):
        _setup_ledger(monkeypatch, tmp_path)
        _write_session(tmp_path, "x", 10)
        from backend.core.ouroboros.governance.determinism.decisions_reader import (  # noqa: E501
            aggregate_kinds_for_session,
        )
        agg = aggregate_kinds_for_session("x")
        kinds = {e.kind: e.count for e in agg}
        assert kinds == {
            "route_selection": 5,
            "gate_pass": 5,
        }

    def test_recent_records_across_sessions(
        self, monkeypatch, tmp_path,
    ):
        _setup_ledger(monkeypatch, tmp_path)
        _write_session(tmp_path, "alpha", 3)
        time.sleep(0.05)
        _write_session(tmp_path, "beta", 3)
        from backend.core.ouroboros.governance.determinism.decisions_reader import (  # noqa: E501
            recent_records_across_sessions,
        )
        recent = recent_records_across_sessions(limit=4)
        # Newest session first
        assert len(recent) == 4
        assert recent[0][0] == "beta"

    def test_garbage_lines_skipped(
        self, monkeypatch, tmp_path,
    ):
        _setup_ledger(monkeypatch, tmp_path)
        d = tmp_path / "broken"
        d.mkdir(parents=True)
        path = d / "decisions.jsonl"
        path.write_text(
            json.dumps({
                "record_id": "good",
                "session_id": "broken",
                "op_id": "o", "phase": "P",
                "kind": "route_selection",
                "ordinal": 0, "inputs_hash": "h",
                "output_repr": "{}",
                "monotonic_ts": 1.0, "wall_ts": 1.0,
                "schema_version": "decision_record.1",
            }) + "\n"
            + "bad json{\n"
            + "\n",
        )
        from backend.core.ouroboros.governance.determinism.decisions_reader import (  # noqa: E501
            read_records_for_session,
        )
        result = read_records_for_session("broken")
        assert len(result.records) == 1
        assert any(
            "skipped" in d for d in result.diagnostics
        )


# ---------------------------------------------------------------------------
# § 2 — HTTP route handlers
# ---------------------------------------------------------------------------


class TestObservabilityHandlers:
    @pytest.mark.asyncio
    async def test_overview_disabled_returns_503(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_DETERMINISM_LEDGER_ENABLED", "false",
        )
        from backend.core.ouroboros.governance.decisions_observability import (  # noqa: E501
            _DecisionsRoutesHandler,
        )
        h = _DecisionsRoutesHandler()
        resp = await h.handle_overview(_make_request())
        assert resp.status == 503

    @pytest.mark.asyncio
    async def test_overview_returns_sessions_and_recent(
        self, monkeypatch, tmp_path,
    ):
        _setup_ledger(monkeypatch, tmp_path)
        _write_session(tmp_path, "alpha", 3)
        time.sleep(0.05)
        _write_session(tmp_path, "beta", 3)
        from backend.core.ouroboros.governance.decisions_observability import (  # noqa: E501
            _DecisionsRoutesHandler,
        )
        h = _DecisionsRoutesHandler()
        resp = await h.handle_overview(_make_request())
        assert resp.status == 200
        body = json.loads(resp.body)
        assert len(body["sessions"]) == 2
        # Newest first
        assert body["sessions"][0]["session_id"] == "beta"
        assert body["recent_records"]
        assert "kind_histogram" in body
        assert "decision_kinds" in body
        # Decision-kind enum values exposed
        assert "route_selection" in body["decision_kinds"]
        assert (
            body["sse_event_type"] == "decision_drift_detected"
        )

    @pytest.mark.asyncio
    async def test_overview_respects_kind_filter(
        self, monkeypatch, tmp_path,
    ):
        _setup_ledger(monkeypatch, tmp_path)
        _write_session(tmp_path, "x", 10)
        from backend.core.ouroboros.governance.decisions_observability import (  # noqa: E501
            _DecisionsRoutesHandler,
        )
        h = _DecisionsRoutesHandler()
        resp = await h.handle_overview(
            _make_request(query={"kind": "gate_pass"}),
        )
        body = json.loads(resp.body)
        recent = body["recent_records"]
        assert len(recent) > 0
        for entry in recent:
            assert entry["record"]["kind"] == "gate_pass"

    @pytest.mark.asyncio
    async def test_session_detail_returns_records(
        self, monkeypatch, tmp_path,
    ):
        _setup_ledger(monkeypatch, tmp_path)
        _write_session(tmp_path, "x", 5)
        from backend.core.ouroboros.governance.decisions_observability import (  # noqa: E501
            _DecisionsRoutesHandler,
        )
        h = _DecisionsRoutesHandler()
        resp = await h.handle_session_detail(
            _make_request(match_info={"session_id": "x"}),
        )
        assert resp.status == 200
        body = json.loads(resp.body)
        assert body["session_id"] == "x"
        assert body["total_records_in_file"] == 5
        assert len(body["records"]) == 5

    @pytest.mark.asyncio
    async def test_session_detail_unknown_returns_404(
        self, monkeypatch, tmp_path,
    ):
        _setup_ledger(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.decisions_observability import (  # noqa: E501
            _DecisionsRoutesHandler,
        )
        h = _DecisionsRoutesHandler()
        resp = await h.handle_session_detail(
            _make_request(match_info={"session_id": "missing"}),
        )
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_session_detail_missing_id_returns_400(
        self, monkeypatch, tmp_path,
    ):
        _setup_ledger(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.decisions_observability import (  # noqa: E501
            _DecisionsRoutesHandler,
        )
        h = _DecisionsRoutesHandler()
        resp = await h.handle_session_detail(
            _make_request(match_info={"session_id": ""}),
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_rate_limit_returns_429(
        self, monkeypatch, tmp_path,
    ):
        _setup_ledger(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.decisions_observability import (  # noqa: E501
            _DecisionsRoutesHandler,
        )
        h = _DecisionsRoutesHandler(
            rate_limit_check=lambda req: False,
        )
        resp = await h.handle_overview(_make_request())
        assert resp.status == 429


# ---------------------------------------------------------------------------
# § 3 — register_routes
# ---------------------------------------------------------------------------


class TestRegisterRoutes:
    def test_register_routes_mounts_both(self):
        from aiohttp import web
        from backend.core.ouroboros.governance.decisions_observability import (  # noqa: E501
            register_routes,
        )
        app = web.Application()
        register_routes(app)
        paths = {
            r.resource.canonical for r in app.router.routes()
            if r.resource is not None
        }
        assert "/observability/decisions" in paths
        assert any(
            "/observability/decisions/session/" in p
            for p in paths
        )


# ---------------------------------------------------------------------------
# § 4 — REPL dispatcher
# ---------------------------------------------------------------------------


class TestDecisionsREPL:
    def test_help_works_when_disabled(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_DETERMINISM_LEDGER_ENABLED", "false",
        )
        from backend.core.ouroboros.governance.decisions_repl import (  # noqa: E501
            dispatch_decisions_command,
        )
        res = dispatch_decisions_command("/decisions help")
        assert res.ok is True
        assert "PRD §31.3" in res.text

    def test_disabled_returns_friendly_message(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_DETERMINISM_LEDGER_ENABLED", "false",
        )
        from backend.core.ouroboros.governance.decisions_repl import (  # noqa: E501
            dispatch_decisions_command,
        )
        res = dispatch_decisions_command("/decisions recent")
        assert res.ok is False
        assert "disabled" in res.text.lower()

    def test_recent_no_records(self, monkeypatch, tmp_path):
        _setup_ledger(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.decisions_repl import (  # noqa: E501
            dispatch_decisions_command,
        )
        res = dispatch_decisions_command("/decisions recent")
        assert res.ok is True
        assert "no records" in res.text.lower()

    def test_recent_with_data(self, monkeypatch, tmp_path):
        _setup_ledger(monkeypatch, tmp_path)
        _write_session(tmp_path, "x", 5)
        from backend.core.ouroboros.governance.decisions_repl import (  # noqa: E501
            dispatch_decisions_command,
        )
        res = dispatch_decisions_command(
            "/decisions recent 3",
        )
        assert res.ok is True
        assert "3 record(s)" in res.text

    def test_session_detail(self, monkeypatch, tmp_path):
        _setup_ledger(monkeypatch, tmp_path)
        _write_session(tmp_path, "alpha", 4)
        from backend.core.ouroboros.governance.decisions_repl import (  # noqa: E501
            dispatch_decisions_command,
        )
        res = dispatch_decisions_command(
            "/decisions session alpha",
        )
        assert res.ok is True
        assert "/decisions session alpha" in res.text

    def test_session_missing_arg(
        self, monkeypatch, tmp_path,
    ):
        _setup_ledger(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.decisions_repl import (  # noqa: E501
            dispatch_decisions_command,
        )
        res = dispatch_decisions_command("/decisions session")
        assert res.ok is False
        assert "missing session_id" in res.text

    def test_sessions_list(self, monkeypatch, tmp_path):
        _setup_ledger(monkeypatch, tmp_path)
        _write_session(tmp_path, "x", 2)
        from backend.core.ouroboros.governance.decisions_repl import (  # noqa: E501
            dispatch_decisions_command,
        )
        res = dispatch_decisions_command("/decisions sessions")
        assert res.ok is True
        assert "x" in res.text

    def test_kind_filter(self, monkeypatch, tmp_path):
        _setup_ledger(monkeypatch, tmp_path)
        _write_session(tmp_path, "x", 6)
        from backend.core.ouroboros.governance.decisions_repl import (  # noqa: E501
            dispatch_decisions_command,
        )
        res = dispatch_decisions_command(
            "/decisions kind gate_pass",
        )
        assert res.ok is True
        assert "gate_pass" in res.text

    def test_kind_missing_arg(
        self, monkeypatch, tmp_path,
    ):
        _setup_ledger(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.decisions_repl import (  # noqa: E501
            dispatch_decisions_command,
        )
        res = dispatch_decisions_command("/decisions kind")
        assert res.ok is False
        assert "missing kind" in res.text

    def test_count_per_session(
        self, monkeypatch, tmp_path,
    ):
        _setup_ledger(monkeypatch, tmp_path)
        _write_session(tmp_path, "x", 6)
        from backend.core.ouroboros.governance.decisions_repl import (  # noqa: E501
            dispatch_decisions_command,
        )
        res = dispatch_decisions_command(
            "/decisions count x",
        )
        assert res.ok is True
        assert "route_selection" in res.text
        assert "gate_pass" in res.text

    def test_count_aggregate(self, monkeypatch, tmp_path):
        _setup_ledger(monkeypatch, tmp_path)
        _write_session(tmp_path, "alpha", 4)
        _write_session(tmp_path, "beta", 6)
        from backend.core.ouroboros.governance.decisions_repl import (  # noqa: E501
            dispatch_decisions_command,
        )
        res = dispatch_decisions_command("/decisions count")
        assert res.ok is True
        # Aggregate across both = 10 records total
        assert "10" in res.text

    def test_unknown_subcommand(
        self, monkeypatch, tmp_path,
    ):
        _setup_ledger(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.decisions_repl import (  # noqa: E501
            dispatch_decisions_command,
        )
        res = dispatch_decisions_command("/decisions xyzzy")
        assert res.ok is False
        assert "unknown subcommand" in res.text

    def test_non_decisions_line_doesnt_match(self):
        from backend.core.ouroboros.governance.decisions_repl import (  # noqa: E501
            dispatch_decisions_command,
        )
        res = dispatch_decisions_command("/posture status")
        assert res.matched is False


# ---------------------------------------------------------------------------
# § 5 — register_verbs auto-discovery
# ---------------------------------------------------------------------------


class TestRegisterVerbs:
    def test_register_verbs_returns_one(self):
        from backend.core.ouroboros.governance.decisions_repl import (  # noqa: E501
            register_verbs,
        )
        registry = MagicMock()
        n = register_verbs(registry)
        assert n == 1
        registry.register.assert_called_once()
        spec = registry.register.call_args[0][0]
        assert spec.name == "/decisions"


# ---------------------------------------------------------------------------
# § 6 — Authority floor
# ---------------------------------------------------------------------------


class TestAuthorityFloor:
    _FORBIDDEN = (
        "from backend.core.ouroboros.governance.orchestrator",
        "from backend.core.ouroboros.governance.iron_gate",
        "from backend.core.ouroboros.governance.candidate_generator",
        "from backend.core.ouroboros.governance.providers",
        "from backend.core.ouroboros.governance.urgency_router",
        "from backend.core.ouroboros.governance.semantic_guardian",
        "from backend.core.ouroboros.governance.tool_executor",
        "from backend.core.ouroboros.governance.change_engine",
        "from backend.core.ouroboros.governance.subagent_scheduler",
        "from backend.core.ouroboros.governance.policy",
        "from backend.core.ouroboros.governance.auto_action_router",
        "from backend.core.ouroboros.governance.strategic_direction",
    )

    def _read(self, name: str) -> str:
        return (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / name
        ).read_text(encoding="utf-8")

    def test_observability_module_floor(self):
        source = self._read("decisions_observability.py")
        for forbidden in self._FORBIDDEN:
            assert forbidden not in source, forbidden

    def test_repl_module_floor(self):
        source = self._read("decisions_repl.py")
        for forbidden in self._FORBIDDEN:
            assert forbidden not in source, forbidden

    def test_reader_module_floor(self):
        source = self._read("determinism/decisions_reader.py")
        for forbidden in self._FORBIDDEN:
            assert forbidden not in source, forbidden


# ---------------------------------------------------------------------------
# § 7 — Read-only contract
# ---------------------------------------------------------------------------


class TestReadOnlyContract:
    """All three modules are read-only — no
    DecisionRuntime.record() / write() / mutation calls."""

    def _read(self, name: str) -> str:
        return (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / name
        ).read_text(encoding="utf-8")

    def test_observability_no_mutation_calls(self):
        source = self._read("decisions_observability.py")
        forbidden_calls = (
            "DecisionRuntime(",
            ".record(",
            "_persist_history",
            "decisions_path.write_text",
            "decisions_path.unlink",
        )
        for fcall in forbidden_calls:
            assert fcall not in source, (
                f"decisions_observability.py is read-only — "
                f"found mutation token {fcall}"
            )

    def test_repl_no_mutation_calls(self):
        source = self._read("decisions_repl.py")
        forbidden_calls = (
            "DecisionRuntime(",
            ".record(",
            "_persist_history",
            "decisions_path.write_text",
            "decisions_path.unlink",
        )
        for fcall in forbidden_calls:
            assert fcall not in source, (
                f"decisions_repl.py is read-only — found "
                f"mutation token {fcall}"
            )

    def test_reader_no_mutation_calls(self):
        source = self._read("determinism/decisions_reader.py")
        forbidden_calls = (
            "DecisionRuntime(",
            ".record(",
            "_persist_history",
            "decisions_path.write_text",
            "decisions_path.unlink",
        )
        for fcall in forbidden_calls:
            assert fcall not in source, (
                f"decisions_reader.py is read-only — found "
                f"mutation token {fcall}"
            )


# ---------------------------------------------------------------------------
# § 8 — Public exports
# ---------------------------------------------------------------------------


class TestPublicExports:
    def test_reader_exports(self):
        from backend.core.ouroboros.governance.determinism import (  # noqa: E501
            decisions_reader as r,
        )
        expected = sorted([
            "DECISIONS_READER_SCHEMA_VERSION",
            "DecisionsQueryResult",
            "KindAggregation",
            "SessionListEntry",
            "aggregate_kinds_for_session",
            "default_record_limit",
            "list_available_sessions",
            "max_records_per_session",
            "max_sessions_listed",
            "read_records_for_session",
            "recent_records_across_sessions",
        ])
        assert sorted(r.__all__) == expected

    def test_observability_exports(self):
        from backend.core.ouroboros.governance import (
            decisions_observability as obs,
        )
        assert sorted(obs.__all__) == ["register_routes"]

    def test_repl_exports(self):
        from backend.core.ouroboros.governance import (
            decisions_repl as r,
        )
        expected = sorted([
            "DecisionsReplDispatchResult",
            "dispatch_decisions_command",
            "register_verbs",
        ])
        assert sorted(r.__all__) == expected
