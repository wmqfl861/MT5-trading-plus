"""A 层回测 CLI。

示例：
    .venv/Scripts/python.exe scripts/run_backtest.py \
        --strategy mt5quant.strategies.library.buy_hold:BuyHold \
        --symbol XAUUSD --timeframe H1 --params '{}' \
        --research-store --end 2026-09-01 --out data/reports/backtests

约定：
- 数据根（T27/H3 + T38/H4/D24）：**缺省读研究库 data_research**（管理员用
  scripts/export_research_store.py 导出的隔离副本：只含 time < cutoff 的
  K 线 + 成本缓存文件，无 tick，锁箱语义自然满足——研究库里没有锁箱段）；
  缺省时 data_research 不存在 → 报错指路 export_research_store，绝不静默
  回退主库。读主库须显式 --main-store（data_root=data；--data-root 指到
  主库也一样要求该 flag）；读主库的锁箱期（--end 越过 cutoff）再加
  --ops-ack（运维测量专用，manifest 记 ops_ack，不产生研究结论）。
  为什么用 CLI flag 而不是环境变量：命令行里可见、可审计、可复制，
  不依赖会话全局状态；真正的边界是 ACL（docs/p6/permissions.md），
  flag 只是把研究工作流自然指向隔离副本。--research-store（T27）保留为
  兼容别名（缺省已是研究库）。研究库模式下 --costs auto 必须命中
  data_research/costs/spread_model.json（无 tick 可自建，缺失报错指路
  export_research_store）。
- --end 经 mt5data.lockbox.research_bars（锁箱语义：显式越过 cutoff 的请求
  直接报错，绝不静默截断）；无 lockbox.json 时返回全量，行为与直读一致。
- 金额口径：contract_size 取 --contract-size，否则读
  {data-root}/specs/latest.json（scripts/snapshot_specs.py 产出）；两者都没有
  → 报错退出（绝不静默按 1 折算——那会让盈亏差几百到十万倍）。
- 成本：--costs auto（默认）＝tick 实测点差+滑点，点差默认**相对口径**
  （--spread-mode relative，R1：p90 of (ask−bid)/mid 按小时，回测时按成交
  基准价缩放；--spread-mode absolute 为旧口径）；佣金与隔夜费优先
  {data-root}/costs/calibrated.json（scripts/calibrate_costs.py 实测），其次
  品种快照推算（SwapModel.from_snapshot）；两者都没有 → 报错，除非显式
  --allow-zero-swap（零隔夜费会写进 manifest）。--costs zero 用零成本占位。
- 点差模型缓存（T28/M8）：--costs auto 优先加载 {data-root}/costs/
  spread_model.json（scripts/build_cost_model.py 产出，按 tick 水位失效），
  不再每次回测重读全部原始 tick（XAUUSD 7,780 万行全列约 3.7 GB）。缓存
  缺失 → 明确报错指路（加 --build-cost-model 可本次运行内从 tick 现建——
  选择"缺缓存报错 + 显式 flag"而非静默直建：批量/计划任务里的隐性 4 GB
  加载正是 M8 要消除的）；水位变化而模型未重建 → 同样报错，绝不静默用
  旧模型。函数级 build_costs 保留无缓存直建回退（bt.py / 对账脚本 / 测试
  的兼容路径，行为与旧版一致）。
- 时钟校验（R5）：读 {data-root}/server_clock.json 最新有效样本，与配置的
  server_utc_offset_hours 比对，超限拒绝运行；无有效样本仅警告。
- 禁入窗（T20）：默认加载 config/sessions.toml（SessionPolicy，与实盘风控
  共用的唯一定义源），禁入时段的开仓信号推迟到下一个允许的 bar 开盘；
  其 sha256 写进 manifest 与 cost_model_version。点差模型按纽约本地小时
  分桶，缺统计小时按 missing_hour_policy="forbid" 禁入并把 miss 标注写进
  cost_model_version。
- 定手数（T23/M2）：**默认按风险定手数**——--risk-per-trade 缺省 0.005
  （单笔风险 0.5% 资金；引擎按止损距离折算手数，见 mt5quant.sizing）。
  语义（resolve_lots_mode）：
    * --volume-lots 显式给出 → 固定手数（旧口径；与默认风险值不冲突）；
    * --risk-per-trade 0 → 固定手数（手数取 --volume-lots，未给则 1.0）；
    * --risk-per-trade >0 且 --volume-lots 同时显式给出 → 拒绝（口径二义）；
    * 两者都未给 → 风险定手数 0.005。
  风险定手数需要品种 vol_min/step/max：自动读 {data-root}/specs/latest.json
  （mt5quant.sizing.load_volume_limits，缺数据报错指路 snapshot_specs），
  定手数方式写进 manifest（extra.sizing）与 summary（skipped_zero_lots
  为跳过的零手数入场计数）。
- 代码指纹（T37/D23）：运行前先过 cards.ensure_strategy_clean（策略类
  所在模块 + 其 import 的 strategies/library 内模块必须已提交且工作区
  干净），指纹（strategy_sha）写进 manifest——报告可追溯到产生它的
  确切策略代码；改策略代码请先提交（配合研究卡的新版本，见 bt.py）。
- 每次运行写入独占目录 {out}/{SYMBOL}_{TF}_{UTC戳}_{随机}，不覆盖历史运行。
"""
import argparse
import hashlib
import importlib
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5data.clock import (  # noqa: E402
    ClockMismatchError, check_server_offset, latest_valid_sample,
)
from mt5data.lockbox import research_bars  # noqa: E402
from mt5data.sessions import SessionPolicy, load_session_policy  # noqa: E402
from mt5data.store import TIMEFRAME_SECONDS, load_bars  # noqa: E402
from mt5data.ticks import load_ticks, tick_watermark  # noqa: E402
from mt5quant.backtest.engine_a import run_backtest  # noqa: E402
from mt5quant.backtest.engine_b import run_backtest_ticks  # noqa: E402
from mt5quant.backtest.report import make_run_dir, summarize, write_report  # noqa: E402
from mt5quant.research import cards  # noqa: E402
from mt5quant.sizing import RiskSizing, load_volume_limits  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
SESSIONS_PATH = REPO_ROOT / "config" / "sessions.toml"

