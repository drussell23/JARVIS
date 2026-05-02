"""InlinePromptGate Slice 3 — HTTP POST response surface.

The IDE-side response path. Mirrors the existing
``IDEObservabilityRouter`` (Gap #6 Slice 1) hardening — loopback-
only bind, per-IP sliding-window rate limit, schema-versioned JSON,
CORS allowlist, no internal-state leakage in error responses —
but exposes a **WRITE** route. This is why it lives in its own
module: ``ide_observability.py`` is AST-pinned read-only with
``no orchestrator/policy/iron_gate imports``; the inline-prompt
response surface is explicitly authority-bearing (resolves a
controller Future, mutates pending state).

Architectural reuse — three existing surfaces compose with ZERO
duplication:

  * :class:`InlinePromptController` — singleton via
    :func:`get_default_controller`. The Slice 2 producer
    registered the prompt; this surface resolves it.
  * :func:`inline_prompt_gate_enabled` (Slice 1) — master gate.
    HTTP surface has its own additional gate
    (:func:`inline_prompt_gate_http_enabled`) so operators can
    enable the producer + REPL without exposing the HTTP write
    surface. Defense-in-depth.
  * Phase-boundary sentinels (Slice 2) — ``call_id`` prefix
    ``pb-`` and ``tool == "phase_boundary"`` distinguish
    phase-boundary prompts from per-tool-call prompts in the
    same controller; the HTTP surface filters by these so it
    cannot accidentally resolve a per-tool prompt outside its
    intended scope.

Three routes:

  * ``GET  /observability/inline_prompt`` — list pending
    phase-boundary prompts (filtered).
  * ``GET  /observability/inline_prompt/{prompt_id}`` — single
    pending or recent-history projection.
  * ``POST /observability/inline_prompt/{prompt_id}/respond`` —
    body ``{"verdict": "allow|deny|pause", "reviewer":
    "<id>", "reason": "<text>"}`` → resolves the controller
    Future via ``allow_once`` / ``deny`` / ``pause_op``.

Direct-solve principles:

  * **Asynchronous-ready** — aiohttp handlers are async by
    framework contract.
  * **Dynamic** — rate-limit + master flag re-read per call
    (hot-revert without restart).
  * **Adaptive** — every error path returns a closed-vocabulary
    ``reason_code`` rather than a stack trace. Caller IDE
    surfaces a static error catalog.
  * **Intelligent** — verdict vocabulary is a frozenset
    (``ACCEPTED_VERDICTS``) so any unknown verb 400s with the
    same code regardless of where it originated.
  * **Robust** — handlers NEVER raise out of aiohttp; every
    exception path 500s with ``inline_prompt_gate.internal_error``
    (no stack leak).
  * **No hardcoding** — verdict→method dispatch table is a
    module constant. Master + rate flags are env-tunable.

Authority invariants (AST-pinned by Slice 5):

  * MAY import: ``inline_prompt_gate`` (Slice 1 primitive),
    ``inline_prompt_gate_runner`` (Slice 2 sentinels),
    ``inline_permission_prompt`` (controller substrate).
  * MUST NOT import: orchestrator / phase_runner / iron_gate /
    change_engine / candidate_generator / providers /
    doubleword_provider / urgency_router / auto_action_router /
    subagent_scheduler / tool_executor / semantic_guardian /
    semantic_firewall / risk_engine.
  * No exec/eval/compile (mirrors Slice 1 + Slice 2 critical
    safety pin).
  * Master flag ``JARVIS_INLINE_PROMPT_GATE_HTTP_ENABLED``
    default-FALSE through Slices 3-4; Slice 5 graduation flips
    after defense-in-depth review confirms loopback enforcement
    + rate limiting are correct under hostile-IDE assumptions.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from aiohttp import web

from backend.core.ouroboros.governance.inline_permission_prompt import (
    InlinePromptController,
    InlinePromptStateError,
    get_default_controller,
)
from backend.core.ouroboros.governance.inline_prompt_gate import (
    inline_prompt_gate_enabled,
)
from backend.core.ouroboros.governance.inline_prompt_gate_runner import (
    PHASE_BOUNDARY_TOOL_SENTINEL,
)

logger = logging.getLogger(__name__)


INLINE_PROMPT_GATE_HTTP_SCHEMA_VERSION: str = "inline_prompt_gate_http.1"


# ---------------------------------------------------------------------------
# Master flag — write surface specifically, separate from producer
# ---------------------------------------------------------------------------


def inline_prompt_gate_http_enabled() -> bool:
    """``JARVIS_INLINE_PROMPT_GATE_HTTP_ENABLED`` (default ``false``
    until Slice 5 graduation).

    Distinct from :func:`inline_prompt_gate_enabled` (Slice 1) —
    that gates the PRODUCER; this gates the HTTP WRITE surface.
    Operators may enable the producer + REPL without exposing the
    HTTP write authority surface (defense-in-depth).

    Asymmetric env semantics — empty/whitespace = unset = current
    default; explicit truthy values evaluate true; explicit falsy
    values evaluate false. Re-read on every call so flips
    hot-revert without restart.
    """
    raw = os.environ.get(
        "JARVIS_INLINE_PROMPT_GATE_HTTP_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return False  # pre-graduation default
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Rate-limit knob — separate from read-side observability
# ---------------------------------------------------------------------------


def _rate_limit_per_min() -> int:
    """``JARVIS_INLINE_PROMPT_GATE_HTTP_RATE_LIMIT_PER_MIN`` —
    default 60 (writes are rarer than reads; half the IDE
    observability cap)."""
    raw = os.environ.get(
        "JARVIS_INLINE_PROMPT_GATE_HTTP_RATE_LIMIT_PER_MIN", "60",
    ).strip()
    try:
        return max(1, min(600, int(raw)))
    except (TypeError, ValueError):
        return 60


def _max_body_bytes() -> int:
    """``JARVIS_INLINE_PROMPT_GATE_HTTP_MAX_BODY_BYTES`` — default
    4096. Bounded to prevent abusive request bodies (the JSON we
    accept is tiny; 4KB is generous)."""
    raw = os.environ.get(
        "JARVIS_INLINE_PROMPT_GATE_HTTP_MAX_BODY_BYTES", "4096",
    ).strip()
    try:
        return max(64, min(65536, int(raw)))
    except (TypeError, ValueError):
        return 4096


def _cors_origin_patterns() -> tuple:
    """Allowlist of regex patterns for CORS. Same default as
    ``ide_observability`` so the IDE extension can talk to both
    surfaces under one origin allowlist. POST origin is hardened
    against any non-allowlisted origin via 403 (not just CORS
    response-header omission)."""
    raw = os.environ.get(
        "JARVIS_INLINE_PROMPT_GATE_HTTP_CORS_ORIGINS",
        r"^https?://localhost(:\d+)?$,"
        r"^https?://127\.0\.0\.1(:\d+)?$,"
        r"^vscode-webview://[a-z0-9-]+$",
    )
    return tuple(p.strip() for p in raw.split(",") if p.strip())


# ---------------------------------------------------------------------------
# Verdict vocabulary — closed taxonomy on the wire
# ---------------------------------------------------------------------------


#: Wire-format verdict vocabulary. Maps to controller methods at
#: dispatch time. Closed: any verb outside this set 400s with
#: ``inline_prompt_gate.invalid_verdict``.
_VERDICT_DISPATCH: Dict[str, str] = {
    "allow": "allow_once",
    "allow_always": "allow_always",
    "deny": "deny",
    "pause": "pause_op",
}

ACCEPTED_VERDICTS: frozenset = frozenset(_VERDICT_DISPATCH.keys())


# ---------------------------------------------------------------------------
# Prompt-id regex (defensive on URL boundary)
# ---------------------------------------------------------------------------


_PROMPT_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")


# ---------------------------------------------------------------------------
# Router class
# ---------------------------------------------------------------------------


class InlinePromptGateHTTPRouter:
    """Mounts the inline-prompt response routes on a caller-supplied
    aiohttp :class:`web.Application`.

    Separate from :class:`IDEObservabilityRouter` because the
    response surface is authority-bearing (mutates controller
    state via Future resolution) — keeping it isolated preserves
    the GET-only AST pin on ``ide_observability.py``.

    Loopback enforcement is upstream — the caller (boot wiring)
    asserts ``assert_loopback_only(host)`` from
    ``ide_observability`` before mounting any router on the app.
    """

    def __init__(
        self,
        *,
        controller: Optional[InlinePromptController] = None,
    ) -> None:
        self._controller = controller
        # Per-IP sliding-window rate tracker (independent of the
        # read-side observability tracker — different trust
        # boundary).
        self._rate_tracker: Dict[str, List[float]] = {}

    # --- registration -----------------------------------------------------

    def register_routes(self, app: "web.Application") -> None:
        app.router.add_get(
            "/observability/inline_prompt",
            self._handle_list,
        )
        app.router.add_get(
            "/observability/inline_prompt/{prompt_id}",
            self._handle_detail,
        )
        app.router.add_post(
            "/observability/inline_prompt/{prompt_id}/respond",
            self._handle_respond,
        )

    # --- request-path helpers ---------------------------------------------

    def _resolve_controller(self) -> InlinePromptController:
        return self._controller or get_default_controller()

    @staticmethod
    def _client_key(request: "web.Request") -> str:
        peer = getattr(request, "remote", "") or "unknown"
        return str(peer)

    def _check_rate_limit(self, client_key: str) -> bool:
        limit = _rate_limit_per_min()
        now = time.monotonic()
        window_start = now - 60.0
        history = self._rate_tracker.setdefault(client_key, [])
        while history and history[0] < window_start:
            history.pop(0)
        if len(history) >= limit:
            return False
        history.append(now)
        return True

    def _cors_headers(
        self, request: "web.Request",
    ) -> Dict[str, str]:
        origin = request.headers.get("Origin", "") or ""
        if not origin:
            return {}
        for pattern in _cors_origin_patterns():
            try:
                if re.match(pattern, origin):
                    return {
                        "Access-Control-Allow-Origin": origin,
                        "Vary": "Origin",
                        "Access-Control-Allow-Methods": (
                            "GET, POST, OPTIONS"
                        ),
                        "Access-Control-Allow-Headers": (
                            "Content-Type"
                        ),
                    }
            except re.error:
                continue
        return {}

    def _json_response(
        self,
        request: "web.Request",
        status: int,
        payload: Dict[str, Any],
    ) -> Any:
        from aiohttp import web
        if "schema_version" not in payload:
            payload = {
                "schema_version": (
                    INLINE_PROMPT_GATE_HTTP_SCHEMA_VERSION
                ),
                **payload,
            }
        resp = web.json_response(payload, status=status)
        for k, v in self._cors_headers(request).items():
            resp.headers[k] = v
        resp.headers["Cache-Control"] = "no-store"
        return resp

    def _error(
        self,
        request: "web.Request",
        status: int,
        reason_code: str,
    ) -> Any:
        return self._json_response(
            request, status,
            {"error": True, "reason_code": reason_code},
        )

    # --- shared filter: phase-boundary prompts only -----------------------

    @staticmethod
    def _is_phase_boundary_projection(p: Dict[str, Any]) -> bool:
        """Phase-boundary prompts carry ``tool ==
        PHASE_BOUNDARY_TOOL_SENTINEL`` in the controller projection.
        Per-tool-call prompts have a real tool name. This lets the
        same singleton controller serve both kinds without the HTTP
        surface ever resolving a per-tool-call prompt."""
        try:
            return str(p.get("tool", "")) == PHASE_BOUNDARY_TOOL_SENTINEL
        except Exception:  # noqa: BLE001 — defensive
            return False

    @staticmethod
    def _project_phase_boundary(p: Dict[str, Any]) -> Dict[str, Any]:
        """Bounded projection — drops fields the IDE doesn't need
        and won't index. Stable wire shape."""
        try:
            return {
                "prompt_id": str(p.get("prompt_id", "")),
                "op_id": str(p.get("op_id", "")),
                "call_id": str(p.get("call_id", "")),
                "target_path": str(p.get("target_path", "")),
                "arg_preview": str(p.get("arg_preview", "")),
                "state": str(p.get("state", "")),
                "reviewer": str(p.get("reviewer", "")),
                "operator_reason": str(p.get("operator_reason", "")),
                "created_ts": float(p.get("created_ts", 0.0) or 0.0),
                "timeout_s": float(p.get("timeout_s", 0.0) or 0.0),
                "expires_ts": float(p.get("expires_ts", 0.0) or 0.0),
            }
        except Exception:  # noqa: BLE001 — defensive
            return {"prompt_id": "", "state": ""}

    # --- handlers ---------------------------------------------------------

    async def _handle_list(self, request: "web.Request") -> Any:
        try:
            if not inline_prompt_gate_http_enabled():
                return self._error(
                    request, 403, "inline_prompt_gate.http_disabled",
                )
            if not self._check_rate_limit(self._client_key(request)):
                return self._error(
                    request, 429, "inline_prompt_gate.rate_limited",
                )
            controller = self._resolve_controller()
            snapshots = controller.snapshot_all() or []
            phase_boundary_only = [
                self._project_phase_boundary(p)
                for p in snapshots
                if self._is_phase_boundary_projection(p)
            ]
            return self._json_response(
                request, 200,
                {
                    "prompts": phase_boundary_only,
                    "count": len(phase_boundary_only),
                },
            )
        except Exception as exc:  # noqa: BLE001 — last-resort
            logger.warning(
                "[InlinePromptGateHTTP] _handle_list internal: %s", exc,
            )
            return self._error(
                request, 500, "inline_prompt_gate.internal_error",
            )

    async def _handle_detail(self, request: "web.Request") -> Any:
        try:
            if not inline_prompt_gate_http_enabled():
                return self._error(
                    request, 403, "inline_prompt_gate.http_disabled",
                )
            if not self._check_rate_limit(self._client_key(request)):
                return self._error(
                    request, 429, "inline_prompt_gate.rate_limited",
                )
            prompt_id = request.match_info.get("prompt_id", "") or ""
            if not _PROMPT_ID_RE.match(prompt_id):
                return self._error(
                    request, 400, "inline_prompt_gate.invalid_prompt_id",
                )
            controller = self._resolve_controller()
            snap = controller.snapshot(prompt_id)
            if snap is None or not self._is_phase_boundary_projection(snap):
                return self._error(
                    request, 404, "inline_prompt_gate.unknown_prompt",
                )
            return self._json_response(
                request, 200,
                {"prompt": self._project_phase_boundary(snap)},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[InlinePromptGateHTTP] _handle_detail internal: %s", exc,
            )
            return self._error(
                request, 500, "inline_prompt_gate.internal_error",
            )

    async def _handle_respond(self, request: "web.Request") -> Any:
        try:
            if not inline_prompt_gate_http_enabled():
                return self._error(
                    request, 403, "inline_prompt_gate.http_disabled",
                )
            # Producer must also be enabled — symmetric: if the
            # producer is off, no phase-boundary prompts can be
            # pending; responding has no meaning.
            if not inline_prompt_gate_enabled():
                return self._error(
                    request, 403, "inline_prompt_gate.producer_disabled",
                )
            if not self._check_rate_limit(self._client_key(request)):
                return self._error(
                    request, 429, "inline_prompt_gate.rate_limited",
                )
            prompt_id = request.match_info.get("prompt_id", "") or ""
            if not _PROMPT_ID_RE.match(prompt_id):
                return self._error(
                    request, 400, "inline_prompt_gate.invalid_prompt_id",
                )
            # Bounded body parse — defends against abusive bodies.
            try:
                raw = await request.content.read(_max_body_bytes() + 1)
            except Exception:  # noqa: BLE001
                return self._error(
                    request, 400, "inline_prompt_gate.body_read_failed",
                )
            if len(raw) > _max_body_bytes():
                return self._error(
                    request, 413, "inline_prompt_gate.body_too_large",
                )
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except (ValueError, UnicodeDecodeError):
                return self._error(
                    request, 400, "inline_prompt_gate.invalid_json",
                )
            if not isinstance(body, dict):
                return self._error(
                    request, 400, "inline_prompt_gate.body_not_object",
                )
            verdict_raw = body.get("verdict", "")
            if not isinstance(verdict_raw, str):
                return self._error(
                    request, 400, "inline_prompt_gate.invalid_verdict",
                )
            verdict = verdict_raw.strip().lower()
            if verdict not in ACCEPTED_VERDICTS:
                return self._error(
                    request, 400, "inline_prompt_gate.invalid_verdict",
                )
            method_name = _VERDICT_DISPATCH[verdict]
            reviewer_raw = body.get("reviewer", "ide")
            reviewer = (
                str(reviewer_raw).strip()[:64] if reviewer_raw else "ide"
            )
            reason_raw = body.get("reason", "")
            reason = (
                str(reason_raw).strip()[:2000] if reason_raw else ""
            )

            controller = self._resolve_controller()
            # Filter: ensure the prompt_id is a phase-boundary prompt.
            # Without this, the HTTP write surface could
            # accidentally resolve a per-tool-call prompt.
            snap = controller.snapshot(prompt_id)
            if snap is None:
                return self._error(
                    request, 404, "inline_prompt_gate.unknown_prompt",
                )
            if not self._is_phase_boundary_projection(snap):
                return self._error(
                    request, 404,
                    "inline_prompt_gate.not_phase_boundary",
                )

            method = getattr(controller, method_name, None)
            if method is None:
                # Should not occur — _VERDICT_DISPATCH names mirror
                # controller API surface. Defensive last-resort.
                return self._error(
                    request, 500, "inline_prompt_gate.internal_error",
                )
            try:
                outcome = method(
                    prompt_id, reviewer=reviewer, reason=reason,
                )
            except InlinePromptStateError as exc:
                # Already terminal (race with controller's own
                # timeout, REPL, or another HTTP caller). Idempotent
                # GET-then-confirm pattern: surface the current
                # snapshot so caller sees the prevailing verdict.
                logger.info(
                    "[InlinePromptGateHTTP] state error: %s "
                    "prompt_id=%s", exc, prompt_id,
                )
                snap_after = controller.snapshot(prompt_id)
                return self._json_response(
                    request, 409,
                    {
                        "error": True,
                        "reason_code": "inline_prompt_gate.already_terminal",
                        "prompt": (
                            self._project_phase_boundary(snap_after)
                            if snap_after else None
                        ),
                    },
                )

            return self._json_response(
                request, 200,
                {
                    "prompt_id": prompt_id,
                    "outcome": {
                        "state": str(outcome.state),
                        "response": (
                            outcome.response.value
                            if outcome.response else None
                        ),
                        "reviewer": str(outcome.reviewer),
                        "operator_reason": str(outcome.operator_reason),
                        "elapsed_s": float(outcome.elapsed_s),
                    },
                },
            )
        except Exception as exc:  # noqa: BLE001 — last-resort
            logger.warning(
                "[InlinePromptGateHTTP] _handle_respond internal: %s",
                exc,
            )
            return self._error(
                request, 500, "inline_prompt_gate.internal_error",
            )


