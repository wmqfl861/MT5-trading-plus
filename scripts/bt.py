"""bt：研究命令行（P4 试验台账与假设卡）。

用法：
    .venv/Scripts/python.exe scripts/bt.py card check research/cards/H001.toml
    .venv/Scripts/python.exe scripts/bt.py candidate check C001
    .venv/Scripts/python.exe scripts/bt.py screen --card H001 \
        --strategy mt5quant.strategies.library.buy_hold:BuyHold
    .venv/Scripts/python.exe scripts/bt.py ledger stats --card H001
    .venv/Scripts/python.exe scripts/bt.py gate --card H001 \
        --strategy mt5quant.strategies.library.buy_hold:BuyHold --gates G0,G1,G2,G3,G4

约定：
- 候选预筛（T62/D31，铁律 17/18）：bt candidate check 读附录 C 候选登记
  → precheck 五条件（文献/持仓 ≥H1/事前成本倍数 ≥3/检验力 ≥50%/机制空缺）
  → 写 research/candidates/C###.precheck.json 并记入台账 candidate_checks
  表（带哈希链）；退出码 pass=0 / fail=1 / waiting_data=2。研究年数按
  研究库品种×周期实际覆盖计算（不用常数），检验力函数与 round11 探针
  逐位一致（Lo 2002 近似）。
- 撤回与标定样品（T61/D30）：research/candidates/ 候选登记里
  status=withdrawn 的卡（source_card 指向卡 id，如 H002-v2/H003-v2/
  H006-v2）拒绝 screen/gate；status=specimen 的标定样品卡（H001–H007）
  必须显式 --specimen 运行且台账 actor=calibration（不进入研究轮次
  统计），--specimen 用于非样品卡 → 拒绝（cards.candidate_statuses_by_card）。
- 先卡后测：卡文件必须已提交到 git、工作区干净（git status --porcelain 为空，
  探针 P6），且最后提交时间早于该卡（id+version，按卡文件判定）在台账中的
  首次运行（cards.ensure_card_precedes_run）；未提交/脏文件/事后改动 →
  拒绝运行。卡上绑定了 strategy 时与 --strategy 不一致 → 拒绝
  （cards.ensure_strategy_matches，T21d）；旧卡（无 strategy 字段）沿用
  --strategy 并打印一次迁移提示。
- screen 在卡的参数网格 × 卡的 symbols 上跑 A 层（engine_a.run_backtest），
  逐组合写入台账；抛异常的组合也记一行（ok=false + 错误摘要），有失败则
  退出码 1。数据经 mt5data.lockbox.research_bars（锁箱语义）。
- gate：在卡的主品种上跑一次基准运行记入 runs，然后逐关卡评估并把结果写
  gates 表；任一关卡未过 → 退出码 1。门槛文件 config/gates.toml 的 sha256
  与冻结值不符 → 拒绝运行（不产生任何 runs）。G5（锁箱一次性）必须显式
  --approve。
- 参数选择（T21e/M5）：--params 缺省或 "auto" 时按卡 selection_rule（附录 A
  枚举：max_net_profit / max_sharpe）从同 data_sha256 + cost_model_version
  的 screen 运行里自动选参，候选数与选中 run_id 写进关卡报告与 gates 表
  details（params_selection）；显式 --params 记 selection="manual"。
- G5 编排（T21a 协议）：_g5_orchestrate = 参照重建（G3 拼接的样本外收益，
  只读研究段）→ begin_lockbox（读锁箱数据**之前**写 started）→
  load_lockbox_window（cutoff 前卡策略声明的 lookback_bars 根起步）→
  回测（交易与收益只计 cutoff 后）→ run_g5（单侧判定，写终态 done）。
- G4（T21c 接线）：n_trials 与试验夏普按 (strategy, params, symbol,
  timeframe, data_sha256, cost_model_version) 在谱系内去重
  （_dedup_trial_sharpes，同参 screen+gate 只计 1）；蒙特卡洛回撤块长 =
  一个交易日的 bar 数（block_len）。
- contract_size 只认 {data-root}/specs/latest.json（与 run_backtest 同规：
  缺则报错，绝不静默按 1 折算）。
- 定手数（T23/M2）：--risk-per-trade 缺省 0.005（按风险定手数，与
  run_backtest 同一 resolve_lots_mode 语义：--volume-lots 显式给出或
  --risk-per-trade 0 → 固定手数；两者同时显式且风险>0 → 拒绝）。风险
  定手数需要各品种快照 volume_min/step/max（缺则装配期报错，不产生
  runs）；定手数方式随 metrics（summary["sizing"]）落台账，G2–G5 的
  一切复算经 BacktestSpec.sizing 沿用同一口径。
- 禁入窗（T20）：screen/gate 默认加载 config/sessions.toml（与 run_backtest、
  实盘风控共用），基准运行与锁箱运行按禁入窗推迟开仓；其 sha256 随
  cost_model_version 写进台账。
- gates_hash：config/gates.toml 存在时取 vgate.gates_sha256（LF 归一，
  与 load_gates 同口径——探针 P7：裸字节哈希在 CRLF 检出下两侧不一致），
  否则空串（T12 起强制核对）。
- 台账缺省 {data-root}/research/ledger.sqlite（data/ 不入库，本地持久）。
- 研究库缺省（T27/H3 + T38/H4/D24）：**--data-root 缺省即研究库
  data_research**（scripts/export_research_store.py 导出的隔离副本：只含
  time < cutoff 的 K 线 + 成本缓存文件，无 tick；研究库无锁箱段，锁箱语义
  自然满足；--research-store 保留为兼容别名）。缺省时 data_research 不存在
  → 报错指路 export_research_store，绝不静默回退主库。读主库须显式
  --main-store（data_root=data）。G5 需读锁箱段，只能经 --main-store +
  --approve 由管理员跑——研究库模式与自定义 --data-root 一律拒绝
  （R6/C6：主库校验对所有数据根模式生效）。**--ops-ack 不能用于
  screen/gate**（R6/C6）：运维豁免仅供 reconcile_ab/measure 类运维命令
  （那里的台账通道记 actor=ops），不能参与研究关卡与试验统计。
  台账缺省**不**跟随切换，仍落 data/research/ledger.sqlite——permissions.md
  的 ACL 里研究库对研究账户只读，可写的台账目录是 data\\research。
- 代码指纹（T37/H3/M6，D23/D27）：screen/gate 运行前过
  cards.ensure_strategy_clean（策略类所在模块 + 其 import 的
  strategies/library 内模块必须已提交且工作区干净），指纹
  （strategy_sha）随每次运行落 runs 表并参与去重 N 的七元组键；
  同一卡版本首跑登记指纹，之后代码指纹不同 → 拒绝运行（提示新建
  下一版本卡）；`bt ledger verify` 校验 runs/gates/lockbox_uses 三条
  哈希链 + chain_meta 快照（R6/C5：篡改/删尾/未登记写入逐项报出，
  台账不完整时一切写入 fail-closed 抛 LedgerTamperedError），链头由
  scripts/ledger_anchor.py 每日锚定进 git（docs/ops/ledger-anchors.md，
  链式锚 + --verify 校验模式，R6/C5）。
"""
import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))   # 复用 run_backtest 的装配

from mt5data.store import TIMEFRAME_SECONDS  # noqa: E402
from mt5quant.backtest.engine_a import ENGINE_VERSION, run_backtest  # noqa: E402
from mt5quant.backtest.report import (  # noqa: E402
    bars_per_year, current_git_commit, data_sha256, observed_bars_per_year,
    summarize,
)
from mt5quant.research import cards  # noqa: E402
from mt5quant.research import candidates as cand  # noqa: E402
from mt5quant.research.ledger import Ledger, LedgerTamperedError  # noqa: E402
from mt5quant.validation import gates as vgate  # noqa: E402
from mt5quant.validation import lookahead as vlook  # noqa: E402
from mt5quant.validation import robustness as vrob  # noqa: E402
from mt5quant.validation import stats as vstats  # noqa: E402
from mt5quant.validation import walkforward as vwf  # noqa: E402
from mt5quant.validation.robustness import BacktestSpec  # noqa: E402
from run_backtest import (  # noqa: E402
    SESSIONS_PATH, ZeroCosts, build_costs, classify_store, entry_policy_for,
    load_series, load_strategy, resolve_contract_size, resolve_data_root,
    resolve_lots_mode, sessions_sha256,
)
from mt5quant.sizing import load_volume_limits  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
# T37/D23：策略代码指纹/干净检查的 git 根。从 mt5quant 包位置推导而非
# REPO_ROOT——测试会 monkeypatch bt.REPO_ROOT 重定向 gates 配置（P7 的
# CRLF 用例），而策略文件永远住在真正的仓库里，git 交互必须指向它。
STRATEGY_FP_ROOT = Path(cards.__file__).resolve().parents[3]


