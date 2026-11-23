"""Import tscircuit SimpleRouteJson into PCB Studio's mil-based board model."""
from collections import defaultdict
from itertools import chain, combinations, pairwise
import math


def source_board(data):
    """Recognize both a bare SRJ board and the benchmark envelope names."""
    default = data.get('default')
    candidate = default if isinstance(default, dict) else data
    for key in ('simpleRouteJson', 'simple_route_json'):
        if key in candidate:
            return _object(candidate[key], key)
    return candidate if 'layerCount' in candidate else None


def _object(value, label):
    if not isinstance(value, dict):
        raise ValueError(f'SimpleRouteJson {label} must be an object.')
    return value


def _array(value, label):
    if not isinstance(value, list):
        raise ValueError(f'SimpleRouteJson {label} must be an array.')
    return value


def _name(value, label):
    if not isinstance(value, str) or not value:
        raise ValueError(f'SimpleRouteJson {label} must be a nonempty string.')
    return value


def _number(value, label, *, positive=False, nonnegative=False):
    if (type(value) not in (int, float) or not math.isfinite(value) or
            (positive and value <= 0) or (nonnegative and value < 0)):
        raise ValueError(f'SimpleRouteJson {label} has an invalid numeric value.')
    return value


def _mil(value, label, **options):
    converted = _number(value, label, **options) * (1000 / 25.4)
    return _number(converted, label, **options)


def _xy(point, label):
    point = _object(point, label)
    return tuple(_mil(point.get(axis), f'{label}.{axis}') for axis in ('x', 'y'))


def _names(value, label):
    return list(dict.fromkeys(_name(item, label) for item in _array(value, label)))


class _Components:
    """Disjoint sets for net merging and minimum spanning trees."""

    def __init__(self, size):
        self.parent = list(range(size))

    def root(self, index):
        while self.parent[index] != index:
            self.parent[index] = self.parent[self.parent[index]]
            index = self.parent[index]
        return index

    def join(self, a, b):
        a, b = sorted((self.root(a), self.root(b)))
        self.parent[b] = a
        return a != b


def _connections(source, bounds, layers, minimum):
    """Resolve copper owners and turn multi-terminal nets into point pairs."""
    records = _array(source.get('connections'), 'connections')
    components = _Components(len(records))
    identifiers = defaultdict(set)
    terminals = defaultdict(set)
    connections = []
    nominal = source.get('nominalTraceWidth', source['minTraceWidth'])
    xmin, xmax, ymin, ymax = bounds
    for i, record in enumerate(records):
        label = f'connections[{i}]'
        record = _object(record, label)
        name = _name(record.get('name'), f'{label}.name')
        refs = [name, record.get('source_trace_id'), record.get('rootConnectionName'),
                record.get('netConnectionName'), record.get('__netConnectionName')]
        for key in ('mergedConnectionNames', '__rootConnectionNames'):
            refs.extend(_names(record.get(key, []), f'{label}.{key}'))
        width = max(minimum, _mil(record.get('nominalTraceWidth', record.get('width', nominal)),
                                  f'{label}.nominalTraceWidth', positive=True))
        points = {}
        by_id = {}
        off_board = record.get('isOffBoard', False)
        for point in _array(record.get('pointsToConnect'), f'{label}.pointsToConnect'):
            xy = _xy(point, f'{label}.point')
            if ('layers' in point) == ('layer' in point):
                raise ValueError(f'SimpleRouteJson {label} points require exactly one of layer or layers.')
            point_layers = (_names(point['layers'], f'{label}.point.layers') if 'layers' in point
                            else [_name(point['layer'], f'{label}.point.layer')])
            if not point_layers:
                raise ValueError(f'SimpleRouteJson {label} has a point with no layers.')
            layers.update(dict.fromkeys(point_layers))
            point_ids = [_name(point[key], f'{label}.point.{key}')
                         for key in ('pointId', 'pcb_port_id') if key in point]
            point_key = point_ids[0] if point_ids else (xy, frozenset(point_layers))
            if not off_board and not (xmin <= xy[0] <= xmax and ymin <= xy[1] <= ymax):
                raise ValueError(f'SimpleRouteJson {label} ({name}) has a point outside bounds.')
            previous = points.setdefault(point_key, (xy, []))[1]
            previous.extend(layer for layer in point_layers if layer not in previous)
            if point.get('pointId'):
                by_id[point['pointId']] = point_key
            refs.extend(point_ids)
            for layer in point_layers:
                terminals[xy, layer].add(i)
        for ref in filter(None, refs):
            identifiers[_name(ref, f'{label} identifier')].add(i)
        path = [_name(item, f'{label}.path') for item in _array(record.get('path', []), f'{label}.path')]
        if any(ref not in by_id for ref in path):
            raise ValueError(f'SimpleRouteJson {label}.path refers to an unknown pointId.')
        connections.append((name, width, points, [(by_id[a], by_id[b]) for a, b in pairwise(path)], off_board))

    for indexes in chain(identifiers.values(), terminals.values()):
        first = min(indexes)
        for index in indexes:
            components.join(first, index)
    net_name = [connections[components.root(i)][0] for i in range(len(records))]
    owners = {ref: net_name[min(indexes)] for ref, indexes in identifiers.items()}
    pad_owners = {key: net_name[min(indexes)] for key, indexes in terminals.items()}
    groups = defaultdict(list)
    jobs = []
    unsupported = []
    for i, (name, width, points, path, off_board) in enumerate(connections):
        if off_board:
            unsupported.append({'net': net_name[i], 'connection': name, 'reason': 'off-board connection'})
        else:
            groups[net_name[i]].append((width, points, path))
    for net, group in groups.items():
        points = {}
        written_edges = set()
        for _, members, path in group:
            for key, (xy, point_layers) in members.items():
                previous = points.setdefault(key, (xy, {}))[1]
                previous.update(dict.fromkeys(point_layers))
            written_edges.update(frozenset(edge) for edge in path)
        width = max(record[0] for record in group)
        keys = list(points)
        pairs = []
        for a, b in combinations(range(len(keys)), 2):
            start, start_layers = points[keys[a]]
            end, end_layers = points[keys[b]]
            common = next((layer for layer in start_layers if layer in end_layers), None)
            explicit = frozenset((keys[a], keys[b])) in written_edges
            pairs.append((not explicit, common is None, math.dist(start, end), a, b, common))
        tree = _Components(len(keys))
        for _, _, distance, a, b, layer in sorted(pairs):
            if not tree.join(a, b):
                continue
            start, start_layers = points[keys[a]]
            end, end_layers = points[keys[b]]
            pair = {'net': net, 'start': list(start), 'end': list(end)}
            if layer is None:
                unsupported.append({**pair, 'start_layers': list(start_layers),
                                    'end_layers': list(end_layers), 'reason': 'layer change required'})
            elif distance > 0:
                jobs.append({**pair, 'layer': layer, 'width': width})
    return jobs, unsupported, owners, pad_owners


