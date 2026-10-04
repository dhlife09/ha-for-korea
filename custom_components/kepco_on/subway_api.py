"""Seoul public arrival API, implemented independently from its specification."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote

from aiohttp import ClientError, ClientSession, ClientTimeout

API_ORIGIN = "http://swopenapi.seoul.go.kr"
SERVICE = "seoul_subway"
CONF_SERVICE = "service"
CONF_API_KEY = "api_key"
CONF_STATION = "station"
CONF_LINE = "line_id"
CONF_HTTP = "allow_http"
OPT_INTERVAL = "subway_interval_seconds"
DEFAULT_INTERVAL = 120
DAILY_LIMIT = 950
LINES = {
    "1001": "1호선",
    "1002": "2호선",
    "1003": "3호선",
    "1004": "4호선",
    "1005": "5호선",
    "1006": "6호선",
    "1007": "7호선",
    "1008": "8호선",
    "1009": "9호선",
    "1063": "경의중앙선",
    "1065": "공항철도",
    "1067": "경춘선",
    "1075": "수인분당선",
    "1077": "신분당선",
    "1092": "우이신설선",
    "1093": "서해선",
    "1081": "경강선",
    "1032": "GTX-A",
}
DIRECTIONS = ("상행", "하행", "내선", "외선")


class SubwayError(Exception):
    """An error with a safe, fixed message; never carry a request URL."""


class SubwayAuthError(SubwayError):
    """Invalid API key."""


class SubwayQuotaError(SubwayError):
    """Daily request budget exhausted."""


def normalize_station(value: str) -> str:
    """Normalize surrounding whitespace, retaining exact API station names."""
    return value.strip()


def validate_settings(data: dict[str, Any]) -> None:
    """Reject path injection and require explicit plaintext transport consent."""
    key = str(data.get(CONF_API_KEY, "")).strip()
    station = normalize_station(str(data.get(CONF_STATION, "")))
    if re.fullmatch(r"[A-Za-z0-9]{10,128}", key) is None:
        raise SubwayAuthError("Invalid API key format")
    if not station or len(station) > 50 or any(c in station for c in "/\\?#\r\n"):
        raise SubwayError("Invalid station")
    if data.get(CONF_LINE) not in LINES:
        raise SubwayError("Invalid line")
    if data.get(CONF_HTTP) is not True:
        raise SubwayError("HTTP consent required")


def settings_id(station: str, line: str) -> str:
    """Stable identifier independent of credentials; avoid exposing location in IDs."""
    return "subway_" + hashlib.sha256(f"{station}:{line}".encode()).hexdigest()[:24]


@dataclass(frozen=True, slots=True)
class Arrival:
    """A whitelisted public train record."""

    direction: str
    destination: str
    message: str
    position: str
    train_number: str
    train_type: str
    seconds: int | None
    generated_at: datetime | None
    code: str
    last_train: bool
    order: str


def parse_response(payload: object, station: str, line: str) -> tuple[Arrival, ...]:
    """Check API status, filter exact station/line, deduplicate and order trains."""
    if not isinstance(payload, dict):
        raise SubwayError("Invalid API response")
    status = payload.get("errorMessage", payload.get("RESULT", payload))
    if not isinstance(status, dict):
        raise SubwayError("Invalid API status")
    code = status.get("code", status.get("CODE"))
    if code == "INFO-100":
        raise SubwayAuthError("API key rejected")
    if code in {"ERROR-337", "ERROR-338", "ERROR-429"}:
        raise SubwayQuotaError("API request quota exhausted")
    if code == "INFO-200":
        return ()
    if code != "INFO-000":
        raise SubwayError("API request failed")
    records = payload.get("realtimeArrivalList")
    if not isinstance(records, list):
        raise SubwayError("Invalid arrival list")
    result: list[Arrival] = []
    seen: set[tuple[str, str, str]] = set()
    for record in records:
        if not isinstance(record, dict):
            raise SubwayError("Invalid arrival record")
        if str(record.get("subwayId")) != line or record.get("statnNm") != station:
            continue
        direction = str(record.get("updnLine", ""))
        if direction not in DIRECTIONS:
            continue
        train = str(record.get("btrainNo", ""))
        destination = str(record.get("bstatnNm", ""))
        identity = (direction, train, destination)
        if train and identity in seen:
            continue
        seen.add(identity)
        raw_seconds = str(record.get("barvlDt", ""))
        # Zero in this API can mean unknown, rather than a train arriving now.
        seconds = int(raw_seconds) if raw_seconds.isdigit() and int(raw_seconds) > 0 else None
        generated: datetime | None = None
        with suppress(ValueError):
            generated = datetime.fromisoformat(str(record.get("recptnDt", "")))
        result.append(
            Arrival(
                direction=direction,
                destination=destination,
                message=str(record.get("arvlMsg2", ""))[:255],
                position=str(record.get("arvlMsg3", ""))[:255],
                train_number=train,
                train_type=str(record.get("btrainSttus", "")),
                seconds=seconds,
                generated_at=generated,
                code=str(record.get("arvlCd", "")),
                last_train=str(record.get("lstcarAt", "0")) == "1",
                order=str(record.get("ordkey", "")),
            )
        )
    return tuple(sorted(result, key=lambda a: (a.direction, a.order)))


async def fetch_arrivals(
    session: ClientSession,
    key: str,
    station: str,
    line: str,
) -> tuple[Arrival, ...]:
    """Contact only the documented Seoul host; do not follow redirects or log URLs."""
    url = (
        f"{API_ORIGIN}/api/subway/{quote(key, safe='')}/json/realtimeStationArrival/0/1000/"
        f"{quote(station, safe='')}"
    )
    try:
        async with session.get(
            url,
            timeout=ClientTimeout(total=15),
            allow_redirects=False,
        ) as response:
            if response.status in {401, 403}:
                raise SubwayAuthError("API key rejected")
            if response.status == 429:
                raise SubwayQuotaError("API request quota exhausted")
            if response.status != 200:
                raise SubwayError("API server unavailable")
            chunks = []
            size = 0
            async for chunk in response.content.iter_chunked(65536):
                chunks.append(chunk)
                size += len(chunk)
                if size > 2_000_000:
                    raise SubwayError("API response too large")
            return parse_response(json.loads(b"".join(chunks)), station, line)
    except ClientError, TimeoutError, UnicodeError, ValueError:
        raise SubwayError("Could not read Seoul arrival API") from None


class RequestBudget:
    """Serialize and persist request reservations across entries sharing a key."""

    def __init__(self, store: Any) -> None:
        self.store = store
        self.lock = asyncio.Lock()
        self.state: dict[str, Any] | None = None

    async def reserve(self, day: str) -> None:
        """Count every attempt before transmission, including failed requests."""
        async with self.lock:
            if self.state is None:
                self.state = await self.store.async_load() or {}
            if self.state.get("day") != day:
                self.state = {"day": day, "count": 0}
            if self.state["count"] >= DAILY_LIMIT:
                raise SubwayQuotaError("Local daily request budget exhausted")
            self.state["count"] += 1
            await self.store.async_save(self.state)
