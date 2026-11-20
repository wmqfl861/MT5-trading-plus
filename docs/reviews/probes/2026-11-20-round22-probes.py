"""第二十二轮审核复现脚本（2026-11-20，审核对象 main 5a4b3d3）。

用法（仓库根）：python -I docs/reviews/probes/2026-11-20-round22-probes.py

只读：不 import MetaTrader5、不连终端。复用 tests/test_demo_formal_window.py
的合成正式窗口（写临时目录）与 tests/test_repo_hygiene.py 的扫描函数。

- P22-1 [High] 运行器在断线时按 T201 设计 fail-closed 并写 BROKER_UNAVAILABLE
  （"断线（NotConnectedError：…）——本轮不开新仓"）。正式检查把
  BROKER_UNAVAILABLE 归为异常类：窗口内出现一条就判失败。本机每天都有闪断，
  11-18～11-19 一天就有 22 条，照现状正式窗口第 1 天就会失败。
- P22-2 [High] 非交易日（周末）的长断线：T141 的 rc=78 退出告警按异常处理、
  1800 秒/日降级上限按全部自然日计。11-15（周日）两段 1523 秒、1554 秒的断线
  若落在窗口内，正式检查会失败——与执行链在交易时段的表现无关。
- P22-3 [High] 8fdbe3d 入库的三个根目录脚本含本机绝对路径，仓库卫生测试失败，
  main 的 CI hygiene 闸门变红。
- P22-4 [High] live_supervisor.py 用 CREATE_NO_WINDOW 拉起进程，Windows 会给
  新进程配一个 conhost.exe 子进程；verified_tree 要求整棵树的每个后代都通过
  _identity（解释器 + 参数逐项相符），conhost 过不了，报
  "process script/arguments mismatch"——与 11-17 演练 A 的报错原文相同。
  也就是说，11-17 记录的"--risk-config 与 --config 参数名不匹配"是误诊。
"""
from __future__ import annotations

import importlib.util
import re
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
RESULTS: list[tuple[str, str, str]] = []


def report(pid: str, status: str, detail: str) -> None:
    RESULTS.append((pid, status, detail))
    print(f"[{pid}] {status}: {detail}", flush=True)


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def fixture():
    return load("r22_formal_fixture", ROOT / "tests" / "test_demo_formal_window.py")


def p22_1() -> None:
    fx = fixture()
    root = Path(tempfile.mkdtemp(prefix="r22-p1-"))
    f = fx.build_evidence(root)
    magic = fx.MAGICS[0]
    t = f.start + timedelta(days=1, hours=9)
    # 与 src/mt5live/executor.py 断线分支写的告警同形
    msg = (f"magic {magic} 断线（NotConnectedError：terminal online, trade server not connected）"
           "——本轮不开新仓、订单未发出，下一 bar 重新决策；网关按 5/10/30/60 秒退避重连，"
           "连续 15 分钟连不上由风控/对账进程以专用退出码交给 supervisor（T141）")
    fx.add_alerts(f, [(t, "BROKER_UNAVAILABLE", msg)])
    res = fx.formal(root)
    out = res.stdout + res.stderr
    failed = res.returncode == 1 and "窗口内异常" in out and "BROKER_UNAVAILABLE" in out
    report("P22-1", "REPRODUCED" if failed else "NOT-REPRODUCED",
           f"窗口第 2 天一条运行器断线告警 BROKER_UNAVAILABLE → 返回 {res.returncode}，"
           f"判'窗口内异常'={failed}（基线证据本身返回 3）")


def append_restart_at(fx, f, when, component):
    """同 tests 的 append_restart，但重启时刻可指定（原函数固定在窗口第 1 天）。"""
    import copy
    import json
    journal = f.live / "ops-restarts.jsonl"
    rows = [json.loads(x) for x in journal.read_text().splitlines()] if journal.exists() else []
    n = len(rows) + 1
    before = fx.get(f.live / "instances.json")
    after = copy.deepcopy(before)
    e = next(e for e in after["instances"]
             if (str(e["magic"]) if e["kind"] == "runner" else e["kind"]) == component)
    old = e["pid"]
    e.update(pid=old + 20, started_utc=fx.iso(when + timedelta(seconds=3)), deployment_sha=fx.SHA)
    e.pop("gateway_config", None)
    e.pop("account_hint", None)
    for label, doc in (("before", before), ("after", after)):
        fx.put(f.root / f"retained/restart-{n}-{label}.json", doc)
    rows.append(dict(utc=fx.iso(when), component=component, reason_code="process_missing",
                     old_pid=old, new_pid=e["pid"], deployment_sha=fx.SHA))
    fx.lines(journal, rows)
    fx.put(f.live / "instances.json", after)
    f.descriptor["restart_bindings"].append(dict(line_number=n, **{
        label + "_instances": fx.ref(f.root, f.root / f"retained/restart-{n}-{label}.json")
        for label in ("before", "after")}))
    fx.save_descriptor(f)


EXIT_78 = ("risk 进程连续断线 15 分钟（≥ 15 分钟上限，最后错误：NotConnectedError）——"
           "以专用退出码 78 退出交给 supervisor（T141）")


