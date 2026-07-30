"""Where `ov` actually stands — derived, never listed.

Thirty-odd features shipped behind `JARVIS_*` flags. Some default ON, some
OFF, and some were merged with **no live caller at all**. Today there is no
way to ask "what is actually running when I type `ov`?" short of reading
commit history, and commit history says what was *merged*, not what *runs*.

A hand-maintained checklist cannot answer it either: it would be written once,
drift within a week, and then confidently report features that no longer exist.
So nothing here is enumerated by hand. The board is a READING of two registries
that already exist —

  * ``governance/flag_registry.py`` — every flag with its type, default,
    category and ``source_file``
  * ``battle_test/repl_dispatch_registry.py`` — the verbs actually primed

— crossed with an import-graph scan of the tree.

The distinction that makes this worth building
-----------------------------------------------
"The flag is ON" and "the code runs" are DIFFERENT QUESTIONS, and conflating
them is the single most repeated defect in this codebase's recent history: a
module imported that never existed, two producers with no consumer, a completer
implementing the one method the library never calls, a fake modelling a
superseded cancel surface. Every one passed its own tests. Every one was
"merged and enabled".

So a flag being ON is not evidence. `DARK` is the state this board exists to
name: **enabled, present on disk, and imported by nothing in production.**
Merged, flagged on, and inert. That state is invisible everywhere else.

Provenance over confidence
--------------------------
Following the discipline `advisor_locality` established: a state that cannot
be MEASURED is reported as ``UNKNOWN``, never guessed. A flag whose
``source_file`` is blank or unresolvable does not get a hopeful `LIVE`; it gets
`UNKNOWN` and says why. A board that fabricates one row is a board an operator
stops trusting entirely, and then the twenty-nine true rows are worthless too.
"""
from __future__ import annotations

import ast
import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger("Ouroboros.ProgressBoard")

__all__ = [
    "FeatureState", "FeatureRow", "BoardReading", "ProgressBoard", "ENTRY",
    "DYNAMIC_LIVE",
    "board_enabled", "scan_roots",
]

#: States a feature can be in. Ordered worst-news-first for display: an
#: operator scanning this wants MISSING and DARK to be the first things their
#: eye lands on, not buried under thirty healthy rows.
MISSING = "missing"      # registry names a source_file that is not on disk
DARK = "dark"            # ON, present, imported by NOTHING in production
LIVE = "live"            # ON, present, and something in production imports it
OFF = "off"              # flag resolves off — deliberately dormant
UNKNOWN = "unknown"      # cannot be measured; never a guess
ENTRY = "entry"          # runs via `python -m` / console script, not imports
DYNAMIC_LIVE = "dynamic" # discovered at runtime by a registry, not imported

_STATE_ORDER = (MISSING, DARK, LIVE, DYNAMIC_LIVE, ENTRY, OFF, UNKNOWN)


class FeatureState:
    """Namespace for the state constants (kept as plain strings for JSON)."""

    MISSING = MISSING
    DARK = DARK
    LIVE = LIVE
    OFF = OFF
    UNKNOWN = UNKNOWN
    ENTRY = ENTRY
    DYNAMIC_LIVE = DYNAMIC_LIVE
    ORDER = _STATE_ORDER


def board_enabled() -> bool:
    """Default ON. This is a read-only reading; it holds no authority."""
    return os.environ.get(
        "JARVIS_PROGRESS_BOARD_ENABLED", "1",
    ).strip().lower() not in ("0", "false", "no", "off")


def scan_roots() -> Tuple[str, ...]:
    """Production roots for the import-graph scan.

    A knob, not a constant: what counts as "production" differs between this
    repo and a consumer of it, and hardcoding ``backend`` would make the board
    silently wrong anywhere else rather than configurably right.
    """
    raw = os.environ.get("JARVIS_PROGRESS_BOARD_ROOTS", "").strip()
    if raw:
        return tuple(p.strip() for p in raw.split(",") if p.strip())
    return ("backend",)


#: Directories that are not this codebase. A venv vendored under the scan root
#: turned a 900-file walk into 20,121 files and 119 seconds — unusable from a
#: live cockpit, and every vendored module counted as a production importer,
#: which would quietly launder dark modules into live ones.
_EXCLUDE_DIRS = frozenset({
    "venv", ".venv", "site-packages", "node_modules", "__pycache__",
    ".git", ".mypy_cache", ".pytest_cache", "build", "dist", ".tox",
    "vendor", "third_party", ".ouroboros", ".jarvis",
})

