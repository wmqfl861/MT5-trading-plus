"""mt5live.riskproc（T14/P7）：独立风控进程——纯函数检查器 + 主循环周期。

全部用 fake 账户状态（mt5gw.models 的 frozen 对象），不连 MT5、不下单。
每条风控规则各一过一拒两例；文件级停机（killSwitch/halt.json）、心跳超时、
日亏触发 flatten 均落在 tmp_path 的 live 目录，不碰仓库 data/。
"""
from __future__ import annotations

import importlib.util
import json
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mt5gw.models import Deal, Position
from mt5data.sessions import SessionPolicy
from mt5live.riskproc import (
    OrderRequest,
    RiskConfig,
    RiskConfigError,
    RiskProcess,
    RiskState,
    check,
    daily_pnl,
    load_risk_config,
)
from mt5gw.gateway import MT5Gateway
from tests.fakes import FakeMT5

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "risk_process.py"

NOW = datetime(2026, 10, 26, 14, 30, 0)  # 周一 14:30 UTC，非禁止时段

# 禁入策略（T20/T35）：与 config/sessions.toml 同构——17:00 纽约，前 5 后 60，
# BTCUSD 豁免；EURUSD 扩展窗已删（T35：按纽约本地小时重测后不再需要）。
# check 的禁入判断唯一来源。
POLICY = SessionPolicy(
    rollover_local="17:00", rollover_tz="America/New_York",
    pre_minutes=5, post_minutes=60,
    exempt_symbols=frozenset({"BTCUSD"}),
)
M1 = 20261101  # magic：策略 1

CONTRACT = {"XAUUSD": 100.0, "EURUSD": 100_000.0, "BTCUSD": 1.0}
PRICES = {"XAUUSD": 2400.0, "EURUSD": 1.10, "BTCUSD": 50_000.0}


# ---- 构造工具 ----

def make_config(**overrides) -> RiskConfig:
    kw = dict(
        version=1,
        symbols=("XAUUSD", "EURUSD", "BTCUSD"),
        max_lots_per_order=0.5,
        max_positions_per_symbol=3,
        max_usd_exposure=200_000.0,
        max_orders_per_minute=3,
        daily_loss_cap_usd=500.0,
    )
    kw.update(overrides)
    return RiskConfig(**kw)


def make_state(**overrides) -> RiskState:
    kw = dict(
        positions=(),
        contract_sizes=dict(CONTRACT),
        prices=dict(PRICES),
        recent_order_times=(),
        today_realized=0.0,
        today_floating=0.0,
        halted=False,
    )
    kw.update(overrides)
    return RiskState(**kw)


def req(symbol="XAUUSD", volume=0.1, side="buy", magic=M1) -> OrderRequest:
    return OrderRequest(symbol=symbol, side=side, volume_lots=volume, magic=magic)


def pos(symbol, volume, *, side="buy", magic=M1, price_current=None,
        profit=0.0, swap=0.0, time=None) -> Position:
    price = price_current if price_current is not None else PRICES.get(symbol, 100.0)
    return Position(
        ticket=9001, symbol=symbol, side=side, volume=volume,
        price_open=price, price_current=price, sl=0.0, tp=0.0,
        swap=swap, profit=profit, time=time or NOW, comment="", magic=magic,
    )


def deal(*, entry="out", profit=0.0, commission=0.0, swap=0.0, fee=0.0,
         magic=M1, symbol="XAUUSD", volume=0.1, side="buy",
         time=None) -> Deal:
    return Deal(
        ticket=7001, order=7001, position_id=6001, symbol=symbol, side=side,
        entry=entry, volume=volume, price=100.0, profit=profit,
        commission=commission, swap=swap, fee=fee,
        time=time or NOW, comment="", magic=magic,
    )


# ---- 规则 1：品种白名单 ----

def test_whitelist_pass():
    d = check(req("XAUUSD"), make_state(), make_config(), NOW, POLICY)
    assert d.allowed, d.reason


def test_whitelist_reject():
    d = check(req("USTEC"), make_state(), make_config(), NOW, POLICY)
    assert not d.allowed
    assert "USTEC" in d.reason
    assert "白名单" in d.reason


