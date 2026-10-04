"""Local vision model client: screenshots -> text through an OpenAI-compatible server.

The paid vision lanes (Claude, DoubleWord) are off on a local-first host, and
the J-Prime LLaVA lane assumes a GCP vision server. This client asks any
OpenAI-compatible ``/chat/completions`` endpoint that accepts ``image_url``
data URIs -- Ollama on the same host is the intended one.

Configuration (no model name is hardcoded):
  JARVIS_VISION_MODEL_NAME      model to ask; unset = client disabled.
                                Shared with InteractiveBrainRouter's vision lane.
  JARVIS_LOCAL_VISION_URL       base URL, default http://127.0.0.1:11434/v1
                                (from WSL, mirrored networking reaches Windows Ollama)
  JARVIS_LOCAL_VISION_ENABLED   "false" disables even when a model is named
  JARVIS_LOCAL_VISION_MAX_DIM   longest image side sent, default 1280
  JARVIS_LOCAL_VISION_TIMEOUT_S request timeout, default 120 (covers a cold load)

Use an INSTRUCT (non-thinking) model. Ollama's OpenAI endpoint ignores
``think``/``reasoning_effort``, so a thinking model spends the whole token
budget reasoning and returns empty content -- reported here as
``reasoning_only`` rather than as a blank answer.
"""
from __future__ import annotations

import base64
import io
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class VisionAnswer:
    ok: bool
    text: str = ""
    model: str = ""
    latency_ms: float = 0.0
    error: Optional[str] = None


def _to_jpeg(image: Any, max_dim: int) -> bytes:
    """PNG/JPEG bytes, PIL image or RGB ndarray -> JPEG bytes no larger than max_dim."""
    from PIL import Image

    if isinstance(image, (bytes, bytearray)):
        img = Image.open(io.BytesIO(image))
    elif hasattr(image, "save"):
        img = image
    else:
        img = Image.fromarray(image)
    img = img.convert("RGB")
    if max(img.size) > max_dim:
        img = img.copy()
        img.thumbnail((max_dim, max_dim))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return buf.getvalue()


class LocalVisionClient:
    def __init__(self, base_url: Optional[str] = None, model: Optional[str] = None,
                 timeout_s: Optional[float] = None, max_dim: Optional[int] = None) -> None:
        self.base_url = (base_url or os.environ.get("JARVIS_LOCAL_VISION_URL")
                         or "http://127.0.0.1:11434/v1").rstrip("/")
        self.model = model if model is not None else os.environ.get("JARVIS_VISION_MODEL_NAME", "").strip()
        self.timeout_s = timeout_s or float(os.environ.get("JARVIS_LOCAL_VISION_TIMEOUT_S", "120"))
        self.max_dim = max_dim or int(os.environ.get("JARVIS_LOCAL_VISION_MAX_DIM", "1280"))

    @property
    def enabled(self) -> bool:
        flag = os.environ.get("JARVIS_LOCAL_VISION_ENABLED", "true").strip().lower()
        return bool(self.model) and flag != "false"

    async def describe(self, image: Any, prompt: str, *, max_tokens: int = 300) -> VisionAnswer:
        """Ask the model about one image. Never raises; failures come back as ``ok=False``."""
        if not self.enabled:
            return VisionAnswer(ok=False, error="disabled")
        import aiohttp

        try:
            jpeg = _to_jpeg(image, self.max_dim)
        except Exception as e:  # noqa: BLE001 -- undecodable input
            return VisionAnswer(ok=False, model=self.model, error=f"bad_image: {e}")
        payload = {
            "model": self.model,
            "temperature": 0.1,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": [
                {"type": "image_url",
                 "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()}},
                {"type": "text", "text": prompt},
            ]}],
        }
        started = time.monotonic()
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout_s)) as s:
                async with s.post(f"{self.base_url}/chat/completions", json=payload) as r:
                    if r.status != 200:
                        body = (await r.text())[:200]
                        return VisionAnswer(ok=False, model=self.model, error=f"http_{r.status}: {body}",
                                            latency_ms=(time.monotonic() - started) * 1000)
                    data = await r.json()
        except Exception as e:  # noqa: BLE001 -- unreachable / timeout
            return VisionAnswer(ok=False, model=self.model, error=f"{type(e).__name__}: {e}",
                                latency_ms=(time.monotonic() - started) * 1000)
        latency = (time.monotonic() - started) * 1000
        msg = (data.get("choices") or [{}])[0].get("message", {})
        text = (msg.get("content") or "").strip()
        if not text:
            err = "reasoning_only" if msg.get("reasoning") else "empty_answer"
            return VisionAnswer(ok=False, model=self.model, error=err, latency_ms=latency)
        return VisionAnswer(ok=True, text=text, model=self.model, latency_ms=latency)


_client: Optional[LocalVisionClient] = None


def get_local_vision_client() -> LocalVisionClient:
    global _client
    if _client is None:
        _client = LocalVisionClient()
    return _client
