"""
host_platform - how prompts and user-facing text name the machine JARVIS runs on
(and the day it runs on).

Model prompts said "a macOS desktop assistant" / "native macOS application"
on every host, so a model on Windows resolved Spotify to a ``.app`` bundle
and was told to emit ``open -a``. Prompts ask here instead.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def os_display_name() -> str:
    """The running OS as a person would name it."""
    return {"darwin": "macOS", "win32": "Windows"}.get(sys.platform, "Linux")


def native_app_noun() -> str:
    """What an installed desktop application is called on this OS."""
    return {"darwin": "macOS application (.app)", "win32": "Windows application"}.get(
        sys.platform, "Linux desktop application")


def today_display() -> str:
    """Today's date as prompts state it: a model knows nothing newer than its
    training, so "this year" / "latest" mean nothing to it without this."""
    from datetime import datetime

    return datetime.now().strftime("%A, %B %d, %Y")


def log_dir() -> Path:
    """Where JARVIS's logs live on this OS -- where an operator looks for them.

    ``JARVIS_LOG_DIR`` overrides. Otherwise each OS's own convention:
    ``~/Library/Logs/JARVIS`` (macOS), ``%LOCALAPPDATA%\JARVIS\logs``
    (Windows, where scripts/windows/jarvis.ps1 writes), and
    ``$XDG_STATE_HOME/jarvis/logs`` (Linux). Not created here.
    """
    explicit = os.environ.get("JARVIS_LOG_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "JARVIS"
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "JARVIS" / "logs"
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "jarvis" / "logs"


__all__ = ["log_dir", "native_app_noun", "os_display_name", "today_display"]
