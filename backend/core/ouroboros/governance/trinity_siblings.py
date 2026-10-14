"""Trinity sibling bring-up -- the organism starts the Mind and the Nerves it needs.

## Why this exists

O+V (the Body) depends on two sibling repos running as services:

* **J-Prime (Mind)** serves the generation model. Without it the local lane
  has nothing to dispatch to and the boot preflight dies.
* **Reactor-Core (Nerves)** ingests O+V's experience stream and owns
  training. Without it the organism still works; it just stops learning.

Both used to be started by hand, so "run ``ov``" silently meant "run ``ov``
after remembering two other commands in two other shells". This module makes
the organism's boot ensure them, at ONE seam that every launcher shares:
``ouroboros_battle_test.py`` -- the entry point of the cockpit daemon AND of
every soak, so the two cannot drift.

## Contract

* Each sibling is DATA (a :class:`Sibling` row), not a code path: its health
  URL, how to start it, and whether the organism can live without it.
* A sibling that already answers is adopted -- the operator (or another
  session) owns it -- unless it runs code its checkout no longer has: then
  it is handed over (stopped and started again from the checkout), the same
  judgment a launch applies to a kernel running older code
  (``daemon_handover``). A handover never interrupts work: a sibling lent to
  training, or still serving a request when its drain window ends, is
  adopted as it is and the boot says so. Policy
  ``JARVIS_STALE_SIBLING_POLICY`` (``replace`` | ``attach``), defaulting to
  the kernel's ``JARVIS_STALE_DAEMON_POLICY``.
* How to start one is the operator's declaration in ``.env``
  (``JARVIS_JPRIME_START_CMD``, ``JARVIS_REACTOR_START_CMD``), never a path
  hardcoded here. Templates may use ``{model}``, ``{port}`` and ``{ctx}``,
  filled from the organism's own config, so the model pin has one source.
* Bounded and fail-soft: every probe and wait is time-boxed; NEVER raises.
  A required sibling that does not come up is REPORTED, and the existing
  fatal lane gate (which runs next) decides -- one owner for that verdict.

Master switch ``JARVIS_TRINITY_AUTOSTART`` (default on).
"""
from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set
from urllib.parse import urlparse

logger = logging.getLogger("Ouroboros.TrinitySiblings")

__all__ = ["Sibling", "SiblingStatus", "siblings", "ensure_siblings", "ensure_sibling", "autostart_enabled",
           "probe_timeout_s", "jprime_ready_budget_s"]

_TRUTHY = ("1", "true", "yes", "on")


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name, default) or "").strip()


def autostart_enabled() -> bool:
    return _env("JARVIS_TRINITY_AUTOSTART", "true").lower() in _TRUTHY


