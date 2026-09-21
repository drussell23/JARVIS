"""Whether this interpreter can actually run the work we are about to dispatch.

Two scopes, one question, so they cannot drift apart:

* **The runtime's own closure** -- can O+V itself run? Asked once, at boot,
  before the Sentinel arms. A negative answer is an
  :class:`EnvironmentDesyncFault` and the cockpit refuses rather than burning a
  soak on a starved venv.
* **One goal's target** -- can the module this goal edits even be imported, so
  that a test over it could pass? Asked per candidate during discovery. A
  negative answer DEMOTES the goal; it does not delete it.

## The measurement that produced this module

The operator's report was "it keeps doing the same thing over and over". The
injected lessons were suspected of being low-entropy. They were not -- they
carry module, phase, exact test name, exact exception and occurrence count.
Measured on the live roadmap instead:

    51 goals | 29 BLOCKED by an unresolvable import | 22 importable

Seventeen of the twenty-nine were blocked on ``fastapi``, which
``requirements.txt`` DECLARES at ``fastapi==0.124.2`` and which was absent from
the venv. The model was being asked, over and over, to write tests for modules
that cannot be imported in this environment. No lesson can fix that, and every
retry failed identically -- which is exactly what repetition looks like from
the transcript.

## Why this does NOT hash the manifest against ``pip freeze``

That was the obvious design and it is wrong here, provably::

    $ uv pip install --dry-run -r requirements.txt
    x No solution found when resolving dependencies:
      '-> Because there is no version of torchaudio==2.12.0 ...

The root manifest is UNSATISFIABLE -- and beyond that it is a superset spanning
macOS-only wheels (``pyobjc-framework-libdispatch``) and the ML monolith, of
which 95 of its 167 entries are absent from this venv BY DESIGN. A gate that
refuses to boot whenever the manifest and the environment differ would refuse
forever: a fail-closed brake that never opens, which is worse than the disease
it treats.

So the boot gate asserts the manifests that declare THE RUNTIME's closure --
the empirically-derived governance and ov-surface sets, each of which says so
in its own header -- and those are satisfiable, which is what makes a refusal
mean something. Drift in the wider manifest is reported, never fatal.

## Why the per-goal check is not the same check

Importability is not a property of the environment alone; it is a property of
this target IN this environment. ``backend/api/enhanced_vision_api.py`` needs
``torch``; ``backend/core/ouroboros/governance/production_oracle.py`` needs
nothing. One global question would either quarantine everything or nothing, so
the per-goal path resolves the target's own import closure and answers for that
goal alone.
"""
from __future__ import annotations

import ast
import importlib.metadata as _md
import importlib.util
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, FrozenSet, Iterator, List, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.EnvironmentIntegrity")

__all__ = [
    "EnvironmentDesyncFault",
    "EnvironmentVerdict",
    "TargetImportVerdict",
    "UNRESOLVABLE_TARGET_DEPENDENCY",
    "PLATFORM_UNAVAILABLE",
    "PlatformVerdict",
    "platform_capability",
    "structurally_unavailable",
    "DEPENDENCY_VIOLATION",
    "ENVIRONMENT_STARVATION",
    "ImportFailureVerdict",
    "classify_import_failure",
    "declared_distribution_names",
    "runtime_manifests",
    "environment_verdict",
    "assert_environment",
    "unresolvable_imports",
    "target_import_verdict",
    "boot_gate_enabled",
    "quarantine_enabled",
]

#: The telemetry reason a demoted goal carries, so a trace can be grepped for
#: "why did nothing land" without re-deriving the cause from scratch.
UNRESOLVABLE_TARGET_DEPENDENCY = "unresolvable_target_dependency"

_ENV_MANIFESTS = "JARVIS_RUNTIME_MANIFESTS"
_ENV_BOOT_GATE = "JARVIS_ENV_PREFLIGHT_ENABLED"
_ENV_QUARANTINE = "JARVIS_QUARANTINE_UNIMPORTABLE_TARGETS"

#: Manifests are found by NAME, never by path. The governance profile has
#: already moved once (``governance/sandbox_profiles/`` ->
#: ``backend/core/ouroboros/governance/sandbox_profiles/``), and a hardcoded
#: path would have turned this gate into a silent no-op the day it moved -- the
#: failure mode this repo keeps rediscovering.
_RUNTIME_MANIFEST_NAMES = (
    "requirements-governance.txt",
    "requirements-ov-surface.txt",
)

#: Hidden directories are pruned wholesale rather than enumerated. The live
#: tree carries ``.worktrees/ouroboros__auto__bt-*`` sandbox CLONES of the
#: whole repo, so an enumerated denylist returned four copies of every manifest
#: -- the same contract asserted four times -- and would have grown a new hole
#: every time a sandbox directory was renamed.
_SKIP_DIRS = frozenset({"node_modules", "venv", "build", "dist", "site-packages"})


def _prunable(name: str) -> bool:
    return name.startswith(".") or name in _SKIP_DIRS

#: Cache of the AST answer ONLY. It is a pure function of file content, so it
#: is safe to hold keyed on ``(path, mtime_ns)``. The ENVIRONMENT answer is
#: deliberately never cached: a package can be installed while the daemon runs,
#: and a stale "missing" would quarantine work that has just become possible.
_ast_cache: Dict[Tuple[str, int], Tuple[str, ...]] = {}


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def boot_gate_enabled() -> bool:
    """The boot asserter. ON by default -- a starved runtime must not arm."""
    return _flag(_ENV_BOOT_GATE, True)


def quarantine_enabled() -> bool:
    """Whether an unimportable target becomes UNDISPATCHABLE rather than merely
    sorted last.

    OFF by default, and that default is load-bearing. On the live roadmap 21 of
    51 goals have an unresolvable import -- most of them macOS-only (``Quartz``,
    ``pyautogui``) or the multi-gigabyte ML stack. Quarantining by default would
    silently delete 41% of the queue on a judgement the operator has not made.
    Demotion already achieves the operator's actual goal: with 30 importable
    goals and a dispatch cap of 8, the blocked ones are never reached.
    """
    return _flag(_ENV_QUARANTINE, False)


# ---------------------------------------------------------------------------
# Scope 1 -- the runtime's own closure
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EnvironmentVerdict:
    """What the boot asserter found. ``satisfied`` is the only gate."""

    satisfied: bool
    reason: str
    missing: Tuple[str, ...] = ()
    mismatched: Tuple[Tuple[str, str, str], ...] = ()  # (name, declared, installed)
    manifests: Tuple[str, ...] = ()
    #: ``[(manifest, gaps), ...]`` per profile that fell short. Populated even
    #: when ``satisfied`` -- a profile the environment does not hold is worth
    #: saying out loud, and saying it is not the same as refusing over it.
    shortfalls: Tuple[Tuple[str, Tuple[str, ...]], ...] = ()

    def summary(self) -> str:
        if self.satisfied:
            return self.reason
        return f"{self.reason}: {', '.join(self.missing) or '-'}"


