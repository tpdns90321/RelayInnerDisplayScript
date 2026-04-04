#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if [ "$(id -u)" -ne 0 ]; then
  echo "uninstall.sh must be run as root" >&2
  exit 1
fi

if [ -n "${PYTHONPATH:-}" ]; then
  export PYTHONPATH="$script_dir:$PYTHONPATH"
else
  export PYTHONPATH="$script_dir"
fi

exec python3 -m relayinner_display.bootstrap --repo-root "$script_dir" --uninstall "$@"
