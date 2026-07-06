#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Sovereign Brain-VM Golden-Image Bake -- CPU-only orchestrator runtime image.

Stage-1 Task 1 of the GCP Brain-VM provisioning plan
(plan:  docs/superpowers/plans/2026-07-04-stage1-brain-vm-provisioning.md
 spec:  docs/superpowers/specs/2026-07-03-gcp-orchestrator-relocation-design.md).

This bakes the golden image the GCP **Brain VM** boots from. Per the Body/Brain
split, the Brain VM hosts the Ouroboros orchestrator + full FSM pipeline and the
mutation-target repo clone at ``/opt/trinity/jarvis`` -- the ``IsomorphicEnv``
canonical path, now PROMOTED from soak-sandbox to the primary runtime.

It is the CPU sibling of ``bake_jprime_golden_image.py``. The difference from the
J-Prime code-model image:

  * NO Ollama, NO NVIDIA / GPU, NO model pull or hydration. Just a base Debian
    image + python3.11 + git.
  * Clones the repo to ``/opt/trinity/jarvis`` (URL + ref env-resolved at bake
    time -- ``JARVIS_BRAIN_REPO_URL`` / ``JARVIS_BRAIN_REPO_REF``).
  * venv ``pip install`` of the brain-scoped requirements
    (``backend/requirements-brain.txt`` -- Debian 12 system pip is PEP 668
    externally-managed, and the Brain never carries the Mac voice/ML stack).
  * Bakes a systemd unit ``jarvis-brain.service`` (headless warm-standby soak)
    + an idle self-shutdown timer.
  * ``/etc/jarvis/brain.env`` is written AT BOOT from instance metadata (tokens /
    cost-cap / endpoints are per-instance, NEVER baked into the image).

READINESS LOCK (the J-Prime failure contract, preserved): the readiness sentinel
is written ONLY after a REAL proof -- ``/opt/trinity/jarvis/.git`` exists AND
``python3 -c "import backend.core.ouroboros.governance.transport"`` succeeds.
Never a fixed sleep. Either proof fails -> no sentinel -> the reaper rolls the
node.

DRY: the run/log/verdict/stamp machinery is SHARED with the J-Prime baker via a
direct import (see below) -- zero reimplementation. Those helpers are
module-private-but-stable in ``bake_jprime_golden_image``; we import them
directly and accept the coupling on purpose (one bake substrate, two images).

Usage:
    python3 scripts/bake_brain_golden_image.py --dry-run   # default; spends $0
    python3 scripts/bake_brain_golden_image.py --execute    # actually bakes
