"""T153/D67（第十六轮 H16-4；第十八轮施工）：开轮前彩排（rehearsal）。

D67 原文：**开轮前彩排——同一条命令在安慰剂输入上端到端跑通（含诊断
与临时台账写入），记录耗时；彩排报告是 A1 材料**（docs/blueprint.md
决策表；第十六轮 H16-4：R3 的 4 类机械故障都能在"平移日历或合成收益、
临时台账、同一条命令"的安慰剂上提前暴露）。第十六轮计划 T153 定义：

- `bt round rehearse R#` 用安慰剂输入跑同一条 gate 命令——安慰剂按
  **预写规则**把事件日历平移，或把收益替换为合成序列；
- 写**临时（影子）台账**；
- 输出每个阶段的通过情况与耗时；
- 彩排报告是之后每次 A1 的必备材料。

实现（本模块 + scripts/bt.py 的 `round rehearse` 子命令接线）：

- **安慰剂预写规则**（常量钉死，确定性）：D1 合成零漂移随机游走
  （种子 = sha256("rehearse:<R#>")，σ=0.005/日，T153 的"收益替换为
  合成序列"）；事件日历平移 = 6 周节奏（42 日）锚格 **+1 日平移**的
  合成 FOMC 日历（平移后公告日落周四，与真实 FOMC 公告日落点错开，
  T153 的"事件日历平移"）。
- **影子沙盒**：`data/rehearse/<R#>-<UTC时刻>/` 下自建迷你 git 仓库
  （轮次预注册字节复制 + 合成候选登记 + 合成卡——真实候选登记的数据
  源属真实输入，按 D67 用安慰剂替换，但 side_rule 等与预注册交叉
  核对的字段逐字一致）、安慰剂数据根、影子台账 ledger.sqlite——与
  正式 `data/research/ledger.sqlite` 物理分离；正式台账只以 sqlite
  **只读 URI** 读取 chain_meta 头快照做彩排前后的无污染断言，绝不
  打开写连接（防迁移副作用）。
- **同一条命令**：各阶段以子进程运行真实 `scripts/bt.py` CLI
  （round open → screen → gate → ledger verify），逐阶段计时、记
  退出码/输出尾部/台账足迹；gate 关卡在安慰剂上的**实质通过与否只
  报告**（安慰剂无效应，关卡不过属预期），机械完整（跑完、有 gates
  行、无超时/崩溃）才算阶段完成。
- **报告**：JSON（原始产物，随沙盒留 data/，不入库）+ 人读 md
  （`docs/research/rehearse-<R#>-<时刻>.md`，A1 材料，入库）。

研究纪律红线（机器保证并写进报告）：

- 纯离线合成——不连任何 MT5/终端（本模块不 import 任何 mt5live/
  mt5gw 路径，数据为内存生成后落 parquet）；
- 不跑真实候选收益（数据根是沙盒合成库；正式研究库/主库不读）；
- 不伪造批准、不触碰锁箱（G5 不彩排——一次性资源，彩排命令不传
  --approve、不调用任何 lockbox 写方法、不写正式台账任何行）；影子
  台账 rounds 行的 approved_by 是轮次预注册必填字段的**继承值**（沙盒
  内随真实 round open 代码路径写入，非彩排新增批准）；
- 不改 config/gates.toml（只读冻结门槛，与正式运行同一文件）。

沙盒清理（F-4）：缺省（CLI 不带 ``--keep-sandbox``）彩排**成功**后自动
清理沙盒目录 ``data/rehearse/<R#>-<时刻>/``（自含：repo/store/影子
台账/原始 JSON；只删该目录本身、绝不递归上级，护栏见 _cleanup_sandbox）；
人读 md 报告（A1 材料）保留。彩排失败/被拒时不清理（排查需要）；
``--keep-sandbox`` 一律保留沙盒供排查。

覆盖边界（F-5，如实声明；各阶段 argv 全量落报告可审计）：

- screen/gate 带 ``--allow-unbounded-data``：安慰剂合成库无"声明研究
  上界/数据来源"语义（X-03），豁免必要；真实运行**不带**此 flag——
  因此"裸库未声明上界被拒（X-03）"一类故障不在彩排暴露面内；
- 多候选轮 v1 只彩排**首候选**（candidates_all 全列于报告）：混合
  mode 轮次（如首候选 bar、次候选 event）只彩排首候选 mode 的命令
  形状，其余候选 mode 的 screen/gate 事件绑定与关卡集不在本次彩排
  面内；概率轮（P 线休眠 D69）只彩排开轮段。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from mt5data.store import _COLUMNS
from mt5quant.research import rounds as rnd

__all__ = [
    "REHEARSE_VERSION", "RehearseError", "placebo_bars", "placebo_fomc_doc",
    "run_rehearsal", "sandbox_tag",
]

REHEARSE_VERSION = "t153-v1"

# ------------------------------------------------ 安慰剂预写规则（T153/D67）
PLACEBO_SYMBOL = "XAUUSD"
PLACEBO_TIMEFRAME = "D1"
PLACEBO_START = "2024-01-01"       # 合成序列起点（日历日逐日一根 D1）
PLACEBO_BARS = 800                 # 缺省根数（≈2.2 年）
PLACEBO_SIGMA = 0.005              # 日收益波动（零漂移随机游走——零效应）
PLACEBO_FOMC_INTERVAL_DAYS = 42    # 合成日历节奏：6 周（FOMC 8 次/年量级）
PLACEBO_FOMC_SHIFT_DAYS = 1        # 日历平移：锚格 +1 日（错开真实公告日）
PLACEBO_EXPECTED_DELTA_P = 0.05    # 概率候选安慰剂 Δp（标定档 ≥0.03——
                                   # 开轮 D52 复核可在安慰剂上机械通过）
PLACEBO_TRIGGER_DAYS = 1_000_000   # 概率预筛夹具的触发日数（恒过 D52 下限）

# 彩排用的注册策略类（真实库内、已提交；不新写策略代码）
BAR_STRATEGY = "mt5quant.strategies.library.buy_hold:BuyHold"
BAR_GATES = "G0,G1,G2,G3,G4"
EVENT_STRATEGY = ("mt5quant.strategies.library.fomc_preannouncement:"
                  "PreFomcAnnouncementDrift")
EVENT_GATES = "V3"
EVENT_KIND = "fomc_calendar"

REHEARSAL_CARD_ID = "H901"         # 沙盒合成卡（不与真实卡目录共存）
_GIT_AUTHOR = ("zcode-rehearse", "rehearse@invalid")


class RehearseError(RuntimeError):
    """彩排无法进行（轮次已在正式台账开启/沙盒无法建立等）。"""


def sandbox_tag(rid: str, now: datetime | None = None) -> str:
    """沙盒目录名：<R#>-<UTC 时刻>（可区分、可事后整目录清理）。"""
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    return f"{rid}-{stamp}"


