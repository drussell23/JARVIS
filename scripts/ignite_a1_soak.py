#!/usr/bin/env python3
"""Adaptive Ignition Harness -- docker-aware preflight + immutable tee + [FOR CLAUDE] summarizer.

Usage:
    python3 scripts/ignite_a1_soak.py [--max-wall-seconds N] [--skip-preflight]
                                       [--run-root DIR] [--dry-run]

Exit codes:
    0   A1_DISPATCH_PROVEN
    non-zero (the driver's own exit code)   soak non-zero (failure telemetry printed as [FOR CLAUDE] block)
    2   docker daemon not responsive
    3   preflight (integration test) failed -- soak skipped
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from subprocess import PIPE, STDOUT


# ---------------------------------------------------------------------------
# Helper: Docker daemon check
# ---------------------------------------------------------------------------

def docker_responsive(probe=None) -> tuple[bool, str]:
    """(ok, reason).  Pings the daemon via `docker info` (not just PATH).

    Parameters
    ----------
    probe : callable | None
        Injectable for tests -- called as ``probe()`` returning ``(bool, str)``.
        When None the real subprocess path runs.
    """
    if probe is not None:
        return probe()

    if shutil.which("docker") is None:
        return (False, "docker CLI not found on PATH")

    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            return (True, "daemon responsive")
        stderr_lines = [ln.strip() for ln in result.stderr.splitlines() if ln.strip()]
        reason = stderr_lines[-1] if stderr_lines else "docker info failed"
        return (False, reason)
    except subprocess.TimeoutExpired:
        return (False, "docker info timed out (daemon may be hung)")
    except OSError as exc:
        return (False, str(exc))


# ---------------------------------------------------------------------------
# Helper: Immutable tee runner
# ---------------------------------------------------------------------------

def tee_run(argv: list[str], log_handle, *, env=None, cwd=None) -> int:
    """Run *argv*; stream merged stdout+stderr to both sys.stdout and log_handle.

    Both streams are flushed after every line -- zero data loss guarantee.
    Returns the exit code of the subprocess.
    """
    _env = dict(env if env is not None else os.environ)
    _env.setdefault("PYTHONUNBUFFERED", "1")   # real-time tee: child flushes per line
    proc = subprocess.Popen(
        argv,
        stdout=PIPE,
        stderr=STDOUT,
        text=True,
        bufsize=1,
        cwd=cwd,
        env=_env,
    )
    if proc.stdout is None:
        raise RuntimeError("tee_run: subprocess stdout pipe was not created")
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        log_handle.write(line)
        log_handle.flush()
    return proc.wait()


# ---------------------------------------------------------------------------
# Helper: Telemetry discovery
# ---------------------------------------------------------------------------

def find_failure_telemetry(run_root: Path) -> dict | None:
    """Find the newest failure_telemetry.json under *run_root*.

    Searches ``<run_root>/**/failure_telemetry_*/failure_telemetry.json``,
    picks the newest by mtime, parses JSON, requires a ``fsm_phase`` key.
    Attaches ``_artifact_path`` (str) to the returned dict.
    Returns None if nothing valid is found.
    """
    candidates = list(run_root.glob("**/failure_telemetry_*/failure_telemetry.json"))
    if not candidates:
        return None

    # Sort newest-last, iterate in reverse
    candidates.sort(key=lambda p: p.stat().st_mtime)
    for path in reversed(candidates):
        try:
            data = json.loads(path.read_text())
            if "fsm_phase" not in data:
                continue
            data["_artifact_path"] = str(path)
            return data
        except (json.JSONDecodeError, OSError):
            continue
    return None


# ---------------------------------------------------------------------------
# Helper: [FOR CLAUDE] block formatter
# ---------------------------------------------------------------------------

def format_for_claude(
    telemetry: dict | None,
    *,
    log_path: str,
    exit_code: int,
    log_tail: list[str],
) -> str:
    """Return a fenced markdown block the operator can blindly copy-paste."""
    lines: list[str] = []
    lines.append("```markdown")
    lines.append("[FOR CLAUDE] A1 soak failed")
    lines.append("")
    lines.append(f"exit_code: {exit_code}")
    lines.append(f"verdict:   FAILED")
    lines.append(f"log_path:  {log_path}")
    lines.append("")

    if telemetry is None:
        lines.append("[!] No failure_telemetry.json found under run-root.")
        lines.append("    The soak may have crashed before writing telemetry,")
        lines.append("    or --run-root was not passed to the driver.")
    else:
        artifact = telemetry.get("_artifact_path", "(unknown)")
        lines.append(f"artifact_path: {artifact}")
        lines.append("")

        fsm_phase = telemetry.get("fsm_phase")
        lines.append(f"fsm_phase: {fsm_phase}")
        lines.append("")

        # Causal chain
        causal_chain = telemetry.get("causal_chain")
        if causal_chain:
            lines.append("causal_chain:")
            for hop in causal_chain:
                seq = hop.get("seq", "?")
                parent = hop.get("causal_parent_seq", "?")
                rest = {k: v for k, v in hop.items() if k not in ("seq", "causal_parent_seq")}
                rest_str = "  " + str(rest) if rest else ""
                lines.append(f"  seq={seq} -> parent={parent}{rest_str}")
        else:
            lines.append("causal_chain: (none)")
        lines.append("")

        # Memory snapshot
        mem = telemetry.get("memory_snapshot")
        if mem:
            lines.append("memory_snapshot:")
            for k, v in mem.items():
                lines.append(f"  {k}: {v}")
        else:
            lines.append("memory_snapshot: (none)")
        lines.append("")

        # A1 trace hops
        hops = telemetry.get("a1trace_hops")
        if hops:
            lines.append(f"a1trace_hops ({len(hops)} total):")
            for h in hops:
                lines.append(f"  {h}")
        else:
            lines.append("a1trace_hops: (none)")
        lines.append("")

    # Log tail
    lines.append(f"--- last {len(log_tail)} lines of log ---")
    lines.extend(log_tail)
    lines.append("```")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Log helpers
# ---------------------------------------------------------------------------

def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _git_head(cwd: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            cwd=str(cwd),
            timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else "(unknown)"
    except Exception:
        return "(unknown)"


def _write_log_header(fh, *, argv: list[str], cwd: Path) -> None:
    stamp = datetime.now(timezone.utc).isoformat()
    fh.write(f"# A1 Ignition Harness -- {stamp}\n")
    fh.write(f"# git HEAD: {_git_head(cwd)}\n")
    fh.write(f"# command: {' '.join(argv)}\n")
    fh.write("#\n")
    fh.flush()


def _read_tail(log_path: Path, n: int = 40) -> list[str]:
    try:
        lines = log_path.read_text(errors="replace").splitlines()
        return lines[-n:]
    except OSError:
        return ["(log unreadable)"]


# ---------------------------------------------------------------------------
# Helpers: live-failover
# ---------------------------------------------------------------------------

def _arm_failover_env(env: dict) -> dict:
    """Stamp all JARVIS_FAILOVER_* keys into *env* for a live-failover run.

    Returns the same dict (mutated in place).  GCP_PROJECT_ID / GCP_ZONE are
    NOT set here -- they come from the operator's exported environment via the
    caller's os.environ copy.
    """
    env.update({
        # Failover mesh armed: DW primary -> J-Prime 32B golden-image fallback
        "JARVIS_FAILOVER_LIFECYCLE_ENABLED": "true",
        "JARVIS_FAILOVER_BUDGET_AWAKEN_ENABLED": "true",
        "JARVIS_FAILOVER_VIOLENT_TEARDOWN_ENABLED": "true",
        "JARVIS_FAILOVER_HEADER_AWARE_RECOVERY_ENABLED": "true",
        # Force the 32B QUALITY GPU tier (g2-standard-8 + nvidia-l4 + jarvis-prime-coder-32b).
        # Host bump 4->8: g2-standard-4 has only 16GB system RAM, but the 32B GGUF is
        # ~19.85GB -> llama-server was kernel-OOM-killed loading weights into RAM
        # (BEFORE the L4 VRAM was ever used). g2-standard-8 = 32GB RAM > 19.85GB +
        # 4GB overhead; SAME nvidia-l4 GPU + SAME model (no GPU/quality change). The
        # RAM Pre-Flight Gate now asserts this mathematically before instances.insert.
        "JARVIS_FAILOVER_QUALITY_TIER_ENABLED": "true",
        "JARVIS_FAILOVER_AWAKEN_URGENCY": "immediate",
        "JARVIS_FAILOVER_QUALITY_MACHINE": "g2-standard-8",
        "JARVIS_FAILOVER_QUALITY_ACCEL_TYPE": "nvidia-l4",
        "JARVIS_FAILOVER_QUALITY_ACCEL_COUNT": "1",
        "JARVIS_FAILOVER_QUALITY_IMAGE": "jarvis-prime-coder-32b",
        "JARVIS_FAILOVER_INFERENCE_BIND_ENABLED": "true",
        # Semantic Pre-Flight FSM boundary: a HEAVY (32B/GPU) tier is only legally
        # SERVING after a CONFIRMED warmup (loads ~20GB into L4 VRAM with the RIGHT
        # model, via /api/tags). A failed warmup keeps the FSM in AWAKENING (bounded
        # by the outer deadline) instead of advertising a cold node whose first op
        # times out and cascades. Default TRUE; set explicit for the soak record.
        "JARVIS_FAILOVER_WARMUP_STRICT": "true",
        # Absolute Route Sealing: once the router commits to the sovereign 32B, a
        # failure/timeout HALTS the op (terminal) -- it must NEVER cascade to the
        # dead DW / synthetic-adversary lane (the cascade leak that retry-stalled
        # the last soak). The 32B is the designated provider here, not a bonus.
        "JARVIS_FAILOVER_ABSOLUTE_ROUTE_SEALING": "true",
    })
    return env


def _build_soak_argv(repo_root: Path, run_root: Path, *, live_failover: bool) -> list[str]:
    """Build the soak driver argv.  Appends ``--enable-failover`` when *live_failover*."""
    argv = [
        sys.executable,
        str(repo_root / "scripts" / "isomorphic_a1_local.py"),
        "--mode", "container",
        "--run-root", str(run_root),
    ]
    if live_failover:
        argv.append("--enable-failover")
    return argv


def _load_preflight_fn(scripts_dir: Path):
    """Return ``preflight_gcp_ready`` from the sibling a1_gcp_preflight module.

    Tries a direct import first (works when scripts/ is on sys.path), then
    falls back to importlib path-loading so this script works as __main__ too.
    Never raises -- callers should catch ImportError.
    """
    try:
        from a1_gcp_preflight import preflight_gcp_ready  # noqa: PLC0415
        return preflight_gcp_ready
    except Exception:
        pass
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location(
        "a1_gcp_preflight",
        scripts_dir / "a1_gcp_preflight.py",
    )
    if _spec is None or _spec.loader is None:
        raise ImportError("Cannot load a1_gcp_preflight.py from " + str(scripts_dir))
    _mod = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)  # type: ignore[union-attr]
    return _mod.preflight_gcp_ready


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------

def main() -> int:  # noqa: C901 -- intentionally linear top-level flow
    parser = argparse.ArgumentParser(
        description="Adaptive Ignition Harness: fire A1 live soak with docker pre-flight and autonomous failure summary.",
    )
    parser.add_argument(
        "--max-wall-seconds",
        type=int,
        default=2400,
        metavar="N",
        help="Hard wall-clock ceiling passed to the soak driver (default: 2400).",
    )
    parser.add_argument(
        "--skip-preflight",
        action="store_true",
        help="Skip the integration test pre-flight (tests/integration/test_iron_triad_live_pipeline.py).",
    )
    parser.add_argument(
        "--run-root",
        type=Path,
        default=None,
        metavar="DIR",
        help="Directory under which the soak driver writes its run_id/ tree. "
             "Default: <repo>/logs/a1_runs/<stamp>.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Stub real subprocesses -- exercises wiring without Docker or soak.",
    )
    parser.add_argument(
        "--live-failover",
        action="store_true",
        help=(
            "Awaken the GCP J-Prime 32B golden image as DW's fallback for a REAL "
            "autonomous-PR run (real bounded GPU spend; runs the $0 credential "
            "preflight first)."
        ),
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]

    # ------------------------------------------------------------------ stamp
    stamp = _utc_stamp()
    run_root: Path = args.run_root if args.run_root is not None else repo_root / "logs" / "a1_runs" / stamp

    # ------------------------------------------------------------------ logs dir
    logs_dir = repo_root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / f"a1_ignition_{stamp}.log"

    # ------------------------------------------------------------------ dry-run stubs
    _scripts_dir = Path(__file__).resolve().parent
    if args.dry_run:
        print("[DRY-RUN] Adaptive Ignition Harness (no real subprocesses)")
        print(f"[DRY-RUN] log_file   : {log_file}")
        print(f"[DRY-RUN] run_root   : {run_root}")
        print(f"[DRY-RUN] max-wall-s : {args.max_wall_seconds}")
        print(f"[DRY-RUN] preflight  : {'SKIP' if args.skip_preflight else 'would run'}")
        print("[DRY-RUN] Docker check: STUBBED -> OK")
        if args.live_failover:
            print("[>] live-failover: running $0 GCP credential preflight ...")
            try:
                import asyncio as _aio
                _preflight_fn = _load_preflight_fn(_scripts_dir)
                _ok, _problems = _aio.run(_preflight_fn())
            except Exception as _exc:
                print(f"[!] live-failover preflight raised unexpectedly: {_exc}")
                return 2
            if not _ok:
                print("\n[!] live-failover ABORTED at $0 -- GCP not ready (no node was created).")
                print("    Run these in THIS terminal, then re-run with --live-failover:\n")
                for _p in _problems:
                    print("    " + _p.replace("\n", "\n    "))
                return 2
            print("[OK] GCP preflight green.")
            _armed = _arm_failover_env({})
            print("[DRY-RUN] live-failover env armed:")
            for _k, _v in sorted(_armed.items()):
                print(f"  {_k}={_v}")
            _argv_preview = _build_soak_argv(repo_root, run_root, live_failover=True)
            print(f"[DRY-RUN] soak argv: {' '.join(_argv_preview)}")
        print("[DRY-RUN] Soak       : STUBBED -> exit 0 (A1_DISPATCH_PROVEN)")
        print("")
        print("[OK] A1_DISPATCH_PROVEN (dry-run stub)")
        return 0

    # ------------------------------------------------------------------ docker check
    ok, reason = docker_responsive()
    if not ok:
        print(
            f"\n  Docker daemon is not responding -- please open Docker Desktop and wait for it to start, then re-run.\n"
            f"  Reason: {reason}\n"
        )
        return 2

    print(f"[OK] Docker daemon: {reason}")

    # ------------------------------------------------------------------ GCP preflight (live-failover only)
    if args.live_failover:
        print("[>] live-failover: running $0 GCP credential preflight ...")
        try:
            import asyncio as _aio
            _preflight_fn = _load_preflight_fn(_scripts_dir)
            _ok, _problems = _aio.run(_preflight_fn())
        except Exception as _exc:
            print(f"[!] live-failover preflight raised unexpectedly: {_exc}")
            return 2
        if not _ok:
            print("\n[!] live-failover ABORTED at $0 -- GCP not ready (no node was created).")
            print("    Run these in THIS terminal, then re-run with --live-failover:\n")
            for _p in _problems:
                print("    " + _p.replace("\n", "\n    "))
            return 2
        print("[OK] GCP preflight green -- proceeding to awaken the 32B fallback.")

    # ------------------------------------------------------------------ open log
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_fh = log_file.open("w", encoding="utf-8", errors="replace")

    try:
        # ---------------------------------------------------------------- preflight
        if not args.skip_preflight:
            preflight_argv = [
                sys.executable,
                "-m",
                "pytest",
                "tests/integration/test_iron_triad_live_pipeline.py",
                "-q",
            ]
            _write_log_header(log_fh, argv=preflight_argv, cwd=repo_root)
            print(f"\n[>] Preflight: {' '.join(preflight_argv)}")
            pf_rc = tee_run(preflight_argv, log_fh, cwd=str(repo_root))
            if pf_rc != 0:
                print(f"\n[!] Preflight FAILED (exit {pf_rc}) -- aborting before expensive soak.")
                log_fh.write(f"\n# Preflight FAILED -- soak skipped\n")
                log_fh.flush()
                return 3
            print("[OK] Preflight passed.\n")
        else:
            print("[SKIP] Preflight skipped (--skip-preflight).")

        # ---------------------------------------------------------------- soak
        run_root.mkdir(parents=True, exist_ok=True)
        soak_argv = _build_soak_argv(repo_root, run_root, live_failover=args.live_failover)
        # The isomorphic driver does NOT take --max-wall-seconds; the wall cap is
        # read from the env by the battle-test harness the SoakRunner spawns
        # (OUROBOROS_BATTLE_MAX_WALL_SECONDS). Headless too, since this is non-TTY.
        _soak_env = dict(os.environ)
        _soak_env["OUROBOROS_BATTLE_MAX_WALL_SECONDS"] = str(args.max_wall_seconds)
        _soak_env.setdefault("OUROBOROS_BATTLE_HEADLESS", "1")
        if args.live_failover:
            _arm_failover_env(_soak_env)
        _write_log_header(log_fh, argv=soak_argv, cwd=repo_root)
        print(f"[>] Soak: {' '.join(soak_argv)}  (OUROBOROS_BATTLE_MAX_WALL_SECONDS={args.max_wall_seconds})")
        soak_rc = tee_run(soak_argv, log_fh, cwd=str(repo_root), env=_soak_env)

        # ---------------------------------------------------------------- result
        if soak_rc == 0:
            print("\n[OK] A1_DISPATCH_PROVEN")
            log_fh.write("\n# A1_DISPATCH_PROVEN\n")
            return 0

        # Non-zero -- locate telemetry and emit [FOR CLAUDE] block
        telemetry = find_failure_telemetry(run_root)
        log_tail = _read_tail(log_file)
        summary = format_for_claude(
            telemetry,
            log_path=str(log_file),
            exit_code=soak_rc,
            log_tail=log_tail,
        )
        print(summary)
        log_fh.write("\n")
        log_fh.write(summary)
        log_fh.write("\n")
        log_fh.flush()
        return soak_rc

    finally:
        log_fh.close()


if __name__ == "__main__":
    sys.exit(main())