#: The prefix that marks a knob as ours. Read from env so a fork with a
#: different prefix is configurably right rather than silently empty.
def flag_prefix() -> str:
    return os.environ.get("JARVIS_PROGRESS_BOARD_PREFIX", "JARVIS_").strip() or "JARVIS_"


def _excluded(path: Path) -> bool:
    return any(part in _EXCLUDE_DIRS for part in path.parts)


def _is_test_path(rel: str) -> bool:
    """Tests import things without making them live.

    A module whose ONLY importers are tests is inert in production — that is
    precisely the state this board exists to surface, so test importers must
    not be allowed to launder a dark module into a live one.
    """
    lowered = rel.replace("\\", "/").lower()
    return (
        lowered.startswith("test") or "/test" in lowered
        or "/tests/" in lowered or Path(lowered).name.startswith("test_")
        or lowered.endswith("_test.py") or "/conftest.py" in lowered
    )


@dataclass(frozen=True)
class FeatureRow:
    """One feature's reading. Carries WHY, not just what."""

    flag: str
    state: str
    module: str = ""
    category: str = ""
    enabled: Optional[bool] = None
    importers: int = 0
    reason: str = ""
    value: Any = None

    @property
    def kind(self) -> str:
        """``switch`` or ``knob`` — a real distinction, not a formatting one.

        A boolean-defaulted flag TURNS SOMETHING ON. A value-defaulted flag
        (``"30"``, ``"notify_apply"``) TUNES something that is already on. An
        unwired switch is a feature nobody connected; an unwired knob is a
        dial on a module nobody imports — which is the SAME finding as its
        module, repeated once per dial.

        Sorting them together let twelve ``ADAPTATION_*`` thresholds crowd out
        every interesting row, and gave a threshold the same glyph as a dead
        feature.
        """
        # Name first for the AMBIGUOUS defaults only. A literal `True`, or a
        # word like "true"/"on", is decisive on its own; "1" is not, and
        # trusting it made every threshold-defaulting-to-1 look like a feature.
        from_name = _kind_from_name(self.flag)
        if from_name is not None and isinstance(self.value, str) \
                and self.value.strip() in ("0", "1"):
            return from_name
        if self.enabled is not None:
            return "switch"
        return from_name or "knob"

    @property
    def is_actionable(self) -> bool:
        """Worth an operator's attention right now."""
        return self.state in (MISSING, DARK)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "flag": self.flag, "state": self.state, "module": self.module,
            "category": self.category, "enabled": self.enabled,
            "importers": self.importers, "reason": self.reason,
            "value": self.value,
        }


@dataclass
class BoardReading:
    """A whole board. Bounded, sortable, and honest about what it could not see."""

    rows: List[FeatureRow] = field(default_factory=list)
    scanned_files: int = 0
    duration_s: float = 0.0
    degraded: str = ""
    verbs: Tuple[str, ...] = ()
    #: False means discovery has not RUN — distinct from 'ran and
    #: found none'. Collapsing the two rendered an unprimed registry
    #: as 'verbs primed 0', which reads as 'this cockpit has no verbs'.
    verbs_primed: bool = False

    def by_state(self, state: str) -> List[FeatureRow]:
        return [r for r in self.rows if r.state == state]

    @property
    def counts(self) -> Dict[str, int]:
        out = {s: 0 for s in _STATE_ORDER}
        for row in self.rows:
            out[row.state] = out.get(row.state, 0) + 1
        return out

    @property
    def actionable(self) -> List[FeatureRow]:
        """MISSING then DARK — the rows that mean something is wrong."""
        return sorted(
            [r for r in self.rows if r.is_actionable],
            # MISSING before DARK, switches before knobs, then grouped by
            # module. Alphabetical-by-flag put `JARVIS_A*` first and nothing
            # else was ever seen — the sort was hiding the signal it existed
            # to surface.
            key=lambda r: (_STATE_ORDER.index(r.state),
                           0 if r.kind == "switch" else 1,
                           r.module, r.flag),
        )

    @property
    def actionable_modules(self) -> "List[Tuple[str, List[FeatureRow]]]":
        """Actionable rows COLLAPSED BY MODULE, worst-first.

        One unimported module holding twelve thresholds is ONE thing to fix,
        and listing it twelve times is how a real finding gets buried under
        its own repetitions.
        """
        grouped: Dict[str, List[FeatureRow]] = {}
        for row in self.actionable:
            grouped.setdefault(row.module or row.flag, []).append(row)
        return sorted(
            grouped.items(),
            key=lambda kv: (_STATE_ORDER.index(kv[1][0].state),
                            0 if any(r.kind == "switch" for r in kv[1]) else 1,
                            -len(kv[1]), kv[0]),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": "progress_board.1",
            "counts": self.counts,
            "scanned_files": self.scanned_files,
            "duration_s": round(self.duration_s, 3),
            "degraded": self.degraded,
            "verbs": len(self.verbs),
            "verbs_primed": self.verbs_primed,
            "rows": [r.to_dict() for r in self.rows],
        }


