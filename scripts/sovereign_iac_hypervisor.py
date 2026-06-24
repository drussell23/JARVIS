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
import datetime as _dt
import os
import pathlib
import re
import shlex
import subprocess
import sys
import time
from typing import Callable, List, Optional, Tuple

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

# Completion-sentinel the surgery writes -- the node-side dead-man fires
# IMMEDIATELY when it appears (don't wait the idle timeout).
_COMPLETION_SENTINEL = os.environ.get(
    "JARVIS_IAC_COMPLETION_SENTINEL", "/var/run/sovereign_surgery_complete"
)

# Local autopsy output dir.
_AUTOPSY_DIR = os.environ.get("JARVIS_IAC_AUTOPSY_DIR", "autopsy_reports")

# Readiness sentinel written by the node startup-script once Docker is up.
_READY_SENTINEL = "/var/run/sovereign_iac_ready"

# Verdict markers emitted by the remote surgery (mirrors cross_repo_first_surgery).
_VERDICT_PASS = "VERDICT: PASS"
_VERDICT_FRACTURE = "SOVEREIGN YIELD: CROSS-REPO FRACTURE"


# --------------------------------------------------------------------------- #
# Logging.
# --------------------------------------------------------------------------- #
def _log(msg: str) -> None:
    print(f"[IAC] {msg}", flush=True)


def _abort(msg: str) -> None:
    print(f"[IAC ABORTED: {msg}]", flush=True)


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
            bufsize=1,  # line-buffered
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
if [ -f "${ACTIVITY_FILE}" ]; then
    FILE_AGE_S=$(( $(date +%s) - $(stat -c %Y "${ACTIVITY_FILE}" 2>/dev/null || echo 0) ))
    if [ "${FILE_AGE_S}" -lt "${IDLE_TIMEOUT_S}" ]; then
        echo "[sovereign-burn] activity recent (age=${FILE_AGE_S}s < ${IDLE_TIMEOUT_S}s) -- no action"
        exit 0
    fi
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
def _create_node_cmd(
    args: argparse.Namespace, node: str, startup_script_path: str
) -> List[str]:
    """e2-standard-8 (32GB) SPOT node, DELETE-on-preempt, max_run_duration,
    cloud-platform scope (the dead-man needs it)."""
    return [
        "gcloud", "compute", "instances", "create", node,
        f"--project={args.project}", f"--zone={args.zone}",
        f"--machine-type={args.machine_type}",
        f"--image-family={args.source_image_family}",
        f"--image-project={args.source_image_project}",
        f"--boot-disk-size={args.boot_disk_size}",
        "--boot-disk-type=pd-balanced",
        "--provisioning-model=SPOT",
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
        "rsync", "-az", "--delete",
        *exclude_flags,
        "-e", ssh_transport,
        local_dir.rstrip("/") + "/",
        f"{node}:{remote_dir}",
    ]


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


# --------------------------------------------------------------------------- #
# Phase 1: Cloud Projector (provision).
# --------------------------------------------------------------------------- #
def provision_sandbox_node(
    args: argparse.Namespace, node: str, startup_script: str
) -> Tuple[bool, str]:
    """Create the e2-standard-8 Spot node with the burn startup-script.

    Returns (ok, detail). Fail-soft.
    """
    import tempfile

    fd, sp_path = tempfile.mkstemp(prefix="sovereign_iac_startup_", suffix=".sh")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(startup_script)
        _log(f"provisioning {node} (e2-standard-8 SPOT, 32GB, DELETE-on-preempt)")
        rc, out = _run(_create_node_cmd(args, node, sp_path), timeout_s=300.0)
        if rc != 0:
            return False, f"provision failed rc={rc}: {out.strip()[:400]}"
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
    args: argparse.Namespace, node: str, excludes: List[str]
) -> Tuple[bool, str]:
    """rsync/scp the 3 repos into /opt/trinity/{jarvis,prime,reactor}. Excludes
    .git/__pycache__/node_modules/.venv/etc -- keep the beam lean. Bounded.

    Transport selected by JARVIS_IAC_SYNC_TRANSPORT (default scp -- robust over
    IAP). Returns (ok, detail). Fail-soft.
    """
    transport = os.environ.get("JARVIS_IAC_SYNC_TRANSPORT", "scp").strip().lower()
    pairs = _resolve_repo_paths(args)
    for name, local in pairs:
        if not local:
            return False, f"repo path for '{name}' is unset (set JARVIS_{name.upper()}_REPO_PATH)"
        remote_dir = f"{_REMOTE_TRINITY_ROOT}/{name}"
        if transport == "rsync":
            cmd = _rsync_cmd(args, node, local, remote_dir, excludes)
        else:
            cmd = _scp_cmd(args, node, local, remote_dir, excludes)
        _log(f"syncing {name}: {local} -> {node}:{remote_dir} (transport={transport})")
        rc, out = _run(cmd, timeout_s=float(args.sync_timeout_s))
        if rc != 0:
            return False, f"sync of '{name}' failed rc={rc}: {out.strip()[:300]}"
    return True, "synced"


