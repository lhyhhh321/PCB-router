# PCB routing pipeline design

The single-signal-layer router previously ran ordered placement, early
refinement, congestion repair, legalization, missing-route swaps, and board
compaction as global routing. Its detailed routing loop straightened traces,
searched for missing connections with hard-obstacle A*, and dragged blockers
when A* found no path. The detour guard ran on both sides of that loop, followed
by geometric crossing cleanup.

## Pipeline 1

The pipeline executes the original operations in this order. Obstacle
construction, pin and grid access, shared pattern and A* search, and final
read-only conflict checks are available to every pipeline.

| Order | Block | Implementation |
| --- | --- | --- |
| 1 | Place connections (`initial`) | Try safe patterns and 8-neighbor A* in the configured job order. Resumed jobs are skipped. |
| 2 | Refine placement (`early_refinement`) | When adjacent to placement, invoke force-guided reroutes between initial batches. Else refine the current routes once. |
| 3 | Repair congestion (`repair`) | Rip up conflicted routes and reroute with present and historical congestion costs. Keep every rebuilt path free of crossings. |
| 4 | Legalize routes (`legalization`) | Remove conflicts and retry removed jobs with hard avoidance. |
| 5 | Swap routes to make room (`missing_route_swaps`) | Probe for missing paths and rebuild a bounded set of displaced routes. |
| 6 | Compact board traces (`combined`) | Apply the board-wide geometric force model to eligible traces and retain accepted moves. |
| 7 | Reject excessive detours (`detour_guard`) | Reopen overlong connections so the following blocks can attempt them. |
| 8 | Straighten traces (`detailed_straighten`) | Simplify legal bend spans while keeping terminals fixed. |
| 9 | Connect with A* (`detailed_astar`) | Try hard-obstacle A* for each missing connection. |
| 10 | Drag blocking segments (`detailed_drag`) | When A* finds no path, try sliding nearby interior segments to open a route. |
| 11 | Reject excessive detours (`detour_guard`) | Remove excessive stretches remaining after trace repair. |
| 12 | Remove crossings (`crossing_cleanup`) | Remove crossing generated routes, choosing the most conflicted route first. |

Blocks 8–10 are consecutive, so they repeat in that order until a pass makes
no changes or reaches the configured pass limit. An intervening non-repair
block starts a separate loop for any following trace-repair blocks.
Consecutive A* and drag blocks run in order per missing connection. Stop
when a path is accepted or when an A* candidate is rejected by the board-wide
legality check. Trials each execute the same pipeline with an independent
routing state.

## Place & rip-up

The optional `place_ripup` block uses the configured placement order. It first
attempts to repair existing raster conflicts or geometric crossings in resumed or
previously generated routes. For each remaining connection, it chooses the next
action from the result of the search:

1. Place a path found with hard clearance and crossing checks.
2. If strict placement fails, run a congestion-weighted probe. Move on if the
   probe finds no route. Place a returned path directly if it has no blockers.
3. If the probe finds movable blockers, lift those traces before inserting the
   path. Reroute the lifted connections with increasing congestion costs. Before
   each further pass, focus on routes changed by the attempt. Keep the result
   only if all connections are preserved and all changed routes are clear of
   blockers. A failed or cancelled attempt restores the previous routes.

Both **Place & rip-up** and **Repair congestion** use the trace-intersection and
diagonal checks from **Place connections**. These checks apply to every reroute,
including fallback searches that price clearance congestion. Existing generated
crossings are lifted before repair, and routes that cannot be rebuilt without
crossing copper stay unrouted. The placement probe runs off the board, so its
intersections never appear in the routed state or the live preview.

The congestion repair limit allows up to `iterations - 1` passes for existing
conflicts and up to that many passes per blocked connection. A following
**Refine placement** block still runs at placement checkpoints. Pipeline 1
uses its original separate placement and repair blocks.

## Input contract

Preferred inputs provide an explicit connection list:

```json
{
  "routing_connections": [
    {
      "net": "N1",
      "layer": "ART03",
      "width": 2.0,
      "start": [10.0, 20.0],
      "end": [80.0, 50.0],
      "guide": [[10.0, 20.0], [40.0, 20.0], [80.0, 50.0]]
    }
  ]
}
```

`guide` is optional. For older inputs with `deleted_connections.wires`, the router extracts the first/last point, width, net, and layer. Interior points are discarded. The router never reads `02第三层右侧并删去地线参考.json`.

Geometry is read from `pads`, `pins`, `vias`, `wires`, `copper_polygons`, and `keepouts`. Clearance defaults to `metadata.design_rules.clearance_mil`. Missing via/pad shapes use the same fallback radii as the viewer.

