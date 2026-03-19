# Hybrid Vision + Accessibility Element Resolution Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace approximate LLaVA pixel coordinates with exact macOS Accessibility API positions, using LLaVA only to identify WHAT to interact with while AX finds WHERE it is.

**Architecture:** The vision prompt changes from "give me (x,y) coordinates" to "describe the UI element to interact with (role, title, description)." A new `AccessibilityElementResolver` queries the macOS AX tree to find the exact element matching the description, extracts its position+size, and returns center coordinates for clicking. Fallback chain: AX exact match → AX fuzzy match → AppleScript UI element query → original pixel coordinates (last resort).

**Tech Stack:** Existing: `ApplicationServices.AXUIElement*` (pyobjc, installed), `AccessibilityBackend` in `yabai_aware_actuator.py`, `NSWorkspace` for PID resolution. No new dependencies.

---

## Root Cause

LLaVA (and all vision-language models) estimate pixel coordinates from image understanding. Small UI elements (search bars, buttons, text fields) require ~5px precision, but LLaVA is accurate to ~50-100px. This is a fundamental limitation of image-to-coordinate regression.

The macOS Accessibility API (`AXUIElement`) knows the **exact frame** of every UI element because the OS renders them. By combining LLaVA's understanding (WHAT) with AX precision (WHERE), we get reliable automation.

## Existing Infrastructure (DO NOT rebuild)

| Component | File | What It Has |
|-----------|------|-------------|
| `AccessibilityBackend` | `backend/ghost_hands/yabai_aware_actuator.py:353-603` | `_find_element(title, role)`, `_cg_click(x, y)`, `AXUIElementCreateApplication`, recursive child search |
| `PermissionManager` | `backend/macos_helper/permission_manager.py` | TCC accessibility permission check |
| `NativeAppControlAgent` | `backend/neural_mesh/agents/native_app_control_agent.py` | Vision-action loop, step decomposition, `_ask_jprime_for_action()` |
| NSWorkspace PID resolution | `backend/vision/macos_space_detector.py` | `NSWorkspace.sharedWorkspace().runningApplications()` for app→PID |

## File Structure

### New Files

| File | Responsibility |
|------|---------------|
| `backend/neural_mesh/agents/accessibility_resolver.py` | Resolves LLaVA element descriptions to exact (x, y) coordinates via AX tree. Single responsibility: description in → coordinates out. |

### Modified Files

| File | Change |
|------|--------|
| `backend/neural_mesh/agents/native_app_control_agent.py` | Change vision prompt from "return coordinates" to "return element description". Use `AccessibilityResolver` for coordinates. Modify `_click()` to use AX-resolved positions. |

---

## Task 1: AccessibilityResolver — Exact Element Position from Description

**Files:**
- Create: `backend/neural_mesh/agents/accessibility_resolver.py`
- Test: `tests/integration/test_execution_tiers.py` (append)

The resolver takes an element description from LLaVA (e.g., `{"element": "search bar", "role": "text field", "near_text": "Chats"}`) and returns exact screen coordinates by querying the macOS Accessibility tree.

- [ ] **Step 1: Write failing tests**

```python
class TestAccessibilityResolver:
    @pytest.mark.asyncio
    async def test_resolve_returns_coordinates_or_none(self):
        from backend.neural_mesh.agents.accessibility_resolver import AccessibilityResolver
        resolver = AccessibilityResolver()
        result = await resolver.resolve("search bar", app_name="WhatsApp")
        # Should return dict with x, y or None
        assert result is None or ("x" in result and "y" in result)

    @pytest.mark.asyncio
    async def test_resolve_with_role_filter(self):
        from backend.neural_mesh.agents.accessibility_resolver import AccessibilityResolver
        resolver = AccessibilityResolver()
        result = await resolver.resolve(
            "search", app_name="Finder", role="AXTextField"
        )
        # Finder's search bar is an AXTextField

    def test_get_pid_for_app(self):
        from backend.neural_mesh.agents.accessibility_resolver import AccessibilityResolver
        resolver = AccessibilityResolver()
        pid = resolver._get_pid_for_app("Finder")
        assert pid is not None  # Finder is always running
        assert isinstance(pid, int)

    def test_get_pid_for_nonexistent_app(self):
        from backend.neural_mesh.agents.accessibility_resolver import AccessibilityResolver
        resolver = AccessibilityResolver()
        pid = resolver._get_pid_for_app("NonExistentApp99999")
        assert pid is None
```

- [ ] **Step 2: Run tests — verify they fail**

- [ ] **Step 3: Implement AccessibilityResolver**

Key methods:
- `resolve(description, app_name, role=None, near_text=None) -> Optional[Dict]` — main entry point
- `_get_pid_for_app(app_name) -> Optional[int]` — NSWorkspace PID lookup
- `_find_element_ax(app_pid, description, role) -> Optional[AXElement]` — AX tree search
- `_find_element_applescript(app_name, description) -> Optional[Dict]` — AppleScript fallback
- `_get_element_center(element) -> Dict[str, int]` — extract position+size, return center

Fallback chain:
1. AX exact title match (fastest, most reliable)
2. AX fuzzy title match (substring, case-insensitive)
3. AX role+description match (when title is empty)
4. AppleScript UI element query (different API path)
5. Return None (caller falls back to pixel coords)

