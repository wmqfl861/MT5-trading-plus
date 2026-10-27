"""scripts/demo_check.py 双档口径测试（T49/R8-I5 + T53/T54/R9-I3+I4）。

两档口径（R8-I5）：日常档 ``--day``/``--summary`` 宽松显示（无异常证据即 0，
无样本如实标注、不冒充通过）；验收档 ``--accept`` 严格（无样本/未覆盖=未验收，
退出码非 0）。

T53（R9-I3）：``--accept`` 的证据源钉死为 Demo 运行契约——
- C-1 ``<data>/live/run-manifest.json``：mode 必须 "Demo"、magics 与证据一致、
  运行窗=当前 mode 最早 started_utc..now（重启不清窗）；manifest 缺失/mode
  不符/存在 paper 状态文件/日志含 [paper 行 = "证据来源可疑"拒绝；
- C-2 ``deals-<magic>.jsonl``：成交/SL/TP（external_exit reason∈{SL,TP}）判据
  唯一采信源；external_exit 的 decision_bar 语义=检测当轮 bar 或 null；
- 持仓判据源=state-<magic>.json 的 known_positions（executor 每轮 _finish
  已持久化）——不连 MT5、不读 paper；
- 点差样本结构化解析（日报点差表须含 品种|小时|样本 可解析行）；滑点=
  deals 成交价 vs 决策参考价（bid/ask）差值分布，有样本+分位数入报告；
- §7 六项演练逐项判据（运行窗内）：killSwitch 停机+恢复对、心跳超时告警、
  分策略暂停+解除、gateway_reconnected 重连行、幂等（同 bar 同 magic 无
  重复开仓请求，推导）、换日禁入拒单记录；每项缺失=未验收；
- 存活判据加强：风控心跳日志覆盖当日每小时（而非一条日志）。

T54（R9-I4）：双全平验收读 C-3 drill-archive（或 commands 目录）三点关联：
指令与全部参与 magic（manifest 集合）的 ack 须 command_id 匹配+magic+时间
在指令后合理窗内+closed>0 或附空仓证明（编号协议 ack 本身即执行器重查
清零后的空仓证明，closed=0 的关联 ack 计入并标注）；孤儿 ack 不计；
一轮缺任一 magic 的 ack=该轮未完成；窗外旧指令（D′）不计。

R9 审核三反例（probes/2026-10-27-round9-audit.py build_full_evidence 变体）
回归：纯 paper 证据/标题日报/仅两条 paper 保护性出场日志 --accept 必须失败。

T59（R10 成果审核 P1-P4，2026-10-27）：跨组契约联测——消费端夹具不再
手写"两侧恰好同字符串"的掩蔽形态，改用生产端真实产物/真实格式：
- C-3 联测：真实调 mt5live.commands 的 write_flatten+write_ack+
  archive_if_all_acked 产两轮归档（ack 副本保留原名 flatten.ack-<magic>
  **无扩展名**）→ load_drill_rounds 必须读出（P1 修复前 acks=[]）；
- C-2 联测：deals 的 decision_bar 用真实裸 bar ISO（iso_utc 带 +00:00）、
  决策行/台账 decision_id 用真实 runner 格式（SYMBOL|TF|bar，naive 无
  时区）——两侧形态不同，关联键必须归一为解析后的 bar 收盘时刻
  （P2 修复前字符串相等比较恒 miss）；
- P3：manifest 缺 schema_version 即拒绝（fail-closed，与存在且 ≠1 同）；
- P4：activity_days 四个消费点按 C-1 运行窗裁剪——窗外旧证据
  （D′ 演练期遗留）不得撑长覆盖/被要求补齐。

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
MAGIC_B = 902602
SYMBOL = "XAUUSD"
# 运行窗起点定在固定的过去时刻（2026-10-12T00:05Z）：真实 now（≥2026-10-27）
# 总在全部证据之后，运行窗=WIN_START..now 覆盖全部证据日期，测试确定性成立。
WIN_START = "2026-10-12T00:05:00Z"
WIN_RESTART = "2026-10-14T12:30:00Z"
# 2026-10-12 是周一：10-12..10-16 + 10-19..10-23 = 连续 10 个交易日
TRADING_DAYS = [date(2026, 10, d) for d in (12, 13, 14, 15, 16, 19, 20, 21, 22, 23)]
WEEKEND = [date(2026, 10, 17), date(2026, 10, 18)]
SPAN_DAYS = [TRADING_DAYS[0] + timedelta(days=i)
             for i in range((TRADING_DAYS[-1] - TRADING_DAYS[0]).days + 1)]

DEC_SL = f"{SYMBOL}|M15|2026-10-12T10:45:00"
DEC_TP = f"{SYMBOL}|M15|2026-10-13T10:45:00"
DEC_IN3 = f"{SYMBOL}|M15|2026-10-15T10:45:00"
DEC_IN4 = f"{SYMBOL}|M15|2026-10-19T10:45:00"
# 生产端真实格式（T59/C-2 契约联测修正）：executor deals 行的 decision_bar
# = iso_utc(decision.bar_close_time)——裸 bar ISO、**带 +00:00**
# （executor.py:471）；runner 决策行/下单台账的 decision_id 后缀 =
# close_time.isoformat()——**naive 无时区**（runner.py:267-268）。两侧
# 字符串形态不同（这正是 P2 断链根因），关联键=解析归一后的 bar 收盘时刻。
BAR_SL = "2026-10-12T10:45:00+00:00"
BAR_TP = "2026-10-13T10:45:00+00:00"
BAR_IN3 = "2026-10-15T10:45:00+00:00"
BAR_IN4 = "2026-10-19T10:45:00+00:00"

DRILL1_CID = "a" * 31 + "1"
DRILL2_CID = "b" * 31 + "2"
DRILL1_TS = "2026-10-15T13:00:00+00:00"
DRILL2_TS = "2026-10-22T09:30:00+00:00"


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


# ---------------------------------------------------------------- 行构造

def decision_line(day: date, hh: int = 10, mm: int = 45, signal: int = 1,
                  *, quote: bool = True) -> str:
    """运行器决策行（live_runner 主循环格式；quote=False 构造无报价行）。"""
    bar = f"{day.isoformat()}T{hh:02d}:{mm:02d}:00"
    tail = " bid=4000.1 ask=4000.3" if quote else ""
    return (f"[{day.isoformat()}T{hh:02d}:{mm:02d}:05.000000] 决策 "
            f"{SYMBOL}|M15|{bar}：signal={signal} target={signal} "
            f"volume=None{tail}")


def risk_line(day: date, hh: int, hb: str = "ok", halt: str = "否") -> str:
    """风控进程轮询行（scripts/risk_process.py 格式；Demo 模式标签 [live]）。"""
    return (f"· {day.isoformat()} {hh:02d}:10:00 UTC [live] 心跳={hb} "
            f"halt={halt} flatten=— 日亏触发=否")


def c2_deal(ts: str, ticket: int, event: str, *, reason: str | None = None,
            price: float, side: str, volume: float = 0.01,
            decision_bar: str | None = None, magic: int = MAGIC) -> dict:
    """C-2 deals-<magic>.jsonl 单行（契约字段；external_exit 的 decision_bar
    =检测当轮 bar 或 null）。"""
    return {"ts": ts, "magic": magic, "ticket": ticket, "event": event,
            "reason": reason, "price": price, "volume": volume, "side": side,
            "decision_bar": decision_bar}


def write_drill_round(live: Path, cid: str, cmd_ts: str,
                      acks: list[tuple[int, int, str]]) -> None:
    """C-3 drill-archive/<command_id>/：指令副本 + 各 magic 的 ack 副本。"""
    d = live / "drill-archive" / cid
    d.mkdir(parents=True, exist_ok=True)
    (d / "command.json").write_text(json.dumps(
        {"command": "flatten_all", "id": cid, "reason": "daily_loss_cap",
         "ts": cmd_ts}), encoding="utf-8")
    for magic, closed, ts in acks:
        (d / f"ack-{magic}.json").write_text(json.dumps(
            {"magic": magic, "id": cid, "closed": closed, "ts": ts}),
            encoding="utf-8")


def write_flatten_archive(live: Path, cmd_id: str, day: date) -> None:
    """旧式人工归档指令（commands/archive；无逐 magic ack——T54 起不再单独
    构成完成证据，仅作 paper 反例 fixtures 用）。"""
    arch = live / "commands" / "archive"
    arch.mkdir(parents=True, exist_ok=True)
    (arch / f"flatten-{cmd_id[:8]}-{day.isoformat()}T120000-manual.json").write_text(
        json.dumps({"command": "flatten_all", "id": cmd_id,
                    "reason": "daily_loss_cap",
                    "ts": f"{day.isoformat()}T12:00:00+00:00"}), encoding="utf-8")


# ---------------------------------------------------------------- Demo 正例证据

def build_full_evidence(root: Path) -> dict:
    """构造阶段 F Demo 全证据树（C-1/C-2/C-3 契约格式 + §7 演练记录 +
    逐小时风控心跳 + 结构化点差日报 + 滑点样本）。返回关键路径。"""
    live = root / "data" / "live"
    logs = root / "logs"
    reports = root / "reports" / "live"
    for p in (live, live / "commands" / "archive", logs, reports):
        p.mkdir(parents=True, exist_ok=True)

    # C-1 run-manifest：mode=Demo、magics、追加式 runs（start+restart）
    manifest = {
        "schema_version": 1, "mode": "Demo", "magics": [MAGIC],
        "runs": [
            {"started_utc": WIN_START, "log_dir": str(logs), "kind": "start"},
            {"started_utc": WIN_RESTART, "log_dir": str(logs), "kind": "restart"},
        ],
        "account_hint": "demo"}
    (live / "run-manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8")

    # 意图（confirmed 空仓）+ 执行器状态（known_positions 已清空=全平后）
    (live / f"expected-{MAGIC}.json").write_text(json.dumps({
        "symbol": SYMBOL, "magic": MAGIC, "side": "buy", "volume": 0.0,
        "status": "confirmed", "cause": f"flatten:{DRILL2_CID}",
        "ts": "2026-10-22T09:30:31+00:00"}), encoding="utf-8")
    (live / f"state-{MAGIC}.json").write_text(json.dumps({
        "magic": MAGIC, "updated_at_utc": "2026-10-22T09:30:31+00:00",
        "blocked_dir": 0, "known_positions": {},
        "pending_external_exits": {}, "last_decision_id": DEC_IN4}),
        encoding="utf-8")
    # 分策略暂停演练的解除证据（对账进程写回 paused=false）
    (live / f"pause-{MAGIC}.json").write_text(json.dumps({
        "magic": MAGIC, "paused": False, "reason": None,
        "ts": "2026-10-15T13:35:00+00:00", "diffs": []}), encoding="utf-8")

    # C-2 deals-<magic>.jsonl：开仓/保护性出场（SL+TP）/两轮全平的主动平仓。
    # fill_open 的 decision_bar 用生产端真实格式（executor.py:471 iso_utc——
    # 裸 bar 带 +00:00；T59 契约联测修正，旧夹具写全 decision_id 属掩蔽形态）
    deals = [
        c2_deal("2026-10-12T10:45:06+00:00", 101, "fill_open",
                price=4000.4, side="buy", decision_bar=BAR_SL),
        c2_deal("2026-10-12T16:20:11+00:00", 101, "external_exit",
                reason="SL", price=3995.0, side="sell",
                decision_bar="2026-10-12T16:15:00Z"),
        c2_deal("2026-10-13T10:45:07+00:00", 102, "fill_open",
                price=4001.2, side="buy", decision_bar=BAR_TP),
        c2_deal("2026-10-13T15:30:05+00:00", 102, "external_exit",
                reason="TP", price=4006.8, side="sell", decision_bar=None),
        c2_deal("2026-10-15T10:45:06+00:00", 103, "fill_open",
                price=4002.0, side="buy", decision_bar=BAR_IN3),
        c2_deal("2026-10-15T13:00:30+00:00", 103, "fill_close",
                price=4001.5, side="sell", decision_bar=None),
        c2_deal("2026-10-19T10:45:06+00:00", 104, "fill_open",
                price=4003.0, side="buy", decision_bar=BAR_IN4),
        c2_deal("2026-10-22T09:30:30+00:00", 104, "fill_close",
                price=4005.0, side="sell", decision_bar=None),
    ]
    with open(live / f"deals-{MAGIC}.jsonl", "w", encoding="utf-8") as f:
        for d in deals:
            f.write(json.dumps(d) + "\n")

    # 下单台账：四笔开仓（唯一 decision_id——幂等推导样本）+ 两笔全平平仓
    with open(live / f"orders-{MAGIC}.jsonl", "w", encoding="utf-8") as f:
        for dec, ts in ((DEC_SL, "2026-10-12T10:45:06"),
                        (DEC_TP, "2026-10-13T10:45:07"),
                        (DEC_IN3, "2026-10-15T10:45:06"),
                        (DEC_IN4, "2026-10-19T10:45:06")):
            f.write(json.dumps({
                "ts": ts + "+00:00", "magic": MAGIC, "symbol": SYMBOL,
                "kind": "open", "side": "buy", "lots": 0.01,
                "decision_id": dec}) + "\n")
        for ts in ("2026-10-15T13:00:31", "2026-10-22T09:30:31"):
            f.write(json.dumps({
                "ts": ts + "+00:00", "magic": MAGIC, "symbol": SYMBOL,
                "kind": "close", "side": "sell", "lots": 0.01,
                "decision_id": "-"}) + "\n")

    # 告警（ts\tKIND\t消息）：§7 演练记录（killSwitch/心跳超时/分策略暂停/禁入拒单）
    alerts = [
        "2026-10-13T09:00:00+00:00\tRISK_HALT\t风控停机：kill_switch",
        "2026-10-14T12:00:05+00:00\tHEARTBEAT_TIMEOUT\t运行器心跳 timeout"
        "（heartbeat-902601.json）最后心跳 125 秒前，已全局停机",
        "2026-10-15T13:00:00+00:00\tRECONCILE_MISMATCH\t1 处持仓不一致，"
        "已暂停 magic [902601]（详见 pause-<magic>.json）",
        "2026-10-16T22:05:00+00:00\tRISK_REJECT\t"
        f"magic {MAGIC} {SYMBOL}|M15|2026-10-16T22:00:00：forbidden_session: "
        f"{SYMBOL} UTC 22:05 处于禁止开仓时段（config/sessions.toml 换日窗，"
        "平仓不受限）",
    ]
    (live / "alerts.log").write_text("\n".join(alerts) + "\n", encoding="utf-8")

    # 运行器日志 A：10-12 00:05 启动 .. 10-14 11:45（心跳超时前）
    runner_a = ["mt5live 启动：XAUUSD M15 magic=902601 strategy=demo "
                "手数=固定 0.01 [LIVE]"]
    for d in (date(2026, 10, 12), date(2026, 10, 13)):
        runner_a.append(decision_line(d, 10, 45, signal=1))
        runner_a.append(decision_line(d, 16, 0, signal=0))
    runner_a.append(decision_line(date(2026, 10, 14), 10, 45, signal=1))
    runner_a.append(decision_line(date(2026, 10, 14), 11, 45, signal=0))
    (logs / "runner-902601-20261012-000500.log").write_text(
        "\n".join(runner_a) + "\n", encoding="utf-8")

    # 运行器日志 B：10-14 12:30 重启（心跳超时后恢复）.. 10-23；
    # 含 C-4 重连标记行（网关失败后的下一次成功调用）
    runner_b = ["mt5live 启动：XAUUSD M15 magic=902601 strategy=demo "
                "手数=固定 0.01 [LIVE]",
                "[2026-10-14T12:30:05.000000] 启动恢复：读取 "
                f"state-{MAGIC}.json 重放已知持仓",
                "[2026-10-16T09:15:02.000000] gateway_reconnected "
                "prev_failure=quote（网关调用失败后的下一次成功调用）"]
    for d in TRADING_DAYS:
        if d < date(2026, 10, 14):
            continue
        runner_b.append(decision_line(d, 10, 45, signal=1))
        runner_b.append(decision_line(d, 16, 0, signal=0))
    for d in WEEKEND:
        runner_b.append(
            f"[{d.isoformat()}T12:00:05.000000] 休市跳过 1：{SYMBOL} 休市"
            "（最新报价陈旧超过阈值），跳过本网格点")
    (logs / "runner-902601-20261014-123000.log").write_text(
        "\n".join(runner_b) + "\n", encoding="utf-8")

    # 风控日志：跨度内逐日逐小时心跳行（存活判据=覆盖当日每小时）；
    # 10-13 09 点 killSwitch 停机（halt=是）后恢复；10-14 12-13 时心跳超时
    risk_lines = []
    for d in SPAN_DAYS:
        for hh in range(24):
            hb, halt = "ok", "否"
            if d == date(2026, 10, 13) and hh == 9:
                halt = "是"
            if d == date(2026, 10, 14) and hh in (12, 13):
                hb, halt = "timeout", "是"
            risk_lines.append(risk_line(d, hh, hb=hb, halt=halt))
    (logs / "risk-account-20261012-000500.log").write_text(
        "\n".join(risk_lines) + "\n", encoding="utf-8")

    # 对账日志：逐日对账行（10-15 不一致→13:35 解除）
    recon_lines = [f"· {d.isoformat()} 10:00:41 UTC 对账一致（1 个持仓）"
                   for d in SPAN_DAYS]
    recon_lines.append("· 2026-10-15 13:35:41 UTC 对账一致（0 个持仓）")
    (logs / "reconcile-all-20261012-000500.log").write_text(
        "\n".join(recon_lines) + "\n", encoding="utf-8")

    # 日报：交易日均有结构化点差表（品种|小时|样本 可解析行）且无超标
    spread_md = ("# 实盘日报 {d}\n\n## 实际点差 vs SpreadModel p90\n\n"
                 "| 品种 | 小时(UTC) | 样本 | 实测最大 | 模型 p90 | 实测/p90 "
                 "| 判定 |\n| --- | --- | ---: | ---: | ---: | ---: | --- |\n"
                 "| XAUUSD | 10 | 4 | 0.2 | 0.3 | 0.666667 | 正常 |\n"
                 "| XAUUSD | 16 | 4 | 0.25 | 0.3 | 0.833333 | 正常 |\n")
    for d in TRADING_DAYS:
        (reports / f"daily-{d.isoformat()}.md").write_text(
            spread_md.format(d=d.isoformat()), encoding="utf-8")

    # C-3 演练归档：两轮不同 uuid 指令 × 全部参与 magic 的关联 ack
    write_drill_round(live, DRILL1_CID, DRILL1_TS,
                      [(MAGIC, 1, "2026-10-15T13:00:30+00:00")])
    write_drill_round(live, DRILL2_CID, DRILL2_TS,
                      [(MAGIC, 1, "2026-10-22T09:30:30+00:00")])

    return {
        "live": live, "logs": logs, "reports": reports,
        "manifest": live / "run-manifest.json",
        "deals_log": live / f"deals-{MAGIC}.jsonl",
        "orders_log": live / f"orders-{MAGIC}.jsonl",
        "alerts": live / "alerts.log",
        "state": live / f"state-{MAGIC}.json",
        "intent": live / f"expected-{MAGIC}.json",
        "runner_a": logs / "runner-902601-20261012-000500.log",
        "runner_b": logs / "runner-902601-20261014-123000.log",
        "risk_log": logs / "risk-account-20261012-000500.log",
        "recon_log": logs / "reconcile-all-20261012-000500.log",
        "drill1": live / "drill-archive" / DRILL1_CID,
        "drill2": live / "drill-archive" / DRILL2_CID,
    }


def add_extra_magic(paths: dict, magic: int) -> None:
    """给正例证据加第二个 magic（意图/状态/运行器日志/演练 ack）——
    T54 多 magic 关联测试用。"""
    live, logs = paths["live"], paths["logs"]
    (live / f"expected-{magic}.json").write_text(json.dumps({
        "symbol": SYMBOL, "magic": magic, "side": "buy", "volume": 0.0,
        "status": "confirmed", "cause": "initial",
        "ts": "2026-10-12T01:00:00+00:00"}), encoding="utf-8")
    (live / f"state-{magic}.json").write_text(json.dumps({
        "magic": magic, "updated_at_utc": "2026-10-22T09:30:31+00:00",
        "blocked_dir": 0, "known_positions": {},
        "pending_external_exits": {}, "last_decision_id": None}),
        encoding="utf-8")
    (logs / f"runner-{magic}-20261012-000500.log").write_text(
        f"mt5live 启动：{SYMBOL} M15 magic={magic} strategy=demo "
        "手数=固定 0.01 [LIVE]\n"
        "[2026-10-12T10:45:05.000000] 决策 ...\n", encoding="utf-8")
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    manifest["magics"] = sorted(manifest["magics"] + [magic])
    paths["manifest"].write_text(json.dumps(manifest), encoding="utf-8")


def drop_day_from_log(path: Path, day: date) -> None:
    """删掉日志里某日的全部行（构造"该日无进程存活证据"）。"""
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines()
             if day.isoformat() not in ln]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def drop_hour_from_log(path: Path, day: date, hour: int) -> None:
    """删掉风控日志某日某小时的心跳行（构造"逐小时覆盖缺格"）。"""
    keep = f"{day.isoformat()} {hour:02d}:"
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines()
             if not ln.startswith("·") or keep not in ln]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def filter_alerts(path: Path, drop_substring: str) -> None:
    """删掉 alerts.log 中含指定子串的告警行。"""
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines()
             if drop_substring not in ln]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- --accept 正例

def test_accept_passes_with_full_evidence(tmp_path):
    mod = load_demo_check()
    build_full_evidence(tmp_path)
    code, out = run_cli(mod, tmp_path, "--accept")
    assert code == 0, out
    assert "验收通过" in out
    for name in ("证据来源", "known_positions", "连续交易日覆盖", "逐小时",
                 "SL 与 TP 样本", "六项演练", "双全平演练", "点差样本", "滑点"):
        assert name in out, name + " 判据缺失：\n" + out


# ---------------------------------------------------------------- R9-I3 三反例

def build_paper_only_evidence(root: Path) -> None:
    """纯 paper 证据（R9-I3 反例 1）：影子账本 + [paper] 风控日志 + 文本日报 +
    人工归档指令——无 manifest、无 C-2 deals、无演练记录。旧版 --accept 误过。"""
    live = root / "data" / "live"
    logs = root / "logs"
    reports = root / "reports" / "live"
    for p in (live / "paper", live / "commands" / "archive", logs, reports):
        p.mkdir(parents=True, exist_ok=True)
    (live / f"expected-{MAGIC}.json").write_text(json.dumps({
        "symbol": SYMBOL, "magic": MAGIC, "side": "buy", "volume": 0.0,
        "status": "confirmed", "cause": "initial",
        "ts": "2026-10-23T20:00:00+00:00"}), encoding="utf-8")

    def pdeal(ticket, day, entry, reason=3, comment=""):
        return {"ticket": ticket, "position_id": ticket, "symbol": SYMBOL,
                "side": "buy" if entry == "in" else "sell", "entry": entry,
                "volume": 0.01, "price": 4000.0, "profit": 0.0,
                "commission": 0.0, "swap": 0.0, "fee": 0.0,
                "time": f"{day.isoformat()}T10:45:05+00:00",
                "comment": comment, "magic": MAGIC, "reason": reason}

    (live / "paper" / f"state-{MAGIC}.json").write_text(json.dumps({
        "magic": MAGIC, "balance": 10000.0, "equity": 10000.0,
        "positions": [],
        "deals": [pdeal(1, TRADING_DAYS[0], "in", comment=DEC_SL),
                  pdeal(2, TRADING_DAYS[0], "out", reason=4),
                  pdeal(3, TRADING_DAYS[1], "in", comment=DEC_TP),
                  pdeal(4, TRADING_DAYS[1], "out", reason=5)]}), encoding="utf-8")
    (live / "alerts.log").write_text("", encoding="utf-8")
    (logs / "runner-paper.log").write_text(
        f"mt5live 启动：{SYMBOL} M15 magic={MAGIC} strategy=demo "
        "手数=固定 0.01 [paper]\n", encoding="utf-8")
    risk, recon = [], []
    for d in SPAN_DAYS:
        for hm in ("10:00:10", "16:00:10", "21:00:10"):
            risk.append(f"· {d.isoformat()} {hm} UTC [paper] 心跳=ok halt=否 "
                        "flatten=— 日亏触发=否")
        recon.append(f"· {d.isoformat()} 10:00:41 UTC 对账一致（0 个持仓）")
    (logs / "risk-paper.log").write_text("\n".join(risk) + "\n", encoding="utf-8")
    (logs / "recon-paper.log").write_text("\n".join(recon) + "\n", encoding="utf-8")
    for d in TRADING_DAYS:
        (reports / f"daily-{d.isoformat()}.md").write_text(
            f"# 演练日报 {d.isoformat()}\n\n点差比对（按小时）：12 行全部在"
            "成本模型区间内（无超标行）。\n", encoding="utf-8")
    write_flatten_archive(live, "a" * 31 + "1", TRADING_DAYS[3])
    write_flatten_archive(live, "b" * 31 + "2", TRADING_DAYS[7])


def test_accept_rejects_pure_paper_evidence(tmp_path):
    """R9-I3 反例 1：纯 paper 证据（无 manifest/C-2/C-3）→ 证据来源可疑拒绝。"""
    mod = load_demo_check()
    build_paper_only_evidence(tmp_path)
    code, out = run_cli(mod, tmp_path, "--accept")
    assert code == 1, out
    assert "证据来源可疑" in out
    assert "验收通过" not in out


def test_accept_rejects_headings_only_reports(tmp_path):
    """R9-I3 反例 2：日报只剩标题（无结构化点差行）→ 点差判据不通过。"""
    mod = load_demo_check()
    paths = build_full_evidence(tmp_path)
    for f in paths["reports"].glob("daily-*.md"):
        f.write_text("# Daily report\n", encoding="utf-8")
    code, out = run_cli(mod, tmp_path, "--accept")
    assert code == 1, out
    assert "点差" in out and "不通过" in out
    assert "验收通过" not in out


def test_accept_rejects_paper_protective_logs_without_deals(tmp_path):
    """R9-I3 反例 3：删 C-2 deals、只留两条 [paper] 保护性出场日志 →
    拒绝（paper 日志不构成 Demo SL/TP 证据，且日志含 [paper 行=来源可疑）。"""
    mod = load_demo_check()
    paths = build_full_evidence(tmp_path)
    paths["deals_log"].unlink()
    with open(paths["runner_a"], "a", encoding="utf-8") as f:
        f.write(f"[2026-10-12T16:20:12] [paper] 保护性出场 1 笔：#101 "
                "reason=4 price=3995.0 pnl=-5.40\n")
        f.write(f"[2026-10-13T15:30:06] [paper] 保护性出场 1 笔：#102 "
                "reason=5 price=4006.8 pnl=+6.60\n")
    code, out = run_cli(mod, tmp_path, "--accept")
    assert code == 1, out
    assert "证据来源可疑" in out
    assert "SL 与 TP 样本" in out and "不通过" in out
    assert "验收通过" not in out


# ---------------------------------------------------------------- 证据源（C-1）

def test_accept_rejects_missing_manifest(tmp_path):
    """无 run-manifest.json → 证据来源可疑（旧版纯文本证据不再可验收）。"""
    mod = load_demo_check()
    paths = build_full_evidence(tmp_path)
    paths["manifest"].unlink()
    code, out = run_cli(mod, tmp_path, "--accept")
    assert code == 1, out
    assert "证据来源可疑" in out
    assert "验收通过" not in out


def test_accept_rejects_papersoak_mode_manifest(tmp_path):
    """manifest mode=PaperSoak → --accept 不采信（要求 mode=="Demo"）。"""
    mod = load_demo_check()
    paths = build_full_evidence(tmp_path)
    doc = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    doc["mode"] = "PaperSoak"
    paths["manifest"].write_text(json.dumps(doc), encoding="utf-8")
    code, out = run_cli(mod, tmp_path, "--accept")
    assert code == 1, out
    assert "PaperSoak" in out
    assert "验收通过" not in out


def test_accept_rejects_magic_mismatch(tmp_path):
    """manifest magics 与证据不一致（多出无证据的 902602）→ 拒绝。"""
    mod = load_demo_check()
    paths = build_full_evidence(tmp_path)
    doc = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    doc["magics"] = [MAGIC, MAGIC_B]
    paths["manifest"].write_text(json.dumps(doc), encoding="utf-8")
    code, out = run_cli(mod, tmp_path, "--accept")
    assert code == 1, out
    assert str(MAGIC_B) in out
    assert "验收通过" not in out


def test_accept_rejects_leftover_paper_state_files(tmp_path):
    """Demo 证据完整但残留 paper 状态文件 → 证据来源可疑。"""
    mod = load_demo_check()
    paths = build_full_evidence(tmp_path)
    (paths["live"] / "paper").mkdir(exist_ok=True)
    (paths["live"] / "paper" / f"state-{MAGIC}.json").write_text(
        "{}", encoding="utf-8")
    code, out = run_cli(mod, tmp_path, "--accept")
    assert code == 1, out
    assert "证据来源可疑" in out
    assert "验收通过" not in out


# ---------------------------------------------------------------- 持仓判据（known_positions）

def test_accept_fails_missing_state_known_positions(tmp_path):
    """持仓判据：magic 无 state-<magic>.json（executor 未持久化）→ 未验收。"""
    mod = load_demo_check()
    paths = build_full_evidence(tmp_path)
    paths["state"].unlink()
    code, out = run_cli(mod, tmp_path, "--accept")
    assert code == 1, out
    assert "known_positions" in out


def test_accept_fails_known_positions_intent_mismatch(tmp_path):
    """持仓判据：known_positions 净额与 confirmed 意图不一致 → 未验收。"""
    mod = load_demo_check()
    paths = build_full_evidence(tmp_path)
    doc = json.loads(paths["state"].read_text(encoding="utf-8"))
    doc["known_positions"] = {"555": {"dir": 1, "volume": 0.01}}
    paths["state"].write_text(json.dumps(doc), encoding="utf-8")
    code, out = run_cli(mod, tmp_path, "--accept")
    assert code == 1, out
    assert "known_positions" in out and "不一致" in out


# ---------------------------------------------------------------- 覆盖与存活

def test_accept_fails_short_coverage(tmp_path):
    """判据：删掉一个交易日（周三 10-14）→ 覆盖不连续 → 未验收。"""
    mod = load_demo_check()
    paths = build_full_evidence(tmp_path)
    gone = date(2026, 10, 14)
    (paths["reports"] / f"daily-{gone.isoformat()}.md").unlink()
    drop_day_from_log(paths["runner_a"], gone)
    drop_day_from_log(paths["runner_b"], gone)
    drop_day_from_log(paths["risk_log"], gone)
    drop_day_from_log(paths["recon_log"], gone)
    code, out = run_cli(mod, tmp_path, "--accept")
    assert code == 1, out
    assert "覆盖" in out and "不通过" in out


def test_accept_fails_only_nine_trading_days(tmp_path):
    """判据：只有 9 个交易日 → 未验收。"""
    mod = load_demo_check()
    paths = build_full_evidence(tmp_path)
    last = TRADING_DAYS[-1]
    (paths["reports"] / f"daily-{last.isoformat()}.md").unlink()
    drop_day_from_log(paths["runner_a"], last)
    drop_day_from_log(paths["runner_b"], last)
    drop_day_from_log(paths["risk_log"], last)
    drop_day_from_log(paths["recon_log"], last)
    code, out = run_cli(mod, tmp_path, "--accept")
    assert code == 1, out
    assert "10" in out and "覆盖" in out


def test_accept_fails_missing_risk_evidence(tmp_path):
    """判据：某日无风控日志行 → 进程存活证据缺 → 未验收。"""
    mod = load_demo_check()
    paths = build_full_evidence(tmp_path)
    drop_day_from_log(paths["risk_log"], date(2026, 10, 15))
    code, out = run_cli(mod, tmp_path, "--accept")
    assert code == 1, out
    assert "风控" in out


def test_accept_fails_missing_reconcile_evidence(tmp_path):
    """判据：某日无对账日志行 → 未验收。"""
    mod = load_demo_check()
    paths = build_full_evidence(tmp_path)
    drop_day_from_log(paths["recon_log"], date(2026, 10, 20))
    code, out = run_cli(mod, tmp_path, "--accept")
    assert code == 1, out
    assert "对账进程" in out


def test_accept_fails_dead_runner_day(tmp_path):
    """判据：某日运行器无自身日志行、也无风控"心跳=ok"佐证 → 未验收。"""
    mod = load_demo_check()
    paths = build_full_evidence(tmp_path)
    day = date(2026, 10, 21)
    drop_day_from_log(paths["runner_a"], day)      # 当日行在日志 B
    drop_day_from_log(paths["runner_b"], day)
    drop_day_from_log(paths["risk_log"], day)
    with open(paths["risk_log"], "a", encoding="utf-8") as f:
        f.write(risk_line(day, 16, hb="timeout", halt="是"))
    code, out = run_cli(mod, tmp_path, "--accept")
    assert code == 1, out
    assert "运行器" in out


def test_accept_fails_hourly_heartbeat_gap(tmp_path):
    """T53 存活判据加强：某日缺一个整小时的风控心跳行 → 未验收
    （一条日志不再能证明整日存活）。"""
    mod = load_demo_check()
    paths = build_full_evidence(tmp_path)
    drop_hour_from_log(paths["risk_log"], date(2026, 10, 16), 5)
    code, out = run_cli(mod, tmp_path, "--accept")
    assert code == 1, out
    assert "逐小时" in out or "小时" in out


# ---------------------------------------------------------------- SL/TP（C-2）

def test_accept_fails_no_tp_sample(tmp_path):
    """判据：C-2 deals 只有 SL 样本、无 TP 样本 → 未验收。"""
    mod = load_demo_check()
    paths = build_full_evidence(tmp_path)
    lines = [ln for ln in paths["deals_log"].read_text(
        encoding="utf-8").splitlines() if '"TP"' not in ln]
    paths["deals_log"].write_text("\n".join(lines) + "\n", encoding="utf-8")
    code, out = run_cli(mod, tmp_path, "--accept")
    assert code == 1, out
    assert "TP" in out and "不通过" in out


def test_accept_fails_no_sl_sample(tmp_path):
    """判据：C-2 deals 只有 TP 样本、无 SL 样本 → 未验收。"""
    mod = load_demo_check()
    paths = build_full_evidence(tmp_path)
    lines = [ln for ln in paths["deals_log"].read_text(
        encoding="utf-8").splitlines() if '"SL"' not in ln]
    paths["deals_log"].write_text("\n".join(lines) + "\n", encoding="utf-8")
    code, out = run_cli(mod, tmp_path, "--accept")
    assert code == 1, out
    assert "SL" in out and "不通过" in out


# ---------------------------------------------------------------- §7 六项演练

def test_accept_fails_drill_kill_switch_missing(tmp_path):
    """演练项 1：killSwitch 停机告警缺失 → 未验收。"""
    mod = load_demo_check()
    paths = build_full_evidence(tmp_path)
    filter_alerts(paths["alerts"], "kill_switch")
    code, out = run_cli(mod, tmp_path, "--accept")
    assert code == 1, out
    assert "killSwitch" in out and "不通过" in out


def test_accept_fails_drill_kill_switch_no_recovery(tmp_path):
    """演练项 1：killSwitch 停机后无恢复（风控日志无后续 halt=否 行）→
    未验收。"""
    mod = load_demo_check()
    paths = build_full_evidence(tmp_path)
    lines = [ln for ln in paths["risk_log"].read_text(
        encoding="utf-8").splitlines()
        if not (ln.startswith("· 2026-10-13") and "halt=否" in ln)]
    paths["risk_log"].write_text("\n".join(lines) + "\n", encoding="utf-8")
    code, out = run_cli(mod, tmp_path, "--accept")
    assert code == 1, out
    assert "killSwitch" in out and "恢复" in out


def test_accept_fails_drill_heartbeat_timeout_missing(tmp_path):
    """演练项 2：HEARTBEAT_TIMEOUT 告警缺失 → 未验收。"""
    mod = load_demo_check()
    paths = build_full_evidence(tmp_path)
    filter_alerts(paths["alerts"], "HEARTBEAT_TIMEOUT")
    code, out = run_cli(mod, tmp_path, "--accept")
    assert code == 1, out
    assert "心跳超时" in out


def test_accept_fails_drill_pause_missing(tmp_path):
    """演练项 3：RECONCILE 暂停+解除缺失（告警被删）→ 未验收。"""
    mod = load_demo_check()
    paths = build_full_evidence(tmp_path)
    filter_alerts(paths["alerts"], "RECONCILE_MISMATCH")
    code, out = run_cli(mod, tmp_path, "--accept")
    assert code == 1, out
    assert "分策略暂停" in out


def test_accept_fails_drill_pause_not_released(tmp_path):
    """演练项 3：暂停写了但未解除（pause 仍 paused=true）→ 未验收。"""
    mod = load_demo_check()
    paths = build_full_evidence(tmp_path)
    (paths["live"] / f"pause-{MAGIC}.json").write_text(json.dumps({
        "magic": MAGIC, "paused": True, "reason": "reconcile_mismatch",
        "ts": "2026-10-15T13:00:10+00:00", "diffs": []}), encoding="utf-8")
    code, out = run_cli(mod, tmp_path, "--accept")
    assert code == 1, out
    assert "分策略暂停" in out and "解除" in out


def test_accept_fails_drill_reconnect_missing(tmp_path):
    """演练项 4：gateway_reconnected 重连行缺失 → 未验收。"""
    mod = load_demo_check()
    paths = build_full_evidence(tmp_path)
    lines = [ln for ln in paths["runner_b"].read_text(
        encoding="utf-8").splitlines() if "gateway_reconnected" not in ln]
    paths["runner_b"].write_text("\n".join(lines) + "\n", encoding="utf-8")
    code, out = run_cli(mod, tmp_path, "--accept")
    assert code == 1, out
    assert "重连" in out


def test_accept_fails_drill_duplicate_open_requests(tmp_path):
    """演练项 5（幂等推导）：同 bar 同 magic 重复开仓请求 → 未验收。"""
    mod = load_demo_check()
    paths = build_full_evidence(tmp_path)
    dup = json.dumps({
        "ts": "2026-10-12T10:45:08+00:00", "magic": MAGIC, "symbol": SYMBOL,
        "kind": "open", "side": "buy", "lots": 0.01, "decision_id": DEC_SL})
    with open(paths["orders_log"], "a", encoding="utf-8") as f:
        f.write(dup + "\n")
    code, out = run_cli(mod, tmp_path, "--accept")
    assert code == 1, out
    assert "幂等" in out


def test_accept_fails_drill_forbidden_reject_missing(tmp_path):
    """演练项 6：换日禁入窗拒单记录缺失 → 未验收。"""
    mod = load_demo_check()
    paths = build_full_evidence(tmp_path)
    filter_alerts(paths["alerts"], "forbidden_session")
    code, out = run_cli(mod, tmp_path, "--accept")
    assert code == 1, out
    assert "禁入" in out


# ---------------------------------------------------------------- 点差/滑点（结构化）

def test_accept_fails_spread_sample_missing_one_day(tmp_path):
    """判据：某交易日日报"当日无点差样本" → 未验收（无样本≠通过）。"""
    mod = load_demo_check()
    paths = build_full_evidence(tmp_path)
    (paths["reports"] / "daily-2026-10-22.md").write_text(
        "# 实盘日报 2026-10-22\n\n当日无点差样本\n", encoding="utf-8")
    code, out = run_cli(mod, tmp_path, "--accept")
    assert code == 1, out
    assert "点差样本" in out and "不通过" in out


def test_accept_fails_spread_breach_hours(tmp_path):
    """判据：日报含"**超标**"小时 → 未验收（实际点差超出成本模型区间）。"""
    mod = load_demo_check()
    paths = build_full_evidence(tmp_path)
    (paths["reports"] / "daily-2026-10-16.md").write_text(
        "# 实盘日报 2026-10-16\n\n| 品种 | 小时(UTC) | 样本 | 实测最大 | "
        "模型 p90 | 实测/p90 | 判定 |\n| --- | --- | ---: | ---: | ---: | "
        "---: | --- |\n| XAUUSD | 22 | 4 | 0.9 | 0.4 | 2.25 | **超标** |\n",
        encoding="utf-8")
    code, out = run_cli(mod, tmp_path, "--accept")
    assert code == 1, out
    assert "超标" in out


def test_accept_fails_no_slippage_samples(tmp_path):
    """T53 滑点判据：决策行缺 bid/ask（无决策参考价）→ 无滑点样本 →
    未验收。"""
    mod = load_demo_check()
    paths = build_full_evidence(tmp_path)
    for key in ("runner_a", "runner_b"):
        p = paths[key]
        lines = [ln.replace(" bid=4000.1 ask=4000.3", "")
                 for ln in p.read_text(encoding="utf-8").splitlines()]
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    code, out = run_cli(mod, tmp_path, "--accept")
    assert code == 1, out
    assert "滑点" in out and "不通过" in out


# ---------------------------------------------------------------- T54 双全平

def test_accept_rejects_orphan_acks_closed_zero(tmp_path):
    """R9-I4 反例：删全部指令/演练归档，只放两个无 magic 无指令的
    {"id","closed":0} 孤儿 ack → 未验收（三点关联不成立，孤儿 ack 不计）。"""
    mod = load_demo_check()
    paths = build_full_evidence(tmp_path)
    import shutil
    shutil.rmtree(paths["live"] / "drill-archive")
    arch = paths["live"] / "commands" / "archive"
    arch.mkdir(parents=True, exist_ok=True)
    for cid in ("c" * 32, "d" * 32):
        (arch / f"flatten.ack-{cid}.json").write_text(
            json.dumps({"id": cid, "closed": 0}), encoding="utf-8")
    code, out = run_cli(mod, tmp_path, "--accept")
    assert code == 1, out
    assert "双全平" in out and "不通过" in out
    assert "验收通过" not in out


def test_accept_fails_single_flatten_round(tmp_path):
    """T54：只有一轮完成（第二个演练轮被删）→ 未验收。"""
    mod = load_demo_check()
    paths = build_full_evidence(tmp_path)
    import shutil
    shutil.rmtree(paths["drill2"])
    code, out = run_cli(mod, tmp_path, "--accept")
    assert code == 1, out
    assert "双全平" in out and "不通过" in out


def test_accept_fails_same_uuid_twice(tmp_path):
    """T54：两轮演练但第二轮指令 uuid 被改成与第一轮相同 → 未验收。"""
    mod = load_demo_check()
    paths = build_full_evidence(tmp_path)
    (paths["drill2"] / "command.json").write_text(json.dumps(
        {"command": "flatten_all", "id": DRILL1_CID, "reason": "replay",
         "ts": DRILL2_TS}), encoding="utf-8")
    (paths["drill2"] / f"ack-{MAGIC}.json").write_text(json.dumps(
        {"magic": MAGIC, "id": DRILL1_CID, "closed": 1,
         "ts": "2026-10-22T09:30:30+00:00"}), encoding="utf-8")
    code, out = run_cli(mod, tmp_path, "--accept")
    assert code == 1, out
    assert "双全平" in out and "不通过" in out


def test_accept_ignores_out_of_window_flatten_command(tmp_path):
    """T54：第二轮指令时间在运行窗外（D′ 旧指令）→ 不计 → 未验收。"""
    mod = load_demo_check()
    paths = build_full_evidence(tmp_path)
    write_drill_round(paths["live"], DRILL2_CID, "2026-09-30T09:30:00+00:00",
                      [(MAGIC, 1, "2026-09-30T09:30:30+00:00")])
    code, out = run_cli(mod, tmp_path, "--accept")
    assert code == 1, out
    assert "双全平" in out and "不通过" in out


def test_accept_fails_round_missing_magic_ack(tmp_path):
    """T54：两个 magic 参与演练，第二轮缺 902602 的 ack → 该轮未完成 →
    未验收（须全部参与 magic 的关联 ack）。"""
    mod = load_demo_check()
    paths = build_full_evidence(tmp_path)
    add_extra_magic(paths, MAGIC_B)
    # 第一轮全员 ack；第二轮只留 902601 的 ack（缺 902602）
    write_drill_round(paths["live"], DRILL1_CID, DRILL1_TS, [
        (MAGIC, 1, "2026-10-15T13:00:30+00:00"),
        (MAGIC_B, 0, "2026-10-15T13:00:29+00:00"),   # closed=0：关联 ack 即空仓证明
    ])
    code, out = run_cli(mod, tmp_path, "--accept")
    assert code == 1, out
    assert "双全平" in out and str(MAGIC_B) in out


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
    code, out = run_cli(mod, tmp_path, "--day", "2026-10-14")
    assert code == 0, out


def test_day_lenient_flags_real_anomaly(tmp_path):
    """--day 宽松但异常证据（未解释持仓差异）仍返回 1。"""
    mod = load_demo_check()
    build_full_evidence(tmp_path)
    # 最新日意图持多 0.01，known_positions 已清空 → 现场差分（无法解释差异）
    doc_path = tmp_path / "data" / "live" / f"expected-{MAGIC}.json"
    doc = json.loads(doc_path.read_text(encoding="utf-8"))
    doc["volume"] = 0.01
    doc_path.write_text(json.dumps(doc), encoding="utf-8")
    code, out = run_cli(mod, tmp_path, "--day", TRADING_DAYS[-1].isoformat())
    assert code == 1, out


# ------------------------------------------------ T59 跨组契约联测（R10 审核阻断）

def test_drill_archive_contract_real_producer(tmp_path):
    """C-3 联测：真实生产端（mt5live.commands）write_flatten+write_ack+
    archive_if_all_acked 产两轮归档（副本保留原名 flatten.ack-<magic>——
    **无 .json 扩展名**，commands.py:48/191-194）→ load_drill_rounds 必须
    读出两轮指令与全 magic ack，--accept 双全平判据必须通过。
    修复前（P1 断链）：drill-archive 只 glob *.json → acks=[] 恒失败
    （消费端旧夹具手写 ack-<magic>.json 恰好带扩展名，掩蔽了断链）。"""
    import shutil
    from datetime import timedelta

    from mt5live.commands import (
        archive_if_all_acked,
        write_ack,
        write_flatten,
    )
    from mt5live.monitor import parse_iso

    mod = load_demo_check()
    paths = build_full_evidence(tmp_path)
    add_extra_magic(paths, MAGIC_B)      # manifest 两 magic：全 magic ack 关联
    shutil.rmtree(paths["live"] / "drill-archive")   # 删手写夹具，只用真实产物
    live = str(paths["live"])
    magics = {MAGIC, MAGIC_B}
    cids = []
    for cmd_ts in (parse_iso(DRILL1_TS), parse_iso(DRILL2_TS)):
        cmd = write_flatten(live, reason="daily_loss_cap", now=cmd_ts)
        ack_ts = cmd_ts + timedelta(seconds=30)
        for m in sorted(magics):
            write_ack(live, m, cmd.id, closed=1, now=ack_ts)
        assert archive_if_all_acked(live, magics, now=ack_ts)
        cids.append(cmd.id)
    # 生产端真实产物形状钉死：ack 副本无扩展名（这是 P1 断链根因）
    drill1 = paths["live"] / "drill-archive" / cids[0]
    assert (drill1 / "flatten.json").exists()
    for m in sorted(magics):
        assert (drill1 / f"flatten.ack-{m}").exists()
        assert not (drill1 / f"flatten.ack-{m}.json").exists()
    rounds = mod.load_drill_rounds(live)
    assert sorted(rounds) == sorted(cids)
    for cid in cids:
        assert rounds[cid]["command"] is not None
        assert rounds[cid]["command_ts"] is not None
        assert {a["magic"] for a in rounds[cid]["acks"]} == magics
        assert all(a["id"] == cid for a in rounds[cid]["acks"])
    code, out = run_cli(mod, tmp_path, "--accept")
    assert code == 0, out
    assert "验收通过" in out


def test_deals_decision_bar_contract_real_format(tmp_path):
    """C-2 联测：deals 的 decision_bar 用生产端真实裸 bar 格式
    （iso_utc 带 +00:00，executor.py:471）、决策行/台账 decision_id 用真实
    runner 格式（SYMBOL|TF|bar naive 无时区，runner.py:267-268）——两侧
    字符串不相等（P2 断链根因），--accept 滑点样本与逐日归因必须仍命中
    （修复前：无滑点样本 + N 笔无对应决策，恒失败）。"""
    mod = load_demo_check()
    paths = build_full_evidence(tmp_path)
    # 夹具形状钉死（防再漂回"两侧同字符串"的掩蔽形态）
    first_fill = json.loads(
        paths["deals_log"].read_text(encoding="utf-8").splitlines()[0])
    assert first_fill["event"] == "fill_open"
    assert first_fill["decision_bar"] == BAR_SL          # 裸 bar，带 +00:00
    assert "|" not in first_fill["decision_bar"]
    runner_ln = next(ln for ln in paths["runner_a"].read_text(
        encoding="utf-8").splitlines() if "决策 " in ln and "bid=" in ln)
    did = runner_ln.split("决策 ", 1)[1].split("：", 1)[0]
    assert did == DEC_SL                                 # naive 无时区后缀
    assert first_fill["decision_bar"] != did             # 正是断链差异
    code, out = run_cli(mod, tmp_path, "--accept")
    assert code == 0, out
    assert "验收通过" in out
    assert "无滑点样本" not in out
    assert "无对应决策" not in out
    assert "4 样本：min" in out                          # 4 笔 fill_open 全命中


def test_accept_rejects_manifest_missing_schema_version(tmp_path):
    """P3：manifest 缺 schema_version 即拒绝（fail-closed——生产端写入器
    恒写该字段，读不到=契约不符；与存在且 ≠1 同样拒绝）。夹具形状钉死：
    C-1 恒写全部字段 schema_version/mode/magics/runs/account_hint。"""
    mod = load_demo_check()
    paths = build_full_evidence(tmp_path)
    doc = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    for key in ("schema_version", "mode", "magics", "runs", "account_hint"):
        assert key in doc, key + " 缺失（生产端恒写，夹具不得削弱）"
    del doc["schema_version"]
    paths["manifest"].write_text(json.dumps(doc), encoding="utf-8")
    code, out = run_cli(mod, tmp_path, "--accept")
    assert code == 1, out
    assert "schema_version" in out
    assert "验收通过" not in out


def test_accept_ignores_out_of_window_stale_evidence(tmp_path):
    """P4：窗外旧证据（D′ 演练期遗留）不得计入 --accept 覆盖——活动日按
    C-1 运行窗（WIN_START=2026-10-12 起）统一裁剪后，连续覆盖/逐日存活/
    点差样本覆盖/逐日严格 sweep 四个消费点都不得被窗外旧证据撑长，也不
    得反过来要求为窗外旧日补齐证据（2026-10-08 故意不补日报/全天心跳）。
    任一消费点泄漏（修复前：覆盖缺 10-09、10-08 缺逐小时心跳/日报）→
    --accept 误不通过。"""
    mod = load_demo_check()
    paths = build_full_evidence(tmp_path)
    stale = date(2026, 10, 8)            # 周四，窗起点 2026-10-12 之前
    with open(paths["alerts"], "a", encoding="utf-8") as f:
        f.write(f"2026-10-08T10:00:00+00:00\tRISK_REJECT\t"
                f"magic {MAGIC} {SYMBOL}|M15|2026-10-08T22:00:00："
                "forbidden_session: D′ 旧演练期遗留\n")
    with open(paths["risk_log"], "a", encoding="utf-8") as f:
        f.write(risk_line(stale, 10))
    with open(paths["recon_log"], "a", encoding="utf-8") as f:
        f.write(f"· 2026-10-08 10:00:41 UTC 对账一致（0 个持仓）\n")
    with open(paths["runner_b"], "a", encoding="utf-8") as f:
        f.write(decision_line(stale, 10, 45))
    code, out = run_cli(mod, tmp_path, "--accept")
    assert code == 0, out
    assert "验收通过" in out
    assert "2026-10-08" not in out       # 数据范围/逐日复核都不得再含窗外旧日
