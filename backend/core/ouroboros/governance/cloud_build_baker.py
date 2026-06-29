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
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_CLOUD_BUILD_BASE = "https://cloudbuild.googleapis.com/v1"
_STORAGE_BASE = "https://storage.googleapis.com/storage/v1"
_PACKER_IMAGE = "hashicorp/packer:latest"
_WRITER_IMAGE = "gcr.io/cloud-builders/gcloud"
_SPEC_PATH_IN_BUILD = "/workspace/main.pkr.hcl"

_TERMINAL_STATUSES = frozenset({
    "SUCCESS", "FAILURE", "TIMEOUT", "CANCELLED", "EXPIRED", "INTERNAL_ERROR",
})


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
    return {
        "steps": steps,
        "timeout": "{}s".format(int(timeout_s)),
        "options": {"machineType": machine_type, "diskSizeGb": int(disk_gb)},
        "substitutions": dict(substitutions or {}),
    }


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


__all__ = [
    "CloudBuildBaker", "build_packer_cloud_build",
    "build_status_is_terminal", "build_status_is_success",
]
