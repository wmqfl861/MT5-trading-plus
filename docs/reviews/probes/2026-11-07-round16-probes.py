"""第十六轮审核复现脚本（2026-11-07）。

只依赖 numpy / pandas。仓库根目录运行：

    python -I docs/reviews/probes/2026-11-07-round16-probes.py

本机缺 pyarrow / duckdb 时，脚本对这两个包打空桩（只为让 scripts/bt.py
能被 import；被检查的代码路径不读写 parquet、不连 duckdb）。装了真包的
环境不打桩。

逐项输出 REPRODUCED / NOT-REPRODUCED（修好后应为 NOT-REPRODUCED）或 INFO：

- P16-1 事件模式卡（H102，R3 [evaluation] mode=event）的 G5 编排走 bar 级
  策略类：在锁箱窗上回测的是 BuyHold 脚手架，不是事件收益。
- P16-2 C005 的策略类 PreFomcAnnouncementDrift 就是 BuyHold：引擎 A 上
  10 年只有 1 笔交易、持仓时间≈100%；同期事件定义应有约 80 笔 1 日持仓。
- P16-3 实盘运行器遇到确定态拒单（例如 10021 No prices）整进程退出：
  RETCode 没有 10021，执行器重抛 TradeRejectedError，运行器主循环重抛。
- P16-4 P 线 USDJPY 早年"H1"每天只有一根：按根数定义的窗口（S3=480 根、
  特征预热 2000 根、持有 24 根）在这些年份被拉长为天；按审核记录里的
  计数估算真 H1 日数。
- P16-5 随机化检验吞吐参照：同形合成数据（约 1 万根 D1、213 个事件）上
  每次抽取的耗时（R3 run5 实测约 9.4 秒/次，1999 次约 6 小时）。
- P16-6 8 个事件的锁箱（约 1 年 FOMC）对"期望 > 0"类判据的区分力（蒙特卡洛）。
"""
from __future__ import annotations

import argparse
import importlib.util
import inspect
import re
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

RESULTS: list[tuple[str, str, str]] = []


def report(pid: str, status: str, detail: str) -> None:
    RESULTS.append((pid, status, detail))
    print(f"[{pid}] {status}: {detail}", flush=True)


def _stub_optional_modules() -> list[str]:
    """pandas 先 import（让它判定 pyarrow 不可用），再给缺失的包打空桩。"""
    stubbed = []
    for name in ("pyarrow", "duckdb"):
        if importlib.util.find_spec(name) is not None:
            continue
        for sub in (name, f"{name}.parquet", f"{name}.dataset", f"{name}.compute"):
            if sub.startswith("duckdb.") and sub != "duckdb":
                continue
            sys.modules[sub] = types.ModuleType(sub)
            stubbed.append(sub)
        if name == "pyarrow":
            sys.modules["pyarrow"].parquet = sys.modules["pyarrow.parquet"]
    return stubbed


