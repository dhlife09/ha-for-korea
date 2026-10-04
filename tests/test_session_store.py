"""Session persistence and cookie-isolation tests for KEPCO ON."""

from __future__ import annotations

import json
import traceback
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from typing import Any, cast

import pytest
from aiohttp import CookieJar
from custom_components.kepco_on.exceptions import KepcoOnProtocolError
from custom_components.kepco_on.models import KepcoAccountSession, KepcoCookie
from custom_components.kepco_on.session_store import (
    KepcoOnSessionStore,
    export_cookies,
    restore_cookies,
    session_from_payload,
    session_to_payload,
)
from homeassistant.core import HomeAssistant
from yarl import URL

COOKIE_SECRET = "COOKIE_SECRET_CANARY"
TOKEN_SECRET = "TOKEN_SECRET_CANARY"
REFRESH_SECRET = "REFRESH_SECRET_CANARY"
NESTED_SECRET = "NESTED_SECRET_CANARY"


class MemoryStore:
    """Small stand-in for Home Assistant Store used for schema edge cases."""

    saved: dict[str, Any] | None = None
    removed = False
    init_args: tuple[Any, ...] | None = None
    init_kwargs: dict[str, Any] | None = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        type(self).init_args = args
        type(self).init_kwargs = kwargs

    async def async_load(self) -> dict[str, Any] | None:
        return type(self).saved

    async def async_save(self, data: dict[str, Any]) -> None:
        type(self).saved = data

    async def async_remove(self) -> None:
        type(self).removed = True
        type(self).saved = None


@pytest.fixture(autouse=True)
def reset_memory_store() -> None:
    MemoryStore.saved = None
    MemoryStore.removed = False
    MemoryStore.init_args = None
    MemoryStore.init_kwargs = None


def utc_now() -> datetime:
    return datetime(2026, 8, 31, 15, 0, tzinfo=UTC)


def make_session(*, cookies: tuple[KepcoCookie, ...] = ()) -> KepcoAccountSession:
    return KepcoAccountSession(
        refresh_token=REFRESH_SECRET,
        token=TOKEN_SECRET,
        user_id="USER_ID_SECRET",
        member_name="MEMBER_NAME_SECRET",
        user_mng_seqno="USER_SEQ_SECRET",
        cookies=cookies,
        updated_at=utc_now(),
    )


def add_cookie(
    jar: CookieJar,
    *,
    name: str,
    value: str,
    domain: str | None = "online.kepco.co.kr",
    path: str = "/",
    secure: bool = True,
    expires: datetime | None = None,
    max_age: int | None = None,
) -> None:
    cookies = SimpleCookie()
    cookies[name] = value
    if domain is not None:
        cookies[name]["domain"] = domain
    cookies[name]["path"] = path
    if secure:
        cookies[name]["secure"] = True
    if expires is not None:
        cookies[name]["expires"] = expires.strftime("%a, %d %b %Y %H:%M:%S GMT")
    if max_age is not None:
        cookies[name]["max-age"] = str(max_age)
    host = (domain or "online.kepco.co.kr").removeprefix(".")
    jar.update_cookies(cookies, URL(f"https://{host}/"))


def assert_canaries_absent(text: str) -> None:
    for canary in (COOKIE_SECRET, TOKEN_SECRET, REFRESH_SECRET, NESTED_SECRET):
        assert canary not in text


@pytest.mark.asyncio
async def test_store_load_returns_none_when_no_data(monkeypatch: pytest.MonkeyPatch) -> None:
    import custom_components.kepco_on.session_store as session_store

    monkeypatch.setattr(session_store, "Store", MemoryStore)

    hass = cast("HomeAssistant", object())
    loaded = await KepcoOnSessionStore(hass, "entry-1").async_load()

    assert loaded is None
    assert MemoryStore.init_args == (hass, 1, "kepco_on.entry-1")
    assert MemoryStore.init_kwargs == {"atomic_writes": True, "private": True}