def _ledger_default(args) -> Path:
    """台账缺省路径（T27/T38）：研究库模式（T38/D24 起为**缺省**）下固定
    data/research/ledger.sqlite——研究库对研究账户只读，可写台账目录是
    data\\research（permissions.md），缺省切换不改这一语义；--main-store
    或显式 --data-root 时为 {data-root}/research/ledger.sqlite（主库时
    与固定路径相同）。显式 --ledger 永远优先。"""
    if args.ledger is not None:
        return Path(args.ledger)
    data_root = resolve_data_root(
        args.data_root, getattr(args, "research_store", False),
        getattr(args, "main_store", False))
    if classify_store(data_root) == "research":
        return Path("data") / "research" / "ledger.sqlite"
    return Path(data_root) / "research" / "ledger.sqlite"


def _resolve_store(args) -> tuple[str, str]:
    """T38/D24：解析数据根并执行守卫，返回 (data_root, store_kind)。

    - 主库（data 字面/仓库内路径）未给 --main-store → ValueError（缺省只读
      研究库；主库含完整锁箱段）；
    - 研究库（缺省指向）不存在 → ValueError 指路 export_research_store，
      绝不静默回退主库；
    - 调用方打印并退出 1（不产生任何 runs / lockbox_uses）。
    """
    data_root = resolve_data_root(
        args.data_root, getattr(args, "research_store", False),
        getattr(args, "main_store", False))
    kind = classify_store(data_root)
    if kind == "main" and not getattr(args, "main_store", False):
        raise ValueError(
            f"读主库 {data_root} 须显式 --main-store（T38/D24：bt 缺省只读研究库"
            " data_research；主库含完整锁箱段，仅管理员可读，读锁箱期另需"
            " --ops-ack，G5 也只能经 --main-store 跑）")
    if kind == "research" and not os.path.isdir(data_root):
        raise ValueError(
            f"研究库 {data_root} 不存在——T38/D24 起缺省数据根是研究库"
            " data_research；请管理员在主库侧跑 scripts/export_research_store.py"
            " 导出（只含 time < cutoff 的 K 线 + 成本缓存，无 tick），或显式"
            " --data-root 指定数据目录；绝不静默回退主库")
    return data_root, kind


def _reject_ops_ack(args) -> bool:
    """R6/C6：--ops-ack 不能用于研究命令（screen/gate）。

    第六轮审核 C6：--ops-ack 曾被 screen/gate 接受，直接读到含锁箱段的
    行情照常跑关卡登记（actor=ops 只是标签）——运维豁免由此进入研究关卡
    与试验统计，锁箱隔离被旁路。运维测量锁箱期数据走独立入口
    （scripts/reconcile_ab.py 已有 --ops-ack + actor=ops 通道），
    研究命令一律拒绝。
    """
    if getattr(args, "ops_ack", False):
        print("✗ --ops-ack 不能用于研究命令 screen/gate（R6/C6）：运维豁免"
              "仅供 reconcile_ab/measure 类运维命令读取锁箱期数据，不能"
              "参与研究关卡与试验统计；运维测量请走 scripts/reconcile_ab.py"
              "（actor=ops 通道）")
        return True
    return False

# 参数选择规则枚举（附录 A v2；卡定义时 bt card check 校验，非法值拒绝）
_SELECTION_METRICS = {
    "max_net_profit": "net_profit",
    "max_sharpe": "sharpe",
}


def _gates_hash(path=None) -> str:
    """config/gates.toml 的 sha256（T12 冻结后与运行前核对）；缺文件 → 空串。

    T21e/P7：经 vgate.gates_sha256（LF 归一）——与 bt gate（load_gates）同
    一口径；裸字节哈希在 CRLF 检出下会得到另一个值。
    """
    p = Path(path) if path is not None else REPO_ROOT / "config" / "gates.toml"
    if not p.is_file():
        return ""
    return vgate.gates_sha256(p)


def _card_path(value: str, cards_dir: Path) -> Path:
    """--card 接受 id（H012）或卡文件路径（.toml 结尾）。"""
    if value.lower().endswith(".toml"):
        return Path(value)
    return cards_dir / f"{value}.toml"


def _candidate_statuses(cards_dir: Path) -> dict[str, str]:
    """T61/D30：候选登记表（research/candidates/，--cards-dir 的兄弟目录）。"""
    return cards.candidate_statuses_by_card(Path(cards_dir).parent / "candidates")


def _enforce_candidate_status(args, card) -> str | None:
    """T61/D30：按候选登记约束卡的运行，返回本次运行的台账 actor。

    - status=withdrawn（撤回卡，如 H002-v2/H003-v2/H006-v2）→ 拒绝运行：
      打印原因（含 withdrawn）并返回 None——撤回的卡不再产生任何 runs；
    - status=specimen（标定样品卡，H001–H007）→ 必须 --specimen 才运行，
      且 actor 强制 calibration（蓝图 v4 §6.1：样品只用于校准关卡/成本/
      引擎/实盘链路，不作研究候选，不进入研究轮次统计）；
    - --specimen 用于非样品卡 → 拒绝（标定通道不能给研究卡统计逃逸）；
    - 无登记（普通研究卡、不带 --specimen）→ 原样返回 --actor。
    """
    status = _candidate_statuses(Path(args.cards_dir)).get(card.id)
    if status == cards.WITHDRAWN:
        print(f"✗ 卡 {card.id} 已撤回（status=withdrawn，T61/D30）——候选"
              f"登记 research/candidates/ 记录了撤回理由，撤回的卡不再"
              "运行；同谱系新版本须作为新候选登记、过预筛并列入轮次"
              "（蓝图 v4 §6.2）")
        return None
    if status == cards.SPECIMEN:
        if not args.specimen:
            print(f"✗ 卡 {card.id} 是标定样品（status=specimen，T61/D30）"
                  "——用于校准关卡/成本/引擎/实盘链路，不作研究候选；"
                  "运行必须显式 --specimen（台账记 actor=calibration，"
                  "不进入研究轮次统计）")
            return None
        print(f"— 标定样品运行（T61/D30）：{card.id}，台账 actor=calibration，"
              "不进入研究轮次统计")
        return "calibration"
    if args.specimen:
        print(f"✗ --specimen 只用于 status=specimen 的标定样品卡（T61/D30）"
              f"；卡 {card.id} 不是标定样品——标定通道（actor=calibration，"
              "不进研究统计）不能用于研究卡")
        return None
    return args.actor


def _check_card_lineage(card, source: str) -> str | None:
    """selection_rule 枚举校验（M5：非法值定义时拒）；返回错误信息或 None。"""
    if card.selection_rule not in _SELECTION_METRICS:
        return (f"{source}：selection_rule {card.selection_rule!r} 不在支持"
                f"枚举 {sorted(_SELECTION_METRICS)} 内（附录 A v2；"
                "bt gate --params auto 按该规则自动选参）")
    return None


def _select_params_auto(ledger, card, *, symbol: str, data_sha256: str,
                        cost_model_version: str) -> dict:
    """M5：按卡 selection_rule 从同 data_sha256 + cost_model_version 的
    screen 运行里选参（返回选择信息，含选中的 params 与 run_id）。

    候选 = 该卡在该 symbol/timeframe/数据哈希/成本版本下 metrics ok 且
    规则指标有值的运行；并列取 run_id 最小（稳定）。非法规则 / 无候选 →
    ValueError（调用方在基准运行之前拒绝，不产生任何 runs）。
    """
    rule = card.selection_rule
    if rule not in _SELECTION_METRICS:
        raise ValueError(_check_card_lineage(card, f"卡 {card.id}"))
    metric = _SELECTION_METRICS[rule]
    rows = ledger.conn.execute(
        "SELECT run_id, params_json, metrics_json FROM runs"
        " WHERE card_id=? AND symbol=? AND timeframe=? AND data_sha256=?"
        " AND cost_model_version=? ORDER BY run_id",
        (card.id, symbol, card.timeframe, data_sha256,
         cost_model_version)).fetchall()
    best = None          # (指标值, run_id, params)
    n_candidates = 0
    for r in rows:
        payload = json.loads(r["metrics_json"])
        if not payload.get("ok"):
            continue
        value = (payload.get("metrics") or {}).get(metric)
        if value is None:
            continue
        n_candidates += 1
        if best is None or float(value) > best[0]:
            best = (float(value), int(r["run_id"]),
                    json.loads(r["params_json"]))
    if best is None:
        raise ValueError(
            f"自动选参（--params auto）找不到候选：卡 {card.id} 在 {symbol} "
            f"{card.timeframe}、data_sha256={data_sha256[:12]}…、成本版本 "
            f"{cost_model_version} 下没有可用的 screen 运行——先跑 bt screen")
    return {"selection": "auto", "rule": rule, "metric": metric,
            "candidates": n_candidates, "selected_run_id": best[1],
            "selected_value": best[0], "params": best[2]}


