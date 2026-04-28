# CI/CD Failure Analysis Report

## Executive Summary

- **Workflow**: Complete Unlock Test Suite (Master)
- **Run Number**: #646
- **Branch**: `main`
- **Commit**: `74df0c2ffbb7b4bcd1aa716ed25c6dc4774a344c`
- **Status**: ❌ FAILED
- **Timestamp**: 2026-04-28T06:23:33Z
- **Triggered By**: @drussell23
- **Workflow URL**: [View Run](https://github.com/drussell23/JARVIS/actions/runs/25037396782)

## Failure Overview

Total Failed Jobs: **4**

| # | Job Name | Category | Severity | Duration |
|---|----------|----------|----------|----------|
| 1 | Run Biometric Voice E2E / Integration Biometric Tests - macOS | dependency_error | high | 36s |
| 2 | Run Unlock Integration E2E / Integration Tests - macOS | dependency_error | high | 35s |
| 3 | Generate Combined Test Summary | test_failure | high | 5s |
| 4 | Notify Test Status | test_failure | high | 4s |

## Detailed Analysis

### 1. Run Biometric Voice E2E / Integration Biometric Tests - macOS

**Status**: ❌ failure
**Category**: Dependency Error
**Severity**: HIGH
**Started**: 2026-04-28T06:23:55Z
**Completed**: 2026-04-28T06:24:31Z
**Duration**: 36 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/25037396782/job/73332187820)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 6
  - Sample matches:
    - Line 38: `2026-04-28T06:24:27.2962100Z   Getting requirements to build wheel: finished with status 'error'`
    - Line 39: `2026-04-28T06:24:27.2993140Z   error: subprocess-exited-with-error`
    - Line 60: `2026-04-28T06:24:27.3014540Z       ModuleNotFoundError: No module named 'pkg_resources'`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 2
  - Sample matches:
    - Line 64: `2026-04-28T06:24:27.3015660Z ERROR: Failed to build 'openai-whisper' when getting requirements to bu`
    - Line 96: `2026-04-28T06:24:27.8426840Z ##[warning]The process '/opt/homebrew/bin/git' failed with exit code 12`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 4
  - Sample matches:
    - Line 71: `2026-04-28T06:24:27.3999630Z   if-no-files-found: warn`
    - Line 85: `2026-04-28T06:24:27.5860520Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 96: `2026-04-28T06:24:27.8426840Z ##[warning]The process '/opt/homebrew/bin/git' failed with exit code 12`

- Pattern: `AssertionError|Exception`
  - Occurrences: 2
  - Sample matches:
    - Line 4: `2026-04-28T06:24:20.6594380Z Installing collected packages: typing-extensions, tomli, pygments, prop`
    - Line 6: `2026-04-28T06:24:25.2564990Z Successfully installed aiohappyeyeballs-2.6.1 aiohttp-3.13.5 aiosignal-`

- Pattern: `timeout|timed out`
  - Occurrences: 2
  - Sample matches:
    - Line 4: `2026-04-28T06:24:20.6594380Z Installing collected packages: typing-extensions, tomli, pygments, prop`
    - Line 6: `2026-04-28T06:24:25.2564990Z Successfully installed aiohappyeyeballs-2.6.1 aiohttp-3.13.5 aiosignal-`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 2. Run Unlock Integration E2E / Integration Tests - macOS

