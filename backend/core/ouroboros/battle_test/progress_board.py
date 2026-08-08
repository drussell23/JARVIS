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
import fnmatch
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple,
)

logger = logging.getLogger("Ouroboros.ProgressBoard")

__all__ = [
    "FeatureState", "FeatureRow", "BoardReading", "ProgressBoard", "ENTRY",
    "DYNAMIC_LIVE",
    "board_enabled", "scan_roots",
    "cache_enabled", "cache_path", "scan_budget_s", "CACHE_SCHEMA",
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
    # `scripts` is production. `scripts/ouroboros_battle_test.py` is THE entry
    # point that boots the six-layer stack — it is how this system actually
    # runs — and 361 backend modules are reachable from `scripts/` and from
    # nowhere else. Scanning only `backend` reported every one of them DARK
    # while they were imported on every session.
    #
    # Found via `aegis/preflight.py`: flagged dark-and-enabled, imported at
    # `scripts/ouroboros_battle_test.py:1997`. Same shape as the relative-
    # import blindness — the board was right about its own graph and the
    # graph was missing an edge.
    # "." is the REPO ROOT, and leaving it out was the fifth blindness.
    #
    # `unified_supervisor.py` — the 102K-line kernel, the thing that actually
    # boots this system — lives at the root, so none of its import edges were
    # ever scanned. Every module reached ONLY from the kernel read DARK.
    #
    # Found via `elite_dashboard.py`: flagged dark-and-enabled and one step
    # from deletion, while the kernel imports it at Zone 6.14, calls
    # `get_elite_dashboard()`, `await start()`, `install_narrator_hook()` and
    # keeps the instance. A dashboard that runs on every boot, invisible to
    # the board that exists to say what runs.
    #
    # Same shape as the four before it: the board was right about its own
    # graph, and the graph was missing an edge. This one was the largest —
    # the entry point itself.
    #
    # "." CONTAINS `backend` and `scripts`, and this comment used to claim the
    # overlap was free "because paths are normalised to module names and the
    # set is deduplicated". That was false. `flags` was deduplicated by
    # `if flag not in flags`; `counts` was incremented unconditionally, so
    # 3,810 of 14,830 files were parsed twice and every importer total under
    # those two roots was close to double. `_iter_source_files` now yields
    # each real path exactly once, which is what makes the overlap actually
    # free — and makes `FeatureRow.importers` a count of importers rather than
    # a count of walks that reached one.
    return (".", "backend", "scripts")


#: Directories that are not this codebase. A venv vendored under the scan root
#: turned a 900-file walk into 20,121 files and 119 seconds — unusable from a
#: live cockpit, and every vendored module counted as a production importer,
#: which would quietly launder dark modules into live ones.
#:
#: This list stays a list because these names are CONVENTIONS with no property
#: to detect — nothing about a directory says "I am a build output". Anything
#: that CAN be detected structurally belongs in a rule instead, which is why
#: `.worktrees` is absent here and handled by `_nested_checkout`: naming it
#: would have fixed this repo and left submodules and vendored clones counted.
_EXCLUDE_DIRS = frozenset({
    "venv", ".venv", "site-packages", "node_modules", "__pycache__",
    ".git", ".mypy_cache", ".pytest_cache", "build", "dist", ".tox",
    "vendor", "third_party", ".ouroboros", ".jarvis",
})

#: The prefix that marks a knob as ours. Read from env so a fork with a
#: different prefix is configurably right rather than silently empty.
#: Bumped whenever a change to THIS module could change a reading.
#:
#: A cache keyed only on the scanned tree would keep serving the old verdict
#: after the board itself is corrected — a surface reporting a state it no
#: longer measures, which is the exact defect this file exists to name. The
#: board's own source hash is folded into the key for the same reason, so this
#: constant is a belt to that braces: it lets a deliberate semantic change
#: invalidate every cache in every checkout in one edit.
CACHE_SCHEMA = "progress_board.cache.v1"


def cache_enabled() -> bool:
    """Default ON.

    The cache changes only how long a reading takes, never what it says: a
    hit requires every input to be byte-identical. Off is for proving that —
    the regression spine reads both ways and asserts they agree.
    """
    return os.environ.get(
        "JARVIS_PROGRESS_BOARD_CACHE", "1",
    ).strip().lower() not in ("0", "false", "no", "off")


def scan_budget_s() -> float:
    """Worst-case wall time for a COLD reading, as the instrument's own claim.

    Measured on this tree: ~162 s to `ast.parse` 7,333 files / 143 MB. The
    warm path is ~1.3 s, but a budget sized for the warm path is a budget that
    fails the first time anyone touches a file.

    This lives here rather than in the tests that need it because the board is
    the only thing that knows what a board scan costs, and the alternative had
    already gone wrong: `test_progress_board_relative` recorded "around 110
    seconds" in a docstring when it was written, never re-derived it, and by
    today's 162 s was passing or timing out depending on how warm the page
    cache happened to be. Two test modules now need this number, which is
    exactly when a number must stop being written down twice.

    A budget, not a promise. `ProgressBoard` never raises and has no internal
    deadline; this is what a CALLER should allow before concluding something
    is wrong.
    """
    raw = os.environ.get("JARVIS_PROGRESS_BOARD_BUDGET_S", "").strip()
    try:
        got = float(raw)
    except ValueError:
        got = 0.0
    # ~5x headroom over the measurement, so a slower disk or a CI runner under
    # contention produces a slow pass rather than a flake that reads like a
    # finding.
    return got if got > 0 else 900.0


def cache_path(repo_root: Optional[Path] = None) -> Path:
    """Where a reading is remembered.

    Under ``.jarvis/`` — which is already in :data:`_EXCLUDE_DIRS`, so the
    cache cannot appear in the scan that produced it. A cache that perturbs
    its own key never hits, and would have been a slow, silent no-op.
    """
    raw = os.environ.get("JARVIS_PROGRESS_BOARD_CACHE_PATH", "").strip()
    if raw:
        return Path(raw).expanduser()
    root = Path(repo_root) if repo_root else _default_root()
    return root / ".jarvis" / "progress_board_cache.json"


def _tree_digest(repo_root: Path, roots: Sequence[str]) -> str:
    """Identity of the source tree, at the granularity the reading depends on.

    ``(relative path, mtime_ns, size)`` per file. Content hashing 143 MB of
    source would be the only stricter answer and costs ~40x more than the walk
    it would be protecting; mtime+size is what every build system in existence
    trusts for the same reason.

    Deliberately reuses :func:`_iter_source_files` rather than walking again —
    the fingerprint must cover EXACTLY the files the scan will parse. Two
    walks with independently-maintained exclusion rules would drift, and the
    drift would show up as a cache that is confidently stale about whichever
    files only one of them could see.
    """
    digest = hashlib.sha256()
    # The pytest configuration is an input to the reading and is not a `.py`
    # file, so the walk below cannot see it. Editing `testpaths` changes which
    # files count as production importers and therefore which flags are dark;
    # a key blind to that would serve the old verdict under the new rule.
    for name in ("pytest.ini", "setup.cfg", "pyproject.toml"):
        try:
            stat = (repo_root / name).stat()
            digest.update(
                f"cfg:{name}\0{stat.st_mtime_ns}\0{stat.st_size}\n".encode(
                    "utf-8", "replace"))
        except OSError:
            digest.update(f"cfg:{name}\0absent\n".encode("utf-8"))
    for rel, path in _iter_source_files(repo_root, roots):
        try:
            stat = path.stat()
        except OSError:
            # Raced away between walk and stat. Feed the path alone: the file
            # is part of the tree's identity even when unreadable, and the
            # next run will see a different state and miss — correctly.
            digest.update(f"{rel}\0?\0?\n".encode("utf-8", "replace"))
            continue
        digest.update(
            f"{rel}\0{stat.st_mtime_ns}\0{stat.st_size}\n".encode(
                "utf-8", "replace"),
        )
    return digest.hexdigest()


def _environ_digest() -> str:
    """The flag environment, which decides `off` versus `dark`.

    ``_resolve_enabled`` reads the LIVE environment, so two processes with
    different ``JARVIS_*`` settings legitimately produce different readings
    from an identical tree. Keying on the tree alone would let one operator's
    ``JARVIS_META_SENSOR_ENABLED=0`` session poison another's — a row reported
    ``off`` when it is in fact ``dark``, which is the more comfortable of the
    two lies and therefore the more dangerous.

    The whole family is hashed rather than the flags a reading happened to
    touch: which flags matter is only known AFTER the scan, and a key that
    depends on its own result is not a key.
    """
    prefix = flag_prefix()
    digest = hashlib.sha256()
    for name in sorted(k for k in os.environ if k.startswith(prefix)):
        digest.update(f"{name}\0{os.environ[name]}\n".encode("utf-8", "replace"))
    return digest.hexdigest()


def _board_source_digest() -> str:
    """This module's own bytes. A fixed board must re-read a stale tree."""
    try:
        return hashlib.sha256(
            Path(__file__).read_bytes(),
        ).hexdigest()
    except OSError:
        # Unreadable source (zipimport, a stripped install). Fall back to a
        # value that can never collide with a real digest, so the cache misses
        # rather than trusting a key it could not fully compute.
        return "unreadable"


def flag_prefix() -> str:
    return os.environ.get("JARVIS_PROGRESS_BOARD_PREFIX", "JARVIS_").strip() or "JARVIS_"


def _excluded(path: Path) -> bool:
    return any(part in _EXCLUDE_DIRS for part in path.parts)


def nested_checkout_pruning() -> bool:
    """``JARVIS_PROGRESS_BOARD_PRUNE_NESTED`` (default on). NEVER raises."""
    return os.environ.get(
        "JARVIS_PROGRESS_BOARD_PRUNE_NESTED", "1",
    ).strip().lower() not in ("0", "false", "no", "off")


def _nested_checkout(directory: Path) -> bool:
    """Is this directory the root of a DIFFERENT working tree?

    A `.git` entry inside a directory is git saying that directory is a
    checkout in its own right: a linked worktree (where `.git` is a FILE
    holding a gitdir pointer), a submodule, or a vendored clone. In every one
    of those the Python underneath is a COPY of code that already exists in
    this tree, and counting a copy's imports as production importers is
    precisely the laundering `_EXCLUDE_DIRS` was written to prevent — its own
    docstring says so about vendored venvs.

    DERIVED, never listed. `.worktrees` was the instance that prompted this
    (7,382 files, 26% of the walk on this repo), but adding that name to the
    exclusion set would leave submodules, vendored clones, and whatever the
    next directory is called still counted. The rule is the property, not the
    name — so a fork that puts its worktrees somewhere else is configurably
    right rather than silently wrong.

    Measured, not assumed: a linked worktree here carries `.git` as a 126-byte
    regular file while the repo root carries a directory, so a check for
    either shape alone would have caught one and missed the other.
    `exists()` covers both and follows symlinks — this repo's `.git` has been
    one under the iCloud `.nosync` layout — and `is_symlink()` catches a
    dangling link, which is still evidence of a checkout that was there.
    """
    marker = directory / ".git"
    try:
        return marker.exists() or marker.is_symlink()
    except OSError:  # permission, or a path that races away mid-walk
        return False


def _iter_source_files(repo_root: Path,
                       roots: Sequence[str]) -> Iterable[Tuple[str, Path]]:
    """Every production `.py` under `roots` — exactly once, pruned at the directory.

    Three properties the previous `rglob` pass did not have, each of which was
    costing something measurable.

    **Pruned, not filtered.** `rglob` enumerates a whole tree and then discards
    what `_excluded` rejects, so an excluded directory is paid for in full
    before being thrown away. `os.walk(topdown=True)` publishes `dirnames` for
    the caller to edit, and removing an entry there means the walker never
    descends into it at all.

    **Deduplicated across roots.** `scan_roots()` returns
    ``(".", "backend", "scripts")`` and "." CONTAINS the other two. The comment
    there claimed module-name normalisation deduplicated the overlap; it did
    not. `flags` was deduplicated by `if flag not in flags`, but `counts` was
    incremented unconditionally, so every import in an overlapping file was
    counted TWICE and every importer total under `backend/` and `scripts/` was
    close to double. Measured on this repo: 3,810 of 14,830 unique files were
    parsed twice. That is a wrong number in `FeatureRow.importers`, not merely
    wasted time — which is why the fix belongs here and not in a cache.

    **Blind to other checkouts.** See :func:`_nested_checkout`.

    A scan root that IS itself a nested checkout is still scanned: only
    children are pruned. Someone who names `vendor/thing` in
    ``JARVIS_PROGRESS_BOARD_ROOTS`` has asked for it, and an instrument that
    silently refuses an explicit request is worse than one that costs a little.

    Symlinked directories are NOT followed — `os.walk` defaults to
    ``followlinks=False`` and that default is kept deliberately, because a
    symlink pointing at an ancestor is an infinite walk and a status view that
    can hang is worse than one that misses a directory. Measured before
    relying on it: every symlinked directory under the scan roots here lives in
    `.build` or `venv`, both already excluded, so nothing real is lost. A tree
    that genuinely reaches source through a symlink should name the real path
    in ``JARVIS_PROGRESS_BOARD_ROOTS`` — explicit, and still cycle-free.
    """
    seen: Set[str] = set()
    prune_nested = nested_checkout_pruning()
    for root in roots:
        base = repo_root / root
        if not base.is_dir() or _excluded(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base, topdown=True):
            here = Path(dirpath)
            # In place, and it must be: `os.walk` re-reads this list to decide
            # what to descend into. Rebinding the name would prune nothing.
            dirnames[:] = [
                d for d in dirnames
                if d not in _EXCLUDE_DIRS
                and not (prune_nested and _nested_checkout(here / d))
            ]
            for name in filenames:
                if not name.endswith(".py"):
                    continue
                path = here / name
                try:
                    rel = str(path.relative_to(repo_root))
                except ValueError:
                    continue
                if rel in seen:
                    continue
                seen.add(rel)
                yield rel, path


#: Where tests live, and what a test file is called, when the repository does
#: not say. Only a fallback — :func:`test_collection_config` prefers the
#: project's own pytest configuration, which is the sole authority on the
#: question "would this file be collected as a test".
_FALLBACK_TESTPATHS = ("tests",)
_FALLBACK_PYTHON_FILES = ("test_*.py", "*_test.py")


def _parse_pytest_config(repo_root: Path) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """``(testpaths, python_files)`` as the project declares them.

    Reads ``pytest.ini``, then ``setup.cfg`` (``[tool:pytest]``), then
    ``pyproject.toml`` (``[tool.pytest.ini_options]``) — pytest's own
    precedence. Returns the fallbacks when nothing declares them.

    NEVER raises: an unparseable config yields the fallback, because a status
    view must not be the thing that breaks on a malformed ini.
    """
    import configparser

    def _split(raw: str) -> Tuple[str, ...]:
        return tuple(p for p in raw.replace("\n", " ").split(" ") if p.strip())

    for name, section in (("pytest.ini", "pytest"),
                          ("setup.cfg", "tool:pytest")):
        path = repo_root / name
        try:
            if not path.is_file():
                continue
            parser = configparser.ConfigParser()
            parser.read(path, encoding="utf-8")
            if not parser.has_section(section):
                continue
            paths = _split(parser.get(section, "testpaths", fallback=""))
            files = _split(parser.get(section, "python_files", fallback=""))
            if paths or files:
                return (paths or _FALLBACK_TESTPATHS,
                        files or _FALLBACK_PYTHON_FILES)
        except Exception:  # noqa: BLE001
            logger.debug("[ProgressBoard] %s unreadable", name, exc_info=True)

    try:
        import tomllib  # py3.11+
        path = repo_root / "pyproject.toml"
        if path.is_file():
            table = tomllib.loads(path.read_text(encoding="utf-8"))
            opts = table.get("tool", {}).get("pytest", {}).get(
                "ini_options", {})
            paths = tuple(opts.get("testpaths") or ())
            files = tuple(opts.get("python_files") or ())
            if paths or files:
                return (paths or _FALLBACK_TESTPATHS,
                        files or _FALLBACK_PYTHON_FILES)
    except Exception:  # noqa: BLE001
        logger.debug("[ProgressBoard] pyproject unreadable", exc_info=True)

    return _FALLBACK_TESTPATHS, _FALLBACK_PYTHON_FILES


def test_collection_config(
        repo_root: Optional[Path] = None,
) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """The project's own answer to "what is a test", cached per root."""
    root = Path(repo_root) if repo_root else _default_root()
    key = str(root)
    got = _TEST_CONFIG_CACHE.get(key)
    if got is None:
        got = _parse_pytest_config(root)
        _TEST_CONFIG_CACHE[key] = got
    return got


_TEST_CONFIG_CACHE: Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...]]] = {}


