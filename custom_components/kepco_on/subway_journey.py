"""User-controlled subway journeys and iOS Live Activity notifications."""

from __future__ import annotations

import asyncio
import math
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .subway import async_query
from .subway_api import CONF_LINE, CONF_SERVICE, CONF_STATION, LINES, SERVICE, SubwayError
from .subway_network import SubwayNetwork


@dataclass(slots=True)
class Journey:
    """One user's explicit route selection, retained only while running."""

    route: dict[str, Any]
    notify: str
    url: str
    tag: str = field(default_factory=lambda: "ha_korea_subway_" + secrets.token_hex(12))
    leg_index: int = 0
    phase: str = "waiting"
    arrival: str = "도착정보 확인 중"
    message: str = ""
    boarded_at: datetime | None = None
    expires_at: datetime = field(default_factory=lambda: dt_util.utcnow() + timedelta(hours=2))
    last_message: str = ""


class JourneyManager:
    """Poll only active journeys, using the existing consent and shared API quota."""

    def __init__(self, hass: HomeAssistant, network: SubwayNetwork) -> None:
        self.hass = hass
        self.network = network
        self.journeys: dict[str, Journey] = {}
        self.lock = asyncio.Lock()
        self.cancel_timer: Callable[[], None] | None = None

    def status(self, user_id: str) -> dict[str, Any]:
        journey = self.journeys.get(user_id)
        if journey is None:
            return {"active": False}
        return {
            "active": True,
            "phase": journey.phase,
            "leg_index": journey.leg_index,
            "legs": journey.route["legs"],
            "arrival": journey.arrival,
            "message": journey.message,
            "expires_at": journey.expires_at.isoformat(),
        }

    async def command(self, user_id: str, message: dict[str, Any]) -> dict[str, Any]:
        """Serialize start/board/next/stop; status never sends a notification."""
        async with self.lock:
            command = message["command"]
            if command == "status":
                return self.status(user_id)
            if command == "stop":
                await self.stop(user_id)
                return self.status(user_id)
            if command == "start":
                route = self.network.route(
                    message["origin"],
                    message["destination"],
                    message.get("via", []),
                    message.get("preference", "time"),
                )
                if not route["legs"]:
                    raise ValueError("출발역과 도착역을 다르게 선택해 주세요.")
                notify = message.get("notify_service", "")
                if (
                    not isinstance(notify, str)
                    or re.fullmatch(r"notify\.mobile_app_[a-z0-9_]+", notify) is None
                    or not self.hass.services.has_service("notify", notify[7:])
                ):
                    raise ValueError("카드에 아이폰 mobile_app 알림 서비스를 설정해 주세요.")
                url = message.get("url", "/lovelace/subway")
                if (
                    not isinstance(url, str)
                    or not url.startswith("/")
                    or url.startswith("//")
                    or any(character in url for character in ("\\", "\r", "\n"))
                ):
                    raise ValueError("알림 이동 주소는 HA 내부 경로여야 합니다.")
                await self.stop(user_id)
                journey = Journey(route, notify[7:], url)
                self.journeys[user_id] = journey
                try:
                    await self.refresh(journey, initial=True)
                except HomeAssistantError:
                    self.journeys.pop(user_id, None)
                    raise ValueError("아이폰 알림을 보내지 못했습니다.") from None
                if self.cancel_timer is None:
                    self.cancel_timer = async_track_time_interval(
                        self.hass, self.tick, timedelta(seconds=120)
                    )
            else:
                existing_journey = self.journeys.get(user_id)
                if existing_journey is None:
                    raise ValueError("먼저 경로 안내를 시작해 주세요.")
                journey = existing_journey
                if command == "board":
                    journey.phase = "riding"
                    journey.boarded_at = dt_util.utcnow()
                elif command == "next":
                    if journey.leg_index + 1 == len(journey.route["legs"]):
                        await self.stop(user_id)
                        return self.status(user_id)
                    journey.leg_index += 1
                    journey.phase = "waiting"
                    journey.boarded_at = None
                else:
                    raise ValueError("지원하지 않는 안내 동작입니다.")
                await self.refresh(journey)
            return self.status(user_id)

    async def stop(self, user_id: str) -> None:
        """Clear both this journey's Live Activity and its stop notification."""
        journey = self.journeys.pop(user_id, None)
        if journey is None:
            return
        if not self.journeys and self.cancel_timer is not None:
            self.cancel_timer()
            self.cancel_timer = None
        for tag in (journey.tag, journey.tag + "_control"):
            await self.hass.services.async_call(
                "notify",
                journey.notify,
                {"message": "clear_notification", "data": {"tag": tag}},
                blocking=True,
            )

    async def arrivals(self, journey: Journey) -> str:
        """Reject stale/departed trains and never display the opposite direction."""
        leg = journey.route["legs"][journey.leg_index]
        if leg["line"] not in LINES or not leg["direction"]:
            return "이 구간의 실시간 방향 정보는 지원하지 않습니다"
        entries = [
            entry
            for entry in self.hass.config_entries.async_entries(DOMAIN)
            if entry.data.get(CONF_SERVICE) == SERVICE and entry.state == ConfigEntryState.LOADED
        ]
        if not entries:
            return "서울 지하철 도착정보 통합을 먼저 설정해 주세요"
        data = dict(entries[0].data)
        data.update({CONF_STATION: leg["origin"], CONF_LINE: leg["line"]})
        try:
            arrivals = await async_query(self.hass, data)
        except SubwayError:
            return "도착정보를 가져오지 못했습니다"
        now = dt_util.utcnow()
        for arrival in arrivals:
            if arrival.direction != leg["direction"] or arrival.code == "2":
                continue
            if arrival.next_station and arrival.next_station != leg["next_station"]:
                continue
            if arrival.generated_at is None:
                continue
            generated = arrival.generated_at
            if generated.tzinfo is None:
                generated = generated.replace(tzinfo=dt_util.get_time_zone("Asia/Seoul"))
            age = (now - generated).total_seconds()
            if age < -60 or age > 300:
                continue
            eta = ""
            if arrival.seconds is not None:
                remaining = max(0, math.ceil((arrival.seconds - age) / 60))
                eta = f"약 {remaining}분 · "
            return f"{arrival.destination}행 · {eta}{arrival.message}"
        return "해당 방향 도착정보가 없습니다"

    async def refresh(self, journey: Journey, *, initial: bool = False) -> None:
        """Keep the route title fixed; update station, direction and remaining time."""
        leg = journey.route["legs"][journey.leg_index]
        line = LINES.get(leg["line"], leg["line"])
        if journey.phase == "waiting":
            journey.arrival = await self.arrivals(journey)
            detail = journey.arrival
            current = f"{leg['origin']} · {leg['next_station']} 방면"
        else:
            assert journey.boarded_at is not None
            elapsed = (dt_util.utcnow() - journey.boarded_at).total_seconds()
            minutes = max(0, math.ceil((leg["seconds"] - elapsed) / 60))
            detail = f"{leg['destination']}까지 약 {minutes}분 (추정)"
            current = f"{leg['origin']} → {leg['destination']}"
            journey.arrival = detail
        journey.message = f"{line} {current} · {detail}"
        if not initial and journey.message == journey.last_message:
            return
        await self.hass.services.async_call(
            "notify",
            journey.notify,
            {
                "title": f"{journey.route['origin']} → {journey.route['destination']}",
                "message": journey.message,
                "data": {
                    "tag": journey.tag,
                    "live_update": True,
                    "critical_text": f"{current} · {detail}"[:80],
                    "notification_icon": "mdi:subway-variant",
                    "url": journey.url,
                    "alert_once": True,
                    **({} if initial else {"silent": True}),
                },
            },
            blocking=True,
        )
        journey.last_message = journey.message
        if initial:
            await self.hass.services.async_call(
                "notify",
                journey.notify,
                {
                    "title": "지하철 안내 제어",
                    "message": "눌러서 경로 화면 열기 · 안내 중지",
                    "data": {
                        "tag": journey.tag + "_control",
                        "url": journey.url,
                        "actions": [
                            {
                                "action": journey.tag + "_stop",
                                "title": "안내 중지",
                                "activationMode": "background",
                            }
                        ],
                    },
                },
                blocking=True,
            )

    async def tick(self, now: datetime) -> None:
        """A bounded two-hour session avoids forgotten background requests."""
        async with self.lock:
            for user_id, journey in list(self.journeys.items()):
                try:
                    if now >= journey.expires_at:
                        await self.stop(user_id)
                    else:
                        await self.refresh(journey)
                except HomeAssistantError:
                    # Keep the timer alive; never log exception URLs or credentials.
                    continue

    async def notification_action(self, event: Event) -> None:
        """Match an unguessable session action rather than a global stop button."""
        action = event.data.get("action")
        async with self.lock:
            for user_id, journey in list(self.journeys.items()):
                if action == journey.tag + "_stop":
                    await self.stop(user_id)

    def setup(self) -> None:
        """One shared timer; idle sessions do not perform network calls."""
        self.hass.bus.async_listen("mobile_app_notification_action", self.notification_action)
