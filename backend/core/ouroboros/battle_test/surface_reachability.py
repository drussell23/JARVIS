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
import keyword
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import (Dict, FrozenSet, Iterable, List, Mapping, Optional,
                    Set, Tuple, Union)

logger = logging.getLogger("Ouroboros.SurfaceReachability")

SURFACE_REACHABILITY_SCHEMA_VERSION = "surface_reachability.v1"

MASTER_FLAG_ENV_VAR = "JARVIS_SURFACE_AUDIT_ENABLED"
SURFACES_ENV_VAR = "JARVIS_SURFACE_AUDIT_SURFACES"
ROOTS_ENV_VAR = "JARVIS_SURFACE_AUDIT_ROOTS"
TRAVERSAL_ENV_VAR = "JARVIS_SURFACE_AUDIT_TRAVERSAL"

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


def traversal_root() -> str:
    """Where EDGES may run, as distinct from where findings are REPORTED.

    These were one scope, and conflating them severed the graph. ``_reach``
    follows an edge only when the target is indexed, so any import that left
    the three reported roots and came back was invisible —
    ``transcript_timeline`` was called an orphan while ``why_engine`` imports
    it, because ``why_engine`` lives in ``governance/`` and governance was
    not indexed. The audit was measuring a subgraph and reporting about the
    graph.

    Reporting scope stays narrow on purpose: reachability into the governance
    core is a different question with a different answer, and answering it
    here would bury the surface finding under a thousand rows. But a
    TRAVERSAL that stops at the same boundary manufactures orphans out of
    round trips, which is the strictly worse error — it invents deaths.

    Derived from this module's own location rather than written down, so a
    package move cannot leave a stale literal behind.
    """
    raw = os.environ.get(TRAVERSAL_ENV_VAR, "").strip()
    if raw:
        return raw
    try:
        return Path(__file__).resolve().parent.parent.relative_to(
            _repo_root()).as_posix()
    except Exception:  # noqa: BLE001
        return "backend/core/ouroboros"


def _is_importable(module: str) -> bool:
    """Can any ``import`` statement in any file spell this name?

    ``audio_pump 2.py`` — a Finder/iCloud duplicate — indexes as the module
    ``…ui.audio_pump 2``, which contains a space. No import statement can
    name it, so its unreachability is a TAUTOLOGY rather than a finding, and
    five of eight reported orphans were files of exactly this kind.

    Filtering on the property that makes them unreachable, rather than on the
    string that happens to produce them today, is what keeps this general: a
    ``copy.py``, a ``foo-bar.py``, a leading-digit name and a name colliding
    with a keyword all fail for the same structural reason and are all
    excluded by the same test.

    Deliberately NOT a gitignore query. An ignored-but-importable module is
    still importable, so excluding it would hide a real edge; and measured
    against this tree the ignore rules add exactly zero exclusions the
    identifier test does not already make. A subprocess, a timeout and a
    degraded mode for zero cases is machinery, not rigour.
    """
    parts = module.split(".")
    return bool(parts) and all(
        p.isidentifier() and not keyword.iskeyword(p) for p in parts)


@dataclass
class _Scan:
    """One audit's parse and edge memo. Created per call; never shared.

    ``_edges`` is pure over file content, and the audit calls it once per
    node per entry walk. Widening traversal to the whole package takes that
    from ~800 parses to ~30,000, so the memo is what makes an honest
    traversal scope affordable rather than a trade against it.

    Per-call rather than module-level: a cache that outlived the call would
    answer tomorrow's audit with yesterday's source, and an audit that cannot
    see an edit is worse than a slow one. It is also the reason this needs no
    lock — two concurrent audits share nothing.
    """

    trees: Dict[Path, Optional[ast.AST]] = field(default_factory=dict)
    edges: Dict[Tuple[str, str], FrozenSet[str]] = field(default_factory=dict)

    def tree(self, path: Path) -> Optional[ast.AST]:
        if path in self.trees:
            return self.trees[path]
        try:
            parsed: Optional[ast.AST] = ast.parse(
                path.read_text(encoding="utf-8", errors="replace"))
        except Exception:  # noqa: BLE001 — a file that will not parse has none
            parsed = None
        self.trees[path] = parsed
        return parsed


#: Where the package DECLARES its console entry points. Read rather than
#: transcribed: a script added tomorrow becomes a root with no edit here.
_PYPROJECT = "pyproject.toml"

#: Scripts invoked directly rather than imported. The battle-test harness is
#: reached this way and reaches a large subtree nothing else does.
_SCRIPT_DIR = "scripts"