class ProgressBoard:
    """Reads two registries and an import graph. Mutates nothing."""

    def __init__(self, repo_root: Optional[Path] = None) -> None:
        self._repo_root = Path(repo_root) if repo_root else _default_root()
        #: module-name -> count of PRODUCTION importers. Built once per scan;
        #: an import graph over a tree this size is far too expensive to
        #: rebuild per row (there are 481+ flags).
        self._importers: Dict[str, int] = {}
        #: flag name -> the file that READS it. Ground truth for provenance:
        #: the registry's `source_file` is hand-written and can drift, but the
        #: file containing `os.environ.get("JARVIS_X")` is where X lives.
        self._flag_sites: Dict[str, str] = {}
        #: flag -> default literal as written at the call site.
        self._flag_defaults: Dict[str, Any] = {}
        #: modules with a `__main__` guard — reachable by execution,
        #: never by import.
        self._entry_modules: Set[str] = set()
        #: module -> why a runtime registry would discover it.
        self._shadow_edges: Dict[str, str] = {}
        self._scanned = 0

    # -- import graph ------------------------------------------------------

    def build_import_graph(self) -> int:
        """ONE pass: importers per module AND where each flag is read.

        Both jobs need every file parsed, and `ast.parse` is the entire cost —
        doing them as two walks doubled a scan that has to be fast enough to
        call from a live cockpit.

        Both `import a.b.c` and `from a.b import c` are counted, because a
        module can be reached either way and a graph that sees only one shape
        would report half the tree as dark.
        """
        counts: Dict[str, int] = {}
        flags: Dict[str, str] = {}
        defaults: Dict[str, Any] = {}
        entries: Set[str] = set()
        shadows: Dict[str, str] = {}
        prefix = flag_prefix()
        scanned = 0
        for root in scan_roots():
            base = self._repo_root / root
            if not base.is_dir():
                continue
            for path in base.rglob("*.py"):
                if _excluded(path):
                    continue
                try:
                    rel = str(path.relative_to(self._repo_root))
                except ValueError:
                    continue
                try:
                    tree = ast.parse(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
                    continue
                scanned += 1
                is_test = _is_test_path(rel)
                if not is_test:
                    self_mod = _module_name(rel)
                    for name in _imported_modules(
                            tree, self_mod, rel.endswith("__init__.py")):
                        # A module importing ITSELF is not a caller. Without
                        # this every module would look live, which is the same
                        # as having no signal at all.
                        if name and name != self_mod:
                            counts[name] = counts.get(name, 0) + 1
                    # The flag universe comes from where flags are actually
                    # READ, not from the registry: `get_default_registry()` is
                    # a CURATED SEED (52 entries) and knew nothing about any
                    # feature shipped this session. Deriving from source makes
                    # the board complete by construction and immune to anyone
                    # forgetting to register.
                    if _has_main_guard(tree):
                        entries.add(_module_name(rel))
                    marker = _semantic_marker(tree, rel)
                    if marker:
                        shadows[_module_name(rel)] = marker
                    for flag, dflt in _flag_literals(tree, prefix):
                        if flag not in flags:
                            flags[flag] = rel
                            defaults[flag] = dflt
        self._importers = counts
        self._flag_sites = flags
        self._flag_defaults = defaults
        self._entry_modules = entries
        self._shadow_edges = shadows
        self._scanned = scanned
        return scanned

    # -- reading -----------------------------------------------------------

    def read(self) -> BoardReading:
        """Build the board. NEVER raises — a status view must not be the thing
        that breaks the cockpit it is reporting on."""
        started = time.monotonic()
        reading = BoardReading()
        try:
            if not board_enabled():
                reading.degraded = "disabled"
                return reading
            self.build_import_graph()
            reading.scanned_files = self._scanned
            reading.verbs, reading.verbs_primed = self._load_verbs()
            # Registry specs ENRICH (description, category, declared default);
            # they no longer define the universe. A flag present in the source
            # but absent from the registry is a real feature, not a non-entity.
            specs = {
                str(getattr(s, "name", "")): s for s in self._load_specs()
            }
            if not self._flag_sites:
                reading.degraded = "no_flags_discovered"
            reading.rows = [
                self._row_for(flag, site, specs.get(flag))
                for flag, site in sorted(self._flag_sites.items())
            ]
        except Exception as exc:  # noqa: BLE001
            logger.debug("[ProgressBoard] degraded", exc_info=True)
            reading.degraded = f"error:{type(exc).__name__}"
        reading.duration_s = time.monotonic() - started
        return reading

    async def read_async(self) -> BoardReading:
        """Off-loop. The graph scan is thousands of `ast.parse` calls — on the
        event loop that is a multi-second freeze, and this module is meant to
        be callable from the live cockpit."""
        try:
            return await asyncio.to_thread(self.read)
        except AttributeError:  # pragma: no cover — <3.9 safety
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, self.read)

    # -- internals ---------------------------------------------------------

    def _load_specs(self) -> List[Any]:
        try:
            from backend.core.ouroboros.governance.flag_registry import (
                Category, get_default_registry,
            )
            registry = get_default_registry()
            specs: List[Any] = []
            for category in Category:
                try:
                    specs.extend(registry.list_by_category(category))
                except Exception:  # noqa: BLE001
                    continue
            return specs
        except Exception:  # noqa: BLE001
            logger.debug("[ProgressBoard] flag registry unavailable",
                         exc_info=True)
            return []

    def _load_verbs(self) -> Tuple[Tuple[str, ...], bool]:
        """(verbs, primed). Deliberately does NOT prime.

        A read-only status view must not trigger the import walk that priming
        performs — that is a side effect the operator did not ask for, and it
        would make simply LOOKING at the board change what the process has
        loaded. So the board reports what is true now, and says plainly when
        discovery has not run.
        """
        try:
            from backend.core.ouroboros.battle_test.repl_dispatch_registry import (
                list_verbs, registry_primed,
            )
            return (tuple(list_verbs()), bool(registry_primed()))
        except Exception:  # noqa: BLE001
            return ((), False)

    def _row_for(self, flag: str, site: str,
                 spec: Optional[Any] = None) -> FeatureRow:
        category = _category_name(getattr(spec, "category", "")) if spec else ""
        default = getattr(spec, "default", None) if spec else None
        # Registry default wins when present (it is declared intent); the
        # AST-derived literal is the fallback, and for every flag shipped
        # this session it is the ONLY source there is.
        if default is None:
            default = self._flag_defaults.get(flag)
        enabled, value = _resolve_enabled(flag, default)

        module_rel = site
        path = self._repo_root / module_rel
        if not path.exists():
            # The scan found it a moment ago, so this means the tree changed
            # underneath us. Say so rather than assert a state from a file
            # that is gone.
            return FeatureRow(flag, MISSING, module_rel, category, enabled,
                              reason="reading file vanished mid-scan",
                              value=value)

        module = _module_name(module_rel)
        importers = int(self._importers.get(module, 0))

        if enabled is False:
            return FeatureRow(flag, OFF, module_rel, category, enabled,
                              importers, "flag resolves off", value)
        if enabled is None:
            # Non-boolean flag: "on" is not a meaningful question, so the row
            # reports its VALUE rather than pretending to a state it does not
            # have.
            #
            # ENTRY is checked HERE as well as below, because this branch
            # returns first: `JARVIS_COMMIT_GRANT_ONESHOT` has a non-boolean
            # default, so it exited through this path and was reported dark
            # even after entry-point detection shipped. A state check that
            # only one of two exits consults is not a state check.
            if importers == 0 and module in self._shadow_edges:
                # A runtime registry finds this by convention. NOT live:
                # 'discovered by a registry' and 'imported by a caller'
                # are different facts, and collapsing them would hide the
                # day the registry stops being primed.
                return FeatureRow(flag, DYNAMIC_LIVE, module_rel, category,
                                  None, 0, self._shadow_edges[module], value)
            if importers == 0 and module in self._entry_modules:
                return FeatureRow(flag, ENTRY, module_rel, category, None, 0,
                                  "module entry point (__main__ guard)", value)
            state = DARK if importers == 0 else LIVE
            return FeatureRow(flag, state, module_rel, category, None,
                              importers, "non-boolean flag; state from graph",
                              value)
        if importers == 0 and module in self._shadow_edges:
            # A runtime registry finds this by convention. NOT live:
            # 'discovered by a registry' and 'imported by a caller'
            # are different facts, and collapsing them would hide the
            # day the registry stops being primed.
            return FeatureRow(flag, DYNAMIC_LIVE, module_rel, category,
                              enabled, 0, self._shadow_edges[module], value)
        if importers == 0 and module in self._entry_modules:
            # Runs via `python -m` or a console script. Nothing imports
            # `commit_authority_cli`, and it is emphatically not dead —
            # the operator invoked it by hand this session. Reachability
            # by EXECUTION is invisible to an import graph, and a board
            # that calls every CLI dead is a board nobody reads twice.
            return FeatureRow(flag, ENTRY, module_rel, category, enabled, 0,
                              "module entry point (__main__ guard)", value)
        if importers == 0:
            return FeatureRow(
                flag, DARK, module_rel, category, enabled, 0,
                "enabled but NOTHING in production imports it", value,
            )
        return FeatureRow(flag, LIVE, module_rel, category, enabled, importers,
                          f"{importers} production importer(s)", value)


