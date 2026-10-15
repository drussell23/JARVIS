"""
flight_check - the pre-flight checklist JARVIS runs before it says it is ready.

A pilot does not leave the gate because the engines started; each item that
the flight depends on is checked and called out. JARVIS declared itself
ready when its backend answered ``/health`` and its loading page was up --
nothing the user relies on (an answer from its model, its voice, their mail
and calendar, the abilities it can use on this machine) was exercised, and
the loading page said "JARVIS is online!" whatever the boot found.

Each item here EXERCISES its capability -- a real generation, a real voice
render, a real read of Gmail and Calendar -- concurrently, each within its
own time limit; an item that raises or does not answer is a failure, never
a pass. The report says what passed, what is limited and why, and what to do
about it, and every surface reads the same report: the terminal checklist,
the spoken completion line, the loading page, and the readiness tier (a
REQUIRED item that fails holds the tier below fully ready).

Items are registered (``register``); the kernel adds its own measurements
(the services it supervises) to a run as extra items.

Settings (environment), read on every run:
  JARVIS_FLIGHT_CHECK_ENABLED     -- run it at all (default on).
  JARVIS_FLIGHT_CHECK_TIMEOUT_S   -- each item's time limit (default 30 s: a
                                     cold local model may still be loading).
  JARVIS_FLIGHT_CHECK_REQUIRED    -- comma-separated item keys that hold
                                     readiness, replacing the items' own
                                     declarations.
  JARVIS_FLIGHT_CHECK_SKIP        -- comma-separated item keys not to run.
"""
from __future__ import annotations

import asyncio
import enum
import logging
import os
import time
from dataclasses import dataclass, replace
from typing import Awaitable, Callable, Dict, Iterable, List, Optional, Tuple

from backend.core.async_offload import import_off_loop

logger = logging.getLogger(__name__)


class Status(str, enum.Enum):
    GO = "go"              # works
    LIMITED = "limited"    # works in part, or not set up here (with a remedy)
    NO_GO = "no_go"        # does not work
    SKIPPED = "skipped"    # not run (the operator's setting)


@dataclass(frozen=True)
class Outcome:
    """What one probe found."""
    status: Status
    detail: str                 # what was found, for the terminal
    remedy: str = ""            # what would fix it, when something would


Probe = Callable[[float], Awaitable[Outcome]]   # (time limit) -> outcome


@dataclass(frozen=True)
class Item:
    key: str            # stable id ("mind"), what settings name
    title: str          # the checklist line ("Answering")
    spoken: str         # how JARVIS names it aloud ("my language model")
    probe: Probe
    required: bool = False
    plural: bool = False  # "some of my abilities ARE ..."


@dataclass(frozen=True)
class Result:
    item: Item
    outcome: Outcome
    elapsed_s: float

    @property
    def ok(self) -> bool:
        return self.outcome.status in (Status.GO, Status.SKIPPED)


@dataclass(frozen=True)
class Report:
    results: Tuple[Result, ...]
    elapsed_s: float

    @property
    def verdict(self) -> str:
        """"ready" (every item go), "limited" (an optional item is not),
        "not_ready" (a required item is not)."""
        if any(r.item.required and not r.ok for r in self.results):
            return "not_ready"
        if any(not r.ok for r in self.results):
            return "limited"
        return "ready"

    @property
    def blocking(self) -> List[str]:
        """Keys of the required items that did not pass."""
        return [r.item.key for r in self.results if r.item.required and not r.ok]

    @property
    def degraded(self) -> List[str]:
        """Keys of the optional items that did not pass."""
        return [r.item.key for r in self.results if not r.item.required and not r.ok]

    def issues_spoken(self) -> List[str]:
        """What is wrong, phrased to be said: "Google needs attention",
        "my language model is not working"."""
        phrases = []
        for r in self.results:
            if r.ok:
                continue
            if r.outcome.status is Status.LIMITED:
                phrases.append(f"{r.item.spoken} {'need' if r.item.plural else 'needs'} attention")
            else:
                phrases.append(f"{r.item.spoken} {'are' if r.item.plural else 'is'} not working")
        return phrases

    def summary(self) -> str:
        """One line for a status message (the loading page)."""
        passed = sum(1 for r in self.results if r.outcome.status is Status.GO)
        counted = sum(1 for r in self.results if r.outcome.status is not Status.SKIPPED)
        problems = [r.item.title for r in self.results if not r.ok]
        head = f"{passed} of {counted} checks passed"
        return f"{head}; not ready: {', '.join(problems)}" if problems else head

    def as_dict(self) -> Dict[str, object]:
        return {
            "verdict": self.verdict,
            "elapsed_s": round(self.elapsed_s, 2),
            "items": [{
                "key": r.item.key, "title": r.item.title, "required": r.item.required,
                "status": r.outcome.status.value, "detail": r.outcome.detail,
                "remedy": r.outcome.remedy, "elapsed_s": round(r.elapsed_s, 2),
            } for r in self.results],
        }


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def enabled() -> bool:
    return os.getenv("JARVIS_FLIGHT_CHECK_ENABLED", "true").strip().lower() not in (
        "0", "false", "no", "off")