def p22_2() -> None:
    fx = fixture()
    # ① 周六的 T141 rc=78 退出告警，2 分钟后 supervisor 按设计重启了 risk
    root = Path(tempfile.mkdtemp(prefix="r22-p2a-"))
    f = fx.build_evidence(root)
    sat = f.start + timedelta(days=(5 - f.start.weekday()) % 7 or 7, hours=5)
    assert sat.weekday() == 5 and sat < f.now
    append_restart_at(fx, f, sat + timedelta(minutes=2), "risk")
    fx.add_alerts(f, [(sat, "DISCONNECTED", EXIT_78)])
    res_a = fx.formal(root)
    out_a = res_a.stdout + res_a.stderr
    exit_fails = res_a.returncode == 1 and "窗口内异常" in out_a
    # 对照（T210 之后也应判异常）：周六 rc=78 但没有 supervisor 重启
    root = Path(tempfile.mkdtemp(prefix="r22-p2c-"))
    f = fx.build_evidence(root)
    sat_c = f.start + timedelta(days=(5 - f.start.weekday()) % 7 or 7, hours=5)
    fx.add_alerts(f, [(sat_c, "DISCONNECTED", EXIT_78)])
    res_c = fx.formal(root)
    report("P22-2c", "INFO",
           f"对照：周六 rc=78、无 supervisor 重启 → 返回 {res_c.returncode}（T210 前后都应为 1）")
    # ② 周六一段已恢复、长 1554 秒的断线（11-15 周日实况之一）
    root = Path(tempfile.mkdtemp(prefix="r22-p2b-"))
    f = fx.build_evidence(root)
    sat = f.start + timedelta(days=(5 - f.start.weekday()) % 7 or 7, hours=5)
    fx.add_alerts(f, [
        (sat, "DISCONNECTED", "risk 进程与交易服务器断线（NotConnectedError: synthetic）——降级继续"),
        (sat + timedelta(seconds=1554), "DISCONNECTED",
         "risk 进程与交易服务器恢复连接（断线 1554 秒）——恢复正常轮询（T141）"),
        (sat + timedelta(hours=2), "DISCONNECTED",
         "risk 进程与交易服务器断线（NotConnectedError: synthetic）——降级继续"),
        (sat + timedelta(hours=2, seconds=1523), "DISCONNECTED",
         "risk 进程与交易服务器恢复连接（断线 1523 秒）——恢复正常轮询（T141）"),
    ])
    res_b = fx.formal(root)
    out_b = res_b.stdout + res_b.stderr
    cap_fails = res_b.returncode == 1 and "环境降级超限" in out_b
    status = "REPRODUCED" if (exit_fails and cap_fails) else (
        "PARTIAL" if (exit_fails or cap_fails) else "NOT-REPRODUCED")
    report("P22-2", status,
           f"周六 rc=78 退出告警且 2 分钟后 supervisor 已重启 → 返回 {res_a.returncode}（判异常={exit_fails}）；"
           f"周六两段已恢复断线共 3077 秒 → 返回 {res_b.returncode}（判降级超限={cap_fails}）")


def p22_3() -> None:
    hy = load("r22_hygiene", ROOT / "tests" / "test_repo_hygiene.py")
    violations = hy.scan_files(hy.REPO_ROOT, hy.repo_tracked_files())
    files = sorted({v.split(":", 1)[0] for v in violations})
    report("P22-3", "REPRODUCED" if violations else "NOT-REPRODUCED",
           f"仓库卫生扫描违规 {len(violations)} 处，涉及 {files}")


def p22_4() -> None:
    sup = load("r22_supervisor", ROOT / "scripts" / "live_supervisor.py")
    src = (ROOT / "scripts" / "live_supervisor.py").read_text(encoding="utf-8")
    uses_no_window = bool(re.search(r"creationflags\s*=\s*subprocess\.CREATE_NO_WINDOW", src))
    t0 = datetime(2026, 11, 19, 12, 0, 0)
    iso = lambda t: t.isoformat()  # noqa: E731
    argv = ["C:/venv/Scripts/python.exe", "C:/deploy/scripts/risk_process.py", "--config", "C:/deploy/config/risk.toml"]
    entry = {"pid": 100, "started_utc": iso(t0)}
    table = {
        100: {"pid": 100, "parent_pid": 1, "argv": list(argv), "executable": "C:/venv/Scripts/python.exe",
              "started_utc": iso(t0)},
        101: {"pid": 101, "parent_pid": 100, "argv": ["C:/Python313/python.exe", *argv[1:]],
              "executable": "C:/Python313/python.exe", "started_utc": iso(t0 + timedelta(seconds=1))},
    }
    clean = sup.verified_tree(entry, table, argv)
    table[102] = {"pid": 102, "parent_pid": 101,
                  "argv": ["C:/Windows/system32/conhost.exe", "0xffffffff", "-ForceV1"],
                  "executable": "C:/Windows/system32/conhost.exe",
                  "started_utc": iso(t0 + timedelta(seconds=1))}
    try:
        sup.verified_tree(entry, table, argv)
        error = None
    except ValueError as exc:
        error = str(exc)
    hit = uses_no_window and len(clean) == 2 and error == "process script/arguments mismatch"
    report("P22-4", "REPRODUCED" if hit else "NOT-REPRODUCED",
           f"launch() 使用 CREATE_NO_WINDOW={uses_no_window}；无 conhost 时校验通过（{len(clean)} 个进程），"
           f"加一个 conhost.exe 子进程后报错={error!r}（11-17 演练 A 报错原文相同）")


def main() -> int:
    for fn in (p22_1, p22_2, p22_3, p22_4):
        try:
            fn()
        except Exception as exc:  # 复现脚本自身出错也如实报告
            report(fn.__name__.upper().replace("_", "-"), "ERROR", f"{type(exc).__name__}: {exc}")
    print("\n汇总：")
    for pid, status, _ in RESULTS:
        print(f"  {pid}: {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
