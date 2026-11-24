"""Synchronous lattice deformation for the Repulsion routing block."""
from dataclasses import dataclass
from itertools import groupby, pairwise
import math

from .router import Cell, DetailedTrace, _number


BUCKET_SIZE = 12
TRACE_REACH = 24  # Grid steps beyond clearance.
PASSES = 14
CONTROL_SPACING = 4


@dataclass
class _Move:
    path: list[Cell]
    indices: list[int]
    shifted: dict[int, Cell]
    spans: list[tuple[int, int, list[Cell]]]


class _RepulsionField:
    def __init__(self, router, paths, fixed):
        self.buckets = {bucket: list(points) for bucket, points in fixed.items()}
        self.clearance = router.clearance / router.step
        self.max_half = router.max_half_width / router.step
        for job_id, path in sorted(paths.items()):
            job = router.jobs_by_id[job_id]
            half = job.width / (2 * router.step)
            for x, y in path:
                bucket = x // BUCKET_SIZE, y // BUCKET_SIZE
                self.buckets.setdefault(bucket, []).append((x, y, half, job.net))

    def force(self, cell, net, half):
        x, y = cell
        radius = TRACE_REACH + self.clearance + half + self.max_half
        radius_squared = radius * radius
        nearest = {}
        bucket_rows = range(math.floor((y - radius) / BUCKET_SIZE),
                            math.floor((y + radius) / BUCKET_SIZE) + 1)
        for bx in range(math.floor((x - radius) / BUCKET_SIZE), math.floor((x + radius) / BUCKET_SIZE) + 1):
            for by in bucket_rows:
                for sx, sy, source_half, other_net in self.buckets.get((bx, by), ()):
                    if net == other_net:
                        continue
                    dx, dy = x - sx, y - sy
                    distance_squared = dx * dx + dy * dy
                    if distance_squared >= radius_squared:
                        continue
                    distance = math.sqrt(distance_squared)
                    surface = distance - source_half
                    previous = nearest.get(other_net)
                    if previous is None or surface < previous[0]:
                        nearest[other_net] = surface, distance, dx, dy
        fx = fy = 0.0
        for net_name in sorted(nearest):
            surface, distance, dx, dy = nearest[net_name]
            gap = max(0.0, surface - half - self.clearance)
            if distance and gap < TRACE_REACH:
                scale = (1.0 - gap / TRACE_REACH) / distance
                fx += dx * scale
                fy += dy * scale
        return fx, fy


def _fixed_geometry(router):
    """Index same-net contacts and sample force from fixed traces and pours."""
    buckets = {}
    contacts = {}
    shapes = []
    routed_nets = {router.jobs_by_id[i].net for i in router.routes}
    tiles = set()

    def add(net, geometry, cells, *, repels=False):
        shape_id = len(shapes)
        shapes.append(geometry)
        by_cell = contacts.setdefault(net, {}) if net in routed_nets else None
        for x, y in dict.fromkeys(cells):
            if by_cell is not None:
                by_cell.setdefault((x, y), []).append(shape_id)
            tile = x // 2, y // 2, net
            if repels and tile not in tiles:
                tiles.add(tile)
                bucket = x // BUCKET_SIZE, y // BUCKET_SIZE
                buckets.setdefault(bucket, []).append((x, y, 0, net))

    for group in ('pins', 'pads', 'vias', 'copper_polygons', 'wires'):
        for obj in router.data.get(group, []):
            if not isinstance(obj, dict) or not router._applies_to_layer(obj):
                continue
            net = obj.get('net')
            if not isinstance(net, str) or not net:
                continue
            if group == 'wires':
                radius = max(0.0, _number(obj.get('width'), 2.0) / 2)
                cells = (cell for a, b in pairwise(obj.get('points', []))
                         for cell in router._segment_cells(tuple(a), tuple(b), radius))
                add(net, None, cells, repels=True)
            elif group == 'copper_polygons':
                points = tuple((float(x), float(y)) for x, y in obj.get('vertices', []))
                add(net, ('polygon', (points,)), router._polygon_cells(points), repels=True)
            elif net in routed_nets:
                geometry = router._terminal_geometry(obj, group)
                if geometry is not None:
                    add(net, geometry, router._shape_cells(*geometry))
    return buckets, contacts, shapes


