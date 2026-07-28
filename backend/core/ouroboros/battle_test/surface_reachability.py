"""Which of `ov`'s surfaces can actually reach a module — the recurring blind spot.

Five modules in a row have shipped complete, tested, and invisible on the
surface the operator uses:

    agent_roster        `render()` had zero production callers
    transcript_search   complete, 21 tests, never bound to a key
    live_status_line    wired into the daemon REPL only
    rewind_menu         reached from `ov.py` only
    op_fanout_tree      default-FALSE, reachable via one verb

The progress board reports all of them LIVE, and it is right by its own
measure: every one is imported by something. What it cannot see is WHICH
SURFACE imports it, and `ov` has three that render:

    serpent_flow        the daemon's flowing REPL
    ov.AttachUI         the attach client's PromptSession fallback
    bipartite_layout    the cockpit both of them mount

A module reachable from exactly one of those is not a defect — but it is the
shape every one of those five had, and nothing in the repo could see it. So
this measures it: transitive import reachability from each surface's entry,
over the same AST graph the board already builds.

A signal, not a verdict
-----------------------
Asymmetry is evidence, never a conclusion, and the difference matters because
acting on it blindly would be the same mistake in the other direction.
`transcript_hatches` is reached from `ov.py` alone and is nonetheless LIVE on
the cockpit, because `ov.py` installs its bindings into the `extra_key_bindings`
the cockpit is built with. Reachability sees imports; it cannot see that a
KeyBindings object was handed across.

So this reports asymmetry and says what it means. The same stance the board
takes on `dark`: a place to look, not a count of bugs.

Lazy imports count
------------------
This codebase imports inside functions constantly — `try: from … import x`
guarded per call site. `ast.walk` sees those, and it should: a lazy import IS
a reachable edge, and treating only module-level imports as real would report
most of the cockpit as unreachable from anything.

Never raises, never imports the code it measures. Pure AST over source text —
the audit must be safe to run against a module that would explode on import,
because those are exactly the ones worth auditing.
"""
from __future__ import annotations

import ast
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, FrozenSet, List, Mapping, Optional, Set, Tuple

logger = logging.getLogger("Ouroboros.SurfaceReachability")

SURFACE_REACHABILITY_SCHEMA_VERSION = "surface_reachability.v1"

MASTER_FLAG_ENV_VAR = "JARVIS_SURFACE_AUDIT_ENABLED"
SURFACES_ENV_VAR = "JARVIS_SURFACE_AUDIT_SURFACES"
ROOTS_ENV_VAR = "JARVIS_SURFACE_AUDIT_ROOTS"

#: surface label → entry module. The three places `ov` draws from.
#:
#: Overridable via ``JARVIS_SURFACE_AUDIT_SURFACES`` as
#: ``label=dotted.module,label=dotted.module`` — a fourth surface is a
#: config change, not a code change, and this table has grown once already.
_DEFAULT_SURFACES: Tuple[Tuple[str, str], ...] = (
    ("daemon", "backend.core.ouroboros.battle_test.serpent_flow"),
    ("attach", "backend.core.ouroboros.cli.ov"),
    ("cockpit", "backend.core.ouroboros.battle_test.bipartite_layout"),
)

#: Where a module has to live to be worth auditing. Reachability into the
#: governance core is a different question with a different answer.
_DEFAULT_ROOTS: Tuple[str, ...] = (
    "backend/core/ouroboros/battle_test",
    "backend/core/ouroboros/ui",
    "backend/core/ouroboros/cli",
)


def audit_enabled() -> bool:
    """Default ON. This reads source; it never imports or executes it."""
    return os.environ.get(
        MASTER_FLAG_ENV_VAR, "1",
    ).strip().lower() not in ("0", "false", "no", "off")


def surfaces() -> Tuple[Tuple[str, str], ...]:
    """``((label, entry_module), …)`` — the rendering surfaces to measure."""
    raw = os.environ.get(SURFACES_ENV_VAR, "").strip()
    if not raw:
        return _DEFAULT_SURFACES
    out: List[Tuple[str, str]] = []
    for chunk in raw.split(","):
        if "=" in chunk:
            label, module = chunk.split("=", 1)
            label, module = label.strip(), module.strip()
            if label and module:
                out.append((label, module))
    return tuple(out) or _DEFAULT_SURFACES


def audit_roots() -> Tuple[str, ...]:
    raw = os.environ.get(ROOTS_ENV_VAR, "").strip()
    if not raw:
        return _DEFAULT_ROOTS
    return tuple(p.strip() for p in raw.split(",") if p.strip()) or _DEFAULT_ROOTS


