"""Slice 5b consolidation Slice 4 — REPL command auto-dispatch
registry (PRD §32.5 / §32.11).

Closes the dispatch-pattern duplication in
``serpent_flow.py``: the pre-Slice-4 implementation used a
hardcoded if/elif ladder + ``_print_observability_verb`` helper
that lazy-imported one of 5 known dispatchers. New REPL
surfaces (m10, decisions, curiosity, ...) had to manually edit
that ladder to wire in — exactly the same Slice 5b debt class
Slice 3 closed for HTTP routes.

Slice 4 introduces a single :func:`try_dispatch` entry point
that auto-discovers every module-level
``dispatch_<verb>_command(line)`` callable across the curated
provider packages (using the Slice 2 ``module_discovery``
primitive), maps verb→callable, and routes lines like
``"<verb>"`` / ``"/<verb>"`` / ``"<verb> ..."`` /
``"/<verb> ..."`` to the matching dispatcher. Future Slice 5
arcs ship `*_repl.py` files with ``dispatch_<basename>_command``
and they auto-route zero-edit.

## Architectural locks (operator mandate, AST-pinned)

1. **Composes Slice 2 primitive** — uses
   :func:`module_discovery.discover_module_provided_callable`;
   no parallel walker.
2. **Verb name extracted from filename** — for module
   ``X_repl.py`` the verb is ``X``; for ``governance/m10/repl.py``
   the verb is ``m10``. Naming convention enforced by AST pin.
3. **Custom-handler exclusion list** — verbs with bespoke
   operator semantics that diverge from the
   ``dispatch_<verb>_command(line) -> SomeDispatchResult``
   contract are explicitly excluded:
   ``budget`` / ``risk`` / ``goal`` / ``cancel`` / ``plan`` /
   ``postmortems`` / ``inline`` retain their legacy custom
   handlers in :mod:`serpent_flow`. Auto-routing them would
   shadow operator UX (e.g. ``/budget 1.00`` to set cost cap).
4. **Idempotent verb→callable map** — built once on first
   :func:`try_dispatch` call (or via explicit
   :func:`prime_registry`); cached for subsequent calls.
   :func:`reset_registry_for_tests` clears the cache.
5. **Master-flag-gated** —
   ``JARVIS_REPL_DISPATCH_AUTODISCOVERY_ENABLED`` default-true.
   When off, :func:`try_dispatch` returns ``DispatchOutcome(matched=False)``
   and ``serpent_flow`` falls back to the legacy hardcoded
   ladder (preserved for instant rollback).
6. **Authority asymmetry** — imports stdlib + Slice 2 primitive
   ONLY. NEVER imports orchestrator / iron_gate / providers /
   candidate_generator / urgency_router / change_engine /
   semantic_guardian.

## Contract for surfaces

A consumer module under
``backend.core.ouroboros.governance`` (or its submodules) opts
into auto-dispatch by exposing a module-level callable named
``dispatch_<verb>_command`` with signature::

    def dispatch_<verb>_command(line: str) -> DispatchResult: ...

where ``DispatchResult`` is any object with ``.matched: bool``,
``.ok: bool``, and ``.text: str`` attributes.

For files named ``<verb>_repl.py``, the verb is the basename
minus ``_repl`` (e.g. ``decisions_repl.py`` → verb
``decisions``). For files named ``repl.py`` inside a
sub-package (e.g. ``m10/repl.py``), the verb is the
sub-package name (``m10``).
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


REPL_DISPATCH_REGISTRY_SCHEMA_VERSION: str = (
    "repl_dispatch_registry.1"
)


# ---------------------------------------------------------------------------
# Default provider packages
# ---------------------------------------------------------------------------


_DEFAULT_PROVIDER_PACKAGES: Tuple[str, ...] = (
    "backend.core.ouroboros.governance",
    "backend.core.ouroboros.governance.m10",
    "backend.core.ouroboros.governance.verification",
    "backend.core.ouroboros.governance.adaptation",
)


# Verbs whose operator-facing semantics diverge from the
# pure ``dispatch_<verb>_command(line)`` contract (e.g.
# ``/budget 1.00`` sets cost cap; ``/cancel <op-id>`` schedules
# cooperative cancellation; ``/postmortems`` takes argv-style).
# These retain their legacy custom handlers in
# :mod:`serpent_flow`.
_CUSTOM_HANDLER_EXCLUSIONS: Tuple[str, ...] = (
    "budget",
    "risk",
    "goal",
    "cancel",
    "plan",
    "postmortems",
    "inline",
)


# ---------------------------------------------------------------------------
# Master flag
# ---------------------------------------------------------------------------


def repl_dispatch_autodiscovery_enabled() -> bool:
    """``JARVIS_REPL_DISPATCH_AUTODISCOVERY_ENABLED``
    (default ``true``). When off, :func:`try_dispatch` returns
    a no-match outcome so the legacy hardcoded ladder in
    ``serpent_flow`` carries the load. NEVER raises."""
    raw = os.environ.get(
        "JARVIS_REPL_DISPATCH_AUTODISCOVERY_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated default — Slice 5
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Result types — frozen, JSON-projectable
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DispatchOutcome:
    """Result of a :func:`try_dispatch` call. ``matched=False``
    signals the line wasn't a known auto-discoverable verb (the
    caller routes elsewhere)."""

    matched: bool
    ok: bool
    text: str
    verb: str = ""
    schema_version: str = field(
        default=REPL_DISPATCH_REGISTRY_SCHEMA_VERSION,
    )


@dataclass(frozen=True)
class RegistryReport:
    """Snapshot of the verb→dispatcher map."""

    verb_count: int
    verbs: Tuple[str, ...]
    excluded: Tuple[str, ...]
    elapsed_s: float = 0.0
    master_flag_on: bool = True
    schema_version: str = field(
        default=REPL_DISPATCH_REGISTRY_SCHEMA_VERSION,
    )

    def as_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "verb_count": self.verb_count,
            "verbs": list(self.verbs),
            "excluded": list(self.excluded),
            "elapsed_s": float(self.elapsed_s),
            "master_flag_on": bool(self.master_flag_on),
        }


# ---------------------------------------------------------------------------
# Registry singleton
# ---------------------------------------------------------------------------


_REGISTRY_LOCK = threading.RLock()
# verb (str) → dispatcher callable (line: str) -> DispatchResult
_VERB_TO_DISPATCHER: dict = {}
_REGISTRY_PRIMED: bool = False


def reset_registry_for_tests() -> None:
    """Test helper — clears the cached verb→dispatcher map so a
    fresh discovery run can repopulate. NEVER raises."""
    global _REGISTRY_PRIMED
    with _REGISTRY_LOCK:
        _VERB_TO_DISPATCHER.clear()
        _REGISTRY_PRIMED = False


def list_verbs() -> Tuple[str, ...]:
    """Snapshot of registered verb names. Read-only."""
    with _REGISTRY_LOCK:
        return tuple(sorted(_VERB_TO_DISPATCHER.keys()))


# ---------------------------------------------------------------------------
# Verb-name extraction from module path
# ---------------------------------------------------------------------------


def _extract_verb_name(full_module_name: str) -> Optional[str]:
    """Map a discovered module's dotted name to its verb name.

    Rules:
      * ``X_repl`` (e.g. ``decisions_repl``) → ``X``
      * ``repl`` inside a sub-package (e.g. ``m10.repl``) →
        sub-package name (``m10``)
      * Anything else → None (skip)

    Returns the verb in lowercase. NEVER raises."""
    if not full_module_name:
        return None
    parts = full_module_name.rsplit(".", 1)
    if len(parts) != 2:
        return None
    parent_dotted, leaf = parts
    if leaf == "repl":
        # Sub-package case: parent's last segment is the verb.
        sub_parts = parent_dotted.rsplit(".", 1)
        if len(sub_parts) != 2:
            return None
        return sub_parts[1].lower() or None
    if leaf.endswith("_repl"):
        verb = leaf[: -len("_repl")]
        return verb.lower() or None
    return None


# ---------------------------------------------------------------------------
# Signature validator
# ---------------------------------------------------------------------------


def _validate_dispatch_signature(fn: Any) -> Optional[str]:
    """Validate that ``fn`` accepts a single positional ``line``
    argument. Returns None on accept; reason string on reject."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError) as exc:
        return f"signature_unavailable: {exc}"
    params = list(sig.parameters.values())
    if not params:
        return "no_parameters"
    first = params[0]
    if first.kind not in (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    ):
        return "first_param_must_be_positional_line"
    # Additional params MUST be optional (default values).
    for p in params[1:]:
        if (
            p.kind
            in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            )
            and p.default is inspect.Parameter.empty
            and p.kind
            != inspect.Parameter.VAR_POSITIONAL
        ):
            return (
                f"required_extra_param: {p.name!r} has no "
                f"default — registry calls dispatch with "
                f"line only"
            )
    return None


