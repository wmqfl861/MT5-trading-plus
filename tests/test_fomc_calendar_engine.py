"""FOMC 日历事件引擎适配（C005/H102/R3；A1 已批准轮次 R3 的机械前置）。

四个部件（TDD，先 RED 后 GREEN；全部合成数据——不读真实行情、不写
真实台账，台账一律 tmp_path 合成库）：

1. ``fomc_calendar_events`` 构造器（mt5quant.validation.events）：冻结
   工件 SHA256 绑定 fail-closed / 结构校验 / 默认事件集→公告日 D +
   前一主段交易日入场锚 / 缺公告 bar（dropped_no_bar）与缺锚
   （dropped_no_anchor）丢弃登记——不移窗、不补锚；
2. ``--event-kind fomc_calendar`` CLI 枚举 + ``--fomc-calendar`` /
   ``--fomc-calendar-sha256`` / ``--event-days-start`` /
   ``--event-days-end`` 必选成对（其他 kind 给出即拒绝，行为不变）；
3. 随机化抽样池限主段 [start, end)（E10：MT5/延长非盲段不得作对照
   入口）；month_end/london_fix 的 days 取值行为逐字节不变；
4. 事件策略身份类 ``PreFomcAnnouncementDrift``（event_kind/side_rule
   声明；T151/D66 重写为事件窗信号实现——不再继承 buy&hold 脚手架，
   逐事件对账见 tests/test_t151_event_identity.py）。
"""
import hashlib
import importlib.util
import json
from pathlib import Path

import pandas as pd
import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "bt.py"


