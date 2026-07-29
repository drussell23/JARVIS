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
    # OR, equivalently:
    async def dispatch_<verb>_command(line: str) -> DispatchResult: ...

:func:`try_dispatch` is itself ``async`` and awaits the result
when the dispatcher is a coroutine function — callers MUST
``await try_dispatch(line)`` (it always runs on a live event
loop). ``DispatchResult`` is any object with ``.matched: bool``,
``.ok: bool``, and ``.text: str`` attributes.

For files named ``<verb>_repl.py``, the verb is the basename
minus ``_repl`` (e.g. ``decisions_repl.py`` → verb
``decisions``). For files named ``repl.py`` inside a
sub-package (e.g. ``m10/repl.py``), the verb is the
sub-package name (``m10``).
"""
from __future__ import annotations

import ast
import inspect
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

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
#: Verbs that EXIST but are routed by someone else → a provenance note.
#: Populated at prime time by derivation, never by hand: a hand-written
#: list is exactly what drifted here before, and a verb added to the
#: harness chain tomorrow must not need a second edit to be findable.
_EXTERNAL_VERBS: Dict[str, str] = {}

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
        # Both maps, or `list_verbs()` stays non-empty after a reset and
        # "discovery has not run" becomes indistinguishable from
        # "discovery found nothing" — a distinction the progress board
        # reports on and therefore must be able to make.
        _EXTERNAL_VERBS.clear()
        _REGISTRY_PRIMED = False


#: Modules whose if/elif command chains are scanned for verbs that exist
#: but are dispatched there. A curated LIST OF FILES, not of verbs — the
#: verbs are derived, so adding one needs no edit here.
_CHAIN_SOURCES: Tuple[str, ...] = (
    "backend/core/ouroboros/battle_test/harness.py",
    "backend/core/ouroboros/battle_test/serpent_flow.py",
)

#: Names a REPL command chain binds its input to. Structural rather than
#: positional: the scan finds branches by what they TEST, so the chain can
#: be reordered, renamed or split without breaking discovery.
_CHAIN_SUBJECTS = frozenset({"cmd", "command"})


def discover_chain_verbs(
    sources: Optional[Sequence[str]] = None, root: Optional[str] = None,
) -> Dict[str, str]:
    """Verbs handled by an if/elif command chain → where. NEVER raises.

    Derived by reading the chain itself, because every hand-maintained
    alternative drifts. That is not hypothetical here: `/help` lost
    sixteen verbs precisely because a list said what existed and the code
    said something else, and nothing compared them.

    The scan is structural — it looks for branches whose test inspects a
    command-shaped variable (``cmd == "x"``, ``cmd.startswith("/x")``) —
    so the chain can be reordered, renamed or split and discovery follows.
    It runs once at prime time on two files and is bounded by that.

    Only literals that look like verbs are admitted, and only from
    top-level branch TESTS, never from bodies — a string inside a branch
    is an argument or a message, not a verb.
    """
    out: Dict[str, str] = {}
    try:
        import os as _os
        base = root or _os.getcwd()
        for rel in (sources if sources is not None else _CHAIN_SOURCES):
            path = _os.path.join(base, rel)
            try:
                tree = ast.parse(open(path, "r", encoding="utf-8").read())
            except Exception:  # noqa: BLE001
                continue
            name = _os.path.basename(rel)
            for node in ast.walk(tree):
                if not isinstance(node, ast.If):
                    continue
                if not _branch_routes_somewhere(node):
                    continue
                for verb in _verbs_in_test(node.test):
                    out.setdefault(verb, f"{name}:{node.lineno}")
    except Exception:  # noqa: BLE001
        logger.debug("[ReplRegistry] chain discovery degraded", exc_info=True)
    return out


#: Call-name shapes a verb branch routes to. Prefixes rather than exact
#: names, so a new handler needs no edit here.
_HANDLER_SHAPES: Tuple[str, ...] = (
    "_repl_cmd_", "_handle_", "_cmd_", "dispatch_",
)


def _branch_routes_somewhere(node: Any) -> bool:
    """Does this branch's BODY hand off to a command handler?

    The discriminator between a verb and a coincidence. ``cmd == "ops"``
    and ``raw == "true"`` are the same shape as tests; they differ in what
    they do next — one calls ``self._repl_cmd_ops()``, the other assigns a
    boolean. Asking the body keeps this derived: a denylist of words that
    merely LOOK like verbs would need an entry every time someone wrote a
    new env parse.

    Only the branch's own body is inspected, never nested ``If`` bodies,
    so a sub-command handler inside a verb branch is not mistaken for a
    second verb. NEVER raises.
    """
    try:
        for stmt in getattr(node, "body", ()) or ():
            for sub in ast.walk(stmt):
                if isinstance(sub, ast.If):
                    continue
                if not isinstance(sub, ast.Call):
                    continue
                fn = sub.func
                fname = getattr(fn, "attr", None) or getattr(fn, "id", "")
                if any(str(fname).startswith(p) for p in _HANDLER_SHAPES):
                    return True
        return False
    except Exception:  # noqa: BLE001
        return False


def _verbs_in_test(test: Any) -> Tuple[str, ...]:
    """Verb literals a branch test compares against. Pure. NEVER raises."""
    found = []
    try:
        for node in ast.walk(test):
            subject = None
            literals = []
            if isinstance(node, ast.Compare):
                subject = node.left
                literals = [c for c in node.comparators]
            elif isinstance(node, ast.Call):
                fn = node.func
                if (isinstance(fn, ast.Attribute)
                        and fn.attr in ("startswith", "endswith")):
                    subject = fn.value
                    literals = list(node.args)
            if subject is None:
                continue
            # Unwrap `cmd.strip()` / `cmd.lower()` to reach the name.
            while isinstance(subject, ast.Call) and isinstance(
                    subject.func, ast.Attribute):
                subject = subject.func.value
            if isinstance(subject, ast.Attribute):
                subject = subject.value
            if not (isinstance(subject, ast.Name)
                    and subject.id in _CHAIN_SUBJECTS):
                continue
            for lit in literals:
                if isinstance(lit, (ast.Tuple, ast.List, ast.Set)):
                    lit_iter = lit.elts
                else:
                    lit_iter = [lit]
                for item in lit_iter:
                    if not (isinstance(item, ast.Constant)
                            and isinstance(item.value, str)):
                        continue
                    token = item.value.strip().lstrip("/").split()[0] \
                        if item.value.strip() else ""
                    token = token.lower()
                    if (token and len(token) <= 24
                            and token.replace("_", "").replace(
                                "-", "").isalnum()
                            and not token.isdigit()):
                        found.append(token)
    except Exception:  # noqa: BLE001
        return tuple()
    return tuple(found)


def list_verbs() -> Tuple[str, ...]:
    """Every verb that EXISTS, however it is dispatched. Read-only.

    Discovery and dispatch are different questions, and fusing them cost
    16 verbs their visibility. `_CUSTOM_HANDLER_EXCLUSIONS` says it out
    loud — those verbs "retain their legacy custom handlers" — so the
    exclusion was a routing decision ("do not auto-dispatch this"), and
    reading it as an existence claim erased `/goal`, `/budget`, `/risk`,
    `/plan`, `/undo` and eleven others from `/help`, tab completion, the
    slash palette and the progress board's count.

    An operator cannot use a verb they cannot find. This returns the
    union; :func:`try_dispatch` still consults only the dispatcher map,
    so ROUTING is byte-for-byte unchanged.
    """
    with _REGISTRY_LOCK:
        return tuple(sorted(
            set(_VERB_TO_DISPATCHER.keys()) | set(_EXTERNAL_VERBS.keys())))


def list_dispatchable_verbs() -> Tuple[str, ...]:
    """Only verbs THIS registry routes. The dispatch half of the split."""
    with _REGISTRY_LOCK:
        return tuple(sorted(_VERB_TO_DISPATCHER.keys()))


def external_verbs() -> Dict[str, str]:
    """Verbs handled elsewhere → where. Read-only snapshot.

    The value is a provenance string (``harness.py:4292``, ``custom
    handler``) so `/help` can tell an operator where a verb lives rather
    than implying this registry owns it.
    """
    with _REGISTRY_LOCK:
        return dict(_EXTERNAL_VERBS)


def register_external_verb(verb: object, where: object = "") -> bool:
    """Record that ``verb`` exists and is dispatched somewhere else.

    Discovery only — this NEVER makes the verb routable here, so a
    mis-registration cannot hijack a line. NEVER raises.
    """
    try:
        name = str(verb or "").strip().lstrip("/").lower()
        if not name or not name.replace("_", "").replace("-", "").isalnum():
            return False
        with _REGISTRY_LOCK:
            if name in _VERB_TO_DISPATCHER:
                # Already routable here. Claiming it is also external
                # would make `/help` ambiguous about who owns it.
                return False
            _EXTERNAL_VERBS[name] = str(where or "external handler")[:80]
        return True
    except Exception:  # noqa: BLE001
        return False


def registry_primed() -> bool:
    """Has discovery RUN? Distinct from "are there verbs".

    Added because a consumer could not tell the two apart. `list_verbs()`
    returns an empty tuple both when priming has not happened yet and when it
    happened and found nothing — and the progress board rendered the first case
    as `verbs primed 0`, which reads as "this cockpit has no verbs" rather than
    "nobody has asked yet".

    The alternative was for callers to read `_REGISTRY_PRIMED` directly. That
    is the same private reach-around the risk-tier ladder has an authority
    invariant against, and it would have made this module's internals part of
    its contract by accident.

    Read-only and side-effect-free ON PURPOSE: a status view must be able to
    ask this WITHOUT triggering the import walk that priming performs.
    """
    with _REGISTRY_LOCK:
        return bool(_REGISTRY_PRIMED)


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
    argument. Returns None on accept; reason string on reject.

    Coroutine functions (``async def dispatch_<verb>_command``)
    are first-class here — :func:`inspect.signature` reports the
    same parameter shape for sync and async callables, so no
    ``iscoroutinefunction`` branch is needed to accept them.
    :func:`try_dispatch` is the seam that awaits an awaitable
    result; this validator only cares about the call signature."""
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
            _known = tuple(sorted(
                set(_VERB_TO_DISPATCHER.keys()) | set(_EXTERNAL_VERBS.keys())))
            return RegistryReport(
                verb_count=len(_known),
                verbs=_known,
                excluded=tuple(_CUSTOM_HANDLER_EXCLUSIONS),
                elapsed_s=time.monotonic() - t0,
            )
        if force:
            _VERB_TO_DISPATCHER.clear()
            # Rediscovered below from the same two derived sources. Leaving
            # them would make a forced re-prime cumulative rather than a
            # fresh read.
            _EXTERNAL_VERBS.clear()

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

    def _declared_aliases(mod: Any) -> tuple:
        """Extra verb names a module opts into. NEVER raises.

        Read defensively: a module that sets ``__aliases__`` to a string
        would otherwise register one verb per character."""
        try:
            raw = getattr(mod, "__aliases__", ())
            if isinstance(raw, str) or not isinstance(raw, (tuple, list, set)):
                return ()
            return tuple(str(a).strip() for a in raw if str(a).strip())
        except Exception:  # noqa: BLE001
            return ()

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
        registered = 1

        # ALIAS UNMASKING.
        #
        # Discovery keys on the module BASENAME, so a module may expose only
        # one verb no matter how many dispatchers it defines. `moltbook_repl`
        # defined both `dispatch_moltbook_command` and `dispatch_molt_command`;
        # only the first was ever reachable, and `/molt` sat in the tree with
        # no caller — the wired-but-inert trap, produced by the naming cage
        # rather than by an omission.
        #
        # A module now declares extra verbs with a module-level tuple:
        #
        #     __aliases__ = ("molt",)
        #
        # Each alias binds to its OWN `dispatch_<alias>_command` when the
        # module defines one (distinct behaviour, e.g. posting vs reading),
        # and otherwise to the basename dispatcher (a true synonym). The
        # basename convention is untouched: aliases are additive and opt-in.
        for alias in _declared_aliases(mod):
            if alias in exclusions or not alias.isidentifier():
                continue
            alias_fn = getattr(mod, f"dispatch_{alias}_command", None)
            if not callable(alias_fn):
                alias_fn = fn                       # synonym for the basename
            if _validate_dispatch_signature(alias_fn) is not None:
                continue
            with _REGISTRY_LOCK:
                if alias in _VERB_TO_DISPATCHER:
                    continue
                _VERB_TO_DISPATCHER[alias] = alias_fn
            registered += 1
            logger.debug(
                "[ReplRegistry] alias %r -> %s", alias, full_name,
            )
        return registered

    discover_module_provided_callable(
        packages=pkg_list,
        attr_name=None,  # module-scan mode
        handler=module_handler,
        log_prefix="ReplRegistry",
    )

    # Verbs that EXIST but are routed elsewhere, from two DERIVED sources
    # so neither can drift into a stale hand-written list:
    #
    #  1. the exclusion list itself. Its own comment says these "retain
    #     their legacy custom handlers" — that is a statement that they
    #     exist, and reading it as "these do not exist" is what erased
    #     /goal, /budget, /risk and /plan from every discovery surface.
    #  2. the if/elif command chains, read structurally.
    #
    # Discovery only. `try_dispatch` still consults `_VERB_TO_DISPATCHER`
    # alone, so routing is byte-for-byte what it was.
    try:
        for _verb in exclusions:
            register_external_verb(_verb, "custom handler")
        for _verb, _where in discover_chain_verbs().items():
            register_external_verb(_verb, _where)
    except Exception:  # noqa: BLE001
        logger.debug("[ReplRegistry] external verb discovery degraded",
                     exc_info=True)

    with _REGISTRY_LOCK:
        _REGISTRY_PRIMED = True
        verbs = tuple(sorted(
            set(_VERB_TO_DISPATCHER.keys()) | set(_EXTERNAL_VERBS.keys())))

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


async def try_dispatch(line: str) -> DispatchOutcome:
    """Attempt to dispatch ``line`` through the auto-discovered
    verb→dispatcher map. Returns ``DispatchOutcome(matched=False)``
    if no verb matches; otherwise returns the dispatcher's
    result projected into a frozen ``DispatchOutcome``.

    Master-flag-gated. Idempotently primes the registry on
    first call. NEVER raises out — dispatcher exceptions
    surface as ``DispatchOutcome(matched=True, ok=False,
    text=<reason>)``.

    **async-aware** — ``dispatch_<verb>_command`` callables may
    be plain functions OR coroutine functions. When the call
    returns an awaitable (``inspect.isawaitable``), it is
    awaited here before projecting the result. This is the ONE
    seam where the registry bridges sync and async dispatchers;
    callers of :func:`try_dispatch` MUST ``await`` it (it always
    runs on a live event loop — the REPL's ``_loop`` coroutine)."""
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
                if inspect.isawaitable(result):
                    result = await result
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
    "registry_primed",
    "repl_dispatch_autodiscovery_enabled",
    "reset_registry_for_tests",
    "try_dispatch",
]