def _dedup_trial_sharpes(ledger, card_id: str,
                         bars_per_year: float) -> tuple[int, list[float]]:
    """T21c/M4：谱系（cards.family_of）内试验夏普按 (strategy, params,
    symbol, timeframe, data_sha256, cost_model_version, strategy_sha)
    去重——同一组参数先 screen 后 gate 只计 1 次，v1/v2 新卡累计进同一
    谱系；T37：策略代码指纹进键（改代码重跑 = 新试验）；失败运行与
    迁移行（actor=migration）不计。

    返回 (去重后 N, 每根 bar 口径的试验夏普列表)；成功但无夏普的运行计入
    N、不进夏普列表（与 ledger.stats 的 n_dedup 同键口径）。
    """
    family = cards.family_of(card_id)
    rows = ledger.conn.execute(
        "SELECT card_id, strategy, params_json, symbol, timeframe,"
        " data_sha256, strategy_sha, cost_model_version, actor, metrics_json"
        " FROM runs").fetchall()
    by_key: dict[tuple, float | None] = {}
    for r in rows:
        if r["actor"] == "migration" or cards.family_of(r["card_id"]) != family:
            continue
        payload = json.loads(r["metrics_json"])
        if not payload.get("ok"):
            continue
        key = (r["strategy"],
               json.dumps(json.loads(r["params_json"]), sort_keys=True,
                          ensure_ascii=False),
               r["symbol"], r["timeframe"], r["data_sha256"],
               r["cost_model_version"], r["strategy_sha"])
        if key in by_key:
            continue
        s = (payload.get("metrics") or {}).get("sharpe")
        by_key[key] = None if s is None else float(s) / math.sqrt(bars_per_year)
    return len(by_key), [v for v in by_key.values() if v is not None]


def _bars_per_day(timeframe: str) -> int:
    """一个交易日的 bar 数（G4 块自助块长 / G5 参照块长，T21c/T21a）。"""
    return max(1, round(86400.0 / TIMEFRAME_SECONDS[timeframe.upper()]))


def _bind_strategy_sha(ledger, card, strategy_ref: str) -> str | None:
    """T37/D23：策略代码必须已提交且干净，并登记/校验卡版本的指纹绑定。

    - ensure_strategy_clean 不过 → 打印原因，返回 None（调用方退出 1，
      不产生任何 runs）；
    - 该卡版本已绑定其他指纹（首跑登记后代码改动）→ 拒绝，提示新建
      下一版本卡；legacy 行（迁移前的运行，无指纹）不构成绑定；
    - 返回当前代码指纹（record_run 落 runs.strategy_sha）。
    """
    try:
        cards.ensure_strategy_clean(strategy_ref, STRATEGY_FP_ROOT)
    except cards.StrategyDirtyError as e:
        print(f"✗ {e}")
        return None
    sha = cards.strategy_fingerprint(strategy_ref, STRATEGY_FP_ROOT)
    registered = ledger.card_strategy_sha(card.id, card.version)
    if registered is not None and registered != sha:
        print(
            f"✗ 卡 {card.id} v{card.version} 首跑已登记策略指纹 "
            f"{registered[:12]}…，与当前代码指纹 {sha[:12]}… 不符——"
            "策略代码在试验开始后改动过（同卡版本下改代码重跑会静默低估"
            " N，H3）；请新建下一版本卡（research/cards/ 下新建 "
            f"{cards.family_of(card.id)}-v{card.version + 1}.toml）再运行")
        return None
    print(f"— 策略指纹 {sha[:12]}…（T37/D23：strategy_sha 随运行落台账）")
    return sha


def _g3_reference_returns(ledger, card, spec, bars, costs, *,
                          data_sha: str) -> pd.Series:
    """重建 G3 拼接的样本外收益（run_g5 的参照分布，T21a）。

    读台账里该卡最近一次 G3 通过记录的 segments（test_range + 段内选中
    params），按 walkforward.run_g3 的同一切片/拼接口径重放各测试窗
    （research 段数据，不碰锁箱）。G3 记录的数据哈希与当前不符 / 无可用
    segments → ValueError（发生在 begin_lockbox 之前，不消耗锁箱一次性）。
    """
    row = ledger.conn.execute(
        "SELECT r.data_sha256 AS dsha, g.details_json AS dj"
        " FROM gates g JOIN runs r ON r.run_id = g.run_id"
        " WHERE r.card_id=? AND g.gate='G3' AND g.passed=1"
        " ORDER BY r.run_id DESC LIMIT 1", (card.id,)).fetchone()
    if row is None:
        raise ValueError(
            f"卡 {card.id} 无通过的 G3 关卡记录——G5 的参照分布取自 G3 拼接"
            "的样本外收益，没有可信参照不允许跑 G5")
    if row["dsha"] != data_sha:
        raise ValueError(
            f"卡 {card.id} 的 G3 记录基于 data_sha256={row['dsha'][:12]}…，"
            f"与当前数据 {data_sha[:12]}… 不符——参照分布已过期，先在新数据"
            "上重跑 G3")
    segments = (json.loads(row["dj"]) or {}).get("segments") or []
    lookback = max(int(getattr(spec.strategy.required_data,
                               "lookback_bars", 0) or 0), 0)
    rets = []
    for seg in segments:
        t1, t2 = (seg.get("test_range") or [0, 0])[:2]
        t1, t2 = int(t1), int(t2)
        if t2 <= t1:
            continue
        test = bars.iloc[max(0, t1 - lookback): t2]
        if not len(test):
            continue
        oos = vrob.run_engine(spec, test, costs, seg.get("params") or {})
        # 与 run_g3 同口径：测试窗权益收益（含测试窗首根，去预热段）
        rets.append(oos.equity.astype("float64")
                    .iloc[-(t2 - t1 + 1):].pct_change().dropna())
    if not rets:
        raise ValueError(
            f"卡 {card.id} 的 G3 记录没有可用的 segments——参照分布无从重建")
    return pd.concat(rets, ignore_index=True)


def _g5_orchestrate(ledger, card, *, strategy, spec, params, base_summary,
                    bars, costs, session_policy, data_root, approved_by,
                    cfg) -> dict:
    """G5 编排（T21a 协议，T21e 接线）：

    1. 参照重建（_g3_reference_returns，只读研究段；失败不消耗一次性）；
    2. begin_lockbox：读锁箱数据**之前**写 started（未批准/未过 G3/已用过
       /预算用尽（T27：cutoff 级 max_uses，默认 5）→ 如实抛
       GateApprovalError / LockboxPrerequisiteError /
       LockboxAlreadyUsedError / LockboxBudgetExhaustedError，
       不消耗或已消耗由语义决定）；
    3. load_lockbox_window：cutoff 前卡策略声明的 lookback_bars 根起步
       （无预热段时信号与全史不一致，C3）；
    4. 回测（引擎 A，与基准同一装配含禁入窗）；交易与权益收益**只计
       cutoff 之后**（bar_count 同）；
    5. run_g5：期望 > 0、交易数 ≥ 预期 × 比例、锁箱夏普 ≥ 参照 p5
       （单侧），评估完把 started 写成终态 done。

    expected_trades = 样本内交易频率（基准运行 trade_count / 研究段 bar 数）
    × 锁箱评估窗 bar 数。
    """
    # 1) 参照分布（G3 样本外收益；任何失败都发生在 begin_lockbox 之前）
    reference = _g3_reference_returns(
        ledger, card, spec, bars, costs, data_sha=data_sha256(bars))
    # 2) 读锁箱数据之前写 started（中途崩溃也不允许重跑）；cutoff 与
    #    load_lockbox_window 同一来源（lockbox.json），作为预算计数口径
    from mt5data.lockbox import cutoff_for, load_config
    lb_cutoff = cutoff_for(load_config(data_root), spec.symbol,
                           card.timeframe)
    vgate.begin_lockbox(ledger, card, approved_by=approved_by,
                        cutoff=lb_cutoff)
    # 3) 锁箱运行窗（带预热段）
    lookback = max(int(getattr(strategy.required_data, "lookback_bars", 0)
                       or 0), 0)
    lb_bars, cutoff = vgate.load_lockbox_window(
        data_root, spec.symbol, card.timeframe, lookback_bars=lookback)
    # 4) 回测；交易与收益只计 cutoff 之后（定手数口径与基准同一 spec）
    lb_kwargs = dict(symbol=spec.symbol, params=dict(params),
                     contract_size=spec.contract_size,
                     initial_capital=spec.initial_capital,
                     timeframe=card.timeframe,
                     session_policy=session_policy)
    if spec.sizing is not None:                     # T23：sizing 透传
        lb_kwargs.update(volume_lots=None, sizing=spec.sizing,
                         vol_min=spec.vol_min, vol_step=spec.vol_step,
                         vol_max=spec.vol_max)
    else:
        lb_kwargs["volume_lots"] = spec.volume_lots
    lb = run_backtest(strategy, lb_bars, costs, **lb_kwargs)
    entries = pd.to_datetime(lb.trades["entry_time"])
    lb_trades = lb.trades[entries >= cutoff]
    k = int(lb_bars["time"].searchsorted(pd.Timestamp(cutoff), side="left"))
    times_post = lb_bars["time"].iloc[k:]
    sharpe = vrob.equity_sharpe(lb.equity.astype("float64").iloc[k:],
                                times_post, card.timeframe)
    bpy = observed_bars_per_year(times_post) or bars_per_year(card.timeframe)
    lockbox_result = {
        "net_profit": (float(lb_trades["net"].sum()) if len(lb_trades)
                       else 0.0),
        "expectancy": (float(lb_trades["net"].mean()) if len(lb_trades)
                       else None),
        "sharpe": sharpe,
        "trade_count": int(len(lb_trades)),
        "bar_count": int(len(lb_bars) - k),
        "annualization_bars_per_year": float(bpy),
        "cutoff": pd.Timestamp(cutoff).isoformat(),
    }
    # 5) 预期交易数：样本内频率 × 锁箱评估窗长度；run_g5 写终态
    in_sample_trades = float(base_summary.get("trade_count") or 0)
    expected_trades = in_sample_trades / len(bars) * lockbox_result["bar_count"]
    return vgate.run_g5(ledger, card, lockbox_result=lockbox_result,
                        reference_returns=reference,
                        expected_trades=expected_trades,
                        approved_by=approved_by, cfg=cfg, cutoff=lb_cutoff)