"""
from __future__ import annotations

import argparse
import os
import shlex
import sys
import time
from typing import List, Optional, Tuple

# --------------------------------------------------------------------------- #
# DRY: share the J-Prime bake helpers directly. These are module-private (`_`-
# prefixed) but stable -- the run/log/abort/stamp/verdict machinery is identical
# across the bake family, so we import it rather than duplicate it. The coupling
# to ``bake_jprime_golden_image`` is intentional and load-bearing (one bake
# substrate, two golden images). ``parse_validation_verdict`` is imported for
# family parity + the shared-helper identity smoke; the Brain's readiness proof
# is done IN the startup-script (git + transport import), not via a generation.
# The sibling lives next to this file; add scripts/ to the path so a file-path
# load (tests) resolves the import the same way a normal `python3 scripts/...`
# invocation does.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bake_jprime_golden_image import (  # noqa: E402  (path insert must precede)
    _abort,
    _log,
    _now_stamp,
    _run,
    parse_validation_verdict,
)

# --------------------------------------------------------------------------- #
# Defaults -- EVERY value env/arg-resolved. Mandate 2 (Architectural Purity):
# no literal project IDs, zones, IPs, repo URLs, or cost caps anywhere. Where
# J-Prime hardcodes a project/zone fallback, the Brain baker deliberately does
# NOT -- an unset project/zone yields an empty default the operator must supply.
# --------------------------------------------------------------------------- #
_DEFAULT_PROJECT = os.environ.get("JARVIS_GCP_PROJECT", os.environ.get("GCP_PROJECT", ""))
# JARVIS_BRAIN_VM_ZONES may be a comma-list; the bake uses the first entry.
_DEFAULT_ZONE = (os.environ.get("JARVIS_BRAIN_VM_ZONES", "").split(",")[0]).strip()
_DEFAULT_MACHINE = os.environ.get("JARVIS_BRAIN_BAKE_MACHINE", "e2-standard-4")
_DEFAULT_REPO_URL = os.environ.get("JARVIS_BRAIN_REPO_URL", "")
_DEFAULT_REPO_REF = os.environ.get("JARVIS_BRAIN_REPO_REF", "main")
# SINGLE SOURCE OF TRUTH with the provisioner: the bake MUST create the same
# image family that gcp_compute_rest._brain_image_family() provisions FROM,
# or Task-5 ignition finds no image. Both read JARVIS_BRAIN_VM_IMAGE_FAMILY and
# default to "jarvis-brain-golden" -- structurally coupled, cannot drift.
# (The legacy JARVIS_BRAIN_IMAGE_FAMILY name is honored as a fallback so an
# operator who set the old var is not silently ignored.)
_DEFAULT_IMAGE_FAMILY = os.environ.get(
    "JARVIS_BRAIN_VM_IMAGE_FAMILY",
    os.environ.get("JARVIS_BRAIN_IMAGE_FAMILY", "jarvis-brain-golden"),
)
_DEFAULT_BOOT_DISK = os.environ.get("JARVIS_BRAIN_BAKE_BOOT_DISK_SIZE", "30GB")
_DEFAULT_BOOT_DISK_TYPE = os.environ.get("JARVIS_BRAIN_BAKE_BOOT_DISK_TYPE", "pd-ssd")
_DEFAULT_BAKE_TIMEOUT_S = int(os.environ.get("JARVIS_BRAIN_BAKE_TIMEOUT_S", "1800"))
_DEFAULT_DEBIAN_IMAGE_FAMILY = os.environ.get(
    "JARVIS_BRAIN_BAKE_SOURCE_IMAGE_FAMILY", "debian-12"
)
_DEFAULT_DEBIAN_IMAGE_PROJECT = os.environ.get(
    "JARVIS_BRAIN_BAKE_SOURCE_IMAGE_PROJECT", "debian-cloud"
)
# Idle self-shutdown default (seconds) baked into the idle-check as a fallback;
# the authoritative value comes from brain.env (instance metadata) at boot.
_DEFAULT_IDLE_SHUTDOWN_S = int(os.environ.get("JARVIS_BRAIN_VM_IDLE_SHUTDOWN_S", "1800"))

# The IsomorphicEnv canonical path -- the REAL primary runtime for the Brain VM.
_REPO_PATH = "/opt/trinity/jarvis"
# Readiness sentinel written by the startup-script ONLY after the git+transport
# proof. The poll loop keys on this (never a fixed sleep).
_SENTINEL_PATH = "/var/run/jarvis_brain_ready"
# Where per-instance config lands (written from metadata at boot).
_BRAIN_ENV_PATH = "/etc/jarvis/brain.env"
# The instance-metadata attribute carrying the brain.env body.
_BRAIN_ENV_METADATA_KEY = "brain-env"


def _default_image_name() -> str:
    return os.environ.get(
        "JARVIS_BRAIN_IMAGE_NAME", f"jarvis-brain-golden-{_now_stamp()}"
    )


# --------------------------------------------------------------------------- #
# Startup-script generator (base image + python3.11 + git + repo clone + units).
# Pure string assembly -- NO I/O, NO subprocess (exactly like J-Prime's
# build_startup_script). ASCII only. Mandate 1 (Root-Cause Only) + Mandate 4.
# --------------------------------------------------------------------------- #
def build_startup_script(
    repo_url: str,
    *,
    repo_ref: str = _DEFAULT_REPO_REF,
    sentinel_path: str = _SENTINEL_PATH,
    brain_env_path: str = _BRAIN_ENV_PATH,
    idle_shutdown_default_s: int = _DEFAULT_IDLE_SHUTDOWN_S,
) -> str:
    """Return the metadata startup-script that bakes the Brain VM image.

    Steps (each with an ISO phase-timestamp echo for measurable per-phase time):
      1. Install base deps: python3.11 + dev headers + build-essential + pip +
         git (NO Ollama / NO NVIDIA).
      2. Populate ``/etc/jarvis/brain.env`` FROM INSTANCE METADATA (the J-Prime
         metadata pattern) -- BEFORE any unit is enabled, so per-instance
         tokens / cost-cap / endpoints are never baked into the image.
      3. Clone the repo to ``/opt/trinity/jarvis`` (the IsomorphicEnv path).
      4. venv at ``/opt/trinity/venv`` (PEP 668) + ``pip install -r
         backend/requirements-brain.txt`` (brain-scoped, no torch/voice/ML).
      5. Write + enable the ``jarvis-brain.service`` warm-standby unit
         (headless battle-test soak, ``--max-wall-seconds 0`` = no wall) and the
         ``jarvis-brain-idle`` self-shutdown timer.
      6. READINESS PROOF: sentinel written ONLY after ``.git`` exists AND the
         transport module imports -- never a fixed sleep. Either fails -> no
         sentinel -> the reaper rolls the node (J-Prime failure contract).
    """
    repo_url_q = shlex.quote(repo_url)
    # Mandate 2 (no static keys on the snapshot): if the clone URL carries an
    # inline credential (``https://x-access-token:TOKEN@github.com/...``), the
    # token would persist in ``.git/config`` on the baked image. Compute the
    # token-free URL; the startup-script resets the remote to it right after the
    # clone, so the snapshot never carries the token. The runtime does not need
    # to pull (the image is immutable + current at bake) -- a boot-time fetch
    # that cannot auth simply no-ops.
    if "://" in repo_url and "@" in repo_url.split("://", 1)[1]:
        _scheme, _rest = repo_url.split("://", 1)
        repo_url_clean = _scheme + "://" + _rest.split("@", 1)[1]
    else:
        repo_url_clean = repo_url
    repo_url_clean_q = shlex.quote(repo_url_clean)
    repo_ref_q = shlex.quote(repo_ref)
    sentinel_q = shlex.quote(sentinel_path)
    brain_env_q = shlex.quote(brain_env_path)
    idle_default = int(idle_shutdown_default_s)
    meta_url = (
        "http://metadata.google.internal/computeMetadata/v1/instance/attributes/"
        + _BRAIN_ENV_METADATA_KEY
    )
    return f"""#!/usr/bin/env bash
