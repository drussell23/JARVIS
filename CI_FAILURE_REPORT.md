# CI/CD Failure Analysis Report

## Executive Summary

- **Workflow**: Unlock Integration E2E Testing
- **Run Number**: #154
- **Branch**: `cursor/investigate-voice-authentication-system-integration-gemini-3-pro-preview-e56d`
- **Commit**: `e1086ec42a310b89ceabbeec7014a49fe2d54b6d`
- **Status**: ❌ FAILED
- **Timestamp**: 2025-12-06T06:10:29Z
- **Triggered By**: @cursor[bot]
- **Workflow URL**: [View Run](https://github.com/drussell23/JARVIS/actions/runs/19984432898)

## Failure Overview

Total Failed Jobs: **2**

| # | Job Name | Category | Severity | Duration |
|---|----------|----------|----------|----------|
| 1 | Mock Tests - security-checks | test_failure | high | 48s |
| 2 | Generate Test Summary | test_failure | high | 6s |

## Detailed Analysis

### 1. Mock Tests - security-checks

**Status**: ❌ failure
**Category**: Test Failure
**Severity**: HIGH
**Started**: 2025-12-06T06:12:36Z
**Completed**: 2025-12-06T06:13:24Z
**Duration**: 48 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984432898/job/57316248705)

#### Failed Steps

- **Step 6**: Run Mock Tests

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 2
  - Sample matches:
    - Line 39: `2025-12-06T06:13:20.7816909Z 2025-12-06 06:13:20,781 - __main__ - ERROR - ❌ 1 test(s) failed`
    - Line 56: `2025-12-06T06:13:20.7928787Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 3
  - Sample matches:
    - Line 39: `2025-12-06T06:13:20.7816909Z 2025-12-06 06:13:20,781 - __main__ - ERROR - ❌ 1 test(s) failed`
    - Line 48: `2025-12-06T06:13:20.7823960Z ❌ Failed: 1`
    - Line 97: `2025-12-06T06:13:21.9322589Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 2
  - Sample matches:
    - Line 62: `2025-12-06T06:13:20.7999994Z   if-no-files-found: warn`
    - Line 97: `2025-12-06T06:13:21.9322589Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations
2. Review test cases and ensure code changes haven't broken existing functionality

---

### 2. Generate Test Summary

**Status**: ❌ failure
**Category**: Test Failure
**Severity**: HIGH
**Started**: 2025-12-06T06:14:21Z
**Completed**: 2025-12-06T06:14:27Z
**Duration**: 6 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984432898/job/57316319161)

#### Failed Steps

- **Step 4**: Check Test Status

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 2
  - Sample matches:
    - Line 1: `2025-12-06T06:14:24.8452344Z Starting download of artifact to: /home/runner/work/JARVIS/JARVIS/all-r`
    - Line 97: `2025-12-06T06:14:25.2175671Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 13
  - Sample matches:
    - Line 39: `2025-12-06T06:14:25.0492433Z [36;1mTOTAL_FAILED=0[0m`
    - Line 44: `2025-12-06T06:14:25.0494458Z [36;1m    FAILED=$(jq -r '.summary.failed' "$report")[0m`
    - Line 46: `2025-12-06T06:14:25.0495316Z [36;1m    TOTAL_FAILED=$((TOTAL_FAILED + FAILED))[0m`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 2
  - Sample matches:
    - Line 4: `2025-12-06T06:14:24.9170352Z (node:1994) [DEP0005] DeprecationWarning: Buffer() is deprecated due to`
    - Line 5: `2025-12-06T06:14:24.9172248Z (Use `node --trace-deprecation ...` to show where the warning was creat`

#### Suggested Fixes

1. Review test cases and ensure code changes haven't broken existing functionality

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

📊 *Report generated on 2025-12-06T06:15:53.741780*
🤖 *JARVIS CI/CD Auto-PR Manager*
