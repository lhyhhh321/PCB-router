import json

import pytest

from pcb_viewer.pipelines import PIPELINE_1, RoutingPipeline
from pcb_viewer.router import Autorouter, ROUTING_STAGES, RouterConfig, autoroute_data


@pytest.fixture
def board():
    return {'layer': 'TOP', 'wires': [], 'routing_connections': [
        {'net': f'N{i}', 'layer': 'TOP', 'width': 1,
         'start': [0, i * 10], 'end': [20, i * 10]} for i in range(3)
    ]}


def config(**kwargs):
    return RouterConfig(grid_step=2, clearance=0, boundary_margin=10,
                        early_refine_interval=1, **kwargs)


@pytest.mark.parametrize('stages', ['initial', ('typo',), ('initial', 'initial'), ([],)])
def test_invalid_selection_is_rejected(stages):
    with pytest.raises(ValueError, match='Routing stages'):
        RouterConfig(stages=stages).validate()


def test_explicit_all_preserves_default_output(board):
    default_wires, default = autoroute_data(board, config())
    explicit_wires, explicit = autoroute_data(board, config(stages=tuple(ROUTING_STAGES)))
    assert default_wires == explicit_wires
    assert (default.routed, default.conflict_cells, default.wire_length) == (
        explicit.routed, explicit.conflict_cells, explicit.wire_length)
    assert default.config['enabled_stages'] == list(ROUTING_STAGES)
    assert default.early_refinement_passes == 2


def test_pipeline_order_and_repeated_blocks_change_routing(board, monkeypatch):
    monkeypatch.setattr(Autorouter, '_excessive_detour', lambda *_: True)
    base = ('detour_guard', 'initial', 'detour_guard')
    _, removed = autoroute_data(board, config(pipeline=RoutingPipeline('Remove', base)))
    _, kept = autoroute_data(board, config(pipeline=RoutingPipeline('Keep', (
        'detour_guard', 'detour_guard', 'initial'))))
    assert [entry['stage'] for entry in removed.stage_results] == list(base)
    assert removed.config['pipeline_blocks'] == list(base)
    assert removed.routed == 0 and kept.routed == 3


def test_place_ripup_repairs_blocked_connection_before_placing_next_job():
    board = {'layer': 'TOP', 'routing_connections': [
        {'net': net, 'layer': 'TOP', 'width': 1, 'start': start, 'end': end}
        for net, start, end in (('A', [0, 0], [20, 0]), ('B', [10, 0], [10, 30]),
                                ('C', [30, 40], [65, 40]))
    ]}
    # Isolate rip-up by letting the first placement occupy B's terminal region.
    _, separate = autoroute_data(board, config(ordering='short', stages=('initial', 'repair'),
                                               unrouted_pin_cost=0))
    messages = []

    def crossing_free_preview(router, *_):
        assert not router._geometric_crossings(router._unique_wire_runs())
        assert not any(router._path_has_crossing(router.jobs_by_id[job], path)
                       for job, path in router.routes.items())

    wires, merged = autoroute_data(
        board, config(ordering='short', pipeline=RoutingPipeline('Adaptive', ('place_ripup',)),
                      unrouted_pin_cost=0),
        progress=lambda _done, _total, message: messages.append(message.removeprefix('Round 1/1: ')),
        preview=crossing_free_preview,
    )
    assert separate.routed == 2 and separate.repair_iterations == 0
    assert merged.clean and merged.routed == 3 and merged.repair_iterations == 1
    assert wires[0]['points'] != [[0, 0], [20, 0]]
    assert [row['stage'] for row in merged.stage_results] == ['place_ripup']
    assert messages[:-1] == [f'Place & rip-up: {action}' for action in (
        'place A', 'place B', 'probe B', 'repair 1: A', 'place C',
    )]


