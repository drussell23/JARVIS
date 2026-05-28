#!/usr/bin/env python3
"""Slice 38 → v34 — Inline Pre-Flight Smoke Test Gate.

Operator-authorized automated hybrid:

  * Phase 1 — Bare-metal ``/v1/files`` upload via the canonical
    ``DoublewordProvider._upload_file`` composing
    ``_compose_jsonl_batch_entry``. ≤ $0.001 spend (upload-only;
    no batch creation, no inference). Single isolated call.

  * Phase 2 — Branching:
      SUCCESS  → log ``[PreflightGate] Trailing newline fix
                 accepted by Doubleword gateway. Proceeding to
                 unconstrained capability soak.`` then exec the
                 full v34 capability soak runbook
                 (``/tmp/claude/aegis_high_capital_soak.sh``).
      DEGRADED → terminate BEFORE any agent worker starts; dump
                 the multipart layout (headers + body sample +
                 composed JSONL) to
                 ``logs/diagnostics/failed_v34_upload.log`` so the
                 next-layer delta surfaces structurally.

Operator binding: no workarounds, no brute force, no shortcuts.
The gate composes the live ``DoublewordProvider`` stack — it does
NOT reach around it. The diagnostic log captures the exact bytes
the provider would have sent if a future delta exists below the
``\\n`` layer (Content-Type, purpose field, bearer header).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

# Make project root importable when invoked from any cwd.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _load_dotenv_minimal(env_path: Path) -> int:
    """Minimal .env loader — no python-dotenv dependency. Parses
    ``KEY=VALUE`` lines (ignoring comments + blanks), populates
    ``os.environ`` via ``setdefault`` so any explicit operator
    override at the shell level wins.

    Returns the count of variables loaded. The harness relies on
    the operator's shell environment already carrying credentials;
    this gatekick is a standalone smoke probe so it must do the
    minimal load itself.
    """
    if not env_path.exists():
        return 0
    loaded = 0
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        # Strip optional surrounding quotes.
        value = value.strip().strip("'").strip('"')
        if not key:
            continue
        if key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded


# Load .env before any provider imports — DoublewordProvider reads
# ``DOUBLEWORD_API_KEY`` at module-level (line 58) so this load MUST
# happen before that import.
_DOTENV_LOADED = _load_dotenv_minimal(REPO_ROOT / ".env")

# Configure root logger so the gate's structured messages surface
# even if no harness logger has been initialized.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("Ouroboros.V34Gatekick")

DIAG_DIR = REPO_ROOT / "logs" / "diagnostics"
DIAG_DIR.mkdir(parents=True, exist_ok=True)
FAILED_LOG = DIAG_DIR / "failed_v34_upload.log"
DETONATE_SCRIPT = Path("/tmp/claude/aegis_high_capital_soak.sh")

# Default model — operator topology routes STANDARD via 35B-A3B-FP8
# (the model where v33 captured 3/3 HTTP 500s). Same model = same
# upstream code path = sharpest signal.
DEFAULT_MODEL = "Qwen/Qwen3.5-35B-A3B-FP8"

# Minimal ping-style prompt — token cost negligible.
_PING_PROMPT = "ping"


def _build_smoke_entry(
    custom_id: str, model_id: str
) -> dict:
    """Build the minimal batch entry shape used by both production
    call sites (``submit_batch`` + ``prompt_only``). Required by the
    composer's signature validation."""
    return {
        "custom_id": custom_id,
        "method": "POST",
        "url": "/v1/chat/completions",
        "body": {
            "model": model_id,
            "messages": [
                {"role": "user", "content": _PING_PROMPT},
            ],
            "max_tokens": 16,
            "temperature": 0.0,
            "chat_template_kwargs": {"enable_thinking": False},
        },
    }


def _dump_failed_diagnostic(
    *,
    jsonl_content: str,
    file_id: Optional[str],
    last_error_status: int,
    model_id: str,
    custom_id: str,
    elapsed_s: float,
    extra: Optional[dict] = None,
) -> None:
    """Capture the exact provider-composed multipart layout +
    response metadata so the next-layer delta is structurally
    surfaceable from a single log file."""
    diag = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "slice": 38,
        "phase": "preflight_gate",
        "verdict": "degraded",
        "model_id": model_id,
        "custom_id": custom_id,
        "elapsed_s": elapsed_s,
        "_upload_file": {
            "return": file_id,
            "_last_error_status": last_error_status,
        },
        "composed_jsonl": {
            "bytes": len(jsonl_content.encode("utf-8")),
            "ends_with_newline": jsonl_content.endswith("\n"),
            "last_4_bytes_repr": repr(jsonl_content[-4:]),
            # First 500 chars only — full payload may contain
            # user prompt; the structural shape is in the
            # opening keys.
            "first_500_chars": jsonl_content[:500],
        },
        "expected_multipart_layout": {
            # Mirrors _upload_file's aiohttp.FormData construction.
            "field_1": {
                "name": "file",
                "filename": "batch_input.jsonl",
                "content_type": "application/jsonl",
                "content_source": "io.BytesIO(jsonl_content.encode())",
            },
            "field_2": {
                "name": "purpose",
                "value": "batch",
            },
        },
        "next_layer_suspects": [
            "Content-Type on the file field "
            "(application/jsonl vs application/x-ndjson "
            "vs application/json)",
            "purpose field value (batch vs other)",
            "field ordering (file vs purpose first)",
            "Aegis bearer header set on /v1/files specifically",
            "filename extension (.jsonl vs .ndjson)",
            "JSONL content schema (custom_id length, "
            "method, url path)",
        ],
        "extra": extra or {},
    }
    FAILED_LOG.write_text(json.dumps(diag, indent=2, sort_keys=True))
    logger.error(
        "[PreflightGate] DEGRADED — diagnostic dump written: %s",
        FAILED_LOG,
    )


