"""Did the value handed across that call boundary actually get USED?

`surface_reachability` states this module's reason for existing, in its own
docstring, as the limit of what it can see:

    `transcript_hatches` is reached from `ov.py` alone and is nonetheless LIVE
    on the cockpit, because `ov.py` installs its bindings into the
    `extra_key_bindings` the cockpit is built with. **Reachability sees
    imports; it cannot see that a KeyBindings object was handed across.**

So there are two ways a finished feature goes dark, and the repo could only
measure one of them:

    IMPORT reachability   nothing imports the module          → the board sees it
    HANDOFF consumption   a caller passes it and the callee    → NOTHING saw it
                          drops the value on the floor

The second is what happened to `search_rows`. `ov.py` resolves
`transcript_hatches.search_status`, hands it to `run_bipartite_repl`, which
forwards it to `build_bipartite_application`, whose signature accepts it and
whose body never reads it. Three cooperating call sites, one dropped value, and
transcript search was dark on the SHIPPING client — not merely in a demo. The
board reported the module LIVE and was right: it is imported. The surface audit
reported it reachable and was right too.

The test that was supposed to hold it asserted ``"search_rows=" in src``. It
passed for the entire period the feature did nothing, which is the third
appearance of that antipattern in a week.

Why a signature is the right source of truth
============================================
The alternative is a checklist of "capabilities the cockpit has", maintained by
hand. That is `/narrate`'s hardcoded producer list in a new costume: correct the
day it is written, wrong the moment a hook is added, with nothing to detect the
drift. `narrative_density` inverted exactly this — producers REGISTER and the
dial PULLS — and the same inversion applies here with an even better authority
available, because a builder's parameter list already IS the complete,
self-updating statement of what it can be given.

Nothing here is hardcoded to `bipartite_layout`. Sinks are DISCOVERED by shape
(:func:`discover_sinks`): a function carrying many keyword-only collaborators is
a hook-shaped builder, whatever it is called and wherever it lives. Add a
second cockpit builder and it is audited without editing this file.

UNSET is not WAIVED
===================
A surface may legitimately decline a hook — `ov demo live` keeps input inert on
purpose, so a completer would be furniture. But *silence* cannot be the way it
says so, because silence is also what a forgotten hook looks like, and the whole
class of defect here is a decision nobody made.

:func:`waived` is the answer, and it is deliberately the same shape as
`provenance`'s UNKNOWN-vs-UNSET distinction: it returns ``None``, so at runtime
it is byte-for-byte identical to not passing the argument, and statically it is
a declared decision with a reason attached, sitting AT the call site where the
decision was actually made rather than in a registry that can drift from it.

    completer=waived("input is inert in a demo — nothing to complete against")

A forward is not a consumption
==============================
The analysis has to distinguish three things a body can do with a parameter,
because collapsing them is how a pass-through wrapper looks like a defect and a
real defect looks like a wrapper:

    READ             the body does something with the value
    FORWARDED_ONLY   every use is handing it to another sink, unexamined
    UNREAD           the body never mentions it

``FORWARDED_ONLY`` is not a finding by itself — `run_bipartite_repl` forwards
almost everything and is correct to. It becomes a finding when the chain it
forwards INTO does not consume it either, so consumption resolves transitively
(:func:`effective_consumption`) with a cycle guard, and only a chain that
terminates in ``UNREAD`` is reported.

Honest about what it cannot see
===============================
Static analysis of a dynamic language has edges, and quietly guessing at them
would make this instrument the thing it was built to catch. Each is reported as
``OPAQUE`` rather than as a pass or a fail:

  * a sink with ``**kwargs`` accepts names no signature enumerates
  * a caller using ``**splat`` fills names no call site enumerates
  * a body reaching a parameter through ``getattr``/``locals()`` reads it in a
    way no AST walk can prove

Pure AST over source text. Never imports the code it measures — a module that
would explode on import is exactly the one worth auditing — and never raises.
"""
from __future__ import annotations

import ast
import enum
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import (Any, Dict, FrozenSet, Iterable, List, Optional,
                    Sequence, Set, Tuple)

logger = logging.getLogger("Ouroboros.CapabilityHandoff")

CAPABILITY_HANDOFF_SCHEMA_VERSION = "capability_handoff.v1"

MASTER_FLAG_ENV_VAR = "JARVIS_CAPABILITY_HANDOFF_ENABLED"
SINKS_ENV_VAR = "JARVIS_HANDOFF_SINKS"
SURFACES_ENV_VAR = "JARVIS_HANDOFF_SURFACES"
MIN_HOOKS_ENV_VAR = "JARVIS_HANDOFF_MIN_HOOKS"
ROOTS_ENV_VAR = "JARVIS_HANDOFF_ROOTS"

#: How many keyword-only collaborators make a function a hook-shaped builder.
#: Tunable because "many" is a judgement and the right number is whatever
#: separates builders from ordinary functions in a given tree — not a constant
#: this module is entitled to fix forever.
DEFAULT_MIN_HOOKS = 8

#: Where to look for sinks and surfaces. Same roots the surface audit uses, for
#: the same reason: handoff into the governance core is a different question.
DEFAULT_ROOTS: Tuple[str, ...] = (
    "backend/core/ouroboros/battle_test",
    "backend/core/ouroboros/ui",
    "backend/core/ouroboros/cli",
)

_FALSY = ("0", "false", "no", "off")


def handoff_enabled() -> bool:
    """Default ON. This reads source; it never imports or executes it."""
    return os.environ.get(
        MASTER_FLAG_ENV_VAR, "1",
    ).strip().lower() not in _FALSY


