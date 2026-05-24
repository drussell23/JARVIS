"""Upstream registry — env composition + credential alignment."""
from __future__ import annotations

import pytest

from backend.core.ouroboros.aegis.credential_registry import (
    upstream_credential_env_vars,
)
from backend.core.ouroboros.aegis.upstream_registry import (
    AuthScheme,
    ENV_AEGIS_UPSTREAM_ANTHROPIC_URL,
    ENV_AEGIS_UPSTREAM_DOUBLEWORD_URL,
    WireFamily,
    endpoint_for_path,
    known_aegis_paths,
    snapshot,
)


def test_known_paths_are_v1():
    """All registered Aegis paths follow the /v1/ convention.
    Slice 2B-i added 5 PASSTHROUGH endpoints + the 2 LLM_COMPLETION
    endpoints from Slice 2A — total 7."""
    paths = known_aegis_paths()
    assert paths == (
        "/v1/batches",
        "/v1/batches/{batch_id}",
        "/v1/chat/completions",
        "/v1/files",
        "/v1/files/{file_id}/content",
        "/v1/messages",
        "/v1/models",
    )


def test_snapshot_returns_all_endpoints():
    reg = snapshot()
    assert set(reg.keys()) == {
        "/v1/messages",
        "/v1/chat/completions",
        "/v1/files",
        "/v1/batches",
        "/v1/batches/{batch_id}",
        "/v1/files/{file_id}/content",
        "/v1/models",
    }


def test_anthropic_endpoint_descriptor_defaults():
    reg = snapshot()
    ep = reg["/v1/messages"]
    from backend.core.ouroboros.aegis.upstream_registry import EndpointKind
    assert ep.wire_family is WireFamily.ANTHROPIC
    assert ep.auth_scheme is AuthScheme.HEADER_RAW
    assert ep.auth_header == "x-api-key"
    assert ep.credential_env_var == "ANTHROPIC_API_KEY"
    assert ep.upstream_base_url.endswith("api.anthropic.com")
    assert ep.kind is EndpointKind.LLM_COMPLETION
    assert ep.http_methods == ("POST",)
    assert ep.upstream_url_for("/v1/messages").endswith("/v1/messages")


def test_doubleword_endpoint_descriptor_defaults():
    reg = snapshot()
    ep = reg["/v1/chat/completions"]
    from backend.core.ouroboros.aegis.upstream_registry import EndpointKind
    assert ep.wire_family is WireFamily.OPENAI_COMPAT
    assert ep.auth_scheme is AuthScheme.HEADER_BEARER
    assert ep.auth_header == "Authorization"
    assert ep.credential_env_var == "DOUBLEWORD_API_KEY"
    assert ep.kind is EndpointKind.LLM_COMPLETION
    assert ep.http_methods == ("POST",)
    assert ep.upstream_url_for("/v1/chat/completions").endswith("/v1/chat/completions")


def test_anthropic_url_env_override(monkeypatch):
    monkeypatch.setenv(ENV_AEGIS_UPSTREAM_ANTHROPIC_URL, "https://my-proxy.example.com")
    reg = snapshot()
    ep = reg["/v1/messages"]
    assert ep.upstream_base_url == "https://my-proxy.example.com"
    assert ep.upstream_url_for("/v1/messages") == "https://my-proxy.example.com/v1/messages"


def test_doubleword_url_env_override_strips_trailing_v1(monkeypatch):
    """DOUBLEWORD_BASE_URL convention in existing code includes /v1.
    The registry strips it so it can re-append via the served path."""
    monkeypatch.setenv("DOUBLEWORD_BASE_URL", "https://my-dw.example.com/v1")
    reg = snapshot()
    ep = reg["/v1/chat/completions"]
    assert ep.upstream_base_url == "https://my-dw.example.com"
    assert ep.upstream_url_for("/v1/chat/completions") == "https://my-dw.example.com/v1/chat/completions"


# ---------------------------------------------------------------------------
# Slice 2B-i: passthrough endpoints
# ---------------------------------------------------------------------------


def test_passthrough_endpoints_all_present():
    from backend.core.ouroboros.aegis.upstream_registry import (
        EndpointKind, passthrough_endpoints,
    )
    pts = passthrough_endpoints()
    assert set(pts.keys()) == {
        "/v1/files",
        "/v1/batches",
        "/v1/batches/{batch_id}",
        "/v1/files/{file_id}/content",
        "/v1/models",
    }
    for ep in pts.values():
        assert ep.kind is EndpointKind.PASSTHROUGH
        assert ep.wire_family is None
        assert ep.credential_env_var == "DOUBLEWORD_API_KEY"
        assert ep.auth_scheme is AuthScheme.HEADER_BEARER


def test_files_endpoint_is_post():
    reg = snapshot()
    assert reg["/v1/files"].http_methods == ("POST",)


def test_batches_create_is_post():
    reg = snapshot()
    assert reg["/v1/batches"].http_methods == ("POST",)


def test_batches_poll_is_get():
    reg = snapshot()
    assert reg["/v1/batches/{batch_id}"].http_methods == ("GET",)


def test_files_content_is_get():
    reg = snapshot()
    assert reg["/v1/files/{file_id}/content"].http_methods == ("GET",)


def test_models_is_get():
    reg = snapshot()
    assert reg["/v1/models"].http_methods == ("GET",)


