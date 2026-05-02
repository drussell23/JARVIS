"""Gap #2 Slice 4 — ide_policy_router regression suite.

Covers:

  §1   master flag + default-off
  §2   loopback-only construction
  §3   gate (master + rate limit)
  §4   POST /policy/confidence/proposals body validation
  §5   POST propose substrate-integration (INVALID / LOOSEN / no-op / APPLIED)
  §6   POST propose end-to-end (ledger persist + SSE publish)
  §7   POST approve / reject (decision dispatch + SSE)
  §8   GET /policy/confidence snapshot shape
  §9   AST authority pins
  §10  SSE event-vocabulary parity
"""
from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

aiohttp = pytest.importorskip("aiohttp")
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from backend.core.ouroboros.governance.adaptation.confidence_threshold_tightener import (
    install_surface_validator,
)
from backend.core.ouroboros.governance.adaptation.ledger import (
    AdaptationLedger,
    AdaptationSurface,
)
from backend.core.ouroboros.governance.ide_observability_stream import (
    EVENT_TYPE_CONFIDENCE_POLICY_APPROVED,
    EVENT_TYPE_CONFIDENCE_POLICY_PROPOSED,
    EVENT_TYPE_CONFIDENCE_POLICY_REJECTED,
    StreamEventBroker,
)
from backend.core.ouroboros.governance.ide_policy_router import (
    IDE_POLICY_ROUTER_SCHEMA_VERSION,
    IDEPolicyRouter,
    ide_policy_router_enabled,
)
from backend.core.ouroboros.governance.verification.confidence_policy import (
    ConfidencePolicy,
)


_MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "backend"
    / "core"
    / "ouroboros"
    / "governance"
    / "ide_policy_router.py"
)


def _baseline() -> ConfidencePolicy:
    return ConfidencePolicy(
        floor=0.05, window_k=16, approaching_factor=1.5, enforce=False,
    )


def _proposed_tighten() -> ConfidencePolicy:
    return ConfidencePolicy(
        floor=0.10, window_k=16, approaching_factor=1.5, enforce=False,
    )


def _make_request(
    path: str,
    *,
    method: str = "POST",
    headers: Optional[Dict[str, str]] = None,
    match_info: Optional[Dict[str, str]] = None,
    body: bytes = b"",
    remote: str = "127.0.0.1",
) -> web.Request:
    headers = headers or {}
    req = make_mocked_request(method, path, headers=headers, payload=None)
    if match_info:
        req.match_info.update(match_info)
    req._transport_peername = (remote, 0)  # type: ignore[attr-defined]

    # Patch request.read() to return the supplied body
    async def _read():
        return body
    req.read = _read  # type: ignore[assignment]
    return req


def _enable(monkeypatch):
    monkeypatch.setenv("JARVIS_IDE_POLICY_ROUTER_ENABLED", "true")
    monkeypatch.setenv("JARVIS_ADAPTATION_LEDGER_ENABLED", "true")


def _build_router(tmp_path):
    """Build a router with an isolated ledger + broker so tests
    don't cross-contaminate."""
    install_surface_validator()  # other tests may have reset
    ledger = AdaptationLedger(path=tmp_path / "adapt.jsonl")
    broker = StreamEventBroker()
    return IDEPolicyRouter(
        host="127.0.0.1", ledger=ledger, broker=broker,
    ), ledger, broker


# ============================================================================
# §1 — Master flag + default-off
# ============================================================================


class TestMasterFlag:
    def test_default_post_graduation_is_true(self, monkeypatch):
        """Slice 5 graduation (2026-05-02): structurally safe by
        construction (loopback + rate-limit + cage validator +
        bounded body)."""
        monkeypatch.delenv(
            "JARVIS_IDE_POLICY_ROUTER_ENABLED", raising=False,
        )
        assert ide_policy_router_enabled() is True

    def test_explicit_true(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_IDE_POLICY_ROUTER_ENABLED", "true",
        )
        assert ide_policy_router_enabled() is True

    def test_explicit_false_hot_revert(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_IDE_POLICY_ROUTER_ENABLED", "false",
        )
        assert ide_policy_router_enabled() is False

    def test_garbage_value_treated_as_false(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_IDE_POLICY_ROUTER_ENABLED", "maybe",
        )
        assert ide_policy_router_enabled() is False


# ============================================================================
# §2 — Loopback-only construction
# ============================================================================


