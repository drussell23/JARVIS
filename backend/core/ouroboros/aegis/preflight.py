"""Harness preflight — spawn Aegis subprocess + scrub JARVIS env.

This module is the SINGLE seam by which the harness wires up Aegis.
The outer entry point (``scripts/ouroboros_battle_test.py``) calls
:func:`aegis_preflight` **before** importing any provider module
(otherwise providers would already have captured the credential env
vars into module-level constants — e.g.,
``doubleword_provider.py:43`` reads ``DOUBLEWORD_API_KEY`` at import
time).

Bootstrap dance (matches the §43 spine):

  1. Generate a unique bootstrap-out path under :func:`bootstrap_dir`.
  2. Snapshot upstream credentials currently in the harness env.
  3. Spawn the Aegis daemon subprocess (``python -m
     backend.core.ouroboros.aegis.daemon``) with credentials in
     **its** env. Use ``close_fds=True`` and ``stdin=DEVNULL`` so the
     subprocess starts clean.
  4. Wait (poll-with-backoff) for the daemon to write its bootstrap
     payload to the chosen path. Timeout via
     ``JARVIS_AEGIS_BOOTSTRAP_TIMEOUT_S``.
  5. Read the payload, unlink it (forensic-trace-free).
  6. Validate ``expires_at`` is still in the future (rejects stale).
  7. Scrub the upstream credential env vars from the harness env.
  8. Assert post-scrub absence (binding-correction #6 hard invariant).
  9. Return the preflight result for the harness to consume.

Default-off: if :func:`flags.is_enabled` returns False, the function
short-circuits with a SKIPPED result and zero behavior change.
"""
from __future__ import annotations

import asyncio
import enum
import logging
import os
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

from backend.core.ouroboros.aegis.bootstrap import (
    BootstrapPayload,
    read_and_unlink_payload,
)
from backend.core.ouroboros.aegis.credential_registry import (
    upstream_credential_env_vars,
)
from backend.core.ouroboros.aegis.env_scrub import (
    UpstreamCredentialPresentError,
    assert_no_upstream_credentials,
    scrub_upstream_credentials,
)
from backend.core.ouroboros.aegis.flags import (
    bootstrap_dir,
    bootstrap_timeout_s,
    is_enabled,
    register_aegis_flags,
)

logger = logging.getLogger(__name__)


PREFLIGHT_SCHEMA_VERSION: str = "aegis_preflight.1"


class PreflightOutcome(str, enum.Enum):
    """Closed 5-value outcome taxonomy for the preflight."""

    SKIPPED_DISABLED = "skipped_disabled"
    READY = "ready"
    FAILED_SPAWN = "failed_spawn"
    FAILED_BOOTSTRAP_TIMEOUT = "failed_bootstrap_timeout"
    FAILED_CREDENTIAL_SCRUB = "failed_credential_scrub"


@dataclass(frozen=True)
class AegisPreflightResult:
    """Frozen preflight result. Lossless §33.5 to_dict/from_dict.

    On non-READY outcomes, ``aegis_url`` / ``bootstrap_psk`` are None
    and ``subprocess_pid`` is None. The harness inspects ``outcome``
    to decide whether to proceed (READY) or abort the session
    (any FAILED_* — Aegis is enabled but unhealthy is fatal per the
    operator's binding directive "Aegis death = session ends").
    """

    outcome: PreflightOutcome
    aegis_url: Optional[str] = None
    bootstrap_psk: Optional[str] = None
    subprocess_pid: Optional[int] = None
    detail: Optional[str] = None
    schema_version: str = PREFLIGHT_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "outcome": self.outcome.value,
            "aegis_url": self.aegis_url,
            # NEVER include bootstrap_psk in to_dict output — it's a
            # credential. Caller has access via the dataclass field
            # directly; we don't put it in any payload that might be
            # logged or persisted.
            "subprocess_pid": self.subprocess_pid,
            "detail": self.detail,
            "schema_version": self.schema_version,
        }


def _unique_bootstrap_path(dir_: Path) -> Path:
    """Generate a fresh bootstrap-out path with high-entropy suffix.

    Random suffix means concurrent harness invocations (e.g., parallel
    test runs) never collide on the O_EXCL atomic write.
    """
    dir_.mkdir(parents=True, exist_ok=True, mode=0o700)
    suffix = secrets.token_hex(8)
    return dir_ / f"aegis-{os.getpid()}-{suffix}.json"


def _spawn_daemon(
    *,
    bootstrap_out: Path,
    credentials: Mapping[str, str],
    bind_host_override: Optional[str] = None,
) -> subprocess.Popen:
    """Spawn the Aegis daemon subprocess. Caller owns the lifecycle."""
    # Build the subprocess env: start from the current env (so PATH,
    # PYTHONPATH, etc. survive) BUT inject the credentials (which the
    # harness env is about to lose).
    sub_env = dict(os.environ)
    sub_env.update(credentials)

    cmd = [
        sys.executable,
        "-m",
        "backend.core.ouroboros.aegis.daemon",
        "--bootstrap-out",
        str(bootstrap_out),
    ]
    if bind_host_override:
        cmd.extend(["--bind-host", bind_host_override])

    return subprocess.Popen(
        cmd,
        env=sub_env,
        stdin=subprocess.DEVNULL,
        # stdout/stderr go to the harness's tty/log — operator can
        # tail them. The daemon's logging format is prefixed
        # `aegis-daemon` for easy grep.
        close_fds=True,
    )


