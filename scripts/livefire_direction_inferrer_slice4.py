#!/usr/bin/env python3
"""Slice 4 graduation live-fire: zero-env boot + full-revert matrix.

Graduation centerpiece. With ZERO posture-related env vars set (pure
defaults), the DirectionInferrer arc must wire itself together
end-to-end:

  Phase 1 — Graduated boot (no env vars):
    1. ``is_enabled()`` returns True on defaults
    2. SignalCollector.build_bundle() works on real repo
    3. Observer cycles, writes store
    4. ``compose_posture_section`` returns a non-empty block
    5. StrategicDirection.format_for_prompt() includes the block
    6. REPL /posture status works
    7. GET /observability/posture returns 200 with 12-evidence payload
    8. SSE posture_changed frame received via raw socket on observer
       flip
    9. REPL /posture override → audit record + SSE override_set frame

  Phase 2 — Full-revert matrix (single env flip):
    Setting ``JARVIS_DIRECTION_INFERRER_ENABLED=false`` at runtime must
    revert ALL FOUR surfaces in lockstep:
      - prompt injection → empty string
      - REPL /posture status → rejected with discoverable error
      - GET /observability/posture → 403
      - /posture help still works (discoverability exception)

  Phase 3 — Authority invariants on every arc file.

Exit 0 on success, 1 on any failure.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import socket
import sys
import tempfile
import time
from dataclasses import replace
from typing import Any, Dict, List

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _scrub_posture_env():
    """Graduation premise: zero posture env vars set → defaults apply."""
    for key in list(os.environ):
        if key.startswith("JARVIS_DIRECTION_INFERRER") or key.startswith("JARVIS_POSTURE"):
            del os.environ[key]


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


def _raw_http_get(port: int, path: str, timeout_s: float = 5.0) -> tuple:
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=timeout_s)
    except Exception as exc:
        return 0, {"_error": f"connect failed: {exc}"}
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
    print("DirectionInferrer Slice 4 — GRADUATION Live-Fire")
    print("=" * 72)

    _scrub_posture_env()
    os.environ["JARVIS_IDE_OBSERVABILITY_ENABLED"] = "true"
    os.environ["JARVIS_IDE_STREAM_ENABLED"] = "true"
    os.environ["JARVIS_EVENT_CHANNELS_ENABLED"] = "true"

    checks: List[tuple] = []

    # Import AFTER env scrub — modules capture env at top-level in some
    # paths. direction_inferrer reads env each call so order doesn't
    # actually matter for it, but cost_state/worktree env may.
    from backend.core.ouroboros.governance.direction_inferrer import (
        DirectionInferrer, is_enabled,
    )
    from backend.core.ouroboros.governance.posture import (
        Posture, baseline_bundle,
    )
    from backend.core.ouroboros.governance.posture_observer import (
        OverrideState, PostureObserver, SignalCollector,
        get_default_store, reset_default_observer, reset_default_store,
    )
    from backend.core.ouroboros.governance.posture_prompt import (
        compose_posture_section, prompt_injection_enabled,
    )
    from backend.core.ouroboros.governance.posture_repl import (
        dispatch_posture_command, reset_default_providers,
        set_default_override_state, set_default_store,
    )
    from backend.core.ouroboros.governance.posture_store import PostureStore
    from backend.core.ouroboros.governance.ide_observability_stream import (
        bridge_posture_to_broker, get_default_broker, reset_default_broker,
    )
    from backend.core.ouroboros.governance.strategic_direction import (
        StrategicDirectionService,
    )
    from backend.core.ouroboros.governance.event_channel import (
        EventChannelServer,
    )

    # -----------------------------------------------------------------
    # PHASE 1 — GRADUATED BOOT (zero posture env vars set)
    # -----------------------------------------------------------------

    print("\n--- Phase 1 — Graduated defaults ---")

    checks.append(("is_enabled() defaults to True post-graduation", is_enabled() is True))
    checks.append(("prompt_injection_enabled() True on defaults", prompt_injection_enabled() is True))

    with tempfile.TemporaryDirectory(prefix="livefire_posture_slice4_") as tmp:
        tmp_path = pathlib.Path(tmp)
        reset_default_store()
        reset_default_observer()
        reset_default_providers()
        reset_default_broker()

        store = get_default_store(tmp_path / ".jarvis")
        override = OverrideState()
        set_default_store(store)
        set_default_override_state(override)

        # Real signal collection
        collector = SignalCollector(REPO_ROOT)
        bundle = collector.build_bundle()
        print(
            f"[collect] feat={bundle.feat_ratio:.2f} fix={bundle.fix_ratio:.2f} "
            f"refactor={bundle.refactor_ratio:.2f}"
        )
        checks.append(("SignalCollector.build_bundle on real repo", bundle.schema_version == "1.0"))

        # Observer cycle → writes current
        observer = PostureObserver(REPO_ROOT, store, collector=collector)
        bridge_posture_to_broker(observer=observer)
        reading = await observer.run_one_cycle()
        if reading is not None:
            print(f"[cycle] posture={reading.posture.value} confidence={reading.confidence:.3f}")
        else:
            print("[cycle] no reading")
        checks.append(("Observer cycle produces reading", reading is not None))
        checks.append(("PostureStore current populated", store.load_current() is not None))

        # Prompt section renders (defaults — both flags on)
        block = compose_posture_section(reading)
        checks.append(("compose_posture_section non-empty on defaults", len(block) > 0))
        checks.append(("Prompt block under 600 char budget", len(block) < 600))

        # StrategicDirection integration
        svc = StrategicDirectionService(REPO_ROOT)
        svc._digest = "(livefire graduation digest)"
        svc._loaded = True
        prompt_out = svc.format_for_prompt()
        checks.append(
            ("StrategicDirection.format_for_prompt includes posture section on defaults",
             "Current Strategic Posture" in prompt_out),
        )

        # REPL /posture status
        r_status = dispatch_posture_command("/posture status")
        checks.append(("REPL /posture status works on defaults", r_status.ok))
        checks.append(("REPL status includes posture value", reading is not None and reading.posture.value in r_status.text))

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
                return await asyncio.to_thread(_raw_http_get, port, path)

            # GET /observability/posture
            status, payload = await _aget("/observability/posture")
            print(f"[GET] /observability/posture → {status}")
            checks.append(("GET /observability/posture returns 200 on defaults", status == 200))
            if payload:
                checks.append(("GET posture payload schema_version=1.0", payload.get("schema_version") == "1.0"))
                checks.append(("GET posture has 12 evidence entries", len(payload.get("evidence", [])) == 12))
                checks.append(("GET posture has 4 all_scores entries", len(payload.get("all_scores", [])) == 4))

            # SSE posture_changed via observer flip
            os.environ["JARVIS_POSTURE_HIGH_CONFIDENCE_BYPASS"] = "0.0"

            class _Flip:
                def __init__(self, b):
                    self.b = b
                def build_bundle(self):
                    return self.b

            harden_bundle = replace(
                baseline_bundle(), fix_ratio=0.75,
                postmortem_failure_rate=0.55, iron_gate_reject_rate=0.45,
                session_lessons_infra_ratio=0.80,
            )

            sse_task = asyncio.create_task(_subscribe_sse("127.0.0.1", port, timeout_s=4.0))
            await asyncio.sleep(0.4)
            observer._collector = _Flip(harden_bundle)
            await observer.run_one_cycle()
            frames = await sse_task
            data_frames: List[dict] = []
            for f in frames:
                if "data" in f:
                    try:
                        data_frames.append(json.loads(f["data"]))
                    except (json.JSONDecodeError, TypeError):
                        pass
            posture_frames = [
                d for d in data_frames if d.get("event_type") == "posture_changed"
            ]
            print(f"[SSE] posture_changed frames: {len(posture_frames)}")
            checks.append(("SSE posture_changed frame received on defaults", len(posture_frames) >= 1))

            # REPL override → audit + SSE override_set
            sse_task2 = asyncio.create_task(_subscribe_sse("127.0.0.1", port, timeout_s=3.0))
            await asyncio.sleep(0.3)
            r_ov = dispatch_posture_command(
                "/posture override EXPLORE --until 10m --reason graduation_livefire",
            )
            frames2 = await sse_task2
            data2: List[dict] = []
            for f in frames2:
                if "data" in f:
                    try:
                        data2.append(json.loads(f["data"]))
                    except (json.JSONDecodeError, TypeError):
                        pass
            override_frames = [
                d for d in data2
                if d.get("event_type") == "posture_changed"
                and d.get("payload", {}).get("trigger") == "override_set"
            ]
            checks.append(("REPL override returns ok", r_ov.ok))
            checks.append(("REPL override writes audit record", any(
                rec.event == "set" for rec in store.load_audit()
            )))
            checks.append(("REPL override publishes SSE override_set", len(override_frames) >= 1))

            # -------------------------------------------------------------
            # PHASE 2 — FULL-REVERT MATRIX (single env flip)
            # -------------------------------------------------------------
            print("\n--- Phase 2 — Full-Revert Matrix ---")

            os.environ["JARVIS_DIRECTION_INFERRER_ENABLED"] = "false"

            checks.append(("master=false: is_enabled() False",
                           is_enabled() is False))
            checks.append(("master=false: prompt_injection_enabled() False",
                           prompt_injection_enabled() is False))

            # Surface 1: prompt injection empty
            empty_prompt = svc.format_for_prompt()
            checks.append(("Surface 1 prompt: posture section removed",
                           "Current Strategic Posture" not in empty_prompt))

            # Surface 2: REPL status rejected
            r_after = dispatch_posture_command("/posture status")
            checks.append(("Surface 2 REPL: status rejected", r_after.ok is False))
            checks.append(("Surface 2 REPL: error cites master flag",
                           "JARVIS_DIRECTION_INFERRER_ENABLED" in r_after.text
                           or "DirectionInferrer disabled" in r_after.text))

            # Surface 3: GET returns 403
            status_after, _ = await _aget("/observability/posture")
            checks.append(("Surface 3 GET: 403 on master=false",
                           status_after == 403))

            # Surface 4: REPL /posture help STILL works (discoverability)
            r_help = dispatch_posture_command("/posture help")
            checks.append(("help still works master=false (discoverability)",
                           r_help.ok and "JARVIS_DIRECTION_INFERRER_ENABLED" in r_help.text))

            # Re-enable to verify flip is bidirectional
            del os.environ["JARVIS_DIRECTION_INFERRER_ENABLED"]
            checks.append(("master re-default: is_enabled() back to True",
                           is_enabled() is True))
            status_back, _ = await _aget("/observability/posture")
            checks.append(("master re-default: GET back to 200",
                           status_back == 200))

        finally:
            await server.stop()

    # -----------------------------------------------------------------
    # PHASE 3 — AUTHORITY INVARIANTS
    # -----------------------------------------------------------------
    print("\n--- Phase 3 — Authority Invariants ---")

    authority_forbidden = (
        "orchestrator", "policy", "iron_gate", "risk_tier",
        "change_engine", "candidate_generator",
    )
    arc_files = (
        "backend/core/ouroboros/governance/direction_inferrer.py",
        "backend/core/ouroboros/governance/posture.py",
        "backend/core/ouroboros/governance/posture_store.py",
        "backend/core/ouroboros/governance/posture_prompt.py",
        "backend/core/ouroboros/governance/posture_observer.py",
        "backend/core/ouroboros/governance/posture_repl.py",
    )
    for relpath in arc_files:
        src = (REPO_ROOT / relpath).read_text(encoding="utf-8")
        bad = []
        for line in src.splitlines():
            if line.startswith(("from ", "import ")):
                for forbidden in authority_forbidden:
                    if f".{forbidden}" in line:
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

    pass_log = REPO_ROOT / "scripts" / "livefire_direction_inferrer_slice4_PASS.log"
    fail_log = REPO_ROOT / "scripts" / "livefire_direction_inferrer_slice4_FAIL.log"
    log_path = pass_log if all_pass else fail_log
    other = fail_log if all_pass else pass_log
    if other.exists():
        other.unlink()

    artifact = {
        "slice": 4,
        "feature": "DirectionInferrer GRADUATION + Full-Revert Matrix",
        "timestamp": time.time(),
        "all_pass": all_pass,
        "total_checks": len(checks),
        "passed": sum(1 for _, ok in checks if ok),
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
