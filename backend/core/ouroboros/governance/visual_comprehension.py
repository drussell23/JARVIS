"""
VisualCodeComprehension — Screenshot-based code and UI analysis for Ouroboros.

Closes the "visual code comprehension" gap. Gives Ouroboros the ability
to SEE code and UI state the way Claude does when looking at screenshots.

Flow:
1. Capture screenshot (screencapture subprocess, argv-based, no shell)
2. Compress to JPEG (Pillow, bounded to 1MB)
3. Send to Claude Vision with focused analysis prompt
4. Parse structured insights (JSON)
5. Inject into CONTEXT_EXPANSION

Use cases:
  - Code structure analysis (class layout, import organization)
  - Error dialog detection (visual error messages in terminal/IDE)
  - UI regression verification (did the patch break the UI?)
  - Terminal output analysis (log patterns, stack traces)

Boundary Principle:
  Deterministic: Screenshot (argv subprocess), compression, JSON parsing.
  Agentic: Visual analysis by Claude Vision / LLaVA.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_VISION_TIMEOUT_S = float(os.environ.get("JARVIS_VISUAL_COMPREHENSION_TIMEOUT_S", "30"))
_MAX_IMAGE_BYTES = int(os.environ.get("JARVIS_VISUAL_COMPREHENSION_MAX_BYTES", "1048576"))
_CAPTURE_WIDTH = int(os.environ.get("JARVIS_VISUAL_COMPREHENSION_WIDTH", "1280"))
_CAPTURE_HEIGHT = int(os.environ.get("JARVIS_VISUAL_COMPREHENSION_HEIGHT", "800"))


@dataclass
class VisualInsight:
    """Structured insight from visual analysis."""
    category: str
    description: str
    confidence: float
    affected_elements: List[str]
    suggested_action: str
    raw_response: str = ""


@dataclass
class VisualAnalysisResult:
    """Complete result of a visual comprehension pass."""
    success: bool
    insights: List[VisualInsight]
    image_size_bytes: int
    analysis_duration_s: float
    model_used: str
    error: str = ""


class VisualCodeComprehension:
    """Screenshot-based code and UI analysis for Ouroboros.

    Captures screen state, sends to vision model, returns structured
    insights. Gives Ouroboros eyes for code layout, UI state, error
    dialogs, and visual regressions.
    """

    def __init__(
        self,
        anthropic_api_key: Optional[str] = None,
        vision_model: Optional[str] = None,
    ) -> None:
        self._api_key = anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self._vision_model = vision_model or os.environ.get(
            "JARVIS_VISUAL_COMPREHENSION_MODEL", "claude-sonnet-4-20250514"
        )

    @property
    def is_available(self) -> bool:
        return bool(self._api_key)

    async def analyze_screen(
        self, prompt: str, region: Optional[Tuple[int, int, int, int]] = None,
    ) -> VisualAnalysisResult:
        """Capture and analyze current screen state."""
        t0 = time.monotonic()
        if not self.is_available:
            return VisualAnalysisResult(
                success=False, insights=[], image_size_bytes=0,
                analysis_duration_s=0, model_used="none",
                error="No vision API key configured",
            )

        b64_image = await self._capture_screenshot(region)
        if b64_image is None:
            return VisualAnalysisResult(
                success=False, insights=[], image_size_bytes=0,
                analysis_duration_s=time.monotonic() - t0, model_used="none",
                error="Screenshot capture failed",
            )

        image_bytes = len(base64.b64decode(b64_image))
        raw_response = await self._analyze_with_vision(b64_image, prompt)
        if raw_response is None:
            return VisualAnalysisResult(
                success=False, insights=[], image_size_bytes=image_bytes,
                analysis_duration_s=time.monotonic() - t0,
                model_used=self._vision_model, error="Vision model failed",
            )

        insights = self._parse_insights(raw_response)
        elapsed = time.monotonic() - t0
        logger.info(
            "[VisualComprehension] %d insights in %.1fs (%d bytes, %s)",
            len(insights), elapsed, image_bytes, self._vision_model,
        )
        return VisualAnalysisResult(
            success=True, insights=insights, image_size_bytes=image_bytes,
            analysis_duration_s=elapsed, model_used=self._vision_model,
        )

    async def analyze_for_context(
        self, analysis_type: str = "code_structure",
    ) -> VisualAnalysisResult:
        """Analyze screen for CONTEXT_EXPANSION injection."""
        prompts = {
            "code_structure": (
                "Analyze the code visible on screen. Identify: "
                "1) Class/function organization quality, "
                "2) Anti-patterns (deep nesting, long functions, poor naming), "
                "3) Import organization, 4) Code health assessment. "
                'Return JSON: {"insights": [{"category": "...", '
                '"description": "...", "confidence": 0.0-1.0, '
                '"affected_elements": [...], "suggested_action": "..."}]}'
            ),
            "error_analysis": (
                "Identify any visible errors, warnings, or problems on screen. "
                "Check for: red/orange highlights, error messages, tracebacks, "
                "failed test output, broken UI elements. "
                "Return JSON with the same insight format."
            ),
            "ui_state": (
                "Describe the current UI state visible on screen. "
                "What application is active? What is the user looking at? "
                "Are there any anomalies, loading states, or error indicators? "
                "Return JSON with the same insight format."
            ),
            "terminal_output": (
                "Analyze the terminal output visible on screen. "
                "Identify: log patterns, error messages, warnings, stack traces, "
                "performance metrics, and any actionable information. "
                "Return JSON with the same insight format."
            ),
        }
        prompt = prompts.get(analysis_type, prompts["code_structure"])
        return await self.analyze_screen(prompt)

    def format_for_prompt(self, result: VisualAnalysisResult) -> str:
        """Format visual insights for generation prompt injection."""
        if not result.success or not result.insights:
            return ""
        lines = ["## Visual Analysis Insights"]
        for insight in result.insights:
            lines.append(
                f"- **[{insight.category}]** ({insight.confidence:.0%}): "
                f"{insight.description}"
            )
            if insight.affected_elements:
                lines.append(f"  Affected: {', '.join(insight.affected_elements[:5])}")
            if insight.suggested_action:
                lines.append(f"  Action: {insight.suggested_action}")
        lines.append(
            "\nThese insights are from visual screen analysis. "
            "Consider alongside code-level analysis."
        )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Screenshot capture (deterministic — argv subprocess, no shell)
    # ------------------------------------------------------------------

    async def _capture_screenshot(
        self, region: Optional[Tuple[int, int, int, int]] = None,
    ) -> Optional[str]:
        """Capture screenshot. Returns base64 encoded image."""
        try:
            # Try existing screen capture tool first
            try:
                from backend.core_contexts.tools.screen import capture_and_compress
                compressed = await capture_and_compress(
                    logical_screen_size=(_CAPTURE_WIDTH, _CAPTURE_HEIGHT)
                )
                if compressed is not None:
                    return compressed.b64_jpeg
            except Exception:
                pass

            # Fallback: screencapture subprocess (argv, no shell)
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                tmp_path = tmp.name

            argv = ["screencapture", "-x"]
            if region:
                x, y, w, h = region
                argv.extend(["-R", f"{x},{y},{w},{h}"])
            argv.append(tmp_path)

            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=10.0)

            tmp_file = Path(tmp_path)
            if proc.returncode != 0 or not tmp_file.exists():
                tmp_file.unlink(missing_ok=True)
                return None

            raw_bytes = tmp_file.read_bytes()
            tmp_file.unlink(missing_ok=True)

            # Compress if oversized
            if len(raw_bytes) > _MAX_IMAGE_BYTES:
                try:
                    from PIL import Image
                    img = Image.open(io.BytesIO(raw_bytes))
                    img = img.resize((_CAPTURE_WIDTH, _CAPTURE_HEIGHT), Image.LANCZOS)
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=75)
                    raw_bytes = buf.getvalue()
                except ImportError:
                    pass

            return base64.b64encode(raw_bytes).decode("ascii")

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("[VisualComprehension] Capture failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Vision model (agentic — Claude Vision API)
    # ------------------------------------------------------------------

    async def _analyze_with_vision(self, b64_image: str, prompt: str) -> Optional[str]:
        """Send screenshot to Claude Vision. Returns raw response text."""
        try:
            import anthropic
            client = anthropic.AsyncAnthropic(api_key=self._api_key)

            media_type = "image/jpeg" if b64_image[:4] == "/9j/" else "image/png"

            response = await asyncio.wait_for(
                client.messages.create(
                    model=self._vision_model,
                    max_tokens=2000,
                    messages=[{
                        "role": "user",
                        "content": [
                            {"type": "image", "source": {
                                "type": "base64", "media_type": media_type,
                                "data": b64_image,
                            }},
                            {"type": "text", "text": prompt},
                        ],
                    }],
                ),
                timeout=_VISION_TIMEOUT_S,
            )
            return response.content[0].text if response.content else None

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[VisualComprehension] Vision API: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Response parsing (deterministic — JSON extraction)
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_insights(raw: str) -> List[VisualInsight]:
        """Parse structured insights from vision response."""
        try:
            stripped = raw.strip()
            if stripped.startswith("```"):
                stripped = "\n".join(
                    l for l in stripped.split("\n") if not l.startswith("```")
                ).strip()
            data = json.loads(stripped)
            items = data.get("insights", [data] if "category" in data else [])

            return [
                VisualInsight(
                    category=item.get("category", "visual"),
                    description=item.get("description", ""),
                    confidence=float(item.get("confidence", 0.7)),
                    affected_elements=item.get("affected_elements", []),
                    suggested_action=item.get("suggested_action", ""),
                    raw_response=raw[:500],
                )
                for item in items
                if isinstance(item, dict)
            ]
        except (json.JSONDecodeError, ValueError):
            if len(raw.strip()) > 20:
                return [VisualInsight(
                    category="visual_analysis", description=raw[:500],
                    confidence=0.6, affected_elements=[],
                    suggested_action="", raw_response=raw[:500],
                )]
            return []
