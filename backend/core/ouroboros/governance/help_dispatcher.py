"""/help REPL dispatcher + VerbRegistry — Slice 2 of FlagRegistry arc.

Top-level operator surface enumerating every registered REPL verb and
every registered env flag. Consumes :class:`FlagRegistry` (Slice 1)
for flag introspection and, optionally, :func:`get_current_posture`
from the DirectionInferrer arc for posture-relevance filtering.

Subcommands::

    /help                       top-level index (verbs + flag categories)
    /help verbs                 verb-only list
    /help <verb>                delegate to that verb's own help
    /help flags                 all registered flags
    /help flags --category CAT  filter by category
    /help flags --posture P     filter by posture relevance
    /help flags --search Q      case-insensitive substring
    /help flag <NAME>           full detail for one flag
    /help category <CAT>        alias for /help flags --category CAT
    /help posture [P]           alias for /help flags --posture P (or current)
    /help unregistered          typo-hunter output
    /help stats                 registry metrics rollup
    /help help                  this text

Authority posture
-----------------

* §1 read-only — the dispatcher never mutates flag state. Operators
  change env vars through the OS / shell, not through this surface.
  There is explicitly no ``/help set X=Y``.
* §8 observability — `/help stats` + `/help unregistered` make the
  flag surface auditable without grepping the codebase.
* No imports from ``orchestrator`` / ``policy`` / ``iron_gate`` /
  ``risk_tier`` / ``change_engine`` / ``candidate_generator`` / ``gate``.
  Grep-pinned at Slice 4.

Rendering
---------

Rich tables when a TTY is attached; flat fallback otherwise. Same
pattern as ``/posture explain`` and ``stream_renderer.py``. ``rich``
is a soft dependency — if import fails we go flat-only.
"""
from __future__ import annotations

import logging
import os
import shlex
import sys
import textwrap
import threading
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.HelpDispatcher")


_COMMANDS = frozenset({"/help"})


_HELP = textwrap.dedent(
    """
    /help — operator-facing index of every REPL verb and env flag
    ---------------------------------------------------------------
      /help                       top-level index
      /help verbs                 list all registered REPL verbs
      /help <verb>                that verb's own help (delegates)
      /help flags                 all env flags
      /help flags --category CAT  filter by category
      /help flags --posture P     filter by posture relevance
      /help flags --search Q      substring search on name + description
      /help flag <NAME>           full detail for one flag
      /help category <CAT>        alias for flags --category
      /help posture [P]           alias for flags --posture (current if omitted)
      /help unregistered          typo-hunter: JARVIS_* env vars not registered
      /help stats                 registry metrics rollup
      /help help                  this text

    Requires JARVIS_FLAG_REGISTRY_ENABLED=true for non-help verbs.
    """
).strip()


@dataclass
class HelpDispatchResult:
    ok: bool
    text: str
    matched: bool = True


# ---------------------------------------------------------------------------
# Verb registry — consumed by /help at dispatch time
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerbSpec:
    """Frozen descriptor for one REPL verb.

    ``one_line`` is rendered in the top-level /help index; ``help_text``
    is what ``/help <verb>`` shows. Either the static ``help_text`` is
    used, or ``help_text_fn`` is invoked at render time (useful when
    the verb's help depends on runtime state).

    ``category`` is a free-form string like ``"observability"`` or
    ``"governance"`` — not tied to the FlagRegistry's Category enum,
    since verbs and flags have different categorization concerns.
    """

    name: str
    one_line: str
    category: str = "general"
    help_text: Optional[str] = None
    help_text_fn: Optional[Callable[[], str]] = None
    since: str = "v1.0"

    def resolve_help(self) -> str:
        if self.help_text_fn is not None:
            try:
                return self.help_text_fn()
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[HelpDispatcher] help_text_fn raised for %s", self.name,
                    exc_info=True,
                )
                return self.help_text or f"(no help available for {self.name})"
        return self.help_text or f"(no help available for {self.name})"


class VerbRegistry:
    """Thread-safe registry of REPL verbs."""

    def __init__(self) -> None:
        self._verbs: dict = {}
        self._lock = threading.Lock()

    def register(self, spec: VerbSpec, *, override: bool = True) -> None:
        if not isinstance(spec, VerbSpec):
            raise TypeError(f"expected VerbSpec, got {type(spec).__name__}")
        with self._lock:
            if spec.name in self._verbs and not override:
                raise ValueError(f"verb {spec.name!r} already registered")
            self._verbs[spec.name] = spec

    def bulk_register(self, specs: List[VerbSpec]) -> None:
        for s in specs:
            self.register(s)

    def get(self, name: str) -> Optional[VerbSpec]:
        with self._lock:
            return self._verbs.get(name.strip())

    def list_all(self) -> List[VerbSpec]:
        with self._lock:
            return sorted(self._verbs.values(), key=lambda s: s.name)

    def clear(self) -> None:
        with self._lock:
            self._verbs.clear()