# ---------------------------------------------------------------------------
# Registry priming
# ---------------------------------------------------------------------------


def prime_registry(
    *,
    packages: Optional[Sequence[str]] = None,
    excluded_verbs: Optional[Sequence[str]] = None,
    excluded_modules: Optional[Sequence[str]] = None,
    force: bool = False,
) -> RegistryReport:
    """Walk the curated provider packages, find module-level
    ``dispatch_<verb>_command`` callables, and build the
    verb→dispatcher map.

    Idempotent: subsequent calls return immediately unless
    ``force=True`` (or :func:`reset_registry_for_tests` was
    invoked between calls).

    Master-flag gate: when off, returns
    ``RegistryReport(verb_count=0, master_flag_on=False, ...)``
    and the registry stays empty so :func:`try_dispatch` is a
    fast no-op."""
    global _REGISTRY_PRIMED
    t0 = time.monotonic()

    if not repl_dispatch_autodiscovery_enabled():
        return RegistryReport(
            verb_count=0,
            verbs=tuple(),
            excluded=tuple(_CUSTOM_HANDLER_EXCLUSIONS),
            elapsed_s=time.monotonic() - t0,
            master_flag_on=False,
        )

    with _REGISTRY_LOCK:
        if _REGISTRY_PRIMED and not force:
            return RegistryReport(
                verb_count=len(_VERB_TO_DISPATCHER),
                verbs=tuple(sorted(_VERB_TO_DISPATCHER.keys())),
                excluded=tuple(_CUSTOM_HANDLER_EXCLUSIONS),
                elapsed_s=time.monotonic() - t0,
            )
        if force:
            _VERB_TO_DISPATCHER.clear()

    pkg_list = (
        tuple(packages) if packages is not None
        else _DEFAULT_PROVIDER_PACKAGES
    )
    exclusions = frozenset(
        excluded_verbs if excluded_verbs is not None
        else _CUSTOM_HANDLER_EXCLUSIONS
    )
    excluded_modules_t = (
        tuple(excluded_modules)
        if excluded_modules is not None
        else ()
    )

    try:
        from backend.core.ouroboros.governance.meta.module_discovery import (  # noqa: E501
            discover_module_provided_callable,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[ReplRegistry] module_discovery primitive "
            "unavailable: %s", exc,
        )
        return RegistryReport(
            verb_count=0,
            verbs=tuple(),
            excluded=tuple(exclusions),
            elapsed_s=time.monotonic() - t0,
        )

    # Strategy: use the Slice 2 primitive's module-scan mode
    # (``attr_name=None``) so the handler is invoked once per
    # successfully-imported module with the module object. Each
    # *_repl.py file's verb name is encoded in its filename per
    # naming convention; the handler resolves
    # ``dispatch_<verb>_command`` per module.

    def module_handler(full_name: str, mod: Any) -> int:
        if full_name in excluded_modules_t:
            return 0
        verb = _extract_verb_name(full_name)
        if not verb:
            return 0
        if verb in exclusions:
            return 0
        attr_name = f"dispatch_{verb}_command"
        fn = getattr(mod, attr_name, None)
        if not callable(fn):
            return 0
        reason = _validate_dispatch_signature(fn)
        if reason is not None:
            logger.debug(
                "[ReplRegistry] %s rejected: %s",
                full_name, reason,
            )
            return 0
        with _REGISTRY_LOCK:
            if verb in _VERB_TO_DISPATCHER:
                logger.debug(
                    "[ReplRegistry] verb %r already "
                    "registered; ignoring %s",
                    verb, full_name,
                )
                return 0
            _VERB_TO_DISPATCHER[verb] = fn
        return 1

    discover_module_provided_callable(
        packages=pkg_list,
        attr_name=None,  # module-scan mode
        handler=module_handler,
        log_prefix="ReplRegistry",
    )

    with _REGISTRY_LOCK:
        _REGISTRY_PRIMED = True
        verbs = tuple(sorted(_VERB_TO_DISPATCHER.keys()))

    return RegistryReport(
        verb_count=len(verbs),
        verbs=verbs,
        excluded=tuple(exclusions),
        elapsed_s=time.monotonic() - t0,
    )


