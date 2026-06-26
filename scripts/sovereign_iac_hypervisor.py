#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Sovereign IaC Hypervisor -- project the Trinity into GCP, run the unsimulated
cross-repo surgery on a 32GB node, stream it back to the local terminal, self-burn.

This is the venue that finally runs the UNSIMULATED 3-repo surgery. The 16GB M1
cannot host the full Trinity prebake -> air-gap compose; a 32GB Linux node can.
This standalone orchestrator:

    1. CLOUD PROJECTOR      provision an e2-standard-8 (32GB) Spot Linux node with
                            Docker + a node-side Dead-Man's Burn watchdog injected
                            via startup-script (metadata-SA-token Compute REST
                            self-DELETE -- the T+221s-proven pattern).
    2. SYNC BRIDGE          rsync/scp the 3 repos (jarvis/prime/reactor) into
                            /opt/trinity/{jarvis,prime,reactor} on the node,
                            excluding .git/__pycache__/node_modules/.venv/data.
    3. REMOTE SURGERY       SSH-exec the Trinity Sandbox surgery remotely (the WAN
                            prebake happens ON the node, then the air-gap compose),
                            STREAMING stdout/stderr back to the LOCAL terminal in
                            real-time. The operator watches the Blast-Radius
                            Visualizer + FRACTURE/PASS live. Capture the verdict.
    4. THE ULTIMATE BURN    in a `finally` that ALWAYS runs (PASS / FRACTURE /
                            exception / SSH-drop): local `gcloud instances delete`
                            + the remote dead-man self-DELETEs independently + the
                            Spot DELETE-on-preempt + max_run_duration backstop ->
                            QUADRUPLE teardown. No orphaned 32GB node under ANY exit.

~80% COMPOSITION of proven primitives (bake_jprime_golden_image.py structure,
failover_deadman.py self-DELETE pattern, ignite_sovereign_cloud_node.py
provisioning, the Trinity Sandbox Matrix as the remote payload). REAL-MONEY infra
-> default-OFF (JARVIS_IAC_HYPERVISOR_ENABLED), triple-gated (--execute +
--i-understand-this-spends-money), --dry-run default prints the full plan + every
gcloud/ssh/rsync command WITHOUT executing.

Standalone by design -- imports NOTHING from the JARVIS core that requires the
heavy runtime. It optionally reuses build_deadman_startup_script() if importable;
otherwise it falls back to an embedded equivalent so the script is self-contained.

Usage:
    # default DRY-RUN: print the full plan + every command, spend nothing
    JARVIS_IAC_HYPERVISOR_ENABLED=1 python3 scripts/sovereign_iac_hypervisor.py --dry-run

    # actually run (operator-gated -- spends money):
    JARVIS_IAC_HYPERVISOR_ENABLED=1 python3 scripts/sovereign_iac_hypervisor.py \
        --execute --i-understand-this-spends-money
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import hashlib
import json
import os
import pathlib
import random
import re
import shlex
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# Repo root (the jarvis repo == the cwd repo).
# --------------------------------------------------------------------------- #
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# --------------------------------------------------------------------------- #
# Master gate + triple-gate env names.
# --------------------------------------------------------------------------- #
_ENV_MASTER = "JARVIS_IAC_HYPERVISOR_ENABLED"

# --------------------------------------------------------------------------- #
# Defaults (every value overridable via argparse; argparse defaults read env).
# No hardcoding -- machine-type, zone, timeouts, excludes, max-run all env/arg.
# --------------------------------------------------------------------------- #
_DEFAULT_PROJECT = os.environ.get("GCP_PROJECT", "jarvis-473803")
_DEFAULT_ZONE = os.environ.get("GCP_ZONE", "us-central1-a")
_DEFAULT_MACHINE = os.environ.get("JARVIS_IAC_MACHINE", "e2-standard-8")
_DEFAULT_BOOT_DISK = os.environ.get("JARVIS_IAC_BOOT_DISK_SIZE", "100GB")
_DEFAULT_DEBIAN_IMAGE_FAMILY = os.environ.get("JARVIS_IAC_SOURCE_IMAGE_FAMILY", "debian-12")
_DEFAULT_DEBIAN_IMAGE_PROJECT = os.environ.get("JARVIS_IAC_SOURCE_IMAGE_PROJECT", "debian-cloud")
_DEFAULT_MAX_RUN_DURATION_S = int(os.environ.get("JARVIS_IAC_MAX_RUN_DURATION_S", "3600"))
_DEFAULT_READY_TIMEOUT_S = int(os.environ.get("JARVIS_IAC_READY_TIMEOUT_S", "600"))
_DEFAULT_DEADMAN_IDLE_TIMEOUT_S = int(os.environ.get("JARVIS_IAC_DEADMAN_IDLE_TIMEOUT_S", "900"))
_DEFAULT_DEADMAN_CHECK_INTERVAL_S = int(os.environ.get("JARVIS_IAC_DEADMAN_CHECK_INTERVAL_S", "120"))
_DEFAULT_DEADMAN_BOOT_GRACE_S = int(os.environ.get("JARVIS_IAC_DEADMAN_BOOT_GRACE_S", "300"))

# The remote root the 3 repos sync into.
_REMOTE_TRINITY_ROOT = os.environ.get("JARVIS_IAC_REMOTE_ROOT", "/opt/trinity")

# --------------------------------------------------------------------------- #
# Git-clone transport (JARVIS_IAC_SYNC_TRANSPORT=git) -- the node clones origin
# at the EXACT local HEAD commit over FAST WAN, replacing the <1MB/s IAP tar-pipe
# (which runs over the SSH tunnel and killed 3 A1 soak runs). 581MB over WAN is
# ~1-2min vs ~10min+ over the tar-pipe. Parity-guaranteed: local HEAD must be on
# origin (pushed) and the node's resulting HEAD must == the local sha. PUBLIC
# repo -> ANONYMOUS clone, NO token.
#
# Bounded clone timeout (581MB over WAN ~1-2min; 300s == ample margin).
_DEFAULT_GIT_CLONE_TIMEOUT_S = int(
    os.environ.get("JARVIS_IAC_GIT_CLONE_TIMEOUT_S", "300")
)
# Strict secret-injection timeout. A node without .env is USELESS -> fail-CLOSED
# fast (do NOT keep a secretless node warm). 30s == strict.
_DEFAULT_SECRET_TIMEOUT_S = int(os.environ.get("JARVIS_IAC_SECRET_TIMEOUT_S", "30"))
# The secret/untracked files git clone CANNOT bring (gitignored). Comma-separated.
# `.env` always required; operator may add untracked sqlite / `.jarvis/*.jsonl`
# ledgers the test needs. NEVER log the CONTENTS of these (paths ok).
_DEFAULT_SECRET_FILES = os.environ.get("JARVIS_IAC_SECRET_FILES", ".env")
# Overlap the deps install (reads requirements.txt -- needs NO secret) with the
# secret injection -> faster boot. Env-gated; default ON.
_DEFAULT_CONCURRENT_DEPS = os.environ.get("JARVIS_IAC_CONCURRENT_DEPS", "true")
# The node-side deps install command (empty == skip gracefully). Uses a node pip
# cache dir if cheap (PIP_CACHE_DIR) -- the main win is the concurrency overlap.
_DEFAULT_DEPS_CMD = os.environ.get("JARVIS_IAC_DEPS_CMD", "")

# Marker stamped into a sync-failure detail to classify it as a BURN failure (a
# secret transfer failed -> the node is useless, do NOT keep-warm).
SYNC_FAILURE_BURN = "SECRET_FAILURE_BURN"


class GitTransportError(RuntimeError):
    """Raised on a fail-CLOSED git-transport parity violation (HEAD not on
    origin, or the node's checked-out HEAD != the local sha). The caller maps
    this to a transport failure (clone/parity -> resumable keep-warm; secret ->
    burn). NEVER carries secret file contents."""

# Default rsync excludes -- keep the beam lean (env-overridable, comma-separated).
_DEFAULT_RSYNC_EXCLUDES = os.environ.get(
    "JARVIS_IAC_RSYNC_EXCLUDES",
    ".git,__pycache__,node_modules,.venv,venv,.mypy_cache,.pytest_cache,*.pyc,data,models,.ouroboros,autopsy_reports",
)

# The remote surgery command (env-overridable -- the real trinity runner).
_DEFAULT_SURGERY_CMD = os.environ.get(
    "JARVIS_IAC_SURGERY_CMD",
    "python3 scripts/cross_repo_first_surgery.py --run",
)

# The remote prebake command -- the WAN Docker image layer build that happens ON
# the node (it has WAN) BEFORE the air-gapped compose. This is the step that hits
# first-run friction (a PyPI timeout pulling wheels). Streaming the remote
# `docker build` layer output line-by-line + checkpointing it as its own phase is
# exactly why a resume can skip a completed sync + re-run only the prebake.
# Empty == prebake is folded into the surgery command (legacy single-exec).
_DEFAULT_PREBAKE_CMD = os.environ.get("JARVIS_IAC_PREBAKE_CMD", "")

# The remote boot command -- the air-gapped compose bring-up that happens AFTER
# the prebake and BEFORE the surgery. Streamed as the `booted` phase. Empty ==
# the boot is folded into the surgery command (legacy single-exec).
_DEFAULT_BOOT_CMD = os.environ.get("JARVIS_IAC_BOOT_CMD", "")

# Completion-sentinel the surgery writes -- the node-side dead-man fires
# IMMEDIATELY when it appears (don't wait the idle timeout).
_COMPLETION_SENTINEL = os.environ.get(
    "JARVIS_IAC_COMPLETION_SENTINEL", "/var/run/sovereign_surgery_complete"
)

# Local autopsy output dir.
_AUTOPSY_DIR = os.environ.get("JARVIS_IAC_AUTOPSY_DIR", "autopsy_reports")

# --------------------------------------------------------------------------- #
# Checkpoint ledger (resume-don't-restart). A local JSON file records the live
# node + per-phase completion so a 40-min multi-stage run that hits first-run
# friction (PyPI timeout in the prebake, an SSH blip) can be RE-RUN to RESUME
# from the first incomplete phase against the still-warm node, instead of
# re-provisioning + re-syncing from zero. Env-overridable path -- no hardcoding.
# --------------------------------------------------------------------------- #
_DEFAULT_STATE_PATH = os.environ.get("JARVIS_IAC_STATE_PATH", ".hypervisor_state.json")

# The node-side dead-man idle timeout the operator can tune so a warm node left
# for resume is bounded (no infinite orphan) yet generous enough to re-run an
# --execute resume. This is the env the spec calls out; it feeds the same
# dead-man builder knob (_DEFAULT_DEADMAN_IDLE_TIMEOUT_S) when set.
_DEFAULT_NODE_IDLE_TIMEOUT_S = int(
    os.environ.get(
        "JARVIS_IAC_NODE_IDLE_TIMEOUT_S",
        os.environ.get("JARVIS_IAC_DEADMAN_IDLE_TIMEOUT_S", "3600"),
    )
)

# The ordered pipeline phases recorded in the ledger. The orchestrator walks
# these in order; a RESUME skips every phase already marked complete and starts
# at the first incomplete one.
_PHASE_ORDER: List[str] = [
    "provisioned",
    "docker_ready",
    "synced",
    "prebaked",
    "booted",
    "surgery_done",
]

# Readiness sentinel written by the node startup-script once Docker is up.
_READY_SENTINEL = "/var/run/sovereign_iac_ready"

# Verdict markers emitted by the remote surgery (mirrors cross_repo_first_surgery).
_VERDICT_PASS = "VERDICT: PASS"
_VERDICT_FRACTURE = "SOVEREIGN YIELD: CROSS-REPO FRACTURE"


# --------------------------------------------------------------------------- #
# Detached remote surgery (decouple the surgery from the SSH session lifetime).
#
# The deterministic run-#15 bug: the surgery rode ONE long-lived streaming SSH
# session (`gcloud compute ssh --command=<whole surgery>`). During the heavy pip
# install the IAP tunnel dropped (`Broken pipe / Connection closed by remote host
# / rc=255`) and the harness fell over -- even though the NODE was fine. The fix:
# the SSH call LAUNCHES the surgery DETACHED (setsid/nohup/systemd-run) and returns
# immediately; the local harness then POLLS the node with SHORT disposable SSH
# probes (exp-backoff + jitter), reading a node-side `soak_state.json` + a
# `soak_in_progress.lock`, and TAILS `/tmp/surgery.out` by byte-offset (zero loss /
# zero dup across a drop). A broken pipe on any probe is SWALLOWED + retried.
#
# Master gate: default-ON. OFF -> the legacy single long-stream path (byte-identical).
# --------------------------------------------------------------------------- #
def _env_truthy(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


_DETACHED_SURGERY_ENABLED = _env_truthy("JARVIS_IAC_DETACHED_SURGERY_ENABLED", "true")

# Node-side surgery workspace + signal files (derived from the established trinity
# workspace root -- no fresh hardcoded literal). The structured state + lock live
# alongside the synced jarvis repo so the autopsy/keep-warm logic can find them.
_REMOTE_JARVIS_DIR = f"{_REMOTE_TRINITY_ROOT}/jarvis"
_DEFAULT_SOAK_STATE_PATH = os.environ.get(
    "JARVIS_IAC_SOAK_STATE_PATH", f"{_REMOTE_JARVIS_DIR}/soak_state.json"
)
_DEFAULT_SOAK_LOCK_PATH = os.environ.get(
    "JARVIS_IAC_SOAK_LOCK_PATH", f"{_REMOTE_JARVIS_DIR}/soak_in_progress.lock"
)
# The detached surgery stdout sink the launcher redirects into (and the local
# offset-tailer streams from). Env-overridable; defaults to the established path.
_DEFAULT_SURGERY_OUT_PATH = os.environ.get(
    "JARVIS_IAC_SURGERY_OUT_PATH", "/tmp/surgery.out"
)

# Poll/reconnect loop tuning (exp-backoff base/cap + jitter, all env-tuned).
_DEFAULT_POLL_BASE_S = float(os.environ.get("JARVIS_IAC_POLL_BASE_S", "3"))
_DEFAULT_POLL_CAP_S = float(os.environ.get("JARVIS_IAC_POLL_CAP_S", "30"))
_DEFAULT_POLL_JITTER_S = float(os.environ.get("JARVIS_IAC_POLL_JITTER_S", "2"))
# A single disposable SSH probe is SHORT -- it reads a small file or a byte range.
_DEFAULT_PROBE_TIMEOUT_S = float(os.environ.get("JARVIS_IAC_PROBE_TIMEOUT_S", "45"))
# Liveness deadline: N CONSECUTIVE failed probes whose elapsed wall exceeds this
# bound == the node is genuinely unreachable (terminate + reap). Generous so a
# transient WAN blip during pip install (the run-#15 class) never trips it.
_DEFAULT_LIVENESS_DEADLINE_S = float(
    os.environ.get("JARVIS_IAC_LIVENESS_DEADLINE_S", "900")
)
# Absolute wall ceiling for the whole detached poll loop (hard stop -> reap).
# 0/unset -> falls back to the surgery timeout (--surgery-timeout-s).
_DEFAULT_MAX_WALL_S = float(os.environ.get("JARVIS_IAC_MAX_WALL_SECONDS", "0"))


# --------------------------------------------------------------------------- #
# Fault-tolerant observability (Omni-Soak #2 reaped-completing-surgery fix).
#
# Master gate default-OFF -> the CURRENT byte-offset tail + dumb-wall behavior is
# byte-identical. When armed it swaps in four decoupled mechanisms:
#   (1) a node-side anti-starvation HEARTBEAT (nice/ionice elevated) that ticks
#       soak_state.json.last_active even during a long-quiet step -> liveness is
#       judged by last_active ADVANCING, not by log output;
#   (2) a line-safe SIZE-AWARE delta sync replacing the byte-offset tail (resumes
#       on drop from last_synced_size, never emits a half line / split utf-8);
#   (3) a MANDATORY artifact-rescue phase (scp + sha256 verify) before ANY
#       teardown, with a dead-SSH OUT-OF-BAND gcloud fallback (serial console +
#       disk snapshot) -> we NEVER burn a node before its data is local;
#   (4) a dual-boundary phase-adaptive wall (extend on advancing heartbeat,
#       capped HARD by MAX_PHASE_CEILING -> a zombie ticking-but-stuck swarm is
#       still reaped, no infinite extend).
# --------------------------------------------------------------------------- #
def _env_truthy_off(name: str) -> bool:
    """Master-gate resolver defaulting to OFF (byte-identical legacy behavior)."""
    return _env_truthy(name, "false")


_FAULT_TOLERANT_OBS_ENABLED = _env_truthy_off("JARVIS_IAC_FAULT_TOLERANT_OBS_ENABLED")


# --------------------------------------------------------------------------- #
# SOAK GOLDEN IMAGE (kills the ~20-min pip-install sink).
#
# Master gate default-OFF -> the CURRENT debian-12 + full-pip path is byte-
# identical. When armed AND the jarvis-soak-golden image exists, node-create
# boots from the golden image (deps pre-installed) and the surgery's deps step
# DETECTS pre-installed deps and SKIPS the pip install. Staleness: if the
# image's requirements.txt sha label != the current sha, the deps step
# DELTA-ensures (pip the missing/changed only) -- never silently runs stale.
#
# CONSTRAINT 3 -- INDESTRUCTIBLE bootstrapper: if the golden image fails to boot
# OR the deps-present probe fails within the verify timeout, the surgery logs a
# LOUD warning and FALLS BACK to the raw Debian + full pip-install path. A
# corrupt/missing image NEVER hard-blocks a run (completion > speed).
# --------------------------------------------------------------------------- #
_SOAK_GOLDEN_ENABLED = _env_truthy_off("JARVIS_IAC_SOAK_GOLDEN_ENABLED")
_DEFAULT_SOAK_GOLDEN_IMAGE_FAMILY = os.environ.get(
    "JARVIS_IAC_SOAK_GOLDEN_IMAGE_FAMILY", "jarvis-soak-golden"
)
# The label key the baker stamps the requirements.txt sha into.
_GOLDEN_REQ_SHA_LABEL = os.environ.get(
    "JARVIS_IAC_GOLDEN_REQ_SHA_LABEL", "jarvis_req_sha"
)
# How long the deps-present probe gets before the indestructible fallback fires.
_DEFAULT_GOLDEN_VERIFY_TIMEOUT_S = int(
    os.environ.get("JARVIS_IAC_GOLDEN_VERIFY_TIMEOUT_S", "120")
)
# The repo requirements.txt (for the local-side staleness sha compute).
_DEFAULT_REQUIREMENTS_PATH = os.environ.get(
    "JARVIS_IAC_REQUIREMENTS_PATH", "requirements.txt"
)
# The node-side file the baker stamps the baked requirements sha into (read by
# the surgery's staleness check). Absent on a non-golden node -> mismatch.
_GOLDEN_BAKED_SHA_PATH = os.environ.get(
    "JARVIS_IAC_GOLDEN_BAKED_SHA_PATH", "/etc/jarvis_soak_golden_sha"
)


def _soak_golden_enabled(args: Optional["argparse.Namespace"] = None) -> bool:
    """Resolve the soak-golden master gate from args (if present) else env.
    Default-OFF -> the current debian-12 + full-pip behavior (byte-identical)."""
    if args is not None:
        val = getattr(args, "soak_golden", None)
        if val is not None:
            return bool(val)
    return _env_truthy_off("JARVIS_IAC_SOAK_GOLDEN_ENABLED")


def requirements_sha(req_path: str) -> str:
    """sha256[:16] of requirements.txt -- the staleness stamp (matches the baker).

    Fail-soft: a missing file yields 'norequirements' so the staleness check
    treats it as a guaranteed mismatch (delta-ensure / full path), never a crash.
    """
    try:
        data = pathlib.Path(req_path).read_bytes()
    except Exception:  # noqa: BLE001
        return "norequirements"
    return hashlib.sha256(data).hexdigest()[:16]


def golden_image_status(
    args: argparse.Namespace,
) -> Tuple[bool, Optional[str]]:
    """Describe the golden image: (exists, req_sha_label_or_None). Fail-soft.

    Reads the most-recent image in the family + its req-sha label via gcloud.
    Any failure -> (False, None) so the caller uses the debian-12 + pip path.
    """
    family = getattr(args, "soak_golden_image_family", _DEFAULT_SOAK_GOLDEN_IMAGE_FAMILY)
    rc, out = _run([
        "gcloud", "compute", "images", "describe-from-family", family,
        f"--project={args.project}",
        f"--format=value(labels.{_GOLDEN_REQ_SHA_LABEL})",
    ])
    if rc != 0:
        return False, None
    label = (out or "").strip() or None
    return True, label

# Heartbeat: node-side setsid loop interval + elevated OS priority (so a swarm
# redlining CPU/IO at 100% can NEVER starve/OOM-kill it -> no false freeze).
_DEFAULT_HEARTBEAT_INTERVAL_S = float(
    os.environ.get("JARVIS_IAC_HEARTBEAT_INTERVAL_S", "10")
)
_DEFAULT_HEARTBEAT_NICE = os.environ.get("JARVIS_IAC_HEARTBEAT_NICE", "-20")
# ionice realtime class (-c1) preferred; falls back to best-effort highest (-c2 -n0)
# at runtime if -c1 is denied (needs CAP_SYS_ADMIN / root).
_DEFAULT_HEARTBEAT_IONICE_CLASS = os.environ.get("JARVIS_IAC_HEARTBEAT_IONICE_CLASS", "1")
_DEFAULT_HEARTBEAT_IONICE_PRIO = os.environ.get("JARVIS_IAC_HEARTBEAT_IONICE_PRIO", "0")
# Liveness staleness: last_active must advance within this bound or the heartbeat
# is judged FROZEN (a true hang, not a quiet step). Generous default.
_DEFAULT_HEARTBEAT_STALE_S = float(
    os.environ.get("JARVIS_IAC_HEARTBEAT_STALE_S", "120")
)

# Per-phase wall allowances (env-tunable). deps gets a tight budget; the swarm
# fanout / soak a wider one. Active (advancing heartbeat) extends WITHIN the per-
# phase MAX_PHASE_CEILING -- never past it (CONSTRAINT 4: trust but bound).
def _phase_allowance(phase: str) -> float:
    key = "JARVIS_IAC_PHASE_ALLOWANCE_" + re.sub(r"[^A-Z0-9]", "_", (phase or "").upper())
    default = _PHASE_ALLOWANCE_DEFAULTS.get((phase or "").lower(), 1800.0)
    return float(os.environ.get(key, str(default)))


def _phase_ceiling(phase: str) -> float:
    key = "JARVIS_IAC_PHASE_CEILING_" + re.sub(r"[^A-Z0-9]", "_", (phase or "").upper())
    default = _PHASE_CEILING_DEFAULTS.get((phase or "").lower(), 5400.0)
    return float(os.environ.get(key, str(default)))


_PHASE_ALLOWANCE_DEFAULTS: Dict[str, float] = {
    "deps": float(os.environ.get("JARVIS_IAC_PHASE_ALLOWANCE_DEPS", "900")),
    "inject": float(os.environ.get("JARVIS_IAC_PHASE_ALLOWANCE_INJECT", "1800")),
    "soak": float(os.environ.get("JARVIS_IAC_PHASE_ALLOWANCE_SOAK", "3600")),
    "audit": float(os.environ.get("JARVIS_IAC_PHASE_ALLOWANCE_AUDIT", "900")),
}
# Absolute per-phase ceiling: the dynamic heartbeat extension is CAPPED here so a
# `while True` that still ticks the heartbeat is reaped, never extended forever.
_PHASE_CEILING_DEFAULTS: Dict[str, float] = {
    "deps": float(os.environ.get("JARVIS_IAC_PHASE_CEILING_DEPS", "2700")),
    "inject": float(os.environ.get("JARVIS_IAC_PHASE_CEILING_INJECT", "5400")),
    "soak": float(os.environ.get("JARVIS_IAC_PHASE_CEILING_SOAK", "10800")),
    "audit": float(os.environ.get("JARVIS_IAC_PHASE_CEILING_AUDIT", "2700")),
}
# Global hard absolute ceiling across ALL phases (the non-negotiable backstop --
# 0/unset -> falls back to max_wall / surgery timeout).
_DEFAULT_GLOBAL_CEILING_S = float(
    os.environ.get("JARVIS_IAC_GLOBAL_CEILING_SECONDS", "0")
)

# Artifact rescue: where the pulled black-box lands locally, the per-pull retry
# count, and the artifact manifest (env-tunable). Rescue runs BEFORE every burn.
_DEFAULT_RESCUE_DIR = os.environ.get("JARVIS_IAC_RESCUE_DIR", "rescue_artifacts")
_DEFAULT_RESCUE_RETRIES = int(os.environ.get("JARVIS_IAC_RESCUE_RETRIES", "3"))
_DEFAULT_RESCUE_TIMEOUT_S = float(os.environ.get("JARVIS_IAC_RESCUE_TIMEOUT_S", "120"))
# The remote artifacts pulled before burn (relative to the jarvis repo unless abs).
_RESCUE_ARTIFACTS: List[str] = [
    _DEFAULT_SOAK_STATE_PATH,
    _DEFAULT_SURGERY_OUT_PATH,
    f"{_REMOTE_JARVIS_DIR}/.ouroboros/sessions",
    f"{_REMOTE_JARVIS_DIR}/a1_runs",
]


# --------------------------------------------------------------------------- #
# Logging.
# --------------------------------------------------------------------------- #
def _log(msg: str) -> None:
    print(f"[IAC] {msg}", flush=True)


def _abort(msg: str) -> None:
    print(f"[IAC ABORTED: {msg}]", flush=True)


# --------------------------------------------------------------------------- #
# Checkpoint ledger -- the resume-don't-restart state file.
# --------------------------------------------------------------------------- #
class CheckpointLedger:
    """A local `.hypervisor_state.json` ledger recording the live node + per-phase
    completion so an --execute that hit a *resumable* mid-pipeline failure can be
    RE-RUN to resume from the first incomplete phase against the still-warm node.

    Schema (JSON):
        {
          "run_id": "<cli-passed or now-stamp>",
          "node_name": "...", "zone": "...", "project": "...",
          "external_ip": "<ip or ''>",   # connection info (best-effort)
          "phases": { "<phase>": {"status": "complete", "ts": "<iso>"}, ... },
          "updated": "<iso>"
        }

    Atomic writes (tmpfile + os.replace). Fail-soft: a corrupt/absent ledger
    reads as empty; a write error logs and is swallowed (the run still works,
    it just loses resumability for that step). NO authority -- it never decides
    to spend money on its own; resume only happens when the node is verified
    ALIVE by the orchestrator.
    """

    def __init__(self, path: str) -> None:
        self.path = pathlib.Path(path)

    # -- read -------------------------------------------------------------- #
    def read(self) -> Dict[str, Any]:
        try:
            if not self.path.exists():
                return {}
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
            return {}
        except Exception as exc:  # noqa: BLE001 -- a corrupt ledger reads as empty
            _log(f"checkpoint read failed ({exc!r}); treating as no checkpoint")
            return {}

    # -- write (atomic) ---------------------------------------------------- #
    def write(self, data: Dict[str, Any]) -> None:
        try:
            data = dict(data)
            data["updated"] = _iso_now()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                prefix=self.path.name + ".", suffix=".tmp",
                dir=str(self.path.parent),
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, indent=2, sort_keys=True)
                os.replace(tmp, str(self.path))
            finally:
                try:
                    if os.path.exists(tmp):
                        os.unlink(tmp)
                except OSError:
                    pass
        except Exception as exc:  # noqa: BLE001 -- a write error never crashes the run
            _log(f"checkpoint write failed ({exc!r}); resumability degraded for this step")

    # -- mutate ------------------------------------------------------------ #
    def init_run(
        self, *, run_id: str, node_name: str, zone: str, project: str,
        external_ip: str = "",
    ) -> Dict[str, Any]:
        """Seed a fresh ledger for a brand-new node (clears any prior phases)."""
        data: Dict[str, Any] = {
            "run_id": run_id,
            "node_name": node_name,
            "zone": zone,
            "project": project,
            "external_ip": external_ip,
            "phases": {},
        }
        self.write(data)
        return data

    def mark_phase(self, data: Dict[str, Any], phase: str, **info: Any) -> Dict[str, Any]:
        """Stamp *phase* complete and persist atomically. `info` (e.g. external_ip)
        is merged at the top level so connection info can be recorded as it lands."""
        phases = dict(data.get("phases") or {})
        phases[phase] = {"status": "complete", "ts": _iso_now()}
        data["phases"] = phases
        for k, v in info.items():
            data[k] = v
        self.write(data)
        return data

    def clear(self) -> None:
        try:
            if self.path.exists():
                self.path.unlink()
        except OSError as exc:
            _log(f"checkpoint clear failed ({exc!r}); next run uses --fresh semantics anyway")

    # -- queries ----------------------------------------------------------- #
    @staticmethod
    def phase_complete(data: Dict[str, Any], phase: str) -> bool:
        rec = (data.get("phases") or {}).get(phase) or {}
        return rec.get("status") == "complete"

    @classmethod
    def first_incomplete(cls, data: Dict[str, Any]) -> Optional[str]:
        """First phase in _PHASE_ORDER not yet complete (None == all done)."""
        for phase in _PHASE_ORDER:
            if not cls.phase_complete(data, phase):
                return phase
        return None

    @classmethod
    def completed_phases(cls, data: Dict[str, Any]) -> List[str]:
        return [p for p in _PHASE_ORDER if cls.phase_complete(data, p)]


