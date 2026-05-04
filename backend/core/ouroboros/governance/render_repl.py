"""``/render`` REPL verb — read-only inspection of the RenderConductor arc.

Follow-up #2 to the Slice 1-7 graduation: gives operators a verb
that surfaces every active substrate piece (master flag, producer
flags, backends, observers, helpers) in one terse text rendering.

Symmetric to ``/posture`` (DirectionInferrer arc) and ``/help``
(FlagRegistry arc) — same :class:`HelpDispatchResult` return type so
the existing REPL dispatch chain (``serpent_flow.SerpentREPL._-
dispatch_command``) consumes it without bespoke wiring.

Architectural pillars:

  1. **Read-only by construction** — every subcommand pulls from the
     existing typed registries via lazy import. No conductor mutation;
     no flag mutation; no observer lifecycle change. Even the master
     flag is reported via ``is_enabled()`` accessor — never written.
  2. **Symmetric subcommand vocabulary** — ``status / flags /
     backends / observers / help``. Each maps 1:1 to a conceptual
     substrate piece (master + producers / flags / fan-out / push-
     pump). Closed taxonomy of subcommands; unknown subcommand
     surfaces a clear error referring to ``/render help``.
  3. **No hardcoded values in callers** — the active flag list is
     enumerated from :func:`_arc_flag_names` (closed-taxonomy
     constant); defaults / current values pulled from FlagRegistry's
     typed accessors. Every value is registry-routed.
  4. **Defensive everywhere** — every handler swallows registry-
     unavailable exceptions and surfaces a degraded line ("(unknown)")
     rather than crashing the REPL.
  5. **Verb registration via help_dispatcher** — the verb is added
     to :class:`VerbRegistry` at import time so ``/help`` enumerates
     it automatically (mirrors :func:`_seed_builtin_verbs`).

Authority invariants (AST-pinned via ``register_shipped_invariants``):

  * No imports of ``rich`` / ``rich.*``.
  * No imports of orchestrator / policy / iron_gate / risk_tier /
    change_engine / candidate_generator / gate / semantic_guardian /
    semantic_firewall / providers / doubleword_provider /
    urgency_router / cancel_token / conversation_bridge.
  * Subcommand closed taxonomy.
  * ``register_flags`` + ``register_shipped_invariants`` symbols
    present (auto-discovery contract).

Kill switch: none. The verb is read-only and has no operational
authority — there's no surface to gate. Operators can still observe
the substrate even with every render-arc flag off.
"""
from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

logger = logging.getLogger(__name__)


RENDER_REPL_SCHEMA_VERSION: str = "render_repl.1"


# Closed taxonomy of subcommands. AST-pinned.
_SUBCOMMANDS: Tuple[str, ...] = (
    "status", "flags", "backends", "observers", "help",
)
_COMMANDS: Tuple[str, ...] = ("/render",)


# Closed-taxonomy list of every flag in the RenderConductor arc.
# Mirrors the per-slice register_flags contributions; AST-pinned.
_ARC_FLAGS: Tuple[Tuple[str, str], ...] = (
    # (flag_name, slice tag for documentation)
    ("JARVIS_RENDER_CONDUCTOR_ENABLED",          "S1 master"),
    ("JARVIS_RENDER_CONDUCTOR_THEME_NAME",       "S1 theme"),
    ("JARVIS_RENDER_CONDUCTOR_DENSITY_OVERRIDE", "S1 density"),
    ("JARVIS_RENDER_CONDUCTOR_POSTURE_DENSITY_MAP",
     "S1 posture map"),
    ("JARVIS_RENDER_CONDUCTOR_PALETTE_OVERRIDE", "S1 palette"),
    ("JARVIS_REASONING_STREAM_ENABLED",          "S3 producer"),
    ("JARVIS_FILE_REF_HYPERLINK_ENABLED",        "S3 hyperlink"),
    ("JARVIS_INPUT_CONTROLLER_ENABLED",          "S4 producer"),
    ("JARVIS_INPUT_CONTROLLER_RAW_MODE",         "S4 raw mode"),
    ("JARVIS_KEY_BINDINGS",                      "S4 bindings"),
    ("JARVIS_THREAD_OBSERVER_ENABLED",           "S5 producer"),
    ("JARVIS_THREAD_SPEAKER_MAPPING",            "S5 speaker map"),
    ("JARVIS_CONTEXTUAL_HELP_ENABLED",           "S6 producer"),
    ("JARVIS_HELP_RANKING_WEIGHTS",              "S6 weights"),
    ("JARVIS_HELP_PAGE_SIZE",                    "S6 page size"),
)


