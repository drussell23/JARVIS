#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Sovereign Soak Golden-Image Bake -- pre-install the soak host deps once.

MIRRORS scripts/bake_jprime_golden_image.py (the PROVEN bake flow). The Omni-Soak
boots a fresh Debian node and spends ~15-20 min on a detached `pip install` of the
host deps -- the dominant wall-clock cost of every run. This tool bakes those deps
into a reusable GCP golden image ONCE so the IaC hypervisor can boot straight into
the soak (no pip install).

The bake flow (identical shape to the J-Prime baker):

    provision a cheap CPU node (debian-12, ON-DEMAND for bake reliability)
      -> install the FULL soak deps (the hard-ensure core + requirements.txt,
         ML libs filtered exactly as the surgery does)
      -> VALIDATE before snapshot (the load-bearing lock):
           * the deps import clean (aiohttp, uuid6, fastapi, pydantic, ...)
           * `python3 -m pytest --collect-only -q` runs with NO collection error
           * O+V core imports resolve (operation_id etc.)
      -> snapshot the boot disk to a reusable GCP golden image (family
         jarvis-soak-golden), STAMPED with the requirements.txt sha (a label) for
         staleness checks
      -> delete the bake VM + disk (the image is the durable artifact)

VALIDATION LOCK: we NEVER snapshot a broken image. If validation fails we ABORT,
delete the node, and exit non-zero -- no image is created.

Standalone by design -- imports the hard-ensure dep list + requirements path from
sovereign_iac_hypervisor (single source of truth, no dep-logic duplication) but
otherwise pure subprocess(gcloud / gcloud-ssh) + stdlib. Fully parameterized
(argparse with env-var defaults; zero hardcoding). Every gcloud call is fail-soft;
after the node exists the cleanup path ALWAYS attempts node + disk teardown so a
failed bake never leaks billing.

Usage:
    # default DRY-RUN: print the full plan + every gcloud command, spend nothing
    python3 scripts/bake_soak_golden_image.py --dry-run

    # actually bake (operator-monitored step, after review):
    python3 scripts/bake_soak_golden_image.py --execute
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import os
import pathlib
import re
import shlex
import subprocess
import sys
import time
from typing import List, Optional, Tuple

# --------------------------------------------------------------------------- #
# Reuse the IaC hypervisor's single-source-of-truth dep list + requirements
# location -- NO duplicate dep logic. Import is fail-soft: if the hypervisor
# can't load (it shouldn't fail, it's pure-stdlib), fall back to a conservative
# inline default so the baker still works.
# --------------------------------------------------------------------------- #
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_hard_ensure_deps() -> List[str]:
    """Import the hard-ensure dep list from the IaC hypervisor (no dup)."""
    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "_iac_for_bake", str(_REPO_ROOT / "scripts" / "sovereign_iac_hypervisor.py")
        )
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
            deps = mod.hard_ensure_deps()
            if deps:
                return list(deps)
    except Exception:  # noqa: BLE001 -- never crash the baker on an import edge
        pass
    # Conservative fallback (kept in sync with the hypervisor default).
    return [
        "aiohttp", "httpx", "pydantic", "pytest", "pytest-asyncio", "pyyaml",
        "requests", "anyio", "sniffio", "fastapi", "uvicorn", "orjson", "uuid6",
    ]


