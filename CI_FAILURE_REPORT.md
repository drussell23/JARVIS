# CI/CD Failure Analysis Report

## Executive Summary

- **Workflow**: WebSocket Self-Healing Validation
- **Run Number**: #25
- **Branch**: `feature/macos-hud-ui`
- **Commit**: `e95f4c8cf62baefcb91885fcbf7215be52162c1a`
- **Status**: ❌ FAILED
- **Timestamp**: 2025-11-15T04:41:48Z
- **Triggered By**: @drussell23
- **Workflow URL**: [View Run](https://github.com/drussell23/JARVIS/actions/runs/19384578932)

## Failure Overview

Total Failed Jobs: **6**

| # | Job Name | Category | Severity | Duration |
|---|----------|----------|----------|----------|
| 1 | WebSocket Health Tests (connection-lifecycle, Connection Lifecycle, 🔌) | test_failure | high | 66s |
| 2 | WebSocket Health Tests (concurrent-connections, Concurrent Connections, 🔗) | test_failure | high | 44s |
| 3 | WebSocket Health Tests (self-healing, Self-Healing & Recovery, 🔄) | test_failure | high | 49s |
| 4 | WebSocket Health Tests (heartbeat-monitoring, Heartbeat & Health Monitoring, 💓) | test_failure | high | 46s |
| 5 | WebSocket Health Tests (latency-performance, Latency & Performance, ⚡) | test_failure | high | 53s |
| 6 | WebSocket Health Tests (message-delivery, Message Delivery & Reliability, 📨) | test_failure | high | 54s |

## Detailed Analysis

### 1. WebSocket Health Tests (connection-lifecycle, Connection Lifecycle, 🔌)

**Status**: ❌ failure
**Category**: Test Failure
**Severity**: HIGH
**Started**: 2025-11-15T04:42:02Z
**Completed**: 2025-11-15T04:43:08Z
**Duration**: 66 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19384578932/job/55469493283)

#### Failed Steps

- **Step 5**: Install Python Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 45: `2025-11-15T04:43:04.8529389Z ERROR: Cannot install psutil==5.9.6 and psutil==5.9.8 because these pac`
    - Line 58: `2025-11-15T04:43:04.8543776Z ERROR: ResolutionImpossible: for help visit https://pip.pypa.io/en/late`
    - Line 59: `2025-11-15T04:43:05.3721130Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 2
  - Sample matches:
    - Line 67: `2025-11-15T04:43:05.3788484Z [36;1m  echo "❌ Some tests failed - review logs" >> $GITHUB_STEP_SUMMA`
    - Line 97: `2025-11-15T04:43:05.5343792Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-11-15T04:43:05.5343792Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations
2. Review test cases and ensure code changes haven't broken existing functionality

---

### 2. WebSocket Health Tests (concurrent-connections, Concurrent Connections, 🔗)

**Status**: ❌ failure
**Category**: Test Failure
**Severity**: HIGH
**Started**: 2025-11-15T04:42:01Z
**Completed**: 2025-11-15T04:42:45Z
**Duration**: 44 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19384578932/job/55469493288)

#### Failed Steps

- **Step 5**: Install Python Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 45: `2025-11-15T04:42:43.6299096Z ERROR: Cannot install psutil==5.9.6 and psutil==5.9.8 because these pac`
    - Line 58: `2025-11-15T04:42:43.6313313Z ERROR: ResolutionImpossible: for help visit https://pip.pypa.io/en/late`
    - Line 59: `2025-11-15T04:42:44.0236954Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 2
  - Sample matches:
    - Line 67: `2025-11-15T04:42:44.0304013Z [36;1m  echo "❌ Some tests failed - review logs" >> $GITHUB_STEP_SUMMA`
    - Line 97: `2025-11-15T04:42:44.1830882Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-11-15T04:42:44.1830882Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations
2. Review test cases and ensure code changes haven't broken existing functionality

---

### 3. WebSocket Health Tests (self-healing, Self-Healing & Recovery, 🔄)

**Status**: ❌ failure
**Category**: Test Failure
**Severity**: HIGH
**Started**: 2025-11-15T04:42:02Z
**Completed**: 2025-11-15T04:42:51Z
**Duration**: 49 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19384578932/job/55469493289)

#### Failed Steps