def placebo_seed(round_id: str) -> int:
    """安慰剂种子（确定性）：sha256("rehearse:<R#>") 前 8 字节。"""
    return int.from_bytes(
        hashlib.sha256(f"rehearse:{round_id}".encode("utf-8")).digest()[:8],
        "big")


def placebo_bars(round_id: str, n: int = PLACEBO_BARS) -> pd.DataFrame:
    """安慰剂 D1 K 线：零漂移随机游走（T153"收益替换为合成序列"）。

    日历日逐日一根（与 tests/test_fomc_calendar_engine 的合成事件轮次
    同形状）；open=前收（统一口径），high/low 包络 ±0.2%。"""
    times = pd.date_range(PLACEBO_START, periods=n, freq="D")
    rng = np.random.default_rng(placebo_seed(round_id))
    close = 100.0 * np.exp(np.cumsum(rng.normal(0.0, PLACEBO_SIGMA, n)))
    open_ = np.r_[close[0], close[:-1]]
    return pd.DataFrame({
        "time": times, "open": open_,
        "high": np.maximum(open_, close) * 1.002,
        "low": np.minimum(open_, close) * 0.998,
        "close": close, "tick_volume": 100, "spread": 0, "real_volume": 0,
    })[_COLUMNS]


def placebo_fomc_doc(bars: pd.DataFrame) -> dict:
    """安慰剂 FOMC 日历工件（T153"事件日历平移"）。

    预写规则：自合成序列起点 +30 日起，每 PLACEBO_FOMC_INTERVAL_DAYS
    天取一个锚格日，公告日 = 锚格 + PLACEBO_FOMC_SHIFT_DAYS（平移后
    落周四——与真实 FOMC 公告日（周三）错开，合成即安慰剂）。条目
    形状与冻结工件同构（tests/test_fomc_calendar_engine.meeting）；
    era_start 用引擎钉死值 FOMC_ERA_START（1994-09-27）。"""
    days = pd.DatetimeIndex(pd.to_datetime(bars["time"])).normalize()
    first = days[0] + pd.Timedelta(days=30)
    last = days[-1] - pd.Timedelta(days=7)
    meetings = []
    d = first
    while d <= last:
        iso = (d + pd.Timedelta(days=PLACEBO_FOMC_SHIFT_DAYS)) \
            .date().isoformat()
        meetings.append({
            "year": int(iso[:4]), "label": f"{iso} Meeting",
            "start_date": iso, "end_date": iso, "kind": "meeting",
            "qualifier": None, "source_page": "rehearse-placebo",
            "announcement_date": iso, "in_lm_era": True,
            "in_default_event_set": True})
        d = d + pd.Timedelta(days=PLACEBO_FOMC_INTERVAL_DAYS)
    return {
        "artifact": "rehearse-placebo", "purpose": "T153/D67 rehearsal placebo",
        "event_definition": {
            "era_start": "1994-09-27",
            "default_event_set": "kind=meeting with qualifier None",
            "announcement_day_rule": "last calendar day of the meeting"},
        "meetings": meetings}


# ------------------------------------------------------------ 正式台账只读

def _ro_connect(path: Path) -> sqlite3.Connection:
    """只读 URI 连接（mode=ro——绝不触发建表/迁移等写副作用）。"""
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def formal_chain_heads(path) -> dict | None:
    """正式台账 chain_meta 快照（表 → [last_rowid, row_count, head_hash]）。

    文件不存在 → None（无台账可保护，如实报告）。读取失败（损坏/占用）
    → RehearseError（fail-closed：无法证明不污染就不彩排）。"""
    p = Path(path)
    if not p.is_file():
        return None
    try:
        con = _ro_connect(p)
        try:
            rows = con.execute(
                "SELECT table_name, last_rowid, row_count, head_hash"
                " FROM chain_meta ORDER BY table_name").fetchall()
        finally:
            con.close()
    except sqlite3.Error as e:
        raise RehearseError(
            f"正式台账 {p} 无法只读快照（{e}）——无法证明彩排不污染，"
            "拒绝进行（fail-closed）") from e
    return {r[0]: [r[1], r[2], r[3]] for r in rows}


