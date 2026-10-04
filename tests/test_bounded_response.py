"""Regression coverage for bounded reads of split HTTP response bodies."""

from __future__ import annotations

import asyncio
from typing import cast
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from aiohttp import ClientResponse
from custom_components.kepco_on import api
from custom_components.kepco_on.exceptions import KepcoOnProtocolError

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    ("chunks", "expected"),
    [
        ((b"",), b""),
        ((b"a", b"bc", b""), b"abc"),
        ((b"{", b'"ok":', b"true}", b""), b'{"ok":true}'),
    ],
)
async def test_split_response_reads_through_eof(chunks: tuple[bytes, ...], expected: bytes) -> None:
    response = MagicMock(spec=ClientResponse)
    response.content.read = AsyncMock(side_effect=chunks)
    result = await api._read_bounded_body(cast("ClientResponse", response))
    assert result == expected
    assert response.content.read.await_count == len(chunks)


async def test_exact_limit_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api, "MAX_RESPONSE_BYTES", 4)
    response = MagicMock(spec=ClientResponse)
    response.content.read = AsyncMock(side_effect=[b"ab", b"cd", b""])
    assert await api._read_bounded_body(cast("ClientResponse", response)) == b"abcd"
    assert response.content.read.await_args_list == [call(5), call(3), call(1)]


async def test_overflow_keeps_original_safe_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api, "MAX_RESPONSE_BYTES", 4)
    response = MagicMock(spec=ClientResponse)
    response.content.read = AsyncMock(side_effect=[b"abcd", b"e"])
    with pytest.raises(KepcoOnProtocolError, match="login bootstrap response was too large"):
        await api._read_bounded_body(
            cast("ClientResponse", response),
            error_reason="login bootstrap response was too large",
        )
    assert response.content.read.await_args_list == [call(5), call(1)]


@pytest.mark.parametrize("error_type", [TimeoutError, asyncio.CancelledError])
async def test_transport_failure_and_cancellation_propagate(
    error_type: type[BaseException],
) -> None:
    response = MagicMock(spec=ClientResponse)
    response.content.read = AsyncMock(side_effect=error_type)
    with pytest.raises(error_type):
        await api._read_bounded_body(cast("ClientResponse", response))
    response.content.read.assert_awaited_once()
