# SPDX-License-Identifier: ISC
"""Tests for :mod:`lora.bridges.serial` — argparse, main entrypoint."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


def test_main_help_exits_0() -> None:
    from lora.bridges.serial import main

    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0


def test_main_with_mocked_serial_and_server() -> None:
    from lora.bridges.serial import main

    mock_server = AsyncMock()
    mock_server.serve_forever = AsyncMock(side_effect=KeyboardInterrupt())

    with (
        patch("lora.bridges.serial.serial") as mock_serial,
        patch("lora.bridges.serial.asyncio.start_server", return_value=mock_server),
    ):
        mock_serial.Serial.return_value = mock_serial.Serial()

        rc = main(["--serial", "/dev/ttyUSB0"])
        assert rc == 0


def test_main_with_custom_port_mocked() -> None:
    from lora.bridges.serial import main

    mock_server = AsyncMock()
    mock_server.serve_forever = AsyncMock(side_effect=KeyboardInterrupt())

    with (
        patch("lora.bridges.serial.serial") as mock_serial,
        patch("lora.bridges.serial.asyncio.start_server", return_value=mock_server),
    ):
        mock_serial.Serial.return_value = mock_serial.Serial()

        rc = main(["--serial", "/dev/ttyUSB0", "--baud", "9600", "--tcp-port", "8000"])
        assert rc == 0
