"""Slice 5b consolidation Slice 3 — observability route auto-mount
registry (PRD §32.5 / §32.11).

Closes the dormant-observability-surface debt class structurally.
Pre-Slice-3, four `*_observability.py` modules
(`decisions_observability` / `curiosity_observability` /
`epistemic_budget_observability` / `m10/observability`) shipped
``register_routes(app)`` callables that nothing in
``event_channel.py`` invoked, so 5+ HTTP surfaces were
operationally invisible. Slice 3 introduces a single
auto-discovery call that mounts every module exposing the
canonical ``register_routes(app, *, rate_limit_check, cors_headers)``
signature. Future Slice 5 arcs that ship surfaces auto-mount
zero-edit.

## Architectural locks (operator mandate, AST-pinned)

1. **Composes Slice 2 primitive** — uses
   :func:`module_discovery.discover_module_provided_callable`;
   no parallel walker.
2. **Module-level functions only** — class-based routers
   (`IDEObservabilityRouter`, `IDEStreamRouter`, etc.) require
   constructor dependencies (scheduler / worktree_manager / ...)
   and stay explicitly wired in ``event_channel.py``. The
   auto-discovery path is for stateless module-level
   ``register_routes(app, **kwargs) -> None`` functions only.
3. **Signature validation** — primitive uses :mod:`inspect` to
   verify the discovered callable accepts ``app`` + optional
   ``rate_limit_check`` / ``cors_headers`` kwargs. Off-shape
   ``register_routes`` symbols (e.g. test fixtures, methods)
   are skipped with a structured reason.
4. **Idempotent at the registry** — module-level singleton
   tracks already-mounted modules; second call is a fast no-op.
   aiohttp's `add_get(path)` raises on duplicate; we never let
   that fire.
5. **Master-flag-gated** — ``JARVIS_OBSERVABILITY_AUTODISCOVERY_-
   ENABLED`` default-true. When off, the boot path falls back
   to the legacy explicit `register_routes(app)` call sites in
   ``event_channel.py`` (preserved for instant rollback).
6. **Authority asymmetry** — imports stdlib + Slice 2 primitive
   ONLY. NEVER imports orchestrator / iron_gate / providers /
   candidate_generator / urgency_router / change_engine /
   semantic_guardian.

## Contract for surfaces

A consumer module under `backend.core.ouroboros.governance` (or
the verification subpackage) opts into auto-mount by exposing a
module-level callable named exactly ``register_routes`` with
signature::

    def register_routes(
        app: Any,
        *,
        rate_limit_check: Optional[Callable[[Any], bool]] = None,
        cors_headers: Optional[Callable[[Any], Any]] = None,
    ) -> None: ...

Future Slice 5 arcs naming the file ``*_observability.py`` MUST
expose this surface (AST-pinned by Slice 5 graduation pin).
"""
from __future__ import annotations

import inspect
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


OBSERVABILITY_REGISTRY_SCHEMA_VERSION: str = (
    "observability_route_registry.1"
)


# ---------------------------------------------------------------------------
# Default provider packages
# ---------------------------------------------------------------------------


# Curated list of dotted package paths whose direct submodules
# may expose ``register_routes(app, **kwargs)``. Caller-overridable
# for testing; production boot uses these defaults.
_DEFAULT_PROVIDER_PACKAGES: Tuple[str, ...] = (
    "backend.core.ouroboros.governance",
    "backend.core.ouroboros.governance.m10",
    "backend.core.ouroboros.governance.verification",
    "backend.core.ouroboros.governance.observability",
)


# Recursion guard — modules that themselves OWN the discovery
# substrate must NOT be invoked as observability surfaces.
_SUBSTRATE_EXCLUSIONS: Tuple[str, ...] = (
    "backend.core.ouroboros.governance.observability_route_registry",  # noqa: E501
    "backend.core.ouroboros.governance.event_channel",
    # Class-based routers — these expose `register_routes` as
    # a class method, but the module-level lookup still hits the
    # CLASS object (not callable in the function-signature sense
    # we validate). Excluded explicitly to avoid signature-check
    # noise.
    "backend.core.ouroboros.governance.ide_observability",
    "backend.core.ouroboros.governance.ide_observability_stream",
    "backend.core.ouroboros.governance.ide_policy_router",
    "backend.core.ouroboros.governance.inline_permission_observability",  # noqa: E501
    "backend.core.ouroboros.governance.inline_prompt_gate_http",
    "backend.core.ouroboros.governance.context_manifest",
)


# ---------------------------------------------------------------------------
# Master flag
# ---------------------------------------------------------------------------


