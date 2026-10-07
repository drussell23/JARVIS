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
* A sibling that already answers is left alone -- the operator (or another
  session) owns it. Nothing here ever stops a sibling.
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

import logging
import os
import shlex
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger("Ouroboros.TrinitySiblings")

__all__ = ["Sibling", "SiblingStatus", "siblings", "ensure_siblings", "autostart_enabled",
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
        ),
        Sibling(
            key="reactor", title="Reactor-Core (Nerves)",
            base_url=lambda: _env("REACTOR_CORE_API_URL", "http://127.0.0.1:8090"),
            probe_path="/health", start_env="JARVIS_REACTOR_START_CMD",
            required=lambda: False,
            ready_budget_s=lambda: _float_env("JARVIS_REACTOR_START_BUDGET_S", 120.0),
        ),
    ]


def _answers(url: str, timeout: float) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return 200 <= getattr(r, "status", 200) < 300
    except Exception:  # noqa: BLE001 -- unreachable/refused/timeout all mean "no"
        return False


def _template_values(base_url: str) -> Dict[str, str]:
    port = urlparse(base_url).port
    return {
        "model": _env("JARVIS_LOCAL_MODEL_NAME"),
        "port": str(port or ""),
        "ctx": _env("JARVIS_LOCAL_NUM_CTX") or _env("JARVIS_NUM_CTX_CEILING") or "32768",
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


def ensure_siblings(*, say: Callable[[str], None] = print,
                    probe: Callable[[str, float], bool] = _answers,
                    spawn: Callable[[str, Dict[str, str]], object] = _spawn,
                    sleep: Callable[[float], None] = time.sleep,
                    clock: Callable[[], float] = time.monotonic,
                    lent: Callable[[Sibling, str], str] = _lent_detail) -> List[SiblingStatus]:
    """Bring up every configured sibling that is not already serving. NEVER raises."""
    out: List[SiblingStatus] = []
    if not autostart_enabled():
        return out
    probe_timeout = probe_timeout_s()
    for sib in siblings():
        try:
            base = sib.base_url().rstrip("/")
            url = base + sib.probe_path
            if sib.key == "jprime" and not sib.required():
                out.append(SiblingStatus(sib.key, sib.title, base, "skipped", "local lane off"))
                continue
            if probe(url, probe_timeout):
                why = lent(sib, base)
                if why:
                    # Up, but lent out: not started over (it is not down),
                    # not called "serving" (it will refuse us). The lane
                    # gate owns what that means for this boot.
                    say(f"  {sib.title}: up at {base}, lent -- {why}")
                    out.append(SiblingStatus(sib.key, sib.title, base, "lent", why))
                    continue
                say(f"  {sib.title}: serving at {base}")
                out.append(SiblingStatus(sib.key, sib.title, base, "serving"))
                continue
            cmd = _env(sib.start_env)
            if not cmd:
                detail = f"not serving at {base} and {sib.start_env} is unset"
                say(f"  {sib.title}: {detail}" + (" (required)" if sib.required() else ""))
                out.append(SiblingStatus(sib.key, sib.title, base, "not_configured", detail))
                continue
            say(f"  {sib.title}: not serving at {base} -- starting it")
            spawn(cmd, _template_values(base))
            deadline = clock() + sib.ready_budget_s()
            while clock() < deadline:
                if probe(url, probe_timeout):
                    break
                sleep(1.0)
            if probe(url, probe_timeout):
                say(f"  {sib.title}: started, serving at {base}")
                out.append(SiblingStatus(sib.key, sib.title, base, "started"))
            else:
                detail = f"did not answer at {url} within {sib.ready_budget_s():.0f}s; see {_launch_log()}"
                say(f"  {sib.title}: {detail}")
                out.append(SiblingStatus(sib.key, sib.title, base, "failed", detail))
        except Exception as exc:  # noqa: BLE001 -- bring-up must never take the boot down
            logger.warning("[TrinitySiblings] %s bring-up error: %s", sib.key, exc, exc_info=True)
            out.append(SiblingStatus(sib.key, sib.title, "", "failed", str(exc)))
    return out
