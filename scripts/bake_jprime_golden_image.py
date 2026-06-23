#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Sovereign J-Prime Golden-Image Bake -- one-time autonomous code-only image bake.

Phase 1 of the Sovereign Provider Failover Lifecycle
(spec: docs/superpowers/specs/2026-06-23-sovereign-provider-failover-lifecycle.md
SS5/SS8/SS11). J-Prime is the last-resort code generator that AWAKENS when
DoubleWord collapses (global outage) AND Claude is unavailable. To make the
awaken path cheap, J-Prime lives DORMANT as a reusable GCP golden image
(VM + disk deleted; image-only ~$0.50/mo) and is recreated on demand.

This tool performs the ONE-TIME bake:

    provision a cheap CPU node (ON-DEMAND for bake reliability)
      -> install the Ollama serving runtime
      -> pull the code model (qwen2.5-coder:7b) into RAM-backed serving
      -> VALIDATE it actually generates Python code (the load-bearing lock)
      -> snapshot the boot disk to a reusable GCP golden image
      -> delete the bake VM + disk (the image is the durable artifact)

VALIDATION LOCK: we NEVER snapshot a broken image. Before any image is created
we run a real generation against the node's Ollama endpoint and require HTTP 200
+ a non-empty body containing plausible Python ('def '). On failure we ABORT,
delete the node, and exit non-zero -- no image is created.

NOTE: e2-highmem-2 is a CPU-only machine. The model is loaded in RAM and
generates on CPU. There is NO GPU / NO VRAM here -- messages say
"loaded in RAM + generating", never "VRAM".

Standalone by design -- imports NOTHING from the JARVIS core. Pure
subprocess(gcloud / gcloud-ssh) + stdlib. Fully parameterized (argparse with
env-var defaults; zero hardcoding). Every gcloud call is fail-soft; after the
node exists, the cleanup path ALWAYS attempts node + disk teardown so a failed
bake never leaks billing.

Usage:
    # default DRY-RUN: print the full plan + every gcloud command, spend nothing
    python3 scripts/bake_jprime_golden_image.py --dry-run

    # actually bake (operator-monitored step, after review):
    python3 scripts/bake_jprime_golden_image.py --execute
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
import time
from typing import List, Optional, Tuple

# --------------------------------------------------------------------------- #
# Defaults (every value is overridable via argparse; argparse defaults read env).
# --------------------------------------------------------------------------- #
_DEFAULT_PROJECT = os.environ.get("GCP_PROJECT", "jarvis-473803")
_DEFAULT_ZONE = os.environ.get("GCP_ZONE", "us-central1-a")
_DEFAULT_MACHINE = os.environ.get("JPRIME_BAKE_MACHINE", "e2-highmem-2")
_DEFAULT_MODEL = os.environ.get("JPRIME_BAKE_MODEL", "qwen2.5-coder:7b")
_DEFAULT_IMAGE_FAMILY = os.environ.get("JPRIME_IMAGE_FAMILY", "jarvis-prime-coder")
_DEFAULT_BOOT_DISK = os.environ.get("JPRIME_BAKE_BOOT_DISK_SIZE", "30GB")
_DEFAULT_BAKE_TIMEOUT_S = int(os.environ.get("JPRIME_BAKE_TIMEOUT_S", "1800"))
_DEFAULT_DEBIAN_IMAGE_FAMILY = os.environ.get(
    "JPRIME_BAKE_SOURCE_IMAGE_FAMILY", "debian-12"
)
_DEFAULT_DEBIAN_IMAGE_PROJECT = os.environ.get(
    "JPRIME_BAKE_SOURCE_IMAGE_PROJECT", "debian-cloud"
)

# Readiness sentinel written by the startup-script ONLY after the model pull
# completes -- the poll loop keys on this.
_SENTINEL_PATH = "/var/run/jprime_bake_ready"
_OLLAMA_PORT = 11434
_VALIDATION_PROMPT = (
    "Write a Python function is_even(n) that returns True if n is even."
)


def _now_stamp() -> str:
    return _dt.datetime.now().strftime("%Y%m%d")


