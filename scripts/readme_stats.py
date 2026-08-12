#!/usr/bin/env python3
"""readme_stats — regenerate the README's "By the Numbers" table from git.

Why this exists
---------------

The README published its own reproduction command::

    git ls-files -z | xargs -0 wc -l

On a repo this size ``xargs`` splits the argument list into several
batches, so ``wc`` emits one ``total`` line PER BATCH — five, at the time
of writing. A reader who takes the last one (the natural reading) gets
166,096 against a real 4,903,339: **3.4% of the truth**. The instruction
invited verification and then failed it, which is worse than publishing
no instruction at all, because it turns an honest figure into an
apparent 26x inflation.

The deeper problem is that the numbers were transcribed by hand. A
point-in-time fact with no authority that regenerates it drifts silently
— which is exactly what happened: every figure in the table understated
reality by 12-16% within three weeks. So the fix is not to correct the
numbers; it is to stop storing numbers that nothing derives.

Counting, done correctly
------------------------

Lines are counted in-process by summing ``b"\\n"`` occurrences over every
git-tracked file, which is precisely ``wc -l`` semantics without the
batching hazard. Files are read in bounded chunks so a large blob costs
constant memory, and unreadable paths are skipped rather than guessed at.

The Trinity, honestly
---------------------

JARVIS is one of three repositories. The sibling paths are resolved from
the SAME environment variables the daemon already uses
(``JARVIS_PRIME_REPO_PATH`` / ``JARVIS_REACTOR_REPO_PATH``) — not a
second configuration surface — falling back to a sibling-directory
convention.

A repo that cannot be measured is reported as **unmeasured**, never
omitted and never estimated. An ecosystem total that quietly drops a
missing member would understate by an unknown amount while looking
authoritative; saying "2 of 3 measured" costs one word and keeps the
figure honest.

Usage
-----

    python3 scripts/readme_stats.py            # print the table
    python3 scripts/readme_stats.py --write    # rewrite README in place
    python3 scripts/readme_stats.py --check    # exit 1 if README is stale
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

#: Delimiters for the generated block. Everything between them is owned
#: by this script; everything outside is prose nobody regenerates.
BEGIN_MARKER = "<!-- readme-stats:begin -->"
END_MARKER = "<!-- readme-stats:end -->"

#: Env vars the daemon ALREADY defines for these repos. Reusing them
#: means a relocated checkout is configured once, not twice.
PRIME_PATH_ENV = "JARVIS_PRIME_REPO_PATH"
REACTOR_PATH_ENV = "JARVIS_REACTOR_REPO_PATH"
README_ENV = "JARVIS_README_PATH"

_CHUNK = 1 << 20

#: Passes allowed for the self-referential fixed point (see main()).
_MAX_FIXPOINT_PASSES = 6


@dataclass(frozen=True)
class RepoStats:
    """One repository's measured size. ``ok`` False means UNMEASURED —
    a state that must survive into the rendering rather than collapsing
    into a zero, which would read as "this repo is empty"."""

    name: str
    path: Optional[Path]
    lines: int = 0
    files: int = 0
    py_files: int = 0
    py_lines: int = 0
    commits: int = 0
    ok: bool = False
    note: str = ""


def _git(repo: Path, *args: str) -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", *args], cwd=str(repo),
            capture_output=True, text=True, timeout=180,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def _count_newlines(path: Path) -> int:
    """``wc -l`` semantics: the number of ``\\n`` bytes. Streamed, so a
    100 MB blob costs one chunk of memory rather than its own size."""
    total = 0
    try:
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(_CHUNK)
                if not chunk:
                    break
                total += chunk.count(b"\n")
    except OSError:
        return 0
    return total


def measure(name: str, path: Optional[Path]) -> RepoStats:
    """Measure one repo. NEVER raises; an unmeasurable repo says so."""
    if path is None or not (path / ".git").exists():
        return RepoStats(name=name, path=path, ok=False,
                         note="not present")
    listing = _git(path, "ls-files", "-z")
    if listing is None:
        return RepoStats(name=name, path=path, ok=False,
                         note="git ls-files failed")

    rel = [p for p in listing.split("\0") if p]
    lines = py_lines = py_files = 0
    for r in rel:
        n = _count_newlines(path / r)
        lines += n
        if r.endswith(".py"):
            py_files += 1
            py_lines += n

    commits_raw = _git(path, "rev-list", "--count", "HEAD")
    try:
        commits = int((commits_raw or "0").strip())
    except ValueError:
        commits = 0

    return RepoStats(
        name=name, path=path, lines=lines, files=len(rel),
        py_files=py_files, py_lines=py_lines, commits=commits, ok=True,
    )


def _sibling(primary: Path, env_var: str, default_dir: str) -> Optional[Path]:
    raw = os.environ.get(env_var, "").strip()
    if raw:
        return Path(raw).expanduser()
    candidate = primary.parent / default_dir
    return candidate if candidate.exists() else None


def collect(primary: Path) -> List[RepoStats]:
    return [
        measure("JARVIS (Body)", primary),
        measure("J-Prime (Mind)",
                _sibling(primary, PRIME_PATH_ENV, "jarvis-prime")),
        measure("Reactor Core (Soul)",
                _sibling(primary, REACTOR_PATH_ENV, "reactor-core")),
    ]


#: Sub-counts within JARVIS, each defined by a PATH PREFIX so the figure
#: is reproducible from the name alone. The previous table quoted "928K
#: engine · 682K test spine" with no stated boundary, which is a number
#: nobody could check and therefore nobody could correct.
_SUBSETS: Tuple[Tuple[str, str], ...] = (
    ("O+V engine", "backend/core/ouroboros/"),
    ("Test spine", "tests/"),
)


def measure_subsets(primary: Path) -> List[Tuple[str, str, int, int]]:
    """(label, prefix, lines, files) for each path-scoped subset."""
    listing = _git(primary, "ls-files", "-z")
    if listing is None:
        return []
    rel = [p for p in listing.split("\0") if p]
    out = []
    for label, prefix in _SUBSETS:
        lines = files = 0
        for r in rel:
            if r.startswith(prefix):
                files += 1
                lines += _count_newlines(primary / r)
        out.append((label, prefix, lines, files))
    return out


def _m(n: int) -> str:
    """Human scale, without inventing precision the count does not have."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)