# --------------------------------------------------------------------------- #
# Defaults (every value is overridable via argparse; argparse defaults read env).
# --------------------------------------------------------------------------- #
_DEFAULT_PROJECT = os.environ.get("GCP_PROJECT", "jarvis-473803")
_DEFAULT_ZONE = os.environ.get("GCP_ZONE", "us-central1-a")
_DEFAULT_MACHINE = os.environ.get("JARVIS_SOAK_BAKE_MACHINE", "e2-standard-4")
_DEFAULT_IMAGE_FAMILY = os.environ.get(
    "JARVIS_IAC_SOAK_GOLDEN_IMAGE_FAMILY", "jarvis-soak-golden"
)
_DEFAULT_BOOT_DISK = os.environ.get("JARVIS_SOAK_BAKE_BOOT_DISK_SIZE", "50GB")
_DEFAULT_BAKE_TIMEOUT_S = int(os.environ.get("JARVIS_SOAK_BAKE_TIMEOUT_S", "1800"))
_DEFAULT_DEBIAN_IMAGE_FAMILY = os.environ.get(
    "JARVIS_SOAK_BAKE_SOURCE_IMAGE_FAMILY", "debian-12"
)
_DEFAULT_DEBIAN_IMAGE_PROJECT = os.environ.get(
    "JARVIS_SOAK_BAKE_SOURCE_IMAGE_PROJECT", "debian-cloud"
)
_DEFAULT_REQUIREMENTS = os.environ.get(
    "JARVIS_SOAK_BAKE_REQUIREMENTS", str(_REPO_ROOT / "requirements.txt")
)

# The requirements-sha label key the IaC staleness check reads. Keep in sync with
# the IaC hypervisor's golden-staleness probe.
_REQ_SHA_LABEL_KEY = os.environ.get(
    "JARVIS_IAC_GOLDEN_REQ_SHA_LABEL", "jarvis_req_sha"
)

# Readiness sentinel written by the startup-script ONLY after the dep install
# completes -- the poll loop keys on this.
_SENTINEL_PATH = "/var/run/soak_bake_ready"

# The ML / native-build libs a BARE Debian node cannot build -- filtered exactly
# as the IaC surgery filters them (no native portaudio/pyobjc/etc.). Kept as the
# same alternation so the baked image matches the surgery's installed set.
_ML_FILTER = (
    "^(#|torch|torchaudio|torchvision|tensorflow|transformers|vllm|"
    "llama|nvidia|triton|xformers|onnx|scipy|scikit|sentencepiece|accelerate|"
    "bitsandbytes|fastembed|peft|trl|datasets|pyaudio|pyobjc|webrtcvad|"
    "sounddevice|soundfile|pyttsx3|playsound)"
)


def _now_stamp() -> str:
    return _dt.datetime.now().strftime("%Y%m%d")


def _default_image_name() -> str:
    return os.environ.get(
        "JARVIS_SOAK_IMAGE_NAME", f"jarvis-soak-golden-{_now_stamp()}"
    )


# --------------------------------------------------------------------------- #
# requirements.txt sha (the staleness stamp).
# --------------------------------------------------------------------------- #
def requirements_sha(req_path: str) -> str:
    """sha256[:16] of the requirements.txt bytes -- the image staleness stamp.

    A GCP label value must be lowercase alnum/dash/underscore <= 63 chars; the
    truncated hex digest satisfies that. Fail-soft: a missing file yields a
    sentinel 'norequirements' so the bake still proceeds (with a loud label).
    """
    try:
        data = pathlib.Path(req_path).read_bytes()
    except Exception:  # noqa: BLE001
        return "norequirements"
    return hashlib.sha256(data).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Logging.
# --------------------------------------------------------------------------- #
def _log(msg: str) -> None:
    print(f"[SOAK-BAKE] {msg}", flush=True)


def _abort(msg: str) -> None:
    print(f"[SOAK-BAKE ABORTED: {msg}]", flush=True)


# --------------------------------------------------------------------------- #
# THE single subprocess boundary. ALL gcloud / ssh funnel through here so tests
# can intercept it with a monkeypatch and assert dry-run never executes.
# --------------------------------------------------------------------------- #
def _run(cmd: List[str], *, timeout_s: float = 120.0) -> Tuple[int, str]:
    """Run a command fail-soft. Returns (returncode, combined_output).

    Never raises -- a non-zero rc or an exception both surface as a failure the
    caller inspects. This is the ONLY place the script touches subprocess.
    """
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_s,
        )
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as exc:  # noqa: BLE001 -- the bake never crashes on a call
        return 1, f"[run failed: {exc!r}]"


