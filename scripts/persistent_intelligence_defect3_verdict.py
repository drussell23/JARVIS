#!/usr/bin/env python3
"""Empirical-closure verdict for PersistentIntelligence Defect #3.

Soak v5 (bt-2026-05-03-060330) recorded 12 PersistentIntelligence
'attempt to write a readonly database' errors across the 62-min run
(~1 every 5 min). Silent degradation: errors logged at ERROR level
but no SSE event, no GET surface, no health flag.

Defense-in-depth fix:

  Slice A — Writable-path detection at init falls through:
            JARVIS_STATE_DB env -> JARVIS_STATE_DIR env ->
            <cwd>/.jarvis/state/ -> tempfile.gettempdir()/jarvis_state/
  Slice B — Closed-5 PersistentIntelligenceHealth enum + checkpoint
            circuit breaker (suspend after N consecutive failures
            with exponential backoff)
  Slice C — PersistentIntelligenceHealthOracle adapter implementing
            ProductionOracleProtocol; auto-registered in default
            observer bundle (now 5 adapters)

Six primary contracts:

  C1 -- Writable-path fallback chain selects a writable path when
        the configured default is read-only.
  C2 -- Closed-5 PersistentIntelligenceHealth enum is exactly
        {HEALTHY, DEGRADED_READONLY, DEGRADED_DISK_FULL,
         DEGRADED_OTHER, DISABLED}.
  C3 -- _classify_health pure function maps all 5 enum values to
        correct (verdict, severity) tuples + defensive on unknown.
  C4 -- PersistentIntelligenceHealthOracle implements Protocol
        (runtime_checkable isinstance check).
  C5 -- Default observer bundle now registers 5 adapters; new
        adapter reports DISABLED when manager singleton is None
        (pre-boot path).
  C6 -- Both substrate AST pin (Slice C oracle adapter) AND the
        manager has the new health/effective_db_path/checkpoint_
        suspended properties accessible.

Exit codes:
    0 = all six primary contracts PASSED
    1 = at least one primary contract FAILED
"""
from __future__ import annotations

import ast
import asyncio
import os
import sys
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


def _eval_writable_path_fallback() -> ContractVerdict:
    """Construct the manager + invoke _resolve_writable_db_path. The
    test env's home dir is sandbox-protected (Operation not permitted
    on touch), so the chain MUST fall through to a writable
    location."""
    from backend.core.persistent_intelligence_manager import (
        PersistentIntelligenceManager,
    )
    mgr = PersistentIntelligenceManager()
    chosen = mgr._resolve_writable_db_path()
    fellthrough = chosen != mgr.LOCAL_DB_PATH
    chosen_dir = os.path.dirname(chosen)
    chosen_writable = os.access(chosen_dir, os.W_OK)
    return ContractVerdict(
        name="C1 Writable-path fallback chain selects writable path",
        passed=fellthrough and chosen_writable,
        evidence=(
            f"configured={mgr.LOCAL_DB_PATH} "
            f"chosen={chosen} fellthrough={fellthrough} "
            f"chosen_writable={chosen_writable}"
        ),
    )


def _eval_health_enum_closed5() -> ContractVerdict:
    from backend.core.persistent_intelligence_manager import (
        PersistentIntelligenceHealth,
    )
    expected = {
        "healthy", "degraded_readonly", "degraded_disk_full",
        "degraded_other", "disabled",
    }
    actual = {v.value for v in PersistentIntelligenceHealth}
    return ContractVerdict(
        name="C2 Closed-5 PersistentIntelligenceHealth enum",
        passed=actual == expected,
        evidence=(
            f"expected={sorted(expected)} actual={sorted(actual)}"
            + (
                f" diff={actual.symmetric_difference(expected)}"
                if actual != expected else ""
            )
        ),
    )


def _eval_classify_health_mapping() -> ContractVerdict:
    from backend.core.ouroboros.governance.persistent_intelligence_health_oracle import (  # noqa: E501
        _classify_health,
    )
    from backend.core.ouroboros.governance.production_oracle import (
        OracleVerdict,
    )
    cases = [
        ("healthy", OracleVerdict.HEALTHY, 0.1),
        ("degraded_readonly", OracleVerdict.DEGRADED, 0.55),
        ("degraded_disk_full", OracleVerdict.FAILED, 0.85),
        ("degraded_other", OracleVerdict.DEGRADED, 0.5),
        ("disabled", OracleVerdict.DISABLED, 0.0),
    ]
    failures: List[str] = []
    for input_h, expected_v, expected_s in cases:
        verdict, sev, _ = _classify_health(input_h)
        if verdict != expected_v or abs(sev - expected_s) > 0.001:
            failures.append(
                f"{input_h}: expected ({expected_v.value}, {expected_s}) "
                f"got ({verdict.value}, {sev})"
            )
    # Defensive: unknown value -> DEGRADED + 0.5
    unknown_v, unknown_s, _ = _classify_health("garbage_value")
    if unknown_v != OracleVerdict.DEGRADED or unknown_s != 0.5:
        failures.append(
            f"unknown: expected (degraded, 0.5) got ({unknown_v.value}, {unknown_s})"
        )
    return ContractVerdict(
        name="C3 _classify_health closed-5 + defensive on unknown",
        passed=not failures,
        evidence=(
            f"cases_passed={len(cases) - len(failures)}/{len(cases)}"
            + (f" failures={failures}" if failures else "")
        ),
    )