def _keys(name: str) -> Optional[frozenset]:
    raw = os.getenv(name)
    if raw is None:
        return None
    return frozenset(k.strip().lower() for k in raw.split(",") if k.strip())


def item_timeout_s() -> float:
    raw = os.getenv("JARVIS_FLIGHT_CHECK_TIMEOUT_S", "").strip()
    try:
        value = float(raw) if raw else 30.0
        if value > 0:
            return value
    except ValueError:
        pass
    logger.warning("[FlightCheck] invalid JARVIS_FLIGHT_CHECK_TIMEOUT_S=%r; using 30", raw)
    return 30.0


# ---------------------------------------------------------------------------
# Registry and run
# ---------------------------------------------------------------------------

_REGISTRY: Dict[str, Item] = {}


def register(item: Item) -> Item:
    """Add (or replace, by key) an item every run checks."""
    _REGISTRY[item.key] = item
    return item


def registered() -> List[Item]:
    _ensure_builtin()
    return list(_REGISTRY.values())


async def _run_one(item: Item, limit: float) -> Result:
    started = time.monotonic()
    try:
        outcome = await asyncio.wait_for(item.probe(limit), timeout=limit)
    except asyncio.TimeoutError:
        outcome = Outcome(Status.NO_GO, f"did not answer within {limit:g}s")
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 -- a probe that fails IS the finding
        outcome = Outcome(Status.NO_GO, f"{type(exc).__name__}: {exc}" if str(exc)
                          else type(exc).__name__)
    if not isinstance(outcome, Outcome):
        outcome = Outcome(Status.NO_GO, f"probe returned {type(outcome).__name__}, not an outcome")
    return Result(item, outcome, time.monotonic() - started)


async def run(extra: Iterable[Item] = ()) -> Report:
    """Run every registered item and *extra*, concurrently."""
    started = time.monotonic()
    items: Dict[str, Item] = {item.key: item for item in registered()}
    items.update({item.key: item for item in extra})
    required, skip = _keys("JARVIS_FLIGHT_CHECK_REQUIRED"), _keys("JARVIS_FLIGHT_CHECK_SKIP") or frozenset()
    if required is not None:
        items = {k: replace(i, required=k in required) for k, i in items.items()}
    limit = item_timeout_s()

    async def skipped(item: Item) -> Result:
        return Result(item, Outcome(Status.SKIPPED, "skipped by JARVIS_FLIGHT_CHECK_SKIP"), 0.0)

    results = await asyncio.gather(*(
        skipped(item) if key in skip else _run_one(item, limit) for key, item in items.items()))
    return Report(tuple(results), time.monotonic() - started)


def lines(report: Report) -> List[Tuple[str, str, str]]:
    """The checklist as (tone, text, suffix) for the terminal's narrative
    (backend.core.console_view): ✓ for go, ✗ otherwise, the remedy beneath."""
    out = []
    for r in report.results:
        tone = "done" if r.ok else "failed"
        tag = "" if r.item.required or r.ok else " (optional)"
        text = f"{r.item.title}{tag} -- {r.outcome.detail}"
        out.append((tone, text, f"{r.elapsed_s:.1f}s"))
        if r.outcome.remedy and not r.ok:
            out.append(("detail", r.outcome.remedy, ""))
    return out


# ---------------------------------------------------------------------------
# The built-in items: what the user relies on, exercised
# ---------------------------------------------------------------------------
# Each imports what it exercises off the event loop (import_off_loop): run at
# boot, the checklist shares the kernel's loop, and four cold imports on it at
# once starved the Mind's own status probe into reporting a live model down.

async def _probe_mind(limit: float) -> Outcome:
    """The Mind answers: one real generation through the router every
    command uses (prime_router), judged by require_generation -- an error
    or apology dressed as content is not an answer."""
    get_prime_router, require_generation = await import_off_loop(
        "backend.core.prime_router", "get_prime_router", "require_generation")
    router = await get_prime_router()
    response = await router.generate(  # the router's deadline is on time.monotonic()
        prompt="Reply with the single word: ready", max_tokens=8, temperature=0.0,
        deadline=time.monotonic() + limit)
    text = require_generation(response).strip()
    if not text:
        return Outcome(Status.NO_GO, "the model returned nothing")
    source = getattr(response, "source", "") or "model"
    latency = getattr(response, "latency_ms", None)
    timing = f" in {latency / 1000:.1f}s" if isinstance(latency, (int, float)) else ""
    return Outcome(Status.GO, f"{source} answered{timing}")