# JARVIS Brain-VM golden-image bake startup-script (auto-generated).
# CPU-only orchestrator runtime: base image + python3.11 + git + repo clone at
# {_REPO_PATH}. No local model server, no hardware acceleration, no weight pull.
# The readiness sentinel is written ONLY after a REAL proof (.git exists AND the
# transport module imports) -- never a fixed sleep.
set -uo pipefail

# GCP metadata startup-scripts run as root with HOME unset; export it so any
# tool that reads $HOME (git config, pip cache) behaves.
export HOME=/root

LOG=/var/log/jarvis_brain_bake.log
exec > >(tee -a "$LOG") 2>&1
_ts() {{ date -u +%FT%T.%3NZ; }}
echo "[brain-bake] startup-script begin $(_ts) (HOME=$HOME)"

# Never leave a stale sentinel from a re-run.
rm -f {sentinel_q} || true

# 1. Install base deps (CPU-only: no model server, no hardware acceleration).
echo "[brain-bake] phase=deps-start ts=$(_ts)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3.11 python3.11-venv python3.11-dev python3-pip git curl ca-certificates build-essential
echo "[brain-bake] phase=deps-done ts=$(_ts)"

# 2. Populate {brain_env_path} FROM INSTANCE METADATA -- BEFORE any unit is
#    enabled. Per-instance tokens / cost-cap / endpoints live here, never in the
#    image (the J-Prime metadata pattern).
echo "[brain-bake] phase=brain-env-start ts=$(_ts)"
mkdir -p /etc/jarvis
if curl -fsS -H "Metadata-Flavor: Google" {shlex.quote(meta_url)} -o {brain_env_q}; then
    echo "[brain-bake] {brain_env_path} populated from instance metadata"
else
    echo "[brain-bake] WARNING: '{_BRAIN_ENV_METADATA_KEY}' metadata absent -- writing empty {brain_env_path}"
    : > {brain_env_q}
fi
echo "[brain-bake] phase=brain-env-done ts=$(_ts)"

