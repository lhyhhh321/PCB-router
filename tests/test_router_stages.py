import json

import pytest

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


def test_unlegalized_crossings_are_retained_and_reported(monkeypatch):
    board = {'routing_connections': [
        {'net': 'A', 'layer': 'TOP', 'width': 1, 'start': [0, 10], 'end': [20, 10]},
        {'net': 'B', 'layer': 'TOP', 'width': 1, 'start': [10, 0], 'end': [10, 20]},
    ]}
    def direct(self, job, *_args):
        return self._grid_line(self._cell(job.start), self._cell(job.goal))
    monkeypatch.setattr(Autorouter, '_route_job', direct)
    wires, report = autoroute_data(board, config(stages=('initial',)))
    assert len(wires) == 2 and report.routed == 2
    assert report.conflict_cells > 0 and report.conflicting_jobs == [0, 1]
    assert not report.clean


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
