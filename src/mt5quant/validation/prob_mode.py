"""概率模式评估（T75，蓝图 P 线 §3.2 / 决策 D42；在 T66 事件模式框架上实现）。

P1 主检验：Δp = 局面触发赢率 − 匹配随机时点赢率，单侧；同一天的多个
触发先按日聚合；p 值按月做块自助；同一轮全部预注册假设用 Holm 校正。
零假设：触发时刻随机化（保持纽约小时、星期与 ATR 分位分布）；正对照：
在标签上植入已知 Δp。P2 校准否决项：Brier 技能分。

**本模块只用合成数据 / 随机时点 / 随机化触发 / 植入效应做标定与测试**
（蓝图 v4 §2 禁止事项：T75 标定报告与 T77 轮次草案批准之前，不在真实
局面触发上计算任何标签、拟合或 Δp）。评估代码（D45：研究角色只读）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

NY_TZ = "America/New_York"


# ---------------------------------------------------------------- 随机化

def randomize_triggers(triggers: pd.DataFrame, bars: pd.DataFrame, *,
                       seed: int,
                       match=("ny_hour", "weekday", "atr_quantile")
                       ) -> pd.DataFrame:
    """零假设触发：随机时刻，保持纽约小时 / 星期 / ATR 分位的分布。

    做法：把候选 bar 池按（小时、星期）分桶，桶内均匀随机抽时刻替换每
    个原触发（桶内任何 bar 等概率——分布与原触发的桶分布一致，桶内
    时刻均匀是零假设）。ATR 分位作为第三个匹配维：桶键加入触发 ATR
    分位档（高/低，按池中位分档）。确定性：同一 seed 同一结果。
    """
    rng = np.random.default_rng(seed)
    times = pd.to_datetime(bars["time"])
    ny = times.dt.tz_localize("UTC").dt.tz_convert(NY_TZ)
    hour = ny.dt.hour.to_numpy()
    weekday = ny.dt.weekday.to_numpy()
    from mt5quant.backtest.engine_a import atr_series
    atr = atr_series(bars).to_numpy()
    q = np.full(len(bars), np.nan)
    finite = np.isfinite(atr)
    if finite.any():
        med = float(np.median(atr[finite]))
        q[finite] = np.where(atr[finite] >= med, 1.0, 0.0)
    pool = {}
    for i in range(len(bars)):
        if not np.isfinite(q[i]):
            continue
        pool.setdefault((int(hour[i]), int(weekday[i])), []).append(i)
    trig_times = pd.to_datetime(triggers["time"])
    t_ny = trig_times.dt.tz_localize("UTC").dt.tz_convert(NY_TZ)
    out_idx = []
    for k, t in enumerate(trig_times):
        key = (int(t_ny.iloc[k].hour), int(t_ny.iloc[k].weekday()))
        cands = pool.get(key)
        if not cands:
            # 无同桶候选（罕见：触发在池外时刻）——退化为全域均匀抽样
            cands = [i for i in range(len(bars)) if np.isfinite(q[i])]
        out_idx.append(int(cands[rng.integers(0, len(cands))]))
    out = triggers.copy()
    out["time"] = times.iloc[out_idx].to_numpy()
    out["bar"] = out_idx
    return out


# ---------------------------------------------------------------- Δp 检验

def _by_day(values: pd.Series, days: pd.Series) -> pd.Series:
    """同日聚合（均值）——样本单位为触发日（D52）。"""
    return pd.Series(values).groupby(days).mean()


def delta_p_test(real_labels: pd.DataFrame, null_labels: pd.DataFrame, *,
                 block: str = "month", sims: int, seed: int) -> dict:
    """单侧 Δp 检验：触发组日赢率均值 − 随机组日赢率均值（样本单位=
    触发日，D52）；p 值按**月内置换**（把两组的日赢率在各自月份内随机
    重新分组，sims 次；单侧：置换统计量 ≥ 观测 Δ 的比例）。

    real_labels / null_labels：含 time、outcome 的标签表（win 计 1）。
    返回 {"delta_p", "p_value", "n_days_real", "n_days_null",
    "block", "sims"}。
    """
    rng = np.random.default_rng(seed)

    def _day_winrates(df: pd.DataFrame) -> pd.Series:
        t = pd.to_datetime(df["time"])
        win = (df["outcome"] == "win").astype(float)
        return win.groupby(t.dt.date).mean()

    r = _day_winrates(real_labels)
    nul = _day_winrates(null_labels)
    obs = float(r.mean() - nul.mean())
    r_months = pd.PeriodIndex(pd.to_datetime(
        [d for d in r.index]), freq="M")
    n_months = pd.PeriodIndex(pd.to_datetime(
        [d for d in nul.index]), freq="M")
    counts = np.empty(sims)
    r_arr, n_arr = r.to_numpy(), nul.to_numpy()
    for s_i in range(sims):
        d_r, d_n = [], []
        for m in set(r_months) | set(n_months):
            pooled = np.concatenate([
                r_arr[r_months == m], n_arr[n_months == m]])
            rng.shuffle(pooled)
            k = int((r_months == m).sum())
            d_r.append(pooled[:k])
            d_n.append(pooled[k:])
        a = np.concatenate(d_r) if d_r else np.array([0.0])
        b = np.concatenate(d_n) if d_n else np.array([0.0])
        counts[s_i] = a.mean() - b.mean()
    p = float((counts >= obs).mean()) if sims else 1.0
    return {"delta_p": obs, "p_value": p,
            "n_days_real": int(len(r)), "n_days_null": int(len(nul)),
            "block": block, "sims": int(sims)}


def holm(pvalues: dict[str, float]) -> dict[str, float]:
    """Holm 逐步下调校正（与手算一致：排序后 p_(i)×(m−i) 取运行最大，
    截到 1）。"""
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
    结果。"""
    rng = np.random.default_rng(seed)
    out = labels.copy()
    loss_idx = np.flatnonzero((out["outcome"] == "loss").to_numpy())
    if delta <= 0 or len(loss_idx) == 0:
        return out
    n_flip = min(len(loss_idx), int(round(delta * len(out))))
    flip = rng.choice(loss_idx, size=n_flip, replace=False)
    win_r = out.loc[out["outcome"] == "win", "net_r"]
    r_val = float(win_r.mean()) if len(win_r) else 1.9
    out.loc[flip, "outcome"] = "win"
    out.loc[flip, "net_r"] = r_val
    return out


def brier_skill(p_pred, outcome, p_base) -> float:
    """Brier 技能分：相对基准率预测的改进（>0 更准；≤0 无信息）。

    outcome 为布尔（win=1）。SS = 1 − BS_model / BS_base。
    """
    y = np.asarray(outcome, dtype=float)
    p = np.asarray(p_pred, dtype=float)
    b = float(np.mean((np.full_like(y, p_base) - y) ** 2))
    m = float(np.mean((p - y) ** 2))
    if b <= 0:
        return 0.0
    return 1.0 - m / b