# ---------------------------------------------------------------------------
# The graph
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModuleReach:
    """One module and the surfaces that can reach it."""

    module: str
    path: str
    reached_by: FrozenSet[str]

    @property
    def short(self) -> str:
        return self.module.rsplit(".", 1)[-1]

    @property
    def orphan(self) -> bool:
        """No surface reaches it. Either dead, or reached some way the import
        graph cannot see — a plugin registry, a dynamic import, a verb table."""
        return not self.reached_by

    def asymmetric(self, total: int) -> bool:
        """Reached by SOME surfaces but not all — the recurring shape."""
        return 0 < len(self.reached_by) < total


@dataclass
class SurfaceReading:
    """The whole audit, as data. Rendering is a separate concern."""

    modules: List[ModuleReach] = field(default_factory=list)
    surface_labels: Tuple[str, ...] = ()
    scanned: int = 0
    unresolved_entries: Tuple[str, ...] = ()
    #: Modules that IMPORT a surface — boot paths, above the graph.
    roots: FrozenSet[str] = frozenset()
    schema_version: str = SURFACE_REACHABILITY_SCHEMA_VERSION

    def asymmetric(self) -> List[ModuleReach]:
        n = len(self.surface_labels)
        return sorted(
            (m for m in self.modules if m.asymmetric(n)),
            key=lambda m: (len(m.reached_by), m.module),
        )

    def orphans(self) -> List[ModuleReach]:
        """Unreached AND not a boot path. `harness` reaches every surface and
        nothing reaches it; calling that an orphan would bury the real ones
        under the entry points of the program."""
        return sorted(
            (m for m in self.modules
             if m.orphan and m.module not in self.roots),
            key=lambda m: m.module,
        )

    def reached_by(self, label: str) -> List[ModuleReach]:
        return sorted((m for m in self.modules if label in m.reached_by),
                      key=lambda m: m.module)


def _repo_root() -> Path:
    try:
        from backend.core.ouroboros.battle_test.progress_board import (
            _default_root,
        )
        return _default_root()
    except Exception:  # noqa: BLE001
        return Path(__file__).resolve().parents[4]


def _index(roots: Tuple[str, ...]) -> Dict[str, Path]:
    """``dotted module → source path`` for everything under ``roots``."""
    out: Dict[str, Path] = {}
    base = _repo_root()
    for root in roots:
        directory = base / root
        if not directory.is_dir():
            continue
        for path in directory.rglob("*.py"):
            try:
                rel = path.relative_to(base).as_posix()
            except ValueError:
                continue
            if "/tests/" in f"/{rel}" or rel.split("/")[-1].startswith("test_"):
                continue
            try:
                from backend.core.ouroboros.battle_test.progress_board import (
                    _module_name,
                )
                out[_module_name(rel)] = path
            except Exception:  # noqa: BLE001
                continue
    return out


def _edges(path: Path) -> Set[str]:
    """Modules this file imports — module-level AND inside functions.

    Reuses the board's extractor rather than writing a second one: two AST
    walkers over the same syntax would eventually disagree about which
    import shapes count, and the disagreement would be invisible.
    """
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
    except Exception:  # noqa: BLE001 — a file that will not parse has no edges
        return set()
    try:
        from backend.core.ouroboros.battle_test.progress_board import (
            _imported_modules,
        )
        return set(_imported_modules(tree))
    except Exception:  # noqa: BLE001
        return set()


def _reach(
    entry: str,
    index: Mapping[str, Path],
    barriers: FrozenSet[str] = frozenset(),
) -> Set[str]:
    """Modules reachable from ``entry`` WITHOUT passing through a barrier.

    The barriers are the other surfaces, and they are the whole reason this
    function is not a plain transitive closure.

    The first version was one, and it reported that every module is reachable
    from every surface — which is true and useless. The three surfaces form a
    CYCLE (`serpent_flow` imports `bipartite_layout`; `ov` imports both), so
    closure from any one of them swallows the other two and everything they
    touch. The asymmetry this exists to find collapsed to zero, and the
    instrument would have confidently reported a clean bill of health for the
    exact defect that motivated it.

    Cutting at the boundary asks the question that actually matters: does THIS
    surface reach the module on its own, or only by way of another surface? A
    module the cockpit can only see through `ov.py` is precisely the shape of
    `live_status_line` and `rewind_menu`.

    Iterative with an explicit stack: the graph is cyclic even after the cuts,
    and a recursive walk would blow the stack on the first loop.
    """
    seen: Set[str] = set()
    stack: List[str] = [entry]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        # A barrier is REACHED but never traversed: knowing the cockpit
        # imports `ov` is true; inheriting everything `ov` imports is what
        # destroyed the signal.
        if current in barriers and current != entry:
            continue
        path = index.get(current)
        if path is None:
            continue
        for target in _edges(path):
            # Only follow edges that stay in scope. Third-party and
            # governance-core imports are real, and out of this question.
            if target in index and target not in seen:
                stack.append(target)
    return seen


