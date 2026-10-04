"""Validate travel optimization and ordered vias with synthetic networks."""

import pytest
from custom_components.kepco_on.subway_route import Connection, RouteNetwork


def test_route_optimization() -> None:
    network = RouteNetwork(
        {
            "a": (Connection("b", 60, True), Connection("c", 200)),
            "b": (Connection("d", 60),),
            "c": (Connection("d", 200),),
            "d": (),
        }
    )
    stops = (frozenset({"a"}), frozenset({"d"}))
    assert network.find(stops).seconds == 120
    assert network.find(stops).transfers == 1
    assert network.find(stops, fewest_transfers=True).platforms == ("a", "c", "d")
    assert network.find((stops[0], frozenset({"c"}), stops[1])).seconds == 400


def test_invalid_and_disconnected_routes() -> None:
    with pytest.raises(ValueError, match="Invalid"):
        RouteNetwork({"a": (Connection("missing", 60),)})
    network = RouteNetwork({"a": (), "b": ()})
    with pytest.raises(ValueError, match="Unknown"):
        network.find((frozenset({"a"}), frozenset({"unknown"})))
    with pytest.raises(ValueError, match="No route"):
        network.find((frozenset({"a"}), frozenset({"b"})))