def min_hooks() -> int:
    """The hook-count threshold that makes a function a sink."""
    raw = os.environ.get(MIN_HOOKS_ENV_VAR, "").strip()
    try:
        return max(1, int(raw)) if raw else DEFAULT_MIN_HOOKS
    except (TypeError, ValueError):
        return DEFAULT_MIN_HOOKS


def audit_roots() -> Tuple[str, ...]:
    raw = os.environ.get(ROOTS_ENV_VAR, "").strip()
    if not raw:
        return DEFAULT_ROOTS
    return tuple(p.strip() for p in raw.split(",") if p.strip()) or DEFAULT_ROOTS


# ---------------------------------------------------------------------------
# The waiver — runtime-inert, statically loud
# ---------------------------------------------------------------------------


def waived(reason: str) -> None:
    """Declare that a hook is deliberately NOT filled here. Returns ``None``.

    Passing ``hook=waived("...")`` is identical at runtime to omitting the
    argument entirely — the callee's ``if hook is not None`` guard sees exactly
    what it saw before, so adopting this can never change behaviour. The value
    is that the omission stops being silent.

    The reason is not decoration. An unfilled hook has two possible causes and
    they need opposite responses: "this surface has no use for it" is a design
    decision, and "nobody noticed it existed" is the defect this module exists
    to find. Silence cannot distinguish them; a sentence can.

    Deliberately mirrors `provenance`'s rule that UNKNOWN and UNSET are
    different states. Nobody-asked and asked-and-declined are not the same
    fact, and a surface that renders them identically is the bug.

    The reason is consumed by the AST auditor reading this call site, never at
    runtime — so this function declares its own discard with the same ``del``
    statement :class:`Consumption.DECLARED_DROP` was taught to recognise. If
    the convention is good enough to exempt other people's parameters it is
    good enough for this one.
    """
    del reason
    return None


#: The call-site spelling the auditor looks for. Derived from the function's own
#: name so a rename cannot desynchronise the analyser from the API.
WAIVER_CALLABLE_NAME = waived.__name__


class Consumption(enum.Enum):
    """What a sink's body does with a parameter it accepts."""

    READ = "read"
    FORWARDED_ONLY = "forwarded_only"
    UNREAD = "unread"
    #: ``del param`` — the body accepts the name and deliberately discards it.
    #:
    #: Python already has the statement for "I take this and intentionally do
    #: not use it", this codebase already uses it for exactly that
    #: (`presentation_restraint.render_minimal_welcome` accepts ``session_id``
    #: for caller compatibility and deletes it), and inventing a decorator or a
    #: registry to say the same thing would be a second vocabulary for a
    #: sentence the language can already form.
    #:
    #: This is the sink-side mirror of :func:`waived`, and it earns the same
    #: treatment: a declared discard is accounted for, an undeclared one is a
    #: finding. Silence is the only thing that is ever a defect.
    DECLARED_DROP = "declared_drop"
    #: The body could reach it in a way AST cannot prove (``**kwargs``,
    #: ``getattr``, ``locals()``). Never counted as either a pass or a fail.
    OPAQUE = "opaque"


class FillState(enum.Enum):
    """What a calling surface does about a hook the sink offers."""

    FILLED = "filled"
    WAIVED = "waived"
    #: Neither passed nor declared — the state that is a finding.
    UNSET = "unset"
    #: The caller splats ``**kwargs``, so no call site enumerates the name.
    OPAQUE = "opaque"


@dataclass(frozen=True)
class Hook:
    """One collaborator a sink is willing to accept."""

    name: str
    sink: str
    positional: bool
    required: bool
    consumption: Consumption
    #: Sinks this parameter is handed onward to, unexamined.
    forwards_to: Tuple[str, ...] = ()

    @property
    def short_sink(self) -> str:
        return self.sink.rsplit(".", 1)[-1]


@dataclass(frozen=True)
class Fill:
    """What one surface does about one hook."""

    surface: str
    sink: str
    hook: str
    state: FillState
    reason: str = ""
    line: int = 0


@dataclass(frozen=True)
class SinkSpec:
    """A discovered hook-shaped builder: where it lives and what it is called."""

    module: str
    function: str
    path: str
    hook_count: int

    @property
    def qualname(self) -> str:
        return f"{self.module}.{self.function}"