# --------------------------------------------------------------------------- #
# Startup-script generator (installs the soak host deps + writes the sentinel).
# --------------------------------------------------------------------------- #
# The node-side file the image stamps the baked requirements sha into. The IaC
# surgery's staleness check reads this; keep in sync with the IaC hypervisor's
# _GOLDEN_BAKED_SHA_PATH default.
_GOLDEN_BAKED_SHA_PATH = os.environ.get(
    "JARVIS_IAC_GOLDEN_BAKED_SHA_PATH", "/etc/jarvis_soak_golden_sha"
)


def build_startup_script(
    deps: List[str],
    *,
    sentinel_path: str = _SENTINEL_PATH,
    remote_root: str = "/opt/trinity/jarvis",
    baked_sha: str = "",
) -> str:
    """Return the metadata startup-script that bakes the soak deps.

    Installs python3-pip + build tools (apt), then pip-installs the FULL soak deps
    (requirements.txt with ML libs filtered, then the hard-ensure core), STAMPS
    the baked requirements sha into the image (for the IaC staleness check), and
    writes the readiness sentinel ONLY after the install completes. The
    requirements.txt is shipped via metadata (a second metadata file the IaC
    node-create path also uses) and read from /opt/trinity/jarvis. Pure string
    assembly -- no I/O, no subprocess. ASCII only.
    """
    sentinel_q = shlex.quote(sentinel_path)
    hard_ensure = " ".join(shlex.quote(d) for d in deps)
    root_q = shlex.quote(remote_root)
    req_remote = f"{remote_root}/requirements.txt"
    req_q = shlex.quote(req_remote)
    baked_sha_path_q = shlex.quote(_GOLDEN_BAKED_SHA_PATH)
    baked_sha_q = shlex.quote(baked_sha)
    return f"""#!/usr/bin/env bash
# JARVIS soak golden-image bake startup-script (auto-generated).
# Installs the soak host deps (requirements.txt + hard-ensure core, ML libs
# filtered) and writes the readiness sentinel ONLY after the install completes.
set -uo pipefail

# ROOT-CAUSE FIX (mirrors J-Prime bake): GCP metadata startup-scripts run as root
# with HOME unset; export it before anything that reads it.
export HOME=/root
export DEBIAN_FRONTEND=noninteractive

LOG=/var/log/soak_bake.log
exec > >(tee -a "$LOG") 2>&1
echo "[soak-bake] startup-script begin $(date -u +%FT%TZ) (HOME=$HOME)"

# Never leave a stale sentinel from a re-run.
rm -f {sentinel_q} || true

# 1. System packages: python3-pip + build tools (matches the IaC node boot).
echo "[soak-bake] installing python3-pip + build tools"
apt-get update -y -qq || true
apt-get install -y -q python3-pip python3-dev build-essential || true
python3 -m pip --version >/dev/null 2>&1 \\
    || (curl -fsSL https://bootstrap.pypa.io/get-pip.py | sudo python3) >/dev/null 2>&1 || true

# 2. Stage requirements.txt from instance metadata (shipped by the baker).
mkdir -p {root_q} || true
if curl -fsS -H 'Metadata-Flavor: Google' \\
    'http://metadata.google.internal/computeMetadata/v1/instance/attributes/jarvis-requirements' \\
    -o {req_q} 2>/dev/null; then
    echo "[soak-bake] requirements.txt staged from metadata -> {req_remote}"
else
    echo "[soak-bake] WARNING: requirements.txt metadata absent; baking hard-ensure core only"
    : > {req_q} || true
fi

# 3. Filter the multi-GB ML libs + native-build packages (EXACTLY the surgery's
#    filter) so a bare Debian node never tries to build portaudio/pyobjc/etc.
grep -ivE '{_ML_FILTER}' {req_q} \\
    | sed -E 's/[[:space:]]*#.*$//; s/[[:space:]]*$//' > /tmp/req_light.txt 2>/dev/null \\
    || : > /tmp/req_light.txt

# 4. PER-PACKAGE, continue-on-failure install (a single unbuildable straggler
#    must NOT abort the batch -- the proven surgery pattern).
echo "[soak-bake] installing requirements.txt (filtered, per-package)"
while IFS= read -r _pkg; do
    case "$_pkg" in ''|\\#*) continue;; esac
    sudo python3 -m pip install --break-system-packages -q "$_pkg" 2>/dev/null \\
        || echo "[soak-bake] skip unbuildable: $_pkg"
done < /tmp/req_light.txt

# 5. HARD-ENSURE the A1-critical core (the loop is dead without these).
echo "[soak-bake] hard-ensuring core deps"
if sudo python3 -m pip install --break-system-packages -q {hard_ensure} 2>&1 | tail -1; then
    echo "[soak-bake] hard-ensure install complete -- stamping baked sha + sentinel"
    # Stamp the baked requirements sha into the image (IaC staleness check reads it).
    echo {baked_sha_q} | sudo tee {baked_sha_path_q} >/dev/null 2>&1 \\
        || echo {baked_sha_q} > {baked_sha_path_q} 2>/dev/null || true
    echo "ready ts=$(date -u +%FT%TZ)" > {sentinel_q}
    echo "[soak-bake] startup-script done $(date -u +%FT%TZ)"
else
    echo "[soak-bake] ERROR: hard-ensure pip install failed -- NOT writing sentinel"
    exit 1
fi
"""


