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
- 研究轮次预注册（T63/D32，铁律 16）：research/rounds/R#.toml（蓝图
  附录 D：候选 ≤3、试验预算、锁箱用量、推进/结束规则、批准人、每候选
  评估方式）**已提交且工作区干净**、候选全 screened_pass（台账
  candidate_checks 最新 decision=pass）、lockbox_uses_max ≤ D16 剩余
  预算（D16：本锁箱 3 次，写死常量待用户决策后可调）才可 bt round open
  ——开启写台账 rounds 表（带哈希链，toml_sha256 绑定预注册内容）。
  运行约束：screen/gate 只接受属于已开启轮次的卡（卡 round/candidate
  字段对上台账轮次行且候选 screened_pass；轮次关闭后拒；台账一旦有
  轮次，不带轮次字段的卡拒——轮次制之前的旧卡仅在 rounds 空的台账上
  运行）；H-08 起轮次/候选以**冻结快照**为准——当前轮次文件 sha256 须
  与开启时入账的 toml_sha256 一致（开轮后改文件拒），候选 pass 记录
  绑定候选内容（改候选登记须重新 bt candidate check 才生效），模式
  文件损坏即失败不回退；本轮去重试验数达 trial_budget 拒新试验；本轮
  G5 次数达 lockbox_uses_max 拒（占位事务内复查；标定样品运行
  actor=calibration 豁免——不进轮次统计）。bt round close 生成
  docs/research/round-R#-<日期>.md 骨架（列全部运行含失败 + 按
  advance_rule 的判定占位）并关闭轮次。
- 候选预筛（T62/D31，铁律 17/18）：bt candidate check 读附录 C 候选登记
  → precheck 五条件（文献/持仓 ≥H1/事前成本倍数 ≥3/检验力 ≥50%/机制空缺）
  → 写 research/candidates/C###.precheck.json 并记入台账 candidate_checks
  表（带哈希链）；退出码 pass=0 / fail=1 / waiting_data=2。研究年数按
  研究库品种×周期实际覆盖计算（不用常数；T80/G3 起合并已批准外部序列
  data_research/ext/（fred/cdd，D36 白名单），逐段列来源与年数，未批准
  来源不计）；检验力函数与 round11 探针逐位一致（Lo 2002 近似），夏普
  取声明与隐含中较小者（T80/G2：隐含夏普 =（每事件毛收益−事前来回成本）
  ÷ 品种无条件收益波动 σ × √每年事件数，σ 与口径写进 precheck JSON）。
- 撤回与标定样品（T61/D30）：research/candidates/ 候选登记里
  status=withdrawn 的卡（source_card 指向卡 id，如 H002-v2/H003-v2/
  H006-v2）拒绝 screen/gate；status=specimen 的标定样品卡（H001–H007）
  必须显式 --specimen 运行且台账 actor=calibration、run_class=calibration
  （不进入研究轮次与项目级研究 N 统计），--specimen 用于非样品卡 →
  拒绝（cards.candidate_statuses_by_card）。
- 运行类别（T80/G4，D33）：--actor 保留名（calibration/ops/migration）
  拒绝——运行类别由 bt 按候选状态与轮次写台账 runs.run_class（research|
  calibration|ops|migration）并入哈希链；N 的研究口径
  （project_n_dedup_research）只看这个字段，不看 actor 文本。生产台账
  加列走 _rechain 迁移（旧行按 actor 推断+记迁移事件），迁移后须用
  scripts/ledger_anchor.py --accept-rechain 重锚（主会话执行）。
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
- G5 编排（T21a 协议；H-02/H-03 修订）：关卡结果随评估**即时入账**
  （先可靠保存研究关卡结果），_g5_orchestrate = 参照重建（G3 拼接的
  样本外收益，只读研究段）→ begin_lockbox（读锁箱数据**之前**原子占位：
  绑定当前试验身份 LockboxTrial——参数/数据哈希/成本版本/代码指纹/
  冻结门槛 + 当前通过的 G3 与 V3 判决，历史成功不授权；项目预算/轮次
  上限/卡一次性的检查与 started 写入同一事务）→ load_lockbox_window
  （cutoff 前卡策略声明的 lookback_bars 根起步）→ 回测（交易与收益
  只计 cutoff 后）→ run_g5（单侧判定，写终态 done）。
- G4（T21c 接线；T64/D33 取数口径）：n_trials 与试验夏普按
  (strategy, params, symbol, timeframe, data_sha256, cost_model_version,
  strategy_sha) 去重（_dedup_trial_sharpes，同参 screen+gate 只计 1），
  取数范围按门槛键 [g4].dsr_trial_scope（family|project|rounds_lineage；
  D34 冻结后 gates.toml 显式写 rounds_lineage = ledger.primary_test_n 的
  门槛部分——T85 冻结 L1；family/project 语义不变）；关卡结果与报告
  同时列谱系/轮次/项目三种 N；蒙特卡洛回撤块长 = 一个交易日的 bar 数
  （block_len）。
- V3（T86，T85/D34 冻结后的关卡 v3 生产接线；--gates V3 显式选择，G1–G4
  现行路径不变）：bar 级卡 = 主检验（[v3].primary_test，冻结 dsr_v0，
  OOS 取 G3 拼接样本外——本次运行 segments 或台账最近通过记录）+ 三项
  否决 + 诊断只报告；轮次 [evaluation] mode="event" 的卡走事件分支
  （随机化参照 p ≤ α/N、随机化次数 ≥ max(1999,⌈20N/α⌉)——低于下限在
  基准运行前拒绝、成本 ×1.5 否决、buyhold/延迟不适用、四项诊断）。
  N = ledger.primary_test_n(候选声明的 continues_family) 的门槛部分
  （rounds_lineage；报告列轮次内/延续谱系/其余历史三行）。事件窗类别
  --event-kind month_end|london_fix 必须显式声明（按预注册机制锚定）。
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
  cost_model_version 写进台账。H-06 起 G2–G5/V3 的一切关卡复算经
  BacktestSpec.session_policy / run_g2(policies_by_symbol) 沿用同一
  逐品种禁入政策（复算不再丢掉换日/低样本小时禁入）。
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
import hashlib
import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))   # 复用 run_backtest 的装配

