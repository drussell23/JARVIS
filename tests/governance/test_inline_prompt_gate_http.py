"""InlinePromptGate Slice 3 — HTTP POST response surface tests.

Covers:
  * Master flag gating (write surface specifically — distinct from
    Slice 1 producer flag)
  * GET /observability/inline_prompt — list pending phase-boundary
    prompts (filtered)
  * GET /observability/inline_prompt/{prompt_id} — single detail
  * POST /observability/inline_prompt/{prompt_id}/respond — body
    validation, all 3 verdicts, race-with-controller-timeout, etc.
  * Rate limiting (per-IP sliding window)
  * Body-size cap
  * Phase-boundary filter — per-tool-call prompts are NOT exposed
    via this surface (404 not_phase_boundary) even on the same
    singleton controller
  * Schema version stamped on every response
  * CORS allowlist
  * Closed verdict vocabulary (ACCEPTED_VERDICTS)
  * Authority allowlist (no orchestrator-tier imports)
  * Idempotent state-error path (409 + current snapshot)
"""
from __future__ import annotations

import ast
import asyncio
import json
import pathlib
import uuid
from typing import Any, Dict, Optional
from unittest.mock import Mock

import pytest
from aiohttp import streams, web
from aiohttp.test_utils import make_mocked_request

from backend.core.ouroboros.governance.inline_permission import (
    InlineDecision,
    InlineGateVerdict,
    RoutePosture,
    UpstreamPolicy,
)
from backend.core.ouroboros.governance.inline_permission_prompt import (
    InlinePromptController,
    InlinePromptRequest,
)
from backend.core.ouroboros.governance.inline_prompt_gate import (
    PhaseInlinePromptRequest,
)
from backend.core.ouroboros.governance.inline_prompt_gate_http import (
    ACCEPTED_VERDICTS,
    INLINE_PROMPT_GATE_HTTP_SCHEMA_VERSION,
    InlinePromptGateHTTPRouter,
    inline_prompt_gate_http_enabled,
)
from backend.core.ouroboros.governance.inline_prompt_gate_runner import (
    PHASE_BOUNDARY_RULE_ID,
    PHASE_BOUNDARY_TOOL_SENTINEL,
    bridge_to_controller_request,
)


