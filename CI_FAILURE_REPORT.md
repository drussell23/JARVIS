# CI/CD Failure Analysis Report

## Executive Summary

- **Workflow**: PR Automation & Validation
- **Run Number**: #101376
- **Branch**: `fix/ci/pr-automation-validation-run101344-20260528-105107`
- **Commit**: `d984ba810a1fd7b17f8aa185d95499e2af0bd559`
- **Status**: ❌ FAILED
- **Timestamp**: 2026-05-28T10:51:46Z
- **Triggered By**: @cubic-dev-ai[bot]
- **Workflow URL**: [View Run](https://github.com/drussell23/JARVIS/actions/runs/26570282465)

## Failure Overview

Total Failed Jobs: **5**

| # | Job Name | Category | Severity | Duration |
|---|----------|----------|----------|----------|
| 1 | Auto-Label PR | permission_error | high | 13s |
| 2 | PR Size Check | permission_error | high | 5s |
| 3 | Validate PR Title | timeout | high | 4s |
| 4 | Check PR Description | permission_error | high | 5s |
| 5 | Check for Conflicts | permission_error | high | 3s |

## Detailed Analysis

### 1. Auto-Label PR

**Status**: ❌ failure
**Category**: Permission Error
**Severity**: HIGH
**Started**: 2026-05-28T10:52:05Z
**Completed**: 2026-05-28T10:52:18Z
**Duration**: 13 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26570282465/job/78275349695)

#### Failed Steps

- **Step 4**: Intelligent Auto-Labeling

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 2
  - Sample matches:
    - Line 33: `2026-05-28T10:52:16.2677729Z RequestError [HttpError]: API rate limit exceeded for installation. If `
    - Line 87: `2026-05-28T10:52:16.2731543Z ##[error]Unhandled error: HttpError: API rate limit exceeded for instal`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2026-05-28T10:52:16.4117377Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2026-05-28T10:52:16.4117377Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

#### Suggested Fixes

1. Review the logs above for specific error messages

---

### 2. PR Size Check

**Status**: ❌ failure
**Category**: Permission Error
**Severity**: HIGH
**Started**: 2026-05-28T10:52:07Z
**Completed**: 2026-05-28T10:52:12Z
**Duration**: 5 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26570282465/job/78275349699)

#### Failed Steps

- **Step 2**: Check PR Size

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 2
  - Sample matches:
    - Line 45: `2026-05-28T10:52:09.8208795Z RequestError [HttpError]: API rate limit exceeded for installation. If `
    - Line 46: `2026-05-28T10:52:09.8262539Z ##[error]Unhandled error: HttpError: API rate limit exceeded for instal`

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
**Started**: 2026-05-28T10:52:06Z
**Completed**: 2026-05-28T10:52:10Z
**Duration**: 4 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26570282465/job/78275349711)

#### Failed Steps

- **Step 2**: Validate Conventional Commits

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 2
  - Sample matches:
    - Line 16: `2026-05-28T10:52:08.4361991Z   subjectPatternError: The PR title must start with a capital letter.`
    - Line 28: `2026-05-28T10:52:08.6364847Z ##[error]API rate limit exceeded for installation. If you reach out to `

- Pattern: `WARN|Warning|warning`
  - Occurrences: 1
  - Sample matches:
    - Line 30: `2026-05-28T10:52:08.6806310Z ##[warning]Node.js 20 actions are deprecated. The following actions are`

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
**Started**: 2026-05-28T10:52:06Z
**Completed**: 2026-05-28T10:52:11Z
**Duration**: 5 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26570282465/job/78275349754)

#### Failed Steps

- **Step 2**: Verify PR Description

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 2
  - Sample matches:
    - Line 43: `2026-05-28T10:52:09.4161657Z RequestError [HttpError]: API rate limit exceeded for installation. If `
    - Line 45: `2026-05-28T10:52:09.4225949Z ##[error]Unhandled error: HttpError: API rate limit exceeded for instal`

#### Suggested Fixes

1. Review the logs above for specific error messages

---

### 5. Check for Conflicts

**Status**: ❌ failure
**Category**: Permission Error
**Severity**: HIGH
**Started**: 2026-05-28T10:52:09Z
**Completed**: 2026-05-28T10:52:12Z
**Duration**: 3 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26570282465/job/78275349788)

#### Failed Steps

- **Step 2**: Check Merge Conflicts

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 2
  - Sample matches:
    - Line 45: `2026-05-28T10:52:10.9484352Z RequestError [HttpError]: API rate limit exceeded for installation. If `
    - Line 46: `2026-05-28T10:52:10.9526760Z ##[error]Unhandled error: HttpError: API rate limit exceeded for instal`

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

📊 *Report generated on 2026-05-28T10:54:06.852196*
🤖 *JARVIS CI/CD Auto-PR Manager*
