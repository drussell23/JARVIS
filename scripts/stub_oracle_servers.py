#!/usr/bin/env python3
"""Local stub HTTP servers for Sentry + Datadog vendor APIs.

Provides controlled vendor responses for end-to-end testing of
:class:`SentryOracle` + :class:`DatadogOracle` against the real
``urllib.request`` code path WITHOUT requiring real Sentry/Datadog
tokens or network egress.

Two stdlib ``http.server`` instances:

  * ``SentryStubServer`` -- serves ``/api/0/projects/{org}/{project}
    /issues/`` and ``/api/0/organizations/{org}/issues/`` with a
    configurable issue-list response. Auth header (``Authorization:
    Bearer <token>``) is validated when a token is configured;
    invalid tokens return 401.
  * ``DatadogStubServer`` -- serves ``/api/v1/monitor`` with a
    configurable monitor-list response. Auth headers (``DD-API-KEY``
    + ``DD-APPLICATION-KEY``) are validated when keys are configured.

Both servers run in background threads on dynamically-allocated
ports (port 0 → OS picks). Tests connect via the configured base
URL pointed at the stub (``JARVIS_SENTRY_API_BASE`` /
``JARVIS_DATADOG_API_BASE``).

Usage as test harness::

    python3 scripts/stub_oracle_servers.py
    # Runs the integration test suite against the stubs and exits.

Usage as library::

    from scripts.stub_oracle_servers import SentryStubServer
    with SentryStubServer(issues=[...], expected_token="abc") as base:
        # base = "http://127.0.0.1:54321"
        ...

Authority invariant: this script is TEST INFRASTRUCTURE only --
runs in the operator's local env, never imported by production code,
never reaches real vendor endpoints. Pure stdlib (http.server,
threading, json).
"""
from __future__ import annotations

import asyncio
import http.server
import json
import os
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Sentry stub
# ---------------------------------------------------------------------------


class _SentryHandler(http.server.BaseHTTPRequestHandler):
    """Per-request handler. Reads the parent server's configured
    issues + expected_token via the server attribute set by
    :class:`SentryStubServer.__init__`."""

    def log_message(self, format, *args):  # noqa: A002, ARG002
        # Suppress default logging -- test output should be clean.
        pass

    def do_GET(self):  # noqa: N802
        srv = self.server  # type: ignore[assignment]
        # Token check: when expected_token configured, require Bearer.
        if srv.expected_token:  # type: ignore[attr-defined]
            auth = self.headers.get("Authorization", "")
            if auth != f"Bearer {srv.expected_token}":  # type: ignore[attr-defined]
                self._reply(401, {"detail": "auth required"})
                return
        # Path routing -- match issues endpoints.
        if (
            "/api/0/projects/" in self.path
            and self.path.endswith("/issues/")
            or "/api/0/organizations/" in self.path
            and "/issues/" in self.path
        ) or "/issues/" in self.path:
            self._reply(200, srv.issues)  # type: ignore[attr-defined]
            return
        self._reply(404, {"detail": "not found"})

    def _reply(self, status: int, body) -> None:
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class SentryStubServer:
    """Background-thread Sentry stub. Use as context manager.

    The ``base_url`` returned by ``__enter__`` is the URL operators
    configure as ``JARVIS_SENTRY_API_BASE`` to point ``SentryOracle``
    at this stub.
    """

    def __init__(
        self,
        *,
        issues: Optional[List[dict]] = None,
        expected_token: str = "",
    ) -> None:
        self.issues: List[dict] = list(issues or [])
        self.expected_token: str = expected_token
        self._server: Optional[http.server.HTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.base_url: str = ""

    def __enter__(self) -> str:
        self._server = http.server.HTTPServer(
            ("127.0.0.1", 0), _SentryHandler,
        )
        # Attach our config to the server instance so the handler
        # can read it.
        self._server.issues = self.issues  # type: ignore[attr-defined]
        self._server.expected_token = self.expected_token  # type: ignore[attr-defined]
        host, port = self._server.server_address[:2]
        self.base_url = f"http://{host}:{port}"
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
        )
        self._thread.start()
        return self.base_url

    def __exit__(self, *args) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


# ---------------------------------------------------------------------------
# Datadog stub
# ---------------------------------------------------------------------------


