# CI/CD Failure Analysis Report

## Executive Summary

- **Workflow**: Complete Unlock Test Suite (Master)
- **Run Number**: #238
- **Branch**: `cursor/investigate-voice-authentication-system-integration-gemini-3-pro-preview-e56d`
- **Commit**: `1f39d702bfbd2938d342f2fa5b6f57bdde8c8dd1`
- **Status**: ❌ FAILED
- **Timestamp**: 2025-12-06T06:15:50Z
- **Triggered By**: @cursor[bot]
- **Workflow URL**: [View Run](https://github.com/drussell23/JARVIS/actions/runs/19984493847)

## Failure Overview

Total Failed Jobs: **22**

| # | Job Name | Category | Severity | Duration |
|---|----------|----------|----------|----------|
| 1 | Run Unlock Integration E2E / Mock Tests - security-checks | test_failure | high | 36s |
| 2 | Run Biometric Voice E2E / Mock Biometric Tests - wake-word-detection | timeout | high | 43s |
| 3 | Run Biometric Voice E2E / Mock Biometric Tests - dimension-adaptation | timeout | high | 60s |
| 4 | Run Biometric Voice E2E / Mock Biometric Tests - adaptive-thresholds | timeout | high | 56s |
| 5 | Run Biometric Voice E2E / Mock Biometric Tests - stt-transcription | timeout | high | 49s |
| 6 | Run Biometric Voice E2E / Mock Biometric Tests - embedding-validation | timeout | high | 44s |
| 7 | Run Biometric Voice E2E / Mock Biometric Tests - profile-quality-assessment | timeout | high | 43s |
| 8 | Run Biometric Voice E2E / Mock Biometric Tests - edge-case-voice-drift | timeout | high | 60s |
| 9 | Run Biometric Voice E2E / Mock Biometric Tests - edge-case-noise | timeout | high | 50s |
| 10 | Run Biometric Voice E2E / Mock Biometric Tests - edge-case-cold-start | timeout | high | 58s |
| 11 | Run Biometric Voice E2E / Mock Biometric Tests - voice-verification | timeout | high | 54s |
| 12 | Run Biometric Voice E2E / Mock Biometric Tests - replay-attack-detection | timeout | high | 62s |
| 13 | Run Biometric Voice E2E / Mock Biometric Tests - voice-synthesis-detection | timeout | high | 56s |
| 14 | Run Biometric Voice E2E / Mock Biometric Tests - end-to-end-flow | timeout | high | 41s |
| 15 | Run Biometric Voice E2E / Mock Biometric Tests - edge-case-database-failure | test_failure | high | 44s |
| 16 | Run Biometric Voice E2E / Mock Biometric Tests - performance-baseline | timeout | high | 41s |
| 17 | Run Biometric Voice E2E / Mock Biometric Tests - anti-spoofing | timeout | high | 58s |
| 18 | Run Biometric Voice E2E / Mock Biometric Tests - security-validation | timeout | high | 44s |
| 19 | Run Unlock Integration E2E / Generate Test Summary | test_failure | high | 6s |
| 20 | Run Biometric Voice E2E / Generate Biometric Test Summary | test_failure | high | 6s |
| 21 | Generate Combined Test Summary | test_failure | high | 2s |
| 22 | Notify Test Status | test_failure | high | 4s |

## Detailed Analysis

### 1. Run Unlock Integration E2E / Mock Tests - security-checks

**Status**: ❌ failure
**Category**: Test Failure
**Severity**: HIGH
**Started**: 2025-12-06T06:19:23Z
**Completed**: 2025-12-06T06:19:59Z
**Duration**: 36 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984493847/job/57316418691)

#### Failed Steps

