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

The app includes a single-layer, octilinear detailed router and an optional [Freerouting](https://github.com/freerouting/freerouting) benchmark. Open a routing input, select its signal layer, and choose **Auto route…** (Ctrl+R). Select the algorithm in the dialog; for Freerouting, provide a downloaded `.jar` (or set `FREEROUTING_JAR`). The built-in router defaults to denser-area-first priority for higher completion; short-route-first is faster. By default the app tries **one** routing order. You can increase the trial count to add reproducible, locally shuffled variants; it keeps the conflict-free result with the most routed connections, breaking ties by shorter copper length. The dialog controls trial count/seed, grid spacing, copper clearance, early refinement checkpoints, initial congestion-repair passes, board force-compaction iterations, inter-net attraction strength, maximum route stretch, and the Freerouting time limit. All generated traces are committed as one undoable edit and use `route_type: "autoroute"`. The right sidebar shows board-wide auto-route statistics: fully routed and partially routed nets, routed and unrouted requested connections, connection completion, and generated copper length. A net counts as fully routed only when every requested connection for that net is marked routed; the figures update after routing, edits, undo/redo, and tab switches.

The dedicated manual-routing mode has been removed. Previously saved manual traces remain visible as board data, but the app no longer creates new ones through click-to-route controls.

During a built-in auto-route run, the board shows the router's provisional copper in bright green and updates it as connections are added, removed, or rerouted. The sidebar shows provisional connection counts; the optional dashed airwire overlay follows the live routed set. These drawings are temporary, not edits: cancelling or failing restores the original view, and only the selected final result is committed as one undo step. Progress and Cancel routing now occupy the bottom status bar, leaving the board unobstructed. Freerouting is an external Java process and does not expose intermediate traces to this viewer; its board updates when the completed session is imported.

For headless runs:

```bash
python -m pcb_viewer --autoroute \
  "pcb_example/03第三层右侧删去线网（布线输入）.json" \
  --output /tmp/pcb-routed.json --grid 2 --iterations 6 \
  --early-refine-interval 192 --combined-iterations 6 \
  --detailed-max-passes 32 --detailed-drag-states 12 \
  --attraction-strength 1.0 \
  --random-rounds 1 --random-seed 0 \
  --max-detour-ratio 3

# Optional external benchmark, using the same JSON input:
python -m pcb_viewer --autoroute \
  "pcb_example/03第三层右侧删去线网（布线输入）.json" \
  --algorithm freerouting --freerouting-jar /path/to/freerouting.jar \
  --java /path/to/jre-25/bin/java \
  --output /tmp/pcb-freerouted.json
```

The router reads explicit `routing_connections` records (`start`, `end`, `net`, `width`, and `layer`). For older boards with `deleted_connections.wires`, it reads the layer, net, width, and two endpoints. A `routing_connections[].guide` can supply an explicit routing guide.

The flow follows TritonRoute's broad architecture: pin/grid access, pattern routing, DRC-aware A* maze search, then iterative rip-up and search-and-repair with present and historical congestion costs. Optional explicit guides can bias the search, but the supplied testcase is routed from endpoints. PCB pads, vias, copper polygons, existing traces, per-net same-copper access, trace widths, design-rule clearance, and `wire_keepout` polygons participate in obstacle checks. `via_keepout` masks do not block a trace on the current layer.

Pipeline 1 preserves the built-in order from placement through final crossing removal. Its trace-repair blocks repeat straightening, hard-obstacle A*, and segment dragging until a pass makes no changes. When A* fails to find a path, a bounded search can slide interior segments of nearby traces while keeping their terminals fixed. Use `--detailed-max-passes` to cap the passes and `--detailed-drag-states` to bound each drag search. The report records passes, straightened traces, drag-assisted connections, and connections added.

**Place connections**, **Repair congestion**, and **Place & rip-up** forbid foreign-net crossings, including diagonal crossings and intersections at off-grid terminals. A connection that cannot be routed without a crossing stays unrouted. Bounded refinement checkpoints (default every 192 jobs, 0 disables) use repulsion from nearby foreign nets and unrouted airwires to guide legal reroutes. Repair uses a default present congestion cost of 8. Missing-route swap probes use this base cost, and a swap succeeds only when every displaced route can be rebuilt legally.

Multi-round routing varies only the initial job order: each seeded trial adds bounded rank jitter (about 5% of the request count) to the chosen density/length priority. Every trial routes the same JSON independently, including repair and refinement; none reuses copper from another trial. The first trial always keeps the deterministic order, so the selection cannot regress below it by the report score. The app, CLI, and programmatic `RouterConfig` all default to one trial; increase the trial count to compare shuffled orders. `--random-seed` reproduces later permutations. Trial results and the chosen round are included in the output report.

Before the straightening-first change, three file-03 trials with seed 0 and 5% jitter routed 201/220, 195/220, and 195/220 requested connections, respectively; the baseline won. That multi-round search took about 753 seconds versus about 205 seconds for one trial. Randomized trials are therefore an experiment, not a guaranteed improvement; set one trial for a faster run. The global-only straightening-first baseline routed 202/220. With the previous whole-net rebuild version of detailed routing, one deterministic trial routed 204/220 with zero reported conflict cells in about 501 seconds. With the new segment-slide search, the same single-trial input and defaults routed 204/220, also with zero conflict cells and 32,723.182 units of generated copper, in 724 seconds. Detailed routing took three passes and straightened 70 traces, but no drag-assisted connection succeeded on this dense fixture. The new drag search therefore did **not** improve completion here and added runtime; `--detailed-drag-states 0` disables it when speed matters. Focused synthetic tests do show one- and two-net drag-assisted completions. Earlier global-only attraction strengths 0, 0.5, and 2 all selected the same 202-connection board.

The board force-compaction block applies attraction and repulsion to eligible board traces, including generated traces and unlocked existing wires on the active layer. It preserves terminals and junctions and rolls back passes that fail its energy or legality checks. Use `--combined-iterations 0` to disable it. `--force-iterations` is an alias. A force budget of zero also disables compaction. The detour guard removes routes that exceed both the configured stretch ratio and 200 source units of excess length. Set the ratio to zero to disable the guard.

The built-in router emits shared same-net grid edges only once; one requested connection may therefore produce no new copper when it is already covered by another generated branch. The report counts connected requests, while the wire array counts physical trace fragments. An optional reverse-direction pin escape is available through `RouterConfig.reverse_escape`.

The Freerouting adapter exports a temporary single-layer Specctra DSN from the *same open JSON*, runs the external Java router, and imports its session (`.ses`). It does not inspect file 02 or archived trace interiors. It preserves existing wires and copper polygons as protected geometry, and treats non-terminal pad/via copper as fixed keepouts. For a net with several requested widths, the widest width is used for its Freerouting net class. These adapter choices are conservative but mean its result is a benchmark rather than an identical internal search-space comparison. The app shows route count, wire length, and time for both algorithms after they are run on the same tab from the same unchanged input. The app does not bundle or download the JAR; see [official releases](https://github.com/freerouting/freerouting/releases). A six-pass Freerouting 2.1.0 run on the supplied sample completed in about 216 seconds (166/220 requested connections); the default benchmark time limit is ten minutes.

Current Freerouting releases require Java 25; older JARs such as 2.1.0 can run on Java 21. The app inspects the selected JAR's main class to determine its minimum Java version, then selects a compatible runtime from `FREEROUTING_JAVA`, `JAVA_HOME`, a macOS Java installation, or `PATH`. In the Auto route dialog you can instead browse to a specific `bin/java` executable; for headless runs use `--java /path/to/bin/java`. An incompatible or missing runtime produces a clear error before routing starts. This changes only the app's Freerouting subprocess, not the machine's system-wide Java version. The [Freerouting installation guide](https://github.com/freerouting/freerouting#running-freerouting-using-java-jre) describes obtaining a Java 25 JRE.

The command writes an `autoroute_report` into the output. The built-in router's final legalization pass removes trace crossings and lists connections that could not be routed without a crossing. Both backends check emitted polylines for proper centerline intersections; this is not a full fabrication DRC. Exit status is 0 for a fully routed conflict-free result, 2 when output was produced with unrouted connections or reported conflicts, and 1 for an input/runtime error. A status-2 board is preserved for inspection.

This is currently a single-layer research router: it does not insert vias, tune differential pairs, enforce length/impedance constraints, or replace a PCB manufacturer's final DRC. See [ROUTER_DESIGN.md](ROUTER_DESIGN.md) for the data contract, cost model, implementation stages, and limitations.

## Optional PyTorch acceleration

Install `python -m pip install -r requirements-torch.txt`, then select a
**Congestion calculation** backend in the Auto route dialog, or run:

```bash
python -m pcb_viewer "pcb_example/03第三层右侧删去线网（布线输入）.json" \
  --autoroute --compute-backend torch-cpu --output /tmp/pcb-routed.json
```

Backends are `python` (unchanged default), `torch-cpu`, `torch-mps` (Apple GPU),
`torch-cuda` (NVIDIA GPU), and `torch-auto` (CUDA, then MPS, then CPU).
Auto chooses an available device, not the fastest measured device. GPU setup
and transfers can outweigh parallelism on small searches; compare backends
on your board using [the benchmark script](benchmarks/README.md).
On the supplied full file-03 board (Apple M5), Python took 724.9 seconds,
PyTorch CPU 416.2 seconds (1.74× faster), and MPS 433.7 seconds (1.67× faster).
All three emitted exactly the same wires: 204/220 connections, zero reported
conflicts. These are single full runs; see the benchmark results for details.

An explicitly requested unavailable GPU produces an actionable error.
PyTorch is optional and is not imported by the default Python backend.

PyTorch batches clearance-based congestion counts and historical costs for
larger A* searches. The priority queue, search ordering, cost formula, force
refinement, route repair, and final checks retain their existing behavior.
Calculations use integer tensor operations on CPU/GPU; results transfer back
once per field, rather than once per grid expansion. Small searches and
oversized or numerically ambiguous fields use the original scalar checks.
The report records `config.compute_device`, `accelerated_searches`, and
`acceleration_fallbacks`. CUDA is supported but requires validation on CUDA
hardware; local differential tests exercise CPU and Apple MPS when available.

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

## Build and run routing pipelines

Click **Pipelines** in the top toolbar. Use **New** to start with an empty
sequence or **Duplicate** to start from Pipeline 1 or another loaded pipeline.
Drag blocks from **Available blocks** into **Execution order**. Drag again to
reorder them. To remove a block, drag it back to **Available blocks**, click
**Remove block**, or press Delete while it is selected. Blocks can appear more than once.

Enter a name and click **Save** to add the pipeline to the app's library. Use
the **Saved pipelines** list and **Load** to reopen it. Pipeline 1 is the
built-in preset. Save variations under a new name. **Export…** writes a
portable pipeline JSON file, and **Import…** opens one for editing and saving.

Click **Board**, open **Auto route…**, and choose a pipeline. Each trial
runs that pipeline on a fresh copy of the routing input. CLI runs default to
Pipeline 1. To run an exported pipeline, use:

```bash
python -m pcb_viewer input.json --autoroute --pipeline my-pipeline.json --output routed.json
```

In Python, pass `RouterConfig(pipeline=RoutingPipeline("Placement only", ("initial",)))`
with `RoutingPipeline` imported from `pcb_viewer.pipelines`. An empty block list
only checks the current routing state. The optional `--stages` CLI flag,
`RouterConfig.stages`, and **Filter Pipeline 1 stages…** still filter the
original preset in its original order.

**Place & rip-up** can replace **Place connections** and **Repair congestion** in a
custom pipeline. It tries a clear route for each missing connection. If that fails, it
probes for movable blockers, lifts them before placing the new trace, and attempts to
reroute them. A failed repair restores the previous traces. The congestion
repair limit bounds the attempts per blocked connection. A limit of 1 skips
negotiated repair passes. [Placement and rip-up decisions](ROUTER_DESIGN.md#place--rip-up)

**Refine placement** runs at checkpoints when it immediately follows
**Place connections** or **Place & rip-up**. In other positions it refines the current board once.
Consecutive **Straighten traces**, **Connect with A***, and **Drag blocking
segments** blocks run as an ordered group until stable or until the detailed
pass limit. Final clearance and crossing checks run after every pipeline.
Add **Remove crossings** where you want the pipeline to remove crossing
generated traces.

The CLI's `autoroute_report.config.pipeline_blocks` records the selected
order. `stage_results` records elapsed time, connection count, emitted length,
and raster conflicts for each completed step or group. Placement timing
includes its nested checkpoints. See [Pipeline 1's order and block behavior](ROUTER_DESIGN.md#pipeline-1).

## Continue a partially routed board

Open the saved board and use **Auto route → Continue existing routing**. This
option is checked automatically when the board contains generated routes;
uncheck it to start over. For the supplied example:

```bash
python -m pcb_viewer pcb_example/route140.json --autoroute --resume \
  --output /tmp/route140-continued.json
```

Python callers can pass the original board (including its `wires`) to
`autoroute_data(data, RouterConfig(resume=True))`. The returned wires replace
previous generated wires on the routed layer; retain ordinary fixed copper and
other layers. The GUI and CLI perform this replacement automatically. The GUI
commits the whole continuation as one undoable edit, and cancellation preserves
the original board.

Resume imports saved `route_type: "autoroute"` connections using their
`autoroute_job`/`autoroute_jobs` metadata and the original routing requests.
Shared fragments are reassembled and connectivity is checked in source
coordinates before converting to the routing grid. Existing complete connections
become editable routes, and initial placement attempts only missing requests.
Selected repair, legalization, and refinement stages can subsequently modify or
remove imported routes. Unchanged imported centerlines retain their exact saved
geometry, including off-grid terminals. Use the original grid spacing when
possible; route occupancy and any new or modified routes use the selected grid.

The supplied `route140.json` contains 204 wire records representing **206 of 220
connections**, leaving 14 missing. `resumed_connections` records this starting
count; `routed` records final total completion. Stage measurements start from
the imported state. Randomized trials each start from the same saved copper.

This mode resumes saved built-in routing results, not arbitrary CAD netlist
reconstruction. Keep the original connection requests and generated-wire IDs.
Mismatched IDs/nets or fragments that do not connect both terminals produce an
error instead of being counted as completed or silently discarded. Ordinary
untagged wires remain fixed copper. Resume is available for the built-in router;
Freerouting retains its existing fresh-run behavior.

Attraction now includes every nearby foreign net represented by copper on the
active routing layer: fixed wires, pads, pins, vias, and pours, as well as
generated routes. A net contributes once across fixed and generated geometry.
The default attraction strength is **1.0**, doubled from 0.5; the configurable
range remains 0–2, with 0 disabling attraction. Same-net copper, keepouts, and
other-layer copper do not attract. The existing finite attraction range,
missing-airwire protection, displacement cap, and hard legality checks remain.
