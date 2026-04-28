# CI/CD Failure Analysis Report

## Executive Summary

- **Workflow**: Unlock Integration E2E Testing
- **Run Number**: #392
- **Branch**: `main`
- **Commit**: `74df0c2ffbb7b4bcd1aa716ed25c6dc4774a344c`
- **Status**: ❌ FAILED
- **Timestamp**: 2026-04-28T06:32:02Z
- **Triggered By**: @drussell23
- **Workflow URL**: [View Run](https://github.com/drussell23/JARVIS/actions/runs/25037684343)

## Failure Overview

Total Failed Jobs: **1**

| # | Job Name | Category | Severity | Duration |
|---|----------|----------|----------|----------|
| 1 | Integration Tests - macOS | dependency_error | high | 44s |

## Detailed Analysis

### 1. Integration Tests - macOS

**Status**: ❌ failure
**Category**: Dependency Error
**Severity**: HIGH
**Started**: 2026-04-28T06:32:16Z
**Completed**: 2026-04-28T06:33:00Z
**Duration**: 44 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/25037684343/job/73333098803)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 6
  - Sample matches:
    - Line 38: `2026-04-28T06:32:57.7315220Z   Getting requirements to build wheel: finished with status 'error'`
    - Line 39: `2026-04-28T06:32:57.7343440Z   error: subprocess-exited-with-error`
    - Line 60: `2026-04-28T06:32:57.7360860Z       ModuleNotFoundError: No module named 'pkg_resources'`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 2
  - Sample matches:
    - Line 64: `2026-04-28T06:32:57.7361930Z ERROR: Failed to build 'openai-whisper' when getting requirements to bu`
    - Line 96: `2026-04-28T06:32:58.2177030Z ##[warning]The process '/opt/homebrew/bin/git' failed with exit code 12`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 4
  - Sample matches:
    - Line 71: `2026-04-28T06:32:57.8266190Z   if-no-files-found: warn`
    - Line 85: `2026-04-28T06:32:57.9950940Z ##[warning]No files were found with the provided path: test-results/unl`
    - Line 96: `2026-04-28T06:32:58.2177030Z ##[warning]The process '/opt/homebrew/bin/git' failed with exit code 12`

- Pattern: `AssertionError|Exception`
  - Occurrences: 2
  - Sample matches:
    - Line 4: `2026-04-28T06:32:54.3019580Z Installing collected packages: typing-extensions, tomli, pygments, prop`
    - Line 6: `2026-04-28T06:32:55.2383840Z Successfully installed aiohappyeyeballs-2.6.1 aiohttp-3.13.5 aiosignal-`

- Pattern: `timeout|timed out`
  - Occurrences: 2
  - Sample matches:
    - Line 4: `2026-04-28T06:32:54.3019580Z Installing collected packages: typing-extensions, tomli, pygments, prop`
    - Line 6: `2026-04-28T06:32:55.2383840Z Successfully installed aiohappyeyeballs-2.6.1 aiohttp-3.13.5 aiosignal-`

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

📊 *Report generated on 2026-04-28T06:34:37.777915*
🤖 *JARVIS CI/CD Auto-PR Manager*