- **Step 6**: Run Mock Tests

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 2
  - Sample matches:
    - Line 39: `2025-12-06T06:19:57.0374958Z 2025-12-06 06:19:57,037 - __main__ - ERROR - ❌ 1 test(s) failed`
    - Line 56: `2025-12-06T06:19:57.0495512Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 3
  - Sample matches:
    - Line 39: `2025-12-06T06:19:57.0374958Z 2025-12-06 06:19:57,037 - __main__ - ERROR - ❌ 1 test(s) failed`
    - Line 48: `2025-12-06T06:19:57.0382551Z ❌ Failed: 1`
    - Line 97: `2025-12-06T06:19:57.6662978Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 2
  - Sample matches:
    - Line 62: `2025-12-06T06:19:57.0565500Z   if-no-files-found: warn`
    - Line 97: `2025-12-06T06:19:57.6662978Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations
2. Review test cases and ensure code changes haven't broken existing functionality

---

### 2. Run Biometric Voice E2E / Mock Biometric Tests - wake-word-detection

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2025-12-06T06:20:55Z
**Completed**: 2025-12-06T06:21:38Z
**Duration**: 43 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984493847/job/57316456865)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 64: `2025-12-06T06:21:36.4261832Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 65: `2025-12-06T06:21:36.4270640Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 66: `2025-12-06T06:21:36.4637756Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:21:36.8407364Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 3
  - Sample matches:
    - Line 72: `2025-12-06T06:21:36.4749429Z   if-no-files-found: warn`
    - Line 87: `2025-12-06T06:21:36.6905790Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 97: `2025-12-06T06:21:36.8407364Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `AssertionError|Exception`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2025-12-06T06:21:27.4165131Z   Using cached exceptiongroup-1.3.1-py3-none-any.whl.metadata (6.7 kB)`
    - Line 52: `2025-12-06T06:21:28.9713749Z Using cached exceptiongroup-1.3.1-py3-none-any.whl (16 kB)`
    - Line 61: `2025-12-06T06:21:29.1287916Z Installing collected packages: typing-extensions, tomli, pygments, prop`

- Pattern: `timeout|timed out`
  - Occurrences: 6
  - Sample matches:
    - Line 19: `2025-12-06T06:21:27.6347645Z Collecting async-timeout<6.0,>=4.0 (from aiohttp)`
    - Line 20: `2025-12-06T06:21:27.6492729Z   Downloading async_timeout-5.0.1-py3-none-any.whl.metadata (5.1 kB)`
    - Line 37: `2025-12-06T06:21:28.5116137Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 3. Run Biometric Voice E2E / Mock Biometric Tests - dimension-adaptation

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2025-12-06T06:20:37Z
**Completed**: 2025-12-06T06:21:37Z
**Duration**: 60 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984493847/job/57316456866)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 64: `2025-12-06T06:21:34.3400319Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 65: `2025-12-06T06:21:34.3409575Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 66: `2025-12-06T06:21:34.3792820Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:21:34.7630185Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 3
  - Sample matches:
    - Line 72: `2025-12-06T06:21:34.3894883Z   if-no-files-found: warn`
    - Line 87: `2025-12-06T06:21:34.6072306Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 97: `2025-12-06T06:21:34.7630185Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `AssertionError|Exception`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2025-12-06T06:21:25.1376010Z   Using cached exceptiongroup-1.3.1-py3-none-any.whl.metadata (6.7 kB)`
    - Line 52: `2025-12-06T06:21:26.5445171Z Using cached exceptiongroup-1.3.1-py3-none-any.whl (16 kB)`
    - Line 61: `2025-12-06T06:21:26.7055677Z Installing collected packages: typing-extensions, tomli, pygments, prop`

- Pattern: `timeout|timed out`
  - Occurrences: 6
  - Sample matches:
    - Line 19: `2025-12-06T06:21:25.3155034Z Collecting async-timeout<6.0,>=4.0 (from aiohttp)`
    - Line 20: `2025-12-06T06:21:25.3214480Z   Downloading async_timeout-5.0.1-py3-none-any.whl.metadata (5.1 kB)`
    - Line 37: `2025-12-06T06:21:26.2184434Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 4. Run Biometric Voice E2E / Mock Biometric Tests - adaptive-thresholds

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2025-12-06T06:20:53Z
**Completed**: 2025-12-06T06:21:49Z
**Duration**: 56 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984493847/job/57316456868)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 64: `2025-12-06T06:21:47.2776071Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 65: `2025-12-06T06:21:47.2785632Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 66: `2025-12-06T06:21:47.3128513Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:21:47.6907179Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 3
  - Sample matches:
    - Line 72: `2025-12-06T06:21:47.3231721Z   if-no-files-found: warn`
    - Line 87: `2025-12-06T06:21:47.5403301Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 97: `2025-12-06T06:21:47.6907179Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `AssertionError|Exception`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2025-12-06T06:21:38.2426852Z   Using cached exceptiongroup-1.3.1-py3-none-any.whl.metadata (6.7 kB)`
    - Line 52: `2025-12-06T06:21:39.5743359Z Using cached exceptiongroup-1.3.1-py3-none-any.whl (16 kB)`
    - Line 61: `2025-12-06T06:21:39.7396562Z Installing collected packages: typing-extensions, tomli, pygments, prop`

- Pattern: `timeout|timed out`
  - Occurrences: 6
  - Sample matches:
    - Line 19: `2025-12-06T06:21:38.4210459Z Collecting async-timeout<6.0,>=4.0 (from aiohttp)`
    - Line 20: `2025-12-06T06:21:38.4252333Z   Downloading async_timeout-5.0.1-py3-none-any.whl.metadata (5.1 kB)`
    - Line 37: `2025-12-06T06:21:39.2838504Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 5. Run Biometric Voice E2E / Mock Biometric Tests - stt-transcription

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2025-12-06T06:20:37Z
**Completed**: 2025-12-06T06:21:26Z
**Duration**: 49 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984493847/job/57316456869)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 64: `2025-12-06T06:21:23.7966115Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 65: `2025-12-06T06:21:23.7976388Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 66: `2025-12-06T06:21:23.8352163Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:21:24.2231611Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 3
  - Sample matches:
    - Line 72: `2025-12-06T06:21:23.8453358Z   if-no-files-found: warn`
    - Line 87: `2025-12-06T06:21:24.0728928Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 97: `2025-12-06T06:21:24.2231611Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `AssertionError|Exception`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2025-12-06T06:21:13.6423992Z   Using cached exceptiongroup-1.3.1-py3-none-any.whl.metadata (6.7 kB)`
    - Line 52: `2025-12-06T06:21:15.8341127Z Using cached exceptiongroup-1.3.1-py3-none-any.whl (16 kB)`
    - Line 61: `2025-12-06T06:21:15.9988940Z Installing collected packages: typing-extensions, tomli, pygments, prop`

