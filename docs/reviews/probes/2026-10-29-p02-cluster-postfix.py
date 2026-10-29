"""T100 模块乙：P-02 成簇负对照重设计的行为探针（2026-10-29）。

用法（仓库根目录）：
    ./.venv/Scripts/python.exe docs/reviews/probes/2026-10-29-p02-cluster-postfix.py

背景：T98 波次 1 重写了 scripts/prob_calibration.py 的 phase_cluster——
real 与 null 均独立整周平移（基础 wk ∈ {3,7} 周 + 独立抽取 0..51 附加周，
NY 时区往返保本地小时/星期），null 不再是 real 平移时刻或原始触发的
子集；越界时刻丢弃不做 nearest 末端堆积。旧的
docs/reviews/probes/2026-10-29-global-audit.py 在 H-02 段（begin_lockbox）
因 H 线修复崩溃，永远到不了 cluster 段，且其 cluster 段断言旧行为
（cluster_null_uses_original=True）、stub 也不带 swap_ref_price kwarg——
该探针归 H 线所有不许改，本探针接替其 P-02 覆盖。

全部合成数据（numpy default_rng 固定种子构造 bars 价格，不读研究库、
不看任何真实局面结局），纯外部捕获/patch，不改产品代码。断言：

A1 两组均无原始触发时刻：label_triggers 捕获的 real 与 null 触发时刻
   集合都与原始触发时刻不相交（平移最少 3 周）。
A2 null 与 real 不同源：shift_weeks_real ≠ shift_weeks_null 的每一行，
   两组时刻集合不相等、互不为子集（本构造下实际全不相交）；独立置换
   ——至少一对两组的行序（level 顺序）不同。
A3 整周粒度：每个平移时刻与对应原始触发（按 level 配对）相差恰为该行
   记录的 shift_weeks 整数周（允许 ±1 小时 DST 位移），且 NY 本地
   小时/分钟/星期不变。
A4 越界丢弃不堆积：序列末尾附近的触发（任何 ≥3 周平移下必然越界）
   不产生标签，也不被 nearest 夹到末端 bar——捕获时刻中无末端 bar
   时刻，每次捕获恰为三个存活核心触发。
A5 phase_cluster 正常返回 rows（含 p / n_days_real / n_days_null 等
   字段，P-03 口径），退出码 0；任一断言不符打印 FAIL 并退出码 1。

实现要点：
- 短 bars 方案（二选一中选了 monkeypatch）：CAL.CLUSTER_EXTRA_WEEKS_MAX
  在 phase_cluster 里只作 rng.integers(0, MAX+1) 的附加周上限，patch 成
  5 → 最大平移 7+5=12 周，合成 bars 只需 16 周（2688 根连续 H1，
  2024-01-01 起，含 3 月 DST 切换）；CLUSTER_SEEDS 缩到 4（seed 0..3，
  实测每 seed 的 extra_real ≠ extra_null，故 A2 的条件断言每行都生效），
  CLUSTER_SHIFT_WEEKS 保持 (3, 7) 不动。
- stub CAL._costs_and_sizes 必须接受 swap_ref_price kwarg（P-03 后
  phase_cluster 以 swap_ref_price=最后收盘调用）；stub CAL.delta_p_test
  返回含 n_days_real / n_days_null（phase_cluster 行内直接取这些键）。
- 触发行用互不相同的 level 标记身份，可穿过 sample(frac=1.0) 置换逐行
  配对回原始时刻；三个核心触发位于索引 276/540/660（NY 墙钟 07:00，
  全部平移下存活），一个必越界触发位于 2604（距末端 84 根 < 3 周）。

反向自证（断言口径如何区分新旧实现）：
- 若 null 退回 real 平移时刻的子集（旧嵌套实现）：A2 的"集合不等/互不
  包含/全不相交"红。
- 若 null 退回原始触发时刻（旧全局审计探针所断言的行为）：A1 的"null
  与原始不相交"红，A2 也红。
- 若 real 退回原始触发（不平移）或平移非整周：A1/A3 红。
- 若恢复越界 nearest 末端堆积：A4 的"无末端 bar 时刻 / 必越界触发无
  标签 / 每次恰三行"红。
- 若 delta_p_test 结果不再携带 n_days_real/n_days_null（P-03 回退）：
  phase_cluster 取键直接 KeyError → A5 红（探针会以异常路径 FAIL）。
"""
from __future__ import annotations

import contextlib
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(REPO / "src"), str(REPO / "scripts"), str(REPO / "tests"),
                str(REPO)]

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import prob_calibration as CAL  # noqa: E402
import run_backtest as RUN  # noqa: E402

