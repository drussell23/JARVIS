#!/usr/bin/env python3
"""Audit tracked-but-ignored files in the working tree.

The AutoCommitterIgnoreGuard arc (2026-05-03) closed the
sovereignty breach STRUCTURALLY: AutoCommitter now refuses to
stage any path matching ``.gitignore``, regardless of tracked
status (via ``git check-ignore --no-index``). Future breaches
are mathematically impossible at the structural layer.

Legacy state cleanup is operator-paced: this script enumerates
the existing tracked-but-ignored paths so the operator can
review each and decide which to ``git rm --cached``. Some are
deliberate (e.g., ``.gitkeep`` markers, Xcode project files,
Pass B substrate); others are clear cruft (compiled artifacts,
log files).

Usage:
    JARVIS_AUTO_COMMITTER_GITIGNORE_GUARD_ENABLED=true \\
        python3 scripts/audit_tracked_but_ignored.py

The script never mutates state. Output groups paths by category
suggestion based on simple suffix/prefix heuristics.

Exit codes:
    0 = clean (no tracked-but-ignored paths)
    1 = breaches found (operator review required)
    2 = guard disabled or unavailable
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    # Ensure the repo root is on sys.path so the ``backend.*``
    # import works regardless of where the script is invoked.
    repo_root_for_import = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo_root_for_import))
    # Force-enable the guard for the audit run -- the script is
    # operator-invoked so the env-flag check shouldn't gate it.
    os.environ.setdefault(
        "JARVIS_AUTO_COMMITTER_GITIGNORE_GUARD_ENABLED", "true",
    )
    try:
        from backend.core.ouroboros.governance.gitignore_guard import (
            find_tracked_but_ignored,
            gitignore_guard_enabled,
        )
    except ImportError as exc:
        print(f"FATAL: gitignore_guard unavailable: {exc}",
              file=sys.stderr)
        return 2

    if not gitignore_guard_enabled():
        print(
            "FATAL: guard reports disabled despite forced env",
            file=sys.stderr,
        )
        return 2

    repo_root = Path(__file__).resolve().parents[1]
    print(f"Auditing tracked-but-ignored files in {repo_root}")
    print()

    breaches = find_tracked_but_ignored(repo_root)

    if not breaches:
        print("CLEAN: zero tracked-but-ignored paths.")
        return 0

    print(f"Found {len(breaches)} tracked-but-ignored path(s).")
    print()

    # Group by simple suffix/prefix heuristic for operator review.
    groups: dict = {
        "Compiled artifacts (.so / .pyc / .dylib)": [],
        "Log files (.log)": [],
        "Archived modules (_archived_*)": [],
        ".jarvis/ persistence (Order 2 / etc.)": [],
        "Xcode project files (.xcodeproj/)": [],
        ".gitkeep markers (intentional)": [],
        "Other": [],
    }
    for p in breaches:
        if p.endswith((".so", ".pyc", ".dylib", ".pyd")):
            groups["Compiled artifacts (.so / .pyc / .dylib)"].append(p)
        elif p.endswith(".log"):
            groups["Log files (.log)"].append(p)
        elif "_archived_" in p:
            groups["Archived modules (_archived_*)"].append(p)
        elif p.startswith(".jarvis/"):
            groups[".jarvis/ persistence (Order 2 / etc.)"].append(p)
        elif ".xcodeproj/" in p:
            groups["Xcode project files (.xcodeproj/)"].append(p)
        elif p.endswith(".gitkeep"):
            groups[".gitkeep markers (intentional)"].append(p)
        else:
            groups["Other"].append(p)

    for label, paths in groups.items():
        if not paths:
            continue
        print(f"=== {label} ({len(paths)}) ===")
        for p in paths:
            print(f"  {p}")
        print()

    print("Operator action: review each group + decide which to")
    print("``git rm --cached <path>``. Categories typically safe")
    print("to remove: compiled artifacts, log files, archived modules.")
    print("Categories needing operator judgment: .jarvis/ persistence,")
    print(".xcodeproj/ files, .gitkeep markers (often deliberate).")
    print()
    print("Structural prevention is already live: AutoCommitter will")
    print("refuse to stage any new breach via gitignore_guard's")
    print("--no-index check. Legacy cleanup is operator-paced.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
