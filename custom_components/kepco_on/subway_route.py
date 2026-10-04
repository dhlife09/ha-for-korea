"""Route through a sourced network of line-specific station platforms."""

from __future__ import annotations

import heapq
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Connection:
    """One directed ride or transfer with a source-provided travel duration."""

    target: str
    seconds: int
    transfer: bool = False
    direction: str = ""


@dataclass(frozen=True, slots=True)
class Route:
    """Ordered platform IDs and connections; never infer rider location."""

    platforms: tuple[str, ...]
    connections: tuple[Connection, ...]

    @property
    def seconds(self) -> int:
        return sum(edge.seconds for edge in self.connections)

    @property
    def transfers(self) -> int:
        return sum(edge.transfer for edge in self.connections)


class RouteNetwork:
    """Dijkstra search with ordered station groups for interchange endpoints."""

    def __init__(self, connections: dict[str, tuple[Connection, ...]]) -> None:
        self.connections = dict(connections)
        if not self.connections or any(
            edge.target not in self.connections or edge.seconds <= 0
            for edges in self.connections.values()
            for edge in edges
        ):
            raise ValueError("Invalid subway connections")

    def find(
        self,
        stops: tuple[frozenset[str], ...],
        *,
        fewest_transfers: bool = False,
    ) -> Route:
        """Optimize the complete journey, preserving platform at each via stop."""
        if len(stops) < 2 or any(
            not group or not group <= self.connections.keys() for group in stops
        ):
            raise ValueError("Unknown or empty route stop")
        queue: list[tuple[int, int, str, int]] = []
        best: dict[tuple[str, int], tuple[int, int]] = {}
        previous: dict[tuple[str, int], tuple[tuple[str, int], Connection]] = {}
        for origin in sorted(stops[0]):
            stage = 1
            while stage < len(stops) and origin in stops[stage]:
                stage += 1
            best[origin, stage] = (0, 0)
            heapq.heappush(queue, (0, 0, origin, stage))
        while queue:
            first, second, node, stage = heapq.heappop(queue)
            state = (node, stage)
            if best[state] != (first, second):
                continue
            if stage == len(stops):
                nodes = [node]
                edges = []
                while state in previous:
                    state, edge = previous[state]
                    nodes.append(state[0])
                    edges.append(edge)
                return Route(tuple(reversed(nodes)), tuple(reversed(edges)))
            for edge in self.connections[node]:
                next_stage = stage
                while next_stage < len(stops) and edge.target in stops[next_stage]:
                    next_stage += 1
                next_state = (edge.target, next_stage)
                cost = (
                    (first + int(edge.transfer), second + edge.seconds)
                    if fewest_transfers
                    else (first + edge.seconds, second + int(edge.transfer))
                )
                if next_state in best and best[next_state] <= cost:
                    continue
                best[next_state] = cost
                previous[next_state] = (state, edge)
                heapq.heappush(queue, (*cost, edge.target, next_stage))
        raise ValueError("No route connects the selected stops")
