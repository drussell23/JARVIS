#!/usr/bin/env python3
"""
Test script: Fire a mock Hive debate through IPC (port 8742).

Simulates a full Trinity debate visible in the native SwiftUI HiveView:
  1. Cognitive transition: BASELINE → FLOW
  2. Thread lifecycle: OPEN → DEBATING
  3. Agent log: health_monitor_agent detects memory pressure
  4. JARVIS observes
  5. J-Prime proposes a fix
  6. Reactor validates and approves
  7. Thread lifecycle: CONSENSUS → EXECUTING
  8. Cognitive transition: FLOW → BASELINE (spindown)

Usage:
  python3 scripts/test_hive_ipc.py

Requires: JARVISHUD running in Xcode (brainstem listening on port 8742)
"""

import json
import socket
import time
import uuid
from datetime import datetime, timezone


IPC_HOST = "127.0.0.1"
IPC_PORT = 8742

THREAD_ID = f"thr_{uuid.uuid4().hex[:12]}"


def send_event(sock: socket.socket, event_type: str, data: dict) -> None:
    """Send a newline-delimited JSON event to the IPC server."""
    envelope = json.dumps({"event_type": event_type, "data": data})
    sock.sendall((envelope + "\n").encode("utf-8"))
    print(f"  → [{event_type}] sent")


def now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def msg_id() -> str:
    return f"msg_{uuid.uuid4().hex[:12]}"


def main():
    print(f"\n🐝 Hive IPC Test — Thread {THREAD_ID}")
    print(f"   Connecting to {IPC_HOST}:{IPC_PORT}...\n")

    try:
        sock = socket.create_connection((IPC_HOST, IPC_PORT), timeout=5)
    except ConnectionRefusedError:
        print("❌ Connection refused. Is the JARVISHUD app running in Xcode?")
        print("   The brainstem IPC server must be listening on port 8742.")
        return
    except Exception as e:
        print(f"❌ Connection error: {e}")
        return

    print("✅ Connected to IPC server\n")
    time.sleep(0.5)

    # 1. Cognitive transition: BASELINE → FLOW
    print("Step 1: BASELINE → FLOW")
    send_event(sock, "cognitive_transition", {
        "from_state": "baseline",
        "to_state": "flow",
        "reason_code": "T2_FLOW_TRIGGER",
        "_seq": 1,
    })
    time.sleep(1)

    # 2. Thread lifecycle: OPEN
    print("Step 2: Thread OPEN")
    send_event(sock, "thread_lifecycle", {
        "thread_id": THREAD_ID,
        "title": "Memory Pressure in Vision Loop",
        "state": "open",
        "_seq": 2,
    })
    time.sleep(0.5)

    # 3. Thread lifecycle: DEBATING
    print("Step 3: Thread → DEBATING")
    send_event(sock, "thread_lifecycle", {
        "thread_id": THREAD_ID,
        "state": "debating",
        "_seq": 3,
    })
    time.sleep(1)

    # 4. Agent log: health_monitor_agent
    print("Step 4: Agent log — health_monitor_agent")
    send_event(sock, "agent_log", {
        "type": "agent_log",
        "thread_id": THREAD_ID,
        "message_id": msg_id(),
        "agent_name": "health_monitor_agent",
        "trinity_parent": "jarvis",
        "severity": "warning",
        "category": "memory_pressure",
        "payload": {"metric": "ram_percent", "value": 87.3, "threshold": 85.0, "trend": "rising"},
        "ts": now_iso(),
        "monotonic_ns": time.monotonic_ns(),
    })
    time.sleep(1.5)

    # 5. JARVIS observes
    print("Step 5: JARVIS observes")
    send_event(sock, "persona_reasoning", {
        "type": "persona_reasoning",
        "thread_id": THREAD_ID,
        "message_id": msg_id(),
        "persona": "jarvis",
        "role": "body",
        "intent": "observe",
        "references": [],
        "reasoning": "I'm detecting sustained memory pressure from the vision loop. RAM at 87.3% and climbing. The FramePipeline has 47 active SHM segments with 12 stale entries older than 60 seconds that aren't being evicted.",
        "confidence": 0.92,
        "model_used": "Qwen/Qwen3.5-397B-A17B-FP8",
        "token_cost": 847,
        "manifesto_principle": "§7 Absolute Observability",
        "validate_verdict": None,
        "ts": now_iso(),
    })
    time.sleep(2)

    # 6. J-Prime proposes
    print("Step 6: J-Prime proposes")
    send_event(sock, "persona_reasoning", {
        "type": "persona_reasoning",
        "thread_id": THREAD_ID,
        "message_id": msg_id(),
        "persona": "j_prime",
        "role": "mind",
        "intent": "propose",
        "references": [],
        "reasoning": "Root cause: FramePipeline in SHM mode has no TTL eviction. The zero-alloc retina downsample path writes segments but never marks them for cleanup. Proposal: Add TTL-based eviction with 30s max age to FramePipeline._shm_cleanup(), with a background sweep every 5 seconds. Per Manifesto §3, this is a deterministic fix — no agentic routing needed.",
        "confidence": 0.87,
        "model_used": "Qwen/Qwen3.5-397B-A17B-FP8",
        "token_cost": 1203,
        "manifesto_principle": "§3 Spinal Cord",
        "validate_verdict": None,
        "ts": now_iso(),
    })
    time.sleep(2)

    # 7. Reactor validates (approve)
    print("Step 7: Reactor validates — APPROVED")
    send_event(sock, "persona_reasoning", {
        "type": "persona_reasoning",
        "thread_id": THREAD_ID,
        "message_id": msg_id(),
        "persona": "reactor",
        "role": "immune_system",
        "intent": "validate",
        "references": [],
        "reasoning": "Safety review: TTL eviction is low-risk — it only touches SHM segments owned by the vision process. No cross-domain access. No file I/O outside /tmp. AST scan: clean. Blast radius: minimal. Approved for Ouroboros synthesis.",
        "confidence": 0.95,
        "model_used": "Qwen/Qwen3.5-397B-A17B-FP8",
        "token_cost": 923,
        "manifesto_principle": "§6 Iron Gate",
        "validate_verdict": "approve",
        "ts": now_iso(),
    })
    time.sleep(1.5)

    # 8. Thread lifecycle: CONSENSUS
    print("Step 8: Thread → CONSENSUS")
    send_event(sock, "thread_lifecycle", {
        "thread_id": THREAD_ID,
        "state": "consensus",
        "_seq": 8,
    })
    time.sleep(1)

    # 9. Thread lifecycle: EXECUTING
    print("Step 9: Thread → EXECUTING")
    send_event(sock, "thread_lifecycle", {
        "thread_id": THREAD_ID,
        "state": "executing",
        "linked_op_id": f"op-{uuid.uuid4().hex[:8]}",
        "_seq": 9,
    })
    time.sleep(2)

    # 10. Cognitive transition: FLOW → BASELINE
    print("Step 10: FLOW → BASELINE (spindown)")
    send_event(sock, "cognitive_transition", {
        "from_state": "flow",
        "to_state": "baseline",
        "reason_code": "T3_SPINDOWN_ALL_THREADS_RESOLVED",
        "_seq": 10,
    })
    time.sleep(0.5)

    sock.close()
    print(f"\n✅ Mock debate complete! Check the Hive tab in JARVISHUD.")
    print(f"   Thread: '{THREAD_ID}' should show the full Trinity debate.")
    print(f"   Cognitive state should cycle: BASELINE → FLOW → BASELINE\n")


if __name__ == "__main__":
    main()
