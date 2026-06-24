"""trinity_integration_gate — Guardrail 2 of the Sovereign Cross-Repo Mutator.

The air-gapped Trinity integration sandbox. A cross-repo mutation that passes
unit tests (CrossRepoVerifier Tier 1/2/3) can still *fracture the organism* —
break the Body<->Mind<->Nerves handshake. This gate spins an ephemeral,
**network-air-gapped** Docker network with all three repos built from the
mutated tree, asserts the Trinity handshake still works **without any live
DW/Claude/GCP calls** (egress sinkhole -> synthetic mock), and yields
``[SOVEREIGN YIELD: CROSS-REPO FRACTURE]`` on failure.

Composition point: runs in the cross-repo verify path AFTER the existing
``CrossRepoVerifier`` structure/compilation/integration tiers pass (spec §4).
A FRACTURE verdict tells the saga to abort -> compensating rollback (existing),
so the op is sealed/terminal, never half-applied across repos.

Design invariants (spec §4, §6):
  * **Fail-CLOSED.** ANY uncertainty -> FRACTURE, never a silent pass. Docker
    absent, ``up`` fails, air-gap cannot be asserted, handshake times out ->
    all FRACTURE. A cross-repo mutation can never become *less* gated through
    a failure.
  * **Air-gap assertion is mandatory and runs FIRST.** Before trusting any
    integration result we prove (a) the compose network is ``internal: true``
    and (b) a container CANNOT resolve/reach a real provider host. If either
    cannot be asserted we refuse to run the integration test at all.
  * **Teardown-always.** ``docker compose down -v`` runs in a ``finally`` on
    every path (timeout / exception / pass / fracture). Dead-man discipline:
    never leave sandbox containers or networks running. Teardown errors are
    fail-soft (logged, never re-raised).
  * **Injectable runner.** All Docker / subprocess behaviour lives behind a
    single ``DockerRunner`` protocol so tests inject a fake -> NO real Docker
    in tests.

Env knobs (no hardcoding):
  * ``JARVIS_TRINITY_SANDBOX_GATE_ENABLED`` (default true) — gate master switch.
  * ``JARVIS_TRINITY_SANDBOX_TIMEOUT_S`` (default 300) — spin/handshake bound.
  * ``JARVIS_TRINITY_SANDBOX_EGRESS_PORT`` (default 8099) — egress-mock port.
  * ``JARVIS_TRINITY_SANDBOX_NETWORK`` (default ``trinity_sandbox_net``).

NOTE on the master cross-repo flag: this gate is only reached when a cross-repo
mutation is being validated, which today requires ``JARVIS_CROSS_REPO_MUTATION_ENABLED``
(default OFF). So in production this gate is unreachable until that master flag
is flipped. When *this* gate's own flag is OFF we return a no-op pass verdict
(the surrounding cross-repo path is itself gated off, so this cannot silently
pass a real cross-repo op).
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, Tuple

try:  # PyYAML is available across the soak/prod images.
    import yaml  # type: ignore
except Exception:  # pragma: no cover - degraded path; air-gap render falls back
    yaml = None  # type: ignore

logger = logging.getLogger("Ouroboros.TrinitySandboxGate")

# --------------------------------------------------------------------------- #
# Env knobs
# --------------------------------------------------------------------------- #
_ENV_GATE_ENABLED = "JARVIS_TRINITY_SANDBOX_GATE_ENABLED"
_ENV_TIMEOUT_S = "JARVIS_TRINITY_SANDBOX_TIMEOUT_S"
_ENV_EGRESS_PORT = "JARVIS_TRINITY_SANDBOX_EGRESS_PORT"
_ENV_NETWORK = "JARVIS_TRINITY_SANDBOX_NETWORK"

_DEFAULT_TIMEOUT_S = 300.0
_DEFAULT_EGRESS_PORT = 8099
_DEFAULT_NETWORK = "trinity_sandbox_net"

_TRUTHY = {"1", "true", "yes", "on"}

# The egress-mock service name; the ONLY reachable "external" endpoint in the
# air-gapped network. Provider base URLs are pointed here.
EGRESS_MOCK_SERVICE = "egress-mock"

# Live provider hosts the air-gap MUST NOT be able to reach. The probe asserts
# every one of these is unreachable from inside a sandbox container.
LIVE_PROVIDER_HOSTS: Tuple[str, ...] = (
    "api.anthropic.com",
    "api.doubleword.ai",
    "metadata.google.internal",
)


def gate_enabled() -> bool:
    """``JARVIS_TRINITY_SANDBOX_GATE_ENABLED`` (default true)."""
    raw = os.environ.get(_ENV_GATE_ENABLED, "true").strip().lower()
    return raw in _TRUTHY


def _timeout_s() -> float:
    try:
        return max(1.0, float(os.environ.get(_ENV_TIMEOUT_S, _DEFAULT_TIMEOUT_S)))
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT_S


def _egress_port() -> int:
    try:
        return int(os.environ.get(_ENV_EGRESS_PORT, _DEFAULT_EGRESS_PORT))
    except (TypeError, ValueError):
        return _DEFAULT_EGRESS_PORT


def _network_name() -> str:
    return os.environ.get(_ENV_NETWORK, _DEFAULT_NETWORK).strip() or _DEFAULT_NETWORK


# --------------------------------------------------------------------------- #
# Verdict
# --------------------------------------------------------------------------- #
@dataclass
class SandboxVerdict:
    """Outcome of the air-gapped Trinity sandbox gate."""

    passed: bool
    fracture: bool
    reason: str
    air_gapped: bool
    handshake_ok: bool
    containers: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "fracture": self.fracture,
            "reason": self.reason,
            "air_gapped": self.air_gapped,
            "handshake_ok": self.handshake_ok,
            "containers": list(self.containers),
        }


# --------------------------------------------------------------------------- #
# Runner boundary — ALL docker/subprocess lives behind this protocol
# --------------------------------------------------------------------------- #
@dataclass
class RunResult:
    """Result of a single runner invocation."""

    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class DockerRunner(Protocol):
    """Injectable boundary around ``docker compose`` / probe commands.

    Tests provide a fake implementation that records commands and returns
    scripted results — so the test-suite touches no real Docker.
    """

    async def compose_up(self, compose_path: str) -> RunResult:
        """``docker compose -f <compose_path> up -d --build`` (detached)."""

    async def compose_down(self, compose_path: str) -> RunResult:
        """``docker compose -f <compose_path> down -v`` (teardown)."""

    async def inspect_network(self, network: str) -> RunResult:
        """Inspect a network; stdout should reveal ``internal`` truthiness."""

    async def probe_provider_host(self, service: str, host: str) -> RunResult:
        """Probe (DNS/connect) ``host`` from inside ``service``.

        Returns ``returncode != 0`` when the host is UNREACHABLE — which is the
        REQUIRED outcome for a properly air-gapped network.
        """

    async def health_handshake(self, service: str) -> RunResult:
        """Poll the Trinity health/handshake endpoint inside ``service``.

        ``returncode == 0`` => all-green handshake (Body<->Mind<->Nerves talk
        through the sinkhole, all provider calls resolved to the mock).
        """


class _DockerRunner:
    """Default real runner wrapping ``docker compose`` via subprocess.

    Never imported at module load except lazily inside ``run_trinity_sandbox_gate``
    so the gate module imports clean in test/sandbox environments without Docker.
    Operator-gated: a real sandbox run requires explicit operator action.
    """

    def __init__(self, *, network: str, egress_port: int) -> None:
        self._network = network
        self._egress_port = egress_port

    async def _run(self, argv: List[str], *, timeout: float = 120.0) -> RunResult:
        import subprocess  # noqa: PLC0415 - lazy, behind the boundary

        def _call() -> RunResult:
            proc = subprocess.run(  # noqa: S603 - argv is constructed, never shell
                argv, capture_output=True, text=True, timeout=timeout
            )
            return RunResult(proc.returncode, proc.stdout or "", proc.stderr or "")

        return await asyncio.to_thread(_call)

    async def compose_up(self, compose_path: str) -> RunResult:
        return await self._run(
            ["docker", "compose", "-f", compose_path, "up", "-d", "--build"],
            timeout=_timeout_s(),
        )

    async def compose_down(self, compose_path: str) -> RunResult:
        return await self._run(
            ["docker", "compose", "-f", compose_path, "down", "-v"]
        )

    async def inspect_network(self, network: str) -> RunResult:
        return await self._run(
            ["docker", "network", "inspect", "-f", "{{.Internal}}", network]
        )

    async def probe_provider_host(self, service: str, host: str) -> RunResult:
        # A connect attempt that MUST fail in an air-gapped network. Uses python
        # so we don't depend on curl/nc being installed in the sandbox image.
        snippet = (
            "import socket,sys\n"
            "try:\n"
            "    socket.setdefaulttimeout(3)\n"
            "    socket.create_connection((%r,443),3).close()\n"
            "    sys.exit(0)\n"
            "except Exception:\n"
            "    sys.exit(7)\n"
        ) % host
        return await self._run(
            ["docker", "compose", "exec", "-T", service, "python3", "-c", snippet]
        )

    async def health_handshake(self, service: str) -> RunResult:
        snippet = (
            "import json,sys,urllib.request\n"
            "try:\n"
            "    r=urllib.request.urlopen('http://localhost:8000/trinity/health',timeout=5)\n"
            "    d=json.loads(r.read().decode())\n"
            "    sys.exit(0 if d.get('all_green') else 11)\n"
            "except Exception:\n"
            "    sys.exit(12)\n"
        )
        return await self._run(
            ["docker", "compose", "exec", "-T", service, "python3", "-c", snippet]
        )


# --------------------------------------------------------------------------- #
# Air-gap compose overlay generation (pure — testable)
# --------------------------------------------------------------------------- #
def _provider_url_overrides(*, mock_port: int) -> Dict[str, str]:
    """Env overrides pointing every provider base URL at the egress mock.

    These are injected into EVERY service so no container can talk to a live
    provider — the only reachable external endpoint is the egress mock.
    """
    base = "http://%s:%d" % (EGRESS_MOCK_SERVICE, mock_port)
    return {
        "DOUBLEWORD_BASE_URL": base,
        "ANTHROPIC_BASE_URL": base,
        "CLAUDE_BASE_URL": base,
        "GCP_METADATA_HOST": "%s:%d" % (EGRESS_MOCK_SERVICE, mock_port),
        "GCE_METADATA_HOST": "%s:%d" % (EGRESS_MOCK_SERVICE, mock_port),
        # Belt-and-suspenders: even if a code path reads a raw host, it lands
        # on the in-network mock, never the internet.
        "JARVIS_EGRESS_SINKHOLE": base,
    }


def build_airgap_compose(
    base_compose_path: str,
    *,
    mock_port: Optional[int] = None,
    network: Optional[str] = None,
    egress_mock_script: str = "/app/scripts/trinity_sandbox_egress_mock.py",
) -> str:
    """Render an air-gapped overlay compose from an existing base compose.

    Pure string/dict assembly (no Docker, no I/O beyond reading the base file)
    so it is fully unit-testable. Produces YAML that:
      * declares the sandbox network ``internal: true`` (no host/internet route);
      * attaches every existing service to that network;
      * injects the provider-URL env overrides into every service (so all
        DW/Claude/GCP calls resolve to the egress mock);
      * adds the single ``egress-mock`` service (the only reachable "external"
        endpoint), also on the internal network.

    Raises ``RuntimeError`` if the base compose cannot be parsed — fail-CLOSED:
    a gate that cannot render a provably-air-gapped overlay must not run.
    """
    port = mock_port if mock_port is not None else _egress_port()
    net = (network or _network_name()).strip() or _DEFAULT_NETWORK

    if yaml is None:  # pragma: no cover - PyYAML missing
        raise RuntimeError("build_airgap_compose requires PyYAML to parse the base compose")

    try:
        with open(base_compose_path, "r", encoding="utf-8") as fh:
            base = yaml.safe_load(fh) or {}
    except FileNotFoundError as exc:
        raise RuntimeError("base compose not found: %s" % base_compose_path) from exc
    except Exception as exc:  # malformed YAML -> fail-CLOSED
        raise RuntimeError("base compose unparseable: %s" % exc) from exc

    if not isinstance(base, dict):
        raise RuntimeError("base compose is not a mapping")

    services = dict(base.get("services") or {})
    overrides = _provider_url_overrides(mock_port=port)

    out_services: Dict[str, Any] = {}
    for name, svc in services.items():
        svc = dict(svc or {})
        # Normalise environment to a dict so we can layer overrides.
        env = svc.get("environment")
        env_map: Dict[str, Any] = {}
        if isinstance(env, dict):
            env_map.update(env)
        elif isinstance(env, list):
            for item in env:
                if isinstance(item, str) and "=" in item:
                    k, v = item.split("=", 1)
                    env_map[k] = v
                elif isinstance(item, str):
                    env_map[item] = ""
        env_map.update(overrides)
        svc["environment"] = env_map
        # Attach to the internal network ONLY (replace any host networking).
        svc["networks"] = [net]
        out_services[name] = svc

    # The egress sinkhole service — the only reachable external endpoint.
    out_services[EGRESS_MOCK_SERVICE] = {
        "image": "python:3.11-slim",
        "container_name": "trinity_sandbox_egress_mock",
        "command": [
            "python3",
            egress_mock_script,
            "--port",
            str(port),
        ],
        "volumes": ["./scripts:/app/scripts:ro"],
        "environment": {"TRINITY_SANDBOX_EGRESS_PORT": str(port)},
        "networks": [net],
    }

    overlay: Dict[str, Any] = {
        "services": out_services,
        "networks": {
            net: {
                # The load-bearing air-gap declaration: no route to host/internet.
                "internal": True,
                "driver": "bridge",
            }
        },
    }

    return yaml.safe_dump(overlay, sort_keys=False, default_flow_style=False)


def _rendered_overlay_is_air_gapped(overlay_yaml: str, *, network: str) -> bool:
    """Static check on the RENDERED overlay: the network is internal AND no
    service env exposes a live provider host. Pure (no Docker)."""
    if yaml is None:  # pragma: no cover
        return False
    try:
        doc = yaml.safe_load(overlay_yaml) or {}
    except Exception:
        return False
    nets = doc.get("networks") or {}
    net = nets.get(network) or {}
    if net.get("internal") is not True:
        return False
    # No service env may point a provider base URL at a live host.
    services = doc.get("services") or {}
    for svc in services.values():
        env = (svc or {}).get("environment") or {}
        if not isinstance(env, dict):
            continue
        for val in env.values():
            sval = str(val)
            for host in LIVE_PROVIDER_HOSTS:
                if host in sval:
                    return False
    return True


# --------------------------------------------------------------------------- #
# Air-gap assertion (fail-CLOSED)
# --------------------------------------------------------------------------- #
async def assert_air_gapped(
    runner: DockerRunner,
    *,
    network: str,
    services: Tuple[str, ...],
    overlay_yaml: Optional[str] = None,
) -> bool:
    """Prove the sandbox network is actually isolated.

    Returns True ONLY when BOTH hold:
      (a) the network is declared/instantiated ``internal: true``; and
      (b) from inside a sandbox container, a connect to EVERY live provider
          host FAILS (the probe returncode is non-zero => unreachable).

    Any uncertainty (inspect fails, probe SUCCEEDS in reaching a host, no
    service to probe, exception) -> returns False => the caller treats it as a
    FRACTURE and refuses to run the integration test. Never raises.
    """
    try:
        # (Optional) static pre-check on the rendered overlay.
        if overlay_yaml is not None and not _rendered_overlay_is_air_gapped(
            overlay_yaml, network=network
        ):
            logger.warning(
                "[TrinitySandboxGate] rendered overlay is NOT air-gapped "
                "(network not internal or provider host leaked) -> FRACTURE"
            )
            return False

        # (a) Network must be internal.
        net_res = await runner.inspect_network(network)
        internal_truthy = net_res.ok and "true" in (net_res.stdout or "").strip().lower()
        if not internal_truthy:
            logger.warning(
                "[TrinitySandboxGate] network %s not internal (rc=%s out=%r) -> FRACTURE",
                network,
                getattr(net_res, "returncode", "?"),
                (net_res.stdout or "")[:80],
            )
            return False

        # (b) From inside a container, EVERY live provider host must be UNREACHABLE.
        if not services:
            logger.warning(
                "[TrinitySandboxGate] no service to probe air-gap -> FRACTURE"
            )
            return False
        probe_service = services[0]
        for host in LIVE_PROVIDER_HOSTS:
            res = await runner.probe_provider_host(probe_service, host)
            # ok == reachable == BAD. We REQUIRE failure (non-zero).
            if res.ok:
                logger.warning(
                    "[TrinitySandboxGate] live host %s REACHABLE from %s "
                    "(air-gap breached) -> FRACTURE",
                    host,
                    probe_service,
                )
                return False
        return True
    except Exception:
        logger.warning(
            "[TrinitySandboxGate] air-gap assertion raised -> FRACTURE",
            exc_info=True,
        )
        return False


# --------------------------------------------------------------------------- #
# FRACTURE yield
# --------------------------------------------------------------------------- #
def _emit_fracture(op_id: str) -> None:
    """Emit ``[SOVEREIGN YIELD: CROSS-REPO FRACTURE]`` — reuse, fail-soft."""
    try:
        from backend.core.ouroboros.governance.convergence_watchdog import (  # noqa: PLC0415
            emit_sovereign_yield,
        )

        emit_sovereign_yield(
            op_id,
            lineage_id=op_id,
            ratio=0.0,
            consecutive_stalls=0,
            parent_chars=0,
            child_chars=0,
            tier="cross_repo",
            reason="CROSS-REPO FRACTURE",
        )
    except Exception:  # pragma: no cover - telemetry is best-effort
        logger.debug("[TrinitySandboxGate] emit_sovereign_yield fail-soft", exc_info=True)


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #
async def run_trinity_sandbox_gate(
    *,
    candidate_root: str,
    op_id: str,
    base_compose_path: Optional[str] = None,
    services: Tuple[str, ...] = ("jarvis", "jarvis-prime", "reactor-core"),
    runner: Optional[DockerRunner] = None,
    overlay_writer: Optional[Any] = None,
) -> SandboxVerdict:
    """Run the air-gapped Trinity integration sandbox gate.

    Parameters
    ----------
    candidate_root:
        The mutated tree root (all 3 repos built from it). Used to resolve the
        base compose when ``base_compose_path`` is not given.
    op_id:
        The cross-repo operation id (for the FRACTURE yield + telemetry).
    base_compose_path:
        Path to the existing multi-repo compose to derive the overlay from.
        Defaults to ``<candidate_root>/docker-compose.soak.yml``.
    services:
        Expected service names (for probing/handshake). The real services come
        from the rendered overlay; this drives which container we probe.
    runner:
        Injectable Docker boundary. Defaults to the real ``_DockerRunner``.
        Tests inject a fake -> no real Docker.
    overlay_writer:
        Optional callable ``(yaml_str) -> path`` to persist the overlay; defaults
        to a temp file. Injectable so tests avoid disk.

    Returns a :class:`SandboxVerdict`. NEVER raises (fail-CLOSED: any uncertainty
    -> FRACTURE). Teardown (``compose down -v``) ALWAYS runs in ``finally``.
    """
    # OFF -> no-op pass. See module docstring: the surrounding cross-repo path is
    # itself master-gated OFF, so this cannot silently pass a real cross-repo op.
    if not gate_enabled():
        return SandboxVerdict(
            passed=True,
            fracture=False,
            reason="gate_disabled",
            air_gapped=False,
            handshake_ok=False,
            containers=(),
        )

    if runner is None:  # pragma: no cover - real Docker path, operator-gated
        runner = _DockerRunner(network=_network_name(), egress_port=_egress_port())

    network = _network_name()
    base = base_compose_path or os.path.join(candidate_root, "docker-compose.soak.yml")

    overlay_path: Optional[str] = None
    up_done = False
    try:
        # 1. Render the air-gapped overlay (pure). Render failure -> FRACTURE.
        try:
            overlay_yaml = build_airgap_compose(base, network=network)
        except Exception as exc:
            logger.warning(
                "[TrinitySandboxGate] overlay render failed -> FRACTURE: %s", exc
            )
            _emit_fracture(op_id)
            return SandboxVerdict(
                passed=False,
                fracture=True,
                reason="overlay_render_failed:%s" % exc,
                air_gapped=False,
                handshake_ok=False,
                containers=(),
            )

        # Persist the overlay so `docker compose -f <overlay>` can read it.
        overlay_path = _write_overlay(overlay_yaml, overlay_writer, candidate_root)

        # 2/3. Spin + air-gap-assert + handshake, all bounded by the timeout.
        async def _spin_and_check() -> SandboxVerdict:
            nonlocal up_done
            # 3. Spin. up failure (incl. Docker absent) -> FRACTURE (fail-CLOSED).
            up = await runner.compose_up(overlay_path)  # type: ignore[arg-type]
            up_done = True  # mark so teardown always fires even on partial up
            if not up.ok:
                logger.warning(
                    "[TrinitySandboxGate] compose up failed (rc=%s) -> FRACTURE: %s",
                    up.returncode,
                    (up.stderr or up.stdout or "")[:200],
                )
                _emit_fracture(op_id)
                return SandboxVerdict(
                    passed=False,
                    fracture=True,
                    reason="compose_up_failed",
                    air_gapped=False,
                    handshake_ok=False,
                    containers=services,
                )

            # 2. AIR-GAP ASSERTION (fail-CLOSED, runs BEFORE trusting any result).
            air_gapped = await assert_air_gapped(
                runner,
                network=network,
                services=services,
                overlay_yaml=overlay_yaml,
            )
            if not air_gapped:
                logger.warning(
                    "[TrinitySandboxGate] air-gap could NOT be asserted -> "
                    "refusing integration test, treating as FRACTURE"
                )
                _emit_fracture(op_id)
                return SandboxVerdict(
                    passed=False,
                    fracture=True,
                    reason="air_gap_unverified",
                    air_gapped=False,
                    handshake_ok=False,
                    containers=services,
                )

            # 4. Handshake assert (only AFTER air-gap is proven).
            hs = await runner.health_handshake(services[0])
            handshake_ok = hs.ok
            if not handshake_ok:
                logger.warning(
                    "[TrinitySandboxGate] Trinity handshake FAILED (rc=%s) -> FRACTURE",
                    hs.returncode,
                )
                _emit_fracture(op_id)
                return SandboxVerdict(
                    passed=False,
                    fracture=True,
                    reason="handshake_failed",
                    air_gapped=True,
                    handshake_ok=False,
                    containers=services,
                )

            # 5. Verdict: handshake OK + air-gapped -> PASS.
            return SandboxVerdict(
                passed=True,
                fracture=False,
                reason="handshake_ok_air_gapped",
                air_gapped=True,
                handshake_ok=True,
                containers=services,
            )

        try:
            return await asyncio.wait_for(_spin_and_check(), timeout=_timeout_s())
        except asyncio.TimeoutError:
            logger.warning(
                "[TrinitySandboxGate] sandbox timed out after %ss -> FRACTURE",
                _timeout_s(),
            )
            _emit_fracture(op_id)
            return SandboxVerdict(
                passed=False,
                fracture=True,
                reason="sandbox_timeout",
                air_gapped=False,
                handshake_ok=False,
                containers=services,
            )

    except Exception as exc:  # any uncertainty -> FRACTURE (fail-CLOSED)
        logger.warning(
            "[TrinitySandboxGate] unexpected error -> FRACTURE: %s", exc, exc_info=True
        )
        _emit_fracture(op_id)
        return SandboxVerdict(
            passed=False,
            fracture=True,
            reason="sandbox_error:%s" % exc,
            air_gapped=False,
            handshake_ok=False,
            containers=services,
        )
    finally:
        # TEARDOWN-ALWAYS (dead-man discipline). Never leave containers/networks
        # running. Fail-soft on teardown error. Fires whenever an `up` may have
        # been attempted, even on timeout/exception.
        if overlay_path is not None and (up_done or runner is not None):
            try:
                await runner.compose_down(overlay_path)
            except Exception:
                logger.warning(
                    "[TrinitySandboxGate] teardown (compose down -v) failed "
                    "(fail-soft, leaving telemetry)",
                    exc_info=True,
                )


# --------------------------------------------------------------------------- #
# REAL sandbox gate -- drives the generated 3-repo compose + handshake suite
# --------------------------------------------------------------------------- #
async def run_real_trinity_sandbox_gate(
    *,
    op_id: str,
    jarvis_root: str,
    prime_root: str,
    reactor_root: str,
    mutated_endpoints: Any,
    http_runner: Any,
    mock_port: int = 9900,
    base_image: str = "python:3.11-slim",
    cmd_runner: Optional[Any] = None,
    health_checker: Optional[Any] = None,
    overlay_writer: Optional[Any] = None,
) -> SandboxVerdict:
    """Production G2 path: REAL air-gapped 3-repo Trinity sandbox.

    Replaces the simulated overlay path: generates the real compose
    (``trinity_compose_generator``), drives ``TrinityDockerRunner`` (up ->
    health-gate -> handshake suite -> teardown-always), and maps the boot
    verdict onto :class:`SandboxVerdict`.

    The roots come from ``RepoRegistry.from_env()`` at the call site (no
    hardcoded paths). Fail-CLOSED: any fracture emits the SOVEREIGN YIELD.
    When the gate flag is OFF, returns a no-op pass (the surrounding cross-repo
    path is itself master-gated OFF).
    """
    if not gate_enabled():
        return SandboxVerdict(
            passed=True,
            fracture=False,
            reason="gate_disabled",
            air_gapped=False,
            handshake_ok=False,
            containers=(),
        )

    # Lazy import to keep this module import-clean without the compose deps.
    from backend.core.ouroboros.governance.saga.trinity_docker_runner import (  # noqa: PLC0415
        TrinityDockerRunner,
    )

    runner = TrinityDockerRunner(
        jarvis_root=jarvis_root,
        prime_root=prime_root,
        reactor_root=reactor_root,
        http_runner=http_runner,
        mock_port=mock_port,
        base_image=base_image,
        cmd_runner=cmd_runner,
        health_checker=health_checker,
    )

    async def _drive() -> SandboxVerdict:
        boot = await runner.run(
            mutated_endpoints=mutated_endpoints,
            overlay_writer=overlay_writer,
        )
        if boot.fracture or not boot.passed:
            _emit_fracture(op_id)
        return SandboxVerdict(
            passed=boot.passed,
            fracture=boot.fracture,
            reason=boot.reason,
            air_gapped=boot.sinkhole_ok,
            handshake_ok=bool(boot.handshake and boot.handshake.passed),
            containers=("jarvis", "prime", "reactor", EGRESS_MOCK_SERVICE),
        )

    try:
        return await asyncio.wait_for(_drive(), timeout=_timeout_s())
    except asyncio.TimeoutError:
        logger.warning(
            "[TrinitySandboxGate] real sandbox timed out after %ss -> FRACTURE",
            _timeout_s(),
        )
        _emit_fracture(op_id)
        return SandboxVerdict(
            passed=False,
            fracture=True,
            reason="real_sandbox_timeout",
            air_gapped=False,
            handshake_ok=False,
            containers=(),
        )
    except Exception as exc:  # fail-CLOSED
        logger.warning(
            "[TrinitySandboxGate] real sandbox error -> FRACTURE: %s", exc, exc_info=True
        )
        _emit_fracture(op_id)
        return SandboxVerdict(
            passed=False,
            fracture=True,
            reason="real_sandbox_error:%s" % exc,
            air_gapped=False,
            handshake_ok=False,
            containers=(),
        )


def _write_overlay(
    overlay_yaml: str, overlay_writer: Optional[Any], candidate_root: str
) -> str:
    """Persist the overlay; injectable writer for tests. Returns the path."""
    if overlay_writer is not None:
        return overlay_writer(overlay_yaml)
    import tempfile  # noqa: PLC0415

    fd, path = tempfile.mkstemp(
        prefix="trinity_airgap_", suffix=".yml", dir=None
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(overlay_yaml)
    except Exception:
        try:
            os.close(fd)
        except Exception:
            pass
    return path