@pytest.mark.parametrize('iterations, cancel, passes', [(1, False, 0), (3, False, 2), (3, True, 1)])
def test_place_ripup_restores_existing_routes_after_failed_or_cancelled_repair(iterations, cancel, passes):
    board = {'layer': 'TOP', 'routing_connections': [
        {'net': 'A', 'layer': 'TOP', 'width': 1, 'start': [0, 10], 'end': [20, 10]},
        {'net': 'B', 'layer': 'TOP', 'width': 1, 'start': [10, 0], 'end': [10, 20]},
    ]}
    config = RouterConfig(grid_step=2, clearance=0, boundary_margin=0, ordering='short',
                          iterations=iterations, pipeline=RoutingPipeline('Adaptive', ('place_ripup',)))
    wires, report = autoroute_data(board, config, progress=lambda _done, _total, msg:
                                   not (cancel and 'Place & rip-up: repair 1:' in msg))
    assert report.cancelled is cancel
    assert report.repair_iterations == passes
    assert report.conflict_cells == 0 and report.routed == 1
    assert [wire['points'] for wire in wires] == [[[0, 10], [20, 10]]]


@pytest.mark.parametrize('stage', ['repair', 'place_ripup'])
@pytest.mark.parametrize('fixed', [False, True])
def test_repair_blocks_rebuild_resumed_off_grid_crossing(stage, fixed):
    board = {'layer': 'TOP', 'routing_connections': [
        {'net': 'A', 'layer': 'TOP', 'width': 0.1, 'start': [0.2, 0.49], 'end': [0.49, 0.4]},
        {'net': 'B', 'layer': 'TOP', 'width': 0.1, 'start': [0, 0.75], 'end': [0.75, 0]},
    ]}
    board['wires'] = [
        {'net': job['net'], 'layer': 'TOP', 'width': job['width'], 'route_type': 'autoroute',
         'autoroute_job': index, 'points': [job['start'], job['end']]}
        for index, job in enumerate(board['routing_connections'])
    ]
    if fixed:
        board['routing_connections'].pop(0)
        board['wires'][0].pop('route_type')
        board['wires'][1]['autoroute_job'] = 0
    router = Autorouter(board, RouterConfig(grid_step=1, clearance=0, boundary_margin=2, resume=True,
                        hard_avoidance=False, present_congestion_cost=0,
                        pipeline=RoutingPipeline('Resume', (stage,))))
    assert not router._conflicts()[0]
    assert router._path_has_crossing(router.jobs_by_id[0], router.routes[0])

    def preview(active, *_):
        assert not any(active._path_has_crossing(active.jobs_by_id[job], path)
                       for job, path in active.routes.items())

    _, report = router.route(preview=preview)
    assert report.clean and report.routed == report.resumed_connections == (1 if fixed else 2)
    assert report.repair_iterations == 1


@pytest.mark.parametrize('stage', ['repair', 'place_ripup'])
@pytest.mark.parametrize('iterations', [1, 3])
def test_repair_blocks_leave_unavoidable_resumed_crossing_unrouted(stage, iterations):
    board = {'layer': 'TOP', 'routing_connections': [
        {'net': 'A', 'layer': 'TOP', 'width': 1, 'start': [0, 10], 'end': [20, 10]},
        {'net': 'B', 'layer': 'TOP', 'width': 1, 'start': [10, 0], 'end': [10, 20]},
    ]}
    board['wires'] = [
        {'net': job['net'], 'layer': 'TOP', 'width': job['width'], 'route_type': 'autoroute',
         'autoroute_job': index, 'points': [job['start'], job['end']]}
        for index, job in enumerate(board['routing_connections'])
    ]
    opts = RouterConfig(grid_step=2, clearance=0, boundary_margin=0, resume=True,
                        iterations=iterations, hard_avoidance=False, present_congestion_cost=0,
                        pipeline=RoutingPipeline('Resume', (stage,)))

    def preview(router, *_):
        assert not router._geometric_crossings(router._unique_wire_runs())

    wires, report = autoroute_data(board, opts, preview=preview)
    assert report.routed == len(wires) == 1 and report.resumed_connections == 2
    assert report.conflict_cells == 0 and len(report.failed) == 1


