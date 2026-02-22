# CI/CD Failure Analysis Report

## Executive Summary

- **Workflow**: WebSocket Self-Healing Validation
- **Run Number**: #11
- **Branch**: `main`
- **Commit**: `c96d3fe81af341aa058ec32544c3559f5f4275b6`
- **Status**: ❌ FAILED
- **Timestamp**: 2025-11-09T03:06:36Z
- **Triggered By**: @drussell23
- **Workflow URL**: [View Run](https://github.com/drussell23/JARVIS/actions/runs/19202473978)

## Failure Overview

Total Failed Jobs: **6**

| # | Job Name | Category | Severity | Duration |
|---|----------|----------|----------|----------|
| 1 | WebSocket Health Tests (message-delivery, Message Delivery & Reliability, 📨) | test_failure | high | 33s |
| 2 | WebSocket Health Tests (connection-lifecycle, Connection Lifecycle, 🔌) | test_failure | high | 30s |
| 3 | WebSocket Health Tests (latency-performance, Latency & Performance, ⚡) | test_failure | high | 31s |
| 4 | WebSocket Health Tests (heartbeat-monitoring, Heartbeat & Health Monitoring, 💓) | test_failure | high | 33s |
| 5 | WebSocket Health Tests (concurrent-connections, Concurrent Connections, 🔗) | test_failure | high | 29s |
| 6 | WebSocket Health Tests (self-healing, Self-Healing & Recovery, 🔄) | test_failure | high | 29s |

## Detailed Analysis

### 1. WebSocket Health Tests (message-delivery, Message Delivery & Reliability, 📨)

**Status**: ❌ failure
**Category**: Test Failure
**Severity**: HIGH
**Started**: 2025-11-09T03:06:47Z
**Completed**: 2025-11-09T03:07:20Z
**Duration**: 33 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19202473978/job/54892557569)

#### Failed Steps

- **Step 5**: Install Python Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 45: `2025-11-09T03:07:16.8165588Z ERROR: Cannot install psutil==5.9.6 and psutil==5.9.8 because these pac`
    - Line 58: `2025-11-09T03:07:16.8180202Z ERROR: ResolutionImpossible: for help visit https://pip.pypa.io/en/late`
    - Line 59: `2025-11-09T03:07:17.3395144Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 2
  - Sample matches:
    - Line 67: `2025-11-09T03:07:17.3462355Z [36;1m  echo "❌ Some tests failed - review logs" >> $GITHUB_STEP_SUMMA`
    - Line 97: `2025-11-09T03:07:17.5003952Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-11-09T03:07:17.5003952Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations
2. Review test cases and ensure code changes haven't broken existing functionality

---

### 2. WebSocket Health Tests (connection-lifecycle, Connection Lifecycle, 🔌)

**Status**: ❌ failure
**Category**: Test Failure
**Severity**: HIGH
**Started**: 2025-11-09T03:06:47Z
**Completed**: 2025-11-09T03:07:17Z
**Duration**: 30 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19202473978/job/54892557571)

#### Failed Steps

- **Step 5**: Install Python Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 45: `2025-11-09T03:07:14.8006091Z ERROR: Cannot install psutil==5.9.6 and psutil==5.9.8 because these pac`
    - Line 58: `2025-11-09T03:07:14.8020979Z ERROR: ResolutionImpossible: for help visit https://pip.pypa.io/en/late`
    - Line 59: `2025-11-09T03:07:15.0892025Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 2
  - Sample matches:
    - Line 67: `2025-11-09T03:07:15.0953446Z [36;1m  echo "❌ Some tests failed - review logs" >> $GITHUB_STEP_SUMMA`
    - Line 97: `2025-11-09T03:07:15.2336866Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-11-09T03:07:15.2336866Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations
2. Review test cases and ensure code changes haven't broken existing functionality

---

### 3. WebSocket Health Tests (latency-performance, Latency & Performance, ⚡)

**Status**: ❌ failure
**Category**: Test Failure
**Severity**: HIGH
**Started**: 2025-11-09T03:06:47Z
**Completed**: 2025-11-09T03:07:18Z
**Duration**: 31 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19202473978/job/54892557572)

#### Failed Steps