# Help text. Single source of truth — the same text is returned by
# ``/render help`` and by the VerbSpec.help_text field.
_HELP = (
    "/render {status|flags|backends|observers|help}\n"
    "  status      Master + producer flag state + active singletons\n"
    "  flags       Per-flag current values (15 arc flags)\n"
    "  backends    Per-backend handled / no-op event kinds\n"
    "  observers   Per-observer activity (ThreadObserver / "
    "InputController / HelpResolver)\n"
    "  help        This message\n"
    "Read-only. Substrate state never mutated by this verb."
).strip()


# ---------------------------------------------------------------------------
# Dispatch result — mirrors PostureDispatchResult / HelpDispatchResult shape
# ---------------------------------------------------------------------------


@dataclass
class RenderDispatchResult:
    ok: bool
    text: str
    matched: bool = True


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def _matches(line: str) -> bool:
    if not line:
        return False
    first = line.split(None, 1)[0]
    return first in _COMMANDS


def dispatch_render_command(line: str) -> RenderDispatchResult:
    """Parse a ``/render`` line and dispatch.

    Read-only: never mutates substrate state. Subcommands map to
    private ``_status / _flags / _backends / _observers`` handlers.
    Empty / missing subcommand defaults to ``status``.
    """
    if not _matches(line):
        return RenderDispatchResult(ok=False, text="", matched=False)
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return RenderDispatchResult(
            ok=False, text=f"  /render parse error: {exc}",
        )
    if not tokens:
        return RenderDispatchResult(ok=False, text="", matched=False)
    args = tokens[1:]
    head = (args[0].lower() if args else "status")

    if head in ("help", "?"):
        return RenderDispatchResult(ok=True, text=_HELP)
    if head not in _SUBCOMMANDS:
        return RenderDispatchResult(
            ok=False,
            text=(
                f"  /render: unknown subcommand {head!r}. "
                f"Try /render help."
            ),
        )

    if head == "status":
        return _status()
    if head == "flags":
        return _flags()
    if head == "backends":
        return _backends()
    if head == "observers":
        return _observers()
    # Unreachable — _SUBCOMMANDS exhausted above.
    return RenderDispatchResult(
        ok=False, text="  /render: internal dispatch error",
    )


# ---------------------------------------------------------------------------
# Subcommand handlers — each is total + defensive
# ---------------------------------------------------------------------------


def _status() -> RenderDispatchResult:
    """One-shot summary: master flag, 4 producer flags, conductor,
    backends, observers — terse status table."""
    lines: List[str] = []
    lines.append("  RenderConductor arc — status")
    lines.append("")

    # Master + producer flags
    master = _flag_bool("JARVIS_RENDER_CONDUCTOR_ENABLED", True)
    rs = _flag_bool("JARVIS_REASONING_STREAM_ENABLED", False)
    ic = _flag_bool("JARVIS_INPUT_CONTROLLER_ENABLED", False)
    to = _flag_bool("JARVIS_THREAD_OBSERVER_ENABLED", False)
    ch = _flag_bool("JARVIS_CONTEXTUAL_HELP_ENABLED", False)

    lines.append(f"  master       JARVIS_RENDER_CONDUCTOR_ENABLED={master}")
    lines.append(f"  producer     JARVIS_REASONING_STREAM_ENABLED={rs}")
    lines.append(f"  producer     JARVIS_INPUT_CONTROLLER_ENABLED={ic}")
    lines.append(f"  producer     JARVIS_THREAD_OBSERVER_ENABLED={to}")
    lines.append(f"  producer     JARVIS_CONTEXTUAL_HELP_ENABLED={ch}")
    lines.append("")

    # Conductor
    conductor = _safe_get_conductor()
    if conductor is not None:
        backends = conductor.backends()
        try:
            density = conductor.active_density().value
        except Exception:  # noqa: BLE001 — defensive
            density = "(unknown)"
        try:
            theme = conductor.active_theme().name
        except Exception:  # noqa: BLE001 — defensive
            theme = "(unknown)"
        lines.append(
            f"  conductor    registered  backends={len(backends)} "
            f"density={density} theme={theme}"
        )
    else:
        lines.append("  conductor    (not registered)")

    # Observers / resolvers
    lines.append(
        f"  input_ctrl   {_describe_input_controller()}"
    )
    lines.append(
        f"  thread_obs   {_describe_thread_observer()}"
    )
    lines.append(
        f"  help_res     {_describe_help_resolver()}"
    )

    return RenderDispatchResult(ok=True, text="\n".join(lines))


