"""Load the attributed, bundled subway topology and produce journey legs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .subway_route import Connection, RouteNetwork


class SubwayNetwork:
    """Keep line-specific platforms distinct even when names are identical."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.stations = {station["id"]: station for station in payload["stations"]}
        connections: dict[str, list[Connection]] = {node: [] for node in self.stations}
        self.estimated: dict[tuple[str, str], bool] = {}
        for edge in payload["edges"]:
            connections[edge["from"]].append(
                Connection(edge["to"], edge["seconds"], edge["transfer"], edge["direction"])
            )
            self.estimated[edge["from"], edge["to"]] = edge["estimated"]
        self.network = RouteNetwork({node: tuple(edges) for node, edges in connections.items()})

    def group(self, node: str) -> frozenset[str]:
        """A selected interchange can use any of its actual connected platforms."""
        if node not in self.stations:
            raise ValueError("알 수 없는 역입니다.")
        group = self.stations[node]["group"]
        return frozenset(key for key, station in self.stations.items() if station["group"] == group)

    def route(
        self, origin: str, destination: str, via: list[str], preference: str = "time"
    ) -> dict[str, Any]:
        """Optimize ordered vias and group adjacent rides into actionable legs."""
        stops = tuple(self.group(node) for node in [origin, *via, destination])
        route = self.network.find(stops, fewest_transfers=preference == "transfers")
        legs: list[dict[str, Any]] = []
        riding: list[str] = []
        duration = 0
        direction = ""

        def finish() -> None:
            if len(riding) < 2:
                return
            station = self.stations[riding[0]]
            legs.append(
                {
                    "line": station["line"],
                    "origin": station["name"],
                    "destination": self.stations[riding[-1]]["name"],
                    "next_station": self.stations[riding[1]]["name"],
                    "direction": direction,
                    "station_ids": list(riding),
                    "seconds": duration,
                }
            )

        for node, edge in zip(route.platforms, route.connections, strict=False):
            if edge.transfer:
                finish()
                riding = []
                duration = 0
                continue
            if not riding:
                riding = [node]
                direction = edge.direction
            riding.append(edge.target)
            duration += edge.seconds
        finish()
        return {
            "platforms": list(route.platforms),
            "seconds": route.seconds,
            "transfers": route.transfers,
            "legs": legs,
            "estimated": any(
                self.estimated[node, edge.target]
                for node, edge in zip(route.platforms, route.connections, strict=False)
            ),
            "origin": self.stations[origin]["name"],
            "destination": self.stations[destination]["name"],
        }


def load_network() -> SubwayNetwork:
    """Read only our packaged factual data, never execute downloaded scripts."""
    path = Path(__file__).parent / "frontend" / "subway-network.json"
    return SubwayNetwork(json.loads(path.read_text(encoding="utf-8")))
