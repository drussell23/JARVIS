# CI/CD Failure Analysis Report

## Executive Summary

- **Workflow**: PR Automation & Validation
- **Run Number**: #101371
- **Branch**: `fix/ci/pr-automation-validation-run101350-20260528-105057`
- **Commit**: `b8d39e1caba10b652112570365bbd6852dff2d64`
- **Status**: ❌ FAILED
- **Timestamp**: 2026-05-28T10:51:25Z
- **Triggered By**: @cubic-dev-ai[bot]
- **Workflow URL**: [View Run](https://github.com/drussell23/JARVIS/actions/runs/26570266595)

## Failure Overview

Total Failed Jobs: **5**

| # | Job Name | Category | Severity | Duration |
|---|----------|----------|----------|----------|
| 1 | PR Size Check | permission_error | high | 5s |
| 2 | Check for Conflicts | permission_error | high | 3s |
| 3 | Validate PR Title | timeout | high | 5s |
| 4 | Check PR Description | permission_error | high | 3s |
| 5 | Auto-Label PR | permission_error | high | 11s |

## Detailed Analysis

### 1. PR Size Check

**Status**: ❌ failure
**Category**: Permission Error
**Severity**: HIGH
**Started**: 2026-05-28T10:51:55Z
**Completed**: 2026-05-28T10:52:00Z
**Duration**: 5 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26570266595/job/78275298373)

#### Failed Steps

- **Step 2**: Check PR Size

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 2
  - Sample matches:
    - Line 45: `2026-05-28T10:51:58.3058175Z RequestError [HttpError]: API rate limit exceeded for installation. If `
    - Line 46: `2026-05-28T10:51:58.3092090Z ##[error]Unhandled error: HttpError: API rate limit exceeded for instal`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 1
  - Sample matches:
    - Line 1: `${warning}`

#### Suggested Fixes

1. Review the logs above for specific error messages

---

### 2. Check for Conflicts

**Status**: ❌ failure
**Category**: Permission Error
**Severity**: HIGH
**Started**: 2026-05-28T10:51:54Z
**Completed**: 2026-05-28T10:51:57Z
**Duration**: 3 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26570266595/job/78275298382)

#### Failed Steps

- **Step 2**: Check Merge Conflicts

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 2
  - Sample matches:
    - Line 45: `2026-05-28T10:51:56.2805420Z RequestError [HttpError]: API rate limit exceeded for installation. If `
    - Line 46: `2026-05-28T10:51:56.2865673Z ##[error]Unhandled error: HttpError: API rate limit exceeded for instal`

#### Suggested Fixes

1. Review the logs above for specific error messages

---

### 3. Validate PR Title

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2026-05-28T10:51:55Z
**Completed**: 2026-05-28T10:52:00Z
**Duration**: 5 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26570266595/job/78275298391)

#### Failed Steps

- **Step 2**: Validate Conventional Commits

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 2
  - Sample matches:
    - Line 16: `2026-05-28T10:51:57.9521101Z   subjectPatternError: The PR title must start with a capital letter.`
    - Line 28: `2026-05-28T10:51:58.2448217Z ##[error]API rate limit exceeded for installation. If you reach out to `

- Pattern: `WARN|Warning|warning`
  - Occurrences: 1
  - Sample matches:
    - Line 30: `2026-05-28T10:51:58.2905445Z ##[warning]Node.js 20 actions are deprecated. The following actions are`

- Pattern: `timeout|timed out`
  - Occurrences: 1
  - Sample matches:
    - Line 20: `- fix: Resolve database connection timeout`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations
2. Check service availability and network connectivity

---

### 4. Check PR Description

**Status**: ❌ failure
**Category**: Permission Error
**Severity**: HIGH
**Started**: 2026-05-28T10:52:00Z
**Completed**: 2026-05-28T10:52:03Z
**Duration**: 3 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26570266595/job/78275298403)

#### Failed Steps

- **Step 2**: Verify PR Description

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 2
  - Sample matches:
    - Line 43: `2026-05-28T10:52:02.3049647Z RequestError [HttpError]: API rate limit exceeded for installation. If `
    - Line 44: `2026-05-28T10:52:02.3103530Z ##[error]Unhandled error: HttpError: API rate limit exceeded for instal`

#### Suggested Fixes

1. Review the logs above for specific error messages

---

### 5. Auto-Label PR

**Status**: ❌ failure
**Category**: Permission Error
**Severity**: HIGH
**Started**: 2026-05-28T10:52:03Z
**Completed**: 2026-05-28T10:52:14Z
**Duration**: 11 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26570266595/job/78275298470)

#### Failed Steps

- **Step 4**: Intelligent Auto-Labeling

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 2
  - Sample matches:
    - Line 33: `2026-05-28T10:52:12.6664129Z RequestError [HttpError]: API rate limit exceeded for installation. If `
    - Line 87: `2026-05-28T10:52:12.6707743Z ##[error]Unhandled error: HttpError: API rate limit exceeded for instal`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2026-05-28T10:52:12.8226182Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2026-05-28T10:52:12.8226182Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

#### Suggested Fixes

1. Review the logs above for specific error messages

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

📊 *Report generated on 2026-05-28T10:54:20.007502*
🤖 *JARVIS CI/CD Auto-PR Manager*
