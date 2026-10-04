"""Build factual station/topology data; never execute the source site's scripts.

Inputs are an already parsed JSON copy of Seoul Metro's getLineData.do response
and the official OA-12034 CSV (CP949). No network access or credentials required.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

LINE_IDS = {
    "1-GA": ("1032", "GTX-A"),
    "2-1": ("1001", "1호선"),
    "3-2": ("1002", "2호선"),
    "4-3": ("1003", "3호선"),
    "5-4": ("1004", "4호선"),
    "6-5": ("1005", "5호선"),
    "7-6": ("1006", "6호선"),
    "8-7": ("1007", "7호선"),
    "9-8": ("1008", "8호선"),
    "10-9": ("1009", "9호선"),
    "KP": ("gimpo", "김포골드라인"),
    "U": ("uijeongbu", "의정부경전철"),
    "I": ("incheon1", "인천1호선"),
    "I2": ("incheon2", "인천2호선"),
    "G": ("1067", "경춘선"),
    "K": ("1063", "경의중앙선"),
    "A": ("1065", "공항철도"),
    "B": ("1075", "수인분당선"),
    "S": ("1077", "신분당선"),
    "KK": ("1081", "경강선"),
    "E": ("everline", "용인에버라인"),
    "W": ("1092", "우이신설선"),
    "SH": ("1093", "서해선"),
    "SL": ("1094", "신림선"),
}


def station_name(value: str) -> str:
    """Retain distinguishing names and remove map-only line wrapping."""
    return value.replace("\n", "").strip()


def direction(line: str, first: dict[str, Any], second: dict[str, Any]) -> str:
    """API direction is explicit only where the topology establishes it."""
    if line not in {f"100{i}" for i in range(2, 10)}:
        return ""
    a, b = int(first["code"]), int(second["code"])
    if line == "1002":
        main = set(range(201, 244))
        if a in main and b in main:
            return "내선" if (b == a + 1 or (a, b) == (243, 201)) else "외선"
        return "하행" if b > a else "상행"
    if line in {"1003", "1004", "1005", "1006", "1007", "1008", "1009"}:
        return "하행" if b > a else "상행"
    return ""


def build(lines_file: Path, times_file: Path, updated: str) -> dict[str, Any]:
    """Convert source polylines into platforms and directed connections."""
    source = json.loads(lines_file.read_text(encoding="utf-8"))
    stations: dict[str, dict[str, Any]] = {}
    polylines: list[dict[str, Any]] = []
    edges: dict[tuple[str, str], dict[str, Any]] = {}
    for key, (line_id, _) in LINE_IDS.items():
        for record in source[key]["stations"]:
            if not record.get("station-nm"):
                continue
            code = record.get("station-cd") or record["data-uid"]
            x, y = map(float, record["data-coords"].split(","))
            node_id = f"{line_id}:{code}"
            name = station_name(record["station-nm"])
            if name == "경의서강대":
                name = "서강대"
            stations.setdefault(
                node_id,
                {
                    "id": node_id,
                    "name": name,
                    "line": line_id,
                    "x": x,
                    "y": y,
                    "labelPos": record.get("data-labelPos", "E"),
                    "group": record.get("data-uid", code),
                    "code": code,
                },
            )

    def connect(first_id: str, second_id: str, forward: str | None = None) -> None:
        if first_id == second_id:
            return
        first, second = stations[first_id], stations[second_id]
        line = first["line"]
        loop = line == "1006" and {first["code"], second["code"]} <= {
            str(i) for i in range(2611, 2617)
        }
        pairs = [(first_id, second_id)] if loop else [(first_id, second_id), (second_id, first_id)]
        for a, b in pairs:
            d = (
                forward
                if a == first_id and forward is not None
                else direction(line, stations[a], stations[b])
            )
            if forward is not None and a != first_id:
                d = "상행" if forward == "하행" else "하행"
            if loop:
                d = "상행"
            edges[(a, b)] = {
                "from": a,
                "to": b,
                "seconds": 180,
                "transfer": False,
                "direction": d,
                "estimated": True,
            }

    for key, (line_id, line_name) in LINE_IDS.items():
        paths: list[list[list[float]]] = [[]]
        previous: str | None = None
        source_line = source[key]
        for record in source_line["stations"]:
            code = record.get("station-cd") or record.get("data-uid")
            current = f"{line_id}:{code}" if code else None
            if current not in stations:
                current = None
            if record.get("data-moveTo"):
                paths.append([])
                # A named move point (광명) starts a branch which reconnects to
                # the next 금천구청 record; an unnamed reference resets the pen.
                previous = current
            elif current is not None:
                if previous is not None:
                    connect(previous, current, "하행" if line_id == "1001" else None)
                previous = current
            coords = record.get("data-coords") or record.get("data-moveTo")
            if coords:
                point = list(map(float, coords.split(",")))
                if not paths[-1] or paths[-1][-1] != point:
                    paths[-1].append(point)
        polylines.append(
            {
                "id": line_id,
                "name": line_name,
                "color": source_line["attr"]["data-color"],
                "paths": [path for path in paths if len(path) > 1],
            }
        )

    # The 광명 branch is drawn with a pen move, but the return record establishes
    # the physical link. Set its directions by the branch terminus, not pen order.
    for a, b, d in [("1001:1703", "1001:1750", "하행"), ("1001:1750", "1001:1703", "상행")]:
        edges[(a, b)]["direction"] = d

    by_name = {(s["line"], s["name"]): s["id"] for s in stations.values()}
    previous_row: dict[str, str] | None = None
    with times_file.open(encoding="cp949", newline="") as stream:
        for row in csv.DictReader(stream):
            minutes, seconds = map(int, row["소요시간"].split(":"))
            duration = minutes * 60 + seconds
            if previous_row is not None and previous_row["호선"] == row["호선"] and duration > 0:
                line = "100" + row["호선"]
                a = by_name.get((line, station_name(previous_row["역명"])))
                b = by_name.get((line, station_name(row["역명"])))
                for pair in [(a, b), (b, a)]:
                    if pair in edges:
                        edges[pair].update(seconds=duration, estimated=False)
            previous_row = row

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for station in stations.values():
        groups[station["group"]].append(station)
    for group in groups.values():
        for first in group:
            for second in group:
                if first["line"] == second["line"]:
                    continue
                edges[(first["id"], second["id"])] = {
                    "from": first["id"],
                    "to": second["id"],
                    "seconds": 300,
                    "transfer": True,
                    "direction": "",
                    "estimated": True,
                }
    return {
        "stations": list(stations.values()),
        "lines": polylines,
        "edges": list(edges.values()),
        "updated": updated,
        "source": {
            "topology": "http://www.seoulmetro.co.kr/kr/getLineData.do",
            "topology_page": "http://www.seoulmetro.co.kr/kr/cyberStation.do",
            "times": "https://data.seoul.go.kr/dataList/OA-12034/F/1/datasetView.do",
            "times_file": "서울교통공사 역간거리 및 소요시간_240810.csv",
            "times_license": "공공누리 1유형 (출처표시)",
            "attribution": "서울교통공사 제공 · HA for Korea에서 경로 데이터로 변환",
            "lines_sha256": hashlib.sha256(lines_file.read_bytes()).hexdigest(),
            "times_sha256": hashlib.sha256(times_file.read_bytes()).hexdigest(),
            "estimated_ride_seconds": 180,
            "estimated_transfer_seconds": 300,
        },
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lines", type=Path, required=True)
    parser.add_argument("--times", type=Path, required=True)
    parser.add_argument("--updated", required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("custom_components/kepco_on/frontend/subway-network.json"),
    )
    args = parser.parse_args()
    result = build(args.lines, args.times, args.updated)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8"
    )