def formal_round_status(path, round_id: str) -> str | None:
    """正式台账里该轮次的 status（None = 未开启/无台账）。

    文件不存在 → None（无台账可查，与 formal_chain_heads 同口径）；
    读取失败（损坏/占用/无 rounds 表等 sqlite3.Error）→ RehearseError
    （F-1：读不到就按"无法确认轮次状态"处理、拒绝彩排——fail-closed，
    不得视为未开启而放行；formal_chain_heads 的头快照 fail-closed 兜底
    不变）。"""
    p = Path(path)
    if not p.is_file():
        return None
    try:
        con = _ro_connect(p)
        try:
            row = con.execute(
                "SELECT status FROM rounds WHERE round_id=?",
                (round_id,)).fetchone()
        finally:
            con.close()
    except sqlite3.Error as e:
        raise RehearseError(
            f"正式台账 {p} 无法只读查询轮次 {round_id} 的状态（{e}）——"
            "无法确认该轮是否已开启，拒绝彩排（fail-closed，F-1）") from e
    return row[0] if row else None


# ------------------------------------------------------------ 沙盒装夹具

def _candidate_toml(cid: str, round_obj, mode: str,
                    side_rule: str | None) -> str:
    """合成候选登记（安慰剂替代——真实候选登记的数据源是真实输入，
    彩排按 D67 用合成物替换；side_rule 与轮次 [evaluation] 逐字一致，
    使开轮的 T112 交叉核对走真实代码路径）。"""
    mech = ("event" if mode == "event"
            else "price_levels" if mode == "probability"
            else "flow_calendar")
    lines = [
        f'id = "{cid}"',
        'title = "彩排安慰剂候选（合成，T153/D67）"',
        f'mechanism_class = "{mech}"',
        'status = "registered"',
        f'symbols = ["{PLACEBO_SYMBOL}"]',
        'holding = "D1"',
        'mechanism = "彩排安慰剂（合成候选，不评估真实机制）"',
        "",
        "[literature]",
        'original = ["rehearsal placebo (T153/D67)"]',
        "replications = []",
        "rebuttals = []",
        'search_log = "rehearsal placebo"',
        'published_effect = "安慰剂（无效应）"',
        'expected_effect = "安慰剂（无效应）"',
    ]
    if mode == "probability":
        hyps = list(round_obj.hypotheses.get(cid) or ())
        if not hyps:
            return "\n".join(lines) + "\n"   # 无假设族 → 开轮拒绝（如实暴露）
        situations = sorted({h.split(":")[0] for h in hyps})
        lines += [
            "",
            "[prob]",
            f"situations = {json.dumps(situations)}",
            f"hypotheses = {json.dumps(hyps)}",
            "bracket = { sl_atr = 1.0, tp_atr = 1.0, horizon_bars = 4 }",
            f"level_grid = {{ {PLACEBO_SYMBOL} = 10.0 }}",
            "approach = { lookback_bars = 10, min_distance_atr = 0.5,"
            " touch_atr = 0.1 }",
            "breakout_atr = 0.5",
            "retest_window_bars = 3",
            f"expected_delta_p = {PLACEBO_EXPECTED_DELTA_P}",
        ]
        return "\n".join(lines) + "\n"
    lines += [
        "",
        "[declared]",
        "events_per_year = 8",
        "gross_return_per_event = 0.001",
        "window_bars = 1",
        "entry_hour_ny = 16",
        "exit_hour_ny = 16",
        "overnight_nights = 1.0",
        "expected_sharpe = 0.5",
    ]
    if side_rule:
        lines.append(f'side_rule = "{side_rule}"')
    return "\n".join(lines) + "\n"


def _card_toml(round_id: str, cid: str, mode: str) -> str:
    """合成卡（绑轮次/候选；事件模式与柱级模式同一形状，策略由 CLI 给出）。"""
    return f"""\
id = "{REHEARSAL_CARD_ID}"
version = 1
symbols = ["{PLACEBO_SYMBOL}"]
timeframe = "{PLACEBO_TIMEFRAME}"
session = "全时段"
phenomenon = "彩排安慰剂（{mode}）"
mechanism = "合成（T153/D67 彩排，不评估真实机制）"
should_fail_when = "—（彩排）"
falsified_if = "—（彩排）"
graveyard_checked = []
round = "{round_id}"
candidate = "{cid}"

[rules]
entry = "彩排合成信号（策略类事件窗/首根）"
exit = "数据末尾/信号出场"
stop = "无"

[params]

[expected]
trades_per_year = "8"
win_rate = "未登记"
payoff_ratio = "不适用"
"""


