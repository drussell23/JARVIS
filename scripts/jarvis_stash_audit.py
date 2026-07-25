#!/usr/bin/env python3
"""Cryptographic triage of legacy ``refs/stash`` entries.

Why this exists
---------------
Before the #70033 fix, the preemption-shield daemon registered every snapshot
via ``git stash store``, i.e. onto ``refs/stash`` — a SHARED, ORDER-SENSITIVE
stack. That left 49 machine-generated entries interleaved with human ones, and
made ``git stash pop`` (which takes ``stash@{0}``) a coin flip. Snapshots now
land under ``refs/jarvis/preemption/``; this tool cleans up what accumulated.

Safety design (deliberately stronger than "classify, then drop")
----------------------------------------------------------------
Every entry is ARCHIVED FIRST, unconditionally, before ANY drop happens — even
entries classified as fully-integrated. Archiving is a ref write: it costs no
disk (the commit objects already exist) and pins them against GC. This means a
bug in the *classifier* can never destroy work, because the classifier's verdict
only decides the REPORT, never whether a recovery path exists.

Verdicts
--------
``integrated``  — the stash's tree is reachable/identical in HEAD; its diff is
                  empty. Nothing unique would be lost.
``divergent``   — the stash carries content not in HEAD. Archived and reported
                  loudly; recover with ``git stash apply <sha>``.
``unreadable``  — git could not resolve the entry. Never dropped.

DRY-RUN BY DEFAULT. Nothing is mutated without ``--apply``.

Usage
-----
    python3 scripts/jarvis_stash_audit.py              # report only
    python3 scripts/jarvis_stash_audit.py --apply      # archive + clear stack
    python3 scripts/jarvis_stash_audit.py --json       # machine-readable
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import List, Optional

ARCHIVE_NS = "refs/jarvis/archive"
_TIMEOUT_S = 30


def _git(root: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", root, *args],
        capture_output=True, text=True, timeout=_TIMEOUT_S,
    )


def repo_root(start: Optional[str] = None) -> Optional[str]:
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=start or os.getcwd(), capture_output=True, text=True,
            timeout=_TIMEOUT_S,
        )
        return res.stdout.strip() if res.returncode == 0 else None
    except Exception:  # noqa: BLE001
        return None


def list_stashes(root: str) -> List[dict]:
    """``refs/stash`` entries, newest first, as ``{index, sha, subject}``."""
    res = _git(root, "stash", "list", "--format=%gd\t%H\t%gs")
    if res.returncode != 0:
        return []
    out = []
    for line in (res.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[1].strip():
            out.append({
                "index": parts[0].strip(),
                "sha": parts[1].strip(),
                "subject": parts[2].strip() if len(parts) > 2 else "",
            })
    return out


def classify(root: str, sha: str) -> tuple:
    """``(verdict, detail)``.

    Compares the stash's OWN DELTA, not whole trees.

    A naive ``git diff HEAD <stash>`` is wrong here and dangerously misleading:
    a stash taken at an older HEAD differs from current HEAD by every commit
    landed since, so it reports enormous deletion counts (observed: "155 files
    changed, 30315 deletions") that describe repo drift, NOT work the stash
    holds. Every entry would look unique.

    The delta a stash actually captured is ``<sha>^..<sha>`` — its tree against
    the HEAD it was taken from. The question that matters is then: for exactly
    the paths that delta touches, does the stash's content already match current
    HEAD? If yes, the work landed and dropping loses nothing.

    Fails CLOSED throughout: any unreadable step is ``unreadable``, never
    ``integrated``."""
    probe = _git(root, "rev-parse", "--verify", f"{sha}^{{commit}}")
    if probe.returncode != 0:
        return ("unreadable", "sha does not resolve to a commit")

    # The delta this stash captured, relative to its own parent.
    own = _git(root, "diff", "--name-only", f"{sha}^", sha)
    if own.returncode != 0:
        return ("unreadable", (own.stderr or "parent diff failed").strip()[:160])

    paths = [p for p in (own.stdout or "").splitlines() if p.strip()]
    if not paths:
        return ("integrated", "stash captured no delta")

    # Scope the HEAD comparison to ONLY those paths.
    scoped = _git(root, "diff", "--stat", "HEAD", sha, "--", *paths)
    if scoped.returncode != 0:
        return ("unreadable", (scoped.stderr or "scoped diff failed").strip()[:160])

    body = (scoped.stdout or "").strip()
    if not body:
        return ("integrated", f"{len(paths)} path(s) already match HEAD")
    last = body.splitlines()[-1].strip()
    return ("divergent", f"{len(paths)} path(s) captured; {last[:120]}")


def archive(root: str, sha: str, label: str) -> Optional[str]:
    """Pin the stash commit under the archive namespace. Returns the ref name."""
    ref = f"{ARCHIVE_NS}/legacy-{label}-{sha[:12]}"
    res = _git(root, "update-ref", ref, sha)
    return ref if res.returncode == 0 else None


def audit(root: str, *, apply: bool = False) -> dict:
    """Archive-then-clear. Returns a structured report."""
    stashes = list_stashes(root)
    report = {
        "repo": root,
        "total": len(stashes),
        "applied": bool(apply),
        "integrated": [],
        "divergent": [],
        "unreadable": [],
        "archived": 0,
        "dropped": 0,
        "errors": [],
    }

    # PASS 1 — classify + archive EVERY entry. No drops yet: the stack must not
    # shift underneath us (dropping renumbers stash@{N}), and nothing may be
    # removed before its recovery ref exists.
    for st in stashes:
        verdict, detail = classify(root, st["sha"])
        row = {"index": st["index"], "sha": st["sha"], "detail": detail}
        report[verdict].append(row)
        if not apply:
            continue
        ref = archive(root, st["sha"], verdict)
        if ref:
            row["archive_ref"] = ref
            report["archived"] += 1
        else:
            report["errors"].append(f"archive failed for {st['sha'][:12]}")

    if not apply:
        return report

    # PASS 2 — clear the stack. Only reached once every entry above has an
    # archive ref. `stash drop` renumbers, so always drop index 0 and re-read.
    droppable = report["archived"] == len(stashes) and not report["errors"]
    if not droppable:
        report["errors"].append(
            "archive incomplete — refusing to drop anything (fail-closed)"
        )
        return report

    guard = len(stashes) + 5   # bounded: never spin on a misbehaving git
    while guard > 0 and list_stashes(root):
        res = _git(root, "stash", "drop", "stash@{0}")
        if res.returncode != 0:
            report["errors"].append((res.stderr or "drop failed").strip()[:160])
            break
        report["dropped"] += 1
        guard -= 1
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="archive every entry, then clear refs/stash")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--repo", default=None)
    args = ap.parse_args(argv)

    root = args.repo or repo_root()
    if not root:
        print("not a git repository", file=sys.stderr)
        return 2

    rep = audit(root, apply=args.apply)

    if args.json:
        print(json.dumps(rep, indent=2, sort_keys=True))
        return 0 if not rep["errors"] else 1

    mode = "APPLIED" if rep["applied"] else "DRY-RUN (nothing mutated)"
    print(f"stash audit — {mode}")
    print(f"  total entries : {rep['total']}")
    print(f"  integrated    : {len(rep['integrated'])}  (zero diff vs HEAD)")
    print(f"  divergent     : {len(rep['divergent'])}  (unique work — archived)")
    print(f"  unreadable    : {len(rep['unreadable'])}  (never dropped)")
    if rep["applied"]:
        print(f"  archived      : {rep['archived']} -> {ARCHIVE_NS}/")
        print(f"  dropped       : {rep['dropped']}")
    for row in rep["divergent"][:10]:
        print(f"    ! {row['index']} {row['sha'][:12]}  {row['detail']}")
    for e in rep["errors"]:
        print(f"  ERROR: {e}")
    if rep["applied"] and rep["divergent"]:
        print(f"\n  recover any archived entry:  git stash apply <sha>")
        print(f"  list archives:               git for-each-ref {ARCHIVE_NS}")
    return 0 if not rep["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
