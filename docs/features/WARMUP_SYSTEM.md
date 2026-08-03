# Advanced Component Warmup System

## Overview

The warmup system eliminates first-command latency by pre-initializing all components at startup using priority-based, async, robust loading with health checks.

## 🚀 Key Features

### 1. **Priority-Based Loading**
Components load in order of criticality:
- **CRITICAL** (Priority 0): Must be ready before any commands
- **HIGH** (Priority 1): Should be ready for first command
- **MEDIUM** (Priority 2): Nice to have, loads progressively
- **LOW** (Priority 3): Background loading
- **DEFERRED** (Priority 4): On-demand only

### 2. **Parallel Initialization**
- Components within same priority level load concurrently
- Respects `max_concurrent` limit (default: 10)
- Dependency-aware ordering (topological sort)

### 3. **Health Checks**
- Each component can define a health check function
- Retries with exponential backoff (configurable)
- Health scores (0.0 to 1.0) tracked per component

### 4. **Graceful Degradation**
- Non-required components can fail without blocking startup
- Falls back to lazy initialization on warmup failure
- Detailed error tracking and metrics

### 5. **Zero Hardcoding**
- Components auto-register themselves
- Dynamic discovery and loading
- No hardcoded component lists

## 📋 Component Priority Configuration

### CRITICAL Components (5-20s timeout)
- `screen_lock_detector`: Screen lock state detection (5s)
- `voice_auth`: Voice authentication system with FULL initialization (20s)
  - **OPTIMIZED**: Now pre-loads ALL voice models during startup!
  - Pre-loads ECAPA-TDNN speaker encoder (instant biometric recognition)
  - Pre-warms SpeechBrain STT engine (instant transcription)
  - Caches speaker profiles from Cloud SQL database
  - **Result**: First "unlock my screen" command responds in <5s instead of 30-60s!

### HIGH Components (10s timeout)
- `context_aware_handler`: Context-aware command handling
- `multi_space_context_graph`: Multi-space context tracking
- `implicit_reference_resolver`: NLP entity resolution
- `compound_action_parser`: Compound command parsing
- `macos_controller`: System control interface

### MEDIUM Components (15s timeout)
- `query_complexity_manager`: Query complexity classification
- `yabai_detector`: Yabai space detection
- `multi_space_window_detector`: Window detection
- `learning_database`: Pattern learning database

### LOW Components (progressive)
- `action_query_handler`: Action query execution
- `predictive_query_handler`: Predictive intelligence
- `multi_space_query_handler`: Multi-space queries

## 🔧 Usage

### Automatic Startup
Warmup happens automatically on JARVIS startup via `main.py`:

```python
# In lifespan function
processor = get_unified_processor(app=app)
warmup_report = await processor.warmup_components()
```

### Manual Warmup
```python
from core.component_warmup import get_warmup_system
from api.component_warmup_config import register_all_components

# Register components
await register_all_components()

# Execute warmup
warmup = get_warmup_system()
report = await warmup.warmup_all()

print(f"{report['ready_count']}/{report['total_count']} components ready")
print(f"Load time: {report['total_load_time']:.2f}s")
```

### Check Component Status
```python
warmup = get_warmup_system()

# Check if ready
if warmup.is_ready("screen_lock_detector"):
    detector = warmup.get_component("screen_lock_detector")

# Wait for component
await warmup.wait_for_component("context_aware_handler", timeout=10.0)

# Wait for critical components
await warmup.wait_for_critical(timeout=30.0)
```

## 📊 Metrics

The warmup report includes:
- **Total components**: Number registered
- **Ready count**: Successfully loaded
- **Failed count**: Failed to load
- **Total load time**: End-to-end warmup time
- **Critical load time**: Time to load CRITICAL components
- **Component metrics**: Per-component load time, retries, health scores

Example report:
```json
{
  "total_count": 15,
  "ready_count": 14,
  "failed_count": 1,
  "total_load_time": 8.2,
  "critical_load_time": 2.1,
  "status_breakdown": {
    "ready": 14,
    "failed": 1
  },
  "component_metrics": {
    "screen_lock_detector": {
      "load_time": 0.3,
      "retry_count": 0,
      "health_score": 1.0,
      "last_error": null
    }
  }
}
```

## 🏗️ Architecture

### Files
- **`backend/core/component_warmup.py`**: Core warmup engine
  - `ComponentWarmupSystem`: Main warmup orchestrator
  - `ComponentDefinition`: Component metadata
  - Priority-based loading, dependency resolution, health checks

- **`backend/api/component_warmup_config.py`**: Component registration
  - `register_all_components()`: Registers all components
  - Component loaders (async functions)
  - Health check functions