def _is_test_path(rel: str, repo_root: Optional[Path] = None) -> bool:
    """Tests import things without making them live.

    A module whose ONLY importers are tests is inert in production — that is
    precisely the state this board exists to surface, so test importers must
    not be allowed to launder a dark module into a live one.

    THE HARD PART IS THAT A NAME IS NOT A ROLE
    ------------------------------------------
    Two rules were tried before this one and both decided a file's role from
    the spelling of its path.

    The first matched substrings. ``"/test" in path`` is true of
    ``voice_unlock/testing/`` and ``scripts/testrunner_streaming_livefire.py``,
    so those production modules' flags never entered the board's universe;
    and ``"/conftest.py"`` is false of the repository-ROOT ``conftest.py``, so
    the one conftest every session loads counted as a production importer.

    The second matched segments, which fixed those four and left the deeper
    error in place: a bare ``name.endswith("_test.py")`` classifies
    ``scripts/ouroboros_battle_test.py`` — THE entry point that boots the
    six-layer stack, the thing :func:`scan_roots` was widened to include
    precisely because it "is how this system actually runs" — as a test. So
    did ``governance/test_runner.py``, ``intent/test_watcher.py``,
    ``intake/sensors/test_failure_sensor.py`` and
    ``intent/test_source_attribution.py``: production modules named for the
    thing they OPERATE ON rather than for what they are. Everything reachable
    only through them read DARK.

    SO ASK THE PROJECT
    ------------------
    pytest already answers this question, in configuration, for this exact
    repository::

        testpaths    = tests test_*.py
        python_files = test_*.py *_test.py

    A file is a test iff pytest would collect it: it matches ``python_files``
    AND lies under a declared ``testpaths`` entry. ``governance/test_runner.py``
    matches the name pattern and is under neither, so pytest never collects
    it and neither does this. The rule now tracks the project's own
    definition and moves when it moves, rather than restating a convention
    that a hundred and sixty-seven files in this tree do not follow.

    Two belts stay on top of that, both conservative in the safe direction —
    over-classifying as test costs a visible false DARK, under-classifying
    launders a dark module into live:

    * any ``test``/``tests`` DIRECTORY segment, wherever it sits;
    * ``conftest.py`` anywhere, since it is loaded by collection and never by
      production.
    """
    lowered = rel.replace("\\", "/").lower()
    parts = lowered.split("/")
    name = parts[-1] if parts else lowered

    if name == "conftest.py":
        return True
    if any(segment in ("test", "tests") for segment in parts[:-1]):
        return True

    testpaths, python_files = test_collection_config(repo_root)
    if not any(fnmatch.fnmatch(name, pat) for pat in python_files):
        return False

    for entry in testpaths:
        cleaned = entry.strip("/").lower()
        if not cleaned:
            continue
        if "*" in cleaned or "?" in cleaned:
            # A bare glob in `testpaths` addresses files at the root — pytest
            # resolves testpaths relative to the rootdir, and fnmatch's `*`
            # would otherwise cross directory boundaries and re-swallow
            # `scripts/ouroboros_battle_test.py` through the back door.
            if len(parts) == 1 and fnmatch.fnmatch(name, cleaned):
                return True
            continue
        if lowered == cleaned or lowered.startswith(f"{cleaned}/"):
            return True
    return False


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

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "FeatureRow":
        """Inverse of :meth:`to_dict`, tolerant of a row written by an older
        build.

        Missing keys take the field default rather than raising: a cache is a
        performance artefact and must never be the thing that breaks the
        reader. Round-trip fidelity is proven by test, not assumed here —
        ``kind`` and ``is_actionable`` are DERIVED properties, so a row that
        restores its eight stored fields restores its behaviour too.
        """
        def _text(key: str) -> str:
            got = raw.get(key)
            return got if isinstance(got, str) else ""

        return cls(
            flag=_text("flag"),
            state=_text("state") or UNKNOWN,
            module=_text("module"),
            category=_text("category"),
            enabled=raw.get("enabled") if isinstance(
                raw.get("enabled"), bool) else None,
            importers=int(raw.get("importers") or 0),
            reason=_text("reason"),
            value=raw.get("value"),
        )


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
    #: The inputs this reading was derived from — tree + environment + roots
    #: + board source. Empty when the reading was not fingerprinted.
    fingerprint: str = ""
    #: Whether the rows were restored rather than scanned. Provenance, in the
    #: same spirit as `advisor_locality`'s blast_provenance: a consumer that
    #: cannot tell a measurement from a recollection will eventually present
    #: one as the other.
    from_cache: bool = False

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
        #: module -> a production file that spells its dotted name as a
        #: STRING. Evidence of a relationship, never of liveness: the only
        #: such list in this repo that is not a provider-package list is an
        #: EXCLUSION list. Annotates a dark row; decides nothing.
        self._string_refs: Dict[str, str] = {}
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
        #: alias -> canonical module name, for BOTH spellings (see `_row_for`).
        #: Built during the walk so string references can be resolved against
        #: real modules afterwards rather than believed on sight.
        aliases: Dict[str, str] = {}
        #: dotted string literal -> the production file that spelled it.
        string_refs: Dict[str, str] = {}
        roots = scan_roots()
        prefix = flag_prefix()
        scanned = 0
        for rel, path in _iter_source_files(self._repo_root, roots):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
                continue
            scanned += 1
            self_mod = _module_name(rel)
            # Every module gets an alias entry regardless of test-ness: this is
            # the NAME index, not the evidence index. Excluding test modules
            # here would only mean a string reference to one resolved to
            # nothing, which is silence where a decision belongs.
            aliases.setdefault(self_mod, self_mod)
            for root in roots:
                root_prefix = f"{root}."
                if self_mod.startswith(root_prefix):
                    aliases.setdefault(self_mod[len(root_prefix):], self_mod)
            is_test = _is_test_path(rel, self._repo_root)
            if not is_test:
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
                    entries.add(self_mod)
                marker = _semantic_marker(tree, rel)
                if marker:
                    shadows[self_mod] = marker
                if string_ref_edges_enabled():
                    for dotted in _dotted_module_strings(tree):
                        # A module naming ITSELF proves nothing, and a
                        # registry listing its own package would otherwise
                        # vouch for itself.
                        if dotted != self_mod:
                            string_refs.setdefault(dotted, rel)
                for flag, dflt in _flag_literals(tree, prefix):
                    if flag not in flags:
                        flags[flag] = rel
                        defaults[flag] = dflt

        # -- resolve string references into ANNOTATIONS, never into edges ----
        #
        # Deliberately AFTER the walk: a reference can only be resolved once
        # the module index is complete, and resolving eagerly would make the
        # answer depend on directory order.
        #
        # This started out promoting a referenced module to DYNAMIC_LIVE and
        # that was WRONG, in the most instructive way available. The only two
        # production sites in this repo that name modules as dotted strings are
        # `repl_dispatch_registry`'s provider PACKAGES and
        # `observability_route_registry._SUBSTRATE_EXCLUSIONS` — and the second
        # is a list of modules the registry refuses to mount. Being named by a
        # registry is not being mounted by one; eight flags were promoted on
        # the strength of an exclusion list before that was checked.
        #
        # Distinguishing a mount list from an exclusion list statically means
        # dataflow analysis, which is guessing with extra steps. So the
        # detection is kept and the CONCLUSION is dropped: a string reference
        # annotates a dark row with the file to go read, and changes no state.
        # Actual mounting is measured where it is measurable — by the
        # `register_routes` / `dispatch_*_command` conventions the registries
        # really dispatch on (see `marker_functions`).
        resolved_refs: Dict[str, str] = {}
        for dotted, ref_file in string_refs.items():
            canonical = aliases.get(dotted)
            if not canonical or canonical == _module_name(ref_file):
                continue
            resolved_refs.setdefault(canonical, ref_file)
        self._string_refs = resolved_refs

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

    # -- cached reading ----------------------------------------------------

    def fingerprint(self) -> str:
        """Everything a reading depends on, as one hex string.

        Four inputs, because a reading is a function of exactly four things:
        the source tree, the ``JARVIS_*`` environment, the scan roots, and the
        board's own logic. Omitting any one produces a cache that is stale in
        a way its consumer cannot detect.
        """
        roots = tuple(scan_roots())
        parts = (
            CACHE_SCHEMA,
            str(self._repo_root),
            ",".join(roots),
            "1" if board_enabled() else "0",
            _board_source_digest(),
            _environ_digest(),
            _tree_digest(self._repo_root, roots),
        )
        return hashlib.sha256(
            "\0".join(parts).encode("utf-8", "replace"),
        ).hexdigest()

    def cached_read(self) -> BoardReading:
        """:meth:`read`, memoised on :meth:`fingerprint`. NEVER raises.

        The scan is ~157 s on this repository and the walk that identifies it
        is ~1.2 s, so a hit is two orders of magnitude cheaper than a miss.
        That ratio is the whole point: an invariant nobody runs because it is
        slow catches nothing, and the honest way to make a check fast is to
        avoid repeating work whose inputs have not moved — not to look at
        fewer files.

        What is NOT restored from cache, deliberately:

        ``verbs`` / ``verbs_primed`` come from a runtime registry, not from
        the tree. They describe THIS process — whether discovery has run yet —
        and are re-read live on every call, hit or miss. Caching them would
        let a reading assert that a cockpit had primed its verbs because some
        earlier process did.

        ``duration_s`` reports how long THIS call took, so a hit reports the
        hit. A cache that reported the original scan's 157 s would be lying
        about the only thing the operator can verify with a stopwatch.
        """
        started = time.monotonic()
        if not cache_enabled():
            return self.read()

        try:
            key = self.fingerprint()
        except Exception:  # noqa: BLE001
            logger.debug("[ProgressBoard] fingerprint failed", exc_info=True)
            return self.read()

        restored = self._load_cache(key)
        if restored is not None:
            # Live, never restored — see the docstring.
            restored.verbs, restored.verbs_primed = self._load_verbs()
            restored.fingerprint = key
            restored.from_cache = True
            restored.duration_s = time.monotonic() - started
            return restored

        reading = self.read()
        reading.fingerprint = key
        reading.from_cache = False
        # A degraded reading is a reading of something other than the tree —
        # a disabled board, an unavailable registry, an exception mid-scan.
        # Storing it would serve that transient failure back for as long as
        # nothing in the tree moves.
        if not reading.degraded:
            self._store_cache(key, reading)
        return reading

    async def cached_read_async(self) -> BoardReading:
        """:meth:`cached_read` off the event loop. Both the fingerprint walk
        and the scan it may trigger are blocking filesystem work."""
        try:
            return await asyncio.to_thread(self.cached_read)
        except AttributeError:  # pragma: no cover — <3.9 safety
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, self.cached_read)

    def _load_cache(self, key: str) -> Optional[BoardReading]:
        """The stored reading if it was taken under `key`, else None."""
        path = cache_path(self._repo_root)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        except Exception:  # noqa: BLE001
            logger.debug("[ProgressBoard] cache unreadable", exc_info=True)
            return None
        if not isinstance(payload, dict):
            return None
        # Both guards matter. `schema` catches a payload written by a build
        # whose ROW SHAPE differs — where `fingerprint` might coincidentally
        # match because the tree did not move across the upgrade.
        if payload.get("schema") != CACHE_SCHEMA:
            return None
        if payload.get("fingerprint") != key:
            return None
        raw_rows = payload.get("rows")
        if not isinstance(raw_rows, list):
            return None
        try:
            rows = [FeatureRow.from_dict(r) for r in raw_rows
                    if isinstance(r, Mapping)]
        except Exception:  # noqa: BLE001
            logger.debug("[ProgressBoard] cache rows unusable", exc_info=True)
            return None
        if len(rows) != len(raw_rows):
            # A row that would not restore means the file is not what it
            # claims. Rescan rather than serve a reading with holes in it.
            return None
        return BoardReading(
            rows=rows,
            scanned_files=int(payload.get("scanned_files") or 0),
        )

    def _store_cache(self, key: str, reading: BoardReading) -> None:
        """Write the reading. NEVER raises — a cache miss is not an error, and
        an unwritable cache must not break the board."""
        path = cache_path(self._repo_root)
        payload = {
            "schema": CACHE_SCHEMA,
            "fingerprint": key,
            "scanned_files": reading.scanned_files,
            "rows": [row.to_dict() for row in reading.rows],
        }
        # Temp-then-replace, in the SAME directory so the rename is atomic on
        # POSIX. A reader concurrent with a write sees the whole old file or
        # the whole new one, never a truncated payload it would then have to
        # decide how much to trust. `os.getpid()` keeps two concurrent
        # writers off each other's temp file; the losing rename is harmless
        # because both wrote the same bytes for the same key.
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps(payload, separators=(",", ":"), default=str),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except Exception:  # noqa: BLE001
            logger.debug("[ProgressBoard] cache not written", exc_info=True)
            try:
                tmp.unlink()
            except OSError:
                pass

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
        # BOTH spellings of the same module.
        #
        # `backend/` is on `sys.path` at runtime, so 597 files import their
        # siblings as `from core.x import y` rather than
        # `from backend.core.x import y`. This resolves FILE PATHS to the
        # fully-qualified form, so the two never matched and every module
        # imported that way counted zero importers and reported DARK.
        #
        # Found via `transport_handlers`: flagged dark-and-enabled with
        # destructive COMPUTER_USE defaults, and imported three times from
        # `backend/api/` as `from core.transport_handlers import ...`. It was
        # one edit away from being defaulted off as "unreached" while live.
        importers = int(self._importers.get(module, 0))
        for root in scan_roots():
            prefix = f"{root}."
            if module.startswith(prefix):
                importers += int(
                    self._importers.get(module[len(prefix):], 0))

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
            # The annotation belongs on BOTH dark exits, for the reason this
            # branch's own comment gives about ENTRY: a check that only one of
            # two exits consults is not a check. `JARVIS_IDE_POLICY_ROUTER_*`
            # is three non-boolean knobs and one switch on the same module, and
            # without this the switch carried the pointer to
            # `observability_route_registry` while the three knobs beside it
            # said only "state from graph".
            reason = ("non-boolean flag; state from graph" if state == LIVE
                      else f"non-boolean flag; {self._dark_reason(module)}")
            return FeatureRow(flag, state, module_rel, category, None,
                              importers, reason, value)
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
                self._dark_reason(module), value,
            )
        return FeatureRow(flag, LIVE, module_rel, category, enabled, importers,
                          f"{importers} production importer(s)", value)

    def _dark_reason(self, module: str) -> str:
        """Why it is dark — and, when there is one, where to go look.

        A bare "nothing imports it" is true and leaves the operator to find
        out for themselves whether it is unreachable or merely reached by a
        mechanism an import graph cannot see. When some production file spells
        this module's dotted name as a string, that file is the first place
        worth opening, and saying so costs one clause.

        It stays a QUESTION rather than becoming an answer, because the answer
        genuinely differs per site: in this repo the same shape is a provider
        list in one registry and an exclusion list in another.
        """
        base = "enabled but NOTHING in production imports it"
        ref = self._string_refs.get(module)
        if not ref:
            return base
        return (f"{base} — but {ref} names it as a string; "
                f"check whether that list mounts or excludes")


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
    #: Candidate edges from a lazy table — kept only if this file turns out
    #: to import dynamically. Collected in THIS walk rather than a second
    #: one: the board parses thousands of files, and a extra `ast.walk` per
    #: file is a real cost for an instrument people run interactively.
    lazy: Set[str] = set()
    dynamic = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.Call):
            fn = node.func
            if (getattr(fn, "attr", None) or getattr(fn, "id", None)) in (
                    "import_module", "__import__"):
                dynamic = True
        elif isinstance(node, ast.Constant):
            # Rejection is INLINE and the call is not. String constants are
            # the most common node in this codebase by a wide margin, and a
            # function call per literal cost 34% of a scan that was already
            # the slowest thing this instrument does. Two `startswith`
            # checks reject essentially all of them.
            v = node.value
            if type(v) is str and (v[:1] == "." or v[:8] == "backend."):
                edge = _lazy_edge(v, self_mod, is_package)
                if edge:
                    lazy.add(edge)
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
    # Only a file that actually imports dynamically gets its string literals
    # counted. A dotted-looking literal elsewhere is a message, a regex or a
    # config key — counting it would trade one false DARK for a much larger
    # class of false LIVE, and a board that over-reports reachability hides
    # dead code, which is strictly worse than one that under-reports and only
    # asks for a second look.
    if dynamic:
        found |= lazy
    return found


