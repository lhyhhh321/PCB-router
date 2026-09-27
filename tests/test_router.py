import math

from pcb_viewer.router import (Autorouter, DetailedTrace, RouteReport, RouterConfig,
                               autoroute_data, extract_jobs)


def test_routes_around_wire_keepout():
    board = {
        'layer': 'TOP',
        'metadata': {
            'layers': [{'name': 'TOP', 'type': 'signal'}],
            'design_rules': {'clearance_mil': 1.0, 'width_mil': 2.0},
        },
        'routing_connections': [
            {'net': 'N1', 'layer': 'TOP', 'width': 2, 'start': [0, 0], 'end': [20, 20]},
        ],
        'keepouts': [
            {'type': 'wire_keepout', 'layer': 'TOP', 'shape': 'polygon',
             'points': [[8, 5], [12, 5], [12, 15], [8, 15], [8, 5]]},
        ],
    }
    wires, report = autoroute_data(
        board, RouterConfig(grid_step=2, boundary_margin=20, guide_margin=10, iterations=3)
    )
    assert report.clean and report.routed == 1
    assert wires[0]['points'][0] == [0.0, 0.0]
    assert wires[0]['points'][-1] == [20.0, 20.0]
    # The direct diagonal crosses the keepout, so a legal route must be longer.
    assert report.wire_length > math.sqrt(800)


def test_seeded_initial_order_is_reproducible_and_changes_sequence():
    board = {'layer': 'TOP', 'routing_connections': [
        {'net': f'N{index}', 'layer': 'TOP', 'width': 2,
         'start': [0, index * 8], 'end': [20, index * 8]}
        for index in range(20)
    ]}
    config = RouterConfig(ordering='short', boundary_margin=10)
    def order(seed):
        return [job.id for job in Autorouter(board, config, order_seed=seed)._initial_order()]
    assert order(None) == list(range(20))
    assert order(7) == order(7)
    assert order(7) != order(None)
    assert order(7) != order(8)


def test_random_order_trials_select_best_cleaner_and_shorter_run(monkeypatch):
    board = {'layer': 'TOP', 'routing_connections': [
        {'net': f'N{index}', 'layer': 'TOP', 'start': [0, index * 10],
         'end': [20, index * 10]} for index in range(2)
    ]}
    seen = []
    def fake_route(self, progress=None):
        seen.append(self.order_seed)
        if progress:
            progress(len(self.jobs), len(self.jobs), 'done')
        report = RouteReport(self.layer, len(self.jobs), routed=1,
                             failed=[{'job': 1, 'net': 'N1', 'reason': 'no path'}],
                             conflict_cells=1 if self.order_seed is None else 0,
                             wire_length={None: 10.0, 7: 17.0, 8: 18.0}[self.order_seed],
                             elapsed_seconds=1.0)
        return [{'seed': self.order_seed}], report
    monkeypatch.setattr(Autorouter, 'route', fake_route)
    updates = []
    wires, report = autoroute_data(
        board, RouterConfig(random_rounds=3, random_seed=7),
        lambda done, total, message: updates.append((done, total, message)))
    assert seen == [None, 7, 8]
    assert wires == [{'seed': 7}]
    assert report.selected_round == 2
    assert report.routed == 1 and report.conflict_cells == 0 and report.wire_length == 17.0
    assert [result['seed'] for result in report.round_results] == [None, 7, 8]
    assert [done for done, _, _ in updates] == [2, 4, 6]
    assert all(total == 6 for _, total, _ in updates)


def test_random_order_trials_stop_when_baseline_is_complete(monkeypatch):
    board = {'layer': 'TOP', 'routing_connections': [
        {'net': 'N', 'layer': 'TOP', 'start': [0, 0], 'end': [20, 0]},
    ]}
    seen = []
    def fake_route(self, progress=None):
        seen.append(self.order_seed)
        return [], RouteReport(self.layer, len(self.jobs), routed=1)
    monkeypatch.setattr(Autorouter, 'route', fake_route)
    _, report = autoroute_data(board, RouterConfig(random_rounds=5))
    assert seen == [None]
    assert report.selected_round == 1
    assert len(report.round_results) == 1


