"""Wire contract between the Windows desktop daemon and its WSL provider.

Stdlib only: the daemon imports this file as a top-level module on the
Windows host (it is not run inside the ``backend`` package), and the
provider imports it as ``vision.windows_desktop.protocol`` inside WSL.

Discovery: the daemon writes ``endpoint.json`` -- ``{port, token, pid,
started_at, version}`` -- under ``%LOCALAPPDATA%\\JARVIS\\desktop_agent``.
That directory inherits the per-user ACL of LOCALAPPDATA, which is the
whole trust boundary: 127.0.0.1 is shared by every session on the host
(and by WSL under mirrored networking), so loopback alone authenticates
nobody. Every request must carry the token in ``TOKEN_HEADER``. A custom
header also keeps browsers out: a page cannot attach one cross-origin
without a CORS preflight the daemon never answers.
"""
from __future__ import annotations

import glob
import json
import os
import sys
from typing import Any, Dict, List, Optional

PROTOCOL_VERSION = 1
DEFAULT_PORT = int(os.environ.get("JARVIS_DESKTOP_AGENT_PORT", "47390"))
TOKEN_HEADER = "X-Jarvis-Desktop-Token"
ENDPOINT_FILENAME = "endpoint.json"
AGENT_DIRNAME = os.path.join("JARVIS", "desktop_agent")

# Error codes carried in ``{"ok": false, "error": <code>}`` payloads. The
# provider branches on these, never on HTTP reason text.
ERR_UNAUTHORIZED = "unauthorized"
ERR_NOT_FOUND = "not_found"
ERR_WINDOW_NOT_FOUND = "window_not_found"
ERR_SPACE_NOT_FOUND = "space_not_found"
ERR_WINDOW_MINIMIZED = "window_minimized"
ERR_WINDOW_NOT_RESPONDING = "window_not_responding"
ERR_DC_UNAVAILABLE = "dc_unavailable"
ERR_PRINTWINDOW_FAILED = "printwindow_failed"
ERR_BLANK_FRAME = "blank_frame"
ERR_SESSION_DISCONNECTED = "session_disconnected"
ERR_WIN32 = "win32_error"
ERR_INTERNAL = "internal_error"


def windows_endpoint_path() -> Optional[str]:
    """Where the daemon writes endpoint.json (Windows side)."""
    base = os.environ.get("LOCALAPPDATA")
    return os.path.join(base, AGENT_DIRNAME, ENDPOINT_FILENAME) if base else None


def candidate_endpoint_paths() -> List[str]:
    """Where a client may find endpoint.json, most explicit first.

    ``JARVIS_DESKTOP_AGENT_ENDPOINT_FILE`` wins. On Windows the caller's own
    LOCALAPPDATA is next. Inside WSL the Windows user is not knowable from
    the guest, so every ``/mnt/<drive>/Users/*`` profile is a candidate --
    ACLs make other users' files unreadable, and the freshest readable one
    wins in :func:`load_endpoint`.
    """
    paths: List[str] = []
    explicit = os.environ.get("JARVIS_DESKTOP_AGENT_ENDPOINT_FILE")
    if explicit:
        paths.append(explicit)
    if sys.platform == "win32":
        own = windows_endpoint_path()
        if own:
            paths.append(own)
    else:
        rel = os.path.join("AppData", "Local", AGENT_DIRNAME, ENDPOINT_FILENAME)
        paths.extend(sorted(glob.glob(os.path.join("/mnt", "?", "Users", "*", rel))))
    return paths


def load_endpoint() -> Optional[Dict[str, Any]]:
    """Return the freshest readable endpoint record, or None."""
    best: Optional[Dict[str, Any]] = None
    for path in candidate_endpoint_paths():
        try:
            with open(path, "r", encoding="utf-8") as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict) or "port" not in rec or "token" not in rec:
            continue
        rec["_path"] = path
        if best is None or rec.get("started_at", 0) > best.get("started_at", 0):
            best = rec
    return best
