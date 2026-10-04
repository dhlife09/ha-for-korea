"""Config-entry lifecycle and coordinator tests for KEPCO ON."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, cast
from unittest.mock import Mock

import pytest
from aiohttp import CookieJar
from custom_components.kepco_on.const import (
    CONF_ACCOUNT_UID_HASH,
    CONF_CUSTOMERS,
    CONF_DISPLAY_NAME,
    CONF_SAVE_PASSWORD,
    CONF_SELECTED_CUSTOMERS,
    CONF_SESSION_HANDOFF,
    CONF_USERNAME,
    CONFIG_ENTRY_VERSION,
    DEFAULT_POLLING_INTERVAL_HOURS,
    OPT_CO2_FACTOR_KG_PER_KWH,
    OPT_ENABLE_CO2_ESTIMATE,
    OPT_ENABLE_DETAILED_SENSORS,
    OPT_HISTORY_MONTHS,
    OPT_POLLING_INTERVAL_HOURS,
    PLATFORMS,
)
from custom_components.kepco_on.exceptions import (
    KepcoOnConnectionError,
    KepcoOnNoCustomersError,
    KepcoOnProtocolError,
    KepcoOnRateLimitError,
    KepcoOnSessionExpired,
    KepcoOnUnsupportedAccount,
)
from custom_components.kepco_on.models import (
    KepcoAccountSession,
    KepcoBill,
    KepcoCustomer,
    serialize_customer,
)
from custom_components.kepco_on.session_store import session_to_payload
from homeassistant.config_entries import ConfigEntryState, OptionsFlowManager
from homeassistant.const import CONF_PASSWORD
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util

PASSWORD_SECRET = "PASSWORD_SECRET_CANARY"
TOKEN_SECRET = "TOKEN_SECRET_CANARY"
RAW_CUSTOMER_SECRET = "CUST_NO_SECRET_CANARY"
RAW_HOUSE_SECRET = "HOUSE_NO_SECRET_CANARY"


class FakeSession:
    """Client session returned by the patched HA session factory."""

    def __init__(self) -> None:
        self.cookie_jar = CookieJar()
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeStore:
    """Session store with controllable load/save behavior."""

    instances: ClassVar[list[FakeStore]] = []
    load_results: ClassVar[list[KepcoAccountSession | Exception | None]] = []
    save_errors: ClassVar[list[Exception]] = []

    def __init__(self, hass: FakeHass, entry_id: str) -> None:
        del hass
        self.entry_id = entry_id
        self.saved: list[KepcoAccountSession] = []
        self.cleared = False
        FakeStore.instances.append(self)

    async def async_load(self) -> KepcoAccountSession | None:
        result = FakeStore.load_results.pop(0) if FakeStore.load_results else None
        if isinstance(result, Exception):
            raise result
        if result is not None:
            return result
        return self.saved[-1] if self.saved else None

    async def async_save(self, session: KepcoAccountSession) -> None:
        if FakeStore.save_errors:
            raise FakeStore.save_errors.pop(0)
        self.saved.append(session)

    async def async_clear(self) -> None:
        self.cleared = True


class FakeAuth:
    """Auth fake used by setup tests."""

    instances: ClassVar[list[FakeAuth]] = []
    restore_results: ClassVar[list[bool | Exception]] = []
    validate_results: ClassVar[list[bool | Exception]] = [True]
    login_results: ClassVar[list[KepcoAccountSession | Exception]] = []

    def __init__(
        self,
        session: FakeSession,
        *,
        store: FakeStore,
        reauth_username: str | None = None,
        reauth_password: str | None = None,
    ) -> None:
        self.session = session
        self.store = store
        self.reauth_username = reauth_username
        self.reauth_password = reauth_password
        self.login_calls: list[tuple[str, str]] = []
        FakeAuth.instances.append(self)

    async def async_restore_session(self) -> bool:
        result = FakeAuth.restore_results.pop(0) if FakeAuth.restore_results else None
        if isinstance(result, Exception):
            raise result
        if result is not None:
            return result
        return await self.store.async_load() is not None

    async def async_validate_session(self) -> bool:
        result = FakeAuth.validate_results.pop(0) if FakeAuth.validate_results else True
        if isinstance(result, Exception):
            raise result
        return result

    async def async_reset_session(self) -> None:
        await self.store.async_clear()

    async def async_reauthenticate(self) -> None:
        if self.reauth_username is None or self.reauth_password is None:
            raise RuntimeError("reauthentication credentials are missing")
        await self.async_login(self.reauth_username, self.reauth_password)

    async def async_login(self, username: str, password: str) -> KepcoAccountSession:
        self.login_calls.append((username, password))
        result = FakeAuth.login_results.pop(0)
        if isinstance(result, Exception):
            raise result
        await self.store.async_save(result)
        return result


class FakeClient:
    """Client fake used by setup and coordinator tests."""

    instances: ClassVar[list[FakeClient]] = []
    account_results: ClassVar[list[str | Exception]] = ["INDI"]
    customer_results: ClassVar[list[tuple[KepcoCustomer, ...] | Exception]] = []
    bill_results: ClassVar[list[KepcoBill | Exception]] = []

    def __init__(self, auth: FakeAuth, *, clock: Callable[[], datetime] | None = None) -> None:
        self.auth = auth
        self.clock = clock
        self.bill_calls: list[KepcoCustomer] = []
        FakeClient.instances.append(self)

    async def async_get_account_type(self) -> str:
        result = FakeClient.account_results.pop(0) if FakeClient.account_results else "INDI"
        if isinstance(result, Exception):
            raise result
        return result

    async def async_get_customers(self) -> tuple[KepcoCustomer, ...]:
        result = FakeClient.customer_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def async_get_bill(self, customer: KepcoCustomer) -> KepcoBill:
        self.bill_calls.append(customer)
        result = FakeClient.bill_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeCoordinator:
    """Setup-seam coordinator that records first refresh."""

    instances: ClassVar[list[FakeCoordinator]] = []
    refresh_results: ClassVar[list[Exception | None]] = [None]

    def __init__(
        self,
        hass: FakeHass,
        entry: FakeConfigEntry,
        client: FakeClient,
        customers: tuple[KepcoCustomer, ...],
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.client = client
        self.customers = customers
        self.refresh_calls = 0
        self.update_interval = timedelta(
            hours=int(entry.options.get(OPT_POLLING_INTERVAL_HOURS, DEFAULT_POLLING_INTERVAL_HOURS))
        )
        FakeCoordinator.instances.append(self)

    async def async_config_entry_first_refresh(self) -> None:
        self.refresh_calls += 1
        result = FakeCoordinator.refresh_results.pop(0)
        if result is not None:
            raise result


class FakeConfigEntries:
    """Minimal config entries facade for setup lifecycle tests."""

    def __init__(self) -> None:
        self.forwarded: list[tuple[FakeConfigEntry, tuple[Any, ...]]] = []
        self.unloaded: list[tuple[FakeConfigEntry, tuple[Any, ...]]] = []
        self.reloads: list[str] = []
        self.updates: list[dict[str, Any]] = []
        self.version_updates: list[int] = []
        self.entries_by_id: dict[str, FakeConfigEntry] = {}
        self.forward_result = True
        self.unload_result = True

    def async_get_known_entry(self, entry_id: str) -> FakeConfigEntry:
        return self.entries_by_id[entry_id]

    def async_update_entry(
        self,
        entry: FakeConfigEntry,
        *,
        data: Mapping[str, Any] | None = None,
        options: Mapping[str, Any] | None = None,
        title: str | None = None,
        version: int | None = None,
        **_: Any,
    ) -> bool:
        if data is not None:
            entry.data = dict(data)
            self.updates.append(dict(data))
        if options is not None:
            entry.options = dict(options)
        if title is not None:
            entry.title = title
        if version is not None:
            entry.version = version
            self.version_updates.append(version)
        return True

    def async_schedule_reload(self, entry_id: str) -> None:
        self.reloads.append(entry_id)

    async def async_forward_entry_setups(
        self,
        entry: FakeConfigEntry,
        platforms: tuple[Any, ...],
    ) -> bool:
        assert entry.runtime_data is not None
        self.forwarded.append((entry, platforms))
        if not self.forward_result:
            raise ConfigEntryError("forward failed")
        return self.forward_result

    async def async_unload_platforms(
        self,
        entry: FakeConfigEntry,
        platforms: tuple[Any, ...],
    ) -> bool:
        self.unloaded.append((entry, platforms))
        return self.unload_result

    async def async_reload(self, entry_id: str) -> None:
        self.reloads.append(entry_id)


class FakeHass:
    """Small Home Assistant surface for lifecycle tests."""

    def __init__(self) -> None:
        self.config_entries = FakeConfigEntries()
        self.loop = asyncio.get_running_loop()

    def async_create_task(self, target: Awaitable[Any]) -> asyncio.Task[Any]:
        return asyncio.create_task(cast("Any", target))


class FakeConfigEntry:
    """Mutable typed-entry stand-in with runtime data and unload callbacks."""

    def __init__(
        self,
        *,
        data: Mapping[str, Any],
        options: Mapping[str, Any] | None = None,
        entry_id: str = "entry-1",
        title: str = "한전ON",
        version: int = CONFIG_ENTRY_VERSION,
    ) -> None:
        self.entry_id = entry_id
        self.title = title
        self.data = dict(data)
        self.options = dict(options or {})
        self.runtime_data: Any = None
        self.version = version
        self.state = ConfigEntryState.LOADED
        self.unload_callbacks: list[Callable[[], None]] = []
        self.update_listeners: list[Callable[..., Any]] = []

    def async_on_unload(self, func: Callable[[], None]) -> None:
        self.unload_callbacks.append(func)

    def add_update_listener(self, listener: Callable[..., Any]) -> Callable[[], None]:
        self.update_listeners.append(listener)

        def remove_listener() -> None:
            self.update_listeners.remove(listener)

        return remove_listener


@pytest.fixture(autouse=True)
def reset_fakes(monkeypatch: pytest.MonkeyPatch) -> list[FakeSession]:
    """Patch lifecycle dependencies at the integration boundary."""
    import custom_components.kepco_on as init_module
    import custom_components.kepco_on.coordinator as coordinator_module

    sessions: list[FakeSession] = []

    def make_session(
        hass: FakeHass,
        *,
        auto_cleanup: bool,
        cookie_jar: CookieJar,
    ) -> FakeSession:
        del hass
        assert auto_cleanup is False
        session = FakeSession()
        session.cookie_jar = cookie_jar
        sessions.append(session)
        return session

    FakeStore.instances = []
    FakeStore.load_results = []
    FakeStore.save_errors = []
    FakeAuth.instances = []
    FakeAuth.restore_results = []
    FakeAuth.validate_results = [True]
    FakeAuth.login_results = []
    FakeClient.instances = []
    FakeClient.account_results = ["INDI"]
    FakeClient.customer_results = [(customer("key-1"),)]
    FakeClient.bill_results = [bill("202608")]
    FakeCoordinator.instances = []
    FakeCoordinator.refresh_results = [None]
    monkeypatch.setattr(init_module, "async_create_clientsession", make_session)
    monkeypatch.setattr(init_module, "KepcoOnSessionStore", FakeStore)
    monkeypatch.setattr(init_module, "KepcoOnAuth", FakeAuth)
    monkeypatch.setattr(init_module, "KepcoOnClient", FakeClient)
    monkeypatch.setattr(init_module, "KepcoOnDataUpdateCoordinator", FakeCoordinator)
    monkeypatch.setattr(init_module, "async_create_issue", lambda hass, entry, kind: None)
    monkeypatch.setattr(init_module, "async_clear_issue", lambda hass, entry, kind: None)
    monkeypatch.setattr(coordinator_module, "async_create_issue", lambda hass, entry, kind: None)
    monkeypatch.setattr(coordinator_module, "async_clear_issue", lambda hass, entry, kind: None)
    return sessions


def account_session() -> KepcoAccountSession:
    """Return a canary-bearing authenticated session."""
    return KepcoAccountSession(
        refresh_token=TOKEN_SECRET,
        token="ACCESS_SECRET_CANARY",
        user_id="USER_SECRET",
        member_name="MEMBER_SECRET",
        user_mng_seqno="SEQ_SECRET",
        updated_at=datetime(2026, 9, 1, tzinfo=UTC),
    )


def customer(stable_key: str, *, apartment: str = "푸른아파트") -> KepcoCustomer:
    """Return a synthetic selected customer."""
    return KepcoCustomer(
        stable_key=stable_key,
        apartment_name=apartment,
        dong="101",
        ho="1001",
        contract_method="아파트(단일계약)",
        is_supported=True,
        _customer_number=f"{RAW_CUSTOMER_SECRET}_{stable_key}",
        _house_contract_number=f"{RAW_HOUSE_SECRET}_{stable_key}",
    )


def bill(month: str, usage: int = 321) -> KepcoBill:
    """Return a synthetic bill."""
    return KepcoBill(bill_month=month, usage_kwh=usage, amount_krw=96330)


def make_entry(
    *,
    with_handoff: bool = False,
    save_password: bool = False,
    customers: tuple[KepcoCustomer, ...] = (customer("key-1"),),
    selected: list[str] | None = None,
    options: Mapping[str, Any] | None = None,
    version: int = CONFIG_ENTRY_VERSION,
) -> FakeConfigEntry:
    data: dict[str, Any] = {
        CONF_USERNAME: "input-user",
        CONF_SAVE_PASSWORD: save_password,
        CONF_ACCOUNT_UID_HASH: "account-hash",
        CONF_CUSTOMERS: [serialize_customer(item) for item in customers],
        CONF_SELECTED_CUSTOMERS: selected or [customers[0].stable_key],
    }
    if save_password:
        data[CONF_PASSWORD] = PASSWORD_SECRET
    if with_handoff:
        data[CONF_SESSION_HANDOFF] = session_to_payload(account_session())
    return FakeConfigEntry(data=data, options=options, version=version)


@pytest.mark.asyncio
async def test_migrate_v1_removes_legacy_sensor_toggles_and_preserves_options() -> None:
    from custom_components.kepco_on import async_migrate_entry

    hass = FakeHass()
    entry = make_entry(
        version=1,
        options={
            OPT_POLLING_INTERVAL_HOURS: 12,
            OPT_ENABLE_DETAILED_SENSORS: False,
            OPT_ENABLE_CO2_ESTIMATE: False,
            OPT_CO2_FACTOR_KG_PER_KWH: 0.5,
            OPT_HISTORY_MONTHS: 18,
        },
    )

    assert await async_migrate_entry(cast("Any", hass), cast("Any", entry)) is True

    assert entry.version == CONFIG_ENTRY_VERSION
    assert entry.options == {
        OPT_POLLING_INTERVAL_HOURS: 12,
        OPT_CO2_FACTOR_KG_PER_KWH: 0.5,
        OPT_HISTORY_MONTHS: 18,
    }
    assert hass.config_entries.version_updates == [CONFIG_ENTRY_VERSION]
    assert entry.title == "101동 1001호"


@pytest.mark.asyncio
async def test_migrate_v2_updates_location_title_and_preserves_explicit_display_name() -> None:
    from custom_components.kepco_on import async_migrate_entry

    hass = FakeHass()
    location_entry = make_entry(version=2)
    explicit_entry = make_entry(version=2)
    explicit_entry.data[CONF_DISPLAY_NAME] = "우리 집 전기"

    assert await async_migrate_entry(cast("Any", hass), cast("Any", location_entry)) is True
    assert await async_migrate_entry(cast("Any", hass), cast("Any", explicit_entry)) is True

    assert location_entry.title == "101동 1001호"
    assert explicit_entry.title == "우리 집 전기"
    assert location_entry.version == CONFIG_ENTRY_VERSION
    assert explicit_entry.version == CONFIG_ENTRY_VERSION


@pytest.mark.asyncio
async def test_migrate_current_entry_is_noop_and_unknown_version_is_rejected() -> None:
    from custom_components.kepco_on import async_migrate_entry

    hass = FakeHass()
    current = make_entry(options={OPT_HISTORY_MONTHS: 12})
    future = make_entry(version=CONFIG_ENTRY_VERSION + 1)

    assert await async_migrate_entry(cast("Any", hass), cast("Any", current)) is True
    assert await async_migrate_entry(cast("Any", hass), cast("Any", future)) is False
    assert hass.config_entries.version_updates == []
    assert current.options == {OPT_HISTORY_MONTHS: 12}


@pytest.mark.asyncio
async def test_setup_consumes_handoff_saves_store_and_scrubs_entry_data(
    reset_fakes: list[FakeSession],
) -> None:
    from custom_components.kepco_on import async_setup_entry

    hass = FakeHass()
    entry = make_entry(with_handoff=True)

    assert await async_setup_entry(cast("Any", hass), cast("Any", entry)) is True

    assert FakeStore.instances[0].saved == [account_session()]
    assert CONF_SESSION_HANDOFF not in entry.data
    assert TOKEN_SECRET not in json.dumps(entry.data)
    assert hass.config_entries.updates[-1] == entry.data
    assert FakeCoordinator.instances[0].refresh_calls == 1
    assert hass.config_entries.forwarded == [(entry, PLATFORMS)]
    assert entry.runtime_data.session is reset_fakes[0]
    assert entry.runtime_data.client is FakeClient.instances[0]
    assert entry.runtime_data.session_store is FakeStore.instances[0]
    assert entry.unload_callbacks == []
    assert entry.update_listeners == []


@pytest.mark.asyncio
async def test_setup_constructs_runtime_client_with_home_assistant_local_clock() -> None:
    import custom_components.kepco_on as init_module

    hass = FakeHass()
    entry = make_entry(save_password=True)
    FakeAuth.login_results = [account_session()]

    assert await init_module.async_setup_entry(cast("Any", hass), cast("Any", entry)) is True

    assert FakeClient.instances[0].clock is dt_util.now


@pytest.mark.asyncio
async def test_setup_save_failure_retains_handoff_and_closes_session(
    reset_fakes: list[FakeSession],
) -> None:
    from custom_components.kepco_on import async_setup_entry

    hass = FakeHass()
    entry = make_entry(with_handoff=True)
    FakeStore.save_errors = [RuntimeError("store unavailable")]

    with pytest.raises(ConfigEntryError) as raised:
        await async_setup_entry(cast("Any", hass), cast("Any", entry))

    assert "store unavailable" not in str(raised.value)
    assert CONF_SESSION_HANDOFF in entry.data
    assert hass.config_entries.updates == []
    assert reset_fakes[0].closed is True


@pytest.mark.asyncio
async def test_setup_malformed_handoff_is_safe_and_keeps_handoff(
    reset_fakes: list[FakeSession],
) -> None:
    from custom_components.kepco_on import async_setup_entry

    hass = FakeHass()
    entry = make_entry(with_handoff=True)
    entry.data[CONF_SESSION_HANDOFF] = {"schema": 1, "refresh_token": TOKEN_SECRET}

    with pytest.raises(ConfigEntryError) as raised:
        await async_setup_entry(cast("Any", hass), cast("Any", entry))

    assert CONF_SESSION_HANDOFF in entry.data
    assert TOKEN_SECRET not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert reset_fakes[0].closed is True


@pytest.mark.asyncio
async def test_setup_without_handoff_restores_valid_session(
    reset_fakes: list[FakeSession],
) -> None:
    from custom_components.kepco_on import async_setup_entry

    hass = FakeHass()
    entry = make_entry()
    FakeAuth.restore_results = [True]
    FakeAuth.validate_results = [True]

    assert await async_setup_entry(cast("Any", hass), cast("Any", entry)) is True

    assert CONF_SESSION_HANDOFF not in entry.data
    assert FakeAuth.instances[0].login_calls == []
    assert reset_fakes[0].closed is False


@pytest.mark.asyncio
async def test_setup_invalid_restore_uses_saved_password_reauth() -> None:
    from custom_components.kepco_on import async_setup_entry

    hass = FakeHass()
    entry = make_entry(save_password=True)
    FakeAuth.restore_results = [True]
    FakeAuth.validate_results = [False]
    FakeAuth.login_results = [account_session()]

    assert await async_setup_entry(cast("Any", hass), cast("Any", entry)) is True

    assert FakeAuth.instances[0].login_calls == [("input-user", PASSWORD_SECRET)]


@pytest.mark.asyncio
async def test_setup_reauthenticates_legacy_session_without_cookies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import custom_components.kepco_on as init_module

    class LegacySessionAuth(FakeAuth):
        @property
        def current_session(self) -> KepcoAccountSession:
            return account_session()

    monkeypatch.setattr(init_module, "KepcoOnAuth", LegacySessionAuth)
    hass = FakeHass()
    entry = make_entry(save_password=True)
    FakeAuth.restore_results = [True]
    FakeAuth.login_results = [account_session()]

    assert await init_module.async_setup_entry(cast("Any", hass), cast("Any", entry)) is True

    assert FakeStore.instances[0].cleared is True
    assert FakeAuth.instances[0].login_calls == [("input-user", PASSWORD_SECRET)]


@pytest.mark.asyncio
async def test_setup_legacy_session_without_password_requests_reauthentication(
    monkeypatch: pytest.MonkeyPatch,
    reset_fakes: list[FakeSession],
) -> None:
    import custom_components.kepco_on as init_module

    class LegacySessionAuth(FakeAuth):
        @property
        def current_session(self) -> KepcoAccountSession:
            return account_session()

    monkeypatch.setattr(init_module, "KepcoOnAuth", LegacySessionAuth)
    hass = FakeHass()
    entry = make_entry()
    FakeAuth.restore_results = [True]

    with pytest.raises(ConfigEntryAuthFailed):
        await init_module.async_setup_entry(cast("Any", hass), cast("Any", entry))

    assert reset_fakes[0].closed is True


@pytest.mark.asyncio
async def test_setup_reauthenticates_when_account_response_is_incomplete() -> None:
    import custom_components.kepco_on as init_module

    hass = FakeHass()
    entry = make_entry(save_password=True)
    FakeAuth.restore_results = [True]
    FakeAuth.validate_results = [True]
    FakeAuth.login_results = [account_session()]
    FakeClient.account_results = [
        KepcoOnSessionExpired("incomplete restored account session"),
        "INDI",
    ]

    assert await init_module.async_setup_entry(cast("Any", hass), cast("Any", entry)) is True

    assert FakeStore.instances[0].cleared is True
    assert FakeAuth.instances[0].login_calls == [("input-user", PASSWORD_SECRET)]


@pytest.mark.asyncio
async def test_setup_incomplete_account_without_password_requests_reauthentication(
    reset_fakes: list[FakeSession],
) -> None:
    import custom_components.kepco_on as init_module

    hass = FakeHass()
    entry = make_entry()
    FakeAuth.restore_results = [True]
    FakeAuth.validate_results = [True]
    FakeClient.account_results = [KepcoOnSessionExpired("incomplete restored account session")]

    with pytest.raises(ConfigEntryAuthFailed):
        await init_module.async_setup_entry(cast("Any", hass), cast("Any", entry))

    assert reset_fakes[0].closed is True


@pytest.mark.asyncio
async def test_setup_invalid_restore_without_saved_password_raises_auth_failed(
    reset_fakes: list[FakeSession],
) -> None:
    from custom_components.kepco_on import async_setup_entry

    hass = FakeHass()
    entry = make_entry()
    FakeAuth.restore_results = [True]
    FakeAuth.validate_results = [False]

    with pytest.raises(ConfigEntryAuthFailed):
        await async_setup_entry(cast("Any", hass), cast("Any", entry))

    assert reset_fakes[0].closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("raised", [KepcoOnConnectionError("down"), KepcoOnRateLimitError("slow")])
async def test_setup_connection_errors_raise_not_ready(
    reset_fakes: list[FakeSession],
    raised: Exception,
) -> None:
    from custom_components.kepco_on import async_setup_entry

    hass = FakeHass()
    entry = make_entry(save_password=True)
    FakeAuth.login_results = [raised]

    with pytest.raises(ConfigEntryNotReady):
        await async_setup_entry(cast("Any", hass), cast("Any", entry))

    assert reset_fakes[0].closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raised",
    [KepcoOnProtocolError(f"bad {TOKEN_SECRET}"), KepcoOnNoCustomersError("none")],
)
async def test_setup_protocol_errors_raise_safe_config_entry_error(
    reset_fakes: list[FakeSession],
    raised: Exception,
) -> None:
    from custom_components.kepco_on import async_setup_entry

    hass = FakeHass()
    entry = make_entry(save_password=True)
    FakeAuth.login_results = [account_session()]
    FakeClient.account_results = [raised]

    with pytest.raises(ConfigEntryError) as error:
        await async_setup_entry(cast("Any", hass), cast("Any", entry))

    assert TOKEN_SECRET not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert reset_fakes[0].closed is True


@pytest.mark.asyncio
async def test_setup_reports_and_clears_safe_repair_issues(
    reset_fakes: list[FakeSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import custom_components.kepco_on as init_module

    del reset_fakes
    created: list[tuple[str, str]] = []
    cleared: list[tuple[str, str]] = []
    monkeypatch.setattr(
        init_module,
        "async_create_issue",
        lambda hass, entry, kind: created.append((entry.entry_id, kind)),
    )
    monkeypatch.setattr(
        init_module,
        "async_clear_issue",
        lambda hass, entry, kind: cleared.append((entry.entry_id, kind)),
    )
    hass = FakeHass()
    entry = make_entry(save_password=True)
    FakeAuth.login_results = [account_session()]
    FakeClient.account_results = [KepcoOnUnsupportedAccount(f"corp {TOKEN_SECRET}")]

    with pytest.raises(ConfigEntryError):
        await init_module.async_setup_entry(cast("Any", hass), cast("Any", entry))

    assert created == [(entry.entry_id, "unsupported_account")]
    assert TOKEN_SECRET not in repr(created)

    FakeAuth.login_results = [account_session()]
    FakeClient.account_results = ["INDI"]
    FakeCoordinator.refresh_results = [None]

    assert await init_module.async_setup_entry(cast("Any", hass), cast("Any", entry)) is True

    assert cleared == [
        (entry.entry_id, "login_schema_changed"),
        (entry.entry_id, "customer_schema_changed"),
        (entry.entry_id, "unsupported_account"),
        (entry.entry_id, "session_restore_failed"),
    ]


@pytest.mark.asyncio
async def test_setup_reports_protocol_phase_repairs_without_raw_exception_data(
    reset_fakes: list[FakeSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import custom_components.kepco_on as init_module

    del reset_fakes
    created: list[tuple[str, str]] = []
    monkeypatch.setattr(
        init_module,
        "async_create_issue",
        lambda hass, entry, kind: created.append((entry.entry_id, kind)),
    )
    monkeypatch.setattr(init_module, "async_clear_issue", lambda hass, entry, kind: None)
    hass = FakeHass()
    entry = make_entry(save_password=True)
    FakeAuth.login_results = [KepcoOnProtocolError(f"login changed {TOKEN_SECRET}")]

    with pytest.raises(ConfigEntryError) as error:
        await init_module.async_setup_entry(cast("Any", hass), cast("Any", entry))

    assert created == [(entry.entry_id, "login_schema_changed")]
    assert TOKEN_SECRET not in str(error.value)
    assert TOKEN_SECRET not in repr(created)


@pytest.mark.asyncio
async def test_setup_reports_session_restore_persistence_failure_only(
    reset_fakes: list[FakeSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import custom_components.kepco_on as init_module

    created: list[tuple[str, str]] = []
    monkeypatch.setattr(
        init_module,
        "async_create_issue",
        lambda hass, entry, kind: created.append((entry.entry_id, kind)),
    )
    monkeypatch.setattr(init_module, "async_clear_issue", lambda hass, entry, kind: None)

    hass = FakeHass()
    entry = make_entry(with_handoff=True)
    FakeStore.save_errors = [RuntimeError(f"store failed {TOKEN_SECRET}")]

    with pytest.raises(ConfigEntryError) as error:
        await init_module.async_setup_entry(cast("Any", hass), cast("Any", entry))

    assert created == [(entry.entry_id, "session_restore_failed")]
    assert TOKEN_SECRET not in str(error.value)
    assert TOKEN_SECRET not in repr(created)
    assert reset_fakes[0].closed is True

    created.clear()
    entry = make_entry()
    FakeAuth.restore_results = [True]
    FakeAuth.validate_results = [False]

    with pytest.raises(ConfigEntryAuthFailed):
        await init_module.async_setup_entry(cast("Any", hass), cast("Any", entry))

    assert created == []


@pytest.mark.asyncio
@pytest.mark.parametrize("raised", [KepcoOnConnectionError("down"), KepcoOnRateLimitError("slow")])
async def test_setup_temporary_failures_do_not_create_repairs(
    reset_fakes: list[FakeSession],
    monkeypatch: pytest.MonkeyPatch,
    raised: Exception,
) -> None:
    import custom_components.kepco_on as init_module

    del reset_fakes
    created: list[tuple[str, str]] = []
    monkeypatch.setattr(
        init_module,
        "async_create_issue",
        lambda hass, entry, kind: created.append((entry.entry_id, kind)),
    )
    monkeypatch.setattr(init_module, "async_clear_issue", lambda hass, entry, kind: None)
    hass = FakeHass()
    entry = make_entry(save_password=True)
    FakeAuth.login_results = [raised]

    with pytest.raises(ConfigEntryNotReady):
        await init_module.async_setup_entry(cast("Any", hass), cast("Any", entry))

    assert created == []


@pytest.mark.asyncio
async def test_setup_uses_polling_option_for_coordinator_interval() -> None:
    from custom_components.kepco_on import async_setup_entry

    hass = FakeHass()
    entry = make_entry(save_password=True, options={OPT_POLLING_INTERVAL_HOURS: 12})
    FakeAuth.login_results = [account_session()]

    assert await async_setup_entry(cast("Any", hass), cast("Any", entry)) is True

    assert FakeCoordinator.instances[0].update_interval == timedelta(hours=12)


@pytest.mark.asyncio
async def test_options_flow_after_setup_schedules_one_reload_without_listener_conflict() -> None:
    from custom_components.kepco_on import async_setup_entry
    from custom_components.kepco_on.config_flow import KepcoOnOptionsFlow

    hass = FakeHass()
    entry = make_entry(save_password=True)
    FakeAuth.login_results = [account_session()]
    assert await async_setup_entry(cast("Any", hass), cast("Any", entry)) is True
    hass.config_entries.entries_by_id[entry.entry_id] = entry
    flow = KepcoOnOptionsFlow(cast("Any", entry))
    flow.handler = entry.entry_id
    manager = OptionsFlowManager(cast("Any", hass))

    result = await flow.async_step_init(
        {
            OPT_POLLING_INTERVAL_HOURS: "6",
            OPT_CO2_FACTOR_KG_PER_KWH: "0.459",
            OPT_HISTORY_MONTHS: 18,
        }
    )
    finished = await manager.async_finish_flow(cast("Any", flow), result)

    assert finished is result
    assert entry.update_listeners == []
    assert hass.config_entries.reloads == [entry.entry_id]
    assert entry.options[OPT_POLLING_INTERVAL_HOURS] == 6


@pytest.mark.asyncio
async def test_unload_unloads_platforms_and_closes_session() -> None:
    from custom_components.kepco_on import async_setup_entry, async_unload_entry

    hass = FakeHass()
    entry = make_entry(save_password=True)
    FakeAuth.login_results = [account_session()]
    assert await async_setup_entry(cast("Any", hass), cast("Any", entry)) is True
    session = entry.runtime_data.session

    assert await async_unload_entry(cast("Any", hass), cast("Any", entry)) is True

    assert hass.config_entries.unloaded == [(entry, PLATFORMS)]
    assert session.closed is True
    assert entry.runtime_data is None


@pytest.mark.asyncio
async def test_unload_keeps_session_open_when_platform_unload_fails() -> None:
    from custom_components.kepco_on import async_setup_entry, async_unload_entry

    hass = FakeHass()
    hass.config_entries.unload_result = False
    entry = make_entry(save_password=True)
    FakeAuth.login_results = [account_session()]
    assert await async_setup_entry(cast("Any", hass), cast("Any", entry)) is True
    runtime_data = entry.runtime_data

    assert await async_unload_entry(cast("Any", hass), cast("Any", entry)) is False

    assert entry.runtime_data is runtime_data
    assert entry.runtime_data.session.closed is False


@pytest.mark.asyncio
async def test_first_refresh_failure_does_not_assign_runtime_and_closes_session(
    reset_fakes: list[FakeSession],
) -> None:
    from custom_components.kepco_on import async_setup_entry

    hass = FakeHass()
    entry = make_entry(save_password=True)
    FakeAuth.login_results = [account_session()]
    FakeCoordinator.refresh_results = [UpdateFailed("temporary")]

    with pytest.raises(ConfigEntryNotReady):
        await async_setup_entry(cast("Any", hass), cast("Any", entry))

    assert reset_fakes[0].closed is True
    assert entry.runtime_data is None


@pytest.mark.asyncio
async def test_platform_forward_failure_clears_runtime_and_closes_session(
    reset_fakes: list[FakeSession],
) -> None:
    from custom_components.kepco_on import async_setup_entry

    hass = FakeHass()
    hass.config_entries.forward_result = False
    entry = make_entry(save_password=True)
    FakeAuth.login_results = [account_session()]

    with pytest.raises(ConfigEntryError):
        await async_setup_entry(cast("Any", hass), cast("Any", entry))

    assert reset_fakes[0].closed is True
    assert entry.runtime_data is None


def test_coordinator_default_and_option_intervals() -> None:
    from custom_components.kepco_on.coordinator import KepcoOnDataUpdateCoordinator

    hass = cast("Any", Mock())
    default_entry = make_entry()
    custom_entry = make_entry(options={OPT_POLLING_INTERVAL_HOURS: 1})

    default_coordinator = KepcoOnDataUpdateCoordinator(
        hass,
        cast("Any", default_entry),
        cast("Any", FakeClient(cast("Any", object()))),
        (customer("key-1"),),
    )
    custom_coordinator = KepcoOnDataUpdateCoordinator(
        hass,
        cast("Any", custom_entry),
        cast("Any", FakeClient(cast("Any", object()))),
        (customer("key-1"),),
    )

    assert default_coordinator.update_interval == timedelta(hours=DEFAULT_POLLING_INTERVAL_HOURS)
    assert custom_coordinator.update_interval == timedelta(hours=1)


def test_coordinator_invalid_option_interval_falls_back_to_default() -> None:
    from custom_components.kepco_on.coordinator import KepcoOnDataUpdateCoordinator

    coordinator = KepcoOnDataUpdateCoordinator(
        cast("Any", Mock()),
        cast("Any", make_entry(options={OPT_POLLING_INTERVAL_HOURS: object()})),
        cast("Any", FakeClient(cast("Any", object()))),
        (customer("key-1"),),
    )

    assert coordinator.update_interval == timedelta(hours=DEFAULT_POLLING_INTERVAL_HOURS)


@pytest.mark.asyncio
async def test_coordinator_fetches_selected_customers_sequentially_and_records_success() -> None:
    from custom_components.kepco_on.coordinator import KepcoOnDataUpdateCoordinator

    client = FakeClient(cast("Any", object()))
    customers = (customer("key-1"), customer("key-2"))
    FakeClient.bill_results = [bill("202608", 111), bill("202608", 222)]
    coordinator = KepcoOnDataUpdateCoordinator(
        cast("Any", Mock()),
        cast("Any", make_entry(customers=customers, selected=["key-1", "key-2"])),
        cast("Any", client),
        customers,
    )

    data = await coordinator._async_update_data()

    assert [item.stable_key for item in client.bill_calls] == ["key-1", "key-2"]
    assert data.customers == customers
    assert set(data.bills_by_customer_key) == {"key-1", "key-2"}
    assert data.errors_by_customer_key == {}
    assert data.last_success is not None
    assert data.last_success.tzinfo is UTC


@pytest.mark.asyncio
async def test_coordinator_preserves_successful_bill_when_one_customer_fails() -> None:
    from custom_components.kepco_on.coordinator import KepcoOnDataUpdateCoordinator

    client = FakeClient(cast("Any", object()))
    customers = (customer("key-1"), customer("key-2"))
    FakeClient.bill_results = [bill("202608", 111), KepcoOnProtocolError(f"bad {TOKEN_SECRET}")]
    coordinator = KepcoOnDataUpdateCoordinator(
        cast("Any", Mock()),
        cast("Any", make_entry(customers=customers, selected=["key-1", "key-2"])),
        cast("Any", client),
        customers,
    )

    data = await coordinator._async_update_data()

    assert set(data.bills_by_customer_key) == {"key-1"}
    assert data.errors_by_customer_key == {"key-2": "protocol_error"}
    assert TOKEN_SECRET not in repr(data)


@pytest.mark.asyncio
async def test_coordinator_reports_and_clears_bill_schema_repair_issue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import custom_components.kepco_on.coordinator as coordinator_module
    from custom_components.kepco_on.coordinator import KepcoOnDataUpdateCoordinator

    created: list[tuple[str, str]] = []
    cleared: list[tuple[str, str]] = []
    monkeypatch.setattr(
        coordinator_module,
        "async_create_issue",
        lambda hass, entry, kind: created.append((entry.entry_id, kind)),
    )
    monkeypatch.setattr(
        coordinator_module,
        "async_clear_issue",
        lambda hass, entry, kind: cleared.append((entry.entry_id, kind)),
    )
    client = FakeClient(cast("Any", object()))
    customers = (customer("key-1"), customer("key-2"))
    entry = make_entry(customers=customers, selected=["key-1", "key-2"])
    FakeClient.bill_results = [bill("202608", 111), KepcoOnProtocolError(f"bad {TOKEN_SECRET}")]
    coordinator = KepcoOnDataUpdateCoordinator(
        cast("Any", Mock()),
        cast("Any", entry),
        cast("Any", client),
        customers,
    )

    data = await coordinator._async_update_data()

    assert data.errors_by_customer_key == {"key-2": "protocol_error"}
    assert created == [(entry.entry_id, "bill_schema_changed")]
    assert cleared == []

    FakeClient.bill_results = [bill("202609", 111), bill("202609", 222)]
    data = await coordinator._async_update_data()

    assert data.errors_by_customer_key == {}
    assert cleared == [(entry.entry_id, "bill_schema_changed")]


@pytest.mark.asyncio
async def test_coordinator_does_not_report_network_or_auth_repairs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import custom_components.kepco_on.coordinator as coordinator_module
    from custom_components.kepco_on.coordinator import KepcoOnDataUpdateCoordinator

    created: list[tuple[str, str]] = []
    monkeypatch.setattr(
        coordinator_module,
        "async_create_issue",
        lambda hass, entry, kind: created.append((entry.entry_id, kind)),
    )
    monkeypatch.setattr(
        coordinator_module,
        "async_clear_issue",
        lambda hass, entry, kind: None,
    )

    for raised in (KepcoOnConnectionError("down"), KepcoOnRateLimitError("slow")):
        client = FakeClient(cast("Any", object()))
        FakeClient.bill_results = [raised]
        coordinator = KepcoOnDataUpdateCoordinator(
            cast("Any", Mock()),
            cast("Any", make_entry()),
            cast("Any", client),
            (customer("key-1"),),
        )

        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()

    assert created == []


@pytest.mark.asyncio
async def test_coordinator_all_customers_failed_raises_update_failed() -> None:
    from custom_components.kepco_on.coordinator import KepcoOnDataUpdateCoordinator

    client = FakeClient(cast("Any", object()))
    customers = (customer("key-1"), customer("key-2"))
    FakeClient.bill_results = [KepcoOnProtocolError("bad"), KepcoOnConnectionError("down")]
    coordinator = KepcoOnDataUpdateCoordinator(
        cast("Any", Mock()),
        cast("Any", make_entry(customers=customers, selected=["key-1", "key-2"])),
        cast("Any", client),
        customers,
    )

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


@pytest.mark.asyncio
async def test_coordinator_propagates_unexpected_customer_exception() -> None:
    from custom_components.kepco_on.coordinator import KepcoOnDataUpdateCoordinator

    client = FakeClient(cast("Any", object()))
    customers = (customer("key-1"), customer("key-2"))
    FakeClient.bill_results = [bill("202608", 111), RuntimeError(f"boom {TOKEN_SECRET}")]
    coordinator = KepcoOnDataUpdateCoordinator(
        cast("Any", Mock()),
        cast("Any", make_entry(customers=customers, selected=["key-1", "key-2"])),
        cast("Any", client),
        customers,
    )

    with pytest.raises(RuntimeError) as raised:
        await coordinator._async_update_data()

    assert str(raised.value) == f"boom {TOKEN_SECRET}"


@pytest.mark.asyncio
async def test_coordinator_records_no_customer_and_api_errors_by_safe_category() -> None:
    from custom_components.kepco_on.coordinator import KepcoOnDataUpdateCoordinator

    client = FakeClient(cast("Any", object()))
    customers = (customer("key-1"), customer("key-2"), customer("key-3"))
    FakeClient.bill_results = [
        bill("202608", 111),
        KepcoOnNoCustomersError("none"),
        KepcoOnUnsupportedAccount(f"bad {TOKEN_SECRET}"),
    ]
    coordinator = KepcoOnDataUpdateCoordinator(
        cast("Any", Mock()),
        cast("Any", make_entry(customers=customers, selected=["key-1", "key-2", "key-3"])),
        cast("Any", client),
        customers,
    )

    data = await coordinator._async_update_data()

    assert data.errors_by_customer_key == {"key-2": "no_customers", "key-3": "api_error"}
    assert TOKEN_SECRET not in repr(data)


@pytest.mark.asyncio
async def test_coordinator_all_unexpected_customer_errors_propagate() -> None:
    from custom_components.kepco_on.coordinator import KepcoOnDataUpdateCoordinator

    client = FakeClient(cast("Any", object()))
    FakeClient.bill_results = [RuntimeError(f"boom {TOKEN_SECRET}")]
    coordinator = KepcoOnDataUpdateCoordinator(
        cast("Any", Mock()),
        cast("Any", make_entry()),
        cast("Any", client),
        (customer("key-1"),),
    )

    with pytest.raises(RuntimeError) as raised:
        await coordinator._async_update_data()

    assert str(raised.value) == f"boom {TOKEN_SECRET}"


@pytest.mark.asyncio
async def test_coordinator_auth_expiry_maps_to_config_entry_auth_failed() -> None:
    from custom_components.kepco_on.coordinator import KepcoOnDataUpdateCoordinator

    client = FakeClient(cast("Any", object()))
    FakeClient.bill_results = [KepcoOnSessionExpired("expired")]
    coordinator = KepcoOnDataUpdateCoordinator(
        cast("Any", Mock()),
        cast("Any", make_entry()),
        cast("Any", client),
        (customer("key-1"),),
    )

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


@pytest.mark.asyncio
async def test_coordinator_connection_and_rate_limit_raise_update_failed() -> None:
    from custom_components.kepco_on.coordinator import KepcoOnDataUpdateCoordinator

    for raised in (KepcoOnConnectionError("down"), KepcoOnRateLimitError("slow")):
        client = FakeClient(cast("Any", object()))
        FakeClient.bill_results = [raised]
        coordinator = KepcoOnDataUpdateCoordinator(
            cast("Any", Mock()),
            cast("Any", make_entry()),
            cast("Any", client),
            (customer("key-1"),),
        )

        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()
