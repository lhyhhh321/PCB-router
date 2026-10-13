import copy
import json
from pathlib import Path
import sys

import pytest

from pcb_viewer.router import Autorouter, RouterConfig, autoroute_data


def board():
    return {'layer': 'TOP', 'wires': [], 'routing_connections': [
        {'net': f'N{i}', 'layer': 'TOP', 'width': 1,
         'start': [0, i * 20], 'end': [20, i * 20]} for i in range(2)
    ]}


def partial_board():
    data = board()
    wires, _ = autoroute_data(data, RouterConfig(stages=('initial',)))
    data['wires'] = [wire for wire in wires if wire['autoroute_job'] == 0]
    return data


def test_resume_routes_only_missing_requests_and_does_not_mutate_input(monkeypatch):
    data = partial_board()
    original = copy.deepcopy(data)
    router = Autorouter(data, RouterConfig(resume=True, stages=('initial',)))
    assert set(router.routes) == {0}
    assert not router.static  # Imported copper is movable, not duplicated as obstacles.
    called = []
    route_job = router._route_job
    def observe(job, *args):
        called.append(job.id)
        return route_job(job, *args)
    monkeypatch.setattr(router, '_route_job', observe)
    wires, report = router.route()
    assert called == [1]
    assert report.clean and report.routed == 2 and report.resumed_connections == 1
    assert report.stage_results[0]['before']['routed'] == 1
    assert len(wires) == 2 and data == original


def test_resume_reassembles_shared_fragments():
    data = {'layer': 'TOP', 'routing_connections': [
        {'net': 'N', 'layer': 'TOP', 'width': 1, 'start': [0, 0], 'end': [10, 0]},
        {'net': 'N', 'layer': 'TOP', 'width': 1, 'start': [0, 0], 'end': [20, 0]},
    ]}
    wires, _ = autoroute_data(data, RouterConfig(stages=('initial',)))
    data['wires'] = wires
    resumed, report = autoroute_data(data, RouterConfig(resume=True, stages=()))
    assert report.routed == 2 and report.resumed_connections == 2
    assert report.wire_length == 20 and resumed == wires


def test_incomplete_saved_connection_is_not_counted_as_connected():
    data = partial_board()
    data['wires'][0]['points'] = [[0, 0], [19.9, 0]]
    with pytest.raises(ValueError, match='do not connect both terminals'):
        Autorouter(data, RouterConfig(resume=True))


def test_mismatched_saved_ids_are_rejected():
    data = partial_board()
    data['wires'][0]['autoroute_jobs'] = [9999]
    with pytest.raises(ValueError, match='do not match'):
        Autorouter(data, RouterConfig(resume=True))


def test_resume_preserves_off_grid_terminal_geometry():
    data = {'layer': 'TOP', 'routing_connections': [
        {'net': 'N', 'layer': 'TOP', 'width': 1, 'start': [0.1, 1], 'end': [10, 0]},
    ], 'wires': [{'net': 'N', 'layer': 'TOP', 'width': 1,
                  'route_type': 'autoroute', 'autoroute_job': 0, 'autoroute_jobs': [0],
                  'points': [[0.1, 1], [2, 0], [10, 0]]}]}
    wires, report = autoroute_data(data, RouterConfig(resume=True, stages=()))
    assert wires == data['wires'] and report.routed == 1


def test_cli_resume_replaces_generated_wires_without_duplication(tmp_path, monkeypatch):
    from pcb_viewer.startup import main
    data = partial_board()
    fixed = {'net': 'FIXED', 'layer': 'TOP', 'width': 1, 'points': [[0, 100], [20, 100]]}
    other_layer = {**data['wires'][0], 'layer': 'BOTTOM'}
    data['wires'].extend([fixed, other_layer])
    source, output = tmp_path / 'input.json', tmp_path / 'output.json'
    source.write_text(json.dumps(data))
    monkeypatch.setattr(sys, 'argv', ['pcb_viewer', str(source), '--autoroute', '--resume',
                                    '--stages', 'initial', '--output', str(output)])
    assert main() == 0
    saved = json.loads(output.read_text())
    assert len(saved['wires']) == 4
    assert fixed in saved['wires'] and other_layer in saved['wires']
    assert saved['autoroute_report']['resumed_connections'] == 1
    assert json.loads(source.read_text()) == data


def test_route140_import_preserves_all_existing_wires():
    path = Path('pcb_example/route140.json')
    if not path.exists():
        pytest.skip('User-provided partial-board fixture is not installed')
    data = json.loads(path.read_text())
    wires, report = autoroute_data(data, RouterConfig(resume=True, stages=()))
    assert report.resumed_connections == 206 and report.routed == 206
    assert report.jobs_total == 220 and len(report.failed) == 14
    assert report.conflict_cells == 0
    assert {w['autoroute_job']: w for w in wires} == {w['autoroute_job']: w for w in data['wires']}