@pytest.mark.asyncio
async def test_store_save_load_round_trip_uses_versioned_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import custom_components.kepco_on.session_store as session_store

    monkeypatch.setattr(session_store, "Store", MemoryStore)
    session = make_session(
        cookies=(
            KepcoCookie(
                name="JSESSIONID",
                value=COOKIE_SECRET,
                domain="online.kepco.co.kr",
                path="/",
                secure=True,
                expires=1_798_729_200,
            ),
        )
    )
    store = KepcoOnSessionStore(
        cast("HomeAssistant", object()), "entry-1", allowed_cookie_names={"JSESSIONID"}
    )

    await store.async_save(session)
    assert MemoryStore.saved is not None
    assert MemoryStore.saved["schema"] == 1
    assert MemoryStore.saved["updated_at"] == "2026-08-31T15:00:00+00:00"

    loaded = await store.async_load()

    assert loaded == session
    assert json.loads(json.dumps(MemoryStore.saved)) == MemoryStore.saved


@pytest.mark.asyncio
async def test_store_clear_removes_saved_data(monkeypatch: pytest.MonkeyPatch) -> None:
    import custom_components.kepco_on.session_store as session_store

    monkeypatch.setattr(session_store, "Store", MemoryStore)
    store = KepcoOnSessionStore(cast("HomeAssistant", object()), "entry-1")
    await store.async_save(make_session())

    await store.async_clear()

    assert MemoryStore.removed is True
    assert await store.async_load() is None


@pytest.mark.asyncio
async def test_store_rejects_non_mapping_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    import custom_components.kepco_on.session_store as session_store

    monkeypatch.setattr(session_store, "Store", MemoryStore)
    MemoryStore.saved = cast("Any", ["not-mapping"])
    store = KepcoOnSessionStore(cast("HomeAssistant", object()), "entry-1")

    with pytest.raises(KepcoOnProtocolError):
        await store.async_load()


@pytest.mark.parametrize(
    "payload",
    [
        {"schema": 2, "refresh_token": NESTED_SECRET},
        {"schema": 1, "refresh_token": NESTED_SECRET},
        {
            "schema": 1,
            "refresh_token": REFRESH_SECRET,
            "token": TOKEN_SECRET,
            "user_id": "user",
            "member_name": "member",
            "updated_at": "not-a-date",
        },
        {
            "schema": 1,
            "refresh_token": REFRESH_SECRET,
            "token": TOKEN_SECRET,
            "user_id": "user",
            "member_name": "member",
            "updated_at": "2026-08-31T15:00:00+00:00",
            "cookies": [{"name": "JSESSIONID", "value": NESTED_SECRET, "path": "relative"}],
        },
        {
            "schema": 1,
            "refresh_token": REFRESH_SECRET,
            "token": TOKEN_SECRET,
            "user_id": "user",
            "member_name": "member",
            "updated_at": "2026-08-31T15:00:00+00:00",
            "cookies": [
                {
                    "name": "JSESSIONID",
                    "value": NESTED_SECRET,
                    "domain": "online.kepco.co.kr",
                    "path": "/",
                    "secure": True,
                    "expires": True,
                    "host_only": True,
                }
            ],
        },
        {
            "schema": 1,
            "refresh_token": REFRESH_SECRET,
            "token": TOKEN_SECRET,
            "user_id": "user",
            "member_name": "member",
            "updated_at": "2026-08-31T15:00:00+00:00",
            "cookies": [
                {
                    "name": "JSESSIONID",
                    "value": NESTED_SECRET,
                    "domain": "online.kepco.co.kr",
                    "path": "/",
                    "secure": True,
                    "expires": False,
                    "host_only": True,
                }
            ],
        },
        {
            "schema": 1,
            "refresh_token": REFRESH_SECRET,
            "token": TOKEN_SECRET,
            "user_id": "user",
            "member_name": "member",
            "updated_at": "2026-08-31T15:00:00+00:00",
            "cookies": [
                {
                    "name": "JSESSIONID",
                    "value": NESTED_SECRET,
                    "domain": "online.kepco.co.kr",
                    "path": "/",
                    "secure": True,
                    "expires": -1,
                    "host_only": True,
                }
            ],
        },
    ],
)
def test_malformed_or_unknown_schema_raises_safe_protocol_error(
    payload: dict[str, Any],
) -> None:
    with pytest.raises(KepcoOnProtocolError) as raised:
        session_from_payload(payload)

    assert_canaries_absent(str(raised.value))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("token", 123),
        ("user_mng_seqno", 123),
    ],
)
def test_optional_session_payload_strings_reject_non_string_values(
    field: str,
    value: object,
) -> None:
    payload: dict[str, object] = {
        "schema": 1,
        "refresh_token": REFRESH_SECRET,
        "token": TOKEN_SECRET,
        "user_id": "user",
        "member_name": "member",
        "user_mng_seqno": "seq",
        "updated_at": "2026-08-31T15:00:00+00:00",
    }
    payload[field] = value

    with pytest.raises(KepcoOnProtocolError) as raised:
        session_from_payload(payload)

    assert field in str(raised.value)
    assert_canaries_absent(str(raised.value))


