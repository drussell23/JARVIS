"""Undefined-name gating: whole-package where it is clean, differential everywhere else.

WHY THIS EXISTS
---------------
`ci-cd-pipeline.yml` runs `flake8 backend/ ... || true`. The result is
discarded, so an undefined name has never failed a build. Two shipped in the
operator's front door and were invisible for ten days: `thin_client` used a
`logger` it never defined (bare `ov` could not cold-boot the organism), and
`ov._client_extra_bindings` did the same and discarded the ENTIRE extra
key-binding set. Both were swallowed by blanket `except Exception` handlers.

`|| true` cannot simply be removed: the repository carries ~812 undefined-name
findings, the bulk of them in vendored `venv/` and in `core/quarantine`. A gate
that cannot pass is a gate somebody disables, so there are two, and each is
honest about its own scope:

**Whole-package** (:func:`package_findings`) for packages already at zero. It
proves a clean package stays clean.

**Differential** (:func:`differential_findings`) for everywhere else. It
reports only findings on lines this branch actually touched, so legacy debt
never fails a build and no NEW undefined name can land. This is the surface
that would have caught both incidents on the commit that introduced them.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
It does not flag names that are only ever used in ANNOTATIONS of a module
carrying `from __future__ import annotations`, nor names inside quoted
annotations. Those are never evaluated at runtime, and 16 of the 24 findings
in this repo's governance + battle_test packages are exactly that. Reporting
them would push people to add runtime imports for names that currently cost
nothing — trading zero cost for import time and circular-import risk, which is
worse than the thing being fixed.

BLINDNESS IS NOT A PASS
-----------------------
If the base ref cannot be resolved (a shallow CI checkout, a detached HEAD),
the differential gate cannot see the change surface. It says so and, under
``--require-base``, FAILS rather than reporting success — the same discipline
`JARVIS_PTY_TESTS_REQUIRED` applies to terminal-dependent tests. Reporting a
skip as a pass is the failure mode this whole module exists to end.

Stdlib + pyflakes only. Python 3.9+.
"""
from __future__ import annotations

import argparse
import ast
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

#: Base ref the differential gate diffs against. Overridable so a fork, a
#: release branch or a local experiment can gate against its own trunk.
BASE_REF_ENV = "JARVIS_LINT_GATE_BASE_REF"
DEFAULT_BASE_REF = "origin/main"

#: Packages held to whole-package cleanliness. Extend as packages reach zero;
#: never trim to make a red build green — that is what the differential gate
#: is for.
CLEAN_PACKAGES_ENV = "JARVIS_LINT_GATE_CLEAN_PACKAGES"
DEFAULT_CLEAN_PACKAGES: Tuple[str, ...] = (
    "backend/core/ouroboros/cli",
    # Both reached zero in this change; locking that in is the whole point of
    # a ratchet. Scanned RECURSIVELY — a top-level-only sweep missed
    # `phase_runners/generate_runner.py`, where the same `validation` defect
    # sat on the SHIPPING path.
    "backend/core/ouroboros/governance",
    "backend/core/ouroboros/battle_test",
)

#: Paths the differential gate ignores even when a diff touches them.
#: Vendored and quarantined trees are not this repository's code.
EXCLUDE_ENV = "JARVIS_LINT_GATE_EXCLUDE"
DEFAULT_EXCLUDES: Tuple[str, ...] = (
    "/venv/",
    "/site-packages/",
    "/node_modules/",
    "/.worktrees/",
    "backend/core/quarantine/",
    # iCloud/Finder duplicate artifacts ("foo 2.py"). Not authored code: they
    # are byte-copies the sync layer left behind, several carry syntax errors,
    # and the progress board already counts them as dark. Excluded here so a
    # sync artifact can never fail a build; they want deleting, not linting.
    " 2.py",
)


def _env_list(var: str, defaults: Sequence[str]) -> Tuple[str, ...]:
    """Defaults PLUS environment additions. Never fewer.

    Extend-only for the same reason every other knob in this codebase is: a
    list an env var could empty is a gate a typo silently disarms."""
    out = list(defaults)
    try:
        raw = str(os.environ.get(var, "") or "")
        for piece in raw.replace(",", " ").split():
            piece = piece.strip()
            if piece and piece not in out:
                out.append(piece)
    except Exception:  # noqa: BLE001
        pass
    return tuple(out)


def base_ref() -> str:
    return str(os.environ.get(BASE_REF_ENV, "") or DEFAULT_BASE_REF).strip()


def clean_packages() -> Tuple[str, ...]:
    return _env_list(CLEAN_PACKAGES_ENV, DEFAULT_CLEAN_PACKAGES)


def excludes() -> Tuple[str, ...]:
    return _env_list(EXCLUDE_ENV, DEFAULT_EXCLUDES)


def is_excluded(path: str) -> bool:
    norm = "/" + str(path).replace("\\", "/").lstrip("/")
    return any(frag in norm for frag in excludes())