async def _probe_voice(limit: float) -> Outcome:
    """The voice renders: the persona's line from the engines that speak it
    (trinity_voice_coordinator.render_speech) -- produced, not played, so
    the check is silent."""
    voice_muted = await import_off_loop("backend.core.voice_mute", "voice_muted")
    if voice_muted():
        return Outcome(Status.LIMITED, "muted by the operator", "unmute to hear JARVIS")
    render_speech = await import_off_loop("backend.core.trinity_voice_coordinator", "render_speech")
    rendered = await render_speech("Ready.", timeout=limit)
    if rendered is None:
        return Outcome(Status.NO_GO, "no voice engine can render JARVIS's voice",
                       "install edge-tts, or a system voice matching the persona")
    return Outcome(Status.GO, f"{rendered.engine} renders the persona")


async def _probe_google(limit: float) -> Outcome:
    """Gmail and Calendar answer: a real read through each with the saved
    sign-in (verify_workspace, the same read the sign-in script makes)."""
    (GOOGLE_API_AVAILABLE, OAUTH_SETUP_COMMAND, GoogleWorkspaceClient, GoogleWorkspaceConfig,
     verify_workspace) = await import_off_loop(
        "backend.neural_mesh.agents.google_workspace_agent", "GOOGLE_API_AVAILABLE",
        "OAUTH_SETUP_COMMAND", "GoogleWorkspaceClient", "GoogleWorkspaceConfig", "verify_workspace")

    if not GOOGLE_API_AVAILABLE:
        return Outcome(Status.LIMITED, "Google API packages are not installed",
                       "install the packages in backend/requirements-windows.txt")
    client = GoogleWorkspaceClient(GoogleWorkspaceConfig())
    if not await client.authenticate(interactive=False):
        return Outcome(Status.LIMITED, f"not signed in ({client.auth_state.value})",
                       f"run: {OAUTH_SETUP_COMMAND}")
    services = await verify_workspace(client)
    failed = [f"{name}: {detail}" for name, ok, detail in services if not ok]
    if failed:
        return Outcome(Status.LIMITED, "; ".join(failed), f"run: {OAUTH_SETUP_COMMAND}")
    return Outcome(Status.GO, ", ".join(f"{name} {detail}" for name, _, detail in services))


async def _probe_abilities(limit: float) -> Outcome:
    """The abilities JARVIS can use on this machine (agent_bindings): one it
    can never run here (another platform) is not a fault; one that is not
    set up (a missing package) is, with its fix."""
    get_agent_bindings = await import_off_loop("backend.core.agent_bindings", "get_agent_bindings")
    bindings = await asyncio.to_thread(get_agent_bindings)
    usable, elsewhere, missing = [], [], []
    for name, binding in bindings.items():
        if binding.platform_reason():
            elsewhere.append(name)
        elif binding.missing_requirements():
            missing.append(f"{name} (needs {', '.join(binding.missing_requirements())})")
        else:
            usable.append(name)
    detail = f"{len(usable)} of {len(usable) + len(missing)} usable on this machine"
    if elsewhere:
        detail += f"; {len(elsewhere)} belong to other platforms"
    if missing:
        return Outcome(Status.LIMITED, f"{detail}; not set up: {', '.join(missing)}",
                       "install the packages named above")
    return Outcome(Status.GO, detail)


_builtin_registered = False


def _ensure_builtin() -> None:
    global _builtin_registered
    if _builtin_registered:
        return
    _builtin_registered = True
    for item in (
        Item("mind", "Answering", "my language model", _probe_mind, required=True),
        Item("voice", "Voice", "my voice", _probe_voice, required=True),
        Item("google", "Gmail and Calendar", "Google", _probe_google),
        Item("abilities", "Abilities", "some of my abilities", _probe_abilities, plural=True),
    ):
        _REGISTRY.setdefault(item.key, item)


def health_item(reading, *, required: bool) -> Item:
    """An item from an already-taken health reading (the kernel's services:
    elite_dashboard.probe_health) -- one measurement, every surface."""
    name = reading.display_name.split(" (")[0]
    status = Status.GO if reading.status == "ONLINE" else Status.NO_GO
    detail = reading.detail or reading.status.lower()

    async def probe(_limit: float) -> Outcome:
        return Outcome(status, detail)

    return Item(f"service:{reading.name}", name, name, probe, required=required)


__all__ = ["Item", "Outcome", "Report", "Result", "Status", "enabled", "health_item",
           "item_timeout_s", "lines", "register", "registered", "run"]