class EnvironmentDesyncFault(RuntimeError):
    """The declared runtime closure is not installed. Raised at boot only."""

    def __init__(self, verdict: "EnvironmentVerdict") -> None:
        super().__init__(verdict.summary())
        self.verdict = verdict


def _canonical(name: str) -> str:
    """PEP 503 normalisation, so ``PyYAML``, ``typing_extensions`` and
    ``ruamel.yaml`` compare equal to what the installed metadata reports."""
    return re.sub(r"[-_.]+", "-", str(name or "")).strip().lower()


def runtime_manifests(repo_root: Path) -> Tuple[Path, ...]:
    """The manifests that declare what O+V ITSELF needs, resolved dynamically.

    An explicit ``JARVIS_RUNTIME_MANIFESTS`` (``os.pathsep``- or
    comma-separated, repo-relative or absolute) wins, so an operator can point
    the gate at a different profile without editing code. Otherwise they are
    found by filename anywhere in the tree.

    Returns ``()`` when nothing resolves -- and the caller must then PASS. A
    gate that cannot find its own contract has no standing to refuse.
    """
    override = os.environ.get(_ENV_MANIFESTS, "")
    if override.strip():
        out: List[Path] = []
        for chunk in re.split(r"[,%s]" % re.escape(os.pathsep), override):
            chunk = chunk.strip()
            if not chunk:
                continue
            p = Path(chunk)
            p = p if p.is_absolute() else (Path(repo_root) / p)
            if p.is_file():
                out.append(p)
        return tuple(out)

    found: List[Path] = []
    try:
        for dirpath, dirnames, filenames in os.walk(Path(repo_root)):
            dirnames[:] = [d for d in dirnames if not _prunable(d)]
            for fn in filenames:
                if fn in _RUNTIME_MANIFEST_NAMES:
                    found.append(Path(dirpath) / fn)
    except Exception:  # noqa: BLE001 -- an unwalkable tree just means no contract
        logger.debug("[EnvIntegrity] manifest discovery degraded", exc_info=True)
    found.sort(key=lambda p: (str(p).count(os.sep), str(p)))
    return tuple(found)


def _declared_requirements(manifest: Path) -> List[Tuple[str, Optional[str], str]]:
    """``[(canonical_name, pinned_version_or_None, raw_line), ...]``.

    Deliberately NOT a full PEP 508 parser. Only two facts are used -- the name
    and an ``==`` pin -- and inventing a parser for markers and extras would put
    a second, weaker copy of pip's rules in the boot path.
    """
    out: List[Tuple[str, Optional[str], str]] = []
    try:
        text = manifest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        raw = line.split("#", 1)[0].strip()
        if not raw or raw.startswith("-"):
            continue
        # A line carrying an environment marker is not an unconditional promise
        # about THIS host, so only its name half is read.
        head = raw.split(";", 1)[0]
        name = re.split(r"[<>=!~\[ ]", head, 1)[0].strip()
        if not name:
            continue
        m = re.search(r"==\s*([^\s,;]+)", head)
        out.append((_canonical(name), m.group(1) if m else None, raw.strip()))
    return out


def _installed_distributions() -> Dict[str, str]:
    inst: Dict[str, str] = {}
    try:
        for dist in _md.distributions():
            try:
                name = dist.metadata["Name"]
            except Exception:  # noqa: BLE001 -- a damaged dist is not a verdict
                continue
            if name:
                inst[_canonical(name)] = str(getattr(dist, "version", "") or "")
    except Exception:  # noqa: BLE001
        logger.debug("[EnvIntegrity] distribution scan degraded", exc_info=True)
    return inst


def environment_verdict(repo_root: Path) -> EnvironmentVerdict:
    """Compare the declared RUNTIME closure against what is installed.

    NEVER raises. Absent declarations and an unreadable environment both come
    back ``satisfied=True`` with a reason that says which -- because "I could
    not measure" and "I measured starvation" must not produce the same brake.
    """
    try:
        manifests = runtime_manifests(Path(repo_root))
        if not manifests:
            return EnvironmentVerdict(
                True, "no runtime manifest declared -- nothing to assert",
            )
        installed = _installed_distributions()
        if not installed:
            return EnvironmentVerdict(
                True, "environment not introspectable -- refusing to accuse",
                manifests=tuple(str(m) for m in manifests),
            )
        # PER PROFILE, and the environment need only match ONE of them.
        #
        # The first cut asserted the UNION of every discovered profile, and
        # that is the same mistake this module was written to avoid, one level
        # down: it asserts a closure the environment was never meant to hold.
        # It broke CI within minutes of merging. The `ov-surface` lane installs
        # ONLY `ci/requirements-ov-surface.txt` -- its workflow says so, and
        # calls that file its single source of truth -- so the governance
        # profile is legitimately absent there, the gate refused, and four
        # routing tests failed with `assert 78 == 0`.
        #
        # A profile is a SHAPE the environment may take, not a clause in one
        # long contract. The governance profile is the organism's closure; the
        # ov-surface profile is the CLI lane's. An environment satisfying
        # either is coherent and must boot. Only an environment matching NO
        # declared shape is starved.
        #
        # The unsatisfied profiles are still reported, loudly, because that is
        # how `pyflakes>=3` was found -- and a warning naming it would have
        # been just as discoverable as a refusal, without bricking a lane that
        # is provisioned exactly as intended.
        satisfied: List[str] = []
        shortfalls: List[Tuple[str, Tuple[str, ...]]] = []
        mismatched: List[Tuple[str, str, str]] = []
        total = 0
        for manifest in manifests:
            declared = _declared_requirements(manifest)
            if not declared:
                continue
            total += 1
            gaps: List[str] = []
            for name, pin, raw in declared:
                have = installed.get(name)
                if have is None:
                    gaps.append(raw)
                elif pin and have != pin:
                    # ADVISORY only. A patch-level difference is drift, not
                    # starvation, and refusing to boot over it would make the
                    # gate the thing that stops the organism.
                    mismatched.append((name, pin, have))
            if gaps:
                shortfalls.append((str(manifest), tuple(gaps)))
            else:
                satisfied.append(str(manifest))

        if not total:
            return EnvironmentVerdict(
                True, "no runtime requirement declared -- nothing to assert",
                manifests=tuple(str(m) for m in manifests),
            )
        if not satisfied:
            # Flattened for the operator: every profile fell short, so every
            # gap is a gap, and naming them all is the actionable answer.
            every_gap = tuple(g for _m, gaps in shortfalls for g in gaps)
            return EnvironmentVerdict(
                False,
                f"no declared runtime profile is satisfied ({len(shortfalls)} "
                f"checked)",
                missing=every_gap, mismatched=tuple(mismatched),
                manifests=tuple(str(m) for m in manifests),
                shortfalls=tuple(shortfalls),
            )
        return EnvironmentVerdict(
            True,
            f"{len(satisfied)}/{total} declared runtime profile(s) satisfied",
            mismatched=tuple(mismatched),
            manifests=tuple(str(m) for m in manifests),
            shortfalls=tuple(shortfalls),
        )
    except Exception:  # noqa: BLE001 -- the gate never becomes the outage
        logger.debug("[EnvIntegrity] verdict degraded", exc_info=True)
        return EnvironmentVerdict(True, "assertion degraded -- proceeding")


