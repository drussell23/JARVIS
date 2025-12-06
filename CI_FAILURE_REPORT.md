# CI/CD Failure Analysis Report

## Executive Summary

- **Workflow**: Unlock Integration E2E Testing
- **Run Number**: #153
- **Branch**: `cursor/investigate-voice-authentication-system-integration-gemini-3-pro-preview-e56d`
- **Commit**: `d4047c9067920a1dffef2b81142a39b3218d414c`
- **Status**: ❌ FAILED
- **Timestamp**: 2025-12-06T06:06:21Z
- **Triggered By**: @cursor[bot]
- **Workflow URL**: [View Run](https://github.com/drussell23/JARVIS/actions/runs/19984381990)

## Failure Overview

Total Failed Jobs: **2**

| # | Job Name | Category | Severity | Duration |
|---|----------|----------|----------|----------|
| 1 | Mock Tests - security-checks | test_failure | high | 46s |
| 2 | Generate Test Summary | test_failure | high | 8s |

## Detailed Analysis

### 1. Mock Tests - security-checks

**Status**: ❌ failure
**Category**: Test Failure
**Severity**: HIGH
**Started**: 2025-12-06T06:08:17Z
**Completed**: 2025-12-06T06:09:03Z
**Duration**: 46 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984381990/job/57316096114)

#### Failed Steps

- **Step 6**: Run Mock Tests

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 2
  - Sample matches:
    - Line 39: `2025-12-06T06:09:01.2499665Z 2025-12-06 06:09:01,249 - __main__ - ERROR - ❌ 1 test(s) failed`
    - Line 56: `2025-12-06T06:09:01.2629587Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 3
  - Sample matches:
    - Line 39: `2025-12-06T06:09:01.2499665Z 2025-12-06 06:09:01,249 - __main__ - ERROR - ❌ 1 test(s) failed`
    - Line 48: `2025-12-06T06:09:01.2507576Z ❌ Failed: 1`
    - Line 97: `2025-12-06T06:09:02.2917069Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 2
  - Sample matches:
    - Line 62: `2025-12-06T06:09:01.2705417Z   if-no-files-found: warn`
    - Line 97: `2025-12-06T06:09:02.2917069Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations
2. Review test cases and ensure code changes haven't broken existing functionality

---

### 2. Generate Test Summary

**Status**: ❌ failure
**Category**: Test Failure
**Severity**: HIGH
**Started**: 2025-12-06T06:09:44Z
**Completed**: 2025-12-06T06:09:52Z
**Duration**: 8 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984381990/job/57316182153)

#### Failed Steps

- **Step 4**: Check Test Status

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 2
  - Sample matches:
    - Line 1: `2025-12-06T06:09:48.2655059Z Starting download of artifact to: /home/runner/work/JARVIS/JARVIS/all-r`
    - Line 97: `2025-12-06T06:09:48.7628210Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 13
  - Sample matches:
    - Line 39: `2025-12-06T06:09:48.5914664Z [36;1mTOTAL_FAILED=0[0m`
    - Line 44: `2025-12-06T06:09:48.5916619Z [36;1m    FAILED=$(jq -r '.summary.failed' "$report")[0m`
    - Line 46: `2025-12-06T06:09:48.5917452Z [36;1m    TOTAL_FAILED=$((TOTAL_FAILED + FAILED))[0m`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 2
  - Sample matches:
    - Line 4: `2025-12-06T06:09:48.3591185Z (node:1990) [DEP0005] DeprecationWarning: Buffer() is deprecated due to`
    - Line 5: `2025-12-06T06:09:48.3592685Z (Use `node --trace-deprecation ...` to show where the warning was creat`

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

📊 *Report generated on 2025-12-06T06:11:14.930817*
🤖 *JARVIS CI/CD Auto-PR Manager*