def _write_placebo_store(store: Path, round_id: str, bars_n: int
                         ) -> tuple[pd.DataFrame, Path, str]:
    """安慰剂数据根：K 线 + specs + 显式交易日历 + 平移 FOMC 工件。"""
    bars = placebo_bars(round_id, bars_n)
    sdir = store / "bars" / PLACEBO_SYMBOL / PLACEBO_TIMEFRAME
    sdir.mkdir(parents=True, exist_ok=True)
    bars.to_parquet(sdir / "data.parquet")
    (store / "specs").mkdir(exist_ok=True)
    (store / "specs" / "latest.json").write_text(json.dumps(
        {PLACEBO_SYMBOL: {"contract_size": 100.0, "volume_min": 0.01,
                          "volume_step": 0.01, "volume_max": 200.0}}),
        encoding="utf-8")
    days = pd.DatetimeIndex(pd.to_datetime(bars["time"])).normalize()
    cal_days = pd.date_range(days[0], days[-1] + pd.Timedelta(days=30),
                             freq="D")
    caldir = store / "calendars"
    caldir.mkdir(exist_ok=True)
    (caldir / f"{PLACEBO_SYMBOL}.json").write_text(json.dumps({
        "version": 1, "complete_through": str(cal_days[-1].date()),
        "sessions": [{"day": str(d.date()),
                      "close_time": f"{d.date()}T22:00:00"}
                     for d in cal_days]}), encoding="utf-8")
    doc = placebo_fomc_doc(bars)
    cal_path = store / "fomc_placebo.json"
    cal_path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    sha = hashlib.sha256(cal_path.read_bytes()).hexdigest()
    return bars, cal_path, sha


def _fomc_fixture_binding(round_obj, cal_path: Path, cal_sha: str,
                          days: pd.DatetimeIndex) -> dict | None:
    """F-3：事件候选的 F1 登记绑定载荷（precheck_source.calendar_sha256
    + calendar_coverage）。coverage 用与运行侧**同一构造器函数**
    （mt5quant.validation.events.fomc_calendar_events）在安慰剂日历上、
    以同式主段窗 [首根日, 末根日) 计算——按构造与 screen/gate 运行侧
    一致，使 F1 机械核对（CLI SHA ↔ 登记绑定 SHA、构造器 report ↔
    登记 calendar_coverage 逐字段）走真实路径，而非 no_registered_
    precheck 兼容路径。轮次无事件候选 → None。"""
    if not any((round_obj.evaluation.get(cid) or {}).get("mode") == "event"
               for cid in round_obj.candidates):
        return None
    from mt5quant.validation.events import fomc_calendar_events

    main_days = days[(days >= days[0]) & (days < days[-1])]
    _, rep = fomc_calendar_events(cal_path, cal_sha, main_days)
    return {"calendar_sha256": cal_sha,
            "calendar_coverage": {
                "era_start": rep["era_start"],
                "default_event_set": rep["default_event_set"],
                "window_events": rep["window_events"],
                "events_with_entry_anchor": rep["events"],
                "dropped_no_bar": rep["dropped_no_bar"],
                "dropped_no_anchor": rep["dropped_no_anchor"]}}


def _seed_shadow_checks(ledger_path: Path, round_obj, years: float,
                        candidates_dir: Path,
                        fomc_binding: dict | None = None) -> None:
    """影子台账夹具：每候选一条 pass 预筛（含覆盖基线；概率候选另带
    per_hypothesis 触发日数——使开轮的 D52 复核在安慰剂上机械可走）。

    F-3 载荷补齐（开轮机械核对的真实路径覆盖）：
    - 每行带 candidate_sha256（沙盒候选登记文件的当前字节摘要）——
      H-08"候选内容与预筛结论一致"绑定核对不再走"旧记录未绑定"兼容
      路径（改候选文件会被机器拒绝）；
    - 事件候选另带 precheck_source/calendar_coverage（见
      _fomc_fixture_binding）——F1 日历登记交叉核对不再标注
      no_registered_precheck。"""
    from mt5quant.research.ledger import Ledger

    key = f"{PLACEBO_SYMBOL}/{PLACEBO_TIMEFRAME}"
    with Ledger(ledger_path) as led:
        for cid in round_obj.candidates:
            checks: dict = {"power": {
                "years": years, "years_detail": {key: years},
                "sharpe_used": 0.5}}
            if (round_obj.evaluation.get(cid) or {}).get("mode") \
                    == "probability":
                checks["power_prob"] = {"per_hypothesis": {
                    h: {"passed": True,
                        "trigger_days": PLACEBO_TRIGGER_DAYS}
                    for h in (round_obj.hypotheses.get(cid) or ())}}
            result: dict = {"candidate_id": cid, "decision": "pass",
                            "note": "T153/D67 rehearsal placebo fixture",
                            "checks": checks}
            cand_file = Path(candidates_dir) / f"{cid}.toml"
            if cand_file.is_file():
                result["candidate_sha256"] = hashlib.sha256(
                    cand_file.read_bytes()).hexdigest()
            if fomc_binding and (round_obj.evaluation.get(cid) or {}) \
                    .get("mode") == "event":
                result["precheck_source"] = {
                    "stable_key": f"rehearse:{round_obj.id}:{cid}",
                    "calendar_sha256":
                        fomc_binding["calendar_sha256"]}
                result["calendar_coverage"] = \
                    fomc_binding["calendar_coverage"]
            led.record_candidate_check(cid, decision="pass", result=result)


def _cleanup_sandbox(sandbox: Path, sandbox_root: Path, tag: str) -> bool:
    """F-4：删除本次彩排的自含沙盒目录（缺省在彩排成功后调用）。

    防误删护栏：只删 sandbox_root 的**直接子目录**、目录名须等于本次
    生成的 ``<R#>-<时刻>`` tag、目录内须有本次夹具形状（repo/ 与
    store/ 子目录）——绝不递归上级、绝不删 sandbox_root 本身；任一
    护栏不过或删除失败 → 返回 False（保留沙盒，如实不报清理成功）。
    git 对象文件只读 → rmtree 回调先恢复可写再重试。"""
    def _rm_onexc(func, target, exc):
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            raise exc from None

    try:
        root = sandbox_root.resolve()
        target = sandbox.resolve()
        if (not tag or target == root or target.parent != root
                or sandbox.name != tag
                or not (target / "repo").is_dir()
                or not (target / "store").is_dir()):
            return False
        shutil.rmtree(target, onexc=_rm_onexc)
        return not target.exists()
    except OSError:
        return False