def test_naive_updated_at_is_rejected_without_secret_leak() -> None:
    with pytest.raises(KepcoOnProtocolError) as raised:
        session_from_payload(
            {
                "schema": 1,
                "refresh_token": REFRESH_SECRET,
                "token": TOKEN_SECRET,
                "user_id": "user",
                "member_name": "member",
                "updated_at": "2026-08-31T15:00:00",
            }
        )

    assert str(raised.value) == "Invalid stored KEPCO ON session field: updated_at"
    assert_canaries_absent(str(raised.value))


@pytest.mark.parametrize(
    "cookie_payload",
    [
        "not-object",
        {
            "name": "JSESSIONID",
            "value": NESTED_SECRET,
            "domain": "online.kepco.co.kr",
            "path": "/",
            "secure": "yes",
            "expires": None,
            "host_only": True,
        },
        {
            "name": "JSESSIONID",
            "value": NESTED_SECRET,
            "domain": "online.kepco.co.kr",
            "path": "/",
            "secure": True,
            "expires": None,
            "host_only": "yes",
        },
        {
            "name": "",
            "value": NESTED_SECRET,
            "domain": "online.kepco.co.kr",
            "path": "/",
            "secure": True,
            "expires": None,
            "host_only": True,
        },
    ],
)
def test_cookie_payload_shape_errors_are_safe(cookie_payload: object) -> None:
    with pytest.raises(KepcoOnProtocolError) as raised:
        session_from_payload(
            {
                "schema": 1,
                "refresh_token": REFRESH_SECRET,
                "token": TOKEN_SECRET,
                "user_id": "user",
                "member_name": "member",
                "updated_at": "2026-08-31T15:00:00+00:00",
                "cookies": [cookie_payload],
            }
        )

    assert_canaries_absent(str(raised.value))


def test_cookie_payload_rejects_invalid_domain_shape_after_fields_parse() -> None:
    with pytest.raises(KepcoOnProtocolError) as raised:
        session_from_payload(
            {
                "schema": 1,
                "refresh_token": REFRESH_SECRET,
                "token": TOKEN_SECRET,
                "user_id": "user",
                "member_name": "member",
                "updated_at": "2026-08-31T15:00:00+00:00",
                "cookies": [
                    {
                        "name": "JSESSIONID",
                        "value": NESTED_SECRET,
                        "domain": "evil.example",
                        "path": "/",
                        "secure": True,
                        "expires": None,
                        "host_only": True,
                    }
                ],
            }
        )

    assert str(raised.value) == "Invalid stored KEPCO ON session field: cookie"
    assert_canaries_absent(str(raised.value))