@dataclass
class HandoffReading:
    """The whole audit, as data. Rendering is a separate concern."""

    hooks: List[Hook] = field(default_factory=list)
    fills: List[Fill] = field(default_factory=list)
    sinks: List[SinkSpec] = field(default_factory=list)
    surfaces: Tuple[str, ...] = ()
    unparseable: Tuple[str, ...] = ()
    schema_version: str = CAPABILITY_HANDOFF_SCHEMA_VERSION

    # -- findings ----------------------------------------------------------

    def dropped(self) -> List[Hook]:
        """Hooks accepted by a sink and consumed by NOTHING it forwards to.

        The `search_rows` class. This is the list that should always be empty,
        and the only one of these accessors that describes a defect in the
        cockpit itself rather than in a surface's coverage of it.
        """
        by_qual = {h.sink + "." + h.name: h for h in self.hooks}
        out: List[Hook] = []
        for hook in self.hooks:
            if effective_consumption(hook, by_qual) is Consumption.UNREAD:
                out.append(hook)
        return sorted(out, key=lambda h: (h.sink, h.name))

    def unset(self, surface: Optional[str] = None) -> List[Fill]:
        """Hooks a surface neither fills nor declines. The coverage finding."""
        return sorted(
            (f for f in self.fills
             if f.state is FillState.UNSET
             and (surface is None or f.surface == surface)),
            key=lambda f: (f.surface, f.sink, f.hook),
        )

    def divergence(self) -> List[Tuple[str, str, Tuple[str, ...],
                                       Tuple[str, ...]]]:
        """``(sink, hook, filled_by, unset_by)`` where surfaces DISAGREE.

        This is the coverage finding, and scoping it to disagreement is what
        makes it a signal instead of a wall. Every hook-shaped builder has
        optional parameters most callers rightly ignore; reporting each of them
        produced 102 rows of which four mattered, and an instrument nobody can
        read is an instrument nobody runs.

        Disagreement is the shape that actually indicates a gap: if one surface
        passes a hook and another leaves it unset, the second is either missing
        a capability the first proves is real, or making a decision it has not
        declared. A hook NO caller fills is not a demo gap — it is an unused
        option, which is a different and much weaker observation.

        Precisely the doctrine `surface_reachability` arrived at for imports:
        "Asymmetry is evidence, never a conclusion." Same reasoning, applied to
        values handed across a boundary rather than to modules imported.
        """
        by_key: Dict[Tuple[str, str], Dict[str, List[FillState]]] = {}
        for fill in self.fills:
            key = (fill.sink, fill.hook)
            by_key.setdefault(key, {}).setdefault(fill.surface, []).append(
                fill.state)
        out: List[Tuple[str, str, Tuple[str, ...], Tuple[str, ...]]] = []
        for (sink, hook), per_surface in by_key.items():
            filled, unset = [], []
            for surface, states in per_surface.items():
                # A surface with several call sites counts as filling the hook
                # if ANY of them does: the capability is demonstrably reachable
                # from there, which is the question.
                if FillState.FILLED in states:
                    filled.append(surface)
                elif all(s is FillState.UNSET for s in states):
                    unset.append(surface)
            if filled and unset:
                out.append((sink, hook, tuple(sorted(filled)),
                            tuple(sorted(unset))))
        return sorted(out, key=lambda row: (row[0], row[1]))

    def waivers(self, surface: Optional[str] = None) -> List[Fill]:
        return sorted(
            (f for f in self.fills
             if f.state is FillState.WAIVED
             and (surface is None or f.surface == surface)),
            key=lambda f: (f.surface, f.sink, f.hook),
        )

    def coverage(self, surface: str) -> Tuple[int, int]:
        """``(accounted_for, offered)`` for one surface.

        A WAIVED hook counts as accounted for. The number this exists to move
        is not "hooks filled" — a surface that fills everything regardless of
        whether it needs it is not better — it is "hooks nobody has thought
        about", and that number should be zero.
        """
        rows = [f for f in self.fills if f.surface == surface]
        if not rows:
            return (0, 0)
        ok = sum(1 for f in rows if f.state in (FillState.FILLED,
                                                FillState.WAIVED,
                                                FillState.OPAQUE))
        return (ok, len(rows))


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------

#: Both function flavours. `run_bipartite_repl` is an `async def`, and a walker
#: that only knew `FunctionDef` would silently skip every async sink — which is
#: most of a cockpit.
_FUNC_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)


def _repo_root() -> Path:
    """Reuses the board's root resolution rather than inventing a second one."""
    try:
        from backend.core.ouroboros.battle_test.progress_board import (
            _default_root,
        )
        return _default_root()
    except Exception:  # noqa: BLE001
        return Path(__file__).resolve().parents[4]


def _parse(path: Path) -> Optional[ast.AST]:
    try:
        return ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 — a file that will not parse has no hooks
        return None


def _module_name_for(rel: str) -> str:
    try:
        from backend.core.ouroboros.battle_test.progress_board import (
            _module_name,
        )
        return _module_name(rel)
    except Exception:  # noqa: BLE001
        return rel[:-3].replace("/", ".") if rel.endswith(".py") else rel


def _source_files(roots: Sequence[str]) -> List[Tuple[str, Path]]:
    """``(dotted_module, path)`` for every non-test source file under roots."""
    base = _repo_root()
    out: List[Tuple[str, Path]] = []
    for root in roots:
        directory = base / root
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.py")):
            try:
                rel = path.relative_to(base).as_posix()
            except ValueError:
                continue
            leaf = rel.rsplit("/", 1)[-1]
            if "/tests/" in f"/{rel}" or leaf.startswith("test_"):
                continue
            out.append((_module_name_for(rel), path))
    return out


def _functions(tree: ast.AST) -> Dict[str, Any]:
    """Every function in a module by name, nested ones included.

    Nested definitions matter in both directions: a sink can be defined inside
    a factory, and a hook is frequently consumed inside a closure the builder
    defines — `_toolbar_fragments` reads `toolbar` from the enclosing scope,
    and a walker that stopped at the top level would call that hook UNREAD.
    """
    out: Dict[str, Any] = {}
    for node in ast.walk(tree):
        if isinstance(node, _FUNC_NODES):
            out.setdefault(node.name, node)
    return out


def _hook_names(fn: Any) -> Tuple[List[Tuple[str, bool, bool]], bool]:
    """``([(name, positional, required)], opaque)`` for a function's params.

    ``opaque`` is True when the signature carries ``**kwargs``: the sink then
    accepts names no signature enumerates, and claiming a complete hook list
    would be a lie of exactly the kind this module exists to stop.
    """
    args = fn.args
    out: List[Tuple[str, bool, bool]] = []
    # Positional params: required iff no default. Defaults bind to the TAIL of
    # the positional list, so the offset matters.
    pos = list(args.posonlyargs) + list(args.args)
    n_defaults = len(args.defaults)
    first_defaulted = len(pos) - n_defaults
    for i, a in enumerate(pos):
        if a.arg in ("self", "cls"):
            continue
        out.append((a.arg, True, i < first_defaulted))
    for a, default in zip(args.kwonlyargs, args.kw_defaults):
        out.append((a.arg, False, default is None))
    return out, args.kwarg is not None


