"""T191 守卫测试（第十八轮 §3.1.7）：运行时会写的全部告警类型必须都已定类。

提取方式与 docs/reviews/probes/2026-11-13-round18-probes.py 的 P18-3 相同
（静态解析 src/mt5live、src/mt5gw 与四个 scripts 进程脚本里的 ALERT_* 常量
与 append_alert 调用）。以后新增告警类型，必须同时在 demo_check.py 的
FORMAL_*_ALERTS 分类集合里定类，否则本测试失败。"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def checker_sets() -> dict[str, set[str]]:
    src = (ROOT / "scripts" / "demo_check.py").read_text(encoding="utf-8")
    out: dict[str, set[str]] = {}
    for m in re.finditer(r"^(FORMAL_[A-Z_]+_ALERTS)\s*=\s*frozenset\(\{(.*?)\}\)", src, re.S | re.M):
        out[m.group(1)] = set(re.findall(r'"([A-Z][A-Z0-9_]*)"', m.group(2)))
    assert {"FORMAL_ANOMALY_ALERTS", "FORMAL_DRILL_ALERTS", "FORMAL_RECORDED_ALERTS",
            "FORMAL_INVALID_ALERTS"} <= set(out), "demo_check.py 缺少 FORMAL_*_ALERTS 分类集合"
    return out


def runtime_alert_kinds() -> dict[str, set[str]]:
    files = sorted(ROOT.glob("src/mt5live/*.py")) + sorted(ROOT.glob("src/mt5gw/*.py")) + [
        ROOT / "scripts" / n for n in ("risk_process.py", "live_reconcile.py",
                                       "live_runner.py", "live_supervisor.py")]
    consts: dict[str, str] = {}
    texts: dict[Path, str] = {}
    for p in files:
        if not p.exists():
            continue
        t = p.read_text(encoding="utf-8")
        texts[p] = t
        for m in re.finditer(r'^\s*(ALERT_[A-Z0-9_]+)\s*=\s*"([A-Z][A-Z0-9_]*)"', t, re.M):
            consts[m.group(1)] = m.group(2)
    kinds: dict[str, set[str]] = {}
    for p, t in texts.items():
        for m in re.finditer(r'append_alert\(\s*[^,]+,\s*(?:"([A-Z][A-Z0-9_]*)"|([A-Za-z_][A-Za-z0-9_.]*))', t):
            lit, name = m.group(1), m.group(2)
            kind = lit or consts.get((name or "").split(".")[-1])
            if kind:
                kinds.setdefault(kind, set()).add(p.name)
    # executor 的 block_alert 只取 ALERT_* 常量（已逐一核对均为异常类），不另列
    return kinds


def test_every_runtime_alert_kind_is_classified():
    sets = checker_sets()
    classified = set().union(*sets.values())
    kinds = runtime_alert_kinds()
    missing = sorted(k for k in kinds if k not in classified)
    assert kinds, "运行时告警类型提取为空——提取器失效，守卫失去意义"
    assert not missing, (
        f"运行时告警类型未在 demo_check.py 分类集合里定类：{missing}"
        "——新增告警类型必须同时定类（docs/handoff/2026-11-13-round18-plan.md §3.1）")


def test_classification_sets_are_pairwise_disjoint():
    sets = checker_sets()
    names = sorted(sets)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            assert not sets[a] & sets[b], f"{a} 与 {b} 重复定类：{sorted(sets[a] & sets[b])}"
