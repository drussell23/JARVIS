# CI/CD Failure Analysis Report

## Executive Summary

- **Workflow**: Unlock Integration E2E Testing
- **Run Number**: #70
- **Branch**: `main`
- **Commit**: `091f42eb5cd43ad29b8c5bcb4fc32aea8c31d34b`
- **Status**: ❌ FAILED
- **Timestamp**: 2025-11-09T04:25:46Z
- **Triggered By**: @drussell23
- **Workflow URL**: [View Run](https://github.com/drussell23/JARVIS/actions/runs/19203325139)

## Failure Overview

Total Failed Jobs: **1**

| # | Job Name | Category | Severity | Duration |
|---|----------|----------|----------|----------|
| 1 | Integration Tests - macOS | timeout | high | 49s |

## Detailed Analysis

### 1. Integration Tests - macOS

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2025-11-09T04:26:02Z
**Completed**: 2025-11-09T04:26:51Z
**Duration**: 49 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19203325139/job/54894944337)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 52: `2025-11-09T04:26:46.7952670Z ERROR: Cannot install psutil==5.9.6 and psutil==5.9.8 because these pac`
    - Line 65: `2025-11-09T04:26:46.7960780Z ERROR: ResolutionImpossible: for help visit https://pip.pypa.io/en/late`
    - Line 66: `2025-11-09T04:26:47.2797040Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-11-09T04:26:47.7749490Z ##[warning]The process '/opt/homebrew/bin/git' failed with exit code 12`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 3
  - Sample matches:
    - Line 72: `2025-11-09T04:26:47.2869360Z   if-no-files-found: warn`
    - Line 86: `2025-11-09T04:26:47.4760360Z ##[warning]No files were found with the provided path: test-results/unl`
    - Line 97: `2025-11-09T04:26:47.7749490Z ##[warning]The process '/opt/homebrew/bin/git' failed with exit code 12`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

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

📊 *Report generated on 2025-11-09T04:27:27.023859*
🤖 *JARVIS CI/CD Auto-PR Manager*
