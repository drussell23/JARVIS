# CI/CD Failure Analysis Report

## Executive Summary

- **Workflow**: PR Automation & Validation
- **Run Number**: #91031
- **Branch**: `fix/ci/pr-automation-validation-run91024-20260523-060852`
- **Commit**: `29376bffe0b70b4d1a5cfce5ecf14977d7e10f5c`
- **Status**: ❌ FAILED
- **Timestamp**: 2026-05-23T06:09:20Z
- **Triggered By**: @cubic-dev-ai[bot]
- **Workflow URL**: [View Run](https://github.com/drussell23/JARVIS/actions/runs/26325412182)

## Failure Overview

Total Failed Jobs: **2**

| # | Job Name | Category | Severity | Duration |
|---|----------|----------|----------|----------|
| 1 | PR Size Check | permission_error | high | 4s |
| 2 | Validate PR Title | timeout | high | 4s |

## Detailed Analysis

### 1. PR Size Check

**Status**: ❌ failure
**Category**: Permission Error
**Severity**: HIGH
**Started**: 2026-05-23T06:09:23Z
**Completed**: 2026-05-23T06:09:27Z
**Duration**: 4 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26325412182/job/77502129808)

#### Failed Steps

- **Step 2**: Check PR Size

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 2
  - Sample matches:
    - Line 50: `2026-05-23T06:09:25.9200317Z RequestError [HttpError]: Bad credentials`
    - Line 61: `2026-05-23T06:09:25.9264319Z ##[error]Unhandled error: HttpError: Bad credentials`

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
**Started**: 2026-05-23T06:09:23Z
**Completed**: 2026-05-23T06:09:27Z
**Duration**: 4 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26325412182/job/77502129813)

#### Failed Steps

- **Step 2**: Validate Conventional Commits

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 2
  - Sample matches:
    - Line 30: `2026-05-23T06:09:25.5296929Z   subjectPatternError: The PR title must start with a capital letter.`
    - Line 42: `2026-05-23T06:09:26.1532501Z ##[error]No release type found in pull request title "🚨 Fix CI/CD: PR A`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 1
  - Sample matches:
    - Line 58: `2026-05-23T06:09:26.2089560Z ##[warning]Node.js 20 actions are deprecated. The following actions are`

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

📊 *Report generated on 2026-05-23T06:10:50.435029*
🤖 *JARVIS CI/CD Auto-PR Manager*
