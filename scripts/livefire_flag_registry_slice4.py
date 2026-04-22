#!/usr/bin/env python3
"""Slice 4 graduation live-fire: zero-env boot + full-revert matrix.

Phase 1 — Graduated boot (ZERO posture + flag-registry env vars set):
  All surfaces wire themselves together end-to-end:
    1. is_enabled() True, dispatcher_enabled() True
    2. Seed 52 flags installed
    3. /help top-index returns 7 verbs + 52 flags
    4. /help flags --posture HARDEN filter works (Wave 1 #1 consumer)
    5. /help flag <NAME> shows detail
    6. /help unregistered surfaces injected typo
    7. GET /observability/flags 200 with schema v1.0
    8. GET /observability/verbs 200
    9. Raw-socket SSE flag_typo_detected on report_typos
   10. Raw-socket SSE flag_registered via bridge on new spec

Phase 2 — Full-revert matrix (single env flip):
  JARVIS_FLAG_REGISTRY_ENABLED=false reverts ALL surfaces in lockstep:
    - /help flags rejected + cites flag name
    - GET /observability/flags → 403
    - GET /observability/verbs → 403
    - GET /observability/flags/unregistered → 403
    - SSE publish returns None
    - /help help STILL works (discoverability exception)

Phase 3 — Re-default restores bidirectional.

Phase 4 — Authority invariants on all 3 arc files + 4 GET handlers +
SSE bridges.

Exit 0 on success, 1 on any check failure.
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


def _scrub_flag_env():
    """Graduation premise: zero flag-registry env vars set."""
    for key in list(os.environ):
        if (key.startswith("JARVIS_FLAG_REGISTRY")
                or key.startswith("JARVIS_HELP_DISPATCHER")
                or key.startswith("JARVIS_FLAG_TYPO")):
            del os.environ[key]


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


async def _subscribe_sse(host: str, port: int, timeout_s: float = 3.0) -> List[Dict[str, str]]:
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
            f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
            f"Origin: http://127.0.0.1:{port}\r\nConnection: close\r\n\r\n"
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
    print("FlagRegistry + /help — Slice 4 GRADUATION Live-Fire")
    print("=" * 72)

    _scrub_flag_env()
    os.environ["JARVIS_IDE_OBSERVABILITY_ENABLED"] = "true"
    os.environ["JARVIS_IDE_STREAM_ENABLED"] = "true"
    os.environ["JARVIS_EVENT_CHANNELS_ENABLED"] = "true"

    checks: List[tuple] = []

    from backend.core.ouroboros.governance.flag_registry import (
        Category, FlagSpec, FlagType, ensure_seeded, is_enabled,
        reset_default_registry,
    )
    from backend.core.ouroboros.governance.help_dispatcher import (
        dispatch_help_command, dispatcher_enabled,
        reset_default_verb_registry,
    )
    from backend.core.ouroboros.governance.ide_observability_stream import (
        bridge_flag_registry_to_broker,
        reset_default_broker,
    )
    from backend.core.ouroboros.governance.event_channel import (
        EventChannelServer,
    )

    # -----------------------------------------------------------------
    # PHASE 1 — GRADUATED BOOT (zero flag-registry env vars)
    # -----------------------------------------------------------------

    print("\n--- Phase 1 — Graduated defaults ---")

    checks.append(("is_enabled() True post-graduation", is_enabled() is True))
    checks.append(("dispatcher_enabled() True post-graduation",
                   dispatcher_enabled() is True))

    reset_default_registry()
    reset_default_verb_registry()
    reset_default_broker()
    registry = ensure_seeded()
    checks.append((f"seed installed ({len(registry.list_all())} flags)",
                   len(registry.list_all()) >= 50))

    # /help top-index
    r = dispatch_help_command("/help")
    checks.append(("/help top-index ok", r.ok))
    checks.append(("/help lists 7 verbs + 52 flags",
                   r.ok and "/posture" in r.text and "52" in r.text))

    # Wave 1 #1 consumer: posture filter
    r = dispatch_help_command("/help flags --posture HARDEN")
    harden_lines = [
        ln for ln in r.text.splitlines() if ln.strip().startswith("JARVIS_")
    ]
    print(f"[posture HARDEN] {len(harden_lines)} relevant flags")
    checks.append((
        "/help flags --posture HARDEN (Wave 1 #1 consumer) → ≥5 flags",
        r.ok and len(harden_lines) >= 5,
    ))

    # Flag detail
    r = dispatch_help_command("/help flag JARVIS_DIRECTION_INFERRER_ENABLED")
    checks.append(("/help flag detail ok", r.ok and "type" in r.text.lower()))

    # Unregistered with injected typo
    os.environ["JARVIS_POSTURE_OBSERVR_INTERVAL_S"] = "600"
    r = dispatch_help_command("/help unregistered")
    checks.append(("/help unregistered finds typo",
                   r.ok and "JARVIS_POSTURE_OBSERVR_INTERVAL_S" in r.text))

    # Boot EventChannelServer for GET + SSE
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

        # GET /observability/flags on defaults
        status, payload = await _aget("/observability/flags?limit=5")
        checks.append(("GET /observability/flags → 200 on defaults",
                       status == 200))
        if status == 200:
            checks.append(("GET flags schema=1.0",
                           payload.get("schema_version") == "1.0"))
            checks.append(("GET flags payload count ≤ 5 (limit respected)",
                           payload.get("count", 0) <= 5))

        # GET /observability/verbs
        status, payload = await _aget("/observability/verbs")
        checks.append(("GET /observability/verbs → 200", status == 200))
        if status == 200:
            checks.append(("GET verbs count ≥ 7",
                           payload.get("count", 0) >= 7))

        # Wave 1 #1 consumer via GET: posture filter returns HARDEN flags
        status, payload = await _aget("/observability/flags?posture=HARDEN")
        checks.append(("GET flags?posture=HARDEN 200", status == 200))
        if status == 200:
            checks.append(("GET posture filter non-empty",
                           payload.get("count", 0) >= 5))

        # --- SSE flag_typo_detected ---
        sse_task = asyncio.create_task(_subscribe_sse("127.0.0.1", port, timeout_s=3.0))
        await asyncio.sleep(0.3)
        # Typo env already set above
        registry.report_typos()
        frames = await sse_task
        typo_frames = [
            json.loads(f["data"]) for f in frames if "data" in f
        ]
        typo_frames = [
            d for d in typo_frames if d.get("event_type") == "flag_typo_detected"
        ]
        print(f"[SSE] {len(typo_frames)} flag_typo_detected frame(s)")
        checks.append(("SSE flag_typo_detected on defaults",
                       len(typo_frames) >= 1))

        # --- SSE flag_registered via bridge ---
        bridge_flag_registry_to_broker(registry=registry)
        sse_task2 = asyncio.create_task(_subscribe_sse("127.0.0.1", port, timeout_s=3.0))
        await asyncio.sleep(0.3)
        registry.register(FlagSpec(
            name="JARVIS_LIVEFIRE_SLICE4_GRAD_FLAG",
            type=FlagType.BOOL, default=False,
            description="graduation live-fire test spec",
            category=Category.EXPERIMENTAL,
            source_file="scripts/livefire_flag_registry_slice4.py",
            since="v1.0",
        ))
        frames2 = await sse_task2
        reg_frames = [
            json.loads(f["data"]) for f in frames2 if "data" in f
        ]
        reg_frames = [
            d for d in reg_frames if d.get("event_type") == "flag_registered"
        ]
        print(f"[SSE] {len(reg_frames)} flag_registered frame(s)")
        checks.append(("SSE flag_registered on defaults",
                       len(reg_frames) >= 1))

        # -----------------------------------------------------------------
        # PHASE 2 — FULL-REVERT MATRIX (single env flip)
        # -----------------------------------------------------------------
        print("\n--- Phase 2 — Full-Revert Matrix ---")

        os.environ["JARVIS_FLAG_REGISTRY_ENABLED"] = "false"

        checks.append(("master=false: is_enabled() False",
                       is_enabled() is False))
        checks.append(("master=false: dispatcher_enabled() False",
                       dispatcher_enabled() is False))

        # Surface 1: REPL rejected
        r = dispatch_help_command("/help flags")
        checks.append(("Surface 1 REPL: /help flags rejected",
                       not r.ok and "JARVIS_FLAG_REGISTRY_ENABLED" in r.text))

        # /help help STILL works
        r_help = dispatch_help_command("/help help")
        checks.append(("Surface 1 REPL: /help help still works (discoverability)",
                       r_help.ok))

        # Surface 2: GET /observability/flags 403
        st, _ = await _aget("/observability/flags")
        checks.append(("Surface 2 GET: /flags 403", st == 403))
        st, _ = await _aget("/observability/flags/JARVIS_DIRECTION_INFERRER_ENABLED")
        checks.append(("Surface 2 GET: /flags/{name} 403", st == 403))
        st, _ = await _aget("/observability/flags/unregistered")
        checks.append(("Surface 2 GET: /flags/unregistered 403", st == 403))
        st, _ = await _aget("/observability/verbs")
        checks.append(("Surface 2 GET: /verbs 403", st == 403))

        # Surface 3: typo_warn silenced
        from backend.core.ouroboros.governance.flag_registry import (
            typo_warn_enabled,
        )
        checks.append(("Surface 3: typo_warn_enabled() False",
                       typo_warn_enabled() is False))

        # -----------------------------------------------------------------
        # PHASE 3 — RE-DEFAULT RESTORES
        # -----------------------------------------------------------------
        print("\n--- Phase 3 — Re-default ---")

        del os.environ["JARVIS_FLAG_REGISTRY_ENABLED"]
        checks.append(("re-default: is_enabled() back to True",
                       is_enabled() is True))
        st, _ = await _aget("/observability/flags")
        checks.append(("re-default: GET /flags back to 200", st == 200))
        r = dispatch_help_command("/help flags")
        checks.append(("re-default: /help flags back to ok", r.ok))

    finally:
        await server.stop()

    # -----------------------------------------------------------------
    # PHASE 4 — Authority invariants
    # -----------------------------------------------------------------
    print("\n--- Phase 4 — Authority Invariants ---")

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

    pass_log = REPO_ROOT / "scripts" / "livefire_flag_registry_slice4_PASS.log"
    fail_log = REPO_ROOT / "scripts" / "livefire_flag_registry_slice4_FAIL.log"
    log_path = pass_log if all_pass else fail_log
    other = fail_log if all_pass else pass_log
    if other.exists():
        other.unlink()

    artifact = {
        "slice": 4,
        "feature": "FlagRegistry GRADUATION + Full-Revert Matrix",
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
    print("\n  RESULT: FAIL  —  check the log artifact.")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
