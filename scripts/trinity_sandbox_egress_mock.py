#!/usr/bin/env python3
"""trinity_sandbox_egress_mock — synthetic egress sinkhole for the air-gapped
Trinity integration sandbox (Sovereign Cross-Repo Mutator, Guardrail 2).

This is the ONLY reachable "external" endpoint inside the `internal: true`
(air-gapped) Docker network. The sandboxed Body/Mind/Nerves containers have
their DoubleWord / Claude / GCP base URLs env-overridden to point here, so the
Trinity handshake can complete with ZERO risk of a live DW/Claude/GCP call.

Pure stdlib (no third-party deps) so it builds/runs inside the lean sandbox
image with no pip install. Deterministic synthetic responses only — it never
proxies anywhere. There is intentionally NO upstream socket in this process;
the *only* network surface it exposes is loopback/in-network on its bind port.

Routes:
  POST /v1/chat/completions   -> synthetic DoubleWord chat completion
  POST /v1/batches            -> synthetic DoubleWord batch ack
  POST /v1/messages           -> synthetic Claude (Anthropic) message
  GET  /computeMetadata/v1/*  -> synthetic GCP metadata token / value
  GET  /healthz               -> {"ok": true} liveness for the sandbox harness
  *                           -> 404 {"error": "egress_sinkhole_unmapped"}

Run standalone:
  python3 scripts/trinity_sandbox_egress_mock.py --port 8099
Env override for the bind port: TRINITY_SANDBOX_EGRESS_PORT (default 8099).
"""
from __future__ import annotations

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Tuple

_DEFAULT_PORT = 8099
_ENV_PORT = "TRINITY_SANDBOX_EGRESS_PORT"

# Marker stamped into every synthetic body so a handshake/test can prove the
# response came from the sinkhole and NOT a live provider.
SYNTHETIC_MARKER = "trinity-sandbox-egress-mock"


def synthetic_response(path: str, *, method: str = "POST") -> Tuple[int, Dict[str, Any]]:
    """Pure function: map a request path -> (status, json body).

    Exposed (and import-friendly) so the gate's unit tests can assert the
    synthetic shapes WITHOUT binding a real socket.
    """
    p = (path or "").split("?", 1)[0].rstrip("/") or "/"

    if p == "/v1/chat/completions":
        # OpenAI-compatible (DoubleWord) chat completion.
        return 200, {
            "id": "chatcmpl-" + SYNTHETIC_MARKER,
            "object": "chat.completion",
            "model": "synthetic-dw",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            "_source": SYNTHETIC_MARKER,
        }

    if p == "/v1/batches":
        # DoubleWord batch ack.
        return 200, {
            "id": "batch_" + SYNTHETIC_MARKER,
            "object": "batch",
            "status": "completed",
            "_source": SYNTHETIC_MARKER,
        }

    if p == "/v1/messages":
        # Anthropic (Claude) messages.
        return 200, {
            "id": "msg_" + SYNTHETIC_MARKER,
            "type": "message",
            "role": "assistant",
            "model": "synthetic-claude",
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "_source": SYNTHETIC_MARKER,
        }

    if p == "/healthz":
        return 200, {"ok": True, "_source": SYNTHETIC_MARKER}

    if p.startswith("/computeMetadata/v1"):
        # GCP metadata server shape: token endpoint vs scalar value.
        if p.endswith("/token"):
            return 200, {
                "access_token": "synthetic-" + SYNTHETIC_MARKER,
                "expires_in": 3600,
                "token_type": "Bearer",
                "_source": SYNTHETIC_MARKER,
            }
        return 200, {"value": SYNTHETIC_MARKER, "_source": SYNTHETIC_MARKER}

    return 404, {"error": "egress_sinkhole_unmapped", "path": p, "_source": SYNTHETIC_MARKER}


class _Handler(BaseHTTPRequestHandler):
    server_version = "TrinitySandboxEgressMock/1.0"

    def _emit(self, method: str) -> None:
        # Drain any request body so the socket is clean (we never inspect it).
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length > 0:
                self.rfile.read(length)
        except Exception:
            pass
        status, body = synthetic_response(self.path, method=method)
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except Exception:
            pass

    def do_POST(self) -> None:  # noqa: N802 (http.server contract)
        self._emit("POST")

    def do_GET(self) -> None:  # noqa: N802
        self._emit("GET")

    def log_message(self, *_args: Any) -> None:  # silence default stderr spam
        return


def serve(port: int) -> None:  # pragma: no cover - exercised only at runtime
    httpd = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


def main() -> int:  # pragma: no cover - CLI entry
    ap = argparse.ArgumentParser(description="Trinity sandbox egress sinkhole")
    ap.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get(_ENV_PORT, _DEFAULT_PORT)),
        help="bind port (default env %s or %d)" % (_ENV_PORT, _DEFAULT_PORT),
    )
    args = ap.parse_args()
    serve(args.port)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