def _default_image_name() -> str:
    return os.environ.get(
        "JPRIME_IMAGE_NAME", f"jarvis-prime-coder-golden-{_now_stamp()}"
    )


# --------------------------------------------------------------------------- #
# Logging.
# --------------------------------------------------------------------------- #
def _log(msg: str) -> None:
    print(f"[BAKE] {msg}", flush=True)


def _abort(msg: str) -> None:
    print(f"[BAKE ABORTED: {msg}]", flush=True)


# --------------------------------------------------------------------------- #
# THE single subprocess boundary. ALL gcloud / ssh funnel through here so tests
# can intercept it with a monkeypatch and assert dry-run never executes.
# --------------------------------------------------------------------------- #
def _run(cmd: List[str], *, timeout_s: float = 120.0) -> Tuple[int, str]:
    """Run a command fail-soft. Returns (returncode, combined_output).

    Never raises -- a non-zero rc or an exception both surface as a failure
    the caller inspects. This is the ONLY place the script touches subprocess.
    """
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_s,
        )
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as exc:  # noqa: BLE001 -- the bake never crashes on a call
        return 1, f"[run failed: {exc!r}]"


# --------------------------------------------------------------------------- #
# Startup-script generator (installs Ollama, serves, pulls model, sentinels).
# --------------------------------------------------------------------------- #
def build_startup_script(model: str, *, sentinel_path: str = _SENTINEL_PATH) -> str:
    """Return the metadata startup-script that bakes the node.

    Installs Ollama, runs `ollama serve` (systemd if available, nohup fallback),
    `ollama pull <model>`, and writes the readiness sentinel ONLY after the pull
    completes. Pure string assembly -- no I/O, no subprocess. ASCII only.
    """
    model_q = shlex.quote(model)
    sentinel_q = shlex.quote(sentinel_path)
    return f"""#!/usr/bin/env bash
# JARVIS J-Prime golden-image bake startup-script (auto-generated).
# Installs the Ollama serving runtime, pulls the code model into RAM-backed
# serving, and writes the readiness sentinel ONLY after the pull completes.
set -uo pipefail

LOG=/var/log/jprime_bake.log
exec > >(tee -a "$LOG") 2>&1
echo "[jprime-bake] startup-script begin $(date -u +%FT%TZ)"

# Never leave a stale sentinel from a re-run.
rm -f {sentinel_q} || true

# 1. Install the Ollama serving runtime.
echo "[jprime-bake] installing Ollama serving runtime"
curl -fsSL https://ollama.com/install.sh | sh

# 2. Serve. Prefer the systemd unit the installer ships; fall back to nohup.
if systemctl list-unit-files 2>/dev/null | grep -q '^ollama.service'; then
    echo "[jprime-bake] starting ollama via systemd"
    systemctl enable ollama || true
    systemctl restart ollama || true
else
    echo "[jprime-bake] systemd unit absent -- starting ollama via nohup"
    nohup ollama serve > /var/log/ollama_serve.log 2>&1 &
fi

# 3. Wait for the Ollama HTTP endpoint to answer before pulling.
echo "[jprime-bake] waiting for ollama endpoint on :{_OLLAMA_PORT}"
for i in $(seq 1 60); do
    if curl -fsS "http://localhost:{_OLLAMA_PORT}/api/tags" >/dev/null 2>&1; then
        echo "[jprime-bake] ollama endpoint is up"
        break
    fi
    sleep 5
done

# 4. Pull the code model (loaded in RAM + generating on CPU -- no GPU here).
echo "[jprime-bake] pulling model {model_q}"
if ollama pull {model_q}; then
    echo "[jprime-bake] model pull complete -- writing readiness sentinel"
    # Sentinel written ONLY after a successful pull.
    echo "ready model={model} ts=$(date -u +%FT%TZ)" > {sentinel_q}
    echo "[jprime-bake] startup-script done $(date -u +%FT%TZ)"
else
    echo "[jprime-bake] ERROR: ollama pull failed -- NOT writing sentinel"
    exit 1
fi
"""


