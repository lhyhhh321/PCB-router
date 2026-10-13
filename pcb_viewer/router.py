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
import random
import time
from typing import Callable, Iterable


Point = tuple[float, float]
Cell = tuple[int, int]
Progress = Callable[[int, int, str], bool | None]
RoutePreview = Callable[["Autorouter", int, int, str], None]


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
    use_routing_guides: bool = False
    guide_cost: float = 0.8
    hard_avoidance: bool = True
    max_shove_blockers: int = 4
    short_routes_first: bool = True
    ordering: str = "dense_first"
    random_rounds: int = 1
    random_seed: int = 0
    reverse_escape: bool = False
    strict_final: bool = True
    early_refine_interval: int = 192
    force_iterations: int = 6
    force_max_jobs: int = 12
    attraction_strength: float = 0.5
    max_detour_ratio: float = 3.0
    detailed_max_passes: int = 32
    detailed_drag_states: int = 12
    compute_backend: str = "python"

    def validate(self) -> None:
        if self.compute_backend not in ("python", "torch-cpu", "torch-mps", "torch-cuda", "torch-auto"):
            raise ValueError("Unknown routing compute backend.")
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
        if self.max_shove_blockers < 0:
            raise ValueError("Maximum shove blockers cannot be negative.")
        if self.early_refine_interval < 0:
            raise ValueError("Early refinement interval cannot be negative.")
        if not 0 <= self.force_iterations <= 20:
            raise ValueError("Force-refinement iterations must be between 0 and 20.")
        if self.force_max_jobs < 0:
            raise ValueError("Maximum force-refinement jobs cannot be negative.")
        if not math.isfinite(self.attraction_strength) or not 0 <= self.attraction_strength <= 2:
            raise ValueError("Attraction strength must be between 0 and 2.")
        if not math.isfinite(self.max_detour_ratio) or self.max_detour_ratio < 0:
            raise ValueError("Maximum detour ratio cannot be negative.")
        if not 0 <= self.detailed_max_passes <= 64:
            raise ValueError("Detailed-routing passes must be between 0 and 64.")
        if not 0 <= self.detailed_drag_states <= 64:
            raise ValueError("Detailed drag search states must be between 0 and 64.")
        if self.ordering not in ("short", "dense_first"):
            raise ValueError("Routing order must be short or dense_first.")
        if not isinstance(self.random_rounds, int) or not 1 <= self.random_rounds <= 16:
            raise ValueError("Randomized routing rounds must be between 1 and 16.")
        if not isinstance(self.random_seed, int):
            raise ValueError("Randomized routing seed must be an integer.")
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


@dataclass(frozen=True)
class DetailedTrace:
    """A routed connection as movable bends joined by straight grid segments."""

    job_id: int
    bend_indices: tuple[int, ...]
    bends: tuple[Cell, ...]

    @classmethod
    def from_path(cls, job_id: int, path: list[Cell]) -> DetailedTrace:
        indices = [0]
        for index in range(1, len(path) - 1):
            before = (path[index][0] - path[index - 1][0],
                      path[index][1] - path[index - 1][1])
            after = (path[index + 1][0] - path[index][0],
                     path[index + 1][1] - path[index][1])
            if before != after:
                indices.append(index)
        if len(path) > 1:
            indices.append(len(path) - 1)
        return cls(job_id, tuple(indices), tuple(path[index] for index in indices))

    @property
    def segments(self) -> tuple[tuple[Cell, Cell], ...]:
        return tuple(zip(self.bends, self.bends[1:]))