# --------------------------------------------------------------------------- #
# Phase 3: Remote Execution & Terminal Tunnelling (streamed).
# --------------------------------------------------------------------------- #
def _remote_surgery_shell(args: argparse.Namespace) -> str:
    """The remote shell command: set the trinity env so PRIME/REACTOR repo paths
    point at the synced /opt/trinity/*, enable prebake + cross-repo mutation +
    chaos, run the surgery, then ALWAYS touch the completion-sentinel (the burn
    fires immediately on surgery-done)."""
    jr = f"{_REMOTE_TRINITY_ROOT}/jarvis"
    pr = f"{_REMOTE_TRINITY_ROOT}/prime"
    rr = f"{_REMOTE_TRINITY_ROOT}/reactor"
    env = (
        f"export JARVIS_PRIME_REPO_PATH={shlex.quote(pr)}; "
        f"export JARVIS_REACTOR_REPO_PATH={shlex.quote(rr)}; "
        "export JARVIS_TRINITY_PREBAKE_ENABLED=1; "
        "export JARVIS_CROSS_REPO_MUTATION_ENABLED=1; "
        "export JARVIS_CHAOS_INJECTOR_ENABLED=1; "
    )
    # cd into the synced jarvis repo, run the surgery, ALWAYS drop the sentinel.
    return (
        f"cd {shlex.quote(jr)} && {env} "
        f"({args.surgery_cmd}); rc=$?; "
        f"sudo touch {shlex.quote(args.completion_sentinel)} 2>/dev/null "
        f"|| touch {shlex.quote(args.completion_sentinel)} 2>/dev/null || true; "
        "exit $rc"
    )


def parse_verdict(captured: List[str]) -> str:
    """Decide PASS / FRACTURE / UNKNOWN from the captured surgery output."""
    blob = "".join(captured)
    if _VERDICT_FRACTURE in blob:
        return "FRACTURE"
    if _VERDICT_PASS in blob:
        return "PASS"
    return "UNKNOWN"


def run_remote_surgery(
    args: argparse.Namespace, node: str
) -> Tuple[int, List[str], str]:
    """SSH-exec the Trinity surgery remotely, STREAMING stdout/stderr to the local
    terminal in real-time. Returns (rc, captured_lines, verdict). Fail-soft.

    The WAN prebake happens ON the node before the air-gap compose -- the surgery
    command drives the full prebake -> air-gap flow locally on the node.
    """
    remote = _remote_surgery_shell(args)
    cmd = _ssh_cmd(args, node, remote)
    _log("running remote surgery (streaming to local terminal in real-time)...")
    rc, captured = _run_streaming(cmd, timeout_s=float(args.surgery_timeout_s))
    verdict = parse_verdict(captured)
    _log(f"remote surgery finished rc={rc} verdict={verdict}")
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