def _callee_name(call: ast.Call) -> str:
    """The bare function name a Call targets, through either spelling.

    ``build_bipartite_application(...)`` and
    ``bipartite_layout.build_bipartite_application(...)`` are the same handoff,
    and an analyser that only recognised one of them would report the other as
    a drop. Aliased imports resolve to the alias here and are reconciled by the
    caller against the sink index.
    """
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


# ---------------------------------------------------------------------------
# Callee resolution — a name is not an identity
# ---------------------------------------------------------------------------
#
# Matching a call to a sink by its BARE last segment was the analyser's
# sharpest blind spot, and it produced confident false findings rather than
# quiet ones. `serpent_flow` calls ``narrative_renderer.compose``; the audit
# reported it as failing to fill seven parameters of
# ``tool_render_view.compose`` — a different function that happens to share a
# name. There are SIX distinct ``compose`` definitions under these roots, so
# every caller of any of them was judged against all of them.
#
# Seven of eleven reported divergences came from that one collision. An
# auditor wrong most of the time is an auditor nobody reads, and the real
# finding underneath it (the daemon cockpit genuinely dropping four
# capabilities the attach client passes) was sitting in the same list.
#
# It is also the SAME defect this codebase keeps rediscovering: the caller
# index that read ``repair_engine`` 40% severed did so because it keyed on
# bare symbol names and could not see
# ``self._config.repair_engine.run(...)``. Unqualified names are not
# identities. This resolves them.

#: Names that are never a module, so ``x.compose(...)`` through them is a
#: METHOD call and cannot be a module-level sink. Without this, every
#: ``self.compose(...)`` in the tree matched every module-level ``compose``.
_NON_MODULE_ROOTS = frozenset({"self", "cls", "super"})

#: Calls whose target the AST could not settle, kept so the audit can REPORT
#: its own blind spot instead of silently dropping them. A tool that quietly
#: discards what it cannot resolve looks identical to one with nothing to
#: find — the distinction this codebase spent today learning to make.
_UNRESOLVED_CALLS: List[Tuple[str, str, int]] = []


def unresolved_calls() -> Tuple[Tuple[str, str, int], ...]:
    """``(module, callee, line)`` the resolver refused to guess at."""
    return tuple(_UNRESOLVED_CALLS)


def _relative_base(module: str, is_init: bool, level: int) -> Optional[str]:
    """Absolute package a relative import resolves against, or None.

    The rule is CPython's and is already stated once in
    `reverse_dep_resolver._add_relative_import_edges`; it is restated here
    rather than imported because that function resolves EDGES into a set and
    this needs the BASE to build a binding from. Same algorithm, different
    return type — importing it would mean reconstructing the base from its
    output, which is the more fragile coupling.
    """
    if is_init:
        package = module
    elif "." in module:
        package = module.rsplit(".", 1)[0]
    else:
        package = ""
    parts = package.split(".") if package else []
    drop = max(0, level - 1)
    if drop > len(parts):
        return None                       # escapes the tree — unresolvable
    return ".".join(parts[: len(parts) - drop])


