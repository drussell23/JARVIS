# CI/CD Failure Analysis Report

## Executive Summary

- **Workflow**: Complete Unlock Test Suite (Master)
- **Run Number**: #237
- **Branch**: `cursor/investigate-voice-authentication-system-integration-gemini-3-pro-preview-e56d`
- **Commit**: `e1086ec42a310b89ceabbeec7014a49fe2d54b6d`
- **Status**: ❌ FAILED
- **Timestamp**: 2025-12-06T06:10:30Z
- **Triggered By**: @cursor[bot]
- **Workflow URL**: [View Run](https://github.com/drussell23/JARVIS/actions/runs/19984432958)

## Failure Overview

Total Failed Jobs: **22**

| # | Job Name | Category | Severity | Duration |
|---|----------|----------|----------|----------|
| 1 | Run Unlock Integration E2E / Mock Tests - security-checks | test_failure | high | 51s |
| 2 | Run Biometric Voice E2E / Mock Biometric Tests - wake-word-detection | timeout | high | 42s |
| 3 | Run Biometric Voice E2E / Mock Biometric Tests - profile-quality-assessment | timeout | high | 44s |
| 4 | Run Biometric Voice E2E / Mock Biometric Tests - stt-transcription | timeout | high | 56s |
| 5 | Run Biometric Voice E2E / Mock Biometric Tests - anti-spoofing | timeout | high | 46s |
| 6 | Run Biometric Voice E2E / Mock Biometric Tests - embedding-validation | timeout | high | 40s |
| 7 | Run Biometric Voice E2E / Mock Biometric Tests - edge-case-noise | timeout | high | 46s |
| 8 | Run Biometric Voice E2E / Mock Biometric Tests - edge-case-voice-drift | timeout | high | 52s |
| 9 | Run Biometric Voice E2E / Mock Biometric Tests - adaptive-thresholds | timeout | high | 43s |
| 10 | Run Biometric Voice E2E / Mock Biometric Tests - edge-case-database-failure | test_failure | high | 52s |
| 11 | Run Biometric Voice E2E / Mock Biometric Tests - dimension-adaptation | timeout | high | 59s |
| 12 | Run Biometric Voice E2E / Mock Biometric Tests - edge-case-cold-start | timeout | high | 60s |
| 13 | Run Biometric Voice E2E / Mock Biometric Tests - performance-baseline | timeout | high | 39s |
| 14 | Run Biometric Voice E2E / Mock Biometric Tests - end-to-end-flow | timeout | high | 47s |
| 15 | Run Biometric Voice E2E / Mock Biometric Tests - voice-verification | timeout | high | 48s |
| 16 | Run Biometric Voice E2E / Mock Biometric Tests - voice-synthesis-detection | timeout | high | 51s |
| 17 | Run Biometric Voice E2E / Mock Biometric Tests - replay-attack-detection | timeout | high | 47s |
| 18 | Run Biometric Voice E2E / Mock Biometric Tests - security-validation | timeout | high | 46s |
| 19 | Run Unlock Integration E2E / Generate Test Summary | test_failure | high | 4s |
| 20 | Run Biometric Voice E2E / Generate Biometric Test Summary | test_failure | high | 6s |
| 21 | Generate Combined Test Summary | test_failure | high | 5s |
| 22 | Notify Test Status | test_failure | high | 2s |

## Detailed Analysis

### 1. Run Unlock Integration E2E / Mock Tests - security-checks

**Status**: ❌ failure
**Category**: Test Failure
**Severity**: HIGH
**Started**: 2025-12-06T06:13:48Z
**Completed**: 2025-12-06T06:14:39Z
**Duration**: 51 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984432958/job/57316299083)

#### Failed Steps

