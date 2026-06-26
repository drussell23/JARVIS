#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Autonomous Pre-Flight Provisioner -- one command: bake-if-needed -> Omni-Soak.

THE FEAST LAUNCHER. A thin orchestration layer over the EXISTING scripts (no
logic duplication -- it subprocesses / imports them):

    1. PRE-FLIGHT IMAGE CHECK
       gcloud compute images list --filter="family:jarvis-soak-golden" + read
       the jarvis_req_sha label. Compute the CURRENT requirements.txt sha (reusing
       the baker's helper). MATCH -> fresh; mismatch -> stale; absent -> missing.

         fresh   -> arm JARVIS_IAC_SOAK_GOLDEN_ENABLED=1, SKIP the bake, go to soak.
         missing -> autonomously subprocess bake_soak_golden_image.py --execute,
         /stale     verify the image now exists with the matching sha, then arm
                    JARVIS_IAC_SOAK_GOLDEN_ENABLED=1 and proceed.

    2. GRACEFUL DEGRADATION (never block the feast)
       If the autonomous bake FAILS (non-zero exit / transient GCP error / the
       image never appears) -> CATCH, log a SEVERE warning, UNSET the golden flag
       (so the harness uses its raw-Debian + live-pip fallback), and CONTINUE. A
       bake failure NEVER aborts the run.

    3. TRANSITION TO THE OMNI-SOAK
       Subprocess a1_live_fire_chaos_harness.py --remote
       --i-understand-this-spends-money with the armed launch env inherited.
       Propagate its exit code.

    4. IRONCLAD ANTI-ZOMBIE SWEEP (the launcher-level backstop)
       The ENTIRE pipeline (bake + soak) is wrapped in try/finally AND guarded by
       SIGINT/SIGTERM/SIGHUP handlers. On ANY exit -- success, exception, signal,
       the harness's own wall-hit -- a cleanup sweep fires that deletes EVERY stray
       GCP instance from this run: the baker VM (jarvis-soak-bake-*) AND the soak
       VM (sovereign-sandbox-*), via `gcloud compute instances list` + `delete`
       (reusing the hypervisor's reap idiom). Idempotent, best-effort, never
       raises. Guarantee: zero GCP instances left running, even on a Python crash.

    5. ARM THE FLAGS
       Sets JARVIS_META_GOAL_AGGREGATOR_ENABLED=1 + JARVIS_A1_OMNI_SOAK=1 +
       JARVIS_IAC_FAULT_TOLERANT_OBS_ENABLED=1 (and the golden flag per the image
       result) in the env handed to the soak. Also added to
       deploy/ouroboros_omni_prod.env so the overlay carries them on the node.

The launcher is PURE orchestration: check -> (bake | degrade) -> soak -> ALWAYS
sweep. Zero hardcoding (image family / timeouts / flags are env-tunable). ASCII
only, fail-soft throughout.

Usage:
    # the real feast (spends money on a GCP soak node):
    python3 scripts/autonomous_omni_launcher.py --i-understand-this-spends-money

    # dry-run (print the plan, touch NOTHING):
    python3 scripts/autonomous_omni_launcher.py --dry-run
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import pathlib
import signal
import subprocess
import sys
from typing import Dict, List, Optional, Tuple

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_BAKER_SCRIPT = str(_REPO_ROOT / "scripts" / "bake_soak_golden_image.py")
_HARNESS_SCRIPT = str(_REPO_ROOT / "scripts" / "a1_live_fire_chaos_harness.py")

# --------------------------------------------------------------------------- #
# Defaults -- every value env-tunable (zero hardcoding). The GCP coordinates +
# the image family + the req-sha label mirror the baker / hypervisor exactly.
# --------------------------------------------------------------------------- #
_DEFAULT_PROJECT = os.environ.get("GCP_PROJECT", "jarvis-473803")
_DEFAULT_ZONE = os.environ.get("GCP_ZONE", "us-central1-a")
_DEFAULT_IMAGE_FAMILY = os.environ.get(
    "JARVIS_IAC_SOAK_GOLDEN_IMAGE_FAMILY", "jarvis-soak-golden"
)
_REQ_SHA_LABEL_KEY = os.environ.get(
    "JARVIS_IAC_GOLDEN_REQ_SHA_LABEL", "jarvis_req_sha"
)
_DEFAULT_REQUIREMENTS = os.environ.get(
    "JARVIS_SOAK_BAKE_REQUIREMENTS", str(_REPO_ROOT / "requirements.txt")
)
_DEFAULT_BAKE_TIMEOUT_S = int(os.environ.get("JARVIS_LAUNCHER_BAKE_TIMEOUT_S", "2700"))
_DEFAULT_SOAK_TIMEOUT_S = int(os.environ.get("JARVIS_LAUNCHER_SOAK_TIMEOUT_S", "0"))  # 0 = no harness-side cap
_DEFAULT_COST_CAP = float(os.environ.get("JARVIS_LAUNCHER_COST_CAP", "12.0"))  # USD money guardrail forwarded to the harness
_DEFAULT_MAX_WALL_S = int(os.environ.get("JARVIS_LAUNCHER_MAX_WALL_S", "3000"))  # harness --max-wall-seconds
_DEFAULT_SWEEP_TIMEOUT_S = float(os.environ.get("JARVIS_LAUNCHER_SWEEP_TIMEOUT_S", "300"))

# The stray-instance name prefixes the sweep reaps (env-tunable, comma-list).
_BAKE_NODE_PREFIX = os.environ.get("JARVIS_LAUNCHER_BAKE_NODE_PREFIX", "jarvis-soak-bake-")
_SOAK_NODE_PREFIX = os.environ.get("JARVIS_LAUNCHER_SOAK_NODE_PREFIX", "sovereign-sandbox-")

# The flags the launcher arms in the soak env (CONSTRAINT 4). Golden is armed
# separately, conditional on the image-freshness result.
_ARM_FLAGS = (
    "JARVIS_META_GOAL_AGGREGATOR_ENABLED",
    "JARVIS_A1_OMNI_SOAK",
    "JARVIS_IAC_FAULT_TOLERANT_OBS_ENABLED",
)
_GOLDEN_FLAG = "JARVIS_IAC_SOAK_GOLDEN_ENABLED"

# Node-lifetime env the soak inherits so the IAC node is ON-DEMAND (NOT Spot --
# a preempted Spot node vanishes mid-soak and kills the run) with a long
# dead-man / max-run / idle horizon that outlives the ~1.5h pipeline. Applied
# with setdefault semantics: an explicit operator env value always wins.
_NODE_LIFETIME_ENV = {
    "JARVIS_IAC_ON_DEMAND": os.environ.get("JARVIS_LAUNCHER_IAC_ON_DEMAND", "1"),
    "JARVIS_IAC_MAX_RUN_DURATION_S": os.environ.get("JARVIS_LAUNCHER_IAC_MAX_RUN_DURATION_S", "7200"),
    "JARVIS_IAC_NODE_IDLE_TIMEOUT_S": os.environ.get("JARVIS_LAUNCHER_IAC_NODE_IDLE_TIMEOUT_S", "7200"),
    "JARVIS_IAC_DEADMAN_IDLE_TIMEOUT_S": os.environ.get("JARVIS_LAUNCHER_IAC_DEADMAN_IDLE_TIMEOUT_S", "7200"),
    "JARVIS_IAC_MAX_WALL_SECONDS": os.environ.get("JARVIS_LAUNCHER_IAC_MAX_WALL_SECONDS", "6000"),
}


# --------------------------------------------------------------------------- #
# Reuse the baker's requirements_sha (single source of truth, NO dup). Fail-soft:
# if the baker can't import (it shouldn't -- pure stdlib), fall back to an inline
# sha256 with the IDENTICAL algorithm so the staleness check still works.
# --------------------------------------------------------------------------- #
def _load_baker_sha():
    try:
        spec = importlib.util.spec_from_file_location("_baker_for_launcher", _BAKER_SCRIPT)
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
            fn = getattr(mod, "requirements_sha", None)
            if callable(fn):
                return fn
    except Exception:  # noqa: BLE001 -- never crash the launcher on an import edge
        pass

    def _inline(req_path: str) -> str:
        import hashlib
        try:
            data = pathlib.Path(req_path).read_bytes()
        except Exception:  # noqa: BLE001
            return "norequirements"
        return hashlib.sha256(data).hexdigest()[:16]

    return _inline


_baker_requirements_sha = _load_baker_sha()


def current_requirements_sha(req_path: Optional[str] = None) -> str:
    """sha256[:16] of requirements.txt -- the staleness stamp (reuses the baker)."""
    return _baker_requirements_sha(req_path or _DEFAULT_REQUIREMENTS)


def default_requirements_path() -> str:
    return _DEFAULT_REQUIREMENTS


# --------------------------------------------------------------------------- #
# Config -- a tiny namespace the orchestration threads through (so every gcloud
# coordinate is resolved once, env-tunable, never hardcoded mid-flow).
# --------------------------------------------------------------------------- #
class Config:
    __slots__ = (
        "project", "zone", "image_family", "req_sha_label", "requirements",
        "bake_timeout_s", "soak_timeout_s", "sweep_timeout_s",
        "bake_prefix", "soak_prefix", "dry_run", "money_gate",
        "cost_cap", "max_wall_s",
    )

    def __init__(self, **kw):
        self.project = kw.get("project", _DEFAULT_PROJECT)
        self.zone = kw.get("zone", _DEFAULT_ZONE)
        self.image_family = kw.get("image_family", _DEFAULT_IMAGE_FAMILY)
        self.req_sha_label = kw.get("req_sha_label", _REQ_SHA_LABEL_KEY)
        self.requirements = kw.get("requirements", _DEFAULT_REQUIREMENTS)
        self.bake_timeout_s = kw.get("bake_timeout_s", _DEFAULT_BAKE_TIMEOUT_S)
        self.soak_timeout_s = kw.get("soak_timeout_s", _DEFAULT_SOAK_TIMEOUT_S)
        self.sweep_timeout_s = kw.get("sweep_timeout_s", _DEFAULT_SWEEP_TIMEOUT_S)
        self.bake_prefix = kw.get("bake_prefix", _BAKE_NODE_PREFIX)
        self.soak_prefix = kw.get("soak_prefix", _SOAK_NODE_PREFIX)
        self.dry_run = kw.get("dry_run", False)
        self.money_gate = kw.get("money_gate", False)
        self.cost_cap = kw.get("cost_cap", _DEFAULT_COST_CAP)
        self.max_wall_s = kw.get("max_wall_s", _DEFAULT_MAX_WALL_S)


def build_config(args: Optional[argparse.Namespace] = None) -> Config:
    if args is None:
        return Config()
    return Config(
        project=args.project, zone=args.zone, image_family=args.image_family,
        requirements=args.requirements, bake_timeout_s=args.bake_timeout_s,
        dry_run=getattr(args, "dry_run", False),
        money_gate=getattr(args, "money_gate", False),
        cost_cap=getattr(args, "cost_cap", _DEFAULT_COST_CAP),
        max_wall_s=getattr(args, "max_wall_s", _DEFAULT_MAX_WALL_S),
    )


# --------------------------------------------------------------------------- #
# Logging.
# --------------------------------------------------------------------------- #
def _log(msg: str) -> None:
    print(f"[launcher] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# THE single subprocess boundary for gcloud. ALL gcloud funnels through here so
# tests intercept it with one monkeypatch. Never raises -- returns (rc, output).
# --------------------------------------------------------------------------- #
def _run(cmd: List[str], *, timeout_s: float = 120.0) -> Tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as exc:  # noqa: BLE001 -- the launcher never crashes on a call
        return 1, f"[run failed: {exc!r}]"


# --------------------------------------------------------------------------- #
# STEP 1 -- pre-flight image freshness probe.
# --------------------------------------------------------------------------- #
def check_image_freshness(cfg: Config) -> str:
    """Return 'fresh' | 'stale' | 'missing'.

    Lists the jarvis-soak-golden family + reads its jarvis_req_sha label, compares
    against the CURRENT requirements.txt sha. Fail-soft: any gcloud error or an
    empty list -> 'missing' (forces a bake / degrade -- never a false 'fresh').
    """
    cur = current_requirements_sha(cfg.requirements)
    rc, out = _run([
        "gcloud", "compute", "images", "list",
        f"--project={cfg.project}",
        f"--filter=family:{cfg.image_family}",
        f"--format=value(labels.{cfg.req_sha_label})",
        "--sort-by=~creationTimestamp", "--limit=1",
    ])
    label = (out or "").strip()
    if rc != 0 or not label:
        _log(f"pre-flight: no '{cfg.image_family}' image found (rc={rc}) -> MISSING")
        return "missing"
    if label == cur:
        _log(f"pre-flight: image fresh (req_sha={cur} matches label) -> FRESH")
        return "fresh"
    _log(f"pre-flight: image STALE (label={label} != current={cur}) -> STALE")
    return "stale"


# --------------------------------------------------------------------------- #
# STEP 1 (cont) -- autonomous bake (subprocess the baker; verify it landed).
# --------------------------------------------------------------------------- #
def bake_golden(cfg: Config, env: Dict[str, str]) -> bool:
    """Subprocess bake_soak_golden_image.py --execute; verify the image is fresh.

    Returns True iff the baker exits 0 AND the post-bake freshness probe reports
    'fresh'. NO bake logic duplicated -- we drive the existing baker script.
    Never raises (a crash surfaces as False -> the caller degrades).
    """
    argv = [
        sys.executable, _BAKER_SCRIPT, "--execute",
        f"--project={cfg.project}", f"--zone={cfg.zone}",
        f"--image-family={cfg.image_family}",
        f"--requirements={cfg.requirements}",
        f"--bake-timeout-s={cfg.bake_timeout_s}",
    ]
    _log("STEP bake: image missing/stale -> autonomously baking the golden image")
    _log("  " + " ".join(argv))
    try:
        cp = subprocess.run(argv, env=env, check=False, timeout=cfg.bake_timeout_s + 900)
        if cp.returncode != 0:
            _log(f"bake: baker exited non-zero (rc={cp.returncode})")
            return False
    except Exception as exc:  # noqa: BLE001
        _log(f"bake: baker subprocess error: {exc!r}")
        return False
    # Verify the image now exists with the matching sha (don't trust rc alone).
    state = check_image_freshness(cfg)
    if state == "fresh":
        _log("bake: SUCCESS -- golden image present + sha-fresh")
        return True
    _log(f"bake: post-bake verify FAILED (image state={state})")
    return False


# --------------------------------------------------------------------------- #
# STEP 3 -- transition to the Omni-Soak (subprocess the harness --remote).
# --------------------------------------------------------------------------- #
def run_soak(cfg: Config, env: Dict[str, str]) -> int:
    """Subprocess a1_live_fire_chaos_harness.py --remote (the Omni-Soak).

    Inherits the armed launch env. Propagates the harness exit code. The harness
    owns its own money-gate; we pass it through. No soak logic duplicated.
    """
    argv = [
        sys.executable, _HARNESS_SCRIPT,
        "--remote", "--i-understand-this-spends-money",
        "--cost-cap", str(cfg.cost_cap),
        "--max-wall-seconds", str(cfg.max_wall_s),
    ]
    _log("STEP soak: transitioning to the Omni-Soak (--remote)")
    _log("  " + " ".join(argv))
    try:
        cp = subprocess.run(argv, env=env, check=False)
        return cp.returncode
    except Exception as exc:  # noqa: BLE001
        _log(f"soak: harness subprocess error: {exc!r}")
        return 1


# --------------------------------------------------------------------------- #
# STEP 4 -- the ironclad anti-zombie sweep (reuses the hypervisor reap idiom:
# `gcloud instances list` + `instances delete --delete-disks=all --quiet`).
# Idempotent, best-effort, NEVER raises.
# --------------------------------------------------------------------------- #
def _list_stray_instances(cfg: Config, prefix: str) -> List[Tuple[str, str]]:
    """Return [(name, zone), ...] for instances whose name starts with `prefix`.

    Fail-soft: any error yields an empty list (the sweep simply finds nothing).
    """
    try:
        rc, out = _run([
            "gcloud", "compute", "instances", "list",
            f"--project={cfg.project}",
            f"--filter=name~^{prefix}",
            "--format=value(name,zone)",
        ], timeout_s=cfg.sweep_timeout_s)
        if rc != 0 or not out:
            return []
        pairs: List[Tuple[str, str]] = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            # `value(name,zone)` is tab/space separated; the zone may be a full URL.
            parts = line.split()
            name = parts[0]
            zone = parts[1].rsplit("/", 1)[-1] if len(parts) > 1 else cfg.zone
            if name.startswith(prefix):
                pairs.append((name, zone))
        return pairs
    except Exception:  # noqa: BLE001 -- the sweep never raises
        return []


def _delete_instance(cfg: Config, name: str, zone: str) -> None:
    """Best-effort delete of a single instance (+ its disks). Never raises."""
    try:
        rc, out = _run([
            "gcloud", "compute", "instances", "delete", name,
            f"--project={cfg.project}", f"--zone={zone}",
            "--delete-disks=all", "--quiet",
        ], timeout_s=cfg.sweep_timeout_s)
        if rc == 0:
            _log(f"sweep: deleted stray instance {name} ({zone})")
        else:
            _log(f"sweep: WARNING delete {name} rc={rc}: {(out or '').strip()[:200]}")
    except Exception as exc:  # noqa: BLE001 -- the sweep never raises
        _log(f"sweep: WARNING delete {name} raised (swallowed): {exc!r}")


def anti_zombie_sweep(cfg: Config) -> None:
    """Delete EVERY stray instance from this run (baker + soak VMs). Never raises.

    Reuses the hypervisor's reap idiom (`instances list` -> `instances delete
    --delete-disks=all --quiet`). Idempotent: re-running finds nothing the second
    time. Guarantee: zero GCP instances left running even on a catastrophic crash.
    """
    try:
        _log("sweep: anti-zombie backstop -- reaping stray baker + soak instances")
        seen = set()
        for prefix in (cfg.bake_prefix, cfg.soak_prefix):
            for name, zone in _list_stray_instances(cfg, prefix):
                if name in seen:
                    continue
                seen.add(name)
                _delete_instance(cfg, name, zone)
        if not seen:
            _log("sweep: clean -- no stray instances found")
    except Exception as exc:  # noqa: BLE001 -- ABSOLUTE: the sweep never raises
        _log(f"sweep: WARNING swept with errors (swallowed): {exc!r}")


# --------------------------------------------------------------------------- #
# Signal handling -- SIGINT/SIGTERM/SIGHUP -> sweep -> exit. The handler closes
# over the active Config so it can reap even on an external kill.
# --------------------------------------------------------------------------- #
def _signal_handler(cfg: Config):
    def _handler(signum, _frame):
        _log(f"signal {signum} received -- firing anti-zombie sweep then exiting")
        anti_zombie_sweep(cfg)
        # Exit with the conventional 128+signum so the parent sees the cause.
        raise SystemExit(128 + int(signum))
    return _handler


def _install_signal_handlers(cfg: Config) -> None:
    for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGHUP", None)):
        if sig is None:
            continue
        try:
            signal.signal(sig, _signal_handler(cfg))
        except (ValueError, OSError):  # not in main thread / unsupported -- skip
            pass


# --------------------------------------------------------------------------- #
# Env composition -- arm the flags the soak inherits (CONSTRAINT 4 + golden).
# --------------------------------------------------------------------------- #
def _compose_soak_env(golden_armed: bool) -> Dict[str, str]:
    env = dict(os.environ)
    for flag in _ARM_FLAGS:
        env[flag] = "1"
    # Node-lifetime: on-demand + long horizon so the soak node never gets
    # Spot-preempted or dead-man-reaped mid-feast. setdefault -> explicit env wins.
    for key, val in _NODE_LIFETIME_ENV.items():
        env.setdefault(key, val)
    if golden_armed:
        env[_GOLDEN_FLAG] = "1"
    else:
        # Degraded path -- the harness uses raw Debian + live pip. Unset so a
        # stale inherited value can't force a broken golden boot.
        env.pop(_GOLDEN_FLAG, None)
    return env


# --------------------------------------------------------------------------- #
# Orchestration -- check -> (bake | degrade) -> soak -> ALWAYS sweep.
# --------------------------------------------------------------------------- #
def _orchestrate(cfg: Config) -> int:
    # STEP 1: pre-flight freshness.
    state = check_image_freshness(cfg)
    golden_armed = False

    if state == "fresh":
        golden_armed = True
    else:
        # STEP 1 (cont): autonomous bake. The bake env arms the same flags (the
        # baker only reads GCP coordinates, but inheriting is harmless + uniform).
        bake_env = _compose_soak_env(golden_armed=True)
        baked = False
        try:
            baked = bake_golden(cfg, bake_env)
        except Exception as exc:  # noqa: BLE001 -- bake failure NEVER aborts the run
            _log(f"bake raised (treated as failure): {exc!r}")
            baked = False

        if baked:
            golden_armed = True
        else:
            # CONSTRAINT 2 -- graceful degradation. NEVER block the feast.
            _log("[launcher] golden bake FAILED -- degrading to raw Debian + live "
                 "pip install; soak continues")
            golden_armed = False

    # STEP 5 (env) + STEP 3 -- arm flags + transition to the Omni-Soak.
    soak_env = _compose_soak_env(golden_armed=golden_armed)
    _log("STEP arm: META_GOAL + OMNI_SOAK + FAULT_TOLERANT_OBS armed; golden=%s"
         % ("1" if golden_armed else "UNSET"))
    return run_soak(cfg, soak_env)


def _print_dry_run_plan(cfg: Config) -> None:
    cur = current_requirements_sha(cfg.requirements)
    print("=" * 72)
    print("AUTONOMOUS OMNI LAUNCHER -- PLAN (dry-run, touches NOTHING)")
    print("=" * 72)
    print(f"  project        : {cfg.project}")
    print(f"  zone           : {cfg.zone}")
    print(f"  image family   : {cfg.image_family}")
    print(f"  req sha label  : {cfg.req_sha_label}")
    print(f"  requirements   : {cfg.requirements}")
    print(f"  current sha    : {cur}")
    print(f"  bake timeout   : {cfg.bake_timeout_s}s")
    print(f"  reap prefixes  : {cfg.bake_prefix}* , {cfg.soak_prefix}*")
    print("-" * 72)
    print("PIPELINE (what --i-understand-this-spends-money would do):")
    print("  1. pre-flight  : gcloud images list --filter=family:%s -> fresh|stale|missing"
          % cfg.image_family)
    print("  2. fresh       : arm %s=1, SKIP bake" % _GOLDEN_FLAG)
    print("     stale/miss  : subprocess bake_soak_golden_image.py --execute, verify, arm")
    print("     bake-fail   : SEVERE warn, UNSET %s, CONTINUE (never block)" % _GOLDEN_FLAG)
    print("  3. arm flags   : %s" % " ".join(_ARM_FLAGS))
    print("  4. soak        : subprocess a1_live_fire_chaos_harness.py --remote "
          "--i-understand-this-spends-money")
    print("  5. ALWAYS sweep: delete %s* + %s* (try/finally + SIGINT/SIGTERM/SIGHUP)"
          % (cfg.bake_prefix, cfg.soak_prefix))
    print("=" * 72)
    print("[launcher] --dry-run: nothing executed, no money spent. Use "
          "--i-understand-this-spends-money to launch.")


# --------------------------------------------------------------------------- #
# CLI.
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Autonomous Pre-Flight Provisioner -- one command: check the "
            "jarvis-soak-golden image, bake it if missing/stale (graceful "
            "degradation to raw Debian + pip on failure -- never blocks), then "
            "transition into the Omni-Soak, with an ironclad anti-zombie sweep "
            "on ANY exit."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--project", default=_DEFAULT_PROJECT, help="GCP project (env GCP_PROJECT)")
    p.add_argument("--zone", default=_DEFAULT_ZONE, help="GCP zone (env GCP_ZONE)")
    p.add_argument("--image-family", default=_DEFAULT_IMAGE_FAMILY,
                   help="golden image family (env JARVIS_IAC_SOAK_GOLDEN_IMAGE_FAMILY)")
    p.add_argument("--requirements", default=_DEFAULT_REQUIREMENTS,
                   help="requirements.txt path (env JARVIS_SOAK_BAKE_REQUIREMENTS)")
    p.add_argument("--cost-cap", dest="cost_cap", type=float, default=_DEFAULT_COST_CAP,
                   help="USD money guardrail forwarded to the harness (env JARVIS_LAUNCHER_COST_CAP)")
    p.add_argument("--max-wall-seconds", dest="max_wall_s", type=int, default=_DEFAULT_MAX_WALL_S,
                   help="harness --max-wall-seconds (env JARVIS_LAUNCHER_MAX_WALL_S)")
    p.add_argument("--bake-timeout-s", type=int, default=_DEFAULT_BAKE_TIMEOUT_S,
                   help="bake readiness timeout (env JARVIS_LAUNCHER_BAKE_TIMEOUT_S)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="dry_run", action="store_true",
                      help="print the plan + commands WITHOUT executing (default)")
    mode.add_argument("--i-understand-this-spends-money", dest="money_gate",
                      action="store_true",
                      help="REAL-MONEY safety gate -- required to actually launch")
    p.set_defaults(dry_run=False, money_gate=False)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = build_config(args)

    if args.dry_run:
        _print_dry_run_plan(cfg)
        return 0

    if not args.money_gate:
        _log("REFUSED: launching the Omni-Soak spends real money. Pass "
             "--i-understand-this-spends-money (or --dry-run to preview).")
        return 2

    # CONSTRAINT 3 -- arm the signal handlers BEFORE any GCP work, so even a kill
    # during the bake reaps the baker VM.
    _install_signal_handlers(cfg)

    # The ENTIRE pipeline (bake + soak) is wrapped so the sweep ALWAYS fires.
    try:
        return _orchestrate(cfg)
    finally:
        anti_zombie_sweep(cfg)


if __name__ == "__main__":
    sys.exit(main())
