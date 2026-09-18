"""Loaded weights are not a new allocation.

`local_model_admission` guards "the act of LOADING model weights" — its own
words — by asking whether the footprint fits in FREE VRAM. Once the model is
loaded those bytes have moved from `free` into `used`, so charging the
footprint again bills the weights against the space they are themselves
occupying. The gate then refuses to USE the model precisely because it is
already there, and the better the lane serves the more certainly it blocks the
next op.

Measured in `bt-2026-09-18-034951`: 42 of 54 failed passes died
`background_dw_blocked_by_topology` with ZERO tokens, each preceded by
`20.0 GiB of weights plus a 1.6 GiB learned margin exceeds the 7.9 GiB free`.
The error named the DoubleWord catalog. The cause was this double count.

Reproduced on the host: idle 31.4 GiB free; after ONE warm-up token, 10.0 GiB
free with `/api/ps` reporting `size_vram=20.3 GiB`. Admission then refused the
resident model and admitted it once charged the incremental cost.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance import candidate_generator as CG

GIB = 1024 ** 3


class _Resp:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def json(self, content_type=None):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Sess:
    def __init__(self, resp):
        self._resp = resp

    def get(self, url):
        assert url.endswith("/api/ps"), url
        return self._resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _patch(monkeypatch, status, payload):
    import types
    fake = types.SimpleNamespace(
        ClientTimeout=lambda **kw: None,
        ClientSession=lambda **kw: _Sess(_Resp(status, payload)),
    )
    monkeypatch.setitem(__import__("sys").modules, "aiohttp", fake)


def _run(ep="http://x:11434"):
    return asyncio.run(CG.fetch_resident_weights(ep))


def test_it_reports_the_resident_model_and_its_vram(monkeypatch):
    _patch(monkeypatch, 200, {"models": [
        {"model": "qwen3-coder-ov:30b", "size": 22 * GIB, "size_vram": 20 * GIB},
    ]})
    assert _run() == ("qwen3-coder-ov:30b", 20 * GIB)


def test_it_credits_size_vram_NOT_size(monkeypatch):
    """`size` includes any part paged to host RAM, which does not relieve VRAM
    pressure and must never be credited against it."""
    _patch(monkeypatch, 200, {"models": [
        {"model": "m", "size": 30 * GIB, "size_vram": 5 * GIB},
    ]})
    assert _run()[1] == 5 * GIB


def test_nothing_resident_is_zero(monkeypatch):
    _patch(monkeypatch, 200, {"models": []})
    assert _run() == ("", 0)


def test_a_model_fully_paged_out_credits_nothing(monkeypatch):
    _patch(monkeypatch, 200, {"models": [
        {"model": "m", "size": 20 * GIB, "size_vram": 0},
    ]})
    assert _run() == ("", 0)


def test_the_largest_resident_model_wins(monkeypatch):
    _patch(monkeypatch, 200, {"models": [
        {"model": "small", "size_vram": 2 * GIB},
        {"model": "big", "size_vram": 18 * GIB},
    ]})
    assert _run() == ("big", 18 * GIB)


@pytest.mark.parametrize("status,payload", [
    (503, {}), (404, {}), (200, None), (200, {"models": "nonsense"}),
    (200, {"models": [None, 7]}),
])
def test_every_unreadable_reply_is_no_evidence(monkeypatch, status, payload):
    """No evidence means charge the full footprint — never assume residency."""
    _patch(monkeypatch, status, payload)
    assert _run() == ("", 0)


def test_an_unreachable_endpoint_never_raises(monkeypatch):
    import types
    def _boom(**kw):
        raise OSError("connection refused")
    monkeypatch.setitem(__import__("sys").modules, "aiohttp",
                        types.SimpleNamespace(ClientTimeout=lambda **k: None,
                                              ClientSession=_boom))
    assert _run() == ("", 0)


def test_it_is_NOT_memoized(monkeypatch):
    """Residency is the one fact here that changes on its own — an idle timer
    evicts, another model loads. A cached 'resident' keeps discounting weights
    that have left the card, and that over-admission ends in an OOM rather
    than a deferral."""
    import inspect
    src = inspect.getsource(CG.fetch_resident_weights)
    assert "_CACHE" not in src
    assert "cached" not in src.split('"""')[-1]

    calls = {"n": 0}
    payloads = [
        {"models": [{"model": "m", "size_vram": 20 * GIB}]},
        {"models": []},
    ]

    class _Seq(_Sess):
        def get(self, url):
            r = _Resp(200, payloads[min(calls["n"], 1)])
            calls["n"] += 1
            return r

    import types
    monkeypatch.setitem(__import__("sys").modules, "aiohttp",
                        types.SimpleNamespace(
                            ClientTimeout=lambda **k: None,
                            ClientSession=lambda **k: _Seq(None)))
    assert _run()[1] == 20 * GIB
    assert _run()[1] == 0          # eviction is observed, not cached away


# --------------------------------------------------------------------------
# The charging rule at the call site
# --------------------------------------------------------------------------

def test_only_the_SERVED_models_residency_is_discounted():
    """If some OTHER model holds the card, serving ours evicts it — the
    footprint really would be allocated, so it is charged in full."""
    import inspect

    from backend.core.ouroboros.governance import providers as P

    src = inspect.getsource(P.PrimeProvider) if hasattr(P, "PrimeProvider") else ""
    if not src:
        pytest.skip("PrimeProvider not exposed for source inspection")
    assert "fetch_resident_weights" in src
    # The discount is gated on the resident name matching the served model.
    assert "_res_name" in src and "_served" in src


def test_the_discount_is_subtractive_not_a_bypass():
    """`max(0, footprint - resident)` keeps a PARTIAL residency honest: a model
    half on the card still has to fit the other half."""
    import inspect

    from backend.core.ouroboros.governance import providers as P

    src = inspect.getsource(P.PrimeProvider) if hasattr(P, "PrimeProvider") else ""
    if not src:
        pytest.skip("PrimeProvider not exposed for source inspection")
    assert "max(0, _weight_bytes - _res_vram)" in src
