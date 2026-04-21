"""Sublime Text plugin entry point — `JARVIS Observability`.

Wires the pure-logic `jarvis_api` client + stream consumer into
Sublime's main-thread dispatch. Commands:

  * ``jarvis_connect``    — start the SSE consumer
  * ``jarvis_disconnect`` — stop the SSE consumer
  * ``jarvis_refresh``    — re-fetch the op list
  * ``jarvis_show_ops``   — open the quick panel with the live op list
  * ``jarvis_show_log``   — reveal the output panel used for logging

All listeners from the consumer thread dispatch to the main thread
via ``sublime.set_timeout`` — Sublime's view API is not thread-safe.

This module is imported at plugin_load. The `plugin_unloaded` hook
is the canonical shutdown path; it calls `stop()` on any active
consumer, matching the Slice 3 extension-ctx shutdown discipline.
"""
from __future__ import annotations

import json
import sys
from typing import Any, Dict, List, Optional

# Sublime-only imports — stubbed via `sys.modules` manipulation in
# tests that want to exercise plugin logic without Sublime running.
# The main-thread dispatch wrapper abstracts the import so CPython
# unit tests can replace it with a pass-through.
try:
    import sublime  # type: ignore[import-not-found]
    import sublime_plugin  # type: ignore[import-not-found]
    _HAS_SUBLIME = True
except ImportError:
    sublime = None  # type: ignore[assignment]
    sublime_plugin = None  # type: ignore[assignment]
    _HAS_SUBLIME = False

# Sublime re-imports plugin modules from the Packages dir, where
# relative imports do not work reliably. Use absolute import with
# fallback for CI / local testing.
try:
    from .jarvis_api import (
        ObservabilityClient,
        ObservabilityError,
        SchemaMismatchError,
        StreamConsumer,
        STATE_CONNECTED,
        STATE_DISCONNECTED,
        STATE_ERROR,
        STATE_RECONNECTING,
        is_control_event,
        is_task_event,
    )
except ImportError:
    from jarvis_api import (  # type: ignore[no-redef]
        ObservabilityClient,
        ObservabilityError,
        SchemaMismatchError,
        StreamConsumer,
        STATE_CONNECTED,
        STATE_DISCONNECTED,
        STATE_ERROR,
        STATE_RECONNECTING,
        is_control_event,
        is_task_event,
    )


SETTINGS_FILE = "JARVIS.sublime-settings"
OUTPUT_PANEL_NAME = "jarvis_observability"
STATUS_KEY = "jarvis_observability"


# --- Main-thread dispatch --------------------------------------------------


def main_thread(fn):
    """Dispatch a zero-arg callable onto Sublime's main thread.

    Used by every listener invoked from the consumer thread —
    Sublime's view/window APIs are main-thread-only.
    """
    if _HAS_SUBLIME:
        sublime.set_timeout(fn, 0)  # type: ignore[union-attr]
    else:
        # In tests without Sublime, just run synchronously.
        fn()


# --- Plugin state (module-level singleton) ---------------------------------


class _PluginState:
    """Process-wide state holder. Sublime imports this module exactly
    once per process; the singleton survives across view changes."""

    def __init__(self) -> None:
        self.consumer: Optional[StreamConsumer] = None
        self.client: Optional[ObservabilityClient] = None
        self.op_ids: List[str] = []
        self.last_state: str = STATE_DISCONNECTED

    def read_settings(self) -> Dict[str, Any]:
        """Read the `JARVIS.sublime-settings` values with typed
        defaults. Sublime merges user settings over the defaults
        automatically."""
        defaults = {
            "endpoint": "http://127.0.0.1:8765",
            "enabled": True,
            "auto_reconnect": True,
            "reconnect_max_backoff_s": 30.0,
            "op_id_filter": "",
        }
        if not _HAS_SUBLIME:
            return defaults
        s = sublime.load_settings(SETTINGS_FILE)  # type: ignore[union-attr]
        out = {}
        for k, d in defaults.items():
            out[k] = s.get(k, d)
        return out

    def build_client(self) -> ObservabilityClient:
        cfg = self.read_settings()
        self.client = ObservabilityClient(endpoint=cfg["endpoint"])
        return self.client


_state = _PluginState()


# --- Logging ---------------------------------------------------------------


def _log(msg: str) -> None:
    """Write to the output panel. Safe to call from any thread —
    always dispatches to the main thread."""
    def _do() -> None:
        if not _HAS_SUBLIME:
            print("[jarvis] " + msg)
            return
        window = sublime.active_window()  # type: ignore[union-attr]
        if window is None:
            return
        panel = window.find_output_panel(OUTPUT_PANEL_NAME)
        if panel is None:
            panel = window.create_output_panel(OUTPUT_PANEL_NAME)
        panel.run_command("append", {"characters": "[jarvis] " + msg + "\n"})
    main_thread(_do)


# --- Stream wiring ---------------------------------------------------------


def _on_state(new_state: str) -> None:
    _state.last_state = new_state

    def _do() -> None:
        if not _HAS_SUBLIME:
            return
        window = sublime.active_window()  # type: ignore[union-attr]
        if window is None:
            return
        view = window.active_view()
        if view is None:
            return
        label = "JARVIS: " + new_state
        view.set_status(STATUS_KEY, label)
    main_thread(_do)


def _on_event(event: Dict[str, Any]) -> None:
    """Handle a stream event. Tree updates flow through a main-
    thread dispatch."""
    if is_control_event(event):
        if event.get("event_type") == "stream_lag":
            _log("stream_lag — refreshing op list")
            _refresh()
        return
    if is_task_event(event):
        op_id = event.get("op_id")
        if isinstance(op_id, str) and op_id and op_id not in _state.op_ids:
            _state.op_ids.append(op_id)