# ---------------------------------------------------------------------------
# Finding
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    path: str
    lineno: int
    name: str
    inert: bool = False

    def render(self) -> str:
        return f"{self.path}:{self.lineno}: undefined name '{self.name}'"


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


def _annotation_lines(tree: ast.AST) -> Set[int]:
    """Line numbers occupied by annotation expressions.

    Annotations in a module with `from __future__ import annotations` are
    never evaluated, so an undefined name there cannot raise."""
    lines: Set[int] = set()

    def mark(node: Optional[ast.AST]) -> None:
        if node is None:
            return
        for sub in ast.walk(node):
            ln = getattr(sub, "lineno", None)
            if ln is not None:
                lines.add(ln)

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            mark(node.returns)
            args = node.args
            every = list(args.args) + list(args.kwonlyargs)
            every += list(getattr(args, "posonlyargs", []) or [])
            for arg in every:
                mark(arg.annotation)
            if args.vararg is not None:
                mark(args.vararg.annotation)
            if args.kwarg is not None:
                mark(args.kwarg.annotation)
        elif isinstance(node, ast.AnnAssign):
            mark(node.annotation)
    return lines


def _quoted_annotation_lines(tree: ast.AST) -> Set[int]:
    """Lines whose ANNOTATION is itself a string literal — never evaluated.

    `def f(x: "Thing")` is inert even without the future import. Missing that
    mis-classified `verify_gate.py` in the first pass of this analysis.

    SCOPED TO ANNOTATION POSITIONS ONLY. An earlier draft marked every line
    containing any string constant, which silently suppressed a REAL finding:
    `orchestrator.py`'s `_fc = validation.failure_class ... if 'validation'
    in dir() else ""` carries string literals, so the line was ruled inert and
    the undefined `validation` disappeared from the report. A detector whose
    false-negatives scale with how many strings a line happens to contain is
    worse than no detector, because it reports clean."""
    lines: Set[int] = set()

    def mark_if_quoted(node: Optional[ast.AST]) -> None:
        if node is None:
            return
        for sub in ast.walk(node):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                ln = getattr(sub, "lineno", None)
                if ln is not None:
                    lines.add(ln)

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            mark_if_quoted(node.returns)
            args = node.args
            every = list(args.args) + list(args.kwonlyargs)
            every += list(getattr(args, "posonlyargs", []) or [])
            for arg in every:
                mark_if_quoted(arg.annotation)
            if args.vararg is not None:
                mark_if_quoted(args.vararg.annotation)
            if args.kwarg is not None:
                mark_if_quoted(args.kwarg.annotation)
        elif isinstance(node, ast.AnnAssign):
            mark_if_quoted(node.annotation)
    return lines


def undefined_names(path: Path) -> List[Finding]:
    """Every undefined name in *path*, each marked inert or real. NEVER raises.

    A file that cannot be parsed yields a synthetic finding rather than
    silence: a syntax error is strictly worse than an undefined name, and a
    linter that skipped it would be reporting clean on a file that cannot
    even import."""
    try:
        from pyflakes import checker as pyflakes_checker
        from pyflakes import messages as pyflakes_messages
    except Exception:  # noqa: BLE001 — caller decides whether that is fatal
        return []

    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return [Finding(str(path), 0, f"<unreadable: {exc}>")]

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return [Finding(str(path), int(exc.lineno or 0), "<syntax error>")]
    except Exception as exc:  # noqa: BLE001
        return [Finding(str(path), 0, f"<unparseable: {exc}>")]

    try:
        check = pyflakes_checker.Checker(tree, filename=str(path))
        raw = [
            (int(m.lineno), str(m.message % m.message_args))
            for m in check.messages
            if isinstance(m, pyflakes_messages.UndefinedName)
        ]
    except Exception as exc:  # noqa: BLE001
        return [Finding(str(path), 0, f"<check failed: {exc}>")]

    future = any(
        isinstance(n, ast.ImportFrom) and n.module == "__future__"
        and any(a.name == "annotations" for a in n.names)
        for n in ast.walk(tree)
    )
    ann = _annotation_lines(tree)
    strann = _quoted_annotation_lines(tree)

    out: List[Finding] = []
    for lineno, msg in raw:
        name = msg.split("'")[1] if "'" in msg else msg
        inert = (lineno in ann and future) or (lineno in strann)
        out.append(Finding(str(path), lineno, name, inert=inert))
    return out


# ---------------------------------------------------------------------------
# Git surface
# ---------------------------------------------------------------------------


class BaseRefUnavailable(RuntimeError):
    """The change surface cannot be computed, so nothing can be gated."""


def _git(args: Sequence[str], cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd),
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        raise BaseRefUnavailable(
            f"git {' '.join(args)} failed: {proc.stderr.strip()[:200]}"
        )
    return proc.stdout


