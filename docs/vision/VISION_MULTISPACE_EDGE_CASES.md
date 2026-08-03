# JARVIS Vision-Multispace Intelligence: Edge Cases & Scenarios

**Version:** 2.0 (Intelligent Edition)
**Last Updated:** 2025-10-19
**Status:** Production Reference Guide
**New in v2.0:** 6 Intelligent Systems with ML-Powered Proactive Monitoring

---

## Table of Contents

1. [User Query Patterns & Scenarios](#user-query-patterns--scenarios)
2. [Edge Cases & System States](#edge-cases--system-states)
3. [Error Handling Matrix](#error-handling-matrix)
4. [Query Complexity Levels](#query-complexity-levels)
5. [Response Strategies](#response-strategies)
6. [Multi-Monitor Scenarios](#multi-monitor-scenarios)
7. [Temporal & State-Based Scenarios](#temporal--state-based-scenarios)
8. [Performance & Rate Limiting](#performance--rate-limiting)
9. [Security & Privacy Considerations](#security--privacy-considerations)

---

## 1. User Query Patterns & Scenarios

### 1.1 Direct Space Queries

**Clear & Unambiguous:**
```
✅ "What's in space 3?"
✅ "Show me space 1"
✅ "What errors are in space 5?"
✅ "Read space 2"
```

**JARVIS Response:**
- Capture specified space
- Extract text via Claude Vision
- Return OCR results
- Handle gracefully if space doesn't exist

---

### 1.2 Ambiguous/Contextual Queries

**Missing Space Number:**
```
❓ "What's on that screen?"
❓ "What's the error?"
❓ "What IDE am I using?"
❓ "What's happening?"
```

**JARVIS Strategy:**
- Assume **current/active space** (from yabai query)
- Or ask for clarification: `"Which space? (Currently on Space 2)"`
- Default to Space 1 if active space detection fails

---

**Pronoun References:**
```
User: "What's in space 3?"
JARVIS: [Returns OCR results]
User: "What about space 5?"
JARVIS: [Returns space 5 results]
User: "Compare them"
       ^^^^
       Refers to spaces 3 & 5
```

**JARVIS Strategy:**
- Track conversation context (last 2-3 spaces queried)
- Resolve pronouns ("it", "that", "them") to spaces
- Maintain session state in memory

---

### 1.3 Multi-Space Queries

**Comparison:**
```
❓ "Compare space 3 and space 5"
❓ "Which space has the error?"
❓ "Find the terminal across all spaces"
❓ "What's different between space 1 and space 2?"
```

**JARVIS Strategy:**
- Capture all specified spaces in parallel
- Run Claude Vision analysis on each
- Synthesize comparison results
- Return unified response

**Example Response:**
```
Space 3: VS Code, Python file with TypeError on line 421
Space 5: Browser showing documentation
Difference: Space 3 has an active error, Space 5 is reference material
```

---

### 1.4 Temporal Queries ✅ IMPLEMENTED v2.0

**Time-Based:**
```
✅ "What changed in space 3?"
✅ "Has the error been fixed?"
✅ "What's new in the last 5 minutes?"
✅ "When did this error first appear?"
✅ "What patterns have you noticed?" (NEW v3.0)
✅ "Show me predicted events" (NEW v3.0)
✅ "Are there any anomalies?" (NEW v3.0)
```

**v2.0 Implementation - TemporalQueryHandler v3.0:**
- ✅ **Pattern Analysis**: Learns correlations (e.g., "build in Space 5 → error in Space 3")
- ✅ **Predictive Analysis**: Shows predicted events from ML patterns
- ✅ **Anomaly Detection**: Detects unusual behavior in monitoring data
- ✅ **Correlation Analysis**: Multi-space relationship detection
- ✅ **Change Detection**: Uses monitoring cache for instant temporal queries
- ✅ **Error Tracking**: Tracks error appearance/resolution automatically
- ✅ **Monitoring Integration**: Uses HybridProactiveMonitoringManager alerts
- ✅ **Alert Categorization**: Separate queues for anomaly, predictive, correlation alerts

**Example Response:**
```
User: "What patterns have you noticed?"
JARVIS: "I've detected a pattern: When builds complete in Space 5,
        errors appear in Space 3 within 2 minutes (confidence: 85%).
        This has occurred 5 times in the last hour."
```

---

### 1.5 Predictive/Analytical Queries ✅ IMPLEMENTED v2.0

**High-Level Analysis:**
```
✅ "Am I making progress?"
✅ "What should I work on next?"
✅ "Are there any potential bugs?"
✅ "Explain what this code does"
✅ "Will I finish this soon?" (NEW)
✅ "What's my productivity score?" (NEW)
```

**v2.0 Implementation - PredictiveQueryHandler v2.0:**
- ✅ **Progress Tracking**: Analyzes monitoring events (builds, errors, changes)
- ✅ **Bug Prediction**: Learns from error patterns to predict future bugs
- ✅ **Next Step Suggestions**: "Fix errors in Space 3 (high priority)"
- ✅ **Workspace Tracking**: Productivity score with evidence
- ✅ **Evidence-Based**: All predictions include reasoning + supporting evidence
- ✅ **Monitoring Integration**: Real-time analysis from HybridProactiveMonitoring

**Example Response:**
```
User: "Am I making progress?"
JARVIS: "Yes, 70% progress in last 30 minutes.

        Evidence:
        - 3 successful builds
        - Fixed 2 errors
        - Made 15 code changes

        Reasoning: Positive progress detected - successful builds
        outnumber errors, steady code changes indicate active development."
```

---

### 1.6 Action-Oriented Queries

**Requests for Action:**
```
❓ "Fix the error in space 3"
❓ "Switch to space 5"
❓ "Close the browser in space 2"
❓ "Run the tests"
```

**JARVIS Strategy (v1.0):**
- Vision is **read-only**
- Return: `"I can see [error], but cannot execute actions yet"`
- Suggest manual steps

**JARVIS Strategy (v2.0):**
- Integrate with action APIs (yabai, AppleScript)
- Execute safe commands with user confirmation
- Autonomous execution for trusted actions

---

## 2. Edge Cases & System States

### 2.1 Space-Related Edge Cases

| Edge Case | Detection | JARVIS Response |
|-----------|-----------|-----------------|
| **Space doesn't exist** | `yabai -m query --spaces` returns no match | `"Space 10 doesn't exist. You have 6 spaces."` |
| **Empty space** | No windows in space | `"Space 3 is empty (no windows)."` |
| **Space with only minimized windows** | All windows minimized | `"Space 4 has minimized windows only. Cannot capture."` |
| **Space mid-transition** | User switching spaces during capture | Retry with 500ms delay |
| **Fullscreen app** | Single fullscreen window | Capture works normally |
| **Split view** | Multiple windows side-by-side | Capture entire space (both windows) |

---

### 2.2 Window Capture Edge Cases

| Edge Case | Cause | Handling |
|-----------|-------|----------|
| **Invalid window ID** | Window closed mid-capture | Fallback to next window in space |
| **Permission denied** | Screen recording disabled | `"Enable Screen Recording in System Settings > Privacy"` |
| **Window off-screen** | Window partially/fully outside display bounds | CoreGraphics clips to visible area |
| **Transparent windows** | Overlay/HUD windows | Capture underlying content |
| **4K/5K displays** | Very large screenshots | Resize to 2560px max width before sending to Claude |

---

### 2.3 System State Edge Cases

| State | Detection | Response |
|-------|-----------|----------|
| **Yabai not running** | `yabai -m query` fails | `"Yabai not detected. Install: brew install koekeishiya/formulae/yabai"` |
| **Yabai crashed** | Command hangs/timeout | Restart yabai: `brew services restart yabai` |
| **Display sleep** | Screen off, no capture possible | `"Display is sleeping. Wake to use vision."` |
| **Screen locked** | Login screen active | `"Screen is locked. Unlock to capture."` |
| **No displays** | Headless/SSH session | `"No displays detected. Vision requires GUI session."` |

---

### 2.4 API & Network Edge Cases

| Edge Case | Cause | Fallback Strategy |
|-----------|-------|-------------------|
| **Claude API timeout** | Network issues | Retry 3x with exponential backoff (1s, 2s, 4s) |
| **Rate limit (429)** | Too many requests | Wait & retry, use cached results if available |
| **Invalid API key** | Expired/wrong key | `"Claude API key invalid. Check .env"` |
| **Image too large** | Screenshot >5MB | Resize to max 2560px width, compress to JPEG 85% |
| **Network offline** | No internet | `"Offline. Vision requires internet for Claude API."` |

---

## 3. Error Handling Matrix

### 3.1 Graceful Degradation Strategy

```
Priority 1: Try primary method
   ↓ (fails)
Priority 2: Try fallback method
   ↓ (fails)
Priority 3: Return partial results + warning
   ↓ (fails)
Priority 4: Return user-friendly error message
```

---

### 3.2 Capture Fallbacks

```python
# Primary: Capture specific window
try:
    capture_window(window_id)
except:
    # Fallback 1: Capture entire space
    try:
        capture_space(space_id)
    except:
        # Fallback 2: Use cached screenshot (if <60s old)
        try:
            use_cached_screenshot(space_id)
        except:
            # Fallback 3: Return error
            return "Unable to capture Space {space_id}"
```

---

### 3.3 OCR Fallbacks

```python
# Primary: Claude Vision API
try:
    claude_vision_ocr(image)
except RateLimitError:
    # Fallback 1: Use cached OCR (if <5min old)
    return cached_ocr_results(image_hash)
except NetworkError:
    # Fallback 2: Local OCR (Tesseract)
    return tesseract_ocr(image)
except:
    # Fallback 3: Return image metadata only
    return f"Image: {width}x{height}, {window_title}"
```

---

## 4. Query Complexity Levels

### Level 1: Simple (Single Space, Single Question)

**Examples:**
- "What's in space 3?"
- "Show me space 1"

**Processing:**
1. Parse space number
2. Capture space
3. Run OCR
4. Return results

**Latency:** 2-4s
**API Calls:** 1 (Claude Vision)

---

### Level 2: Medium (Multiple Spaces or Context)

**Examples:**
- "Compare space 3 and space 5"
- "Which space has the terminal?"

**Processing:**
1. Parse multiple spaces
2. Capture in parallel
3. Run OCR on each
4. Synthesize comparison

**Latency:** 3-6s
**API Calls:** 2-6 (depending on spaces)

---

### Level 3: Complex (Temporal, Predictive, Cross-Space) ✅ 87% FASTER v2.0

**Examples:**
- "What changed in the last 5 minutes?"
- "Find all errors across all spaces"
- "Am I making progress?"

**v1.0 Processing:**
1. Query all spaces (1-10+)
2. Capture each
3. Run OCR + analysis
4. Apply temporal/semantic logic
5. Synthesize high-level answer

**v1.0 Performance:**
- **Latency:** 10-30s
- **API Calls:** 5-15+

**v2.0 Processing (ComplexComplexityHandler v2.0):**
1. Check HybridProactiveMonitoring cache
2. Use pre-captured snapshots (instant!)
3. Only capture spaces with no recent cache
4. Apply intelligent analysis
5. Synthesize answer

**v2.0 Performance:**
- **Latency:** <5s (87% faster! ⚡)
- **Temporal queries:** 15s → 2s
- **Cross-space queries:** 25s → 4s
- **API Calls:** 2-3 (80% reduction)
- **Cache hit rate:** 60-90% depending on query frequency

**How it achieves 87% speedup:**
- Uses HybridMonitoring's pre-captured snapshots
- Eliminates 10-30s capture/OCR latency
- Parallel processing of remaining captures
- Intelligent cache invalidation (only refresh when changes detected)

---

## 5. Response Strategies

### 5.1 Clear & Actionable

**Good:**
```
✅ "Space 3 has a TypeError on line 421 in test_vision.py.
   The error is: 'NoneType' object has no attribute 'get'"
```

**Bad:**
```
❌ "There's an error."
❌ "I see some text in a code editor."
```

---

### 5.2 Context-Aware

**User Context Matters:**

```
Query: "What's the error?"
Context: User just asked about Space 3

Response: "The error in Space 3 is a TypeError on line 421."
          (No need to re-ask which space)
```

---

### 5.3 Proactive Suggestions

**Offer Next Steps:**

```
Query: "What's in space 5?"
Response: "Space 5 shows Chrome with error documentation for NoneType.
           Would you like me to compare this with the error in Space 3?"
```

---

### 5.4 Confidence Levels

**Express Uncertainty:**

```
High Confidence:
✅ "Space 3 has 15 visible lines of Python code."

Medium Confidence:
⚠️ "Space 3 appears to have a syntax error, though the text is partially obscured."

Low Confidence:
❓ "Space 3 may contain a terminal, but the resolution is too low to confirm."
```

---

## 6. Multi-Monitor Scenarios

### 6.1 Current Limitation (v1.0)

- ❌ Assumes single display
- ❌ Doesn't map spaces to monitors
- ❌ Can't distinguish "left monitor" vs "right monitor"

---

### 6.2 User Queries (Multi-Monitor)

```
❓ "What's on my second monitor?"
❓ "Show me all my displays"
❓ "Which monitor has the terminal?"
❓ "Move space 3 to the left monitor"
```

**v1.0 Response:**
```
"Multi-monitor detection not yet supported.
 I can see Space 3, but cannot identify which monitor it's on."
```

---

### 6.3 v2.0 Multi-Monitor Support ⚠️ PARTIALLY IMPLEMENTED

**Implementation Status:**

✅ **Implemented:**
- Display detection via CoreGraphics (`CGGetActiveDisplayList()`)
- Space-to-display mapping via yabai (`yabai -m query --spaces`)
- Multi-display capture support in vision pipeline
- Display bounds detection for proper window capture

⚠️ **Partially Implemented:**
- Natural language display queries (e.g., "left monitor", "right monitor")
- Display position inference (requires explicit space IDs)
- Multi-display state tracking

❌ **Not Yet Implemented:**
- Display nicknames ("my main monitor", "coding display")
- Automatic monitor role detection (primary, secondary)
- Cross-monitor context awareness

```python
# Current Implementation (backend/display/display_manager.py)
def get_all_displays():
    """Get all active displays."""
    displays = []
    for i in range(CGDisplayGetActiveDisplayList(0, None, None)[1]):
        display_id = CGDisplayGetActiveDisplayList(0, None, None)[2][i]
        bounds = CGDisplayBounds(display_id)
        displays.append({
            'id': display_id,
            'bounds': bounds,
            'is_main': CGDisplayIsMain(display_id)
        })
    return displays

# Current Implementation (backend/yabai_integration.py)
def get_space_display_mapping():
    """Map spaces to displays."""
    spaces = json.loads(subprocess.check_output(['yabai', '-m', 'query', '--spaces']))
    return {space['id']: space['display'] for space in spaces}
```

**Current Capabilities:**
- ✅ "What's in space 3?" → Works (regardless of display)
- ✅ "Compare space 1 and space 5" → Works (even on different displays)
- ⚠️ "What's on my left monitor?" → Requires explicit space IDs
- ❌ "Move this to my main monitor" → Not yet implemented
- ❌ "Compare left and right monitors" → Needs display position logic

**Planned v2.1 Enhancements:**
- Natural language display resolution via ImplicitReferenceResolver
- Display position inference (left/right/center)
- Display role detection (coding/reference/communication)
- Cross-monitor workflow tracking

---

## 7. Temporal & State-Based Scenarios

### 7.1 Change Detection ✅ IMPLEMENTED v2.0

**User Queries:**
```
✅ "What changed since I last asked?"
✅ "Did the error get fixed?"
✅ "Has the build finished?"
✅ "What changes happened in the last 5 minutes?"
✅ "Show me all changes across all spaces"
```

**v2.0 Implementation - ChangeDetectionManager + TemporalQueryHandler:**

✅ **Implemented Features:**
- **Screenshot Hashing**: Perceptual hash (pHash) for change detection
- **Temporal Change Tracking**: Stores all changes with timestamps
- **Multi-Space Change Detection**: Tracks changes across all spaces
- **Change Classification**: NEW_ERROR, ERROR_RESOLVED, CONTENT_CHANGED, VISUAL_CHANGE, NO_CHANGE
- **Performance**: <2s for temporal queries (uses monitoring cache)
- **Integration**: HybridProactiveMonitoring pre-captures snapshots

```python
# Real Implementation (backend/context_intelligence/managers/change_detection_manager.py)
class ChangeDetectionManager:
    def detect_change(self, space_id: int, new_screenshot: bytes):
        # Get cached screenshot
        old_screenshot = self.screenshot_cache.get(space_id)

        if not old_screenshot:
            return ChangeType.NO_CHANGE

        # Calculate perceptual hash
        old_hash = imagehash.phash(Image.open(io.BytesIO(old_screenshot)))
        new_hash = imagehash.phash(Image.open(io.BytesIO(new_screenshot)))

        # Compare hashes
        if old_hash == new_hash:
            return ChangeType.NO_CHANGE
        elif self._is_error_resolved(old_screenshot, new_screenshot):
            return ChangeType.ERROR_RESOLVED
        elif self._is_new_error(new_screenshot):
            return ChangeType.NEW_ERROR
        else:
            return ChangeType.CONTENT_CHANGED
```

**Example Response:**
```
User: "What changed in the last 5 minutes?"
JARVIS: "3 changes detected:
        1. Space 3: Error resolved (TypeError on line 421)
        2. Space 5: Build completed successfully
        3. Space 2: Content changed (new code added)"
```

---

### 7.2 Proactive Monitoring ✅ IMPLEMENTED v2.0

**Autonomous Alerts:**

✅ **Implemented - HybridProactiveMonitoringManager v2.0:**
- **Fast Path Monitoring**: OCR + regex pattern matching (50-100ms)
- **Deep Path Monitoring**: Claude Vision for complex analysis (2-5s)
- **Auto-Detection**: Errors, builds, state changes, stuck states
- **Frequency-Based Escalation**: Same error 3+ times → CRITICAL
- **Multi-Space Correlation**: Detects cascading failures
- **Alert Categorization**: ERROR, WARNING, INFO, CRITICAL
- **Monitoring Interval**: Configurable (default: 30s per space)
- **Pre-Cached Snapshots**: Ready for instant temporal queries

```python
# Real Implementation (backend/context_intelligence/managers/hybrid_proactive_monitoring_manager.py)
class HybridProactiveMonitoringManager:
    async def monitor_space(self, space_id: int):
        """Continuous monitoring loop for a space."""
        while self.is_monitoring:
            # Capture screenshot
            screenshot = await self.capture_space(space_id)

            # Fast Path: OCR + regex
            ocr_text = await self.fast_ocr(screenshot)

            # Check for patterns
            if self.error_pattern.search(ocr_text):
                # Escalate to Deep Path
                analysis = await self.claude_vision_analysis(screenshot)

                # Track frequency
                error_sig = self.get_error_signature(analysis)
                self.error_frequency[error_sig] += 1

                # Escalate if repeated
                severity = "CRITICAL" if self.error_frequency[error_sig] >= 3 else "ERROR"

                # Alert user
                await self.alert_callback({
                    'space_id': space_id,
                    'severity': severity,
                    'message': analysis['error_description'],
                    'frequency': self.error_frequency[error_sig]
                })

            # Cache snapshot for temporal queries
            self.snapshot_cache[space_id] = screenshot

            # Wait interval
            await asyncio.sleep(self.monitoring_interval)
```

**User Experience:**
```
[JARVIS, unprompted]: "Sir, a new TypeError appeared in Space 3, line 422.
                      This is the 4th occurrence in the last hour (CRITICAL)."

[JARVIS, unprompted]: "Build completed in Space 5. All tests passed."

[JARVIS, unprompted]: "Stuck state detected: Space 2 has been in ERROR_STATE
                      for 35 minutes with no changes."
```

**Integration Points:**
- ✅ ErrorRecoveryManager v2.0 - Auto-healing from monitoring alerts
- ✅ StateIntelligence v2.0 - Stuck state detection
- ✅ TemporalQueryHandler v3.0 - Pattern analysis from monitoring data
- ✅ PredictiveQueryHandler v2.0 - Progress tracking from monitoring events

---

### 7.3 Session Memory ✅ PARTIALLY IMPLEMENTED v2.0

**Cross-Session Learning:**

✅ **Implemented Features:**
- **Pattern Recognition**: TemporalQueryHandler v3.0 learns error patterns
- **Visual Signature Library**: StateDetectionPipeline v2.0 auto-learns state signatures
- **Error Frequency Tracking**: ErrorRecoveryManager v2.0 tracks repeated errors
- **Learned Patterns Persistence**: Saved to `~/.jarvis/learned_patterns.json`
- **Signature Library Persistence**: Saved to `~/.jarvis/state_signature_library.json`
- **Cross-Session Recovery**: Recovery strategies persist across sessions

⚠️ **Partially Implemented:**
- **Conversation Memory**: Currently session-scoped (lost on restart)
- **Solution Tracking**: Not yet explicitly tracked ("I fixed it by...")
- **Cross-Session Recommendations**: Limited (only pattern-based)

```python
# Real Implementation (backend/context_intelligence/handlers/temporal_query_handler.py v3.0)
class TemporalQueryHandler:
    def _load_learned_patterns(self):
        """Load learned patterns from disk (NEW v3.0)."""
        pattern_file = os.path.expanduser('~/.jarvis/learned_patterns.json')

        if os.path.exists(pattern_file):
            with open(pattern_file, 'r') as f:
                self.learned_patterns = json.load(f)
                logger.info(f"Loaded {len(self.learned_patterns)} learned patterns")

    def _save_learned_patterns(self):
        """Save learned patterns to disk (NEW v3.0)."""
        pattern_file = os.path.expanduser('~/.jarvis/learned_patterns.json')
        os.makedirs(os.path.dirname(pattern_file), exist_ok=True)

        with open(pattern_file, 'w') as f:
            json.dump(self.learned_patterns, f, indent=2)

        logger.info(f"Saved {len(self.learned_patterns)} learned patterns")

# Real Implementation (backend/vision/intelligence/state_detection_pipeline.py v2.0)
class StateDetectionPipeline:
    async def save_signature_library(self):
        """Persist signature library to disk (NEW v2.0)."""
        library_file = os.path.expanduser('~/.jarvis/state_signature_library.json')
        os.makedirs(os.path.dirname(library_file), exist_ok=True)

        # Serialize signatures
        signatures_data = [
            {
                'signature_type': sig.signature_type,
                'features': sig.features,
                'state_id': sig.state_id,
                'space_id': sig.space_id,
                'auto_learned': sig.auto_learned,
                'match_count': sig.match_count
            }
            for sig in self.signature_library
        ]

        with open(library_file, 'w') as f:
            json.dump(signatures_data, f, indent=2)

        logger.info(f"Saved {len(signatures_data)} visual signatures")
```

**Example Response:**
```
# Session 1 (Monday)
User: "What's the error in space 3?"
JARVIS: "TypeError on line 421: 'NoneType' object has no attribute 'get'"
[Pattern learned: TypeError + NoneType → stored in learned_patterns.json]

# Session 2 (Wednesday) - After JARVIS restart
User: "What's the error in space 5?"
JARVIS: "TypeError on line 89: 'NoneType' object has no attribute 'get'

        Pattern detected: This matches a known error pattern (confidence: 85%).
        Appeared 5 times across 3 spaces in the last week."
```

**❌ Still Need to Implement:**
- Explicit solution tracking ("I fixed it by adding null check" → save solution)
- Cross-session recommendation engine ("Try adding null check like you did Monday")
- Conversation context persistence (currently lost on restart)
- User feedback loop ("Did that solution work?" → improve recommendations)

---

## 8. Performance & Rate Limiting

### 8.1 Claude API Limits

| Tier | RPM | TPM | Daily Limit |
|------|-----|-----|-------------|
| Free | 5 | 20k | 1000 requests |
| Pro | 50 | 100k | 10k requests |
| Team | 100 | 200k | 50k requests |

---

### 8.2 Cost Optimization

**Without Caching:**
- 10 queries/session × 1 image/query = 10 API calls
- ~$0.10/call = **$1.00/session**

**With Smart Caching (v2.0):**
- 10 queries/session × 30% cache hit rate = 7 API calls
- ~$0.10/call = **$0.70/session** (30% savings)

**With Aggressive Caching:**
- 10 queries/session × 60% cache hit rate = 4 API calls
- ~$0.10/call = **$0.40/session** (60% savings)

---

### 8.3 Rate Limit Handling

**Strategy:**

```python
import time
from functools import wraps

def rate_limited(max_per_minute=50):
    min_interval = 60.0 / max_per_minute
    last_called = [0.0]

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            elapsed = time.time() - last_called[0]
            wait_time = min_interval - elapsed

            if wait_time > 0:
                time.sleep(wait_time)

            last_called[0] = time.time()
            return func(*args, **kwargs)
        return wrapper
    return decorator

@rate_limited(max_per_minute=50)
def call_claude_vision(image):
    # API call here
    pass
```

---

### 8.4 Parallelization

**Sequential (Slow):**
```python
# 4 spaces × 3s/space = 12 seconds total
for space in [1, 2, 3, 4]:
    capture_and_ocr(space)
```

**Parallel (Fast):**
```python
# 4 spaces in parallel = 3 seconds total
import concurrent.futures

with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
    futures = [executor.submit(capture_and_ocr, s) for s in [1, 2, 3, 4]]
    results = [f.result() for f in futures]
```

**Speedup:** 4x faster for multi-space queries

---

## 9. Security & Privacy Considerations

### 9.1 Sensitive Data in Screenshots

**Potential Exposure:**
- Passwords visible in terminals
- API keys in .env files
- Personal messages in Slack/email
- Financial data in spreadsheets
- Health records in browser

---

### 9.2 Mitigation Strategies

**1. Redaction (v2.0):**
```python
def redact_sensitive_data(image):
    # OCR to find patterns
    text = ocr(image)

    # Detect sensitive patterns
    patterns = [
        r'password\s*[:=]\s*\S+',  # Passwords
        r'sk-[a-zA-Z0-9]{32,}',     # OpenAI keys
        r'\d{4}-\d{4}-\d{4}-\d{4}', # Credit cards
    ]

    # Black out matches
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            bbox = get_bounding_box(match)
            image = black_out_region(image, bbox)

    return image
```

**2. User Consent:**
```python
# First time capturing a space
if not user_consented(space_id):
    preview = capture_space(space_id)
    show_preview(preview)

    consent = ask_user(f"Allow JARVIS to read Space {space_id}?")
    if consent:
        store_consent(space_id)
    else:
        return "User denied consent for Space {space_id}"
```

**3. Local-Only Mode:**
```python
# .env
JARVIS_VISION_MODE=local  # Never send to Claude API

# Use local OCR (Tesseract) instead
def ocr_image(image):
    if os.getenv('JARVIS_VISION_MODE') == 'local':
        return tesseract.image_to_string(image)
    else:
        return claude_vision_api(image)
```

---

### 9.3 Data Retention

**Current (v1.0):**
- Screenshots stored temporarily in `/tmp/jarvis_vision/`
- Deleted after processing
- No persistent storage

**Future (v2.0 with caching):**
- Screenshots cached for 30-60 seconds
- OCR results cached for 5 minutes
- Automatic expiration/cleanup
- Option to disable caching: `JARVIS_VISION_CACHE=false`

---

### 9.4 Audit Logging

**Track Vision Usage:**

```python
# Log every capture
import logging

logging.info({
    'timestamp': '2025-10-14T15:30:42Z',
    'space_id': 3,
    'query': 'What errors are visible?',
    'user': 'derek',
    'api_call': True,
    'cache_hit': False
})
```

**Benefits:**
- Debugging
- Cost tracking
- Security audits
- Privacy compliance

---

## Summary: Edge Case Coverage

### ✅ Well Handled (v1.0)
- Single space queries
- Basic error detection
- Simple OCR
- Yabai integration
- Permission errors

### ⚠️ Partially Handled
- Multi-space queries (works but slow)
- Rate limiting (manual backoff)
- Large images (resize but not optimal)

### ✅ Well Handled (v2.0 - Intelligent Edition)
- ✅ Temporal tracking (TemporalQueryHandler v3.0)
- ✅ Change detection (ChangeDetectionManager)
- ✅ Proactive monitoring (HybridProactiveMonitoringManager)
- ✅ Predictive analysis (PredictiveQueryHandler v2.0)
- ✅ Error recovery (ErrorRecoveryManager v2.0)
- ✅ State intelligence (StateIntelligence v2.0)
- ✅ Pattern learning (StateDetectionPipeline v2.0)
- ✅ Complex queries (ComplexComplexityHandler v2.0 - 87% faster!)

### ⚠️ Partially Handled (v2.0)
- ⚠️ Multi-monitor detection (display detection works, natural language queries limited)
- ⚠️ Session memory (pattern learning works, conversation context limited)
- ⚠️ Autonomous actions (error recovery auto-healing works, general actions limited)

### ❌ Not Yet Handled (Requires v2.1+)
- ❌ Sensitive data redaction (planned)
- ❌ Solution tracking from user feedback ("I fixed it by..." → save solution)
- ❌ Cross-session conversation memory (currently session-scoped)
- ❌ Display nicknames and role detection
- ❌ Advanced autonomous actions (file edits, refactoring)

---

## Next Steps

1. **Read this document** before handling user queries
2. **Reference edge case matrix** when encountering errors
3. **Implement missing features** from roadmap
4. **Update this doc** as new edge cases are discovered

---

---

## 10. New Edge Cases Discovered in v2.0

### 10.1 Monitoring Cache Edge Cases

| Edge Case | Cause | Handling |
|-----------|-------|----------|
| **Cache miss on temporal query** | Space not monitored or cache expired | Fallback to fresh capture (adds 2-3s latency) |
| **Stale cache used** | Change happened after last monitoring cycle | Change detection invalidates cache automatically |
| **Cache invalidation race** | Change detected while query in progress | Query uses timestamped cache, flags as potentially stale |
| **Memory pressure** | Too many cached snapshots | LRU eviction (keeps most recent 50 spaces) |

**Example:**
```
User: "What changed in space 3?"
[Cache miss - space 3 not monitored]
JARVIS: "Capturing fresh snapshot... [2s delay]
         Space 3: No recent monitoring data, captured new snapshot.
         Would you like me to add Space 3 to monitoring?"
```

---

### 10.2 Pattern Learning Edge Cases

| Edge Case | Cause | Handling |
|-----------|-------|----------|
| **Insufficient data for pattern** | <3 occurrences | No pattern reported, continues collecting |
| **Conflicting patterns** | Same trigger → different outcomes | Reports confidence scores for each |
| **Pattern overfitting** | Spurious correlation | Requires minimum confidence threshold (70%) |
| **Pattern false positive** | Correlation without causation | User can dismiss pattern (reduces confidence) |

**Example:**
```
User: "What patterns have you noticed?"
JARVIS: "Detected 2 patterns:

        1. Build in Space 5 → Error in Space 3 (85% confidence, 5 occurrences)
        2. Terminal command in Space 2 → Build in Space 5 (60% confidence, 3 occurrences)

        Pattern 2 has low confidence - need more data."
```

---

### 10.3 Stuck State Detection Edge Cases

| Edge Case | Cause | Handling |
|-----------|-------|----------|
| **False positive: Intentional long task** | User legitimately working >30min | User can dismiss alert, increases threshold for that space |
| **False negative: Rapid context switching** | User stuck but keeps switching spaces | Productivity score drops, triggers different alert |
| **State misclassification** | Debugging misclassified as STUCK | Uses multiple signals (code changes, errors, builds) |
| **Alert spam** | Multiple stuck alerts for same state | Deduplicate: 1 alert per state per session |

**Example:**
```
[JARVIS, unprompted]: "Stuck state detected: Space 3 has been in ERROR_STATE
                      for 35 minutes. Would you like suggestions?"

User: "I'm intentionally debugging this slowly"
JARVIS: "Understood. I'll increase the stuck threshold for Space 3 to 60 minutes."
```

---

### 10.4 Visual Signature Collision Edge Cases

| Edge Case | Cause | Handling |
|-----------|-------|----------|
| **Two states same signature** | Different states visually identical | Uses additional context (space ID, recent events) |
| **Partial match ambiguity** | Signature matches 70% for two states | Reports ambiguity: "Could be CODING or READING" |
| **Signature drift** | State visually evolves over time | Auto-updates signature on repeated matches |
| **New signature conflicts** | Auto-learned signature matches existing | Merges if confidence >90%, else keeps separate |

**Example:**
```
User: "What state is Space 3 in?"
JARVIS: "Ambiguous match: Visual signature matches both CODING (72%) and
        DEBUGGING (68%). Context suggests DEBUGGING based on recent error."
```

---

### 10.5 Multi-Space Error Correlation Edge Cases

| Edge Case | Cause | Handling |
|-----------|-------|----------|
| **False correlation** | Unrelated errors happen simultaneously | Requires minimum confidence (70%) and temporal proximity |
| **Cascading failure spam** | 10+ errors in chain | Groups related errors, reports root cause only |
| **Correlation across sessions** | Related errors hours apart | Tracks error fingerprints across sessions |
| **Correlation false negative** | Related errors in different formats | Uses error fingerprinting (not just text matching) |

**Example:**
```
User: "Why is Space 7 failing?"
JARVIS: "Cascading failure detected:

        Root cause: Import error in Space 3 (file_utils.py)
        ↓
        Space 5: Test failures (missing dependency)
        ↓
        Space 7: Build failed (missing module)

        Fix the import in Space 3 to resolve all errors."
```

---

### 10.6 Predictive Analysis Edge Cases

| Edge Case | Cause | Handling |
|-----------|-------|----------|
| **Insufficient monitoring history** | <5 minutes of data | Returns: "Not enough data for prediction" |
| **Conflicting evidence** | Some indicators positive, some negative | Reports confidence score + reasoning |
| **Prediction overconfidence** | Pattern matches but context changes | Always includes reasoning + evidence |
| **Prediction drift** | User workflow changes over time | Re-learns patterns, deprecates old ones after 7 days |

**Example:**
```
User: "Will I finish this task soon?"
JARVIS: "Uncertain (40% confidence).

        Evidence:
        - Positive: 3 successful builds in last 20 minutes
        - Negative: 4 new errors introduced, error rate increasing

        Reasoning: Progress is happening but error rate suggests
        significant debugging still needed."
```

---

### 10.7 Async Error Recovery Edge Cases

| Edge Case | Cause | Handling |
|-----------|-------|----------|
| **Recovery strategy fails** | Auto-healing doesn't work | Escalates to user with alternative strategies |
| **Recovery causes new error** | Fix introduces regression | Detects new error, reverts recovery action |
| **Multiple recovery attempts** | First strategy fails | Tries up to 3 strategies with exponential backoff |
| **Recovery timing conflict** | User fixes error while recovery in progress | Detects user action, cancels recovery gracefully |

**Example:**
```
[JARVIS, unprompted]: "Attempting auto-recovery for TypeError in Space 3..."
[2 seconds later]: "Recovery failed. Alternative strategy: Add null check.
                   Shall I continue?"

User: "No, I already fixed it"
JARVIS: "Confirmed. Cancelling recovery. Error resolved by user."
```

---

**Document Maintainer:** Derek Russell
**JARVIS Version:** 2.0 (Intelligent Edition - multi-monitor-support branch)
**Last Test Date:** 2025-10-19
**v2.0 Release Date:** 2025-10-19
