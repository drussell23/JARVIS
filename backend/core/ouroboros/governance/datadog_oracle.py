"""backend/core/ouroboros/governance/datadog_oracle.py

Datadog vendor adapter for the Production Oracle substrate.

Implements :class:`production_oracle.ProductionOracleProtocol` against
the Datadog Monitor API. Polls a configurable monitor query (or
all monitors when no query specified) and maps overall_state values
to METRIC-kind OracleSignals:

  * ``OK``       → HEALTHY (severity 0.1)
  * ``Warn``     → DEGRADED (severity 0.55)
  * ``Alert``    → FAILED (severity 0.85)
  * ``No Data``  → DEGRADED (severity 0.4)
  * ``Skipped`` / ``Ignored`` -> HEALTHY (severity 0.1)

Configuration (env-driven, NEVER hardcoded):
  * ``DD_API_KEY`` -- required; reports DISABLED when unset.
  * ``DD_APP_KEY`` -- required (Datadog API requires both).
  * ``JARVIS_DATADOG_MONITOR_QUERY`` -- monitor name/tag query.
    Default empty (queries ALL monitors). Standard Datadog query
    syntax accepted (e.g., ``tag:team:platform``).
  * ``JARVIS_DATADOG_API_BASE`` -- API base URL. Defaults to
    ``https://api.datadoghq.com``; EU operators set to
    ``https://api.datadoghq.eu``.
  * ``JARVIS_DATADOG_TIMEOUT_S`` -- HTTP timeout. Default 10.0.

Authority invariant: emits ADVISORY signals only. NEVER raises.
"""
from __future__ import annotations

import asyncio
import json
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


_ORACLE_NAME = "datadog"
_DEFAULT_API_BASE = "https://api.datadoghq.com"
_DEFAULT_TIMEOUT_S = 10.0
_TIMEOUT_FLOOR_S = 1.0
_TIMEOUT_CEILING_S = 60.0


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, float(raw))
    except (TypeError, ValueError):
        return default


def datadog_oracle_enabled() -> bool:
    return _env_bool("JARVIS_DATADOG_ORACLE_ENABLED", True)


def datadog_api_key() -> str:
    return os.environ.get("DD_API_KEY", "").strip()


def datadog_app_key() -> str:
    return os.environ.get("DD_APP_KEY", "").strip()


def datadog_monitor_query() -> str:
    return os.environ.get(
        "JARVIS_DATADOG_MONITOR_QUERY", "",
    ).strip()


def datadog_api_base() -> str:
    raw = os.environ.get(
        "JARVIS_DATADOG_API_BASE", _DEFAULT_API_BASE,
    ).strip()
    return raw or _DEFAULT_API_BASE


def datadog_timeout_s() -> float:
    v = _env_float(
        "JARVIS_DATADOG_TIMEOUT_S", _DEFAULT_TIMEOUT_S,
        minimum=_TIMEOUT_FLOOR_S,
    )
    return min(_TIMEOUT_CEILING_S, v)


def _build_query_url(api_base: str, monitor_query: str) -> str:
    """Construct the Datadog Monitor API URL.

    GET {api_base}/api/v1/monitor[?monitor_tags={query}]
    """
    base = (api_base or _DEFAULT_API_BASE).rstrip("/")
    path = "/api/v1/monitor"
    if monitor_query:
        from urllib.parse import quote
        return f"{base}{path}?monitor_tags={quote(monitor_query)}"
    return f"{base}{path}"


def _classify_states(states: list) -> Tuple[OracleVerdict, float, dict]:
    """Aggregate per-monitor overall_state values into a single
    verdict + severity + per-state counts payload.

    Decision precedence (first match wins):
      1. Empty input -> INSUFFICIENT_DATA, 0.0
      2. Any "Alert" -> FAILED, 0.85
      3. Any "Warn" -> DEGRADED, 0.55
      4. Any "No Data" -> DEGRADED, 0.4
      5. Otherwise -> HEALTHY, 0.1
    """
    if not states:
        return OracleVerdict.INSUFFICIENT_DATA, 0.0, {}
    counts: dict = {}
    for s in states:
        norm = (str(s) if s is not None else "").strip()
        if not norm:
            norm = "Unknown"
        counts[norm] = counts.get(norm, 0) + 1
    if counts.get("Alert", 0) > 0:
        return OracleVerdict.FAILED, 0.85, counts
    if counts.get("Warn", 0) > 0:
        return OracleVerdict.DEGRADED, 0.55, counts
    if counts.get("No Data", 0) > 0:
        return OracleVerdict.DEGRADED, 0.4, counts
    return OracleVerdict.HEALTHY, 0.1, counts