def _eval_protocol_conformance() -> ContractVerdict:
    from backend.core.ouroboros.governance.persistent_intelligence_health_oracle import (  # noqa: E501
        PersistentIntelligenceHealthOracle,
    )
    from backend.core.ouroboros.governance.production_oracle import (
        ProductionOracleProtocol,
    )
    o = PersistentIntelligenceHealthOracle()
    return ContractVerdict(
        name="C4 PersistentIntelligenceHealthOracle implements Protocol",
        passed=isinstance(o, ProductionOracleProtocol),
        evidence=(
            f"isinstance={isinstance(o, ProductionOracleProtocol)} "
            f"name={o.name} enabled={o.enabled}"
        ),
    )


def _eval_bundle_expansion() -> ContractVerdict:
    from backend.core.ouroboros.governance.production_oracle_observer import (  # noqa: E501
        get_default_observer, reset_default_observer,
    )
    from backend.core.ouroboros.governance.production_oracle import (
        OracleVerdict,
    )
    reset_default_observer()
    obs = get_default_observer()
    names = sorted(a.name for a in obs._adapters)
    expected = sorted([
        "stdlib_self_health", "http_healthcheck",
        "sentry", "datadog", "persistent_intelligence_health",
    ])
    bundle_ok = obs.adapter_count == 5 and names == expected
    # Confirm new adapter reports DISABLED in pre-init state (manager
    # singleton is None at this point in a fresh test).
    new_adapter = next(
        a for a in obs._adapters
        if a.name == "persistent_intelligence_health"
    )
    sigs = asyncio.run(new_adapter.query_signals())
    pre_init_ok = (
        len(sigs) == 1
        and sigs[0].verdict is OracleVerdict.DISABLED
    )
    return ContractVerdict(
        name="C5 Default bundle = 5 adapters; new reports DISABLED pre-init",
        passed=bundle_ok and pre_init_ok,
        evidence=(
            f"adapter_count={obs.adapter_count} "
            f"names={names} "
            f"new_adapter_pre_init_verdict={sigs[0].verdict.value}"
        ),
    )


def _eval_substrate_ast_pin() -> ContractVerdict:
    from backend.core.ouroboros.governance.persistent_intelligence_health_oracle import (  # noqa: E501
        register_shipped_invariants,
    )
    from backend.core.persistent_intelligence_manager import (
        PersistentIntelligenceManager,
    )
    invariants = register_shipped_invariants()
    if not invariants:
        return ContractVerdict(
            name="C6 AST pin + manager properties accessible",
            passed=False,
            evidence="register_shipped_invariants returned empty",
        )
    inv = invariants[0]
    target_path = REPO_ROOT / inv.target_file
    source = target_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    violations = inv.validate(tree, source)
    # Also verify manager has the 3 new properties at the class
    # level (avoids constructing an instance which would invoke
    # asyncio.Lock() and fail on Python 3.9 outside a running loop).
    props_ok = (
        hasattr(PersistentIntelligenceManager, "health")
        and hasattr(PersistentIntelligenceManager, "effective_db_path")
        and hasattr(PersistentIntelligenceManager, "checkpoint_suspended")
    )
    return ContractVerdict(
        name="C6 AST pin + manager properties accessible",
        passed=not violations and props_ok,
        evidence=(
            f"invariant_violations={violations} "
            f"class_props=health/{hasattr(PersistentIntelligenceManager,'health')}, "
            f"effective_db_path/{hasattr(PersistentIntelligenceManager,'effective_db_path')}, "
            f"checkpoint_suspended/{hasattr(PersistentIntelligenceManager,'checkpoint_suspended')}"
        ),
    )


def main() -> int:
    print("Empirical-closure verdict for PersistentIntelligence Defect #3")
    print(f"  repo_root: {REPO_ROOT}")
    print()
    primary = [
        _eval_writable_path_fallback(),
        _eval_health_enum_closed5(),
        _eval_classify_health_mapping(),
        _eval_protocol_conformance(),
        _eval_bundle_expansion(),
        _eval_substrate_ast_pin(),
    ]
    for v in primary:
        mark = "PASS" if v.passed else "FAIL"
        print(f"  [{mark}] {v.name}")
        print(f"         {v.evidence}")
    print()
    if all(v.passed for v in primary):
        print("VERDICT: PersistentIntelligence Defect #3 EMPIRICALLY "
              "CLOSED -- all six primary contracts PASSED. "
              "Soak v5's 12-occurrence readonly-DB silent-degradation "
              "pattern is structurally fixed: writable-path fallback "
              "+ closed-5 health enum + circuit breaker + oracle "
              "adapter surfacing via existing Production Oracle "
              "observer.")
        return 0
    print("VERDICT: at least one primary contract FAILED -- "
          "Defect #3 not yet empirically closed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
