"""Slice 212 — Boot-Time Runtime Attestation & Integrity Gate.

Hardens against the lived deployment-drift failure (2026-06-10): a dirty
compose file silently blocked the ff-merge, the image was rebuilt from STALE
Slice-208 sources while everyone believed it carried Slice-211, and a buggy
monitor false-positived. Nothing in the runtime could attest which code it was
actually running.

DESIGN (corrected from the original plan):
- The image ships no ``.git`` (excluded from the build context), and checking
  live ``origin/main`` on every boot would FALSE-TRIP under ``restart:always``
  after any later legitimate merge — the watchdog would brick a healthy soak.
- Instead: the image is STAMPED at build time (Dockerfile build args →
  ``/app/.build_attestation.json`` with ``{commit, dirty}``), and at boot the
  stamp is compared against an OPERATOR-PINNED expected commit
  (``JARVIS_ATTESTATION_EXPECTED_COMMIT``, set by the launch path at launch).
  Container restarts keep the same pin → no false trips; a stale or
  dirty-tree build trips the gate.

Verdicts: DISABLED / MATCH / MISMATCH / DIRTY_BUILD / UNSTAMPED / UNPINNED.
Strict mode (default ON when enabled): MISMATCH / DIRTY_BUILD / UNSTAMPED →
``DeploymentIntegrityMismatch`` raised BEFORE the GLS fail-soft boot block
(state → FAILED, the loop never runs). UNPINNED is always warn-only so a
casual ``docker compose up`` without the launch wrapper degrades loudly, not
fatally. A best-effort telemetry-sentinel webhook fires on any failure
verdict. Master ``JARVIS_RUNTIME_ATTESTATION_ENABLED`` default-FALSE
(OFF = byte-identical legacy boot).
"""
from __future__ import annotations

import enum
import json
import logging
import os
from pathlib import Path
from typing import Tuple

logger = logging.getLogger(__name__)

_ENV_MASTER = "JARVIS_RUNTIME_ATTESTATION_ENABLED"
_ENV_STRICT = "JARVIS_RUNTIME_ATTESTATION_STRICT"
_ENV_EXPECTED = "JARVIS_ATTESTATION_EXPECTED_COMMIT"
_ENV_STAMP_PATH = "JARVIS_BUILD_ATTESTATION_PATH"
_DEFAULT_STAMP_PATH = "/app/.build_attestation.json"


class AttestationVerdict(str, enum.Enum):
    DISABLED = "disabled"
    MATCH = "match"
    MISMATCH = "mismatch"
    DIRTY_BUILD = "dirty_build"
    UNSTAMPED = "unstamped"
    UNPINNED = "unpinned"


class DeploymentIntegrityMismatch(RuntimeError):
    """Strict attestation failure — the running image does not carry the code
    the operator pinned. The loop must not run on unattested code."""


def _truthy(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def attestation_enabled() -> bool:
    """Master gate, default-FALSE. NEVER raises."""
    return _truthy(_ENV_MASTER, False)


def strict_mode() -> bool:
    """Fail-closed on failure verdicts (default TRUE when enabled)."""
    return _truthy(_ENV_STRICT, True)


def _stamp_path() -> Path:
    return Path(os.environ.get(_ENV_STAMP_PATH, "").strip() or _DEFAULT_STAMP_PATH)


def load_build_stamp() -> Tuple[str, str]:
    """Return (commit, dirty) from the build stamp; ('', '') if unreadable.
    NEVER raises."""
    try:
        doc = json.loads(_stamp_path().read_text(encoding="utf-8"))
        return (
            str(doc.get("commit", "") or "").strip().lower(),
            str(doc.get("dirty", "") or "").strip().lower(),
        )
    except Exception:  # noqa: BLE001
        return "", ""


def expected_commit() -> str:
    return os.environ.get(_ENV_EXPECTED, "").strip().lower()


def verify() -> Tuple[AttestationVerdict, str]:
    """Compare the image's build stamp against the operator pin. NEVER raises.

    Prefix comparison (either direction) so a short pin matches a full stamped
    hash. ``commit == 'unstamped'`` (the Dockerfile ARG default) counts as
    UNSTAMPED, not a weird mismatch.
    """
    try:
        if not attestation_enabled():
            return AttestationVerdict.DISABLED, "attestation disabled"
        commit, dirty = load_build_stamp()
        if not commit or commit == "unstamped":
            return (
                AttestationVerdict.UNSTAMPED,
                f"image carries no build stamp at {_stamp_path()} — built "
                "outside the attested launch path",
            )
        if dirty in ("true", "1", "yes"):
            return (
                AttestationVerdict.DIRTY_BUILD,
                f"image was built from a DIRTY tree at {commit[:12]} — "
                "uncommitted changes were baked in (the exact drift class that "
                "shipped stale Slice-208 code as 'Slice 211')",
            )
        pin = expected_commit()
        if not pin:
            return (
                AttestationVerdict.UNPINNED,
                f"image stamped {commit[:12]} but no operator pin set "
                f"({_ENV_EXPECTED}) — drift check skipped",
            )
        if commit.startswith(pin) or pin.startswith(commit):
            return AttestationVerdict.MATCH, f"image {commit[:12]} == pin {pin[:12]}"
        return (
            AttestationVerdict.MISMATCH,
            f"image stamped {commit[:12]} but operator pinned {pin[:12]} — "
            "the container is NOT running the code you think it is",
        )
    except Exception as exc:  # noqa: BLE001
        return AttestationVerdict.UNSTAMPED, f"attestation verify error: {exc!r}"


def _alert_best_effort(detail: str) -> None:
    """Fire the telemetry-sentinel webhook if configured. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.telemetry_sentinel import (
            get_sentinel, telemetry_sentinel_enabled,
        )
        if not telemetry_sentinel_enabled():
            return
        sentinel = get_sentinel()
        alert = sentinel.note_safety_refusal(
            f"DEPLOYMENT_INTEGRITY_MISMATCH: {detail}",
        )
        if alert is not None:
            import asyncio
            try:
                loop = asyncio.get_running_loop()
                task = loop.create_task(sentinel.dispatch(alert))
                _ = task  # strong ref held by loop until done
            except RuntimeError:
                asyncio.run(sentinel.dispatch(alert))
    except Exception:  # noqa: BLE001
        logger.debug("[Attestation] sentinel alert swallowed", exc_info=True)


def enforce() -> AttestationVerdict:
    """Boot-time gate. MATCH/DISABLED → silent pass. UNPINNED → warn.
    Failure verdicts → CRITICAL log + best-effort sentinel alert, then
    ``DeploymentIntegrityMismatch`` in strict mode (warn-only otherwise)."""
    verdict, detail = verify()
    if verdict in (AttestationVerdict.DISABLED, AttestationVerdict.MATCH):
        if verdict is AttestationVerdict.MATCH:
            logger.info("[Attestation] runtime attested: %s", detail)
        return verdict
    if verdict is AttestationVerdict.UNPINNED:
        logger.warning("[Attestation] %s", detail)
        return verdict
    logger.critical(
        "[Attestation] DEPLOYMENT_INTEGRITY_MISMATCH (%s): %s",
        verdict.value, detail,
    )
    _alert_best_effort(detail)
    if strict_mode():
        raise DeploymentIntegrityMismatch(
            f"DEPLOYMENT_INTEGRITY_MISMATCH ({verdict.value}): {detail}",
        )
    logger.warning(
        "[Attestation] strict mode OFF — continuing on unattested code "
        "(verdict=%s)", verdict.value,
    )
    return verdict