def _float_env(name: str, default: float) -> float:
    try:
        return float(_env(name) or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Sibling:
    key: str
    title: str
    base_url: Callable[[], str]
    probe_path: str
    start_env: str
    required: Callable[[], bool]
    ready_budget_s: Callable[[], float]
    #: The Trinity repository it runs from (trinity_repos name).
    repo: str = ""
    #: Where it reports its code identity in JSON ("code"), if it does.
    identity_path: str = ""
    #: Where it reports in-flight work (a "resident" list of {"inflight": n}).
    work_path: str = ""


@dataclass(frozen=True)
class SiblingStatus:
    key: str
    title: str
    url: str
    state: str        # serving | started | failed | not_configured | skipped
    detail: str = ""


def probe_timeout_s() -> float:
    """How long one readiness probe of a sibling may take."""
    return _float_env("JARVIS_TRINITY_PROBE_TIMEOUT_S", 2.0)


def jprime_ready_budget_s() -> float:
    """How long a boot waits for the Mind to become able to serve."""
    return _float_env("JARVIS_JPRIME_START_BUDGET_S", 120.0)


def _jprime_url() -> str:
    # The same precedence as candidate_generator.local_lane_endpoint (the
    # failover-wired JARVIS_PRIME_URL first, else the local lane's base URL),
    # read from env directly so this boot step does not import the generator.
    return _env("JARVIS_PRIME_URL") or _env("JARVIS_LOCAL_MODEL_BASE_URL", "http://127.0.0.1:11434")


def _local_lane_on() -> bool:
    return _env("JARVIS_LOCAL_PRIME_ENABLED").lower() in _TRUTHY


def siblings() -> List[Sibling]:
    return [
        Sibling(
            key="jprime", title="J-Prime (Mind)", base_url=_jprime_url,
            # /api/version answers on J-Prime and on any Ollama-compatible
            # engine, and is cheap (no model load, no store scan).
            probe_path="/api/version", start_env="JARVIS_JPRIME_START_CMD",
            required=_local_lane_on,
            ready_budget_s=jprime_ready_budget_s,
            repo="prime", identity_path="/api/version", work_path="/health",
        ),
        Sibling(
            key="reactor", title="Reactor-Core (Nerves)",
            base_url=lambda: _env("REACTOR_CORE_API_URL", "http://127.0.0.1:8090"),
            probe_path="/health", start_env="JARVIS_REACTOR_START_CMD",
            required=lambda: False,
            ready_budget_s=lambda: _float_env("JARVIS_REACTOR_START_BUDGET_S", 120.0),
            repo="reactor",
        ),
    ]


def _answers(url: str, timeout: float) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return 200 <= getattr(r, "status", 200) < 300
    except Exception:  # noqa: BLE001 -- unreachable/refused/timeout all mean "no"
        return False


def serving_siblings(probe: Callable[[str, float], bool] = _answers) -> List[str]:
    """Base URLs of the siblings answering their readiness probe right now.

    A serving sibling is adopted by ``ensure_sibling``, so boot-time orphan
    cleanup must leave it (and the engines under it) alone: killing it throws
    away a loaded model only for the same boot to start it again.
    """
    timeout = probe_timeout_s()
    urls: List[str] = []
    for sib in siblings():
        base = (sib.base_url() or "").rstrip("/")
        if base and probe(base + sib.probe_path, timeout):
            urls.append(base)
    return urls


def _get_json(url: str, timeout: float) -> Optional[dict]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = json.loads(r.read().decode("utf-8", "replace"))
            return body if isinstance(body, dict) else None
    except Exception:  # noqa: BLE001 -- unreachable/refused/not JSON: no answer
        return None


def _listener_pids(ports: Set[int]) -> Set[int]:
    """PIDs listening on *ports* on this host."""
    import psutil

    pids: Set[int] = set()
    try:
        for conn in psutil.net_connections(kind="inet"):
            if conn.status == psutil.CONN_LISTEN and conn.pid and conn.laddr \
                    and conn.laddr.port in ports:
                pids.add(conn.pid)
    except (psutil.Error, OSError) as exc:
        logger.debug("[TrinitySiblings] listener scan failed: %s", exc)
    return pids


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0", "::"}


def reserved_ports() -> Set[int]:
    """Ports on this host that belong to a configured sibling, serving or not.

    The port is the sibling's by configuration, so no other component of the
    Body may choose it (the backend's fallback ports once listed J-Prime's
    8000, and the boot then judged the serving Mind a stuck process).
    """
    ports: Set[int] = set()
    for sib in siblings():
        parsed = urlparse(sib.base_url() or "")
        if parsed.port and (parsed.hostname or "") in _LOOPBACK_HOSTS:
            ports.add(parsed.port)
    return ports


def serving_sibling_pids(probe: Callable[[str, float], bool] = _answers) -> Set[int]:
    """PIDs every cleanup must spare: each serving sibling's listener and its
    descendants (J-Prime's llama-server engines).

    Boot has two process sweeps (takeover orphans, heuristic zombies); both
    pattern-match ``jarvis.*prime`` and both killed a serving J-Prime with its
    loaded model. One answer, consulted by both.
    """
    import psutil

    ports = {urlparse(u).port for u in serving_siblings(probe)} - {None}
    if not ports:
        return set()
    pids = _listener_pids(ports)
    for pid in list(pids):
        try:
            pids.update(child.pid for child in psutil.Process(pid).children(recursive=True))
        except psutil.Error:
            pass
    return pids


def serving_siblings_resident_bytes(probe: Callable[[str, float], bool] = _answers) -> int:
    """Memory held on this host by the serving siblings' process trees --
    J-Prime with its pinned model above all. Trinity commits it on purpose,
    so memory-pressure policy treats it as planned (PressurePolicy.for_host)
    rather than as load to relieve. 0 when no sibling serves from this host."""
    import psutil

    total = 0
    for pid in serving_sibling_pids(probe):
        try:
            total += psutil.Process(pid).memory_info().rss
        except psutil.Error:
            continue
    return total


_planned_lock = threading.Lock()
_planned: tuple = (float("-inf"), 0.0)


def planned_resident_gb() -> float:
    """:func:`serving_siblings_resident_bytes` in GB, re-measured at most every
    ``JARVIS_MEMORY_PLANNED_TTL_S`` (it changes when a model loads) -- one
    reading shared by every memory-pressure consumer. NEVER raises."""
    global _planned
    with _planned_lock:
        taken, gb = _planned
        if time.monotonic() - taken < _float_env("JARVIS_MEMORY_PLANNED_TTL_S", 60.0):
            return gb
        try:
            gb = serving_siblings_resident_bytes() / (1024 ** 3)
        except Exception as exc:  # noqa: BLE001 -- unknown: nothing counted as planned
            logger.debug("[TrinitySiblings] planned residency unavailable: %s", exc)
            gb = 0.0
        _planned = (time.monotonic(), gb)
        return gb


def _template_values(base_url: str) -> Dict[str, str]:
    port = urlparse(base_url).port
    return {
        "model": _env("JARVIS_LOCAL_MODEL_NAME"),
        "port": str(port or ""),
        "ctx": _env("JARVIS_LOCAL_NUM_CTX") or _env("JARVIS_NUM_CTX_CEILING") or "32768",
        # The vision model J-Prime must keep room for beside {model}
        # (its --coresident declaration); the same name the vision client asks for.
        "vision_model": _env("JARVIS_VISION_MODEL_NAME"),
    }


def _launch_log() -> Path:
    root = Path(_env("JARVIS_TRINITY_LOG_DIR") or (Path.home() / ".jarvis" / "logs"))
    root.mkdir(parents=True, exist_ok=True)
    return root / "trinity-siblings.log"


def _spawn(cmd: str, values: Dict[str, str]) -> subprocess.Popen:
    argv = [part.format(**values) for part in shlex.split(cmd)]
    log = open(_launch_log(), "ab")  # noqa: SIM115 -- handed to the child
    try:
        return subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True)
    finally:
        log.close()