class TestLoopbackBinding:
    def test_127_0_0_1_accepted(self):
        IDEPolicyRouter(host="127.0.0.1")  # no raise

    def test_localhost_accepted(self):
        IDEPolicyRouter(host="localhost")

    def test_ipv6_loopback_accepted(self):
        IDEPolicyRouter(host="::1")

    def test_zero_addr_rejected(self):
        with pytest.raises(ValueError):
            IDEPolicyRouter(host="0.0.0.0")

    def test_external_ip_rejected(self):
        with pytest.raises(ValueError):
            IDEPolicyRouter(host="10.0.0.1")

    def test_empty_string_rejected(self):
        with pytest.raises(ValueError):
            IDEPolicyRouter(host="")


# ============================================================================
# §3 — Gate (master + rate limit)
# ============================================================================


class TestGate:
    def test_disabled_returns_403(self, monkeypatch, tmp_path):
        # Hot-revert post-graduation: explicit ``=false`` reverts
        # the panel surface to deny-by-default.
        monkeypatch.setenv(
            "JARVIS_IDE_POLICY_ROUTER_ENABLED", "false",
        )
        router, _, _ = _build_router(tmp_path)
        req = _make_request("/policy/confidence")
        resp = asyncio.run(router._handle_snapshot(req))
        assert resp.status == 403
        body = json.loads(resp.body)
        assert body["reason_code"] == "ide_policy_router.disabled"

    def test_rate_limit_returns_429(self, monkeypatch, tmp_path):
        _enable(monkeypatch)
        # Lower the limit so we can exhaust it cheaply
        monkeypatch.setenv(
            "JARVIS_IDE_POLICY_ROUTER_RATE_LIMIT_PER_MIN", "2",
        )
        router, _, _ = _build_router(tmp_path)
        # Burn through the quota
        for _ in range(2):
            req = _make_request("/policy/confidence", method="GET")
            asyncio.run(router._handle_snapshot(req))
        # Third request must be 429
        req = _make_request("/policy/confidence", method="GET")
        resp = asyncio.run(router._handle_snapshot(req))
        assert resp.status == 429


# ============================================================================
# §4 — POST propose body validation
# ============================================================================


class TestProposeBodyValidation:
    @pytest.fixture(autouse=True)
    def _enable_master(self, monkeypatch):
        _enable(monkeypatch)

    def _post(self, router, body: dict):
        req = _make_request(
            "/policy/confidence/proposals",
            method="POST",
            body=json.dumps(body).encode("utf-8"),
        )
        return asyncio.run(router._handle_propose(req))

    def test_oversized_body_413(self, monkeypatch, tmp_path):
        monkeypatch.setenv(
            "JARVIS_IDE_POLICY_ROUTER_MAX_BODY_BYTES", "1024",
        )
        router, _, _ = _build_router(tmp_path)
        big = "x" * 2048
        req = _make_request(
            "/policy/confidence/proposals",
            method="POST",
            body=big.encode("utf-8"),
        )
        resp = asyncio.run(router._handle_propose(req))
        assert resp.status == 413

    def test_invalid_json_400(self, tmp_path):
        router, _, _ = _build_router(tmp_path)
        req = _make_request(
            "/policy/confidence/proposals",
            method="POST",
            body=b"not json{",
        )
        resp = asyncio.run(router._handle_propose(req))
        assert resp.status == 400
        body = json.loads(resp.body)
        assert body["reason_code"] == "ide_policy_router.invalid_json"

    def test_body_not_object_400(self, tmp_path):
        router, _, _ = _build_router(tmp_path)
        req = _make_request(
            "/policy/confidence/proposals",
            method="POST",
            body=json.dumps([]).encode("utf-8"),
        )
        resp = asyncio.run(router._handle_propose(req))
        assert resp.status == 400
        body = json.loads(resp.body)
        assert body["reason_code"] == (
            "ide_policy_router.body_not_object"
        )

    def test_missing_operator_400(self, tmp_path):
        router, _, _ = _build_router(tmp_path)
        resp = self._post(router, {
            "current": _baseline().to_dict(),
            "proposed": _proposed_tighten().to_dict(),
            "evidence_summary": "x → y",
            "observation_count": 5,
        })
        assert resp.status == 400
        body = json.loads(resp.body)
        assert body["reason_code"] == (
            "ide_policy_router.operator_required"
        )

    def test_missing_evidence_summary_400(self, tmp_path):
        router, _, _ = _build_router(tmp_path)
        resp = self._post(router, {
            "current": _baseline().to_dict(),
            "proposed": _proposed_tighten().to_dict(),
            "operator": "alice",
            "observation_count": 5,
        })
        assert resp.status == 400
        body = json.loads(resp.body)
        assert body["reason_code"] == (
            "ide_policy_router.evidence_summary_required"
        )

    def test_observation_count_below_one_400(self, tmp_path):
        router, _, _ = _build_router(tmp_path)
        resp = self._post(router, {
            "current": _baseline().to_dict(),
            "proposed": _proposed_tighten().to_dict(),
            "operator": "alice",
            "evidence_summary": "x → y",
            "observation_count": 0,
        })
        assert resp.status == 400
        body = json.loads(resp.body)
        assert body["reason_code"] == (
            "ide_policy_router.observation_count_below_one"
        )

    def test_current_not_object_400(self, tmp_path):
        router, _, _ = _build_router(tmp_path)
        resp = self._post(router, {
            "current": "not an object",
            "proposed": _proposed_tighten().to_dict(),
            "operator": "alice",
            "evidence_summary": "x → y",
            "observation_count": 5,
        })
        assert resp.status == 400


