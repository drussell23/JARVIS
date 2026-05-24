"""Upstream endpoint registry — single source of truth for forwarding.

Maps each Aegis-side method+path Aegis serves to its upstream descriptor.

Two endpoint **kinds** (closed taxonomy, AST-pinned):

  * ``LLM_COMPLETION`` — Lease-required; body parsed for usage; budget
    reconciled authoritatively by Aegis on stream end / response body.
    Examples: ``POST /v1/messages``, ``POST /v1/chat/completions``.

  * ``PASSTHROUGH`` — Session-token authenticated; transparent body
    pass-through; NO usage parsing, NO budget reconcile. Used for
    non-LLM API operations where JARVIS still needs to talk to the
    upstream but the call doesn't burn tokens (file uploads, batch
    management, model listing).

Per binding directives (Slice 2B-i):
  * Closed allowlist — Aegis is NOT an open proxy.
  * Only the explicit (method, path) pairs in :func:`_build_registry`
    are served; unknown ``/v1/*`` returns 404.
  * Aegis injects the real upstream credential; JARVIS-side auth
    (lease/session/internal) is stripped before forwarding.
  * Multipart bodies (``POST /v1/files``) pass through byte-identically.

Adding a new upstream surface requires:
  1. Adding an :class:`UpstreamEndpoint` to ``_build_registry``
  2. Adding its ``credential_env_var`` to
     :mod:`backend.core.ouroboros.aegis.credential_registry`
  3. (LLM_COMPLETION only) Wire-family usage parser in
     :mod:`backend.core.ouroboros.aegis.forwarding`
"""
from __future__ import annotations

import enum
import os
from dataclasses import dataclass
from typing import Mapping, Optional, Tuple

from backend.core.ouroboros.aegis.credential_registry import (
    upstream_credential_env_vars,
)


UPSTREAM_REGISTRY_SCHEMA_VERSION: str = "aegis_upstream_registry.2"


class WireFamily(str, enum.Enum):
    """Closed 2-value taxonomy of upstream wire shapes for LLM completion."""

    ANTHROPIC = "anthropic"          # /v1/messages, SSE with `usage` in message_start + message_delta
    OPENAI_COMPAT = "openai_compat"  # /v1/chat/completions, SSE with `usage` in final delta


class AuthScheme(str, enum.Enum):
    """How the upstream expects the credential to be presented."""

    HEADER_RAW = "header_raw"             # Header value IS the raw key (e.g. x-api-key)
    HEADER_BEARER = "header_bearer"       # Header value is `Bearer <key>`


class EndpointKind(str, enum.Enum):
    """Closed 2-value taxonomy of how Aegis treats the endpoint.

    - LLM_COMPLETION: lease-gated, usage-parsed, budget-reconciled
    - PASSTHROUGH:    session-gated, transparent, no budget impact
    """

    LLM_COMPLETION = "llm_completion"
    PASSTHROUGH = "passthrough"


# ---------------------------------------------------------------------------
# Env knobs (single seam)
# ---------------------------------------------------------------------------

ENV_AEGIS_UPSTREAM_ANTHROPIC_URL: str = "JARVIS_AEGIS_UPSTREAM_ANTHROPIC_URL"
ENV_AEGIS_UPSTREAM_DOUBLEWORD_URL: str = "JARVIS_AEGIS_UPSTREAM_DOUBLEWORD_URL"

_ANTHROPIC_DEFAULT_BASE_URL: str = "https://api.anthropic.com"
_DOUBLEWORD_DEFAULT_BASE_URL: str = "https://api.doubleword.ai"


def _resolve_anthropic_base_url() -> str:
    return (
        os.environ.get(ENV_AEGIS_UPSTREAM_ANTHROPIC_URL, "").strip()
        or _ANTHROPIC_DEFAULT_BASE_URL
    )


def _resolve_doubleword_base_url() -> str:
    """Resolve the DW base URL.

    Priority: Aegis-specific override > existing DOUBLEWORD_BASE_URL > default.
    DOUBLEWORD_BASE_URL in the rest of the codebase already includes ``/v1``
    (see doubleword_provider.py:44). Aegis's upstream base URL should be the
    SCHEME+HOST only — we strip the /v1 suffix if present so we can re-add
    it via the path being served (``/v1/chat/completions``)."""
    override = os.environ.get(ENV_AEGIS_UPSTREAM_DOUBLEWORD_URL, "").strip()
    if override:
        return override.rstrip("/")
    existing = os.environ.get("DOUBLEWORD_BASE_URL", "").strip()
    if existing:
        stripped = existing.rstrip("/")
        if stripped.endswith("/v1"):
            stripped = stripped[: -len("/v1")]
        return stripped
    return _DOUBLEWORD_DEFAULT_BASE_URL