# ------------------------------------------------------------ candidate check

def _merged_costs(data_root, symbols, *, spread_fraction: float,
                  commission: float, research_mode: bool):
    """候选预筛用的成本模型 v3：逐品种 build_costs 后合并成一个聚合体。

    预筛只信**缓存文件**的成本模型（与引擎同一缓存；tick 水位新鲜度由
    导出/构建侧把关），因此按研究库语义加载（build_costs research_store
    语义：缺缓存报错指路，不读 tick、不静默零成本）。合并：SpreadModel.
    merge（多品种点差）、CommissionModel per_symbol、SwapModel per_symbol
    规则、滑点按同一 spread_fraction 重建。返回 (Costs, cost_version)——
    版本串取各品种版本的去重拼接（进 precheck.json 的成本身份）。
    """
    from mt5quant.costs import CommissionModel, Costs, SlippageModel, SwapModel

    spread = None
    comm_by_sym: dict[str, float] = {}
    rules: dict[str, object] = {}
    versions: list[str] = []
    for sym in symbols:
        costs_i, ver_i = build_costs(
            data_root, sym, spread_fraction, commission,
            research_store=True)
        versions.append(ver_i)
        spread = costs_i.spread if spread is None else spread.merge(costs_i.spread)
        comm_by_sym[sym] = costs_i.commission_per_lot(sym)
        rules[sym] = costs_i.swap.rule(sym)
    merged = Costs(
        spread=spread,
        slippage_model=SlippageModel(spread, spread_fraction=spread_fraction),
        commission=CommissionModel(default_per_lot=commission,
                                   per_symbol=comm_by_sym),
        swap=SwapModel(per_symbol=rules))
    version = versions[0] if len(set(versions)) == 1 else "+".join(versions)
    return merged, version


def _active_mechanism_classes(candidates_dir: Path,
                              exclude_id: str) -> set[str]:
    """组合内已有活跃谱系的机制类别（蓝图 §6.2 条件 5 的"排后"依据）。

    活跃 = 候选登记 status ∈ {in_round, screened_pass}。装载失败的登记
    退回最小读取（status/mechanism_class 两键）；再失败 → 警告跳过（该
    扫描只供排序类理由，不阻塞决定）。
    """
    import tomllib

    active: set[str] = set()
    if not candidates_dir.is_dir():
        return active
    for p in sorted(candidates_dir.glob("*.toml")):
        try:
            c = cand.load_candidate(p)
            status, mclass = c.status, c.mechanism_class
        except cand.CandidateError:
            try:
                with open(p, "rb") as f:
                    raw = tomllib.load(f)
                status, mclass = raw.get("status"), raw.get("mechanism_class")
            except Exception:
                print(f"⚠ 机制扫描跳过 {p.name}（不可读）")
                continue
        if status in ("in_round", "screened_pass") and mclass \
                and p.stem != exclude_id:
            active.add(str(mclass))
    return active


def cmd_candidate_check(args) -> int:
    """T62/D31：候选预筛——读候选 → precheck → 写 json + 台账 → 打印结论。

    退出码：pass=0、fail=1、waiting_data=2。预筛结果写
    research/candidates/C###.precheck.json（工具生成、随候选提交）并记入
    台账 candidate_checks 表（带哈希链）。
    """
    value = args.candidate
    path = Path(value) if value.lower().endswith(".toml") \
        else Path(args.candidates_dir) / f"{value}.toml"
    try:
        candidate = cand.load_candidate(path)
    except cand.CandidateError as e:
        print(f"✗ {e}")
        return 1
    try:
        data_root, _store_kind = _resolve_store(args)
    except ValueError as e:
        print(f"✗ {e}")
        return 1
    if candidate.status in cand.BOOKKEEPING_STATUSES:
        # specimen/withdrawn 簿记：不是研究候选，precheck 短路为 fail，
        # 不装配成本、不算覆盖（其品种可能根本不在研究库）
        costs, cost_version, years, active = None, "n/a（非研究候选）", {}, set()
    else:
        try:
            costs, cost_version = _merged_costs(
                data_root, candidate.symbols,
                spread_fraction=args.spread_fraction,
                commission=args.commission, research_mode=True)
        except Exception as e:
            print(f"✗ 成本模型装配失败：{type(e).__name__}: {e}")
            return 1
        years = cand.coverage_years(data_root, candidate.symbols,
                                    [candidate.holding])
        active = _active_mechanism_classes(Path(candidate.path).parent,
                                           candidate.id)
    result = cand.precheck(candidate, data_root=data_root, cost_model=costs,
                           research_years=years, active_mechanisms=active)
    payload = result.to_payload()
    payload["cost_model_version"] = cost_version
    payload["checked_at_utc"] = datetime.now(timezone.utc).isoformat()

    # 结果落地：C###.precheck.json（原子写，铁律 15）+ 台账（哈希链）
    out_path = Path(candidate.path).with_suffix("").with_name(
        f"{candidate.id}.precheck.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, out_path)
    ledger_path = _ledger_default(args)
    with Ledger(ledger_path) as ledger:
        ledger.record_candidate_check(candidate.id, decision=result.decision,
                                      result=payload)

    mark = {"pass": "✓", "fail": "✗", "waiting_data": "⏳"}[result.decision]
    print(f"{mark} 候选 {candidate.id} 预筛结论：{result.decision}")
    years_note = (f"研究段 {min(payload['checks'].get('power', {})
                               .get('years_detail', {}).values() or [0]):.2f} 年"
                  if payload["checks"] else "")
    pw = payload["checks"].get("power", {}).get("power")
    mult = payload["checks"].get("ex_ante_cost", {}).get("multiple")
    if mult is not None:
        print(f"  事前成本倍数 {mult:.2f}（门槛 ≥3）")
    if pw is not None:
        print(f"  单次检验力 {pw:.0%}（门槛 ≥50%，{years_note}）")
    for r in result.reasons:
        print(f"  - {r}")
    print(f"预筛结果：{out_path}；台账 {ledger_path}（candidate_checks 哈希链）")
    return {"pass": 0, "fail": 1, "waiting_data": 2}[result.decision]


# ---------------------------------------------------------------- card check

