"""``ov doctor`` — the 8-edge full-chain connectivity matrix (Slice B/C).

Every edge is asserted with the SAME probe its real consumer uses (the
Slice-A law: no probe may be weaker than the contract it vouches for):

  1 process   — an organism process exists (pgrep, bounded)
  2 cockpit   — thin_client.probe_socket(deep=True): SERVING means the
                hydration frame is actually served, not merely accepted
  3 hydration — the frame parses; subsystem states read from it
  4 fabrics   — hive aggregator attachment (trinity/sse/emitter) from the
                hydration ``fabrics`` block (daemon-side pull provider)
  5 sensors   — GET /channel/health (EventChannelServer)
  6 providers — hydration ``liquidity`` block (DW=tokens lane, Claude=time
                lane, J-Prime=sovereignty lane)
  7 liveness  — GET /observability/liveness (Gap 2: static ∩ runtime)
  8 mcp       — JARVIS_MCP_CONFIG declared servers (NEVER executed)

Structure (mandate 1): edges 1+2 gate the chain; everything downstream
that is structurally independent (hydration-read, channel-health,
liveness, mcp-config) runs under ONE ``asyncio.gather`` — the command is
as fast as its slowest probe, not the sum. Every probe is bounded, returns
a typed enum, and NEVER raises. Exit codes: 0 all-green · 1 degraded ·
2 chain severed (the FIRST broken edge is named).

``--live`` (Slice C) additionally asks the daemon (over the cockpit input
lane) to fire the trace-context-isolated synthetic web_search probe, then
watches its own connection for the resulting synthetic ``actor_edge``
frame — tool execution → Step 2 emitter → aggregator → UDS proven in one
self-verifying loop, with zero state mutation daemon-side.
"""
from __future__ import annotations

import asyncio
import enum
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


class EdgeState(str, enum.Enum):
    OK = "OK"                 # green — contract proven
    DEGRADED = "DEGRADED"     # yellow — reachable but impaired
    SEVERED = "SEVERED"       # red — the chain breaks here
    SKIPPED = "SKIPPED"       # upstream severed — not probed
    ABSENT = "ABSENT"         # optional surface not configured (neutral)


@dataclass
class EdgeVerdict:
    edge: str
    state: EdgeState
    detail: str = ""
    latency_ms: float = 0.0


@dataclass
class DoctorReport:
    verdicts: List[EdgeVerdict] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        states = [v.state for v in self.verdicts]
        if EdgeState.SEVERED in states:
            return 2
        if EdgeState.DEGRADED in states:
            return 1
        return 0

    @property
    def first_broken(self) -> Optional[EdgeVerdict]:
        for v in self.verdicts:
            if v.state is EdgeState.SEVERED:
                return v
        for v in self.verdicts:
            if v.state is EdgeState.DEGRADED:
                return v
        return None


def _channel_host_port() -> Tuple[str, int]:
    host = os.environ.get("JARVIS_CHANNEL_HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("JARVIS_CHANNEL_PORT", "") or 8099)
    except (TypeError, ValueError):
        port = 8099
    return host, port


def _edge_timeout_s() -> float:
    try:
        raw = os.environ.get("JARVIS_DOCTOR_EDGE_TIMEOUT_S", "")
        return float(raw) if raw else 3.0
    except (TypeError, ValueError):
        return 3.0


async def _timed(coro: Any) -> Tuple[Any, float]:
    t0 = time.monotonic()
    out = await coro
    return out, (time.monotonic() - t0) * 1000.0


# ---------------------------------------------------------------------------
# edge probes — each bounded, typed, NEVER raises
# ---------------------------------------------------------------------------


async def probe_edge_process() -> EdgeVerdict:
    """Edge 1: a live organism process (harness or converged daemon)."""
    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                "pgrep", "-f",
                "ouroboros_battle_test|unified_supervisor.*--headless",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            ),
            timeout=_edge_timeout_s(),
        )
        out, _ = await asyncio.wait_for(
            proc.communicate(), timeout=_edge_timeout_s())
        pids = [p for p in out.decode().split() if p.strip()]
        if pids:
            return EdgeVerdict("1 process", EdgeState.OK,
                               f"pid {', '.join(pids[:3])}")
        return EdgeVerdict("1 process", EdgeState.SEVERED,
                           "no organism process")
    except Exception as exc:  # noqa: BLE001
        return EdgeVerdict("1 process", EdgeState.DEGRADED,
                           f"probe error: {str(exc)[:60]}")


