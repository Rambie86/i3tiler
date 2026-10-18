#!/bin/sh
# Launcher for the recursive tiling manager (used from exec_always in the i3 config).
# Single instance: every i3 reload replaces the previous one.
cd "$(dirname "$0")" || exit 1
exec python3 -m i3tiler daemon "$@"
