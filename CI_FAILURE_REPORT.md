# CI/CD Failure Analysis Report

## Executive Summary

- **Workflow**: PR Automation & Validation
- **Run Number**: #91113
- **Branch**: `fix/ci/pr-automation-validation-run91110-20260523-171522`
- **Commit**: `4c70d03fe7434070e246b314d258255446b7e59c`
- **Status**: ❌ FAILED
- **Timestamp**: 2026-05-23T17:15:52Z
- **Triggered By**: @cubic-dev-ai[bot]
- **Workflow URL**: [View Run](https://github.com/drussell23/JARVIS/actions/runs/26338784932)

## Failure Overview

Total Failed Jobs: **1**

| # | Job Name | Category | Severity | Duration |
|---|----------|----------|----------|----------|
| 1 | Validate PR Title | unknown | medium | 5s |

## Detailed Analysis

### 1. Validate PR Title

**Status**: ❌ failure
**Category**: Unknown
**Severity**: MEDIUM
**Started**: 2026-05-23T17:15:55Z
**Completed**: 2026-05-23T17:16:00Z
**Duration**: 5 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26338784932/job/77536991202)

#### Failed Steps

- **Step 2**: Validate Conventional Commits

#### Error Analysis

*No specific error patterns detected*

#### Suggested Fixes

1. Check the workflow logs for more details

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

📊 *Report generated on 2026-05-23T17:18:01.338165*
🤖 *JARVIS CI/CD Auto-PR Manager*