def assert_environment(
    repo_root: Path, *, raise_on_desync: Optional[bool] = None,
) -> EnvironmentVerdict:
    """Boot gate. Returns the verdict; raises :class:`EnvironmentDesyncFault`
    when the closure is starved and the gate is armed."""
    verdict = environment_verdict(Path(repo_root))
    armed = boot_gate_enabled() if raise_on_desync is None else bool(raise_on_desync)
    for manifest, gaps in verdict.shortfalls:
        # Said out loud even on a PASS. This is how `pyflakes>=3` surfaced, and
        # a named warning is as discoverable as a refusal without bricking a
        # lane that is provisioned exactly as its workflow intends.
        logger.warning(
            "[EnvIntegrity] profile %s is not fully installed here (%d gap(s): "
            "%s)", Path(manifest).name, len(gaps), ", ".join(gaps[:6]),
        )
    if verdict.mismatched:
        logger.info(
            "[EnvIntegrity] %d declared pin(s) differ from installed: %s",
            len(verdict.mismatched),
            ", ".join(f"{n} {d}!={i}" for n, d, i in verdict.mismatched[:6]),
        )
    if not verdict.satisfied:
        logger.error(
            "[EnvIntegrity] EnvironmentDesyncFault -- %s", verdict.summary(),
        )
        if armed:
            raise EnvironmentDesyncFault(verdict)
    else:
        logger.debug("[EnvIntegrity] %s", verdict.reason)
    return verdict


# ---------------------------------------------------------------------------
# Scope 2 -- one goal's target
# ---------------------------------------------------------------------------

def _resolves(top: str) -> bool:
    """Whether a TOP-LEVEL module name can be found WITHOUT importing it.

    ``find_spec`` on a top-level name runs no module code. An unanswerable name
    returns True: a wrong accusation deletes real work, whereas a wrong pass
    costs one op that cooldown already bounds, and VALIDATE is perfectly
    capable of discovering a genuine ImportError on its own.
    """
    if not top:
        return True
    if top in sys.builtin_module_names:
        return True
    if top in getattr(sys, "stdlib_module_names", frozenset()):
        return True
    try:
        return importlib.util.find_spec(top) is not None
    except (ImportError, ValueError, AttributeError, TypeError):
        return False
    except Exception:  # noqa: BLE001
        return True


#: ``pythonpath = . backend`` in pytest.ini, or the TOML list form.
_PYTHONPATH_INI_RE = re.compile(r"^\s*pythonpath\s*=\s*(.+?)\s*$", re.M)
_PYTHONPATH_TOML_RE = re.compile(r"pythonpath\s*=\s*\[([^\]]*)\]", re.S)

_roots_cache: Dict[Tuple[str, int], Tuple[Path, ...]] = {}


def _pythonpath_roots(repo_root: Path) -> Tuple[Path, ...]:
    """The import roots the repository's OWN pytest configuration declares.

    ## The false positive this exists to kill

    Without it, ``import vision.multi_space_intelligence`` reads as an absent
    third-party package, because ``vision`` is a subpackage of ``backend/`` and
    only resolves when ``backend/`` is on ``sys.path``. The live census proved
    it in production: nine goals demoted for ``vision`` and three for ``core``
    — all of them perfectly importable under the pytest that actually runs
    VALIDATE, because ``pytest.ini`` declares ``pythonpath = . backend``.

    Accusing a first-party package of being a missing dependency is the worst
    outcome this module can produce: it demotes exactly the work that CAN land.
    So the roots are read from the repo's own config rather than assumed, and a
    layout that names no pythonpath simply gets none.
    """
    out: List[Path] = []
    root = Path(repo_root)
    for name in ("pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml"):
        cfg = root / name
        try:
            if not cfg.is_file():
                continue
            key = (str(cfg), int(cfg.stat().st_mtime_ns))
            hit = _roots_cache.get(key)
            if hit is not None:
                out.extend(hit)
                continue
            text = cfg.read_text(encoding="utf-8", errors="replace")
            chunks: List[str] = []
            if name.endswith(".toml"):
                m = _PYTHONPATH_TOML_RE.search(text)
                if m:
                    chunks = re.findall(r"['\"]([^'\"]+)['\"]", m.group(1))
            else:
                m = _PYTHONPATH_INI_RE.search(text)
                if m:
                    chunks = m.group(1).split()
            found = tuple(
                (root / c).resolve()
                for c in chunks
                if c and (root / c).is_dir()
            )
            _roots_cache[key] = found
            out.extend(found)
        except Exception:  # noqa: BLE001 — an unreadable config declares nothing
            continue
    # Dedup, order-preserving.
    seen: List[Path] = []
    for p in out:
        if p not in seen:
            seen.append(p)
    return tuple(seen)