# ---------------------------------------------------------------------------
# Shared enable fixture — every test enables both flags by default
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _enable_both_flags(monkeypatch):
    monkeypatch.setenv("JARVIS_INLINE_PROMPT_GATE_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_INLINE_PROMPT_GATE_HTTP_ENABLED", "true",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(
    path: str,
    *,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    match_info: Optional[Dict[str, str]] = None,
    body: Optional[Dict[str, Any]] = None,
    body_bytes: Optional[bytes] = None,
    remote: str = "127.0.0.1",
) -> web.Request:
    """Build a minimal aiohttp Request for handler testing."""
    headers = headers or {}
    if body is not None:
        body_bytes = json.dumps(body).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    payload = None
    if body_bytes is not None:
        loop = asyncio.get_event_loop()
        # StreamReader requires a BaseProtocol with _reading_paused.
        # A Mock with the attribute satisfies the contract for tests.
        mock_protocol = Mock()
        mock_protocol._reading_paused = False
        payload = streams.StreamReader(
            protocol=mock_protocol, limit=2**16, loop=loop,
        )
        payload.feed_data(body_bytes)
        payload.feed_eof()
    if payload is not None:
        req = make_mocked_request(
            method, path, headers=headers, payload=payload,
        )
    else:
        req = make_mocked_request(method, path, headers=headers)
    if match_info:
        req.match_info.update(match_info)
    req._transport_peername = (remote, 0)  # type: ignore[attr-defined]
    return req


def _phase_boundary_request(
    *,
    prompt_id: Optional[str] = None,
    op_id: str = "op-test",
) -> PhaseInlinePromptRequest:
    return PhaseInlinePromptRequest(
        prompt_id=prompt_id or ("ipg-" + uuid.uuid4().hex[:24]),
        op_id=op_id,
        phase_at_request="GATE",
        risk_tier="NOTIFY_APPLY",
        change_summary="phase boundary test prompt",
        change_fingerprint="a" * 64,
        target_paths=("backend/foo.py",),
    )


def _per_tool_call_request(
    *,
    prompt_id: str,
    op_id: str = "op-tool",
) -> InlinePromptRequest:
    """Direct-construct a per-tool-call request (NOT phase boundary)
    to verify the HTTP surface filters it out."""
    return InlinePromptRequest(
        prompt_id=prompt_id,
        op_id=op_id,
        call_id="call-tool-1",
        tool="edit_file",
        arg_fingerprint="x" * 32,
        arg_preview="edit foo.py: change line 1",
        target_path="backend/foo.py",
        verdict=InlineGateVerdict(
            decision=InlineDecision.ASK,
            rule_id="some_real_tool_rule",
            reason="real tool call",
        ),
        rationale="model rationale",
        route=RoutePosture.INTERACTIVE,
        upstream_decision=UpstreamPolicy.NO_MATCH,
    )


def _register_phase_boundary_prompt(
    controller: InlinePromptController,
    *,
    prompt_id: Optional[str] = None,
) -> str:
    """Register a phase-boundary prompt directly on the controller
    (bypasses the async runner — we want the prompt sitting pending
    for the HTTP handler to act on)."""
    req = _phase_boundary_request(prompt_id=prompt_id)
    bridged = bridge_to_controller_request(req)
    controller.request(bridged, timeout_s=30.0)
    return req.prompt_id


# ---------------------------------------------------------------------------
# Master flag — write surface gate
# ---------------------------------------------------------------------------


class TestMasterFlagGate:
    def test_default_pre_graduation_is_false(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_INLINE_PROMPT_GATE_HTTP_ENABLED", raising=False,
        )
        assert inline_prompt_gate_http_enabled() is False

    def test_explicit_true_enables(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_INLINE_PROMPT_GATE_HTTP_ENABLED", "true",
        )
        assert inline_prompt_gate_http_enabled() is True

    def test_empty_string_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_INLINE_PROMPT_GATE_HTTP_ENABLED", "",
        )
        assert inline_prompt_gate_http_enabled() is False

    def test_falsy_values_disable(self, monkeypatch):
        for v in ("0", "false", "no", "off"):
            monkeypatch.setenv(
                "JARVIS_INLINE_PROMPT_GATE_HTTP_ENABLED", v,
            )
            assert inline_prompt_gate_http_enabled() is False

    @pytest.mark.asyncio
    async def test_disabled_returns_403_on_list(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_INLINE_PROMPT_GATE_HTTP_ENABLED", "false",
        )
        controller = InlinePromptController(default_timeout_s=30.0)
        router = InlinePromptGateHTTPRouter(controller=controller)
        req = _make_request("/observability/inline_prompt")
        resp = await router._handle_list(req)
        assert resp.status == 403
        body = json.loads(resp.body.decode("utf-8"))
        assert body["reason_code"] == "inline_prompt_gate.http_disabled"
        assert body["schema_version"] == (
            INLINE_PROMPT_GATE_HTTP_SCHEMA_VERSION
        )

    @pytest.mark.asyncio
    async def test_disabled_returns_403_on_respond(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_INLINE_PROMPT_GATE_HTTP_ENABLED", "false",
        )
        controller = InlinePromptController(default_timeout_s=30.0)
        router = InlinePromptGateHTTPRouter(controller=controller)
        req = _make_request(
            "/observability/inline_prompt/x/respond",
            method="POST",
            match_info={"prompt_id": "x"},
            body={"verdict": "allow"},
        )
        resp = await router._handle_respond(req)
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_producer_disabled_returns_403_on_respond(
        self, monkeypatch,
    ):
        """Even if the HTTP surface is enabled, if the producer
        master flag is off, responding has no meaning — symmetric
        gate."""
        monkeypatch.setenv("JARVIS_INLINE_PROMPT_GATE_ENABLED", "false")
        monkeypatch.setenv(
            "JARVIS_INLINE_PROMPT_GATE_HTTP_ENABLED", "true",
        )
        controller = InlinePromptController(default_timeout_s=30.0)
        router = InlinePromptGateHTTPRouter(controller=controller)
        req = _make_request(
            "/observability/inline_prompt/x/respond",
            method="POST",
            match_info={"prompt_id": "x"},
            body={"verdict": "allow"},
        )
        resp = await router._handle_respond(req)
        assert resp.status == 403
        body = json.loads(resp.body.decode("utf-8"))
        assert body["reason_code"] == (
            "inline_prompt_gate.producer_disabled"
        )


