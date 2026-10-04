"""Catalog and branding regressions using synthetic station names only."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from custom_components.kepco_on.subway_api import SubwayError
from custom_components.kepco_on.subway_catalog import (
    Station,
    StationCatalog,
    SubwayStationError,
    async_resolve_station,
    parse_catalog,
    resolve_station,
    station_aliases,
)

pytestmark = pytest.mark.usefixtures("socket_enabled")

STATIONS = (
    Station("1006", "1006000101", "테스트A(테스트학교)"),
    Station("1006", "1006000102", "테스트B순환(상선)"),
    Station("1006", "1006000103", "테스트C(공통부역명)"),
    Station("1006", "1006000104", "테스트D(공통부역명)"),
    Station("1007", "1007000101", "테스트A(다른학교)"),
    Station("1006", "1006000105", "테스트학교"),
)
NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def workbook(stations: tuple[Station, ...] = STATIONS) -> bytes:
    """Build a tiny Excel response; no production catalog is shipped as a fixture."""
    rows = [["SUBWAY_ID", "STATN_ID", "STATN_NM"]] + [
        [station.line, station.station_id, station.name] for station in stations
    ]
    values = list(dict.fromkeys(value for row in rows for value in row))
    strings = ET.Element(f"{{{NS}}}sst")
    for value in values:
        node = ET.SubElement(strings, f"{{{NS}}}si")
        ET.SubElement(node, f"{{{NS}}}t").text = value
    sheet = ET.Element(f"{{{NS}}}worksheet")
    data = ET.SubElement(sheet, f"{{{NS}}}sheetData")
    for index, values_in_row in enumerate(rows, 1):
        row = ET.SubElement(data, f"{{{NS}}}row", r=str(index))
        for column, value in zip("ABC", values_in_row, strict=True):
            cell = ET.SubElement(row, f"{{{NS}}}c", r=f"{column}{index}", t="s")
            ET.SubElement(cell, f"{{{NS}}}v").text = str(values.index(value))
    output = io.BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("xl/sharedStrings.xml", ET.tostring(strings))
        archive.writestr("xl/worksheets/sheet1.xml", ET.tostring(sheet))
    return output.getvalue()


def test_catalog_reads_complete_names_and_line_ids() -> None:
    assert parse_catalog(workbook()) == STATIONS
    for station in STATIONS:
        assert resolve_station(STATIONS, station.name, station.line) == station.name
        base = station.name.split("(", 1)[0]
        assert resolve_station(STATIONS, base, station.line) == station.name


@pytest.mark.parametrize(
    ("query", "line", "expected"),
    [
        ("테스트A", "1006", "테스트A(테스트학교)"),
        ("테스트A역", "1006", "테스트A(테스트학교)"),
        (" 테스트A \uff08 테스트학교 \uff09 ", "1006", "테스트A(테스트학교)"),
        ("테스트A", "1007", "테스트A(다른학교)"),
        ("테스트B", "1006", "테스트B순환(상선)"),
        ("테스트학교", "1006", "테스트학교"),
    ],
)
def test_aliases_resolve_with_line_filter_and_exact_name_priority(
    query: str,
    line: str,
    expected: str,
) -> None:
    assert resolve_station(STATIONS, query, line) == expected


@pytest.mark.parametrize(
    ("query", "line"),
    [("공통부역명", "1006"), ("상선", "1006"), ("없는역", "1006"), ("테스트A", "1001")],
)
def test_ambiguous_and_unknown_names_do_not_guess(query: str, line: str) -> None:
    with pytest.raises(SubwayStationError):
        resolve_station(STATIONS, query, line)


def test_parenthetical_landmark_aliases_and_commas() -> None:
    aliases = station_aliases("테스트(랜드마크A,랜드마크B)")
    assert {"테스트", "랜드마크A", "랜드마크B", "테스트역"} <= aliases
    assert "경의중앙선" not in station_aliases("테스트(경의중앙선)")


@pytest.mark.parametrize("raw", [b"not-a-zip", b""])
def test_invalid_catalog_is_safe(raw: bytes) -> None:
    with pytest.raises(SubwayError, match="Could not read station catalog"):
        parse_catalog(raw)


@pytest.mark.parametrize("content", [b"<!DOCTYPE forbidden>", b"<bad", b"<sst/>"])
def test_untrusted_xml_is_rejected(content: bytes) -> None:
    data = io.BytesIO()
    with ZipFile(data, "w") as archive:
        archive.writestr("xl/sharedStrings.xml", content)
        archive.writestr("xl/worksheets/sheet1.xml", content)
    with pytest.raises(SubwayError):
        parse_catalog(data.getvalue())


def test_empty_and_oversized_workbooks_are_rejected() -> None:
    with pytest.raises(SubwayError):
        parse_catalog(workbook(()))
    data = io.BytesIO()
    with ZipFile(data, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("huge.xml", b"a" * 5_000_001)
    with pytest.raises(SubwayError, match="too large"):
        parse_catalog(data.getvalue())


async def test_catalog_download_cache_and_shared_instance(hass: Any, aresponses: Any) -> None:
    aresponses.add(
        "datafile.seoul.go.kr",
        "/bigfile/iot/inf/nio_download.do",
        "get",
        aresponses.Response(body=workbook()),
    )
    assert await async_resolve_station(hass, "테스트A", "1006") == "테스트A(테스트학교)"
    assert await async_resolve_station(hass, "테스트B", "1006") == "테스트B순환(상선)"
    # Simulate HA restart: cached public data should work with no network call.
    catalog = StationCatalog(hass)
    assert await catalog.async_resolve("테스트A", "1007") == "테스트A(다른학교)"
    aresponses.assert_plan_strictly_followed()


@pytest.mark.parametrize(("status", "body"), [(302, b""), (500, b""), (200, b"a" * 2_000_001)])
async def test_catalog_download_errors_are_bounded(
    hass: Any, aresponses: Any, status: int, body: bytes
) -> None:
    aresponses.add(
        "datafile.seoul.go.kr",
        "/bigfile/iot/inf/nio_download.do",
        "get",
        aresponses.Response(status=status, body=body),
    )
    with pytest.raises(SubwayError):
        await StationCatalog(hass).async_resolve("테스트A", "1006")


async def test_catalog_transport_failure_is_safe(hass: Any) -> None:
    from aiohttp import ClientConnectionError

    with patch("custom_components.kepco_on.subway_catalog.async_get_clientsession") as session:
        session.return_value.get.side_effect = ClientConnectionError("PRIVATE_URL_CANARY")
        with pytest.raises(SubwayError) as caught:
            await StationCatalog(hass).async_resolve("테스트A", "1006")
    assert "PRIVATE_URL_CANARY" not in str(caught.value)


async def test_invalid_station_has_specific_setup_error() -> None:
    from tests.test_config_flow import make_flow
    from tests.test_subway import SETTINGS

    with patch(
        "custom_components.kepco_on.subway_flow.async_query",
        new=AsyncMock(side_effect=SubwayStationError("safe")),
    ):
        result = await make_flow().async_step_subway(SETTINGS)
    assert result["errors"] == {"base": "subway_invalid_station"}


def test_general_integration_titles_are_independent_of_service() -> None:
    root = Path(__file__).resolve().parents[1] / "custom_components/kepco_on"
    for path in [
        root / "manifest.json",
        root / "strings.json",
        root / "translations/en.json",
        root / "translations/ko.json",
    ]:
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data.get("name", data.get("title")) == "HA for Korea"
    svg = (root / "brand/icon.svg").read_text(encoding="utf-8")
    assert "KEPCO" not in svg
    assert "한전" not in svg
    for theme in ("", "dark_"):
        for resolution in ("", "@2x"):
            for asset in ("icon", "logo"):
                data = (root / f"brand/{theme}{asset}{resolution}.png").read_bytes()
                assert data.startswith(b"\x89PNG\r\n\x1a\n")
