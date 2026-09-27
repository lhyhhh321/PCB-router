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
- Enable **Show unrouted connections (dashed)** to display straight-line airwires for every requested connection without generated copper. The toggle is saved per open tab and does not modify the JSON.
- Wheel zooms around the pointer; middle/right drag pans; F fits visible geometry. The initial full-board view displays the first layer for readability.
- Enter a net or reference substring and press Enter to filter geometry. Keepouts remain visible.
- Click geometry to inspect its original JSON. Edit coordinates, net, layer, width, or other properties, then click **Apply properties**. Shift-click or drag a selection rectangle to select several objects. **Move selected by offset** translates geometry; Delete removes selected objects.
- Undo: Ctrl+Z. Redo: Ctrl+Shift+Z. Multi-object moves/deletes are recorded one object at a time.

## Auto routing

The app includes a single-layer, octilinear detailed router and an optional [Freerouting](https://github.com/freerouting/freerouting) benchmark. Open a routing input, select its signal layer, and choose **Auto route…** (Ctrl+R). Select the algorithm in the dialog; for Freerouting, provide a downloaded `.jar` (or set `FREEROUTING_JAR`). The built-in router defaults to denser-area-first priority for higher completion; short-route-first is faster. By default the app tries three routing orders: the original heuristic order and two reproducible, locally shuffled variants. It keeps the conflict-free result with the most routed connections, breaking ties by shorter copper length; a fully completed route stops further trials. The dialog controls trial count/seed, grid spacing, copper clearance, early refinement checkpoints, initial congestion-repair passes, combined straighten/repair/balance iterations, inter-net attraction strength, maximum route stretch, and the Freerouting time limit. All generated traces are committed as one undoable edit and use `route_type: "autoroute"`. The right sidebar shows board-wide auto-route statistics: fully routed and partially routed nets, routed and unrouted requested connections, connection completion, and generated copper length. A net counts as fully routed only when every requested connection for that net is marked routed; the figures update after routing, edits, undo/redo, and tab switches.

For headless runs:

```bash
python -m pcb_viewer --autoroute \
  "pcb_example/03第三层右侧删去线网（布线输入）.json" \
  --output /tmp/pcb-routed.json --grid 2 --iterations 6 \
  --early-refine-interval 192 --combined-iterations 6 \
  --detailed-max-passes 32 \
  --attraction-strength 0.5 \
  --random-rounds 3 --random-seed 0 \
  --max-detour-ratio 3

# Optional external benchmark, using the same JSON input:
python -m pcb_viewer --autoroute \
  "pcb_example/03第三层右侧删去线网（布线输入）.json" \
  --algorithm freerouting --freerouting-jar /path/to/freerouting.jar \
  --java /path/to/jre-25/bin/java \
  --output /tmp/pcb-freerouted.json
```

The router reads explicit `routing_connections` records (`start`, `end`, `net`, `width`, and `layer`). For this repository's testcase, it reads only those fields and the two endpoints from `deleted_connections.wires`. Every archived interior point is ignored: file 02 is never loaded, compared, or used as a reference. An explicitly authored `routing_connections[].guide` remains supported for other input formats.

The flow follows TritonRoute's broad architecture: pin/grid access, pattern routing, DRC-aware A* maze search, then iterative rip-up and search-and-repair with present and historical congestion costs. Optional explicit guides can bias the search, but the supplied testcase is routed from endpoints. PCB pads, vias, copper polygons, existing traces, per-net same-copper access, trace widths, design-rule clearance, and `wire_keepout` polygons participate in obstacle checks. `via_keepout` masks do not block a trace on the current layer.

The built-in flow now has two stages. The existing ordered placement, congestion repair, legalization, and force iterations are global routing. Afterward, detailed routing models each routed connection as bend points joined by straight segments. In each pass it first removes legal bends, then runs hard-obstacle A* for unfinished connections. If A* cannot connect one, a soft probe identifies the actual blocking traces; the router temporarily removes at most four blockers, commits the missing connection, and drags each blocker's nearest segment into a parallel jog using a bend/segment guide and hard A*. The entire local change rolls back unless every displaced trace is rebuilt with no clearance conflicts or geometric crossings. Passes stop when no bends are removed and no new connection is made, or at the configurable safety limit (`--detailed-max-passes 32`, with 0 disabling this stage). The report records detailed passes, straightened traces, blocker drags, and connections added.

Initial routing now has bounded refinement checkpoints (default every 192 jobs, 0 disables). At each checkpoint, sampled points repel nearby foreign nets and airwires of requests that have already failed, then a legal or lower-conflict reroute is attempted for a few affected routes. The same checkpoint invokes a separate rip-up/reroute method only for conflicts involving accepted early moves, before routing more jobs. That reusable method also applies a bounded force-guided fallback when an ordinary rip-up reroute stalls; it keeps the fallback only if its local blocker/length score improves. This lets early decisions respond to congestion without disturbing unrelated routes.

Multi-round routing varies only the initial job order: each seeded trial adds bounded rank jitter (about 5% of the request count) to the chosen density/length priority. Every trial routes the same JSON independently, including repair and refinement; none reuses copper from another trial. The first trial always keeps the old deterministic order, so the selection cannot regress below it by the report score. `--random-rounds 1` runs only that baseline; `--random-seed` reproduces the later permutations. Trial results and the chosen round are included in the output report. Programmatic `RouterConfig` keeps one trial by default for existing callers; the app and CLI request three.

Before the straightening-first change, three file-03 trials with seed 0 and 5% jitter routed 201/220, 195/220, and 195/220 requested connections, respectively; the baseline won. That multi-round search took about 753 seconds versus about 205 seconds for one trial. Randomized trials are therefore an experiment, not a guaranteed improvement; set one trial for a faster run. The global-only straightening-first baseline routed 202/220. With the new detailed stage enabled, one deterministic trial routed 204/220 with zero reported conflict cells in about 501 seconds. Detailed routing took three passes, straightened 70 traces, and recovered two overlong connections; no blocker drag succeeded on this fixture. The stage improves completion here but has a substantial runtime cost. Earlier global-only attraction strengths 0, 0.5, and 2 all selected the same 202-connection board.

After initial routing and congestion repair, each combined iteration (1) straightens legal zigzags with short octilinear replacements, (2) retries missing connections and tries local swaps to shorten severe detours, then (3) samples interior route points for force/gradient refinement. Straightening keeps endpoints fixed, checks foreign copper and fixed keepouts, and accepts only fewer bends without added emitted wire length. Nearby foreign-net copper repels points when too close and weakly attracts them when farther than a preferred spacing, producing a bounded spacing equilibrium; virtual straight-line corridors of still-unrouted connections only repel. A weak spring further discourages sharp bends. The moved points are **soft guides**, not copper: legal A* reroutes the selected nets, then retries affected unfinished connections. Each iteration is checked for clearance and geometric crossings, and the best legal completion/length result is retained. Hard-avoidance A* ignores old congestion-history penalties so they cannot force a legal route around a needless loop. Finally, routes longer than the configured stretch ratio *and* more than 200 source units beyond their straight-line distance are left explicitly unrouted; 0 disables this limit. Use `--combined-iterations 0` to disable the combined stage (`--force-iterations` remains an alias); `--force-max-jobs` limits the per-iteration gradient work. `--attraction-strength 0` disables the added attraction for comparison.

The built-in router emits shared same-net grid edges only once; one requested connection may therefore produce no new copper when it is already covered by another generated branch. The report counts connected requests, while the wire array counts physical trace fragments. An optional reverse-direction pin escape is available through `RouterConfig.reverse_escape`.

The Freerouting adapter exports a temporary single-layer Specctra DSN from the *same open JSON*, runs the external Java router, and imports its session (`.ses`). It does not inspect file 02 or archived trace interiors. It preserves existing wires and copper polygons as protected geometry, and treats non-terminal pad/via copper as fixed keepouts. For a net with several requested widths, the widest width is used for its Freerouting net class. These adapter choices are conservative but mean its result is a benchmark rather than an identical internal search-space comparison. The app shows route count, wire length, and time for both algorithms after they are run on the same tab from the same unchanged input. The app does not bundle or download the JAR; see [official releases](https://github.com/freerouting/freerouting/releases). A six-pass Freerouting 2.1.0 run on the supplied sample completed in about 216 seconds (166/220 requested connections); the default benchmark time limit is ten minutes.

Current Freerouting releases require Java 25; older JARs such as 2.1.0 can run on Java 21. The app inspects the selected JAR's main class to determine its minimum Java version, then selects a compatible runtime from `FREEROUTING_JAVA`, `JAVA_HOME`, a macOS Java installation, or `PATH`. In the Auto route dialog you can instead browse to a specific `bin/java` executable; for headless runs use `--java /path/to/bin/java`. An incompatible or missing runtime produces a clear error before routing starts. This changes only the app's Freerouting subprocess, not the machine's system-wide Java version. The [Freerouting installation guide](https://github.com/freerouting/freerouting#running-freerouting-using-java-jre) describes obtaining a Java 25 JRE.

The command writes an `autoroute_report` into the output. The built-in router's final legalization pass removes trace crossings and lists connections that could not be routed without a crossing. Both backends check emitted polylines for proper centerline intersections; this is not a full fabrication DRC. Exit status is 0 for a fully routed conflict-free result, 2 when output was produced with unrouted connections or reported conflicts, and 1 for an input/runtime error. A status-2 board is preserved for inspection.

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