# -- helpers ---------------------------------------------------------------


def _default_root() -> Path:
    """Repo root, found by walking up from this file rather than assumed."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "backend").is_dir() and (parent / ".git").exists():
            return parent
    return here.parents[4] if len(here.parents) > 4 else Path.cwd()


def _module_name(rel_path: str) -> str:
    rel = str(rel_path).replace("\\", "/")
    if rel.endswith(".py"):
        rel = rel[:-3]
    if rel.endswith("/__init__"):
        rel = rel[: -len("/__init__")]
    return rel.replace("/", ".")


def _normalise_source(source: str) -> str:
    """Accept either a path or a dotted module name.

    The registry has 481+ entries written by many hands over many slices; both
    shapes are present in it, and a board that understood only one would report
    the other half as MISSING and look like a catastrophe.
    """
    src = source.replace("\\", "/").strip().lstrip("./")
    if src.endswith(".py"):
        return src
    if "/" in src:
        return src if src.endswith(".py") else src + ".py"
    if "." in src:
        return src.replace(".", "/") + ".py"
    return src + ".py"


def _resolve_relative(self_mod: str, level: int, module: str,
                      is_package: bool = False) -> str:
    """``from ..x import y`` inside ``a.b.c`` -> ``a.x``. NEVER raises.

    `level` counts dots: 1 is the current package, 2 its parent. The
    importing module's OWN dotted name carries the package, so this needs no
    filesystem walk — which is what the previous `continue` assumed it did.

    ``is_package`` is load-bearing and easy to miss. `_module_name` strips
    `__init__`, so a package's own `__init__.py` is already NAMED for the
    package — `level=1` there means itself, not its parent, and stripping a
    segment walks one level too far. That single off-by-one is what kept the
    sensors dark after relative imports were resolved: `sensors/__init__.py`
    re-exporting `.backlog_sensor` resolved to `intake.backlog_sensor`, a
    module that does not exist.
    """
    try:
        parts = str(self_mod or "").split(".")
        strip = max(0, int(level) - 1) if is_package else int(level)
        keep = len(parts) - strip
        if keep < 0:
            return ""
        base = ".".join(parts[:keep])
        if not base:
            return str(module or "")
        return f"{base}.{module}" if module else base
    except Exception:  # noqa: BLE001
        return ""


def _imported_modules(tree: ast.AST, self_mod: str = "",
                      is_package: bool = False) -> Set[str]:
    """Every module this file imports, in both syntaxes.

    RELATIVE imports are resolved rather than skipped, and that omission was
    a systematic false-DARK rather than a rounding error. A package that
    re-exports — `sensors/__init__.py` doing `from .backlog_sensor import
    BacklogSensor` — is the ONLY edge between a consumer and the module the
    flag lives in: the consumer writes `from ...sensors import BacklogSensor`,
    which names a CLASS, and a class name never matches a module name. With
    the re-export skipped there was no path at all, so every module reached
    that way reported DARK while being imported on every boot.
    """
    found: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                resolved = _resolve_relative(
                    self_mod, node.level, node.module or "", is_package)
                if resolved:
                    found.add(resolved)
                    for alias in node.names:
                        found.add(f"{resolved}.{alias.name}")
                continue
            module = node.module or ""
            if module:
                found.add(module)
                # `from a.b import c` may be importing the MODULE `a.b.c`,
                # which is how most of this codebase reaches its siblings.
                for alias in node.names:
                    found.add(f"{module}.{alias.name}")
    return found






# ---------------------------------------------------------------------------
# The semantic shadow graph
# ---------------------------------------------------------------------------
#
# Static AST cannot evaluate `inspect.getmembers`. But it does not have to:
# a runtime registry only finds what it can RECOGNISE, and what it recognises
# is a convention written into the source. Detect the convention and you have
# the edge the import graph is missing — without importing or executing
# anything.
#
# The markers below were MEASURED, not assumed. `repl_dispatch_registry`
# has no `@verb` decorator; it walks curated packages for modules named
# `*_repl` (or `repl` inside a sub-package) and picks up module-level
# callables named `dispatch_<verb>_command`. Guessing a plausible-looking
# decorator would have produced a detector that matched nothing and reported
# a confident zero.


def marker_functions() -> Tuple[str, ...]:
    """Function-name prefixes/suffixes a registry discovers by convention.

    ``prefix*suffix`` form. Env-overridable because a second registry with a
    different convention should be a config change, not a code change.
    """
    raw = os.environ.get("JARVIS_PROGRESS_BOARD_FN_MARKERS", "").strip()
    if raw:
        return tuple(x.strip() for x in raw.split(",") if x.strip())
    return ("dispatch_*_command",)


def marker_modules() -> Tuple[str, ...]:
    """Module basenames that a discovery walk treats as registrable."""
    raw = os.environ.get("JARVIS_PROGRESS_BOARD_MODULE_MARKERS", "").strip()
    if raw:
        return tuple(x.strip() for x in raw.split(",") if x.strip())
    return ("*_repl", "repl")


def marker_decorators() -> frozenset:
    """Decorator names that register their target at import time.

    Empty by default HERE — this codebase's REPL registry uses naming, not
    decorators. Populated by env for subsystems that do, rather than shipping
    a speculative list that quietly matches nothing.
    """
    raw = os.environ.get("JARVIS_PROGRESS_BOARD_DECORATORS", "").strip()
    return frozenset(x.strip() for x in raw.split(",") if x.strip())


def marker_base_classes() -> frozenset:
    """Base classes whose subclasses a registry collects."""
    raw = os.environ.get("JARVIS_PROGRESS_BOARD_BASES", "").strip()
    return frozenset(x.strip() for x in raw.split(",") if x.strip())


def _glob_match(name: str, pattern: str) -> bool:
    """`a*b` without importing fnmatch's full machinery per node."""
    if "*" not in pattern:
        return name == pattern
    head, _, tail = pattern.partition("*")
    return (name.startswith(head) and name.endswith(tail)
            and len(name) >= len(head) + len(tail))


