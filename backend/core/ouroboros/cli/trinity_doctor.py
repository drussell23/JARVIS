"""``trinity doctor`` — the control plane validates its own environment.

Operator authorization 2026-07-19. A pre-boot diagnostic that answers
"is this machine ready to run the organism?" honestly and *actionably* —
it names the PID holding a contested port, clears the ghost socket a
SIGKILL'd daemon left behind, and validates only the subsystems the
operator's ``.env`` actually turned on.

Mandate 1 — Root-Cause, not bind-probe:
  Port/UDS contention is resolved by IDENTIFYING the holder (``lsof -F``
  machine-readable → real PID + command; ``psutil`` fallback), never by
  blindly trying to bind and catching the failure. A contested port
  reports *who* holds it so the operator can act.

Mandate 2 — Architectural Purity (edge cases):
  * **Ghost Port Resolution** — a violently-killed daemon leaves an
    orphaned ``.sock``. The doctor classifies it with a REAL non-blocking
    connect (reusing ``thin_client.probe_socket``); a *stale* inode is
    auto-unlinked (``thin_client.clean_stale_socket``) so the next
    ``trinity up`` boots clean. A *live* socket is left alone.
  * **Configuration-Aware Validation** — the ``.env`` is parsed FIRST;
    an API key / system dep / model artifact is only demanded when its
    subsystem toggle (``JARVIS_AUDIO_BUS_ENABLED`` …) is asserted. No
    monolithic "everything must be present" gate.
  * **Zero-Load Model Verification** — model health is filesystem
    presence + size + a lightweight header checksum. It is STRICTLY
    FORBIDDEN to import ``torch`` or load a tensor into RAM.

Mandate 3 — DRY: reuses the Phase-0 ``env_bootstrap`` parser and the
``thin_client`` socket primitives + ``cockpit_attach`` path contract. No
duplicate config loader, no duplicate socket classifier.

Every public entry point NEVER raises — a diagnostic that crashes is
worse than useless.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import socket
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional


class Status(str, Enum):
    READY = "READY"     # healthy / remediated / good to boot
    WARN = "WARN"       # degraded but bootable (e.g. fallback provider)
    FAIL = "FAIL"       # will block a clean boot
    SKIPPED = "SKIPPED"  # subsystem toggle off — not checked by design


@dataclass
class CheckResult:
    name: str
    status: Status
    detail: str = ""
    remediation: str = ""          # what the doctor DID (e.g. unlinked ghost)
    holder_pid: Optional[int] = None
    holder_cmd: str = ""


@dataclass
class DoctorReport:
    results: List[CheckResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(r.status is Status.FAIL for r in self.results)

    def add(self, r: CheckResult) -> None:
        self.results.append(r)


# ---------------------------------------------------------------------------
# Root-cause port/socket holder identification (mandate 1)
# ---------------------------------------------------------------------------

@dataclass
class Holder:
    pid: int
    command: str
    name: str = ""


def _lsof_holder(args: List[str]) -> Optional[Holder]:
    """Run ``lsof -F`` with the given selector args and parse the FIRST
    holder (machine-readable field output — p<pid>/c<command>/n<name>).
    Returns None when nothing holds it or lsof is unavailable. NEVER
    raises."""
    if shutil.which("lsof") is None:
        return None
    try:
        proc = subprocess.run(
            ["lsof", "-nP", "-Fpcn", *args],
            capture_output=True, text=True, timeout=5,
        )
        # rc != 0 with empty output simply means "nothing holds it".
        pid: Optional[int] = None
        command = ""
        name = ""
        for line in proc.stdout.splitlines():
            if not line:
                continue
            tag, val = line[0], line[1:]
            if tag == "p":
                if pid is not None:
                    break                       # first holder only
                try:
                    pid = int(val)
                except ValueError:
                    pid = None
            elif tag == "c":
                command = val
            elif tag == "n":
                name = val
        if pid is None:
            return None
        return Holder(pid=pid, command=command, name=name)
    except Exception:  # noqa: BLE001
        return None


def _psutil_port_holder(port: int) -> Optional[Holder]:
    """Fallback PID identification for a listening TCP port when lsof is
    absent. Iterates the process table (own processes are always
    visible; others best-effort). NEVER raises."""
    try:
        import psutil
    except Exception:  # noqa: BLE001
        return None
    try:
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                for c in proc.net_connections(kind="inet"):
                    if (c.laddr and getattr(c.laddr, "port", None) == port
                            and c.status == psutil.CONN_LISTEN):
                        return Holder(pid=proc.info["pid"],
                                      command=proc.info.get("name") or "")
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                continue
    except Exception:  # noqa: BLE001
        return None
    return None


def find_port_holder(port: int) -> Optional[Holder]:
    """Who is LISTENING on this TCP port? lsof primary (root-less,
    accurate on macOS), psutil fallback. None = the port is free.
    NEVER raises."""
    h = _lsof_holder([f"-iTCP:{port}", "-sTCP:LISTEN"])
    if h is not None:
        return h
    return _psutil_port_holder(port)


def find_socket_holder(path: Path) -> Optional[Holder]:
    """Who holds this UDS inode? Best-effort PID (informational — the
    doctor doesn't need it to remediate, but reports it for a *live*
    socket). NEVER raises."""
    try:
        if not path.exists():
            return None
        return _lsof_holder([f"{path}"])
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def _is_placeholder(val: str) -> bool:
    """A key that is present-but-fake (the .env example scaffolding).
    Case-insensitive substring match against the usual tells."""
    low = (val or "").strip().lower()
    if not low:
        return True
    return any(t in low for t in (
        "your", "replace", "example", "xxxx", "changeme", "placeholder",
        "sk-...", "<", "todo",
    ))


def check_python_runtime() -> CheckResult:
    import sys
    v = sys.version_info
    # Repo mandate: Python 3.9+ (no asyncio.timeout).
    if (v.major, v.minor) < (3, 9):
        return CheckResult(
            "python-runtime", Status.FAIL,
            detail=f"Python {v.major}.{v.minor} < 3.9 required",
        )
    return CheckResult(
        "python-runtime", Status.READY,
        detail=f"Python {v.major}.{v.minor}.{v.micro}",
    )


def _backend_port() -> int:
    try:
        p = int(os.environ.get("JARVIS_BACKEND_PORT", "0") or "0")
        # 0 = "auto" (supervisor scans the range from 8010); probe the
        # canonical first port of that range.
        return p if p > 0 else 8010
    except ValueError:
        return 8010


def check_backend_port() -> CheckResult:
    """Is the backend port free, or contested — and by WHOM (mandate 1)?"""
    port = _backend_port()
    holder = find_port_holder(port)
    if holder is None:
        return CheckResult("backend-port", Status.READY,
                           detail=f"port {port} free")
    # A JARVIS/uvicorn/python holder is very likely our own already-running
    # backend — that's informational, not a fault. A foreign holder blocks.
    ours = any(tok in (holder.command or "").lower()
               for tok in ("python", "uvicorn", "jarvis"))
    status = Status.WARN if ours else Status.FAIL
    detail = (f"port {port} held by pid {holder.pid} ({holder.command})"
              + (" — likely an existing backend; `trinity status` to confirm"
                 if ours else " — foreign process, free it before `trinity up`"))
    return CheckResult("backend-port", status, detail=detail,
                       holder_pid=holder.pid, holder_cmd=holder.command)


async def check_attach_socket() -> CheckResult:
    """Ghost Port Resolution (mandate 2): classify the UDS with a REAL
    connect; auto-unlink a stale ghost so the next boot is clean."""
    try:
        from backend.core.ouroboros.cli.thin_client import (
            probe_socket, clean_stale_socket,
        )
        from backend.core.ouroboros.battle_test.cockpit_attach import (
            attach_socket_path,
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult("attach-socket", Status.WARN,
                           detail=f"socket primitives unavailable: {exc}")

    path = attach_socket_path()
    state = await probe_socket(path)               # live / stale / absent

    if state == "absent":
        return CheckResult("attach-socket", Status.READY,
                           detail=f"clean path ({path})")
    if state == "live":
        holder = find_socket_holder(path)
        return CheckResult(
            "attach-socket", Status.READY,
            detail=f"live daemon home ({path})"
                   + (f", pid {holder.pid}" if holder else ""),
            holder_pid=holder.pid if holder else None,
            holder_cmd=holder.command if holder else "",
        )
    # state == "stale" → GHOST from a SIGKILL'd daemon. Remediate.
    removed = clean_stale_socket(path)
    if removed:
        return CheckResult(
            "attach-socket", Status.READY,
            detail=f"orphaned socket cleared ({path})",
            remediation="unlinked dead UDS — path ready for clean boot",
        )
    return CheckResult(
        "attach-socket", Status.FAIL,
        detail=f"stale socket at {path} could not be unlinked "
               "(check filesystem permissions)",
    )


def check_provider_keys() -> List[CheckResult]:
    """Config-aware provider validation (mandate 2). Anthropic is always
    required (CLAUDE_MODEL is always resolved); DoubleWord is the Tier-0
    preference but degrades to Claude, so a missing DW key is a WARN, not
    a FAIL."""
    out: List[CheckResult] = []
    anthropic = os.environ.get("ANTHROPIC_API_KEY", "")
    if _is_placeholder(anthropic):
        out.append(CheckResult(
            "provider-anthropic", Status.FAIL,
            detail="ANTHROPIC_API_KEY missing/placeholder — the Tier-1 "
                   "fallback provider cannot authenticate",
        ))
    else:
        out.append(CheckResult("provider-anthropic", Status.READY,
                               detail="ANTHROPIC_API_KEY present"))

    dw = os.environ.get("DOUBLEWORD_API_KEY", "")
    if _is_placeholder(dw):
        out.append(CheckResult(
            "provider-doubleword", Status.WARN,
            detail="DOUBLEWORD_API_KEY missing — Tier-0 unavailable; the "
                   "cascade falls back to Claude (higher cost, still works)",
        ))
    else:
        out.append(CheckResult("provider-doubleword", Status.READY,
                               detail="DOUBLEWORD_API_KEY present"))
    return out


# --- Config-aware subsystems + zero-load model manifest -------------------

def _env_true(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _header_checksum(path: Path, *, nbytes: int = 65536) -> str:
    """sha256 of the first ``nbytes`` — a lightweight identity fingerprint
    that NEVER reads a multi-GB weight file whole and NEVER imports torch.
    Empty string on any error."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            h.update(fh.read(nbytes))
        return h.hexdigest()[:16]
    except Exception:  # noqa: BLE001
        return ""