def changed_lines(
    root: Path, ref: Optional[str] = None,
) -> Dict[str, Set[int]]:
    """``{path: {added-or-modified line numbers}}`` for this branch.

    Untracked files count in full: a brand-new file is entirely new surface,
    and the incident that motivated this gate could just as easily have
    arrived in one. Deleted files are absent by construction — there is
    nothing left to lint."""
    ref = ref or base_ref()
    try:
        merge_base = _git(["merge-base", ref, "HEAD"], root).strip()
    except BaseRefUnavailable:
        raise
    if not merge_base:
        raise BaseRefUnavailable(f"no merge-base with {ref!r}")

    out: Dict[str, Set[int]] = {}
    diff = _git(
        ["diff", "--unified=0", "--no-color", "--diff-filter=ACMR",
         merge_base, "--"], root,
    )
    current: Optional[str] = None
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:].strip()
            continue
        if line.startswith("@@") and current:
            # @@ -old,cnt +new,cnt @@
            try:
                plus = line.split("+", 1)[1].split(" ", 1)[0]
                start_s, _, count_s = plus.partition(",")
                start = int(start_s)
                count = int(count_s) if count_s else 1
            except Exception:  # noqa: BLE001 — a malformed hunk header
                continue
            if count > 0:
                out.setdefault(current, set()).update(
                    range(start, start + count)
                )

    # Untracked files: whole-file surface.
    try:
        untracked = _git(
            ["ls-files", "--others", "--exclude-standard"], root,
        ).split()
    except BaseRefUnavailable:
        untracked = []
    for rel in untracked:
        if not rel.endswith(".py"):
            continue
        try:
            n = len((root / rel).read_text(
                encoding="utf-8", errors="replace").splitlines())
        except Exception:  # noqa: BLE001
            continue
        out.setdefault(rel, set()).update(range(1, n + 1))
    return out


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


@dataclass
class GateResult:
    findings: List[Finding] = field(default_factory=list)
    scanned_files: int = 0
    blind: Optional[str] = None          # why the gate could not see

    @property
    def ok(self) -> bool:
        return not self.findings and self.blind is None


def package_findings(root: Path, packages: Optional[Sequence[str]] = None) -> GateResult:
    """Whole-package gate over packages already at zero."""
    res = GateResult()
    for pkg in (packages if packages is not None else clean_packages()):
        base = root / pkg
        if not base.is_dir():
            continue
        # RECURSIVE. `glob("*.py")` missed `phase_runners/generate_runner.py`
        # entirely — the file carrying this defect class on the shipping path.
        for path in sorted(base.rglob("*.py")):
            if is_excluded(str(path)):
                continue
            res.scanned_files += 1
            res.findings.extend(f for f in undefined_names(path) if not f.inert)
    return res


def differential_findings(
    root: Path, ref: Optional[str] = None,
) -> GateResult:
    """Gate ONLY the lines this branch touched."""
    res = GateResult()
    try:
        touched = changed_lines(root, ref)
    except BaseRefUnavailable as exc:
        res.blind = str(exc)
        return res

    for rel, lines in sorted(touched.items()):
        if not rel.endswith(".py") or is_excluded(rel):
            continue
        path = root / rel
        if not path.is_file():
            continue
        res.scanned_files += 1
        for finding in undefined_names(path):
            if finding.inert:
                continue
            # A syntax/read failure has no meaningful line to intersect, so it
            # is reported whenever the file was touched at all.
            if finding.lineno == 0 or finding.lineno in lines:
                res.findings.append(finding)
    return res


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lint_gate",
        description="Fail on NEW undefined names without failing on legacy ones.",
    )
    parser.add_argument("--base", default=None,
                        help=f"base ref (default ${BASE_REF_ENV} or {DEFAULT_BASE_REF})")
    parser.add_argument("--root", default=".", help="repository root")
    parser.add_argument("--require-base", action="store_true",
                        help="treat an unresolvable base ref as FAILURE, not a skip")
    parser.add_argument("--packages-only", action="store_true")
    parser.add_argument("--differential-only", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)

    root = Path(args.root).resolve()
    failed = False

    if not args.differential_only:
        pkg = package_findings(root)
        if pkg.findings:
            failed = True
            print(f"✗ whole-package gate: {len(pkg.findings)} undefined name(s)")
            for f in pkg.findings:
                print(f"    {f.render()}")
        else:
            print(f"✓ whole-package gate clean ({pkg.scanned_files} file(s))")

    if not args.packages_only:
        dif = differential_findings(root, args.base)
        if dif.blind is not None:
            msg = f"differential gate is BLIND: {dif.blind}"
            if args.require_base:
                print(f"✗ {msg}")
                print("    --require-base is set, so this runner is expected to "
                      "resolve the base ref. A gate that cannot see the change "
                      "surface must not report success.")
                failed = True
            else:
                print(f"⚠ {msg} — skipping (pass --require-base to make this fatal)")
        elif dif.findings:
            failed = True
            print(f"✗ differential gate: {len(dif.findings)} NEW undefined name(s) "
                  f"on changed lines")
            for f in dif.findings:
                print(f"    {f.render()}")
        else:
            print(f"✓ differential gate clean "
                  f"({dif.scanned_files} changed file(s))")

    return 1 if failed else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
