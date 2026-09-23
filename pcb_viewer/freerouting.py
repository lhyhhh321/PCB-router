"""Optional Specctra DSN bridge for benchmarking the external Freerouting JAR.

Only the current JSON board is read.  Archived deleted trace interiors are
never exported: their two endpoints become the pins to be connected.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import zipfile

from .router import Autorouter, RouteReport, RouterConfig, extract_jobs


def required_java_version(jar_path: str | Path) -> int:
    """Read the JAR entry point's class-file version (Java 25 = 69)."""
    try:
        with zipfile.ZipFile(jar_path) as archive:
            manifest = archive.read('META-INF/MANIFEST.MF').decode('utf-8-sig')
            match = re.search(r'^Main-Class:\s*(\S+)', manifest, re.MULTILINE)
            if not match:
                raise ValueError('Freerouting JAR has no Main-Class manifest entry.')
            entry = match.group(1).replace('.', '/') + '.class'
            header = archive.read(entry)[:8]
    except (OSError, KeyError, zipfile.BadZipFile) as error:
        raise ValueError(f'Cannot inspect Freerouting JAR: {error}') from error
    if len(header) != 8 or header[:4] != b'\xca\xfe\xba\xbe':
        raise ValueError('Freerouting JAR has an invalid main class.')
    major = int.from_bytes(header[6:8], 'big')
    if major < 45:
        raise ValueError('Freerouting JAR has an invalid class-file version.')
    return major - 44


def _java_version(executable: str) -> int | None:
    try:
        result = subprocess.run([executable, '-version'], capture_output=True,
                                text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r'\bversion\s+"(?:1\.)?(\d+)',
                      result.stdout + result.stderr, re.IGNORECASE)
    return int(match.group(1)) if result.returncode == 0 and match else None


def resolve_java(jar_path: str | Path, executable: str | None = None) -> tuple[str, int]:
    """Select a runtime compatible with the JAR, without changing system Java."""
    required = required_java_version(jar_path)
    candidates = []
    explicit = (executable or '').strip()
    if explicit:
        candidates.append(explicit)
    else:
        if os.environ.get('FREEROUTING_JAVA'):
            candidates.append(os.environ['FREEROUTING_JAVA'])
        if os.environ.get('JAVA_HOME'):
            candidates.append(str(Path(os.environ['JAVA_HOME']) / 'bin' / 'java'))
        if sys.platform == 'darwin':
            try:
                result = subprocess.run(['/usr/libexec/java_home', '-v', str(required)],
                                        capture_output=True, text=True, timeout=10,
                                        check=False)
                if result.returncode == 0 and result.stdout.strip():
                    candidates.append(str(Path(result.stdout.strip()) / 'bin' / 'java'))
            except (OSError, subprocess.TimeoutExpired):
                pass
        candidates.append('java')
    seen = set()
    found = []
    for candidate in candidates:
        if '/' in candidate or '\\' in candidate:
            candidate = str(Path(candidate).expanduser().resolve())
        if candidate in seen:
            continue
        seen.add(candidate)
        version = _java_version(candidate)
        if version is not None:
            found.append(f'{candidate} (Java {version})')
            if version >= required:
                return candidate, version
        elif explicit:
            found.append(f'{candidate} (not executable or version unavailable)')
    observed = ', '.join(found) or 'none'
    raise ValueError(f'This Freerouting JAR requires Java {required} or newer; '
                     f'compatible runtime not found. Checked: {observed}. '
                     'Install a matching JRE and select its bin/java executable '
                     '(or set FREEROUTING_JAVA).')


def _atom(value: str) -> str:
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _num(value: float) -> str:
    return f'{float(value):.6f}'.rstrip('0').rstrip('.') or '0'


def _xy(points) -> str:
    return ' '.join(f'{_num(x)} {_num(y)}' for x, y in points)