def _pyproject_entry_modules(base: "Path") -> List[str]:
    """Dotted modules named by ``[project.scripts]``. NEVER raises.

    ``ov = "backend.core.ouroboros.cli.ov:main"`` → the module before the
    colon. Parsed with the stdlib TOML reader when available and by a narrow
    line match when not, because this must work on 3.9 where `tomllib` does
    not exist and the audit is not worth a dependency.
    """
    out: List[str] = []
    try:
        path = base / _PYPROJECT
        if not path.is_file():
            return []
        text = path.read_text(encoding="utf-8", errors="replace")
        try:
            import tomllib          # 3.11+
            table = tomllib.loads(text).get("project", {}).get("scripts", {})
            for target in table.values():
                mod = str(target).split(":", 1)[0].strip()
                if mod:
                    out.append(mod)
            return out
        except Exception:  # noqa: BLE001 — fall through to the line reader
            pass
        in_scripts = False
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("["):
                in_scripts = line == "[project.scripts]"
                continue
            if not in_scripts or "=" not in line or line.startswith("#"):
                continue
            value = line.split("=", 1)[1].strip().strip('"\'')
            mod = value.split(":", 1)[0].strip()
            if mod:
                out.append(mod)
    except Exception:  # noqa: BLE001
        return out
    return out