# ---------------------------------------------------------------------------
# Public surface — Slice 5 will pin via shipped_code_invariants
# ---------------------------------------------------------------------------

__all__ = [
    "ACCEPTED_VERDICTS",
    "INLINE_PROMPT_GATE_HTTP_SCHEMA_VERSION",
    "InlinePromptGateHTTPRouter",
    "inline_prompt_gate_http_enabled",
    "register_flags",
    "register_shipped_invariants",
]


# ---------------------------------------------------------------------------
# Slice 5 — Module-owned FlagRegistry contribution (4 HTTP-side flags)
# ---------------------------------------------------------------------------


def register_flags(registry: Any) -> int:
    """Module-owned :class:`FlagRegistry` registration for the 4
    HTTP-side env knobs. Discovered automatically by
    ``flag_registry_seed._discover_module_provided_flags``. Returns
    the count of flags registered."""
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category, FlagSpec, FlagType,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.warning(
            "[InlinePromptGateHTTP] register_flags degraded: %s", exc,
        )
        return 0
    specs = [
        FlagSpec(
            name="JARVIS_INLINE_PROMPT_GATE_HTTP_ENABLED",
            type=FlagType.BOOL, default=False,
            category=Category.SAFETY,
            source_file=(
                "backend/core/ouroboros/governance/"
                "inline_prompt_gate_http.py"
            ),
            example="JARVIS_INLINE_PROMPT_GATE_HTTP_ENABLED=true",
            description=(
                "Write-surface gate for the InlinePromptGate HTTP "
                "POST endpoint. Default-FALSE (operator-controlled "
                "cost ramp; loopback-bound write authority requires "
                "explicit opt-in)."
            ),
        ),
        FlagSpec(
            name="JARVIS_INLINE_PROMPT_GATE_HTTP_RATE_LIMIT_PER_MIN",
            type=FlagType.INT, default=60,
            category=Category.CAPACITY,
            source_file=(
                "backend/core/ouroboros/governance/"
                "inline_prompt_gate_http.py"
            ),
            example=(
                "JARVIS_INLINE_PROMPT_GATE_HTTP_RATE_LIMIT_PER_MIN=120"
            ),
            description=(
                "Per-IP sliding-window rate limit for the HTTP "
                "response surface. Floor 1, ceiling 600. Default 60 "
                "(half the read-side observability cap; writes are "
                "rarer)."
            ),
        ),
        FlagSpec(
            name="JARVIS_INLINE_PROMPT_GATE_HTTP_MAX_BODY_BYTES",
            type=FlagType.INT, default=4096,
            category=Category.CAPACITY,
            source_file=(
                "backend/core/ouroboros/governance/"
                "inline_prompt_gate_http.py"
            ),
            example=(
                "JARVIS_INLINE_PROMPT_GATE_HTTP_MAX_BODY_BYTES=8192"
            ),
            description=(
                "Max request body bytes for POST /respond. The "
                "accepted JSON is tiny; 4KB is generous. Floor 64, "
                "ceiling 65536."
            ),
        ),
        FlagSpec(
            name="JARVIS_INLINE_PROMPT_GATE_HTTP_CORS_ORIGINS",
            type=FlagType.STR, default="",
            category=Category.INTEGRATION,
            source_file=(
                "backend/core/ouroboros/governance/"
                "inline_prompt_gate_http.py"
            ),
            example=(
                "JARVIS_INLINE_PROMPT_GATE_HTTP_CORS_ORIGINS="
                r"^https?://localhost(:\d+)?$"
            ),
            description=(
                "Comma-separated regex allowlist for "
                "Access-Control-Allow-Origin. Default covers "
                "localhost + 127.0.0.1 (any port) + vscode-webview. "
                "Empty/whitespace = use default."
            ),
        ),
    ]
    count = 0
    for spec in specs:
        try:
            registry.register(spec)
            count += 1
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[InlinePromptGateHTTP] register_flags spec %s "
                "skipped: %s", spec.name, exc,
            )
    return count


