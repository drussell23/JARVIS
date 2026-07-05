"""Stage-1 Brain-VM dynamic discovery + mTLS WS reachability.

Root-cause discovery with ZERO hardcoded topology:

  1. LIST -- ``gcp_compute_rest.list_instances_by_label`` filters GCE instances
     on the ``jarvis-role=brain`` label (added in Task 2). Each RUNNING match
     yields candidate endpoints (external natIP + internal IP) at the WS
     port/path resolved from the Stage-0 ``TransportConfig``.
  2. RACE -- the candidate WS URLs are handed to the Asynchronous Reachability
     Racer (``FailoverLifecycleController._race_node_ready``, reused verbatim):
     probe ALL candidates concurrently, bind whichever answers a healthy mTLS
     handshake FIRST (``asyncio.wait(FIRST_COMPLETED)``). No IS_LOCAL flag, no
     hardcoded host swap -- external natIP wins off-VPC, internal wins on-VPC.
  3. RETURN -- the single winning WS URL. NOTHING is cached beyond that one
     returned value.

**Statelessness contract (load-bearing).** ``discover_brain_endpoint`` is
idempotent and holds NO cross-call state: the CALLER re-invokes discovery on
EVERY reconnect, so a Brain that moved zones / IPs between connections is
re-resolved from scratch. Never memoize an IP here.

mTLS material is resolved -- not reimplemented. The client material comes from
local env (``JARVIS_BRAIN_WS_TLS_*`` -> ``TransportConfig.from_env``); the
server material is written to the Brain's ``/etc/jarvis/brain.env`` from
instance metadata (Task 1). Both are fed to the Stage-0
``build_client_ssl_context`` / ``build_server_ssl_context`` UNCHANGED -- this
module only resolves the material PATHS/values, it does not touch TLS itself.

Firewall lifecycle helpers (``open_brain_firewall`` / ``close_brain_firewall``)
are thin wrappers over ``gcp_compute_rest.create_firewall_rule`` /
``delete_firewall_rule`` with the Mac's ``resolve_local_public_ip()`` as the
/32 source. They live here as reusable helpers; Task 4's ignition driver OWNS
the create/teardown SEQUENCING (open before connect, delete on every exit path)
and calls these.

Fail-soft throughout: discovery returns ``None`` (never raises) when no healthy
Brain is reachable; the firewall wrappers return ``(ok, detail)`` and REFUSE an
empty source IP (no open-to-the-whole-internet CIDR fallback, ever).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Env knobs (resolved at CALL time -- zero baked assumptions).
# ---------------------------------------------------------------------------

_ENV_FW_RULE_NAME = "JARVIS_BRAIN_FIREWALL_RULE_NAME"
_DEFAULT_FW_RULE_NAME = "jarvis-brain-mtls"

_ENV_PROBE_TIMEOUT_S = "JARVIS_BRAIN_WS_PROBE_TIMEOUT_S"
_DEFAULT_PROBE_TIMEOUT_S = 5.0

# Firewall / candidate port fallback used ONLY when TransportConfig.port is
# unset (0). Not an endpoint literal -- just an integer default.
_DEFAULT_WS_PORT = 8443


def _brain_fw_rule_name() -> str:
    val = (os.environ.get(_ENV_FW_RULE_NAME, "") or "").strip()
    return val or _DEFAULT_FW_RULE_NAME


def _probe_timeout_s() -> float:
    try:
        v = float((os.environ.get(_ENV_PROBE_TIMEOUT_S, "") or "").strip())
        return v if v > 0 else _DEFAULT_PROBE_TIMEOUT_S
    except (ValueError, AttributeError):
        return _DEFAULT_PROBE_TIMEOUT_S


# ---------------------------------------------------------------------------
# Transport config + mTLS material (resolve, do not reimplement).
# ---------------------------------------------------------------------------


def _brain_transport_config() -> Any:
    """Env-resolved Stage-0 ``TransportConfig`` for the Brain WS surface.

    Re-read on every call (no caching) so a reconnect picks up any rotated
    endpoint/cert env. NEVER raises -> a minimal fail-soft stand-in on import
    failure (discovery then yields no candidates)."""
    try:
        from backend.core.ouroboros.governance.transport.transport_config import (  # noqa: PLC0415
            TransportConfig,
        )

        return TransportConfig.from_env(role="brain-client")
    except Exception as exc:  # noqa: BLE001
        logger.debug("[BrainDiscovery] transport config resolve fail-soft err=%r", exc)
        return None


def build_brain_client_ssl_context(cfg: Optional[Any] = None) -> Optional[Any]:
    """Stage-0 mTLS CLIENT context from LOCAL env material (unchanged builder).

    The Mac side of the cross-host handshake. Returns ``None`` when TLS is
    disabled or the builder fails (fail-soft)."""
    try:
        from backend.core.ouroboros.governance.transport.transport_security import (  # noqa: PLC0415
            build_client_ssl_context,
        )

        resolved = cfg or _brain_transport_config()
        if resolved is None:
            return None
        return build_client_ssl_context(resolved)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[BrainDiscovery] client ssl context fail-soft err=%r", exc)
        return None


def build_brain_server_ssl_context(cfg: Optional[Any] = None) -> Optional[Any]:
    """Stage-0 mTLS SERVER context from env material (unchanged builder).

    On the Brain VM the env is populated from ``/etc/jarvis/brain.env`` (written
    from instance metadata at boot, Task 1). Provided here for symmetry; the
    discovery CALLER on the Mac uses the client context. Fail-soft -> None."""
    try:
        from backend.core.ouroboros.governance.transport.transport_security import (  # noqa: PLC0415
            build_server_ssl_context,
        )

        resolved = cfg or _brain_transport_config()
        if resolved is None:
            return None
        return build_server_ssl_context(resolved)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[BrainDiscovery] server ssl context fail-soft err=%r", exc)
        return None


# ---------------------------------------------------------------------------
# Generation filter (Stage-4 Task 3).
# ---------------------------------------------------------------------------

_ENV_CURRENT_GEN = "JARVIS_BRAIN_CURRENT_GEN"


def _current_gen_floor() -> int:
    """The keeper-exported generation floor, or 0 when the filter is inactive.

    BACKWARD COMPAT (load-bearing): unset / empty / malformed / non-positive
    values all resolve to 0 = ZERO behavior change -- discovery stays exactly
    the pre-Stage-4 role-label-only path."""
    raw = (os.environ.get(_ENV_CURRENT_GEN, "") or "").strip()
    if not raw:
        return 0
    try:
        val = int(raw)
    except ValueError:
        return 0
    return val if val > 0 else 0


def _filter_stale_generations(
    instances: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Exclude candidates from generations OLDER than the keeper's current one,
    BEFORE any probe is attempted.

    When ``JARVIS_BRAIN_CURRENT_GEN`` is set (the Body driver exports it from
    ``BrainKeeper.current_gen()``), a role-labelled instance whose
    ``jarvis-brain-gen`` label is < current is a superseded generation --
    never probed, never raced. An instance LACKING the gen label is excluded
    too: an unlabeled brain is pre-Stage-4 = obsolete by definition when a
    gen'd keeper runs (it predates the ownership substrate, so the keeper
    cannot reason about it). Equal/higher generations pass through.

    Fail-soft: with the env unset (or the lineage constants unimportable) the
    input list is returned unchanged -- zero behavior change."""
    floor = _current_gen_floor()
    if floor <= 0:
        return list(instances or [])
    try:
        from backend.core.ouroboros.governance.brain_lifecycle import (  # noqa: PLC0415
            LABEL_GEN,
        )
    except Exception as exc:  # noqa: BLE001 -- fail-soft: no filter, no break
        logger.debug("[BrainDiscovery] gen filter unavailable err=%r", exc)
        return list(instances or [])
    out: List[Dict[str, Any]] = []
    for inst in instances or []:
        raw_gen: Any = None
        try:
            raw_gen = (inst.get("labels") or {}).get(LABEL_GEN)
            gen: Optional[int] = int(str(raw_gen).strip())
        except (TypeError, ValueError, AttributeError):
            gen = None
        if gen is None or gen < floor:
            logger.info(
                "[BrainDiscovery] excluding stale-generation candidate "
                "name=%s gen=%s current_gen=%d (never probed)",
                (inst or {}).get("name"), raw_gen, floor,
            )
            continue
        out.append(inst)
    return out


