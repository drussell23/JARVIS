#!/usr/bin/env python3
"""Zero-cost standalone diagnostic for prepare_problem `test_patch_failed`.

COMPOSES the canonical Phase B.1 primitives — no reimplemented clone /
checkout / apply logic, no API spend, reuses the existing repo cache:

    load_problem (dataset_loader)            -> ProblemSpec
    _ensure_repo_cached (per_problem_harness)-> cached clone (idempotent)
    _create_problem_worktree                 -> worktree @ base_commit
    _run_git                                 -> the SAME git runner the
                                                production apply uses

The production `_apply_test_patch` runs `git apply --index -` and
truncates stderr to 300 chars in the log — that truncation is exactly
why the real failure was invisible.  Here we run a battery of
`git apply --check` variants against the canonical worktree and dump
FULL untruncated stderr to classify the failure:
  strip-level (-p0/-p1) · 3-way · recount · which hunks reject ·
  upstream-convention mismatch.

Usage: python3 scripts/diagnose_prepare_problem.py [instance_id]
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Canonical surface — discriminator corpus is the local JSONL we staged.
os.environ.setdefault("JARVIS_SWE_BENCH_PRO_ENABLED", "true")
os.environ.setdefault(
    "JARVIS_SWE_BENCH_PRO_LOCAL_DATASET_PATH",
    "./.jarvis/swe_bench_pro/discriminator.jsonl",
)

from backend.core.ouroboros.governance.swe_bench_pro.dataset_loader import (  # noqa: E501
    load_problem,
)
from backend.core.ouroboros.governance.swe_bench_pro import (
    per_problem_harness as pph,
)


def _hr(t: str) -> None:
    print(f"\n{'=' * 12} {t} {'=' * 12}")


async def _git(args, cwd, patch=None):
    """Compose the canonical _run_git → (rc, stdout, stderr) FULL."""
    return await pph._run_git(
        args,
        cwd=cwd,
        stdin_input=(patch.encode("utf-8") if patch is not None else None),
    )


async def main(instance_id: str) -> int:
    _hr(f"LOAD {instance_id}")
    spec, outcome = load_problem(instance_id)
    if spec is None:
        print(f"ABORT: load_problem outcome={outcome.value}")
        return 2
    print(
        f"repo={spec.repo} repo_url={spec.repo_url} "
        f"base_commit={spec.base_commit[:12]} "
        f"|test_patch|={len(spec.test_patch)}"
    )

    _hr("TEST_PATCH HEAD (first 40 lines — headers/paths/strip-level)")
    for ln in spec.test_patch.splitlines()[:40]:
        print(f"  | {ln}")

    _hr("CANONICAL CLONE (cached, idempotent — no re-download)")
    cached = await pph._ensure_repo_cached(spec.repo_url)
    if cached is None:
        print("ABORT: _ensure_repo_cached returned None")
        return 3
    print(f"cached_repo={cached}")

    _hr("CANONICAL WORKTREE @ base_commit")
    wt = await pph._create_problem_worktree(
        cached, spec.base_commit, spec.instance_id
    )
    if wt is None:
        print("ABORT: _create_problem_worktree returned None")
        return 4
    wt_path, branch = wt
    print(f"worktree={wt_path} branch={branch}")

    try:
        # The canonical production command, with --check + FULL stderr.
        battery = [
            ("PROD: apply --index --check", ["apply", "--index", "--check", "-"]),
            ("apply --check (no --index)", ["apply", "--check", "-"]),
            ("apply --check -p0", ["apply", "--check", "-p0", "-"]),
            ("apply --check -p1", ["apply", "--check", "-p1", "-"]),
            ("apply --check --3way", ["apply", "--check", "--3way", "-"]),
            ("apply --check --recount", ["apply", "--check", "--recount", "-"]),
            ("apply --stat (parse-only)", ["apply", "--stat", "-"]),
            ("apply --check --reject --verbose",
             ["apply", "--check", "--reject", "--verbose", "-"]),
        ]
        for label, args in battery:
            rc, out, err = await _git(args, wt_path, patch=spec.test_patch)
            _hr(f"{label}  →  rc={rc}")
            if out.strip():
                print("  STDOUT:")
                for ln in out.strip().splitlines()[:20]:
                    print(f"    {ln}")
            if err.strip():
                print("  STDERR (FULL, untruncated):")
                for ln in err.strip().splitlines()[:30]:
                    print(f"    {ln}")
            if not out.strip() and not err.strip():
                print("  (no output — clean)")

        # What paths does the patch target, and do they exist at base?
        _hr("TARGET PATH EXISTENCE @ base_commit")
        for p in pph._extract_target_paths_from_patch(spec.test_patch):
            exists = (wt_path / p).exists()
            print(f"  {'OK ' if exists else 'MISSING'}  {p}")
    finally:
        _hr("CLEANUP (canonical worktree teardown)")
        await _git(
            ["worktree", "remove", "--force", str(wt_path)], cached
        )
        await _git(["branch", "-D", branch], cached)
        print("cleaned")

    return 0


if __name__ == "__main__":
    iid = sys.argv[1] if len(sys.argv) > 1 else "psf__requests-3362"
    raise SystemExit(asyncio.run(main(iid)))