def cmd_card_check(args) -> int:
    try:
        card = cards.load_card(args.path)
    except cards.CardError as e:
        print(f"✗ {e}")
        return 1
    lineage_err = _check_card_lineage(card, str(args.path))
    if lineage_err is not None:
        print(f"✗ {lineage_err}")
        return 1
    committed = cards.committed_at(args.path)
    committed_s = (f"已提交 {committed.isoformat()}" if committed is not None
                   else "⚠ 尚未提交到 git（先卡后测：运行前必须 git commit）")
    if committed is not None and not cards.worktree_clean(args.path):
        committed_s += "；⚠ 工作区有未提交改动（P6：先卡后测会拒绝运行）"
    print(f"✓ {args.path}")
    print(f"  id={card.id} version={card.version} "
          f"family={card.family} symbols={list(card.symbols)} "
          f"timeframe={card.timeframe}")
    print(f"  strategy={card.strategy or '（旧卡，未绑定：运行须 --strategy）'} "
          f"selection_rule={card.selection_rule}")
    print(f"  参数 {len(card.params)} 个，网格 {len(card.param_grid())} 组；{committed_s}")
    return 0


# ---------------------------------------------------------------- screen

def cmd_screen(args) -> int:
    if _reject_ops_ack(args):                 # R6/C6：研究命令拒绝运维豁免
        return 1
    path = _card_path(args.card, Path(args.cards_dir))
    try:
        card = cards.load_card(path)
    except cards.CardError as e:
        print(f"✗ {e}")
        return 1
    # T21d：卡绑定的策略与 --strategy 一致才放行（旧卡不绑定，沿 --strategy）
    try:
        cards.ensure_strategy_matches(card, args.strategy)
    except cards.CardStrategyMismatchError as e:
        print(f"✗ {e}")
        return 1
    if card.migration_hint:
        print(f"⚠ 迁移提示：{card.migration_hint}")
    # T61/D30：撤回卡拒绝运行；标定样品卡须 --specimen 且 actor=calibration
    actor = _enforce_candidate_status(args, card)
    if actor is None:
        return 1

    # T38/D24：数据根解析 + 守卫（主库须显式 --main-store；缺省研究库须存在）
    try:
        data_root, store_kind = _resolve_store(args)
    except ValueError as e:
        print(f"✗ {e}")
        return 1
    ledger_path = _ledger_default(args)
    research_mode = store_kind == "research"
    if research_mode:
        print(f"— 研究库模式（T27/H3；T38/D24 起为缺省）：data_root={data_root}"
              f"（无 tick，成本经缓存文件；台账 {ledger_path}）")
    # R6/C6：--ops-ack 已在入口拒绝，研究命令 actor 恒为原值
    # （T61：标定样品卡例外——actor 已在候选状态守卫里定为 calibration）
    with Ledger(ledger_path) as ledger:
        # 先卡后测：未提交 / 工作区脏（P6）/ 提交晚于该卡文件首次运行 →
        # 拒绝（不产生任何 runs）
        committed = cards.committed_at(path)
        try:
            cards.ensure_card_precedes_run(
                card.id, committed,
                ledger.first_run_at(card.id, version=card.version),
                worktree_clean=cards.worktree_clean(path))
        except (cards.CardNotCommittedError, cards.CardLateCommitError,
                cards.CardDirtyWorktreeError) as e:
            print(f"✗ {e}")
            return 1
        # T37/D23：策略代码已提交且干净 + 卡版本的指纹绑定校验
        strategy_sha = _bind_strategy_sha(ledger, card, args.strategy)
        if strategy_sha is None:
            return 1
        ledger.register_card(card.id, card.version, str(path),
                             current_git_commit(), committed)

        # 装配（setup 失败 = 尚无任何组合开跑，fail-fast 不记 runs）
        try:
            strategy_cls = load_strategy(args.strategy)
            sizes = {sym: resolve_contract_size(data_root, sym, None)
                     for sym in card.symbols}
            # T23：定手数口径（默认风险定手数；risk 模式逐品种读快照 vol 三元组）
            lots, sizing, sizing_info = resolve_lots_mode(args.risk_per_trade,
                                                          args.volume_lots)
            if sizing is not None:
                vols_by_sym = {sym: load_volume_limits(data_root, sym)
                               for sym in card.symbols}
            else:
                vols_by_sym = None
            bars_by_sym = {sym: load_series(data_root, sym,
                                            card.timeframe, args.end)
                           for sym in card.symbols}
            # T20：禁入窗唯一来源 config/sessions.toml（sha 进 cost_model_version）
            from mt5data.sessions import load_session_policy
            sess_policy = load_session_policy(str(SESSIONS_PATH))
            sess_sha = sessions_sha256(str(SESSIONS_PATH))
            # 成本按品种各自实测（点差/滑点是 per-symbol 的，不能借用别的品种）
            if args.costs == "auto":
                costs_by_sym = {}
                policy_by_sym = {}
                for sym in card.symbols:
                    costs_by_sym[sym], cost_version = build_costs(
                        data_root, sym, args.spread_fraction,
                        args.commission, sessions_sha=sess_sha,
                        research_store=research_mode)
                    policy_by_sym[sym] = entry_policy_for(
                        costs_by_sym[sym], sym, sess_policy)
            else:
                cost_version = "zero-v0"
                costs_by_sym = {sym: ZeroCosts() for sym in card.symbols}
                policy_by_sym = {sym: sess_policy for sym in card.symbols}
        except Exception as e:
            print(f"✗ 装配失败（未开跑，无 runs 记录）：{type(e).__name__}: {e}")
            return 1

        gates_hash = _gates_hash()
        combos = card.param_grid()
        failed = 0
        for sym in card.symbols:
            bars = bars_by_sym[sym]
            dsha = data_sha256(bars)
            times = bars["time"]
            data_first = pd_iso(times.iloc[0])
            data_last = pd_iso(times.iloc[-1])
            for params in combos:
                label = f"{sym} {card.timeframe} {params}"
                try:
                    if sizing is not None:
                        result = run_backtest(
                            strategy_cls(), bars, costs_by_sym[sym], symbol=sym,
                            params=dict(params), contract_size=sizes[sym],
                            volume_lots=None, sizing=sizing,
                            vol_min=vols_by_sym[sym][0],
                            vol_step=vols_by_sym[sym][1],
                            vol_max=vols_by_sym[sym][2],
                            initial_capital=args.initial_capital,
                            timeframe=card.timeframe,
                            session_policy=policy_by_sym[sym])
                    else:
                        result = run_backtest(
                            strategy_cls(), bars, costs_by_sym[sym], symbol=sym,
                            params=dict(params), contract_size=sizes[sym],
                            volume_lots=lots,
                            initial_capital=args.initial_capital,
                            timeframe=card.timeframe,
                            session_policy=policy_by_sym[sym])
                    summary = summarize(result, cost_version=cost_version)
                    summary["sizing"] = dict(   # T23：定手数方式随 metrics 落台账
                        sizing_info,
                        vol_min=vols_by_sym[sym][0] if vols_by_sym else None,
                        vol_step=vols_by_sym[sym][1] if vols_by_sym else None,
                        vol_max=vols_by_sym[sym][2] if vols_by_sym else None)
                    rid = ledger.record_run(
                        card_id=card.id, card_version=card.version,
                        strategy=args.strategy, params=params, symbol=sym,
                        timeframe=card.timeframe, data_first=data_first,
                        data_last=data_last, data_sha256=dsha,
                        engine_version=ENGINE_VERSION,
                        cost_model_version=cost_version, gates_hash=gates_hash,
                        metrics=summary, actor=actor,
                        strategy_sha=strategy_sha)
                    skipped = summary.get("skipped_zero_lots") or 0
                    print(f"✓ run#{rid} {label} -> "
                          f"净利 {summary['net_profit']:.2f} "
                          f"交易 {summary['trade_count']} 笔"
                          + (f"（零手数跳过 {skipped}）" if skipped else ""))
                except Exception as e:  # 失败组合也进台账
                    failed += 1
                    rid = ledger.record_run(
                        card_id=card.id, card_version=card.version,
                        strategy=args.strategy, params=params, symbol=sym,
                        timeframe=card.timeframe, data_first=data_first,
                        data_last=data_last, data_sha256=dsha,
                        engine_version=ENGINE_VERSION,
                        cost_model_version=cost_version,
                        gates_hash=gates_hash, metrics=None,
                        error=f"{type(e).__name__}: {e}", actor=actor,
                        strategy_sha=strategy_sha)
                    print(f"✗ run#{rid} {label} -> {type(e).__name__}: {e}")

        st = ledger.stats(card.id)
        print(f"— 卡 {card.id}：{st['total_runs']} 次运行（成功 {st['ok_runs']} / "
              f"失败 {st['failed_runs']}），台账 {ledger.path}")
        return 1 if failed else 0


def pd_iso(v) -> str:
    """pandas Timestamp → ISO 字符串（台账 data_first/last 用）。"""
    import pandas as pd
    return pd.Timestamp(v).isoformat()


