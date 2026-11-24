# PCB Studio

PCB Studio is a Python and PySide6 desktop editor for PCB JSON files. It includes a single-layer autorouter, a routing pipeline editor, and support for tscircuit SimpleRouteJson.

## Run

Python 3.10 or newer and a desktop display are required.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pcb_viewer
```

On Windows, activate the environment with `.venv\Scripts\activate`. For launch problems, run `./scripts/run-python.sh --diagnose`. [Desktop requirements and diagnostics](STARTUP_FINDINGS.md)

## Edit and route a board

- Open one or more boards with **Ctrl+O**. Choose a visible layer and select geometry to edit its JSON properties.
- Choose **Rectangle** or **Polygon** to draw a keepout on the editing layer. Use `wire_keepout` to block routing on that layer.
- Choose **Auto route…** (**Ctrl+R**) to route the selected signal layer. Choose a pipeline, placement priority, grid spacing, and clearance. Use **Show unrouted connections (dashed)** to see missing connections.
- Use **Continue existing routing** to resume a board saved with its generated routes. A completed routing run is one undoable edit.

## Routing pipelines

Use **Pipelines** in the top toolbar to create or duplicate a pipeline. Drag blocks into **Execution order** and drag them back to **Available blocks** to remove them. **Save** writes a JSON file under `pipelines/`, so the pipeline travels with the project when committed to Git. The project includes Pipeline 1, Pipeline 2, and Pipeline 3. [Saved pipelines](pipelines/)

Choose a saved pipeline in **Auto route…** or pass its file to the CLI. The default is Pipeline 1. Add `--resume` to continue saved routing.

```bash
python -m pcb_viewer input.json --autoroute \
  --pipeline pipelines/pipeline-3.json --output routed.json
```

The CLI writes an `autoroute_report` with completion, conflicts, and stage results. Exit code `0` indicates complete routing, `2` indicates output with incomplete routing or conflicts, and `1` indicates an input or runtime error. [Routing stages and configuration](ROUTER_DESIGN.md#pipeline-configuration-and-measurements)

## SimpleRouteJson

Open a SimpleRouteJson file directly. The importer converts millimeters to mils, reads the board geometry, and converts multi-terminal nets into routing requests. Choose a layer in the viewer or pass `--layer` to the CLI. Requests that need a layer transition or off-board routing appear under `unsupported_connections`. Save the result as a new PCB Studio JSON file.

```bash
python -m pcb_viewer pcb_example/simple_route_json/circuit001.simple-route.json
```

The bundled examples come from the tscircuit routing dataset. [Examples](pcb_example/simple_route_json/) · [MIT license](pcb_example/simple_route_json/LICENSE) · [Dataset source](https://github.com/tscircuit/autorouting-dataset-01/tree/b97f5052a2359ab2da3f186765c6a5e839535efb/lib/dataset)

## Optional backends

- **PyTorch** accelerates congestion calculations for the built-in router. Install with `python -m pip install -r requirements-torch.txt`, then select a **Congestion calculation** backend or pass `--compute-backend torch-auto`.
- **Freerouting** runs as an external Java router. Select **Freerouting benchmark** in **Auto route…** and provide a JAR and a compatible Java runtime. [Freerouting releases](https://github.com/freerouting/freerouting/releases)

## Tests

```bash
python -m pip install pytest
QT_QPA_PLATFORM=offscreen python -m pytest -q
```