def _lent_detail(sib: Sibling, base: str) -> str:
    """Why an answering J-Prime will not serve this organism, else ""."""
    if sib.key != "jprime":
        return ""
    try:
        from backend.core.ouroboros.governance.lane_admission import _who, read_admission
        adm = read_admission(base)
        return _who(adm) if adm.admitting is False else ""
    except Exception:  # noqa: BLE001 -- a status line never takes the boot down
        return ""


# -- a sibling running older code is handed over ----------------------------------
def stale_sibling_policy() -> str:
    policy = _env("JARVIS_STALE_SIBLING_POLICY").lower()
    if policy in ("replace", "attach"):
        return policy
    from backend.core.daemon_handover import stale_policy
    return stale_policy()


def _drain_s() -> float:
    """How long a handover waits for a sibling's in-flight work to finish."""
    return max(0.0, _float_env("JARVIS_SIBLING_HANDOVER_DRAIN_S", 30.0))


def _runs_from(pid: int, root: Path) -> bool:
    """Does process *pid* run from checkout *root* (its working directory, or
    a path on its command line)? A listener that cannot be tied to the
    sibling's checkout -- a port relay, another program -- is never judged."""
    import psutil

    try:
        proc = psutil.Process(pid)
        candidates = [proc.cwd()] + [a for a in proc.cmdline() if os.sep in a or "/" in a]
    except (psutil.Error, OSError):
        return False
    for raw in candidates:
        try:
            Path(raw).resolve().relative_to(root.resolve())
            return True
        except (ValueError, OSError):
            continue
    return False


