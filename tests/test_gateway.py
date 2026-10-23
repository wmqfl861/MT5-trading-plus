import json

import pytest

import mt5gw
from mt5gw.config import GatewayConfig
from mt5gw.gateway import MT5Gateway

from tests.fakes import FakeMT5


def test_package_exports_gateway():
    assert mt5gw.MT5Gateway is MT5Gateway


def test_connect_builds_services_sharing_one_connection():
    fake = FakeMT5()
    gw = MT5Gateway(mt5_module=fake)
    gw.connect()
    assert gw.connected
    conns = {
        id(svc._conn)
        for svc in (gw.symbols, gw.market, gw.account, gw.trading)
    }
    assert len(conns) == 1  # 四个服务共用同一连接（同一把锁）


def test_shutdown_disconnects():
    fake = FakeMT5()
    gw = MT5Gateway(mt5_module=fake)
    gw.connect()
    gw.shutdown()
    assert gw.connected is False


def test_context_manager_connects_and_shuts_down():
    fake = FakeMT5()
    with MT5Gateway(mt5_module=fake) as gw:
        assert gw.connected
    assert gw.connected is False


def test_from_config_file_builds_config(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"magic": 7, "allow_live_trading": True,
                             "expected_login": 42}), encoding="utf-8")
    gw = MT5Gateway.from_config_file(str(p), mt5_module=FakeMT5())
    assert gw.config.magic == 7
    assert gw.config.allow_live_trading is True


def test_default_config_is_safe():
    gw = MT5Gateway(mt5_module=FakeMT5())
    assert gw.config == GatewayConfig()
    assert gw.config.allow_live_trading is False