- **Step 6**: Run Mock Tests

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 2
  - Sample matches:
    - Line 39: `2025-12-06T06:14:36.0772756Z 2025-12-06 06:14:36,077 - __main__ - ERROR - ❌ 1 test(s) failed`
    - Line 56: `2025-12-06T06:14:36.0894724Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 3
  - Sample matches:
    - Line 39: `2025-12-06T06:14:36.0772756Z 2025-12-06 06:14:36,077 - __main__ - ERROR - ❌ 1 test(s) failed`
    - Line 48: `2025-12-06T06:14:36.0781461Z ❌ Failed: 1`
    - Line 97: `2025-12-06T06:14:37.3074498Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 2
  - Sample matches:
    - Line 62: `2025-12-06T06:14:36.0965642Z   if-no-files-found: warn`
    - Line 97: `2025-12-06T06:14:37.3074498Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations
2. Review test cases and ensure code changes haven't broken existing functionality

---

### 2. Run Biometric Voice E2E / Mock Biometric Tests - wake-word-detection

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2025-12-06T06:15:06Z
**Completed**: 2025-12-06T06:15:48Z
**Duration**: 42 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984432958/job/57316363689)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 64: `2025-12-06T06:15:46.6532022Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 65: `2025-12-06T06:15:46.6542429Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 66: `2025-12-06T06:15:46.6908095Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:15:47.0304372Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 3
  - Sample matches:
    - Line 72: `2025-12-06T06:15:46.7009362Z   if-no-files-found: warn`
    - Line 87: `2025-12-06T06:15:46.8999262Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 97: `2025-12-06T06:15:47.0304372Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `AssertionError|Exception`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2025-12-06T06:15:38.7892638Z   Using cached exceptiongroup-1.3.1-py3-none-any.whl.metadata (6.7 kB)`
    - Line 52: `2025-12-06T06:15:39.9332435Z Using cached exceptiongroup-1.3.1-py3-none-any.whl (16 kB)`
    - Line 61: `2025-12-06T06:15:40.0921822Z Installing collected packages: typing-extensions, tomli, pygments, prop`

- Pattern: `timeout|timed out`
  - Occurrences: 6
  - Sample matches:
    - Line 19: `2025-12-06T06:15:38.9720000Z Collecting async-timeout<6.0,>=4.0 (from aiohttp)`
    - Line 20: `2025-12-06T06:15:38.9749888Z   Downloading async_timeout-5.0.1-py3-none-any.whl.metadata (5.1 kB)`
    - Line 37: `2025-12-06T06:15:39.7431939Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 3. Run Biometric Voice E2E / Mock Biometric Tests - profile-quality-assessment

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2025-12-06T06:15:25Z
**Completed**: 2025-12-06T06:16:09Z
**Duration**: 44 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984432958/job/57316363691)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 64: `2025-12-06T06:16:07.4661440Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 65: `2025-12-06T06:16:07.4670670Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 66: `2025-12-06T06:16:07.5053947Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:16:07.8792098Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 3
  - Sample matches:
    - Line 72: `2025-12-06T06:16:07.5143882Z   if-no-files-found: warn`
    - Line 87: `2025-12-06T06:16:07.7298376Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 97: `2025-12-06T06:16:07.8792098Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `AssertionError|Exception`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2025-12-06T06:15:58.3084387Z   Using cached exceptiongroup-1.3.1-py3-none-any.whl.metadata (6.7 kB)`
    - Line 52: `2025-12-06T06:15:59.9798803Z Using cached exceptiongroup-1.3.1-py3-none-any.whl (16 kB)`
    - Line 61: `2025-12-06T06:16:00.1382217Z Installing collected packages: typing-extensions, tomli, pygments, prop`

- Pattern: `timeout|timed out`
  - Occurrences: 6
  - Sample matches:
    - Line 19: `2025-12-06T06:15:58.5335988Z Collecting async-timeout<6.0,>=4.0 (from aiohttp)`
    - Line 20: `2025-12-06T06:15:58.5525810Z   Downloading async_timeout-5.0.1-py3-none-any.whl.metadata (5.1 kB)`
    - Line 37: `2025-12-06T06:15:59.4291196Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 4. Run Biometric Voice E2E / Mock Biometric Tests - stt-transcription

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2025-12-06T06:15:36Z
**Completed**: 2025-12-06T06:16:32Z
**Duration**: 56 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984432958/job/57316363693)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 64: `2025-12-06T06:16:29.4443315Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 65: `2025-12-06T06:16:29.4452444Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 66: `2025-12-06T06:16:29.4813032Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:16:29.8507610Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 3
  - Sample matches:
    - Line 72: `2025-12-06T06:16:29.4915803Z   if-no-files-found: warn`
    - Line 87: `2025-12-06T06:16:29.7013930Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 97: `2025-12-06T06:16:29.8507610Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `AssertionError|Exception`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2025-12-06T06:16:20.1164714Z   Using cached exceptiongroup-1.3.1-py3-none-any.whl.metadata (6.7 kB)`
    - Line 52: `2025-12-06T06:16:21.6395847Z Using cached exceptiongroup-1.3.1-py3-none-any.whl (16 kB)`
    - Line 61: `2025-12-06T06:16:21.7966691Z Installing collected packages: typing-extensions, tomli, pygments, prop`

- Pattern: `timeout|timed out`
  - Occurrences: 6
  - Sample matches:
    - Line 19: `2025-12-06T06:16:20.3723407Z Collecting async-timeout<6.0,>=4.0 (from aiohttp)`
    - Line 20: `2025-12-06T06:16:20.3756494Z   Downloading async_timeout-5.0.1-py3-none-any.whl.metadata (5.1 kB)`
    - Line 37: `2025-12-06T06:16:21.2428798Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 5. Run Biometric Voice E2E / Mock Biometric Tests - anti-spoofing

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2025-12-06T06:15:04Z
**Completed**: 2025-12-06T06:15:50Z
**Duration**: 46 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984432958/job/57316363696)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 64: `2025-12-06T06:15:48.3725874Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 65: `2025-12-06T06:15:48.3734986Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 66: `2025-12-06T06:15:48.4085163Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:15:48.7842333Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 3
  - Sample matches:
    - Line 72: `2025-12-06T06:15:48.4188709Z   if-no-files-found: warn`
    - Line 87: `2025-12-06T06:15:48.6363093Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 97: `2025-12-06T06:15:48.7842333Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `AssertionError|Exception`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2025-12-06T06:15:39.1107400Z   Using cached exceptiongroup-1.3.1-py3-none-any.whl.metadata (6.7 kB)`
    - Line 52: `2025-12-06T06:15:40.8595560Z Using cached exceptiongroup-1.3.1-py3-none-any.whl (16 kB)`
    - Line 61: `2025-12-06T06:15:41.0169423Z Installing collected packages: typing-extensions, tomli, pygments, prop`