def _main_guarded(index: Mapping[str, "Path"],
                  scan: Optional["_Scan"] = None) -> List[str]:
    """Modules with an ``if __name__ == "__main__":`` block.

    A module that can be run is a module that can be entered, and nothing
    in-tree needs to import it for that to be true.

    Two-stage, and the first stage is EXACT rather than a heuristic: a run
    guard is a comparison against the literal ``"__main__"``, which cannot be
    written without those bytes appearing in the source. So a file lacking
    them provably has no guard, and reading 1522 files to reject 1489 of them
    costs a fraction of parsing 1522 to find 28 — the difference between an
    honest whole-package traversal being affordable and being a 15-second
    stall. Survivors are still confirmed by the AST; the bytes only narrow.

    The obvious pre-filter — ``__name__`` — is worthless here and measuring
    said so: 883 of 1522 files contain it, because
    ``logging.getLogger(__name__)`` is in almost every module. The literal
    that makes a module RUNNABLE is ``__main__``, and that is in 33.

    Matching that literal also TIGHTENS the AST test, which previously
    accepted any ``__name__ ==`` comparison. "This module can be run" means
    the interpreter set ``__name__`` to ``"__main__"``; a comparison against
    anything else is not that claim.
    """
    scan = scan or _Scan()
    out: List[str] = []
    for module, path in index.items():
        try:
            if _RUN_GUARD_LITERAL.encode() not in path.read_bytes():
                continue
        except OSError:
            continue
        tree = scan.tree(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            if _is_run_guard(node.test):
                out.append(module)
                break
    return out


#: What the interpreter sets ``__name__`` to for the module it was asked to
#: run. Named once: the byte pre-filter and the AST confirmation must agree,
#: and two copies of a magic string are two chances for them to diverge.
_RUN_GUARD_LITERAL = "__main__"


def _is_run_guard(test: ast.AST) -> bool:
    """``__name__ == "__main__"``, in either order. NEVER raises."""
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return False
    if not isinstance(test.ops[0], ast.Eq):
        return False
    sides = (test.left, test.comparators[0])
    names = {n.id for n in sides if isinstance(n, ast.Name)}
    literals = {n.value for n in sides
                if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    return "__name__" in names and _RUN_GUARD_LITERAL in literals


def _cage_mounted(index: Mapping[str, "Path"]) -> List[str]:
    """Modules the naming cage mounts as REPL verbs. NEVER raises.

    THE BLIND SPOT THIS INSTRUMENT WOULD OTHERWISE HAVE ACQUIRED. A
    ``governance/<verb>_repl.py`` that exports ``__verb_help__`` and
    ``dispatch_<verb>_command`` is the verb ``/<verb>``, and the cage reaches
    it with ``importlib.import_module`` — an edge no AST walker can see. The
    fix that made those modules reachable by a human therefore made them
    unreachable by this audit, and every one of them, plus its whole
    subtree, would have started reporting as an orphan.

    That is the fourth import shape this audit could not spell, after entry
    points, ``scripts/`` and relative imports — and the same disease each
    time: an edge the extractor cannot see becomes an absence it reports as
    a finding.

    A dynamic import is legible here only because the cage DECLARES its
    mounts: ``discover()`` is a static AST scan that imports nothing, so
    this asks the cage what it will mount rather than guessing. A dynamic
    import with no declaration remains invisible, and correctly so — nothing
    in the repo could see it either.
    """
    try:
        from backend.core.ouroboros.governance.repl_verb_cage import discover

        return [spec.module for spec in discover() if spec.module in index]
    except Exception:  # noqa: BLE001 — an absent cage is not a finding
        return []


def _script_reached(base: "Path", index: Mapping[str, "Path"],
                    scan: Optional["_Scan"] = None) -> List[str]:
    """Modules imported by a top-level ``scripts/*.py``.

    THE case that dominated this audit. ``scripts/ouroboros_battle_test.py``
    imports `harness`, which boots the six-layer stack — so the harness and
    everything it owns (termination hooks, session recording, watchdogs,
    preflight, telemetry) sits at the head of a large subtree.

    The direction is what made it invisible: the harness BOOTS the rendering
    surfaces, and a surface never imports the thing that started it. Measuring
    reachability from the surfaces alone therefore reported an entire
    entry-point's worth of live code as unreachable.
    """
    out: List[str] = []
    try:
        directory = base / _SCRIPT_DIR
        if not directory.is_dir():
            return []
        for path in sorted(directory.glob("*.py")):
            for edge in _edges(path, scan=scan):
                if edge in index:
                    out.append(edge)
    except Exception:  # noqa: BLE001
        return out
    return out


def derived_entries(base: "Path",
                    index: Mapping[str, "Path"],
                    scan: Optional["_Scan"] = None,
                    ) -> Tuple[Tuple[str, str], ...]:
    """Entry points DISCOVERED rather than transcribed. NEVER raises.

    The audit measured three RENDERING surfaces, which is a fair question and
    the wrong root set for "is this module dead". The package declares three
    console scripts, ships modules that run themselves, and boots its primary
    workload from ``scripts/``; of 39 reported orphans, **32 were reachable
    from an entry point the audit did not know about**.

    Adding those three names to `_DEFAULT_SURFACES` would have fixed today's
    number and rotted on the next script. Each source here is a structural
    fact — a declaration in ``pyproject.toml``, a ``__main__`` guard, an
    import from ``scripts/`` — so the root set tracks the package.
    """
    out: List[Tuple[str, str]] = []
    seen = set()
    for label, modules in (
        ("script", _pyproject_entry_modules(base)),
        ("runnable", _main_guarded(index, scan)),
        ("soak", _script_reached(base, index, scan)),
        ("verb", _cage_mounted(index)),
    ):
        for module in modules:
            if module in index and module not in seen:
                seen.add(module)
                out.append((label, module))
    return tuple(out)


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
    #: Reachable from ANY entry point by a plain closure. Distinct from
    #: `reached_by`, which is per-surface and barriered: that answers "who
    #: reaches it first", this answers "is it alive at all".
    entry_reachable: FrozenSet[str] = frozenset()
    schema_version: str = SURFACE_REACHABILITY_SCHEMA_VERSION

    def asymmetric(self) -> List[ModuleReach]:
        n = len(self.surface_labels)
        return sorted(
            (m for m in self.modules if m.asymmetric(n)),
            key=lambda m: (len(m.reached_by), m.module),
        )

    def orphans(self) -> List[ModuleReach]:
        """Unreached by any SURFACE, any ENTRY POINT, and not a boot path.

        `harness` reaches every surface and nothing reaches it; calling that an
        orphan would bury the real ones under the entry points of the program.

        ``entry_reachable`` is the correction that mattered most. The surface
        walk answers "does the cockpit reach this on its own", which needs the
        other surfaces as barriers — and that same barrier makes it the wrong
        instrument for "is this dead". Of 39 modules once reported orphaned,
        **32 were reachable from an entry point the audit never knew about**:
        three console scripts declared in ``pyproject.toml``, modules that run
        themselves, and the battle-test harness that ``scripts/`` boots. The
        harness case is the instructive one — it BOOTS the rendering surfaces,
        and a surface never imports what started it, so an entire entry
        point's subtree read as unreachable."""
        return sorted(
            (m for m in self.modules
             if m.orphan and m.module not in self.roots
             and m.module not in self.entry_reachable),
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
                module = _module_name(rel)
            except Exception:  # noqa: BLE001
                continue
            # A name no import statement can spell is unreachable by
            # construction, so reporting it is a tautology rather than a
            # finding. See `_is_importable`.
            if not _is_importable(module):
                continue
            out[module] = path
    return out


def _edges(path: Path, module: str = "",
           scan: Optional["_Scan"] = None) -> Set[str]:
    """Modules this file imports — module-level AND inside functions.

    Reuses the canonical extractor rather than writing a second one: two AST
    walkers over the same syntax would eventually disagree about which import
    shapes count, and the disagreement would be invisible.

    ``module`` is the importing module's dotted name, required to resolve
    relative imports against its package.
    """
    key = (path.as_posix(), module)
    if scan is not None:
        memo = scan.edges.get(key)
        if memo is not None:
            return set(memo)
    tree = (scan or _Scan()).tree(path)
    if tree is None:
        # A file that will not parse has no edges. Memoised too — a broken
        # file is re-visited once per entry walk, and re-reading it each time
        # is the same wasted work as re-parsing a good one.
        if scan is not None:
            scan.edges[key] = frozenset()
        return set()
    try:
        # `reverse_dep_resolver` calls itself "THE single import extractor"
        # and is the only one here that RESOLVES RELATIVE IMPORTS.
        #
        # The board's extractor returns the bare tail for
        # ``from .wake_sequence import X`` — a name matching no indexed
        # module, so the edge dangles and the target reads as unreachable.
        # `wake_sequence` was reported orphaned on exactly that basis while
        # `awakening.py` imports it on line 45.
        #
        # Third import shape this audit could not see, after entry points and
        # `scripts/`. Same disease every time: an edge the extractor cannot
        # spell becomes an absence it reports as a finding.
        from backend.core.ouroboros.governance.reverse_dep_resolver import (
            extract_module_imports,
        )
        found = set(extract_module_imports(
            tree, module, path.name == "__init__.py"))
    except Exception:  # noqa: BLE001
        try:
            from backend.core.ouroboros.battle_test.progress_board import (
                _imported_modules,
            )
            found = set(_imported_modules(tree))
        except Exception:  # noqa: BLE001
            found = set()
    if scan is not None:
        scan.edges[key] = frozenset(found)
    return found


def _reach(
    entry: "Union[str, Iterable[str]]",
    index: Mapping[str, Path],
    barriers: FrozenSet[str] = frozenset(),
    scan: Optional["_Scan"] = None,
) -> Set[str]:
    """Modules reachable from ``entry`` WITHOUT passing through a barrier.

    ``entry`` may be one module or many. MANY IS NOT A CONVENIENCE: the
    orphan question is the union of the closures from 228 discovered entry
    points, and computing it one entry at a time re-walks a 1500-node graph
    228 times for an answer a single multi-source walk gives exactly — the
    seeded frontier IS the union, by definition of reachability. That is a
    complexity fix (O(V+E) rather than O(entries·(V+E))), not a cache trick,
    and it is what makes an honest whole-package traversal affordable.

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
    seeds: Tuple[str, ...] = (
        (entry,) if isinstance(entry, str) else tuple(entry))
    seed_set = frozenset(seeds)
    seen: Set[str] = set()
    stack: List[str] = list(seeds)
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        # A barrier is REACHED but never traversed: knowing the cockpit
        # imports `ov` is true; inheriting everything `ov` imports is what
        # destroyed the signal. A seed is never its own barrier — otherwise
        # a surface would stop at its own front door.
        if current in barriers and current not in seed_set:
            continue
        path = index.get(current)
        if path is None:
            continue
        for target in _edges(path, current, scan):
            # Only follow edges that stay in scope. Third-party and
            # governance-core imports are real, and out of this question.
            if target in index and target not in seen:
                stack.append(target)
    return seen


def _roots(index: Mapping[str, Path], entries: FrozenSet[str],
           scan: Optional["_Scan"] = None) -> Set[str]:
    """Modules that import a SURFACE — boot paths, not orphans.

    `harness` reaches every surface; nothing reaches `harness`. Listing it as
    unreachable would be technically true and would bury the real orphans
    under the entry points of the program.

    Scoped to the REPORTED index rather than the traversal graph, because the
    only consumer is ``orphans()``, which iterates reported rows. Classifying
    a governance module as a boot path answers a question nobody asks, at the
    cost of an edge extraction over a thousand modules.
    """
    out: Set[str] = set()
    for module, path in index.items():
        if module in entries:
            continue
        if _edges(path, module, scan) & entries:
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
        scan = _Scan()
        # TWO SCOPES, deliberately different (see `traversal_root`).
        #
        # `graph` is where edges may run — the whole package, so an import
        # that leaves the reported roots and comes back is followed rather
        # than severed. `index` is what gets a row, and stays narrow so the
        # surface finding is not buried under a thousand governance modules.
        #
        # Fused, they manufactured orphans out of round trips. Widening the
        # REPORT instead would have been the other error: it answers a
        # different question loudly and drowns this one.
        graph = _index((traversal_root(),))
        index = _index(scope)
        # Reported rows must be walkable. Normally a superset; if an operator
        # points `JARVIS_SURFACE_AUDIT_ROOTS` outside the package, the union
        # keeps those rows measurable instead of silently all-orphan.
        for module, path in index.items():
            graph.setdefault(module, path)
        # SURFACES ONLY. Entry points are deliberately NOT added here.
        #
        # `_reach` treats every other surface as a BARRIER — reached but not
        # traversed — because a plain closure "reported that every module is
        # reachable from every surface, which is true and useless". Feeding
        # the package's entry points into this table therefore does the
        # opposite of what it looks like: each new entry becomes another wall,
        # every surface's reach SHRINKS, and the orphan count goes UP. Adding
        # 20 discovered entries here took 39 orphans to 23 the wrong way, by
        # making live modules look less reachable rather than more.
        #
        # Two different questions were being answered by one number:
        #
        #   ASYMMETRY  — does THIS surface reach it on its own? Needs the
        #                barriers, and is what `_DEFAULT_SURFACES` is for.
        #   ORPHANHOOD — does ANY entry point reach it at all? Needs a plain
        #                closure over every entry, and barriers would be
        #                actively wrong.
        #
        # They are computed separately below.
        table = entries or surfaces()
        reading.scanned = len(index)
        reading.surface_labels = tuple(label for label, _ in table)

        missing: List[str] = []
        reach: Dict[str, Set[str]] = {}
        entry_set = frozenset(entry for _, entry in table)
        for label, entry in table:
            if entry not in graph:
                # An entry outside the scanned roots cannot be walked. Said
                # out loud rather than silently contributing an empty set —
                # which would mark every module unreachable from it and read
                # as a catastrophic finding.
                missing.append(f"{label}={entry}")
                continue
            reach[label] = _reach(entry, graph, barriers=entry_set, scan=scan)
        reading.unresolved_entries = tuple(missing)
        reading.roots = frozenset(_roots(index, entry_set, scan))

        # ORPHANHOOD, computed separately and WITHOUT barriers. A module is
        # dead only if no entry point reaches it at all; which particular
        # surface got there first is the asymmetry question, answered above.
        entry_reachable: Set[str] = set()
        try:
            seeds = [e for _, e in derived_entries(_repo_root(), graph, scan)]
            seeds += [e for _, e in table if e in graph]
            entry_reachable = _reach(
                seeds, graph, barriers=frozenset(), scan=scan)
        except Exception:  # noqa: BLE001 — a failed walk must not invent deaths
            entry_reachable = set()
        # A PACKAGE is reachable if any of its modules is. Nothing imports
        # `backend.core.ouroboros.cli` by name — importing
        # `...cli.ov` executes the package `__init__` implicitly — so an
        # `__init__.py` is structurally invisible to an import-edge walk and
        # was reported as an orphan on that basis alone.
        for module in list(entry_reachable):
            parts = module.split(".")
            for depth in range(1, len(parts)):
                entry_reachable.add(".".join(parts[:depth]))
        reading.entry_reachable = frozenset(entry_reachable)

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


async def audit_async(
    *,
    roots: Optional[Tuple[str, ...]] = None,
    entries: Optional[Tuple[Tuple[str, str], ...]] = None,
) -> SurfaceReading:
    """:func:`audit`, off the event loop. NEVER raises.

    The walk parses roughly a thousand files and takes seconds — far too
    much for the loop that also runs the organism, and Principle 3 does not
    have an exception for diagnostics. The offload lives HERE, in the module
    that knows the work is heavy, rather than in each caller: `run_watchdog`
    remembered to wrap it in a thread and the `/reach` REPL path did not, so
    typing the verb froze the daemon for the duration of its own audit.

    ``to_thread`` rather than a process: the cost is `ast.parse`, which holds
    the GIL, so this buys RESPONSIVENESS rather than parallelism — the loop
    keeps serving while the scan runs. That is the property being bought.
    """
    import asyncio

    return await asyncio.to_thread(audit, roots=roots, entries=entries)


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
    "audit_async",
    "audit_enabled",
    "audit_roots",
    "render",
    "surfaces",
]