# ---------------------------------------------------------------------------
# GET /observability/inline_prompt — list
# ---------------------------------------------------------------------------


class TestListPhaseBoundaryPrompts:
    @pytest.mark.asyncio
    async def test_empty_controller_returns_empty_list(self):
        controller = InlinePromptController(default_timeout_s=30.0)
        router = InlinePromptGateHTTPRouter(controller=controller)
        req = _make_request("/observability/inline_prompt")
        resp = await router._handle_list(req)
        assert resp.status == 200
        body = json.loads(resp.body.decode("utf-8"))
        assert body["prompts"] == []
        assert body["count"] == 0

    @pytest.mark.asyncio
    async def test_list_includes_phase_boundary_prompts(self):
        controller = InlinePromptController(default_timeout_s=30.0)
        pid_1 = _register_phase_boundary_prompt(
            controller, prompt_id="ipg-pb-a",
        )
        pid_2 = _register_phase_boundary_prompt(
            controller, prompt_id="ipg-pb-b",
        )
        router = InlinePromptGateHTTPRouter(controller=controller)
        req = _make_request("/observability/inline_prompt")
        resp = await router._handle_list(req)
        assert resp.status == 200
        body = json.loads(resp.body.decode("utf-8"))
        prompt_ids = {p["prompt_id"] for p in body["prompts"]}
        assert pid_1 in prompt_ids
        assert pid_2 in prompt_ids
        assert body["count"] == 2

    @pytest.mark.asyncio
    async def test_list_excludes_per_tool_call_prompts(self):
        """The HTTP surface MUST NOT expose per-tool-call prompts
        even though they share the controller singleton."""
        controller = InlinePromptController(default_timeout_s=30.0)
        # Register one phase-boundary + one per-tool-call.
        pb_id = _register_phase_boundary_prompt(
            controller, prompt_id="ipg-pb",
        )
        tool_req = _per_tool_call_request(prompt_id="tool-call-1")
        controller.request(tool_req, timeout_s=30.0)

        router = InlinePromptGateHTTPRouter(controller=controller)
        req = _make_request("/observability/inline_prompt")
        resp = await router._handle_list(req)
        body = json.loads(resp.body.decode("utf-8"))
        prompt_ids = {p["prompt_id"] for p in body["prompts"]}
        assert pb_id in prompt_ids
        assert "tool-call-1" not in prompt_ids
        assert body["count"] == 1


# ---------------------------------------------------------------------------
# GET /observability/inline_prompt/{prompt_id} — detail
# ---------------------------------------------------------------------------


class TestDetailPhaseBoundaryPrompt:
    @pytest.mark.asyncio
    async def test_detail_returns_projection(self):
        controller = InlinePromptController(default_timeout_s=30.0)
        pid = _register_phase_boundary_prompt(
            controller, prompt_id="ipg-detail",
        )
        router = InlinePromptGateHTTPRouter(controller=controller)
        req = _make_request(
            f"/observability/inline_prompt/{pid}",
            match_info={"prompt_id": pid},
        )
        resp = await router._handle_detail(req)
        assert resp.status == 200
        body = json.loads(resp.body.decode("utf-8"))
        assert body["prompt"]["prompt_id"] == pid
        assert body["prompt"]["state"] == "pending"

    @pytest.mark.asyncio
    async def test_detail_unknown_prompt_404(self):
        controller = InlinePromptController(default_timeout_s=30.0)
        router = InlinePromptGateHTTPRouter(controller=controller)
        req = _make_request(
            "/observability/inline_prompt/not-real",
            match_info={"prompt_id": "not-real"},
        )
        resp = await router._handle_detail(req)
        assert resp.status == 404
        body = json.loads(resp.body.decode("utf-8"))
        assert body["reason_code"] == "inline_prompt_gate.unknown_prompt"

    @pytest.mark.asyncio
    async def test_detail_per_tool_prompt_returns_404(self):
        """Per-tool-call prompts MUST 404 on the phase-boundary
        detail endpoint even when the prompt_id matches an existing
        per-tool prompt on the singleton controller."""
        controller = InlinePromptController(default_timeout_s=30.0)
        tool_req = _per_tool_call_request(prompt_id="tool-call-1")
        controller.request(tool_req, timeout_s=30.0)
        router = InlinePromptGateHTTPRouter(controller=controller)
        req = _make_request(
            "/observability/inline_prompt/tool-call-1",
            match_info={"prompt_id": "tool-call-1"},
        )
        resp = await router._handle_detail(req)
        assert resp.status == 404
        body = json.loads(resp.body.decode("utf-8"))
        assert body["reason_code"] == "inline_prompt_gate.unknown_prompt"

    @pytest.mark.asyncio
    async def test_detail_invalid_prompt_id_format_400(self):
        controller = InlinePromptController(default_timeout_s=30.0)
        router = InlinePromptGateHTTPRouter(controller=controller)
        req = _make_request(
            "/observability/inline_prompt/has spaces",
            match_info={"prompt_id": "has spaces"},
        )
        resp = await router._handle_detail(req)
        assert resp.status == 400
        body = json.loads(resp.body.decode("utf-8"))
        assert body["reason_code"] == (
            "inline_prompt_gate.invalid_prompt_id"
        )


