# PCB detailed-router design

## Scope

The router in `pcb_viewer/router.py` is a single-signal-layer PCB detailed router inspired by TritonRoute's architecture. It is not a source port: TritonRoute targets LEF/DEF IC routing grids and a foundry rule deck, while this application has JSON PCB geometry and a compact clearance rule.

The corresponding TritonRoute/OpenROAD stages are documented in the official [OpenROAD detailed-routing documentation](https://github.com/The-OpenROAD-Project/OpenROAD/blob/master/src/drt/README.md): pin access analysis, track assignment, initial detailed routing, search and repair, and DRC. The PCB router maps those ideas as follows.

| Routing stage | PCB implementation |
| --- | --- |
| Pin access | Preserve exact endpoints, snap the search to a configurable grid, and permit only the exact access cell to escape imperfect stacked source data. |
| Route guides | Use `routing_connections[].guide`, or coarsen archived deleted polylines with Douglas–Peucker. Guides affect topology/cost but are not copied to output. |
| Track assignment | Try a legal octilinear guide/pattern route before invoking maze search. |
| Initial detailed route | Route wider and longer connections first with 8-neighbor A*. |
| DRC model | Inflate traces by half-width plus clearance; block foreign-net pads, vias, traces, pours, and wire keepouts. Same-net copper remains accessible. |
| Search and repair | Identify foreign-net trace-spacing conflicts, rip up implicated routes, and reroute with increasing present and historical congestion costs. Retain the best iteration. |

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

`guide` is optional. The supplied `03第三层右侧删去线网（布线输入）.json` uses `deleted_connections.wires`; each archived record supplies the two-terminal request, width, net, layer, and a route guide.

Geometry is read from `pads`, `pins`, `vias`, `wires`, `copper_polygons`, and `keepouts`. Clearance defaults to `metadata.design_rules.clearance_mil`. Missing via/pad shapes use the same fallback radii as the viewer.

## Search model

A state is a grid cell. Its eight edges produce horizontal, vertical, and 45-degree trace segments. A* uses octile distance as its heuristic. The incremental cost is:

```text
movement + bend_penalty
         + present_factor * conflicting_routes
         + history_factor * prior_violation_count
         + guide_factor * distance_from_coarse_guide
```

Fixed geometry is a hard constraint. Already-routed traces are negotiated congestion: crossing them is costly rather than initially impossible, allowing a complete candidate solution to be constructed before repair. Each repair pass increases the present cost, increments history around violations, and keeps the lowest-conflict solution seen.

Output polylines retain exact source/target coordinates, collapse collinear grid points, and are tagged with `route_type: "autoroute"` and `autoroute_job`.

## Test fixture result

With grid spacing 2, clearance 2, and six passes, the supplied testcase routes all 220 requested connections. The deterministic verification run completed in about 18 seconds in this workspace and reported 28 approximate trace-clearance conflict cells across 20 jobs. These are deliberately reported, not hidden. A smaller grid can improve geometric resolution at a substantial runtime/memory cost.

## Limitations and next steps

- One routing layer per run; there is no via selection or layer assignment.
- The DRC is grid/raster based. It covers width/clearance and the supported shapes, not a full fabrication rule deck.
- No differential-pair coupling, length matching, impedance, neck-down, thermal, or current-density rules.
- Copper-pour connectivity is treated geometrically; there is no post-route electrical graph/LVS pass yet.
- Search is synchronous in the GUI. It remains cancelable between connections, but a worker thread would improve responsiveness for very fine grids.
- Dense boards should eventually use tiled workers and localized DRC markers, closer to TritonRoute's scalable worker model.

Always inspect status-2 results and run the target CAD/fabrication DRC before manufacturing.