# 3. Clone the repo to the IsomorphicEnv canonical path (the REAL primary
#    runtime). URL + ref are bake-time values (env-resolved), never literals.
echo "[brain-bake] phase=clone-start repo={repo_url_clean_q} ref={repo_ref_q} ts=$(_ts)"
mkdir -p /opt/trinity
rm -rf {_REPO_PATH}
# FULL clone (no --depth 1): a shallow clone cannot reliably `git pull
# --ff-only` at ignition -- it silently no-ops, stranding the node on the stale
# baked ref (live-fire: the A1 vector's test was absent though it was on main).
# A full clone lets the boot-time fetch/pull deterministically land current
# origin/main. Costs a little bake-time disk; worth it for a mutation-target
# repo the IsomorphicEnv + chaos injector actually exercise.
if git clone --branch {repo_ref_q} {repo_url_q} {_REPO_PATH}; then
    echo "[brain-bake] phase=clone-done ts=$(_ts)"
    # Mandate 2: scrub the clone credential so it NEVER lands on the snapshot.
    git -C {_REPO_PATH} remote set-url origin {repo_url_clean_q} 2>/dev/null || true
    rm -f /root/.git-credentials 2>/dev/null || true
    git config --global --remove-section credential 2>/dev/null || true
    echo "[brain-bake] phase=token-scrubbed remote=$(git -C {_REPO_PATH} remote get-url origin 2>/dev/null) ts=$(_ts)"
else
    echo "[brain-bake] ERROR: git clone failed -- NOT writing sentinel"
    exit 1
fi

# 4. Install the BRAIN-SCOPED requirements into a venv.
#    - venv, never system pip: Debian 12 pip is PEP 668 externally-managed and
#      refuses system-wide installs outright (live-fire attempt 1, 2026-07-04).
#    - backend/requirements-brain.txt, never the root requirements.txt: the
#      Brain is a CPU orchestrator (design Non-Goals) -- torch/voice/local-ML
#      belong to the Mac Body / J-Prime, not this image.
#    - The venv lives OUTSIDE the repo path (the clone step rm -rf's the repo).
echo "[brain-bake] phase=pip-start ts=$(_ts)"
cd {_REPO_PATH}
if ! python3.11 -m venv /opt/trinity/venv; then
    echo "[brain-bake] ERROR: venv creation failed -- NOT writing sentinel"
    exit 1
fi
/opt/trinity/venv/bin/pip install --upgrade pip || true
if /opt/trinity/venv/bin/pip install -r backend/requirements-brain.txt; then
    echo "[brain-bake] phase=pip-done ts=$(_ts)"
else
    echo "[brain-bake] ERROR: pip install failed -- NOT writing sentinel"
    exit 1
fi

# 5a. The Brain warm-standby unit: headless battle-test soak with NO wall-clock
#     cap (warm standby). cost-cap is resolved from brain.env at runtime by
#     systemd (${{JARVIS_BRAIN_COST_CAP}}) -- never a baked literal.
cat > /etc/systemd/system/jarvis-brain.service <<'UNIT'
[Unit]
Description=JARVIS Brain (Ouroboros orchestrator, headless warm standby)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={_REPO_PATH}
EnvironmentFile={brain_env_path}
# RuntimeDirectory creates /run/jarvis (tmpfs, correct perms) -- the dir the idle
# self-shutdown check reads its liveness marker from (Task-4 coupling: the brain
# unit is the toucher). ExecStartPre seeds the marker so the idle timer never
# nukes a freshly-booted node during the soak's boot window.
RuntimeDirectory=jarvis
ExecStartPre=/bin/touch /run/jarvis/brain_liveness
ExecStart=/opt/trinity/venv/bin/python scripts/ouroboros_battle_test.py --production-soak --headless --cost-cap ${{JARVIS_BRAIN_COST_CAP}} --max-wall-seconds 0
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT

# 5b. Idle self-shutdown checker + timer. If the brain process has not touched
#     its liveness marker within JARVIS_BRAIN_VM_IDLE_SHUTDOWN_S (no active WS
#     peer / no work), power the VM down so an on-demand Brain node never bills
#     while idle. Threshold is env-driven from brain.env (default {idle_default}).
#     NOTE (Task 4 coupling): the brain process must TOUCH {shlex.quote('/run/jarvis/brain_liveness')}
#     on WS-peer activity -- that harness-side wiring lands in Task 4.
cat > /opt/trinity/jarvis_brain_idle_check.sh <<'IDLE'
#!/usr/bin/env bash
set -uo pipefail
[ -f {brain_env_path} ] && . {brain_env_path}
THRESHOLD="${{JARVIS_BRAIN_VM_IDLE_SHUTDOWN_S:-{idle_default}}}"
MARKER=/run/jarvis/brain_liveness
# No marker yet -> brain still booting; never shut down mid-boot.
[ -f "$MARKER" ] || exit 0
NOW=$(date +%s)
MTIME=$(stat -c %Y "$MARKER" 2>/dev/null || echo "$NOW")
AGE=$(( NOW - MTIME ))
if [ "$AGE" -ge "$THRESHOLD" ]; then
    logger -t jarvis-brain-idle "idle ${{AGE}}s >= ${{THRESHOLD}}s -- shutting down"
    shutdown -h now
