"""Board-wide spacing field and geometric trace compaction. No maze searches."""
from __future__ import annotations

import math


class BoardForceField:
    """A snapshot field, evaluated sparsely at movable trace control points.

    Each foreign net contributes its nearest sampled copper surface. The
    finite-range potential favors clearance-sized bundles, not route completion.
    Python and optional batched PyTorch evaluate the same force and potential.
    """

    def __init__(self, sources, clearance, step, strength, backend=None):
        self.sources = sources  # net -> [(x, y, half_width), ...]
        self.clearance, self.step, self.strength = clearance, step, strength
        self.reach = 16 * step
        self.backend = backend
        self.max_half = max((p[2] for points in sources.values() for p in points), default=0)
        self.buckets = {}
        if backend is None:
            for net, points in sources.items():
                for sx, sy, half in points:
                    self.buckets.setdefault((math.floor(sx/self.reach), math.floor(sy/self.reach)), []).append((net, sx, sy, half))

    def evaluate(self, queries):
        # query = (x, y, half_width, net)
        if self.backend is not None:
            return self._torch(queries)
        result = []
        for x, y, half, net in queries:
            fx = fy = energy = 0.0
            nearest = {}
            radius = half + self.max_half + self.clearance + self.step + self.reach
            for bx in range(math.floor((x-radius)/self.reach), math.floor((x+radius)/self.reach)+1):
                for by in range(math.floor((y-radius)/self.reach), math.floor((y+radius)/self.reach)+1):
                    for other_net, sx, sy, shalf in self.buckets.get((bx, by), ()):
                        if other_net == net:
                            continue
                        dx, dy = sx - x, sy - y
                        distance = math.hypot(dx, dy)
                        surface = distance - shalf
                        previous = nearest.get(other_net)
                        if previous is None or surface < previous[0]:
                            nearest[other_net] = (surface, distance, dx, dy, shalf)
            for _, distance, dx, dy, shalf in nearest.values():
                target = half + shalf + self.clearance + self.step
                gap = distance - target
                if gap >= self.reach:
                    continue
                if gap < 0:
                    magnitude = 4 * gap / target
                    energy += 2 * gap * gap / target - self.strength * self.reach / 2
                else:
                    magnitude = self.strength * gap / self.reach
                    energy += self.strength * (gap * gap / self.reach - self.reach) / 2
                if distance > 1e-9:
                    fx += magnitude * dx / distance
                    fy += magnitude * dy / distance
            result.append((fx, fy, energy))
        return result

    def _torch(self, queries):
        torch, device = self.backend.torch, self.backend.device
        if not queries:
            return []
        result = []
        with torch.inference_mode():
            source_tensors = {net: torch.tensor(points, dtype=torch.float32, device=device)
                              for net, points in self.sources.items() if points}
            for start in range(0, len(queries), 128):
                batch = queries[start:start+128]
                q = torch.tensor([row[:3] for row in batch], dtype=torch.float32, device=device)
                force = torch.zeros((len(batch), 3), device=device)
                for net, source in source_tensors.items():
                    # Explicit differences avoid cdist's cancellation-prone MM
                    # formulation on large board coordinates; bound scratch RAM.
                    best = torch.full((len(batch),), float('inf'), device=device)
                    selected = torch.zeros((len(batch), 3), device=device)
                    for offset in range(0, len(source), 2048):
                        block = source[offset:offset+2048]
                        delta = block[None, :, :2] - q[:, None, :2]
                        distances = torch.sqrt((delta * delta).sum(dim=2))
                        values, indices = (distances - block[None, :, 2]).min(dim=1)
                        better = values < best
                        selected = torch.where(better[:, None], block[indices], selected)
                        best = torch.minimum(best, values)
                    delta = selected[:, :2] - q[:, :2]
                    distance = torch.sqrt((delta * delta).sum(dim=1))
                    target = q[:, 2] + selected[:, 2] + self.clearance + self.step
                    gap = distance - target
                    active = torch.tensor([row[3] != net for row in batch], device=device) & (gap < self.reach)
                    magnitude = torch.where(gap < 0, 4 * gap / target,
                                            self.strength * gap / self.reach)
                    energy = torch.where(gap < 0, 2 * gap * gap / target - self.strength*self.reach/2,
                                         self.strength*(gap*gap/self.reach-self.reach)/2)
                    force[:, :2] += torch.where((active & (distance > 1e-9))[:, None],
                        magnitude[:, None] * delta / distance.clamp_min(1e-9)[:, None], 0)
                    force[:, 2] += torch.where(active, energy, 0)
                result.extend(tuple(row) for row in force.cpu().tolist())
        return result


