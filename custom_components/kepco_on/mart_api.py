"""Public store directories and published holiday dates from fixed HTTPS origins."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout

BRANDS = {
    "emart": "이마트·트레이더스",
    "everyday": "이마트 에브리데이",
    "nobrand": "노브랜드",
    "lottemart": "롯데마트",
    "lottesuper": "롯데슈퍼",
}
EMART_URL = "https://store.emart.com/branch/searchList.do"
LOTTE_STORES = "https://www.lotteon.com/p/lotteplus/offlinestore/selectStoreSearchList"
LOTTE_HOLIDAYS = "https://www.lotteon.com/p/lotteplus/holiday/selectHolidayList"
MAX_BODY = 3_000_000


class MartError(Exception):
    """Sanitized network or protocol error with no selected location or raw body."""


@dataclass(frozen=True, slots=True)
class Shop:
    """Only public fields needed for exact store selection and date lookup."""

    id: str
    name: str
    brand: str
    lookup: tuple[tuple[str, str], ...] = ()
    holidays: tuple[date, ...] = ()


@dataclass(frozen=True, slots=True)
class HolidaySchedule:
    """Coverage is explicit; an empty publication is not proof of no holidays."""

    dates: tuple[date, ...]
    months: frozenset[str]
    source: str = "official"


def month_key(day: date) -> str:
    return day.strftime("%Y-%m")


def next_month(day: date) -> date:
    return date(day.year + (day.month == 12), day.month % 12 + 1, 1)


def parse_dates(values: list[object], month: date | None = None) -> tuple[date, ...]:
    """Read complete official dates; do not extrapolate a second/fourth Sunday rule."""
    dates: set[date] = set()
    try:
        for value in values:
            if value in (None, ""):
                continue
            raw = str(value)
            if not re.fullmatch(r"[0-9]{8}", raw):
                raise ValueError
            day = date(int(raw[:4]), int(raw[4:6]), int(raw[6:]))
            if month and (day.year, day.month) != (month.year, month.month):
                raise ValueError
            dates.add(day)
    except ValueError:
        raise MartError("Unexpected holiday date") from None
    return tuple(sorted(dates))


def checked_text(value: object, *, identifier: bool = False) -> str:
    """Bound names and public IDs, before using them as form/query parameters."""
    result = str(value or "").strip()
    if (
        not result
        or len(result) > (40 if identifier else 100)
        or any(ord(char) < 32 for char in result)
        or (identifier and not re.fullmatch(r"[A-Za-z0-9_-]+", result))
    ):
        raise MartError("Unexpected store information")
    return result


def parse_catalog(payload: object, group: str, month: date) -> tuple[Shop, ...]:
    """Discard addresses, coordinates, identifiers of staff and all other fields."""
    if not isinstance(payload, dict):
        raise MartError("Unexpected store directory")
    rows = payload.get("dataList" if group == "emart" else "storeInfo")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 2000:
        raise MartError("Unexpected store directory")
    shops: list[Shop] = []
    identities: set[tuple[str, str]] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise MartError("Unexpected store directory")
        if group == "emart":
            name = checked_text(row.get("NAME"))
            brand = (
                "everyday" if "에브리데이" in name else "nobrand" if "노브랜드" in name else "emart"
            )
            shop = Shop(
                checked_text(row.get("ID"), identifier=True),
                name,
                brand,
                holidays=parse_dates(
                    [row.get(f"HOLIDAY_DAY{i}_YYYYMMDD") for i in (1, 2, 3)], month
                ),
            )
        else:
            name = checked_text(row.get("strNm"))
            if group == "lottemart" and (name.startswith("TRU") or "토이저러스" in name):
                continue
            fields = ("trNo", "lrtrNo", "oflnAfflCd", "strKndCd")
            # Some locations have no holiday lookup ID. Keep them selectable as unpublished.
            lookup = (
                tuple((key, checked_text(row[key], identifier=True)) for key in fields)
                if all(row.get(key) for key in fields)
                else ()
            )
            shop = Shop(checked_text(row.get("sstrCd"), identifier=True), name, group, lookup)
        identity = (shop.brand, shop.id)
        if identity in identities:
            raise MartError("Duplicate store identity")
        identities.add(identity)
        shops.append(shop)
    return tuple(shops)


async def request_json(
    session: ClientSession,
    url: str,
    *,
    form: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
) -> Any:
    """All URLs are internal constants; bound streams and refuse redirects."""
    try:
        async with session.request(
            "POST" if form is not None else "GET",
            url,
            data=form,
            params=params,
            allow_redirects=False,
            timeout=ClientTimeout(total=20),
            headers={"Accept": "application/json"},
        ) as response:
            if response.status not in {200, 201}:
                raise MartError("Official store information unavailable")
            body = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                body.extend(chunk)
                if len(body) > MAX_BODY:
                    raise MartError("Store response too large")
            return json.loads(body)
    except ClientError, TimeoutError, ValueError:
        raise MartError("Official store information unavailable") from None


async def fetch_catalog(session: ClientSession, brand: str, month: date) -> tuple[Shop, ...]:
    """Anonymous store lookup; no login cookies, member numbers or API keys."""
    if brand not in BRANDS:
        raise MartError("Unknown store brand")
    if brand in {"emart", "everyday", "nobrand"}:
        payload = await request_json(
            session,
            EMART_URL,
            form={
                "srchMode": "jijum",
                "year": str(month.year),
                "month": str(month.month),
                "areaId": "",
                "jMode": "true",
                "strConfirmYN": "N",
            },
        )
        return parse_catalog(payload, "emart", month)
    payload = await request_json(
        session,
        LOTTE_STORES,
        params={
            "mallNo": "1",
            "oflnMallNo": "4" if brand == "lottemart" else "5",
            "mbNo": "",
            "area1": "",
            "area2": "",
            "strNm": "",
            "scrollIndex": "0",
            "strKndCd": "",
            "osDvsCd": "W",
            "fromMallNo": "",
        },
    )
    return parse_catalog(payload, brand, month)


async def fetch_lotte_holidays(session: ClientSession, shop: Shop) -> tuple[date, ...]:
    """Use only the exact public identifiers returned by the official directory."""
    if not shop.lookup:
        return ()
    payload = await request_json(session, LOTTE_HOLIDAYS, params=dict(shop.lookup))
    if not isinstance(payload, dict) or not isinstance(payload.get("holidayList"), list):
        raise MartError("Unexpected holiday publication")
    rows = payload["holidayList"]
    if len(rows) > 100 or any(not isinstance(row, dict) for row in rows):
        raise MartError("Unexpected holiday publication")
    return parse_dates([row.get("holidayDate") for row in rows])
