#!/usr/bin/env python3
"""Empirical-closure verdict for the Multi-Repo Sharding arc (Tier 2 #4).

Three primary contracts, fully in-process (no soak required):

  Contract 1 -- Repo signature stability + uniqueness:
                same path -> same signature, different paths ->
                different signatures, signature length pinned.
  Contract 2 -- SemanticIndex per-repo isolation:
                two distinct project_roots in the same process
                produce two distinct SemanticIndex instances; same
                root twice produces the same instance (single-repo
                identity preserved).
  Contract 3 -- DomainMapStore per-repo isolation:
                same shape as C2 for the cross-session memory
                substrate. Plus on-disk layout proof: each store's
                directory is under its own ``<project_root>/.jarvis/
                domain_map/`` so file-system entries also stay
                separated.

Optional Contract 4 -- AST pins hold against current source
(repo_signature substrate + domain_map_memory_authority pin
extended to allow the new shard-key import).

Exit codes:
    0 = all three primary contracts PASSED
    1 = at least one primary contract FAILED

Usage:
    python3 scripts/multi_repo_sharding_closure_verdict.py
"""
from __future__ import annotations

import ast
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@dataclass(frozen=True)
class ContractVerdict:
    name: str
    passed: bool
    evidence: str
    details: Dict[str, object] = field(default_factory=dict)


def _eval_signature_substrate() -> ContractVerdict:
    from backend.core.ouroboros.governance.multi_repo.repo_signature import (  # noqa: E501
        compute_repo_signature,
    )
    sig_a = compute_repo_signature(Path("/tmp"))
    sig_a_repeat = compute_repo_signature(Path("/tmp"))
    sig_b = compute_repo_signature(Path("/usr"))
    same_repeat = sig_a == sig_a_repeat
    distinct_paths = sig_a != sig_b
    length_correct = len(sig_a) == 8 and len(sig_b) == 8
    passed = same_repeat and distinct_paths and length_correct
    return ContractVerdict(
        name="C1 Repo signature deterministic + unique + length=8",
        passed=passed,
        evidence=(
            f"sig(/tmp)={sig_a} sig(/tmp)_repeat={sig_a_repeat} "
            f"sig(/usr)={sig_b} length={len(sig_a)}"
        ),
    )


def _eval_semantic_index_isolation() -> ContractVerdict:
    from backend.core.ouroboros.governance.semantic_index import (
        get_default_index, reset_default_index,
    )
    reset_default_index()
    with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
        pa, pb = Path(a), Path(b)
        ia = get_default_index(pa)
        ib = get_default_index(pb)
        # Identity within shard
        ia2 = get_default_index(pa)
        ib2 = get_default_index(pb)
        # Per-shard reset
        reset_default_index(pa)
        ia3 = get_default_index(pa)
        ib3 = get_default_index(pb)
        isolation = ia is not ib
        identity = ia is ia2 and ib is ib2
        per_shard_reset = ia3 is not ia and ib3 is ib
    reset_default_index()
    passed = isolation and identity and per_shard_reset
    return ContractVerdict(
        name="C2 SemanticIndex per-repo isolation + identity + reset",
        passed=passed,
        evidence=(
            f"distinct_roots->distinct_instances={isolation} "
            f"same_root->same_instance={identity} "
            f"per_shard_reset_isolated={per_shard_reset}"
        ),
    )


def _eval_domain_map_store_isolation() -> ContractVerdict:
    from backend.core.ouroboros.governance.domain_map_memory import (
        get_default_store, reset_default_store,
    )
    reset_default_store()
    with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
        pa, pb = Path(a), Path(b)
        sa = get_default_store(pa)
        sb = get_default_store(pb)
        # Identity
        sa2 = get_default_store(pa)
        sb2 = get_default_store(pb)
        # On-disk layout proof: each store points at its own dir
        sa_dir = getattr(sa, "_dir", None)
        sb_dir = getattr(sb, "_dir", None)
        # No-arg post-construction returns None (deferred contract)
        none_after = get_default_store() is None
        # Per-shard reset
        reset_default_store(pa)
        sa3 = get_default_store(pa)
        sb3 = get_default_store(pb)
        isolation = sa is not sb and sa is not None and sb is not None
        identity = sa is sa2 and sb is sb2
        on_disk_separated = (
            sa_dir is not None and sb_dir is not None
            and sa_dir != sb_dir
        )
        per_shard_reset = sa3 is not sa and sb3 is sb
    reset_default_store()
    passed = (
        isolation and identity and on_disk_separated
        and none_after and per_shard_reset
    )
    return ContractVerdict(
        name="C3 DomainMapStore per-repo isolation + on-disk separation",
        passed=passed,
        evidence=(
            f"distinct_roots->distinct_instances={isolation} "
            f"same_root->same_instance={identity} "
            f"on_disk_dirs_separated={on_disk_separated} "
            f"none_arg_after_construction_returns_None={none_after} "
            f"per_shard_reset_isolated={per_shard_reset}"
        ),
    )


def _eval_ast_pins() -> ContractVerdict:
    from backend.core.ouroboros.governance.multi_repo import (
        repo_signature as rs,
    )
    from backend.core.ouroboros.governance import domain_map_memory as dm
    failures: List[str] = []
    inv_status: List[str] = []
    for invariant_provider in (rs, dm):
        invariants = invariant_provider.register_shipped_invariants()
        for inv in invariants:
            target_path = REPO_ROOT / inv.target_file
            source = target_path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            violations = inv.validate(tree, source)
            if violations:
                failures.append(
                    f"{inv.invariant_name}: {violations[:3]}"
                )
            else:
                inv_status.append(f"{inv.invariant_name}=PASS")
    return ContractVerdict(
        name="C4 AST pins hold against current source (advisory)",
        passed=not failures,
        evidence=(
            f"results=[{', '.join(inv_status)}]"
            + (f" failures={failures}" if failures else "")
        ),
    )


def main() -> int:
    print("Empirical-closure verdict for Multi-Repo Sharding arc")
    print(f"  repo_root: {REPO_ROOT}")
    print()
    primary = [
        _eval_signature_substrate(),
        _eval_semantic_index_isolation(),
        _eval_domain_map_store_isolation(),
    ]
    advisory = _eval_ast_pins()
    for v in primary:
        mark = "PASS" if v.passed else "FAIL"
        print(f"  [{mark}] {v.name}")
        print(f"         {v.evidence}")
    print()
    mark = "PASS" if advisory.passed else "INFO"
    print(f"  [{mark}] {advisory.name}")
    print(f"         {advisory.evidence}")
    print()
    if all(v.passed for v in primary):
        print("VERDICT: Multi-Repo Sharding arc EMPIRICALLY CLOSED -- "
              "all three primary contracts PASSED.")
        return 0
    print("VERDICT: at least one primary contract FAILED -- "
          "Multi-Repo Sharding arc not yet empirically closed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