def export_dsn(data: dict, config: RouterConfig) -> str:
    """Make a one-signal-layer DSN with real obstacles and requested pins."""
    jobs = extract_jobs(data, config.layer)
    if not jobs:
        raise ValueError('No routing requests found.')
    layers = {job.layer for job in jobs}
    if len(layers) != 1:
        raise ValueError('Freerouting benchmark accepts one signal layer per run.')
    layer = next(iter(layers))
    clearance = config.clearance
    if clearance is None:
        clearance = float(data.get('metadata', {}).get('design_rules', {}).get('clearance_mil', 2))
    terminals: dict[tuple[str, tuple[float, float]], str] = {}
    width_by_net: dict[str, float] = {}
    for job in jobs:
        width_by_net[job.net] = max(job.width, width_by_net.get(job.net, 0))
        for point in (job.start, job.goal):
            terminals.setdefault((job.net, point), f'P{len(terminals)}')
    terminal_shapes = {}
    for group in ('vias', 'pads', 'pins'):
        for obj in data.get(group, []):
            if 'x' not in obj or 'y' not in obj:
                continue
            key = obj.get('net'), (float(obj['x']), float(obj['y']))
            if key in terminals:
                terminal_shapes[key] = obj.get('shape') or {
                    'shape': 'circle', 'radius': 3 if group == 'vias' else 4}
    all_points = [point for _, point in terminals]
    for group in ('vias', 'pads', 'pins'):
        all_points += [(obj['x'], obj['y']) for obj in data.get(group, [])
                       if 'x' in obj and 'y' in obj]
    for group, key in (('copper_polygons', 'vertices'), ('keepouts', 'points'), ('wires', 'points')):
        all_points += [tuple(p) for obj in data.get(group, []) for p in obj.get(key, [])]
    margin = max(20, config.boundary_margin)
    xmin = min(x for x, _ in all_points) - margin
    xmax = max(x for x, _ in all_points) + margin
    ymin = min(y for _, y in all_points) - margin
    ymax = max(y for _, y in all_points) + margin
    qlayer = _atom(layer)
    lines = [f'(PCB "pcb_view_benchmark"',
             ' (parser (string_quote ") (space_in_quoted_tokens on))',
             ' (resolution mil 1000)', ' (unit mil)',
             f' (structure (layer {qlayer} (type signal))',
             f'  (boundary (rect pcb {_num(xmin)} {_num(ymin)} {_num(xmax)} {_num(ymax)}))',
             '  (via VIA_DEFAULT)',
             f'  (rule (width {_num(min(width_by_net.values()))}) (clearance {_num(clearance)})))',
             ' (placement (component ROUTE_PINS (place ROUTE_PINS 0 0 front 0)))',
             ' (library (image ROUTE_PINS']
    # Every requested endpoint is a pad.  Other copper shapes are immutable
    # obstacles in this single-layer comparison, including unrelated vias.
    for (_, point), name in terminals.items():
        lines.append(f'  (pin PAD_{name} {name} {_xy([point])})')
    terminal_locations = set(terminals)
    for group in ('vias', 'pads', 'pins'):
        for obj in data.get(group, []):
            if obj.get('layer', layer) not in (None, layer) or (obj.get('layers') and layer not in obj['layers']):
                continue
            x, y = obj.get('x'), obj.get('y')
            if not isinstance(x, (float, int)) or not isinstance(y, (float, int)):
                continue
            if (obj.get('net'), (float(x), float(y))) in terminal_locations:
                continue
            shape = obj.get('shape', {})
            if shape.get('shape') == 'rect':
                x1, y1 = x + shape['x1'], y + shape['y1']
                x2, y2 = x + shape['x2'], y + shape['y2']
                geometry = f'(rect {qlayer} {_num(x1)} {_num(y1)} {_num(x2)} {_num(y2)})'
            else:
                radius = float(shape.get('radius', 3 if group == 'vias' else 4))
                geometry = f'(circ {qlayer} {_num(radius * 2)} {_num(x)} {_num(y)})'
            lines.append(f'  (wire_keepout {geometry})')
    for obj in data.get('keepouts', []):
        if (obj.get('layer', layer) not in (None, layer) or
                obj.get('type', 'wire_keepout') != 'wire_keepout'):
            continue
        points = obj.get('points', [])
        if len(points) >= 3:
            if points[0] == points[-1]:
                points = points[:-1]
            lines.append(f'  (wire_keepout (polygon {qlayer} 0 {_xy(points)}))')
    for polygon in data.get('copper_polygons', []):
        if polygon.get('layer', layer) != layer or polygon.get('net') in width_by_net:
            continue
        points = polygon.get('vertices', [])
        if len(points) >= 3:
            if points[0] == points[-1]:
                points = points[:-1]
            lines.append(f'  (wire_keepout (polygon {qlayer} 0 {_xy(points)}))')
    for wire in data.get('wires', []):
        if (wire.get('layer', layer) == layer and wire.get('net') not in width_by_net
                and len(wire.get('points', [])) >= 2):
            lines.append(f'  (wire_keepout (path {qlayer} '
                         f'{_num(wire.get("width", 2))} {_xy(wire["points"])}))')
    lines.append(')')
    for key, name in terminals.items():
        shape = terminal_shapes.get(key, {'shape': 'circle', 'radius': 1})
        if shape.get('shape') == 'rect':
            x1, y1, x2, y2 = (shape[field] for field in ('x1', 'y1', 'x2', 'y2'))
            coordinates = _xy(((x1, y1), (x2, y1), (x2, y2), (x1, y2)))
            geometry = f'(polygon {qlayer} 0.01 {coordinates})'
        else:
            geometry = f'(circle {qlayer} {_num(max(1, shape.get("radius", 1)) * 2)} 0 0)'
        lines.append(f'  (padstack PAD_{name} (shape {geometry}))')
    lines += [f'  (padstack VIA_DEFAULT (shape (circle {qlayer} 4 0 0)))',
              ' )', ' (network']
    net_pins: dict[str, list[str]] = defaultdict(list)
    for (net, _), name in terminals.items():
        net_pins[net].append(f'ROUTE_PINS-{name}')
    for net, pins in net_pins.items():
        lines.append(f'  (net {_atom(net)} (pins {" ".join(pins)}))')
        # A net can have several requested edges of different widths; the
        # widest is used so this benchmark never silently narrows a trace.
        lines.append(f'  (class {_atom("CLASS_" + net)} {_atom(net)} '
                     f'(circuit (use_via VIA_DEFAULT)) '
                     f'(rule (width {_num(width_by_net[net])}) (clearance {_num(clearance)})))')
    lines += [' )', ' (wiring']
    for wire in data.get('wires', []):
        if (wire.get('layer', layer) != layer or wire.get('net') not in width_by_net
                or len(wire.get('points', [])) < 2):
            continue
        lines.append(f'  (wire (path {qlayer} {_num(wire.get("width", 2))} '
                     f'{_xy(wire["points"])}) (net {_atom(wire.get("net", ""))}) (type protect))')
    for polygon in data.get('copper_polygons', []):
        if polygon.get('layer', layer) != layer or polygon.get('net') not in width_by_net:
            continue
        points = polygon.get('vertices', [])
        if len(points) < 3:
            continue
        if points[0] == points[-1]:
            points = points[:-1]
        lines.append(f'  (wire (polygon {qlayer} 0 {_xy(points)}) '
                     f'(net {_atom(polygon["net"])}) (type protect))')
    lines += [' )', ')']
    return '\n'.join(lines) + '\n'


