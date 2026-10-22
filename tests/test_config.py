import json
from dataclasses import fields

import pytest

from mt5gw.config import (
    GatewayConfig,
    ensure_trade_allowed,
    load,
    LiveTradingBlockedError,
)

# account trade_mode: 0=DEMO, 1=CONTEST, 2=REAL (mt5.ACCOUNT_TRADE_MODE_*)


def test_defaults_are_safe():
    cfg = GatewayConfig()
    assert cfg.terminal_path == r"C:\Program Files\MetaTrader 5\terminal64.exe"
    assert cfg.allow_live_trading is False
    assert cfg.magic == 20261022


def test_defaults_cover_every_field():
    cfg = GatewayConfig()
    assert all(getattr(cfg, f.name) is not None for f in fields(cfg))


def test_load_missing_file_gives_defaults(tmp_path):
    cfg = load(str(tmp_path / "nope.json"))
    assert cfg == GatewayConfig()


def test_load_json_overrides(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"allow_live_trading": True, "magic": 42}), encoding="utf-8")
    cfg = load(str(p))
    assert cfg.allow_live_trading is True
    assert cfg.magic == 42
    assert cfg.terminal_path == GatewayConfig().terminal_path


def test_load_ignores_unknown_keys(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"nonsense": 1}), encoding="utf-8")
    assert load(str(p)) == GatewayConfig()


def test_load_rejects_non_bool_allow_live_trading(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"allow_live_trading": "false"}), encoding="utf-8")
    with pytest.raises(ValueError, match="allow_live_trading"):
        load(str(p))  # 字符串 "false" 为真值，会静默武装实盘——必须当场拒绝


def test_load_rejects_wrong_types(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"magic": "x", "terminal_path": 5}), encoding="utf-8")
    with pytest.raises(ValueError):
        load(str(p))


def test_fuse_blocks_real_account():
    with pytest.raises(LiveTradingBlockedError):
        ensure_trade_allowed(2, False)


def test_fuse_allows_real_when_unlocked():
    ensure_trade_allowed(2, True)  # must not raise


def test_fuse_allows_demo_and_contest():
    ensure_trade_allowed(0, False)
    ensure_trade_allowed(1, False)