def test_archived_trace_supplies_endpoints_but_never_a_route_guide():
    board = {
        'layer': 'TOP',
        'metadata': {'layers': [{'name': 'TOP', 'type': 'signal'}]},
        'wires': [],
        'deleted_connections': {'wires': [
            {'net': 'N', 'layer': 'TOP', 'width': 2,
             'points': [[0, 0], [5, 20], [20, 20]]},
        ]},
    }
    jobs = extract_jobs(board)
    assert jobs[0].start == (0.0, 0.0) and jobs[0].goal == (20.0, 20.0)
    assert jobs[0].guide == ()
    wires, report = Autorouter(
        board, RouterConfig(grid_step=2, boundary_margin=20, guide_margin=8, iterations=1)
    ).route()
    assert report.clean
    assert wires[0]['points'] != board['deleted_connections']['wires'][0]['points']


def test_initial_no_path_job_gets_final_shove_attempt():
    board = {'layer': 'TOP', 'routing_connections': [
        {'net': 'N', 'layer': 'TOP', 'width': 2,
         'start': [0, 0], 'end': [20, 0]},
    ]}
    router = Autorouter(board, RouterConfig(iterations=1, boundary_margin=10,
                                          force_iterations=0, detailed_max_passes=0))
    router._route_job = lambda *args: None
    router._legalize_routes = lambda *args: set()
    retried = []

    def capture(failed, *_):
        retried.append(set(failed))
        return failed

    router._shove_unrouted = capture
    _, report = router.route()
    assert retried == [{0}]
    assert report.routed == 0 and len(report.failed) == 1


def test_via_keepout_does_not_block_trace_but_wire_keepout_does():
    base = {
        'layer': 'TOP',
        'metadata': {'layers': [{'name': 'TOP', 'type': 'signal'}]},
        'routing_connections': [
            {'net': 'N', 'layer': 'TOP', 'width': 1, 'start': [0, 0], 'end': [20, 0]},
        ],
    }
    polygon = [[8, -4], [12, -4], [12, 4], [8, 4], [8, -4]]
    via_board = dict(base, keepouts=[{'type': 'via_keepout', 'layer': 'TOP', 'points': polygon}])
    wire_board = dict(base, keepouts=[{'type': 'wire_keepout', 'layer': 'TOP', 'points': polygon}])
    via_wires, _ = autoroute_data(via_board, RouterConfig(grid_step=1, clearance=0, iterations=1,
                                                          boundary_margin=10, guide_margin=5))
    wire_wires, _ = autoroute_data(wire_board, RouterConfig(grid_step=1, clearance=0, iterations=1,
                                                            boundary_margin=10, guide_margin=5))
    assert len(via_wires[0]['points']) == 2
    assert len(wire_wires[0]['points']) > 2


def test_opposing_diagonals_are_detected_even_without_shared_grid_node():
    board = {
        'layer': 'TOP',
        'routing_connections': [
            {'net': 'A', 'layer': 'TOP', 'width': 1, 'start': [0, 0], 'end': [10, 10]},
            {'net': 'B', 'layer': 'TOP', 'width': 1, 'start': [0, 10], 'end': [10, 0]},
        ],
    }
    wires, report = autoroute_data(
        board, RouterConfig(grid_step=1, clearance=0, boundary_margin=0,
                            guide_margin=0, iterations=2, max_shove_blockers=0)
    )
    assert report.routed == 1
    assert len(report.failed) == 1
    assert report.conflict_cells == 0
    assert len(wires) == 1


def test_geometric_crossing_guard_detects_off_grid_segments():
    wires = [
        {'net': 'A', 'autoroute_job': 0, 'points': [[0.2, 0.2], [5.7, 5.7]]},
        {'net': 'B', 'autoroute_job': 1, 'points': [[0.3, 5.7], [5.8, 0.2]]},
    ]
    assert Autorouter._geometric_crossings(wires) == {(0, 1)}


def test_shared_same_net_corridor_is_emitted_only_once():
    board = {'layer': 'TOP', 'routing_connections': [
        {'net': 'N', 'layer': 'TOP', 'width': 2, 'start': [0, 0], 'end': [10, 0]},
        {'net': 'N', 'layer': 'TOP', 'width': 2, 'start': [0, 0], 'end': [20, 0]},
    ]}
    wires, report = autoroute_data(
        board, RouterConfig(grid_step=1, clearance=0, iterations=1,
                            boundary_margin=10, guide_margin=5))
    assert report.routed == 2 and report.clean
    assert math.isclose(report.wire_length, 20)
    assert {job for wire in wires for job in wire['autoroute_jobs']} == {0, 1}
    edges = [tuple(sorted((tuple(a), tuple(b)))) for wire in wires
             for a, b in zip(wire['points'], wire['points'][1:])]
    assert len(edges) == len(set(edges))