async def probe_edge_socket() -> Tuple[EdgeVerdict, str]:
    """Edge 2: the deep application handshake (Slice A's probe)."""
    try:
        from backend.core.ouroboros.cli.thin_client import (
            probe_socket,
        )
        from backend.core.ouroboros.battle_test.cockpit_attach import (
            attach_socket_path,
        )
        path = attach_socket_path()
        state = await probe_socket(path, timeout=_edge_timeout_s(), deep=True)
        mapping = {
            "live": (EdgeState.OK, "hydration served"),
            "booting": (EdgeState.DEGRADED,
                        "accepting but not yet serving (boot-starved)"),
            "stale": (EdgeState.SEVERED, "ghost socket (dead daemon)"),
            "absent": (EdgeState.SEVERED, "no socket"),
        }
        es, detail = mapping.get(state, (EdgeState.SEVERED, state))
        return EdgeVerdict("2 cockpit UDS", es,
                           f"{detail} · {path}"), state
    except Exception as exc:  # noqa: BLE001
        return EdgeVerdict("2 cockpit UDS", EdgeState.SEVERED,
                           f"probe error: {str(exc)[:60]}"), "error"


async def _read_hydration() -> Optional[Dict[str, Any]]:
    """One bounded connect + first-frame read + parse. NEVER raises."""
    try:
        from backend.core.ouroboros.battle_test.cockpit_attach import (
            attach_socket_path,
        )
        r, w = await asyncio.wait_for(
            asyncio.open_unix_connection(path=str(attach_socket_path())),
            timeout=_edge_timeout_s(),
        )
        try:
            line = await asyncio.wait_for(
                r.readline(), timeout=_edge_timeout_s())
            return json.loads(line.decode())
        finally:
            try:
                w.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        return None


def _verdicts_from_hydration(
    payload: Optional[Dict[str, Any]],
) -> List[EdgeVerdict]:
    """Edges 3, 4, 6 — all read from ONE hydration frame (one connect)."""
    if payload is None:
        return [
            EdgeVerdict("3 hydration", EdgeState.SEVERED, "no frame served"),
            EdgeVerdict("4 hive fabrics", EdgeState.SKIPPED, "no hydration"),
            EdgeVerdict("6 providers", EdgeState.SKIPPED, "no hydration"),
        ]
    out: List[EdgeVerdict] = []
    # 3 — hydration content
    status = payload.get("status") or {}
    hyd = (status.get("hydration") or {}) if isinstance(status, dict) else {}
    subs = hyd.get("subsystems") or {}
    bad = [k for k, v in subs.items() if str(v) not in ("ok", "ready")]
    if payload.get("type") != "hydration":
        out.append(EdgeVerdict("3 hydration", EdgeState.DEGRADED,
                               f"unexpected first frame {payload.get('type')}"))
    elif bad:
        out.append(EdgeVerdict("3 hydration", EdgeState.DEGRADED,
                               "subsystems degraded: " + ", ".join(bad[:4])))
    else:
        loaded = hyd.get("loaded"), hyd.get("total")
        out.append(EdgeVerdict(
            "3 hydration", EdgeState.OK,
            f"subsystems {loaded[0]}/{loaded[1]}" if loaded[0] is not None
            else "frame parsed"))
    # 4 — hive fabrics
    fab = payload.get("fabrics") or {}
    if not fab:
        out.append(EdgeVerdict("4 hive fabrics", EdgeState.DEGRADED,
                               "no fabrics block (older daemon?)"))
    else:
        tsubs = int(fab.get("trinity_subs", 0) or 0)
        sse = bool(fab.get("sse"))
        emitter = bool(fab.get("emitter"))
        missing = [n for n, on in (("trinity", tsubs > 0), ("sse", sse),
                                   ("emitter", emitter)) if not on]
        detail = f"trinity_subs={tsubs} sse={sse} emitter={emitter}"
        if not missing:
            out.append(EdgeVerdict("4 hive fabrics", EdgeState.OK, detail))
        elif missing == ["trinity"]:
            out.append(EdgeVerdict(
                "4 hive fabrics", EdgeState.DEGRADED,
                detail + " (bus not yet materialized — re-attach pending)"))
        else:
            out.append(EdgeVerdict("4 hive fabrics", EdgeState.DEGRADED,
                                   detail + " missing: " + ",".join(missing)))
    # 6 — providers (DW = tokens lane · Claude = time lane · J-Prime =
    # sovereignty lane)
    liq = payload.get("liquidity") or {}
    providers = liq.get("providers") or {}
    if not providers:
        out.append(EdgeVerdict("6 providers", EdgeState.DEGRADED,
                               "no liquidity data in hydration"))
    else:
        rows = []
        exhausted = bool(liq.get("any_exhausted"))
        for name, row in list(providers.items())[:4]:
            tok = (row or {}).get("tokens_remaining")
            rows.append(f"{name}:{tok:,}" if isinstance(tok, int)
                        else f"{name}:?")
        out.append(EdgeVerdict(
            "6 providers",
            EdgeState.DEGRADED if exhausted else EdgeState.OK,
            (" ".join(rows) + (" · RUNWAY EXHAUSTED" if exhausted else "")),
        ))
    return out