# --------------------------------------------------------------------------- #
# Validation-verdict parser (the load-bearing lock).
# --------------------------------------------------------------------------- #
def parse_validation_verdict(
    rc: int, body: str
) -> Tuple[bool, str]:
    """Decide PASS/ABORT from an SSH-curl generation result.

    PASS iff the call succeeded (rc == 0), the body is non-empty, and it
    contains plausible Python ('def '). Otherwise FAIL -- the caller ABORTS
    and never snapshots.

    The SSH transport returns rc==0 on a clean curl with HTTP 200 (we use
    `curl --fail` upstream so a non-200 sets rc != 0). The body is the raw
    Ollama JSON; we extract the generated text (`response` or chat `content`)
    and look for `def `.

    Returns (passed, reason). `reason` doubles as the sample/diagnostic.
    """
    if rc != 0:
        return False, f"transport/HTTP failure (rc={rc})"
    text = (body or "").strip()
    if not text:
        return False, "empty response body"

    generated = _extract_generated_text(text)
    if not generated:
        return False, "no generated text in response body"
    if "def " not in generated:
        return False, f"no plausible Python ('def ' absent) in: {generated[:200]!r}"
    return True, generated.strip()


def _extract_generated_text(body: str) -> str:
    """Pull the generated text out of an Ollama JSON body, fail-soft.

    Handles /api/generate ({"response": "..."}) and the OpenAI-compat
    /v1/chat/completions ({"choices":[{"message":{"content":"..."}}]}).
    Falls back to the raw body if it isn't JSON (so a literal `def ` in plain
    text still counts as a generation).
    """
    try:
        obj = json.loads(body)
    except Exception:  # noqa: BLE001
        return body  # non-JSON: treat the raw text as the generation
    if isinstance(obj, dict):
        if isinstance(obj.get("response"), str):
            return obj["response"]
        choices = obj.get("choices")
        if isinstance(choices, list) and choices:
            msg = choices[0].get("message") if isinstance(choices[0], dict) else None
            if isinstance(msg, dict) and isinstance(msg.get("content"), str):
                return msg["content"]
            # legacy completion shape
            if isinstance(choices[0], dict) and isinstance(
                choices[0].get("text"), str
            ):
                return choices[0]["text"]
    return body


