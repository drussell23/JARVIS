"""The request may only contain what the installed SDK accepts.

Measured in bt-2026-09-09-024244::

    [ContextExpander] op=op-01a0841b round=1 plan() failed:
        AsyncMessages.create() got an unexpected keyword argument 'temperature'

and one line earlier, the same SDK major from the other side::

    custom http_client rejected by the anthropic SDK (httpx version drift:
    Expected an instance of `httpx2.AsyncClient` ...) — failing SAFE
"""
from __future__ import annotations

import inspect

import pytest

from backend.core.ouroboros.governance import anthropic_sdk_surface as S


@pytest.fixture(autouse=True)
def _fresh():
    S.reset_cache_for_tests()
    yield
    S.reset_cache_for_tests()


# --------------------------------------------------------------------------
# The defect
# --------------------------------------------------------------------------

def test_a_removed_sampling_parameter_is_dropped_not_raised():
    """THE regression: context expansion died on this exact kwarg."""
    allowed = S.supported_params("create")
    if allowed is not None and "temperature" in allowed:
        pytest.skip("this SDK still accepts temperature — nothing to drop")
    clean, dropped = S.sanitize(
        {"model": "m", "max_tokens": 8, "messages": [], "temperature": 0.2},
        method="create",
    )
    assert dropped == ("temperature",)
    assert "temperature" not in clean
    assert clean["model"] == "m" and clean["max_tokens"] == 8


def test_the_real_sdk_would_accept_what_comes_back():
    """Sanitized kwargs bind against the real signature — the end of the bug."""
    from anthropic.resources.messages import AsyncMessages

    clean, _ = S.sanitize(
        {
            "model": "claude-opus-5", "max_tokens": 8, "messages": [],
            "system": "s", "temperature": 0.2, "top_p": 0.9, "top_k": 40,
        },
        method="create",
    )
    # binds cleanly => no "unexpected keyword argument" at call time
    inspect.signature(AsyncMessages.create).bind_partial(None, **clean)


def test_supported_params_are_read_from_the_sdk_not_a_list():
    """A hardcoded removal list encodes today's surface as a constant."""
    src = inspect.getsource(S)
    assert "signature" in src
    assert '"temperature"' not in src.split('"""')[-1], (
        "the removal set must be derived, never enumerated in code"
    )
    allowed = S.supported_params("create")
    assert allowed is None or "messages" in allowed


# --------------------------------------------------------------------------
# Failure direction
# --------------------------------------------------------------------------

def test_an_unreadable_surface_filters_nothing(monkeypatch):
    """Fail OPEN. A shim that cannot read the surface must not start deciding
    what the request contains."""
    monkeypatch.setattr(S, "_resolve", lambda method: None)
    S.reset_cache_for_tests()
    payload = {"model": "m", "temperature": 0.2, "anything": 1}
    clean, dropped = S.sanitize(payload, method="create")
    assert dropped == ()
    assert clean == payload


def test_a_var_keyword_signature_filters_nothing(monkeypatch):
    class _Fake:
        def create(self, **kwargs):
            ...

    import anthropic.resources.messages as M

    monkeypatch.setattr(M, "AsyncMessages", _Fake)
    S.reset_cache_for_tests()
    assert S.supported_params("create") is None


def test_stream_is_resolved_separately_from_create():
    """stream() carries its own signature; sharing one set would send create-only
    parameters to stream and vice versa."""
    c, s = S.supported_params("create"), S.supported_params("stream")
    if c is None or s is None:
        pytest.skip("unfiltered SDK surface")
    assert c != s


def test_the_drop_is_reported_to_the_caller():
    """A request whose sampling was discarded is not the request the caller
    described — the caller has to be able to know."""
    allowed = S.supported_params("create")
    if allowed is not None and "temperature" in allowed:
        pytest.skip("this SDK still accepts temperature")
    _, dropped = S.sanitize({"temperature": 1.0}, method="create")
    assert dropped, "a silent drop is indistinguishable from an honoured request"


def test_warning_is_emitted_once_per_parameter(caplog):
    allowed = S.supported_params("create")
    if allowed is not None and "temperature" in allowed:
        pytest.skip("this SDK still accepts temperature")
    with caplog.at_level("WARNING"):
        for _ in range(5):
            S.sanitize({"temperature": 0.2}, method="create")
    hits = [r for r in caplog.records if "temperature" in r.getMessage()]
    assert len(hits) == 1, f"structural drop warned {len(hits)} times"


# --------------------------------------------------------------------------
# The transport half of the same SDK major
# --------------------------------------------------------------------------

def test_the_transport_config_reaches_the_sdks_own_client():
    """The custom http_client used to be rejected on every boot, so the whole
    Transport Resilience Layer was discarded and nobody saw it."""
    anthropic = pytest.importorskip("anthropic")
    import httpx

    from backend.core.ouroboros.governance.providers import _resolve_sdk_http_module

    mod = _resolve_sdk_http_module(anthropic, httpx)
    assert hasattr(mod, "Timeout") and hasattr(mod, "Limits")
    cls = getattr(anthropic, "DefaultAsyncHttpxClient", mod.AsyncClient)
    client = cls(
        timeout=mod.Timeout(connect=10, read=600, write=600, pool=600),
        limits=mod.Limits(
            max_connections=10, max_keepalive_connections=5, keepalive_expiry=30,
        ),
    )
    # The assertion is that this does NOT raise TypeError.
    anthropic.AsyncAnthropic(api_key="x", http_client=client, max_retries=0)


def test_the_http_module_is_derived_not_named():
    from backend.core.ouroboros.governance import providers as P

    src = inspect.getsource(P._resolve_sdk_http_module)
    assert "httpx2" not in src, "the vendored module name must not be hardcoded"