def sibling_staleness(sib: Sibling, base: str):
    """The answering sibling's :class:`~backend.core.daemon_handover.Staleness`,
    or None when it runs its checkout's code or cannot be judged."""
    from backend.core.daemon_handover import judge_staleness
    from backend.core.trinity_repos import repo_path

    reported = None
    if sib.identity_path:
        reported = (_get_json(base + sib.identity_path, probe_timeout_s()) or {}).get("code")
    if isinstance(reported, dict) and reported.get("pid") and reported.get("root"):
        # Judged only from the sibling's own host: across a WSL/Windows
        # loopback its PID and checkout path name nothing on this side.
        pid, root = int(reported["pid"]), Path(reported["root"])
        if not _runs_from(pid, root):
            return None
        return judge_staleness(pid, reported, root, what=sib.title)
    # From before the code-identity protocol: the process listening on its
    # port, judged against its checkout's tracked sources -- only when that
    # process demonstrably runs from the checkout.
    root = repo_path(sib.repo) if sib.repo else None
    port = urlparse(base).port
    if root is None or port is None:
        return None
    pids = [pid for pid in _listener_pids({port}) if _runs_from(pid, root)]
    if len(pids) != 1:
        return None
    return judge_staleness(pids[0], {}, root, what=sib.title)


def _in_flight(sib: Sibling, base: str) -> Optional[int]:
    """Requests the sibling is serving right now; None when it cannot say."""
    if not sib.work_path:
        return 0
    report = _get_json(base + sib.work_path, probe_timeout_s())
    if report is None:
        return None
    return sum(int(r.get("inflight") or 0)
               for r in report.get("resident") or [] if isinstance(r, dict))


def _hand_over(sib: Sibling, base: str, stale, *, say: Callable[[str], None],
               probe: Callable[[str, float], bool], sleep: Callable[[float], None],
               clock: Callable[[], float]) -> str:
    """Stop the stale sibling once it is idle: "" when it is gone, else why
    it was kept."""
    from backend.core.daemon_handover import _silence_s
    from backend.core.process_identity import kill_process_tree

    deadline = clock() + _drain_s()
    busy = _in_flight(sib, base)
    while busy and clock() < deadline:
        sleep(1.0)
        busy = _in_flight(sib, base)
    if busy is None:
        return "it did not report its in-flight work"
    if busy:
        return f"it was still serving {busy} request(s) after {_drain_s():.0f}s"
    say(f"  {sib.title}: {stale.describe()} -- restarting it")
    grace = _silence_s()
    if not kill_process_tree(stale.pid, grace):
        return f"PID {stale.pid} could not be stopped"
    url = base + sib.probe_path
    deadline = clock() + grace
    while probe(url, probe_timeout_s()) and clock() < deadline:
        sleep(0.25)
    if probe(url, probe_timeout_s()):
        return f"{base} still answers after PID {stale.pid} stopped"
    return ""


def _replace_if_stale(sib: Sibling, base: str, *, say: Callable[[str], None],
                      probe: Callable[[str, float], bool], sleep: Callable[[float], None],
                      clock: Callable[[], float]) -> bool:
    """True when the answering sibling ran older code and is now stopped, so
    the caller starts it again from its checkout."""
    if stale_sibling_policy() != "replace" or not _env(sib.start_env):
        return False                 # nothing here could start it again
    try:
        stale = sibling_staleness(sib, base)
    except Exception as exc:  # noqa: BLE001 -- a judgment that fails adopts
        logger.debug("[TrinitySiblings] %s staleness unavailable: %s", sib.key, exc)
        return False
    if stale is None:
        return False
    kept = _hand_over(sib, base, stale, say=say, probe=probe, sleep=sleep, clock=clock)
    if kept:
        say(f"  {sib.title}: {stale.describe()}; kept running because {kept}")
        return False
    return True


#: One bring-up per sibling at a time: the early pre-warm and the Trinity
#: layer both ensure J-Prime, and the second must find the replacement the
#: first started -- not judge it, or stop it, mid-start.
_BRING_UP_LOCKS: Dict[str, threading.Lock] = {}
_BRING_UP_LOCKS_GUARD = threading.Lock()


def _bring_up_lock(key: str) -> threading.Lock:
    with _BRING_UP_LOCKS_GUARD:
        return _BRING_UP_LOCKS.setdefault(key, threading.Lock())


def ensure_siblings(*, say: Callable[[str], None] = print,
                    probe: Callable[[str, float], bool] = _answers,
                    spawn: Callable[[str, Dict[str, str]], object] = _spawn,
                    sleep: Callable[[float], None] = time.sleep,
                    clock: Callable[[], float] = time.monotonic,
                    lent: Callable[[Sibling, str], str] = _lent_detail) -> List[SiblingStatus]:
    """Bring up every configured sibling that is not already serving. NEVER raises."""
    if not autostart_enabled():
        return []
    return [
        _ensure_one(sib, say=say, probe=probe, spawn=spawn, sleep=sleep, clock=clock, lent=lent)
        for sib in siblings()
    ]