# ============================================================================
# §5 — POST propose substrate integration
# ============================================================================


class TestProposeSubstrate:
    @pytest.fixture(autouse=True)
    def _enable_master(self, monkeypatch):
        _enable(monkeypatch)

    def _post(self, router, current, proposed):
        req = _make_request(
            "/policy/confidence/proposals",
            method="POST",
            body=json.dumps({
                "current": current.to_dict(),
                "proposed": proposed.to_dict(),
                "operator": "alice",
                "evidence_summary": "knob 1 → knob 2",
                "observation_count": 5,
            }).encode("utf-8"),
        )
        return asyncio.run(router._handle_propose(req))

    def test_invalid_policy_400(self, tmp_path):
        router, _, _ = _build_router(tmp_path)
        bad = ConfidencePolicy(
            floor=2.0, window_k=16,
            approaching_factor=1.5, enforce=False,
        )
        resp = self._post(router, _baseline(), bad)
        assert resp.status == 400
        body = json.loads(resp.body)
        assert body["reason_code"] == "ide_policy_router.policy_invalid"

    def test_loosen_proposal_400(self, tmp_path):
        router, _, _ = _build_router(tmp_path)
        # Loosen: floor 0.10 → 0.05
        current = ConfidencePolicy(
            floor=0.10, window_k=16,
            approaching_factor=1.5, enforce=False,
        )
        resp = self._post(router, current, _baseline())
        assert resp.status == 400
        body = json.loads(resp.body)
        assert body["reason_code"] == (
            "ide_policy_router.policy_would_loosen"
        )

    def test_no_op_proposal_400(self, tmp_path):
        router, _, _ = _build_router(tmp_path)
        resp = self._post(router, _baseline(), _baseline())
        assert resp.status == 400
        body = json.loads(resp.body)
        assert body["reason_code"] == (
            "ide_policy_router.no_op_proposal_rejected"
        )

    def test_applied_returns_201(self, tmp_path):
        router, _, _ = _build_router(tmp_path)
        resp = self._post(router, _baseline(), _proposed_tighten())
        assert resp.status == 201
        body = json.loads(resp.body)
        assert body["ok"] is True
        assert body["kind"] == "raise_floor"
        assert "raise_floor" in body["moved_dimensions"]


# ============================================================================
# §6 — POST propose end-to-end (ledger + SSE)
# ============================================================================