def _lazy_edge(value: Any, self_mod: str, is_package: bool) -> str:
    """A module path named as a STRING, resolved. "" when it is not one.

    The fourth blind spot of this instrument, and the same shape as the other
    three: an edge that exists at runtime with no `import` statement to find.
    `backend/core/__init__.py` maps public names to `(".jarvis_core",
    "JARVISCore")` and resolves them in a PEP 562 `__getattr__` — so
    `jarvis_core` is imported on essentially every boot and was reported DARK,
    because a string constant is not an import node.

    Relative forms go through `_resolve_relative`, the same function the
    `ImportFrom` branch uses, so the two syntaxes cannot disagree about what
    they name. Absolute forms are `backend.`-anchored so an arbitrary dotted
    string — a version, a hostname, a metric key — cannot enter.

    Deliberately does NOT model which BRANCH resolves a given entry. A lazy
    table is a promise that every entry is reachable; proving which ones a
    particular run takes needs execution, and this instrument is static.
    """
    if not isinstance(value, str) or not value:
        return ""
    if " " in value or "\n" in value:
        return ""
    if value.startswith("."):
        level = len(value) - len(value.lstrip("."))
        return _resolve_relative(
            self_mod, level, value.lstrip("."), is_package) or ""
    if value.startswith("backend."):
        return value
    return ""






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
    # Both entries were MEASURED against the registry that consumes them, not
    # guessed from a plausible-looking shape.
    #
    #   dispatch_*_command  `repl_dispatch_registry` walks `*_repl` modules for
    #                       module-level callables of this name.
    #   register_routes     `observability_route_registry` walks its provider
    #                       packages for a module-level callable named exactly
    #                       this and mounts it on the HTTP app at boot.
    #
    # The second was the sixth missing edge. Eleven modules define
    # `register_routes` and are imported by NOTHING — `decisions_observability`,
    # `bus_observability`, `causal_observability` and eight more — so they read
    # DARK while serving routes on every session. Naming the individual modules
    # would have been a list that rots; naming the CONVENTION is the same
    # measured move that made `dispatch_*_command` correct.
    return ("dispatch_*_command", "register_routes")


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


