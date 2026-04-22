#!/usr/bin/env python3
"""Slice 1 live-fire: DirectionInferrer on real repo state.

Pulls one real signal (Conventional Commit type histogram over the last
50 commits via ``git log``) plus baseline zeros for the 11 signals whose
collectors are Slice 2 work. Runs ``DirectionInferrer.infer()`` on the
resulting bundle and asserts the output is structurally well-formed
(posture in enum, confidence in [0,1], evidence length 12, hash present,
schema_version "1.0"). Writes a PASS / FAIL log artifact.

Per Derek's §E2E mandate: no slice advances without a live-fire exit 0
+ committed log. Slice 1 is a pure primitive, so the live-fire proves
the primitive handles real repo data without crashing or producing
malformed output — the sanity floor before Slice 2 wires it to an
observer.

Exit codes:
  0 — all checks passed, PASS log written
  1 — any check failed, FAIL log written
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

REPO_ROOT = Path(
    subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
)
sys.path.insert(0, str(REPO_ROOT))

from backend.core.ouroboros.governance.direction_inferrer import (  # noqa: E402
    DEFAULT_WEIGHTS,
    DirectionInferrer,
    confidence_floor,
)
from backend.core.ouroboros.governance.posture import (  # noqa: E402
    Posture,
    SCHEMA_VERSION,
    baseline_bundle,
)


CONV_COMMIT_RE = re.compile(
    r"^(feat|fix|refactor|test|docs|chore|perf|style|build|ci|revert)"
    r"(\([^)]+\))?!?:",
    re.IGNORECASE,
)


def collect_commit_histogram(window: int = 50) -> dict:
    """Parse the last ``window`` commit subjects, return type ratios."""
    result = subprocess.run(
        ["git", "log", f"-{window}", "--pretty=format:%s"],
        capture_output=True, text=True, check=True,
        cwd=REPO_ROOT,
    )
    subjects = [s.strip() for s in result.stdout.splitlines() if s.strip()]
    total = len(subjects) or 1
    counts = {"feat": 0, "fix": 0, "refactor": 0, "test": 0, "docs": 0}
    for subj in subjects:
        m = CONV_COMMIT_RE.match(subj)
        if not m:
            continue
        ctype = m.group(1).lower()
        if ctype in counts:
            counts[ctype] += 1
    ratios = {k: v / total for k, v in counts.items()}
    ratios["_total_commits"] = total
    ratios["_raw_counts"] = counts
    return ratios


def main() -> int:
    print("=" * 72)
    print("DirectionInferrer Slice 1 — Live-Fire on Real Repo State")
    print("=" * 72)

    checks = []

    # --- Signal collection ---
    ratios = collect_commit_histogram(window=50)
    print(f"[signal] Parsed {ratios['_total_commits']} commits from git log -50")
    print(f"[signal] Conventional-commit counts: {ratios['_raw_counts']}")
    print(
        f"[signal] Ratios: feat={ratios['feat']:.2f} fix={ratios['fix']:.2f} "
        f"refactor={ratios['refactor']:.2f} "
        f"test+docs={ratios['test'] + ratios['docs']:.2f}"
    )

    bundle = replace(
        baseline_bundle(),
        feat_ratio=ratios["feat"],
        fix_ratio=ratios["fix"],
        refactor_ratio=ratios["refactor"],
        test_docs_ratio=ratios["test"] + ratios["docs"],
    )

    # --- Inference ---
    inferrer = DirectionInferrer()
    t0 = time.perf_counter()
    reading = inferrer.infer(bundle)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    print(f"[infer] Completed in {elapsed_ms:.2f}ms")
    print(f"[infer] Posture={reading.posture.value} confidence={reading.confidence:.4f}")
    print(f"[infer] Bundle hash={reading.signal_bundle_hash}")
    print(f"[infer] Schema version={reading.schema_version}")
    print(f"[infer] Confidence floor (env)={confidence_floor():.2f}")
    print("[infer] All-posture scores:")
    for posture, score in reading.all_scores:
        print(f"          {posture.value:13s} = {score:+.4f}")
    print("[infer] Top 3 evidence entries:")
    for c in reading.evidence[:3]:
        print(
            f"          signal={c.signal_name:32s} raw={c.raw_value:.3f} "
            f"norm={c.normalized:.3f} weight={c.weight:+.2f} "
            f"contrib={c.contribution_score:+.4f}"
        )

    # --- Structural checks ---
    checks.append(("posture in enum", reading.posture in set(Posture)))
    checks.append((
        "confidence in [0,1]",
        0.0 <= reading.confidence <= 1.0,
    ))
    checks.append((
        "evidence list has 12 entries",
        len(reading.evidence) == 12,
    ))
    checks.append((
        "all_scores has 4 entries (one per posture)",
        len(reading.all_scores) == 4
        and {p for p, _ in reading.all_scores} == set(Posture),
    ))
    checks.append((
        "signal_bundle_hash is 8-char hex",
        len(reading.signal_bundle_hash) == 8
        and all(c in "0123456789abcdef" for c in reading.signal_bundle_hash),
    ))
    checks.append((
        "schema_version is literal string '1.0'",
        reading.schema_version == "1.0" and SCHEMA_VERSION == "1.0",
    ))
    checks.append((
        "inference under 10ms (§5 Tier 0 budget)",
        elapsed_ms < 10.0,
    ))

    # --- Idempotence on real bundle ---
    reading2 = inferrer.infer(bundle)
    checks.append((
        "idempotence: same bundle → same hash",
        reading.signal_bundle_hash == reading2.signal_bundle_hash,
    ))
    checks.append((
        "idempotence: same bundle → same posture",
        reading.posture is reading2.posture,
    ))

    # --- Weight table sanity ---
    checks.append((
        "DEFAULT_WEIGHTS covers 12 signals",
        len(DEFAULT_WEIGHTS) == 12,
    ))

    # --- Authority invariant (grep) ---
    authority_forbidden = (
        "orchestrator", "policy", "iron_gate", "risk_tier",
        "change_engine", "candidate_generator",
    )
    for relpath in (
        "backend/core/ouroboros/governance/direction_inferrer.py",
        "backend/core/ouroboros/governance/posture.py",
    ):
        path = REPO_ROOT / relpath
        src = path.read_text(encoding="utf-8")
        bad = []
        for line in src.splitlines():
            if line.startswith(("from ", "import ")):
                for forbidden in authority_forbidden:
                    if f".{forbidden}" in line:
                        bad.append((forbidden, line))
        checks.append((
            f"authority-import-free: {relpath}",
            not bad,
        ))

    # --- Report ---
    print()
    print("-" * 72)
    print("Checks:")
    all_pass = True
    for name, ok in checks:
        mark = "PASS" if ok else "FAIL"
        if not ok:
            all_pass = False
        print(f"  [{mark}] {name}")
    print("-" * 72)

    log_path = REPO_ROOT / "scripts" / (
        "livefire_direction_inferrer_slice1_PASS.log" if all_pass
        else "livefire_direction_inferrer_slice1_FAIL.log"
    )
    # Clean up stale opposite-state log
    other = REPO_ROOT / "scripts" / (
        "livefire_direction_inferrer_slice1_FAIL.log" if all_pass
        else "livefire_direction_inferrer_slice1_PASS.log"
    )
    if other.exists():
        other.unlink()

    artifact = {
        "slice": 1,
        "feature": "DirectionInferrer",
        "timestamp": time.time(),
        "all_pass": all_pass,
        "elapsed_ms": elapsed_ms,
        "posture": reading.posture.value,
        "confidence": reading.confidence,
        "signal_bundle_hash": reading.signal_bundle_hash,
        "all_scores": [(p.value, s) for p, s in reading.all_scores],
        "commit_ratios": {
            k: ratios[k] for k in ("feat", "fix", "refactor", "test", "docs")
        },
        "checks": [{"name": n, "pass": ok} for n, ok in checks],
    }
    log_path.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    print(f"Artifact: {log_path}")

    if all_pass:
        print("\n  RESULT: PASS  —  Slice 1 live-fire clean on real repo state.")
        return 0
    print("\n  RESULT: FAIL  —  check the log artifact.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