def test_shared_corridor_keeps_the_widest_required_copper():
    board = {'layer': 'TOP', 'routing_connections': [
        {'net': 'N', 'layer': 'TOP', 'width': 2, 'start': [0, 0], 'end': [10, 0]},
        {'net': 'N', 'layer': 'TOP', 'width': 4, 'start': [0, 0], 'end': [20, 0]},
    ]}
    wires, report = autoroute_data(
        board, RouterConfig(grid_step=1, clearance=0, iterations=1,
                            boundary_margin=10, guide_margin=5))
    assert report.routed == 2
    assert math.isclose(report.wire_length, 20)
    assert all(wire['width'] == 4 for wire in wires)


def test_force_gradient_repels_interior_points_but_keeps_pins_fixed():
    board = {'layer': 'TOP', 'routing_connections': [
        {'net': 'A', 'layer': 'TOP', 'width': 1,
         'start': [0, 0], 'end': [20, 0]},
        {'net': 'B', 'layer': 'TOP', 'width': 1,
         'start': [0, 4], 'end': [20, 4]},
    ]}
    router = Autorouter(board, RouterConfig(grid_step=2, clearance=0,
                        boundary_margin=10, iterations=1,
                        attraction_strength=0))
    paths = [[router._cell((x, y)) for x in range(0, 21, 2)]
             for y in (0, 4)]
    for job, path in zip(router.jobs, paths):
        router._commit(job, path)
    guide, energy, _ = router._relaxed_guide(router.jobs[0], paths[0], [])
    assert guide[0] == router.jobs[0].start
    assert guide[-1] == router.jobs[0].goal
    assert energy > 0
    assert any(point[1] < 0 for point in guide[1:-1])
    _, reduced_energy, _ = router._relaxed_guide(router.jobs[0], paths[0], [], guide)
    assert reduced_energy < energy


def test_attraction_draws_distant_parallel_nets_toward_even_spacing():
    board = {'layer': 'TOP', 'routing_connections': [
        {'net': 'A', 'layer': 'TOP', 'width': 1,
         'start': [0, 0], 'end': [20, 0]},
        {'net': 'B', 'layer': 'TOP', 'width': 1,
         'start': [0, 10], 'end': [20, 10]},
    ]}
    config = RouterConfig(grid_step=2, clearance=0, boundary_margin=10,
                          iterations=1, attraction_strength=0.5)
    router = Autorouter(board, config)
    paths = [[router._cell((x, y)) for x in range(0, 21, 2)]
             for y in (0, 10)]
    for job, path in zip(router.jobs, paths):
        router._commit(job, path)
    guide, energy, _ = router._relaxed_guide(router.jobs[0], paths[0], [],
                                             attract=True)
    assert guide[0] == router.jobs[0].start and guide[-1] == router.jobs[0].goal
    assert any(point[1] > 0 for point in guide[1:-1])
    _, reduced_energy, _ = router._relaxed_guide(router.jobs[0], paths[0], [], guide,
                                                 attract=True)
    assert reduced_energy < energy

    disabled = Autorouter(board, RouterConfig(grid_step=2, clearance=0,
                          boundary_margin=10, iterations=1,
                          attraction_strength=0))
    for job, path in zip(disabled.jobs, paths):
        disabled._commit(job, path)
    unchanged, _, _ = disabled._relaxed_guide(disabled.jobs[0], paths[0], [],
                                              attract=True)
    assert unchanged == disabled._sample_route(disabled.jobs[0], paths[0])


def test_attraction_does_not_pull_into_a_missing_connection_corridor():
    board = {'layer': 'TOP', 'routing_connections': [
        {'net': 'A', 'layer': 'TOP', 'width': 1,
         'start': [0, 0], 'end': [20, 0]},
        {'net': 'B', 'layer': 'TOP', 'width': 1,
         'start': [0, 10], 'end': [20, 10]},
        {'net': 'C', 'layer': 'TOP', 'width': 1,
         'start': [0, 2], 'end': [20, 2]},
    ]}
    def guide(strength):
        router = Autorouter(board, RouterConfig(grid_step=2, clearance=0,
                            boundary_margin=10, iterations=1,
                            attraction_strength=strength))
        for job_id, y in ((0, 0), (1, 10)):
            router._commit(router.jobs[job_id],
                           [router._cell((x, y)) for x in range(0, 21, 2)])
        return router._relaxed_guide(router.jobs[0], router.routes[0],
                                     [router.jobs[2]], attract=True)[0]
    assert guide(0.5) == guide(0)
    assert any(point[1] < 0 for point in guide(0.5)[1:-1])


