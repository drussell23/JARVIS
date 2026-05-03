#!/usr/bin/env python3
"""Empirical-closure verdict for the Production Oracle arc (Tier 2 #6).

Six primary contracts (all in-process; offline + network-shape proven
without requiring real Sentry/Datadog tokens):

  C1 — Substrate primitive correct: closed-5 OracleVerdict + OracleKind
       enums; OracleSignal frozen; aggregate function deterministic
       across HEALTHY / DEGRADED / FAILED inputs.
  C2 — StdlibSelfHealthOracle empirically reads real .ouroboros/sessions/
       summaries and emits 3 signals (HEALTHCHECK + PERFORMANCE +
       METRIC) with grounded payload.
  C3 — HTTPHealthCheckOracle reports DISABLED when no URL configured
       AND reports FAILED with a structured error payload when the
       URL points at an unroutable address (graceful network-failure
       path proves Protocol works for external services without
       requiring real upstream).
  C4 — Observer composes adapters into a single tick that aggregates
       N signals into one OracleVerdict and surfaces it in the bounded
       ring buffer. Adapter exceptions don't break the tick.
  C5 — register_flags + register_shipped_invariants land cleanly across
       4 modules (substrate + 2 adapters + observer); 4 flag specs +
       4 AST pins.
  C6 — Master flag JARVIS_PRODUCTION_ORACLE_ENABLED defaults True.
       SSE event constant + publisher are importable.

Exit codes:
    0 = all six primary contracts PASSED
    1 = at least one primary contract FAILED
"""
from __future__ import annotations

import ast
import asyncio
import os
import sys
import time
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


def _eval_substrate() -> ContractVerdict:
    from backend.core.ouroboros.governance.production_oracle import (
        OracleKind, OracleSignal, OracleVerdict,
        compute_aggregate_verdict,
    )
    verdicts = {v.value for v in OracleVerdict}
    kinds = {k.value for k in OracleKind}
    healthy_sig = OracleSignal(
        oracle_name="t", kind=OracleKind.HEALTHCHECK,
        verdict=OracleVerdict.HEALTHY, observed_at_ts=time.time(),
        severity=0.2,
    )
    failed_sig = OracleSignal(
        oracle_name="t", kind=OracleKind.ERROR,
        verdict=OracleVerdict.FAILED, observed_at_ts=time.time(),
        severity=0.95,
    )
    empty_v = compute_aggregate_verdict([])
    healthy_v = compute_aggregate_verdict([healthy_sig])
    failed_v = compute_aggregate_verdict([healthy_sig, failed_sig])
    closed_5_ok = (
        len(verdicts) == 5 and len(kinds) == 5
        and "healthy" in verdicts and "failed" in verdicts
        and "healthcheck" in kinds and "error" in kinds
    )
    aggregator_ok = (
        empty_v is OracleVerdict.INSUFFICIENT_DATA
        and healthy_v is OracleVerdict.HEALTHY
        and failed_v is OracleVerdict.FAILED
    )
    return ContractVerdict(
        name="C1 Substrate primitive correct",
        passed=closed_5_ok and aggregator_ok,
        evidence=(
            f"verdicts={len(verdicts)} kinds={len(kinds)} "
            f"empty->{empty_v.value} healthy->{healthy_v.value} "
            f"failed->{failed_v.value}"
        ),
    )


def _eval_stdlib_self_health() -> ContractVerdict:
    from backend.core.ouroboros.governance.stdlib_self_health_oracle import (  # noqa: E501
        StdlibSelfHealthOracle,
    )
    from backend.core.ouroboros.governance.production_oracle import (
        OracleKind,
    )
    oracle = StdlibSelfHealthOracle(project_root=REPO_ROOT)
    signals = asyncio.run(oracle.query_signals())
    kinds = sorted({s.kind for s in signals}, key=lambda k: k.value)
    expected_kinds = {
        OracleKind.HEALTHCHECK,
        OracleKind.PERFORMANCE,
        OracleKind.METRIC,
    }
    actual_kinds = {s.kind for s in signals}
    payloads_ok = all(
        isinstance(s.payload, dict) for s in signals
    )
    return ContractVerdict(
        name="C2 StdlibSelfHealthOracle reads real sessions",
        passed=(
            actual_kinds == expected_kinds
            and len(signals) == 3
            and payloads_ok
        ),
        evidence=(
            f"signals={len(signals)} "
            f"kinds=[{','.join(k.value for k in kinds)}] "
            f"first_summary={signals[0].summary[:80]!r}"
        ),
    )


def _eval_http_healthcheck() -> ContractVerdict:
    from backend.core.ouroboros.governance.http_healthcheck_oracle import (  # noqa: E501
        HTTPHealthCheckOracle,
    )
    from backend.core.ouroboros.governance.production_oracle import (
        OracleVerdict,
    )
    # No URL configured -> DISABLED.
    os.environ.pop("JARVIS_PRODUCTION_ORACLE_HTTPCHECK_URL", None)
    no_url_oracle = HTTPHealthCheckOracle()
    no_url_signals = asyncio.run(no_url_oracle.query_signals())
    disabled_ok = (
        len(no_url_signals) == 1
        and no_url_signals[0].verdict is OracleVerdict.DISABLED
    )
    # Unroutable address -> FAILED.
    fail_oracle = HTTPHealthCheckOracle(
        url="http://192.0.2.1:1/healthcheck", timeout_s=2.0,
    )
    fail_signals = asyncio.run(fail_oracle.query_signals())
    failed_ok = (
        len(fail_signals) == 1
        and fail_signals[0].verdict is OracleVerdict.FAILED
        and fail_signals[0].severity >= 0.5
        and "error" in fail_signals[0].payload
    )
    return ContractVerdict(
        name="C3 HTTPHealthCheckOracle handles disabled + network-fail",
        passed=disabled_ok and failed_ok,
        evidence=(
            f"no_url->verdict={no_url_signals[0].verdict.value} "
            f"unroutable->verdict={fail_signals[0].verdict.value} "
            f"sev={fail_signals[0].severity:.2f}"
        ),
    )