from mt5data.quality import read_known_gaps  # noqa: E402
from mt5data.store import TIMEFRAME_SECONDS  # noqa: E402
from mt5quant.backtest.engine_a import ENGINE_VERSION, run_backtest  # noqa: E402
from mt5quant.backtest.report import (  # noqa: E402
    bars_per_year, current_git_commit, data_sha256, observed_bars_per_year,
    summarize,
)
from mt5quant.research import cards  # noqa: E402
from mt5quant.research import candidates as cand  # noqa: E402
from mt5quant.research import power  # noqa: E402
from mt5quant.research import rounds as rnd  # noqa: E402
from mt5quant.research.ledger import (  # noqa: E402
    Ledger, LedgerTamperedError, RESERVED_ACTORS, run_class_for_actor,
)
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
# T83/R13-K6：账户隔夜费画像（研究侧口径开关；缺省不存在=全 charged）
ACCOUNT_PROFILE_PATH = REPO_ROOT / "config" / "account-profile.toml"
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

    - T80/G4：--actor 保留名（calibration/ops/migration）拒绝——运行类别
      由 bt 按候选状态与轮次写入台账 run_class 字段，不看 actor 文本
      （防研究运行借 actor 逃出研究 N，第十二轮 G4）；
    - status=withdrawn（撤回卡，如 H002-v2/H003-v2/H006-v2）→ 拒绝运行：
      打印原因（含 withdrawn）并返回 None——撤回的卡不再产生任何 runs；
    - status=specimen（标定样品卡，H001–H007）→ 必须 --specimen 才运行，
      且 actor 强制 calibration（蓝图 v4 §6.1：样品只用于校准关卡/成本/
      引擎/实盘链路，不作研究候选，不进入研究轮次统计）；
    - --specimen 用于非样品卡 → 拒绝（标定通道不能给研究卡统计逃逸）；
    - 无登记（普通研究卡、不带 --specimen）→ 原样返回 --actor。
    """
    if getattr(args, "actor", None) in RESERVED_ACTORS:
        print(f"✗ --actor {args.actor!r} 是保留名（{list(RESERVED_ACTORS)}，"
              "T80/G4）——运行类别（run_class）由 bt 按候选状态与轮次"
              "写入台账，N 的口径只看 run_class 字段、不看 actor 文本；"
              "请用非保留的 actor 名")
        return None
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


def _run_class_for(actor: str) -> str:
    """T80/G4：运行类别——标定样品（_enforce_candidate_status 已把 actor
    强制为 calibration）→ calibration；研究运行（含轮次内）→ research。
    运维通道（reconcile_ab，actor=ops）不经 bt，台账侧按 actor 缺省推断
    为 ops；迁移行由台账迁移写入 migration。"""
    return "calibration" if actor == "calibration" else "research"


def _check_card_lineage(card, source: str) -> str | None:
    """selection_rule 枚举校验（M5：非法值定义时拒）；返回错误信息或 None。"""
    if card.selection_rule not in _SELECTION_METRICS:
        return (f"{source}：selection_rule {card.selection_rule!r} 不在支持"
                f"枚举 {sorted(_SELECTION_METRICS)} 内（附录 A v2；"
                "bt gate --params auto 按该规则自动选参）")
    return None


# ---------------------------------------------------------------- 轮次（T63）

def _round_path(value: str, rounds_dir: Path) -> Path:
    """--round 接受 id（R1）或轮次文件路径（.toml 结尾）。"""
    if value.lower().endswith(".toml"):
        return Path(value)
    return rounds_dir / f"{value}.toml"


def _round_file_path(card) -> Path:
    """卡所属轮次的预注册文件路径（cards/ 的兄弟目录 rounds/）。

    卡对象无 path（替身/不完整装载）→ RoundError（fail-closed：轮次
    以冻结快照为准，定位不到预注册文件即无从核对）。"""
    p = getattr(card, "path", "")
    if not p:
        raise rnd.RoundError(
            f"卡 {getattr(card, 'id', '?')!r} 无文件路径——轮次预注册无从"
            "定位（H-08：轮次以冻结快照为准，定位不到文件即无从核对）")
    return Path(p).parent.parent / "rounds" / f"{card.round}.toml"


def _candidate_frozen_check(ledger, candidate_id: str,
                            candidates_dir: Path) -> str | None:
    """H-08：候选 pass 记录绑定候选内容——最新 pass 行带 candidate_sha256
    时，当前文件 hash 须与绑定值一致；不一致 → 返回错误信息（改候选须
    重新 bt candidate check 绑定新内容后才生效）。旧记录（无绑定键，
    H-08 之前的预筛行）不强制——内容绑定自新记录起生效。"""
    rows = ledger.candidate_checks(candidate_id)
    if not rows:
        return None                      # 无记录：由 screened_pass 前置拦
    last = rows[-1]
    raw = last["result_json"] if "result_json" in last.keys() else None
    if raw is None:
        return None                      # 无 result_json（旧形态/替身）——无绑定可核
    try:
        payload = json.loads(raw)
    except ValueError:
        return (f"候选 {candidate_id} 最新预筛记录不可解析（result_json"
                "损坏）——预筛结论不可信，须重新 bt candidate check")
    bound = (payload or {}).get("candidate_sha256")
    if not bound:
        return None                      # 旧记录未绑定（H-08 前的兼容）
    p = Path(candidates_dir) / f"{candidate_id}.toml"
    if not p.is_file():
        return (f"候选登记 {p} 不存在——预筛通过绑定的内容快照无从核对"
                "（H-08：候选内容与预筛结论必须一致）")
    cur = hashlib.sha256(p.read_bytes()).hexdigest()
    if cur != bound:
        return (f"候选 {candidate_id} 的登记文件在预筛通过后被改动"
                f"（当前 sha256 {cur[:12]}… vs 预筛绑定的 {str(bound)[:12]}…，"
                "H-08）——改动作为新候选内容须重新 bt candidate check "
                "（重检绑定新内容）后才可在轮次内运行")
    return None


def _round_context(ledger, card, *, actor: str, wanted_gates=(),
                   round_file=None):
    """T63/D32（铁律 16）：研究卡只能属于**已开启**轮次——运行约束的统一
    入口（screen/gate 共用）。返回：

    - dict：轮次上下文（round_id / trial_budget / lockbox_uses_max），
      调用方把它随 record_run 落 runs.round_id 并执行预算约束；
    - None：无轮次约束（actor=calibration 的标定样品运行——不进入研究
      轮次统计，T61；或轮次制之前——卡不带 round 字段且台账 rounds 空，
      runs.round_id 记 NULL）；
    - False：拒绝（原因已打印，调用方退出 1，不产生任何 runs）。

    与 T61 _enforce_candidate_status 协同：状态检查（withdrawn/specimen）
    在前（无台账），轮次检查在后（需台账）——两者语义都保留：撤回卡
    先被 T61 拒；轮次约束只在研究运行上生效。

    H-08（全局审核 2026-10-29）：轮次以**冻结快照**为准——当前轮次文件
    hash 必须与开启轮次入账的 toml_sha256 一致（开轮后改预算/分界/假设
    表/候选名单无效，改动须登记新轮次）；候选 pass 记录绑定的内容 hash
    与当前候选文件一致（改候选须重检）。round_file 显式给出时以它为
    准（调用方已定位预注册文件的路径，如 _prob_preconditions）。
    """
    if actor == "calibration":
        return None                     # 标定样品：不进入研究轮次统计
    if not card.round:
        if ledger.rounds_any():
            print(f"✗ 卡 {card.id} 未声明所属轮次（round/candidate 字段）"
                  "——台账已有研究轮次，铁律 16：bt 拒绝运行不属于已提交"
                  "轮次的卡；请在卡文件补 round/candidate 字段（该轮名单"
                  "内候选）后提交再运行")
            return False
        return None                     # 轮次制之前：rounds 表为空
    row = ledger.round(card.round)
    if row is None:
        print(f"✗ 卡 {card.id} 声明的轮次 {card.round} 未开启（rounds 表无"
              f"此行）——先提交 research/rounds/{card.round}.toml 并跑 "
              f"bt round open {card.round}（T63/D32：先定规则再开卡）")
        return False
    if row["status"] != "open":
        print(f"✗ 轮次 {card.round} 已关闭（closed_utc={row['closed_utc']}）"
              "——关闭后该轮不能再运行；后续试验须预注册新轮次"
              "（research/rounds/R#.toml 提交后 bt round open）")
        return False
    # H-08：冻结快照——开轮后改轮次文件（预算/分界/假设表/评估方式）
    # 一律拒绝；文件缺失同样拒绝（快照无从核对）。
    try:
        rnd_path = (Path(round_file) if round_file is not None
                    else _round_file_path(card))
    except rnd.RoundError as e:
        print(f"✗ {e}")
        return False
    if not rnd_path.is_file():
        print(f"✗ 轮次文件 {rnd_path} 不存在——开启轮次时入账的预注册"
              f"（toml_sha256={str(row['toml_sha256'])[:12]}…）无从核对"
              "（H-08：轮次以冻结快照为准），恢复文件或登记新轮次")
        return False
    file_sha = hashlib.sha256(rnd_path.read_bytes()).hexdigest()
    if file_sha != row["toml_sha256"]:
        print(f"✗ 轮次文件 {rnd_path.name} 与开启时入账的内容不一致"
              f"（sha256 {file_sha[:12]}… vs 台账 {str(row['toml_sha256'])[:12]}"
              "…）——开轮后改预注册（分界/假设族/预算/评估方式）无效"
              "（H-08：轮次以冻结快照为准）；改动作为新轮次登记")
        return False
    candidates = json.loads(row["candidates_json"])
    if card.candidate not in candidates:
        print(f"✗ 卡 {card.id} 的候选 {card.candidate} 不在轮次 {card.round}"
              f" 的候选名单 {candidates} 内（T63/D32：轮次预注册钉死候选"
              "名单，名单外的卡不属于该轮）")
        return False
    if not rnd.candidate_screened_pass(ledger, card.candidate):
        latest = ledger.candidate_checks(card.candidate)
        what = (f"最新预筛结论为 {latest[-1]['decision']!r}"
                if latest else "台账无预筛记录（candidate_checks 空）")
        print(f"✗ 候选 {card.candidate} {what}——screened_pass（最新"
              " decision=pass）是轮次内运行的持续前置（T62/D31+T63）")
        return False
    # H-08：候选 pass 记录绑定候选内容（改候选须重检才生效）；候选登记
    # 目录 = 轮次文件的兄弟目录（research/candidates）
    frozen_err = _candidate_frozen_check(
        ledger, card.candidate, rnd_path.parent.parent / "candidates")
    if frozen_err is not None:
        print(f"✗ {frozen_err}")
        return False
    if "G5" in wanted_gates:
        used = ledger.round_lockbox_uses(card.round)
        if used >= int(row["lockbox_uses_max"]):
            print(f"✗ 轮次 {card.round} 的 G5 次数已达上限（本轮 "
                  f"{used}/{row['lockbox_uses_max']}，lockbox_uses_max；"
                  "计数=该轮内跑过的卡的 lockbox_uses 行数）——拒绝运行")
            return False
    return {"round_id": card.round,
            "trial_budget": int(row["trial_budget"]),
            "lockbox_uses_max": int(row["lockbox_uses_max"])}


def _round_budget_exceeded(ledger, ctx) -> bool:
    """本轮累计去重试验数（round_n_dedup，七元组去重口径）已达
    trial_budget → True（原因已打印；调用方退出 1，不产生任何 runs）。
    保守口径：预算满后连同参重复运行一并拒绝——重复运行不增信息，
    预算的目的是防在同轮反复挑选。"""
    if ctx is None:
        return False
    used = ledger.round_n_dedup(ctx["round_id"])
    if used >= ctx["trial_budget"]:
        print(f"✗ 轮次 {ctx['round_id']} 的累计去重试验数已达预算上限"
              f"（{used}/{ctx['trial_budget']}，trial_budget）——本轮不再"
              "运行新试验（T63/D32：预算在预注册里钉死，用尽即结束；"
              "下一轮须重新预注册）")
        return True
    return False


def _round_power_recheck(ledger, candidates, *, n_tests: int,
                         as_of: str | None = None) -> dict[str, dict]:
    """T84（R13 §4.2 修改 4）：按本轮 N（候选数，Bonferroni α/N）复核
    每个候选的检验力——**只报告**，不阻塞开轮、不改判定。

    数据源 = 台账 candidate_checks 里该候选（as_of 给定时取 checked_at_utc
    ≤ as_of 的）最新一条预筛的 checks.power（sharpe_used/years）——
    开轮输出用当前最新（as_of=None），轮次报告骨架用 as_of=opened_utc
    （开轮时点快照：开轮后的新预筛不改开轮复核）。单次与 α/N 两档检验力
    都用 power.power_one_sided 现算（与预筛同一函数、逐位可复算）；
    预筛本身仍按单次判定（蓝图 §6.2 条件 4），α/N 只进事件模式样本量
    规则与本复核。概率模式候选（无 checks.power，按登记假设条数
    Bonferroni）与无预筛记录的候选如实注明，不报数字。
    """
    out: dict[str, dict] = {}
    for cid in candidates:
        rows = ledger.candidate_checks(cid)
        if as_of is not None:
            rows = [row for row in rows
                    if row["checked_at_utc"] <= as_of]
        if not rows:
            out[cid] = {"note": "无预筛记录（开轮前置已另行拦截）"}
            continue
        try:
            result = json.loads(rows[-1]["result_json"])
            pcheck = (result.get("checks") or {}).get("power") or {}
            sr = float(pcheck["sharpe_used"])
            yrs = float(pcheck["years"])
        except (ValueError, KeyError, TypeError):
            out[cid] = {"note": "概率模式候选（无单次检验力记录）——按登记"
                                "假设条数 Bonferroni（T77 全轮口径），不在"
                                "本复核内"}
            continue
        out[cid] = {
            "decision": rows[-1]["decision"],
            "sharpe_used": sr, "years": yrs, "n_tests": int(n_tests),
            "power_single_test": (power.power_one_sided(sr, yrs)
                                  if yrs > 0.0 else 0.0),
            "power_at_n": (power.power_one_sided(sr, yrs, n_tests=int(n_tests))
                           if yrs > 0.0 else 0.0),
        }
    return out


def cmd_round_open(args) -> int:
    """bt round open R#：前置条件全过 → 写台账 rounds 表（带哈希链）。

    前置条件（T63/D32，铁律 16）：
    - 轮次文件已提交且工作区干净（该文件无未提交改动）；
    - 候选全部 screened_pass（台账 candidate_checks 最新 decision=pass）；
    - lockbox_uses_max ≤ D16 剩余预算（D16：本锁箱 3 次，写死常量）。
    """
    path = _round_path(args.round, Path(args.rounds_dir))
    try:
        r = rnd.load_round(path)
    except rnd.RoundError as e:
        print(f"✗ {e}")
        return 1
    committed = cards.committed_at(path)
    if committed is None:
        print(f"✗ 轮次预注册 {path} 尚未提交到 git（铁律 16：先定规则再"
              "开卡——预注册必须在看到任何结果之前提交）——先 git commit"
              " 轮次文件，再 bt round open")
        return 1
    if not cards.worktree_clean(path):
        print(f"✗ 轮次预注册 {path} 在工作区有未提交改动（git status "
              "--porcelain 非空）——提交（或还原）后再开启；改预算/候选"
              "须在开启前改完并提交，开启即绑定（toml_sha256 入台账）")
        return 1
    with Ledger(args.ledger) as ledger:
        if ledger.round(r.id) is not None:
            print(f"✗ 轮次 {r.id} 已开启过（rounds 表已有此行）——预注册"
                  "是一次性动作，同 id 不得重开；后续轮次用新 R#")
            return 1
        not_pass = [c for c in r.candidates
                    if not rnd.candidate_screened_pass(ledger, c)]
        if not_pass:
            detail = []
            for c in not_pass:
                latest = ledger.candidate_checks(c)
                detail.append(
                    f"{c}（{'最新 decision=' + latest[-1]['decision']
                       if latest else '无预筛记录'}）")
            print(f"✗ 候选 {', '.join(detail)} 不是 screened_pass——轮次的"
                  "全部候选必须最新预筛 decision=pass（bt candidate check）"
                  "才可开启（T62/D31 + T63/D32）")
            return 1
        # P 线小改（审核三）：[hypotheses] 与候选登记的假设交叉校验——
        # 轮次列出的每条假设都必须在候选登记的假设里（预筛/候选侧的
        # 预先声明是假设族的上界；先卡后测，轮次不得引入候选没登记的
        # 假设）
        for cid, hyps in r.hypotheses.items():
            cpath = Path(args.rounds_dir).parent / "candidates" / f"{cid}.toml"
            try:
                registered = cand.load_candidate(cpath)
            except cand.CandidateError as e:
                print(f"✗ 候选 {cid} 登记不可装载（{e}）——[hypotheses] 的"
                      "交叉校验需要候选登记")
                return 1
            declared = set(getattr(registered.prob_declared, "hypotheses",
                                   ()) or ())
            extra = [h for h in hyps if h not in declared]
            if extra:
                print(f"✗ [hypotheses].{cid} 列出了候选登记之外的假设 "
                      f"{extra}——候选登记（{cid}.toml [prob].hypotheses）"
                      "是假设族上界，轮次不得加新假设（先卡后测）")
                return 1
        used = int(ledger.conn.execute(
            "SELECT COUNT(*) n FROM lockbox_uses").fetchone()["n"])
        remaining = max(0, rnd.D16_LOCKBOX_USES_TOTAL - used)
        if r.lockbox_uses_max > remaining:
            print(f"✗ 轮次 {r.id} 申报 lockbox_uses_max={r.lockbox_uses_max}"
                  f"，超过 D16 剩余预算（本锁箱总预算 "
                  f"{rnd.D16_LOCKBOX_USES_TOTAL}，已用 {used}，剩余 "
                  f"{remaining}；D16/D49：H 线与 P 线共用）——轮次的锁箱"
                  "用量申报不得超总预算余额")
            return 1
        # T84：按本轮 N（候选数）复核每个候选的检验力（α/N，只报告）
        recheck = _round_power_recheck(ledger, r.candidates,
                                       n_tests=len(r.candidates))
        ledger.record_round_open(
            round_id=r.id, candidates=r.candidates,
            trial_budget=r.trial_budget,
            lockbox_uses_max=r.lockbox_uses_max,
            dsr_trial_scope=r.dsr_trial_scope, advance_rule=r.advance_rule,
            close_rule=r.close_rule, approved_by=r.approved_by,
            evaluation=r.evaluation, toml_sha256=r.content_sha256(),
            git_commit=current_git_commit())
    print(f"✓ 轮次 {r.id} 已开启（T63/D32）：候选 {list(r.candidates)}，"
          f"试验预算 {r.trial_budget}，锁箱用量上限 {r.lockbox_uses_max}"
          f"（D16 剩余 {remaining}），批准人 {r.approved_by}；台账 "
          f"{args.ledger}（rounds 哈希链，toml_sha256="
          f"{r.content_sha256()[:12]}…）")
    print(f"— 检验力复核（α/N，N={len(r.candidates)}（本轮候选数），只报告"
          "不阻塞——预筛按单次判定，蓝图 §6.2 条件 4）：")
    for cid, d in recheck.items():
        if "power_at_n" not in d:
            print(f"    — {cid}：{d['note']}")
            continue
        flag = "✓" if d["power_at_n"] >= 0.5 else "⚠"
        tail = ("" if d["power_at_n"] >= 0.5
                else "——低于 50%（只报告，不是开轮条件）")
        print(f"    {flag} {cid}：单次 {d['power_single_test']:.0%} → "
              f"α/N 后 {d['power_at_n']:.0%}（sharpe_used "
              f"{d['sharpe_used']:.2f}、{d['years']:.1f} 年）{tail}")
    print(f"— 该轮的卡须带 round = \"{r.id}\" 与 candidate 字段；"
          f"bt round close {r.id} 关闭并生成报告骨架")
    return 0


def cmd_round_status(args) -> int:
    """bt round status R#：轮次状态与预算用量（读台账，不动链）。"""
    with Ledger(args.ledger) as ledger:
        row = ledger.round(args.round)
        if row is None:
            print(f"✗ 轮次 {args.round} 未开启过（rounds 表无此行；先 "
                  f"bt round open {args.round}）")
            return 1
        n_dedup = ledger.round_n_dedup(args.round)
        g5_used = ledger.round_lockbox_uses(args.round)
    print(f"轮次 {row['round_id']}：status={row['status']}")
    print(f"  开启 {row['opened_utc']}（批准人 {row['approved_by']}，"
          f"git {row['git_commit'][:12]}…，toml_sha256="
          f"{row['toml_sha256'][:12]}…）")
    if row["closed_utc"]:
        print(f"  关闭 {row['closed_utc']}")
    print(f"  候选 {json.loads(row['candidates_json'])}；评估 "
          f"{json.loads(row['evaluation_json'])}")
    print(f"  试验预算：{n_dedup}/{row['trial_budget']}（本轮去重试验数/"
          f"trial_budget）；G5 用量：{g5_used}/{row['lockbox_uses_max']}"
          "（lockbox_uses_max）")
    print(f"  推进规则：{row['advance_rule']}")
    print(f"  结束条件：{row['close_rule']}")
    return 0


