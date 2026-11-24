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

Additional issues fixed in the Python viewer:

- Stale search/drawing state after opening a different document.
- Acceptance of invalid layer types and malformed inline pad shapes before rendering.
- All-layer pad rendering used only one layer's shape.

## Desktop launch

On Ubuntu/Debian, install the XCB runtime packages and run from a graphical desktop terminal.

```bash
sudo apt-get install libxcb-cursor0 libxcb-icccm4 libxcb-util1 libxcb-image0 libxcb-keysyms1 libxcb-render-util0
./scripts/run-python.sh
```

The Python launcher also picks up the runtime libraries if they are unpacked in `.tools/xcb`.