def _semantic_marker(tree: ast.AST, rel: str) -> str:
    """Why a runtime registry would find this module, or ''.

    Returns a REASON, not a boolean: an operator told a module is
    dynamically live deserves to know which convention made it so, or the
    state is just a different flavour of guess.
    """
    stem = Path(rel).stem
    module_hit = any(_glob_match(stem, pat) for pat in marker_modules())

    decorators = marker_decorators()
    bases = marker_base_classes()
    fn_pats = marker_functions()

    for node in getattr(tree, "body", ()):  # MODULE level only
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for pat in fn_pats:
                if _glob_match(node.name, pat):
                    return f"registry convention: {node.name}()"
            for dec in node.decorator_list:
                nm = _decorator_name(dec)
                if nm and nm in decorators:
                    return f"decorator @{nm}"
        elif isinstance(node, ast.ClassDef):
            for dec in node.decorator_list:
                nm = _decorator_name(dec)
                if nm and nm in decorators:
                    return f"decorator @{nm}"
            for base in node.bases:
                nm = _decorator_name(base)
                if nm and nm in bases:
                    return f"subclass of {nm}"
    if module_hit:
        return f"module naming convention: {stem}"
    return ""


def _decorator_name(node: ast.AST) -> str:
    """Last dotted segment of a decorator/base expression."""
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""

