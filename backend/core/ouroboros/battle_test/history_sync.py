"""Distributed history sync — what you type in one cockpit, Up recalls in
every other, live.

The file-level unification (one deduped ``FileHistory`` at
``.jarvis/repl_history``) made history CONSISTENT across surfaces, but only
at load time: two `ov` panes in a tmux split each snapshot the file at
attach, and a command typed in pane A does not exist in pane B's memory
until B restarts. Polling the file would be a workaround wearing a fix's
clothes — the root cause is that history state has no NETWORK propagation,
while a UDS bridge purpose-built for state propagation is already running
between every one of these processes.

So history rides the bridge as one more typed frame:

  * every operator line already crosses upstream as an ``input`` frame WITH
    its session id — the daemon derives the fan-out from that seam rather
    than demanding a second frame for the same keystroke;
  * lines the client handles LOCALLY (``/deck``, ``/keys``) never cross, so
    only those travel as an explicit upstream ``history_append``;
  * the daemon routes: every OTHER attached session receives a downstream
    ``history_append`` (originator excluded server-side — it already has
    the line), and the daemon injects into its own singleton so the
    operator at the daemon terminal gets the same recall;
  * receivers inject MEMORY-ONLY. The originator's buffer already made the
    one disk write to the shared file; a second store would duplicate the
    entry for every attached terminal.

Injection exploits prompt_toolkit's own lifecycle rather than fighting it:
``Buffer.reset()`` cancels the history-load task and the next repaint
re-runs ``History.load()``, which re-yields ``_loaded_strings`` — so an
entry inserted there is picked up automatically at the next prompt cycle.
For the tmux edge (pane B idle at a pristine prompt, no reset coming), the
entry is also folded into the live buffer's working lines exactly the way
the library's own loader does, and the app is invalidated.

Env: ``JARVIS_HISTORY_SYNC_ENABLED`` (default true).

NEVER raises into the bridge read-loop, the accept path, or a repaint.
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

HISTORY_SYNC_SCHEMA_VERSION: str = "history_sync.1"

MASTER_FLAG_ENV_VAR: str = "JARVIS_HISTORY_SYNC_ENABLED"

#: The frame type, both directions. Upstream (client→daemon) it carries the
#: lines that never cross as ``input``; downstream (daemon→client) it is the
#: fan-out, stamped with ``origin`` so a client can drop its own echo even
#: if server-side exclusion ever mis-routes.
HISTORY_APPEND_FRAME: str = "history_append"


def is_history_sync_enabled() -> bool:
    """Master flag — default true. NEVER raises."""
    raw = os.environ.get(MASTER_FLAG_ENV_VAR, "true")
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _live_buffer_refresh(text: str, history: Any) -> None:
    """Fold *text* into the ACTIVE prompt buffer, if there is one and it is
    safe: same history object, pristine (empty) composing line. A buffer
    mid-composition is left alone — it collects the entry at its next
    reset+repaint through the normal load path. Mirrors the library's own
    loader mutation (``_working_lines.appendleft`` + index bump), which is
    the one documented-by-implementation way working lines grow."""
    try:
        from prompt_toolkit.application.current import get_app_or_none
        app = get_app_or_none()
        if app is None:
            return
        buf = app.current_buffer
        if buf is None or buf.history is not history:
            return
        if buf.text:
            return  # composing — next reset picks it up
        buf._working_lines.appendleft(text)
        buf._Buffer__working_index += 1  # noqa: SLF001 — the loader's own move
        app.invalidate()
    except Exception:  # noqa: BLE001
        logger.debug("[HistorySync] live refresh degraded", exc_info=True)


def inject_history_entry(
    text: object,
    *,
    history: Any = None,
) -> bool:
    """Memory-only injection of one remote entry into this process's
    history. No ``store_string`` — the originating terminal already made
    the single disk write to the shared file.

    Dedupes against the most recent entry (same rule as local appends) and
    refuses blanks. Returns True when the entry landed. NEVER raises."""
    if not is_history_sync_enabled():
        return False
    try:
        entry = str(text or "")
        if not entry.strip():
            return False
        if history is None:
            from backend.core.ouroboros.battle_test.repl_completion import (
                build_history,
            )
            history = build_history()
        if history is None:
            return False
        # EDGE: a history nothing has loaded yet (headless daemon, no REPL
        # surface) destroys injections — prompt_toolkit's first ``load()``
        # REPLACES ``_loaded_strings`` from storage. Force the storage read
        # first, exactly as ``History.load()`` would, so the insert lands
        # on top of the real past instead of under it.
        if getattr(history, "_loaded", True) is False:
            try:
                history._loaded_strings = list(
                    history.load_history_strings()
                )
                history._loaded = True
            except Exception:  # noqa: BLE001
                pass
        loaded = getattr(history, "_loaded_strings", None)
        if loaded is None:
            return False
        if loaded and loaded[0] == entry:
            return False  # immediate repeat — already recallable
        loaded.insert(0, entry)
        _live_buffer_refresh(entry, history)
        return True
    except Exception:  # noqa: BLE001
        logger.debug("[HistorySync] inject degraded", exc_info=True)
        return False


def make_client_injector() -> Any:
    """The ``on_history_append`` callback for a ``CockpitAttachClient`` —
    one place, so both attach loops mount identical behavior."""
    def _on_history_append(text: str) -> None:
        inject_history_entry(text)
    return _on_history_append


def send_local_append(client: Any, text: object) -> bool:
    """Report a CLIENT-HANDLED line upstream so other terminals recall it.

    Only for lines that never cross as ``input`` frames — sending both
    would fan the same keystroke out twice. NEVER raises."""
    if not is_history_sync_enabled():
        return False
    try:
        entry = str(text or "").strip()
        if not entry:
            return False
        send = getattr(client, "send_history", None)
        if not callable(send):
            return False
        return bool(send(entry))
    except Exception:  # noqa: BLE001
        return False


__all__ = [
    "HISTORY_APPEND_FRAME",
    "HISTORY_SYNC_SCHEMA_VERSION",
    "MASTER_FLAG_ENV_VAR",
    "inject_history_entry",
    "is_history_sync_enabled",
    "make_client_injector",
    "send_local_append",
]
