"""LiveConfig：TOML 装载与 fail-fast 校验（实盘配置拼错必须当场暴露）。"""
import dataclasses

import pytest

from mt5live.config import LiveConfig, load

FULL_TOML = """
symbol = "XAUUSD"
timeframe = "M15"
strategy = "mt5quant.strategies.library.buy_hold:BuyHold"
magic = 20261101
lookback_bars = 200
volume_lots = 0.01
deviation = 15
risk_check_enabled = false
data_root = "data/live1"
trigger_delay_s = 10.0
heartbeat_path = "data/live1/hb.json"

[params]
fast = 10
"""

MINIMAL_TOML = """
symbol = "EURUSD"
timeframe = "H1"
strategy = "mod:Cls"
magic = 777
lookback_bars = 50
risk_fraction = 0.005
"""


def write_toml(tmp_path, text):
    p = tmp_path / "live.toml"
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_load_full_toml(tmp_path):
    cfg = load(write_toml(tmp_path, FULL_TOML))
    assert cfg.symbol == "XAUUSD"
    assert cfg.timeframe == "M15"
    assert cfg.strategy == "mt5quant.strategies.library.buy_hold:BuyHold"
    assert cfg.magic == 20261101
    assert cfg.lookback_bars == 200
    assert cfg.volume_lots == 0.01
    assert cfg.deviation == 15
    assert cfg.risk_check_enabled is False   # 关掉风控检查但钉死了 volume_lots → 合法
    assert cfg.data_root == "data/live1"
    assert cfg.trigger_delay_s == 10.0
    assert cfg.heartbeat_path == "data/live1/hb.json"
    assert cfg.params == {"fast": 10}


def test_minimal_toml_gets_defaults(tmp_path):
    cfg = load(write_toml(tmp_path, MINIMAL_TOML))
    assert cfg.volume_lots is None            # 手数走 risk_fraction（按风险定）
    assert cfg.risk_fraction == 0.005
    assert cfg.deviation == 20
    assert cfg.risk_check_enabled is True
    assert cfg.data_root == "data"
    assert cfg.trigger_delay_s == 5.0
    assert cfg.heartbeat_path is None
    assert cfg.params == {}


def test_no_volume_source_rejected(tmp_path):
    # 手数来源必须二选一（T24/M2）：都不给 = 静默 1 手的爆仓来源
    text = MINIMAL_TOML.replace("risk_fraction = 0.005\n", "")
    with pytest.raises(ValueError, match="二选一"):
        load(write_toml(tmp_path, text))


def test_both_volume_sources_rejected(tmp_path):
    text = MINIMAL_TOML + "volume_lots = 0.01\n"
    with pytest.raises(ValueError, match="只能给一个"):
        load(write_toml(tmp_path, text))


@pytest.mark.parametrize("bad", ['risk_fraction = 0', 'risk_fraction = 0.5',
                                 'risk_fraction = -0.01'])
def test_bad_risk_fraction_rejected(tmp_path, bad):
    with pytest.raises(ValueError, match="risk_fraction"):
        load(write_toml(tmp_path, patched_toml(bad)))


def test_config_is_frozen(tmp_path):
    cfg = load(write_toml(tmp_path, MINIMAL_TOML))
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.magic = 1


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load(str(tmp_path / "nope.toml"))


def test_unknown_key_rejected(tmp_path):
    text = MINIMAL_TOML + 'devation = 30\n'   # 拼错：静默忽略等于没设
    with pytest.raises(ValueError, match="未知配置项.*devation"):
        load(write_toml(tmp_path, text))


def test_missing_required_key(tmp_path):
    text = MINIMAL_TOML.replace("magic = 777\n", "")
    with pytest.raises(ValueError, match="magic"):
        load(write_toml(tmp_path, text))


@pytest.mark.parametrize("tf", ["M7", "h1 ", "W1", "MN1"])
def test_bad_timeframe_rejected(tmp_path, tf):
    text = MINIMAL_TOML.replace('timeframe = "H1"', f'timeframe = "{tf}"')
    with pytest.raises(ValueError, match="timeframe"):
        load(write_toml(tmp_path, text))


def test_w1_rejected_because_grid_not_day_aligned(tmp_path):
    text = MINIMAL_TOML.replace('timeframe = "H1"', 'timeframe = "W1"')
    with pytest.raises(ValueError, match="对齐"):
        load(write_toml(tmp_path, text))


def patched_toml(bad: str) -> str:
    """在 MINIMAL_TOML 上覆盖同名键（避免 TOML 重复键先报错）。"""
    key = bad.split(" =")[0]
    lines = MINIMAL_TOML.strip().splitlines()
    kept = [ln for ln in lines if not ln.startswith(f"{key} =")]
    return "\n".join(kept + [bad]) + "\n"


@pytest.mark.parametrize("bad", [
    'magic = "777"',          # 字符串
    'magic = true',           # bool 是 int 子类，必须显式拒绝
    'lookback_bars = 0',
    'lookback_bars = true',
    'volume_lots = -0.1',
    'volume_lots = 0',
    'deviation = 1.5',        # 必须整数（点数）
    'risk_check_enabled = "true"',
    'trigger_delay_s = -1',
    'symbol = ""',
])
def test_bad_types_rejected(tmp_path, bad):
    key = bad.split(" =")[0]
    with pytest.raises(ValueError, match=key):
        load(write_toml(tmp_path, patched_toml(bad)))


def test_strategy_without_colon_rejected(tmp_path):
    text = MINIMAL_TOML.replace('strategy = "mod:Cls"', 'strategy = "BuyHold"')
    with pytest.raises(ValueError, match="strategy"):
        load(write_toml(tmp_path, text))


def test_risk_disabled_requires_explicit_volume(tmp_path):
    # 关掉风控检查又没钉死手数 = 无上限的自由下单，拒绝
    text = MINIMAL_TOML + "risk_check_enabled = false\n"
    with pytest.raises(ValueError, match="volume_lots"):
        load(write_toml(tmp_path, text))


def test_params_must_be_scalars(tmp_path):
    text = MINIMAL_TOML + "[params]\nfast = [1, 2]\n"
    with pytest.raises(ValueError, match="标量"):
        load(write_toml(tmp_path, text))
