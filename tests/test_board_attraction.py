import pytest

from pcb_viewer.router import Autorouter, RouterConfig


def guide(group, objects, strength=1.0, missing=False, dynamic=False):
    board = {'layer': 'TOP', 'routing_connections': [
        {'net': 'A', 'layer': 'TOP', 'width': 1, 'start': [0, 0], 'end': [20, 0]},
        {'net': 'GHOST', 'layer': 'TOP', 'width': 1, 'start': [0, 2], 'end': [20, 2]},
        {'net': 'B', 'layer': 'TOP', 'width': 1, 'start': [0, 10], 'end': [20, 10]},
    ], group: objects}
    router = Autorouter(board, RouterConfig(grid_step=2, clearance=0, boundary_margin=20,
                                           attraction_strength=strength))
    path = [router._cell((x, 0)) for x in range(0, 21, 2)]
    router._commit(router.jobs[0], path)
    if dynamic:
        router._commit(router.jobs[2], [router._cell((x, 10)) for x in range(0, 21, 2)])
    return router._relaxed_guide(router.jobs[0], path,
                                [router.jobs[1]] if missing else [], attract=True)


@pytest.mark.parametrize('group,geometry', [
    ('wires', {'width': 1, 'points': [[0, 10], [20, 10]]}),
    ('pads', {'x': 10, 'y': 10, 'shape': {'radius': 0.5}}),
    ('pins', {'x': 10, 'y': 10, 'shape': {'radius': 0.5}}),
    ('vias', {'x': 10, 'y': 10, 'shape': {'radius': 0.5}}),
    ('copper_polygons', {'vertices': [[0, 8], [20, 8], [20, 12], [0, 12]]}),
])
def test_fixed_board_net_without_routing_request_attracts(group, geometry):
    objects = [{**geometry, 'net': 'FIXED_ONLY', 'layer': 'TOP'}]
    moved, energy, _ = guide(group, objects)
    assert any(y > 0 for _, y in moved[1:-1])
    assert energy < 0
    assert moved[0] == (0, 0) and moved[-1] == (20, 0)
    disabled, _, _ = guide(group, objects, strength=0)
    assert all(y == 0 for _, y in disabled)


@pytest.mark.parametrize('net,layer', [('A', 'TOP'), (None, 'TOP'), ('FIXED', 'BOTTOM')])
def test_own_net_unnamed_and_other_layers_do_not_attract(net, layer):
    points, _, _ = guide('wires', [{'net': net, 'layer': layer, 'width': 1,
                                   'points': [[0, 10], [20, 10]]}])
    assert all(y == 0 for _, y in points)


def test_keepouts_do_not_attract_even_with_net_field():
    points, _, _ = guide('keepouts', [{'net': 'FIXED', 'layer': 'TOP', 'type': 'wire_keepout',
        'points': [[0, 8], [20, 8], [20, 12], [0, 12]]}])
    assert all(y == 0 for _, y in points)


def test_stronger_default_and_missing_corridor_protection():
    objects = [{'net': 'FIXED_ONLY', 'layer': 'TOP', 'width': 1, 'points': [[0, 8], [20, 8]]}]
    assert RouterConfig().attraction_strength == 1.0
    weak, _, _ = guide('wires', objects, strength=0.5)
    strong, _, _ = guide('wires', objects)
    assert max(y for _, y in strong) == pytest.approx(2 * max(y for _, y in weak))
    assert guide('wires', objects, missing=True)[0] == guide('wires', objects, strength=0, missing=True)[0]


def test_duplicate_static_objects_and_same_net_dynamic_route_do_not_multiply_attraction():
    wire = {'net': 'B', 'layer': 'TOP', 'width': 1, 'points': [[0, 10], [20, 10]]}
    assert guide('wires', [wire]) == guide('wires', [wire, wire])
    # Dynamic centerline has a slightly larger spacing target and wins the
    # normalized nearest-net comparison; static copper must not add a second pull.
    assert guide('wires', [wire], dynamic=True) == guide('wires', [], dynamic=True)