def test_session_repr_and_error_messages_do_not_expose_secret_values() -> None:
    session = make_session(
        cookies=(KepcoCookie(name="JSESSIONID", value=COOKIE_SECRET, domain=".kepco.co.kr"),)
    )

    rendered = repr(session)

    assert_canaries_absent(rendered)
    with pytest.raises(KepcoOnProtocolError) as raised:
        session_from_payload(
            {
                "schema": 1,
                "refresh_token": REFRESH_SECRET,
                "token": TOKEN_SECRET,
                "user_id": "user",
                "member_name": "member",
                "updated_at": "2026-08-31T15:00:00+00:00",
                "cookies": [{"name": NESTED_SECRET, "value": COOKIE_SECRET, "path": "bad"}],
            }
        )
    assert_canaries_absent(str(raised.value))


def test_invalid_updated_at_does_not_retain_secret_in_exception_chain() -> None:
    with pytest.raises(KepcoOnProtocolError) as raised:
        session_from_payload(
            {
                "schema": 1,
                "refresh_token": REFRESH_SECRET,
                "token": TOKEN_SECRET,
                "user_id": "user",
                "member_name": "member",
                "updated_at": TOKEN_SECRET,
            }
        )

    error = raised.value
    rendered = "".join(traceback.format_exception(error))
    assert TOKEN_SECRET not in str(error)
    assert TOKEN_SECRET not in repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None
    assert TOKEN_SECRET not in rendered


def test_session_payload_is_json_safe_and_excludes_username_password() -> None:
    payload = session_to_payload(make_session())

    encoded = json.dumps(payload)

    assert json.loads(encoded) == payload
    assert "username" not in payload
    assert "password" not in payload
    assert payload["cookies"] == []


def test_session_payload_filters_cookies_before_store_receives_them() -> None:
    future = int(datetime(2099, 1, 1, tzinfo=UTC).timestamp())
    session = make_session(
        cookies=(
            KepcoCookie(
                name="JSESSIONID",
                value=COOKIE_SECRET,
                domain="online.kepco.co.kr",
                path="/",
                secure=True,
                expires=future,
                host_only=True,
            ),
            KepcoCookie(name="JSESSIONID", value="BAD_DOMAIN", domain="evil.example", path="/"),
            KepcoCookie(
                name="JSESSIONID",
                value="BAD_PATH",
                domain="online.kepco.co.kr",
                path="bad",
            ),
            KepcoCookie(
                name="JSESSIONID",
                value="EXPIRED",
                domain="online.kepco.co.kr",
                path="/",
                expires=1,
            ),
            KepcoCookie(name="tracking", value="TRACKER", domain="online.kepco.co.kr", path="/"),
        )
    )

    payload = session_to_payload(session, allowed_cookie_names={"JSESSIONID"}, now=utc_now())

    assert payload["cookies"] == [
        {
            "name": "JSESSIONID",
            "value": COOKIE_SECRET,
            "domain": "online.kepco.co.kr",
            "path": "/",
            "secure": True,
            "expires": future,
            "host_only": True,
        }
    ]


@pytest.mark.asyncio
async def test_store_save_persists_allowed_cookies_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import custom_components.kepco_on.session_store as session_store

    monkeypatch.setattr(session_store, "Store", MemoryStore)
    store = KepcoOnSessionStore(cast("HomeAssistant", object()), "entry-1")
    await store.async_save(
        make_session(
            cookies=(
                KepcoCookie(name="JSESSIONID", value=COOKIE_SECRET, domain="online.kepco.co.kr"),
            )
        )
    )

    assert MemoryStore.saved is not None
    cookies = cast("list[dict[str, Any]]", MemoryStore.saved["cookies"])
    assert cookies == [
        {
            "name": "JSESSIONID",
            "value": COOKIE_SECRET,
            "domain": "online.kepco.co.kr",
            "path": "/",
            "secure": True,
            "expires": None,
            "host_only": True,
        }
    ]