def render(stats: List[RepoStats],
           subsets: Optional[List[Tuple[str, str, int, int]]] = None) -> str:
    measured = [s for s in stats if s.ok]
    total_lines = sum(s.lines for s in measured)
    total_commits = sum(s.commits for s in measured)
    total_py = sum(s.py_lines for s in measured)

    rows = [
        BEGIN_MARKER,
        "",
        "<!-- Generated by scripts/readme_stats.py — do not edit by hand.",
        "     Regenerate: python3 scripts/readme_stats.py --write",
        "     CI/pre-commit verifies with --check. -->",
        "",
        "| Repository | Lines | Files | Python | Commits |",
        "|---|---:|---:|---:|---:|",
    ]
    for s in stats:
        if not s.ok:
            rows.append(
                f"| **{s.name}** | _unmeasured_ | _unmeasured_ | "
                f"_unmeasured_ | _unmeasured_ |"
            )
            continue
        rows.append(
            f"| **{s.name}** | {s.lines:,} | {s.files:,} | "
            f"{s.py_lines:,} ({s.py_files:,} files) | {s.commits:,} |"
        )

    coverage = f"{len(measured)} of {len(stats)} repositories measured"
    rows += [
        f"| **Trinity total** | **{total_lines:,}** (~{_m(total_lines)}) "
        f"| | {total_py:,} | **{total_commits:,}** |",
    ]

    if subsets:
        rows += [
            "",
            "Within JARVIS, by path — each row is reproducible from its "
            "own prefix:",
            "",
            "| Subset | Path | Lines | Files |",
            "|---|---|---:|---:|",
        ]
        for label, prefix, lines, files in subsets:
            rows.append(
                f"| **{label}** | `{prefix}` | {lines:,} | {files:,} |"
            )

    rows += [
        "",
        f"_{coverage}._"
        + ("" if len(measured) == len(stats) else
           " Absent repositories are reported as `unmeasured` rather than"
           " counted as zero — an ecosystem total that silently drops a"
           " member understates by an unknown amount while looking"
           " authoritative."),
        "",
        "Reproduce exactly:",
        "",
        "```bash",
        "python3 scripts/readme_stats.py",
        "",
        "# Or by hand — note the awk. `git ls-files -z | xargs -0 wc -l`",
        "# alone splits into several batches and prints one 'total' PER",
        "# BATCH, so reading the last one reports a small fraction of the",
        "# real figure.",
        "git ls-files -z | xargs -0 wc -l \\",
        "  | awk '$2==\"total\"{s+=$1} END{print s}'",
        "```",
        "",
        END_MARKER,
    ]
    return "\n".join(rows)