def load_bt():
    spec = importlib.util.spec_from_file_location("bt_script_fomc", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- 合成工件

def meeting(end, *, default=True, kind="meeting", qualifier=None, era=True):
    """合成日历条目：默认集条目 announcement_date == end_date（与冻结
    工件 same-shape；非默认条目按 kind/qualifier 如实标 false）。"""
    return {"year": int(end[:4]), "label": f"{end} Meeting",
            "start_date": end, "end_date": end, "kind": kind,
            "qualifier": qualifier, "source_page": "synthetic.htm",
            "announcement_date": end, "in_lm_era": era,
            "in_default_event_set": default}


def write_calendar(tmp_path, meetings, *, era_start="1994-09-27",
                   name="fomc_test.json"):
    doc = {"artifact": "fomc-test", "purpose": "synthetic test calendar",
           "event_definition": {
               "era_start": era_start,
               "default_event_set": "kind=meeting with qualifier None",
               "announcement_day_rule": "last calendar day of the meeting"},
           "meetings": meetings}
    path = tmp_path / name
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


# ================================================== 部件 1：构造器（纯函数）

def test_constructor_sha_binding_fail_closed(tmp_path):
    from mt5quant.validation.events import fomc_calendar_events
    days = pd.date_range("2024-01-01", "2024-12-30", freq="D")
    path, sha = write_calendar(tmp_path, [meeting("2024-01-31")])
    with pytest.raises(ValueError, match="fomc"):
        fomc_calendar_events(tmp_path / "absent.json", sha, days)   # 工件缺失
    with pytest.raises(ValueError, match="sha256"):
        fomc_calendar_events(path, "0" * 64, days)                  # SHA 不符
    with pytest.raises(ValueError, match="sha256"):
        fomc_calendar_events(path, "not-a-digest", days)            # 期望值非 64hex


def test_constructor_structure_fail_closed(tmp_path):
    from mt5quant.validation.events import fomc_calendar_events
    days = pd.date_range("2024-01-01", "2024-12-30", freq="D")
    bad_era, era_sha = write_calendar(tmp_path, [meeting("2024-01-31")],
                                      era_start="1990-01-01", name="bad_era.json")
    with pytest.raises(ValueError, match="era_start"):
        fomc_calendar_events(bad_era, era_sha, days)                # 时代起点不符
    split = meeting("2024-03-20")
    split["announcement_date"] = "2024-03-19"                       # 默认条目公告≠末日
    bad_ann, ann_sha = write_calendar(tmp_path, [split], name="bad_ann.json")
    with pytest.raises(ValueError, match="announcement_date"):
        fomc_calendar_events(bad_ann, ann_sha, days)
    dup = [meeting("2024-03-20"), meeting("2024-03-20")]
    dup_path, dup_sha = write_calendar(tmp_path, dup, name="dup.json")
    with pytest.raises(ValueError, match="重复"):
        fomc_calendar_events(dup_path, dup_sha, days)               # 公告日重复


def test_constructor_windows_drops_and_no_shift(tmp_path):
    """默认事件集→公告日 D + 前一主段交易日锚；缺公告 bar/缺锚丢弃登记；
    窗外/非默认条目不触发；丢弃后不移窗不补锚（无邻近事件生成）。"""
    from mt5quant.validation.events import fomc_calendar_events
    days = pd.DatetimeIndex(pd.date_range("2024-01-01", "2024-12-30", freq="D"))
    days = days.delete(days.get_indexer([pd.Timestamp("2024-11-28")]))  # 公告日落洞
    meetings = [
        meeting("2024-01-01"),                        # 主段首日 → dropped_no_anchor
        meeting("2024-01-31"),                        # 正常
        meeting("2024-03-20"),                        # 正常
        meeting("2024-11-28"),                        # 无公告 bar → dropped_no_bar
        meeting("2024-11-29"),                        # 锚 = 11-27（跳过洞，不补邻日）
        meeting("2025-06-18"),                        # 窗外（主段日历 span 外）
        meeting("2024-02-14", default=False, kind="conference_call"),  # 非默认集
    ]
    path, sha = write_calendar(tmp_path, meetings)
    events, report = fomc_calendar_events(path, sha, days)
    assert [e.label for e in events] == [
        "fomc_calendar:2024-01-31", "fomc_calendar:2024-03-20",
        "fomc_calendar:2024-11-29"]
    assert all(e.kind == "fomc_calendar" for e in events)
    by_label = {e.label: e for e in events}
    assert by_label["fomc_calendar:2024-01-31"].entry_time == pd.Timestamp("2024-01-30")
    assert by_label["fomc_calendar:2024-01-31"].exit_time == pd.Timestamp("2024-01-31")
    assert by_label["fomc_calendar:2024-03-20"].entry_time == pd.Timestamp("2024-03-19")
    # 11-28 落洞：11-29 的入场锚是前一"主段交易日"11-27，不是前一日历日
    assert by_label["fomc_calendar:2024-11-29"].entry_time == pd.Timestamp("2024-11-27")
    assert report["default_event_set"] == 6
    assert report["window_events"] == 5               # 窗内默认事件（含两个将被丢弃的）
    assert report["events"] == 3
    assert report["dropped_no_bar"] == ["2024-11-28"]
    assert report["dropped_no_anchor"] == ["2024-01-01"]
    assert report["calendar_sha256"] == sha


def test_constructor_main_segment_filter(tmp_path):
    """主段窗由传入的主段交易日历决定：日历截至 2021-05-14 → 之后默认
    事件全部窗外（R3 主段 [首日, 2021-05-17) 同一语义）。"""
    from mt5quant.validation.events import fomc_calendar_events
    days = pd.bdate_range("2020-01-01", "2021-05-14")
    meetings = [meeting("2020-01-29"), meeting("2021-04-28"),
                meeting("2021-06-16"), meeting("2022-01-26")]
    path, sha = write_calendar(tmp_path, meetings)
    events, report = fomc_calendar_events(path, sha, days)
    assert [e.label for e in events] == ["fomc_calendar:2020-01-29",
                                         "fomc_calendar:2021-04-28"]
    assert report["window_events"] == 2
    assert report["dropped_no_bar"] == [] and report["dropped_no_anchor"] == []


# ================================================== 部件 2+3：CLI 与抽样池

def test_cli_enumerates_fomc_calendar_kind():
    mod = load_bt()
    for command in ("screen", "gate"):
        args = mod.build_parser().parse_args(
            [command, "--card", "H0", "--strategy", "x:Y",
             "--event-kind", "fomc_calendar"])
        assert args.event_kind == "fomc_calendar"
        assert args.fomc_calendar is None
        assert args.fomc_calendar_sha256 is None
        assert args.event_days_start is None and args.event_days_end is None


def _h_ctx(bars, calendar_days):
    from types import SimpleNamespace
    return {"declared": SimpleNamespace(signal_symbol=None,
                                        signal_timeframe=None,
                                        side_rule="static_long"),
            "calendars": {"X": {"version": 1,
                                "complete_through": str(calendar_days[-1].date()),
                                "sessions": [{"day": str(d.date()),
                                              "close_time": str(d + pd.Timedelta(hours=22))}
                                             for d in calendar_days]}},
            "signals": {"X": bars}, "timeframe": "D1"}


def test_h_event_setup_fomc_pool_limited_to_main_segment(tmp_path):
    """fomc_calendar：事件构造与随机化 days 都限主段 [start, end)；登记
    报告入 ctx；同 ctx 上 month_end 的 days 仍取全观察窗（行为不变）。"""
    mod = load_bt()
    cal_days = pd.date_range("2024-01-01", "2026-03-31", freq="D")
    times = pd.date_range("2024-01-01", "2026-03-09", freq="D")
    bars = pd.DataFrame({"time": times, "open": 100., "close": 100.})
    ctx = _h_ctx(bars, cal_days)
    path, sha = write_calendar(tmp_path, [meeting("2024-01-31"),
                                           meeting("2024-03-20"),
                                           meeting("2025-06-18")])
    fomc = (path, sha, pd.Timestamp("2024-01-01"), pd.Timestamp("2024-12-31"))
    events, days, side_fn = mod._h_event_setup(ctx, "X", bars,
                                               "fomc_calendar", {}, fomc=fomc)
    assert [e.label for e in events] == ["fomc_calendar:2024-01-31",
                                         "fomc_calendar:2024-03-20"]
    assert days.min() == pd.Timestamp("2024-01-01")
    assert days.max() == pd.Timestamp("2024-12-30")   # end 不含端点
    assert not (days >= pd.Timestamp("2024-12-31")).any()   # E10：窗外绝不做池
    assert side_fn(events[0]) == 1                     # static_long 恒 +1
    assert ctx["fomc_calendar"]["X"]["events"] == 2
    assert ctx["fomc_calendar"]["X"]["window_events"] == 2
    # 同一 ctx：month_end 的 days 仍为全观察窗（不因 fomc 参数改变）
    _, days_m, _ = mod._h_event_setup(ctx, "X", bars, "month_end", {})
    assert days_m.max() == pd.Timestamp("2026-03-09")


def test_h_event_setup_fomc_requires_binding(tmp_path):
    mod = load_bt()
    times = pd.date_range("2024-01-01", "2024-06-30", freq="D")
    bars = pd.DataFrame({"time": times, "open": 100., "close": 100.})
    ctx = _h_ctx(bars, times)
    with pytest.raises(ValueError, match="fomc"):
        mod._h_event_setup(ctx, "X", bars, "fomc_calendar", {})


# ------------------------------------------- CLI 端到端（关卡 V3 事件分支）

_CARD_H704 = """\
id = "H704"
version = 1
symbols = ["XAUUSD"]
timeframe = "D1"
session = "全时段"
phenomenon = "Pre-FOMC 公告前漂移"
mechanism = "央行会议日程信息溢价（event）"
should_fail_when = "公告前漂移消失"
falsified_if = "随机化参照主检验不满足"
graveyard_checked = []
round = "R1"
candidate = "C004"

[rules]
entry = "公告日前一主段交易日收盘做多"
exit = "公告日收盘平仓"
stop = "无"

[params]

[expected]
trades_per_year = "8"
win_rate = "未登记"
payoff_ratio = "不适用"
"""

_ROUND_R1_FOMC = """\
id = "R1"
opened = "2026-11-07"
candidates = ["C004"]
trial_budget = 40
lockbox_uses_max = 1
dsr_trial_scope = "rounds_lineage"
advance_rule = "事件模式候选按 [v3.event] 随机化参照 p<=alpha/N 且成本x1.5 净均值为正"
close_rule = "预算用尽或全部候选有结论即结束"
approved_by = "用户A"

[evaluation]
C004 = { mode = "event", primary = "randomization", side_rule = "static_long" }
"""


def _setup_fomc_round(tmp_path, monkeypatch, mod):
    """合成 D1 事件轮次：数据根（逐日一根 bar）+ 日历文件 + 候选/卡/轮次。"""
    from mt5data.store import _COLUMNS
    from mt5quant.research.ledger import Ledger
    from tests.test_research_candidates import CANDIDATE_TOML
    from tests.test_research_bt_script import patch_card_git

    root = tmp_path / "data"
    sdir = root / "bars" / "XAUUSD" / "D1"
    sdir.mkdir(parents=True)
    times = pd.date_range("2024-01-01", periods=800, freq="D")
    closes = 100.0 + pd.Series(range(800)) * 0.01
    df = pd.DataFrame({"time": times, "open": closes.tolist(),
                       "high": closes.tolist(), "low": closes.tolist(),
                       "close": closes.tolist(), "tick_volume": 1,
                       "spread": 0, "real_volume": 0})[_COLUMNS]
    df.to_parquet(sdir / "data.parquet")
    (root / "specs").mkdir()
    (root / "specs" / "latest.json").write_text(
        json.dumps({"XAUUSD": {"contract_size": 100.0, "volume_min": 0.01,
                               "volume_step": 0.01, "volume_max": 200.0}}),
        encoding="utf-8")
    cal_days = pd.date_range("2024-01-01", "2026-06-30", freq="D")
    caldir = root / "calendars"
    caldir.mkdir()
    (caldir / "XAUUSD.json").write_text(json.dumps({
        "version": 1, "complete_through": str(cal_days[-1].date()),
        "sessions": [{"day": str(d.date()),
                      "close_time": str(d + pd.Timedelta(hours=22))}
                     for d in cal_days]}), encoding="utf-8")
    cards_dir = tmp_path / "cards"
    cards_dir.mkdir(exist_ok=True)
    (cards_dir / "H704.toml").write_text(_CARD_H704, encoding="utf-8")
    cdir = tmp_path / "candidates"
    cdir.mkdir(exist_ok=True)
    body = CANDIDATE_TOML.replace("C001", "C004").replace("TESTUSD", "XAUUSD")
    body += '\nside_rule = "static_long"\n'
    (cdir / "C004.toml").write_text(body, encoding="utf-8")
    rdir = tmp_path / "rounds"
    rdir.mkdir(exist_ok=True)
    (rdir / "R1.toml").write_text(_ROUND_R1_FOMC, encoding="utf-8")
    ledger_path = tmp_path / "ledger.sqlite"
    with Ledger(ledger_path) as led:
        led.record_candidate_check(
            "C004", decision="pass",
            result={"candidate_id": "C004", "decision": "pass",
                    "checks": {"power": {"years": 799 / 365.25,
                                         "years_detail": {"XAUUSD/D1": 799 / 365.25}}}})
    patch_card_git(monkeypatch, mod)
    monkeypatch.setattr(mod.cards, "ensure_strategy_clean",
                        lambda ref, root_dir, **kw: None)
    monkeypatch.setattr(mod.cards, "strategy_fingerprint",
                        lambda ref, root_dir, **kw: "f" * 64)
    rc = mod.main(["round", "open", "R1", "--rounds-dir", str(rdir),
                   "--ledger", str(ledger_path)])
    assert rc == 0, "轮次开启前置未过（用具故障，非被测行为）"
    return root, cards_dir, ledger_path


def _fomc_gate_args(root, cards_dir, ledger_path, cal_path, sha):
    return ["gate", "--card", "H704", "--allow-unbounded-data",
            "--cards-dir", str(cards_dir), "--data-root", str(root),
            "--ledger", str(ledger_path),
            "--strategy", "mt5quant.strategies.library.fomc_preannouncement:"
                          "PreFomcAnnouncementDrift",
            "--costs", "zero", "--gates", "V3", "--params", "{}",
            "--event-kind", "fomc_calendar",
            "--fomc-calendar", str(cal_path), "--fomc-calendar-sha256", sha,
            "--event-days-start", "2024-01-01", "--event-days-end", "2024-12-31"]


def test_gate_fomc_calendar_end_to_end(tmp_path, monkeypatch, capsys):
    """端到端：fomc_calendar 事件分支全链路（构造器→抽样池限主段→
    随机化主检验→V3 details）；丢弃登记进覆盖报告。"""
    import mt5quant.validation.events as vevents
    from mt5quant.research.ledger import Ledger
    mod = load_bt()
    root, cards_dir, ledger_path = _setup_fomc_round(tmp_path, monkeypatch, mod)
    cal_path, sha = write_calendar(
        tmp_path, [meeting("2024-01-01"),    # 首日 → dropped_no_anchor（登记）
                   meeting("2024-01-31"), meeting("2024-03-20"),
                   meeting("2024-11-29"),    # 窗内可解析事件共 3 个
                   meeting("2025-06-18")])   # 窗外（主段 end 之后）
    pools = {}
    real = vevents.randomization_test

    def spy(bars, events, *, trading_days=None, **kw):
        pools["max_day"] = None if trading_days is None else max(trading_days)
        pools["n_events"] = len(events)
        return real(bars, events, trading_days=trading_days, **kw)

    monkeypatch.setattr(vevents, "randomization_test", spy)
    mod.main(_fomc_gate_args(root, cards_dir, ledger_path, cal_path, sha))
    out = capsys.readouterr().out
    assert pools["n_events"] == 3
    assert pools["max_day"] == pd.Timestamp("2024-12-30")   # 抽样池限主段
    assert "dropped_no_anchor" in out                        # 丢弃登记可见
    with Ledger(ledger_path) as led:
        assert led.conn.execute(
            "SELECT COUNT(*) FROM runs WHERE card_id='H704'").fetchone()[0] == 1
        row = led.conn.execute(
            "SELECT details_json FROM gates WHERE gate='V3'").fetchone()
    assert row is not None, out
    det = json.loads(row["details_json"])
    assert det["mode"] == "event"
    assert det["primary"]["event_kind"] == "fomc_calendar"
    assert det["primary"]["test"] == "randomization"
    assert det["primary"]["side_rule"] == "static_long"


def test_gate_fomc_missing_binding_rejected_before_runs(tmp_path, monkeypatch, capsys):
    """--event-kind fomc_calendar 缺四参之一 → 拒绝运行（无 runs）。"""
    from mt5quant.research.ledger import Ledger
    mod = load_bt()
    root, cards_dir, ledger_path = _setup_fomc_round(tmp_path, monkeypatch, mod)
    cal_path, sha = write_calendar(tmp_path, [meeting("2024-01-31")])
    full = _fomc_gate_args(root, cards_dir, ledger_path, cal_path, sha)
    for drop in ("--fomc-calendar", "--fomc-calendar-sha256",
                 "--event-days-start", "--event-days-end"):
        args = [a for a in full]
        i = args.index(drop)
        del args[i:i + 2]
        assert mod.main(args) == 1
        out = capsys.readouterr().out
        assert drop in out and "成对" in out
    with Ledger(ledger_path) as led:
        assert led.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_gate_fomc_args_rejected_for_other_kinds(tmp_path, monkeypatch, capsys):
    """其他事件 kind 携带 fomc 参数 → 拒绝（既有 kind 行为不受影响）。"""
    from mt5quant.research.ledger import Ledger
    mod = load_bt()
    root, cards_dir, ledger_path = _setup_fomc_round(tmp_path, monkeypatch, mod)
    cal_path, sha = write_calendar(tmp_path, [meeting("2024-01-31")])
    args = _fomc_gate_args(root, cards_dir, ledger_path, cal_path, sha)
    i = args.index("--event-kind")
    args[i + 1] = "month_end"
    assert mod.main(args) == 1
    out = capsys.readouterr().out
    assert "fomc_calendar" in out
    with Ledger(ledger_path) as led:
        assert led.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_gate_fomc_bad_sha_rejected_before_runs(tmp_path, monkeypatch, capsys):
    """SHA 不符 → fail-closed 拒绝（装配阶段，无 runs）。"""
    from mt5quant.research.ledger import Ledger
    mod = load_bt()
    root, cards_dir, ledger_path = _setup_fomc_round(tmp_path, monkeypatch, mod)
    cal_path, _sha = write_calendar(tmp_path, [meeting("2024-01-31")])
    args = _fomc_gate_args(root, cards_dir, ledger_path, cal_path, "0" * 64)
    assert mod.main(args) == 1
    out = capsys.readouterr().out
    assert "sha256" in out
    with Ledger(ledger_path) as led:
        assert led.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


# ================================================== 部件 4：事件策略身份类

def test_prefomc_strategy_identity_and_event_window_signals():
    """T151/D66 重写后：事件窗信号策略（不再继承 BuyHold 脚手架——
    H16-2 的被测对象）。身份声明不变；信号形状=事件窗（详见
    tests/test_t151_event_identity.py 的逐事件对账）。"""
    from mt5quant.strategies.library.fomc_preannouncement import (
        PreFomcAnnouncementDrift,
    )
    st = PreFomcAnnouncementDrift()
    assert st.event_kind == "fomc_calendar"       # _h_preflight 与 --event-kind 核对
    assert st.side_rule == "static_long"          # C005 [declared] 同一方向声明
    assert st.required_data.timeframe == "D1"
    assert st.required_data.symbols == ("USTEC",)
    bars = pd.DataFrame({"time": pd.date_range("2024-01-01", periods=3),
                         "close": [1., 1., 1.]})
    # 无注入日历 → 同序列日历近似（仅诊断，非恒多脚手架）：信号只可能
    # 落在"伪公告日（每 6 周一个周三）的前一交易日"（bdate 数据=周一）。
    sig = st.generate_signals(bars, {})
    dates = pd.to_datetime(bars["time"]).dt.normalize()
    expect = ((dates + pd.Timedelta(days=2) - pd.Timestamp("1970-01-05"))
              .dt.days // 7) % 6 == 0
    assert list(sig) == list((dates.dt.weekday == 0) & expect)


def test_library_exports_prefomc():
    from mt5quant.strategies import library
    assert library.PreFomcAnnouncementDrift.event_kind == "fomc_calendar"


# ================= F1/N1 修复（聚焦评审 docs/reviews/2026-11-07-fomc-engine-review.md）

def _registered_payload(sha, *, window_events=2, events=2,
                        dropped_no_bar=None, dropped_no_anchor=None,
                        default_event_set=3):
    """与登记侧 C005.precheck.json 同构的合成 precheck 载荷（F1 机械核对
    的登记期望源：precheck_source.calendar_sha256 + calendar_coverage）。"""
    return {"candidate_id": "C004", "decision": "pass",
            "checks": {"power": {"years": 799 / 365.25,
                                 "years_detail": {"XAUUSD/D1": 799 / 365.25}}},
            "precheck_source": {"stable_key": "N1:C004:main-only-real-precheck",
                                "calendar_sha256": sha},
            "calendar_coverage": {
                "era_start": "1994-09-27",
                "default_event_set": default_event_set,
                "window_events": window_events,
                "events_with_announcement_bar": window_events - len(dropped_no_bar or []),
                "events_with_entry_anchor": events,
                "dropped_no_bar": dropped_no_bar or [],
                "dropped_no_anchor": dropped_no_anchor or []}}


def test_fomc_registration_binding_unit(tmp_path):
    """F1 单元：登记载荷一致 → report 带 precheck_binding+pool；SHA 不符/
    窗口计数不符 → ValueError；无载荷 → no_registered_precheck（合成路径
    保持现状）。"""
    mod = load_bt()
    cal_days = pd.date_range("2024-01-01", "2026-03-31", freq="D")
    times = pd.date_range("2024-01-01", "2026-03-09", freq="D")
    bars = pd.DataFrame({"time": times, "open": 100., "close": 100.})
    path, sha = write_calendar(tmp_path, [meeting("2024-01-31"),
                                           meeting("2024-03-20"),
                                           meeting("2025-06-18")])
    fomc = (path, sha, pd.Timestamp("2024-01-01"), pd.Timestamp("2024-12-31"))
    # 无载荷：合成测试路径保持现状 + 标注
    ctx = _h_ctx(bars, cal_days)
    mod._h_event_setup(ctx, "X", bars, "fomc_calendar", {}, fomc=fomc)
    rep = ctx["fomc_calendar"]["X"]
    assert rep["precheck_binding"] == {"no_registered_precheck": True}
    assert rep["pool"] == ["2024-01-01", "2024-12-31"]
    # 一致载荷：比对通过（window_events=2/events=2/default=3 与构造器一致）
    ctx = _h_ctx(bars, cal_days)
    ctx["candidate_precheck"] = _registered_payload(sha)
    mod._h_event_setup(ctx, "X", bars, "fomc_calendar", {}, fomc=fomc)
    binding = ctx["fomc_calendar"]["X"]["precheck_binding"]
    assert binding["calendar_sha256_match"] is True
    assert binding["coverage_match"] is True
    assert "window_events" in binding["checked"]
    assert "dropped_no_anchor" in binding["checked"]
    # SHA 不符（自洽重抓日历，CLI=工件一致但 ≠ 登记绑定）→ ValueError
    ctx = _h_ctx(bars, cal_days)
    ctx["candidate_precheck"] = _registered_payload("b" * 64)
    with pytest.raises(ValueError, match="precheck_source"):
        mod._h_event_setup(ctx, "X", bars, "fomc_calendar", {}, fomc=fomc)
    # 窗口计数不符（登记 213/0/0 vs 运行偏离的同型）→ ValueError
    ctx = _h_ctx(bars, cal_days)
    ctx["candidate_precheck"] = _registered_payload(sha, window_events=3)
    with pytest.raises(ValueError, match="window_events"):
        mod._h_event_setup(ctx, "X", bars, "fomc_calendar", {}, fomc=fomc)


def test_gate_fomc_registered_binding_pass_and_details(tmp_path, monkeypatch, capsys):
    """F1 正例（CLI）：登记 precheck 载荷一致 → 完整运行通过，V3
    details_json 含 fomc_calendar 报告（SHA/池界/比对结果）——机器审计链。"""
    from mt5quant.research.ledger import Ledger
    mod = load_bt()
    root, cards_dir, ledger_path = _setup_fomc_round(tmp_path, monkeypatch, mod)
    cal_path, sha = write_calendar(
        tmp_path, [meeting("2024-01-01"), meeting("2024-01-31"),
                   meeting("2024-03-20"), meeting("2024-11-29"),
                   meeting("2025-06-18")])
    with Ledger(ledger_path) as led:      # 追加最新 precheck（装载器取 rows[-1]）
        led.record_candidate_check(
            "C004", decision="pass",
            result=_registered_payload(sha, window_events=4, events=3,
                                       default_event_set=5,
                                       dropped_no_anchor=["2024-01-01"]))
    mod.main(_fomc_gate_args(root, cards_dir, ledger_path, cal_path, sha))
    out = capsys.readouterr().out
    with Ledger(ledger_path) as led:
        assert led.conn.execute(
            "SELECT COUNT(*) FROM runs WHERE card_id='H704'").fetchone()[0] == 1
        row = led.conn.execute(
            "SELECT details_json FROM gates WHERE gate='V3'").fetchone()
    assert row is not None, out
    rep = json.loads(row["details_json"])["fomc_calendar"]["XAUUSD"]
    assert rep["calendar_sha256"] == sha
    assert rep["pool"] == ["2024-01-01", "2024-12-31"]
    assert rep["precheck_binding"]["calendar_sha256_match"] is True
    assert rep["precheck_binding"]["coverage_match"] is True
    assert rep["window_events"] == 4 and rep["dropped_no_anchor"] == ["2024-01-01"]


def test_gate_fomc_registered_mismatch_zero_runs(tmp_path, monkeypatch, capsys):
    """F1 反例（CLI）：登记 SHA 不符 / 窗口计数不符 → 装配段 ValueError，
    零 runs（自洽重抓日历不得无机器异议跑主检验）。"""
    from mt5quant.research.ledger import Ledger
    mod = load_bt()
    root, cards_dir, ledger_path = _setup_fomc_round(tmp_path, monkeypatch, mod)
    cal_path, sha = write_calendar(tmp_path, [meeting("2024-01-31")])
    cases = [
        (_registered_payload("b" * 64, window_events=1, events=1,
                             default_event_set=1), "precheck_source"),
        (_registered_payload(sha, window_events=9, events=1,
                             default_event_set=1), "window_events"),
    ]
    for payload, needle in cases:
        with Ledger(ledger_path) as led:
            led.record_candidate_check("C004", decision="pass", result=payload)
        assert mod.main(_fomc_gate_args(root, cards_dir, ledger_path,
                                        cal_path, sha)) == 1
        out = capsys.readouterr().out
        assert needle in out
        with Ledger(ledger_path) as led:
            assert led.conn.execute(
                "SELECT COUNT(*) FROM runs WHERE card_id='H704'"
            ).fetchone()[0] == 0


def test_fomc_binding_strict_dates():
    """N1：主段窗日期严格 YYYY-MM-DD——宽松格式（'2024-1-1'/本地语序/
    月日斜杠/带时间）与 tz-aware 串一律拒绝，不再落入宽松解析。"""
    from types import SimpleNamespace
    mod = load_bt()

    def binding(start, end):
        return mod._fomc_event_binding(SimpleNamespace(
            event_kind="fomc_calendar", fomc_calendar="p",
            fomc_calendar_sha256="a" * 64, event_days_start=start,
            event_days_end=end))

    assert binding("2024-01-01", "2024-12-31")[2:] == (
        pd.Timestamp("2024-01-01"), pd.Timestamp("2024-12-31"))
    for bad in ("2024-1-1", "Jan 1 2024", "01/02/2024", "2024-01-31T00:00:00",
                "2024-01-01 00:00", "2024-01-01Z", "2024-01-01+00:00"):
        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            binding(bad, "2024-12-31")