def test_detail_blocks_follow_pipeline_order_per_connection(board, monkeypatch):
    events = []
    astar = Autorouter._detailed_astar

    def record_astar(router, job):
        events.append((job.id, 'astar'))
        return astar(router, job)

    def record_drag(router, job):
        events.append((job.id, 'drag'))
        return False

    monkeypatch.setattr(Autorouter, '_detailed_astar', record_astar)
    monkeypatch.setattr(Autorouter, '_detailed_drag_blockers', record_drag)
    _, report = autoroute_data(board, config(pipeline=RoutingPipeline('Drag first', (
        'detailed_drag', 'detailed_astar'))))
    assert events == [(job, block) for job in range(3) for block in ('drag', 'astar')]
    assert report.detailed_passes == 2 and report.routed == 3


def test_pipeline_cli_loads_ordered_definition(board, tmp_path, monkeypatch):
    import sys
    from pcb_viewer.startup import main

    source = tmp_path / 'board.json'
    output = tmp_path / 'result.json'
    pipeline_file = tmp_path / 'pipeline.json'
    pipeline = RoutingPipeline('Crossings first', ('crossing_cleanup', 'initial'))
    source.write_text(json.dumps(board))
    pipeline_file.write_text(json.dumps(pipeline.to_dict()))
    monkeypatch.setattr(sys, 'argv', ['pcb_viewer', str(source), '--autoroute',
                                    '--pipeline', str(pipeline_file), '--output', str(output)])
    assert main() == 0
    report = json.loads(output.read_text())['autoroute_report']
    assert report['config']['pipeline_name'] == pipeline.name
    assert report['config']['pipeline_blocks'] == list(pipeline.blocks)
    assert PIPELINE_1.blocks.count('detour_guard') == 2
    pipeline_file.write_text(json.dumps({**pipeline.to_dict(), 'blocks': ['unknown']}))
    assert main() == 1


