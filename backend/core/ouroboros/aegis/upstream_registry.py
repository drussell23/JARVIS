"""Upstream endpoint registry — single source of truth for forwarding.

Maps each Aegis-side path Aegis serves (``/v1/messages``,
``/v1/chat/completions``) to:

  1. The upstream base URL (where Aegis sends the forwarded request).
  2. The auth header name expected by the upstream (e.g. ``x-api-key``
     for Anthropic, ``Authorization: Bearer`` for OpenAI-compat).
  3. The credential env var name (so Aegis knows which env var holds
     the real API key the forwarded request needs).
  4. The wire family (``anthropic`` or ``openai_compat``) so the
     forwarder knows which streaming-usage parser to apply.

The registry is built at module import from existing JARVIS env
conventions (``DOUBLEWORD_BASE_URL`` is composed; Anthropic's URL is
sourced from env with the well-known default). NO hardcoded constants
beyond the upstream domain names — and even those are env-overridable.

This is a CLOSED registry. Adding a new upstream provider requires:
  1. Adding an :class:`UpstreamEndpoint` to ``_REGISTRY``
  2. Adding the credential env var to
     :mod:`backend.core.ouroboros.aegis.credential_registry`
  3. Adding the wire-family handler in
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


UPSTREAM_REGISTRY_SCHEMA_VERSION: str = "aegis_upstream_registry.1"


class WireFamily(str, enum.Enum):
    """Closed 2-value taxonomy of upstream wire shapes Aegis forwards.

    Each family has a corresponding streaming-usage parser in
    :mod:`backend.core.ouroboros.aegis.forwarding`.
    """

    ANTHROPIC = "anthropic"          # /v1/messages, SSE with `usage` in message_start + message_delta
    OPENAI_COMPAT = "openai_compat"  # /v1/chat/completions, SSE with `usage` in final delta


class AuthScheme(str, enum.Enum):
    """How the upstream expects the credential to be presented."""

    HEADER_RAW = "header_raw"             # Header value IS the raw key (e.g. x-api-key)
    HEADER_BEARER = "header_bearer"       # Header value is `Bearer <key>`


# ---------------------------------------------------------------------------
# Env knobs (single seam)
# ---------------------------------------------------------------------------

ENV_AEGIS_UPSTREAM_ANTHROPIC_URL: str = "JARVIS_AEGIS_UPSTREAM_ANTHROPIC_URL"
ENV_AEGIS_UPSTREAM_DOUBLEWORD_URL: str = "JARVIS_AEGIS_UPSTREAM_DOUBLEWORD_URL"

_ANTHROPIC_DEFAULT_BASE_URL: str = "https://api.anthropic.com"
# DOUBLEWORD_BASE_URL convention from doubleword_provider.py:44 includes
# the /v1 suffix. Aegis aligns: the upstream BASE URL we use for outbound
# already includes /v1, and the path Aegis serves is /v1/chat/completions.
# We compose by reading the existing env, falling back to the same default.
_DOUBLEWORD_DEFAULT_BASE_URL: str = "https://api.doubleword.ai"


def _resolve_anthropic_base_url() -> str:
    # Operator override first, then bare default.
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
        # Strip trailing /v1 if present so we can append the path served.
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

    ``aegis_path`` is what Aegis registers as its own route. The
    forwarder appends this path to ``upstream_base_url`` to compose
    the outbound URL — preserves byte-identity between what JARVIS
    requests and what upstream receives.
    """

    aegis_path: str
    upstream_base_url: str
    wire_family: WireFamily
    auth_header: str
    auth_scheme: AuthScheme
    credential_env_var: str
    # Default route hint — used only if the lease doesn't specify a route.
    # Aegis pricing lookup is keyed on (route, model); the lease already
    # carries route, so this is just a defensive fallback.
    default_route_hint: str

    def upstream_url(self) -> str:
        return f"{self.upstream_base_url.rstrip('/')}{self.aegis_path}"


# ---------------------------------------------------------------------------
# Registry builder — called fresh per snapshot() so env overrides apply.
# ---------------------------------------------------------------------------


def _build_registry() -> Mapping[str, UpstreamEndpoint]:
    return {
        "/v1/messages": UpstreamEndpoint(
            aegis_path="/v1/messages",
            upstream_base_url=_resolve_anthropic_base_url(),
            wire_family=WireFamily.ANTHROPIC,
            auth_header="x-api-key",
            auth_scheme=AuthScheme.HEADER_RAW,
            credential_env_var="ANTHROPIC_API_KEY",
            default_route_hint="IMMEDIATE",
        ),
        "/v1/chat/completions": UpstreamEndpoint(
            aegis_path="/v1/chat/completions",
            upstream_base_url=_resolve_doubleword_base_url(),
            wire_family=WireFamily.OPENAI_COMPAT,
            auth_header="Authorization",
            auth_scheme=AuthScheme.HEADER_BEARER,
            credential_env_var="DOUBLEWORD_API_KEY",
            default_route_hint="STANDARD",
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
    would miss it. Raises at boot rather than silently degrading.

    This is the structural seam that keeps the two registries from
    drifting. Adding a new provider requires both edits.
    """
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
    """Stable tuple of the Aegis-side paths this registry serves.
    Used by AST pins to enumerate the allowed /v1/* surface."""
    return tuple(sorted(_build_registry().keys()))


def endpoint_for_path(path: str) -> Optional[UpstreamEndpoint]:
    """Lookup helper. Returns None if the path is not registered."""
    return _build_registry().get(path)


__all__ = [
    "AuthScheme",
    "ENV_AEGIS_UPSTREAM_ANTHROPIC_URL",
    "ENV_AEGIS_UPSTREAM_DOUBLEWORD_URL",
    "UPSTREAM_REGISTRY_SCHEMA_VERSION",
    "UpstreamEndpoint",
    "WireFamily",
    "endpoint_for_path",
    "known_aegis_paths",
    "snapshot",
]