- Pattern: `timeout|timed out`
  - Occurrences: 6
  - Sample matches:
    - Line 19: `2025-12-06T06:21:13.9483113Z Collecting async-timeout<6.0,>=4.0 (from aiohttp)`
    - Line 20: `2025-12-06T06:21:13.9590478Z   Downloading async_timeout-5.0.1-py3-none-any.whl.metadata (5.1 kB)`
    - Line 37: `2025-12-06T06:21:14.9079064Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 6. Run Biometric Voice E2E / Mock Biometric Tests - embedding-validation

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2025-12-06T06:20:59Z
**Completed**: 2025-12-06T06:21:43Z
**Duration**: 44 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984493847/job/57316456871)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 64: `2025-12-06T06:21:41.3823114Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 65: `2025-12-06T06:21:41.3833442Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 66: `2025-12-06T06:21:41.4340116Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:21:41.8247025Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 3
  - Sample matches:
    - Line 72: `2025-12-06T06:21:41.4445007Z   if-no-files-found: warn`
    - Line 87: `2025-12-06T06:21:41.6672154Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 97: `2025-12-06T06:21:41.8247025Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `AssertionError|Exception`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2025-12-06T06:21:31.3206720Z   Using cached exceptiongroup-1.3.1-py3-none-any.whl.metadata (6.7 kB)`
    - Line 52: `2025-12-06T06:21:33.5517575Z Using cached exceptiongroup-1.3.1-py3-none-any.whl (16 kB)`
    - Line 61: `2025-12-06T06:21:33.7132574Z Installing collected packages: typing-extensions, tomli, pygments, prop`

- Pattern: `timeout|timed out`
  - Occurrences: 6
  - Sample matches:
    - Line 19: `2025-12-06T06:21:31.5411287Z Collecting async-timeout<6.0,>=4.0 (from aiohttp)`
    - Line 20: `2025-12-06T06:21:31.5560255Z   Downloading async_timeout-5.0.1-py3-none-any.whl.metadata (5.1 kB)`
    - Line 37: `2025-12-06T06:21:32.4483652Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 7. Run Biometric Voice E2E / Mock Biometric Tests - profile-quality-assessment

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2025-12-06T06:20:40Z
**Completed**: 2025-12-06T06:21:23Z
**Duration**: 43 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984493847/job/57316456875)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 64: `2025-12-06T06:21:22.5754700Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 65: `2025-12-06T06:21:22.5764423Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 66: `2025-12-06T06:21:22.6106378Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:21:22.9785568Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 3
  - Sample matches:
    - Line 72: `2025-12-06T06:21:22.6208634Z   if-no-files-found: warn`
    - Line 87: `2025-12-06T06:21:22.8314001Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 97: `2025-12-06T06:21:22.9785568Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `AssertionError|Exception`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2025-12-06T06:21:13.6839996Z   Using cached exceptiongroup-1.3.1-py3-none-any.whl.metadata (6.7 kB)`
    - Line 52: `2025-12-06T06:21:15.3570950Z Using cached exceptiongroup-1.3.1-py3-none-any.whl (16 kB)`
    - Line 61: `2025-12-06T06:21:15.5135352Z Installing collected packages: typing-extensions, tomli, pygments, prop`

