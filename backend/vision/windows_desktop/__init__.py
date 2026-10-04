"""Windows virtual-desktop backend for multi-space intelligence.

``daemon.py`` runs natively on the Windows host (Win32 + pyvda);
``provider.py`` runs with the JARVIS backend and speaks to it over loopback.
``create_space_detector`` is the one place a space backend is chosen.
"""
from __future__ import annotations

import os
import sys

_WSL_MARKER = "/proc/sys/kernel/osrelease"


def _running_under_wsl() -> bool:
    try:
        with open(_WSL_MARKER, "r", encoding="utf-8") as fh:
            return "microsoft" in fh.read().lower()
    except OSError:
        return False


def space_backend() -> str:
    """``yabai`` or ``windows_agent``, from ``JARVIS_SPACE_BACKEND`` (default ``auto``).

    ``auto`` picks the Windows agent on Windows and inside WSL -- the only
    hosts where it can exist -- and yabai everywhere else, so macOS is
    byte-identical unless the operator opts in.
    """
    choice = os.environ.get("JARVIS_SPACE_BACKEND", "auto").strip().lower()
    if choice in ("yabai", "windows_agent"):
        return choice
    return "windows_agent" if sys.platform == "win32" or _running_under_wsl() else "yabai"


def create_space_detector(**kwargs):
    """A new space detector for this host. Same kwargs as ``YabaiSpaceDetector``."""
    if space_backend() == "windows_agent":
        from .provider import WindowsDesktopProvider

        return WindowsDesktopProvider(**kwargs)
    from vision.yabai_space_detector import YabaiSpaceDetector

    return YabaiSpaceDetector(**kwargs)
