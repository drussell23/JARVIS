"""
JARVIS Neural Mesh — VisualBrowserAgent

Drives Chrome via Playwright with a J-Prime vision-action loop.

Architecture:
  1. Launch visible Chrome (headless=False) or connect via CDP
     (BROWSE_CDP_URL env var).
  2. Navigate to the target URL.
  3. Screenshot the current page via page.screenshot().
  4. Send screenshot + goal to J-Prime vision server for next action.
  5. Execute the action: click, fill, type, press, scroll, navigate.
  6. Verify the action succeeded via a post-action screenshot + vision check.
  7. Repeat until goal achieved or max steps exhausted.
  8. If J-Prime is unavailable, fall back to Claude API (paid tier).

All configuration is env-var driven — nothing is hard-coded.

Agent identity:
  name:  visual_browser_agent
  type:  autonomy
  caps:  {visual_browser, browse_and_interact}

Additional env vars:
  JARVIS_VISION_VERIFY_ACTIONS      — enable post-action verification (default "true")
  JARVIS_VISION_VERIFY_MAX_RETRIES  — verification retry limit per action (default "2")
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from ..base.base_neural_mesh_agent import BaseNeuralMeshAgent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Env-var helpers (consistent with browsing_agent.py convention)
# ---------------------------------------------------------------------------

def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except (ValueError, TypeError):
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)))
    except (ValueError, TypeError):
        return default


def _env_bool(key: str, default: bool) -> bool:
    val = os.environ.get(key, "").lower()
    if val in ("true", "1", "yes"):
        return True
    if val in ("false", "0", "no"):
        return False
    return default


# ---------------------------------------------------------------------------
# Action schema returned by vision model
# ---------------------------------------------------------------------------

_VALID_ACTION_TYPES = frozenset(
    {"click", "fill", "type", "press", "scroll", "navigate", "done"}
)

_SYSTEM_PROMPT = (
    "You are an autonomous browser agent. "
    "You will be shown a screenshot of a web page along with a goal to accomplish. "
    "Respond with a single JSON object (no markdown fences) describing the NEXT "
    "atomic action to take, or signal completion.\n\n"
    "JSON schema:\n"
    "{\n"
    '  "done": <bool>,          // true when the goal is fully achieved\n'
    '  "action_type": <str>,    // one of: click|fill|type|press|scroll|navigate\n'
    '  "detail": {              // action-specific parameters\n'
    '    "element": <str>,      // PREFERRED for click: visible label/name of the element\n'
    '    "role": <str>,         // PREFERRED for click: ARIA role (button, link, textbox, etc.)\n'
    '    "text": <str>,         // for click: visible text content. For fill/type: text to enter\n'
    '    "near_text": <str>,    // for click: text near the target element (disambiguation)\n'
    '    "x": <int>,            // FALLBACK for click: pixel coordinates (use only if element/text unavailable)\n'
    '    "y": <int>,\n'
    '    "selector": <str>,     // CSS selector for fill\n'
    '    "key": <str>,          // for press (e.g. "Enter", "Tab")\n'
    '    "delta": <int>,        // for scroll (positive = down)\n'
    '    "url": <str>           // for navigate\n'
    "  },\n"
    '  "message": <str>         // brief explanation of what you\'re doing\n'
    "}\n\n"
    "IMPORTANT: For click actions, ALWAYS prefer 'element' + 'role' over pixel coordinates. "
    "The element name and role allow precise accessibility-based clicking. "
    "Only use x/y pixel coordinates as a last resort when the element has no visible label.\n\n"
    "Only include relevant keys in 'detail'. "
    "When done=true, action_type and detail are ignored."
)


# ---------------------------------------------------------------------------
# VisualBrowserAgent
# ---------------------------------------------------------------------------

class VisualBrowserAgent(BaseNeuralMeshAgent):
    """
    Autonomous browser agent driven by a J-Prime / Claude vision loop.

    The browser launches lazily on the first task so startup cost is zero
    for workloads that never need browser control.
    """

    def __init__(self) -> None:
        super().__init__(
            agent_name="visual_browser_agent",
            agent_type="autonomy",
            capabilities={"visual_browser", "browse_and_interact"},
            description=(
                "Drives Chrome via Playwright with J-Prime vision guidance "
                "to autonomously accomplish browser-based goals."
            ),
        )

        # --- Runtime config (all env-var driven) ---
        self._max_steps: int = _env_int("JARVIS_BROWSER_CONTROL_MAX_STEPS", 15)
        self._step_delay: float = _env_float("JARVIS_BROWSER_CONTROL_STEP_DELAY", 1.5)
        self._nav_timeout: int = _env_int("JARVIS_BROWSER_NAV_TIMEOUT_MS", 20000)
        self._screenshot_quality: int = _env_int("JARVIS_BROWSER_SCREENSHOT_QUALITY", 80)
        self._vision_timeout: float = _env_float("JARVIS_BROWSER_VISION_TIMEOUT_S", 60.0)

        # --- Playwright state (all lazy) ---
        self._playwright: Optional[Any] = None
        self._browser: Optional[Any] = None
        self._browser_lock: Optional[asyncio.Lock] = None

        # --- Clients (lazy) ---
        self._prime_client: Optional[Any] = None
        self._claude_client: Optional[Any] = None
        from backend.core.interactive_brain_router import get_interactive_brain_router
        _selection = get_interactive_brain_router().select_for_task("browser_navigation")
        self._claude_model: str = os.getenv("JARVIS_BROWSER_CLAUDE_MODEL") or _selection.claude_model

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def on_initialize(self, **kwargs: Any) -> None:
        """No-op — browser and clients are launched lazily on first task."""
        logger.info("[VBA] VisualBrowserAgent ready (lazy init)")

    async def get_capability_manifest(self):  # type: ignore[override]
        """Return a CapabilityManifest declaring browser-driving capabilities."""
        from ..data_models import CapabilityManifest

        return CapabilityManifest(
            agent_name=self.agent_name,
            agent_type=self.agent_type,
            capabilities=set(self.capabilities),
            supported_apps=["Google Chrome", "Chrome", "Chromium"],
            supported_url_patterns=["*"],  # can navigate to any URL
            supported_task_types=[
                "browser_navigation",
                "vision_action",
                "vision_verification",
                "visual_browser",
            ],
            metadata={
                "version": self.version,
                "backend": self.backend,
                "engine": "playwright",
            },
        )

    async def on_stop(self) -> None:
        """Graceful cleanup: close browser and playwright."""
        await self.cleanup()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def _resolve_url(
        self, provider: str, search_query: str, goal: str,
    ) -> str:
        """Resolve a URL from structured intent fields.

        The agent owns URL knowledge — the classifier only provides
        (provider, search_query).  Unknown providers fall back to
        J-Prime synthesis or a generic search.
        """
        from urllib.parse import quote_plus

        if not provider:
            return ""

        q = quote_plus(search_query) if search_query else ""

        # Ask J-Prime for unknown providers (future: query a provider registry)
        # For now, the agent has built-in knowledge for common providers.
        if provider == "youtube":
            return f"https://www.youtube.com/results?search_query={q}" if q else "https://www.youtube.com"
        if provider == "google":
            return f"https://www.google.com/search?q={q}" if q else "https://www.google.com"
        if provider == "reddit":
            return f"https://www.reddit.com/search/?q={q}" if q else "https://www.reddit.com"
        if provider == "github":
            return f"https://github.com/search?q={q}" if q else "https://github.com"
        if provider == "linkedin":
            return f"https://www.linkedin.com/search/results/all/?keywords={q}" if q else "https://www.linkedin.com"
        if provider == "twitter":
            return f"https://twitter.com/search?q={q}" if q else "https://twitter.com"
        if provider == "amazon":
            return f"https://www.amazon.com/s?k={q}" if q else "https://www.amazon.com"

        # Unknown provider — try J-Prime for URL resolution
        try:
            from backend.core.prime_client import get_prime_client
            prime = get_prime_client()
            if prime is not None:
                resp = await prime.generate(
                    prompt=(
                        f"What is the search URL for the website '{provider}'? "
                        f"Return ONLY the URL template with {{q}} as the query placeholder. "
                        f"Example: https://www.youtube.com/results?search_query={{q}}"
                    ),
                    system_prompt="Return only a URL. No explanation.",
                    max_tokens=128,
                    temperature=0.0,
                )
                url_text = resp.content.strip()
                if url_text.startswith("http"):
                    resolved = url_text.replace("{q}", q) if q else url_text.split("?")[0]
                    logger.info("[VBA] J-Prime resolved URL for provider '%s': %s", provider, resolved)
                    return resolved
        except Exception as exc:
            logger.debug("[VBA] J-Prime URL resolution failed for '%s': %s", provider, exc)

        # Last resort: google search for the provider
        if q:
            return f"https://www.google.com/search?q={quote_plus(f'{provider} {search_query}')}"
        return f"https://www.google.com/search?q={quote_plus(provider)}"

    async def execute_task(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute a browse_and_interact task.

        Accepts structured payload from TaskEnvelope:
          goal          (str) — natural-language goal
          provider      (str) — "youtube", "google", etc. (optional)
          search_query  (str) — extracted search term (optional)
          url           (str) — explicit URL if user provided one (optional)

        The agent resolves URLs from (provider, search_query) — the classifier
        and orchestrator never hardcode URLs.
        """
        url: str = payload.get("url", "").strip()
        goal: str = payload.get("goal", "").strip()
        provider: str = payload.get("provider", "").strip()
        search_query: str = payload.get("search_query", "").strip()

        # Resolve URL from structured fields if not explicitly provided
        if not url and provider:
            url = await self._resolve_url(provider, search_query, goal)
            logger.info("[VBA] Resolved URL from provider=%s, query=%s → %s", provider, search_query, url)

        if not url and not goal:
            return {
                "success": False,
                "error": "Either 'url' or 'goal' must be provided",
                "goal": goal,
                "url": url,
                "steps_taken": 0,
                "actions": [],
                "final_message": "Task rejected: no url or goal",
            }

        # Normalise URL scheme
        if url and not url.startswith(("http://", "https://")):
            url = f"https://{url}"

        # Ensure browser is ready
        try:
            await self._ensure_browser()
        except Exception as exc:
            logger.error("[VBA] Browser launch failed: %s", exc)
            return {
                "success": False,
                "error": f"Browser launch failed: {exc}",
                "goal": goal,
                "url": url,
                "steps_taken": 0,
                "actions": [],
                "final_message": "Browser could not be initialised",
            }

        # Each task gets its own isolated browser context + page
        context = None
        try:
            context = await self._browser.new_context(
                viewport={"width": 1280, "height": 800},
                locale=os.getenv("JARVIS_BROWSER_LOCALE", "en-US"),
            )
            context.set_default_timeout(self._nav_timeout)
            context.set_default_navigation_timeout(self._nav_timeout)
            page = await context.new_page()

            # Navigate to starting URL if provided
            if url:
                try:
                    await page.goto(url, wait_until="domcontentloaded")
                    logger.info("[VBA] Navigated to %s", url)
                except Exception as nav_err:
                    logger.warning("[VBA] Initial navigation failed: %s", nav_err)

            current_url = page.url or url
            if not goal:
                goal = f"Navigate to {url} and report what you see"

            # Vision-action loop
            return await self._vision_action_loop(page, goal, current_url)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("[VBA] execute_task error: %s", exc)
            return {
                "success": False,
                "error": str(exc),
                "goal": goal,
                "url": url,
                "steps_taken": 0,
                "actions": [],
                "final_message": f"Unexpected error: {exc}",
            }
        finally:
            if context is not None:
                try:
                    await context.close()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Vision-action loop
    # ------------------------------------------------------------------

    async def _vision_action_loop(
        self,
        page: Any,
        goal: str,
        initial_url: str,
    ) -> Dict[str, Any]:
        """Core loop: screenshot → vision model → execute → repeat."""
        actions_taken: List[Dict[str, Any]] = []
        step = 0
        final_message = "Max steps reached without completing goal"

        while step < self._max_steps:
            step += 1

            # 1. Screenshot
            try:
                screenshot_bytes = await page.screenshot(
                    type="jpeg",
                    quality=self._screenshot_quality,
                    full_page=False,
                )
            except Exception as ss_err:
                logger.warning("[VBA] Screenshot failed on step %d: %s", step, ss_err)
                final_message = f"Screenshot failed: {ss_err}"
                break

            # Compress if over 4MB (Claude limit is 5MB, J-Prime works best under 2MB)
            if len(screenshot_bytes) > 4 * 1024 * 1024:
                try:
                    from PIL import Image
                    import io
                    img = Image.open(io.BytesIO(screenshot_bytes))
                    max_dim = int(os.getenv("JARVIS_SCREENSHOT_MAX_DIM", "1536"))
                    if max(img.size) > max_dim:
                        img.thumbnail((max_dim, max_dim), Image.LANCZOS)
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=70)
                    screenshot_bytes = buf.getvalue()
                except Exception:
                    pass  # Use original if compression fails

            screenshot_b64 = base64.b64encode(screenshot_bytes).decode("ascii")
            current_url = page.url

            # 2. Ask vision model for next action
            try:
                decision = await asyncio.wait_for(
                    self._ask_vision_model(
                        screenshot_b64, goal, current_url, actions_taken
                    ),
                    timeout=self._vision_timeout,
                )
            except asyncio.TimeoutError:
                logger.warning("[VBA] Vision model timed out on step %d", step)
                final_message = "Vision model timed out"
                break
            except Exception as vm_err:
                logger.warning("[VBA] Vision model error on step %d: %s", step, vm_err)
                final_message = f"Vision model error: {vm_err}"
                break

            action_type = decision.get("action_type", "")
            detail = decision.get("detail", {})
            message = decision.get("message", "")

            logger.info(
                "[VBA] Step %d/%d — action=%s message=%s",
                step, self._max_steps, action_type, message,
            )

            actions_taken.append({
                "step": step,
                "action_type": action_type,
                "detail": detail,
                "message": message,
                "url": current_url,
            })

            # 3. Check completion
            if decision.get("done"):
                final_message = message or "Goal achieved"
                return {
                    "success": True,
                    "goal": goal,
                    "url": current_url,
                    "steps_taken": step,
                    "actions": actions_taken,
                    "final_message": final_message,
                }

            # 4. Execute action
            try:
                await self._execute_action(page, action_type, detail)
            except Exception as act_err:
                logger.warning(
                    "[VBA] Action %r failed on step %d: %s",
                    action_type, step, act_err,
                )
                actions_taken[-1]["error"] = str(act_err)

            # 5. Post-action verification
            if _env_bool("JARVIS_VISION_VERIFY_ACTIONS", True):
                max_retries = _env_int("JARVIS_VISION_VERIFY_MAX_RETRIES", 2)
                verified = await self._verify_action(
                    page=page,
                    action_description=message,
                    expected_result=goal,
                    max_retries=max_retries,
                )
                if not verified:
                    logger.warning(
                        "[VBA] Action verification failed on step %d: %s",
                        step, message,
                    )
                    actions_taken[-1]["verified"] = False
                else:
                    actions_taken[-1]["verified"] = True

            # 6. Delay so the user can see what's happening
            if self._step_delay > 0:
                await asyncio.sleep(self._step_delay)

        return {
            "success": False,
            "goal": goal,
            "url": page.url if page else initial_url,
            "steps_taken": step,
            "actions": actions_taken,
            "final_message": final_message,
        }

    # ------------------------------------------------------------------
    # Action dispatcher
    # ------------------------------------------------------------------

    async def _execute_action(
        self, page: Any, action_type: str, detail: Dict[str, Any]
    ) -> None:
        """Dispatch a single Playwright action.

        For click actions, uses a 3-tier fallback chain:
            1. Accessibility: ARIA role/name selector (most precise, no coords needed)
            2. Text content: Playwright text selector (finds visible text)
            3. Pixel coordinates: raw mouse.click(x, y) (least precise, vision-only)

        The vision model can return either:
            - {"element": "Search", "role": "button"} → accessibility path
            - {"x": 450, "y": 320} → pixel path (legacy fallback)
            - {"text": "Download"} → text selector path
        """
        if action_type == "click":
            clicked = False

            # Tier 1: Accessibility selector (ARIA role + name)
            element_name = detail.get("element", "")
            element_role = detail.get("role", "")
            near_text = detail.get("near_text", "")
            if element_name and not clicked:
                try:
                    # Playwright ARIA selector: role[name="..."]
                    if element_role:
                        selector = f'role={element_role}[name="{element_name}"]'
                    else:
                        selector = f'role=button[name="{element_name}"]'
                    locator = page.locator(selector).first
                    if await locator.count() > 0:
                        await locator.click(timeout=5000)
                        clicked = True
                        logger.info("[VBA] Click via ARIA: %s (role=%s)", element_name, element_role)
                except Exception as ax_err:
                    logger.debug("[VBA] ARIA click failed for %r: %s", element_name, ax_err)

            # Tier 1b: macOS AccessibilityResolver for native browser chrome
            if not clicked and element_name:
                try:
                    from backend.neural_mesh.agents.accessibility_resolver import (
                        get_accessibility_resolver,
                    )
                    resolver = get_accessibility_resolver()
                    coords = await resolver.resolve(
                        description=element_name,
                        app_name="Google Chrome",
                        role=element_role or None,
                        near_text=near_text or None,
                    )
                    if coords:
                        await page.mouse.click(coords["x"], coords["y"])
                        clicked = True
                        logger.info(
                            "[VBA] Click via AccessibilityResolver: %s at (%d, %d)",
                            element_name, coords["x"], coords["y"],
                        )
                except ImportError:
                    pass
                except Exception as resolver_err:
                    logger.debug("[VBA] AccessibilityResolver failed: %s", resolver_err)

            # Tier 2: Text content selector
            text_content = detail.get("text", "")
            if not clicked and text_content:
                try:
                    locator = page.get_by_text(text_content, exact=False).first
                    if await locator.count() > 0:
                        await locator.click(timeout=5000)
                        clicked = True
                        logger.info("[VBA] Click via text: %r", text_content)
                except Exception as text_err:
                    logger.debug("[VBA] Text click failed for %r: %s", text_content, text_err)

            # Tier 3: Pixel coordinates (vision-only fallback)
            if not clicked:
                x = int(detail.get("x", 0))
                y = int(detail.get("y", 0))
                if x > 0 and y > 0:
                    await page.mouse.click(x, y)
                    clicked = True
                    logger.info("[VBA] Click via pixel: (%d, %d)", x, y)

            if not clicked:
                logger.warning("[VBA] Click failed — no valid selector or coordinates in: %s", detail)

        elif action_type == "fill":
            selector: str = detail.get("selector", "")
            text: str = detail.get("text", "")
            if selector:
                await page.fill(selector, text)
            else:
                await page.keyboard.type(text, delay=50)

        elif action_type == "type":
            text = detail.get("text", "")
            await page.keyboard.type(text, delay=50)

        elif action_type == "press":
            key: str = detail.get("key", "")
            if key:
                await page.keyboard.press(key)

        elif action_type == "scroll":
            delta = int(detail.get("delta", 300))
            await page.mouse.wheel(0, delta)

        elif action_type == "navigate":
            nav_url: str = detail.get("url", "")
            if nav_url:
                if not nav_url.startswith(("http://", "https://")):
                    nav_url = f"https://{nav_url}"
                await page.goto(nav_url, wait_until="domcontentloaded")

        else:
            logger.debug("[VBA] Unknown action_type=%r — skipping", action_type)

    # ------------------------------------------------------------------
    # Vision model (J-Prime first, Claude fallback)
    # ------------------------------------------------------------------

    async def _ask_vision_model(
        self,
        screenshot_b64: str,
        goal: str,
        url: str,
        previous_actions: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Ask the vision model for the next action.

        Tries J-Prime vision server first (free, local).
        Falls back to Claude API if J-Prime is unavailable.

        Returns a dict matching the action schema defined in _SYSTEM_PROMPT.
        """
        user_message = self._build_user_message(goal, url, previous_actions)

        # --- Attempt 1: J-Prime vision server ---
        try:
            response_text = await self._ask_jprime(screenshot_b64, user_message)
            if response_text:
                return self._parse_vision_response(response_text)
        except Exception as jprime_err:
            logger.debug("[VBA] J-Prime vision unavailable: %s", jprime_err)

        # --- Attempt 2: Claude API fallback ---
        try:
            response_text = await self._ask_claude(screenshot_b64, user_message)
            if response_text:
                return self._parse_vision_response(response_text)
        except Exception as claude_err:
            logger.warning("[VBA] Claude API fallback also failed: %s", claude_err)

        # Hard failure — stop the loop gracefully
        return {"done": True, "action_type": "done", "detail": {}, "message": "No vision model available"}

    async def _ask_jprime(self, screenshot_b64: str, user_message: str) -> Optional[str]:
        """Send vision request to J-Prime server."""
        # Ensure GPU VM is running (starts on-demand if needed)
        try:
            from .vision_gpu_lifecycle import ensure_vision_available
            await ensure_vision_available()
        except Exception:
            pass  # Best-effort — will fall through to Claude if J-Prime unavailable

        if self._prime_client is None:
            try:
                from backend.core.prime_client import get_prime_client
                self._prime_client = await asyncio.wait_for(
                    get_prime_client(), timeout=5.0
                )
            except Exception as e:
                raise RuntimeError(f"PrimeClient unavailable: {e}") from e

        # send_vision_request includes the system prompt in the combined prompt
        combined_prompt = f"{_SYSTEM_PROMPT}\n\n{user_message}"
        response = await self._prime_client.send_vision_request(
            image_base64=screenshot_b64,
            prompt=combined_prompt,
            max_tokens=512,
            temperature=0.1,
            timeout=self._vision_timeout,
        )
        return response.content if response else None

    async def _ask_claude(self, screenshot_b64: str, user_message: str) -> Optional[str]:
        """Send vision request to Claude API (paid fallback)."""
        if self._claude_client is None:
            api_key = os.getenv("ANTHROPIC_API_KEY")
            if not api_key:
                raise RuntimeError("ANTHROPIC_API_KEY not set")
            try:
                import anthropic
                self._claude_client = anthropic.AsyncAnthropic(api_key=api_key)
            except ImportError as ie:
                raise RuntimeError("anthropic package not installed") from ie

        response = await self._claude_client.messages.create(
            model=self._claude_model,
            max_tokens=512,
            system=_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": screenshot_b64,
                            },
                        },
                        {"type": "text", "text": user_message},
                    ],
                }
            ],
        )
        return response.content[0].text if response.content else None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_user_message(
        goal: str, url: str, previous_actions: List[Dict[str, Any]]
    ) -> str:
        """Build the user-facing prompt for the vision model."""
        lines = [
            f"Goal: {goal}",
            f"Current URL: {url}",
        ]
        if previous_actions:
            lines.append(f"Steps taken so far: {len(previous_actions)}")
            last = previous_actions[-1]
            lines.append(
                f"Last action: {last.get('action_type')} — {last.get('message', '')}"
            )
        lines.append(
            "\nLook at the screenshot and respond with the next action JSON."
        )
        return "\n".join(lines)

    @staticmethod
    def _parse_vision_response(text: str) -> Dict[str, Any]:
        """
        Parse JSON from the vision model response.

        Handles responses wrapped in markdown code fences and extracts
        the first JSON object found.  Falls back to a safe 'done' signal
        if parsing fails entirely.
        """
        # Strip markdown fences
        cleaned = text.strip()
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            # Drop first and last fence lines
            inner = "\n".join(
                line for line in lines
                if not line.startswith("```")
            )
            cleaned = inner.strip()

        # Attempt direct parse
        try:
            data = json.loads(cleaned)
            if isinstance(data, dict):
                # Normalise action_type
                if data.get("action_type") not in _VALID_ACTION_TYPES:
                    data["action_type"] = "done"
                return data
        except json.JSONDecodeError:
            pass

        # Attempt to extract first {...} block
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                data = json.loads(cleaned[start : end + 1])
                if isinstance(data, dict):
                    if data.get("action_type") not in _VALID_ACTION_TYPES:
                        data["action_type"] = "done"
                    return data
            except json.JSONDecodeError:
                pass

        logger.warning("[VBA] Could not parse vision response: %r", text[:200])
        return {
            "done": True,
            "action_type": "done",
            "detail": {},
            "message": f"Could not parse model response: {text[:100]}",
        }

    # ------------------------------------------------------------------
    # Browser lifecycle
    # ------------------------------------------------------------------

    async def _get_browser_lock(self) -> asyncio.Lock:
        if self._browser_lock is None:
            self._browser_lock = asyncio.Lock()
        return self._browser_lock

    async def _ensure_browser(self) -> None:
        """
        Lazy browser initialisation.

        Connection order:
          1. CDP URL from BROWSE_CDP_URL env var
          2. Launch Chromium with headless=False (channel="chrome" prefers
             installed Chrome; falls back to Chromium download if absent)
        """
        lock = await self._get_browser_lock()
        async with lock:
            if self._browser is not None:
                return  # Already initialised

            try:
                from playwright.async_api import async_playwright
            except ImportError as ie:
                raise RuntimeError(
                    "Playwright is not installed. "
                    "Run: pip install playwright && python -m playwright install chromium"
                ) from ie

            self._playwright = await async_playwright().start()

            cdp_url = os.getenv("BROWSE_CDP_URL", "").strip()
            if cdp_url:
                # Connect to already-running Chrome via CDP
                try:
                    self._browser = await self._playwright.chromium.connect_over_cdp(
                        cdp_url, timeout=10_000
                    )
                    logger.info("[VBA] Connected to Chrome via CDP: %s", cdp_url)
                    return
                except Exception as cdp_err:
                    logger.warning(
                        "[VBA] CDP connection to %s failed (%s); launching new Chrome",
                        cdp_url, cdp_err,
                    )

            # Launch visible Chrome (channel="chrome" picks up installed Google Chrome)
            launch_kwargs: Dict[str, Any] = {
                "headless": False,
                "args": [
                    "--disable-gpu-sandbox",
                    "--disable-setuid-sandbox",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
            }

            extra_args_raw = os.getenv("JARVIS_BROWSER_EXTRA_ARGS", "")
            if extra_args_raw:
                launch_kwargs["args"].extend(
                    a.strip() for a in extra_args_raw.split(",") if a.strip()
                )

            # Prefer installed Google Chrome; fall back to Playwright Chromium
            try:
                self._browser = await self._playwright.chromium.launch(
                    channel="chrome", **launch_kwargs
                )
                logger.info("[VBA] Launched Google Chrome (visible)")
            except Exception:
                # channel="chrome" requires Google Chrome to be installed
                self._browser = await self._playwright.chromium.launch(**launch_kwargs)
                logger.info("[VBA] Launched Playwright Chromium (visible, Chrome not found)")

    async def cleanup(self) -> None:
        """Close browser and Playwright gracefully."""
        lock = await self._get_browser_lock()
        async with lock:
            if self._browser is not None:
                try:
                    await self._browser.close()
                except Exception:
                    pass
                self._browser = None

            if self._playwright is not None:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None

        logger.info("[VBA] Playwright browser closed")

    # ------------------------------------------------------------------
    # Post-action verification
    # ------------------------------------------------------------------

    async def _verify_action(
        self,
        page: Any,
        action_description: str,
        expected_result: str,
        max_retries: int = 2,
    ) -> bool:
        """Verify that the previous browser action succeeded.

        Takes a post-action page screenshot and asks the vision model whether
        the action had the intended effect.  Falls back to True when no model
        is reachable so the main loop is never blocked.

        Args:
            page:               Playwright Page object to capture the screenshot from.
            action_description: Human-readable description of the action taken.
            expected_result:    The overall goal being worked toward.
            max_retries:        Additional attempts after the initial check.

        Returns:
            True when verified (or no model available), False after all retries fail.
        """
        await asyncio.sleep(0.5)  # Wait for browser UI to settle

        import re as _re

        verify_prompt = (
            f"I just performed this browser action: {action_description}\n"
            f"The overall goal is: {expected_result}\n\n"
            f"Look at the screenshot. Did the action succeed? "
            f'Respond with JSON: {{"verified": true/false, "reason": "..."}}'
        )

        for attempt in range(max_retries + 1):
            if attempt > 0:
                logger.debug(
                    "[VBA] Verification retry %d/%d for: %s",
                    attempt, max_retries, action_description,
                )
                await asyncio.sleep(0.5)

            # Capture post-action screenshot via Playwright
            try:
                screenshot_bytes = await page.screenshot(
                    type="jpeg",
                    quality=self._screenshot_quality,
                    full_page=False,
                )
            except Exception as ss_err:
                logger.debug("[VBA] Verification screenshot failed: %s", ss_err)
                return True  # Can't verify; don't block the loop

            screenshot_b64 = base64.b64encode(screenshot_bytes).decode("ascii")

            # --- Attempt: J-Prime vision server ---
            try:
                response_text = await self._ask_jprime(screenshot_b64, verify_prompt)
                if response_text:
                    match = _re.search(r'\{[\s\S]*\}', response_text)
                    if match:
                        data = json.loads(match.group())
                        verified = bool(data.get("verified", False))
                        logger.debug(
                            "[VBA] J-Prime verification=%s reason=%s",
                            verified, data.get("reason", ""),
                        )
                        if verified:
                            return True
                        continue  # Retry
            except Exception as exc:
                logger.debug("[VBA] Verification via J-Prime failed: %s", exc)

            # --- Attempt: Claude API fallback ---
            try:
                response_text = await self._ask_claude(screenshot_b64, verify_prompt)
                if response_text:
                    match = _re.search(r'\{[\s\S]*\}', response_text)
                    if match:
                        data = json.loads(match.group())
                        verified = bool(data.get("verified", False))
                        logger.debug(
                            "[VBA] Claude verification=%s reason=%s",
                            verified, data.get("reason", ""),
                        )
                        if verified:
                            return True
                        continue  # Retry
            except Exception as exc:
                logger.debug("[VBA] Verification via Claude failed: %s", exc)

            # No model available for this attempt — assume success
            logger.debug(
                "[VBA] No vision model available for verification; assuming success."
            )
            return True

        # All retries exhausted without a positive verification
        return False