_TOKEN = re.compile(r'\(|\)|"(?:\\.|[^"\\])*"|[^\s()]+')


def _sexpr(source: str):
    source = source.replace('(string_quote ")', '(string_quote QUOTE)')
    stack = []
    root = None
    for match in _TOKEN.finditer(source):
        token = match.group()
        if token == '(':
            node = []
            if stack:
                stack[-1].append(node)
            stack.append(node)
        elif token == ')':
            if not stack:
                raise ValueError('Malformed Freerouting DSN: unexpected closing parenthesis.')
            root = stack.pop()
        elif stack:
            if token.startswith('"'):
                token = json.loads(token)
            stack[-1].append(token)
    if stack or not isinstance(root, list):
        raise ValueError('Malformed Freerouting DSN: incomplete expression.')
    return root


def _children(node, name: str):
    return [part for part in node if isinstance(part, list) and part and part[0] == name]


def parse_routed_session(source: str, layer: str) -> list[dict]:
    root = _sexpr(source)
    if not root or root[0].lower() != 'session':
        raise ValueError('Freerouting did not produce a Specctra session.')
    routes = _children(root, 'routes')
    if not routes:
        raise ValueError('Freerouting session contains no routes section.')
    resolution = _children(routes[0], 'resolution')
    scale = float(resolution[0][2]) if resolution and len(resolution[0]) >= 3 else 1.0
    if scale <= 0:
        raise ValueError('Invalid Freerouting session resolution.')
    network = _children(routes[0], 'network_out')
    if not network:
        raise ValueError('Freerouting session contains no network_out section.')
    wires = []
    for net_node in _children(network[0], 'net'):
        if len(net_node) < 2:
            continue
        net = net_node[1]
        for wire in _children(net_node, 'wire'):
            paths = _children(wire, 'path')
            if not paths or len(paths[0]) < 7:
                continue
            path = paths[0]
            if path[1] != layer:
                continue
            try:
                coords = [float(value) / scale for value in path[3:]]
                width = float(path[2]) / scale
            except ValueError:
                continue
            if len(coords) < 4 or len(coords) % 2 or width <= 0:
                continue
            points = [[round(coords[i], 6), round(coords[i + 1], 6)]
                      for i in range(0, len(coords), 2)]
            wires.append({'layer': layer, 'width': width, 'net': net,
                          'route_type': 'autoroute', 'points': points,
                          'autoroute_job': len(wires)})
    return wires