# ---------------------------------------------------------------------------
# POST /observability/inline_prompt/{prompt_id}/respond
# ---------------------------------------------------------------------------


class TestRespondHandler:
    @pytest.mark.asyncio
    async def test_allow_resolves_controller_future(self):
        controller = InlinePromptController(default_timeout_s=30.0)
        pid = _register_phase_boundary_prompt(
            controller, prompt_id="ipg-allow",
        )
        router = InlinePromptGateHTTPRouter(controller=controller)
        req = _make_request(
            f"/observability/inline_prompt/{pid}/respond",
            method="POST",
            match_info={"prompt_id": pid},
            body={
                "verdict": "allow",
                "reviewer": "ide-vscode",
                "reason": "looks safe",
            },
        )
        resp = await router._handle_respond(req)
        assert resp.status == 200
        body = json.loads(resp.body.decode("utf-8"))
        assert body["prompt_id"] == pid
        assert body["outcome"]["state"] == "allowed"
        assert body["outcome"]["response"] == "allow_once"
        assert body["outcome"]["reviewer"] == "ide-vscode"
        assert body["outcome"]["operator_reason"] == "looks safe"

    @pytest.mark.asyncio
    async def test_deny_resolves_controller_future(self):
        controller = InlinePromptController(default_timeout_s=30.0)
        pid = _register_phase_boundary_prompt(
            controller, prompt_id="ipg-deny",
        )
        router = InlinePromptGateHTTPRouter(controller=controller)
        req = _make_request(
            f"/observability/inline_prompt/{pid}/respond",
            method="POST",
            match_info={"prompt_id": pid},
            body={"verdict": "deny", "reviewer": "ide"},
        )
        resp = await router._handle_respond(req)
        assert resp.status == 200
        body = json.loads(resp.body.decode("utf-8"))
        assert body["outcome"]["state"] == "denied"

    @pytest.mark.asyncio
    async def test_pause_resolves_controller_future(self):
        controller = InlinePromptController(default_timeout_s=30.0)
        pid = _register_phase_boundary_prompt(
            controller, prompt_id="ipg-pause",
        )
        router = InlinePromptGateHTTPRouter(controller=controller)
        req = _make_request(
            f"/observability/inline_prompt/{pid}/respond",
            method="POST",
            match_info={"prompt_id": pid},
            body={"verdict": "pause"},
        )
        resp = await router._handle_respond(req)
        assert resp.status == 200
        body = json.loads(resp.body.decode("utf-8"))
        assert body["outcome"]["state"] == "paused"

    @pytest.mark.asyncio
    async def test_allow_always_resolves_with_remember_response(self):
        controller = InlinePromptController(default_timeout_s=30.0)
        pid = _register_phase_boundary_prompt(
            controller, prompt_id="ipg-always",
        )
        router = InlinePromptGateHTTPRouter(controller=controller)
        req = _make_request(
            f"/observability/inline_prompt/{pid}/respond",
            method="POST",
            match_info={"prompt_id": pid},
            body={"verdict": "allow_always"},
        )
        resp = await router._handle_respond(req)
        assert resp.status == 200
        body = json.loads(resp.body.decode("utf-8"))
        assert body["outcome"]["state"] == "allowed"
        assert body["outcome"]["response"] == "allow_always"

    @pytest.mark.asyncio
    async def test_invalid_verdict_400(self):
        controller = InlinePromptController(default_timeout_s=30.0)
        pid = _register_phase_boundary_prompt(
            controller, prompt_id="ipg-invalid",
        )
        router = InlinePromptGateHTTPRouter(controller=controller)
        req = _make_request(
            f"/observability/inline_prompt/{pid}/respond",
            method="POST",
            match_info={"prompt_id": pid},
            body={"verdict": "sudo_yes"},
        )
        resp = await router._handle_respond(req)
        assert resp.status == 400
        body = json.loads(resp.body.decode("utf-8"))
        assert body["reason_code"] == "inline_prompt_gate.invalid_verdict"

    @pytest.mark.asyncio
    async def test_missing_verdict_400(self):
        controller = InlinePromptController(default_timeout_s=30.0)
        pid = _register_phase_boundary_prompt(
            controller, prompt_id="ipg-missing",
        )
        router = InlinePromptGateHTTPRouter(controller=controller)
        req = _make_request(
            f"/observability/inline_prompt/{pid}/respond",
            method="POST",
            match_info={"prompt_id": pid},
            body={"reviewer": "ide"},
        )
        resp = await router._handle_respond(req)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_malformed_json_400(self):
        controller = InlinePromptController(default_timeout_s=30.0)
        router = InlinePromptGateHTTPRouter(controller=controller)
        req = _make_request(
            "/observability/inline_prompt/x/respond",
            method="POST",
            match_info={"prompt_id": "x"},
            body_bytes=b"{not json",
        )
        resp = await router._handle_respond(req)
        assert resp.status == 400
        body = json.loads(resp.body.decode("utf-8"))
        assert body["reason_code"] == "inline_prompt_gate.invalid_json"

    @pytest.mark.asyncio
    async def test_body_not_object_400(self):
        controller = InlinePromptController(default_timeout_s=30.0)
        router = InlinePromptGateHTTPRouter(controller=controller)
        req = _make_request(
            "/observability/inline_prompt/x/respond",
            method="POST",
            match_info={"prompt_id": "x"},
            body_bytes=b'["array", "not", "object"]',
        )
        resp = await router._handle_respond(req)
        assert resp.status == 400
        body = json.loads(resp.body.decode("utf-8"))
        assert body["reason_code"] == "inline_prompt_gate.body_not_object"

    @pytest.mark.asyncio
    async def test_oversized_body_413(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_INLINE_PROMPT_GATE_HTTP_MAX_BODY_BYTES", "64",
        )
        controller = InlinePromptController(default_timeout_s=30.0)
        router = InlinePromptGateHTTPRouter(controller=controller)
        big = b"x" * 200
        req = _make_request(
            "/observability/inline_prompt/x/respond",
            method="POST",
            match_info={"prompt_id": "x"},
            body_bytes=big,
        )
        resp = await router._handle_respond(req)
        assert resp.status == 413
        body = json.loads(resp.body.decode("utf-8"))
        assert body["reason_code"] == "inline_prompt_gate.body_too_large"

    @pytest.mark.asyncio
    async def test_unknown_prompt_id_404(self):
        controller = InlinePromptController(default_timeout_s=30.0)
        router = InlinePromptGateHTTPRouter(controller=controller)
        req = _make_request(
            "/observability/inline_prompt/missing/respond",
            method="POST",
            match_info={"prompt_id": "missing"},
            body={"verdict": "allow"},
        )
        resp = await router._handle_respond(req)
        assert resp.status == 404
        body = json.loads(resp.body.decode("utf-8"))
        assert body["reason_code"] == "inline_prompt_gate.unknown_prompt"

    @pytest.mark.asyncio
    async def test_per_tool_prompt_id_returns_404_not_phase_boundary(self):
        """Hostile ide-client cannot resolve per-tool-call prompts
        via this surface even with knowledge of the prompt_id."""
        controller = InlinePromptController(default_timeout_s=30.0)
        tool_req = _per_tool_call_request(prompt_id="tool-call-2")
        controller.request(tool_req, timeout_s=30.0)
        router = InlinePromptGateHTTPRouter(controller=controller)
        req = _make_request(
            "/observability/inline_prompt/tool-call-2/respond",
            method="POST",
            match_info={"prompt_id": "tool-call-2"},
            body={"verdict": "allow"},
        )
        resp = await router._handle_respond(req)
        assert resp.status == 404
        body = json.loads(resp.body.decode("utf-8"))
        assert body["reason_code"] == (
            "inline_prompt_gate.not_phase_boundary"
        )

    @pytest.mark.asyncio
    async def test_invalid_prompt_id_format_400(self):
        controller = InlinePromptController(default_timeout_s=30.0)
        router = InlinePromptGateHTTPRouter(controller=controller)
        req = _make_request(
            "/observability/inline_prompt/bad id/respond",
            method="POST",
            match_info={"prompt_id": "bad id"},
            body={"verdict": "allow"},
        )
        resp = await router._handle_respond(req)
        assert resp.status == 400
        body = json.loads(resp.body.decode("utf-8"))
        assert body["reason_code"] == (
            "inline_prompt_gate.invalid_prompt_id"
        )

    @pytest.mark.asyncio
    async def test_already_terminal_returns_409_with_snapshot(self):
        controller = InlinePromptController(default_timeout_s=30.0)
        pid = _register_phase_boundary_prompt(
            controller, prompt_id="ipg-terminal",
        )
        # Pre-resolve via the controller directly.
        controller.allow_once(pid, reviewer="repl")

        router = InlinePromptGateHTTPRouter(controller=controller)
        req = _make_request(
            f"/observability/inline_prompt/{pid}/respond",
            method="POST",
            match_info={"prompt_id": pid},
            body={"verdict": "deny"},
        )
        resp = await router._handle_respond(req)
        assert resp.status == 409
        body = json.loads(resp.body.decode("utf-8"))
        assert body["reason_code"] == (
            "inline_prompt_gate.already_terminal"
        )
        # Snapshot of current state included.
        assert body["prompt"]["state"] == "allowed"

    @pytest.mark.asyncio
    async def test_reviewer_truncated_to_64_chars(self):
        controller = InlinePromptController(default_timeout_s=30.0)
        pid = _register_phase_boundary_prompt(
            controller, prompt_id="ipg-trunc",
        )
        router = InlinePromptGateHTTPRouter(controller=controller)
        req = _make_request(
            f"/observability/inline_prompt/{pid}/respond",
            method="POST",
            match_info={"prompt_id": pid},
            body={
                "verdict": "allow",
                "reviewer": "x" * 200,
            },
        )
        resp = await router._handle_respond(req)
        assert resp.status == 200
        body = json.loads(resp.body.decode("utf-8"))
        assert len(body["outcome"]["reviewer"]) == 64


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class TestRateLimit:
    @pytest.mark.asyncio
    async def test_rate_limit_enforces_per_ip_quota(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_INLINE_PROMPT_GATE_HTTP_RATE_LIMIT_PER_MIN", "3",
        )
        controller = InlinePromptController(default_timeout_s=30.0)
        router = InlinePromptGateHTTPRouter(controller=controller)
        # 3 calls succeed.
        for _ in range(3):
            req = _make_request("/observability/inline_prompt")
            resp = await router._handle_list(req)
            assert resp.status == 200
        # 4th from same IP throttled.
        req = _make_request("/observability/inline_prompt")
        resp = await router._handle_list(req)
        assert resp.status == 429
        body = json.loads(resp.body.decode("utf-8"))
        assert body["reason_code"] == "inline_prompt_gate.rate_limited"

    @pytest.mark.asyncio
    async def test_rate_limit_per_ip_isolation(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_INLINE_PROMPT_GATE_HTTP_RATE_LIMIT_PER_MIN", "2",
        )
        controller = InlinePromptController(default_timeout_s=30.0)
        router = InlinePromptGateHTTPRouter(controller=controller)
        # IP A burns its budget.
        for _ in range(2):
            req = _make_request(
                "/observability/inline_prompt", remote="127.0.0.1",
            )
            await router._handle_list(req)
        # IP B still works.
        req = _make_request(
            "/observability/inline_prompt", remote="::1",
        )
        resp = await router._handle_list(req)
        assert resp.status == 200


