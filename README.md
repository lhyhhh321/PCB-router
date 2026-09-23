# PCB Studio

A native C++/Qt 6 version is now available: see [cpp/README.md](cpp/README.md). For the reproduced launch failure and fixes, see [STARTUP_FINDINGS.md](STARTUP_FINDINGS.md).

Python startup diagnostics: `./scripts/run-python.sh --diagnose`. Run from a graphical desktop; a headless terminal cannot show the interactive window.

A Python / PySide6 desktop PCB JSON viewer and geometry editor, built for the full-board and filtered-layer files in `pcb_example/`.

## Run

Python 3.10 or newer and a desktop display are required.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m pcb_viewer
# Or open a board directly:
python -m pcb_viewer "pcb_example/01全板.json"
```

On Windows, activate with `.venv\Scripts\activate`. `python main.py` is an equivalent entry point. Linux systems may need their distribution's Qt/XCB runtime packages if Qt reports a missing platform library.

## Viewing and editing

- Open one or several JSON files with Ctrl+O. Each board has a browser-style tab at the top; clicking a tab restores that board's layer, filter, tool settings, and viewport. The tab close button checks for unsaved changes. Save uses Ctrl+S, Save as uses Ctrl+Shift+S, and New opens a blank TOP-layer tab.
- Choose a visible layer or All layers. Toggle wires, vias, pins, pads, copper polygons, component origins, and keepouts separately.
- Wheel zooms around the pointer; middle/right drag pans; F fits visible geometry. The initial full-board view displays the first layer for readability.
- Enter a net or reference substring and press Enter to filter geometry. Keepouts remain visible.
- Click geometry to inspect its original JSON. Edit coordinates, net, layer, width, or other properties, then click **Apply properties**. Shift-click or drag a selection rectangle to select several objects. **Move selected by offset** translates geometry; Delete removes selected objects.
- Undo: Ctrl+Z. Redo: Ctrl+Shift+Z. Multi-object moves/deletes are recorded one object at a time.

## Auto routing

The app includes a single-layer, octilinear detailed router. Open a routing input, select its signal layer, and choose **Auto route…** (Ctrl+R). The dialog controls grid spacing, copper clearance, and search-and-repair passes. All generated traces are committed as one undoable edit and use `route_type: "autoroute"`.

For headless runs:

```bash
python -m pcb_viewer --autoroute \
  "pcb_example/03第三层右侧删去线网（布线输入）.json" \
  --output /tmp/pcb-routed.json --grid 2 --iterations 6
```

The router reads explicit `routing_connections` records (`start`, `end`, `net`, `width`, and `layer`). For this repository's testcase, it reads only those fields and the two endpoints from `deleted_connections.wires`. Every archived interior point is ignored: file 02 is never loaded, compared, or used as a reference. An explicitly authored `routing_connections[].guide` remains supported for other input formats.

The flow follows TritonRoute's broad architecture: pin/grid access, pattern routing, DRC-aware A* maze search, then iterative rip-up and search-and-repair with present and historical congestion costs. Optional explicit guides can bias the search, but the supplied testcase is routed from endpoints. PCB pads, vias, copper polygons, existing traces, per-net same-copper access, trace widths, design-rule clearance, and `wire_keepout` polygons participate in obstacle checks. `via_keepout` masks do not block a trace on the current layer.

The command writes an `autoroute_report` into the output. A final legalization pass removes trace crossings and lists connections that could not be routed without a crossing. It also checks the emitted polylines for geometric intersections. Exit status is 0 for a fully routed conflict-free result, 2 when output was produced with unrouted connections or remaining approximate clearance conflicts, and 1 for an input/runtime error. A status-2 board is preserved for inspection.

This is currently a single-layer research router: it does not insert vias, tune differential pairs, enforce length/impedance constraints, or replace a PCB manufacturer's final DRC. See [ROUTER_DESIGN.md](ROUTER_DESIGN.md) for the data contract, cost model, implementation stages, and limitations.

## Route keepout masks

1. Choose **Place keepouts on layer**, and choose `wire_keepout` (routing mask) or `via_keepout`.
2. Set snap spacing in the original coordinate units; use 0 to disable snapping.
3. Choose **Rectangle** and click two opposite corners, or **Polygon**, click vertices, and press Enter (or Finish polygon).
4. Esc cancels unfinished geometry. Switch back to Select to edit, move, or delete a mask.
5. Save the JSON. New masks are closed polygons in the existing format:

```json
{"type":"wire_keepout","layer":"ART03","shape":"polygon","points":[[0,0],[100,0],[100,50],[0,50],[0,0]]}
```

A mask applies to its chosen layer. Repeat placement on other layers when needed. Wire masks are pink and via masks amber. Masks may overlap existing traces; they do not remove or reroute them until an auto-routing run replaces generated traces.

## Format and rendering

The document retains all supplied keys, metadata, padstacks, nets, deleted-connection archives, and unknown fields. Save uses an atomic file replacement. An unedited round trip preserves JSON values (whitespace is reformatted); relevant existing statistics are recalculated after edits. Original sample files are not modified unless you save over them.

Coordinates are stored unchanged; display Y is inverted so positive Y points up. No resolution scaling is applied. Full-board pads/vias use `padstacks.shapes_per_layer`; filtered files use inline pad shapes. Where shape definitions are absent, a small fallback marker is used. Rectangular pad shapes are shown axis-aligned; component footprint rotation/mirroring is not reconstructed. Component markers represent origins, not package outlines; moving one does not move its separate pins or routing. All-layer pad display combines distinct applicable layer shapes. Board fitting uses actual geometry rather than the samples' unusually small metadata boundary rectangle.

The property editor changes individual JSON records, so it is intended for geometric edits rather than coordinated netlist or footprint refactoring. It validates coordinate structure and positive wire widths; newly edited keepout polygons must also have nonzero area and no crossing edges.

## Verification

```bash
pip install pytest
QT_QPA_PLATFORM=offscreen python -m pytest -q
```

Tests cover all four supplied boards, lossless JSON round trips, mask persistence and metadata preservation, undo/redo (including a complete routing run as one edit), failed saves, invalid polygons, router obstacle behavior, and offscreen GUI rendering.
