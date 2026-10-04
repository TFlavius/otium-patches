#!/bin/sh
set -eu

otium_script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
otium_script="$otium_script_dir/otium_patch.py"
if [ ! -f "$otium_script" ]; then
    printf '%s\n' 'Error: Missing otium_patch.py next to this launcher.' >&2
    exit 1
fi
if command -v python3 >/dev/null 2>&1; then
    exec python3 -B "$otium_script" "$@"
fi
if command -v python >/dev/null 2>&1; then
    exec python -B "$otium_script" "$@"
fi
printf '%s\n' 'Error: Python 3.10 or newer is required. Install Python and add python3 to PATH.' >&2
exit 1
