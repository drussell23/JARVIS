#!/usr/bin/env python3
"""Empirical-closure verdict for the Sentry + Datadog vendor adapters
(Tier 2 #6 follow-up Arc 2).

Five primary contracts (in-process; no real Sentry/Datadog tokens
required — all paths exercise DISABLED/empty-env behavior):

  C1 — SentryOracle DISABLED when SENTRY_AUTH_TOKEN unset.
  C2 — DatadogOracle DISABLED when DD_API_KEY/DD_APP_KEY unset.
  C3 — Both adapters implement ProductionOracleProtocol structurally
       (runtime_checkable isinstance check passes).
  C4 — Default observer bundle now registers 4 adapters (stdlib +
       http + sentry + datadog); aggregator handles DISABLED signals
       cleanly when only the offline anchor is configured.
  C5 — All 4 register_shipped_invariants AST pins hold against live
       source (substrate + 3 adapters).

Exit codes:
    0 = all five primary contracts PASSED
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


def _eval_sentry_disabled() -> ContractVerdict:
    from backend.core.ouroboros.governance.sentry_oracle import (
        SentryOracle,
    )
    from backend.core.ouroboros.governance.production_oracle import (
        OracleVerdict,
    )
    for k in ("SENTRY_AUTH_TOKEN", "JARVIS_SENTRY_ORG"):
        os.environ.pop(k, None)
    o = SentryOracle()
    enabled_when_no_env = o.enabled
    sigs = asyncio.run(o.query_signals())
    no_token = sigs[0].verdict is OracleVerdict.DISABLED
    # With token but no org -> still DISABLED.
    os.environ["SENTRY_AUTH_TOKEN"] = "stub-token"
    os.environ.pop("JARVIS_SENTRY_ORG", None)
    sigs2 = asyncio.run(SentryOracle().query_signals())
    no_org = sigs2[0].verdict is OracleVerdict.DISABLED
    os.environ.pop("SENTRY_AUTH_TOKEN", None)
    return ContractVerdict(
        name="C1 SentryOracle DISABLED when env unset",
        passed=(
            enabled_when_no_env is False
            and no_token and no_org
        ),
        evidence=(
            f"enabled_when_no_env={enabled_when_no_env} "
            f"no_token_verdict={sigs[0].verdict.value} "
            f"no_org_verdict={sigs2[0].verdict.value}"
        ),
    )


def _eval_datadog_disabled() -> ContractVerdict:
    from backend.core.ouroboros.governance.datadog_oracle import (
        DatadogOracle,
    )
    from backend.core.ouroboros.governance.production_oracle import (
        OracleVerdict,
    )
    for k in ("DD_API_KEY", "DD_APP_KEY"):
        os.environ.pop(k, None)
    o = DatadogOracle()
    enabled_when_no_env = o.enabled
    sigs = asyncio.run(o.query_signals())
    no_keys = sigs[0].verdict is OracleVerdict.DISABLED
    # With api_key but no app_key -> still DISABLED.
    os.environ["DD_API_KEY"] = "stub-api-key"
    os.environ.pop("DD_APP_KEY", None)
    sigs2 = asyncio.run(DatadogOracle().query_signals())
    no_app_key = sigs2[0].verdict is OracleVerdict.DISABLED
    os.environ.pop("DD_API_KEY", None)
    return ContractVerdict(
        name="C2 DatadogOracle DISABLED when env unset",
        passed=(
            enabled_when_no_env is False
            and no_keys and no_app_key
        ),
        evidence=(
            f"enabled_when_no_env={enabled_when_no_env} "
            f"no_keys_verdict={sigs[0].verdict.value} "
            f"no_app_key_verdict={sigs2[0].verdict.value}"
        ),
    )


def _eval_protocol_conformance() -> ContractVerdict:
    from backend.core.ouroboros.governance.sentry_oracle import (
        SentryOracle,
    )
    from backend.core.ouroboros.governance.datadog_oracle import (
        DatadogOracle,
    )
    from backend.core.ouroboros.governance.production_oracle import (
        ProductionOracleProtocol,
    )
    s_ok = isinstance(SentryOracle(), ProductionOracleProtocol)
    d_ok = isinstance(DatadogOracle(), ProductionOracleProtocol)
    return ContractVerdict(
        name="C3 Both adapters structurally implement Protocol",
        passed=s_ok and d_ok,
        evidence=(
            f"SentryOracle_isinstance={s_ok} "
            f"DatadogOracle_isinstance={d_ok}"
        ),
    )


def _eval_default_bundle() -> ContractVerdict:
    from backend.core.ouroboros.governance.production_oracle_observer import (  # noqa: E501
        get_default_observer, reset_default_observer,
    )
    reset_default_observer()
    obs = get_default_observer()
    names = [a.name for a in obs._adapters]
    expected = {
        "stdlib_self_health", "http_healthcheck",
        "sentry", "datadog",
    }
    have = set(names)
    # Tick once to confirm the aggregator handles a mix of enabled
    # + disabled adapters.
    result = asyncio.run(obs.tick_once(posture="MAINTAIN"))
    return ContractVerdict(
        name="C4 Default bundle registers 4 adapters",
        passed=(
            obs.adapter_count == 4
            and have == expected
            and result.adapters_failed == 0
        ),
        evidence=(
            f"adapter_count={obs.adapter_count} "
            f"names={sorted(names)} "
            f"verdict={result.aggregate_verdict.value} "
            f"adapters_failed={result.adapters_failed}"
        ),
    )


def _eval_ast_pins() -> ContractVerdict:
    from backend.core.ouroboros.governance import (
        production_oracle as po,
        production_oracle_observer as poo,
        sentry_oracle as so,
        datadog_oracle as ddo,
        stdlib_self_health_oracle as ssho,
        http_healthcheck_oracle as hho,
    )
    failures: List[str] = []
    pin_ok: List[str] = []
    for mod in (po, ssho, hho, poo, so, ddo):
        invariants = mod.register_shipped_invariants()
        for inv in invariants:
            target_path = REPO_ROOT / inv.target_file
            source = target_path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            violations = inv.validate(tree, source)
            if violations:
                failures.append(
                    f"{inv.invariant_name}: {violations[:2]}"
                )
            else:
                pin_ok.append(inv.invariant_name)
    return ContractVerdict(
        name="C5 All AST pins hold across substrate + adapters",
        passed=not failures,
        evidence=(
            f"pins={len(pin_ok)} "
            f"({', '.join(pin_ok[:3])}...)"
            + (f" failures={failures}" if failures else "")
        ),
    )


def main() -> int:
    print("Empirical-closure verdict for Sentry + Datadog vendor "
          "adapters (Arc 2)")
    print(f"  repo_root: {REPO_ROOT}")
    print()
    primary = [
        _eval_sentry_disabled(),
        _eval_datadog_disabled(),
        _eval_protocol_conformance(),
        _eval_default_bundle(),
        _eval_ast_pins(),
    ]
    for v in primary:
        mark = "PASS" if v.passed else "FAIL"
        print(f"  [{mark}] {v.name}")
        print(f"         {v.evidence}")
    print()
    if all(v.passed for v in primary):
        print("VERDICT: Arc 2 EMPIRICALLY CLOSED -- all five primary "
              "contracts PASSED. Sentry + Datadog adapters live in "
              "the default bundle; both report DISABLED until env "
              "auth is configured.")
        return 0
    print("VERDICT: at least one primary contract FAILED -- "
          "Arc 2 not yet empirically closed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
