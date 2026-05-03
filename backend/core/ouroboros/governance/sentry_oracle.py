"""backend/core/ouroboros/governance/sentry_oracle.py

Sentry vendor adapter for the Production Oracle substrate.

Implements :class:`production_oracle.ProductionOracleProtocol` against
the Sentry Issues API. Maps unresolved-issue counts to ERROR-kind
OracleSignals with verdict thresholds:

  * 0 unresolved      → HEALTHY (severity 0.1)
  * 1-9 unresolved    → DEGRADED (severity scaled by count)
  * 10-49 unresolved  → FAILED (severity 0.85)
  * 50+ unresolved    → FAILED (severity 0.95 — burst alert)

Configuration (env-driven, NEVER hardcoded):
  * ``SENTRY_AUTH_TOKEN`` -- required; reports DISABLED when unset.
  * ``JARVIS_SENTRY_ORG`` -- organization slug. Required.
  * ``JARVIS_SENTRY_PROJECT`` -- project slug; when unset the query
    is org-wide (matches every project).
  * ``JARVIS_SENTRY_API_BASE`` -- API base URL. Defaults to
    ``https://sentry.io``; operators self-host point at their
    own ``https://sentry.example.com``.
  * ``JARVIS_SENTRY_STATS_PERIOD`` -- query window. Default ``1h``;
    accepts the standard Sentry tokens (``5m``/``1h``/``24h``/``7d``).
  * ``JARVIS_SENTRY_TIMEOUT_S`` -- HTTP timeout. Default 10.0;
    floor 1.0; ceiling 60.0.

Authority invariant: emits ADVISORY signals only. Network failure ->
FAILED signal with structured error payload (NEVER raises). Sentry
auth failures (401/403) -> FAILED signal with the auth detail. The
substrate aggregator handles the rest.
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


_ORACLE_NAME = "sentry"
_DEFAULT_API_BASE = "https://sentry.io"
_DEFAULT_STATS_PERIOD = "1h"
_DEFAULT_TIMEOUT_S = 10.0
_TIMEOUT_FLOOR_S = 1.0
_TIMEOUT_CEILING_S = 60.0
_DEGRADED_THRESHOLD_COUNT = 1
_FAILED_THRESHOLD_COUNT = 10
_BURST_THRESHOLD_COUNT = 50


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


def sentry_oracle_enabled() -> bool:
    return _env_bool("JARVIS_SENTRY_ORACLE_ENABLED", True)


def sentry_auth_token() -> str:
    return os.environ.get("SENTRY_AUTH_TOKEN", "").strip()


def sentry_org() -> str:
    return os.environ.get("JARVIS_SENTRY_ORG", "").strip()


def sentry_project() -> str:
    return os.environ.get("JARVIS_SENTRY_PROJECT", "").strip()


def sentry_api_base() -> str:
    raw = os.environ.get(
        "JARVIS_SENTRY_API_BASE", _DEFAULT_API_BASE,
    ).strip()
    return raw or _DEFAULT_API_BASE


def sentry_stats_period() -> str:
    raw = os.environ.get(
        "JARVIS_SENTRY_STATS_PERIOD", _DEFAULT_STATS_PERIOD,
    ).strip()
    return raw or _DEFAULT_STATS_PERIOD


def sentry_timeout_s() -> float:
    v = _env_float(
        "JARVIS_SENTRY_TIMEOUT_S", _DEFAULT_TIMEOUT_S,
        minimum=_TIMEOUT_FLOOR_S,
    )
    return min(_TIMEOUT_CEILING_S, v)


def _build_query_url(
    api_base: str, org: str, project: str, stats_period: str,
) -> str:
    """Construct the Sentry Issues API URL.

    Project-scoped:
        GET {api_base}/api/0/projects/{org}/{project}/issues/
            ?query=is:unresolved&statsPeriod={stats_period}
    Org-wide (when project unset):
        GET {api_base}/api/0/organizations/{org}/issues/
            ?query=is:unresolved&statsPeriod={stats_period}
    """
    base = (api_base or _DEFAULT_API_BASE).rstrip("/")
    period = stats_period or _DEFAULT_STATS_PERIOD
    if project:
        path = f"/api/0/projects/{org}/{project}/issues/"
    else:
        path = f"/api/0/organizations/{org}/issues/"
    # urllib's quote handles special chars in stats_period (e.g. "1h").
    from urllib.parse import quote
    qs = f"query=is:unresolved&statsPeriod={quote(period)}"
    return f"{base}{path}?{qs}"


def _classify_count(count: int) -> Tuple[OracleVerdict, float]:
    if count <= 0:
        return OracleVerdict.HEALTHY, 0.1
    if count < _FAILED_THRESHOLD_COUNT:
        # 1-9 issues: severity scales linearly between 0.3 and 0.6.
        sev = 0.3 + (count - 1) * (0.3 / 8)
        return OracleVerdict.DEGRADED, min(0.6, sev)
    if count < _BURST_THRESHOLD_COUNT:
        return OracleVerdict.FAILED, 0.85
    return OracleVerdict.FAILED, 0.95


def _do_blocking_get(
    url: str, token: str, timeout_s: float,
) -> Tuple[int, str, list]:
    """Synchronous Sentry HTTP GET. Returns
    ``(status_code, error_summary, issues_list)``.

    Sentry returns a JSON array of issue objects; we don't parse the
    full schema -- just the count for verdict classification. The
    caller may inspect the raw list for structured payload fields
    (top issue title, etc.)."""
    import urllib.request
    import urllib.error
    try:
        req = urllib.request.Request(
            url, method="GET",
            headers={
                "Authorization": f"Bearer {token}",
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
        # Try to read body for error detail (Sentry returns JSON
        # error structure on 4xx/5xx).
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
        oracle_name=_ORACLE_NAME, kind=OracleKind.ERROR,
        verdict=OracleVerdict.DISABLED, observed_at_ts=time.time(),
        summary=reason, payload=payload or {}, severity=0.0,
    )


class SentryOracle:
    """Sentry Issues API adapter implementing
    :class:`ProductionOracleProtocol`.
    """

    def __init__(
        self, *,
        token: Optional[str] = None,
        org: Optional[str] = None,
        project: Optional[str] = None,
        api_base: Optional[str] = None,
        timeout_s: Optional[float] = None,
        stats_period: Optional[str] = None,
    ) -> None:
        # Constructor args take precedence over env; pass None to
        # defer to env at every call (default behavior so operators
        # can hot-swap config without reconstructing the adapter).
        self._explicit_token = token
        self._explicit_org = org
        self._explicit_project = project
        self._explicit_api_base = api_base
        self._explicit_timeout = timeout_s
        self._explicit_stats_period = stats_period

    @property
    def name(self) -> str:
        return _ORACLE_NAME

    @property
    def enabled(self) -> bool:
        if not sentry_oracle_enabled():
            return False
        token = self._resolve_token()
        org = self._resolve_org()
        return bool(token and org)

    def _resolve_token(self) -> str:
        if self._explicit_token is not None:
            return self._explicit_token
        return sentry_auth_token()

    def _resolve_org(self) -> str:
        if self._explicit_org is not None:
            return self._explicit_org
        return sentry_org()

    def _resolve_project(self) -> str:
        if self._explicit_project is not None:
            return self._explicit_project
        return sentry_project()

    def _resolve_api_base(self) -> str:
        if self._explicit_api_base is not None:
            return self._explicit_api_base
        return sentry_api_base()

    def _resolve_timeout(self) -> float:
        if self._explicit_timeout is not None:
            return max(
                _TIMEOUT_FLOOR_S,
                min(_TIMEOUT_CEILING_S, float(self._explicit_timeout)),
            )
        return sentry_timeout_s()

    def _resolve_stats_period(self) -> str:
        if self._explicit_stats_period is not None:
            return self._explicit_stats_period
        return sentry_stats_period()

    async def query_signals(
        self, *, since_ts: float = 0.0,  # noqa: ARG002 -- single-shot
    ) -> Tuple[OracleSignal, ...]:
        try:
            if not sentry_oracle_enabled():
                return (_disabled_signal(
                    "sentry_oracle_enabled=false",
                ),)
            token = self._resolve_token()
            org = self._resolve_org()
            if not token:
                return (_disabled_signal(
                    "SENTRY_AUTH_TOKEN not set",
                ),)
            if not org:
                return (_disabled_signal(
                    "JARVIS_SENTRY_ORG not set",
                ),)
            project = self._resolve_project()
            api_base = self._resolve_api_base()
            stats_period = self._resolve_stats_period()
            timeout_s = self._resolve_timeout()
            url = _build_query_url(
                api_base, org, project, stats_period,
            )
            loop = asyncio.get_event_loop()
            status, err, issues = await loop.run_in_executor(
                None, _do_blocking_get, url, token, timeout_s,
            )
            now = time.time()
            if status == 0:
                return (OracleSignal(
                    oracle_name=_ORACLE_NAME, kind=OracleKind.ERROR,
                    verdict=OracleVerdict.FAILED,
                    observed_at_ts=now,
                    summary=f"Sentry API network error",
                    payload={
                        "url": url[:200],
                        "error": err,
                        "timeout_s": timeout_s,
                    },
                    severity=0.85,
                ),)
            if status == 401 or status == 403:
                return (OracleSignal(
                    oracle_name=_ORACLE_NAME, kind=OracleKind.ERROR,
                    verdict=OracleVerdict.FAILED,
                    observed_at_ts=now,
                    summary=f"Sentry auth failed (HTTP {status})",
                    payload={
                        "url": url[:200],
                        "status": status,
                        "error": err[:200],
                    },
                    severity=0.9,
                ),)
            if status >= 400:
                return (OracleSignal(
                    oracle_name=_ORACLE_NAME, kind=OracleKind.ERROR,
                    verdict=OracleVerdict.FAILED,
                    observed_at_ts=now,
                    summary=f"Sentry API error (HTTP {status})",
                    payload={
                        "url": url[:200],
                        "status": status,
                        "error": err[:200],
                    },
                    severity=0.7,
                ),)
            count = len(issues)
            verdict, severity = _classify_count(count)
            top_title = ""
            try:
                if issues and isinstance(issues[0], dict):
                    top_title = str(issues[0].get("title", ""))[:120]
            except Exception:
                top_title = ""
            return (OracleSignal(
                oracle_name=_ORACLE_NAME, kind=OracleKind.ERROR,
                verdict=verdict,
                observed_at_ts=now,
                summary=(
                    f"Sentry: {count} unresolved issue(s) "
                    f"in last {stats_period}"
                ),
                payload={
                    "url": url[:200],
                    "issue_count": count,
                    "stats_period": stats_period,
                    "org": org,
                    "project": project,
                    "top_title": top_title,
                },
                severity=severity,
            ),)
        except Exception:  # noqa: BLE001 -- contract: never raise
            logger.debug(
                "[SentryOracle] query_signals failed", exc_info=True,
            )
            return (_disabled_signal(
                "oracle internal failure",
                {"reason": "query_signals_exception"},
            ),)


def register_shipped_invariants() -> list:
    """Pin: name + enabled + query_signals + classify_count + URL
    builder + auth-aware blocking GET all present; no exec/eval/compile."""
    import ast as _ast
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    REQUIRED_FUNCS = (
        "sentry_oracle_enabled",
        "sentry_auth_token",
        "sentry_org",
        "_build_query_url",
        "_classify_count",
        "_do_blocking_get",
        "register_shipped_invariants",
    )
    REQUIRED_CLASSES = ("SentryOracle",)

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
                            f"sentry_oracle MUST NOT call "
                            f"{node.func.id}"
                        )
        for fn in REQUIRED_FUNCS:
            if fn not in seen_funcs:
                violations.append(f"missing function {fn!r}")
        for cls in REQUIRED_CLASSES:
            if cls not in seen_classes:
                violations.append(f"missing class {cls!r}")
        return tuple(violations)

    target = "backend/core/ouroboros/governance/sentry_oracle.py"
    return [
        ShippedCodeInvariant(
            invariant_name="sentry_oracle_substrate",
            target_file=target,
            description=(
                "Sentry vendor adapter: env-config + URL builder + "
                "_classify_count + auth-aware HTTP GET + "
                "SentryOracle class implementing ProductionOracle"
                "Protocol; no dynamic-code calls."
            ),
            validate=_validate,
        ),
    ]


__all__ = [
    "SentryOracle",
    "sentry_oracle_enabled",
    "sentry_auth_token",
    "sentry_org",
    "sentry_project",
    "sentry_api_base",
    "sentry_stats_period",
    "sentry_timeout_s",
    "register_shipped_invariants",
]