- Pattern: `timeout|timed out`
  - Occurrences: 6
  - Sample matches:
    - Line 19: `2025-12-06T06:15:39.4444112Z Collecting async-timeout<6.0,>=4.0 (from aiohttp)`
    - Line 20: `2025-12-06T06:15:39.4599058Z   Downloading async_timeout-5.0.1-py3-none-any.whl.metadata (5.1 kB)`
    - Line 37: `2025-12-06T06:15:40.4121728Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 6. Run Biometric Voice E2E / Mock Biometric Tests - embedding-validation

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2025-12-06T06:15:35Z
**Completed**: 2025-12-06T06:16:15Z
**Duration**: 40 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984432958/job/57316363697)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 64: `2025-12-06T06:16:13.8694551Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 65: `2025-12-06T06:16:13.8703821Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 66: `2025-12-06T06:16:13.9086344Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:16:14.2868650Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 3
  - Sample matches:
    - Line 72: `2025-12-06T06:16:13.9190343Z   if-no-files-found: warn`
    - Line 87: `2025-12-06T06:16:14.1347024Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 97: `2025-12-06T06:16:14.2868650Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `AssertionError|Exception`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2025-12-06T06:16:04.5013392Z   Using cached exceptiongroup-1.3.1-py3-none-any.whl.metadata (6.7 kB)`
    - Line 52: `2025-12-06T06:16:05.9767855Z Using cached exceptiongroup-1.3.1-py3-none-any.whl (16 kB)`
    - Line 61: `2025-12-06T06:16:06.1349823Z Installing collected packages: typing-extensions, tomli, pygments, prop`

- Pattern: `timeout|timed out`
  - Occurrences: 6
  - Sample matches:
    - Line 19: `2025-12-06T06:16:04.7036576Z Collecting async-timeout<6.0,>=4.0 (from aiohttp)`
    - Line 20: `2025-12-06T06:16:04.7081609Z   Downloading async_timeout-5.0.1-py3-none-any.whl.metadata (5.1 kB)`
    - Line 37: `2025-12-06T06:16:05.5947844Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 7. Run Biometric Voice E2E / Mock Biometric Tests - edge-case-noise

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2025-12-06T06:15:29Z
**Completed**: 2025-12-06T06:16:15Z
**Duration**: 46 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984432958/job/57316363699)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 64: `2025-12-06T06:16:13.9246131Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 65: `2025-12-06T06:16:13.9255113Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 66: `2025-12-06T06:16:13.9619189Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:16:14.3377642Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 3
  - Sample matches:
    - Line 72: `2025-12-06T06:16:13.9722362Z   if-no-files-found: warn`
    - Line 87: `2025-12-06T06:16:14.1908889Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 97: `2025-12-06T06:16:14.3377642Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `AssertionError|Exception`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2025-12-06T06:16:04.5477537Z   Using cached exceptiongroup-1.3.1-py3-none-any.whl.metadata (6.7 kB)`
    - Line 52: `2025-12-06T06:16:05.9127907Z Using cached exceptiongroup-1.3.1-py3-none-any.whl (16 kB)`
    - Line 61: `2025-12-06T06:16:06.0785676Z Installing collected packages: typing-extensions, tomli, pygments, prop`

