"""trinity_docker_runner -- the REAL Docker-compose driver for Guardrail 2.

This is the production path that ERADICATES the simulated seam: instead of an
overlay derived from a static compose + scripted fake runner, it generates the
real air-gapped 3-repo compose (``trinity_compose_generator``), drives
``docker compose up -d``, waits for the **health-gate** (prime + reactor report
healthy), runs the autonomous **handshake suite** against the mutated APIs, and
ALWAYS tears down (``docker compose down -v``) in a ``finally``.

The injected fake runner stays the unit-test path; ``TrinityDockerRunner`` is
the operator-gated real boot.

Discipline (spec sec.4):
  * **Teardown-always.** ``down -v`` fires on every path (pass / fracture /
    timeout / exception). Dead-man: never leave containers/networks running.
  * **Fail-CLOSED.** Compose-up failure, health-gate timeout, handshake fracture
    -> a FRACTURE verdict. A cross-repo mutation can never become less gated
    through an infra failure.
  * **Injectable command boundary.** All ``docker``/subprocess and HTTP lives
    behind injectable callables so tests drive the full up->gate->handshake->
    down sequence with NO real Docker and NO real network.
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, Sequence, Tuple

from backend.core.ouroboros.governance.saga.trinity_compose_generator import (
    generate_trinity_compose,
    serialize_compose,
    assert_sinkhole,
)
from backend.core.ouroboros.governance.saga.trinity_handshake_suite import (
    HandshakeHttpRunner,
    HandshakeResult,
    MutatedEndpoint,
    run_handshake_suite,
)

logger = logging.getLogger("Ouroboros.TrinityDockerRunner")

_ENV_HEALTH_GATE_TIMEOUT_S = "JARVIS_TRINITY_HEALTH_GATE_TIMEOUT_S"
_ENV_HEALTH_POLL_INTERVAL_S = "JARVIS_TRINITY_HEALTH_POLL_INTERVAL_S"
_DEFAULT_HEALTH_GATE_TIMEOUT_S = 180.0
_DEFAULT_HEALTH_POLL_INTERVAL_S = 5.0


def _float_env(name: str, default: float) -> float:
    try:
        return max(1.0, float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


@dataclass
class CmdResult:
    """Result of a single shell command invocation."""

    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


# A command boundary: (argv) -> CmdResult. Injectable for tests.
CmdRunner = Callable[[Sequence[str]], Awaitable[CmdResult]]


async def _default_cmd_runner(argv: Sequence[str]) -> CmdResult:  # pragma: no cover - real subprocess, operator-gated
    import subprocess  # noqa: PLC0415 - lazy, behind boundary

    def _call() -> CmdResult:
        proc = subprocess.run(  # noqa: S603 - argv constructed, never shell
            list(argv), capture_output=True, text=True, timeout=600
        )
        return CmdResult(proc.returncode, proc.stdout or "", proc.stderr or "")

    return await asyncio.to_thread(_call)


@dataclass
class TrinityBootVerdict:
    """Outcome of a real Trinity sandbox boot + handshake."""

    passed: bool
    fracture: bool
    reason: str
    handshake: Optional[HandshakeResult] = None
    sinkhole_ok: bool = False

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "fracture": self.fracture,
            "reason": self.reason,
            "sinkhole_ok": self.sinkhole_ok,
            "handshake": self.handshake.to_dict() if self.handshake else None,
        }


class TrinityDockerRunner:
    """Drives the real air-gapped Trinity sandbox.

    Sequence: generate compose -> static sinkhole assert (fail-CLOSED) ->
    ``up -d`` -> health-gate poll (prime + reactor healthy) -> handshake suite
    against the mutated endpoints -> verdict. ``down -v`` ALWAYS in ``finally``.

    Injectable boundaries (so tests run the whole flow with no Docker/network):
      * ``cmd_runner`` -- runs ``docker`` argv -> :class:`CmdResult`.
      * ``http_runner`` -- the handshake HTTP boundary.
      * ``health_checker`` -- async ``() -> bool`` polled until True or timeout;
        defaults to ``docker compose ps`` healthy-parsing via ``cmd_runner``.
    """

    def __init__(
        self,
        *,
        jarvis_root: str,
        prime_root: str,
        reactor_root: str,
        http_runner: HandshakeHttpRunner,
        mock_port: int = 9900,
        base_image: str = "python:3.11-slim",
        cmd_runner: Optional[CmdRunner] = None,
        health_checker: Optional[Callable[[], Awaitable[bool]]] = None,
        jarvis_url: str = "http://jarvis:8091",
        prime_url: str = "http://prime:8000",
        reactor_url: str = "http://reactor:8090",
    ) -> None:
        self._jarvis_root = jarvis_root
        self._prime_root = prime_root
        self._reactor_root = reactor_root
        self._http_runner = http_runner
        self._mock_port = mock_port
        self._base_image = base_image
        self._cmd_runner = cmd_runner or _default_cmd_runner
        self._health_checker = health_checker
        self._jarvis_url = jarvis_url
        self._prime_url = prime_url
        self._reactor_url = reactor_url

    # ------------------------------------------------------------------ #
    # Compose lifecycle (each behind the injectable cmd boundary)
    # ------------------------------------------------------------------ #
    async def _compose_up(self, compose_path: str) -> CmdResult:
        return await self._cmd_runner(
            ["docker", "compose", "-f", compose_path, "up", "-d"]
        )

    async def _compose_down(self, compose_path: str) -> CmdResult:
        return await self._cmd_runner(
            ["docker", "compose", "-f", compose_path, "down", "-v"]
        )

    async def _default_health_gate(self, compose_path: str) -> bool:
        """Poll ``docker compose ps`` until prime + reactor are healthy.

        Bounded by ``JARVIS_TRINITY_HEALTH_GATE_TIMEOUT_S``. Returns True iff
        BOTH leaves report healthy before the deadline (fail-CLOSED otherwise).
        """
        deadline = _float_env(_ENV_HEALTH_GATE_TIMEOUT_S, _DEFAULT_HEALTH_GATE_TIMEOUT_S)
        interval = _float_env(_ENV_HEALTH_POLL_INTERVAL_S, _DEFAULT_HEALTH_POLL_INTERVAL_S)
        loop = asyncio.get_event_loop()
        start = loop.time()
        while (loop.time() - start) < deadline:
            res = await self._cmd_runner(
                ["docker", "compose", "-f", compose_path, "ps", "--format", "{{.Service}} {{.Health}}"]
            )
            if res.ok and _both_leaves_healthy(res.stdout):
                return True
            await asyncio.sleep(interval)
        return False

    # ------------------------------------------------------------------ #
    # The orchestrated boot
    # ------------------------------------------------------------------ #
    async def run(
        self,
        *,
        mutated_endpoints: Sequence[MutatedEndpoint],
        overlay_writer: Optional[Callable[[str], str]] = None,
    ) -> TrinityBootVerdict:
        """Generate -> sinkhole-assert -> up -> health-gate -> handshake -> down.

        NEVER raises (fail-CLOSED). ``down -v`` ALWAYS runs in ``finally``.
        """
        compose = generate_trinity_compose(
            jarvis_root=self._jarvis_root,
            prime_root=self._prime_root,
            reactor_root=self._reactor_root,
            mock_port=self._mock_port,
            base_image=self._base_image,
        )

        # Static sinkhole guarantee BEFORE any boot (fail-CLOSED).
        sinkhole_ok, sinkhole_reason = assert_sinkhole(compose)
        if not sinkhole_ok:
            return TrinityBootVerdict(
                passed=False,
                fracture=True,
                reason="sinkhole_unverified:%s" % sinkhole_reason,
                sinkhole_ok=False,
            )

        compose_yaml = serialize_compose(compose)
        compose_path = _write_compose(compose_yaml, overlay_writer)

        try:
            up = await self._compose_up(compose_path)
            if not up.ok:
                return TrinityBootVerdict(
                    passed=False,
                    fracture=True,
                    reason="compose_up_failed:%s"
                    % ((up.stderr or up.stdout or "")[:160]),
                    sinkhole_ok=True,
                )

            # HEALTH-GATE: jarvis must not handshake before the leaves are up.
            checker = self._health_checker or (
                lambda: self._default_health_gate(compose_path)
            )
            healthy = await checker()
            if not healthy:
                return TrinityBootVerdict(
                    passed=False,
                    fracture=True,
                    reason="health_gate_timeout",
                    sinkhole_ok=True,
                )

            # Autonomous handshake against the MUTATED endpoints.
            handshake = await run_handshake_suite(
                runner=self._http_runner,
                jarvis_url=self._jarvis_url,
                prime_url=self._prime_url,
                reactor_url=self._reactor_url,
                mutated_endpoints=mutated_endpoints,
            )
            if handshake.fracture or not handshake.passed:
                return TrinityBootVerdict(
                    passed=False,
                    fracture=True,
                    reason="handshake_fracture:%s" % handshake.reason,
                    handshake=handshake,
                    sinkhole_ok=True,
                )
            return TrinityBootVerdict(
                passed=True,
                fracture=False,
                reason="trinity_handshake_ok_air_gapped",
                handshake=handshake,
                sinkhole_ok=True,
            )
        except Exception as exc:  # any uncertainty -> FRACTURE
            logger.warning(
                "[TrinityDockerRunner] boot raised -> FRACTURE: %s", exc, exc_info=True
            )
            return TrinityBootVerdict(
                passed=False,
                fracture=True,
                reason="boot_error:%s" % exc,
                sinkhole_ok=True,
            )
        finally:
            # TEARDOWN-ALWAYS (dead-man discipline). Once a compose_path exists,
            # an `up` may have partially started containers (incl. when it
            # RAISED) -> always attempt `down -v`. ``down`` on a never-upped
            # compose is harmless/idempotent. Fail-soft (never re-raise).
            try:
                await self._compose_down(compose_path)
            except Exception:
                logger.warning(
                    "[TrinityDockerRunner] teardown (down -v) failed (fail-soft)",
                    exc_info=True,
                )


def _both_leaves_healthy(ps_stdout: str) -> bool:
    """Parse ``docker compose ps`` output: prime AND reactor both 'healthy'."""
    seen_healthy = set()
    for line in (ps_stdout or "").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        service, health = parts[0].strip().lower(), parts[1].strip().lower()
        if service in ("prime", "reactor") and health == "healthy":
            seen_healthy.add(service)
    return {"prime", "reactor"}.issubset(seen_healthy)


def _write_compose(
    compose_yaml: str, overlay_writer: Optional[Callable[[str], str]]
) -> str:
    if overlay_writer is not None:
        return overlay_writer(compose_yaml)
    fd, path = tempfile.mkstemp(prefix="trinity_real_", suffix=".yml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(compose_yaml)
    except Exception:
        try:
            os.close(fd)
        except Exception:
            pass
    return path


__all__ = [
    "CmdResult",
    "CmdRunner",
    "TrinityBootVerdict",
    "TrinityDockerRunner",
]