# ---------------------------------------------------------------------------
# Candidate endpoint construction.
# ---------------------------------------------------------------------------


def _brain_ws_port(cfg: Optional[Any] = None) -> int:
    """The WS port -- from ``TransportConfig.port``, falling back to the module
    default only when unset (0). Never an endpoint literal."""
    resolved = cfg or _brain_transport_config()
    port = int(getattr(resolved, "port", 0) or 0)
    return port if port > 0 else _DEFAULT_WS_PORT


def _ws_scheme(cfg: Any) -> str:
    """``wss`` when mTLS is on (the Stage-1 default), else ``ws``. Built from
    parts so no ``wss?://`` literal ever appears in this module."""
    return "wss" if bool(getattr(cfg, "tls_enabled", True)) else "ws"


def _candidate_urls(instances: List[Dict[str, Any]], cfg: Any) -> List[str]:
    """Build the Reachability-Racer candidate WS URLs from RUNNING brain
    instances: external natIP FIRST (off-VPC most common), then internal IP.

    IP extraction reuses ``GCPComputeRest._extract_external_ip`` /
    ``_extract_internal_ip`` (single source of truth for the natIP/networkIP
    shape). NEVER raises -> a malformed instance contributes no candidates."""
    from backend.core.ouroboros.governance.gcp_compute_rest import (  # noqa: PLC0415
        GCPComputeRest,
    )

    scheme = _ws_scheme(cfg)
    port = _brain_ws_port(cfg)
    path = str(getattr(cfg, "path", "") or "")
    out: List[str] = []
    for inst in instances or []:
        try:
            if str(inst.get("status") or "").upper() != "RUNNING":
                continue
            external = GCPComputeRest._extract_external_ip(inst)
            internal = GCPComputeRest._extract_internal_ip(inst)
            for ip in (external, internal):
                if ip:
                    out.append("{}://{}:{}{}".format(scheme, ip, port, path))
        except Exception:  # noqa: BLE001
            continue
    return out