async def _run_smoke() -> Tuple[bool, dict]:
    """Execute one isolated /v1/files upload via the canonical
    provider path. Returns (success, telemetry_dict)."""
    from backend.core.ouroboros.governance.doubleword_provider import (
        DoublewordProvider,
    )

    model_id = os.environ.get(
        "JARVIS_V34_GATEKICK_MODEL", DEFAULT_MODEL,
    )
    custom_id = f"v34-gatekick-{int(time.time())}"

    # Compose JSONL via Slice 38 canonical helper — proves the
    # composer wires through to _upload_file end-to-end.
    entry = _build_smoke_entry(custom_id, model_id)
    jsonl_content = DoublewordProvider._compose_jsonl_batch_entry(entry)
    logger.info(
        "[PreflightGate] composed JSONL: bytes=%d ends_nl=%s "
        "custom_id=%s model=%s",
        len(jsonl_content.encode("utf-8")),
        jsonl_content.endswith("\n"),
        custom_id, model_id,
    )

    # Provider instance — Aegis-enabled topology comes from env,
    # not from arguments. No magic globals; the provider reads its
    # own credential surface at __init__.
    provider = DoublewordProvider()
    if not provider.is_available:
        logger.error(
            "[PreflightGate] DoublewordProvider not available "
            "(no API key + Aegis disabled). Cannot smoke-test."
        )
        return False, {
            "reason": "provider_unavailable",
            "is_available": False,
        }

    t0 = time.monotonic()
    try:
        file_id = await provider._upload_file(
            jsonl_content, op_id=custom_id,
        )
    except Exception as exc:
        elapsed = time.monotonic() - t0
        logger.exception(
            "[PreflightGate] _upload_file raised: %s",
            type(exc).__name__,
        )
        _dump_failed_diagnostic(
            jsonl_content=jsonl_content,
            file_id=None,
            last_error_status=getattr(
                provider, "_last_error_status", -1,
            ),
            model_id=model_id,
            custom_id=custom_id,
            elapsed_s=elapsed,
            extra={"exception": f"{type(exc).__name__}: {exc}"},
        )
        return False, {
            "reason": "upload_raised",
            "exception": str(exc),
        }
    elapsed = time.monotonic() - t0

    last_status = getattr(provider, "_last_error_status", 0) or 0

    if file_id and last_status < 300:
        logger.info(
            "[PreflightGate] Trailing newline fix accepted by "
            "Doubleword gateway. Proceeding to unconstrained "
            "capability soak. file_id=%s elapsed=%.2fs",
            file_id, elapsed,
        )
        return True, {
            "file_id": file_id,
            "elapsed_s": elapsed,
            "status": last_status,
        }

    logger.error(
        "[PreflightGate] DEGRADED — file_id=%s _last_error_status=%s "
        "elapsed=%.2fs",
        file_id, last_status, elapsed,
    )
    _dump_failed_diagnostic(
        jsonl_content=jsonl_content,
        file_id=file_id,
        last_error_status=last_status,
        model_id=model_id,
        custom_id=custom_id,
        elapsed_s=elapsed,
    )
    return False, {
        "reason": "upload_failed",
        "file_id": file_id,
        "status": last_status,
    }


def _detonate_v34() -> int:
    """Launch the operator-authorized v34 capability runbook
    in a fresh process. Returns exit code."""
    if not DETONATE_SCRIPT.exists():
        logger.error(
            "[PreflightGate] detonation script missing: %s",
            DETONATE_SCRIPT,
        )
        return 2
    logger.info(
        "[PreflightGate] Triggering v34 capability soak: %s",
        DETONATE_SCRIPT,
    )
    # exec — replace the current process so the soak inherits
    # the gate's clean env. The soak script handles its own
    # backgrounding via the harness.
    os.execv(str(DETONATE_SCRIPT), [str(DETONATE_SCRIPT)])
    # Unreachable
    return 0


async def _main() -> int:
    logger.info(
        "[PreflightGate] Slice 38 → v34 inline smoke test starting "
        "(.env vars loaded=%d)", _DOTENV_LOADED,
    )
    success, telemetry = await _run_smoke()
    logger.info(
        "[PreflightGate] smoke complete: success=%s telemetry=%s",
        success, json.dumps(telemetry, sort_keys=True),
    )

    if not success:
        logger.error(
            "[PreflightGate] HALTING — v34 capability soak NOT "
            "initiated. Review %s for next-layer delta.",
            FAILED_LOG,
        )
        return 1

    auto_detonate = os.environ.get(
        "JARVIS_V34_GATEKICK_AUTO_DETONATE", "1",
    ).lower() not in ("0", "false", "no")
    if not auto_detonate:
        logger.info(
            "[PreflightGate] auto-detonate disabled "
            "(JARVIS_V34_GATEKICK_AUTO_DETONATE=0) — exit 0"
        )
        return 0

    return _detonate_v34()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
