# Startup investigation

The user reported **failure to launch**. The original launch command was reproduced in this workspace:

```bash
.venv/bin/python -m pcb_viewer
```

It exited with status **134 (Qt abort)** and reported that the `xcb` platform plugin could not be initialized. `ldd` on PySide6's `platforms/libqxcb.so` identified these missing runtime libraries:

- `libxcb-cursor.so.0`
- `libxcb-icccm.so.4`
- `libxcb-util.so.1`
- `libxcb-image.so.0`
- `libxcb-keysyms.so.1`
- `libxcb-render-util.so.0`

`DISPLAY` and `WAYLAND_DISPLAY` are also unset in this environment. Installing a Python wheel does not install these OS libraries or provide a display server. The earlier GUI tests used `QT_QPA_PLATFORM=offscreen`, which bypassed both desktop requirements. Passing those tests did not establish that the ordinary desktop command would launch. These are confirmed local causes; no error log from a separate user machine was provided.

## Changes

The Python entry points now check these conditions before constructing `QApplication` and print actionable diagnostics instead of reaching the Qt abort. `--help` works without Qt initialization, and `--diagnose` checks startup prerequisites. `scripts/run-python.sh` chooses the project virtual environment and uses the locally unpacked XCB libraries when available, without mixing incompatible Qt versions.

A C++17/Qt 6 implementation is in `cpp/`. It has separate `--check` and `--render` modes for headless validation, startup diagnostics, build/run scripts, and documented OS dependencies. C++ still requires Qt runtime libraries and a display for interactive use. It is not a language-level workaround for a missing display server.

Additional source issues found and fixed in both implementations:

- Stale search/drawing state after opening a different document.
- Acceptance of invalid layer types and malformed inline pad shapes before rendering.
- All-layer pad rendering used only one layer's shape.

The C++ implementation additionally makes multi-object edits one undo step and makes target keepouts visible upon creation. The full samples include legitimate `net: null` and zero-radius `unknown_padstack` definitions; these are preserved rather than treated as corrupt files.

## Desktop launch

On Ubuntu/Debian, install the six XCB packages listed in `cpp/README.md` and run from a graphical desktop terminal. In this restricted workspace, dependencies were downloaded and unpacked locally because sudo authentication is unavailable. Use `scripts/run-cpp.sh` or `scripts/run-python.sh` to pick them up. Neither launcher creates a desktop session.