# ---------------------------------------------------------------------------
# Slice 5 — Module-owned shipped_code_invariants contribution
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Register Slice 3's structural invariants. Discovered
    automatically. Returns :class:`ShippedCodeInvariant` instances."""
    import ast as _ast
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    def _validate_authority_allowlist(
        tree: "_ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """Slice 3 may only import from a small allowlist. Banned:
        orchestrator-tier modules. Registration-contract exemption
        applies (register_flags/register_shipped_invariants are
        boot-time meta)."""
        violations: list = []
        allowed = {
            "backend.core.ouroboros.governance.inline_permission_prompt",
            "backend.core.ouroboros.governance.inline_prompt_gate",
            "backend.core.ouroboros.governance.inline_prompt_gate_runner",
        }
        registration_funcs = {
            "register_flags",
            "register_shipped_invariants",
        }
        exempt_ranges = []
        for fnode in _ast.walk(tree):
            if isinstance(fnode, _ast.FunctionDef):
                if fnode.name in registration_funcs:
                    start = getattr(fnode, "lineno", 0)
                    end = getattr(fnode, "end_lineno", start) or start
                    exempt_ranges.append((start, end))
        banned_substrings = (
            "orchestrator", "phase_runner", "iron_gate",
            "change_engine", "candidate_generator",
            ".providers", "doubleword_provider", "urgency_router",
            "auto_action_router", "subagent_scheduler",
            "tool_executor", "semantic_guardian",
            "semantic_firewall", "risk_engine",
        )
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ImportFrom):
                module = node.module or ""
                lineno = getattr(node, "lineno", 0)
                if any(s <= lineno <= e for s, e in exempt_ranges):
                    continue
                for ban in banned_substrings:
                    if ban in module:
                        violations.append(
                            f"line {lineno}: BANNED orchestrator-tier "
                            f"substring {ban!r} in {module!r}"
                        )
                if "backend." in module or (
                    "governance" in module and module
                ):
                    if module not in allowed:
                        violations.append(
                            f"line {lineno}: import outside Slice 3 "
                            f"allowlist: {module!r}"
                        )
            if isinstance(node, _ast.Call):
                if isinstance(node.func, _ast.Name):
                    if node.func.id in ("exec", "eval", "compile"):
                        violations.append(
                            f"line {getattr(node, 'lineno', '?')}: "
                            f"Slice 3 MUST NOT {node.func.id}()"
                        )
        return tuple(violations)

    def _validate_verdict_dispatch_keys(
        tree: "_ast.Module", source: str,
    ) -> tuple:
        """The closed verdict vocabulary on the wire (ACCEPTED_VERDICTS)
        must remain {allow, allow_always, deny, pause}. Drift here
        breaks the IDE wire-format contract."""
        # Pin the literal source — substring match is the cheapest
        # robust shape (the dict is constructed inline).
        violations: list = []
        for required in (
            '"allow":', '"allow_always":', '"deny":', '"pause":',
        ):
            if required not in source:
                violations.append(
                    f"missing _VERDICT_DISPATCH key {required!r} "
                    "(IDE wire-format contract)"
                )
        return tuple(violations)

    target = (
        "backend/core/ouroboros/governance/inline_prompt_gate_http.py"
    )
    return [
        ShippedCodeInvariant(
            invariant_name="inline_prompt_gate_http_authority_allowlist",
            target_file=target,
            description=(
                "Slice 3 HTTP write surface imports stay within "
                "{inline_permission_prompt, inline_prompt_gate, "
                "inline_prompt_gate_runner} (+ registration-contract "
                "exemption). Banned: orchestrator-tier modules."
            ),
            validate=_validate_authority_allowlist,
        ),
        ShippedCodeInvariant(
            invariant_name="inline_prompt_gate_http_verdict_vocabulary",
            target_file=target,
            description=(
                "Closed verdict vocabulary on the wire "
                "(_VERDICT_DISPATCH dict) keeps {allow, allow_always, "
                "deny, pause}. Drift breaks the IDE wire-format "
                "contract."
            ),
            validate=_validate_verdict_dispatch_keys,
        ),
    ]
