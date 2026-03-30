"""Terminal-based HUD stub (v1). Full overlay is v2."""
import logging
import sys
from typing import Optional

logger = logging.getLogger("jarvis.brainstem.hud")

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_CYAN = "\033[36m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_MAGENTA = "\033[35m"


class HUD:
    def __init__(self) -> None:
        self._active_streams: dict[str, list[str]] = {}

    def show_status(self, message: str, duration: float = 0.0) -> None:
        print(f"{_CYAN}{_BOLD}[JARVIS]{_RESET} {message}", flush=True)

    def show_error(self, message: str) -> None:
        print(f"{_RED}{_BOLD}[ERROR]{_RESET} {message}", file=sys.stderr, flush=True)

    def begin_stream(self, command_id: str, source: str = "claude") -> None:
        self._active_streams[command_id] = []
        print(f"\n{_DIM}[{source}]{_RESET} ", end="", flush=True)

    def append_token(self, command_id: str, token: str) -> None:
        if command_id in self._active_streams:
            self._active_streams[command_id].append(token)
        print(token, end="", flush=True)

    def complete_stream(self, command_id: str, source: Optional[str] = None, latency_ms: Optional[int] = None, artifacts: Optional[list] = None) -> None:
        self._active_streams.pop(command_id, None)
        parts = []
        if latency_ms is not None:
            parts.append(f"{latency_ms}ms")
        if artifacts:
            parts.append(f"{len(artifacts)} artifacts")
        suffix = f" {_DIM}({', '.join(parts)}){_RESET}" if parts else ""
        print(f"\n{_GREEN}✓{_RESET}{suffix}", flush=True)

    def show_daemon(self, text: str, source: str = "unknown", urgent: bool = False) -> None:
        color = _RED if urgent else _MAGENTA
        label = "URGENT" if urgent else "daemon"
        print(f"{color}{_BOLD}[{label}]{_RESET} {_DIM}({source}){_RESET} {text}", flush=True)

    def show_progress(self, command_id: str, phase: str, progress: Optional[int] = None, message: str = "") -> None:
        bar = ""
        if progress is not None:
            filled = progress // 5
            bar = f" [{'█' * filled}{'░' * (20 - filled)}] {progress}%"
        print(f"{_YELLOW}[{phase}]{_RESET}{bar} {_DIM}{message}{_RESET}", flush=True)

    def show_action(self, action_type: str, payload: dict) -> None:
        print(f"{_CYAN}[action:{action_type}]{_RESET} {_DIM}{payload}{_RESET}", flush=True)