def test_force_iteration_reserves_a_spacing_candidate():
    board = {'layer': 'TOP', 'routing_connections': [
        {'net': f'N{index}', 'layer': 'TOP', 'width': 1,
         'start': [0, index * 20], 'end': [20, index * 20]}
        for index in range(7)
    ]}
    router = Autorouter(board, RouterConfig(grid_step=2, clearance=0,
                        boundary_margin=10, iterations=1,
                        force_iterations=1, force_max_jobs=3,
                        attraction_strength=0.5))
    paths = {}
    for job in router.jobs[:6]:
        path = [router._cell((x, job.id * 20)) for x in range(0, 21, 2)]
        paths[job.id] = path
        router._commit(job, path)
    router._straighten_routes = lambda: 0
    router._shove_unrouted = lambda failed, *_: failed
    router._shorten_detours = lambda *_: 0
    router._route_job = lambda *_: None
    def controls(job, path, missing, previous=None, **_):
        sampled = previous or router._sample_route(job, path)
        moved = tuple(point if index in (0, len(sampled) - 1)
                      else (point[0], point[1] + 1)
                      for index, point in enumerate(sampled))
        affected = {6} if job.id < 4 else set()
        return moved, 1.0 if affected else -1.0, affected
    router._relaxed_guide = controls
    attempted = []
    def unchanged(job, *_):
        attempted.append(job.id)
        return paths[job.id]
    router._astar = unchanged
    router._force_refine({6})
    assert len(attempted) == 3
    assert 4 in attempted or 5 in attempted


def test_force_round_restores_original_when_no_connection_is_gained():
    board = {'layer': 'TOP', 'routing_connections': [
        {'net': 'A', 'layer': 'TOP', 'width': 1,
         'start': [0, 0], 'end': [20, 0]},
        {'net': 'B', 'layer': 'TOP', 'width': 1,
         'start': [0, 4], 'end': [20, 4]},
    ]}
    router = Autorouter(board, RouterConfig(grid_step=2, clearance=0,
                        boundary_margin=10, iterations=1,
                        force_iterations=2, force_max_jobs=1))
    path = [router._cell((x, 0)) for x in range(0, 21, 2)]
    router._commit(router.jobs[0], path)
    router._route_job = lambda *args: None
    failed, rounds, moved, cancelled = router._force_refine({1})
    assert failed == {1} and rounds >= 1 and moved == 0 and not cancelled
    assert router.routes == {0: path}


