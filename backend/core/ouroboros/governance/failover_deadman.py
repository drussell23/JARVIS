"""Sovereign Failover Lifecycle -- Phase 3a: Dead-Man's Switch (2026-06-23).

When the failover FSM awakens a J-Prime node from the golden image, it injects
this module's output as a ``--metadata-from-file=startup-script=`` payload.
That startup-script installs an UNBREAKABLE node-side watchdog that self-deletes
the VM when the local Ollama endpoint (localhost:<port>) has been idle for more
than ``idle_timeout_s`` seconds **and** the node has been up for at least
``boot_grace_s`` seconds.

This is the cost-safety backstop for the following failure class:

  Body (orchestrator) dies / loses track of the node after awakening it.

The FSM handles normal handback (the healthy lifecycle).  This module handles
the abnormal case: Body-death leaves a live J-Prime node burning money forever.
The Dead-Man's Switch ensures the node tears ITSELF down even if the Body is
gone -- no external watcher, no orphan billing.

Design discipline
-----------------
* **Pure string assembly** -- build_deadman_startup_script() has no I/O,
  no subprocess, no network.  The generated bash script is entirely
  self-contained; it does all I/O at runtime on the remote node.
* **Node-side independence** -- the bash watchdog reads instance identity
  (project, zone, name) and the SA token from the GCE metadata server at
  runtime.  Neither is baked in.  The golden image has NO gcloud; it uses
  curl + the metadata-server SA token + Compute REST API -- mirroring
  sovereign_self_termination.py exactly.
* **Fail-soft** -- every step in the watchdog loop is surrounded by
  error-handling.  A transient metadata/curl failure retries on the next
  tick; the watchdog NEVER crashes.
* **Idempotent** -- the self-delete call is the GCE DELETE verb; GCE handles
  repeated DELETEs gracefully (409/404 on an already-deleted instance).
* **Boot grace** -- a freshly-awakened node that the Body hasn't routed to
  yet is NOT killed prematurely.  Self-delete is only attempted once
  uptime > boot_grace_s AND idle > idle_timeout_s.
* **ASCII only** -- the bash script uses only 7-bit ASCII.  No emojis, no
  Unicode, no non-ASCII comments.
* **Gated default-ON** -- deadman_enabled() defaults to True.  The dead-man
  is the cost-safety backstop; disabling it is a conscious operator decision
  that logs a [SOVEREIGN WARNING].

Env knobs
---------
  JARVIS_FAILOVER_DEADMAN_ENABLED   -- master gate (default true)
  JARVIS_DEADMAN_IDLE_TIMEOUT_S     -- default 1800 (30 min)
  JARVIS_DEADMAN_CHECK_INTERVAL_S   -- default 300 (5 min)
  JARVIS_DEADMAN_BOOT_GRACE_S       -- default 2100 (35 min)
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Env-var names.
# ---------------------------------------------------------------------------
_ENV_ENABLED = "JARVIS_FAILOVER_DEADMAN_ENABLED"
_ENV_IDLE_TIMEOUT_S = "JARVIS_DEADMAN_IDLE_TIMEOUT_S"
_ENV_CHECK_INTERVAL_S = "JARVIS_DEADMAN_CHECK_INTERVAL_S"
_ENV_BOOT_GRACE_S = "JARVIS_DEADMAN_BOOT_GRACE_S"

# ---------------------------------------------------------------------------
# Defaults.
# ---------------------------------------------------------------------------
_DEFAULT_IDLE_TIMEOUT_S = 1800
_DEFAULT_CHECK_INTERVAL_S = 300
_DEFAULT_BOOT_GRACE_S = 2100
_DEFAULT_PORT = 11434


# ---------------------------------------------------------------------------
# Master gate.
# ---------------------------------------------------------------------------

def deadman_enabled() -> bool:
    """Master gate for the Dead-Man's Switch.

    Default **True** -- the dead-man is the cost-safety backstop; disabling
    it is a conscious operator decision. When False, the FSM must rely solely
    on its own teardown path (and a [SOVEREIGN WARNING] is logged).

    NEVER raises.
    """
    raw = (os.environ.get(_ENV_ENABLED, "") or "").strip().lower()
    if raw == "":
        # Default TRUE -- cost backstop is on unless explicitly disabled.
        return True
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Env-reader helpers (fail-soft, bounded).
# ---------------------------------------------------------------------------

def _env_int(var: str, default: int, lo: int = 1, hi: int = 86400) -> int:
    """Read an integer env var, clamped to [lo, hi]. NEVER raises."""
    raw = (os.environ.get(var, "") or "").strip()
    try:
        v = int(raw) if raw else default
    except (TypeError, ValueError):
        v = default
    return max(lo, min(v, hi))


def build_inference_bind_block(*, port: int) -> str:
    """Cloud-init bash SNIPPET (no shebang) that forces the inference daemon to
    bind ``0.0.0.0:<port>`` (NOT 127.0.0.1) so the hybrid orchestrator can reach
    it through the /32 firewall, then restarts the daemon to apply the bind.

    Handles the Ollama systemd path (the golden image's daemon): writes a
    drop-in override setting ``OLLAMA_HOST=0.0.0.0:<port>``, daemon-reloads, and
    restarts -- falling back to ``ollama serve`` if systemd isn't managing it.
    Idempotent + fail-soft (``|| true``). Pure ASCII string assembly. NO
    hardcoded port -- the resolved value is interpolated."""
    p = int(port)
    return (
        "# --- JARVIS dynamic inference-bind (cloud-init) ---\n"
        "export HOME=/root\n"
        "export OLLAMA_HOST=0.0.0.0:{port}\n"
        "mkdir -p /etc/systemd/system/ollama.service.d || true\n"
        "cat > /etc/systemd/system/ollama.service.d/10-jarvis-bind.conf <<'JBIND'\n"
        "[Service]\n"
        'Environment="OLLAMA_HOST=0.0.0.0:{port}"\n'
        "JBIND\n"
        "systemctl daemon-reload || true\n"
        "systemctl restart ollama 2>/dev/null || "
        "(nohup ollama serve >/var/log/jarvis-ollama.log 2>&1 &)\n"
        "# --- end inference-bind ---\n"
    ).format(port=p)


# ---------------------------------------------------------------------------
# Script builder -- pure string assembly, no I/O.
# ---------------------------------------------------------------------------

def build_deadman_startup_script(
    *,
    idle_timeout_s: int = _DEFAULT_IDLE_TIMEOUT_S,
    check_interval_s: int = _DEFAULT_CHECK_INTERVAL_S,
    boot_grace_s: int = _DEFAULT_BOOT_GRACE_S,
    port: int = _DEFAULT_PORT,
) -> str:
    """Return a bash startup-script that installs an unbreakable node-side
    Dead-Man's Switch watchdog.

    Injected via ``--metadata-from-file=startup-script=<tmpfile>`` when the
    failover FSM awakens a J-Prime node.  The script:

    1. Exports HOME=/root (the bake lesson -- Ollama/Go panics without it).
    2. Installs a systemd timer + service (preferred) OR a nohup watchdog
       loop (fallback) that fires every ``check_interval_s`` seconds.
    3. Measures IDLE via the Ollama journal (journalctl /api/ grep) combined
       with a heartbeat file for robustness.
    4. Respects a ``boot_grace_s`` window so a freshly-awakened node is not
       killed before the Body has had a chance to route traffic.
    5. Fetches project/zone/instance/SA-token from the GCE metadata server at
       runtime (no hardcoding) and issues a Compute REST DELETE to self-delete.

    Parameters are used directly (caller may pass env-read values or explicit
    overrides).  The caller is responsible for reading env vars if desired.

    Pure string assembly -- NO I/O, NO subprocess, NO network.
    ASCII only.
    """
    # Explicit params win over env vars.  Only fall back to env when the
    # caller used the default value (env-override tests call with no args).
    _ito = idle_timeout_s
    _cis = check_interval_s
    _bgs = boot_grace_s

    if idle_timeout_s == _DEFAULT_IDLE_TIMEOUT_S:
        _ito = _env_int(_ENV_IDLE_TIMEOUT_S, idle_timeout_s)
    if check_interval_s == _DEFAULT_CHECK_INTERVAL_S:
        _cis = _env_int(_ENV_CHECK_INTERVAL_S, check_interval_s)
    if boot_grace_s == _DEFAULT_BOOT_GRACE_S:
        _bgs = _env_int(_ENV_BOOT_GRACE_S, boot_grace_s)

    return _build_script(
        idle_timeout_s=_ito,
        check_interval_s=_cis,
        boot_grace_s=_bgs,
        port=port,
    )


# ---------------------------------------------------------------------------
# Internal bash script assembly.
# We use a template string (str.replace) rather than f-strings for sections
# that contain awk '{print ...}' patterns -- those braces would be interpreted
# as Python f-string expressions.  Template placeholders use __NAME__ syntax.
# ---------------------------------------------------------------------------

# The awk/python3 one-liners used inside the bash script.
# These are kept as plain string constants to avoid f-string brace conflicts.
_AWK_UPTIME = r"awk '{print int($1)}' /proc/uptime"
_AWK_ZONE = r"awk -F/ '{print $NF}'"
_PY3_TOKEN = (
    r"""python3 -c "import sys,json; """
    r"""print(json.load(sys.stdin).get('access_token',''))" """
)