def _bindings_from(nodes: Iterable[ast.AST], module: str,
                   is_init: bool) -> Dict[str, Optional[str]]:
    """``local name -> absolute dotted target``, or None where AMBIGUOUS.

    ``None`` is a real value here: a scope that binds one name to two
    different targets cannot resolve a call to either, and guessing would
    reintroduce exactly the confident-wrong-answer this replaces.

    Both spellings are recorded because both appear at call sites:

        from x import compose            ->  compose -> x.compose
        from x import compose as c       ->  c       -> x.compose
        import x                         ->  x       -> x
        import x.y as z                  ->  z       -> x.y
    """
    out: Dict[str, Optional[str]] = {}

    def _bind(name: str, target: str) -> None:
        if not name or not target:
            return
        if name in out and out[name] != target:
            out[name] = None              # ambiguous in this scope
            return
        out.setdefault(name, target)

    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not alias.name:
                    continue
                # `import a.b.c` with no alias binds the ROOT package `a`;
                # with an alias it binds the alias to the full path.
                if alias.asname:
                    _bind(alias.asname, alias.name)
                else:
                    _bind(alias.name.split(".", 1)[0],
                          alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                base = _relative_base(module, is_init, node.level)
                if base is None:
                    continue
                target_module = (f"{base}.{node.module}" if node.module
                                 else base)
            else:
                target_module = node.module or ""
            if not target_module:
                continue
            for alias in node.names:
                if not alias.name or alias.name == "*":
                    continue
                _bind(alias.asname or alias.name,
                      f"{target_module}.{alias.name}")
    return out


def _direct_children(scope: ast.AST) -> List[ast.AST]:
    """Every node inside *scope* EXCLUDING nested function bodies.

    Nested scopes get their own maps; folding them in would let a helper's
    local import shadow its parent's, which is the ambiguity this is trying
    to avoid rather than create.
    """
    out: List[ast.AST] = []
    stack: List[ast.AST] = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        out.append(node)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            # YIELDED but not DESCENDED INTO. The caller needs to see the
            # nested scope in order to recurse with its own bindings;
            # dropping it here (the first version did) meant `_visit` never
            # found a single nested function and scanned module level only —
            # which silently took the audit from 11 findings to 0 and looked
            # like a clean bill of health.
            continue
        stack.extend(ast.iter_child_nodes(node))
    return out


def _resolve_callee(call: ast.Call, scopes: Sequence[Dict[str, Optional[str]]],
                    local_defs: FrozenSet[str], module: str) -> Optional[str]:
    """Absolute dotted target of *call*, or None when unprovable.

    None is returned for anything the AST cannot settle — an unbound name, an
    ambiguous binding, a call through a variable, a method on an instance.
    Every one of those used to MATCH, on the strength of the last path
    segment alone.

    Scope order is innermost-first, which matters because this codebase
    imports inside functions constantly: a module-level
    ``from tool_render_view import compose`` and a function-local
    ``from narrative_renderer import compose`` are both correct, and the call
    means whichever one encloses it.
    """
    func = call.func
    if isinstance(func, ast.Name):
        name = func.id
        for scope in scopes:
            if name in scope:
                target = scope[name]
                return target             # None => ambiguous, honestly so
        # A module-level def in THIS file shadows nothing and binds locally.
        if name in local_defs:
            return f"{module}.{name}"
        return None
    if isinstance(func, ast.Attribute):
        value = func.value
        if not isinstance(value, ast.Name):
            return None                  # a.b.c(...) / expr().m(...) — no
        root = value.id
        if root in _NON_MODULE_ROOTS:
            return None                  # a METHOD, never a module sink
        for scope in scopes:
            if root in scope:
                base = scope[root]
                return f"{base}.{func.attr}" if base else None
        return None
    return None


def _reads_opaquely(fn: Any) -> bool:
    """Does the body reach its own scope dynamically?

    ``locals()``, ``vars()`` and ``getattr`` on a local can all consume a
    parameter in a way no AST walk can prove. Rather than guess, the sink's
    hooks become OPAQUE and are excluded from both the pass and the fail
    columns.
    """
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            name = _callee_name(node)
            if name in ("locals", "vars", "eval", "exec"):
                return True
    return False


# ---------------------------------------------------------------------------
# Sink discovery — by SHAPE, never by name
# ---------------------------------------------------------------------------


def discover_sinks(
    *,
    roots: Optional[Sequence[str]] = None,
    threshold: Optional[int] = None,
) -> List[SinkSpec]:
    """Every hook-shaped builder in the tree. NEVER raises.

    A sink is a function carrying at least ``threshold`` keyword-only
    parameters. That is a structural property, not a naming convention, so it
    holds for a builder this module has never heard of — which is the entire
    point. Detecting them by name (``build_*``) or by annotation
    (``Optional[Callable]``) would both have missed real hooks: `completer`,
    `history`, `auto_suggest` and `turn_spinner` are all annotated ``Any``.

    Explicit override via ``JARVIS_HANDOFF_SINKS`` as
    ``dotted.module:function,dotted.module:function`` — an operator pinning the
    audit to one sink should not have to satisfy a heuristic.
    """
    if not handoff_enabled():
        return []
    roots = tuple(roots) if roots is not None else audit_roots()
    want = threshold if threshold is not None else min_hooks()
    pinned = _pinned_sinks()
    out: List[SinkSpec] = []
    try:
        for module, path in _source_files(roots):
            tree = _parse(path)
            if tree is None:
                continue
            rel = _relpath(path)
            for name, fn in _functions(tree).items():
                hooks = _hook_names(fn)[0]
                kwonly = sum(1 for entry in hooks if not entry[1])
                if pinned:
                    if (module, name) not in pinned:
                        continue
                elif kwonly < want:
                    continue
                out.append(SinkSpec(module=module, function=name, path=rel,
                                    hook_count=len(hooks)))
    except Exception:  # noqa: BLE001
        logger.debug("[CapabilityHandoff] sink discovery degraded",
                     exc_info=True)
    return sorted(out, key=lambda s: s.qualname)


def _pinned_sinks() -> Set[Tuple[str, str]]:
    raw = os.environ.get(SINKS_ENV_VAR, "").strip()
    out: Set[Tuple[str, str]] = set()
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if ":" in chunk:
            module, func = chunk.rsplit(":", 1)
            if module.strip() and func.strip():
                out.add((module.strip(), func.strip()))
    return out


def _relpath(path: Path) -> str:
    try:
        return path.relative_to(_repo_root()).as_posix()
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# Consumption — what the sink's body does with what it was given
# ---------------------------------------------------------------------------


def analyse_sink(spec: SinkSpec, *,
                 sink_names: Optional[Set[str]] = None) -> List[Hook]:
    """The hooks one sink offers, each with what its body does. NEVER raises.

    ``sink_names`` is the set of bare function names that count as onward
    sinks. A use of a parameter as an argument to one of those is a FORWARD,
    not a consumption — the distinction that keeps a pass-through wrapper from
    reading as a defect, and keeps `search_rows` from hiding behind one.
    """
    try:
        base = _repo_root()
        tree = _parse(base / spec.path)
        if tree is None:
            return []
        fn = _functions(tree).get(spec.function)
        if fn is None:
            return []
        names, kwargs_opaque = _hook_names(fn)
        opaque_body = _reads_opaquely(fn)
        onward = set(sink_names or ())
        out: List[Hook] = []
        for name, positional, required in names:
            if kwargs_opaque or opaque_body:
                consumption, forwards = Consumption.OPAQUE, ()
            else:
                consumption, forwards = _classify(fn, name, onward)
            out.append(Hook(name=name, sink=spec.qualname,
                            positional=positional, required=required,
                            consumption=consumption, forwards_to=forwards))
        return out
    except Exception:  # noqa: BLE001
        logger.debug("[CapabilityHandoff] sink analysis degraded: %s",
                     spec.qualname, exc_info=True)
        return []


def _classify(fn: Any, param: str,
              onward: Set[str]) -> Tuple[Consumption, Tuple[str, ...]]:
    """READ / FORWARDED_ONLY / UNREAD for one parameter of one function.

    Every LOAD of the name is examined. A load that is an argument to an onward
    sink is a forward; anything else — a guard, a container, a call to a
    renderer, a comparison — is a read. Stores are ignored: rebinding a
    parameter is not consuming what the caller passed.
    """
    forwards: Set[str] = set()
    loads = 0
    forwarded = 0
    deleted = False
    for node in ast.walk(fn):
        if isinstance(node, ast.Delete):
            for target_node in node.targets:
                if (isinstance(target_node, ast.Name)
                        and target_node.id == param):
                    deleted = True
            continue
        if not (isinstance(node, ast.Name) and node.id == param):
            continue
        if not isinstance(node.ctx, ast.Load):
            continue
        loads += 1
        target = _forward_target(fn, node, onward)
        if target:
            forwarded += 1
            forwards.add(target)
    # Checked before the load count, because `del param` is a Del context and
    # contributes no loads: a declared discard would otherwise be
    # indistinguishable from the silent drop it exists to differentiate from.
    if deleted and loads == 0:
        return Consumption.DECLARED_DROP, ()
    if loads == 0:
        return Consumption.UNREAD, ()
    if forwarded == loads:
        return Consumption.FORWARDED_ONLY, tuple(sorted(forwards))
    return Consumption.READ, tuple(sorted(forwards))


def _forward_target(fn: Any, name_node: ast.Name,
                    onward: Set[str]) -> str:
    """The onward sink this load is being handed to, or "".

    Matches the load against the argument lists of every Call in the function,
    by identity, so a name appearing twice in one statement is judged per
    occurrence rather than per spelling.
    """
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        callee = _callee_name(node)
        if not callee or callee not in onward:
            continue
        for arg in node.args:
            if arg is name_node:
                return callee
        for kw in node.keywords:
            if kw.value is name_node:
                return callee
    return ""


def effective_consumption(hook: Hook,
                          by_qual: Dict[str, Hook],
                          _seen: Optional[Set[str]] = None) -> Consumption:
    """Resolve a forward CHAIN to what actually happens at the end of it.

    A parameter forwarded into a sink that reads it IS consumed — the wrapper
    is doing its job. A parameter forwarded into a sink that drops it is
    dropped, however many polite hops it took to get there. Reporting the hop
    instead of the drop would point every investigation at the wrong file.

    Cycle-guarded because mutual forwarding between overloads is legal and a
    naive walk would not terminate. An unresolvable link (the target sink is
    outside the audited roots) resolves to OPAQUE rather than to UNREAD:
    "I cannot see the far end" and "the far end drops it" are different
    findings and only one of them is a defect.
    """
    seen = set(_seen or ())
    key = f"{hook.sink}.{hook.name}"
    if key in seen:
        return Consumption.OPAQUE
    seen.add(key)
    if hook.consumption is not Consumption.FORWARDED_ONLY:
        return hook.consumption
    verdicts: List[Consumption] = []
    for target in hook.forwards_to:
        # Same bare function name, any module: the forward was resolved by
        # name, so the lookup has to be too.
        matches = [h for qual, h in by_qual.items()
                   if h.name == hook.name
                   and qual.rsplit(".", 2)[-2] == target]
        if not matches:
            verdicts.append(Consumption.OPAQUE)
            continue
        for match in matches:
            verdicts.append(effective_consumption(match, by_qual, seen))
    if not verdicts:
        return Consumption.OPAQUE
    # ANY terminal read redeems the chain; a hook forwarded two ways where one
    # end uses it is not dropped.
    if Consumption.READ in verdicts:
        return Consumption.READ
    if Consumption.OPAQUE in verdicts:
        return Consumption.OPAQUE
    return Consumption.UNREAD


# ---------------------------------------------------------------------------
# Fills — what each calling surface does about each hook
# ---------------------------------------------------------------------------


def analyse_surface(module: str, path: Path, hooks: Sequence[Hook],
                    *, sink_functions: Set[str]) -> List[Fill]:
    """What ``module``'s call sites do about every hook. NEVER raises.

    A module may call the same sink more than once for genuinely different
    surfaces — `ov.py` builds a PromptSession fallback and a full cockpit — so
    each call site is judged separately and labelled by line. Merging them
    would let a fully-wired cockpit hide an unwired fallback.
    """
    out: List[Fill] = []
    try:
        tree = _parse(path)
        if tree is None:
            return []
        # Sinks keyed by their FULL dotted qualname. The previous index keyed
        # on the last segment, so `tool_render_view.compose` and
        # `narrative_renderer.compose` — six such collisions exist under these
        # roots — were the same key, and every caller of either was judged
        # against both.
        by_qualname: Dict[str, List[Hook]] = {}
        for hook in hooks:
            by_qualname.setdefault(hook.sink, []).append(hook)

        is_init = path.name == "__init__.py"
        module_scope = _bindings_from(_direct_children(tree), module, is_init)
        local_defs = frozenset(
            n.name for n in ast.iter_child_nodes(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        )

        # (call, enclosing scope chain) — innermost first. Walking scopes
        # rather than the whole tree is what lets a function-local import
        # shadow a module-level one, which this codebase does constantly.
        def _visit(scope: ast.AST,
                   chain: Sequence[Dict[str, Optional[str]]]) -> None:
            body = _direct_children(scope)
            for node in body:
                if isinstance(node, ast.Call):
                    resolved = _resolve_callee(
                        node, chain, local_defs, module)
                    if resolved is None:
                        # Unprovable, and recorded as such rather than
                        # matched. `_callee_name` still names it for the
                        # blind-spot report below.
                        name = _callee_name(node)
                        if name in sink_functions:
                            _UNRESOLVED_CALLS.append(
                                (module, name, getattr(node, "lineno", 0)))
                        continue
                    targets = by_qualname.get(resolved)
                    if targets:
                        out.extend(_fills_for_call(module, node, targets))
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                       ast.ClassDef)):
                    inner = _bindings_from(
                        _direct_children(node), module, is_init)
                    _visit(node, (inner,) + tuple(chain) if inner else chain)

        _visit(tree, (module_scope,))
    except Exception:  # noqa: BLE001
        logger.debug("[CapabilityHandoff] surface analysis degraded: %s",
                     module, exc_info=True)
    return out


