import pytest

from pcb_viewer.pipelines import RoutingPipeline
from pcb_viewer.repulsion import PASSES, _RepulsionField, _apply_batch, _fit_spans
from pcb_viewer.router import Autorouter, RouterConfig, RoutingJob


def parallel_routes(*, keepouts=(), separation=4, length=30, **options):
    board = {'layer': 'TOP', 'routing_connections': [
        {'net': net, 'layer': 'TOP', 'start': [0, y], 'end': [length, y], 'width': 1}
        for net, y in [('A', 0), ('B', separation)]
    ], 'keepouts': list(keepouts)}
    config = RouterConfig(grid_step=1, clearance=1, boundary_margin=16,
                          force_iterations=0, force_max_jobs=0, attraction_strength=2,
                          pipeline=RoutingPipeline('Spread traces', ('repulsion',)), **options)
    router = Autorouter(board, config)
    for job in router.jobs:
        router._commit(job, router._grid_line(router._cell(job.start), router._cell(job.goal)))
    return router


def test_repulsion_moves_parallel_traces_together_and_keeps_endpoints():
    router = parallel_routes()
    observations = []

    def preview(active, _done, _total, _message):
        lower, upper = (active.routes[i] for i in range(2))
        assert len(lower) == len(upper)
        assert [(x, active._cell((0, 4))[1] + active._cell((0, 0))[1] - y) for x, y in lower] == upper
        assert not active._conflicts()[0]
        observations.append(min(active._point(cell)[1] for cell in lower))

    wires, report = router.route(preview=preview)
    assert observations[0] == 0 and observations[-1] < 0
    for job, wire in zip(router.jobs, wires):
        assert wire['points'][0] == list(job.start)
        assert wire['points'][-1] == list(job.goal)
    assert report.clean and report.repulsion_traces_moved == 2 and report.repulsion_passes > 0
    assert report.force_iterations_run == 0
    assert [row['stage'] for row in report.stage_results] == ['repulsion']


def test_repulsion_pushes_across_a_wider_gap_into_open_space():
    router = parallel_routes(separation=17, length=64)
    _, report = router.route()
    lower, upper = (router.routes[i] for i in range(2))
    assert min(router._point(cell)[1] for cell in lower) <= -5
    assert max(router._point(cell)[1] for cell in upper) >= 22
    assert report.clean and report.repulsion_traces_moved == 2


@pytest.mark.parametrize('source', ['pins', 'pads', 'vias', 'unrouted terminal'])
def test_terminals_do_not_exert_repulsion(source):
    board = {'layer': 'TOP', 'routing_connections': [
        {'net': 'A', 'layer': 'TOP', 'start': [0, 0], 'end': [64, 0], 'width': 1}
    ]}
    pin = {'layer': 'TOP', 'net': 'B', 'x': 32, 'y': 12,
           'shape': {'shape': 'circle', 'radius': 3}}
    if source == 'unrouted terminal':
        board['routing_connections'].append(
            {'layer': 'TOP', 'net': 'B', 'start': [24, 12], 'end': [40, 12], 'width': 1})
    else:
        board[source] = [pin]
    router = Autorouter(board, RouterConfig(grid_step=1, clearance=1, boundary_margin=24,
                        pipeline=RoutingPipeline('Spread traces', ('repulsion',))))
    job = router.jobs[0]
    straight = router._grid_line(router._cell(job.start), router._cell(job.goal))
    router._commit(job, straight)

    wires, report = router.route()
    assert report.routed == 1 and report.conflict_cells == 0
    assert wires[0]['points'][0] == list(job.start) and wires[0]['points'][-1] == list(job.goal)
    assert router.routes[job.id] == straight


def test_repulsion_releases_short_trace_between_its_pads():
    router = parallel_routes(separation=7, length=8)
    router.data['pads'] = [
        {'x': x, 'y': 0, 'net': 'A', 'layer': 'TOP', 'shape': {'shape': 'circle', 'radius': 3.2}}
        for x in (0, 8)
    ]
    router._rasterize_board()
    original = router.routes[0]
    wires, report = router.route()
    assert router.routes[0] != original
    assert wires[0]['points'][0] == [0, 0] and wires[0]['points'][-1] == [8, 0]
    assert min(y for _, y in wires[0]['points']) < 0
    assert report.clean


def test_repulsion_retains_distinct_same_net_contacts():
    router = parallel_routes(separation=7, length=40)
    router.data['vias'] = [
        {'x': x, 'y': 0, 'net': 'A', 'layer': 'TOP', 'shape': {'shape': 'circle', 'radius': 0.7}}
        for x in (20, 22)
    ]
    router._rasterize_board()
    original = router.routes[0]
    _, report = router.route()
    assert router.routes[0] != original and report.clean
    assert all(router._cell((x, 0)) in router.routes[0] for x in (20, 22))