# Module-level singleton
_default_verbs: Optional[VerbRegistry] = None
_default_verbs_lock = threading.Lock()


def get_default_verb_registry() -> VerbRegistry:
    global _default_verbs
    with _default_verbs_lock:
        if _default_verbs is None:
            _default_verbs = VerbRegistry()
            _seed_builtin_verbs(_default_verbs)
            # Module-owned discovery (mirrors flag_registry's
            # _discover_module_provided_flags + shipped_code_invariants's
            # _discover_module_provided_invariants pattern). Modules
            # add a verb co-located with their REPL surface by exposing
            # ``register_verbs(registry) -> int``; the discovery loop
            # finds and invokes it. Adding a new verb requires zero
            # edits to this seed function.
            _discover_module_provided_verbs(_default_verbs)
        return _default_verbs


def reset_default_verb_registry() -> None:
    global _default_verbs
    with _default_verbs_lock:
        _default_verbs = None


def _seed_builtin_verbs(registry: VerbRegistry) -> None:
    """Pre-register the verbs we already ship. Light-touch — no
    changes to the REPL modules themselves; we just describe their
    surface here so ``/help`` can enumerate them.
    """
    registry.bulk_register([
        VerbSpec(
            name="/help",
            one_line="List REPL verbs + env flags (this surface).",
            category="observability",
            help_text=_HELP,
        ),
        VerbSpec(
            name="/posture",
            one_line="Inspect or override the inferred strategic posture.",
            category="governance",
            help_text=(
                "/posture {status|explain|history|signals|override|clear-override|help}\n"
                "See DirectionInferrer (Wave 1 #1). Master flag: "
                "JARVIS_DIRECTION_INFERRER_ENABLED."
            ),
        ),
        VerbSpec(
            name="/recover",
            one_line="Render 'three things to try next' guidance for a failed op.",
            category="governance",
            help_text=(
                "/recover [<op-id> [speak] | session <sid> | help]\n"
                "Deterministic rule-based advisor. Voice output requires "
                "OUROBOROS_NARRATOR_ENABLED + JARVIS_RECOVERY_VOICE_ENABLED."
            ),
        ),
        VerbSpec(
            name="/session",
            one_line="Browse past Ouroboros sessions (read-only index).",
            category="observability",
            help_text=(
                "/session {list|show <sid>|bookmark <sid>|pin <sid>|help}\n"
                "Read-only over .ouroboros/sessions/*."
            ),
        ),
        VerbSpec(
            name="/cost",
            one_line="Per-phase cost drill-down for the current session.",
            category="observability",
            help_text=(
                "/cost {summary|phase <phase>|op <op-id>|session <sid>|help}\n"
                "Instrumentation-only; budget-cap behavior unchanged."
            ),
        ),
        VerbSpec(
            name="/plan",
            one_line="Halt-for-review operator modality for complex ops.",
            category="governance",
            help_text=(
                "/plan {mode|pending|show|approve|reject|history|help}\n"
                "Deliberately default-off — JARVIS_PLAN_APPROVAL_MODE halts "
                "every op when true."
            ),
        ),
        VerbSpec(
            name="/layout",
            one_line="Toggle SerpentFlow flowing vs split-pane TUI layout.",
            category="observability",
            help_text=(
                "/layout {flowing|split|focus <op-id>|help}\n"
                "Default flowing. Split/focus opt-in via --split CLI or verb."
            ),
        ),
    ])


# ---------------------------------------------------------------------------
# Module-owned verb discovery (mirrors flag_registry / shipped_code_invariants
# discovery pattern). Modules co-locate verb declarations with their REPL
# surface; the loop finds them at first ``get_default_verb_registry`` call.
# ---------------------------------------------------------------------------


# Curated list of provider PACKAGES whose direct submodules may
# contribute verbs via ``register_verbs(registry)``. Adding a verb
# inside an existing module requires zero edits here. Adding a verb
# in a NEW package requires one entry. Same architectural pattern
# as flag_registry_seed._FLAG_PROVIDER_PACKAGES.
_VERB_PROVIDER_PACKAGES: tuple = (
    "backend.core.ouroboros.governance",
    "backend.core.ouroboros.battle_test",
)


