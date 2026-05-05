"""Module discovery primitive — Slice 5b consolidation Slice 2
(PRD §32.5 + §32.11).

Single source of truth for the
"walk a curated package list → find direct submodules exposing a
named callable → invoke a handler" pattern that
:mod:`flag_registry_seed`, :mod:`shipped_code_invariants`, and
:mod:`help_dispatcher` independently implemented (~120 LOC of
near-identical code across three call sites).

Slices 3 + 4 of the consolidation arc add two more consumers
(observability route auto-mount + REPL dispatcher auto-discovery)
without duplicating the walker. Five future Slice 5 arcs will
continue to land surfaces that auto-mount through this primitive
— the Slice 5b debt class closes structurally.

## Architectural invariants (operator mandate, AST-pinned at Slice 5)

1. **Pure substrate** — stdlib + ``importlib`` + ``pkgutil`` ONLY.
   No governance imports. No async (pure sync; idempotent against
   re-entry). No ``exec`` / ``eval`` / ``compile``.
2. **NEVER raises** — every fault path produces a structured
   :class:`DiscoveryReport` with diagnostics. Boot is never
   blocked by one misconfigured module.
3. **Caller-injected packages + handler** — primitive accepts the
   curated package list + the per-module handler as parameters.
   Zero hardcoding of consumer-specific package lists or
   registration shapes inside this module.
4. **Per-module exception isolation** — one bad submodule's
   import or handler crash does NOT prevent discovery of its
   siblings. Per-package exception isolation likewise.
5. **Master-flag-gated** — :func:`module_discovery_enabled`
   reads ``JARVIS_MODULE_DISCOVERY_ENABLED``; when off, returns
   a zero-count :class:`DiscoveryReport` and consumers fall
   back to their static seed lists (the legacy `_register_seed_*`
   paths already preserved across the codebase).
6. **Idempotent at the substrate** — calling the primitive twice
   walks twice; idempotency at the consumer level is the
   consumer's responsibility (e.g. registry override semantics).

## Authority asymmetry

Imports stdlib ONLY. Specifically forbidden (AST-pinned):
``orchestrator`` / ``iron_gate`` / ``policy`` / ``providers`` /
``candidate_generator`` / ``urgency_router`` / ``change_engine``
/ ``semantic_guardian`` / ``graduation_orchestrator`` (archived).
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


MODULE_DISCOVERY_SCHEMA_VERSION: str = "module_discovery.1"


# ---------------------------------------------------------------------------
# Master flag
# ---------------------------------------------------------------------------


def module_discovery_enabled() -> bool:
    """``JARVIS_MODULE_DISCOVERY_ENABLED`` (default ``true``).

    When off, :func:`discover_module_provided_callable` is a fast
    no-op returning a zero-count report. Consumers fall back to
    their static seed lists (legacy behavior preserved). NEVER
    raises."""
    raw = os.environ.get(
        "JARVIS_MODULE_DISCOVERY_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated default — Slice 5b consolidation Slice 5
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Result types — frozen, JSON-projectable
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SkippedModule:
    """One module skipped during discovery + the reason. Frozen
    so reports propagate safely across async boundaries."""

    full_name: str
    reason: str


@dataclass(frozen=True)
class DiscoveryReport:
    """Structured outcome of one
    :func:`discover_module_provided_callable` call. NEVER raises
    out of construction; every field is bounded."""

    discovered_count: int
    modules_scanned: int
    submodules_seen: int
    packages_unavailable: Tuple[SkippedModule, ...] = field(
        default_factory=tuple,
    )
    modules_skipped: Tuple[SkippedModule, ...] = field(
        default_factory=tuple,
    )
    elapsed_s: float = 0.0
    master_flag_on: bool = True
    schema_version: str = field(
        default=MODULE_DISCOVERY_SCHEMA_VERSION,
    )

    def as_dict(self) -> dict:
        """Projection helper for telemetry / observability
        surfaces. Bounded."""
        return {
            "schema_version": self.schema_version,
            "discovered_count": self.discovered_count,
            "modules_scanned": self.modules_scanned,
            "submodules_seen": self.submodules_seen,
            "packages_unavailable": [
                {"full_name": s.full_name, "reason": s.reason}
                for s in self.packages_unavailable
            ],
            "modules_skipped": [
                {"full_name": s.full_name, "reason": s.reason}
                for s in self.modules_skipped
            ],
            "elapsed_s": float(self.elapsed_s),
            "master_flag_on": bool(self.master_flag_on),
        }


# ---------------------------------------------------------------------------
# Public API — discover_module_provided_callable
# ---------------------------------------------------------------------------


# Per-handler return: int = number of items contributed by this
# module. Non-int returns are treated as 0 (the legacy three
# implementations shared this convention).
HandlerCallable = Callable[[str, Any], int]


def _coerce_skipped_reason(exc: BaseException) -> str:
    """Bounded reason string for telemetry. NEVER raises."""
    try:
        return f"{type(exc).__name__}: {str(exc)[:200]}"
    except Exception:  # noqa: BLE001 — defensive
        return "unknown"


def discover_module_provided_callable(
    *,
    packages: Sequence[str],
    attr_name: Optional[str],
    handler: HandlerCallable,
    excluded_modules: Sequence[str] = (),
    log_prefix: str = "ModuleDiscovery",
) -> DiscoveryReport:
    """Walk every package in ``packages`` for direct submodules
    exposing a callable named ``attr_name`` and dispatch each to
    ``handler``. Returns a structured :class:`DiscoveryReport`.

    Contract:
      * ``packages`` — curated dotted package paths to walk
        (caller-supplied; primitive does NOT hardcode any list).
      * ``attr_name`` — the module-level attribute the primitive
        looks up via :func:`getattr` and tests for callability
        (e.g. ``"register_flags"`` / ``"register_shipped_invariants"``
        / ``"register_verbs"`` / ``"register_routes"``). Pass
        ``None`` for "module-scan mode" — handler is invoked
        once per successfully-imported module with the module
        object itself in lieu of an attribute callable. Used
        by consumers (e.g. REPL dispatch registry) where the
        attribute name varies per module by naming convention
        and must be resolved inside the handler.
      * ``handler(full_name, attr_or_module)`` — invoked once
        per matching module; returns the count contributed.
        Non-int / negative / handler-raising → counted as 0,
        skip recorded.
      * ``excluded_modules`` — full dotted names to skip
        (recursion guards; e.g. the calling consumer's own
        ``__name__``).
      * ``log_prefix`` — string prefix for debug log lines so
        operators can grep per-consumer telemetry.

    Master-flag gate: ``JARVIS_MODULE_DISCOVERY_ENABLED`` (default
    ``true``). When off, returns ``DiscoveryReport(discovered_count=0,
    master_flag_on=False, ...)`` and consumers fall back to their
    static seed lists.

    NEVER raises. Per-module + per-package exception isolation
    is structural (try/except at every boundary).
    """
    t0 = time.monotonic()
    if not module_discovery_enabled():
        return DiscoveryReport(
            discovered_count=0,
            modules_scanned=0,
            submodules_seen=0,
            elapsed_s=time.monotonic() - t0,
            master_flag_on=False,
        )

    discovered = 0
    modules_scanned = 0
    submodules_seen = 0
    packages_unavailable: list = []
    modules_skipped: list = []
    excluded = frozenset(excluded_modules or ())

    try:
        # Lazy stdlib imports — keep substrate authority floor
        # tight and avoid paying module-load cost when the
        # master flag is off (the early-return path above).
        from importlib import import_module
        import pkgutil

        for pkg_name in packages:
            try:
                pkg_mod = import_module(pkg_name)
                pkg_path = getattr(pkg_mod, "__path__", None)
                if not pkg_path:
                    packages_unavailable.append(
                        SkippedModule(
                            full_name=pkg_name,
                            reason="package has no __path__",
                        ),
                    )
                    continue
            except Exception as exc:  # noqa: BLE001 — defensive
                packages_unavailable.append(
                    SkippedModule(
                        full_name=pkg_name,
                        reason=_coerce_skipped_reason(exc),
                    ),
                )
                logger.debug(
                    "[%s] provider package %s unavailable: %s",
                    log_prefix, pkg_name, exc,
                )
                continue

            for _, name, _ispkg in pkgutil.iter_modules(pkg_path):
                submodules_seen += 1
                full_name = f"{pkg_name}.{name}"
                if full_name in excluded:
                    continue
                try:
                    mod = import_module(full_name)
                except Exception as exc:  # noqa: BLE001
                    modules_skipped.append(
                        SkippedModule(
                            full_name=full_name,
                            reason=(
                                f"import_failed: "
                                f"{_coerce_skipped_reason(exc)}"
                            ),
                        ),
                    )
                    logger.debug(
                        "[%s] import skipped %s: %s",
                        log_prefix, full_name, exc,
                    )
                    continue
                if attr_name is None:
                    # Module-scan mode: hand the module itself
                    # to the handler. Handler resolves the
                    # per-module attribute name internally
                    # (used by REPL dispatch registry where
                    # ``dispatch_<verb>_command`` varies per
                    # module by naming convention).
                    fn = mod
                else:
                    try:
                        fn = getattr(mod, attr_name, None)
                        if not callable(fn):
                            # Module simply doesn't contribute via
                            # this attr — common case, not an error.
                            continue
                    except Exception as exc:  # noqa: BLE001
                        modules_skipped.append(
                            SkippedModule(
                                full_name=full_name,
                                reason=(
                                    f"getattr_failed: "
                                    f"{_coerce_skipped_reason(exc)}"
                                ),
                            ),
                        )
                        continue
                modules_scanned += 1
                try:
                    count = handler(full_name, fn)
                except Exception as exc:  # noqa: BLE001
                    modules_skipped.append(
                        SkippedModule(
                            full_name=full_name,
                            reason=(
                                f"handler_raised: "
                                f"{_coerce_skipped_reason(exc)}"
                            ),
                        ),
                    )
                    logger.debug(
                        "[%s] handler raised on %s: %s",
                        log_prefix, full_name, exc,
                    )
                    continue
                if isinstance(count, int) and count > 0:
                    discovered += count
                    logger.debug(
                        "[%s] %s contributed %d item(s)",
                        log_prefix, full_name, count,
                    )
    except Exception as exc:  # noqa: BLE001 — outermost guard
        logger.debug(
            "[%s] discover_module_provided_callable "
            "outer exception: %s", log_prefix, exc,
        )

    return DiscoveryReport(
        discovered_count=discovered,
        modules_scanned=modules_scanned,
        submodules_seen=submodules_seen,
        packages_unavailable=tuple(packages_unavailable),
        modules_skipped=tuple(modules_skipped),
        elapsed_s=time.monotonic() - t0,
        master_flag_on=True,
    )


# ---------------------------------------------------------------------------
# Convenience handlers — composable building blocks for consumers
# ---------------------------------------------------------------------------


def make_registry_handler(
    *,
    registry: Any,
) -> HandlerCallable:
    """Build a handler for the
    ``fn(registry) -> int`` shape (used by FlagRegistry +
    HelpDispatcher). Returns the int directly; non-int /
    non-positive returns coerce to 0.

    The closure binds ``registry`` so callers don't need to
    construct lambdas inline. Exceptions surface to
    :func:`discover_module_provided_callable`'s per-handler
    isolation — the report records the skip with reason."""

    def _handler(_full_name: str, fn: Any) -> int:
        count = fn(registry)
        if isinstance(count, int) and count > 0:
            return count
        return 0

    return _handler


def make_factory_handler(
    *,
    register_one: Callable[[Any], None],
    iterable_validator: Optional[
        Callable[[Any], bool]
    ] = None,
) -> HandlerCallable:
    """Build a handler for the ``fn() -> Iterable[X]`` shape
    (used by ShippedCodeInvariants — module returns a list of
    specs; primitive iterates and registers each). Returns the
    count of successfully registered items.

    ``register_one(spec)`` — caller-supplied registrar invoked
    once per yielded spec; per-spec exceptions are caught and
    counted as 0 contribution.

    ``iterable_validator`` — optional predicate for additional
    type-checking (default: accept any non-falsy iterable)."""

    def _handler(_full_name: str, fn: Any) -> int:
        items = fn()
        if not items:
            return 0
        if iterable_validator is not None:
            try:
                if not iterable_validator(items):
                    return 0
            except Exception:  # noqa: BLE001
                return 0
        try:
            seq = list(items)
        except TypeError:
            return 0
        count = 0
        for spec in seq:
            try:
                register_one(spec)
                count += 1
            except Exception:  # noqa: BLE001 — defensive
                continue
        return count

    return _handler


__all__ = [
    "MODULE_DISCOVERY_SCHEMA_VERSION",
    "DiscoveryReport",
    "HandlerCallable",
    "SkippedModule",
    "discover_module_provided_callable",
    "make_factory_handler",
    "make_registry_handler",
    "module_discovery_enabled",
]
