"""第五轮审核复现探针（P8–P13）——只读调用仓库代码，不改任何文件、不连 MT5。

运行（仓库根目录，Git Bash）：
    ./.venv/Scripts/python.exe docs/reviews/probes/2026-10-25-round5-probes.py

不在 tests/ 下（CI 不收集）：这些是"缺陷现场"，修复时请改写成 tests/ 里的
正式失败测试（先红后绿），修好后本脚本的输出应与注释里的"期望"一致。
依赖 tests/test_mt5live_integration.py 的 LiveStack 与 tests/fakes_live.py。
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
try:
    import pytest  # noqa: F401  （集成测试模块在导入时需要）
except ImportError:  # pragma: no cover
    sys.exit("需要仓库 .venv（含 pytest）运行本脚本")

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from tests import test_mt5live_integration as it  # noqa: E402
from tests.fakes_live import boundary_open, grid_bars  # noqa: E402
from mt5live.runner import write_heartbeat  # noqa: E402


def _paused(stack, magics=(it.MAGIC,)) -> str:
    """全局 pause.json 与各 pause-<magic>.json 的 paused 状态（None = 文件不存在）。

    第六轮审核 M1：对账写的是分策略 pause-<magic>.json（T30/D20），全局
    pause.json 只留给人工——只读全局文件打印 None 不能证明策略未暂停，
    两者分别报告。"""
    parts = []
    g = os.path.join(stack.live_dir, "pause.json")
    v = it.read_json(g)["paused"] if os.path.exists(g) else None
    parts.append(f"全局={v}")
    for m in magics:
        p = os.path.join(stack.live_dir, f"pause-{m}.json")
        v = it.read_json(p)["paused"] if os.path.exists(p) else None
        parts.append(f"pause-{m}={v}")
    return " ".join(parts)


def p8_pause_deadlock() -> None:
    print("=== P8 服务器端止损/止盈出场后，对账暂停能否自动解除"
          "（期望：能；且只暂停该策略；执行器先/对账先两种调度顺序均如此）")
    for label, rules in (("after_signal_change", it.ASC_RULES),
                         ("immediate", it.DEFAULT_RULES)):
        for order in ("executor_first", "reconcile_first"):
            with tempfile.TemporaryDirectory() as tmp:
                st = it.LiveStack(Path(tmp), exit_rules=rules)
                other = it.LiveStack(Path(tmp), magic=it.MAGIC_B)   # 同 live 目录的另一策略
                st.round(it.T1, "up")
                ticket = st.broker.positions(it.SYMBOL)[0].ticket
                st.broker.close_external(ticket, profit=-30.0, comment="[sl]")
                print(f"  [{label}/{order}] 开仓 → 止损 →（出场后每轮按该顺序调度）")
                for t, last in ((it.T2, "up"), (it.T3, "flat"), (it.T4, "up"), (it.T5, "up")):
                    if order == "reconcile_first":
                        rc, _ = st.reconcile_once()
                        _, rr = st.round(t, last)
                    else:
                        _, rr = st.round(t, last)
                        rc, _ = st.reconcile_once()
                    print(f"    {t:%H:%M} 信号={last:<4} 执行器={rr.action:<16} "
                          f"对账 rc={rc} {_paused(st)}")
                _, rr_b = other.round(it.T5 + timedelta(minutes=15), "up")
                print(f"    另一策略 magic {it.MAGIC_B} 开仓 → {rr_b.action}；"
                      f"{_paused(st, magics=(it.MAGIC, it.MAGIC_B))}")


def _round_tr(stack, now, last, tr):
    stack._bars_df = grid_bars(boundary_open(now), last=last, tr=tr)
    d = stack.runner.decide(now)
    rr = stack.executor.poll(d, now)
    write_heartbeat(stack.hb_path, config=stack.cfg, decision=d,
                    error=rr.error, dry_run=False, now=now)
    return d, rr


def p9_resize_churn() -> None:
    print("\n=== P9 按风险定手数逐 bar 重算：信号不变时是否反复加减仓（期望：1 笔入场、0 次调仓）")
    with tempfile.TemporaryDirectory() as tmp:
        st = it.LiveStack(Path(tmp))
        times = [it.T1 + timedelta(minutes=15 * i) for i in range(6)]
        for t, tr in zip(times, (2.0, 2.4, 1.8, 2.6, 2.0, 1.7)):
            d, rr = _round_tr(st, t, "up", tr)
            pos = st.broker.positions(it.SYMBOL)
            print(f"  {t:%H:%M} ATR={d.atr:.3f} 动作={rr.action:<14} 持仓票数={len(pos)}"
                  f" 净手数={sum(p.volume for p in pos):.2f}")
        print(f"  市价单 {len(st.broker.market_orders)} 笔、平仓 {len(st.broker.close_calls)} 笔；"
              f"各票止损 {sorted({round(p.sl, 3) for p in st.broker.positions(it.SYMBOL)})}")


def p10_stale_flatten_ack() -> None:
    print("\n=== P10 第二次全平指令会不会被旧 ack 立即归档（期望：不会，执行器全平）")
    with tempfile.TemporaryDirectory() as tmp:
        st = it.LiveStack(Path(tmp))
        st.round(it.T1, "up")
        st.broker.seed_deal(profit=-600.0, magic=it.MAGIC, time=it.T2)
        st.touch_heartbeat(it.T2)
        st.risk_cycle(it.T2)
        st.round(it.T2, "up")
        st.touch_heartbeat(it.T2 + timedelta(seconds=30))
        st.risk_cycle(it.T2 + timedelta(seconds=30))
        cmd = Path(st.live_dir) / "commands"
        print(f"  第一次全平后 commands/：{sorted(p.name for p in cmd.iterdir())}")
        os.remove(os.path.join(st.live_dir, "halt.json"))     # 运维次日解除停机
        day2 = it.T1 + timedelta(days=1)
        st.round(day2, "up")
        st.broker.seed_deal(profit=-900.0, magic=it.MAGIC, time=day2 + timedelta(minutes=5))
        st.touch_heartbeat(day2 + timedelta(minutes=5))
        res = st.risk_cycle(day2 + timedelta(minutes=5))
        print(f"  第二次日亏：flatten_written={res.flatten_written} "
              f"flatten_archived={res.flatten_archived} 指令仍在={(cmd / 'flatten.json').exists()}")
        _, rr = st.round(day2 + timedelta(minutes=15), "up")
        print(f"  执行器下一轮={rr.action}；剩余持仓 {len(st.broker.positions(it.SYMBOL))} 笔")


def p11_eurusd_window() -> None:
    print("\n=== P11 EURUSD 扩展禁入窗（期望：换日后约 1–2 小时，而不是 6 小时）")
    from mt5data.sessions import load_session_policy
    pol = load_session_policy(str(ROOT / "config" / "sessions.toml"))
    cells = []
    for h in list(range(19, 24)) + list(range(0, 5)):
        day = 15 if h >= 19 else 16
        ok = pol.entry_allowed("EURUSD", datetime(2026, 7, day, h, 30))
        cells.append(f"{h:02d}:30{'允' if ok else '禁'}")
    print("  2026-07-15/16 UTC：" + " ".join(cells))


def p12_g5_power() -> None:
    print("\n=== P12 G5 判别力：12 个月锁箱的参照分布有多宽")
    from mt5quant.validation import gates as vg

    class _Ledger:
        def gate_passed(self, cid, gate):
            return True

        def lockbox_status(self, cid):
            return None

        def lockbox_budget(self, key, max_uses=None):
            return {"exhausted": False, "used": 0, "max_uses": 5}

        def finish_lockbox_use(self, cid, **kw):
            pass

    cfg, _ = vg.load_gates()
    bpy = 252 * 24
    rng = np.random.default_rng(1)
    ref = pd.Series(rng.normal(0.002 / np.sqrt(bpy), 0.002, 4 * bpy))
    realized = float(ref.mean() / ref.std(ddof=1) * np.sqrt(bpy))
    print(f"  参照（4 年样本外收益）：生成夏普 1.0，实现夏普 {realized:.2f}")
    for sharpe, exp_ in ((2.0, 5.0), (0.0, -1.0), (-0.3, 0.5)):
        lr = {"expectancy": exp_, "sharpe": sharpe, "trade_count": 200,
              "bar_count": bpy, "annualization_bars_per_year": bpy}
        r = vg.run_g5(_Ledger(), "HX", lockbox_result=lr, reference_returns=ref,
                      expected_trades=250, approved_by="probe", cfg=cfg, bars_per_day=24)
        c = r["checks"]["sharpe_vs_reference"]
        print(f"  锁箱夏普 {sharpe:>4}、每笔期望 {exp_:>4} → p5={c['p5']:.2f} p95={c['p95']:.2f}"
              f" 通过={r['passed']} 告警={'有' if 'warning' in r else '无'}")


def p13_g4_memory() -> None:
    print("\n=== P13 G4 回撤蒙特卡洛：块长 = 一个交易日时的峰值内存")
    import tracemalloc
    from mt5quant.validation import stats as vs
    r5 = np.random.default_rng(7).normal(0, 1e-3, 370_000)
    tracemalloc.start()
    vs.mc_drawdown_distribution(r5, sims=1000, seed=7, block_len=288)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    print(f"  M5 37 万根 × 1000 次，block_len=288：峰值 {peak / 1e6:.0f} MB"
          "（block_len=1 缺省路径的起点数组仍是 1000×370000×8 B ≈ 3.0 GB，未实测）")


if __name__ == "__main__":
    p8_pause_deadlock()
    p9_resize_churn()
    p10_stale_flatten_ack()
    p11_eurusd_window()
    p12_g5_power()
    p13_g4_memory()
