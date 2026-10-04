"""Regression checks for the bundled real-world capital-area topology."""

import json
from collections import defaultdict
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest

NETWORK_FILE = (
    Path(__file__).parents[1]
    / "custom_components"
    / "kepco_on"
    / "frontend"
    / "subway-network.json"
)


@pytest.fixture(scope="module")
def network_data() -> dict[str, Any]:
    result: dict[str, Any] = json.loads(NETWORK_FILE.read_text(encoding="utf-8"))
    return result


def test_network_integrity(network_data: dict[str, Any]) -> None:
    stations = {row["id"]: row for row in network_data["stations"]}
    assert len(stations) == len(network_data["stations"])
    assert len(stations) > 750
    line_ids = {row["id"] for row in network_data["lines"]}
    assert len(line_ids) == 24
    assert all(row["line"] in line_ids for row in stations.values())
    pairs: set[tuple[str, str]] = set()
    neighbors: dict[str, set[str]] = defaultdict(set)
    for edge in network_data["edges"]:
        a, b = edge["from"], edge["to"]
        assert a != b
        assert a in stations
        assert b in stations
        assert (a, b) not in pairs
        pairs.add((a, b))
        assert edge["seconds"] > 0
        assert edge["direction"] in {"상행", "하행", "내선", "외선", ""}
        if edge["transfer"]:
            assert stations[a]["group"] == stations[b]["group"]
            assert stations[a]["line"] != stations[b]["line"]
            assert edge["estimated"] is True
            assert edge["seconds"] == 300
        else:
            assert stations[a]["line"] == stations[b]["line"]
        neighbors[a].add(b)
        neighbors[b].add(a)
    visited: set[str] = set()
    pending = [next(iter(stations))]
    while pending:
        current = pending.pop()
        if current not in visited:
            visited.add(current)
            pending.extend(neighbors[current] - visited)
    assert visited == set(stations)


def test_hwarangdae_direction_and_official_time(network_data: dict[str, Any]) -> None:
    edges = {(row["from"], row["to"]): row for row in network_data["edges"]}
    towards_taereung = edges[("1006:2647", "1006:2646")]
    assert towards_taereung["direction"] == "상행"
    assert towards_taereung["seconds"] == 70
    assert towards_taereung["estimated"] is False
    assert edges[("1006:2647", "1006:2648")]["direction"] == "하행"
    assert edges[("1001:0150", "1001:0151")]["direction"] == "상행"


def test_branches_and_circular_closures(network_data: dict[str, Any]) -> None:
    pairs = {(row["from"], row["to"]) for row in network_data["edges"]}
    # Both ends of the 2호선 branches and the main circle must remain connected.
    for a, b in [
        ("1002:0233", "1002:0234"),
        ("1002:0247", "1002:0234"),
        ("1002:0211", "1002:0244"),
        ("1001:1701", "1001:1702"),
        ("1001:1703", "1001:1750"),
        ("1001:1716", "1001:1749"),
        ("1005:2549", "1005:2555"),
    ]:
        assert (a, b) in pairs
        assert (b, a) in pairs
    assert ("1002:0243", "1002:0201") in pairs
    # A drawing pen move must not create a shortcut between separate branches.
    assert ("1002:0234", "1002:0244") not in pairs
    assert ("1032:0011", "1032:9000") not in pairs
    assert ("1001:1812", "1001:1702") not in pairs


def test_eungam_loop_is_one_way(network_data: dict[str, Any]) -> None:
    pairs = {(row["from"], row["to"]) for row in network_data["edges"]}
    codes = ["2611", "2612", "2613", "2614", "2615", "2616", "2611"]
    for a, b in pairwise(codes):
        assert (f"1006:{a}", f"1006:{b}") in pairs
        assert (f"1006:{b}", f"1006:{a}") not in pairs
    assert ("1006:2611", "1006:2617") in pairs
    assert ("1006:2617", "1006:2611") in pairs


def test_same_name_does_not_create_a_transfer(network_data: dict[str, Any]) -> None:
    stations = network_data["stations"]
    sinchons = {row["id"] for row in stations if row["name"] == "신촌"}
    assert len(sinchons) == 2
    assert not any(
        row["from"] in sinchons and row["to"] in sinchons for row in network_data["edges"]
    )
    assert not any(row["name"].endswith("선착장") for row in stations)
    assert all("\n" not in row["name"] for row in stations)


def test_data_sources_and_estimates_are_preserved(network_data: dict[str, Any]) -> None:
    assert network_data["source"]["times_license"] == "공공누리 1유형 (출처표시)"
    assert network_data["source"]["times"].startswith("https://data.seoul.go.kr/")
    assert len(network_data["source"]["lines_sha256"]) == 64
    assert len(network_data["source"]["times_sha256"]) == 64
    assert any(not row["estimated"] for row in network_data["edges"])
    assert any(row["estimated"] and not row["transfer"] for row in network_data["edges"])