async def _fetch_json(path: str) -> Optional[Any]:
    """Minimal bounded HTTP/1.1 GET → parsed JSON body. NEVER raises."""
    host, port = _channel_host_port()
    try:
        r, w = await asyncio.wait_for(
            asyncio.open_connection(host=host, port=port),
            timeout=_edge_timeout_s(),
        )
        try:
            req = (f"GET {path} HTTP/1.1\r\nHost: {host}\r\n"
                   "Connection: close\r\n\r\n")
            w.write(req.encode())
            await w.drain()
            raw = await asyncio.wait_for(
                r.read(262144), timeout=_edge_timeout_s())
            head, _, body = raw.partition(b"\r\n\r\n")
            if b" 200 " not in head.split(b"\r\n", 1)[0]:
                return None
            # tolerate chunked encoding by scanning for the JSON braces
            text = body.decode(errors="replace").strip()
            start = text.find("{")
            end = text.rfind("}")
            if start < 0 or end <= start:
                return None
            return json.loads(text[start:end + 1])
        finally:
            try:
                w.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        return None


async def probe_edge_sensors() -> EdgeVerdict:
    """Edge 5: /channel/health — the REAL Gap-4 schema: top-level
    ``status`` + per-sensor blocks under ``*_sensor`` keys."""
    data = await _fetch_json("/channel/health")
    if data is None:
        return EdgeVerdict("5 sensors", EdgeState.ABSENT,
                           "channel server unreachable "
                           f"({':'.join(map(str, _channel_host_port()))})")
    healthy = str(data.get("status", "")).lower() == "healthy"
    sensor_blocks = {k: v for k, v in data.items()
                     if k.endswith("_sensor") and isinstance(v, dict)}
    wired = sum(1 for v in sensor_blocks.values() if v.get("wired"))
    total_events = data.get("total_events", 0)
    detail = (f"{wired}/{len(sensor_blocks)} webhook sensor(s) wired · "
              f"{total_events} events routed")
    if healthy:
        return EdgeVerdict("5 sensors", EdgeState.OK, detail)
    return EdgeVerdict("5 sensors", EdgeState.DEGRADED,
                       f"status={data.get('status')} · {detail}")


async def probe_edge_liveness() -> EdgeVerdict:
    """Edge 7: /observability/liveness (static ∩ runtime capabilities)."""
    data = await _fetch_json("/observability/liveness")
    if data is None:
        return EdgeVerdict("7 liveness", EdgeState.ABSENT,
                           "liveness endpoint unreachable")
    caps = data.get("capabilities") or data.get("liveness") or data
    if isinstance(caps, dict) and caps:
        dormant = [k for k, v in caps.items()
                   if isinstance(v, (str, dict))
                   and "dormant" in str(v).lower()][:3]
        if dormant:
            return EdgeVerdict("7 liveness", EdgeState.DEGRADED,
                               "dormant: " + ", ".join(dormant))
        return EdgeVerdict("7 liveness", EdgeState.OK,
                           f"{len(caps)} capabilities reported")
    return EdgeVerdict("7 liveness", EdgeState.DEGRADED, "empty response")