async def _await_bootstrap_payload(
    path: Path,
    *,
    timeout_s: int,
    proc: subprocess.Popen,
) -> Optional[BootstrapPayload]:
    """Poll for the bootstrap-payload file to appear.

    Returns the parsed payload on success, None on timeout or
    subprocess death.

    Polls with exponential backoff capped at 100ms — fast enough
    that a quick boot (<50ms) is observed promptly, slow enough
    that a hung boot doesn't burn CPU.
    """
    deadline = time.monotonic() + float(timeout_s)
    backoff = 0.010  # 10ms initial
    max_backoff = 0.100

    while time.monotonic() < deadline:
        # Subprocess crashed?
        ret = proc.poll()
        if ret is not None:
            logger.error(
                "[AegisPreflight] daemon subprocess exited prematurely "
                "with code %d before writing payload", ret,
            )
            return None

        if path.exists():
            try:
                payload = read_and_unlink_payload(path)
                return payload
            except (FileNotFoundError, ValueError, OSError) as exc:
                logger.warning(
                    "[AegisPreflight] payload at %s failed to parse: %s",
                    path, exc,
                )
                # Don't loop on a malformed payload — return None
                # so the caller can treat as failure.
                return None

        await asyncio.sleep(backoff)
        backoff = min(max_backoff, backoff * 1.5)

    return None


async def aegis_preflight(
    *,
    env: Optional[dict] = None,
    bind_host_override: Optional[str] = None,
) -> AegisPreflightResult:
    """Run the full Aegis preflight handshake.

    ``env`` defaults to ``os.environ``. When passed explicitly (tests),
    the scrub mutates that dict only — handy for asserting "JARVIS
    env is empty of upstream creds post-preflight" without touching
    the real environment.

    Always async (we await the bootstrap-payload poll). On success,
    returns READY with ``aegis_url`` + ``bootstrap_psk`` + ``pid``.
    On any failure, returns a FAILED_* outcome with ``detail``.
    Never raises.
    """
    target_env = os.environ if env is None else env

    register_aegis_flags()  # idempotent

    if not is_enabled():
        return AegisPreflightResult(
            outcome=PreflightOutcome.SKIPPED_DISABLED,
            detail="JARVIS_AEGIS_ENABLED is false (Slice 1 default)",
        )

    # Snapshot credentials — we need them to hand to the subprocess
    # BEFORE we strip them from our env.
    creds = {
        name: target_env[name]
        for name in upstream_credential_env_vars()
        if name in target_env
    }

    bootstrap_out = _unique_bootstrap_path(bootstrap_dir())

    try:
        proc = _spawn_daemon(
            bootstrap_out=bootstrap_out,
            credentials=creds,
            bind_host_override=bind_host_override,
        )
    except (OSError, ValueError) as exc:
        return AegisPreflightResult(
            outcome=PreflightOutcome.FAILED_SPAWN,
            detail=f"subprocess spawn failed: {exc}",
        )

    payload = await _await_bootstrap_payload(
        bootstrap_out, timeout_s=bootstrap_timeout_s(), proc=proc,
    )
    if payload is None:
        try:
            proc.terminate()
        except OSError:
            pass
        return AegisPreflightResult(
            outcome=PreflightOutcome.FAILED_BOOTSTRAP_TIMEOUT,
            subprocess_pid=proc.pid,
            detail=(
                f"daemon did not write bootstrap payload within "
                f"{bootstrap_timeout_s()}s"
            ),
        )

    # Defense: reject a stale payload (a previous boot's leftover the
    # daemon somehow re-served). Should never happen given O_EXCL +
    # unique-path, but cheap to check.
    if time.time() >= payload.expires_at:
        try:
            proc.terminate()
        except OSError:
            pass
        return AegisPreflightResult(
            outcome=PreflightOutcome.FAILED_BOOTSTRAP_TIMEOUT,
            subprocess_pid=proc.pid,
            detail="bootstrap payload expired before harness read it",
        )

    # Scrub credentials from the harness env. ``creds`` already holds
    # the values; the env is now safe to lose them.
    scrub_upstream_credentials(target_env)
    try:
        assert_no_upstream_credentials(target_env)
    except UpstreamCredentialPresentError as exc:
        try:
            proc.terminate()
        except OSError:
            pass
        return AegisPreflightResult(
            outcome=PreflightOutcome.FAILED_CREDENTIAL_SCRUB,
            subprocess_pid=proc.pid,
            detail=str(exc),
        )

    # Expose Aegis coordinates to JARVIS via env. Slice 2's provider
    # rewrite will consume these.
    target_env["JARVIS_AEGIS_URL"] = payload.aegis_url
    target_env["JARVIS_AEGIS_BOOTSTRAP_PSK"] = payload.bootstrap_psk

    return AegisPreflightResult(
        outcome=PreflightOutcome.READY,
        aegis_url=payload.aegis_url,
        bootstrap_psk=payload.bootstrap_psk,
        subprocess_pid=payload.daemon_pid,
        detail=f"daemon pid={payload.daemon_pid}",
    )


__all__ = [
    "AegisPreflightResult",
    "PREFLIGHT_SCHEMA_VERSION",
    "PreflightOutcome",
    "aegis_preflight",
]
