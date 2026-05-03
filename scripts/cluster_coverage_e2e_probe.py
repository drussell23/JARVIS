#!/usr/bin/env python3
"""End-to-end deterministic probe for ClusterIntelligence-CrossSession
cluster_coverage envelope -> DomainMap cascade.

Exercises the full structural path WITHOUT a soak by:
  1. Building the SemanticIndex (will engage adaptive embedder fallback
     in environments where fastembed cannot load).
  2. Constructing a real ProactiveExplorationSensor wired to a stub
     UnifiedIntakeRouter that captures every envelope ingested.
  3. Calling the sensor's emit path directly so cluster_coverage
     envelopes are produced for the live clusters.
  4. Synthesizing a ``OperationContext`` carrying the envelope's
     evidence as ``intake_evidence_json`` and invoking
     ``observe_cluster_coverage_completion`` -- the post-verify cascade
     hook the orchestrator wires in production.
  5. Verifying that DomainMap entries are persisted to
     ``.jarvis/domain_map/<centroid_hash8>.json``.

This is the surgical proof that the cluster_coverage -> cascade ->
DomainMap chain is structurally healthy, complementing the soak
verdict (which proves the embedder fallback path engages and the
substrate populates clusters under real harness conditions).

Exit codes:
    0 = chain end-to-end functional
    1 = chain broken (specific failure printed)
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import List

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class _CapturingRouter:
    """Stub router that mirrors UnifiedIntakeRouter.ingest()'s shape
    enough for the sensor's emit path to succeed."""

    def __init__(self) -> None:
        self.envelopes: List[object] = []

    async def ingest(self, envelope: object) -> None:
        self.envelopes.append(envelope)


async def _amain() -> int:
    from backend.core.ouroboros.governance.semantic_index import (
        get_default_index, reset_default_index,
    )
    from backend.core.ouroboros.governance.codebase_character import (
        codebase_character_enabled,
    )
    from backend.core.ouroboros.governance.intake.sensors.proactive_exploration_sensor import (  # noqa: E501
        ProactiveExplorationSensor,
    )
    from backend.core.ouroboros.governance.cluster_exploration_cascade_observer import (  # noqa: E501
        observe_cluster_coverage_completion,
        cascade_observer_enabled,
    )
    from backend.core.ouroboros.governance.domain_map_memory import (
        domain_map_enabled,
    )

    print("Step 0 -- environment + flag check")
    print(f"  codebase_character_enabled : {codebase_character_enabled()}")
    print(f"  cascade_observer_enabled   : {cascade_observer_enabled()}")
    print(f"  domain_map_enabled         : {domain_map_enabled()}")

    print("Step 1 -- build SemanticIndex (force=True)")
    reset_default_index()
    idx = get_default_index(REPO_ROOT)
    print(f"  embedder type     : {type(idx._embedder).__name__}")
    t0 = time.time()
    built = idx.build(force=True)
    duration = time.time() - t0
    stats = idx.stats()
    print(f"  built             : {built} in {duration:.2f}s")
    print(f"  corpus_n          : {stats.corpus_n}")
    print(f"  cluster_count     : {stats.cluster_count}")
    print(f"  cluster_mode      : {stats.cluster_mode}")
    print(f"  using_fallback    : {getattr(idx._embedder, 'using_fallback', 'N/A')}")
    if stats.cluster_count == 0:
        print("FAIL: no clusters built -- substrate inert")
        return 1

    print("Step 2 -- emit cluster_coverage envelopes")
    router = _CapturingRouter()
    sensor = ProactiveExplorationSensor(
        router=router,  # type: ignore[arg-type]
        repo="JARVIS-AI-Agent",
        project_root=REPO_ROOT,
    )
    emitted = await sensor._emit_cluster_coverage_signals()
    print(f"  emitted_hashes    : {emitted}")
    print(f"  envelopes_captured: {len(router.envelopes)}")
    if not router.envelopes:
        print("FAIL: cluster_coverage emission produced zero envelopes")
        return 1

    print("Step 3 -- run cascade observer for each envelope")
    initial_files = sorted(
        (REPO_ROOT / ".jarvis" / "domain_map").glob("*.json"),
    ) if (REPO_ROOT / ".jarvis" / "domain_map").is_dir() else []
    initial_count = len(initial_files)
    print(f"  initial_domain_map_entries: {initial_count}")
    op_id_base = f"e2e-probe-{int(time.time())}"
    persisted = 0
    for i, env in enumerate(router.envelopes):
        evidence = getattr(env, "evidence", {}) or {}
        evidence_json = json.dumps(dict(evidence))
        # Real Op carries discovered files in target_files. Synthesize
        # a couple to mirror what a real Venom round would surface.
        target_files = getattr(env, "target_files", ()) or ()
        await observe_cluster_coverage_completion(
            op_id=f"{op_id_base}-{i}",
            intake_evidence_json=evidence_json,
            touched_files=tuple(target_files),
            verify_passed=True,
            project_root=REPO_ROOT,
        )
        persisted += 1
    print(f"  cascade_invocations: {persisted}")

    print("Step 4 -- verify DomainMap persistence")
    after_files = sorted(
        (REPO_ROOT / ".jarvis" / "domain_map").glob("*.json"),
    ) if (REPO_ROOT / ".jarvis" / "domain_map").is_dir() else []
    after_count = len(after_files)
    new_entries = [p for p in after_files if p not in initial_files]
    print(f"  domain_map_entries: {initial_count} -> {after_count}")
    print(f"  new_entries       : {[p.name for p in new_entries]}")
    if not new_entries:
        print("FAIL: cascade invocations did not produce DomainMap entries")
        return 1
    sample = json.loads(new_entries[0].read_text(encoding="utf-8"))
    print("  sample_entry      :")
    for k in (
        "centroid_hash8", "theme_label", "kind", "cluster_id",
        "exploration_count", "discovered_files", "confidence",
    ):
        v = sample.get(k)
        if isinstance(v, list):
            v = f"[{len(v)} files]"
        print(f"    {k}: {v}")

    print()
    print("VERDICT: cluster_coverage -> cascade -> DomainMap chain "
          "EMPIRICALLY VERIFIED end-to-end.")
    return 0


def main() -> int:
    try:
        return asyncio.run(_amain())
    except Exception as exc:
        print(f"FATAL: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