def _fills_for_call(surface: str, call: ast.Call,
                    hooks: Sequence[Hook]) -> List[Fill]:
    """One call site → one Fill per hook the sink offers."""
    line = getattr(call, "lineno", 0)
    sink = hooks[0].sink if hooks else ""
    splat = any(kw.arg is None for kw in call.keywords)
    positional = [h for h in hooks if h.positional]
    supplied_positionally = {
        h.name for h in positional[:len(call.args)]
    }
    by_keyword: Dict[str, ast.AST] = {
        kw.arg: kw.value for kw in call.keywords if kw.arg
    }
    out: List[Fill] = []
    for hook in hooks:
        if hook.name in supplied_positionally:
            out.append(Fill(surface, sink, hook.name, FillState.FILLED,
                            line=line))
            continue
        value = by_keyword.get(hook.name)
        if value is None:
            # A splatted caller may well be filling it; nothing at this call
            # site enumerates the name, so neither pass nor fail is honest.
            state = FillState.OPAQUE if splat else FillState.UNSET
            out.append(Fill(surface, sink, hook.name, state, line=line))
            continue
        reason = _waiver_reason(value)
        if reason is not None:
            out.append(Fill(surface, sink, hook.name, FillState.WAIVED,
                            reason=reason, line=line))
        else:
            out.append(Fill(surface, sink, hook.name, FillState.FILLED,
                            line=line))
    return out