# T27（H3）+ T38（H4/D24）：数据根缺省即研究库 data_research（可选开关
# 等于没有隔离——H4：--research-store 时代的缺省仍是主库）。不用环境变量：
# 研究账户拿不到能写环境变量的 shell 也不该依赖隐式全局状态，命令行里可见
# 的 flag 更可审计；真正的边界是 ACL（permissions.md），flag 只是把研究
# 工作流自然指向隔离副本。显式 --data-root 永远优先（指到主库的仍须
# --main-store 确认）。
DEFAULT_DATA_ROOT = "data"
RESEARCH_STORE_ROOT = "data_research"


def resolve_data_root(explicit: str | None, research_store: bool = False,
                      main_store: bool = False) -> str:
    """--data-root 显式值 > --main-store（data）> 缺省/--research-store
    （data_research，T38/D24 翻转）；--main-store 与 --research-store 同时
    给出 → ValueError（口径二义）。"""
    if main_store and research_store:
        raise ValueError(
            "--main-store 与 --research-store 互斥：读主库（--main-store，"
            "缺省 data）或研究库（--research-store；T38 起缺省即研究库，"
            "通常两个都不用）二选一")
    if explicit is not None:
        return explicit
    if main_store:
        return DEFAULT_DATA_ROOT
    return RESEARCH_STORE_ROOT


def classify_store(data_root: str) -> str:
    """T38/D24：数据根三分类——"research"（研究库 data_research）、
    "main"（主库 data）、"custom"（其余：测试与临时数据根）。

    判定取字面相对名或解析到仓库内对应目录（换写法指同一目录不改性质）；
    custom 根不套研究库约束（水位核验等成本路径按主库口径走）。
    """
    p = Path(data_root)
    if p == Path(RESEARCH_STORE_ROOT):
        return "research"
    if p == Path(DEFAULT_DATA_ROOT):
        return "main"
    rp = p.resolve()
    if rp == (REPO_ROOT / RESEARCH_STORE_ROOT).resolve():
        return "research"
    if rp == (REPO_ROOT / DEFAULT_DATA_ROOT).resolve():
        return "main"
    return "custom"


def sessions_sha256(path: str) -> str:
    """config/sessions.toml 的 sha256（写进 manifest 与 cost_model_version）。"""
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class ZeroCosts:
    """零成本占位：实现 engine_a.Costs Protocol。"""

    def spread_p90(self, symbol, hour_ny, *, price=None):
        return 0.0

    def slippage(self, symbol, hour_ny, *, price=None):
        return 0.0

    def commission_per_lot(self, symbol):
        return 0.0

    def overnight_fee(self, symbol, side, from_ts, to_ts):
        return 0.0


def resolve_contract_size(data_root: str, symbol: str, override: float | None) -> float:
    """合约大小：CLI 覆盖 > {data_root}/specs/latest.json；都没有 → ValueError。"""
    if override is not None:
        if not override > 0:
            raise ValueError(f"--contract-size 必须为正，收到 {override}")
        return float(override)
    snap_path = os.path.join(data_root, "specs", "latest.json")
    if os.path.exists(snap_path):
        with open(snap_path, encoding="utf-8") as f:
            snap = json.load(f)
        spec = snap.get(symbol) if isinstance(snap, dict) else None
        cs = spec.get("contract_size") if isinstance(spec, dict) else None
        if isinstance(cs, (int, float)) and cs > 0:
            return float(cs)
    raise ValueError(
        f"{symbol} 合约大小未知：先跑 scripts/snapshot_specs.py 生成 "
        f"{snap_path}，或显式传 --contract-size（XAUUSD 常见 100、EURUSD 100000）"
    )


# ------------------------------------------------ T23（M2）：定手数口径解析
#
# CLI 语义（--risk-per-trade 与 --volume-lots，两者 argparse 默认均为 None）：
#   * 都未给               → 风险定手数，risk_fraction=0.005（缺省即新口径）
#   * 仅 --volume-lots     → 固定手数（旧口径；显式手数优先于缺省风险值）
#   * 仅 --risk-per-trade>0→ 风险定手数（该比例）
#   * 仅 --risk-per-trade 0→ 固定手数（手数取 --volume-lots，未给则 1.0）
#   * 两者同时显式给出且风险>0 → 拒绝（口径二义：不知道该听谁的）
# run_backtest 与 bt.py（screen/gate）共用本函数，保证两条路径同一语义。

DEFAULT_RISK_PER_TRADE = 0.005


def resolve_lots_mode(risk_per_trade: float | None,
                      volume_lots: float | None) -> tuple[float | None,
                                                           RiskSizing | None,
                                                           dict]:
    """把 CLI 手数参数解析成引擎入参（volume_lots, sizing, 说明 dict）。

    说明 dict 写进 manifest（run_backtest）/台账 metrics（bt）：
    {"mode": "risk", ...} 或 {"mode": "fixed", ...}。
    """
    if volume_lots is not None:
        if not (isinstance(volume_lots, (int, float))
                and math.isfinite(volume_lots) and volume_lots > 0):
            raise ValueError(f"--volume-lots 必须是有限正数，收到 {volume_lots!r}")
    if risk_per_trade is not None:
        if not (isinstance(risk_per_trade, (int, float))
                and math.isfinite(risk_per_trade) and risk_per_trade >= 0):
            raise ValueError(
                f"--risk-per-trade 必须是非负有限数（0 = 改用固定手数），"
                f"收到 {risk_per_trade!r}")
    explicit_risk = risk_per_trade is not None and risk_per_trade > 0
    if explicit_risk and volume_lots is not None:
        raise ValueError(
            f"--risk-per-trade {risk_per_trade} 与 --volume-lots "
            f"{volume_lots} 互斥：按风险定手数（--risk-per-trade，缺省 "
            f"{DEFAULT_RISK_PER_TRADE}）或固定手数（--volume-lots）二选一"
            "（--risk-per-trade 0 = 显式选固定手数）")
    if volume_lots is not None:
        return volume_lots, None, {
            "mode": "fixed", "volume_lots": float(volume_lots)}
    if explicit_risk:
        return None, RiskSizing(risk_fraction=float(risk_per_trade)), {
            "mode": "risk", "risk_fraction": float(risk_per_trade),
            "basis": "initial", "fallback_stop_atr": 1.0}
    if risk_per_trade is not None and risk_per_trade == 0:
        return 1.0, None, {
            "mode": "fixed", "volume_lots": 1.0,
            "note": "--risk-per-trade 0 = 固定手数（未显式给 --volume-lots，取 1.0）"}
    return None, RiskSizing(risk_fraction=DEFAULT_RISK_PER_TRADE), {
        "mode": "risk", "risk_fraction": DEFAULT_RISK_PER_TRADE,
        "basis": "initial", "fallback_stop_atr": 1.0}


