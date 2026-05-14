# TDD Cycle Workflow

This repository has a confirmed TDD-cycle policy in `.pi/tdd-cycle/config.json`. The policy is intentionally narrow: red-phase test writes are limited to unittest files matching `tests/test_*.py`, green and refactor phases may not edit tests by default, coverage enforcement is an explicit opt-in gate for managed cycles, and no network domains are allowed unless a later spec changes the policy.

The TDD-cycle runtime folders `.tdd-cycle/`, `red/`, `green/`, and `refactor/` are disposable local state and are ignored by git. Relay-managed coverage output is written under `coverage/`, which is also ignored by git. Phase preflight may also refresh the managed repo-view marker `.tdd-cycle-repo-view.json` and its temporary atomic-write file inside the phase `repo/` view; those marker writes are intentionally allowed as TDD-cycle tooling output so sandboxed phase preflight can complete.

## First managed pilot status

Spec 72 ran the first managed TDD-cycle pilot from the required `tasks/spec-*` worktree layout. Cycle start succeeded with the confirmed config, created `.tdd-cycle/active.json`, generated `red/`, `green/`, and `refactor/` phase workspaces, and wrote verified phase sandbox files.

The tmux-backed orchestration then blocked in the red phase before repository test changes were made. The red preflight attempted to write `repo/.tdd-cycle-repo-view.json.tmp-*`, but the generated red sandbox only allows test writes under `repo/tests/**`, configured test-output paths, coverage output paths, and the red result file. This is a TDD-cycle tooling/policy gap, not a product-runtime failure.

Until a later TDD-cycle follow-up resolves that preflight temp-file policy, expect managed cycles in this repository to start successfully but potentially block before a red result JSON is written. Keep using the commands below for ordinary repository and phase-workspace verification.

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

## Optional coverage non-regression gate

Spec 73 enables the managed coverage gate for active TDD cycles. The selected tool is Python `coverage.py`, exposed as the optional project dependency group `tdd`. The policy calls the standalone `coverage` executable, so ensure that executable is on `PATH` before running managed coverage checks:

```sh
python -m pip install '.[tdd]'
coverage --version
```

The configured coverage command is:

```sh
mkdir -p coverage && coverage run --data-file=coverage/.coverage --source=relayinner_display -m unittest discover -s tests -p 'test_*.py' && coverage json --data-file=coverage/.coverage -o coverage/coverage-summary.json
```

The configured summary file is `coverage/coverage-summary.json`, the enforced metric is `totals.percent_covered`, and coverage outputs are limited to `coverage/**`.

Coverage setup installed `.pi/tdd-cycle/check-coverage.sh` and integrated it through `.pre-commit-config.yaml` using one managed `tdd-cycle-coverage` local hook block. Install the pre-commit framework from the same optional dependency group and run `pre-commit install` if this checkout should enforce the hook locally. Re-run setup after helper-path changes or local checkout moves:

```sh
/path/to/custom-pi-style-skills/tdd-cycle-shared/bin/tdd-cycle.js coverage setup --repo-root .
```

The wrapper exits successfully with `reason: "no-active-cycle"` when no managed cycle is active. During an active coverage-enabled cycle, commits require the cycle-start `coverage-baseline.json`; then the configured coverage metric must stay at or above that baseline.