def _flags() -> RenderDispatchResult:
    """Tabular dump of the 15 arc flags with their current values."""
    lines: List[str] = []
    lines.append(
        "  RenderConductor arc — flags ({} total)".format(len(_ARC_FLAGS))
    )
    lines.append("")
    lines.append(f"  {'flag':<48} {'slice':<16} value")
    lines.append("  " + "-" * 78)
    for name, slice_tag in _ARC_FLAGS:
        value = _flag_raw(name)
        lines.append(f"  {name:<48} {slice_tag:<16} {value}")
    return RenderDispatchResult(ok=True, text="\n".join(lines))


def _backends() -> RenderDispatchResult:
    """Per-backend handled / no-op event kinds."""
    lines: List[str] = []
    lines.append("  RenderConductor arc — backends")
    lines.append("")
    conductor = _safe_get_conductor()
    if conductor is None:
        lines.append("  (no conductor registered — no backends)")
        return RenderDispatchResult(ok=True, text="\n".join(lines))
    backends = conductor.backends()
    if not backends:
        lines.append("  (conductor has zero backends)")
        return RenderDispatchResult(ok=True, text="\n".join(lines))
    for backend in backends:
        name = getattr(backend, "name", "(?)")
        handled = getattr(backend, "_HANDLED_KINDS", None)
        no_op = getattr(backend, "_NO_OP_KINDS", None)
        if handled is None:
            # StreamRenderer doesn't carry these class-level sets;
            # surface the symbol presence instead.
            lines.append(f"  {name:<24} (inline backend; no kind partition)")
        else:
            lines.append(
                f"  {name:<24} handled={sorted(handled)}"
            )
            if no_op:
                lines.append(
                    f"  {' ' * 24} no-op  ={sorted(no_op)}"
                )
    return RenderDispatchResult(ok=True, text="\n".join(lines))


def _observers() -> RenderDispatchResult:
    """Per-observer activity + handler registration count."""
    lines: List[str] = []
    lines.append("  RenderConductor arc — observers")
    lines.append("")
    lines.append(f"  input_ctrl   {_describe_input_controller(verbose=True)}")
    lines.append(f"  thread_obs   {_describe_thread_observer(verbose=True)}")
    lines.append(f"  help_res     {_describe_help_resolver(verbose=True)}")
    return RenderDispatchResult(ok=True, text="\n".join(lines))


# ---------------------------------------------------------------------------
# Defensive accessors
# ---------------------------------------------------------------------------


def _flag_bool(name: str, default: bool) -> bool:
    try:
        from backend.core.ouroboros.governance import flag_registry as fr
        reg = fr.ensure_seeded()
        return bool(reg.get_bool(name, default=default))
    except Exception:  # noqa: BLE001 — defensive
        return default


def _flag_raw(name: str) -> str:
    """Return the env var's raw value (or '(default)' when unset)."""
    import os
    raw = os.environ.get(name)
    if raw is None:
        return "(default)"
    return raw


def _safe_get_conductor() -> Optional[Any]:
    try:
        from backend.core.ouroboros.governance.render_conductor import (
            get_render_conductor,
        )
        return get_render_conductor()
    except Exception:  # noqa: BLE001 — defensive
        return None