def _roots(index: Mapping[str, Path], entries: FrozenSet[str]) -> Set[str]:
    """Modules that import a SURFACE — boot paths, not orphans.

    `harness` reaches every surface; nothing reaches `harness`. Listing it as
    unreachable would be technically true and would bury the real orphans
    under the entry points of the program.
    """
    out: Set[str] = set()
    for module, path in index.items():
        if module in entries:
            continue
        if _edges(path) & entries:
            out.add(module)
    return out


def audit(
    *,
    roots: Optional[Tuple[str, ...]] = None,
    entries: Optional[Tuple[Tuple[str, str], ...]] = None,
) -> SurfaceReading:
    """Measure per-surface reachability. NEVER raises.

    The entry modules themselves are excluded from the report — a surface
    trivially reaches itself, and three rows saying so would be the loudest
    thing in the output.
    """
    reading = SurfaceReading()
    try:
        if not audit_enabled():
            return reading
        scope = roots or audit_roots()
        table = entries or surfaces()
        index = _index(scope)
        reading.scanned = len(index)
        reading.surface_labels = tuple(label for label, _ in table)

        missing: List[str] = []
        reach: Dict[str, Set[str]] = {}
        entry_set = frozenset(entry for _, entry in table)
        for label, entry in table:
            if entry not in index:
                # An entry outside the scanned roots cannot be walked. Said
                # out loud rather than silently contributing an empty set —
                # which would mark every module unreachable from it and read
                # as a catastrophic finding.
                missing.append(f"{label}={entry}")
                continue
            reach[label] = _reach(entry, index, barriers=entry_set)
        reading.unresolved_entries = tuple(missing)
        reading.roots = frozenset(_roots(index, entry_set))

        entry_modules = entry_set
        for module in sorted(index):
            if module in entry_modules:
                continue
            hit = frozenset(
                label for label, reached in reach.items() if module in reached
            )
            reading.modules.append(ModuleReach(
                module=module,
                path=index[module].as_posix(),
                reached_by=hit,
            ))
        return reading
    except Exception:  # noqa: BLE001
        logger.debug("[SurfaceReach] audit degraded", exc_info=True)
        return reading


# ---------------------------------------------------------------------------
# Rendering — a separate concern, as the board keeps its own
# ---------------------------------------------------------------------------


def render(reading: SurfaceReading, *, limit: int = 20) -> List[str]:
    """Operator-readable rows. NEVER raises.

    Leads with the ASYMMETRIC modules because they are the finding; the
    fully-reachable majority is the uninteresting background this exists to
    filter out.
    """
    out: List[str] = []
    try:
        labels = reading.surface_labels
        if not labels:
            return ["  surface audit unavailable"]
        for entry in reading.unresolved_entries:
            out.append(f"  ⚠ entry outside the scanned roots: {entry}")

        asym = reading.asymmetric()
        out.append(
            f"  {reading.scanned} modules · {len(labels)} surfaces "
            f"({' · '.join(labels)})"
        )
        out.append("")
        if not asym:
            out.append("  every module is reachable from every surface")
        else:
            out.append(f"  reachable from SOME surfaces, not all "
                       f"({len(asym)}):")
            width = min(38, max((len(m.short) for m in asym), default=20))
            for row in asym[:limit]:
                marks = " ".join(
                    ("✓" if label in row.reached_by else "·") for label in labels
                )
                out.append(f"    {row.short:<{width}}  {marks}")
            if len(asym) > limit:
                out.append(f"    … {len(asym) - limit} more")

        orphans = reading.orphans()
        if orphans:
            out.append("")
            out.append(f"  reached by NO surface ({len(orphans)}):")
            for row in orphans[:limit]:
                out.append(f"    ◇ {row.short}")
            if len(orphans) > limit:
                out.append(f"    … {len(orphans) - limit} more")

        out.append("")
        out.append("  asymmetry is a SIGNAL, not a defect count: bindings and")
        out.append("  callables handed across a surface boundary are invisible")
        out.append("  to an import graph. A place to look, not a verdict.")
        return out
    except Exception:  # noqa: BLE001
        logger.debug("[SurfaceReach] render degraded", exc_info=True)
        return out or ["  surface audit unavailable"]


__all__ = [
    "MASTER_FLAG_ENV_VAR",
    "ROOTS_ENV_VAR",
    "SURFACES_ENV_VAR",
    "SURFACE_REACHABILITY_SCHEMA_VERSION",
    "ModuleReach",
    "SurfaceReading",
    "audit",
    "audit_enabled",
    "audit_roots",
    "render",
    "surfaces",
]