Nearest routing starts with the remaining connection whose terminals are farthest apart. Each next connection minimizes the Euclidean distance between any of its terminals and either terminal of the previous choice. The baseline resolves ties by wider trace and then connection ID. Already routed connections are excluded from this order.

The app, CLI, and `RouterConfig` default to one deterministic trial. Later trials add seeded rank jitter for density-first and short-route-first in a window of 5% of all requests (at least two places). Nearest routing uses the seed for equal-distance ties. Every trial creates a fresh router from the same input board. Trial selection prefers fewer final conflict locations, more connected requests, and finally less emitted copper. Exact ties preserve the baseline. A fully routed, conflict-free board stops early. `elapsed_seconds` includes all attempted trials, and `round_results` records each trial's seed and elapsed time.

## Search model

A state is a grid cell. Its eight edges produce horizontal, vertical, and 45-degree trace segments. A* uses octile distance as its heuristic. The incremental cost is:

```text
movement + bend_penalty
         + present_factor * conflicting_routes
         + history_factor * prior_violation_count
         + optional_guide_factor * distance_from_explicit_guide
         + unrouted_pin_cost * max(0, 1 - distance_to_nearest_pending_pin / radius)^2
```

The pin term uses the nearest pending terminal on another net. Its radius is the board clearance plus the largest trace half-width plus twelve grid steps. The default peak cost is 4 in A* step units, and `RouterConfig(unrouted_pin_cost=0)` disables the term. The search uses the terminal's actual coordinates. A terminal shared by several requests stops contributing as soon as any of those requests is routed. A legal pattern through a pin penalty region triggers an A* search and remains available if the search fails.

Initial patterns and maze searches treat fixed geometry and existing foreign-net routes as hard constraints. They check grid occupancy, opposing diagonal steps, and physical trace intersections, including those involving off-grid terminal segments. An initial job with no legal path stays unrouted. The soft blocker probes and both rip-up blocks use a shared base present congestion cost of 8 per conflicting route. When a resumed board contains raster violations, each congestion-repair pass increases the present cost, increments history around violations, and retains the lowest-conflict solution seen. For up to two stalled jobs per pass, it tests a force-displaced guide and keeps the guided candidate only when its blocker count or tied length improves within a bounded stretch. Hard-avoidance searches omit historical congestion cost.

Output polylines retain exact source/target coordinates, collapse collinear grid points, and are tagged with `route_type: "autoroute"` and `autoroute_job`. When two same-net requests share a grid edge, that copper is emitted once. Output may therefore contain several fragments for one request, or no new fragment when its entire path is already conductive. `autoroute_jobs` on a shared fragment records all covered request IDs; `report.routed` counts connected requests rather than wire records.

## Placement refinement and board compaction

The checkpoint interval defaults to 192 attempted jobs. Checkpoints sample
existing traces, prioritize those near airwires or conflicts, and try a bounded
number of legal force-guided reroutes. Accepted changes reduce repulsion,
length, or conflict count within the checkpoint's stretch bounds. A zero
interval disables the in-placement checkpoints.

Board compaction in `pcb_viewer/compaction.py` uses a geometric force field.
Foreign nets repel nearby copper and weakly attract it within a finite band.
It considers generated routes and eligible unlocked wires on the active layer.
Pads, pins, vias, and pours contribute to the field while staying anchored.
The block projects forces onto the normal of each trace and accumulates
sub-grid displacement across iterations. It preserves terminals and same-net
junctions. A pass is accepted when energy decreases without increasing raster
conflicts or adding geometric crossings. Cancelled compaction restores the
block's starting routes. The default limit is six iterations.

The detour guard removes a trace when its grid path exceeds the configured
ratio of terminal-to-terminal distance and adds more than 200 source units.
The default ratio is three. A zero ratio disables the guard.

## Trace-repair blocks

`DetailedTrace` represents each connected request as its first terminal, direction-change cells, and last terminal, with straight segments between consecutive bends. The underlying grid path remains the authoritative occupancy and emitted copper; the bend/segment model is a movable control representation. Pipeline 1 runs these operations in each repair pass:

1. Straighten routed traces. A bend-span is replaced by a shorter one-bend octilinear pattern only if the path has fewer bends, does not grow, and remains clear of fixed geometry, foreign nets, and centerline crossings. Terminals stay pinned.
2. For each missing connection, try hard-obstacle A* with zero historical congestion cost, first near its terminal corridor and then over the full board. If no legal path exists, a soft A* probe supplies a candidate corridor. Rank *both* actual blockers and traces within eight grid spacings plus width/clearance of that corridor. Up to 16 nearby internal bend segments (at most two per net) are considered. A move translates exactly one segment parallel by one to four grid cells in either direction. The two new bend cells are exact integer intersections with the original neighboring segment lines; those neighbors may shorten or lengthen but cannot rotate, while every other segment and both terminals stay untouched. Fractional-grid intersections, reversals, self-overlap, detours, fixed obstacles, foreign-net clearance violations, and diagonal crossings are rejected. A bounded best-first search ranks states by how many nets still block the probe corridor, can combine moves on up to three distinct traces, and tries hard A* for the missing connection after each state. It evaluates at most `detailed_drag_states` states (default 12; zero disables dragging). The completed board receives final clearance and crossing checks; unsuccessful searches restore the original routes exactly. This is a constrained geometric drag, not a whole-net rip-up/rebuild.

The loop reaches a fixed point when a whole pass neither removes a bend nor connects a missing request. A 32-pass default safety limit prevents pathological boards from running indefinitely; `detailed_max_passes=0` disables the stage. The report separates `detailed_passes`, `detailed_straightened`, `detailed_dragged`, and `detailed_new_connections` from global-stage counts. Like the global route, this remains a single-layer geometric heuristic, not fabrication DRC.

The desktop built-in route supplies a read-only preview callback at routing progress boundaries. It receives the active router for the current randomized-order trial; the Qt scene compares its actual route-cell paths with the previous snapshot and updates only changed temporary trace items, throttled to roughly seven repaints per second. A new trial clears the preceding trial's preview. Old generated copper is hidden only in the scene, never removed from the document; cancellation and errors remove temporary items and restore the old display. The selected final trial is still committed once, as one undo step. Freerouting has no corresponding intermediate in-process route state.

## Test fixture result

With grid spacing 2, default clearance, six initial congestion-repair passes, six global combined iterations, dense-first ordering, no routing guides, and one deterministic order trial, the **previous whole-net rebuild** version of the two-stage implementation routed 204/220 requests on file 03. It emitted 32,723.18 source units of copper with zero reported conflict cells in about 501 seconds. The new one-segment slide search, run on the same file 03 with the same settings and the default 12 search states, also routed 204/220 with zero conflict cells and 32,723.182 units of copper, but took 724 seconds. Both runs took three detailed passes, straightened 70 traces, recovered two connections after global routing, and recorded zero successful drag-assisted connections. Thus the new search is a geometric-behavior correction and a capability demonstrated in targeted tests, **not** a completion gain on this fixture; it costs runtime. Set `detailed_drag_states=0` to skip dragging. The global-only straightening-first baseline routed 202/220 in about 215 seconds with 32,255.786 source units. A smaller grid can improve geometric resolution at a substantial runtime/memory cost.

Randomized-order trials were tested against that same file-03 input before the straightening-first change, with no other board file loaded. The first experiment used a 15%-of-job-count jitter window: baseline/seed-0/seed-1 completed 201/194/193 requests in about 711 seconds total. After reducing the jitter window to 5%, the three trials completed 201/195/195 requests in about 753 seconds total, all with zero reported conflict cells. Thus this set of seeds **did not improve** completion; the selected result remained the baseline. The 5% window is retained because it disrupted priority less, but multi-round routing is computationally expensive and not guaranteed to help. A different seed, trial count, or board may yield a better result. Use one trial when turnaround time matters.

## Freerouting comparison

`pcb_viewer/freerouting.py` converts the same JSON request to a temporary single-layer Specctra DSN, invokes a user-provided Freerouting JAR, imports the `.ses` result, and reports connected requests, new wire length, elapsed time, and crossings. Before launching, it reads the JAR's main class-file version and chooses a compatible Java runtime; current Freerouting releases require Java 25, while the older 2.1.0 benchmark JAR runs on Java 21. The exporter maps requested endpoints to pins, existing wires/polygons to protected copper, and other pad/via geometry to fixed keepouts. Per-net width uses the widest requested edge. Those conservative modeling differences mean the results are a baseline, not an identical-route-space comparison. A six-pass Freerouting 2.1.0 run on the supplied file 03 completed 166/220 requested connections in about 216 seconds with 24,729.913 units of new trace and zero proper generated centerline crossings. Freerouting's stochastic results may vary between runs. The adapter has a configurable time limit (default ten minutes).

## Limitations and next steps