def observability_autodiscovery_enabled() -> bool:
    """``JARVIS_OBSERVABILITY_AUTODISCOVERY_ENABLED`` (default
    ``true``). When off, the boot path skips auto-mount and the
    legacy explicit `register_routes(app)` blocks in
    ``event_channel.py`` carry the load (preserved for instant
    rollback). NEVER raises."""
    raw = os.environ.get(
        "JARVIS_OBSERVABILITY_AUTODISCOVERY_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated default — Slice 5
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Registry singleton — idempotency tracking
# ---------------------------------------------------------------------------


_MOUNTED_LOCK = threading.RLock()
_MOUNTED_MODULES: set = set()  # full dotted module names already mounted


def _record_mounted(module_full_name: str) -> bool:
    """Returns True if this is a NEW mount (caller may proceed),
    False if already mounted (idempotency guard fired)."""
    with _MOUNTED_LOCK:
        if module_full_name in _MOUNTED_MODULES:
            return False
        _MOUNTED_MODULES.add(module_full_name)
        return True


def reset_registry_for_tests() -> None:
    """Test helper. Clears the mounted-modules set so the
    registry can be re-mounted in fresh aiohttp app fixtures.
    NEVER raises."""
    with _MOUNTED_LOCK:
        _MOUNTED_MODULES.clear()


# ---------------------------------------------------------------------------
# Result types — frozen, JSON-projectable
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MountedRoute:
    """One module successfully mounted by the registry."""

    module_full_name: str
    mounted_at_unix: float


@dataclass(frozen=True)
class MountReport:
    """Structured outcome of one
    :func:`discover_and_mount_observability_routes` call. Frozen,
    JSON-projectable for telemetry."""

    mounted_count: int
    already_mounted: int
    signature_rejected: int
    handler_failed: int
    mounted: Tuple[MountedRoute, ...] = field(default_factory=tuple)
    skipped_reasons: Tuple[Tuple[str, str], ...] = field(
        default_factory=tuple,
    )
    elapsed_s: float = 0.0
    master_flag_on: bool = True
    schema_version: str = field(
        default=OBSERVABILITY_REGISTRY_SCHEMA_VERSION,
    )

    def as_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "mounted_count": self.mounted_count,
            "already_mounted": self.already_mounted,
            "signature_rejected": self.signature_rejected,
            "handler_failed": self.handler_failed,
            "mounted": [
                {
                    "module_full_name": r.module_full_name,
                    "mounted_at_unix": r.mounted_at_unix,
                }
                for r in self.mounted
            ],
            "skipped_reasons": [
                {"module_full_name": m, "reason": r}
                for m, r in self.skipped_reasons
            ],
            "elapsed_s": float(self.elapsed_s),
            "master_flag_on": bool(self.master_flag_on),
        }


# ---------------------------------------------------------------------------
# Signature validator
# ---------------------------------------------------------------------------