# ---- 设计常量（见 docstring：只缩规模，不动被测机制） ---------------------
N_WEEKS = 16
WEEK_BARS = 24 * 7
N_BARS = N_WEEKS * WEEK_BARS          # 2688 根连续 H1
EXTRA_MAX = 5                         # 平移上限 = 7 + 5 = 12 周 < 16 周
SEEDS = 4
RNG_SEED = 20261029
CORE_IDX = (276, 540, 660)            # 全平移下存活；NY 墙钟 07:00
DOOMED_IDX = 2604                     # 距末端 84 根（<3 周）→ 必越界
CORE_LEVELS = (101.0, 102.0, 103.0)
DOOMED_LEVEL = 104.0
DST_TOL = pd.Timedelta(hours=1)       # NY 往返跨 DST 的整周平移 ±1 小时

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f" —— {detail}" if detail else ""))


# ---- 合成数据（确定性） ---------------------------------------------------
rng = np.random.default_rng(RNG_SEED)
steps = rng.normal(0.0, 0.2, N_BARS)
close = 2000.0 + np.cumsum(steps)
open_ = close - steps
high = np.maximum(open_, close) + rng.random(N_BARS) * 0.1
low = np.minimum(open_, close) - rng.random(N_BARS) * 0.1
bars = pd.DataFrame({
    "time": pd.date_range("2024-01-01", periods=N_BARS, freq="h"),
    "open": open_, "high": high, "low": low, "close": close,
    "tick_volume": 10, "spread": 5, "real_volume": 0,
})
times_idx = pd.Index(bars["time"])
last_bar = pd.Timestamp(bars["time"].iloc[-1])

trig = pd.DataFrame({
    "time": bars["time"].iloc[list(CORE_IDX) + [DOOMED_IDX]].to_numpy(),
    "situation": "S1", "action": "A3", "side": 1,
    "level": list(CORE_LEVELS) + [DOOMED_LEVEL],
})
orig_by_level = {lev: pd.Timestamp(t)
                 for lev, t in zip(trig["level"], trig["time"])}
orig_times = set(orig_by_level.values())

# ---- 捕获桩 ---------------------------------------------------------------
label_calls: list[pd.DataFrame] = []


def capture_labels(b, trig_df, costs, **kw):
    label_calls.append(trig_df.copy())
    return trig_df.assign(outcome="win", net_r=1.0)


dp_calls: list[tuple[int, pd.DataFrame, pd.DataFrame]] = []


def fake_delta_p(real, null, *, block=None, sims, seed):
    dp_calls.append((seed, real, null))
    return {"p_value": 0.5,
            "n_days_real": int(len(real)), "n_days_null": int(len(null))}


rows: list[dict] = []
err: str = ""
try:
    with contextlib.ExitStack() as stack:
        for obj, name, value in [
            (RUN, "load_series", lambda *a, **k: bars),
            (CAL, "_costs_and_sizes",
             lambda swap_ref_price=None: (SimpleNamespace(), SimpleNamespace(),
                                          100.0, "synthetic")),
            (CAL, "CLUSTER_SEEDS", SEEDS),
            (CAL, "CLUSTER_EXTRA_WEEKS_MAX", EXTRA_MAX),
            (CAL, "label_triggers", capture_labels),
            (CAL, "delta_p_test", fake_delta_p),
        ]:
            stack.enter_context(patch.object(obj, name, value))
        # phase_cluster 函数内 from ... import 的三个探测函数按模块属性 patch
        stack.enter_context(patch("mt5quant.prob.counting."
                                  "level_rows_for_situation", return_value=[]))
        stack.enter_context(patch("mt5quant.strategies.library.prob."
                                  "situations.find_level_events",
                                  return_value=[]))
        stack.enter_context(patch("mt5quant.strategies.library.prob."
                                  "actions.action_triggers",
                                  return_value=trig))
        rows = CAL.phase_cluster(0, 1, "synthetic")
except Exception:
    err = traceback.format_exc()

if err:
    print("FAIL  phase_cluster 抛异常 ——")
    print(err)
    sys.exit(1)

# ---- A5：返回结构与调用配对（calls[2i]=该行 real、calls[2i+1]=null） ----
want_keys = {"kind", "weeks", "seed", "shift_weeks_real", "shift_weeks_null",
             "n_real", "n_null", "n_days_real", "n_days_null", "p"}
check("A5 rows 结构与数量",
      isinstance(rows, list) and len(rows) == len(CAL.CLUSTER_SHIFT_WEEKS) * SEEDS
      and all(want_keys <= set(r) for r in rows)
      and all(r["kind"] == "cluster_neg" for r in rows)
      and len(label_calls) == 2 * len(rows)
      and len(dp_calls) == len(rows),
      f"{len(rows)} 行 / {len(label_calls)} 次 label_triggers / "
      f"{len(dp_calls)} 次 delta_p_test")

real_calls = label_calls[0::2]
null_calls = label_calls[1::2]


def times_of(df: pd.DataFrame) -> set:
    return set(pd.to_datetime(df["time"]).tolist())


