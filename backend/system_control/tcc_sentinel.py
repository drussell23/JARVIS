"""Refuse to boot blind rather than fail silently mid-DAG.

macOS gates screen capture and UI automation behind TCC grants that attach to a
CODE IDENTITY, not to a path. An app launched from Xcode has a different
identity than the same app launched from Finder, so grants do not carry over —
and nothing announces it. The APIs simply start returning nothing.

That is not hypothetical here. Measured on this machine, in this process:

    AXIsProcessTrusted()             -> False
    CGPreflightScreenCaptureAccess() -> False

Which is exactly why `cg_window_capture` returned no windows and the black box
recorded "window list unavailable" — a permissions problem wearing the costume
of a bug. A vision tool that returns an empty list looks, to a model, like a
screen with nothing on it. It will reason confidently from that.

WHY A SENTINEL AND NOT A try/except AT EACH CALL SITE
-------------------------------------------------------
Per-call handling means every consumer independently guesses whether "no
windows" means "no windows". The grant is a BOOT-TIME fact about this process's
identity — it cannot change while running — so it is checked once, loudly, at
the point where a bad answer is still cheap.

FAIL FAST, BUT ONLY WHERE FAST IS SAFE
----------------------------------------
Blocking the boot is the correct response for a HUD whose whole job is seeing
the screen. It is the WRONG response for a headless CI run, a unit test, or a
soak on a Linux node, where these grants are meaningless and unobtainable.

So the Sentinel reports, and `enforce()` blocks only when the caller declares
that it needs the grant. Non-macOS and no-GUI sessions are NOT failures — they
are environments where the question does not apply, which is a different fact
and gets a different verdict.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import ctypes
import enum
import logging
import os
import platform
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("JARVIS.TCCSentinel")

TCC_SENTINEL_SCHEMA_VERSION: str = "tcc_sentinel.v1"

_APPLICATION_SERVICES = (
    "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
)
_CORE_GRAPHICS = "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"


class TCCMissingError(RuntimeError):
    """A required macOS privacy grant is absent for THIS process identity."""

    def __init__(self, missing: List[str], detail: str = "") -> None:
        self.missing = list(missing)
        self.code = "TCC_MISSING"
        super().__init__(detail or f"TCC_MISSING: {', '.join(missing)}")


class Grant(str, enum.Enum):
    ACCESSIBILITY = "accessibility"
    SCREEN_RECORDING = "screen_recording"


class Verdict(str, enum.Enum):
    """Three states, because two would lie about one of them."""

    GRANTED = "granted"
    DENIED = "denied"
    #: Not macOS, or no GUI session. The question does not apply — which is not
    #: the same as the answer being no, and must not block a Linux soak node.
    NOT_APPLICABLE = "not_applicable"


def sentinel_enabled() -> bool:
    """Master gate. Default TRUE — read-only probes. NEVER raises."""
    return (os.environ.get("JARVIS_TCC_SENTINEL_ENABLED", "true")
            or "").strip().lower() not in ("0", "false", "no", "off")


def _is_macos_gui() -> bool:
    """Is this a macOS session where TCC even has meaning? NEVER raises."""
    try:
        if platform.system() != "Darwin":
            return False
        # A launchd daemon or ssh session has no window server connection, so
        # the grants are unobtainable rather than missing.
        return bool(os.environ.get("__CFBundleIdentifier")
                    or os.environ.get("TERM_SESSION_ID")
                    or os.environ.get("SSH_TTY") is None)
    except Exception:  # noqa: BLE001
        return False


def _probe_accessibility() -> Verdict:
    """`AXIsProcessTrusted()` — may this process drive other apps' UI?

    ctypes against the system framework rather than a pyobjc wrapper: the
    `ApplicationServices` wrapper is NOT installed here (verified), and adding
    a pip dependency to ask a yes/no question the OS answers directly would be
    a heavier fix than the problem. NEVER raises.
    """
    if not _is_macos_gui():
        return Verdict.NOT_APPLICABLE
    try:
        lib = ctypes.CDLL(_APPLICATION_SERVICES)
        lib.AXIsProcessTrusted.restype = ctypes.c_bool
        return Verdict.GRANTED if lib.AXIsProcessTrusted() else Verdict.DENIED
    except Exception:  # noqa: BLE001
        logger.debug("[TCCSentinel] accessibility probe degraded", exc_info=True)
        return Verdict.NOT_APPLICABLE


def _probe_screen_recording() -> Verdict:
    """`CGPreflightScreenCaptureAccess()` — PREFLIGHT, never Request.

    `CGRequestScreenCaptureAccess` shows a system dialog and, on first denial,
    permanently poisons the answer until the app is re-added by hand. A boot
    probe must observe, not prompt. NEVER raises.
    """
    if not _is_macos_gui():
        return Verdict.NOT_APPLICABLE
    try:
        cg = ctypes.CDLL(_CORE_GRAPHICS)
        fn = getattr(cg, "CGPreflightScreenCaptureAccess", None)
        if fn is None:
            return Verdict.NOT_APPLICABLE      # pre-10.15
        fn.restype = ctypes.c_bool
        return Verdict.GRANTED if fn() else Verdict.DENIED
    except Exception:  # noqa: BLE001
        logger.debug("[TCCSentinel] screen-recording probe degraded",
                     exc_info=True)
        return Verdict.NOT_APPLICABLE


def _bundle_identity() -> str:
    """Which code identity the grants would attach to. NEVER raises.

    The whole reason grants vanish under Xcode: TCC keys on this, and a bare
    interpreter has none.
    """
    try:
        from Foundation import NSBundle
        return str(NSBundle.mainBundle().bundleIdentifier() or "")
    except Exception:  # noqa: BLE001
        return ""


@dataclass
class TCCReading:
    """What this process identity may actually do."""

    accessibility: str = Verdict.NOT_APPLICABLE.value
    screen_recording: str = Verdict.NOT_APPLICABLE.value
    bundle_identifier: str = ""
    executable: str = ""
    schema_version: str = TCC_SENTINEL_SCHEMA_VERSION
    notes: List[str] = field(default_factory=list)

    def verdict(self, grant: Grant) -> Verdict:
        raw = (self.accessibility if grant is Grant.ACCESSIBILITY
               else self.screen_recording)
        try:
            return Verdict(raw)
        except ValueError:
            return Verdict.NOT_APPLICABLE

    def denied(self, required: Optional[List[Grant]] = None) -> List[str]:
        """Required grants explicitly DENIED. NOT_APPLICABLE is not denial."""
        req = required if required is not None else list(Grant)
        return [g.value for g in req if self.verdict(g) is Verdict.DENIED]

    def as_dict(self) -> Dict[str, Any]:
        return {"schema_version": self.schema_version,
                "accessibility": self.accessibility,
                "screen_recording": self.screen_recording,
                "bundle_identifier": self.bundle_identifier or "(none)",
                "executable": self.executable, "notes": list(self.notes)}


def probe() -> TCCReading:
    """Read this process's grants. NEVER raises."""
    reading = TCCReading()
    if not sentinel_enabled():
        reading.notes.append("sentinel disabled by env")
        return reading
    try:
        reading.accessibility = _probe_accessibility().value
        reading.screen_recording = _probe_screen_recording().value
        reading.bundle_identifier = _bundle_identity()
        reading.executable = str(sys.executable or "")
        if not reading.bundle_identifier and platform.system() == "Darwin":
            reading.notes.append(
                "no bundle identifier — TCC grants attach to a CODE IDENTITY, "
                "so a bare interpreter cannot inherit an app's grants")
    except Exception:  # noqa: BLE001
        logger.debug("[TCCSentinel] probe degraded", exc_info=True)
    return reading


