# Spec 72. First Spec Worktree TDD Pilot

## Context / Problem

Specs 70 and 71 prepare and confirm repository policy, but they do not prove that an active managed TDD cycle works in this project. The project also has a repository-specific implementation workflow: approved specs are implemented on a branch and checked out through a git worktree under `tasks/spec-*`.

A first pilot cycle is needed to verify that the confirmed policy, phase workspaces, sandbox files, tmux orchestration, result JSON files, and project test commands all work together in the intended worktree location before the workflow is relied on for larger implementation work.

## Goals / Non-goals

Goals:

- Run the first active TDD cycle from a `tasks/spec-*` implementation worktree.
- Use one small observable behavior from the next approved implementation spec as the pilot scope.
- Verify `tdd-cycle-start` creates the active cycle state, phase workspaces, and phase sandbox files.
- Verify tmux-backed orchestration completes one red to green-or-no-op to refactor sequence.
- Record schema-valid phase result JSON files.
- Confirm the final project tests pass after the pilot behavior.

Non-goals:

- Running TDD-cycle from the main checkout.
- Running multiple automatic cycles.
- Implementing a broad horizontal feature slice.
- Accepting a dirty worktree without explicit user approval.
- Installing coverage hooks.
- Creating commits automatically from the TDD-cycle skills.

## User stories

- As an implementer, I want proof that TDD-cycle works inside the repository's required spec worktree layout.
- As a maintainer, I want the first pilot to be scoped to one observable behavior so failures are easy to attribute.
- As a reviewer, I want red, green, and refactor phase results recorded as JSON rather than inferred from tmux pane text.
- As a future phase agent, I want the pilot to confirm the `repo` symlink and sandbox policies work with the project test command.

## Public API / Interfaces

Worktree convention:

```text
tasks/spec-<id-or-slug>/
```

Cycle preparation skill/helper:

```sh
/path/to/custom-pi-style-skills/tdd-cycle-shared/bin/tdd-cycle.js start --repo-root <worktree-root>
```

Tmux orchestration skill/helper:

```sh
/path/to/custom-pi-style-skills/tdd-cycle/bin/orchestrate.js --repo-root <worktree-root>
```

Generated active-cycle files:

```text
.tdd-cycle/active.json
.tdd-cycle/cycles/<cycle-id>/state.json
red/repo -> ..
green/repo -> ..
refactor/repo -> ..
red/.pi/sandbox.json
green/.pi/sandbox.json
refactor/.pi/sandbox.json
```

Generated phase result files:

```text
.tdd-cycle/cycles/<cycle-id>/red-result.json
.tdd-cycle/cycles/<cycle-id>/green-result.json
.tdd-cycle/cycles/<cycle-id>/refactor-result.json
```

Phase-workspace test command:

```sh
PYTHONPATH=repo python -m unittest discover -s repo/tests -p 'test_*.py'
```

## Data model / Persistence

Runtime state is worktree-local and disposable:

- `.tdd-cycle/`
- `red/`
- `green/`
- `refactor/`

The worktree is created from an implementation branch for the selected spec. The runtime state must stay ignored by git through the rules from Spec 70.

The authoritative cycle completion signal is the set of schema-valid result JSON files under `.tdd-cycle/cycles/<cycle-id>/`, not tmux pane output.

## Security model (Permission/Isolation/Audit)

Permissions:

- Cycle start must use the confirmed `.pi/tdd-cycle/config.json` from Spec 71.
- Red phase may write only approved test globs.
- Green and refactor phases must follow their generated sandbox policies.

Isolation:

- The pilot runs in a spec worktree, not the main checkout.
- Phase agents must read and write repository files through `repo/...` paths from their phase workspace.
- Dirty worktree acceptance must be explicit; the workflow must not silently baseline unrelated local changes.

Audit:

- Cycle id, worktree path, phase result paths, tmux session name, and verification commands must be reported.
- Blocked or timed-out orchestration must preserve bounded pane logs under the cycle logs directory.

## Acceptance criteria (Testable, Verifiable)

- A branch and `tasks/spec-*` worktree exist for the selected pilot spec.
- `git status --short` is checked before cycle start.
- `tdd-cycle-start` succeeds with the confirmed config.
- `.tdd-cycle/active.json` identifies an active cycle.
- `red/`, `green/`, and `refactor/` exist with `repo -> ..` links and `.pi/sandbox.json` files.
- `tdd-cycle` orchestration completes one sequence or reports a valid blocked status with logs.
- For a completed pilot, red, green, and refactor result JSON files are schema-valid.
- The final repository-root test command passes from the worktree.
- The final phase-workspace test command passes from at least one phase workspace.

## Test plan

Preflight:

```sh
git status --short
/path/to/custom-pi-style-skills/tdd-cycle-shared/bin/tdd-cycle.js validate-config --repo-root <worktree-root>
/path/to/custom-pi-style-skills/tdd-cycle-shared/bin/tdd-cycle.js status --repo-root <worktree-root>
```

Cycle start:

```sh
/path/to/custom-pi-style-skills/tdd-cycle-shared/bin/tdd-cycle.js start --repo-root <worktree-root>
```

Orchestration:

```sh
/path/to/custom-pi-style-skills/tdd-cycle/bin/orchestrate.js --repo-root <worktree-root>
```

Result verification:

```sh
test -f .tdd-cycle/cycles/<cycle-id>/red-result.json
test -f .tdd-cycle/cycles/<cycle-id>/green-result.json
test -f .tdd-cycle/cycles/<cycle-id>/refactor-result.json
```

Project verification from worktree root:

```sh
python -m unittest discover -s tests -p 'test_*.py'
```

Project verification from a phase workspace:

```sh
PYTHONPATH=repo python -m unittest discover -s repo/tests -p 'test_*.py'
```

## Rollout / Backward compatibility

This spec validates the workflow in an implementation worktree and should not create active cycle state in the main checkout. If the pilot blocks, the implementation should report the blocked phase, result-file status, and pane log path instead of continuing into broader feature work.

The pilot should be treated as a workflow validation slice. Future implementation specs may use the same workflow after this pilot succeeds, but they should still keep each cycle scoped to one observable behavior.

## Open questions

- Which approved product spec and exact observable behavior should be selected for the first pilot cycle.
- Whether the completed pilot cycle should be archived by a later repository maintenance step.
- Whether successful pilot results should be summarized in `docs/tdd-cycle-workflow.md` after field use.

## Spec Dependencies

- Spec 70. TDD Cycle Bootstrap Policy Scaffold
- Spec 71. Repository TDD Policy and Test Command
- Project implementation workflow requiring branch plus `git worktree` under `tasks/spec-*`
- A future approved implementation spec with one small observable behavior suitable for the pilot