fi
IDLE
chmod +x /opt/trinity/jarvis_brain_idle_check.sh

cat > /etc/systemd/system/jarvis-brain-idle.service <<'UNIT'
[Unit]
Description=JARVIS Brain idle self-shutdown check

[Service]
Type=oneshot
EnvironmentFile={brain_env_path}
ExecStart=/opt/trinity/jarvis_brain_idle_check.sh
UNIT

cat > /etc/systemd/system/jarvis-brain-idle.timer <<'UNIT'
[Unit]
Description=Periodic JARVIS Brain idle self-shutdown check

[Timer]
OnBootSec=300
OnUnitActiveSec=120

[Install]
WantedBy=timers.target
UNIT

# 5c. Bake the ENABLED state into the image (auto-start on the real Brain VM
#     boot). We deliberately do NOT `--now` here: the paid soak must never run on
#     the ephemeral bake node.
systemctl daemon-reload || true
systemctl enable jarvis-brain.service || true
systemctl enable jarvis-brain-idle.timer || true

# 6. READINESS PROOF -> sentinel. Written ONLY after BOTH:
#      (a) {_REPO_PATH}/.git exists (a real clone landed), AND
#      (b) the transport module imports under the cloned tree.
#    Never a fixed sleep. Either fails -> no sentinel -> reaper rolls the node.
echo "[brain-bake] phase=readiness-proof-start ts=$(_ts)"
if [ -d {_REPO_PATH}/.git ] && \\
   ( cd {_REPO_PATH} && /opt/trinity/venv/bin/python -c "import backend.core.ouroboros.governance.transport" ); then
    echo "ready repo={repo_ref_q} path={_REPO_PATH} ts=$(_ts)" > {sentinel_q}
    echo "[brain-bake] phase=sentinel-written ts=$(_ts)"
    echo "[brain-bake] startup-script done $(_ts)"
else
    echo "[brain-bake] ERROR: readiness proof failed (.git or transport import) -- NOT writing sentinel"
    exit 1