# ---- 规则 2：单笔最大手数 ----

def test_max_lots_boundary_pass():
    d = check(req("XAUUSD", volume=0.5), make_state(), make_config(), NOW, POLICY)
    assert d.allowed, d.reason


def test_max_lots_reject():
    d = check(req("XAUUSD", volume=0.51), make_state(), make_config(), NOW, POLICY)
    assert not d.allowed
    assert "0.51" in d.reason


def test_nonpositive_volume_rejected():
    d = check(req("XAUUSD", volume=0.0), make_state(), make_config(), NOW, POLICY)
    assert not d.allowed
    d = check(req("XAUUSD", volume=-0.1), make_state(), make_config(), NOW, POLICY)
    assert not d.allowed


# ---- 规则 3：每品种最大持仓数 ----

def test_positions_per_symbol_pass():
    state = make_state(positions=(pos("XAUUSD", 0.1), pos("XAUUSD", 0.1)))
    d = check(req("XAUUSD"), state, make_config(), NOW, POLICY)  # 第 3 个，未超
    assert d.allowed, d.reason


def test_positions_per_symbol_reject():
    state = make_state(positions=(
        pos("XAUUSD", 0.1), pos("XAUUSD", 0.1), pos("XAUUSD", 0.1),
        pos("EURUSD", 0.1),  # 其他品种不计入
    ))
    d = check(req("XAUUSD"), state, make_config(), NOW, POLICY)  # 第 4 个 XAUUSD
    assert not d.allowed
    assert "XAUUSD" in d.reason and "3" in d.reason


# ---- 规则 4：组合美元暴露（XAUUSD 与 EURUSD 名义合并） ----

def test_usd_exposure_merged_pass():
    # XAUUSD 0.5×100×2400 = 120,000；EURUSD 0.5×100000×1.10 = 55,000 → 175,000
    state = make_state(positions=(
        pos("XAUUSD", 0.5), pos("EURUSD", 0.5),
    ))
    # 新单 XAUUSD 0.1 → +24,000 → 199,000 ≤ 200,000
    d = check(req("XAUUSD", volume=0.1), state, make_config(), NOW, POLICY)
    assert d.allowed, d.reason


def test_usd_exposure_merged_reject():
    state = make_state(positions=(
        pos("XAUUSD", 0.5), pos("EURUSD", 0.5),
    ))
    # 新单 XAUUSD 0.2 → +48,000 → 223,000 > 200,000
    d = check(req("XAUUSD", volume=0.2), state, make_config(), NOW, POLICY)
    assert not d.allowed
    assert "223,000" in d.reason or "223000" in d.reason.replace(",", "")


def test_usd_exposure_missing_price_rejects():
    prices = dict(PRICES)
    del prices["BTCUSD"]
    state = make_state(prices=prices)
    d = check(req("BTCUSD", volume=0.01), state, make_config(), NOW, POLICY)
    assert not d.allowed
    assert "BTCUSD" in d.reason and "price" in d.reason


# ---- 规则 5：每分钟最大下单数（60 秒滑窗） ----

def test_frequency_window_pass():
    state = make_state(recent_order_times=(NOW - timedelta(seconds=30),
                                           NOW - timedelta(seconds=10)))
    d = check(req("XAUUSD"), state, make_config(), NOW, POLICY)  # 1 分钟内第 3 笔
    assert d.allowed, d.reason


def test_frequency_window_reject():
    state = make_state(recent_order_times=(NOW - timedelta(seconds=59),
                                           NOW - timedelta(seconds=30),
                                           NOW - timedelta(seconds=10)))
    d = check(req("XAUUSD"), state, make_config(), NOW, POLICY)  # 第 4 笔
    assert not d.allowed
    assert "3" in d.reason and "1 分钟" in d.reason


def test_frequency_window_excludes_exactly_60s_old():
    # 恰好 60 秒前的单不计入（窗口为左开右闭的 60 秒）
    state = make_state(recent_order_times=(NOW - timedelta(seconds=60),
                                           NOW - timedelta(seconds=59),
                                           NOW - timedelta(seconds=10)))
    d = check(req("XAUUSD"), state, make_config(), NOW, POLICY)  # 计 2 笔 + 本笔 = 3
    assert d.allowed, d.reason