- Pattern: `timeout|timed out`
  - Occurrences: 6
  - Sample matches:
    - Line 19: `2025-12-06T06:16:04.7529571Z Collecting async-timeout<6.0,>=4.0 (from aiohttp)`
    - Line 20: `2025-12-06T06:16:04.7562269Z   Downloading async_timeout-5.0.1-py3-none-any.whl.metadata (5.1 kB)`
    - Line 37: `2025-12-06T06:16:05.6353055Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 8. Run Biometric Voice E2E / Mock Biometric Tests - edge-case-voice-drift

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2025-12-06T06:15:32Z
**Completed**: 2025-12-06T06:16:24Z
**Duration**: 52 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984432958/job/57316363700)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 64: `2025-12-06T06:16:21.8054562Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 65: `2025-12-06T06:16:21.8064052Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 66: `2025-12-06T06:16:21.8413371Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:16:22.2346281Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 3
  - Sample matches:
    - Line 72: `2025-12-06T06:16:21.8515804Z   if-no-files-found: warn`
    - Line 87: `2025-12-06T06:16:22.0800016Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 97: `2025-12-06T06:16:22.2346281Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `AssertionError|Exception`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2025-12-06T06:16:11.7816411Z   Using cached exceptiongroup-1.3.1-py3-none-any.whl.metadata (6.7 kB)`
    - Line 52: `2025-12-06T06:16:14.1096993Z Using cached exceptiongroup-1.3.1-py3-none-any.whl (16 kB)`
    - Line 61: `2025-12-06T06:16:14.2940692Z Installing collected packages: typing-extensions, tomli, pygments, prop`

- Pattern: `timeout|timed out`
  - Occurrences: 6
  - Sample matches:
    - Line 19: `2025-12-06T06:16:12.1536458Z Collecting async-timeout<6.0,>=4.0 (from aiohttp)`
    - Line 20: `2025-12-06T06:16:12.1728402Z   Downloading async_timeout-5.0.1-py3-none-any.whl.metadata (5.1 kB)`
    - Line 37: `2025-12-06T06:16:13.5906880Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 9. Run Biometric Voice E2E / Mock Biometric Tests - adaptive-thresholds

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2025-12-06T06:15:46Z
**Completed**: 2025-12-06T06:16:29Z
**Duration**: 43 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984432958/job/57316363702)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 64: `2025-12-06T06:16:28.0080307Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 65: `2025-12-06T06:16:28.0089789Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 66: `2025-12-06T06:16:28.0530055Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:16:28.4295969Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 3
  - Sample matches:
    - Line 72: `2025-12-06T06:16:28.0629866Z   if-no-files-found: warn`
    - Line 87: `2025-12-06T06:16:28.2802869Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 97: `2025-12-06T06:16:28.4295969Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `AssertionError|Exception`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2025-12-06T06:16:18.8442486Z   Using cached exceptiongroup-1.3.1-py3-none-any.whl.metadata (6.7 kB)`
    - Line 52: `2025-12-06T06:16:20.4680573Z Using cached exceptiongroup-1.3.1-py3-none-any.whl (16 kB)`
    - Line 61: `2025-12-06T06:16:20.6247639Z Installing collected packages: typing-extensions, tomli, pygments, prop`

- Pattern: `timeout|timed out`
  - Occurrences: 6
  - Sample matches:
    - Line 19: `2025-12-06T06:16:19.0515620Z Collecting async-timeout<6.0,>=4.0 (from aiohttp)`
    - Line 20: `2025-12-06T06:16:19.0655597Z   Downloading async_timeout-5.0.1-py3-none-any.whl.metadata (5.1 kB)`
    - Line 37: `2025-12-06T06:16:19.9457118Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 10. Run Biometric Voice E2E / Mock Biometric Tests - edge-case-database-failure

**Status**: ❌ failure
**Category**: Test Failure
**Severity**: HIGH
**Started**: 2025-12-06T06:15:34Z
**Completed**: 2025-12-06T06:16:26Z
**Duration**: 52 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984432958/job/57316363703)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 64: `2025-12-06T06:16:24.7012622Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 65: `2025-12-06T06:16:24.7021620Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 66: `2025-12-06T06:16:24.7385597Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 2
  - Sample matches:
    - Line 69: `2025-12-06T06:16:24.7483494Z   name: test-results-biometric-mock-edge-case-database-failure`
    - Line 97: `2025-12-06T06:16:25.1105231Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 3
  - Sample matches:
    - Line 72: `2025-12-06T06:16:24.7484332Z   if-no-files-found: warn`
    - Line 87: `2025-12-06T06:16:24.9623906Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 97: `2025-12-06T06:16:25.1105231Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `AssertionError|Exception`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2025-12-06T06:16:14.8388569Z   Using cached exceptiongroup-1.3.1-py3-none-any.whl.metadata (6.7 kB)`
    - Line 52: `2025-12-06T06:16:16.8611707Z Using cached exceptiongroup-1.3.1-py3-none-any.whl (16 kB)`
    - Line 61: `2025-12-06T06:16:17.0580367Z Installing collected packages: typing-extensions, tomli, pygments, prop`

