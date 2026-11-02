"""第十五轮审核（H 线 + P 线合并审核）复现与论证脚本（2026-11-02）。

运行：python docs/reviews/probes/2026-11-02-round15-probes.py
只依赖 numpy / pandas（不需要 pyarrow，不读研究库、不连 MT5、不写任何文件）。

输出约定：
- REPRODUCED：复现了报告所述缺陷（不是产品通过）；修好后应变为 NOT-REPRODUCED。
- INFO：方法论证据（模拟），不是缺陷复现，数字随 seed 固定可重放。
退出码恒为 0（本脚本是证据，不是 CI 门禁）。

探针清单：
  H15-1  bt gate V3 事件分支的方向是常量（--event-side），C002 的登记方向
         （按 USTEC 当月涨跌定向）从未进入检验——R1 的"falsified"不是对
         C002 的检验。
  H15-2  H 线 bt screen/gate 只读 MT5 研究库；合并外部数据的读取只在 P 线
         入口——预筛按 18.72 年（含 HistData）给检验力，检验只用了 MT5 段
         46 个事件。
  H15-3  mt5gw / mt5live 没有任何调用超时或卡死看门狗（阶段 F 第 3、5 日
         "进程活着但冻结 / 静默退出"的代码侧前提）。
  P15-1  cluster-permutation-v1 的簇按 real∪null 合并日单链聚合：触发越密，
         簇越少、最大簇越大——样本越多检验越退化（v4 报告 n≥2000 功率 0 的
         机制）。
  P15-2  （INFO）P1 检验设计对照模拟：全局随机对照 + 月内置换（现行思路）
         vs 局部匹配对照 + 按月符号翻转（候选新设计）。小时级随机游走、
         约 2:1 括号、成簇同向触发。结论只作方法论参考：这个简化模拟里两种
         设计的误报率与检出率相近，**没有**复现 T75 在真实池上的成簇误报
         （11%）——说明那个误报来自真实行情的状态结构（趋势/波动聚集），
         任何新方法都必须在 T75 的真实池负对照上实测，不能用简化模拟代替。
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

RESULTS: list[tuple[str, str, str]] = []


def report(probe: str, status: str, detail: str) -> None:
    RESULTS.append((probe, status, detail))
    print(f"[{probe}] {status}: {detail}")


# ---------------------------------------------------------------- H15-1

def _find_func(tree: ast.Module, name: str) -> ast.FunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def probe_h15_1_event_side_is_constant() -> None:
    src = (ROOT / "scripts" / "bt.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = _find_func(tree, "_v3_gate_evaluate")
    if fn is None:
        report("H15-1", "NOT-REPRODUCED", "找不到 _v3_gate_evaluate（结构已变，人工复核）")
        return
    side_from_ctx = False
    lambda_returns_side = False
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            tgt = node.targets[0]
            if isinstance(tgt, ast.Name) and tgt.id == "side":
                v = node.value
                if (isinstance(v, ast.Subscript) and isinstance(v.value, ast.Name)
                        and v.value.id == "ctx"):
                    side_from_ctx = True
            if isinstance(tgt, ast.Name) and tgt.id == "side_fn":
                v = node.value
                if isinstance(v, ast.Lambda) and isinstance(v.body, ast.Name) \
                        and v.body.id == "side":
                    lambda_returns_side = True
    cli_const = bool(re.search(
        r'"--event-side",\s*choices=\("long",\s*"short"\)', src))
    ctx_from_cli = '"side": args.event_side' in src
    static = side_from_ctx and lambda_returns_side and cli_const and ctx_from_cli

    # 数值演示：同一组合成事件，常量方向 vs 登记方向（按信号品种当月涨跌）
    from mt5quant.validation import events as ev

    class ZeroCosts:            # 只为隔离方向效应：成本全 0
        swap = None

        def spread_p90(self, symbol, hour, price=None):
            return 0.0

        def slippage(self, symbol, hour, price=None):
            return 0.0

        def commission_per_lot(self, symbol):
            return 0.0

    rng = np.random.default_rng(20261102)
    months = pd.period_range("2010-01", periods=120, freq="M")
    signal_up = rng.random(len(months)) < 0.5          # 合成"USTEC 当月上涨"
    rows, events, sign_by_label = [], [], {}
    price = 1.20
    for m, up in zip(months, signal_up):
        last = m.to_timestamp(how="end").normalize()
        while last.weekday() >= 5:
            last -= pd.Timedelta(days=1)
        fix_events = ev.london_fix_events(pd.DatetimeIndex([last]))
        e = fix_events[0]
        move = (0.0010 if up else -0.0010) + rng.normal(0, 0.0002)
        t0 = e.entry_time.floor("h")
        rows.append({"time": t0, "open": price, "close": price})
        rows.append({"time": t0 + pd.Timedelta(hours=1),
                     "open": price * (1 + move), "close": price * (1 + move)})
        rows.append({"time": t0 + pd.Timedelta(hours=2),
                     "open": price * (1 + move), "close": price * (1 + move)})
        events.append(e)
        sign_by_label[e.label] = "long" if up else "short"
    bars = pd.DataFrame(rows).sort_values("time").reset_index(drop=True)
    costs = ZeroCosts()
    static_long = ev.event_returns(bars, events, side_fn=lambda e: "long",
                                   costs=costs, symbol="EURUSD")
    registered = ev.event_returns(bars, events,
                                  side_fn=lambda e: sign_by_label[e.label],
                                  costs=costs, symbol="EURUSD")
    s_mean = float(static_long["gross"].mean()) * 1e4
    r_mean = float(registered["gross"].mean()) * 1e4
    detail = (f"bt 事件分支 side=ctx['side']（来自 --event-side 常量）且 side_fn="
              f"lambda: side → {'常量方向' if static else '已改'}；合成演示（信号品种"
              f"50% 月份上涨、定盘小时按登记方向 ±10bp）：常量 long 均值 {s_mean:+.2f}bp，"
              f"登记方向均值 {r_mean:+.2f}bp——bt 只能算出前者")
    report("H15-1", "REPRODUCED" if static else "NOT-REPRODUCED", detail)


# ---------------------------------------------------------------- H15-2

def probe_h15_2_coverage_mismatch() -> None:
    src = (ROOT / "scripts" / "bt.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    users = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            body_src = ast.get_source_segment(src, node) or ""
            if "merged_research_series" in body_src:
                users.append(node.name)
    top_level_users = sorted(set(users))
    h_paths_use_merged = any(u in ("cmd_gate", "cmd_screen")
                             for u in top_level_users)
    pre = json.loads((ROOT / "research" / "candidates" / "C002.precheck.json")
                     .read_text(encoding="utf-8"))
    power = (pre.get("checks") or {}).get("power") or {}
    years = power.get("years")
    segs = [f"{s_['source']} {s_['first'][:7]}..{s_['last'][:7]}"
            for s_ in (power.get("coverage_segments") or {}).get(
                "EURUSD/H1", [])]
    rpt = (ROOT / "docs" / "research" / "round-R1-2026-10-28.md").read_text(
        encoding="utf-8")
    m = re.search(r"MT5 段 (\d+) 事件", rpt)
    n_events = int(m.group(1)) if m else None
    mismatch = (not h_paths_use_merged) and n_events is not None
    detail = (f"含 merged_research_series 的函数：{top_level_users}（H 线 cmd_screen/"
              f"cmd_gate 不在其中）；C002 预筛研究年数={years:.2f}（段：{segs}）；"
              f"R1 检验事件数={n_events}（≈{(n_events or 0) / 12:.1f} 年月末）")
    report("H15-2", "REPRODUCED" if mismatch else "NOT-REPRODUCED", detail)


# ---------------------------------------------------------------- H15-3

def probe_h15_3_no_timeouts() -> None:
    """只认真正的"调用超时 / 卡死看门狗 / 原生崩溃留痕"实现：
    future.result(timeout=…)、ThreadPoolExecutor、threading.Timer、
    signal.alarm、stall/watchdog 线程、faulthandler。心跳超时判定与文件锁
    重试不算（它们检测的是别的进程，救不了自己卡死的进程）。"""
    pattern = re.compile(
        r"result\([^)]*timeout|ThreadPoolExecutor|threading\.Timer|"
        r"signal\.alarm|stall|watchdog|faulthandler", re.I)
    hits = []
    files = sorted((ROOT / "src" / "mt5gw").glob("*.py")) + \
        sorted((ROOT / "src" / "mt5live").glob("*.py")) + \
        [ROOT / "scripts" / f for f in ("risk_process.py", "live_runner.py",
                                        "live_reconcile.py")]
    for f in files:
        if f.exists() and pattern.search(f.read_text(encoding="utf-8")):
            hits.append(f.name)
    status = "REPRODUCED" if not hits else "NOT-REPRODUCED"
    report("H15-3", status,
           "网关与三个实盘进程中的调用超时/卡死看门狗/faulthandler 实现："
           f"{hits or '无'}（心跳超时只能发现别的进程停了，进程自己卡在 MT5 "
           "调用里时没有任何自救或留痕）")


# ---------------------------------------------------------------- P15-1

def probe_p15_1_cluster_chaining() -> None:
    from mt5quant.validation.prob_mode import (CLUSTER_GAP_DAYS,
                                               _cluster_ids_for_days)
    rng = np.random.default_rng(7)
    weekdays = pd.bdate_range("2014-01-01", "2019-12-31")   # 约 6 年发现段
    lines = []
    worst_share = 0.0
    for n_each in (100, 300, 600, 1200):
        real = rng.choice(len(weekdays), n_each, replace=False)
        null = rng.choice(len(weekdays), n_each, replace=False)
        merged = sorted(set(weekdays[real]) | set(weekdays[null]))
        ids = _cluster_ids_for_days(merged)
        sizes = np.bincount(ids)
        share = sizes.max() / sizes.sum()
        worst_share = max(worst_share, share)
        lines.append(f"每组 {n_each} 日→合并 {len(merged)} 日、{len(sizes)} 簇、"
                     f"最大簇占 {share:.0%}")
    status = "REPRODUCED" if worst_share > 0.5 else "NOT-REPRODUCED"
    report("P15-1", status,
           f"CLUSTER_GAP_DAYS={CLUSTER_GAP_DAYS} 单链聚合（均匀随机日）："
           + "；".join(lines))


# ---------------------------------------------------------------- P15-2

def _bracket_outcomes(path: np.ndarray, horizon: int, tp: float, sl: float):
    """每个起点 s、两个方向的首达结局：1=止盈、0=止损或到期（日内 bar 路径）。"""
    n = len(path)
    win = {+1: np.zeros(n, dtype=float), -1: np.zeros(n, dtype=float)}
    valid = np.zeros(n, dtype=bool)
    idx = np.arange(n - horizon)
    fut = path[idx[:, None] + np.arange(1, horizon + 1)[None, :]] - path[idx, None]
    for d in (+1, -1):
        up = (d * fut) >= tp
        dn = (d * fut) <= -sl
        t_up = np.where(up.any(1), up.argmax(1), horizon + 1)
        t_dn = np.where(dn.any(1), dn.argmax(1), horizon + 1)
        win[d][idx] = (t_up < t_dn).astype(float)  # 同根同时触及按止损（不利优先）
    valid[idx] = True
    return win, valid


def _episodes(rng, n_days: int, start_p: float = 0.06, mean_len: float = 5,
              day_p: float = 0.7):
    """成簇触发：事件段内逐日 day_p 概率触发，段内方向固定（同向突破/同向反转）。"""
    trig = np.zeros(n_days, dtype=bool)
    direction = np.zeros(n_days, dtype=int)
    t = 0
    while t < n_days:
        if rng.random() < start_p:
            length = int(rng.geometric(1 / mean_len))
            d = 1 if rng.random() < 0.5 else -1
            for u in range(t, min(n_days, t + length)):
                if rng.random() < day_p:
                    trig[u] = True
                    direction[u] = d
            t += length
        else:
            t += 1
    return trig, direction


def _one_rep(rng, n_days: int, dp: float, B: int, bars_per_day: int = 24,
             horizon: int = 24, n_ctrl: int = 5, window: int = 5):
    path = np.cumsum(rng.normal(0, 1, n_days * bars_per_day))
    win, valid = _bracket_outcomes(path, horizon, tp=2.4, sl=1.2)
    trig, direction = _episodes(rng, n_days)
    month_of = np.arange(n_days) // 21

    def outcome(day, d):
        s = day * bars_per_day + int(rng.integers(0, bars_per_day))
        return (win[d][s], valid[s])

    real_days, real_y, real_dir = [], [], []
    p_flip = dp / (1 - 0.33) if dp > 0 else 0.0
    for day in np.flatnonzero(trig):
        y, ok = outcome(day, int(direction[day]))
        if not ok:
            continue
        if y == 0 and p_flip and rng.random() < p_flip:   # 植入 Δp
            y = 1.0
        real_days.append(day)
        real_y.append(y)
        real_dir.append(int(direction[day]))
    real_days = np.asarray(real_days)
    real_y = np.asarray(real_y, dtype=float)
    if len(real_days) < 20:
        return None

    # A：全局随机对照（同等对照数）+ 月内置换（现行思路）
    non = np.flatnonzero(~trig)
    null_days = rng.choice(non, size=min(n_ctrl * len(real_days), len(non)),
                           replace=True)
    null_y, keep = [], []
    for day in null_days:
        y, ok = outcome(day, 1 if rng.random() < 0.5 else -1)
        if ok:
            null_y.append(y)
            keep.append(day)
    null_days = np.asarray(keep)
    null_y = np.asarray(null_y, dtype=float)
    obs_a = real_y.mean() - null_y.mean()
    months = np.concatenate([month_of[real_days], month_of[null_days]])
    yy = np.concatenate([real_y, null_y])
    is_real = np.concatenate([np.ones(len(real_y), bool),
                              np.zeros(len(null_y), bool)])
    order = np.argsort(months, kind="stable")
    months, yy, is_real = months[order], yy[order], is_real[order]
    perm = np.argsort(months[None, :] * 10.0 + rng.random((B, len(yy))), axis=1)
    pv = yy[perm]
    stat_a = (np.where(is_real, pv, 0).sum(1) / is_real.sum()
              - np.where(~is_real, pv, 0).sum(1) / (~is_real).sum())
    p_a = (1 + int((stat_a >= obs_a - 1e-12).sum())) / (1 + B)

    # B：局部匹配对照（±window 日、同方向、非触发日）+ 按月符号翻转（建议设计）
    diffs, dmonth = [], []
    for k, day in enumerate(real_days):
        lo, hi = max(0, day - window), min(n_days, day + window + 1)
        cand = [u for u in range(lo, hi) if not trig[u]]
        ys = []
        for u in (rng.choice(cand, size=n_ctrl, replace=True) if cand else []):
            y, ok = outcome(int(u), real_dir[k])
            if ok:
                ys.append(y)
        if ys:
            diffs.append(real_y[k] - float(np.mean(ys)))
            dmonth.append(month_of[day])
    diffs = np.asarray(diffs)
    if len(diffs) < 20:
        return None
    uniq, inv = np.unique(np.asarray(dmonth), return_inverse=True)
    sums = np.bincount(inv, weights=diffs, minlength=len(uniq))
    signs = rng.choice((-1.0, 1.0), size=(B, len(uniq)))
    stat_b = (signs * sums).sum(1) / len(diffs)
    p_b = (1 + int((stat_b >= diffs.mean() - 1e-12).sum())) / (1 + B)
    return p_a, p_b, len(real_days)


def probe_p15_2_design_simulation(B: int = 499) -> None:
    """路径式模拟：小时级随机游走、止盈 2.4σ/止损 1.2σ/24 根（约 2:1 括号）、
    成簇同向触发（"无优势但成簇"）。触发窗跨日重叠，相邻触发日的结局天然
    相关。随机游走没有趋势/波动状态，所以它只检验"窗口重叠"这一种相关来源；
    T75 真实池里的成簇误报主要来自状态结构，本模拟不覆盖。"""
    rng = np.random.default_rng(20261102)
    lines = []
    for n_days, reps in ((1500, 400), (6000, 150)):
        for dp in (0.0, 0.03, 0.05):
            rej_a = rej_b = done = 0
            n_trig = []
            for _ in range(reps):
                r = _one_rep(rng, n_days, dp, B)
                if r is None:
                    continue
                p_a, p_b, nt = r
                rej_a += p_a <= 0.05
                rej_b += p_b <= 0.05
                done += 1
                n_trig.append(nt)
            tag = "误报率" if dp == 0 else f"检出率 Δp={dp:.2f}"
            lines.append(f"{n_days} 日（触发日中位 {int(np.median(n_trig))}，"
                         f"{done} 次）{tag}：全局+月内置换 {rej_a / done:.1%}、"
                         f"局部配对+月符号翻转 {rej_b / done:.1%}")
    report("P15-2", "INFO", "；".join(lines))


def main() -> int:
    probe_h15_1_event_side_is_constant()
    probe_h15_2_coverage_mismatch()
    probe_h15_3_no_timeouts()
    probe_p15_1_cluster_chaining()
    probe_p15_2_design_simulation()
    print()
    print("汇总：")
    for probe, status, _ in RESULTS:
        print(f"  {probe}: {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
