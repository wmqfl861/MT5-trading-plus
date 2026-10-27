"""概率表（T72，蓝图 P 线 §2.4 / 决策 D40）：条件频率表 + 经验贝叶斯收缩。

单元格 = 局面 × 动作 × 最多 2 个分组特征；每格估计赢 / 输 / 到期三者的
比例与各自平均净收益（R，含成本）。每格向**该动作的全样本基准率**收缩
（伪样本数 m = prior_strength）；标准误按格内**实际**样本数 n 计算；
n < min_cell 的格在 lookup 时退回上一级（局面 × 动作聚合），再退到该动作
基准。不用机器学习模型（D40）。评估代码（D45：研究角色只读）。
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import pandas as pd

SCHEMA_VERSION = 1
_USABLE_OUTCOMES = ("win", "loss", "time")


@dataclass(frozen=True)
class CellEstimate:
    """一格（或父级/基准）的估计。r_* 为含成本的平均净收益（R）。"""

    n: int
    p_win: float
    p_loss: float
    p_time: float
    r_win: float
    r_loss: float
    r_time: float
    se_win: float
    se_loss: float


def _se(p: float, n: int) -> float:
    """按格内实际样本数 n 的二项标准误（n=0 时无定义，返回 0 由调用方保证不出现）。"""
    if n <= 0:
        return 0.0
    return math.sqrt(max(p * (1.0 - p), 0.0) / n)


def _aggregate(rows: pd.DataFrame, m: int,
               base: CellEstimate | None) -> CellEstimate | None:
    """聚合 rows（win/loss/time 行）为一格估计，向 base 收缩（base=None 即基准自身）。

    概率收缩（审核修正 1）：p̂_o = (k_o + m·p_base_o)/(n + m)——伪样本 m 个按
    基准率分摊到各结局。
    净收益收缩按**结局的伪样本数**（审核修正 1）：r̄_o = (Σr_o +
    m·p_base_o·r_base_o)/(k_o + m·p_base_o)——分子是"该结局的总收益"，分母
    是"该结局的样本数（含伪样本）"，n→∞ 时趋近经验条件均值 Σr_o/k_o；
    旧式 (Σr_o + m·r_base_o)/(n+m) 分母混入其他结局的计数，大样本时
    趋近 p_o×条件均值，EV 被严重低估。
    k_o 与 m·p_base_o 同时为 0（该结局格内外都无样本）→ r̄_o = 0。
    """
    n = len(rows)
    if n == 0:
        return None
    k = {o: int((rows["outcome"] == o).sum()) for o in _USABLE_OUTCOMES}
    sums = {o: float(rows.loc[rows["outcome"] == o, "net_r"].sum())
            for o in _USABLE_OUTCOMES}
    denom_p = n + m if base is not None else n
    ps, rs = {}, {}
    for o in _USABLE_OUTCOMES:
        if base is None:
            ps[o] = k[o] / n
            rs[o] = sums[o] / k[o] if k[o] else 0.0
        else:
            pseudo = m * getattr(base, f"p_{o}")
            ps[o] = (k[o] + pseudo) / denom_p
            r_denom = k[o] + pseudo
            rs[o] = ((sums[o] + pseudo * getattr(base, f"r_{o}")) / r_denom
                     if r_denom > 0 else 0.0)
    est = CellEstimate(
        n=n, p_win=ps["win"], p_loss=ps["loss"], p_time=ps["time"],
        r_win=rs["win"], r_loss=rs["loss"], r_time=rs["time"],
        se_win=_se(ps["win"], n), se_loss=_se(ps["loss"], n))
    return est


def _est_to_dict(est: CellEstimate) -> dict:
    return asdict(est)


def _est_from_dict(d: dict) -> CellEstimate:
    return CellEstimate(
        n=int(d["n"]), p_win=float(d["p_win"]), p_loss=float(d["p_loss"]),
        p_time=float(d["p_time"]), r_win=float(d["r_win"]),
        r_loss=float(d["r_loss"]), r_time=float(d["r_time"]),
        se_win=float(d["se_win"]), se_loss=float(d["se_loss"]))


class ProbTable:
    """拟合好的概率表：lookup 按格 → 父级（局面×动作）→ 动作基准回退。"""

    def __init__(self, *, group_by: tuple[str, ...], prior_strength: int,
                 min_cell: int, cells: dict, parents: dict, base_rates: dict):
        self.group_by = tuple(group_by)
        self.prior_strength = int(prior_strength)
        self.min_cell = int(min_cell)
        self._cells = cells          # {(situation, action, feat...): CellEstimate}
        self._parents = parents      # {(situation, action): CellEstimate}
        self._base = base_rates      # {action: CellEstimate}

    # ------------------------------------------------------------- 拟合

    @classmethod
    def fit(cls, labels: pd.DataFrame, *, group_by: list[str],
            prior_strength: int, min_cell: int) -> "ProbTable":
        """从 label_triggers 的产出（或同构 DataFrame）拟合。

        要求列：situation、action、outcome、net_r，以及 group_by 的每个
        特征列；outcome=skipped 的行不进概率。
        """
        if not isinstance(prior_strength, int) or isinstance(prior_strength, bool) \
                or prior_strength < 1:
            raise ValueError(f"prior_strength 须为 ≥1 整数：{prior_strength!r}")
        if not isinstance(min_cell, int) or isinstance(min_cell, bool) or min_cell < 1:
            raise ValueError(f"min_cell 须为 ≥1 整数：{min_cell!r}")
        if len(group_by) > 2:
            raise ValueError(f"分组特征最多 2 个（D40），收到 {list(group_by)}")
        need = ["situation", "action", "outcome", "net_r", *group_by]
        missing = [c for c in need if c not in labels.columns]
        if missing:
            raise ValueError(f"labels 缺列：{missing}")
        usable = labels[labels["outcome"].isin(_USABLE_OUTCOMES)]
        if len(usable) == 0:
            raise ValueError("labels 没有可用行（win/loss/time）")

        base_rates: dict[str, CellEstimate] = {}
        for action, rows in usable.groupby("action"):
            est = _aggregate(rows, prior_strength, base=None)
            if est is not None:
                base_rates[str(action)] = est

        parents: dict[tuple[str, str], CellEstimate] = {}
        for (sit, act), rows in usable.groupby(["situation", "action"]):
            est = _aggregate(rows, prior_strength,
                             base=base_rates.get(str(act)))
            if est is not None:
                parents[(str(sit), str(act))] = est

        cells: dict[tuple, CellEstimate] = {}
        if group_by:
            keys = ["situation", "action", *group_by]
            for key, rows in usable.groupby(keys):
                sit, act, feats = str(key[0]), str(key[1]), key[2:]
                est = _aggregate(rows, prior_strength,
                                 base=base_rates.get(act))
                if est is not None:
                    cells[(sit, act, *feats)] = est

        return cls(group_by=tuple(group_by), prior_strength=prior_strength,
                   min_cell=min_cell, cells=cells, parents=parents,
                   base_rates=base_rates)

    # ------------------------------------------------------------- 查询

    def base_rate(self, action: str) -> CellEstimate:
        try:
            return self._base[action]
        except KeyError:
            raise KeyError(f"动作 {action!r} 无任何样本") from None

    def lookup(self, situation: str, action: str,
               features: dict) -> CellEstimate:
        """格（n ≥ min_cell）→ 父级（局面×动作）→ 动作基准。"""
        if all(g in features for g in self.group_by):
            key = (situation, action, *(features[g] for g in self.group_by))
            cell = self._cells.get(key)
            if cell is not None and cell.n >= self.min_cell:
                return cell
        parent = self._parents.get((situation, action))
        if parent is not None:
            return parent
        return self.base_rate(action)

    # ------------------------------------------------------------- 序列化

    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "group_by": list(self.group_by),
            "prior_strength": self.prior_strength,
            "min_cell": self.min_cell,
            "base_rates": {a: _est_to_dict(e)
                           for a, e in sorted(self._base.items())},
            "parents": [{"situation": s, "action": a, **_est_to_dict(e)}
                        for (s, a), e in sorted(self._parents.items())],
            "cells": [{"situation": k[0], "action": k[1],
                       "features": dict(zip(self.group_by, k[2:])),
                       **_est_to_dict(e)}
                      for k, e in sorted(self._cells.items(),
                                         key=lambda kv: tuple(map(str, kv[0])))],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ProbTable":
        if d.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(
                f"概率表 schema_version 不支持：{d.get('schema_version')!r}")
        group_by = tuple(d["group_by"])
        cells = {(c["situation"], c["action"],
                  *(c["features"][g] for g in group_by)): _est_from_dict(c)
                 for c in d["cells"]}
        parents = {(p["situation"], p["action"]): _est_from_dict(p)
                   for p in d["parents"]}
        base = {a: _est_from_dict(e) for a, e in d["base_rates"].items()}
        return cls(group_by=group_by, prior_strength=d["prior_strength"],
                   min_cell=d["min_cell"], cells=cells, parents=parents,
                   base_rates=base)