def _resolves_under_roots(top: str, roots: Sequence[Path]) -> bool:
    """Whether *top* is a package or module under one of the declared roots."""
    for r in roots:
        try:
            if (r / f"{top}.py").is_file() or (r / top / "__init__.py").is_file():
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _third_party_tops(path: Path, repo_root: Path) -> Tuple[str, ...]:
    """Top-level names *path* imports that are NOT first-party, content-cached.

    Reuses ``ast_signature_anchor``'s two helpers rather than re-walking the
    AST: ``_imported_module_names`` for extraction and ``_resolve_first_party``
    for the in-repo test -- the same pair the prompt's signature anchor uses, so
    "first-party" means the same thing to this gate as it does to the prompt.
    """
    try:
        st = path.stat()
        key = (str(path), int(st.st_mtime_ns))
    except OSError:
        return ()
    hit = _ast_cache.get(key)
    if hit is not None:
        return hit
    try:
        from backend.core.ouroboros.governance.ast_signature_anchor import (  # noqa: PLC0415
            _imported_module_names,
            _resolve_first_party,
        )
        src = path.read_text(encoding="utf-8", errors="replace")
        roots = _pythonpath_roots(repo_root)
        tops: List[str] = []
        for mod in _imported_module_names(src):
            if _resolve_first_party(mod, path, repo_root) is not None:
                continue
            top = str(mod).split(".")[0]
            if not top or top in tops:
                continue
            # `_resolve_first_party` walks the IMPORTER's ancestors, which is
            # how Python finds a sibling — but not how it finds a package that
            # is first-party only because pytest puts its parent on sys.path.
            if _resolves_under_roots(top, roots):
                continue
            tops.append(top)
        out: Tuple[str, ...] = tuple(tops)
    except Exception:  # noqa: BLE001 -- an unparseable file accuses nobody
        out = ()
    if len(_ast_cache) > 4096:
        _ast_cache.clear()
    _ast_cache[key] = out
    return out


def unresolvable_imports(path: Path, repo_root: Path) -> Tuple[str, ...]:
    """Third-party top-level imports of *path* this interpreter cannot find."""
    return tuple(
        t for t in _third_party_tops(Path(path), Path(repo_root)) if not _resolves(t)
    )


@dataclass(frozen=True)
class TargetImportVerdict:
    """Whether a goal's subject can be imported at all in this environment."""

    importable: bool
    unresolvable: Tuple[str, ...] = ()
    inspected: Tuple[str, ...] = ()
    #: The subset of ``unresolvable`` that no install on THIS machine can fix.
    structural: Tuple[str, ...] = ()
    #: Subjects whose import EXECUTES A PROGRAM (``path: why``). No install and
    #: no host reverses this either -- only refactoring the subject does.
    executes: Tuple[str, ...] = ()
    #: First-party modules a subject imports UNCONDITIONALLY that do not exist
    #: (``subject -> module``). The subject cannot be imported by anyone until
    #: it is repaired, so no test of it can pass.
    broken: Tuple[str, ...] = ()

    @property
    def impossible(self) -> bool:
        """Impossible here, as opposed to merely not provisioned.

        The two demand different answers. An absent package is reversed by an
        install, so the goal is DEMOTED and rises again the moment its
        dependency lands. A macOS framework on Linux is reversed by nothing, so
        the goal is QUARANTINED -- removed from dispatch on this host while
        staying in the roadmap, selectable again the day the same repository is
        driven from a Mac. Deleting it would delete Mac functionality; leaving
        it dispatchable burns an op per pass forever.
        """
        return bool(self.structural or self.executes or self.broken)

    @property
    def reason(self) -> str:
        if self.importable:
            return ""
        if self.broken:
            return f"{FIRST_PARTY_IMPORT_MISSING}: {'; '.join(self.broken)}"
        if self.executes:
            return f"{IMPORT_EXECUTES_PROGRAM}: {'; '.join(self.executes)}"
        if self.structural:
            return f"{PLATFORM_UNAVAILABLE}: {', '.join(self.structural)}"
        return f"{UNRESOLVABLE_TARGET_DEPENDENCY}: {', '.join(self.unresolvable)}"


_IMPORTABLE_OK = TargetImportVerdict(True)


# ---------------------------------------------------------------------------
# Importing the subject runs a program
# ---------------------------------------------------------------------------

#: A subject that is a SCRIPT: it exposes nothing a test could call, and the
#: act of importing it executes it. Every validation this system performs is an
#: import-based unit test, so such a subject is out of reach until someone puts
#: its body in a function behind ``if __name__ == "__main__":``.
IMPORT_EXECUTES_PROGRAM = "import_executes_program"

#: Statements that only DECLARE. Anything else at import scope does work.
_DECLARATIVE_STMTS = (
    ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef,
    ast.ClassDef, ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Pass,
    ast.Global, ast.Nonlocal, ast.Delete,
)


def _is_main_guard(node: ast.AST) -> bool:
    if not isinstance(node, ast.If):
        return False
    test = node.test
    if not (isinstance(test, ast.Compare) and len(test.comparators) == 1):
        return False
    sides = (test.left, test.comparators[0])
    return (
        any(isinstance(s, ast.Name) and s.id == "__name__" for s in sides)
        and any(isinstance(s, ast.Constant) and s.value == "__main__" for s in sides)
    )


def _import_scope(body: Sequence[ast.stmt]) -> Iterator[ast.stmt]:
    """Every statement that runs when the module is imported: descends into
    compound statements, never into a def/class body or a ``__main__`` guard."""
    for node in body:
        if _is_main_guard(node):
            continue
        yield node
        if isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While)):
            yield from _import_scope(node.body)
            yield from _import_scope(node.orelse)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            yield from _import_scope(node.body)
        elif isinstance(node, ast.Try):
            yield from _import_scope(node.body)
            for handler in node.handlers:
                yield from _import_scope(handler.body)
            yield from _import_scope(node.orelse)
            yield from _import_scope(node.finalbody)


# ---------------------------------------------------------------------------
# The subject imports a first-party module that does not exist
# ---------------------------------------------------------------------------

#: The subject's own import statement names a module of THIS repository that
#: is not there. Not unprovisioned (nothing to install) and not platform-bound:
#: the subject is simply broken, and stays unimportable until it is repaired.
FIRST_PARTY_IMPORT_MISSING = "first_party_import_missing"

#: A package whose ``__init__`` shapes its own namespace cannot be judged from
#: the filesystem, so nothing beneath it is ever accused.
_DYNAMIC_PACKAGE_MARKERS = ("__path__", "sys.modules", "def __getattr__")

#: ``(path, mtime_ns, size) -> ((module, level), ...)`` -- the PARSE only. See
#: ``missing_first_party_imports`` for why the answer itself is never cached.
_mandatory_import_cache: Dict[Tuple[str, int, int], Tuple[Tuple[str, int], ...]] = {}


def _mandatory_imports(body: Sequence[ast.stmt]) -> Iterator[ast.stmt]:
    """Import statements that MUST succeed for the module to import at all.

    Excluded, because their failure is survivable or never happens at import:
    anything inside a ``try`` (the optional-dependency idiom), under any ``if``
    (``TYPE_CHECKING``, ``__main__``, feature probes), or inside a def/class.
    """
    for node in body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield node
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            yield from _mandatory_imports(node.body)