- Pattern: `timeout|timed out`
  - Occurrences: 6
  - Sample matches:
    - Line 19: `2025-12-06T06:16:15.1760284Z Collecting async-timeout<6.0,>=4.0 (from aiohttp)`
    - Line 20: `2025-12-06T06:16:15.1894829Z   Downloading async_timeout-5.0.1-py3-none-any.whl.metadata (5.1 kB)`
    - Line 37: `2025-12-06T06:16:16.4432980Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 11. Run Biometric Voice E2E / Mock Biometric Tests - dimension-adaptation

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2025-12-06T06:16:36Z
**Completed**: 2025-12-06T06:17:35Z
**Duration**: 59 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984432958/job/57316363704)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 64: `2025-12-06T06:17:32.7279688Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 65: `2025-12-06T06:17:32.7290070Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 66: `2025-12-06T06:17:32.7669322Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:17:33.1410499Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 3
  - Sample matches:
    - Line 72: `2025-12-06T06:17:32.7770801Z   if-no-files-found: warn`
    - Line 87: `2025-12-06T06:17:32.9929468Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 97: `2025-12-06T06:17:33.1410499Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `AssertionError|Exception`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2025-12-06T06:17:23.2875449Z   Using cached exceptiongroup-1.3.1-py3-none-any.whl.metadata (6.7 kB)`
    - Line 52: `2025-12-06T06:17:24.7906216Z Using cached exceptiongroup-1.3.1-py3-none-any.whl (16 kB)`
    - Line 61: `2025-12-06T06:17:24.9579152Z Installing collected packages: typing-extensions, tomli, pygments, prop`

- Pattern: `timeout|timed out`
  - Occurrences: 6
  - Sample matches:
    - Line 19: `2025-12-06T06:17:23.4747758Z Collecting async-timeout<6.0,>=4.0 (from aiohttp)`
    - Line 20: `2025-12-06T06:17:23.4779104Z   Downloading async_timeout-5.0.1-py3-none-any.whl.metadata (5.1 kB)`
    - Line 37: `2025-12-06T06:17:24.3725343Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 12. Run Biometric Voice E2E / Mock Biometric Tests - edge-case-cold-start

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2025-12-06T06:15:37Z
**Completed**: 2025-12-06T06:16:37Z
**Duration**: 60 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984432958/job/57316363706)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 64: `2025-12-06T06:16:35.2937284Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 65: `2025-12-06T06:16:35.2946315Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 66: `2025-12-06T06:16:35.3310411Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:16:35.7070788Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 3
  - Sample matches:
    - Line 72: `2025-12-06T06:16:35.3411232Z   if-no-files-found: warn`
    - Line 87: `2025-12-06T06:16:35.5538986Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 97: `2025-12-06T06:16:35.7070788Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `AssertionError|Exception`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2025-12-06T06:16:25.3050609Z   Using cached exceptiongroup-1.3.1-py3-none-any.whl.metadata (6.7 kB)`
    - Line 52: `2025-12-06T06:16:27.3219628Z Using cached exceptiongroup-1.3.1-py3-none-any.whl (16 kB)`
    - Line 61: `2025-12-06T06:16:27.4989553Z Installing collected packages: typing-extensions, tomli, pygments, prop`