def _waiver_reason(value: ast.AST) -> Optional[str]:
    """The reason string if this argument is a :func:`waived` call, else None.

    Matched on the callable's own ``__name__`` so renaming the function cannot
    leave the analyser matching a spelling that no longer exists. A waiver with
    no reason is NOT a waiver — the reason is the entire difference between a
    declared decision and a shrug — so an empty string falls through and the
    hook reports UNSET.
    """
    if not isinstance(value, ast.Call):
        return None
    if _callee_name(value) != WAIVER_CALLABLE_NAME:
        return None
    for arg in list(value.args) + [kw.value for kw in value.keywords]:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            text = arg.value.strip()
            if text:
                return text
    return None


def _pinned_surfaces() -> Tuple[str, ...]:
    raw = os.environ.get(SURFACES_ENV_VAR, "").strip()
    if not raw:
        return ()
    return tuple(s.strip() for s in raw.split(",") if s.strip())


# ---------------------------------------------------------------------------
# The audit
# ---------------------------------------------------------------------------


def audit(*, roots: Optional[Sequence[str]] = None,
          threshold: Optional[int] = None) -> HandoffReading:
    """Discover sinks, classify their hooks, and measure every caller.

    NEVER raises: a degraded audit reports what it managed to establish. An
    instrument that fails closed on a malformed tree is an instrument nobody
    runs.
    """
    reading = HandoffReading()
    if not handoff_enabled():
        return reading
    try:
        roots = tuple(roots) if roots is not None else audit_roots()
        sinks = discover_sinks(roots=roots, threshold=threshold)
        reading.sinks = sinks
        sink_functions = {s.function for s in sinks}
        hooks: List[Hook] = []
        for spec in sinks:
            hooks.extend(analyse_sink(spec, sink_names=sink_functions))
        reading.hooks = hooks

        pinned = _pinned_surfaces()
        labels: List[str] = []
        unparseable: List[str] = []
        for module, path in _source_files(roots):
            if pinned and module not in pinned:
                continue
            if _parse(path) is None:
                unparseable.append(module)
                continue
            fills = analyse_surface(module, path, hooks,
                                    sink_functions=sink_functions)
            if not fills:
                continue
            # A sink calling itself recursively is not a surface consuming it.
            if any(s.module == module and s.function in sink_functions
                   for s in sinks) and module not in pinned:
                fills = [f for f in fills
                         if f.sink.rsplit(".", 1)[0] != module]
                if not fills:
                    continue
            reading.fills.extend(fills)
            labels.append(module)
        reading.surfaces = tuple(sorted(set(labels)))
        reading.unparseable = tuple(sorted(unparseable))
        # After every direct fill is known, never during: propagation reads the
        # complete fill set, and folding it into the loop above would let a
        # surface scanned early miss an edge a surface scanned later proved.
        reading.fills.extend(propagate_fills(reading))
    except Exception:  # noqa: BLE001
        logger.debug("[CapabilityHandoff] audit degraded", exc_info=True)
    return reading


