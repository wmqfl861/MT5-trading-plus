"""合并 gate_calibration 分片产物为正式 gate-power 报告。

用法：
    ./.venv/Scripts/python.exe scripts/merge_gate_power.py \
        --shards "data/reports/gates/shard-0,data/reports/gates/shard-1,..." \
        --out data/reports/gates

各分片由 `gate_calibration.py --seeds-from K --seeds N --out <shard-dir>` 产出，
种子段互不相交。本脚本拼接三个实验臂的 runs 记录，用与原脚本同一套聚合函数
（aggregate_rates / collect_flags / render_report / compute_flags——R6/C8 起
flags 也走单进程的 compute_flags，正对照与 G5 检出率双源遍历）重算比率与
门槛建议，产出与单进程版完全同构的 gate-power.{json,md}。

R6 修正（第六轮审核 C8/M2）：
- schema 对齐真实产物：G5 runs 按 **setup** 分组（生成器写 setup，不写 key）；
  正对照 calibration 保持 {setup: {level: …}} **字典**（转列表会让
  render_report 的 .get(key) 直接 AttributeError）；
- G5 误报臂按 **null**（day/slot）分桶分别聚合（绝不混桶平均——100% 与 0%
  混成 50% 违反"任一零假设超限即标记"的取最差规则）；flags 的最差值由
  compute_flags 对各 null 取最大；
- 种子校验除 meta 声明段互不相交外，还核对 runs 里的**实际种子**：每个实验
  条件（负对照 name×null、正对照 setup×level、G5 setup×level×arm×null）
  必须恰好覆盖本分片声明的种子段——重复/缺样/越界都拒绝合并；
- 仓库根定位（M2）：__file__ 同目录有 gate_calibration.py（正常部署）→ 用
  其父级；否则**必须**由环境变量 MT5QUANT_REPO 提供——都没有明确报错，
  不再有个人工作区绝对路径兜底（硬编码路径在他机/CI 不可用）。

R7 修正（第七轮审核 N-I4）：
- **应存在的完整条件清单**：从实验设计（meta：baselines/零假设模式与各卡
  缺省 null/positive_symbols/levels）生成分片应有的全部实验条件——负对照
  baselines×nulls + RND@{key}×nulls、正对照 setups×levels、G5
  setups×levels×arms×nulls。旧校验只遍历"已经出现的组"：删掉整组记录
  （如某分片全部 XAUUSD H1 × level=1.0 正对照）时该组根本不进校验——
  现在整组缺样也拒绝合并并列出条件；
- **实验身份校验**：meta 比对从 baselines 扩展到 phase/levels/null 配置
  （null_hypothesis + null_defaults）/positive_symbols/initial_capital/
  risk_per_trade/g2_random_entry_sims/g5_bootstrap_sims——任一不一致即
  拒绝（不同实验的 runs 不能拼成同一实验的汇总）。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

_here = Path(__file__).resolve()


def _resolve_repo() -> Path:
    """仓库根（scripts/gate_calibration.py 所在目录的父级），M2/R6。

    1) __file__ 同目录存在 gate_calibration.py（仓库内正常运行）→ 其父级；
    2) 否则环境变量 MT5QUANT_REPO（测试把本脚本复制到临时目录时注入），
       且其下必须真的有 scripts/gate_calibration.py；
    3) 都没有 → RuntimeError（绝不静默回退到任何硬编码绝对路径）。
    """
    if (_here.parent / "gate_calibration.py").is_file():
        return _here.parents[1]
    env = os.environ.get("MT5QUANT_REPO")
    if env:
        repo = Path(env)
        if (repo / "scripts" / "gate_calibration.py").is_file():
            return repo
        raise RuntimeError(
            f"MT5QUANT_REPO={env} 下没有 scripts/gate_calibration.py"
            "——请指向本仓库根目录")
    raise RuntimeError(
        "无法定位仓库根：merge_gate_power.py 同目录没有 gate_calibration.py，"
        "且环境变量 MT5QUANT_REPO 未设置（应指向仓库根，含 "
        "scripts/gate_calibration.py）")


REPO = _resolve_repo()
_spec = importlib.util.spec_from_file_location(
    "gate_calibration", REPO / "scripts" / "gate_calibration.py")
gc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gc)

import numpy as np  # noqa: E402


def _agg(runs: list[dict]) -> dict[str, float]:
    return gc.aggregate_rates(runs)


def merge_negative(shards: list[dict]) -> dict:
    runs = [r for s in shards for r in s["negative"]["runs"]]
    merged = [r for s in shards for r in s["negative"].get("merged", [])]
    buckets: dict[tuple, list] = {}
    for run in runs:
        for check, value in run["checks"].items():
            buckets.setdefault((run.get("null", "day"), check), []).append(bool(value))
    by_null: dict[str, dict[str, float]] = {}
    for (null, check), values in buckets.items():
        by_null.setdefault(null, {})[check] = float(np.mean(values))
    worst = {check: max(rates.get(check, 0.0) for rates in by_null.values())
             for check in {c for r in by_null.values() for c in r}}
    mbuckets: dict[str, list[bool]] = {}
    for row in merged:
        mbuckets.setdefault(f"{row['name']}|{row['null']}", []).append(bool(row["merged_pass"]))
    return {"runs": runs, "by_null": by_null, "false_positive_rates": worst,
            "merged": merged,
            "merged_pass_rates": {k: float(np.mean(v)) for k, v in mbuckets.items()}}


def merge_positive(shards: list[dict]) -> dict:
    """正对照合并（R6/C8）：runs 按 setup（生成器实际写的键）× level 拼接
    重算检出率；calibration 保持 {setup: {level: …}} 字典——校准与分片种子
    无关（calibrate_drift 恒用 seed=0 的同数据），各分片应逐位一致，不一致
    即数据/成本不同源，拒绝合并。"""
    runs = [r for s in shards for r in s["positive"]["runs"]]
    by_key_level: dict[str, dict[str, list]] = {}
    for run in runs:
        by_key_level.setdefault(run["setup"], {}).setdefault(
            str(run["level"]), []).append(run)
    detection = {key: {level: _agg(bucket) for level, bucket in levels.items()}
                 for key, levels in by_key_level.items()}
    calibration: dict[str, dict] = {}
    for s in shards:
        c = s["positive"].get("calibration")
        if not isinstance(c, dict):
            continue
        for key, by_level in c.items():
            if key in calibration and calibration[key] != by_level:
                raise ValueError(
                    f"分片 calibration 不一致（setup {key}）——各分片必须"
                    "来自同一数据/成本/档位配置")
            calibration.setdefault(key, by_level)
    return {"runs": runs, "detection_rates": detection, "calibration": calibration}


def merge_g5(shards: list[dict]) -> dict:
    """G5 判别力合并（R6/C8）：检出臂按 setup（真实产物键，不是 key）×
    level；误报臂按 **null** 分桶（day/slot 两种零假设分别聚合，结构同
    单进程 g5_power 的 {null: {check: rate}}，决策规则的最差值由
    compute_flags 对各 null 取最大——绝不混桶平均）。"""
    out = {}
    for s in shards:
        g5 = s.get("g5")
        if g5:
            out = dict(g5)          # 窗口/设计等元数据各分片一致，取首个
            break
    runs = [r for s in shards for r in (s.get("g5") or {}).get("runs", [])]
    det_buckets: dict[str, dict[str, list]] = {}
    fp_buckets: dict[str, list] = {}
    for run in runs:
        if run.get("arm") == "detection":
            det_buckets.setdefault(run["setup"], {}).setdefault(
                str(run["level"]), []).append(run)
        else:
            fp_buckets.setdefault(str(run.get("null") or "?"), []).append(run)
    detection = {key: {level: _agg(bucket) for level, bucket in levels.items()}
                 for key, levels in det_buckets.items()}
    fp = {null: _agg(bucket) for null, bucket in fp_buckets.items() if bucket}
    out.update({"runs": runs, "detection_rates": detection,
                "false_positive_rates": fp})
    return out


def merge_meta(shards: list[dict]) -> dict:
    meta = dict(shards[0]["meta"])
    total = sum(int(s["meta"].get("seeds", 0)) for s in shards)
    meta["seeds"] = total
    meta["shards"] = sorted(
        ({"from": int(s["meta"].get("seeds_from", 0)),
          "n": int(s["meta"].get("seeds", 0))} for s in shards),
        key=lambda d: d["from"])
    meta.pop("seeds_from", None)
    meta["merged_from_shards"] = True
    meta["generated_utc"] = shards[-1]["meta"].get("generated_utc")
    return meta


def recompute_flags(results: dict) -> list[dict]:
    """R6/C8：与单进程主流程共用 gc.compute_flags（正对照 + G5 检出率双源
    遍历、G5 误报按 null 取最差）。"""
    return gc.compute_flags(results)


# 实验身份键（N-I4/R7）：这些 meta 字段定义"这是哪个实验"——不一致的
# 分片不能拼成同一份 gate-power（seeds/seeds_from/generated_utc 除外，
# 前两者按设计各分片不同，后者只记时间）。
META_IDENTITY_KEYS = (
    "phase", "baselines", "null_hypothesis", "null_defaults", "levels",
    "positive_symbols", "initial_capital", "risk_per_trade",
    "g2_random_entry_sims", "g5_bootstrap_sims",
    "fingerprint",
)


def _meta_identity_problems(docs: list[dict]) -> list[str]:
    """N-I4/R7：分片 meta 实验身份一致性——除 baselines 外还要求 levels、
    零假设配置（模式 + 各卡缺省 null）、正对照品种、资金/风险参数、G2/G5
    模拟次数逐分片一致，不一致即拒绝合并。"""
    problems: list[str] = []
    ref = docs[0].get("meta") or {}
    for key in META_IDENTITY_KEYS:
        ref_val = ref.get(key)
        for idx, s in enumerate(docs[1:], start=1):
            other = (s.get("meta") or {}).get(key)
            if other != ref_val:
                problems.append(
                    f"分片 meta.{key} 不一致（分片0={ref_val!r}，"
                    f"分片{idx}={other!r}）——实验身份不同，拒绝合并")
    return problems


def _expected_conditions(doc: dict, idx: int = 0) -> tuple[set[tuple], list[str]]:
    """N-I4/R7：从实验设计（meta）生成该分片**应存在**的完整实验条件集合。

    生成规则与 gate_calibration.main 的编排一致：
    - negative（分片含该段时）：baselines × nulls_for(模式, 卡缺省 null)
      + 每个正对照品种的 RND@{key} × nulls_for(模式, "slot")；
    - positive：positive_symbols × levels；
    - g5：positive_symbols × levels ×（detection + 误报臂 × 两种零假设——
      g5_power 恒跑 NULL_ORDER 全部）。

    条件键与 _seed_problems 的分组键同构。meta 缺生成所需字段（无法从设计
    推出清单）时返回问题而非静默放行。返回 (条件集合, 问题清单)。"""
    meta = doc.get("meta") or {}
    problems: list[str] = []
    mode = meta.get("null_hypothesis")
    defaults = dict(meta.get("null_defaults") or {})
    for b in getattr(gc, "BASELINES", ()):        # 兜底：仓库缺省 null
        defaults.setdefault(b["name"], b.get("null", "day"))
    levels = [str(float(lv)) for lv in (meta.get("levels") or [])]
    out: set[tuple] = set()

    def _nulls(default: str) -> list[str]:
        if mode is None:
            problems.append(
                f"分片{idx} meta 缺 null_hypothesis——无法从实验设计生成应"
                "存在的负对照条件清单，拒绝合并")
            return []
        try:
            return list(gc.nulls_for(mode, default))
        except ValueError as e:
            problems.append(f"分片{idx} meta.null_hypothesis 无法展开：{e}")
            return []

    if doc.get("negative"):
        names = list(meta.get("baselines") or [])
        if not names:
            problems.append(f"分片{idx} meta 缺 baselines——无法生成应存在的"
                            "负对照条件清单，拒绝合并")
        for name in names:
            for null in _nulls(defaults.get(name, "day")):
                out.add(("negative", name, null))
        keys = list(meta.get("positive_symbols") or [])
        for key in keys:
            for null in _nulls("slot"):
                out.add(("negative", f"RND@{key}", null))
    if doc.get("positive"):
        keys = list(meta.get("positive_symbols") or [])
        if not keys or not levels:
            problems.append(f"分片{idx} meta 缺 positive_symbols/levels——"
                            "无法生成应存在的正对照条件清单，拒绝合并")
        for key in keys:
            for lv in levels:
                out.add(("positive", key, lv))
    if doc.get("g5"):
        keys = list(meta.get("positive_symbols") or [])
        if not keys or not levels:
            problems.append(f"分片{idx} meta 缺 positive_symbols/levels——"
                            "无法生成应存在的 G5 条件清单，拒绝合并")
        for key in keys:
            for lv in levels:
                out.add(("g5", key, lv, "detection", None))
                for null in gc.NULL_ORDER:
                    out.add(("g5", key, lv, "false_positive", null))
    return out, problems


def _seed_problems(docs: list[dict]) -> list[str]:
    """分片种子校验（R6/C8 + R7/N-I4）：meta 声明段互不相交之外，再核
    runs 里的实际种子——**先从实验设计生成应存在的完整条件清单**（旧实现
    只遍历已出现的组，整组缺样检测不到），然后每个实验条件必须恰好覆盖
    本分片声明的种子段（无重复、无缺样、无越界）。返回问题清单（空=通过）。"""
    problems: list[str] = []
    for idx, s in enumerate(docs):
        meta = s.get("meta") or {}
        start = int(meta.get("seeds_from", 0))
        count = int(meta.get("seeds", 0))
        declared = set(range(start, start + count))
        groups: dict[tuple, list[int]] = {}

        def _add(kind: tuple, run: dict) -> None:
            groups.setdefault(kind, []).append(int(run["seed"]))

        for run in (s.get("negative") or {}).get("runs", []):
            _add(("negative", run.get("name"), run.get("null")), run)
        for run in (s.get("positive") or {}).get("runs", []):
            _add(("positive", run.get("setup"), str(run.get("level"))), run)
        for run in (s.get("g5") or {}).get("runs", []):
            _add(("g5", run.get("setup"), str(run.get("level")),
                  run.get("arm"), run.get("null")), run)
        expected, derive_problems = _expected_conditions(s, idx)
        for p in derive_problems:
            problems.append(p)
        # N-I4：设计要求的条件整组缺样（该条件在本分片一条记录都没有）
        for kind in sorted(expected - set(groups), key=repr):
            problems.append(
                f"分片{idx} {kind}: 整组缺样——缺种子 {sorted(declared)}"
                "（实验设计要求本分片在该条件下跑满声明种子段）")
        for kind, seeds in sorted(groups.items(), key=repr):
            got = set(seeds)
            dup = sorted(x for x in got if seeds.count(x) > 1)
            if dup:
                problems.append(f"分片{idx} {kind}: 种子重复 {dup}")
            missing = sorted(declared - got)
            if missing:
                problems.append(f"分片{idx} {kind}: 缺种子 {missing}")
            extra = sorted(got - declared)
            if extra:
                problems.append(f"分片{idx} {kind}: 声明段外种子 {extra}")
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="合并 gate-power 分片产物")
    ap.add_argument("--shards", required=True,
                    help="逗号分隔的分片目录（各含 gate-power.json）")
    ap.add_argument("--out", default=str(REPO / "data" / "reports" / "gates"))
    args = ap.parse_args(argv)

    docs = []
    for d in args.shards.split(","):
        p = Path(d.strip()) / "gate-power.json"
        docs.append(json.loads(p.read_text(encoding="utf-8")))
    if len({tuple(sorted(s["meta"]["baselines"])) for s in docs}) != 1:
        print("✗ 分片的 baselines 不一致，拒绝合并")
        return 1
    identity = _meta_identity_problems(docs)          # N-I4/R7：实验身份
    if identity:
        for p in identity:
            print(f"✗ {p}")
        print("✗ 分片实验身份（meta：levels/零假设配置/资金/风险/模拟次数等）"
              "不一致，拒绝合并")
        return 1
    seed_sets = [range(int(s["meta"].get("seeds_from", 0)),
                       int(s["meta"].get("seeds_from", 0))
                       + int(s["meta"].get("seeds", 0)))
                 for s in docs]
    all_seeds = [x for r in seed_sets for x in r]
    if len(all_seeds) != len(set(all_seeds)):
        print("✗ 分片种子重叠，拒绝合并")
        return 1
    problems = _seed_problems(docs)
    if problems:
        for p in problems:
            print(f"✗ {p}")
        print("✗ 分片实际运行与实验设计不符（条件整组缺样/种子重复/缺样/"
              "越界），拒绝合并")
        return 1

    try:
        results = {"meta": merge_meta(docs)}
        if "negative" in docs[0]:
            results["negative"] = merge_negative(docs)
        if "positive" in docs[0]:
            results["positive"] = merge_positive(docs)
            results["meta"]["detection_min_note"] = docs[0]["meta"].get(
                "detection_min_note")
        if any("g5" in d for d in docs):
            results["g5"] = merge_g5(docs)
        results["flags"] = recompute_flags(results)
    except (KeyError, ValueError) as e:
        print(f"✗ 分片产物结构与合并器不兼容：{type(e).__name__}: {e}")
        return 1

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "gate-power.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2,
                   default=gc._json_default), encoding="utf-8")
    (out / "gate-power.md").write_text(
        gc.render_report(results), encoding="utf-8")
    n_runs = sum(len((results.get(k) or {}).get("runs", []))
                 for k in ("negative", "positive", "g5"))
    print(f"— 合并 {len(docs)} 分片 / {len(all_seeds)} 种子 / {n_runs} 条运行 "
          f"→ {out / 'gate-power.md'}")
    flags = results["flags"]
    if flags:
        print(f"⚠ 决策规则触发 {len(flags)} 项："
              f"{[f['check'] for f in flags]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