def test_force_control_points_accumulate_across_rounds():
    board = {'layer': 'TOP', 'routing_connections': [
        {'net': 'A', 'layer': 'TOP', 'width': 1,
         'start': [0, 0], 'end': [20, 0]},
        {'net': 'B', 'layer': 'TOP', 'width': 1,
         'start': [0, 4], 'end': [20, 4]},
    ]}
    router = Autorouter(board, RouterConfig(grid_step=2, clearance=0,
                        boundary_margin=10, iterations=1,
                        force_iterations=3, force_max_jobs=1))
    path = [router._cell((x, 0)) for x in range(0, 21, 2)]
    router._commit(router.jobs[0], path)
    guided_y = []

    def unchanged_path(job, *_):
        guide = router.force_guides.get(job.id)
        if guide:
            guided_y.append(guide[len(guide) // 2][1])
        return path

    router._astar = unchanged_path
    router._route_job = lambda *args: None
    phases = []
    router._shove_unrouted = lambda failed, *_: phases.append('repair') or failed
    router._shorten_detours = lambda *_: phases.append('detour') or 0
    router._straighten_routes = lambda: phases.append('straighten') or 0
    relax = router._relaxed_guide

    def record_refinement(*args, **kwargs):
        phases.append('refine')
        return relax(*args, **kwargs)

    router._relaxed_guide = record_refinement
    failed, rounds, moved, _ = router._force_refine({1})
    assert failed == {1} and rounds == 3 and moved == 0
    assert phases.count('repair') == 3
    for index, phase in enumerate(phases):
        if phase == 'straighten':
            assert phases[index:index + 4] == ['straighten', 'repair', 'detour', 'refine']
    assert len(guided_y) == 3
    assert guided_y[0] > guided_y[1] > guided_y[2]
    assert router.routes == {0: path}


def test_force_round_moves_a_route_and_commits_new_connection():
    board = {'layer': 'TOP', 'routing_connections': [
        {'net': 'A', 'layer': 'TOP', 'width': 1,
         'start': [0, 0], 'end': [20, 0]},
        {'net': 'B', 'layer': 'TOP', 'width': 1,
         'start': [0, 4], 'end': [20, 4]},
    ]}
    router = Autorouter(board, RouterConfig(grid_step=2, clearance=0,
                        boundary_margin=10, iterations=1,
                        force_iterations=2, force_max_jobs=1))
    first = [router._cell((x, 0)) for x in range(0, 21, 2)]
    router._commit(router.jobs[0], first)
    failed, rounds, moved, cancelled = router._force_refine({1})
    assert failed == set() and rounds == 2 and moved == 1 and not cancelled
    assert router.routes[0] != first and 1 in router.routes
    assert router._conflicts()[0] == set()


def test_detour_repair_shortens_a_completed_route():
    board = {'layer': 'TOP', 'routing_connections': [
        {'net': 'A', 'layer': 'TOP', 'width': 1,
         'start': [0, 0], 'end': [100, 0]},
        {'net': 'B', 'layer': 'TOP', 'width': 1,
         'start': [48, -8], 'end': [52, -8]},
    ]}
    router = Autorouter(board, RouterConfig(grid_step=2, clearance=0,
                        boundary_margin=120, iterations=1))

    def cells(points):
        path = []
        for a, b in zip(points, points[1:]):
            section = router._grid_line(router._cell(a), router._cell(b))
            path.extend(section if not path else section[1:])
        return path

    router._commit(router.jobs[0], cells([(0, 0), (0, 100), (100, 100), (100, 0)]))
    router._commit(router.jobs[1], cells([(48, -8), (48, 0), (52, 0), (52, -8)]))
    assert router._shorten_detours(24) == 1
    assert router._path_length(router.routes[0]) < 150
    assert router._conflicts()[0] == set()
    assert router._geometric_crossings(router._unique_wire_runs()) == set()


def test_legal_astar_does_not_detour_around_old_congestion_history():
    board = {'layer': 'TOP', 'routing_connections': [
        {'net': 'A', 'layer': 'TOP', 'width': 1,
         'start': [0, 0], 'end': [20, 0]},
    ]}
    router = Autorouter(board, RouterConfig(grid_step=2, clearance=0,
                        boundary_margin=10, iterations=1))
    for x in range(2, 20, 2):
        router.history[router._key(router._cell((x, 0)))] = 100
    bounds = router._guide_bounds(router.jobs[0], 10)
    legal = router._astar(router.jobs[0], bounds, 24, True)
    assert legal is not None
    assert all(router._point(cell)[1] == 0 for cell in legal)


def test_extreme_detour_limit_can_be_disabled():
    board = {'layer': 'TOP', 'routing_connections': [
        {'net': 'A', 'layer': 'TOP', 'width': 1,
         'start': [0, 0], 'end': [100, 0]},
    ]}
    router = Autorouter(board, RouterConfig(grid_step=2, clearance=0,
                        boundary_margin=150, max_detour_ratio=3))
    path = []
    for a, b in zip([(0, 0), (0, 120), (100, 120), (100, 0)],
                    [(0, 120), (100, 120), (100, 0)]):
        section = router._grid_line(router._cell(a), router._cell(b))
        path.extend(section if not path else section[1:])
    assert router._excessive_detour(router.jobs[0], path)
    router.config.max_detour_ratio = 0
    assert not router._excessive_detour(router.jobs[0], path)


def test_each_iteration_straightens_legal_zigzags():
    board = {'layer': 'TOP', 'routing_connections': [
        {'net': 'A', 'layer': 'TOP', 'width': 1,
         'start': [0, 0], 'end': [20, 0]},
    ]}
    router = Autorouter(board, RouterConfig(grid_step=2, clearance=0,
                        boundary_margin=10, iterations=1, force_iterations=1,
                        force_max_jobs=0))
    zigzag = [router._cell((x, 2 if x % 4 == 2 else 0))
              for x in range(0, 21, 2)]
    router._commit(router.jobs[0], zigzag)
    failed, rounds, _, cancelled = router._force_refine(set())
    assert failed == set() and rounds == 1 and not cancelled
    assert router._path_bends(router.routes[0]) == 0
    assert router._path_length(router.routes[0]) == 20


def test_straightening_keeps_foreign_net_obstacle():
    board = {'layer': 'TOP', 'routing_connections': [
        {'net': 'A', 'layer': 'TOP', 'width': 1,
         'start': [0, 0], 'end': [20, 0]},
        {'net': 'B', 'layer': 'TOP', 'width': 1,
         'start': [10, 0], 'end': [10, 0]},
    ]}
    router = Autorouter(board, RouterConfig(grid_step=2, clearance=0,
                        boundary_margin=10, iterations=1))
    zigzag = [router._cell((x, 2 if x % 4 == 2 else 0))
              for x in range(0, 21, 2)]
    router._commit(router.jobs[0], zigzag)
    router._commit(router.jobs[1], [router._cell((10, 0))])
    router._straighten_routes()
    assert router._cell((10, 0)) not in router.routes[0]
    assert router._conflicts()[0] == set()


def test_detailed_trace_uses_bends_and_segments():
    path = [(0, 0), (1, 0), (2, 0), (2, 1), (2, 2)]
    trace = DetailedTrace.from_path(7, path)
    assert trace.job_id == 7
    assert trace.bend_indices == (0, 2, 4)
    assert trace.bends == ((0, 0), (2, 0), (2, 2))
    assert trace.segments == (((0, 0), (2, 0)), ((2, 0), (2, 2)))


def test_straightening_long_bend_list_can_sample_turns():
    board = {'layer': 'TOP', 'routing_connections': [
        {'net': 'A', 'layer': 'TOP', 'width': 1,
         'start': [0, 0], 'end': [60, 0]},
    ]}
    router = Autorouter(board, RouterConfig(grid_step=2, clearance=0,
                        boundary_margin=10, iterations=1, force_iterations=0))
    zigzag = [router._cell((x, 2 if x % 4 == 2 else 0))
              for x in range(0, 61, 2)]
    router._commit(router.jobs[0], zigzag)
    assert len(DetailedTrace.from_path(0, zigzag).bend_indices) > 24
    assert router._straighten_routes(limit=1) == 1
    assert router._path_bends(router.routes[0]) < router._path_bends(zigzag)


def test_detailed_stage_straightens_then_connects_with_hard_astar():
    board = {'layer': 'TOP', 'routing_connections': [
        {'net': 'A', 'layer': 'TOP', 'width': 1,
         'start': [0, 0], 'end': [20, 0]},
        {'net': 'B', 'layer': 'TOP', 'width': 1,
         'start': [0, 20], 'end': [20, 20]},
    ]}
    router = Autorouter(board, RouterConfig(grid_step=2, clearance=0,
                        boundary_margin=10, iterations=1, force_iterations=0))
    zigzag = [router._cell((x, 2 if x % 4 == 2 else 0))
              for x in range(0, 21, 2)]
    router._commit(router.jobs[0], zigzag)
    failed, passes, straightened, dragged, cancelled = router._detailed_route({1})
    assert not cancelled and failed == set() and passes >= 2
    assert straightened >= 1 and dragged == 0
    assert router._path_bends(router.routes[0]) == 0
    assert router._conflicts()[0] == set()


def test_detailed_drag_moves_blocking_bend_trace_transactionally():
    board = {'layer': 'TOP', 'routing_connections': [
        {'net': 'A', 'layer': 'TOP', 'width': 1,
         'start': [0, 0], 'end': [20, 0]},
        {'net': 'B', 'layer': 'TOP', 'width': 1,
         'start': [8, -8], 'end': [12, -8]},
    ]}
    router = Autorouter(board, RouterConfig(grid_step=2, clearance=0,
                        boundary_margin=20, iterations=1, force_iterations=0))
    target = [router._cell((x, 0)) for x in range(0, 21, 2)]
    blocker = []
    for a, b in zip([(8, -8), (8, 0), (12, 0)],
                    [(8, 0), (12, 0), (12, -8)]):
        section = router._grid_line(router._cell(a), router._cell(b))
        blocker.extend(section if not blocker else section[1:])
    clear_blocker = [router._cell((x, -8)) for x in range(8, 13, 2)]
    router._commit(router.jobs[1], blocker)
    guide = router._detailed_drag_guide(DetailedTrace.from_path(1, blocker),
                                        target, 4, 1)
    assert guide[0] == router.jobs[1].start and guide[-1] == router.jobs[1].goal
    assert len(guide) > len(DetailedTrace.from_path(1, blocker).bends)
    original_astar = router._astar
    router._astar = lambda job, bounds, factor, hard=False, history_weight=None: (
        target if job.id == 0 and not hard else
        original_astar(job, bounds, factor, hard, history_weight))
    router._detailed_astar = lambda job: (
        None if job.id == 0 and 1 in router.routes else
        target if job.id == 0 else clear_blocker)
    router._straighten_routes = lambda **_kwargs: 0
    failed, _, _, dragged, cancelled = router._detailed_route({0})
    assert not cancelled and failed == set() and dragged == 1
    assert router.routes[0] == target and router.routes[1] == clear_blocker
    assert router._conflicts()[0] == set()


def test_detailed_drag_rolls_back_when_blocker_cannot_be_rebuilt():
    board = {'layer': 'TOP', 'routing_connections': [
        {'net': 'A', 'layer': 'TOP', 'width': 1,
         'start': [0, 0], 'end': [20, 0]},
        {'net': 'B', 'layer': 'TOP', 'width': 1,
         'start': [8, -8], 'end': [12, -8]},
    ]}
    router = Autorouter(board, RouterConfig(grid_step=2, clearance=0,
                        boundary_margin=20, iterations=1, force_iterations=0))
    target = [router._cell((x, 0)) for x in range(0, 21, 2)]
    blocker = []
    for a, b in zip([(8, -8), (8, 0), (12, 0)],
                    [(8, 0), (12, 0), (12, -8)]):
        section = router._grid_line(router._cell(a), router._cell(b))
        blocker.extend(section if not blocker else section[1:])
    router._commit(router.jobs[1], blocker)
    router._astar = lambda job, _bounds, _factor, hard=False, _history=None: (
        target if job.id == 0 and not hard else None)
    router._detailed_astar = lambda job: target if job.id == 0 else None
    assert not router._detailed_drag_blockers(router.jobs[0])
    assert router.routes == {1: blocker}
    assert router.force_guides == {}


def test_detailed_stage_receives_routes_rejected_by_detour_guard():
    board = {'layer': 'TOP', 'routing_connections': [
        {'net': 'A', 'layer': 'TOP', 'width': 1,
         'start': [0, 0], 'end': [20, 0]},
    ]}
    router = Autorouter(board, RouterConfig(grid_step=2, clearance=0,
                        boundary_margin=10, iterations=1, force_iterations=0))
    router._excessive_detour = lambda *_args: True
    seen = []

    def detailed(failed, _progress):
        seen.append(set(failed))
        return failed, 1, 0, 0, False

    router._detailed_route = detailed
    _, report = router.route()
    assert seen == [{0}]
    assert report.routed == 0 and report.detailed_passes == 1


def test_initial_refinement_runs_before_all_jobs_are_placed():
    board = {'layer': 'TOP', 'routing_connections': [
        {'net': name, 'layer': 'TOP', 'width': 1,
         'start': [0, y], 'end': [20, y]}
        for name, y in (('A', 0), ('B', 20), ('C', 40))
    ]}
    router = Autorouter(board, RouterConfig(grid_step=2, clearance=0,
                        boundary_margin=10, iterations=1,
                        early_refine_interval=2, force_iterations=0))
    visited = []
    router._refine_initial_routes = lambda *_: (visited.append(len(router.routes)) or set(), False)
    _, report = router.route()
    assert visited == [2]
    assert report.early_refinement_passes == 1
    assert report.routed == 3


def test_early_refinement_moves_a_route_away_from_missing_airwire():
    board = {'layer': 'TOP', 'routing_connections': [
        {'net': 'A', 'layer': 'TOP', 'width': 1,
         'start': [0, 0], 'end': [20, 0]},
        {'net': 'B', 'layer': 'TOP', 'width': 1,
         'start': [0, 4], 'end': [20, 4]},
        {'net': 'C', 'layer': 'TOP', 'width': 1,
         'start': [0, 20], 'end': [20, 20]},
    ]}
    router = Autorouter(board, RouterConfig(grid_step=2, clearance=0,
                        boundary_margin=10, iterations=1))
    for job_id, y in ((0, 0), (2, 20)):
        router._commit(router.jobs[job_id],
                       [router._cell((x, y)) for x in range(0, 21, 2)])
    original = list(router.routes[0])
    moves, cancelled = router._refine_initial_routes({1})
    assert moves == {0} and not cancelled
    assert router.routes[0] != original
    assert router._conflicts()[0] == set()


def test_ripup_and_reroute_method_reduces_provisional_conflicts():
    board = {'layer': 'TOP', 'routing_connections': [
        {'net': 'A', 'layer': 'TOP', 'width': 1,
         'start': [0, 0], 'end': [20, 0]},
        {'net': 'B', 'layer': 'TOP', 'width': 1,
         'start': [0, 2], 'end': [20, 2]},
    ]}
    router = Autorouter(board, RouterConfig(grid_step=2, clearance=2,
                        boundary_margin=10, iterations=2))
    for job, y in zip(router.jobs, (0, 2)):
        router._commit(job, [router._cell((x, y)) for x in range(0, 21, 2)])
    before = len(router._conflicts()[0])
    worked, cancelled = router._ripup_and_reroute(1)
    assert worked and not cancelled
    assert len(router._conflicts()[0]) < before
    assert router.history


def test_focused_early_ripup_leaves_unrelated_conflicts_untouched():
    board = {'layer': 'TOP', 'routing_connections': [
        {'net': name, 'layer': 'TOP', 'width': 1,
         'start': [0, y], 'end': [20, y]}
        for name, y in (('A', 0), ('B', 2), ('C', 40), ('D', 42))
    ]}
    router = Autorouter(board, RouterConfig(grid_step=2, clearance=2,
                        boundary_margin=10, iterations=2))
    for job, y in zip(router.jobs, (0, 2, 40, 42)):
        router._commit(job, [router._cell((x, y)) for x in range(0, 21, 2)])
    unrelated = {job_id: list(router.routes[job_id]) for job_id in (2, 3)}
    worked, cancelled = router._ripup_and_reroute(1, focus_jobs={0})
    assert worked and not cancelled
    assert {job_id: router.routes[job_id] for job_id in (2, 3)} == unrelated


def test_ripup_refinement_can_avoid_a_local_blocker():
    board = {'layer': 'TOP', 'routing_connections': [
        {'net': 'A', 'layer': 'TOP', 'width': 1,
         'start': [0, 0], 'end': [20, 0]},
        {'net': 'B', 'layer': 'TOP', 'width': 1,
         'start': [8, -8], 'end': [12, -8]},
    ]}
    router = Autorouter(board, RouterConfig(grid_step=2, clearance=0,
                        boundary_margin=20, iterations=2))
    first = [router._cell((x, 0)) for x in range(0, 21, 2)]
    second = []
    for a, b in zip([(8, -8), (8, 0), (12, 0)],
                    [(8, 0), (12, 0), (12, -8)]):
        section = router._grid_line(router._cell(a), router._cell(b))
        second.extend(section if not second else section[1:])
    router._commit(router.jobs[0], first)
    router._commit(router.jobs[1], second)
    router._remove(0)
    refined = router._refined_ripup_path(router.jobs[0], first, first, 24)
    assert refined is not None and refined != first
    assert not router._path_blockers(router.jobs[0], refined)


def test_ripup_method_calls_refinement_when_ordinary_reroute_stalls():
    board = {'layer': 'TOP', 'routing_connections': [
        {'net': 'A', 'layer': 'TOP', 'width': 1,
         'start': [0, 0], 'end': [20, 0]},
        {'net': 'B', 'layer': 'TOP', 'width': 1,
         'start': [0, 2], 'end': [20, 2]},
    ]}
    router = Autorouter(board, RouterConfig(grid_step=2, clearance=2,
                        boundary_margin=10, iterations=2))
    for job, y in zip(router.jobs, (0, 2)):
        router._commit(job, [router._cell((x, y)) for x in range(0, 21, 2)])
    old = {job_id: list(path) for job_id, path in router.routes.items()}
    router._route_job = lambda job, *_: old[job.id]
    calls = []

    def inspect(job, previous, current, *_):
        calls.append(job.id)
        return current

    router._refined_ripup_path = inspect
    worked, cancelled = router._ripup_and_reroute(1)
    assert worked and not cancelled and calls