def propagate_fills(reading: HandoffReading) -> List[Fill]:
    """Fills implied by a wrapper's forwarding. NEVER raises.

    `ov.py` reaches the cockpit through `run_bipartite_repl`; `ov_demo` calls
    `build_bipartite_application` directly. Both are filling the SAME
    capability, and comparing them literally answers nothing — the two surfaces
    never name the same sink, so the divergence that motivated this whole module
    was invisible to the first version of it.

    A wrapper that forwards a hook onward means anyone who filled the wrapper
    has, in effect, filled the sink at the far end. That relationship is not
    guessed: it is exactly the ``forwards_to`` edge already proven by
    :func:`_classify`, so this reuses the forward graph rather than introducing
    an "equivalent sinks" table — which would be the hardcoded checklist this
    module exists to avoid, one level up.

    Indirect fills never MASK a direct finding: they are additive, and a
    surface that both calls the sink directly and leaves a hook unset there
    keeps its UNSET row. The forward edge widens what counts as covered; it
    cannot narrow it.
    """
    out: List[Fill] = []
    try:
        # hook name → sinks it is forwarded INTO, by the sink that forwards it.
        forwards: Dict[Tuple[str, str], Tuple[str, ...]] = {
            (h.sink, h.name): h.forwards_to for h in reading.hooks
            if h.forwards_to
        }
        # bare function name → full qualnames, to resolve a forward target.
        by_func: Dict[str, List[str]] = {}
        for hook in reading.hooks:
            by_func.setdefault(hook.sink.rsplit(".", 1)[-1], [])
            if hook.sink not in by_func[hook.sink.rsplit(".", 1)[-1]]:
                by_func[hook.sink.rsplit(".", 1)[-1]].append(hook.sink)
        known = {(h.sink, h.name) for h in reading.hooks}
        seen = {(f.surface, f.sink, f.hook) for f in reading.fills}
        for fill in reading.fills:
            for target_func in forwards.get((fill.sink, fill.hook), ()):
                for target_sink in by_func.get(target_func, ()):
                    if (target_sink, fill.hook) not in known:
                        continue
                    key = (fill.surface, target_sink, fill.hook)
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(Fill(fill.surface, target_sink, fill.hook,
                                    fill.state, reason=fill.reason,
                                    line=fill.line))
    except Exception:  # noqa: BLE001
        logger.debug("[CapabilityHandoff] fill propagation degraded",
                     exc_info=True)
    return out


def render(reading: HandoffReading, *, limit: int = 20) -> List[str]:
    """Operator-readable rows. NEVER raises.

    Leads with DROPPED hooks because those are defects in the cockpit itself,
    not gaps in a surface's coverage of it — a dropped hook means a feature is
    dark for every caller, including the shipping one.
    """
    out: List[str] = []
    try:
        if not reading.sinks:
            return ["  no hook-shaped sinks found (audit disabled or "
                    "threshold too high)"]
        out.append(f"  {len(reading.sinks)} sinks · {len(reading.hooks)} hooks "
                   f"· {len(reading.surfaces)} calling surfaces")
        for module in reading.unparseable:
            out.append(f"  ⚠ unparseable, skipped: {module}")
        out.append("")

        dropped = reading.dropped()
        if dropped:
            out.append(f"  ✗ ACCEPTED AND DROPPED — dark for every caller "
                       f"({len(dropped)}):")
            for hook in dropped[:limit]:
                out.append(f"      {hook.short_sink}({hook.name}) "
                           f"— accepted, never read")
            out.append("")
        else:
            out.append("  ✓ every hook a sink accepts is consumed, forwarded "
                       "to a consumer, or declared discarded")
            out.append("")

        diverged = reading.divergence()
        if diverged:
            out.append(f"  · one surface proves it, another has not decided "
                       f"({len(diverged)}):")
            for sink, hook, filled, unset in diverged[:limit]:
                fills = ", ".join(s.rsplit(".", 1)[-1] for s in filled)
                gaps = ", ".join(s.rsplit(".", 1)[-1] for s in unset)
                out.append(f"      {sink.rsplit('.', 1)[-1]}({hook})")
                out.append(f"          filled by {fills} · unset in {gaps}")
            if len(diverged) > limit:
                out.append(f"      … {len(diverged) - limit} more")
            out.append("")

        for surface in reading.surfaces:
            ok, total = reading.coverage(surface)
            short = surface.rsplit(".", 1)[-1]
            mark = "✓" if ok == total else "·"
            out.append(f"  {mark} {short:<24} {ok}/{total} accounted for")

        waivers = reading.waivers()
        if waivers:
            out.append("")
            out.append(f"  declared waivers ({len(waivers)}):")
            for fill in waivers[:limit]:
                short = fill.surface.rsplit(".", 1)[-1]
                out.append(f"      {short}·{fill.hook} — {fill.reason}")
        out.append("")
        out.append("  a WAIVED hook is accounted for. The number to drive to")
        out.append("  zero is hooks nobody has decided about, not hooks unfilled.")
    except Exception:  # noqa: BLE001
        logger.debug("[CapabilityHandoff] render degraded", exc_info=True)
    return out


__all__ = [
    "CAPABILITY_HANDOFF_SCHEMA_VERSION",
    "Consumption",
    "Fill",
    "FillState",
    "HandoffReading",
    "Hook",
    "SinkSpec",
    "analyse_sink",
    "analyse_surface",
    "audit",
    "discover_sinks",
    "effective_consumption",
    "handoff_enabled",
    "render",
    "waived",
]