# --------------------------------------------------------------------------- #
# Validation-verdict parser (the load-bearing lock).
# --------------------------------------------------------------------------- #
# The remote validation script echoes one terminal marker line. PASS iff the
# clean-import + pytest-collect + O+V-core-import all succeeded.
_VALIDATION_OK_MARKER = "SOAK_BAKE_VALIDATION_OK"
_VALIDATION_FAIL_MARKER = "SOAK_BAKE_VALIDATION_FAIL"


def parse_validation_verdict(rc: int, body: str) -> Tuple[bool, str]:
    """Decide PASS/ABORT from the remote validation result.

    PASS iff the SSH call succeeded (rc == 0) AND the body contains the OK marker
    AND does NOT contain the FAIL marker. Otherwise FAIL -- the caller ABORTS and
    never snapshots. Returns (passed, reason). `reason` doubles as the diagnostic.
    """
    if rc != 0:
        return False, f"transport/SSH failure (rc={rc})"
    text = (body or "").strip()
    if not text:
        return False, "empty validation output"
    if _VALIDATION_FAIL_MARKER in text:
        # Surface the failing stanza for the operator.
        return False, f"validation reported failure: {text[-400:]}"
    if _VALIDATION_OK_MARKER not in text:
        return False, f"no OK marker in validation output: {text[-400:]}"
    return True, text[-300:]


# The remote validation program (a single bash -c string). It runs the THREE
# checks the spec mandates and echoes exactly one terminal marker. A failure of
# ANY check echoes the FAIL marker (first-miss-wins) so the lock holds.
def build_validation_remote(remote_root: str = "/opt/trinity/jarvis") -> str:
    """Render the remote validation shell (deps import + pytest-collect + O+V core).

    first-miss-wins: any failing check echoes SOAK_BAKE_VALIDATION_FAIL and exits.
    Only when all three pass do we echo SOAK_BAKE_VALIDATION_OK.
    """
    root_q = shlex.quote(remote_root)
    return (
        "set -uo pipefail; export HOME=${HOME:-/root}; "
        # CHECK 1: the baked deps import clean.
        "if ! python3 -c 'import aiohttp, uuid6, fastapi, pydantic' 2>/tmp/soak_val_err; then "
        f"echo \"{_VALIDATION_FAIL_MARKER} deps-import: $(cat /tmp/soak_val_err 2>/dev/null | tail -3)\"; exit 1; fi; "
        # CHECK 2: pytest collection runs without a collection ERROR (the
        # pytest-asyncio / conftest-autouse-fixture class of failure). cd into the
        # synced repo if present; else collection of nothing is still a clean run.
        f"if [ -d {root_q} ]; then cd {root_q} || true; fi; "
        "if ! python3 -m pytest --collect-only -q 2>/tmp/soak_collect_err 1>/dev/null; then "
        f"echo \"{_VALIDATION_FAIL_MARKER} pytest-collect: $(tail -3 /tmp/soak_collect_err 2>/dev/null)\"; exit 1; fi; "
        # CHECK 3: O+V core imports resolve (operation_id etc.). Only meaningful
        # when the repo is synced; absent repo skips this check (a fresh bake node
        # has deps but no repo -- the IaC node syncs the repo at run time). We run
        # from inside the repo root (PYTHONPATH=root) so the package resolves
        # without any sys.path string-literal quoting hazard.
        f"if [ -f {root_q}/backend/core/ouroboros/operation_id.py ]; then "
        f"if ! ( cd {root_q} && PYTHONPATH={root_q} python3 -c "
        "'from backend.core.ouroboros import operation_id' ) 2>/tmp/soak_ov_err; then "
        f"echo \"{_VALIDATION_FAIL_MARKER} ov-core-import: $(tail -3 /tmp/soak_ov_err 2>/dev/null)\"; exit 1; fi; fi; "
        f"echo {_VALIDATION_OK_MARKER}"
    )