# ---- 规则 6：禁止开仓时段（T20：SessionPolicy，17:00 纽约 = 夏 21:00 / 冬 22:00 UTC） ----

@pytest.mark.parametrize("when", [
    datetime(2026, 10, 26, 20, 55, 0),   # 边界起点（含）：16:55 NY
    datetime(2026, 10, 26, 21, 0, 0),
    datetime(2026, 10, 26, 21, 30, 0),
    datetime(2026, 10, 26, 21, 59, 59),
])
def test_forbidden_session_reject_edt_week(when):
    d = check(req("XAUUSD"), make_state(), make_config(), when, POLICY)
    assert not d.allowed
    assert "forbidden_session" in d.reason and "禁止" in d.reason


@pytest.mark.parametrize("when", [
    datetime(2026, 10, 26, 20, 54, 59),  # 禁入窗开始前
    datetime(2026, 10, 26, 22, 0, 0),    # 边界终点（不含）：18:00 NY
])
def test_forbidden_session_pass_edt_week(when):
    d = check(req("XAUUSD"), make_state(), make_config(), when, POLICY)
    assert d.allowed, d.reason


def test_forbidden_session_dst_summer_2110_reject():
    # 夏令时（2026-07-15，EDT）：换日 = 21:00 UTC → 21:10 禁入
    d = check(req("XAUUSD"), make_state(), make_config(),
              datetime(2026, 7, 15, 21, 10, 0), POLICY)
    assert not d.allowed


def test_forbidden_session_dst_winter_2110_allowed_2210_reject():
    # 冬令时（2026-12-15，EST）：换日 = 22:00 UTC → 21:10 允许、22:10 禁入
    d_ok = check(req("XAUUSD"), make_state(), make_config(),
                 datetime(2026, 12, 15, 21, 10, 0), POLICY)
    assert d_ok.allowed, d_ok.reason
    d_no = check(req("XAUUSD"), make_state(), make_config(),
                 datetime(2026, 12, 15, 22, 10, 0), POLICY)
    assert not d_no.allowed


def test_btcusd_exempt_from_rollover_window():
    d = check(req("BTCUSD", volume=0.01), make_state(), make_config(),
              datetime(2026, 10, 26, 21, 30, 0), POLICY)
    assert d.allowed, d.reason


@pytest.mark.parametrize("when,rejected", [
    (datetime(2026, 10, 26, 22, 30, 0), False),   # 18:30 NY：扩展窗已删（T35/P11 反转），允许
    (datetime(2026, 10, 26, 21, 30, 0), True),    # 17:30 NY：换日窗内（17:00–18:00）
    (datetime(2026, 10, 27, 3, 0, 0), False),     # 23:00 NY：窗口早已结束
    (datetime(2026, 10, 26, 20, 0, 0), False),    # 16:00 NY：窗开始前
])
def test_eurusd_rollover_window_after_extension_removal(when, rejected):
    d = check(req("EURUSD"), make_state(), make_config(), when, POLICY)
    assert d.allowed == (not rejected), d.reason


def test_risk_process_uses_session_policy_for_forbidden_time(tmp_path):
    rp = make_process(tmp_path)
    d = rp.check_order(req("XAUUSD"), make_state(),
                       datetime(2026, 12, 15, 22, 10, 0))   # 冬令时换日后 10 分钟
    assert not d.allowed and "禁止" in d.reason
    d2 = rp.check_order(req("XAUUSD"), make_state(),
                        datetime(2026, 12, 15, 21, 10, 0))  # 冬令时 21:10 允许
    assert d2.allowed, d2.reason


def test_risk_process_default_policy_loads_repo_sessions_toml():
    # 不显式传策略 → RiskProcess 默认加载仓库 config/sessions.toml（唯一禁入定义源）
    rp = RiskProcess(make_config(), live_dir="data/live-never-used")
    assert rp.session_policy is not None
    assert not rp.session_policy.entry_allowed(
        "XAUUSD", datetime(2026, 7, 15, 21, 10))
    assert rp.session_policy.entry_allowed(
        "XAUUSD", datetime(2026, 12, 15, 21, 10))