class TestProposeEndToEnd:
    @pytest.fixture(autouse=True)
    def _enable_master(self, monkeypatch):
        _enable(monkeypatch)

    def test_proposal_persists_to_ledger(self, tmp_path):
        router, ledger, broker = _build_router(tmp_path)
        body = json.dumps({
            "current": _baseline().to_dict(),
            "proposed": _proposed_tighten().to_dict(),
            "operator": "alice",
            "evidence_summary": "floor 0.05 → 0.10 (5 events)",
            "observation_count": 5,
        }).encode("utf-8")
        req = _make_request(
            "/policy/confidence/proposals",
            method="POST", body=body,
        )
        resp = asyncio.run(router._handle_propose(req))
        assert resp.status == 201
        # Ledger should now have one pending proposal on our surface
        pending = ledger.list_pending()
        assert any(
            p.surface is AdaptationSurface.CONFIDENCE_MONITOR_THRESHOLDS
            for p in pending
        )

    def test_sse_proposed_event_published(self, tmp_path):
        router, _, broker = _build_router(tmp_path)
        body = json.dumps({
            "current": _baseline().to_dict(),
            "proposed": _proposed_tighten().to_dict(),
            "operator": "alice",
            "evidence_summary": "floor 0.05 → 0.10 (5 events)",
            "observation_count": 5,
        }).encode("utf-8")
        req = _make_request(
            "/policy/confidence/proposals",
            method="POST", body=body,
        )
        resp = asyncio.run(router._handle_propose(req))
        assert resp.status == 201
        history = broker._history  # internal access — test only
        types = [e.event_type for e in history]
        assert EVENT_TYPE_CONFIDENCE_POLICY_PROPOSED in types

    def test_caller_supplied_proposal_id_used(self, tmp_path):
        router, ledger, _ = _build_router(tmp_path)
        body = json.dumps({
            "current": _baseline().to_dict(),
            "proposed": _proposed_tighten().to_dict(),
            "operator": "alice",
            "evidence_summary": "floor 0.05 → 0.10",
            "observation_count": 5,
            "proposal_id": "conf-explicit-1",
        }).encode("utf-8")
        req = _make_request(
            "/policy/confidence/proposals",
            method="POST", body=body,
        )
        resp = asyncio.run(router._handle_propose(req))
        body_resp = json.loads(resp.body)
        assert body_resp["proposal_id"] == "conf-explicit-1"

    def test_malformed_proposal_id_rejected(self, tmp_path):
        router, _, _ = _build_router(tmp_path)
        body = json.dumps({
            "current": _baseline().to_dict(),
            "proposed": _proposed_tighten().to_dict(),
            "operator": "alice",
            "evidence_summary": "x → y",
            "observation_count": 5,
            "proposal_id": "has spaces and !@#",
        }).encode("utf-8")
        req = _make_request(
            "/policy/confidence/proposals",
            method="POST", body=body,
        )
        resp = asyncio.run(router._handle_propose(req))
        assert resp.status == 400


# ============================================================================
# §7 — POST approve / reject
# ============================================================================


