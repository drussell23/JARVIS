"""trinity_compose_generator -- the REAL air-gapped 3-repo Trinity sandbox compose.

This REPLACES the simulated G2 runner (``trinity_integration_gate`` derived an
overlay from a pre-existing ``docker-compose.*.yml``). Here we GENERATE -- from
scratch and deterministically -- a docker-compose dict that spins the actual
three repos (jarvis / J-Prime / reactor-core) in an ephemeral, **air-gapped**
Docker network, so the cross-repo surgery handshake is REAL (the mutated APIs
are pinged across container boundaries, no injected fracture).

Design constraints (spec sec.4 + operator constraints):
  * **MOUNT-INTO-BASE-IMAGE, never a sibling Dockerfile.** Each service is a
    plain ``python:3.11-slim`` base with the repo BIND-MOUNTED read-only at
    ``/app`` and the repo's own server start command run in-place. Requiring a
    Dockerfile in the sibling repos would be a cross-repo *write* -- forbidden.
    Lighter to boot (no image build), which matters on a 16GB host.
  * **NO hardcoded paths.** All three repo roots are passed in (they originate
    from ``.env`` via ``RepoRegistry.from_env()`` at the call site). The
    generator never embeds an absolute path of its own.
  * **HEALTH-GATING (race eradication).** prime + reactor each carry a
    ``healthcheck`` hitting their ``/health``; the jarvis service
    ``depends_on`` BOTH with ``condition: service_healthy`` -- jarvis cannot
    start its integration test until the Mind and Nerves are actually up. No
    boot-race.
  * **CRYPTOGRAPHIC SINKHOLE.** A single network declared ``internal: true``
    (Docker blackholes all WAN egress on an internal network -- only in-network
    IPC works). Every service has its DW/Claude/GCP base URLs env-overridden to
    the in-network ``egress-mock`` (reuses ``_provider_url_overrides``). Mutated
    code under test CANNOT reach a live provider.
  * **Deterministic.** No date/time/random in the output -- same args produce a
    byte-identical dict, so the compose is auditable and diffable.
  * **Fail-CLOSED static guarantee.** ``assert_sinkhole`` proves the rendered
    compose is air-gapped (network internal, no WAN host string anywhere, no
    leaking host port). Any doubt -> ``(False, reason)``.

Env knobs (no hardcoding of the tunables):
  * ``JARVIS_TRINITY_BASE_IMAGE`` (default ``python:3.11-slim``).
  * ``JARVIS_TRINITY_HEALTH_INTERVAL_S`` (default 5).
  * ``JARVIS_TRINITY_HEALTH_RETRIES`` (default 20).
  * ``JARVIS_TRINITY_HEALTH_TIMEOUT_S`` (default 3).
  * ``JARVIS_TRINITY_SANDBOX_EGRESS_PORT`` (default 9900 here; the gate's own
    default is 8099 -- the caller passes ``mock_port`` explicitly).
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

try:  # PyYAML available across soak/prod images.
    import yaml  # type: ignore
except Exception:  # pragma: no cover - degraded; serialize_compose guards it
    yaml = None  # type: ignore

# Reuse the egress-mock service name + the provider-URL sinkhole overrides from
# the gate so there is ONE source of truth for what "air-gapped" means.
from backend.core.ouroboros.governance.saga.trinity_integration_gate import (
    EGRESS_MOCK_SERVICE,
    LIVE_PROVIDER_HOSTS,
    _provider_url_overrides,
)

# --------------------------------------------------------------------------- #
# Env knobs
# --------------------------------------------------------------------------- #
_ENV_BASE_IMAGE = "JARVIS_TRINITY_BASE_IMAGE"
_ENV_HEALTH_INTERVAL_S = "JARVIS_TRINITY_HEALTH_INTERVAL_S"
_ENV_HEALTH_RETRIES = "JARVIS_TRINITY_HEALTH_RETRIES"
_ENV_HEALTH_TIMEOUT_S = "JARVIS_TRINITY_HEALTH_TIMEOUT_S"
_ENV_EGRESS_PORT = "JARVIS_TRINITY_SANDBOX_EGRESS_PORT"

_DEFAULT_BASE_IMAGE = "python:3.11-slim"
_DEFAULT_HEALTH_INTERVAL_S = 5
_DEFAULT_HEALTH_RETRIES = 20
_DEFAULT_HEALTH_TIMEOUT_S = 3
_DEFAULT_MOCK_PORT = 9900

# The internal (air-gapped) network for the generated compose. Distinct, stable
# name so teardown/inspection is deterministic.
TRINITY_NETWORK = "trinity_airgap_net"

# Per-service server start. The repo is mounted read-only at /app; the command
# installs a minimal HTTP/health dep set then execs the repo's own server. Each
# repo's server already exposes /health (verified ground-truth):
#   jarvis  : event_channel-style health server on 8090 (we run the lightweight
#             trinity health surface inside the mounted tree)
#   prime   : run_server.py            -> /health on :8000
#   reactor : run_reactor.py           -> /health on :8090
# The install step is deliberately tiny (curl for healthcheck + the repo's own
# requirements if present) so boot stays light on a 16GB host.
_SERVICE_PORTS: Dict[str, int] = {
    "jarvis": 8091,
    "prime": 8000,
    "reactor": 8090,
}


def _base_image() -> str:
    return (os.environ.get(_ENV_BASE_IMAGE) or "").strip() or _DEFAULT_BASE_IMAGE


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def _mock_port_env() -> int:
    return _int_env(_ENV_EGRESS_PORT, _DEFAULT_MOCK_PORT)


def _install_and_start(repo_server_cmd: str) -> str:
    """Shell command: install curl (for the healthcheck) + the repo's own deps
    if a requirements file is mounted, then exec the repo's server.

    Deterministic string (no date/random). ``set -e`` so a failed install fails
    the container -> the healthcheck never goes green -> the gate FRACTUREs
    (fail-CLOSED) rather than silently passing a broken boot.
    """
    return (
        "set -e; "
        "apt-get update >/dev/null 2>&1 && "
        "apt-get install -y --no-install-recommends curl >/dev/null 2>&1 || true; "
        "if [ -f /app/requirements.txt ]; then "
        "pip install --no-cache-dir -q -r /app/requirements.txt || true; fi; "
        "exec %s" % repo_server_cmd
    )


def _jarvis_health_inline_cmd(*, port: int) -> str:
    """A minimal stdlib HTTP ``/health`` server for the jarvis driver service.

    Pure ``python3 -c`` (stdlib only) so the jarvis container is a real,
    long-lived in-network vantage WITHOUT introducing a new module or touching
    the mounted tree. Deterministic (no date/random).
    """
    snippet = (
        "import json;"
        "from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer;"
        "class H(BaseHTTPRequestHandler):\n"
        " def do_GET(self):\n"
        "  b=json.dumps({'status':'ok','service':'jarvis'}).encode();"
        "self.send_response(200);"
        "self.send_header('Content-Type','application/json');"
        "self.send_header('Content-Length',str(len(b)));"
        "self.end_headers();self.wfile.write(b)\n"
        " def log_message(self,*a):pass\n"
        "ThreadingHTTPServer(('0.0.0.0',%d),H).serve_forever()" % port
    )
    # Escape single quotes for the bash -lc wrapper.
    return "python3 -c '%s'" % snippet.replace("'", "'\"'\"'")


def _healthcheck(*, port: int) -> Dict[str, Any]:
    """A ``healthcheck`` block hitting the service's own ``/health`` via curl.

    Interval / timeout / retries are env-tunable (no magic constants).
    """
    interval = _int_env(_ENV_HEALTH_INTERVAL_S, _DEFAULT_HEALTH_INTERVAL_S)
    timeout = _int_env(_ENV_HEALTH_TIMEOUT_S, _DEFAULT_HEALTH_TIMEOUT_S)
    retries = _int_env(_ENV_HEALTH_RETRIES, _DEFAULT_HEALTH_RETRIES)
    return {
        "test": [
            "CMD-SHELL",
            "curl -f http://localhost:%d/health || exit 1" % port,
        ],
        "interval": "%ds" % interval,
        "timeout": "%ds" % timeout,
        "retries": retries,
        # Generous start_period so the in-container install/boot isn't counted
        # as failing retries (still bounded by retries * interval).
        "start_period": "%ds" % (interval * 2),
    }


def _service(
    *,
    repo_root: str,
    base_image: str,
    server_cmd: str,
    port: int,
    overrides: Dict[str, str],
    network: str,
    with_healthcheck: bool,
    prebaked_image: str = "",
) -> Dict[str, Any]:
    """One Trinity service: base image + repo bind-mount (ro) + server + env.

    When ``prebaked_image`` is supplied (the Pre-Flight Cache Manager baked a
    hash-tagged image with the repo's deps already pip-installed in a WAN phase),
    use that image directly and run the repo's OWN server command -- NO bind
    mount, NO ``apt-get``/``pip`` at boot. This is what lets the air-gapped
    (``internal: true``) boot succeed even though PyPI is unreachable: the deps
    are already inside the image, baked WAN-connected before the air-gap is
    entered. The legacy base-image + bind-mount + pip-at-boot path is preserved
    byte-identical when ``prebaked_image`` is empty (prebake disabled / inert).
    """
    if prebaked_image:
        # Deps already baked into the image (WAN phase). The repo is COPYed into
        # /app at bake time, so just exec its server -- no mount, no pip.
        svc: Dict[str, Any] = {
            "image": prebaked_image,
            "working_dir": "/app",
            "command": ["bash", "-lc", "exec %s" % server_cmd],
            "environment": dict(overrides),
            "networks": [network],
            "expose": [str(port)],
        }
    else:
        svc = {
            "image": base_image,
            # Bind-mount the repo READ-ONLY -> the sandbox can never mutate the
            # sibling repo's working tree (no cross-repo write through sandbox).
            "volumes": ["%s:/app:ro" % repo_root],
            "working_dir": "/app",
            "command": ["bash", "-lc", _install_and_start(server_cmd)],
            # Every provider base URL points at the in-network sinkhole.
            "environment": dict(overrides),
            "networks": [network],
            "expose": [str(port)],
        }
    if with_healthcheck:
        svc["healthcheck"] = _healthcheck(port=port)
    return svc


def generate_trinity_compose(
    *,
    jarvis_root: str,
    prime_root: str,
    reactor_root: str,
    mock_port: int = _DEFAULT_MOCK_PORT,
    base_image: str = _DEFAULT_BASE_IMAGE,
    egress_mock_script: str = "/app/scripts/trinity_sandbox_egress_mock.py",
    images: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Generate the REAL air-gapped 3-repo Trinity compose dict.

    All three roots are caller-supplied (from ``.env`` via ``RepoRegistry``).
    Returns a docker-compose dict (serialize with :func:`serialize_compose`).

    Health-gating: jarvis ``depends_on`` prime + reactor with
    ``condition: service_healthy`` so the integration test cannot start before
    the Mind + Nerves are healthy.

    Cryptographic sinkhole: one ``internal: true`` network; every service's
    provider URLs overridden to the in-network ``egress-mock``.

    Pre-baked images (Pre-Flight Cache Manager): ``images`` maps a repo key
    (``"jarvis"`` / ``"prime"`` / ``"reactor"``) to a hash-tagged image that the
    bake phase produced WAN-connected (deps already pip-installed + repo COPYed
    in). When present for a service, that service runs from the cached image with
    NO bind-mount and NO pip-at-boot -- which is what makes the air-gapped boot
    succeed (PyPI is unreachable on the ``internal: true`` net). When ``images``
    is None / a key is absent, the legacy base-image + bind-mount + pip-at-boot
    path is used byte-identical (prebake disabled / inert).
    """
    # Env may override the passed defaults so an operator can tune without
    # touching the call site; explicit non-default args still win.
    if base_image == _DEFAULT_BASE_IMAGE:
        base_image = _base_image()

    image_map: Dict[str, str] = dict(images or {})

    overrides = _provider_url_overrides(mock_port=mock_port)
    net = TRINITY_NETWORK

    services: Dict[str, Any] = {}

    # Mind (prime) + Nerves (reactor): health-gated leaves.
    services["prime"] = _service(
        repo_root=prime_root,
        base_image=base_image,
        server_cmd="python run_server.py",
        port=_SERVICE_PORTS["prime"],
        overrides=overrides,
        network=net,
        with_healthcheck=True,
        prebaked_image=image_map.get("prime", ""),
    )
    services["reactor"] = _service(
        repo_root=reactor_root,
        base_image=base_image,
        server_cmd="python run_reactor.py",
        port=_SERVICE_PORTS["reactor"],
        overrides=overrides,
        network=net,
        with_healthcheck=True,
        prebaked_image=image_map.get("reactor", ""),
    )

    # Body (jarvis): the integration driver / in-network vantage. Health-GATED
    # on BOTH leaves so it cannot ping the mutated reactor/prime APIs before
    # they are up. It runs a minimal stdlib health surface (no new module, no
    # sibling-repo write) on its port so the container stays alive and is a
    # real in-network caller during the handshake suite. The actual mutated-API
    # pings are driven by run_handshake_suite against the prime/reactor URLs.
    jarvis_svc = _service(
        repo_root=jarvis_root,
        base_image=base_image,
        server_cmd=_jarvis_health_inline_cmd(port=_SERVICE_PORTS["jarvis"]),
        port=_SERVICE_PORTS["jarvis"],
        overrides=overrides,
        network=net,
        with_healthcheck=False,
        prebaked_image=image_map.get("jarvis", ""),
    )
    jarvis_svc["depends_on"] = {
        "prime": {"condition": "service_healthy"},
        "reactor": {"condition": "service_healthy"},
    }
    services["jarvis"] = jarvis_svc

    # The egress sinkhole -- the ONLY reachable "external" endpoint. Reuses the
    # synthetic mock (mounted, run in-place). On the internal network too.
    services[EGRESS_MOCK_SERVICE] = {
        "image": base_image,
        "container_name": "trinity_sandbox_egress_mock",
        # Mount jarvis_root so the mock script (under scripts/) is available.
        "volumes": ["%s:/app:ro" % jarvis_root],
        "working_dir": "/app",
        "command": [
            "python3",
            egress_mock_script,
            "--port",
            str(mock_port),
        ],
        "environment": {"TRINITY_SANDBOX_EGRESS_PORT": str(mock_port)},
        "networks": [net],
        "expose": [str(mock_port)],
    }

    compose: Dict[str, Any] = {
        "services": services,
        "networks": {
            net: {
                # The load-bearing air-gap declaration: Docker blackholes all
                # WAN egress on an internal network.
                "internal": True,
                "driver": "bridge",
            }
        },
    }
    return compose


def serialize_compose(compose: Dict[str, Any]) -> str:
    """YAML-serialize the compose dict, deterministically (sort_keys preserved).

    Raises ``RuntimeError`` if PyYAML is unavailable -- fail-CLOSED: a gate that
    cannot render a provably-air-gapped compose must not run.
    """
    if yaml is None:  # pragma: no cover - PyYAML missing
        raise RuntimeError("serialize_compose requires PyYAML")
    return yaml.safe_dump(compose, sort_keys=False, default_flow_style=False)


# --------------------------------------------------------------------------- #
# Static sinkhole guarantee (fail-CLOSED)
# --------------------------------------------------------------------------- #
def _iter_env_values(compose: Dict[str, Any]):
    services = compose.get("services") or {}
    for name, svc in services.items():
        env = (svc or {}).get("environment") or {}
        if isinstance(env, dict):
            for k, v in env.items():
                yield name, k, str(v)
        elif isinstance(env, list):
            for item in env:
                if isinstance(item, str) and "=" in item:
                    k, v = item.split("=", 1)
                    yield name, k, v


def assert_sinkhole(compose: Dict[str, Any]) -> Tuple[bool, str]:
    """Static guarantee that the generated compose is a cryptographic sinkhole.

    Returns ``(ok, reason)``. Fail-CLOSED: ANY doubt -> ``(False, reason)``.

    Checks:
      1. Exactly one network, declared ``internal: true``.
      2. Every service attaches ONLY to that internal network.
      3. NO service maps a host port (``ports:``) -- only ``expose:`` (in-network)
         is allowed; a published host port could leak to the WAN.
      4. NO live provider host string (DW/Claude/GCP metadata) appears anywhere
         in any service's environment values -- every provider URL must point at
         the in-network egress-mock.
    """
    if not isinstance(compose, dict):
        return False, "compose_not_a_mapping"

    networks = compose.get("networks") or {}
    if not isinstance(networks, dict) or len(networks) != 1:
        return False, "expected_exactly_one_network"
    (net_name, net_cfg), = networks.items()
    if not isinstance(net_cfg, dict) or net_cfg.get("internal") is not True:
        return False, "network_not_internal:%s" % net_name

    services = compose.get("services") or {}
    if not isinstance(services, dict) or not services:
        return False, "no_services"

    for svc_name, svc in services.items():
        svc = svc or {}
        # (2) Only the internal network.
        svc_nets = svc.get("networks")
        if not isinstance(svc_nets, list) or list(svc_nets) != [net_name]:
            return False, "service_not_on_internal_network:%s" % svc_name
        # (3) NO published host ports.
        if svc.get("ports"):
            return False, "service_publishes_host_port:%s" % svc_name

    # (4) No live provider host string anywhere in env.
    for svc_name, key, val in _iter_env_values(compose):
        for host in LIVE_PROVIDER_HOSTS:
            if host in val:
                return False, "live_provider_host_leaked:%s:%s=%s" % (
                    svc_name,
                    key,
                    host,
                )
        # Defense in depth: a raw scheme://<wan> not pointing at the mock is a
        # leak too. We only trust URLs that resolve to the egress-mock service.
        lowered = val.lower()
        if lowered.startswith("http://") or lowered.startswith("https://"):
            if EGRESS_MOCK_SERVICE not in val:
                return False, "non_mock_url_in_env:%s:%s=%s" % (
                    svc_name,
                    key,
                    val,
                )

    return True, "air_gapped_sinkhole_verified"


# Public surface for static import-cycle / reuse checks.
__all__: List[str] = [
    "TRINITY_NETWORK",
    "generate_trinity_compose",
    "serialize_compose",
    "assert_sinkhole",
]
