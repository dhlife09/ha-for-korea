"""Authentication and session lifecycle for KEPCO ON."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from typing import Protocol

from aiohttp import ClientSession, CookieJar

from .api import KepcoOnTransport
from .const import (
    ENDPOINT_LOGIN_INDI,
    ENDPOINT_SESSION_CHECK,
    ENDPOINT_SSO_CHECK,
    PERSISTED_COOKIE_ALLOWLIST,
)
from .exceptions import KepcoOnAuthError, KepcoOnProtocolError, KepcoOnSessionExpired
from .models import KepcoAccountSession, KepcoCookie
from .session_store import export_cookies, restore_cookies

JsonObject = dict[str, object]
ClockCallback = Callable[[], datetime]
LOGIN_REQUEST_FIELD = "dma_loginData"
LOGIN_RESPONSE_FIELD = "dma_loginData2"
LOGIN_RESPONSE_FIELDS = (
    "result",
    "errorCode",
    "errorMessage",
    "token",
    "refreshToken",
    "userId",
    "mbrsNm",
    "movePage",
    "serviceMode",
    "pwdUpdFlag",
    "frstLoginTF",
    "pwdUp",
    "userMngSeqno",
)
SESSION_VALIDATION_FIELDS = (
    "refreshToken",
    "token",
    "userId",
    "mbrsNm",
    "userMngSeqno",
)


class SessionStore(Protocol):
    """Small persistence protocol used by auth."""

    async def async_load(self) -> KepcoAccountSession | None:
        """Load a persisted session."""

    async def async_save(self, session: KepcoAccountSession) -> None:
        """Persist a session."""

    async def async_clear(self) -> None:
        """Clear a persisted session."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _require_str(payload: JsonObject, field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise KepcoOnProtocolError(f"KEPCO ON authentication response is missing {field}")
    return value


def _safe_value_shape(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, str):
        return f"str:{'nonempty' if value else 'empty'}"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    return "invalid"


def _safe_login_response_shape(payload: JsonObject) -> str:
    return ", ".join(
        f"{field}={_safe_value_shape(payload[field]) if field in payload else 'missing'}"
        for field in LOGIN_RESPONSE_FIELDS
    )


def _login_request_payload(username: str, password: str) -> JsonObject:
    return {
        LOGIN_REQUEST_FIELD: {
            "userId": username,
            "pwdVal": password,
            "autoFlag": "N",
        }
    }


def _login_response_payload(payload: JsonObject) -> JsonObject:
    wrapped = payload.get(LOGIN_RESPONSE_FIELD)
    if isinstance(wrapped, dict):
        return wrapped
    return payload


