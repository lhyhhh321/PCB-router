#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cmake_args=()
if [[ -f "$project_dir/.tools/env.sh" ]]; then
    source "$project_dir/.tools/env.sh"
    cmake_args+=("-DCMAKE_CXX_COMPILER=$PCB_SYSROOT/usr/bin/g++"
                "-DCMAKE_SYSROOT=$PCB_SYSROOT" "-DCMAKE_PREFIX_PATH=$PCB_SYSROOT/usr"
                "-DCMAKE_MAKE_PROGRAM=$PCB_SYSROOT/usr/bin/make")
fi
if ! command -v cmake >/dev/null 2>&1; then
    printf '%s\n' 'Install CMake, a C++17 compiler, and Qt 6 development packages. See cpp/README.md.' >&2
    exit 1
fi
cmake -S "$project_dir/cpp" -B "$project_dir/build" -DCMAKE_BUILD_TYPE=Release "${cmake_args[@]}"
cmake --build "$project_dir/build" --parallel 2
ctest --test-dir "$project_dir/build" --output-on-failure
