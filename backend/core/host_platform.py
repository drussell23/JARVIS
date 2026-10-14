"""
host_platform - how prompts and user-facing text name the machine JARVIS runs on.

Model prompts said "a macOS desktop assistant" / "native macOS application"
on every host, so a model on Windows resolved Spotify to a ``.app`` bundle
and was told to emit ``open -a``. Prompts ask here instead.
"""
from __future__ import annotations

import sys


def os_display_name() -> str:
    """The running OS as a person would name it."""
    return {"darwin": "macOS", "win32": "Windows"}.get(sys.platform, "Linux")


def native_app_noun() -> str:
    """What an installed desktop application is called on this OS."""
    return {"darwin": "macOS application (.app)", "win32": "Windows application"}.get(
        sys.platform, "Linux desktop application")


__all__ = ["native_app_noun", "os_display_name"]