# ---------------------------------------------------------------------------
# Public API — try_dispatch
# ---------------------------------------------------------------------------


def _matches_verb(line: str, verb: str) -> bool:
    """Match the line shape used by every ``*_repl.py`` module:
    ``<verb>``, ``/<verb>``, ``<verb> ...``, ``/<verb> ...``."""
    s = (line or "").strip()
    if not s:
        return False
    return (
        s == verb
        or s == f"/{verb}"
        or s.startswith(f"{verb} ")
        or s.startswith(f"/{verb} ")
    )


def try_dispatch(line: str) -> DispatchOutcome:
    """Attempt to dispatch ``line`` through the auto-discovered
    verb→dispatcher map. Returns ``DispatchOutcome(matched=False)``
    if no verb matches; otherwise returns the dispatcher's
    result projected into a frozen ``DispatchOutcome``.

    Master-flag-gated. Idempotently primes the registry on
    first call. NEVER raises out — dispatcher exceptions
    surface as ``DispatchOutcome(matched=True, ok=False,
    text=<reason>)``."""
    s = (line or "").strip()
    if not s:
        return DispatchOutcome(matched=False, ok=False, text="")

    if not repl_dispatch_autodiscovery_enabled():
        return DispatchOutcome(matched=False, ok=False, text="")

    # Lazy prime on first call.
    with _REGISTRY_LOCK:
        if not _REGISTRY_PRIMED:
            # Release lock before priming (priming may import
            # modules that themselves try to query verb status).
            pass
    if not _REGISTRY_PRIMED:
        prime_registry()

    # Find the matching verb. We check from the longest verb
    # downward so e.g. ``/decisions`` matches before any
    # hypothetical ``/dec``.
    with _REGISTRY_LOCK:
        verbs_by_length = sorted(
            _VERB_TO_DISPATCHER.keys(),
            key=lambda v: -len(v),
        )

    for verb in verbs_by_length:
        if _matches_verb(s, verb):
            with _REGISTRY_LOCK:
                fn = _VERB_TO_DISPATCHER.get(verb)
            if fn is None:
                continue
            try:
                result = fn(line)
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.debug(
                    "[ReplRegistry] dispatcher %r raised: %s",
                    verb, exc,
                )
                return DispatchOutcome(
                    matched=True,
                    ok=False,
                    text=(
                        f"  /{verb} dispatcher raised "
                        f"{type(exc).__name__}: "
                        f"{str(exc)[:200]}"
                    ),
                    verb=verb,
                )
            # Project the dispatcher's result. Each *_repl.py
            # returns its own DispatchResult dataclass; we read
            # the standard tri-attribute shape via getattr.
            matched = bool(
                getattr(result, "matched", True),
            )
            if not matched:
                # Dispatcher recognized the line shape but
                # opted to defer (rare; e.g. dispatcher's own
                # ``_matches`` is stricter than ours). Fall
                # through to next verb.
                continue
            ok = bool(getattr(result, "ok", False))
            text = str(getattr(result, "text", ""))
            return DispatchOutcome(
                matched=True, ok=ok, text=text, verb=verb,
            )

    return DispatchOutcome(matched=False, ok=False, text="")


__all__ = [
    "DispatchOutcome",
    "REPL_DISPATCH_REGISTRY_SCHEMA_VERSION",
    "RegistryReport",
    "list_verbs",
    "prime_registry",
    "repl_dispatch_autodiscovery_enabled",
    "reset_registry_for_tests",
    "try_dispatch",
]