def _dotted_module_exists(base: Path, parts: Sequence[str]) -> bool:
    """Whether dotted *parts* names a module or package under *base*.

    Errs toward True everywhere it cannot be sure: a dynamic package, an
    unreadable ``__init__``, a namespace directory, a compiled extension, or a
    path that continues past a plain ``.py`` file (attribute access, which the
    filesystem cannot speak to).
    """
    cur = Path(base)
    for index, part in enumerate(parts):
        last = index == len(parts) - 1
        pkg = cur / part
        if pkg.is_dir():
            init = pkg / "__init__.py"
            if init.is_file():
                try:
                    head = init.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    return True
                if any(marker in head for marker in _DYNAMIC_PACKAGE_MARKERS):
                    return True
            cur = pkg
            continue
        if (cur / f"{part}.py").is_file():
            return True
        if last and (any(cur.glob(f"{part}.*.so")) or any(cur.glob(f"{part}.pyd"))):
            return True
        return False
    return True


def missing_first_party_imports(path: Path, repo_root: Path) -> Tuple[str, ...]:
    """First-party modules *path* imports unconditionally that do not exist.

    ## The leak this closes

    ``_third_party_tops`` stops at the TOP of a dotted import: once ``vision``
    is found under a pythonpath root the import is filed as first-party and
    never looked at again. ``backend/jarvis_integrated_assistant.py`` opens
    with ``from vision.proactive_vision_assistant import ...`` -- ``vision`` is
    a real package, ``proactive_vision_assistant`` is not in it. The Sentinel
    chose that subject, the 30B wrote three different test files for it over
    three rounds, and all nine attempts failed identically:

        ModuleNotFoundError: No module named 'vision.proactive_vision_assistant'

    Sixteen minutes of a soak hour on a goal no candidate could pass. Measured
    on this repository: 27 of 3,767 modules (0.7%); on the absolute ones,
    ``importlib.util.find_spec`` agreed 9 of 9 with no dissent.

    ## What is cached, and what must not be

    The PARSE is cached -- which modules the subject imports is a function of
    the subject's bytes. The ANSWER is not: whether those modules exist is a
    function of the rest of the tree. The first draft cached the answer on the
    importer's mtime, and its own test caught it: repair the import by CREATING
    ``vision/soon.py`` and the subject is never touched, so a long-lived daemon
    would have held the quarantine forever -- a gate that cannot notice it has
    been satisfied. Existence is a handful of ``stat`` calls; it is re-asked
    every time. (Size rides in the key because Linux stamps mtime from a coarse
    clock, and two writes a few milliseconds apart can share one.)

    NEVER raises; a file that cannot be read or parsed accuses nobody.
    """
    try:
        path = Path(path)
        stat = path.stat()
        key = (str(path), int(stat.st_mtime_ns), int(stat.st_size))
        imports = _mandatory_import_cache.get(key)
        if imports is None:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            collected: List[Tuple[str, int]] = []
            for node in _mandatory_imports(tree.body):
                if isinstance(node, ast.Import):
                    collected.extend((alias.name, 0) for alias in node.names)
                else:
                    collected.append((node.module or "", int(node.level or 0)))
            imports = tuple(collected)
            if len(_mandatory_import_cache) > 4096:
                _mandatory_import_cache.clear()
            _mandatory_import_cache[key] = imports
        roots = _pythonpath_roots(Path(repo_root))
        found: List[str] = []
        for module, level in imports:
            parts = [p for p in module.split(".") if p]
            if level:
                base = path.parent
                for _ in range(level - 1):
                    base = base.parent
                if parts and not _dotted_module_exists(base, parts):
                    found.append("." * level + module)
                continue
            if len(parts) < 2:
                continue  # a bare top-level name belongs to the check above
            homes = [
                r for r in roots
                if (r / parts[0]).is_dir() or (r / f"{parts[0]}.py").is_file()
            ]
            if homes and not any(_dotted_module_exists(r, parts) for r in homes):
                found.append(module)
        return tuple(dict.fromkeys(found))
    except Exception:  # noqa: BLE001 -- includes SyntaxError / RecursionError
        return ()


def import_execution_hazard(path: Path, repo_root: Path) -> str:
    """``""`` when importing *path* only declares; else why it does not.

    ## What it caught, and what it cost before it existed

    The Sentinel chose, unprompted, to write tests for
    ``backend/start_minimal_with_upgrader.py`` -- 43 lines, no function, no
    class, no ``__main__`` guard, ending in ``subprocess.run(["tail", "-f",
    ...])``. The goal text demands "an import smoke test". The import started a
    backend server on port 8010 and blocked on ``tail`` until ``pytest-timeout``
    killed it: 14 minutes of VALIDATE per attempt for 3 seconds of generation,
    a second attempt in which the model invented ``get_version_info`` and
    ``perform_upgrade`` to have something to call, and a leaked server per
    candidate. No candidate, from any model, could have passed.

    ## The rule is structural -- there is no list of dangerous calls

    A module is a script when BOTH hold:

    * it exposes no importable API -- no ``def``/``class``, no ``__all__``,
      and no re-export (a relative import, or a ``from X import`` of a
      first-party module; a shim or a package ``__init__`` has an API even
      though it defines nothing itself); and
    * import scope does work -- an expression statement that is a call, a
      ``with``, or a ``while`` -- outside any ``__main__`` guard.

    Measured on this repository: 18 of 3,767 modules (0.5%), every one a
    launcher, a one-off fixer or an archived ad-hoc test. The three false
    positives of the first draft were all re-exporters, which is where the
    re-export clause came from.

    ## What it deliberately does not catch

    A module that defines functions AND also runs its program at import. That
    needs knowing which calls block, which is a list of names and a guess. A
    wrong accusation here quarantines real work, so the static rule stays
    narrow and the DYNAMIC backstop carries the rest: a hung import is now
    killed once, classified ``infra``, never learned as a duration, never
    retried, and its descendants are reaped (``process_session``).

    NEVER raises; an unreadable or unparseable subject is not a hazard.
    """
    try:
        path = Path(path)
        if path.suffix != ".py" or path.name == "__init__.py":
            return ""
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        roots = _pythonpath_roots(Path(repo_root))
        effectful: List[int] = []
        for node in _import_scope(tree.body):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                return ""
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = getattr(node, "targets", None) or [getattr(node, "target", None)]
                if any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
                    return ""
            elif isinstance(node, ast.ImportFrom):
                top = (node.module or "").split(".")[0]
                if node.level or (top and _resolves_under_roots(top, roots)):
                    return ""
            elif isinstance(node, ast.Expr):
                if isinstance(node.value, (ast.Call, ast.Await)):
                    effectful.append(node.lineno)
            elif isinstance(node, (ast.With, ast.AsyncWith, ast.While)):
                effectful.append(node.lineno)
        if not effectful:
            return ""
        return (
            f"defines nothing importable and executes {len(effectful)} "
            f"statement(s) at import (first: line {min(effectful)}) -- move the "
            f"body into a function behind `if __name__ == \"__main__\":` to "
            f"make it testable"
        )
    except Exception:  # noqa: BLE001 -- includes SyntaxError / RecursionError
        return ""