@dataclass
class ModelArtifact:
    label: str
    env_var: str                  # env override for the path (no hardcoding)
    default_path: str
    min_bytes: int = 1


@dataclass
class Subsystem:
    name: str
    toggle: str                   # the .env flag that turns it on
    models: List[ModelArtifact] = field(default_factory=list)


#: Declarative subsystem → model manifest. Paths come from env with
#: sane defaults (no hardcoding); checked ONLY when the toggle is on.
_SUBSYSTEMS: List[Subsystem] = [
    Subsystem(
        name="voice", toggle="JARVIS_AUDIO_BUS_ENABLED",
        models=[
            ModelArtifact(
                "speaker-profile-db", "JARVIS_SPEAKER_DB",
                "~/.jarvis/learning/jarvis_learning.db", min_bytes=1024,
            ),
        ],
    ),
    Subsystem(
        name="vision", toggle="JARVIS_VISION_LOOP_ENABLED",
        models=[],   # VLM is remote (Qwen3-VL) — nothing local to verify
    ),
]


def check_subsystem_models() -> List[CheckResult]:
    """Zero-load model verification (mandate 2), config-aware (mandate 2):
    for each ENABLED subsystem, verify its model artifacts by filesystem
    presence + size + header checksum — NO torch, NO tensor load."""
    out: List[CheckResult] = []
    for sub in _SUBSYSTEMS:
        if not _env_true(sub.toggle):
            out.append(CheckResult(
                f"subsystem-{sub.name}", Status.SKIPPED,
                detail=f"{sub.toggle}=off — not validated",
            ))
            continue
        if not sub.models:
            out.append(CheckResult(
                f"subsystem-{sub.name}", Status.READY,
                detail="enabled; no local model artifacts to verify",
            ))
            continue
        for m in sub.models:
            raw = os.environ.get(m.env_var, "") or m.default_path
            path = Path(os.path.expanduser(raw))
            name = f"model-{sub.name}-{m.label}"
            try:
                if not path.exists():
                    out.append(CheckResult(
                        name, Status.FAIL,
                        detail=f"{m.label} missing at {path} "
                               f"({sub.name} is enabled)",
                    ))
                    continue
                size = path.stat().st_size
                if size < m.min_bytes:
                    out.append(CheckResult(
                        name, Status.FAIL,
                        detail=f"{m.label} at {path} is {size}B "
                               f"(< {m.min_bytes}B — looks truncated)",
                    ))
                    continue
                fp = _header_checksum(path)
                out.append(CheckResult(
                    name, Status.READY,
                    detail=f"{m.label} present ({size:,}B, hdr={fp or 'n/a'})",
                ))
            except Exception as exc:  # noqa: BLE001
                out.append(CheckResult(
                    name, Status.WARN,
                    detail=f"{m.label} check degraded: {exc}",
                ))
    return out