def _git_init_commit(repo: Path) -> None:
    """沙盒迷你 git 仓库：先卡后测/先预注册后开轮的真实检查需要
    committed_at + worktree_clean（cards.committed_at 对沙盒文件跑
    git——自建仓库让真实代码路径零旁路通过）。"""
    def git(*argv: str) -> None:
        proc = subprocess.run(
            ["git", *argv], cwd=str(repo), capture_output=True, text=True,
            timeout=60)
        if proc.returncode != 0:
            raise RehearseError(
                f"沙盒 git 操作失败（git {' '.join(argv)}）："
                f"{(proc.stderr or proc.stdout).strip()}")
    git("init", "-q")
    git("add", "-A")
    git("-c", f"user.name={_GIT_AUTHOR[0]}", "-c",
        f"user.email={_GIT_AUTHOR[1]}", "commit", "-qm",
        f"rehearsal fixture (T153/D67) [{REHEARSE_VERSION}]")


# ------------------------------------------------------------ 阶段执行

def _run_stage(name: str, argv: list[str], *, cwd: Path,
               timeout: float, stdout_tail: int = 40) -> dict:
    """跑一个阶段（真实 CLI 子进程），计时并截取输出尾部。"""
    started = time.perf_counter()
    timed_out = False
    out = err = ""
    rc: int | None = None
    try:
        proc = subprocess.run(
            [str(a) for a in argv], cwd=str(cwd), capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=timeout)
        rc, out, err = proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired as e:
        timed_out = True
        raw = e.stdout
        out = raw.decode("utf-8", "replace") if isinstance(raw, bytes) \
            else (raw or "")
        err = f"stage timeout after {timeout}s" \
              "（T153：运行时长/执行载体错配类机械故障——彩排暴露）"
    seconds = round(time.perf_counter() - started, 3)
    return {"stage": name, "argv": [str(a) for a in argv],
            "rc": rc, "timeout": timed_out, "seconds": seconds,
            "stdout_tail": "\n".join(out.strip().splitlines()[-stdout_tail:]),
            "stderr_tail": "\n".join(err.strip().splitlines()[-10:])}


def _shadow_gate_rows(ledger_path: Path) -> list[dict]:
    """影子台账里彩排卡的关卡结果（最新 run 的逐关卡通过情况）。"""
    if not ledger_path.is_file():
        return []
    con = _ro_connect(ledger_path)
    try:
        row = con.execute(
            "SELECT MAX(run_id) FROM runs WHERE card_id=?",
            (REHEARSAL_CARD_ID,)).fetchone()
        if not row or row[0] is None:
            return []
        return [{"gate": r[0], "passed": bool(r[1])}
                for r in con.execute(
                    "SELECT gate, passed FROM gates"
                    " WHERE run_id=? ORDER BY gate", (row[0],))]
    except sqlite3.Error:
        return []
    finally:
        con.close()


def _shadow_counts(ledger_path: Path) -> dict:
    if not ledger_path.is_file():
        return {}
    con = _ro_connect(ledger_path)
    try:
        out = {}
        for tbl in ("runs", "gates", "rounds", "trial_reservations",
                    "candidate_checks", "lockbox_uses"):
            try:
                out[tbl] = int(con.execute(
                    f"SELECT COUNT(*) FROM {tbl}").fetchone()[0])
            except sqlite3.Error:
                out[tbl] = None
        return out
    finally:
        con.close()


def _skipped(stage: dict) -> bool:
    return bool(stage.get("skipped"))


# ------------------------------------------------------------ 主流程

