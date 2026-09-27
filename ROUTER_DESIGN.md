# PCB two-stage router design

## Scope

The router in `pcb_viewer/router.py` is a single-signal-layer PCB router inspired by TritonRoute's architecture. Its ordered placement, congestion repair, legalization, and force refinement form the global stage; a bend/segment drag loop follows as the detailed stage. It is not a source port: TritonRoute targets LEF/DEF IC routing grids and a foundry rule deck, while this application has JSON PCB geometry and a compact clearance rule.

The corresponding TritonRoute/OpenROAD stages are documented in the official [OpenROAD detailed-routing documentation](https://github.com/The-OpenROAD-Project/OpenROAD/blob/master/src/drt/README.md): pin access analysis, track assignment, initial detailed routing, search and repair, and DRC. The PCB router maps those ideas as follows.

| Routing stage | PCB implementation |
| --- | --- |
| Pin access | Preserve exact endpoints, snap the search to a configurable grid, and permit only the exact access cell to escape imperfect stacked source data. |
| Route guides | Use only an explicitly authored `routing_connections[].guide`. Archived deleted-wire interiors are ignored and cannot leak a reference solution into routing. |
| Track assignment | Try a legal octilinear guide/pattern route before invoking maze search. |
| Global initial route | Route the densest pin neighborhoods first (or choose faster short-route-first ordering), using 8-neighbor A*. |
| Randomized order trials | Run the baseline priority order and independently rerun with seeded, bounded rank jitter. Select the cleanest and most complete result, then shorter copper. |
| Early refinement checkpoints | After every configured batch of initial jobs, move a few force-affected routes and call the reusable rip-up/reroute pass before placing more jobs. |
| DRC model | Inflate traces by half-width plus clearance; block foreign-net pads, vias, traces, pours, and wire keepouts. Same-net copper remains accessible. |
| Search and repair | Identify foreign-net trace-spacing conflicts, rip up implicated routes, and reroute with increasing present and historical congestion costs. Retain the best iteration. |
| Final legalization | Keep a conflict-free subset, retry removed connections with hard avoidance, try small route swaps, and reject any remaining geometric intersections. An optional reverse-direction pin escape can be enabled programmatically. |
| Combined straighten/repair/refinement | In each iteration, first replace local zigzags with fewer-bend octilinear patterns; retry missing connections and shorten severe detours by legal local swaps; then sample interior route points, apply balanced attraction/repulsion and spring-gradient steps, and use displaced points as soft A* guides. Retain the best legal completion/length state. |
| Detailed bend/segment routing | Compress every existing connection into bending points and straight segments. Iteratively straighten them, try hard-obstacle A* for missing lines, and drag actual blocking traces when a simple route cannot be found. |
| Detour guard | After local repair, leave an extreme stretch explicitly unrouted instead of committing many times the direct distance; configurable or disabled. |

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

`guide` is optional. The supplied `03第三层右侧删去线网（布线输入）.json` uses `deleted_connections.wires`; only the first/last point, width, net, and layer are extracted. Interior points are discarded. The router never reads `02第三层右侧并删去地线参考.json`.

Geometry is read from `pads`, `pins`, `vias`, `wires`, `copper_polygons`, and `keepouts`. Clearance defaults to `metadata.design_rules.clearance_mil`. Missing via/pad shapes use the same fallback radii as the viewer.

The app, CLI, and `RouterConfig` default to one deterministic trial. Increasing the count adds seeded permutations. For a randomized trial, each job's rank in the selected heuristic order receives uniform jitter in a window of 5% of all requests (at least two places), so priority is perturbed locally instead of discarded. Seeds are deterministic and recorded in `round_results`. Every trial creates a fresh router from the same input board. Trial selection prefers fewer final conflict locations, more connected requests, and finally less emitted copper; exact ties preserve the baseline. A fully routed, conflict-free board stops early. `elapsed_seconds` includes all attempted trials, while each trial's own elapsed time remains in `round_results`.

## Search model

A state is a grid cell. Its eight edges produce horizontal, vertical, and 45-degree trace segments. A* uses octile distance as its heuristic. The incremental cost is:

```text
movement + bend_penalty
         + present_factor * conflicting_routes
         + history_factor * prior_violation_count
         + optional_guide_factor * distance_from_explicit_guide
```

Fixed geometry is a hard constraint. Already-routed traces are negotiated congestion: crossing them is costly rather than initially impossible, allowing a complete candidate solution to be constructed before repair. Each initial congestion-repair pass increases the present cost, increments history around violations, and keeps the lowest-conflict solution seen. This rip-up/reroute pass is a reusable method called both at early checkpoints and in the main negotiation loop. For up to two stalled jobs per pass, it tests a force-displaced guide and keeps the guided candidate only when its blocker count or tied length improves within a bounded stretch. Once hard avoidance is active, historical congestion cost is omitted: a legal short path should not be displaced by old violations that no longer exist.

Output polylines retain exact source/target coordinates, collapse collinear grid points, and are tagged with `route_type: "autoroute"` and `autoroute_job`. When two same-net requests share a grid edge, that copper is emitted once. Output may therefore contain several fragments for one request, or no new fragment when its entire path is already conductive. `autoroute_jobs` on a shared fragment records all covered request IDs; `report.routed` counts connected requests rather than wire records.

## Global-stage early and iterative force refinement

During initial placement, `early_refine_interval` (default 192 attempted jobs, 0 disables) triggers checkpoints before all jobs have been attempted. Each checkpoint samples existing routes, prioritizes those near conflicts or airwires of requests already missing, and tries a small number of force-guided maze reroutes. Because the initial board may still have negotiated conflicts, a move is accepted only when it lowers conflict cells without a severe length penalty, or lowers repulsion/length without increasing conflicts. The extracted rip-up/reroute method then repairs only conflicts involving the accepted early moves; unrelated provisional routes are left for the normal repair passes. A checkpoint that worsens the conflict count is rolled back before initial placement continues. The report records checkpoint and accepted-move counts. These early and ordinary rip-up guides remain repulsion-only; attraction is applied only in the later combined iterations, where a legal-state rollback is available.

After initial maze-routing, congestion repair, and legalization, the combined loop runs up to six iterations by default. **Each iteration has three phases:** first straighten legal zigzags; next retry missing requests and try bounded local swaps that shorten already-connected detours; then perform force/gradient refinement. The detour phase probes a shorter low-congestion path, identifies its actual blockers, temporarily removes at most four of them, and commits the swap only when all displaced routes can be rebuilt legally and total emitted copper length decreases. Straightening examines a bounded number of the bendiest routes and replaces local subpaths with one-bend octilinear patterns. It keeps endpoints fixed, rejects foreign-net clearance or crossing violations, requires fewer bends without increasing path or emitted board length, and rolls back any invalid change. The refinement phase samples at most 20 points along each routed connection, never moving the exact two terminals. For a sampled point `p`, nearby foreign-net route cells repel it inside the preferred separation and weakly attract it in a finite band outside that separation. Attraction is counted once per foreign net, not once per connection; missing connections' straight airwires only repel, preserving corridor room. A weak spring term pulls toward the midpoint of adjacent samples. In conceptual form, the update is a clipped gradient step on

```text
E = sum(weight * max(0, desired_separation - distance)^2
        / (2 * desired_separation))
    - sum(attraction_strength * band / 3 * smoothstep(1 - u))
    + 0.075 / grid_step * sum(|sample - neighbor_midpoint|^2)
```

Here `u = clamp((distance - desired_separation) / band, 0, 1)`, `band = max(2 * grid_step, desired_separation / 2)`, and `smoothstep(1 - u) = 1 - 3u² + 2u³`. Thus the attraction potential is lowest at the preferred separation, levels off at the outer edge of the band, and contributes no force at or inside the preferred separation. Its maximum force magnitude is half `attraction_strength`; the default strength is 0.5, and 0 disables it. Where a missing connection's airwire exerts repulsion, attraction is suppressed so it cannot immediately close the corridor being reserved. Fixed pads and keepouts remain hard A* constraints, not sources of attraction.

The step is capped at two routing-grid spacings; virtual airwires receive double weight to reserve room for unfinished requests. The most affected routed jobs (default at most 12) are searched again with their displaced samples as **soft A* guides**. When attraction is active, up to one third of that candidate budget is reserved for spacing-dominated routes so missing-airwire pressure cannot starve the new method. The raw displaced polyline is never committed directly: hard-avoidance A* must find a legal grid path with fixed endpoints. Missing jobs whose airwires exerted force are then retried. An iteration with crossings or clearance conflicts is rolled back. Gradient moves without an immediate connection gain may be retained provisionally only if combined spacing energy decreases and trace length stays within a 3% cap. Across all iterations, the router saves the legal state with the most completed connections, breaking ties by shorter copper length; a shortening repair can therefore be kept even if no extra request connects. `RouterConfig.force_iterations` (default 6, 0 disables; CLI alias `--combined-iterations`) and `force_max_jobs` (default 12) bound the work.

After global repair, `max_detour_ratio` defaults to 3. A trace is handed to detailed routing as unrouted when its grid path exceeds three times the direct terminal distance **and** adds more than 200 source units. Set the ratio to 0 to allow arbitrarily long legal traces. This exposes single-layer/topological cases instead of silently drawing very long copper, but may reduce the completed-request count. None of these heuristics replaces final CAD DRC.

## Detailed bend/segment stage

After the global stage, `DetailedTrace` represents each connected request as its first terminal, direction-change cells, and last terminal, with straight segments between consecutive bends. The underlying grid path remains the authoritative occupancy and emitted copper; the bend/segment model is a movable control representation. The detailed stage repeats two operations in order:

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
