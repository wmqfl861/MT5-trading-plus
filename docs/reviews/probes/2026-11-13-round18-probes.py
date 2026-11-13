"""第十八轮审核复现脚本（2026-11-13，审核对象 main 0cc1a33）。

用法（仓库根）：python -I docs/reviews/probes/2026-11-13-round18-probes.py

只读：合成证据全部写在临时目录，复用 tests/test_demo_formal_window.py 的
合成窗口（基线应返回 3）；不连终端、不改仓库文件、不回显敏感内容。

- P18-1 [High] 正式检查读不懂运行时会写的告警类型：窗口内一对
  DISCONNECTED（T141 断线段首 + 恢复，正常降级路径；"重连"演练必然产生）
  → 检查器返回 2（alerts.unknown_kind），窗口无法评估。
- P18-2 [High] 告警历史污染：锚点之前的一行 DISCONNECTED 同样返回 2。
- P18-3 [High] 运行时告警类型与检查器分类集合的差集（静态）。
- P18-4 [High] 日志历史污染：锚点之前的 *.log.err 里一行无时间戳的
  CRITICAL（T110 期限硬停的原样 stderr 输出）→ 返回 2
  （logs.missing_event_time）。
- P18-5 [High] 周六数据任务与实盘进程共用交易终端，而正式检查按时刻
  （含周末）计异常：窗口内周六的一行期限硬停 CRITICAL → 返回 1。
- P18-6 [Info] 日报节奏：05:57 本地 + --date yesterday → D 日报告在
  D+1 21:57 UTC 生成；13:15 UTC 日检只能看到 D-2；最终检查须等第 10 天
  报告生成之后。
- P18-7 [Medium] 公开仓库脱敏：本机路径与"账号关键词 + 8–10 位数字"
  （只报文件:行与规则名，不回显内容）。
- P18-8 [Info] supervisor 每分钟一次、无退避、不处理终端楔死（静态）。
- P18-9 [High] 换日禁入演练无法归属：生产 M15 策略不可能自然触发，只能用
  演练 magic 的探针；检查器要求告警里的 magic 全在部署清单内 → 返回 2
  （alerts.magic）。修复（描述符声明演练 magic）后应返回 3。
"""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RESULTS: list[tuple[str, str, str]] = []


def report(pid: str, status: str, detail: str) -> None:
    RESULTS.append((pid, status, detail))
    print(f"[{pid}] {status}: {detail}", flush=True)


