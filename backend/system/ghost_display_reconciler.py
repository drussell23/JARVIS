"""How many ghost displays are there, really — and converge to the right number.

Measured on this machine before this module existed: **nine** virtual screens
all named "JARVIS GHOST", tagIDs 153 through 201, none of them connected. The
menu bar showed a wall of identical toggles. History says it has been worse:
`GHOST_DISPLAY_SYSTEM.md` records "~150 orphaned definitions accumulated".

THE PROBE WAS BLIND TO THE THING IT GUARDED
---------------------------------------------
`ensure_ghost_display_exists_async` asked "does one already exist?" by running
`system_profiler SPDisplaysDataType` and searching for the name. Measured: with
nine ghosts defined, that command reports exactly one display — "Color LCD".
`system_profiler` does not see BetterDisplay virtual screens **at all** until
they are attached to the GPU framebuffer, and often not even then.

So the check returned "absent" every single time, and STEP 4 created another
one. Every boot. Every health recovery. Every command-triggered call.

WHY THE OBVIOUS FIX IS STILL WRONG
------------------------------------
"Use the CLI instead of system_profiler" (the unchecked Phase 1 item in the
design doc) is necessary and not sufficient, because it keeps the same fatal
inference: *no evidence of a display* → *no display* → *create one*.

The CLI cannot answer when BetterDisplay.app is not running, when CLI
integration is switched off in its settings, or when the request times out — and
in all three cases the old code read the silence as ABSENT and created. So the
rule this module is built around is:

    **UNKNOWN NEVER AUTHORISES A CREATE.**

Only a CERTAIN absence does. That single inversion makes the accumulation class
impossible rather than merely less likely, and it is the same rule the rest of
this codebase already lives by — `Readiness.UNHYDRATED` is not `EMPTY`,
`unverified` is not `safe`, silence in the capability registry means gated.

DEFINED AND CONNECTED ARE DIFFERENT FACTS
-------------------------------------------
This is the loop that produced the nine. `DisplayPressureController` deliberately
DISCONNECTS the ghost display under memory pressure, and graceful shutdown is
meant to as well. A probe that only detects CONNECTED displays therefore reports
"absent" for a display the system itself just detached — and the next `ensure`
creates a replacement, forever.

So a defined-but-disconnected ghost is RECONNECTED, never re-created. `defined`
and `connected` are tracked as separate counts and neither is inferred from the
other.

CONVERGE, DON'T JUST ENSURE
-----------------------------
"Ensure at least one exists" cannot subtract. `reconcile()` targets an exact
count: it discards surplus by `tagID`, reconnects what is defined but detached,
and creates only when it is certain nothing is there. Same doctrine as
`WorktreeManager.reap_orphans()`, which already sweeps this repo's leftovers on
boot for exactly the same reason.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import asyncio
import enum
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("JARVIS.GhostDisplayReconciler")

GHOST_RECONCILER_SCHEMA_VERSION: str = "ghost_display_reconciler.v1"

#: Runs one BetterDisplay CLI invocation. Injected rather than built here: the
#: manager already owns multi-path discovery, app auto-launch and path caching,
#: and a second copy of that logic is exactly the duplication this module is
#: supposed to avoid. Returns (returncode, combined_output).
CliRunner = Callable[..., Awaitable[Tuple[int, str]]]


def reconciler_enabled() -> bool:
    """Master gate. Default TRUE. NEVER raises.

    Off means no probe and no convergence — creation falls back to whatever the
    manager did before. It does NOT mean "create freely": the manager still
    refuses to create on an UNKNOWN answer, because that guard is the fix.
    """
    return (os.environ.get("JARVIS_GHOST_RECONCILE_ENABLED", "true")
            or "").strip().lower() not in ("0", "false", "no", "off")


def target_ghost_count() -> int:
    """How many ghost displays there should be. Clamped. NEVER raises.

    A number rather than a boolean because "exactly one" is a policy, not a law
    of nature — a future mosaic-capture mode may want two. Clamped to a small
    ceiling so a mis-set env var cannot ask for a hundred.
    """
    try:
        return max(0, min(int(os.environ.get("JARVIS_GHOST_TARGET_COUNT", "1")), 4))
    except (TypeError, ValueError):
        return 1


def discard_surplus_enabled() -> bool:
    """Whether convergence may DELETE. Default TRUE. NEVER raises.

    Separable from the master switch because discarding is the only
    irreversible thing here — BetterDisplay's own help says discard has no undo.
    An operator who wants the count REPORTED but not acted on has a way to say
    so without giving up the probe that stops the leak.
    """
    return (os.environ.get("JARVIS_GHOST_DISCARD_SURPLUS", "true")
            or "").strip().lower() not in ("0", "false", "no", "off")


def max_discards_per_sweep() -> int:
    """Ceiling on deletions in one pass. NEVER raises.

    A blast-radius bound. If the probe is ever wrong about what belongs to
    JARVIS, this is the difference between losing a few virtual screens and
    losing all of them — and whatever it declines to remove is LOGGED rather
    than silently left, so a truncated sweep never reads as a finished one.
    """
    try:
        return max(1, min(int(os.environ.get("JARVIS_GHOST_MAX_DISCARDS", "16")), 256))
    except (TypeError, ValueError):
        return 16


def cli_timeout_s() -> float:
    """Budget for one CLI call. NEVER raises."""
    try:
        return max(2.0, min(float(os.environ.get(
            "JARVIS_GHOST_CLI_TIMEOUT_S", "8")), 60.0))
    except (TypeError, ValueError):
        return 8.0


class Presence(str, enum.Enum):
    """Whether a ghost display exists — with UNKNOWN kept distinct from ABSENT.

    The whole module turns on this enum having three members instead of two.
    A boolean forces "we could not tell" to be spelled `False`, and `False` is
    what authorises a create.
    """

    PRESENT = "present"
    ABSENT = "absent"        # measured by a source that CAN see virtual screens
    UNKNOWN = "unknown"      # nothing authoritative could answer — do NOT create


@dataclass
class GhostDisplay:
    """One virtual screen definition, as BetterDisplay reports it."""

    tag_id: str
    uuid: str = ""
    display_id: str = ""
    name: str = ""
    device_type: str = "VirtualScreen"
    connected: Optional[bool] = None      # None = not determined

    @property
    def sort_key(self) -> int:
        """Ascending tagID — BetterDisplay issues them monotonically, so the
        lowest is the OLDEST. Used to decide which ghost survives convergence.
        """
        try:
            return int(self.tag_id)
        except (TypeError, ValueError):
            return 1 << 30

    def to_dict(self) -> Dict[str, Any]:
        return {"tag_id": self.tag_id, "uuid": self.uuid,
                "display_id": self.display_id, "name": self.name,
                "connected": self.connected}


@dataclass
class GhostInventory:
    """What is actually out there. Counts DEFINED and CONNECTED separately."""

    presence: str = Presence.UNKNOWN.value
    displays: List[GhostDisplay] = field(default_factory=list)
    #: How many displays macOS itself has online. From CoreGraphics, which sees
    #: attached virtual screens; None when Quartz is unavailable, because
    #: "no data" and "zero" are different and only one of them is a fact.
    online_displays: Optional[int] = None
    #: macOS displayIDs that are ATTACHED but that BetterDisplay does not
    #: account for. See :meth:`orphans` — this is a state that was completely
    #: invisible to every instrument before it was measured.
    orphan_display_ids: List[int] = field(default_factory=list)
    source: str = ""
    detail: str = ""

    @property
    def defined(self) -> int:
        return len(self.displays)

    @property
    def connected(self) -> int:
        return sum(1 for d in self.displays if d.connected)

    @property
    def certain(self) -> bool:
        """Whether this inventory may be acted on destructively OR creatively."""
        return self.presence != Presence.UNKNOWN.value

    @property
    def orphans(self) -> int:
        """Attached framebuffers nobody owns.

        A real state, measured: discarding eight CONNECTED virtual screens left
        BetterDisplay reporting ONE virtual screen while macOS still had TEN
        displays attached. The definitions were gone, the framebuffers were not,
        and nothing could address them because the app that made them had
        forgotten they existed.

        Detected by SET DIFFERENCE on displayIDs rather than by comparing
        counts, so a genuine external monitor — which BetterDisplay does know
        about — is never mistaken for an orphan.

        Nothing here tries to remove them. There is no API to; quitting
        BetterDisplay releases them, and that is an operator's call, not a
        reconciler's.
        """
        return len(self.orphan_display_ids)

    @property
    def surplus(self) -> int:
        return max(0, self.defined - target_ghost_count())

    def ranked(self) -> List[GhostDisplay]:
        """Every ghost, best-to-keep first. NEVER raises.

        CONNECTED outranks detached — a connected ghost is the one macOS and
        yabai already know about, and killing it to keep an older definition
        would strand whatever windows live there. Within a rank, OLDEST first,
        because `GhostPersistenceManager` may hold window state referencing it.
        """
        return sorted(self.displays,
                      key=lambda d: (0 if d.connected else 1, d.sort_key))

    def survivors(self) -> List[GhostDisplay]:
        """The ghosts convergence keeps — as many as the target asks for.

        Returning a LIST rather than one display: `_discard_surplus` used to
        keep exactly `survivor()` and doom everything else, which silently
        ignored the target and removed 8 of 9 even when asked to keep 3. The
        target is a policy knob; a keeper that can only count to one is not
        honouring it.
        """
        return self.ranked()[:max(0, target_ghost_count())]

    def survivor(self) -> Optional[GhostDisplay]:
        """The single best ghost — the one to reconnect. NEVER raises."""
        ranked = self.ranked()
        return ranked[0] if ranked else None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": GHOST_RECONCILER_SCHEMA_VERSION,
            "presence": self.presence, "defined": self.defined,
            "connected": self.connected, "target": target_ghost_count(),
            "surplus": self.surplus, "online_displays": self.online_displays,
            "orphans": self.orphans,
            "orphan_display_ids": list(self.orphan_display_ids),
            "source": self.source, "detail": self.detail[:300],
            "displays": [d.to_dict() for d in self.displays],
        }


def name_variants(ghost_name: str) -> Tuple[str, ...]:
    """The spellings one ghost display answers to. NEVER raises.

    `JARVIS_GHOST` is the configured name; BetterDisplay stores and displays it
    as `JARVIS GHOST`. Matching only the configured spelling is how a probe
    misses the very displays it created.
    """
    base = (ghost_name or "").strip().lower()
    return tuple({base, base.replace("_", " "), base.replace("_", ""),
                  base.replace(" ", "_")} - {""})


def _matches(name: str, variants: Tuple[str, ...]) -> bool:
    low = (name or "").strip().lower()
    return any(v and v in low for v in variants)


def parse_identifiers(raw: str, ghost_name: str) -> List[GhostDisplay]:
    """Parse ``get -identifiers`` output into OUR virtual screens. NEVER raises.

    BetterDisplay emits a comma-separated run of JSON objects with no enclosing
    brackets, so it is wrapped before parsing rather than pattern-scraped —
    scraping a JSON document with regexes is how a `tagID` ends up belonging to
    the wrong device, and a `tagID` is what `discard` acts on.

    Filters to `deviceType == VirtualScreen` AND a name match. Both, because
    discarding by name alone could reach a real monitor an operator happened to
    label similarly, and discarding every VirtualScreen would destroy ones
    JARVIS never made.
    """
    out: List[GhostDisplay] = []
    try:
        text = (raw or "").strip()
        if not text:
            return out
        if not text.startswith("["):
            text = f"[{text}]"
        objs = json.loads(text)
        if isinstance(objs, dict):
            objs = [objs]
        variants = name_variants(ghost_name)
        for o in objs:
            if not isinstance(o, dict):
                continue
            if str(o.get("deviceType") or "") != "VirtualScreen":
                continue
            name = str(o.get("name") or o.get("originalName") or "")
            if not _matches(name, variants):
                continue
            # Prefer the VirtualScreen-specific tag: an attached virtual screen
            # reports BOTH `tagID (VirtualScreen)` and `tagID (Display)`, and
            # discarding by the Display tag is addressing the wrong object.
            tag = str(o.get("tagID (VirtualScreen)") or o.get("tagID") or "")
            if not tag:
                continue
            out.append(GhostDisplay(
                tag_id=tag, uuid=str(o.get("UUID") or ""),
                display_id=str(o.get("displayID") or ""), name=name))
    except (ValueError, TypeError) as exc:
        logger.debug("[Ghost] identifier parse failed: %s", exc)
    except Exception:  # noqa: BLE001
        logger.debug("[Ghost] identifier parse degraded", exc_info=True)
    return out


def parse_known_display_ids(raw: str) -> Optional[set]:
    """Every macOS displayID BetterDisplay accounts for. None if unparseable.

    ALL devices, not just ours — a real external monitor is accounted for too,
    which is what keeps :meth:`GhostInventory.orphans` from calling somebody's
    actual second screen an orphan.
    """
    try:
        text = (raw or "").strip()
        if not text:
            return None
        if not text.startswith("["):
            text = f"[{text}]"
        objs = json.loads(text)
        if isinstance(objs, dict):
            objs = [objs]
        out = set()
        for o in objs:
            if not isinstance(o, dict):
                continue
            did = str(o.get("displayID") or "").strip()
            if did.isdigit():
                out.add(int(did))
        return out
    except Exception:  # noqa: BLE001
        return None


def online_display_ids() -> Optional[set]:
    """macOS displayIDs currently attached. None if CoreGraphics cannot answer.

    The set rather than the count, because the useful question is not "how
    many" but "which ones does BetterDisplay not know about".
    """
    try:
        import Quartz  # type: ignore[import-not-found]
        err, ids, count = Quartz.CGGetOnlineDisplayList(  # type: ignore[attr-defined]
            32, None, None)
        if err != 0 or ids is None:
            return None
        return {int(d) for d in list(ids)[:int(count)]}
    except Exception:  # noqa: BLE001
        return None


def online_display_count() -> Optional[int]:
    """How many displays macOS has ONLINE. None if it cannot be asked.

    CoreGraphics rather than `system_profiler`: measured on this machine with
    nine ghosts defined, `CGGetOnlineDisplayList` correctly reported 1 while
    `system_profiler` could not name a single virtual screen. Returning None
    when Quartz is missing keeps "unavailable" from being read as "zero" — the
    conflation this whole module exists to remove.
    """
    try:
        import Quartz  # type: ignore[import-not-found]
        # 32 is a max-count ceiling, not a promise; the third return value is
        # how many were actually filled in.
        err, _ids, count = Quartz.CGGetOnlineDisplayList(  # type: ignore[attr-defined]
            32, None, None)
        if err != 0:
            return None
        return int(count)
    except Exception:  # noqa: BLE001
        return None


class GhostDisplayReconciler:
    """Measures ghost displays and converges them to a target. NEVER raises."""

    def __init__(self, ghost_name: str, run_cli: Optional[CliRunner] = None) -> None:
        self.ghost_name = ghost_name or "JARVIS_GHOST"
        self._run_cli = run_cli
        self._lock = asyncio.Lock()
        self._stats: Dict[str, int] = {
            "probes": 0, "probe_unknown": 0, "discarded": 0, "detached": 0,
            "reconnected": 0, "created": 0, "refused_create_unknown": 0,
        }

    def set_cli_runner(self, run_cli: Optional[CliRunner]) -> None:
        """Install what actually invokes the CLI. NEVER raises."""
        self._run_cli = run_cli

    # -- measurement -------------------------------------------------------

    async def probe(self) -> GhostInventory:
        """Ask what exists. NEVER raises, NEVER creates, NEVER deletes.

        Authority order matters and is not negotiable:

        1. **BetterDisplay CLI `get -identifiers`** — the ONLY source that can
           see a virtual screen that is defined but not attached, which is the
           state the pressure controller deliberately puts them in.
        2. **CoreGraphics** — corroborates how many displays are actually
           online. Cannot enumerate definitions, so it can never prove absence.
        3. `system_profiler` is not consulted at all. It was measured blind to
           all nine ghosts; a source that cannot see the thing has no vote.
        """
        inv = GhostInventory()
        inv.online_displays = online_display_count()
        try:
            self._stats["probes"] += 1
            if not reconciler_enabled():
                inv.presence = Presence.UNKNOWN.value
                inv.source = "disabled"
                inv.detail = "JARVIS_GHOST_RECONCILE_ENABLED is off"
                self._stats["probe_unknown"] += 1
                return inv
            if self._run_cli is None:
                inv.presence = Presence.UNKNOWN.value
                inv.source = "no-cli"
                inv.detail = ("no CLI runner installed — cannot enumerate "
                              "virtual screens")
                self._stats["probe_unknown"] += 1
                return inv

            rc, out = await self._cli("get", "-identifiers")
            if rc != 0 or _cli_failed(out):
                # The app is down, integration is off, or the request timed
                # out. Every one of those is UNKNOWN. Reading them as ABSENT is
                # what produced nine displays.
                inv.presence = Presence.UNKNOWN.value
                inv.source = "cli"
                inv.detail = f"CLI could not answer: {out.strip()[:200] or f'rc={rc}'}"
                self._stats["probe_unknown"] += 1
                logger.warning("[Ghost] inventory UNKNOWN — %s. Creation is "
                               "refused while the count cannot be measured.",
                               inv.detail)
                return inv

            inv.displays = parse_identifiers(out, self.ghost_name)
            inv.source = "cli:get -identifiers"
            inv.presence = (Presence.PRESENT.value if inv.displays
                            else Presence.ABSENT.value)
            await self._mark_connected(inv)
            self._detect_orphans(inv, out)
            logger.info("[Ghost] inventory: %d defined, %d connected, "
                        "target %d (%d online displays)",
                        inv.defined, inv.connected, target_ghost_count(),
                        inv.online_displays if inv.online_displays is not None else -1)
            if inv.orphans:
                logger.error(
                    "[Ghost] %d ORPHANED framebuffer(s) attached that "
                    "BetterDisplay does not own (displayIDs %s). Nothing can "
                    "address these; quitting BetterDisplay releases them.",
                    inv.orphans,
                    ", ".join(str(i) for i in inv.orphan_display_ids))
            return inv
        except Exception as exc:  # noqa: BLE001
            inv.presence = Presence.UNKNOWN.value
            inv.source = "error"
            inv.detail = f"{type(exc).__name__}: {exc}"
            self._stats["probe_unknown"] += 1
            logger.debug("[Ghost] probe degraded", exc_info=True)
            return inv

    def _detect_orphans(self, inv: GhostInventory, raw: str) -> None:
        """Attached displays BetterDisplay does not know about. NEVER raises.

        Both sides must be readable for the difference to mean anything. If
        either CoreGraphics or the identifier list cannot be parsed, the orphan
        list stays EMPTY rather than becoming the whole online set — reporting
        every display on the machine as an orphan because one probe failed
        would be a worse lie than the state it is trying to catch.
        """
        try:
            online = online_display_ids()
            known = parse_known_display_ids(raw)
            if online is None or known is None:
                return
            inv.orphan_display_ids = sorted(online - known)
        except Exception:  # noqa: BLE001
            logger.debug("[Ghost] orphan detection degraded", exc_info=True)

    async def _mark_connected(self, inv: GhostInventory) -> None:
        """Fill in per-display connected state. Best effort. NEVER raises.

        A failure here leaves `connected` as None rather than False. None means
        "not determined"; False would claim a measurement nobody made, and
        `survivor()` uses this to decide which ghost lives.
        """
        for d in inv.displays:
            try:
                rc, out = await self._cli("get", f"-tagID={d.tag_id}", "-connected")
                if rc == 0 and not _cli_failed(out):
                    d.connected = out.strip().lower().startswith("on")
            except Exception:  # noqa: BLE001
                continue

    # -- convergence -------------------------------------------------------

    async def reconcile(self) -> Dict[str, Any]:
        """Converge to exactly `target_ghost_count()`. NEVER raises.

        Returns a report of what it did. Serialised by a lock so a boot sweep
        and a health-recovery call cannot both decide to create the display that
        the other one is already creating — the race that turns one missing
        ghost into two.
        """
        async with self._lock:
            return await self._reconcile_impl()

    async def _reconcile_impl(self) -> Dict[str, Any]:
        report: Dict[str, Any] = {
            "schema_version": GHOST_RECONCILER_SCHEMA_VERSION,
            "acted": False, "discarded": [], "reconnected": [],
            "create_needed": False, "refused": "",
        }
        try:
            inv = await self.probe()
            report["inventory"] = inv.to_dict()

            if not inv.certain:
                # The one branch that matters. Everything downstream — discard,
                # reconnect, create — is refused on an unmeasured count.
                report["refused"] = (
                    "inventory is UNKNOWN; refusing to create or discard. "
                    + inv.detail)
                self._stats["refused_create_unknown"] += 1
                return report

            target = target_ghost_count()

            # 1. SUBTRACT. The step "ensure exists" structurally cannot do.
            if inv.surplus > 0:
                report["discarded"] = await self._discard_surplus(inv)
                report["acted"] = bool(report["discarded"])

            # 2. RECONNECT rather than create. A defined-but-detached ghost is
            #    the normal resting state after the pressure controller sheds
            #    it, and creating a replacement is how nine of them happened.
            survivor = inv.survivor()
            if survivor is not None and not survivor.connected:
                if await self._reconnect(survivor):
                    report["reconnected"] = [survivor.tag_id]
                    report["acted"] = True

            # 3. CREATE only against a measured zero. Reported rather than
            #    performed: creation stays the manager's job, since it owns the
            #    aspect/resolution policy and the registration wait.
            if inv.defined < target:
                report["create_needed"] = True
                report["create_count"] = target - inv.defined

            return report
        except Exception as exc:  # noqa: BLE001
            report["refused"] = f"{type(exc).__name__}: {exc}"
            logger.debug("[Ghost] reconcile degraded", exc_info=True)
            return report

    async def _discard_surplus(self, inv: GhostInventory) -> List[str]:
        """Delete the extras, newest first, BY TAG ID. NEVER raises.

        Newest-first so the survivor is the settled one. Bounded by
        `max_discards_per_sweep`, and whatever it declines to remove is logged
        by count — a truncated sweep that reported success would read as "the
        problem is fixed" while the wall of toggles was still there.
        """
        killed: List[str] = []
        try:
            if not discard_surplus_enabled():
                logger.warning("[Ghost] %d surplus ghost display(s) found but "
                               "JARVIS_GHOST_DISCARD_SURPLUS is off — leaving "
                               "them in place", inv.surplus)
                return killed
            keep = {d.tag_id for d in inv.survivors()}
            doomed = [d for d in inv.displays if d.tag_id not in keep]
            doomed.sort(key=lambda d: d.sort_key, reverse=True)
            cap = max_discards_per_sweep()
            if len(doomed) > cap:
                logger.warning("[Ghost] %d surplus displays exceed the "
                               "per-sweep cap of %d — removing %d now, the "
                               "rest on the next sweep",
                               len(doomed), cap, cap)
                doomed = doomed[:cap]
            for d in doomed:
                if await self._discard(d):
                    killed.append(d.tag_id)
            if killed:
                logger.info("[Ghost] discarded %d surplus display(s) "
                            "(tagIDs %s); kept %s",
                            len(killed), ", ".join(killed),
                            ", ".join(sorted(keep)) or "-")
        except Exception:  # noqa: BLE001
            logger.debug("[Ghost] discard sweep degraded", exc_info=True)
        return killed

    async def _discard(self, display: GhostDisplay) -> bool:
        """Detach, THEN discard, one display by tagID. NEVER raises.

        DISCONNECT FIRST — this order is load-bearing and was learned the
        expensive way. Discarding eight CONNECTED virtual screens removed their
        BetterDisplay definitions while macOS kept all eight framebuffers
        attached: `get -identifiers` reported one virtual screen and
        `CGGetOnlineDisplayList` reported ten displays, and because BetterDisplay
        no longer knew about them, nothing could address them to clean them up.
        They survived a 20-second settle and only died when BetterDisplay itself
        was quit. Discarding a connected screen does not detach it; it just
        forgets who owns it.

        The detach is best-effort rather than a preconditon: a screen that is
        already detached returns a refusal here, and refusing to discard it on
        that basis would leave the surplus in place forever.

        The `-tagID=` argument is not optional and is asserted before the call.
        BetterDisplay's own help: "without identifiers, the app will discard all
        discardable devices without question and any ability to undo". An empty
        tag would silently become that command.
        """
        try:
            tag = (display.tag_id or "").strip()
            if not tag:
                logger.error("[Ghost] refusing to discard with an empty tagID "
                             "— an unidentified discard removes EVERY virtual "
                             "screen on the machine")
                return False

            if display.connected is not False:
                rc, out = await self._cli(
                    "set", f"-tagID={tag}", "-connected=off")
                if rc == 0 and not _cli_failed(out):
                    display.connected = False
                    self._stats["detached"] += 1
                else:
                    # Proceed anyway, but SAY so: this is the exact condition
                    # that produces an orphaned framebuffer, and an operator
                    # seeing a stray display later needs this line to exist.
                    logger.warning(
                        "[Ghost] could not detach tagID %s before discarding "
                        "(%s) — if a stray framebuffer remains, quitting "
                        "BetterDisplay releases it", tag, out.strip()[:120])

            rc, out = await self._cli("discard", f"-tagID={tag}")
            if rc == 0 and not _cli_failed(out):
                self._stats["discarded"] += 1
                return True
            logger.warning("[Ghost] discard of tagID %s failed: %s",
                           tag, out.strip()[:160])
            return False
        except Exception:  # noqa: BLE001
            return False

    async def _reconnect(self, display: GhostDisplay) -> bool:
        """Attach a defined-but-detached ghost. NEVER raises."""
        try:
            rc, out = await self._cli(
                "set", f"-tagID={display.tag_id}", "-connected=on")
            ok = rc == 0 and not _cli_failed(out)
            if ok:
                self._stats["reconnected"] += 1
                display.connected = True
                logger.info("[Ghost] reconnected existing display tagID %s "
                            "instead of creating a new one", display.tag_id)
            return ok
        except Exception:  # noqa: BLE001
            return False

    # -- plumbing ----------------------------------------------------------

    async def _cli(self, *args: str) -> Tuple[int, str]:
        """One bounded CLI call. NEVER raises."""
        if self._run_cli is None:
            return (1, "no CLI runner")
        try:
            return await asyncio.wait_for(self._run_cli(*args),
                                          timeout=cli_timeout_s())
        except asyncio.TimeoutError:
            return (1, f"timed out after {cli_timeout_s():.0f}s")
        except Exception as exc:  # noqa: BLE001
            return (1, f"{type(exc).__name__}: {exc}")

    def stats(self) -> Dict[str, Any]:
        return {"schema_version": GHOST_RECONCILER_SCHEMA_VERSION,
                "enabled": reconciler_enabled(),
                "ghost_name": self.ghost_name,
                "target": target_ghost_count(),
                "discard_surplus": discard_surplus_enabled(),
                **self._stats}


#: BetterDisplay reports refusal in prose on stdout with a ZERO exit code —
#: "Failed. Request timed out. Host app might not be running..." — so the return
#: code alone cannot be trusted to mean success. Checked in one place because
#: every call site would otherwise have to remember it.
_FAILURE_MARKERS = ("failed.", "failed:", "request timed out",
                    "host app might not be running", "not accepting")


def _cli_failed(output: str) -> bool:
    """Whether CLI prose says the request did not happen. NEVER raises."""
    low = (output or "").strip().lower()
    if not low:
        return False
    return any(m in low for m in _FAILURE_MARKERS)
