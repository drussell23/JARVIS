# CI/CD Failure Analysis Report

## Executive Summary

- **Workflow**: WebSocket Self-Healing Validation
- **Run Number**: #260
- **Branch**: `main`
- **Commit**: `74df0c2ffbb7b4bcd1aa716ed25c6dc4774a344c`
- **Status**: ❌ FAILED
- **Timestamp**: 2026-04-28T05:21:20Z
- **Triggered By**: @drussell23
- **Workflow URL**: [View Run](https://github.com/drussell23/JARVIS/actions/runs/25035431431)

## Failure Overview

Total Failed Jobs: **6**

| # | Job Name | Category | Severity | Duration |
|---|----------|----------|----------|----------|
| 1 | WebSocket Health Tests (latency-performance, Latency & Performance, ⚡) | test_failure | high | 74s |
| 2 | WebSocket Health Tests (self-healing, Self-Healing & Recovery, 🔄) | test_failure | high | 56s |
| 3 | WebSocket Health Tests (heartbeat-monitoring, Heartbeat & Health Monitoring, 💓) | test_failure | high | 43s |
| 4 | WebSocket Health Tests (connection-lifecycle, Connection Lifecycle, 🔌) | test_failure | high | 49s |
| 5 | WebSocket Health Tests (concurrent-connections, Concurrent Connections, 🔗) | test_failure | high | 86s |
| 6 | WebSocket Health Tests (message-delivery, Message Delivery & Reliability, 📨) | test_failure | high | 49s |

## Detailed Analysis

### 1. WebSocket Health Tests (latency-performance, Latency & Performance, ⚡)

**Status**: ❌ failure
**Category**: Test Failure
**Severity**: HIGH
**Started**: 2026-04-28T05:21:55Z
**Completed**: 2026-04-28T05:23:09Z
**Duration**: 74 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/25035431431/job/73325968367)

#### Failed Steps

- **Step 5**: Install Python Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 6
  - Sample matches:
    - Line 31: `2026-04-28T05:23:06.6247597Z   Getting requirements to build wheel: finished with status 'error'`
    - Line 32: `2026-04-28T05:23:06.6285035Z   error: subprocess-exited-with-error`
    - Line 53: `2026-04-28T05:23:06.6295733Z       ModuleNotFoundError: No module named 'pkg_resources'`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 3
  - Sample matches:
    - Line 57: `2026-04-28T05:23:06.6296920Z ERROR: Failed to build 'openai-whisper' when getting requirements to bu`
    - Line 66: `2026-04-28T05:23:06.7038125Z [36;1m  echo "❌ Some tests failed - review logs" >> $GITHUB_STEP_SUMMA`
    - Line 96: `2026-04-28T05:23:06.8292603Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 2
  - Sample matches:
    - Line 96: `2026-04-28T05:23:06.8292603Z ##[warning]The process '/usr/bin/git' failed with exit code 128`
    - Line 98: `2026-04-28T05:23:06.8628470Z ##[warning]Node.js 20 actions are deprecated. The following actions are`

- Pattern: `AssertionError|Exception`
  - Occurrences: 1
  - Sample matches:
    - Line 0: `2026-04-28T05:23:02.6132974Z Successfully installed aiohappyeyeballs-2.6.1 aiohttp-3.13.5 aiosignal-`

- Pattern: `timeout|timed out`
  - Occurrences: 1
  - Sample matches:
    - Line 0: `2026-04-28T05:23:02.6132974Z Successfully installed aiohappyeyeballs-2.6.1 aiohttp-3.13.5 aiosignal-`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations
2. Review test cases and ensure code changes haven't broken existing functionality

---

### 2. WebSocket Health Tests (self-healing, Self-Healing & Recovery, 🔄)

**Status**: ❌ failure
**Category**: Test Failure
**Severity**: HIGH
**Started**: 2026-04-28T05:21:55Z
**Completed**: 2026-04-28T05:22:51Z
**Duration**: 56 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/25035431431/job/73325968387)

#### Failed Steps

- **Step 5**: Install Python Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 6
  - Sample matches:
    - Line 31: `2026-04-28T05:22:48.1800553Z   Getting requirements to build wheel: finished with status 'error'`
    - Line 32: `2026-04-28T05:22:48.1847311Z   error: subprocess-exited-with-error`
    - Line 53: `2026-04-28T05:22:48.1862275Z       ModuleNotFoundError: No module named 'pkg_resources'`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 3
  - Sample matches:
    - Line 57: `2026-04-28T05:22:48.1863801Z ERROR: Failed to build 'openai-whisper' when getting requirements to bu`
    - Line 66: `2026-04-28T05:22:48.2797891Z [36;1m  echo "❌ Some tests failed - review logs" >> $GITHUB_STEP_SUMMA`
    - Line 96: `2026-04-28T05:22:48.4391159Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 2
  - Sample matches:
    - Line 96: `2026-04-28T05:22:48.4391159Z ##[warning]The process '/usr/bin/git' failed with exit code 128`
    - Line 98: `2026-04-28T05:22:48.4780804Z ##[warning]Node.js 20 actions are deprecated. The following actions are`