class _DatadogHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002, ARG002
        pass

    def do_GET(self):  # noqa: N802
        srv = self.server  # type: ignore[assignment]
        # Dual-key auth check.
        if srv.expected_api_key:  # type: ignore[attr-defined]
            api_key = self.headers.get("DD-API-KEY", "")
            if api_key != srv.expected_api_key:  # type: ignore[attr-defined]
                self._reply(401, {"errors": ["api key invalid"]})
                return
        if srv.expected_app_key:  # type: ignore[attr-defined]
            app_key = self.headers.get("DD-APPLICATION-KEY", "")
            if app_key != srv.expected_app_key:  # type: ignore[attr-defined]
                self._reply(403, {"errors": ["app key invalid"]})
                return
        if "/api/v1/monitor" in self.path:
            self._reply(200, srv.monitors)  # type: ignore[attr-defined]
            return
        self._reply(404, {"errors": ["not found"]})

    def _reply(self, status: int, body) -> None:
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class DatadogStubServer:
    def __init__(
        self,
        *,
        monitors: Optional[List[dict]] = None,
        expected_api_key: str = "",
        expected_app_key: str = "",
    ) -> None:
        self.monitors: List[dict] = list(monitors or [])
        self.expected_api_key: str = expected_api_key
        self.expected_app_key: str = expected_app_key
        self._server: Optional[http.server.HTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.base_url: str = ""

    def __enter__(self) -> str:
        self._server = http.server.HTTPServer(
            ("127.0.0.1", 0), _DatadogHandler,
        )
        self._server.monitors = self.monitors  # type: ignore[attr-defined]
        self._server.expected_api_key = self.expected_api_key  # type: ignore[attr-defined]
        self._server.expected_app_key = self.expected_app_key  # type: ignore[attr-defined]
        host, port = self._server.server_address[:2]
        self.base_url = f"http://{host}:{port}"
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
        )
        self._thread.start()
        return self.base_url

    def __exit__(self, *args) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


# ---------------------------------------------------------------------------
# Integration test harness (runs as the script's main)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TestVerdict:
    name: str
    passed: bool
    evidence: str
    details: Dict[str, object] = field(default_factory=dict)


def _set_env(**kwargs) -> Dict[str, Optional[str]]:
    """Set env vars + return previous values for restore."""
    prev: Dict[str, Optional[str]] = {}
    for k, v in kwargs.items():
        prev[k] = os.environ.get(k)
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = str(v)
    return prev


def _restore_env(prev: Dict[str, Optional[str]]) -> None:
    for k, v in prev.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _run_sentry_test(
    *, issues: List[dict], expected_token: str, expected_verdict: str,
    expected_kind: str = "error",
) -> TestVerdict:
    from backend.core.ouroboros.governance.sentry_oracle import (
        SentryOracle,
    )
    with SentryStubServer(
        issues=issues, expected_token=expected_token,
    ) as base_url:
        prev = _set_env(
            SENTRY_AUTH_TOKEN=expected_token,
            JARVIS_SENTRY_ORG="testorg",
            JARVIS_SENTRY_PROJECT="testproject",
            JARVIS_SENTRY_API_BASE=base_url,
            JARVIS_SENTRY_TIMEOUT_S="5.0",
        )
        try:
            oracle = SentryOracle()
            sigs = asyncio.run(oracle.query_signals())
        finally:
            _restore_env(prev)
    if not sigs:
        return TestVerdict(
            name=f"sentry: {len(issues)} issues -> {expected_verdict}",
            passed=False, evidence="zero signals returned",
        )
    sig = sigs[0]
    ok = (
        sig.verdict.value == expected_verdict
        and sig.kind.value == expected_kind
    )
    return TestVerdict(
        name=f"sentry: {len(issues)} issues -> {expected_verdict}",
        passed=ok,
        evidence=(
            f"got verdict={sig.verdict.value} kind={sig.kind.value} "
            f"sev={sig.severity:.2f} summary={sig.summary[:80]!r}"
        ),
    )


def _run_datadog_test(
    *, monitors: List[dict], expected_api_key: str,
    expected_app_key: str, expected_verdict: str,
    expected_kind: str = "metric",
) -> TestVerdict:
    from backend.core.ouroboros.governance.datadog_oracle import (
        DatadogOracle,
    )
    with DatadogStubServer(
        monitors=monitors,
        expected_api_key=expected_api_key,
        expected_app_key=expected_app_key,
    ) as base_url:
        prev = _set_env(
            DD_API_KEY=expected_api_key,
            DD_APP_KEY=expected_app_key,
            JARVIS_DATADOG_API_BASE=base_url,
            JARVIS_DATADOG_TIMEOUT_S="5.0",
            JARVIS_DATADOG_MONITOR_QUERY="",
        )
        try:
            oracle = DatadogOracle()
            sigs = asyncio.run(oracle.query_signals())
        finally:
            _restore_env(prev)
    if not sigs:
        return TestVerdict(
            name=f"datadog: {len(monitors)} monitors -> {expected_verdict}",
            passed=False, evidence="zero signals returned",
        )
    sig = sigs[0]
    ok = (
        sig.verdict.value == expected_verdict
        and sig.kind.value == expected_kind
    )
    return TestVerdict(
        name=f"datadog: {len(monitors)} monitors -> {expected_verdict}",
        passed=ok,
        evidence=(
            f"got verdict={sig.verdict.value} kind={sig.kind.value} "
            f"sev={sig.severity:.2f} summary={sig.summary[:80]!r}"
        ),
    )