- Pattern: `timeout|timed out`
  - Occurrences: 6
  - Sample matches:
    - Line 19: `2025-12-06T06:21:13.9258638Z Collecting async-timeout<6.0,>=4.0 (from aiohttp)`
    - Line 20: `2025-12-06T06:21:13.9292444Z   Downloading async_timeout-5.0.1-py3-none-any.whl.metadata (5.1 kB)`
    - Line 37: `2025-12-06T06:21:14.8165987Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 8. Run Biometric Voice E2E / Mock Biometric Tests - edge-case-voice-drift

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2025-12-06T06:21:18Z
**Completed**: 2025-12-06T06:22:18Z
**Duration**: 60 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984493847/job/57316456878)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 64: `2025-12-06T06:22:16.2433092Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 65: `2025-12-06T06:22:16.2442484Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 66: `2025-12-06T06:22:16.2787236Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:22:16.6480134Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 3
  - Sample matches:
    - Line 72: `2025-12-06T06:22:16.2891241Z   if-no-files-found: warn`
    - Line 87: `2025-12-06T06:22:16.5000427Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 97: `2025-12-06T06:22:16.6480134Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `AssertionError|Exception`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2025-12-06T06:22:07.1210646Z   Using cached exceptiongroup-1.3.1-py3-none-any.whl.metadata (6.7 kB)`
    - Line 52: `2025-12-06T06:22:08.5733677Z Using cached exceptiongroup-1.3.1-py3-none-any.whl (16 kB)`
    - Line 61: `2025-12-06T06:22:08.7322732Z Installing collected packages: typing-extensions, tomli, pygments, prop`

- Pattern: `timeout|timed out`
  - Occurrences: 6
  - Sample matches:
    - Line 19: `2025-12-06T06:22:07.3098064Z Collecting async-timeout<6.0,>=4.0 (from aiohttp)`
    - Line 20: `2025-12-06T06:22:07.3130843Z   Downloading async_timeout-5.0.1-py3-none-any.whl.metadata (5.1 kB)`
    - Line 37: `2025-12-06T06:22:08.1909524Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 9. Run Biometric Voice E2E / Mock Biometric Tests - edge-case-noise

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2025-12-06T06:21:04Z
**Completed**: 2025-12-06T06:21:54Z
**Duration**: 50 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984493847/job/57316456881)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 64: `2025-12-06T06:21:52.4779941Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 65: `2025-12-06T06:21:52.4790379Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 66: `2025-12-06T06:21:52.5186471Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:21:52.8921309Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 3
  - Sample matches:
    - Line 72: `2025-12-06T06:21:52.5287359Z   if-no-files-found: warn`
    - Line 87: `2025-12-06T06:21:52.7422444Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 97: `2025-12-06T06:21:52.8921309Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `AssertionError|Exception`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2025-12-06T06:21:42.4134135Z   Using cached exceptiongroup-1.3.1-py3-none-any.whl.metadata (6.7 kB)`
    - Line 52: `2025-12-06T06:21:44.3919133Z Using cached exceptiongroup-1.3.1-py3-none-any.whl (16 kB)`
    - Line 61: `2025-12-06T06:21:44.5615123Z Installing collected packages: typing-extensions, tomli, pygments, prop`

