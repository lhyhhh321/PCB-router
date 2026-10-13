"""Optional integer PyTorch congestion fields; A* itself stays on the CPU.

Each foreign route contributes at most one conflict at a cell, even when many
of its path cells overlap that cell's clearance disk. No floating point GPU
arithmetic or reductions are used for routing costs.
"""
from __future__ import annotations

import math


class TorchCosts:
    # Bound dense scratch space and broadcast temporaries, including on GPUs.
    MAX_FIELD_CELLS = 1_000_000
    MAX_ROUTE_CELLS = 32_000_000
    MAX_PAIRS = 500_000

    def __init__(self, device: str):
        try:
            import torch
        except ImportError as exc:
            raise ValueError('PyTorch acceleration requires: python -m pip install -r requirements-torch.txt') from exc
        if device == 'auto':
            device = ('cuda' if torch.cuda.is_available() else
                      'mps' if torch.backends.mps.is_available() else 'cpu')
        if device == 'cuda' and not torch.cuda.is_available():
            raise ValueError('PyTorch CUDA acceleration is unavailable on this machine.')
        if device == 'mps' and not torch.backends.mps.is_available():
            raise ValueError('PyTorch MPS acceleration is unavailable on this machine.')
        self.torch = torch
        self.device = device
        self.fields_built = 0
        self.fields_skipped = 0

    def build(self, router, job, bounds):
        """Snapshot one search's occupancy/history. Return host lists or None.

        Routes cannot change during a synchronous A* call. A fresh field on
        every call also handles rip-up, rollback and history updates safely.
        """
        torch = self.torch
        x1, x2, y1, y2 = bounds
        width, height = x2 - x1 + 1, y2 - y1 + 1
        area = width * height
        foreign = [other for other in router.jobs
                   if other.id in router.routes and other.net != job.net]
        if area > min(self.MAX_FIELD_CELLS, self.MAX_ROUTE_CELLS):
            self.fields_skipped += 1
            return None
        offsets = router._offsets(job.width / 2 + router.clearance + router.max_half_width)
        # Subtracting large floating coordinates can differ from offset * step.
        # If that rounding could flip a clearance decision, use scalar checks.
        coordinate_error = 8 * max(math.ulp(value) for value in (
            router.origin_x, router.origin_y,
            router.origin_x + (router.cols - 1) * router.step,
            router.origin_y + (router.rows - 1) * router.step))
        allowed_by_width = {}
        for other in foreign:
            if other.width in allowed_by_width:
                continue
            required = abs(job.width / 2 + other.width / 2 + router.clearance - 1e-7)
            allowed = []
            for dx, dy in offsets:
                distance = math.hypot(dx * router.step, dy * router.step)
                if (dx or dy) and abs(distance - required) <= coordinate_error + 8 * math.ulp(distance):
                    self.fields_skipped += 1
                    return None
                if (dx * router.step) ** 2 + (dy * router.step) ** 2 < required ** 2:
                    allowed.append((dx, dy))
            allowed_by_width[other.width] = allowed
        # Match the scalar distance predicate (including its epsilon) on CPU.
        with torch.inference_mode():
            counts = torch.zeros(area, dtype=torch.int32, device=self.device)
            routes_per_batch = max(1, self.MAX_ROUTE_CELLS // area)
            for first in range(0, len(foreign), routes_per_batch):
                batch = foreign[first:first + routes_per_batch]
                hits = torch.zeros(len(batch) * area, dtype=torch.bool, device=self.device)
                # Width groups share a clearance stencil. Pack many routes into
                # one launch rather than synchronizing the GPU for each trace.
                by_width = {}
                for index, other in enumerate(batch):
                    allowed = allowed_by_width[other.width]
                    if not allowed:
                        continue
                    radius = max(max(abs(dx), abs(dy)) for dx, dy in allowed)
                    points = by_width.setdefault(other.width, [])
                    points.extend((x, y, index) for x, y in router.routes[other.id]
                                  if x1-radius <= x <= x2+radius and y1-radius <= y <= y2+radius)
                for route_width, cells in by_width.items():
                    if not cells:
                        continue
                    allowed = allowed_by_width[route_width]
                    delta = torch.tensor(allowed, dtype=torch.int64, device=self.device)
                    chunk = max(1, self.MAX_PAIRS // len(allowed))
                    for start in range(0, len(cells), chunk):
                        points = torch.tensor(cells[start:start+chunk], dtype=torch.int64, device=self.device)
                        x = points[:, 0, None] - delta[None, :, 0] - x1
                        y = points[:, 1, None] - delta[None, :, 1] - y1
                        valid = (x >= 0) & (x < width) & (y >= 0) & (y < height)
                        keys = (points[:, 2, None] * area + y * width + x)[valid]
                        hits.index_fill_(0, keys, True)
                counts += hits.reshape(-1, area).sum(dim=0, dtype=torch.int32)
            history = torch.zeros(area, dtype=torch.int64, device=self.device)
            if router.history:
                radius = max(max(abs(dx), abs(dy)) for dx, dy in offsets)
                entries = [(router._cell_from_key(key), value) for key, value in router.history.items()]
                entries = [(cell, value) for cell, value in entries
                           if x1-radius <= cell[0] <= x2+radius and y1-radius <= cell[1] <= y2+radius]
                delta = torch.tensor(offsets, dtype=torch.int64, device=self.device)
                chunk = max(1, self.MAX_PAIRS // len(offsets))
                for start in range(0, len(entries), chunk):
                    batch = entries[start:start+chunk]
                    points = torch.tensor([cell for cell, _ in batch], dtype=torch.int64, device=self.device)
                    values = torch.tensor([value for _, value in batch], dtype=torch.int64, device=self.device)
                    x = points[:, 0, None] - delta[None, :, 0] - x1
                    y = points[:, 1, None] - delta[None, :, 1] - y1
                    valid = (x >= 0) & (x < width) & (y >= 0) & (y < height)
                    keys = (y * width + x)[valid]
                    history.scatter_add_(0, keys, values[:, None].expand_as(x)[valid])
            self.fields_built += 1
            return counts.cpu().tolist(), history.cpu().tolist()
