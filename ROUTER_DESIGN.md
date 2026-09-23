# PCB detailed-router design

## Scope

The router in `pcb_viewer/router.py` is a single-signal-layer PCB detailed router inspired by TritonRoute's architecture. It is not a source port: TritonRoute targets LEF/DEF IC routing grids and a foundry rule deck, while this application has JSON PCB geometry and a compact clearance rule.

The corresponding TritonRoute/OpenROAD stages are documented in the official [OpenROAD detailed-routing documentation](https://github.com/The-OpenROAD-Project/OpenROAD/blob/master/src/drt/README.md): pin access analysis, track assignment, initial detailed routing, search and repair, and DRC. The PCB router maps those ideas as follows.

| Routing stage | PCB implementation |
| --- | --- |
| Pin access | Preserve exact endpoints, snap the search to a configurable grid, and permit only the exact access cell to escape imperfect stacked source data. |
| Route guides | Use only an explicitly authored `routing_connections[].guide`. Archived deleted-wire interiors are ignored and cannot leak a reference solution into routing. |
| Track assignment | Try a legal octilinear guide/pattern route before invoking maze search. |
| Initial detailed route | Route the densest pin neighborhoods first (or choose faster short-route-first ordering), using 8-neighbor A*. |
| DRC model | Inflate traces by half-width plus clearance; block foreign-net pads, vias, traces, pours, and wire keepouts. Same-net copper remains accessible. |
| Search and repair | Identify foreign-net trace-spacing conflicts, rip up implicated routes, and reroute with increasing present and historical congestion costs. Retain the best iteration. |
| Final legalization | Keep a conflict-free subset, retry removed connections with hard avoidance, try small route swaps, and reject any remaining geometric intersections. An optional reverse-direction pin escape can be enabled programmatically. |

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

## Search model

A state is a grid cell. Its eight edges produce horizontal, vertical, and 45-degree trace segments. A* uses octile distance as its heuristic. The incremental cost is:

```text
movement + bend_penalty
         + present_factor * conflicting_routes
         + history_factor * prior_violation_count
         + optional_guide_factor * distance_from_explicit_guide
```

Fixed geometry is a hard constraint. Already-routed traces are negotiated congestion: crossing them is costly rather than initially impossible, allowing a complete candidate solution to be constructed before repair. Each repair pass increases the present cost, increments history around violations, and keeps the lowest-conflict solution seen.

Output polylines retain exact source/target coordinates, collapse collinear grid points, and are tagged with `route_type: "autoroute"` and `autoroute_job`. When two same-net requests share a grid edge, that copper is emitted once. Output may therefore contain several fragments for one request, or no new fragment when its entire path is already conductive. `autoroute_jobs` on a shared fragment records all covered request IDs; `report.routed` counts connected requests rather than wire records.

## Test fixture result

With grid spacing 2, clearance 2, six passes, dense-first ordering, and routing guides disabled, the supplied testcase produces 199 routed connections and 21 explicitly unrouted connections using file 03 alone. The verification run reported zero trace-clearance conflict cells, and an independent emitted-geometry audit found zero foreign-net crossings and zero positive-length same-net segment overlaps. The 199 reported connections were also checked against the emitted copper graph. This run took about 125 seconds in this workspace; runtime varies with machine load. Short-route-first ordering routes 194 connections. The search does not draw missed connections as crossed traces. A smaller grid can improve geometric resolution at a substantial runtime/memory cost.

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