def _do_blocking_get(
    url: str, api_key: str, app_key: str, timeout_s: float,
) -> Tuple[int, str, list]:
    """Synchronous Datadog HTTP GET. Returns
    ``(status_code, error_summary, monitors_list)``."""
    import urllib.request
    import urllib.error
    try:
        req = urllib.request.Request(
            url, method="GET",
            headers={
                "DD-API-KEY": api_key,
                "DD-APPLICATION-KEY": app_key,
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            status = int(resp.getcode())
            body = resp.read()
            try:
                parsed = json.loads(body)
            except Exception:
                parsed = []
            if not isinstance(parsed, list):
                parsed = []
            return status, "", parsed
    except urllib.error.HTTPError as http_exc:
        try:
            err_body = http_exc.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = ""
        return int(http_exc.code), f"http_{http_exc.code}:{err_body[:200]}", []
    except urllib.error.URLError as url_exc:
        return 0, f"url_error:{url_exc.reason!r}"[:200], []
    except OSError as os_exc:
        return 0, f"os_error:{os_exc!r}"[:200], []
    except Exception as exc:  # noqa: BLE001
        return 0, f"exception:{type(exc).__name__}:{exc}"[:200], []


def _disabled_signal(reason: str, payload: Optional[dict] = None) -> OracleSignal:
    return OracleSignal(
        oracle_name=_ORACLE_NAME, kind=OracleKind.METRIC,
        verdict=OracleVerdict.DISABLED, observed_at_ts=time.time(),
        summary=reason, payload=payload or {}, severity=0.0,
    )


class DatadogOracle:
    """Datadog Monitor API adapter implementing
    :class:`ProductionOracleProtocol`."""

    def __init__(
        self, *,
        api_key: Optional[str] = None,
        app_key: Optional[str] = None,
        monitor_query: Optional[str] = None,
        api_base: Optional[str] = None,
        timeout_s: Optional[float] = None,
    ) -> None:
        self._explicit_api_key = api_key
        self._explicit_app_key = app_key
        self._explicit_monitor_query = monitor_query
        self._explicit_api_base = api_base
        self._explicit_timeout = timeout_s

    @property
    def name(self) -> str:
        return _ORACLE_NAME

    @property
    def enabled(self) -> bool:
        if not datadog_oracle_enabled():
            return False
        return bool(self._resolve_api_key() and self._resolve_app_key())

    def _resolve_api_key(self) -> str:
        if self._explicit_api_key is not None:
            return self._explicit_api_key
        return datadog_api_key()

    def _resolve_app_key(self) -> str:
        if self._explicit_app_key is not None:
            return self._explicit_app_key
        return datadog_app_key()

    def _resolve_monitor_query(self) -> str:
        if self._explicit_monitor_query is not None:
            return self._explicit_monitor_query
        return datadog_monitor_query()

    def _resolve_api_base(self) -> str:
        if self._explicit_api_base is not None:
            return self._explicit_api_base
        return datadog_api_base()

    def _resolve_timeout(self) -> float:
        if self._explicit_timeout is not None:
            return max(
                _TIMEOUT_FLOOR_S,
                min(_TIMEOUT_CEILING_S, float(self._explicit_timeout)),
            )
        return datadog_timeout_s()

    async def query_signals(
        self, *, since_ts: float = 0.0,  # noqa: ARG002 -- single-shot
    ) -> Tuple[OracleSignal, ...]:
        try:
            if not datadog_oracle_enabled():
                return (_disabled_signal(
                    "datadog_oracle_enabled=false",
                ),)
            api_key = self._resolve_api_key()
            app_key = self._resolve_app_key()
            if not api_key:
                return (_disabled_signal(
                    "DD_API_KEY not set",
                ),)
            if not app_key:
                return (_disabled_signal(
                    "DD_APP_KEY not set",
                ),)
            api_base = self._resolve_api_base()
            monitor_query = self._resolve_monitor_query()
            timeout_s = self._resolve_timeout()
            url = _build_query_url(api_base, monitor_query)
            loop = asyncio.get_event_loop()
            status, err, monitors = await loop.run_in_executor(
                None, _do_blocking_get,
                url, api_key, app_key, timeout_s,
            )
            now = time.time()
            if status == 0:
                return (OracleSignal(
                    oracle_name=_ORACLE_NAME, kind=OracleKind.METRIC,
                    verdict=OracleVerdict.FAILED,
                    observed_at_ts=now,
                    summary="Datadog API network error",
                    payload={
                        "url": url[:200],
                        "error": err,
                        "timeout_s": timeout_s,
                    },
                    severity=0.85,
                ),)
            if status == 401 or status == 403:
                return (OracleSignal(
                    oracle_name=_ORACLE_NAME, kind=OracleKind.METRIC,
                    verdict=OracleVerdict.FAILED,
                    observed_at_ts=now,
                    summary=f"Datadog auth failed (HTTP {status})",
                    payload={
                        "url": url[:200],
                        "status": status,
                        "error": err[:200],
                    },
                    severity=0.9,
                ),)
            if status >= 400:
                return (OracleSignal(
                    oracle_name=_ORACLE_NAME, kind=OracleKind.METRIC,
                    verdict=OracleVerdict.FAILED,
                    observed_at_ts=now,
                    summary=f"Datadog API error (HTTP {status})",
                    payload={
                        "url": url[:200],
                        "status": status,
                        "error": err[:200],
                    },
                    severity=0.7,
                ),)
            states = []
            for m in monitors:
                if isinstance(m, dict):
                    states.append(m.get("overall_state", ""))
            verdict, severity, state_counts = _classify_states(states)
            return (OracleSignal(
                oracle_name=_ORACLE_NAME, kind=OracleKind.METRIC,
                verdict=verdict,
                observed_at_ts=now,
                summary=(
                    f"Datadog: {len(monitors)} monitor(s); "
                    f"states={state_counts}"
                ),
                payload={
                    "url": url[:200],
                    "monitor_count": len(monitors),
                    "state_counts": state_counts,
                    "monitor_query": monitor_query,
                },
                severity=severity,
            ),)
        except Exception:  # noqa: BLE001 -- contract: never raise
            logger.debug(
                "[DatadogOracle] query_signals failed", exc_info=True,
            )
            return (_disabled_signal(
                "oracle internal failure",
                {"reason": "query_signals_exception"},
            ),)


def register_shipped_invariants() -> list:
    import ast as _ast
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    REQUIRED_FUNCS = (
        "datadog_oracle_enabled",
        "datadog_api_key",
        "datadog_app_key",
        "_build_query_url",
        "_classify_states",
        "_do_blocking_get",
        "register_shipped_invariants",
    )
    REQUIRED_CLASSES = ("DatadogOracle",)

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
                            f"datadog_oracle MUST NOT call "
                            f"{node.func.id}"
                        )
        for fn in REQUIRED_FUNCS:
            if fn not in seen_funcs:
                violations.append(f"missing function {fn!r}")
        for cls in REQUIRED_CLASSES:
            if cls not in seen_classes:
                violations.append(f"missing class {cls!r}")
        return tuple(violations)

    target = "backend/core/ouroboros/governance/datadog_oracle.py"
    return [
        ShippedCodeInvariant(
            invariant_name="datadog_oracle_substrate",
            target_file=target,
            description=(
                "Datadog vendor adapter: env-config + URL builder + "
                "_classify_states + dual-key auth + DatadogOracle "
                "class implementing ProductionOracleProtocol; no "
                "dynamic-code calls."
            ),
            validate=_validate,
        ),
    ]


__all__ = [
    "DatadogOracle",
    "datadog_oracle_enabled",
    "datadog_api_key",
    "datadog_app_key",
    "datadog_monitor_query",
    "datadog_api_base",
    "datadog_timeout_s",
    "register_shipped_invariants",
]
