"""scripts/demo_check.py 双档口径测试（T49/R8-I5：日常检查 vs 阶段 F 最终验收）。

R8-I5 反例（P16）回归：临时目录里只放**一个空仓 confirmed 意图**和**一份
写着"当日无点差样本"的日报**——旧版 ``--summary`` 输出"全部通过"且退出码
0。修后：日常档（--day/--summary）保持宽松显示（返回 0 只要无异常证据），
但不再宣称"全部通过"；最终验收档 ``--accept`` 逐条强制证据，任何
"无样本/未覆盖"项 = 不通过（未验收），退出码非 0。

--accept 判据（蓝图 §5 F 行 + §7，缺一即未验收）：
1. 连续交易日覆盖 ≥10（工作日无缺日；假日历不可得按周一至五近似）；
2. 各进程存活证据（覆盖跨度内逐日：风控/对账有日志行，每个 magic 的
   运行器有当日日志行或有风控"心跳=ok"行佐证）；
3. SL 与 TP 样本均存在（全期成交/运行器日志保护性出场）；
4. 双全平演练真实完成（两个不同 uuid 指令 + 两轮完成 ack——归档指令
   （归档=全员 ack 核对通过）或归档 ack 文件）；
5. 实际点差样本存在（覆盖工作日日报非"当日无点差样本"）且无
   "**超标**"小时（超限小时由日报逐小时列出）；
另加逐日严格复核：--accept 下 daily 检查项 ok=None（无样本/无法判定）
同样按不通过处理。

绝不连真实 MT5、绝不下真实单：全部证据在临时目录构造（只读检查器）。
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SESSIONS = REPO_ROOT / "config" / "sessions.toml"

MAGIC = 902601
SYMBOL = "XAUUSD"
# 2026-11-02 是周一：11-02..11-06 + 11-09..11-13 = 连续 10 个交易日
TRADING_DAYS = [date(2026, 11, d) for d in (2, 3, 4, 5, 6, 9, 10, 11, 12, 13)]
WEEKEND = [date(2026, 11, 7), date(2026, 11, 8)]
SPAN_DAYS = [TRADING_DAYS[0] + timedelta(days=i)
             for i in range((TRADING_DAYS[-1] - TRADING_DAYS[0]).days + 1)]

DEC_SL = f"{SYMBOL}|M15|2026-11-02T10:45:00"
DEC_TP = f"{SYMBOL}|M15|2026-11-03T11:00:00"


def load_demo_check():
    spec = importlib.util.spec_from_file_location(
        "demo_check_script", REPO_ROOT / "scripts" / "demo_check.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod   # dataclass 需要模块在 sys.modules 里
    spec.loader.exec_module(mod)
    return mod


def run_cli(mod, root: Path, *flags: str):
    """跑 demo_check CLI（cwd 无关：全部路径显式）。返回 (exit_code, stdout)。"""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = mod.main([
            "--data-dir", str(root / "data"),
            "--log-dir", str(root / "logs"),
            "--reports-dir", str(root / "reports"),
            "--sessions-config", str(SESSIONS),
            *flags])
    return code, out.getvalue()


# ---------------------------------------------------------------- 证据构造

def _deal(ticket: int, position: int, day: date, entry: str, *, reason: int = 3,
          comment: str = "", ts: str = "10:45:05") -> dict:
    return {
        "ticket": ticket, "order": 0, "position_id": position, "symbol": SYMBOL,
        "side": "buy" if entry == "in" else "sell", "entry": entry,
        "volume": 0.01, "price": 4000.0, "profit": 0.0, "commission": 0.0,
        "swap": 0.0, "fee": 0.0, "time": f"{day.isoformat()}T{ts}+00:00",
        "comment": comment, "magic": MAGIC, "reason": reason,
    }


def write_flatten_archive(live: Path, cmd_id: str, day: date,
                          reason: str = "daily_loss_cap") -> None:
    arch = live / "commands" / "archive"
    arch.mkdir(parents=True, exist_ok=True)
    (arch / f"flatten-{cmd_id[:8]}-{day.isoformat()}T120000-manual.json").write_text(
        json.dumps({"command": "flatten_all", "id": cmd_id, "reason": reason,
                    "ts": f"{day.isoformat()}T12:00:00+00:00"}), encoding="utf-8")


def build_full_evidence(root: Path) -> dict:
    """构造阶段 F 全证据树（10 连续交易日、双进程日志、SL+TP、双全平、
    每日点差样本）。返回各关键路径，便于测试逐条拆证据。"""
    live = root / "data" / "live"
    logs = root / "logs"
    reports = root / "reports" / "live"
    for p in (live / "paper", live / "commands" / "archive",
              logs, reports, root / "data", root / "logs"):
        p.mkdir(parents=True, exist_ok=True)

    # 意图：最新日空仓 confirmed（与实际一致）
    (live / f"expected-{MAGIC}.json").write_text(json.dumps({
        "symbol": SYMBOL, "magic": MAGIC, "side": "buy", "volume": 0.0,
        "status": "confirmed", "cause": DEC_TP,
        "ts": f"{TRADING_DAYS[-1].isoformat()}T20:00:00+00:00"}), encoding="utf-8")
    # 执行器状态：无未决外部出场
    (live / f"state-{MAGIC}.json").write_text(json.dumps({
        "magic": MAGIC, "pending_external_exits": {}}), encoding="utf-8")
    # 影子券商账本：一笔 SL 出场（11-02）+ 一笔 TP 出场（11-03），全部平掉
    (live / "paper" / f"state-{MAGIC}.json").write_text(json.dumps({
        "magic": MAGIC, "balance": 10000.0, "equity": 10000.0,
        "positions": [],
        "deals": [
            _deal(1, 101, TRADING_DAYS[0], "in", comment=DEC_SL),
            _deal(2, 101, TRADING_DAYS[0], "out", reason=4),
            _deal(3, 102, TRADING_DAYS[1], "in", comment=DEC_TP, ts="11:00:05"),
            _deal(4, 102, TRADING_DAYS[1], "out", reason=5, ts="15:30:05"),
        ]}), encoding="utf-8")
    # 下单台账：两笔开仓可归因
    with open(live / f"orders-{MAGIC}.jsonl", "w", encoding="utf-8") as f:
        for dec, day, ts in ((DEC_SL, TRADING_DAYS[0], "10:45:06"),
                             (DEC_TP, TRADING_DAYS[1], "11:00:06")):
            f.write(json.dumps({
                "ts": f"{day.isoformat()}T{ts}+00:00", "magic": MAGIC,
                "symbol": SYMBOL, "kind": "open", "side": "buy",
                "lots": 0.01, "decision_id": dec}) + "\n")
    # 告警日志：空（无异常证据）
    (live / "alerts.log").write_text("", encoding="utf-8")

    # 运行器日志：启动行 + 每个跨度日至少一行（周末为"休市跳过"行）
    runner_lines = [
        f"mt5live 启动：{SYMBOL} M15 magic={MAGIC} "
        "strategy=demo 手数=固定 0.01"]
    for d in SPAN_DAYS:
        if d in WEEKEND:
            runner_lines.append(
                f"[{d.isoformat()}T12:00:05.000000] 休市跳过 1：{SYMBOL} 休市"
                "（最新报价陈旧超过阈值），跳过本网格点")
            continue
        # 10:45 决策行（11-02 这条即 SL 入场决策 DEC_SL）
        runner_lines.append(
            f"[{d.isoformat()}T10:45:05.000000] 决策 {SYMBOL}|M15|"
            f"{d.isoformat()}T10:45:00：signal=1 target=1 volume=None "
            "bid=4000.1 ask=4000.3")
        runner_lines.append(
            f"[{d.isoformat()}T16:00:05.000000] 决策 {SYMBOL}|M15|"
            f"{d.isoformat()}T16:00:00：signal=0 target=0 volume=None "
            "bid=4000.1 ask=4000.3")
    # 11-03 的 TP 入场决策在 11:00 网格（对应成交 comment=DEC_TP）
    runner_lines.insert(
        3, f"[{TRADING_DAYS[1].isoformat()}T11:00:05.000000] 决策 {DEC_TP}："
           "signal=1 target=1 volume=None bid=4000.1 ask=4000.3")
    (logs / f"runner-{MAGIC}-20261102-080000.log").write_text(
        "\n".join(runner_lines) + "\n", encoding="utf-8")

    # 风控/对账日志：跨度内逐日有行（心跳=ok 佐证运行器活着）
    risk_lines, recon_lines = [], []
    for d in SPAN_DAYS:
        for hm in ("10:00:10", "16:00:10", "21:00:10"):
            risk_lines.append(
                f"· {d.isoformat()} {hm} UTC [paper] 心跳=ok halt=否 "
                "flatten=— 日亏触发=否")
        recon_lines.append(f"· {d.isoformat()} 10:00:41 UTC 对账一致（0 个持仓）")
    (logs / "risk-account-20261102-080000.log").write_text(
        "\n".join(risk_lines) + "\n", encoding="utf-8")
    (logs / "reconcile-all-20261102-080000.log").write_text(
        "\n".join(recon_lines) + "\n", encoding="utf-8")

    # 日报：每个交易日有点差比对样本、无超标行
    for d in TRADING_DAYS:
        (reports / f"daily-{d.isoformat()}.md").write_text(
            f"# 演练日报 {d.isoformat()}\n\n点差比对（按小时）：12 行全部在"
            "成本模型区间内（无超标行）。\n", encoding="utf-8")

    # 双全平：两个不同 uuid 指令（归档=全员完成 ack 核对通过）
    write_flatten_archive(live, "a" * 31 + "1", TRADING_DAYS[3])
    write_flatten_archive(live, "b" * 31 + "2", TRADING_DAYS[7])

    return {"live": live, "logs": logs, "reports": reports,
            "runner_log": logs / f"runner-{MAGIC}-20261102-080000.log",
            "risk_log": logs / "risk-account-20261102-080000.log",
            "recon_log": logs / "reconcile-all-20261102-080000.log",
            "paper_state": live / "paper" / f"state-{MAGIC}.json"}


def drop_day_from_log(path: Path, day: date) -> None:
    """删掉日志里某日的全部行（构造"该日无进程存活证据"）。"""
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines()
             if day.isoformat() not in ln]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- --accept 通过

def test_accept_passes_with_full_evidence(tmp_path):
    mod = load_demo_check()
    build_full_evidence(tmp_path)
    code, out = run_cli(mod, tmp_path, "--accept")
    assert code == 0, out
    assert "验收通过" in out
    for name in ("连续交易日覆盖", "进程存活", "SL 与 TP 样本",
                 "双全平演练", "点差样本"):
        assert name in out, out


# ---------------------------------------------------------------- P16 反例（R8-I5）

def build_p16_evidence(root: Path) -> None:
    """P16：一个空仓 confirmed 意图 + 一份"当日无点差样本"日报，别无其他。"""
    live = root / "data" / "live"
    reports = root / "reports" / "live"
    (live / "commands").mkdir(parents=True, exist_ok=True)
    reports.mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(exist_ok=True)
    (root / "logs").mkdir(exist_ok=True)
    (reports / "daily-2026-10-26.md").write_text("当日无点差样本\n", encoding="utf-8")
    (live / "expected-1.json").write_text(json.dumps({
        "symbol": "XAUUSD", "magic": 1, "side": "buy", "volume": 0,
        "status": "confirmed", "cause": "initial",
        "ts": "2026-10-26T00:00:00+00:00"}), encoding="utf-8")


def test_accept_rejects_p16_counterexample(tmp_path):
    """仅一个空仓意图 + "当日无点差样本"日报 → --accept 必须不通过（未验收）。"""
    mod = load_demo_check()
    build_p16_evidence(tmp_path)
    code, out = run_cli(mod, tmp_path, "--accept")
    assert code != 0
    assert code == 1, out                       # 未验收 = 1（区别于 2=数据错）
    assert "未通过" in out or "未验收" in out
    assert "连续交易日覆盖" in out               # 覆盖判据必须出现且不通过
    assert "验收通过" not in out


def test_summary_lenient_no_overclaim_on_p16(tmp_path):
    """日常档宽松：P16 反例 --summary 返回 0（无异常证据），但不得宣称
    "全部通过"——须标注日常口径并指向 --accept。"""
    mod = load_demo_check()
    build_p16_evidence(tmp_path)
    code, out = run_cli(mod, tmp_path, "--summary")
    assert code == 0, out
    assert "全部通过" not in out
    assert "--accept" in out


# ---------------------------------------------------------------- 逐条判据失败

def test_accept_fails_short_coverage(tmp_path):
    """判据 1：删掉一个交易日（周三 11-04）→ 覆盖不连续 → 未验收。"""
    mod = load_demo_check()
    paths = build_full_evidence(tmp_path)
    gone = date(2026, 11, 4)
    (paths["reports"] / f"daily-{gone.isoformat()}.md").unlink()
    drop_day_from_log(paths["runner_log"], gone)
    drop_day_from_log(paths["risk_log"], gone)
    drop_day_from_log(paths["recon_log"], gone)
    code, out = run_cli(mod, tmp_path, "--accept")
    assert code == 1
    assert "覆盖" in out and "不通过" in out


def test_accept_fails_only_nine_trading_days(tmp_path):
    """判据 1：只有 9 个交易日 → 未验收。"""
    mod = load_demo_check()
    paths = build_full_evidence(tmp_path)
    last = TRADING_DAYS[-1]
    (paths["reports"] / f"daily-{last.isoformat()}.md").unlink()
    drop_day_from_log(paths["runner_log"], last)
    drop_day_from_log(paths["risk_log"], last)
    drop_day_from_log(paths["recon_log"], last)
    code, out = run_cli(mod, tmp_path, "--accept")
    assert code == 1
    assert "10" in out and "覆盖" in out


def test_accept_fails_missing_risk_evidence(tmp_path):
    """判据 2：某日无风控日志行 → 进程存活证据缺 → 未验收。"""
    mod = load_demo_check()
    paths = build_full_evidence(tmp_path)
    drop_day_from_log(paths["risk_log"], date(2026, 11, 5))
    code, out = run_cli(mod, tmp_path, "--accept")
    assert code == 1
    assert "风控" in out


def test_accept_fails_missing_reconcile_evidence(tmp_path):
    """判据 2：某日无对账日志行 → 未验收。"""
    mod = load_demo_check()
    paths = build_full_evidence(tmp_path)
    drop_day_from_log(paths["recon_log"], date(2026, 11, 10))
    code, out = run_cli(mod, tmp_path, "--accept")
    assert code == 1
    assert "对账进程" in out


def test_accept_fails_dead_runner_day(tmp_path):
    """判据 2：某日运行器无自身日志行、也无风控"心跳=ok"佐证 → 未验收。"""
    mod = load_demo_check()
    paths = build_full_evidence(tmp_path)
    day = date(2026, 11, 11)
    # 该日运行器行全删；风控该日只留一行"心跳超时"（风控活着、运行器
    # 死了——不能靠别日证据或当日的 ok 心跳蒙混）
    drop_day_from_log(paths["runner_log"], day)
    drop_day_from_log(paths["risk_log"], day)
    with open(paths["risk_log"], "a", encoding="utf-8") as f:
        f.write(f"· {day.isoformat()} 16:00:10 UTC [paper] "
                f"心跳=heartbeat_timeout({MAGIC}) halt=是 flatten=— "
                "日亏触发=否\n")
    code, out = run_cli(mod, tmp_path, "--accept")
    assert code == 1
    assert "运行器" in out


def test_accept_fails_no_tp_sample(tmp_path):
    """判据 3：只有 SL 样本、无 TP 样本 → 未验收。"""
    mod = load_demo_check()
    paths = build_full_evidence(tmp_path)
    doc = json.loads(paths["paper_state"].read_text(encoding="utf-8"))
    doc["deals"] = [d for d in doc["deals"] if d.get("reason") != 5]
    paths["paper_state"].write_text(json.dumps(doc), encoding="utf-8")
    code, out = run_cli(mod, tmp_path, "--accept")
    assert code == 1
    assert "TP" in out and "不通过" in out


def test_accept_fails_no_sl_sample(tmp_path):
    """判据 3：只有 TP 样本、无 SL 样本 → 未验收。"""
    mod = load_demo_check()
    paths = build_full_evidence(tmp_path)
    doc = json.loads(paths["paper_state"].read_text(encoding="utf-8"))
    doc["deals"] = [d for d in doc["deals"] if d.get("reason") != 4]
    paths["paper_state"].write_text(json.dumps(doc), encoding="utf-8")
    code, out = run_cli(mod, tmp_path, "--accept")
    assert code == 1
    assert "SL" in out


def test_accept_fails_single_flatten_round(tmp_path):
    """判据 4：只有一个 uuid 全平指令（一轮）→ 未验收。"""
    mod = load_demo_check()
    paths = build_full_evidence(tmp_path)
    arch = paths["live"] / "commands" / "archive"
    kept = sorted(arch.glob("flatten-*.json"))[:1]
    for f in arch.glob("flatten-*.json"):
        if f not in kept:
            f.unlink()
    code, out = run_cli(mod, tmp_path, "--accept")
    assert code == 1
    assert "全平" in out and "不通过" in out


def test_accept_fails_same_uuid_twice(tmp_path):
    """判据 4：两轮归档但同一 uuid（旧 ack 重放形态）→ 未验收。"""
    mod = load_demo_check()
    paths = build_full_evidence(tmp_path)
    arch = paths["live"] / "commands" / "archive"
    files = sorted(arch.glob("flatten-*.json"))
    doc = json.loads(files[1].read_text(encoding="utf-8"))
    doc["id"] = json.loads(files[0].read_text(encoding="utf-8"))["id"]
    files[1].write_text(json.dumps(doc), encoding="utf-8")
    code, out = run_cli(mod, tmp_path, "--accept")
    assert code == 1
    assert "全平" in out


def test_accept_fails_spread_sample_missing_one_day(tmp_path):
    """判据 5：某交易日日报"当日无点差样本" → 未验收（无样本≠通过）。"""
    mod = load_demo_check()
    paths = build_full_evidence(tmp_path)
    (paths["reports"] / "daily-2026-11-12.md").write_text(
        "# 演练日报 2026-11-12\n\n当日无点差样本\n", encoding="utf-8")
    code, out = run_cli(mod, tmp_path, "--accept")
    assert code == 1
    assert "点差样本" in out and "不通过" in out


def test_accept_fails_spread_breach_hours(tmp_path):
    """判据 5：日报含"**超标**"小时 → 未验收（实际点差超出成本模型区间）。"""
    mod = load_demo_check()
    paths = build_full_evidence(tmp_path)
    (paths["reports"] / "daily-2026-11-06.md").write_text(
        "# 演练日报 2026-11-06\n\n| 小时 | 实测 | 模型 |\n| 22 | 0.9 | 0.4 "
        "| **超标** |\n", encoding="utf-8")
    code, out = run_cli(mod, tmp_path, "--accept")
    assert code == 1
    assert "超标" in out


def test_accept_daily_sweep_treats_na_as_fail(tmp_path):
    """逐日严格复核：--accept 下 daily 检查 ok=None（无样本/无法判定）同样
    不通过——会话配置不可读 → 禁入窗判 None → 未验收。"""
    mod = load_demo_check()
    build_full_evidence(tmp_path)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = mod.main([
            "--data-dir", str(tmp_path / "data"),
            "--log-dir", str(tmp_path / "logs"),
            "--reports-dir", str(tmp_path / "reports"),
            "--sessions-config", str(tmp_path / "nope-sessions.toml"),
            "--accept"])
    assert code == 1
    assert "无样本" in out.getvalue() or "无法判定" in out.getvalue()


# ---------------------------------------------------------------- 日常档（宽松）

def test_day_lenient_returns_zero_without_anomaly(tmp_path):
    """--day 日常检查：无异常证据即返回 0（不要求 SL/TP/全平等全期证据）。"""
    mod = load_demo_check()
    build_full_evidence(tmp_path)
    code, out = run_cli(mod, tmp_path, "--day", "2026-11-04")
    assert code == 0, out


def test_day_lenient_flags_real_anomaly(tmp_path):
    """--day 宽松但异常证据（未解释持仓差异）仍返回 1。"""
    mod = load_demo_check()
    build_full_evidence(tmp_path)
    # 最新日意图持多 0.01，实际空仓 → 现场差分（无法解释差异）
    doc_path = tmp_path / "data" / "live" / f"expected-{MAGIC}.json"
    doc = json.loads(doc_path.read_text(encoding="utf-8"))
    doc["volume"] = 0.01
    doc_path.write_text(json.dumps(doc), encoding="utf-8")
    code, out = run_cli(mod, tmp_path, "--day", TRADING_DAYS[-1].isoformat())
    assert code == 1, out
