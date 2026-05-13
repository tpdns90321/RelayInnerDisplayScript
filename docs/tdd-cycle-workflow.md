# TDD Cycle Workflow

This repository has a confirmed TDD-cycle policy in `.pi/tdd-cycle/config.json`. The policy is intentionally narrow: red-phase test writes are limited to the tracked suite under `tests/**`, green and refactor phases may not edit tests by default, coverage is disabled, and no network domains are allowed unless a later spec changes the policy.

The TDD-cycle runtime folders `.tdd-cycle/`, `red/`, `green/`, and `refactor/` are disposable local state and are ignored by git.

## Official test commands

From the repository root, run the managed test suite with the standard-library unittest discovery command:

```sh
python -m unittest discover -s tests -p 'test_*.py'
```

From a TDD-cycle phase workspace such as `red/`, `green/`, or `refactor/`, the repository is available through a `repo` symlink. Add that symlink to `PYTHONPATH` and discover tests under `repo/tests`:

```sh
PYTHONPATH=repo python -m unittest discover -s repo/tests -p 'test_*.py'
```

The `PYTHONPATH=repo` prefix is required in phase workspaces so tests can import `relayinner_display` from the symlinked repository checkout.

Targeted phase-workspace examples:

```sh
PYTHONPATH=repo python -m unittest repo.tests.test_config
PYTHONPATH=repo python -m unittest repo.tests.test_daemon.DisplayDaemonTests
```

## Manual diagnostic boundary

`test_find_powerbutton_event.py` at the repository root is a manual operator diagnostic script for finding the host power-button event device. It is not part of the managed TDD test suite, is not matched by the default unittest discovery command, and is not included in the red-phase `testWriteGlobs` policy.

## Policy checks

Validate the checked-in policy from the repository root with strict confirmation enabled:

```sh
/path/to/custom-pi-style-skills/tdd-cycle-shared/bin/tdd-cycle.js validate-config --repo-root .
```

Coverage remains disabled until a later opt-in spec enables it. Do not introduce `pytest`, `tox`, `nox`, a test wrapper, coverage hooks, or pre-commit hooks as part of this baseline workflow.