def target_import_verdict(
    target_files: Sequence[str],
    description: str,
    repo_root: Path,
) -> TargetImportVerdict:
    """Can the modules this goal must honour be imported here?

    Resolved through ``collect_anchor_sources`` -- the SAME ladder that builds
    the prompt's authoritative-signature block. That is the point: if the
    modules the candidate is TOLD to call cannot be imported, the test the
    candidate writes cannot pass, however good the candidate is. One
    resolution, two consumers, no second opinion to drift.

    NEVER raises; an unanswerable goal is reported importable, because the cost
    of a wrong accusation (work deleted) exceeds the cost of a wrong pass (one
    op, already bounded by cooldown).
    """
    try:
        from backend.core.ouroboros.governance.ast_signature_anchor import (  # noqa: PLC0415
            collect_anchor_sources,
        )
        root = Path(repo_root)
        sources = collect_anchor_sources(
            list(target_files or ()), description or "", root,
        )
        if not sources:
            return _IMPORTABLE_OK
        bad: List[str] = []
        inspected: List[str] = []
        executes: List[str] = []
        broken: List[str] = []
        for _label, src in sources:
            try:
                shown = str(Path(src).relative_to(root))
            except Exception:  # noqa: BLE001
                shown = str(src)
            inspected.append(shown)
            for top in unresolvable_imports(Path(src), root):
                if top not in bad:
                    bad.append(top)
            hazard = import_execution_hazard(Path(src), root)
            if hazard:
                executes.append(f"{shown} {hazard}")
            for module in missing_first_party_imports(Path(src), root):
                broken.append(f"{shown} -> {module}")
        if not bad and not executes and not broken:
            return _IMPORTABLE_OK
        return TargetImportVerdict(
            False, tuple(bad), tuple(inspected),
            structurally_unavailable(bad, root) if bad else (),
            tuple(executes), tuple(broken),
        )
    except Exception:  # noqa: BLE001
        logger.debug("[EnvIntegrity] target verdict degraded", exc_info=True)
        return _IMPORTABLE_OK


# ---------------------------------------------------------------------------
# Cross-platform capability -- impossible here, or merely not installed?
# ---------------------------------------------------------------------------

#: An unresolvable import that CANNOT be satisfied on this machine, ever. The
#: distinction from a merely-absent package is the whole point: absent is a
#: provisioning fact that an install reverses, and this is not.
PLATFORM_UNAVAILABLE = "platform_unavailable"

_DARWIN = frozenset({"darwin"})
_WINDOWS = frozenset({"win32", "cygwin"})
_ANY = frozenset({"darwin", "win32", "cygwin", "linux"})

#: Modules whose availability is an OS or hardware fact rather than a
#: provisioning one, as ``(platforms_that_can_run_it, needs_a_display)``.
#:
#: ## Why a registry and not pure derivation
#:
#: The authoritative mechanism is the PEP 508 environment marker, and it IS
#: honoured first -- ``coremltools>=7.0.0; platform_system == "Darwin"`` at
#: ``requirements.txt:159`` is read straight off the manifest. But a marker can
#: only speak for a DECLARED distribution, and the names that actually block
#: this queue are not declared at all: ``Quartz`` and ``AppKit`` are import
#: names from the PyObjC bridge, which no manifest in this repo names. There is
#: nothing to derive from. So the registry carries the facts that cannot be
#: derived, markers carry the ones that can, and adding a marker to a manifest
#: remains the self-documenting way to extend this -- see
#: ``JARVIS_PLATFORM_BOUND_MODULES`` for the third, operator-level lever.
_PLATFORM_BOUND: Dict[str, Tuple[FrozenSet[str], bool]] = {
    # The PyObjC bridge. These are Objective-C frameworks; there is no
    # non-macOS build and there never will be.
    "objc": (_DARWIN, False),
    "Quartz": (_DARWIN, False),
    "AppKit": (_DARWIN, False),
    "Foundation": (_DARWIN, False),
    "CoreFoundation": (_DARWIN, False),
    "CoreGraphics": (_DARWIN, False),
    "CoreMedia": (_DARWIN, False),
    "AVFoundation": (_DARWIN, False),
    "ApplicationServices": (_DARWIN, False),
    "ScreenCaptureKit": (_DARWIN, False),
    "Vision": (_DARWIN, False),
    "coremltools": (_DARWIN, False),
    # Windows-only.
    "win32api": (_WINDOWS, False),
    "win32gui": (_WINDOWS, False),
    "winreg": (_WINDOWS, False),
    # Everywhere, but only with a display server attached. NOT macOS-only and
    # NOT unavailable under WSLg, which publishes a real DISPLAY -- calling
    # these "headless-blocked" without asking the machine would quarantine
    # work that can in fact run here.
    "pyautogui": (_ANY, True),
    "pygetwindow": (_ANY, True),
    "pynput": (_ANY, True),
    "mss": (_ANY, True),
}

_ENV_PLATFORM_BOUND = "JARVIS_PLATFORM_BOUND_MODULES"


@dataclass(frozen=True)
class PlatformVerdict:
    """Whether *module* can exist on this machine at all."""

    available: bool
    module: str = ""
    reason: str = ""
    supported: Tuple[str, ...] = ()

    @property
    def detail(self) -> str:
        if self.available:
            return ""
        if self.reason == "display_required":
            return f"{self.module} needs an attached display; this session has none"
        return (
            f"{self.module} runs only on {'/'.join(self.supported) or '?'}; "
            f"this is {sys.platform}"
        )


_PLATFORM_OK = PlatformVerdict(True)


def _has_display() -> bool:
    """Whether a display server is attached. Asked, never assumed.

    macOS and Windows always have one from a process's point of view. On Linux
    it is ``DISPLAY`` or ``WAYLAND_DISPLAY`` -- and WSLg sets both, which is why
    this host is NOT headless and ``pyautogui`` is a missing package here
    rather than an impossible one.
    """
    if sys.platform != "linux":
        return True
    return bool(
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    )


