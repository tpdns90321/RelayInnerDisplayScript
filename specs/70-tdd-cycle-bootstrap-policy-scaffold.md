# Spec 70. TDD Cycle Bootstrap Policy Scaffold

## Context / Problem

The repository has a working Python `unittest` suite, but it has no managed TDD-cycle policy file, no TDD-cycle runtime ignore rules, and no active cycle state. The TDD-cycle skillset requires a repository-level bootstrap step before any worktree can safely start red, green, and refactor phase workspaces.

Without a checked-in bootstrap policy, future spec implementation work has to guess which files red may edit, which runtime paths are disposable, and which safety gates must be reviewed before cycle start. This spec introduces the smallest repository preparation step: create the policy scaffold and ignore only the TDD-cycle runtime paths, while keeping the policy unconfirmed until a human review follows in Spec 71.

## Goals / Non-goals

Goals:

- Add the managed TDD-cycle runtime ignore entries to the repository ignore policy.
- Create `.pi/tdd-cycle/config.json` through the TDD-cycle bootstrap helper.
- Preserve the helper-generated safety gate by keeping `confirmed` set to `false` after bootstrap.
- Verify that bootstrap does not create active cycle state or phase workspaces.
- Keep this step safe to run before any product implementation spec begins.

Non-goals:

- Confirming the TDD-cycle policy for cycle start.
- Starting an active TDD cycle.
- Creating `red/`, `green/`, or `refactor/` workspaces.
- Installing coverage hooks or pre-commit hooks.
- Changing the project test runner.
- Updating product runtime behavior.

## User stories

- As a maintainer, I want the repository to contain the baseline TDD-cycle policy scaffold so later spec work does not invent policy per worktree.
- As an implementer, I want disposable TDD-cycle runtime folders ignored by git before a cycle creates them.
- As a reviewer, I want the generated policy to remain unconfirmed until its write globs, coverage settings, network policy, and timeouts are reviewed.
- As an operator of this repository, I want bootstrap to avoid active cycle side effects so the main checkout stays clean.

## Public API / Interfaces

Bootstrap is performed through the managed TDD-cycle bootstrap skill or its shared helper:

```sh
/path/to/custom-pi-style-skills/tdd-cycle-shared/bin/tdd-cycle.js bootstrap --repo-root <repo-root>
```

Repository files touched by this spec:

```text
.gitignore
.pi/tdd-cycle/config.json
```

Required `.gitignore` runtime entries:

```gitignore
.tdd-cycle/
red/
green/
refactor/
```

The bootstrap result must be treated as a scaffold only. The generated config is expected to include `confirmed: false`, and cycle start must remain blocked until Spec 71 reviews and confirms the policy.

No command from this spec may create active cycle runtime state or phase workspaces.

## Data model / Persistence

Persistent repository policy:

- `.pi/tdd-cycle/config.json`
- `.gitignore`

Expected config lifecycle:

- created by bootstrap when missing
- validated with the unconfirmed-config gate allowed
- left with `confirmed: false`
- reviewed and confirmed later by Spec 71

Runtime paths that remain absent after this spec:

```text
.tdd-cycle/
red/
green/
refactor/
```

The config schema version is owned by the TDD-cycle helper. This repository must not hand-roll a divergent schema.

## Security model (Permission/Isolation/Audit)

Permissions:

- Bootstrap writes only repository policy files.
- Bootstrap does not install hooks, run tests, execute product code, or start tmux.

Isolation:

- Runtime paths are ignored because they are disposable TDD-cycle state.
- The generated config remains unconfirmed to prevent accidental cycle start with unreviewed permissions.

Audit:

- The bootstrap helper's JSON output must be captured or summarized in the implementation review.
- The review must report whether `.gitignore` changed, which entries were added, and whether config validation produced warnings or errors.

## Acceptance criteria (Testable, Verifiable)

- `.gitignore` contains `.tdd-cycle/`, `red/`, `green/`, and `refactor/` exactly once each.
- `.pi/tdd-cycle/config.json` exists after bootstrap.
- `.pi/tdd-cycle/config.json` has `confirmed` set to `false` after bootstrap.
- Bootstrap validation reports no schema errors when unconfirmed configs are allowed.
- `.tdd-cycle/`, `red/`, `green/`, and `refactor/` are not created by this spec.
- The repository status after bootstrap contains only the intended policy file changes.

## Test plan

Bootstrap verification:

```sh
/path/to/custom-pi-style-skills/tdd-cycle-shared/bin/tdd-cycle.js bootstrap --repo-root <repo-root>
```

Config validation:

```sh
/path/to/custom-pi-style-skills/tdd-cycle-shared/bin/tdd-cycle.js validate-config --repo-root <repo-root> --allow-unconfirmed
```

Runtime absence check:

```sh
test ! -e .tdd-cycle && test ! -e red && test ! -e green && test ! -e refactor
```

Repository review:

```sh
git status --short
git diff -- .gitignore .pi/tdd-cycle/config.json
```

## Rollout / Backward compatibility

This spec is additive at the repository-policy layer. It does not change runtime code, tests, installer behavior, or operator docs. Existing contributors who do not use TDD-cycle are only affected by the new ignored runtime folder patterns.

If `.pi/tdd-cycle/config.json` already exists, the implementation must reuse and validate the existing file rather than overwrite it silently.

## Open questions

- Whether future TDD-cycle helper versions will add new required config fields that should be reviewed in a follow-up spec.
- Whether this repository should eventually add a short maintainer note explaining why TDD-cycle runtime folders are ignored.

## Spec Dependencies

- Managed TDD-cycle bootstrap helper availability.
- Existing repository test suite remains runnable independently of TDD-cycle.
