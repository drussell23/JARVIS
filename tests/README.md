# 🧪 JARVIS AI Agent Test Suite

Comprehensive test suite for the JARVIS AI Agent system, organized by test type and component.

## 📁 Test Organization

### 🔬 `/unit/` - Unit Tests
Fast, isolated tests for individual components.

#### `/unit/backend/`
Core backend functionality:
- `test_imports.py` - Import verification
- `test_jarvis_agent.py` - JARVIS agent core
- `test_jarvis_commands.py` - Command processing
- `test_jarvis_import.py` - Import system
- `test_microphone.py` - Microphone functionality
- `test_close_apps.py` - App closing
- `test_jarvis_close_apps.py` - JARVIS app control
- `test_jarvis_fixed.py` - Bug fixes
- `test_ml_enhanced_jarvis.py` - ML enhancements

#### `/unit/vision/`
Vision system components:
- `test_vision_system.py` - Core vision
- `test_jarvis_vision_commands.py` - Vision commands
- `test_enhanced_vision_commands.py` - Enhanced features
- `test_enhanced_vision.py` - Enhanced system
- `test_jarvis_vision_response.py` - Vision responses
- `test_claude_vision_debug.py` - Debug utilities

#### `/unit/voice/`
Voice system components (to be added)

### 🔗 `/integration/` - Integration Tests
Tests for component interactions and integrations.

- `test_jarvis.py` - Full JARVIS system integration
- `test_jarvis_voice.py` - Voice integration
- `test_claude_math.py` - Claude math capabilities
- `test_memory_api.py` - Memory API integration
- `test_jarvis_vision_integration.py` - Vision integration
- `test_vision_websocket.py` - Vision WebSocket integration
- `test_jarvis_websocket.py` - JARVIS WebSocket integration
- `test_vision_integration.py` - Vision system integration

### ⚙️ `/functional/` - Functional Tests
Feature-level tests validating complete workflows.

#### `/functional/vision/`
- `test_jarvis_vision.py` - Vision feature testing
- `test_jarvis_vision_debug.py` - Vision debugging
- `test_screen_lock.py` - Screen lock functionality
- `test_screen_lock_complete.py` - Complete lock system
- `test_vision_capture.py` - Vision capture
- `test_advanced_vision_intelligence.py` - Advanced intelligence
- `test_vision_edge_cases.py` - Edge case handling
- `test_multi_window_phase1.py` - Multi-window capture
- `test_f1_2_multi_window_capture.py` - Advanced multi-window
- `test_phase2_intelligence.py` - Phase 2 features
- `test_phase3_advanced.py` - Phase 3 features
- `test_prd_complete.py` - PRD compliance
- `test_concise_responses.py` - Response formatting

#### `/functional/voice/`
- `test_lock_unlock.py` - Voice lock/unlock
- `test_jarvis_activation.py` - Voice activation

#### `/functional/automation/`
- `test_autonomy_activation.py` - Autonomous activation

### 📊 `/performance/` - Performance Tests
Performance, load, and resource tests.

- `test_resource_management.py` - Resource management
- `/performance/vision/test_performance.py` - Vision performance

### 🎯 `/e2e/` - End-to-End Tests
Complete system workflow tests.

- `test_startup.py` - System startup
- `test_full_system.py` - Full system workflow
- `test_vision_functional.py` - Vision functional flow

#### `/e2e/email_triage/` - Email Triage Pipeline E2E (Production-Gated)

This suite validates the complete email triage runtime path:

`fetch -> extract -> score -> label -> notify -> snapshot commit`

It is designed to prove correctness under realistic operational constraints
(quiet hours, interrupt budgets, dedup windows, partial dependency failure,
snapshot preservation, and observability event integrity).

**Final suite status:**

| Test File | Tests | Status | Purpose |
|---|---:|---|---|
| `conftest.py` | Fixtures | N/A | Factories, deterministic time control, event capture, singleton isolation |
| `test_full_pipeline.py` | 9 | ✅ 9/9 PASS | Happy path, tiering spread, extraction contract, caps, determinism |
| `test_notification_policy_e2e.py` | 7 | ✅ 7/7 PASS | Quiet hours, budget exhaustion, dedup behavior, quarantine, summary flush |
| `test_snapshot_consistency.py` | 8 | ✅ 8/8 PASS | Commit gating, degraded-cycle preservation, cold-start/staleness behavior |
| `test_error_resilience.py` | 6 | ✅ 6/6 PASS | Partial failures, cascading failures, graceful degradation guarantees |
| `test_observability.py` | 5 | ✅ 5/5 PASS | Event sequence, payload shape, delivery/error telemetry coverage |
| `test_live_gmail_integration.py` | 5 | ⏭️ 5/5 SKIP (by design) | Real Gmail API integration, gated behind explicit opt-in |
| **Total** | **40** | **35 passed, 5 skipped** | **Runtime safety + behavior confidence** |

### Email Triage E2E Run Commands

```bash
# Full triage e2e suite (CI-safe, mocked infra)
pytest tests/e2e/email_triage -v

# Run only deterministic mocked tests (exclude live Gmail)
pytest tests/e2e/email_triage -v -m "not api and not slow"

# Opt in to live Gmail tests (requires credentials + delegated user)
RUN_LIVE_GMAIL_TESTS=true pytest tests/e2e/email_triage/test_live_gmail_integration.py -v
```

### Live Gmail Test Gating and Safety

