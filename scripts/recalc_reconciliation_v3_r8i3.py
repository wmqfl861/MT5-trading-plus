"""T48 / R8-I3 一次性重算（2026-10-27）：从既有 reconciliation-v3 四组
diff.csv **本地重算**净利拆解分类，不重跑回测、不读 bars/tick——diff.csv
是已产出的对账产物，本次重算不触锁箱段数据、不需要 --ops-ack 豁免。

新拆解规则（与修复后的 scripts/reconcile_ab.py 一致，由该测试锚定）：
- 越幅分量仅在 exit_reason_a=="stop" 且 exit_reason_b=="stop" 且入场
  同 bar 的可比行上使用（保留原分量值）；A=stop/B-非stop（原因分叉）、
  跨 bar 配对、A=stop_gap 的行越幅分量一律取 0；
- 同 bar stop↔stop 行保留原分量的前提：这些行 exit_time_ok 全为 True
  （机制核对过正确一侧首笔穿价、成交价=触发侧价±滑点）→ 回推实际越幅
  ≥ 0（浮点噪声以上），"负越幅拒绝"不触发。脚本对每个 run 显式断言
  这一前提，任何一行不满足即中止（届时才需要带 --ops-ack 重跑）；
- new_rest = net_diff − net_spread_comp − new_overshoot；
  new_net_ok = |new_rest| ≤ net_tol（容差不变，不放宽）。

净利违规分类（沿用 P17 口径并新增原因分叉类）：
- reason_divergence（新增）：A=stop、B 非 stop 出场——出场路径分叉，
  不做越幅抵消；
- reason_mismatch_other：其余原因不一致（如 A=signal/B=TP）；
- cross_bar_skew：原因一致但配对跨 bar（entry_note=before_ready）；
- tp_gap / tp_cross：跳空 TP 保守口径 / TP 有利穿价距离。

产物：data/reports/reconciliation-v3/{SYMBOL}_{TF}_<新时间戳>_r8i3-<源hash>/
（diff.csv / net_violations.csv / summary.json；源目录不动）。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "reconcile_ab.py"
SRC = ROOT / "data" / "reports" / "reconciliation-v3"

spec = importlib.util.spec_from_file_location("rec_mod", SCRIPT)
rec = importlib.util.module_from_spec(spec)
sys.modules["rec_mod"] = rec
spec.loader.exec_module(rec)

RUNS = [
    "XAUUSD_H1_20261026T105800Z_15b701",   # 运行 1 XAU H1 immediate
    "XAUUSD_H1_20261026T105752Z_e7d328",   # 运行 2 XAU H1 asc
    "USTEC_M5_20261026T105826Z_2fe72d",    # 运行 3 UST M5 immediate
    "USTEC_M5_20261026T105954Z_1835cc",    # 运行 4 UST M5 asc
]


def classify(row, ra_m: str, rb_m: str) -> str:
    if row["exit_reason_a"] == "stop" and rb_m != "stop":
        return "reason_divergence"
    if ra_m != rb_m:
        return "reason_mismatch_other"
    if row["entry_note"] == "before_ready":
        return "cross_bar_skew"
    if not bool(row["same_entry_bar"]):
        return "cross_bar_skew"
    if row["exit_reason_a"] == "take_profit_gap":
        return "tp_gap"
    if row["exit_reason_a"] in ("take_profit", "stop"):
        return "tp_cross" if row["exit_reason_a"] == "take_profit" \
            else "stop_stop_same_bar"
    return "unclassified"


def recalc_run(src_dir: Path, stamp: str) -> dict:
    rows = pd.read_csv(src_dir / "diff.csv")
    with open(src_dir / "summary.json", encoding="utf-8") as f:
        src_summary = json.load(f)
    tf_seconds = 3600 if "H1" in src_dir.name else 300

    paired_mask = rows["match"] == "paired"
    ea = pd.to_datetime(rows.loc[paired_mask, "entry_time_a"], format="mixed")
    eb = pd.to_datetime(rows.loc[paired_mask, "entry_time_b"], format="mixed")
    same = ea.dt.floor(f"{tf_seconds}s") == eb.dt.floor(f"{tf_seconds}s")
    rows.loc[paired_mask, "same_entry_bar"] = same.values

    # 新越幅适用条件：stop↔stop 同 bar。前提断言：这些行机制核对全过
    # （exit_time_ok=True）→ 实际越幅非负，"负越幅拒绝"在本地重算中为
    # 无操作；任何一行不满足即说明需要带 --ops-ack 的全量重跑。
    keep_ov = paired_mask & (rows["exit_reason_a"] == "stop") \
        & (rows["exit_reason_b"] == "stop") \
        & rows["same_entry_bar"].fillna(False)
    unverified = keep_ov & ~rows["exit_time_ok"].astype(bool)
    if int(unverified.sum()):
        raise SystemExit(f"{src_dir.name}: {int(unverified.sum())} 行 stop↔stop "
                         "同 bar 未过出场机制核对，本地重算前提不成立，"
                         "需按运维规则带 --ops-ack 重跑")

    old_ov = rows["net_overshoot_comp"].fillna(0.0)
    rows["overshoot_applied"] = keep_ov
    rows.loc[paired_mask, "net_overshoot_comp"] = \
        old_ov.where(keep_ov, 0.0)[paired_mask].values
    rows.loc[paired_mask, "net_rest"] = (
        rows.loc[paired_mask, "net_diff"]
        - rows.loc[paired_mask, "net_spread_comp"]
        - rows.loc[paired_mask, "net_overshoot_comp"])
    old_net_ok = rows.loc[paired_mask, "net_ok"].astype(bool).copy()
    rows.loc[paired_mask, "net_ok"] = (
        rows.loc[paired_mask, "net_rest"].abs()
        <= rows.loc[paired_mask, "net_tol"]).values
    rows.loc[paired_mask, "row_ok"] = (
        rows.loc[paired_mask, "entry_time_ok"].astype(bool)
        & rows.loc[paired_mask, "exit_time_ok"].astype(bool)
        & rows.loc[paired_mask, "reason_ok"].astype(bool)
        & rows.loc[paired_mask, "net_ok"].astype(bool)).values

    # 分类（配对行）
    eq = rec._REASON_EQUIV
    ra_m = rows.loc[paired_mask, "exit_reason_a"].astype(str) \
        .map(lambda s: eq.get(s, s))
    rb_m = rows.loc[paired_mask, "exit_reason_b"].astype(str) \
        .map(lambda s: eq.get(s, s))
    rows.loc[paired_mask, "net_class"] = [
        classify(r, a, b) for (_, r), a, b
        in zip(rows[paired_mask].iterrows(), ra_m, rb_m)]

    summary = rec.summarize_net_diffs(rows)
    summary["engine_a_version"] = src_summary["engine_a_version"]
    summary["engine_b_version"] = src_summary["engine_b_version"]
    summary["tolerances"] = src_summary["tolerances"]
    summary["ok"] = bool(summary["violations"]["total"] == 0)
    summary["meta"] = dict(src_summary["meta"])
    summary["meta"].update({
        "recalc": True,
        "recalc_task": "T48/R8-I3",
        "recalc_time": stamp,
        "source_run": src_dir.name,
        "recalc_rule": ("overshoot comp only for A=stop & B=stop & same entry "
                        "bar (negative overshoot rejected); all other rows "
                        "overshoot=0, rest=net_diff-spread; tolerance "
                        "unchanged; recomputed locally from source diff.csv "
                        "without re-running backtests or reading bars/ticks"),
    })

    # 新目录：新时间戳 + r8i3 + 源 run hash
    sym_tf, src_hash = src_dir.name.rsplit("_", 1)
    out_dir = SRC / f"{sym_tf}_{stamp}_r8i3-{src_hash}"
    out_dir.mkdir(parents=False, exist_ok=False)
    cols = [c for c in rows.columns]
    rows[cols].to_csv(out_dir / "diff.csv", index=False)

    bad = rows[paired_mask & ~rows["net_ok"].astype(bool)]
    bad_cols = ["side", "entry_time_a", "entry_time_b", "exit_time_a",
                "exit_time_b", "exit_reason_a", "exit_reason_b",
                "same_entry_bar", "net_a", "net_b", "net_diff",
                "net_spread_comp", "net_overshoot_comp", "net_rest",
                "net_tol", "net_class"]
    bad_out = bad[bad_cols].copy()
    bad_out.insert(0, "run", src_dir.name)
    bad_out.to_csv(out_dir / "net_violations.csv", index=False)

    with open(out_dir / "summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)

    paired = rows[paired_mask]
    n_div = int((paired["net_class"] == "reason_divergence").sum())
    div = paired[paired["net_class"] == "reason_divergence"]
    stats = {
        "run": src_dir.name,
        "out": out_dir.name,
        "paired": int(paired_mask.sum()),
        "net_viol_old": int((~old_net_ok).sum()),
        "net_viol_new": int((~paired["net_ok"].astype(bool)).sum()),
        "reason_divergence_rows": n_div,
        "reason_div_now_violating": int((~div["net_ok"].astype(bool)).sum()),
        "stop_stop_same_bar": int(summary["n_stop_stop_same_bar"]),
        "stop_stop_same_bar_viol": int((~paired[paired["net_class"]
                                    == "stop_stop_same_bar"]["net_ok"]
                                    .astype(bool)).sum()),
        "class_counts_new": bad["net_class"].value_counts().to_dict(),
        "b_favorable_new_bad": int((bad["net_b"] > bad["net_a"]).sum()),
        "within_tol": int(paired["net_ok"].astype(bool).sum()),
    }
    return stats


def main() -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    all_stats = []
    for name in RUNS:
        stats = recalc_run(SRC / name, stamp)
        all_stats.append(stats)
        print(json.dumps(stats, ensure_ascii=False))
    tot_old = sum(s["net_viol_old"] for s in all_stats)
    tot_new = sum(s["net_viol_new"] for s in all_stats)
    tot_div = sum(s["reason_divergence_rows"] for s in all_stats)
    print(f"合计：净利违规 {tot_old} → {tot_new}（原因分叉行 {tot_div}，"
          f"其中 {sum(s['reason_div_now_violating'] for s in all_stats)} 条浮出）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
