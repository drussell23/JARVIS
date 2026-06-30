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
import re
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

# ---------------------------------------------------------------------------
# Dynamic IAM Credential Bridge (Hybrid Execution Mesh, 2026-06-28)
# ---------------------------------------------------------------------------
#
# When the orchestrator runs OFF-GCE (a local Mac bridged to real GCP), the
# metadata server is unreachable. If GOOGLE_APPLICATION_CREDENTIALS points at a
# Service Account JSON, mint the Compute OAuth token via the native google-auth
# SDK -- ZERO gcloud CLI, ZERO metadata. Identity (token + project) flows from
# the SA JSON; zone from GCP_ZONE. The SA's IAM role is enforced server-side at
# instances.insert (a 403 surfaces there -- fail-CLOSED preserved).

def _sa_credentials_path() -> str:
    """The Service Account JSON path from GOOGLE_APPLICATION_CREDENTIALS, or ""
    when unset (legacy metadata-only mode). NEVER raises."""
    return (os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "") or "").strip()


def mint_sa_access_token(sa_path: str) -> Tuple[Optional[str], Optional[str]]:
    """Mint a Compute OAuth access token from a Service Account JSON via the
    native google-auth SDK. Returns ``(access_token, project_id)``.

    Fail-soft: a missing file / missing SDK / refresh error returns
    ``(None, None)`` so the caller degrades to a graceful IAM-denied abort (the
    cognitive loop is NEVER crashed). Injectable -- tests monkeypatch this so no
    real google-auth / network is touched."""
    try:
        from google.oauth2 import service_account  # noqa: PLC0415
        from google.auth.transport.requests import Request  # noqa: PLC0415

        creds = service_account.Credentials.from_service_account_file(
            sa_path, scopes=[_COMPUTE_GRANT_SCOPES[0]]  # cloud-platform
        )
        creds.refresh(Request())
        token = getattr(creds, "token", None)
        project = getattr(creds, "project_id", None)
        return (token or None, project or None)
    except Exception as exc:  # noqa: BLE001 -- a mint failure is a graceful denial
        logger.warning("[GCPComputeRest] SA token mint fail-soft err=%r", exc)
        return (None, None)


def _adc_available() -> bool:
    """True when gcloud Application Default Credentials are usable (the operator
    has run ``gcloud auth application-default login`` -- an authorized_user
    refresh-token, NOT a SA JSON). Detected via the well-known ADC file or an
    explicit ``JARVIS_FAILOVER_USE_ADC`` opt-in. NEVER raises."""
    val = (os.environ.get("JARVIS_FAILOVER_USE_ADC", "") or "").strip().lower()
    if val in ("1", "true", "yes", "on"):
        return True
    if val in ("0", "false", "no", "off"):
        return False
    try:
        cfg = os.environ.get("CLOUDSDK_CONFIG", "") or os.path.expanduser(
            "~/.config/gcloud"
        )
        return os.path.isfile(os.path.join(cfg, "application_default_credentials.json"))
    except Exception:  # noqa: BLE001
        return False


_IPV4_RE = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")


def _fetch_public_ip() -> str:
    """Fetch the orchestrator's public egress IP from an external echo service
    (stdlib urllib; env-tunable provider list). Raises on total failure -- the
    async wrapper catches. NO hardcoded IP."""
    providers = (
        os.environ.get("JARVIS_PUBLIC_IP_ECHO_URLS", "")
        or "https://checkip.amazonaws.com,https://api.ipify.org"
    )
    last_exc: Optional[BaseException] = None
    for url in [u.strip() for u in providers.split(",") if u.strip()]:
        try:
            with urllib.request.urlopen(url, timeout=5.0) as resp:  # noqa: S310
                return resp.read().decode("utf-8", errors="replace").strip()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
    if last_exc is not None:
        raise last_exc
    return ""


