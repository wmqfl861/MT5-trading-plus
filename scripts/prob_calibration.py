"""T75 概率模式正式标定（D35 已批准；蓝图 P 线 §3.2 / 计划 T75 设计）。

标定对象：`mt5quant.validation.prob_mode` 的 delta_p_test / holm /
plant_delta / brier_skill——**只用随机时点、随机化触发、成簇平移与
植入效应**，不生成任何真实局面触发的标签（蓝图 v4 §2）。随机时点的
标签池 parquet 只放本次运行的临时目录，merge 后删除；
pool.meta.json（来源指纹）保留（P-12）。

T98 审核修正（2026-10-29）：
- P-02 成簇负对照 real/null **均只用平移后时刻**（同种子两次独立
  整周平移+独立置换，宽范围 0..51 附加周，NY 时区往返保本地小时/
  星期，越界丢弃不 nearest 堆末端）。
- P-03 样本单位=触发日：名义 n（有放回抽日次数）≠实际不同日数，
  记录 delta_p_test 的 n_days_real/n_days_null；pool 源指纹入
  run fingerprint；build_costs 传 swap_ref_price=全序列最后收盘
  （与正式 P1 同口径，bt.py T83/K5）。
- P-12 run 各 phase 写预期完备集 manifest.json，merge 精确匹配
  （缺/多/重复行键一律拒绝）。

方法变更（先于运行声明）：D42 写定"p 值按月做块自助"；现行实现为
**月内置换**（两组日赢率在各自月份内重新分组）。审核三 S9 的模拟
（每簇 10/20 个交易日、状态赢率 0.20/0.60 与 0.15/0.65）显示月内置换
不劣于块自助（6.0%/4.5% vs 4.0%/6.0%，名义 5%）；本报告按月内置换
标定并附成簇负对照实测。正式运行 B ≥ 10,000（+1 修正后分辨率
1/(1+B) ≤ 0.01%）。

用法（分片；并发进程数 ≤ CPU 核数一半）：
    .venv/Scripts/python.exe scripts/prob_calibration.py pool
    .venv/Scripts/python.exe scripts/prob_calibration.py run --phase neg  --shard 0/4 ...
    .venv/Scripts/python.exe scripts/prob_calibration.py run --phase pos  --shard 0/4 ...
    .venv/Scripts/python.exe scripts/prob_calibration.py run --phase cluster --shard 0/1
    .venv/Scripts/python.exe scripts/prob_calibration.py run --phase brier --shard 0/1
    .venv/Scripts/python.exe scripts/prob_calibration.py merge
产物：data/prob/t75-calibration/<stamp>/（分片 JSON + 汇总 summary.json）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import numpy as np
import pandas as pd

from mt5quant.backtest.engine_a import atr_series
from mt5quant.prob.labels import Bracket, label_triggers
from mt5quant.strategies.library.prob.actions import ActionParams
from mt5quant.strategies.library.prob.situations import Approach
from mt5quant.validation.prob_mode import (
    brier_skill, delta_p_test, holm, plant_delta,
)

REPO = Path(__file__).resolve().parents[1]
OUT_ROOT = REPO / "data" / "prob" / "t75-calibration"
SYMBOL = "XAUUSD"
TF = "H1"
DATA_ROOT = "data_research"
B_SIMS = 10_000                       # 正式运行置换次数（+1 修正分辨率 1e-4）
# T101（P-02 处置）：v3 标定的置换块长——季度块（T100 重跑成簇 w7 档
# 误报 11%>5%，月内置换低估跨月成簇方差）；随 v3 报告采纳状态决定
# 正式 P1（run_p1_card）是否同步切换
BLOCK = "quarter"
N_POOL = 4_200                        # 随机时点标签池大小（≥ 最大 n=4000）
BRACKET = Bracket(sl_atr=1.0, tp_atr=2.0, horizon_bars=24)
POOL_SEED = 20261028
# 设计（报告开头写清；与计划 T75 一致）
NEG_NS = (500, 1000, 2000)
NEG_SEEDS = 200
POS_DELTAS = (0.02, 0.03, 0.05)
POS_NS = (500, 1000, 2000, 4000)
POS_SEEDS = 100
HOLM_HYPS = 12                        # 3 局面 × 4 动作
CLUSTER_SHIFT_WEEKS = (3, 7)
CLUSTER_SEEDS = 100
CLUSTER_EXTRA_WEEKS_MAX = 51          # 附加整周偏移抽值 0..51（P-02 宽范围）
BRIER_SEEDS = 200                     # P2 校准否证种子数


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _run_fingerprint(pool_source_fp: str = "") -> str:
    """运行指纹：方法源码 + 设计常量 + pool 源指纹（P-03——同方法下
    标签池的日赢率分布变了也算不同一次运行）。"""
    srcs = [
        Path("src/mt5quant/validation/prob_mode.py"),
        Path("src/mt5quant/prob/labels.py"),
        Path("src/mt5quant/prob/p1.py"),
        Path(__file__),
    ]
    h = hashlib.sha256()
    for p in srcs:
        h.update(p.read_bytes())
    h.update(json.dumps({
        "B": B_SIMS, "N_POOL": N_POOL, "block": BLOCK, "bracket": vars(BRACKET) |
         {k: v for k, v in vars(BRACKET).items()},
        "neg": [NEG_NS, NEG_SEEDS], "pos": [POS_DELTAS, POS_NS, POS_SEEDS],
        "holm": HOLM_HYPS,
        "cluster": [CLUSTER_SHIFT_WEEKS, CLUSTER_SEEDS,
                    CLUSTER_EXTRA_WEEKS_MAX],
        "brier": BRIER_SEEDS,
        "pool_source_fingerprint": pool_source_fp,
    }, sort_keys=True).encode())
    return h.hexdigest()


def _pool_path(stamp: str) -> Path:
    return OUT_ROOT / stamp / "pool.parquet"


def _load_bars():
    from run_backtest import load_series
    return load_series(DATA_ROOT, SYMBOL, TF, None)


def _costs_and_sizes(swap_ref_price: float | None = None):
    """标定成本口径与正式 P1 对齐：swap_ref_price=全序列最后收盘价
    （bt.py T83/K5 同一约定），调用方从装载的全序列 bars 传入。"""
    from run_backtest import (
        SESSIONS_PATH, build_costs, entry_policy_for, resolve_contract_size,
    )
    from mt5data.sessions import load_session_policy
    costs, ver = build_costs(DATA_ROOT, SYMBOL, 0.1, 0.0,
                             research_store=True,
                             swap_ref_price=swap_ref_price)
    sess = load_session_policy(str(SESSIONS_PATH))
    policy = entry_policy_for(costs, SYMBOL, sess)
    cs = resolve_contract_size(DATA_ROOT, SYMBOL, None)
    return costs, policy, cs, ver


# ---------------------------------------------------------------- pool

def build_pool(stamp: str) -> Path:
    """随机时点标签池：N_POOL 个（小时、星期、ATR 档）桶内随机时刻，
    引擎 A 同口径打标。池子只落本次运行目录，merge 后删除。"""
    bars = _load_bars()
    costs, policy, cs, cost_ver = _costs_and_sizes(
        swap_ref_price=float(bars["close"].iloc[-1]))
    rng = np.random.default_rng(POOL_SEED)
    # 桶结构：真实计数的小时分布作权重，随机抽时刻（不连接触发条件）
    times = pd.to_datetime(bars["time"])
    ny = times.dt.tz_localize("UTC").dt.tz_convert("America/New_York")
    hour_pool = ny.dt.hour.to_numpy()
    rng_hours = rng.choice(hour_pool, size=N_POOL, replace=True)
    # 在给定纽约小时内的 bar 中均匀抽一根（保持小时分布 + 时刻均匀）
    idx_by_hour = {h: np.flatnonzero(hour_pool == h) for h in np.unique(hour_pool)}
    picks = np.array([idx_by_hour[h][rng.integers(0, len(idx_by_hour[h]))]
                      for h in rng_hours])
    picks.sort()
    triggers = pd.DataFrame({
        "time": times.iloc[picks].to_numpy(),
        "situation": "POOL", "action": "A0", "side": 1, "level": np.nan})
    labeled = label_triggers(
        bars, triggers, costs, symbol=SYMBOL, timeframe=TF,
        bracket=BRACKET, contract_size=cs, volume_lots=1.0,
        session_policy=policy)
    out = _pool_path(stamp)
    out.parent.mkdir(parents=True, exist_ok=True)
    labeled.to_parquet(out, index=False)
    pool_fp = _pool_source_fingerprint(labeled)
    meta = {"pool_seed": POOL_SEED, "n_pool": N_POOL, "cost_ver": cost_ver,
            "bracket": {"sl_atr": BRACKET.sl_atr, "tp_atr": BRACKET.tp_atr,
                        "horizon_bars": BRACKET.horizon_bars},
            "pool_source_fingerprint": pool_fp,
            "fingerprint": _run_fingerprint(pool_fp)}
    (out.parent / "pool.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✓ 池 {len(labeled)} 条 → {out}（{meta['cost_ver']}）")
    return out


def _pool_source_fingerprint(labeled: pd.DataFrame) -> str:
    """标签池来源指纹（P-03）：日赢率分布摘要（20 桶直方图）+ 天数 +
    池种子/池大小的 sha256——同方法下区分不同来源的标签池。"""
    days = _day_winrate_table(labeled)
    hist, _ = np.histogram(days["winrate"].to_numpy(dtype=float),
                           bins=np.linspace(0.0, 1.0, 21))
    payload = json.dumps({
        "hist": [int(x) for x in hist], "n_days": int(len(days)),
        "pool_seed": POOL_SEED, "n_pool": int(len(labeled)),
    }, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _day_winrate_table(pool: pd.DataFrame) -> pd.DataFrame:
    usable = pool[pool["outcome"].isin(["win", "loss", "time"])].copy()
    usable["day"] = pd.to_datetime(usable["time"]).dt.date
    g = usable.groupby("day")["outcome"].apply(
        lambda s: float((s == "win").mean()))
    return g.reset_index().rename(columns={"outcome": "winrate"})


def _sample_labels(pool_days: pd.DataFrame, n: int, seed: int
                   ) -> pd.DataFrame:
    """从池中**有放回按日抽** n 次，重建成 labels 帧（day → time/outcome）。

    P-03：n 是**名义**触发次数——有放回抽样同一日可重复出现，实际
    不同触发日数 ≤ n；记录与锚点解释一律以 delta_p_test 返回的
    n_days_real / n_days_null 为准（phase_neg/phase_pos 已随行记录）。"""
    rng = np.random.default_rng(seed)
    take = rng.integers(0, len(pool_days), size=n)
    rows = []
    for k, i in enumerate(take):
        d = pool_days.iloc[i]
        rows.append({"time": pd.Timestamp(d["day"]) + pd.Timedelta(hours=12),
                     "outcome": "win" if rng.random() < d["winrate"]
                     else "loss", "net_r": 1.9, "situation": "S1",
                     "action": "A3", "side": 1, "level": 0.0})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- phases

def phase_neg(pool: pd.DataFrame, shard_i: int, shard_n: int, stamp: str
              ) -> list[dict]:
    out = []
    jobs = [(n, seed) for n in NEG_NS for seed in range(NEG_SEEDS)]
    my = jobs[shard_i::shard_n]
    for n, seed in my:
        real = _sample_labels(pool, n, seed=900_000 + seed)
        null = _sample_labels(pool, n, seed=800_000 + seed)
        res = delta_p_test(real, null, block=BLOCK, sims=B_SIMS, seed=seed)
        out.append({"kind": "neg_single", "n": n, "seed": seed,
                    "p": res["p_value"],
                    "n_days_real": res["n_days_real"],
                    "n_days_null": res["n_days_null"]})
    return out


def phase_neg_holm(pool: pd.DataFrame, shard_i: int, shard_n: int
                   ) -> list[dict]:
    out = []
    seeds = range(NEG_SEEDS)
    for seed in seeds[shard_i::shard_n]:
        pv = {}
        for h in range(HOLM_HYPS):
            real = _sample_labels(pool, NEG_NS[1], seed=700_000 + seed * 20 + h)
            null = _sample_labels(pool, NEG_NS[1], seed=600_000 + seed)
            pv[f"H{h:02d}"] = delta_p_test(real, null, block=BLOCK,
                                           sims=B_SIMS,
                                           seed=seed * 20 + h)["p_value"]
        adj = holm(pv)
        out.append({"kind": "neg_holm", "n": NEG_NS[1], "seed": seed,
                    "reject_any": any(v < 0.05 for v in adj.values())})
    return out


def phase_pos(pool: pd.DataFrame, shard_i: int, shard_n: int
              ) -> list[dict]:
    out = []
    jobs = [(d, n, seed) for d in POS_DELTAS for n in POS_NS
            for seed in range(POS_SEEDS)]
    my = jobs[shard_i::shard_n]
    for d, n, seed in my:
        real = plant_delta(_sample_labels(pool, n, seed=500_000 + seed), d,
                           seed=seed)
        null = _sample_labels(pool, n, seed=400_000 + seed)
        res = delta_p_test(real, null, block=BLOCK, sims=B_SIMS, seed=seed)
        out.append({"kind": "pos", "delta": d, "n": n, "seed": seed,
                    "p": res["p_value"], "dp_hat": res["delta_p"],
                    "n_days_real": res["n_days_real"],
                    "n_days_null": res["n_days_null"]})
    return out


def _shift_weeks_ny(times, weeks: int) -> pd.Series:
    """整周平移，NY 时区往返保**本地小时与星期**（P-02）。

    触发时刻是 naive UTC：UTC→NY→墙钟加 weeks 周→回 UTC（DST 不存在
    的本地时刻顺延一小时；歧义时刻取较早一次）→去 tz。整周粒度下墙钟
    加周与 tz-aware DateOffset(weeks=N) 等价，但绕开后者在 DST 边界的
    抛错（向量化路径会 raise NonExistentTimeError）。"""
    ny = pd.DatetimeIndex(pd.to_datetime(times)).tz_localize(
        "UTC").tz_convert("America/New_York")
    wall = ny.tz_localize(None) + pd.DateOffset(weeks=int(weeks))
    # 不存在（春季拨快被跳过）→ 顺延到第一个存在时刻；歧义（秋季拨回
    # 出现两次）→ True=DST 侧=较早一次（DatetimeIndex 不认 "earliest"）
    back = wall.tz_localize("America/New_York", nonexistent="shift_forward",
                            ambiguous=True)
    return pd.Series(back.tz_convert("UTC").tz_localize(None))


def _shifted_triggers(trig: pd.DataFrame, times_idx: pd.Index, *, weeks: int,
                      perm_state: int) -> pd.DataFrame:
    """种子化独立平移+独立置换（P-02）：整周平移后映射到 bar。

    平移后早于首根或晚于末根的触发**丢弃**——不做 nearest 末端堆积；
    存活时刻映射到最近 bar（查询已在序列范围内，最近 bar 必在界内）。"""
    perm = trig.sample(frac=1.0, random_state=perm_state).reset_index(
        drop=True)
    shifted = _shift_weeks_ny(perm["time"], weeks).to_numpy()
    keep = (shifted >= times_idx[0]) & (shifted <= times_idx[-1])
    kept = perm[keep].reset_index(drop=True)
    if len(kept) == 0:
        return kept
    pos = times_idx.get_indexer(pd.DatetimeIndex(shifted[keep]),
                                method="nearest")
    kept["time"] = np.asarray(times_idx)[pos]
    return kept


def phase_cluster(shard_i: int, shard_n: int, stamp: str) -> list[dict]:
    """成簇负对照（审核三；P-02 重写）。

    计数得到真实 S1 触发时刻（只数不看结果）。**real 与 null 均只用
    平移后时刻的标签**：同种子下各做一次**独立**整周平移（基础 wk 周
    + 随机抽 0..51 附加周，NY 时区往返保本地小时/星期）+ **独立**行
    置换——null 不是 real 平移时刻、也不是原始触发时刻的子集（旧实现
    的嵌套弱对照已删）。平移越界丢弃。名义 5% 下误报率应 ≤5%。"""
    from mt5quant.prob.counting import level_rows_for_situation
    from mt5quant.strategies.library.prob.situations import find_level_events
    from run_backtest import load_series
    bars_full = load_series(DATA_ROOT, SYMBOL, TF, None)
    costs, policy, cs, _ = _costs_and_sizes(
        swap_ref_price=float(bars_full["close"].iloc[-1]))
    approach = Approach(lookback_bars=24, min_distance_atr=1.0,
                        touch_atr=0.25)
    aparams = ActionParams(breakout_atr=0.25, retest_window_bars=6)
    atr = atr_series(bars_full)
    levels = level_rows_for_situation(bars_full, "S1", grid_step=50.0,
                                      approach=approach, atr=atr)
    events = find_level_events(bars_full, levels, situation="S1",
                               approach=approach, atr=atr)
    from mt5quant.strategies.library.prob.actions import action_triggers
    trig_all = action_triggers(bars_full, events, params=aparams, atr=atr)
    if len(trig_all) == 0:
        raise RuntimeError("S1 触发为空——无法做成簇负对照")
    trig = trig_all[["time", "situation", "action", "side", "level"]].copy()
    kw = dict(symbol=SYMBOL, timeframe=TF, bracket=BRACKET,
              contract_size=cs, volume_lots=1.0, session_policy=policy)
    out = []
    times_idx = pd.Index(bars_full["time"])

    def _labels(trig_df: pd.DataFrame) -> pd.DataFrame:
        if len(trig_df) == 0:      # 越界丢弃后可为空：给可检验的空标签帧
            return pd.DataFrame({"time": pd.Series(dtype="object"),
                                 "outcome": pd.Series(dtype="object")})
        return label_triggers(bars_full, trig_df, costs, **kw)

    for wk in CLUSTER_SHIFT_WEEKS:
        for seed in range(CLUSTER_SEEDS)[shard_i::shard_n]:
            rng = np.random.default_rng(seed)
            extra_real = int(rng.integers(0, CLUSTER_EXTRA_WEEKS_MAX + 1))
            extra_null = int(rng.integers(0, CLUSTER_EXTRA_WEEKS_MAX + 1))
            perm_real = int(rng.integers(0, 2**31 - 1))
            perm_null = int(rng.integers(0, 2**31 - 1))
            real_trig = _shifted_triggers(
                trig, times_idx, weeks=wk + extra_real, perm_state=perm_real)
            null_trig = _shifted_triggers(
                trig, times_idx, weeks=wk + extra_null, perm_state=perm_null)
            real = _labels(real_trig)
            null = _labels(null_trig)
            res = delta_p_test(real, null, block=BLOCK, sims=B_SIMS, seed=seed)
            out.append({"kind": "cluster_neg", "weeks": wk, "seed": seed,
                        "shift_weeks_real": wk + extra_real,
                        "shift_weeks_null": wk + extra_null,
                        "n_real": int(len(real)), "n_null": int(len(null)),
                        "n_days_real": res["n_days_real"],
                        "n_days_null": res["n_days_null"],
                        "p": res["p_value"]})
    return out


def phase_brier(pool: pd.DataFrame, shard_i: int, shard_n: int
                ) -> list[dict]:
    """P2 校准否决项：无信息特征下 Brier 技能分 > 0 的比例（应≈0）与
    植入信息下的检出。技能分 = 相对基准率的 Brier 改进。"""
    out = []
    seeds = range(BRIER_SEEDS)
    for seed in seeds[shard_i::shard_n]:
        rng = np.random.default_rng(seed)
        # 无信息：预测=基准率（特征不含信息）
        y = (rng.random(2000) < 0.40).astype(float)
        s_uninf = brier_skill(np.full(2000, 0.40), y, 0.40)
        # 植入信息：预测与真实相关（p 随 y 升高）
        p_pred = np.where(y == 1, 0.55, 0.25)
        s_inf = brier_skill(p_pred, y, 0.40)
        out.append({"kind": "brier", "seed": seed,
                    "s_uninformative": s_uninf, "s_planted": s_inf})
    return out


# ---------------------------------------------------------------- run/merge

def _job_key(row: dict) -> str:
    """行的规范键 kind|条件…|seed——manifest 与 merge 用同一推导。"""
    k = row.get("kind")
    if k == "neg_single":
        return f"neg_single|{int(row['n'])}|{int(row['seed'])}"
    if k == "neg_holm":
        return f"neg_holm|{int(row['seed'])}"
    if k == "pos":
        return f"pos|{float(row['delta']):g}|{int(row['n'])}|{int(row['seed'])}"
    if k == "cluster_neg":
        return f"cluster_neg|{int(row['weeks'])}|{int(row['seed'])}"
    if k == "brier":
        return f"brier|{int(row['seed'])}"
    raise ValueError(f"未知 kind：{k!r}")


def _job_manifest() -> dict:
    """预期 (phase, condition, seed) 完备集（P-12）：从设计常量推导的
    本次运行应产出的全部行键。run 各 phase 时落盘 stamp 目录
    manifest.json；merge 校验分片并集与它精确匹配。"""
    jobs = []
    for n in NEG_NS:
        for s in range(NEG_SEEDS):
            jobs.append({"kind": "neg_single", "n": int(n), "seed": int(s)})
    for s in range(NEG_SEEDS):
        jobs.append({"kind": "neg_holm", "n": int(NEG_NS[1]),
                     "seed": int(s)})
    for dlt in POS_DELTAS:
        for n in POS_NS:
            for s in range(POS_SEEDS):
                jobs.append({"kind": "pos", "delta": float(dlt),
                             "n": int(n), "seed": int(s)})
    for w in CLUSTER_SHIFT_WEEKS:
        for s in range(CLUSTER_SEEDS):
            jobs.append({"kind": "cluster_neg", "weeks": int(w),
                         "seed": int(s)})
    for s in range(BRIER_SEEDS):
        jobs.append({"kind": "brier", "seed": int(s)})
    return {"version": 1, "jobs": sorted(_job_key(j) for j in jobs)}


def cmd_run(args) -> int:
    stamp = args.stamp or _stamp()
    pool_p = _pool_path(stamp)
    if args.phase != "cluster":
        if not pool_p.is_file():
            print(f"✗ 池不存在：{pool_p}（先跑 pool 子命令）")
            return 1
        pool = pd.read_parquet(pool_p)
        pool_days = _day_winrate_table(pool)
    # P-12：run 即写预期完备集 manifest（幂等；与已有 manifest 不一致
    # 说明同一 stamp 目录混入了不同版本/设计——拒绝）
    mpath = OUT_ROOT / stamp / "manifest.json"
    want = _job_manifest()
    if mpath.is_file():
        try:
            have = json.loads(mpath.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            have = None
            print(f"✗ manifest.json 解析失败：{e}")
        if have != want:
            print("✗ manifest.json 与当前脚本设计常量不一致（同一 stamp"
                  " 目录不得混用不同版本/设计）")
            return 1
    else:
        mpath.parent.mkdir(parents=True, exist_ok=True)
        mpath.write_text(json.dumps(want, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    # P-03：run 指纹绑定 pool 源指纹（同 stamp 的 pool.meta.json）
    pool_fp = ""
    meta_p = OUT_ROOT / stamp / "pool.meta.json"
    if meta_p.is_file():
        try:
            pool_fp = json.loads(
                meta_p.read_text(encoding="utf-8")).get(
                    "pool_source_fingerprint", "")
        except json.JSONDecodeError:
            pool_fp = ""
    t0 = time.time()
    if args.phase == "neg":
        rows = phase_neg(pool_days, args.shard_i, args.shard_n, stamp)
    elif args.phase == "neg-holm":
        rows = phase_neg_holm(pool_days, args.shard_i, args.shard_n)
    elif args.phase == "pos":
        rows = phase_pos(pool_days, args.shard_i, args.shard_n)
    elif args.phase == "cluster":
        rows = phase_cluster(args.shard_i, args.shard_n, stamp)
    elif args.phase == "brier":
        rows = phase_brier(pool, args.shard_i, args.shard_n)
    else:
        print(f"✗ 未知 phase {args.phase}")
        return 1
    out = OUT_ROOT / stamp / f"{args.phase}-s{args.shard_i}of{args.shard_n}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {"phase": args.phase, "shard": [args.shard_i, args.shard_n],
         "fingerprint": _run_fingerprint(pool_fp), "B": B_SIMS,
         "rows": rows}, ensure_ascii=False), encoding="utf-8")
    print(f"✓ {args.phase} 分片 {args.shard_i}/{args.shard_n}：{len(rows)} 行"
          f"（{time.time() - t0:.0f}s）→ {out}")
    return 0


def cmd_merge(args) -> int:
    stamp_dir = OUT_ROOT / args.stamp
    files = sorted(stamp_dir.glob("*-s*of*.json"))
    # P-12：manifest 完备集校验（缺/多/重复一律拒绝）
    mpath = stamp_dir / "manifest.json"
    if not mpath.is_file():
        print("✗ 缺 manifest.json——先以 run 子命令跑齐各 phase")
        return 1
    try:
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"✗ manifest.json 解析失败：{e}")
        return 1
    if manifest != _job_manifest():
        print("✗ manifest.json 与当前脚本设计常量不一致（分片与 merge 用"
              "了不同版本/设计——拒绝）")
        return 1
    expected = set(manifest["jobs"])
    rows = []
    fps = set()
    seen: dict[str, str] = {}
    dup: list[str] = []
    for f in files:
        d = json.loads(f.read_text(encoding="utf-8"))
        fps.add(d["fingerprint"])
        for r in d["rows"]:
            try:
                key = _job_key(r)
            except (KeyError, TypeError, ValueError) as e:
                print(f"✗ {f.name} 有无法识别的行（{e}）")
                return 1
            if key in seen:
                dup.append(f"{key}（{seen[key]} 与 {f.name}）")
            else:
                seen[key] = f.name
            rows.append(r)
    if len(fps) != 1:
        print(f"✗ 分片指纹不一致：{len(fps)} 种")
        return 1
    if dup:
        print(f"✗ 行键重复 {len(dup)} 处（前 10）：")
        for k in dup[:10]:
            print(f"    - {k}")
        return 1
    missing = sorted(expected - set(seen))
    extra = sorted(set(seen) - expected)
    if missing:
        print(f"✗ 缺 {len(missing)} 个预期行键（前 10）：")
        for k in missing[:10]:
            print(f"    - {k}")
    if extra:
        print(f"✗ 多 {len(extra)} 个非预期行键（前 10）：")
        for k in extra[:10]:
            print(f"    - {k}")
    if missing or extra:
        return 1
    df = pd.DataFrame(rows)
    summary: dict = {"stamp": args.stamp, "B": B_SIMS, "rows": len(df),
                     "fingerprint": fps.pop()}

    neg = df[df["kind"] == "neg_single"]
    if len(neg):
        summary["neg_single"] = {
            f"n{n}": {"fpr": float((g["p"] < 0.05).mean()), "seeds": len(g)}
            for n, g in neg.groupby("n")}
    nh = df[df["kind"] == "neg_holm"]
    if len(nh):
        summary["neg_holm"] = {"n": int(nh["n"].iloc[0]) if len(nh) else None,
                               "fpr_joint": float(nh["reject_any"].mean()),
                               "seeds": len(nh)}
    pos = df[df["kind"] == "pos"]
    if len(pos):
        summary["pos"] = {
            f"dp{d}-n{n}": {"power": float((g["p"] < 0.05).mean()),
                            "dp_hat_median": float(g["dp_hat"].median()),
                            "seeds": len(g)}
            for (d, n), g in pos.groupby(["delta", "n"])}
    cl = df[df["kind"] == "cluster_neg"]
    if len(cl):
        summary["cluster_neg"] = {
            f"w{w}": {"fpr": float((g["p"] < 0.05).mean()), "seeds": len(g)}
            for w, g in cl.groupby("weeks")}
    br = df[df["kind"] == "brier"]
    if len(br):
        summary["brier"] = {
            "uninformative_gt0_fraction":
                float((br["s_uninformative"] > 0).mean()),
            "planted_gt0_fraction": float((br["s_planted"] > 0).mean()),
            "seeds": len(br)}
    out = stamp_dir / "summary.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"✓ 汇总 → {out}")
    # 删除标签池（临时产物）；pool.meta.json 保留——P-12：来源指纹与
    # run 指纹的可追溯记录不随池本体销毁
    pool_p = _pool_path(args.stamp)
    if pool_p.is_file() and args.keep_pool is False:
        pool_p.unlink()
        print("— 标签池已删除（pool.meta.json 保留：来源指纹）")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="prob_calibration")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_pool = sub.add_parser("pool", help="建随机时点标签池")
    p_pool.add_argument("--stamp", default=None)
    p_run = sub.add_parser("run", help="跑一个分片")
    p_run.add_argument("--phase", required=True,
                       choices=["neg", "neg-holm", "pos", "cluster", "brier"])
    p_run.add_argument("--shard-i", type=int, default=0)
    p_run.add_argument("--shard-n", type=int, default=1)
    p_run.add_argument("--stamp", default=None)
    p_mrg = sub.add_parser("merge", help="合并分片并汇总")
    p_mrg.add_argument("--stamp", required=True)
    p_mrg.add_argument("--keep-pool", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "pool":
        return 0 if build_pool(args.stamp or _stamp()) else 1
    if args.cmd == "run":
        return cmd_run(args)
    return cmd_merge(args)


if __name__ == "__main__":
    raise SystemExit(main())