@pytest.mark.asyncio
async def test_export_cookies_keeps_only_allowed_nonexpired_kepco_rooted_cookies() -> None:
    now = utc_now()
    future_expiry = datetime(2099, 1, 1, tzinfo=UTC)
    jar = CookieJar()
    add_cookie(
        jar,
        name="JSESSIONID",
        value=COOKIE_SECRET,
        domain="online.kepco.co.kr",
        secure=True,
        expires=future_expiry,
    )
    add_cookie(jar, name="not_allowed", value="TRACKER", domain="online.kepco.co.kr")
    add_cookie(jar, name="JSESSIONID", value="WRONG_DOMAIN", domain="evil.example")
    add_cookie(
        jar,
        name="kepcoSSO",
        value="EXPIRED_SECRET",
        domain=".kepco.co.kr",
        expires=now - timedelta(seconds=1),
    )

    cookies = export_cookies(jar, {"JSESSIONID", "kepcoSSO"}, now=now)

    assert cookies == (
        KepcoCookie(
            name="JSESSIONID",
            value=COOKIE_SECRET,
            domain="online.kepco.co.kr",
            path="/",
            secure=True,
            expires=int(future_expiry.timestamp()),
            host_only=False,
        ),
    )


@pytest.mark.asyncio
async def test_export_cookies_rejects_non_rooted_paths() -> None:
    jar = CookieJar()
    add_cookie(jar, name="JSESSIONID", value=COOKIE_SECRET, path="/")
    morsel = next(iter(jar))
    morsel["path"] = "relative"

    assert export_cookies(jar, {"JSESSIONID"}, now=utc_now()) == ()


@pytest.mark.asyncio
async def test_restore_cookies_keeps_only_allowed_nonexpired_valid_cookie_shapes() -> None:
    now = utc_now()
    jar = CookieJar()
    cookies = (
        KepcoCookie(
            name="JSESSIONID",
            value=COOKIE_SECRET,
            domain="online.kepco.co.kr",
            path="/",
            secure=True,
            expires=1_798_728_000,
            host_only=True,
        ),
        KepcoCookie(
            name="kepcoSSO",
            value="EXPIRED_SECRET",
            domain=".kepco.co.kr",
            path="/",
            secure=False,
            expires=int((now - timedelta(seconds=1)).timestamp()),
        ),
        KepcoCookie(name="tracking", value="TRACKER", domain="online.kepco.co.kr", path="/"),
        KepcoCookie(name="JSESSIONID", value="BAD_DOMAIN", domain="evil.example", path="/"),
        KepcoCookie(name="JSESSIONID", value="BAD_PATH", domain="online.kepco.co.kr", path="bad"),
    )

    restore_cookies(jar, cookies, {"JSESSIONID", "kepcoSSO"}, now=now)

    restored = jar.filter_cookies(URL("https://online.kepco.co.kr/"))
    assert set(restored) == {"JSESSIONID"}
    assert restored["JSESSIONID"].value == COOKIE_SECRET


@pytest.mark.asyncio
async def test_current_empty_allowlist_persists_zero_cookies_by_default() -> None:
    jar = CookieJar()
    add_cookie(jar, name="JSESSIONID", value=COOKIE_SECRET)

    assert export_cookies(jar, set(), now=utc_now()) == ()


def test_cookie_repr_hides_value_and_preserves_secure_expires_fields() -> None:
    cookie = KepcoCookie(
        name="JSESSIONID",
        value=COOKIE_SECRET,
        domain=".kepco.co.kr",
        path="/kepco",
        secure=True,
        expires=1_798_728_000,
        host_only=True,
    )

    assert COOKIE_SECRET not in repr(cookie)
    assert cookie.secure is True
    assert cookie.expires == 1_798_728_000


