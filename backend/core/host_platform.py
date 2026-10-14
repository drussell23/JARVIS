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


def account_full_name() -> str:
    """The signed-in account's full (display) name as the OS records it, or "".

    Never the login name: an account called "Jarvis" or "admin" names an
    account, not a person, and greeting with it addressed the operator as
    "Jarvis". macOS: Directory Services RealName. Windows: the account's Full
    Name (GetUserNameExW display name, else NetUserGetInfo). Elsewhere: GECOS.
    Read once per process: it is a directory lookup, and it does not change
    while JARVIS runs.
    """
    global _account_full_name
    if _account_full_name is None:
        _account_full_name = _read_account_full_name()
    return _account_full_name


_account_full_name = None


def _read_account_full_name() -> str:
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            size = wintypes.ULONG(256)
            buf = ctypes.create_unicode_buffer(size.value)
            name_display = 3  # EXTENDED_NAME_FORMAT.NameDisplay
            if ctypes.windll.secur32.GetUserNameExW(name_display, buf, ctypes.byref(size)):
                if buf.value.strip():
                    return buf.value.strip()
            # Local accounts: the Full Name field of the user record.
            class _UserInfo10(ctypes.Structure):  # USER_INFO_10
                _fields_ = [("name", wintypes.LPWSTR), ("comment", wintypes.LPWSTR),
                            ("usr_comment", wintypes.LPWSTR), ("full_name", wintypes.LPWSTR)]

            login = os.environ.get("USERNAME", "")
            info = ctypes.POINTER(_UserInfo10)()
            if login and ctypes.windll.netapi32.NetUserGetInfo(None, login, 10, ctypes.byref(info)) == 0:
                try:
                    return (info.contents.full_name or "").strip()
                finally:
                    ctypes.windll.netapi32.NetApiBufferFree(info)
            return ""
        if sys.platform == "darwin":
            import subprocess

            login = os.environ.get("USER") or os.getlogin()
            out = subprocess.run(["dscl", ".", "-read", f"/Users/{login}", "RealName"],
                                 capture_output=True, text=True, timeout=5)
            lines = [line.strip() for line in out.stdout.splitlines()]
            if out.returncode == 0:
                value = lines[0].partition(":")[2].strip() or (lines[1] if len(lines) > 1 else "")
                return value.strip()
            return ""
        import pwd

        return pwd.getpwuid(os.getuid()).pw_gecos.split(",")[0].strip()
    except Exception:  # noqa: BLE001 -- no directory service answer means no name
        return ""


def remote_session() -> bool:
    """True when this desktop is being driven over Remote Desktop.

    Then the microphone, speakers and screen belong to the CLIENT machine: a
    browser here sees a microphone only if the client redirects one, and "no
    microphone" means the client's redirection is off, not a missing device.
    Windows: GetSystemMetrics(SM_REMOTESESSION). Elsewhere there is no such
    session to detect.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.user32.GetSystemMetrics(0x1000))  # SM_REMOTESESSION
    except Exception:  # noqa: BLE001
        return False


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


__all__ = ["account_full_name", "log_dir", "native_app_noun", "os_display_name", "remote_session",
           "today_display"]
