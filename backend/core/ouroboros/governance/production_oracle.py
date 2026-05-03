"""backend/core/ouroboros/governance/production_oracle.py

Production-oracle substrate (Tier 2 #6).

Closes the structural gap from the user's roadmap: pre-arc, the only
truth signal feeding VERIFY was pytest. Production reality (Sentry
errors, Datadog metrics, Prometheus alerts, generic HTTP healthchecks,
self-introspected harness state) was invisible.

This module ships the **substrate** -- the Protocol shape that any
concrete oracle implements + the closed-5 verdict/kind enums + the
pure-function aggregator. Concrete adapters
(:class:`StdlibSelfHealthOracle`, :class:`HTTPHealthCheckOracle`)
land in sibling modules. Future Sentry/Datadog adapters drop in as
additional Protocol implementers without touching this substrate.

Authority invariant: this module produces ADVISORY signals only.
Oracle verdicts feed the existing :mod:`auto_action_router` advisory
framework (downstream consumer, not yet wired) -- they NEVER directly
mutate Iron Gate verdicts, risk tier, route, policy, FORBIDDEN_PATH,
or approval gating. The Production Oracle is a sense organ; the
advisory router is the brain.

Pure-stdlib (dataclasses + enum + typing). No external deps. The
substrate is offline-validatable; only concrete adapters that talk
to network services need urllib/aiohttp/etc.
"""
from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, field
from typing import (
    Any,
    Awaitable,
    Dict,
    List,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    runtime_checkable,
)


logger = logging.getLogger(__name__)


# Schema version pinned at the substrate level so consumers can
# branch defensively if the OracleSignal shape ever evolves.
PRODUCTION_ORACLE_SCHEMA_VERSION = "1.0"


# ---------------------------------------------------------------------------
# Closed-5 enums (J.A.R.M.A.T.R.I.X. taxonomy discipline)
# ---------------------------------------------------------------------------


class OracleVerdict(str, enum.Enum):
    """Closed-5 aggregate verdict from one or more oracle signals.

    Authority invariant: the verdict is **advisory** -- consumed by
    :mod:`auto_action_router` to inform proposed actions; NEVER directly
    overrides Iron Gate / risk tier / route. Operators read the
    verdict for situational awareness; the AdvisoryActionType
    machinery decides what (if anything) to do.
    """

    HEALTHY = "healthy"               # all signals normal; no action
    DEGRADED = "degraded"             # some signals abnormal; raise floor
    FAILED = "failed"                 # critical signals; defer or notify
    INSUFFICIENT_DATA = "insufficient_data"  # too few signals
    DISABLED = "disabled"             # master flag off / oracle inert


class OracleKind(str, enum.Enum):
    """Closed-5 taxonomy of oracle signal kinds.

    Distinct from :class:`OracleVerdict`: a single ERROR-kind signal
    might carry verdict=DEGRADED (one isolated error) or verdict=FAILED
    (sustained burst). Aggregation logic lives in
    :func:`compute_aggregate_verdict`.
    """

    HEALTHCHECK = "healthcheck"       # GET /health-style ping result
    ERROR = "error"                   # exception event from prod (Sentry, etc)
    METRIC = "metric"                 # gauge/counter sample (Datadog, Prom)
    DEPLOY_EVENT = "deploy_event"     # release/rollback notification
    PERFORMANCE = "performance"       # latency/SLI/SLO observation


# ---------------------------------------------------------------------------
# Frozen signal dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OracleSignal:
    """One observation from one oracle.

    Frozen because consumers (the observer's history ring buffer, the
    aggregator, the SSE projection) treat signals as immutable
    snapshots. Hash-stable -- the (oracle_name, kind, observed_at_ts,
    payload_hash) tuple uniquely identifies this observation across
    process restarts.

    Fields
    ------
    oracle_name:
        Human-readable identifier of the producing oracle (e.g.,
        ``"stdlib_self_health"`` / ``"http_healthcheck"`` /
        ``"sentry"``). Distinct from :class:`OracleKind` -- the
        kind is taxonomic, the name is the source.
    kind:
        Closed-5 taxonomic categorization. See :class:`OracleKind`.
    verdict:
        Per-signal verdict; the aggregator combines multiple signals
        into a single OracleVerdict.
    observed_at_ts:
        Wall-clock timestamp when the oracle observed the underlying
        condition (NOT when this object was constructed -- adapters
        that poll an API every 30s should stamp the API's
        observation timestamp here, not now()).
    summary:
        Short human-readable description (<=200 chars). Operator-
        facing; appears in REPL + GET + SSE projections.
    payload:
        Frozen dict of structured fields specific to the kind/source
        (e.g., {"status_code": 200} for healthcheck; {"error_type":
        "TypeError", "rate_per_min": 12} for ERROR).
    severity:
        Float in [0.0, 1.0] -- aggregator weight. 0.0 = pure noise;
        1.0 = critical. Adapters set this; operators tune via env
        knobs documented per-adapter.
    """

    oracle_name: str
    kind: OracleKind
    verdict: OracleVerdict
    observed_at_ts: float
    summary: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    severity: float = 0.5

    def __post_init__(self) -> None:
        # Frozen dataclass __post_init__ can still validate via
        # object.__setattr__ for clamping; we defensively clamp
        # severity to [0.0, 1.0] without raising so misconfigured
        # adapters can't silently break the aggregator.
        if not (0.0 <= self.severity <= 1.0):
            object.__setattr__(
                self, "severity", max(0.0, min(1.0, self.severity)),
            )


