# Spec 73. Optional Coverage Non-Regression Gate

## Context / Problem

The baseline TDD-cycle environment deliberately avoids coverage enforcement. The current repository has no `coverage.py` dependency, no coverage output contract, no pre-commit framework config, and no existing coverage baseline. Enabling coverage too early would mix policy bootstrap with dependency and hook decisions.

This spec defines coverage non-regression as an explicit opt-in follow-up. It should only be implemented after the repository has a confirmed TDD-cycle policy and after maintainers choose the coverage tool, command, summary file, metrics, and hook behavior.

## Goals / Non-goals

Goals:

- Define the conditions required before enabling TDD-cycle coverage enforcement.
- Add a deliberate coverage config contract to `.pi/tdd-cycle/config.json` only when maintainers opt in.
- Install the managed coverage check wrapper and hook integration through the TDD-cycle coverage setup helper.
- Preserve existing hook behavior when adding the managed coverage block.
- Verify that coverage checks skip when no active cycle exists and enforce metrics only when an active coverage-enabled cycle has a baseline.

Non-goals:

- Making coverage mandatory for the baseline TDD-cycle environment.
- Introducing CI.
- Replacing or rewriting unrelated pre-commit hooks.
- Creating a coverage baseline during hook setup.
- Changing product runtime behavior.
- Using coverage metrics as a substitute for behavior-focused red tests.

## User stories

- As a maintainer, I want coverage enforcement to be opt-in so the repository can adopt TDD-cycle safely before adding dependencies.
- As a reviewer, I want the coverage command and metrics to be explicit before a hook can block commits.
- As an implementer, I want commits during an active cycle to fail only when configured coverage metrics regress below the cycle-start baseline.
- As a contributor outside an active TDD cycle, I want the coverage hook to skip rather than block unrelated work.

## Public API / Interfaces

Repository policy file:

```text
.pi/tdd-cycle/config.json
```

Coverage config fields that must be deliberately reviewed before enabling:

```json
{
  "coverage": {
    "enabled": true,
    "command": "<coverage command>",
    "summaryFile": "<coverage summary json path>",
    "metrics": ["<metric path>"],
    "outputGlobs": ["<coverage output glob>"]
  }
}
```

Coverage setup helper:

```sh
/path/to/custom-pi-style-skills/tdd-cycle-shared/bin/tdd-cycle.js coverage setup --repo-root <repo-root>
```

Installed wrapper path:

```text
.pi/tdd-cycle/check-coverage.sh
```

Possible hook targets, selected by the helper when safe:

```text
.pre-commit-config.yaml
.pre-commit-config.yml
.husky/pre-commit
.git/hooks/pre-commit
```

The wrapper delegates enforcement to:

```sh
/path/to/custom-pi-style-skills/tdd-cycle-shared/bin/tdd-cycle.js coverage check --repo-root <repo-root>
```

## Data model / Persistence

Persistent repository files when coverage is enabled:

- `.pi/tdd-cycle/config.json`
- `.pi/tdd-cycle/check-coverage.sh`
- the selected hook target

Cycle-start baseline file created by the TDD-cycle start workflow, not by hook setup:

```text
.tdd-cycle/cycles/<cycle-id>/coverage-baseline.json
```

Coverage command outputs are limited to configured `coverage.outputGlobs`. The exact output paths depend on the selected coverage tool and remain open until implementation chooses the tool.

## Security model (Permission/Isolation/Audit)

Permissions:

- Hook setup writes only the managed wrapper and one selected hook target.
- Hook setup must not run the coverage command.
- Coverage baseline creation belongs to cycle start and executes the configured project command only after maintainers opt in.

Isolation:

- Managed hook content must be bounded by marker comments or the helper's supported idempotent integration mechanism.
- Existing hook content must be preserved.
- If hook detection is ambiguous or unsafe, setup must stop for manual selection instead of falling back silently.

Audit:

- The implementation review must report the selected hook mechanism, changed files, whether the managed marker was duplicated, and the no-active-cycle wrapper result.
- Coverage config changes must show the selected command, summary file, metrics, and output globs.

## Acceptance criteria (Testable, Verifiable)

- With `coverage.enabled = false`, coverage setup skips and does not create a wrapper or modify hooks.
- With `coverage.enabled = true`, the config includes a non-empty command, summary file, metrics, and output globs.
- Coverage setup installs `.pi/tdd-cycle/check-coverage.sh` as executable.
- Coverage setup modifies only one safe hook target.
- Re-running coverage setup does not duplicate the managed hook block.
- With no active TDD cycle, `.pi/tdd-cycle/check-coverage.sh` exits successfully with a no-active-cycle skip result.
- During an active coverage-enabled cycle, missing baseline fails clearly.
- During an active coverage-enabled cycle with a baseline, configured metrics must meet or exceed baseline values.

## Test plan

Disabled-path verification:

```sh
/path/to/custom-pi-style-skills/tdd-cycle-shared/bin/tdd-cycle.js coverage setup --repo-root <repo-root>
```

Expected disabled result:

- status is skipped
- reason is coverage disabled
- no hook file changes are made

Enabled-path verification after maintainers choose the coverage tool:

```sh
/path/to/custom-pi-style-skills/tdd-cycle-shared/bin/tdd-cycle.js validate-config --repo-root <repo-root>
/path/to/custom-pi-style-skills/tdd-cycle-shared/bin/tdd-cycle.js coverage setup --repo-root <repo-root>
/path/to/custom-pi-style-skills/tdd-cycle-shared/bin/tdd-cycle.js coverage setup --repo-root <repo-root>
```

Wrapper smoke check with no active cycle:

```sh
.pi/tdd-cycle/check-coverage.sh
```

Active-cycle checks:

- start a coverage-enabled cycle and confirm `coverage-baseline.json` exists
- run the wrapper after a passing cycle state and confirm metrics pass
- simulate or create a lower metric and confirm the wrapper fails with comparison details

## Rollout / Backward compatibility

Coverage remains disabled by default after Specs 70 and 71. This spec should be implemented only when the repository is ready to add the coverage dependency and choose a stable summary format.

The hook setup must preserve existing hook behavior and must be idempotent. If the repository later adopts a different hook framework, coverage setup should be re-run only after the new hook target is unambiguous.

## Open questions

- Which coverage tool and package management path should the repository adopt.
- Which summary file format should be used for metric extraction.
- Which metric or metrics should be enforced initially.
- Whether coverage enforcement should remain local-only or later be mirrored in CI.

## Spec Dependencies

- Spec 71. Repository TDD Policy and Test Command
- TDD-cycle coverage setup helper availability
- A maintainer decision to opt in to coverage enforcement
- A selected Python coverage tool and stable summary output format