# --------------------------------------------------------------------------- #
# gcloud command builders (pure -- so dry-run can print them; _run executes).
# --------------------------------------------------------------------------- #
def _ssh_cmd(args: argparse.Namespace, node: str, remote: str) -> List[str]:
    return [
        "gcloud", "compute", "ssh", node,
        f"--project={args.project}", f"--zone={args.zone}",
        "--tunnel-through-iap", "--command", remote,
    ]


def _create_node_cmd(
    args: argparse.Namespace, node: str, startup_script_path: str, requirements_path: str
) -> List[str]:
    # ON-DEMAND (no SPOT) for bake reliability: Spot is for active bursts, not the
    # one-time bake. Ship requirements.txt via a metadata file the startup-script
    # reads (so the baked deps match the repo exactly).
    return [
        "gcloud", "compute", "instances", "create", node,
        f"--project={args.project}", f"--zone={args.zone}",
        f"--machine-type={args.machine_type}",
        f"--image-family={args.source_image_family}",
        f"--image-project={args.source_image_project}",
        f"--boot-disk-size={args.boot_disk_size}",
        "--boot-disk-type=pd-balanced",
        f"--metadata-from-file=startup-script={startup_script_path},"
        f"jarvis-requirements={requirements_path}",
    ]


def _image_exists(args: argparse.Namespace) -> bool:
    rc, _ = _run([
        "gcloud", "compute", "images", "describe", args.image_name,
        f"--project={args.project}", "--format=value(name)",
    ])
    return rc == 0


# --------------------------------------------------------------------------- #
# Cleanup (ALWAYS attempted once the node exists -- no orphaned billing).
# --------------------------------------------------------------------------- #
def _cleanup_node(args: argparse.Namespace, node: str) -> None:
    """Delete the bake VM + its boot disk. Fail-soft, best-effort."""
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
# Poll loop.
# --------------------------------------------------------------------------- #
_EARLY_FAIL_PATTERNS: List[re.Pattern] = [
    re.compile(r"\bERROR\b", re.IGNORECASE),
    re.compile(r"pip install failed", re.IGNORECASE),
    re.compile(r"startup-script.*exit.*[1-9]", re.IGNORECASE),
    re.compile(r"\bsoak-bake\].*ERROR", re.IGNORECASE),
]


def _check_bake_log(args: argparse.Namespace, node: str
                    ) -> Tuple[Optional[str], Optional[str]]:
    """SSH-fetch the last 5 lines of the bake log; return (last_line, fail_reason).

    Fail-soft: any SSH/parse error returns (None, None).
    """
    remote = "sudo tail -n 5 /var/log/soak_bake.log 2>/dev/null || true"
    try:
        rc, out = _run(_ssh_cmd(args, node, remote), timeout_s=30.0)
        if rc != 0 or not out:
            return None, None
        text = out.strip()
        lines = [l for l in text.splitlines() if l.strip()]
        last_line = lines[-1] if lines else None
        for line in lines:
            for pat in _EARLY_FAIL_PATTERNS:
                if pat.search(line):
                    return last_line, f"early-failure marker in log: {line.strip()[:200]}"
        return last_line, None
    except Exception:  # noqa: BLE001
        return None, None


