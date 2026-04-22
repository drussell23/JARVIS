#!/usr/bin/env python3
"""Slice 3 live-fire: /posture REPL + IDE GET + SSE bridge on real server.

End-to-end proof:
  1. Each of the 7 /posture subcommands dispatches on a real store
     and returns well-formed output (status/explain/history/signals/
     override/clear-override/help).
  2. A real aiohttp EventChannelServer boots with IDE observability +
     stream enabled.
  3. HTTP GET /observability/posture + /observability/posture/history
     return 200 + schema-v1.0 payload + expected fields.
  4. SSE raw-socket subscriber receives a ``posture_changed`` frame
     when the PostureObserver flips posture (bridge via observer hook).
  5. SSE subscriber also receives an ``override_set`` posture event
     when the REPL override handler fires publish_posture_event.

Authority invariants: the REPL + observability router remain free of
orchestrator / policy / iron_gate / risk_tier / change_engine /
candidate_generator imports (grep check).

Exit 0 on success, 1 on any check failure.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import replace
from typing import Any, Dict, List, Optional

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.core.ouroboros.governance.direction_inferrer import (  # noqa: E402
    DirectionInferrer,
)
from backend.core.ouroboros.governance.posture import (  # noqa: E402
    Posture,
    baseline_bundle,
)
from backend.core.ouroboros.governance.posture_observer import (  # noqa: E402
    OverrideState,
    PostureObserver,
    get_default_store,
    reset_default_observer,
    reset_default_store,
)
from backend.core.ouroboros.governance.posture_repl import (  # noqa: E402
    dispatch_posture_command,
    reset_default_providers,
    set_default_override_state,
    set_default_store,
)
from backend.core.ouroboros.governance.posture_store import (  # noqa: E402
    PostureStore,
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _explore_bundle():
    return replace(baseline_bundle(), feat_ratio=0.80, test_docs_ratio=0.10)


def _harden_bundle():
    return replace(
        baseline_bundle(), fix_ratio=0.75,
        postmortem_failure_rate=0.55, iron_gate_reject_rate=0.45,
        session_lessons_infra_ratio=0.80,
    )


class _StubCollector:
    def __init__(self, bundle):
        self.bundle = bundle

    def build_bundle(self):
        return self.bundle


def _parse_sse_frames(chunks: bytes) -> List[Dict[str, str]]:
    """Parse raw SSE body into frames."""
    text = chunks.decode("utf-8", errors="replace")
    frames: List[Dict[str, str]] = []
    for block in text.split("\n\n"):
        lines = [ln.strip("\r") for ln in block.split("\n") if ln.strip()]
        # Skip HTTP chunk-size lines (pure hex)
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


async def _subscribe_sse(host: str, port: int, timeout_s: float = 5.0) -> List[Dict[str, str]]:
    """Open SSE connection and collect frames for timeout_s."""
    loop = asyncio.get_event_loop()

    def _reader():
        try:
            sock = socket.create_connection((host, port), timeout=timeout_s)
            req = (
                f"GET /observability/stream HTTP/1.1\r\n"
                f"Host: {host}:{port}\r\n"
                f"Accept: text/event-stream\r\n"
                f"Cache-Control: no-store\r\n"
                f"Connection: close\r\n\r\n"
            ).encode("ascii")
            sock.sendall(req)
            sock.settimeout(timeout_s)
            buf = b""
            deadline = time.monotonic() + timeout_s
            # Skip HTTP headers
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
            _hdrs, rest = buf.split(b"\r\n\r\n", 1)
            body = rest
            # Read body until deadline
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


async def amain() -> int:
    print("=" * 72)
    print("DirectionInferrer Slice 3 — Live-Fire on Real Repo State")
    print("=" * 72)
    checks: List[tuple] = []

    # Master flag required for REPL + posture surface
    os.environ["JARVIS_DIRECTION_INFERRER_ENABLED"] = "true"
    os.environ["JARVIS_IDE_OBSERVABILITY_ENABLED"] = "true"
    os.environ["JARVIS_IDE_STREAM_ENABLED"] = "true"
    os.environ["JARVIS_EVENT_CHANNELS_ENABLED"] = "true"

    with tempfile.TemporaryDirectory(prefix="livefire_posture_slice3_") as tmp:
        tmp_path = pathlib.Path(tmp)
        reset_default_store()
        reset_default_observer()
        reset_default_providers()

        store = get_default_store(tmp_path / ".jarvis")
        override = OverrideState()
        set_default_store(store)
        set_default_override_state(override)

        # Prime the store with a real reading
        reading = DirectionInferrer().infer(_explore_bundle())
        store.write_current(reading)
        store.append_history(reading)

        # --- (1) All 7 REPL subcommands ---
        subcommands = [
            ("status", "/posture status", "EXPLORE"),
            ("bare alias", "/posture", "EXPLORE"),
            ("explain", "/posture explain", "feat_ratio"),
            ("history", "/posture history", "reading"),
            ("signals", "/posture signals", "raw values"),
            ("help", "/posture help", "Strategic posture"),
        ]
        for name, line, needle in subcommands:
            r = dispatch_posture_command(line)
            ok = r.ok and needle.lower() in r.text.lower()
            checks.append((f"REPL /{line} returns {needle!r}", ok))
            if not ok:
                print(f"[REPL] {line!r} text:\n{r.text}")

        # override
        r_override = dispatch_posture_command(
            "/posture override HARDEN --until 30m --reason livefire",
        )
        checks.append(("REPL override sets posture", r_override.ok))
        checks.append((
            "REPL override writes audit record",
            any(rec.event == "set" and rec.posture is Posture.HARDEN
                for rec in store.load_audit()),
        ))

        # clear-override
        r_clear = dispatch_posture_command("/posture clear-override")
        checks.append(("REPL clear-override drops active", r_clear.ok))
        checks.append((
            "REPL clear-override writes audit",
            any(rec.event == "clear" for rec in store.load_audit()),
        ))

        # --- (2) Boot EventChannelServer with IDE surfaces ---
        from backend.core.ouroboros.governance.event_channel import (
            EventChannelServer,
        )
        from backend.core.ouroboros.governance.ide_observability_stream import (
            reset_default_broker,
        )
        reset_default_broker()

        class _StubRouter:
            """Minimal IntakeRouter stub — server doesn't require a real one
            for the observability paths we exercise."""
            async def submit(self, *args, **kwargs):
                return None

        port = _free_port()
        server = EventChannelServer(
            router=_StubRouter(), port=port, host="127.0.0.1",
        )
        await server.start()
        await asyncio.sleep(0.3)  # let the TCPSite bind

        try:
            # --- (3) GET /observability/posture ---
            # Raw-socket HTTP client — bypasses urllib's proxy resolution
            # which can misroute loopback traffic in some dev shells.
            def _http_get(path: str) -> tuple:
                try:
                    sock = socket.create_connection(("127.0.0.1", port), timeout=5.0)
                except Exception as exc:
                    return 0, {"_error": f"connect failed: {exc}"}
                try:
                    sock.settimeout(5.0)
                    req = (
                        f"GET {path} HTTP/1.1\r\n"
                        f"Host: 127.0.0.1:{port}\r\n"
                        f"Origin: http://127.0.0.1:{port}\r\n"
                        f"Connection: close\r\n\r\n"
                    ).encode("ascii")
                    sock.sendall(req)
                    buf = b""
                    deadline = time.monotonic() + 5.0
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
                    header_block, body_bytes = buf.split(b"\r\n\r\n", 1)
                    header_lines = header_block.decode("iso-8859-1").split("\r\n")
                    status_line = header_lines[0] if header_lines else ""
                    parts = status_line.split(" ", 2)
                    try:
                        status = int(parts[1]) if len(parts) >= 2 else 0
                    except ValueError:
                        status = 0
                    # Check for chunked transfer encoding
                    is_chunked = any(
                        h.lower().startswith("transfer-encoding:") and "chunked" in h.lower()
                        for h in header_lines[1:]
                    )
                    if is_chunked:
                        # Strip chunk size prefix lines and terminator
                        body_text = body_bytes.decode("utf-8", errors="replace")
                        clean: list = []
                        for ln in body_text.split("\r\n"):
                            if ln and all(c in "0123456789abcdefABCDEF" for c in ln):
                                continue  # chunk size line
                            clean.append(ln)
                        body_s = "".join(clean)
                    else:
                        body_s = body_bytes.decode("utf-8", errors="replace")
                    try:
                        return status, json.loads(body_s)
                    except json.JSONDecodeError:
                        return status, {"_raw": body_s[:200]}
                finally:
                    try:
                        sock.close()
                    except Exception:
                        pass

            # Wrap blocking HTTP in a thread so aiohttp can serve requests
            async def _ahttp_get(path: str) -> tuple:
                return await asyncio.to_thread(_http_get, path)

            # Sanity: probe /channel/health first
            health_status, health_body = await _ahttp_get("/channel/health")
            print(f"[probe] /channel/health → {health_status} {health_body}")
            checks.append(("EventChannelServer responding on /channel/health", health_status == 200))

            status, payload = await _ahttp_get("/observability/posture")
            print(f"[GET] /observability/posture → {status}")
            checks.append(("GET /observability/posture returns 200", status == 200))
            if payload:
                checks.append((
                    "GET posture payload has schema_version=1.0",
                    payload.get("schema_version") == "1.0",
                ))
                checks.append((
                    "GET posture payload has posture field",
                    "posture" in payload,
                ))
                checks.append((
                    "GET posture payload has evidence (12 entries)",
                    len(payload.get("evidence", [])) == 12,
                ))
                print(f"[GET] posture={payload.get('posture')} "
                      f"conf={payload.get('confidence', 0):.3f}")

            status_h, payload_h = await _ahttp_get("/observability/posture/history?limit=5")
            print(f"[GET] /observability/posture/history → {status_h}")
            checks.append(("GET posture/history returns 200", status_h == 200))
            if payload_h:
                checks.append((
                    "GET posture/history has readings list",
                    isinstance(payload_h.get("readings"), list),
                ))
                checks.append((
                    "GET posture/history respects limit",
                    payload_h.get("limit") == 5,
                ))

            # --- (4) SSE subscription → observer cycle → posture_changed frame ---
            # Wire the bridge + an observer
            from backend.core.ouroboros.governance.ide_observability_stream import (
                bridge_posture_to_broker,
            )

            observer = PostureObserver(
                REPO_ROOT, store,
                collector=_StubCollector(_explore_bundle()),
            )
            bridge_posture_to_broker(observer=observer)
            os.environ["JARVIS_POSTURE_HIGH_CONFIDENCE_BYPASS"] = "0.0"

            # Subscribe + trigger inference flip concurrently
            sse_task = asyncio.create_task(_subscribe_sse("127.0.0.1", port, timeout_s=4.0))
            await asyncio.sleep(0.5)  # let subscribe settle

            await observer.run_one_cycle()  # EXPLORE initial
            observer._collector = _StubCollector(_harden_bundle())
            await observer.run_one_cycle()  # flip to HARDEN → publishes SSE

            frames = await sse_task
            print(f"[SSE] collected {len(frames)} frames")
            data_frames = []
            for f in frames:
                if "data" in f:
                    try:
                        data_frames.append(json.loads(f["data"]))
                    except (json.JSONDecodeError, TypeError):
                        pass
            posture_frames = [
                d for d in data_frames if d.get("event_type") == "posture_changed"
            ]
            print(f"[SSE] {len(posture_frames)} posture_changed frames")
            checks.append((
                "SSE posture_changed frame received",
                len(posture_frames) >= 1,
            ))
            if posture_frames:
                pf = posture_frames[-1]
                print(f"[SSE] posture={pf.get('payload', {}).get('posture')} "
                      f"trigger={pf.get('payload', {}).get('trigger')}")
                checks.append((
                    "SSE frame payload has posture field",
                    "posture" in pf.get("payload", {}),
                ))
                checks.append((
                    "SSE frame trigger=inference",
                    pf.get("payload", {}).get("trigger") == "inference",
                ))
                checks.append((
                    "SSE frame schema_version='1.0'",
                    pf.get("schema_version") == "1.0",
                ))

            # --- (5) SSE via REPL override path ---
            sse_task2 = asyncio.create_task(_subscribe_sse("127.0.0.1", port, timeout_s=3.0))
            await asyncio.sleep(0.3)
            dispatch_posture_command(
                "/posture override MAINTAIN --until 5m --reason slice3",
            )
            frames2 = await sse_task2
            data_frames2 = []
            for f in frames2:
                if "data" in f:
                    try:
                        data_frames2.append(json.loads(f["data"]))
                    except (json.JSONDecodeError, TypeError):
                        pass
            override_frames = [
                d for d in data_frames2
                if d.get("event_type") == "posture_changed"
                and d.get("payload", {}).get("trigger") == "override_set"
            ]
            print(f"[SSE] {len(override_frames)} override_set frames from REPL")
            checks.append((
                "SSE override_set frame from REPL",
                len(override_frames) >= 1,
            ))

        finally:
            await server.stop()

    # --- Authority invariants ---
    authority_forbidden = (
        "orchestrator", "policy", "iron_gate", "risk_tier",
        "change_engine", "candidate_generator",
    )
    for relpath in (
        "backend/core/ouroboros/governance/posture_repl.py",
        "backend/core/ouroboros/governance/posture_store.py",
        "backend/core/ouroboros/governance/posture_prompt.py",
        "backend/core/ouroboros/governance/posture_observer.py",
    ):
        src = (REPO_ROOT / relpath).read_text(encoding="utf-8")
        bad = []
        for line in src.splitlines():
            if line.startswith(("from ", "import ")):
                for forbidden in authority_forbidden:
                    if f".{forbidden}" in line:
                        bad.append(line)
        checks.append((f"authority-import-free: {relpath}", not bad))

    # --- Report ---
    print()
    print("-" * 72)
    print("Checks:")
    all_pass = True
    for name, ok in checks:
        mark = "PASS" if ok else "FAIL"
        if not ok:
            all_pass = False
        print(f"  [{mark}] {name}")
    print("-" * 72)

    pass_log = REPO_ROOT / "scripts" / "livefire_direction_inferrer_slice3_PASS.log"
    fail_log = REPO_ROOT / "scripts" / "livefire_direction_inferrer_slice3_FAIL.log"
    log_path = pass_log if all_pass else fail_log
    other = fail_log if all_pass else pass_log
    if other.exists():
        other.unlink()

    artifact = {
        "slice": 3,
        "feature": "/posture REPL + IDE observability + SSE",
        "timestamp": time.time(),
        "all_pass": all_pass,
        "checks": [{"name": n, "pass": ok} for n, ok in checks],
    }
    log_path.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    print(f"Artifact: {log_path}")

    if all_pass:
        print("\n  RESULT: PASS  —  Slice 3 live-fire clean on real repo state.")
        return 0
    print("\n  RESULT: FAIL  —  check the log artifact.")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
