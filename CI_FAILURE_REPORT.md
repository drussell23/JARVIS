# CI/CD Failure Analysis Report

## Executive Summary

- **Workflow**: PR Automation & Validation
- **Run Number**: #102200
- **Branch**: `fix/ci/pr-automation-validation-run102194-20260529-002634`
- **Commit**: `4a26c3d76a76027c8633e14370e4e6a2c7794f14`
- **Status**: ❌ FAILED
- **Timestamp**: 2026-05-29T00:26:57Z
- **Triggered By**: @cubic-dev-ai[bot]
- **Workflow URL**: [View Run](https://github.com/drussell23/JARVIS/actions/runs/26610443623)

## Failure Overview

Total Failed Jobs: **1**

| # | Job Name | Category | Severity | Duration |
|---|----------|----------|----------|----------|
| 1 | Validate PR Title | unknown | high | 3s |

## Detailed Analysis

### 1. Validate PR Title

**Status**: ❌ failure
**Category**: Unknown
**Severity**: HIGH
**Started**: 2026-05-29T00:27:00Z
**Completed**: 2026-05-29T00:27:03Z
**Duration**: 3 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26610443623/job/78414932466)

#### Failed Steps

- **Step 2**: Validate Conventional Commits

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 2
  - Sample matches:
    - Line -97: `﻿<?xml version="1.0" encoding="utf-8"?><Error><Code>ServerBusy</Code><Message>The server is busy.`
    - Line -95: `Time:2026-05-29T00:29:28.8169008Z</Message></Error>`

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

📊 *Report generated on 2026-05-29T00:29:28.855246*
🤖 *JARVIS CI/CD Auto-PR Manager*