def levels_of(df: pd.DataFrame) -> list:
    return list(df["level"])


# ---- A1：real 与 null 的触发时刻都与原始触发不相交 ------------------------
a1_hits = [t for df in label_calls for t in times_of(df) if t in orig_times]
check("A1 两组均无原始触发时刻", not a1_hits,
      f"撞上原始时刻 {sorted(a1_hits)[:3]}" if a1_hits
      else f"全部 {sum(len(df) for df in label_calls)} 个捕获时刻与 "
           f"{len(orig_times)} 个原始时刻不相交")

# ---- A2：null 与 real 集合不等、互不包含（不同平移周时全不相交） ----------
a2_bad, a2_pairs_diff_shift, a2_perm_diff = [], 0, False
for i, r in enumerate(rows):
    rs, ns = times_of(real_calls[i]), times_of(null_calls[i])
    if r["shift_weeks_real"] != r["shift_weeks_null"]:
        a2_pairs_diff_shift += 1
        if rs == ns or rs <= ns or ns <= rs or not rs.isdisjoint(ns):
            a2_bad.append((r["weeks"], r["seed"], sorted(rs & ns)[:3]))
    if levels_of(real_calls[i]) != levels_of(null_calls[i]):
        a2_perm_diff = True
check("A2 null 非 real 子集且不同源", not a2_bad and a2_pairs_diff_shift > 0
      and a2_perm_diff,
      f"{a2_pairs_diff_shift}/{len(rows)} 行平移周不同→集合全不相交；"
      f"独立置换行序不同：{a2_perm_diff}"
      + (f"；违例 {a2_bad[:2]}" if a2_bad else ""))

# ---- A3：整周粒度 + NY 本地小时/星期不变 ---------------------------------
a3_bad = []
for i, r in enumerate(rows):
    for group, shift in (("real", r["shift_weeks_real"]),
                         ("null", r["shift_weeks_null"])):
        df = real_calls[i] if group == "real" else null_calls[i]
        for t, lev in zip(pd.to_datetime(df["time"]), df["level"]):
            t0 = orig_by_level[lev]
            delta = t - t0
            if abs(delta - pd.Timedelta(weeks=int(shift))) > DST_TOL:
                a3_bad.append((group, r["weeks"], r["seed"], lev, str(delta)))
                continue
            t_ny = t.tz_localize("UTC").tz_convert("America/New_York")
            t0_ny = t0.tz_localize("UTC").tz_convert("America/New_York")
            if (t_ny.hour, t_ny.minute, t_ny.weekday()) != \
                    (t0_ny.hour, t0_ny.minute, t0_ny.weekday()):
                a3_bad.append((group, r["weeks"], r["seed"], lev,
                               f"NY {t0_ny}→{t_ny}"))
check("A3 整周平移且 NY 本地小时/星期不变", not a3_bad,
      f"核对 {2 * sum(len(df) for df in label_calls)} 个配对（±{DST_TOL}）"
      + (f"；违例 {a3_bad[:3]}" if a3_bad else ""))

# ---- A4：越界触发无标签、无末端堆积、每次捕获恰三核心 ----------------------
doomed_seen = [i for i, df in enumerate(label_calls)
               if DOOMED_LEVEL in set(df["level"])]
end_seen = [i for i, df in enumerate(label_calls) if last_bar in times_of(df)]
bad_counts = [i for i, df in enumerate(label_calls)
              if set(df["level"]) != set(CORE_LEVELS) or len(df) != len(CORE_LEVELS)]
bad_dp = [(s, len(a), len(b)) for s, a, b in dp_calls
          if len(a) != len(CORE_LEVELS) or len(b) != len(CORE_LEVELS)]
bad_rows = [r for r in rows
            if r["n_real"] != len(CORE_LEVELS) or r["n_null"] != len(CORE_LEVELS)
            or r["n_days_real"] != len(CORE_LEVELS)
            or r["n_days_null"] != len(CORE_LEVELS)]
check("A4 越界丢弃不堆积", not doomed_seen and not end_seen and not bad_counts
      and not bad_dp and not bad_rows,
      f"必越界触发(level {DOOMED_LEVEL})出现于 {len(doomed_seen)} 次捕获；"
      f"末端 bar {last_bar} 出现于 {len(end_seen)} 次捕获；"
      f"计数异常捕获 {len(bad_counts)}、delta_p 输入异常 {len(bad_dp)}、"
      f"行计数异常 {len(bad_rows)}")

# ---- 汇总 -----------------------------------------------------------------
fails = [(n, d) for n, ok, d in results if not ok]
print(f"\n{len(results) - len(fails)}/{len(results)} 断言通过"
      + ("" if not fails else "；失败：" + "; ".join(n for n, _ in fails)))
sys.exit(1 if fails else 0)