def _operator_platform_bound() -> Dict[str, Tuple[FrozenSet[str], bool]]:
    """``JARVIS_PLATFORM_BOUND_MODULES='Quartz:darwin,foo:win32|linux'``."""
    out: Dict[str, Tuple[FrozenSet[str], bool]] = {}
    raw = os.environ.get(_ENV_PLATFORM_BOUND, "")
    if not raw.strip():
        return out
    try:
        for entry in raw.split(","):
            if ":" not in entry:
                continue
            name, plats = entry.split(":", 1)
            name = name.strip()
            keep = frozenset(p.strip() for p in plats.split("|") if p.strip())
            if name and keep:
                out[name] = (keep, False)
    except Exception:  # noqa: BLE001 -- a malformed override declares nothing
        logger.debug("[EnvIntegrity] platform override ignored", exc_info=True)
    return out


_marker_memo: Dict[Tuple[str, str], FrozenSet[str]] = {}


def _marker_excluded(repo_root: Path) -> FrozenSet[str]:
    """:func:`_scan_marker_excluded`, answered once per tree state.

    It walks the repository for every ``requirements*.txt``, and it was called
    once per unresolvable import per goal -- 50 walks, ~1 s, on every discovery
    pass. The answer is a pure function of the tree (the manifests are in
    ``repo_state``'s relevance set) and of the host, which does not change
    under a running process. With an UNKNOWN tree state nothing is kept.
    """
    try:
        from backend.core.ouroboros.governance import repo_state  # noqa: PLC0415
        state = repo_state.current_fingerprint(Path(repo_root))
    except Exception:  # noqa: BLE001
        state = ""
    if not state:
        return _scan_marker_excluded(repo_root)
    key = (str(repo_root), state)
    hit = _marker_memo.get(key)
    if hit is None:
        hit = _scan_marker_excluded(repo_root)
        _marker_memo.clear()  # one tree state at a time; no partial staleness
        _marker_memo[key] = hit
    return hit


def _scan_marker_excluded(repo_root: Path) -> FrozenSet[str]:
    """Canonical distribution names a manifest marker excludes on THIS host.

    Reads the repository's own declarations, so ``coremltools>=7.0.0;
    platform_system == "Darwin"`` is honoured without this module knowing what
    coremltools is. The marker is also the self-documenting way to teach the
    gate about a new platform-bound dependency: declare it with a marker and
    the queue stops dispatching it here, with no code change.
    """
    excluded: List[str] = []
    try:
        from packaging.markers import Marker  # noqa: PLC0415
    except Exception:  # noqa: BLE001 -- no evaluator, no exclusions
        return frozenset()
    try:
        for dirpath, dirnames, filenames in os.walk(Path(repo_root)):
            dirnames[:] = [d for d in dirnames if not _prunable(d)]
            for fn in filenames:
                if not (fn.startswith("requirements") and fn.endswith(".txt")):
                    continue
                try:
                    text = (Path(dirpath) / fn).read_text(
                        encoding="utf-8", errors="replace",
                    )
                except OSError:
                    continue
                for line in text.splitlines():
                    raw = line.split("#", 1)[0].strip()
                    if ";" not in raw or raw.startswith("-"):
                        continue
                    head, marker = raw.split(";", 1)
                    name = re.split(r"[<>=!~\[ ]", head, 1)[0].strip()
                    if not name:
                        continue
                    try:
                        if not Marker(marker.strip()).evaluate():
                            excluded.append(_canonical(name))
                    except Exception:  # noqa: BLE001 -- unparseable marker
                        continue
    except Exception:  # noqa: BLE001
        logger.debug("[EnvIntegrity] marker scan degraded", exc_info=True)
    return frozenset(excluded)


def platform_capability(
    module: str, *, repo_root: Optional[Path] = None,
) -> PlatformVerdict:
    """Can *module* exist on this machine at all?

    Three sources, most authoritative first: the repository's own PEP 508
    markers, the operator's ``JARVIS_PLATFORM_BOUND_MODULES`` override, and the
    empirical registry. Anything not named by any of them is reported AVAILABLE
    -- silence is not evidence of impossibility, and the demotion path already
    handles "absent".

    NEVER raises.
    """
    try:
        top = str(module or "").split(".")[0]
        if not top:
            return _PLATFORM_OK
        if repo_root is not None and _canonical(top) in _marker_excluded(repo_root):
            return PlatformVerdict(
                False, top, "marker_excluded", (f"not {sys.platform}",),
            )
        entry = _operator_platform_bound().get(top) or _PLATFORM_BOUND.get(top)
        if entry is None:
            return _PLATFORM_OK
        platforms, needs_display = entry
        if sys.platform not in platforms:
            return PlatformVerdict(
                False, top, "platform_bound", tuple(sorted(platforms)),
            )
        if needs_display and not _has_display():
            return PlatformVerdict(
                False, top, "display_required", tuple(sorted(platforms)),
            )
        return _PLATFORM_OK
    except Exception:  # noqa: BLE001
        logger.debug("[EnvIntegrity] platform capability degraded", exc_info=True)
        return _PLATFORM_OK


def structurally_unavailable(
    modules: Sequence[str], repo_root: Optional[Path] = None,
) -> Tuple[str, ...]:
    """The subset of *modules* that CANNOT be satisfied on this machine."""
    out: List[str] = []
    for mod in modules or ():
        verdict = platform_capability(mod, repo_root=repo_root)
        if not verdict.available and mod not in out:
            out.append(mod)
    return tuple(out)


# ---------------------------------------------------------------------------
# Scope 3 -- whose fault is this ImportError?
# ---------------------------------------------------------------------------

#: The candidate introduced an import this environment cannot satisfy. The
#: candidate is at fault and the change must not stand.
DEPENDENCY_VIOLATION = "dependency_violation"

#: The unresolvable import was ALREADY there -- in the subject, or in the file
#: before the candidate touched it. The candidate is blameless.
ENVIRONMENT_STARVATION = "environment_starvation"

#: ``ModuleNotFoundError: No module named 'x.y'`` in every dialect pytest and
#: CPython emit it. The quote style varies; the phrase does not.
_MISSING_MODULE_RE = re.compile(
    r"No module named ['\"]?([A-Za-z_][\w.]*)['\"]?"
)


