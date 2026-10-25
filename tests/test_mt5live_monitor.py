"""mt5live.monitor（T14/P7）：心跳/即时告警/分策略盈亏聚合/点差比对/日报渲染。

全部内存与 tmp_path 数据，不连 MT5。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from mt5gw.models import AccountState, Deal
from mt5live.monitor import (
    MagicPnl,
    SpreadSample,
    aggregate_by_magic,
    append_alert,
    check_heartbeat,
    compare_spreads,
    read_alerts,
    read_heartbeat,
    render_daily_report,
    write_daily_report,
    write_heartbeat,
)
from mt5quant.costs.spread import SpreadModel, SpreadStat

NOW = datetime(2026, 10, 26, 23, 59, 0)
DAY = date(2026, 10, 26)
M1 = 20261101
M2 = 20261102


def deal(*, magic=M1, entry="out", profit=0.0, commission=0.0, swap=0.0,
         fee=0.0, volume=0.1, symbol="XAUUSD", side="buy",
         time=None) -> Deal:
    return Deal(
        ticket=7001, order=7001, position_id=6001, symbol=symbol, side=side,
        entry=entry, volume=volume, price=100.0, profit=profit,
        commission=commission, swap=swap, fee=fee,
        time=time or NOW, comment="", magic=magic,
    )


# ---- 告警日志 ----

def test_append_and_read_alerts(tmp_path):
    live = tmp_path / "live"
    append_alert(str(live), "HEARTBEAT_TIMEOUT", "runner 300 秒无心跳", NOW)
    append_alert(str(live), "RISK_HALT", "kill_switch", NOW + timedelta(seconds=1))
    alerts = read_alerts(str(live))
    assert len(alerts) == 2
    assert alerts[0].kind == "HEARTBEAT_TIMEOUT"
    assert alerts[0].time == NOW
    assert "runner 300" in alerts[0].message
    assert alerts[1].kind == "RISK_HALT"
    # 行格式：{iso}\t{kind}\t{message}
    line = (live / "alerts.log").read_text(encoding="utf-8").splitlines()[0]
    assert line.startswith(NOW.isoformat())
    assert "\tHEARTBEAT_TIMEOUT\t" in line


def test_read_alerts_missing_dir(tmp_path):
    assert read_alerts(str(tmp_path / "live")) == ()


# ---- 心跳 ----

def test_write_and_read_heartbeat(tmp_path):
    live = str(tmp_path / "live")
    ts = datetime(2026, 10, 26, 14, 30, 0, tzinfo=timezone.utc)
    write_heartbeat(live, runner_alive=True, ts=ts)
    hb = read_heartbeat(live)
    assert hb is not None
    assert hb.runner_alive is True
    assert hb.ts == datetime(2026, 10, 26, 14, 30, 0)  # 归一化为 naive UTC


def test_read_heartbeat_naive_ts_tolerated(tmp_path):
    live = tmp_path / "live"
    live.mkdir()
    (live / "heartbeat.json").write_text(
        '{"runner_alive": true, "ts": "2026-10-26T14:30:00"}',
        encoding="utf-8")
    hb = read_heartbeat(str(live))
    assert hb.ts == datetime(2026, 10, 26, 14, 30, 0)


def test_read_heartbeat_missing_returns_none(tmp_path):
    assert read_heartbeat(str(tmp_path / "live")) is None


def test_read_heartbeat_invalid_raises(tmp_path):
    live = tmp_path / "live"
    live.mkdir()
    (live / "heartbeat.json").write_text("{bad", encoding="utf-8")
    with pytest.raises(ValueError):
        read_heartbeat(str(live))


def test_check_heartbeat_states(tmp_path):
    live = str(tmp_path / "live")
    # 新鲜 → ok
    write_heartbeat(live, True, ts=datetime.now(timezone.utc))
    st = check_heartbeat(live, datetime.now(timezone.utc) + timedelta(seconds=1),
                         timeout_s=120.0)
    assert st.ok and st.reason == "ok"
    # 超时 → timeout
    write_heartbeat(live, True,
                    ts=datetime.now(timezone.utc) - timedelta(seconds=300))
    st = check_heartbeat(live, datetime.now(timezone.utc), timeout_s=120.0)
    assert not st.ok and st.reason == "timeout"
    assert st.age_s == pytest.approx(300.0, abs=2.0)
    # runner 主动报告停止 → runner_stopped
    write_heartbeat(live, False, ts=datetime.now(timezone.utc))
    st = check_heartbeat(live, datetime.now(timezone.utc), timeout_s=120.0)
    assert not st.ok and st.reason == "runner_stopped"
    # 文件缺失 → missing
    st = check_heartbeat(str(tmp_path / "empty"), datetime.now(timezone.utc),
                         timeout_s=120.0)
    assert not st.ok and st.reason == "missing"


# ---- 分策略盈亏（按 magic 聚合 deals，排除出入金） ----

def test_aggregate_by_magic():
    deals = (
        deal(magic=M1, entry="in", commission=-2.0),
        deal(magic=M1, entry="out", profit=100.0, commission=-2.0, swap=-1.0),
        deal(magic=M1, entry="out", profit=30.0, commission=-2.0),
        deal(magic=M2, entry="out", profit=-15.0, commission=-1.0),
        deal(magic=0, entry=None, side="balance", profit=5000.0),  # 入金排除
    )
    rows = aggregate_by_magic(deals, strategy_names={M1: "B1 通道突破"})
    assert rows == (
        MagicPnl(magic=M1, strategy="B1 通道突破", n_deals=3,
                 volume=pytest.approx(0.3), net_pnl=pytest.approx(123.0)),
        MagicPnl(magic=M2, strategy="", n_deals=1,
                 volume=pytest.approx(0.1), net_pnl=pytest.approx(-16.0)),
    )


def test_aggregate_by_magic_empty():
    assert aggregate_by_magic(()) == ()


# ---- 实际点差 vs SpreadModel p90 ----

def rel_model() -> SpreadModel:
    return SpreadModel(
        {"XAUUSD": {14: SpreadStat(median=0.0002, p90=0.0004, p99=0.0009,
                                   n=100)}},
        {"XAUUSD": 0.0002}, mode="relative",
    )


def test_compare_spreads_relative_breach():
    samples = (
        SpreadSample("XAUUSD", 14, spread_price=0.96, mid=2400.0),  # rel=0.0004
        SpreadSample("XAUUSD", 14, spread_price=1.68, mid=2400.0),  # rel=0.0007
    )
    rows = compare_spreads(samples, rel_model(), threshold_multiple=1.5)
    assert len(rows) == 1
    r = rows[0]
    assert (r.symbol, r.hour_utc, r.n) == ("XAUUSD", 14, 2)
    assert r.observed_max == pytest.approx(0.0007)
    assert r.model_p90 == pytest.approx(0.0004)
    assert r.ratio == pytest.approx(1.75)
    assert r.breach  # 1.75 > 1.5


def test_compare_spreads_relative_within_threshold():
    samples = (SpreadSample("XAUUSD", 14, spread_price=0.96, mid=2400.0),)
    rows = compare_spreads(samples, rel_model(), threshold_multiple=1.5)
    assert rows[0].ratio == pytest.approx(1.0)
    assert not rows[0].breach


def test_compare_spreads_absolute_mode():
    model = SpreadModel(
        {"EURUSD": {15: SpreadStat(median=0.0001, p90=0.0005, p99=0.001,
                                   n=50)}},
        {"EURUSD": 0.0001}, mode="absolute",
    )
    samples = (SpreadSample("EURUSD", 15, spread_price=0.0009, mid=1.10),)
    rows = compare_spreads(samples, model, threshold_multiple=1.5)
    assert rows[0].observed_max == pytest.approx(0.0009)  # 价格单位直接比
    assert rows[0].ratio == pytest.approx(1.8)
    assert rows[0].breach


def test_compare_spreads_symbol_not_in_model():
    samples = (SpreadSample("USTEC", 14, spread_price=2.0, mid=18000.0),)
    rows = compare_spreads(samples, rel_model())
    assert rows[0].model_p90 is None
    assert rows[0].ratio is None
    assert not rows[0].breach


# ---- 日报渲染 ----

def account() -> AccountState:
    return AccountState(
        login=123456, server="MetaQuotes-Demo",
        company="MetaQuotes Software Corp.", currency="USD", leverage=100,
        balance=10_000.0, equity=10_123.45, margin=100.0,
        margin_free=10_023.45, margin_mode=2, trade_mode=0,
    )


def test_render_daily_report_key_lines(tmp_path):
    live = str(tmp_path / "live")
    write_heartbeat(live, True, ts=datetime(2026, 10, 26, 23, 58, 0,
                                            tzinfo=timezone.utc))
    hb = check_heartbeat(live, NOW, timeout_s=120.0)
    magic_pnl = (MagicPnl(M1, "B1 通道突破", 6, 0.6, 123.45),
                 MagicPnl(M2, "", 2, 0.2, -12.30))
    spread_rows = compare_spreads(
        (SpreadSample("XAUUSD", 14, spread_price=1.68, mid=2400.0),),
        rel_model(), threshold_multiple=1.5)
    text = render_daily_report(
        DAY, generated_at=NOW, account=account(), heartbeat=hb,
        magic_pnl=magic_pnl, spread_rows=spread_rows,
        alert_counts=(("RECONCILE_MISMATCH", 1), ("HEARTBEAT_TIMEOUT", 2)),
    )
    assert "# 实盘日报 2026-10-26" in text
    assert "10,123.45" in text          # equity
    assert "分策略盈亏" in text
    assert "20261101" in text and "B1 通道突破" in text and "+123.45" in text
    assert "20261102" in text and "-12.30" in text
    assert "实际点差 vs SpreadModel p90" in text
    assert "XAUUSD" in text and "0.0004" in text
    assert "RECONCILE_MISMATCH" in text
    assert "信号一致性" in text and "占位" in text


def test_render_daily_report_empty_sections():
    text = render_daily_report(DAY, generated_at=NOW)
    assert "# 实盘日报 2026-10-26" in text
    assert "当日无成交" in text
    assert "当日无点差样本" in text
    assert "信号一致性" in text


def test_write_daily_report_path(tmp_path):
    text = render_daily_report(DAY, generated_at=NOW)
    p = write_daily_report(str(tmp_path / "reports"), DAY, text)
    assert p.endswith("daily-2026-10-26.md")
    with open(p, encoding="utf-8") as f:
        assert f.read() == text
