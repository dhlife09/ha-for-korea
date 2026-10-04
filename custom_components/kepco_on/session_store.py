"""Versioned storage for KEPCO ON authenticated session state."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from http.cookies import Morsel, SimpleCookie
from typing import NotRequired, TypedDict, cast

from aiohttp import CookieJar
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from yarl import URL

from .const import PERSISTED_COOKIE_ALLOWLIST
from .exceptions import KepcoOnProtocolError
from .models import KepcoAccountSession, KepcoCookie

STORE_VERSION = 1
PAYLOAD_SCHEMA_VERSION = 1
COOKIE_DOMAINS = frozenset({"online.kepco.co.kr", ".kepco.co.kr"})


class CookiePayload(TypedDict):
    """JSON payload for one persisted cookie."""

    name: str
    value: str
    domain: str
    path: str
    secure: bool
    expires: int | None
    host_only: bool


class SessionPayload(TypedDict):
    """JSON payload for a KEPCO ON account session."""

    schema: int
    refresh_token: str
    user_id: str
    member_name: str
    updated_at: str
    token: str | None
    user_mng_seqno: str | None
    cookies: NotRequired[list[CookiePayload]]


def _protocol_error(category: str) -> KepcoOnProtocolError:
    return KepcoOnProtocolError(f"Invalid stored KEPCO ON session field: {category}")


def _require_str(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise _protocol_error(field)
    return value


def _optional_str(payload: Mapping[str, object], field: str) -> str | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _protocol_error(field)
    return value


def _parse_updated_at(value: object) -> datetime:
    if not isinstance(value, str):
        raise _protocol_error("updated_at")
    parsed: datetime | None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        parsed = None
    if parsed is None:
        raise _protocol_error("updated_at")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _protocol_error("updated_at")
    return parsed.astimezone(UTC)


def _validate_cookie_shape(cookie: KepcoCookie, now: datetime | None) -> bool:
    if cookie.name == "":
        return False
    if cookie.domain not in COOKIE_DOMAINS:
        return False
    if not cookie.path.startswith("/"):
        return False
    return not (
        now is not None and cookie.expires is not None and cookie.expires <= int(now.timestamp())
    )


def _is_valid_expires(value: object) -> bool:
    return value is None or (isinstance(value, int) and not isinstance(value, bool) and value >= 0)


def _filter_cookies(
    cookies: Iterable[KepcoCookie],
    allowed_names: Iterable[str],
    now: datetime | None = None,
) -> tuple[KepcoCookie, ...]:
    allowed = frozenset(allowed_names)
    current = (now or datetime.now(UTC)).astimezone(UTC)
    return tuple(
        cookie
        for cookie in cookies
        if cookie.name in allowed and _validate_cookie_shape(cookie, current)
    )


def _cookie_from_payload(value: object) -> KepcoCookie:
    if not isinstance(value, Mapping):
        raise _protocol_error("cookie")
    name = _require_str(value, "name")
    cookie_value = _require_str(value, "value")
    domain = _require_str(value, "domain")
    path = _require_str(value, "path")
    secure = value.get("secure")
    if not isinstance(secure, bool):
        raise _protocol_error("cookie secure")
    expires = value.get("expires")
    if not _is_valid_expires(expires):
        raise _protocol_error("cookie expires")
    host_only = value.get("host_only")
    if not isinstance(host_only, bool):
        raise _protocol_error("cookie host_only")
    cookie = KepcoCookie(
        name=name,
        value=cookie_value,
        domain=domain,
        path=path,
        secure=secure,
        expires=expires,
        host_only=host_only,
    )
    if not _validate_cookie_shape(cookie, None):
        raise _protocol_error("cookie")
    return cookie


def session_to_payload(
    session: KepcoAccountSession,
    allowed_cookie_names: Iterable[str] = PERSISTED_COOKIE_ALLOWLIST,
    now: datetime | None = None,
) -> SessionPayload:
    """Convert a session model into JSON-safe storage data."""
    cookies = _filter_cookies(session.cookies, allowed_cookie_names, now)
    return {
        "schema": PAYLOAD_SCHEMA_VERSION,
        "refresh_token": session.refresh_token,
        "token": session.token,
        "user_id": session.user_id,
        "member_name": session.member_name,
        "user_mng_seqno": session.user_mng_seqno,
        "updated_at": session.updated_at.astimezone(UTC).isoformat(),
        "cookies": [
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path,
                "secure": cookie.secure,
                "expires": cookie.expires,
                "host_only": cookie.host_only,
            }
            for cookie in cookies
        ],
    }


def session_from_payload(payload: Mapping[str, object]) -> KepcoAccountSession:
    """Convert JSON storage data into a session model."""
    if payload.get("schema") != PAYLOAD_SCHEMA_VERSION:
        raise _protocol_error("schema")
    cookies_payload = payload.get("cookies", [])
    if not isinstance(cookies_payload, list):
        raise _protocol_error("cookies")
    return KepcoAccountSession(
        refresh_token=_require_str(payload, "refresh_token"),
        token=_optional_str(payload, "token"),
        user_id=_require_str(payload, "user_id"),
        member_name=_require_str(payload, "member_name"),
        user_mng_seqno=_optional_str(payload, "user_mng_seqno"),
        cookies=tuple(_cookie_from_payload(cookie) for cookie in cookies_payload),
        updated_at=_parse_updated_at(payload.get("updated_at")),
    )


class KepcoOnSessionStore:
    """Home Assistant storage wrapper for KEPCO ON session persistence."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        allowed_cookie_names: Iterable[str] = PERSISTED_COOKIE_ALLOWLIST,
    ) -> None:
        self._store: Store[SessionPayload] = Store(
            hass,
            STORE_VERSION,
            f"kepco_on.{entry_id}",
            private=True,
            atomic_writes=True,
        )
        self._allowed_cookie_names = frozenset(allowed_cookie_names)

    async def async_load(self) -> KepcoAccountSession | None:
        """Load a persisted session."""
        payload = await self._store.async_load()
        if payload is None:
            return None
        if not isinstance(payload, Mapping):
            raise _protocol_error("payload")
        return session_from_payload(payload)

    async def async_save(self, session: KepcoAccountSession) -> None:
        """Atomically persist a session."""
        await self._store.async_save(
            session_to_payload(session, allowed_cookie_names=self._allowed_cookie_names)
        )

    async def async_clear(self) -> None:
        """Remove any persisted session."""
        await self._store.async_remove()