def _validate_register_routes_signature(
    fn: Any,
) -> Optional[str]:
    """Validate that ``fn`` matches the canonical
    ``register_routes(app, *, rate_limit_check=None,
    cors_headers=None)`` shape. Returns None on accept; returns
    a human-readable reason string on reject. NEVER raises."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError) as exc:
        return f"signature_unavailable: {exc}"
    params = list(sig.parameters.values())
    if not params:
        return "no_parameters"
    # First param is the aiohttp Application — positional or
    # keyword. We only check it exists; type annotations vary.
    first = params[0]
    if first.kind in (
        inspect.Parameter.VAR_POSITIONAL,
        inspect.Parameter.VAR_KEYWORD,
    ):
        return "first_param_must_be_positional_app"
    # Optional kwargs MUST be acceptable. Either the function has
    # explicit ``rate_limit_check`` / ``cors_headers`` keyword
    # parameters, OR it accepts ``**kwargs`` (forward-compatible).
    accepts_var_keyword = any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in params
    )
    if accepts_var_keyword:
        return None
    # Otherwise: explicit kwargs must be present.
    explicit_kwarg_names = {
        p.name for p in params
        if p.kind in (
            inspect.Parameter.KEYWORD_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    }
    # Both kwargs MUST be representable. We allow EITHER
    # rate_limit_check or cors_headers individually missing
    # (forward-compat with simpler signatures).
    if not (
        "rate_limit_check" in explicit_kwarg_names
        or "cors_headers" in explicit_kwarg_names
    ):
        return (
            "missing_optional_kwargs: "
            "expected rate_limit_check / cors_headers"
        )
    return None


# ---------------------------------------------------------------------------
# Public API — discover_and_mount_observability_routes
# ---------------------------------------------------------------------------


def discover_and_mount_observability_routes(
    app: Any,
    *,
    rate_limit_check: Optional[Callable[[Any], bool]] = None,
    cors_headers: Optional[Callable[[Any], Any]] = None,
    packages: Optional[Sequence[str]] = None,
    excluded_modules: Optional[Sequence[str]] = None,
) -> MountReport:
    """Walk the curated provider packages, find modules exposing
    ``register_routes(app, **kwargs)``, and mount each.

    Composition: delegates the walk to
    :func:`module_discovery.discover_module_provided_callable`
    (Slice 2 primitive). Idempotent at module-name granularity —
    second call for an already-mounted module is a fast no-op.

    NEVER raises. Per-module signature/mount failures are
    recorded in the report and skipped.

    Master-flag gate: ``JARVIS_OBSERVABILITY_AUTODISCOVERY_ENABLED``
    (default ``true``). When off, returns
    ``MountReport(mounted_count=0, master_flag_on=False, ...)``."""
    t0 = time.monotonic()
    if not observability_autodiscovery_enabled():
        return MountReport(
            mounted_count=0,
            already_mounted=0,
            signature_rejected=0,
            handler_failed=0,
            elapsed_s=time.monotonic() - t0,
            master_flag_on=False,
        )

    pkg_list = (
        tuple(packages) if packages is not None
        else _DEFAULT_PROVIDER_PACKAGES
    )
    excluded = tuple(
        excluded_modules if excluded_modules is not None
        else _SUBSTRATE_EXCLUSIONS
    )

    counters = {
        "mounted": 0,
        "already_mounted": 0,
        "signature_rejected": 0,
        "handler_failed": 0,
    }
    mounted_routes: list = []
    skipped_reasons: list = []

    try:
        from backend.core.ouroboros.governance.meta.module_discovery import (  # noqa: E501
            discover_module_provided_callable,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[ObservabilityRegistry] module_discovery primitive "
            "unavailable: %s", exc,
        )
        return MountReport(
            mounted_count=0,
            already_mounted=0,
            signature_rejected=0,
            handler_failed=0,
            elapsed_s=time.monotonic() - t0,
            skipped_reasons=(
                ("__substrate__", f"primitive_unavailable: {exc}"),
            ),
        )

    def handler(full_name: str, fn: Any) -> int:
        # Recursion + class-based-router exclusion already filtered
        # by `excluded_modules`; reaching the handler means the
        # module passed the substrate's name filter.
        if not _record_mounted(full_name):
            counters["already_mounted"] += 1
            skipped_reasons.append(
                (full_name, "already_mounted"),
            )
            return 0
        # Signature validation — reject off-shape symbols
        # (e.g. a class with a `register_routes` method, or a
        # test fixture's stub).
        reason = _validate_register_routes_signature(fn)
        if reason is not None:
            counters["signature_rejected"] += 1
            skipped_reasons.append(
                (full_name, f"signature_rejected: {reason}"),
            )
            # Roll back the mounted-record so a future call
            # with a fixed signature can succeed.
            with _MOUNTED_LOCK:
                _MOUNTED_MODULES.discard(full_name)
            return 0
        # Mount.
        try:
            fn(
                app,
                rate_limit_check=rate_limit_check,
                cors_headers=cors_headers,
            )
        except TypeError as exc:
            # Most common: fn doesn't accept rate_limit_check
            # or cors_headers — try positional only.
            try:
                fn(app)
            except Exception as exc2:  # noqa: BLE001
                counters["handler_failed"] += 1
                skipped_reasons.append(
                    (
                        full_name,
                        f"mount_raised: "
                        f"{type(exc2).__name__}: "
                        f"{str(exc2)[:200]}",
                    ),
                )
                with _MOUNTED_LOCK:
                    _MOUNTED_MODULES.discard(full_name)
                return 0
            # Fallback succeeded — record it.
            _ = exc  # diagnostic; primary failure path handled
        except Exception as exc:  # noqa: BLE001
            counters["handler_failed"] += 1
            skipped_reasons.append(
                (
                    full_name,
                    f"mount_raised: "
                    f"{type(exc).__name__}: "
                    f"{str(exc)[:200]}",
                ),
            )
            with _MOUNTED_LOCK:
                _MOUNTED_MODULES.discard(full_name)
            return 0
        counters["mounted"] += 1
        mounted_routes.append(
            MountedRoute(
                module_full_name=full_name,
                mounted_at_unix=time.time(),
            ),
        )
        logger.debug(
            "[ObservabilityRegistry] mounted %s",
            full_name,
        )
        return 1

    discover_module_provided_callable(
        packages=pkg_list,
        attr_name="register_routes",
        handler=handler,
        excluded_modules=excluded,
        log_prefix="ObservabilityRegistry",
    )

    return MountReport(
        mounted_count=counters["mounted"],
        already_mounted=counters["already_mounted"],
        signature_rejected=counters["signature_rejected"],
        handler_failed=counters["handler_failed"],
        mounted=tuple(mounted_routes),
        skipped_reasons=tuple(skipped_reasons),
        elapsed_s=time.monotonic() - t0,
        master_flag_on=True,
    )


def list_mounted_modules() -> Tuple[str, ...]:
    """Snapshot of currently-mounted module names. Read-only;
    returns a tuple copy. Useful for telemetry + tests."""
    with _MOUNTED_LOCK:
        return tuple(sorted(_MOUNTED_MODULES))


__all__ = [
    "MountReport",
    "MountedRoute",
    "OBSERVABILITY_REGISTRY_SCHEMA_VERSION",
    "discover_and_mount_observability_routes",
    "list_mounted_modules",
    "observability_autodiscovery_enabled",
    "reset_registry_for_tests",
]