# ---------------------------------------------------------------------------
# Endpoint descriptor
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UpstreamEndpoint:
    """Frozen descriptor of one upstream Aegis can forward to.

    ``aegis_path`` can be a literal path (``/v1/messages``) or an aiohttp
    route template with placeholders (``/v1/batches/{batch_id}``). For
    template paths, the path parameter is substituted from
    ``request.match_info`` into ``request.path`` at forward time — no
    template engine here; the path Aegis serves is the path Aegis forwards.

    For LLM_COMPLETION endpoints, ``wire_family`` MUST be set (the
    forwarder dispatches its usage parser by family). For PASSTHROUGH
    endpoints, ``wire_family`` MUST be None.
    """

    aegis_path: str
    upstream_base_url: str
    auth_header: str
    auth_scheme: AuthScheme
    credential_env_var: str
    kind: EndpointKind
    http_methods: Tuple[str, ...]
    # LLM_COMPLETION-only fields:
    wire_family: Optional[WireFamily] = None
    default_route_hint: str = "STANDARD"

    def __post_init__(self) -> None:
        # Frozen-dataclass-compatible validation — fail loud at construction.
        if self.kind is EndpointKind.LLM_COMPLETION and self.wire_family is None:
            raise ValueError(
                f"LLM_COMPLETION endpoint {self.aegis_path} must declare a "
                f"wire_family (got None)"
            )
        if self.kind is EndpointKind.PASSTHROUGH and self.wire_family is not None:
            raise ValueError(
                f"PASSTHROUGH endpoint {self.aegis_path} must NOT declare a "
                f"wire_family (got {self.wire_family}). Passthrough endpoints "
                f"don't parse usage."
            )
        if not self.http_methods:
            raise ValueError(
                f"endpoint {self.aegis_path} must declare at least one HTTP method"
            )
        for m in self.http_methods:
            if m != m.upper() or not m.isascii():
                raise ValueError(
                    f"endpoint {self.aegis_path} method {m!r} must be uppercase ASCII"
                )

    def upstream_url_for(self, request_path: str, query_string: str = "") -> str:
        """Compose the outbound URL for a given inbound request path
        (and optional query string). For templated paths
        (``/v1/batches/{batch_id}``), ``request_path`` is the concrete
        path the client requested (``/v1/batches/abc123``) — aiohttp
        already matched the template; we just forward the concrete
        path byte-identically."""
        url = f"{self.upstream_base_url.rstrip('/')}{request_path}"
        if query_string:
            url = f"{url}?{query_string}"
        return url


# ---------------------------------------------------------------------------
# Registry builder — called fresh per snapshot() so env overrides apply.
# ---------------------------------------------------------------------------


