#!/usr/bin/env python3
"""Slice 3 live-fire: /governor REPL + IDE GET + SSE on real server.

  1. All 6 /governor REPL subcommands dispatch on real state
  2. Real EventChannelServer boots with governor + memory surfaces
  3. GET /observability/governor returns snapshot
  4. GET /observability/governor/history returns decisions
  5. GET /observability/memory-pressure returns probe
  6. Raw-socket SSE receives governor_throttle_applied on saturation
  7. Raw-socket SSE receives memory_pressure_changed on transition
  8. Master-off revert: all GETs 403; SSE publishes drop
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
                or k.startswith("JARVIS_MEMORY_PRESSURE")):
            del os.environ[k]


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _parse_sse(body):
    text = body.decode("utf-8", errors="replace")
    frames = []
    for block in text.split("\n\n"):
        lines = [ln.strip("\r") for ln in block.split("\n") if ln.strip()]
        clean = [ln for ln in lines
                 if not (ln and all(c in "0123456789abcdefABCDEF" for c in ln))]
        frame = {}
        for ln in clean:
            if ln.startswith(":"):
                continue
            if ":" in ln:
                k, _, v = ln.partition(":")
                frame[k.strip()] = v.strip()
        if frame:
            frames.append(frame)
    return frames


async def _sse_subscribe(port, timeout_s=3.0):
    loop = asyncio.get_event_loop()

    def _reader():
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=timeout_s)
            sock.sendall((
                f"GET /observability/stream HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                "Accept: text/event-stream\r\nConnection: close\r\n\r\n"
            ).encode())
            sock.settimeout(timeout_s)
            buf = b""
            deadline = time.monotonic() + timeout_s
            while b"\r\n\r\n" not in buf and time.monotonic() < deadline:
                try:
                    ch = sock.recv(4096)
                except socket.timeout:
                    break
                if not ch:
                    return b""
                buf += ch
            if b"\r\n\r\n" not in buf:
                return b""
            _h, rest = buf.split(b"\r\n\r\n", 1)
            body = rest
            while time.monotonic() < deadline:
                try:
                    ch = sock.recv(4096)
                except socket.timeout:
                    break
                if not ch:
                    break
                body += ch
            try:
                sock.close()
            except Exception:
                pass
            return body
        except Exception as exc:
            return str(exc).encode()

    body = await loop.run_in_executor(None, _reader)
    return _parse_sse(body)


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
                         and "chunked" in h.lower()
                         for h in lines[1:])
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
    print("SensorGovernor + MemoryPressureGate — Slice 3 Live-Fire")
    print("=" * 72)
    _scrub()
    os.environ["JARVIS_IDE_OBSERVABILITY_ENABLED"] = "true"
    os.environ["JARVIS_IDE_STREAM_ENABLED"] = "true"
    os.environ["JARVIS_EVENT_CHANNELS_ENABLED"] = "true"
    os.environ["JARVIS_SENSOR_GOVERNOR_ENABLED"] = "true"
    os.environ["JARVIS_MEMORY_PRESSURE_GATE_ENABLED"] = "true"

    from backend.core.ouroboros.governance.sensor_governor import (
        SensorBudgetSpec, SensorGovernor, Urgency,
        ensure_seeded, reset_default_governor,
    )
    from backend.core.ouroboros.governance.memory_pressure_gate import (
        MemoryPressureGate, MemoryProbe, PressureLevel,
        reset_default_gate,
    )
    from backend.core.ouroboros.governance.governor_repl import (
        dispatch_governor_command,
    )
    from backend.core.ouroboros.governance.ide_observability_stream import (
        bridge_governor_to_broker, bridge_memory_pressure_to_broker,
        reset_default_broker,
    )
    from backend.core.ouroboros.governance.event_channel import (
        EventChannelServer,
    )

    reset_default_governor()
    reset_default_gate()
    reset_default_broker()

    governor = ensure_seeded()

    checks = []

    # (1) REPL subcommands
    for name, line, needle in [
        ("status", "/governor status", "Sensors"),
        ("bare-alias", "/governor", "Sensors"),
        ("explain", "/governor explain", "Sensor budgets"),
        ("history-empty", "/governor history", "recent"),
        ("help", "/governor help", "/governor"),
    ]:
        r = dispatch_governor_command(line, governor=governor)
        ok = r.ok and needle.lower() in r.text.lower()
        checks.append((f"REPL {name}: {needle!r} in output", ok))

    # /governor memory
    from backend.core.ouroboros.governance.memory_pressure_gate import (
        get_default_gate,
    )
    gate = get_default_gate()
    r = dispatch_governor_command("/governor memory", gate=gate)
    checks.append(("REPL memory shows fanout projection",
                   r.ok and "Fanout projection" in r.text))

    # /governor reset
    governor.record_emission("TestFailureSensor")
    r = dispatch_governor_command("/governor reset", governor=governor)
    checks.append(("REPL reset clears counters", r.ok))
    d = governor.request_budget("TestFailureSensor")
    checks.append(("REPL reset: count now 0 after reset",
                   d.current_count == 0))

    # (2) Boot server + GETs
    class _StubRouter:
        async def submit(self, *a, **k):
            return None

    port = _free_port()
    server = EventChannelServer(
        router=_StubRouter(), port=port, host="127.0.0.1",
    )
    await server.start()
    await asyncio.sleep(0.3)

    try:
        async def _aget(p):
            return await asyncio.to_thread(_http_get, port, p)

        st, h = await _aget("/channel/health")
        checks.append(("/channel/health 200", st == 200))

        # GET /observability/governor
        st, payload = await _aget("/observability/governor")
        checks.append(("GET /governor 200", st == 200))
        if st == 200:
            checks.append(("GET /governor has 16 sensors",
                           len(payload.get("sensors", [])) == 16))
            checks.append(("GET /governor schema=1.0",
                           payload.get("schema_version") == "1.0"))

        # GET /observability/governor/history
        st, payload = await _aget("/observability/governor/history?limit=5")
        checks.append(("GET /governor/history 200", st == 200))
        if st == 200:
            checks.append(("GET /governor/history schema=1.0",
                           payload.get("schema_version") == "1.0"))
            checks.append(("GET /governor/history limit respected",
                           payload.get("limit") == 5))

        # GET /observability/memory-pressure
        st, payload = await _aget("/observability/memory-pressure")
        checks.append(("GET /memory-pressure 200", st == 200))
        if st == 200:
            print(f"[memory] level={payload.get('level')} "
                  f"source={payload.get('probe', {}).get('source')}")
            checks.append(("GET /memory-pressure schema=1.0",
                           payload.get("schema_version") == "1.0"))
            checks.append(("GET /memory-pressure has probe",
                           "probe" in payload))
            checks.append(("GET /memory-pressure has level",
                           "level" in payload))

        # (6) SSE throttle bridge
        # Register a tiny-cap sensor
        governor.register(SensorBudgetSpec(
            sensor_name="LiveFireTinySensor", base_cap_per_hour=1,
        ))
        bridge_governor_to_broker(governor=governor)

        sse_task = asyncio.create_task(_sse_subscribe(port, timeout_s=3.0))
        await asyncio.sleep(0.4)
        # Allow first + saturate
        governor.record_emission("LiveFireTinySensor")
        # Now next request will be denied → publishes throttle event
        governor.request_budget("LiveFireTinySensor")
        frames = await sse_task
        throttle_frames = []
        for f in frames:
            if "data" in f:
                try:
                    d = json.loads(f["data"])
                    if d.get("event_type") == "governor_throttle_applied":
                        throttle_frames.append(d)
                except (json.JSONDecodeError, TypeError):
                    pass
        print(f"[SSE] {len(throttle_frames)} governor_throttle frame(s)")
        checks.append(("SSE governor_throttle_applied received",
                       len(throttle_frames) >= 1))
        if throttle_frames:
            p = throttle_frames[0].get("payload", {})
            checks.append(("throttle frame sensor=LiveFireTinySensor",
                           p.get("sensor_name") == "LiveFireTinySensor"))

        # (7) SSE memory pressure transition
        # Build a gate with controllable probe
        states = [50.0, 5.0]  # OK → CRITICAL
        def _probe():
            pct = states.pop(0) if states else 5.0
            return MemoryProbe(
                free_pct=pct, total_bytes=16 * (1024**3),
                available_bytes=int(pct * 16 * (1024**3) / 100),
                source="test",
            )
        test_gate = MemoryPressureGate(probe_fn=_probe)
        bridge_memory_pressure_to_broker(gate=test_gate)

        sse_task2 = asyncio.create_task(_sse_subscribe(port, timeout_s=3.0))
        await asyncio.sleep(0.3)
        # Prime (OK, no publish since prev=None), then transition to CRITICAL
        test_gate.pressure()
        test_gate.pressure()
        frames2 = await sse_task2
        pressure_frames = []
        for f in frames2:
            if "data" in f:
                try:
                    d = json.loads(f["data"])
                    if d.get("event_type") == "memory_pressure_changed":
                        pressure_frames.append(d)
                except (json.JSONDecodeError, TypeError):
                    pass
        print(f"[SSE] {len(pressure_frames)} memory_pressure frame(s)")
        checks.append(("SSE memory_pressure_changed on transition",
                       len(pressure_frames) >= 1))
        if pressure_frames:
            p = pressure_frames[0].get("payload", {})
            checks.append(("pressure frame previous_level=ok",
                           p.get("previous_level") == "ok"))
            checks.append(("pressure frame current_level=critical",
                           p.get("current_level") == "critical"))

        # (8) Master-off revert
        os.environ["JARVIS_SENSOR_GOVERNOR_ENABLED"] = "false"
        st, _ = await _aget("/observability/governor")
        checks.append(("master=false governor 403", st == 403))
        os.environ["JARVIS_MEMORY_PRESSURE_GATE_ENABLED"] = "false"
        st, _ = await _aget("/observability/memory-pressure")
        checks.append(("master=false memory-pressure 403", st == 403))

        # Re-enable
        os.environ["JARVIS_SENSOR_GOVERNOR_ENABLED"] = "true"
        os.environ["JARVIS_MEMORY_PRESSURE_GATE_ENABLED"] = "true"
        st, _ = await _aget("/observability/governor")
        checks.append(("re-enable governor 200", st == 200))

    finally:
        await server.stop()

    # Authority invariants
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

    pass_log = REPO_ROOT / "scripts" / "livefire_sensor_governor_slice3_PASS.log"
    fail_log = REPO_ROOT / "scripts" / "livefire_sensor_governor_slice3_FAIL.log"
    log_path = pass_log if all_pass else fail_log
    other = fail_log if all_pass else pass_log
    if other.exists():
        other.unlink()

    artifact = {
        "slice": 3,
        "feature": "/governor REPL + IDE GET + SSE",
        "timestamp": time.time(),
        "all_pass": all_pass,
        "total_checks": len(checks),
        "checks": [{"name": n, "pass": ok} for n, ok in checks],
    }
    log_path.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    print(f"Artifact: {log_path}")

    if all_pass:
        print(f"\n  RESULT: PASS  —  {len(checks)}/{len(checks)} checks green.")
        return 0
    print("\n  RESULT: FAIL  —  check the log artifact.")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
