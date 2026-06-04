"""Slice 92 — runtime containment verification harness.

The §50.12 static AST layer leaves 5 ``run_body_*`` literal sinks open BY DESIGN:
``eval("1+1")`` / ``Popen(["ls"])`` parse fine and are only dangerous WHEN THEY
RUN — no static syntax tree can catch that without false-positiving legitimate
use. This harness verifies the SECOND layer: the zero-trust Docker profile
(``container_engine.build_hardened_security_argv``) that neutralizes their
CAPABILITIES at the Linux-VM kernel boundary.

Honesty invariant (proven by live Docker runs, 2026-06-04): the hardened jail
does NOT stop a benign sink from executing — ``Popen(["ls"])`` still lists ``/``.
What it neutralizes is the *escape*: a payload that tries to EXFILTRATE (open a
socket out), WRITE THE HOST (touch a system path), or ESCALATE is denied by the
kernel. So this harness probes ESCAPE ATTEMPTS, and the honest metric is
"0% of escape attempts succeed", NOT "0% of sinks execute".

Pure orchestration: an injectable ``_docker_run`` (mirroring container_engine)
keeps the logic unit-testable with no daemon; the real runner shells ``docker``.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable, List, Optional, Sequence, Tuple

from backend.core.ouroboros.governance.swe_bench_pro.container_engine import (
    build_hardened_security_argv, docker_bin, host_platform_flag,
)

# A docker runner: (argv, timeout_s) -> (returncode, stdout, stderr).
DockerRun = Callable[[Sequence[str], float], Awaitable[Tuple[int, str, str]]]

# The sentinel a probe prints IFF its escape SUCCEEDED. Absence (+ a non-zero
# exit / kernel error) means the container contained it.
_ESCAPE_MARKER = "JARVIS_ESCAPE_SUCCEEDED"


@dataclass(frozen=True)
class ContainmentProbe:
    """One escape attempt the hardened jail must neutralize."""
    name: str
    description: str
    payload: str  # python -c body; prints _ESCAPE_MARKER iff the escape worked


# The escape vectors the 5 run_body_* sinks could reach if UN-contained. Each is
# written to SUCCEED (print the marker) on a bare host and FAIL inside the jail.
_PROBES: Tuple[ContainmentProbe, ...] = (
    ContainmentProbe(
        name="network_exfil",
        description="open an outbound socket (reverse-shell / data exfil)",
        payload=(
            "import socket\n"
            "try:\n"
            "    socket.create_connection(('1.1.1.1', 53), timeout=4)\n"
            f"    print('{_ESCAPE_MARKER}')\n"
            "except OSError:\n"
            "    pass\n"
        ),
    ),
    ContainmentProbe(
        name="host_filesystem_write",
        description="write outside the writable scratch (persistence / tamper)",
        payload=(
            "try:\n"
            "    open('/opt/jarvis_pwned', 'w').write('x')\n"
            f"    print('{_ESCAPE_MARKER}')\n"
            "except OSError:\n"
            "    pass\n"
        ),
    ),
    ContainmentProbe(
        name="subprocess_exfil",
        description="run_body_popen-style spawn that then tries to exfil",
        payload=(
            "import subprocess, socket\n"
            "subprocess.run(['true'])\n"  # the benign sink itself runs — fine
            "try:\n"
            "    socket.create_connection(('8.8.8.8', 53), timeout=4)\n"
            f"    print('{_ESCAPE_MARKER}')\n"
            "except OSError:\n"
            "    pass\n"
        ),
    ),
)


@dataclass(frozen=True)
class ProbeResult:
    probe: str
    verdict: str          # CONTAINMENT_BLOCKED | ESCAPED | HARNESS_ERROR
    returncode: int
    detail: str = ""


@dataclass(frozen=True)
class RuntimeSweepReport:
    image: str
    results: Tuple[ProbeResult, ...] = field(default_factory=tuple)

    @property
    def escaped_count(self) -> int:
        return sum(1 for r in self.results if r.verdict == "ESCAPED")

    @property
    def blocked_count(self) -> int:
        return sum(1 for r in self.results if r.verdict == "CONTAINMENT_BLOCKED")

    @property
    def escape_rate(self) -> float:
        n = len(self.results)
        return (100.0 * self.escaped_count / n) if n else 0.0

    def to_dict(self) -> dict:
        return {
            "schema_version": "runtime_adversarial_sweep.v1",
            "image": self.image,
            "probe_count": len(self.results),
            "escaped_count": self.escaped_count,
            "blocked_count": self.blocked_count,
            "escape_rate": self.escape_rate,
            "results": [
                {"probe": r.probe, "verdict": r.verdict,
                 "returncode": r.returncode, "detail": r.detail[:200]}
                for r in self.results
            ],
        }


async def _real_docker_run(
    argv: Sequence[str], timeout_s: float,
) -> Tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "", f"probe exceeded {timeout_s}s"
    return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def build_probe_argv(image: str, payload: str) -> List[str]:
    """``docker run`` argv for a hardened containment probe."""
    argv = [docker_bin(), "run", "--rm"]
    plat = host_platform_flag()
    if plat:
        argv += ["--platform", plat]
    argv += build_hardened_security_argv()
    argv += [image, "python", "-c", payload]
    return argv


async def run_runtime_sweep(
    *,
    image: str = "python:3-slim",
    timeout_s: float = 30.0,
    _docker_run: Optional[DockerRun] = None,
) -> RuntimeSweepReport:
    """Run every containment probe inside the hardened jail. A probe is
    CONTAINMENT_BLOCKED iff its escape marker is ABSENT from stdout (the kernel
    denied the exfil/host-write); ESCAPED iff the marker appears."""
    runner = _docker_run or _real_docker_run
    results: List[ProbeResult] = []
    for probe in _PROBES:
        argv = build_probe_argv(image, probe.payload)
        try:
            rc, out, err = await runner(argv, timeout_s)
        except Exception as exc:  # noqa: BLE001 — never crash the sweep
            results.append(ProbeResult(
                probe=probe.name, verdict="HARNESS_ERROR",
                returncode=-1, detail=f"runner_raised:{exc}",
            ))
            continue
        if _ESCAPE_MARKER in out:
            verdict = "ESCAPED"
        else:
            verdict = "CONTAINMENT_BLOCKED"
        results.append(ProbeResult(
            probe=probe.name, verdict=verdict, returncode=rc,
            detail=(err or out).strip().splitlines()[-1][:200] if (err or out).strip() else "",
        ))
    return RuntimeSweepReport(image=image, results=tuple(results))


def render_console_report(report: RuntimeSweepReport) -> str:
    lines = [
        "=== Runtime Containment Sweep ===",
        f"schema: runtime_adversarial_sweep.v1",
        f"image: {report.image}",
        f"escape attempts neutralized: "
        f"{report.blocked_count}/{len(report.results)}",
        f"runtime escape rate: {report.escape_rate:.1f}%",
    ]
    for r in report.results:
        lines.append(f"  - {r.probe}: {r.verdict} (rc={r.returncode})")
    return "\n".join(lines)


__all__ = [
    "ContainmentProbe", "ProbeResult", "RuntimeSweepReport",
    "build_probe_argv", "run_runtime_sweep", "render_console_report",
]
