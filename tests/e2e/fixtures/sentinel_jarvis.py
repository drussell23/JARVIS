"""Sentinel file with deliberately high cyclomatic complexity for E2E testing."""


def process_command(cmd: str, flags: dict) -> str:
    if cmd == "start":
        if flags.get("verbose"):
            if flags.get("debug"):
                return "start-verbose-debug"
            return "start-verbose"
        return "start"
    elif cmd == "stop":
        if flags.get("force"):
            return "stop-force"
        return "stop"
    elif cmd == "restart":
        if flags.get("graceful"):
            if flags.get("timeout"):
                return "restart-graceful-timeout"
            return "restart-graceful"
        return "restart"
    elif cmd == "status":
        if flags.get("json"):
            return "status-json"
        if flags.get("verbose"):
            return "status-verbose"
        return "status"
    elif cmd == "config":
        if flags.get("validate"):
            return "config-validate"
        if flags.get("reset"):
            return "config-reset"
        return "config"
    else:
        return "unknown"