- One routing layer per run; there is no via selection or layer assignment.
- The DRC is grid/raster based. It covers width/clearance and the supported shapes, not a full fabrication rule deck.
- No differential-pair coupling, length matching, impedance, neck-down, thermal, or current-density rules.
- Copper-pour connectivity is treated geometrically; there is no post-route electrical graph/LVS pass yet.
- Search is synchronous in the GUI. It remains cancelable between connections, but a worker thread would improve responsiveness for very fine grids.
- Dense boards should eventually use tiled workers and localized DRC markers, closer to TritonRoute's scalable worker model.

Always inspect status-2 results and run the target CAD/fabrication DRC before manufacturing.

## Optional parallel congestion fields

`RouterConfig.compute_backend` selects an optional PyTorch engine implemented
in `pcb_viewer/torch_costs.py`. After 256 expansions in an A* invocation, the
engine snapshots its search rectangle and computes dynamic cell costs in
batches. Each foreign route has a Boolean clearance footprint, using the same
width-dependent offsets and strict clearance predicate as `_dynamic_context`.
Reducing these footprints counts distinct route IDs (not nearby cells and not
nets). Same-net routes are excluded. Sparse history is accumulated with integer
scatter-add using the original, wider history stencil. Diagonal crossings
remain a separate edge check in A*. Static geometry and exact endpoint access
retain the scalar implementation.

Routes are grouped by width and packed into parallel tensor operations. Route
batches limit footprint storage to 32 million Boolean entries; broadcasts are
chunked, and search rectangles exceeding one million cells use scalar checks.
The numerical guard also falls back when floating coordinate subtraction
could affect a clearance-boundary decision. Tensor fields live only for one
A* call, so commits, removals, rollbacks and history changes require no cache
invalidation protocol. Counts and history return as host integer lists; A*
performs no per-cell device synchronization. PyTorch CPU uses its native
threaded kernels; CUDA and MPS execute those tensor operations on the GPU.
No route order, stopping criterion, heuristic or legalization rule changes.

See [benchmarks/README.md](benchmarks/README.md) for profiling evidence and
reproduction. Accelerating independent randomized trials with processes is
possible future work, but is separate from this tensor backend.

## Pipeline configuration and measurements

`RoutingPipeline` stores a name and an ordered tuple of block IDs, including
repeats. The app saves each definition as a separate JSON file in `pipelines/`
at the project root. Project files and exports both contain `version`, `name`,
and `blocks` and can be passed to `--pipeline`.
`RouterConfig.pipeline` accepts a pipeline directly. When omitted, the router
uses Pipeline 1. Legacy `RouterConfig.stages` and `--stages` options filter
Pipeline 1 in its original order. Filtering out initial placement also skips
its nested checkpoints. Iteration limits and `strict_final` retain their
previous behavior.

Final read-only clearance and geometric crossing checks run after the last
block. Geometric crossing pairs contribute to the final conflict count
alongside raster conflict locations. The **Remove crossings** block modifies
the route state wherever it is placed.

The report identifies the pipeline and block order in `config`. Each executed
step adds a `stage_results` record with before/after connection count, emitted
length, raster conflicts, elapsed time, and cancellation status. Adjacent
legalization and swaps share one record, and a contiguous trace-repair loop
shares one record. Pipeline 1 reports its detour phases separately. Nested
early checkpoints appear before their enclosing placement record and their
time is included in placement. Top-level results describe the selected trial,
and every `round_results` entry retains its own measurements.

## Resume saved partial routing

`RouterConfig.resume` defaults to false. When enabled, generated wires on the
selected layer are removed from the fixed-obstacle input and reconstructed as
initial entries in `routes`, `usage`, and diagonal occupancy. Non-generated
copper and other layers remain fixed. The input dictionary is not mutated.

`_load_existing_routes()` groups saved wire fragments by their recorded request
IDs, verifies matching nets, builds source-coordinate connectivity graphs, and
splits segments at shared fragment junctions and terminals. A complete path
between each recorded request's exact terminals is required. This avoids treating
a small physical gap as connected merely because its ends snap to one grid cell.
Saved centerlines are retained for unchanged imported paths; modified paths use
the normal grid-derived output. The search region includes imported geometry.

Initial routing skips already-loaded jobs. Imported copper is movable through
all selected repair/refinement stages, and existing legalization checks still
apply. Each randomized trial independently imports the same partial board.
`RouteReport.resumed_connections` distinguishes initial completion from final
`routed`; stage before/after measurements include the imported routes.
The CLI and GUI replace generated wires once after successful completion,
avoiding duplicate copper. Invalid or disconnected saved route records fail
before the GUI document or output file is changed.