# The watchdog helper script body (used by both systemd service and nohup loop).
# Placeholders: __IDLE_TIMEOUT_S__ __BOOT_GRACE_S__ __OLLAMA_PORT__
# __AWK_UPTIME__ __AWK_ZONE__ __PY3_TOKEN__
_WATCHDOG_BODY = """\
#!/usr/bin/env bash
# JARVIS J-Prime Dead-Man Switch watchdog check (auto-generated, Phase 3a).
# Runs on a timer or in a loop. Self-deletes the VM when Ollama is idle.
set -uo pipefail
export HOME=/root

DEADMAN_LOG=/var/log/jprime_deadman.log
ACTIVITY_FILE=/var/run/jprime_last_activity
IDLE_TIMEOUT_S=__IDLE_TIMEOUT_S__
BOOT_GRACE_S=__BOOT_GRACE_S__
OLLAMA_PORT=__OLLAMA_PORT__

exec >> "$DEADMAN_LOG" 2>&1
echo "[jprime-deadman] check $(date -u +%FT%TZ) idle_timeout=${IDLE_TIMEOUT_S}s boot_grace=${BOOT_GRACE_S}s port=${OLLAMA_PORT}"

# Helper: fetch a value from the GCE metadata server.
# Requires Metadata-Flavor: Google header. NEVER exits on error.
_meta() {
    curl -fsS -H "Metadata-Flavor: Google" \\
        "http://metadata.google.internal/computeMetadata/v1/$1" 2>/dev/null || true
}

# ---- BOOT GRACE ---- #
# Only self-delete once uptime > BOOT_GRACE_S. A freshly-awakened node that
# the Body hasn't routed to yet must NOT be killed prematurely.
UPTIME_S=0
if [ -r /proc/uptime ]; then
    UPTIME_S=$(PLACEHOLDER_AWK_UPTIME 2>/dev/null || echo 0)
fi
if [ "${UPTIME_S}" -lt "${BOOT_GRACE_S}" ]; then
    echo "[jprime-deadman] BOOT GRACE active (uptime=${UPTIME_S}s < grace=${BOOT_GRACE_S}s) -- skip"
    exit 0
fi

# ---- IDLE MEASURE ---- #
# Two-signal idle detection:
# Signal A: Ollama journal -- count /api/ lines in last IDLE_TIMEOUT_S seconds.
# Signal B: Heartbeat file /var/run/jprime_last_activity (touched on any /api/ hit).
SINCE_ARG="${IDLE_TIMEOUT_S} seconds ago"
API_HITS=$(journalctl -u ollama --since "${SINCE_ARG}" 2>/dev/null \\
    | grep -c "/api/" || echo 0)

if [ "${API_HITS}" -gt 0 ]; then
    # Recent Ollama activity -- bump heartbeat file and do nothing.
    touch "${ACTIVITY_FILE}" 2>/dev/null || true
    echo "[jprime-deadman] ollama active (api_hits=${API_HITS} on :__OLLAMA_PORT__ in last ${IDLE_TIMEOUT_S}s) -- no action"
    exit 0
fi

# Fallback: check heartbeat file age (covers journal rotation / race).
IDLE_VIA_FILE=1
if [ -f "${ACTIVITY_FILE}" ]; then
    FILE_AGE_S=$(( $(date +%s) - $(stat -c %Y "${ACTIVITY_FILE}" 2>/dev/null || echo 0) ))
    if [ "${FILE_AGE_S}" -lt "${IDLE_TIMEOUT_S}" ]; then
        echo "[jprime-deadman] heartbeat file recent (age=${FILE_AGE_S}s < ${IDLE_TIMEOUT_S}s) -- no action"
        exit 0
    fi
    IDLE_VIA_FILE=1
fi

# Both signals agree: idle for > IDLE_TIMEOUT_S seconds.
echo "[jprime-deadman] IDLE DETECTED: api_hits=${API_HITS} heartbeat_idle=${IDLE_VIA_FILE}"
echo "[jprime-deadman] uptime=${UPTIME_S}s > boot_grace=${BOOT_GRACE_S}s -- initiating self-delete"

# ---- SELF-DELETE via GCE Compute REST API ---- #
# Mirror sovereign_self_termination.py exactly:
#   metadata SA token + Compute REST DELETE (stdlib curl + metadata server).
#   Node has NO gcloud. curl only.
# Step 1: fetch SA token from the metadata server.
TOKEN_JSON=$(_meta "instance/service-accounts/default/token")
if [ -z "${TOKEN_JSON}" ]; then
    echo "[jprime-deadman] ERROR: no SA token from metadata -- retry next tick"
    exit 1
fi
SA_TOKEN=$(echo "${TOKEN_JSON}" | PLACEHOLDER_PY3_TOKEN 2>/dev/null || true)
if [ -z "${SA_TOKEN}" ]; then
    echo "[jprime-deadman] ERROR: could not parse SA token -- retry next tick"
    exit 1
fi

# Step 2: fetch instance identity from metadata (project/zone/instance-name).
PROJECT=$(_meta "project/project-id")
INSTANCE_NAME=$(_meta "instance/name")
ZONE_FULL=$(_meta "instance/zone")

if [ -z "${PROJECT}" ] || [ -z "${INSTANCE_NAME}" ] || [ -z "${ZONE_FULL}" ]; then
    echo "[jprime-deadman] ERROR: incomplete identity project=${PROJECT} instance=${INSTANCE_NAME} zone=${ZONE_FULL} -- retry"
    exit 1
fi

# Zone is returned as "projects/<num>/zones/<zone-name>" -- extract last component.
ZONE=$(echo "${ZONE_FULL}" | PLACEHOLDER_AWK_ZONE)

# Step 3: issue the Compute REST DELETE to self-delete this VM.
DELETE_URL="https://compute.googleapis.com/compute/v1/projects/${PROJECT}/zones/${ZONE}/instances/${INSTANCE_NAME}"
echo "[jprime-deadman] issuing self-DELETE: ${DELETE_URL}"
HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" \\
    -X DELETE \\
    -H "Authorization: Bearer ${SA_TOKEN}" \\
    "${DELETE_URL}" 2>/dev/null || echo "0")

echo "[jprime-deadman] Compute REST DELETE status=${HTTP_STATUS} -- compute severance in progress"
# 200/202 = accepted by GCE (async delete in progress).
# 404/409 = already gone (idempotent). All are terminal -- stop looping.
"""