@dataclass(frozen=True)
class ImportFailureVerdict:
    """Why an ImportError happened, and therefore who must change.

    ``kind`` is ``""`` when the evidence is not an import failure at all --
    the caller must then leave the existing classification alone rather than
    treat "not mine" as "no failure".
    """

    kind: str = ""
    module: str = ""
    declared: bool = False
    net_new: bool = False

    @property
    def is_violation(self) -> bool:
        return self.kind == DEPENDENCY_VIOLATION

    @property
    def lesson(self) -> str:
        """The advice a future GENERATE actually needs.

        The three cases were collapsed into one ``import_error`` class whose
        canned advice is "never invent packages". For the case that dominates
        this repo's telemetry that advice is not merely useless, it is FALSE:
        the package was not invented, it is declared in ``requirements.txt``
        and simply absent -- and the candidate did not import it at all, the
        subject did. Injecting it 15 to 50 times taught the model nothing,
        because there was nothing it could have done differently.
        """
        if self.kind == DEPENDENCY_VIOLATION:
            if self.declared:
                return (
                    f"The candidate added `import {self.module}`. That "
                    f"distribution is DECLARED in this project's requirements "
                    f"but is not installed in the environment tests run in, so "
                    f"the import fails. Solve the task using only modules that "
                    f"import successfully here; do not add an import the "
                    f"environment cannot satisfy."
                )
            return (
                f"The candidate added `import {self.module}`, which is neither "
                f"in this repository nor in its declared dependencies. Adding a "
                f"dependency is not part of this task. Use the repository's own "
                f"modules and the standard library."
            )
        if self.kind == ENVIRONMENT_STARVATION:
            return (
                f"`{self.module}` is missing from the ENVIRONMENT, not from the "
                f"candidate -- the module under test already imported it before "
                f"any change. Nothing written here can fix that, and the "
                f"candidate is not at fault. An operator must install the "
                f"dependency or retire the target."
            )
        return ""


_NO_IMPORT_FAULT = ImportFailureVerdict()


def declared_distribution_names(repo_root: Path) -> FrozenSet[str]:
    """Every distribution any manifest in this repository declares.

    The WIDE set on purpose, unlike the boot gate's. The question here is not
    "is the environment provisioned" but "is this module part of the project's
    dependency graph at all" -- and ``requirements.txt`` answers that even for
    the 95 entries this venv deliberately does not install.
    """
    names: List[str] = []
    try:
        for dirpath, dirnames, filenames in os.walk(Path(repo_root)):
            dirnames[:] = [d for d in dirnames if not _prunable(d)]
            for fn in filenames:
                if not (fn.startswith("requirements") and fn.endswith(".txt")):
                    continue
                for name, _pin, _raw in _declared_requirements(Path(dirpath) / fn):
                    names.append(name)
    except Exception:  # noqa: BLE001
        logger.debug("[EnvIntegrity] declared scan degraded", exc_info=True)
    return frozenset(names)


#: A unified diff announces itself; a full file never does.
_DIFF_MARKER_RE = re.compile(r"^@@ -\d", re.M)
#: Import statements read off RAW LINES, for the diff path only. The AST is the
#: right tool for a whole file and the wrong one for a hunk: a diff does not
#: parse, so an AST attempt there returns nothing and every violation in
#: diff mode would silently read as "the candidate added no imports".
_IMPORT_LINE_RE = re.compile(r"^\s*(?:from\s+([\w.]+)|import\s+([\w.,\s]+))")


def _import_names(source: str) -> FrozenSet[str]:
    """Top-level names *source* imports. ``frozenset()`` for unparseable text."""
    try:
        from backend.core.ouroboros.governance.ast_signature_anchor import (  # noqa: PLC0415
            _imported_module_names,
        )
        return frozenset(
            str(m).split(".")[0] for m in _imported_module_names(source or "") if m
        )
    except Exception:  # noqa: BLE001
        return frozenset()


def _added_import_names(candidate: str, baseline: str) -> FrozenSet[str]:
    """Top-level names THIS CANDIDATE introduces, in either candidate schema.

    Full content is diffed against the file it replaces. A unified diff is read
    from its added lines alone — the ``-`` side is what is going away, and
    counting it would blame a candidate for an import it is REMOVING.
    """
    text = str(candidate or "")
    if not text:
        return frozenset()
    if not _DIFF_MARKER_RE.search(text):
        return _import_names(text) - _import_names(baseline)
    out: List[str] = []
    for line in text.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        m = _IMPORT_LINE_RE.match(line[1:])
        if not m:
            continue
        mods = m.group(1) or m.group(2) or ""
        for chunk in mods.split(","):
            top = chunk.strip().split(" ")[0].split(".")[0]
            if top and top.isidentifier():
                out.append(top)
    # Subtract what the file already imported, so a moved or re-indented
    # import line is not read as a new dependency.
    return frozenset(out) - _import_names(baseline)


def classify_import_failure(
    error_text: str,
    *,
    repo_root: Path,
    candidate_source: str = "",
    baseline_source: str = "",
) -> ImportFailureVerdict:
    """Did the CANDIDATE introduce this missing import, or was it already there?

    ## Why the verdict does not turn on "is it a real package"

    The tempting rule is: declared-but-absent is the environment's problem,
    undeclared is an invention. It is the wrong axis. A candidate that adds
    ``import requests`` to a governance test has broken the build whether or
    not ``requests`` is a real and famous library — the test cannot run here.
    So the VERDICT turns only on provenance (did this candidate add the
    import?), and declaredness refines only the LESSON TEXT, where being wrong
    about an alias costs a slightly-off sentence instead of a wrong rollback.

    That also sidesteps a problem with no static solution: an import name is
    not a distribution name (``cv2`` ships as ``opencv-python``, ``sklearn`` as
    ``scikit-learn``). Any mapping built here would be a guess, and a guess must
    never be load-bearing for a rollback.

    NEVER raises. Returns an empty verdict when the evidence is not an import
    failure, which the caller must treat as "leave the existing classification
    alone" rather than as "nothing failed".
    """
    try:
        text = str(error_text or "")
        missing = [m for m in _MISSING_MODULE_RE.findall(text) if m]
        if not missing:
            return _NO_IMPORT_FAULT
        added = _added_import_names(candidate_source, baseline_source)
        declared = declared_distribution_names(Path(repo_root))

        # A net-new import wins over a pre-existing one: if the candidate added
        # ANY of the failing modules, that is the actionable fault, even when
        # the subject is also starved.
        for raw in missing:
            top = str(raw).split(".")[0]
            if top in added:
                return ImportFailureVerdict(
                    DEPENDENCY_VIOLATION, top, _canonical(top) in declared, True,
                )
        top = str(missing[0]).split(".")[0]
        return ImportFailureVerdict(
            ENVIRONMENT_STARVATION, top, _canonical(top) in declared, False,
        )
    except Exception:  # noqa: BLE001
        logger.debug("[EnvIntegrity] import-failure classify degraded", exc_info=True)
        return _NO_IMPORT_FAULT