def _has_main_guard(tree: ast.AST) -> bool:
    """Does this module run itself?

    `if __name__ == "__main__":` means the module is reachable by EXECUTION —
    `python -m pkg.mod`, a console script, a subprocess call. An import graph
    cannot see any of those, so without this every CLI in the tree reads as
    inert. The board's own sampling caught this: `commit_authority_cli` was
    reported dark in the same session the operator ran it by hand.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if (isinstance(test, ast.Compare)
                and isinstance(test.left, ast.Name)
                and test.left.id == "__name__"):
            for comparator in test.comparators:
                if (isinstance(comparator, ast.Constant)
                        and comparator.value == "__main__"):
                    return True
    return False


def switch_suffixes() -> Tuple[str, ...]:
    """Name endings that mean a flag TURNS SOMETHING ON."""
    raw = os.environ.get("JARVIS_PROGRESS_BOARD_SWITCH_SUFFIXES", "").strip()
    if raw:
        return tuple(x.strip().upper() for x in raw.split(",") if x.strip())
    return ("_ENABLED", "_DISABLED", "_ON", "_OFF", "_ALLOW", "_ALLOWED",
            "_REQUIRE", "_REQUIRED", "_FORCE", "_STRICT", "_DRY_RUN")


def knob_suffixes() -> Tuple[str, ...]:
    """Name endings that mean a flag TUNES something already on."""
    raw = os.environ.get("JARVIS_PROGRESS_BOARD_KNOB_SUFFIXES", "").strip()
    if raw:
        return tuple(x.strip().upper() for x in raw.split(",") if x.strip())
    return ("_S", "_MS", "_SEC", "_SECONDS", "_TIMEOUT", "_TTL", "_INTERVAL",
            "_SIZE", "_MAX", "_MIN", "_LIMIT", "_THRESHOLD", "_PCT", "_RATIO",
            "_COUNT", "_DEPTH", "_BUDGET", "_WIDTH", "_HEIGHT", "_PORT",
            "_PATH", "_DIR", "_URL", "_HOST", "_MODE", "_LEVEL", "_TIER")


def _kind_from_name(flag: str) -> Optional[str]:
    """`switch` / `knob` from the flag's NAME, or None when it says nothing.

    Needed because the default literal is not always decisive. `"1"` means
    both "on" and "the number one", so a threshold defaulting to 1 was
    classified as a switch — a real misreading, surfaced when a test fixture
    happened to name one `JARVIS_DIAL_1`.

    The name is the stronger signal precisely where the value is weakest, so
    it is consulted FIRST and only for the ambiguous numeric cases.
    """
    name = str(flag or "").upper()
    for suffix in switch_suffixes():
        if name.endswith(suffix):
            return "switch"
    for suffix in knob_suffixes():
        if name.endswith(suffix):
            return "knob"
    return None

def _coerce_bool(value: Any) -> Optional[bool]:
    """A default literal's boolean meaning, or None if it has none.

    Deliberately conservative. `"1"` and `"true"` are unambiguous; a default of
    `"5"` or `"notify_apply"` is a VALUE, not a switch, and guessing at those
    would put tuning knobs into a column that is supposed to mean
    "enabled but inert".
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off"):
            return False
    return None