class TestApproveReject:
    @pytest.fixture(autouse=True)
    def _enable_master(self, monkeypatch):
        _enable(monkeypatch)

    def _propose(self, router):
        body = json.dumps({
            "current": _baseline().to_dict(),
            "proposed": _proposed_tighten().to_dict(),
            "operator": "alice",
            "evidence_summary": "floor 0.05 → 0.10",
            "observation_count": 5,
        }).encode("utf-8")
        req = _make_request(
            "/policy/confidence/proposals",
            method="POST", body=body,
        )
        resp = asyncio.run(router._handle_propose(req))
        return json.loads(resp.body)["proposal_id"]

    def test_approve_returns_200(self, tmp_path):
        router, _, _ = _build_router(tmp_path)
        pid = self._propose(router)
        req = _make_request(
            f"/policy/confidence/proposals/{pid}/approve",
            method="POST",
            match_info={"proposal_id": pid},
            body=json.dumps({"operator": "alice"}).encode("utf-8"),
        )
        resp = asyncio.run(router._handle_approve(req))
        assert resp.status == 200
        body = json.loads(resp.body)
        assert body["operator_decision"] == "approved"

    def test_reject_returns_200(self, tmp_path):
        router, _, _ = _build_router(tmp_path)
        pid = self._propose(router)
        req = _make_request(
            f"/policy/confidence/proposals/{pid}/reject",
            method="POST",
            match_info={"proposal_id": pid},
            body=json.dumps({
                "operator": "alice", "reason": "too aggressive",
            }).encode("utf-8"),
        )
        resp = asyncio.run(router._handle_reject(req))
        assert resp.status == 200
        body = json.loads(resp.body)
        assert body["operator_decision"] == "rejected"

    def test_approve_unknown_proposal_404(self, tmp_path):
        router, _, _ = _build_router(tmp_path)
        req = _make_request(
            "/policy/confidence/proposals/conf-nope/approve",
            method="POST",
            match_info={"proposal_id": "conf-nope"},
            body=json.dumps({"operator": "alice"}).encode("utf-8"),
        )
        resp = asyncio.run(router._handle_approve(req))
        assert resp.status == 404

    def test_approve_missing_operator_400(self, tmp_path):
        router, _, _ = _build_router(tmp_path)
        pid = self._propose(router)
        req = _make_request(
            f"/policy/confidence/proposals/{pid}/approve",
            method="POST",
            match_info={"proposal_id": pid},
            body=json.dumps({}).encode("utf-8"),
        )
        resp = asyncio.run(router._handle_approve(req))
        assert resp.status == 400

    def test_approve_malformed_proposal_id_400(self, tmp_path):
        router, _, _ = _build_router(tmp_path)
        req = _make_request(
            "/policy/confidence/proposals/spaces here/approve",
            method="POST",
            match_info={"proposal_id": "spaces here"},
            body=json.dumps({"operator": "alice"}).encode("utf-8"),
        )
        resp = asyncio.run(router._handle_approve(req))
        assert resp.status == 400

    def test_double_approve_409(self, tmp_path):
        router, _, _ = _build_router(tmp_path)
        pid = self._propose(router)
        # First approve succeeds
        req1 = _make_request(
            f"/policy/confidence/proposals/{pid}/approve",
            method="POST",
            match_info={"proposal_id": pid},
            body=json.dumps({"operator": "alice"}).encode("utf-8"),
        )
        asyncio.run(router._handle_approve(req1))
        # Second approve must 409 (NOT_PENDING)
        req2 = _make_request(
            f"/policy/confidence/proposals/{pid}/approve",
            method="POST",
            match_info={"proposal_id": pid},
            body=json.dumps({"operator": "alice"}).encode("utf-8"),
        )
        resp = asyncio.run(router._handle_approve(req2))
        assert resp.status == 409

    def test_sse_approved_event_published(self, tmp_path):
        router, _, broker = _build_router(tmp_path)
        pid = self._propose(router)
        req = _make_request(
            f"/policy/confidence/proposals/{pid}/approve",
            method="POST",
            match_info={"proposal_id": pid},
            body=json.dumps({"operator": "alice"}).encode("utf-8"),
        )
        asyncio.run(router._handle_approve(req))
        types = [e.event_type for e in broker._history]
        assert EVENT_TYPE_CONFIDENCE_POLICY_APPROVED in types

    def test_sse_rejected_event_published(self, tmp_path):
        router, _, broker = _build_router(tmp_path)
        pid = self._propose(router)
        req = _make_request(
            f"/policy/confidence/proposals/{pid}/reject",
            method="POST",
            match_info={"proposal_id": pid},
            body=json.dumps({"operator": "alice"}).encode("utf-8"),
        )
        asyncio.run(router._handle_reject(req))
        types = [e.event_type for e in broker._history]
        assert EVENT_TYPE_CONFIDENCE_POLICY_REJECTED in types


# ============================================================================
# §8 — GET /policy/confidence snapshot
# ============================================================================


class TestSnapshot:
    @pytest.fixture(autouse=True)
    def _enable_master(self, monkeypatch):
        _enable(monkeypatch)

    def test_snapshot_returns_200_and_shape(self, tmp_path):
        router, _, _ = _build_router(tmp_path)
        req = _make_request("/policy/confidence", method="GET")
        resp = asyncio.run(router._handle_snapshot(req))
        assert resp.status == 200
        body = json.loads(resp.body)
        assert body["schema_version"] == (
            IDE_POLICY_ROUTER_SCHEMA_VERSION
        )
        assert "current_effective" in body
        assert "adapted" in body
        assert "proposals" in body

        for k in (
            "floor", "window_k", "approaching_factor", "enforce",
        ):
            assert k in body["current_effective"]
        for k in ("loader_enabled", "in_effect", "values"):
            assert k in body["adapted"]
        for k in ("pending", "approved", "rejected", "items"):
            assert k in body["proposals"]

    def test_snapshot_includes_pending_proposal(self, tmp_path):
        router, _, _ = _build_router(tmp_path)
        # Submit a proposal first
        body = json.dumps({
            "current": _baseline().to_dict(),
            "proposed": _proposed_tighten().to_dict(),
            "operator": "alice",
            "evidence_summary": "floor 0.05 → 0.10",
            "observation_count": 5,
        }).encode("utf-8")
        req = _make_request(
            "/policy/confidence/proposals",
            method="POST", body=body,
        )
        asyncio.run(router._handle_propose(req))
        # Snapshot should reflect 1 pending
        req2 = _make_request("/policy/confidence", method="GET")
        resp = asyncio.run(router._handle_snapshot(req2))
        body = json.loads(resp.body)
        assert body["proposals"]["pending"] >= 1
        assert len(body["proposals"]["items"]) >= 1