def _run_sentry_auth_failure_test() -> TestVerdict:
    """Stub validates token; oracle sends WRONG token; expect FAILED."""
    from backend.core.ouroboros.governance.sentry_oracle import (
        SentryOracle,
    )
    with SentryStubServer(
        issues=[], expected_token="correct-token",
    ) as base_url:
        prev = _set_env(
            SENTRY_AUTH_TOKEN="WRONG-TOKEN",
            JARVIS_SENTRY_ORG="testorg",
            JARVIS_SENTRY_API_BASE=base_url,
            JARVIS_SENTRY_TIMEOUT_S="5.0",
        )
        try:
            sigs = asyncio.run(SentryOracle().query_signals())
        finally:
            _restore_env(prev)
    sig = sigs[0]
    ok = (
        sig.verdict.value == "failed"
        and "auth" in sig.summary.lower()
    )
    return TestVerdict(
        name="sentry: auth failure (wrong token) -> FAILED",
        passed=ok,
        evidence=(
            f"verdict={sig.verdict.value} sev={sig.severity:.2f} "
            f"summary={sig.summary[:80]!r}"
        ),
    )


def _run_datadog_auth_failure_test() -> TestVerdict:
    from backend.core.ouroboros.governance.datadog_oracle import (
        DatadogOracle,
    )
    with DatadogStubServer(
        monitors=[],
        expected_api_key="correct-api",
        expected_app_key="correct-app",
    ) as base_url:
        prev = _set_env(
            DD_API_KEY="WRONG-API",
            DD_APP_KEY="correct-app",
            JARVIS_DATADOG_API_BASE=base_url,
            JARVIS_DATADOG_TIMEOUT_S="5.0",
        )
        try:
            sigs = asyncio.run(DatadogOracle().query_signals())
        finally:
            _restore_env(prev)
    sig = sigs[0]
    ok = (
        sig.verdict.value == "failed"
        and "auth" in sig.summary.lower()
    )
    return TestVerdict(
        name="datadog: auth failure (wrong api key) -> FAILED",
        passed=ok,
        evidence=(
            f"verdict={sig.verdict.value} sev={sig.severity:.2f} "
            f"summary={sig.summary[:80]!r}"
        ),
    )


def main() -> int:
    print("Stub vendor server integration tests")
    print()
    tests: List[TestVerdict] = []

    # --- Sentry happy paths ---
    tests.append(_run_sentry_test(
        issues=[], expected_token="t1", expected_verdict="healthy",
    ))
    tests.append(_run_sentry_test(
        issues=[{"title": "TypeError"}, {"title": "ValueError"}],
        expected_token="t2", expected_verdict="degraded",
    ))
    tests.append(_run_sentry_test(
        issues=[{"title": f"err{i}"} for i in range(15)],
        expected_token="t3", expected_verdict="failed",
    ))
    tests.append(_run_sentry_test(
        issues=[{"title": f"err{i}"} for i in range(60)],
        expected_token="t4", expected_verdict="failed",
    ))

    # --- Sentry auth failure ---
    tests.append(_run_sentry_auth_failure_test())

    # --- Datadog happy paths ---
    tests.append(_run_datadog_test(
        monitors=[
            {"overall_state": "OK"},
            {"overall_state": "OK"},
        ],
        expected_api_key="api1", expected_app_key="app1",
        expected_verdict="healthy",
    ))
    tests.append(_run_datadog_test(
        monitors=[
            {"overall_state": "OK"},
            {"overall_state": "Warn"},
            {"overall_state": "OK"},
        ],
        expected_api_key="api2", expected_app_key="app2",
        expected_verdict="degraded",
    ))
    tests.append(_run_datadog_test(
        monitors=[
            {"overall_state": "Alert"},
            {"overall_state": "OK"},
        ],
        expected_api_key="api3", expected_app_key="app3",
        expected_verdict="failed",
    ))
    tests.append(_run_datadog_test(
        monitors=[
            {"overall_state": "No Data"},
            {"overall_state": "OK"},
        ],
        expected_api_key="api4", expected_app_key="app4",
        expected_verdict="degraded",
    ))

    # --- Datadog auth failure ---
    tests.append(_run_datadog_auth_failure_test())

    for v in tests:
        mark = "PASS" if v.passed else "FAIL"
        print(f"  [{mark}] {v.name}")
        print(f"         {v.evidence}")
    print()
    passed = sum(1 for v in tests if v.passed)
    if passed == len(tests):
        print(f"VERDICT: stub-server integration EMPIRICALLY CLOSED "
              f"-- all {passed}/{len(tests)} tests PASSED. "
              f"Sentry + Datadog adapters work end-to-end against "
              f"controlled vendor responses.")
        return 0
    print(f"VERDICT: {len(tests) - passed}/{len(tests)} tests FAILED.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