**Status**: ❌ failure
**Category**: Dependency Error
**Severity**: HIGH
**Started**: 2026-04-28T06:23:56Z
**Completed**: 2026-04-28T06:24:31Z
**Duration**: 35 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/25037396782/job/73332190459)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 6
  - Sample matches:
    - Line 38: `2026-04-28T06:24:27.6227390Z   Getting requirements to build wheel: finished with status 'error'`
    - Line 39: `2026-04-28T06:24:27.6253680Z   error: subprocess-exited-with-error`
    - Line 60: `2026-04-28T06:24:27.6273670Z       ModuleNotFoundError: No module named 'pkg_resources'`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 2
  - Sample matches:
    - Line 64: `2026-04-28T06:24:27.6274850Z ERROR: Failed to build 'openai-whisper' when getting requirements to bu`
    - Line 96: `2026-04-28T06:24:28.0644610Z ##[warning]The process '/opt/homebrew/bin/git' failed with exit code 12`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 4
  - Sample matches:
    - Line 71: `2026-04-28T06:24:27.7066140Z   if-no-files-found: warn`
    - Line 85: `2026-04-28T06:24:27.8694240Z ##[warning]No files were found with the provided path: test-results/unl`
    - Line 96: `2026-04-28T06:24:28.0644610Z ##[warning]The process '/opt/homebrew/bin/git' failed with exit code 12`

- Pattern: `AssertionError|Exception`
  - Occurrences: 2
  - Sample matches:
    - Line 4: `2026-04-28T06:24:24.4751270Z Installing collected packages: typing-extensions, tomli, pygments, prop`
    - Line 6: `2026-04-28T06:24:25.2463210Z Successfully installed aiohappyeyeballs-2.6.1 aiohttp-3.13.5 aiosignal-`

- Pattern: `timeout|timed out`
  - Occurrences: 2
  - Sample matches:
    - Line 4: `2026-04-28T06:24:24.4751270Z Installing collected packages: typing-extensions, tomli, pygments, prop`
    - Line 6: `2026-04-28T06:24:25.2463210Z Successfully installed aiohappyeyeballs-2.6.1 aiohttp-3.13.5 aiosignal-`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 3. Generate Combined Test Summary

**Status**: ❌ failure
**Category**: Test Failure
**Severity**: HIGH
**Started**: 2026-04-28T06:24:35Z
**Completed**: 2026-04-28T06:24:40Z
**Duration**: 5 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/25037396782/job/73332262966)

#### Failed Steps

- **Step 2**: Generate Combined Summary

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 1
  - Sample matches:
    - Line 75: `2026-04-28T06:24:37.7214037Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 11
  - Sample matches:
    - Line 39: `2026-04-28T06:24:37.7021095Z [36;1mif [ "failure" = "success" ]; then[0m`
    - Line 42: `2026-04-28T06:24:37.7025226Z [36;1m  echo "- ❌ **Unlock Integration E2E:** failure" >> $GITHUB_STEP`
    - Line 46: `2026-04-28T06:24:37.7029256Z [36;1mif [ "failure" = "success" ]; then[0m`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 1
  - Sample matches:
    - Line 98: `2026-04-28T06:24:38.4935317Z ##[warning]Node.js 20 actions are deprecated. The following actions are`

#### Suggested Fixes

1. Review test cases and ensure code changes haven't broken existing functionality

---

### 4. Notify Test Status

**Status**: ❌ failure
**Category**: Test Failure
**Severity**: HIGH
**Started**: 2026-04-28T06:24:44Z
**Completed**: 2026-04-28T06:24:48Z
**Duration**: 4 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/25037396782/job/73332281315)

#### Failed Steps

- **Step 3**: Failure Notification

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 1
  - Sample matches:
    - Line 5: `2026-04-28T06:24:45.6992202Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 3
  - Sample matches:
    - Line -3: `2026-04-28T06:24:45.6360703Z ##[group]Run echo "❌ Unlock tests failed - 'unlock my screen' may be br`
    - Line -2: `2026-04-28T06:24:45.6361793Z [36;1mecho "❌ Unlock tests failed - 'unlock my screen' may be broken!"`
    - Line 3: `2026-04-28T06:24:45.6972737Z ❌ Unlock tests failed - 'unlock my screen' may be broken!`

#### Suggested Fixes

1. Review test cases and ensure code changes haven't broken existing functionality

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

📊 *Report generated on 2026-04-28T06:26:21.488305*
🤖 *JARVIS CI/CD Auto-PR Manager*