def run_rehearsal(round_path, *, bt_script, sandbox_root=None,
                  formal_ledger=None, report_dir=None,
                  bars: int = PLACEBO_BARS, stage_timeout: float = 900.0,
                  keep_sandbox: bool = False,
                  now: datetime | None = None) -> dict:
    """对一份轮次预注册跑彩排，返回报告 dict（同时落盘 JSON+md）。

    - round_path：research/rounds/R#.toml（真实文件，字节复制进沙盒；
      装载失败（RoundError）如实上抛——预注册本身不合法时无彩排可言）；
    - bt_script：scripts/bt.py 绝对路径（阶段以子进程跑真实 CLI）；
    - sandbox_root：缺省 data/rehearse（data/ 不入库，事后整目录可清理）；
    - formal_ledger：缺省 data/research/ledger.sqlite——**只读**快照对比，
      该轮已在正式台账开启时拒绝彩排（D67 是开轮**前**彩排；读不到
      轮次状态同样拒绝——fail-closed，F-1）；
    - report_dir：人读 md 落盘目录（缺省 docs/research，A1 材料）；
    - keep_sandbox：F-4——缺省 False，彩排**成功**（verdict=pass）后
      自动清理自含沙盒目录（md 报告保留；见 _cleanup_sandbox 护栏）；
      True 一律保留沙盒供排查。失败/被拒不清理。
    """
    round_path = Path(round_path)
    bt_script = Path(bt_script)
    sandbox_root = Path(sandbox_root) if sandbox_root is not None \
        else Path("data") / "rehearse"
    formal_ledger = Path(formal_ledger) if formal_ledger is not None \
        else Path("data") / "research" / "ledger.sqlite"
    report_dir = Path(report_dir) if report_dir is not None \
        else Path("docs") / "research"
    now = now or datetime.now(timezone.utc)

    r = rnd.load_round(round_path)                    # RoundError 上抛
    status = formal_round_status(formal_ledger, r.id)
    if status is not None:
        raise RehearseError(
            f"轮次 {r.id} 已在正式台账 {formal_ledger} 开启过"
            f"（status={status}）——D67 是**开轮前**彩排；已开启/已关闭的"
            "轮次不彩排，彩排报告属于该轮的 A1 材料")

    tag = sandbox_tag(r.id, now)
    sandbox = sandbox_root / tag
    repo = sandbox / "repo"
    store = sandbox / "store"
    shadow_ledger = sandbox / "ledger.sqlite"
    json_path = sandbox / "rehearsal-report.json"

    # ---- 夹具阶段（计时）：沙盒 + 迷你 git + 影子台账种子 ----
    t0 = time.perf_counter()
    (repo / "rounds").mkdir(parents=True)
    (repo / "candidates").mkdir()
    (repo / "cards").mkdir()
    round_bytes = round_path.read_bytes()
    (repo / "rounds" / round_path.name).write_bytes(round_bytes)
    first_cid = r.candidates[0]
    mode = (r.evaluation.get(first_cid) or {}).get("mode", "")
    for cid in r.candidates:
        ev = r.evaluation.get(cid) or {}
        (repo / "candidates" / f"{cid}.toml").write_text(
            _candidate_toml(cid, r, ev.get("mode", ""),
                            ev.get("side_rule")), encoding="utf-8")
    (repo / "cards" / f"{REHEARSAL_CARD_ID}.toml").write_text(
        _card_toml(r.id, first_cid, mode or "bar"), encoding="utf-8")
    p_bars, cal_path, cal_sha = _write_placebo_store(store, r.id, bars)
    years = round((len(p_bars) - 1) / 365.25, 6)
    days = pd.DatetimeIndex(pd.to_datetime(p_bars["time"])).normalize()
    fomc_binding = _fomc_fixture_binding(r, cal_path, cal_sha, days)
    _seed_shadow_checks(shadow_ledger, r, years, repo / "candidates",
                        fomc_binding)
    _git_init_commit(repo)
    fixture_seconds = round(time.perf_counter() - t0, 3)

    window = (str(days[0].date()), str(days[-1].date()))
    is_event = mode == "event"
    is_prob = mode == "probability"
    strategy = EVENT_STRATEGY if is_event else BAR_STRATEGY
    gates_arg = EVENT_GATES if is_event else BAR_GATES
    event_binding = (["--event-kind", EVENT_KIND,
                      "--fomc-calendar", str(cal_path),
                      "--fomc-calendar-sha256", cal_sha,
                      "--event-days-start", window[0],
                      "--event-days-end", window[1]]
                     if is_event else [])

    def cli(*extra: str) -> list[str]:
        return [sys.executable or "python", str(bt_script), *extra]

    actor = f"bt-rehearse-{r.id}"
    base = ["--card", REHEARSAL_CARD_ID, "--allow-unbounded-data",
            "--cards-dir", str(repo / "cards"), "--data-root", str(store),
            "--ledger", str(shadow_ledger), "--strategy", strategy,
            "--costs", "zero", "--volume-lots", "0.01", "--actor", actor]

    stages: list[dict] = []
    heads_before = formal_chain_heads(formal_ledger)

    def add(name: str, argv: list[str,], *, timeout: float = stage_timeout
            ) -> dict:
        stage = _run_stage(name, argv, cwd=sandbox, timeout=timeout)
        stages.append(stage)
        return stage

    def add_skip(name: str, why: str) -> dict:
        stage = {"stage": name, "argv": [], "rc": None, "timeout": False,
                 "seconds": 0.0, "stdout_tail": "", "stderr_tail": "",
                 "skipped": why}
        stages.append(stage)
        return stage

    # ---- 阶段 1：round open（影子台账；真实 CLI 与全部前置核对）----
    open_stage = add(
        "round_open",
        cli("round", "open", r.id, "--rounds-dir", str(repo / "rounds"),
            "--ledger", str(shadow_ledger)))
    round_open_ok = open_stage["rc"] == 0 and not open_stage["timeout"]

    # ---- 阶段 2/3：screen + gate（概率模式 v1 不彩排——P 线休眠 D69）----
    screen_stage = gate_stage = None
    if round_open_ok and not is_prob:
        screen_stage = add("screen", cli("screen", *base, *event_binding))
        gate_stage = add("gate", cli("gate", *base, "--gates", gates_arg,
                                     "--params", "{}", *event_binding))
    elif round_open_ok:
        screen_stage = add_skip(
            "screen", "概率模式轮次（evaluation mode=probability）的"
                      " fit/prob test 链不在彩排 v1 范围——P 线休眠"
                      "（D69）；重启 P 线时扩展本命令")
        gate_stage = add_skip("gate", "同上（概率模式 v1 不彩排）")

    # ---- 阶段 4：ledger verify（影子链完整性——同一命令可复核）----
    verify_stage = None
    if round_open_ok:
        verify_stage = add("ledger_verify",
                           cli("ledger", "verify",
                               "--ledger", str(shadow_ledger)),
                           timeout=120.0)

    # ---- 机械完成判定 + 关卡/台账足迹 ----
    gate_rows = _shadow_gate_rows(shadow_ledger)
    gate_completed = (gate_stage is not None and not _skipped(gate_stage)
                      and not gate_stage["timeout"]
                      and gate_stage["rc"] in (0, 1) and bool(gate_rows))
    screen_completed = (screen_stage is not None
                        and not _skipped(screen_stage)
                        and not screen_stage["timeout"]
                        and screen_stage["rc"] == 0)
    verify_ok = (verify_stage is not None and not verify_stage["timeout"]
                 and verify_stage["rc"] == 0)
    stage_status = {
        "fixture": "pass",
        "round_open": "pass" if round_open_ok else "fail",
        "screen": ("pass" if screen_completed else
                   "skipped" if screen_stage is not None
                   and _skipped(screen_stage) else
                   "fail" if round_open_ok else "not_run"),
        "gate": ("pass" if gate_completed else
                 "skipped" if gate_stage is not None
                 and _skipped(gate_stage) else
                 "fail" if round_open_ok else "not_run"),
        "ledger_verify": ("pass" if verify_ok else
                          "not_run" if verify_stage is None else "fail"),
    }
    # 彩排判定：夹具 + 开轮 + （非概率轮的）screen/gate 机械完整 + 影子
    # 链 verify 通过；关卡实质通过与否不进判定（安慰剂无效应——只报告）。
    verdict = "pass" if (
        round_open_ok and verify_ok
        and (is_prob or (screen_completed and gate_completed))
    ) else "fail"

    heads_after = formal_chain_heads(formal_ledger)
    pollution = {
        "formal_ledger": str(formal_ledger),
        "shadow_ledger": str(shadow_ledger),
        "heads_before": heads_before,
        "heads_after": heads_after,
        "unchanged": heads_before == heads_after,
        "note": "正式台账只以 sqlite mode=ro 读取（无写连接、无迁移）；"
                "彩排全部写入落影子台账",
    }

    report = {
        "version": REHEARSE_VERSION,
        "decision": "D67（开轮前彩排）+ 第十六轮 T153",
        "round_id": r.id,
        "round_toml_sha256": hashlib.sha256(round_bytes).hexdigest(),
        "round_path": str(round_path),
        "rehearsed_at_utc": now.isoformat(),
        "rehearsed_mode": mode or "bar",
        "rehearsed_candidate": first_cid,
        "candidates_all": list(r.candidates),
        "placebo": {
            "rule": "D1 零漂移随机游走（收益替换为合成序列）+ 6 周节奏"
                    "锚格 +1 日平移的合成 FOMC 日历（事件日历平移）",
            "symbol": PLACEBO_SYMBOL, "timeframe": PLACEBO_TIMEFRAME,
            "bars": int(len(p_bars)), "sigma_daily": PLACEBO_SIGMA,
            "seed_source": f"sha256('rehearse:{r.id}')",
            "fomc_interval_days": PLACEBO_FOMC_INTERVAL_DAYS,
            "fomc_shift_days": PLACEBO_FOMC_SHIFT_DAYS,
            "fomc_sha256": cal_sha,
            "event_window": list(window) if is_event else None,
            "costs": "zero（--costs zero，安慰剂成本口径）",
        },
        "sandbox": str(sandbox),
        "fixture_seconds": fixture_seconds,
        "stages": stages,
        "stage_status": stage_status,
        "gate_rows": gate_rows,
        "shadow_ledger_counts": _shadow_counts(shadow_ledger),
        "pollution": pollution,
        "red_lines": {
            "offline_no_mt5": "纯离线合成（无 MT5/终端连接；数据为内存"
                              "生成 parquet）",
            "no_real_candidate_returns": "数据根为沙盒合成库，不读研究库/"
                                         "主库，不跑真实候选收益",
            "no_approved_by": "不彩排 G5/锁箱（不传 --approve、不产生任何"
                              "批准动作、不写正式台账任何行、不写任何 "
                              "lockbox 行）；影子台账 rounds 行的 "
                              "approved_by 是预注册必填字段的继承值"
                              "（沙盒内随真实 round open 代码路径写入，"
                              "非彩排新增批准）",
            "gates_toml_readonly": "config/gates.toml 只读（不修改）",
        },
        "verdict": verdict,
        # F-4：缺省彩排成功后自动清理沙盒（keep_sandbox=True 保留）；
        # 报告 md（A1 材料）不受影响
        "sandbox_cleaned": verdict == "pass" and not keep_sandbox,
    }

    def _dump() -> None:
        json_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8")

    def _render() -> None:
        md_path.write_text(
            _render_md(report, md_name,
                       repo_root=bt_script.resolve().parents[1]),
            encoding="utf-8")

    _dump()
    report_dir.mkdir(parents=True, exist_ok=True)
    md_name = f"rehearse-{r.id}-{now.strftime('%Y%m%d-%H%M%S')}.md"
    md_path = report_dir / md_name
    _render()
    report["report_md"] = str(md_path)
    report["report_json"] = str(json_path)
    _dump()
    if report["sandbox_cleaned"]:
        # F-4：只删本次自含沙盒目录（护栏见 _cleanup_sandbox）；原始
        # JSON 随沙盒删除属预期（md 已含结论与计时；排查保留用
        # --keep-sandbox）。护栏不过/删除失败 → 如实改记并重写报告。
        if not _cleanup_sandbox(sandbox, sandbox_root, tag):
            report["sandbox_cleaned"] = False
            _render()
            _dump()
    return report