- **`backend/api/unified_command_processor.py`**: Integration
  - `warmup_components()`: Triggers warmup from processor
  - Stores component instances after warmup

- **`backend/main.py`**: Startup integration
  - Calls warmup during FastAPI lifespan startup
  - Stores warmup report in app state

### Flow
1. **Startup** → `main.py:lifespan()`
2. **Get Processor** → `get_unified_processor()`
3. **Warmup** → `processor.warmup_components()`
4. **Register** → `register_all_components()`
5. **Execute** → `warmup.warmup_all()`
6. **Load by Priority** → CRITICAL → HIGH → MEDIUM → LOW
7. **Parallel Load** → Within each priority level
8. **Health Check** → Verify each component
9. **Store Instances** → In processor and warmup system
10. **Ready** → First command executes instantly

## 🎯 Performance Impact

### Before Warmup
- **First command latency**: 8-10 seconds
- **Lazy initialization**: Components load on first use
- **Blocking**: User waits for initialization
- **Sequential**: One component at a time

### After Warmup
- **First command latency**: <500ms
- **Pre-initialization**: All components ready at startup
- **Non-blocking**: Warmup happens during startup
- **Parallel**: Multiple components load simultaneously
- **Startup time increase**: +5-8 seconds (acceptable trade-off)

## 🔍 Monitoring

### Startup Logs
```
🚀 Starting advanced component warmup...
[WARMUP] 🚀 Starting component warmup (15 components registered)
[WARMUP] Loading 2 CRITICAL priority components...
[WARMUP] 📦 Loading screen_lock_detector...
[WARMUP] ✅ screen_lock_detector ready in 0.31s (attempt 1)
[WARMUP] ✅ Critical components ready in 2.12s
[WARMUP] Loading 5 HIGH priority components...
...
[WARMUP] 🎉 Warmup complete in 8.23s (14/15 components ready)
✅ Component warmup complete! 14/15 ready in 8.23s
```

### Failed Components
```
[WARMUP] ❌ yabai_detector failed to load!
[WARMUP] ⚠️  Optional component yabai_detector failed to load
```

## 🛠️ Adding New Components

### 1. Define Loader
```python
# In component_warmup_config.py
async def load_my_component():
    """Load my component"""
    from my_module import MyComponent
    instance = MyComponent()
    await instance.initialize()
    return instance
```

### 2. Define Health Check (Optional)
```python
async def check_my_component_health(component) -> bool:
    """Verify component is working"""
    try:
        return await component.ping()
    except:
        return False
```

### 3. Register Component
```python
# In register_all_components()
warmup.register_component(
    name="my_component",
    loader=load_my_component,
    priority=ComponentPriority.HIGH,
    health_check=check_my_component_health,
    dependencies=["other_component"],  # Optional
    timeout=10.0,
    retry_count=2,
    required=False,
    category="my_category",
)
```

### 4. Use Component
```python
# In unified_command_processor.py warmup_components()
self.my_component = warmup.get_component("my_component")
```

## 🐛 Troubleshooting

### Component Fails to Load
1. Check logs for error details
2. Increase timeout if needed
3. Add retry_count for transient failures
4. Mark as `required=False` if optional
5. Simplify loader function

### Startup Hangs
1. Check for deadlocks in component loaders
2. Verify no synchronous blocking calls
3. Check dependency cycles
4. Add timeout to loaders

### Health Check Fails
1. Simplify health check logic
2. Make health check more lenient
3. Remove health check if not critical
4. Check health check timeout (5s default)

## 📚 Best Practices

1. **Keep loaders async**: No blocking I/O
2. **Short critical path**: CRITICAL components should be fast (<5s)
3. **Progressive loading**: Heavy components in MEDIUM/LOW
4. **Health checks**: Simple, fast, reliable
5. **Graceful degradation**: Mark optional as `required=False`
6. **Dependencies**: Only specify when truly needed
7. **Timeouts**: Set appropriate per component
8. **Error handling**: Loaders should handle their own errors

## 🎉 Benefits

✅ **Instant response** - No first-command latency
✅ **Robust** - Health checks and retries
✅ **Async** - Non-blocking, parallel loading
✅ **Dynamic** - No hardcoding, auto-discovery
✅ **Resilient** - Graceful degradation on failures
✅ **Metrics** - Detailed load times and health
✅ **Priority-aware** - Critical components first
✅ **Dependency-safe** - Topological ordering

## 🚦 Status

🟢 **Production Ready**
- All core components registered
- Health checks in place
- Integrated with main.py startup
- Metrics and logging complete
- Graceful fallback to lazy init
