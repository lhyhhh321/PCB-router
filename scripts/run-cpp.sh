#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
# The optional local toolchain is used in this workspace where sudo is unavailable.
if [[ -f "$project_dir/.tools/env.sh" ]]; then source "$project_dir/.tools/env.sh"; fi
binary="$project_dir/build/pcb_studio"
if [[ ! -x "$binary" ]]; then
    printf '%s\n' 'C++ application is not built. Run scripts/build-cpp.sh first.' >&2
    exit 1
fi
if command -v ldd >/dev/null 2>&1; then
    missing="$(ldd "$binary" | awk '/not found/ {print $1}')"
    if [[ -n "$missing" ]]; then
        printf 'Missing shared libraries:\n%s\nInstall Qt 6 runtime packages, or see cpp/README.md.\n' "$missing" >&2
        exit 1
    fi
fi
exec "$binary" "$@"