def check_clock(data_root: str, *, tol_s: float = 120.0) -> None:
    """R5：最新有效时钟样本 vs 配置的 server_utc_offset_hours；无样本仅警告。"""
    sample = latest_valid_sample(os.path.join(data_root, "server_clock.json"))
    expected = 0.0
    for cfg_name in ("config.json", "gateway.json"):
        p = os.path.join(os.getcwd(), cfg_name)
        if os.path.exists(p):
            from mt5gw import load
            expected = load(p).server_utc_offset_hours
            break
    if sample is None:
        print(f"⚠ {data_root}/server_clock.json 无有效时钟样本（休市或未测），"
              f"跳过时区校验；开市后跑 scripts/measure_server_clock.py")
        return
    check_server_offset(sample["offset_s"], expected, tol_s=tol_s)
    print(f"✓ 时钟校验：实测偏移 {sample['offset_s']:+.0f}s 与配置 "
          f"{expected:g}h 一致（样本 {sample['wall_utc']}）")


def _load_calibrated(data_root: str) -> dict | None:
    p = os.path.join(data_root, "costs", "calibrated.json")
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


# ------------------------------------------------ T28（M8）：点差模型缓存
#
# 缓存文件 {data-root}/costs/spread_model.json 由 scripts/build_cost_model.py
# 产出（本模块的 save/load 是同一读写的另一入口，供 --build-cost-model 与
# 测试使用）。IO/校验逻辑放这里而不是 build_cost_model.py，是因为
# build_costs 在函数级就要消费它，而 bt.py / 测试经 importlib 装载本模块时
# scripts 目录不在 sys.path 上（不能顶层 import 兄弟脚本）。

REBUILD_HINT = ("重跑 scripts/build_cost_model.py（或回测时加 --build-cost-model"
                " 本次现建）")


def spread_model_cache_path(data_root: str) -> str:
    return os.path.join(data_root, "costs", "spread_model.json")


def canonical_json_sha256(payload: dict) -> str:
    """模型负载的规范 sha256（sort_keys + 紧致分隔符，任何等价 dict 同哈希）。"""
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")).hexdigest()


