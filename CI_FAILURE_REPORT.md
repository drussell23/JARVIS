# CI/CD Failure Analysis Report

## Executive Summary

- **Workflow**: PR Automation & Validation
- **Run Number**: #91045
- **Branch**: `fix/ci/pr-automation-validation-run91039-20260523-061313`
- **Commit**: `0a897fc17d54ac6be94a697da183bbcf77d11e95`
- **Status**: ❌ FAILED
- **Timestamp**: 2026-05-23T06:13:55Z
- **Triggered By**: @cubic-dev-ai[bot]
- **Workflow URL**: [View Run](https://github.com/drussell23/JARVIS/actions/runs/26325500139)

## Failure Overview

Total Failed Jobs: **2**

| # | Job Name | Category | Severity | Duration |
|---|----------|----------|----------|----------|
| 1 | PR Size Check | permission_error | high | 6s |
| 2 | Validate PR Title | timeout | high | 5s |

## Detailed Analysis

### 1. PR Size Check

**Status**: ❌ failure
**Category**: Permission Error
**Severity**: HIGH
**Started**: 2026-05-23T06:13:58Z
**Completed**: 2026-05-23T06:14:04Z
**Duration**: 6 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26325500139/job/77502381180)

#### Failed Steps

- **Step 2**: Check PR Size

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 2
  - Sample matches:
    - Line 50: `2026-05-23T06:14:01.8785385Z RequestError [HttpError]: Bad credentials`
    - Line 51: `2026-05-23T06:14:01.8820895Z ##[error]Unhandled error: HttpError: Bad credentials`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 1
  - Sample matches:
    - Line 6: `${warning}`

#### Suggested Fixes

1. Review the logs above for specific error messages

---

### 2. Validate PR Title

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2026-05-23T06:13:58Z
**Completed**: 2026-05-23T06:14:03Z
**Duration**: 5 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26325500139/job/77502381198)

#### Failed Steps

- **Step 2**: Validate Conventional Commits

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 2
  - Sample matches:
    - Line 30: `2026-05-23T06:14:01.0553778Z   subjectPatternError: The PR title must start with a capital letter.`
    - Line 42: `2026-05-23T06:14:01.6982942Z ##[error]No release type found in pull request title "🚨 Fix CI/CD: PR A`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 1
  - Sample matches:
    - Line 58: `2026-05-23T06:14:01.7521995Z ##[warning]Node.js 20 actions are deprecated. The following actions are`

- Pattern: `timeout|timed out`
  - Occurrences: 1
  - Sample matches:
    - Line 34: `- fix: Resolve database connection timeout`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations
2. Check service availability and network connectivity

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

📊 *Report generated on 2026-05-23T06:15:31.373387*
🤖 *JARVIS CI/CD Auto-PR Manager*