def _iso_now() -> str:
    """ISO-8601 UTC timestamp for ledger records (stable, sortable)."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# THE single subprocess boundary for NON-STREAMING commands. ALL gcloud / ssh /
# rsync funnel through here (or the streaming variant below) so tests can
# intercept with a monkeypatch and assert dry-run never executes + assert order.
# --------------------------------------------------------------------------- #
def _run(cmd: List[str], *, timeout_s: float = 120.0) -> Tuple[int, str]:
    """Run a command fail-soft. Returns (returncode, combined_output).

    Never raises -- a non-zero rc or an exception both surface as a failure the
    caller inspects. This is the ONLY place the script touches subprocess for
    non-streaming calls (the streaming exec uses _run_streaming).
    """
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as exc:  # noqa: BLE001 -- the hypervisor never crashes on a call
        return 1, f"[run failed: {exc!r}]"


# --------------------------------------------------------------------------- #
# Streaming subprocess boundary -- line-buffered Popen, stdout streamed LIVE to
# the local terminal. Returns (returncode, captured_lines). Also injectable.
# --------------------------------------------------------------------------- #
def _run_streaming(
    cmd: List[str],
    *,
    timeout_s: float = 3600.0,
    sink: Optional[Callable[[str], None]] = None,
) -> Tuple[int, List[str]]:
    """Run *cmd* with stdout/stderr STREAMED line-by-line to the local terminal.

    The operator watches the Blast-Radius Visualizer + FRACTURE/PASS live while
    the surgery runs in the cloud. Each line is printed locally as it arrives
    (line-buffered) and also captured into the returned list for verdict parsing.

    Never raises. On any exception returns (1, captured_so_far).
    """
    emit = sink or (lambda line: print(line, end="", flush=True))
    captured: List[str] = []
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,  # line-buffered -- lines arrive + stream as the command runs
        )
        deadline = time.monotonic() + timeout_s
        assert proc.stdout is not None
        # Iterate stdout lines as they arrive -> stream to local terminal.
        for line in proc.stdout:
            captured.append(line)
            emit(line)
            if time.monotonic() > deadline:
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
                captured.append("[IAC] streaming surgery exceeded timeout -- killed\n")
                emit("[IAC] streaming surgery exceeded timeout -- killed\n")
                return 124, captured
        rc = proc.wait(timeout=30)
        return rc, captured
    except Exception as exc:  # noqa: BLE001 -- never crash the orchestrator
        captured.append(f"[IAC] streaming exec failed: {exc!r}\n")
        return 1, captured


def _make_labeled_sink(label: str, log_path: Optional[pathlib.Path]) -> Callable[[str], None]:
    """Build a sink that prefixes every streamed line with `[<label>] ` and TEES
    it to *log_path* (the per-run autopsy log) so the full live stream is also
    captured on disk for post-mortem -- the operator follows which stage is
    talking in real-time, and nothing is lost if the terminal scrolls away.

    Fail-soft on the file write (a tee failure never stops the live stream)."""
    prefix = f"[{label}] "

    def _sink(line: str) -> None:
        # Normalize the raw line (it usually carries its own trailing newline)
        # to exactly one prefixed line; stream LIVE to the local terminal.
        out = prefix + line.rstrip("\n") + "\n"
        sys.stdout.write(out)
        sys.stdout.flush()
        if log_path is not None:
            try:
                with open(log_path, "a", encoding="utf-8") as fh:
                    fh.write(out)
            except Exception:  # noqa: BLE001 -- a tee failure never blocks the stream
                pass

    return _sink


def _run_streaming_labeled(
    cmd: List[str], *, label: str, log_path: Optional[pathlib.Path] = None,
    timeout_s: float = 3600.0,
) -> Tuple[int, List[str]]:
    """Convenience: stream *cmd* live with a phase `label` prefix + tee to log.
    Used for the long phases (provision / sync / prebake / surgery)."""
    return _run_streaming(cmd, timeout_s=timeout_s, sink=_make_labeled_sink(label, log_path))


def _run_log_path(run_id: str) -> Optional[pathlib.Path]:
    """The per-run tee log `autopsy_reports/iac_run_<run-id>.log` -- the full
    streamed transcript for autopsy. Fail-soft (None if the dir can't be made)."""
    try:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", run_id)[:80] or "run"
        outdir = pathlib.Path(_AUTOPSY_DIR)
        outdir.mkdir(parents=True, exist_ok=True)
        return outdir / f"iac_run_{safe}.log"
    except Exception as exc:  # noqa: BLE001
        _log(f"run-log path setup failed ({exc!r}); streaming continues without tee")
        return None


# --------------------------------------------------------------------------- #
# Timestamp / node naming.
# --------------------------------------------------------------------------- #
def _now_stamp() -> str:
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def default_node_name(stamp: str) -> str:
    """Node name `sovereign-sandbox-<ts>` -- ts stamped from the CLI (no Date.now
    embedded in a way that could break naming)."""
    return f"sovereign-sandbox-{stamp}"


# --------------------------------------------------------------------------- #
# Excludes parsing.
# --------------------------------------------------------------------------- #
def parse_excludes(raw: str) -> List[str]:
    """Comma-separated exclude list -> clean list (drops empties)."""
    return [e.strip() for e in (raw or "").split(",") if e.strip()]


# --------------------------------------------------------------------------- #
# Dead-Man's Burn startup-script. REUSE failover_deadman.build_deadman_startup_script
# pattern if importable; else fall back to an embedded equivalent so the script
# is self-contained. The watchdog fires on: (a) the completion-sentinel file
# (immediate burn on surgery-done), (b) idle > timeout, (c) Spot preempt DELETE
# (handled by GCP), (d) max_run_duration (handled by GCP). The (a)/(b) paths are
# the node-side metadata-SA-token Compute REST self-DELETE.
# --------------------------------------------------------------------------- #
def _try_import_deadman() -> Optional[Callable[..., str]]:
    """Try to import the proven deadman builder. Returns the builder or None."""
    try:
        if str(_REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(_REPO_ROOT))
        from backend.core.ouroboros.governance.failover_deadman import (  # noqa: E402
            build_deadman_startup_script,
        )
        return build_deadman_startup_script
    except Exception:  # noqa: BLE001 -- standalone fallback if core not importable
        return None


# awk/python3 one-liners (kept as constants to avoid f-string brace conflicts).
_AWK_UPTIME = r"awk '{print int($1)}' /proc/uptime"
_AWK_ZONE = r"awk -F/ '{print $NF}'"
_PY3_TOKEN = (
    r"""python3 -c "import sys,json; """
    r"""print(json.load(sys.stdin).get('access_token',''))" """
)


def _embedded_burn_watchdog_body(
    *, idle_timeout_s: int, boot_grace_s: int, sentinel_path: str
) -> str:
    """The node-side watchdog body: self-DELETE via metadata-SA-token Compute REST
    on EITHER the completion-sentinel appearing OR idle > timeout. Pure string."""
    body = """\
#!/usr/bin/env bash
# Sovereign IaC Hypervisor Dead-Man's Burn watchdog (auto-generated).
# Self-deletes this VM when the surgery completion-sentinel appears OR the node
# has been idle > IDLE_TIMEOUT_S (and uptime > BOOT_GRACE_S). Node has NO gcloud;
# uses curl + the metadata-server SA token + Compute REST DELETE.
set -uo pipefail
export HOME=/root

BURN_LOG=/var/log/sovereign_burn.log
COMPLETION_SENTINEL=__COMPLETION_SENTINEL__
ACTIVITY_FILE=/var/run/sovereign_last_activity
IDLE_TIMEOUT_S=__IDLE_TIMEOUT_S__
BOOT_GRACE_S=__BOOT_GRACE_S__

exec >> "$BURN_LOG" 2>&1
echo "[sovereign-burn] check $(date -u +%FT%TZ) idle_timeout=${IDLE_TIMEOUT_S}s boot_grace=${BOOT_GRACE_S}s"

_meta() {
    curl -fsS -H "Metadata-Flavor: Google" \\
        "http://metadata.google.internal/computeMetadata/v1/$1" 2>/dev/null || true
}

_self_delete() {
    # Self-DELETE via GCE Compute REST API (metadata SA token + curl). No gcloud.
    TOKEN_JSON=$(_meta "instance/service-accounts/default/token")
    [ -z "${TOKEN_JSON}" ] && { echo "[sovereign-burn] no SA token -- retry"; return 1; }
    SA_TOKEN=$(echo "${TOKEN_JSON}" | PLACEHOLDER_PY3_TOKEN 2>/dev/null || true)
    [ -z "${SA_TOKEN}" ] && { echo "[sovereign-burn] could not parse SA token -- retry"; return 1; }
    PROJECT=$(_meta "project/project-id")
    INSTANCE_NAME=$(_meta "instance/name")
    ZONE_FULL=$(_meta "instance/zone")
    [ -z "${PROJECT}" ] || [ -z "${INSTANCE_NAME}" ] || [ -z "${ZONE_FULL}" ] && {
        echo "[sovereign-burn] incomplete identity -- retry"; return 1; }
    ZONE=$(echo "${ZONE_FULL}" | PLACEHOLDER_AWK_ZONE)
    DELETE_URL="https://compute.googleapis.com/compute/v1/projects/${PROJECT}/zones/${ZONE}/instances/${INSTANCE_NAME}"
    echo "[sovereign-burn] issuing self-DELETE: ${DELETE_URL}"
    HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" \\
        -X DELETE \\
        -H "Authorization: Bearer ${SA_TOKEN}" \\
        "${DELETE_URL}" 2>/dev/null || echo "0")
    echo "[sovereign-burn] Compute REST DELETE status=${HTTP_STATUS} -- compute severance in progress"
}

# (a) COMPLETION SENTINEL: surgery finished -> burn IMMEDIATELY (no grace wait).
if [ -f "${COMPLETION_SENTINEL}" ]; then
    echo "[sovereign-burn] COMPLETION SENTINEL present (${COMPLETION_SENTINEL}) -- immediate self-DELETE"
    _self_delete
    exit 0
fi

# (b) IDLE TIMEOUT: only after boot grace.
UPTIME_S=0
if [ -r /proc/uptime ]; then
    UPTIME_S=$(PLACEHOLDER_AWK_UPTIME 2>/dev/null || echo 0)
fi
if [ "${UPTIME_S}" -lt "${BOOT_GRACE_S}" ]; then
    echo "[sovereign-burn] BOOT GRACE active (uptime=${UPTIME_S}s < grace=${BOOT_GRACE_S}s) -- skip"
    exit 0
fi
# FAIL-SAFE: if the activity file does NOT exist, we CANNOT determine idleness
# (the IaC sandbox node has no Ollama bumping it) -- so DO NOT delete on a missing
# file. The no-orphan guarantee then rests on max-run-duration (GCP hard ceiling)
# + the completion-sentinel (verdict burn) + the local orchestrator delete. Only
# an EXISTING activity file older than the timeout triggers an idle self-delete.
if [ ! -f "${ACTIVITY_FILE}" ]; then
    echo "[sovereign-burn] no activity file -- cannot assess idle; relying on max-run-duration + sentinel -- skip"
    exit 0
fi
FILE_AGE_S=$(( $(date +%s) - $(stat -c %Y "${ACTIVITY_FILE}" 2>/dev/null || echo 0) ))
if [ "${FILE_AGE_S}" -lt "${IDLE_TIMEOUT_S}" ]; then
    echo "[sovereign-burn] activity recent (age=${FILE_AGE_S}s < ${IDLE_TIMEOUT_S}s) -- no action"
    exit 0
fi
echo "[sovereign-burn] IDLE > ${IDLE_TIMEOUT_S}s and uptime > boot_grace -- initiating self-DELETE"
_self_delete
"""
    return (
        body.replace("__COMPLETION_SENTINEL__", sentinel_path)
        .replace("__IDLE_TIMEOUT_S__", str(idle_timeout_s))
        .replace("__BOOT_GRACE_S__", str(boot_grace_s))
        .replace("PLACEHOLDER_AWK_UPTIME", _AWK_UPTIME)
        .replace("PLACEHOLDER_AWK_ZONE", _AWK_ZONE)
        .replace("PLACEHOLDER_PY3_TOKEN", _PY3_TOKEN)
    )


def build_startup_script(
    *,
    idle_timeout_s: int = _DEFAULT_DEADMAN_IDLE_TIMEOUT_S,
    check_interval_s: int = _DEFAULT_DEADMAN_CHECK_INTERVAL_S,
    boot_grace_s: int = _DEFAULT_DEADMAN_BOOT_GRACE_S,
    completion_sentinel: str = _COMPLETION_SENTINEL,
    ready_sentinel: str = _READY_SENTINEL,
) -> str:
    """Return the node startup-script: install Docker + start the daemon, write
    the ready sentinel, AND install the Dead-Man's Burn watchdog (systemd timer
    + nohup fallback). Pure string assembly -- no I/O, no subprocess. ASCII only.

    The WAN prebake happens ON the node (it has WAN) before the air-gapped
    compose -- the node does the full prebake -> air-gap flow locally during the
    remote surgery exec, not in this startup-script.
    """
    watchdog_body = _embedded_burn_watchdog_body(
        idle_timeout_s=idle_timeout_s,
        boot_grace_s=boot_grace_s,
        sentinel_path=completion_sentinel,
    )
    sentinel_q = shlex.quote(ready_sentinel)

    preamble = (
        "#!/usr/bin/env bash\n"
        "# Sovereign IaC Hypervisor node startup-script (auto-generated).\n"
        "# Installs Docker + starts the daemon, opens the minimal local firewall\n"
        "# (sandbox is internal-network), writes the ready sentinel, then installs\n"
        "# the Dead-Man's Burn watchdog (self-DELETE on completion-sentinel OR idle).\n"
        "set -uo pipefail\n"
        "# ROOT-CAUSE FIX: GCP startup-scripts run as root with HOME unset; export it.\n"
        "export HOME=/root\n"
        "\n"
        "LOG=/var/log/sovereign_iac_startup.log\n"
        'exec > >(tee -a "$LOG") 2>&1\n'
        'echo "[sovereign-iac] startup-script begin $(date -u +%FT%TZ) (HOME=$HOME)"\n'
        "rm -f " + sentinel_q + " || true\n"
        "\n"
    )

    docker_install = (
        "# 1. Install Docker + Compose plugin and start the daemon.\n"
        'echo "[sovereign-iac] installing Docker"\n'
        "export DEBIAN_FRONTEND=noninteractive\n"
        "apt-get update -y || true\n"
        "apt-get install -y docker.io docker-compose-plugin rsync || "
        "curl -fsSL https://get.docker.com | sh || true\n"
        "systemctl enable --now docker || service docker start || true\n"
        "# Bootstrap python3-pip + build tools at BOOT (system pkgs, no repo needed)\n"
        "# so the surgery harness deps install cleanly later (apt is fresh here).\n"
        "apt-get install -y python3-pip python3-dev build-essential || true\n"
        "\n"
        "# 2. Open the minimal local firewall (sandbox is internal-network only).\n"
        "iptables -A INPUT -i lo -j ACCEPT 2>/dev/null || true\n"
        "\n"
        "# 3. Wait for the Docker daemon to answer, then write the ready sentinel.\n"
        'echo "[sovereign-iac] waiting for docker daemon"\n'
        "for i in $(seq 1 60); do\n"
        "    if docker info >/dev/null 2>&1; then\n"
        '        echo "[sovereign-iac] docker daemon is up"\n'
        '        echo "ready ts=$(date -u +%FT%TZ)" > ' + sentinel_q + "\n"
        "        break\n"
        "    fi\n"
        "    sleep 5\n"
        "done\n"
        "mkdir -p " + shlex.quote(_REMOTE_TRINITY_ROOT) + " || true\n"
        "\n"
    )

    burn_install = (
        "# ------------------------------------------------------------------ #\n"
        "# INSTALL DEAD-MAN'S BURN: write watchdog, then systemd timer or nohup. #\n"
        "# ------------------------------------------------------------------ #\n"
        "BURN_BIN=/usr/local/bin/sovereign_burn_check.sh\n"
        "cat > \"$BURN_BIN\" << 'BURN_HELPER_EOF'\n"
        + watchdog_body
        + "BURN_HELPER_EOF\n"
        "chmod +x \"$BURN_BIN\"\n"
        "\n"
        "if [ -d /run/systemd/system ]; then\n"
        '    echo "[sovereign-iac] installing burn systemd timer + service"\n'
        "    cat > /etc/systemd/system/sovereign-burn.service << 'EOF'\n"
        "[Unit]\n"
        "Description=Sovereign IaC Dead-Man Burn Check\n"
        "After=network.target\n"
        "[Service]\n"
        "Type=oneshot\n"
        "ExecStart=/usr/local/bin/sovereign_burn_check.sh\n"
        "EOF\n"
        "    cat > /etc/systemd/system/sovereign-burn.timer << EOF\n"
        "[Unit]\n"
        "Description=Sovereign IaC Dead-Man Burn Timer\n"
        "[Timer]\n"
        "OnBootSec=" + str(check_interval_s) + "s\n"
        "OnUnitActiveSec=" + str(check_interval_s) + "s\n"
        "Unit=sovereign-burn.service\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
        "EOF\n"
        "    systemctl daemon-reload || true\n"
        "    systemctl enable --now sovereign-burn.timer || true\n"
        '    echo "[sovereign-iac] burn systemd timer installed (interval='
        + str(check_interval_s) + 's)"\n'
        "else\n"
        '    echo "[sovereign-iac] systemd not init -- starting nohup burn loop"\n'
        "    nohup bash -c '\n"
        "while true; do\n"
        "    sleep " + str(check_interval_s) + " || true\n"
        "    /usr/local/bin/sovereign_burn_check.sh || true\n"
        "done\n"
        "' >> /var/log/sovereign_burn.log 2>&1 &\n"
        '    echo "[sovereign-iac] nohup burn loop started (PID=$!)"\n'
        "fi\n"
        "\n"
        'echo "[sovereign-iac] startup-script done $(date -u +%FT%TZ)"\n'
    )

    return preamble + docker_install + burn_install


# --------------------------------------------------------------------------- #
# gcloud / ssh / rsync command builders (pure -- so dry-run can print them).
# --------------------------------------------------------------------------- #
def _resolve_node_image(args: argparse.Namespace) -> Tuple[str, str]:
    """Resolve the (image_family, image_project) the node boots from.

    SOAK GOLDEN (gated default-OFF, byte-identical when off): when
    JARVIS_IAC_SOAK_GOLDEN_ENABLED AND the jarvis-soak-golden image EXISTS, boot
    from the golden family in THIS project (deps pre-installed). Otherwise (gate
    off OR no image) use the configured debian-12 family/project -- byte-identical
    to the legacy path. Fail-soft: any describe error falls back to debian-12.
    """
    if _soak_golden_enabled(args):
        try:
            exists, _label = golden_image_status(args)
        except Exception:  # noqa: BLE001 -- never crash node-create on a probe
            exists = False
        if exists:
            family = getattr(
                args, "soak_golden_image_family", _DEFAULT_SOAK_GOLDEN_IMAGE_FAMILY
            )
            _log(f"[golden] booting from soak golden image family '{family}' "
                 "(deps pre-installed)")
            # The golden image lives in THIS project, not debian-cloud.
            return family, args.project
        _log("[golden] soak-golden enabled but image absent -- using debian-12 + pip")
    return args.source_image_family, args.source_image_project


def _create_node_cmd(
    args: argparse.Namespace, node: str, startup_script_path: str
) -> List[str]:
    """e2-standard-8 (32GB) node, max_run_duration + DELETE (cost ceiling),
    cloud-platform scope (the dead-man needs it). SPOT by default (cheapest);
    --on-demand uses STANDARD for an UNINTERRUPTED window (no preemption) when a
    multi-stage run needs to complete without a Spot reclaim mid-surgery.

    Image family/project resolved via _resolve_node_image (soak-golden when
    enabled+present, else debian-12 -- byte-identical when the gate is off)."""
    # on-demand (STANDARD, no Spot preemption) when the CLI flag is set OR the
    # JARVIS_IAC_ON_DEMAND env override is truthy -- the env path is what lets a
    # programmatic caller (the harness builds the args Namespace, not the CLI)
    # force a non-preemptible node for a long multi-stage soak that must not be
    # reclaimed mid-feast.
    on_demand = bool(getattr(args, "on_demand", False)) or (
        os.environ.get("JARVIS_IAC_ON_DEMAND", "").strip().lower()
        in ("1", "true", "yes", "on")
    )
    sched = (
        ["--provisioning-model=STANDARD"] if on_demand
        else ["--provisioning-model=SPOT"]
    )
    image_family, image_project = _resolve_node_image(args)
    return [
        "gcloud", "compute", "instances", "create", node,
        f"--project={args.project}", f"--zone={args.zone}",
        f"--machine-type={args.machine_type}",
        f"--image-family={image_family}",
        f"--image-project={image_project}",
        f"--boot-disk-size={args.boot_disk_size}",
        "--boot-disk-type=pd-balanced",
        *sched,
        "--instance-termination-action=DELETE",
        f"--max-run-duration={args.max_run_duration_s}s",
        "--scopes=cloud-platform",
        f"--metadata-from-file=startup-script={startup_script_path}",
    ]


def _ssh_cmd(args: argparse.Namespace, node: str, remote: str) -> List[str]:
    """SSH-over-IAP exec (mirrors the bake)."""
    return [
        "gcloud", "compute", "ssh", node,
        f"--project={args.project}", f"--zone={args.zone}",
        "--tunnel-through-iap", "--command", remote,
    ]


def _tar_pipe_cmd(
    args: argparse.Namespace, node: str, local: str, remote_dir: str,
    excludes: List[str],
) -> List[str]:
    """Bulletproof remote sync: `tar czf - -C <local> <excludes> . | gcloud ssh
    <node> --command 'tar xzf - -C <remote_dir>'`. Avoids the well-known
    `gcloud compute scp --recurse` IAP flakiness ('stat remote: No such file or
    directory') by streaming a tarball through the SSH tunnel and extracting it
    into the pre-created remote dir. Returns a `bash -lc` command (a shell
    pipeline) so the existing streaming runner can drive it. The tar progress
    streams to the local terminal (prefixed [sync])."""
    excl = " ".join(f"--exclude={shlex.quote(e)}" for e in excludes)
    # remote side: gcloud ssh exec that pipes stdin into `tar xzf - -C <dir>`
    remote_extract = (
        f"gcloud compute ssh {shlex.quote(node)} "
        f"--project={shlex.quote(args.project)} --zone={shlex.quote(args.zone)} "
        f"--tunnel-through-iap --command {shlex.quote('tar xzf - -C ' + shlex.quote(remote_dir))}"
    )
    pipeline = (
        f"tar czf - -C {shlex.quote(local)} {excl} . | {remote_extract}"
    )
    return ["bash", "-lc", pipeline]


def _scp_cmd(
    args: argparse.Namespace, node: str, local_dir: str, remote_dir: str,
    excludes: List[str],
) -> List[str]:
    """`gcloud compute scp --recurse` over the IAP tunnel -- the robust sync path
    (rsync-over-IAP is awkward; scp --recurse is the supported transport).

    Excludes are honored at the staging step (we sync a pre-pruned tree); the
    exclude list is recorded in the command via a trailing comment-arg the
    dry-run printer surfaces, and enforced by sync_repos_to_node staging.
    """
    return [
        "gcloud", "compute", "scp", "--recurse",
        f"--project={args.project}", f"--zone={args.zone}",
        "--tunnel-through-iap",
        local_dir, f"{node}:{remote_dir}",
    ]


def _rsync_cmd(
    args: argparse.Namespace, node: str, local_dir: str, remote_dir: str,
    excludes: List[str],
) -> List[str]:
    """rsync over the gcloud SSH transport, with --exclude per entry. Used when
    JARVIS_IAC_SYNC_TRANSPORT=rsync. Kept lean via the exclude list."""
    exclude_flags: List[str] = []
    for e in excludes:
        exclude_flags.extend(["--exclude", e])
    ssh_transport = (
        f"gcloud compute ssh --project={args.project} --zone={args.zone} "
        "--tunnel-through-iap --command"
    )
    return [
        # -v + --progress so the live stream carries per-file + progress-bar
        # output the operator can watch line-by-line during the long sync.
        "rsync", "-az", "--delete", "--progress", "-v",
        *exclude_flags,
        "-e", ssh_transport,
        local_dir.rstrip("/") + "/",
        f"{node}:{remote_dir}",
    ]


# --------------------------------------------------------------------------- #
# Git-clone transport: resolve -> clone (parity) -> (concurrent: deps || secrets).
#
# The node clones origin at the EXACT local HEAD over fast WAN. NO hardcoded
# `main` -- the EXACT commit being soaked + its branch are read from the local
# repo (parity local-dev <-> remote-soak). PUBLIC repo -> anonymous clone.
# --------------------------------------------------------------------------- #
def _git_cmd(local_root: str, *git_args: str) -> List[str]:
    """A local `git -C <root> <args...>` command (routed through `_run`)."""
    return ["git", "-C", local_root, *git_args]


def resolve_local_git_target(local_root: str) -> Tuple[str, str, str]:
    """Resolve `(origin_url, commit_sha, branch)` for the EXACT local HEAD being
    soaked -- and VERIFY HEAD is reachable on origin (pushed). NO hardcoded
    branch: the commit + branch + origin url are read live from the local repo.

    Parity guarantee: the node can only clone PUSHED commits, so if local HEAD is
    NOT on origin we fail-CLOSED here (before spending a cent on a node that would
    clone a STALE commit). Raises GitTransportError on any resolution failure or
    when HEAD is unpushed.
    """
    rc, out = _run(_git_cmd(local_root, "rev-parse", "HEAD"), timeout_s=30.0)
    if rc != 0 or not (out or "").strip():
        raise GitTransportError(f"git rev-parse HEAD failed in {local_root}: {out.strip()[:200]}")
    sha = out.strip().splitlines()[0].strip()

    rc, out = _run(_git_cmd(local_root, "rev-parse", "--abbrev-ref", "HEAD"), timeout_s=30.0)
    if rc != 0 or not (out or "").strip():
        raise GitTransportError(f"git rev-parse --abbrev-ref HEAD failed in {local_root}: {out.strip()[:200]}")
    branch = out.strip().splitlines()[0].strip()

    rc, out = _run(_git_cmd(local_root, "remote", "get-url", "origin"), timeout_s=30.0)
    if rc != 0 or not (out or "").strip():
        raise GitTransportError(f"git remote get-url origin failed in {local_root}: {out.strip()[:200]}")
    origin_url = out.strip().splitlines()[0].strip()

    # Parity gate: HEAD MUST be on origin (the node can only clone pushed commits).
    # Primary check: `git branch -r --contains <sha>` lists the remote branches
    # that contain the sha; if any origin/* contains it -> pushed.
    rc, out = _run(_git_cmd(local_root, "branch", "-r", "--contains", sha), timeout_s=30.0)
    pushed = rc == 0 and any(
        ln.strip().startswith("origin/") for ln in (out or "").splitlines()
    )
    if not pushed:
        # Fallback: ask the remote directly via ls-remote (the sha may sit on a
        # remote branch our local refs haven't fetched).
        rc2, out2 = _run(_git_cmd(local_root, "ls-remote", "origin"), timeout_s=60.0)
        pushed = rc2 == 0 and sha in (out2 or "")
    if not pushed:
        raise GitTransportError(
            f"HEAD {sha} not on origin/{branch} -- push before soak "
            f"(the node can only clone pushed commits)"
        )
    return origin_url, sha, branch


def _git_clone_remote_shell(origin_url: str, commit_sha: str, remote_dir: str) -> str:
    """The node-side shell: rm the dir, anonymous clone, checkout the EXACT sha,
    then echo the resulting HEAD (the parity probe reads the LAST line). The repo
    is PUBLIC -> anonymous clone, NO token. Quoted for safe SSH transport."""
    rd = shlex.quote(remote_dir)
    url = shlex.quote(origin_url)
    sha = shlex.quote(commit_sha)
    return (
        f"rm -rf {rd} && "
        f"git clone {url} {rd} && "
        f"git -C {rd} checkout {sha} && "
        # Emit the node HEAD behind a UNIQUE MARKER so the parity probe extracts it
        # reliably -- NOT 'the last streamed line' (a clone/checkout stderr advice
        # line can land last -> empty -> false parity burn, the run #8 wall).
        f"echo \"__NODE_HEAD__$(git -C {rd} rev-parse HEAD)\""
    )


def git_clone_on_node(
    args: argparse.Namespace, node: str, origin_url: str, commit_sha: str,
    branch: str, remote_dir: str, log_path: Optional[pathlib.Path] = None,
) -> Tuple[bool, str]:
    """SSH the node to anonymously clone *origin_url*, checkout the EXACT
    *commit_sha*, and PARITY-ASSERT the node's resulting HEAD == *commit_sha*.

    Streams the clone progress as the `synced` phase (resume-aware checkpointing
    happens in the caller on success). Bounded by JARVIS_IAC_GIT_CLONE_TIMEOUT_S.
    On a clone rc!=0 -> returns (False, detail) (resumable). On a PARITY mismatch
    (node HEAD != sha) -> raises GitTransportError (the caller burns -- a node on
    the WRONG commit defeats the entire parity invariant)."""
    timeout_s = float(getattr(
        args, "git_clone_timeout_s", _DEFAULT_GIT_CLONE_TIMEOUT_S))
    remote = _git_clone_remote_shell(origin_url, commit_sha, remote_dir)
    cmd = _ssh_cmd(args, node, remote)
    _log(f"git-clone on {node}: {origin_url}@{commit_sha[:12]} (branch {branch}) "
         f"-> {remote_dir} (anonymous, streamed, timeout={int(timeout_s)}s)")
    rc, captured = _run_streaming_labeled(
        cmd, label="synced", log_path=log_path, timeout_s=timeout_s,
    )
    if rc != 0:
        return False, f"git clone of {origin_url} failed rc={rc}: {''.join(captured).strip()[:300]}"
    # Parity probe: extract the node HEAD from behind the unique marker (robust to
    # interleaved clone/checkout stderr). Burn ONLY on a CONFIRMED real mismatch (a
    # non-empty DIFFERENT sha). An UNREADABLE probe (empty) with clone rc==0 is a
    # tooling read-failure, NOT a stale-commit soak -- warn + proceed (the exact-sha
    # clone succeeded). Burning on an empty read falsely killed run #8.
    node_head = ""
    for ln in captured:
        s = ln.strip()
        if "__NODE_HEAD__" in s:
            tail = s.split("__NODE_HEAD__", 1)[1].strip().split()
            node_head = tail[0] if tail else ""
            break
    if node_head and node_head != commit_sha:
        raise GitTransportError(
            f"node {node} HEAD parity FAILED: node checked out {node_head[:12]} "
            f"but local soak commit is {commit_sha[:12]} -- mismatch, burn (no stale-commit soak)"
        )
    if not node_head:
        _log(f"[parity] WARN: node HEAD probe unreadable (clone rc=0) -- proceeding; "
             f"the exact-sha clone @ {commit_sha[:12]} succeeded")
        return True, f"cloned (parity-probe-unreadable, clone ok) @ {commit_sha[:12]}"
    return True, f"cloned + parity-verified @ {commit_sha[:12]}"


def _secret_files() -> List[str]:
    """The configured secret/untracked files git clone can't bring. `.env` always
    included even if absent from the env list (the parity-incomplete tree needs
    it)."""
    raw = os.environ.get("JARVIS_IAC_SECRET_FILES", _DEFAULT_SECRET_FILES)
    files = [p.strip() for p in (raw or "").split(",") if p.strip()]
    if ".env" not in files:
        files.insert(0, ".env")
    # de-dup, preserve order
    seen: set = set()
    out: List[str] = []
    for f in files:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def _scp_secret_push_cmd(
    args: argparse.Namespace, node: str, local_path: str, remote_path: str,
) -> List[str]:
    """`gcloud compute scp` over IAP pushing a SINGLE secret file. Carries PATHS
    only -- NEVER the file CONTENTS (no contents ever touch the argv or the log)."""
    return [
        "gcloud", "compute", "scp",
        f"--project={args.project}", f"--zone={args.zone}",
        "--tunnel-through-iap",
        local_path, f"{node}:{remote_path}",
    ]


def inject_secrets_to_node(
    args: argparse.Namespace, node: str, remote_dir: str,
    local_root: Optional[str] = None,
) -> Tuple[bool, str]:
    """Asynchronously inject the secret/untracked files the git clone CANNOT
    bring (.env + any operator-configured untracked ledgers) into *remote_dir*.

    STRICT timeout (JARVIS_IAC_SECRET_TIMEOUT_S, default 30s). FAIL-CLOSED: if ANY
    REQUIRED secret fails to transfer (timeout / error) -> returns (False, ...) so
    the transport FAILS and the node BURNS (a node without .env is useless -- do
    NOT keep-warm). NEVER logs secret file CONTENTS (paths only).

    `.env` is REQUIRED -- if it's missing locally the injection fails-CLOSED.
    Operator-added extra files that are absent locally are skipped with a notice
    (only `.env` is hard-required)."""
    root = local_root or str(_REPO_ROOT)
    timeout_s = float(getattr(args, "secret_timeout_s", _DEFAULT_SECRET_TIMEOUT_S))
    for rel in _secret_files():
        local_path = os.path.join(root, rel)
        required = (rel == ".env")
        if not os.path.isfile(local_path):
            if required:
                return False, f"required secret '{rel}' missing locally -- cannot inject (burn)"
            _log(f"secret '{rel}' absent locally -- skipping (not required)")
            continue
        remote_path = remote_dir.rstrip("/") + "/" + rel
        # Ensure the parent dir exists on the node for nested ledgers (.jarvis/..).
        parent = os.path.dirname(rel)
        if parent:
            mkdir = f"mkdir -p {shlex.quote(remote_dir.rstrip('/') + '/' + parent)}"
            _run(_ssh_cmd(args, node, mkdir), timeout_s=timeout_s)
        _log(f"injecting secret '{rel}' -> {node}:{remote_path} (strict {int(timeout_s)}s, contents NOT logged)")
        rc, out = _run(
            _scp_secret_push_cmd(args, node, local_path, remote_path),
            timeout_s=timeout_s,
        )
        if rc != 0:
            # Do NOT echo `out` verbatim if it could carry contents -- scp errors
            # are path/transport messages, but stay conservative: report rel only.
            return False, f"secret '{rel}' transfer FAILED rc={rc} -- fail-CLOSED, burn"
    return True, "secrets injected"


def run_node_deps_install(
    args: argparse.Namespace, node: str, remote_dir: str,
    log_path: Optional[pathlib.Path] = None,
) -> Tuple[bool, str]:
    """Run the node-side deps install (reads requirements.txt -- needs NO secret).
    Designed to overlap inject_secrets_to_node for a faster boot. Uses a node pip
    cache dir (PIP_CACHE_DIR) if cheap. Empty/not-configured deps cmd -> skip
    gracefully. Bounded by the sync timeout. Returns (ok, detail). Fail-soft."""
    deps_cmd = os.environ.get("JARVIS_IAC_DEPS_CMD", _DEFAULT_DEPS_CMD).strip()
    if not deps_cmd:
        return True, "deps install skipped (no JARVIS_IAC_DEPS_CMD)"
    cache = "export PIP_CACHE_DIR=/opt/trinity/.pip_cache; mkdir -p /opt/trinity/.pip_cache; "
    remote = f"cd {shlex.quote(remote_dir)} && {cache}{deps_cmd}"
    cmd = _ssh_cmd(args, node, remote)
    _log(f"deps install on {node} (concurrent with secret injection, streamed)")
    rc, captured = _run_streaming_labeled(
        cmd, label="synced", log_path=log_path, timeout_s=float(args.sync_timeout_s),
    )
    if rc != 0:
        return False, f"deps install failed rc={rc}: {''.join(captured).strip()[:300]}"
    return True, "deps installed"


def is_secret_failure(detail: str) -> bool:
    """Classify a sync-failure *detail* as a SECRET failure (-> BURN, the node is
    useless without .env) vs a clone/parity failure (-> resumable keep-warm). The
    secret path stamps SYNC_FAILURE_BURN / the word 'secret' into its detail."""
    d = (detail or "").lower()
    return SYNC_FAILURE_BURN.lower() in d or "secret" in d


def _git_transport_sync(
    args: argparse.Namespace, node: str,
    log_path: Optional[pathlib.Path] = None,
) -> Tuple[bool, str]:
    """Run the git-clone transport for the jarvis repo: resolve the local target
    (parity gate) -> clone+checkout on the node (parity-assert) -> overlap the
    deps install with the secret injection -> join.

    Returns (ok, detail). Clone/parity failures are RESUMABLE (keep-warm); a
    SECRET failure is stamped SYNC_FAILURE_BURN so the orchestrator BURNS (a node
    without .env is useless). NEVER logs secret CONTENTS."""
    pairs = _resolve_repo_paths(args)
    # The git transport clones the jarvis repo (the cwd repo == origin). prime /
    # reactor (if configured + are their own remotes) still ride the tar-pipe;
    # the A1 soak only needs jarvis. Resolve jarvis's local root.
    jarvis_root = ""
    for name, local in pairs:
        if name == "jarvis":
            jarvis_root = local
            break
    if not jarvis_root:
        return False, "jarvis repo path unset -- cannot run git transport"

    remote_dir = f"{_REMOTE_TRINITY_ROOT}/jarvis"

    # 1) Resolve local target + parity gate (HEAD must be pushed). fail-CLOSED.
    try:
        origin_url, commit_sha, branch = resolve_local_git_target(jarvis_root)
    except GitTransportError as exc:
        return False, f"git target resolution failed (fail-CLOSED): {exc}"

    # 2) Clone + checkout on the node, parity-assert (mismatch -> burn).
    try:
        ok, detail = git_clone_on_node(
            args, node, origin_url, commit_sha, branch, remote_dir, log_path=log_path,
        )
    except GitTransportError as exc:
        # Parity violation == wrong-commit soak -> BURN (defeats the invariant).
        return False, f"{SYNC_FAILURE_BURN}: parity violation -- {exc}"
    if not ok:
        return False, detail  # clone rc!=0 -> resumable keep-warm.

    # 3) Concurrency: launch deps install ALONGSIDE the secret injection, join.
    concurrent = os.environ.get(
        "JARVIS_IAC_CONCURRENT_DEPS", _DEFAULT_CONCURRENT_DEPS
    ).strip().lower() in {"1", "true", "yes", "on"}
    deps_result: Dict[str, Tuple[bool, str]] = {}
    secret_result: Dict[str, Tuple[bool, str]] = {}

    def _deps() -> None:
        deps_result["r"] = run_node_deps_install(args, node, remote_dir, log_path=log_path)

    def _secrets() -> None:
        secret_result["r"] = inject_secrets_to_node(
            args, node, remote_dir, local_root=jarvis_root)

    if concurrent:
        import threading
        t_deps = threading.Thread(target=_deps, name="iac-deps", daemon=True)
        t_deps.start()                       # deps launched FIRST (overlaps secrets)
        _secrets()                           # secrets run on this thread
        t_deps.join(timeout=float(args.sync_timeout_s) + 30.0)
    else:
        _secrets()
        _deps()

    secret_ok, secret_detail = secret_result.get("r", (False, "secret injection did not run"))
    deps_ok, deps_detail = deps_result.get("r", (True, "deps skipped"))

    # SECRET failure -> fail-CLOSED -> BURN (the node is useless without .env).
    if not secret_ok:
        return False, f"{SYNC_FAILURE_BURN}: {secret_detail}"
    # Deps failure is resumable (a re-run re-installs); do NOT burn for deps.
    if not deps_ok:
        return False, f"deps install failed (resumable): {deps_detail}"
    return True, "synced"


def _delete_node_cmd(args: argparse.Namespace, node: str) -> List[str]:
    return [
        "gcloud", "compute", "instances", "delete", node,
        f"--project={args.project}", f"--zone={args.zone}",
        "--delete-disks=all", "--quiet",
    ]


def _describe_node_cmd(args: argparse.Namespace, node: str) -> List[str]:
    return [
        "gcloud", "compute", "instances", "describe", node,
        f"--project={args.project}", f"--zone={args.zone}",
        "--format=value(name)",
    ]


def _describe_status_cmd(args: argparse.Namespace, node: str) -> List[str]:
    """Describe the node's lifecycle status -- RUNNING means it is warm + alive
    and a RESUME can reconnect to it (vs burned / preempted / terminated)."""
    return [
        "gcloud", "compute", "instances", "describe", node,
        f"--project={args.project}", f"--zone={args.zone}",
        "--format=value(status)",
    ]


def node_is_alive(args: argparse.Namespace, node: str) -> bool:
    """Verify the node is still warm via `gcloud instances describe status`.

    Returns True ONLY when describe succeeds AND status == RUNNING. A burned /
    preempted / terminated node (describe rc != 0, or any non-RUNNING status)
    returns False -> the orchestrator starts clean. Fail-soft: a describe error
    is treated as 'not alive' (cannot confirm warm == start fresh, never resume
    into a node we cannot prove is up)."""
    rc, out = _run(_describe_status_cmd(args, node), timeout_s=60.0)
    alive = rc == 0 and (out or "").strip().upper() == "RUNNING"
    _log(f"resume-check: node {node} status={(out or '').strip() or '<none>'} -> {'ALIVE' if alive else 'not resumable'}")
    return alive


# --------------------------------------------------------------------------- #
# Phase 1: Cloud Projector (provision).
# --------------------------------------------------------------------------- #
def provision_sandbox_node(
    args: argparse.Namespace, node: str, startup_script: str,
    log_path: Optional[pathlib.Path] = None,
) -> Tuple[bool, str]:
    """Create the e2-standard-8 Spot node with the burn startup-script, STREAMING
    the `gcloud compute instances create` output line-by-line to the local
    terminal (prefixed `[provision] ...`) so the operator follows provisioning
    live instead of waiting on a silent capture.

    Returns (ok, detail). Fail-soft.
    """
    fd, sp_path = tempfile.mkstemp(prefix="sovereign_iac_startup_", suffix=".sh")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(startup_script)
        _prov_mode = (
            "ON-DEMAND/STANDARD"
            if os.environ.get("JARVIS_IAC_ON_DEMAND", "").strip().lower()
            in ("1", "true", "yes", "on")
            else "SPOT, DELETE-on-preempt"
        )
        _log(f"provisioning {node} (e2-standard-8 {_prov_mode}, 32GB)")
        rc, captured = _run_streaming_labeled(
            _create_node_cmd(args, node, sp_path),
            label="provision", log_path=log_path, timeout_s=300.0,
        )
        if rc != 0:
            return False, f"provision failed rc={rc}: {''.join(captured).strip()[:400]}"
        return True, "provisioned"
    finally:
        try:
            os.unlink(sp_path)
        except OSError:
            pass


def poll_node_ready(args: argparse.Namespace, node: str) -> Tuple[bool, str]:
    """Poll (via SSH) for the ready sentinel written once Docker is up. Bounded
    by --ready-timeout-s. Returns (ready, reason). Fail-soft."""
    deadline = time.monotonic() + float(args.ready_timeout_s)
    delay = 15.0
    attempt = 0
    check = (
        f"test -f {shlex.quote(_READY_SENTINEL)} "
        "&& docker info >/dev/null 2>&1 && echo IAC_READY || echo IAC_NOT_READY"
    )
    while time.monotonic() < deadline:
        attempt += 1
        rc, out = _run(_ssh_cmd(args, node, check), timeout_s=90.0)
        if rc == 0 and "IAC_READY" in out:
            _log(f"node ready (attempt {attempt})")
            return True, ""
        remaining = int(deadline - time.monotonic())
        _log(f"node not ready (attempt {attempt}, rc={rc}); {remaining}s left, sleeping {int(delay)}s")
        if time.monotonic() + delay >= deadline:
            break
        time.sleep(delay)
        delay = min(delay * 1.5, 90.0)
    return False, "ready_timeout"


# --------------------------------------------------------------------------- #
# Phase 2: Bidirectional Sync Bridge.
# --------------------------------------------------------------------------- #
def _resolve_repo_paths(args: argparse.Namespace) -> List[Tuple[str, str]]:
    """Resolve the 3 repo (name, local_path) pairs. jarvis = cwd repo root."""
    pairs = [
        ("jarvis", str(_REPO_ROOT)),
        ("prime", args.prime_repo_path or os.environ.get("JARVIS_PRIME_REPO_PATH", "")),
        ("reactor", args.reactor_repo_path or os.environ.get("JARVIS_REACTOR_REPO_PATH", "")),
    ]
    return pairs


def sync_repos_to_node(
    args: argparse.Namespace, node: str, excludes: List[str],
    log_path: Optional[pathlib.Path] = None,
) -> Tuple[bool, str]:
    """rsync/scp the 3 repos into /opt/trinity/{jarvis,prime,reactor}, STREAMING
    the transfer output (rsync --progress -v / scp progress) line-by-line to the
    local terminal (prefixed `[sync] ...`). Excludes .git/__pycache__/etc -- keep
    the beam lean. Bounded.

    Transport selected by JARVIS_IAC_SYNC_TRANSPORT (default scp -- robust over
    IAP). Returns (ok, detail). Fail-soft.
    """
    transport = os.environ.get("JARVIS_IAC_SYNC_TRANSPORT", "tar").strip().lower()
    pairs = _resolve_repo_paths(args)
    # GIT TRANSPORT: the node clones origin at the EXACT local HEAD over FAST WAN
    # (replacing the <1MB/s IAP tar-pipe). resolve -> clone (parity) -> (concurrent
    # deps || secret injection) -> join. Clone/parity failure = resumable keep-warm;
    # SECRET failure = burn (a node without .env is useless). Done BEFORE the
    # tar-pipe workspace-prep (the clone's `rm -rf && git clone` owns the dir).
    if transport == "git":
        # Ensure the remote root is writable by the login user (the startup-script
        # created it as ROOT). The clone then `rm -rf`s + recreates the jarvis dir.
        _root = shlex.quote(_REMOTE_TRINITY_ROOT)
        _prep = (
            f"sudo mkdir -p {_root} && "
            f'sudo chown -R "$(id -un)":"$(id -gn)" {_root} && '
            f"echo workspace_ready {_root}"
        )
        _prc, _pcap = _run_streaming_labeled(
            _ssh_cmd(args, node, _prep), label="synced", log_path=log_path,
            timeout_s=float(args.sync_timeout_s),
        )
        if _prc != 0:
            return False, f"remote workspace prep failed rc={_prc}: {''.join(_pcap).strip()[:300]}"
        return _git_transport_sync(args, node, log_path=log_path)
    # PREP: the startup-script created _REMOTE_TRINITY_ROOT as ROOT, so the scp/ssh
    # user cannot write into it ("stat remote: No such file or directory" / perm
    # denied). Create the per-repo subdirs + chown the whole tree to the login
    # user BEFORE the transfer. Reuses sudo (passwordless on the GCP image).
    _root = shlex.quote(_REMOTE_TRINITY_ROOT)
    # Create + chown the root AND the per-repo subdirs: the tar-pipe transport
    # (default) extracts the repo CONTENTS into a pre-existing subdir (no nesting).
    _subdirs = " ".join(shlex.quote(f"{_REMOTE_TRINITY_ROOT}/{n}") for n, _ in pairs)
    _prep = (
        f"sudo mkdir -p {_subdirs} && "
        f'sudo chown -R "$(id -un)":"$(id -gn)" {_root} && '
        f"echo workspace_ready {_root}"
    )
    _log(f"preparing remote workspace {_REMOTE_TRINITY_ROOT} (mkdir + chown to login user)")
    _prc, _pcap = _run_streaming_labeled(
        _ssh_cmd(args, node, _prep), label="sync", log_path=log_path,
        timeout_s=float(args.sync_timeout_s),
    )
    if _prc != 0:
        return False, f"remote workspace prep failed rc={_prc}: {''.join(_pcap).strip()[:300]}"
    for name, local in pairs:
        if not local:
            return False, f"repo path for '{name}' is unset (set JARVIS_{name.upper()}_REPO_PATH)"
        remote_dir = f"{_REMOTE_TRINITY_ROOT}/{name}"
        if transport == "tar":
            cmd = _tar_pipe_cmd(args, node, local, remote_dir, excludes)
        elif transport == "rsync":
            cmd = _rsync_cmd(args, node, local, remote_dir, excludes)
        else:
            cmd = _scp_cmd(args, node, local, remote_dir, excludes)
        _log(f"syncing {name}: {local} -> {node}:{remote_dir} (transport={transport}, streamed)")
        rc, captured = _run_streaming_labeled(
            cmd, label="sync", log_path=log_path, timeout_s=float(args.sync_timeout_s),
        )
        if rc != 0:
            return False, f"sync of '{name}' failed rc={rc}: {''.join(captured).strip()[:300]}"
    return True, "synced"


# --------------------------------------------------------------------------- #
# Phase 3: Remote Execution & Terminal Tunnelling (streamed).
# --------------------------------------------------------------------------- #
def _surgery_env_exports() -> str:
    """The trinity env exports shared by the legacy + daemon surgery shells."""
    pr = f"{_REMOTE_TRINITY_ROOT}/prime"
    rr = f"{_REMOTE_TRINITY_ROOT}/reactor"
    return (
        f"export JARVIS_PRIME_REPO_PATH={shlex.quote(pr)}; "
        f"export JARVIS_REACTOR_REPO_PATH={shlex.quote(rr)}; "
        "export JARVIS_TRINITY_PREBAKE_ENABLED=1; "
        "export JARVIS_CROSS_REPO_MUTATION_ENABLED=1; "
        "export JARVIS_CHAOS_INJECTOR_ENABLED=1; "
    )


# --------------------------------------------------------------------------- #
# HARD-ENSURE core deps -- the single source of truth (no literal duplication).
#
# These are the A1-critical packages the code-repair loop is DEAD without. The
# surgery's dep-install hard-ensures them after the requirements.txt batch; the
# golden-image baker bakes EXACTLY this set + requirements.txt so a golden node
# can SKIP the pip install entirely; and the golden deps-present probe imports a
# representative subset of these to verify the image is good. Env-overridable so
# the set is never hardcoded in two places.
# --------------------------------------------------------------------------- #
_HARD_ENSURE_DEPS: List[str] = (
    os.environ.get(
        "JARVIS_IAC_HARD_ENSURE_DEPS",
        "aiohttp httpx pydantic pytest pytest-asyncio pyyaml requests anyio "
        "sniffio fastapi uvicorn orjson uuid6",
    )
    .strip()
    .split()
)


def hard_ensure_deps() -> List[str]:
    """Return the hard-ensure core dep list (single source of truth)."""
    return list(_HARD_ENSURE_DEPS)


def _surgery_dep_install() -> str:
    """The host-python dep install (heavy pip -- the run-#15 SSH-drop locus).
    Shared verbatim by the legacy single-stream + the detached daemon paths."""
    _hard_ensure = " ".join(_HARD_ENSURE_DEPS)
    return os.environ.get(
        "JARVIS_IAC_SURGERY_DEP_INSTALL",
        "echo '[deps] installing jarvis host deps (pip from boot; ML libs filtered)'; "
        # pip is installed at BOOT (startup-script). Fallback-bootstrap if missing.
        "python3 -m pip --version >/dev/null 2>&1 "
        "|| (sudo apt-get update -y -qq && sudo apt-get install -y -q python3-pip) >/dev/null 2>&1 "
        "|| (curl -fsSL https://bootstrap.pypa.io/get-pip.py | sudo python3) >/dev/null 2>&1 || true; "
        # Filter the multi-GB ML libs + the native-build packages a BARE Debian node
        # cannot build (PyAudio needs portaudio; pyobjc is mac-only; webrtcvad/
        # sounddevice need build libs) -- the A1 code-repair loop exercises NONE.
        "grep -ivE '^(#|torch|torchaudio|torchvision|tensorflow|transformers|vllm|"
        "llama|nvidia|triton|xformers|onnx|scipy|scikit|sentencepiece|accelerate|"
        "bitsandbytes|fastembed|peft|trl|datasets|pyaudio|pyobjc|webrtcvad|"
        "sounddevice|soundfile|pyttsx3|playsound)' requirements.txt "
        # Strip INLINE comments (`pkg>=x  # note`) + trailing space so each line is a
        # CLEAN pip spec -- else the per-package loop feeds pip `pkg # note` -> parse fail.
        "| sed -E 's/[[:space:]]*#.*$//; s/[[:space:]]*$//' > /tmp/req_light.txt 2>/dev/null "
        "|| sed -E 's/[[:space:]]*#.*$//; s/[[:space:]]*$//' requirements.txt > /tmp/req_light.txt; "
        # PER-PACKAGE, continue-on-failure: a single unbuildable straggler must NOT
        # abort the batch (the atomic `-r` did exactly that on PyAudio -> killed aiohttp).
        "while IFS= read -r _pkg; do case \"$_pkg\" in ''|\\#*) continue;; esac; "
        "sudo python3 -m pip install --break-system-packages -q \"$_pkg\" 2>/dev/null "
        "|| echo \"[deps] skip unbuildable: $_pkg\"; done < /tmp/req_light.txt; "
        # HARD-ENSURE the A1-critical core -- the loop is dead without these.
        # pytest-asyncio is LOAD-BEARING: the repo's conftest has an autouse ASYNC
        # fixture -- without the plugin EVERY test ERRORS at setup -> the chaos
        # injector can never confirm a green test (the runs #4-6 wall).
        # uuid6 is UNDECLARED in requirements.txt but imported by core governance
        # (operation_id.py) -- O+V crashes at boot without it (the run #8 wall: the
        # soak never produced a debug.log, no A1Trace, because the import died).
        "sudo python3 -m pip install --break-system-packages -q " + _hard_ensure + " 2>&1 | tail -1; "
        "python3 -c 'import aiohttp; print(\"[deps] aiohttp ok\", aiohttp.__version__)' 2>&1 | tail -1",
    )


# --------------------------------------------------------------------------- #
# Golden-image-aware deps step (CONSTRAINT 2 + CONSTRAINT 3).
#
# When the soak-golden gate is OFF -> returns the legacy full-pip body verbatim
# (byte-identical). When ON, returns a node-side bash program that:
#   * probes for the pre-installed deps within the verify timeout;
#   * deps PRESENT + sha MATCHES   -> SKIP pip (logs the skip line);
#   * deps PRESENT + sha MISMATCH  -> DELTA-ENSURE (hard-ensure only the core,
#                                     never silently run stale);
#   * deps ABSENT / probe FAILS    -> INDESTRUCTIBLE FALLBACK: loud warning +
#                                     the FULL pip-install path (never blocks).
# `expected_sha` is computed LOCALLY (the repo requirements.txt) and embedded;
# the node reads the baked sha from /etc/jarvis_soak_golden_sha (stamped by the
# baker's image -- absent on a non-golden node -> treated as a mismatch).
# --------------------------------------------------------------------------- #
def _surgery_dep_step(args: argparse.Namespace) -> str:
    """Dispatch the deps step: golden-aware when enabled, else legacy (byte-id)."""
    if not _soak_golden_enabled(args):
        return _surgery_dep_install()
    return _golden_dep_install(args)


def _golden_dep_install(args: argparse.Namespace) -> str:
    """Golden-aware deps body: probe -> skip / delta-ensure / fallback-to-pip."""
    full_pip = _surgery_dep_install()  # the indestructible fallback path
    hard_ensure = " ".join(shlex.quote(d) for d in _HARD_ENSURE_DEPS)
    # Local-side expected sha of THIS checkout's requirements.txt.
    req_path = getattr(args, "requirements_path", _DEFAULT_REQUIREMENTS_PATH)
    expected_sha = requirements_sha(req_path)
    verify_to = int(getattr(args, "golden_verify_timeout_s",
                            _DEFAULT_GOLDEN_VERIFY_TIMEOUT_S))
    baked_sha_file = shlex.quote(_GOLDEN_BAKED_SHA_PATH)
    # The deps-present probe: a representative import of the hard-ensure core. We
    # wrap it in `timeout` so a hung interpreter can't exceed the verify budget ->
    # CONSTRAINT 3 fires (fall back to pip) instead of stalling the run.
    probe = (
        f"timeout {verify_to} python3 -c "
        "'import aiohttp, uuid6, fastapi, pydantic, pytest_asyncio' "
        "2>/tmp/golden_probe_err"
    )
    return (
        "echo '[deps] soak-golden enabled -- probing pre-installed deps'; "
        f"if {probe}; then "
        # Deps present. Compare the baked sha to the expected sha for staleness.
        f"_baked_sha=$(cat {baked_sha_file} 2>/dev/null || echo ''); "
        f"if [ \"$_baked_sha\" = {shlex.quote(expected_sha)} ]; then "
        "echo '[deps] golden image -- deps present, skipping install'; "
        "else "
        "echo \"[deps] golden image STALE (baked=$_baked_sha "
        f"expected={expected_sha}) -- delta-ensuring core deps\"; "
        f"sudo python3 -m pip install --break-system-packages -q {hard_ensure} 2>&1 | tail -1 || true; "
        "fi; "
        "else "
        # CONSTRAINT 3: probe failed within the verify timeout (corrupt/missing
        # image OR a deps interpreter that won't import). LOUD warning + FULL pip.
        "echo '[bootstrap] golden image unavailable/unverified -- FALLING BACK "
        "to raw Debian + full pip install'; "
        "echo \"[bootstrap] probe stderr: $(tail -3 /tmp/golden_probe_err 2>/dev/null)\"; "
        f"{full_pip}; "
        "fi"
    )


def _surgery_synchronous_tail() -> str:
    """The SYNCHRONOUS TAIL block (raw verdict + A1Trace + FAILURE LOCUS -> stdout).
    Identical in the legacy + daemon paths -- preserves the redundant telemetry
    that physically reaches the operator even if the Black-Box scp fails."""
    out_q = shlex.quote(_DEFAULT_SURGERY_OUT_PATH)
    return (
        "echo '===== SYNCHRONOUS TAIL (raw verdict + A1Trace -> stdout) ====='; "
        "echo '--- a1_verdict.json ---'; cat a1_runs/*/a1_verdict.json 2>/dev/null | tail -40 || echo '(no verdict file)'; "
        "echo '--- [A1Trace] hops (O+V debug.log) ---'; grep -hE '\\[A1Trace\\]' .ouroboros/sessions/*/debug.log 2>/dev/null | tail -20 || echo '(no A1Trace lines emitted)'; "
        f"echo '--- FAILURE LOCUS / final O+V state ---'; grep -hE 'FAILURE LOCUS|A1_DISPATCH_PROVEN|VERDICT:|state=applied|SOVEREIGN YIELD|Traceback' {out_q} 2>/dev/null | tail -15; "
        "echo '===== END SYNCHRONOUS TAIL ====='; "
    )


def _surgery_sentinel_drop(args: argparse.Namespace) -> str:
    """Verdict-conditional completion-sentinel drop (only burn on a SUCCESS
    verdict; a FAILED verdict keeps the node warm for the Black Box). Shared."""
    sentinel_q2 = shlex.quote(args.completion_sentinel)
    out_q = shlex.quote(_DEFAULT_SURGERY_OUT_PATH)
    return (
        f"if grep -qE 'A1_DISPATCH_PROVEN|VERDICT: PASS|ROLLBACK VERIFIED' {out_q}; then "
        f"sudo touch {sentinel_q2} 2>/dev/null || touch {sentinel_q2} 2>/dev/null || true; "
        "echo '[iac] verdict reached -- completion-sentinel dropped (dead-man will burn)'; "
        "else echo '[iac] no verdict (resumable) -- sentinel NOT dropped, node stays warm'; fi; "
    )


def _remote_surgery_shell(args: argparse.Namespace) -> str:
    """The LEGACY remote shell command (single long-lived streaming SSH): set the
    trinity env, install deps, run the surgery, emit the synchronous tail, then
    verdict-conditionally touch the completion-sentinel. Used when the detached
    daemon path is OFF (JARVIS_IAC_DETACHED_SURGERY_ENABLED=false) -- byte-identical."""
    jr = f"{_REMOTE_TRINITY_ROOT}/jarvis"
    env = _surgery_env_exports()
    dep_install = _surgery_dep_step(args)
    out_q = shlex.quote(_DEFAULT_SURGERY_OUT_PATH)
    # cd into the synced jarvis repo, install deps, run the surgery. The completion
    # sentinel (which arms the node-side dead-man to burn IMMEDIATELY) is dropped
    # ONLY when the surgery reached a real VERDICT (PASS/FRACTURE) -- NOT on a
    # resumable failure (e.g. a missing dep), so keep-warm actually keeps the node
    # alive for a resume instead of the dead-man burning it out from under us.
    return (
        f"cd {shlex.quote(jr)} && {env} "
        f"{dep_install}; "
        f"({args.surgery_cmd}) 2>&1 | tee {out_q}; rc=${{PIPESTATUS[0]}}; "
        # ---- SYNCHRONOUS TAIL (redundant telemetry) runs on EVERY exit.
        + _surgery_synchronous_tail()
        + _surgery_sentinel_drop(args)
        + "exit $rc"
    )


# --------------------------------------------------------------------------- #
# Detached daemon surgery shell -- the PRIMARY path. The surgery body runs the
# SAME deps/surgery/tail/sentinel work as the legacy path, but is wrapped so it:
#   * writes structured JSON to soak_state.json at each phase boundary (deps ->
#     inject/surgery -> done|failed) via an ATOMIC temp+mv;
#   * maintains soak_in_progress.lock (the PID), REMOVED in a trap on ANY exit;
#   * runs the chaos-revert-ALWAYS trap (folded into the surgery_cmd itself).
# This script is what setsid/nohup launches detached -- it survives the launching
# SSH closing. The local harness polls soak_state.json + tails surgery.out.
# --------------------------------------------------------------------------- #
def _fault_tolerant_obs_enabled(args: argparse.Namespace) -> bool:
    """Resolve the fault-tolerant-observability master gate from args (if present)
    else env. Default-OFF -> the legacy byte-offset/dumb-wall behavior (byte-
    identical). The omni-soak arms it via JARVIS_IAC_FAULT_TOLERANT_OBS_ENABLED."""
    val = getattr(args, "fault_tolerant_obs", None)
    if val is not None:
        return bool(val)
    return _env_truthy_off("JARVIS_IAC_FAULT_TOLERANT_OBS_ENABLED")


def _heartbeat_block(args: argparse.Namespace) -> str:
    """Render the node-side anti-starvation HEARTBEAT (CONSTRAINT 1): a setsid
    background loop that ATOMICALLY (temp+mv) ticks soak_state.json.last_active +
    step_seq every ~interval seconds -- even during a long quiet step (deps).

    Launched at ELEVATED OS priority (`nice -n -20 ionice -c1 -n0`, realtime IO)
    so a swarm redlining CPU/IO at 100%% can NEVER starve/OOM-kill it -> no false
    heartbeat freeze. `ionice -c1` falls back to `-c2 -n0` (best-effort highest)
    at RUNTIME if -c1 is denied (it needs root/CAP_SYS_ADMIN). The heartbeat reads
    the current phase from the marker file the writer drops, so its ticks carry
    the live phase. Its PID is exported (_HB_PID) so the EXIT trap can kill it."""
    state_q = shlex.quote(_DEFAULT_SOAK_STATE_PATH)
    phase_marker_q = f"{state_q}.phase"
    interval = float(getattr(args, "heartbeat_interval_s", _DEFAULT_HEARTBEAT_INTERVAL_S))
    nice = str(getattr(args, "heartbeat_nice", _DEFAULT_HEARTBEAT_NICE))
    ioc = str(getattr(args, "heartbeat_ionice_class", _DEFAULT_HEARTBEAT_IONICE_CLASS))
    iop = str(getattr(args, "heartbeat_ionice_prio", _DEFAULT_HEARTBEAT_IONICE_PRIO))
    # The inner loop body (a self-contained bash -c program string): read phase
    # marker, atomically rewrite soak_state.json with a fresh last_active +
    # monotonically-incrementing step_seq. Pure printf JSON. It NEVER parses the
    # existing JSON (avoids half-read races) -- it re-emits the known fields with
    # the heartbeat's own advancing counters. Passed as a single bash -c arg so it
    # runs in the freshly-priority-elevated child (no exported-function reliance).
    loop_body = (
        "_seq=0; "
        "while true; do "
        f"_ph=$(cat {phase_marker_q} 2>/dev/null || echo running); "
        "_seq=$((_seq+1)); "
        f"_tmp={state_q}.hb.$$; "
        "printf '{\"phase\":\"%s\",\"status\":\"running\",\"rc\":null,\"ts\":%s,"
        "\"verdict\":\"running\",\"last_active\":%s,\"step_seq\":%s}\\n' "
        "\"$_ph\" \"$(date +%s)\" \"$(date +%s)\" \"$_seq\" "
        f"> \"$_tmp\" 2>/dev/null && mv -f \"$_tmp\" {state_q} 2>/dev/null || true; "
        f"sleep {interval}; "
        "done"
    )
    loop_q = shlex.quote(loop_body)
    nice_q = shlex.quote(nice)
    ioc_q = shlex.quote(ioc)
    iop_q = shlex.quote(iop)
    # Launch at REALTIME io class -c1 (preferred); on EPERM fall back to the
    # best-effort highest -c2 -n0. setsid detaches it from the surgery TTY. The
    # priority prefix is applied to the launched child; the loop body is a single
    # `bash -c '<program>'` arg so it runs intact in the elevated child.
    return (
        "# ---- CONSTRAINT 1: anti-starvation heartbeat (elevated nice/ionice) ----\n"
        f"if ionice -c{ioc_q} -n{iop_q} true 2>/dev/null; then "
        f"setsid nice -n {nice_q} ionice -c{ioc_q} -n{iop_q} "
        f"bash -c {loop_q} </dev/null >/dev/null 2>&1 & _HB_PID=$!; "
        "else "
        f"setsid nice -n {nice_q} ionice -c2 -n0 "
        f"bash -c {loop_q} </dev/null >/dev/null 2>&1 & _HB_PID=$!; "
        "fi; "
        "export _HB_PID; "
        "echo \"[iac] heartbeat launched pid=$_HB_PID "
        f"(nice {nice} ionice -c{ioc} -n{iop} realtime, fallback -c2 -n0)\";\n"
    )


def _remote_surgery_body_script(args: argparse.Namespace) -> str:
    """Render the node-side detached `surgery.sh` body (a self-contained bash
    program). Writes soak_state.json at phase boundaries + a PID lock removed in a
    trap. ASCII only, no f-string brace conflicts in the JSON (built via printf)."""
    jr = f"{_REMOTE_TRINITY_ROOT}/jarvis"
    env = _surgery_env_exports()
    dep_install = _surgery_dep_step(args)
    state_q = shlex.quote(_DEFAULT_SOAK_STATE_PATH)
    lock_q = shlex.quote(_DEFAULT_SOAK_LOCK_PATH)
    out_q = shlex.quote(_DEFAULT_SURGERY_OUT_PATH)
    ft_obs = _fault_tolerant_obs_enabled(args)
    # State writer: atomic temp+mv. Args: phase status rc verdict. Pure printf JSON.
    # We keep it ASCII and brace-safe by assembling the JSON with printf %s.
    #
    # FAULT-TOLERANT OBS (CONSTRAINT 1): when armed, the writer ALSO records
    # last_active (epoch) + step_seq, and the CURRENT phase into a small marker
    # file (_PHASE) the independent heartbeat reads -- so even a long-quiet step
    # (deps) keeps last_active ADVANCING. Liveness decouples from the log stream.
    if ft_obs:
        phase_marker_q = f"{state_q}.phase"
        writer = (
            "_write_state() { "
            "_p=\"$1\"; _s=\"$2\"; _rc=\"$3\"; _v=\"$4\"; "
            f"_tmp={state_q}.tmp.$$; "
            f"printf '%s' \"$_p\" > {phase_marker_q} 2>/dev/null || true; "
            "printf '{\"phase\":\"%s\",\"status\":\"%s\",\"rc\":%s,\"ts\":%s,"
            "\"verdict\":\"%s\",\"last_active\":%s,\"step_seq\":%s}\\n' "
            "\"$_p\" \"$_s\" \"${_rc:-null}\" \"$(date +%s)\" \"$_v\" "
            "\"$(date +%s)\" \"${_HB_SEQ:-0}\" > \"$_tmp\" 2>/dev/null "
            f"&& mv -f \"$_tmp\" {state_q} 2>/dev/null || true; }}; "
        )
    else:
        writer = (
            "_write_state() { "
            "_p=\"$1\"; _s=\"$2\"; _rc=\"$3\"; _v=\"$4\"; "
            f"_tmp={state_q}.tmp.$$; "
            "printf '{\"phase\":\"%s\",\"status\":\"%s\",\"rc\":%s,\"ts\":%s,\"verdict\":\"%s\"}\\n' "
            "\"$_p\" \"$_s\" \"${_rc:-null}\" \"$(date +%s)\" \"$_v\" > \"$_tmp\" 2>/dev/null "
            f"&& mv -f \"$_tmp\" {state_q} 2>/dev/null || true; }}; "
        )
    # Lock: write our PID; trap removes it on EXIT (success OR failure). The
    # surgery_cmd carries its OWN chaos-revert-ALWAYS trap; this lock-clear trap is
    # additive (bash runs all EXIT traps -- we chain via a single handler).
    # When the heartbeat is armed the trap ALSO kills the heartbeat PID.
    if ft_obs:
        lock_trap = (
            f"echo \"$$\" > {lock_q} 2>/dev/null || true; "
            f"_cleanup() {{ rm -f {lock_q} 2>/dev/null || true; "
            "[ -n \"${_HB_PID:-}\" ] && kill \"$_HB_PID\" 2>/dev/null || true; }; "
            "trap _cleanup EXIT INT TERM; "
        )
    else:
        lock_trap = (
            f"echo \"$$\" > {lock_q} 2>/dev/null || true; "
            f"_cleanup() {{ rm -f {lock_q} 2>/dev/null || true; }}; "
            "trap _cleanup EXIT INT TERM; "
        )
    parts: List[str] = [
        "#!/usr/bin/env bash\n",
        "set -uo pipefail\n",
        "export HOME=${HOME:-/root}\n",
        f"cd {shlex.quote(jr)} || exit 91\n",
        f"{env}\n",
        f"{writer}\n",
        f"{lock_trap}\n",
    ]
    # ---- CONSTRAINT 1: anti-starvation HEARTBEAT (elevated OS priority) ----- #
    # Launched BEFORE deps so a long-quiet pip install keeps last_active advancing.
    if ft_obs:
        parts.append(_heartbeat_block(args))
    parts += [
        # ---- PHASE: deps (the run-#15 SSH-drop locus -- now detached) ------ #
        "_write_state deps running null running\n",
        f"{dep_install}\n",
        # ---- PHASE: surgery (inject -> soak -> audit folded in surgery_cmd) #
        "_write_state inject running null running\n",
        f"({args.surgery_cmd}) 2>&1 | tee {out_q}; rc=${{PIPESTATUS[0]}}\n",
        "_write_state soak done \"$rc\" running\n",
        # ---- SYNCHRONOUS TAIL (redundant telemetry) ----------------------- #
        _surgery_synchronous_tail(),
        "\n",
        # ---- PHASE: audit -> sentinel ------------------------------------- #
        "_write_state audit running \"$rc\" running\n",
        _surgery_sentinel_drop(args),
        # Terminal state: failed iff a non-zero rc, else done. Verdict parsed from
        # the surgery.out markers so the local poll can read it from state.
        f"if grep -qE 'SOVEREIGN YIELD: CROSS-REPO FRACTURE' {out_q} 2>/dev/null; then _verd=FRACTURE; ",
        f"elif grep -qE 'VERDICT: PASS|A1_DISPATCH_PROVEN|ROLLBACK VERIFIED' {out_q} 2>/dev/null; then _verd=PASS; ",
        "else _verd=UNKNOWN; fi\n",
        "if [ \"${rc:-1}\" -eq 0 ]; then _write_state done done \"$rc\" \"$_verd\"; ",
        "else _write_state failed failed \"$rc\" \"$_verd\"; fi\n",
        "exit ${rc:-1}\n",
    ]
    return "".join(parts)


def _remote_surgery_launch_shell(args: argparse.Namespace) -> str:
    """The SHORT remote shell the launching SSH runs: write surgery.sh to the node,
    then LAUNCH it DETACHED via setsid+nohup and RETURN IMMEDIATELY. The detached
    surgery survives this SSH closing. setsid+nohup is auth-free (no systemd/polkit
    dependency) -- `systemd-run --scope` was removed because it requires interactive
    polkit auth over a non-interactive SSH --command and silently no-ops (run #16)."""
    jr = f"{_REMOTE_TRINITY_ROOT}/jarvis"
    script_path = f"{jr}/surgery.sh"
    script_q = shlex.quote(script_path)
    out_q = shlex.quote(_DEFAULT_SURGERY_OUT_PATH)
    body = _remote_surgery_body_script(args)
    # Heredoc-write the body (quoted EOF -> no expansion on the node), chmod, then
    # detach. The launcher itself returns the moment the background fork is spawned.
    b64 = _b64(body)
    return (
        f"mkdir -p {shlex.quote(jr)} 2>/dev/null || true; "
        # decode the base64 body to surgery.sh (robust against quoting/heredoc edge
        # cases over the SSH --command boundary).
        f"echo {shlex.quote(b64)} | base64 -d > {script_q} && chmod +x {script_q}; "
        # truncate the prior out (idempotent relaunch starts a fresh log).
        f": > {out_q} 2>/dev/null || true; "
        # DETACH: setsid+nohup is PRIMARY -- it is auth-free and fully detaches from
        # the SSH TTY (new session + SIGHUP-immune + stdin from /dev/null). We do NOT
        # use `systemd-run --scope`: in a non-interactive SSH --command it fails with
        # "Interactive authentication required" (polkit) and backgrounds nothing, yet
        # the launcher's echo still reports success -> the surgery silently never runs
        # (the run-#16 failure). setsid nohup has no such dependency.
        "if command -v setsid >/dev/null 2>&1; then "
        f"setsid nohup bash {script_q} >{out_q} 2>&1 </dev/null & "
        "echo '[iac] surgery launched detached (setsid nohup)'; "
        "else "
        f"nohup bash {script_q} >{out_q} 2>&1 </dev/null & disown 2>/dev/null || true; "
        "echo '[iac] surgery launched detached (nohup+disown)'; "
        "fi; "
        # return immediately -- do NOT wait on the surgery body.
        "echo '[iac] launcher returning (surgery detached, SSH may now close)'; "
        "exit 0"
    )


def _b64(s: str) -> str:
    """base64-encode a string for safe transport over the SSH --command boundary."""
    import base64

    return base64.b64encode(s.encode("utf-8")).decode("ascii")


def parse_verdict(captured: List[str]) -> str:
    """Decide PASS / FRACTURE / UNKNOWN from the captured surgery output."""
    blob = "".join(captured)
    if _VERDICT_FRACTURE in blob:
        return "FRACTURE"
    if _VERDICT_PASS in blob:
        return "PASS"
    return "UNKNOWN"


def _remote_prebake_shell(args: argparse.Namespace) -> str:
    """The remote prebake shell: cd into the synced jarvis repo, set the trinity
    env, run the WAN prebake command (the `docker build` layer pull). Does NOT
    touch the completion-sentinel -- prebake is a mid-pipeline step the resume
    can re-run; only the surgery's terminal verdict drops the sentinel."""
    jr = f"{_REMOTE_TRINITY_ROOT}/jarvis"
    pr = f"{_REMOTE_TRINITY_ROOT}/prime"
    rr = f"{_REMOTE_TRINITY_ROOT}/reactor"
    env = (
        f"export JARVIS_PRIME_REPO_PATH={shlex.quote(pr)}; "
        f"export JARVIS_REACTOR_REPO_PATH={shlex.quote(rr)}; "
        "export JARVIS_TRINITY_PREBAKE_ENABLED=1; "
    )
    return f"cd {shlex.quote(jr)} && {env} {args.prebake_cmd}"


def run_remote_prebake(
    args: argparse.Namespace, node: str, log_path: Optional[pathlib.Path] = None,
) -> Tuple[bool, str]:
    """SSH-exec the WAN prebake (remote `docker build` layer build) remotely,
    STREAMING the layer-build output line-by-line to the local terminal
    (prefixed `[prebake] ...`). This is the first-run-friction step (PyPI
    timeout); checkpointing it as its own phase lets a resume skip a completed
    sync and re-run ONLY the prebake. Returns (ok, detail). Fail-soft.

    When JARVIS_IAC_PREBAKE_CMD is empty the prebake is folded into the surgery
    command (legacy single-exec) -- this returns ok with a 'skipped' detail."""
    if not (args.prebake_cmd or "").strip():
        return True, "prebake folded into surgery (no JARVIS_IAC_PREBAKE_CMD)"
    remote = _remote_prebake_shell(args)
    cmd = _ssh_cmd(args, node, remote)
    _log("running remote prebake (WAN docker build, streaming to local terminal)...")
    rc, captured = _run_streaming_labeled(
        cmd, label="prebake", log_path=log_path, timeout_s=float(args.prebake_timeout_s),
    )
    if rc != 0:
        return False, f"prebake failed rc={rc}: {''.join(captured).strip()[:400]}"
    return True, "prebaked"


def _remote_boot_shell(args: argparse.Namespace) -> str:
    """The remote boot shell: cd into the synced jarvis repo, set the trinity env,
    bring up the air-gapped compose (the `booted` phase). No completion-sentinel
    -- boot is a mid-pipeline step a resume can re-run."""
    jr = f"{_REMOTE_TRINITY_ROOT}/jarvis"
    pr = f"{_REMOTE_TRINITY_ROOT}/prime"
    rr = f"{_REMOTE_TRINITY_ROOT}/reactor"
    env = (
        f"export JARVIS_PRIME_REPO_PATH={shlex.quote(pr)}; "
        f"export JARVIS_REACTOR_REPO_PATH={shlex.quote(rr)}; "
    )
    return f"cd {shlex.quote(jr)} && {env} {args.boot_cmd}"


def run_remote_boot(
    args: argparse.Namespace, node: str, log_path: Optional[pathlib.Path] = None,
) -> Tuple[bool, str]:
    """SSH-exec the air-gapped compose boot remotely, STREAMING line-by-line
    (prefixed `[boot] ...`). Checkpointed as the `booted` phase. Returns
    (ok, detail). Fail-soft. Empty JARVIS_IAC_BOOT_CMD == folded into surgery."""
    if not (args.boot_cmd or "").strip():
        return True, "boot folded into surgery (no JARVIS_IAC_BOOT_CMD)"
    remote = _remote_boot_shell(args)
    cmd = _ssh_cmd(args, node, remote)
    _log("running remote boot (air-gap compose, streaming to local terminal)...")
    rc, captured = _run_streaming_labeled(
        cmd, label="boot", log_path=log_path, timeout_s=float(args.boot_timeout_s),
    )
    if rc != 0:
        return False, f"boot failed rc={rc}: {''.join(captured).strip()[:400]}"
    return True, "booted"


def run_remote_surgery(
    args: argparse.Namespace, node: str, log_path: Optional[pathlib.Path] = None,
) -> Tuple[int, List[str], str]:
    """Run the Trinity surgery remotely. Returns (rc, captured_lines, verdict).
    Fail-soft.

    PRIMARY (JARVIS_IAC_DETACHED_SURGERY_ENABLED, default true): launch the surgery
    DETACHED (setsid/nohup/systemd-run) over a SHORT SSH that returns immediately,
    then POLL the node (exp-backoff + jitter, short disposable probes) for
    soak_state.json + soak_in_progress.lock and TAIL surgery.out by byte-offset --
    a dropped SSH during pip install is a NON-EVENT (the run-#15 rc=255 fix).

    LEGACY (master OFF): the byte-identical single long-lived streaming SSH session.
    """
    if not _detached_surgery_enabled(args):
        # ---- LEGACY single-stream path (byte-identical) -------------------- #
        remote = _remote_surgery_shell(args)
        cmd = _ssh_cmd(args, node, remote)
        _log("running remote surgery (streaming to local terminal in real-time)...")
        rc, captured = _run_streaming_labeled(
            cmd, label="surgery", log_path=log_path,
            timeout_s=float(args.surgery_timeout_s),
        )
        verdict = parse_verdict(captured)
        _log(f"remote surgery finished rc={rc} verdict={verdict}")
        return rc, captured, verdict
    # ---- PRIMARY detached daemon + poll/reconnect path --------------------- #
    return run_remote_surgery_detached(args, node, log_path=log_path)


def _detached_surgery_enabled(args: argparse.Namespace) -> bool:
    """Resolve the detached-surgery master gate from args (if present) else env.
    Default-ON. OFF -> legacy single-stream path."""
    val = getattr(args, "detached_surgery", None)
    if val is not None:
        return bool(val)
    return _env_truthy("JARVIS_IAC_DETACHED_SURGERY_ENABLED", "true")


def launch_detached_surgery(
    args: argparse.Namespace, node: str, log_path: Optional[pathlib.Path] = None,
) -> Tuple[int, List[str]]:
    """Run the SHORT launching SSH that writes surgery.sh + spawns it DETACHED and
    RETURNS IMMEDIATELY. Uses the non-streaming `_run` boundary (a short call --
    NOT a long stream). Fail-soft: a launch failure returns its rc for the caller."""
    remote = _remote_surgery_launch_shell(args)
    cmd = _ssh_cmd(args, node, remote)
    sink = _make_labeled_sink("launch", log_path)
    _log("launching detached surgery (short SSH, returns immediately)...")
    rc, out = _run(cmd, timeout_s=float(args.probe_timeout_s))
    for ln in (out or "").splitlines():
        sink(ln + "\n")
    return rc, [out or ""]


def run_remote_surgery_detached(
    args: argparse.Namespace, node: str, log_path: Optional[pathlib.Path] = None,
) -> Tuple[int, List[str], str]:
    """Launch the detached surgery, then drive the async poll/reconnect loop +
    byte-offset tailer to terminal. Returns (rc, captured_lines, verdict).

    Async-safe entry: runs the asyncio loop via asyncio.run (Python 3.9+ -- no
    asyncio.timeout; bounded with asyncio.wait_for inside)."""
    # Launch detached first (short SSH). A launch failure is itself resumable --
    # the poll loop will simply find no state and trip the liveness deadline.
    lrc, lcap = launch_detached_surgery(args, node, log_path=log_path)
    if lrc != 0:
        _log(f"detached launch rc={lrc} (poll loop will still probe -- node may be live)")
    try:
        return asyncio.run(_poll_surgery_to_terminal(args, node, log_path=log_path))
    except Exception as exc:  # noqa: BLE001 -- the poll loop must never crash the run
        _log(f"detached poll loop raised (swallowed): {exc!r}")
        return 1, [f"[surgery] poll loop error: {exc!r}\n"], "UNKNOWN"


# --------------------------------------------------------------------------- #
# Async helpers -- run a (blocking) short SSH probe off the event loop.
# --------------------------------------------------------------------------- #
async def _probe(cmd: List[str], *, timeout_s: float) -> Tuple[int, str]:
    """Run a short disposable SSH probe off-thread, bounded by asyncio.wait_for.
    NEVER raises -- a broken pipe / 255 / timeout surfaces as (rc!=0, detail) which
    the caller SWALLOWS + retries. This is the whole point: a dropped probe is a
    NON-EVENT."""
    try:
        loop = asyncio.get_event_loop()
        # _run is already fail-soft (returns (1, detail) on any exception). Bound
        # it again here so a wedged SSH can't hang the tick.
        return await asyncio.wait_for(
            loop.run_in_executor(None, lambda: _run(cmd, timeout_s=timeout_s)),
            timeout=timeout_s + 5.0,
        )
    except Exception as exc:  # noqa: BLE001 -- swallow EVERYTHING; the tick retries
        return 1, f"[probe failed: {exc!r}]"


def _is_transport_drop(rc: int, detail: str) -> bool:
    """True iff the probe failure looks like a transport drop (the run-#15 class):
    broken pipe / connection closed / rc=255 / timeout. Such failures are SWALLOWED
    + retried; they count only against the generous liveness deadline."""
    if rc == 255:
        return True
    low = (detail or "").lower()
    return any(
        m in low
        for m in (
            "broken pipe", "connection closed", "connection reset",
            "connection refused", "timed out", "timeout", "rc=255",
            "failed to send all data", "remote host", "probe failed",
        )
    )


def _read_state_cmd(args: argparse.Namespace, node: str) -> List[str]:
    """A SHORT SSH that cats soak_state.json (small JSON)."""
    state_q = shlex.quote(_DEFAULT_SOAK_STATE_PATH)
    remote = f"cat {state_q} 2>/dev/null || true"
    return _ssh_cmd(args, node, remote)


def _liveness_probe_cmd(args: argparse.Namespace, node: str) -> List[str]:
    """A SHORT SSH that checks soak_in_progress.lock + its PID liveness. Prints
    `ALIVE <pid>` if the lock exists AND the PID is running, `GONE` otherwise."""
    lock_q = shlex.quote(_DEFAULT_SOAK_LOCK_PATH)
    remote = (
        f"if [ -f {lock_q} ]; then _p=$(cat {lock_q} 2>/dev/null); "
        "if [ -n \"$_p\" ] && kill -0 \"$_p\" 2>/dev/null; then echo \"ALIVE $_p\"; "
        "else echo \"STALE $_p\"; fi; else echo GONE; fi"
    )
    return _ssh_cmd(args, node, remote)


def _tail_cmd(args: argparse.Namespace, node: str, offset: int) -> List[str]:
    """A SHORT SSH that fetches ONLY the bytes of surgery.out at >= offset+1
    (`tail -c +N` is 1-indexed -> +offset+1 == bytes after `offset`). Zero-loss /
    zero-dup byte-offset tailing: the local side advances `offset` by what arrived."""
    out_q = shlex.quote(_DEFAULT_SURGERY_OUT_PATH)
    start = max(0, int(offset)) + 1
    remote = f"tail -c +{start} {out_q} 2>/dev/null || true"
    return _ssh_cmd(args, node, remote)


def _parse_state_blob(blob: str) -> Dict[str, Any]:
    """Parse soak_state.json text -> dict (fail-soft -> {} on garbage/partial)."""
    try:
        blob = (blob or "").strip()
        if not blob:
            return {}
        data = json.loads(blob)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 -- a partial/garbage read is just "no state yet"
        return {}


def _backoff_delay(attempt: int, *, base: float, cap: float, jitter: float) -> float:
    """Exponential backoff base*2**attempt capped at `cap`, plus uniform jitter."""
    grow = base * (2 ** max(0, attempt))
    return min(grow, cap) + random.uniform(0.0, max(0.0, jitter))


async def _tail_once(
    args: argparse.Namespace, node: str, offset: int, sink: Callable[[str], None],
) -> int:
    """One byte-offset tail tick: fetch bytes after `offset`, stream them, return
    the NEW offset (advanced by bytes received). Fail-soft: a failed tail leaves
    the offset UNCHANGED (next tick retries -> zero loss, zero dup)."""
    rc, out = await _probe(_tail_cmd(args, node, offset), timeout_s=float(args.probe_timeout_s))
    if rc != 0:
        return offset  # transport drop -> retry next tick, offset unchanged
    if not out:
        return offset
    # advance by the byte length actually received (utf-8) -- this is the seam that
    # guarantees no-dup on reconnect: we only ever ask for bytes AFTER `offset`.
    for ln in out.splitlines(keepends=True):
        sink(ln if ln.endswith("\n") else ln + "\n")
    return offset + len(out.encode("utf-8"))


# --------------------------------------------------------------------------- #
# CONSTRAINT 2: line-safe SIZE-AWARE delta sync (replaces the byte-offset tail).
#
# `stat` the remote surgery.out size, pull the byte-range [last_synced_size,
# current_size]. Commit ONLY up to the LAST COMPLETE newline in the pulled delta;
# buffer the trailing partial line locally and prepend it to the next sync. On an
# SSH drop -> resume from last_synced_size (zero missed lines, never a half line /
# mid-utf-8 char / half-JSON). Reuses the short-SSH probe boundary -- NO new
# transport. The delta tracker carries (last_synced_size, partial-byte-buffer).
# --------------------------------------------------------------------------- #
class _DeltaSyncState:
    """Mutable delta-sync cursor: the last committed remote byte size + the
    trailing partial-line bytes buffered locally (prepended to the next pull)."""

    __slots__ = ("last_synced_size", "partial")

    def __init__(self) -> None:
        self.last_synced_size: int = 0
        self.partial: bytes = b""


def _stat_size_cmd(args: argparse.Namespace, node: str) -> List[str]:
    """A SHORT SSH that prints the byte size of surgery.out (`stat -c %s`, GNU;
    BSD `stat -f %z` fallback chained). 0 when the file is absent."""
    out_q = shlex.quote(_DEFAULT_SURGERY_OUT_PATH)
    remote = (
        f"stat -c %s {out_q} 2>/dev/null "
        f"|| stat -f %z {out_q} 2>/dev/null "
        f"|| echo 0"
    )
    return _ssh_cmd(args, node, remote)


def _delta_range_cmd(args: argparse.Namespace, node: str, start: int, length: int) -> List[str]:
    """A SHORT SSH that pulls the byte-range [start, start+length) of surgery.out:
    `tail -c +<start+1>` (1-indexed) piped into `head -c <length>`. Pulling a
    bounded range (not the open tail) keeps each delta sync size-bounded + lets the
    line-safe buffer reason about an exact window."""
    out_q = shlex.quote(_DEFAULT_SURGERY_OUT_PATH)
    s1 = max(0, int(start)) + 1
    n = max(0, int(length))
    remote = f"tail -c +{s1} {out_q} 2>/dev/null | head -c {n} 2>/dev/null || true"
    return _ssh_cmd(args, node, remote)


def _split_line_safe(buf: bytes) -> Tuple[bytes, bytes]:
    """Split *buf* at the LAST complete newline: returns (committable, trailing
    partial). The committable bytes end on a `\\n`; the trailing partial (an
    incomplete line, possibly mid-utf-8) is buffered for the next sync. If there
    is no newline yet, NOTHING is committable -- the whole buffer is held."""
    idx = buf.rfind(b"\n")
    if idx < 0:
        return b"", buf
    return buf[: idx + 1], buf[idx + 1:]


async def _delta_sync_once(
    args: argparse.Namespace, node: str, state: "_DeltaSyncState",
    sink: Callable[[str], None],
) -> bool:
    """One line-safe delta-sync tick. Returns True iff the transport tick was OK
    (a drop -> False so the caller counts it against liveness). Fail-soft: a failed
    stat/pull leaves last_synced_size + the partial buffer UNCHANGED (resume on the
    next tick from exactly where we left off -> zero missed lines, no half line)."""
    # 1. stat the remote size.
    src, sout = await _probe(_stat_size_cmd(args, node), timeout_s=float(args.probe_timeout_s))
    if src != 0 and _is_transport_drop(src, sout):
        return False  # transport drop -> resume next tick, cursor unchanged
    try:
        current = int((sout or "0").strip().split()[0])
    except (ValueError, IndexError):
        current = state.last_synced_size  # garbage stat -> treat as no growth
    if current < state.last_synced_size:
        # surgery.out was truncated/rotated (idempotent relaunch `: > out`) -> reset.
        state.last_synced_size = 0
        state.partial = b""
    if current <= state.last_synced_size:
        return True  # no new bytes (a quiet step) -- NOT a drop
    # 2. pull ONLY the new byte-range [last_synced_size, current).
    length = current - state.last_synced_size
    drc, dout = await _probe(
        _delta_range_cmd(args, node, state.last_synced_size, length),
        timeout_s=float(args.probe_timeout_s),
    )
    if drc != 0 and _is_transport_drop(drc, dout):
        return False  # drop mid-pull -> cursor unchanged, retry the SAME range
    delta = (dout or "").encode("utf-8")
    if not delta:
        return True
    # 3. LINE-SAFE commit: prepend the buffered partial, split at the last newline,
    #    emit only complete lines, re-buffer the trailing partial.
    combined = state.partial + delta
    committable, state.partial = _split_line_safe(combined)
    # advance the cursor by the RAW delta bytes actually pulled (the partial buffer
    # carries the un-emitted remainder across ticks -- never re-pulled).
    state.last_synced_size += len(delta)
    if committable:
        text = committable.decode("utf-8", errors="replace")
        for ln in text.splitlines(keepends=True):
            sink(ln)
    return True


def _delta_flush_partial(state: "_DeltaSyncState", sink: Callable[[str], None]) -> None:
    """Flush any buffered trailing partial at terminal (the surgery wrote a final
    line without a newline before exit). Emits it once, line-terminated."""
    if state.partial:
        text = state.partial.decode("utf-8", errors="replace")
        sink(text if text.endswith("\n") else text + "\n")
        state.partial = b""


async def _poll_surgery_to_terminal(
    args: argparse.Namespace, node: str, log_path: Optional[pathlib.Path] = None,
) -> Tuple[int, List[str], str]:
    """The idempotent poll/reconnect loop. Each tick (exp-backoff + jitter):
        (a) tail surgery.out by byte-offset -> stream new bytes locally (no dup/loss);
        (b) read soak_state.json -> terminal on status in (done, failed);
        (c) check soak_in_progress.lock liveness -> GONE+terminal-state == done.
    TERMINATES on: state done/failed; OR lock gone AND state done/failed; OR the
    absolute wall ceiling (reap); OR N consecutive failed probes beyond the liveness
    deadline (node genuinely unreachable -> reap). A broken-pipe probe is SWALLOWED."""
    sink = _make_labeled_sink("surgery", log_path)
    captured: List[str] = []

    def _emit(line: str) -> None:
        captured.append(line)
        sink(line)

    base = float(getattr(args, "poll_base_s", _DEFAULT_POLL_BASE_S))
    cap = float(getattr(args, "poll_cap_s", _DEFAULT_POLL_CAP_S))
    jitter = float(getattr(args, "poll_jitter_s", _DEFAULT_POLL_JITTER_S))
    liveness_deadline = float(getattr(args, "liveness_deadline_s", _DEFAULT_LIVENESS_DEADLINE_S))
    max_wall = float(getattr(args, "max_wall_seconds", _DEFAULT_MAX_WALL_S) or 0.0)
    if max_wall <= 0.0:
        max_wall = float(args.surgery_timeout_s)

    # --- FAULT-TOLERANT OBS (gated): delta-sync + last_active liveness + dual --
    #     boundary phase-adaptive wall. OFF -> the legacy byte-offset/dumb-wall.
    ft_obs = _fault_tolerant_obs_enabled(args)
    global_ceiling = float(getattr(args, "global_ceiling_s", _DEFAULT_GLOBAL_CEILING_S) or 0.0)
    if global_ceiling <= 0.0:
        global_ceiling = max_wall
    stale_s = float(getattr(args, "heartbeat_stale_s", _DEFAULT_HEARTBEAT_STALE_S))
    delta = _DeltaSyncState()
    last_seen_active: Optional[int] = None
    last_active_change_mono = time.monotonic()

    offset = 0
    attempt = 0
    consecutive_fail = 0
    first_fail_mono: Optional[float] = None
    start_mono = time.monotonic()
    verdict = "UNKNOWN"
    rc = 1

    if ft_obs:
        _emit("[iac] fault-tolerant poll/reconnect loop engaged "
              f"(delta-sync + last_active liveness + dual-boundary wall; "
              f"global_ceiling={global_ceiling}s stale={stale_s}s)\n")
    else:
        _emit("[iac] detached poll/reconnect loop engaged "
              f"(base={base}s cap={cap}s liveness={liveness_deadline}s wall={max_wall}s)\n")

    while True:
        now = time.monotonic()
        # --- absolute wall ceiling (hard stop -> reap) --------------------- #
        # Legacy: a single dumb wall. FT-obs (CONSTRAINT 4): the GLOBAL hard
        # ceiling is the non-negotiable backstop; the per-phase dual-boundary
        # decision (below, after we know the phase) handles extend-vs-reap.
        if not ft_obs and now - start_mono > max_wall:
            _emit(f"[iac] absolute wall ceiling {max_wall}s exceeded -- reaping\n")
            rc, verdict = 124, parse_verdict(captured)
            break
        if ft_obs and now - start_mono > global_ceiling:
            _emit(f"[iac] GLOBAL hard ceiling {global_ceiling}s exceeded -- reaping "
                  "(dual-boundary backstop)\n")
            rc, verdict = 124, parse_verdict(captured)
            break

        tick_ok = True

        # --- (a) log sync: delta-sync (ft) OR byte-offset tail (legacy) ---- #
        if ft_obs:
            sync_ok = await _delta_sync_once(args, node, delta, _emit)
            if not sync_ok:
                tick_ok = False
        else:
            new_offset = await _tail_once(args, node, offset, _emit)
            if new_offset == offset:
                # no new bytes -- a transport drop OR genuinely no output yet.
                pass
            offset = new_offset

        # --- (b) read structured state ------------------------------------ #
        sr: int
        sout: str
        src_rc, sout = await _probe(
            _read_state_cmd(args, node), timeout_s=float(args.probe_timeout_s)
        )
        state = _parse_state_blob(sout)
        if src_rc != 0 and _is_transport_drop(src_rc, sout):
            tick_ok = False
        status = str(state.get("status") or "")
        phase = str(state.get("phase") or "")
        state_verdict = str(state.get("verdict") or "")
        state_rc = state.get("rc")

        if status in ("done", "failed"):
            verdict = state_verdict if state_verdict in ("PASS", "FRACTURE") else parse_verdict(captured)
            try:
                rc = int(state_rc) if state_rc is not None else (0 if status == "done" else 1)
            except (TypeError, ValueError):
                rc = 0 if status == "done" else 1
            _emit(f"[iac] terminal state status={status} phase={phase} "
                  f"rc={rc} verdict={verdict}\n")
            # one final drain of any trailing bytes written before the flush.
            if ft_obs:
                await _delta_sync_once(args, node, delta, _emit)
                _delta_flush_partial(delta, _emit)
            else:
                offset = await _tail_once(args, node, offset, _emit)
            break

        # --- (c) lock liveness -------------------------------------------- #
        lrc, lout = await _probe(
            _liveness_probe_cmd(args, node), timeout_s=float(args.probe_timeout_s)
        )
        lout = (lout or "").strip()
        if lrc != 0 and _is_transport_drop(lrc, lout):
            tick_ok = False
        elif lout.startswith("GONE") and status not in ("running", ""):
            # lock gone AND a terminal-ish state seen -> treat as done/failed.
            verdict = state_verdict if state_verdict in ("PASS", "FRACTURE") else parse_verdict(captured)
            rc = 0 if status == "done" else 1
            _emit(f"[iac] lock GONE with state={status} -- terminal\n")
            break

        # --- CONSTRAINT 1+4: heartbeat-driven liveness + dual-boundary wall - #
        # Liveness is judged by last_active ADVANCING (NOT by log output) -- a
        # paused surgery.out during a long quiet step is NO LONGER 'dead'. The
        # wall reads phase + the advancing heartbeat: active in a heavy phase ->
        # EXTEND, but CAPPED by MAX_PHASE_CEILING (a zombie ticking-but-stuck
        # swarm is still reaped, no infinite extend).
        if ft_obs:
            cur_active = state.get("last_active")
            try:
                cur_active_i = int(cur_active) if cur_active is not None else None
            except (TypeError, ValueError):
                cur_active_i = None
            if cur_active_i is not None and cur_active_i != last_seen_active:
                last_seen_active = cur_active_i
                last_active_change_mono = now  # heartbeat advanced -> alive
            heartbeat_stale = (now - last_active_change_mono) > stale_s
            elapsed = now - start_mono
            allowance = _phase_allowance(phase)
            ceiling = _phase_ceiling(phase)
            if elapsed > ceiling:
                # CONSTRAINT 4: past MAX_PHASE_CEILING -> REAP even if ticking.
                _emit(f"[iac] phase '{phase}' MAX_PHASE_CEILING {ceiling}s exceeded "
                      f"(elapsed {elapsed:.0f}s) -- reaping (no infinite extend)\n")
                rc, verdict = 124, parse_verdict(captured)
                break
            if elapsed > allowance:
                if heartbeat_stale:
                    # past the soft allowance AND the heartbeat froze -> a true
                    # hang (not a quiet step) -> reap (no zombie burning budget).
                    _emit(f"[iac] phase '{phase}' allowance {allowance}s exceeded AND "
                          f"heartbeat FROZEN ({now - last_active_change_mono:.0f}s "
                          f"> {stale_s}s) -- reaping\n")
                    rc, verdict = 125, parse_verdict(captured)
                    break
                # past the soft allowance but heartbeat ADVANCING -> EXTEND (bounded
                # by the ceiling, checked above).
                _emit(f"[iac] phase '{phase}' active (heartbeat advancing) past "
                      f"allowance {allowance}s -- EXTENDING toward ceiling {ceiling}s\n")

        # --- liveness accounting ------------------------------------------ #
        if tick_ok:
            consecutive_fail = 0
            first_fail_mono = None
            attempt = 0  # a good tick resets the backoff (fast next poll)
        else:
            consecutive_fail += 1
            if first_fail_mono is None:
                first_fail_mono = now
            elapsed_fail = now - first_fail_mono
            if elapsed_fail > liveness_deadline:
                _emit(f"[iac] liveness deadline {liveness_deadline}s exceeded over "
                      f"{consecutive_fail} consecutive failed probes -- node unreachable, reaping\n")
                rc, verdict = 125, parse_verdict(captured)
                break
            attempt += 1
            _emit(f"[iac] probe drop #{consecutive_fail} swallowed "
                  f"(elapsed {elapsed_fail:.0f}s / {liveness_deadline}s) -- retrying\n")

        delay = _backoff_delay(attempt, base=base, cap=cap, jitter=jitter)
        await asyncio.sleep(delay)

    _log(f"remote surgery (detached) finished rc={rc} verdict={verdict}")
    return rc, captured, verdict


# --------------------------------------------------------------------------- #
# Autopsy-before-burn (mirror the bake). Bounded + fail-soft, NEVER blocks burn.
# --------------------------------------------------------------------------- #
_AUTOPSY_CMDS: List[Tuple[str, str]] = [
    ("startup.log", "sudo cat /var/log/sovereign_iac_startup.log 2>/dev/null || echo '(absent)'"),
    ("burn.log", "sudo cat /var/log/sovereign_burn.log 2>/dev/null || echo '(absent)'"),
    ("docker_compose_logs", f"cd {_REMOTE_TRINITY_ROOT}/jarvis 2>/dev/null && sudo docker compose logs --no-color 2>&1 | tail -200 || echo '(no compose)'"),
    ("docker_ps", "sudo docker ps -a 2>&1 || echo '(docker unavailable)'"),
    ("df_h", "df -h / 2>&1"),
]
_AUTOPSY_CMD_TIMEOUT_S = float(os.environ.get("JARVIS_IAC_AUTOPSY_CMD_TIMEOUT_S", "30"))


def run_autopsy(
    args: argparse.Namespace, node: str, reason: str,
    surgery_output: Optional[List[str]] = None,
) -> Optional[pathlib.Path]:
    """Capture remote docker compose logs + the surgery output BEFORE the burn.
    Bounded + fail-soft: NEVER raises, NEVER blocks the burn."""
    try:
        stamp = _now_stamp()
        safe_node = re.sub(r"[^A-Za-z0-9_.-]", "_", node)[:60]
        outdir = pathlib.Path(_AUTOPSY_DIR)
        outdir.mkdir(parents=True, exist_ok=True)
        report_path = outdir / f"iac_{safe_node}_{stamp}.log"
        lines: List[str] = [
            "# Sovereign IaC Hypervisor autopsy",
            f"# node    : {node}",
            f"# reason  : {reason}",
            f"# captured: {stamp}",
            f"# zone    : {args.zone}",
            f"# project : {args.project}",
            "",
        ]
        if surgery_output:
            lines.append("=" * 60)
            lines.append("# surgery_output (local capture)")
            lines.append("=" * 60)
            lines.append("".join(surgery_output).strip())
            lines.append("")
        for label, remote_cmd in _AUTOPSY_CMDS:
            lines.append("=" * 60)
            lines.append(f"# {label}")
            lines.append("=" * 60)
            try:
                rc, out = _run(_ssh_cmd(args, node, remote_cmd), timeout_s=_AUTOPSY_CMD_TIMEOUT_S)
                lines.append(f"[rc={rc}]")
                lines.append((out or "(empty output)").strip())
            except Exception as exc:  # noqa: BLE001
                lines.append(f"[capture failed: {exc!r}]")
            lines.append("")
        report_path.write_text("\n".join(lines), encoding="utf-8")
        _log(f"autopsy written -> {report_path}")
        return report_path
    except Exception as exc:  # noqa: BLE001 -- autopsy NEVER blocks the burn
        _log(f"autopsy FAILED (proceeding to burn): {exc!r}")
        return None


# --------------------------------------------------------------------------- #
# CONSTRAINT 3: MANDATORY artifact-rescue phase BEFORE any teardown.
#
# Generalizes the Black-Box checksum-gated teardown to EVERY reap path: scp-pull
# (with retries) soak_state.json + surgery.out + the session debug.log + a1_runs/
# to a local rescue dir, sha256-VERIFY they landed, and ONLY THEN issue the GCP
# delete. If scp fails after max retries (sshd crashed / NIC dropped) the ultimate
# fallback BEFORE deletion is OUT-OF-BAND gcloud: capture the serial console
# (`get-serial-port-output`) + best-effort `disks snapshot` -> we NEVER burn a
# node without capturing serial/disk. Reuses _scp_cmd (the IAP pull transport) +
# _run -- NO new transport.
# --------------------------------------------------------------------------- #
def _scp_pull_cmd(
    args: argparse.Namespace, node: str, remote_path: str, local_dir: str,
) -> List[str]:
    """`gcloud compute scp --recurse <node>:<remote> <local>` over IAP -- the PULL
    direction of the established _scp_cmd transport (node -> local rescue dir)."""
    return [
        "gcloud", "compute", "scp", "--recurse",
        f"--project={args.project}", f"--zone={args.zone}",
        "--tunnel-through-iap",
        f"{node}:{remote_path}", local_dir,
    ]


def _serial_port_oob_cmd(args: argparse.Namespace, node: str) -> List[str]:
    """OUT-OF-BAND serial console capture (no SSH -- survives a dead sshd / NIC).
    The control-plane API streams the boot/console buffer."""
    return [
        "gcloud", "compute", "instances", "get-serial-port-output", node,
        f"--project={args.project}", f"--zone={args.zone}", "--port=1",
    ]


def _disk_snapshot_oob_cmd(
    args: argparse.Namespace, node: str, snapshot_name: str,
) -> List[str]:
    """OUT-OF-BAND best-effort disk snapshot (control-plane, no SSH) -- captures
    the boot disk so a dead-SSH node's state survives the burn. The boot disk
    shares the node name on these single-disk soak instances."""
    return [
        "gcloud", "compute", "disks", "snapshot", node,
        f"--project={args.project}", f"--zone={args.zone}",
        f"--snapshot-names={snapshot_name}",
    ]


def _sha256_file(path: pathlib.Path) -> Optional[str]:
    """sha256 of a file (None if unreadable). Used to VERIFY a rescued artifact
    actually landed locally before the node is burned."""
    try:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:  # noqa: BLE001 -- a missing/unreadable rescue artifact == not landed
        return None


def rescue_artifacts_before_teardown(
    args: argparse.Namespace, node: str, reason: str,
) -> Dict[str, Any]:
    """Pull + sha256-VERIFY the black-box artifacts BEFORE any burn. Returns a
    manifest dict {artifact: sha256|None, oob: {...}, verified: bool}. Bounded +
    fail-soft: NEVER raises, NEVER blocks the burn -- but ALWAYS runs first.

    Order (load-bearing): scp-pull (retries) -> sha256-verify -> [if any pull
    failed] OUT-OF-BAND serial + snapshot. The CALLER issues the GCP delete only
    AFTER this returns. Gated: when fault-tolerant-obs is OFF this is a no-op
    (legacy autopsy-only behavior, byte-identical)."""
    manifest: Dict[str, Any] = {"reason": reason, "artifacts": {}, "oob": {}, "verified": False}
    if not _fault_tolerant_obs_enabled(args):
        return manifest  # OFF -> legacy behavior (autopsy only), byte-identical
    try:
        stamp = _now_stamp()
        safe_node = re.sub(r"[^A-Za-z0-9_.-]", "_", node)[:60]
        rescue_root = pathlib.Path(_DEFAULT_RESCUE_DIR) / f"{safe_node}_{stamp}"
        rescue_root.mkdir(parents=True, exist_ok=True)
        retries = int(getattr(args, "rescue_retries", _DEFAULT_RESCUE_RETRIES))
        timeout_s = float(getattr(args, "rescue_timeout_s", _DEFAULT_RESCUE_TIMEOUT_S))
        any_pull_failed = False
        _log(f"RESCUE: pulling black-box artifacts BEFORE burn -> {rescue_root}")
        for remote_path in _RESCUE_ARTIFACTS:
            landed = False
            for attempt in range(max(1, retries)):
                rc, out = _run(
                    _scp_pull_cmd(args, node, remote_path, str(rescue_root)),
                    timeout_s=timeout_s,
                )
                if rc == 0:
                    landed = True
                    break
                _log(f"RESCUE: pull {remote_path} attempt {attempt + 1}/{retries} "
                     f"rc={rc} ({(out or '').strip()[:120]})")
            # sha256-verify the landed copy (the basename under rescue_root).
            local_copy = rescue_root / pathlib.PurePosixPath(remote_path).name
            digest = _sha256_file(local_copy) if landed and local_copy.is_file() else None
            # a directory artifact (sessions/a1_runs) lands as a tree -> mark present.
            if landed and digest is None and local_copy.is_dir():
                digest = "dir:present"
            manifest["artifacts"][remote_path] = digest
            if not landed or digest is None:
                any_pull_failed = True
        # CONSTRAINT 3: dead-SSH fallback -> OUT-OF-BAND serial + snapshot BEFORE burn.
        if any_pull_failed:
            _log("RESCUE: scp pulls incomplete (dead-SSH?) -- OUT-OF-BAND serial + snapshot")
            src, sout = _run(_serial_port_oob_cmd(args, node), timeout_s=timeout_s)
            serial_path = rescue_root / "serial_console.log"
            try:
                serial_path.write_text(sout or "(empty serial output)", encoding="utf-8")
                manifest["oob"]["serial"] = _sha256_file(serial_path)
            except Exception as exc:  # noqa: BLE001
                manifest["oob"]["serial"] = f"[write failed: {exc!r}]"
            snap_name = f"rescue-{safe_node}-{stamp}"[:62]
            drc, dout = _run(_disk_snapshot_oob_cmd(args, node, snap_name), timeout_s=timeout_s)
            manifest["oob"]["snapshot"] = snap_name if drc == 0 else f"[snapshot rc={drc}]"
        manifest["verified"] = not any_pull_failed
        try:
            (rescue_root / "rescue_manifest.json").write_text(
                json.dumps(manifest, indent=2), encoding="utf-8"
            )
        except Exception:  # noqa: BLE001
            pass
        _log(f"RESCUE: complete verified={manifest['verified']} -> {rescue_root}")
        return manifest
    except Exception as exc:  # noqa: BLE001 -- rescue NEVER blocks the burn
        _log(f"RESCUE: FAILED (proceeding to burn): {exc!r}")
        manifest["error"] = repr(exc)
        return manifest


# --------------------------------------------------------------------------- #
# Phase 4: The Ultimate Dead-Man's Burn.
# --------------------------------------------------------------------------- #
def burn_node(args: argparse.Namespace, node: str) -> None:
    """Burn the node -- ALWAYS runs (PASS / FRACTURE / exception / SSH-drop).

    (a) LOCAL: gcloud instances delete (best-effort, fail-soft).
    (b) REMOTE: the node-side dead-man self-DELETEs independently (the
        startup-script's SA-token REST) -- fires even if this local Mac dies /
        SSH drops / the orchestrator is killed.
    (c) SPOT: DELETE-on-preempt backstop (GCP).
    (d) max_run_duration: hard ceiling backstop (GCP).

    The completion-sentinel makes the remote dead-man fire IMMEDIATELY on
    surgery-done. This function issues the LOCAL delete + records the quadruple
    teardown; the remote/spot/max-run paths are independent of this process.
    """
    _log(f"BURN: issuing local gcloud delete for {node} (best-effort)")
    rc, out = _run(_delete_node_cmd(args, node), timeout_s=300.0)
    if rc == 0:
        _log(f"BURN: local delete accepted for {node}")
    else:
        _log(f"BURN: local delete rc={rc} ({out.strip()[:200]}) -- remote dead-man + spot + max-run still burn it")
    print(
        "[IAC] node burned (local-delete + remote-deadman + spot-DELETE + "
        "max-run-duration -- quadruple teardown)",
        flush=True,
    )


def verify_node_gone(args: argparse.Namespace, node: str) -> bool:
    """Confirm the node is GONE via `gcloud instances describe` (rc != 0 == gone).
    Fail-soft -- a describe error is treated as 'cannot confirm' (False)."""
    rc, _ = _run(_describe_node_cmd(args, node), timeout_s=60.0)
    gone = rc != 0
    _log(f"verify-gone: node {node} {'GONE (confirmed)' if gone else 'STILL PRESENT (describe rc=0)'}")
    return gone


# --------------------------------------------------------------------------- #
# Dry-run plan printer.
# --------------------------------------------------------------------------- #
def _print_plan(args: argparse.Namespace, node: str, startup_script: str, excludes: List[str]) -> None:
    print("=" * 72)
    print("SOVEREIGN IaC HYPERVISOR -- PLAN (dry-run, spends nothing)")
    print("=" * 72)
    print(f"  project          : {args.project}")
    print(f"  zone             : {args.zone}")
    print(f"  machine-type     : {args.machine_type}  (32GB -- the M1 can't host this)")
    print(f"  node             : {node}  (SPOT, DELETE-on-preempt)")
    print(f"  source image     : {args.source_image_family}/{args.source_image_project}")
    print(f"  boot disk        : {args.boot_disk_size}")
    print(f"  max-run-duration : {args.max_run_duration_s}s  (GCP hard ceiling)")
    print(f"  ready timeout    : {args.ready_timeout_s}s")
    print(f"  surgery cmd      : {args.surgery_cmd}")
    print(f"  surgery timeout  : {args.surgery_timeout_s}s")
    print(f"  completion sentinel: {args.completion_sentinel}")
    print(f"  remote root      : {_REMOTE_TRINITY_ROOT}/{{jarvis,prime,reactor}}")
    print(f"  rsync excludes   : {excludes}")
    print("  repo paths:")
    for name, local in _resolve_repo_paths(args):
        print(f"    {name:8s}: {local or '<UNSET -- set JARVIS_' + name.upper() + '_REPO_PATH>'}")
    print("-" * 72)
    print("STARTUP-SCRIPT (Docker install + ready sentinel + Dead-Man's Burn watchdog):")
    print(startup_script)
    print("-" * 72)
    print("COMMANDS THAT WOULD RUN (in order):")
    print("  1. PROVISION (Cloud Projector):")
    print("     " + " ".join(shlex.quote(c) for c in _create_node_cmd(args, node, "<startup-script-tmpfile>")))
    print("  2. POLL READY (repeated, bounded):")
    print("     " + " ".join(shlex.quote(c) for c in _ssh_cmd(args, node, "test -f " + _READY_SENTINEL + " ...")))
    print("  3. SYNC BRIDGE (3 repos -> /opt/trinity/*, excludes applied):")
    for name, local in _resolve_repo_paths(args):
        cmd = _scp_cmd(args, node, local or "<UNSET>", f"{_REMOTE_TRINITY_ROOT}/{name}", excludes)
        print("     " + " ".join(shlex.quote(c) for c in cmd))
    print("  4. REMOTE SURGERY (streamed live to local terminal):")
    print("     " + " ".join(shlex.quote(c) for c in _ssh_cmd(args, node, _remote_surgery_shell(args))))
    print("  5. BURN (finally -- ALWAYS, quadruple teardown):")
    print("     " + " ".join(shlex.quote(c) for c in _delete_node_cmd(args, node)))
    print("     + remote dead-man self-DELETE (independent) + spot-DELETE + max-run-duration")
    print("  6. VERIFY GONE:")
    print("     " + " ".join(shlex.quote(c) for c in _describe_node_cmd(args, node)))
    print("-" * 72)
    print("QUADRUPLE TEARDOWN: local-delete + remote-deadman + spot-DELETE + max-run-duration")
    print(f"COST ESTIMATE: e2-standard-8 Spot ~$0.08/hr; one surgery ~{args.max_run_duration_s // 60}min")
    print(f"  -> typical ~$0.02-0.10 per surgery (bounded HARD by max-run-duration={args.max_run_duration_s}s)")
    print("=" * 72)
    print("[IAC] --dry-run: nothing executed, no money spent. Use --execute "
          "--i-understand-this-spends-money to run.")


def _print_checkpoint_plan(args: argparse.Namespace, ledger: CheckpointLedger, run_id: str) -> None:
    """Dry-run: surface the checkpoint/resume + streaming plan (spends nothing)."""
    print("-" * 72)
    print("CHECKPOINT / RESUME PLAN (resume-don't-restart):")
    print(f"  state ledger     : {ledger.path}  (env JARVIS_IAC_STATE_PATH)")
    print(f"  run-id           : {run_id}")
    print(f"  phase order      : {' -> '.join(_PHASE_ORDER)}")
    data = ledger.read()
    if data.get("node_name"):
        completed = CheckpointLedger.completed_phases(data)
        resume_at = CheckpointLedger.first_incomplete(data)
        print(f"  EXISTING checkpoint: node={data.get('node_name')} "
              f"zone={data.get('zone')} completed={completed or '<none>'}")
        if args.fresh:
            print("  --fresh           : the checkpoint would be CLEARED -> brand-new node")
        else:
            print(f"  on --execute      : if node is RUNNING -> RESUME from '{resume_at}' "
                  f"(skip {completed or '<none>'}); if GONE -> fresh node")
    else:
        print("  EXISTING checkpoint: <none>  -> --execute starts a fresh node")
    print(f"  keep-warm-on-fail : {args.keep_warm_on_failure}  "
          f"(resumable failure -> node LEFT WARM + checkpoint persisted + non-zero exit)")
    print(f"  burn-on-failure   : {args.burn_on_failure}  (force-burn even on resumable failure)")
    print("  NO-ORPHAN BACKSTOP: keep-warm is bounded -- node-side dead-man "
          f"idle>{args.node_idle_timeout_s}s + max-run-duration={args.max_run_duration_s}s "
          "+ Spot DELETE-on-preempt remain armed + independent of this process.")
    print("-" * 72)
    print("STREAMING PLAN (every long phase streams stdout/stderr LIVE, line-by-line):")
    print("  [provision] gcloud compute instances create ... (provisioning)")
    print("  [sync]      rsync --progress -v / scp ... (per-file + progress bars)")
    print("  [prebake]   remote docker build ... (WAN image layer builds)")
    print("  [boot]      remote air-gap compose bring-up ...")
    print("  [surgery]   remote Trinity surgery + FRACTURE/PASS ...")
    print(f"  tee log     : {_AUTOPSY_DIR}/iac_run_<run-id>.log (full transcript captured)")
    print("-" * 72)


# --------------------------------------------------------------------------- #
# Execute pipeline.
# --------------------------------------------------------------------------- #
def resolve_run_context(
    args: argparse.Namespace, ledger: CheckpointLedger, default_node: str, run_id: str,
) -> Tuple[str, Dict[str, Any], bool]:
    """Decide RESUME vs FRESH from the ledger + the live node status.

    Returns (node, ledger_data, resuming):
      * RESUME  -- a node is recorded AND verified still RUNNING AND not --fresh:
                   reconnect to the warm node, ledger_data carries the completed
                   phases, resuming=True. The orchestrator skips completed phases.
      * FRESH   -- no record / recorded node GONE (burned/preempted) / --fresh:
                   the ledger is cleared + re-seeded for `default_node`,
                   resuming=False.

    No money is spent here -- a resume only reconnects to a node ALREADY proven
    alive by `node_is_alive` (gcloud describe status == RUNNING)."""
    prior = ledger.read()
    recorded_node = (prior.get("node_name") or "").strip()

    if args.fresh:
        if recorded_node:
            _log(f"--fresh: ignoring checkpoint for {recorded_node}, starting clean")
        ledger.clear()
        data = ledger.init_run(
            run_id=run_id, node_name=default_node, zone=args.zone, project=args.project,
        )
        return default_node, data, False

    if not recorded_node:
        data = ledger.init_run(
            run_id=run_id, node_name=default_node, zone=args.zone, project=args.project,
        )
        return default_node, data, False

    # A node is recorded -- is it still warm? Reuse its zone/project from the
    # ledger so the alive-check targets the SAME node that was provisioned.
    probe_args = argparse.Namespace(**vars(args))
    probe_args.zone = prior.get("zone") or args.zone
    probe_args.project = prior.get("project") or args.project
    if node_is_alive(probe_args, recorded_node):
        completed = CheckpointLedger.completed_phases(prior)
        resume_at = CheckpointLedger.first_incomplete(prior) or "surgery_done"
        synced = CheckpointLedger.phase_complete(prior, "synced")
        print(
            f"[IAC RESUME] node {recorded_node} warm, files synced={synced}, "
            f"resuming from phase {resume_at} (skipping {completed or '<none>'})",
            flush=True,
        )
        # Inherit the recorded zone/project so we keep talking to the same node.
        args.zone = probe_args.zone
        args.project = probe_args.project
        return recorded_node, prior, True

    # Recorded node is GONE (burned / preempted / terminated) -> start clean.
    _log(f"checkpoint node {recorded_node} is GONE (not RUNNING) -- starting fresh")
    ledger.clear()
    data = ledger.init_run(
        run_id=run_id, node_name=default_node, zone=args.zone, project=args.project,
    )
    return default_node, data, False


def _execute(
    args: argparse.Namespace, node: str, startup_script: str, excludes: List[str],
    ledger: Optional[CheckpointLedger] = None, ledger_data: Optional[Dict[str, Any]] = None,
    resuming: bool = False, log_path: Optional[pathlib.Path] = None,
) -> int:
    """Run the checkpointed pipeline. Each phase, on success, stamps the ledger
    (atomic) so a *resumable* mid-pipeline failure can be re-run to resume from
    the first incomplete phase against the still-warm node.

    BURN POLICY (the #1 invariant, refined for resumability):
      * TERMINAL outcome (PASS / FRACTURE)  -> BURN ALWAYS. The surgery reached a
        verdict; the node has served its purpose.
      * UNRECOVERABLE error (raised exception / SSH-drop mid-finally) -> BURN. We
        cannot prove the node is in a resumable state, so we do not leave it warm.
      * --burn-on-failure -> BURN even on a resumable failure (operator cleanup).
      * RESUMABLE mid-pipeline failure (e.g. prebake PyPI timeout) with
        --keep-warm-on-failure (default TRUE) -> DO NOT burn: log the autopsy,
        leave the node WARM, persist the checkpoint, EXIT non-zero so the operator
        re-runs --execute to resume.

    NO-ORPHAN BACKSTOP (the trade made explicit): keep-warm enables resume, but a
    warm-left node can NEVER orphan forever -- the node-side dead-man (idle >
    JARVIS_IAC_NODE_IDLE_TIMEOUT_S) + the GCP max_run_duration hard ceiling + the
    Spot DELETE-on-preempt are still armed and independent of this process. The
    idle timeout is generous enough to permit a resume yet bounded; the max-run
    duration is the absolute ceiling. The local burn is one of FOUR teardowns;
    skipping it for resume does not remove the other three.
    """
    if ledger is None:
        ledger = CheckpointLedger(args.state_path)
    data: Dict[str, Any] = ledger_data if ledger_data is not None else ledger.read()

    node_exists = resuming or CheckpointLedger.phase_complete(data, "provisioned")
    verdict = "UNKNOWN"
    surgery_output: List[str] = []
    surgery_rc = 1
    abort_reason = ""
    succeeded = False
    resumable_failure = False  # set True when a phase fails recoverably

    def _done(phase: str) -> bool:
        return CheckpointLedger.phase_complete(data, phase)

    try:
        # PHASE 1: PROVISION. (skip if already provisioned on a warm resume)
        if not _done("provisioned"):
            ok, detail = provision_sandbox_node(args, node, startup_script, log_path=log_path)
            if not ok:
                _abort(detail)
                abort_reason = detail
                resumable_failure = True
                return 4
            node_exists = True
            data = ledger.mark_phase(data, "provisioned")
            _log(f"node {node} created; startup-script installing Docker + burn watchdog")
        else:
            _log("[resume] phase provisioned already complete -- skipping create")
        node_exists = True

        # PHASE 1b: DOCKER READY (poll for the ready sentinel).
        if not _done("docker_ready"):
            ready, reason = poll_node_ready(args, node)
            if not ready:
                _abort(f"readiness abort: {reason}")
                abort_reason = reason
                resumable_failure = True
                return 5
            data = ledger.mark_phase(data, "docker_ready")
        else:
            _log("[resume] phase docker_ready already complete -- skipping poll")

        # PHASE 2: SYNC BRIDGE.
        if not _done("synced"):
            ok, detail = sync_repos_to_node(args, node, excludes, log_path=log_path)
            if not ok:
                _abort(f"sync abort: {detail}")
                abort_reason = detail
                # A SECRET failure (git transport: .env couldn't be injected, or a
                # parity violation) is NOT resumable -- a node without .env / on the
                # WRONG commit is useless. Leave resumable_failure False so the
                # finally-block BURNS it (do NOT keep-warm a secretless node).
                if is_secret_failure(detail):
                    _log("sync failure classified SECRET/PARITY -> BURN (node useless, no keep-warm)")
                    resumable_failure = False
                else:
                    resumable_failure = True
                return 6
            data = ledger.mark_phase(data, "synced")
        else:
            _log("[resume] phase synced already complete -- skipping sync")

        # PHASE 2b: PREBAKE (WAN docker build -- the first-run-friction step).
        if not _done("prebaked"):
            ok, detail = run_remote_prebake(args, node, log_path=log_path)
            if not ok:
                _abort(f"prebake abort: {detail}")
                abort_reason = detail
                resumable_failure = True
                return 8
            data = ledger.mark_phase(data, "prebaked")
        else:
            _log("[resume] phase prebaked already complete -- skipping prebake")

        # PHASE 2c: BOOT (air-gapped compose bring-up).
        if not _done("booted"):
            ok, detail = run_remote_boot(args, node, log_path=log_path)
            if not ok:
                _abort(f"boot abort: {detail}")
                abort_reason = detail
                resumable_failure = True
                return 9
            data = ledger.mark_phase(data, "booted")
        else:
            _log("[resume] phase booted already complete -- skipping boot")

        # PHASE 3: REMOTE SURGERY (streamed). This is TERMINAL -- PASS/FRACTURE.
        surgery_rc, surgery_output, verdict = run_remote_surgery(args, node, log_path=log_path)
        if verdict in ("PASS", "FRACTURE"):
            data = ledger.mark_phase(data, "surgery_done", verdict=verdict)
            succeeded = True  # reached a terminal verdict
            return 0
        # No verdict -> the surgery did not converge; treat as resumable (the node
        # is warm + synced + prebaked, a re-run resumes straight at surgery).
        resumable_failure = True
        return 7
    finally:
        # PHASE 4: THE ULTIMATE BURN -- refined for resumability.
        #
        # keep_warm == True iff: a RESUMABLE failure happened AND
        # --keep-warm-on-failure AND NOT --burn-on-failure AND we did NOT reach a
        # terminal verdict. A raised exception bypasses the resumable_failure flag
        # (it's set only on the recoverable return paths), so an exception / SSH
        # drop ALWAYS burns -- we cannot prove a crashed run left a resumable node.
        terminal = succeeded and verdict in ("PASS", "FRACTURE")
        keep_warm = (
            node_exists
            and resumable_failure
            and args.keep_warm_on_failure
            and not args.burn_on_failure
            and not terminal
        )
        if node_exists and keep_warm:
            # Autopsy (never blocks), persist the checkpoint, leave the node WARM.
            run_autopsy(args, node, abort_reason or f"verdict={verdict}", surgery_output)
            ledger.write(data)
            print(
                f"[IAC KEEP-WARM] node {node} left RUNNING after resumable failure "
                f"({abort_reason or 'no verdict'}); checkpoint persisted -> {ledger.path}. "
                f"Re-run --execute to RESUME. NO-ORPHAN BACKSTOP armed: node-side "
                f"dead-man idle>{args.node_idle_timeout_s}s + max-run-duration="
                f"{args.max_run_duration_s}s + Spot DELETE-on-preempt.",
                flush=True,
            )
        elif node_exists:
            # Terminal verdict / exception / --burn-on-failure -> BURN ALWAYS.
            if not terminal:
                run_autopsy(args, node, abort_reason or f"verdict={verdict}", surgery_output)
            # CONSTRAINT 3: MANDATORY artifact rescue + sha256 verify (+ dead-SSH
            # OOB fallback) BEFORE the delete is issued -- NEVER burn a node before
            # its data is local. No-op (legacy) when fault-tolerant-obs is OFF.
            rescue_artifacts_before_teardown(args, node, abort_reason or f"verdict={verdict}")
            burn_node(args, node)
            verify_node_gone(args, node)
            # Surgery reached a verdict -> the run is DONE, clear the checkpoint.
            if terminal:
                ledger.clear()
        _report(args, node, verdict, surgery_rc, node_exists, keep_warm)


def _report(
    args: argparse.Namespace, node: str, verdict: str, surgery_rc: int,
    node_existed: bool, kept_warm: bool = False,
) -> None:
    print("=" * 72)
    print("[IAC] SESSION REPORT")
    print("=" * 72)
    if not node_existed:
        node_state = "never provisioned"
    elif kept_warm:
        node_state = "LEFT WARM (resumable -- re-run --execute)"
    else:
        node_state = "provisioned + burned"
    print(f"  node       : {node} ({node_state})")
    print(f"  verdict    : {verdict}")
    print(f"  surgery rc : {surgery_rc}")
    if kept_warm:
        print("  teardown   : DEFERRED (keep-warm) -- backstop: node-deadman + "
              "spot-DELETE + max-run-duration (no infinite orphan)")
    else:
        print("  teardown   : quadruple (local-delete + remote-deadman + spot-DELETE + max-run-duration)")
    print("=" * 72)


# --------------------------------------------------------------------------- #
# Triple-gate check.
# --------------------------------------------------------------------------- #
def _master_enabled() -> bool:
    raw = (os.environ.get(_ENV_MASTER, "") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def check_triple_gate(args: argparse.Namespace) -> Tuple[bool, str]:
    """Triple-gate: requires ALL of JARVIS_IAC_HYPERVISOR_ENABLED + --execute +
    --i-understand-this-spends-money. Returns (ok, reason-if-refused)."""
    if not _master_enabled():
        return False, f"master gate off -- set {_ENV_MASTER}=1 to enable"
    if args.dry_run:
        return False, "--dry-run (default) -- pass --execute to run"
    if not args.i_understand_this_spends_money:
        return False, "missing --i-understand-this-spends-money (real-money safety gate)"
    return True, ""


# --------------------------------------------------------------------------- #
# CLI.
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Sovereign IaC Hypervisor -- project the Trinity to GCP (e2-standard-8 "
            "Spot), sync 3 repos, run the unsimulated cross-repo surgery streamed "
            "to the local terminal, self-burn (quadruple dead-man). Default --dry-run."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--project", default=_DEFAULT_PROJECT, help="GCP project (env GCP_PROJECT)")
    p.add_argument("--zone", default=_DEFAULT_ZONE, help="GCP zone (env GCP_ZONE)")
    p.add_argument("--machine-type", default=_DEFAULT_MACHINE, help="32GB node (env JARVIS_IAC_MACHINE)")
    p.add_argument("--boot-disk-size", default=_DEFAULT_BOOT_DISK)
    p.add_argument("--source-image-family", default=_DEFAULT_DEBIAN_IMAGE_FAMILY)
    p.add_argument("--source-image-project", default=_DEFAULT_DEBIAN_IMAGE_PROJECT)
    # ---- Soak golden image (kills the ~20-min pip-install sink). Default-OFF --- #
    p.add_argument(
        "--soak-golden", dest="soak_golden", action="store_true",
        default=_env_truthy_off("JARVIS_IAC_SOAK_GOLDEN_ENABLED"),
        help="boot from the jarvis-soak-golden image (deps pre-installed) when it "
             "exists + skip pip; INDESTRUCTIBLE fallback to debian-12+pip on any "
             "golden failure (default OFF -> byte-identical debian-12+pip)",
    )
    p.add_argument(
        "--no-soak-golden", dest="soak_golden", action="store_false",
        help="force the legacy debian-12 + full-pip path (byte-identical OFF path)",
    )
    p.add_argument("--soak-golden-image-family", default=_DEFAULT_SOAK_GOLDEN_IMAGE_FAMILY,
                   help="soak golden image family (env JARVIS_IAC_SOAK_GOLDEN_IMAGE_FAMILY)")
    p.add_argument("--golden-verify-timeout-s", type=int,
                   default=_DEFAULT_GOLDEN_VERIFY_TIMEOUT_S,
                   help="deps-present probe budget before the indestructible "
                        "fallback fires (env JARVIS_IAC_GOLDEN_VERIFY_TIMEOUT_S)")
    p.add_argument("--requirements-path", default=_DEFAULT_REQUIREMENTS_PATH,
                   help="requirements.txt for the staleness sha (env JARVIS_IAC_REQUIREMENTS_PATH)")
    p.add_argument("--max-run-duration-s", type=int, default=_DEFAULT_MAX_RUN_DURATION_S,
                   help="GCP hard ceiling (env JARVIS_IAC_MAX_RUN_DURATION_S)")
    p.add_argument("--ready-timeout-s", type=int, default=_DEFAULT_READY_TIMEOUT_S)
    p.add_argument("--sync-timeout-s", type=int, default=int(os.environ.get("JARVIS_IAC_SYNC_TIMEOUT_S", "900")))
    p.add_argument("--surgery-timeout-s", type=int, default=int(os.environ.get("JARVIS_IAC_SURGERY_TIMEOUT_S", "2400")))
    p.add_argument("--surgery-cmd", default=_DEFAULT_SURGERY_CMD, help="remote surgery command (env JARVIS_IAC_SURGERY_CMD)")
    # Detached surgery daemon + poll/reconnect loop (the run-#15 SSH-drop fix).
    # Default-ON; --no-detached-surgery falls back to the legacy single-stream path.
    p.add_argument(
        "--detached-surgery", dest="detached_surgery", action="store_true",
        default=_env_truthy("JARVIS_IAC_DETACHED_SURGERY_ENABLED", "true"),
        help="detach the surgery (setsid/nohup/systemd-run) + poll/reconnect (default ON)",
    )
    p.add_argument(
        "--no-detached-surgery", dest="detached_surgery", action="store_false",
        help="legacy single long-lived streaming SSH session (byte-identical OFF path)",
    )
    p.add_argument("--poll-base-s", type=float, default=_DEFAULT_POLL_BASE_S,
                   help="poll backoff base seconds (env JARVIS_IAC_POLL_BASE_S)")
    p.add_argument("--poll-cap-s", type=float, default=_DEFAULT_POLL_CAP_S,
                   help="poll backoff cap seconds (env JARVIS_IAC_POLL_CAP_S)")
    p.add_argument("--poll-jitter-s", type=float, default=_DEFAULT_POLL_JITTER_S,
                   help="poll backoff jitter seconds (env JARVIS_IAC_POLL_JITTER_S)")
    p.add_argument("--probe-timeout-s", type=float, default=_DEFAULT_PROBE_TIMEOUT_S,
                   help="single SSH probe timeout (env JARVIS_IAC_PROBE_TIMEOUT_S)")
    p.add_argument("--liveness-deadline-s", type=float, default=_DEFAULT_LIVENESS_DEADLINE_S,
                   help="consecutive-failed-probe liveness bound (env JARVIS_IAC_LIVENESS_DEADLINE_S)")
    p.add_argument("--max-wall-seconds", type=float, default=_DEFAULT_MAX_WALL_S,
                   help="absolute poll-loop wall ceiling, 0=use surgery-timeout (env JARVIS_IAC_MAX_WALL_SECONDS)")
    # ---- Fault-tolerant observability (Omni-Soak #2 fix). Default-OFF -------- #
    p.add_argument(
        "--fault-tolerant-obs", dest="fault_tolerant_obs", action="store_true",
        default=_env_truthy_off("JARVIS_IAC_FAULT_TOLERANT_OBS_ENABLED"),
        help="anti-starvation heartbeat + line-safe delta-sync + artifact-rescue + "
             "dual-boundary wall (default OFF -> byte-identical legacy)",
    )
    p.add_argument(
        "--no-fault-tolerant-obs", dest="fault_tolerant_obs", action="store_false",
        help="legacy byte-offset tail + dumb wall (byte-identical OFF path)",
    )
    p.add_argument("--heartbeat-interval-s", type=float, default=_DEFAULT_HEARTBEAT_INTERVAL_S,
                   help="node-side heartbeat tick interval (env JARVIS_IAC_HEARTBEAT_INTERVAL_S)")
    p.add_argument("--heartbeat-nice", default=_DEFAULT_HEARTBEAT_NICE,
                   help="heartbeat nice level (env JARVIS_IAC_HEARTBEAT_NICE)")
    p.add_argument("--heartbeat-ionice-class", default=_DEFAULT_HEARTBEAT_IONICE_CLASS,
                   help="heartbeat ionice class, 1=realtime (env JARVIS_IAC_HEARTBEAT_IONICE_CLASS)")
    p.add_argument("--heartbeat-ionice-prio", default=_DEFAULT_HEARTBEAT_IONICE_PRIO,
                   help="heartbeat ionice priority (env JARVIS_IAC_HEARTBEAT_IONICE_PRIO)")
    p.add_argument("--heartbeat-stale-s", type=float, default=_DEFAULT_HEARTBEAT_STALE_S,
                   help="last_active staleness -> heartbeat FROZEN (env JARVIS_IAC_HEARTBEAT_STALE_S)")
    p.add_argument("--global-ceiling-s", type=float, default=_DEFAULT_GLOBAL_CEILING_S,
                   help="global hard wall ceiling, 0=use max-wall (env JARVIS_IAC_GLOBAL_CEILING_SECONDS)")
    p.add_argument("--rescue-retries", type=int, default=_DEFAULT_RESCUE_RETRIES,
                   help="artifact-rescue scp pull retries (env JARVIS_IAC_RESCUE_RETRIES)")
    p.add_argument("--rescue-timeout-s", type=float, default=_DEFAULT_RESCUE_TIMEOUT_S,
                   help="artifact-rescue per-pull timeout (env JARVIS_IAC_RESCUE_TIMEOUT_S)")
    p.add_argument("--completion-sentinel", default=_COMPLETION_SENTINEL)
    p.add_argument("--prime-repo-path", default=None, help="prime repo (env JARVIS_PRIME_REPO_PATH)")
    p.add_argument("--reactor-repo-path", default=None, help="reactor repo (env JARVIS_REACTOR_REPO_PATH)")
    p.add_argument("--rsync-excludes", default=_DEFAULT_RSYNC_EXCLUDES,
                   help="comma-separated excludes (env JARVIS_IAC_RSYNC_EXCLUDES)")
    p.add_argument("--node-name", default=None, help="node name (default sovereign-sandbox-<ts>)")
    p.add_argument("--i-understand-this-spends-money", action="store_true",
                   help="REAL-MONEY safety gate (required with --execute)")
    # -- prebake / boot phases (streamed + checkpointed when their cmds are set) --
    p.add_argument("--prebake-cmd", default=_DEFAULT_PREBAKE_CMD,
                   help="remote WAN docker-build prebake (env JARVIS_IAC_PREBAKE_CMD; "
                        "empty == folded into surgery)")
    p.add_argument("--prebake-timeout-s", type=int,
                   default=int(os.environ.get("JARVIS_IAC_PREBAKE_TIMEOUT_S", "1800")))
    p.add_argument("--boot-cmd", default=_DEFAULT_BOOT_CMD,
                   help="remote air-gap compose boot (env JARVIS_IAC_BOOT_CMD; empty == folded into surgery)")
    p.add_argument("--boot-timeout-s", type=int,
                   default=int(os.environ.get("JARVIS_IAC_BOOT_TIMEOUT_S", "600")))
    # -- git-clone transport (JARVIS_IAC_SYNC_TRANSPORT=git) --
    p.add_argument("--git-clone-timeout-s", dest="git_clone_timeout_s", type=int,
                   default=_DEFAULT_GIT_CLONE_TIMEOUT_S,
                   help="bound the node-side anonymous git clone of origin@HEAD "
                        "(env JARVIS_IAC_GIT_CLONE_TIMEOUT_S; 581MB over WAN ~1-2min)")
    p.add_argument("--secret-timeout-s", dest="secret_timeout_s", type=int,
                   default=_DEFAULT_SECRET_TIMEOUT_S,
                   help="STRICT per-secret scp timeout for the .env injection "
                        "(env JARVIS_IAC_SECRET_TIMEOUT_S; fail-CLOSED -> burn)")
    # -- checkpoint / resume (resume-don't-restart) --
    p.add_argument("--state-path", default=_DEFAULT_STATE_PATH,
                   help="checkpoint ledger path (env JARVIS_IAC_STATE_PATH)")
    p.add_argument("--run-id", default=None,
                   help="run id stamped into the checkpoint (default: the node timestamp)")
    p.add_argument("--node-idle-timeout-s", type=int, default=_DEFAULT_NODE_IDLE_TIMEOUT_S,
                   help="node-side dead-man idle timeout, generous enough to allow a "
                        "resume yet bounded (env JARVIS_IAC_NODE_IDLE_TIMEOUT_S)")
    p.add_argument("--fresh", action="store_true",
                   help="ignore + clear any checkpoint; provision a brand-new node")
    p.add_argument("--on-demand", dest="on_demand", action="store_true",
                   help="provision STANDARD (on-demand, no Spot preemption) for an "
                        "uninterrupted multi-stage run; keeps max-run-duration + DELETE")
    p.add_argument("--keep-warm-on-failure", dest="keep_warm_on_failure",
                   action="store_true", default=True,
                   help="on a RESUMABLE mid-pipeline failure leave the node WARM + "
                        "persist the checkpoint so --execute can resume (default TRUE; "
                        "the dead-man + max-run-duration remain the no-orphan backstop)")
    p.add_argument("--no-keep-warm-on-failure", dest="keep_warm_on_failure",
                   action="store_false",
                   help="burn the node even on a resumable failure (legacy always-burn)")
    p.add_argument("--burn-on-failure", action="store_true",
                   help="force a burn even on a resumable failure (overrides keep-warm)")
    p.add_argument("--burn", action="store_true",
                   help="force-burn the checkpointed node (cleanup) then exit")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="dry_run", action="store_true",
                      help="print the plan + commands WITHOUT executing (default)")
    mode.add_argument("--execute", dest="dry_run", action="store_false",
                      help="actually run (spends money; needs the triple gate)")
    p.set_defaults(dry_run=True)
    return p


def _force_burn_checkpointed(args: argparse.Namespace, ledger: CheckpointLedger) -> int:
    """`--burn`: force-burn the node recorded in the checkpoint (cleanup), then
    clear the ledger. Reuses the same gcloud-delete + verify-gone path. If no
    node is recorded, there is nothing to burn."""
    data = ledger.read()
    node = (data.get("node_name") or args.node_name or "").strip()
    if not node:
        _log("--burn: no checkpointed node to burn (clean ledger)")
        return 0
    # Talk to the node's recorded zone/project.
    args.zone = data.get("zone") or args.zone
    args.project = data.get("project") or args.project
    _log(f"--burn: force-burning checkpointed node {node}")
    burn_node(args, node)
    verify_node_gone(args, node)
    ledger.clear()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    stamp = _now_stamp()
    run_id = args.run_id or stamp
    default_node = args.node_name or default_node_name(stamp)
    excludes = parse_excludes(args.rsync_excludes)
    # The dead-man idle timeout the startup-script bakes in is the resume-aware
    # JARVIS_IAC_NODE_IDLE_TIMEOUT_S -- generous enough to permit a re-run resume
    # yet bounded so a warm-left node can never orphan forever (the max-run
    # duration is the absolute ceiling regardless).
    startup_script = build_startup_script(
        completion_sentinel=args.completion_sentinel,
        idle_timeout_s=args.node_idle_timeout_s,
    )
    ledger = CheckpointLedger(args.state_path)

    if args.dry_run:
        if not _master_enabled():
            _log(f"NOTE: master gate {_ENV_MASTER} is OFF (dry-run prints the plan anyway).")
        _print_plan(args, default_node, startup_script, excludes)
        _print_checkpoint_plan(args, ledger, run_id)
        return 0

    if args.burn:
        return _force_burn_checkpointed(args, ledger)

    ok, reason = check_triple_gate(args)
    if not ok:
        _abort(f"triple-gate REFUSED: {reason}")
        _log("required: JARVIS_IAC_HYPERVISOR_ENABLED=1 + --execute + --i-understand-this-spends-money")
        return 2

    _log("EXECUTE mode -- this WILL provision a 32GB GCP node and spend money")
    # RESUME vs FRESH: reconnect to a warm checkpointed node + skip done phases.
    node, ledger_data, resuming = resolve_run_context(args, ledger, default_node, run_id)
    log_path = _run_log_path(ledger_data.get("run_id") or run_id)
    return _execute(
        args, node, startup_script, excludes,
        ledger=ledger, ledger_data=ledger_data, resuming=resuming, log_path=log_path,
    )


if __name__ == "__main__":
    sys.exit(main())