fi
"""


# --------------------------------------------------------------------------- #
# gcloud command builders (pure -- dry-run prints them; _run executes).
# --------------------------------------------------------------------------- #
def _ssh_cmd(args: argparse.Namespace, node: str, remote: str) -> List[str]:
    return [
        "gcloud", "compute", "ssh", node,
        f"--project={args.project}", f"--zone={args.zone}",
        "--tunnel-through-iap", "--command", remote,
    ]


def _create_node_cmd(
    args: argparse.Namespace, node: str, startup_script_path: str
) -> List[str]:
    # ON-DEMAND (no SPOT) for bake reliability -- the one-time bake must not be
    # pre-empted mid-clone.
    return [
        "gcloud", "compute", "instances", "create", node,
        f"--project={args.project}", f"--zone={args.zone}",
        f"--machine-type={args.machine_type}",
        f"--image-family={args.source_image_family}",
        f"--image-project={args.source_image_project}",
        f"--boot-disk-size={args.boot_disk_size}",
        f"--boot-disk-type={args.boot_disk_type}",
        f"--metadata-from-file=startup-script={startup_script_path}",
    ]


def _image_exists(args: argparse.Namespace) -> bool:
    rc, _ = _run([
        "gcloud", "compute", "images", "describe", args.image_name,
        f"--project={args.project}", "--format=value(name)",
    ])
    return rc == 0


def build_awaken_one_liner(args: argparse.Namespace) -> str:
    """The `gcloud compute instances create --image-family=...` the Brain-VM
    lifecycle (Task 2/4) runs to boot a Brain node from this golden image."""
    return (
        "gcloud compute instances create jarvis-brain "
        f"--project={args.project} --zone={args.zone} "
        f"--machine-type={args.machine_type} "
        f"--image-family={args.image_family} "
        f"--image-project={args.project} "
        "--labels=jarvis-role=brain"
    )


# --------------------------------------------------------------------------- #
# Readiness poll (SSH for the sentinel; the git+transport proof already ran
# in-startup-script). Bounded by --bake-timeout-s.
# --------------------------------------------------------------------------- #
def _poll_readiness(args: argparse.Namespace, node: str) -> Tuple[bool, str]:
    deadline = time.monotonic() + float(args.bake_timeout_s)
    delay = 15.0
    attempt = 0
    check = (
        f"test -f {shlex.quote(_SENTINEL_PATH)} "
        "&& echo BRAIN_READY || echo BRAIN_NOT_READY"
    )
    tail = "sudo tail -n 5 /var/log/jarvis_brain_bake.log 2>/dev/null || true"
    while time.monotonic() < deadline:
        attempt += 1
        rc, out = _run(_ssh_cmd(args, node, check), timeout_s=90.0)
        if rc == 0 and "BRAIN_READY" in out:
            _log(f"readiness: node ready (attempt {attempt})")
            return True, ""

        # Live progress + early-fail detection off the bake log tail.
        rc2, out2 = _run(_ssh_cmd(args, node, tail), timeout_s=30.0)
        if rc2 == 0 and out2:
            lines = [l for l in out2.strip().splitlines() if l.strip()]
            if lines:
                _log(f"node progress: {lines[-1]}")
            for line in lines:
                if "ERROR" in line or "NOT writing sentinel" in line:
                    _log(f"readiness: EARLY ABORT -- {line.strip()[:200]}")
                    return False, f"early_failure: {line.strip()[:200]}"

        remaining = int(deadline - time.monotonic())
        _log(f"readiness: not ready (attempt {attempt}); {remaining}s left, "
             f"sleeping {int(delay)}s")
        if time.monotonic() + delay >= deadline:
            break
        time.sleep(delay)
        delay = min(delay * 1.5, 120.0)
    _log(f"readiness: TIMEOUT after {args.bake_timeout_s}s")
    return False, "readiness_timeout"


# --------------------------------------------------------------------------- #
# Cleanup (ALWAYS attempted once the node exists -- no orphaned billing).
# --------------------------------------------------------------------------- #
def _cleanup_node(args: argparse.Namespace, node: str) -> None:
    _log(f"cleanup: deleting bake node {node} + disk (no orphaned billing)")
    rc, out = _run([
        "gcloud", "compute", "instances", "delete", node,
        f"--project={args.project}", f"--zone={args.zone}",
        "--delete-disks=all", "--quiet",
    ], timeout_s=300.0)
    if rc == 0:
        _log(f"cleanup: node {node} + disk deleted")
    else:
        _log(f"cleanup: WARNING node delete rc={rc}: {out.strip()[:300]}")


# --------------------------------------------------------------------------- #
# Dry-run plan printer.
# --------------------------------------------------------------------------- #
def _print_plan(args: argparse.Namespace, node: str, startup_script: str) -> None:
    print("=" * 72)
    print("BRAIN-VM GOLDEN-IMAGE BAKE -- PLAN (dry-run, spends nothing)")
    print("=" * 72)
    print(f"  project        : {args.project or '(unset -- pass --project / JARVIS_GCP_PROJECT)'}")
    print(f"  zone           : {args.zone or '(unset -- pass --zone / JARVIS_BRAIN_VM_ZONES)'}")
    print(f"  machine-type   : {args.machine_type}  (CPU-only orchestrator runtime)")
    print(f"  repo url       : {args.repo_url or '(unset -- pass --repo-url / JARVIS_BRAIN_REPO_URL)'}")
    print(f"  repo ref       : {args.repo_ref}")
    print(f"  repo path      : {_REPO_PATH}  (IsomorphicEnv canonical, primary runtime)")
    print(f"  bake node      : {node}  (ON-DEMAND for bake reliability)")
    print(f"  source image   : {args.source_image_family}/{args.source_image_project}")
    print(f"  boot disk      : {args.boot_disk_size} ({args.boot_disk_type})")
    print(f"  bake timeout   : {args.bake_timeout_s}s")
    print(f"  -> image name  : {args.image_name}")
    print(f"  -> image family: {args.image_family}")
    print("-" * 72)
    print("STARTUP-SCRIPT (metadata startup-script):")
    print(startup_script)
    print("-" * 72)
    print("GCLOUD COMMANDS THAT WOULD RUN (in order):")
    print("  1. PROVISION:")
    print("     " + " ".join(shlex.quote(c) for c in
                              _create_node_cmd(args, node, "<startup-script-tmpfile>")))
    print("  2. POLL READINESS (repeated; keys on the git+transport sentinel):")
    print("     " + " ".join(shlex.quote(c) for c in
                              _ssh_cmd(args, node, "test -f " + _SENTINEL_PATH + " ...")))
    print("  3. STOP instance (consistent image):")
    print(f"     gcloud compute instances stop {node} "
          f"--project={args.project} --zone={args.zone} --quiet")
    print("  4. CREATE GOLDEN IMAGE:")
    print(f"     gcloud compute images create {args.image_name} "
          f"--project={args.project} --source-disk={node} "
          f"--source-disk-zone={args.zone} --family={args.image_family}")
    print("  5. DELETE-TO-SNAPSHOT (node + disk; image is the durable artifact):")
    print(f"     gcloud compute instances delete {node} "
          f"--project={args.project} --zone={args.zone} --delete-disks=all --quiet")
    print("-" * 72)
    print("AWAKEN ONE-LINER (used by the Brain-VM lifecycle, Task 2/4):")
    print("  " + build_awaken_one_liner(args))
    print("=" * 72)
    print("[BAKE] --dry-run: nothing executed, no money spent. Use --execute to bake.")


# --------------------------------------------------------------------------- #
# Execute pipeline.
# --------------------------------------------------------------------------- #
def _execute_bake(args: argparse.Namespace, node: str, startup_script: str) -> int:
    if not args.repo_url:
        _abort("no repo URL -- set JARVIS_BRAIN_REPO_URL or pass --repo-url")
        return 2
    if not args.project or not args.zone:
        _abort("project/zone unset -- set JARVIS_GCP_PROJECT + JARVIS_BRAIN_VM_ZONES "
               "(or pass --project/--zone)")
        return 2

    # Idempotency guard: refuse to clobber an existing image unless --force.
    if _image_exists(args):
        if not args.force:
            _abort(
                f"image '{args.image_name}' already exists -- "
                "rerun with --force to replace, or pass a new --image-name"
            )
            return 3
        _log(f"WARNING: image '{args.image_name}' exists -- --force given, "
             "will delete + recreate after a clean bake")

    import tempfile
    fd, sp_path = tempfile.mkstemp(prefix="brain_startup_", suffix=".sh")
    node_exists = False
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(startup_script)

        # 1. PROVISION (ON-DEMAND).
        _log(f"provisioning bake node {node} (ON-DEMAND, {args.machine_type})")
        rc, out = _run(_create_node_cmd(args, node, sp_path), timeout_s=300.0)
        if rc != 0:
            _abort(f"provision failed rc={rc}: {out.strip()[:400]}")
            return 4
        node_exists = True
        _log(f"node {node} created; startup-script installing deps + cloning repo")

        # 2. POLL READINESS (the git+transport sentinel).
        ready, reason = _poll_readiness(args, node)
        if not ready:
            _abort(f"readiness abort: {reason}")
            return 5

        # 3. STOP for a consistent image.
        _log(f"stopping {node} for a consistent image snapshot")
        rc, out = _run([
            "gcloud", "compute", "instances", "stop", node,
            f"--project={args.project}", f"--zone={args.zone}", "--quiet",
        ], timeout_s=300.0)
        if rc != 0:
            _abort(f"instance stop failed rc={rc}: {out.strip()[:300]}")
            return 7

        if args.force and _image_exists(args):
            _log(f"--force: deleting existing image {args.image_name}")
            _run([
                "gcloud", "compute", "images", "delete", args.image_name,
                f"--project={args.project}", "--quiet",
            ], timeout_s=300.0)

        # 4. CREATE GOLDEN IMAGE.
        _log(f"creating golden image {args.image_name} (family={args.image_family})")
        rc, out = _run([
            "gcloud", "compute", "images", "create", args.image_name,
            f"--project={args.project}", f"--source-disk={node}",
            f"--source-disk-zone={args.zone}", f"--family={args.image_family}",
        ], timeout_s=900.0)
        if rc != 0:
            _abort(f"image create failed rc={rc}: {out.strip()[:400]}")
            return 8
        _log(f"golden image created: {args.image_name}")

        # 5. DELETE-TO-SNAPSHOT happens in the finally cleanup.
        _report_success(args, node)
        return 0
    finally:
        try:
            os.unlink(sp_path)
        except OSError:
            pass
        if node_exists:
            _cleanup_node(args, node)


def _report_success(args: argparse.Namespace, node: str) -> None:
    print("=" * 72)
    print("[BAKE] SUCCESS -- Brain-VM golden image baked")
    print("=" * 72)
    print(f"  image name   : {args.image_name}")
    print(f"  image family : {args.image_family}")
    print(f"  repo runtime : {_REPO_PATH} (cloned + transport-import proven)")
    print("  readiness    : PASS (.git exists AND transport module imports)")
    print("-" * 72)
    print("  AWAKEN later (Brain-VM lifecycle, Task 2/4):")
    print("    " + build_awaken_one_liner(args))
    print("=" * 72)


# --------------------------------------------------------------------------- #
# CLI.
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "One-time bake of the CPU Brain-VM golden image (provision -> "
            "install python3.11+build-essential+git -> clone /opt/trinity/jarvis "
            "-> venv pip install backend/requirements-brain.txt -> systemd units "
            "-> git+transport readiness proof -> snapshot -> "
            "delete-to-snapshot). Default is --dry-run."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--project", default=_DEFAULT_PROJECT,
                   help="GCP project (env JARVIS_GCP_PROJECT / GCP_PROJECT)")
    p.add_argument("--zone", default=_DEFAULT_ZONE,
                   help="GCP zone (env JARVIS_BRAIN_VM_ZONES, first entry)")
    p.add_argument("--machine-type", default=_DEFAULT_MACHINE,
                   help="bake machine type (CPU-only orchestrator runtime)")
    p.add_argument("--repo-url", default=_DEFAULT_REPO_URL,
                   help="repo URL cloned to /opt/trinity/jarvis "
                        "(env JARVIS_BRAIN_REPO_URL)")
    p.add_argument("--repo-ref", default=_DEFAULT_REPO_REF,
                   help="repo ref/branch to clone (env JARVIS_BRAIN_REPO_REF)")
    p.add_argument("--image-name", default=_default_image_name(),
                   help="golden image name (env JARVIS_BRAIN_IMAGE_NAME)")
    p.add_argument("--image-family", default=_DEFAULT_IMAGE_FAMILY,
                   help="golden image family (env JARVIS_BRAIN_IMAGE_FAMILY)")
    p.add_argument("--boot-disk-size", default=_DEFAULT_BOOT_DISK,
                   help="bake node boot disk size")
    p.add_argument("--boot-disk-type", default=_DEFAULT_BOOT_DISK_TYPE,
                   help="bake node boot disk type")
    p.add_argument("--bake-timeout-s", type=int, default=_DEFAULT_BAKE_TIMEOUT_S,
                   help="readiness poll timeout (seconds)")
    p.add_argument("--source-image-family", default=_DEFAULT_DEBIAN_IMAGE_FAMILY,
                   help="base OS image family (Debian)")
    p.add_argument("--source-image-project", default=_DEFAULT_DEBIAN_IMAGE_PROJECT,
                   help="base OS image project")
    p.add_argument("--node-name", default=None,
                   help="bake node name (default jarvis-brain-bake-<stamp>)")
    p.add_argument("--force", action="store_true",
                   help="replace an existing image of the same name")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="dry_run", action="store_true",
                      help="print the plan + commands WITHOUT executing (default)")
    mode.add_argument("--execute", dest="dry_run", action="store_false",
                      help="actually run the bake (spends money)")
    p.set_defaults(dry_run=True)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    node = args.node_name or f"jarvis-brain-bake-{_now_stamp()}-{int(time.time()) % 100000}"
    startup_script = build_startup_script(args.repo_url, repo_ref=args.repo_ref)

    if args.dry_run:
        _print_plan(args, node, startup_script)
        return 0

    _log("EXECUTE mode -- this WILL provision a GCP node and spend money")
    return _execute_bake(args, node, startup_script)


if __name__ == "__main__":
    sys.exit(main())
