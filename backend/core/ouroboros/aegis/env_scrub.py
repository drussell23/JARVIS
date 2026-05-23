"""Env scrub — remove upstream credentials from JARVIS process env.

Per binding correction #2, this module discovers credential env names
from the lightweight :mod:`credential_registry` constants module — not
from dynamic provider imports (which would cause side effects + bloat
preflight).

Per binding correction #6, ``assert_no_upstream_credentials`` is a
hard invariant: when Aegis is enabled, the JARVIS process must boot
with **zero** upstream credentials in its environment. Any presence is
session-fatal.

Functions take the env mapping as an explicit argument so tests can
pass synthetic dicts without mutating the real ``os.environ``.
"""
from __future__ import annotations

import logging
from typing import Dict, Iterable, MutableMapping, Optional

from backend.core.ouroboros.aegis.credential_registry import (
    upstream_credential_env_vars,
)

logger = logging.getLogger(__name__)


class UpstreamCredentialPresentError(RuntimeError):
    """Raised by :func:`assert_no_upstream_credentials` when one or more
    upstream credential env vars are still present in the supplied env.

    The exception message lists the offending KEYS only — never the
    values. Even logging the values briefly would defeat the purpose
    of the scrub.
    """


def scrub_upstream_credentials(
    env: MutableMapping[str, str],
    *,
    extra: Optional[Iterable[str]] = None,
) -> Dict[str, str]:
    """Pop every known upstream credential env var from ``env`` and
    return a dict of the captured ``{name: value}`` pairs.

    The returned dict is meant for the harness to pass the credentials
    into the Aegis subprocess at spawn time — across the fork — and
    then discard. After that pair (return + spawn handoff), no Python
    object in the JARVIS process should retain the value.

    ``extra`` lets callers add env names that aren't in the registry
    (e.g., a one-off provider used by a test harness).

    Returns:
        Dict[name -> value] of every variable that was present and
        popped. If the env contained none of the registered names,
        returns an empty dict.
    """
    names = set(upstream_credential_env_vars())
    if extra is not None:
        names.update(extra)

    captured: Dict[str, str] = {}
    for name in sorted(names):
        if name in env:
            captured[name] = env.pop(name)
            logger.info(
                "[AegisEnvScrub] popped credential env var: %s "
                "(value redacted)", name,
            )
    return captured


def assert_no_upstream_credentials(
    env: MutableMapping[str, str],
    *,
    extra: Optional[Iterable[str]] = None,
) -> None:
    """Raise :class:`UpstreamCredentialPresentError` if ``env`` still
    contains any known upstream credential env var.

    Called by the harness AFTER ``scrub_upstream_credentials`` to
    enforce the binding-correction #6 invariant. Never logs the
    credential value — only the variable name.
    """
    names = set(upstream_credential_env_vars())
    if extra is not None:
        names.update(extra)

    present = sorted(n for n in names if n in env)
    if present:
        raise UpstreamCredentialPresentError(
            "upstream credential env vars still present after Aegis "
            f"preflight scrub: {present}. "
            "JARVIS must boot unprivileged when JARVIS_AEGIS_ENABLED=true."
        )


__all__ = [
    "UpstreamCredentialPresentError",
    "assert_no_upstream_credentials",
    "scrub_upstream_credentials",
]