async def probe_edge_mcp() -> EdgeVerdict:
    """Edge 8: MCP server CONFIGURATION only — never executed."""
    cfg = os.environ.get("JARVIS_MCP_CONFIG", "").strip()
    if not cfg:
        return EdgeVerdict("8 mcp servers", EdgeState.ABSENT,
                           "JARVIS_MCP_CONFIG unset")
    p = Path(cfg)
    if not p.exists():
        return EdgeVerdict("8 mcp servers", EdgeState.SEVERED,
                           f"config path missing: {cfg}")
    try:
        import yaml  # lazy — only when a config is declared
        data = yaml.safe_load(p.read_text()) or {}
        servers = data.get("servers") or data.get("mcp_servers") or []
        n = len(servers) if isinstance(servers, (list, dict)) else 0
        return EdgeVerdict("8 mcp servers", EdgeState.OK,
                           f"{n} server(s) declared (config only — "
                           "never executed by doctor)")
    except Exception as exc:  # noqa: BLE001
        return EdgeVerdict("8 mcp servers", EdgeState.DEGRADED,
                           f"config unparseable: {str(exc)[:60]}")


# ---------------------------------------------------------------------------
# the matrix
# ---------------------------------------------------------------------------


async def run_matrix() -> DoctorReport:
    """Edges 1+2 gate; every independent downstream probe runs in ONE
    gather. NEVER raises."""
    report = DoctorReport()
    (v1, l1), ((v2, sock_state), l2) = await asyncio.gather(
        _timed(probe_edge_process()), _timed(probe_edge_socket()))
    v1.latency_ms, v2.latency_ms = l1, l2
    report.verdicts.append(v1)
    report.verdicts.append(v2)

    if sock_state == "live":
        (hyd, lh), (v5, l5), (v7, l7), (v8, l8) = await asyncio.gather(
            _timed(_read_hydration()),
            _timed(probe_edge_sensors()),
            _timed(probe_edge_liveness()),
            _timed(probe_edge_mcp()),
        )
        hydration_verdicts = _verdicts_from_hydration(hyd)
        for v in hydration_verdicts:
            v.latency_ms = lh / max(1, len(hydration_verdicts))
        v5.latency_ms, v7.latency_ms, v8.latency_ms = l5, l7, l8
        # canonical edge order: 3,4,5,6,7,8
        e3, e4, e6 = hydration_verdicts
        report.verdicts.extend([e3, e4, v5, e6, v7, v8])
    else:
        # chain severed at the socket — probe the independent HTTP/static
        # edges anyway (they may localize the fault), skip the UDS-bound ones.
        (v5, l5), (v7, l7), (v8, l8) = await asyncio.gather(
            _timed(probe_edge_sensors()),
            _timed(probe_edge_liveness()),
            _timed(probe_edge_mcp()),
        )
        v5.latency_ms, v7.latency_ms, v8.latency_ms = l5, l7, l8
        report.verdicts.extend([
            EdgeVerdict("3 hydration", EdgeState.SKIPPED, "socket not serving"),
            EdgeVerdict("4 hive fabrics", EdgeState.SKIPPED,
                        "socket not serving"),
            v5,
            EdgeVerdict("6 providers", EdgeState.SKIPPED, "socket not serving"),
            v7, v8,
        ])
    return report


# ---------------------------------------------------------------------------
# --live (Slice C): the synthetic probe loop
# ---------------------------------------------------------------------------


def _live_wait_s() -> float:
    try:
        raw = os.environ.get("JARVIS_DOCTOR_LIVE_WAIT_S", "")
        return float(raw) if raw else 45.0
    except (TypeError, ValueError):
        return 45.0