- Live Gmail tests are intentionally skipped by default.
- They require `RUN_LIVE_GMAIL_TESTS=true` plus Gmail credentials.
- Label writes use a unique per-run prefix (`jarvis/test/<uuid>/...`) and clean up in `finally` blocks.
- Fetch-oriented tests remain read-only where possible.

### Runtime Integration Notes (Why `unified_supervisor.py` Is Unchanged)

The triage pipeline is wired through backend runtime subsystems rather than
the top-level supervisor:

- `backend/autonomy/agent_runtime.py`: housekeeping loop invokes triage cycles behind `EMAIL_TRIAGE_ENABLED`.
- `backend/api/unified_command_processor.py`: email responses are enriched with triage metadata when fresh snapshots exist.
- DLM locking (`email_triage_cycle`) prevents duplicate writes in multi-process scenarios.

Because of that architecture, enabling email triage is an environment/config
concern, not a supervisor bootstrap concern.

### 🛠️ `/utilities/` - Test Utilities
Shared test utilities and helpers.

- `safe_test_runner.py` - Safe test execution
- `quick_test_runner.py` - Quick test runner
- `test_utils.py` - Test utilities
- `run_all_tests.py` - Test suite runner
- `debug_test.py` - Debugging utilities
- `verify_api_key.py` - API key verification
- `demo_enhanced_vision.py` - Vision demo
- `test_advanced_launcher.py` - Advanced launcher

### 📦 `/fixtures/` - Test Fixtures
Shared test data and fixtures.

## 🚀 Running Tests

### Run all tests:
```bash
pytest
```

### Run by test type:
```bash
pytest tests/unit/              # Unit tests only
pytest tests/integration/        # Integration tests only
pytest tests/functional/         # Functional tests only
pytest tests/performance/        # Performance tests only
pytest tests/e2e/                # E2E tests only
```

### Run by component:
```bash
pytest -m vision                 # All vision tests
pytest -m voice                  # All voice tests
pytest -m backend                # All backend tests
```

### Run by marker:
```bash
pytest -m unit                   # All unit tests
pytest -m "not slow"             # Exclude slow tests
pytest -m "api"                  # Tests requiring API keys
pytest -m "permissions"          # Tests requiring permissions
```

### Run specific test file:
```bash
pytest tests/unit/backend/test_jarvis_agent.py
pytest tests/integration/test_jarvis.py
```

### Run with verbose output:
```bash
pytest -v                        # Verbose
pytest -vv                       # Extra verbose
pytest -s                        # Show print statements
```

## 🏷️ Test Markers

Tests are automatically marked based on their location:

- `@pytest.mark.unit` - Unit tests
- `@pytest.mark.integration` - Integration tests
- `@pytest.mark.functional` - Functional tests
- `@pytest.mark.performance` - Performance tests
- `@pytest.mark.e2e` - End-to-end tests
- `@pytest.mark.vision` - Vision system tests
- `@pytest.mark.voice` - Voice system tests
- `@pytest.mark.backend` - Backend tests
- `@pytest.mark.slow` - Slow-running tests
- `@pytest.mark.api` - Tests requiring API keys
- `@pytest.mark.permissions` - Tests requiring system permissions

## 📝 Writing Tests

### Test Guidelines

1. **Location**: Place tests in the appropriate directory:
   - Unit tests → `/unit/`
   - Integration tests → `/integration/`
   - Functional tests → `/functional/`
   - Performance tests → `/performance/`
   - E2E tests → `/e2e/`

2. **Naming**: Follow naming conventions:
   - Files: `test_*.py` or `*_test.py`
   - Classes: `Test*` or `*Tests`
   - Functions: `test_*`

3. **Documentation**: Include clear docstrings:
   ```python
   def test_jarvis_activation():
       """Test JARVIS voice activation workflow."""
       # Test implementation
   ```

4. **Markers**: Add markers for special requirements:
   ```python
   @pytest.mark.api
   @pytest.mark.slow
   def test_api_integration():
       """Test requiring API key and longer runtime."""
       pass
   ```

5. **Fixtures**: Use shared fixtures from `conftest.py`:
   ```python
   def test_with_fixture(project_root_path, mock_env_vars):
       """Test using shared fixtures."""
       pass
   ```

## ⚙️ Configuration

Test configuration is in `pytest.ini` at the project root.

Shared fixtures and hooks are in `tests/conftest.py`.

## ⚠️ Important Notes

- **API Keys**: Some tests require `ANTHROPIC_API_KEY` in `.env`
- **Permissions**: Vision tests need screen recording permission on macOS
- **Microphone**: Voice tests need microphone access
- **Performance**: Performance/E2E tests may take longer to run
- **CI**: Some tests are marked `skip_ci` for CI environments

## 🔍 Debugging Failed Tests

1. **Run with verbose output**: `pytest -vv`
2. **Show print statements**: `pytest -s`
3. **Run specific test**: `pytest path/to/test_file.py::test_name`
4. **Check permissions**: Verify screen/microphone access
5. **Verify API keys**: Ensure `.env` is configured
6. **Check dependencies**: Run `pip install -r requirements.txt`
7. **Review logs**: Check test output for error details

## 📊 Test Coverage

To run tests with coverage reporting:

```bash
pytest --cov=backend --cov-report=html
```

View coverage report: `open htmlcov/index.html`

## 🤝 Contributing Tests

When adding new tests:

1. Follow the organization structure
2. Add appropriate markers
3. Update this README
4. Ensure tests are isolated and repeatable
5. Add fixtures for shared test data
6. Document test requirements

---

**Last Updated**: 2025-10-08
**Test Framework**: pytest 6.0+