- Pattern: `timeout|timed out`
  - Occurrences: 6
  - Sample matches:
    - Line 19: `2025-12-06T06:21:42.6425805Z Collecting async-timeout<6.0,>=4.0 (from aiohttp)`
    - Line 20: `2025-12-06T06:21:42.6627300Z   Downloading async_timeout-5.0.1-py3-none-any.whl.metadata (5.1 kB)`
    - Line 37: `2025-12-06T06:21:43.5484829Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 10. Run Biometric Voice E2E / Mock Biometric Tests - edge-case-cold-start

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2025-12-06T06:21:26Z
**Completed**: 2025-12-06T06:22:24Z
**Duration**: 58 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984493847/job/57316456885)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 64: `2025-12-06T06:22:21.5100128Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 65: `2025-12-06T06:22:21.5109267Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 66: `2025-12-06T06:22:21.5479635Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:22:21.9171327Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 3
  - Sample matches:
    - Line 72: `2025-12-06T06:22:21.5580041Z   if-no-files-found: warn`
    - Line 87: `2025-12-06T06:22:21.7694004Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 97: `2025-12-06T06:22:21.9171327Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `AssertionError|Exception`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2025-12-06T06:22:12.5663714Z   Using cached exceptiongroup-1.3.1-py3-none-any.whl.metadata (6.7 kB)`
    - Line 52: `2025-12-06T06:22:13.9612767Z Using cached exceptiongroup-1.3.1-py3-none-any.whl (16 kB)`
    - Line 61: `2025-12-06T06:22:14.1189578Z Installing collected packages: typing-extensions, tomli, pygments, prop`

- Pattern: `timeout|timed out`
  - Occurrences: 6
  - Sample matches:
    - Line 19: `2025-12-06T06:22:12.7650036Z Collecting async-timeout<6.0,>=4.0 (from aiohttp)`
    - Line 20: `2025-12-06T06:22:12.7685246Z   Downloading async_timeout-5.0.1-py3-none-any.whl.metadata (5.1 kB)`
    - Line 37: `2025-12-06T06:22:13.6320457Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 11. Run Biometric Voice E2E / Mock Biometric Tests - voice-verification

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2025-12-06T06:21:23Z
**Completed**: 2025-12-06T06:22:17Z
**Duration**: 54 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984493847/job/57316456890)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 64: `2025-12-06T06:22:14.7711299Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 65: `2025-12-06T06:22:14.7721610Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 66: `2025-12-06T06:22:14.8086035Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:22:15.1801065Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 3
  - Sample matches:
    - Line 72: `2025-12-06T06:22:14.8191951Z   if-no-files-found: warn`
    - Line 87: `2025-12-06T06:22:15.0306638Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 97: `2025-12-06T06:22:15.1801065Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `AssertionError|Exception`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2025-12-06T06:22:05.3890540Z   Using cached exceptiongroup-1.3.1-py3-none-any.whl.metadata (6.7 kB)`
    - Line 52: `2025-12-06T06:22:06.7404816Z Using cached exceptiongroup-1.3.1-py3-none-any.whl (16 kB)`
    - Line 61: `2025-12-06T06:22:06.8979959Z Installing collected packages: typing-extensions, tomli, pygments, prop`

- Pattern: `timeout|timed out`
  - Occurrences: 6
  - Sample matches:
    - Line 19: `2025-12-06T06:22:05.5900881Z Collecting async-timeout<6.0,>=4.0 (from aiohttp)`
    - Line 20: `2025-12-06T06:22:05.5933331Z   Downloading async_timeout-5.0.1-py3-none-any.whl.metadata (5.1 kB)`
    - Line 37: `2025-12-06T06:22:06.4805503Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 12. Run Biometric Voice E2E / Mock Biometric Tests - replay-attack-detection

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2025-12-06T06:21:41Z
**Completed**: 2025-12-06T06:22:43Z
**Duration**: 62 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984493847/job/57316456895)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 64: `2025-12-06T06:22:40.7183103Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 65: `2025-12-06T06:22:40.7192960Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 66: `2025-12-06T06:22:40.7623871Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:22:41.1364255Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 3
  - Sample matches:
    - Line 72: `2025-12-06T06:22:40.7727171Z   if-no-files-found: warn`
    - Line 87: `2025-12-06T06:22:40.9873557Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 97: `2025-12-06T06:22:41.1364255Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `AssertionError|Exception`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2025-12-06T06:22:31.3732233Z   Using cached exceptiongroup-1.3.1-py3-none-any.whl.metadata (6.7 kB)`
    - Line 52: `2025-12-06T06:22:32.8198783Z Using cached exceptiongroup-1.3.1-py3-none-any.whl (16 kB)`
    - Line 61: `2025-12-06T06:22:32.9777058Z Installing collected packages: typing-extensions, tomli, pygments, prop`

- Pattern: `timeout|timed out`
  - Occurrences: 6
  - Sample matches:
    - Line 19: `2025-12-06T06:22:31.5713227Z Collecting async-timeout<6.0,>=4.0 (from aiohttp)`
    - Line 20: `2025-12-06T06:22:31.5747145Z   Downloading async_timeout-5.0.1-py3-none-any.whl.metadata (5.1 kB)`
    - Line 37: `2025-12-06T06:22:32.4557497Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 13. Run Biometric Voice E2E / Mock Biometric Tests - voice-synthesis-detection

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2025-12-06T06:20:42Z
**Completed**: 2025-12-06T06:21:38Z
**Duration**: 56 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984493847/job/57316456896)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 64: `2025-12-06T06:21:36.5353861Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 65: `2025-12-06T06:21:36.5363770Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 66: `2025-12-06T06:21:36.5777785Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:21:36.9633123Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 3
  - Sample matches:
    - Line 72: `2025-12-06T06:21:36.5882067Z   if-no-files-found: warn`
    - Line 87: `2025-12-06T06:21:36.8123282Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 97: `2025-12-06T06:21:36.9633123Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `AssertionError|Exception`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2025-12-06T06:21:27.1716100Z   Using cached exceptiongroup-1.3.1-py3-none-any.whl.metadata (6.7 kB)`
    - Line 52: `2025-12-06T06:21:28.6866518Z Using cached exceptiongroup-1.3.1-py3-none-any.whl (16 kB)`
    - Line 61: `2025-12-06T06:21:28.8456934Z Installing collected packages: typing-extensions, tomli, pygments, prop`