async def run_live_probe() -> EdgeVerdict:
    """Ask the daemon to fire the trace-isolated synthetic web_search, then
    watch THIS connection for the synthetic actor_edge frame. Proves tool
    execution → emitter → aggregator → cockpit end-to-end. NEVER raises."""
    try:
        from backend.core.ouroboros.battle_test.cockpit_attach import (
            attach_socket_path,
        )
        r, w = await asyncio.wait_for(
            asyncio.open_unix_connection(path=str(attach_socket_path())),
            timeout=_edge_timeout_s(),
        )
    except Exception as exc:  # noqa: BLE001
        return EdgeVerdict("9 live probe", EdgeState.SEVERED,
                           f"could not attach: {str(exc)[:60]}")
    try:
        # consume hydration first, then request the probe over the input lane
        try:
            await asyncio.wait_for(r.readline(), timeout=_edge_timeout_s())
        except Exception:  # noqa: BLE001
            pass
        w.write((json.dumps(
            {"type": "input", "text": "/doctor probe"},
            separators=(",", ":")) + "\n").encode())
        await w.drain()
        deadline = time.monotonic() + _live_wait_s()
        saw_verdict_line = ""
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                line = await asyncio.wait_for(
                    r.readline(), timeout=max(0.1, remaining))
            except asyncio.TimeoutError:
                break
            if not line:
                return EdgeVerdict("9 live probe", EdgeState.SEVERED,
                                   "daemon closed the connection mid-probe")
            try:
                frame = json.loads(line.decode())
            except Exception:  # noqa: BLE001
                continue
            payload = frame.get("payload") if isinstance(
                frame.get("payload"), dict) else frame
            detail = payload.get("detail") or {}
            if (payload.get("hive")
                    and (detail.get("trace_class") == "synthetic_probe"
                         or "[synthetic probe]" in str(
                             payload.get("action_summary", "")))):
                return EdgeVerdict(
                    "9 live probe", EdgeState.OK,
                    "synthetic actor_edge frame observed — tool → emitter "
                    "→ aggregator → cockpit proven "
                    f"({payload.get('action_summary', '')[:60]})")
            text = str(frame.get("text", ""))
            if "[doctor] probe" in text:
                saw_verdict_line = text
        if saw_verdict_line:
            return EdgeVerdict(
                "9 live probe", EdgeState.DEGRADED,
                "daemon ran the probe but no synthetic actor_edge frame "
                f"arrived in {_live_wait_s():.0f}s ({saw_verdict_line[:60]})")
        return EdgeVerdict(
            "9 live probe", EdgeState.SEVERED,
            f"no probe response within {_live_wait_s():.0f}s "
            "(daemon may not support /doctor probe)")
    finally:
        try:
            w.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Rich rendering (ov console idiom) + entry point
# ---------------------------------------------------------------------------

_STATE_STYLE = {
    EdgeState.OK: "green", EdgeState.DEGRADED: "yellow",
    EdgeState.SEVERED: "red", EdgeState.SKIPPED: "dim",
    EdgeState.ABSENT: "dim",
}


def render_report(console: Any, report: DoctorReport) -> None:
    """Rich table in the ov cockpit idiom. NEVER raises."""
    try:
        from rich.table import Table
        table = Table(title="ov doctor — full-chain connectivity",
                      title_justify="left", expand=False)
        table.add_column("edge", no_wrap=True)
        table.add_column("state", no_wrap=True)
        table.add_column("detail")
        table.add_column("ms", justify="right", style="dim")
        for v in report.verdicts:
            table.add_row(
                v.edge,
                f"[{_STATE_STYLE.get(v.state, 'white')}]{v.state.value}[/]",
                v.detail, f"{v.latency_ms:.0f}")
        console.print(table)
        broken = report.first_broken
        if broken is None:
            console.print("● chain green — all edges proven",
                          markup=False, highlight=False)
        else:
            console.print(
                f"● first broken edge: {broken.edge} — {broken.detail}",
                markup=False, highlight=False)
    except Exception:  # noqa: BLE001
        for v in report.verdicts:
            try:
                console.print(f"{v.edge}: {v.state.value} — {v.detail}",
                              markup=False, highlight=False)
            except Exception:  # noqa: BLE001
                pass


def run_doctor(console: Any, *, live: bool = False) -> int:
    """The ov verb entry. Returns the exit code. NEVER raises."""
    async def _run() -> int:
        report = await run_matrix()
        if live:
            v = await run_live_probe()
            report.verdicts.append(v)
        render_report(console, report)
        return report.exit_code

    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        return 130
    except Exception:  # noqa: BLE001
        return 2


__all__ = [
    "EdgeState", "EdgeVerdict", "DoctorReport", "run_matrix",
    "run_live_probe", "run_doctor", "render_report",
]
