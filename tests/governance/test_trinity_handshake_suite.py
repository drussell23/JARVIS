"""Tests for the autonomous Trinity handshake suite (G2).

NO real network. A fake HTTP runner scripts responses keyed by URL.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.saga.trinity_handshake_suite import (
    HttpResponse,
    MutatedEndpoint,
    run_handshake_suite,
)

_J = "http://jarvis:8091"
_P = "http://prime:8000"
_R = "http://reactor:8090"


class FakeHttp:
    """Scripts (status, body) per URL suffix; default 200 {ok:true}."""

    def __init__(self, script=None, raise_on=None):
        self._script = script or {}
        self._raise_on = raise_on or set()
        self.calls = []

    async def call(self, method, url, *, timeout):
        self.calls.append((method, url, timeout))
        for suffix in self._raise_on:
            if url.endswith(suffix):
                raise RuntimeError("boom:%s" % suffix)
        for suffix, resp in self._script.items():
            if url.endswith(suffix):
                return resp
        return HttpResponse(status=200, body={"ok": True})


async def _run(endpoints, http):
    return await run_handshake_suite(
        runner=http,
        jarvis_url=_J,
        prime_url=_P,
        reactor_url=_R,
        mutated_endpoints=endpoints,
        per_call_timeout_s=1.0,
    )


@pytest.mark.asyncio
async def test_all_200_correct_schema_passes():
    eps = [
        MutatedEndpoint("reactor", "GET", "/metrics", ("count", "ts")),
        MutatedEndpoint("prime", "GET", "/model/ready", ("ready",)),
    ]
    http = FakeHttp(
        {
            "/metrics": HttpResponse(200, {"count": 1, "ts": 2}),
            "/model/ready": HttpResponse(200, {"ready": True}),
        }
    )
    res = await _run(eps, http)
    assert res.passed and not res.fracture
    assert not res.failures


@pytest.mark.asyncio
async def test_404_fractures():
    eps = [MutatedEndpoint("reactor", "GET", "/gone")]
    http = FakeHttp({"/gone": HttpResponse(404, {"error": "x"})})
    res = await _run(eps, http)
    assert res.fracture and not res.passed
    assert res.failures[0].reason == "http_status_404"


@pytest.mark.asyncio
async def test_500_fractures():
    eps = [MutatedEndpoint("prime", "GET", "/boom")]
    http = FakeHttp({"/boom": HttpResponse(500, {"error": "x"})})
    res = await _run(eps, http)
    assert res.fracture
    assert res.failures[0].reason == "http_status_500"


@pytest.mark.asyncio
async def test_schema_mismatch_fractures():
    eps = [MutatedEndpoint("reactor", "GET", "/metrics", ("count", "ts"))]
    # Missing 'ts' key -> contract violation.
    http = FakeHttp({"/metrics": HttpResponse(200, {"count": 1})})
    res = await _run(eps, http)
    assert res.fracture
    assert "schema_mismatch" in res.failures[0].reason
    assert "ts" in res.failures[0].reason


@pytest.mark.asyncio
async def test_body_not_object_fractures():
    eps = [MutatedEndpoint("prime", "GET", "/x", ("ready",))]
    http = FakeHttp({"/x": HttpResponse(200, "not-a-dict")})
    res = await _run(eps, http)
    assert res.fracture
    assert "body_not_object" in res.failures[0].reason


@pytest.mark.asyncio
async def test_transport_failure_fractures():
    eps = [MutatedEndpoint("reactor", "GET", "/x")]
    http = FakeHttp({"/x": HttpResponse(status=0, error="conn_refused")})
    res = await _run(eps, http)
    assert res.fracture
    assert "transport_failed" in res.failures[0].reason


@pytest.mark.asyncio
async def test_call_raising_is_caught_as_fracture():
    eps = [MutatedEndpoint("prime", "GET", "/x")]
    http = FakeHttp(raise_on={"/x"})
    res = await _run(eps, http)
    assert res.fracture
    assert "call_raised" in res.failures[0].reason


@pytest.mark.asyncio
async def test_unknown_service_fractures():
    eps = [MutatedEndpoint("ghost", "GET", "/x")]
    http = FakeHttp()
    res = await _run(eps, http)
    assert res.fracture
    assert "unknown_service" in res.failures[0].reason


@pytest.mark.asyncio
async def test_no_endpoints_fail_closed():
    http = FakeHttp()
    res = await _run([], http)
    assert res.fracture and not res.passed
    assert "no_mutated_endpoints" in res.reason


@pytest.mark.asyncio
async def test_bounded_each_call_gets_timeout():
    eps = [MutatedEndpoint("reactor", "GET", "/x")]
    http = FakeHttp()
    await _run(eps, http)
    # The per-call timeout was forwarded to the runner.
    assert http.calls[0][2] == 1.0
