"""Sovereign Ephemeral Self-Termination Matrix (2026-06-21).

The organism manages its own death. The instant the autonomous crucible opens a
[SOVEREIGN GRADUATION] PR, the ephemeral soak node:

  1. marks a TERMINAL_SUCCESS sentinel (with the PR URL),
  2. synchronously flushes the .jarvis ledger + state to the GCS Vault one last time
     (immortalizes the victory across the impending teardown), and
  3. severs its own compute — deletes its own GCE VM via the metadata-server service
     account + the Compute REST API (stdlib urllib; the lean image has NO gcloud CLI).

No external watcher script, no zombie nodes. Absolute, programmatic, in-organism
cost control.

Design discipline:
  * **Gated default-OFF** (``JARVIS_SOVEREIGN_SELF_TERMINATE_ENABLED``). Self-deleting
    the host VM is destructive — only the EPHEMERAL crucible overlay opts in. A dev /
    local / long-lived deployment NEVER self-terminates by default.
  * **Fires ONLY on genuine graduation-PR success** (a real ``pr_url``), never on
    errors / advisories / dry-runs.
  * **Flush-before-sever**: the GCS push completes before the VM delete is issued, so
    the historical record survives.
  * **Idempotent**: the sentinel guard means a second success can't double-fire.
  * **Fail-soft**: NEVER raises into the graduation path. If the VM can't self-delete
    (no IAM, not on GCE), it logs + relies on the Spot max-lifetime backstop; the
    graduation PR is already safely open regardless.
  * **No CLI / no new heavy deps**: metadata server + Compute REST API via urllib;
    GCS flush reuses the existing ``state_persistence_daemon``.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

_ENV_ENABLED = "JARVIS_SOVEREIGN_SELF_TERMINATE_ENABLED"
_ENV_SENTINEL = "JARVIS_SOVEREIGN_TERMINAL_SENTINEL"
_ENV_GRACE_S = "JARVIS_SOVEREIGN_SELF_TERMINATE_GRACE_S"

_METADATA_BASE = "http://metadata.google.internal/computeMetadata/v1"
_METADATA_HEADERS = {"Metadata-Flavor": "Google"}

_fired_lock = threading.Lock()
_fired = False


def self_terminate_enabled() -> bool:
    """Master gate. Default FALSE — self-deleting the host VM is destructive, so only
    the ephemeral crucible overlay opts in. NEVER raises."""
    return (os.environ.get(_ENV_ENABLED, "false") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _sentinel_path() -> str:
    return (os.environ.get(_ENV_SENTINEL, "") or "").strip() \
        or ".jarvis/SOVEREIGN_TERMINAL_SUCCESS"


def _grace_s() -> float:
    """Seconds to let in-flight logging/flush settle before the VM delete is issued.
    Bounded [0, 120]. NEVER raises."""
    raw = (os.environ.get(_ENV_GRACE_S, "") or "").strip()
    try:
        v = float(raw) if raw else 5.0
    except (TypeError, ValueError):
        v = 5.0
    return max(0.0, min(v, 120.0))


def _metadata(path: str) -> Optional[str]:
    """GET a metadata-server value. None off-GCE / on error. NEVER raises."""
    try:
        req = urllib.request.Request(
            f"{_METADATA_BASE}/{path}", headers=_METADATA_HEADERS,
        )
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            return resp.read().decode("utf-8").strip()
    except Exception:  # noqa: BLE001 — off-GCE or metadata unreachable
        return None


def _instance_identity() -> Optional[Tuple[str, str, str]]:
    """Resolve (project, zone, instance_name) from the metadata server. None when not
    running on GCE. NEVER raises."""
    project = _metadata("project/project-id")
    name = _metadata("instance/name")
    zone_raw = _metadata("instance/zone")  # "projects/<num>/zones/us-central1-a"
    if not project or not name or not zone_raw:
        return None
    zone = zone_raw.rsplit("/", 1)[-1]
    return project, zone, name


def _sa_token() -> Optional[str]:
    """Fetch the instance default service-account OAuth token from the metadata
    server. None on error. NEVER raises."""
    raw = _metadata("instance/service-accounts/default/token")
    if not raw:
        return None
    try:
        return json.loads(raw).get("access_token")
    except Exception:  # noqa: BLE001
        return None


def mark_terminal_success(pr_url: str) -> None:
    """Write the TERMINAL_SUCCESS sentinel (PR URL + timestamp) and record an episodic
    transition. Idempotent-safe to call repeatedly. NEVER raises."""
    try:
        path = _sentinel_path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        payload = {
            "state": "TERMINAL_SUCCESS",
            "pr_url": pr_url,
            "marked_at_unix": time.time(),
        }
        # atomic temp+rename
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, indent=2))
        os.replace(tmp, path)
        logger.info(
            "[SovereignTermination] TERMINAL_SUCCESS marked — graduation PR=%s", pr_url,
        )
    except Exception:  # noqa: BLE001
        logger.debug("[SovereignTermination] mark swallowed", exc_info=True)


def flush_state_vault() -> bool:
    """Synchronously flush the .jarvis state to the GCS Vault one last time (reuses
    the existing state_persistence_daemon native-GCS push). Returns True on a clean
    push. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.state_persistence_daemon import (
            _gcs_push_blocking, _src, _target,
        )
        ok = bool(_gcs_push_blocking(_src(), _target()))
        logger.info("[SovereignTermination] final GCS flush ok=%s", ok)
        return ok
    except Exception as exc:  # noqa: BLE001
        logger.warning("[SovereignTermination] final GCS flush degraded: %s", exc)
        return False