def _cookie_expires(morsel: Morsel[str]) -> int | None:
    expires = morsel["expires"]
    if not expires:
        return None
    try:
        parsed = parsedate_to_datetime(expires)
    except (TypeError, ValueError) as err:
        del err
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.astimezone(UTC).timestamp())


def _host_only_cookies(jar: CookieJar) -> frozenset[tuple[str, str]]:
    """Return aiohttp host-only markers, or none if the private shape changes."""
    value = getattr(jar, "_host_only_cookies", None)
    if not isinstance(value, set):
        return frozenset()
    pairs: set[tuple[str, str]] = set()
    for item in value:
        if (
            isinstance(item, tuple)
            and len(item) == 2
            and isinstance(item[0], str)
            and isinstance(item[1], str)
        ):
            pairs.add((item[0], item[1]))
    return frozenset(pairs)


def _stored_expirations(jar: CookieJar) -> Mapping[tuple[str, str, str], float]:
    """Return aiohttp absolute expiry metadata, or none if the private shape changes."""
    value = getattr(jar, "_expirations", None)
    if not isinstance(value, dict):
        return {}
    expirations: dict[tuple[str, str, str], float] = {}
    for key, expires in value.items():
        if (
            isinstance(key, tuple)
            and len(key) == 3
            and all(isinstance(part, str) for part in key)
            and isinstance(expires, int | float)
            and not isinstance(expires, bool)
            and expires >= 0
        ):
            expirations[cast("tuple[str, str, str]", key)] = float(expires)
    return expirations


def _stored_domain(morsel: Morsel[str], host_only: bool) -> str:
    domain = cast("str", morsel["domain"])
    if not host_only and domain == "kepco.co.kr":
        return ".kepco.co.kr"
    return domain


def _stored_expiry(
    expirations: Mapping[tuple[str, str, str], float],
    domain: str,
    path: str,
    name: str,
) -> int | None:
    normalized_domain = domain.removeprefix(".")
    normalized_path = path.rstrip("/")
    for key in (
        (normalized_domain, path, name),
        (normalized_domain, normalized_path, name),
        (domain, path, name),
        (domain, normalized_path, name),
    ):
        if (expires := expirations.get(key)) is not None:
            return int(expires)
    return None


def export_cookies(
    jar: CookieJar,
    allowed_names: Iterable[str],
    now: datetime | None = None,
) -> tuple[KepcoCookie, ...]:
    """Export only explicitly allowed, valid, unexpired KEPCO cookies."""
    allowed = frozenset(allowed_names)
    current = (now or datetime.now(UTC)).astimezone(UTC)
    host_only_cookies = _host_only_cookies(jar)
    expirations = _stored_expirations(jar)
    cookies: list[KepcoCookie] = []
    for morsel in jar:
        path = morsel["path"] or "/"
        host_only = (cast("str", morsel["domain"]), morsel.key) in host_only_cookies
        domain = _stored_domain(morsel, host_only)
        expires = _cookie_expires(morsel)
        if expires is None:
            expires = _stored_expiry(expirations, domain, path, morsel.key)
        cookie = KepcoCookie(
            name=morsel.key,
            value=morsel.value,
            domain=domain,
            path=path,
            secure=bool(morsel["secure"]),
            expires=expires,
            host_only=host_only,
        )
        if cookie.name not in allowed:
            continue
        if not _validate_cookie_shape(cookie, current):
            continue
        cookies.append(cookie)
    return tuple(cookies)


def restore_cookies(
    jar: CookieJar,
    cookies: Iterable[KepcoCookie],
    allowed_names: Iterable[str],
    now: datetime | None = None,
) -> None:
    """Restore only explicitly allowed, valid, unexpired KEPCO cookies into a jar."""
    allowed = frozenset(allowed_names)
    current = (now or datetime.now(UTC)).astimezone(UTC)
    for cookie in cookies:
        if cookie.name not in allowed:
            continue
        if not _validate_cookie_shape(cookie, current):
            continue
        simple = SimpleCookie()
        simple[cookie.name] = cookie.value
        if not cookie.host_only:
            simple[cookie.name]["domain"] = cookie.domain
        simple[cookie.name]["path"] = cookie.path
        if cookie.secure:
            simple[cookie.name]["secure"] = True
        if cookie.expires is not None:
            simple[cookie.name]["expires"] = datetime.fromtimestamp(cookie.expires, UTC).strftime(
                "%a, %d %b %Y %H:%M:%S GMT"
            )
        jar.update_cookies(simple, URL(f"https://{cookie.domain.removeprefix('.')}/"))


__all__ = [
    "KepcoOnSessionStore",
    "export_cookies",
    "restore_cookies",
    "session_from_payload",
    "session_to_payload",
]