def _covers(router, shape, point):
    kind, values = shape
    x, y = point
    if kind == 'disk':
        cx, cy, radius = values
        return math.hypot(x - cx, y - cy) <= radius
    if kind == 'rect':
        x1, y1, x2, y2 = values
        return min(x1, x2) <= x <= max(x1, x2) and min(y1, y2) <= y <= max(y1, y2)
    polygon, = values
    inside = False
    for a, b in pairwise((*polygon, *polygon[:1])):
        if router._distance_to_segment(x, y, a, b) <= 1e-8:
            return True
        if (a[1] > y) != (b[1] > y) and x < a[0] + (y - a[1]) * (b[0] - a[0]) / (b[1] - a[1]):
            inside = not inside
    return inside


def _anchors(router, job, path, points, contacts, shapes):
    anchored = {0, len(path) - 1}
    hits = {}
    for index, cell in enumerate(path):
        key = router._key(cell)
        for shape_id in contacts.get(cell, ()):
            hits.setdefault(shape_id, []).append(index)
        if any(other != job.id and router.jobs_by_id[other].net == job.net
               for other in router.usage.get(key, ())):
            anchored.update(range(max(0, index - 1), min(len(path), index + 2)))
    for shape_id, indices in hits.items():
        shape = shapes[shape_id]
        for _, group in groupby(enumerate(indices), lambda entry: entry[1] - entry[0]):
            run = [index for _, index in group]
            center = run[len(run) // 2]
            # Keep a verified contact, preferring a terminal or junction that
            # is already fixed. Preserve the whole run for grazing contacts.
            if shape is not None:
                candidates = sorted(run, key=lambda i: (i not in anchored, abs(i - center)))
                contact = next((i for i in candidates if _covers(router, shape, points[i])), None)
                if contact is not None:
                    anchored.add(contact)
                    continue
            anchored.update(range(max(0, run[0] - 1), min(len(path), run[-1] + 2)))
    # Preserve the entire original edge at a shared contact, including half-grid junctions.
    for index, (a, b) in enumerate(pairwise(points)):
        if (next(router.route_traces.intersections(a, b, job.net, same_net=True,
                                                   exclude_job=job.id), None) is not None or
                next(router.fixed_traces.intersections(a, b, job.net, same_net=True), None) is not None):
            anchored.update((index, index + 1))
    return anchored


def _span_points(router, job, span, first, last):
    points = [router._point(cell) for cell in span]
    if first:
        points[0] = job.start
    if last:
        points[-1] = job.goal
    return points


def _preserves_saved_geometry(router, job, path):
    if path != router.resume_paths.get(job.id):
        return True
    points = _span_points(router, job, path, True, True)
    return router._simplify_polyline(router.resume_points[job.id]) == router._simplify_polyline(points)


def _fit_spans(router, job, path, indices, shifted):
    original_cells = set(path)
    while shifted:
        blocked = set()
        candidate = [path[0]]
        spans = []
        for i, j in pairwise(indices):
            if i not in shifted and j not in shifted:
                candidate.extend(path[i + 1:j + 1])
                continue
            span = router._grid_line(shifted.get(i, path[i]), shifted.get(j, path[j]))
            if len(span) < 2 or any(cell not in original_cells and not router._static_legal(cell, job)
                                   for cell in span):
                blocked.update((i, j))
                continue
            points = _span_points(router, job, span, i == 0, j == len(path) - 1)
            if any(not router._trace_edge_clear(a, b, job, False) for a, b in pairwise(points)):
                blocked.update((i, j))
                continue
            candidate.extend(span[1:])
            spans.append((i, j, span))
        if not blocked:
            if (candidate == path or len(set(candidate)) != len(candidate) or
                    not _preserves_saved_geometry(router, job, candidate)):
                return None
            diagonals = {}
            for a, b in pairwise(candidate):
                diagonal = router._diagonal_square(a, b)
                if diagonal is not None:
                    square, orientation = diagonal
                    if diagonals.setdefault(square, orientation) != orientation:
                        return None
            return _Move(candidate, indices, shifted, spans)
        # Hold obstructed controls in place and rebuild their adjacent spans.
        # A blocked end must not veto movement of the rest of the trace.
        shifted = {i: cell for i, cell in shifted.items() if i not in blocked}
    return None


def _normal_force(field, job, path, index, half):
    dx, dy = field.force(path[index], job.net, half)
    a = path[max(0, index - 2)]
    b = path[min(len(path) - 1, index + 2)]
    tx, ty = b[0] - a[0], b[1] - a[1]
    length_squared = tx * tx + ty * ty
    if length_squared:
        along = (dx * tx + dy * ty) / length_squared
        dx, dy = dx - along * tx, dy - along * ty
    return dx, dy


def _proposal(router, job, path, field, residual, contacts, shapes):
    if len(path) < 3:
        return None, False
    if not _preserves_saved_geometry(router, job, path):
        # Preserve imported geometry if moving it would snap an off-grid junction.
        return None, False
    points = _span_points(router, job, path, True, True)
    anchored = _anchors(router, job, path, points, contacts, shapes)
    half = job.width / (2 * router.step)
    turns = DetailedTrace.from_path(job.id, path).bend_indices
    landmarks = sorted(anchored | set(turns))
    controls = set(landmarks)
    if len(path) <= 2 * CONTROL_SPACING + 1:
        # On short traces a single sample can land where opposing forces
        # balance even when the adjacent cells can move outward.
        controls.update(range(1, len(path) - 1))
    else:
        for i, j in pairwise(landmarks):
            gap = j - i
            if gap > 1:
                parts = max(2, (gap + CONTROL_SPACING - 1) // CONTROL_SPACING)
                controls.update(i + part * gap // parts for part in range(1, parts))
    indices = sorted(controls)
    shifted = {}
    has_force = False
    taper_length = min(CONTROL_SPACING, (len(path) - 1) / 2)
    for i in indices:
        if i in anchored:
            continue
        cell = path[i]
        dx, dy = _normal_force(field, job, path, i, half)
        if abs(dx) + abs(dy) < 1e-9:
            continue
        has_force = True
        previous_x, previous_y = residual.get(cell, (0.0, 0.0))
        taper = min(1.0, min(i, len(path) - 1 - i) / taper_length)
        strength = router.config.repulsion_strength * taper
        x, y = previous_x + strength * dx, previous_y + strength * dy
        scale = min(1.0, 1.0 / math.hypot(x, y)) if x or y else 1.0
        x, y = x * scale, y * scale
        sx, sy = round(x), round(y)
        if sx or sy:
            shifted[i] = cell[0] + sx, cell[1] + sy
        residual[cell] = x - sx, y - sy
    if not shifted:
        return None, has_force
    return _fit_spans(router, job, path, indices, shifted), has_force


def _blocked_controls(router, job, original, move):
    blocked = set()
    # Adjacent spans share control cells and see the same candidate board.
    clearance_conflicts = {}

    def has_clearance_conflict(cell):
        conflict = clearance_conflicts.get(cell)
        if conflict is None:
            conflict = bool(router._dynamic_context(cell, job)[0])
            clearance_conflicts[cell] = conflict
        return conflict

    for i, j, span in move.spans:
        if (any(map(has_clearance_conflict, span)) or
                any(router._diagonal_blockers(a, b, job) for a, b in pairwise(span))):
            blocked.update((i, j))
            continue
        points = _span_points(router, job, span, i == 0, j == len(original) - 1)
        if any(next(router.route_traces.intersections(a, b, job.net), None) is not None
               for a, b in pairwise(points)):
            blocked.update((i, j))
    return blocked


def _settling_move(router, job, path, field, contacts, shapes):
    """Find a legal outward grid step for a trace that never moved."""
    if len(path) < 3 or not _preserves_saved_geometry(router, job, path):
        return None
    points = _span_points(router, job, path, True, True)
    anchored = _anchors(router, job, path, points, contacts, shapes)
    occupied = set(path)
    half = job.width / (2 * router.step)
    # Require enough force over the block for a half-cell displacement.
    minimum = 0.5 / (PASSES * router.config.repulsion_strength)
    candidates = []
    for i, (x, y) in enumerate(path):
        if i in anchored:
            continue
        fx, fy = _normal_force(field, job, path, i, half)
        for dx, dy in router.DIRECTIONS:
            score = (fx * dx + fy * dy) / math.hypot(dx, dy)
            cell = x + dx, y + dy
            if score >= minimum and cell not in occupied:
                candidates.append((-score, i, cell))
    indices = list(range(len(path)))
    for _, i, cell in sorted(candidates):
        move = _fit_spans(router, job, path, indices, {i: cell})
        if move is not None and not _blocked_controls(router, job, path, move):
            return move
    return None


def _route_bounds(router, job, path):
    xs, ys = zip(*path)
    left, bottom = router._point((min(xs), min(ys)))
    right, top = router._point((max(xs), max(ys)))
    points = (router.resume_points[job.id] if path == router.resume_paths.get(job.id)
              else (job.start, job.goal))
    for x, y in points:
        left, right = min(left, x), max(right, x)
        bottom, top = min(bottom, y), max(top, y)
    return left, bottom, right, top


def _affects(bounds, changed, margin):
    left, bottom, right, top = bounds
    return any(left <= x2 + margin and x1 <= right + margin and
               bottom <= y2 + margin and y1 <= top + margin
               for x1, y1, x2, y2 in changed)


def _apply_batch(router, snapshot, proposals):
    candidate_board = {**snapshot, **{job_id: move.path for job_id, move in proposals.items()}}
    router._restore_routes(candidate_board)
    bounds = {i: _route_bounds(router, router.jobs_by_id[i], move.path)
              for i, move in proposals.items()}
    recheck = set(proposals)
    # Bounds cover the lattice and exact terminal geometry. Expand them for
    # every possible dynamic clearance check and opposing diagonal.
    margin = 2 * router.max_half_width + abs(router.clearance) + router.step
    updated_index = False
    while proposals:
        next_proposals = {}
        trimmed = False
        for job_id, move in proposals.items():
            if job_id not in recheck:
                next_proposals[job_id] = move
                continue
            job = router.jobs_by_id[job_id]
            blocked = _blocked_controls(router, job, snapshot[job_id], move)
            if blocked:
                trimmed = True
                shifted = {i: cell for i, cell in move.shifted.items() if i not in blocked}
                move = _fit_spans(router, job, snapshot[job_id], move.indices, shifted)
            if move is not None:
                next_proposals[job_id] = move
        if not trimmed:
            if updated_index:
                # Rebuild once in the board's original order for later stages.
                router._restore_routes(candidate_board)
            return set(proposals)
        if not next_proposals:
            break

        # Update every revised trace before checking any revised move.
        changed = []
        for job_id, old_move in proposals.items():
            move = next_proposals.get(job_id)
            path = move.path if move is not None else snapshot[job_id]
            if path == old_move.path:
                continue
            job = router.jobs_by_id[job_id]
            changed.append(bounds[job_id])
            bounds[job_id] = _route_bounds(router, job, path)
            changed.append(bounds[job_id])
            router._remove(job_id)
            router._commit(job, path)
            candidate_board[job_id] = path
            updated_index = True
        recheck = {job_id for job_id, move in next_proposals.items()
                   if move is not proposals[job_id] or _affects(bounds[job_id], changed, margin)}
        proposals = next_proposals
    router._restore_routes(snapshot)
    return set()


def repel_routes(router, progress=None):
    """Evaluate one board snapshot per pass, then apply a legal batch of moves."""
    starting = dict(router.routes)
    if not starting or not router.config.repulsion_strength:
        return 0, 0, False
    residuals = {}
    fixed, contacts, shapes = _fixed_geometry(router)
    field = None
    passes = 0
    try:
        for iteration in range(PASSES):
            if progress and progress(iteration, PASSES + 1, f'Repulsion pass {iteration + 1}') is False:
                router._restore_routes(starting)
                return passes, 0, True
            passes += 1
            snapshot = dict(router.routes)
            if field is None:
                field = _RepulsionField(router, snapshot, fixed)
            proposals = {}
            has_force = False
            for job_id, path in sorted(snapshot.items()):
                job = router.jobs_by_id[job_id]
                candidate, active = _proposal(router, job, path, field,
                                              residuals.setdefault(job_id, {}),
                                              contacts.get(job.net, {}), shapes)
                has_force |= active
                if candidate is not None:
                    proposals[job_id] = candidate
            if proposals:
                moved = _apply_batch(router, snapshot, proposals)
                for job_id in moved:
                    previous = residuals[job_id]
                    residuals[job_id] = {cell: previous[cell] for cell in router.routes[job_id]
                                         if cell in previous}
                if moved:
                    field = None
            if not has_force:
                break

        snapshot = dict(router.routes)
        stationary = {i: path for i, path in snapshot.items() if path == starting[i]}
        if stationary:
            if field is None:
                field = _RepulsionField(router, snapshot, fixed)
            proposals = {}
            for job_id, path in stationary.items():
                job = router.jobs_by_id[job_id]
                move = _settling_move(router, job, path, field, contacts.get(job.net, {}), shapes)
                if move is not None:
                    proposals[job_id] = move
            if proposals:
                if progress and progress(passes, PASSES + 1, 'Repulsion settling') is False:
                    router._restore_routes(starting)
                    return passes, 0, True
                passes += 1
                _apply_batch(router, snapshot, proposals)
    except BaseException:
        router._restore_routes(starting)
        raise
    moved = sum(path != starting[job_id] for job_id, path in router.routes.items())
    return passes, moved, False
