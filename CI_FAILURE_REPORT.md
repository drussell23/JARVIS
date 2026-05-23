# CI/CD Failure Analysis Report

## Executive Summary

- **Workflow**: PR Automation & Validation
- **Run Number**: #91140
- **Branch**: `fix/ci/pr-automation-validation-run91138-20260523-181706`
- **Commit**: `955e2cde4b794d2af684a97fd9fe4d713081d7f8`
- **Status**: ❌ FAILED
- **Timestamp**: 2026-05-23T18:17:36Z
- **Triggered By**: @cubic-dev-ai[bot]
- **Workflow URL**: [View Run](https://github.com/drussell23/JARVIS/actions/runs/26340056491)

## Failure Overview

Total Failed Jobs: **2**

| # | Job Name | Category | Severity | Duration |
|---|----------|----------|----------|----------|
| 1 | Validate PR Title | timeout | high | 3s |
| 2 | Auto-Label PR | permission_error | high | 13s |

## Detailed Analysis

### 1. Validate PR Title

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2026-05-23T18:17:39Z
**Completed**: 2026-05-23T18:17:42Z
**Duration**: 3 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26340056491/job/77540366092)

#### Failed Steps

- **Step 2**: Validate Conventional Commits

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 2
  - Sample matches:
    - Line 30: `2026-05-23T18:17:40.5802756Z   subjectPatternError: The PR title must start with a capital letter.`
    - Line 42: `2026-05-23T18:17:41.0535611Z ##[error]No release type found in pull request title "🚨 Fix CI/CD: PR A`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 1
  - Sample matches:
    - Line 58: `2026-05-23T18:17:41.0959331Z ##[warning]Node.js 20 actions are deprecated. The following actions are`

- Pattern: `timeout|timed out`
  - Occurrences: 1
  - Sample matches:
    - Line 34: `- fix: Resolve database connection timeout`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations
2. Check service availability and network connectivity

---

### 2. Auto-Label PR

**Status**: ❌ failure
**Category**: Permission Error
**Severity**: HIGH
**Started**: 2026-05-23T18:17:39Z
**Completed**: 2026-05-23T18:17:52Z
**Duration**: 13 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26340056491/job/77540366099)

#### Failed Steps

- **Step 3**: Label Based on Files Changed

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 2
  - Sample matches:
    - Line 86: `2026-05-23T18:17:50.0374826Z ##[error]HttpError: Bad credentials - https://docs.github.com/rest`
    - Line 87: `2026-05-23T18:17:50.0385452Z ##[error]Bad credentials - https://docs.github.com/rest`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2026-05-23T18:17:50.1816123Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 2
  - Sample matches:
    - Line 17: `2026-05-23T18:17:42.6372407Z hint: to use in all of your new repositories, which will suppress this `
    - Line 97: `2026-05-23T18:17:50.1816123Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

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

📊 *Report generated on 2026-05-23T18:20:22.012656*
🤖 *JARVIS CI/CD Auto-PR Manager*