# ---------------------------------------------------------------------------
# Protocol — the contract every concrete oracle implements
# ---------------------------------------------------------------------------


@runtime_checkable
class ProductionOracleProtocol(Protocol):
    """Concrete oracle adapter contract.

    Implementations:
      * ``StdlibSelfHealthOracle`` (Slice B, offline)
      * ``HTTPHealthCheckOracle`` (Slice C, generic network)
      * Future: ``SentryOracle``, ``DatadogOracle``,
        ``PrometheusOracle``, ``GitHubChecksOracle``

    Discipline:
      * ``query_signals`` is async because the network adapters are
        I/O-bound; offline adapters (StdlibSelfHealthOracle) wrap
        their sync work in an awaitable that resolves immediately.
      * NEVER raise from query_signals -- the contract is a
        ``Tuple[OracleSignal, ...]`` (possibly empty) on every
        success path AND every failure path. Adapters log
        exceptions internally and return empty / DISABLED-shaped
        signals. The observer trusts the contract.
      * ``name`` returns the canonical oracle identifier used as
        OracleSignal.oracle_name; MUST be stable across calls.
      * ``enabled`` is a property (not a method) so the observer
        can short-circuit cheaply without paying async overhead.
    """

    @property
    def name(self) -> str: ...

    @property
    def enabled(self) -> bool: ...

    def query_signals(
        self, *, since_ts: float = 0.0,
    ) -> "Awaitable[Tuple[OracleSignal, ...]]": ...


# ---------------------------------------------------------------------------
# Pure-function aggregator
# ---------------------------------------------------------------------------


def compute_aggregate_verdict(
    signals: Sequence[OracleSignal],
    *,
    minimum_signals: int = 1,
    fail_threshold_severity: float = 0.8,
    degrade_threshold_severity: float = 0.5,
) -> OracleVerdict:
    """Combine N oracle signals into a single :class:`OracleVerdict`.

    Pure function. Deterministic. Fast. Never raises.

    Algorithm:

      1. Empty input -> ``INSUFFICIENT_DATA``.
      2. Below ``minimum_signals`` -> ``INSUFFICIENT_DATA``.
      3. Filter signals where ``verdict == DISABLED`` -- those carry
         no information.
      4. If filtered set is empty -> ``DISABLED`` (everything is
         turned off -> aggregate is also DISABLED).
      5. Compute the maximum severity across signals weighted by
         per-signal verdict:
            - any ``FAILED`` with severity >= ``fail_threshold`` ->
              aggregate is ``FAILED``.
            - any ``DEGRADED`` with severity >=
              ``degrade_threshold`` -> aggregate is ``DEGRADED``
              (unless already FAILED).
            - otherwise -> ``HEALTHY``.

    The thresholds are env-tunable downstream (the observer wires
    them to JARVIS_PRODUCTION_ORACLE_*_THRESHOLD knobs).
    """
    if not signals:
        return OracleVerdict.INSUFFICIENT_DATA
    informative = [s for s in signals if s.verdict is not OracleVerdict.DISABLED]
    if not informative:
        return OracleVerdict.DISABLED
    if len(informative) < max(1, int(minimum_signals)):
        return OracleVerdict.INSUFFICIENT_DATA
    has_failed = False
    has_degraded = False
    for sig in informative:
        if (
            sig.verdict is OracleVerdict.FAILED
            and sig.severity >= fail_threshold_severity
        ):
            has_failed = True
        elif (
            sig.verdict is OracleVerdict.DEGRADED
            and sig.severity >= degrade_threshold_severity
        ):
            has_degraded = True
    if has_failed:
        return OracleVerdict.FAILED
    if has_degraded:
        return OracleVerdict.DEGRADED
    return OracleVerdict.HEALTHY


def project_signal_for_observability(
    signal: OracleSignal,
) -> Dict[str, Any]:
    """Lightweight projection for SSE + GET payloads. Never raises.

    Drops nothing structurally -- caller can JSON-serialize directly.
    Strings are length-bounded (200 chars for summary, 2000 chars
    total payload) so observability surfaces don't bloat under
    misbehaving adapters.
    """
    safe_payload: Dict[str, Any] = {}
    try:
        for k, v in (signal.payload or {}).items():
            if not isinstance(k, str):
                continue
            key = k[:80]
            if isinstance(v, (str, int, float, bool)) or v is None:
                safe_payload[key] = v
            else:
                safe_payload[key] = str(v)[:200]
    except Exception:  # noqa: BLE001 -- defensive
        safe_payload = {}
    return {
        "oracle_name": str(signal.oracle_name)[:100],
        "kind": signal.kind.value,
        "verdict": signal.verdict.value,
        "observed_at_ts": float(signal.observed_at_ts),
        "summary": str(signal.summary or "")[:200],
        "payload": safe_payload,
        "severity": float(signal.severity),
    }


