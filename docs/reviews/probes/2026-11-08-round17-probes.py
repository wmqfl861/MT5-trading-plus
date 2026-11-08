"""第十七轮审核复现脚本（2026-11-08）。

只依赖 numpy / pandas。仓库根目录运行：

    python -I docs/reviews/probes/2026-11-08-round17-probes.py

本机缺 pyarrow / duckdb 时对这两个包打空桩（只为 import；被检查的代码
路径不读写 parquet、不连 duckdb）。

逐项输出 REPRODUCED / NOT-REPRODUCED（修好后应为 NOT-REPRODUCED）或 INFO：

- P17-1 D1 事件窗的时间口径：FRED close-only 行"时间=当日 00:00、开盘=
  当日收盘"，MT5 行"时间=当日 00:00、开盘≈前一日收盘"。事件窗按"锚日
  00:00 的 bar 开盘进、公告日 00:00 的 bar 开盘出"解析，在 MT5（OHLC）
  段量到的是公告日**前一天**的收益；引擎 A 的事件窗策略与之逐笔一致，
  所以 T151 的逐事件对账测试抓不到。
- P17-2 verdict_code_sha 只覆盖 validation/events.py、整份 bt.py 与日历，
  事件模式 V3 判决实际还调用 validation/lookahead.py（G0）、
  validation/robustness.py（成本 ×1.5 的 ScaledCosts）、
  validation/gates.py（evaluate_v3）与 costs/*（成本计算）。
- P17-3 下单前 account_info 为空 → 连接层抛 DataUnavailableError（不是
  NotConnectedError）→ 执行器落"回报未知"并重抛，运行器退出；而请求
  实际上确定没有发出。
- P17-4 风控进程把 DataUnavailableError 计入 5 轮上限：--interval 10 时
  约 50 秒账户取数失败即 rc=79 + 风控 halt；supervisor 只清自己写的 halt，
  这个 halt 要人工解除。
- P17-5 每日数据任务（本机 UTC+8 的 00:05–00:20）落在 UTC 16:05–16:20
  的纽约盘中，与交易三进程共用同一终端；11-05 风控 rc=75 的 terminal_info
  挂起与 00:15 refresh_ticks 的 copy_ticks_range 在同一分钟开始。
- P17-6 D68 的"5 年前向检验力"门槛对应的夏普：参照表。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import inspect
import json
import re
import sys
import tempfile
import types
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

RESULTS: list[tuple[str, str, str]] = []


def report(pid: str, status: str, detail: str) -> None:
    RESULTS.append((pid, status, detail))
    print(f"[{pid}] {status}: {detail}", flush=True)


def _stub_optional_modules() -> list[str]:
    stubbed = []
    for name in ("pyarrow", "duckdb"):
        if importlib.util.find_spec(name) is not None:
            continue
        subs = (name,) if name == "duckdb" else (
            name, f"{name}.parquet", f"{name}.dataset", f"{name}.compute")
        for sub in subs:
            sys.modules[sub] = types.ModuleType(sub)
            stubbed.append(sub)
        if name == "pyarrow":
            sys.modules["pyarrow"].parquet = sys.modules["pyarrow.parquet"]
    return stubbed


class ZeroCosts:
    def spread_p90(self, symbol, hour, *, price=None):
        return 0.0

    def slippage(self, symbol, hour, *, price=None):
        return 0.0

    def commission_per_lot(self, symbol):
        return 0.0

    def overnight_fee(self, *args, **kwargs):
        return 0.0

    def stop_overshoot(self, *args, **kwargs):
        return 0.0


# --------------------------------------------------------------------- P17-1

def _seam_series(fred_ann: str, mt5_ann: str) -> pd.DataFrame:
    """FRED close-only 段（O=H=L=C）接 MT5 OHLC 段（O≈前收）。

    公告日 D 的收盘对收盘涨 3%，锚日（D−1）当天持平；其余日子小幅噪声。
    """
    fred_days = pd.bdate_range("2021-05-03", "2021-07-01")
    mt5_days = pd.bdate_range("2021-07-02", "2021-09-30")
    days = fred_days.append(mt5_days)
    rng = np.random.default_rng(17)
    r = rng.normal(0.0, 0.0005, len(days))
    for ann in (fred_ann, mt5_ann):
        i = int(days.get_loc(pd.Timestamp(ann)))
        r[i] = 0.03          # 公告日 close/close = +3%
        r[i - 1] = 0.0       # 锚日 close/close 持平
    close = 14000.0 * np.exp(np.cumsum(r))
    is_mt5 = days >= mt5_days[0]
    prev_close = np.r_[close[0], close[:-1]]
    open_ = np.where(is_mt5, prev_close, close)      # MT5：开≈前收；FRED：开=收
    return pd.DataFrame({
        "time": days, "open": open_,
        "high": np.maximum(open_, close), "low": np.minimum(open_, close),
        "close": close, "tick_volume": 100, "spread": 0, "real_volume": 0,
    })


def probe_event_time_convention() -> None:
    from mt5quant.validation import events as ev

    fred_ann, mt5_ann = "2021-06-16", "2021-07-28"   # 两次真实 FOMC 公告日
    bars = _seam_series(fred_ann, mt5_ann)
    days = pd.DatetimeIndex(pd.to_datetime(bars["time"]).dt.normalize())
    meetings = [{"year": int(d[:4]), "label": f"{d} Meeting", "start_date": d,
                 "end_date": d, "kind": "meeting", "qualifier": None,
                 "source_page": "synthetic.htm", "announcement_date": d,
                 "in_lm_era": True, "in_default_event_set": True}
                for d in (fred_ann, mt5_ann)]
    doc = {"artifact": "round17-probe", "purpose": "synthetic",
           "event_definition": {"era_start": "1994-09-27",
                                "default_event_set": "kind=meeting with qualifier None",
                                "announcement_day_rule": "last calendar day"},
           "meetings": meetings}
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "fomc_probe.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        events, _ = ev.fomc_calendar_events(path, sha, days)
    frame = ev.event_returns(bars, events, side_fn=ev.declared_event_side("static_long"),
                             costs=ZeroCosts(), symbol="USTEC")
    got = {str(pd.Timestamp(row.exit_time).date()): float(row.gross)
           for row in frame.itertuples()}
    close = bars.set_index("time")["close"]

    def registered(ann):            # 登记窗：锚日收盘 → 公告日收盘
        i = int(days.get_loc(pd.Timestamp(ann)))
        return float(close.iloc[i] / close.iloc[i - 1] - 1.0)

    fred_ok = abs(got[fred_ann] - registered(fred_ann)) < 1e-9
    mt5_ok = abs(got[mt5_ann] - registered(mt5_ann)) < 1e-9

    # 引擎 A：注入事件窗的 T151 策略在同一序列上的成交
    from mt5quant.backtest.engine_a import run_backtest
    from mt5quant.strategies.library.fomc_preannouncement import PreFomcAnnouncementDrift
    cal = {"version": 1, "complete_through": str(days[-1].date()),
           "sessions": [{"day": str(d.date()), "close_time": f"{d.date()}T22:00:00"}
                        for d in days]}
    res = run_backtest(PreFomcAnnouncementDrift(event_windows=events, event_calendar=cal),
                       bars, ZeroCosts(), symbol="USTEC", params={},
                       contract_size=1.0, volume_lots=1.0, timeframe="D1")
    trades = res.trades
    eng = {}
    for _, tr in trades.iterrows():
        eng[str(pd.Timestamp(tr["exit_time"]).date())] = (
            float(tr["exit_price"]) / float(tr["entry_price"]) - 1.0
            if "exit_price" in tr and "entry_price" in tr else float("nan"))
    eng_mt5 = eng.get(mt5_ann)
    status = "REPRODUCED" if (fred_ok and not mt5_ok) else "NOT-REPRODUCED"
    report("P17-1", status,
           f"登记窗（锚日收→公告日收）收益：FRED 段 {registered(fred_ann):+.2%}、"
           f"MT5 段 {registered(mt5_ann):+.2%}；event_returns 量到 FRED 段 "
           f"{got[fred_ann]:+.2%}（一致={fred_ok}）、MT5 段 {got[mt5_ann]:+.2%}"
           f"（一致={mt5_ok}）；引擎 A 事件窗策略在 MT5 段该笔 "
           + (f"{eng_mt5:+.2%}" if eng_mt5 is not None else "无成交")
           + "——MT5 段量的是锚日当天，不是公告日")


# --------------------------------------------------------------------- P17-2

def probe_verdict_fingerprint(repo: Path, bt) -> None:
    src = inspect.getsource(bt._v3_gate_evaluate)
    fp_src = inspect.getsource(bt._verdict_code_sha)
    # 事件分支：从函数开头到 bar 级主检验的实际调用（vgate.run_primary_test(）之前
    # （docstring 里也出现 run_primary_test 字样，按调用形态切分）
    cut = src.find("vgate.run_primary_test(")
    event_part = src[:cut] if cut > 0 else src
    used = sorted(set(re.findall(r"\b(vlook|vrob|vgate|vstats|vwf)\.[A-Za-z_][A-Za-z0-9_]*", event_part)))
    used_calls = sorted(set(re.findall(r"\b(?:vlook|vrob|vgate|vstats|vwf)\.[A-Za-z_][A-Za-z0-9_]*", event_part)))
    alias = {"vlook": "validation/lookahead.py", "vrob": "validation/robustness.py",
             "vgate": "validation/gates.py", "vstats": "validation/stats.py",
             "vwf": "validation/walkforward.py"}
    covered = {"validation/events.py", "scripts/bt.py"}
    missing = sorted({alias[a] for a in used} - covered)
    # 指纹输入按函数体里 digest.update 的三段前缀判定（events.py: / bt.py: / calendar:）
    in_fp = [label for label, key in (("validation/events.py", '"\\nevents.py:'),
                                      ("scripts/bt.py", '"\\nbt.py:'),
                                      ("日历工件", '"\\ncalendar:'))
             if key in fp_src]
    status = "REPRODUCED" if missing else "NOT-REPRODUCED"
    report("P17-2", status,
           f"事件模式 V3 判决调用了 {', '.join(used_calls)}；指纹函数只引用 "
           f"{', '.join(in_fp)}；未进指纹的判决模块：{', '.join(missing)}，"
           "另有 costs/*（成本计算代码，cost_model_version 只描述配置）")


# --------------------------------------------------------------------- P17-3

def probe_presend_unavailable(repo: Path) -> None:
    from mt5gw.config import GatewayConfig
    from mt5gw.connection import MT5Connection
    from mt5gw.errors import DataUnavailableError, NotConnectedError

    class FakeMT5:
        def initialize(self, *a, **k):
            return True

        def terminal_info(self):
            return types.SimpleNamespace(connected=True)

        def account_info(self):
            return None                      # 终端瞬时取不到账户

        def last_error(self):
            return (1, "Success")

        def order_send(self, request):
            raise AssertionError("不应发出")

        def shutdown(self):
            return True

    conn = MT5Connection(GatewayConfig(), mt5_module=FakeMT5())
    raised = None
    try:
        conn.trade_send({"action": 1}, lambda mode: None)
    except Exception as exc:              # 记录实际异常类型
        raised = exc
    finally:
        try:
            conn.shutdown()
        except Exception:
            pass
    is_dua = isinstance(raised, DataUnavailableError)
    is_nce = isinstance(raised, NotConnectedError)
    exec_src = (repo / "src" / "mt5live" / "executor.py").read_text(encoding="utf-8")
    m = re.search(r"except NotConnectedError as exc:[\s\S]{0,2400}?except Exception as exc:"
                  r"[\s\S]{0,900}?\"unknown\"[\s\S]{0,300}?\n\s*raise\n", exec_src)
    unknown_reraise = m is not None
    handles_dua = re.search(r"except\s*\(?[^)\n]*DataUnavailableError", exec_src) is not None
    status = ("REPRODUCED" if (is_dua and not is_nce and unknown_reraise and not handles_dua)
              else "NOT-REPRODUCED")
    report("P17-3", status,
           f"account_info 为空时 trade_send 抛 {type(raised).__name__}（order_send 未调用）；"
           f"是 NotConnectedError={is_nce}；执行器下单路径的兜底分支记 unknown 后重抛="
           f"{unknown_reraise}；执行器单独处理 DataUnavailableError={handles_dua}"
           "——确定未发出的请求被当成'回报未知'，运行器退出")


# --------------------------------------------------------------------- P17-4

def probe_risk_data_unavailable_halt(repo: Path) -> None:
    from mt5gw.errors import DataUnavailableError
    from mt5live.monitor import LOOP_EXIT_RC, LoopFailureWatch

    rp_src = (repo / "scripts" / "risk_process.py").read_text(encoding="utf-8")
    recoverable_has_dua = re.search(r"_RECOVERABLE\s*=\s*\(\s*DataUnavailableError", rp_src) is not None
    interval = 10.0
    with tempfile.TemporaryDirectory() as td:
        watch = LoopFailureWatch(td, "risk")
        rounds = 0
        rc = None
        while rc is None and rounds < 100:
            rounds += 1
            watch.note_failure(DataUnavailableError("account_info 返回空"))
            if watch.expired():
                rc = watch.exit_code(exc=DataUnavailableError("account_info 返回空"))
        halt_path = Path(td) / "halt.json"
        owner = json.loads(halt_path.read_text(encoding="utf-8")).get("owner") if halt_path.exists() else None
    sup_src = (repo / "scripts" / "live_supervisor.py").read_text(encoding="utf-8")
    sup_owner_only = 'halt.get("owner") == "supervisor"' in sup_src
    status = ("REPRODUCED" if (recoverable_has_dua and rc == LOOP_EXIT_RC and owner == "risk"
                               and sup_owner_only) else "NOT-REPRODUCED")
    report("P17-4", status,
           f"DataUnavailableError 计入 5 轮上限={recoverable_has_dua}；{rounds} 轮"
           f"（--interval {interval:.0f} 约 {rounds * interval:.0f} 秒）后 rc={rc}，"
           f"halt.owner={owner!r}；supervisor 只清 owner=supervisor 的 halt={sup_owner_only}"
           "——约 50 秒账户取数失败即停交易，等人工解除")


# --------------------------------------------------------------------- P17-5

def probe_data_tasks_overlap(repo: Path) -> None:
    ps1 = (repo / "scripts" / "setup_scheduled_tasks.ps1").read_text(encoding="utf-8-sig")
    tasks = re.findall(r'New-DailyTask\s+"([a-z_]+)"\s+"(\d\d:\d\d)"', ps1)
    local_offset = 8                                   # 脚本注释：本机 UTC+8
    utc = []
    for name, hhmm in tasks:
        h, m = map(int, hhmm.split(":"))
        u = (h - local_offset) % 24
        utc.append((name, hhmm, f"{u:02d}:{m:02d}"))
    # XAUUSD：周一至周五 UTC 约 22:00(前日)–21:00 交易；16:xx UTC 一定在盘中
    in_session = [n for n, _, u in utc if 1 <= int(u[:2]) <= 20]
    # 11-05 取证（docs/ops/exit-forensics-2026-11-05.md §1）：
    ticks_end = datetime(2026, 11, 5, 16, 15, 49, 845085)
    ticks_start = ticks_end - timedelta(seconds=37.969)
    risk_end = datetime(2026, 11, 5, 16, 15, 49, 603988)
    risk_start = risk_end - timedelta(seconds=30.594)
    refresh_ticks_utc = next((u for n, _, u in utc if n == "refresh_ticks"), None)
    aligned = refresh_ticks_utc == "16:15" and ticks_start.strftime("%H:%M") == "16:15"
    status = "REPRODUCED" if (in_session and aligned) else "NOT-REPRODUCED"
    report("P17-5", status,
           "每日数据任务（本机→UTC）：" + "，".join(f"{n} {l}→{u}" for n, l, u in utc)
           + f"；落在交易时段的：{', '.join(in_session)}。11-05 refresh_ticks 的 "
           f"copy_ticks_range 起于 {ticks_start:%H:%M:%S}，风控 terminal_info 起于 "
           f"{risk_start:%H:%M:%S}（晚 {(risk_start - ticks_start).total_seconds():.1f} 秒），"
           "两者在 16:15:49 先后被 30 秒期限硬停")


# --------------------------------------------------------------------- P17-6

def probe_power_years() -> None:
    z_a, z_80 = 1.959964, 0.841621
    rows = []
    for sr in (0.3, 0.5, 0.7, 0.9, 1.2):
        y50 = (z_a / sr) ** 2
        y80 = ((z_a + z_80) / sr) ** 2
        rows.append(f"SR {sr:.1f}：50% 检验力 {y50:.1f} 年 / 80% {y80:.1f} 年")
    report("P17-6", "INFO",
           "单侧 α=0.025、按年化夏普的前向检验所需年数——" + "；".join(rows)
           + "。D68 的'≤5 年'门槛等价于要求年化夏普 ≥ 0.88")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".")
    args = ap.parse_args(argv)
    repo = Path(args.repo).resolve()
    sys.path.insert(0, str(repo / "src"))
    sys.path.insert(0, str(repo / "scripts"))
    stubbed = _stub_optional_modules()
    if stubbed:
        print(f"（本机缺包，已打空桩：{', '.join(stubbed)}）", flush=True)
    for fn in (probe_event_time_convention,):
        try:
            fn()
        except Exception as exc:
            report("P17-1", "INCONCLUSIVE", f"{type(exc).__name__}: {exc}")
    try:
        spec = importlib.util.spec_from_file_location("bt_probe17", repo / "scripts" / "bt.py")
        bt = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bt)
        probe_verdict_fingerprint(repo, bt)
    except Exception as exc:
        report("P17-2", "INCONCLUSIVE", f"{type(exc).__name__}: {exc}")
    for pid, fn in (("P17-3", probe_presend_unavailable),
                    ("P17-4", probe_risk_data_unavailable_halt),
                    ("P17-5", probe_data_tasks_overlap)):
        try:
            fn(repo)
        except Exception as exc:
            report(pid, "INCONCLUSIVE", f"{type(exc).__name__}: {exc}")
    probe_power_years()
    print("\n汇总：")
    for pid, status, _ in RESULTS:
        print(f"  {pid}: {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