- Pattern: `AssertionError|Exception`
  - Occurrences: 1
  - Sample matches:
    - Line 0: `2026-04-28T05:22:44.9215643Z Successfully installed aiohappyeyeballs-2.6.1 aiohttp-3.13.5 aiosignal-`

- Pattern: `timeout|timed out`
  - Occurrences: 1
  - Sample matches:
    - Line 0: `2026-04-28T05:22:44.9215643Z Successfully installed aiohappyeyeballs-2.6.1 aiohttp-3.13.5 aiosignal-`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations
2. Review test cases and ensure code changes haven't broken existing functionality

---

### 3. WebSocket Health Tests (heartbeat-monitoring, Heartbeat & Health Monitoring, 💓)

**Status**: ❌ failure
**Category**: Test Failure
**Severity**: HIGH
**Started**: 2026-04-28T05:21:55Z
**Completed**: 2026-04-28T05:22:38Z
**Duration**: 43 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/25035431431/job/73325968392)

#### Failed Steps

- **Step 5**: Install Python Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 6
  - Sample matches:
    - Line 31: `2026-04-28T05:22:36.1406296Z   Getting requirements to build wheel: finished with status 'error'`
    - Line 32: `2026-04-28T05:22:36.1453045Z   error: subprocess-exited-with-error`
    - Line 53: `2026-04-28T05:22:36.1472008Z       ModuleNotFoundError: No module named 'pkg_resources'`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 3
  - Sample matches:
    - Line 57: `2026-04-28T05:22:36.1474677Z ERROR: Failed to build 'openai-whisper' when getting requirements to bu`
    - Line 66: `2026-04-28T05:22:36.2496965Z [36;1m  echo "❌ Some tests failed - review logs" >> $GITHUB_STEP_SUMMA`
    - Line 96: `2026-04-28T05:22:36.4202991Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 2
  - Sample matches:
    - Line 96: `2026-04-28T05:22:36.4202991Z ##[warning]The process '/usr/bin/git' failed with exit code 128`
    - Line 98: `2026-04-28T05:22:36.4585294Z ##[warning]Node.js 20 actions are deprecated. The following actions are`

- Pattern: `AssertionError|Exception`
  - Occurrences: 1
  - Sample matches:
    - Line 0: `2026-04-28T05:22:33.1890079Z Successfully installed aiohappyeyeballs-2.6.1 aiohttp-3.13.5 aiosignal-`

- Pattern: `timeout|timed out`
  - Occurrences: 1
  - Sample matches:
    - Line 0: `2026-04-28T05:22:33.1890079Z Successfully installed aiohappyeyeballs-2.6.1 aiohttp-3.13.5 aiosignal-`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations
2. Review test cases and ensure code changes haven't broken existing functionality

---

### 4. WebSocket Health Tests (connection-lifecycle, Connection Lifecycle, 🔌)

**Status**: ❌ failure
**Category**: Test Failure
**Severity**: HIGH
**Started**: 2026-04-28T05:21:55Z
**Completed**: 2026-04-28T05:22:44Z
**Duration**: 49 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/25035431431/job/73325968398)

#### Failed Steps

- **Step 5**: Install Python Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 6
  - Sample matches:
    - Line 31: `2026-04-28T05:22:42.7779410Z   Getting requirements to build wheel: finished with status 'error'`
    - Line 32: `2026-04-28T05:22:42.7823579Z   error: subprocess-exited-with-error`
    - Line 53: `2026-04-28T05:22:42.7837363Z       ModuleNotFoundError: No module named 'pkg_resources'`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 3
  - Sample matches:
    - Line 57: `2026-04-28T05:22:42.7838825Z ERROR: Failed to build 'openai-whisper' when getting requirements to bu`
    - Line 66: `2026-04-28T05:22:42.8685108Z [36;1m  echo "❌ Some tests failed - review logs" >> $GITHUB_STEP_SUMMA`
    - Line 96: `2026-04-28T05:22:43.0196571Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 2
  - Sample matches:
    - Line 96: `2026-04-28T05:22:43.0196571Z ##[warning]The process '/usr/bin/git' failed with exit code 128`
    - Line 98: `2026-04-28T05:22:43.0470481Z ##[warning]Node.js 20 actions are deprecated. The following actions are`

- Pattern: `AssertionError|Exception`
  - Occurrences: 1
  - Sample matches:
    - Line 0: `2026-04-28T05:22:40.1705503Z Successfully installed aiohappyeyeballs-2.6.1 aiohttp-3.13.5 aiosignal-`

- Pattern: `timeout|timed out`
  - Occurrences: 1
  - Sample matches:
    - Line 0: `2026-04-28T05:22:40.1705503Z Successfully installed aiohappyeyeballs-2.6.1 aiohttp-3.13.5 aiosignal-`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations
2. Review test cases and ensure code changes haven't broken existing functionality

---

### 5. WebSocket Health Tests (concurrent-connections, Concurrent Connections, 🔗)

**Status**: ❌ failure
**Category**: Test Failure
**Severity**: HIGH
**Started**: 2026-04-28T05:21:56Z
**Completed**: 2026-04-28T05:23:22Z
**Duration**: 86 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/25035431431/job/73325968405)

#### Failed Steps

- **Step 5**: Install Python Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 6
  - Sample matches:
    - Line 31: `2026-04-28T05:23:19.5088565Z   Getting requirements to build wheel: finished with status 'error'`
    - Line 32: `2026-04-28T05:23:19.5123924Z   error: subprocess-exited-with-error`
    - Line 53: `2026-04-28T05:23:19.5135019Z       ModuleNotFoundError: No module named 'pkg_resources'`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 3
  - Sample matches:
    - Line 57: `2026-04-28T05:23:19.5136194Z ERROR: Failed to build 'openai-whisper' when getting requirements to bu`
    - Line 66: `2026-04-28T05:23:19.6038017Z [36;1m  echo "❌ Some tests failed - review logs" >> $GITHUB_STEP_SUMMA`
    - Line 96: `2026-04-28T05:23:19.7310737Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 2
  - Sample matches:
    - Line 96: `2026-04-28T05:23:19.7310737Z ##[warning]The process '/usr/bin/git' failed with exit code 128`
    - Line 98: `2026-04-28T05:23:19.7609631Z ##[warning]Node.js 20 actions are deprecated. The following actions are`

- Pattern: `AssertionError|Exception`
  - Occurrences: 1
  - Sample matches:
    - Line 0: `2026-04-28T05:23:15.3130768Z Successfully installed aiohappyeyeballs-2.6.1 aiohttp-3.13.5 aiosignal-`

- Pattern: `timeout|timed out`
  - Occurrences: 1
  - Sample matches:
    - Line 0: `2026-04-28T05:23:15.3130768Z Successfully installed aiohappyeyeballs-2.6.1 aiohttp-3.13.5 aiosignal-`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations
2. Review test cases and ensure code changes haven't broken existing functionality

---

### 6. WebSocket Health Tests (message-delivery, Message Delivery & Reliability, 📨)

**Status**: ❌ failure
**Category**: Test Failure
**Severity**: HIGH
**Started**: 2026-04-28T05:21:55Z
**Completed**: 2026-04-28T05:22:44Z
**Duration**: 49 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/25035431431/job/73325968415)

#### Failed Steps

- **Step 5**: Install Python Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 6
  - Sample matches:
    - Line 31: `2026-04-28T05:22:42.9460969Z   Getting requirements to build wheel: finished with status 'error'`
    - Line 32: `2026-04-28T05:22:42.9509565Z   error: subprocess-exited-with-error`
    - Line 53: `2026-04-28T05:22:42.9524406Z       ModuleNotFoundError: No module named 'pkg_resources'`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 3
  - Sample matches:
    - Line 57: `2026-04-28T05:22:42.9525925Z ERROR: Failed to build 'openai-whisper' when getting requirements to bu`
    - Line 66: `2026-04-28T05:22:43.0346479Z [36;1m  echo "❌ Some tests failed - review logs" >> $GITHUB_STEP_SUMMA`
    - Line 96: `2026-04-28T05:22:43.1906781Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 2
  - Sample matches:
    - Line 96: `2026-04-28T05:22:43.1906781Z ##[warning]The process '/usr/bin/git' failed with exit code 128`
    - Line 98: `2026-04-28T05:22:43.2286497Z ##[warning]Node.js 20 actions are deprecated. The following actions are`

- Pattern: `AssertionError|Exception`
  - Occurrences: 1
  - Sample matches:
    - Line 0: `2026-04-28T05:22:40.2017662Z Successfully installed aiohappyeyeballs-2.6.1 aiohttp-3.13.5 aiosignal-`

- Pattern: `timeout|timed out`
  - Occurrences: 1
  - Sample matches:
    - Line 0: `2026-04-28T05:22:40.2017662Z Successfully installed aiohappyeyeballs-2.6.1 aiohttp-3.13.5 aiosignal-`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations
2. Review test cases and ensure code changes haven't broken existing functionality

---

## Action Items

- [ ] Review detailed logs for each failed job
- [ ] Implement suggested fixes
- [ ] Add or update tests to prevent regression
- [ ] Verify fixes locally before pushing
- [ ] Update CI/CD configuration if needed

## Additional Resources

- [Workflow File](.github/workflows/)
- [CI/CD Documentation](../../docs/ci-cd/)
- [Troubleshooting Guide](../../docs/troubleshooting/)

---

📊 *Report generated on 2026-04-28T05:25:14.986160*
🤖 *JARVIS CI/CD Auto-PR Manager*