def _build_registry() -> Mapping[str, UpstreamEndpoint]:
    anthropic_base = _resolve_anthropic_base_url()
    dw_base = _resolve_doubleword_base_url()
    return {
        # ---- LLM_COMPLETION ----
        "/v1/messages": UpstreamEndpoint(
            aegis_path="/v1/messages",
            upstream_base_url=anthropic_base,
            wire_family=WireFamily.ANTHROPIC,
            auth_header="x-api-key",
            auth_scheme=AuthScheme.HEADER_RAW,
            credential_env_var="ANTHROPIC_API_KEY",
            default_route_hint="IMMEDIATE",
            kind=EndpointKind.LLM_COMPLETION,
            http_methods=("POST",),
        ),
        "/v1/chat/completions": UpstreamEndpoint(
            aegis_path="/v1/chat/completions",
            upstream_base_url=dw_base,
            wire_family=WireFamily.OPENAI_COMPAT,
            auth_header="Authorization",
            auth_scheme=AuthScheme.HEADER_BEARER,
            credential_env_var="DOUBLEWORD_API_KEY",
            default_route_hint="STANDARD",
            kind=EndpointKind.LLM_COMPLETION,
            http_methods=("POST",),
        ),
        # ---- PASSTHROUGH (DW non-LLM operations, allowlisted) ----
        # POST /v1/files — multipart upload of batch input JSONL
        "/v1/files": UpstreamEndpoint(
            aegis_path="/v1/files",
            upstream_base_url=dw_base,
            auth_header="Authorization",
            auth_scheme=AuthScheme.HEADER_BEARER,
            credential_env_var="DOUBLEWORD_API_KEY",
            kind=EndpointKind.PASSTHROUGH,
            http_methods=("POST",),
        ),
        # POST /v1/batches — create batch job
        "/v1/batches": UpstreamEndpoint(
            aegis_path="/v1/batches",
            upstream_base_url=dw_base,
            auth_header="Authorization",
            auth_scheme=AuthScheme.HEADER_BEARER,
            credential_env_var="DOUBLEWORD_API_KEY",
            kind=EndpointKind.PASSTHROUGH,
            http_methods=("POST",),
        ),
        # GET /v1/batches/{batch_id} — poll batch status
        "/v1/batches/{batch_id}": UpstreamEndpoint(
            aegis_path="/v1/batches/{batch_id}",
            upstream_base_url=dw_base,
            auth_header="Authorization",
            auth_scheme=AuthScheme.HEADER_BEARER,
            credential_env_var="DOUBLEWORD_API_KEY",
            kind=EndpointKind.PASSTHROUGH,
            http_methods=("GET",),
        ),
        # GET /v1/files/{file_id}/content — retrieve batch output JSONL
        "/v1/files/{file_id}/content": UpstreamEndpoint(
            aegis_path="/v1/files/{file_id}/content",
            upstream_base_url=dw_base,
            auth_header="Authorization",
            auth_scheme=AuthScheme.HEADER_BEARER,
            credential_env_var="DOUBLEWORD_API_KEY",
            kind=EndpointKind.PASSTHROUGH,
            http_methods=("GET",),
        ),
        # GET /v1/models — list available models (DW health probe)
        "/v1/models": UpstreamEndpoint(
            aegis_path="/v1/models",
            upstream_base_url=dw_base,
            auth_header="Authorization",
            auth_scheme=AuthScheme.HEADER_BEARER,
            credential_env_var="DOUBLEWORD_API_KEY",
            kind=EndpointKind.PASSTHROUGH,
            http_methods=("GET",),
        ),
    }


def snapshot() -> Mapping[str, UpstreamEndpoint]:
    """Build a fresh registry snapshot from current env. Returns a
    new dict each call — caller can hold it as long as env is stable.

    Aegis daemon reads this once at boot. Tests can re-snapshot after
    monkeypatching env."""
    registry = _build_registry()
    _validate_credential_registry_alignment(registry)
    return registry


def _validate_credential_registry_alignment(
    registry: Mapping[str, UpstreamEndpoint],
) -> None:
    """Defense: every upstream endpoint's credential_env_var MUST appear
    in :mod:`credential_registry`'s frozen set — otherwise env_scrub
    would miss it. Raises at boot rather than silently degrading."""
    known = upstream_credential_env_vars()
    missing = sorted(
        ep.credential_env_var for ep in registry.values()
        if ep.credential_env_var not in known
    )
    if missing:
        raise RuntimeError(
            f"upstream_registry references credential env vars not in "
            f"credential_registry: {missing}. Add them to "
            "credential_registry.py:_UPSTREAM_CREDENTIAL_ENV_VARS so "
            "env_scrub strips them at preflight."
        )


def known_aegis_paths() -> Tuple[str, ...]:
    """Stable tuple of Aegis-side paths (templates included). Used by
    AST pins to enumerate the allowed /v1/* surface."""
    return tuple(sorted(_build_registry().keys()))


def endpoint_for_path(path: str) -> Optional[UpstreamEndpoint]:
    """Lookup by aegis_path (template or literal). Returns None if
    not registered."""
    return _build_registry().get(path)


def llm_completion_endpoints() -> Mapping[str, UpstreamEndpoint]:
    """Sub-snapshot of LLM completion endpoints only."""
    return {
        path: ep for path, ep in _build_registry().items()
        if ep.kind is EndpointKind.LLM_COMPLETION
    }


def passthrough_endpoints() -> Mapping[str, UpstreamEndpoint]:
    """Sub-snapshot of passthrough endpoints only."""
    return {
        path: ep for path, ep in _build_registry().items()
        if ep.kind is EndpointKind.PASSTHROUGH
    }


__all__ = [
    "AuthScheme",
    "ENV_AEGIS_UPSTREAM_ANTHROPIC_URL",
    "ENV_AEGIS_UPSTREAM_DOUBLEWORD_URL",
    "EndpointKind",
    "UPSTREAM_REGISTRY_SCHEMA_VERSION",
    "UpstreamEndpoint",
    "WireFamily",
    "endpoint_for_path",
    "known_aegis_paths",
    "llm_completion_endpoints",
    "passthrough_endpoints",
    "snapshot",
]
