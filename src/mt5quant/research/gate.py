"""研究门禁四态 + 实验指纹六维 + 验收阈值快照（T107 模块 A，Issue #4）。

计划：docs/superpowers/plans/2026-11-01-t107-p0-gates-p210.md 模块 A。
本模块是**读侧单点**（P2-09 方法门、P2-08 样本量证据三分、X-05 评估代码
身份的统一读出）；不改变任何写入/判定路径——阻断动作仍在开轮 D52 冻结
（bt round open）与 round-report 强制不推进（P2-09），这里只给出统一状态
与依据。只读引用：prob_mode（METHOD_REGISTRY/PRIMARY_METHOD）、
candidates（sample_size_evidence）、level_decision/tables（阈值快照）。

**按候选结果反调验收阈值 = 违规**：ACCEPTANCE_CONSTANTS 是只读快照
（现行决策阈值 + 源文件 sha256）；消费方（round-report/报告审核）应断言
快照与登记值一致——**快照漂移即阻断**（任何"为了让本轮候选通过而调
lb_z/threshold_mult/min_cell/prior_strength"的动作都会先表现为快照漂移；
阈值变更只能走独立提案轮次，不与使用它的研究同批）。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "GATE_STATES", "GateResult", "research_gate",
    "EXPECTED_ACCEPTANCE_CONSTANTS", "ACCEPTANCE_CONSTANTS",
    "acceptance_snapshot", "acceptance_drift", "experiment_fingerprint",
]

# 四态（显式枚举；判定按 research_gate docstring 的优先序）
GATE_STATES = ("PASS", "FAIL", "INSUFFICIENT_DATA", "NOT_RUN")

# ---------------------------------------------------------------- 快照

# 登记值（T107 冻结）：现行决策层缺省——level_decision.DecisionPolicy 的
# z（=lb_z）/threshold_mult 与 LevelDecisionV1 的 PRIOR_STRENGTH/MIN_CELL
# （它们就是 ProbTable.fit 的实际入参）。改这四个数=研究口径变更，须走
# 独立提案；acceptance_snapshot() 读现行值与之比对，不一致即漂移。
EXPECTED_ACCEPTANCE_CONSTANTS: dict[str, float | int] = {
    "lb_z": 1.0,
    "threshold_mult": 2.0,
    "min_cell": 100,
    "prior_strength": 100,
}

# 快照覆盖的源文件（阈值定义处 + 收缩估计器实现处）：现行内容 sha256。
_SNAPSHOT_MODULES = (
    "mt5quant.strategies.library.prob.level_decision",
    "mt5quant.prob.tables",
)


def _project_root() -> Path | None:
    """mt5quant 包根的上一级（= 仓库 src 的父目录；定位失败 → None）。"""
    import mt5quant
    pkg = Path(mt5quant.__file__).resolve().parent          # …/src/mt5quant
    return pkg.parent.parent if len(pkg.parents) >= 2 else None


def _rel_key(path: Path) -> str:
    """快照源键：仓库相对 posix 路径（定位失败 → 绝对路径如实记录）。"""
    root = _project_root()
    if root is not None:
        try:
            return path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            pass
    return path.resolve().as_posix()


def acceptance_snapshot() -> dict:
    """验收阈值只读快照：现行值（活读）+ 登记值 + 源文件 sha256 + 漂移项。

    - ``constants``：从 level_decision 现行类常量/缺省**活读**（不复制数字
      ——源处改了，这里跟着变，漂移立刻可见）；
    - ``expected``：登记值（EXPECTED_ACCEPTANCE_CONSTANTS）；
    - ``sources``：{源文件: sha256}（改文件即换哈希）；
    - ``drift``：活读值 ≠ 登记值的键列表（空 = 未漂移；非空 = 阻断理由）。

    **按候选结果反调验收阈值=违规**：任何研究会话不得为了让候选通过而调
    这四个数；阈值变更走独立提案轮次，且变更前后快照哈希必然不同——
    消费方据此阻断。
    """
    from mt5quant.strategies.library.prob.level_decision import (
        LevelDecisionV1,
    )
    constants = {
        "lb_z": float(LevelDecisionV1.POLICY.z),
        "threshold_mult": float(LevelDecisionV1.POLICY.threshold_mult),
        "min_cell": int(LevelDecisionV1.MIN_CELL),
        "prior_strength": int(LevelDecisionV1.PRIOR_STRENGTH),
    }
    sources: dict[str, str] = {}
    for name in _SNAPSHOT_MODULES:
        import importlib
        mod = importlib.import_module(name)
        f = Path(getattr(mod, "__file__", "") or "")
        if f.is_file():
            sources[_rel_key(f)] = hashlib.sha256(
                f.read_bytes()).hexdigest()
    return {
        "constants": constants,
        "expected": dict(EXPECTED_ACCEPTANCE_CONSTANTS),
        "sources": sources,
        "drift": acceptance_drift(constants),
    }


def acceptance_drift(constants: dict) -> list[str]:
    """活读阈值 vs 登记值 → 漂移键列表（空 = 未漂移；非空 = 阻断理由）。"""
    return sorted(k for k, v in EXPECTED_ACCEPTANCE_CONSTANTS.items()
                  if not _same_number(constants.get(k), v))


def _same_number(a, b) -> bool:
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return False


# 模块级快照（导入时计算；活读源文件与常量——漂移在测试与消费方断言）
ACCEPTANCE_CONSTANTS: dict = acceptance_snapshot()


# ---------------------------------------------------------------- 四态

@dataclass(frozen=True)
class GateResult:
    """研究门禁判定：四态 + 理由 + 输入/证据明细 + 阈值快照。

    status ∈ GATE_STATES（见 research_gate 的优先序）；acceptance 恒携带
    ACCEPTANCE_CONSTANTS（消费方可断言未漂移）。"""

    status: str
    reasons: tuple[str, ...]
    method_id: str
    detail: dict = field(default_factory=dict)
    acceptance: dict = field(default_factory=lambda: dict(ACCEPTANCE_CONSTANTS))

    def describe(self) -> str:
        """单行表述（round-report 接线用）：`研究门禁：<态>——<理由；…>`。"""
        return f"研究门禁：{self.status}——" + "；".join(self.reasons)


def research_gate(*, method_id: str, days: int | None = None,
                  dp: float | None = None, n_hyp: int | None = None,
                  calibration_ok: bool = True,
                  data_root=None, split=None, counted=None) -> GateResult:
    """研究门禁四态（按下列优先序**首个命中**；T107 模块 A）。

    1. **NOT_RUN**：method_id 未登记于 METHOD_REGISTRY，或登记状态 =
       calibrating（标定中——方法还没跑出可判定的东西）；
    2. **FAIL**：登记 adopted=False（含 failed_cluster_control /
       not_adopted_* 全部形态）**或**显式传入 calibration_ok=False。
       校准/批准失败必须阻断：**不存在任何参数组合（dp/days/n_hyp）能从
       FAIL 到 PASS**——这是与 INSUFFICIENT_DATA 的本质区别（后者补数据
       可达 PASS，前者只有方法重新标定并获批登记）；
    3. **INSUFFICIENT_DATA**：方法 adopted=True 但样本量证据不足——
       evidence ∈ {lower_bound_only, uncalibrated}，或 days < 该 dp 的
       充分门槛。**门槛来源 = candidates.sample_size_evidence 的同一判定
       （max(解析 D52, T75 实测)），本函数不另立数字**；dp/days 给不出
       （None）同样按本态处理并在理由注明缺了什么；
    4. **PASS**：adopted=True 且 evidence=calibrated_sufficient 且 days
       达标。**当前仓库三方法全部 adopted=False → 任何输入都到不了 PASS**
       （诚实状态，测试钉死；测试构造 PASS 须临时登记探针条目）。

    输入：method_id 必填（METHOD_REGISTRY 的键）；days/dp/n_hyp 为
    sample_size_evidence 的输入（n_hyp 缺省 1，与该函数缺省一致）；
    calibration_ok 缺省 True（不额外注入失败）；data_root/split/counted
    可选——只作为证据明细随 result 记录（供复核样本量判定的取数来源），
    本函数不自行从 counted 推 dp/days（期望效应来自候选登记，触发日数
    来自计数工具——都是调用方的判定输入，这里不替它猜）。
    """
    if not isinstance(method_id, str) or not method_id.strip():
        raise ValueError(f"method_id 必须是非空字符串，收到 {method_id!r}")
    # 运行时经 prob_mode 取登记（而非 import 时复制引用——登记是活数据）
    from mt5quant.validation import prob_mode
    from mt5quant.research.candidates import sample_size_evidence

    registry = prob_mode.METHOD_REGISTRY
    inputs = {"method_id": method_id, "days": days, "dp": dp,
              "n_hyp": (1 if n_hyp is None else int(n_hyp)),
              "calibration_ok": bool(calibration_ok),
              "data_root": (str(data_root)
                            if data_root is not None else None),
              "split": (str(split) if split is not None else None),
              "counted": (_counted_note(counted))}
    entry = registry.get(method_id)

    # ---- 1) NOT_RUN（优先于 FAIL：calibrating 且登记在册 → 未运行）
    if entry is None:
        return GateResult(
            status="NOT_RUN",
            reasons=(f"方法 {method_id!r} 不在 METHOD_REGISTRY（登记缺失"
                     "按未运行处理，fail-closed——登记缺失不是放行理由）",),
            method_id=method_id, detail={"registry": None, "inputs": inputs})
    status = str(entry.get("status", ""))
    if status == "calibrating":
        return GateResult(
            status="NOT_RUN",
            reasons=(f"方法 {method_id!r} 状态=calibrating（标定中）——"
                     "按 NOT_RUN 处理（方法尚未产出可判定的证据）",),
            method_id=method_id,
            detail={"registry": _entry_brief(method_id, entry),
                    "inputs": inputs})

    # ---- 2) FAIL（adopted=False / 校准失败 / 验收口径漂移——必须阻断）
    reasons: list[str] = []
    try:
        snapshot = acceptance_snapshot()
    except Exception as exc:
        # 无法读取验收口径本身即不能证明研究使用了冻结阈值。
        return GateResult(
            status="FAIL",
            reasons=(f"验收阈值快照不可用（{type(exc).__name__}: {exc}）"
                     "——fail-closed，禁止研究放行",),
            method_id=method_id,
            detail={"registry": _entry_brief(method_id, entry),
                    "inputs": inputs,
                    "acceptance_snapshot_error":
                        f"{type(exc).__name__}: {exc}"})
    if snapshot.get("drift"):
        reasons.append(
            "验收阈值相对冻结登记值发生漂移："
            f"{snapshot['drift']}——必须走独立版本化提案，当前研究不得放行")
    if not entry.get("adopted"):
        reasons.append(
            f"方法 {method_id} 未获批准（registry status={status}、"
            "adopted=False）——校准/批准失败必须阻断：不存在任何参数组合"
            "能从 FAIL 到 PASS")
    if calibration_ok is False:
        reasons.append(
            "显式校准失败（calibration_ok=False）——必须阻断（优先于样本"
            "量判定；样本量再充分也不放行）")
    if reasons:
        registry_now = "；".join(
            f"{k}={str(v.get('status'))}" for k, v in sorted(registry.items()))
        all_false = ("（全部 adopted=False，见 METHOD_REGISTRY）"
                     if registry and not any(v.get("adopted")
                                             for v in registry.values())
                     else "（见 METHOD_REGISTRY）")
        reasons.append(f"METHOD_REGISTRY 现况：{registry_now}{all_false}")
        note = str(entry.get("note") or "")
        if note:
            reasons.append(f"登记注记：{note}")
        return GateResult(
            status="FAIL", reasons=tuple(reasons), method_id=method_id,
            detail={"registry": _entry_brief(method_id, entry),
                    "inputs": inputs, "acceptance_snapshot": snapshot},
            acceptance=snapshot)

    # ---- 3)/4) adopted=True：样本量证据三分（sample_size_evidence 同一判定）
    if dp is None or days is None:
        missing = []
        if dp is None:
            missing.append("dp（候选登记的期望效应 expected_delta_p）")
        if days is None:
            missing.append("days（触发日数）")
        return GateResult(
            status="INSUFFICIENT_DATA",
            reasons=(f"样本量证据无法判定：缺 {'、'.join(missing)}"
                     "——按 INSUFFICIENT_DATA 处理（不猜、不默认达标）",),
            method_id=method_id,
            detail={"registry": _entry_brief(method_id, entry),
                    "inputs": inputs, "evidence": None,
                    "acceptance_snapshot": snapshot},
            acceptance=snapshot)
    decision, state, detail_txt = sample_size_evidence(
        float(dp), days, n_hyp=(1 if n_hyp is None else int(n_hyp)))
    evidence = {"decision": decision, "state": state, "detail": detail_txt,
                "threshold_source": "candidates.sample_size_evidence"
                                    "（max(解析 D52, T75 实测)；不另立数字）"}
    if decision == "pass":
        return GateResult(
            status="PASS", method_id=method_id,
            reasons=(f"方法 {method_id} 已获批（{status}）且样本量证据"
                     f"calibrated_sufficient：{detail_txt}",),
            detail={"registry": _entry_brief(method_id, entry),
                    "inputs": inputs, "evidence": evidence,
                    "acceptance_snapshot": snapshot},
            acceptance=snapshot)
    return GateResult(
        status="INSUFFICIENT_DATA", method_id=method_id,
        reasons=(f"样本量证据不足（{state}）：{detail_txt}",),
        detail={"registry": _entry_brief(method_id, entry),
                "inputs": inputs, "evidence": evidence,
                "acceptance_snapshot": snapshot},
        acceptance=snapshot)


def _entry_brief(method_id: str, entry: dict) -> dict:
    return {"method_id": method_id, "status": entry.get("status"),
            "adopted": bool(entry.get("adopted"))}


def _counted_note(counted) -> Any:
    """counted 只记复核线索（行数），不复制数据进结果。"""
    if counted is None:
        return None
    try:
        return {"rows": int(len(counted))}
    except TypeError:
        return {"provided": True}


# ---------------------------------------------------------------- 指纹

def _canonical(obj) -> str:
    """规范化序列化（键序无关、无冗余空白；numpy 等经 str 兜底）。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), default=str)