def test_repulsion_settles_a_weak_diagonal_move(monkeypatch):
    board = {'layer': 'TOP', 'routing_connections': [
        {'net': 'A', 'start': [0, 0], 'end': [4, 4], 'width': 1},
    ]}
    router = Autorouter(board, RouterConfig(grid_step=1, clearance=1,
                        pipeline=RoutingPipeline('Spread traces', ('repulsion',))))
    job = router.jobs[0]
    original = router._grid_line(router._cell(job.start), router._cell(job.goal))
    router._commit(job, original)
    # Neither axis accumulates half a cell, but a diagonal step follows the field.
    monkeypatch.setattr(_RepulsionField, 'force', lambda *_args: (0.03, -0.03))
    wires, report = router.route()
    assert report.clean and report.repulsion_passes == PASSES + 1
    assert router.routes[0] != original
    assert wires[0]['points'][0] == [0, 0] and wires[0]['points'][-1] == [4, 4]


def test_repulsion_keeps_shared_junction_and_fixed_copper():
    board = {'layer': 'TOP', 'routing_connections': [
        {'net': 'A', 'layer': 'TOP', 'start': [0, 0], 'end': [40, 0], 'width': 1},
        {'net': 'A', 'layer': 'TOP', 'start': [20, 0], 'end': [20, 14], 'width': 1},
    ], 'wires': [{'net': 'fixed', 'layer': 'TOP', 'width': 1, 'points': [[0, 4], [16, 4]]}]}
    router = Autorouter(board, RouterConfig(grid_step=1, clearance=0, repulsion_strength=2,
                        pipeline=RoutingPipeline('Spread traces', ('repulsion',))))
    for job in router.jobs:
        router._commit(job, router._grid_line(router._cell(job.start), router._cell(job.goal)))
    starting = dict(router.routes)
    _, report = router.route()
    junction = router._cell((20, 0))
    assert router.routes[0] != starting[0]
    assert junction in router.routes[0] and router.routes[1][0] == junction
    assert report.clean and not report.wire_updates


def test_repulsion_checks_both_proposals_in_the_same_board_state():
    router = parallel_routes()
    starting = dict(router.routes)

    def jog(job_id, line):
        return _fit_spans(router, router.jobs_by_id[job_id], starting[job_id], [0, 8, 22, 30],
                          {8: router._cell((8, line)), 22: router._cell((22, line))})

    # A can move into B's old space when B moves away in the same batch.
    lower, upper = jog(0, 4), jog(1, 8)
    assert router._path_blockers(router.jobs[0], lower.path) == {1}
    assert _apply_batch(router, starting, {0: lower, 1: upper}) == {0, 1}
    assert not router._conflicts()[0]
    assert not _apply_batch(router, starting, {0: jog(0, 2), 1: jog(1, 2)})
    assert router.routes == starting

    # The guard blocks the upper move. As the upper trace returns to its old
    # corridor, the lower move must be checked against it again.
    guard = RoutingJob(2, 'C', (0, 8), (30, 8), 1, 'TOP')
    router.jobs_by_id[guard.id] = guard
    router._commit(guard, router._grid_line(router._cell(guard.start), router._cell(guard.goal)))
    starting = dict(router.routes)
    assert not _apply_batch(router, starting, {0: jog(0, 4), 1: jog(1, 8)})
    assert router.routes == starting


def test_repulsion_preserves_blocked_span_while_free_spans_move():
    router = parallel_routes(keepouts=[{'layer': 'TOP', 'type': 'wire_keepout',
                                        'points': [[2, -2], [12, -2], [12, -7], [2, -7]]}])
    _, report = router.route()
    lower = {router._point(cell) for cell in router.routes[0]}
    assert (6, 0) in lower
    assert any(x > 15 and y < 0 for x, y in lower)
    assert report.clean and report.repulsion_traces_moved == 2


def test_repulsion_preserves_local_clearance_when_another_route_blocks_a_span():
    router = parallel_routes()
    starting = dict(router.routes)
    indices = [0, 4, 8, 12, 18, 22, 26, 30]
    lower = _fit_spans(router, router.jobs[0], starting[0], indices, {
        8: router._cell((8, 4)), 22: router._cell((22, -3)),
    })
    assert _apply_batch(router, starting, {0: lower}) == {0}
    assert router._cell((8, 0)) in router.routes[0]
    assert router._cell((22, -3)) in router.routes[0]
    assert not router._path_blockers(router.jobs[0], router.routes[0])


def test_repulsion_disabled_and_cancelled_runs_preserve_routes():
    disabled = parallel_routes(repulsion_strength=0)
    starting = dict(disabled.routes)
    _, report = disabled.route()
    assert disabled.routes == starting and report.repulsion_passes == 0

    router = parallel_routes()
    starting = dict(router.routes)

    def cancel(_done, _total, message):
        if message == 'Repulsion pass 2':
            assert router.routes != starting
            return False
        return True

    _, report = router.route(progress=cancel)
    assert report.cancelled and report.repulsion_traces_moved == 0
    assert router.routes == starting
