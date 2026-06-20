"""JARVIS C2 Telemetry Subscriber — lightweight remote observability (2026-06-20).

The DEV-side (M1) half of the bi-directional C2 bridge. Subscribes to the remote
Linux engine's NATIVE SSE event stream (``GET /observability/stream`` — the same
StreamEventBroker the IDE extensions consume) and renders a live local dashboard
of the telemetry that matters: FleetEvaluator EWMA, OperationAdvisor blocks, and
state=applied victories.

This is NOT log-tailing — it's the real-time TrinityEventBus/SSE event stream,
just consumed remotely. Deliberately a STANDALONE thin client: it imports ZERO
backend/organism modules (only stdlib + aiohttp), so it cannot drag the heavy
engine onto the Mac or starve its event loop. Bounded memory, exp-backoff
reconnect, heartbeat-aware.

Secure transport (respects the engine's loopback-only invariant — no new attack
surface): SSH local-forward to the host's loopback stream, then point here at it:
    ssh -N -L 8099:localhost:8099 user@linux-host        # in one terminal
    python3 scripts/jarvis_c2_subscriber.py              # in another (M1)

Run: python3 scripts/jarvis_c2_subscriber.py [--url http://localhost:8099] [--token T]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from typing import Dict, List, Optional, Tuple

# ---- pure event projection (unit-tested) ----------------------------------- #

# The C2-relevant subset of the engine's ~60 SSE event types.
_C2_EVENTS = {
    "fleet_calibrated", "fleet_graduated", "operation_terminal",
    "governor_throttle_applied", "governor_emergency_brake",
    "memory_pressure_changed", "circuit_breaker_tripped",
}


def parse_sse_block(block: str) -> Tuple[Optional[str], Optional[dict]]:
    """Parse one SSE record (``event:``/``data:`` lines) → (event_type, payload).
    Returns (None, None) for heartbeats/comments/malformed. NEVER raises."""
    event_type: Optional[str] = None
    data_lines: List[str] = []
    for line in block.splitlines():
        if line.startswith(":"):           # SSE comment / heartbeat
            continue
        if line.startswith("event:"):
            event_type = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].strip())
    if not data_lines:
        return event_type, None
    try:
        return event_type, json.loads("\n".join(data_lines))
    except Exception:  # noqa: BLE001
        return event_type, None


class C2Dashboard:
    """Bounded, pure telemetry aggregator. ingest() returns a one-line summary
    (or None to suppress); render_status() gives the rolling headline."""

    def __init__(self, recent_cap: int = 50) -> None:
        self.applied = 0
        self.blocked = 0
        self.failed = 0
        self.advisor_blocks = 0
        self.graduations = 0
        self.ewma: Dict[str, float] = {}      # model -> last valid_tok_per_s
        self.ast_pass: Dict[str, float] = {}  # model -> last ast_pass_rate
        self.recent: List[str] = []
        self._cap = recent_cap

    def ingest(self, event_type: Optional[str], data: Optional[dict]) -> Optional[str]:
        if event_type not in _C2_EVENTS or not isinstance(data, dict):
            return None
        line: Optional[str] = None
        if event_type == "fleet_calibrated":
            m = str(data.get("model_id", "?")).split("/")[-1]
            if "valid_tok_per_s" in data:
                self.ewma[m] = float(data.get("valid_tok_per_s", 0.0) or 0.0)
            if "ast_pass_rate" in data:
                self.ast_pass[m] = float(data.get("ast_pass_rate", 0.0) or 0.0)
            src = data.get("source", "calib")
            applied = data.get("applied")
            line = (f"📊 calib[{src}] {m} ast={self.ast_pass.get(m, '?')} "
                    f"vtps={self.ewma.get(m, '?')}"
                    + (f" applied={applied}" if applied is not None else ""))
        elif event_type == "fleet_graduated":
            self.graduations += 1
            line = f"🎓 GRADUATED coder={data.get('winner', data.get('model_id', '?'))}"
        elif event_type == "operation_terminal":
            state = str(data.get("state", "")).lower()
            reason = str(data.get("terminal_reason_code", data.get("reason", "")))
            if state == "applied":
                self.applied += 1
                line = f"✅ state=applied op={str(data.get('op_id', '?'))[:18]}"
            elif "advisor_blocked" in reason:
                self.advisor_blocks += 1
                line = f"🛡  advisor BLOCKED op={str(data.get('op_id', '?'))[:18]}"
            elif state in ("blocked", "failed"):
                self.failed += 1
                line = f"❌ {state} op={str(data.get('op_id', '?'))[:18]} ({reason[:30]})"
        elif event_type == "circuit_breaker_tripped":
            line = f"⚡ breaker tripped: {data.get('terminal_reason_code', '?')}"
        elif event_type.startswith("governor") or event_type == "memory_pressure_changed":
            line = f"🔧 {event_type}: {json.dumps(data)[:60]}"
        if line:
            self.recent.append(line)
            if len(self.recent) > self._cap:
                self.recent.pop(0)
        return line

    def render_status(self) -> str:
        ew = " ".join(f"{m}={v:.0f}" for m, v in sorted(self.ewma.items())) or "—"
        return (f"applied={self.applied} advisor_blocked={self.advisor_blocks} "
                f"failed={self.failed} grad={self.graduations} | EWMA[{ew}]")


# ---- async network shell (thin; not unit-tested) --------------------------- #

async def subscribe(url: str, token: Optional[str] = None) -> None:
    import aiohttp  # late import — keeps the module importable for unit tests
    dash = C2Dashboard()
    headers = {"Accept": "text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    backoff = 1.0
    stream_url = url.rstrip("/") + "/observability/stream"
    print(f"[C2] subscribing → {stream_url}")
    while True:
        try:
            timeout = aiohttp.ClientTimeout(total=None, sock_read=60)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.get(stream_url, headers=headers) as resp:
                    if resp.status != 200:
                        print(f"[C2] stream HTTP {resp.status} — retrying");
                        await asyncio.sleep(backoff); backoff = min(backoff * 2, 30); continue
                    backoff = 1.0
                    print("[C2] connected — live engine telemetry:")
                    buf = ""
                    async for chunk in resp.content.iter_any():
                        buf += chunk.decode("utf-8", errors="ignore")
                        while "\n\n" in buf:
                            block, buf = buf.split("\n\n", 1)
                            et, data = parse_sse_block(block)
                            line = dash.ingest(et, data)
                            if line:
                                ts = time.strftime("%H:%M:%S")
                                print(f"  {ts} {line}")
                                print(f"         └ {dash.render_status()}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            print(f"[C2] disconnected ({type(exc).__name__}) — reconnecting in {backoff:.0f}s")
            await asyncio.sleep(backoff); backoff = min(backoff * 2, 30)


def main() -> int:
    ap = argparse.ArgumentParser(description="JARVIS C2 telemetry subscriber")
    ap.add_argument("--url", default="http://localhost:8099",
                    help="engine base URL (default loopback / SSH-forwarded)")
    ap.add_argument("--token", default=None, help="bearer token (if engine C2 auth on)")
    args = ap.parse_args()
    try:
        asyncio.run(subscribe(args.url, args.token))
    except KeyboardInterrupt:
        print("\n[C2] subscriber stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
