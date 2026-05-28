# CI/CD Failure Analysis Report

## Executive Summary

- **Workflow**: PR Automation & Validation
- **Run Number**: #101004
- **Branch**: `fix/ci/pr-automation-validation-run100981-20260528-095245`
- **Commit**: `e47614fdaa528ba6e140ed17c5d0ab93edc0f477`
- **Status**: ❌ FAILED
- **Timestamp**: 2026-05-28T09:53:17Z
- **Triggered By**: @cubic-dev-ai[bot]
- **Workflow URL**: [View Run](https://github.com/drussell23/JARVIS/actions/runs/26567666958)

## Failure Overview

Total Failed Jobs: **3**

| # | Job Name | Category | Severity | Duration |
|---|----------|----------|----------|----------|
| 1 | Validate PR Title | timeout | high | 4s |
| 2 | PR Size Check | permission_error | high | 2s |
| 3 | Auto-Label PR | permission_error | high | 13s |

## Detailed Analysis

### 1. Validate PR Title

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2026-05-28T09:53:30Z
**Completed**: 2026-05-28T09:53:34Z
**Duration**: 4 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26567666958/job/78266392434)

#### Failed Steps

- **Step 2**: Validate Conventional Commits

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 2
  - Sample matches:
    - Line 16: `2026-05-28T09:53:32.7263424Z   subjectPatternError: The PR title must start with a capital letter.`
    - Line 28: `2026-05-28T09:53:32.9909568Z ##[error]API rate limit exceeded for installation. If you reach out to `

- Pattern: `WARN|Warning|warning`
  - Occurrences: 1
  - Sample matches:
    - Line 30: `2026-05-28T09:53:33.0339831Z ##[warning]Node.js 20 actions are deprecated. The following actions are`

- Pattern: `timeout|timed out`
  - Occurrences: 1
  - Sample matches:
    - Line 20: `- fix: Resolve database connection timeout`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations
2. Check service availability and network connectivity

---

### 2. PR Size Check

**Status**: ❌ failure
**Category**: Permission Error
**Severity**: HIGH
**Started**: 2026-05-28T09:53:39Z
**Completed**: 2026-05-28T09:53:41Z
**Duration**: 2 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26567666958/job/78266392460)

#### Failed Steps

- **Step 2**: Check PR Size

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 2
  - Sample matches:
    - Line 45: `2026-05-28T09:53:40.8750544Z RequestError [HttpError]: API rate limit exceeded for installation. If `
    - Line 77: `2026-05-28T09:53:40.8822359Z ##[error]Unhandled error: HttpError: API rate limit exceeded for instal`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 1
  - Sample matches:
    - Line 1: `${warning}`

#### Suggested Fixes

1. Review the logs above for specific error messages

---

### 3. Auto-Label PR

**Status**: ❌ failure
**Category**: Permission Error
**Severity**: HIGH
**Started**: 2026-05-28T09:53:39Z
**Completed**: 2026-05-28T09:53:52Z
**Duration**: 13 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26567666958/job/78266392462)

#### Failed Steps

- **Step 4**: Intelligent Auto-Labeling

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 2
  - Sample matches:
    - Line 33: `2026-05-28T09:53:49.8071411Z RequestError [HttpError]: API rate limit exceeded for installation. If `
    - Line 87: `2026-05-28T09:53:49.8087882Z ##[error]Unhandled error: HttpError: API rate limit exceeded for instal`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2026-05-28T09:53:49.8968729Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2026-05-28T09:53:49.8968729Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

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

📊 *Report generated on 2026-05-28T09:58:03.954974*
🤖 *JARVIS CI/CD Auto-PR Manager*
