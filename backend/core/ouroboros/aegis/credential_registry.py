"""Credential env var registry — single source of truth.

Per Slice Aegis-1 binding correction #2: env scrub MUST NOT dynamically
import provider modules just to learn credential env var names (heavy
imports cause side-effects + pollute preflight). Instead, every component
that needs to know "which env vars carry upstream credentials" composes
this single lightweight constants module.

Sources of truth this list mirrors:

  * ``governance/doubleword_provider.py:43`` — ``DOUBLEWORD_API_KEY``
  * ``governance/providers.py:5408`` — ``ANTHROPIC_API_KEY`` (Anthropic SDK
    convention; also read by various harness preflight checks)
  * ``battle_test/presentation_restraint.py:479,518`` — both above

Adding a new upstream provider:

  1. Add the env var name to ``_UPSTREAM_CREDENTIAL_ENV_VARS`` below.
  2. The Aegis env_scrub will pick it up at next boot — no other change.

Authority posture: zero imports beyond stdlib. AST-pinned. No
``os.environ`` reads here; that's the scrub's job. This module is a
pure declarative manifest.
"""
from __future__ import annotations

from typing import FrozenSet


_UPSTREAM_CREDENTIAL_ENV_VARS: FrozenSet[str] = frozenset({
    "ANTHROPIC_API_KEY",
    "DOUBLEWORD_API_KEY",
})


def upstream_credential_env_vars() -> FrozenSet[str]:
    """Return the frozen set of env var names that carry upstream
    LLM provider credentials.

    Aegis env scrub pops each of these from the JARVIS process
    environment before the supervisor boots. Providers (in Slice 2)
    will discover this list rather than hardcoding their own env
    var names.
    """
    return _UPSTREAM_CREDENTIAL_ENV_VARS


__all__ = ["upstream_credential_env_vars"]
