"""Move 6.5 Slice 4 — HTTP observability surface.

`GET /observability/multi-prior` — recent K observations from
the dispatch ledger (default 50; clamped via Slice 4
:func:`read_limit_default`).
`GET /observability/multi-prior/{op_id}` — per-op detail.

Auto-mounted by `event_channel.py` via the §32.11 Slice 3
``observability_route_registry`` (file ends in
``_observability.py`` + exposes module-level
``register_routes(app, **kwargs)`` per §33.3 Slice 5b
naming-cage convention) — **zero-edit registration**.

## Architectural locks (operator mandate, AST-pinned)

  * **Read-only** — composes Slice 4's read API
    (:func:`read_recent_observations`,
    :func:`find_by_op_id`); NEVER writes; NEVER triggers a
    record (operator snapshot, not active probe).
  * **Master-flag-gated** — returns 503 when Slice 4's
    :func:`master_enabled` returns False so operators see an
    explicit "disabled" signal rather than stale data.
  * **Authority asymmetry** — imports stdlib + aiohttp +
    Slice 4 ONLY. NEVER imports orchestrator / iron_gate /
    policy / providers / candidate_generator.
  * **Naming-cage compliant** — module name ends
    ``_observability.py``; module-level ``register_routes``
    with the §33.3 contract signature.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


MULTI_PRIOR_OBSERVABILITY_SCHEMA_VERSION: str = (
    "multi_prior_observability.1"
)


# ---------------------------------------------------------------------------
# Route handler
# ---------------------------------------------------------------------------


class _MultiPriorRoutesHandler:
    """aiohttp route handler for the
    ``GET /observability/multi-prior`` family.

    Read-only. Master-flag-gated. Per-request rate-limit + CORS
    via the shared IDEObservabilityRouter helper (caller-
    supplied)."""

    def __init__(
        self,
        *,
        rate_limit_check: Optional[
            Callable[[Any], bool]
        ] = None,
        cors_headers: Optional[
            Callable[[Any], Any]
        ] = None,
    ) -> None:
        self._rate_limit_check = rate_limit_check
        self._cors_headers = cors_headers

    def _gate(self, request: Any) -> Optional[Any]:
        try:
            from backend.core.ouroboros.governance.verification.multi_prior_observer import (  # noqa: E501
                master_enabled,
            )
        except Exception:  # noqa: BLE001
            return self._json_response(
                {"error": "substrate_unavailable"},
                status=503,
            )
        if not master_enabled():
            return self._json_response(
                {
                    "error": "disabled",
                    "schema_version": (
                        MULTI_PRIOR_OBSERVABILITY_SCHEMA_VERSION  # noqa: E501
                    ),
                },
                status=503,
            )
        if self._rate_limit_check is not None:
            try:
                if not self._rate_limit_check(request):
                    return self._json_response(
                        {"error": "rate_limited"},
                        status=429,
                    )
            except Exception:  # noqa: BLE001
                pass
        return None

    def _json_response(
        self, payload: dict, *, status: int = 200,
    ) -> Any:
        from aiohttp import web
        return web.json_response(
            payload,
            status=status,
            headers={"Cache-Control": "no-store"},
        )

    async def handle_overview(self, request: Any) -> Any:
        """``GET /observability/multi-prior`` — recent K rows
        from the dispatch ledger.

        Optional ``?limit=N`` query param (clamped to
        [1, 1000])."""
        gated = self._gate(request)
        if gated is not None:
            return gated
        try:
            from backend.core.ouroboros.governance.verification.multi_prior_observer import (  # noqa: E501
                read_limit_default,
                read_recent_observations,
            )
            limit = read_limit_default()
            try:
                raw = request.query.get("limit", "")
                if raw:
                    n = int(raw)
                    if n >= 1:
                        limit = min(1000, n)
            except (AttributeError, TypeError, ValueError):
                pass
            rows = read_recent_observations(limit=limit)
            payload = {
                "schema_version": (
                    MULTI_PRIOR_OBSERVABILITY_SCHEMA_VERSION
                ),
                "sse_event_type": "multi_prior_dispatch",
                "limit": int(limit),
                "count": len(rows),
                "observations": [r.to_dict() for r in rows],
            }
            return self._json_response(payload)
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[MultiPriorObservability] overview "
                "raised: %s", exc,
            )
            return self._json_response(
                {"error": "snapshot_failed"},
                status=500,
            )

    async def handle_per_op(self, request: Any) -> Any:
        """``GET /observability/multi-prior/{op_id}`` — most
        recent observation for a specific op_id (404 on miss).
        """
        gated = self._gate(request)
        if gated is not None:
            return gated
        try:
            op_id = str(
                request.match_info.get("op_id", ""),
            ).strip()
            if not op_id:
                return self._json_response(
                    {"error": "missing_op_id"},
                    status=400,
                )
            from backend.core.ouroboros.governance.verification.multi_prior_observer import (  # noqa: E501
                find_by_op_id,
            )
            obs = find_by_op_id(op_id)
            if obs is None:
                return self._json_response(
                    {
                        "error": "not_found",
                        "op_id": op_id,
                        "schema_version": (
                            MULTI_PRIOR_OBSERVABILITY_SCHEMA_VERSION  # noqa: E501
                        ),
                    },
                    status=404,
                )
            payload = {
                "schema_version": (
                    MULTI_PRIOR_OBSERVABILITY_SCHEMA_VERSION
                ),
                "observation": obs.to_dict(),
            }
            return self._json_response(payload)
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[MultiPriorObservability] per-op raised: "
                "%s", exc,
            )
            return self._json_response(
                {"error": "snapshot_failed"},
                status=500,
            )


# ---------------------------------------------------------------------------
# Public auto-mount surface — §33.3 naming-cage contract
# ---------------------------------------------------------------------------


def register_routes(
    app: Any,
    *,
    rate_limit_check: Optional[
        Callable[[Any], bool]
    ] = None,
    cors_headers: Optional[Callable[[Any], Any]] = None,
) -> None:
    """Module-level register_routes per §33.3 Slice 5b
    naming-cage. Called by event_channel via §32.11 Slice 3
    auto-discovery; zero-edit registration. NEVER raises."""
    handler = _MultiPriorRoutesHandler(
        rate_limit_check=rate_limit_check,
        cors_headers=cors_headers,
    )
    app.router.add_get(
        "/observability/multi-prior",
        handler.handle_overview,
    )
    app.router.add_get(
        "/observability/multi-prior/{op_id}",
        handler.handle_per_op,
    )


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins:

      1. ``multi_prior_observability_authority_asymmetry`` —
         no orchestrator-tier imports.
      2. ``multi_prior_observability_read_only`` — handler
         MUST NOT invoke any record / persist function.
      3. ``multi_prior_observability_naming_cage_compliant``
         — module-level ``register_routes`` with §33.3
         signature is present.
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/verification/"
        "multi_prior_observability.py"
    )

    def _validate_authority_asymmetry(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        forbidden_substring = (
            "iron_gate", "providers", "candidate_generator",
            "urgency_router", "change_engine",
            "semantic_guardian", "plan_generator",
            "direction_inferrer",
        )
        forbidden_exact = {"orchestrator", "policy"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                segments = module.split(".")
                if any(
                    "multi_prior_observability" in s
                    for s in segments
                ):
                    continue
                for seg in segments:
                    if seg in forbidden_exact:
                        violations.append(
                            f"multi_prior_observability.py "
                            f"MUST NOT import {module!r} "
                            f"(forbidden segment {seg!r})"
                        )
                        break
                for f in forbidden_substring:
                    if any(f in seg for seg in segments):
                        violations.append(
                            f"multi_prior_observability.py "
                            f"MUST NOT import {module!r} "
                            f"(forbidden token {f!r})"
                        )
                        break
        return tuple(violations)

    def _validate_read_only(
        tree: "ast.Module", source: str,
    ) -> tuple:
        """Forbidden: any call to ``record_dispatch_outcome``,
        ``flock_append_line``, or method named ``record`` on
        the observer singleton. Only read API + helpers
        permitted."""
        violations: list = []
        forbidden_names = {
            "record_dispatch_outcome",
            "flock_append_line",
            "record_quorum_run",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if (
                    isinstance(func, ast.Name)
                    and func.id in forbidden_names
                ):
                    violations.append(
                        f"observability MUST NOT invoke "
                        f"{func.id!r} — read-only surface "
                        f"(line {node.lineno})"
                    )
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr in forbidden_names
                ):
                    violations.append(
                        f"observability MUST NOT invoke "
                        f"{func.attr!r} — read-only surface "
                        f"(line {node.lineno})"
                    )
        return tuple(violations)

    def _validate_naming_cage_compliant(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        found = False
        for node in tree.body:
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "register_routes"
            ):
                found = True
                # Must have ``app`` as first positional.
                if not node.args.args:
                    violations.append(
                        "register_routes MUST take ``app`` "
                        "as first positional"
                    )
                else:
                    if node.args.args[0].arg != "app":
                        violations.append(
                            "register_routes first "
                            "positional arg MUST be ``app`` "
                            "per §33.3 contract"
                        )
                # Must have keyword-only rate_limit_check +
                # cors_headers per §33.3 contract.
                kwonly_names = {
                    a.arg for a in node.args.kwonlyargs
                }
                if "rate_limit_check" not in kwonly_names:
                    violations.append(
                        "register_routes MUST accept "
                        "``rate_limit_check`` keyword per "
                        "§33.3 contract"
                    )
                if "cors_headers" not in kwonly_names:
                    violations.append(
                        "register_routes MUST accept "
                        "``cors_headers`` keyword per §33.3 "
                        "contract"
                    )
                break
        if not found:
            violations.append(
                "module-level register_routes(app, **kwargs) "
                "MUST exist per §33.3 Slice 5b naming-cage"
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "multi_prior_observability_"
                "authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Move 6.5 Slice 4 — observability surface "
                "stays substrate-pure: no orchestrator-tier "
                "imports."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "multi_prior_observability_read_only"
            ),
            target_file=target,
            description=(
                "Move 6.5 Slice 4 — observability MUST NOT "
                "invoke any record / persist function "
                "(read-only surface)."
            ),
            validate=_validate_read_only,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "multi_prior_observability_"
                "naming_cage_compliant"
            ),
            target_file=target,
            description=(
                "Move 6.5 Slice 4 — module-level "
                "register_routes(app, *, rate_limit_check, "
                "cors_headers) per §33.3 Slice 5b "
                "naming-cage convention so §32.11 Slice 3 "
                "auto-mounts zero-edit."
            ),
            validate=_validate_naming_cage_compliant,
        ),
    ]


__all__ = [
    "MULTI_PRIOR_OBSERVABILITY_SCHEMA_VERSION",
    "register_routes",
    "register_shipped_invariants",
]
