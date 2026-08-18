"""``ov doctor`` — the full-chain connectivity matrix (Slice B/C).

Every edge is asserted with the SAME probe its real consumer uses (the
Slice-A law: no probe may be weaker than the contract it vouches for).

**The edge set is declared in :data:`EDGES`, and that declaration is the
only place it is written down.** This docstring used to enumerate the edges
and call the matrix "8-edge"; ``probe_edge_compute`` was added as edge 9 and
this text kept saying eight, while two tests kept asserting eight and failed
on ``main`` for days. Prose that restates a data structure is prose that
will eventually contradict it, so the per-edge summaries now live on
:class:`EdgeSpec` where the code can read them too.

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


@dataclass(frozen=True)
class EdgeSpec:
    """One edge of the connectivity matrix, declared once.

    ``number`` is the operator-facing position, ``key`` the stable
    identifier code refers to, ``summary`` the one-line explanation that
    used to live in this module's docstring. Keeping the prose here is the
    point: a description parked in a docstring is a fifth place that claims
    to know the edge set, and it was already wrong."""

    number: int
    key: str
    name: str
    summary: str
    #: True for edges that only exist under an opt-in flag. They are
    #: declared here so they get a NUMBER from the same sequence as
    #: everything else -- which is exactly what the live probe did not have.
    optional: bool = False

    @property
    def label(self) -> str:
        """The rendered edge label -- DERIVED, never spelled twice."""
        return f"{self.number} {self.name}"


#: THE declaration of the matrix. Ordered, and the order IS canonical.
#:
#: WHY THIS EXISTS
#: ---------------
#: `probe_edge_compute` was added as edge 9 and four separate places went on
#: describing an 8-edge matrix: this module's own docstring ("the 8-edge
#: full-chain connectivity matrix", enumerating 1-8), the hand-ordered list
#: in `run_matrix`, and two tests asserting `len(report.verdicts) == 8`. The
#: tests failed on `main` for days. Nothing was authoritative, so nothing
#: could be updated.
#:
#: The edge's identity was also re-spelled at ~30 `EdgeVerdict(...)` call
#: sites -- every return path of every probe repeating its own label -- so a
#: rename could half-land and a typo would invent a tenth edge that renders
#: once and matches nothing.
#:
#: Adding an edge is now ONE entry here. Ordering, cardinality, labels and
#: the operator-facing prose all follow, and `test_edge_registry_is_the_only
#: _source` pins that no call site may reintroduce a literal.
EDGES: "Tuple[EdgeSpec, ...]" = (
    EdgeSpec(1, "process", "process",
             "an organism process exists (pgrep, bounded)"),
    EdgeSpec(2, "cockpit", "cockpit UDS",
             "thin_client.probe_socket(deep=True): SERVING means the "
             "hydration frame is actually served, not merely accepted"),
    EdgeSpec(3, "hydration", "hydration",
             "the frame parses; subsystem states read from it"),
    EdgeSpec(4, "fabrics", "hive fabrics",
             "hive aggregator attachment (trinity/sse/emitter) from the "
             "hydration ``fabrics`` block (daemon-side pull provider)"),
    EdgeSpec(5, "sensors", "sensors",
             "GET /channel/health (EventChannelServer)"),
    EdgeSpec(6, "providers", "providers",
             "hydration ``liquidity`` block (DW=tokens lane, Claude=time "
             "lane, J-Prime=sovereignty lane)"),
    EdgeSpec(7, "liveness", "liveness",
             "GET /observability/liveness (Gap 2: static n runtime)"),
    EdgeSpec(8, "mcp", "mcp servers",
             "JARVIS_MCP_CONFIG declared servers (NEVER executed)"),
    EdgeSpec(9, "compute", "compute",
             "host compute topology -- socket-independent BY DESIGN, so a "
             "machine the organism has never run on can still answer "
             "'can this host load anything?'"),
    # `--live` only. It was numbered 9 when 9 was free, `probe_edge_compute`
    # later took 9 as well, and `run_doctor(live=True)` appends this verdict
    # to the SAME report -- so an operator running `ov doctor --live` saw TWO
    # rows numbered 9. Neither site could see the other; only a shared
    # declaration can. Optional, so it is not part of `edge_count()`.
    EdgeSpec(10, "live", "live probe",
             "the trace-isolated synthetic web_search loop: tool execution "
             "-> Step 2 emitter -> aggregator -> UDS, proven end to end",
             optional=True),
)

#: The edges `run_matrix()` ALWAYS produces. `edge_count()` means this set,
#: not `len(EDGES)`: an opt-in probe that inflated the expected cardinality
#: would make every base-matrix assertion wrong by one.
MATRIX_EDGES: "Tuple[EdgeSpec, ...]" = tuple(e for e in EDGES if not e.optional)

_BY_KEY: "Dict[str, EdgeSpec]" = {e.key: e for e in EDGES}
_ORDER: "Dict[str, int]" = {e.label: i for i, e in enumerate(EDGES)}


def edge(key: str) -> str:
    """The canonical label for *key*. NEVER raises.

    An unknown key renders ``? <key>`` rather than raising or silently
    inventing a plausible label: this module's contract is that no probe may
    raise from a return path, and a diagnostic that fabricates an edge name
    is worse than one that says it does not recognise it. The AST pin in the
    test suite is what stops an unknown key from ever shipping."""
    spec = _BY_KEY.get(str(key))
    return spec.label if spec is not None else f"? {key}"


def edge_count() -> int:
    """How many edges `run_matrix()` produces. The number no test may hardcode.

    Counts `MATRIX_EDGES`, so declaring a new opt-in probe never silently
    changes what the base matrix is expected to contain."""
    return len(MATRIX_EDGES)


def order_verdicts(verdicts: "List[EdgeVerdict]") -> "List[EdgeVerdict]":
    """Sort verdicts into canonical edge order. NEVER raises.

    `run_matrix` used to achieve this by placing them in a hand-written list
    under a `# canonical edge order: 3,4,5,6,7,8,9` comment -- correct, and
    correct only for as long as someone reads the comment. Deriving the order
    from the declaration means a new edge lands in the right place because of
    where it was declared, not because of where it was appended.

    Unrecognised labels sort last, in arrival order, so an edge this registry
    has never heard of is still REPORTED. Dropping it would make the matrix
    quietly lie about what it probed."""
    try:
        return sorted(
            verdicts,
            key=lambda v: (_ORDER.get(getattr(v, "edge", ""), len(EDGES)),),
        )
    except Exception:  # noqa: BLE001 -- ordering must never break a report
        return list(verdicts)


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
            return EdgeVerdict(edge("process"), EdgeState.OK,
                               f"pid {', '.join(pids[:3])}")
        return EdgeVerdict(edge("process"), EdgeState.SEVERED,
                           "no organism process")
    except Exception as exc:  # noqa: BLE001
        return EdgeVerdict(edge("process"), EdgeState.DEGRADED,
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
        if state == "booting":
            # Slice-A patience, applied to diagnosis: a boot-starved daemon
            # is a WAIT state, not a verdict. Re-probe with the same
            # jittered-backoff deep handshake the ignite path uses, under a
            # doctor-bounded window — a transient never reads as a fault.
            from backend.core.ouroboros.cli.thin_client import await_socket
            try:
                raw = os.environ.get("JARVIS_DOCTOR_BOOT_WAIT_S", "")
                boot_wait = float(raw) if raw else 20.0
            except (TypeError, ValueError):
                boot_wait = 20.0
            if await await_socket(path, deadline_s=boot_wait):
                state = "live"
        mapping = {
            "live": (EdgeState.OK, "hydration served"),
            "booting": (EdgeState.DEGRADED,
                        "accepting but not yet serving (boot-starved)"),
            "stale": (EdgeState.SEVERED, "ghost socket (dead daemon)"),
            "absent": (EdgeState.SEVERED, "no socket"),
        }
        es, detail = mapping.get(state, (EdgeState.SEVERED, state))
        return EdgeVerdict(edge("cockpit"), es,
                           f"{detail} · {path}"), state
    except Exception as exc:  # noqa: BLE001
        return EdgeVerdict(edge("cockpit"), EdgeState.SEVERED,
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
            EdgeVerdict(edge("hydration"), EdgeState.SEVERED, "no frame served"),
            EdgeVerdict(edge("fabrics"), EdgeState.SKIPPED, "no hydration"),
            EdgeVerdict(edge("providers"), EdgeState.SKIPPED, "no hydration"),
        ]
    out: List[EdgeVerdict] = []
    # 3 — hydration content
    status = payload.get("status") or {}
    hyd = (status.get("hydration") or {}) if isinstance(status, dict) else {}
    subs = hyd.get("subsystems") or {}
    bad = [k for k, v in subs.items() if str(v) not in ("ok", "ready")]
    if payload.get("type") != "hydration":
        out.append(EdgeVerdict(edge("hydration"), EdgeState.DEGRADED,
                               f"unexpected first frame {payload.get('type')}"))
    elif bad:
        out.append(EdgeVerdict(edge("hydration"), EdgeState.DEGRADED,
                               "subsystems degraded: " + ", ".join(bad[:4])))
    else:
        loaded = hyd.get("loaded"), hyd.get("total")
        out.append(EdgeVerdict(
            edge("hydration"), EdgeState.OK,
            f"subsystems {loaded[0]}/{loaded[1]}" if loaded[0] is not None
            else "frame parsed"))
    # 4 — hive fabrics
    fab = payload.get("fabrics") or {}
    if not fab:
        out.append(EdgeVerdict(edge("fabrics"), EdgeState.DEGRADED,
                               "no fabrics block (older daemon?)"))
    else:
        tsubs = int(fab.get("trinity_subs", 0) or 0)
        sse = bool(fab.get("sse"))
        emitter = bool(fab.get("emitter"))
        missing = [n for n, on in (("trinity", tsubs > 0), ("sse", sse),
                                   ("emitter", emitter)) if not on]
        detail = f"trinity_subs={tsubs} sse={sse} emitter={emitter}"
        if not missing:
            out.append(EdgeVerdict(edge("fabrics"), EdgeState.OK, detail))
        elif missing == ["trinity"]:
            out.append(EdgeVerdict(
                edge("fabrics"), EdgeState.DEGRADED,
                detail + " (bus not yet materialized — re-attach pending)"))
        else:
            out.append(EdgeVerdict(edge("fabrics"), EdgeState.DEGRADED,
                                   detail + " missing: " + ",".join(missing)))
    # 6 — providers (DW = tokens lane · Claude = time lane · J-Prime =
    # sovereignty lane)
    liq = payload.get("liquidity") or {}
    providers = liq.get("providers") or {}
    if not providers:
        out.append(EdgeVerdict(edge("providers"), EdgeState.DEGRADED,
                               "no liquidity data in hydration"))
    else:
        rows = []
        exhausted = bool(liq.get("any_exhausted"))
        for name, row in list(providers.items())[:4]:
            tok = (row or {}).get("tokens_remaining")
            rows.append(f"{name}:{tok:,}" if isinstance(tok, int)
                        else f"{name}:?")
        out.append(EdgeVerdict(
            edge("providers"),
            EdgeState.DEGRADED if exhausted else EdgeState.OK,
            (" ".join(rows) + (" · RUNWAY EXHAUSTED" if exhausted else "")),
        ))
    return out


async def probe_edge_compute() -> EdgeVerdict:
    """Edge 9 — what this host can actually load, and which pool it lands in.

    Deliberately INDEPENDENT of the socket. This is the edge an operator
    needs most on a machine the organism has never run on: a fresh box where
    the daemon is not up yet, and the first question is whether the hardware
    is even visible. Every other hydration-derived edge goes SKIPPED there;
    this one still answers.

    Reports the accelerator reading and the admission ceiling together,
    because a refusal is unreadable without both — an operator who sees
    "deferred" on a 64 GB machine, with no accelerator half to explain it,
    turns the gate off. NEVER raises.
    """
    try:
        from backend.core.ouroboros.governance import compute_topology as ct
        if not ct.is_enabled():
            return EdgeVerdict(
                edge("compute"), EdgeState.SKIPPED,
                "topology probe disabled (JARVIS_COMPUTE_TOPOLOGY_ENABLED)")
        reading = await asyncio.wait_for(
            ct.resolve(), timeout=_edge_timeout_s() * 4)
    except asyncio.TimeoutError:
        return EdgeVerdict(edge("compute"), EdgeState.DEGRADED,
                           "topology probe timed out — driver may be wedged")
    except Exception as exc:  # noqa: BLE001
        return EdgeVerdict(edge("compute"), EdgeState.DEGRADED,
                           f"topology unavailable ({type(exc).__name__})")

    if not getattr(reading, "measured", False):
        return EdgeVerdict(
            edge("compute"), EdgeState.DEGRADED,
            f"host unresolved ({reading.source}) — admission falls back to "
            f"the host bound with the unverified ceiling")

    gib = 1024 ** 3
    detail = (f"{reading.topology.value} {reading.resolved_class} · "
              f"{reading.usable_bytes / gib:.1f} GiB usable · "
              f"src={reading.source}")
    if not getattr(reading, "free_is_measured", True):
        detail += " · free derived from host RAM"
    try:
        from backend.core.ouroboros.governance import local_model_admission as lma
        snap = lma.snapshot()
        ooms = sum((snap.get("observed_ooms") or {}).values())
        if ooms:
            detail += f" · {ooms} recent OOM(s) raising the margin"
    except Exception:  # noqa: BLE001 — the reading alone is still useful
        pass
    return EdgeVerdict(edge("compute"), EdgeState.OK, detail)


async def _http_get(path: str) -> Tuple[Optional[int], Optional[Any]]:
    """Minimal bounded HTTP/1.1 GET → (status_code, parsed_json|None).

    ``(None, None)`` means the SERVER is unreachable (refused/timeout) —
    a different fault class than a reachable server returning 404 (path
    not mounted, e.g. a daemon predating the endpoint). NEVER raises."""
    host, port = _channel_host_port()
    try:
        r, w = await asyncio.wait_for(
            asyncio.open_connection(host=host, port=port),
            timeout=_edge_timeout_s(),
        )
    except Exception:  # noqa: BLE001
        return None, None
    try:
        req = (f"GET {path} HTTP/1.1\r\nHost: {host}\r\n"
               "Connection: close\r\n\r\n")
        w.write(req.encode())
        await w.drain()
        raw = await asyncio.wait_for(
            r.read(262144), timeout=_edge_timeout_s())
        status_line = raw.split(b"\r\n", 1)[0]
        try:
            code = int(status_line.split()[1])
        except Exception:  # noqa: BLE001
            code = 0
        _head, _, body = raw.partition(b"\r\n\r\n")
        if code != 200:
            return code, None
        # tolerate chunked encoding by scanning for the JSON braces
        text = body.decode(errors="replace").strip()
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return code, None
        return code, json.loads(text[start:end + 1])
    except Exception:  # noqa: BLE001
        return None, None
    finally:
        try:
            w.close()
        except Exception:  # noqa: BLE001
            pass


async def probe_edge_sensors() -> EdgeVerdict:
    """Edge 5: /channel/health — the REAL Gap-4 schema: top-level
    ``status`` + per-sensor blocks under ``*_sensor`` keys."""
    code, data = await _http_get("/channel/health")
    if code is None:
        return EdgeVerdict(edge("sensors"), EdgeState.ABSENT,
                           "channel server unreachable "
                           f"({':'.join(map(str, _channel_host_port()))})")
    if data is None:
        return EdgeVerdict(edge("sensors"), EdgeState.DEGRADED,
                           f"server up, /channel/health returned {code}")
    healthy = str(data.get("status", "")).lower() == "healthy"
    sensor_blocks = {k: v for k, v in data.items()
                     if k.endswith("_sensor") and isinstance(v, dict)}
    wired = sum(1 for v in sensor_blocks.values() if v.get("wired"))
    total_events = data.get("total_events", 0)
    detail = (f"{wired}/{len(sensor_blocks)} webhook sensor(s) wired · "
              f"{total_events} events routed")
    if healthy:
        return EdgeVerdict(edge("sensors"), EdgeState.OK, detail)
    return EdgeVerdict(edge("sensors"), EdgeState.DEGRADED,
                       f"status={data.get('status')} · {detail}")


async def probe_edge_liveness() -> EdgeVerdict:
    """Edge 7: /observability/liveness (static ∩ runtime capabilities)."""
    code, data = await _http_get("/observability/liveness")
    if code is None:
        return EdgeVerdict(edge("liveness"), EdgeState.ABSENT,
                           "channel server unreachable")
    if data is None:
        return EdgeVerdict(
            edge("liveness"), EdgeState.ABSENT,
            f"server up, path not mounted ({code}) — daemon predates "
            "the endpoint or the router is disabled")
    caps = data.get("capabilities") or data.get("liveness") or data
    if isinstance(caps, dict) and caps:
        dormant = [k for k, v in caps.items()
                   if isinstance(v, (str, dict))
                   and "dormant" in str(v).lower()][:3]
        if dormant:
            return EdgeVerdict(edge("liveness"), EdgeState.DEGRADED,
                               "dormant: " + ", ".join(dormant))
        return EdgeVerdict(edge("liveness"), EdgeState.OK,
                           f"{len(caps)} capabilities reported")
    return EdgeVerdict(edge("liveness"), EdgeState.DEGRADED, "empty response")


async def probe_edge_mcp() -> EdgeVerdict:
    """Edge 8: MCP server CONFIGURATION only — never executed."""
    cfg = os.environ.get("JARVIS_MCP_CONFIG", "").strip()
    if not cfg:
        return EdgeVerdict(edge("mcp"), EdgeState.ABSENT,
                           "JARVIS_MCP_CONFIG unset")
    p = Path(cfg)
    if not p.exists():
        return EdgeVerdict(edge("mcp"), EdgeState.SEVERED,
                           f"config path missing: {cfg}")
    try:
        import yaml  # lazy — only when a config is declared
        data = yaml.safe_load(p.read_text()) or {}
        servers = data.get("servers") or data.get("mcp_servers") or []
        n = len(servers) if isinstance(servers, (list, dict)) else 0
        return EdgeVerdict(edge("mcp"), EdgeState.OK,
                           f"{n} server(s) declared (config only — "
                           "never executed by doctor)")
    except Exception as exc:  # noqa: BLE001
        return EdgeVerdict(edge("mcp"), EdgeState.DEGRADED,
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
        (hyd, lh), (v5, l5), (v7, l7), (v8, l8), (v9, l9) = await asyncio.gather(
            _timed(_read_hydration()),
            _timed(probe_edge_sensors()),
            _timed(probe_edge_liveness()),
            _timed(probe_edge_mcp()),
            _timed(probe_edge_compute()),
        )
        hydration_verdicts = _verdicts_from_hydration(hyd)
        for v in hydration_verdicts:
            v.latency_ms = lh / max(1, len(hydration_verdicts))
        v5.latency_ms, v7.latency_ms, v8.latency_ms = l5, l7, l8
        v9.latency_ms = l9
        e3, e4, e6 = hydration_verdicts
        # Order comes from the declaration, not from where these happen to
        # be appended. The hand-written sequence this replaces was correct
        # only for as long as someone read the comment above it.
        report.verdicts.extend(
            order_verdicts([e3, e4, v5, e6, v7, v8, v9]))
    else:
        # chain severed at the socket — probe the independent HTTP/static
        # edges anyway (they may localize the fault), skip the UDS-bound ones.
        (v5, l5), (v7, l7), (v8, l8), (v9, l9) = await asyncio.gather(
            _timed(probe_edge_sensors()),
            _timed(probe_edge_liveness()),
            _timed(probe_edge_mcp()),
            # Socket-independent BY DESIGN: on a machine the organism has
            # never run on, "can this host load anything?" is the first
            # question and the daemon is not up to answer it.
            _timed(probe_edge_compute()),
        )
        v5.latency_ms, v7.latency_ms, v8.latency_ms = l5, l7, l8
        v9.latency_ms = l9
        report.verdicts.extend(order_verdicts([
            EdgeVerdict(edge("hydration"), EdgeState.SKIPPED,
                        "socket not serving"),
            EdgeVerdict(edge("fabrics"), EdgeState.SKIPPED,
                        "socket not serving"),
            v5,
            EdgeVerdict(edge("providers"), EdgeState.SKIPPED,
                        "socket not serving"),
            v7, v8, v9,
        ]))
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
        return EdgeVerdict(edge("live"), EdgeState.SEVERED,
                           f"could not attach: {str(exc)[:60]}")
    try:
        # consume hydration first — it is ALSO the capability signature:
        # only daemons carrying tonight's code serve a ``fabrics`` block,
        # and only those daemons have the /doctor probe verb. An older
        # daemon gets NOTHING injected (no blind 45s wait, no unknown-verb
        # noise) — the doctor skips with the remedy instead.
        # Read-timeout and old-daemon are DIFFERENT worlds: a starved boot
        # serves nothing yet (wait and re-run), a genuinely old daemon
        # serves hydration WITHOUT the fabrics signature (restart it).
        # Conflating them misdiagnosed a current daemon as stale.
        hydration: Optional[Dict[str, Any]] = None
        try:
            first = await asyncio.wait_for(
                r.readline(), timeout=_edge_timeout_s())
            if first:
                hydration = json.loads(first.decode())
        except Exception:  # noqa: BLE001
            hydration = None
        if hydration is None:
            return EdgeVerdict(
                edge("live"), EdgeState.DEGRADED,
                "hydration not served within the bound (boot-starved?) — "
                "re-run `ov doctor --live` in a moment")
        if "fabrics" not in hydration:
            return EdgeVerdict(
                edge("live"), EdgeState.SKIPPED,
                "daemon predates /doctor probe (no fabrics capability "
                "signature) — restart the organism to enable the live loop")
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
                return EdgeVerdict(edge("live"), EdgeState.SEVERED,
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
                    edge("live"), EdgeState.OK,
                    "synthetic actor_edge frame observed — tool → emitter "
                    "→ aggregator → cockpit proven "
                    f"({payload.get('action_summary', '')[:60]})")
            text = str(frame.get("text", ""))
            if "[doctor] probe" in text:
                saw_verdict_line = text
        if saw_verdict_line:
            return EdgeVerdict(
                edge("live"), EdgeState.DEGRADED,
                "daemon ran the probe but no synthetic actor_edge frame "
                f"arrived in {_live_wait_s():.0f}s ({saw_verdict_line[:60]})")
        return EdgeVerdict(
            edge("live"), EdgeState.SEVERED,
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
        # Version-skew synthesizer: the "older daemon" signature (edges 4/7/9
        # each carrying a predates-hint) collapses into ONE actionable remedy
        # instead of three puzzling rows.
        stale_signs = [v for v in report.verdicts
                       if "predates" in v.detail or "older daemon" in v.detail]
        if stale_signs:
            pid = ""
            for v in report.verdicts:
                if v.edge.startswith("1 ") and "pid" in v.detail:
                    pid = v.detail.split("pid", 1)[1].strip().split(",")[0]
                    break
            console.print(
                f"⚕ the running organism predates this client "
                f"({len(stale_signs)} edge(s) affected) — restart it"
                + (f" (kill {pid}; then `ov`)" if pid else " (`ov`)")
                + " to load current capabilities",
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