def _mic_authorization() -> Optional[str]:
    """Non-invasive macOS microphone TCC status WITHOUT prompting:
    ``authorized`` / ``denied`` / ``notDetermined`` / ``restricted``, or
    None if it can't be determined (non-macOS / pyobjc absent). NEVER
    raises, NEVER triggers a consent dialog."""
    try:
        from AVFoundation import (            # type: ignore
            AVCaptureDevice, AVMediaTypeAudio,
        )
        # 0 notDetermined, 1 restricted, 2 denied, 3 authorized.
        status = AVCaptureDevice.authorizationStatusForMediaType_(
            AVMediaTypeAudio)
        return {0: "notDetermined", 1: "restricted",
                2: "denied", 3: "authorized"}.get(int(status), "unknown")
    except Exception:  # noqa: BLE001
        return None


def _screen_authorization() -> Optional[bool]:
    """Non-invasive screen-recording TCC preflight (``CGPreflightScreen
    CaptureAccess`` — checks WITHOUT prompting). True/False, or None if
    unavailable. NEVER raises, NEVER prompts."""
    try:
        import Quartz                          # type: ignore
        fn = getattr(Quartz, "CGPreflightScreenCaptureAccess", None)
        if fn is None:
            return None
        return bool(fn())
    except Exception:  # noqa: BLE001
        return None