# ---- 规则 7：日亏损上限（≤ −cap → flatten_all + halt） ----

def test_daily_loss_pass():
    state = make_state(today_realized=-300.0, today_floating=-150.0)  # -450
    d = check(req("XAUUSD"), state, make_config(), NOW, POLICY)
    assert d.allowed, d.reason


def test_daily_loss_reject_triggers_flatten_and_halt():
    state = make_state(today_realized=-400.0, today_floating=-100.0)  # 恰 -500
    d = check(req("XAUUSD"), state, make_config(), NOW, POLICY)
    assert not d.allowed
    assert d.flatten_all and d.halt
    assert "-500" in d.reason or "500" in d.reason


def test_daily_loss_deeper_reject():
    state = make_state(today_realized=-400.0, today_floating=-101.0)  # -501
    d = check(req("XAUUSD"), state, make_config(), NOW, POLICY)
    assert not d.allowed and d.flatten_all and d.halt


# ---- 规则 8：停机状态（halted） ----

def test_halted_state_rejects_everything():
    d = check(req("XAUUSD"), make_state(halted=True), make_config(), NOW, POLICY)
    assert not d.allowed
    assert d.halt and not d.flatten_all


# ---- 干净路径 ----

def test_clean_check_ok_reason():
    d = check(req("XAUUSD"), make_state(), make_config(), NOW, POLICY)
    assert d.allowed
    assert d.reason == "ok"


# ---- RiskConfig TOML 加载 ----

TOML_BODY = """version = 1
symbols = ["XAUUSD", "EURUSD", "BTCUSD"]
max_lots_per_order = 0.5
max_positions_per_symbol = 3
max_usd_exposure = 200000.0
max_orders_per_minute = 3
daily_loss_cap_usd = 500.0
"""


def write_toml(tmp_path, body=TOML_BODY) -> str:
    p = tmp_path / "risk.toml"
    p.write_text(body, encoding="utf-8")
    return str(p)


def test_load_risk_config_full(tmp_path):
    cfg = load_risk_config(write_toml(tmp_path))
    assert cfg.symbols == ("XAUUSD", "EURUSD", "BTCUSD")
    assert cfg.max_lots_per_order == 0.5
    assert cfg.max_usd_exposure == 200_000.0
    assert cfg.daily_loss_cap_usd == 500.0
    # T20：禁入时段已迁 config/sessions.toml（单一定义源），risk.toml 不再有该节


def test_load_risk_config_missing_key(tmp_path):
    body = TOML_BODY.replace("daily_loss_cap_usd = 500.0\n", "")
    with pytest.raises(RiskConfigError, match="daily_loss_cap_usd"):
        load_risk_config(write_toml(tmp_path, body))


def test_load_risk_config_unknown_key(tmp_path):
    body = TOML_BODY + "foo = 1\n"
    with pytest.raises(RiskConfigError, match="foo"):
        load_risk_config(write_toml(tmp_path, body))


def test_load_risk_config_rejects_legacy_forbidden_section(tmp_path):
    # 旧版 [[forbidden]] / crypto_symbols 已迁 sessions.toml——出现即拒绝（防两处定义）
    body = TOML_BODY + """
[[forbidden]]
symbol = "EURUSD"
start_hour_utc = 22
end_hour_utc = 23
"""
    with pytest.raises(RiskConfigError, match="forbidden"):
        load_risk_config(write_toml(tmp_path, body))
    body2 = TOML_BODY + 'crypto_symbols = ["BTCUSD"]\n'
    with pytest.raises(RiskConfigError, match="crypto_symbols"):
        load_risk_config(write_toml(tmp_path, body2))


def test_load_risk_config_missing_file(tmp_path):
    with pytest.raises(RiskConfigError, match="不存在"):
        load_risk_config(str(tmp_path / "nope.toml"))


def test_risk_config_validates_values():
    with pytest.raises(ValueError):
        make_config(max_usd_exposure=0.0)
    with pytest.raises(ValueError):
        make_config(symbols=())
    with pytest.raises(ValueError):
        make_config(max_orders_per_minute=0)


# ---- daily_pnl：已实现（排除出入金）+ 浮动 ----

