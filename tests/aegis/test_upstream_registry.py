"""Upstream registry — env composition + credential alignment."""
from __future__ import annotations

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
    paths = known_aegis_paths()
    assert paths == ("/v1/chat/completions", "/v1/messages")


def test_snapshot_returns_both_endpoints():
    reg = snapshot()
    assert set(reg.keys()) == {"/v1/messages", "/v1/chat/completions"}


def test_anthropic_endpoint_descriptor_defaults():
    reg = snapshot()
    ep = reg["/v1/messages"]
    assert ep.wire_family is WireFamily.ANTHROPIC
    assert ep.auth_scheme is AuthScheme.HEADER_RAW
    assert ep.auth_header == "x-api-key"
    assert ep.credential_env_var == "ANTHROPIC_API_KEY"
    assert ep.upstream_base_url.endswith("api.anthropic.com")
    assert ep.upstream_url().endswith("/v1/messages")


def test_doubleword_endpoint_descriptor_defaults():
    reg = snapshot()
    ep = reg["/v1/chat/completions"]
    assert ep.wire_family is WireFamily.OPENAI_COMPAT
    assert ep.auth_scheme is AuthScheme.HEADER_BEARER
    assert ep.auth_header == "Authorization"
    assert ep.credential_env_var == "DOUBLEWORD_API_KEY"
    assert ep.upstream_url().endswith("/v1/chat/completions")


def test_anthropic_url_env_override(monkeypatch):
    monkeypatch.setenv(ENV_AEGIS_UPSTREAM_ANTHROPIC_URL, "https://my-proxy.example.com")
    reg = snapshot()
    ep = reg["/v1/messages"]
    assert ep.upstream_base_url == "https://my-proxy.example.com"
    assert ep.upstream_url() == "https://my-proxy.example.com/v1/messages"


def test_doubleword_url_env_override_strips_trailing_v1(monkeypatch):
    """DOUBLEWORD_BASE_URL convention in existing code includes /v1.
    The registry strips it so it can re-append via the served path."""
    monkeypatch.setenv("DOUBLEWORD_BASE_URL", "https://my-dw.example.com/v1")
    reg = snapshot()
    ep = reg["/v1/chat/completions"]
    assert ep.upstream_base_url == "https://my-dw.example.com"
    assert ep.upstream_url() == "https://my-dw.example.com/v1/chat/completions"


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