def _segments_intersect(a, b, c, d) -> bool:
    def cross(p, q, r):
        return ((q[0] - p[0]) * (r[1] - p[1]) -
                (q[1] - p[1]) * (r[0] - p[0]))

    if (max(a[0], b[0]) < min(c[0], d[0]) - 1e-6 or
            max(c[0], d[0]) < min(a[0], b[0]) - 1e-6 or
            max(a[1], b[1]) < min(c[1], d[1]) - 1e-6 or
            max(c[1], d[1]) < min(a[1], b[1]) - 1e-6):
        return False
    ab_c, ab_d = cross(a, b, c), cross(a, b, d)
    cd_a, cd_b = cross(c, d, a), cross(c, d, b)
    return (ab_c * ab_d <= 1e-12 and cd_a * cd_b <= 1e-12)


def _connected_jobs(jobs, wires, data=None):
    """Check each requested endpoint pair against returned same-net copper."""
    by_net = defaultdict(list)
    for wire in wires:
        by_net[wire['net']].append(wire)
    access_radius = {}
    for group in ('vias', 'pads', 'pins'):
        for obj in (data or {}).get(group, []):
            if 'x' not in obj or 'y' not in obj or not obj.get('net'):
                continue
            shape = obj.get('shape') or {}
            if shape.get('shape') == 'rect':
                radius = max(math.hypot(shape[x], shape[y])
                             for x in ('x1', 'x2') for y in ('y1', 'y2'))
            else:
                radius = shape.get('radius', 3 if group == 'vias' else 4)
            access_radius[(obj['net'], (float(obj['x']), float(obj['y'])))] = float(radius)
    completed = set()
    for net in {job.net for job in jobs}:
        net_jobs = [job for job in jobs if job.net == net]
        net_wires = by_net[net]
        nodes = {point for job in net_jobs for point in (job.start, job.goal)}
        for wire in net_wires:
            nodes.update(tuple(point) for point in wire['points'])
        node_list = list(nodes)
        index = {point: i for i, point in enumerate(node_list)}
        parent = list(range(len(node_list)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def union(a, b):
            parent[find(a)] = find(b)

        segments = [(tuple(a), tuple(b), wire['width']) for wire in net_wires
                    for a, b in zip(wire['points'], wire['points'][1:])]
        for a, b, _ in segments:
            union(index[a], index[b])
        for first, (a, b, _) in enumerate(segments):
            for c, d, _ in segments[first + 1:]:
                if _segments_intersect(a, b, c, d):
                    union(index[a], index[c])
        for first, job in enumerate(net_jobs):
            for other in net_jobs[first + 1:]:
                for a in (job.start, job.goal):
                    for b in (other.start, other.goal):
                        if math.dist(a, b) <= (access_radius.get((net, a), 0) +
                                               access_radius.get((net, b), 0)):
                            union(index[a], index[b])
        for point in node_list:
            for a, b, width in segments:
                reach = width / 2 + access_radius.get((net, point), 0) + 0.01
                if (min(a[0], b[0]) - reach <= point[0] <= max(a[0], b[0]) + reach and
                    min(a[1], b[1]) - reach <= point[1] <= max(a[1], b[1]) + reach and
                    Autorouter._distance_to_segment(*point, a, b) <= reach):
                    union(index[point], index[a])
        for job in net_jobs:
            if find(index[job.start]) == find(index[job.goal]):
                completed.add(job.id)
        if completed:
            for wire in net_wires:
                wire['autoroute_jobs'] = [job.id for job in net_jobs if job.id in completed]
    return completed


def freeroute_data(data: dict, config: RouterConfig, jar_path: str,
                   progress=None, timeout_seconds: float = 600,
                   java_executable: str | None = None) -> tuple[list[dict], RouteReport]:
    """Execute a user-installed Freerouting JAR and import its routed SES."""
    config.validate()
    jar = Path(jar_path).expanduser()
    if not jar.is_file():
        raise ValueError('Freerouting JAR not found. Select a downloaded Freerouting .jar file.')
    if timeout_seconds <= 0:
        raise ValueError('Freerouting time limit must be positive.')
    java, java_version = resolve_java(jar, java_executable)
    jobs = extract_jobs(data, config.layer)
    if not jobs:
        raise ValueError('No routing requests found.')
    started = time.monotonic()
    layer = jobs[0].layer
    report = RouteReport(layer, len(jobs), config={'backend': 'freerouting',
                         'jar': str(jar), 'timeout_seconds': timeout_seconds,
                         **asdict(config), 'java_executable': java,
                         'java_version': java_version})
    with tempfile.TemporaryDirectory(prefix='pcb-freerouting-') as directory:
        source = Path(directory) / 'input.dsn'
        output = Path(directory) / 'routed.ses'
        source.write_text(export_dsn(data, config), encoding='utf-8')
        command = [java, '-jar', str(jar), '--gui.enabled=false',
                   '-de', str(source),
                   '-do', str(output), '-mp', str(config.iterations),
                   # Freerouting 2.0/2.1 headless mode ignores -mp unless the
                   # board's stop-pass field is also set (upstream #376).
                   f'--router.stop_pass_no={config.iterations}', '-mt', '0']
        log_path = Path(directory) / 'freerouting.log'
        try:
            with log_path.open('w+', encoding='utf-8') as log_file:
                process = subprocess.Popen(command, cwd=directory, stdout=log_file,
                                           stderr=subprocess.STDOUT, text=True)
                while process.poll() is None:
                    timed_out = time.monotonic() - started >= timeout_seconds
                    cancelled = progress and progress(0, 1, 'Freerouting is routing…') is False
                    if cancelled or timed_out:
                        process.terminate()
                        try:
                            process.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                        if timed_out:
                            raise ValueError(f'Freerouting exceeded the {timeout_seconds:g}s time limit.')
                        report.cancelled = True
                        report.elapsed_seconds = round(time.monotonic() - started, 3)
                        return [], report
                    time.sleep(0.2)
                log_file.seek(0)
                log = log_file.read()[-4000:]
        except OSError as error:
            raise ValueError(f'Cannot start Freerouting: {error}') from error
        if process.returncode or not output.is_file() or output.stat().st_size < 20:
            raise ValueError(f'Freerouting failed (exit {process.returncode}).\n{log}')
        wires = parse_routed_session(output.read_text(encoding='utf-8-sig'), layer)
    def signature(wire):
        points = tuple(tuple(round(float(value), 6) for value in point)
                       for point in wire.get('points', []))
        return (wire.get('net'), round(float(wire.get('width', 2)), 6),
                min(points, tuple(reversed(points))))

    original = {signature(wire) for wire in data.get('wires', [])
                if wire.get('layer', layer) == layer}
    wires = [wire for wire in wires
             if signature(wire) not in original]
    for index, wire in enumerate(wires):
        wire['autoroute_job'] = index
    fixed_wires = [dict(wire) for wire in data.get('wires', [])
                   if wire.get('layer', layer) == layer and wire.get('net')]
    for index, wire in enumerate(fixed_wires):
        wire['autoroute_job'] = -index - 1
    completed = _connected_jobs(jobs, wires + fixed_wires, data)
    report.routed = len(completed)
    report.failed = [{'job': job.id, 'net': job.net, 'reason': 'not connected by Freerouting'}
                     for job in jobs if job.id not in completed]
    report.wire_length = round(sum(math.dist(a, b) for wire in wires
                                   for a, b in zip(wire['points'], wire['points'][1:])), 3)
    crossings = {pair for pair in Autorouter._geometric_crossings(wires + fixed_wires)
                 if pair[1] >= 0}
    report.conflict_cells = len(crossings)
    report.conflicting_jobs = sorted({index for pair in crossings for index in pair
                                      if index >= 0})
    report.elapsed_seconds = round(time.monotonic() - started, 3)
    if progress:
        progress(1, 1, 'Freerouting complete')
    return wires, report