def test_initial_only_bypasses_optional_stages(board, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Disabled stage was called')
    for name in ('_refine_initial_routes', '_ripup_and_reroute', '_legalize_routes',
                 '_shove_unrouted', '_force_refine', '_detailed_route', '_excessive_detour'):
        monkeypatch.setattr(Autorouter, name, forbidden)
    _, report = autoroute_data(board, config(stages=('initial',)))
    assert report.clean and report.routed == 3
    assert [row['stage'] for row in report.stage_results] == ['initial']
    result = report.stage_results[0]
    assert result['before']['routed'] == 0
    assert result['after']['routed'] == 3
    assert result['after']['wire_length'] == report.wire_length
    assert result['elapsed_seconds'] >= 0
    assert report.round_results[0]['stage_results'] == report.stage_results
    json.dumps(report.to_dict())


def test_empty_selection_and_random_trials(board):
    wires, report = autoroute_data(board, config(stages=(), random_rounds=3))
    assert wires == [] and report.routed == 0 and len(report.failed) == 3
    assert not report.clean and report.stage_results == []
    assert report.config['enabled_stages'] == []
    assert len(report.round_results) == 3
    assert all(row['stage_results'] == [] for row in report.round_results)


def test_detailed_can_run_without_initial_or_legalization(board):
    _, report = autoroute_data(board, config(stages=('detailed',)))
    assert report.clean and report.detailed_new_connections == 3
    assert [row['stage'] for row in report.stage_results] == ['detailed']


@pytest.mark.parametrize('guided', [False, True])
def test_initial_rejects_unavoidable_crossings_without_legalization(guided):
    board = {'routing_connections': [
        {'net': 'A', 'layer': 'TOP', 'width': 1, 'start': [0, 10], 'end': [20, 10],
         'guide': [[0, 10], [20, 10]]},
        {'net': 'B', 'layer': 'TOP', 'width': 1, 'start': [10, 0], 'end': [10, 20],
         'guide': [[10, 0], [10, 20]]},
    ]}
    wires, report = autoroute_data(
        board, RouterConfig(grid_step=2, clearance=0, boundary_margin=0,
                            use_routing_guides=guided, hard_avoidance=False,
                            present_congestion_cost=0, stages=('initial', 'early_refinement'),
                            early_refine_interval=1, strict_final=False))
    assert len(wires) == report.routed == 1
    assert len(report.failed) == 1
    assert report.conflict_cells == 0
    assert report.stage_results[-1]['after']['conflict_cells'] == 0


@pytest.mark.parametrize('fixed', [False, True])
def test_initial_routes_around_crossings_hidden_by_terminal_snapping(fixed):
    start, end = [0.2, 0.49], [0.49, 0.4]
    board = {'routing_connections': [
        {'net': 'B', 'layer': 'TOP', 'width': 0.1, 'start': [0, 0.75], 'end': [0.75, 0]},
    ]}
    if fixed:
        board['wires'] = [{'net': 'A', 'layer': 'TOP', 'width': 0.1, 'points': [start, end]}]
    else:
        board['routing_connections'].insert(0, {
            'net': 'A', 'layer': 'TOP', 'width': 0.1, 'start': start, 'end': end})
    wires, report = autoroute_data(
        board, RouterConfig(grid_step=1, clearance=0, boundary_margin=0, stages=('initial',)))
    assert report.clean
    routed = next(wire for wire in wires if wire['net'] == 'B')
    assert routed['points'] == [[0, 0.75], [1, 1], [0.75, 0]]


def test_cancelled_initial_does_not_enter_later_stages(board):
    _, report = autoroute_data(board, config(), progress=lambda *_args: False)
    assert report.cancelled and report.routed == 0
    assert [row['stage'] for row in report.stage_results] == ['initial']
    assert report.stage_results[0]['cancelled']


def test_zero_limits_override_stage_selection(board):
    _, report = autoroute_data(board, config(stages=('combined', 'detailed', 'detour_guard'),
                                             force_iterations=0, detailed_max_passes=0,
                                             max_detour_ratio=0))
    assert report.stage_results == [] and report.routed == 0


def test_cli_selection_roundtrip(board, tmp_path, monkeypatch):
    import sys
    from pcb_viewer.startup import main
    source = tmp_path / 'input.json'
    output = tmp_path / 'output.json'
    source.write_text(json.dumps(board))
    monkeypatch.setattr(sys, 'argv', ['pcb_viewer', str(source), '--autoroute',
                                    '--stages', 'initial', '--output', str(output)])
    assert main() == 0
    saved = json.loads(output.read_text())['autoroute_report']
    assert saved['config']['enabled_stages'] == ['initial']
    assert [row['stage'] for row in saved['stage_results']] == ['initial']
    monkeypatch.setattr(sys, 'argv', ['pcb_viewer', str(source), '--autoroute',
                                    '--stages', '--output', str(output)])
    assert main() == 2
    assert json.loads(output.read_text())['autoroute_report']['routed'] == 0
    monkeypatch.setattr(sys, 'argv', ['pcb_viewer', str(source), '--autoroute',
                                    '--stages', 'typo'])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2


@pytest.mark.parametrize('final_cleanup', [False, True])
def test_removal_prefers_shortest_terminal_distance(monkeypatch, final_cleanup):
    board = {'routing_connections': [
        {'net': 'SHORT', 'layer': 'TOP', 'width': 1,
         'start': [10, 6], 'end': [10, 14]},
        {'net': 'LONG', 'layer': 'TOP', 'width': 1,
         'start': [0, 10], 'end': [20, 10]},
    ]}
    router = Autorouter(board, config(stages=('legalization',)))
    for job in router.jobs:
        router._commit(job, router._grid_line(router._cell(job.start), router._cell(job.goal)))
    removed = []
    original_remove = router._remove

    def observe_removal(job_id):
        removed.append(job_id)
        original_remove(job_id)

    monkeypatch.setattr(router, '_remove', observe_removal)
    if final_cleanup:
        # Leave the crossing for the final geometric cleanup to resolve.
        monkeypatch.setattr(router, '_legalize_routes', lambda *_args: set())
        monkeypatch.setattr(router, '_shove_unrouted', lambda failed, *_args: failed)
        _, report = router.route()
        assert report.failed[0]['job'] == 0
    else:
        # Prevent retries from hiding which route the conflict graph removed.
        monkeypatch.setattr(router, '_route_job', lambda *_args: None)
        monkeypatch.setattr(router, '_astar', lambda *_args: None)
        assert router._legalize_routes(24) == {0}
    assert removed == [0]
    assert set(router.routes) == {1}
