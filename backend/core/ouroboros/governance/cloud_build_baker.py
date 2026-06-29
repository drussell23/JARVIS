"""cloud_build_baker.py -- Serverless IaC Execution (the Automated REST Baker).

Submits a Packer ``.hcl`` as a remote **Google Cloud Build** job over REST, so the
32B GPU golden image is manufactured hands-off: GCP provisions the builder, runs
``packer build`` (which itself spins a GPU node, bakes the NVIDIA driver + CUDA +
Ollama + pre-pulls the 32B weights, snapshots the image, tears the node down),
then tears the builder down. Zero local Packer, zero gcloud, **zero GCS upload**
-- the spec is inlined as base64 into the build steps.

Auth is the SAME dynamic ADC REST bridge the provisioner uses
(``gcp_compute_rest.GCPComputeRest.access_token`` -> ``cloud-platform`` scope,
which also covers Cloud Build + Storage) and the same async ``_http_request``
helper -- no duplicate auth, no new SDK.

The pure helpers (:func:`build_packer_cloud_build`, the status predicates) are
unit-tested; :class:`CloudBuildBaker` is the async orchestration (submit -> poll
-> stream logs).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import secrets
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_CLOUD_BUILD_BASE = "https://cloudbuild.googleapis.com/v1"
_STORAGE_BASE = "https://storage.googleapis.com/storage/v1"
_IAM_BASE = "https://iam.googleapis.com/v1"
_CRM_BASE = "https://cloudresourcemanager.googleapis.com/v1"
_PACKER_IMAGE = "hashicorp/packer:latest"
_WRITER_IMAGE = "gcr.io/cloud-builders/gcloud"
_SPEC_PATH_IN_BUILD = "/workspace/main.pkr.hcl"

_TERMINAL_STATUSES = frozenset({
    "SUCCESS", "FAILURE", "TIMEOUT", "CANCELLED", "EXPIRED", "INTERNAL_ERROR",
})

# Least-privilege roles Packer needs to bake a GPU image + write build logs.
# Deliberately NOT roles/compute.admin (the blast-radius role we refused to grant
# the default SA): instanceAdmin can create/delete the build VM, storageAdmin can
# create the image, serviceAccountUser lets the build act-as the runtime SA,
# logWriter lets the custom-SA build emit logs to Cloud Logging.
_DEFAULT_BAKER_ROLES = (
    "roles/compute.instanceAdmin.v1",
    "roles/compute.storageAdmin",
    "roles/iam.serviceAccountUser",
    "roles/logging.logWriter",
)


def baker_sa_roles() -> List[str]:
    """Least-privilege roles bound to the ephemeral baker SA. Env-overridable
    (``JARVIS_BAKE_SA_ROLES``, comma-separated) -- no hardcoded blast radius."""
    raw = os.environ.get("JARVIS_BAKE_SA_ROLES", "").strip()
    if raw:
        return [r.strip() for r in raw.split(",") if r.strip()]
    return list(_DEFAULT_BAKER_ROLES)


def temp_sa_account_id() -> str:
    """A unique, valid GCP SA accountId for an ephemeral baker
    (``^[a-z][a-z0-9-]{4,28}[a-z0-9]$``). 6 hex of entropy -> collision-free."""
    return "jarvis-gpu-baker-{}".format(secrets.token_hex(3))


def create_sa_payload(account_id: str, display_name: str) -> Dict[str, Any]:
    """serviceAccounts.create request body."""
    return {"accountId": account_id, "serviceAccount": {"displayName": display_name}}


def iam_policy_add_binding(policy: Dict[str, Any], role: str, member: str) -> Dict[str, Any]:
    """Read-modify-write: add ``member`` to ``role``'s binding (creating the
    binding if absent, never duplicating the member). Returns the policy."""
    bindings = policy.setdefault("bindings", [])
    for b in bindings:
        if b.get("role") == role:
            members = b.setdefault("members", [])
            if member not in members:
                members.append(member)
            return policy
    bindings.append({"role": role, "members": [member]})
    return policy


def iam_policy_remove_member(policy: Dict[str, Any], member: str) -> Dict[str, Any]:
    """Strip ``member`` from EVERY binding; drop any binding left empty (no
    lingering privilege residue). Returns the policy."""
    kept = []
    for b in policy.get("bindings", []):
        members = [m for m in b.get("members", []) if m != member]
        if members:
            kept.append({**b, "members": members})
    policy["bindings"] = kept
    return policy


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested)
# ---------------------------------------------------------------------------
def build_status_is_terminal(status: str) -> bool:
    return str(status or "").upper() in _TERMINAL_STATUSES


def build_status_is_success(status: str) -> bool:
    return str(status or "").upper() == "SUCCESS"


def build_packer_cloud_build(
    *,
    spec_text: str,
    project: str,
    image_family: str,
    substitutions: Optional[Dict[str, str]] = None,
    extra_vars: Optional[Dict[str, str]] = None,
    timeout_s: int = 3600,
    machine_type: str = "E2_HIGHCPU_8",
    disk_gb: int = 100,
    packer_image: str = _PACKER_IMAGE,
    service_account: Optional[str] = None,
) -> Dict[str, Any]:
    """Construct the EXACT Cloud Build ``Build`` resource that bakes the image:
    (1) write the base64-inlined Packer spec into the build workspace,
    (2) ``packer init``, (3) ``packer build`` with the project/image-family/model
    vars. No ``source`` -> no GCS upload (the spec rides inline). Pure."""
    b64 = base64.b64encode((spec_text or "").encode("utf-8")).decode("ascii")
    var_flags = [
        "-var=project_id={}".format(project),
        "-var=image_family={}".format(image_family),
    ]
    for k, v in (extra_vars or {}).items():
        var_flags.append("-var={}={}".format(k, v))
    steps = [
        {
            "name": _WRITER_IMAGE,
            "entrypoint": "bash",
            # base64 is [A-Za-z0-9+/=] only -> safe inside single quotes.
            "args": ["-c", "printf %s '{}' | base64 -d > {}".format(b64, _SPEC_PATH_IN_BUILD)],
        },
        {"name": packer_image, "args": ["init", _SPEC_PATH_IN_BUILD]},
        {"name": packer_image, "args": ["build", *var_flags, _SPEC_PATH_IN_BUILD]},
    ]
    options: Dict[str, Any] = {"machineType": machine_type, "diskSizeGb": int(disk_gb)}
    cfg: Dict[str, Any] = {
        "steps": steps,
        "timeout": "{}s".format(int(timeout_s)),
        "options": options,
        "substitutions": dict(substitutions or {}),
    }
    if service_account:
        # A custom build SA REQUIRES an explicit logging mode (Cloud Build won't
        # use the default logs bucket under a user-supplied SA).
        cfg["serviceAccount"] = service_account
        options["logging"] = "CLOUD_LOGGING_ONLY"
    return cfg


# ---------------------------------------------------------------------------
# Async orchestration
# ---------------------------------------------------------------------------
class CloudBuildBaker:
    """Submit + poll a remote Packer Cloud Build. Reuses the provisioner's ADC
    auth + async HTTP. Fail-soft: every method returns a value / logs; the only
    hard failure surfaced is a non-success terminal build status."""

    def __init__(
        self,
        *,
        spec_path: str,
        project: Optional[str] = None,
        image_family: str = "jarvis-prime-coder-32b",
        model: Optional[str] = None,
        zone: Optional[str] = None,
        timeout_s: int = 3600,
        poll_interval_s: int = 15,
    ) -> None:
        self.spec_path = spec_path
        self._project = project
        self.image_family = image_family
        self.model = model
        self.zone = zone
        self.timeout_s = timeout_s
        self.poll_interval_s = poll_interval_s
        self._log_offset = 0
        # Ephemeral Zero-Trust IAM: the custom build SA (set during the lifecycle).
        self.service_account: Optional[str] = None
        self._iam_settle_s = int(os.environ.get("JARVIS_BAKE_IAM_SETTLE_S", "10"))

    # -- auth/identity reused from the provisioner's ADC bridge ---------------
    async def _auth(self):
        from backend.core.ouroboros.governance.gcp_compute_rest import (  # noqa: PLC0415
            get_compute_rest,
        )
        client = get_compute_rest()
        token = await client.access_token()
        project = self._project or await client.project() or os.environ.get("GCP_PROJECT")
        return token, project

    def _read_spec(self) -> str:
        return Path(self.spec_path).read_text(encoding="utf-8")

    def build_config(self, project: str) -> Dict[str, Any]:
        extra = {}
        if self.model:
            extra["model_label"] = self.model
        if self.zone:
            extra["zone"] = self.zone
        return build_packer_cloud_build(
            spec_text=self._read_spec(), project=project,
            image_family=self.image_family, extra_vars=extra, timeout_s=self.timeout_s,
            service_account=self.service_account,
        )

    async def submit(self) -> Optional[str]:
        """POST the build; return the build id (or None on failure)."""
        from backend.core.ouroboros.governance.gcp_compute_rest import (  # noqa: PLC0415
            _http_request,
        )
        token, project = await self._auth()
        if not token or not project:
            logger.error("[CloudBuildBaker] auth/project unresolved -> abort")
            return None
        cfg = self.build_config(project)
        url = "{}/projects/{}/builds".format(_CLOUD_BUILD_BASE, project)
        status, body = await _http_request(
            url, method="POST",
            headers={"Authorization": "Bearer {}".format(token),
                     "Content-Type": "application/json"},
            body=json.dumps(cfg).encode("utf-8"), timeout_s=60.0,
        )
        if status not in (200, 201):
            logger.error("[CloudBuildBaker] submit failed status=%s body=%s", status, body[:400])
            return None
        try:
            op = json.loads(body)
            build_id = op.get("metadata", {}).get("build", {}).get("id")
            logger.info("[CloudBuildBaker] build submitted id=%s", build_id)
            return build_id
        except Exception as exc:  # noqa: BLE001
            logger.error("[CloudBuildBaker] submit parse err=%r body=%s", exc, body[:400])
            return None

    async def _get_build(self, project: str, token: str, build_id: str) -> Dict[str, Any]:
        from backend.core.ouroboros.governance.gcp_compute_rest import (  # noqa: PLC0415
            _http_request,
        )
        url = "{}/projects/{}/builds/{}".format(_CLOUD_BUILD_BASE, project, build_id)
        status, body = await _http_request(
            url, method="GET",
            headers={"Authorization": "Bearer {}".format(token)}, timeout_s=30.0,
        )
        if status != 200:
            return {}
        try:
            return json.loads(body)
        except Exception:  # noqa: BLE001
            return {}

    async def _stream_new_logs(self, project: str, token: str, logs_bucket: str, build_id: str) -> None:
        """Fetch the GCS build-log object and print only the NEW tail. Fail-soft."""
        if not logs_bucket:
            return
        from backend.core.ouroboros.governance.gcp_compute_rest import (  # noqa: PLC0415
            _http_request,
        )
        bucket = logs_bucket.replace("gs://", "").split("/", 1)[0]
        obj = "log-{}.txt".format(build_id)
        url = "{}/b/{}/o/{}?alt=media".format(_STORAGE_BASE, bucket, obj.replace("/", "%2F"))
        try:
            status, body = await _http_request(
                url, method="GET",
                headers={"Authorization": "Bearer {}".format(token)}, timeout_s=30.0,
            )
            if status == 200 and len(body) > self._log_offset:
                new = body[self._log_offset:]
                self._log_offset = len(body)
                print(new, end="", flush=True)
        except Exception:  # noqa: BLE001
            pass  # logs are best-effort; status polling is authoritative

    async def poll(self, build_id: str, *, max_wait_s: Optional[int] = None) -> str:
        """Poll the build to a terminal status, streaming logs. Returns the final
        status string (or "UNKNOWN")."""
        token, project = await self._auth()
        if not token or not project:
            return "UNKNOWN"
        waited = 0
        cap = max_wait_s if max_wait_s is not None else (self.timeout_s + 600)
        while True:
            build = await self._get_build(project, token, build_id)
            status = str(build.get("status", ""))
            await self._stream_new_logs(project, token, build.get("logsBucket", ""), build_id)
            if build_status_is_terminal(status):
                logger.info("[CloudBuildBaker] build %s -> %s", build_id, status)
                return status
            if waited >= cap:
                logger.warning("[CloudBuildBaker] poll cap %ss reached (status=%s)", cap, status)
                return status or "UNKNOWN"
            await asyncio.sleep(self.poll_interval_s)
            waited += self.poll_interval_s

    async def bake(self) -> bool:
        """Full hands-off bake: submit -> poll -> stream logs. True iff SUCCESS."""
        build_id = await self.submit()
        if not build_id:
            return False
        status = await self.poll(build_id)
        return build_status_is_success(status)

    # -- Ephemeral Zero-Trust IAM lifecycle ----------------------------------
    def _wal_path(self) -> Path:
        p = Path(os.environ.get(
            "JARVIS_BAKE_WAL", str(Path(".jarvis") / "bake" / "bake_wal.log")
        ))
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def _write_flare(self, message: str) -> None:
        """Append an immutable, timestamped flare to the bake WAL (never raises)."""
        try:
            import datetime as _dt  # noqa: PLC0415
            stamp = _dt.datetime.now(_dt.timezone.utc).isoformat()
            with self._wal_path().open("a", encoding="utf-8") as fh:
                fh.write("[{}] [bake] {}\n".format(stamp, message))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[CloudBuildBaker] flare write fail-soft err=%r", exc)
        logger.info("[CloudBuildBaker][flare] %s", message)

    async def _create_temp_sa(self, project: str, token: str, account_id: str) -> Optional[str]:
        from backend.core.ouroboros.governance.gcp_compute_rest import _http_request  # noqa: PLC0415
        url = "{}/projects/{}/serviceAccounts".format(_IAM_BASE, project)
        status, body = await _http_request(
            url, method="POST",
            headers={"Authorization": "Bearer {}".format(token), "Content-Type": "application/json"},
            body=json.dumps(create_sa_payload(account_id, "JARVIS GPU baker (ephemeral)")).encode("utf-8"),
            timeout_s=60.0,
        )
        if status not in (200, 201):
            logger.error("[CloudBuildBaker] SA create failed status=%s body=%s", status, body[:300])
            return None
        try:
            return json.loads(body).get("email")
        except Exception:  # noqa: BLE001
            return None

    async def _get_project_policy(self, project: str, token: str) -> Optional[Dict[str, Any]]:
        from backend.core.ouroboros.governance.gcp_compute_rest import _http_request  # noqa: PLC0415
        url = "{}/projects/{}:getIamPolicy".format(_CRM_BASE, project)
        status, body = await _http_request(
            url, method="POST",
            headers={"Authorization": "Bearer {}".format(token), "Content-Type": "application/json"},
            body=b"{}", timeout_s=60.0,
        )
        if status != 200:
            return None
        try:
            return json.loads(body)
        except Exception:  # noqa: BLE001
            return None

    async def _set_project_policy(self, project: str, token: str, policy: Dict[str, Any]) -> bool:
        from backend.core.ouroboros.governance.gcp_compute_rest import _http_request  # noqa: PLC0415
        url = "{}/projects/{}:setIamPolicy".format(_CRM_BASE, project)
        status, _ = await _http_request(
            url, method="POST",
            headers={"Authorization": "Bearer {}".format(token), "Content-Type": "application/json"},
            body=json.dumps({"policy": policy}).encode("utf-8"), timeout_s=60.0,
        )
        return status == 200

    async def _bind_roles(self, project: str, token: str, member: str, roles: List[str]) -> bool:
        policy = await self._get_project_policy(project, token)
        if policy is None:
            return False
        for role in roles:
            iam_policy_add_binding(policy, role, member)
        return await self._set_project_policy(project, token, policy)

    async def _unbind_member(self, project: str, token: str, member: str) -> bool:
        policy = await self._get_project_policy(project, token)
        if policy is None:
            return False
        iam_policy_remove_member(policy, member)
        return await self._set_project_policy(project, token, policy)

    async def _delete_temp_sa(self, project: str, token: str, sa_email: str) -> bool:
        from backend.core.ouroboros.governance.gcp_compute_rest import _http_request  # noqa: PLC0415
        url = "{}/projects/{}/serviceAccounts/{}".format(_IAM_BASE, project, sa_email)
        status, _ = await _http_request(
            url, method="DELETE",
            headers={"Authorization": "Bearer {}".format(token)}, timeout_s=60.0,
        )
        return status == 200

    async def bake_with_ephemeral_iam(self) -> bool:
        """The Zero-Trust bake: create a DEDICATED temp SA -> bind least-privilege
        roles -> run the build AS that SA -> GUARANTEE teardown (delete the SA the
        moment the build concludes, success or fail). The default Cloud Build SA is
        never touched; zero lingering privilege. Drops WAL flares throughout."""
        token, project = await self._auth()
        if not token or not project:
            self._write_flare("BAKE ABORT: auth/project unresolved")
            return False
        sa_email: Optional[str] = None
        member: Optional[str] = None
        try:
            sa_email = await self._create_temp_sa(project, token, temp_sa_account_id())
            if not sa_email:
                self._write_flare("BAKE ABORT: temp SA create denied (need iam.serviceAccounts.create)")
                return False
            member = "serviceAccount:{}".format(sa_email)
            self._write_flare("EPHEMERAL SA CREATED sa={}".format(sa_email))
            if not await self._bind_roles(project, token, member, baker_sa_roles()):
                self._write_flare("BAKE ABORT: role bind denied (need resourcemanager.projects.setIamPolicy)")
                return False
            self._write_flare("LEAST-PRIV ROLES BOUND roles={}".format(",".join(baker_sa_roles())))
            await asyncio.sleep(self._iam_settle_s)  # IAM propagation settle
            self.service_account = "projects/{}/serviceAccounts/{}".format(project, sa_email)
            build_id = await self.submit()
            if not build_id:
                self._write_flare("BAKE FAILED: submit rejected")
                return False
            self._write_flare("BAKE SUBMITTED build={} sa={}".format(build_id, sa_email))
            status = await self.poll(build_id)
            ok = build_status_is_success(status)
            self._write_flare(
                "GOLDEN IMAGE READY image_family={} build={}".format(self.image_family, build_id)
                if ok else "BAKE FAILED status={} build={}".format(status, build_id)
            )
            return ok
        finally:
            if sa_email:
                try:
                    t2, p2 = await self._auth()  # token may have rotated over a long bake
                    proj, tok = (p2 or project), (t2 or token)
                    if member:
                        await self._unbind_member(proj, tok, member)
                    await self._delete_temp_sa(proj, tok, sa_email)
                    self._write_flare("EPHEMERAL SA TORN DOWN sa={} (zero lingering privilege)".format(sa_email))
                except Exception as exc:  # noqa: BLE001
                    self._write_flare("WARN teardown err={!r} sa={} -- MANUAL CHECK".format(exc, sa_email))


__all__ = [
    "CloudBuildBaker", "build_packer_cloud_build",
    "build_status_is_terminal", "build_status_is_success",
    "baker_sa_roles", "temp_sa_account_id", "create_sa_payload",
    "iam_policy_add_binding", "iam_policy_remove_member",
]