def _flag_literals(tree: ast.AST, prefix: str):
    """Yield ``(flag_name, default_literal)`` for every env read in a file.

    Covers the three shapes this codebase actually uses —
    ``os.environ.get("X", "1")``, ``os.getenv("X", "1")`` and
    ``os.environ["X"]`` — because a discoverer that understood only one would
    report the features using the others as non-existent, which is worse than
    reporting nothing: it looks like a complete answer.

    The default literal matters as much as the name. A flag read with
    ``get("X", "1")`` is ON by default, and a board that cannot see that would
    call every unset feature OFF and hide precisely the dark ones.
    """
    for node in ast.walk(tree):
        name = None
        default = None
        if isinstance(node, ast.Call):
            fn = node.func
            is_get = (
                isinstance(fn, ast.Attribute) and fn.attr in ("get", "getenv")
            )
            if is_get and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    name = first.value
                    if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                        default = node.args[1].value
        elif isinstance(node, ast.Subscript):
            base = node.value
            if isinstance(base, ast.Attribute) and base.attr == "environ":
                key = node.slice
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    name = key.value
        if name and name.startswith(prefix):
            yield (name, default)

def _category_name(category: Any) -> str:
    return str(getattr(category, "value", category) or "")


def _resolve_enabled(flag: str, default: Any) -> Tuple[Optional[bool], Any]:
    """(enabled, raw_value). `enabled` is None for non-boolean flags.

    Reads the RESOLVED value — env if set, else the registry default. Keying
    off the env string alone would miss every default-on feature, which is
    most of them, and keying off the default alone would ignore the operator.
    """
    raw = os.environ.get(flag)
    if raw is None:
        if isinstance(default, bool):
            return (default, default)
        # Almost every flag in this codebase is written
        # `os.environ.get("X", "1")` — a STRING that means a boolean. Treating
        # those as non-boolean put 729 flags into a bucket whose reason line
        # read "non-boolean flag", which is both wrong and useless: the number
        # an operator scans for was inflated by ordinary default-on features.
        coerced = _coerce_bool(default)
        if coerced is not None:
            return (coerced, default)
        return (None, default)
    lowered = raw.strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return (True, raw)
    if lowered in ("0", "false", "no", "off"):
        return (False, raw)
    return (None, raw)


