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

_STATE_ORDER = (MISSING, DARK, LIVE, ENTRY, OFF, UNKNOWN)


class FeatureState:
    """Namespace for the state constants (kept as plain strings for JSON)."""

    MISSING = MISSING
    DARK = DARK
    LIVE = LIVE
    OFF = OFF
    UNKNOWN = UNKNOWN
    ENTRY = ENTRY
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
            key=lambda r: (_STATE_ORDER.index(r.state), r.flag),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": "progress_board.1",
            "counts": self.counts,
            "scanned_files": self.scanned_files,
            "duration_s": round(self.duration_s, 3),
            "degraded": self.degraded,
            "verbs": len(self.verbs),
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
                    for name in _imported_modules(tree):
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
                    for flag, dflt in _flag_literals(tree, prefix):
                        if flag not in flags:
                            flags[flag] = rel
                            defaults[flag] = dflt
        self._importers = counts
        self._flag_sites = flags
        self._flag_defaults = defaults
        self._entry_modules = entries
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
            reading.verbs = self._load_verbs()
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

    def _load_verbs(self) -> Tuple[str, ...]:
        try:
            from backend.core.ouroboros.battle_test.repl_dispatch_registry import (
                list_verbs,
            )
            return tuple(list_verbs())
        except Exception:  # noqa: BLE001
            return ()

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
            if importers == 0 and module in self._entry_modules:
                return FeatureRow(flag, ENTRY, module_rel, category, None, 0,
                                  "module entry point (__main__ guard)", value)
            state = DARK if importers == 0 else LIVE
            return FeatureRow(flag, state, module_rel, category, None,
                              importers, "non-boolean flag; state from graph",
                              value)
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


def _imported_modules(tree: ast.AST) -> Set[str]:
    """Every module this file imports, in both syntaxes."""
    found: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import — not resolvable without a package walk
                continue
            module = node.module or ""
            if module:
                found.add(module)
                # `from a.b import c` may be importing the MODULE `a.b.c`,
                # which is how most of this codebase reaches its siblings.
                for alias in node.names:
                    found.add(f"{module}.{alias.name}")
    return found





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


def render_board(reading: BoardReading, *, width: int = 78,
                 limit: int = 0) -> List[str]:
    """Operator-facing lines. Worst news first.

    Deliberately not a full dump by default: 481 rows is not a status view, it
    is a log. What an operator needs on sight is the count line and the rows
    that mean something is wrong.
    """
    try:
        counts = reading.counts
        out: List[str] = [
            f"  live {counts.get(LIVE, 0)}   dark {counts.get(DARK, 0)}   "
            f"off {counts.get(OFF, 0)}   missing {counts.get(MISSING, 0)}   "
            f"unknown {counts.get(UNKNOWN, 0)}"
            f"    ({reading.scanned_files} files, "
            f"{reading.duration_s:.1f}s)",
        ]
        if reading.degraded:
            out.append(f"  ⚠ degraded: {reading.degraded}")
        rows = reading.actionable
        if not rows:
            out.append("  ✓ nothing enabled-but-inert")
            return out
        shown = rows if limit <= 0 else rows[:limit]
        out.append("")
        for row in shown:
            glyph = "✗" if row.state == MISSING else "◌"
            name = row.flag[:44]
            out.append(f"  {glyph} {name:<44} {row.state:<8} {row.reason}"[:width])
        hidden = len(rows) - len(shown)
        if hidden > 0:
            out.append(f"    … {hidden} more")
        return out
    except Exception:  # noqa: BLE001
        return ["  ⚠ board render degraded"]