def splice(readme: str, block: str) -> Tuple[str, bool]:
    """Replace the delimited block. Returns (text, found)."""
    start = readme.find(BEGIN_MARKER)
    end = readme.find(END_MARKER)
    if start == -1 or end == -1 or end < start:
        return readme, False
    return readme[:start] + block + readme[end + len(END_MARKER):], True


def _readme_path(primary: Path) -> Path:
    raw = os.environ.get(README_ENV, "").strip()
    return Path(raw).expanduser() if raw else primary / "README.md"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write", action="store_true",
                    help="rewrite the README block in place")
    ap.add_argument("--check", action="store_true",
                    help="exit 1 when the README block is stale")
    ap.add_argument("--repo", default="",
                    help="primary repo root (default: this checkout)")
    args = ap.parse_args(argv)

    primary = (Path(args.repo).expanduser() if args.repo
               else Path(__file__).resolve().parents[1])
    stats = collect(primary)
    block = render(stats, measure_subsets(primary))

    if not (args.write or args.check):
        print(block)
        return 0

    path = _readme_path(primary)
    try:
        current = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"readme_stats: cannot read {path}: {exc}", file=sys.stderr)
        return 2

    updated, found = splice(current, block)
    if not found:
        print(
            f"readme_stats: markers not found in {path}.\n"
            f"  Add {BEGIN_MARKER} / {END_MARKER} around the stats table.",
            file=sys.stderr,
        )
        return 2

    if args.check:
        if updated != current:
            print(
                "readme_stats: README stats are STALE.\n"
                "  Run: python3 scripts/readme_stats.py --write",
                file=sys.stderr,
            )
            return 1
        print("readme_stats: README stats are current.")
        return 0

    # FIXED POINT. README.md is itself git-tracked, so the table is an
    # input to the very count it publishes: writing it changes the repo's
    # line total, which changes the table. A single pass can therefore
    # never be self-consistent, and --check would report STALE
    # immediately after --write — a CI flake with a true cause.
    #
    # Iterating to a fixed point resolves it honestly: the published
    # figure is the one that is true OF THE FILE AS PUBLISHED. It
    # converges as soon as the rendered block stops changing width,
    # normally on the second pass. The cap exists so a pathological
    # oscillation (a digit boundary crossed on every write) terminates
    # loudly instead of spinning.
    passes = 0
    for _ in range(_MAX_FIXPOINT_PASSES):
        if updated == current:
            # Report what HAPPENED, not what the last comparison said.
            # Reaching the fixed point after writing is not "already
            # current"; announcing no change while having changed the
            # file is the same dishonesty this script exists to remove,
            # in miniature.
            if passes:
                print(
                    f"readme_stats: updated {path} "
                    f"(converged in {passes} pass"
                    f"{'' if passes == 1 else 'es'})"
                )
            else:
                print("readme_stats: already current")
            return 0
        path.write_text(updated, encoding="utf-8")
        passes += 1
        current = updated
        block = render(collect(primary), measure_subsets(primary))
        updated, _found = splice(current, block)

    print(
        f"readme_stats: did not converge in {_MAX_FIXPOINT_PASSES} passes — "
        f"the table's own size keeps changing the count it reports",
        file=sys.stderr,
    )
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