def string_ref_edges_enabled() -> bool:
    """``JARVIS_PROGRESS_BOARD_STRING_REFS`` (default on). NEVER raises."""
    return os.environ.get(
        "JARVIS_PROGRESS_BOARD_STRING_REFS", "1",
    ).strip().lower() not in ("0", "false", "no", "off")


#: A dotted path with identifier-shaped segments and nothing else. The
#: anchors matter: without them `"see backend.core.x for details"` matches on
#: a substring and prose starts voting on reachability.
_DOTTED_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$")


def _docstring_constants(tree: ast.AST) -> Set[int]:
    """Node ids of every docstring, so prose cannot be read as a reference.

    This is the single most-repeated defect in this codebase's audit tooling
    and it has now appeared four times: a docstring is an `ast.Constant` like
    any other string, so a module that DOCUMENTS its collaborator
    (``"the HTTP write surface lives in ide_policy_router.py"``) would vouch
    for it as loudly as a registry that actually mounts it.

    Position is the only reliable discriminator — a docstring is the first
    statement of a module, class, or function body — so that is what is
    matched, rather than any guess from the content.
    """
    out: Set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            out.add(id(first.value))
    return out


def _dotted_module_strings(tree: ast.AST) -> Set[str]:
    """Dotted module paths spelled as STRING LITERALS in executable positions.

    The mechanism this exists for is real and this codebase uses it twice:
    `observability_route_registry` and `repl_dispatch_registry` both hold
    tuples of dotted module names that are mounted through
    `importlib.import_module` at boot. Nothing imports those modules, so the
    graph reports them DARK while they serve HTTP on every session — the sixth
    time the board has been right about its own graph and the graph has been
    missing an edge.

    Three filters, each earning its place:

      * **docstrings excluded** — see :func:`_docstring_constants`.
      * **anchored identifier shape** — kills prose, `"e.g."`, version
        strings, URLs and dotted attribute chains in messages.
      * **`.py` suffix rejected** — `"category_weight_rebalancer.py"` passes
        the identifier shape (both segments are valid identifiers) and is a
        FILENAME in prose, not a module path. This codebase writes exactly
        that, repeatedly, in the loader docstrings that first misled this
        audit.

    An f-string (`ast.JoinedStr`) is deliberately NOT resolved: its value is
    not knowable statically, and inventing one would make this instrument the
    kind of thing it was built to catch. Unresolvable stays unreported.

    The real gate is downstream — a string only becomes an edge if it resolves
    to a module the walk actually found — so this can afford to be generous
    and let the index be strict.
    """
    docs = _docstring_constants(tree)
    out: Set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or id(node) in docs:
            continue
        value = node.value
        if not isinstance(value, str) or "." not in value:
            continue
        if len(value) > 256 or value.endswith(".py"):
            continue
        if _DOTTED_RE.match(value):
            out.add(value)
    return out


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
        # Provenance on the same line as the cost, because they answer one
        # question: is this a measurement or a recollection? `ov_demo` used to
        # refuse caching outright on the grounds that "the operator cannot
        # tell which they are looking at" — which was the right objection to
        # an untagged cache, and is answered by saying so here.
        origin = "cached" if reading.from_cache else "scanned"
        out.append(f"  ({reading.scanned_files} files, "
                   f"{reading.duration_s:.1f}s, {origin})"[:cols])
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