def sever_compute() -> bool:
    """Delete THIS GCE VM via the metadata-server SA token + Compute REST API
    (stdlib only; no gcloud). Returns True iff the delete request was accepted.
    NEVER raises. Off-GCE / no-IAM → returns False (the Spot max-lifetime is the
    backstop; the PR is already open)."""
    ident = _instance_identity()
    if ident is None:
        logger.warning(
            "[SovereignTermination] not on GCE (no metadata) — cannot self-delete; "
            "relying on Spot max-lifetime backstop",
        )
        return False
    project, zone, name = ident
    token = _sa_token()
    if not token:
        logger.warning(
            "[SovereignTermination] no SA token from metadata — cannot self-delete",
        )
        return False
    url = (
        f"https://compute.googleapis.com/compute/v1/projects/{project}"
        f"/zones/{zone}/instances/{name}"
    )
    try:
        req = urllib.request.Request(url, method="DELETE", headers={
            "Authorization": f"Bearer {token}",
        })
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            status = resp.status
        logger.warning(
            "[SovereignTermination] SELF-DELETE issued for %s/%s/%s (status=%s) — "
            "compute severance in progress", project, zone, name, status,
        )
        return 200 <= status < 300
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[SovereignTermination] self-delete request failed (%s) — VM not deleted; "
            "Spot max-lifetime is the backstop", exc,
        )
        return False


def trigger_self_termination(pr_url: str) -> bool:
    """The full self-destruct sequence, fired when a graduation PR opens successfully:
    mark TERMINAL_SUCCESS → flush GCS → sever compute. Gated, idempotent (sentinel +
    process guard), fail-soft. Returns True iff the sever was issued. NEVER raises —
    a graduation PR is already open and must never be undone by a teardown error."""
    global _fired
    try:
        if not self_terminate_enabled():
            return False
        if not pr_url:
            return False
        with _fired_lock:
            if _fired:
                return False
            _fired = True
        logger.warning(
            "[SovereignTermination] graduation PR opened (%s) — initiating sovereign "
            "self-termination: mark → flush → sever", pr_url,
        )
        mark_terminal_success(pr_url)
        flush_state_vault()
        # Brief grace so the PR-open + flush logs land before the VM vanishes.
        time.sleep(_grace_s())
        return sever_compute()
    except Exception:  # noqa: BLE001 — NEVER undo a graduation on a teardown error
        logger.debug("[SovereignTermination] trigger swallowed", exc_info=True)
        return False


__all__ = [
    "self_terminate_enabled",
    "mark_terminal_success",
    "flush_state_vault",
    "sever_compute",
    "trigger_self_termination",
]