def _discover_module_provided_verbs(registry: VerbRegistry) -> int:
    """Walk every package in ``_VERB_PROVIDER_PACKAGES`` for direct
    submodules exposing ``register_verbs(registry) -> int``.

    Each matching module installs its own VerbSpecs into the registry
    + returns the count installed. Per-module failures are logged
    and skipped — boot is never blocked by one misconfigured module.

    Architecture: instead of editing this file every time a new REPL
    surface ships, the consuming module declares its verb co-located
    with its dispatch logic (``render_repl.register_verbs``,
    ``posture_repl.register_verbs``, etc.). Same auto-discovery
    mechanism that already protects FlagRegistry + ShippedCodeInvariants
    from drift.

    NEVER raises. Returns total verbs registered."""
    discovered = 0
    try:
        from importlib import import_module
        import pkgutil
        for pkg_name in _VERB_PROVIDER_PACKAGES:
            try:
                pkg_mod = import_module(pkg_name)
                pkg_path = getattr(pkg_mod, "__path__", None)
                if not pkg_path:
                    continue
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.debug(
                    "[HelpDispatcher] verb provider package %s "
                    "unavailable: %s", pkg_name, exc,
                )
                continue
            for _, name, _ispkg in pkgutil.iter_modules(pkg_path):
                full_name = f"{pkg_name}.{name}"
                if full_name == __name__:
                    continue
                try:
                    mod = import_module(full_name)
                    fn = getattr(mod, "register_verbs", None)
                    if not callable(fn):
                        continue
                    count = fn(registry)
                    if isinstance(count, int) and count > 0:
                        discovered += count
                        logger.debug(
                            "[HelpDispatcher] %s registered %d verb(s)",
                            full_name, count,
                        )
                except Exception as exc:  # noqa: BLE001 — defensive
                    logger.debug(
                        "[HelpDispatcher] verb discovery skipped %s: %s",
                        full_name, exc,
                    )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[HelpDispatcher] _discover_module_provided_verbs exc: %s",
            exc,
        )
    return discovered


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _master_enabled() -> bool:
    """Master kill switch inherited from FlagRegistry arc."""
    try:
        from backend.core.ouroboros.governance.flag_registry import is_enabled
    except ImportError:
        return False
    return is_enabled()


def dispatcher_enabled() -> bool:
    """Sub-gate for the /help dispatcher surface specifically."""
    if not _master_enabled():
        return False
    return _env_bool("JARVIS_HELP_DISPATCHER_ENABLED", True)


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _is_tty() -> bool:
    try:
        return bool(sys.stdout.isatty())
    except Exception:  # noqa: BLE001
        return False


