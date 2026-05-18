#!/usr/bin/env python3
"""Stage a SWE-Bench discriminator corpus by COMPOSING the canonical loader.

This is deliberately NOT a parallel fetcher. It sets the canonical HF env
knobs and delegates every byte of acquisition + schema normalization to the
existing ``dataset_loader.load_problem`` path:

    load_problem(id)
      -> _load_from_huggingface(id)
        -> _iter_hf_records()         (single datasets.load_dataset seam)
          -> ProblemSpec.from_dict()  (single canonical normalizer:
                                       patch->gold_patch alias, repo_url
                                       derivation, metadata fold)
    ProblemSpec.to_dict()             (single canonical serializer,
                                       schema swe_bench_pro_problem.v1)

Discipline:
  * No hardcoding — instance ids / output path / dataset / split are argv.
  * No silent substitution — any id not resolvable aborts with a non-zero
    exit and writes NOTHING (the named id failing is a signal, not noise).
  * Atomic — composes into a tmp file, fsync, os.replace; a partial or
    garbage corpus never lands at the target path.
  * Integrity gate — every staged spec must have non-empty
    problem_statement / test_patch / gold_patch or the run aborts (a
    malformed corpus would waste the downstream live-fire budget).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Stage SWE-Bench discriminator corpus")
    p.add_argument(
        "--instance-ids",
        required=True,
        help="Comma-separated instance ids (e.g. good_id,hard_id)",
    )
    p.add_argument(
        "--out",
        required=True,
        help="Output JSONL path (atomic write)",
    )
    p.add_argument(
        "--hf-dataset",
        required=True,
        help="HuggingFace dataset name (e.g. princeton-nlp/SWE-bench_Lite)",
    )
    p.add_argument("--hf-split", default="test", help="HF split (default: test)")
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    ns = _parse_args(argv)
    ids = [s.strip() for s in ns.instance_ids.split(",") if s.strip()]
    if len(ids) < 2:
        print(f"ABORT: need >=2 instance ids, got {ids!r}", file=sys.stderr)
        return 2

    # Canonical env knobs — the loader reads these at call time.
    os.environ["JARVIS_SWE_BENCH_PRO_ENABLED"] = "true"
    os.environ["JARVIS_SWE_BENCH_PRO_HF_DATASET"] = ns.hf_dataset
    os.environ["JARVIS_SWE_BENCH_PRO_HF_SPLIT"] = ns.hf_split

    # Import AFTER env is set (loader reads env lazily, but be explicit).
    from backend.core.ouroboros.governance.swe_bench_pro.dataset_loader import (  # noqa: E501
        load_problem,
    )

    staged: list[dict] = []
    for iid in ids:
        spec, outcome = load_problem(iid)
        if spec is None:
            print(
                f"ABORT: instance {iid!r} not resolvable "
                f"(outcome={outcome.value}) — NO silent substitution. "
                f"Verify the id exists in {ns.hf_dataset}:{ns.hf_split}.",
                file=sys.stderr,
            )
            return 3
        d = spec.to_dict()
        # Integrity gate — a corpus with empty bodies wastes live-fire $.
        missing = [
            f
            for f in ("problem_statement", "test_patch", "gold_patch")
            if not str(d.get(f, "")).strip()
        ]
        if missing:
            print(
                f"ABORT: instance {iid!r} has empty {missing} — "
                f"unusable discriminator corpus.",
                file=sys.stderr,
            )
            return 4
        staged.append(d)
        print(
            f"  staged {iid}  outcome={outcome.value}  repo={d['repo']}  "
            f"base={d['base_commit'][:12]}  "
            f"|stmt|={len(d['problem_statement'])}  "
            f"|test_patch|={len(d['test_patch'])}  "
            f"|gold_patch|={len(d['gold_patch'])}  "
            f"difficulty={d['difficulty']}"
        )

    out = Path(ns.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    # Atomic: write tmp in the same dir, fsync, os.replace.
    fd, tmp = tempfile.mkstemp(prefix=".discriminator.", dir=str(out.parent))
    try:
        with os.fdopen(fd, "w") as f:
            for d in staged:
                f.write(json.dumps(d) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, out)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    print(f"OK: staged {len(staged)} instance(s) -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