# ============================================================================
# §9 — AST authority pins
# ============================================================================


_FORBIDDEN_AUTH_TOKENS = (
    "orchestrator", "iron_gate", "policy_engine",
    "risk_engine", "change_engine", "tool_executor",
    "providers", "candidate_generator", "semantic_guardian",
    "semantic_firewall", "scoped_tool_backend",
    "subagent_scheduler",
)
_SUBPROC_TOKENS = (
    "subprocess" + ".",
    "os." + "system",
    "popen",
)
_FS_TOKENS = (
    ".write_text(", ".write_bytes(", "shutil.",
    "os.remove", "os.unlink", "os.rmdir",
)
_ENV_MUTATION_TOKENS = (
    "os.environ[", "os.environ.pop", "os.environ.update",
    "os.put" + "env", "os.set" + "env",
)


class TestAuthorityInvariants:
    @pytest.fixture(scope="class")
    def source(self):
        return _MODULE_PATH.read_text(encoding="utf-8")

    def test_no_authority_imports(self, source):
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for f in _FORBIDDEN_AUTH_TOKENS:
                    assert f not in module, (
                        f"forbidden import contains {f!r}: "
                        f"{module}"
                    )

    def test_governance_imports_in_allowlist(self, source):
        allowed = {
            "backend.core.ouroboros.governance.adaptation.ledger",
            "backend.core.ouroboros.governance.adaptation.confidence_threshold_tightener",
            "backend.core.ouroboros.governance.adaptation.adapted_confidence_loader",
            "backend.core.ouroboros.governance.adaptation.yaml_writer",  # Slice 5 cage close
            "backend.core.ouroboros.governance.verification.confidence_policy",
            "backend.core.ouroboros.governance.verification.confidence_monitor",
            "backend.core.ouroboros.governance.ide_observability_stream",
            "backend.core.ouroboros.governance.ide_observability",
        }
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if "governance" in module:
                    assert module in allowed, (
                        f"governance import outside allowlist: "
                        f"{module}"
                    )

    def test_no_filesystem_writes(self, source):
        for tok in _FS_TOKENS:
            assert tok not in source, (
                f"forbidden FS-write token: {tok}"
            )

    def test_no_eval_exec_compile(self, source):
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(
                node.func, ast.Name,
            ):
                assert node.func.id not in (
                    "eval", "exec", "compile",
                ), f"forbidden bare call: {node.func.id}"

    def test_no_subprocess(self, source):
        for token in _SUBPROC_TOKENS:
            assert token not in source, (
                f"forbidden subprocess token: {token}"
            )

    def test_no_env_mutation(self, source):
        for token in _ENV_MUTATION_TOKENS:
            assert token not in source, (
                f"forbidden env mutation token: {token}"
            )

    def test_loopback_assertion_called(self, source):
        # Defensive: ensure the loopback check is wired in __init__
        assert "assert_loopback_only" in source, (
            "loopback assertion symbol missing — refusing to ship"
        )


# ============================================================================
# §10 — SSE event vocabulary parity
# ============================================================================


class TestSSEEventVocabulary:
    def test_proposed_event_in_valid_set(self):
        from backend.core.ouroboros.governance.ide_observability_stream import (
            _VALID_EVENT_TYPES,
        )
        assert (
            EVENT_TYPE_CONFIDENCE_POLICY_PROPOSED
            in _VALID_EVENT_TYPES
        )

    def test_approved_event_in_valid_set(self):
        from backend.core.ouroboros.governance.ide_observability_stream import (
            _VALID_EVENT_TYPES,
        )
        assert (
            EVENT_TYPE_CONFIDENCE_POLICY_APPROVED
            in _VALID_EVENT_TYPES
        )

    def test_rejected_event_in_valid_set(self):
        from backend.core.ouroboros.governance.ide_observability_stream import (
            _VALID_EVENT_TYPES,
        )
        assert (
            EVENT_TYPE_CONFIDENCE_POLICY_REJECTED
            in _VALID_EVENT_TYPES
        )

    def test_applied_event_in_valid_set(self):
        from backend.core.ouroboros.governance.ide_observability_stream import (
            _VALID_EVENT_TYPES,
            EVENT_TYPE_CONFIDENCE_POLICY_APPLIED,
        )
        assert (
            EVENT_TYPE_CONFIDENCE_POLICY_APPLIED
            in _VALID_EVENT_TYPES
        )