def test_daily_pnl_excludes_balance_deals():
    deals = (
        deal(entry="in", commission=-2.0),
        deal(entry="out", profit=100.0, commission=-2.0, swap=-1.0, fee=-0.5),
        deal(entry=None, side="balance", profit=5000.0),  # 入金，绝不计入
        deal(entry="in", commission=-1.0, magic=20261102),
    )
    positions = (pos("XAUUSD", 0.1, profit=10.0, swap=-3.0),
                 pos("EURUSD", 0.1, profit=-5.0))
    realized, floating = daily_pnl(deals, positions)
    assert realized == pytest.approx(-2.0 + 96.5 - 1.0)
    assert floating == pytest.approx(7.0 - 5.0)


# ---- RiskProcess：文件级停机与主循环 ----

def make_process(tmp_path, **kw) -> RiskProcess:
    return RiskProcess(make_config(), live_dir=str(tmp_path / "live"),
                       heartbeat_timeout_s=120.0, session_policy=POLICY, **kw)


def write_heartbeat_file(live: Path, runner_alive: bool, ts: datetime):
    live.mkdir(parents=True, exist_ok=True)
    (live / "heartbeat.json").write_text(
        json.dumps({"runner_alive": runner_alive, "ts": ts.isoformat()}),
        encoding="utf-8")


def read_json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def test_kill_switch_halts_and_writes_halt_file(tmp_path):
    rp = make_process(tmp_path)
    live = tmp_path / "live"
    live.mkdir(parents=True)
    (live / "killSwitch").write_text("halt", encoding="utf-8")
    d = rp.check_order(req("XAUUSD"), make_state(), NOW)
    assert not d.allowed and d.halt
    halt = read_json(live / "halt.json")
    assert halt["reason"] == "kill_switch"
    assert (live / "alerts.log").exists()  # 风控触发即时告警


def test_existing_halt_file_blocks_orders(tmp_path):
    rp = make_process(tmp_path)
    live = tmp_path / "live"
    live.mkdir(parents=True)
    (live / "halt.json").write_text(
        json.dumps({"reason": "manual", "ts": NOW.isoformat()}), encoding="utf-8")
    d = rp.check_order(req("XAUUSD"), make_state(), NOW)
    assert not d.allowed and d.halt
    assert "manual" in d.reason
    # 已有 halt 不被覆盖（保留首次原因）
    assert read_json(live / "halt.json")["reason"] == "manual"


def test_check_order_delegates_to_pure_check_when_clean(tmp_path):
    rp = make_process(tmp_path)
    live = tmp_path / "live"
    live.mkdir(parents=True)
    d = rp.check_order(req("USTEC"), make_state(), NOW)  # 白名单外
    assert not d.allowed and "白名单" in d.reason


# ---- run_cycle：心跳超时 / 缺失 / 正常 / 干净停止 ----

def test_run_cycle_heartbeat_timeout_halts(tmp_path):
    rp = make_process(tmp_path)
    live = tmp_path / "live"
    write_heartbeat_file(live, True, NOW - timedelta(seconds=300))
    res = rp.run_cycle(None, NOW)
    assert not res.heartbeat_ok
    assert res.heartbeat_reason == "timeout"
    assert res.halt_written and res.halt_active
    hb = read_json(live / "heartbeat.json")
    assert hb["runner_alive"] is False
    assert read_json(live / "halt.json")["reason"] == "heartbeat_timeout"
    log = (live / "alerts.log").read_text(encoding="utf-8")
    assert "HEARTBEAT_TIMEOUT" in log


def test_run_cycle_heartbeat_missing_halts(tmp_path):
    rp = make_process(tmp_path)
    live = tmp_path / "live"
    live.mkdir(parents=True)  # 无 heartbeat.json → fail-closed
    res = rp.run_cycle(None, NOW)
    assert not res.heartbeat_ok
    assert res.heartbeat_reason == "missing"
    assert read_json(live / "halt.json")["reason"] == "heartbeat_missing"


def test_run_cycle_fresh_heartbeat_no_halt(tmp_path):
    rp = make_process(tmp_path)
    live = tmp_path / "live"
    write_heartbeat_file(live, True, NOW - timedelta(seconds=10))
    res = rp.run_cycle(None, NOW)
    assert res.heartbeat_ok and res.heartbeat_reason == "ok"
    assert not res.halt_written and not res.halt_active
    assert not (live / "halt.json").exists()
    assert read_json(live / "heartbeat.json")["runner_alive"] is True  # 不改写