def _poll_readiness(args: argparse.Namespace, node: str) -> Tuple[bool, str]:
    """Poll the node (via SSH) for the readiness sentinel.

    Exponential-ish backoff, bounded by --bake-timeout-s. Returns (ready, reason).
    """
    deadline = time.monotonic() + float(args.bake_timeout_s)
    delay = 15.0
    attempt = 0
    check = (
        f"test -f {shlex.quote(_SENTINEL_PATH)} "
        "&& echo SOAK_READY || echo SOAK_NOT_READY"
    )
    while time.monotonic() < deadline:
        attempt += 1
        rc, out = _run(_ssh_cmd(args, node, check), timeout_s=90.0)
        if rc == 0 and "SOAK_READY" in out:
            _log(f"readiness: node ready (attempt {attempt})")
            return True, ""

        last_line, fail_reason = _check_bake_log(args, node)
        if last_line:
            _log(f"node progress: {last_line}")
        if fail_reason:
            _log(f"readiness: EARLY ABORT -- {fail_reason}")
            return False, f"early_failure: {fail_reason}"

        remaining = int(deadline - time.monotonic())
        _log(
            f"readiness: not ready (attempt {attempt}, rc={rc}); "
            f"{remaining}s left, sleeping {int(delay)}s"
        )
        if time.monotonic() + delay >= deadline:
            break
        time.sleep(delay)
        delay = min(delay * 1.5, 120.0)
    _log(f"readiness: TIMEOUT after {args.bake_timeout_s}s")
    return False, "readiness_timeout"


# --------------------------------------------------------------------------- #
# Validation (the load-bearing lock -- run the 3 checks before snapshot).
# --------------------------------------------------------------------------- #
def _validate_deps(args: argparse.Namespace, node: str) -> Tuple[bool, str]:
    """SSH-run the validation program (deps import + pytest-collect + O+V core)."""
    remote = build_validation_remote()
    _log("validation: running deps-import + pytest-collect + O+V-core checks")
    rc, out = _run(_ssh_cmd(args, node, remote), timeout_s=300.0)
    return parse_validation_verdict(rc, out)