def _load_bt(repo: Path):
    spec = importlib.util.spec_from_file_location("bt_probe", repo / "scripts" / "bt.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FlatRelCosts:
    """相对点差假成本（≈USTEC 0.67bp 半幅），零佣金，零隔夜费。"""

    def spread_p90(self, symbol, hour, *, price=None):
        return 6.7e-5 * float(price if price is not None else 1.0)

    def slippage(self, symbol, hour, *, price=None):
        return 6.7e-6 * float(price if price is not None else 1.0)

    def commission_per_lot(self, symbol):
        return 0.0

    def overnight_fee(self, symbol, side, from_ts, to_ts, *, price=None):
        return 0.0

    def stop_overshoot(self, *args, **kwargs):
        return 0.0


def _daily_bars(start: str, end: str, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    days = pd.bdate_range(start, end)
    close = 15000.0 * np.exp(np.cumsum(rng.normal(0.0004, 0.012, len(days))))
    open_ = np.r_[close[0], close[:-1]]
    return pd.DataFrame({
        "time": days, "open": open_,
        "high": np.maximum(open_, close) * 1.004,
        "low": np.minimum(open_, close) * 0.996,
        "close": close, "tick_volume": 1000, "spread": 0, "real_volume": 0,
    })


def _time_in_market(trades: pd.DataFrame, bars: pd.DataFrame) -> float:
    if trades is None or len(trades) == 0:
        return 0.0
    t = pd.to_datetime(bars["time"])
    held = np.zeros(len(t), dtype=bool)
    for _, tr in trades.iterrows():
        held |= (t >= pd.Timestamp(tr["entry_time"])).to_numpy() & (
            t < pd.Timestamp(tr["exit_time"])).to_numpy()
    return float(held.mean())


# --------------------------------------------------------------------- P16-1/2

def probe_g5_event_mode(repo: Path, bt) -> None:
    from mt5quant.research.cards import load_card
    from mt5quant.strategies.library.fomc_preannouncement import PreFomcAnnouncementDrift
    import mt5data.lockbox as lockbox_mod

    card = load_card(repo / "research" / "cards" / "H102.toml")
    try:
        mode = bt._round_eval_mode(card)
    except Exception as exc:  # 轮次文件读不到时如实报告
        mode = f"<error {type(exc).__name__}: {exc}>"

    research = _daily_bars("2015-01-02", "2024-12-31", seed=11)
    cutoff = pd.Timestamp("2025-01-02")
    lockbox = _daily_bars("2024-12-31", "2025-12-31", seed=12)
    captured: dict = {}

    saved = {
        "ref": bt._g3_reference_returns, "sha": getattr(bt, "data_sha256", None),
        "begin": bt.vgate.begin_lockbox, "load": bt.vgate.load_lockbox_window,
        "run_g5": bt.vgate.run_g5, "cutoff_for": lockbox_mod.cutoff_for,
        "load_config": lockbox_mod.load_config,
    }
    try:
        bt._g3_reference_returns = lambda *a, **k: (np.zeros(60), {"g3_run_id": 0})
        bt.data_sha256 = lambda bars: "probe"
        bt.vgate.begin_lockbox = lambda *a, **k: None
        bt.vgate.load_lockbox_window = lambda *a, **k: (lockbox, cutoff)
        lockbox_mod.cutoff_for = lambda *a, **k: cutoff
        lockbox_mod.load_config = lambda *a, **k: {}

        def fake_run_g5(ledger, card_, *, lockbox_result, reference_returns,
                        expected_trades, **kwargs):
            captured.update(lockbox_result=lockbox_result,
                            expected_trades=expected_trades)
            return {"passed": None, "probe": True}

        bt.vgate.run_g5 = fake_run_g5
        spec = SimpleNamespace(symbol="USTEC", contract_size=1.0,
                               initial_capital=10_000.0, sizing=None,
                               volume_lots=0.01, vol_min=None, vol_step=None,
                               vol_max=None)
        error = None
        try:
            bt._g5_orchestrate(
                None, card, strategy=PreFomcAnnouncementDrift(), spec=spec,
                params={}, base_summary={"trade_count": 1}, bars=research,
                costs=FlatRelCosts(), session_policy=None, data_root=".",
                approved_by="probe", cfg=None, trial=None,
                round_id=card.round, round_lockbox_uses_max=1)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
    finally:
        bt._g3_reference_returns = saved["ref"]
        if saved["sha"] is not None:
            bt.data_sha256 = saved["sha"]
        bt.vgate.begin_lockbox = saved["begin"]
        bt.vgate.load_lockbox_window = saved["load"]
        bt.vgate.run_g5 = saved["run_g5"]
        lockbox_mod.cutoff_for = saved["cutoff_for"]
        lockbox_mod.load_config = saved["load_config"]

    if error is not None:
        hit = any(w in error for w in ("event", "事件"))
        report("P16-1", "NOT-REPRODUCED" if hit else "INCONCLUSIVE",
               f"round mode={mode!r}; _g5_orchestrate raised {error}")
    else:
        lr = captured.get("lockbox_result", {})
        n_trades = lr.get("trade_count")
        status = "REPRODUCED" if (mode == "event" and n_trades is not None
                                  and n_trades <= 1) else "NOT-REPRODUCED"
        sharpe = lr.get("sharpe")
        report("P16-1", status,
               f"round mode={mode!r}; G5 回测的是卡的 bar 级策略类（BuyHold 脚手架）："
               f"锁箱计入交易 {n_trades} 笔（脚手架在 cutoff 前已入场），"
               f"expected_trades={captured.get('expected_trades'):.4f}，"
               f"锁箱夏普={sharpe:.2f}（买入持有权益曲线）；同期按事件定义应约 8 笔 1 日持仓。"
               "G5 判的不是 C005")

    # P16-2：同一策略类在 10 年 D1 上的形状
    from mt5quant.backtest.engine_a import run_backtest
    res = run_backtest(PreFomcAnnouncementDrift(), research, FlatRelCosts(),
                       symbol="USTEC", params={}, contract_size=1.0,
                       volume_lots=0.01, timeframe="D1")
    tim = _time_in_market(res.trades, research)
    n_events = int(round(len(research) / 252 * 8))
    status = "REPRODUCED" if len(res.trades) <= 1 and tim > 0.9 else "NOT-REPRODUCED"
    report("P16-2", status,
           f"PreFomcAnnouncementDrift 在 {len(research)} 根 D1 上：交易 {len(res.trades)} 笔，"
           f"持仓时间 {tim:.2%}；事件定义同期约 {n_events} 个 1 日窗（持仓≈3%）。"
           "run#451 台账：1 笔、0.9986、ruined——同一形状")


# --------------------------------------------------------------------- P16-3

def probe_live_rejection_exit(repo: Path) -> None:
    from mt5gw.errors import RETCode

    codes = {v for k, v in vars(RETCode).items() if not k.startswith("_")}
    has_10021 = 10021 in codes
    runner_src = (repo / "scripts" / "live_runner.py").read_text(encoding="utf-8")
    exec_src = (repo / "src" / "mt5live" / "executor.py").read_text(encoding="utf-8")
    runner_reraises = re.search(
        r"rr\s*=\s*executor\.poll\(decision,\s*now\)\s*\n\s*except Exception:[\s\S]{0,300}?\n\s*raise\b",
        runner_src) is not None
    runner_handles_reject = "TradeRejectedError" in runner_src
    exec_reraises = re.search(
        r"except TradeRejectedError as exc:[\s\S]{0,900}?\n\s*raise\n", exec_src) is not None
    status = ("REPRODUCED" if (not has_10021 and runner_reraises and exec_reraises
                               and not runner_handles_reject) else "NOT-REPRODUCED")
    report("P16-3", status,
           f"RETCode 含 10021={has_10021}；执行器确定态拒单后重抛={exec_reraises}；"
           f"运行器主循环对 executor.poll 异常一律重抛={runner_reraises}；"
           f"运行器单独处理 TradeRejectedError={runner_handles_reject}"
           "——11-05 14:22 '10021 No prices' 致运行器退出的路径在 main 上仍在")


# --------------------------------------------------------------------- P16-4

def probe_pseudo_h1(repo: Path) -> None:
    from mt5quant.strategies.library.prob.situations import s3_level_rows

    # 审核记录（2026-11-07 t121-release-v2 登记审核 C 节）里的时间列统计
    bars_total, utc_days, p50_bars_per_day = 114_530, 12_568, 1
    # 两类日：1 根/日（伪 H1）与真 H1 日（按每 UTC 日平均 20 根计，含周日短日）
    genuine_days = (bars_total - utc_days) / (20 - 1)
    pseudo_days = utc_days - genuine_days
    # S3 = 20 日 × 24 根；在每日一根的序列上它回看多少个日历日？
    daily = _daily_bars("1980-01-01", "1984-12-31", seed=3)
    rows = s3_level_rows(daily, bars_per_day=24)
    i = len(daily) - 1
    window = daily["high"].iloc[i - 480:i].max()
    span_days = (daily["time"].iloc[i - 1] - daily["time"].iloc[i - 480]).days
    same = bool(np.isclose(rows["chan_high"].iloc[i], window))
    status = "REPRODUCED" if (p50_bars_per_day == 1 and same and span_days > 600) else "NOT-REPRODUCED"
    need_share = 4521 / genuine_days
    report("P16-4", status,
           f"每日一根的'H1'上 S3 通道=前 480 根=约 {span_days} 个日历日（登记含义 20 日）；"
           f"特征预热 2000 根≈8 年、持有 24 根≈24 天。按 114,530 根/12,568 日、中位 1 根/日估算："
           f"真 H1 日约 {genuine_days:,.0f}（含周日短日），伪 H1 日约 {pseudo_days:,.0f}；"
           f"只用真 H1 日时，单行要在约 {need_share:.0%} 的 UTC 日上都有触发才够 4,521"
           "（C104 实测最好的一条约 1,333 触发日）")


# --------------------------------------------------------------------- P16-5

def probe_randomization_throughput(repo: Path) -> None:
    from mt5quant.validation import events as ev

    bars = _daily_bars("1986-01-02", "2025-12-31", seed=1)
    days = pd.to_datetime(bars["time"])
    idx = np.arange(2200, 2200 + 213 * 32, 32)
    evts = [ev.EventWindow(kind="fomc_calendar", label=f"fomc_calendar:{days.iloc[i].date()}",
                           entry_time=days.iloc[i - 1], exit_time=days.iloc[i]) for i in idx]
    side = ev.declared_event_side("static_long")
    sims = 30
    t0 = time.perf_counter()
    ev.randomization_test(bars, evts, side_fn=side, costs=FlatRelCosts(),
                          symbol="USTEC", sims=sims, seed=7)
    per = (time.perf_counter() - t0) / sims
    report("P16-5", "INFO",
           f"同形合成数据（{len(bars)} 根 D1、{len(evts)} 个事件）每次抽取 {per * 1000:.0f} ms，"
           f"1999 次约 {per * 1999 / 60:.1f} 分钟；R3 run5 实测约 9.4 s/次（约 400 倍）——"
           "热点在真实输入装配或成本对象，不在算法本身，须先在真实输入上 cProfile 一次抽取")


# --------------------------------------------------------------------- P16-6

def probe_small_lockbox_power() -> None:
    """σ 取 C005 真实预筛的事件窗口径 1.666%（N1 A1 裁定 §B），每年约 8 个事件。"""
    rng = np.random.default_rng(16)
    sigma, n, reps = 0.01666, 8, 200_000
    out = {}
    for label, mu in (("无效应 μ=0", 0.0), ("2016 年后量级 μ=9bp", 0.0009),
                      ("登记效应 μ=24.5bp", 0.00245), ("原作效应 μ=49bp", 0.0049)):
        x = rng.normal(mu, sigma, size=(reps, n))
        out[label] = float((x.mean(axis=1) > 0).mean())
    ratio = out["登记效应 μ=24.5bp"] / out["无效应 μ=0"]
    years = {lab: (1.96 / (mu / sigma)) ** 2 / 8 for lab, mu in
             (("24.5bp", 0.00245), ("9bp", 0.0009))}
    report("P16-6", "INFO",
           "8 个事件的锁箱上'期望 > 0'通过率："
           + "，".join(f"{k} {v:.0%}" for k, v in out.items())
           + f"——登记效应与无效应的似然比仅 {ratio:.2f}。单侧 α=0.025 达到 50% 检验力"
           f"所需年数：24.5bp 约 {years['24.5bp']:.0f} 年，9bp 约 {years['9bp']:.0f} 年")


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
    try:
        bt = _load_bt(repo)
    except Exception as exc:
        bt = None
        report("P16-1", "INCONCLUSIVE", f"scripts/bt.py 无法 import：{type(exc).__name__}: {exc}")
    if bt is not None:
        probe_g5_event_mode(repo, bt)
    probe_live_rejection_exit(repo)
    probe_pseudo_h1(repo)
    probe_randomization_throughput(repo)
    probe_small_lockbox_power()
    print("\n汇总：")
    for pid, status, _ in RESULTS:
        print(f"  {pid}: {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