def save_spread_model_cache(path: str, model, watermarks: dict) -> dict:
    """原子写缓存：{version, mode, watermarks, model, model_sha256, built_at_utc}。

    watermarks：{symbol: {rows, first_msc, last_msc}}（mt5data.ticks.
    tick_watermark 的产出）——读取方逐项比对当前水位，不一致即要求重建。
    """
    payload = model.to_dict()
    doc = {
        "version": 1,
        "mode": model.mode,
        "watermarks": {s: dict(w) for s, w in watermarks.items()},
        "model": payload,
        "model_sha256": canonical_json_sha256(payload),
        "built_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return doc


def load_spread_model_cache(path: str, symbol: str, *,
                            spread_mode: str,
                            watermark: dict | None,
                            require_watermark: bool = True):
    """读缓存并做四重校验，任何一处不过 → ValueError（提示重建，绝不静默）。

    1) 文件可读、结构完整；2) model_sha256 与模型负载一致（防手改/半截写）；
    3) 口径（mode）与请求一致；4) 品种在模型中且 tick 水位（rows/first/last）
    与当前库逐项一致。watermark=None（tick 库没了）而缓存还在属环境异常，同报错。

    采样口径（T36/R6-I2）从**模型负载**读取（to_dict 自带 sampling 键；无键的
    旧文件按 "tick"，与 SpreadModel.load 同规）——重建 SpreadModel 时不落回
    缺省值，否则 cost_model_version 会把 tick 口径的缓存错标成 bar_open_m1。

    require_watermark=False（T27 研究库）：研究库 data_research **有意**不含
    tick（tick 对研究账户拒读），水位核验无从做起——缓存必须自带水位
    （第 4 检的前半仍执行），与主库 tick 的一致性由导出方
    scripts/export_research_store.py 在导出时把关。
    """
    from mt5quant.costs import SpreadModel
    from mt5quant.costs.spread import BUCKET_TZ, SAMPLINGS, SpreadStat

    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        payload = doc["model"]
        sha = doc["model_sha256"]
        wms = doc["watermarks"]
        cached_mode = doc["mode"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise ValueError(f"点差缓存 {path} 不可读或结构不完整（{e}）——{REBUILD_HINT}") from e
    if canonical_json_sha256(payload) != sha:
        raise ValueError(
            f"点差缓存 {path} 的 model_sha256 与模型内容不一致"
            f"（文件被改动或写坏）——{REBUILD_HINT}")
    if cached_mode != spread_mode:
        raise ValueError(
            f"点差缓存口径为 {cached_mode}，本次请求 {spread_mode}——"
            f"{REBUILD_HINT} 并加 --spread-mode {spread_mode}")
    # 与 SpreadModel.load 相同的口径防线（此处直接从负载重建，不经文件）
    if payload.get("bucket_tz") != BUCKET_TZ:
        raise ValueError(
            f"点差缓存 bucket_tz={payload.get('bucket_tz')!r}"
            f"（缺标注=UTC 分桶的旧文件），与纽约本地小时口径不兼容——{REBUILD_HINT}")
    sampling = payload.get("sampling", "tick")   # 旧负载无键 → tick 口径
    if sampling not in SAMPLINGS:
        raise ValueError(
            f"点差缓存 sampling 非法：{sampling!r}（合法 {SAMPLINGS}）"
            f"——{REBUILD_HINT}")
    try:
        per = {s: {int(h): SpreadStat(**st) for h, st in hours.items()}
               for s, hours in payload["per_symbol"].items()}
        overall = {s: float(v) for s, v in payload["overall_median"].items()}
        model = SpreadModel(per, overall, mode=cached_mode, sampling=sampling)
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"点差缓存 {path} 模型负载非法（{e}）——{REBUILD_HINT}") from e
    if symbol not in per:
        raise ValueError(
            f"{symbol} 不在点差缓存中（缓存品种：{sorted(per)}）——{REBUILD_HINT}"
            f" --symbols {symbol}")
    wm_cached = wms.get(symbol)
    if wm_cached is None:
        raise ValueError(f"{symbol} 的 tick 水位不在点差缓存中——{REBUILD_HINT}")
    if not require_watermark:
        return model                       # 研究库：导出方已核水位
    if watermark is None:
        raise ValueError(
            f"{symbol} 当前无 tick 库却存在点差缓存（库被移动/删除？）——"
            f"先确认数据目录，再{REBUILD_HINT}")
    for k in ("rows", "first_msc", "last_msc"):
        if wm_cached.get(k) != watermark.get(k):
            raise ValueError(
                f"{symbol} tick 水位已变化（缓存 {k}={wm_cached.get(k)!r}，"
                f"当前 {watermark.get(k)!r}）——模型可能过期，{REBUILD_HINT}；"
                "绝不静默沿用旧模型")
    return model


def load_overshoot_from_cache(path: str, symbol: str):
    """从 v2 点差缓存的顶层 overshoot/overshoot_sha256 键装载止损越幅表
    （T36 写入、R6/C4 接线）。返回 (model|None, sha8|None, status)：

    - ("loaded", model, sha8)：表存在、sha 自洽且 symbol 在**引擎口径**
      （stop_level）下有覆盖 → 调用方注入 Costs（A-1.6 的 stop 出场越幅）；
    - ("no-table", None, None)：v1 缓存/构建未加 --overshoot → 不注入
      （保持 A-1.5 行为，cost_version 标 no-overshoot）；
    - ("basis-coverage(<原因>)", None, None)：表在但 symbol 无 stop_level
      统计（旧 version 1 adjacent_jump 表 / 品种不在主口径表）→ 同上不注入
      （注入会让引擎 stop 出场在该品种上直接抛错）；
    - overshoot_sha256 与内容不一致 → ValueError（缓存被改动，绝不静默跳过）。
    """
    from mt5quant.costs.overshoot import DEFAULT_BASIS, StopOvershootModel

    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    if "overshoot" not in doc:
        return None, None, "no-table"
    sha = doc.get("overshoot_sha256")
    if not sha or canonical_json_sha256(doc["overshoot"]) != sha:
        raise ValueError(
            f"点差缓存 {path} 的 overshoot_sha256 与越幅表内容不一致"
            f"（文件被改动或写坏）——请重建（scripts/build_cost_model.py "
            "--overshoot）")
    om = StopOvershootModel.from_dict(doc["overshoot"])
    try:
        # 探测 symbol 在引擎口径（stop_level，I3 后的主口径）下是否覆盖：
        # 只缺品种会抛（缺小时有保守回退，不抛）
        om.overshoot(symbol, 0, price=1.0, basis=DEFAULT_BASIS)
    except ValueError as e:
        return None, None, f"basis-coverage({e})"
    return om, str(sha)[:8], "loaded"


def build_costs(data_root: str, symbol: str, spread_fraction: float,
                commission_per_lot: float = 0.0, *,
                allow_zero_swap: bool = False,
                spread_mode: str = "relative",
                sessions_sha: str | None = None,
                cache: bool = True,
                research_store: bool = False) -> tuple:
    """tick 实测点差 + 佣金/隔夜费（calibrated.json > 品种快照 > 报错）。

    T28/M8：优先加载点差模型缓存 {data-root}/costs/spread_model.json（按
    tick 水位校验，绝不静默沿用过期模型），**不再为每次回测重读全部原始
    tick**（XAUUSD 全库全列约 3.7 GB）。CLI 主路径缺缓存先报错（见 main 的
    --build-cost-model）；本函数保留"无缓存→直建"回退是给 bt.py / 对账
    脚本 / 测试的兼容路径（cache=False 可强制直建）。

    research_store=True（T27/H3）：data_root 是导出的研究库 data_research
    ——它**有意**不含 tick（tick 对研究账户拒读，成本经缓存文件提供），
    此时缓存文件必须存在（缺 → 报错指路 scripts/export_research_store.py，
    研究侧无 tick 可自建），加载不做水位对照（新鲜度由导出方把关），也不
    走"无 tick → 直建/零成本"回退。

    spread_mode（R1/T3）：relative（默认）＝(ask−bid)/mid 按**纽约本地小时**
    分位（T20：换日尖峰不被 DST 摊薄），引擎按成交基准价缩放——近期点差
    结构可外推到价格水平不同的历史；absolute＝旧口径（价格单位，忽略
    price）。cost_version 里写明 rel/abs。

    隔夜费（T20/H1）：calibrated 与快照两条路径的**三倍日一律取快照
    swap_rollover3days**（经 mt5_to_py_weekday），计费时刻来自 SwapRule
    默认（17:00 America/New_York，DST 感知）——校准费率按同一规则复算，
    不再用 frozenset() 摊薄。

    sessions_sha（T20）：config/sessions.toml 的 sha256；给出时写进
    cost_version（sess[前 8 位]）。缺统计小时按 missing_hour_policy=forbid
    标注（missNY[...]），由 entry_policy_for 并入引擎禁入（覆盖天数阈值
    min_days_per_hour 也从策略取——n_days 不足的小时同按缺统计禁入，T36）。

    T36/R6（C4+I2）：缓存命中时从同一缓存文件装载止损越幅表（顶层
    overshoot 键，build_cost_model --overshoot 写入）注入 Costs——
    引擎 reason="stop" 的出场成交价加该纽约小时越幅 p50（A-1.6）。无越幅键
    /品种不覆盖 → overshoot_model=None 保持 A-1.5 行为，版本串标
    no-overshoot。cost_model_version 纳入模型内容 sha 前 8 位、采样口径与
    越幅表 sha——不同缓存内容必产生不同版本串（台账去重据此区分成本假设）。

    返回 (costs, cost_version)。佣金优先 calibrated 实测，其次 CLI 显式值；
    隔夜费优先 calibrated 实测费率（free_of_swap=true 属合法实测零费），其次
    快照推算；两者都没有且未 --allow-zero-swap → ValueError（禁止静默按 0）。
    """
    from mt5quant.costs import (
        CommissionModel, Costs, SlippageModel, SpreadModel, SwapModel,
        SwapRule,
    )
    from mt5quant.costs.swap import mt5_to_py_weekday

    cache_path = spread_model_cache_path(data_root)
    wm = tick_watermark(data_root, symbol)   # 只读 parquet 元数据，不载数据行
    used_cache = False
    if research_store:
        # T27：研究库无 tick（拒读），缓存必须存在；不做水位对照（导出方已核）
        if not os.path.exists(cache_path):
            raise ValueError(
                f"研究库缺成本缓存 {cache_path}——研究侧无 tick 库不能自建；"
                "请管理员在主库侧重跑 scripts/export_research_store.py"
                "（它会同时核验缓存与主库 tick 水位的一致性）")
        spread = load_spread_model_cache(cache_path, symbol,
                                         spread_mode=spread_mode,
                                         watermark=None,
                                         require_watermark=False)
        used_cache = True
    elif cache and wm is not None and os.path.exists(cache_path):
        spread = load_spread_model_cache(cache_path, symbol,
                                         spread_mode=spread_mode,
                                         watermark=wm)
        used_cache = True
    else:
        if cache and wm is None and os.path.exists(cache_path):
            raise ValueError(
                f"{symbol} 无 tick 库却存在点差缓存 {cache_path}——环境异常，"
                f"请检查数据目录；确认后可{REBUILD_HINT}"
                "（研究库请用 --research-store）")
        ticks = load_ticks(data_root, symbol,
                           columns=["time_msc", "bid", "ask"])
        if ticks is None or len(ticks) == 0:
            print(f"⚠ {symbol} 无 tick 数据，退回零成本；"
                  f"先跑 scripts/download_ticks.py 可启用实测点差/滑点")
            return ZeroCosts(), "zero-v0(no-ticks)"
        spread = SpreadModel.from_ticks(symbol, ticks, mode=spread_mode)
    slip = SlippageModel(spread, spread_fraction=spread_fraction)
    mode_tag = "rel" if spread_mode == "relative" else "abs"

    calibrated = _load_calibrated(data_root)
    cal_sym = None
    if calibrated and symbol in calibrated.get("symbols", {}):
        cal_sym = calibrated["symbols"][symbol]

    # ---- 佣金：calibrated 实测 > CLI 显式值（默认 0，标准账户点差内含属合法） ----
    if cal_sym and cal_sym.get("commission", {}).get("round_trip_per_lot") is not None:
        rt = float(cal_sym["commission"]["round_trip_per_lot"])
        commission = CommissionModel(default_per_lot=rt)
        comm_tag = f"comm{rt:g}-calibrated"
    else:
        commission = CommissionModel(default_per_lot=commission_per_lot)
        comm_tag = f"comm{commission_per_lot:g}-cli"

    # ---- 隔夜费：calibrated 实测费率 > 快照推算 > 报错/显式零 ----
    swap = None
    swap_tag = "swap?"
    cal_sw = cal_sym.get("swap") if cal_sym else None
    cal_rate = None
    if cal_sw:
        lng_r = cal_sw.get("long", {}).get("per_lot_per_night")
        sht_r = cal_sw.get("short", {}).get("per_lot_per_night")
        if lng_r is not None or sht_r is not None:
            cal_rate = (lng_r if lng_r is not None else 0.0,
                        sht_r if sht_r is not None else 0.0)
    if cal_rate is not None:
        # T20：三倍日一律取快照 swap_rollover3days——校准费率是"每夜"口径，
        # 回测按同一规则（含 ×3）复算才与实测对上（探针 P3 的修复）。
        # 快照缺品种/字段 → 如实报错，不静默按无三倍日。
        snap_entry = _snapshot_swap_entry(data_root, symbol)
        triple_py = mt5_to_py_weekday(snap_entry["swap_rollover3days"])
        swap = SwapModel(per_symbol={symbol: SwapRule(
            long_per_day=cal_rate[0], short_per_day=cal_rate[1],
            triple_weekdays=frozenset({triple_py}))})
        swap_tag = f"swap-cal(tri{triple_py})"
    elif cal_sw and cal_sw.get("free_of_swap") is True:
        swap = SwapModel(per_symbol={symbol: SwapRule(
            long_per_day=0.0, short_per_day=0.0)})
        swap_tag = "swap0-calibrated(free)"
    else:
        snap_path = os.path.join(data_root, "specs", "latest.json")
        try:
            with open(snap_path, encoding="utf-8") as f:
                snap = json.load(f)
            swap = SwapModel.from_snapshot(snap, [symbol])
            swap_tag = "swap-snapshot"
        except (OSError, KeyError, NotImplementedError, ValueError) as e:
            if allow_zero_swap:
                swap = SwapModel(per_symbol={symbol: SwapRule(
                    long_per_day=0.0, short_per_day=0.0)})
                swap_tag = "swap0-explicit-allow"
                print(f"⚠ 隔夜费无法推算（{e}），--allow-zero-swap 显式按 0 计"
                      "（已写进 manifest）")
            else:
                raise ValueError(
                    f"{symbol} 隔夜费未知（calibrated.json 无实测、快照推算失败：{e}）。"
                    "先跑 scripts/calibrate_costs.py / scripts/snapshot_specs.py，"
                    "或显式 --allow-zero-swap（会记录在 manifest）") from e

    # ---- T20：缺统计小时（missing_hour_policy=forbid）写进 cost_version ----
    missing = spread.missing_hours(symbol)
    if missing:
        if len(missing) > 8:
            miss_tag = f"-missNY[{len(missing)}h]"
        else:
            miss_tag = "-missNY[" + ",".join(str(h) for h in missing) + "]"
    else:
        miss_tag = ""
    sess_tag = f"-sess[{sessions_sha[:8]}]" if sessions_sha else ""

    # ---- T36/R6（C4）：缓存命中时装载越幅表并注入 Costs ----
    overshoot_model = None
    ov_tag = "-no-overshoot"
    if used_cache:
        om, ov_sha8, ov_status = load_overshoot_from_cache(cache_path, symbol)
        if om is not None:
            overshoot_model = om
            ov_tag = f"-ov[{ov_sha8}]"
        else:
            print(f"⚠ 缓存无止损越幅表（{ov_status}）：stop 出场不加越幅"
                  "（A-1.5 行为）——重建加 --overshoot"
                  "（scripts/build_cost_model.py）")

    # ---- I2：版本串纳入模型内容 sha/采样口径/越幅有无（成本身份） ----
    model_sha8 = canonical_json_sha256(spread.to_dict())[:8]
    costs = Costs(spread=spread, slippage_model=slip, commission=commission,
                  swap=swap, overshoot_model=overshoot_model)
    if research_store:
        src = "研究库缓存命中（导出时已对照主库 tick 水位）"
    elif used_cache:
        src = f"缓存命中，tick 水位一致（{wm['rows']:,} 条实测）"
    else:
        src = f"tick {len(ticks):,} 条实测点差"
    print(f"✓ 成本：{src}（{mode_tag}@NY/{spread.sampling}#{model_sha8} p90"
          f"{'×成交价' if mode_tag == 'rel' else ''}）；{comm_tag}；{swap_tag}"
          + (f"；缺统计 {len(missing)} 个纽约小时（forbid 禁入）" if missing else ""))
    return (costs,
            f"tick-spread-p90[{mode_tag}@NY/{spread.sampling}#{model_sha8}]"
            f"+slip{spread_fraction}-{comm_tag}-{swap_tag}{ov_tag}"
            f"{miss_tag}{sess_tag}")


def _snapshot_swap_entry(data_root: str, symbol: str) -> dict:
    """读快照里 symbol 的条目（calibrated 三倍日来源）；缺文件/品种/字段即抛。"""
    snap_path = os.path.join(data_root, "specs", "latest.json")
    try:
        with open(snap_path, encoding="utf-8") as f:
            snap = json.load(f)
        entry = snap[symbol]
    except (OSError, KeyError) as e:
        raise ValueError(
            f"{symbol} 的 calibrated 隔夜费率需要快照的 swap_rollover3days"
            f"（三倍日，T20 不再按无三倍日摊薄），读 {snap_path} 失败：{e}"
            "——先跑 scripts/snapshot_specs.py") from e
    if "swap_rollover3days" not in entry:
        raise ValueError(
            f"{symbol} 快照条目缺 swap_rollover3days（旧快照），"
            f"重跑 scripts/snapshot_specs.py")
    return entry


def entry_policy_for(costs, symbol: str, base_policy: SessionPolicy):
    """把点差模型的缺统计小时并入禁入策略（T20：missing_hour_policy=forbid；
    T36/R6-C4：min_days_per_hour 也在策略上——n_days 不足阈值的小时同按
    缺统计禁入，此前只看小时有无统计，一天样本的小时在要求 20 天时仍放行）。

    costs 无 spread 属性（ZeroCosts 等）或策略不是 forbid → 原样返回。
    """
    if base_policy is None or base_policy.missing_hour_policy != "forbid":
        return base_policy
    spread = getattr(costs, "spread", None)
    if spread is None:
        return base_policy
    missing = spread.missing_hours(
        symbol, min_days_per_hour=base_policy.min_days_per_hour)
    if not missing:
        return base_policy
    return base_policy.with_forbidden_hours({symbol: missing})


def load_strategy(path: str):
    """按 "module:Class" 装载策略类。"""
    try:
        module_name, class_name = path.split(":", 1)
        mod = importlib.import_module(module_name)
        return getattr(mod, class_name)
    except (ValueError, ImportError, AttributeError) as e:
        raise ValueError(f"无法装载策略 {path!r}：{e}") from e


def load_series(data_root, symbol, timeframe, end, *, ops_ack: bool = False):
    """读序列：默认经锁箱加载器（end 越过 cutoff 直接报错，绝不静默截断）。

    ops_ack=True（T38/D24，沿用 T27 的 --ops-ack 语义）：运维豁免，直接
    读库（含锁箱段行），供换日/成本/执行口径等测量——只用于运维，不产生
    研究结论；调用方负责把 ops_ack 记进 manifest/台账（bt 侧记 actor=ops）。

    end 语义与 research_bars 一致：保留 time <= end 的行（含端点）——
    注意 end 时刻"开盘"的那根 bar 会整根纳入（含其整段 OHLC）；
    做切分严谨的样本外研究时请传 bar 的收盘时刻。
    """
    import pandas as pd

    end_ts = pd.Timestamp(end) if end else None
    if end_ts is not None and end_ts.tzinfo is not None:
        raise ValueError("--end 只接受不带时区的 UTC 时间（如 2026-09-01）")
    if ops_ack:
        df = load_bars(data_root, symbol, timeframe)
        if df is None or len(df) == 0:
            raise ValueError(f"无可用数据：{symbol} {timeframe}（--ops-ack 直读）")
        if end_ts is not None:
            df = df[df["time"] <= end_ts].reset_index(drop=True)
            if len(df) == 0:
                raise ValueError(
                    f"无可用数据：{symbol} {timeframe}（--end {end} 截断后为空）")
        return df
    df = research_bars(data_root, symbol, timeframe, end=end_ts)
    if df is None or len(df) == 0:
        suffix = (f"（--end {end} 截断后为空或序列不存在）" if end
                  else "（先跑 scripts/download_bars.py）")
        raise ValueError(f"无可用数据：{symbol} {timeframe}{suffix}")
    return df


def main() -> int:
    ap = argparse.ArgumentParser(description="跑 A 层回测并出标准报告")
    ap.add_argument("--strategy", required=True,
                    help="module:Class，如 mt5quant.strategies.library.buy_hold:BuyHold")
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--timeframe", required=True,
                    help="M1/H1/D1 等（TIMEFRAME_SECONDS 之一）")
    ap.add_argument("--params", default="{}", help='JSON 字符串，如 \'{"k":1}\'')
    ap.add_argument("--data-root", default=None,
                    help="数据根目录；缺省 data_research（T38/D24），"
                         "--main-store 时缺省 data（显式给出者永远优先；"
                         "指到主库的仍须 --main-store）")
    ap.add_argument("--research-store", action="store_true",
                    help="（T27 兼容别名）读导出的研究库 data_research——"
                         "T38 起缺省即研究库，本 flag 通常不用；主库仅管理员"
                         "与实盘侧可读（读主库用 --main-store）")
    ap.add_argument("--main-store", action="store_true",
                    help="读主库 data（T38/D24：缺省数据根是研究库"
                         " data_research；主库含完整锁箱段，须显式给出本 flag"
                         " 才读；--end 越过 cutoff 的锁箱期另需 --ops-ack）")
    ap.add_argument("--ops-ack", action="store_true",
                    help="运维豁免：允许读到主库锁箱期数据（--end 越过"
                         " cutoff），仅用于换日/成本/执行口径测量，不产生研究"
                         "结论；manifest 记 ops_ack（bt 侧台账记 actor=ops）")
    ap.add_argument("--end", default=None,
                    help="只用该 UTC 时刻及之前的数据（naive UTC；越过锁箱 cutoff 会报错）")
    ap.add_argument("--out", default="data/reports/backtests")
    ap.add_argument("--risk-per-trade", type=float, default=None,
                    help="按风险定手数：单笔风险占资金比例（缺省 0.005=0.5%%；"
                         "传 0 = 改用 --volume-lots 固定手数）。需快照的 "
                         "volume_min/step/max，缺则报错指路 snapshot_specs")
    ap.add_argument("--volume-lots", type=float, default=None,
                    help="固定手数（旧口径）；显式给出即用固定手数，与 "
                         "--risk-per-trade>0 同时给会拒绝")
    ap.add_argument("--initial-capital", type=float, default=10_000.0)
    ap.add_argument("--costs", choices=("auto", "zero"), default="auto",
                    help="auto=本地 tick 实测点差+滑点（佣金/隔夜未配置按 0 并说明）；zero=零成本")
    ap.add_argument("--spread-fraction", type=float, default=0.1,
                    help="--costs auto 时的滑点比例（×p90 点差）")
    ap.add_argument("--spread-mode", choices=("relative", "absolute"),
                    default="relative",
                    help="点差口径：relative=(ask−bid)/mid 按小时 p90，回测按成交基准价"
                         "缩放（R1 默认）；absolute=旧口径（价格单位，忽略 price）")
    ap.add_argument("--commission", type=float, default=0.0,
                    help="每手来回佣金（账户货币），--costs auto 时无 calibrated 实测则用此值")
    ap.add_argument("--engine", choices=("a", "b"), default="a",
                    help="a=向量化；b=tick 级事件驱动（需 tick 库覆盖回测区间）")
    ap.add_argument("--latency-ms", type=int, default=0,
                    help="--engine b：信号 bar 收盘后的执行延迟（毫秒）")
    ap.add_argument("--slippage-price", type=float, default=0.0,
                    help="--engine b：成交价叠加的额外不利滑点（价格单位）")
    ap.add_argument("--allow-zero-swap", action="store_true",
                    help="隔夜费推算不出时显式按 0 计（记入 manifest；默认报错）")
    ap.add_argument("--contract-size", type=float, default=None,
                    help="合约大小覆盖；缺省读 {data-root}/specs/latest.json")
    ap.add_argument("--cost-version", default=None,
                    help="成本模型版本（写进 manifest；缺省按 --costs 自动生成）")
    ap.add_argument("--build-cost-model", action="store_true",
                    help="--costs auto 时本次运行内（重）建点差模型缓存 "
                         "{data-root}/costs/spread_model.json（等价先跑 "
                         "scripts/build_cost_model.py；XAUUSD 全库约数分钟）。"
                         "缺省要求缓存已存在——缺缓存直接报错，不静默重读全部 tick")
    args = ap.parse_args()

    if args.timeframe.upper() not in TIMEFRAME_SECONDS:
        print(f"✗ 未知周期：{args.timeframe}")
        return 1
    try:
        params = json.loads(args.params)
    except ValueError as e:
        print(f"✗ --params 不是合法 JSON：{e}")
        return 1
    # ---- T38/D24：数据根解析与三重守卫（主库须显式豁免；研究库须存在） ----
    try:
        data_root = resolve_data_root(args.data_root, args.research_store,
                                      args.main_store)
    except ValueError as e:
        print(f"✗ {e}")
        return 1
    store_kind = classify_store(data_root)
    if store_kind == "main" and not args.main_store:
        print(f"✗ 读主库 {data_root} 须显式 --main-store（T38/D24：本 CLI 缺省"
              "只读研究库 data_research；主库含完整锁箱段，仅管理员与实盘侧"
              "可读，读锁箱期另需 --ops-ack）")
        return 1
    research_mode = store_kind == "research"
    if research_mode:
        if not os.path.isdir(data_root):
            print(f"✗ 研究库 {data_root} 不存在——T38/D24 起缺省数据根是研究库"
                  " data_research；请管理员在主库侧跑"
                  " scripts/export_research_store.py 导出（只含 time < cutoff"
                  " 的 K 线 + 成本缓存，无 tick），或显式 --data-root 指定数据"
                  "目录；绝不静默回退主库")
            return 1
        print(f"— 研究库模式（T27/H3；T38/D24 起为缺省）：data_root={data_root}"
              "（无 tick，成本经 costs/spread_model.json 缓存；库内无锁箱段，"
              "锁箱语义自然满足）")
        if args.build_cost_model:
            print("✗ 研究库没有 tick（对研究账户拒读），--build-cost-model"
                  " 无从建模型——请管理员在主库侧重跑 build_cost_model.py"
                  " 与 export_research_store.py")
            return 1
    elif store_kind == "main":
        print(f"— 主库模式（--main-store，T38/D24）：data_root={data_root}"
              "（含完整锁箱段；--end 越过 cutoff 须 --ops-ack）")
    if args.ops_ack:
        print("⚠ --ops-ack（T38/D24）：本次允许读到锁箱期数据——仅限运维测量"
              "（换日/成本/执行口径），不产生研究结论；manifest 已记 ops_ack")
    try:
        strategy_cls = load_strategy(args.strategy)
        # T37/D23：策略代码必须已提交且干净；指纹（strategy_sha）随
        # manifest 落盘——报告可追溯到产生它的确切策略代码
        cards.ensure_strategy_clean(args.strategy, REPO_ROOT)
        strategy_sha = cards.strategy_fingerprint(args.strategy, REPO_ROOT)
        contract_size = resolve_contract_size(data_root, args.symbol,
                                              args.contract_size)
        # T23：定手数口径（默认风险定手数 0.005；见 resolve_lots_mode 语义）
        lots, sizing, sizing_info = resolve_lots_mode(args.risk_per_trade,
                                                      args.volume_lots)
        if sizing is not None:
            vol_lo, vol_st, vol_hi = load_volume_limits(data_root,
                                                        args.symbol)
            sizing_info.update(vol_min=vol_lo, vol_step=vol_st, vol_max=vol_hi)
            print(f"✓ 定手数：按风险 {sizing.risk_fraction:.4%}"
                  f"（basis={sizing.basis}，vol {vol_lo}/{vol_st}/{vol_hi}）")
        else:
            print(f"✓ 定手数：固定 {lots} 手")
        check_clock(data_root)
        bars = load_series(data_root, args.symbol, args.timeframe.upper(),
                           args.end, ops_ack=args.ops_ack)
        # T20：禁入窗唯一来源 config/sessions.toml（与实盘风控共用）
        sess_policy = load_session_policy(str(SESSIONS_PATH))
        sess_sha = sessions_sha256(str(SESSIONS_PATH))
        if args.costs == "auto":
            # T28/M8：CLI 主路径缺缓存先报错（不静默重读全部 tick——那正是
            # M8 要消除的 4 GB 隐性加载）；--build-cost-model 为显式直建通道。
            # T27/T38：研究库模式（缺省）下缺缓存/新鲜度由
            # build_costs(research_store=True) 给出更准确的指路
            # （export_research_store）。
            cache_path = spread_model_cache_path(data_root)
            if not research_mode:
                if args.build_cost_model:
                    from build_cost_model import build_cost_models  # scripts 目录
                    build_cost_models(data_root, [args.symbol],
                                      spread_mode=args.spread_mode)
                elif not os.path.exists(cache_path):
                    raise ValueError(
                        f"缺 点差模型缓存 {cache_path}：先跑 "
                        f"scripts/build_cost_model.py --data-root {data_root} "
                        f"--symbols {args.symbol}"
                        f"（或本次运行加 --build-cost-model 从 tick 现建）")
            costs, cost_version = build_costs(
                data_root, args.symbol, args.spread_fraction,
                args.commission, allow_zero_swap=args.allow_zero_swap,
                spread_mode=args.spread_mode, sessions_sha=sess_sha,
                research_store=research_mode)
        else:
            costs, cost_version = ZeroCosts(), "zero-v0"
        entry_policy = entry_policy_for(costs, args.symbol, sess_policy)
        if args.cost_version:
            cost_version = args.cost_version
        if args.engine == "b":
            result = run_backtest_ticks(
                strategy_cls(), bars,
                lambda start_msc, end_msc: load_ticks(
                    data_root, args.symbol,
                    start_msc=start_msc, end_msc=end_msc),
                costs, symbol=args.symbol, params=params,
                contract_size=contract_size,
                volume_lots=lots,
                sizing=sizing,
                vol_min=sizing_info.get("vol_min") if sizing else None,
                vol_step=sizing_info.get("vol_step") if sizing else None,
                vol_max=sizing_info.get("vol_max") if sizing else None,
                initial_capital=args.initial_capital,
                timeframe=args.timeframe.upper(),
                latency_ms=args.latency_ms,
                slippage_price=args.slippage_price,
                session_policy=entry_policy,
            )
        else:
            result = run_backtest(
                strategy_cls(), bars, costs, symbol=args.symbol,
                params=params, contract_size=contract_size,
                volume_lots=lots, sizing=sizing,
                vol_min=sizing_info.get("vol_min") if sizing else None,
                vol_step=sizing_info.get("vol_step") if sizing else None,
                vol_max=sizing_info.get("vol_max") if sizing else None,
                initial_capital=args.initial_capital,
                timeframe=args.timeframe.upper(),
                session_policy=entry_policy,
            )
        summary = summarize(result, cost_version=cost_version)
        run_dir = make_run_dir(args.out, args.symbol, args.timeframe.upper())
        report_path = write_report(
            run_dir, result, cost_version=cost_version, strategy=args.strategy,
            strategy_sha=strategy_sha,
            extra={"end": args.end, "costs": args.costs,
                   "spread_fraction": args.spread_fraction,
                   "spread_mode": args.spread_mode,
                   "commission_per_lot": args.commission,
                   "allow_zero_swap": args.allow_zero_swap,
                   "engine": args.engine, "latency_ms": args.latency_ms,
                   "slippage_price": args.slippage_price,
                   "sizing": sizing_info,      # T23：定手数方式进 manifest
                   "sessions_path": str(SESSIONS_PATH),
                   "sessions_sha256": sess_sha,
                   "research_store": research_mode,   # T27/T38：按数据根判定
                   "main_store": store_kind == "main",  # T38/D24
                   "ops_ack": bool(args.ops_ack),       # T38/D24：运维豁免
                   "data_root": str(data_root)})
    except Exception as e:  # fail-fast：任何错误如实报出
        print(f"✗ {type(e).__name__}: {e}")
        return 1

    lots_note = (f"风险{sizing_info['risk_fraction']:.4%}"
                 if sizing_info["mode"] == "risk"
                 else f"固定{sizing_info['volume_lots']:g}手")
    print(f"✓ {args.symbol} {args.timeframe.upper()} "
          f"bars={len(result.bars)} trades={summary['trade_count']} "
          f"合约大小={contract_size:g} 手数={lots_note} "
          f"净利={summary['net_profit']:.2f} "
          f"回撤={summary['max_drawdown']:.2%}")
    if summary.get("skipped_zero_lots"):
        print(f"⚠ {summary['skipped_zero_lots']} 次入场因手数低于 vol_min 跳过"
              f"（skipped_zero_lots，详见 summary.json）")
    print(f"报告：{report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