# ---------------------------------------------------------------------------
# Schema version + CORS
# ---------------------------------------------------------------------------


class TestResponseShape:
    def test_schema_version_constant(self):
        assert (
            INLINE_PROMPT_GATE_HTTP_SCHEMA_VERSION
            == "inline_prompt_gate_http.1"
        )

    @pytest.mark.asyncio
    async def test_cors_header_for_allowed_origin(self):
        controller = InlinePromptController(default_timeout_s=30.0)
        router = InlinePromptGateHTTPRouter(controller=controller)
        req = _make_request(
            "/observability/inline_prompt",
            headers={"Origin": "http://localhost:5173"},
        )
        resp = await router._handle_list(req)
        assert resp.status == 200
        assert resp.headers["Access-Control-Allow-Origin"] == (
            "http://localhost:5173"
        )

    @pytest.mark.asyncio
    async def test_no_cors_for_disallowed_origin(self):
        controller = InlinePromptController(default_timeout_s=30.0)
        router = InlinePromptGateHTTPRouter(controller=controller)
        req = _make_request(
            "/observability/inline_prompt",
            headers={"Origin": "https://evil.example.com"},
        )
        resp = await router._handle_list(req)
        assert "Access-Control-Allow-Origin" not in resp.headers

    @pytest.mark.asyncio
    async def test_cache_control_no_store_on_responses(self):
        controller = InlinePromptController(default_timeout_s=30.0)
        router = InlinePromptGateHTTPRouter(controller=controller)
        req = _make_request("/observability/inline_prompt")
        resp = await router._handle_list(req)
        assert resp.headers["Cache-Control"] == "no-store"


