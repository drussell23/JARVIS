# CI/CD Failure Analysis Report

## Executive Summary

- **Workflow**: PR Automation & Validation
- **Run Number**: #101369
- **Branch**: `fix/ci/pr-automation-validation-run101346-20260528-105053`
- **Commit**: `1746a10323c47e90539f3a2bd21ba1a93030cd36`
- **Status**: ❌ FAILED
- **Timestamp**: 2026-05-28T10:51:24Z
- **Triggered By**: @cubic-dev-ai[bot]
- **Workflow URL**: [View Run](https://github.com/drussell23/JARVIS/actions/runs/26570265792)

## Failure Overview

Total Failed Jobs: **5**

| # | Job Name | Category | Severity | Duration |
|---|----------|----------|----------|----------|
| 1 | Check PR Description | permission_error | high | 4s |
| 2 | Check for Conflicts | permission_error | high | 4s |
| 3 | Validate PR Title | timeout | high | 4s |
| 4 | Auto-Label PR | permission_error | high | 13s |
| 5 | PR Size Check | permission_error | high | 5s |

## Detailed Analysis

### 1. Check PR Description

**Status**: ❌ failure
**Category**: Permission Error
**Severity**: HIGH
**Started**: 2026-05-28T10:51:33Z
**Completed**: 2026-05-28T10:51:37Z
**Duration**: 4 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26570265792/job/78275295900)

#### Failed Steps

- **Step 2**: Verify PR Description

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 2
  - Sample matches:
    - Line 43: `2026-05-28T10:51:35.6553278Z RequestError [HttpError]: API rate limit exceeded for installation. If `
    - Line 44: `2026-05-28T10:51:35.6606099Z ##[error]Unhandled error: HttpError: API rate limit exceeded for instal`

#### Suggested Fixes

1. Review the logs above for specific error messages

---

### 2. Check for Conflicts

**Status**: ❌ failure
**Category**: Permission Error
**Severity**: HIGH
**Started**: 2026-05-28T10:51:31Z
**Completed**: 2026-05-28T10:51:35Z
**Duration**: 4 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26570265792/job/78275295907)

#### Failed Steps

- **Step 2**: Check Merge Conflicts

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 2
  - Sample matches:
    - Line 45: `2026-05-28T10:51:33.8742786Z RequestError [HttpError]: API rate limit exceeded for installation. If `
    - Line 46: `2026-05-28T10:51:33.8780918Z ##[error]Unhandled error: HttpError: API rate limit exceeded for instal`

#### Suggested Fixes

1. Review the logs above for specific error messages

---

### 3. Validate PR Title

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2026-05-28T10:51:32Z
**Completed**: 2026-05-28T10:51:36Z
**Duration**: 4 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26570265792/job/78275295910)

#### Failed Steps

- **Step 2**: Validate Conventional Commits

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 2
  - Sample matches:
    - Line 16: `2026-05-28T10:51:34.3579850Z   subjectPatternError: The PR title must start with a capital letter.`
    - Line 28: `2026-05-28T10:51:34.6965151Z ##[error]API rate limit exceeded for installation. If you reach out to `

- Pattern: `WARN|Warning|warning`
  - Occurrences: 1
  - Sample matches:
    - Line 30: `2026-05-28T10:51:34.7385827Z ##[warning]Node.js 20 actions are deprecated. The following actions are`

- Pattern: `timeout|timed out`
  - Occurrences: 1
  - Sample matches:
    - Line 20: `- fix: Resolve database connection timeout`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations
2. Check service availability and network connectivity

---

### 4. Auto-Label PR

**Status**: ❌ failure
**Category**: Permission Error
**Severity**: HIGH
**Started**: 2026-05-28T10:51:44Z
**Completed**: 2026-05-28T10:51:57Z
**Duration**: 13 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26570265792/job/78275295980)

#### Failed Steps

- **Step 4**: Intelligent Auto-Labeling

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 2
  - Sample matches:
    - Line 33: `2026-05-28T10:51:55.0623863Z RequestError [HttpError]: API rate limit exceeded for installation. If `
    - Line 87: `2026-05-28T10:51:55.0655947Z ##[error]Unhandled error: HttpError: API rate limit exceeded for instal`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2026-05-28T10:51:55.2063431Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2026-05-28T10:51:55.2063431Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

#### Suggested Fixes

1. Review the logs above for specific error messages

---

### 5. PR Size Check

**Status**: ❌ failure
**Category**: Permission Error
**Severity**: HIGH
**Started**: 2026-05-28T10:51:48Z
**Completed**: 2026-05-28T10:51:53Z
**Duration**: 5 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26570265792/job/78275296117)

#### Failed Steps

- **Step 2**: Check PR Size

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 2
  - Sample matches:
    - Line 45: `2026-05-28T10:51:51.0514154Z RequestError [HttpError]: API rate limit exceeded for installation. If `
    - Line 97: `2026-05-28T10:51:51.0609667Z ##[error]Unhandled error: HttpError: API rate limit exceeded for instal`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 1
  - Sample matches:
    - Line 1: `${warning}`

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

📊 *Report generated on 2026-05-28T10:54:08.380281*
🤖 *JARVIS CI/CD Auto-PR Manager*
