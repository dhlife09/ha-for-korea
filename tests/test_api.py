"""Transport and business API tests for KEPCO ON."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
import ssl
import traceback
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any, Self, cast

import custom_components.kepco_on.api as kepco_api
import pytest
from aiohttp import ClientConnectionError, ClientSession, ClientTimeout, TCPConnector, web
from aresponses import Response, ResponsesMockServer
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from custom_components.kepco_on.api import KepcoOnClient, KepcoOnTransport
from custom_components.kepco_on.exceptions import (
    KepcoOnConnectionError,
    KepcoOnProtocolError,
    KepcoOnRateLimitError,
    KepcoOnSessionExpired,
    KepcoOnUnsupportedAccount,
)
from custom_components.kepco_on.models import KepcoCustomer
from yarl import URL

HOST = "online.kepco.co.kr"
REFERER = "https://online.kepco.co.kr/MYM001D00"
ORIGIN = "https://online.kepco.co.kr/"
REFRESH_SECRET = "REFRESH_SECRET_CANARY"
PASSWORD_SECRET = "PASSWORD_SECRET_CANARY"
TOKEN_SECRET = "TOKEN_SECRET_CANARY"
SLEEP_CALLS: list[float] = []

pytestmark = pytest.mark.usefixtures("socket_enabled")


class StaticLocalResolver:
    """Resolve the fixed KEPCO host to the local redirect test server."""

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[dict[str, object]]:
        del family
        return [
            {
                "hostname": host,
                "host": "127.0.0.1",
                "port": port,
                "family": socket.AF_INET,
                "proto": 0,
                "flags": socket.AI_NUMERICHOST,
            }
        ]

    async def close(self) -> None:
        """No resolver resources to close."""


async def sleep_recorder(seconds: float) -> None:
    SLEEP_CALLS.append(seconds)


def make_server_ssl_context(tmp_path: Path) -> ssl.SSLContext:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(UTC)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, HOST)])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName(HOST), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(private_key, hashes.SHA256())
    )
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(cert_path, key_path)
    return context


def json_response(payload: dict[str, object], *, status: int = 200) -> Response:
    return Response(
        text=json.dumps(payload),
        status=status,
        content_type="application/json",
        charset="UTF-8",
    )


async def request_json(request: web.Request) -> dict[str, object]:
    return cast("dict[str, object]", json.loads((await request.read()).decode()))


async def with_transport(
    handler: Callable[[KepcoOnTransport], Awaitable[object]],
    server: ResponsesMockServer,
) -> object:
    async with server, ClientSession() as session:
        transport = KepcoOnTransport(session, sleep=sleep_recorder)
        return await handler(transport)


@pytest.fixture(autouse=True)
def reset_sleep_calls() -> None:
    SLEEP_CALLS.clear()


@pytest.mark.asyncio
async def test_transport_prepares_login_session_with_fixed_safe_get() -> None:
    captured: dict[str, object] = {}

    async def route(request: web.Request) -> Response:
        captured["method"] = request.method
        captured["path"] = request.path
        captured["headers"] = dict(request.headers)
        return Response(text="<html>KEPCO ON</html>", content_type="text/html")

    server = ResponsesMockServer()
    server.add(HOST, "/MYM001D00", "get", response=route)

    await with_transport(lambda transport: transport.async_prepare_login_session(), server)

    headers = cast("dict[str, str]", captured["headers"])
    assert captured["method"] == "GET"
    assert captured["path"] == "/MYM001D00"
    assert headers["Accept"] == "text/html,application/xhtml+xml"
    assert headers["Referer"] == ORIGIN
    assert headers["User-Agent"] == "HomeAssistant-KEPCO-ON/1.2.1"
    assert "submissionid" not in headers
    assert "refreshToken" not in headers


@pytest.mark.asyncio
async def test_transport_accepts_empty_login_bootstrap_without_content_type() -> None:
    server = ResponsesMockServer()
    server.add(
        HOST,
        "/MYM001D00",
        "get",
        response=Response(body=b"", status=200, headers={}),
    )

    await with_transport(lambda transport: transport.async_prepare_login_session(), server)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "error_type"),
    [
        (Response(text="error", status=500, content_type="text/html"), KepcoOnConnectionError),
    ],
)
async def test_transport_rejects_invalid_login_bootstrap_response(
    response: Response,
    error_type: type[Exception],
) -> None:
    server = ResponsesMockServer()
    server.add(HOST, "/MYM001D00", "get", response=response)

    with pytest.raises(error_type):
        await with_transport(lambda transport: transport.async_prepare_login_session(), server)


@pytest.mark.asyncio
async def test_transport_rejects_oversized_login_bootstrap_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(kepco_api, "MAX_RESPONSE_BYTES", 10)
    server = ResponsesMockServer()
    server.add(
        HOST,
        "/MYM001D00",
        "get",
        response=Response(text="x" * 11, content_type="text/html"),
    )

    with pytest.raises(KepcoOnProtocolError):
        await with_transport(lambda transport: transport.async_prepare_login_session(), server)


@pytest.mark.asyncio
async def test_transport_posts_json_headers_to_allowlisted_kepco_path() -> None:
    captured: dict[str, object] = {}

    async def route(request: web.Request) -> Response:
        captured["method"] = request.method
        captured["path"] = request.path
        captured["body"] = await request_json(request)
        captured["headers"] = dict(request.headers)
        return json_response({"ok": True})

    server = ResponsesMockServer()
    server.add(HOST, "/sessionCheck", "post", response=route)

    await with_transport(
        lambda transport: transport.request_json(
            "/sessionCheck",
            {"refreshToken": REFRESH_SECRET, "userId": "user", "mbrsNm": "member"},
            refresh_token=REFRESH_SECRET,
            submission_id="mf_test",
        ),
        server,
    )

    headers = cast("dict[str, str]", captured["headers"])
    assert captured["method"] == "POST"
    assert captured["path"] == "/sessionCheck"
    assert captured["body"] == {
        "refreshToken": REFRESH_SECRET,
        "userId": "user",
        "mbrsNm": "member",
    }
    assert headers["Accept"] == "application/json"
    assert headers["Content-Type"] == "application/json; charset=UTF-8"
    assert headers["Referer"] == REFERER
    assert headers["Origin"] == ORIGIN
    assert headers["User-Agent"] == "HomeAssistant-KEPCO-ON/1.2.1"
    assert headers["refreshToken"] == REFRESH_SECRET
    assert headers["submissionid"] == "mf_test"
    assert "sec-ch-ua" not in {key.lower() for key in headers}
    assert "sec-fetch-site" not in {key.lower() for key in headers}


@pytest.mark.asyncio
async def test_transport_accepts_json_content_type_with_charset() -> None:
    server = ResponsesMockServer()
    server.add(HOST, "/sessionCheck", "post", response=json_response({"result": True}))

    result = await with_transport(
        lambda transport: transport.request_json("/sessionCheck", {}), server
    )

    assert result == {"result": True}


@pytest.mark.asyncio
async def test_transport_accepts_safely_parseable_json_under_non_json_content_type() -> None:
    server = ResponsesMockServer()
    server.add(
        HOST,
        "/sessionCheck",
        "post",
        response=Response(text='{"result": true}', content_type="text/plain"),
    )

    result = await with_transport(
        lambda transport: transport.request_json("/sessionCheck", {}), server
    )

    assert result == {"result": True}


@pytest.mark.asyncio
async def test_transport_treats_html_login_markup_as_expired_without_secret_leak() -> None:
    server = ResponsesMockServer()
    server.add(
        HOST,
        "/sessionCheck",
        "post",
        response=Response(
            text=f"<html><form>login {PASSWORD_SECRET}</form></html>",
            content_type="text/html",
        ),
    )

    with pytest.raises(KepcoOnSessionExpired) as raised:
        await with_transport(
            lambda transport: transport.request_json("/sessionCheck", {"pwdVal": PASSWORD_SECRET}),
            server,
        )

    assert PASSWORD_SECRET not in str(raised.value)


@pytest.mark.asyncio
async def test_transport_rejects_non_json_200_response() -> None:
    server = ResponsesMockServer()
    server.add(
        HOST,
        "/sessionCheck",
        "post",
        response=Response(text="not-json", content_type="text/plain"),
    )

    with pytest.raises(KepcoOnProtocolError):
        await with_transport(lambda transport: transport.request_json("/sessionCheck", {}), server)


@pytest.mark.asyncio
async def test_transport_invalid_json_does_not_chain_secret_bearing_document() -> None:
    server = ResponsesMockServer()
    server.add(
        HOST,
        "/sessionCheck",
        "post",
        response=Response(
            text=f'{{"token": "{TOKEN_SECRET}",',
            content_type="application/json",
        ),
    )

    with pytest.raises(KepcoOnProtocolError) as raised:
        await with_transport(lambda transport: transport.request_json("/sessionCheck", {}), server)

    error = raised.value
    rendered = "".join(traceback.format_exception(error))
    assert TOKEN_SECRET not in str(error)
    assert TOKEN_SECRET not in repr(error)
    assert TOKEN_SECRET not in repr(error.__cause__)
    assert TOKEN_SECRET not in repr(error.__context__)
    assert TOKEN_SECRET not in rendered
    assert error.__cause__ is None
    assert error.__context__ is None


@pytest.mark.asyncio
async def test_transport_handles_204_as_empty_payload() -> None:
    server = ResponsesMockServer()
    server.add(HOST, "/sessionCheck", "post", response=Response(status=204))

    result = await with_transport(
        lambda transport: transport.request_json("/sessionCheck", {}), server
    )

    assert result == {}


@pytest.mark.asyncio
async def test_transport_enforces_two_mebibyte_response_limit() -> None:
    server = ResponsesMockServer()
    server.add(
        HOST,
        "/sessionCheck",
        "post",
        response=Response(
            body=b"{" + (b'"x":' + b'"' + (b"a" * (2 * 1024 * 1024)) + b'"}'),
            content_type="application/json",
        ),
    )

    with pytest.raises(KepcoOnProtocolError):
        await with_transport(lambda transport: transport.request_json("/sessionCheck", {}), server)


@pytest.mark.asyncio
async def test_transport_rejects_final_host_mismatch_after_redirect() -> None:
    server = ResponsesMockServer()
    server.add(
        HOST,
        "/sessionCheck",
        "post",
        response=Response(status=302, headers={"Location": "https://evil.example/sessionCheck"}),
    )
    server.add(
        "evil.example",
        "/sessionCheck",
        "get",
        response=json_response({"result": True}),
    )

    with pytest.raises(KepcoOnProtocolError):
        await with_transport(lambda transport: transport.request_json("/sessionCheck", {}), server)


@pytest.mark.asyncio
async def test_transport_rejects_final_http_scheme_even_when_host_matches() -> None:
    server = ResponsesMockServer()
    server.add(
        HOST,
        "/sessionCheck",
        "post",
        response=Response(
            status=302, headers={"Location": "http://online.kepco.co.kr/sessionCheck"}
        ),
    )
    server.add(HOST, "/sessionCheck", "get", response=json_response({"result": True}))

    with pytest.raises(KepcoOnProtocolError):
        await with_transport(lambda transport: transport.request_json("/sessionCheck", {}), server)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [302, 307])
async def test_transport_rejects_redirect_without_forwarding_credentials_or_body(
    status: int,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target_requests: list[dict[str, object]] = []
    redirect_port: int | None = None

    async def redirect_source(request: web.Request) -> web.Response:
        assert await request_json(request) == {"refreshToken": REFRESH_SECRET}
        assert request.headers["refreshToken"] == REFRESH_SECRET
        assert redirect_port is not None
        return web.Response(
            status=status,
            headers={"Location": f"https://{HOST}:{redirect_port}/leak"},
        )

    async def redirect_target(request: web.Request) -> web.Response:
        target_requests.append(
            {
                "headers": dict(request.headers),
                "body": await request.read(),
            }
        )
        return web.json_response({"leaked": True})

    app = web.Application()
    app.router.add_post("/sessionCheck", redirect_source)
    app.router.add_route("*", "/leak", redirect_target)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(
        runner,
        "127.0.0.1",
        0,
        ssl_context=make_server_ssl_context(tmp_path),
    )
    await site.start()
    redirect_port = cast("tuple[str, int]", runner.addresses[0])[1]
    monkeypatch.setattr(kepco_api, "BASE_URL", f"https://{HOST}:{redirect_port}")

    try:
        connector = TCPConnector(resolver=cast("Any", StaticLocalResolver()), ssl=False)
        async with ClientSession(connector=connector) as session:
            transport = KepcoOnTransport(session, sleep=sleep_recorder)
            with pytest.raises(KepcoOnProtocolError) as raised:
                await transport.request_json(
                    "/sessionCheck",
                    {"refreshToken": REFRESH_SECRET},
                    refresh_token=REFRESH_SECRET,
                )
    finally:
        await runner.cleanup()

    assert str(raised.value) == "Unexpected KEPCO ON response: redirects are not allowed"
    assert target_requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_transport_maps_401_and_403_to_session_expired(status: int) -> None:
    server = ResponsesMockServer()
    server.add(
        HOST, "/sessionCheck", "post", response=json_response({"error": "expired"}, status=status)
    )

    with pytest.raises(KepcoOnSessionExpired):
        await with_transport(lambda transport: transport.request_json("/sessionCheck", {}), server)


@pytest.mark.asyncio
async def test_transport_honors_integer_retry_after_then_raises_rate_limit() -> None:
    server = ResponsesMockServer()
    server.add(
        HOST,
        "/sessionCheck",
        "post",
        response=json_response({"error": "rate"}, status=429),
    )
    server._responses[0][1].headers["Retry-After"] = "7"

    with pytest.raises(KepcoOnRateLimitError):
        await with_transport(lambda transport: transport.request_json("/sessionCheck", {}), server)

    assert SLEEP_CALLS == [7.0]


@pytest.mark.asyncio
async def test_transport_caps_large_integer_retry_after_then_raises_rate_limit() -> None:
    server = ResponsesMockServer()
    response = json_response({"error": "rate"}, status=429)
    response.headers["Retry-After"] = "999999"
    server.add(HOST, "/sessionCheck", "post", response=response)

    with pytest.raises(KepcoOnRateLimitError):
        await with_transport(lambda transport: transport.request_json("/sessionCheck", {}), server)

    assert SLEEP_CALLS == [kepco_api.MAX_RETRY_AFTER_SECONDS]


@pytest.mark.asyncio
async def test_transport_honors_http_date_retry_after_then_raises_rate_limit() -> None:
    now = datetime(2026, 9, 1, 1, 0, tzinfo=UTC)
    server = ResponsesMockServer()
    response = json_response({"error": "rate"}, status=429)
    response.headers["Retry-After"] = format_datetime(
        datetime(2026, 9, 1, 1, 0, 9, tzinfo=UTC), usegmt=True
    )
    server.add(HOST, "/sessionCheck", "post", response=response)

    with pytest.raises(KepcoOnRateLimitError):
        await with_transport(
            lambda transport: transport.request_json("/sessionCheck", {}, clock=lambda: now),
            server,
        )

    assert SLEEP_CALLS == [9.0]


@pytest.mark.asyncio
async def test_transport_caps_far_future_http_date_retry_after_then_raises_rate_limit() -> None:
    now = datetime(2026, 9, 1, 1, 0, tzinfo=UTC)
    server = ResponsesMockServer()
    response = json_response({"error": "rate"}, status=429)
    response.headers["Retry-After"] = format_datetime(
        datetime(2026, 9, 1, 2, 0, tzinfo=UTC), usegmt=True
    )
    server.add(HOST, "/sessionCheck", "post", response=response)

    with pytest.raises(KepcoOnRateLimitError):
        await with_transport(
            lambda transport: transport.request_json("/sessionCheck", {}, clock=lambda: now),
            server,
        )

    assert SLEEP_CALLS == [kepco_api.MAX_RETRY_AFTER_SECONDS]


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_after", [None, "not-a-date", "-1"])
async def test_transport_invalid_retry_after_sleeps_zero_then_raises_rate_limit(
    retry_after: str | None,
) -> None:
    server = ResponsesMockServer()
    response = json_response({"error": "rate"}, status=429)
    if retry_after is not None:
        response.headers["Retry-After"] = retry_after
    server.add(HOST, "/sessionCheck", "post", response=response)

    with pytest.raises(KepcoOnRateLimitError):
        await with_transport(lambda transport: transport.request_json("/sessionCheck", {}), server)

    assert SLEEP_CALLS == [0.0]


@pytest.mark.asyncio
async def test_transport_treats_naive_http_date_retry_after_as_utc() -> None:
    now = datetime(2026, 9, 1, 1, 0, tzinfo=UTC)
    server = ResponsesMockServer()
    response = json_response({"error": "rate"}, status=429)
    response.headers["Retry-After"] = "Tue, 01 Sep 2026 01:00:05"
    server.add(HOST, "/sessionCheck", "post", response=response)

    with pytest.raises(KepcoOnRateLimitError):
        await with_transport(
            lambda transport: transport.request_json("/sessionCheck", {}, clock=lambda: now),
            server,
        )

    assert SLEEP_CALLS == [5.0]


@pytest.mark.asyncio
async def test_transport_rejects_unallowlisted_path_before_request() -> None:
    async with ClientSession() as session:
        transport = KepcoOnTransport(session, sleep=sleep_recorder)
        with pytest.raises(KepcoOnProtocolError) as raised:
            await transport.request_json("/not-allowed", {})

    assert str(raised.value) == "Unexpected KEPCO ON response: path is not allowlisted"
    assert SLEEP_CALLS == []


@pytest.mark.asyncio
async def test_transport_empty_body_with_whitespace_returns_empty_payload() -> None:
    server = ResponsesMockServer()
    server.add(
        HOST,
        "/sessionCheck",
        "post",
        response=Response(body=b" \r\n\t ", content_type="application/json"),
    )

    result = await with_transport(
        lambda transport: transport.request_json("/sessionCheck", {}), server
    )

    assert result == {}


@pytest.mark.asyncio
async def test_transport_rejects_json_array_root() -> None:
    server = ResponsesMockServer()
    server.add(
        HOST,
        "/sessionCheck",
        "post",
        response=Response(text='["not-object"]', content_type="application/json"),
    )

    with pytest.raises(KepcoOnProtocolError):
        await with_transport(lambda transport: transport.request_json("/sessionCheck", {}), server)


@pytest.mark.asyncio
async def test_transport_retries_5xx_twice_with_exponential_backoff() -> None:
    server = ResponsesMockServer()
    server.add(HOST, "/sessionCheck", "post", response=json_response({"error": "bad"}, status=503))
    server.add(HOST, "/sessionCheck", "post", response=json_response({"error": "bad"}, status=502))
    server.add(HOST, "/sessionCheck", "post", response=json_response({"result": True}))

    result = await with_transport(
        lambda transport: transport.request_json("/sessionCheck", {}), server
    )

    assert result == {"result": True}
    assert SLEEP_CALLS == [1.0, 2.0]


@pytest.mark.asyncio
async def test_transport_does_not_unbounded_read_oversized_5xx_response() -> None:
    class FakeContent:
        reads = 0

        async def read(self, limit: int = -1) -> bytes:
            self.reads += 1
            assert limit == 2 * 1024 * 1024 + 1
            return b"x" * min(limit, 1024)

    class FakeResponse:
        status = 500
        url = URL("https://online.kepco.co.kr/sessionCheck")
        content_type = "application/json"
        charset = "utf-8"

        def __init__(self) -> None:
            self.headers: dict[str, str] = {}
            self.content = FakeContent()
            self.released = False

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback_obj: object,
        ) -> None:
            del exc_type, exc, traceback_obj

        async def read(self) -> bytes:
            raise AssertionError("5xx retry path must not call unbounded response.read()")

        def release(self) -> None:
            self.released = True

    class FakeSession:
        attempts = 0
        responses: list[FakeResponse]

        def __init__(self) -> None:
            self.responses = []

        def post(
            self,
            url: str,
            *,
            json: dict[str, object] | None,
            headers: dict[str, str],
            timeout: ClientTimeout,
            allow_redirects: bool,
        ) -> FakeResponse:
            del url, json, headers, timeout
            assert allow_redirects is False
            self.attempts += 1
            response = FakeResponse()
            self.responses.append(response)
            return response

    fake_session = FakeSession()
    transport = KepcoOnTransport(cast("ClientSession", fake_session), sleep=sleep_recorder)

    with pytest.raises(KepcoOnConnectionError) as raised:
        await transport.request_json("/sessionCheck", {})

    assert "HTTP 500" in str(raised.value)
    assert fake_session.attempts == 3
    assert [response.content.reads for response in fake_session.responses] == [1, 1, 1]
    assert [response.released for response in fake_session.responses] == [True, True, True]
    assert SLEEP_CALLS == [1.0, 2.0]


@pytest.mark.asyncio
async def test_transport_wraps_network_errors_without_secret_leak() -> None:
    class FailingPostContext:
        async def __aenter__(self) -> Self:
            raise ClientConnectionError("simulated DNS failure")

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback_obj: object,
        ) -> None:
            del exc_type, exc, traceback_obj

    class FakeSession:
        attempts = 0

        def post(
            self,
            url: str,
            *,
            json: dict[str, object] | None,
            headers: dict[str, str],
            timeout: ClientTimeout,
            allow_redirects: bool,
        ) -> FailingPostContext:
            del url, json, headers, timeout
            assert allow_redirects is False
            self.attempts += 1
            return FailingPostContext()

    fake_session = FakeSession()
    transport = KepcoOnTransport(cast("ClientSession", fake_session), sleep=sleep_recorder)

    with pytest.raises(KepcoOnConnectionError) as raised:
        await transport.request_json("/sessionCheck", {"pwdVal": PASSWORD_SECRET})

    assert PASSWORD_SECRET not in str(raised.value)
    assert PASSWORD_SECRET not in repr(raised.value)
    assert PASSWORD_SECRET not in str(raised.value.__cause__)
    assert PASSWORD_SECRET not in repr(raised.value.__cause__)
    assert PASSWORD_SECRET not in str(raised.value.__context__)
    assert PASSWORD_SECRET not in repr(raised.value.__context__)
    assert PASSWORD_SECRET not in "".join(
        traceback.format_exception(raised.type, raised.value, raised.tb)
    )
    assert fake_session.attempts == 3
    assert SLEEP_CALLS == [1.0, 2.0]


@pytest.mark.asyncio
async def test_client_get_account_type_accepts_indi_and_rejects_other_types() -> None:
    class Auth:
        calls = 0

        async def async_protected_request(
            self, path: str, payload: dict[str, object] | None, *, submission_id: str | None = None
        ) -> dict[str, object]:
            self.calls += 1
            assert path == "/isCorp"
            assert payload is None
            assert submission_id is None
            return {"userClNm": "INDI" if self.calls == 1 else "CORP"}

        def account_uid_hash(self) -> str:
            return "HASH"

    auth = Auth()
    client = KepcoOnClient(cast("Any", auth))

    assert await client.async_get_account_type() == "INDI"
    with pytest.raises(KepcoOnUnsupportedAccount):
        await client.async_get_account_type()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{}, {"result": False}, {"userClNm": ""}])
async def test_client_get_account_type_treats_incomplete_response_as_expired(
    payload: dict[str, object],
) -> None:
    class Auth:
        async def async_protected_request(
            self,
            path: str,
            request_payload: dict[str, object] | None,
            *,
            submission_id: str | None = None,
        ) -> dict[str, object]:
            assert path == "/isCorp"
            assert request_payload is None
            assert submission_id is None
            return payload

        def account_uid_hash(self) -> str:
            return "HASH"

    client = KepcoOnClient(cast("Any", Auth()))

    with pytest.raises(KepcoOnSessionExpired):
        await client.async_get_account_type()


@pytest.mark.asyncio
async def test_client_get_account_type_rejects_invalid_type_shape() -> None:
    class Auth:
        async def async_protected_request(
            self,
            path: str,
            payload: dict[str, object] | None,
            *,
            submission_id: str | None = None,
        ) -> dict[str, object]:
            assert path == "/isCorp"
            assert payload is None
            assert submission_id is None
            return {"userClNm": True}

        def account_uid_hash(self) -> str:
            return "HASH"

    client = KepcoOnClient(cast("Any", Auth()))

    with pytest.raises(KepcoOnProtocolError):
        await client.async_get_account_type()


@pytest.mark.asyncio
async def test_client_get_customers_uses_mypage_endpoint_body_and_parser() -> None:
    async def protected_request(
        path: str, payload: dict[str, object] | None, *, submission_id: str | None = None
    ) -> dict[str, object]:
        assert path == "/my/indi/info/myPageCustNoList"
        assert submission_id == "mf_wfm_layout_sbm_myPageCustList"
        assert payload == {
            "dma_search": {
                "schYm": "",
                "custNo": "",
                "gubun": "",
                "schChart": "12",
                "CUST_NO": "",
                "housCntrNo": "",
                "yyyymm": "",
                "searchType": "",
                "dong": "",
                "ho": "",
                "months": "",
                "chgYmd": "",
            }
        }
        dma_search = cast("dict[str, str]", payload["dma_search"])
        assert "searchKeyword" not in dma_search
        return {
            "dlt_myPageAppendList": [
                {
                    "APT_DONGNO": "1001",
                    "APT_HONO": "0101",
                    "APT_NAME": "TEST_APT_001",
                    "CUST_NO": "TEST_CUST_001",
                    "SI_CUST_NO": "TEST_HOUSE_001",
                    "cntrMthdCd": "아파트(단일계약)",
                }
            ]
        }

    class Auth:
        async_protected_request = staticmethod(protected_request)

        def account_uid_hash(self) -> str:
            return "ACCOUNT_HASH"

    customers = await KepcoOnClient(cast("Any", Auth())).async_get_customers()

    assert customers[0].customer_number == "TEST_CUST_001"
    assert customers[0].house_contract_number == "TEST_HOUSE_001"


@pytest.mark.asyncio
async def test_client_get_bill_uses_latest_and_historical_bodies() -> None:
    calls: list[dict[str, object]] = []

    async def protected_request(
        path: str, payload: dict[str, object] | None, *, submission_id: str | None = None
    ) -> dict[str, object]:
        if path == "/my/memo/powerPlanner":
            assert payload is not None
            search = payload["dma_search"]
            assert isinstance(search, dict)
            assert search["custNo"] == "HOUSE"
            return {"dma_powerPlanner": {"RETURN_CD": "90"}}
        assert path == "/my/charge/pay/aptBillDetail"
        assert submission_id == "mf_wfm_layout_sbm_search"
        assert payload is not None
        calls.append(payload)
        return {
            "rsMsg": {"statusCode": "S"},
            "dma_result": {"DO_ERR_CODE": "HXI001", "DO_BILL_YM": "202608"},
        }

    class Auth:
        async_protected_request = staticmethod(protected_request)

        def account_uid_hash(self) -> str:
            return "ACCOUNT_HASH"

    customer = KepcoCustomer(
        stable_key="key",
        apartment_name="apt",
        dong="101",
        ho="1001",
        contract_method="아파트(단일계약)",
        is_supported=True,
        _customer_number="CUST",
        _house_contract_number="HOUSE",
    )
    client = KepcoOnClient(cast("Any", Auth()))

    latest = await client.async_get_bill(customer)
    history = await client.async_get_bill(customer, "202607")

    assert latest.bill_month == "202608"
    assert history.bill_month == "202607"
    assert calls == [
        {
            "dma_search": {
                "custNo": "CUST",
                "housCntrNo": "HOUSE",
                "yymm": "",
                "yyyymm": "",
                "searchType": "DETAIL",
            }
        },
        {
            "dma_search": {
                "custNo": "CUST",
                "housCntrNo": "HOUSE",
                "yymm": "202607",
                "yyyymm": "202607",
                "searchType": "DETAIL",
            }
        },
    ]


@pytest.mark.asyncio
async def test_client_rejects_bad_or_future_month_before_request() -> None:
    class Auth:
        requests = 0

        async def async_protected_request(
            self, path: str, payload: dict[str, object] | None, *, submission_id: str | None = None
        ) -> dict[str, object]:
            del path, payload, submission_id
            self.requests += 1
            return {}

        def account_uid_hash(self) -> str:
            return "ACCOUNT_HASH"

    auth = Auth()
    customer = KepcoCustomer("key", "apt", "101", "1001", "method", True, "CUST", "HOUSE")
    client = KepcoOnClient(cast("Any", auth), clock=lambda: datetime(2026, 9, 1, tzinfo=UTC))

    for month in ("2026-07", "202613", "202610"):
        with pytest.raises(KepcoOnProtocolError):
            await client.async_get_bill(customer, month)

    assert auth.requests == 0


@pytest.mark.asyncio
async def test_client_rejects_month_before_supported_range_before_request() -> None:
    class Auth:
        requests = 0

        async def async_protected_request(
            self, path: str, payload: dict[str, object] | None, *, submission_id: str | None = None
        ) -> dict[str, object]:
            del path, payload, submission_id
            self.requests += 1
            return {}

        def account_uid_hash(self) -> str:
            return "ACCOUNT_HASH"

    auth = Auth()
    customer = KepcoCustomer("key", "apt", "101", "1001", "method", True, "CUST", "HOUSE")
    client = KepcoOnClient(cast("Any", auth), clock=lambda: datetime(2026, 9, 1, tzinfo=UTC))

    with pytest.raises(KepcoOnProtocolError) as raised:
        await client.async_get_bill(customer, "199912")

    assert str(raised.value) == "month is outside supported range"
    assert auth.requests == 0


@pytest.mark.asyncio
async def test_client_accepts_current_home_assistant_local_month_at_utc_boundary() -> None:
    requests: list[dict[str, object]] = []

    class Auth:
        async def async_protected_request(
            self, path: str, payload: dict[str, object] | None, *, submission_id: str | None = None
        ) -> dict[str, object]:
            del path, submission_id
            assert payload is not None
            requests.append(payload)
            return {
                "rsMsg": {"statusCode": "S"},
                "dma_result": {"DO_ERR_CODE": "HXI001", "DO_BILL_YM": "202609"},
            }

        def account_uid_hash(self) -> str:
            return "ACCOUNT_HASH"

    customer = KepcoCustomer("key", "apt", "101", "1001", "method", True, "CUST", "HOUSE")
    client = KepcoOnClient(
        cast("Any", Auth()),
        clock=lambda: datetime(2026, 9, 1, 0, 30, tzinfo=timezone(timedelta(hours=9))),
    )

    bill = await client.async_get_bill(customer, "202609")

    assert bill.bill_month == "202609"
    assert requests[0]["dma_search"] == {
        "custNo": "CUST",
        "housCntrNo": "HOUSE",
        "yymm": "202609",
        "yyyymm": "202609",
        "searchType": "DETAIL",
    }


@pytest.mark.asyncio
async def test_client_get_all_current_bills_runs_sequentially() -> None:
    in_flight = 0
    max_in_flight = 0

    async def protected_request(
        path: str, payload: dict[str, object] | None, *, submission_id: str | None = None
    ) -> dict[str, object]:
        nonlocal in_flight, max_in_flight
        del path, payload, submission_id
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0)
        in_flight -= 1
        return {
            "rsMsg": {"statusCode": "S"},
            "dma_result": {"DO_ERR_CODE": "HXI001", "DO_BILL_YM": "202608"},
        }

    class Auth:
        async_protected_request = staticmethod(protected_request)

        def account_uid_hash(self) -> str:
            return "ACCOUNT_HASH"

    customers = (
        KepcoCustomer("key-1", "apt", "101", "1001", "method", True, "CUST1", "HOUSE1"),
        KepcoCustomer("key-2", "apt", "102", "1002", "method", True, "CUST2", "HOUSE2"),
    )

    bills = await KepcoOnClient(cast("Any", Auth())).async_get_all_current_bills(customers)

    assert list(bills) == ["key-1", "key-2"]
    assert max_in_flight == 1