- Pattern: `timeout|timed out`
  - Occurrences: 6
  - Sample matches:
    - Line 19: `2025-12-06T06:16:25.5023964Z Collecting async-timeout<6.0,>=4.0 (from aiohttp)`
    - Line 20: `2025-12-06T06:16:25.5057854Z   Downloading async_timeout-5.0.1-py3-none-any.whl.metadata (5.1 kB)`
    - Line 37: `2025-12-06T06:16:26.8260427Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 13. Run Biometric Voice E2E / Mock Biometric Tests - performance-baseline

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2025-12-06T06:15:31Z
**Completed**: 2025-12-06T06:16:10Z
**Duration**: 39 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984432958/job/57316363709)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 64: `2025-12-06T06:16:08.8691773Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 65: `2025-12-06T06:16:08.8702422Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 66: `2025-12-06T06:16:08.9083979Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:16:09.2815989Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 3
  - Sample matches:
    - Line 72: `2025-12-06T06:16:08.9187717Z   if-no-files-found: warn`
    - Line 87: `2025-12-06T06:16:09.1332522Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 97: `2025-12-06T06:16:09.2815989Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `AssertionError|Exception`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2025-12-06T06:16:00.0512129Z   Using cached exceptiongroup-1.3.1-py3-none-any.whl.metadata (6.7 kB)`
    - Line 52: `2025-12-06T06:16:01.5148679Z Using cached exceptiongroup-1.3.1-py3-none-any.whl (16 kB)`
    - Line 61: `2025-12-06T06:16:01.6718712Z Installing collected packages: typing-extensions, tomli, pygments, prop`

- Pattern: `timeout|timed out`
  - Occurrences: 6
  - Sample matches:
    - Line 19: `2025-12-06T06:16:00.2906079Z Collecting async-timeout<6.0,>=4.0 (from aiohttp)`
    - Line 20: `2025-12-06T06:16:00.2979299Z   Downloading async_timeout-5.0.1-py3-none-any.whl.metadata (5.1 kB)`
    - Line 37: `2025-12-06T06:16:01.2034634Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 14. Run Biometric Voice E2E / Mock Biometric Tests - end-to-end-flow

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2025-12-06T06:15:36Z
**Completed**: 2025-12-06T06:16:23Z
**Duration**: 47 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984432958/job/57316363711)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 64: `2025-12-06T06:16:21.6910720Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 65: `2025-12-06T06:16:21.6920216Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 66: `2025-12-06T06:16:21.7295943Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:16:22.1065257Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 3
  - Sample matches:
    - Line 72: `2025-12-06T06:16:21.7397387Z   if-no-files-found: warn`
    - Line 87: `2025-12-06T06:16:21.9548376Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 97: `2025-12-06T06:16:22.1065257Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `AssertionError|Exception`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2025-12-06T06:16:12.4245923Z   Using cached exceptiongroup-1.3.1-py3-none-any.whl.metadata (6.7 kB)`
    - Line 52: `2025-12-06T06:16:14.1762878Z Using cached exceptiongroup-1.3.1-py3-none-any.whl (16 kB)`
    - Line 61: `2025-12-06T06:16:14.3419519Z Installing collected packages: typing-extensions, tomli, pygments, prop`

- Pattern: `timeout|timed out`
  - Occurrences: 6
  - Sample matches:
    - Line 19: `2025-12-06T06:16:12.7553381Z Collecting async-timeout<6.0,>=4.0 (from aiohttp)`
    - Line 20: `2025-12-06T06:16:12.7718846Z   Downloading async_timeout-5.0.1-py3-none-any.whl.metadata (5.1 kB)`
    - Line 37: `2025-12-06T06:16:13.7080922Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 15. Run Biometric Voice E2E / Mock Biometric Tests - voice-verification

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2025-12-06T06:15:34Z
**Completed**: 2025-12-06T06:16:22Z
**Duration**: 48 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984432958/job/57316363714)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 64: `2025-12-06T06:16:20.1897251Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 65: `2025-12-06T06:16:20.1906594Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 66: `2025-12-06T06:16:20.2329505Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:16:20.5808215Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 3
  - Sample matches:
    - Line 72: `2025-12-06T06:16:20.2424626Z   if-no-files-found: warn`
    - Line 87: `2025-12-06T06:16:20.4471316Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 97: `2025-12-06T06:16:20.5808215Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `AssertionError|Exception`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2025-12-06T06:16:11.4125714Z   Using cached exceptiongroup-1.3.1-py3-none-any.whl.metadata (6.7 kB)`
    - Line 52: `2025-12-06T06:16:13.3268981Z Using cached exceptiongroup-1.3.1-py3-none-any.whl (16 kB)`
    - Line 61: `2025-12-06T06:16:13.4848963Z Installing collected packages: typing-extensions, tomli, pygments, prop`

- Pattern: `timeout|timed out`
  - Occurrences: 6
  - Sample matches:
    - Line 19: `2025-12-06T06:16:11.6906535Z Collecting async-timeout<6.0,>=4.0 (from aiohttp)`
    - Line 20: `2025-12-06T06:16:11.7011998Z   Downloading async_timeout-5.0.1-py3-none-any.whl.metadata (5.1 kB)`
    - Line 37: `2025-12-06T06:16:12.5155872Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 16. Run Biometric Voice E2E / Mock Biometric Tests - voice-synthesis-detection

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2025-12-06T06:16:27Z
**Completed**: 2025-12-06T06:17:18Z
**Duration**: 51 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984432958/job/57316363716)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 64: `2025-12-06T06:17:16.6545004Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 65: `2025-12-06T06:17:16.6554140Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 66: `2025-12-06T06:17:16.6927286Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:17:17.0713628Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 3
  - Sample matches:
    - Line 72: `2025-12-06T06:17:16.7028292Z   if-no-files-found: warn`
    - Line 87: `2025-12-06T06:17:16.9201519Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 97: `2025-12-06T06:17:17.0713628Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `AssertionError|Exception`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2025-12-06T06:17:06.7440224Z   Using cached exceptiongroup-1.3.1-py3-none-any.whl.metadata (6.7 kB)`
    - Line 52: `2025-12-06T06:17:08.6689029Z Using cached exceptiongroup-1.3.1-py3-none-any.whl (16 kB)`
    - Line 61: `2025-12-06T06:17:08.8329190Z Installing collected packages: typing-extensions, tomli, pygments, prop`