class KepcoOnAuth:
    """Owns KEPCO ON credentials, current session, and reauthentication."""

    def __init__(
        self,
        session: ClientSession,
        *,
        store: SessionStore,
        reauth_username: str | None = None,
        reauth_password: str | None = None,
        clock: ClockCallback = _utc_now,
    ) -> None:
        self._transport = KepcoOnTransport(session)
        if not isinstance(session.cookie_jar, CookieJar):
            raise KepcoOnProtocolError("KEPCO ON auth requires an aiohttp CookieJar")
        self._cookie_jar = session.cookie_jar
        self._store = store
        self._reauth_username = reauth_username.strip() if reauth_username is not None else None
        self._reauth_password = reauth_password
        self._clock = clock
        self._current_session: KepcoAccountSession | None = None
        self._generation = 0
        self._auth_lock = asyncio.Lock()

    @property
    def current_session(self) -> KepcoAccountSession | None:
        """Return the current account session."""
        return self._current_session

    async def async_login(self, username: str, password: str) -> KepcoAccountSession:
        """Authenticate with one-off credentials and persist the session."""
        async with self._auth_lock:
            return await self._async_login_unlocked(username, password)

    async def async_restore_session(self) -> bool:
        """Restore persisted session state and allowed cookies."""
        session = await self._store.async_load()
        if session is None:
            return False
        self._cookie_jar.clear()
        restore_cookies(
            self._cookie_jar,
            session.cookies,
            PERSISTED_COOKIE_ALLOWLIST,
            now=self._clock(),
        )
        self._current_session = session
        return True

    async def async_reset_session(self) -> None:
        """Discard persisted/runtime authentication state before a clean relogin."""
        async with self._auth_lock:
            await self._store.async_clear()
            self._cookie_jar.clear()
            self._current_session = None
            self._generation += 1

    async def async_validate_session(self) -> bool:
        """Validate and rotate the current session tokens."""
        session = self._require_current_session()
        try:
            payload = await self._transport.request_json(
                ENDPOINT_SESSION_CHECK,
                {
                    "refreshToken": session.refresh_token,
                    "userId": session.user_id,
                    "mbrsNm": session.member_name,
                },
                refresh_token=session.refresh_token,
            )
        except KepcoOnSessionExpired:
            return False
        result = payload.get("result")
        if result is False:
            return False
        if result is not True:
            raise KepcoOnProtocolError("KEPCO ON validation response result is invalid")
        updated = self._session_from_validation(payload, session)
        await self._save_current_session(updated)
        return True

    async def async_reauthenticate(self) -> None:
        """Reauthenticate with the configured password."""
        async with self._auth_lock:
            await self._async_reauthenticate_unlocked()

    async def async_export_session_snapshot(self) -> KepcoAccountSession:
        """Return the current session snapshot without exposing credentials."""
        return self._require_current_session()

    async def async_sso_check(self) -> str:
        """Run the captured SSO check when explicitly needed by validation."""
        session = self._require_current_session()
        if session.user_mng_seqno is None:
            raise KepcoOnProtocolError("KEPCO ON session is missing user sequence")
        payload = await self._transport.request_json(
            ENDPOINT_SSO_CHECK,
            {
                "userId": session.user_id,
                "userMngSeqno": session.user_mng_seqno,
                "name": session.member_name,
                "autoLogin": "Y",
            },
            refresh_token=session.refresh_token,
        )
        if payload.get("loginChk") != "Y":
            raise KepcoOnSessionExpired("KEPCO ON SSO session expired")
        refresh_token = self._optional_str(payload, "refreshToken") or session.refresh_token
        cookies = self._export_cookie_snapshot()
        if refresh_token != session.refresh_token or cookies != session.cookies:
            await self._save_current_session(
                replace(
                    session,
                    refresh_token=refresh_token,
                    cookies=cookies,
                    updated_at=self._clock(),
                ),
                bump_generation=refresh_token != session.refresh_token,
            )
        return refresh_token

    async def async_protected_request(
        self,
        path: str,
        payload: JsonObject | None,
        *,
        submission_id: str | None = None,
    ) -> JsonObject:
        """Run a protected request with one relogin and one replay on expiry."""
        session = self._require_current_session()
        generation = self._generation
        try:
            result = await self._transport.request_json(
                path,
                payload,
                refresh_token=session.refresh_token,
                submission_id=submission_id,
            )
        except KepcoOnSessionExpired:
            async with self._auth_lock:
                if self._generation == generation:
                    await self._async_reauthenticate_unlocked()
            replay_session = self._require_current_session()
            result = await self._transport.request_json(
                path,
                payload,
                refresh_token=replay_session.refresh_token,
                submission_id=submission_id,
            )
        await self._async_persist_cookie_snapshot()
        return result

    def account_uid_hash(self) -> str:
        """Return a stable one-way account hash for customer key derivation."""
        session = self._require_current_session()
        return sha256(
            f"kepco_on:account:v1\0{session.user_id}\0{session.member_name}".encode()
        ).hexdigest()

    async def _async_reauthenticate_unlocked(self) -> None:
        if not self._reauth_username or not self._reauth_password:
            raise KepcoOnAuthError("KEPCO ON password is required to reauthenticate")
        await self._async_login_unlocked(self._reauth_username, self._reauth_password)

    async def _async_login_unlocked(self, username: str, password: str) -> KepcoAccountSession:
        trimmed_username = username.strip()
        if not trimmed_username:
            raise KepcoOnAuthError("KEPCO ON username is required to authenticate")
        if not password.strip():
            raise KepcoOnAuthError("KEPCO ON password is required to authenticate")
        self._cookie_jar.clear()
        await self._transport.async_prepare_login_session()
        login_payload = _login_request_payload(trimmed_username, password)
        payload = await self._transport.request_json(
            ENDPOINT_LOGIN_INDI,
            login_payload,
            submission_id="mf_login_popup_wframe_sbm_submission4",
        )
        if not payload:
            payload = await self._transport.request_json(
                ENDPOINT_LOGIN_INDI,
                login_payload,
                submission_id="mf_login_popup_wframe_sbm_submission4",
            )
        payload = _login_response_payload(payload)
        if payload.get("result") == "NO":
            raise KepcoOnAuthError("KEPCO ON authentication failed")
        try:
            session = KepcoAccountSession(
                refresh_token=_require_str(payload, "refreshToken"),
                token=self._optional_str(payload, "token"),
                user_id=_require_str(payload, "userId"),
                member_name=_require_str(payload, "mbrsNm"),
                user_mng_seqno=self._optional_str(payload, "userMngSeqno"),
                cookies=self._export_cookie_snapshot(),
                updated_at=self._clock(),
            )
        except KepcoOnProtocolError as err:
            raise KepcoOnProtocolError(
                f"{err}; login response shape: {_safe_login_response_shape(payload)}"
            ) from err
        await self._save_current_session(session)
        return session

    async def _async_persist_cookie_snapshot(self) -> None:
        """Persist rotated KEPCO session cookies without changing auth generation."""
        async with self._auth_lock:
            session = self._require_current_session()
            cookies = self._export_cookie_snapshot()
            if cookies == session.cookies:
                return
            await self._save_current_session(
                replace(session, cookies=cookies, updated_at=self._clock()),
                bump_generation=False,
            )

    async def _save_current_session(
        self,
        session: KepcoAccountSession,
        *,
        bump_generation: bool = True,
    ) -> None:
        await self._store.async_save(session)
        self._current_session = session
        if bump_generation:
            self._generation += 1

    def _export_cookie_snapshot(self) -> tuple[KepcoCookie, ...]:
        """Return the current allowlisted KEPCO cookie snapshot."""
        return export_cookies(
            self._cookie_jar,
            PERSISTED_COOKIE_ALLOWLIST,
            now=self._clock(),
        )

    def _session_from_validation(
        self, payload: JsonObject, previous: KepcoAccountSession
    ) -> KepcoAccountSession:
        if not any(field in payload for field in SESSION_VALIDATION_FIELDS):
            return replace(
                previous,
                cookies=self._export_cookie_snapshot(),
                updated_at=self._clock(),
            )
        return KepcoAccountSession(
            refresh_token=_require_str(payload, "refreshToken"),
            token=self._optional_str(payload, "token") or previous.token,
            user_id=_require_str(payload, "userId"),
            member_name=_require_str(payload, "mbrsNm"),
            user_mng_seqno=self._optional_str(payload, "userMngSeqno") or previous.user_mng_seqno,
            cookies=self._export_cookie_snapshot(),
            updated_at=self._clock(),
        )

    def _require_current_session(self) -> KepcoAccountSession:
        if self._current_session is None:
            raise KepcoOnSessionExpired("KEPCO ON session is not available")
        return self._current_session

    @staticmethod
    def _optional_str(payload: JsonObject, field: str) -> str | None:
        value = payload.get(field)
        if value is None:
            return None
        if not isinstance(value, str):
            raise KepcoOnProtocolError(f"KEPCO ON authentication response field {field} is invalid")
        return value or None


__all__ = ["KepcoOnAuth"]
