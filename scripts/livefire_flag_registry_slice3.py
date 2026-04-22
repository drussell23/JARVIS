#!/usr/bin/env python3
"""Slice 3 live-fire: flag observability on real EventChannelServer.

Boots a real aiohttp EventChannelServer on a loopback port, then:

  1. GET /observability/flags returns seeded flags (shape + count)
  2. GET /observability/flags with filter params (?category, ?posture,
     ?search, ?limit)
  3. GET /observability/flags/{name} — detail + suggestions on 404
  4. GET /observability/flags/unregistered — typo hunter
  5. GET /observability/verbs — registered REPL verbs
  6. Raw-socket SSE subscriber receives flag_typo_detected frame when
     registry.report_typos() fires with master on
  7. Raw-socket SSE subscriber receives flag_registered frame when
     a post-bridge registry.register() adds a net-new spec
  8. Master-off revert: GET returns 403, SSE publishes become no-ops
  9. Authority invariants on all 3 arc files

Exit 0 on success, 1 on any failure.
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


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _parse_sse_frames(body: bytes) -> List[Dict[str, str]]:
    text = body.decode("utf-8", errors="replace")
    frames: List[Dict[str, str]] = []
    for block in text.split("\n\n"):
        lines = [ln.strip("\r") for ln in block.split("\n") if ln.strip()]
        clean = [
            ln for ln in lines
            if not (ln and all(c in "0123456789abcdefABCDEF" for c in ln))
        ]
        frame: Dict[str, str] = {}
        for ln in clean:
            if ln.startswith(":"):
                continue
            if ":" in ln:
                k, _, v = ln.partition(":")
                frame[k.strip()] = v.strip()
        if frame:
            frames.append(frame)
    return frames


async def _subscribe_sse(host: str, port: int, timeout_s: float = 4.0) -> List[Dict[str, str]]:
    loop = asyncio.get_event_loop()

    def _reader():
        try:
            sock = socket.create_connection((host, port), timeout=timeout_s)
            req = (
                f"GET /observability/stream HTTP/1.1\r\n"
                f"Host: {host}:{port}\r\n"
                f"Accept: text/event-stream\r\nConnection: close\r\n\r\n"
            ).encode("ascii")
            sock.sendall(req)
            sock.settimeout(timeout_s)
            buf = b""
            deadline = time.monotonic() + timeout_s
            while b"\r\n\r\n" not in buf and time.monotonic() < deadline:
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    break
                if not chunk:
                    return b""
                buf += chunk
            if b"\r\n\r\n" not in buf:
                return b""
            _h, rest = buf.split(b"\r\n\r\n", 1)
            body = rest
            while time.monotonic() < deadline:
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    break
                if not chunk:
                    break
                body += chunk
            try:
                sock.close()
            except Exception:
                pass
            return body
        except Exception as exc:
            return str(exc).encode("utf-8")

    body = await loop.run_in_executor(None, _reader)
    return _parse_sse_frames(body)


def _http_get(port: int, path: str, timeout_s: float = 5.0) -> tuple:
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=timeout_s)
    except Exception as exc:
        return 0, {"_error": str(exc)}
    try:
        sock.settimeout(timeout_s)
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"Origin: http://127.0.0.1:{port}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("ascii")
        sock.sendall(req)
        buf = b""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
        if b"\r\n\r\n" not in buf:
            return 0, {"_error": "no response"}
        hdr, body = buf.split(b"\r\n\r\n", 1)
        lines = hdr.decode("iso-8859-1").split("\r\n")
        parts = (lines[0] if lines else "").split(" ", 2)
        try:
            status = int(parts[1]) if len(parts) >= 2 else 0
        except ValueError:
            status = 0
        is_chunked = any(
            h.lower().startswith("transfer-encoding:") and "chunked" in h.lower()
            for h in lines[1:]
        )
        text = body.decode("utf-8", errors="replace")
        if is_chunked:
            text = "".join(
                ln for ln in text.split("\r\n")
                if not (ln and all(c in "0123456789abcdefABCDEF" for c in ln))
            )
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
    print("FlagRegistry + /help — Slice 3 Live-Fire")
    print("=" * 72)

    # Scrub env
    for key in list(os.environ):
        if (key.startswith("JARVIS_FLAG_REGISTRY")
                or key.startswith("JARVIS_HELP_DISPATCHER")
                or key.startswith("JARVIS_FLAG_TYPO")):
            del os.environ[key]

    os.environ["JARVIS_IDE_OBSERVABILITY_ENABLED"] = "true"
    os.environ["JARVIS_IDE_STREAM_ENABLED"] = "true"
    os.environ["JARVIS_EVENT_CHANNELS_ENABLED"] = "true"
    os.environ["JARVIS_FLAG_REGISTRY_ENABLED"] = "true"

    from backend.core.ouroboros.governance.flag_registry import (
        Category, FlagSpec, FlagType, ensure_seeded, reset_default_registry,
    )
    from backend.core.ouroboros.governance.help_dispatcher import (
        reset_default_verb_registry,
    )
    from backend.core.ouroboros.governance.ide_observability_stream import (
        bridge_flag_registry_to_broker,
        reset_default_broker,
    )
    from backend.core.ouroboros.governance.event_channel import (
        EventChannelServer,
    )

    reset_default_registry()
    reset_default_verb_registry()
    reset_default_broker()
    registry = ensure_seeded()

    checks: List[tuple] = []

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
        async def _aget(path):
            return await asyncio.to_thread(_http_get, port, path)

        # Probe health first
        h_status, h_payload = await _aget("/channel/health")
        checks.append(("/channel/health responds 200", h_status == 200))

        # --- (1) GET /observability/flags ---
        status, payload = await _aget("/observability/flags")
        checks.append(("GET /observability/flags → 200", status == 200))
        if payload and isinstance(payload, dict):
            checks.append((
                f"flags payload count ({payload.get('count')})",
                payload.get("count", 0) >= 40,
            ))
            checks.append(("flags payload schema_version=1.0",
                           payload.get("schema_version") == "1.0"))

        # --- (2) Query filters ---
        st, p = await _aget("/observability/flags?category=safety")
        checks.append(("category filter returns only safety flags",
                       st == 200 and all(
                           f["category"] == "safety" for f in p.get("flags", [])
                       )))

        st, p = await _aget("/observability/flags?category=bogus")
        checks.append(("malformed category → 400", st == 400))

        st, p = await _aget("/observability/flags?posture=HARDEN")
        checks.append(("posture=HARDEN returns HARDEN-tagged flags",
                       st == 200 and p.get("count", 0) >= 5
                       and all(
                           "HARDEN" in f["posture_relevance"]
                           for f in p.get("flags", [])
                       )))

        st, p = await _aget("/observability/flags?search=observer&limit=10")
        checks.append(("search=observer + limit clamp",
                       st == 200
                       and p.get("count", 0) <= 10
                       and all(
                           "observer" in f["name"].lower()
                           or "observer" in f["description"].lower()
                           for f in p.get("flags", [])
                       )))

        st, p = await _aget("/observability/flags?limit=banana")
        checks.append(("malformed limit → 400", st == 400))

        # --- (3) GET /observability/flags/{name} ---
        st, p = await _aget("/observability/flags/JARVIS_DIRECTION_INFERRER_ENABLED")
        checks.append(("flag detail → 200", st == 200))
        if st == 200:
            checks.append(("flag detail name matches",
                           p.get("name") == "JARVIS_DIRECTION_INFERRER_ENABLED"))
            checks.append(("flag detail type=bool",
                           p.get("type") == "bool"))
            checks.append(("flag detail category=safety",
                           p.get("category") == "safety"))

        st, p = await _aget("/observability/flags/JARVIS_POSTURE_OBSERVR_INTERVAL_S")
        checks.append(("unknown flag → 404", st == 404))
        if st == 404:
            checks.append(("404 payload has suggestions",
                           bool(p.get("suggestions"))))

        st, p = await _aget("/observability/flags/BAD_NAME")
        checks.append(("malformed flag name → 400", st == 400))

        # --- (4) /observability/flags/unregistered ---
        # Inject a typo env var before hitting the endpoint
        os.environ["JARVIS_POSTURE_OBSERVR_INTERVAL_S"] = "600"
        st, p = await _aget("/observability/flags/unregistered")
        checks.append(("unregistered endpoint → 200", st == 200))
        if st == 200:
            names = [u["name"] for u in p.get("unregistered", [])]
            checks.append(("unregistered surfaces typo",
                           "JARVIS_POSTURE_OBSERVR_INTERVAL_S" in names))
        del os.environ["JARVIS_POSTURE_OBSERVR_INTERVAL_S"]

        # --- (5) GET /observability/verbs ---
        st, p = await _aget("/observability/verbs")
        checks.append(("verbs endpoint → 200", st == 200))
        if st == 200:
            names = [v["name"] for v in p.get("verbs", [])]
            checks.append(("verbs includes /posture + /help",
                           "/posture" in names and "/help" in names))
            checks.append(("verbs count ≥ 7",
                           p.get("count", 0) >= 7))

        # --- (6) SSE flag_typo_detected via report_typos ---
        os.environ["JARVIS_POSTURE_OBSERVR_INTERVAL_S"] = "600"
        sse_task = asyncio.create_task(_subscribe_sse("127.0.0.1", port, timeout_s=3.0))
        await asyncio.sleep(0.4)
        emitted = registry.report_typos()
        frames = await sse_task
        typo_frames = []
        for f in frames:
            if "data" in f:
                try:
                    d = json.loads(f["data"])
                    if d.get("event_type") == "flag_typo_detected":
                        typo_frames.append(d)
                except (json.JSONDecodeError, TypeError):
                    pass
        print(f"[SSE] {len(typo_frames)} flag_typo_detected frame(s)")
        checks.append(("report_typos emits ≥1 SSE frame",
                       len(typo_frames) >= 1))
        if typo_frames:
            p0 = typo_frames[0].get("payload", {})
            checks.append(("typo frame includes env_name",
                           p0.get("env_name") == "JARVIS_POSTURE_OBSERVR_INTERVAL_S"))
            checks.append(("typo frame includes closest_match",
                           "closest_match" in p0))
        del os.environ["JARVIS_POSTURE_OBSERVR_INTERVAL_S"]

        # --- (7) SSE flag_registered via bridge ---
        bridge_flag_registry_to_broker(registry=registry)
        sse_task2 = asyncio.create_task(_subscribe_sse("127.0.0.1", port, timeout_s=3.0))
        await asyncio.sleep(0.4)
        new_spec = FlagSpec(
            name="JARVIS_LIVEFIRE_SLICE3_FLAG", type=FlagType.BOOL,
            default=False, description="livefire test flag (dynamic)",
            category=Category.EXPERIMENTAL,
            source_file="scripts/livefire_flag_registry_slice3.py",
            since="v1.0",
        )
        registry.register(new_spec)
        frames2 = await sse_task2
        reg_frames = []
        for f in frames2:
            if "data" in f:
                try:
                    d = json.loads(f["data"])
                    if d.get("event_type") == "flag_registered":
                        reg_frames.append(d)
                except (json.JSONDecodeError, TypeError):
                    pass
        print(f"[SSE] {len(reg_frames)} flag_registered frame(s)")
        checks.append(("bridge emits flag_registered SSE on new spec",
                       len(reg_frames) >= 1))
        if reg_frames:
            p0 = reg_frames[0].get("payload", {})
            checks.append(("registered frame name matches",
                           p0.get("name") == "JARVIS_LIVEFIRE_SLICE3_FLAG"))
            checks.append(("registered frame category=experimental",
                           p0.get("category") == "experimental"))

        # --- (8) Master-off revert ---
        os.environ["JARVIS_FLAG_REGISTRY_ENABLED"] = "false"
        st, _ = await _aget("/observability/flags")
        checks.append(("master=false: GET flags → 403", st == 403))
        st, _ = await _aget("/observability/flags/JARVIS_DIRECTION_INFERRER_ENABLED")
        checks.append(("master=false: GET flag detail → 403", st == 403))
        st, _ = await _aget("/observability/flags/unregistered")
        checks.append(("master=false: GET unregistered → 403", st == 403))
        st, _ = await _aget("/observability/verbs")
        checks.append(("master=false: GET verbs → 403", st == 403))

        # Re-enable bidirectional
        os.environ["JARVIS_FLAG_REGISTRY_ENABLED"] = "true"
        st, _ = await _aget("/observability/flags")
        checks.append(("re-enable: GET flags → 200", st == 200))

    finally:
        await server.stop()

    # --- (9) Authority invariant ---
    authority_forbidden = (
        "orchestrator", "policy", "iron_gate", "risk_tier",
        "change_engine", "candidate_generator",
    )
    for relpath in (
        "backend/core/ouroboros/governance/flag_registry.py",
        "backend/core/ouroboros/governance/flag_registry_seed.py",
        "backend/core/ouroboros/governance/help_dispatcher.py",
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

    pass_log = REPO_ROOT / "scripts" / "livefire_flag_registry_slice3_PASS.log"
    fail_log = REPO_ROOT / "scripts" / "livefire_flag_registry_slice3_FAIL.log"
    log_path = pass_log if all_pass else fail_log
    other = fail_log if all_pass else pass_log
    if other.exists():
        other.unlink()

    artifact = {
        "slice": 3,
        "feature": "GET /observability/flags|verbs + SSE",
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
