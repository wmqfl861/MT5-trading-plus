"""T75 概率模式正式标定（D35 已批准；蓝图 P 线 §3.2 / 计划 T75 设计）。

标定对象：`mt5quant.validation.prob_mode` 的 delta_p_test / holm /
plant_delta / brier_skill——**只用随机时点、随机化触发、成簇平移与
植入效应**，不生成任何真实局面触发的标签（蓝图 v4 §2）。随机时点的
标签池 parquet 只放本次运行的临时目录，merge 后删除；pool.meta.json、
manifest.json 与冻结的 cluster 输入保留（P-12/P2-07：来源与重建过程
的可追溯证据不随池本体销毁）。

T98 审核修正（2026-10-29）：
- P-02 成簇负对照 real/null **均只用平移后时刻**（同种子两次独立
  整周平移+独立置换，宽范围 0..51 附加周，NY 时区往返保本地小时/
  星期，越界丢弃不 nearest 堆末端）。
- P-03 样本单位=触发日：名义 n（有放回抽日次数）≠实际不同日数，
  记录 delta_p_test 的 n_days_real/n_days_null；build_costs 传
  swap_ref_price=全序列最后收盘（与正式 P1 同口径，bt.py T83/K5）。
- P-12 run 各 phase 写预期完备集 manifest.json，merge 精确匹配
  （缺/多/重复行键一律拒绝）。

T104 批 2 修正（2026-10-30，audit2 P2-06/P2-07）：
- P2-06 平移映射**严格命中合法 bar 或预写有限容差（±1 根 H1 bar）**：
  内部洞/休市/末端之外一律丢弃该触发（不 nearest 补齐、不末端堆积、
  绝不映回原始触发时刻）；周期性触发与整周平移相撞同样丢弃；最终
  disjoint 断言两组存活时刻都与原始触发集合不相交；平移后重证 NY
  小时/分钟/星期与 ATR 资格；匹配桶（小时×星期、ATR 五分位）审计，
  样本充足时 TV 超预写阈值 → 拒绝整个变换；覆盖损失逐类随行记录。
- P2-07 池"指纹"改为**精确有序内容 hash**（labels 全行规范化序列化，
  固定列序+固定排序键）；run manifest v2 扩展绑定：池内容 hash、
  来源行情快照（研究库 bars meta 的 generation/content_sha256 等
  可得字段）、成本/政策版本（cost_ver + sessions sha256）、方法版本
  （BLOCK 及全部设计常量）、依赖文件清单（prob_mode/p1/labels/
  tables/decision+本脚本逐文件 sha256）、cluster 冻结输入字节指纹；
  pool meta 缺/坏、池或冻结输入被替换 → **拒绝运行/合并**（不退空
  串）；cluster 相位只读 pool 阶段一次提取冻结的 S1 触发+行情快照，
  不再重读可变研究库；merge 校验分片指纹与 manifest 全部绑定一致。
  注意：manifest v1 的旧 stamp（t75-formal-v2/v3）与新代码不兼容——
  **这是有意的**（新 stamp 新指纹）；merge 对旧目录明确报"旧版本
  产物，不可在新代码下合并"。

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
产物：data/prob/t75-calibration/<stamp>/（分片 JSON + manifest.json +
summary.json；merge 后删 pool.parquet，保留全部 meta/manifest/冻结输入）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
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
# 正式 P1（run_p1_card）是否同步切换。方法版本进 manifest 绑定（P2-07）
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

# ---- P2-06：平移映射的预写规则常量（先于运行声明，不看标签/p 值挑变换）
SHIFT_MAP_TOLERANCE = pd.Timedelta(hours=1)   # ±1 根 H1 bar 的有限容差
ATR_BUCKETS = 5                               # ATR 分位桶数（匹配桶审计）
MATCH_AUDIT_MIN_N = 100     # 原始触发数 ≥ 此值才强制桶 TV 阈值（小样本只审计）
HOUR_WEEKDAY_TV_MAX = 0.20  # (小时, 星期) 桶分布总变差上限（超 → 拒绝变换）
ATR_TV_MAX = 0.20           # ATR 桶分布总变差上限（超 → 拒绝变换）

# ---- P2-07：manifest 版本与依赖清单
MANIFEST_VERSION = 2
DEP_FILES = (
    "src/mt5quant/validation/prob_mode.py",
    "src/mt5quant/prob/labels.py",
    "src/mt5quant/prob/p1.py",
    "src/mt5quant/prob/tables.py",
    "src/mt5quant/prob/decision.py",
    "scripts/prob_calibration.py",
)
# 池内容指纹的固定列序（= 固定排序键：time 主键，其余按列序续排）
POOL_FP_COLS = ("time", "outcome", "net_r", "situation", "action", "side",
                "level")


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _file_sha256(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _deps_hashes() -> dict[str, str]:
    """依赖文件清单逐文件 sha256（P2-07）：方法代码任何改动都换指纹。"""
    out: dict[str, str] = {}
    for rel in DEP_FILES:
        p = REPO / rel
        if not p.is_file():
            raise RuntimeError(f"依赖文件缺失，拒绝运行：{rel}")
        out[rel] = _file_sha256(p)
    return out


def _sessions_sha() -> str:
    """config/sessions.toml 的 sha256（与 run_backtest.SESSIONS_PATH /
    sessions_sha256 同一文件同一算法；此处直读——manifest 组装不依赖
    可被测试替身替换的 run_backtest 模块）。"""
    p = REPO / "config" / "sessions.toml"
    if not p.is_file():
        raise RuntimeError(f"缺会话政策文件，拒绝运行：{p}")
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _data_snapshot() -> dict:
    """来源行情快照指纹（P2-07）：研究库 bars meta 的可得字段——批 1
    X-04 的 generation/content_sha256 优先；旧导出只有 data_sha256/
    rows/首尾/source_cutoff——取可得者。缺/坏/无任何内容指纹 → 拒绝。"""
    p = REPO / DATA_ROOT / "bars" / SYMBOL / TF / "meta.json"
    if not p.is_file():
        raise RuntimeError(f"来源行情 meta 缺失，拒绝运行：{p}")
    try:
        meta = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise RuntimeError(f"来源行情 meta 损坏，拒绝运行：{p}（{e}）") from e
    keys = ("generation", "content_sha256", "data_sha256", "rows",
            "first_time", "last_closed_time", "source_cutoff",
            "updated_at_utc")
    snap = {k: meta[k] for k in keys if k in meta}
    if "content_sha256" not in snap and "data_sha256" not in snap:
        raise RuntimeError("来源行情 meta 无内容指纹（content_sha256 与 "
                           "data_sha256 均缺），拒绝运行")
    return snap


# ---------------------------------------------------------------- pool

def build_pool(stamp: str) -> Path:
    """随机时点标签池：N_POOL 个（小时、星期、ATR 档）桶内随机时刻，
    引擎 A 同口径打标。池子只落本次运行目录，merge 后删除。

    P2-07：本相位**一次提取并冻结** cluster 相位需要的 S1 触发与行情
    快照（cluster 不再重读可变研究库）；pool.meta.json 记录池内容
    精确 hash（run/merge 据此核验实际内容）。"""
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
    pool_fp = _pool_content_fingerprint(labeled)
    meta = {"pool_seed": POOL_SEED, "n_pool": N_POOL, "cost_ver": cost_ver,
            "bracket": {"sl_atr": BRACKET.sl_atr, "tp_atr": BRACKET.tp_atr,
                        "horizon_bars": BRACKET.horizon_bars},
            "pool_content_sha256": pool_fp}
    (out.parent / "pool.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    _freeze_cluster_inputs(out.parent, bars)
    print(f"✓ 池 {len(labeled)} 条 → {out}（{meta['cost_ver']}；内容 hash "
          f"{pool_fp[:12]}…；cluster 输入已冻结）")
    return out


def _extract_s1_triggers(bars: pd.DataFrame) -> pd.DataFrame:
    """真实 S1 触发时刻（只数不看结果）——pool 阶段一次提取，供 cluster
    相位冻结使用（P2-07：不再由 cluster 相位重读研究库重新计数）。"""
    from mt5quant.prob.counting import level_rows_for_situation
    from mt5quant.strategies.library.prob.situations import find_level_events
    from mt5quant.strategies.library.prob.actions import action_triggers
    approach = Approach(lookback_bars=24, min_distance_atr=1.0,
                        touch_atr=0.25)
    aparams = ActionParams(breakout_atr=0.25, retest_window_bars=6)
    atr = atr_series(bars)
    levels = level_rows_for_situation(bars, "S1", grid_step=50.0,
                                      approach=approach, atr=atr)
    events = find_level_events(bars, levels, situation="S1",
                               approach=approach, atr=atr)
    trig_all = action_triggers(bars, events, params=aparams, atr=atr)
    return trig_all[["time", "situation", "action", "side", "level"]].copy()


def _freeze_cluster_inputs(stamp_dir: Path, bars: pd.DataFrame) -> None:
    """冻结 cluster 输入（P2-07）：bars + S1 触发落盘，字节指纹入
    cluster-inputs.meta.json（进 manifest 绑定；run/merge 复核）。"""
    trig = _extract_s1_triggers(bars)
    if len(trig) == 0:
        raise RuntimeError("S1 触发为空——无法冻结成簇负对照输入")
    bp = stamp_dir / "cluster-bars.parquet"
    tp = stamp_dir / "cluster-triggers.parquet"
    bars.to_parquet(bp, index=False)
    trig.to_parquet(tp, index=False)
    meta = {"bars_sha256": _file_sha256(bp),
            "triggers_sha256": _file_sha256(tp),
            "n_triggers": int(len(trig))}
    (stamp_dir / "cluster-inputs.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _canon_val(v) -> str:
    """规范化单元格值 → 确定性字符串（NaN/Inf/NaT 用固定标记，全部
    可排序可序列化）。"""
    if v is None:
        return "<null>"
    if isinstance(v, (pd.Timestamp,)):
        return "NaT" if pd.isna(v) else v.isoformat()
    if isinstance(v, (bool, np.bool_)):
        return "true" if v else "false"
    if isinstance(v, (np.floating, float)):
        f = float(v)
        if math.isnan(f):
            return "<nan>"
        if math.isinf(f):
            return "<inf>" if f > 0 else "<-inf>"
        return repr(f)
    if isinstance(v, (np.integer, int)):
        return str(int(v))
    return str(v)


def _pool_content_fingerprint(labeled: pd.DataFrame) -> str:
    """池内容指纹（P2-07）：labels 全行规范化序列化——固定列序 + 固定
    排序键（time 主键）逐行 canonical JSON 的 sha256。

    日期整体平移、赢输时间结构反转、单标签/净 R 改动、成本口径（经
    net_r）任何变化都改变 hash；行序重排（同内容配对）不变。旧的
    日赢率直方图摘要（audit2 P2-07）不区分日期/顺序/确切标签，已废。"""
    cols = [c for c in POOL_FP_COLS if c in labeled.columns]
    rows = [{c: _canon_val(rec[c]) for c in cols}
            for rec in labeled[cols].to_dict("records")] if cols else []
    rows.sort(key=lambda r: tuple(r[c] for c in cols))
    payload = json.dumps({"columns": cols, "rows": rows},
                         sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True, allow_nan=False)
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


def _ny_parts(idx: pd.DatetimeIndex):
    """NY 本地（小时, 分钟, 星期）数组（NaT → -1，恒不匹配）。"""
    ny = idx.tz_localize("UTC").tz_convert("America/New_York")
    return (ny.hour.to_numpy(), ny.minute.to_numpy(),
            ny.dayofweek.to_numpy())


def _atr_bucket_edges(atr: np.ndarray) -> np.ndarray | None:
    finite = atr[np.isfinite(atr)]
    if len(finite) < ATR_BUCKETS:
        return None
    qs = np.quantile(finite, np.linspace(0.0, 1.0, ATR_BUCKETS + 1)[1:-1])
    return np.asarray(qs, dtype=float)


def _buckets_of(atr: np.ndarray, edges, positions: np.ndarray) -> np.ndarray:
    """按 bar 位置取 ATR 分位桶（位置非法/ATR 缺 → -1）。"""
    out = np.full(len(positions), -1, dtype=int)
    if edges is None:
        return out
    okp = positions >= 0
    vals = np.where(okp, atr[np.clip(positions, 0, len(atr) - 1)], np.nan)
    fin = np.isfinite(vals)
    out[fin] = np.searchsorted(edges, vals[fin], side="right")
    return out


def _tv(counts_a: dict, counts_b: dict) -> float:
    """两个计数字典的总变差距离（空侧 → 0：无组可比时不据此拒绝）。"""
    sa, sb = sum(counts_a.values()), sum(counts_b.values())
    if sa == 0 or sb == 0:
        return 0.0
    return 0.5 * sum(abs(counts_a.get(k, 0) / sa - counts_b.get(k, 0) / sb)
                     for k in set(counts_a) | set(counts_b))


def _hw_counts(times) -> dict:
    h, _, w = _ny_parts(pd.DatetimeIndex(times))
    c: dict[str, int] = {}
    for hh, ww in zip(h, w):
        k = f"{int(hh)}-{int(ww)}"
        c[k] = c.get(k, 0) + 1
    return c


def _shifted_triggers(trig: pd.DataFrame, times_idx: pd.Index, *, weeks: int,
                      perm_state: int, atr=None) -> tuple[pd.DataFrame, dict]:
    """种子化独立平移+独立置换（P-02），映射按预写规则严格核验（P2-06）。

    预写规则（先于运行声明；不可根据标签或 p 值挑变换）：
    R1 有限容差映射：平移时刻必须精确命中合法 bar，或落在最近 bar 的
       SHIFT_MAP_TOLERANCE（=1 根 H1 bar）内；超出容差（内部洞/休市/
       序列两端之外）→ **丢弃该触发**——绝不无容差 nearest 吸到洞边或
       原始触发时刻，也不在末端堆积。丢弃分类记 gap（覆盖损失）。
    R2 匹配重证：存活行的 NY 本地（小时, 分钟, 星期）必须与原始触发
       一致（整周 NY 往返本应保墙钟；不一致说明容差吸附/边界把匹配维
       扭了）→ 丢弃该触发（calendar）；ATR（提供时）在原始与映射 bar
       都必须可算（回看不足 K → atr_na 丢弃）。
    R3 原始时刻 disjoint：映射时刻等于任何原始触发时刻（周期性触发+
       整周平移可精确相撞）→ 丢弃该触发（orig_collision）；返回前对
       存活集合再做最终 disjoint 断言（防回归，violation 直接 raise）。
    R4 匹配桶审计与拒绝：按（小时×星期）与 ATR 五分位桶对比存活组与
       原始组的分布（总变差 TV）。原始触发数 ≥ MATCH_AUDIT_MIN_N 时
       TV 超过预写阈值 → 拒绝整个变换（整组清空并记原因——丢弃规则
       系统性偏移了匹配条件）；样本不足只审计不拒绝（小样本 TV 全是
       噪声）。审计随返回值交给调用方入行。

    返回 (存活触发表, 审计 dict)。"""
    perm = trig.sample(frac=1.0, random_state=perm_state).reset_index(
        drop=True)
    n = len(perm)
    orig_dt = pd.DatetimeIndex(pd.to_datetime(trig["time"]))
    orig_times = set(orig_dt.tolist())
    if n == 0:
        empty = perm.iloc[0:0]
        return empty, {"n_orig": 0, "n_kept": 0,
                       "drops": {"gap": 0, "calendar": 0, "atr_na": 0,
                                 "orig_collision": 0},
                       "tv_hour_weekday": 0.0, "tv_atr": None,
                       "atr_bucket_match_rate": None,
                       "bucket_check": "no_atr", "rejected": False,
                       "reject_reason": None}
    # 统一纳秒精度再比较（_shift_weeks_ny 往返可能产生 us 精度索引）
    shifted = pd.DatetimeIndex(
        _shift_weeks_ny(perm["time"], weeks).to_numpy()
    ).astype("datetime64[ns]")
    bars_dt = pd.DatetimeIndex(times_idx)
    pos = bars_dt.get_indexer(shifted, method="nearest")
    mapped = np.asarray(bars_dt.to_numpy())[pos]
    # R1：有限容差（±1 bar），超出即丢（含内部洞/休市/两端之外）
    gap = np.abs(shifted.asi8 - mapped.astype("datetime64[ns]").astype(
        "int64")) > SHIFT_MAP_TOLERANCE.value  # noqa: E501（asi8 已归一 ns）
    # R2：NY 墙钟重证（只对容差内存活者算，避免 NaT 参与比较）
    cal_bad = np.zeros(n, dtype=bool)
    ci = np.flatnonzero(~gap)
    if len(ci):
        oh, om, ow = _ny_parts(pd.DatetimeIndex(
            perm["time"].to_numpy()[ci]))
        mh, mm, mw = _ny_parts(pd.DatetimeIndex(mapped[ci]))
        cal_bad[ci] = (oh != mh) | (om != mm) | (ow != mw)
    elig = ~gap & ~cal_bad
    # R2b：ATR 资格（原始与映射 bar 都必须可算）
    atr_bad = np.zeros(n, dtype=bool)
    o_bucket = np.full(n, -1, dtype=int)
    m_bucket = np.full(n, -1, dtype=int)
    edges = None
    if atr is not None:
        atr = np.asarray(atr, dtype=float)
        ei = np.flatnonzero(elig)
        orig_pos_all = bars_dt.get_indexer(pd.DatetimeIndex(perm["time"]))
        o_atr = np.where(orig_pos_all[ei] >= 0,
                         atr[np.clip(orig_pos_all[ei], 0, len(atr) - 1)],
                         np.nan)
        m_atr = atr[np.clip(pos[ei], 0, len(atr) - 1)]
        atr_bad[ei] = ~np.isfinite(o_atr) | ~np.isfinite(m_atr)
        edges = _atr_bucket_edges(atr)
        if edges is not None:
            o_bucket = _buckets_of(atr, edges, orig_pos_all)
            m_bucket = _buckets_of(atr, edges, pos)
    # R3：与任何原始触发时刻相撞 → 丢弃
    coll = elig & ~atr_bad & pd.DatetimeIndex(mapped).isin(orig_dt)
    keep = elig & ~atr_bad & ~coll
    kept = perm[keep].reset_index(drop=True).copy()
    if len(kept):
        kept["time"] = pd.DatetimeIndex(mapped[keep])
    survivors = set(pd.DatetimeIndex(kept["time"]).tolist()) if len(kept) \
        else set()
    if survivors & orig_times:
        raise RuntimeError("P2-06 最终 disjoint 检查失败：平移后标签时刻"
                           "仍包含原始触发时刻")
    drops = {"gap": int(gap.sum()), "calendar": int(cal_bad.sum()),
             "atr_na": int(atr_bad.sum()),
             "orig_collision": int(coll.sum())}
    # R4：匹配桶审计（存活组 vs 原始组分布）
    tv_hw = _tv(_hw_counts(trig["time"]),
                _hw_counts(kept["time"]) if len(kept) else {})
    if edges is not None:
        o_counts: dict[int, int] = {}
        for b in o_bucket[o_bucket >= 0]:
            o_counts[int(b)] = o_counts.get(int(b), 0) + 1
        km = keep & (m_bucket >= 0)
        m_counts: dict[int, int] = {}
        for b in m_bucket[km]:
            m_counts[int(b)] = m_counts.get(int(b), 0) + 1
        tv_atr = _tv(o_counts, m_counts)
        match_rate = (float((o_bucket[km] == m_bucket[km]).mean())
                      if km.any() else None)
    else:
        tv_atr, match_rate = None, None
    enforced = len(trig) >= MATCH_AUDIT_MIN_N
    rejected, reason = False, None
    if enforced and tv_hw > HOUR_WEEKDAY_TV_MAX:
        rejected, reason = True, (f"hour/weekday 桶 TV={tv_hw:.3f} > "
                                  f"{HOUR_WEEKDAY_TV_MAX}")
    if enforced and tv_atr is not None and tv_atr > ATR_TV_MAX:
        rejected, reason = True, (f"ATR 桶 TV={tv_atr:.3f} > {ATR_TV_MAX}")
    if rejected:
        kept = kept.iloc[0:0].copy()
    if enforced:
        bucket_check = "enforced"
    else:
        bucket_check = "no_atr" if edges is None else "audit_only_low_n"
    audit = {"n_orig": int(len(trig)), "n_kept": int(len(kept)),
             "drops": drops, "tv_hour_weekday": float(tv_hw),
             "tv_atr": None if tv_atr is None else float(tv_atr),
             "atr_bucket_match_rate": match_rate,
             "bucket_check": bucket_check, "rejected": bool(rejected),
             "reject_reason": reason}
    return kept, audit


def phase_cluster(shard_i: int, shard_n: int, stamp: str, *,
                  frozen=None) -> list[dict]:
    """成簇负对照（审核三；P-02 重写；P2-06/P2-07 加固）。

    输入（P2-07）：正式入口 cmd_run 传入 pool 阶段**一次提取并冻结**的
    (bars, S1 触发)——cluster 相位不再重读可变研究库（同一冻结快照与
    pool/manifest 绑定）。frozen=None 的直连路径（受保护探针/单元测试）
    现场经 load_series+计数提取，不落盘。

    real 与 null 均只用平移后时刻的标签：同种子各一次**独立**整周平移
    （基础 wk + 0..CLUSTER_EXTRA_WEEKS_MAX 附加周，NY 往返保本地小时/
    星期）+ **独立**置换；映射/资格/匹配桶按 _shifted_triggers 预写规则
    R1–R4 逐项核验（内部洞/休市/末端/匹配退化 → 丢弃或拒绝变换，覆盖
    损失与桶审计随行记录）。两组重叠（同平移周时集合可相同）按预写
    规则**记录披露**、不据此挑变换——剔除会以平移幅值为条件。"""
    if frozen is not None:
        bars_full, trig = frozen
    else:
        from run_backtest import load_series
        bars_full = load_series(DATA_ROOT, SYMBOL, TF, None)
        trig = _extract_s1_triggers(bars_full)
    if len(trig) == 0:
        raise RuntimeError("S1 触发为空——无法做成簇负对照")
    costs, policy, cs, _ = _costs_and_sizes(
        swap_ref_price=float(bars_full["close"].iloc[-1]))
    kw = dict(symbol=SYMBOL, timeframe=TF, bracket=BRACKET,
              contract_size=cs, volume_lots=1.0, session_policy=policy)
    atr = atr_series(bars_full).to_numpy(dtype=float)
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
            real_trig, a_real = _shifted_triggers(
                trig, times_idx, weeks=wk + extra_real, perm_state=perm_real,
                atr=atr)
            null_trig, a_null = _shifted_triggers(
                trig, times_idx, weeks=wk + extra_null, perm_state=perm_null,
                atr=atr)
            real = _labels(real_trig)
            null = _labels(null_trig)
            res = delta_p_test(real, null, block=BLOCK, sims=B_SIMS, seed=seed)
            rt = set(pd.DatetimeIndex(real_trig["time"]).tolist()) \
                if len(real_trig) else set()
            nt = set(pd.DatetimeIndex(null_trig["time"]).tolist()) \
                if len(null_trig) else set()
            out.append({"kind": "cluster_neg", "weeks": wk, "seed": seed,
                        "shift_weeks_real": wk + extra_real,
                        "shift_weeks_null": wk + extra_null,
                        "n_real": int(len(real)), "n_null": int(len(null)),
                        "n_days_real": res["n_days_real"],
                        "n_days_null": res["n_days_null"],
                        "p": res["p_value"],
                        "n_orig_triggers": int(len(trig)),
                        "n_overlap_real_null": int(len(rt & nt)),
                        "sets_identical": bool(rt and rt == nt),
                        "audit_real": a_real, "audit_null": a_null})
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


def _expected_jobs() -> list[str]:
    """预期 (phase, condition, seed) 完备集（P-12）：从设计常量推导的
    本次运行应产出的全部行键。"""
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
    return sorted(_job_key(j) for j in jobs)


def _job_manifest() -> dict:
    return {"version": MANIFEST_VERSION, "jobs": _expected_jobs()}


def _manifest_bindings(pool_meta: dict, cluster_meta: dict) -> dict:
    """run manifest v2 的绑定集合（P2-07）：池内容、来源行情快照、
    成本/政策、方法版本（BLOCK+设计常量）、依赖清单、cluster 冻结输入。"""
    return {
        "pool_content_sha256": pool_meta["pool_content_sha256"],
        "cost_ver": pool_meta["cost_ver"],
        "sessions_sha256": _sessions_sha(),
        "data_snapshot": _data_snapshot(),
        "method": {
            "block": BLOCK, "B": B_SIMS, "pool_seed": POOL_SEED,
            "n_pool": N_POOL,
            "bracket": {"sl_atr": BRACKET.sl_atr, "tp_atr": BRACKET.tp_atr,
                        "horizon_bars": BRACKET.horizon_bars},
            "neg": [list(NEG_NS), NEG_SEEDS],
            "holm_hyps": HOLM_HYPS,
            "pos": [list(POS_DELTAS), list(POS_NS), POS_SEEDS],
            "cluster": [list(CLUSTER_SHIFT_WEEKS), CLUSTER_SEEDS,
                        CLUSTER_EXTRA_WEEKS_MAX],
            "brier": BRIER_SEEDS,
        },
        "deps": _deps_hashes(),
        "cluster_inputs": dict(cluster_meta),
    }


def _manifest_fingerprint(core: dict) -> str:
    payload = json.dumps(core, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _read_pool_meta(stamp_dir: Path) -> dict:
    p = stamp_dir / "pool.meta.json"
    if not p.is_file():
        raise RuntimeError(f"缺 pool.meta.json（{p}）——拒绝运行（P2-07："
                           "不接受缺 meta 的空串回退；先跑 pool 子命令）")
    try:
        meta = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise RuntimeError(f"pool.meta.json 损坏——拒绝运行：{e}") from e
    if (not isinstance(meta, dict)
            or not meta.get("pool_content_sha256")
            or not meta.get("cost_ver")):
        raise RuntimeError("pool.meta.json 缺 pool_content_sha256/cost_ver"
                           "——旧版本产物，拒绝运行（以新代码重跑 pool）")
    return meta


def _verify_stamp_inputs(stamp_dir: Path) -> dict:
    """启动/合并共用的实际内容核验（P2-07）：标签池、pool meta、冻结
    cluster 输入与各自记录的指纹逐项对得上，否则拒绝（不静默回退）。"""
    pool_p = stamp_dir / "pool.parquet"
    if not pool_p.is_file():
        raise RuntimeError(f"缺标签池 {pool_p}——先跑 pool 子命令")
    pool_meta = _read_pool_meta(stamp_dir)
    actual = _pool_content_fingerprint(pd.read_parquet(pool_p))
    if actual != pool_meta["pool_content_sha256"]:
        raise RuntimeError("标签池实际内容与 pool.meta.json 的 "
                           "pool_content_sha256 不一致（pool 被替换而 meta "
                           "未更新？）——拒绝")
    cmeta_p = stamp_dir / "cluster-inputs.meta.json"
    if not cmeta_p.is_file():
        raise RuntimeError("缺 cluster-inputs.meta.json——pool 阶段未冻结 "
                           "cluster 输入（旧版本产物），拒绝运行")
    try:
        cmeta = json.loads(cmeta_p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise RuntimeError(f"cluster-inputs.meta.json 损坏——拒绝：{e}") from e
    for key, fname in (("bars_sha256", "cluster-bars.parquet"),
                       ("triggers_sha256", "cluster-triggers.parquet")):
        fp = stamp_dir / fname
        if not fp.is_file():
            raise RuntimeError(f"缺冻结 cluster 输入 {fname}——拒绝运行")
        if _file_sha256(fp) != cmeta.get(key):
            raise RuntimeError(f"cluster-inputs 与实际文件不一致：{fname} "
                               "字节指纹对不上——拒绝")
    trig_n = len(pd.read_parquet(stamp_dir / "cluster-triggers.parquet"))
    if trig_n == 0 or int(cmeta.get("n_triggers", -1)) != trig_n:
        raise RuntimeError("冻结 cluster 触发数非法/为空——拒绝运行")
    return {"pool_meta": pool_meta, "cluster_meta": cmeta}


def _load_frozen_cluster_inputs(stamp_dir: Path):
    bars = pd.read_parquet(stamp_dir / "cluster-bars.parquet")
    trig = pd.read_parquet(stamp_dir / "cluster-triggers.parquet")
    return bars, trig


def _build_manifest(stamp_dir: Path) -> dict:
    """完整 run manifest（P2-07）：先核验实际内容，再组装 jobs+绑定+
    指纹。run/merge 共用——两处算出的 manifest 必须逐字节相同。"""
    got = _verify_stamp_inputs(stamp_dir)
    core = {"version": MANIFEST_VERSION, "jobs": _expected_jobs(),
            "bindings": _manifest_bindings(got["pool_meta"],
                                           got["cluster_meta"])}
    return {**core, "fingerprint": _manifest_fingerprint(core)}


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


def _pool_path(stamp: str) -> Path:
    return OUT_ROOT / stamp / "pool.parquet"


def cmd_run(args) -> int:
    stamp = args.stamp or _stamp()
    stamp_dir = OUT_ROOT / stamp
    # P2-07：启动即核验实际内容并组装绑定 manifest——meta 缺/坏、池或
    # 冻结输入被替换、依赖/来源/政策变动，一律拒绝（不退空串）
    try:
        manifest = _build_manifest(stamp_dir)
    except RuntimeError as e:
        print(f"✗ {e}")
        return 1
    # P-12：run 即写预期完备集 manifest（幂等；与已有 manifest 不一致
    # 说明同一 stamp 目录混入了不同版本/设计/来源——拒绝）
    mpath = stamp_dir / "manifest.json"
    if mpath.is_file():
        try:
            have = json.loads(mpath.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            have = None
            print(f"✗ manifest.json 解析失败：{e}")
        if have != manifest:
            print("✗ manifest.json 与当前脚本/输入绑定不一致（同一 stamp"
                  " 目录不得混用不同版本/设计/来源）")
            return 1
    else:
        mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    frozen = None
    if args.phase == "cluster":
        frozen = _load_frozen_cluster_inputs(stamp_dir)   # 已核验的冻结输入
    else:
        pool = pd.read_parquet(_pool_path(stamp))
        pool_days = _day_winrate_table(pool)
    t0 = time.time()
    if args.phase == "neg":
        rows = phase_neg(pool_days, args.shard_i, args.shard_n, stamp)
    elif args.phase == "neg-holm":
        rows = phase_neg_holm(pool_days, args.shard_i, args.shard_n)
    elif args.phase == "pos":
        rows = phase_pos(pool_days, args.shard_i, args.shard_n)
    elif args.phase == "cluster":
        rows = phase_cluster(args.shard_i, args.shard_n, stamp, frozen=frozen)
    elif args.phase == "brier":
        rows = phase_brier(pool, args.shard_i, args.shard_n)
    else:
        print(f"✗ 未知 phase {args.phase}")
        return 1
    out = stamp_dir / f"{args.phase}-s{args.shard_i}of{args.shard_n}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {"phase": args.phase, "shard": [args.shard_i, args.shard_n],
         "fingerprint": manifest["fingerprint"], "B": B_SIMS,
         "rows": rows}, ensure_ascii=False), encoding="utf-8")
    print(f"✓ {args.phase} 分片 {args.shard_i}/{args.shard_n}：{len(rows)} 行"
          f"（{time.time() - t0:.0f}s）→ {out}")
    return 0


def cmd_merge(args) -> int:
    stamp_dir = OUT_ROOT / args.stamp
    # P-12/P2-07：manifest 完备集 + 全部绑定校验（缺/多/重复/混版本/
    # 旧版本一律拒绝）
    mpath = stamp_dir / "manifest.json"
    if not mpath.is_file():
        print("✗ 缺 manifest.json——先以 run 子命令跑齐各 phase")
        return 1
    try:
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"✗ manifest.json 解析失败：{e}")
        return 1
    if (not isinstance(manifest, dict)
            or manifest.get("version") != MANIFEST_VERSION
            or "bindings" not in manifest or "fingerprint" not in manifest):
        print("✗ 旧版本产物（manifest 无 v2 绑定结构），不可在新代码下"
              "合并——请以新代码重跑 pool→run→merge 全流程（新 stamp 新"
              "指纹是有意的不兼容）")
        return 1
    try:
        expected = _build_manifest(stamp_dir)
    except RuntimeError as e:
        print(f"✗ {e}")
        return 1
    if manifest != expected:
        print("✗ manifest 与当前实际产物绑定不一致（分片与 merge 用了"
              "不同版本、设计或来源——拒绝）")
        return 1
    files = sorted(stamp_dir.glob("*-s*of*.json"))
    expected_keys = set(manifest["jobs"])
    rows = []
    seen: dict[str, str] = {}
    dup: list[str] = []
    fp_bad: list[str] = []
    for f in files:
        d = json.loads(f.read_text(encoding="utf-8"))
        if d.get("fingerprint") != manifest["fingerprint"]:
            fp_bad.append(f.name)
            continue
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
    if fp_bad:
        print(f"✗ 分片指纹与 manifest 绑定不一致（混版本/异源），涉及 "
              f"{len(fp_bad)} 个文件（前 5）：")
        for nm in fp_bad[:5]:
            print(f"    - {nm}")
        return 1
    if dup:
        print(f"✗ 行键重复 {len(dup)} 处（前 10）：")
        for k in dup[:10]:
            print(f"    - {k}")
        return 1
    missing = sorted(expected_keys - set(seen))
    extra = sorted(set(seen) - expected_keys)
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
                     "fingerprint": manifest["fingerprint"]}

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
    # 删除标签池（临时产物）；来源证据全部保留——P-12/P2-07：pool.meta
    # .json、manifest.json 与冻结 cluster 输入不随池本体销毁
    pool_p = _pool_path(args.stamp)
    if pool_p.is_file() and args.keep_pool is False:
        pool_p.unlink()
        print("— 标签池已删除（pool.meta.json / manifest.json / "
              "cluster-inputs 保留：来源与重建证据）")
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
