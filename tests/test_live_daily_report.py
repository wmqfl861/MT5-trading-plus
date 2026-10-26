"""scripts/live_daily_report.py 日报自动化测试（G2/T46：阶段 F 演练链路）。

既有覆盖：tests/test_mt5live_integration.py::test_live_daily_report_script
只断言"日报生成 + magic/盈亏行"（FakeGateway 真账户形态）。本文件补齐
演练（paper）链路的关键行断言：

- 关键行：标题/账户/心跳（正常与异常）/分策略盈亏合计/告警计数/
  点差比对节（含"当日无点差样本"如实标注）；
- ``--paper-state``：账户/成交流水来自影子状态文件（PaperStateGateway
  聚合，不连真实终端——与 risk_process / live_reconcile 同口径）；
  状态不可用 → 退出码 2（fail-closed，不落半截日报）；
- ``--spread-model`` + ``--log-dir``：从运行器决策日志行
  ``[ts] 决策 <id>：... bid= ask=`` 采当日点差样本，经
  monitor.compare_spreads 按 (品种, 小时) 比对 SpreadModel p90——
  超标行/正常行/无模型行都如实渲染（蓝图 §7"按小时比对，超出的
  小时写进报告"由日报承担，demo_check.check_daily_report 据此验收）；
- 点差模型加载兼容 build_cost_model.py 的 v2 缓存包装（校验 sha 与
  bucket_tz 后取内层模型）与旧裸模型文件（R6/I5 同口径）。

小时口径：SpreadModel 分桶 = 纽约本地小时（BUCKET_TZ）；决策时刻换算
后填入 SpreadSample.hour_utc（字段名沿用 monitor）。测试日期 2026-11-02
（EST，UTC−5）：14:00Z → 小时 9、15:00Z → 小时 10。

绝不连真实 MT5、绝不下真实单：账户侧用 FakeGateway / PaperBroker。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from mt5live.monitor import append_alert
from mt5live.paper import PaperBroker
from mt5quant.costs.spread import SpreadModel, SpreadStat
from tests.fakes_live import FakeBroker, FakeGateway, QuoteMarket

REPO_ROOT = Path(__file__).resolve().parents[1]

MAGIC = 20261101
MAGIC_B = 20261102
SYMBOL = "XAUUSD"
DAY = date(2026, 11, 2)
DAY_ISO = "2026-11-02"


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(
        f"{name}_script", REPO_ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def write_magic_heartbeat(live_dir: str, magic: int, *, alive: bool = True,
                          ts: datetime) -> None:
    """写每策略心跳文件 heartbeat-<magic>.json（runner 落盘契约）。"""
    path = os.path.join(live_dir, f"heartbeat-{magic}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"runner_alive": alive,
                   "ts": ts.astimezone(timezone.utc).isoformat()}, f)


def make_broker_with_day_deals() -> FakeBroker:
    broker = FakeBroker()
    broker.seed_deal(profit=123.5, magic=MAGIC,
                     time=datetime(2026, 11, 2, 9, 0))
    broker.seed_deal(profit=-12.3, magic=MAGIC_B,
                     time=datetime(2026, 11, 2, 10, 30))
    return broker


def run_main(mod, tmp_path, *extra, gw=None) -> tuple[int, str]:
    (tmp_path / "data" / "live").mkdir(parents=True, exist_ok=True)
    reports = tmp_path / "reports"
    rc = mod.main(["--data-dir", str(tmp_path / "data"),
                   "--reports-dir", str(reports),
                   "--date", DAY_ISO, *extra], gw=gw)
    out = reports / "live" / f"daily-{DAY_ISO}.md"
    text = out.read_text(encoding="utf-8") if out.exists() else ""
    return rc, text


# ================================================================ 关键行

def test_report_key_lines_heartbeat_deals_alerts(tmp_path):
    """日报关键行：标题/账户/心跳正常/分策略盈亏/告警计数/点差比对节。"""
    mod = load_script("live_daily_report")
    live = tmp_path / "data" / "live"
    live.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    write_magic_heartbeat(str(live), MAGIC, ts=now)
    write_magic_heartbeat(str(live), MAGIC_B, ts=now)
    append_alert(str(live), "RISK_FLATTEN", "日亏全平", now=datetime(2026, 11, 2, 9, 30))
    append_alert(str(live), "RISK_HALT", "前一日告警", now=datetime(2026, 11, 1, 9, 0))

    rc, text = run_main(mod, tmp_path, gw=FakeGateway(make_broker_with_day_deals()))
    assert rc == 0
    out = tmp_path / "reports" / "live" / f"daily-{DAY_ISO}.md"
    assert out.exists()
    assert f"# 实盘日报 {DAY_ISO}" in text
    assert "- 生成时间：" in text
    assert "- 账户：Fake-Server login 123456" in text
    assert "- 心跳：正常" in text
    assert "## 分策略盈亏（按 magic 聚合当日 deals）" in text
    assert f"| {MAGIC} |" in text and "123.50" in text
    assert f"| {MAGIC_B} |" in text and "-12.30" in text
    assert "合计：2 笔" in text
    assert "## 实际点差 vs SpreadModel p90" in text
    assert "当日无点差样本" in text          # 未给 --spread-model：如实标注
    assert "- 当日告警：RISK_FLATTEN 1" in text
    assert "RISK_HALT" not in text           # 前一日告警不计入当日


def test_report_heartbeat_abnormal_line(tmp_path):
    """心跳超时 → 异常行（fail-closed 文案带原因与心跳文件名）。"""
    mod = load_script("live_daily_report")
    live = tmp_path / "data" / "live"
    live.mkdir(parents=True)
    write_magic_heartbeat(str(live), MAGIC,
                          ts=datetime.now(timezone.utc) - timedelta(seconds=300))
    rc, text = run_main(mod, tmp_path, gw=FakeGateway(FakeBroker()))
    assert rc == 0
    assert "timeout" in text and "**异常" in text
    assert f"heartbeat-{MAGIC}.json" in text   # 异常行点名心跳文件


# ================================================================ --paper-state

def test_report_reads_paper_state(tmp_path):
    """演练（paper）链路：账户与成交全部来自影子状态文件，不连真实终端。"""
    mod = load_script("live_daily_report")
    data = tmp_path / "data"
    paper_dir = data / "live" / "paper"
    paper_dir.mkdir(parents=True)
    t0 = datetime(2026, 11, 2, 10, 0)
    market = QuoteMarket()                     # 100.0 / 100.2
    paper = PaperBroker(market, state_path=str(paper_dir / f"state-{MAGIC}.json"),
                        contract_sizes={SYMBOL: 100.0}, magic=MAGIC,
                        clock=lambda: t0)
    opened = paper.market_order(SYMBOL, "buy", 0.1, magic=MAGIC)   # 买按 Ask 100.2
    market.set_quote(101.0, 101.2)
    paper.close_position(opened.order_ticket)                     # 平按 Bid 101.0
    write_magic_heartbeat(str(data / "live"), MAGIC,
                          ts=datetime.now(timezone.utc))

    rc, text = run_main(mod, tmp_path, "--paper-state", str(paper_dir))
    assert rc == 0
    assert "- 账户：paper-shadow login 0" in text
    assert "- 心跳：正常" in text
    assert f"| {MAGIC} |" in text and "+8.00" in text   # (101.0−100.2)×0.1×100
    assert "合计：2 笔" in text


def test_paper_state_unavailable_fails_loud(tmp_path, capsys):
    """影子状态不可用 → 退出码 2，不落日报（绝不静默按空账户）。"""
    mod = load_script("live_daily_report")
    data = tmp_path / "data"
    (data / "live").mkdir(parents=True)
    empty = data / "empty-paper"
    empty.mkdir()
    rc, text = run_main(mod, tmp_path, "--paper-state", str(empty))
    assert rc == 2
    assert text == ""                          # 没写半截日报
    assert "影子状态不可用" in capsys.readouterr().out


# ================================================================ 点差比对

def bare_model() -> SpreadModel:
    """XAUUSD 相对口径：小时 9/10 p90=0.0004（小时 15 也给统计——
    用于验证"别的 UTC 日"的样本被日期过滤排除）。"""
    stat = SpreadStat(median=0.0002, p90=0.0004, p99=0.0009, n=100)
    return SpreadModel(
        {SYMBOL: {9: stat, 10: stat, 15: stat}},
        {SYMBOL: 0.0002}, mode="relative")


RUNNER_LOG = (
    "mt5live 启动：XAUUSD M15 magic=20261101 strategy=demo lookback=30 [paper]\n"
    # 14:00Z(EST=小时9)：rel≈0.0004 → 正常
    "[2026-11-02T14:00:07.123456] 决策 XAUUSD|M15|2026-11-02T14:00:00："
    "signal=1 target=1 volume=0.1 bid=2400.0 ask=2400.96 atr=2 sl=None tp=None\n"
    # 15:00Z(小时10)：rel≈0.0007 → 超标（ratio≈1.75 > 1.5）
    "[2026-11-02T15:00:07.000000] 决策 XAUUSD|M15|2026-11-02T15:00:00："
    "signal=0 target=0 volume=None bid=2400.0 ask=2401.68 atr=2 sl=None tp=None\n"
    # 前一 UTC 日的样本：日期过滤后不得出现在报告里（若漏进会落小时 15）
    "[2026-11-01T20:00:07.000000] 决策 XAUUSD|M15|2026-11-01T20:00:00："
    "signal=0 target=0 volume=None bid=2400.0 ask=2404.0 atr=2 sl=None tp=None\n"
    # 无报价的决策行（bid=None）：跳过
    "[2026-11-02T16:00:07.000000] 决策 XAUUSD|M15|2026-11-02T16:00:00："
    "signal=0 target=0 volume=None bid=None ask=None atr=2 sl=None tp=None\n"
    # 品种不在模型：如实标"无模型"（不冒充超标）
    "[2026-11-02T17:00:07.000000] 决策 USTEC|M15|2026-11-02T17:00:00："
    "signal=1 target=1 volume=0.1 bid=18000.0 ask=18002.0 atr=2 sl=None tp=None\n"
)


def test_spread_rows_from_decision_logs(tmp_path):
    """--spread-model + --log-dir：决策日志 → SpreadSample → 按小时比对，
    超标/正常/无模型逐行渲染（蓝图 §7"按小时比对，超出的小时写进报告"）。"""
    mod = load_script("live_daily_report")
    log_dir = tmp_path / "demo"
    log_dir.mkdir()
    (log_dir / "runner-20261101-x.log").write_text(RUNNER_LOG, encoding="utf-8")
    model_path = tmp_path / "spread_model.json"
    bare_model().save(str(model_path))

    rc, text = run_main(mod, tmp_path, "--spread-model", str(model_path),
                        "--log-dir", str(log_dir), gw=FakeGateway(FakeBroker()))
    assert rc == 0
    assert "| XAUUSD | 9 |" in text and "正常" in text
    assert "| XAUUSD | 10 |" in text and "**超标**" in text
    assert "| USTEC |" in text and "无模型" in text
    assert "当日无点差样本" not in text
    assert "| XAUUSD | 15 |" not in text      # 前 UTC 日样本被日期过滤排除


def test_spread_model_cache_wrapper_v2(tmp_path):
    """v2 缓存包装（build_cost_model 产物）：校验 sha 后取内层模型比对。"""
    mod = load_script("live_daily_report")
    payload = bare_model().to_dict()
    sha = hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()
    wrapper = {"version": 2, "mode": "relative",
               "watermarks": {"XAUUSD": {"from": "2025-01-01"}},
               "model": payload, "model_sha256": sha,
               "built_at_utc": "2026-10-01T00:00:00+00:00"}
    model_path = tmp_path / "spread_model.json"
    model_path.write_text(json.dumps(wrapper), encoding="utf-8")
    log_dir = tmp_path / "demo"
    log_dir.mkdir()
    (log_dir / "runner.log").write_text(RUNNER_LOG, encoding="utf-8")

    rc, text = run_main(mod, tmp_path, "--spread-model", str(model_path),
                        "--log-dir", str(log_dir), gw=FakeGateway(FakeBroker()))
    assert rc == 0
    assert "| XAUUSD | 10 |" in text and "**超标**" in text   # 内层模型生效


def test_spread_model_bad_sha_fails_loud(tmp_path, capsys):
    """包装 sha 对不上（文件被动过）→ 退出码 2，不用坏模型出报告。"""
    mod = load_script("live_daily_report")
    wrapper = {"version": 2, "model": bare_model().to_dict(),
               "model_sha256": "0" * 64, "built_at_utc": "x"}
    model_path = tmp_path / "spread_model.json"
    model_path.write_text(json.dumps(wrapper), encoding="utf-8")
    rc, text = run_main(mod, tmp_path, "--spread-model", str(model_path),
                        gw=FakeGateway(FakeBroker()))
    assert rc == 2
    assert text == ""
    assert "model_sha256" in capsys.readouterr().out
