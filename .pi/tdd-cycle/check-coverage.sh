#!/bin/sh
# tdd-cycle coverage wrapper (managed)
set -eu

repo_root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
if [ -n "${TDD_CYCLE_HELPER:-}" ]; then
  helper=$TDD_CYCLE_HELPER
else
  helper='/home/kang/.pi/agent/skills/custom-pi-style-skills/tdd-cycle-shared/bin/tdd-cycle.js'
fi
node_bin=${NODE:-node}

exec "$node_bin" "$helper" coverage check --repo-root "$repo_root"
