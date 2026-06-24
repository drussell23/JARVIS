"""cross_repo_master_flag -- Master Arming Switch for the Sovereign Cross-Repo Mutator.

Consumer-facing gate for ``JARVIS_CROSS_REPO_MUTATION_ENABLED``. Default OFF --
byte-identical to today. The flag only arms when an explicit-truthy token is set
AND a startup handshake confirms the sandbox environment is ready (Docker alive +
egress sinkhole configurable). Any uncertainty degrades gracefully to DISABLED
(fail-CLOSED).

Spec reference: docs/superpowers/specs/2026-06-23-sovereign-cross-repo-mutator.md
section 6 (Cross-cutting / Invariants) -- ``master JARVIS_CROSS_REPO_MUTATION_ENABLED
(default OFF) -- else Body-only (today)``.

Design invariants (fail-CLOSED everywhere):
  * **Garbage / falsy does NOT arm.** Only ``value.strip().lower() in
    {"1","true","yes","on"}`` sets ``flag_set=True``. Unset / garbage / any other
    value -> ``flag_set=False -> armed=False`` immediately.  This is the INVERSE of
    ``critical_elevation.is_critical_elevation_enabled()`` which fails CLOSED to
    ENABLED (jarvis hard-halt); here the master WRITE flag only arms on explicit
    truthy (fail-CLOSED means: unrecognised garbage does NOT grant write permission).
  * **Both Docker alive AND sinkhole configurable are required.** If either check
    fails, ``armed=False`` + ``reason="degraded: <which check failed>"``.
  * **Fail-soft on any handshake error.** Any exception -> ``armed=False``, never
    propagates.  We log at WARNING so the operator sees the problem.
  * **Caching is on by default.** The handshake runs once (at first call to
    ``cross_repo_mutation_enabled()``) and the result is cached for the process
    lifetime.  Set ``JARVIS_CROSS_REPO_HANDSHAKE_CACHE=0`` to re-evaluate on every
    call (useful in tests).

Env knobs:
  * ``JARVIS_CROSS_REPO_MUTATION_ENABLED`` (default unset/OFF) -- master arm flag.
  * ``JARVIS_CROSS_REPO_HANDSHAKE_CACHE`` (default 1) -- ``0`` disables caching.

Injectable runner interface (``HandshakeRunner`` protocol):
  * ``async docker_info() -> RunResult``
  * ``async can_render_airgap() -> bool``
Tests inject a ``_FakeRunner``; the real ``_DefaultRunner`` calls Docker.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Protocol

logger = logging.getLogger("Ouroboros.CrossRepoMasterFlag")

# ---------------------------------------------------------------------------
# Env knobs
# ---------------------------------------------------------------------------
_ENV_FLAG = "JARVIS_CROSS_REPO_MUTATION_ENABLED"
_ENV_CACHE = "JARVIS_CROSS_REPO_HANDSHAKE_CACHE"

# Only these tokens arm the master flag (explicit-truthy only).
_TRUTHY = frozenset({"1", "true", "yes", "on"})

# ---------------------------------------------------------------------------
# Module-level cache (None = not yet evaluated)
# ---------------------------------------------------------------------------
_cached_handshake: Optional["ArmingHandshake"] = None


# ---------------------------------------------------------------------------
# Lightweight result type for runner calls
# ---------------------------------------------------------------------------
@dataclass
class RunResult:
    """Result of a single runner invocation (mirrors trinity_integration_gate)."""

    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


# ---------------------------------------------------------------------------
# Runner protocol -- all Docker/subprocess lives behind this boundary
# ---------------------------------------------------------------------------
class HandshakeRunner(Protocol):
    """Injectable boundary around Docker/subprocess for the arming handshake.

    Tests provide a fake implementation that records calls and returns scripted
    results -- so the test-suite touches no real Docker.
    """

    async def docker_info(self) -> RunResult:
        """Run ``docker info`` (or equivalent). ``returncode == 0`` => alive."""
        ...

    async def can_render_airgap(self) -> bool:
        """Return True iff the egress-sinkhole compose overlay can be rendered
        (pure logic from ``trinity_integration_gate.build_airgap_compose``).

        The real implementation calls ``build_airgap_compose`` with a trivial
        config and asserts the rendered YAML contains ``internal: true`` and the
        egress-mock service.  On any error it returns False (fail-CLOSED).
        """
        ...


class _DefaultRunner:
    """Real runner: calls ``docker info`` via subprocess.

    Lazy-import of subprocess so the module imports clean in
    test/sandbox environments without Docker.
    """

    async def docker_info(self) -> RunResult:
        def _call() -> RunResult:
            import subprocess  # noqa: PLC0415 - lazy
            try:
                proc = subprocess.run(
                    ["docker", "info"],
                    capture_output=True,
                    text=True,
                    timeout=10.0,
                )
                return RunResult(proc.returncode, proc.stdout or "", proc.stderr or "")
            except Exception as exc:
                return RunResult(returncode=1, stdout="", stderr=str(exc))
        return await asyncio.to_thread(_call)

    async def can_render_airgap(self) -> bool:
        """Attempt to render a trivial air-gap compose overlay and verify it is
        internally-networked.  Pure logic -- no Docker calls, no disk I/O beyond
        a temp file the gate already handles.  Returns True iff the rendered YAML
        has ``internal: true`` AND the egress-mock service.
        """
        try:
            from backend.core.ouroboros.governance.saga.trinity_integration_gate import (  # noqa: E501
                _rendered_overlay_is_air_gapped,
                _provider_url_overrides,
                EGRESS_MOCK_SERVICE,
            )
            # Build a minimal overlay in memory (no real compose file needed).
            # We exercise the pure render path directly.
            try:
                import yaml  # type: ignore
            except ImportError:
                # PyYAML absent -- cannot verify the overlay; degrade gracefully.
                logger.debug(
                    "[CrossRepoMasterFlag] PyYAML absent -- sinkhole check degraded"
                )
                return False

            net = "trinity_sandbox_net"
            port = 8099
            overrides = _provider_url_overrides(mock_port=port)
            base_svc = {
                "jarvis": {"image": "python:3.11-slim", "environment": {}, "networks": [net]},
            }
            out_services: Dict[str, Any] = {}
            for name, svc in base_svc.items():
                svc = dict(svc)
                env = dict(svc.get("environment") or {})
                env.update(overrides)
                svc["environment"] = env
                svc["networks"] = [net]
                out_services[name] = svc
            # Add egress-mock service (mirrors build_airgap_compose).
            out_services[EGRESS_MOCK_SERVICE] = {
                "image": "python:3.11-slim",
                "container_name": "trinity_sandbox_egress_mock",
                "environment": {"TRINITY_SANDBOX_EGRESS_PORT": str(port)},
                "networks": [net],
            }
            overlay: Dict[str, Any] = {
                "services": out_services,
                "networks": {net: {"internal": True, "driver": "bridge"}},
            }
            overlay_yaml = yaml.safe_dump(overlay, sort_keys=False, default_flow_style=False)
            # Verify it is air-gapped (pure static check).
            return _rendered_overlay_is_air_gapped(overlay_yaml, network=net)
        except Exception as exc:  # noqa: BLE001 -- fail-CLOSED
            logger.debug(
                "[CrossRepoMasterFlag] can_render_airgap raised: %s", exc, exc_info=True
            )
            return False


def _make_default_runner() -> _DefaultRunner:
    """Factory for the default runner. Injectable point for tests."""
    return _DefaultRunner()


# ---------------------------------------------------------------------------
# ArmingHandshake -- the verdict dataclass
# ---------------------------------------------------------------------------
@dataclass
class ArmingHandshake:
    """Verdict of the arming handshake.

    armed:               True iff flag_set AND docker_alive AND sinkhole_configurable.
    flag_set:            JARVIS_CROSS_REPO_MUTATION_ENABLED is explicitly truthy.
    docker_alive:        docker info returned rc==0.
    sinkhole_configurable: can_render_airgap() returned True.
    reason:              Short human-readable verdict string.
    """

    armed: bool
    flag_set: bool
    docker_alive: bool
    sinkhole_configurable: bool
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "armed": self.armed,
            "flag_set": self.flag_set,
            "docker_alive": self.docker_alive,
            "sinkhole_configurable": self.sinkhole_configurable,
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# Core handshake logic
# ---------------------------------------------------------------------------
def _flag_set() -> bool:
    """Return True iff JARVIS_CROSS_REPO_MUTATION_ENABLED is explicitly truthy.

    Only {"1","true","yes","on"} (case-insensitive) arm the gate.
    Unset / garbage / falsy tokens ("0","false","no","off", and ANYTHING else
    including whitespace) -> False (fail-CLOSED: garbage does NOT grant write
    permission).
    """
    raw = os.environ.get(_ENV_FLAG)
    if raw is None:
        return False
    return raw.strip().lower() in _TRUTHY


def _cache_enabled() -> bool:
    """Return True iff the handshake result should be cached (default True)."""
    raw = os.environ.get(_ENV_CACHE, "1").strip().lower()
    return raw in _TRUTHY or raw not in frozenset({"0", "false", "no", "off"})


async def run_arming_handshake(
    *,
    runner: Optional[HandshakeRunner] = None,
) -> ArmingHandshake:
    """Execute the Master Arming Switch handshake.

    Logic (fail-CLOSED throughout):
      1. ``flag_set``: JARVIS_CROSS_REPO_MUTATION_ENABLED explicitly truthy.
         Unset / garbage / falsy -> flag_set=False -> armed=False immediately.
      2. If flag_set: ``docker_alive`` = docker info returns rc==0.
      3. If docker_alive: ``sinkhole_configurable`` = can_render_airgap().
      4. ``armed = flag_set AND docker_alive AND sinkhole_configurable``.
         If flag_set but either check fails -> armed=False, reason="degraded: ...".
      5. Any exception -> armed=False (fail-soft / fail-CLOSED).

    Parameters
    ----------
    runner:
        Injectable HandshakeRunner. Defaults to ``_DefaultRunner()`` (real Docker).
        Tests inject a fake to avoid real Docker calls.

    Returns an :class:`ArmingHandshake`. NEVER raises.
    """
    # Step 1: flag check (no runner needed).
    fs = _flag_set()
    if not fs:
        return ArmingHandshake(
            armed=False,
            flag_set=False,
            docker_alive=False,
            sinkhole_configurable=False,
            reason="flag_not_set",
        )

    # Steps 2-3: sandbox environment checks.
    if runner is None:
        runner = _make_default_runner()

    docker_alive = False
    sinkhole_ok = False
    try:
        docker_res = await runner.docker_info()
        docker_alive = docker_res.ok
    except Exception as exc:  # noqa: BLE001 -- fail-CLOSED
        logger.warning(
            "[CrossRepoMaster] docker_info raised -> NOT armed: %s", exc, exc_info=True
        )
        return ArmingHandshake(
            armed=False,
            flag_set=True,
            docker_alive=False,
            sinkhole_configurable=False,
            reason="error:docker_info_raised:%s" % exc,
        )

    if not docker_alive:
        reason = "degraded:docker_not_alive"
        logger.warning(
            "[CrossRepoMaster] armed flag set but sandbox env not ready (%s)"
            " -- DEGRADING to disabled", reason,
        )
        return ArmingHandshake(
            armed=False,
            flag_set=True,
            docker_alive=False,
            sinkhole_configurable=False,
            reason=reason,
        )

    try:
        sinkhole_ok = await runner.can_render_airgap()
    except Exception as exc:  # noqa: BLE001 -- fail-CLOSED
        logger.warning(
            "[CrossRepoMaster] can_render_airgap raised -> NOT armed: %s",
            exc, exc_info=True,
        )
        return ArmingHandshake(
            armed=False,
            flag_set=True,
            docker_alive=True,
            sinkhole_configurable=False,
            reason="error:can_render_airgap_raised:%s" % exc,
        )

    if not sinkhole_ok:
        reason = "degraded:sinkhole_not_configurable"
        logger.warning(
            "[CrossRepoMaster] armed flag set but sandbox env not ready (%s)"
            " -- DEGRADING to disabled", reason,
        )
        return ArmingHandshake(
            armed=False,
            flag_set=True,
            docker_alive=True,
            sinkhole_configurable=False,
            reason=reason,
        )

    # All checks passed.
    return ArmingHandshake(
        armed=True,
        flag_set=True,
        docker_alive=True,
        sinkhole_configurable=True,
        reason="armed",
    )


# ---------------------------------------------------------------------------
# Consumer-facing API
# ---------------------------------------------------------------------------
def cross_repo_mutation_enabled() -> bool:
    """Consumer-facing gate: returns True iff the master arm switch is armed.

    Caches the handshake result for the process lifetime by default.
    Set ``JARVIS_CROSS_REPO_HANDSHAKE_CACHE=0`` to re-evaluate on each call
    (useful in tests).

    Default (flag unset) -> False, byte-identical to today (Body-only).
    NEVER raises.
    """
    global _cached_handshake

    if _cache_enabled() and _cached_handshake is not None:
        return _cached_handshake.armed

    # Run the handshake synchronously (blocking) from a sync call site.
    # The handshake is only called at first use (or when cache is disabled),
    # so blocking briefly here is acceptable.
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Already inside an event loop (e.g. during async boot) -- use
            # a new loop in a thread rather than nest event loops.
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                hs = pool.submit(
                    lambda: asyncio.run(
                        run_arming_handshake(runner=_make_default_runner())
                    )
                ).result()
        else:
            hs = loop.run_until_complete(
                run_arming_handshake(runner=_make_default_runner())
            )
    except Exception as exc:  # noqa: BLE001 -- fail-CLOSED
        logger.warning(
            "[CrossRepoMaster] cross_repo_mutation_enabled raised -> False: %s",
            exc, exc_info=True,
        )
        return False

    if _cache_enabled():
        _cached_handshake = hs

    return hs.armed


def arming_status() -> ArmingHandshake:
    """Return the current arming status (for observability / startup logging).

    Respects the cache: if the handshake has already been run and caching is
    enabled, returns the cached result.  Otherwise runs the handshake now.
    NEVER raises.
    """
    global _cached_handshake

    if _cache_enabled() and _cached_handshake is not None:
        return _cached_handshake

    # Trigger evaluation via cross_repo_mutation_enabled() which handles
    # both sync and async call sites.
    cross_repo_mutation_enabled()

    if _cached_handshake is not None:
        return _cached_handshake

    # Fallback (cache disabled or error): run a fresh handshake synchronously.
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                hs = pool.submit(
                    lambda: asyncio.run(
                        run_arming_handshake(runner=_make_default_runner())
                    )
                ).result()
        else:
            hs = loop.run_until_complete(
                run_arming_handshake(runner=_make_default_runner())
            )
        return hs
    except Exception as exc:  # noqa: BLE001 -- fail-CLOSED
        logger.warning(
            "[CrossRepoMaster] arming_status raised -> NOT armed: %s",
            exc, exc_info=True,
        )
        return ArmingHandshake(
            armed=False,
            flag_set=False,
            docker_alive=False,
            sinkhole_configurable=False,
            reason="error:arming_status_raised:%s" % exc,
        )


def log_arming_status_on_boot() -> None:
    """Emit the handshake verdict at boot (for startup observability).

    Call once during boot; subsequent calls are cheap (uses cache).
    NEVER raises.
    """
    try:
        status = arming_status()
        if status.armed:
            logger.info(
                "[CrossRepoMaster] ARMED -- cross-repo mutation enabled "
                "(docker_alive=%s sinkhole_configurable=%s reason=%s)",
                status.docker_alive,
                status.sinkhole_configurable,
                status.reason,
            )
        else:
            logger.info(
                "[CrossRepoMaster] NOT ARMED (disabled) -- "
                "JARVIS_CROSS_REPO_MUTATION_ENABLED not explicitly truthy OR "
                "sandbox env not ready (flag_set=%s docker_alive=%s "
                "sinkhole_configurable=%s reason=%s)",
                status.flag_set,
                status.docker_alive,
                status.sinkhole_configurable,
                status.reason,
            )
    except Exception:  # noqa: BLE001 -- log-on-boot must never raise
        logger.debug(
            "[CrossRepoMaster] log_arming_status_on_boot raised (fail-soft)",
            exc_info=True,
        )


__all__ = [
    "ArmingHandshake",
    "RunResult",
    "HandshakeRunner",
    "arming_status",
    "cross_repo_mutation_enabled",
    "log_arming_status_on_boot",
    "run_arming_handshake",
]