def test_upstream_url_for_substitutes_concrete_path():
    """Templated path → concrete request path forwarded byte-identically."""
    reg = snapshot()
    ep = reg["/v1/batches/{batch_id}"]
    out = ep.upstream_url_for("/v1/batches/abc-123-xyz")
    assert out.endswith("/v1/batches/abc-123-xyz")


def test_upstream_url_for_preserves_query_string():
    reg = snapshot()
    ep = reg["/v1/models"]
    out = ep.upstream_url_for("/v1/models", query_string="limit=10&offset=0")
    assert out.endswith("/v1/models?limit=10&offset=0")


# ---------------------------------------------------------------------------
# Endpoint kind invariants
# ---------------------------------------------------------------------------


def test_endpoint_kind_closed_taxonomy():
    from backend.core.ouroboros.aegis.upstream_registry import EndpointKind
    assert {k.value for k in EndpointKind} == {"llm_completion", "passthrough"}


def test_llm_endpoint_construction_without_wire_family_raises():
    from backend.core.ouroboros.aegis.upstream_registry import (
        EndpointKind, UpstreamEndpoint,
    )
    with pytest.raises(ValueError, match="wire_family"):
        UpstreamEndpoint(
            aegis_path="/v1/test",
            upstream_base_url="https://x",
            auth_header="x-api-key",
            auth_scheme=AuthScheme.HEADER_RAW,
            credential_env_var="ANTHROPIC_API_KEY",
            kind=EndpointKind.LLM_COMPLETION,
            http_methods=("POST",),
            wire_family=None,  # missing → must raise
        )


def test_passthrough_endpoint_construction_with_wire_family_raises():
    from backend.core.ouroboros.aegis.upstream_registry import (
        EndpointKind, UpstreamEndpoint,
    )
    with pytest.raises(ValueError, match="must NOT declare a wire_family"):
        UpstreamEndpoint(
            aegis_path="/v1/test",
            upstream_base_url="https://x",
            auth_header="Authorization",
            auth_scheme=AuthScheme.HEADER_BEARER,
            credential_env_var="DOUBLEWORD_API_KEY",
            kind=EndpointKind.PASSTHROUGH,
            http_methods=("GET",),
            wire_family=WireFamily.OPENAI_COMPAT,  # passthrough must not set
        )


def test_endpoint_construction_requires_at_least_one_method():
    from backend.core.ouroboros.aegis.upstream_registry import (
        EndpointKind, UpstreamEndpoint,
    )
    with pytest.raises(ValueError, match="at least one HTTP method"):
        UpstreamEndpoint(
            aegis_path="/v1/test",
            upstream_base_url="https://x",
            auth_header="Authorization",
            auth_scheme=AuthScheme.HEADER_BEARER,
            credential_env_var="DOUBLEWORD_API_KEY",
            kind=EndpointKind.PASSTHROUGH,
            http_methods=(),
        )


def test_endpoint_methods_must_be_uppercase():
    from backend.core.ouroboros.aegis.upstream_registry import (
        EndpointKind, UpstreamEndpoint,
    )
    with pytest.raises(ValueError, match="uppercase"):
        UpstreamEndpoint(
            aegis_path="/v1/test",
            upstream_base_url="https://x",
            auth_header="Authorization",
            auth_scheme=AuthScheme.HEADER_BEARER,
            credential_env_var="DOUBLEWORD_API_KEY",
            kind=EndpointKind.PASSTHROUGH,
            http_methods=("get",),  # lowercase → must raise
        )


def test_aegis_specific_doubleword_override_wins(monkeypatch):
    monkeypatch.setenv("DOUBLEWORD_BASE_URL", "https://existing.example.com/v1")
    monkeypatch.setenv(ENV_AEGIS_UPSTREAM_DOUBLEWORD_URL, "https://aegis-specific.example.com")
    reg = snapshot()
    ep = reg["/v1/chat/completions"]
    assert ep.upstream_base_url == "https://aegis-specific.example.com"


def test_endpoint_for_path_lookup():
    assert endpoint_for_path("/v1/messages") is not None
    assert endpoint_for_path("/v1/chat/completions") is not None
    assert endpoint_for_path("/v1/embeddings") is None
    assert endpoint_for_path("/lease/acquire") is None


def test_every_registered_credential_is_in_credential_registry():
    """Boot-time invariant: every endpoint's credential_env_var MUST
    appear in credential_registry, otherwise env_scrub would silently
    fail to strip it from the JARVIS env."""
    reg = snapshot()
    known = upstream_credential_env_vars()
    for ep in reg.values():
        assert ep.credential_env_var in known, (
            f"upstream {ep.aegis_path} uses credential env var "
            f"{ep.credential_env_var} which is not in credential_registry"
        )


def test_default_route_hints_are_known_routes():
    from backend.core.ouroboros.aegis.budget_state_machine import KNOWN_ROUTES
    reg = snapshot()
    for ep in reg.values():
        assert ep.default_route_hint in KNOWN_ROUTES, (
            f"{ep.aegis_path} default_route_hint {ep.default_route_hint!r} "
            f"is not a KNOWN_ROUTES member"
        )


def test_wire_family_closed_taxonomy():
    actual = {v.value for v in WireFamily}
    assert actual == {"anthropic", "openai_compat"}


def test_auth_scheme_closed_taxonomy():
    actual = {v.value for v in AuthScheme}
    assert actual == {"header_raw", "header_bearer"}