async def resolve_local_public_ip(
    fetch_fn: Optional[Any] = None,
) -> Optional[str]:
    """Programmatically resolve the orchestrator's PUBLIC egress IP at runtime
    (no hardcoded IP). Off-loaded to a thread (blocking HTTP). Validates a real
    dotted-quad IPv4. Fail-soft -> None. Injectable for tests."""
    fn = fetch_fn or _fetch_public_ip
    try:
        loop = asyncio.get_event_loop()
        raw = await loop.run_in_executor(None, fn)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[GCPComputeRest] public IP fetch fail-soft err=%r", exc)
        return None
    ip = (str(raw) or "").strip()
    m = _IPV4_RE.match(ip)
    if not m or any(int(o) > 255 for o in m.groups()):
        return None
    return ip


def mint_adc_access_token() -> Tuple[Optional[str], Optional[str]]:
    """Mint a Compute OAuth token from gcloud Application Default Credentials via
    ``google.auth.default`` (authorized_user refresh flow). Returns
    ``(access_token, project)``. Fail-soft -> (None, None). Injectable -- tests
    monkeypatch this so no real google-auth / network is touched."""
    try:
        import google.auth  # noqa: PLC0415
        from google.auth.transport.requests import Request  # noqa: PLC0415

        creds, project = google.auth.default(scopes=[_COMPUTE_GRANT_SCOPES[0]])
        creds.refresh(Request())
        token = getattr(creds, "token", None)
        # Env project override wins over the ADC-resolved project.
        env_proj = _env_str("GCP_PROJECT_ID", "") or _env_str("GOOGLE_CLOUD_PROJECT", "")
        return (token or None, (env_proj or project) or None)
    except Exception as exc:  # noqa: BLE001 -- a mint failure is a graceful denial
        logger.warning("[GCPComputeRest] ADC token mint fail-soft err=%r", exc)
        return (None, None)


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


def _insert_op_poll_cap_s() -> float:
    """ABSOLUTE safety ceiling (seconds) for polling a zonal insert operation to a
    TERMINAL state. The control-plane operation reaches DONE within seconds-to-tens
    normally (a GPU capacity failure surfaces AS the op completing with error), so a
    generous ceiling makes a breach a genuine anomaly. On breach the verdict is
    'unknown' (fail-closed phantom protection) -- NEVER an optimistic 'ok'."""
    return _env_float("JARVIS_INSERT_OP_POLL_CAP_S", 90.0, lo=0.0, hi=600.0)


def _insert_op_poll_interval_s() -> float:
    """Delay between insert-operation status polls. Env-tunable (no hardcoded
    cadence); floored > 0 so the poll loop always makes progress."""
    return _env_float("JARVIS_INSERT_OP_POLL_INTERVAL_S", 3.0, lo=0.001, hi=60.0)


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


def _ondemand_on_stockout_enabled() -> bool:
    """When true, a SPOT stockout in a zone falls through to an on-demand insert
    in the SAME zone before giving up on it (L4 Spot is scarce; on-demand has
    capacity in a quota'd region). Default OFF -> Spot-only across zones (prod
    failover stays cheap)."""
    return os.environ.get("JARVIS_FAILOVER_ONDEMAND_ON_STOCKOUT", "false").strip().lower() in ("1", "true", "yes")


# ---------------------------------------------------------------------------
# Low-level async HTTP (aiohttp if present, else stdlib urllib on the executor)
# ---------------------------------------------------------------------------

_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


def is_retryable_status(status: int) -> bool:
    """True iff an HTTP status is a transient, backoff-retryable failure (429 rate
    limit + 5xx). A 4xx (other than 429) or a transport error (0) is NOT retried."""
    return int(status or 0) in _RETRYABLE_STATUSES