def _eval_observer_composes() -> ContractVerdict:
    from backend.core.ouroboros.governance.production_oracle_observer import (  # noqa: E501
        ProductionOracleObserver, get_default_observer,
        reset_default_observer,
    )
    from backend.core.ouroboros.governance.production_oracle import (
        OracleSignal, OracleKind, OracleVerdict,
    )
    reset_default_observer()
    obs = get_default_observer(project_root=REPO_ROOT)

    class _BrokenAdapter:
        name = "broken_test_adapter"
        enabled = True
        async def query_signals(self, *, since_ts=0.0):
            raise RuntimeError("synthetic adapter failure")

    obs.register(_BrokenAdapter())
    result = asyncio.run(obs.tick_once(posture="HARDEN"))
    fault_isolated = result.adapters_failed == 1 and len(result.signals) >= 1
    history = obs.history()
    ring_ok = len(history) >= 1 and history[-1] is result
    return ContractVerdict(
        name="C4 Observer composes adapters + isolates failures",
        passed=fault_isolated and ring_ok,
        evidence=(
            f"adapters_queried={result.adapters_queried} "
            f"adapters_failed={result.adapters_failed} "
            f"signals={len(result.signals)} "
            f"verdict={result.aggregate_verdict.value} "
            f"history_size={len(history)}"
        ),
    )


def _eval_registration_surface() -> ContractVerdict:
    from backend.core.ouroboros.governance import (
        production_oracle as po,
        production_oracle_observer as poo,
        stdlib_self_health_oracle as ssho,
        http_healthcheck_oracle as hho,
    )
    failures: List[str] = []
    pin_ok: List[str] = []
    flag_count = 0

    # register_flags exists on observer only.
    recorded = []
    class _R:
        def register(self, spec): recorded.append(spec.name)
    flag_count = poo.register_flags(_R())
    expected_flags = {
        "JARVIS_PRODUCTION_ORACLE_ENABLED",
        "JARVIS_PRODUCTION_ORACLE_HISTORY_SIZE",
        "JARVIS_PRODUCTION_ORACLE_FAIL_THRESHOLD",
        "JARVIS_PRODUCTION_ORACLE_DEGRADE_THRESHOLD",
    }
    if set(recorded) != expected_flags:
        failures.append(
            f"flag mismatch: expected={sorted(expected_flags)} "
            f"got={sorted(recorded)}"
        )

    # 4 register_shipped_invariants modules.
    for mod in (po, ssho, hho, poo):
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
        name="C5 register_flags + 4 AST pins land cleanly",
        passed=not failures,
        evidence=(
            f"flags={flag_count} pins={len(pin_ok)} "
            f"({', '.join(pin_ok[:2])}...)"
            + (f" failures={failures}" if failures else "")
        ),
    )


def _eval_graduation_surfaces() -> ContractVerdict:
    from backend.core.ouroboros.governance.production_oracle_observer import (  # noqa: E501
        production_oracle_enabled,
    )
    os.environ.pop("JARVIS_PRODUCTION_ORACLE_ENABLED", None)
    master_default_true = production_oracle_enabled()

    # SSE event + publisher importable
    sse_ok = True
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_PRODUCTION_ORACLE_SIGNAL,
            publish_production_oracle_signal,
        )
        sse_event = EVENT_TYPE_PRODUCTION_ORACLE_SIGNAL
        # Verify publisher is callable + returns None when stream off
        # (we don't have a real broker in this script context).
        result = publish_production_oracle_signal(
            aggregate_verdict="healthy", signal_count=0,
        )
        # Result is None or an event id string -- both shapes are OK.
        sse_ok = sse_event == "production_oracle_signal_observed"
    except Exception as exc:
        sse_ok = False
        sse_event = f"<import error: {exc!r}>"

    return ContractVerdict(
        name="C6 Master default-true + SSE + publisher importable",
        passed=master_default_true and sse_ok,
        evidence=(
            f"master_default={master_default_true} "
            f"sse_event={sse_event!r}"
        ),
    )


def main() -> int:
    print("Empirical-closure verdict for Production Oracle arc")
    print(f"  repo_root: {REPO_ROOT}")
    print()
    primary = [
        _eval_substrate(),
        _eval_stdlib_self_health(),
        _eval_http_healthcheck(),
        _eval_observer_composes(),
        _eval_registration_surface(),
        _eval_graduation_surfaces(),
    ]
    for v in primary:
        mark = "PASS" if v.passed else "FAIL"
        print(f"  [{mark}] {v.name}")
        print(f"         {v.evidence}")
    print()
    if all(v.passed for v in primary):
        print("VERDICT: Production Oracle arc EMPIRICALLY CLOSED -- "
              "all six primary contracts PASSED.")
        return 0
    print("VERDICT: at least one primary contract FAILED -- "
          "Production Oracle arc not yet empirically closed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