- **Step 5**: Install Python Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 45: `2025-11-09T03:07:15.2429433Z ERROR: Cannot install psutil==5.9.6 and psutil==5.9.8 because these pac`
    - Line 58: `2025-11-09T03:07:15.2444822Z ERROR: ResolutionImpossible: for help visit https://pip.pypa.io/en/late`
    - Line 59: `2025-11-09T03:07:15.6676142Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 2
  - Sample matches:
    - Line 67: `2025-11-09T03:07:15.6743560Z [36;1m  echo "❌ Some tests failed - review logs" >> $GITHUB_STEP_SUMMA`
    - Line 97: `2025-11-09T03:07:15.8285495Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-11-09T03:07:15.8285495Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations
2. Review test cases and ensure code changes haven't broken existing functionality

---

### 4. WebSocket Health Tests (heartbeat-monitoring, Heartbeat & Health Monitoring, 💓)

**Status**: ❌ failure
**Category**: Test Failure
**Severity**: HIGH
**Started**: 2025-11-09T03:06:47Z
**Completed**: 2025-11-09T03:07:20Z
**Duration**: 33 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19202473978/job/54892557575)

#### Failed Steps

- **Step 5**: Install Python Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 45: `2025-11-09T03:07:17.0870615Z ERROR: Cannot install psutil==5.9.6 and psutil==5.9.8 because these pac`
    - Line 58: `2025-11-09T03:07:17.0885349Z ERROR: ResolutionImpossible: for help visit https://pip.pypa.io/en/late`
    - Line 59: `2025-11-09T03:07:17.5239391Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 2
  - Sample matches:
    - Line 67: `2025-11-09T03:07:17.5305764Z [36;1m  echo "❌ Some tests failed - review logs" >> $GITHUB_STEP_SUMMA`
    - Line 97: `2025-11-09T03:07:17.6825460Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-11-09T03:07:17.6825460Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations
2. Review test cases and ensure code changes haven't broken existing functionality

---

### 5. WebSocket Health Tests (concurrent-connections, Concurrent Connections, 🔗)

**Status**: ❌ failure
**Category**: Test Failure
**Severity**: HIGH
**Started**: 2025-11-09T03:06:47Z
**Completed**: 2025-11-09T03:07:16Z
**Duration**: 29 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19202473978/job/54892557576)

#### Failed Steps

- **Step 5**: Install Python Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 45: `2025-11-09T03:07:12.8704433Z ERROR: Cannot install psutil==5.9.6 and psutil==5.9.8 because these pac`
    - Line 58: `2025-11-09T03:07:12.8719016Z ERROR: ResolutionImpossible: for help visit https://pip.pypa.io/en/late`
    - Line 59: `2025-11-09T03:07:13.2438783Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 2
  - Sample matches:
    - Line 67: `2025-11-09T03:07:13.2505798Z [36;1m  echo "❌ Some tests failed - review logs" >> $GITHUB_STEP_SUMMA`
    - Line 97: `2025-11-09T03:07:13.4001201Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-11-09T03:07:13.4001201Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations
2. Review test cases and ensure code changes haven't broken existing functionality

---

### 6. WebSocket Health Tests (self-healing, Self-Healing & Recovery, 🔄)

**Status**: ❌ failure
**Category**: Test Failure
**Severity**: HIGH
**Started**: 2025-11-09T03:06:47Z
**Completed**: 2025-11-09T03:07:16Z
**Duration**: 29 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19202473978/job/54892557587)

#### Failed Steps

- **Step 5**: Install Python Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 45: `2025-11-09T03:07:14.5513512Z ERROR: Cannot install psutil==5.9.6 and psutil==5.9.8 because these pac`
    - Line 58: `2025-11-09T03:07:14.5529237Z ERROR: ResolutionImpossible: for help visit https://pip.pypa.io/en/late`
    - Line 59: `2025-11-09T03:07:15.0563152Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 2
  - Sample matches:
    - Line 67: `2025-11-09T03:07:15.0630339Z [36;1m  echo "❌ Some tests failed - review logs" >> $GITHUB_STEP_SUMMA`
    - Line 97: `2025-11-09T03:07:15.2101151Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-11-09T03:07:15.2101151Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

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

📊 *Report generated on 2025-11-09T03:08:09.538108*
🤖 *JARVIS CI/CD Auto-PR Manager*
