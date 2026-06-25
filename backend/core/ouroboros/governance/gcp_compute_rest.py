"""gcp_compute_rest.py -- native, dependency-free async GCP Compute REST client.

The Sovereign awaken path with ZERO gcloud-CLI dependency.

This module strips the gcloud-CLI subprocess boundary out of the Sovereign
Failover Lifecycle and replaces it with a native async GCP Compute Engine REST
client authenticated entirely via the GCE **metadata server** (no gcloud, no SA
key files). It mirrors -- and reuses the exact contract of -- the two existing
metadata-token + Compute REST patterns already in the tree:

  * ``sovereign_self_termination.py`` -- the Python node self-DELETE (stdlib
    urllib metadata-token + Compute REST). Same auth + REST contract.
  * ``failover_deadman.py`` -- the node-side bash dead-man that curls the same
    ``compute.googleapis.com/compute/v1/projects/<P>/zones/<Z>/instances/<I>``
    DELETE contract. This client hits the SAME REST contract so the orchestrator
    and the dead-man speak the same API.

Design discipline
-----------------
* **Metadata-token auth only** -- the SA OAuth token, the SCOPES, the current
  ZONE, and the PROJECT are all fetched at runtime from the metadata server
  (``http://metadata.google.internal/computeMetadata/v1/...`` with the
  ``Metadata-Flavor: Google`` header). NOTHING is baked in.
* **Zero hardcoding** -- zone/project/internal-IP are NEVER literals; they are
  resolved dynamically from metadata + the running instance's API response.
  image-family / machine-type / timeouts are env-tunable.
* **Dynamic IAM self-verification** -- ``verify_compute_scopes()`` reads the
  instance scopes from metadata and confirms ``cloud-platform`` OR a compute
  scope is present BEFORE attempting any mutation. A missing scope yields a
  graceful ``IAM_PERMISSION_DENIED`` locus -- mathematically verified, not
  guessed -- so the caller aborts the failover cleanly rather than firing a
  doomed insert.
* **Async-clean** -- prefers ``aiohttp`` if importable; otherwise a stdlib
  ``urllib`` call dispatched on the default executor (``run_in_executor``) so
  the event loop is never blocked. All network calls are bounded by timeouts.
* **Lazy heavy imports** -- ``aiohttp`` is imported lazily inside the request
  helper; the module imports cleanly with only the stdlib.
* **Fail-CLOSED** -- metadata-unreachable / IAM-denied / HTTP error => a
  graceful ``(False, "<LOCUS>:<detail>")`` tuple (or ``None`` IP), NEVER a raise
  into the cognitive loop. The op stays sealed in the Cryo-DLQ; the lifecycle
  stays DORMANT.
* **Default-OFF byte-identical** -- this client only engages when the failover
  lifecycle is armed and reaches its awaken/delete path; importing it has no
  side effects.

Env knobs
---------
  JPRIME_IMAGE_FAMILY                 default "jarvis-prime-coder"
  JARVIS_FAILOVER_NODE_NAME           default "jarvis-prime-failover"
  JARVIS_FAILOVER_MACHINE_TYPE        default "e2-highmem-2"
  JARVIS_FAILOVER_METADATA_TIMEOUT_S  default 3.0
  JARVIS_FAILOVER_REST_TIMEOUT_S      default 30.0
  JARVIS_FAILOVER_RUNNING_TIMEOUT_S   default 180.0  (await_running_ip budget)
  JARVIS_FAILOVER_RUNNING_POLL_S      default 5.0    (await_running_ip cadence)
  GCP_PROJECT_ID / GOOGLE_CLOUD_PROJECT  optional project override (else metadata)
  GCP_ZONE                            optional zone override (else metadata)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Metadata server + Compute API constants (REST contract -- mirrors the
# self-termination + dead-man modules exactly).
# ---------------------------------------------------------------------------

_METADATA_BASE = "http://metadata.google.internal/computeMetadata/v1"
_METADATA_HEADERS = {"Metadata-Flavor": "Google"}
_COMPUTE_BASE = "https://compute.googleapis.com/compute/v1"

# IAM scopes that grant Compute mutation rights. cloud-platform is the umbrella;
# the dedicated compute scopes also suffice. Read-only is explicitly NOT here.
_COMPUTE_GRANT_SCOPES = (
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/compute",
)

# Env-var names.
_ENV_IMAGE_FAMILY = "JPRIME_IMAGE_FAMILY"
_ENV_NODE_NAME = "JARVIS_FAILOVER_NODE_NAME"
_ENV_MACHINE_TYPE = "JARVIS_FAILOVER_MACHINE_TYPE"
_ENV_META_TIMEOUT = "JARVIS_FAILOVER_METADATA_TIMEOUT_S"
_ENV_REST_TIMEOUT = "JARVIS_FAILOVER_REST_TIMEOUT_S"
_ENV_RUNNING_TIMEOUT = "JARVIS_FAILOVER_RUNNING_TIMEOUT_S"
_ENV_RUNNING_POLL = "JARVIS_FAILOVER_RUNNING_POLL_S"

_DEFAULT_IMAGE_FAMILY = "jarvis-prime-coder"
_DEFAULT_NODE_NAME = "jarvis-prime-failover"
_DEFAULT_MACHINE_TYPE = "e2-highmem-2"


# ---------------------------------------------------------------------------
# Env helpers (fail-soft, never raise)
# ---------------------------------------------------------------------------

def _env_str(name: str, default: str) -> str:
    return (os.environ.get(name, default) or default).strip()


def _env_float(name: str, default: float, lo: float = 0.1, hi: float = 86400.0) -> float:
    raw = (os.environ.get(name, "") or "").strip()
    try:
        v = float(raw) if raw else default
    except (TypeError, ValueError):
        v = default
    return max(lo, min(v, hi))


def _meta_timeout() -> float:
    return _env_float(_ENV_META_TIMEOUT, 3.0, lo=0.1, hi=60.0)


def _rest_timeout() -> float:
    return _env_float(_ENV_REST_TIMEOUT, 30.0, lo=1.0, hi=600.0)


def _running_timeout() -> float:
    return _env_float(_ENV_RUNNING_TIMEOUT, 180.0, lo=1.0, hi=3600.0)


def _running_poll() -> float:
    return _env_float(_ENV_RUNNING_POLL, 5.0, lo=0.1, hi=120.0)


def _image_family() -> str:
    return _env_str(_ENV_IMAGE_FAMILY, _DEFAULT_IMAGE_FAMILY)


def _node_name() -> str:
    return _env_str(_ENV_NODE_NAME, _DEFAULT_NODE_NAME)


def _machine_type() -> str:
    return _env_str(_ENV_MACHINE_TYPE, _DEFAULT_MACHINE_TYPE)


# ---------------------------------------------------------------------------
# Low-level async HTTP (aiohttp if present, else stdlib urllib on the executor)
# ---------------------------------------------------------------------------

async def _http_request(
    url: str,
    *,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    body: Optional[bytes] = None,
    timeout_s: float = 10.0,
) -> Tuple[int, str]:
    """Async HTTP request. Returns (status_code, body_text). NEVER raises -- a
    transport error is surfaced as (0, "<repr>") so callers stay fail-soft.

    Prefers aiohttp (truly async); else dispatches a bounded stdlib urllib call
    on the default executor so the event loop is never blocked.
    """
    hdrs = dict(headers or {})
    # Try aiohttp lazily -- truly non-blocking when available.
    try:
        import aiohttp  # noqa: PLC0415

        timeout = aiohttp.ClientTimeout(total=timeout_s)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(
                method, url, headers=hdrs, data=body
            ) as resp:
                text = await resp.text()
                return int(resp.status), text
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001 -- aiohttp transport error
        return 0, "[aiohttp error: {!r}]".format(exc)

    # Stdlib fallback on the executor (urllib is blocking).
    def _blocking() -> Tuple[int, str]:
        try:
            req = urllib.request.Request(url, method=method, headers=hdrs, data=body)
            with urllib.request.urlopen(req, timeout=timeout_s) as r:  # noqa: S310
                status = getattr(r, "status", 200) or 200
                return int(status), r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as he:  # a real HTTP status (4xx/5xx)
            try:
                detail = he.read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                detail = ""
            return int(he.code), detail
        except Exception as exc:  # noqa: BLE001 -- transport / DNS / timeout
            return 0, "[urllib error: {!r}]".format(exc)

    try:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _blocking)
    except Exception as exc:  # noqa: BLE001
        return 0, "[executor error: {!r}]".format(exc)


# ---------------------------------------------------------------------------
# Metadata-server client (token / scopes / zone / project -- all dynamic)
# ---------------------------------------------------------------------------

class GCPComputeRest:
    """Native async GCP Compute REST orchestrator authenticated via the GCE
    metadata server. All identity (token/scopes/zone/project) is resolved at
    runtime -- zero hardcoding. Fail-CLOSED on every boundary.
    """

    def __init__(
        self,
        *,
        project: Optional[str] = None,
        zone: Optional[str] = None,
    ) -> None:
        # Explicit overrides win; otherwise resolved lazily from metadata.
        self._project_override = project or (
            _env_str("GCP_PROJECT_ID", "") or _env_str("GOOGLE_CLOUD_PROJECT", "")
        ) or None
        self._zone_override = zone or (_env_str("GCP_ZONE", "") or None)

    # -- metadata fetch --------------------------------------------------

    async def _metadata(self, path: str) -> Optional[str]:
        """GET a metadata-server value. None off-GCE / on error. NEVER raises."""
        status, text = await _http_request(
            "{}/{}".format(_METADATA_BASE, path),
            method="GET",
            headers=_METADATA_HEADERS,
            timeout_s=_meta_timeout(),
        )
        if status != 200 or not text:
            return None
        return text.strip()

    async def access_token(self) -> Optional[str]:
        """The instance default service-account OAuth token from metadata. None
        on error. NEVER raises."""
        raw = await self._metadata("instance/service-accounts/default/token")
        if not raw:
            return None
        try:
            return json.loads(raw).get("access_token")
        except Exception:  # noqa: BLE001
            return None

    async def scopes(self) -> List[str]:
        """The instance default service-account scopes (newline-delimited in the
        metadata server). [] on error. NEVER raises."""
        raw = await self._metadata("instance/service-accounts/default/scopes")
        if not raw:
            return []
        return [s.strip() for s in raw.splitlines() if s.strip()]

    async def zone(self) -> Optional[str]:
        """The current zone, stripped to the last path component. Honors an
        explicit override. Metadata returns 'projects/<num>/zones/<zone>'."""
        if self._zone_override:
            return self._zone_override.rsplit("/", 1)[-1]
        raw = await self._metadata("instance/zone")
        if not raw:
            return None
        return raw.rsplit("/", 1)[-1]

    async def project(self) -> Optional[str]:
        """The current project id. Honors an explicit override else metadata."""
        if self._project_override:
            return self._project_override
        return await self._metadata("project/project-id")

    # -- dynamic IAM self-verification -----------------------------------

    async def verify_compute_scopes(self) -> Tuple[bool, str]:
        """Mathematically verify (not guess) that the instance SA can mutate
        Compute. OK iff the scopes list contains cloud-platform OR a compute
        scope. Missing -> (False, "IAM_PERMISSION_DENIED:missing_compute_scope:
        <scopes>"). Metadata-unreachable -> (False, "IAM_PERMISSION_DENIED:
        metadata_unreachable"). NEVER raises."""
        scopes = await self.scopes()
        if not scopes:
            # Could not read scopes at all -> fail-CLOSED (do NOT assume grant).
            return (
                False,
                "IAM_PERMISSION_DENIED:metadata_unreachable",
            )
        granted = any(
            any(s == grant or s.endswith(grant.rsplit("/", 1)[-1]) for grant in _COMPUTE_GRANT_SCOPES)
            for s in scopes
        )
        if granted:
            return (True, "compute_scope_present")
        return (
            False,
            "IAM_PERMISSION_DENIED:missing_compute_scope:{}".format(",".join(scopes)),
        )

    # -- instance insert (bootstrap) -------------------------------------

    def _build_insert_payload(
        self,
        *,
        name: str,
        zone: str,
        project: str,
        machine_type: str,
        image_family: str,
        startup_script: str,
        spot: bool,
    ) -> Dict[str, Any]:
        """Construct the EXACT instances.insert JSON payload. Dynamic zone +
        project from metadata; sourceImage = the golden-image family; Spot
        scheduling (provisioningModel=SPOT + instanceTerminationAction=DELETE)
        when spot=True, on-demand otherwise; the project default network."""
        payload: Dict[str, Any] = {
            "name": name,
            "machineType": "zones/{}/machineTypes/{}".format(zone, machine_type),
            "disks": [
                {
                    "boot": True,
                    "autoDelete": True,
                    "initializeParams": {
                        # Golden image by family -- never a hardcoded image name.
                        "sourceImage": "projects/{}/global/images/family/{}".format(
                            project, image_family
                        ),
                    },
                }
            ],
            "networkInterfaces": [
                {
                    # Project default network/subnet -- no hardcoded subnet/IP.
                    "network": "global/networks/default",
                    "accessConfigs": [
                        {"type": "ONE_TO_ONE_NAT", "name": "External NAT"}
                    ],
                }
            ],
            "serviceAccounts": [
                {
                    "email": "default",
                    "scopes": list(_COMPUTE_GRANT_SCOPES[:1]),  # cloud-platform
                }
            ],
            "metadata": {
                "items": [
                    {"key": "startup-script", "value": startup_script},
                ]
            },
        }
        if spot:
            payload["scheduling"] = {
                "provisioningModel": "SPOT",
                "instanceTerminationAction": "DELETE",
                "automaticRestart": False,
                "preemptible": True,
            }
        return payload

    async def create_instance(
        self,
        *,
        startup_script: str,
        name: Optional[str] = None,
        machine_type: Optional[str] = None,
        image_family: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """Async REST bootstrapper: launch the golden image via instances.insert.

        Spot-first with an on-demand fallback if the Spot insert fails. Returns
        (ok, detail). Dynamic zone/project from metadata. Fail-CLOSED: a missing
        token / unresolved zone-or-project / HTTP error -> (False, "<LOCUS>:..").
        NEVER raises.
        """
        token = await self.access_token()
        if not token:
            return (False, "AUTH_TOKEN_UNAVAILABLE:metadata_unreachable")
        zone = await self.zone()
        project = await self.project()
        if not zone or not project:
            return (
                False,
                "IDENTITY_UNRESOLVED:zone={!r}:project={!r}".format(zone, project),
            )

        node = name or _node_name()
        machine = machine_type or _machine_type()
        family = image_family or _image_family()
        url = "{}/projects/{}/zones/{}/instances".format(_COMPUTE_BASE, project, zone)
        headers = {
            "Authorization": "Bearer {}".format(token),
            "Content-Type": "application/json",
        }

        # Spot-first.
        for spot in (True, False):
            payload = self._build_insert_payload(
                name=node,
                zone=zone,
                project=project,
                machine_type=machine,
                image_family=family,
                startup_script=startup_script,
                spot=spot,
            )
            body = json.dumps(payload).encode("utf-8")
            status, text = await _http_request(
                url,
                method="POST",
                headers=headers,
                body=body,
                timeout_s=_rest_timeout(),
            )
            if 200 <= status < 300:
                mode = "SPOT" if spot else "on-demand"
                logger.info(
                    "[GCPComputeRest] instances.insert ok node=%s zone=%s mode=%s "
                    "(status=%s)", node, zone, mode, status,
                )
                return (True, "created:{}:{}".format(mode, status))
            logger.warning(
                "[GCPComputeRest] instances.insert %s failed node=%s status=%s "
                "detail=%s", "SPOT" if spot else "on-demand", node, status,
                (text or "")[-300:],
            )
            # Only retry on-demand after a Spot failure; an on-demand failure is
            # terminal for this attempt.
        return (False, "INSERT_FAILED:spot_and_on_demand_rejected")

    # -- poll-to-RUNNING + internal-IP extraction ------------------------

    async def await_running_ip(
        self,
        name: Optional[str] = None,
        *,
        timeout_s: Optional[float] = None,
        poll_s: Optional[float] = None,
    ) -> Optional[str]:
        """Poll GET instances/<name> until status==RUNNING, then extract
        networkInterfaces[0].networkIP (the internal IP -- no hardcoding).

        Bounded by timeout_s (env JARVIS_FAILOVER_RUNNING_TIMEOUT_S). Returns the
        IP string, or None if the node never reaches RUNNING within the budget
        (fail-soft -- the caller treats None as 'publish nothing'). NEVER raises.
        """
        token = await self.access_token()
        if not token:
            return None
        zone = await self.zone()
        project = await self.project()
        if not zone or not project:
            return None

        node = name or _node_name()
        url = "{}/projects/{}/zones/{}/instances/{}".format(
            _COMPUTE_BASE, project, zone, node
        )
        headers = {"Authorization": "Bearer {}".format(token)}

        budget = timeout_s if timeout_s is not None else _running_timeout()
        cadence = poll_s if poll_s is not None else _running_poll()

        try:
            loop = asyncio.get_event_loop()
            deadline = loop.time() + budget
        except Exception:  # noqa: BLE001
            deadline = None

        while True:
            status_code, text = await _http_request(
                url, method="GET", headers=headers, timeout_s=_rest_timeout()
            )
            if 200 <= status_code < 300 and text:
                try:
                    doc = json.loads(text)
                except Exception:  # noqa: BLE001
                    doc = {}
                if str(doc.get("status", "")).upper() == "RUNNING":
                    ip = self._extract_internal_ip(doc)
                    if ip:
                        logger.info(
                            "[GCPComputeRest] node=%s RUNNING internal_ip=%s",
                            node, ip,
                        )
                        return ip
            # Not yet RUNNING (or transient). Respect the bounded budget.
            if deadline is not None:
                try:
                    if loop.time() >= deadline:
                        logger.warning(
                            "[GCPComputeRest] await_running_ip timed out node=%s "
                            "after %.0fs (fail-soft None)", node, budget,
                        )
                        return None
                except Exception:  # noqa: BLE001
                    return None
            try:
                await asyncio.sleep(cadence)
            except asyncio.CancelledError:
                return None

    @staticmethod
    def _extract_internal_ip(doc: Dict[str, Any]) -> Optional[str]:
        """Pull networkInterfaces[0].networkIP from an instances.get response."""
        try:
            nics = doc.get("networkInterfaces") or []
            if nics:
                ip = (nics[0] or {}).get("networkIP")
                if ip:
                    return str(ip).strip()
        except Exception:  # noqa: BLE001
            pass
        return None

    # -- delete (delete-to-snapshot keeps the golden image untouched) ----

    async def delete_instance(self, name: Optional[str] = None) -> Tuple[bool, str]:
        """DELETE instances/<name> via the SAME REST contract as the bash
        dead-man. Deleting the instance does NOT touch the golden image. Returns
        (ok, detail). Fail-CLOSED -> (False, "<LOCUS>:..") on any error. NEVER
        raises."""
        token = await self.access_token()
        if not token:
            return (False, "AUTH_TOKEN_UNAVAILABLE:metadata_unreachable")
        zone = await self.zone()
        project = await self.project()
        if not zone or not project:
            return (
                False,
                "IDENTITY_UNRESOLVED:zone={!r}:project={!r}".format(zone, project),
            )
        node = name or _node_name()
        url = "{}/projects/{}/zones/{}/instances/{}".format(
            _COMPUTE_BASE, project, zone, node
        )
        headers = {"Authorization": "Bearer {}".format(token)}
        status, text = await _http_request(
            url, method="DELETE", headers=headers, timeout_s=_rest_timeout()
        )
        # 200/202 accepted (async delete in progress); 404 == already gone
        # (idempotent -- treat as success). 409 likewise terminal-OK.
        if 200 <= status < 300 or status in (404, 409):
            logger.info(
                "[GCPComputeRest] instances.delete node=%s status=%s "
                "(delete-to-snapshot; golden image untouched)", node, status,
            )
            return (True, "deleted:{}".format(status))
        logger.warning(
            "[GCPComputeRest] instances.delete failed node=%s status=%s detail=%s",
            node, status, (text or "")[-300:],
        )
        return (False, "DELETE_FAILED:{}".format(status))


# ---------------------------------------------------------------------------
# Module-level convenience constructor (so the lifecycle can build per-call).
# ---------------------------------------------------------------------------

def get_compute_rest() -> GCPComputeRest:
    """Construct a fresh REST client (identity resolved lazily from metadata)."""
    return GCPComputeRest()


__all__ = [
    "GCPComputeRest",
    "get_compute_rest",
]