def _display(path_text: str, repo_root: Path) -> str:
    """md 里的路径显示：仓库内 → 仓库相对（卫生测试禁本机用户目录的
    绝对路径——docs/research 彩排报告是入库文件）；仓库外 → 原样。"""
    try:
        return (repo_root / path_text).resolve().relative_to(
            repo_root.resolve()).as_posix()
    except (ValueError, OSError):
        return path_text


def _fmt_stage(stage: dict) -> str:
    if _skipped(stage):
        return f"| {stage['stage']} | 跳过 | 0 | —（{stage['skipped']}） |"
    if stage["timeout"]:
        return f"| {stage['stage']} | 超时 | {stage['seconds']:.3f} | ✗ |"
    outcome = "✓" if stage["rc"] == 0 else "✗"
    if stage["stage"] == "gate" and stage["rc"] == 1:
        outcome = "✓（机械完整；关卡实质结果见下表）"
    return f"| {stage['stage']} | rc={stage['rc']} | " \
           f"{stage['seconds']:.3f} | {outcome} |"


def _render_md(report: dict, md_name: str, *, repo_root: Path) -> str:
    """人读彩排报告（A1 材料：阶段计时表 + 无污染断言 + 红线声明）。

    入库文件的路径一律仓库相对（卫生测试禁本机绝对路径——tests/
    test_repo_hygiene 的 local_path 模式）。"""
    p = report["placebo"]
    sandbox_disp = _display(report["sandbox"], repo_root)
    round_disp = _display(report["round_path"], repo_root)
    if report.get("sandbox_cleaned"):
        sandbox_note = (f"沙盒已按缺省自动清理（原路径 `{sandbox_disp}`，"
                        "原始 JSON/影子台账随之删除）——排查需保留沙盒时"
                        "下次彩排加 --keep-sandbox")
    else:
        sandbox_note = (f"原始 JSON/影子台账：`{sandbox_disp}`（保留供"
                        "排查，事后删除该目录即清理）")
    lines = [
        f"# 彩排报告 {report['round_id']}（T153/D67，开轮前）",
        "",
        f"- 彩排时刻：{report['rehearsed_at_utc']}（版本 "
        f"{report['version']}，本报告 {md_name}；{sandbox_note}）",
        f"- 预注册：`{round_disp}`（toml_sha256="
        f"{report['round_toml_sha256'][:12]}…）；彩排口径：候选 "
        f"{report['rehearsed_candidate']}（mode="
        f"{report['rehearsed_mode']}）；全部候选 "
        f"{report['candidates_all']}（多候选轮次 v1 只彩排首候选口径）",
        f"- 安慰剂规则（预写、确定性）：{p['bars']} 根 {p['symbol']}/"
        f"{p['timeframe']} 零漂移随机游走（σ={p['sigma_daily']}/日，种子="
        f"{p['seed_source']}）；事件日历 = {p['fomc_interval_days']} 日"
        f"节奏锚格 +{p['fomc_shift_days']} 日平移（sha256="
        f"{str(p['fomc_sha256'])[:12]}…）；成本 {p['costs']}",
        "",
        "## 阶段（同一条命令，逐阶段计时）",
        "",
        "| 阶段 | 退出码 | 耗时（秒） | 机械结果 |",
        "| --- | --- | ---: | --- |",
        f"| fixture（沙盒+影子台账+迷你 git） | — | "
        f"{report['fixture_seconds']:.3f} | ✓ |",
    ]
    lines += [_fmt_stage(s) for s in report["stages"]]
    if report["gate_rows"]:
        lines += [
            "",
            "## 关卡结果（安慰剂上只报告、不进彩排判定——无效应下关卡"
            "不过属预期）",
            "",
            "| 关卡 | passed |",
            "| --- | --- |",
        ]
        lines += [f"| {g['gate']} | {'✓' if g['passed'] else '✗'} |"
                  for g in report["gate_rows"]]
    pl = report["pollution"]
    lines += [
        "",
        "## 台账纪律（无污染断言）",
        "",
        f"- 影子台账：`{_display(pl['shadow_ledger'], repo_root)}`"
        "（沙盒内，独立于正式台账）；影子行计数："
        + json.dumps(report["shadow_ledger_counts"], ensure_ascii=False),
        f"- 正式台账 `{_display(pl['formal_ledger'], repo_root)}`："
        "彩排前后 chain_meta 头快照"
        f"**{'一致' if pl['unchanged'] else '不一致（污染！）'}**"
        "（只读连接，无写、无迁移）",
        "",
        "## 研究纪律红线",
        "",
        f"- {report['red_lines']['offline_no_mt5']}；"
        f"{report['red_lines']['no_real_candidate_returns']}；"
        f"{report['red_lines']['no_approved_by']}；"
        f"{report['red_lines']['gates_toml_readonly']}。",
        "",
        f"## 结论：**{report['verdict']}**",
        "",
        ("端到端机械跑通；本报告按 D67 作为该轮 A1 材料附件。"
         if report["verdict"] == "pass"
         else "存在机械未跑通的阶段——开轮前先处置（见阶段表与"
         "rehearsal-report.json 的输出尾部）。"),
        "",
    ]
    return "\n".join(lines)