def _split_host_port(url: str) -> Optional[tuple]:
    """Parse ``<scheme>://<host>:<port><path>`` -> ``(host, port)``. Returns
    ``None`` on any malformed input. NEVER raises."""
    try:
        after = url.split("://", 1)[1] if "://" in url else url
        authority = after.split("/", 1)[0]
        if ":" not in authority:
            return None
        host, port_s = authority.rsplit(":", 1)
        return (host, int(port_s)) if host and port_s else None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# WS health probe + racer (reuse the Reachability Racer verbatim).
# ---------------------------------------------------------------------------


async def _default_ws_health_probe(url: str) -> bool:
    """Probe the WS HEALTH surface: complete a TLS (mTLS) handshake to the
    candidate host:port. A successful handshake proves the Brain's WS server is
    up AND that our client cert is accepted (mTLS-required); a certless / wrong
    peer is rejected by the server before the WS upgrade. Fail-soft -> False."""
    parsed = _split_host_port(url)
    if not parsed:
        return False
    host, port = parsed
    try:
        ctx = build_brain_client_ssl_context()
        # The server cert's SAN is the DNS identity (jarvis-brain), but discovery
        # dials the raw instance IP -- verify against the configured identity or
        # the handshake can never succeed on the CA path.
        kwargs: Dict[str, Any] = {}
        sni = os.environ.get("JARVIS_BRAIN_WS_TLS_SERVER_HOSTNAME", "").strip()
        if sni:
            kwargs["server_hostname"] = sni
        conn = asyncio.open_connection(host=host, port=port, ssl=ctx, **kwargs)
        reader, writer = await asyncio.wait_for(conn, timeout=_probe_timeout_s())
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("[BrainDiscovery] ws health probe fail-soft url=%s err=%r", url, exc)
        return False


async def _default_race(
    candidates: List[str], probe_fn: Callable[[str], Awaitable[bool]],
) -> Optional[str]:
    """Bind the first healthy candidate via the shared Asynchronous Reachability
    Racer. We REUSE ``FailoverLifecycleController._race_node_ready`` rather than
    replicate its FIRST_COMPLETED loop: the controller constructor is pure state
    init (no threads / no network / all fail-soft defaults), so we build one with
    our ``probe_fn`` injected as ``node_ready_fn`` and call the racer directly.
    Fail-soft -> None."""
    try:
        from backend.core.ouroboros.governance.failover_lifecycle import (  # noqa: PLC0415
            FailoverLifecycleController,
        )

        controller = FailoverLifecycleController(node_ready_fn=probe_fn)
        return await controller._race_node_ready(list(candidates))
    except Exception as exc:  # noqa: BLE001
        logger.debug("[BrainDiscovery] race fail-soft err=%r", exc)
        return None


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------