def render(reading: Optional[TCCReading] = None,
           required: Optional[List[Grant]] = None) -> List[str]:
    """The loud diagnostic. NEVER raises.

    Says what to DO, not merely what is wrong: a boot-blocking error whose fix
    is "open the right pane of System Settings" should name the pane.
    """
    try:
        r = reading if reading is not None else probe()
        rows = [
            "⚠ macOS privacy grants missing for THIS process identity",
            f"   accessibility     : {r.accessibility}",
            f"   screen recording  : {r.screen_recording}",
            f"   bundle identifier : {r.bundle_identifier or '(none)'}",
            f"   executable        : {r.executable}",
        ]
        rows.extend(f"   note: {n}" for n in r.notes)
        if r.denied(required):
            rows += [
                "",
                "   TCC grants attach to a CODE IDENTITY, not a path — an app",
                "   launched from Xcode is a DIFFERENT identity from the same",
                "   app launched from Finder, so grants do not carry over.",
                "",
                "   Fix: System Settings → Privacy & Security →",
                "        Accessibility  AND  Screen Recording",
                "        …add the binary shown above, then relaunch.",
                "",
                "   Refusing to boot: vision tools would return EMPTY rather",
                "   than error, and a model cannot tell an empty screen from",
                "   an unreadable one.",
            ]
        return rows
    except Exception:  # noqa: BLE001
        return ["⚠ TCC sentinel unavailable"]


def enforce(required: Optional[List[Grant]] = None, *,
            reading: Optional[TCCReading] = None) -> TCCReading:
    """Raise `TCCMissingError` if a REQUIRED grant is denied. NEVER raises otherwise.

    The caller declares what it needs, so a headless soak imports this module
    without inheriting a HUD's requirements. NOT_APPLICABLE never blocks: an
    environment where the question is meaningless is not a failure.
    """
    r = reading if reading is not None else probe()
    missing = r.denied(required)
    if not missing:
        return r
    for line in render(r, required):
        logger.error("%s", line)
    raise TCCMissingError(missing, detail=(
        f"TCC_MISSING: {', '.join(missing)} denied for identity "
        f"{r.bundle_identifier or '(no bundle)'} — refusing to boot blind"))
