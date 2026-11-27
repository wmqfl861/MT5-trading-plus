"""T191 守卫测试（第十八轮 §3.1.7）：运行时会写的全部告警类型必须都已定类。

提取方式与 docs/reviews/probes/2026-11-13-round18-probes.py 的 P18-3 相同
（静态解析 src/mt5live、src/mt5gw 与四个 scripts 进程脚本里的 ALERT_* 常量
与 append_alert 调用）。以后新增告警类型，必须同时在 demo_check.py 的
FORMAL_*_ALERTS 分类集合里定类，否则本测试失败。"""
from __future__ import annotations

import importlib.util
import re
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def checker_sets() -> dict[str, set[str]]:
    src = (ROOT / "scripts" / "demo_check.py").read_text(encoding="utf-8")
    out: dict[str, set[str]] = {}
    for m in re.finditer(r"^(FORMAL_[A-Z_]+_ALERTS)\s*=\s*frozenset\(\{(.*?)\}\)", src, re.S | re.M):
        out[m.group(1)] = set(re.findall(r'"([A-Z][A-Z0-9_]*)"', m.group(2)))
    assert {"FORMAL_ANOMALY_ALERTS", "FORMAL_DRILL_ALERTS", "FORMAL_RECORDED_ALERTS",
            "FORMAL_INVALID_ALERTS"} <= set(out), "demo_check.py 缺少 FORMAL_*_ALERTS 分类集合"
    return out


def runtime_alert_kinds() -> dict[str, set[str]]:
    files = sorted(ROOT.glob("src/mt5live/*.py")) + sorted(ROOT.glob("src/mt5gw/*.py")) + [
        ROOT / "scripts" / n for n in ("risk_process.py", "live_reconcile.py",
                                       "live_runner.py", "live_supervisor.py",
                                       "live_daily_report.py")]  # T213：日报任务也写告警
    consts: dict[str, str] = {}
    texts: dict[Path, str] = {}
    for p in files:
        if not p.exists():
            continue
        t = p.read_text(encoding="utf-8")
        texts[p] = t
        for m in re.finditer(r'^\s*(ALERT_[A-Z0-9_]+)\s*=\s*"([A-Z][A-Z0-9_]*)"', t, re.M):
            consts[m.group(1)] = m.group(2)
    kinds: dict[str, set[str]] = {}
    for p, t in texts.items():
        for m in re.finditer(r'append_alert\(\s*[^,]+,\s*(?:"([A-Z][A-Z0-9_]*)"|([A-Za-z_][A-Za-z0-9_.]*))', t):
            lit, name = m.group(1), m.group(2)
            kind = lit or consts.get((name or "").split(".")[-1])
            if kind:
                kinds.setdefault(kind, set()).add(p.name)
    # executor 的 block_alert 只取 ALERT_* 常量（已逐一核对均为异常类），不另列
    return kinds


def test_every_runtime_alert_kind_is_classified():
    sets = checker_sets()
    classified = set().union(*sets.values())
    kinds = runtime_alert_kinds()
    missing = sorted(k for k in kinds if k not in classified)
    assert kinds, "运行时告警类型提取为空——提取器失效，守卫失去意义"
    assert not missing, (
        f"运行时告警类型未在 demo_check.py 分类集合里定类：{missing}"
        "——新增告警类型必须同时定类（docs/handoff/2026-11-13-round18-plan.md §3.1）")


def test_classification_sets_are_pairwise_disjoint():
    sets = checker_sets()
    names = sorted(sets)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            assert not sets[a] & sets[b], f"{a} 与 {b} 重复定类：{sorted(sets[a] & sets[b])}"


# ---- T220：换日停报段缺口跳过（ROLLOVER_GAP_SKIP）——记录类，不判失败 ----

GAP_MESSAGE = ("runner-777 magic=777 换日停报段缺口跳过 magic=777：XAUUSD M15 "
               "收盘网格=2026-11-25T23:00:00 期望bar=2026-11-25T22:45:00"
               "（停报段内没有行情，这根 bar 不存在） 最新已收盘bar=2026-11-25T21:45:00 "
               "报价时刻=2026-11-25T23:02:11 停报段=[2026-11-25T22:00:00, "
               "2026-11-25T23:00:00)——只跳过这个网格点，不产生策略信号和策略订单；"
               "执行器照常轮询，本轮照常收尾")