- Pattern: `timeout|timed out`
  - Occurrences: 6
  - Sample matches:
    - Line 19: `2025-12-06T06:17:07.0213502Z Collecting async-timeout<6.0,>=4.0 (from aiohttp)`
    - Line 20: `2025-12-06T06:17:07.0368719Z   Downloading async_timeout-5.0.1-py3-none-any.whl.metadata (5.1 kB)`
    - Line 37: `2025-12-06T06:17:07.9577226Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 17. Run Biometric Voice E2E / Mock Biometric Tests - replay-attack-detection

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2025-12-06T06:16:09Z
**Completed**: 2025-12-06T06:16:56Z
**Duration**: 47 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984432958/job/57316363718)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 64: `2025-12-06T06:16:54.6707428Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 65: `2025-12-06T06:16:54.6717325Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 66: `2025-12-06T06:16:54.7097609Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:16:55.0974395Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 3
  - Sample matches:
    - Line 72: `2025-12-06T06:16:54.7202362Z   if-no-files-found: warn`
    - Line 87: `2025-12-06T06:16:54.9418122Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 97: `2025-12-06T06:16:55.0974395Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `AssertionError|Exception`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2025-12-06T06:16:44.5445963Z   Using cached exceptiongroup-1.3.1-py3-none-any.whl.metadata (6.7 kB)`
    - Line 52: `2025-12-06T06:16:45.9259455Z Using cached exceptiongroup-1.3.1-py3-none-any.whl (16 kB)`
    - Line 61: `2025-12-06T06:16:46.1063586Z Installing collected packages: typing-extensions, tomli, pygments, prop`

- Pattern: `timeout|timed out`
  - Occurrences: 6
  - Sample matches:
    - Line 19: `2025-12-06T06:16:44.7355314Z Collecting async-timeout<6.0,>=4.0 (from aiohttp)`
    - Line 20: `2025-12-06T06:16:44.7389538Z   Downloading async_timeout-5.0.1-py3-none-any.whl.metadata (5.1 kB)`
    - Line 37: `2025-12-06T06:16:45.6189992Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 18. Run Biometric Voice E2E / Mock Biometric Tests - security-validation

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2025-12-06T06:16:09Z
**Completed**: 2025-12-06T06:16:55Z
**Duration**: 46 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984432958/job/57316363724)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 64: `2025-12-06T06:16:53.2519535Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 65: `2025-12-06T06:16:53.2529220Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 66: `2025-12-06T06:16:53.2913190Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:16:53.6678220Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 3
  - Sample matches:
    - Line 72: `2025-12-06T06:16:53.3016210Z   if-no-files-found: warn`
    - Line 87: `2025-12-06T06:16:53.5128099Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 97: `2025-12-06T06:16:53.6678220Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `AssertionError|Exception`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2025-12-06T06:16:44.0569578Z   Using cached exceptiongroup-1.3.1-py3-none-any.whl.metadata (6.7 kB)`
    - Line 52: `2025-12-06T06:16:45.4195085Z Using cached exceptiongroup-1.3.1-py3-none-any.whl (16 kB)`
    - Line 61: `2025-12-06T06:16:45.5849326Z Installing collected packages: typing-extensions, tomli, pygments, prop`