def _backoff_delay(attempt: int, *, base: float = 0.5, cap: float = 30.0) -> float:
    """Exponential backoff with FULL JITTER: uniform(0, min(cap, base*2^attempt)).
    Jitter de-synchronizes concurrent retriers (the cause of the 429 storm)."""
    import random  # noqa: PLC0415
    ceil = min(cap, base * (2 ** max(0, attempt)))
    return random.uniform(0.0, ceil)


def _http_max_retries() -> int:
    try:
        return max(0, min(8, int(os.environ.get("JARVIS_HTTP_MAX_RETRIES", "4"))))
    except (ValueError, TypeError):
        return 4


async def _http_request(
    url: str,
    *,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    body: Optional[bytes] = None,
    timeout_s: float = 10.0,
) -> Tuple[int, str]:
    """Resilient async HTTP: wraps the single-shot request with exponential
    backoff + full jitter on retryable statuses (429 + 5xx). NEVER raises -- a
    transport error -> (0, "<repr>"); an exhausted retry budget surfaces the last
    retryable response (never a crash). Bounded by ``JARVIS_HTTP_MAX_RETRIES``."""
    attempts = _http_max_retries() + 1
    result = (0, "")
    for attempt in range(attempts):
        result = await _http_request_once(
            url, method=method, headers=headers, body=body, timeout_s=timeout_s,
        )
        if not is_retryable_status(result[0]):
            return result
        if attempt < attempts - 1:
            await asyncio.sleep(_backoff_delay(attempt))
    return result  # retry budget exhausted -> surface the last (retryable) response


