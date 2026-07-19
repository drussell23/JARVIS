"""``jarvis`` — the supervisor thin-client (Campaign Step 2, 2026-07-19).

The exact ``ov`` bifurcation applied to the manager plane (mandate 3:
the zero-trust probe, socket paths, and IPC clients are IMPORTED from
the existing thin-client stack — zero duplicated connection logic).
Boot budget <500ms: stdlib + TUI + IPC clients only.

Renders the **Agentic Topology Map** — live state of both personas
(Daniel/JARVIS on the system plane, Karen/O+V on the engineering
plane) and the Tri-State Audio Broker's hardware lease — fed by the
same pub/sub frames every other attached client receives (the bridge
is a true multiplexer; ``ov`` and ``jarvis`` attach simultaneously).
"""
from __future__ import annotations

import asyncio
import sys
from typing import Any, Dict, Optional


class TopologyMap:
    """Pure renderer: pub/sub frames in, one map block out. The SAME
    frame vocabulary the ov prompt-morph consumes — one broadcast,
    two presentations (the mandate's multiplexer proof)."""

    def __init__(self) -> None:
        self.state: Dict[str, Any] = {
            "phase": "?", "cost": None, "audio": "OFFLINE",
            "lease_held": False, "ops": [],
        }

    def on_hydration(self, payload: dict) -> None:
        try:
            status = payload.get("status") or {}
            self.state["phase"] = status.get("phase", "?")
            self.state["cost"] = (
                status.get("cost_spent_usd"), status.get("cost_budget_usd"),
            )
            self.state["ops"] = list(payload.get("ops") or [])[:4]
            audio = (payload.get("audio") or {}).get("state")
            if audio:
                self.state["audio"] = str(audio)
        except Exception:  # noqa: BLE001
            pass

    def on_audio_state(self, state: str) -> None:
        try:
            s = str(state or "").strip().upper()
            if s:
                self.state["audio"] = s
                self.state["lease_held"] = s not in (
                    "OFFLINE", "UNAVAILABLE", "HELD",
                )
        except Exception:  # noqa: BLE001
            pass

    def on_audio_handshake(self, msg: dict) -> None:
        try:
            lease = msg.get("lease") or {}
            self.state["lease_held"] = bool(lease.get("held"))
        except Exception:  # noqa: BLE001
            pass

    def render(self) -> str:
        """The Agentic Topology Map block. Persona activity derives
        from the audio FSM: SPEAKING/THINKING = Karen holds the floor
        on the engineering plane; the system plane (Daniel) is
        event-driven (wake briefings) and shown armed/idle."""
        audio = self.state["audio"]
        karen = {
            "LISTENING": "🎙 listening", "HEARING": "🎙 hearing you",
            "THINKING": "💭 thinking", "SPEAKING": "🗣 speaking",
            "HELD": "⚠ floor held elsewhere",
        }.get(audio, "· idle")
        daniel = "· armed (wake briefings)" if audio != "OFFLINE" else "· idle"
        lease = "HELD" if self.state["lease_held"] else "free"
        cost = self.state["cost"]
        cost_txt = (
            f"${cost[0]:.2f}/${cost[1]:.2f}"
            if cost and cost[0] is not None else "—"
        )
        ops = ", ".join(str(o) for o in self.state["ops"]) or "none"
        return (
            "╭─ agentic topology ─────────────────────────╮\n"
            f"│ JARVIS (Daniel · system)  {daniel:<17s}│\n"
            f"│ O+V    (Karen · eng)      {karen:<17s}│\n"
            f"│ audio lease: {lease:<6s} · fsm: {audio:<12s}│\n"
            f"│ organism: {self.state['phase']:<8s} cost {cost_txt:<12s}│\n"
            f"│ active ops: {ops[:30]:<30s}│\n"
            "╰────────────────────────────────────────────╯"
        )


def main(argv: Optional[list] = None) -> int:
    """<500ms boot: probe, attach, render, stream. NEVER a traceback
    on the operator surface."""
    # <500ms budget: NO Rich on this surface — the map is pure text.
    class _Plain:
        @staticmethod
        def print(text, **_kw):
            print(text)
    console = _Plain()
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("help", "--help", "-h"):
        console.print(
            "jarvis — supervisor thin-client: live agentic topology\n"
            "  jarvis            attach + live topology map\n"
            "  jarvis status     one-shot topology snapshot",
            markup=False, highlight=False,
        )
        return 0

    async def _session() -> int:
        # DRY: the ov zero-trust stack, byte-for-byte the same probes.
        from backend.core.ouroboros.battle_test.cockpit_attach import (
            CockpitAttachClient,
            attach_socket_path,
        )
        from backend.core.ouroboros.cli.thin_client import (
            clean_stale_socket,
            probe_socket,
        )

        topo = TopologyMap()
        path = attach_socket_path()
        state = await probe_socket(path)
        if state == "stale":
            clean_stale_socket(path)
            state = "absent"
        attach_ok = False
        if state == "live":
            repaint = asyncio.Event()

            def _on_audio(s: str) -> None:
                topo.on_audio_state(s)
                repaint.set()

            client = CockpitAttachClient(
                on_hydration=topo.on_hydration, on_audio_state=_on_audio,
            )
            attach_ok = await client.connect()
        # Audio plane (supervisor socket) — zero-trust, optional.
        audio_client = None
        try:
            from backend.core.ouroboros.governance.comms.duplex.audio_state_ipc import (  # noqa: E501
                AudioStateClient,
                socket_path as audio_socket_path,
            )
            # Zero-trust here too (DRY: the SAME probe) — a ghost
            # audio socket must cost 1ms, not a connect timeout.
            a_path = audio_socket_path()
            a_state = await probe_socket(a_path)
            if a_state == "stale":
                clean_stale_socket(a_path)
            elif a_state == "live":
                audio_client = AudioStateClient(
                    on_handshake=topo.on_audio_handshake,
                )
                if not await audio_client.connect():
                    audio_client = None
        except Exception:  # noqa: BLE001
            audio_client = None
        console.print(topo.render(), markup=False, highlight=False)
        if not attach_ok:
            console.print(
                "⎿ no organism on the attach socket — topology is the "
                "audio plane only ('ov' ignites one)",
                markup=False, highlight=False,
            )
            return 0
        if args and args[0] == "status":
            return 0
        console.print(
            "⎿ streaming topology · Ctrl+C detaches",
            markup=False, highlight=False,
        )
        try:
            while client.connected:
                repaint.clear()
                try:
                    await asyncio.wait_for(repaint.wait(), timeout=15.0)
                except asyncio.TimeoutError:
                    continue
                console.print(topo.render(), markup=False, highlight=False)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await client.close()
            if audio_client is not None:
                await audio_client.close()
        return 0

    try:
        return asyncio.run(_session())
    except KeyboardInterrupt:
        console.print("⎿ detached", markup=False, highlight=False)
        return 0
    except Exception:  # noqa: BLE001 — never a traceback on this surface
        console.print("jarvis: degraded — see logs", markup=False)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