def _round_report(ledger, row) -> str:
    """round close 的报告骨架：本轮全部运行（含失败）+ 按 advance_rule
    的判定占位。被运行约束拒绝的尝试不产生 runs 行（fail-closed，无迹
    于台账——拒绝不是试验），如实注明。"""
    rid = row["round_id"]
    runs = ledger.conn.execute(
        "SELECT run_id, card_id, symbol, timeframe, actor, metrics_json,"
        " created_utc FROM runs WHERE round_id=? ORDER BY run_id",
        (rid,)).fetchall()
    n_dedup = ledger.round_n_dedup(rid)
    g5_used = ledger.round_lockbox_uses(rid)
    lines = [
        f"# 研究轮次 {rid} 关闭报告（骨架，bt round close 自动生成）",
        "",
        f"- 开启：{row['opened_utc']}（批准人 {row['approved_by']}，"
        f"预注册 toml_sha256={row['toml_sha256'][:12]}…）",
        f"- 候选：{json.loads(row['candidates_json'])}",
        f"- 试验预算：{n_dedup}/{row['trial_budget']}"
        "（本轮去重试验数/trial_budget，七元组去重口径）",
        f"- G5 用量：{g5_used}/{row['lockbox_uses_max']}"
        "（lockbox_uses_max，受 D16 总预算约束）",
        f"- 推进规则（advance_rule）：{row['advance_rule']}",
        f"- 结束条件（close_rule）：{row['close_rule']}",
        "",
        "## 运行清单（本轮全部运行，含失败）",
        "",
        "| run# | 卡 | 品种/周期 | actor | 结果 | 时刻 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in runs:
        payload = json.loads(r["metrics_json"])
        if payload.get("ok"):
            m = payload.get("metrics") or {}
            outcome = (f"✓ 净利 {m.get('net_profit')} / "
                       f"交易 {m.get('trade_count')}")
        else:
            outcome = f"✗ 失败：{payload.get('error')}"
        lines.append(
            f"| {r['run_id']} | {r['card_id']} | {r['symbol']}/"
            f"{r['timeframe']} | {r['actor']} | {outcome} |"
            f" {r['created_utc']} |")
    if not runs:
        lines.append("| （无运行） | | | | | |")
    lines += [
        "",
        "注：被运行约束（轮次未开启/预算满/锁箱上限/已关闭）拒绝的尝试"
        "不产生 runs 行——fail-closed，台账无迹；拒绝不是试验。",
    ]
    # T84：开轮检验力复核（α/N，只报告）——开轮时点最新预筛的快照
    candidates = json.loads(row["candidates_json"])
    recheck = _round_power_recheck(ledger, candidates,
                                   n_tests=len(candidates),
                                   as_of=row["opened_utc"])
    lines += [
        "",
        "## 开轮检验力复核（α/N，只报告）",
        "",
        f"按本轮 N={len(candidates)}（候选数，Bonferroni）对每个候选在"
        "**开轮时点最新**预筛的 sharpe_used 复核检验力"
        "（mt5quant.research.power.power_one_sided，n_tests=N；开轮后的"
        "新预筛不改本节）。预筛判定仍按单次检验（蓝图 §6.2 条件 4），"
        "本节只报告、不构成开轮或判定条件。",
        "",
        "| 候选 | 单次检验力 | α/N 检验力 | sharpe_used | 研究年数 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for cid, d in recheck.items():
        if "power_at_n" not in d:
            lines.append(f"| {cid} | — | — | — | — |（{d['note']}）")
            continue
        lines.append(
            f"| {cid} | {d['power_single_test']:.0%} | {d['power_at_n']:.0%}"
            f" | {d['sharpe_used']:.2f} | {d['years']:.1f} |")
    lines += [
        "",
        "## 按 advance_rule 的判定（占位，关闭时人工填写）",
        "",
    ]
    for cid in json.loads(row["candidates_json"]):
        lines.append(
            f"- {cid}：判定 = 待填写（按推进规则「{row['advance_rule']}」"
            "对照运行清单与主统计量给出：通过 / 不通过 / 无结论）")
    return "\n".join(lines) + "\n"


def cmd_round_close(args) -> int:
    """bt round close R#：生成 docs/research/round-R#-<日期>.md 骨架并
    关闭轮次（open→closed 归档改写入链）；关闭后该轮不能再运行。"""
    with Ledger(args.ledger) as ledger:
        row = ledger.round(args.round)
        if row is None:
            print(f"✗ 轮次 {args.round} 未开启过（rounds 表无此行），无从关闭")
            return 1
        if row["status"] != "open":
            print(f"✗ 轮次 {args.round} 已是 {row['status']!r}——关闭是一次性"
                  "动作，不得二改")
            return 1
        report = _round_report(ledger, row)
        ledger.record_round_close(args.round)
    out = Path(args.out) if args.out else (
        REPO_ROOT / "docs" / "research"
        / f"round-{args.round}-{datetime.now(timezone.utc).date().isoformat()}.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(report, encoding="utf-8")      # 原子写（铁律 15）
    os.replace(tmp, out)
    print(f"✓ 轮次 {args.round} 已关闭（T63/D32）：报告骨架 {out}；"
          "该轮的卡不能再运行（screen/gate 拒），runs 已带 round_id 如实"
          "留痕；后续试验须预注册新轮次")
    return 0


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


def _dedup_trial_sharpes(ledger, card_id: str, bars_per_year: float, *,
                         scope: str = "family") -> tuple[int, list[float]]:
    """T21c/M4：试验夏普按 (strategy, params, symbol, timeframe,
    data_sha256, cost_model_version, strategy_sha) 去重——同一组参数先
    screen 后 gate 只计 1 次；T37：策略代码指纹进键（改代码重跑 = 新
    试验）；失败运行不计。

    scope（T64/D33，[g4].dsr_trial_scope）：
    - "family"（函数缺省，**语义逐位不变**）：谱系（cards.family_of）内
      去重，run_class=migration 不计（与旧 actor=migration 过滤等价）；
      calibration 运行照现行语义计入；
    - "project"：项目级、只计 run_class=research（H 线与 P 线合并；
      calibration/ops/migration 不计——标定与运维不是研究试验）；
    - "rounds_lineage"（T85 冻结 L1，D34 后 gates.toml 的冻结值）：
      ledger.primary_test_n(continues_family) 的门槛部分——全项目已开启
      轮次内的去重研究试验（跨轮跨谱系：run_class=research 且 round_id
      非空）+ 被评估卡谱系的轮次外研究历史；轮次外其余历史/标定/迁移
      不计。试验夏普（经验方差）按 T85 冻结只作诊断——v3 主检验接线
      （T86）前，现行 G4 栈仍按此取数运行。

    返回 (去重后 N, 每根 bar 口径的试验夏普列表)；成功但无夏普的运行计入
    N、不进夏普列表（与 ledger.stats 的 n_dedup 同键口径）。
    """
    if scope not in ("family", "project", "rounds_lineage"):
        raise ValueError(
            f"scope 必须是 family|project|rounds_lineage"
            f"（[g4].dsr_trial_scope，T64/D33 + T85 冻结 L1），"
            f"收到 {scope!r}")
    family = cards.family_of(card_id)
    rows = ledger.conn.execute(
        "SELECT card_id, strategy, params_json, symbol, timeframe,"
        " data_sha256, strategy_sha, cost_model_version, actor, run_class,"
        " round_id, metrics_json FROM runs").fetchall()
    by_key: dict[tuple, float | None] = {}
    for r in rows:
        rc = r["run_class"] or run_class_for_actor(r["actor"])
        rid = r["round_id"]
        if scope == "family":
            if rc == "migration" or cards.family_of(r["card_id"]) != family:
                continue
        elif scope == "project":                # project：只计研究类
            if rc != "research":
                continue
        else:               # rounds_lineage（T85 冻结 L1）：门槛口径
            if rc != "research":
                continue
            # 轮次内全项目计；轮次外只计被评估卡延续的谱系（lineage）
            if not rid and cards.family_of(r["card_id"]) != family:
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


def _stitch_oos_from_segments(spec, bars, costs, segments) -> pd.Series:
    """按 walkforward.run_g3 的同一切片/拼接口径重放各测试窗（G3 记录的
    segments：test_range + 段内选中 params），返回拼接的样本外收益。

    T21a（G5 参照分布）与 T86（关卡 v3 bar 级主检验的 OOS）共用同一
    重放实现：测试窗权益收益（含测试窗首根，去预热段）。"""
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
        raise ValueError("segments 没有可用的测试窗——样本外收益无从拼接")
    return pd.concat(rets, ignore_index=True)


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
    try:
        return _stitch_oos_from_segments(spec, bars, costs, segments)
    except ValueError as e:
        raise ValueError(
            f"卡 {card.id} 的 G3 记录没有可用的 segments——参照分布无从重建"
            f"（{e}）") from e


# ------------------------------------------------ 关卡 v3（T86 bt 接线）

# 谱系 id（与 mt5quant.research.candidates._FAMILY_PATTERN 同式：字母前缀
# + 数字，不带 -v 版本后缀——那是卡 id 不是谱系）
_FAMILY_ID_RE = re.compile(r"^[A-Z]+\d{2,}$")


def _round_eval_mode(card) -> str:
    """轮次预注册 [evaluation] 里该卡候选的评估 mode（T86 事件分支判定：
    mode="event" → 关卡 V3 走 [v3.event] 事件分支；其余/无轮次 → bar 级
    标准分支）。评估方式是预注册内容（附录 D），bt 只读不改。

    H-08：模式文件损坏/缺失 → RoundError（fail-closed，绝不回退 bar 级
    普通模式静默放行——评估方式是预注册约束，读不到即无从判定）。"""
    if not card.round or not card.candidate:
        return ""
    rnd_path = _round_file_path(card)
    round_obj = rnd.load_round(rnd_path)      # RoundError 直接上抛（H-08）
    ev = (round_obj.evaluation or {}).get(card.candidate) or {}
    return str(ev.get("mode", "") or "")


def _declared_continues_family(card, cards_dir) -> tuple[str, str]:
    """候选登记声明的 continues_family（T85/K1；R14 §3 语义提示：候选
    声明的延续谱系与卡自身 family 不同时，**按候选声明取**——lineage
    N 是候选延续的历史谱系，不是卡自己的谱系）。返回 (family, 来源)；无
    候选/候选登记缺失/未声明 → ("", 说明)。声明值非法 → ValueError。"""
    if not card.candidate:
        return "", "卡不带 candidate（轮次制之前）——lineage 按全新机制计"
    p = Path(cards_dir).parent / "candidates" / f"{card.candidate}.toml"
    if not p.is_file():
        return "", f"候选登记缺失（{p}）——lineage 按全新机制计"
    import tomllib
    with open(p, "rb") as f:
        data = tomllib.load(f)
    fam = str(data.get("continues_family", "") or "").strip()
    if fam and not _FAMILY_ID_RE.match(fam):
        raise ValueError(
            f"{p}：continues_family {fam!r} 必须是谱系 id（形如 H012——不带"
            "-v 版本后缀，那是卡 id 不是谱系；T85/K1）")
    return fam, f"按候选登记 {p.name} 声明（≠卡自身谱系时以声明为准）"


def _v3_n_composition(ledger, card, cards_dir) -> dict:
    """关卡 v3 主检验 N 的组成（T85 冻结 L1：rounds_lineage =
    ledger.primary_test_n 的门槛部分）。轮次内 + 候选声明谱系的轮次外
    历史；其余历史只报告。来源注记随组成一并落关卡 details。"""
    fam, source = _declared_continues_family(card, cards_dir)
    comp = dict(ledger.primary_test_n(fam))
    comp["continues_family_source"] = source
    return comp


def _v3_gate_evaluate(ledger, card, *, cfg, ctx, cards_dir, strategy, spec,
                      bars, costs, params, base, summary, contract_size,
                      symbol, combos, g0_result, g3_result, data_sha, seed,
                      known_gaps) -> dict:
    """关卡 V3 的评估体（T86 接线；bt gate 的 --gates V3 生产入口）。

    bar 级分支（缺省）：主检验按 [v3].primary_test 分派（冻结 = dsr_v0，
    run_primary_test 取 N_eff = max(N, n_min)）+ 三项否决（成本 ×1.5 /
    延迟一根 / 纯多头相对买入持有超额）+ 诊断只报告（含台账试验夏普的
    经验方差 DSR——冻结后只进诊断）。OOS = G3 拼接的样本外收益（本次
    运行里有 G3 用其 segments，否则读台账最近一次通过的 G3 记录——
    与 G5 参照分布同一重放实现；两处都无 → 主检验 fail-closed）。

    事件分支（轮次 [evaluation] mode="event"，[v3.event]/D35）：主检验 =
    随机化参照 p ≤ α/N（N 不吃 n_min——随机化检验 N=1 也精确）；否决只
    有成本 ×1.5（净均值 > 0）；买入持有超额与延迟一根**不适用不运行**；
    四项诊断只报告（vevents.v3_event_diagnostics）。随机化次数下限已在
    入口预检（低于下限拒绝运行），此处按已定次数执行。
    """
    comp = _v3_n_composition(ledger, card, cards_dir)
    n_raw = int(comp["n"])
    g0 = g0_result if g0_result is not None else vlook.run_g0(
        strategy, bars, params)
    if ctx.get("mode") == "event":
        from mt5quant.validation import events as vevents
        kind = ctx["kind"]
        side = ctx["side"]
        draws = int(ctx["sims"])
        draws_min = int(ctx["draws_min"])
        days = pd.DatetimeIndex(sorted(set(
            pd.to_datetime(bars["time"]).dt.normalize())))
        if kind == "month_end":
            events = vevents.month_end_events(days)
        else:                      # london_fix：月末定盘锚（与 T66 同构）
            anchors = vevents.month_end_events(days, last_n=1, first_n=1)
            events = vevents.london_fix_events(
                pd.DatetimeIndex([e.entry_time for e in anchors]))
        side_fn = (lambda event: side)          # noqa: E731
        threshold = float(cfg.alpha) / max(1, n_raw)
        rat = vevents.randomization_test(
            bars, events, side_fn=side_fn, costs=costs, symbol=symbol,
            trading_days=days, sims=draws, seed=seed,
            contract_size=contract_size)
        primary = {
            "passed": bool(float(rat["p_value"]) <= threshold),
            "test": "randomization", "p_value": float(rat["p_value"]),
            "threshold": threshold, "alpha": float(cfg.alpha),
            "n_tests": n_raw, "sims": draws, "draws_min": draws_min,
            "n_events": int(rat["n_events"]),
            "observed_mean": float(rat["observed_mean"]),
            "null_mean": float(rat["null_mean"]),
            "event_kind": kind, "event_side": side,
            "note": "随机化参照（D35 修改 1）；N 口径同 [v3] 主检验"
                    "（rounds_lineage），不吃 n_min",
        }
        frame15 = vevents.event_returns(
            bars, events, side_fn=side_fn,
            costs=vrob.ScaledCosts(costs, float(cfg.cost_scale_veto)),
            symbol=symbol, contract_size=contract_size)
        cost_scale = {
            "passed": bool(len(frame15)
                           and float(pd.to_numeric(frame15["net"]).mean()) > 0),
            "net_mean_scaled": (float(pd.to_numeric(frame15["net"]).mean())
                                if len(frame15) else None),
            "scale": float(cfg.cost_scale_veto), "n_events": int(len(frame15)),
            "note": "事件模式否决=成本 ×1.5 后净均值 > 0（D35 修改 2）"}
        not_applicable = ("事件模式不适用（D35 修改 2：[v3.event]."
                          "{0}_veto = false——按时钟定义的事件窗上"
                          "'{1}'会误杀）")
        if cfg.event.delay_bars_veto:
            delay = vgate.v3_veto_delay(spec, bars, costs, params, cfg)
        else:
            delay = {"passed": True, "applicable": False,
                     "note": not_applicable.format(
                         "delay_bars", "延迟一根落到定盘后反转段")}
        if cfg.event.buyhold_excess_veto:
            buyhold = vgate.v3_veto_buyhold(
                spec, bars, costs, base.trades, summary.get("sharpe"), cfg)
        else:
            buyhold = {"passed": True, "applicable": False,
                       "note": not_applicable.format(
                           "buyhold_excess", "真实月末效应夏普通常低于全程"
                           "持有")}
        diagnostics = vevents.v3_event_diagnostics(
            bars, events, side_fn=side_fn, costs=costs, symbol=symbol,
            contract_size=contract_size, known_gaps=known_gaps,
            sims=int(cfg.bootstrap_sims), seed=seed)
        verdict = vgate.evaluate_v3(
            g0=g0, primary=primary, cost_scale=cost_scale, delay=delay,
            buyhold=buyhold, diagnostics=diagnostics)
        return {
            "passed": bool(verdict["passed"]),
            "mode": "event",
            "primary": primary,
            "vetoes": {"cost_scale_x15": cost_scale, "delay_1_bar": delay,
                       "buyhold_excess": buyhold},
            "n_composition": comp, "veto": verdict["veto"],
            "failed_vetoes": verdict["failed_vetoes"],
            "diagnostics": diagnostics,
            "checks": {"G0": {"passed": bool(g0["passed"])},
                       "primary": {"passed": bool(primary["passed"])},
                       "cost_scale_x15": {"passed": bool(cost_scale["passed"])},
                       "delay_1_bar": {"passed": bool(delay["passed"])},
                       "buyhold_excess": {"passed": bool(buyhold["passed"])}},
        }

    # ---- bar 级分支（[v3].primary_test，冻结 = dsr_v0）
    oos, oos_source, oos_reason = None, None, None
    if g3_result is not None and g3_result.get("segments"):
        try:
            oos = _stitch_oos_from_segments(
                spec, bars, costs, g3_result["segments"])
            oos_source = "G3（本次运行的 segments，内存重放）"
        except ValueError as e:
            oos_reason = str(e)
    else:
        try:
            oos = _g3_reference_returns(ledger, card, spec, bars, costs,
                                        data_sha=data_sha)
            oos_source = "台账最近一次通过的 G3 记录（与 G5 参照同源）"
        except ValueError:
            oos_reason = (
                f"卡 {card.id} 无（数据哈希相符且通过的）G3 关卡记录——"
                "关卡 v3 bar 级主检验的数据源 = G3 拼接的样本外收益；"
                "请在同一数据上以 --gates G3,V3 重跑")
    if oos is None:
        primary = {"passed": False, "test": str(cfg.primary_test),
                   "probability": None, "reason": f"OOS 无从取得：{oos_reason}"
                   "（关卡 v3 bar 级主检验的数据源 = G3 拼接的样本外收益——"
                   "先在同数据上跑 G3）"}
    else:
        primary = vgate.run_primary_test(oos, n_trials=n_raw, cfg=cfg,
                                         seed=seed)
    cost_scale = vgate.v3_veto_cost_scale(spec, bars, costs, params, cfg)
    delay = vgate.v3_veto_delay(spec, bars, costs, params, cfg)
    buyhold = vgate.v3_veto_buyhold(spec, bars, costs, base.trades,
                                    summary.get("sharpe"), cfg)
    diagnostics: dict = {}
    if oos is not None:
        bpy = summary.get("annualization_bars_per_year") \
            or bars_per_year(card.timeframe)
        _, diag_sharpes = _dedup_trial_sharpes(
            ledger, card.id, float(bpy), scope="rounds_lineage")
        # T85 冻结 L1：台账试验夏普的经验方差只进诊断（对照锚点语义）
        diagnostics["dsr_empirical_variance"] = vgate.v3_primary_dsr(
            oos, n_trials=vgate.v3_n_eff(n_raw, cfg),
            trial_sharpes=diag_sharpes, cfg=cfg)
    diagnostics["g1_sample_line"] = vgate.run_g1(
        base.trades, timeframe=card.timeframe, cfg=cfg)
    if combos:                     # 卡网格非空才算参数邻域诊断
        diagnostics["param_plateau"] = vrob.param_plateau(
            spec, bars, costs, params, {k: list(v)
                                       for k, v in card.params.items()}, cfg)
    diagnostics["yearly_segments"] = vrob.yearly_segments(base.trades, cfg)
    diagnostics["drop_best_5pct"] = vrob.drop_best_check(base.trades, cfg)
    diagnostics = {k: v for k, v in diagnostics.items() if v is not None}
    verdict = vgate.evaluate_v3(
        g0=g0, primary=primary, cost_scale=cost_scale, delay=delay,
        buyhold=buyhold, diagnostics=diagnostics)
    return {
        "passed": bool(verdict["passed"]),
        "mode": "bar",
        "primary": primary,
        "oos_source": oos_source,
        "n_eff": int(vgate.v3_n_eff(n_raw, cfg)),
        "vetoes": {"cost_scale_x15": cost_scale, "delay_1_bar": delay,
                   "buyhold_excess": buyhold},
        "n_composition": comp, "veto": verdict["veto"],
        "failed_vetoes": verdict["failed_vetoes"],
        "diagnostics": diagnostics,
        "checks": {"G0": {"passed": bool(g0["passed"])},
                   "primary": {"passed": bool(primary["passed"])},
                   "cost_scale_x15": {"passed": bool(cost_scale["passed"])},
                   "delay_1_bar": {"passed": bool(delay["passed"])},
                   "buyhold_excess": {"passed": bool(buyhold["passed"])}},
    }


def _g5_orchestrate(ledger, card, *, strategy, spec, params, base_summary,
                    bars, costs, session_policy, data_root, approved_by,
                    cfg, trial, round_id=None, round_lockbox_uses_max=None
                    ) -> dict:
    """G5 编排（T21a 协议，T21e 接线；H-02/H-03 修订）：

    1. 参照重建（_g3_reference_returns，只读研究段；失败不消耗一次性）；
    2. begin_lockbox：读锁箱数据**之前**原子占位——trial（H-02）绑定当前
       试验身份（参数/数据哈希/成本版本/代码指纹/冻结门槛 + 当前通过的
       G3/V3 判决；历史成功参数/旧成本/旧代码不授权），项目预算/轮次上限/
       卡一次性的检查与 started 写入同一事务（H-03）。未批准/绑定不满足/
       已用过/预算用尽（T27：cutoff 级 max_uses，默认 5）→ 如实抛
       GateApprovalError / LockboxPrerequisiteError /
       LockboxAlreadyUsedError / LockboxBudgetExhaustedError，
       不消耗或已消耗由语义决定；
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
    # 2) 读锁箱数据之前原子占位（中途崩溃也不允许重跑）；cutoff 与
    #    load_lockbox_window 同一来源（lockbox.json），作为预算计数口径
    from mt5data.lockbox import cutoff_for, load_config
    lb_cutoff = cutoff_for(load_config(data_root), spec.symbol,
                           card.timeframe)
    vgate.begin_lockbox(ledger, card, approved_by=approved_by,
                        cutoff=lb_cutoff, trial=trial, round_id=round_id,
                        round_lockbox_uses_max=round_lockbox_uses_max)
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

_ACCOUNT_PROFILE_STATUSES = ("charged", "swap_free", "unknown")


def load_account_profile(path=None) -> dict[str, str]:
    """账户隔夜费画像装载器（T83/R13-K6，config/account-profile.toml）。

    返回**生效**条目 {symbol: "swap_free"}（只含确认过的免隔夜费品种）；
    不在返回里的品种一律按 charged（缺省保守）。规则：
    - 文件不存在 → {}（全 charged——未配置即未确认）；
    - 条目 status="swap_free" 且 confirmed_by 非空（用户确认过）→ 生效；
    - status=charged/unknown、或 swap_free 未填确认人 → charged（不生效）；
    - status 非法 → ValueError（fail-fast，不猜）；
    - [account] 表只是将来实盘账户类型的登记，装载不消费（预筛决定只看
      品种级状态）。
    """
    import tomllib

    p = Path(path) if path is not None else ACCOUNT_PROFILE_PATH
    if not p.is_file():
        return {}
    with open(p, "rb") as f:
        doc = tomllib.load(f)
    symbols = doc.get("symbols", {})
    if not isinstance(symbols, dict):
        raise ValueError(f"{p}：[symbols] 须是表（symbol 名 → 状态条目）")
    effective: dict[str, str] = {}
    for sym, entry in sorted(symbols.items()):
        if not isinstance(entry, dict):
            raise ValueError(f"{p}：[symbols.{sym}] 须是表（status/确认人等）")
        status = entry.get("status")
        if status not in _ACCOUNT_PROFILE_STATUSES:
            raise ValueError(
                f"{p}：[symbols.{sym}].status 须为 "
                f"{'/'.join(_ACCOUNT_PROFILE_STATUSES)}，收到 {status!r}")
        confirmed_by = str(entry.get("confirmed_by") or "").strip()
        if status == "swap_free" and confirmed_by:
            effective[str(sym)] = "swap_free"
    return effective


def _swap_ref_prices(data_root, symbols, timeframe: str) -> dict[str, float]:
    """各品种研究库最后收盘（T83/K5：build_costs 的 swap_ref_price）。

    与 CLI 主路径同一约定（装载序列最后收盘）：优先候选持仓周期，该周期
    缺序列时退回该品种任一可用周期（参考价只用于点数费率的相对化基准，
    与周期无关——与 candidates._reference_price 同一 fallback 语义）。
    """
    from mt5data.store import load_bars

    refs: dict[str, float] = {}
    for sym in symbols:
        bars = load_bars(str(data_root), sym, timeframe)
        if bars is None or len(bars) == 0:
            sym_dir = Path(data_root) / "bars" / str(sym)
            bars = None
            if sym_dir.is_dir():
                for tdir in sorted(sym_dir.iterdir()):
                    if tdir.is_dir():
                        bars = load_bars(str(data_root), sym, tdir.name)
                        if bars is not None and len(bars) > 0:
                            break
        if bars is None or len(bars) == 0:
            raise ValueError(
                f"研究库无 {sym} 任何周期的 K 线——swap_ref_price（T83/K5 "
                "点数模式隔夜费相对基准）无从取；检查数据根或先导出研究库")
        refs[sym] = float(pd.to_numeric(bars["close"]).iloc[-1])
    return refs


def _swap_free_costs(costs, symbols):
    """swap_free 口径的成本模型（T83/R13-K6 预筛双口径）：同一份点差/
    滑点/佣金，隔夜费规则全部置 0（用户确认免隔夜费时的口径）。"""
    from mt5quant.costs import Costs, SwapModel, SwapRule

    return Costs(
        spread=costs.spread,
        slippage_model=costs.slippage_model,
        commission=costs.commission,
        swap=SwapModel(per_symbol={
            str(s): SwapRule(long_per_day=0.0, short_per_day=0.0)
            for s in symbols}),
        overshoot_model=getattr(costs, "overshoot_model", None))


def _merged_costs(data_root, symbols, *, spread_fraction: float,
                  commission: float, research_mode: bool,
                  swap_ref_prices: dict[str, float] | None = None):
    """候选预筛用的成本模型 v3：逐品种 build_costs 后合并成一个聚合体。

    预筛只信**缓存文件**的成本模型（与引擎同一缓存；tick 水位新鲜度由
    导出/构建侧把关），因此按研究库语义加载（build_costs research_store
    语义：缺缓存报错指路，不读 tick、不静默零成本）。合并：SpreadModel.
    merge（多品种点差）、CommissionModel per_symbol、SwapModel per_symbol
    规则、滑点按同一 spread_fraction 重建。返回 (Costs, cost_version)——
    版本串取各品种版本的去重拼接（进 precheck.json 的成本身份）。

    swap_ref_prices（T83/R13-K5）：{symbol: 研究库该品种最后收盘}——点数
    模式隔夜费折相对口径（与 CLI 主路径/bt screen/gate 同一约定），
    cost_version 带 relref。预筛的事前成本数字本就按预筛自己的参考价折算
    （candidates._reference_price），不随本参数变（有回归测试钉住逐位不变）。
    """
    from mt5quant.costs import CommissionModel, Costs, SlippageModel, SwapModel

    spread = None
    comm_by_sym: dict[str, float] = {}
    rules: dict[str, object] = {}
    versions: list[str] = []
    for sym in symbols:
        ref = (swap_ref_prices or {}).get(sym)
        costs_i, ver_i = build_costs(
            data_root, sym, spread_fraction, commission,
            research_store=True,
            swap_ref_price=(None if ref is None else float(ref)))
        versions.append(ver_i)
        if spread is None:
            spread = costs_i.spread
        elif set(symbols) <= set(spread._per_symbol):
            # 全品种点差缓存（T97 实测：research_store 的 spread_model.json
            # 覆盖全部品种）——逐品种模型重复包含未请求品种，merge 会因
            # 重复抛错；首个模型已覆盖全部所需品种时直接沿用（多品种预筛
            # 首次启用时暴露；单品种路径不经过此分支，H 线行为不变）
            pass
        else:
            spread = spread.merge(costs_i.spread)
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


def cmd_segment_propose(args) -> int:
    """T76/D50：发现段/验证段分界提案（各品种共同覆盖中点 → 月初）。"""
    symbols = [x.strip() for x in args.symbols.split(",") if x.strip()]
    if not symbols:
        print("✗ --symbols 为空")
        return 1
    try:
        data_root, _store_kind = _resolve_store(args)
    except ValueError as e:
        print(f"✗ {e}")
        return 1
    try:
        split = cand.propose_split(data_root, symbols, args.timeframe)
    except ValueError as e:
        print(f"✗ {e}")
        return 1
    print(f"分界提案（{','.join(symbols)} {args.timeframe.upper()} 共同覆盖"
          f"中点，取整到月初）：{split.date().isoformat()}")
    print("写入第一个 P 线轮次预注册的 [segments].split（T77）后生效"
          "（D50：写死不再改）")
    return 0


def _prob_use_merged(prob_conf) -> bool:
    """T97 波次 2：[prob] 段任一新键（max_gap_days / cost_scenario_mult /
    cost_scenario_reference）非 None → 概率分支改用 merged_research_series
    （ext 前向延长 + 洞集合）；不带新键的候选/卡走原 load_series（探针
    兼容硬约束：行为逐位不变）。prob_conf 为 ProbDeclared 或卡的 prob dict。"""
    def _get(k):
        if isinstance(prob_conf, dict):
            return prob_conf.get(k)
        return getattr(prob_conf, k, None)
    return any(_get(k) is not None for k in
               ("max_gap_days", "cost_scenario_mult", "cost_scenario_reference"))


def _prob_load_bars(data_root, sym, tf, *, use_merged: bool, end=None):
    """概率分支的序列装载（T97 波次 2）：use_merged → merged_research_series
    四元组，返回 (bars, gap_spans|None)；否则原 load_series（gap_spans=None，
    与 T97 之前逐位一致）。空序列与 load_series 同语义 fail-fast。

    T98/P-01：merged 路径不再丢弃 end——end 传给 merged_research_series，
    与全局锁箱 cutoff / ext manifest research_cutoff / 导出 source_cutoff /
    MT5 水位一起取最严（end 比其他界晚则被支配，绝不读出锁箱段；口径见
    mt5data.ext.merge）。"""
    if not use_merged:
        return load_series(data_root, sym, tf, end), None
    from mt5data.ext.merge import merged_research_series
    bars, _tags, gap_spans, _bad = merged_research_series(data_root, sym, tf,
                                                          end=end)
    if bars is None or len(bars) == 0:
        raise ValueError(f"无可用数据：{sym} {tf}（合并研究序列为空——"
                         "检查研究库 bars/ 与 ext/ 数据）")
    return bars, (gap_spans or None)


def _candidate_check_prob(args, candidate, data_root, costs, active):
    """T76：概率模式候选预筛——逐品种×局面计数（不看结果）→ 发现段子集
    → precheck_prob。分界：--split 显式给出，否则 propose_split 自动提案。

    T97 波次 2：候选带 [prob] 新键（max_gap_days 等）时改用
    merged_research_series 取数并按 gap_spans 抑制跨洞触发；era 判定
    （ext 触发 = 时刻 < bars meta first_time）在 precheck_prob 内部读取。"""
    from mt5quant.prob import counting as prob_counting
    from mt5quant.strategies.library.prob.actions import ActionParams
    from mt5quant.strategies.library.prob.situations import Approach

    pdecl = candidate.prob_declared
    split_raw = getattr(args, "split", None)
    if split_raw:
        split = pd.Timestamp(str(split_raw))
        if split.day != 1:
            print(f"✗ --split 必须是月初（收到 {split.date()}；"
                  "用 bt segment propose 生成提案）")
            raise SystemExit(1)
    else:
        split = cand.propose_split(data_root, candidate.symbols,
                                   candidate.holding)
    approach = Approach(**pdecl.approach)
    aparams = ActionParams(breakout_atr=pdecl.breakout_atr,
                           retest_window_bars=pdecl.retest_window_bars)
    use_merged = _prob_use_merged(pdecl)
    parts = []
    for sym in candidate.symbols:
        bars, gap_spans = _prob_load_bars(data_root, sym, candidate.holding,
                                          use_merged=use_merged, end=None)
        for sit in pdecl.situations:
            grid = pdecl.level_grid.get(sym) if sit == "S1" else None
            parts.append(prob_counting.count_triggers(
                bars, symbol=sym, situation=sit, approach=approach,
                action_params=aparams, grid_step=grid,
                gap_spans=gap_spans))
    nonempty = [p for p in parts if len(p)]
    counted = pd.concat(nonempty, ignore_index=True) if nonempty else None
    disc = (None if counted is None
            else prob_counting.triggers_before(counted, split))
    return cand.precheck_prob(candidate, data_root=data_root,
                              cost_model=costs, counted=disc, split=split,
                              active_mechanisms=active)


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
        costs, cost_version, years, segs, active = \
            None, "n/a（非研究候选）", {}, None, set()
        result = cand.precheck(candidate, data_root=data_root,
                               cost_model=costs, research_years=years,
                               active_mechanisms=active,
                               research_coverage_segments=segs)
    else:
        try:
            # T83/K5：点数模式隔夜费相对基准 = 研究库该品种最后收盘
            swap_refs = _swap_ref_prices(data_root, candidate.symbols,
                                         candidate.holding)
            costs, cost_version = _merged_costs(
                data_root, candidate.symbols,
                spread_fraction=args.spread_fraction,
                commission=args.commission, research_mode=True,
                swap_ref_prices=swap_refs)
        except Exception as e:
            print(f"✗ 成本模型装配失败：{type(e).__name__}: {e}")
            return 1
        active = _active_mechanism_classes(Path(candidate.path).parent,
                                           candidate.id)
        if candidate.prob_declared is not None:
            # T76：概率模式（price_levels）——计数（只数触发不看结果）+
            # 分界提案，走 precheck_prob；覆盖年数/事件模式条件不适用
            result = _candidate_check_prob(args, candidate, data_root,
                                           costs, active)
            segs, years = None, {}
        else:
            # T80/G3：主段 + 已批准外部序列（fred/cdd，D36 白名单）逐段列出、
            # 合并年数按**区间并集**（重叠不双计，审核修正）；未批准来源
            # （histdata）在场不计
            segs = cand.coverage_segments(data_root, candidate.symbols,
                                          [candidate.holding])
            years = {k: cand.merged_coverage_years(v) for k, v in segs.items()}
            # T83/R13-K6 预筛双口径：持仓过夜的候选同时算 charged 与
            # swap_free 两口径，决定按 account-profile 状态取
            result = cand.precheck(candidate, data_root=data_root,
                                   cost_model=costs, research_years=years,
                                   active_mechanisms=active,
                                   research_coverage_segments=segs)
            if candidate.declared is not None \
                    and candidate.declared.overnight_nights > 0:
                result_free = cand.precheck(
                    candidate, data_root=data_root,
                    cost_model=_swap_free_costs(costs, candidate.symbols),
                    research_years=years, active_mechanisms=active,
                    research_coverage_segments=segs)
                profile = load_account_profile()
                sym_status = {s: profile.get(s, "charged")
                              for s in candidate.symbols}
                basis = ("swap_free"
                         if all(v == "swap_free" for v in sym_status.values())
                         else "charged")
                picked = result_free if basis == "swap_free" else result
                dual_block = {
                    "applicable": True,
                    "account_profile": sym_status,
                    "decision_basis": basis,
                    "charged": {
                        "multiple": result.checks.get(
                            "ex_ante_cost", {}).get("multiple"),
                        "power": result.checks.get("power", {}).get("power"),
                        "decision": result.decision,
                    },
                    "swap_free": {
                        "multiple": result_free.checks.get(
                            "ex_ante_cost", {}).get("multiple"),
                        "power": result_free.checks.get(
                            "power", {}).get("power"),
                        "decision": result_free.decision,
                    },
                    # swap_free 口径通过而 charged 口径不通过 → 该候选的
                    # 通过依赖账户免隔夜费状态，登记 swap_dependent
                    "swap_dependent": bool(
                        result_free.decision == "pass"
                        and result.decision != "pass"),
                }
                result, extra_reasons = picked, []
                if dual_block["swap_dependent"]:
                    extra_reasons.append(
                        "swap_dependent=true：swap_free 口径"
                        f"（倍数 {dual_block['swap_free']['multiple']:.2f}）"
                        "通过而 charged 口径（倍数 "
                        f"{dual_block['charged']['multiple']:.2f}）不通过——"
                        "该候选的结论依赖账户免隔夜费状态"
                        "（config/account-profile.toml 用户确认条目）")
                if basis == "swap_free":
                    cost_version += "+swapfree(account-profile)"
            else:
                dual_block = {
                    "applicable": False,
                    "note": "候选不过夜（overnight_nights=0）：隔夜费恒 0，"
                    "双口径无差别",
                }
                extra_reasons = []
    payload = result.to_payload()
    payload["cost_model_version"] = cost_version
    payload["checked_at_utc"] = datetime.now(timezone.utc).isoformat()
    # H-08：预筛结论绑定候选内容——pass 记录锚定登记文件的 sha256，
    # 轮次内运行（_round_context）比对当前文件 hash：改候选须重检后才生效
    payload["candidate_sha256"] = hashlib.sha256(
        Path(candidate.path).read_bytes()).hexdigest()
    if candidate.status not in cand.BOOKKEEPING_STATUSES \
            and candidate.prob_declared is None:
        payload["swap_basis"] = dual_block
        payload["reasons"].extend(extra_reasons)

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
    sb = payload.get("swap_basis") or {}
    if sb.get("applicable"):
        def _fmt2(x):
            return "—" if x is None else f"{x:.2f}"

        def _fmt0(x):
            return "—" if x is None else f"{x:.0%}"

        print(f"  双口径（T83）：charged 倍数 {_fmt2(sb['charged']['multiple'])}"
              f"/检验力 {_fmt0(sb['charged']['power'])}；swap_free 倍数 "
              f"{_fmt2(sb['swap_free']['multiple'])}/检验力 "
              f"{_fmt0(sb['swap_free']['power'])}；决定按 "
              f"{sb['decision_basis']}（account-profile）"
              + ("；swap_dependent=true" if sb["swap_dependent"] else ""))
    pp = payload["checks"].get("power_prob")
    if pp is not None:
        # D52 解析 + T75 实测（T76 收尾接线）：逐假设显示两口径与取严结果
        n_meas = pp.get("n_required_measured")
        n_used = pp.get("n_required_used")
        meas_s = "未覆盖（Δp<0.02，恒待数据）" if n_meas is None else str(n_meas)
        used_s = "恒待数据（实测未覆盖）" if n_used is None else str(n_used)
        strict_s = "；开区间档——须严格大于" if pp.get("open_ended") else ""
        for h, d in pp.get("per_hypothesis", {}).items():
            mark = "✓" if d["passed"] else "⏳"
            print(f"  {mark} 假设 {h}：触发日数 {d['trigger_days']} / "
                  f"样本量要求 {used_s}（解析 {pp['n_required_analytic']} / "
                  f"T75 实测 {meas_s}，取严{strict_s}；{pp['n_tests']} 条假设 "
                  f"Bonferroni；分界 {pp['split']}；单位=触发日）")
        if not pp.get("per_hypothesis"):
            print(f"  ⏳ 无触发计数（样本量要求 {used_s}——解析 "
                  f"{pp['n_required_analytic']} / T75 实测 {meas_s}，取严"
                  f"{strict_s}；分界 {pp['split']}；单位=触发日）")
        ec = payload["checks"].get("ex_ante_cost_prob", {})
        if ec.get("cost_r_by_symbol"):
            cs_map = ec["cost_r_by_symbol"]
            dearest = max(cs_map, key=cs_map.get)
            print(f"  ΔEV {ec['edge_ev_r']:.4f}R vs 逐品种 3×成本（最贵 "
                  f"{dearest} 3×{cs_map[dearest]:.4f}R）"
                  + (f"；不满足：{ec['failing_symbols']}"
                     if ec.get("failing_symbols") else "；全部品种满足"))
    for r in payload["reasons"]:
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

class _FittedFactory:
    """fittable 策略的工厂（T73A）：实例化返回装好**冻结**模型的新实例；
    类属性透传（validate_params/required_data 等按类使用的地方不变）。"""

    def __init__(self, cls, model):
        self._cls, self._model = cls, model

    def __call__(self):
        return self._cls().with_model(self._model)

    def __getattr__(self, name):
        return getattr(self._cls, name)


def _check_card_strategy_constants(card, strategy_cls) -> str | None:
    """审核三 D1：卡的 [prob] 与策略类常量逐项核对——不一致返回错误信息
    （bracket/situations/level_grid/approach/breakout_atr/retest）。
    防止卡上登记的检验配置与实际运行的策略配置脱节。"""
    prob = card.prob
    if prob is None:
        return None
    pairs = [
        ("bracket.sl_atr", prob["bracket"]["sl_atr"], "SL_ATR"),
        ("bracket.tp_atr", prob["bracket"]["tp_atr"], "TP_ATR"),
        ("bracket.horizon_bars", prob["bracket"]["horizon_bars"],
         "HORIZON_BARS"),
        ("breakout_atr", prob["breakout_atr"], "BREAKOUT_ATR"),
        ("retest_window_bars", prob["retest_window_bars"],
         "RETEST_WINDOW_BARS"),
    ]
    for pkey, pval, cls_attr in pairs:
        cval = getattr(strategy_cls, cls_attr, None)
        if cval is None or float(pval) != float(cval):
            return (f"卡 [prob].{pkey}={pval!r} 与策略类常量 "
                    f"{cls_attr}={cval!r} 不一致——检验配置脱节，拒绝运行")
    sit_c = tuple(getattr(strategy_cls, "SITUATIONS", ()) or ())
    if tuple(prob["situations"]) != tuple(sit_c):
        return (f"卡 [prob].situations={list(prob['situations'])} 与策略类 "
                f"SITUATIONS={list(sit_c)} 不一致——拒绝运行")
    grid_c = getattr(strategy_cls, "LEVEL_GRID", None) or {}
    for sym, step in prob["level_grid"].items():
        if float(grid_c.get(sym, 0.0)) != float(step):
            return (f"卡 [prob].level_grid[{sym}]={step!r} 与策略类 "
                    f"LEVEL_GRID[{sym}]={grid_c.get(sym)!r} 不一致——拒绝运行")
    ap = prob["approach"]
    cls_ap = getattr(strategy_cls, "APPROACH", None)
    if cls_ap is None:
        return "策略类缺 APPROACH 常量——与卡 [prob].approach 无法核对"
    for k, attr in (("lookback_bars", "lookback_bars"),
                    ("min_distance_atr", "min_distance_atr"),
                    ("touch_atr", "touch_atr")):
        if float(ap[k]) != float(getattr(cls_ap, attr)):
            return (f"卡 [prob].approach.{k}={ap[k]!r} 与策略类 "
                    f"APPROACH.{attr}={getattr(cls_ap, attr)!r} 不一致——"
                    "拒绝运行")
    # 审核四 E2：五个决策字段（写了才核对；None=卡未写，跳过）
    feat_c = tuple(getattr(strategy_cls, "FEATURES", ()) or ())
    if prob.get("features") is not None             and tuple(prob["features"]) != feat_c:
        return (f"卡 [prob].features={list(prob['features'])} 与策略类 "
                f"FEATURES={list(feat_c)} 不一致——拒绝运行")
    for pkey, cls_attr in (("prior_strength", "PRIOR_STRENGTH"),
                           ("min_cell", "MIN_CELL")):
        v = prob.get(pkey)
        c = getattr(strategy_cls, cls_attr, None)
        if v is not None and (c is None or int(v) != int(c)):
            return (f"卡 [prob].{pkey}={v!r} 与策略类 {cls_attr}={c!r} "
                    "不一致——拒绝运行")
    pol = getattr(strategy_cls, "POLICY", None)
    if pol is None:
        return "策略类缺 POLICY 常量——lb_z/门槛无法核对"
    lb = prob.get("lb_z")
    if lb is not None and float(lb) != float(pol.z):
        return (f"卡 [prob].lb_z={lb!r} 与策略类 POLICY.z={pol.z!r} "
                "不一致——拒绝运行（审核四 E2/R7 后半）")
    tm = prob.get("entry_threshold_cost_mult")
    if tm is not None and float(tm) != float(pol.threshold_mult):
        return (f"卡 [prob].entry_threshold_cost_mult={tm!r} 与策略类 "
                f"POLICY.threshold_mult={pol.threshold_mult!r} 不一致——"
                "拒绝运行")
    return None


def _reject_probability_card_backtest(card) -> str | None:
    """审核三 D1：probability 卡只走 bt prob test（P1 现象检验），
    screen/gate 拒绝——两套评估口径不可混用。"""
    if card.evaluation_mode == "probability":
        return ("probability 卡（P1 现象检验）不跑 screen/gate——"
                "Δp 检验经 bt prob test（T90），关卡评估属 P2 决策卡"
                "（standard+validation，T92）")
    return None


def _slice_validation_eval(card, round_obj, bars_by_sym, *,
                           lookback_bars: int) -> dict:
    """审核四 E1：validation 卡的评估窗 = [split − lookback·周期, cutoff)。

    - 分界（含）之后无数据 → ValueError（"在 < split 上评估被拒"）；
    - 从分界往前多取 lookback_bars 根作预热（ATR/SMA200/分位特征需要
      历史），返回**新字典**（训练窗已在拟合上下文里取 < split——调用
      顺序必须先拟合、后切评估窗）；
    - 分界之前的信号由 _ZeroedBeforeSplit 门归零（见下），指标只按
      分界之后入场的交易计算（信号为 0 则分界前不可能入场）。
    """
    if card.segment != "validation":
        return bars_by_sym
    split = pd.Timestamp((round_obj.segments or {})["split"])
    from mt5data.store import TIMEFRAME_SECONDS as _TS
    tf_s = _TS[card.timeframe.upper()]
    warm = pd.Timedelta(seconds=tf_s * max(int(lookback_bars), 0))
    out = {}
    for sym, bars in bars_by_sym.items():
        times = pd.to_datetime(bars["time"])
        if not (times >= split).any():
            raise ValueError(
                f"{sym} 在分界 {split} 之后没有数据——validation 卡的评估"
                "窗是 [split, cutoff)，分界前数据不算样本外（D50；审核三 D1）")
        keep = times >= (split - warm)
        if not keep.any():          # 理论不可达（含分界后必有），防御
            keep = times >= split
        out[sym] = bars[keep].reset_index(drop=True)
    return out


class _ZeroedBeforeSplit:
    """评估门（审核四 E1）：分界之前的信号一律为 0。策略在预热 bar 上
    照常计算特征（有界窗口历史），但不出信号；分界之后透传。纯函数、
    截断一致（掩码按绝对时刻）。"""

    def __init__(self, inner, split):
        self._inner = inner
        self._split = pd.Timestamp(split)
        self.fittable = False

    def generate_signals(self, bars, params):
        sig = self._inner.generate_signals(bars, params)
        times = pd.to_datetime(bars["time"])
        return sig.mask(times < self._split, 0)

    def validate_params(self, params):
        return self._inner.validate_params(params)

    @property
    def exit_rules(self):
        return self._inner.exit_rules

    @property
    def required_data(self):
        return self._inner.required_data


class _GatedFactory:
    """_ZeroedBeforeSplit 的工厂（与 _FittedFactory 同构：实例化返回
    装门的实例；类属性透传）。"""

    def __init__(self, base, split):
        self._base, self._split = base, pd.Timestamp(split)

    def __call__(self):
        return _ZeroedBeforeSplit(self._base(), self._split)

    def __getattr__(self, name):
        return getattr(self._base, name)


def _prob_fit_context(card, round_ctx, bars_by_sym, costs_by_sym, data_root,
                      *, for_g0: bool = False):
    """T73A/D50：概率卡的拟合上下文——分段约束 + 单一训练窗拟合 + 冻结模型。

    返回 None（非概率卡）；否则 dict(model, model_sha, model_path, split,
    purge_bars, params_prob, metrics_prob)。分段约束（D50）：
    - discovery：任一品种的 bar 时刻 >= split → 拒（不得读取分界后数据）；
    - validation：训练窗只用 < split（评估窗的切片由调用方/关卡层处理，
      本函数只备训练数据；评估段在 < split 上无数据时同样拒）。
    for_g0=True（铁律 21/G0）：改按研究段前 70% 拟合冻结模型。
    模型写 data_research 模型库：{data_root}/models/<卡>/<sha256>.json
    （蓝图 P 线附录 B；同一输入重拟合哈希相同——确定性由策略 fit 保证）。
    """
    # 审核三 D1：按卡是否带 segment 生效（probability 卡或
    # standard+validation 的 P2 决策卡），不再只认 evaluation_mode
    if not getattr(card, "segment", ""):
        return None
    rnd_path = Path(card.path).parent.parent / "rounds" / f"{card.round}.toml"
    try:
        round_obj = rnd.load_round(rnd_path)
    except rnd.RoundError as e:
        raise ValueError(
            f"概率卡 {card.id} 所属轮次 {card.round} 的预注册不可装载：{e}"
            "（D50：分界 [segments].split 与假设族 [hypotheses] 由轮次文件"
            "提供）") from e
    split_raw = (round_obj.segments or {}).get("split")
    if not split_raw:
        raise ValueError(
            f"轮次 {card.round} 缺 [segments].split——概率卡的发现段/验证段"
            "分界必须由轮次预注册写死（D50）")
    split = pd.Timestamp(split_raw)
    horizon = int(card.prob["bracket"]["horizon_bars"])
    purge = horizon + 2
    if for_g0:
        fit_bars = {sym: bars.iloc[:max(1, int(len(bars) * 0.7))]
                    for sym, bars in bars_by_sym.items()}
        train_end = None                      # 已物理截断到前 70%
    else:
        fit_bars = {}
        for sym, bars in bars_by_sym.items():
            times = pd.to_datetime(bars["time"])
            below = bars[times < split]
            if card.segment == "discovery":
                if len(times) and times.iloc[-1] >= split:
                    raise ValueError(
                        f"{sym} 的数据到 {times.iloc[-1]}，越过分界 {split}——"
                        "discovery 卡不得读取 >= split 的数据（D50），把 "
                        "--end 收窄到分界之前")
                fit_bars[sym] = bars
            else:                             # validation：训练窗可用更早数据
                if len(below) == 0:
                    raise ValueError(
                        f"{sym} 在分界 {split} 之前没有数据——validation 卡"
                        "的训练窗需要 < split 的数据（D50）")
                fit_bars[sym] = below
        train_end = split
    return {"split": split_raw, "purge_bars": purge,
            "train_end": train_end, "fit_bars": fit_bars}


def _fit_and_freeze(strategy_cls, card, fit_ctx, bars_by_sym, costs_by_sym,
                    data_root, *, fit_meta=None):
    """执行拟合 + 冻结落盘，补全 fit_ctx（model/model_sha/model_path 与
    台账字段）。同一输入重拟合 → 同一模型 dict → 同一 sha（策略 fit 的
    确定性；铁律 21）。fit_meta（合约大小/手数口径等运行侧信息）经
    params["__fit__"] 传入——协议签名不变，标签引擎与评估引擎同口径。"""
    from mt5quant.prob.artifact import save_model

    inst = strategy_cls()
    model = inst.fit(fit_ctx["fit_bars"], costs_by_sym,
                     {"__fit__": fit_meta or {}},
                     train_end=fit_ctx["train_end"],
                     purge_bars=fit_ctx["purge_bars"])
    root = Path(data_root) / "models" / card.id
    path, sha = save_model(model, root)
    fit_ctx.update(
        model=model, model_sha=sha, model_path=path,
        params_prob={"segment": card.segment, "split": fit_ctx["split"],
                     "bracket": dict(card.prob["bracket"])},
        metrics_prob={"model_sha256": sha,
                      "prob_version": model.get("prob_version"),
                      "fit_start": (model.get("fit_window") or {}).get("start"),
                      "fit_end": (model.get("fit_window") or {}).get("end"),
                      "purge_bars": fit_ctx["purge_bars"]})
    return fit_ctx


def _prob_run_fields(fit_ctx, params, summary):
    """P 线字段落台账（T73A：不改 runs 表结构）：
    params_json += {"prob": {segment, split, bracket}}（试验身份）；
    metrics_json += {"prob": {model_sha256, prob_version, fit_start,
    fit_end, purge_bars}}（模型身份，D43）。两处都进哈希链。"""
    if fit_ctx is None:
        return params, summary
    params = {**params, "prob": fit_ctx["params_prob"]}
    if summary is not None:
        summary = {**summary, "prob": fit_ctx["metrics_prob"]}
    return params, summary


def _reject_fittable_walkforward(strategy_cls, wanted_gates) -> str | None:
    """T73A：拟合策略的 walk-forward（G3）与关卡 v3 主检验在 T73B（逐窗
    拟合 + purge 接线，等 H 线审核会话批准 T65）完成前一律拒绝。"""
    if not getattr(strategy_cls, "fittable", False):
        return None
    if "G3" in wanted_gates:
        return ("拟合策略的 walk-forward（G3）在 T73B 之前拒绝运行——"
                "逐窗重新拟合与 purge 接线未完成（等 H 线审核会话批准 "
                "T65 后做 T73B）；关卡 v3 主检验同理")
    return None


def _prob_preconditions(ledger, card, path, *, actor: str) -> tuple | None:
    """T90 前置检查（任一不满足打印原因并返回 None）：
    evaluation_mode=probability 且 segment=discovery；轮次可装载且
    approved_by 非空；候选评估 mode=probability；卡的假设表与轮次
    [hypotheses] 一致；轮次剩余预算 ≥ 假设条数。返回 (round_obj, split)。"""
    if card.evaluation_mode != "probability":
        print(f"✗ bt prob test 只用于 probability 卡（收到 "
              f"{card.evaluation_mode!r}）")
        return None
    if card.segment != "discovery":
        print("✗ P1 只在发现段跑（segment 必须 discovery；验证段属 T92/P2）")
        return None
    rnd_path = Path(path).parent.parent / "rounds" / f"{card.round}.toml"
    try:
        round_obj = rnd.load_round(rnd_path)
    except rnd.RoundError as e:
        print(f"✗ 轮次 {card.round} 不可装载：{e}")
        return None
    if not round_obj.approved_by.strip():
        print(f"✗ 轮次 {card.round} 的 approved_by 为空——轮次未获批"
              "（P 线审核会话批准时填写；执行会话不得代填）")
        return None
    # S2：走与 screen/gate 同一 _round_context（轮次已开启、候选在名单、
    # 最新预筛 pass、轮次文件 sha 与开启时入账一致——H-08 冻结快照在
    # _round_context 内统一核对；round_file 显式给出本函数已定位的路径）
    import hashlib as _hl
    ctx = _round_context(ledger, card, actor=actor, round_file=rnd_path)
    if ctx is False or ctx is None:
        print(f"✗ 轮次约束未过（bt prob test 与 screen/gate 同一铁律 16 "
              "口径）")
        return None
    opened = ledger.round(card.round) or {}
    file_sha = _hl.sha256(Path(rnd_path).read_bytes()).hexdigest()
    if opened.get("toml_sha256") != file_sha:
        print(f"✗ 轮次文件 {rnd_path.name} 与开启时入账的内容不一致"
              f"（sha256 {file_sha[:12]}… vs 台账 "
              f"{str(opened.get('toml_sha256'))[:12]}…）——开启后改预注册"
              "（分界/假设族/预算）无效；改动作为新轮次登记")
        return None
    ev = round_obj.evaluation.get(card.candidate) or {}
    if ev.get("mode") != "probability":
        print(f"✗ 轮次 {card.round} 中候选 {card.candidate} 的评估模式是 "
              f"{ev.get('mode')!r}（须 probability）")
        return None
    listed = list(round_obj.hypotheses.get(card.candidate, ()))
    if sorted(card.prob["hypotheses"]) != sorted(listed):
        print(f"✗ 卡的假设表与轮次 [hypotheses].{card.candidate} 不一致"
              f"（卡 {sorted(card.prob['hypotheses'])} vs 轮次 "
              f"{sorted(listed)}——先卡后测：改卡须新版本入新轮）")
        return None
    if not (round_obj.segments or {}).get("split"):
        print(f"✗ 轮次 {card.round} 缺 [segments].split（D50）")
        return None
    used = ledger.round_n_dedup(round_obj.id)
    if used + len(listed) > round_obj.trial_budget:
        print(f"✗ 轮次预算不足：已用 {used}/{round_obj.trial_budget}，"
              f"本卡还需 {len(listed)} 条假设各一行")
        return None
    return round_obj, ctx


def cmd_prob_test(args) -> int:
    """T90：bt prob test --card <P###-v#>——P1 现象检验（发现段）。"""
    if _reject_ops_ack(args):
        return 1
    path = _card_path(args.card, Path(args.cards_dir))
    try:
        card = cards.load_card(path)
    except cards.CardError as e:
        print(f"✗ {e}")
        return 1
    try:
        cards.ensure_strategy_matches(card, args.strategy)
    except cards.CardStrategyMismatchError as e:
        print(f"✗ {e}")
        return 1
    actor = _enforce_candidate_status(args, card)
    if actor is None:
        return 1
    try:
        data_root, store_kind = _resolve_store(args)
    except ValueError as e:
        print(f"✗ {e}")
        return 1
    ledger_path = _ledger_default(args)
    from mt5quant.prob import p1 as p1mod

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
        pre = _prob_preconditions(ledger, card, path, actor=actor)
        if pre is None:
            return 1
        round_obj, _round_ctx = pre
        split = pd.Timestamp(round_obj.segments["split"])
        try:
            strategy_cls = load_strategy(args.strategy)
            _cs = _check_card_strategy_constants(card, strategy_cls)
            if _cs:
                print(f"✗ {_cs}")
                return 1
            del strategy_cls          # P1 不跑策略引擎（标签经 prob.labels）
            sizes = {sym: resolve_contract_size(data_root, sym, None)
                     for sym in card.symbols}
            # T97 波次 2：卡带 [prob] 新键时改用 merged_research_series
            # （ext 前向延长），gap_spans 交给 run_p1_card 抑制跨洞触发、
            # mt5_first 交给它按 era 分组用 ScenarioCosts 标注 ext 段触发。
            use_merged = _prob_use_merged(card.prob or {})
            bars_by_sym = {}
            gap_by_sym = {}
            mt5_first_by_sym = {}
            if use_merged:
                from mt5quant.prob.gaps import mt5_first_time
            for sym in card.symbols:
                bars_full, gap_spans = _prob_load_bars(
                    data_root, sym, card.timeframe, use_merged=use_merged,
                    end=args.end)
                if use_merged:
                    gap_by_sym[sym] = gap_spans
                    mt5_first_by_sym[sym] = mt5_first_time(
                        data_root, sym, card.timeframe)
                times = pd.to_datetime(bars_full["time"])
                below = bars_full[times < split]
                if len(below) == 0:
                    print(f"✗ {sym} 在分界 {split} 前无数据（发现段为空）")
                    return 1
                bars_by_sym[sym] = below.reset_index(drop=True)
            from mt5data.sessions import load_session_policy
            sess_policy = load_session_policy(str(SESSIONS_PATH))
            sess_sha = sessions_sha256(str(SESSIONS_PATH))
            costs_by_sym = {}
            policy_by_sym = {}
            for sym in card.symbols:
                costs_by_sym[sym], cost_version = build_costs(
                    data_root, sym, args.spread_fraction, args.commission,
                    sessions_sha=sess_sha, research_store=store_kind == "research",
                    # T83/K5：点数模式隔夜费相对价格基准 = 全序列最后收盘
                    # （与 CLI 主路径同一约定；bars_full 为锁箱语义装载的全序列）
                    swap_ref_price=float(bars_full["close"].iloc[-1]))
                policy_by_sym[sym] = entry_policy_for(
                    costs_by_sym[sym], sym, sess_policy)
            for sym in card.symbols:      # S8：合约大小必填，不默认 1.0
                if sym not in sizes:
                    print(f"✗ {sym} 的合约大小未知（specs 缺失）——标签"
                          "引擎需与评估同口径，不默认 1.0")
                    return 1
            results = p1mod.run_p1_card(card, round_obj, bars_by_sym,
                                        costs_by_sym, sizes=sizes,
                                        policy_by_symbol=policy_by_sym,
                                        sims=args.sims,
                                        gap_spans_by_symbol=(gap_by_sym or None),
                                        mt5_first_by_symbol=(
                                            mt5_first_by_sym or None))
        except Exception as e:
            print(f"✗ P1 运行失败：{type(e).__name__}: {e}")
            return 1
        strategy_sha = _bind_strategy_sha(ledger, card, args.strategy)
        if strategy_sha is None:
            return 1                 # 策略未提交/脏/指纹绑定不符（已打印）
        for r in results:
            params = {"hypothesis": r["hypothesis"],
                      "prob": {"candidate": card.candidate,
                               "segment": card.segment,
                               "split": round_obj.segments["split"],
                               "bracket": dict(card.prob["bracket"]),
                               "seed": r["seed"],
                               "prob_version": r["prob_version"]}}
            metrics = {"prob": {"prob_version": r["prob_version"],
                                "n_triggers": r["n_triggers"],
                                "n_trigger_days": r["n_trigger_days"],
                                "real_win_rate": r["real_win_rate"],
                                "null_win_rate": r["null_win_rate"],
                                "delta_p": r["delta_p"],
                                "p_value": r["p_value"]}}
            rid = ledger.record_run(
                card_id=card.id, card_version=card.version,
                strategy=args.strategy, params=params,
                symbol=card.symbols[0], timeframe=card.timeframe,
                data_first=pd_iso(bars_by_sym[card.symbols[0]]["time"].iloc[0]),
                data_last=pd_iso(bars_by_sym[card.symbols[0]]["time"].iloc[-1]),
                data_sha256=data_sha256(bars_by_sym[card.symbols[0]]),
                engine_version=ENGINE_VERSION,
                cost_model_version=cost_version, gates_hash=_gates_hash(),
                metrics=metrics, actor=actor,
                strategy_sha=strategy_sha,
                run_class="research", round_id=round_obj.id)
            mark = "✓" if r["p_value"] < 0.05 else "·"
            print(f"{mark} run#{rid} {r['hypothesis']}：触发 "
                  f"{r['n_triggers']}（{r['n_trigger_days']} 日），Δp="
                  f"{r['delta_p']:+.4f}，p={r['p_value']:.4f}（种子 "
                  f"{r['seed']}）")
        print(f"— {len(results)} 条假设全部完成（不能只跑几条）；台账 "
              f"{ledger_path}")
        return 0


def cmd_prob_round_report(args) -> int:
    """T90：bt prob round-report <R#>——本轮全部假设 Holm + 推进判定。

    推进 = Holm 校正后 p < 0.05 **且** Δp 点估计满足事前成本要求
    （成本取候选预筛 research/candidates/<C###>.precheck.json 的
    ex_ante_cost_prob.cost_r_by_symbol 最大值；判定式与预筛同一公式：
    ΔEV = Δp̂ × (tp_R + 1) ≥ 3 × 成本，tp_R 取该运行 params.prob.bracket）。
    一条假设有多条成功运行 → 拒绝出报告（不允许重跑挑结果）；运行按
    （候选、假设）过滤，不只按假设字符串。
    """
    try:
        data_root, _store_kind = _resolve_store(args)
    except ValueError as e:
        print(f"✗ {e}")
        return 1
    rnd_path = Path(args.rounds_dir) / f"{args.round}.toml"
    try:
        round_obj = rnd.load_round(rnd_path)
    except rnd.RoundError as e:
        print(f"✗ {e}")
        return 1
    ledger_path = _ledger_default(args)
    from mt5quant.validation.prob_mode import holm
    with Ledger(ledger_path) as ledger:
        all_runs = ledger.runs_of_round(round_obj.id)
        pvals: dict[str, float] = {}
        rows = []
        opened_at = (ledger.round(round_obj.id) or {}).get("opened_utc", "")
        for cid, hyps in round_obj.hypotheses.items():
            # 审核四 E6：成本取**开轮时已入账**的预筛结论（台账
            # candidate_checks 里 opened_utc 之前最新一行），不读磁盘上
            # 随时可被重写的 JSON
            rows_chk = [r for r in ledger.candidate_checks(cid)
                        if r["checked_at_utc"] <= opened_at]
            pre_json = Path(args.candidates_dir) / f"{cid}.precheck.json"
            if rows_chk:
                # E6：开轮时已入账的预筛结论优先（磁盘 JSON 开轮后可被
                # 重写，不作数）
                try:
                    pre = json.loads(rows_chk[-1]["result_json"])
                    cost_map = (pre["checks"]["ex_ante_cost_prob"]
                                ["cost_r_by_symbol"])
                except (ValueError, KeyError, TypeError) as e:
                    print(f"✗ 候选 {cid} 开轮时的预筛记录无法解析成本"
                          f"（{e}）")
                    return 1
            elif pre_json.is_file():
                # 兼容回退：台账无开轮前记录时读磁盘 JSON（预筛从未经
                # bt candidate check 入账的旧路径）
                try:
                    pre = json.loads(pre_json.read_text(encoding="utf-8"))
                    cost_map = (pre["checks"]["ex_ante_cost_prob"]
                                ["cost_r_by_symbol"])
                except (ValueError, KeyError, TypeError) as e:
                    print(f"✗ {pre_json.name} 无法解析成本（{e}）")
                    return 1
            else:
                print(f"✗ 候选 {cid} 在开轮（{opened_at}）前没有预筛记录"
                      "，磁盘也无可回退的 precheck.json——成本条件无从判定")
                return 1
            if not cost_map:
                print(f"✗ 候选 {cid} 开轮时的预筛记录 cost_r_by_symbol "
                      "为空——成本条件无从判定（E6：不按成本 0 处理）")
                return 1
            cost_r = max(float(v) for v in cost_map.values())
            for h in hyps:
                key = f"{cid}:{h}"
                # 审核四 E6：按（候选, 假设）过滤（候选号由 bt prob test
                # 写进 params.prob.candidate；兼容缺候选号的旧运行——
                # 假设字符串唯一即可）
                runs = [r for r in all_runs
                        if (r["params"] or {}).get("hypothesis") == h
                        and r.get("ok")
                        and ((r["params"].get("prob") or {}).get(
                            "candidate", cid) == cid)]
                if not runs:
                    print(f"✗ 假设 {h} 没有运行记录——全部卡跑完才能出报告")
                    return 1
                if len(runs) > 1:
                    print(f"✗ 假设 {h} 有 {len(runs)} 条成功运行"
                          f"（run#{[r['run_id'] for r in runs]}）——不允许"
                          "重跑挑结果；须先在轮次报告里说明取舍或作废多余运行")
                    return 1
                run = runs[0]
                m = (run.get("metrics") or {}).get("prob") or {}
                p_raw = float(m.get("p_value", 1.0))
                delta = float(m.get("delta_p", 0.0))
                bracket = (run["params"].get("prob") or {}).get("bracket") or {}
                tp_r = (float(bracket.get("tp_atr", 0.0))
                        / float(bracket.get("sl_atr", 1.0)))
                edge = delta * (tp_r + 1.0)
                cost_ok = edge >= 3.0 * cost_r
                pvals[key] = p_raw
                rows.append({"candidate": cid, "hypothesis": h,
                             "p_value": p_raw, "delta_p": delta,
                             "n_trigger_days": m.get("n_trigger_days"),
                             "cost_r": cost_r, "edge_ev_r": edge,
                             "cost_ok": cost_ok})
        adjusted = holm(pvals)
        out_dir = Path("docs/research")
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = pd.Timestamp.now().strftime("%Y-%m-%d")
        md = out_dir / f"round-{round_obj.id}-p1-{stamp}.md"
        js = out_dir / f"round-{round_obj.id}-p1-{stamp}.json"
        lines = [f"# P1 轮次报告 {round_obj.id}（{stamp}）", "",
                 "状态：待审核", "",
                 "推进规则（预写，轮次文件 advance_rule）：Holm 校正后 "
                 "p < 0.05 **且** ΔEV = Δp̂×(tp_R+1) ≥ 3×预筛成本"
                 "（成本取候选预筛 cost_r_by_symbol 最大值）。", "",
                 "| 候选 | 假设 | 触发日数 | Δp | 原始 p | Holm p | Holm 显著 |"
                 " ΔEV(R) | 3×成本(R) | 成本条件 | 结论 |",
                 "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        verdicts = []
        for r in rows:
            key = f"{r['candidate']}:{r['hypothesis']}"
            adj = adjusted[key]
            holm_ok = adj < 0.05
            advance = holm_ok and r["cost_ok"]
            concl = "推进" if advance else "不推进"
            verdicts.append(advance)
            lines.append(
                f"| {r['candidate']} | {r['hypothesis']} | "
                f"{r['n_trigger_days']} | {r['delta_p']:+.4f} | "
                f"{r['p_value']:.4f} | {adj:.4f} | {'✓' if holm_ok else '✗'} | "
                f"{r['edge_ev_r']:+.4f} | {3 * r['cost_r']:.4f} | "
                f"{'✓' if r['cost_ok'] else '✗'} | {concl} |")
        lines += ["", f"推进 {sum(verdicts)} / {len(verdicts)} 条假设。",
                  "", "（本报告由 bt prob round-report 生成；方法变更与对照"
                  "见 T75 标定报告。）"]
        md.write_text("\n".join(lines) + "\n", encoding="utf-8")
        js.write_text(json.dumps({"round": round_obj.id,
                                  "status": "待审核",
                                  "rows": rows, "holm": adjusted},
                                 ensure_ascii=False, indent=2),
                      encoding="utf-8")
        print(f"✓ 报告：{md}（同 JSON）——交 P 线审核会话")
        return 0


def _card_of(run: dict) -> str:
    """运行行的卡号（runs 行缺 card_id 键时返回空串）。"""
    return str(run.get("card_id") or "")


def cmd_fit(args) -> int:
    """T73A：bt fit <卡>——概率卡按分段约束拟合，模型冻结落盘并打印
    路径与哈希（不产生 runs——模型身份随其后的 screen/gate 运行入台账）。"""
    path = _card_path(args.card, Path(args.cards_dir))
    try:
        card = cards.load_card(path)
    except cards.CardError as e:
        print(f"✗ {e}")
        return 1
    try:
        cards.ensure_strategy_matches(card, args.strategy)
    except cards.CardStrategyMismatchError as e:
        print(f"✗ {e}")
        return 1
    if card.evaluation_mode != "probability":
        print("✗ bt fit 只用于 evaluation_mode = probability 的卡（T73A）")
        return 1
    _cs = _check_card_strategy_constants(
        card, load_strategy(args.strategy))
    if _cs:
        print(f"✗ {_cs}")
        return 1
    try:
        data_root, store_kind = _resolve_store(args)
    except ValueError as e:
        print(f"✗ {e}")
        return 1
    try:
        strategy_cls = load_strategy(args.strategy)
        bars_by_sym = {sym: load_series(data_root, sym, card.timeframe,
                                        args.end)
                       for sym in card.symbols}
        from mt5data.sessions import load_session_policy
        sess_policy = load_session_policy(str(SESSIONS_PATH))
        sess_sha = sessions_sha256(str(SESSIONS_PATH))
        costs_by_sym = {}
        for sym in card.symbols:
            costs_by_sym[sym], _ver = build_costs(
                data_root, sym, args.spread_fraction, args.commission,
                sessions_sha=sess_sha, research_store=store_kind == "research",
                # T83/K5：与 screen/gate 同一约定——该品种装载序列最后收盘
                swap_ref_price=float(bars_by_sym[sym]["close"].iloc[-1]))
        fit_ctx = _prob_fit_context(card, None, bars_by_sym, costs_by_sym,
                                    data_root)
        _fit_and_freeze(strategy_cls, card, fit_ctx, bars_by_sym, costs_by_sym,
                        data_root)
    except Exception as e:
        print(f"✗ 拟合失败：{type(e).__name__}: {e}")
        return 1
    fw = (fit_ctx["model"].get("fit_window") or {})
    print(f"✓ 模型冻结：{fit_ctx['model_path']}")
    print(f"  sha256={fit_ctx['model_sha']}")
    print(f"  段={card.segment} split={fit_ctx['split']} "
          f"fit={fw.get('start')}..{fw.get('end')} "
          f"purge={fit_ctx['purge_bars']} 根")
    return 0


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
    # T80/G4：运行类别按候选状态写入（样品 calibration / 研究 research）
    run_class = _run_class_for(actor)

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
        # T63/D32（铁律 16）：研究卡只能属于已开启轮次（轮次上下文随
        # record_run 落 runs.round_id；预算满拒绝新试验）
        round_ctx = _round_context(ledger, card, actor=actor)
        if round_ctx is False:
            return 1
        if _round_budget_exceeded(ledger, round_ctx):
            return 1

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
                        research_store=research_mode,
                        # T83/R13-K5：点数模式隔夜费相对价格基准 = 该品种
                        # 研究库序列最后收盘（与 CLI 主路径同一约定——研究
                        # 路径不再按快照时代绝对额对早年价格放大隔夜费）
                        swap_ref_price=float(
                            bars_by_sym[sym]["close"].iloc[-1]))
                    policy_by_sym[sym] = entry_policy_for(
                        costs_by_sym[sym], sym, sess_policy)
            else:
                cost_version = "zero-v0"
                costs_by_sym = {sym: ZeroCosts() for sym in card.symbols}
                policy_by_sym = {sym: sess_policy for sym in card.symbols}
                # 审核三 D1：probability 卡拒绝 screen；卡/策略常量核对
            _bc_msg = _reject_probability_card_backtest(card)
            if _bc_msg:
                print(f"✗ {_bc_msg}")
                return 1
            _cs_msg = _check_card_strategy_constants(card, strategy_cls)
            if _cs_msg:
                print(f"✗ {_cs_msg}")
                return 1
            # 审核四 E1：先拟合（训练窗 < split，用**全量** bars），再切
            # 评估窗（含预热）——顺序反了会让训练窗变空
            fit_ctx = None
            if getattr(strategy_cls, "fittable", False):
                try:
                    fit_ctx = _prob_fit_context(
                        card, round_ctx, bars_by_sym, costs_by_sym, data_root)
                    _fit_and_freeze(strategy_cls, card, fit_ctx, bars_by_sym,
                                    costs_by_sym, data_root,
                                    fit_meta={"sizes": sizes,
                                              "vols_by_sym": vols_by_sym,
                                              "sizing": sizing_info,
                                              "policies": policy_by_sym})
                except ValueError as e:
                    print(f"✗ {e}")
                    return 1
                strategy_cls = _FittedFactory(strategy_cls, fit_ctx["model"])
                print(f"— 概率卡拟合：模型 {fit_ctx['model_sha'][:12]}… -> "
                      f"{fit_ctx['model_path']}")
            if getattr(card, "segment", "") == "validation":
                rnd_path = Path(card.path).parent.parent / "rounds" / \
                    f"{card.round}.toml"
                round_obj_v = rnd.load_round(rnd_path)
                lookback = getattr(
                    getattr(strategy_cls, "required_data", None),
                    "lookback_bars", 0) or 0
                bars_by_sym = _slice_validation_eval(
                    card, round_obj_v, bars_by_sym,
                    lookback_bars=lookback)
                split_v = round_obj_v.segments["split"]
                strategy_cls = _GatedFactory(strategy_cls, split_v)
                print(f"— validation 评估窗：分界 {split_v} 前留 "
                      f"{lookback} 根预热，分界前信号为 0")
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
                # T63：本轮预算满 → 该组合被拒（不执行也不记 runs）
                if (round_ctx is not None
                        and ledger.round_n_dedup(round_ctx["round_id"])
                        >= round_ctx["trial_budget"]):
                    failed += 1
                    print(f"✗ {label} -> 轮次 {round_ctx['round_id']} 试验"
                          f"预算已满（trial_budget="
                          f"{round_ctx['trial_budget']}，去重口径）——组合"
                          "被拒（不记 runs）")
                    continue
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
                    rec_params, rec_summary = _prob_run_fields(
                        fit_ctx, params, summary)
                    rid = ledger.record_run(
                        card_id=card.id, card_version=card.version,
                        strategy=args.strategy, params=rec_params, symbol=sym,
                        timeframe=card.timeframe, data_first=data_first,
                        data_last=data_last, data_sha256=dsha,
                        engine_version=ENGINE_VERSION,
                        cost_model_version=cost_version, gates_hash=gates_hash,
                        metrics=rec_summary, actor=actor,
                        strategy_sha=strategy_sha, run_class=run_class,
                        round_id=(round_ctx or {}).get("round_id"))
                    skipped = summary.get("skipped_zero_lots") or 0
                    print(f"✓ run#{rid} {label} -> "
                          f"净利 {summary['net_profit']:.2f} "
                          f"交易 {summary['trade_count']} 笔"
                          + (f"（零手数跳过 {skipped}）" if skipped else ""))
                except Exception as e:  # 失败组合也进台账
                    failed += 1
                    rec_params, _ = _prob_run_fields(fit_ctx, params, None)
                    rid = ledger.record_run(
                        card_id=card.id, card_version=card.version,
                        strategy=args.strategy, params=rec_params, symbol=sym,
                        timeframe=card.timeframe, data_first=data_first,
                        data_last=data_last, data_sha256=dsha,
                        engine_version=ENGINE_VERSION,
                        cost_model_version=cost_version,
                        gates_hash=gates_hash, metrics=None,
                        error=f"{type(e).__name__}: {e}", actor=actor,
                        strategy_sha=strategy_sha, run_class=run_class,
                        round_id=(round_ctx or {}).get("round_id"))
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
    # T80/G4：运行类别按候选状态写入（样品 calibration / 研究 research）
    run_class = _run_class_for(actor)
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
        # T63/D32（铁律 16）：研究卡只能属于已开启轮次；G5 另查本轮
        # lockbox_uses_max（在 approve/一次性/预算检查之前——轮次上限
        # 是预注册钉死的轮次级约束）
        round_ctx = _round_context(ledger, card, actor=actor,
                                   wanted_gates=wanted)
        if round_ctx is False:
            return 1
        if _round_budget_exceeded(ledger, round_ctx):
            return 1
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

        # T86：关卡 V3 的事件分支预检（在装配/基准运行**之前**——拒绝不
        # 产生任何 runs）：mode 判定（轮次 [evaluation]）、事件窗类别声明、
        # 随机化次数下限 max(draws_min_base, ⌈coeff×N/α⌉)。N 取运行入口
        # 时点的 rounds_lineage 门槛 + 1（本次基准运行即将入账——按含本
        # 次的 N 定下限，宁可多跑不少跑；评估与报告的 N 在 V3 块内按
        # 入账后的台账重算，与 G4 同一取数时点）。
        v3_ctx = None
        if "V3" in wanted:
            try:
                if _round_eval_mode(card) == "event":
                    if not args.event_kind:
                        print("✗ 事件模式卡（轮次 [evaluation] mode=\"event\"）"
                              "跑关卡 V3 必须显式声明事件窗类别 --event-kind"
                              " month_end|london_fix（事件窗按预注册机制锚定，"
                              "不静默取缺省）")
                        return 1
                    comp_pre = _v3_n_composition(
                        ledger, card, Path(args.cards_dir))
                    n_pre = int(comp_pre["n"]) + 1
                    draws_min = vgate.v3_event_draws_min(n_pre, cfg)
                    if (args.event_sims is not None
                            and int(args.event_sims) < draws_min):
                        print(f"✗ 随机化次数 {args.event_sims} 低于下限 "
                              f"{draws_min} = max(1999, ⌈20N/α⌉)"
                              f"（N={n_pre}（含本次运行），α={cfg.alpha}——"
                              "[v3.event] 主检验 p ≤ α/N 需要 p 值分辨率"
                              " 1/次数 ≤ α/N，D35 修改 1）——拒绝运行")
                        return 1
                    v3_ctx = {
                        "mode": "event", "kind": args.event_kind,
                        "side": args.event_side,
                        "sims": (int(args.event_sims)
                                 if args.event_sims is not None
                                 else draws_min),
                        "draws_min": draws_min}
                    print(f"— 关卡 V3 事件分支（T86）：kind={args.event_kind}"
                          f"，side={args.event_side}，随机化次数 "
                          f"{v3_ctx['sims']}（下限 {draws_min}，N={n_pre}）")
                else:
                    v3_ctx = {"mode": "bar"}
            except ValueError as e:
                print(f"✗ {e}")
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
                        research_store=research_mode,
                        # T83/R13-K5：与 screen 同一约定——研究库序列最后收盘
                        swap_ref_price=float(
                            bars_by_sym[sym]["close"].iloc[-1]))
                    policy_by_sym[sym] = entry_policy_for(
                        costs_by_sym[sym], sym, sess_policy)
            else:
                cost_version = "zero-v0"
                costs_by_sym = {sym: ZeroCosts() for sym in card.symbols}
                policy_by_sym = {sym: sess_policy for sym in card.symbols}
        except Exception as e:
            print(f"✗ 装配失败（未开跑，无 runs 记录）：{type(e).__name__}: {e}")
            return 1

        # 审核三 D1：probability 卡拒绝 gate；卡/策略常量核对；
        # validation 评估切 [split, cutoff)
        _bc_msg = _reject_probability_card_backtest(card)
        if _bc_msg:
            print(f"✗ {_bc_msg}")
            return 1
        _cs_msg = _check_card_strategy_constants(card, type(strategy))
        if _cs_msg:
            print(f"✗ {_cs_msg}")
            return 1
        # T73A：拟合策略先拟合再评估（审核四 E1：拟合用**全量** bars、
        # 训练窗 < split，之后才切评估窗）；walk-forward（G3）与关卡 v3
        # 主检验在 T73B 完成前一律拒绝。_strategy_cls 恒为装门前的原始类
        # （_ZeroedBeforeSplit 只包实例，不作拟合工厂）。
        _strategy_cls = type(strategy)
        fit_ctx = None
        # T86：bar 级 V3 的主检验同样取自 walk-forward 拼接的 OOS——拟合
        # 策略与 G3 同拒（事件分支不经 walk-forward，不受限）
        _wf_wanted = list(wanted)
        if "V3" in wanted and (v3_ctx or {}).get("mode") != "event":
            _wf_wanted.append("G3")
        _g3_msg = _reject_fittable_walkforward(_strategy_cls, _wf_wanted)
        if _g3_msg:
            print(f"✗ {_g3_msg}")
            return 1
        if getattr(strategy, "fittable", False):
            try:
                fit_ctx = _prob_fit_context(
                    card, round_ctx, bars_by_sym, costs_by_sym, data_root)
                _fit_and_freeze(_strategy_cls, card, fit_ctx, bars_by_sym,
                                costs_by_sym, data_root,
                                fit_meta={"sizes": sizes,
                                          "vols_by_sym": vols_by_sym,
                                          "sizing": sizing_info,
                                          "policies": policy_by_sym})
            except ValueError as e:
                print(f"✗ {e}")
                return 1
            strategy = strategy.with_model(fit_ctx["model"])
            print(f"— 概率卡拟合：模型 {fit_ctx['model_sha'][:12]}… -> "
                  f"{fit_ctx['model_path']}")
        if getattr(card, "segment", "") == "validation":
            rnd_path = Path(card.path).parent.parent / "rounds" /                 f"{card.round}.toml"
            round_obj_v = rnd.load_round(rnd_path)
            lookback = getattr(
                getattr(_strategy_cls, "required_data", None),
                "lookback_bars", 0) or 0
            bars_by_sym = _slice_validation_eval(
                card, round_obj_v, bars_by_sym, lookback_bars=lookback)
            strategy = _ZeroedBeforeSplit(
                strategy, round_obj_v.segments["split"])
            print(f"— validation 评估窗：分界前留 {lookback} 根预热，"
                  "分界前信号为 0")

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
        rec_params, _unused = _prob_run_fields(fit_ctx, params, None)
        common = dict(
            card_id=card.id, card_version=card.version,
            strategy=args.strategy, params=rec_params, symbol=symbol,
            timeframe=card.timeframe, data_first=data_first,
            data_last=data_last, data_sha256=dsha,
            engine_version=ENGINE_VERSION, cost_model_version=cost_version,
            gates_hash=gsha, actor=actor, strategy_sha=strategy_sha,
            run_class=run_class,
            round_id=(round_ctx or {}).get("round_id"))
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
            _, summary = _prob_run_fields(fit_ctx, params, summary)
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

        # T23：spec 带定手数口径 → G2/G3 一切复算经 run_engine 沿用同一口径；
        # H-06：spec.session_policy 携带该品种禁入政策——一切关卡复算
        # （成本放大/邻域/随机入场/G3/V3 否决/G5 参照重放）同一禁入口径
        if sizing is not None:
            spec = BacktestSpec(strategy=strategy, symbol=symbol,
                                timeframe=card.timeframe,
                                contract_size=sizes[symbol],
                                volume_lots=None,
                                initial_capital=args.initial_capital,
                                sizing=sizing,
                                vol_min=vols_by_sym[symbol][0],
                                vol_step=vols_by_sym[symbol][1],
                                vol_max=vols_by_sym[symbol][2],
                                session_policy=policy_by_sym[symbol])
        else:
            spec = BacktestSpec(strategy=strategy, symbol=symbol,
                                timeframe=card.timeframe,
                                contract_size=sizes[symbol],
                                volume_lots=lots,
                                initial_capital=args.initial_capital,
                                session_policy=policy_by_sym[symbol])
        seed = vstats.rng_seed(card.id)
        # T79：序列 meta.json 登记的已知缺口——G2 按年分段 / G3 walk-forward
        # 读到时在关卡 details 里注明相关窗口样本不完整（只注记，不改判定）
        known_gaps = read_known_gaps(data_root, symbol, card.timeframe)
        results: dict[str, dict] = {}

        def _run(name, fn):
            try:
                results[name] = fn()
            except Exception as e:   # 单关卡炸掉不拖垮其余关卡
                results[name] = {"passed": False,
                                 "error": f"{type(e).__name__}: {e}"}

        # H-02：关卡结果随评估**即时入账**（先可靠保存研究关卡结果，再
        # 占预算读锁箱）——同次 G3/V3+G5 流程里 begin_lockbox 才能绑定
        # 本次运行的当前判决；旧实现统一在 G5 之后写账，历史 G3/V3 记录
        # 会授权当前已失败的试验。
        def _run_recorded(name, fn):
            _run(name, fn)
            details = dict(results[name])
            # M5：参数选择过程（候选数、选中 run_id）随每个关卡落台账
            details["params_selection"] = selection
            ledger.record_gate(run_id, name, bool(results[name]["passed"]),
                               details)

        if "G0" in wanted:
            g0_strategy = strategy
            if fit_ctx is not None:
                # T73A：G0 用研究段前 70% 拟合出的冻结模型（截断一致与
                # 窗口不变性检查针对装好模型的策略）。审核三 D1：validation
                # 卡的 G0 只用 **< split 训练段** 的前 70%（评估段是样本外，
                # 不得进 G0 的拟合）
                g0_bars = bars_by_sym
                if getattr(card, "segment", "") == "validation":
                    g0_bars = dict(fit_ctx["fit_bars"])
                g0_ctx = _prob_fit_context(
                    card, round_ctx, g0_bars, costs_by_sym, data_root,
                    for_g0=True)
                _fit_and_freeze(_strategy_cls, card, g0_ctx, bars_by_sym,
                                costs_by_sym, data_root,
                                fit_meta={"sizes": sizes,
                                          "vols_by_sym": vols_by_sym,
                                          "sizing": sizing_info,
                                          "policies": policy_by_sym})
                g0_strategy = _strategy_cls().with_model(g0_ctx["model"])
            _run_recorded("G0", lambda: vlook.run_g0(g0_strategy, bars,
                                                     params))
        if "G1" in wanted:
            _run_recorded("G1", lambda: vgate.run_g1(
                base.trades, timeframe=card.timeframe, cfg=cfg))
        if "G2" in wanted:
            _run_recorded("G2", lambda: vrob.run_g2(
                spec, bars, costs, params, base_result=base,
                base_summary=summary, grid=card.params,
                bars_by_symbol=bars_by_sym, costs_by_symbol=costs_by_sym,
                sizes_by_symbol=sizes, cfg=cfg, seed=seed,
                vols_by_symbol=vols_by_sym,      # T23：sizing 口径跨品种透传
                policies_by_symbol=policy_by_sym,   # H-06：逐品种禁入政策
                known_gaps=known_gaps))          # T79：样本不完整注记
        if "G3" in wanted:
            _run_recorded("G3", lambda: vwf.run_g3(
                spec, bars, costs, grid=combos,
                in_sample_sharpe=summary.get("sharpe"), cfg=cfg,
                known_gaps=known_gaps))          # T79：样本不完整注记
        if "G4" in wanted:
            def _g4():
                returns = base.equity.astype("float64").pct_change().dropna()
                bpy = summary.get("annualization_bars_per_year") \
                    or bars_per_year(card.timeframe)
                # T21c/M4 + T64/D33：N 与试验夏普按 [g4].dsr_trial_scope
                # 取数（D34 冻结后 = rounds_lineage——ledger.primary_test_n
                # 的门槛部分，T85 冻结 L1；family/project 语义不变。经验
                # 方差按冻结只作诊断，v3 主检验接线在 T86）
                scope = cfg.dsr_trial_scope
                n_dedup, trial_sharpes = _dedup_trial_sharpes(
                    ledger, card.id, float(bpy), scope=scope)
                out = vgate.run_g4(
                    returns, n_trials=n_dedup, trial_sharpes=trial_sharpes,
                    actual_max_drawdown=summary.get("max_drawdown", 0.0),
                    cfg=cfg, seed=seed,
                    block_len=_bars_per_day(card.timeframe))
                # T64/D33：关卡结果同时列谱系/轮次/项目三种 N（可审计）
                st = ledger.stats(card.id)
                out["trial_scope"] = scope
                out["n_trials_family"] = _dedup_trial_sharpes(
                    ledger, card.id, float(bpy), scope="family")[0]
                round_row = ledger.round(card.round) if card.round else None
                out["n_trials_round"] = (ledger.round_n_dedup(card.round)
                                         if round_row is not None else None)
                out["project_n_trials"] = st["project_n_dedup"]
                out["n_trials_project_research"] = st[
                    "project_n_dedup_research"]
                # T85（第十三轮 §4.1）：关卡 v3 主检验 N 的轮次内成分
                # （N = project_n_dedup_rounds + 候选 continues_family 的
                # lineage_n_dedup，见 ledger.primary_test_n）——只报告，
                # 现行 G4 语义不变（T86 接线 v3 主检验时作取数来源）
                out["project_n_dedup_rounds"] = st["project_n_dedup_rounds"]
                return out
            _run_recorded("G4", _g4)
        if "V3" in wanted:
            # T86 接线（T85 冻结后）：G0/G3 结果可复用（同一次运行里跑过
            # 就不再重算）；combos 供诊断的参数邻域复用
            _run_recorded("V3", lambda: _v3_gate_evaluate(
                ledger, card, cfg=cfg, ctx=v3_ctx,
                cards_dir=Path(args.cards_dir), strategy=strategy, spec=spec,
                bars=bars, costs=costs, params=params, base=base,
                summary=summary, contract_size=sizes[symbol], symbol=symbol,
                combos=combos, g0_result=results.get("G0"),
                g3_result=results.get("G3"), data_sha=dsha, seed=seed,
                known_gaps=known_gaps))
        if "G5" in wanted:
            # H-02：G5 准入绑定当前试验身份（本次基准运行的参数/数据哈希/
            # 成本版本/代码指纹/冻结门槛）；H-03：轮次上限随原子占位复查
            g5_trial = vgate.LockboxTrial(
                params=rec_params, data_sha256=dsha,
                cost_model_version=cost_version,
                strategy_sha=strategy_sha, gates_hash=gsha)
            _run_recorded("G5", lambda: _g5_orchestrate(
                ledger, card, strategy=strategy, spec=spec, params=params,
                base_summary=summary, bars=bars, costs=costs,
                session_policy=policy_by_sym[symbol],
                data_root=data_root, trial=g5_trial,
                round_id=(round_ctx or {}).get("round_id"),
                round_lockbox_uses_max=(round_ctx or {}).get(
                    "lockbox_uses_max"),
                approved_by=args.approved_by or args.actor, cfg=cfg))

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
        # T64/D33：G4 报告同时列谱系/轮次/项目三种 N 与取数口径
        g4 = results.get("G4")
        if g4 is not None and "n_trials_family" in g4:
            n_round = g4.get("n_trials_round")
            n_round_s = "—（无轮次：轮次制之前/之外）" if n_round is None \
                else str(n_round)
            report += (
                "\n## G4 试验计数（T64/D33）\n\n"
                f"- 取数口径 dsr_trial_scope = {g4.get('trial_scope')}"
                "（[g4].dsr_trial_scope；D34 冻结 = rounds_lineage"
                "（T85/L1））\n"
                f"- 谱系 N：{g4['n_trials_family']}"
                "（谱系去重口径，现行语义）\n"
                f"- 轮次 N：{n_round_s}\n"
                f"- 项目 N（research 口径）："
                f"{g4.get('n_trials_project_research')}"
                "（只计 run_class=research；dsr_trial_scope=project 时"
                " G4 用这个）\n"
                f"- 项目 N（轮次内，T85）：{g4.get('project_n_dedup_rounds')}"
                "（run_class=research 且 round_id 非空——关卡 v3 主检验 N 的"
                "第一成分；N = 它 + 候选延续谱系的 lineage_n_dedup，"
                "见 ledger.primary_test_n）\n")
        # T86：关卡 v3 报告——模式/主检验/否决 + N 的组成三行（与台账
        # primary_test_n 逐项一致，可端到端对账）
        v3 = results.get("V3")
        if v3 is not None and "n_composition" not in v3:
            report += (f"\n## 关卡 v3（T86 接线）\n\n"
                       f"- 评估失败（fail-closed，不通过）："
                       f"{v3.get('error', '未知错误')}\n")
        elif v3 is not None:
            comp = v3["n_composition"]
            fam = comp.get("continues_family") or ""
            mode_line = (
                "事件模式（轮次 [evaluation] mode=\"event\"→[v3.event]："
                "随机化参照主检验 p ≤ α/N，成本 ×1.5 否决，买入持有超额与"
                "延迟一根不适用，四项诊断只报告）"
                if v3["mode"] == "event" else
                f"bar 级（[v3].primary_test={cfg.primary_test} 主检验 + "
                "三项否决：成本 ×1.5 / 延迟一根 / 纯多头相对买入持有超额；"
                "诊断只报告）")
            vetoes = v3["vetoes"]
            def _veto_s(name, key):
                v = vetoes[name]
                if v.get("applicable") is False:
                    return f"{key}：不适用（事件模式，D35 修改 2）"
                return f"{key}：{'✓' if v['passed'] else '✗'}"
            primary = v3["primary"]
            primary_s = (
                f"{primary['test']} → "
                + ("✓" if primary["passed"] else "✗")
                + (f"（p={primary['p_value']:.4f} ≤ α/N="
                   f"{primary['threshold']:.5f}，N={primary['n_tests']}，"
                   f"随机化 {primary['sims']} 次（下限 "
                   f"{primary['draws_min']}），事件 {primary['n_events']} 个）"
                   if primary["test"] == "randomization"
                   else (f"（probability={primary.get('probability')}，"
                         f"N_eff={v3.get('n_eff')}）"
                         if primary.get("probability") is not None else ""))
                + (f"——fail-closed：{primary['reason']}"
                   if primary.get("reason") else ""))
            report += (
                "\n## 关卡 v3（T86 接线）\n\n"
                f"- 模式：{mode_line}\n"
                f"- 主检验：{primary_s}\n"
                f"- G0：{'✓' if v3['checks']['G0']['passed'] else '✗'}；"
                + _veto_s("cost_scale_x15", "成本 ×1.5") + "；"
                + _veto_s("delay_1_bar", "延迟一根") + "；"
                + _veto_s("buyhold_excess", "买入持有超额") + "\n"
                + (f"- 样本外收益来源：{v3['oos_source']}\n"
                   if v3.get("oos_source") else "")
                + "\n### N 的组成（[g4].dsr_trial_scope = rounds_lineage，"
                "T85 冻结 L1）\n\n"
                f"- 轮次内（project_n_dedup_rounds）："
                f"{comp['project_n_dedup_rounds']}\n"
                f"- 延续谱系（lineage_n_dedup，continues_family="
                f"{fam or '—（全新机制）'}）：{comp['lineage_n_dedup']}"
                f"（{comp.get('continues_family_source', '')}）\n"
                f"- 其余历史（pre_round_other_n_dedup，只报告不进门槛）："
                f"{comp['pre_round_other_n_dedup']}\n"
                f"- N = 轮次内 + 延续谱系 = {comp['n']}"
                + (f"（N_eff = max(N, n_min) = {v3.get('n_eff')}）"
                   if v3.get("n_eff") is not None else "（事件分支不吃 n_min）")
                + "\n")
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
    """T37/M6/D27 + R6/C5：校验 runs/gates/lockbox_uses/candidate_checks/
    rounds 五条哈希链 + chain_meta 快照（断链/篡改/删尾/未登记写入）。"""
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
    print("✓ 台账哈希链完好（runs/gates/lockbox_uses/candidate_checks/"
          "rounds 五链逐行校验 + chain_meta 快照比对通过）")
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
    pc.add_argument("--split", default=None,
                    help="概率模式预筛的分界日期（月初；缺省自动 "
                         "propose_split 提案，T76/D50）")
    pc.set_defaults(func=cmd_candidate_check)

    p_prob = sub.add_parser("prob", help="P1 概率模式检验（T90/D42）")
    prob_sub = p_prob.add_subparsers(dest="prob_cmd", required=True)
    ptst = prob_sub.add_parser("test", help="bt prob test --card P###-v#："
                                "真实触发 vs 随机化触发的 Δp 检验（发现段）")
    ptst.add_argument("--card", required=True)
    ptst.add_argument("--strategy", required=True)
    ptst.add_argument("--cards-dir", default="research/cards")
    ptst.add_argument("--data-root", default=None)
    ptst.add_argument("--end", default=None)
    ptst.add_argument("--spread-fraction", type=float, default=0.1)
    ptst.add_argument("--commission", type=float, default=0.0)
    ptst.add_argument("--sims", type=int, default=400,
                      help="月内置换次数（p 值分辨率）")
    ptst.add_argument("--specimen", action="store_true")
    ptst.add_argument("--ops-ack", action="store_true")
    ptst.add_argument("--actor", default="bt", help="台账 actor 字段")
    ptst.add_argument("--ledger", default=None,
                      help="台账路径（缺省按数据根解析）")
    ptst.set_defaults(func=cmd_prob_test)
    prpt = prob_sub.add_parser("round-report", help="本轮全部假设 Holm + "
                               "推进判定，产出待审核报告")
    prpt.add_argument("round", help="轮次 id（R#）")
    prpt.add_argument("--rounds-dir", default="research/rounds")
    prpt.add_argument("--candidates-dir", default="research/candidates",
                      help="候选预筛 JSON 所在目录（成本条件取数）")
    prpt.add_argument("--data-root", default=None)
    prpt.add_argument("--ledger", default=None)
    prpt.set_defaults(func=cmd_prob_round_report)

    p_fit = sub.add_parser("fit", help="概率卡拟合+冻结模型（T73A/D43）")
    p_fit.add_argument("card", help="卡 id（P###-v#）或 .toml 路径")
    p_fit.add_argument("--strategy", required=True,
                       help="策略引用 module:Class（与卡绑定一致）")
    p_fit.add_argument("--cards-dir", default="research/cards")
    p_fit.add_argument("--data-root", default=None)
    p_fit.add_argument("--end", default=None, help="序列末端（缺省全量；"
                       "discovery 卡会被分界校验拦截）")
    p_fit.add_argument("--spread-fraction", type=float, default=0.1)
    p_fit.add_argument("--commission", type=float, default=0.0)
    p_fit.set_defaults(func=cmd_fit)

    p_seg = sub.add_parser("segment", help="P 线发现段/验证段分界（T76/D50）")
    seg_sub = p_seg.add_subparsers(dest="segment_cmd", required=True)
    segp = seg_sub.add_parser(
        "propose", help="各品种共同覆盖中点，取整到月初（确定性提案）")
    segp.add_argument("--symbols", required=True,
                      help="逗号分隔，如 XAUUSD,EURUSD")
    segp.add_argument("--timeframe", default="H1")
    segp.add_argument("--data-root", default=None,
                      help="数据根（缺省研究库 data_research）")
    segp.set_defaults(func=cmd_segment_propose)

    p_check = sub.add_parser("card", help="假设卡操作")
    card_sub = p_check.add_subparsers(dest="card_command", required=True)
    pchk = card_sub.add_parser("check", help="校验卡模式并显示提交状态")
    pchk.add_argument("path", help="卡文件路径（research/cards/H012.toml）")
    pchk.set_defaults(func=cmd_card_check)

    p_round = sub.add_parser("round", help="研究轮次操作（T63/D32，铁律 16）")
    round_sub = p_round.add_subparsers(dest="round_command", required=True)
    pro = round_sub.add_parser(
        "open", help="开启轮次：前置条件全过 → rounds 表入哈希链")
    pro.add_argument("round", help="轮次 id（R1）或 research/rounds/R1.toml 路径")
    pro.add_argument("--rounds-dir",
                     default=str(REPO_ROOT / "research" / "rounds"))
    pro.add_argument("--ledger", default=str(Path("data") / "research"
                                             / "ledger.sqlite"),
                     help="台账路径；缺省 data/research/ledger.sqlite")
    pro.set_defaults(func=cmd_round_open)
    prs = round_sub.add_parser("status", help="轮次状态与预算用量")
    prs.add_argument("round", help="轮次 id（R1）")
    prs.add_argument("--ledger", default=str(Path("data") / "research"
                                             / "ledger.sqlite"))
    prs.set_defaults(func=cmd_round_status)
    prc = round_sub.add_parser(
        "close", help="关闭轮次并生成 docs/research/round-R#-<日期>.md 骨架")
    prc.add_argument("round", help="轮次 id（R1）")
    prc.add_argument("--ledger", default=str(Path("data") / "research"
                                             / "ledger.sqlite"))
    prc.add_argument("--out", default=None,
                     help="报告骨架落盘路径；缺省 docs/research/"
                          "round-R#-<日期>.md")
    prc.set_defaults(func=cmd_round_close)

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
    p_gate.add_argument("--event-kind", choices=("month_end", "london_fix"),
                        default=None,
                        help="T86：关卡 V3 事件分支的事件窗类别（轮次 "
                             "[evaluation] mode=\"event\" 的卡必填——事件窗"
                             "按预注册机制锚定，不静默取缺省）")
    p_gate.add_argument("--event-side", choices=("long", "short"),
                        default="long",
                        help="T86：事件分支的方向（静态方向；信号驱动的"
                             "动态方向由候选机制另行声明）")
    p_gate.add_argument("--event-sims", type=int, default=None,
                        help="T86：事件分支随机化次数（缺省 = 下限 "
                             "max(1999, ⌈20N/α⌉)；低于下限拒绝运行）")
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
