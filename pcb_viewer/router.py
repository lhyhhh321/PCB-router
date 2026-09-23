"""Single-layer PCB detailed router inspired by TritonRoute's routing flow.

This is deliberately a PCB router, not a port of TritonRoute.  It borrows the
useful architecture: pin access, ordered initial maze routing, a small DRC
model, and iterative rip-up/search-and-repair with historical costs.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import heapq
import itertools
import math
import time
from typing import Callable, Iterable


Point = tuple[float, float]
Cell = tuple[int, int]
Progress = Callable[[int, int, str], bool | None]


@dataclass
class RouterConfig:
    grid_step: float = 2.0
    clearance: float | None = None
    iterations: int = 6
    guide_margin: float = 40.0
    boundary_margin: float = 180.0
    bend_penalty: float = 0.35
    present_congestion_cost: float = 24.0
    history_cost: float = 5.0
    max_expansions: int = 250_000
    layer: str | None = None
    use_routing_guides: bool = True
    guide_cost: float = 0.8

    def validate(self) -> None:
        if not math.isfinite(self.grid_step) or self.grid_step <= 0:
            raise ValueError("Routing grid step must be positive.")
        if self.clearance is not None and (not math.isfinite(self.clearance) or self.clearance < 0):
            raise ValueError("Routing clearance cannot be negative.")
        if not 1 <= self.iterations <= 64:
            raise ValueError("Search-and-repair iterations must be between 1 and 64.")
        if self.boundary_margin < 0 or self.guide_margin < 0:
            raise ValueError("Routing margins cannot be negative.")
        if self.max_expansions <= 0:
            raise ValueError("Maximum A* expansions must be positive.")
        for label, value in (("bend penalty", self.bend_penalty),
                             ("present congestion cost", self.present_congestion_cost),
                             ("history cost", self.history_cost),
                             ("guide cost", self.guide_cost)):
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"Routing {label} cannot be negative.")


@dataclass(frozen=True)
class RoutingJob:
    id: int
    net: str
    start: Point
    goal: Point
    width: float
    layer: str
    route_type: str = "route"
    guide: tuple[Point, ...] = ()


@dataclass
class RouteReport:
    layer: str
    jobs_total: int
    routed: int = 0
    failed: list[dict] = field(default_factory=list)
    conflict_cells: int = 0
    conflicting_jobs: list[int] = field(default_factory=list)
    repair_iterations: int = 0
    wire_length: float = 0.0
    elapsed_seconds: float = 0.0
    cancelled: bool = False
    config: dict = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return not self.failed and not self.conflict_cells and not self.cancelled

    def to_dict(self) -> dict:
        result = asdict(self)
        result["clean"] = self.clean
        return result


def _number(value, fallback: float) -> float:
    return float(value) if isinstance(value, (int, float)) and math.isfinite(value) else fallback


def extract_jobs(data: dict, layer: str | None = None) -> list[RoutingJob]:
    """Read route requests without using archived paths as routing geometry.

    The supplied testcase stores its unrouted two-terminal connections under
    ``deleted_connections.wires``.  Endpoints, net, width and layer define the
    request; the interior polyline is retained only as a guide and is heavily
    coarsened before routing.  A generic producer can instead emit
    ``routing_connections`` records with ``start`` and ``end`` pairs.
    """
    default_layer = layer or data.get("layer")
    records = data.get("routing_connections")
    archived = False
    if not isinstance(records, list):
        deleted = data.get("deleted_connections", {})
        records = deleted.get("wires", []) if isinstance(deleted, dict) else []
        archived = True
    jobs: list[RoutingJob] = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        points = record.get("points") if archived else None
        start = points[0] if isinstance(points, list) and len(points) >= 2 else record.get("start")
        goal = points[-1] if isinstance(points, list) and len(points) >= 2 else record.get("end", record.get("goal"))
        if (not isinstance(start, (list, tuple)) or not isinstance(goal, (list, tuple)) or
                len(start) != 2 or len(goal) != 2):
            continue
        try:
            start_point = (float(start[0]), float(start[1]))
            goal_point = (float(goal[0]), float(goal[1]))
        except (TypeError, ValueError):
            continue
        if not all(math.isfinite(v) for v in start_point + goal_point):
            continue
        net = record.get("net")
        job_layer = record.get("layer", default_layer)
        if not isinstance(net, str) or not net or not isinstance(job_layer, str) or not job_layer:
            continue
        if layer and job_layer != layer:
            continue
        width = _number(record.get("width"), 2.0)
        if width <= 0:
            continue
        raw_guide = points if archived else record.get("guide", [])
        guide = ()
        if isinstance(raw_guide, list) and len(raw_guide) >= 2:
            try:
                candidate = tuple((float(p[0]), float(p[1])) for p in raw_guide)
                if all(math.isfinite(v) for point in candidate for v in point):
                    guide = candidate
            except (TypeError, ValueError, IndexError):
                pass
        jobs.append(RoutingJob(index, net, start_point, goal_point, width, job_layer,
                               str(record.get("route_type", "route")), guide))
    return jobs


class Autorouter:
    """DRC-aware octilinear maze router with negotiated congestion."""

    DIRECTIONS = ((1, 0), (1, 1), (0, 1), (-1, 1),
                  (-1, 0), (-1, -1), (0, -1), (1, -1))

    def __init__(self, data: dict, config: RouterConfig | None = None):
        self.data = data
        self.config = config or RouterConfig()
        self.config.validate()
        self.jobs = extract_jobs(data, self.config.layer)
        if not self.jobs:
            raise ValueError("No routing requests found. Expected routing_connections or deleted_connections.wires.")
        layers = {job.layer for job in self.jobs}
        if len(layers) != 1:
            raise ValueError("This router currently accepts one signal layer per run.")
        self.layer = next(iter(layers))
        rules = data.get("metadata", {}).get("design_rules", {})
        self.clearance = (self.config.clearance if self.config.clearance is not None
                          else _number(rules.get("clearance_mil"), 2.0))
        self.step = self.config.grid_step
        self._build_region()
        self.static: dict[int, set[str | None]] = {}
        self.usage: dict[int, set[int]] = {}
        self.footprints: dict[int, set[int]] = {}
        self.routes: dict[int, list[Cell]] = {}
        self.history: dict[int, int] = {}
        self.jobs_by_id = {job.id: job for job in self.jobs}
        self.max_half_width = max(job.width for job in self.jobs) / 2
        self.coarse_guides = {
            job.id: self._simplify_guide(job.guide, max(self.step, self.config.guide_margin / 2))
            for job in self.jobs if self.config.use_routing_guides and len(job.guide) >= 2
        }
        self._offset_cache: dict[tuple[int, int], tuple[Cell, ...]] = {}
        self._rasterize_board()

    def _build_region(self) -> None:
        routing_points = [point for job in self.jobs
                          for point in ((job.guide if self.config.use_routing_guides and job.guide
                                         else (job.start, job.goal)))]
        xs = [point[0] for point in routing_points]
        ys = [point[1] for point in routing_points]
        margin = self.config.boundary_margin
        self.origin_x = math.floor((min(xs) - margin) / self.step) * self.step
        self.origin_y = math.floor((min(ys) - margin) / self.step) * self.step
        max_x = math.ceil((max(xs) + margin) / self.step) * self.step
        max_y = math.ceil((max(ys) + margin) / self.step) * self.step
        self.cols = int(round((max_x - self.origin_x) / self.step)) + 1
        self.rows = int(round((max_y - self.origin_y) / self.step)) + 1

    def _key(self, cell: Cell) -> int:
        return cell[1] * self.cols + cell[0]

    def _cell_from_key(self, key: int) -> Cell:
        return key % self.cols, key // self.cols

    def _inside(self, cell: Cell) -> bool:
        return 0 <= cell[0] < self.cols and 0 <= cell[1] < self.rows

    def _point(self, cell: Cell) -> Point:
        return self.origin_x + cell[0] * self.step, self.origin_y + cell[1] * self.step

    def _cell(self, point: Point) -> Cell:
        return (int(round((point[0] - self.origin_x) / self.step)),
                int(round((point[1] - self.origin_y) / self.step)))

    def _add_static(self, cell: Cell, owner: str | None) -> None:
        if self._inside(cell):
            self.static.setdefault(self._key(cell), set()).add(owner)

    def _indices(self, low: float, high: float, origin: float, limit: int) -> range:
        first = max(0, int(math.floor((low - origin) / self.step)))
        last = min(limit - 1, int(math.ceil((high - origin) / self.step)))
        return range(first, last + 1)

    def _mark_disk(self, x: float, y: float, radius: float, owner: str | None) -> None:
        pad = self.step * 0.55
        for j in self._indices(y - radius - pad, y + radius + pad, self.origin_y, self.rows):
            py = self.origin_y + j * self.step
            for i in self._indices(x - radius - pad, x + radius + pad, self.origin_x, self.cols):
                px = self.origin_x + i * self.step
                if (px - x) ** 2 + (py - y) ** 2 <= (radius + pad) ** 2:
                    self._add_static((i, j), owner)

    def _mark_rect(self, x1: float, y1: float, x2: float, y2: float, owner: str | None) -> None:
        for j in self._indices(min(y1, y2), max(y1, y2), self.origin_y, self.rows):
            for i in self._indices(min(x1, x2), max(x1, x2), self.origin_x, self.cols):
                self._add_static((i, j), owner)

    @staticmethod
    def _distance_to_segment(px: float, py: float, a: Point, b: Point) -> float:
        dx, dy = b[0] - a[0], b[1] - a[1]
        length2 = dx * dx + dy * dy
        if length2 == 0:
            return math.hypot(px - a[0], py - a[1])
        t = max(0.0, min(1.0, ((px - a[0]) * dx + (py - a[1]) * dy) / length2))
        return math.hypot(px - (a[0] + t * dx), py - (a[1] + t * dy))

    def _mark_segment(self, a: Point, b: Point, radius: float, owner: str | None) -> None:
        pad = radius + self.step * 0.55
        for j in self._indices(min(a[1], b[1]) - pad, max(a[1], b[1]) + pad,
                               self.origin_y, self.rows):
            py = self.origin_y + j * self.step
            for i in self._indices(min(a[0], b[0]) - pad, max(a[0], b[0]) + pad,
                                   self.origin_x, self.cols):
                px = self.origin_x + i * self.step
                if self._distance_to_segment(px, py, a, b) <= pad:
                    self._add_static((i, j), owner)

    def _mark_polygon(self, points: Iterable[Iterable[float]], owner: str | None) -> None:
        pts = [(float(p[0]), float(p[1])) for p in points]
        if len(pts) < 3:
            return
        if pts[0] == pts[-1]:
            pts.pop()
        # Scanline fill is much faster than point-in-polygon for large pours.
        min_y, max_y = min(p[1] for p in pts), max(p[1] for p in pts)
        edges = list(zip(pts, pts[1:] + pts[:1]))
        for j in self._indices(min_y, max_y, self.origin_y, self.rows):
            y = self.origin_y + j * self.step
            crossings = []
            for a, b in edges:
                if (a[1] <= y < b[1]) or (b[1] <= y < a[1]):
                    crossings.append(a[0] + (y - a[1]) * (b[0] - a[0]) / (b[1] - a[1]))
            crossings.sort()
            for left, right in zip(crossings[0::2], crossings[1::2]):
                for i in self._indices(left, right, self.origin_x, self.cols):
                    x = self.origin_x + i * self.step
                    if left - self.step * 0.1 <= x <= right + self.step * 0.1:
                        self._add_static((i, j), owner)
        for a, b in edges:
            self._mark_segment(a, b, 0.0, owner)

    def _applies_to_layer(self, obj: dict) -> bool:
        obj_layer = obj.get("layer", self.data.get("layer"))
        layers = obj.get("layers", [])
        return (not obj_layer or obj_layer == self.layer) and (not layers or self.layer in layers)

    def _rasterize_board(self) -> None:
        # Fixed copper and terminals are obstacles to other nets, but conductive
        # access geometry for their own net.
        for group in ("pins", "pads", "vias"):
            for obj in self.data.get(group, []):
                if not isinstance(obj, dict) or not self._applies_to_layer(obj):
                    continue
                x, y = obj.get("x"), obj.get("y")
                if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
                    continue
                owner = obj.get("net") if isinstance(obj.get("net"), str) else None
                shape = obj.get("shape")
                if isinstance(shape, dict) and shape.get("shape") == "rect":
                    self._mark_rect(x + shape["x1"], y + shape["y1"],
                                    x + shape["x2"], y + shape["y2"], owner)
                else:
                    fallback = 3.0 if group == "vias" else 4.0
                    radius = _number(shape.get("radius"), fallback) if isinstance(shape, dict) else fallback
                    self._mark_disk(float(x), float(y), max(0.0, radius), owner)
        for obj in self.data.get("copper_polygons", []):
            if isinstance(obj, dict) and self._applies_to_layer(obj):
                owner = obj.get("net") if isinstance(obj.get("net"), str) else None
                self._mark_polygon(obj.get("vertices", []), owner)
        for obj in self.data.get("wires", []):
            if not isinstance(obj, dict) or not self._applies_to_layer(obj):
                continue
            owner = obj.get("net") if isinstance(obj.get("net"), str) else None
            radius = max(0.0, _number(obj.get("width"), 2.0) / 2)
            points = obj.get("points", [])
            for a, b in zip(points, points[1:]):
                self._mark_segment(tuple(a), tuple(b), radius, owner)
        for obj in self.data.get("keepouts", []):
            if (isinstance(obj, dict) and obj.get("type", "wire_keepout") == "wire_keepout"
                    and self._applies_to_layer(obj)):
                self._mark_polygon(obj.get("points", []), None)

    def _offsets(self, radius: float) -> tuple[Cell, ...]:
        cells = max(0, int(math.ceil(radius / self.step)))
        cache_key = cells, int(round(radius / self.step * 1000))
        if cache_key not in self._offset_cache:
            physical = radius + self.step * 0.35
            self._offset_cache[cache_key] = tuple(
                (dx, dy) for dy in range(-cells, cells + 1) for dx in range(-cells, cells + 1)
                if (dx * self.step) ** 2 + (dy * self.step) ** 2 <= physical ** 2
            ) or ((0, 0),)
        return self._offset_cache[cache_key]

    def _static_legal(self, cell: Cell, job: RoutingJob, endpoint: bool = False) -> bool:
        if not self._inside(cell):
            return False
        radius = job.width / 2 + self.clearance
        for dx, dy in self._offsets(radius):
            nearby = (cell[0] + dx, cell[1] + dy)
            if not self._inside(nearby):
                return False
            owners = self.static.get(self._key(nearby))
            if owners and (None in owners or any(owner != job.net for owner in owners)):
                # The exact access cell may sit inside imperfect/stacked source
                # data.  Only that cell is waived; the escape still must clear.
                if not endpoint:
                    return False
        return True

    def _dynamic_cost(self, cell: Cell, job: RoutingJob, present_factor: float) -> float:
        conflicts: set[int] = set()
        historical = 0
        search_radius = job.width / 2 + self.clearance + self.max_half_width
        x, y = self._point(cell)
        for dx, dy in self._offsets(search_radius):
            nearby = (cell[0] + dx, cell[1] + dy)
            if not self._inside(nearby):
                continue
            key = self._key(nearby)
            for other_id in self.usage.get(key, ()):
                other = self.jobs_by_id[other_id]
                required = job.width / 2 + other.width / 2 + self.clearance
                ox, oy = self._point(nearby)
                if (other.net != job.net and
                        (x - ox) ** 2 + (y - oy) ** 2 < (required - 1e-7) ** 2):
                    conflicts.add(other_id)
            historical += self.history.get(key, 0)
        return present_factor * len(conflicts) + self.config.history_cost * historical

    def _guide_bounds(self, job: RoutingJob, margin: float) -> tuple[int, int, int, int]:
        guide = job.guide if self.config.use_routing_guides and job.guide else (job.start, job.goal)
        cells_in_guide = [self._cell(point) for point in guide]
        cells = int(math.ceil(margin / self.step))
        return (max(0, min(point[0] for point in cells_in_guide) - cells),
                min(self.cols - 1, max(point[0] for point in cells_in_guide) + cells),
                max(0, min(point[1] for point in cells_in_guide) - cells),
                min(self.rows - 1, max(point[1] for point in cells_in_guide) + cells))

    @staticmethod
    def _heuristic(a: Cell, b: Cell) -> float:
        dx, dy = abs(a[0] - b[0]), abs(a[1] - b[1])
        return max(dx, dy) + (math.sqrt(2) - 1) * min(dx, dy)

    def _astar(self, job: RoutingJob, bounds: tuple[int, int, int, int],
               present_factor: float) -> list[Cell] | None:
        start, goal = self._cell(job.start), self._cell(job.goal)
        if start == goal:
            return [start]
        x1, x2, y1, y2 = bounds
        counter = itertools.count()
        best = {start: 0.0}
        incoming = {start: -1}
        previous: dict[Cell, Cell] = {}
        legal_cache: dict[Cell, bool] = {}
        dynamic_cache: dict[Cell, float] = {}
        guide_cache: dict[Cell, float] = {}
        queue = [(self._heuristic(start, goal), next(counter), 0.0, start, -1)]
        expansions = 0
        found = False
        while queue and expansions < self.config.max_expansions:
            _, _, cost, cell, old_direction = heapq.heappop(queue)
            if cost != best.get(cell) or old_direction != incoming.get(cell):
                continue
            if cell == goal:
                found = True
                break
            expansions += 1
            for direction, (dx, dy) in enumerate(self.DIRECTIONS):
                if old_direction >= 0 and direction == (old_direction + 4) % 8:
                    continue
                nxt = cell[0] + dx, cell[1] + dy
                if not (x1 <= nxt[0] <= x2 and y1 <= nxt[1] <= y2):
                    continue
                legal = legal_cache.get(nxt)
                if legal is None:
                    legal = self._static_legal(nxt, job, nxt == goal)
                    legal_cache[nxt] = legal
                if not legal:
                    continue
                move = math.sqrt(2) if dx and dy else 1.0
                bend = self.config.bend_penalty if old_direction >= 0 and old_direction != direction else 0.0
                dynamic = dynamic_cache.get(nxt)
                if dynamic is None:
                    dynamic = self._dynamic_cost(nxt, job, present_factor)
                    dynamic_cache[nxt] = dynamic
                guide_cost = guide_cache.get(nxt)
                if guide_cost is None:
                    guide_cost = self._routing_guide_cost(nxt, job)
                    guide_cache[nxt] = guide_cost
                new_cost = cost + move + bend + dynamic + guide_cost
                if new_cost + 1e-12 < best.get(nxt, math.inf):
                    best[nxt] = new_cost
                    incoming[nxt] = direction
                    previous[nxt] = cell
                    estimate = new_cost + self._heuristic(nxt, goal)
                    heapq.heappush(queue, (estimate, next(counter), new_cost, nxt, direction))
        if not found:
            return None
        path = [goal]
        cell = goal
        while cell != start:
            cell = previous[cell]
            path.append(cell)
        path.reverse()
        return path

    @staticmethod
    def _simplify_guide(points: tuple[Point, ...], tolerance: float) -> tuple[Point, ...]:
        """Douglas-Peucker coarsening turns detailed archives into route guides."""
        if len(points) <= 2:
            return points
        a, b = points[0], points[-1]
        distances = [Autorouter._distance_to_segment(point[0], point[1], a, b)
                     for point in points[1:-1]]
        maximum = max(distances, default=0.0)
        if maximum <= tolerance:
            return a, b
        split = distances.index(maximum) + 1
        left = Autorouter._simplify_guide(points[:split + 1], tolerance)
        right = Autorouter._simplify_guide(points[split:], tolerance)
        return left[:-1] + right

    def _routing_guide_cost(self, cell: Cell, job: RoutingJob) -> float:
        if not self.config.use_routing_guides or len(job.guide) < 2:
            return 0.0
        guide = self.coarse_guides[job.id]
        x, y = self._point(cell)
        distance = min(self._distance_to_segment(x, y, a, b) for a, b in zip(guide, guide[1:]))
        return self.config.guide_cost * min(20.0, distance / self.step)

    @staticmethod
    def _grid_line(a: Cell, b: Cell) -> list[Cell]:
        """Integer supercover-like line for a coarse guide candidate."""
        x, y = a
        dx, sx = abs(b[0] - x), 1 if x < b[0] else -1
        dy, sy = -abs(b[1] - y), 1 if y < b[1] else -1
        error = dx + dy
        result = []
        while True:
            result.append((x, y))
            if (x, y) == b:
                return result
            twice = 2 * error
            if twice >= dy:
                error += dy
                x += sx
            if twice <= dx:
                error += dx
                y += sy

    def _guided_pattern(self, job: RoutingJob, present_factor: float) -> list[Cell] | None:
        if not self.config.use_routing_guides or len(job.guide) < 2:
            return None
        coarse = self.coarse_guides[job.id]
        waypoints = [self._cell(point) for point in coarse]
        path: list[Cell] = []
        for a, b in zip(waypoints, waypoints[1:]):
            segment = self._grid_line(a, b)
            path.extend(segment if not path else segment[1:])
        if not path:
            return None
        for index, cell in enumerate(path):
            if not self._static_legal(cell, job, index in (0, len(path) - 1)):
                return None
            if self._dynamic_cost(cell, job, present_factor):
                return None
        return path

    def _pattern_paths(self, job: RoutingJob) -> Iterable[list[Cell]]:
        """Two shortest octilinear patterns used before invoking maze search."""
        start, goal = self._cell(job.start), self._cell(job.goal)
        dx, dy = goal[0] - start[0], goal[1] - start[1]
        sx, sy = (1 if dx > 0 else -1 if dx < 0 else 0), (1 if dy > 0 else -1 if dy < 0 else 0)
        diagonal = min(abs(dx), abs(dy))
        axial_x, axial_y = abs(dx) - diagonal, abs(dy) - diagonal
        sequences = [((sx, sy), diagonal), ((sx, 0), axial_x), ((0, sy), axial_y)]
        alternatives = [sequences, list(reversed(sequences))]
        for sequence in alternatives:
            path = [start]
            for (mx, my), count in sequence:
                if not count:
                    continue
                for _ in range(count):
                    path.append((path[-1][0] + mx, path[-1][1] + my))
            if path[-1] == goal:
                yield path

    def _pattern_route(self, job: RoutingJob) -> list[Cell] | None:
        best_path = None
        best_cost = math.inf
        for path in self._pattern_paths(job):
            cost = 0.0
            old_direction = None
            valid = True
            for index, cell in enumerate(path[1:], 1):
                if not self._static_legal(cell, job, cell == path[-1]):
                    valid = False
                    break
                previous = path[index - 1]
                direction = (cell[0] - previous[0], cell[1] - previous[1])
                if old_direction is not None and old_direction != direction:
                    cost += self.config.bend_penalty
                old_direction = direction
                cost += (math.sqrt(2) if direction[0] and direction[1] else 1.0)
                cost += self._dynamic_cost(cell, job, self.config.present_congestion_cost)
            if valid and cost < best_cost:
                best_path, best_cost = path, cost
        return best_path

    def _route_job(self, job: RoutingJob, present_factor: float) -> list[Cell] | None:
        guided = self._guided_pattern(job, present_factor)
        if guided is not None:
            return guided
        pattern = self._pattern_route(job) if not (self.config.use_routing_guides and job.guide) else None
        if pattern is not None and not any(self._dynamic_cost(cell, job, present_factor) for cell in pattern):
            return pattern
        # Increasing guide boxes mimic detailed-route guide relaxation.
        margins = [self.config.guide_margin,
                   max(self.config.guide_margin * 2.5, 100.0),
                   self.config.boundary_margin]
        tried = set()
        for margin in margins:
            bounds = self._guide_bounds(job, margin)
            if bounds in tried:
                continue
            tried.add(bounds)
            route = self._astar(job, bounds, present_factor)
            if route is not None:
                return route
        return None

    def _commit(self, job: RoutingJob, path: list[Cell]) -> None:
        footprint = set()
        for cell in path:
            key = self._key(cell)
            self.usage.setdefault(key, set()).add(job.id)
            footprint.add(key)
        self.routes[job.id] = path
        self.footprints[job.id] = footprint

    def _remove(self, job_id: int) -> None:
        for key in self.footprints.pop(job_id, ()):
            users = self.usage.get(key)
            if users:
                users.discard(job_id)
                if not users:
                    del self.usage[key]
        self.routes.pop(job_id, None)

    def _conflicts(self) -> tuple[set[int], set[int]]:
        cells: set[int] = set()
        jobs: set[int] = set()
        checked: set[tuple[int, int]] = set()
        for key, users in self.usage.items():
            cell = self._cell_from_key(key)
            x, y = self._point(cell)
            for job_id in users:
                job = self.jobs_by_id[job_id]
                radius = job.width / 2 + self.clearance + self.max_half_width
                for dx, dy in self._offsets(radius):
                    nearby = cell[0] + dx, cell[1] + dy
                    if not self._inside(nearby):
                        continue
                    other_key = self._key(nearby)
                    for other_id in self.usage.get(other_key, ()):
                        pair = (min(job_id, other_id), max(job_id, other_id))
                        if job_id == other_id or pair in checked:
                            continue
                        other = self.jobs_by_id[other_id]
                        if other.net == job.net:
                            continue
                        ox, oy = self._point(nearby)
                        required = job.width / 2 + other.width / 2 + self.clearance
                        if (x - ox) ** 2 + (y - oy) ** 2 < (required - 1e-7) ** 2:
                            checked.add(pair)
                            cells.update((key, other_key))
                            jobs.update((job_id, other_id))
        return cells, jobs

    def _polyline(self, job: RoutingJob, cells: list[Cell]) -> list[list[float]]:
        points: list[Point] = [job.start]
        points.extend(self._point(cell) for cell in cells[1:-1])
        points.append(job.goal)
        deduped: list[Point] = []
        for point in points:
            rounded = (round(point[0], 6), round(point[1], 6))
            if not deduped or rounded != deduped[-1]:
                deduped.append(rounded)
        simplified: list[Point] = []
        for point in deduped:
            while len(simplified) >= 2:
                a, b = simplified[-2], simplified[-1]
                cross = (b[0] - a[0]) * (point[1] - b[1]) - (b[1] - a[1]) * (point[0] - b[0])
                dot = (b[0] - a[0]) * (point[0] - b[0]) + (b[1] - a[1]) * (point[1] - b[1])
                if abs(cross) > 1e-8 or dot < -1e-8:
                    break
                simplified.pop()
            simplified.append(point)
        return [[x, y] for x, y in simplified]

    def route(self, progress: Progress | None = None) -> tuple[list[dict], RouteReport]:
        started = time.monotonic()
        report = RouteReport(self.layer, len(self.jobs), config=asdict(self.config))
        ordered = sorted(self.jobs, key=lambda job: (-job.width, -math.dist(job.start, job.goal), job.id))

        def update(done: int, message: str) -> bool:
            if progress is None:
                return True
            return progress(done, len(self.jobs), message) is not False

        for done, job in enumerate(ordered):
            if not update(done, f"Initial routing: {job.net}"):
                report.cancelled = True
                break
            path = self._route_job(job, self.config.present_congestion_cost)
            if path is None:
                report.failed.append({"job": job.id, "net": job.net, "reason": "no path"})
            else:
                self._commit(job, path)

        initial_conflicts, _ = self._conflicts()
        best_conflict_count = len(initial_conflicts)
        best_routes = {job_id: list(path) for job_id, path in self.routes.items()}
        for iteration in range(1, self.config.iterations):
            conflict_cells, conflict_jobs = self._conflicts()
            if report.cancelled or not conflict_jobs:
                break
            report.repair_iterations = iteration
            for key in conflict_cells:
                self.history[key] = self.history.get(key, 0) + 1
            # Reroute the most conflicted/widest jobs first while retaining the
            # other routes as present-congestion guidance.
            counts = {job_id: sum(key in conflict_cells for key in self.footprints.get(job_id, ()))
                      for job_id in conflict_jobs}
            repair = sorted(conflict_jobs,
                            key=lambda job_id: (-counts[job_id], -self.jobs_by_id[job_id].width, job_id))
            for done, job_id in enumerate(repair):
                job = self.jobs_by_id[job_id]
                if not update(done, f"Repair {iteration}: {job.net}"):
                    report.cancelled = True
                    break
                old_path = self.routes.get(job_id)
                self._remove(job_id)
                path = self._route_job(job, self.config.present_congestion_cost * (iteration + 1))
                if path is None and old_path is not None:
                    path = old_path
                if path is not None:
                    self._commit(job, path)

            current_conflicts, _ = self._conflicts()
            if len(current_conflicts) < best_conflict_count:
                best_conflict_count = len(current_conflicts)
                best_routes = {job_id: list(path) for job_id, path in self.routes.items()}

        current_conflicts, _ = self._conflicts()
        if len(current_conflicts) > best_conflict_count:
            self.usage.clear()
            self.footprints.clear()
            self.routes.clear()
            for job_id, path in best_routes.items():
                self._commit(self.jobs_by_id[job_id], path)

        conflict_cells, conflict_jobs = self._conflicts()
        report.conflict_cells = len(conflict_cells)
        report.conflicting_jobs = sorted(conflict_jobs)
        wires = []
        for job in sorted(self.jobs, key=lambda value: value.id):
            path = self.routes.get(job.id)
            if path is None:
                continue
            points = self._polyline(job, path)
            report.wire_length += sum(math.dist(a, b) for a, b in zip(points, points[1:]))
            wires.append({"layer": job.layer, "width": job.width, "net": job.net,
                          "route_type": "autoroute", "points": points,
                          "autoroute_job": job.id})
        report.routed = len(wires)
        report.wire_length = round(report.wire_length, 3)
        report.elapsed_seconds = round(time.monotonic() - started, 3)
        update(len(self.jobs), "Routing complete")
        return wires, report


def autoroute_data(data: dict, config: RouterConfig | None = None,
                   progress: Progress | None = None) -> tuple[list[dict], RouteReport]:
    """Convenience API used by both the CLI and Qt application."""
    return Autorouter(data, config).route(progress)