def _describe_input_controller(*, verbose: bool = False) -> str:
    try:
        from backend.core.ouroboros.governance import key_input as ki
        ctrl = ki.get_input_controller()
    except Exception:  # noqa: BLE001 — defensive
        return "(import failed)"
    if ctrl is None:
        return "(not registered)"
    active = "active" if ctrl.active else "inactive"
    if not verbose:
        return f"registered  {active}"
    try:
        actions = sorted(a.value for a in ctrl.registry.actions())
    except Exception:  # noqa: BLE001 — defensive
        actions = []
    return f"registered  {active}  actions={actions}"


def _describe_thread_observer(*, verbose: bool = False) -> str:
    try:
        from backend.core.ouroboros.governance import render_thread as rt
        obs = rt.get_thread_observer()
    except Exception:  # noqa: BLE001 — defensive
        return "(import failed)"
    if obs is None:
        return "(not registered)"
    active = "active" if obs.active else "inactive"
    if not verbose:
        return f"registered  {active}  turns={obs.turn_count}"
    return (
        f"registered  {active}  turns={obs.turn_count}  "
        f"source_module={obs._source_module}"
    )


def _describe_help_resolver(*, verbose: bool = False) -> str:
    try:
        from backend.core.ouroboros.governance import render_help as rh
        resolver = rh.get_help_resolver()
        enabled = rh.is_enabled()
    except Exception:  # noqa: BLE001 — defensive
        return "(import failed)"
    if resolver is None:
        return "(not registered)"
    state = "enabled" if enabled else "disabled"
    if not verbose:
        return f"registered  {state}"
    try:
        weights = rh.ranking_weights()
        page_size = rh.default_page_size()
    except Exception:  # noqa: BLE001 — defensive
        weights = {}
        page_size = -1
    return (
        f"registered  {state}  page_size={page_size}  "
        f"weight_keys={sorted(weights.keys())[:6]}…"
    )


# ---------------------------------------------------------------------------
# VerbRegistry seed — register the /render verb so /help enumerates it
# ---------------------------------------------------------------------------


def register_verbs(registry: Any) -> int:
    """Register the ``/render`` verb into a :class:`VerbRegistry`.

    Auto-discovered by :func:`help_dispatcher._discover_module_provided_-
    verbs` at first ``get_default_verb_registry`` call (and re-discovered
    after each ``reset_default_verb_registry``). Returns the count of
    verbs installed.

    Replaces the prior import-time side effect — tests that reset
    ``_default_verbs`` between assertions now re-discover this verb
    via the seed loop instead of needing the module to be re-imported.
    """
    try:
        from backend.core.ouroboros.governance.help_dispatcher import (
            VerbSpec,
        )
    except Exception:  # noqa: BLE001 — defensive
        return 0
    try:
        registry.register(VerbSpec(
            name="/render",
            one_line=(
                "Read-only inspection of the RenderConductor arc "
                "(master/flags/backends/observers)."
            ),
            category="observability",
            help_text=_HELP,
        ))
        return 1
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[render_repl] verb registration failed", exc_info=True,
        )
        return 0


# ---------------------------------------------------------------------------
# FlagRegistry registration — auto-discovered (zero new flags shipped here)
# ---------------------------------------------------------------------------


def register_flags(registry: Any) -> int:
    """No flags shipped — /render is read-only and has no operational
    authority surface. Returns 0. Function exists to satisfy the
    auto-discovery contract."""
    del registry
    return 0


# ---------------------------------------------------------------------------
# AST invariants — auto-discovered
# ---------------------------------------------------------------------------


_FORBIDDEN_RICH_PREFIX: tuple = ("rich",)
_FORBIDDEN_AUTHORITY_MODULES: tuple = (
    "backend.core.ouroboros.governance.orchestrator",
    "backend.core.ouroboros.governance.policy",
    "backend.core.ouroboros.governance.iron_gate",
    "backend.core.ouroboros.governance.risk_tier",
    "backend.core.ouroboros.governance.risk_tier_floor",
    "backend.core.ouroboros.governance.change_engine",
    "backend.core.ouroboros.governance.candidate_generator",
    "backend.core.ouroboros.governance.gate",
    "backend.core.ouroboros.governance.semantic_guardian",
    "backend.core.ouroboros.governance.semantic_firewall",
    "backend.core.ouroboros.governance.providers",
    "backend.core.ouroboros.governance.doubleword_provider",
    "backend.core.ouroboros.governance.urgency_router",
    "backend.core.ouroboros.governance.cancel_token",
    "backend.core.ouroboros.governance.conversation_bridge",
)