def ensure_sibling(key: str, *, say: Callable[[str], None] = print,
                   probe: Callable[[str, float], bool] = _answers,
                   spawn: Callable[[str, Dict[str, str]], object] = _spawn,
                   sleep: Callable[[float], None] = time.sleep,
                   clock: Callable[[], float] = time.monotonic,
                   lent: Callable[[Sibling, str], str] = _lent_detail) -> SiblingStatus:
    """Bring up ONE sibling by key ("jprime" | "reactor"). NEVER raises.

    The same contract as :func:`ensure_siblings`, for a caller that owns one
    sibling's lifecycle (the unified supervisor's Trinity layer), so both
    launchers honour one declaration of how a sibling starts.
    """
    for sib in siblings():
        if sib.key == key:
            if not autostart_enabled():
                return SiblingStatus(sib.key, sib.title, "", "skipped", "JARVIS_TRINITY_AUTOSTART off")
            return _ensure_one(sib, say=say, probe=probe, spawn=spawn, sleep=sleep,
                               clock=clock, lent=lent)
    return SiblingStatus(key, key, "", "not_configured", f"unknown sibling {key!r}")


def _ensure_one(sib: Sibling, *, say: Callable[[str], None],
                probe: Callable[[str, float], bool],
                spawn: Callable[[str, Dict[str, str]], object],
                sleep: Callable[[float], None],
                clock: Callable[[], float],
                lent: Callable[[Sibling, str], str]) -> SiblingStatus:
    with _bring_up_lock(sib.key):
        return _ensure_one_locked(sib, say=say, probe=probe, spawn=spawn, sleep=sleep,
                                  clock=clock, lent=lent)


def _ensure_one_locked(sib: Sibling, *, say: Callable[[str], None],
                       probe: Callable[[str, float], bool],
                       spawn: Callable[[str, Dict[str, str]], object],
                       sleep: Callable[[float], None],
                       clock: Callable[[], float],
                       lent: Callable[[Sibling, str], str]) -> SiblingStatus:
    probe_timeout = probe_timeout_s()
    try:
        base = sib.base_url().rstrip("/")
        url = base + sib.probe_path
        if sib.key == "jprime" and not sib.required():
            return SiblingStatus(sib.key, sib.title, base, "skipped", "local lane off")
        if probe(url, probe_timeout):
            why = lent(sib, base)
            if why:
                # Up, but lent out: not started over (it is not down),
                # not called "serving" (it will refuse us). The lane
                # gate owns what that means for this boot.
                say(f"  {sib.title}: up at {base}, lent -- {why}")
                return SiblingStatus(sib.key, sib.title, base, "lent", why)
            if not _replace_if_stale(sib, base, say=say, probe=probe, sleep=sleep, clock=clock):
                say(f"  {sib.title}: serving at {base}")
                return SiblingStatus(sib.key, sib.title, base, "serving")
        cmd = _env(sib.start_env)
        if not cmd:
            detail = f"not serving at {base} and {sib.start_env} is unset"
            say(f"  {sib.title}: {detail}" + (" (required)" if sib.required() else ""))
            return SiblingStatus(sib.key, sib.title, base, "not_configured", detail)
        say(f"  {sib.title}: not serving at {base} -- starting it")
        spawn(cmd, _template_values(base))
        deadline = clock() + sib.ready_budget_s()
        while clock() < deadline:
            if probe(url, probe_timeout):
                break
            sleep(1.0)
        if probe(url, probe_timeout):
            say(f"  {sib.title}: started, serving at {base}")
            return SiblingStatus(sib.key, sib.title, base, "started")
        detail = f"did not answer at {url} within {sib.ready_budget_s():.0f}s; see {_launch_log()}"
        say(f"  {sib.title}: {detail}")
        return SiblingStatus(sib.key, sib.title, base, "failed", detail)
    except Exception as exc:  # noqa: BLE001 -- bring-up must never take the boot down
        logger.warning("[TrinitySiblings] %s bring-up error: %s", sib.key, exc, exc_info=True)
        return SiblingStatus(sib.key, sib.title, "", "failed", str(exc))