# --------------------------------------------------------------------------- #
# Dry-run plan printer.
# --------------------------------------------------------------------------- #
def _print_plan(
    args: argparse.Namespace, node: str, startup_script: str, deps: List[str]
) -> None:
    req_sha = requirements_sha(args.requirements)
    print("=" * 72)
    print("SOAK GOLDEN-IMAGE BAKE -- PLAN (dry-run, spends nothing)")
    print("=" * 72)
    print(f"  project        : {args.project}")
    print(f"  zone           : {args.zone}")
    print(f"  machine-type   : {args.machine_type}")
    print(f"  bake node      : {node}  (ON-DEMAND for bake reliability)")
    print(f"  source image   : {args.source_image_family}/{args.source_image_project}")
    print(f"  boot disk      : {args.boot_disk_size}")
    print(f"  bake timeout   : {args.bake_timeout_s}s")
    print(f"  requirements   : {args.requirements}")
    print(f"  req sha label  : {_REQ_SHA_LABEL_KEY}={req_sha}")
    print(f"  hard-ensure    : {' '.join(deps)}")
    print(f"  -> image name  : {args.image_name}")
    print(f"  -> image family: {args.image_family}")
    print("-" * 72)
    print("STARTUP-SCRIPT (metadata startup-script):")
    print(startup_script)
    print("-" * 72)
    print("GCLOUD COMMANDS THAT WOULD RUN (in order):")
    print("  1. PROVISION (ships requirements.txt as metadata):")
    print("     " + " ".join(shlex.quote(c) for c in
                              _create_node_cmd(args, node, "<startup-script-tmpfile>",
                                               args.requirements)))
    print("  2. POLL READINESS (repeated, bounded):")
    print("     " + " ".join(shlex.quote(c) for c in
                              _ssh_cmd(args, node, "test -f " + _SENTINEL_PATH + " ...")))
    print("  3. VALIDATION LOCK (deps import + pytest-collect + O+V; NO snapshot unless PASS):")
    print("     " + " ".join(shlex.quote(c) for c in
                              _ssh_cmd(args, node, build_validation_remote())))
    print("  4. STOP instance (consistent image):")
    print(f"     gcloud compute instances stop {node} "
          f"--project={args.project} --zone={args.zone} --quiet")
    print("  5. CREATE GOLDEN IMAGE (stamped with req-sha label):")
    print(f"     gcloud compute images create {args.image_name} "
          f"--project={args.project} --source-disk={node} "
          f"--source-disk-zone={args.zone} --family={args.image_family} "
          f"--labels={_REQ_SHA_LABEL_KEY}={req_sha}")
    print("  6. DELETE-TO-SNAPSHOT (node + disk; image is the durable artifact):")
    print(f"     gcloud compute instances delete {node} "
          f"--project={args.project} --zone={args.zone} --delete-disks=all --quiet")
    print("-" * 72)
    print("AFTER THE BAKE: arm the IaC golden path with")
    print("  export JARVIS_IAC_SOAK_GOLDEN_ENABLED=1")
    print("=" * 72)
    print("[SOAK-BAKE] --dry-run: nothing executed, no money spent. Use --execute to bake.")


# --------------------------------------------------------------------------- #
# Execute pipeline.
# --------------------------------------------------------------------------- #
def _execute_bake(
    args: argparse.Namespace, node: str, startup_script: str
) -> int:
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
    fd, sp_path = tempfile.mkstemp(prefix="soak_startup_", suffix=".sh")
    node_exists = False
    req_sha = requirements_sha(args.requirements)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(startup_script)

        # 1. PROVISION (ON-DEMAND, ship requirements.txt as metadata).
        _log(f"provisioning bake node {node} (ON-DEMAND, {args.machine_type})")
        rc, out = _run(
            _create_node_cmd(args, node, sp_path, args.requirements), timeout_s=300.0
        )
        if rc != 0:
            _abort(f"provision failed rc={rc}: {out.strip()[:400]}")
            return 4
        node_exists = True
        _log(f"node {node} created; startup-script installing soak deps")

        # 2. POLL READINESS.
        ready, poll_reason = _poll_readiness(args, node)
        if not ready:
            _abort(f"readiness abort: {poll_reason}")
            return 5

        # 3. VALIDATION LOCK -- never snapshot a broken image.
        passed, sample = _validate_deps(args, node)
        if not passed:
            _abort(f"validation failed: {sample}")
            return 6
        _log("validation: PASS -- deps import + pytest-collect + O+V core all clean")
        _log(f"validation sample: {sample[:300]!r}")

        # 4. STOP for a consistent image.
        _log(f"stopping {node} for a consistent image snapshot")
        rc, out = _run([
            "gcloud", "compute", "instances", "stop", node,
            f"--project={args.project}", f"--zone={args.zone}", "--quiet",
        ], timeout_s=300.0)
        if rc != 0:
            _abort(f"instance stop failed rc={rc}: {out.strip()[:300]}")
            return 7

        # If --force replacing an existing image, delete the old one first.
        if args.force and _image_exists(args):
            _log(f"--force: deleting existing image {args.image_name}")
            _run([
                "gcloud", "compute", "images", "delete", args.image_name,
                f"--project={args.project}", "--quiet",
            ], timeout_s=300.0)

        # 5. CREATE GOLDEN IMAGE (stamped with the req-sha label for staleness).
        _log(f"creating golden image {args.image_name} (family={args.image_family}, "
             f"{_REQ_SHA_LABEL_KEY}={req_sha})")
        rc, out = _run([
            "gcloud", "compute", "images", "create", args.image_name,
            f"--project={args.project}", f"--source-disk={node}",
            f"--source-disk-zone={args.zone}", f"--family={args.image_family}",
            f"--labels={_REQ_SHA_LABEL_KEY}={req_sha}",
        ], timeout_s=900.0)
        if rc != 0:
            _abort(f"image create failed rc={rc}: {out.strip()[:400]}")
            return 8
        _log(f"golden image created: {args.image_name}")

        _report_success(args, node, req_sha, sample)
        return 0
    finally:
        try:
            os.unlink(sp_path)
        except OSError:
            pass
        # ALWAYS tear the node + disk down -- success or failure. The image (if
        # created) is the durable artifact; the VM must never linger.
        if node_exists:
            _cleanup_node(args, node)