def load_fixture():
    path = ROOT / "tests" / "test_demo_formal_window.py"
    spec = importlib.util.spec_from_file_location("r18_formal_fixture", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def run_formal(fx, root: Path):
    res = fx.formal(root)
    return res.returncode, res.stdout + res.stderr


def fresh(fx, tag: str):
    root = Path(tempfile.mkdtemp(prefix=f"r18-{tag}-"))
    f = fx.build_evidence(root)
    return root, f


def append_alerts(f, rows):
    with (f.live / "alerts.log").open("a", encoding="utf-8") as out:
        for t, kind, msg in rows:
            out.write(f"{t:%Y-%m-%dT%H:%M:%SZ}\t{kind}\t{msg}\n")


def prepend_alert(f, t, kind, msg):
    p = f.live / "alerts.log"
    old = p.read_text(encoding="utf-8")
    p.write_text(f"{t:%Y-%m-%dT%H:%M:%SZ}\t{kind}\t{msg}\n" + old, encoding="utf-8")


def frozensets(src: str) -> dict[str, set[str]]:
    out = {}
    for m in re.finditer(r"^(FORMAL_[A-Z_]+_ALERTS)\s*=\s*frozenset\(\{(.*?)\}\)", src, re.S | re.M):
        out[m.group(1)] = set(re.findall(r'"([A-Z][A-Z0-9_]*)"', m.group(2)))
    return out


def runtime_alert_kinds() -> dict[str, set[str]]:
    files = sorted(ROOT.glob("src/mt5live/*.py")) + sorted(ROOT.glob("src/mt5gw/*.py")) + [
        ROOT / "scripts" / n for n in ("risk_process.py", "live_reconcile.py",
                                         "live_runner.py", "live_supervisor.py")]
    consts: dict[str, str] = {}
    texts = {}
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


def main() -> int:
    fx = load_fixture()

    # ---------------------------------------------------------------- 基线
    root, f = fresh(fx, "base")
    rc, out = run_formal(fx, root)
    baseline_ok = rc == 3
    print(f"[基线] 合成正式窗口返回 {rc}（期望 3）", flush=True)

    # ---------------------------------------------------------------- P18-1
    root, f = fresh(fx, "p1")
    t0 = f.start + timedelta(hours=5) - timedelta(seconds=40)   # 合成"重连"演练前后
    append_alerts(f, [
        (t0, "DISCONNECTED", "risk 进程与交易服务器断线（NotConnectedError: synthetic）——降级继续"),
        (t0 + timedelta(seconds=55), "DISCONNECTED", "risk 进程与交易服务器恢复连接（断线 55 秒）——恢复正常轮询（T141）"),
    ])
    rc, out = run_formal(fx, root)
    status = "REPRODUCED" if rc == 2 and "alerts.unknown_kind" in out else (
        "NOT-REPRODUCED" if rc in (1, 3) else "INFO")
    report("P18-1", status, f"窗口内一对已恢复的 DISCONNECTED → 返回 {rc}"
           f"{'（alerts.unknown_kind）' if 'alerts.unknown_kind' in out else ''}；"
           "修复后应为 3（已恢复、未退出、未超当日降级上限的断线只记录不判失败）"
           + ("" if baseline_ok else "；注意：基线未返回 3"))

    # ---------------------------------------------------------------- P18-2
    root, f = fresh(fx, "p2")
    prepend_alert(f, f.start - timedelta(days=30), "DISCONNECTED",
                  "risk 进程与交易服务器断线（历史行，早于锚点 30 天）")
    rc, out = run_formal(fx, root)
    status = "REPRODUCED" if rc == 2 and "alerts.unknown_kind" in out else (
        "NOT-REPRODUCED" if rc == 3 else "INFO")
    report("P18-2", status, f"锚点前 30 天的一行 DISCONNECTED → 返回 {rc}；"
           "修复后应为 3（锚点前的告警只校验格式，不参与窗口判定）")

    # ---------------------------------------------------------------- P18-3
    src = (ROOT / "scripts" / "demo_check.py").read_text(encoding="utf-8")
    sets = frozensets(src)
    classified = set().union(*sets.values()) if sets else set()
    kinds = runtime_alert_kinds()
    missing = sorted(k for k in kinds if k not in classified)
    status = "REPRODUCED" if missing else "NOT-REPRODUCED"
    report("P18-3", status, f"检查器分类集合 {sorted(sets)}；运行时类型 {len(kinds)} 种，"
           f"未分类 {len(missing)} 种：{missing}")

    # ---------------------------------------------------------------- P18-4
    root, f = fresh(fx, "p4")
    err = f.logs / "risk-account-20201110-033459.log.err"
    err.write_text("CRITICAL:root:MT5 deadline call=initialize duration=66.422s "
                   "limit=30.000s rc=75\n", encoding="utf-8")
    old = (f.start - timedelta(days=30)).timestamp()
    os.utime(err, (old, old))
    rc, out = run_formal(fx, root)
    status = "REPRODUCED" if rc == 2 and "logs.missing_event_time" in out else (
        "NOT-REPRODUCED" if rc == 3 else "INFO")
    report("P18-4", status, f"锚点前 30 天的 .log.err（一行无时间戳 CRITICAL）→ 返回 {rc}；"
           "修复后应为 3（锚点前修改过的日志文件不读）；窗口内非空 .log.err 应判失败（返回 1）")

    # ---------------------------------------------------------------- P18-5
    ps1 = (ROOT / "scripts" / "setup_scheduled_tasks.ps1").read_text(encoding="utf-8-sig")
    weekly = re.findall(r'New-SaturdayTask\s+"(\w+)"\s+"(\d\d:\d\d)"\s+"(\d\d:\d\d)"', ps1)
    same_cfg = "$cfgArg" in ps1 and "config.json" in ps1
    root, f = fresh(fx, "p5")
    sats = [f.start.date() + timedelta(days=i) for i in range((f.now.date() - f.start.date()).days)]
    sats = [d for d in sats if d.weekday() == 5]
    if not sats:
        report("P18-5", "INFO", "合成窗口内无周六，动态部分跳过")
    else:
        d = sats[-1]
        line = (f"· {d} 05:00:31 UTC CRITICAL MT5 deadline call=terminal_info "
                "duration=31.002s limit=30.000s rc=75\n")
        with (f.logs / "risk.log").open("a", encoding="utf-8") as fh:
            fh.write(line)
        rc, out = run_formal(fx, root)
        dyn = rc == 1 and "窗口内异常" in out
        status = "REPRODUCED" if weekly and same_cfg and dyn else "NOT-REPRODUCED"
        report("P18-5", status, f"周六任务 {[(n, u + ' UTC') for n, _, u in weekly]} 用实盘同一网关配置="
               f"{same_cfg}；窗口内周六 05:00 UTC 一行期限硬停 → 返回 {rc}"
               f"{'（窗口内异常）' if dyn else ''}；机理见 T185 §10.1（tick 下载占住终端 API）")

    # ---------------------------------------------------------------- P18-6
    m = re.search(r'New-DailyTask\s+"live_daily_report"\s+"(\d\d):(\d\d)"', ps1)
    yesterday = "--date yesterday" in ps1
    if m:
        local_h, local_m = int(m.group(1)), int(m.group(2))
        utc = (datetime(2026, 1, 2, local_h, local_m) - timedelta(hours=8))
        report("P18-6", "INFO", f"日报 {local_h:02d}:{local_m:02d} 本地 = 前一日 {utc:%H:%M} UTC，"
               f"--date yesterday={yesterday} → D 日报告在 D+1 {utc:%H:%M} UTC 生成；"
               "13:15 UTC 日检应查 D-2；第 10 天报告要到第 11 天 21:57 UTC 才有")
    else:
        report("P18-6", "INFO", "未找到 live_daily_report 计划时刻")

    # ---------------------------------------------------------------- P18-7
    files = subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True,
                           text=True, encoding="utf-8").stdout.splitlines()
    path_re = re.compile(r"C:[/\\]Users[/\\](?!<)")
    # 关键词后紧跟（可有引号/冒号/空白）8–10 位数字；"demo-next-<日期>" 这类名字不算
    acct_re = re.compile(r"(?i)(?:login|账号|账户|模拟盘|demo)[\"']?\s*[:：=]?\s*[\"']?\d{8,10}(?!\d)")
    hits = []
    allow = (ROOT / "config" / "hygiene-allowlist.txt")
    allowed = allow.read_text(encoding="utf-8") if allow.exists() else ""
    for rel in files:
        p = ROOT / rel
        if p.suffix.lower() not in (".md", ".txt", ".json", ".py", ".toml", ".ps1", ".csv", ".yml", ".yaml"):
            continue
        if rel.startswith("tests/") or rel.endswith("round18-probes.py"):
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, ln in enumerate(text.splitlines(), 1):
            for name, rx in (("local_path", path_re), ("account_number", acct_re)):
                if rx.search(ln) and f"{rel}:{name}" not in allowed:
                    hits.append(f"{rel}:{n}:{name}")
    status = "REPRODUCED" if hits else "NOT-REPRODUCED"
    report("P18-7", status, f"{len(hits)} 处：{hits[:8]}")

    # ---------------------------------------------------------------- P18-8
    sup = (ROOT / "scripts" / "live_supervisor.py").read_text(encoding="utf-8")
    runbook = (ROOT / "docs" / "ops" / "demo-runbook.md").read_text(encoding="utf-8")
    backoff = bool(re.search(r"backoff|restart_count|max_restarts|gave_up", sup))
    every_min = "RepetitionInterval (New-TimeSpan -Minutes 1)" in runbook
    report("P18-8", "INFO", f"supervisor 退避/上限逻辑={backoff}；计划任务每分钟一次={every_min}；"
           "终端楔死（11-10 initialize 66 秒）时会每分钟重启一轮、不能自愈")

    # ---------------------------------------------------------------- P18-9
    root, f = fresh(fx, "p9")
    drill = 902603 if 902603 not in fx.MAGICS else max(fx.MAGICS) + 7
    t = f.start + timedelta(hours=4, minutes=30)
    append_alerts(f, [(t, "RISK_REJECT",
                       f"magic {drill} XAUUSD UTC 21:56 换日窗禁止开仓 forbidden_session（演练探针）")])
    supports = "drill_magics" in src
    if supports:
        f.descriptor["drill_magics"] = [drill]
        fx.save_descriptor(f)
    rc, out = run_formal(fx, root)
    if not supports:
        status = "REPRODUCED" if rc == 2 and "alerts.magic" in out else "INFO"
    else:
        status = "NOT-REPRODUCED" if rc == 3 else "INFO"
    report("P18-9", status, f"演练 magic {drill} 的 forbidden_session 拒单（描述符声明演练 magic="
           f"{supports}）→ 返回 {rc}{'（alerts.magic）' if 'alerts.magic' in out else ''}；"
           "生产 M15 策略自然触发不可能（11-05 phase-f-log：禁入窗 21:55 起、22:00 关市，"
           "窗内无 M15 收线），只能用探针，修复后应能归属并返回 3")

    print("\n汇总：")
    for pid, status, _ in RESULTS:
        print(f"  {pid}: {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
