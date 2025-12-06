# CI/CD Failure Analysis Report

## Executive Summary

- **Workflow**: Unlock Integration E2E Testing
- **Run Number**: #155
- **Branch**: `cursor/investigate-voice-authentication-system-integration-gemini-3-pro-preview-e56d`
- **Commit**: `1f39d702bfbd2938d342f2fa5b6f57bdde8c8dd1`
- **Status**: ❌ FAILED
- **Timestamp**: 2025-12-06T06:15:49Z
- **Triggered By**: @cursor[bot]
- **Workflow URL**: [View Run](https://github.com/drussell23/JARVIS/actions/runs/19984493817)

## Failure Overview

Total Failed Jobs: **2**

| # | Job Name | Category | Severity | Duration |
|---|----------|----------|----------|----------|
| 1 | Mock Tests - security-checks | test_failure | high | 54s |
| 2 | Generate Test Summary | test_failure | high | 4s |

## Detailed Analysis

### 1. Mock Tests - security-checks

**Status**: ❌ failure
**Category**: Test Failure
**Severity**: HIGH
**Started**: 2025-12-06T06:17:44Z
**Completed**: 2025-12-06T06:18:38Z
**Duration**: 54 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984493817/job/57316411400)

#### Failed Steps

- **Step 6**: Run Mock Tests

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 2
  - Sample matches:
    - Line 39: `2025-12-06T06:18:34.2842564Z 2025-12-06 06:18:34,284 - __main__ - ERROR - ❌ 1 test(s) failed`
    - Line 56: `2025-12-06T06:18:34.2951781Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 3
  - Sample matches:
    - Line 39: `2025-12-06T06:18:34.2842564Z 2025-12-06 06:18:34,284 - __main__ - ERROR - ❌ 1 test(s) failed`
    - Line 48: `2025-12-06T06:18:34.2850634Z ❌ Failed: 1`
    - Line 97: `2025-12-06T06:18:35.5009634Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 2
  - Sample matches:
    - Line 62: `2025-12-06T06:18:34.3023751Z   if-no-files-found: warn`
    - Line 97: `2025-12-06T06:18:35.5009634Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations
2. Review test cases and ensure code changes haven't broken existing functionality

---

### 2. Generate Test Summary

**Status**: ❌ failure
**Category**: Test Failure
**Severity**: HIGH
**Started**: 2025-12-06T06:20:02Z
**Completed**: 2025-12-06T06:20:06Z
**Duration**: 4 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984493817/job/57316495727)

#### Failed Steps

- **Step 4**: Check Test Status

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:20:05.0340059Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 13
  - Sample matches:
    - Line 39: `2025-12-06T06:20:04.8460182Z [36;1mTOTAL_FAILED=0[0m`
    - Line 44: `2025-12-06T06:20:04.8464138Z [36;1m    FAILED=$(jq -r '.summary.failed' "$report")[0m`
    - Line 46: `2025-12-06T06:20:04.8465854Z [36;1m    TOTAL_FAILED=$((TOTAL_FAILED + FAILED))[0m`

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

📊 *Report generated on 2025-12-06T06:22:31.343269*
🤖 *JARVIS CI/CD Auto-PR Manager*