def terminal_width(default: int = 78) -> int:
    """Real width, asked at render time.

    The previous default of 78 was a GUESS baked into a signature. Rows padded
    to 44 columns plus an unclipped reason wrapped and broke the layout the
    moment it met an actual terminal — invisible in every unit test, obvious in
    one second of `ov demo board`.
    """
    try:
        import shutil
        return max(40, int(shutil.get_terminal_size((default, 24)).columns))
    except Exception:  # noqa: BLE001
        return default


def render_board(reading: BoardReading, *, width: Optional[int] = None,
                 limit: int = 0) -> List[str]:
    """Operator-facing lines. Worst news first, one line per finding.

    Not a full dump: 3,900 rows is a log, not a status view. What an operator
    needs on sight is the count line and the things that are wrong — grouped by
    module, because one unimported module holding twelve dials is one problem.
    """
    try:
        cols = terminal_width() if width is None else max(40, int(width))
        counts = reading.counts
        head = (f"  live {counts.get(LIVE, 0)}   dark {counts.get(DARK, 0)}   "
                f"dynamic {counts.get(DYNAMIC_LIVE, 0)}   "
                f"entry {counts.get(ENTRY, 0)}   off {counts.get(OFF, 0)}")
        out: List[str] = [head[:cols]]
        out.append(f"  ({reading.scanned_files} files, "
                   f"{reading.duration_s:.1f}s)"[:cols])
        if reading.degraded:
            out.append(f"  ⚠ degraded: {reading.degraded}"[:cols])

        groups = reading.actionable_modules
        if not groups:
            out.append("  ✓ nothing enabled-but-inert")
            return out

        shown = groups if limit <= 0 else groups[:limit]
        out.append("")
        # Widest name we will actually print, so the reason column starts in
        # the same place without being padded to a constant nobody measured.
        # Bounded by the TERMINAL, not just by a constant. With long module
        # paths the name column claimed the whole line, leaving the tail
        # negative room — so the flag was chopped by the final `[:cols]` with
        # no ellipsis, which reads as a different flag. At least 20 columns
        # stay reserved for what the finding actually IS.
        namew = min(48, max(12, cols - 26),
                    max((len(_short(m)) for m, _ in shown), default=20))
        for module, rows in shown:
            first = rows[0]
            glyph = "✗" if first.state == MISSING else "◌"
            switches = [r for r in rows if r.kind == "switch"]
            if switches:
                tail = switches[0].flag
                if len(rows) > 1:
                    tail += f"  +{len(rows) - 1} more"
            else:
                tail = f"{len(rows)} knob{'s' if len(rows) != 1 else ''}"
            # Budget the tail rather than truncating the finished line: a
            # blind `line[:cols]` cut flag names mid-word (`JARVIS_MULTI_FACTOR_BOOS`),
            # which reads as a DIFFERENT flag and is worse than an ellipsis.
            room = cols - (namew + 6)
            if room > 4 and len(tail) > room:
                tail = tail[: room - 1] + "…"
            name = _short(module, namew)
            out.append(f"  {glyph} {name:<{namew}}  {tail}"[:cols])
        hidden = len(groups) - len(shown)
        if hidden > 0:
            out.append(f"    … {hidden} more module(s)"[:cols])
        return out
    except Exception:  # noqa: BLE001
        return ["  ⚠ board render degraded"]


def _short(module: str, width: int = 48) -> str:
    """Trim a module path from the LEFT — the tail identifies it.

    Takes the budget as an argument because `f"{name:<{w}}"` PADS to a minimum
    and never clips: a 48-character path stayed 48 characters inside a
    14-column field, and the final `[:cols]` then ate the tail instead. The
    name column has to enforce its own width.
    """
    text = str(module or "")
    limit = max(4, int(width))
    return text if len(text) <= limit else "…" + text[-(limit - 1):]