# ---------------------------------------------------------------- gate

def cmd_gate(args) -> int:
    """关卡 G0–G5：基准运行入台账，逐关卡评估写 gates 表。"""
    if _reject_ops_ack(args):                 # R6/C6：研究命令拒绝运维豁免
        return 1
    path = _card_path(args.card, Path(args.cards_dir))
    try:
        card = cards.load_card(path)
    except cards.CardError as e:
        print(f"✗ {e}")
        return 1
    # T21d：卡绑定的策略与 --strategy 一致才放行（旧卡不绑定，沿 --strategy）
    try:
        cards.ensure_strategy_matches(card, args.strategy)
    except cards.CardStrategyMismatchError as e:
        print(f"✗ {e}")
        return 1
    if card.migration_hint:
        print(f"⚠ 迁移提示：{card.migration_hint}")
    # T61/D30：撤回卡拒绝运行；标定样品卡须 --specimen 且 actor=calibration
    actor = _enforce_candidate_status(args, card)
    if actor is None:
        return 1
    # 门槛冻结核对：sha256 与冻结值不符 → 拒绝运行（不产生任何 runs）
    try:
        cfg, gsha = vgate.load_gates()
    except vgate.GatesConfigError as e:
        print(f"✗ {e}")
        return 1
    wanted = [g.strip().upper() for g in args.gates.split(",") if g.strip()]
    unknown = [g for g in wanted if g not in vgate.GATE_ORDER]
    if unknown:
        print(f"✗ 未知关卡 {unknown}；合法：{list(vgate.GATE_ORDER)}")
        return 1

    # T38/D24：数据根解析 + 守卫（主库须显式 --main-store；缺省研究库须存在）
    try:
        data_root, store_kind = _resolve_store(args)
    except ValueError as e:
        print(f"✗ {e}")
        return 1
    ledger_path = _ledger_default(args)
    research_mode = store_kind == "research"
    if research_mode:
        print(f"— 研究库模式（T27/H3；T38/D24 起为缺省）：data_root={data_root}"
              f"（无 tick，成本经缓存文件；台账 {ledger_path}）")
    # T38/D24 + R6/C6：G5 需读主库锁箱段，且**主库校验对所有数据根模式
    # 生效**——研究库（缺省数据根）没有锁箱段，一律拒绝（即使已
    # --approve）；自定义 --data-root 同样拒绝（第六轮审核 C6：custom
    # 根可绕过 --main-store 检查，custom 根的数据未在主库授权与锁箱批准
    # 协议下管理，不能产生正式 G5 关卡结果）。G5 的锁箱读取经
    # --approve/--approved-by 协议（T21a），不占 --ops-ack 运维豁免。
    if "G5" in wanted and research_mode:
        print("✗ G5（锁箱一次性）需读主库的锁箱段——研究库模式（缺省数据根"
              " data_research）下无 lockbox.json，拒绝运行；须管理员显式"
              " --main-store（并 --approve）后运行")
        return 1
    if "G5" in wanted and store_kind != "main":
        print("✗ G5（锁箱一次性）只能读主库：自定义 --data-root"
              f"（{data_root}）不能用于 G5（R6/C6：custom 根的数据未在"
              "主库授权与锁箱批准协议下管理，据此登记的 G5 结果不可信）；"
              "须 --main-store（缺省 data）+ --approve 运行")
        return 1
    # R6/C6：--ops-ack 已在入口拒绝，研究命令 actor 恒为原值
    # （T61：标定样品卡例外——actor 已在候选状态守卫里定为 calibration）
    with Ledger(ledger_path) as ledger:
        committed = cards.committed_at(path)
        try:
            cards.ensure_card_precedes_run(
                card.id, committed,
                ledger.first_run_at(card.id, version=card.version),
                worktree_clean=cards.worktree_clean(path))
        except (cards.CardNotCommittedError, cards.CardLateCommitError,
                cards.CardDirtyWorktreeError) as e:
            print(f"✗ {e}")
            return 1
        # T37/D23：策略代码已提交且干净 + 卡版本的指纹绑定校验
        strategy_sha = _bind_strategy_sha(ledger, card, args.strategy)
        if strategy_sha is None:
            return 1
        ledger.register_card(card.id, card.version, str(path),
                             current_git_commit(), committed)
        # G5 预检：未批准 / 已用过 → 拒绝（不烧一次性，也不跑任何运行）
        if "G5" in wanted and not args.approve:
            print("✗ 锁箱（G5）是一次性测试，必须显式 --approve"
                  "（并建议 --approved-by 记录批准人）——本次未执行")
            return 1
        if "G5" in wanted and ledger.lockbox_used(card.id):
            print(f"✗ 卡 {card.id} 的锁箱已用过一次（lockbox_uses 有记录）——"
                  "第二次请求直接拒绝")
            return 1
        # T27/M11：锁箱预算预检（cutoff 级 max_uses，默认 5，D16 未决）——
        # 用尽直接拒绝，不烧该卡一次性、也不跑基准运行
        if "G5" in wanted:
            from mt5data.lockbox import cutoff_for, load_config
            lb_cut = cutoff_for(load_config(data_root), card.symbols[0],
                                card.timeframe)
            if lb_cut is not None:
                budget = ledger.lockbox_budget(lb_cut)
                if budget["exhausted"]:
                    print(f"✗ 锁箱 cutoff={budget['cutoff']} 的使用预算已用尽"
                          f"（{budget['used']}/{budget['max_uses']}，T27/M11"
                          "）——G5 拒绝运行；预算与退役规则见蓝图 D16")
                    return 1

        # 装配（失败 = 未开跑，fail-fast 不记 runs）
        try:
            strategy = load_strategy(args.strategy)()
            sizes = {sym: resolve_contract_size(data_root, sym, None)
                     for sym in card.symbols}
            # T23：定手数口径（默认风险定手数；risk 模式逐品种读快照 vol 三元组）
            lots, sizing, sizing_info = resolve_lots_mode(args.risk_per_trade,
                                                          args.volume_lots)
            if sizing is not None:
                vols_by_sym = {sym: load_volume_limits(data_root, sym)
                               for sym in card.symbols}
            else:
                vols_by_sym = None
            bars_by_sym = {sym: load_series(data_root, sym,
                                            card.timeframe, args.end)
                           for sym in card.symbols}
            # T20：禁入窗唯一来源 config/sessions.toml（sha 进 cost_model_version）
            from mt5data.sessions import load_session_policy
            sess_policy = load_session_policy(str(SESSIONS_PATH))
            sess_sha = sessions_sha256(str(SESSIONS_PATH))
            if args.costs == "auto":
                costs_by_sym = {}
                policy_by_sym = {}
                for sym in card.symbols:
                    costs_by_sym[sym], cost_version = build_costs(
                        data_root, sym, args.spread_fraction,
                        args.commission, sessions_sha=sess_sha,
                        research_store=research_mode)
                    policy_by_sym[sym] = entry_policy_for(
                        costs_by_sym[sym], sym, sess_policy)
            else:
                cost_version = "zero-v0"
                costs_by_sym = {sym: ZeroCosts() for sym in card.symbols}
                policy_by_sym = {sym: sess_policy for sym in card.symbols}
        except Exception as e:
            print(f"✗ 装配失败（未开跑，无 runs 记录）：{type(e).__name__}: {e}")
            return 1

        symbol = card.symbols[0]
        bars = bars_by_sym[symbol]
        costs = costs_by_sym[symbol]
        combos = card.param_grid()      # G3 的网格内重选用

        # 参数选择（T21e/M5）：缺省与 "auto" 均按卡 selection_rule 从
        # 同数据哈希/成本版本的 screen 运行自动选参；显式 JSON 记 manual。
        dsha = data_sha256(bars)
        if args.params is None or args.params == "auto":
            try:
                selection = _select_params_auto(
                    ledger, card, symbol=symbol, data_sha256=dsha,
                    cost_model_version=cost_version)
            except ValueError as e:
                print(f"✗ {e}")
                return 1
            params = selection["params"]
            print(f"— 参数选择（auto）：规则 {selection['rule']}"
                  f"（{selection['metric']}），候选 {selection['candidates']}"
                  f" 个 screen 运行，选中 run#{selection['selected_run_id']}"
                  f" -> {params}")
        else:
            try:
                params = json.loads(args.params)
            except ValueError as e:
                print(f"✗ --params 不是合法 JSON：{e}")
                return 1
            selection = {"selection": "manual", "params": params}
            print(f"— 参数选择（manual）：显式 --params -> {params}")

        # 基准运行（引擎 A）：先入台账（失败的也记），关卡结果挂在它名下
        data_first, data_last = pd_iso(bars["time"].iloc[0]), pd_iso(bars["time"].iloc[-1])
        common = dict(
            card_id=card.id, card_version=card.version,
            strategy=args.strategy, params=params, symbol=symbol,
            timeframe=card.timeframe, data_first=data_first,
            data_last=data_last, data_sha256=dsha,
            engine_version=ENGINE_VERSION, cost_model_version=cost_version,
            gates_hash=gsha, actor=actor, strategy_sha=strategy_sha)
        try:
            base_kwargs = dict(symbol=symbol, params=dict(params),
                               contract_size=sizes[symbol],
                               initial_capital=args.initial_capital,
                               timeframe=card.timeframe,
                               session_policy=policy_by_sym[symbol])
            if sizing is not None:                       # T23：同一口径
                base_kwargs.update(volume_lots=None, sizing=sizing,
                                   vol_min=vols_by_sym[symbol][0],
                                   vol_step=vols_by_sym[symbol][1],
                                   vol_max=vols_by_sym[symbol][2])
            else:
                base_kwargs["volume_lots"] = lots
            base = run_backtest(strategy, bars, costs, **base_kwargs)
            summary = summarize(base, cost_version=cost_version)
            summary["sizing"] = dict(
                sizing_info,
                vol_min=(vols_by_sym[symbol][0] if vols_by_sym else None),
                vol_step=(vols_by_sym[symbol][1] if vols_by_sym else None),
                vol_max=(vols_by_sym[symbol][2] if vols_by_sym else None))
            run_id = ledger.record_run(metrics=summary, **common)
            print(f"— run#{run_id} {symbol} {card.timeframe} {params} -> "
                  f"净利 {summary['net_profit']:.2f} "
                  f"交易 {summary['trade_count']} 笔")
        except Exception as e:
            run_id = ledger.record_run(metrics=None,
                                       error=f"{type(e).__name__}: {e}", **common)
            print(f"✗ run#{run_id} 基准运行失败：{type(e).__name__}: {e}")
            return 1

        # T23：spec 带定手数口径 → G2/G3 一切复算经 run_engine 沿用同一口径
        if sizing is not None:
            spec = BacktestSpec(strategy=strategy, symbol=symbol,
                                timeframe=card.timeframe,
                                contract_size=sizes[symbol],
                                volume_lots=None,
                                initial_capital=args.initial_capital,
                                sizing=sizing,
                                vol_min=vols_by_sym[symbol][0],
                                vol_step=vols_by_sym[symbol][1],
                                vol_max=vols_by_sym[symbol][2])
        else:
            spec = BacktestSpec(strategy=strategy, symbol=symbol,
                                timeframe=card.timeframe,
                                contract_size=sizes[symbol],
                                volume_lots=lots,
                                initial_capital=args.initial_capital)
        seed = vstats.rng_seed(card.id)
        results: dict[str, dict] = {}

        def _run(name, fn):
            try:
                results[name] = fn()
            except Exception as e:   # 单关卡炸掉不拖垮其余关卡
                results[name] = {"passed": False,
                                 "error": f"{type(e).__name__}: {e}"}

        if "G0" in wanted:
            _run("G0", lambda: vlook.run_g0(strategy, bars, params))
        if "G1" in wanted:
            _run("G1", lambda: vgate.run_g1(base.trades,
                                            timeframe=card.timeframe, cfg=cfg))
        if "G2" in wanted:
            _run("G2", lambda: vrob.run_g2(
                spec, bars, costs, params, base_result=base,
                base_summary=summary, grid=card.params,
                bars_by_symbol=bars_by_sym, costs_by_symbol=costs_by_sym,
                sizes_by_symbol=sizes, cfg=cfg, seed=seed,
                vols_by_symbol=vols_by_sym))    # T23：sizing 口径跨品种透传
        if "G3" in wanted:
            _run("G3", lambda: vwf.run_g3(
                spec, bars, costs, grid=combos,
                in_sample_sharpe=summary.get("sharpe"), cfg=cfg))
        if "G4" in wanted:
            def _g4():
                returns = base.equity.astype("float64").pct_change().dropna()
                bpy = summary.get("annualization_bars_per_year") \
                    or bars_per_year(card.timeframe)
                # T21c/M4：N 与试验夏普按谱系内去重（同参 screen+gate 只计 1）
                n_dedup, trial_sharpes = _dedup_trial_sharpes(
                    ledger, card.id, float(bpy))
                out = vgate.run_g4(
                    returns, n_trials=n_dedup, trial_sharpes=trial_sharpes,
                    actual_max_drawdown=summary.get("max_drawdown", 0.0),
                    cfg=cfg, seed=seed,
                    block_len=_bars_per_day(card.timeframe))
                out["project_n_trials"] = ledger.stats(card.id)[
                    "project_n_dedup"]
                return out
            _run("G4", _g4)
        if "G5" in wanted:
            _run("G5", lambda: _g5_orchestrate(
                ledger, card, strategy=strategy, spec=spec, params=params,
                base_summary=summary, bars=bars, costs=costs,
                session_policy=policy_by_sym[symbol],
                data_root=data_root,
                approved_by=args.approved_by or args.actor, cfg=cfg))

        for gate in vgate.GATE_ORDER:
            if gate in results:
                details = dict(results[gate])
                # M5：参数选择过程（候选数、选中 run_id）随每个关卡落台账
                details["params_selection"] = selection
                ledger.record_gate(run_id, gate, bool(results[gate]["passed"]),
                                   details)
        report = vgate.render_gate_report(card.id, params, results)
        if selection["selection"] == "auto":
            report += (
                f"\n## 参数选择（auto）\n\n"
                f"- 规则：`{selection['rule']}`（按 {selection['metric']}）\n"
                f"- 候选：{selection['candidates']} 个同数据哈希/成本版本的"
                f" screen 运行\n"
                f"- 选中：run#{selection['selected_run_id']} -> `{params}`"
                f"（{selection['metric']}={selection['selected_value']:.2f}）\n")
        else:
            report += "\n## 参数选择（manual）\n\n- 显式 --params 指定\n"
        print(report)
        if args.out:
            out = Path(args.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(report, encoding="utf-8")
            print(f"报告：{out}")
        return 0 if all(r["passed"] for r in results.values()) else 1


# ---------------------------------------------------------------- ledger stats

def cmd_ledger_stats(args) -> int:
    ledger_path = Path(args.ledger) if args.ledger else \
        Path(args.data_root) / "research" / "ledger.sqlite"
    try:
        with Ledger(ledger_path, create=False) as ledger:
            st = ledger.stats(args.card)
    except FileNotFoundError as e:
        print(f"✗ {e}（先跑 bt screen 生成台账）")
        return 1
    print(json.dumps(st, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------- ledger verify

def cmd_ledger_verify(args) -> int:
    """T37/M6/D27 + R6/C5：校验 runs/gates/lockbox_uses/candidate_checks
    四条哈希链 + chain_meta 快照（断链/篡改/删尾/未登记写入）。"""
    ledger_path = Path(args.ledger) if args.ledger else \
        Path(args.data_root) / "research" / "ledger.sqlite"
    try:
        with Ledger(ledger_path, create=False) as ledger:
            problems = ledger.verify_chain()
    except FileNotFoundError as e:
        print(f"✗ {e}")
        return 1
    if problems:
        print(f"✗ 台账哈希链校验失败（{len(problems)} 处）：")
        for p in problems:
            print(f"  - {p}")
        print("— 与 docs/ops/ledger-anchors.md 的锚定记录比对（R6/C5 链式"
              "锚：scripts/ledger_anchor.py --verify），可定位改写发生的"
              "时间（D27）；台账不完整期间一切写入被拒（防洗链）")
        return 1
    print("✓ 台账哈希链完好（runs/gates/lockbox_uses/candidate_checks 四链"
          "逐行校验 + chain_meta 快照比对通过）")
    return 0


# ---------------------------------------------------------------- 入口

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="bt", description="研究命令行：假设卡检查、网格筛选进台账、台账统计")

    sub = ap.add_subparsers(dest="command", required=True)

    p_cand = sub.add_parser("candidate", help="候选登记操作（T62/D31）")
    cand_sub = p_cand.add_subparsers(dest="candidate_command", required=True)
    pc = cand_sub.add_parser(
        "check", help="候选预筛：五条件 → C###.precheck.json + 台账")
    pc.add_argument("candidate",
                    help="候选 id（C001）或 research/candidates/C001.toml 路径")
    pc.add_argument("--candidates-dir",
                    default=str(REPO_ROOT / "research" / "candidates"))
    pc.add_argument("--data-root", default=None,
                    help="数据根；缺省研究库 data_research（研究年数按实际"
                         "覆盖、参考价与合约大小取自 specs）")
    pc.add_argument("--research-store", action="store_true",
                    help="（兼容别名）读研究库 data_research——T38 起缺省即研究库")
    pc.add_argument("--main-store", action="store_true",
                    help="读主库 data（含完整锁箱段，须显式本 flag 才读）")
    pc.add_argument("--ledger", default=None,
                    help="台账路径；缺省 data/research/ledger.sqlite"
                         "（研究库模式）或 {data-root}/research/ledger.sqlite")
    pc.add_argument("--spread-fraction", type=float, default=0.1)
    pc.add_argument("--commission", type=float, default=0.0)
    pc.set_defaults(func=cmd_candidate_check)

    p_check = sub.add_parser("card", help="假设卡操作")
    card_sub = p_check.add_subparsers(dest="card_command", required=True)
    pchk = card_sub.add_parser("check", help="校验卡模式并显示提交状态")
    pchk.add_argument("path", help="卡文件路径（research/cards/H012.toml）")
    pchk.set_defaults(func=cmd_card_check)

    p_screen = sub.add_parser("screen", help="在卡的网格上跑 A 层并记入台账")
    p_screen.add_argument("--card", required=True,
                          help="卡 id（H012）或 .toml 文件路径")
    p_screen.add_argument("--strategy", required=True,
                          help="module:Class，如 mt5quant.strategies.library.buy_hold:BuyHold")
    p_screen.add_argument("--cards-dir", default=str(REPO_ROOT / "research" / "cards"))
    p_screen.add_argument("--data-root", default=None,
                          help="数据根；缺省 data_research（T38/D24），"
                               "--main-store 时缺省 data（显式给出者优先；"
                               "指到主库的仍须 --main-store）")
    p_screen.add_argument("--research-store", action="store_true",
                          help="（T27 兼容别名）读研究库 data_research——"
                               "T38 起缺省即研究库，本 flag 通常不用；台账"
                               "缺省仍落 data/research/ledger.sqlite")
    p_screen.add_argument("--main-store", action="store_true",
                          help="读主库 data（T38/D24：主库含完整锁箱段，"
                               "须显式本 flag 才读）")
    p_screen.add_argument("--ops-ack", action="store_true",
                          help="（R6/C6 起拒绝）：运维豁免仅供 reconcile_ab/"
                               "measure 类运维命令，screen/gate 一律拒绝，"
                               "不给锁箱期数据进入研究统计的旁路")
    p_screen.add_argument("--ledger", default=None,
                          help="台账路径；缺省 {data-root}/research/ledger.sqlite"
                               "（--research-store 时固定 data/research/ledger.sqlite）")
    p_screen.add_argument("--costs", choices=("auto", "zero"), default="auto",
                          help="与 run_backtest 同义；测试/冒烟用 zero")
    p_screen.add_argument("--end", default=None,
                          help="只用该 UTC 时刻及之前的数据（锁箱语义同 run_backtest）")
    p_screen.add_argument("--volume-lots", type=float, default=None,
                          help="固定手数；显式给出即用固定手数（与 run_backtest 同义）")
    p_screen.add_argument("--risk-per-trade", type=float, default=None,
                          help="按风险定手数的单笔风险比例（缺省 0.005；0=固定手数；"
                               "与 --volume-lots 同时显式给且>0 会拒绝）")
    p_screen.add_argument("--initial-capital", type=float, default=10_000.0)
    p_screen.add_argument("--spread-fraction", type=float, default=0.1)
    p_screen.add_argument("--commission", type=float, default=0.0)
    p_screen.add_argument("--actor", default="bt-screen", help="台账 actor 字段")
    p_screen.add_argument("--specimen", action="store_true",
                          help="标定样品卡（status=specimen，T61/D30）的运行"
                               "开关：样品卡必须显式给出（否则拒绝），台账记"
                               " actor=calibration，不进入研究轮次统计；"
                               "非样品卡给出则拒绝")
    p_screen.set_defaults(func=cmd_screen)

    p_gate = sub.add_parser("gate", help="关卡 G0–G5：跑关卡并写 gates 表")
    p_gate.add_argument("--card", required=True,
                        help="卡 id（H012）或 .toml 文件路径")
    p_gate.add_argument("--strategy", required=True,
                        help="module:Class，如 mt5quant.strategies.library.buy_hold:BuyHold")
    p_gate.add_argument("--gates", default="G0,G1,G2,G3,G4",
                        help="逗号分隔的关卡子集；G5（锁箱一次性）须显式列出且加 --approve")
    p_gate.add_argument("--params", default=None,
                        help="JSON 参数；缺省与 \"auto\" 均按卡 selection_rule "
                             "从同数据哈希/成本版本的 screen 运行自动选参"
                             "（T21e/M5），选择过程写进关卡报告与 gates 表")
    p_gate.add_argument("--approve", action="store_true",
                        help="批准 G5 锁箱一次性测试（看过一眼即消耗）")
    p_gate.add_argument("--approved-by", default=None,
                        help="G5 批准人（缺省记 --actor）")
    p_gate.add_argument("--cards-dir", default=str(REPO_ROOT / "research" / "cards"))
    p_gate.add_argument("--data-root", default=None,
                        help="数据根；缺省 data_research（T38/D24），"
                             "--main-store 时缺省 data（显式给出者优先；"
                             "指到主库的仍须 --main-store）")
    p_gate.add_argument("--research-store", action="store_true",
                        help="（T27 兼容别名）读研究库 data_research——"
                             "T38 起缺省即研究库，本 flag 通常不用；G5 需锁箱"
                             "段，只能由管理员在主库侧跑")
    p_gate.add_argument("--main-store", action="store_true",
                        help="读主库 data（T38/D24：主库含完整锁箱段，须显式"
                             "本 flag 才读；R6/C6 起 G5 只能经本 flag 跑——"
                             "研究库与自定义 --data-root 一律拒绝）")
    p_gate.add_argument("--ops-ack", action="store_true",
                        help="（R6/C6 起拒绝）：运维豁免仅供 reconcile_ab/"
                             "measure 类运维命令，gate 一律拒绝，不给锁箱期"
                             "数据进入研究关卡的旁路")
    p_gate.add_argument("--ledger", default=None)
    p_gate.add_argument("--costs", choices=("auto", "zero"), default="auto",
                        help="与 run_backtest 同义；测试/冒烟用 zero")
    p_gate.add_argument("--end", default=None,
                        help="只用该 UTC 时刻及之前的数据（锁箱语义同 run_backtest）")
    p_gate.add_argument("--volume-lots", type=float, default=None,
                        help="固定手数；显式给出即用固定手数（与 run_backtest 同义）")
    p_gate.add_argument("--risk-per-trade", type=float, default=None,
                        help="按风险定手数的单笔风险比例（缺省 0.005；0=固定手数；"
                             "与 --volume-lots 同时显式给且>0 会拒绝）")
    p_gate.add_argument("--initial-capital", type=float, default=10_000.0)
    p_gate.add_argument("--spread-fraction", type=float, default=0.1)
    p_gate.add_argument("--commission", type=float, default=0.0)
    p_gate.add_argument("--actor", default="bt-gate", help="台账 actor 字段")
    p_gate.add_argument("--specimen", action="store_true",
                        help="标定样品卡（status=specimen，T61/D30）的运行"
                             "开关：样品卡必须显式给出（否则拒绝），台账记"
                             " actor=calibration，不进入研究轮次统计；"
                             "非样品卡给出则拒绝")
    p_gate.add_argument("--out", default=None, help="关卡报告 Markdown 落盘路径")
    p_gate.set_defaults(func=cmd_gate)

    p_ledger = sub.add_parser("ledger", help="台账操作")
    ledger_sub = p_ledger.add_subparsers(dest="ledger_command", required=True)
    pstat = ledger_sub.add_parser("stats", help="运行计数（试验次数，供 DSR 用）")
    pstat.add_argument("--card", default=None, help="只统计该卡家族（跨版本）")
    pstat.add_argument("--data-root", default="data")
    pstat.add_argument("--ledger", default=None)
    pstat.set_defaults(func=cmd_ledger_stats)
    pverify = ledger_sub.add_parser(
        "verify", help="校验台账哈希链（T37/M6/D27：断链/篡改逐行报出）")
    pverify.add_argument("--data-root", default="data")
    pverify.add_argument("--ledger", default=None)
    pverify.set_defaults(func=cmd_ledger_verify)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except LedgerTamperedError as e:
        # R6/C5：台账不完整 → 一切写入 fail-closed（防洗链）；
        # 打印后退出 1，不产生任何 runs/gates/lockbox_uses 行。
        print(f"✗ {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
