"""Differential tests against the original scalar congestion and maze search."""
import random

import pytest

from pcb_viewer.router import Autorouter, RouterConfig, autoroute_data


torch = pytest.importorskip('torch')
DEVICES = ['cpu'] + (['mps'] if torch.backends.mps.is_available() else []) + (['cuda'] if torch.cuda.is_available() else [])


def board():
    return {'layer': 'TOP', 'routing_connections': [
        {'net': net, 'layer': 'TOP', 'width': width, 'start': [0, i * 4], 'end': [40, i * 4]}
        for i, (net, width) in enumerate([('A', 1), ('B', 3), ('B', 2), ('C', 0.5), ('A', 2)])]}


@pytest.mark.parametrize('device', DEVICES)
@pytest.mark.parametrize('step', [1.0, 0.7, 2.0])
def test_fields_match_scalar_with_overlap_history_edges_and_rollback(device, step):
    r = Autorouter(board(), RouterConfig(grid_step=step, boundary_margin=5, clearance=1.1,
                                        compute_backend='torch-' + device))
    rng = random.Random(7)
    for job in r.jobs[1:]:
        # Deliberate shared/overlapping cells and repeated cells in each route:
        # congestion counts distinct foreign JOBS, not cells or nets.
        cells = [(rng.randrange(r.cols), rng.randrange(r.rows)) for _ in range(50)]
        cells += [(4, 4)] * 3 + [(5, 4), (6, 4)]
        r._commit(job, cells)
    r.history = {r._key((rng.randrange(r.cols), rng.randrange(r.rows))): rng.randrange(1, 9)
                 for _ in range(70)}
    snapshot = {key: list(path) for key, path in r.routes.items()}
    for bounds in [(0, r.cols - 1, 0, r.rows - 1), (2, 13, 3, 15)]:
        for action in ['initial', 'remove', 'restore']:
            if action == 'remove':
                r._remove(1)
                r.history[r._key((5, 5))] = 17
            elif action == 'restore':
                r._restore_routes(snapshot)
            counts, history = r.torch_costs.build(r, r.jobs[0], bounds)
            x1, x2, y1, y2 = bounds
            expected = [r._dynamic_cost_parts((x, y), r.jobs[0])
                        for y in range(y1, y2 + 1) for x in range(x1, x2 + 1)]
            assert list(zip(counts, history)) == expected


@pytest.mark.parametrize('device', DEVICES)
@pytest.mark.parametrize('hard', [False, True])
def test_astar_exact_path_parity_and_fresh_snapshot(device, hard):
    b = board()
    b['keepouts'] = [{'layer': 'TOP', 'points': [[18, -5], [22, -5], [22, 12], [18, 12]]}]
    ref = Autorouter(b, RouterConfig(grid_step=1, clearance=1, boundary_margin=20))
    accelerated = Autorouter(b, RouterConfig(grid_step=1, clearance=1, boundary_margin=20,
                                            compute_backend='torch-' + device))
    for r in (ref, accelerated):
        r._commit(r.jobs[1], [(x, 38) for x in range(10, 60)])
        r.history = {r._key((x, 30)): 5 for x in range(5, 65)}
    bounds = (0, ref.cols - 1, 0, ref.rows - 1)
    for remove in [False, True]:
        if remove:
            for r in (ref, accelerated):
                r._remove(1)
                r.history.clear()
        assert accelerated._astar(accelerated.jobs[0], bounds, 24, hard) == ref._astar(ref.jobs[0], bounds, 24, hard)
    assert accelerated.torch_costs.fields_built > 0


@pytest.mark.parametrize('device', DEVICES)
def test_complete_route_parity(device):
    b = board()
    options = dict(grid_step=1, boundary_margin=20, clearance=1, iterations=2,
                   force_iterations=1, detailed_max_passes=2)
    wires, report = autoroute_data(b, RouterConfig(**options))
    actual, accelerated = autoroute_data(b, RouterConfig(**options, compute_backend='torch-' + device))
    assert actual == wires
    assert (accelerated.routed, accelerated.conflict_cells, accelerated.wire_length, accelerated.failed) == (
        report.routed, report.conflict_cells, report.wire_length, report.failed)