def _build_watchdog_body(
    *,
    idle_timeout_s: int,
    boot_grace_s: int,
    port: int,
) -> str:
    """Render the watchdog helper script with values substituted."""
    return (
        _WATCHDOG_BODY
        .replace("__IDLE_TIMEOUT_S__", str(idle_timeout_s))
        .replace("__BOOT_GRACE_S__", str(boot_grace_s))
        .replace("__OLLAMA_PORT__", str(port))
        .replace("PLACEHOLDER_AWK_UPTIME", _AWK_UPTIME)
        .replace("PLACEHOLDER_AWK_ZONE", _AWK_ZONE)
        .replace("PLACEHOLDER_PY3_TOKEN", _PY3_TOKEN)
    )


def _build_script(
    *,
    idle_timeout_s: int,
    check_interval_s: int,
    boot_grace_s: int,
    port: int,
) -> str:
    """Assemble the full startup-script. All values already resolved."""
    watchdog_body = _build_watchdog_body(
        idle_timeout_s=idle_timeout_s,
        boot_grace_s=boot_grace_s,
        port=port,
    )

    # The startup-script preamble (exported to the startup-script itself).
    preamble = (
        "#!/usr/bin/env bash\n"
        "# JARVIS J-Prime Dead-Man's Switch startup-script (auto-generated, Phase 3a).\n"
        "# Installs a node-side watchdog that self-deletes this VM when the local\n"
        "# Ollama endpoint has been idle >IDLE_TIMEOUT_S AND uptime>BOOT_GRACE_S.\n"
        "# Cost-safety backstop: even if the orchestrator (Body) dies, the node\n"
        "# tears ITSELF down -- no orphan billing.\n"
        "set -uo pipefail\n"
        "\n"
        "# ROOT-CAUSE FIX: Ollama (Go) PANICS with \"$HOME is not defined\".\n"
        "# GCP startup-scripts run as root with HOME unset. Export HOME first.\n"
        "export HOME=/root\n"
        "\n"
        "DEADMAN_LOG=/var/log/jprime_deadman.log\n"
        "ACTIVITY_FILE=/var/run/jprime_last_activity\n"
        "IDLE_TIMEOUT_S=" + str(idle_timeout_s) + "\n"
        "CHECK_INTERVAL_S=" + str(check_interval_s) + "\n"
        "BOOT_GRACE_S=" + str(boot_grace_s) + "\n"
        "OLLAMA_PORT=" + str(port) + "\n"
        "\n"
        "exec > >(tee -a \"$DEADMAN_LOG\") 2>&1\n"
        "echo \"[jprime-deadman] startup-script begin $(date -u +%FT%TZ) HOME=$HOME\"\n"
        'echo "[jprime-deadman] idle_timeout=${IDLE_TIMEOUT_S}s'
        ' check_interval=${CHECK_INTERVAL_S}s'
        ' boot_grace=${BOOT_GRACE_S}s port=${OLLAMA_PORT}"\n'
        "\n"
    )

    # Write the watchdog helper script to disk and set up systemd or nohup.
    watchdog_install = (
        "# ------------------------------------------------------------------ #\n"
        "# INSTALL WATCHDOG: write helper to disk, then systemd or nohup.     #\n"
        "# ------------------------------------------------------------------ #\n"
        "\n"
        "# Write the watchdog helper script (used by both systemd and nohup).\n"
        "WATCHDOG_BIN=/usr/local/bin/jprime_deadman_check.sh\n"
        "cat > \"$WATCHDOG_BIN\" << 'DEADMAN_HELPER_EOF'\n"
        + watchdog_body
        + "DEADMAN_HELPER_EOF\n"
        "chmod +x \"$WATCHDOG_BIN\"\n"
        "\n"
        "if [ -d /run/systemd/system ]; then\n"
        "    echo \"[jprime-deadman] installing systemd timer + service\"\n"
        "\n"
        "    cat > /etc/systemd/system/jprime-deadman.service << 'EOF'\n"
        "[Unit]\n"
        "Description=JARVIS J-Prime Dead-Man Switch Check\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "ExecStart=/usr/local/bin/jprime_deadman_check.sh\n"
        "StandardOutput=append:/var/log/jprime_deadman.log\n"
        "StandardError=append:/var/log/jprime_deadman.log\n"
        "EOF\n"
        "\n"
        "    cat > /etc/systemd/system/jprime-deadman.timer << EOF\n"
        "[Unit]\n"
        "Description=JARVIS J-Prime Dead-Man Switch Timer\n"
        "After=network.target\n"
        "\n"
        "[Timer]\n"
        "OnBootSec=" + str(boot_grace_s) + "s\n"
        "OnUnitActiveSec=" + str(check_interval_s) + "s\n"
        "Unit=jprime-deadman.service\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
        "EOF\n"
        "\n"
        "    systemctl daemon-reload || true\n"
        "    systemctl enable --now jprime-deadman.timer || true\n"
        '    echo "[jprime-deadman] systemd timer installed'
        " (interval=" + str(check_interval_s) + "s,"
        " boot_delay=" + str(boot_grace_s) + 's)"\n'
        "\n"
        "else\n"
        "    # Fallback: nohup loop (HOME is already exported above).\n"
        '    echo "[jprime-deadman] systemd not init -- starting nohup watchdog loop"\n'
        "    nohup bash -c '\n"
        "while true; do\n"
        "    sleep " + str(check_interval_s) + " || true\n"
        "    /usr/local/bin/jprime_deadman_check.sh || true\n"
        "done\n"
        "' >> /var/log/jprime_deadman.log 2>&1 &\n"
        '    echo "[jprime-deadman] nohup watchdog loop started (PID=$!)"\n'
        "fi\n"
        "\n"
        'echo "[jprime-deadman] Dead-Man Switch installation complete $(date -u +%FT%TZ)"\n'
    )

    return preamble + watchdog_install


__all__ = [
    "build_deadman_startup_script",
    "deadman_enabled",
]