def test_run_cycle_runner_stopped_cleanly_no_halt(tmp_path):
    rp = make_process(tmp_path)
    live = tmp_path / "live"
    write_heartbeat_file(live, False, NOW - timedelta(seconds=5))
    res = rp.run_cycle(None, NOW)
    assert not res.heartbeat_ok and res.heartbeat_reason == "runner_stopped"
    assert not (live / "halt.json").exists()


# ---- run_cycle：日亏触发 flatten 指令 ----

def snapshot(currency="USD", positions=(), deals=()):
    from mt5live.riskproc import AccountSnapshot
    return AccountSnapshot(currency=currency, positions=tuple(positions),
                           deals=tuple(deals))


def test_run_cycle_daily_loss_writes_flatten_command(tmp_path):
    rp = make_process(tmp_path)
    live = tmp_path / "live"
    write_heartbeat_file(live, True, NOW - timedelta(seconds=10))
    snap = snapshot(
        positions=(pos("XAUUSD", 0.1, profit=-450.0),),
        deals=(deal(entry="out", profit=-200.0),),
    )  # 已实现 -200 + 浮动 -450 = -650 ≤ -500
    res = rp.run_cycle(snap, NOW)
    assert res.daily_loss_breached and res.flatten_written
    cmd = read_json(live / "commands" / "flatten.json")
    assert cmd["command"] == "flatten_all"
    assert cmd["reason"] == "daily_loss_cap"
    assert "ts" in cmd
    assert read_json(live / "halt.json")["reason"] == "daily_loss_cap"
    log = (live / "alerts.log").read_text(encoding="utf-8")
    assert "RISK_FLATTEN" in log and "RISK_HALT" in log


def test_run_cycle_daily_loss_flatten_idempotent(tmp_path):
    rp = make_process(tmp_path)
    live = tmp_path / "live"
    write_heartbeat_file(live, True, NOW - timedelta(seconds=10))
    cmd_dir = live / "commands"
    cmd_dir.mkdir(parents=True)
    old = {"command": "flatten_all", "reason": "daily_loss_cap",
           "ts": "2026-10-26T08:00:00+00:00"}
    (cmd_dir / "flatten.json").write_text(json.dumps(old), encoding="utf-8")
    snap = snapshot(deals=(deal(entry="out", profit=-600.0),))
    res = rp.run_cycle(snap, NOW)
    assert res.daily_loss_breached
    assert not res.flatten_written  # 已有指令不重写（执行器改名/删除以示完成）
    assert read_json(cmd_dir / "flatten.json")["ts"] == old["ts"]


def test_run_cycle_healthy_account_no_action(tmp_path):
    rp = make_process(tmp_path)
    live = tmp_path / "live"
    write_heartbeat_file(live, True, NOW - timedelta(seconds=10))
    snap = snapshot(positions=(pos("XAUUSD", 0.1, profit=25.0),))
    res = rp.run_cycle(snap, NOW)
    assert res.heartbeat_ok and not res.halt_written
    assert not res.daily_loss_breached and not res.flatten_written
    assert not (live / "halt.json").exists()
    assert not (live / "commands" / "flatten.json").exists()


def test_run_cycle_kill_switch_writes_halt(tmp_path):
    rp = make_process(tmp_path)
    live = tmp_path / "live"
    write_heartbeat_file(live, True, NOW - timedelta(seconds=10))
    (live / "killSwitch").write_text("", encoding="utf-8")
    res = rp.run_cycle(None, NOW)
    assert res.halt_written
    assert read_json(live / "halt.json")["reason"] == "kill_switch"


def test_run_cycle_requires_usd_account(tmp_path):
    rp = make_process(tmp_path)
    (tmp_path / "live").mkdir(parents=True)
    with pytest.raises(ValueError, match="USD"):
        rp.run_cycle(snapshot(currency="EUR"), NOW)


# ---- T32（D21）：全平指令编号协议（mt5live.commands）----

