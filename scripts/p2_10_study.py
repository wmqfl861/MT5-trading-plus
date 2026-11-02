"""P2-10 统计覆盖研究（T107 模块 B，Issue #4 第 3 项）：预注册模拟研究器。

研究对象=真实决策层（level_decision.generate_signals 的内联决策段语义：
候选含局面维度、过门槛取 EV 最高、并列 (sit,action,side) 字典序、方向冲突
弃权、白名单过滤）；估计器对照 E0（现行：ProbTable.fit 收缩 + Wald SE@
原始 n + ev_lower）vs E1 Wilson / E2 Clopper-Pearson / E3 Jeffreys（有效
计数 (k+m·p_base_o, n+m)，win 取下界、loss 取上界、time=1−两项不截断，
r 的不确定度不传播——与现行同口径，单独披露为局限）。

== 口径与简化声明（预注册，结果前冻结）==
1. 统计单位=独立交易日：一日多触发共享当日**单一**结局——这是最强相关
   角点假设（现行 labels 为逐触发 bracket 评估=同日相关而非同一结局），
   作为保守角点显式声明，不称"同口径复刻"；簇大小 {1,3,10} 覆盖相关
   强度梯度。
2. K 跨 session 边界的触发按 **time 结局**计——现行 labels 的 time 出场/
   截尾口径的简化（labels 对跨界为 skipped 截尾；简化为 time 不改变
   "不产生 win/loss 证据"的方向）。
3. 特征缺失按预写比例把行降级为 na 值 → 复刻现行 lookup 的格→父格→
   基准回退（估计器输入按现行行为复刻，格内 n=行数是研究对象本身）。
4. 覆盖计量的估计对象=指定候选（S1,A1）在其决策特征值上的真值 EV
   （na 档=两特征格真值混合）；lookup 回退到父格/基准时仍与该格真值
   比较（决策所要估计的对象）——冻结选择，报告披露。
5. E0 每条件首 rep 与中位 rep 真调用 ProbTable.fit 作**逐位金丝雀**
   （任一字段不一致 → 立即中止整个研究：fail-closed）；其余 rep 用与
   真实 fit 逐位等价的复刻聚合（同一表达式、同一求和顺序，单测 +
   金丝雀双保险）。原因：真实 fit 单次 ~60-100ms（pandas 固定开销×
   ~18 组聚合），全矩阵 1200 条件×2000 rep×2 抽样需 ~100 CPU 小时，
   不可行；复刻使统计口径与真实实现逐位一致而非近似。
6. 预写两级门（覆盖 ≥95% PASS / <80% RED；误开放 ≤5% PASS / >10%
   RED）机器化输出；红牌条件的失败案例落盘保留——**全量失败 rep 索引**
   （种子+rep 可确定性再生全样本）+ 每条件至多 FAIL_DETAIL_MAX 条全细节
   案例（真值/样本充分统计量/决策/EV_lb/EV_true）。**不因对照（E1-E3）
   或任何结果反调 E0 阈值**（预注册禁令；变更走版本化估计器提案另批）。

== 零研究库依赖（防泄漏）==
本模块只 import mt5quant.prob.tables / mt5quant.prob.decision（研究对象，
只读使用）；不 import 研究数据层（其依赖链会拉入 mt5 数据根），不读取
任何数据根——合成数据全部由冻结种子派生。level_decision.py 只按**文件
字节**计入指纹（不 import：其 import 链含引擎与数据层）。决策段等价物
decide_level 的语义一致性由 tests/test_p2_10_study.py 经真实
generate_signals 驱动证明。

== 可复现性 ==
全部设计常量冻结在模块头部：run_id = sha256(本文件字节 + 全部冻结常量
规范 JSON)[0:16]——改动任何常量或代码 → run_id 变。结果目录
data/prob/p2-10/<run_id>/ 只追加：同 run_id 目录已存在 → 拒绝覆盖，
失败实验原样保留。逐条件逐重复种子从 SEED_BASE 确定性派生，与分片
无关（--shard-i/--shard-n 按条件分片，合并结果与整跑逐位一致）。

用法（Git Bash，仓库根）：
    .venv/Scripts/python.exe scripts/p2_10_study.py run
    .venv/Scripts/python.exe scripts/p2_10_study.py run --shard-i 0 --shard-n 16
    .venv/Scripts/python.exe scripts/p2_10_study.py report <run_id>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5quant.prob.decision import ev_lower  # noqa: E402
from mt5quant.prob.tables import CellEstimate, ProbTable  # noqa: E402

# ============================================================ 冻结设计常量
# （改动任何一个 → run_id 变。预注册设计=实现规格。）
SEED_BASE = 20261101            # 种子基（S）
M_REPS = 2000                   # 每条件重复次数
PRIOR_STRENGTH = 100            # 现行 m（LevelDecisionV1.PRIOR_STRENGTH）
MIN_CELL = 100                  # 现行回退阈值（LevelDecisionV1.MIN_CELL）
LB_Z = 1.0                      # 现行 lb_z（DecisionPolicy.z）
THRESHOLD_MULT = 2.0            # 现行门槛倍数（DecisionPolicy.threshold_mult）
COST_R = 0.05                   # 每（局面,动作）平均来回成本 R（研究冻结）
PARENT_N_DAYS = (1, 2, 99, 100)             # 父格 n（单位=独立交易日）
STRUCTURES = ("mixed", "parent_all_win", "parent_all_loss",
              "action_all_win", "action_all_loss")
BASE_RATES = (0.34, 0.1, 0.5, 0.0, 1.0)     # 共享真值基准赢率（0/1=边界档）
CLUSTERS = (1, 3, 10)                       # 同日簇大小
MISS_FRACS = (0.0, 0.3)                     # 特征缺失比例
K_CROSS_FRACS = (0.0, 0.5)                  # K 跨界触发比例（按 time 计）
SITUATIONS = ("S1", "S2")
ACTIONS = ("A1", "A2")
FEAT_COL = "feat"
FEAT_VALUES = ("f0", "f1")
NA_VALUE = "na"
# 白名单（现行 P2-01 语义：（局面,动作）对；S2:A1 有数据但不获准——
# 研究内真实行使白名单过滤）
ALLOWED_HYPOTHESES = ("S1:A1", "S1:A2", "S2:A2")
# r 分布（正态；均值/方差为冻结条件参数）：win/loss/time 各自 (mean, sd)
R_DIST = ((1.90, 0.20), (-1.05, 0.10), (0.10, 0.10))
NON_WIN_SPLIT = (0.85, 0.15)    # 非赢质量在 loss/time 间的拆分
DIRICHLET_CONC = 40.0           # 混合档真值浓度（异质性梯度）
DIRICHLET_MIN = 0.01            # 均值分量低于此 → 退化为定点真值
DESIGNATED = ("S1", "A1")       # 覆盖计量的指定候选
GATE_COVERAGE_PASS = 0.95       # 覆盖门：≥95% PASS
GATE_COVERAGE_RED = 0.80        # 覆盖门：<80% RED（保守性不成立）
GATE_FALSE_OPEN_PASS = 0.05     # 误开放门：≤5% PASS
GATE_FALSE_OPEN_RED = 0.10      # 误开放门：>10% RED
FALSE_OPEN_MIN_NULL = 100       # 空真 rep 数不足 → 误开放不判（NA）
CANARY_REP_OFFSETS = (0, 999)   # 每条件真 fit 金丝雀的 rep 偏移
# 红牌条件失败案例的落盘口径：全量失败 **rep 索引**（可由种子+rep 确定性
# 再生全样本）+ 每条件至多 FAIL_DETAIL_MAX 条全细节案例（真值/样本充分
# 统计量/决策/EV_lb/EV_true）——控制 failures.json 规模
FAIL_DETAIL_MAX = 100
ESTIMATORS = ("E0", "E1", "E2", "E3")
QBISECT_ITERS = 52              # 标量 qbeta 二分迭代数（精度 ~1e-16）
BISECT_PRE_ITERS = 36           # 批量反演的预二分轮数（随后牛顿加速）
NEWTON_STEPS = 8                # 批量反演的护栏牛顿步上限
BETACF_MAXIT = 250              # 不完全贝塔连分式最大迭代
BETACF_EPS = 3e-16
RUN_ID_LEN = 16
OUT_REL_DIR = "data/prob/p2-10"
PREREG_ANCHORS = (
    "lb_z=1.0 单项名义覆盖≈84%（正态单侧）",
    "m=100 收缩 + Wald SE 按原始 n：混合档大 n 覆盖预期 >95%"
    "（SE 高估收缩估计实际波动约 (n+m)/n 倍，n≈100 时有效 z≈2）",
    "动作级全胜/全负档：base=None 层 p̂=1/0 且收缩格 p̂≡1 → SE=0 →"
    " 覆盖预期塌向 ~50%（红牌候选；若兑现即触发版本化估计器变更提案）",
    "误开放：真实 EV−成本 ≤ 0 时开放概率主条件 ≤5%；>10% 红牌",
    "同日多触发共享当日单一结局=最强相关角点假设（非现行 labels 的"
    "逐触发 bracket 口径复刻）；簇 {1,3,10} 覆盖相关强度梯度",
    "K 跨界触发按 time 结局计=labels 截尾口径的简化（声明）",
    "E1/E2/E3 仅为对照：不因对照结果反调 E0 任何阈值（预注册禁令）",
)

_DEP_FILES = (
    "src/mt5quant/prob/tables.py",
    "src/mt5quant/prob/decision.py",
    "src/mt5quant/strategies/library/prob/level_decision.py",
    "scripts/p2_10_study.py",
)
_OUTCOME_NAMES = ("win", "loss", "time")
_WILDCARD = "*"


class StudyError(RuntimeError):
    """研究器 fail-closed 错误（金丝雀失败/目录冲突/结果不完整等）。"""


# ============================================================ 种子与指纹

def frozen_constants() -> dict:
    """全部冻结常量的当前快照（run_id 与 manifest 的输入）。"""
    names = (
        "SEED_BASE", "M_REPS", "PRIOR_STRENGTH", "MIN_CELL", "LB_Z",
        "THRESHOLD_MULT", "COST_R", "PARENT_N_DAYS", "STRUCTURES",
        "BASE_RATES", "CLUSTERS", "MISS_FRACS", "K_CROSS_FRACS",
        "SITUATIONS", "ACTIONS", "FEAT_COL", "FEAT_VALUES", "NA_VALUE",
        "ALLOWED_HYPOTHESES", "R_DIST", "NON_WIN_SPLIT", "DIRICHLET_CONC",
        "DIRICHLET_MIN", "DESIGNATED", "GATE_COVERAGE_PASS",
        "GATE_COVERAGE_RED", "GATE_FALSE_OPEN_PASS", "GATE_FALSE_OPEN_RED",
        "FALSE_OPEN_MIN_NULL", "CANARY_REP_OFFSETS", "FAIL_DETAIL_MAX",
        "ESTIMATORS", "QBISECT_ITERS", "BISECT_PRE_ITERS", "NEWTON_STEPS",
        "BETACF_MAXIT", "BETACF_EPS", "RUN_ID_LEN", "OUT_REL_DIR",
        "PREREG_ANCHORS",
    )
    return {name: globals()[name] for name in names}


def _canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=True,
                      separators=(",", ":"))


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compute_run_id(module_path: Path | None = None) -> str:
    """run_id = sha256(本文件字节 + 全部冻结常量规范 JSON)[0:16]。"""
    path = module_path or Path(__file__).resolve()
    digest = hashlib.sha256(path.read_bytes() + b"\x00"
                            + _canonical_json(frozen_constants())
                            .encode("utf-8")).hexdigest()
    return digest[:RUN_ID_LEN]


def derive_rng(*parts) -> np.random.Generator:
    """从种子基逐部分确定性派生 RNG（与分片无关：部分含条件键与 rep 号）。"""
    text = "|".join([str(SEED_BASE), *(str(p) for p in parts)])
    seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8],
                          "big")
    return np.random.default_rng(seed)


def parse_allowed_pairs(entries) -> frozenset:
    """"S:A" 条目 →（局面,动作）对集合（现行 P2-01 解析语义的最小子集）。"""
    out = []
    for raw in entries:
        s = str(raw)
        sit, sep, act = s.partition(":")
        if not sep or not sit or not act:
            raise ValueError(f"白名单条目 {raw!r} 须为 'S:A' 形式")
        out.append((sit, act))
    return frozenset(out)


# ============================================================ 条件矩阵

def matrix_conditions() -> list[dict]:
    """冻结条件矩阵（顺序冻结——分片与合并的确定性基础）。"""
    conds = []
    for n_days in PARENT_N_DAYS:
        for structure in STRUCTURES:
            for base in BASE_RATES:
                for cluster in CLUSTERS:
                    for miss in MISS_FRACS:
                        for k_cross in K_CROSS_FRACS:
                            conds.append(dict(n_days=n_days, structure=structure,
                                              base_rate=base, cluster=cluster,
                                              miss=miss, k_cross=k_cross))
    return conds


def matrix_size() -> int:
    n = 1
    for seq in (PARENT_N_DAYS, STRUCTURES, BASE_RATES, CLUSTERS, MISS_FRACS,
                K_CROSS_FRACS):
        n *= len(seq)
    return n


def cond_key(cond: dict) -> str:
    return (f"n{cond['n_days']}|{cond['structure']}|b{cond['base_rate']}"
            f"|c{cond['cluster']}|m{cond['miss']}|x{cond['k_cross']}")


# ============================================================ 决策层等价物

def _allowed_in(allowed_pairs, situation, action) -> bool:
    """现行 _allowed_in 语义：精确对或（通配局面, 动作）。"""
    return (situation, action) in allowed_pairs \
        or (_WILDCARD, action) in allowed_pairs


def decide_level(cands, evs, *, threshold_mult, allowed_pairs):
    """level_decision.generate_signals 决策段（~L652-684）的纯函数等价物。

    cands: [(sit, action, side, cost_r|None)]；evs: 平行的 EV 下界
    （None=无表估计——真实段 lookup KeyError/无成本 → 不参与决策）。
    返回赢家 (sit, action, side) 或 None（弃权/全不过门槛/方向冲突）。
    语义逐条复刻：白名单过滤→门槛 ev ≥ threshold_mult×cost→排序
    (-ev, sit, action, side)→过门槛候选含双向 → 弃权→取首名。
    """
    passing = []
    for (sit, act, side, cost), ev in zip(cands, evs):
        if cost is None or ev is None:
            continue
        if allowed_pairs is not None and not _allowed_in(allowed_pairs,
                                                         sit, act):
            continue
        if ev >= threshold_mult * cost:
            passing.append((ev, sit, act, side))
    if not passing:
        return None
    passing.sort(key=lambda t: (-t[0], t[1], t[2], t[3]))
    sides = {t[3] for t in passing}
    if -1 in sides and 1 in sides:
        return None           # E3：方向冲突弃权（跨局面/跨动作 alike）
    top = passing[0]
    return (top[1], top[2], top[3])


# ============================================================ 区间方法（手工）

def alpha_for_z(z: float) -> float:
    """单侧尾部质量 α = Φ(−z)（与现行 lb_z 的名义水平对齐）。"""
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def wilson_lb(k: float, n: float, z: float) -> float:
    p = k / n
    d = 1.0 + z * z / n
    center = (p + z * z / (2.0 * n)) / d
    half = (z / d) * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n))
    return center - half


def wilson_ub(k: float, n: float, z: float) -> float:
    p = k / n
    d = 1.0 + z * z / n
    center = (p + z * z / (2.0 * n)) / d
    half = (z / d) * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n))
    return center + half


def _betacf(a: float, b: float, x: float) -> float:
    """正则化不完全贝塔的连分式（Numerical Recipes，Lentz 截断）。"""
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < 1e-300:
        d = 1e-300
    d = 1.0 / d
    h = d
    for m in range(1, BETACF_MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-300:
            d = 1e-300
        c = 1.0 + aa / c
        if abs(c) < 1e-300:
            c = 1e-300
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-300:
            d = 1e-300
        c = 1.0 + aa / c
        if abs(c) < 1e-300:
            c = 1e-300
        d = 1.0 / d
        de = d * c
        h *= de
        if abs(de - 1.0) < BETACF_EPS:
            break
    return h


def betai(a: float, b: float, x: float) -> float:
    """正则化不完全贝塔 I_x(a,b)（a,b>0，支持非整数——有效计数）。"""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    ln_bt = (math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
             + a * math.log(x) + b * math.log1p(-x))
    bt = math.exp(ln_bt)
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def qbeta(q: float, a: float, b: float) -> float:
    """Beta(a,b) 的 q 分位（二分，QBISECT_ITERS 次 → ~1e-15 精度）。"""
    lo, hi = 0.0, 1.0
    for _ in range(QBISECT_ITERS):
        mid = 0.5 * (lo + hi)
        if betai(a, b, mid) < q:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def _betacf_vec(a, b, x):
    """_betacf 的向量化（与标量路径逐元素同一运算序列）。

    每轮把已收敛元素压缩出工作集（各元素收敛轮数不同——压缩只减少
    后续运算量，不改变任何活跃元素的运算序列）。
    """
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    tiny = 1e-300
    n = x.shape[0]
    c = np.ones(n)
    d = 1.0 - qab * x / qap
    d = np.where(np.abs(d) < tiny, tiny, d)
    d = 1.0 / d
    h = d.copy()
    out_h = np.empty(n)
    idx_live = np.arange(n)
    for m in range(1, BETACF_MAXIT + 1):
        if x.size == 0:
            break
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = np.where(np.abs(d) < tiny, tiny, d)
        c = 1.0 + aa / c
        c = np.where(np.abs(c) < tiny, tiny, c)
        d = 1.0 / d
        h = h * d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = np.where(np.abs(d) < tiny, tiny, d)
        c = 1.0 + aa / c
        c = np.where(np.abs(c) < tiny, tiny, c)
        d = 1.0 / d
        de = d * c
        h = h * de
        conv = np.abs(de - 1.0) < BETACF_EPS
        if conv.any():
            out_h[idx_live[conv]] = h[conv]
            keep = ~conv
            a, b, x = a[keep], b[keep], x[keep]
            qab, qap, qam = qab[keep], qap[keep], qam[keep]
            c, d, h = c[keep], d[keep], h[keep]
            idx_live = idx_live[keep]
    # MAXIT 耗尽仍未收敛的元素（应为空集——防御）：按当前值收尾
    if idx_live.size:
        out_h[idx_live] = h
    return out_h


def _lgamma_vec(v):
    """逐元素 math.lgamma（标量路径同一 libm 调用）。"""
    return np.array([math.lgamma(float(t)) for t in v], dtype=float)


def _betai_fast(a, b, x, lg):
    """betai 的快速向量化（批路径专用）。

    与标量路径的差别仅在超越函数用 np 的 SIMD 实现（差 ≤1ulp；lgamma
    项已按 (a,b) 预计算提出二分循环）。**批的组成按条件固定**（run_
    condition 内一次成批），结果确定性与分片无关；与标量 qbeta 的一致
    性由单测按 1e-9（远超研究需求 ~1e-9 的裕度）钉住。连分式内部只有
    算术运算。
    """
    out = np.empty_like(x)
    le0 = x <= 0.0
    ge1 = x >= 1.0
    out[le0] = 0.0
    out[ge1] = 1.0
    mid = ~(le0 | ge1)
    if not mid.any():
        return out
    am, bm, xm, lgm = a[mid], b[mid], x[mid], lg[mid]
    bt = np.exp(lgm + am * np.log(xm) + bm * np.log1p(-xm))
    swap = xm >= (am + 1.0) / (am + bm + 2.0)
    keep = ~swap
    res = np.empty_like(xm)
    if keep.any():
        res[keep] = bt[keep] * _betacf_vec(am[keep], bm[keep], xm[keep]) \
            / am[keep]
    if swap.any():
        res[swap] = 1.0 - bt[swap] * _betacf_vec(bm[swap], am[swap],
                                                 1.0 - xm[swap]) / bm[swap]
    out[mid] = res
    return out


def qbeta_batch(req: np.ndarray) -> np.ndarray:
    """批量 Beta 分位反演（形状 (N,3)=(q,a,b)；与标量 qbeta 一致到
    ~1e-10，批组成固定 → 确定性；研究需求 ~1e-9 即足）。

    先 BISECT_PRE_ITERS 轮二分把区间压到 ~1e-11，再做至多 NEWTON_STEPS
    轮**带护栏的牛顿步**（解析导数；步出区间即退化为二分半缩；相对
    区间宽 ≤1e-13·max(hi,0.01) 即收敛退出——重尾形状（如 arcsine 尾）
    牛顿线性收敛，靠收敛掩码兜底）。与标量纯二分的一致性由单测钉住。
    """
    q = req[:, 0].copy()
    a = req[:, 1].copy()
    b = req[:, 2].copy()
    lg = _lgamma_vec(a + b) - _lgamma_vec(a) - _lgamma_vec(b)  # 循环外一次
    lo = np.zeros_like(q)
    hi = np.ones_like(q)
    x = 0.5 * (lo + hi)
    for _ in range(BISECT_PRE_ITERS):
        x = 0.5 * (lo + hi)
        f = _betai_fast(a, b, x, lg)
        take = f < q
        lo = np.where(take, x, lo)
        hi = np.where(take, hi, x)
    for _ in range(NEWTON_STEPS):
        tol = 1e-13 * np.maximum(hi, 1e-2)
        live = (hi - lo) > tol
        if not live.any():
            break
        f = _betai_fast(a, b, x, lg)
        take = f < q
        lo = np.where(take & live, x, lo)
        hi = np.where(take & live, hi, x)
        # 导数：x^(a-1)·(1-x)^(b-1)·exp(lg)（lg=−lnB）
        with np.errstate(divide="ignore"):
            dfdx = np.exp((a - 1.0) * np.log(x) + (b - 1.0)
                          * np.log1p(-x) + lg)
        step = f / dfdx
        xn = x - step
        bad = ~(np.isfinite(xn) & (xn > lo) & (xn < hi))
        xn = np.where(bad, 0.5 * (lo + hi), xn)
        x = np.where(live, xn, x)
    return 0.5 * (lo + hi)


def clopper_pearson_lb(k: float, n: float, alpha: float) -> float:
    if k <= 0:
        return 0.0
    return qbeta(alpha, k, n - k + 1.0)


def clopper_pearson_ub(k: float, n: float, alpha: float) -> float:
    if k >= n:
        return 1.0
    return qbeta(1.0 - alpha, k + 1.0, n - k)


def jeffreys_lb(k: float, n: float, alpha: float) -> float:
    return qbeta(alpha, k + 0.5, n - k + 0.5)


def jeffreys_ub(k: float, n: float, alpha: float) -> float:
    return qbeta(1.0 - alpha, k + 0.5, n - k + 0.5)


def ev_from_bounds(p_win_lb: float, p_loss_ub: float, est_r) -> float:
    """ev_lower 同构式合成：win 下界、loss 上界、time=1−两项（不截断）。"""
    p_time = 1.0 - p_win_lb - p_loss_ub
    return p_win_lb * est_r[0] + p_loss_ub * est_r[1] + p_time * est_r[2]


# ============================================================ DGP（合成）

@dataclass
class Sample:
    """一次抽样的标签行（码：feat 0/1/2=na；outcome 0=win/1=loss/2=time）。"""

    parent: np.ndarray    # 父格码（sit 主序：si*len(ACTIONS)+ai）
    day: np.ndarray       # 父格内日序展开成的全局 (parent,day) 唯一码
    feat: np.ndarray
    outcome: np.ndarray
    net_r: np.ndarray


def _mixed_mean(base_rate: float):
    """混合档每格真值均值向量（非赢质量按 NON_WIN_SPLIT 拆分）。"""
    pw = min(max(base_rate, 0.0), 1.0)
    rest = 1.0 - pw
    return (pw, rest * NON_WIN_SPLIT[0], rest * NON_WIN_SPLIT[1])


def _parent_mean(structure: str, situation: str, base_rate: float):
    """结构档 → 父格真值（定点角点或混合均值）。"""
    fixed = {
        "action_all_win": (1.0, 0.0, 0.0),
        "action_all_loss": (0.0, NON_WIN_SPLIT[0], NON_WIN_SPLIT[1]),
        "parent_all_win": (1.0, 0.0, 0.0) if situation == "S1" else None,
        "parent_all_loss": (0.0, NON_WIN_SPLIT[0], NON_WIN_SPLIT[1])
        if situation == "S1" else None,
    }.get(structure)
    if fixed is not None:
        return fixed
    return _mixed_mean(base_rate)


def draw_truth(cond: dict, rng: np.random.Generator) -> dict:
    """逐 (situation, action, feat) 抽真值。

    混合均值全分量 > DIRICHLET_MIN → Dirichlet(DIRICHLET_CONC·均值)；
    否则定点（边界基准率/全胜/全负角点）。na 档 = f0/f1 等权混合。
    返回 {(sit, act, feat_name): np.array([pw, pl, pt])}。
    """
    truth = {}
    for si, sit in enumerate(SITUATIONS):
        for ai, act in enumerate(ACTIONS):
            mean = _parent_mean(cond["structure"], sit, cond["base_rate"])
            cell_ps = []
            for _ in FEAT_VALUES:
                if min(mean) > DIRICHLET_MIN:
                    gam = rng.gamma(shape=[DIRICHLET_CONC * m
                                           for m in mean])
                    total = gam.sum()
                    if total <= 0:
                        p = np.array(mean, dtype=float)
                    else:
                        p = gam / total
                else:
                    p = np.array(mean, dtype=float)
                cell_ps.append(p)
            for fname, p in zip(FEAT_VALUES, cell_ps):
                truth[(sit, act, fname)] = p
            truth[(sit, act, NA_VALUE)] = 0.5 * (cell_ps[0] + cell_ps[1])
    return truth


def _layout(n_days: int, cluster: int):
    n_parents = len(SITUATIONS) * len(ACTIONS)
    row_parent = np.repeat(np.arange(n_parents), n_days * cluster)
    row_day = np.tile(np.repeat(np.arange(n_days), cluster), n_parents)
    return n_parents, row_parent, row_day


def draw_sample(cond: dict, truth: dict, rng: np.random.Generator,
                *, layout=None) -> Sample:
    """按日生成结局 → 展开为同日簇触发行（同日共享当日单一结局）。

    日级特征一次抽取（日属一个特征档，行继承；缺失行按比例降级 na）；
    K 跨界行按预写比例强制 time（labels 截尾口径的简化）；r 按行独立
    正态（结局条件分布）。
    """
    n_parents, row_parent, row_day = layout if layout is not None \
        else _layout(cond["n_days"], cond["cluster"])
    n_days, cluster = cond["n_days"], cond["cluster"]
    # 日级特征与结局（按父格×日矩阵一次抽取）
    f_day = rng.integers(0, 2, size=(n_parents, n_days))
    cell = np.empty((n_parents, len(FEAT_VALUES), 3))
    for pi in range(n_parents):
        sit, act = SITUATIONS[pi // len(ACTIONS)], ACTIONS[pi % len(ACTIONS)]
        for fi, fname in enumerate(FEAT_VALUES):
            cell[pi, fi] = truth[(sit, act, fname)]
    day_p = cell[np.arange(n_parents)[:, None], f_day, :]   # (P, days, 3)
    u = rng.random((n_parents, n_days))
    day_out = np.where(u < day_p[..., 0], 0,
                       np.where(u < day_p[..., 0] + day_p[..., 1], 1, 2))
    # 行级展开
    n_rows = n_parents * n_days * cluster
    feat = f_day[row_parent, row_day]
    feat = np.where(rng.random(n_rows) < cond["miss"], 2, feat)
    outcome = day_out[row_parent, row_day]
    outcome = np.where(rng.random(n_rows) < cond["k_cross"], 2, outcome)
    means = np.array([R_DIST[o][0] for o in range(3)])
    sds = np.array([R_DIST[o][1] for o in range(3)])
    net_r = rng.normal(means[outcome], sds[outcome])
    return Sample(parent=row_parent,
                  day=row_parent * n_days + row_day,
                  feat=feat, outcome=outcome, net_r=net_r)


def rows_to_labels_df(s: Sample) -> pd.DataFrame:
    """与 ProbTable.fit 同构的标签 DataFrame（字符串码 → 名称）。"""
    feat_names = FEAT_VALUES + (NA_VALUE,)
    return pd.DataFrame({
        "situation": [SITUATIONS[p // len(ACTIONS)] for p in s.parent],
        "action": [ACTIONS[p % len(ACTIONS)] for p in s.parent],
        FEAT_COL: [feat_names[f] for f in s.feat],
        "outcome": [_OUTCOME_NAMES[o] for o in s.outcome],
        "net_r": s.net_r,
    })


# ============================================================ fit 逐位复刻

def _replica_se(p: float, n: int) -> float:
    """tables._se 的同表达式复刻（逐位一致）。"""
    if n <= 0:
        return 0.0
    return math.sqrt(max(p * (1.0 - p), 0.0) / n)


def _replica_aggregate(k: dict, sums: dict, n: int, m: int,
                       base: CellEstimate | None) -> CellEstimate:
    """tables._aggregate 的同表达式复刻（k: {outcome:int}，sums: {outcome:
    float}——sums 按 np.sum(掩码选择) 得到，与 pandas .sum() 逐位一致）。"""
    denom_p = n + m if base is not None else n
    ps, rs = {}, {}
    for o in _OUTCOME_NAMES:
        if base is None:
            ps[o] = k[o] / n
            rs[o] = sums[o] / k[o] if k[o] else 0.0
        else:
            pseudo = m * getattr(base, f"p_{o}")
            ps[o] = (k[o] + pseudo) / denom_p
            r_denom = k[o] + pseudo
            rs[o] = ((sums[o] + pseudo * getattr(base, f"r_{o}")) / r_denom
                     if r_denom > 0 else 0.0)
    return CellEstimate(
        n=n, p_win=ps["win"], p_loss=ps["loss"], p_time=ps["time"],
        r_win=rs["win"], r_loss=rs["loss"], r_time=rs["time"],
        se_win=_replica_se(ps["win"], n), se_loss=_replica_se(ps["loss"], n))


class ReplicaTable:
    """ProbTable.fit 的逐位等价复刻（计数级，供 E0-E3 与金丝雀）。

    与真实表逐位一致的条件（单测 + 每条件金丝雀钉住）：
    - 行序与 fit 的组内行序一致（布局固定）；
    - Σr 用 np.sum(掩码选择)（与 pandas .sum() 的成对求和逐位一致，
      已在测试中跨尺寸验证）；
    - 概率/收益/SE 用与 _aggregate 完全相同的表达式与求值顺序。
    细格按需构建（决策查询只触达当轮特征值的格；金丝雀/失败案例按需
    触达全部）——不影响任何数值，只省无效分组计算。
    """

    def __init__(self, sample: Sample):
        self.sample = sample
        self._nr = sample.net_r
        self._oc_masks = tuple(sample.outcome == oi for oi in range(3))
        n_parents = len(SITUATIONS) * len(ACTIONS)
        self._n_parents = n_parents
        self._base: dict[int, CellEstimate] = {}
        self._base_counts: dict[int, tuple] = {}
        for ai in range(len(ACTIONS)):
            m0 = (sample.parent % len(ACTIONS)) == ai
            if m0.any():
                k, sums, n = self._group_stats(m0)
                self._base_counts[ai] = self._pack(k, sums, n)
                self._base[ai] = _replica_aggregate(k, sums, n,
                                                    PRIOR_STRENGTH, None)
        self._parents: dict[int, CellEstimate] = {}
        self._parent_counts: dict[int, tuple] = {}
        for pi in range(n_parents):
            m0 = sample.parent == pi
            if m0.any():
                k, sums, n = self._group_stats(m0)
                self._parent_counts[pi] = self._pack(k, sums, n)
                self._parents[pi] = _replica_aggregate(
                    k, sums, n, PRIOR_STRENGTH,
                    base=self._base.get(pi % len(ACTIONS)))
        self._cells: dict[tuple[int, int], CellEstimate | None] = {}
        self._cell_counts: dict[tuple[int, int], tuple] = {}
        self._cell_stats: dict[str, list] | None = None

    # ---- 内部 ----
    def _group_stats(self, mask):
        """一组的一次通过统计：(k:{outcome:int}, sums:{outcome:float}, n)。

        Σr 用 np.sum(掩码选择)——与 pandas .sum() 的成对求和逐位一致
        （单测跨尺寸钉住）；计数为精确整数。
        """
        n = int(mask.sum())
        k, sums = {}, {}
        for oi, oname in enumerate(_OUTCOME_NAMES):
            m = mask & self._oc_masks[oi]
            ko = int(m.sum())
            k[oname] = ko
            sums[oname] = float(np.sum(self._nr[m])) if ko else 0.0
        return k, sums, n

    @staticmethod
    def _pack(k, sums, n):
        """(kw, kl, kt, n, Σr_win, Σr_loss, Σr_time)——lookup_full 与
        失败案例共用的计数/收益和元组。"""
        return (k["win"], k["loss"], k["time"], n,
                sums["win"], sums["loss"], sums["time"])

    def _ensure_cell(self, pi: int, fi: int) -> None:
        key = (pi, fi)
        if key in self._cells:
            return
        m0 = (self.sample.parent == pi) & (self.sample.feat == fi)
        if not m0.any():
            self._cells[key] = None     # 真实 fit 的 cells 不含无行组
            return
        k, sums, n = self._group_stats(m0)
        self._cells[key] = _replica_aggregate(
            k, sums, n, PRIOR_STRENGTH,
            base=self._base.get(pi % len(ACTIONS)))
        self._cell_counts[key] = self._pack(k, sums, n)

    def _pi(self, sit, act):
        return (SITUATIONS.index(sit) * len(ACTIONS)
                + ACTIONS.index(act))

    # ---- 查询（与 ProbTable.lookup 同一回退语义）----
    def lookup_full(self, sit: str, act: str, feat_name: str):
        """→ (level, est, k_w, k_l, n_level, base_est|None)。"""
        pi = self._pi(sit, act)
        fi = (FEAT_VALUES + (NA_VALUE,)).index(feat_name)
        self._ensure_cell(pi, fi)
        cell = self._cells.get((pi, fi))
        base_est = self._base.get(pi % len(ACTIONS))
        if cell is not None and cell.n >= MIN_CELL:
            kw, kl, _kt, n = self._cell_counts[(pi, fi)][:4]
            return "cell", cell, kw, kl, n, base_est
        parent = self._parents.get(pi)
        if parent is not None:
            kw, kl, _kt, n = self._parent_counts[pi][:4]
            return "parent", parent, kw, kl, n, base_est
        if base_est is None:
            raise KeyError(f"动作 {act!r} 无任何样本")
        kw, kl, _kt, n = self._base_counts[ACTIONS.index(act)][:4]
        return "base", base_est, kw, kl, n, None

    def lookup(self, sit: str, act: str, feat_name: str):
        level, est, _kw, _kl, _n, _b = self.lookup_full(sit, act, feat_name)
        return level, est

    def base_rate(self, act: str):
        est = self._base.get(ACTIONS.index(act))
        if est is None:
            raise KeyError(f"动作 {act!r} 无任何样本")
        return "base", est

    def sample_stats(self) -> dict:
        """失败案例落盘用的样本充分统计量（细格级，按需构建全部格）。"""
        if self._cell_stats is None:
            for pi in range(self._n_parents):
                for fi in range(len(FEAT_VALUES) + 1):
                    self._ensure_cell(pi, fi)
            self._cell_stats = {}
            for (pi, fi), packed in self._cell_counts.items():
                name = "{}|{}|{}".format(
                    SITUATIONS[pi // len(ACTIONS)],
                    ACTIONS[pi % len(ACTIONS)],
                    (FEAT_VALUES + (NA_VALUE,))[fi])
                self._cell_stats[name] = list(packed)
        return self._cell_stats


def replica_table(sample: Sample) -> ReplicaTable:
    """Sample → 逐位等价复刻表（研究主循环与金丝雀共用）。"""
    return ReplicaTable(sample)


def run_canary(cond_key_str: str, sample: Sample) -> None:
    """金丝雀：真实 ProbTable.fit vs 复刻表——全部可达查询逐位比对，
    任一字段不一致 → StudyError（fail-closed，中止整个研究）。"""
    df = rows_to_labels_df(sample)
    tbl = ProbTable.fit(df, group_by=[FEAT_COL],
                        prior_strength=PRIOR_STRENGTH, min_cell=MIN_CELL)
    rep = ReplicaTable(sample)
    fields = ("n", "p_win", "p_loss", "p_time", "r_win", "r_loss", "r_time",
              "se_win", "se_loss")

    def cmp(sit, act, feat_name, real):
        level, est = rep.lookup(sit, act, feat_name)
        for f in fields:
            if getattr(est, f) != getattr(real, f):
                raise StudyError(
                    f"金丝雀失败：{cond_key_str} lookup({sit},{act},"
                    f"{feat_name}) 字段 {f} 复刻={getattr(est, f)!r} vs "
                    f"真实={getattr(real, f)!r}——研究器与现行实现不再"
                    "逐位一致，中止（fail-closed）")

    for act in ACTIONS:
        for f in fields:
            if getattr(rep.base_rate(act)[1], f) != \
                    getattr(tbl.base_rate(act), f):
                raise StudyError(f"金丝雀失败：{cond_key_str} base_rate"
                                 f"({act}) 字段 {f} 不逐位一致")
    for sit in SITUATIONS:
        for act in ACTIONS:
            for fname in FEAT_VALUES + (NA_VALUE,):
                cmp(sit, act, fname, tbl.lookup(sit, act, {FEAT_COL: fname}))


# ============================================================ 估计器

def _effective_counts(level, kw, kl, n_level, base_est):
    """E1-E3 的有效计数：格/父格向动作基准收缩 (k+m·p̂_base, n+m)；
    基准层自身（base=None 聚合）用原始计数——与 E0 同点估计、只换
    不确定度量化（口径声明：E1-E3 与 E0 共享点估计）。"""
    if level == "base" or base_est is None:
        return float(kw), float(kl), float(n_level)
    m = PRIOR_STRENGTH
    return (kw + m * base_est.p_win, kl + m * base_est.p_loss,
            n_level + m)


# ============================================================ 计量与判定

class Metrics:
    """按估计器累积：覆盖/宽度（指定候选）、误开放（空真 rep）、稳定性。"""

    __slots__ = ("covered", "reps", "width_sum", "null_reps", "false_opens",
                 "opened", "_dec1", "_dec2")

    def __init__(self):
        self.covered = 0
        self.reps = 0
        self.width_sum = 0.0
        self.null_reps = 0
        self.false_opens = 0
        self.opened = 0
        self._dec1: list = []
        self._dec2: list = []

    def update(self, *, ev_lb: float, ev_true: float, null=None,
               opened=None, dec=None):
        self.reps += 1
        if ev_lb <= ev_true:
            self.covered += 1
        self.width_sum += ev_true - ev_lb
        if null:
            self.null_reps += 1
            if opened:
                self.false_opens += 1
        if opened:
            self.opened += 1
        self._dec1.append(dec)

    def update_draw2(self, dec):
        self._dec2.append(dec)

    def finalize(self) -> dict:
        reps = self.reps
        pairs = min(len(self._dec1), len(self._dec2))
        return {
            "reps": reps,
            "covered": self.covered,
            "coverage": self.covered / reps if reps else None,
            "width_mean": self.width_sum / reps if reps else None,
            "null_reps": self.null_reps,
            "false_opens": self.false_opens,
            "false_open": (self.false_opens / self.null_reps
                           if self.null_reps else None),
            "opened": self.opened,
            "stability": (sum(1 for a, b in zip(self._dec1, self._dec2)
                              if a == b) / pairs if pairs else None),
        }


def coverage_verdict(rate: float) -> str:
    """两级门（预写）：≥95% PASS；<80% RED；其间 WARN。"""
    if rate >= GATE_COVERAGE_PASS:
        return "PASS"
    if rate < GATE_COVERAGE_RED:
        return "RED"
    return "WARN"


def false_open_verdict(rate: float) -> str:
    """两级门（预写）：≤5% PASS；>10% RED；其间 WARN。"""
    if rate <= GATE_FALSE_OPEN_PASS:
        return "PASS"
    if rate > GATE_FALSE_OPEN_RED:
        return "RED"
    return "WARN"


_WORST = {"PASS": 0, "WARN": 1, "RED": 2}


def condition_gate(coverage, false_open, *, null_reps) -> str:
    """条件总判定（详见 condition_gate_full）。"""
    return condition_gate_full(coverage, false_open,
                               null_reps)["verdict"]


def condition_gate_full(coverage, false_open, null_reps) -> dict:
    vc = coverage_verdict(coverage) if coverage is not None else "WARN"
    vf = (false_open_verdict(false_open)
          if (false_open is not None
              and null_reps >= FALSE_OPEN_MIN_NULL) else "NA")
    worst = vc if vf == "NA" else max(vc, vf, key=lambda v: _WORST[v])
    return {"coverage": coverage, "verdict_c": vc, "false_open": false_open,
            "verdict_f": vf, "verdict": worst}


# ============================================================ 条件执行

def _fmt_dec(dec):
    return None if dec is None else "|".join(map(str, dec))


def run_condition(cond: dict, *, m_reps=None, canary=True,
                  collect_failures=True, rep_range=None) -> dict:
    """跑一个条件：DGP → 四估计器 → 计量；返回指标、判定与失败案例。

    E0/E1 内联完成；E2/E3 的 Beta 分位批量反演（qbeta_batch）后回填
    ——批与标量路径一致到 ~1e-10（单测钉住；批组成按条件固定 → 确定性
    与分片无关）。
    """
    m = M_REPS if m_reps is None else int(m_reps)
    start, end = rep_range if rep_range is not None else (0, m)
    key = cond_key(cond)
    layout = _layout(cond["n_days"], cond["cluster"])
    allowed_pairs = parse_allowed_pairs(ALLOWED_HYPOTHESES)
    cand_keys = tuple((s, a) for s in SITUATIONS for a in ACTIONS)
    desig_idx = cand_keys.index(DESIGNATED)
    r_means = tuple(R_DIST[o][0] for o in range(3))
    alpha = alpha_for_z(LB_Z)
    mets = {e: Metrics() for e in ESTIMATORS}
    reqs: list = []           # (q, a, b)
    pend: list = []           # E2/E3 延迟数据
    failures_raw: list = []   # 全细节失败案例（每条件至多 FAIL_DETAIL_MAX 条）
    failure_reps = {"coverage": [], "false_open": []}   # 全量失败 rep 索引
    n_detail = 0
    canary_ran = False
    canary_set = {off for off in CANARY_REP_OFFSETS if start <= off < end}

    for rep in range(start, end):
        # 逐 rep 两支 RNG：world（真值+决策上下文）与 rows（两次抽样），
        # 消费顺序在代码内固定 → 确定性且与 rep 子段/分片无关
        rng_world = derive_rng(key, rep, "world")
        truth = draw_truth(cond, rng_world)
        feat_name = (NA_VALUE if rng_world.random() < cond["miss"]
                     else FEAT_VALUES[int(rng_world.integers(0, 2))])
        sides = [int(x) * 2 - 1
                 for x in rng_world.integers(0, 2, size=len(cand_keys))]
        cands = [(s, a, sd, COST_R)
                 for (s, a), sd in zip(cand_keys, sides)]
        ev_true_all = {}
        for (s, a) in cand_keys:
            p = truth[(s, a, feat_name)]
            ev_true_all[(s, a)] = (float(p[0]) * r_means[0]
                                   + float(p[1]) * r_means[1]
                                   + float(p[2]) * r_means[2])
        null = all(ev_true_all[ca] - COST_R <= 0.0
                   for ca in allowed_pairs)
        desig_ev_true = ev_true_all[DESIGNATED]

        draw_data = []
        rt_draw1 = None
        rng_rows = derive_rng(key, rep, "rows")
        for di in (0, 1):
            sample = draw_sample(cond, truth, rng_rows, layout=layout)
            if canary and rep in canary_set and di == 0:
                run_canary(key, sample)
                canary_ran = True
            rt = ReplicaTable(sample)
            if di == 0:
                rt_draw1 = rt
            ev0s, ev1s, r3s, slot_rows = [], [], [], []
            for (s, a, sd, cost) in cands:
                level, est, kw, kl, n_lvl, base_est = rt.lookup_full(
                    s, a, feat_name)
                r3 = (est.r_win, est.r_loss, est.r_time)
                ev0s.append(ev_lower(est, z=LB_Z))
                kw_e, kl_e, n_e = _effective_counts(level, kw, kl, n_lvl,
                                                    base_est)
                ev1s.append(ev_from_bounds(
                    wilson_lb(kw_e, n_e, LB_Z), wilson_ub(kl_e, n_e, LB_Z),
                    r3))
                # E2/E3 请求：slot=(e2_lb, e2_ub, e3_lb, e3_ub)，None=待回填
                slot = [0.0, 1.0, None, None]
                if kw_e > 0:
                    reqs.append((alpha, kw_e, n_e - kw_e + 1.0))
                    slot[0] = None
                if kl_e < n_e:
                    reqs.append((1.0 - alpha, kl_e + 1.0, n_e - kl_e))
                    slot[1] = None
                reqs.append((alpha, kw_e + 0.5, n_e - kw_e + 0.5))
                reqs.append((1.0 - alpha, kl_e + 0.5, n_e - kl_e + 0.5))
                slot_rows.append(slot)
                r3s.append(r3)
            dec0 = decide_level(cands, ev0s, threshold_mult=THRESHOLD_MULT,
                                allowed_pairs=allowed_pairs)
            dec1 = decide_level(cands, ev1s, threshold_mult=THRESHOLD_MULT,
                                allowed_pairs=allowed_pairs)
            draw_data.append((ev0s, ev1s, dec0, dec1, r3s, slot_rows))

        # E0/E1 计量（draw1 计覆盖/误开放，draw2 计稳定性）
        for e, d1, d2 in (("E0", draw_data[0], draw_data[1]),
                          ("E1", draw_data[0], draw_data[1])):
            evs = d1[0] if e == "E0" else d1[1]
            dec_a = d1[2] if e == "E0" else d1[3]
            dec_b = d2[2] if e == "E0" else d2[3]
            mets[e].update(ev_lb=evs[desig_idx], ev_true=desig_ev_true,
                           null=null, opened=(dec_a is not None), dec=dec_a)
            mets[e].update_draw2(dec_b)
        if collect_failures:
            ev_lb0 = draw_data[0][0][desig_idx]
            dec0 = draw_data[0][2]
            cov_fail = ev_lb0 > desig_ev_true
            fo_fail = null and dec0 is not None
            if cov_fail:
                failure_reps["coverage"].append(rep)
            if fo_fail:
                failure_reps["false_open"].append(rep)
            if (cov_fail or fo_fail) and n_detail < FAIL_DETAIL_MAX:
                # 失败案例才构建全格充分统计量（惰性）
                stats = rt_draw1.sample_stats()
                if cov_fail:
                    failures_raw.append(_failure_record(
                        key, rep, "coverage", ev_lb0, desig_ev_true, dec0,
                        feat_name, sides, cand_keys, truth, stats))
                if fo_fail:
                    failures_raw.append(_failure_record(
                        key, rep, "false_open", ev_lb0, desig_ev_true, dec0,
                        feat_name, sides, cand_keys, truth, stats))
                n_detail += 1
        pend.append((cands, desig_idx, desig_ev_true, null, draw_data))

    # 批量 Beta 分位反演 → E2/E3 决策与计量。
    # 回填按逐 draw×逐候选×槽位原追加顺序消费 bounds（slot=None 的常量
    # 边界——E2 的 k'≤0→lb=0 / k'≥n'→ub=1——不占请求位）。
    bounds = qbeta_batch(np.array(reqs, dtype=float)) if reqs \
        else np.zeros(0)
    pos = 0
    for cands, desig_idx, desig_ev_true, null, draw_data in pend:
        resolved = []          # 每 draw：每候选 [e2lb,e2ub,e3lb,e3ub]
        for data in draw_data:
            rows_vals = []
            for slot in data[5]:
                vals = []
                for v in slot:
                    if v is None:
                        vals.append(float(bounds[pos]))
                        pos += 1
                    else:
                        vals.append(v)
                rows_vals.append(vals)
            resolved.append(rows_vals)
        for e, base_slot in (("E2", 0), ("E3", 2)):
            evs_d1 = [ev_from_bounds(resolved[0][ci][base_slot],
                                     resolved[0][ci][base_slot + 1],
                                     draw_data[0][4][ci])
                      for ci in range(len(cands))]
            evs_d2 = [ev_from_bounds(resolved[1][ci][base_slot],
                                     resolved[1][ci][base_slot + 1],
                                     draw_data[1][4][ci])
                      for ci in range(len(cands))]
            dec_a = decide_level(cands, evs_d1, threshold_mult=THRESHOLD_MULT,
                                 allowed_pairs=allowed_pairs)
            dec_b = decide_level(cands, evs_d2, threshold_mult=THRESHOLD_MULT,
                                 allowed_pairs=allowed_pairs)
            mets[e].update(ev_lb=evs_d1[desig_idx], ev_true=desig_ev_true,
                           null=null, opened=(dec_a is not None), dec=dec_a)
            mets[e].update_draw2(dec_b)

    metrics = {e: mets[e].finalize() for e in ESTIMATORS}
    gate = condition_gate_full(metrics["E0"]["coverage"],
                               metrics["E0"]["false_open"],
                               metrics["E0"]["null_reps"])
    is_red = gate["verdict"] == "RED" and collect_failures
    return {"cond": dict(cond), "key": key, "metrics": metrics,
            "gate": gate,
            "failures": failures_raw if is_red else [],
            "failure_reps": (failure_reps if is_red
                             else {"coverage": [], "false_open": []}),
            "canary_ok": bool(canary and canary_ran)}


def _failure_record(key, rep, ftype, ev_lb, ev_true, dec, feat_name, sides,
                    cand_keys, truth, sample_stats):
    return {
        "cond": key, "rep": int(rep), "type": ftype,
        "ev_lb": float(ev_lb), "ev_true": float(ev_true),
        "decision": _fmt_dec(dec), "feat": feat_name,
        "sides": {f"{s}:{a}": int(sd)
                  for (s, a), sd in zip(cand_keys, sides)},
        "truth": {f"{s}|{a}|{f}": [float(x) for x in p]
                  for (s, a, f), p in truth.items()},
        "sample": sample_stats,
    }


# ============================================================ manifest / IO

def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_root() -> Path:
    return _repo_root() / OUT_REL_DIR


def build_manifest(*, shard_n: int) -> dict:
    root = _repo_root()
    return {
        "run_id": compute_run_id(),
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "seed_base": SEED_BASE,
        "m_reps": M_REPS,
        "shard_n": int(shard_n),
        "constants": frozen_constants(),
        "matrix": matrix_conditions(),
        "code_sha256": _sha256_file(Path(__file__).resolve()),
        "deps_sha256": {rel: _sha256_file(root / rel) for rel in _DEP_FILES},
        "env": {"python": sys.version.split()[0],
                "numpy": np.__version__, "pandas": pd.__version__},
        "prereg_anchors": list(PREREG_ANCHORS),
        "replica_canary": {
            "note": "E0 复刻聚合与真实 ProbTable.fit 逐位一致；每条件 "
                    f"rep {list(CANARY_REP_OFFSETS)} 真调用 fit 作金丝雀"
                    "（不一致即中止）",
        },
    }


def _write_json_atomic(path: Path, obj) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(_canonical_json(obj), encoding="utf-8")
    tmp.replace(path)


def _shard_name(i: int, n: int) -> str:
    return f"results.shard{i}of{n}.json"


def finalize_run(run_dir: Path, shard_n: int) -> str:
    """合并全部分片 → 门判定 + results/gate/failures（确定性输出）。"""
    conds_out: list[dict] = []
    fails: list[dict] = []
    for i in range(shard_n):
        data = json.loads((run_dir / _shard_name(i, shard_n))
                          .read_text(encoding="utf-8"))
        conds_out.extend(data["conditions"])
        for c in data["conditions"]:
            fails.extend(c.get("failures", []))
    n_expected = matrix_size()
    if len(conds_out) != n_expected:
        raise StudyError(f"分片合并不完整：{len(conds_out)}/{n_expected} 条件")
    conds_out.sort(key=lambda c: c["cond_idx"])
    if [c["cond_idx"] for c in conds_out] != list(range(n_expected)):
        raise StudyError("分片合并条件索引不连续/重复")
    fails.sort(key=lambda f: (f["cond"], f["rep"]))
    gate_conditions = {c["key"]: c["gate"] for c in conds_out}
    overall = "PASS"
    for g in gate_conditions.values():
        v = g["verdict"]
        if v == "RED":
            overall = "RED"
            break
        if v == "WARN":
            overall = "WARN"
    run_id = compute_run_id()
    results = {"run_id": run_id, "seed_base": SEED_BASE, "m_reps": M_REPS,
               "matrix_size": n_expected,
               "conditions": [{k: v for k, v in c.items()
                               if k != "failures"} for c in conds_out]}
    _write_json_atomic(run_dir / "results.json", results)
    _write_json_atomic(run_dir / "gate.json", {
        "run_id": run_id, "overall": overall,
        "gate_constants": {
            "coverage_pass": GATE_COVERAGE_PASS,
            "coverage_red": GATE_COVERAGE_RED,
            "false_open_pass": GATE_FALSE_OPEN_PASS,
            "false_open_red": GATE_FALSE_OPEN_RED,
            "false_open_min_null": FALSE_OPEN_MIN_NULL,
        },
        "prereg_anchors": list(PREREG_ANCHORS),
        "conditions": gate_conditions,
    })
    _write_json_atomic(run_dir / "failures.json", fails)
    return overall


# ============================================================ CLI

def cmd_run(args) -> int:
    shard_n, shard_i = int(args.shard_n), int(args.shard_i)
    if shard_n < 1 or not (0 <= shard_i < shard_n):
        print(f"非法分片参数：--shard-i {shard_i} --shard-n {shard_n}",
              file=sys.stderr)
        return 2
    run_id = compute_run_id()
    root = Path(args.root)
    run_dir = root / run_id
    shard_file = run_dir / _shard_name(shard_i, shard_n)
    # 目录协调（并行分片安全：manifest 原子写，写后重读校验）
    if run_dir.exists():
        foreign = [q.name for q in run_dir.iterdir()
                   if q.name != "manifest.json"
                   and not q.name.startswith("results.shard")
                   and not q.name.endswith(".tmp")]
        if foreign:
            print(f"目录 {run_dir} 含非本研究产物 {foreign[:3]}——拒绝"
                  "（只追加）", file=sys.stderr)
            return 2
    run_dir.mkdir(parents=True, exist_ok=True)
    man_path = run_dir / "manifest.json"
    if not man_path.exists():
        _write_json_atomic(man_path, build_manifest(shard_n=shard_n))
    try:
        man = json.loads(man_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"manifest.json 损坏（{e}）——失败实验原样保留，拒绝",
              file=sys.stderr)
        return 2
    if man.get("run_id") != run_id:
        print(f"目录 {run_dir} 属于 run_id={man.get('run_id')}——与本次"
              "不一致，拒绝覆盖（只追加）", file=sys.stderr)
        return 2
    if int(man.get("shard_n", shard_n)) != shard_n:
        print(f"目录分片数 {man.get('shard_n')} 与本次 --shard-n {shard_n} "
              "不一致，拒绝", file=sys.stderr)
        return 2
    if shard_file.exists():
        print(f"分片产物 {shard_file.name} 已存在——拒绝覆盖（只追加；"
              "失败实验原样保留）", file=sys.stderr)
        return 2
    indexed = list(enumerate(matrix_conditions()))[shard_i::shard_n]
    t_start = time.time()
    out_conditions = []
    for i, (cond_idx, cond) in enumerate(indexed):
        t0 = time.time()
        res = run_condition(cond, canary=True, collect_failures=True)
        out_conditions.append({"cond_idx": cond_idx, **res})
        print(f"[{i + 1}/{len(indexed)}] {res['key']} "
              f"E0cov={res['metrics']['E0']['coverage']:.3f} "
              f"verdict={res['gate']['verdict']} "
              f"fails={len(res['failures'])} ({time.time() - t0:.1f}s)",
              flush=True)
    _write_json_atomic(shard_file, {"run_id": run_id, "shard_i": shard_i,
                                    "shard_n": shard_n,
                                    "conditions": out_conditions})
    print(f"分片 {shard_i + 1}/{shard_n} 完成 "
          f"({time.time() - t_start:.1f}s) → {shard_file}")
    have = [run_dir / _shard_name(j, shard_n) for j in range(shard_n)]
    if all(p.exists() for p in have):
        overall = finalize_run(run_dir, shard_n)
        print(f"全部分片到齐 → 总判定：{overall}")
        print(f"报告：.venv/Scripts/python.exe scripts/p2_10_study.py "
              f"report {run_id} --root {root}")
    else:
        print("尚有分片未完成，等待其余 --shard-i 完成后自动合并判定")
    return 0


def build_report(run_dir: Path) -> str:
    """从结果目录生成报告 markdown 骨架（数字表+判定+失败案例+对照；
    正文结论留白由协调者/报告子代理补写）。"""
    man = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    results = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
    gate = json.loads((run_dir / "gate.json").read_text(encoding="utf-8"))
    try:
        fails = json.loads((run_dir / "failures.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        fails = []
    conds = results["conditions"]
    run_id = man["run_id"]

    def fmt(x, nd=3):
        return "NA" if x is None else f"{x:.{nd}f}"

    lines = [
        f"# P2-10 统计覆盖研究报告（骨架）——run_id={run_id}",
        "",
        "数字表与判定由研究器生成；**结论节留白（待补）**，由协调者/"
        "报告子代理按预注册标准补写。全量数字见 results.json / "
        "gate.json / failures.json。",
        "",
        "## 元信息",
        f"- run_id：`{run_id}`（sha256(代码+冻结常量)[0:16]）",
        f"- 种子基：{man['seed_base']}；每条件重复 M={man['m_reps']}；"
        f"条件数 {len(conds)}",
        f"- 冻结常量快照与依赖哈希见 manifest.json（tables/decision/"
        "level_decision/本脚本）",
        "- 统计单位=独立交易日；同日多触发共享当日单一结局=最强相关"
        "角点假设（非 labels 逐触发口径复刻）；K 跨界按 time 计（简化）",
        "",
        "## 预注册验收标准与预判锚（跑前冻结）",
        f"- 覆盖两级门：≥{man['constants']['GATE_COVERAGE_PASS']} PASS /"
        f" <{man['constants']['GATE_COVERAGE_RED']} RED；误开放两级门："
        f"≤{man['constants']['GATE_FALSE_OPEN_PASS']} PASS /"
        f" >{man['constants']['GATE_FALSE_OPEN_RED']} RED（红牌=版本化"
        "估计器变更提案义务，不直接改产品）",
    ]
    for a in man["prereg_anchors"]:
        lines.append(f"  - 预判锚：{a}")
    lines += [
        "",
        f"## 总判定：**{gate['overall']}**",
        "",
        "## 红牌条件（E0）",
        "",
        "| 条件 | 覆盖 | 误开放 | 判定 |",
        "|---|---|---|---|",
    ]
    reds = [(k, g) for k, g in gate["conditions"].items()
            if g["verdict"] == "RED"]
    for k, g in sorted(reds):
        lines.append(f"| {k} | {fmt(g['coverage'])} | {fmt(g['false_open'])}"
                     f" | {g['verdict']} |")
    if not reds:
        lines.append("| （无） | | | |")
    lines += [
        "",
        "## 覆盖汇总（E0，按结构×基准率聚合）",
        "",
        "| 结构 | 基准率 | 条件数 | 平均覆盖 | 平均误开放 | 平均宽度 |",
        "|---|---|---|---|---|---|",
    ]
    agg = {}
    for c in conds:
        gk = (c["cond"]["structure"], c["cond"]["base_rate"])
        agg.setdefault(gk, []).append(c)
    for (st, br), group in sorted(agg.items(), key=lambda kv: (kv[0][0],
                                                               kv[0][1])):
        cov = [c["metrics"]["E0"]["coverage"] for c in group]
        fos = [c["metrics"]["E0"]["false_open"] for c in group
               if c["metrics"]["E0"]["false_open"] is not None]
        wid = [c["metrics"]["E0"]["width_mean"] for c in group]
        lines.append(f"| {st} | {br} | {len(group)} | "
                     f"{fmt(sum(cov) / len(cov))} | "
                     f"{fmt(sum(fos) / len(fos) if fos else None)} | "
                     f"{fmt(sum(wid) / len(wid))} |")
    lines += [
        "",
        "## E0-E3 对照（全条件平均；不因对照结果改 E0 阈值）",
        "",
        "| 估计器 | 平均覆盖 | 平均误开放 | 平均下界宽度 | 决策稳定性 |",
        "|---|---|---|---|---|",
    ]
    for est in ESTIMATORS:
        cov = [c["metrics"][est]["coverage"] for c in conds]
        fos = [c["metrics"][est]["false_open"] for c in conds
               if c["metrics"][est]["false_open"] is not None]
        wid = [c["metrics"][est]["width_mean"] for c in conds]
        sta = [c["metrics"][est]["stability"] for c in conds
               if c["metrics"][est]["stability"] is not None]
        lines.append(
            f"| {est} | {fmt(sum(cov) / len(cov))} | "
            f"{fmt(sum(fos) / len(fos) if fos else None)} | "
            f"{fmt(sum(wid) / len(wid))} | "
            f"{fmt(sum(sta) / len(sta) if sta else None)} |")
    lines += [
        "",
        "## 失败案例（红牌条件：全量 rep 索引见各条件 failure_reps，"
        "全细节样例见 failures.json——均可由种子+rep 确定性再生）",
        "",
    ]
    rep_counts = {c["key"]: c.get("failure_reps", {}) for c in conds
                  if c["gate"]["verdict"] == "RED"}
    shown = {}
    for f in fails:
        n = shown.get(f["cond"], 0)
        if n >= 5:
            continue
        shown[f["cond"]] = n + 1
        lines.append(
            f"- {f['cond']} rep={f['rep']} 类型={f['type']} "
            f"EV_lb={f['ev_lb']:.4f} EV_true={f['ev_true']:.4f} "
            f"决策={f['decision']} 特征={f['feat']}")
    for k in sorted(rep_counts):
        fr = rep_counts[k]
        lines.append(f"- {k}：coverage 失败 {len(fr.get('coverage', []))}"
                     f" rep / 误开放 {len(fr.get('false_open', []))} rep")
    if not fails:
        lines.append("- （无红牌条件）")
    lines += [
        "",
        "## 复现命令",
        "",
        "```bash",
        f".venv/Scripts/python.exe scripts/p2_10_study.py run --root "
        f"{run_dir.parent}",
        f".venv/Scripts/python.exe scripts/p2_10_study.py report {run_id} "
        f"--root {run_dir.parent}",
        "```",
        "",
        "## 结论（留白——待补：按预注册标准逐条判定，含红牌条件与"
        "预判锚兑现情况、E1-E3 对照解读、（如有）版本化估计器变更提案）",
        "",
    ]
    return "\n".join(lines)


def cmd_report(args) -> int:
    root = Path(args.root)
    run_dir = root / args.run_id
    for name in ("manifest.json", "results.json", "gate.json"):
        if not (run_dir / name).exists():
            print(f"{run_dir / name} 不存在——先完成 run（全部分片到齐"
                  "自动生成 results/gate）", file=sys.stderr)
            return 2
    man = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    if man.get("run_id") != args.run_id:
        print(f"目录 manifest run_id={man.get('run_id')} 与参数不一致",
              file=sys.stderr)
        return 2
    md = build_report(run_dir)
    out = run_dir / "report.md"
    out.write_text(md, encoding="utf-8")
    print(f"报告骨架写入 {out}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="p2_10_study", description="P2-10 统计覆盖研究器（预注册，"
        "T107 模块 B）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run", help="跑（分片）全矩阵 → 结果+判定")
    p_run.add_argument("--shard-i", type=int, default=0)
    p_run.add_argument("--shard-n", type=int, default=1)
    p_run.add_argument("--root", type=Path, default=default_root(),
                       help=f"结果根目录（缺省 {OUT_REL_DIR}/<run_id>）")
    p_run.set_defaults(func=cmd_run)
    p_rep = sub.add_parser("report", help="从结果目录生成报告 markdown 骨架")
    p_rep.add_argument("run_id")
    p_rep.add_argument("--root", type=Path, default=default_root())
    p_rep.set_defaults(func=cmd_report)
    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except StudyError as e:
        print(f"StudyError：{e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