def _matches(line: str) -> bool:
    if not line:
        return False
    return line.split(None, 1)[0] in _COMMANDS


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def dispatch_help_command(
    line: str,
    *,
    flag_registry: Any = None,
    verb_registry: Optional[VerbRegistry] = None,
    current_posture_fn: Optional[Callable[[], Optional[Any]]] = None,
) -> HelpDispatchResult:
    """Parse a ``/help`` line and dispatch.

    ``flag_registry`` defaults to ``ensure_seeded()`` from flag_registry.
    ``verb_registry`` defaults to the module singleton.
    ``current_posture_fn`` optional — returns the current Posture for
    ``/help posture`` without an explicit argument; tests inject stubs.
    """
    if not _matches(line):
        return HelpDispatchResult(ok=False, text="", matched=False)
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return HelpDispatchResult(ok=False, text=f"  /help parse error: {exc}")
    if not tokens:
        return HelpDispatchResult(ok=False, text="", matched=False)

    args = tokens[1:]
    head = (args[0].lower() if args else "").strip()

    # Resolve registries lazily
    if flag_registry is None:
        try:
            from backend.core.ouroboros.governance.flag_registry import ensure_seeded
            flag_registry = ensure_seeded()
        except ImportError:
            flag_registry = None
    if verb_registry is None:
        verb_registry = get_default_verb_registry()

    # /help help is ALWAYS available — operators must be able to discover
    # the flag name even in the master-off state.
    if head in ("help", "?"):
        return HelpDispatchResult(ok=True, text=_HELP)

    if not dispatcher_enabled():
        return HelpDispatchResult(
            ok=False,
            text=(
                "  /help: FlagRegistry dispatcher disabled — set "
                "JARVIS_FLAG_REGISTRY_ENABLED=true"
            ),
        )

    if flag_registry is None:
        return HelpDispatchResult(
            ok=False, text="  /help: FlagRegistry unavailable",
        )

    if not head:
        return _top_index(flag_registry, verb_registry)

    if head == "verbs":
        return _list_verbs(verb_registry)

    if head == "flags":
        return _list_flags(flag_registry, args[1:])

    if head == "flag":
        if len(args) < 2:
            return HelpDispatchResult(
                ok=False, text="  /help flag <NAME>",
            )
        return _flag_detail(flag_registry, args[1])

    if head == "category":
        if len(args) < 2:
            return HelpDispatchResult(
                ok=False, text="  /help category <CAT>",
            )
        return _list_flags(flag_registry, ["--category", args[1]])

    if head == "posture":
        posture_arg = args[1] if len(args) >= 2 else None
        return _list_by_posture(
            flag_registry, posture_arg, current_posture_fn,
        )

    if head == "unregistered":
        return _unregistered(flag_registry)

    if head == "stats":
        return _stats(flag_registry, verb_registry)

    # Otherwise: treat args[0] as a verb name and delegate
    verb_name = args[0] if args[0].startswith("/") else f"/{args[0]}"
    spec = verb_registry.get(verb_name)
    if spec is not None:
        return HelpDispatchResult(ok=True, text=spec.resolve_help())

    return HelpDispatchResult(
        ok=False,
        text=f"  /help: unknown subcommand {head!r}. Try /help help.",
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _top_index(flag_registry: Any, verb_registry: VerbRegistry) -> HelpDispatchResult:
    verbs = verb_registry.list_all()
    stats = flag_registry.stats()
    lines: List[str] = []
    lines.append("  JARVIS Ouroboros — operator surface")
    lines.append("  " + "-" * 40)
    lines.append(f"  {len(verbs)} REPL verbs registered:")
    for v in verbs:
        lines.append(f"    {v.name:<12s}  {v.one_line}")
    lines.append("")
    lines.append(f"  {stats['total']} env flags registered ({stats['by_category']}):")
    lines.append("  Run /help flags [--category X | --posture P | --search Q]")
    lines.append("  Run /help flag <NAME> for one flag's full detail")
    lines.append("  Run /help unregistered to surface possible typos")
    lines.append("  Run /help stats for rollups")
    return HelpDispatchResult(ok=True, text="\n".join(lines))


def _list_verbs(verb_registry: VerbRegistry) -> HelpDispatchResult:
    verbs = verb_registry.list_all()
    lines = [f"  {len(verbs)} REPL verbs:"]
    for v in verbs:
        lines.append(f"    {v.name:<12s}  [{v.category}]  {v.one_line}")
    return HelpDispatchResult(ok=True, text="\n".join(lines))


def _list_flags(flag_registry: Any, args: List[str]) -> HelpDispatchResult:
    category_filter: Optional[str] = None
    posture_filter: Optional[str] = None
    search_query: Optional[str] = None

    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--category" and i + 1 < len(args):
            category_filter = args[i + 1]
            i += 2
        elif tok == "--posture" and i + 1 < len(args):
            posture_filter = args[i + 1]
            i += 2
        elif tok == "--search" and i + 1 < len(args):
            search_query = args[i + 1]
            i += 2
        else:
            return HelpDispatchResult(
                ok=False, text=f"  /help flags: unknown arg {tok!r}",
            )

    # Resolve category filter
    specs: List[Any] = []
    if category_filter:
        from backend.core.ouroboros.governance.flag_registry import Category
        try:
            cat = Category(category_filter.strip().lower())
        except ValueError:
            valid = [c.value for c in Category]
            return HelpDispatchResult(
                ok=False,
                text=(
                    f"  /help flags: unknown category "
                    f"{category_filter!r}. Valid: {valid}"
                ),
            )
        specs = flag_registry.list_by_category(cat)
    elif posture_filter:
        specs = flag_registry.relevant_to_posture(posture_filter)
    elif search_query:
        specs = flag_registry.find(search_query)
    else:
        specs = flag_registry.list_all()

    if not specs:
        return HelpDispatchResult(
            ok=True, text="  (no flags match the filter)",
        )

    lines = [f"  {len(specs)} flag(s):"]
    for s in specs:
        lines.append(
            f"    {s.name:<52s}  [{s.category.value}/{s.type.value}]"
            f"  default={s.default!r}"
        )
    return HelpDispatchResult(ok=True, text="\n".join(lines))


def _flag_detail(flag_registry: Any, name: str) -> HelpDispatchResult:
    spec = flag_registry.get_spec(name)
    if spec is None:
        suggestions = flag_registry.suggest_similar(name)
        suggestion_text = ""
        if suggestions:
            suggestion_text = (
                "\n  Did you mean: "
                + ", ".join(s[0] for s in suggestions)
            )
        return HelpDispatchResult(
            ok=False,
            text=f"  /help flag: {name!r} not registered.{suggestion_text}",
        )
    lines = [
        f"  {spec.name}",
        f"    type         : {spec.type.value}",
        f"    default      : {spec.default!r}",
        f"    category     : {spec.category.value}",
        f"    since        : {spec.since}",
        f"    source_file  : {spec.source_file}",
    ]
    if spec.example is not None:
        lines.append(f"    example      : {spec.example}")
    if spec.posture_relevance:
        rel = ", ".join(
            f"{p}={r.value}" for p, r in spec.posture_relevance.items()
        )
        lines.append(f"    posture      : {rel}")
    if spec.aliases:
        lines.append(f"    aliases      : {', '.join(spec.aliases)}")
    lines.append("")
    lines.append(f"    {spec.description}")
    # Current env value if set
    raw = os.environ.get(spec.name)
    if raw is not None:
        lines.append(f"    (currently set in env: {raw!r})")
    return HelpDispatchResult(ok=True, text="\n".join(lines))


def _list_by_posture(
    flag_registry: Any,
    posture_arg: Optional[str],
    current_posture_fn: Optional[Callable[[], Optional[Any]]],
) -> HelpDispatchResult:
    posture: Optional[str] = None
    if posture_arg:
        posture = posture_arg.strip().upper()
    elif current_posture_fn is not None:
        try:
            current = current_posture_fn()
            if current is not None:
                # Posture enum's .value attribute or str coercion
                posture = getattr(current, "value", None) or str(current)
                posture = posture.strip().upper()
        except Exception:  # noqa: BLE001
            posture = None
    else:
        # Fall back to the DirectionInferrer default if posture arc is loaded
        try:
            from backend.core.ouroboros.governance.posture_observer import (
                get_default_store,
            )
            current = get_default_store().load_current()
            if current is not None:
                posture = current.posture.value
        except Exception:  # noqa: BLE001
            posture = None

    if posture is None:
        return HelpDispatchResult(
            ok=False,
            text=(
                "  /help posture: no posture argument and none inferable. "
                "Try /help posture HARDEN."
            ),
        )

    specs = flag_registry.relevant_to_posture(posture)
    if not specs:
        return HelpDispatchResult(
            ok=True,
            text=f"  (no flags tagged relevant to posture {posture})",
        )
    lines = [f"  {len(specs)} flag(s) relevant to posture {posture}:"]
    for s in specs:
        rel = s.posture_relevance.get(posture)
        rel_str = rel.value if rel is not None else "relevant"
        lines.append(
            f"    [{rel_str:<8s}]  {s.name:<52s}  "
            f"default={s.default!r}"
        )
    return HelpDispatchResult(ok=True, text="\n".join(lines))


def _unregistered(flag_registry: Any) -> HelpDispatchResult:
    unreg = flag_registry.unregistered_env()
    if not unreg:
        return HelpDispatchResult(
            ok=True, text="  (no unregistered JARVIS_* env vars detected)",
        )
    lines = [f"  {len(unreg)} unregistered JARVIS_* env var(s):"]
    for name, suggestions in unreg:
        if suggestions:
            top = suggestions[0]
            lines.append(
                f"    {name}  → closest match: {top[0]} "
                f"(Levenshtein distance {top[1]})"
            )
        else:
            lines.append(f"    {name}  → no close match (possibly a new flag)")
    return HelpDispatchResult(ok=True, text="\n".join(lines))


def _stats(flag_registry: Any, verb_registry: VerbRegistry) -> HelpDispatchResult:
    stats = flag_registry.stats()
    verb_count = len(verb_registry.list_all())
    lines = [
        "  FlagRegistry stats:",
        f"    schema_version : {stats['schema_version']}",
        f"    total_flags    : {stats['total']}",
        f"    by_category    : {stats['by_category']}",
        f"    by_type        : {stats['by_type']}",
        f"    read_count     : {stats['read_count']}",
        f"    typos_reported : {stats['reported_typos']}",
        f"    verbs_registered: {verb_count}",
    ]
    return HelpDispatchResult(ok=True, text="\n".join(lines))


__all__ = [
    "HelpDispatchResult",
    "VerbRegistry",
    "VerbSpec",
    "dispatch_help_command",
    "dispatcher_enabled",
    "get_default_verb_registry",
    "reset_default_verb_registry",
]
