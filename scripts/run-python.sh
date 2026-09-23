#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="$project_dir/.venv/bin/python"
if [[ ! -x "$python_bin" ]]; then python_bin=python3; fi
if [[ -d "$project_dir/.tools/xcb" ]]; then
    export LD_LIBRARY_PATH="$project_dir/.tools/xcb${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi
exec "$python_bin" "$project_dir/main.py" "$@"