def _shape(kind, width, height, degrees):
    if kind in ('circle', 'oval') and width == height:
        return {'shape': 'circle', 'radius': width / 2}
    if kind == 'rect' and not degrees % 180:
        return {'shape': 'rect', 'x1': -width / 2, 'x2': width / 2,
                'y1': -height / 2, 'y2': height / 2}
    angle = math.radians(degrees % 360)
    if kind == 'rect':
        points = [(x * width / 2, y * height / 2) for x, y in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
    else:
        # The legacy oval type represents a capsule. Use a circumscribed
        # polygon so the approximation never opens a path through copper.
        radius = min(width, height) / (2 * math.cos(math.pi / 16))
        straight = abs(width - height) / 2
        angles = [(i - 3.5) * math.pi / 8 for i in range(8)]
        end = [(straight + radius * math.cos(theta), radius * math.sin(theta)) for theta in angles]
        points = end + [(-x, -y) for x, y in end]
        if height > width:
            angle += math.pi / 2
    sine, cosine = math.sin(angle), math.cos(angle)
    return {'shape': 'polygon', 'points': [[x * cosine - y * sine, x * sine + y * cosine]
                                          for x, y in points]}


def _obstacles(source, layers, owners, pad_owners):
    pads = []
    for i, obstacle in enumerate(_array(source.get('obstacles'), 'obstacles')):
        label = f'obstacles[{i}]'
        obstacle = _object(obstacle, label)
        kind = obstacle.get('shape', obstacle.get('type'))
        if kind not in ('rect', 'oval', 'circle'):
            raise ValueError(f'SimpleRouteJson {label} has unsupported shape {kind!r}.')
        xy = _xy(obstacle.get('center'), f'{label}.center')
        pad_layers = _names(obstacle.get('layers'), f'{label}.layers')
        if not pad_layers:
            raise ValueError(f'SimpleRouteJson {label} has no layers.')
        layers.update(dict.fromkeys(pad_layers))
        refs = _names(obstacle.get('connectedTo', []), f'{label}.connectedTo')
        nets = {owners[ref] for ref in refs if ref in owners}
        if not nets:
            nets = {pad_owners[xy, layer] for layer in pad_layers if (xy, layer) in pad_owners}
        shape = _shape(kind, _mil(obstacle.get('width'), f'{label}.width', positive=True),
                       _mil(obstacle.get('height'), f'{label}.height', positive=True),
                       _number(obstacle.get('ccwRotationDegrees', 0), f'{label}.ccwRotationDegrees'))
        net = next(iter(nets)) if len(nets) == 1 and not obstacle.get('isNonPlatedHole') else None
        pads.append({'x': xy[0], 'y': xy[1], 'layers': pad_layers, 'net': net, 'shape': shape})
    return pads


def _traces(source, layers, owners):
    wires, vias = [], []
    via_diameter = source.get('minViaPadDiameter', source.get('min_via_pad_diameter',
                                                             source.get('minViaDiameter', 0.6)))
    for i, trace in enumerate(_array(source.get('traces', []), 'traces')):
        label = f'traces[{i}]'
        trace = _object(trace, label)
        ref = _name(trace.get('pcb_trace_id'), f'{label}.pcb_trace_id')
        name = _name(trace.get('connection_name', ref), f'{label}.connection_name')
        aliases = _names(trace.get('connectsTo', []), f'{label}.connectsTo')
        nets = {owners[key] for key in [name, ref, *aliases] if key in owners}
        net = next(iter(nets)) if len(nets) == 1 else name if not nets else None
        previous = None
        for index, point in enumerate(_array(trace.get('route'), f'{label}.route')):
            element = f'{label}.route[{index}]'
            point = _object(point, element)
            kind = point.get('route_type')
            if kind == 'wire':
                xy = list(_xy(point, element))
                layer = _name(point.get('layer'), f'{element}.layer')
                width = _mil(point.get('width'), f'{element}.width', positive=True)
                layers.setdefault(layer, None)
                if previous and previous[0] == layer and previous[1] != xy:
                    wires.append({'layer': layer, 'net': net, 'width': max(previous[2], width),
                                  'points': [previous[1], xy], 'locked': True, 'ref': ref})
                previous = (layer, xy, width)
            elif kind in ('via', 'through_obstacle'):
                ends = [_name(point.get(key), f'{element}.{key}') for key in ('from_layer', 'to_layer')]
                layers.update(dict.fromkeys(ends))
                stack = _ordered_layers(layers)
                first, last = sorted(stack.index(layer) for layer in ends)
                if kind == 'via':
                    xy = _xy(point, element)
                    span = _names(point.get('layers', []), f'{element}.layers')
                    layers.update(dict.fromkeys(span))
                    if not span:
                        span = stack[first:last + 1] if source.get('allowBlindAndBuriedVias') else stack
                    diameter = _mil(point.get('via_diameter', via_diameter), f'{element}.via_diameter', positive=True)
                    vias.append({'x': xy[0], 'y': xy[1], 'layers': span, 'net': net, 'ref': ref,
                                 'shape': {'shape': 'circle', 'radius': diameter / 2}})
                else:
                    # A zero-length transition uses the plated copper already in the obstacle list.
                    start = list(_xy(point.get('start'), f'{element}.start'))
                    end = list(_xy(point.get('end'), f'{element}.end'))
                    width = _mil(point.get('width'), f'{element}.width', positive=True)
                    if start != end:
                        wires.extend({'layer': layer, 'net': net, 'width': width, 'points': [start, end],
                                      'locked': True, 'ref': ref} for layer in stack[first:last + 1])
                previous = None
            else:
                raise ValueError(f'SimpleRouteJson {element} has unsupported route element {kind!r}.')
    return wires, vias


def _ordered_layers(layers):
    return sorted(layers, key=lambda layer: (layer != 'top', layer == 'bottom'))


def import_board(source, original):
    layer_count = source.get('layerCount')
    if type(layer_count) is not int or layer_count < 1:
        raise ValueError('SimpleRouteJson layerCount must be a positive integer.')
    raw_bounds = _object(source.get('bounds'), 'bounds')
    bounds = [_mil(raw_bounds.get(key), f'bounds.{key}') for key in ('minX', 'maxX', 'minY', 'maxY')]
    if bounds[0] >= bounds[1] or bounds[2] >= bounds[3]:
        raise ValueError('SimpleRouteJson bounds must have positive width and height.')
    minimum = _mil(source.get('minTraceWidth'), 'minTraceWidth', positive=True)
    layers = dict.fromkeys(['top', *(f'inner{i}' for i in range(1, layer_count - 1))])
    if layer_count > 1:
        layers['bottom'] = None
    jobs, unsupported, owners, pad_owners = _connections(source, bounds, layers, minimum)
    pads = _obstacles(source, layers, owners, pad_owners)
    wires, vias = _traces(source, layers, owners)
    ordered_layers = _ordered_layers(layers)
    job_layers = {job['layer'] for job in jobs}
    preferred = next((layer for layer in ordered_layers if layer in job_layers), ordered_layers[0])
    rules = {'clearance_mil': _mil(source.get('minTraceToPadEdgeClearance', source.get('defaultObstacleMargin', 0.15)),
                                   'clearance', nonnegative=True),
             'board_edge_clearance_mil': _mil(source.get('minBoardEdgeClearance', 0),
                                              'minBoardEdgeClearance', nonnegative=True)}
    return {'metadata': {'layers': [{'name': name, 'type': 'signal'} for name in ordered_layers],
                         'routing_bounds': bounds, 'design_rules': rules,
                         'simple_route_json_layer': preferred},
            'simple_route_json_source': original, 'unsupported_connections': unsupported,
            'routing_connections': jobs, 'pads': pads, 'wires': wires, 'vias': vias}
