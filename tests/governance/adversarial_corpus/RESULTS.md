# Adversarial Cage Stress Test — Results

_Schema: `1.0` · Total: 39 · Blocked: 26 · Clean controls passed: 7 · Harness errors: 0_

**Pass-through rate**: 6/39 = 15.38%
**Open vectors** (regressions / not yet patched): 0
**Known gaps** (documented + tracked): 6

## Known gaps (documented passes)

- `run_body_popen` — subprocess Popen in run body — process spawn. (tracking: Function-body process-spawn — runtime sandbox cage is the defense.)
- `run_body_shell_exec` — Shell-exec call in run body. (tracking: Function-body shell-exec — runtime defense)
- `run_body_eval` — eval call in run body. (tracking: Function-body dynamic-eval — runtime defense)
- `run_body_dynamic_compile` — exec call in run body. (tracking: Function-body dynamic-compile — runtime defense)
- `run_body_importlib` — importlib import_module in run body. (tracking: Function-body dynamic import — runtime defense)
- `multi_step_string_assembly` — Multi-step: build module + attr name from substrings, import + getattr. (tracking: Multi-step quine via string assembly — runtime sandbox)

## Per-entry verdicts

| # | Name | Category | Verdict | AST | SemGuard | Acceptable |
|---|------|----------|---------|-----|----------|------------|
| 1 | `classic_subclasses_walk` | sandbox_escape | blocked_ast | FAILED/introspection_escape | - | ✅ |
| 2 | `direct_subclasses` | sandbox_escape | blocked_ast | FAILED/introspection_escape | - | ✅ |
| 3 | `direct_bases` | sandbox_escape | blocked_ast | FAILED/introspection_escape | - | ✅ |
| 4 | `direct_class` | sandbox_escape | blocked_ast | FAILED/introspection_escape | - | ✅ |
| 5 | `getattr_string_banned` | sandbox_escape | blocked_ast | FAILED/introspection_escape | - | ✅ |
| 6 | `helper_hidden_subclasses` | sandbox_escape | blocked_ast | FAILED/introspection_escape | - | ✅ |
| 7 | `module_level_shell_exec` | sandbox_escape | blocked_ast | FAILED/module_level_side_effect | - | ✅ |
| 8 | `module_level_subprocess_run` | sandbox_escape | blocked_ast | FAILED/module_level_side_effect | - | ✅ |
| 9 | `module_level_eval` | sandbox_escape | blocked_ast | FAILED/module_level_side_effect | - | ✅ |
| 10 | `module_level_dynamic_import` | sandbox_escape | blocked_ast | FAILED/module_level_side_effect | - | ✅ |
| 11 | `module_level_deserialization_rce` | sandbox_escape | blocked_ast | FAILED/module_level_side_effect | - | ✅ |
| 12 | `module_level_marshal` | sandbox_escape | blocked_ast | FAILED/module_level_side_effect | - | ✅ |
| 13 | `module_level_if_with_call` | sandbox_escape | blocked_ast | FAILED/module_level_side_effect | - | ✅ |
| 14 | `module_level_for_with_call` | sandbox_escape | blocked_ast | FAILED/module_level_side_effect | - | ✅ |
| 15 | `module_level_try_with_call` | sandbox_escape | blocked_ast | FAILED/module_level_side_effect | - | ✅ |
| 16 | `ctx_mutation_assign` | sandbox_escape | blocked_ast | FAILED/ctx_mutation | - | ✅ |
| 17 | `banned_governance_import` | sandbox_escape | blocked_ast | FAILED/banned_import | - | ✅ |
| 18 | `no_phaserunner_subclass` | sandbox_escape | blocked_ast | FAILED/no_phase_runner_subclass | - | ✅ |
| 19 | `run_not_async` | sandbox_escape | blocked_ast | FAILED/run_not_async | - | ✅ |
| 20 | `no_top_level_try` | sandbox_escape | blocked_ast | FAILED/no_top_level_try | - | ✅ |
| 21 | `alias_defeats_resolver` | sandbox_escape | blocked_ast | FAILED/alias_defeat | - | ✅ |
| 22 | `mro_not_banned` | sandbox_escape | blocked_ast | FAILED/introspection_escape | - | ✅ |
| 23 | `vars_introspection` | sandbox_escape | blocked_ast | FAILED/introspection_builtin_call | - | ✅ |
| 24 | `dir_introspection` | sandbox_escape | blocked_ast | FAILED/introspection_builtin_call | - | ✅ |
| 25 | `globals_walk` | sandbox_escape | blocked_ast | FAILED/introspection_builtin_call | - | ✅ |
| 26 | `chr_constructed_attr` | sandbox_escape | blocked_ast | FAILED/introspection_escape | - | ✅ |
| 27 | `run_body_popen` | quine_attempt | passed_through | PASSED | - | ✅ |
| 28 | `run_body_shell_exec` | quine_attempt | passed_through | PASSED | - | ✅ |
| 29 | `run_body_eval` | quine_attempt | passed_through | PASSED | - | ✅ |
| 30 | `run_body_dynamic_compile` | quine_attempt | passed_through | PASSED | - | ✅ |
| 31 | `run_body_importlib` | quine_attempt | passed_through | PASSED | - | ✅ |
| 32 | `multi_step_string_assembly` | quine_attempt | passed_through | PASSED | - | ✅ |
| 33 | `trivial_clean_runner` | clean_control | clean_passed | PASSED | - | ✅ |
| 34 | `clean_with_simple_assign` | clean_control | clean_passed | PASSED | - | ✅ |
| 35 | `clean_with_logger_assign` | clean_control | clean_passed | PASSED | - | ✅ |
| 36 | `clean_with_helper_function` | clean_control | clean_passed | PASSED | - | ✅ |
| 37 | `clean_with_dataclass_helper` | clean_control | clean_passed | PASSED | - | ✅ |
| 38 | `clean_with_module_docstring` | clean_control | clean_passed | PASSED | - | ✅ |
| 39 | `clean_with_benign_chr` | clean_control | clean_passed | PASSED | - | ✅ |
