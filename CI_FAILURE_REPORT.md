# CI/CD Failure Analysis Report

## Executive Summary

- **Workflow**: PR Automation & Validation
- **Run Number**: #101916
- **Branch**: `fix/ci/pr-automation-validation-run101913-20260528-130645`
- **Commit**: `8b4c9897202c9445701fd297c429136c88912ffa`
- **Status**: ❌ FAILED
- **Timestamp**: 2026-05-28T13:07:18Z
- **Triggered By**: @cubic-dev-ai[bot]
- **Workflow URL**: [View Run](https://github.com/drussell23/JARVIS/actions/runs/26576578823)

## Failure Overview

Total Failed Jobs: **3**

| # | Job Name | Category | Severity | Duration |
|---|----------|----------|----------|----------|
| 1 | Validate PR Title | timeout | high | 7s |
| 2 | PR Size Check | permission_error | high | 6s |
| 3 | Auto-Label PR | permission_error | high | 12s |

## Detailed Analysis

### 1. Validate PR Title

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2026-05-28T13:07:22Z
**Completed**: 2026-05-28T13:07:29Z
**Duration**: 7 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26576578823/job/78297428292)

#### Failed Steps

- **Step 2**: Validate Conventional Commits

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 2
  - Sample matches:
    - Line 30: `2026-05-28T13:07:25.5403513Z   subjectPatternError: The PR title must start with a capital letter.`
    - Line 42: `2026-05-28T13:07:26.2784439Z ##[error]No release type found in pull request title "🚨 Fix CI/CD: PR A`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 1
  - Sample matches:
    - Line 58: `2026-05-28T13:07:26.3375516Z ##[warning]Node.js 20 actions are deprecated. The following actions are`

- Pattern: `timeout|timed out`
  - Occurrences: 1
  - Sample matches:
    - Line 34: `- fix: Resolve database connection timeout`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations
2. Check service availability and network connectivity

---

### 2. PR Size Check

**Status**: ❌ failure
**Category**: Permission Error
**Severity**: HIGH
**Started**: 2026-05-28T13:07:22Z
**Completed**: 2026-05-28T13:07:28Z
**Duration**: 6 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26576578823/job/78297428446)

#### Failed Steps

- **Step 2**: Check PR Size

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 2
  - Sample matches:
    - Line 60: `2026-05-28T13:07:26.1110748Z RequestError [HttpError]`
    - Line 61: `2026-05-28T13:07:26.1152296Z ##[error]Unhandled error: HttpError`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 2
  - Sample matches:
    - Line 1: `warning = '💡 **This PR is large.** Ensure it focuses on a single feature or fix.';`
    - Line 16: `${warning}`

#### Suggested Fixes

1. Review the logs above for specific error messages

---

### 3. Auto-Label PR

**Status**: ❌ failure
**Category**: Permission Error
**Severity**: HIGH
**Started**: 2026-05-28T13:07:22Z
**Completed**: 2026-05-28T13:07:34Z
**Duration**: 12 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26576578823/job/78297428501)

#### Failed Steps

- **Step 3**: Label Based on Files Changed

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 1
  - Sample matches:
    - Line 87: `2026-05-28T13:07:32.3321456Z ##[error]HttpError`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2026-05-28T13:07:32.4744341Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 2
  - Sample matches:
    - Line 19: `2026-05-28T13:07:25.0454867Z hint: to use in all of your new repositories, which will suppress this `
    - Line 97: `2026-05-28T13:07:32.4744341Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

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

📊 *Report generated on 2026-05-28T13:09:35.124030*
🤖 *JARVIS CI/CD Auto-PR Manager*