- Pattern: `timeout|timed out`
  - Occurrences: 6
  - Sample matches:
    - Line 19: `2025-12-06T06:16:44.2659884Z Collecting async-timeout<6.0,>=4.0 (from aiohttp)`
    - Line 20: `2025-12-06T06:16:44.2706811Z   Downloading async_timeout-5.0.1-py3-none-any.whl.metadata (5.1 kB)`
    - Line 37: `2025-12-06T06:16:45.1462954Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 19. Run Unlock Integration E2E / Generate Test Summary

**Status**: ❌ failure
**Category**: Test Failure
**Severity**: HIGH
**Started**: 2025-12-06T06:15:20Z
**Completed**: 2025-12-06T06:15:24Z
**Duration**: 4 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984432958/job/57316373375)

#### Failed Steps

- **Step 4**: Check Test Status

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:15:22.8964530Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 13
  - Sample matches:
    - Line 39: `2025-12-06T06:15:22.7289901Z [36;1mTOTAL_FAILED=0[0m`
    - Line 44: `2025-12-06T06:15:22.7293853Z [36;1m    FAILED=$(jq -r '.summary.failed' "$report")[0m`
    - Line 46: `2025-12-06T06:15:22.7295536Z [36;1m    TOTAL_FAILED=$((TOTAL_FAILED + FAILED))[0m`

#### Suggested Fixes

1. Review test cases and ensure code changes haven't broken existing functionality

---

### 20. Run Biometric Voice E2E / Generate Biometric Test Summary

**Status**: ❌ failure
**Category**: Test Failure
**Severity**: HIGH
**Started**: 2025-12-06T06:20:31Z
**Completed**: 2025-12-06T06:20:37Z
**Duration**: 6 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984432958/job/57316439000)

#### Failed Steps

- **Step 4**: Check Test Status

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:20:35.1172775Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 11
  - Sample matches:
    - Line 32: `2025-12-06T06:20:34.8086995Z [36;1mTOTAL_FAILED=0[0m`
    - Line 37: `2025-12-06T06:20:34.8094241Z [36;1m    FAILED=$(jq -r '.summary.failed' "$report")[0m`
    - Line 39: `2025-12-06T06:20:34.8097476Z [36;1m    TOTAL_FAILED=$((TOTAL_FAILED + FAILED))[0m`

#### Suggested Fixes

1. Review test cases and ensure code changes haven't broken existing functionality

---

### 21. Generate Combined Test Summary

**Status**: ❌ failure
**Category**: Test Failure
**Severity**: HIGH
**Started**: 2025-12-06T06:20:47Z
**Completed**: 2025-12-06T06:20:52Z
**Duration**: 5 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984432958/job/57316521758)

#### Failed Steps

- **Step 2**: Generate Combined Summary

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 1
  - Sample matches:
    - Line 83: `2025-12-06T06:20:49.9659168Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 9
  - Sample matches:
    - Line 47: `2025-12-06T06:20:49.9464818Z [36;1mif [ "failure" = "success" ]; then[0m`
    - Line 50: `2025-12-06T06:20:49.9466790Z [36;1m  echo "- ❌ **Unlock Integration E2E:** failure" >> $GITHUB_STEP`
    - Line 54: `2025-12-06T06:20:49.9468920Z [36;1mif [ "failure" = "success" ]; then[0m`

#### Suggested Fixes

1. Review test cases and ensure code changes haven't broken existing functionality

---

### 22. Notify Test Status

**Status**: ❌ failure
**Category**: Test Failure
**Severity**: HIGH
**Started**: 2025-12-06T06:21:13Z
**Completed**: 2025-12-06T06:21:15Z
**Duration**: 2 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984432958/job/57316527494)

#### Failed Steps

- **Step 3**: Failure Notification

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 1
  - Sample matches:
    - Line -1: `2025-12-06T06:21:14.5912889Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 3
  - Sample matches:
    - Line -9: `2025-12-06T06:21:14.4937468Z ##[group]Run echo "❌ Unlock tests failed - 'unlock my screen' may be br`
    - Line -8: `2025-12-06T06:21:14.4938542Z [36;1mecho "❌ Unlock tests failed - 'unlock my screen' may be broken!"`
    - Line -3: `2025-12-06T06:21:14.5885846Z ❌ Unlock tests failed - 'unlock my screen' may be broken!`

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

📊 *Report generated on 2025-12-06T06:22:59.260946*
🤖 *JARVIS CI/CD Auto-PR Manager*
