#!/usr/bin/env python3
"""Slice 4 graduation live-fire: zero-env boot + full-revert matrix.

Phase 1 — Graduated boot (ZERO governor/gate env vars):
  Both primitives active, all surfaces wire themselves.

Phase 2 — Full-revert matrix:
  Flip each master to false → respective surfaces revert in lockstep.

Phase 3 — Bidirectional re-default.

Phase 4 — Authority invariants on all 4 arc files.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import socket
import sys
import time
from typing import Dict, List

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _scrub():
    for k in list(os.environ):
        if (k.startswith("JARVIS_SENSOR_GOVERNOR")
                or k.startswith("JARVIS_MEMORY_PRESSURE")
                or k.startswith("JARVIS_HELP_DISPATCHER")
                or k.startswith("JARVIS_FLAG_REGISTRY")):
            del os.environ[k]


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _http_get(port, path, timeout_s=5.0):
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=timeout_s)
    except Exception as exc:
        return 0, {"_error": str(exc)}
    try:
        sock.settimeout(timeout_s)
        sock.sendall((
            f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
            f"Origin: http://127.0.0.1:{port}\r\nConnection: close\r\n\r\n"
        ).encode())
        buf = b""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                ch = sock.recv(4096)
            except socket.timeout:
                break
            if not ch:
                break
            buf += ch
        if b"\r\n\r\n" not in buf:
            return 0, {"_error": "no response"}
        hdr, body = buf.split(b"\r\n\r\n", 1)
        lines = hdr.decode("iso-8859-1").split("\r\n")
        parts = (lines[0] if lines else "").split(" ", 2)
        try:
            status = int(parts[1]) if len(parts) >= 2 else 0
        except ValueError:
            status = 0
        is_chunked = any(h.lower().startswith("transfer-encoding:")
                         and "chunked" in h.lower() for h in lines[1:])
        text = body.decode("utf-8", errors="replace")
        if is_chunked:
            text = "".join(ln for ln in text.split("\r\n")
                           if not (ln and all(c in "0123456789abcdefABCDEF" for c in ln)))
        try:
            return status, json.loads(text)
        except json.JSONDecodeError:
            return status, {"_raw": text[:200]}
    finally:
        try:
            sock.close()
        except Exception:
            pass


async def amain() -> int:
    print("=" * 72)
    print("SensorGovernor + MemoryPressureGate — Slice 4 GRADUATION Live-Fire")
    print("=" * 72)

    _scrub()
    os.environ["JARVIS_IDE_OBSERVABILITY_ENABLED"] = "true"
    os.environ["JARVIS_IDE_STREAM_ENABLED"] = "true"
    os.environ["JARVIS_EVENT_CHANNELS_ENABLED"] = "true"

    checks = []

    from backend.core.ouroboros.governance.sensor_governor import (
        ensure_seeded, is_enabled as _gov_enabled, reset_default_governor,
        Urgency,
    )
    from backend.core.ouroboros.governance.memory_pressure_gate import (
        ensure_bridged, is_enabled as _gate_enabled, reset_default_gate,
        get_default_gate,
    )
    from backend.core.ouroboros.governance.governor_repl import (
        dispatch_governor_command,
    )
    from backend.core.ouroboros.governance.event_channel import (
        EventChannelServer,
    )

    # Phase 1 — Graduated defaults
    print("\n--- Phase 1 — Graduated defaults ---")

    checks.append(("governor is_enabled() True post-graduation",
                   _gov_enabled() is True))
    checks.append(("gate is_enabled() True post-graduation",
                   _gate_enabled() is True))

    reset_default_governor()
    reset_default_gate()
    gov = ensure_seeded()
    gate = get_default_gate()
    ensure_bridged()

    checks.append(("seed installed 16 sensors",
                   len(gov.list_specs()) == 16))

    # REPL
    r = dispatch_governor_command("/governor status")
    checks.append(("REPL /governor status works on defaults", r.ok))
    r = dispatch_governor_command("/governor memory")
    checks.append(("REPL /governor memory works on defaults", r.ok))

    # Boot server
    class _StubRouter:
        async def submit(self, *a, **k):
            return None

    port = _free_port()
    server = EventChannelServer(router=_StubRouter(), port=port, host="127.0.0.1")
    await server.start()
    await asyncio.sleep(0.3)

    try:
        async def _aget(p):
            return await asyncio.to_thread(_http_get, port, p)

        st, payload = await _aget("/observability/governor")
        print(f"[GET governor] status={st} sensors={len(payload.get('sensors', []))}")
        checks.append(("GET /observability/governor 200 on defaults", st == 200))
        checks.append(("GET /governor has 16 sensors",
                       len(payload.get("sensors", [])) == 16))

        st, _ = await _aget("/observability/governor/history")
        checks.append(("GET /governor/history 200", st == 200))

        st, payload = await _aget("/observability/memory-pressure")
        print(f"[GET memory] status={st} level={payload.get('level')}")
        checks.append(("GET /memory-pressure 200 on defaults", st == 200))
        checks.append(("GET /memory-pressure has level", "level" in payload))

        # Phase 2 — Full-revert matrix
        print("\n--- Phase 2 — Full-Revert Matrix ---")

        # Governor off
        os.environ["JARVIS_SENSOR_GOVERNOR_ENABLED"] = "false"
        checks.append(("governor off: is_enabled() False",
                       _gov_enabled() is False))
        r = dispatch_governor_command("/governor status")
        checks.append(("governor off: /governor status rejected",
                       not r.ok and "SensorGovernor disabled" in r.text))
        r_help = dispatch_governor_command("/governor help")
        checks.append(("governor off: /governor help still works", r_help.ok))
        st, _ = await _aget("/observability/governor")
        checks.append(("governor off: GET /governor 403", st == 403))
        st, _ = await _aget("/observability/governor/history")
        checks.append(("governor off: GET /governor/history 403", st == 403))

        # Governor back
        del os.environ["JARVIS_SENSOR_GOVERNOR_ENABLED"]

        # Gate off
        os.environ["JARVIS_MEMORY_PRESSURE_GATE_ENABLED"] = "false"
        checks.append(("gate off: is_enabled() False",
                       _gate_enabled() is False))
        r = dispatch_governor_command("/governor memory")
        checks.append(("gate off: /governor memory rejected", not r.ok))
        st, _ = await _aget("/observability/memory-pressure")
        checks.append(("gate off: GET /memory-pressure 403", st == 403))

        # Both flags off
        os.environ["JARVIS_SENSOR_GOVERNOR_ENABLED"] = "false"
        st1, _ = await _aget("/observability/governor")
        st2, _ = await _aget("/observability/memory-pressure")
        checks.append(("both off: governor GET 403", st1 == 403))
        checks.append(("both off: memory GET 403", st2 == 403))

        # Phase 3 — Re-default
        print("\n--- Phase 3 — Re-default ---")

        del os.environ["JARVIS_SENSOR_GOVERNOR_ENABLED"]
        del os.environ["JARVIS_MEMORY_PRESSURE_GATE_ENABLED"]
        st1, _ = await _aget("/observability/governor")
        st2, _ = await _aget("/observability/memory-pressure")
        checks.append(("re-default: governor GET 200", st1 == 200))
        checks.append(("re-default: memory GET 200", st2 == 200))

    finally:
        await server.stop()

    # Phase 4 — Authority invariants
    print("\n--- Phase 4 — Authority Invariants ---")

    authority_forbidden = (
        "orchestrator", "policy", "iron_gate", "risk_tier",
        "change_engine", "candidate_generator",
    )
    for relpath in (
        "backend/core/ouroboros/governance/sensor_governor.py",
        "backend/core/ouroboros/governance/sensor_governor_seed.py",
        "backend/core/ouroboros/governance/memory_pressure_gate.py",
        "backend/core/ouroboros/governance/governor_repl.py",
    ):
        src = (REPO_ROOT / relpath).read_text(encoding="utf-8")
        bad = []
        for line in src.splitlines():
            if line.startswith(("from ", "import ")):
                for f in authority_forbidden:
                    if f".{f}" in line:
                        bad.append(line)
        checks.append((f"authority-free: {relpath}", not bad))

    # Report
    print()
    print("-" * 72)
    print(f"Checks ({len(checks)}):")
    all_pass = True
    for name, ok in checks:
        mark = "PASS" if ok else "FAIL"
        if not ok:
            all_pass = False
        print(f"  [{mark}] {name}")
    print("-" * 72)

    pass_log = REPO_ROOT / "scripts" / "livefire_sensor_governor_slice4_PASS.log"
    fail_log = REPO_ROOT / "scripts" / "livefire_sensor_governor_slice4_FAIL.log"
    log_path = pass_log if all_pass else fail_log
    other = fail_log if all_pass else pass_log
    if other.exists():
        other.unlink()

    artifact = {
        "slice": 4,
        "feature": "SensorGovernor + MemoryPressureGate GRADUATION",
        "timestamp": time.time(),
        "all_pass": all_pass,
        "total_checks": len(checks),
        "checks": [{"name": n, "pass": ok} for n, ok in checks],
    }
    log_path.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    print(f"Artifact: {log_path}")

    if all_pass:
        print(f"\n  RESULT: PASS  —  {len(checks)}/{len(checks)} graduation checks green.")
        return 0
    print("\n  RESULT: FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
