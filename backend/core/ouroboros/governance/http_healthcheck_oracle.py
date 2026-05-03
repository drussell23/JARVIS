"""backend/core/ouroboros/governance/http_healthcheck_oracle.py

Generic HTTP healthcheck oracle. Implements
:class:`production_oracle.ProductionOracleProtocol` against any
configurable HTTP endpoint.

Works against any healthcheck-style endpoint:
  * Generic ``/health`` returning JSON ``{"status": "ok"}``
  * Sentry / Datadog / Prometheus health endpoints (vendor-specific
    response parsing lands in dedicated adapters; this oracle only
    reports HTTP-level success/failure)
  * Self-hosted dashboards / load balancer probes / CDN edge probes

Why this oracle is load-bearing for the arc:
  * Proves the Production Oracle Protocol supports network adapters
    (the substrate is offline-only without it).
  * Mirrors the existing urllib.request pattern from
    ``boot_handshake.py`` -- no new HTTP client dependency, same
    ``urllib.error.URLError`` handling, same async-via-executor
    discipline.
  * Future Sentry/Datadog/Prometheus adapters are siblings of this
    oracle, NOT extensions of it -- vendor-specific JSON parsing,
    auth header construction, and rate-limit handling diverge enough
    that a single "do-everything" HTTP oracle would become a god
    object.

Configuration:
  * ``JARVIS_PRODUCTION_ORACLE_HTTPCHECK_URL`` -- target URL.
    Empty/unset -> oracle reports DISABLED signal.
  * ``JARVIS_PRODUCTION_ORACLE_HTTPCHECK_TIMEOUT_S`` -- seconds.
    Default 5.0; floor 0.5; cap 60.0.
  * ``JARVIS_PRODUCTION_ORACLE_HTTPCHECK_EXPECT_STATUS`` -- comma-
    separated set of status codes that count as HEALTHY (default
    "200"). Status codes outside the set but in 2xx-3xx -> DEGRADED;
    4xx/5xx -> FAILED; network error / timeout -> FAILED.

Authority invariant: same as substrate -- emits ADVISORY signals
only. Network failure does NOT degrade the oracle's enabled state;
it just produces a FAILED OracleSignal and the aggregator decides
what to do.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional, Tuple

from backend.core.ouroboros.governance.production_oracle import (
    OracleKind,
    OracleSignal,
    OracleVerdict,
)


logger = logging.getLogger(__name__)


_ORACLE_NAME = "http_healthcheck"
_DEFAULT_TIMEOUT_S = 5.0
_TIMEOUT_FLOOR_S = 0.5
_TIMEOUT_CEILING_S = 60.0
_DEFAULT_EXPECT_STATUS = "200"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def http_healthcheck_enabled() -> bool:
    """Per-adapter sub-gate. The oracle is also implicitly disabled
    when no URL is configured -- this knob is the explicit operator
    opt-out."""
    return _env_bool("JARVIS_HTTP_HEALTHCHECK_ORACLE_ENABLED", True)


def healthcheck_url() -> str:
    return os.environ.get(
        "JARVIS_PRODUCTION_ORACLE_HTTPCHECK_URL", "",
    ).strip()


def healthcheck_timeout_s() -> float:
    raw = os.environ.get(
        "JARVIS_PRODUCTION_ORACLE_HTTPCHECK_TIMEOUT_S",
    )
    if raw is None:
        return _DEFAULT_TIMEOUT_S
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT_S
    return max(_TIMEOUT_FLOOR_S, min(_TIMEOUT_CEILING_S, v))


def healthcheck_expect_status() -> frozenset:
    raw = os.environ.get(
        "JARVIS_PRODUCTION_ORACLE_HTTPCHECK_EXPECT_STATUS",
        _DEFAULT_EXPECT_STATUS,
    )
    out: set = set()
    for tok in (raw or "").split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            out.add(int(tok))
        except (TypeError, ValueError):
            continue
    if not out:
        out = {200}
    return frozenset(out)


def _disabled_signal(reason: str, payload: Optional[dict] = None) -> OracleSignal:
    return OracleSignal(
        oracle_name=_ORACLE_NAME, kind=OracleKind.HEALTHCHECK,
        verdict=OracleVerdict.DISABLED, observed_at_ts=time.time(),
        summary=reason, payload=payload or {}, severity=0.0,
    )


def _classify_status(status_code: int) -> Tuple[OracleVerdict, float]:
    """Map an HTTP status code to (verdict, severity). The expected-
    status set is consulted by the caller; this helper handles only
    the post-filter classification when the status is NOT in the
    expected set (i.e., the response was unexpected)."""
    if 200 <= status_code < 300:
        # 2xx but unexpected (e.g., 204 when caller expected 200) ->
        # mild degradation, low severity.
        return OracleVerdict.DEGRADED, 0.3
    if 300 <= status_code < 400:
        return OracleVerdict.DEGRADED, 0.4
    if 400 <= status_code < 500:
        # 4xx -- client-side error from our perspective. Likely
        # auth / config drift / stale URL.
        return OracleVerdict.FAILED, 0.7
    if 500 <= status_code < 600:
        # 5xx -- the upstream service is broken. Classic FAILED.
        return OracleVerdict.FAILED, 0.85
    # Anything else (1xx informational, 6xx+ nonstandard) -> DEGRADED.
    return OracleVerdict.DEGRADED, 0.5


def _do_blocking_get(url: str, timeout_s: float) -> Tuple[int, str, str]:
    """Synchronous HTTP GET. Returns
    ``(status_code, reason_phrase, error_summary)``.

    ``status_code`` is 0 on network error / timeout; ``error_summary``
    is empty on success and a short string otherwise.
    Mirrors :func:`boot_handshake._fetch_urllib`.
    """
    import urllib.request
    import urllib.error
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return int(resp.getcode()), str(resp.reason or ""), ""
    except urllib.error.HTTPError as http_exc:
        # HTTPError IS a Response -- carries a status code we can use.
        return int(http_exc.code), str(http_exc.reason or ""), ""
    except urllib.error.URLError as url_exc:
        return 0, "", f"url_error:{url_exc.reason!r}"[:200]
    except OSError as os_exc:
        return 0, "", f"os_error:{os_exc!r}"[:200]
    except Exception as exc:  # noqa: BLE001 -- contract: never raise
        return 0, "", f"exception:{type(exc).__name__}:{exc}"[:200]


class HTTPHealthCheckOracle:
    """Generic HTTP healthcheck oracle.

    Implements :class:`production_oracle.ProductionOracleProtocol`
    structurally (duck-typed; Protocol is ``@runtime_checkable``).

    The HTTP call runs in the default asyncio executor so the
    observer's event loop doesn't block on the network round-trip.
    NEVER raises -- every failure shape produces an OracleSignal
    with verdict=FAILED|DISABLED.
    """

    def __init__(
        self,
        *,
        url: Optional[str] = None,
        timeout_s: Optional[float] = None,
        expect_status: Optional[frozenset] = None,
    ) -> None:
        # Constructor args take precedence over env vars; pass None
        # to defer to env at every call (default behavior so
        # operators can hot-swap config).
        self._explicit_url = (url or "").strip() if url else None
        self._explicit_timeout = timeout_s
        self._explicit_expect = expect_status

    @property
    def name(self) -> str:
        return _ORACLE_NAME

    @property
    def enabled(self) -> bool:
        if not http_healthcheck_enabled():
            return False
        return bool(self._resolve_url())

    def _resolve_url(self) -> str:
        if self._explicit_url is not None:
            return self._explicit_url
        return healthcheck_url()

    def _resolve_timeout(self) -> float:
        if self._explicit_timeout is not None:
            return max(
                _TIMEOUT_FLOOR_S,
                min(_TIMEOUT_CEILING_S, float(self._explicit_timeout)),
            )
        return healthcheck_timeout_s()

    def _resolve_expect(self) -> frozenset:
        if self._explicit_expect is not None:
            return self._explicit_expect
        return healthcheck_expect_status()

    async def query_signals(
        self, *, since_ts: float = 0.0,  # noqa: ARG002 -- single-shot
    ) -> Tuple[OracleSignal, ...]:
        try:
            url = self._resolve_url()
            if not url:
                return (_disabled_signal(
                    "JARVIS_PRODUCTION_ORACLE_HTTPCHECK_URL not set",
                ),)
            if not http_healthcheck_enabled():
                return (_disabled_signal(
                    "http_healthcheck_oracle disabled",
                ),)
            timeout_s = self._resolve_timeout()
            expect = self._resolve_expect()
            loop = asyncio.get_event_loop()
            status, reason, err = await loop.run_in_executor(
                None, _do_blocking_get, url, timeout_s,
            )
            now = time.time()
            if status == 0:
                # Network-level failure / timeout -- can't read a
                # status code. Always FAILED with high severity.
                return (OracleSignal(
                    oracle_name=_ORACLE_NAME,
                    kind=OracleKind.HEALTHCHECK,
                    verdict=OracleVerdict.FAILED,
                    observed_at_ts=now,
                    summary=f"GET {url} -> network error",
                    payload={
                        "url": url[:200],
                        "timeout_s": timeout_s,
                        "error": err,
                    },
                    severity=0.85,
                ),)
            if status in expect:
                return (OracleSignal(
                    oracle_name=_ORACLE_NAME,
                    kind=OracleKind.HEALTHCHECK,
                    verdict=OracleVerdict.HEALTHY,
                    observed_at_ts=now,
                    summary=(
                        f"GET {url} -> {status} {reason} "
                        f"(expected)"
                    ),
                    payload={
                        "url": url[:200],
                        "status_code": status,
                        "reason": reason[:100],
                        "timeout_s": timeout_s,
                    },
                    severity=0.1,
                ),)
            verdict, severity = _classify_status(status)
            return (OracleSignal(
                oracle_name=_ORACLE_NAME,
                kind=OracleKind.HEALTHCHECK,
                verdict=verdict,
                observed_at_ts=now,
                summary=(
                    f"GET {url} -> {status} {reason} "
                    f"(expected one of {sorted(expect)})"
                ),
                payload={
                    "url": url[:200],
                    "status_code": status,
                    "reason": reason[:100],
                    "expected_status": sorted(expect),
                    "timeout_s": timeout_s,
                },
                severity=severity,
            ),)
        except Exception:  # noqa: BLE001 -- contract: never raise
            logger.debug(
                "[HTTPHealthCheckOracle] query_signals failed",
                exc_info=True,
            )
            return (_disabled_signal(
                "oracle internal failure",
                {"reason": "query_signals_exception"},
            ),)


def register_shipped_invariants() -> list:
    """Pin: name + enabled + query_signals present; classify_status
    helper present; status classification stays in 5 buckets
    (2xx/3xx/4xx/5xx/other); no exec/eval/compile."""
    import ast as _ast
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    REQUIRED_FUNCS = (
        "http_healthcheck_enabled",
        "healthcheck_url",
        "healthcheck_timeout_s",
        "healthcheck_expect_status",
        "_classify_status",
        "_do_blocking_get",
        "register_shipped_invariants",
    )
    REQUIRED_CLASSES = ("HTTPHealthCheckOracle",)

    def _validate(
        tree: "_ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        seen_funcs: set = set()
        seen_classes: set = set()
        for node in _ast.walk(tree):
            if isinstance(node, _ast.FunctionDef):
                seen_funcs.add(node.name)
            elif isinstance(node, _ast.AsyncFunctionDef):
                seen_funcs.add(node.name)
            elif isinstance(node, _ast.ClassDef):
                seen_classes.add(node.name)
            elif isinstance(node, _ast.Call):
                if isinstance(node.func, _ast.Name):
                    if node.func.id in ("exec", "eval", "compile"):
                        violations.append(
                            f"line {getattr(node, 'lineno', '?')}: "
                            f"http_healthcheck_oracle MUST NOT "
                            f"call {node.func.id}"
                        )
        for fn in REQUIRED_FUNCS:
            if fn not in seen_funcs:
                violations.append(f"missing function {fn!r}")
        for cls in REQUIRED_CLASSES:
            if cls not in seen_classes:
                violations.append(f"missing class {cls!r}")
        return tuple(violations)

    target = (
        "backend/core/ouroboros/governance/http_healthcheck_oracle.py"
    )
    return [
        ShippedCodeInvariant(
            invariant_name="http_healthcheck_oracle_substrate",
            target_file=target,
            description=(
                "Generic HTTP healthcheck oracle: env-config + "
                "_classify_status + _do_blocking_get + class "
                "implementing the Protocol; no dynamic-code calls."
            ),
            validate=_validate,
        ),
    ]


__all__ = [
    "HTTPHealthCheckOracle",
    "http_healthcheck_enabled",
    "healthcheck_url",
    "healthcheck_timeout_s",
    "healthcheck_expect_status",
    "register_shipped_invariants",
]
