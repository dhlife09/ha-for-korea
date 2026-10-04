"""Monthly RFID records from the Environment Corporation's HTTPS quick lookup."""

from __future__ import annotations

import calendar
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from aiohttp import ClientError, ClientSession, ClientTimeout

SERVICE = "food_waste"
CONF_TAG = "rfid_tag"
CONF_BUILDING = "building"
CONF_UNIT = "unit"
URL = "https://www.citywaste.or.kr/portal/status/selectDischargerQuantityQuickMonthNew.do"
MAX_BODY = 1_000_000
MAX_PAGES = 100


class WasteError(Exception):
    """Safe error containing no household identifiers or upstream messages."""


class WasteLookupError(WasteError):
    """The service rejected the supplied lookup information."""


def validate_settings(data: Mapping[str, object]) -> dict[str, str]:
    """Reject malformed identifiers before contacting the official origin."""
    values = {key: str(data.get(key, "")).strip() for key in (CONF_TAG, CONF_BUILDING, CONF_UNIT)}
    if not re.fullmatch(r"[A-Za-z0-9]{8,40}", values[CONF_TAG]):
        raise WasteLookupError("Check RFID lookup information")
    if any(not re.fullmatch(r"[0-9]{1,8}", values[key]) for key in (CONF_BUILDING, CONF_UNIT)):
        raise WasteLookupError("Check RFID lookup information")
    return values


def settings_id(data: Mapping[str, object]) -> str:
    """Hash household identity; never expose a tag or address in entity IDs."""
    values = validate_settings(data)
    return SERVICE + "_" + hashlib.sha256("|".join(values.values()).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class MonthSummary:
    """Only aggregate data, without names, tag numbers or raw household records."""

    month: str
    kilograms: Decimal | None
    count: int


def previous_month(today: date) -> date:
    """Handle January without a fixed-duration approximation."""
    return date(today.year - (today.month == 1), today.month - 1 or 12, 1)


def parse_page(payload: object, month: date, page: int) -> tuple[int, int, list[Decimal]]:
    """Validate pagination and each record before summing all monthly pages."""
    if not isinstance(payload, dict):
        raise WasteError("Unexpected RFID response")
    if payload.get("msgCode") == "error":
        raise WasteLookupError("Check RFID lookup information")
    if payload.get("msgCode") != "success":
        raise WasteError("Unexpected RFID response")
    rows = payload.get("list")
    total = payload.get("totalCnt")
    pagination = payload.get("paginationInfo")
    if (
        not isinstance(rows, list)
        or type(total) is not int
        or total < 0
        or not isinstance(pagination, dict)
    ):
        raise WasteError("Unexpected RFID response")
    pages = pagination.get("totalPageCount")
    current = pagination.get("currentPageNo")
    size = pagination.get("recordCountPerPage")
    if (
        type(pages) is not int
        or not 0 <= pages <= MAX_PAGES
        or current != page
        or type(size) is not int
        or not 1 <= size <= 100
        or pagination.get("totalRecordCount") != total
        or pages != (total + size - 1) // size
        or len(rows) != min(size, max(0, total - (page - 1) * size))
    ):
        raise WasteError("Incomplete RFID response")
    weights: list[Decimal] = []
    for row in rows:
        try:
            if not isinstance(row, dict):
                raise ValueError
            timestamp = datetime.strptime(str(row["dttime"]), "%Y-%m-%d %H:%M:%S")
            weight = Decimal(str(row["qtyvalue"]))
            if (
                (timestamp.year, timestamp.month) != (month.year, month.month)
                or not weight.is_finite()
                or not 0 <= weight <= 10000
            ):
                raise ValueError
        except KeyError, ValueError, InvalidOperation:
            raise WasteError("Unexpected RFID record") from None
        weights.append(weight)
    return total, pages, weights


async def fetch_month(
    session: ClientSession, data: Mapping[str, object], month: date, today: date
) -> MonthSummary:
    """POST household identifiers only to the fixed HTTPS origin, never a redirect."""
    values = validate_settings(data)
    end_day = calendar.monthrange(month.year, month.month)[1]
    if (month.year, month.month) == (today.year, today.month):
        end_day = today.day
    form = {
        "tagprintcd": values[CONF_TAG],
        "aptdong": values[CONF_BUILDING],
        "apthono": values[CONF_UNIT],
        "startchdate": month.replace(day=1).strftime("%Y%m%d"),
        "endchdate": month.replace(day=end_day).strftime("%Y%m%d"),
    }
    total_expected: int | None = None
    page_count = 1
    count = 0
    total_weight = Decimal(0)
    page = 1
    while page <= page_count:
        try:
            async with session.post(
                URL,
                data={**form, "pageIndex": str(page)},
                headers={
                    "Referer": "https://www.citywaste.or.kr/portal/status/selectSimpleEmissionQuantity.do",
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "application/json",
                },
                allow_redirects=False,
                timeout=ClientTimeout(total=20),
            ) as response:
                if response.status != 200:
                    raise WasteError("RFID lookup unavailable")
                body = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    body.extend(chunk)
                    if len(body) > MAX_BODY:
                        raise WasteError("RFID response too large")
                payload = json.loads(body)
        except ClientError, TimeoutError, ValueError:
            raise WasteError("RFID lookup unavailable") from None
        total, pages, weights = parse_page(payload, month, page)
        if total_expected is not None and (total != total_expected or pages != page_count):
            raise WasteError("RFID records changed during lookup; retry later")
        total_expected, page_count = total, pages
        count += len(weights)
        total_weight += sum(weights, Decimal(0))
        page += 1
    # An empty result cannot distinguish no disposal from incorrect identifiers.
    return MonthSummary(month.strftime("%Y-%m"), total_weight if count else None, count)
