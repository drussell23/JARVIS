# CI/CD Failure Analysis Report

## Executive Summary

- **Workflow**: PR Automation & Validation
- **Run Number**: #101367
- **Branch**: `fix/ci/pr-automation-validation-run101337-20260528-105040`
- **Commit**: `30a97f528369a7190fcabd2f7fcc6b89821225e5`
- **Status**: ❌ FAILED
- **Timestamp**: 2026-05-28T10:51:16Z
- **Triggered By**: @cubic-dev-ai[bot]
- **Workflow URL**: [View Run](https://github.com/drussell23/JARVIS/actions/runs/26570259692)

## Failure Overview

Total Failed Jobs: **5**

| # | Job Name | Category | Severity | Duration |
|---|----------|----------|----------|----------|
| 1 | Check PR Description | permission_error | high | 3s |
| 2 | PR Size Check | permission_error | high | 2s |
| 3 | Validate PR Title | timeout | high | 5s |
| 4 | Auto-Label PR | permission_error | high | 10s |
| 5 | Check for Conflicts | permission_error | high | 3s |

## Detailed Analysis

### 1. Check PR Description

**Status**: ❌ failure
**Category**: Permission Error
**Severity**: HIGH
**Started**: 2026-05-28T10:51:24Z
**Completed**: 2026-05-28T10:51:27Z
**Duration**: 3 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26570259692/job/78275276474)

#### Failed Steps

- **Step 2**: Verify PR Description

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 2
  - Sample matches:
    - Line 43: `2026-05-28T10:51:26.0421632Z RequestError [HttpError]: API rate limit exceeded for installation. If `
    - Line 44: `2026-05-28T10:51:26.0462772Z ##[error]Unhandled error: HttpError: API rate limit exceeded for instal`

#### Suggested Fixes

1. Review the logs above for specific error messages

---

### 2. PR Size Check

**Status**: ❌ failure
**Category**: Permission Error
**Severity**: HIGH
**Started**: 2026-05-28T10:51:21Z
**Completed**: 2026-05-28T10:51:23Z
**Duration**: 2 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26570259692/job/78275276507)

#### Failed Steps

- **Step 2**: Check PR Size

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 2
  - Sample matches:
    - Line 45: `2026-05-28T10:51:23.0455057Z RequestError [HttpError]: API rate limit exceeded for installation. If `
    - Line 64: `2026-05-28T10:51:23.0514302Z ##[error]Unhandled error: HttpError: API rate limit exceeded for instal`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 1
  - Sample matches:
    - Line 1: `${warning}`

#### Suggested Fixes

1. Review the logs above for specific error messages

---

### 3. Validate PR Title

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2026-05-28T10:51:26Z
**Completed**: 2026-05-28T10:51:31Z
**Duration**: 5 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26570259692/job/78275276537)

#### Failed Steps

- **Step 2**: Validate Conventional Commits

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 2
  - Sample matches:
    - Line 16: `2026-05-28T10:51:28.4460211Z   subjectPatternError: The PR title must start with a capital letter.`
    - Line 28: `2026-05-28T10:51:28.7699536Z ##[error]API rate limit exceeded for installation. If you reach out to `

- Pattern: `WARN|Warning|warning`
  - Occurrences: 1
  - Sample matches:
    - Line 30: `2026-05-28T10:51:28.8227285Z ##[warning]Node.js 20 actions are deprecated. The following actions are`

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
**Started**: 2026-05-28T10:51:45Z
**Completed**: 2026-05-28T10:51:55Z
**Duration**: 10 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26570259692/job/78275276556)

#### Failed Steps

- **Step 4**: Intelligent Auto-Labeling

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 2
  - Sample matches:
    - Line 33: `2026-05-28T10:51:54.4386257Z ##[error]Unhandled error: HttpError: API rate limit exceeded for instal`
    - Line 34: `2026-05-28T10:51:54.4391599Z RequestError [HttpError]: API rate limit exceeded for installation. If `

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2026-05-28T10:51:54.5859923Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2026-05-28T10:51:54.5859923Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

#### Suggested Fixes

1. Review the logs above for specific error messages

---

### 5. Check for Conflicts

**Status**: ❌ failure
**Category**: Permission Error
**Severity**: HIGH
**Started**: 2026-05-28T10:51:39Z
**Completed**: 2026-05-28T10:51:42Z
**Duration**: 3 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26570259692/job/78275276677)

#### Failed Steps

- **Step 2**: Check Merge Conflicts

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 2
  - Sample matches:
    - Line 45: `2026-05-28T10:51:41.3764952Z RequestError [HttpError]: API rate limit exceeded for installation. If `
    - Line 46: `2026-05-28T10:51:41.3808837Z ##[error]Unhandled error: HttpError: API rate limit exceeded for instal`

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

📊 *Report generated on 2026-05-28T10:53:59.324364*
🤖 *JARVIS CI/CD Auto-PR Manager*