async def _default_list_brain_instances() -> List[Dict[str, Any]]:
    """Default LIST seam: aggregatedList GCE instances labelled
    ``jarvis-role=brain`` via the existing REST bridge. Fail-soft -> []."""
    try:
        from backend.core.ouroboros.governance.gcp_compute_rest import (  # noqa: PLC0415
            _BRAIN_ROLE_LABEL_KEY,
            _BRAIN_ROLE_LABEL_VALUE,
            get_compute_rest,
        )

        return await get_compute_rest().list_instances_by_label(
            label_key=_BRAIN_ROLE_LABEL_KEY, label_value=_BRAIN_ROLE_LABEL_VALUE,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[BrainDiscovery] default list fail-soft err=%r", exc)
        return []


async def discover_brain_endpoint(
    *,
    project: str = "",
    list_instances_fn: Optional[Callable[[], Awaitable[List[Dict[str, Any]]]]] = None,
    probe_fn: Optional[Callable[[str], Awaitable[bool]]] = None,
    race_fn: Optional[
        Callable[[List[str], Callable[[str], Awaitable[bool]]], Awaitable[Optional[str]]]
    ] = None,
    transport_config: Optional[Any] = None,
) -> Optional[str]:
    """Resolve a healthy Brain WS URL dynamically, or ``None``.

    Stateless / idempotent -- LISTS the brain-labelled instances fresh and RACES
    their candidate endpoints on every call. The caller re-invokes this on every
    reconnect; NOTHING is cached beyond the single returned URL.

    ``project`` is advisory: when empty the REST bridge resolves the project from
    env (``GCP_PROJECT_ID`` / ``GOOGLE_CLOUD_PROJECT``) or instance metadata --
    passing it does not mutate process state.

    Seams (``list_instances_fn`` / ``probe_fn`` / ``race_fn`` /
    ``transport_config``) are injectable for tests; the defaults are the live
    REST list + the mTLS handshake probe + the shared Reachability Racer.

    Fail-soft: NEVER raises into the caller -> ``None`` on any error."""
    try:
        cfg = transport_config or _brain_transport_config()
        if cfg is None:
            return None
        list_fn = list_instances_fn or _default_list_brain_instances
        instances = await list_fn()
        # Stage-4 gen filter: superseded generations (and pre-Stage-4
        # unlabeled brains) never become candidates when the keeper has
        # exported JARVIS_BRAIN_CURRENT_GEN; env unset = zero change.
        instances = _filter_stale_generations(instances or [])
        candidates = _candidate_urls(instances or [], cfg)
        if not candidates:
            logger.info(
                "[BrainDiscovery] no RUNNING brain candidate endpoints "
                "(project=%s) -- returning None (fail-soft)",
                project or "<env>",
            )
            return None
        probe = probe_fn or _default_ws_health_probe
        race = race_fn or _default_race
        winner = await race(candidates, probe)
        if winner:
            logger.info(
                "[BrainDiscovery] discovered brain WS endpoint from %d candidate(s)",
                len(candidates),
            )
        return winner or None
    except Exception as exc:  # noqa: BLE001
        logger.debug("[BrainDiscovery] discover fail-soft err=%r", exc)
        return None


# ---------------------------------------------------------------------------
# Ephemeral /32 firewall micro-perimeter (thin wrappers; Task 4 owns sequencing).
# ---------------------------------------------------------------------------


async def open_brain_firewall(
    source_ip: Optional[str] = None,
    *,
    port: int = 0,
    rule_name: Optional[str] = None,
    resolve_ip_fn: Optional[Callable[[], Awaitable[Optional[str]]]] = None,
    create_fn: Optional[Callable[..., Awaitable[Any]]] = None,
) -> Any:
    """Open a /32-scoped INGRESS rule from the Mac's PUBLIC IP to the Brain's WS
    port. The source IP is resolved via ``resolve_local_public_ip`` (no hardcoded
    IP); an unresolved IP REFUSES to open the rule (no whole-internet CIDR).
    Rule name + port env-resolved. Returns ``(ok, detail)``. NEVER raises."""
    try:
        ip = source_ip
        if not ip:
            resolver = resolve_ip_fn or _default_resolve_public_ip
            ip = await resolver()
        if not ip:
            return (False, "no_source_ip:refuse_open_internet")
        name = rule_name or _brain_fw_rule_name()
        p = int(port or 0) or _brain_ws_port()
        create = create_fn or _default_create_firewall_rule
        return await create(name=name, source_ip=ip, port=p)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[BrainDiscovery] open firewall fail-soft err=%r", exc)
        return (False, "open_firewall_error:{!r}".format(exc))


async def close_brain_firewall(
    *,
    rule_name: Optional[str] = None,
    delete_fn: Optional[Callable[[str], Awaitable[Any]]] = None,
) -> Any:
    """Delete the ephemeral Brain firewall rule (env-resolved name). 404 (already
    gone) is the desired end-state. Returns ``(ok, detail)``. NEVER raises."""
    try:
        name = rule_name or _brain_fw_rule_name()
        delete = delete_fn or _default_delete_firewall_rule
        return await delete(name)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[BrainDiscovery] close firewall fail-soft err=%r", exc)
        return (False, "close_firewall_error:{!r}".format(exc))


async def _default_resolve_public_ip() -> Optional[str]:
    from backend.core.ouroboros.governance.gcp_compute_rest import (  # noqa: PLC0415
        resolve_local_public_ip,
    )

    return await resolve_local_public_ip()


async def _default_create_firewall_rule(*, name: str, source_ip: str, port: int) -> Any:
    from backend.core.ouroboros.governance.gcp_compute_rest import (  # noqa: PLC0415
        get_compute_rest,
    )

    return await get_compute_rest().create_firewall_rule(
        name=name, source_ip=source_ip, port=port,
    )


async def _default_delete_firewall_rule(name: str) -> Any:
    from backend.core.ouroboros.governance.gcp_compute_rest import (  # noqa: PLC0415
        get_compute_rest,
    )

    return await get_compute_rest().delete_firewall_rule(name)
