# SPDX-License-Identifier: ISC
"""In-process tests for :mod:`lora.cli`."""

from __future__ import annotations

import pytest

from lora.cli import (
    _import_bridge_meshcore,
    _import_bridge_serial,
    _import_daemon,
    _import_hwtest,
    _import_migrate_meshcore_json,
    _import_mon,
    _import_spectrum,
    _import_tx,
    _import_wav,
    _stub,
    main,
)


def test_stub_exits_2() -> None:
    with pytest.raises(SystemExit) as exc:
        _stub("test")(None)  # type: ignore[arg-type]
    assert exc.value.code == 2


def test_importers_return_callables() -> None:
    for name, imp in [
        ("mon", _import_mon),
        ("spectrum", _import_spectrum),
        ("wav", _import_wav),
        ("tx", _import_tx),
        ("bridge.meshcore", _import_bridge_meshcore),
        ("bridge.serial", _import_bridge_serial),
        ("migrate-json", _import_migrate_meshcore_json),
        ("hwtest", _import_hwtest),
        ("daemon", _import_daemon),
    ]:
        fn = imp()
        assert callable(fn), f"{name} importer did not return callable"


def test_main_passthrough_mon() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["mon", "--help"])
    assert exc.value.code == 0


def test_main_passthrough_daemon() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["daemon", "--help"])
    assert exc.value.code == 0


def test_main_passthrough_waterfall() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["waterfall", "--help"])
    assert exc.value.code == 0


def test_main_passthrough_spectrum() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["spectrum", "--help"])
    assert exc.value.code == 0


def test_main_passthrough_wav() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["wav", "--help"])
    assert exc.value.code == 0


def test_main_passthrough_tx() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["tx", "--help"])
    assert exc.value.code == 0


def test_main_passthrough_hwtest() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["hwtest", "--help"])
    assert exc.value.code == 0


def test_main_bridge_meshcore() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["bridge", "meshcore", "--help"])
    assert exc.value.code == 0


def test_main_bridge_serial() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["bridge", "serial", "--help"])
    assert exc.value.code == 0


def test_main_bridge_meshcore_migrate_json() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["bridge", "meshcore", "migrate-json", "--help"])
    assert exc.value.code == 0


def test_main_unknown_cmd() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["unknown"])
    assert exc.value.code != 0


def test_main_empty_argv() -> None:
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code != 0
