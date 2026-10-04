"""Resolve station aliases from Seoul's complete public arrival station workbook."""

from __future__ import annotations

import asyncio
import io
import re
import unicodedata
from dataclasses import dataclass
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

from aiohttp import ClientError, ClientTimeout
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .subway_api import SubwayError

# Public workbook linked by the official OA-12764 dataset. No credential is sent.
CATALOG_URL = (
    "https://datafile.seoul.go.kr/bigfile/iot/inf/nio_download.do"
    "?infId=OA-12764&seq=57&infSeq=2&useCache=false"
)
CATALOG_VERSION = "20260902"
XML_NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


class SubwayStationError(SubwayError):
    """No unique station match on the selected line."""


@dataclass(frozen=True, slots=True)
class Station:
    """One official API station; the complete name remains local runtime data."""

    line: str
    station_id: str
    name: str


def _xml(data: bytes) -> ET.Element:
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise SubwayError("Invalid station catalog XML")
    return ET.fromstring(data)


def parse_catalog(raw: bytes) -> tuple[Station, ...]:
    """Read only the required workbook cells with bounded decompression."""
    try:
        with ZipFile(io.BytesIO(raw)) as archive:
            if sum(info.file_size for info in archive.infolist()) > 5_000_000:
                raise SubwayError("Station catalog too large")
            strings = [
                "".join(node.itertext())
                for node in _xml(archive.read("xl/sharedStrings.xml")).findall("s:si", XML_NS)
            ]
            rows = []
            for row in _xml(archive.read("xl/worksheets/sheet1.xml")).findall(".//s:row", XML_NS):
                cells: dict[str, str] = {}
                for cell in row.findall("s:c", XML_NS):
                    column = re.sub(r"[0-9]", "", cell.attrib.get("r", ""))
                    value = cell.find("s:v", XML_NS)
                    text = value.text if value is not None and value.text else ""
                    if cell.attrib.get("t") == "s":
                        text = strings[int(text)]
                    cells[column] = text
                rows.append(cells)
            if not rows:
                raise SubwayError("Empty station catalog")
            columns = {value: key for key, value in rows[0].items()}
            result = tuple(
                Station(
                    row[columns["SUBWAY_ID"]], row[columns["STATN_ID"]], row[columns["STATN_NM"]]
                )
                for row in rows[1:]
                if row.get(columns["STATN_NM"])
            )
            if not result or any(
                not station.line.isdigit() or not station.station_id.isdigit() for station in result
            ):
                raise SubwayError("Invalid station catalog records")
            return result
    except BadZipFile, KeyError, IndexError, ValueError, ET.ParseError:
        raise SubwayError("Could not read station catalog") from None


def _name(value: str) -> str:
    """Treat whitespace and full-width brackets consistently."""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value))


def station_aliases(name: str) -> set[str]:
    """Derive aliases uniformly, without embedding any user's station."""
    name = _name(name)
    base = name.split("(", 1)[0]
    aliases = {name, base}
    for subtitle in re.findall(r"\(([^()]*)\)", name):
        # Parentheses can hold a local station name, a landmark, or a line label.
        if subtitle.endswith("선"):
            continue
        aliases.add(subtitle)
        aliases.update(part for part in subtitle.split(",") if part)
    if base.endswith("순환"):
        aliases.add(base.removesuffix("순환"))
    aliases.update(alias + "역" for alias in tuple(aliases) if not alias.endswith("역"))
    return aliases


def resolve_station(stations: tuple[Station, ...], requested: str, line: str) -> str:
    """Prefer exact official names; refuse ambiguous aliases instead of guessing."""
    name = _name(requested)
    same_line = [station for station in stations if station.line == line]
    exact = {station.name for station in same_line if _name(station.name) == name}
    matches = exact or {
        station.name for station in same_line if name in station_aliases(station.name)
    }
    if len(matches) != 1:
        raise SubwayStationError("Station or line needs a unique official name")
    return matches.pop()


class StationCatalog:
    """Cache the public reference list independently of credentials and preferences."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.store: Store[list[dict[str, str]]] = Store(
            hass,
            1,
            f"{DOMAIN}_subway_catalog_{CATALOG_VERSION}",
        )
        self.lock = asyncio.Lock()
        self.stations: tuple[Station, ...] | None = None

    async def async_resolve(self, station: str, line: str) -> str:
        async with self.lock:
            if self.stations is None:
                cached = await self.store.async_load()
                if cached:
                    self.stations = tuple(Station(**row) for row in cached)
                else:
                    self.stations = await self._async_download()
                    await self.store.async_save(
                        [
                            {"line": row.line, "station_id": row.station_id, "name": row.name}
                            for row in self.stations
                        ]
                    )
        return resolve_station(self.stations, station, line)

    async def _async_download(self) -> tuple[Station, ...]:
        try:
            async with async_get_clientsession(self.hass).get(
                CATALOG_URL,
                timeout=ClientTimeout(total=20),
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    raise SubwayError("Station catalog unavailable")
                chunks = []
                size = 0
                async for chunk in response.content.iter_chunked(65536):
                    size += len(chunk)
                    if size > 2_000_000:
                        raise SubwayError("Station catalog download too large")
                    chunks.append(chunk)
            return await self.hass.async_add_executor_job(parse_catalog, b"".join(chunks))
        except ClientError, TimeoutError:
            raise SubwayError("Station catalog unavailable") from None


async def async_resolve_station(hass: HomeAssistant, station: str, line: str) -> str:
    """Reuse the reference cache across all configured stations."""
    key = f"{DOMAIN}_subway_catalog"
    if key not in hass.data:
        hass.data[key] = StationCatalog(hass)
    catalog: StationCatalog = hass.data[key]
    return await catalog.async_resolve(station, line)