def test_field_memory_budget_falls_back():
    r = Autorouter(board(), RouterConfig(compute_backend='torch-cpu'))
    r.torch_costs.MAX_FIELD_CELLS = 1
    assert r.torch_costs.build(r, r.jobs[0], (0, 10, 0, 10)) is None
    assert r.torch_costs.fields_skipped == 1


def test_invalid_backend():
    with pytest.raises(ValueError, match='backend'):
        RouterConfig(compute_backend='invalid').validate()


def test_ambiguous_float_distance_uses_scalar_fallback():
    r = Autorouter(board(), RouterConfig(grid_step=1, clearance=1e-7, boundary_margin=5,
                                        compute_backend='torch-cpu'))
    # A width-1 and width-3 pair has an exact threshold of two grid cells.
    # Scalar subtraction must decide the boundary, not rounded GPU arithmetic.
    r._commit(r.jobs[1], [(5, 5), (6, 5)])
    assert r.torch_costs.build(r, r.jobs[0], (0, 10, 0, 10)) is None


def test_missing_torch_is_actionable(monkeypatch):
    import sys
    from pcb_viewer.torch_costs import TorchCosts
    monkeypatch.setitem(sys.modules, 'torch', None)
    with pytest.raises(ValueError, match='requirements-torch.txt'):
        TorchCosts('cpu')


def test_unavailable_gpu_is_actionable(monkeypatch):
    from pcb_viewer.torch_costs import TorchCosts
    monkeypatch.setattr(torch.cuda, 'is_available', lambda: False)
    with pytest.raises(ValueError, match='CUDA.*unavailable'):
        TorchCosts('cuda')
    monkeypatch.setattr(torch.backends.mps, 'is_available', lambda: False)
    with pytest.raises(ValueError, match='MPS.*unavailable'):
        TorchCosts('mps')
    assert TorchCosts('auto').device == 'cpu'


@pytest.mark.parametrize('device', DEVICES)
def test_chunked_route_batches_and_large_integer_history(device):
    r = Autorouter(board(), RouterConfig(grid_step=1, boundary_margin=5, clearance=0.2,
                                        compute_backend='torch-' + device))
    for job in r.jobs[1:]:
        r._commit(job, [(x, 5) for x in range(3, 12)])
    r.history = {r._key((5, 5)): 2**35, r._key((6, 5)): 19}
    bounds = (0, 15, 0, 15)
    expected = [r._dynamic_cost_parts((x, y), r.jobs[0]) for y in range(16) for x in range(16)]
    r.torch_costs.MAX_ROUTE_CELLS = 256  # one foreign route per batch
    r.torch_costs.MAX_PAIRS = 100
    assert list(zip(*r.torch_costs.build(r, r.jobs[0], bounds))) == expected
    for job_id in list(r.routes):
        r._remove(job_id)
    counts, history = r.torch_costs.build(r, r.jobs[0], bounds)
    assert counts == [0] * 256
    assert max(history) > 2**35


def test_cli_selects_backend_and_reports_device(tmp_path):
    import json
    import subprocess
    import sys
    source = tmp_path / 'board.json'
    output = tmp_path / 'routed.json'
    data = board()
    data['wires'] = []
    data['routing_connections'] = data['routing_connections'][:1]
    source.write_text(json.dumps(data))
    result = subprocess.run([sys.executable, '-m', 'pcb_viewer', str(source),
                             '--autoroute', '--compute-backend', 'torch-cpu',
                             '--output', str(output)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    report = json.loads(output.read_text())['autoroute_report']
    assert report['config']['compute_backend'] == 'torch-cpu'
    assert report['config']['compute_device'] == 'cpu'
    assert report['routed'] == 1 and report['clean']