@pytest.mark.asyncio
async def test_host_only_restore_is_not_sent_to_subdomain() -> None:
    jar = CookieJar()

    restore_cookies(
        jar,
        (
            KepcoCookie(
                name="JSESSIONID",
                value=COOKIE_SECRET,
                domain="online.kepco.co.kr",
                path="/",
                secure=True,
                host_only=True,
            ),
            KepcoCookie(
                name="kepcoSSO",
                value="DOMAIN_SECRET",
                domain=".kepco.co.kr",
                path="/",
                secure=True,
                host_only=False,
            ),
        ),
        {"JSESSIONID", "kepcoSSO"},
        now=utc_now(),
    )

    exact_host = jar.filter_cookies(URL("https://online.kepco.co.kr/"))
    subdomain = jar.filter_cookies(URL("https://sub.online.kepco.co.kr/"))

    assert set(exact_host) == {"JSESSIONID", "kepcoSSO"}
    assert set(subdomain) == {"kepcoSSO"}


@pytest.mark.asyncio
async def test_export_preserves_host_only_state_from_cookie_jar() -> None:
    jar = CookieJar()
    add_cookie(jar, name="JSESSIONID", value=COOKIE_SECRET, domain=None)

    cookies = export_cookies(jar, {"JSESSIONID"}, now=utc_now())

    assert cookies == (
        KepcoCookie(
            name="JSESSIONID",
            value=COOKIE_SECRET,
            domain="online.kepco.co.kr",
            path="/",
            secure=True,
            host_only=True,
        ),
    )


@pytest.mark.asyncio
async def test_export_derives_max_age_deadline_and_drops_after_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = utc_now()
    monkeypatch.setattr("aiohttp.cookiejar.time.time", lambda: now.timestamp())
    jar = CookieJar()
    add_cookie(
        jar,
        name="JSESSIONID",
        value=COOKIE_SECRET,
        domain="online.kepco.co.kr",
        max_age=60,
    )

    exported = export_cookies(jar, {"JSESSIONID"}, now=now)
    dropped = export_cookies(jar, {"JSESSIONID"}, now=now + timedelta(seconds=61))

    assert exported[0].expires == int((now + timedelta(seconds=60)).timestamp())
    assert dropped == ()


@pytest.mark.asyncio
async def test_export_handles_cookiejar_private_shape_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jar = CookieJar()
    add_cookie(jar, name="JSESSIONID", value=COOKIE_SECRET, domain=None)
    monkeypatch.setattr(jar, "_host_only_cookies", object())
    monkeypatch.setattr(jar, "_expirations", object())

    cookies = export_cookies(jar, {"JSESSIONID"}, now=utc_now())

    assert cookies == (
        KepcoCookie(
            name="JSESSIONID",
            value=COOKIE_SECRET,
            domain="online.kepco.co.kr",
            path="/",
            secure=True,
            host_only=False,
        ),
    )


@pytest.mark.asyncio
async def test_export_ignores_malformed_private_expiration_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jar = CookieJar()
    add_cookie(jar, name="JSESSIONID", value=COOKIE_SECRET, domain="online.kepco.co.kr")
    monkeypatch.setattr(
        jar,
        "_expirations",
        {
            ("online.kepco.co.kr", "/", "JSESSIONID"): True,
            ("online.kepco.co.kr", "/", "other"): -1,
            ("online.kepco.co.kr", "/"): 1_798_728_000,
        },
    )

    cookies = export_cookies(jar, {"JSESSIONID"}, now=utc_now())

    assert cookies[0].expires is None


@pytest.mark.asyncio
async def test_restore_writes_secure_attribute_when_cookie_requires_it() -> None:
    jar = CookieJar()

    restore_cookies(
        jar,
        (
            KepcoCookie(
                name="JSESSIONID",
                value=COOKIE_SECRET,
                domain=".kepco.co.kr",
                path="/",
                secure=True,
                host_only=False,
            ),
        ),
        {"JSESSIONID"},
        now=utc_now(),
    )

    restored = jar.filter_cookies(URL("https://online.kepco.co.kr/"))
    assert restored["JSESSIONID"].value == COOKIE_SECRET
