"""What the machine looked like either side of a step that touched it.

`intent_journal` refuses to auto-replay an EFFECTFUL step whose outcome is
UNKNOWN — correct, and it leaves the operator holding a boolean decision with
no evidence. "Did the click land before it died?" is not answerable by staring
at the screen thirty seconds later, because whatever happened next has already
overwritten it.

So the executor keeps a black box: a small, bounded snapshot of the OS context
immediately before and after each world-touching step. When a step dies, the
last-known state and the delta up to it go into the journal, and the CONFIRM
prompt renders evidence rather than a question.

MEASURED, NOT ASSUMED
-----------------------
``NSWorkspace.frontmostApplication()`` costs **67 ms** on this machine, and
``CGWindowListCopyWindowInfo`` returns ``None`` in a process without a
screen-recording grant. Both facts shape the design:

* **Off the event loop, and bounded.** 67 ms twice per step is 1.3 s of
  overhead on a ten-step macro. Captured in a worker thread under a timeout,
  it overlaps the step's own latency and costs approximately nothing — and a
  hung accessibility call can never stall the automation it is observing.

* **The post-snapshot of step N is the pre-snapshot of step N+1.** Halves the
  captures, and is also more honest: it is literally the same instant.

* **Unavailable is not empty.** No screen-recording permission means the
  window title cannot be read; recording ``None`` there and ``""`` for a
  genuinely untitled window would make an absent capability look like an
  observation. `availability` says which happened, for the same reason
  `coordination_substrate` distinguishes `unverified` from `unsafe`.

REUSE
-------
No new macOS accessibility hooks. `AppKit.NSWorkspace` is the same API the
assistant surfaces already use, and window enumeration goes through
`vision.cg_window_capture`, which owns the CoreGraphics list, its filtering and
its cache. This module only decides WHEN to look and what to keep.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import asyncio
import enum
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger("JARVIS.BlackBox")

BLACK_BOX_SCHEMA_VERSION: str = "black_box.v1"


def enabled() -> bool:
    """Master gate. Default TRUE — bounded, read-only, off-thread. NEVER raises."""
    return (os.environ.get("JARVIS_BLACK_BOX_ENABLED", "true")
            or "").strip().lower() not in ("0", "false", "no", "off")


def capture_timeout_s() -> float:
    """Ceiling on one snapshot. Clamped: a black box that can stall the
    automation it observes is worse than no black box. NEVER raises."""
    try:
        v = float(os.environ.get("JARVIS_BLACK_BOX_TIMEOUT_S", "0.5"))
    except (TypeError, ValueError):
        v = 0.5
    return max(0.05, min(v, 5.0))


def title_capture_enabled() -> bool:
    """Window TITLES can contain document names, message text, URLs.

    Default TRUE because the forensic value is exactly in the title ("was the
    compose window still open?"), but an operator on a shared machine can drop
    it and keep the app name. NEVER raises."""
    return (os.environ.get("JARVIS_BLACK_BOX_TITLES", "true")
            or "").strip().lower() not in ("0", "false", "no", "off")


class Availability(str, enum.Enum):
    """Why a field is missing — never conflated with the field being empty."""

    OBSERVED = "observed"
    UNAVAILABLE = "unavailable"     # no permission / API returned nothing
    TIMED_OUT = "timed_out"
    DISABLED = "disabled"


@dataclass
class Snapshot:
    """One bounded look at the machine. Small enough to journal per step."""

    t: float = field(default_factory=time.time)
    app: Optional[str] = None
    app_pid: Optional[int] = None
    window_title: Optional[str] = None
    window_count: Optional[int] = None
    availability: str = Availability.OBSERVED.value
    detail: str = ""
    schema_version: str = BLACK_BOX_SCHEMA_VERSION

    @property
    def observed(self) -> bool:
        return self.availability == Availability.OBSERVED.value

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


#: One-shot latch for a window-list backend that cannot load. See the comment
#: at its assignment: a failed import is re-attempted on every call, which turns
#: a missing optional dependency into a per-step cost.
_window_list_unavailable: list = [""]


def _blocking_capture() -> Snapshot:
    """Read the OS context. Runs on a worker thread. NEVER raises."""
    snap = Snapshot()
    try:
        from AppKit import NSWorkspace
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            snap.availability = Availability.UNAVAILABLE.value
            snap.detail = "no frontmost application"
            return snap
        snap.app = str(app.localizedName() or "")
        try:
            snap.app_pid = int(app.processIdentifier())
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001 — headless, no AppKit, no GUI session
        snap.availability = Availability.UNAVAILABLE.value
        snap.detail = f"{type(exc).__name__}"
        return snap

    if not title_capture_enabled():
        snap.detail = "titles disabled"
        return snap

    # Window title + count via the module that already owns the CoreGraphics
    # list. Absent a screen-recording grant this returns nothing — which is
    # recorded as UNAVAILABLE for the title, NOT as an untitled window.
    if _window_list_unavailable[0]:
        snap.detail = snap.detail or _window_list_unavailable[0]
        return snap
    try:
        # `CGWindowCapture.get_all_windows` is a STATICMETHOD on the legacy
        # compatibility wrapper, not a module-level function — checked, because
        # importing the name that does not exist would have failed into the
        # `except` below and rendered every title "unavailable" while looking
        # like a permissions problem.
        from backend.vision.cg_window_capture import CGWindowCapture
        windows = CGWindowCapture.get_all_windows() or []
        snap.window_count = len(windows)
        for w in windows:
            owner = (w.get("owner") if isinstance(w, dict)
                     else getattr(w, "owner", "")) or ""
            if snap.app and owner == snap.app:
                name = (w.get("name") if isinstance(w, dict)
                        else getattr(w, "name", "")) or ""
                if name:
                    snap.window_title = str(name)
                    break
        if snap.window_title is None and snap.window_count == 0:
            snap.detail = (snap.detail or
                           "window list empty — screen-recording grant?")
    except Exception as exc:  # noqa: BLE001
        # LATCH the failure. `cg_window_capture` raises at IMPORT time when
        # Quartz is absent — it guards the import (`CG = None`) and then
        # evaluates `CG.kCGWindowImageDefault` in a class body, so the guard
        # does nothing. Python does not cache a module that raised while
        # importing, so retrying per step re-pays the whole failed import:
        # measured 427 ms per capture against 67 ms for NSWorkspace alone.
        # One attempt per process, then app-name-only.
        _window_list_unavailable[0] = (f"window list unavailable: "
                                       f"{type(exc).__name__}")
        snap.detail = snap.detail or _window_list_unavailable[0]
    return snap


async def capture() -> Snapshot:
    """A bounded snapshot, off the event loop. NEVER raises.

    A timeout yields a TIMED_OUT snapshot rather than nothing: knowing the
    machine could not be read in half a second is itself forensic.
    """
    if not enabled():
        return Snapshot(availability=Availability.DISABLED.value)
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_blocking_capture), timeout=capture_timeout_s())
    except asyncio.TimeoutError:
        return Snapshot(availability=Availability.TIMED_OUT.value,
                        detail=f"exceeded {capture_timeout_s():.2f}s")
    except Exception as exc:  # noqa: BLE001
        return Snapshot(availability=Availability.UNAVAILABLE.value,
                        detail=type(exc).__name__)


def delta(before: Optional[Snapshot],
          after: Optional[Snapshot]) -> Dict[str, Any]:
    """What changed between two snapshots. NEVER raises.

    ``changed`` is deliberately three-valued. If either side could not be
    observed the answer is ``None`` — not ``False``. "The app did not change"
    and "I could not tell whether the app changed" lead an operator to
    opposite decisions, and a forensic record that conflates them is worse than
    one that admits the gap.
    """
    out: Dict[str, Any] = {
        "schema_version": BLACK_BOX_SCHEMA_VERSION,
        "before": before.to_dict() if before else None,
        "after": after.to_dict() if after else None,
    }
    try:
        if before is None or after is None or not (
                before.observed and after.observed):
            out["changed"] = None
            out["summary"] = "state change could not be determined"
            return out
        app_changed = before.app != after.app
        title_changed = before.window_title != after.window_title
        out["changed"] = bool(app_changed or title_changed)
        out["app_changed"] = app_changed
        out["title_changed"] = title_changed
        out["elapsed_s"] = round(max(0.0, after.t - before.t), 3)
        if app_changed:
            out["summary"] = f"foreground moved {before.app!r} → {after.app!r}"
        elif title_changed:
            out["summary"] = (f"{after.app}: window title changed "
                              f"{before.window_title!r} → "
                              f"{after.window_title!r}")
        else:
            out["summary"] = f"{after.app}: no observable change"
    except Exception:  # noqa: BLE001
        out["changed"] = None
        out["summary"] = "delta computation failed"
    return out


def forensic_payload(*, step_index: int, action: str, target: str,
                     value: str, error: str,
                     before: Optional[Snapshot],
                     after: Optional[Snapshot]) -> Dict[str, Any]:
    """The record a CONFIRM prompt is rendered from. NEVER raises.

    ``after`` is normally absent — the step died before it could be taken —
    and its absence is the point: it is the difference between "the click
    landed and then we crashed" and "we crashed and cannot say".
    """
    try:
        return {
            "schema_version": BLACK_BOX_SCHEMA_VERSION,
            "step_index": step_index,
            "action": action,
            "target": (target or "")[:200],
            "value": (value or "")[:200],
            "error": (error or "")[:400],
            "post_captured": after is not None,
            **delta(before, after),
        }
    except Exception:  # noqa: BLE001
        return {"schema_version": BLACK_BOX_SCHEMA_VERSION,
                "step_index": step_index, "error": "payload build failed"}