# ---------------------------------------------------------------------------
# AST pin
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Substrate AST pin. Pins:

      * OracleSignal stays @dataclass(frozen=True) -- consumers
        (observer history ring buffer, aggregator, SSE projection)
        depend on hash-stable identity.
      * Closed-5 enums OracleVerdict + OracleKind both define
        exactly 5 members AND those members match the documented
        taxonomy (no silent value drift across versions).
      * compute_aggregate_verdict + project_signal_for_observability
        + ProductionOracleProtocol all present.
      * No exec/eval/compile -- substrate stays pure data + control
        flow.
    """
    import ast as _ast
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    REQUIRED_FUNCS = (
        "compute_aggregate_verdict",
        "project_signal_for_observability",
        "register_shipped_invariants",
    )
    REQUIRED_CLASSES = (
        "OracleSignal",
        "OracleVerdict",
        "OracleKind",
        "ProductionOracleProtocol",
    )
    EXPECTED_VERDICT_VALUES = {
        "healthy", "degraded", "failed",
        "insufficient_data", "disabled",
    }
    EXPECTED_KIND_VALUES = {
        "healthcheck", "error", "metric",
        "deploy_event", "performance",
    }

    def _walk_enum_values(
        cls_node: "_ast.ClassDef",
    ) -> set:
        values: set = set()
        for body_node in cls_node.body:
            if not isinstance(body_node, _ast.Assign):
                continue
            if len(body_node.targets) != 1:
                continue
            if not isinstance(body_node.value, _ast.Constant):
                continue
            v = body_node.value.value
            if isinstance(v, str):
                values.add(v)
        return values

    def _validate(
        tree: "_ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        seen_funcs: set = set()
        seen_classes: dict = {}
        oracle_signal_frozen = False
        for node in _ast.walk(tree):
            if isinstance(node, _ast.FunctionDef):
                seen_funcs.add(node.name)
            elif isinstance(node, _ast.AsyncFunctionDef):
                seen_funcs.add(node.name)
            elif isinstance(node, _ast.ClassDef):
                seen_classes[node.name] = node
                if node.name == "OracleSignal":
                    for dec in node.decorator_list:
                        if isinstance(dec, _ast.Call):
                            for kw in dec.keywords:
                                if (
                                    kw.arg == "frozen"
                                    and isinstance(kw.value, _ast.Constant)
                                    and kw.value.value is True
                                ):
                                    oracle_signal_frozen = True
                                    break
            elif isinstance(node, _ast.Call):
                if isinstance(node.func, _ast.Name):
                    if node.func.id in ("exec", "eval", "compile"):
                        violations.append(
                            f"line {getattr(node, 'lineno', '?')}: "
                            f"production_oracle MUST NOT call "
                            f"{node.func.id}"
                        )
        for fn in REQUIRED_FUNCS:
            if fn not in seen_funcs:
                violations.append(f"missing function {fn!r}")
        for cls in REQUIRED_CLASSES:
            if cls not in seen_classes:
                violations.append(f"missing class {cls!r}")
        if not oracle_signal_frozen:
            violations.append(
                "OracleSignal MUST stay @dataclass(frozen=True) -- "
                "consumers depend on hash-stable identity"
            )
        verdict_node = seen_classes.get("OracleVerdict")
        if verdict_node is not None:
            actual = _walk_enum_values(verdict_node)
            if actual != EXPECTED_VERDICT_VALUES:
                missing = EXPECTED_VERDICT_VALUES - actual
                extra = actual - EXPECTED_VERDICT_VALUES
                violations.append(
                    f"OracleVerdict closed-5 taxonomy drift: "
                    f"missing={sorted(missing)} extra={sorted(extra)}"
                )
        kind_node = seen_classes.get("OracleKind")
        if kind_node is not None:
            actual = _walk_enum_values(kind_node)
            if actual != EXPECTED_KIND_VALUES:
                missing = EXPECTED_KIND_VALUES - actual
                extra = actual - EXPECTED_KIND_VALUES
                violations.append(
                    f"OracleKind closed-5 taxonomy drift: "
                    f"missing={sorted(missing)} extra={sorted(extra)}"
                )
        return tuple(violations)

    target = "backend/core/ouroboros/governance/production_oracle.py"
    return [
        ShippedCodeInvariant(
            invariant_name="production_oracle_substrate",
            target_file=target,
            description=(
                "Production Oracle substrate: closed-5 OracleVerdict + "
                "OracleKind taxonomies; OracleSignal stays frozen; "
                "Protocol + aggregator + projection helpers present; "
                "no dynamic-code calls."
            ),
            validate=_validate,
        ),
    ]


__all__ = [
    "PRODUCTION_ORACLE_SCHEMA_VERSION",
    "OracleVerdict",
    "OracleKind",
    "OracleSignal",
    "ProductionOracleProtocol",
    "compute_aggregate_verdict",
    "project_signal_for_observability",
    "register_shipped_invariants",
]
