import json
from pathlib import Path
import subprocess
import sys

import pytest

from pcb_viewer.model import Document
from pcb_viewer.router import Autorouter, RouterConfig, extract_jobs


def board_json(**fields):
    return {
        'layerCount': 2, 'minTraceWidth': 0.15, 'defaultObstacleMargin': 0,
        'bounds': {'minX': 0, 'maxX': 10, 'minY': 0, 'maxY': 10},
        'obstacles': [], 'connections': [], **fields,
    }


def test_imports_all_default_benchmark_inputs():
    samples = sorted(Path('pcb_example/simple_route_json').glob('*.simple-route.json'))
    assert len(samples) == 85
    for path in samples:
        document = Document.load(path)
        assert document.data['simple_route_json_source'] == json.loads(path.read_bytes())
        assert extract_jobs(document.data), path


def test_import_preserves_preloaded_copper_widths_and_layer_access(tmp_path):
    source = tmp_path / 'input.json'
    source.write_text(json.dumps(board_json(
        layerCount=4,
        connections=[{'name': 'N', 'nominalTraceWidth': 0.2, 'pointsToConnect': [
            {'x': 2, 'y': 3, 'layers': ['top', 'bottom'], 'pointId': 'A'},
            {'x': 5, 'y': 3, 'layer': 'bottom', 'pointId': 'B'},
        ]}],
        obstacles=[{'type': 'rect', 'center': {'x': 2, 'y': 3}, 'layers': ['top', 'bottom'],
                    'width': 0.4, 'height': 0.2, 'ccwRotationDegrees': 90, 'connectedTo': ['A']}],
        traces=[{'pcb_trace_id': 'trace0', 'connection_name': 'N', 'route': [
            {'route_type': 'wire', 'x': 2, 'y': 3, 'layer': 'top', 'width': 0.2},
            {'route_type': 'wire', 'x': 3, 'y': 3, 'layer': 'top', 'width': 0.2},
            {'route_type': 'via', 'x': 3, 'y': 3, 'from_layer': 'top', 'to_layer': 'inner1',
             'via_diameter': 0.5},
        ]}],
    )))
    document = Document.load(source)
    router = Autorouter(document.data)
    assert router.layer == 'bottom'
    assert router.jobs[0].width == pytest.approx(0.2 / 0.0254)
    assert document.data['pads'][0]['net'] == 'N'
    assert document.data['pads'][0]['shape']['shape'] == 'polygon'
    assert document.data['wires'][0]['locked']
    assert document.data['vias'][0]['layers'] == ['top', 'inner1', 'inner2', 'bottom']
    assert document.data['vias'][0]['shape']['radius'] == pytest.approx(0.25 / 0.0254)
    with pytest.raises(ValueError, match='as a new PCB Studio board'):
        document.save()


def test_routing_cannot_detour_outside_imported_bounds(tmp_path):
    path = tmp_path / 'bounded.json'
    path.write_text(json.dumps(board_json(
        connections=[{'name': 'N', 'pointsToConnect': [
            {'x': 3, 'y': 5, 'layer': 'top'}, {'x': 7, 'y': 5, 'layer': 'top'},
        ]}],
        obstacles=[{'type': 'rect', 'center': {'x': 5, 'y': 5}, 'layers': ['top'],
                    'width': 1, 'height': 10, 'connectedTo': []}],
    )))
    router = Autorouter(Document.load(path).data, RouterConfig(grid_step=10, stages=('initial',)))
    wires, report = router.route()
    assert not wires
    assert report.routed == 0 and report.jobs_total == 1


def test_cli_routes_two_layers_without_overwriting_the_source_or_first_layer(tmp_path):
    def connection(name, first, second, y):
        return {'name': name, 'pointsToConnect': [
            {'x': 2, 'y': y, 'layer': first}, {'x': 8, 'y': y, 'layer': second},
        ]}

    source = {'default': {'simpleRouteJson': board_json(connections=[
        connection('N1', 'top', 'top', 2),
        connection('N2', 'bottom', 'bottom', 4),
        connection('N3', 'top', 'bottom', 6),
    ])}}
    path = tmp_path / 'input.json'
    path.write_text(json.dumps(source))

    def route(input_path, output_path, *options):
        result = subprocess.run([sys.executable, '-m', 'pcb_viewer', str(input_path), '--autoroute',
                                 '--output', str(output_path), '--stages', 'initial', *options],
                                capture_output=True, text=True, check=False)
        assert result.returncode == 2, result.stderr
        report = json.loads(result.stdout)
        assert report['routed'] == report['jobs_total'] == 1
        assert report['unsupported_connections'][0]['net'] == 'N3'
        return report

    first = tmp_path / 'top.json'
    second = tmp_path / 'both.json'
    assert route(path, first)['layer'] == 'top'
    assert route(first, second, '--layer', 'bottom')['layer'] == 'bottom'
    saved = Document.load(second).data
    assert {wire['layer'] for wire in saved['wires']} == {'top', 'bottom'}
    assert saved['simple_route_json_source'] == json.loads(path.read_text()) == source
