"""Slice 125 — Aegis credential health probe (daemon-routed, fail-loud at boot).

After the Aegis daemon starts, this proves the daemon is injecting a VALID
credential of the same class the operator funded — BEFORE a multi-hour soak
spends time. It runs TWO arms and compares:

  • DIRECT arm  — the funded env key hits the real provider (api.doubleword.ai).
  • AEGIS arm   — the SAME request goes through the Aegis daemon path.

The comparison disambiguates the failure that cost us four soaks (a misleading
``402 "balance too low"`` that was actually a credential-injection gap, not an
out-of-credits problem). Classification (operator's table):

  direct OK  + aegis 402/401  → AEGIS_CREDENTIAL_INJECTION_FAILED  (daemon bug)
  direct 401/402             → OPERATOR_CREDENTIAL_PROBLEM         (key bad/unfunded)
  network / timeout          → TRANSPORT_PROBLEM
  direct OK  + aegis OK      → OK

A non-OK verdict at boot is meant to FAIL LOUD — never silently fall back and
burn expensive Claude credits for hours. Logs carry only redacted fingerprints
(``sha256(value)[:8]``), never raw keys.
"""

from __future__ import annotations

import enum
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_ENV_MASTER = "JARVIS_AEGIS_CREDENTIAL_PROBE_ENABLED"
_PROBE_TIMEOUT_S = 20.0


def credential_probe_enabled() -> bool:
    return os.getenv(_ENV_MASTER, "false").strip().lower() in ("1", "true", "yes", "on")


class CredentialVerdict(str, enum.Enum):
    OK = "ok"
    AEGIS_CREDENTIAL_INJECTION_FAILED = "aegis_credential_injection_failed"
    OPERATOR_CREDENTIAL_PROBLEM = "operator_credential_problem"
    TRANSPORT_PROBLEM = "transport_problem"
    SKIPPED = "skipped"
    INDETERMINATE = "indeterminate"


def classify_credential_probe(
    direct_status: Optional[int],
    aegis_status: Optional[int],
) -> CredentialVerdict:
    """PURE classifier. ``None`` status = network/timeout/error on that arm.

    The DIRECT arm is the ground truth for the operator's key. If it can't even
    reach the provider, we can't conclude anything about Aegis → TRANSPORT.
    """
    if direct_status is None:
        # Can't establish ground truth — don't blame Aegis.
        return CredentialVerdict.TRANSPORT_PROBLEM
    if direct_status in (401, 402):
        # The funded key itself is rejected → operator credential problem.
        return CredentialVerdict.OPERATOR_CREDENTIAL_PROBLEM
    if direct_status == 200:
        if aegis_status is None:
            return CredentialVerdict.TRANSPORT_PROBLEM        # aegis arm unreachable
        if aegis_status in (401, 402):
            # Funded key works direct but Aegis path is rejected → injection gap.
            return CredentialVerdict.AEGIS_CREDENTIAL_INJECTION_FAILED
        if aegis_status == 200:
            return CredentialVerdict.OK
        if aegis_status >= 500:
            return CredentialVerdict.TRANSPORT_PROBLEM
        return CredentialVerdict.INDETERMINATE
    if direct_status >= 500:
        return CredentialVerdict.TRANSPORT_PROBLEM
    return CredentialVerdict.INDETERMINATE


def is_fatal(verdict: CredentialVerdict) -> bool:
    """Verdicts that must HALT a long soak before it spends time/credits."""
    return verdict in (
        CredentialVerdict.AEGIS_CREDENTIAL_INJECTION_FAILED,
        CredentialVerdict.OPERATOR_CREDENTIAL_PROBLEM,
    )


async def _http_status(url: str, headers: dict, *, timeout_s: float) -> Optional[int]:
    """GET ``url`` and return the HTTP status, or ``None`` on transport error.
    Never raises; never logs response bodies (may echo a key)."""
    try:
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=timeout_s)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:
                return resp.status
    except Exception as exc:  # noqa: BLE001 - transport classified as None
        logger.debug("[CredentialProbe] arm transport error: %s", exc.__class__.__name__)
        return None


async def probe_dw_credential_health(*, timeout_s: float = _PROBE_TIMEOUT_S) -> CredentialVerdict:
    """Run both arms against DW ``/models`` and classify. Inert (SKIPPED) when
    disabled or when no funded key is present in env to ground-truth against."""
    if not credential_probe_enabled():
        return CredentialVerdict.SKIPPED

    dw_key = os.environ.get("DOUBLEWORD_API_KEY", "")
    if not dw_key:
        # Nothing to ground-truth — the env-bootstrap should have filled it; if
        # it didn't, that's the very failure we guard, surfaced as a clear skip.
        logger.warning("[CredentialProbe] no DOUBLEWORD_API_KEY in env — cannot probe (bootstrap gap?)")
        return CredentialVerdict.SKIPPED

    # DIRECT arm — real DW base + funded env key.
    direct_base = os.environ.get("DOUBLEWORD_BASE_URL", "https://api.doubleword.ai/v1")
    direct_status = await _http_status(
        f"{direct_base}/models", {"Authorization": f"Bearer {dw_key}"}, timeout_s=timeout_s,
    )

    # AEGIS arm — the daemon path + the daemon-injected auth header.
    aegis_status: Optional[int] = None
    try:
        from backend.core.ouroboros.governance.aegis_provider_bridge import (
            dw_aegis_base_url,
            dw_session_auth_header,
        )

        aegis_base = dw_aegis_base_url()
        aegis_headers = await dw_session_auth_header()
        aegis_status = await _http_status(
            f"{aegis_base}/models", aegis_headers, timeout_s=timeout_s,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[CredentialProbe] aegis arm setup error: %s", exc.__class__.__name__)
        aegis_status = None

    verdict = classify_credential_probe(direct_status, aegis_status)
    # Redacted fingerprint only — never the key.
    from backend.core.ouroboros.aegis.credential_env_loader import fingerprint
    logger.info(
        "[CredentialProbe] verdict=%s direct=%s aegis=%s key=%s",
        verdict.value, direct_status, aegis_status, fingerprint(dw_key),
    )
    return verdict


__all__ = [
    "credential_probe_enabled",
    "CredentialVerdict",
    "classify_credential_probe",
    "is_fatal",
    "probe_dw_credential_health",
]