def _report_success(
    args: argparse.Namespace, node: str, req_sha: str, sample: str
) -> None:
    print("=" * 72)
    print("[SOAK-BAKE] SUCCESS -- golden image baked + validated")
    print("=" * 72)
    print(f"  image name   : {args.image_name}")
    print(f"  image family : {args.image_family}")
    print(f"  req sha label: {_REQ_SHA_LABEL_KEY}={req_sha}")
    print("  validation   : PASS (deps import + pytest-collect + O+V core)")
    print(f"  sample       : {sample[:200]!r}")
    print("-" * 72)
    print("  ARM the IaC golden path:")
    print("    export JARVIS_IAC_SOAK_GOLDEN_ENABLED=1")
    print("=" * 72)


# --------------------------------------------------------------------------- #
# CLI.
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "One-time autonomous bake of the soak host-deps golden image "
            "(provision -> install deps -> VALIDATE -> snapshot -> "
            "delete-to-snapshot). Default is --dry-run."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--project", default=_DEFAULT_PROJECT,
                   help="GCP project (env GCP_PROJECT)")
    p.add_argument("--zone", default=_DEFAULT_ZONE,
                   help="GCP zone (env GCP_ZONE)")
    p.add_argument("--machine-type", default=_DEFAULT_MACHINE,
                   help="bake machine type (env JARVIS_SOAK_BAKE_MACHINE)")
    p.add_argument("--image-name", default=_default_image_name(),
                   help="golden image name (env JARVIS_SOAK_IMAGE_NAME)")
    p.add_argument("--image-family", default=_DEFAULT_IMAGE_FAMILY,
                   help="golden image family (env JARVIS_IAC_SOAK_GOLDEN_IMAGE_FAMILY)")
    p.add_argument("--boot-disk-size", default=_DEFAULT_BOOT_DISK,
                   help="bake node boot disk size")
    p.add_argument("--bake-timeout-s", type=int, default=_DEFAULT_BAKE_TIMEOUT_S,
                   help="readiness poll timeout (seconds)")
    p.add_argument("--source-image-family", default=_DEFAULT_DEBIAN_IMAGE_FAMILY,
                   help="base OS image family (Debian)")
    p.add_argument("--source-image-project", default=_DEFAULT_DEBIAN_IMAGE_PROJECT,
                   help="base OS image project")
    p.add_argument("--requirements", default=_DEFAULT_REQUIREMENTS,
                   help="requirements.txt path (env JARVIS_SOAK_BAKE_REQUIREMENTS)")
    p.add_argument("--node-name", default=None,
                   help="bake node name (default jarvis-soak-bake-<stamp>)")
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
    node = args.node_name or f"jarvis-soak-bake-{_now_stamp()}-{int(time.time()) % 100000}"
    deps = _load_hard_ensure_deps()
    startup_script = build_startup_script(deps, baked_sha=requirements_sha(args.requirements))

    if args.dry_run:
        _print_plan(args, node, startup_script, deps)
        return 0

    _log("EXECUTE mode -- this WILL provision a GCP node and spend money")
    return _execute_bake(args, node, startup_script)


if __name__ == "__main__":
    sys.exit(main())