def demo_check_module():
    spec = importlib.util.spec_from_file_location(
        "t220_demo_check_guard", ROOT / "scripts" / "demo_check.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_rollover_gap_skip_is_a_recorded_alert_written_by_the_runner():
    sets = checker_sets()
    assert "ROLLOVER_GAP_SKIP" in sets["FORMAL_RECORDED_ALERTS"]
    for name in ("FORMAL_ANOMALY_ALERTS", "FORMAL_DRILL_ALERTS",
                 "FORMAL_INVALID_ALERTS"):
        assert "ROLLOVER_GAP_SKIP" not in sets[name], name
    assert runtime_alert_kinds().get("ROLLOVER_GAP_SKIP") == {"live_runner.py"}


def test_rollover_gap_skip_is_counted_per_day_and_opens_no_degradation_segment():
    """预期中的缺口只逐日计数：不开降级段（跳过的网格点照常收尾、心跳照写），
    也不进"窗口内异常"。"""
    from mt5live.monitor import Alert
    mod = demo_check_module()
    assert mod._recorded_role("ROLLOVER_GAP_SKIP", GAP_MESSAGE) == ("recorded", None)
    events = [Alert(datetime(2026, 11, 25, 23, 2, 12), "ROLLOVER_GAP_SKIP", GAP_MESSAGE),
              Alert(datetime(2026, 11, 25, 23, 17, 12), "ROLLOVER_GAP_SKIP", GAP_MESSAGE),
              Alert(datetime(2026, 11, 26, 0, 2, 12), "ROLLOVER_GAP_SKIP", GAP_MESSAGE)]
    now = datetime(2026, 11, 26, 0, 30)
    segments, daily_events = mod._recorded_degradation(events, now)
    assert segments == []
    assert daily_events == {date(2026, 11, 25): 2, date(2026, 11, 26): 1}
    # 它不属于异常类，也不需要按消息或日历改判——本来就不在"窗口内异常"的筛选里
    assert not mod._alert_is_recorded_class(events[0])
    assert "ROLLOVER_GAP_SKIP" not in mod.FORMAL_ANOMALY_ALERTS


def test_rollover_gap_log_line_is_not_a_runner_log_anomaly(tmp_path):
    """运行器打的缺口日志带时间戳、不含正式检查当作日志异常的字样；同一条内容进
    alerts.log，正式检查读得出 magic、认得出种类。"""
    from types import SimpleNamespace
    from mt5live.monitor import read_alerts
    script = importlib.util.spec_from_file_location(
        "t220_live_runner_guard", ROOT / "scripts" / "live_runner.py")
    runner = importlib.util.module_from_spec(script)
    script.loader.exec_module(runner)
    now = datetime(2026, 11, 25, 23, 2, 12)
    line = f"[{now.isoformat()}] " + runner.note_rollover_gap(
        str(tmp_path), SimpleNamespace(
            symbol="XAUUSD", timeframe="M15", close_time=datetime(2026, 11, 25, 23, 0),
            expected_open=datetime(2026, 11, 25, 22, 45),
            latest_bar=datetime(2026, 11, 25, 21, 45), quote_time=now,
            window=(datetime(2026, 11, 25, 22, 0), datetime(2026, 11, 25, 23, 0))),
        magic=777, now=now)
    check = demo_check_module()
    assert check._formal_log_time(line) is not None
    assert not any(mark in line for mark in (
        "Traceback", "执行异常", "决策异常", "CRITICAL", "ERROR"))
    assert "决策 " not in line
    for text in ("magic=777", "XAUUSD M15", "收盘网格=2026-11-25T23:00:00",
                 "期望bar=2026-11-25T22:45:00", "最新已收盘bar=2026-11-25T21:45:00",
                 "报价时刻=", "停报段=[2026-11-25T22:00:00, 2026-11-25T23:00:00)"):
        assert text in line, text
    check._alert_magics(line, {777})            # 正式检查读得出 magic，且在登记的 magic 里
    alerts = read_alerts(str(tmp_path))
    assert [(a.kind, a.time) for a in alerts] == [("ROLLOVER_GAP_SKIP", now)]
    assert alerts[0].message == f"runner-777 {line.split('] ', 1)[1]}"