def _require_nonempty_str(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"实验指纹组件缺失：{name} 必须是非空字符串"
                         f"（收到 {value!r}）——必填组件缺失即拒绝，不静默"
                         "空串（P2-07 教训）")
    return value


def _require_mapping(value, name: str, *, allow_empty: bool = False) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"实验指纹组件缺失：{name} 必须是字典（收到 "
                         f"{type(value).__name__}）")
    if not allow_empty and not value:
        raise ValueError(f"实验指纹组件缺失：{name} 不能为空字典——空内容"
                         "不构成身份（P2-07：不静默空串）")
    return value


def experiment_fingerprint(*, strategy_ref: str, card: str, model_dict: dict,
                           bars_meta: dict, cost_version: str,
                           sessions_sha: str, params: dict, features,
                           evaluator_sha: str | None = None) -> dict:
    """实验指纹六维 → ``{"run_id", "sha256", "components"}``（T107 模块 A）。

    六个显式组件（任何一个变化 → sha256 变）：
    - **code**：策略引用 + 卡号 + 闭包文件集（经 cards.strategy_files 取
      策略模块及其项目内 import 闭包，逐文件 sha256；闭包不可得 → files
      记 None 并注明——引用本身仍是必填）；
    - **data**：bars_meta 原样规范化（generation/content_sha256/根数等
      由调用方按 manifest 传入）；
    - **params**：运行参数 params + 冻结模型 model_dict（模型即拟合参数，
      与 params 同为参数维）；
    - **features**：排序后的特征名列表（顺序无关，集合敏感）；
    - **cost**：cost_version + sessions_sha（成本模型与禁入窗配置身份）；
    - **evaluator**：evaluator_sha（复用 bt.py `_p1_evaluator_sha` 的产出
      口径）；**缺省 None → 组件记 None 并显式标注"未绑定"，不冒充覆盖**
      （X-05：评估代码身份缺失必须可见，不能静默当作已覆盖）。

    必填组件（code/data/params/features/cost）缺失 → ValueError（不静默
    空串）。sha256 = components 规范化序列化的哈希；run_id = sha256 前
    16 位 hex（报告/结果目录的短标识；同一指纹幂等，输入变化即新身份）。
    """
    from mt5quant.research import cards

    # ---- 校验（必填组件缺失 → ValueError）
    strategy_ref = _require_nonempty_str(strategy_ref, "code.strategy_ref")
    card = _require_nonempty_str(card, "code.card")
    bars_meta = _require_mapping(bars_meta, "data.bars_meta")
    params = _require_mapping(params, "params.params")
    _require_mapping(model_dict, "params.model_dict")
    cost_version = _require_nonempty_str(cost_version, "cost.cost_version")
    sessions_sha = _require_nonempty_str(sessions_sha, "cost.sessions_sha")
    if features is None:
        raise ValueError("实验指纹组件缺失：features 不能为 None")
    feats = list(features)
    if not feats:
        raise ValueError("实验指纹组件缺失：features 不能为空——特征集是"
                         "模型格定义的一部分（P2-07：不静默空串）")
    for f in feats:
        if not isinstance(f, str) or not f.strip():
            raise ValueError(f"features 元素必须是非空字符串，收到 {f!r}")

    # ---- code：策略引用 + 闭包文件集（best-effort；引用本身必填）
    closure_note = "闭包经 cards.strategy_files 解析"
    try:
        files = {p.as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in sorted(cards.strategy_files(strategy_ref),
                                 key=lambda p: p.as_posix())}
        if not files:
            files = None
            closure_note = "闭包解析为空集——仅记策略引用"
    except (ValueError, OSError) as e:
        files = None
        closure_note = f"闭包不可得（{e}）——仅记策略引用"
    code = {"strategy_ref": strategy_ref, "card": card, "files": files,
            "closure_note": closure_note}

    # ---- evaluator：None = 显式未绑定（不冒充覆盖）
    if evaluator_sha is None:
        evaluator = {"sha": None, "bound": False,
                     "note": "评估代码未绑定（evaluator_sha=None——复用 "
                             "bt.py _p1_evaluator_sha 的产出可绑定；缺失"
                             "必须可见，不冒充覆盖）"}
    else:
        evaluator = {"sha": _require_nonempty_str(evaluator_sha,
                                                 "evaluator.evaluator_sha"),
                     "bound": True}

    components = {
        "code": code,
        "data": dict(bars_meta),
        "params": {"params": dict(params), "model": dict(model_dict)},
        "features": sorted(feats),
        "cost": {"cost_version": cost_version, "sessions_sha": sessions_sha},
        "evaluator": evaluator,
    }
    sha256 = hashlib.sha256(_canonical(components).encode("utf-8")) \
        .hexdigest()
    return {"run_id": sha256[:16], "sha256": sha256,
            "components": components}
