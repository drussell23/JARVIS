#!/bin/bash
# JARVIS Supervisor startup wrapper — used by launchd (com.jarvis.supervisor).
# launchd cannot initialize a Python venv directly due to macOS pyvenv.cfg restrictions.
# This script activates the venv explicitly before exec-ing the supervisor, which avoids
# the PermissionError: [Errno 1] on .venv/pyvenv.cfg that plain plist invocation hits.

set -e

REPO="/Users/djrussell23/Documents/repos/JARVIS-AI-Agent"
VENV="$REPO/.venv"

cd "$REPO"

# Activate venv (sets VIRTUAL_ENV, adjusts PATH/PYTHONPATH, no pyvenv.cfg read at exec)
# shellcheck source=/dev/null
source "$VENV/bin/activate"

# Exec replaces this shell with the supervisor — launchd tracks the supervisor PID directly
exec python3 unified_supervisor.py "$@"
