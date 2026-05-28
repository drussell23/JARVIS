# CI/CD Failure Analysis Report

## Executive Summary

- **Workflow**: PR Automation & Validation
- **Run Number**: #101361
- **Branch**: `fix/ci/pr-automation-validation-run101353-20260528-105038`
- **Commit**: `8903a42bcae52a9fe6546d6e2e4ce286a70f6bb1`
- **Status**: ❌ FAILED
- **Timestamp**: 2026-05-28T10:51:08Z
- **Triggered By**: @cubic-dev-ai[bot]
- **Workflow URL**: [View Run](https://github.com/drussell23/JARVIS/actions/runs/26570253641)

## Failure Overview

Total Failed Jobs: **5**

| # | Job Name | Category | Severity | Duration |
|---|----------|----------|----------|----------|
| 1 | Check PR Description | permission_error | high | 5s |
| 2 | Validate PR Title | timeout | high | 3s |
| 3 | PR Size Check | permission_error | high | 4s |
| 4 | Auto-Label PR | permission_error | high | 11s |
| 5 | Check for Conflicts | permission_error | high | 4s |

## Detailed Analysis

### 1. Check PR Description

**Status**: ❌ failure
**Category**: Permission Error
**Severity**: HIGH
**Started**: 2026-05-28T10:51:16Z
**Completed**: 2026-05-28T10:51:21Z
**Duration**: 5 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26570253641/job/78275255653)

#### Failed Steps

- **Step 2**: Verify PR Description

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 2
  - Sample matches:
    - Line 43: `2026-05-28T10:51:18.9038609Z RequestError [HttpError]: API rate limit exceeded for installation. If `
    - Line 44: `2026-05-28T10:51:18.9093919Z ##[error]Unhandled error: HttpError: API rate limit exceeded for instal`

#### Suggested Fixes

1. Review the logs above for specific error messages

---

### 2. Validate PR Title

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2026-05-28T10:51:18Z
**Completed**: 2026-05-28T10:51:21Z
**Duration**: 3 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26570253641/job/78275255673)

#### Failed Steps

- **Step 2**: Validate Conventional Commits

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 2
  - Sample matches:
    - Line 16: `2026-05-28T10:51:19.7723203Z   subjectPatternError: The PR title must start with a capital letter.`
    - Line 28: `2026-05-28T10:51:20.0009386Z ##[error]API rate limit exceeded for installation. If you reach out to `

- Pattern: `WARN|Warning|warning`
  - Occurrences: 1
  - Sample matches:
    - Line 30: `2026-05-28T10:51:20.0483373Z ##[warning]Node.js 20 actions are deprecated. The following actions are`

- Pattern: `timeout|timed out`
  - Occurrences: 1
  - Sample matches:
    - Line 20: `- fix: Resolve database connection timeout`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations
2. Check service availability and network connectivity

---

### 3. PR Size Check

**Status**: ❌ failure
**Category**: Permission Error
**Severity**: HIGH
**Started**: 2026-05-28T10:51:18Z
**Completed**: 2026-05-28T10:51:22Z
**Duration**: 4 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26570253641/job/78275255701)

#### Failed Steps

- **Step 2**: Check PR Size

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 2
  - Sample matches:
    - Line 45: `2026-05-28T10:51:20.7215285Z RequestError [HttpError]: API rate limit exceeded for installation. If `
    - Line 78: `2026-05-28T10:51:20.7319834Z ##[error]Unhandled error: HttpError: API rate limit exceeded for instal`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 1
  - Sample matches:
    - Line 1: `${warning}`

#### Suggested Fixes

1. Review the logs above for specific error messages

---

### 4. Auto-Label PR

**Status**: ❌ failure
**Category**: Permission Error
**Severity**: HIGH
**Started**: 2026-05-28T10:51:26Z
**Completed**: 2026-05-28T10:51:37Z
**Duration**: 11 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26570253641/job/78275255758)

#### Failed Steps

- **Step 4**: Intelligent Auto-Labeling

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 2
  - Sample matches:
    - Line 33: `2026-05-28T10:51:36.1294881Z RequestError [HttpError]: API rate limit exceeded for installation. If `
    - Line 54: `2026-05-28T10:51:36.1316586Z ##[error]Unhandled error: HttpError: API rate limit exceeded for instal`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2026-05-28T10:51:36.2746375Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2026-05-28T10:51:36.2746375Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

#### Suggested Fixes

1. Review the logs above for specific error messages

---

### 5. Check for Conflicts

**Status**: ❌ failure
**Category**: Permission Error
**Severity**: HIGH
**Started**: 2026-05-28T10:51:27Z
**Completed**: 2026-05-28T10:51:31Z
**Duration**: 4 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26570253641/job/78275255792)

#### Failed Steps

- **Step 2**: Check Merge Conflicts

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 2
  - Sample matches:
    - Line 45: `2026-05-28T10:51:29.7491657Z RequestError [HttpError]: API rate limit exceeded for installation. If `
    - Line 77: `2026-05-28T10:51:29.7593278Z ##[error]Unhandled error: HttpError: API rate limit exceeded for instal`

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

📊 *Report generated on 2026-05-28T10:54:07.546831*
🤖 *JARVIS CI/CD Auto-PR Manager*
