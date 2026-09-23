import math

from pcb_viewer.router import Autorouter, RouterConfig, autoroute_data, extract_jobs


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