# --------------------------------------------------------------------------- #
# Awaken one-liner (what the lifecycle controller runs later to recreate).
# --------------------------------------------------------------------------- #
def build_awaken_one_liner(args: argparse.Namespace) -> str:
    """The exact `gcloud compute instances create --image-family=...` the
    failover lifecycle controller will run to AWAKEN J-Prime from this image."""
    return (
        "gcloud compute instances create jarvis-prime-failover "
        f"--project={args.project} --zone={args.zone} "
        f"--machine-type={args.machine_type} "
        f"--image-family={args.image_family} "
        f"--image-project={args.project} "
        "--provisioning-model=SPOT "
        "--instance-termination-action=DELETE"
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
    args: argparse.Namespace, node: str, startup_script_path: str
) -> List[str]:
    # ON-DEMAND (no --provisioning-model=SPOT) for bake reliability: Spot is for
    # the active failover burst, not the one-time bake.
    return [
        "gcloud", "compute", "instances", "create", node,
        f"--project={args.project}", f"--zone={args.zone}",
        f"--machine-type={args.machine_type}",
        f"--image-family={args.source_image_family}",
        f"--image-project={args.source_image_project}",
        f"--boot-disk-size={args.boot_disk_size}",
        "--boot-disk-type=pd-balanced",
        f"--metadata-from-file=startup-script={startup_script_path}",
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
# Abort-autopsy (captures bake-node diagnostics BEFORE teardown so the
# operator can diagnose why a readiness / validation / early-failure abort
# happened -- same principle as sovereign_sentinel.py autopsy).
# Bounded + fail-soft: NEVER raises, NEVER blocks teardown.
# --------------------------------------------------------------------------- #
_AUTOPSY_DIR = os.environ.get("JPRIME_BAKE_AUTOPSY_DIR", "autopsy_reports")

# Commands to capture from the remote node (label, remote shell cmd).
_AUTOPSY_CMDS: List[Tuple[str, str]] = [
    ("jprime_bake.log",          "sudo cat /var/log/jprime_bake.log 2>/dev/null || echo '(absent)'"),
    ("ollama_serve.log",         "sudo cat /var/log/ollama_serve.log 2>/dev/null || echo '(absent)'"),
    ("ollama_list.txt",          "ollama list 2>&1 || echo '(ollama list failed)'"),
    ("systemctl_ollama.txt",     "systemctl status ollama --no-pager 2>&1 || echo '(systemd unavailable)'"),
    ("journalctl_ollama.txt",    "journalctl -u ollama --no-pager 2>/dev/null | tail -50 || echo '(journalctl unavailable)'"),
    ("df_h.txt",                 "df -h / 2>&1"),
    ("ollama_api_tags.json",     "curl -s localhost:11434/api/tags 2>&1 || echo '(curl failed)'"),
]

# Hard per-command SSH timeout for autopsy capture (seconds).
_AUTOPSY_CMD_TIMEOUT_S: float = float(os.environ.get("JPRIME_BAKE_AUTOPSY_CMD_TIMEOUT_S", "30"))


def _run_autopsy(args: argparse.Namespace, node: str, reason: str) -> Optional[pathlib.Path]:
    """Capture diagnostic artifacts from the bake node into a local file.

    Must be called BEFORE _cleanup_node (node must still exist).
    Bounded + fail-soft: any failure is logged and silently skipped so
    teardown is NEVER blocked. Returns the path written, or None on failure.
    """
    try:
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        safe_node = re.sub(r"[^A-Za-z0-9_.-]", "_", node)[:60]
        outdir = pathlib.Path(_AUTOPSY_DIR)
        outdir.mkdir(parents=True, exist_ok=True)
        report_path = outdir / f"bake_{safe_node}_{stamp}.log"

        lines: List[str] = [
            f"# JARVIS J-Prime bake autopsy",
            f"# node    : {node}",
            f"# reason  : {reason}",
            f"# captured: {stamp}",
            f"# zone    : {args.zone}",
            f"# project : {args.project}",
            "",
        ]

        for label, remote_cmd in _AUTOPSY_CMDS:
            lines.append(f"{'=' * 60}")
            lines.append(f"# {label}")
            lines.append(f"{'=' * 60}")
            try:
                rc, out = _run(
                    _ssh_cmd(args, node, remote_cmd),
                    timeout_s=_AUTOPSY_CMD_TIMEOUT_S,
                )
                body = out.strip() if out else "(empty output)"
                lines.append(f"[rc={rc}]")
                lines.append(body)
            except Exception as exc:  # noqa: BLE001
                lines.append(f"[capture failed: {exc!r}]")
            lines.append("")

        report_path.write_text("\n".join(lines), encoding="utf-8")
        _log(f"autopsy written -> {report_path}")
        return report_path
    except Exception as exc:  # noqa: BLE001 -- autopsy NEVER blocks teardown
        _log(f"autopsy FAILED (proceeding to teardown): {exc!r}")
        return None


# --------------------------------------------------------------------------- #
# Poll loop.
# --------------------------------------------------------------------------- #

# Patterns in the bake log that signal an unrecoverable failure so we can
# abort early instead of burning the full timeout.
_EARLY_FAIL_PATTERNS: List[re.Pattern] = [
    re.compile(r"\bERROR\b", re.IGNORECASE),
    re.compile(r"ollama pull failed", re.IGNORECASE),
    re.compile(r"startup-script.*exit.*[1-9]", re.IGNORECASE),
    re.compile(r"\bjprime-bake\].*ERROR", re.IGNORECASE),
    re.compile(r"curl.*failed", re.IGNORECASE),
    re.compile(r"^[^#]*exit [1-9]", re.IGNORECASE),
]


def _check_bake_log(args: argparse.Namespace, node: str
                    ) -> Tuple[Optional[str], Optional[str]]:
    """SSH-fetch the last 5 lines of the bake log; return (last_line, fail_reason).

    last_line  -- the most recent log line for live-progress display (or None).
    fail_reason -- non-None if an early-failure marker was found in the tail.

    Fail-soft: any SSH/parse error returns (None, None).
    """
    remote = "sudo tail -n 5 /var/log/jprime_bake.log 2>/dev/null || true"
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
    """Poll the node (via SSH) for the sentinel + `ollama list` model presence.

    Exponential-ish backoff, bounded by --bake-timeout-s.

    Returns (ready, abort_reason):
      (True, "")            -- node is ready.
      (False, "<reason>")   -- timed-out or early-failure detected.

    Three behaviours added vs the original:
      1. Live progress: every poll attempt also SSH-fetches the last bake-log
         line and prints it so the operator sees install/pull progress.
      2. Early-failure detection: if the bake log contains an unambiguous
         failure marker (ERROR / ollama pull failed / etc.) we abort
         immediately rather than burning the full bake_timeout_s.
      3. Return is now a 2-tuple (ready, reason) so callers can distinguish
         timeout from early-fail for the autopsy manifest.
    """
    deadline = time.monotonic() + float(args.bake_timeout_s)
    delay = 15.0
    attempt = 0
    check = (
        f"test -f {shlex.quote(_SENTINEL_PATH)} "
        f"&& ollama list 2>/dev/null | grep -q {shlex.quote(args.model.split(':')[0])}"
        " && echo JPRIME_READY || echo JPRIME_NOT_READY"
    )
    while time.monotonic() < deadline:
        attempt += 1
        rc, out = _run(_ssh_cmd(args, node, check), timeout_s=90.0)
        if rc == 0 and "JPRIME_READY" in out:
            _log(f"readiness: node ready (attempt {attempt})")
            return True, ""

        # Live progress: fetch last bake-log lines (every attempt; bounded 30s).
        # Also checks for early-failure markers -- bail out immediately if found.
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
# Validation (the load-bearing lock -- run a real generation before snapshot).
# --------------------------------------------------------------------------- #
def _validate_generation(args: argparse.Namespace, node: str) -> Tuple[bool, str]:
    """SSH-curl a real code-generation against the node's Ollama endpoint.

    Uses `curl --fail` so a non-200 sets rc != 0. Returns (passed, sample).
    """
    payload = json.dumps({
        "model": args.model,
        "prompt": _VALIDATION_PROMPT,
        "stream": False,
    })
    # Single-quote the JSON for the remote shell; curl --fail-with-body so we
    # see a body even on a 4xx/5xx (and rc reflects the HTTP status).
    remote = (
        f"curl -fsS --max-time 300 "
        f"http://localhost:{_OLLAMA_PORT}/api/generate "
        f"-H 'Content-Type: application/json' "
        f"-d {shlex.quote(payload)}"
    )
    _log("validation: running real generation against node Ollama endpoint")
    rc, out = _run(_ssh_cmd(args, node, remote), timeout_s=360.0)
    return parse_validation_verdict(rc, out)


# --------------------------------------------------------------------------- #
# Dry-run plan printer.
# --------------------------------------------------------------------------- #
def _print_plan(args: argparse.Namespace, node: str, startup_script: str) -> None:
    print("=" * 72)
    print("J-PRIME GOLDEN-IMAGE BAKE -- PLAN (dry-run, spends nothing)")
    print("=" * 72)
    print(f"  project        : {args.project}")
    print(f"  zone           : {args.zone}")
    print(f"  machine-type   : {args.machine_type}  (CPU-only; model in RAM)")
    print(f"  model          : {args.model}")
    print(f"  bake node      : {node}  (ON-DEMAND for bake reliability)")
    print(f"  source image   : {args.source_image_family}/{args.source_image_project}")
    print(f"  boot disk      : {args.boot_disk_size}")
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
    print("  2. POLL READINESS (repeated, bounded):")
    print("     " + " ".join(shlex.quote(c) for c in
                              _ssh_cmd(args, node, "test -f " + _SENTINEL_PATH + " ...")))
    print("  3. VALIDATION LOCK (real generation; NO snapshot unless PASS):")
    print("     " + " ".join(shlex.quote(c) for c in
                              _ssh_cmd(args, node,
                                       f"curl ... localhost:{_OLLAMA_PORT}/api/generate")))
    print("  4. STOP instance (consistent image):")
    print(f"     gcloud compute instances stop {node} "
          f"--project={args.project} --zone={args.zone} --quiet")
    print("  5. CREATE GOLDEN IMAGE:")
    print(f"     gcloud compute images create {args.image_name} "
          f"--project={args.project} --source-disk={node} "
          f"--source-disk-zone={args.zone} --family={args.image_family}")
    print("  6. DELETE-TO-SNAPSHOT (node + disk; image is the durable artifact):")
    print(f"     gcloud compute instances delete {node} "
          f"--project={args.project} --zone={args.zone} --delete-disks=all --quiet")
    print("-" * 72)
    print("DORMANT FOOTPRINT: image-only (VM + disk deleted) ~ $0.50/mo")
    print("AWAKEN ONE-LINER (used later by the failover lifecycle controller):")
    print("  " + build_awaken_one_liner(args))
    print("=" * 72)
    print("[BAKE] --dry-run: nothing executed, no money spent. Use --execute to bake.")


# --------------------------------------------------------------------------- #
# Execute pipeline.
# --------------------------------------------------------------------------- #
def _execute_bake(args: argparse.Namespace, node: str, startup_script: str) -> int:
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

    # Write the startup-script to a tmpfile for --metadata-from-file.
    import tempfile
    fd, sp_path = tempfile.mkstemp(prefix="jprime_startup_", suffix=".sh")
    node_exists = False
    bake_succeeded = False          # tracks whether we reached SUCCESS (skip autopsy)
    abort_reason: str = ""          # reason passed to autopsy manifest
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(startup_script)

        # 1. PROVISION (ON-DEMAND).
        _log(f"provisioning bake node {node} (ON-DEMAND, {args.machine_type})")
        rc, out = _run(_create_node_cmd(args, node, sp_path), timeout_s=300.0)
        if rc != 0:
            _abort(f"provision failed rc={rc}: {out.strip()[:400]}")
            abort_reason = f"provision_failed rc={rc}"
            return 4
        node_exists = True
        _log(f"node {node} created; startup-script installing Ollama + pulling model")

        # 2. POLL READINESS (includes live progress + early-fail detection).
        ready, poll_abort_reason = _poll_readiness(args, node)
        if not ready:
            _abort(f"readiness abort: {poll_abort_reason}")
            abort_reason = poll_abort_reason
            return 5

        # 3. VALIDATION LOCK -- never snapshot a broken image.
        passed, sample = _validate_generation(args, node)
        if not passed:
            _abort(f"validation failed: {sample}")
            abort_reason = f"validation_failed: {sample[:200]}"
            return 6
        _log("validation: PASS -- node generated plausible Python (loaded in RAM)")
        _log(f"validation sample: {sample[:300]!r}")

        # 4. STOP for a consistent image.
        _log(f"stopping {node} for a consistent image snapshot")
        rc, out = _run([
            "gcloud", "compute", "instances", "stop", node,
            f"--project={args.project}", f"--zone={args.zone}", "--quiet",
        ], timeout_s=300.0)
        if rc != 0:
            _abort(f"instance stop failed rc={rc}: {out.strip()[:300]}")
            abort_reason = f"instance_stop_failed rc={rc}"
            return 7

        # If --force replacing an existing image, delete the old one first.
        if args.force and _image_exists(args):
            _log(f"--force: deleting existing image {args.image_name}")
            _run([
                "gcloud", "compute", "images", "delete", args.image_name,
                f"--project={args.project}", "--quiet",
            ], timeout_s=300.0)

        # 5. CREATE GOLDEN IMAGE.
        _log(f"creating golden image {args.image_name} (family={args.image_family})")
        rc, out = _run([
            "gcloud", "compute", "images", "create", args.image_name,
            f"--project={args.project}", f"--source-disk={node}",
            f"--source-disk-zone={args.zone}", f"--family={args.image_family}",
        ], timeout_s=900.0)
        if rc != 0:
            _abort(f"image create failed rc={rc}: {out.strip()[:400]}")
            abort_reason = f"image_create_failed rc={rc}"
            return 8
        _log(f"golden image created: {args.image_name}")

        # 6. DELETE-TO-SNAPSHOT happens in the finally cleanup.
        bake_succeeded = True
        _report_success(args, node, sample)
        return 0
    finally:
        try:
            os.unlink(sp_path)
        except OSError:
            pass
        if node_exists:
            # ABORT-AUTOPSY: on any non-success path (readiness timeout,
            # early-failure, validation-fail, image-create-fail) -- capture
            # node diagnostics BEFORE teardown so the operator can diagnose
            # why the bake failed. Bounded + fail-soft: autopsy NEVER blocks
            # teardown. A successful bake does NOT autopsy.
            if not bake_succeeded:
                _run_autopsy(args, node, abort_reason or "unknown_abort")
            # ALWAYS tear the node + disk down -- success or failure. The image
            # (if created) is the durable artifact; the VM must never linger.
            _cleanup_node(args, node)


def _report_success(args: argparse.Namespace, node: str, sample: str) -> None:
    print("=" * 72)
    print("[BAKE] SUCCESS -- golden image baked + validated")
    print("=" * 72)
    print(f"  image name   : {args.image_name}")
    print(f"  image family : {args.image_family}")
    print(f"  model        : {args.model} (loaded in RAM + generating on CPU)")
    print("  validation   : PASS (real generation produced plausible Python)")
    print(f"  sample       : {sample[:200]!r}")
    print("  dormant cost : image-only (VM + disk deleted) ~ $0.50/mo")
    print("-" * 72)
    print("  AWAKEN later (failover lifecycle controller):")
    print("    " + build_awaken_one_liner(args))
    print("=" * 72)


# --------------------------------------------------------------------------- #
# CLI.
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "One-time autonomous bake of the J-Prime code-only golden image "
            "(provision -> install Ollama -> pull model -> VALIDATE -> "
            "snapshot -> delete-to-snapshot). Default is --dry-run."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--project", default=_DEFAULT_PROJECT,
                   help="GCP project (env GCP_PROJECT)")
    p.add_argument("--zone", default=_DEFAULT_ZONE,
                   help="GCP zone (env GCP_ZONE)")
    p.add_argument("--machine-type", default=_DEFAULT_MACHINE,
                   help="bake machine type (CPU-only; model in RAM)")
    p.add_argument("--model", default=_DEFAULT_MODEL,
                   help="Ollama code model to pull")
    p.add_argument("--image-name", default=_default_image_name(),
                   help="golden image name (env JPRIME_IMAGE_NAME)")
    p.add_argument("--image-family", default=_DEFAULT_IMAGE_FAMILY,
                   help="golden image family (env JPRIME_IMAGE_FAMILY)")
    p.add_argument("--boot-disk-size", default=_DEFAULT_BOOT_DISK,
                   help="bake node boot disk size")
    p.add_argument("--bake-timeout-s", type=int, default=_DEFAULT_BAKE_TIMEOUT_S,
                   help="readiness poll timeout (seconds)")
    p.add_argument("--source-image-family", default=_DEFAULT_DEBIAN_IMAGE_FAMILY,
                   help="base OS image family (Debian)")
    p.add_argument("--source-image-project", default=_DEFAULT_DEBIAN_IMAGE_PROJECT,
                   help="base OS image project")
    p.add_argument("--node-name", default=None,
                   help="bake node name (default jarvis-prime-bake-<stamp>)")
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
    node = args.node_name or f"jarvis-prime-bake-{_now_stamp()}-{int(time.time()) % 100000}"
    startup_script = build_startup_script(args.model)

    if args.dry_run:
        _print_plan(args, node, startup_script)
        return 0

    _log("EXECUTE mode -- this WILL provision a GCP node and spend money")
    return _execute_bake(args, node, startup_script)


if __name__ == "__main__":
    sys.exit(main())