async def _http_request_once(
    url: str,
    *,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    body: Optional[bytes] = None,
    timeout_s: float = 10.0,
) -> Tuple[int, str]:
    """Single-shot async HTTP request. Returns (status_code, body_text). NEVER
    raises -- a transport error is surfaced as (0, "<repr>") so callers stay
    fail-soft.

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
        # Hybrid Execution Mesh -- ADAPTIVE auth resolution (off-GCE). Priority:
        # an explicit SA JSON (GOOGLE_APPLICATION_CREDENTIALS) wins; else gcloud
        # ADC (authorized_user) when present; else the legacy metadata path.
        self._sa_path = _sa_credentials_path()
        if self._sa_path:
            self._auth_mode = "sa"
        elif _adc_available():
            self._auth_mode = "adc"
        else:
            self._auth_mode = "metadata"
        # Off-GCE iff a local credential source drives auth (SA or ADC).
        self._off_gce = self._auth_mode in ("sa", "adc")
        # The minted token + project are cached on first use (one mint per
        # awaken -- tokens last ~1h, far longer than a soak).
        self._cred_token: Optional[str] = None
        self._cred_project: Optional[str] = None
        self._cred_minted = False
        # Serializes the mint so concurrent callers (the parallel node+firewall
        # teardown gather) never race: the mint runs EXACTLY once and every
        # caller gets the real token. Created lazily to avoid loop-binding at
        # construction (the client is built off the running loop in tests).
        self._cred_lock: Optional["asyncio.Lock"] = None

    # -- adaptive credential bridge (off-GCE auth) -----------------------

    async def _ensure_token(self) -> Tuple[Optional[str], Optional[str]]:
        """Lazily mint + cache the (token, project) for the resolved off-GCE auth
        mode (SA JSON or ADC). asyncio.Lock-serialized so concurrent callers mint
        EXACTLY once (the parallel-teardown race fix). Off-loaded to a thread so
        the blocking google-auth refresh never stalls the loop. Fail-soft."""
        if self._cred_minted:
            return (self._cred_token, self._cred_project)
        if self._cred_lock is None:
            self._cred_lock = asyncio.Lock()
        async with self._cred_lock:
            # Double-check inside the lock: a racer that won the lock first may
            # have already minted while we waited.
            if self._cred_minted:
                return (self._cred_token, self._cred_project)
            try:
                loop = asyncio.get_event_loop()
                if self._auth_mode == "sa":
                    token, project = await loop.run_in_executor(
                        None, mint_sa_access_token, self._sa_path
                    )
                else:  # adc
                    token, project = await loop.run_in_executor(
                        None, mint_adc_access_token
                    )
                self._cred_token, self._cred_project = token, project
            except Exception as exc:  # noqa: BLE001
                logger.warning("[GCPComputeRest] ensure-token fail-soft err=%r", exc)
                self._cred_token, self._cred_project = None, None
            # Set the flag AFTER the mint completes (not before) -- this is the
            # race fix: a concurrent caller never sees minted=True with a None token.
            self._cred_minted = True
        return (self._cred_token, self._cred_project)

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
        """The Compute OAuth token. SA-JSON bridge (off-GCE) when
        GOOGLE_APPLICATION_CREDENTIALS is set; else the instance default SA token
        from metadata. None on error. NEVER raises."""
        if self._off_gce:
            token, _ = await self._ensure_token()
            return token
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
        """The current project id. Explicit override wins; then the SA JSON's
        project_id (off-GCE); else metadata."""
        if self._project_override:
            return self._project_override
        if self._off_gce:
            _, project = await self._ensure_token()
            if project:
                return project
        return await self._metadata("project/project-id")

    # -- dynamic IAM self-verification -----------------------------------

    async def verify_compute_scopes(self) -> Tuple[bool, str]:
        """Mathematically verify (not guess) that the instance SA can mutate
        Compute. OK iff the scopes list contains cloud-platform OR a compute
        scope. Missing -> (False, "IAM_PERMISSION_DENIED:missing_compute_scope:
        <scopes>"). Metadata-unreachable -> (False, "IAM_PERMISSION_DENIED:
        metadata_unreachable"). NEVER raises.

        SA-JSON bridge (off-GCE): we request cloud-platform when minting, so a
        successful mint satisfies the structural scope check; the SA's actual
        IAM role is enforced server-side at instances.insert (a 403 surfaces
        there as a real create failure). A failed mint -> graceful IAM-denied."""
        if self._off_gce:
            token, _ = await self._ensure_token()
            if token:
                return (True, "{}_credentials:compute_scope_requested".format(self._auth_mode))
            return (False, "IAM_PERMISSION_DENIED:{}_token_mint_failed".format(self._auth_mode))
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
        accelerator_type: str = "",
        accelerator_count: int = 0,
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
        # Dynamic IaC accelerator injection (quality tier). A GPU cannot live-
        # migrate, so onHostMaintenance MUST be TERMINATE; the acceleratorType is
        # a DYNAMIC zonal URL (never a hardcoded full path). count<=0 -> CPU
        # payload (no accidental GPU spend).
        if accelerator_type and accelerator_count > 0:
            payload["guestAccelerators"] = [
                {
                    "acceleratorType": "zones/{}/acceleratorTypes/{}".format(
                        zone, accelerator_type
                    ),
                    "acceleratorCount": int(accelerator_count),
                }
            ]
            sched = payload.setdefault("scheduling", {})
            sched["onHostMaintenance"] = "TERMINATE"
            sched.setdefault("automaticRestart", False)
        return payload

    async def create_instance(
        self,
        *,
        startup_script: str,
        name: Optional[str] = None,
        machine_type: Optional[str] = None,
        image_family: Optional[str] = None,
        accelerator_type: str = "",
        accelerator_count: int = 0,
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
        headers = {
            "Authorization": "Bearer {}".format(token),
            "Content-Type": "application/json",
        }

        # MULTI-ZONAL FALLBACK: a GPU STOCKOUT in one zone is transient -- retry
        # the SAME request in the next zone (cross-region) autonomously. A
        # non-stockout rejection stops the chain (no blind retry storm).
        from backend.core.ouroboros.governance.zone_fallback import (  # noqa: PLC0415
            zone_fallback_chain,
        )
        chain = zone_fallback_chain(zone)
        last = ""
        for z in chain:
            verdict, detail = await self._insert_in_zone(
                zone=z, project=project, token=token, headers=headers, node=node,
                machine=machine, family=family, startup_script=startup_script,
                accelerator_type=accelerator_type, accelerator_count=accelerator_count,
            )
            last = detail
            if verdict == "created":
                return (True, "created:{}".format(detail))
            if verdict == "stockout":
                logger.warning("[GCPComputeRest] STOCKOUT zone=%s -> failover to next zone", z)
                continue
            return (False, "INSERT_FAILED:{}".format(detail))  # non-stockout -> stop
        return (False, "INSERT_FAILED:all_zones_stockout:{}".format(last))

    async def _insert_in_zone(
        self, *, zone, project, token, headers, node, machine, family,
        startup_script, accelerator_type, accelerator_count,
    ) -> Tuple[str, str]:
        """Insert in ONE zone (Spot-first, on-demand fallback). Returns a verdict:
        'created' | 'stockout' (retry next zone) | 'failed' (stop). NEVER raises."""
        from backend.core.ouroboros.governance.zone_fallback import (  # noqa: PLC0415
            is_stockout_error,
        )
        url = "{}/projects/{}/zones/{}/instances".format(_COMPUTE_BASE, project, zone)
        for spot in (True, False):
            payload = self._build_insert_payload(
                name=node, zone=zone, project=project, machine_type=machine,
                image_family=family, startup_script=startup_script, spot=spot,
                accelerator_type=accelerator_type, accelerator_count=accelerator_count,
            )
            status, text = await _http_request(
                url, method="POST", headers=headers,
                body=json.dumps(payload).encode("utf-8"), timeout_s=_rest_timeout(),
            )
            mode = "SPOT" if spot else "on-demand"
            if 200 <= status < 300:
                # The insert returns a PENDING operation; a GPU STOCKOUT (or other
                # async failure) surfaces only when the operation RUNS. We MUST poll
                # it to a terminal state -- an accepted insert is NOT a created node.
                op = None
                try:
                    op = json.loads(text or "{}").get("name")
                except Exception:  # noqa: BLE001
                    op = None
                # No trackable operation name -> we cannot verify the outcome.
                # Fail-closed: treat exactly like 'unknown' (reap + roll), never 'ok'.
                verdict = (
                    await self._await_insert_operation(project, zone, op, token)
                    if op else "unknown"
                )
                if verdict == "stockout":
                    if spot and _ondemand_on_stockout_enabled():
                        logger.warning("[GCPComputeRest] SPOT stockout zone=%s -> trying ON-DEMAND same zone (quota'd region)", zone)
                        continue
                    return ("stockout", "zone={}:op_stockout".format(zone))
                if verdict == "unknown":
                    # Fail-closed phantom protection: a node may have spawned late
                    # even though we never saw DONE. Violently reap it BEFORE moving
                    # on (idempotent; 404 == already gone), then roll the chain.
                    logger.warning(
                        "[GCPComputeRest] insert UNVERIFIED zone=%s mode=%s -> reaping "
                        "any phantom node + rolling to next zone", zone, mode,
                    )
                    try:
                        await self.delete_instance(node, zone=zone)
                    except Exception as exc:  # noqa: BLE001 -- reap is best-effort
                        logger.debug("[GCPComputeRest] phantom reap fail-soft err=%r", exc)
                    if spot and _ondemand_on_stockout_enabled():
                        continue  # escalate to on-demand same zone first
                    return ("stockout", "zone={}:unknown_reaped".format(zone))
                if verdict == "error":
                    logger.warning("[GCPComputeRest] insert op error zone=%s mode=%s", zone, mode)
                    continue  # try on-demand
                logger.info("[GCPComputeRest] instances.insert ok node=%s zone=%s mode=%s",
                            node, zone, mode)
                return ("created", "zone={}:mode={}".format(zone, mode))
            # Synchronous rejection: a stockout here -> retry next zone.
            if is_stockout_error(text):
                if spot and _ondemand_on_stockout_enabled():
                    logger.warning("[GCPComputeRest] SPOT sync-stockout zone=%s -> trying ON-DEMAND same zone", zone)
                    continue
                return ("stockout", "zone={}:sync_stockout".format(zone))
            logger.warning("[GCPComputeRest] insert %s failed zone=%s status=%s detail=%s",
                           mode, zone, status, (text or "")[-200:])
        return ("failed", "zone={}:spot_and_on_demand_rejected".format(zone))

    async def _await_insert_operation(self, project, zone, op_name, token) -> str:
        """Poll a zonal insert operation to a TERMINAL state. FAIL-CLOSED.

        Returns:
          'ok'       -- operations.get returned status=DONE with NO error object
                        (the ONLY success path -- explicit terminal confirmation).
          'stockout' -- DONE with a transient capacity error (retry the next zone).
          'error'    -- DONE with a non-stockout error (stop the chain).
          'unknown'  -- the absolute safety ceiling was breached WITHOUT a confirmed
                        DONE, or operations.get was unreachable throughout. The
                        operation's outcome is genuinely unverified -- the caller
                        must assume a phantom node may exist (reap) and roll over.
                        NEVER an optimistic 'ok'.

        NEVER raises."""
        from backend.core.ouroboros.governance.zone_fallback import (  # noqa: PLC0415
            is_stockout_error,
        )
        url = "{}/projects/{}/zones/{}/operations/{}".format(_COMPUTE_BASE, project, zone, op_name)
        cap = _insert_op_poll_cap_s()
        interval = _insert_op_poll_interval_s()
        waited = 0.0
        while waited < cap:
            status, text = await _http_request(
                url, method="GET", headers={"Authorization": "Bearer {}".format(token)},
                timeout_s=30.0,
            )
            # Only a successfully-fetched, parseable, DONE response is terminal.
            # A non-2xx fetch / unparseable body is NOT terminal -> keep polling
            # (it may be transient); if it persists to the ceiling -> 'unknown'.
            if 200 <= status < 300:
                try:
                    op = json.loads(text or "{}")
                except Exception:  # noqa: BLE001
                    op = {}
                if op.get("status") == "DONE":
                    err = op.get("error")
                    if not err:
                        return "ok"
                    return "stockout" if is_stockout_error(json.dumps(err)) else "error"
            await asyncio.sleep(interval)
            waited += interval
        # Ceiling breached without a confirmed terminal DONE. We do NOT know whether
        # a node spawned -> fail-closed 'unknown' (the caller reaps + rolls over).
        logger.warning(
            "[GCPComputeRest] insert op UNVERIFIED after %.1fs (no terminal DONE) "
            "zone=%s op=%s -> 'unknown' (fail-closed; will reap + roll zone)",
            cap, zone, op_name,
        )
        return "unknown"

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
                    ip = self._select_reachable_ip(doc)
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

    @staticmethod
    def _extract_external_ip(doc: Dict[str, Any]) -> Optional[str]:
        """Pull networkInterfaces[0].accessConfigs[0].natIP (the EXTERNAL
        ephemeral IP) from an instances.get response. None when the node has no
        external access config yet. NEVER raises."""
        try:
            nics = doc.get("networkInterfaces") or []
            if nics:
                acs = (nics[0] or {}).get("accessConfigs") or []
                if acs:
                    nat = (acs[0] or {}).get("natIP")
                    if nat:
                        return str(nat).strip()
        except Exception:  # noqa: BLE001
            pass
        return None

    def _hybrid_mesh(self) -> bool:
        """True when the orchestrator is OFF-GCE and must reach J-Prime over its
        EXTERNAL IP -- SA-credential auth implies hybrid; an explicit
        JARVIS_FAILOVER_HYBRID_MESH flag forces it. NEVER raises."""
        if self._off_gce:
            return True
        val = (os.environ.get("JARVIS_FAILOVER_HYBRID_MESH", "") or "").strip().lower()
        return val in ("1", "true", "yes", "on")

    def _select_reachable_ip(self, doc: Dict[str, Any]) -> Optional[str]:
        """The IP the orchestrator should route GENERATE traffic to. Hybrid
        (off-GCE) -> the EXTERNAL natIP, falling back to internal if the external
        is not assigned yet. On-VPC -> the internal IP (byte-identical legacy)."""
        if self._hybrid_mesh():
            return self._extract_external_ip(doc) or self._extract_internal_ip(doc)
        return self._extract_internal_ip(doc)

    async def get_node_endpoints(
        self, name: Optional[str] = None,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Single instances.get -> ``(internal_ip, external_ip)`` for the
        Reachability Racer (which probes BOTH concurrently -- no env guessing).
        Either may be None while the node is still booting. NEVER raises."""
        try:
            token = await self.access_token()
            zone = await self.zone()
            project = await self.project()
            if not token or not zone or not project:
                return (None, None)
            node = name or _node_name()
            url = "{}/projects/{}/zones/{}/instances/{}".format(
                _COMPUTE_BASE, project, zone, node
            )
            status, text = await _http_request(
                url, method="GET",
                headers={"Authorization": "Bearer {}".format(token)},
                timeout_s=_rest_timeout(),
            )
            if not (200 <= status < 300 and text):
                return (None, None)
            doc = json.loads(text)
            return (self._extract_internal_ip(doc), self._extract_external_ip(doc))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[GCPComputeRest] get_node_endpoints fail-soft err=%r", exc)
            return (None, None)

    # -- delete (delete-to-snapshot keeps the golden image untouched) ----

    async def delete_instance(
        self, name: Optional[str] = None, *, zone: Optional[str] = None
    ) -> Tuple[bool, str]:
        """DELETE instances/<name> via the SAME REST contract as the bash
        dead-man. Deleting the instance does NOT touch the golden image. Returns
        (ok, detail). Fail-CLOSED -> (False, "<LOCUS>:..") on any error. NEVER
        raises.

        ``zone`` overrides the resolved default zone -- REQUIRED to reap a node
        that the multi-zonal awaken landed in a fallback zone (not ``GCP_ZONE``);
        without it a node created in us-central1-c is orphaned when the reap only
        looks in us-central1-a."""
        token = await self.access_token()
        if not token:
            return (False, "AUTH_TOKEN_UNAVAILABLE:metadata_unreachable")
        zone = zone or await self.zone()
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

    async def get_serial_port_output(
        self, name: Optional[str] = None, *, zone: Optional[str] = None, port: int = 1,
    ) -> Tuple[Optional[str], str]:
        """GET instances/<name>/serialPort?port=<port> via the SAME REST contract
        as the dead-man delete -- reads the node's boot serial console for the
        Autonomous Diagnostic Reaper (Task HW2). Returns (contents, detail):
        (text, "ok:200") on success, (None, "<LOCUS>:..") on any failure.
        Fail-CLOSED -- NEVER raises."""
        token = await self.access_token()
        if not token:
            return (None, "AUTH_TOKEN_UNAVAILABLE:metadata_unreachable")
        zone = zone or await self.zone()
        project = await self.project()
        if not zone or not project:
            return (
                None,
                "IDENTITY_UNRESOLVED:zone={!r}:project={!r}".format(zone, project),
            )
        node = name or _node_name()
        url = "{}/projects/{}/zones/{}/instances/{}/serialPort?port={}".format(
            _COMPUTE_BASE, project, zone, node, int(port),
        )
        headers = {"Authorization": "Bearer {}".format(token)}
        status, text = await _http_request(
            url, method="GET", headers=headers, timeout_s=_rest_timeout(),
        )
        if 200 <= status < 300:
            try:
                contents = json.loads(text).get("contents", "") if text else ""
            except Exception:  # noqa: BLE001 -- malformed body is still fail-soft
                contents = text or ""
            return (contents, "ok:{}".format(status))
        logger.warning(
            "[GCPComputeRest] serialPort read failed node=%s status=%s detail=%s",
            node, status, (text or "")[-200:],
        )
        return (None, "SERIAL_READ_FAILED:{}".format(status))

    # -- ephemeral firewall micro-perimeter (IaC, same REST bridge) -------

    async def create_firewall_rule(
        self, *, name: str, source_ip: str, port: int = 11434,
    ) -> Tuple[bool, str]:
        """Create a /32-scoped INGRESS firewall rule (``source_ip/32`` ->
        ``tcp:port``) via native Compute REST. REFUSES an empty source IP -- NO
        ``0.0.0.0/0`` fallback, ever (never open the port to the whole internet).
        Returns (ok, detail). Fail-CLOSED. NEVER raises."""
        if not source_ip:
            return (False, "no_source_ip:refuse_open_internet")
        token = await self.access_token()
        project = await self.project()
        if not token or not project:
            return (False, "AUTH_OR_PROJECT_UNRESOLVED:token={}:project={!r}".format(
                bool(token), project))
        url = "{}/projects/{}/global/firewalls".format(_COMPUTE_BASE, project)
        payload = {
            "name": name,
            "network": "global/networks/default",
            "direction": "INGRESS",
            "priority": 1000,
            "sourceRanges": ["{}/32".format(source_ip)],
            "allowed": [{"IPProtocol": "tcp", "ports": [str(port)]}],
            "description": (
                "JARVIS ephemeral failover micro-perimeter -- auto-managed, "
                "bound to the failover node lifecycle; deleted on teardown."
            ),
        }
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": "Bearer {}".format(token),
            "Content-Type": "application/json",
        }
        status, text = await _http_request(
            url, method="POST", headers=headers, body=body, timeout_s=_rest_timeout(),
        )
        if 200 <= status < 300:
            logger.info(
                "[GCPComputeRest] firewall.insert ok name=%s src=%s/32 tcp:%s "
                "(status=%s)", name, source_ip, port, status,
            )
            return (True, "created:{}".format(status))
        # 409 == already exists (a prior awaken's rule) -> treat as present-OK.
        if status == 409:
            return (True, "exists:409")
        logger.warning(
            "[GCPComputeRest] firewall.insert failed name=%s status=%s detail=%s",
            name, status, (text or "")[-200:],
        )
        return (False, "FW_CREATE_FAILED:{}:{}".format(status, (text or "")[:150]))

    async def delete_firewall_rule(self, name: str) -> Tuple[bool, str]:
        """DELETE the ephemeral firewall rule. 404 (already gone) is the desired
        end-state -> success (no orphan-hole anxiety). Fail-CLOSED. NEVER raises."""
        token = await self.access_token()
        project = await self.project()
        if not token or not project:
            return (False, "AUTH_OR_PROJECT_UNRESOLVED")
        url = "{}/projects/{}/global/firewalls/{}".format(_COMPUTE_BASE, project, name)
        headers = {"Authorization": "Bearer {}".format(token)}
        status, text = await _http_request(
            url, method="DELETE", headers=headers, timeout_s=_rest_timeout(),
        )
        if 200 <= status < 300 or status in (404, 409):
            logger.info(
                "[GCPComputeRest] firewall.delete name=%s status=%s "
                "(micro-perimeter closed)", name, status,
            )
            return (True, "deleted:{}".format(status))
        logger.warning(
            "[GCPComputeRest] firewall.delete failed name=%s status=%s detail=%s",
            name, status, (text or "")[-200:],
        )
        return (False, "FW_DELETE_FAILED:{}".format(status))


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
