"""Ordered routing blocks shared by the router, CLI, and pipeline editor."""
from dataclasses import dataclass
import json
from pathlib import Path


ROUTING_BLOCKS = {
    "initial": ("Place connections", "Try pattern routes and A* in the configured routing priority."),
    "early_refinement": ("Refine placement", "After Place connections or Place & rip-up, refine at checkpoints during placement. Otherwise refine the current routes once."),
    "repair": ("Repair congestion", "Rip up and reroute conflicting traces using congestion history and strict crossing checks."),
    "place_ripup": ("Place & rip-up", "Try a clear path, then lift and reroute movable blockers if needed."),
    "legalization": ("Legalize routes", "Remove conflicting routes and retry them with hard clearance constraints."),
    "missing_route_swaps": ("Swap routes to make room", "Try connecting missing traces by moving a bounded number of blocking routes."),
    "combined": ("Compact board traces", "Apply the board-wide force model to eligible fixed and generated copper."),
    "detour_guard": ("Reject excessive detours", "Remove routes that exceed the configured maximum stretch."),
    "detailed_straighten": ("Straighten traces", "Replace bends with shorter or simpler legal segments."),
    "detailed_astar": ("Connect with A*", "Search for hard-obstacle paths for missing connections."),
    "detailed_drag": ("Drag blocking segments", "Slide blocking traces to open a path for missing connections."),
    "crossing_cleanup": ("Remove crossings", "Remove generated routes with physical centerline crossings."),
}

DETAIL_BLOCKS = ("detailed_straighten", "detailed_astar", "detailed_drag")
PLACEMENT_BLOCKS = ("initial", "place_ripup")


@dataclass(frozen=True)
class RoutingPipeline:
    name: str
    blocks: tuple[str, ...]

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("A pipeline needs a name.")
        if not isinstance(self.blocks, (list, tuple)):
            raise ValueError("Pipeline blocks must be an ordered list.")
        for block in self.blocks:
            if not isinstance(block, str) or block not in ROUTING_BLOCKS:
                raise ValueError(f"Unknown routing block: {block!r}.")
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "blocks", tuple(self.blocks))

    def to_dict(self):
        return {"version": 1, "name": self.name, "blocks": list(self.blocks)}

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
            raise ValueError("Unsupported pipeline file version. Expected version 1.")
        return cls(data.get("name"), data.get("blocks"))

    @classmethod
    def load(cls, path):
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def steps(self):
        """Keep checkpoint placement and consecutive trace-repair blocks in their loops."""
        index = 0
        while index < len(self.blocks):
            block = self.blocks[index]
            end = index + 1
            if block in DETAIL_BLOCKS:
                while end < len(self.blocks) and self.blocks[end] in DETAIL_BLOCKS:
                    end += 1
                stage = "detailed"
            else:
                stage = block
                companion = ("early_refinement" if block in PLACEMENT_BLOCKS else
                             "missing_route_swaps" if block == "legalization" else None)
                if end < len(self.blocks) and self.blocks[end] == companion:
                    end += 1
            yield stage, self.blocks[index:end]
            index = end


PIPELINE_1 = RoutingPipeline("Pipeline 1", (
    "initial", "early_refinement", "repair", "legalization", "missing_route_swaps",
    "combined", "detour_guard", *DETAIL_BLOCKS, "detour_guard", "crossing_cleanup",
))