- [ ] **Step 4: Run tests — verify they pass**
- [ ] **Step 5: Commit**

---

## Task 2: Change Vision Prompt — Descriptions Instead of Coordinates

**Files:**
- Modify: `backend/neural_mesh/agents/native_app_control_agent.py`

Change `_VISION_PROMPT_TEMPLATE` so LLaVA returns element descriptions instead of pixel coordinates. The new action format:

```json
{
  "done": false,
  "action_type": "click",
  "detail": {
    "element": "search bar",
    "role": "text field",
    "near_text": "Chats"
  },
  "message": "Clicking the search bar at the top of WhatsApp"
}
```

For `type` and `key` actions, no change needed (they don't need coordinates).

- [ ] **Step 1: Update `_VISION_PROMPT_TEMPLATE`**

Replace coordinate-based instructions with element-description-based:
```
RULES:
- For "click" actions: describe the UI element to click (name, role, nearby text).
  Do NOT guess pixel coordinates. Just describe what you see.
- For "type" actions: provide the exact text to type.
- For "key" actions: provide the key name.

click detail format:
  {"element": "human-readable name", "role": "button|text field|menu item|etc", "near_text": "visible text near the element"}
```

- [ ] **Step 2: Update `_click()` method to use AccessibilityResolver**

Replace raw coordinate click with AX-resolved click:
```python
async def _click(self, detail: Dict[str, Any], app_name: str) -> bool:
    # 1. Try AccessibilityResolver first (exact)
    resolver = self._get_resolver()
    coords = await resolver.resolve(
        description=detail.get("element", ""),
        app_name=app_name,
        role=detail.get("role"),
        near_text=detail.get("near_text"),
    )
    if coords:
        # AX found exact position — use CGEvent click
        return await self._cg_click(coords["x"], coords["y"])

    # 2. Fallback: pixel coordinates from vision model (if provided)
    if "x" in detail and "y" in detail:
        return await self._cg_click(detail["x"], detail["y"])

    # 3. Last resort: AppleScript click by description
    return await self._applescript_click(detail.get("element", ""), app_name)
```

- [ ] **Step 3: Update action dispatch in vision loop**

Change the click handler from `_click(x, y)` to `_click(detail, app_name)`.

- [ ] **Step 4: Run all tests**
- [ ] **Step 5: Commit**

---

## Task 3: Integration Test — WhatsApp Search Bar Click

**Files:**
- Test: `tests/integration/test_execution_tiers.py` (append)

- [ ] **Step 1: Write integration test**

```python
class TestHybridVisionAccessibility:
    @pytest.mark.asyncio
    async def test_whatsapp_search_bar_resolved_by_ax(self):
        """Verify AX can find WhatsApp's search bar exactly."""
        from backend.neural_mesh.agents.accessibility_resolver import AccessibilityResolver
        resolver = AccessibilityResolver()

        # This only works if WhatsApp is running
        result = await resolver.resolve(
            description="search",
            app_name="WhatsApp",
            role="AXTextField",
        )
        if result:
            assert "x" in result and "y" in result
            assert result["x"] > 0 and result["y"] > 0
            print(f"WhatsApp search bar at: ({result['x']}, {result['y']})")
        else:
            pytest.skip("WhatsApp not running or element not found")

    @pytest.mark.asyncio
    async def test_finder_search_resolved(self):
        """Finder is always running — good baseline test."""
        from backend.neural_mesh.agents.accessibility_resolver import AccessibilityResolver
        resolver = AccessibilityResolver()
        pid = resolver._get_pid_for_app("Finder")
        assert pid is not None, "Finder should always be running"
```

- [ ] **Step 2: Run tests**
- [ ] **Step 3: Commit**

---

## Task 4: E2E Test — Full WhatsApp Flow with AX Resolution

**Files:**
- Create or modify: `tests/integration/test_live_visual_control.py`

Live test that runs the full pipeline:
1. Decompose "Send Zach a message on WhatsApp" into steps (J-Prime)
2. For each step, vision model returns element descriptions (not coords)
3. AX resolver finds exact positions
4. Actions execute at exact coordinates
5. Verification confirms each step

- [ ] **Step 1: Write E2E test script**
- [ ] **Step 2: Run with J-Prime + WhatsApp open**
- [ ] **Step 3: Commit**

---

## Architecture After Fix

```
LLaVA sees screenshot:
  "I see a search bar at the top of WhatsApp, with 'Chats' label"
  Returns: {"action_type": "click", "detail": {"element": "search bar", "role": "text field"}}
       |
       v
AccessibilityResolver:
  1. Get WhatsApp PID via NSWorkspace
  2. AXUIElementCreateApplication(pid)
  3. Traverse AX tree: find element with role=AXTextField near title containing "search"
  4. AXUIElementCopyAttributeValue(element, kAXPositionAttribute) -> (145, 98)
  5. AXUIElementCopyAttributeValue(element, kAXSizeAttribute) -> (280, 32)
  6. Return center: (145 + 280/2, 98 + 32/2) = (285, 114)
       |
       v
CGEvent click at (285, 114) — EXACT position, guaranteed hit
```

**Why this cures the disease:**
- Vision models will NEVER be pixel-precise — this is a fundamental limitation
- AX API will ALWAYS know exact positions — this is how the OS renders UI
- The hybrid approach uses each for what it's best at
- Fallback chain ensures graceful degradation if AX fails