def check_tcc_consent() -> List[CheckResult]:
    """TCC Privacy Handshake (mandate 2, fail-soft). Config-aware: mic is
    only checked when voice is enabled, screen only when vision is. A
    denial is a WARN that names the graceful downgrade — NOT a FAIL (the
    daemon runs degraded, it never crashes)."""
    out: List[CheckResult] = []

    if _env_true("JARVIS_AUDIO_BUS_ENABLED"):
        mic = _mic_authorization()
        if mic is None:
            out.append(CheckResult(
                "tcc-microphone", Status.SKIPPED,
                detail="consent state undeterminable (pyobjc/AVFoundation "
                       "unavailable) — will resolve at first app launch"))
        elif mic == "authorized":
            out.append(CheckResult("tcc-microphone", Status.READY,
                                   detail="microphone consent granted"))
        elif mic in ("denied", "restricted"):
            out.append(CheckResult(
                "tcc-microphone", Status.WARN,
                detail=f"microphone TCC {mic} — voice DOWNGRADES to "
                       "text-only mode (no crash); grant it in System "
                       "Settings › Privacy › Microphone to enable voice"))
        else:  # notDetermined
            out.append(CheckResult(
                "tcc-microphone", Status.READY,
                detail="microphone consent not yet requested — the app "
                       "prompts on first launch"))

    if _env_true("JARVIS_VISION_LOOP_ENABLED"):
        screen = _screen_authorization()
        if screen is None:
            out.append(CheckResult(
                "tcc-screen", Status.SKIPPED,
                detail="consent state undeterminable — resolves at launch"))
        elif screen:
            out.append(CheckResult("tcc-screen", Status.READY,
                                   detail="screen-recording consent granted"))
        else:
            out.append(CheckResult(
                "tcc-screen", Status.WARN,
                detail="screen-recording TCC not granted — visual awareness "
                       "DISABLED (no crash); grant it in System Settings › "
                       "Privacy › Screen Recording"))
    return out