- Pattern: `timeout|timed out`
  - Occurrences: 6
  - Sample matches:
    - Line 19: `2025-12-06T06:21:27.3702656Z Collecting async-timeout<6.0,>=4.0 (from aiohttp)`
    - Line 20: `2025-12-06T06:21:27.3732721Z   Downloading async_timeout-5.0.1-py3-none-any.whl.metadata (5.1 kB)`
    - Line 37: `2025-12-06T06:21:28.2732768Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 14. Run Biometric Voice E2E / Mock Biometric Tests - end-to-end-flow

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2025-12-06T06:20:55Z
**Completed**: 2025-12-06T06:21:36Z
**Duration**: 41 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984493847/job/57316456898)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 64: `2025-12-06T06:21:34.6140390Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 65: `2025-12-06T06:21:34.6155118Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 66: `2025-12-06T06:21:34.6628406Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:21:35.0510448Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 3
  - Sample matches:
    - Line 72: `2025-12-06T06:21:34.6733588Z   if-no-files-found: warn`
    - Line 87: `2025-12-06T06:21:34.8978748Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 97: `2025-12-06T06:21:35.0510448Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `AssertionError|Exception`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2025-12-06T06:21:24.8138628Z   Using cached exceptiongroup-1.3.1-py3-none-any.whl.metadata (6.7 kB)`
    - Line 52: `2025-12-06T06:21:26.2506346Z Using cached exceptiongroup-1.3.1-py3-none-any.whl (16 kB)`
    - Line 61: `2025-12-06T06:21:26.4128830Z Installing collected packages: typing-extensions, tomli, pygments, prop`

- Pattern: `timeout|timed out`
  - Occurrences: 6
  - Sample matches:
    - Line 19: `2025-12-06T06:21:25.0181391Z Collecting async-timeout<6.0,>=4.0 (from aiohttp)`
    - Line 20: `2025-12-06T06:21:25.0224919Z   Downloading async_timeout-5.0.1-py3-none-any.whl.metadata (5.1 kB)`
    - Line 37: `2025-12-06T06:21:25.9411520Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 15. Run Biometric Voice E2E / Mock Biometric Tests - edge-case-database-failure

**Status**: ❌ failure
**Category**: Test Failure
**Severity**: HIGH
**Started**: 2025-12-06T06:21:17Z
**Completed**: 2025-12-06T06:22:01Z
**Duration**: 44 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984493847/job/57316456899)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 64: `2025-12-06T06:21:59.6189031Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 65: `2025-12-06T06:21:59.6199632Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 66: `2025-12-06T06:21:59.6737858Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 2
  - Sample matches:
    - Line 69: `2025-12-06T06:21:59.6852875Z   name: test-results-biometric-mock-edge-case-database-failure`
    - Line 97: `2025-12-06T06:22:00.0529597Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 3
  - Sample matches:
    - Line 72: `2025-12-06T06:21:59.6853978Z   if-no-files-found: warn`
    - Line 87: `2025-12-06T06:21:59.9029406Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 97: `2025-12-06T06:22:00.0529597Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `AssertionError|Exception`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2025-12-06T06:21:50.6695453Z   Using cached exceptiongroup-1.3.1-py3-none-any.whl.metadata (6.7 kB)`
    - Line 52: `2025-12-06T06:21:51.9169265Z Using cached exceptiongroup-1.3.1-py3-none-any.whl (16 kB)`
    - Line 61: `2025-12-06T06:21:52.0842411Z Installing collected packages: typing-extensions, tomli, pygments, prop`

- Pattern: `timeout|timed out`
  - Occurrences: 6
  - Sample matches:
    - Line 19: `2025-12-06T06:21:50.8778544Z Collecting async-timeout<6.0,>=4.0 (from aiohttp)`
    - Line 20: `2025-12-06T06:21:50.8823858Z   Downloading async_timeout-5.0.1-py3-none-any.whl.metadata (5.1 kB)`
    - Line 37: `2025-12-06T06:21:51.6987241Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 16. Run Biometric Voice E2E / Mock Biometric Tests - performance-baseline

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2025-12-06T06:21:18Z
**Completed**: 2025-12-06T06:21:59Z
**Duration**: 41 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984493847/job/57316456903)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 64: `2025-12-06T06:21:57.9153240Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 65: `2025-12-06T06:21:57.9162599Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 66: `2025-12-06T06:21:57.9562240Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:21:58.3284379Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 3
  - Sample matches:
    - Line 72: `2025-12-06T06:21:57.9662296Z   if-no-files-found: warn`
    - Line 87: `2025-12-06T06:21:58.1782432Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 97: `2025-12-06T06:21:58.3284379Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `AssertionError|Exception`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2025-12-06T06:21:48.7461238Z   Using cached exceptiongroup-1.3.1-py3-none-any.whl.metadata (6.7 kB)`
    - Line 52: `2025-12-06T06:21:50.5466001Z Using cached exceptiongroup-1.3.1-py3-none-any.whl (16 kB)`
    - Line 61: `2025-12-06T06:21:50.7047575Z Installing collected packages: typing-extensions, tomli, pygments, prop`

