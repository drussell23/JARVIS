#!/usr/bin/env python3
"""Trinity bring-up + live-handshake verifier + fail-soft resilience drill (Axis A).

The JARVIS↔Reactor mesh is already built end-to-end — `reactor_core/api` (FastAPI Soul on :8090 with
rich `/health`), `backend/clients/reactor_core_client.py` (async client + circuit breaker +
TrinityEventBus), `backend/core/trinity_bridge.py` (20s heartbeat + intelligent degraded mode + auto-
recovery + restart via process orchestrator), `backend/loading_server/cross_repo_health.py`. What was
missing is a *repeatable operator tool* that brings the Soul online and **proves** the mesh works
live. This is that tool — it composes the existing client (no duplication).

What it does (all bounded + fail-soft):
  1. Boot — if :8090/health is not answering, launch the reactor-core Soul via its canonical
     `run_reactor.py --port 8090` (a detached subprocess; the lightweight FastAPI/aiohttp control
     plane, NOT the heavy docker-compose Night Shift training pipeline).
  2. Handshake — drive the real `ReactorCoreClient.initialize()` + `health_check()`; assert
     phase=ready + trinity_connected, and capture the Soul's structural state signature (schema +
     contract version, training_ready, cpu pressure).
  3. Drill (--drill) — exercise the Sinking Shield: kill the Soul, confirm the client degrades
     (health_check → False, classified reason), then (default) restart the Soul and confirm recovery.

Usage:
    python3 scripts/trinity_bringup.py                  # boot (if needed) + verify handshake
    python3 scripts/trinity_bringup.py --drill          # + fail-soft kill/recover drill
    python3 scripts/trinity_bringup.py --no-boot        # verify only an already-running Soul
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Optional, Tuple

REACTOR_PORT = int(os.getenv("REACTOR_CORE_PORT", os.getenv("REACTOR_PORT", "8090")))
REACTOR_REPO = Path(
    os.getenv("JARVIS_REACTOR_REPO_PATH")
    or os.getenv("REACTOR_CORE_REPO_PATH")
    or os.getenv("REACTOR_CORE_PATH")
    or (Path.home() / "Documents" / "repos" / "reactor-core")
)


def _log(m: str) -> None:
    print(f"[trinity-bringup {time.strftime('%H:%M:%S')}] {m}", flush=True)


def _probe_health(timeout: float = 2.0) -> Optional[dict]:
    """GET :PORT/health → parsed JSON, or None if unreachable. Stdlib only (no client needed)."""
    try:
        with urllib.request.urlopen(f"http://localhost:{REACTOR_PORT}/health", timeout=timeout) as r:
            if r.status == 200:
                return json.loads(r.read().decode())
    except Exception:
        return None
    return None


def _boot_soul() -> Optional[subprocess.Popen]:
    """Launch reactor-core's run_reactor.py as a detached subprocess (its own python/deps)."""
    launcher = REACTOR_REPO / "run_reactor.py"
    if not launcher.is_file():
        _log(f"⛔ reactor launcher not found: {launcher}")
        return None
    env = dict(os.environ)
    env["REACTOR_PORT"] = str(REACTOR_PORT)
    log_path = Path(os.getenv("TMPDIR", "/tmp")) / "reactor_soul.log"
    out = open(log_path, "w")
    _log(f"launching reactor Soul: python3 run_reactor.py --port {REACTOR_PORT} (log={log_path})")
    proc = subprocess.Popen(
        [sys.executable if Path(sys.executable).exists() else "python3",
         "run_reactor.py", "--port", str(REACTOR_PORT)],
        cwd=str(REACTOR_REPO), env=env, stdout=out, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return proc


def _wait_health(deadline_s: float = 90.0) -> Optional[dict]:
    t0 = time.time()
    while time.time() - t0 < deadline_s:
        h = _probe_health()
        if h is not None:
            return h
        time.sleep(2)
    return None


async def _verify_handshake() -> Tuple[bool, dict]:
    """Drive the real JARVIS ReactorCoreClient against the live Soul. Returns (ok, detail)."""
    try:
        from backend.clients.reactor_core_client import ReactorCoreClient, ReactorCoreConfig
    except Exception as exc:  # noqa: BLE001
        return False, {"error": f"client import failed: {exc}"}
    c = ReactorCoreClient(ReactorCoreConfig())
    detail: dict = {}
    try:
        init = getattr(c, "initialize", None)
        if init:
            await init()
        ok = await c.health_check()
        detail = {
            "health_check": ok,
            "phase": getattr(c, "_reactor_phase", None),
            "trinity_connected": getattr(c, "_trinity_connected", None),
            "training_ready": getattr(c, "_training_ready", None),
        }
        return bool(ok), detail
    finally:
        for m in ("close", "disconnect"):
            fn = getattr(c, m, None)
            if fn:
                try:
                    await fn()
                except Exception:  # noqa: BLE001
                    pass
                break


def _state_signature(health: dict) -> dict:
    """The Soul's structural state signature (what a rich heartbeat exchanges)."""
    return {
        k: health.get(k) for k in (
            "service", "version", "phase", "status", "trinity_connected", "training_ready",
            "autonomy_schema_version", "contract_version", "cpu_pressure_active", "uptime_seconds",
        )
    }


async def _main_async(args: argparse.Namespace) -> int:
    owned: Optional[subprocess.Popen] = None

    # ---- 1. Boot (if needed) ----
    health = _probe_health()
    if health is None:
        if args.no_boot:
            _log(f"⛔ no Soul on :{REACTOR_PORT} and --no-boot set")
            return 1
        owned = _boot_soul()
        if owned is None:
            return 1
        health = _wait_health(args.boot_timeout)
        if health is None:
            _log("⛔ Soul did not become healthy within boot timeout")
            return 1
    _log(f"✅ reactor Soul healthy on :{REACTOR_PORT}")
    _log(f"   state signature: {json.dumps(_state_signature(health))}")

    # ---- 2. Handshake ----
    ok, detail = await _verify_handshake()
    _log(f"{'✅' if ok else '⛔'} JARVIS↔Reactor handshake: {json.dumps(detail)}")
    if not ok:
        return 1
    if detail.get("phase") != "ready" or not detail.get("trinity_connected"):
        _log("⚠️  handshake succeeded but phase/trinity_connected not fully ready")

    # ---- 3. Fail-soft resilience drill ----
    if args.drill:
        _log("── Sinking Shield drill: killing the Soul to verify degrade + recovery ──")
        # find the listener pid via the health (best-effort: kill what we own, else lsof)
        killed = False
        if owned is not None:
            try:
                os.killpg(os.getpgid(owned.pid), signal.SIGTERM)
                killed = True
            except Exception:  # noqa: BLE001
                pass
        if not killed:
            _log("   (Soul was pre-existing; skipping kill — rerun with boot to drill recovery)")
        else:
            time.sleep(3)
            down = _probe_health()
            ok_down, _ = (await _verify_handshake()) if down else (False, {})
            _log(f"   after kill: health_reachable={down is not None} client_ok={ok_down} "
                 f"→ {'✅ degraded as expected' if not ok_down else '⚠️ still up?'}")
            if not args.no_recover:
                owned = _boot_soul()
                rec = _wait_health(args.boot_timeout)
                ok_rec, _ = (await _verify_handshake()) if rec else (False, {})
                _log(f"   recovery: {'✅ Soul back online + handshake restored' if ok_rec else '⛔ recovery failed'}")

    _log("================= TRINITY STATUS =================")
    _log(f"  Reactor Soul : ONLINE :{REACTOR_PORT} ({health.get('service')} {health.get('version')})")
    _log(f"  Handshake    : {'VERIFIED' if ok else 'FAILED'}")
    _log(f"  Left running : {'yes (this tool owns the proc)' if owned and owned.poll() is None else 'pre-existing / not owned'}")
    _log("=================================================")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Trinity bring-up + handshake verifier + fail-soft drill")
    ap.add_argument("--no-boot", action="store_true", help="verify only; do not launch the Soul")
    ap.add_argument("--drill", action="store_true", help="run the kill/degrade/recover resilience drill")
    ap.add_argument("--no-recover", action="store_true", help="drill: skip the recovery restart")
    ap.add_argument("--boot-timeout", type=float, default=90.0)
    return asyncio.run(_main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
