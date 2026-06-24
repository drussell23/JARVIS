"""trinity_handshake_suite -- the AUTONOMOUS cross-repo integration test.

Once the air-gapped Trinity is healthy (post health-gate), this drives the REAL
handshake: force jarvis (Body) to ping the newly-MUTATED APIs in reactor
(Nerves) and prime (Mind). A cross-repo mutation that passes unit tests can
still *fracture the organism* -- break a contract the other repos depend on. This
suite catches exactly that, with NO injected fracture: it makes the actual HTTP
calls across container boundaries and inspects the real responses.

Verdict rules (per mutated endpoint):
  * HTTP error (404 / 500 / 4xx / 5xx)         -> ``fracture=True``.
  * transport failure / timeout                -> ``fracture=True``.
  * schema-mismatch (response JSON shape does  -> ``fracture=True``.
    not match the expected contract keys)
  * all endpoints 2xx + schema-OK             -> ``passed=True``.

Every call is bounded by a per-call timeout (no hang). The HTTP boundary is
behind an injectable ``runner`` (same discipline as the gate) so tests script
responses with NO real network.

Design invariants:
  * Fail-CLOSED: any uncertainty (bad status, missing keys, transport error,
    exception) -> FRACTURE. A handshake can never silently pass.
  * Pure verdict logic: ``_classify_response`` is a pure function over
    (status, body, expected_contract) so it is exhaustively unit-testable.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Protocol, Sequence, Tuple

logger = logging.getLogger("Ouroboros.TrinityHandshakeSuite")

_DEFAULT_PER_CALL_TIMEOUT_S = 10.0


# --------------------------------------------------------------------------- #
# HTTP boundary (injectable) -- no real network in tests
# --------------------------------------------------------------------------- #
@dataclass
class HttpResponse:
    """Result of a single HTTP probe.

    ``status == 0`` is the sentinel for a transport-level failure (connection
    refused / timeout / DNS) -- treated as a FRACTURE.
    """

    status: int
    body: Any = None
    error: str = ""

    @property
    def transport_failed(self) -> bool:
        return self.status == 0


class HandshakeHttpRunner(Protocol):
    """Injectable async HTTP boundary.

    Real implementation does an ``aiohttp``/``urllib`` GET (or POST) against a
    container URL; tests inject a fake that returns scripted
    :class:`HttpResponse` objects.
    """

    async def call(
        self, method: str, url: str, *, timeout: float
    ) -> HttpResponse:
        """Perform an HTTP call. NEVER raises for an HTTP error status -- it
        returns the status. It MAY return ``status=0`` to signal a transport
        failure (the suite treats that as a FRACTURE)."""


# --------------------------------------------------------------------------- #
# Mutated-endpoint contract
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class MutatedEndpoint:
    """A newly-mutated API the Body must successfully handshake with.

    ``service`` is "prime"/"reactor"/"jarvis" (selects the base URL).
    ``expected_keys`` is the contract the response JSON MUST satisfy (a
    schema-mismatch = a key in this set absent from the response -> FRACTURE).
    """

    service: str
    method: str
    path: str
    expected_keys: Tuple[str, ...] = ()


@dataclass
class EndpointFailure:
    """A single fractured endpoint."""

    service: str
    path: str
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {"service": self.service, "path": self.path, "reason": self.reason}


@dataclass
class HandshakeResult:
    """Outcome of the autonomous handshake suite."""

    passed: bool
    fracture: bool
    failures: Tuple[EndpointFailure, ...] = ()
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "fracture": self.fracture,
            "failures": [f.to_dict() for f in self.failures],
            "reason": self.reason,
        }


# --------------------------------------------------------------------------- #
# Pure verdict logic
# --------------------------------------------------------------------------- #
def _classify_response(
    resp: HttpResponse, *, expected_keys: Sequence[str]
) -> Tuple[bool, str]:
    """Pure: classify a response -> (ok, reason).

    ``ok=True`` ONLY when the status is 2xx AND every expected contract key is
    present in the (mapping) body. Everything else is a FRACTURE with a reason.
    """
    if resp.transport_failed:
        return False, "transport_failed:%s" % (resp.error or "no_response")
    if not (200 <= int(resp.status) < 300):
        return False, "http_status_%d" % int(resp.status)
    if expected_keys:
        body = resp.body
        if not isinstance(body, Mapping):
            return False, "schema_mismatch:body_not_object"
        missing = [k for k in expected_keys if k not in body]
        if missing:
            return False, "schema_mismatch:missing_keys=%s" % ",".join(sorted(missing))
    return True, "ok"


def _base_url_for(
    service: str, *, jarvis_url: str, prime_url: str, reactor_url: str
) -> Optional[str]:
    return {
        "jarvis": jarvis_url,
        "prime": prime_url,
        "reactor": reactor_url,
    }.get(service)


def _join(base: str, path: str) -> str:
    return base.rstrip("/") + "/" + path.lstrip("/")


# --------------------------------------------------------------------------- #
# The suite
# --------------------------------------------------------------------------- #
async def run_handshake_suite(
    *,
    runner: HandshakeHttpRunner,
    jarvis_url: str,
    prime_url: str,
    reactor_url: str,
    mutated_endpoints: Sequence[MutatedEndpoint],
    per_call_timeout_s: float = _DEFAULT_PER_CALL_TIMEOUT_S,
) -> HandshakeResult:
    """Drive the autonomous cross-repo handshake against the mutated endpoints.

    For each mutated endpoint: HTTP call (bounded by ``per_call_timeout_s``);
    a 404/500/transport-failure/schema-mismatch -> FRACTURE. All-pass ->
    ``passed=True``. NEVER raises (fail-CLOSED: any exception -> FRACTURE).
    """
    # No endpoints to verify is itself suspicious for a cross-repo mutation:
    # fail-CLOSED rather than vacuously pass.
    if not mutated_endpoints:
        return HandshakeResult(
            passed=False,
            fracture=True,
            failures=(),
            reason="no_mutated_endpoints_to_verify",
        )

    failures: list[EndpointFailure] = []
    try:
        for ep in mutated_endpoints:
            base = _base_url_for(
                ep.service,
                jarvis_url=jarvis_url,
                prime_url=prime_url,
                reactor_url=reactor_url,
            )
            if not base:
                failures.append(
                    EndpointFailure(ep.service, ep.path, "unknown_service:%s" % ep.service)
                )
                continue

            url = _join(base, ep.path)
            try:
                resp = await asyncio.wait_for(
                    runner.call(ep.method, url, timeout=per_call_timeout_s),
                    timeout=per_call_timeout_s + 1.0,
                )
            except asyncio.TimeoutError:
                failures.append(
                    EndpointFailure(ep.service, ep.path, "timeout")
                )
                continue
            except Exception as exc:  # transport boundary raised -> FRACTURE
                failures.append(
                    EndpointFailure(ep.service, ep.path, "call_raised:%s" % exc)
                )
                continue

            ok, reason = _classify_response(resp, expected_keys=ep.expected_keys)
            if not ok:
                failures.append(EndpointFailure(ep.service, ep.path, reason))
    except Exception as exc:  # any unexpected control-flow error -> FRACTURE
        logger.warning(
            "[TrinityHandshakeSuite] suite raised -> FRACTURE: %s", exc, exc_info=True
        )
        return HandshakeResult(
            passed=False,
            fracture=True,
            failures=tuple(failures),
            reason="suite_error:%s" % exc,
        )

    if failures:
        return HandshakeResult(
            passed=False,
            fracture=True,
            failures=tuple(failures),
            reason="cross_repo_fracture:%d_endpoint(s)" % len(failures),
        )
    return HandshakeResult(
        passed=True,
        fracture=False,
        failures=(),
        reason="handshake_ok:%d_endpoint(s)" % len(mutated_endpoints),
    )


__all__ = [
    "HttpResponse",
    "HandshakeHttpRunner",
    "MutatedEndpoint",
    "EndpointFailure",
    "HandshakeResult",
    "run_handshake_suite",
]