# ---------------------------------------------------------------------------
# Verdict vocabulary closed-taxonomy
# ---------------------------------------------------------------------------


class TestVerdictVocabulary:
    def test_accepted_verdicts_exact_set(self):
        assert ACCEPTED_VERDICTS == frozenset({
            "allow", "allow_always", "deny", "pause",
        })

    def test_accepted_verdicts_is_frozenset(self):
        assert isinstance(ACCEPTED_VERDICTS, frozenset)


# ---------------------------------------------------------------------------
# Authority allowlist
# ---------------------------------------------------------------------------


class TestAuthorityAllowlist:
    def _http_source(self) -> str:
        path = (
            pathlib.Path(__file__).parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "inline_prompt_gate_http.py"
        )
        return path.read_text()

    def test_imports_in_allowlist(self):
        """Slice 3 hot path allowlist; module-owned registration
        functions (``register_flags`` / ``register_shipped_invariants``)
        are STRUCTURALLY exempt — boot-time discovery only.
        Mirrors Priority #6 closure exemption."""
        allowed = {
            "backend.core.ouroboros.governance.inline_permission_prompt",
            "backend.core.ouroboros.governance.inline_prompt_gate",
            "backend.core.ouroboros.governance.inline_prompt_gate_runner",
        }
        tree = ast.parse(self._http_source())
        registration_funcs = {"register_flags", "register_shipped_invariants"}
        exempt_ranges = []
        for fnode in ast.walk(tree):
            if isinstance(fnode, ast.FunctionDef):
                if fnode.name in registration_funcs:
                    start = getattr(fnode, "lineno", 0)
                    end = getattr(fnode, "end_lineno", start) or start
                    exempt_ranges.append((start, end))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if "backend." in module or (
                    "governance" in module and module
                ):
                    lineno = getattr(node, "lineno", 0)
                    if any(s <= lineno <= e for s, e in exempt_ranges):
                        continue
                    if module not in allowed:
                        raise AssertionError(
                            f"Slice 3 imported module outside allowlist: "
                            f"{module!r} at line {lineno}"
                        )

    def test_no_orchestrator_tier_imports(self):
        banned_substrings = (
            "orchestrator", "phase_runner", "iron_gate",
            "change_engine", "candidate_generator",
            ".providers", "doubleword_provider", "urgency_router",
            "auto_action_router", "subagent_scheduler",
            "tool_executor", "semantic_guardian",
            "semantic_firewall", "risk_engine",
        )
        tree = ast.parse(self._http_source())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for ban in banned_substrings:
                    if ban in module:
                        raise AssertionError(
                            f"Slice 3 imported BANNED orchestrator-tier "
                            f"substring {ban!r} via {module!r}"
                        )

    def test_no_exec_eval_compile_calls(self):
        tree = ast.parse(self._http_source())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    if node.func.id in ("exec", "eval", "compile"):
                        raise AssertionError(
                            f"Slice 3 must NOT exec/eval/compile — "
                            f"found {node.func.id}() at line "
                            f"{getattr(node, 'lineno', '?')}"
                        )