from mt5live.commands import (  # noqa: E402
    CommandUnreadableError,
    archive_if_all_acked,
    read_flatten,
    write_ack,
    write_flatten,
)

M2 = 20261102  # 第二个 magic（ack 仲裁用）


def test_write_flatten_numbered_and_idempotent(tmp_path):
    """写指令带 uuid4 编号；已有未归档指令 → 返回它（首因优先，不覆盖）。"""
    live = tmp_path / "live"
    live.mkdir()
    c1 = write_flatten(str(live), reason="daily_loss_cap", now=NOW)
    assert c1.id and c1.reason == "daily_loss_cap" and c1.ts == NOW
    data = read_json(live / "commands" / "flatten.json")
    assert data["command"] == "flatten_all"
    assert data["id"] == c1.id and data["reason"] == "daily_loss_cap"
    # 再次写（原因不同）→ 返回既有指令，文件不动
    c2 = write_flatten(str(live), reason="another", now=NOW)
    assert c2 == c1
    assert read_json(live / "commands" / "flatten.json")["reason"] == \
        "daily_loss_cap"


def test_read_flatten_missing_none_unreadable_raises(tmp_path):
    """无文件 → None；文件损坏/缺字段（含旧格式无 id）→ CommandUnreadableError。"""
    live = tmp_path / "live"
    live.mkdir()
    assert read_flatten(str(live)) is None
    cmds = live / "commands"
    cmds.mkdir()
    (cmds / "flatten.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(CommandUnreadableError):
        read_flatten(str(live))
    # 旧格式（T24：无 id）按读不出处理——调用方按"有指令"处理
    (cmds / "flatten.json").write_text(json.dumps(
        {"command": "flatten_all", "reason": "x", "ts": NOW.isoformat()}),
        encoding="utf-8")
    with pytest.raises(CommandUnreadableError):
        read_flatten(str(live))


def test_archive_requires_all_acks_to_match_current_id(tmp_path):
    """归档条件：所有登记 magic 的 ack.id == 当前指令 id（旧 id 不算数）；
    归档后删除这些 ack。"""
    live = tmp_path / "live"
    live.mkdir()
    cmd = write_flatten(str(live), reason="r", now=NOW)
    # 空 magics → 无从确认，不归档
    assert archive_if_all_acked(str(live), set(), NOW) is False
    # 旧 id 的 ack 不触发归档
    write_ack(str(live), M1, "stale-id", closed=0, now=NOW)
    assert archive_if_all_acked(str(live), {M1, M2}, NOW) is False
    assert (live / "commands" / "flatten.json").exists()
    # M1 换成当前 id，M2 未 ack → 仍不归档
    write_ack(str(live), M1, cmd.id, closed=0, now=NOW)
    assert archive_if_all_acked(str(live), {M1, M2}, NOW) is False
    assert (live / "commands" / "flatten.json").exists()
    # 全部 ack 当前 id → 归档并删除 ack
    write_ack(str(live), M2, cmd.id, closed=1, now=NOW)
    assert archive_if_all_acked(str(live), {M1, M2}, NOW) is True
    assert not (live / "commands" / "flatten.json").exists()
    assert not (live / "commands" / f"flatten.ack-{M1}").exists()
    assert not (live / "commands" / f"flatten.ack-{M2}").exists()
    archived = list((live / "commands" / "archive").glob("flatten-*.json"))
    assert archived and cmd.id[:8] in archived[0].name


def test_riskproc_unreadable_flatten_not_archived_with_alert(tmp_path):
    """指令读不出 → 风控进程不归档（fail-closed）并写告警。"""
    rp = make_process(tmp_path)
    live = tmp_path / "live"
    cmds = live / "commands"
    cmds.mkdir(parents=True)
    (cmds / "flatten.json").write_text("oops", encoding="utf-8")
    write_heartbeat_file(live, True, NOW - timedelta(seconds=10))
    res = rp.run_cycle(None, NOW)
    assert not res.flatten_archived
    assert (cmds / "flatten.json").exists()
    assert "COMMAND_UNREADABLE" in (live / "alerts.log").read_text(
        encoding="utf-8")


# ---- scripts/risk_process.py CLI（fake 网关，不连 MT5） ----

AccRow = namedtuple(
    "AccRow",
    "login server company currency leverage balance equity margin margin_free "
    "margin_mode trade_mode",
)
DEMO_ACC = AccRow(
    login=123456, server="MetaQuotes-Demo", company="MetaQuotes Software Corp.",
    currency="USD", leverage=100, balance=10_000.0, equity=10_000.0,
    margin=0.0, margin_free=10_000.0, margin_mode=2, trade_mode=0,
)


def load_script():
    spec = importlib.util.spec_from_file_location("risk_process_script",
                                                  SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fake_gateway() -> MT5Gateway:
    fake = FakeMT5()
    fake.set_behavior("account_info", value=DEMO_ACC)
    # positions_get / history_deals_get 未配置 → None + last_error 成功 → 空集
    gw = MT5Gateway(mt5_module=fake)
    gw.connect()
    return gw


def test_script_once_heartbeat_timeout_writes_halt(tmp_path, monkeypatch):
    mod = load_script()
    live = tmp_path / "live"
    # 脚本用真实墙钟判超时，心跳须早于"现在"
    write_heartbeat_file(live, True,
                         datetime.now(timezone.utc) - timedelta(seconds=999))
    monkeypatch.setattr(mod, "_open_gateway", lambda cfg_path: fake_gateway())
    rc = mod.main(["--config", write_toml(tmp_path), "--data-dir",
                   str(tmp_path), "--once"])
    assert rc == 0
    assert (live / "halt.json").exists()
    assert json.loads((live / "heartbeat.json").read_text(
        encoding="utf-8"))["runner_alive"] is False


def test_script_once_healthy_no_halt(tmp_path, monkeypatch):
    mod = load_script()
    live = tmp_path / "live"
    write_heartbeat_file(live, True,
                         datetime.now(timezone.utc) - timedelta(seconds=5))
    monkeypatch.setattr(mod, "_open_gateway", lambda cfg_path: fake_gateway())
    rc = mod.main(["--config", write_toml(tmp_path), "--data-dir",
                   str(tmp_path), "--once"])
    assert rc == 0
    assert not (live / "halt.json").exists()


def test_script_daily_loss_writes_flatten(tmp_path, monkeypatch):
    mod = load_script()
    live = tmp_path / "live"
    write_heartbeat_file(live, True,
                         datetime.now(timezone.utc) - timedelta(seconds=5))
    fake = FakeMT5()
    fake.set_behavior("account_info", value=DEMO_ACC)
    PosRow = namedtuple(
        "PosRow",
        "ticket symbol type volume price_open price_current sl tp swap profit "
        "time comment magic",
    )
    fake.set_behavior("positions_get", value=(PosRow(
        9001, "XAUUSD", 0, 0.1, 2400.0, 1900.0, 0.0, 0.0, 0.0, -4500.0,
        1790000000, "", M1,
    ),))
    DealRow = namedtuple(
        "DealRow",
        "ticket order position_id symbol type entry volume price profit "
        "commission swap fee time comment magic",
    )
    fake.set_behavior("history_deals_get", value=(DealRow(
        7001, 7001, 6001, "XAUUSD", 0, 1, 0.1, 2400.0, -200.0,
        0.0, 0.0, 0.0, 1790000000, "", M1,
    ),))
    gw = MT5Gateway(mt5_module=fake)
    gw.connect()
    monkeypatch.setattr(mod, "_open_gateway", lambda cfg_path: gw)
    rc = mod.main(["--config", write_toml(tmp_path), "--data-dir",
                   str(tmp_path), "--once"])
    assert rc == 0
    cmd = live / "commands" / "flatten.json"
    assert cmd.exists()
    assert json.loads(cmd.read_text(encoding="utf-8"))["command"] == "flatten_all"


def test_script_missing_config_fails(tmp_path, monkeypatch, capsys):
    mod = load_script()
    monkeypatch.setattr(mod, "_open_gateway", lambda cfg_path: fake_gateway())
    rc = mod.main(["--config", str(tmp_path / "nope.toml"), "--data-dir",
                   str(tmp_path), "--once"])
    assert rc == 2
    assert "nope.toml" in capsys.readouterr().out