# --- Commands --------------------------------------------------------------


def _connect() -> None:
    if _state.consumer is not None:
        _log("already connected")
        return
    cfg = _state.read_settings()
    if not cfg.get("enabled"):
        _log("JARVIS disabled in settings; aborting connect")
        return
    consumer = StreamConsumer(
        endpoint=cfg["endpoint"],
        op_id_filter=cfg.get("op_id_filter") or None,
        auto_reconnect=bool(cfg.get("auto_reconnect", True)),
        reconnect_max_backoff_s=float(cfg.get("reconnect_max_backoff_s", 30.0)),
        logger=_log,
    )
    consumer.on_event(_on_event)
    consumer.on_state(_on_state)
    _state.consumer = consumer
    _state.build_client()
    consumer.start()
    _refresh()
    _log("connected to " + cfg["endpoint"])


def _disconnect() -> None:
    consumer = _state.consumer
    _state.consumer = None
    if consumer is None:
        _log("not connected")
        return
    consumer.stop()
    _log("disconnected")


def _refresh() -> None:
    client = _state.client or _state.build_client()
    try:
        payload = client.task_list()
    except ObservabilityError as exc:
        _log("refresh failed: %s" % exc)
        return
    except SchemaMismatchError as exc:
        _log("schema mismatch on refresh: %s" % exc)
        return
    _state.op_ids = list(payload.get("op_ids", []))
    _log("op list: %d op%s"
         % (len(_state.op_ids), "" if len(_state.op_ids) == 1 else "s"))


def _show_ops() -> None:
    """Open the quick panel with the cached op_id list. Selecting
    one fetches detail and renders into the output panel."""
    if not _HAS_SUBLIME:
        return
    window = sublime.active_window()  # type: ignore[union-attr]
    if window is None:
        return
    if not _state.op_ids:
        _log("no ops cached — run JARVIS: Refresh first")
        return
    items = list(_state.op_ids)

    def on_select(idx: int) -> None:
        if idx < 0 or idx >= len(items):
            return
        op_id = items[idx]
        client = _state.client or _state.build_client()
        try:
            detail = client.task_detail(op_id)
        except ObservabilityError as exc:
            _log("taskDetail(%s) failed: %s" % (op_id, exc))
            return
        _render_op_detail(detail)

    window.show_quick_panel(items, on_select)


def _render_op_detail(detail: Dict[str, Any]) -> None:
    """Render a compact text view of a single op's task list in the
    dedicated output panel."""
    if not _HAS_SUBLIME:
        print(json.dumps(detail, indent=2))
        return
    window = sublime.active_window()  # type: ignore[union-attr]
    if window is None:
        return
    panel = window.find_output_panel(OUTPUT_PANEL_NAME)
    if panel is None:
        panel = window.create_output_panel(OUTPUT_PANEL_NAME)
    text = render_op_detail_text(detail)
    panel.run_command("append", {"characters": text + "\n\n"})
    window.run_command("show_panel", {"panel": "output." + OUTPUT_PANEL_NAME})


def render_op_detail_text(detail: Dict[str, Any]) -> str:
    """Pure function — exported for unit tests. Produces a
    text-mode rendering of the task detail projection."""
    lines: List[str] = []
    op_id = detail.get("op_id", "?")
    closed = detail.get("closed", False)
    tag = "CLOSED" if closed else "LIVE"
    active = detail.get("active_task_id") or "-"
    lines.append("=" * 60)
    lines.append("%s [%s]  active=%s  tasks=%d"
                 % (op_id, tag, active, detail.get("board_size", 0)))
    lines.append("=" * 60)
    for task in detail.get("tasks", []):
        state = task.get("state", "?")
        title = task.get("title", "")
        tid = task.get("task_id", "")
        seq = task.get("sequence", 0)
        lines.append(
            "  [#%03d] %-14s  %s  %s" % (seq, state, tid, title)
        )
        cr = task.get("cancel_reason") or ""
        if cr:
            lines.append("           reason: %s" % cr)
    return "\n".join(lines)


# --- Sublime plugin command classes ----------------------------------------


if _HAS_SUBLIME:

    class JarvisConnectCommand(sublime_plugin.ApplicationCommand):  # type: ignore[misc]
        def run(self) -> None:  # noqa: D401
            _connect()

    class JarvisDisconnectCommand(sublime_plugin.ApplicationCommand):  # type: ignore[misc]
        def run(self) -> None:  # noqa: D401
            _disconnect()

    class JarvisRefreshCommand(sublime_plugin.ApplicationCommand):  # type: ignore[misc]
        def run(self) -> None:  # noqa: D401
            _refresh()

    class JarvisShowOpsCommand(sublime_plugin.ApplicationCommand):  # type: ignore[misc]
        def run(self) -> None:  # noqa: D401
            _show_ops()

    class JarvisShowLogCommand(sublime_plugin.ApplicationCommand):  # type: ignore[misc]
        def run(self) -> None:  # noqa: D401
            window = sublime.active_window()  # type: ignore[union-attr]
            if window is None:
                return
            window.run_command("show_panel", {"panel": "output." + OUTPUT_PANEL_NAME})

    def plugin_loaded() -> None:  # noqa: D401
        cfg = _state.read_settings()
        if cfg.get("enabled"):
            _connect()

    def plugin_unloaded() -> None:  # noqa: D401
        _disconnect()
