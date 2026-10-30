"""概率模式评估（T75，蓝图 P 线 §3.2 / 决策 D42；在 T66 事件模式框架上实现）。

P1 主检验：Δp = 局面触发赢率 − 匹配随机时点赢率，单侧；同一天的多个
触发先按日聚合；p 值按月做块自助；同一轮全部预注册假设用 Holm 校正。
零假设：触发时刻随机化（保持纽约小时、星期与 ATR 分位分布）；正对照：
在标签上植入已知 Δp。P2 校准否决项：Brier 技能分。

T98 audit P-09/P-11：randomize_triggers 可选 eligible_bars 限定抽样池
（真实触发与随机对照的共同可用集合；缺省 None = 全域，行为逐位不变）；
plant_delta 改位置索引（非 RangeIndex 上 .loc 会改错行）；brier_skill /
holm / delta_p_test 补非有限数/形状/概率区间/sims 校验。

**本模块只用合成数据 / 随机时点 / 随机化触发 / 植入效应做标定与测试**
（蓝图 v4 §2 禁止事项：T75 标定报告与 T77 轮次草案批准之前，不在真实
局面触发上计算任何标签、拟合或 Δp）。评估代码（D45：研究角色只读）。
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

NY_TZ = "America/New_York"

# S7：skipped（未成交/截尾）只计数不进概率（T72 约定）
_USABLE_OUTCOMES = {"win", "loss", "time"}

# ---------------------------------------------------------------- 方法登记
# T104 批 2（audit2 P2-09）：Δp 检验主方法的**批准状态登记**——月内置换在
# t75-formal-v2 的成簇负对照（P-02：real 与 null 均独立整周平移）失败期间，
# 其产出（Holm p、推进判定）不得作为推进依据：运行仍可（诊断），但
# bt prob round-report 的推进结论被方法批准门阻塞（消费方：scripts/bt.py）。
# 状态只增改不删行（历史判定可复核）；批准 = 新方法完整标定且成簇对照
# 通过后，在此登记 adopted=True 并更新 PRIMARY_METHOD。
METHOD_REGISTRY: dict[str, dict] = {
    "month-within": {
        # delta_p_test(block="month")——2026-10-28 报告 §1 声明的现行变体
        "status": "failed_cluster_control",
        "adopted": False,
        "note": "月内置换：t75-formal-v2 成簇负对照 w7 档误报率 11% > 5%"
                "（w3 档 4% 通过）——判定标准 2 未通过，方法未获批准"
                "（prob-mode-calibration-2026-10-29-v2.md §3）",
    },
    "quarter": {
        # delta_p_test(block="quarter")——v2 §6 预写升级选项一（T101 全套
        # 试验）；块长参数已进 delta_p_test，方法本身未进现行口径
        "status": "not_adopted",
        "adopted": False,
        "note": "季度内置换：t75-formal-v3（T101）w3=4% 通过、w7=8%"
                "（8/100）> 5%——按预写规则不采纳，未进入现行方法"
                "（prob-mode-calibration-2026-10-29-v3.md）",
    },
}
# 现行主检验方法（round-report 的 Holm p 由它产出）
PRIMARY_METHOD = "month-within"


def primary_method_approved() -> tuple[bool, str]:
    """主检验方法是否获批 → (approved, reason)。

    P2-09：PRIMARY_METHOD 在 METHOD_REGISTRY 的条目 adopted=False（现行
    月内置换在 v2/v3 成簇负对照失败）→ (False, 原因)；获批 → (True, "")。
    reason 直接进 round-report 的方法阻塞标注（不构成推进依据的声明）。
    未知方法名 / 缺条目 → 同样不批准（fail-closed：登记缺失不是放行理由）。
    """
    entry = METHOD_REGISTRY.get(PRIMARY_METHOD)
    if entry is None:
        return False, (f"主检验方法 {PRIMARY_METHOD!r} 不在 METHOD_REGISTRY"
                       "——登记缺失按未批准处理（P2-09 fail-closed）")
    if not entry.get("adopted"):
        return False, (f"主检验方法 {PRIMARY_METHOD!r} 未批准"
                       f"（{entry.get('status')}：成簇负对照失败）——"
                       f"{entry.get('note')}")
    return True, ""


# ---------------------------------------------------------------- 随机化

def randomize_triggers(triggers: pd.DataFrame, bars: pd.DataFrame, *,
                       seed: int,
                       match=("ny_hour", "weekday", "atr_quantile"),
                       eligible_bars=None) -> pd.DataFrame:
    """零假设触发：随机时刻，桶键 =（纽约小时、星期、ATR 分位档）。

    ATR 分位档用卡的特征口径 ``atr_pct_2000``（ATR14 在最近 2,000 根中
    的滚动分位，≥ 0.5 为 high——与 LevelDecisionV1 的特征同一实现）。
    桶内均匀随机抽时刻替换每个原触发（分布与原触发的桶分布一致，桶内
    时刻均匀是零假设）。**同桶没有候选 → ValueError**（审核三 S3：退化为
    全域均匀会让匹配悄悄失效，宁可拒绝也不静默偏差）。确定性：同一
    seed 同一结果。

    eligible_bars（T98 audit P-09，可选）：布尔掩码（与 bars 等长）或
    bar 下标数组（无序/重复按集合语义）——抽样池限定在共同可用集合内，
    桶匹配维不变；**缺省 None = 全域，行为逐位不变**（探针兼容）。
    形状/越界 → ValueError。
    """
    from mt5quant.strategies.library.prob.features import atr_pct_2000
    rng = np.random.default_rng(seed)
    times = pd.to_datetime(bars["time"])
    n_bars = len(bars)
    if eligible_bars is None:
        allowed = np.ones(n_bars, dtype=bool)
    else:
        allowed = np.zeros(n_bars, dtype=bool)
        raw = np.asarray(eligible_bars)
        if raw.dtype == bool:
            if raw.shape != (n_bars,):
                raise ValueError(
                    f"eligible_bars 布尔掩码须与 bars 等长（{n_bars}），"
                    f"形状 {raw.shape}")
            allowed = raw.copy()
        else:
            idx = np.asarray(eligible_bars).astype(np.int64)
            if idx.ndim != 1:
                raise ValueError(
                    f"eligible_bars 须为一维下标数组，形状 {idx.shape}")
            if len(idx) and (idx.min() < 0 or idx.max() >= n_bars):
                raise ValueError(
                    f"eligible_bars 下标越界（合法 0..{n_bars - 1}）")
            allowed[idx] = True
    ny = times.dt.tz_localize("UTC").dt.tz_convert(NY_TZ)
    hour = ny.dt.hour.to_numpy()
    weekday = ny.dt.weekday.to_numpy()
    q = atr_pct_2000(atr_series_for(bars)).to_numpy()
    pool = {}
    for i in range(n_bars):
        if not allowed[i]:
            continue
        # q ∈ {"high","low",None}（None=分位窗口未满）——None 桶只配
        # None 触发，不跨档匹配
        pool.setdefault((int(hour[i]), int(weekday[i]), q[i]), []).append(i)
    trig_times = pd.to_datetime(triggers["time"])
    t_ny = trig_times.dt.tz_localize("UTC").dt.tz_convert(NY_TZ)
    pos = pd.Index(times).get_indexer(trig_times)
    out_idx = []
    for k in range(len(trig_times)):
        q_k = q[pos[k]] if 0 <= pos[k] < len(q) else None
        key = (int(t_ny.iloc[k].hour), int(t_ny.iloc[k].weekday()), q_k)
        cands = pool.get(key)
        if not cands:
            pool_note = ("（抽样池已限定 eligible_bars——P-09 共同可用"
                         "集合；不退化为全域）" if eligible_bars is not None
                         else "")
            raise ValueError(
                "随机化零假设无法匹配触发 "
                f"{trig_times.iloc[k]}（桶 {key[:2]} + ATR 档 {key[2]} 无候选"
                " bar）——匹配维（小时/星期/ATR 分位）缺一不可，不退化为"
                "全域均匀抽样（审核三 S3）" + pool_note)
        out_idx.append(int(cands[rng.integers(0, len(cands))]))
    out = triggers.copy()
    out["time"] = times.iloc[out_idx].to_numpy()
    out["bar"] = out_idx
    return out


def atr_series_for(bars):
    """bars 的 ATR14（与引擎同口径；本模块不 import 引擎运行路径）。"""
    from mt5quant.backtest.engine_a import atr_series
    return atr_series(bars)


# ---------------------------------------------------------------- Δp 检验

def _by_day(values: pd.Series, days: pd.Series) -> pd.Series:
    """同日聚合（均值）——样本单位为触发日（D52）。"""
    return pd.Series(values).groupby(days).mean()


def delta_p_test(real_labels: pd.DataFrame, null_labels: pd.DataFrame, *,
                 block: str = "month", sims: int, seed: int) -> dict:
    """单侧 Δp 检验：触发组日赢率均值 − 随机组日赢率均值（样本单位=
    触发日，D52）；p 值按**块内置换**（把两组的日赢率在各自块内随机
    重新分组，sims 次；单侧：置换统计量 ≥ 观测 Δ 的比例，+1 修正
    (1+k)/(1+B)）。

    block（T101/P-02 处置）：置换块长——"month"=月内（2026-10-28
    报告 §1 声明的方法变体）；"quarter"=日历季度内（Q-DEC：1-3/
    4-6/7-9/10-12 月；T100 重跑发现月内置换在 7 周尺度成簇结构下
    误报率 11%>5%，季度块为 v3 标定的候选正式方法）。季度从不拆分
    月份——数据只落在互不同月的月份时两种块等价。

    向量化实现（与逐 sim 逐块 shuffle 同一统计过程：统计量只依赖
    Σ块(组A子集和)，按块独立随机子集等价于逐块 shuffle；同 seed 确定）。
    real_labels / null_labels：含 time、outcome 的标签表（win 计 1；
    skipped 剔除——S7）。sims 须为 ≥1 整数（T98 audit P-11：显式校验，
    不让 sims=0 静默产出 p=NaN 之类）。
    """
    if isinstance(sims, bool) or not isinstance(sims, (int, np.integer)) \
            or int(sims) < 1:
        raise ValueError(f"sims 须为 ≥1 整数，收到 {sims!r}")
    if block not in ("month", "quarter"):
        raise ValueError(f"block 只认 'month'/'quarter'，收到 {block!r}"
                         "（T101：块长显式校验——其他值不得静默按月处理）")
    rng = np.random.default_rng(seed)

    def _day_winrates(df: pd.DataFrame) -> pd.Series:
        d = df[df["outcome"].isin(_USABLE_OUTCOMES)]
        if len(d) == 0:
            return pd.Series(dtype=float)
        t = pd.to_datetime(d["time"])
        win = (d["outcome"] == "win").astype(float)
        return win.groupby(t.dt.date).mean()

    r = _day_winrates(real_labels)
    nul = _day_winrates(null_labels)
    if len(r) == 0 or len(nul) == 0:
        return {"delta_p": float("nan"), "p_value": 1.0,
                "n_days_real": int(len(r)), "n_days_null": int(len(nul)),
                "block": block, "sims": int(sims)}
    r_arr, n_arr = r.to_numpy(), nul.to_numpy()
    freq = "Q" if block == "quarter" else "M"
    r_months = pd.PeriodIndex(pd.to_datetime(list(r.index)), freq=freq)
    n_months = pd.PeriodIndex(pd.to_datetime(list(nul.index)), freq=freq)
    obs = float(r_arr.mean() - n_arr.mean())

    months = sorted(set(r_months) | set(n_months))
    k_list, pool_list, sum_pool = [], [], 0.0
    for m in months:
        pool = np.concatenate([r_arr[r_months == m], n_arr[n_months == m]])
        k = int((r_months == m).sum())
        k_list.append(k)
        pool_list.append(pool)
        sum_pool += float(pool.sum())
    n_a = int(sum(k_list))
    n_b = int(len(r_arr) + len(n_arr) - n_a)
    # 统计量 = ΣA·(1/N_A + 1/N_B) − S_total/N_B（逐 sim 等价推导）
    t_obs = float(r_arr.sum())
    coef = (1.0 / n_a + 1.0 / n_b)
    t_sim = np.empty(sims)
    for mi, (k, pool) in enumerate(zip(k_list, pool_list)):
        rnd = rng.random((sims, len(pool)))
        mask = np.argsort(rnd, axis=1) < k
        part = np.where(mask, pool[None, :], 0.0).sum(axis=1)
        if mi == 0:
            t_sim = part
        else:
            t_sim = t_sim + part
    stat_sim = t_sim * coef - sum_pool / n_b
    k_ge = int((stat_sim >= obs).sum())
    p = float((1 + k_ge) / (1 + sims))
    return {"delta_p": obs, "p_value": p,
            "n_days_real": n_a, "n_days_null": n_b,
            "block": block, "sims": int(sims)}


def holm(pvalues: dict[str, float]) -> dict[str, float]:
    """Holm 逐步下调校正（与手算一致：排序后 p_(i)×(m−i) 取运行最大，
    截到 1）。

    T98 audit P-11：p 值须为 [0,1] 内有限数（NaN/Inf/越界 → ValueError
    ——NaN 的比较会静默穿过校正）；空 dict 维持既有约定返回 {}。
    """
    for name, p in pvalues.items():
        if isinstance(p, bool) or not isinstance(p, (int, float, np.floating)):
            raise ValueError(f"p 值须为数值（{name}={p!r}）")
        p = float(p)
        if not math.isfinite(p):
            raise ValueError(f"p 值须为有限数（{name}={p!r}）")
        if not 0.0 <= p <= 1.0:
            raise ValueError(f"p 值须在 [0,1] 内（{name}={p!r}）")
    items = sorted(pvalues.items(), key=lambda kv: kv[1])
    m = len(items)
    out: dict[str, float] = {}
    running = 0.0
    for i, (name, p) in enumerate(items):
        adj = min(1.0, p * (m - i))
        running = max(running, adj)
        out[name] = running
    return out


def plant_delta(labels: pd.DataFrame, delta: float, *,
                seed: int) -> pd.DataFrame:
    """正对照：在标签上植入已知 Δp——随机把占比 delta 的 loss 改成 win
    （净收益取现有 win 的均值量级，保持单位自洽）。确定性：同 seed 同
    结果。

    翻转按**位置**索引（T98 audit P-11：flip 是 np.flatnonzero 的位置
    数组，旧 .loc 在非 RangeIndex 上会改错行/抛错）。
    """
    rng = np.random.default_rng(seed)
    out = labels.copy()
    loss_idx = np.flatnonzero((out["outcome"] == "loss").to_numpy())
    if delta <= 0 or len(loss_idx) == 0:
        return out
    n_flip = min(len(loss_idx), int(round(delta * len(out))))
    flip = rng.choice(loss_idx, size=n_flip, replace=False)
    win_r = out.loc[out["outcome"] == "win", "net_r"]
    r_val = float(win_r.mean()) if len(win_r) else 1.9
    col_outcome = out.columns.get_loc("outcome")
    col_net_r = out.columns.get_loc("net_r")
    out.iloc[flip, col_outcome] = "win"
    out.iloc[flip, col_net_r] = r_val
    return out


def brier_skill(p_pred, outcome, p_base) -> float:
    """Brier 技能分：相对基准率预测的改进（>0 更准；≤0 无信息）。

    outcome 为布尔（win=1）。SS = 1 − BS_model / BS_base。

    T98 audit P-11 输入校验（NaN/Inf/空/形状/概率区间 → ValueError）：
    p_pred 与 outcome 须等长非空、p_pred/p_base 在 [0,1]、outcome ∈ {0,1}
    ——不校验时 NaN 会经比较静默变号。
    """
    try:
        p = np.asarray(p_pred, dtype=float)
        y = np.asarray(outcome, dtype=float)
    except (TypeError, ValueError) as e:
        raise ValueError(
            f"p_pred/outcome 须可转为数值数组：{e}") from e
    if p.size == 0 or y.size == 0:
        raise ValueError("p_pred/outcome 不能为空")
    if p.shape != y.shape:
        raise ValueError(
            f"p_pred 与 outcome 形状不一致：{p.shape} vs {y.shape}"
            "（不校验会广播成错误结果）")
    if not np.isfinite(p).all():
        raise ValueError(f"p_pred 含非有限数（NaN/Inf）：{p!r}")
    if (p < 0.0).any() or (p > 1.0).any():
        raise ValueError(f"p_pred 越界：概率须在 [0,1]，范围 "
                         f"[{p.min()}, {p.max()}]")
    if not np.isfinite(y).all():
        raise ValueError(f"outcome 含非有限数（NaN/Inf）：{y!r}")
    if not np.isin(y, (0.0, 1.0)).all():
        raise ValueError(f"outcome 须为 0/1（win=1），收到非常取值 "
                         f"{sorted(set(y[~np.isin(y, (0.0, 1.0))].tolist()))}")
    if isinstance(p_base, bool) or not isinstance(
            p_base, (int, float, np.floating)):
        raise ValueError(f"p_base 须为数值：{p_base!r}")
    b0 = float(p_base)
    if not math.isfinite(b0) or not 0.0 <= b0 <= 1.0:
        raise ValueError(f"p_base 须为 [0,1] 内有限数：{p_base!r}")
    b = float(np.mean((np.full_like(y, b0) - y) ** 2))
    m = float(np.mean((p - y) ** 2))
    if b <= 0:
        return 0.0
    return 1.0 - m / b