# --------------------------------------------------------------------------- #
# Execute pipeline.
# --------------------------------------------------------------------------- #
def _execute(args: argparse.Namespace, node: str, startup_script: str, excludes: List[str]) -> int:
    node_exists = False
    verdict = "UNKNOWN"
    surgery_output: List[str] = []
    surgery_rc = 1
    abort_reason = ""
    succeeded = False
    try:
        # PHASE 1: PROVISION.
        ok, detail = provision_sandbox_node(args, node, startup_script)
        if not ok:
            _abort(detail)
            abort_reason = detail
            return 4
        node_exists = True
        _log(f"node {node} created; startup-script installing Docker + burn watchdog")

        # PHASE 1b: POLL READY.
        ready, reason = poll_node_ready(args, node)
        if not ready:
            _abort(f"readiness abort: {reason}")
            abort_reason = reason
            return 5

        # PHASE 2: SYNC BRIDGE.
        ok, detail = sync_repos_to_node(args, node, excludes)
        if not ok:
            _abort(f"sync abort: {detail}")
            abort_reason = detail
            return 6

        # PHASE 3: REMOTE SURGERY (streamed).
        surgery_rc, surgery_output, verdict = run_remote_surgery(args, node)
        succeeded = True  # we reached the verdict; PASS or FRACTURE are both terminal
        return 0 if verdict in ("PASS", "FRACTURE") else 7
    finally:
        # PHASE 4: THE ULTIMATE BURN -- ALWAYS runs (PASS / FRACTURE / exception
        # / SSH-drop). The burn is the #1 invariant: no orphaned 32GB node under
        # ANY exit. Autopsy-before-burn on failure (NEVER blocks the burn).
        if node_exists:
            if not succeeded or verdict not in ("PASS", "FRACTURE"):
                run_autopsy(args, node, abort_reason or f"verdict={verdict}", surgery_output)
            burn_node(args, node)
            verify_node_gone(args, node)
        _report(args, node, verdict, surgery_rc, node_exists)


def _report(args: argparse.Namespace, node: str, verdict: str, surgery_rc: int, node_existed: bool) -> None:
    print("=" * 72)
    print("[IAC] SESSION REPORT")
    print("=" * 72)
    print(f"  node       : {node} ({'provisioned + burned' if node_existed else 'never provisioned'})")
    print(f"  verdict    : {verdict}")
    print(f"  surgery rc : {surgery_rc}")
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
    p.add_argument("--max-run-duration-s", type=int, default=_DEFAULT_MAX_RUN_DURATION_S,
                   help="GCP hard ceiling (env JARVIS_IAC_MAX_RUN_DURATION_S)")
    p.add_argument("--ready-timeout-s", type=int, default=_DEFAULT_READY_TIMEOUT_S)
    p.add_argument("--sync-timeout-s", type=int, default=int(os.environ.get("JARVIS_IAC_SYNC_TIMEOUT_S", "900")))
    p.add_argument("--surgery-timeout-s", type=int, default=int(os.environ.get("JARVIS_IAC_SURGERY_TIMEOUT_S", "2400")))
    p.add_argument("--surgery-cmd", default=_DEFAULT_SURGERY_CMD, help="remote surgery command (env JARVIS_IAC_SURGERY_CMD)")
    p.add_argument("--completion-sentinel", default=_COMPLETION_SENTINEL)
    p.add_argument("--prime-repo-path", default=None, help="prime repo (env JARVIS_PRIME_REPO_PATH)")
    p.add_argument("--reactor-repo-path", default=None, help="reactor repo (env JARVIS_REACTOR_REPO_PATH)")
    p.add_argument("--rsync-excludes", default=_DEFAULT_RSYNC_EXCLUDES,
                   help="comma-separated excludes (env JARVIS_IAC_RSYNC_EXCLUDES)")
    p.add_argument("--node-name", default=None, help="node name (default sovereign-sandbox-<ts>)")
    p.add_argument("--i-understand-this-spends-money", action="store_true",
                   help="REAL-MONEY safety gate (required with --execute)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="dry_run", action="store_true",
                      help="print the plan + commands WITHOUT executing (default)")
    mode.add_argument("--execute", dest="dry_run", action="store_false",
                      help="actually run (spends money; needs the triple gate)")
    p.set_defaults(dry_run=True)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    stamp = _now_stamp()
    node = args.node_name or default_node_name(stamp)
    excludes = parse_excludes(args.rsync_excludes)
    startup_script = build_startup_script(completion_sentinel=args.completion_sentinel)

    if args.dry_run:
        if not _master_enabled():
            _log(f"NOTE: master gate {_ENV_MASTER} is OFF (dry-run prints the plan anyway).")
        _print_plan(args, node, startup_script, excludes)
        return 0

    ok, reason = check_triple_gate(args)
    if not ok:
        _abort(f"triple-gate REFUSED: {reason}")
        _log("required: JARVIS_IAC_HYPERVISOR_ENABLED=1 + --execute + --i-understand-this-spends-money")
        return 2

    _log("EXECUTE mode -- this WILL provision a 32GB GCP node and spend money")
    return _execute(args, node, startup_script, excludes)


if __name__ == "__main__":
    sys.exit(main())
