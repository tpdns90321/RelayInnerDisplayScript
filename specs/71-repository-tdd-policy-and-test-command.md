# Spec 71. Repository TDD Policy and Test Command

## Context / Problem

Spec 70 creates an unconfirmed TDD-cycle policy scaffold. That scaffold is intentionally not safe for cycle start until the repository-specific write permissions, test commands, coverage behavior, and documentation are reviewed.

This repository currently uses the Python standard library `unittest` runner. The command that works from the repository root is not identical to the command needed from TDD-cycle phase workspaces, because phase workspaces access the repository through a `repo` symlink. The policy and docs must make that distinction explicit before any active cycle starts.

## Goals / Non-goals

Goals:

- Review `.pi/tdd-cycle/config.json` and set `confirmed` to `true` only after the repository policy is correct.
- Restrict red-phase test writes to the tracked test suite under `tests/`.
- Document the official repository-root test command.
- Document the official phase-workspace test command with the required `PYTHONPATH=repo` prefix.
- Clarify that `test_find_powerbutton_event.py` is a manual diagnostic script, not part of the managed TDD test suite.
- Add concise TDD-cycle status documentation without changing the project test runner.

Non-goals:

- Introducing `pytest`, `tox`, `nox`, `make test`, or a new test wrapper.
- Enabling coverage or installing pre-commit hooks.
- Starting an active TDD cycle.
- Changing product runtime behavior.
- Renaming the manual power-button diagnostic script.

## User stories

- As a TDD-cycle phase agent, I want a confirmed policy that tells me which tests I may edit.
- As a maintainer, I want one documented test command for repository-root checks and one documented command for phase-workspace checks.
- As a reviewer, I want `test_find_powerbutton_event.py` excluded from normal TDD writes so manual operator diagnostics are not mistaken for suite tests.
- As an implementer, I want cycle start to fail until the repository policy has been deliberately confirmed.

## Public API / Interfaces

Repository policy file:

```text
.pi/tdd-cycle/config.json
```

Required policy decisions:

```json
{
  "confirmed": true,
  "testWriteGlobs": ["tests/**"],
  "greenAllowedTestArtifactGlobs": [],
  "refactorAllowedTestFiles": [],
  "coverage": {
    "enabled": false
  },
  "testOutputGlobs": ["tmp/test-output/**"],
  "network": {
    "allowedDomains": [],
    "deniedDomains": []
  }
}
```

The generated secret and key deny-read defaults should remain in place unless a later security review changes them.

Repository-root verification command:

```sh
python -m unittest discover -s tests -p 'test_*.py'
```

Phase-workspace verification command from `red/`, `green/`, or `refactor/`:

```sh
PYTHONPATH=repo python -m unittest discover -s repo/tests -p 'test_*.py'
```

Targeted phase-workspace command examples:

```sh
PYTHONPATH=repo python -m unittest repo.tests.test_config
PYTHONPATH=repo python -m unittest repo.tests.test_daemon.DisplayDaemonTests
```

Documentation targets for the implementation of this spec:

- root `README.md`, with a short current-state note
- `docs/tdd-cycle-workflow.md`, with detailed TDD-cycle usage and command guidance

## Data model / Persistence

Persistent repository files:

- `.pi/tdd-cycle/config.json`
- `README.md`
- `docs/tdd-cycle-workflow.md`

Policy lifecycle:

- Spec 70 creates the scaffold with `confirmed: false`.
- This spec reviews the policy and changes it to `confirmed: true`.
- Future worktrees inherit the confirmed policy through normal git checkout/worktree behavior.

Manual diagnostic boundary:

- `test_find_powerbutton_event.py` stays at repository root.
- It is documented as an operator diagnostic script.
- It is not included in `testWriteGlobs`.
- It is not included in the default unittest discovery command.

## Security model (Permission/Isolation/Audit)

Permissions:

- Red phase may write only under `tests/**`.
- Green phase may not edit test files unless a later explicit artifact policy allows it.
- Refactor phase may not edit test files under this baseline policy.

Isolation:

- TDD-cycle phase agents must operate through `repo/...` paths from phase workspaces.
- Network allow and deny lists remain empty unless a later spec introduces an explicit external dependency.
- Coverage remains disabled until Spec 73 or a later opt-in decision enables it.

Audit:

- The config diff must show the deliberate transition from `confirmed: false` to `confirmed: true`.
- Documentation must record the tested commands and the reason for the phase-workspace `PYTHONPATH=repo` prefix.

## Acceptance criteria (Testable, Verifiable)

- `.pi/tdd-cycle/config.json` validates strictly without the unconfirmed override.
- `.pi/tdd-cycle/config.json` has `confirmed` set to `true`.
- `testWriteGlobs` is exactly limited to `tests/**` unless a reviewer explicitly approves more.
- `coverage.enabled` remains `false`.
- The repository-root unittest command passes.
- The phase-workspace unittest command passes from a workspace containing `repo -> ..`.
- Documentation includes both test commands.
- Documentation states that `test_find_powerbutton_event.py` is a manual diagnostic script outside the managed TDD test suite.

## Test plan

Strict config validation:

```sh
/path/to/custom-pi-style-skills/tdd-cycle-shared/bin/tdd-cycle.js validate-config --repo-root <repo-root>
```

Repository-root test run:

```sh
python -m unittest discover -s tests -p 'test_*.py'
```

Phase-workspace simulation:

```sh
tmpdir=$(mktemp -d)
ln -s "$(pwd)" "$tmpdir/repo"
(
  cd "$tmpdir"
  PYTHONPATH=repo python -m unittest discover -s repo/tests -p 'test_*.py'
)
rm -rf "$tmpdir"
```

Documentation checks:

```sh
grep -n "PYTHONPATH=repo" docs/tdd-cycle-workflow.md
grep -n "test_find_powerbutton_event.py" docs/tdd-cycle-workflow.md
```

## Rollout / Backward compatibility

This spec preserves the current stdlib `unittest` workflow and does not add test dependencies. Existing developers can continue running tests from the repository root. TDD-cycle users gain the additional phase-workspace command required by the `repo` symlink layout.

The policy confirmation should be reviewed in the same change that introduces the documentation, so future worktrees do not inherit an unexplained `confirmed: true` transition.

## Open questions

- Whether a later developer-experience spec should add a test command wrapper after the TDD-cycle workflow has been used in practice.
- Whether the root diagnostic script should eventually be moved or renamed to reduce confusion with test discovery conventions.

## Spec Dependencies

- Spec 70. TDD Cycle Bootstrap Policy Scaffold
- Existing Python `unittest` suite under `tests/`