- **Step 5**: Install Python Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 45: `2025-11-15T04:42:49.0791287Z ERROR: Cannot install psutil==5.9.6 and psutil==5.9.8 because these pac`
    - Line 58: `2025-11-15T04:42:49.0804970Z ERROR: ResolutionImpossible: for help visit https://pip.pypa.io/en/late`
    - Line 59: `2025-11-15T04:42:49.5883295Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 2
  - Sample matches:
    - Line 67: `2025-11-15T04:42:49.5946341Z [36;1m  echo "❌ Some tests failed - review logs" >> $GITHUB_STEP_SUMMA`
    - Line 97: `2025-11-15T04:42:49.7402258Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-11-15T04:42:49.7402258Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations
2. Review test cases and ensure code changes haven't broken existing functionality

---

### 4. WebSocket Health Tests (heartbeat-monitoring, Heartbeat & Health Monitoring, 💓)

**Status**: ❌ failure
**Category**: Test Failure
**Severity**: HIGH
**Started**: 2025-11-15T04:42:02Z
**Completed**: 2025-11-15T04:42:48Z
**Duration**: 46 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19384578932/job/55469493290)

#### Failed Steps

- **Step 5**: Install Python Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 45: `2025-11-15T04:42:46.6375671Z ERROR: Cannot install psutil==5.9.6 and psutil==5.9.8 because these pac`
    - Line 58: `2025-11-15T04:42:46.6390781Z ERROR: ResolutionImpossible: for help visit https://pip.pypa.io/en/late`
    - Line 59: `2025-11-15T04:42:47.0130046Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 2
  - Sample matches:
    - Line 67: `2025-11-15T04:42:47.0198173Z [36;1m  echo "❌ Some tests failed - review logs" >> $GITHUB_STEP_SUMMA`
    - Line 97: `2025-11-15T04:42:47.1740926Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-11-15T04:42:47.1740926Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations
2. Review test cases and ensure code changes haven't broken existing functionality

---

### 5. WebSocket Health Tests (latency-performance, Latency & Performance, ⚡)

**Status**: ❌ failure
**Category**: Test Failure
**Severity**: HIGH
**Started**: 2025-11-15T04:42:02Z
**Completed**: 2025-11-15T04:42:55Z
**Duration**: 53 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19384578932/job/55469493293)

#### Failed Steps

- **Step 5**: Install Python Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 45: `2025-11-15T04:42:52.5741538Z ERROR: Cannot install psutil==5.9.6 and psutil==5.9.8 because these pac`
    - Line 58: `2025-11-15T04:42:52.5756863Z ERROR: ResolutionImpossible: for help visit https://pip.pypa.io/en/late`
    - Line 59: `2025-11-15T04:42:53.0839833Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 2
  - Sample matches:
    - Line 67: `2025-11-15T04:42:53.0908741Z [36;1m  echo "❌ Some tests failed - review logs" >> $GITHUB_STEP_SUMMA`
    - Line 97: `2025-11-15T04:42:53.2449906Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-11-15T04:42:53.2449906Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations
2. Review test cases and ensure code changes haven't broken existing functionality

---

### 6. WebSocket Health Tests (message-delivery, Message Delivery & Reliability, 📨)

**Status**: ❌ failure
**Category**: Test Failure
**Severity**: HIGH
**Started**: 2025-11-15T04:42:02Z
**Completed**: 2025-11-15T04:42:56Z
**Duration**: 54 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19384578932/job/55469493296)

#### Failed Steps

- **Step 5**: Install Python Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 45: `2025-11-15T04:42:53.4167889Z ERROR: Cannot install psutil==5.9.6 and psutil==5.9.8 because these pac`
    - Line 58: `2025-11-15T04:42:53.4182459Z ERROR: ResolutionImpossible: for help visit https://pip.pypa.io/en/late`
    - Line 59: `2025-11-15T04:42:53.8966217Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 2
  - Sample matches:
    - Line 67: `2025-11-15T04:42:53.9034965Z [36;1m  echo "❌ Some tests failed - review logs" >> $GITHUB_STEP_SUMMA`
    - Line 97: `2025-11-15T04:42:54.0573935Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-11-15T04:42:54.0573935Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

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

📊 *Report generated on 2025-11-15T04:44:12.284273*
🤖 *JARVIS CI/CD Auto-PR Manager*