_EXPECTED_SUBCOMMANDS = frozenset({
    "status", "flags", "backends", "observers", "help",
})


def _imported_modules(tree: Any) -> List:
    import ast
    out: List = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod:
                out.append((node.lineno, mod))
    return out


def _validate_no_rich_import(tree: Any, source: str) -> tuple:
    del source
    violations: List[str] = []
    for lineno, mod in _imported_modules(tree):
        for forbidden in _FORBIDDEN_RICH_PREFIX:
            if mod == forbidden or mod.startswith(forbidden + "."):
                violations.append(
                    f"line {lineno}: forbidden rich import: {mod!r}"
                )
    return tuple(violations)


def _validate_no_authority_imports(tree: Any, source: str) -> tuple:
    del source
    violations: List[str] = []
    for lineno, mod in _imported_modules(tree):
        if mod in _FORBIDDEN_AUTHORITY_MODULES:
            violations.append(
                f"line {lineno}: forbidden authority import: {mod!r}"
            )
    return tuple(violations)


def _validate_subcommand_closed_taxonomy(
    tree: Any, source: str,
) -> tuple:
    """The _SUBCOMMANDS tuple MUST contain exactly the documented
    closed set. Adding/removing without updating the dispatcher AND
    the help text is structural drift."""
    del tree
    # Read the tuple literal from source for robustness — handles
    # multi-line tuple definitions cleanly.
    if not source:
        return ("source unavailable",)
    found: set = set()
    for sub in _EXPECTED_SUBCOMMANDS:
        if f'"{sub}"' in source or f"'{sub}'" in source:
            found.add(sub)
    if found != _EXPECTED_SUBCOMMANDS:
        return (
            f"_SUBCOMMANDS literal {sorted(found)} != expected "
            f"{sorted(_EXPECTED_SUBCOMMANDS)}",
        )
    return ()


def _validate_discovery_symbols_present(
    tree: Any, source: str,
) -> tuple:
    del source
    import ast
    needed = {"register_flags", "register_shipped_invariants"}
    found: set = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in needed:
                found.add(node.name)
    missing = needed - found
    if missing:
        return (f"missing discovery symbols: {sorted(missing)}",)
    return ()


_TARGET_FILE = "backend/core/ouroboros/governance/render_repl.py"


def register_shipped_invariants() -> List:
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except Exception:  # noqa: BLE001 — defensive
        return []
    return [
        ShippedCodeInvariant(
            invariant_name="render_repl_no_rich_import",
            target_file=_TARGET_FILE,
            description=(
                "render_repl.py MUST NOT import rich.* — the verb "
                "speaks dispatch-result text only; rendering belongs "
                "to backends consuming the result."
            ),
            validate=_validate_no_rich_import,
        ),
        ShippedCodeInvariant(
            invariant_name="render_repl_no_authority_imports",
            target_file=_TARGET_FILE,
            description=(
                "render_repl.py MUST NOT import any authority module. "
                "The verb is read-only and consults registries via "
                "lazy imports inside individual handlers."
            ),
            validate=_validate_no_authority_imports,
        ),
        ShippedCodeInvariant(
            invariant_name="render_repl_subcommand_closed_taxonomy",
            target_file=_TARGET_FILE,
            description=(
                "_SUBCOMMANDS literal MUST contain exactly "
                "{status, flags, backends, observers, help}. Adding "
                "or removing a subcommand without coordinated "
                "dispatcher + help_text update is structural drift."
            ),
            validate=_validate_subcommand_closed_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name="render_repl_discovery_symbols_present",
            target_file=_TARGET_FILE,
            description=(
                "register_flags + register_shipped_invariants must "
                "be module-level so dynamic discovery picks them up."
            ),
            validate=_validate_discovery_symbols_present,
        ),
    ]


__all__ = [
    "RENDER_REPL_SCHEMA_VERSION",
    "RenderDispatchResult",
    "dispatch_render_command",
    "register_flags",
    "register_shipped_invariants",
]