def compact(router, progress=None):
    """Deform all eligible trace nets, preserving terminals and junctions."""
    from .router import RoutingJob

    if not router.config.force_iterations or not router.config.force_max_jobs:
        return 0, 0, False
    original_data = router.data
    old_max_width = router.max_half_width
    promoted = {}
    seed_paths = {}
    original_wires = original_data.get('wires', [])
    next_id = max(router.jobs_by_id, default=-1) + 1
    for index, wire in enumerate(original_wires):
        points = wire.get('points', [])
        if (not router._applies_to_layer(wire) or not wire.get('net') or
                wire.get('locked') or len(points) < 2):
            continue
        job = RoutingJob(next_id, wire['net'], tuple(points[0]), tuple(points[-1]),
                         float(wire.get('width', 2)), router.layer)
        path = [router._cell(points[0])]
        for a, b in zip(points, points[1:]):
            path.extend(router._grid_line(router._cell(a), router._cell(b))[1:])
        router.jobs_by_id[next_id] = job
        router._commit(job, path)
        seed_paths[next_id] = list(path)
        promoted[next_id] = index
        next_id += 1
    router.max_half_width = max(job.width / 2 for job in router.jobs_by_id.values())
    promoted_indices = set(promoted.values())
    router.data = {**original_data, 'wires': [w for i, w in enumerate(original_wires)
                                           if i not in promoted_indices]}
    router.static.clear()
    router._rasterize_board()
    starting = {i: list(path) for i, path in router.routes.items()}
    moved_ids = set()
    rounds = 0
    cancelled = False
    router.compaction_results = []

    # Pads, vias and pours stay anchored across all compaction rounds.
    tiles = {}
    for key, nets in router.static.items():
        cell = router._cell_from_key(key)
        for net in nets:
            if net:
                tiles.setdefault((net, cell[0]//4, cell[1]//4), cell)
    fixed_samples = [(net, (*router._point(cell), 0.0)) for (net, _, _), cell in tiles.items()]

    def field_state():
        sources = {}
        queries, spans = [], {}
        for job_id, path in sorted(router.routes.items()):
            job = router.jobs_by_id[job_id]
            stride = max(1, math.ceil(len(path) / 256))
            samples = path[::stride] + [path[-1]]
            sources.setdefault(job.net, []).extend((*router._point(cell), job.width/2) for cell in samples)
            indices = sorted(set([0, *range(1, len(path)-1, max(1, math.ceil(len(path)/32))), len(path)-1]))
            spans[job_id] = (len(queries), indices)
            queries.extend((*router._point(path[i]), job.width/2, job.net) for i in indices)
        for net, sample in fixed_samples:
            sources.setdefault(net, []).append(sample)
        field = BoardForceField(sources, router.clearance, router.step,
                                router.config.attraction_strength, router.torch_costs)
        values = field.evaluate(queries)
        # Normalize per trace: adding vertices must not manufacture a lower
        # potential just by increasing the number of negative-energy samples.
        energy = sum(sum(v[2] for v in values[offset:offset+len(indices)]) / len(indices)
                     for offset, indices in spans.values())
        return values, spans, energy

    def all_wires():
        return [{'net': router.jobs_by_id[i].net, 'autoroute_job': i,
                 'points': router._polyline(router.jobs_by_id[i], path)}
                for i, path in router.routes.items()]

    residuals = {}
    try:
        for iteration in range(router.config.force_iterations):
            rounds += 1
            if progress and progress(iteration, router.config.force_iterations,
                                     f'Board force compaction {iteration + 1}') is False:
                cancelled = True
                break
            snapshot = {i: list(path) for i, path in router.routes.items()}
            vectors, spans, old_energy = field_state()
            before_conflicts = len(router._conflicts()[0])
            before_crossings = router._geometric_crossings(all_wires())
            pins = {}
            for i, path in snapshot.items():
                net = router.jobs_by_id[i].net
                pins[i] = {path[0], path[-1]}
                for cell in path:
                    key = router._key(cell)
                    if net in router.static.get(key, ()) or any(
                        other != i and router.jobs_by_id[other].net == net
                        for other in router.usage.get(key, ())):
                        pins[i].add(cell)
            # A same-net diagonal junction can lie at a half-grid coordinate.
            for square, users in router.diagonal_usage.items():
                for i in users:
                    if any(j != i and users[i] != users[j] and
                           router.jobs_by_id[i].net == router.jobs_by_id[j].net for j in users):
                        pins[i].update((square, (square[0]+1, square[1]),
                                        (square[0], square[1]+1), (square[0]+1, square[1]+1)))
            changed = set()
            for i, old_path in sorted(snapshot.items()):
                job = router.jobs_by_id[i]
                offset, indices = spans[i]
                displacements = []
                for vx, vy, _ in vectors[offset:offset+len(indices)]:
                    scale = min(2.0, 2.0 / max(math.hypot(vx, vy), 1e-9))
                    displacements.append((vx*scale, vy*scale))  # grid units
                # Accumulate sub-grid force while no legal grid move is possible.
                residual = residuals.setdefault(i, {})
                controls = []
                segment = 0
                for k, cell in enumerate(old_path):
                    while segment + 1 < len(indices)-1 and k > indices[segment+1]:
                        segment += 1
                    end = min(segment+1, len(indices)-1)
                    t = (k-indices[segment]) / max(1, indices[end]-indices[segment])
                    dx = displacements[segment][0]*(1-t) + displacements[end][0]*t
                    dy = displacements[segment][1]*(1-t) + displacements[end][1]*t
                    # Normal motion compacts corridors without sliding copper
                    # along itself. Taper the field into pinned terminals.
                    before = old_path[max(0, k-1)]
                    after = old_path[min(len(old_path)-1, k+1)]
                    tx, ty = after[0]-before[0], after[1]-before[1]
                    tangent2 = tx*tx + ty*ty
                    if tangent2:
                        along = (dx*tx + dy*ty) / tangent2
                        dx, dy = dx-along*tx, dy-along*ty
                    taper = min(1.0, k/4, (len(old_path)-1-k)/4)
                    dx, dy = dx*taper, dy*taper
                    previous = residual.get(cell, (0, 0))
                    shift = (max(-2, min(2, previous[0]+dx)), max(-2, min(2, previous[1]+dy)))
                    residual[cell] = shift
                    controls.append(cell if cell in pins[i] else
                                    (cell[0]+round(shift[0]), cell[1]+round(shift[1])))
                candidate = [controls[0]]
                for a, b in zip(controls, controls[1:]):
                    candidate.extend(router._grid_line(a, b)[1:])
                if (candidate == old_path or len(set(candidate)) != len(candidate) or
                        not pins[i].issubset(candidate) or
                        router._path_length(candidate) > router._path_length(starting[i])*1.3 + 4*router.step):
                    continue
                router._remove(i)
                legal = all(router._inside(cell) and router._static_legal(cell, job, k in (0, len(candidate)-1))
                            for k, cell in enumerate(candidate)) and not router._path_blockers(job, candidate)
                router._commit(job, candidate if legal else old_path)
                if legal:
                    changed.add(i)
            new_energy = field_state()[2] if changed else old_energy
            accepted = (bool(changed) and new_energy < old_energy - 1e-6 and
                        len(router._conflicts()[0]) <= before_conflicts and
                        router._geometric_crossings(all_wires()) <= before_crossings)
            if not accepted:
                router._restore_routes(snapshot)
            else:
                moved_ids.update(changed)
                for i in changed:
                    residuals.pop(i, None)
            router.compaction_results.append({'iteration': iteration+1, 'energy_before': old_energy,
                'energy_after': new_energy if accepted else old_energy, 'moved_traces': len(changed) if accepted else 0})
        if cancelled:
            router._restore_routes(starting)
            moved_ids.clear()
        updated = list(original_wires)
        for job_id, index in promoted.items():
            if router.routes[job_id] != seed_paths[job_id]:
                updated[index] = {**original_wires[index],
                                  'points': router._polyline(router.jobs_by_id[job_id], router.routes[job_id])}
                router.wire_updates[router.input_wire_indices[index]] = updated[index]
        router.data = {**original_data, 'wires': updated}
    except BaseException:
        router._restore_routes(starting)
        router.data = original_data
        raise
    finally:
        for job_id in promoted:
            router._remove(job_id)
            del router.jobs_by_id[job_id]
        router.max_half_width = old_max_width
        router.static.clear()
        router._rasterize_board()
    return rounds, len(moved_ids), cancelled