- Pattern: `timeout|timed out`
  - Occurrences: 6
  - Sample matches:
    - Line 19: `2025-12-06T06:21:48.9803573Z Collecting async-timeout<6.0,>=4.0 (from aiohttp)`
    - Line 20: `2025-12-06T06:21:48.9880769Z   Downloading async_timeout-5.0.1-py3-none-any.whl.metadata (5.1 kB)`
    - Line 37: `2025-12-06T06:21:49.8814914Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 17. Run Biometric Voice E2E / Mock Biometric Tests - anti-spoofing

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2025-12-06T06:21:10Z
**Completed**: 2025-12-06T06:22:08Z
**Duration**: 58 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984493847/job/57316456907)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 64: `2025-12-06T06:22:06.4051907Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 65: `2025-12-06T06:22:06.4061679Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 66: `2025-12-06T06:22:06.4422246Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:22:06.8151856Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 3
  - Sample matches:
    - Line 72: `2025-12-06T06:22:06.4525104Z   if-no-files-found: warn`
    - Line 87: `2025-12-06T06:22:06.6672399Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 97: `2025-12-06T06:22:06.8151856Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `AssertionError|Exception`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2025-12-06T06:21:57.3227181Z   Using cached exceptiongroup-1.3.1-py3-none-any.whl.metadata (6.7 kB)`
    - Line 52: `2025-12-06T06:21:58.8168180Z Using cached exceptiongroup-1.3.1-py3-none-any.whl (16 kB)`
    - Line 61: `2025-12-06T06:21:58.9769623Z Installing collected packages: typing-extensions, tomli, pygments, prop`

- Pattern: `timeout|timed out`
  - Occurrences: 6
  - Sample matches:
    - Line 19: `2025-12-06T06:21:57.5136889Z Collecting async-timeout<6.0,>=4.0 (from aiohttp)`
    - Line 20: `2025-12-06T06:21:57.5174118Z   Downloading async_timeout-5.0.1-py3-none-any.whl.metadata (5.1 kB)`
    - Line 37: `2025-12-06T06:21:58.3897473Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 18. Run Biometric Voice E2E / Mock Biometric Tests - security-validation

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2025-12-06T06:21:39Z
**Completed**: 2025-12-06T06:22:23Z
**Duration**: 44 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984493847/job/57316456912)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 64: `2025-12-06T06:22:21.8613177Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 65: `2025-12-06T06:22:21.8624716Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 66: `2025-12-06T06:22:21.9113240Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:22:22.3134526Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 3
  - Sample matches:
    - Line 72: `2025-12-06T06:22:21.9219256Z   if-no-files-found: warn`
    - Line 87: `2025-12-06T06:22:22.1546039Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 97: `2025-12-06T06:22:22.3134526Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `AssertionError|Exception`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2025-12-06T06:22:12.1368856Z   Using cached exceptiongroup-1.3.1-py3-none-any.whl.metadata (6.7 kB)`
    - Line 52: `2025-12-06T06:22:13.5608608Z Using cached exceptiongroup-1.3.1-py3-none-any.whl (16 kB)`
    - Line 61: `2025-12-06T06:22:13.7226061Z Installing collected packages: typing-extensions, tomli, pygments, prop`

