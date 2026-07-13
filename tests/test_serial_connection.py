"""Tests for local and network serial connection helpers."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.duofern.serial_connection import (
    check_serial_connection,
    create_serial_connection,
    validate_serial_location,
)


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("/dev/ttyUSB0", True),
        ("socket://192.168.1.20:2000", True),
        ("rfc2217://duofern.local:2000", True),
        ("socket://[2001:db8::1]:2000", True),
        ("socket://duofern.local", False),
        ("rfc2217://:2000", False),
        ("http://duofern.local:2000", False),
        ("socket://duofern.local:not-a-port", False),
        ("", False),
    ],
)
def test_validate_serial_location(location: str, expected: bool) -> None:
    assert validate_serial_location(location) is expected


def test_check_serial_connection_opens_pyserial_url() -> None:
    connection = MagicMock()
    with patch(
        "custom_components.duofern.serial_connection.serial.serial_for_url",
        return_value=connection,
    ) as serial_for_url:
        assert check_serial_connection("socket://duofern.local:2000") is True

    serial_for_url.assert_called_once()
    connection.close.assert_called_once_with()


@pytest.mark.asyncio
async def test_socket_connection_uses_fast_async_transport() -> None:
    expected = (MagicMock(), MagicMock())
    protocol_factory = MagicMock()
    loop = asyncio.get_running_loop()

    with patch(
        "custom_components.duofern.serial_connection."
        "serial_asyncio_fast.create_serial_connection",
        new=AsyncMock(return_value=expected),
    ) as fast_connection:
        result = await create_serial_connection(
            loop,
            protocol_factory,
            "socket://duofern.local:2000",
            baudrate=115200,
        )

    assert result == expected
    fast_connection.assert_awaited_once_with(
        loop,
        protocol_factory,
        "socket://duofern.local:2000",
        baudrate=115200,
    )


@pytest.mark.asyncio
async def test_rfc2217_connection_uses_threaded_transport() -> None:
    serial_instance = MagicMock()
    serial_instance.read.return_value = b""
    protocol = MagicMock(spec=asyncio.Protocol)
    loop = asyncio.get_running_loop()

    with patch(
        "custom_components.duofern.serial_connection.serial.serial_for_url",
        return_value=serial_instance,
    ) as serial_for_url:
        transport, result_protocol = await create_serial_connection(
            loop,
            lambda: protocol,
            "rfc2217://duofern.local:2000",
            baudrate=115200,
        )
        await asyncio.sleep(0)
        transport.close()
        await asyncio.sleep(0.05)

    assert result_protocol is protocol
    serial_for_url.assert_called_once()
    serial_instance.close.assert_called_once_with()
    protocol.connection_made.assert_called_once_with(transport)
    protocol.connection_lost.assert_called_once_with(None)