def check_redis() -> Optional[CheckResult]:
    """Config-aware dependency probe: only if REDIS_ENABLED. A REAL
    non-blocking TCP connect (root-cause), not a library import."""
    if not _env_true("REDIS_ENABLED"):
        return CheckResult("dep-redis", Status.SKIPPED,
                           detail="REDIS_ENABLED=off")
    host = os.environ.get("REDIS_HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("REDIS_PORT", "6379"))
    except ValueError:
        port = 6379
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1.5)
            s.connect((host, port))
        return CheckResult("dep-redis", Status.READY,
                           detail=f"reachable at {host}:{port}")
    except Exception:  # noqa: BLE001
        return CheckResult(
            "dep-redis", Status.WARN,
            detail=f"REDIS_ENABLED but {host}:{port} unreachable — "
                   "caching degrades to in-process",
        )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

async def run_doctor() -> DoctorReport:
    """Execute the full diagnostic suite. Parses ``.env`` FIRST (DRY —
    the Phase-0 loader) so every downstream check is config-aware. NEVER
    raises."""
    report = DoctorReport()
    # Mandate 3 — the ONE canonical .env parser; real env still wins.
    try:
        from backend.core.env_bootstrap import load_env_once
        load_env_once()
    except Exception:  # noqa: BLE001
        pass

    report.add(check_python_runtime())
    report.add(check_backend_port())
    report.add(await check_attach_socket())
    for r in check_provider_keys():
        report.add(r)
    redis = check_redis()
    if redis is not None:
        report.add(redis)
    for r in check_subsystem_models():
        report.add(r)
    for r in check_tcc_consent():
        report.add(r)
    return report


_GLYPH: Dict[Status, str] = {
    Status.READY: "⏺", Status.WARN: "▲", Status.FAIL: "✗", Status.SKIPPED: "○",
}


def render_report(report: DoctorReport, console) -> None:
    """Human-facing render (SerpentFlow glyph vocabulary). NEVER
    raises."""
    fails = sum(1 for r in report.results if r.status is Status.FAIL)
    warns = sum(1 for r in report.results if r.status is Status.WARN)
    remediated = [r for r in report.results if r.remediation]
    for r in report.results:
        g = _GLYPH.get(r.status, "·")
        console.print(f"{g} {r.name}: {r.status.value} — {r.detail}",
                      markup=False)
        if r.remediation:
            console.print(f"  ⎿ remediated: {r.remediation}", markup=False)
    console.print("", markup=False)
    if remediated:
        console.print(f"⏺ auto-remediated {len(remediated)} issue(s) "
                      "(ghost sockets cleared)", markup=False)
    if fails:
        console.print(f"✗ {fails} blocking issue(s), {warns} warning(s) — "
                      "resolve the ✗ items before `trinity up`", markup=False)
    else:
        console.print(f"⏺ environment READY ({warns} warning(s)) — "
                      "`trinity up` is clear to boot", markup=False)


def doctor_main(console=None) -> int:
    """Entry point for ``trinity doctor``. Returns 0 when bootable (no
    FAIL), 1 otherwise. NEVER raises."""
    try:
        if console is None:
            from backend.core.ouroboros.ui.theme import build_console
            console = build_console()
        report = asyncio.run(run_doctor())
        render_report(report, console)
        return 0 if report.ok else 1
    except Exception as exc:  # noqa: BLE001
        try:
            (console or None) and console.print(
                f"✗ doctor failed to run: {exc}", markup=False)
        except Exception:  # noqa: BLE001
            pass
        return 1


__all__ = [
    "Status", "CheckResult", "DoctorReport", "Holder",
    "find_port_holder", "find_socket_holder",
    "check_python_runtime", "check_backend_port", "check_attach_socket",
    "check_provider_keys", "check_subsystem_models", "check_redis",
    "run_doctor", "render_report", "doctor_main",
]