- Pattern: `timeout|timed out`
  - Occurrences: 6
  - Sample matches:
    - Line 19: `2025-12-06T06:22:12.3502754Z Collecting async-timeout<6.0,>=4.0 (from aiohttp)`
    - Line 20: `2025-12-06T06:22:12.3543667Z   Downloading async_timeout-5.0.1-py3-none-any.whl.metadata (5.1 kB)`
    - Line 37: `2025-12-06T06:22:13.2510726Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 19. Run Unlock Integration E2E / Generate Test Summary

**Status**: ❌ failure
**Category**: Test Failure
**Severity**: HIGH
**Started**: 2025-12-06T06:21:04Z
**Completed**: 2025-12-06T06:21:10Z
**Duration**: 6 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984493847/job/57316526322)

#### Failed Steps

- **Step 4**: Check Test Status

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:21:08.2426757Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 13
  - Sample matches:
    - Line 39: `2025-12-06T06:21:08.0643226Z [36;1mTOTAL_FAILED=0[0m`
    - Line 44: `2025-12-06T06:21:08.0645903Z [36;1m    FAILED=$(jq -r '.summary.failed' "$report")[0m`
    - Line 46: `2025-12-06T06:21:08.0647214Z [36;1m    TOTAL_FAILED=$((TOTAL_FAILED + FAILED))[0m`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 2
  - Sample matches:
    - Line 4: `2025-12-06T06:21:07.9253587Z (node:1991) [DEP0005] DeprecationWarning: Buffer() is deprecated due to`
    - Line 5: `2025-12-06T06:21:07.9256147Z (Use `node --trace-deprecation ...` to show where the warning was creat`

#### Suggested Fixes

1. Review test cases and ensure code changes haven't broken existing functionality

---

### 20. Run Biometric Voice E2E / Generate Biometric Test Summary

**Status**: ❌ failure
**Category**: Test Failure
**Severity**: HIGH
**Started**: 2025-12-06T06:22:47Z
**Completed**: 2025-12-06T06:22:53Z
**Duration**: 6 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984493847/job/57316576387)

#### Failed Steps

- **Step 4**: Check Test Status

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:22:50.5801824Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 11
  - Sample matches:
    - Line 32: `2025-12-06T06:22:50.4114286Z [36;1mTOTAL_FAILED=0[0m`
    - Line 37: `2025-12-06T06:22:50.4117097Z [36;1m    FAILED=$(jq -r '.summary.failed' "$report")[0m`
    - Line 39: `2025-12-06T06:22:50.4118232Z [36;1m    TOTAL_FAILED=$((TOTAL_FAILED + FAILED))[0m`

#### Suggested Fixes

1. Review test cases and ensure code changes haven't broken existing functionality

---

### 21. Generate Combined Test Summary

**Status**: ❌ failure
**Category**: Test Failure
**Severity**: HIGH
**Started**: 2025-12-06T06:22:57Z
**Completed**: 2025-12-06T06:22:59Z
**Duration**: 2 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984493847/job/57316580607)

#### Failed Steps

- **Step 2**: Generate Combined Summary

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 1
  - Sample matches:
    - Line 83: `2025-12-06T06:22:59.0051318Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 9
  - Sample matches:
    - Line 47: `2025-12-06T06:22:58.9857705Z [36;1mif [ "failure" = "success" ]; then[0m`
    - Line 50: `2025-12-06T06:22:58.9859637Z [36;1m  echo "- ❌ **Unlock Integration E2E:** failure" >> $GITHUB_STEP`
    - Line 54: `2025-12-06T06:22:58.9861585Z [36;1mif [ "failure" = "success" ]; then[0m`

#### Suggested Fixes

1. Review test cases and ensure code changes haven't broken existing functionality

---

### 22. Notify Test Status

**Status**: ❌ failure
**Category**: Test Failure
**Severity**: HIGH
**Started**: 2025-12-06T06:23:02Z
**Completed**: 2025-12-06T06:23:06Z
**Duration**: 4 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984493847/job/57316582752)

#### Failed Steps

- **Step 3**: Failure Notification

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 1
  - Sample matches:
    - Line -1: `2025-12-06T06:23:04.2088831Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 3
  - Sample matches:
    - Line -9: `2025-12-06T06:23:04.0932853Z ##[group]Run echo "❌ Unlock tests failed - 'unlock my screen' may be br`
    - Line -8: `2025-12-06T06:23:04.0934003Z [36;1mecho "❌ Unlock tests failed - 'unlock my screen' may be broken!"`
    - Line -3: `2025-12-06T06:23:04.2061116Z ❌ Unlock tests failed - 'unlock my screen' may be broken!`

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

📊 *Report generated on 2025-12-06T06:24:09.932622*
🤖 *JARVIS CI/CD Auto-PR Manager*