@dataclass
class RouteReport:
    layer: str
    jobs_total: int
    routed: int = 0
    failed: list[dict] = field(default_factory=list)
    conflict_cells: int = 0
    conflicting_jobs: list[int] = field(default_factory=list)
    repair_iterations: int = 0
    early_refinement_passes: int = 0
    early_refinement_moves: int = 0
    wire_length: float = 0.0
    elapsed_seconds: float = 0.0
    cancelled: bool = False
    config: dict = field(default_factory=dict)
    force_iterations_run: int = 0
    force_jobs_moved: int = 0
    force_new_connections: int = 0
    detailed_passes: int = 0
    detailed_straightened: int = 0
    detailed_dragged: int = 0
    detailed_new_connections: int = 0
    accelerated_searches: int = 0
    acceleration_fallbacks: int = 0
    selected_round: int = 1
    round_results: list[dict] = field(default_factory=list)

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
    ``deleted_connections.wires``.  Only endpoints, net, width and layer define
    the request; archived interior points are intentionally ignored so a
    reference route cannot leak into the result.  A generic producer can emit
    ``routing_connections`` records with ``start``/``end`` and an optional,
    explicitly authored ``guide``.
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
        raw_guide = [] if archived else record.get("guide", [])
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

    def __init__(self, data: dict, config: RouterConfig | None = None,
                 order_seed: int | None = None):
        self.data = data
        self.config = config or RouterConfig()
        self.config.validate()
        self.torch_costs = None
        if self.config.compute_backend != "python":
            from .torch_costs import TorchCosts
            self.torch_costs = TorchCosts(self.config.compute_backend.removeprefix("torch-"))
        self.order_seed = order_seed
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
        self.diagonal_usage: dict[Cell, dict[int, int]] = {}
        self.diagonal_footprints: dict[int, set[Cell]] = {}
        self.routes: dict[int, list[Cell]] = {}
        self.history: dict[int, int] = {}
        self.force_guides: dict[int, tuple[Point, ...]] = {}
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

    def _dynamic_context(self, cell: Cell, job: RoutingJob) -> tuple[set[int], int]:
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
        return conflicts, historical

    def _dynamic_cost_parts(self, cell: Cell, job: RoutingJob) -> tuple[int, int]:
        conflicts, historical = self._dynamic_context(cell, job)
        return len(conflicts), historical

    @staticmethod
    def _diagonal_square(a: Cell, b: Cell) -> tuple[Cell, int] | None:
        if abs(a[0] - b[0]) != 1 or abs(a[1] - b[1]) != 1:
            return None
        square = min(a[0], b[0]), min(a[1], b[1])
        orientation = 1 if (b[0] - a[0]) * (b[1] - a[1]) > 0 else -1
        return square, orientation

    def _diagonal_blockers(self, a: Cell, b: Cell, job: RoutingJob) -> set[int]:
        diagonal = self._diagonal_square(a, b)
        if diagonal is None:
            return set()
        square, orientation = diagonal
        return {other_id for other_id, other_orientation in
                self.diagonal_usage.get(square, {}).items()
                if other_orientation != orientation and
                self.jobs_by_id[other_id].net != job.net}

    def _dynamic_cost(self, cell: Cell, job: RoutingJob, present_factor: float) -> float:
        conflicts, historical = self._dynamic_cost_parts(cell, job)
        return present_factor * conflicts + self.config.history_cost * historical

    def _guide_bounds(self, job: RoutingJob, margin: float) -> tuple[int, int, int, int]:
        guide = (self.force_guides.get(job.id) or
                 (job.guide if self.config.use_routing_guides and job.guide
                  else (job.start, job.goal)))
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
               present_factor: float, hard_avoidance: bool = False,
               history_weight: float | None = None) -> list[Cell] | None:
        start, goal = self._cell(job.start), self._cell(job.goal)
        if start == goal:
            return [start]
        x1, x2, y1, y2 = bounds
        counter = itertools.count()
        best = {start: 0.0}
        incoming = {start: -1}
        previous: dict[Cell, Cell] = {}
        legal_cache: dict[Cell, bool] = {}
        dynamic_cache: dict[Cell, tuple[int, int]] = {}
        guide_cache: dict[Cell, float] = {}
        queue = [(self._heuristic(start, goal), next(counter), 0.0, start, -1)]
        expansions = 0
        found = False
        fields = None
        while queue and expansions < self.config.max_expansions:
            _, _, cost, cell, old_direction = heapq.heappop(queue)
            if cost != best.get(cell) or old_direction != incoming.get(cell):
                continue
            if cell == goal:
                found = True
                break
            expansions += 1
            # Small searches stay scalar; amortize tensor setup on larger ones.
            if expansions == 256 and self.torch_costs is not None:
                fields = self.torch_costs.build(self, job, bounds)
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
                dynamic_parts = dynamic_cache.get(nxt)
                if dynamic_parts is None:
                    if fields is None:
                        dynamic_parts = self._dynamic_cost_parts(nxt, job)
                    else:
                        index = (nxt[1] - y1) * (x2 - x1 + 1) + nxt[0] - x1
                        dynamic_parts = fields[0][index], fields[1][index]
                    dynamic_cache[nxt] = dynamic_parts
                conflicts, historical = dynamic_parts
                conflicts += len(self._diagonal_blockers(cell, nxt, job))
                if hard_avoidance and conflicts:
                    continue
                # History helps negotiate conflicts, but must not force a
                # conflict-free final path into a huge detour.
                weight = (0.0 if hard_avoidance else self.config.history_cost)
                if history_weight is not None:
                    weight = history_weight
                dynamic = present_factor * conflicts + weight * historical
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
        force_guide = self.force_guides.get(job.id)
        if not force_guide and not (self.config.use_routing_guides and len(job.guide) >= 2):
            return 0.0
        x, y = self._point(cell)
        cost = 0.0
        if self.config.use_routing_guides and len(job.guide) >= 2:
            guide = self.coarse_guides[job.id]
            distance = min(self._distance_to_segment(x, y, a, b)
                           for a, b in zip(guide, guide[1:]))
            cost += self.config.guide_cost * min(20.0, distance / self.step)
        if force_guide:
            distance = min(self._distance_to_segment(x, y, a, b)
                           for a, b in zip(force_guide, force_guide[1:]))
            cost += 2.0 * min(12.0, distance / self.step)
        return cost

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
            if index and self._diagonal_blockers(path[index - 1], cell, job):
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
                cost += self.config.present_congestion_cost * len(
                    self._diagonal_blockers(previous, cell, job))
            if valid and cost < best_cost:
                best_path, best_cost = path, cost
        return best_path

    def _route_job(self, job: RoutingJob, present_factor: float,
                   hard_avoidance: bool = False) -> list[Cell] | None:
        guided = self._guided_pattern(job, present_factor)
        if guided is not None:
            return guided
        pattern = self._pattern_route(job) if not (self.config.use_routing_guides and job.guide) else None
        if (pattern is not None and
                not any(self._dynamic_cost(cell, job, present_factor) for cell in pattern) and
                not any(self._diagonal_blockers(a, b, job) for a, b in zip(pattern, pattern[1:]))):
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
            route = self._astar(job, bounds, present_factor, hard_avoidance)
            if route is not None:
                return route
        return None

    def _route_reversed(self, job: RoutingJob, present_factor: float,
                        hard_avoidance: bool = True) -> list[Cell] | None:
        """Try the opposite search direction when a pin escape is asymmetric.

        The A* search waives fixed-copper collisions at its destination cell;
        dense terminal clusters can therefore have a viable escape in one
        direction even when the first search could not discover it.
        """
        backward = RoutingJob(job.id, job.net, job.goal, job.start, job.width,
                              job.layer, job.route_type, tuple(reversed(job.guide)))
        route = self._route_job(backward, present_factor, hard_avoidance)
        if route is None:
            route = self._astar(backward, (0, self.cols - 1, 0, self.rows - 1),
                                present_factor, hard_avoidance)
        return list(reversed(route)) if route is not None else None

    def _commit(self, job: RoutingJob, path: list[Cell]) -> None:
        footprint = set()
        diagonals = set()
        for cell in path:
            key = self._key(cell)
            self.usage.setdefault(key, set()).add(job.id)
            footprint.add(key)
        for a, b in zip(path, path[1:]):
            diagonal = self._diagonal_square(a, b)
            if diagonal is not None:
                square, orientation = diagonal
                self.diagonal_usage.setdefault(square, {})[job.id] = orientation
                diagonals.add(square)
        self.routes[job.id] = path
        self.footprints[job.id] = footprint
        self.diagonal_footprints[job.id] = diagonals

    def _remove(self, job_id: int) -> None:
        for key in self.footprints.pop(job_id, ()):
            users = self.usage.get(key)
            if users:
                users.discard(job_id)
                if not users:
                    del self.usage[key]
        self.routes.pop(job_id, None)
        for square in self.diagonal_footprints.pop(job_id, ()):
            users = self.diagonal_usage.get(square)
            if users:
                users.pop(job_id, None)
                if not users:
                    del self.diagonal_usage[square]

    def _path_blockers(self, job: RoutingJob, path: list[Cell]) -> set[int]:
        blockers: set[int] = set()
        for index, cell in enumerate(path):
            conflicts, _ = self._dynamic_context(cell, job)
            blockers.update(conflicts)
            if index:
                blockers.update(self._diagonal_blockers(path[index - 1], cell, job))
        return blockers

    def _conflict_details(self) -> tuple[set[int], set[tuple[int, int]]]:
        cells: set[int] = set()
        pairs: set[tuple[int, int]] = set()
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
                        if job_id == other_id:
                            continue
                        other = self.jobs_by_id[other_id]
                        if other.net == job.net:
                            continue
                        ox, oy = self._point(nearby)
                        required = job.width / 2 + other.width / 2 + self.clearance
                        if (x - ox) ** 2 + (y - oy) ** 2 < (required - 1e-7) ** 2:
                            cells.update((key, other_key))
                            pairs.add((min(job_id, other_id), max(job_id, other_id)))
        for square, users in self.diagonal_usage.items():
            for first, second in itertools.combinations(users, 2):
                if (users[first] != users[second] and
                        self.jobs_by_id[first].net != self.jobs_by_id[second].net):
                    cells.add(self._key(square))
                    pairs.add((min(first, second), max(first, second)))
        return cells, pairs

    def _conflicts(self) -> tuple[set[int], set[int]]:
        cells, pairs = self._conflict_details()
        jobs = {job_id for pair in pairs for job_id in pair}
        return cells, jobs

    def _ripup_and_reroute(self, iteration: int,
                           progress: Progress | None = None,
                           label: str = 'Repair',
                           focus_jobs: set[int] | None = None) -> tuple[bool, bool]:
        """One negotiated-congestion repair pass; return (worked, cancelled)."""
        conflict_cells, pairs = self._conflict_details()
        if focus_jobs is not None:
            pairs = {pair for pair in pairs if focus_jobs.intersection(pair)}
        conflict_jobs = {job_id for pair in pairs for job_id in pair}
        if not conflict_jobs:
            return False, False
        if focus_jobs is not None:
            selected_cells = set().union(*(self.footprints.get(job_id, set())
                                           for job_id in conflict_jobs))
            conflict_cells.intersection_update(selected_cells)
        for key in conflict_cells:
            self.history[key] = self.history.get(key, 0) + 1
        counts = {job_id: sum(key in conflict_cells
                              for key in self.footprints.get(job_id, ()))
                  for job_id in conflict_jobs}
        repair = sorted(conflict_jobs,
                        key=lambda job_id: (-counts[job_id],
                                            -self.jobs_by_id[job_id].width, job_id))
        factor = self.config.present_congestion_cost * (iteration + 1)
        refinement_attempts = 0
        for done, job_id in enumerate(repair):
            job = self.jobs_by_id[job_id]
            if progress and progress(done, len(repair),
                                     f'{label} {iteration}: {job.net}') is False:
                return True, True
            old_path = self.routes.get(job_id)
            old_blockers = (len(self._path_blockers(job, old_path))
                            if old_path is not None else 0)
            self._remove(job_id)
            path = self._route_job(job, factor, self.config.hard_avoidance)
            if path is None:
                path = self._route_job(job, factor)
            if (old_path is not None and refinement_attempts < 2 and
                    (path is None or (old_blockers and
                                      len(self._path_blockers(job, path)) >= old_blockers))):
                refinement_attempts += 1
                path = self._refined_ripup_path(job, old_path, path, factor)
            if path is None:
                path = old_path
            if path is not None:
                self._commit(job, path)
        return True, False

    def _refined_ripup_path(self, job: RoutingJob, old_path: list[Cell],
                            current: list[Cell] | None,
                            present_factor: float) -> list[Cell] | None:
        """Use a force-displaced guide only when ordinary rip-up stalls."""
        guide, _, _ = self._relaxed_guide(job, old_path, [])
        if sum(math.dist(a, b) for a, b in
               zip(guide, self._sample_route(job, old_path))) <= 0.05:
            return current
        self.force_guides[job.id] = guide
        try:
            bounds = self._guide_bounds(job, max(self.config.guide_margin,
                                                 4 * self.step))
            candidate = self._astar(job, bounds, present_factor, True)
            if candidate is None:
                candidate = self._astar(job, bounds, present_factor, False)
        finally:
            self.force_guides.pop(job.id, None)
        if candidate is None or self._excessive_detour(job, candidate):
            return current
        if self._path_length(candidate) > self._path_length(old_path) * 1.15 + 4 * self.step:
            return current
        score = (len(self._path_blockers(job, candidate)),
                 self._path_length(candidate))
        if current is None or score < (len(self._path_blockers(job, current)),
                                       self._path_length(current)):
            return candidate
        return current

    def _legalize_routes(self, present_factor: float) -> set[int]:
        """Keep a conflict-free subset, then strictly retry every removed job.

        A crossing is never written to the final board merely to increase the
        route count.  Failed retries become explicit unrouted connections.
        """
        _, pairs = self._conflict_details()
        adjacency: dict[int, set[int]] = {job_id: set() for job_id in self.routes}
        for first, second in pairs:
            adjacency[first].add(second)
            adjacency[second].add(first)
        removed: set[int] = set()
        while pairs:
            job_id = max(
                (candidate for candidate, neighbours in adjacency.items() if neighbours),
                key=lambda candidate: (len(adjacency[candidate]),
                                       math.dist(self.jobs_by_id[candidate].start,
                                                 self.jobs_by_id[candidate].goal), candidate),
            )
            for neighbour in adjacency[job_id]:
                adjacency[neighbour].discard(job_id)
            adjacency[job_id].clear()
            removed.add(job_id)
            pairs = {pair for pair in pairs if job_id not in pair}
            self._remove(job_id)
        # Shorter jobs usually fit in the available local gaps; a long job
        # that cannot be placed is reported for manual/layer-change routing.
        retry_order = sorted(removed,
                             key=lambda job_id: (math.dist(self.jobs_by_id[job_id].start,
                                                           self.jobs_by_id[job_id].goal), job_id))
        failed: set[int] = set()
        for job_id in retry_order:
            job = self.jobs_by_id[job_id]
            path = self._route_job(job, present_factor, True)
            if path is None:
                path = self._astar(job, (0, self.cols - 1, 0, self.rows - 1),
                                   present_factor, True)
            if path is None and self.config.reverse_escape:
                path = self._route_reversed(job, present_factor)
            if path is None:
                failed.add(job_id)
            else:
                self._commit(job, path)
        return failed

    def _shove_unrouted(self, failed: set[int], present_factor: float) -> set[int]:
        """Try local route swaps while preserving zero-crossing output."""
        if not self.config.max_shove_blockers:
            return failed
        for job_id in sorted(failed,
                             key=lambda item: (math.dist(self.jobs_by_id[item].start,
                                                         self.jobs_by_id[item].goal), item)):
            job = self.jobs_by_id[job_id]
            candidate = self._route_job(job, present_factor)
            if candidate is None and self.config.reverse_escape:
                candidate = self._route_reversed(job, present_factor, False)
            if candidate is None:
                continue
            blockers = self._path_blockers(job, candidate)
            if not 0 < len(blockers) <= self.config.max_shove_blockers:
                continue
            old_paths = {other_id: self.routes[other_id] for other_id in blockers}
            for other_id in blockers:
                self._remove(other_id)
            candidate = self._route_job(job, present_factor, True)
            if candidate is None:
                candidate = self._astar(job, (0, self.cols - 1, 0, self.rows - 1),
                                        present_factor, True)
            if candidate is None and self.config.reverse_escape:
                candidate = self._route_reversed(job, present_factor)
            if candidate is not None:
                self._commit(job, candidate)
            rebuilt = {}
            if candidate is not None:
                for other_id in sorted(blockers,
                                       key=lambda item: (math.dist(self.jobs_by_id[item].start,
                                                                   self.jobs_by_id[item].goal), item)):
                    other = self.jobs_by_id[other_id]
                    path = self._route_job(other, present_factor, True)
                    if path is None and self.config.reverse_escape:
                        path = self._route_reversed(other, present_factor)
                    if path is None:
                        break
                    self._commit(other, path)
                    rebuilt[other_id] = path
            if candidate is None or len(rebuilt) != len(blockers):
                if candidate is not None:
                    self._remove(job_id)
                for other_id in rebuilt:
                    self._remove(other_id)
                for other_id, old_path in old_paths.items():
                    self._commit(self.jobs_by_id[other_id], old_path)
            else:
                failed.remove(job_id)
        return failed

    def _shorten_detours(self, present_factor: float, limit: int = 4) -> int:
        """Swap a few nearby routes when a completed route took a large detour.

        A short soft route identifies the actual blockers.  Copper is changed
        only if the target and every displaced blocker can be legally rebuilt
        and the emitted wire length falls.  This is the same local repair idea
        used for missing connections, applied to avoid pathological detours.
        """
        if not self.config.max_shove_blockers or not limit:
            return 0
        candidates = []
        for job_id, path in self.routes.items():
            job = self.jobs_by_id[job_id]
            direct = math.dist(job.start, job.goal)
            length = self._path_length(path)
            if direct > self.step and length > max(direct * 2.5, direct + 100):
                candidates.append((length - direct, job_id))
        candidates.sort(reverse=True)
        shortened = 0
        for _, job_id in candidates[:limit]:
            if job_id not in self.routes:
                continue
            job = self.jobs_by_id[job_id]
            old_path = self.routes[job_id]
            old_length = self._path_length(old_path)
            before_length = self._emitted_length()
            snapshot = {other_id: list(path) for other_id, path in self.routes.items()}
            self._remove(job_id)
            # A low-congestion, history-free candidate exposes short corridors
            # currently occupied by a small number of other nets.
            candidate = None
            for margin in (self.config.guide_margin,
                           max(self.config.guide_margin * 2.5, 100.0),
                           self.config.boundary_margin):
                bounds = self._guide_bounds(job, margin)
                candidate = self._astar(job, bounds,
                                        self.config.present_congestion_cost,
                                        False, 0.0)
                if candidate is not None:
                    break
            if candidate is None or self._path_length(candidate) >= old_length - 4 * self.step:
                self._restore_routes(snapshot)
                continue
            blockers = self._path_blockers(job, candidate)
            if len(blockers) > self.config.max_shove_blockers:
                self._restore_routes(snapshot)
                continue
            for other_id in blockers:
                self._remove(other_id)
            replacement = self._route_job(job, present_factor, True)
            if replacement is None:
                replacement = self._astar(job, (0, self.cols - 1, 0, self.rows - 1),
                                          present_factor, True)
            if replacement is not None:
                self._commit(job, replacement)
            rebuilt = replacement is not None
            if rebuilt:
                for other_id in sorted(blockers):
                    other = self.jobs_by_id[other_id]
                    path = self._route_job(other, present_factor, True)
                    if path is None:
                        path = self._astar(other, (0, self.cols - 1, 0, self.rows - 1),
                                           present_factor, True)
                    if path is None:
                        rebuilt = False
                        break
                    self._commit(other, path)
            if (not rebuilt or self._conflicts()[0] or
                    self._geometric_crossings(self._unique_wire_runs()) or
                    self._emitted_length() >= before_length - self.step):
                self._restore_routes(snapshot)
            else:
                shortened += 1
        return shortened

    def _path_length(self, path: list[Cell]) -> float:
        return sum(math.dist(self._point(a), self._point(b))
                   for a, b in zip(path, path[1:]))

    @staticmethod
    def _path_bends(path: list[Cell]) -> int:
        directions = [(b[0] - a[0], b[1] - a[1])
                      for a, b in zip(path, path[1:])]
        return sum(a != b for a, b in zip(directions, directions[1:]))

    def _straighten_routes(self, limit: int = 8) -> int:
        """Remove legal local zigzags using shortest one-bend octilinear patterns.

        A route is taken out of occupancy while testing shortcuts, so it cannot
        block itself.  Other nets and fixed geometry stay hard obstacles.  The
        whole emitted board is checked before an edit is accepted.
        """
        if not limit:
            return 0
        ranked = sorted(((self._path_bends(path), job_id)
                         for job_id, path in self.routes.items()), reverse=True)
        straightened = 0
        for bends, job_id in ranked[:limit]:
            if bends < 3 or job_id not in self.routes:
                continue
            job = self.jobs_by_id[job_id]
            original = self.routes[job_id]
            before_length = self._emitted_length()
            self._remove(job_id)
            path = original
            for _ in range(4):
                turns = DetailedTrace.from_path(job_id, path).bend_indices
                stride = max(1, math.ceil((len(turns) - 1) / 24))
                samples = list(turns[::stride])
                if samples[-1] != turns[-1]:
                    samples.append(turns[-1])
                old_bends, old_length = self._path_bends(path), self._path_length(path)
                best_path = None
                best_score = old_bends, old_length
                for index, start in enumerate(samples[:-1]):
                    for end in samples[index + 1:index + 5]:
                        if end - start < 2:
                            continue
                        subjob = RoutingJob(job.id, job.net, self._point(path[start]),
                                            self._point(path[end]), job.width, job.layer)
                        for shortcut in self._pattern_paths(subjob):
                            if shortcut == path[start:end + 1]:
                                continue
                            if any(not self._static_legal(cell, job, cell == path[-1]) or
                                   self._dynamic_context(cell, job)[0]
                                   for cell in shortcut[1:]):
                                continue
                            if any(self._diagonal_blockers(a, b, job)
                                   for a, b in zip(shortcut, shortcut[1:])):
                                continue
                            candidate = path[:start] + shortcut + path[end + 1:]
                            if len(set(candidate)) != len(candidate):
                                continue
                            score = self._path_bends(candidate), self._path_length(candidate)
                            if score[0] < old_bends and score[1] <= old_length + 1e-7 \
                                    and score < best_score:
                                best_path, best_score = candidate, score
                if best_path is None:
                    break
                path = best_path
            self._commit(job, path)
            if (path == original or self._conflicts()[0] or
                    self._geometric_crossings(self._unique_wire_runs()) or
                    self._emitted_length() > before_length + 1e-7):
                self._remove(job_id)
                self._commit(job, original)
            else:
                straightened += 1
        return straightened

    def _excessive_detour(self, job: RoutingJob, path: list[Cell]) -> bool:
        if not self.config.max_detour_ratio:
            return False
        direct = math.dist(job.start, job.goal)
        length = self._path_length(path)
        return (direct > self.step and length > self.config.max_detour_ratio * direct
                and length - direct > 200)

    @staticmethod
    def _closest_point_on_segment(point: Point, a: Point, b: Point) -> Point:
        dx, dy = b[0] - a[0], b[1] - a[1]
        length2 = dx * dx + dy * dy
        if length2 <= 1e-12:
            return a
        t = max(0.0, min(1.0, ((point[0] - a[0]) * dx +
                                 (point[1] - a[1]) * dy) / length2))
        return a[0] + t * dx, a[1] + t * dy

    def _sample_route(self, job: RoutingJob, path: list[Cell]) -> tuple[Point, ...]:
        """Use a bounded number of movable control points; pin ends stay fixed."""
        if len(path) < 3:
            return job.start, job.goal
        stride = max(1, math.ceil((len(path) - 1) / 18))
        indices = [0, *range(stride, len(path) - 1, stride), len(path) - 1]
        return (job.start, *(self._point(path[index]) for index in indices[1:-1]),
                job.goal)

    def _relaxed_guide(self, job: RoutingJob, path: list[Cell],
                       missing: list[RoutingJob],
                       controls: tuple[Point, ...] | None = None,
                       attract: bool = False
                       ) -> tuple[tuple[Point, ...], float, set[int]]:
        """One bounded gradient step toward balanced legal net spacing.

        The moved points are only soft A* guides, never raw copper.  Re-routing
        on the legal grid is what turns a guide into a manufacturable candidate.
        Missing connections exert a stronger *virtual* repulsion along their
        straight airwire, reserving room without treating it as actual copper.
        """
        samples = controls or self._sample_route(job, path)
        if len(samples) <= 2:
            return samples, 0.0, set()
        radius = self.clearance + job.width / 2 + self.max_half_width + 3 * self.step
        use_attraction = attract and self.config.attraction_strength > 0
        if use_attraction:
            radius += max(2 * self.step, radius * 0.5)
        offsets = self._offsets(radius)
        moved: list[Point] = [samples[0]]
        energy = 0.0
        affected_missing: set[int] = set()
        for index in range(1, len(samples) - 1):
            point = samples[index]
            cell = self._cell(point)
            nearest: dict[int, tuple[float, Point]] = {}
            for dx, dy in offsets:
                nearby = cell[0] + dx, cell[1] + dy
                if not self._inside(nearby):
                    continue
                other_point = self._point(nearby)
                distance = math.dist(point, other_point)
                for other_id in self.usage.get(self._key(nearby), ()):
                    if other_id == job.id or self.jobs_by_id[other_id].net == job.net:
                        continue
                    if other_id not in nearest or distance < nearest[other_id][0]:
                        nearest[other_id] = distance, other_point
            fx = fy = 0.0
            tangent = (samples[index + 1][0] - samples[index - 1][0],
                       samples[index + 1][1] - samples[index - 1][1])

            def repel(other_point: Point, distance: float, target: float,
                      weight: float, other_id: int) -> tuple[float, float, float]:
                if distance >= target:
                    return 0.0, 0.0, 0.0
                gap = target - distance
                dx, dy = point[0] - other_point[0], point[1] - other_point[1]
                if distance < 1e-8:
                    dx, dy = -tangent[1], tangent[0]
                    if job.id > other_id:
                        dx, dy = -dx, -dy
                    distance = math.hypot(dx, dy) or 1.0
                magnitude = weight * gap / max(target, 1e-8)
                return magnitude * dx / distance, magnitude * dy / distance, \
                    weight * gap ** 2 / (2 * max(target, 1e-8))

            for other_id, (distance, other_point) in nearest.items():
                other = self.jobs_by_id[other_id]
                target = (job.width + other.width) / 2 + self.clearance + 3 * self.step
                rx, ry, cost = repel(other_point, distance, target, 1.0, other_id)
                fx += rx
                fy += ry
                energy += cost
            near_missing = False
            for ghost in missing:
                if ghost.net == job.net:
                    continue
                closest = self._closest_point_on_segment(point, ghost.start, ghost.goal)
                distance = math.dist(point, closest)
                target = (job.width + ghost.width) / 2 + self.clearance + 3 * self.step
                rx, ry, cost = repel(closest, distance, target, 2.0, ghost.id)
                if cost:
                    near_missing = True
                    affected_missing.add(ghost.id)
                    fx += rx
                    fy += ry
                    energy += cost
            # One attraction per foreign *net* avoids overweighting a net
            # represented by several two-terminal routing jobs. The smooth
            # finite-range well is lowest at the preferred spacing, zero at
            # its outer radius, and exerts no attraction inside the target.
            # Do not counteract a missing airwire's attempt to clear space.
            if use_attraction and not near_missing:
                nearest_nets: dict[str, tuple[float, Point, RoutingJob]] = {}
                for other_id, (distance, other_point) in nearest.items():
                    other = self.jobs_by_id[other_id]
                    if (other.net not in nearest_nets or
                            distance < nearest_nets[other.net][0]):
                        nearest_nets[other.net] = distance, other_point, other
                for distance, other_point, other in nearest_nets.values():
                    target = (job.width + other.width) / 2 + self.clearance + 3 * self.step
                    band = max(2 * self.step, target * 0.5)
                    if distance >= target + band:
                        continue
                    u = max(0.0, (distance - target) / band)
                    strength = self.config.attraction_strength
                    energy -= strength * band / 3 * (1 - 3 * u * u + 2 * u * u * u)
                    if distance > target:
                        magnitude = 2 * strength * u * (1 - u)
                        fx += magnitude * (other_point[0] - point[0]) / distance
                        fy += magnitude * (other_point[1] - point[1]) / distance
            # A weak discrete spring discourages sharp kinks and overlong wire.
            mid_x = (samples[index - 1][0] + samples[index + 1][0]) / 2
            mid_y = (samples[index - 1][1] + samples[index + 1][1]) / 2
            fx += 0.15 * (mid_x - point[0]) / self.step
            fy += 0.15 * (mid_y - point[1]) / self.step
            energy += 0.075 * ((mid_x - point[0]) ** 2 +
                               (mid_y - point[1]) ** 2) / self.step
            shift_x, shift_y = fx * self.step, fy * self.step
            shift = math.hypot(shift_x, shift_y)
            if shift > 2 * self.step:
                shift_x *= 2 * self.step / shift
                shift_y *= 2 * self.step / shift
            moved.append((max(self.origin_x, min(self.origin_x + (self.cols - 1) * self.step,
                                                point[0] + shift_x)),
                          max(self.origin_y, min(self.origin_y + (self.rows - 1) * self.step,
                                                point[1] + shift_y))))
        moved.append(samples[-1])
        return tuple(moved), energy, affected_missing

    def _restore_routes(self, snapshot: dict[int, list[Cell]]) -> None:
        self.usage.clear()
        self.footprints.clear()
        self.diagonal_usage.clear()
        self.diagonal_footprints.clear()
        self.routes.clear()
        for job_id, path in snapshot.items():
            self._commit(self.jobs_by_id[job_id], path)

    def _force_state_energy(self, failed: set[int]) -> float:
        missing = [self.jobs_by_id[job_id] for job_id in sorted(failed)]
        return sum(self._relaxed_guide(self.jobs_by_id[job_id], path, missing,
                                       attract=True)[1]
                   for job_id, path in self.routes.items())

    def _refine_initial_routes(self, failed: set[int],
                               progress: Progress | None = None) -> tuple[set[int], bool]:
        """Move a few congested routes before the remaining jobs are placed.

        Unlike final refinement, this can run on a provisional board with
        conflicts.  A move must not increase conflict cells or make an extreme
        detour; the following rip-up pass can then use the freed corridor.
        """
        if not self.config.force_max_jobs or len(self.routes) < 2:
            return set(), False
        missing = [self.jobs_by_id[job_id] for job_id in sorted(failed)]
        _, conflict_jobs = self._conflicts()
        candidates = []
        for job_id, path in self.routes.items():
            job = self.jobs_by_id[job_id]
            guide, energy, affected = self._relaxed_guide(job, path, missing)
            displacement = sum(math.dist(a, b) for a, b in
                               zip(guide, self._sample_route(job, path)))
            if displacement > 0.05:
                candidates.append((bool(affected), job_id in conflict_jobs,
                                   max(energy, 0.0), displacement, energy, job_id, guide))
        candidates.sort(key=lambda item: (-item[0], -item[1], -item[2], -item[3], item[5]))
        moves: set[int] = set()
        for done, (_, _, _, _, old_energy, job_id, guide) in enumerate(
                candidates[:min(6, self.config.force_max_jobs)]):
            job = self.jobs_by_id[job_id]
            if progress and progress(done, min(6, self.config.force_max_jobs),
                                     f'Early refinement: {job.net}') is False:
                return moves, True
            old_path = self.routes[job_id]
            old_conflicts = len(self._conflicts()[0])
            old_length = self._path_length(old_path)
            self._remove(job_id)
            self.force_guides[job_id] = guide
            try:
                bounds = self._guide_bounds(job, max(self.config.guide_margin,
                                                     4 * self.step))
                path = self._astar(job, bounds, self.config.present_congestion_cost,
                                   True)
                if path is None:
                    path = self._astar(job, bounds,
                                       self.config.present_congestion_cost, False)
            finally:
                self.force_guides.pop(job_id, None)
            self._commit(job, path if path is not None else old_path)
            if path is None or path == old_path or self._excessive_detour(job, path):
                self._remove(job_id)
                self._commit(job, old_path)
                continue
            new_conflicts = len(self._conflicts()[0])
            new_length = self._path_length(path)
            new_energy = self._relaxed_guide(job, path, missing)[1]
            useful = ((new_conflicts < old_conflicts and
                       new_length <= old_length * 1.15 + 4 * self.step) or
                      (new_conflicts == old_conflicts and
                       new_length <= old_length + self.step and
                       (new_energy < old_energy - 1e-6 or
                        new_length < old_length - self.step)))
            if useful:
                moves.add(job_id)
            else:
                self._remove(job_id)
                self._commit(job, old_path)
        return moves, False

    def _emitted_length(self) -> float:
        return sum(math.dist(a, b) for wire in self._unique_wire_runs()
                   for a, b in zip(wire['points'], wire['points'][1:]))

    def _force_refine(self, failed: set[int], progress: Progress | None = None
                      ) -> tuple[set[int], int, int, bool]:
        """Each iteration straightens, repairs, then balances legal routes."""
        if not self.config.force_iterations:
            return failed, 0, 0, False
        original_routes = {job_id: list(path) for job_id, path in self.routes.items()}
        original_failed = set(failed)
        original_length = self._emitted_length()
        best_routes = original_routes
        best_failed = original_failed
        best_length = original_length
        best_bends = sum(self._path_bends(path) for path in original_routes.values())
        best_moved = 0
        iterations_run = moved_total = 0
        control_guides: dict[int, tuple[Point, ...]] = {}
        control_ghosts: dict[int, set[int]] = {}
        present_factor = self.config.present_congestion_cost * self.config.iterations

        def remember_best() -> None:
            nonlocal best_routes, best_failed, best_length, best_bends, best_moved
            length = self._emitted_length()
            bends = sum(self._path_bends(path) for path in self.routes.values())
            if (len(failed) < len(best_failed) or
                    (len(failed) == len(best_failed) and
                     (length < best_length - 1e-7 or
                      (abs(length - best_length) <= 1e-7 and bends < best_bends)))):
                best_routes = {job_id: list(path) for job_id, path in self.routes.items()}
                best_failed = set(failed)
                best_length = length
                best_bends = bends
                best_moved = moved_total

        for iteration in range(1, self.config.force_iterations + 1):
            iterations_run += 1
            if progress and progress(iteration, self.config.force_iterations,
                                     f'Iteration {iteration}: straighten, repair, balance') is False:
                self._restore_routes(original_routes)
                return original_failed, iterations_run, 0, True
            before_repair = {job_id: list(path) for job_id, path in self.routes.items()}
            failed_before_repair = set(failed)
            self._straighten_routes()
            failed = self._shove_unrouted(failed, present_factor)
            self._shorten_detours(present_factor)
            if self._conflicts()[0] or self._geometric_crossings(self._unique_wire_runs()):
                self._restore_routes(before_repair)
                failed = failed_before_repair
            else:
                remember_best()
            if not self.config.force_max_jobs:
                continue
            snapshot = {job_id: list(path) for job_id, path in self.routes.items()}
            old_failed = set(failed)
            old_energy = self._force_state_energy(failed)
            missing = [self.jobs_by_id[job_id] for job_id in sorted(failed)]
            candidates = []
            for job_id, path in snapshot.items():
                job = self.jobs_by_id[job_id]
                guide, energy, affected = self._relaxed_guide(
                    job, path, missing, control_guides.get(job_id), attract=True)
                affected.update(control_ghosts.get(job_id, ()))
                displacement = sum(math.dist(a, b) for a, b in
                                   zip(guide, self._sample_route(job, path)))
                if displacement > 0.05:
                    candidates.append((bool(affected), max(energy, 0.0),
                                       displacement, energy, job_id, guide, affected))
            candidates.sort(key=lambda item: (-item[0], -item[1], -item[2], item[4]))
            if self.config.attraction_strength:
                # Missing-airwire routes remain the priority, but reserve a
                # small share for attraction-dominated spacing candidates.
                # Otherwise dense boards can starve that new force entirely.
                spacing = [item for item in candidates
                           if not item[0] and item[3] < 0]
                reserved = min(self.config.force_max_jobs // 3, len(spacing))
                selected = candidates[:self.config.force_max_jobs - reserved]
                selected_ids = {item[4] for item in selected}
                for item in spacing:
                    if len(selected) >= self.config.force_max_jobs:
                        break
                    if item[4] not in selected_ids:
                        selected.append(item)
                        selected_ids.add(item[4])
                for item in candidates:
                    if len(selected) >= self.config.force_max_jobs:
                        break
                    if item[4] not in selected_ids:
                        selected.append(item)
                        selected_ids.add(item[4])
                candidates = selected
            else:
                candidates = candidates[:self.config.force_max_jobs]
            if not candidates:
                continue
            affected_missing: set[int] = set()
            moved_round = 0
            cancelled = False
            for _, _, _, _, job_id, guide, affected in candidates:
                if progress and progress(iteration, self.config.force_iterations,
                                         f'Force refinement {iteration}: {self.jobs_by_id[job_id].net}') is False:
                    cancelled = True
                    break
                affected_missing.update(affected)
                job = self.jobs_by_id[job_id]
                old_path = self.routes[job_id]
                control_guides[job_id] = guide
                control_ghosts[job_id] = affected
                self._remove(job_id)
                self.force_guides[job_id] = guide
                try:
                    bounds = self._guide_bounds(job, max(self.config.guide_margin, 4 * self.step))
                    path = self._astar(job, bounds, present_factor, True)
                    if path is None:
                        path = self._astar(job, (0, self.cols - 1, 0, self.rows - 1),
                                           present_factor, True)
                finally:
                    self.force_guides.pop(job_id, None)
                self._commit(job, path if path is not None else old_path)
                moved_round += path is not None and path != old_path
                if path is not None and path != old_path:
                    control_guides.pop(job_id, None)
                    control_ghosts.pop(job_id, None)
            if cancelled:
                self._restore_routes(original_routes)
                return original_failed, iterations_run, 0, True
            # Revisit only missing connections whose airwires exerted force.
            for job_id in sorted(affected_missing,
                                 key=lambda item: (math.dist(self.jobs_by_id[item].start,
                                                             self.jobs_by_id[item].goal), item))[:8]:
                if job_id not in failed:
                    continue
                job = self.jobs_by_id[job_id]
                path = self._route_job(job, present_factor, True)
                if path is not None:
                    self._commit(job, path)
                    failed.remove(job_id)
            conflict_cells, _ = self._conflicts()
            wires = self._unique_wire_runs()
            crossings = self._geometric_crossings(wires)
            if conflict_cells or crossings:
                self._restore_routes(snapshot)
                failed = old_failed
                break
            new_energy = self._force_state_energy(failed)
            if (len(failed) > len(old_failed) or
                    (len(failed) == len(old_failed) and moved_round and
                     (new_energy >= old_energy - 1e-6 or
                      self._emitted_length() > original_length * 1.03))):
                self._restore_routes(snapshot)
                failed = old_failed
            else:
                moved_total += moved_round
                remember_best()
        # Later rounds can be useful intermediate steps, but the emitted board
        # is the shortest legal state with the highest completion seen so far.
        self._restore_routes(best_routes)
        return best_failed, iterations_run, best_moved, False

    def _detailed_astar(self, job: RoutingJob) -> list[Cell] | None:
        """Route against actual copper, without negotiated/history penalties."""
        local = self._guide_bounds(job, max(self.config.guide_margin, 4 * self.step))
        whole = (0, self.cols - 1, 0, self.rows - 1)
        for bounds in (local, whole):
            path = self._astar(job, bounds, 0.0, True, 0.0)
            if path is not None and not self._excessive_detour(job, path):
                return path
            if bounds == whole:
                break
        return None

    @staticmethod
    def _detailed_slide_segment(trace: DetailedTrace, path: list[Cell],
                                segment: int, offset: int) -> list[Cell] | None:
        """Slide one interior segment parallel; keep its two neighbors on their lines.

        The two new bends are exact integer intersections of the translated
        segment with the *original* neighboring support lines.  Neither the
        preceding nor following segment can rotate or acquire an extra bend.
        """
        bends = trace.bends
        if not offset or not 1 <= segment < len(bends) - 2:
            return None
        p, a, b, q = bends[segment - 1:segment + 3]
        dx, dy = b[0] - a[0], b[1] - a[1]
        scale = math.gcd(abs(dx), abs(dy))
        if not scale:
            return None
        direction = dx // scale, dy // scale
        shifted = (a[0] - direction[1] * offset,
                   a[1] + direction[0] * offset)

        def intersection(start: Cell, end: Cell) -> Cell | None:
            ux, uy = end[0] - start[0], end[1] - start[1]
            determinant = ux * direction[1] - uy * direction[0]
            if not determinant:
                return None
            vx, vy = shifted[0] - start[0], shifted[1] - start[1]
            numerator = vx * direction[1] - vy * direction[0]
            x_numerator = start[0] * determinant + ux * numerator
            y_numerator = start[1] * determinant + uy * numerator
            if x_numerator % determinant or y_numerator % determinant:
                return None
            return x_numerator // determinant, y_numerator // determinant

        new_a = intersection(p, a)
        new_b = intersection(b, q)
        if new_a is None or new_b is None or new_a in (p, b) or new_b in (a, q):
            return None
        # Adjacent segments retain their direction and cannot reverse; the
        # translated segment also retains the original positive direction.
        if (not 0 < (new_a[0] - p[0]) * (a[0] - p[0]) +
                (new_a[1] - p[1]) * (a[1] - p[1]) or
                not 0 < (q[0] - new_b[0]) * (q[0] - b[0]) +
                (q[1] - new_b[1]) * (q[1] - b[1]) or
                not 0 < (new_b[0] - new_a[0]) * dx +
                (new_b[1] - new_a[1]) * dy):
            return None
        if (new_b[0] - new_a[0]) * dy != (new_b[1] - new_a[1]) * dx:
            return None
        first, last = trace.bend_indices[segment - 1], trace.bend_indices[segment + 2]
        replacement = (Autorouter._grid_line(p, new_a) +
                       Autorouter._grid_line(new_a, new_b)[1:] +
                       Autorouter._grid_line(new_b, q)[1:])
        result = path[:first] + replacement + path[last + 1:]
        return result if len(result) == len(set(result)) else None

    def _detailed_nearby_segments(self, prospective: list[Cell],
                                  target: RoutingJob) -> list[tuple[int, int]]:
        """Rank both direct blockers and non-conflicting neighbors of the airwire."""
        samples = [self._point(cell) for cell in
                   prospective[::max(1, len(prospective) // 32)]]
        if prospective and self._point(prospective[-1]) not in samples:
            samples.append(self._point(prospective[-1]))
        blockers = self._path_blockers(target, prospective)
        nearby = []
        for other_id, path in self.routes.items():
            other = self.jobs_by_id[other_id]
            if other.net == target.net:
                continue
            trace = DetailedTrace.from_path(other_id, path)
            for segment in range(1, len(trace.bends) - 2):
                a, b = (self._point(cell) for cell in trace.segments[segment])
                distance = min(self._distance_to_segment(x, y, a, b)
                               for x, y in samples)
                reach = ((target.width + other.width) / 2 + self.clearance +
                         8 * self.step)
                if other_id in blockers or distance <= reach:
                    nearby.append((0 if other_id in blockers else 1,
                                   distance, other_id, segment))
        nearby.sort()
        # Several traces and two bend segments per trace may participate.
        result, counts = [], {}
        for _, _, other_id, segment in nearby:
            if counts.get(other_id, 0) >= 2:
                continue
            counts[other_id] = counts.get(other_id, 0) + 1
            result.append((other_id, segment))
            if len(result) >= 16:
                break
        return result

    def _detailed_slide_legal(self, other: RoutingJob, path: list[Cell]) -> bool:
        if self._excessive_detour(other, path):
            return False
        for index, cell in enumerate(path):
            if not self._static_legal(cell, other, index in (0, len(path) - 1)):
                return False
            if self._dynamic_context(cell, other)[0]:
                return False
            if index and self._diagonal_blockers(path[index - 1], cell, other):
                return False
        return True

    def _detailed_drag_blockers(self, job: RoutingJob) -> bool:
        """Bounded best-first search over legal one-segment slides near a miss."""
        if not self.config.max_shove_blockers or not self.config.detailed_drag_states:
            return False
        whole = (0, self.cols - 1, 0, self.rows - 1)
        prospective = self._astar(job, whole, self.config.present_congestion_cost,
                                  False, 0.0)
        if prospective is None:
            return False
        candidates = self._detailed_nearby_segments(prospective, job)
        if not candidates:
            return False
        snapshot = {job_id: list(path) for job_id, path in self.routes.items()}
        queue = [(len(self._path_blockers(job, prospective)), 0, 0, snapshot, frozenset())]
        seen = {()}
        serial = 0
        explored = 0
        try:
            while queue and explored < self.config.detailed_drag_states:
                _, depth, _, state, moved = heapq.heappop(queue)
                self._restore_routes(state)
                if depth:
                    explored += 1
                    path = self._detailed_astar(job)
                    if path is not None:
                        self._commit(job, path)
                        if (not self._conflicts()[0] and
                                not self._geometric_crossings(self._unique_wire_runs())):
                            return True
                        self._remove(job.id)
                if depth >= min(3, self.config.max_shove_blockers):
                    continue
                generated = 0
                for offset in (-1, 1, -2, 2, -3, 3, -4, 4):
                    for other_id, segment in candidates:
                        if other_id in moved:
                            continue
                        other = self.jobs_by_id[other_id]
                        old_path = self.routes[other_id]
                        trace = DetailedTrace.from_path(other_id, old_path)
                        if segment >= len(trace.bends) - 2:
                            continue
                        candidate = self._detailed_slide_segment(trace, old_path,
                                                                 segment, offset)
                        if candidate is None:
                            continue
                        self._remove(other_id)
                        legal = self._detailed_slide_legal(other, candidate)
                        if legal:
                            self._commit(other, candidate)
                            next_state = dict(state)
                            next_state[other_id] = candidate
                            signature = tuple((item, tuple(next_state[item]))
                                              for item in sorted(moved | {other_id}))
                            if signature not in seen:
                                seen.add(signature)
                                blockers = len(self._path_blockers(job, prospective))
                                serial += 1
                                heapq.heappush(queue, (blockers, depth + 1, serial,
                                                       next_state, moved | {other_id}))
                                generated += 1
                            self._remove(other_id)
                        self._commit(other, old_path)
                        if generated >= 48:
                            break
                    if generated >= 48:
                        break
                # Limit frontier memory as well as the number of expensive A* calls.
                if len(queue) > self.config.detailed_drag_states * 4:
                    queue = heapq.nsmallest(self.config.detailed_drag_states * 4, queue)
                    heapq.heapify(queue)
        finally:
            if job.id not in self.routes:
                self._restore_routes(snapshot)
        return False

    def _detailed_route(self, failed: set[int], progress: Progress | None = None
                        ) -> tuple[set[int], int, int, int, bool]:
        """Alternate bend dragging and missing-line A* until a fixed point.

        A finite pass cap is a safety limit for densely obstructed boards; most
        runs stop earlier because the entire pass makes no accepted change.
        """
        if not self.config.detailed_max_passes:
            return failed, 0, 0, 0, False
        initial = {job_id: list(path) for job_id, path in self.routes.items()}
        initial_failed = set(failed)
        straightened_total = dragged_total = 0
        passes = 0
        for iteration in range(1, self.config.detailed_max_passes + 1):
            if progress and progress(iteration, self.config.detailed_max_passes,
                                     f'Detailed routing {iteration}: straighten and connect') is False:
                self._restore_routes(initial)
                return initial_failed, passes, 0, 0, True
            passes += 1
            snapshot = {job_id: list(path) for job_id, path in self.routes.items()}
            straightened = self._straighten_routes(limit=len(self.routes))
            connected = 0
            dragged = 0
            order = sorted(failed, key=lambda job_id: (
                math.dist(self.jobs_by_id[job_id].start,
                          self.jobs_by_id[job_id].goal), job_id))
            for done, job_id in enumerate(order):
                job = self.jobs_by_id[job_id]
                if progress and progress(done, len(order),
                                         f'Detailed A*: {job.net}') is False:
                    self._restore_routes(initial)
                    return initial_failed, passes, 0, 0, True
                path = self._detailed_astar(job)
                if path is not None:
                    self._commit(job, path)
                    if (self._conflicts()[0] or
                            self._geometric_crossings(self._unique_wire_runs())):
                        self._remove(job_id)
                    else:
                        failed.remove(job_id)
                        connected += 1
                elif self._detailed_drag_blockers(job):
                    failed.remove(job_id)
                    connected += 1
                    dragged += 1
            if self._conflicts()[0] or self._geometric_crossings(self._unique_wire_runs()):
                self._restore_routes(snapshot)
                failed = {job.id for job in self.jobs if job.id not in self.routes}
                break
            straightened_total += straightened
            dragged_total += dragged
            if not straightened and not connected:
                break
        return failed, passes, straightened_total, dragged_total, False

    def _polyline(self, job: RoutingJob, cells: list[Cell]) -> list[list[float]]:
        points = self._raw_points(job, cells)
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

    def _raw_points(self, job: RoutingJob, cells: list[Cell]) -> list[Point]:
        """Keep every grid edge until duplicate same-net copper is removed."""
        return [job.start, *(self._point(cell) for cell in cells[1:-1]), job.goal]

    def _unique_wire_runs(self) -> list[dict]:
        """Emit each shared same-net edge once, without disconnecting a net.

        Two requested connections can legitimately use the same grid corridor.
        Their independent A* routes must not become two physical copper traces
        over one another.  The first route owns each edge; later routes are
        split around already emitted edges, which remain conductive bridges.
        """
        seen: dict[str, dict[tuple[Point, Point], dict]] = {}
        wires = []
        # A shared edge must be owned by the widest requested trace; otherwise
        # deduplication could silently narrow a later, wider connection.
        for job in sorted(self.jobs, key=lambda value: (-value.width, value.id)):
            cells = self.routes.get(job.id)
            if cells is None:
                continue
            raw = self._raw_points(job, cells)
            owned = seen.setdefault(job.net, {})
            run: list[Point] = []
            run_edges: list[tuple[Point, Point]] = []

            def flush() -> None:
                if len(run) < 2:
                    return
                # Reuse the collinear simplifier on arbitrary run endpoints.
                fragment = RoutingJob(job.id, job.net, run[0], run[-1],
                                      job.width, job.layer)
                points = self._polyline(fragment, [self._cell(p) for p in run])
                # _polyline snaps interior points to the grid; raw interior
                # points are already on-grid by construction.
                wire = {"layer": job.layer, "width": job.width,
                        "net": job.net, "route_type": "autoroute",
                        "points": points, "autoroute_job": job.id,
                        "autoroute_jobs": [job.id]}
                wires.append(wire)
                for edge in run_edges:
                    owned[edge] = wire

            for a, b in zip(raw, raw[1:]):
                first = (round(a[0], 6), round(a[1], 6))
                second = (round(b[0], 6), round(b[1], 6))
                if first == second:
                    continue
                edge = tuple(sorted((first, second)))
                if edge in owned:
                    flush()
                    run = []
                    run_edges = []
                    owner = owned[edge]
                    if job.id not in owner['autoroute_jobs']:
                        owner['autoroute_jobs'].append(job.id)
                    continue
                if not run:
                    run = [first]
                run.append(second)
                run_edges.append(edge)
            flush()
        return wires

    @staticmethod
    def _geometric_crossings(wires: list[dict]) -> set[tuple[int, int]]:
        """Find proper centerline crossings, including off-grid end segments."""
        segments = []
        for wire in wires:
            for a, b in zip(wire['points'], wire['points'][1:]):
                segments.append((a, b, wire['net'], wire['autoroute_job']))

        def side(a: list[float], b: list[float], c: list[float]) -> float:
            return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

        crossings: set[tuple[int, int]] = set()
        for index, (a, b, net, job_id) in enumerate(segments):
            for c, d, other_net, other_id in segments[index + 1:]:
                if job_id == other_id or net == other_net:
                    continue
                if (max(a[0], b[0]) <= min(c[0], d[0]) or
                        max(c[0], d[0]) <= min(a[0], b[0]) or
                        max(a[1], b[1]) <= min(c[1], d[1]) or
                        max(c[1], d[1]) <= min(a[1], b[1])):
                    continue
                if (side(a, b, c) * side(a, b, d) < -1e-9 and
                        side(c, d, a) * side(c, d, b) < -1e-9):
                    crossings.add((min(job_id, other_id), max(job_id, other_id)))
        return crossings

    def _initial_order(self) -> list[RoutingJob]:
        """Preserve heuristic priority while trying reproducible nearby permutations."""
        if self.config.ordering == "dense_first":
            terminals = [(other.net, point) for other in self.jobs
                         for point in (other.start, other.goal)]
            def density(job: RoutingJob) -> int:
                return sum(other_net != job.net and math.dist(point, other_point) < 50
                           for point in (job.start, job.goal)
                           for other_net, other_point in terminals)
            ordered = sorted(self.jobs, key=lambda job: (-density(job),
                             math.dist(job.start, job.goal), -job.width, job.id))
        elif self.config.short_routes_first:
            ordered = sorted(self.jobs, key=lambda job: (math.dist(job.start, job.goal), -job.width, job.id))
        else:
            ordered = sorted(self.jobs, key=lambda job: (-job.width, -math.dist(job.start, job.goal), job.id))
        if self.order_seed is not None and len(ordered) > 1:
            rng = random.Random(self.order_seed)
            window = max(2, round(len(ordered) * 0.05))
            ordered = [job for _, job in sorted(
                enumerate(ordered), key=lambda entry: entry[0] + rng.uniform(-window, window)
            )]
        return ordered

    def route(self, progress: Progress | None = None,
              preview: RoutePreview | None = None) -> tuple[list[dict], RouteReport]:
        started = time.monotonic()
        report = RouteReport(self.layer, len(self.jobs),
                             config={"backend": "builtin", **asdict(self.config)})
        report.config["compute_device"] = self.torch_costs.device if self.torch_costs else "python"
        ordered = self._initial_order()

        def update(done: int, message: str) -> bool:
            if preview is not None:
                preview(self, done, len(self.jobs), message)
            if progress is None:
                return True
            return progress(done, len(self.jobs), message) is not False

        for done, job in enumerate(ordered):
            if not update(done, f"Initial routing: {job.net}"):
                report.cancelled = True
                break
            path = (self._route_job(job, self.config.present_congestion_cost, True)
                    if self.config.hard_avoidance else None)
            if path is None:
                path = self._route_job(job, self.config.present_congestion_cost)
            if path is None:
                report.failed.append({"job": job.id, "net": job.net, "reason": "no path"})
            else:
                self._commit(job, path)
            placed = done + 1
            if (self.config.early_refine_interval and
                    placed % self.config.early_refine_interval == 0 and
                    placed < len(ordered)):
                checkpoint = {job_id: list(route) for job_id, route in self.routes.items()}
                checkpoint_history = dict(self.history)
                before_conflicts = len(self._conflicts()[0])
                missing_so_far = {earlier.id for earlier in ordered[:placed]
                                  if earlier.id not in self.routes}
                moved, cancelled = self._refine_initial_routes(
                    missing_so_far,
                    lambda _local, _total, message: update(done, message),
                )
                if not cancelled and moved:
                    _, cancelled = self._ripup_and_reroute(
                        0, lambda _local, _total, message: update(done, message),
                        label='Early rip-up/reroute', focus_jobs=moved)
                report.early_refinement_passes += 1
                if cancelled or len(self._conflicts()[0]) > before_conflicts:
                    self._restore_routes(checkpoint)
                    self.history = checkpoint_history
                    if cancelled:
                        report.cancelled = True
                        break
                else:
                    report.early_refinement_moves += len(moved)

        initial_conflicts, _ = self._conflicts()
        best_conflict_count = len(initial_conflicts)
        best_routes = {job_id: list(path) for job_id, path in self.routes.items()}
        for iteration in range(1, self.config.iterations):
            if report.cancelled:
                break
            worked, cancelled = self._ripup_and_reroute(
                iteration,
                lambda done, _total, message: update(done, message),
            )
            if not worked:
                break
            report.repair_iterations = iteration
            report.cancelled = report.cancelled or cancelled

            current_conflicts, _ = self._conflicts()
            if len(current_conflicts) < best_conflict_count:
                best_conflict_count = len(current_conflicts)
                best_routes = {job_id: list(path) for job_id, path in self.routes.items()}

        current_conflicts, _ = self._conflicts()
        if len(current_conflicts) > best_conflict_count:
            self.usage.clear()
            self.footprints.clear()
            self.diagonal_usage.clear()
            self.diagonal_footprints.clear()
            self.routes.clear()
            for job_id, path in best_routes.items():
                self._commit(self.jobs_by_id[job_id], path)

        if self.config.strict_final and not report.cancelled:
            failed_final = self._legalize_routes(
                self.config.present_congestion_cost * self.config.iterations
            )
            # Initial maze failures were not part of the conflict graph; they
            # deserve the same post-legalization shove opportunity.
            failed_final.update(job.id for job in self.jobs if job.id not in self.routes)
            failed_final = self._shove_unrouted(
                failed_final, self.config.present_congestion_cost * self.config.iterations
            )
            failed_before_force = len(failed_final)
            (failed_final, report.force_iterations_run, report.force_jobs_moved,
             force_cancelled) = self._force_refine(
                 failed_final,
                 lambda _done, _total, message: update(len(self.jobs) - 1, message),
            )
            report.cancelled = report.cancelled or force_cancelled
            overlong = set()
            for job_id, path in list(self.routes.items()):
                if self._excessive_detour(self.jobs_by_id[job_id], path):
                    self._remove(job_id)
                    overlong.add(job_id)
            failed_final.update(overlong)
            report.force_new_connections = failed_before_force - len(failed_final)
            failed_before_detail = len(failed_final)
            if not report.cancelled:
                (failed_final, report.detailed_passes, report.detailed_straightened,
                 report.detailed_dragged, detailed_cancelled) = self._detailed_route(
                     failed_final,
                     lambda _done, _total, message: update(len(self.jobs) - 1, message),
                )
                report.cancelled = report.cancelled or detailed_cancelled
            for job_id, path in list(self.routes.items()):
                if self._excessive_detour(self.jobs_by_id[job_id], path):
                    self._remove(job_id)
                    overlong.add(job_id)
                    failed_final.add(job_id)
            report.detailed_new_connections = failed_before_detail - len(failed_final)
            report.failed = [{"job": job.id, "net": job.net,
                              "reason": ("route exceeds detour limit; consider another layer"
                                         if job.id in overlong and job.id in failed_final else
                                         "conflict-free route not found")}
                             for job in self.jobs if job.id in failed_final]

        conflict_cells, conflict_jobs = self._conflicts()
        report.conflict_cells = len(conflict_cells)
        report.conflicting_jobs = sorted(conflict_jobs)
        def output_wires() -> list[dict]:
            return self._unique_wire_runs()

        wires = output_wires()
        if self.config.strict_final and not report.cancelled:
            crossings = self._geometric_crossings(wires)
            while crossings:
                severity = {job_id: sum(job_id in pair for pair in crossings)
                            for pair in crossings for job_id in pair}
                rejected_id = max(severity, key=lambda job_id: (severity[job_id],
                                   math.dist(self.jobs_by_id[job_id].start,
                                             self.jobs_by_id[job_id].goal), job_id))
                self._remove(rejected_id)
                job = self.jobs_by_id[rejected_id]
                report.failed.append({"job": job.id, "net": job.net,
                                      "reason": "geometric crossing in final route"})
                wires = output_wires()
                crossings = self._geometric_crossings(wires)
            conflict_cells, conflict_jobs = self._conflicts()
            report.conflict_cells = len(conflict_cells)
            report.conflicting_jobs = sorted(conflict_jobs)
        report.wire_length = sum(
            math.dist(a, b) for wire in wires
            for a, b in zip(wire['points'], wire['points'][1:])
        )
        if self.torch_costs is not None:
            report.accelerated_searches = self.torch_costs.fields_built
            report.acceleration_fallbacks = self.torch_costs.fields_skipped
        report.routed = len(self.routes)
        reasons = {entry['job']: entry['reason'] for entry in report.failed}
        report.failed = [{"job": job.id, "net": job.net,
                          "reason": reasons.get(job.id, "not routed")}
                         for job in self.jobs if job.id not in self.routes]
        report.wire_length = round(report.wire_length, 3)
        report.elapsed_seconds = round(time.monotonic() - started, 3)
        update(len(self.jobs), "Routing complete")
        return wires, report


def autoroute_data(data: dict, config: RouterConfig | None = None,
                   progress: Progress | None = None,
                   preview: RoutePreview | None = None) -> tuple[list[dict], RouteReport]:
    """Try a baseline and seeded order permutations; retain the best legal result."""
    config = config or RouterConfig()
    config.validate()
    started = time.monotonic()
    best: tuple[list[dict], RouteReport] | None = None
    best_score: tuple[int, int, float] | None = None
    results = []
    cancelled = False
    for round_index in range(config.random_rounds):
        seed = None if round_index == 0 else config.random_seed + round_index - 1
        router = Autorouter(data, config, order_seed=seed)
        highest_progress = 0
        def round_progress(done: int, total: int, message: str) -> bool | None:
            nonlocal highest_progress
            if progress is None:
                return True
            highest_progress = max(highest_progress, done)
            return progress(round_index * total + highest_progress,
                            config.random_rounds * total,
                            f'Round {round_index + 1}/{config.random_rounds}: {message}')
        if preview is None:
            wires, report = router.route(round_progress)
        else:
            def round_preview(active: Autorouter, _done: int, _total: int,
                              message: str) -> None:
                preview(active, round_index + 1, config.random_rounds, message)
            wires, report = router.route(round_progress, round_preview)
        results.append({'round': round_index + 1, 'seed': seed, 'routed': report.routed,
                        'jobs_total': report.jobs_total, 'conflict_cells': report.conflict_cells,
                        'wire_length': report.wire_length,
                        'elapsed_seconds': report.elapsed_seconds,
                        'cancelled': report.cancelled})
        if report.cancelled:
            cancelled = True
            if best is None:
                best = wires, report
            break
        # A clean route outranks an illegal one; then maximize completion and
        # break ties by shorter emitted copper. The first baseline wins ties.
        score = (-report.conflict_cells, report.routed, -report.wire_length)
        if best_score is None or score > best_score:
            best, best_score = (wires, report), score
            selected_round = round_index + 1
        if report.clean:
            break
    assert best is not None
    wires, report = best
    report.selected_round = selected_round if best_score is not None else 1
    report.round_results = results
    report.elapsed_seconds = round(time.monotonic() - started, 3)
    report.cancelled = report.cancelled or cancelled
    return wires, report
