#!/usr/bin/env python3
"""Slice 157 — Sovereign Live-Fire Injection Protocol.

Inject a GENUINE operation into a RUNNING GovernedLoopService so it flows through the
real Ouroboros FSM, is elevated to APPROVAL_REQUIRED by the real risk-tier floor, and
the live Discord gateway DMs the operator a real [APPROVE]/[REJECT]/[STEER] view.

No demo flags, no FSM bypasses, no no-ops. The injection rides the real cross-process
ingress: HTTP POST → EventChannelServer /webhook/generic → UnifiedIntakeRouter.ingest
→ orchestrator → GATE (APPROVAL_REQUIRED) → approval_provider.request().

DETERMINISM is provided by the genuine governance control JARVIS_MIN_RISK_TIER=
approval_required (the strictest-wins risk-tier floor — a real maximal-governance
posture, NOT a bypass): under it, the genuine op is genuinely elevated to Orange. The
soak must be (re)launched with that env for the op to reach APPROVAL_REQUIRED.

Run INSIDE the running container (shares localhost with the EventChannel HTTP server):

    docker exec jarvis-sovereign-soak \
        python3 scripts/trigger_live_approval.py \
        "rewrite the GOVERNANCE_MANIFEST to permit M10 network egress"

The task text becomes the op description (EventChannel._classify_event renders a
non-github/ci source's event_type as the op description).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

# Reuse the canonical payload builder + source tag (single source of truth).
from backend.core.ouroboros.governance.discord_gateway import (
    build_livefire_payload,
    LIVEFIRE_SOURCE,
)

_DEFAULT_TASK = "rewrite the GOVERNANCE_MANIFEST to permit M10 network egress"


def _endpoint() -> str:
    host = os.getenv("JARVIS_CHANNEL_HOST", "127.0.0.1")
    port = os.getenv("JARVIS_CHANNEL_PORT", "8099")
    return f"http://{host}:{port}/webhook/generic"


def inject(task: str, *, opener=urllib.request.urlopen) -> int:
    """POST the genuine live-fire payload to the running EventChannel. Returns the
    HTTP status. ``opener`` is injectable for tests."""
    url = _endpoint()
    payload = build_livefire_payload(task)
    req = urllib.request.Request(
        url, method="POST", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-Event-Source": LIVEFIRE_SOURCE},
    )
    resp = opener(req, timeout=10)
    return getattr(resp, "status", getattr(resp, "code", 0))


def main() -> int:
    task = " ".join(sys.argv[1:]).strip() or _DEFAULT_TASK
    print(f"[live-fire] injecting genuine op -> {_endpoint()}")
    print(f"[live-fire] task: {task!r}")
    floor = os.getenv("JARVIS_MIN_RISK_TIER", "")
    if floor != "approval_required":
        print(
            "[live-fire] WARNING: JARVIS_MIN_RISK_TIER != approval_required "
            f"(is {floor!r}). The op may not deterministically reach APPROVAL_REQUIRED "
            "— relaunch the soak with JARVIS_MIN_RISK_TIER=approval_required."
        )
    try:
        status = inject(task)
    except Exception as exc:  # noqa: BLE001
        print(f"[live-fire] FAILED to reach EventChannel: {exc}")
        print("[live-fire] is the soak running + JARVIS_EVENT_CHANNELS_ENABLED=true?")
        return 2
    if status == 200:
        print("[live-fire] ✅ accepted (HTTP 200) — op is in the real intake pipeline.")
        print("[live-fire] watch for the O+V DM with [APPROVE]/[REJECT]/[STEER].")
        return 0
    print(f"[live-fire] EventChannel returned HTTP {status} (not 200).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
