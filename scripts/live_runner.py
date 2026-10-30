"""实盘信号运行器 CLI（T14a + T24 实盘接线；风控/对账进程见 T14b）。

用法：
    # 干跑（离线合成数据）：不连 MT5、不下单，读 parquet + 写心跳
    .venv/Scripts/python.exe scripts/live_runner.py --config live.toml \
        --once --dry-run --offline

    # 干跑（真实终端）：连网关只读行情（TerminalBarsSource 取 bar），不下单
    .venv/Scripts/python.exe scripts/live_runner.py --config live.toml \
        --once --dry-run

    # 影子盘（T34/M3，决策 D26）：真实行情 + 影子券商，全链路运行
    #（闸门、定手数、意图、对账、风控、全平协议照常；成交由 PaperBroker
    #  本地模拟，绝不连真实下单通道；状态按策略落 data/live/paper/
    #  state-<magic>.json——多运行器并行各写各的（R6/C3），风控/对账
    #  进程用 --paper-state 指 paper 目录每轮聚合读）
    .venv/Scripts/python.exe scripts/live_runner.py --config live.toml \
        --once --paper
    #   影子 + 离线合成数据（冒烟：K 线读 parquet、报价由最新 bar 合成）
    .venv/Scripts/python.exe scripts/live_runner.py --config live.toml \
        --once --paper --dry-run --offline

    # 实跑（模拟盘）：bar 收盘后自动差分下单（先过 RiskGate 风控闸门）
    .venv/Scripts/python.exe scripts/live_runner.py --config live.toml --loop

约定（T24）：
- 数据来源（C2）：实盘与真终端干跑用 TerminalBarsSource（进程内直接向终端
  取最近 N 根已收盘 bar；休市=报价陈旧 → 静默跳过本轮并记日志；有行情但
  等不到预期 bar 在 max_wait_s 内重试，仍无才报"数据滞后"）。parquet 只用于
  `--dry-run --offline`（影子离线冒烟同样读 parquet，报价由 BarQuoteMarket
  按最新 bar 收盘价合成——T34）。
- 执行（C1）：LiveExecutor 每轮先查 commands/flatten.json（全平+确认），
  开仓/加仓腿先过 RiskGate.check_open（拒绝 → 不下单，原因写心跳
  last_error 与 alerts.log），平仓不受限；每次执行后写
  expected-<magic>.json（对账进程读全部）。
- 单实例（H-05）：实跑/影子盘启动即取 {data_root}/live/runner-<magic>.lock
  进程锁（内核字节锁，崩溃自动释放）——同 magic 第二份运行器拿不到锁
  拒绝启动（rc=3，不连网关）；dry-run 只读不持锁。
- 手数（M2）：volume_lots 缺省时按 risk_fraction 定手数（LiveSizer，
  mt5quant.sizing.lots_for_risk——与回测同一函数）；风控只设上限。
- 启动校验（M1）：策略用到 time_stop_bars / week_close_exit 直接拒绝启动
  （实盘未实现，dry-run 同样校验）。
- 心跳：每策略一个 {data_root}/live/heartbeat-<magic>.json，每轮更新
  （含无决策轮次），风控进程按 ts 判活；决策异常记 last_error 后继续轮询，
  执行异常原样上抛并停机退出（等人工/风控处置）；**心跳写入失败不退出**
  （T33/M1：记日志下一轮重试，连续 3 次写告警——跨进程文件写入一律经
  mt5live.fileio 的原子写/追加）。
- 点差闸门（T33/L3）：实盘（非 dry-run、非 paper）找不到 --spread-model
  文件 → 拒绝启动（不再静默关闸）；确要关闭须显式 --no-spread-gate（启动
  日志标红）。影子模式缺点差模型只标红警告（模拟成交无真实风险，全链路
  演练建议重建模型）。
- 重连标记（R10/T58 契约 C-4）：网关调用失败后的下一次成功调用记一行
  ``gateway_reconnected prev_failure=<kind>``（kind=失败类型）；幂等不设
  标记——验收由 deals/orders 与决策日志推导。离线模式无网关、不记。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5data.store import load_bars  # noqa: E402
from mt5live.config import LiveConfig, load as load_config  # noqa: E402
from mt5live.executor import ExecutionAdapter, LiveExecutor, \
    validate_live_strategy  # noqa: E402
from mt5live.gate import LiveSizer, RiskGate, load_contract_sizes  # noqa: E402
from mt5live.paper import PAPER_STATE_DIR_RELPATH, PAPER_STATE_RELPATH, \
    BarQuoteMarket, PaperAccountView, PaperBroker, paper_state_path  # noqa: E402（T34 影子券商）
from mt5live.riskproc import RiskConfigError, RiskProcess, load_risk_config  # noqa: E402
from mt5live.runner import (  # noqa: E402
    HeartbeatWriter,
    MagicInstanceLock,
    MarketClosedError,
    SignalRunner,
    TerminalBarsSource,
    default_compute_window_bars,
    default_heartbeat_path,
    terminal_bars_loader,
)
from mt5quant.costs.spread import (  # noqa: E402
    BUCKET_TZ,
    SpreadModel,
    SpreadStat,
)
from mt5quant.sizing import RiskSizing  # noqa: E402


def load_strategy(path: str):
    """按 "module:Class" 装载策略类（与 scripts/run_backtest.py 同一格式）。"""
    try:
        module_name, class_name = path.split(":", 1)
        mod = importlib.import_module(module_name)
        return getattr(mod, class_name)
    except (ValueError, ImportError, AttributeError) as e:
        raise ValueError(f"无法装载策略 {path!r}：{e}") from e


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def print_red(message: str) -> None:
    """启动日志标红（ANSI；不支持颜色的终端只是看不到红色，内容不变）。"""
    print(f"\033[31m{message}\033[0m", flush=True)


def find_gateway_config(override: str | None) -> str:
    if override:
        return override
    for name in ("config.json", "gateway.json"):
        if Path(name).exists():
            return name
    raise SystemExit("找不到网关配置（config.json / gateway.json），"
                     "请用 --gateway-config 指定")


# ---- R6/I5：点差模型加载（与 run_backtest.load_spread_model_cache 同口径） ----
# build_cost_model.py 的产物是缓存包装 {version, mode, watermarks, model,
# model_sha256, built_at_utc, [overshoot, overshoot_sha256]}，而
# SpreadModel.load 只认裸模型——直读包装会抛 bucket_tz=None（第六轮审核
# I5）。这里做等价加载：校验包装 version 与内容 sha 后取内层模型重建；
# 旧裸模型文件（SpreadModel.save 直写）保持兼容。

_SPREAD_REBUILD_HINT = ("重跑 scripts/build_cost_model.py 重建"
                        " data/costs/spread_model.json")


def _canonical_json_sha256(payload: dict) -> str:
    """模型负载的规范 sha256（与 run_backtest.canonical_json_sha256 同一
    算法：sort_keys + 紧致分隔符——等价 dict 同哈希，校验才有意义）。"""
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")).hexdigest()


def load_spread_model_for_live(path: str, symbol: str) -> SpreadModel:
    """加载点差模型：兼容 build_cost_model.py 的缓存包装（v1/v2）与旧
    裸模型文件（R6/I5）。

    - 缓存包装（有 model 键）：校验 version ∈ {1, 2}、model_sha256 与
      内层负载一致、bucket_tz 口径，然后从内层负载重建 SpreadModel；
      品种必须已在模型中（fail-fast，与 run_backtest 同口径）；
    - 裸模型（有 per_symbol 键）：SpreadModel.load（旧文件兼容）。

    任何校验不过 → ValueError，报错指路 build_cost_model.py（绝不静默）。
    """
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError) as e:
        raise ValueError(
            f"点差文件 {path} 不可读（{e}）——{_SPREAD_REBUILD_HINT}") from e
    if not isinstance(doc, dict):
        raise ValueError(f"点差文件 {path} 顶层须为对象"
                         f"——{_SPREAD_REBUILD_HINT}")
    if "model" in doc:
        version = doc.get("version")
        if version not in (1, 2):
            raise ValueError(
                f"点差缓存 {path} version={version!r} 不受支持（当前 1/2）"
                f"——{_SPREAD_REBUILD_HINT}")
        try:
            payload = doc["model"]
            sha = doc["model_sha256"]
        except KeyError as e:
            raise ValueError(
                f"点差缓存 {path} 缺键 {e}——{_SPREAD_REBUILD_HINT}") from e
        if _canonical_json_sha256(payload) != sha:
            raise ValueError(
                f"点差缓存 {path} 的 model_sha256 与模型内容不一致"
                f"（文件被改动或写坏）——{_SPREAD_REBUILD_HINT}")
        if payload.get("bucket_tz") != BUCKET_TZ:
            raise ValueError(
                f"点差缓存 bucket_tz={payload.get('bucket_tz')!r}"
                f"（缺标注=UTC 分桶的旧文件），与纽约本地小时口径不兼容"
                f"——{_SPREAD_REBUILD_HINT}")
        try:
            per = {s: {int(h): SpreadStat(**st) for h, st in hours.items()}
                   for s, hours in payload["per_symbol"].items()}
            overall = {s: float(v)
                       for s, v in payload["overall_median"].items()}
            model = SpreadModel(per, overall,
                                mode=payload.get("mode", "absolute"),
                                sampling=payload.get("sampling", "tick"))
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(
                f"点差缓存 {path} 模型负载非法（{e}）"
                f"——{_SPREAD_REBUILD_HINT}") from e
        if symbol not in per:
            raise ValueError(
                f"{symbol} 不在点差缓存中（缓存品种：{sorted(per)}）"
                f"——{_SPREAD_REBUILD_HINT}")
        return model
    if "per_symbol" in doc:
        return SpreadModel.load(path)   # 旧裸模型文件（SpreadModel.save 直写）
    raise ValueError(
        f"点差文件 {path} 既不是缓存包装（缺 model 键）也不是裸模型"
        f"（缺 per_symbol 键）——{_SPREAD_REBUILD_HINT}")


# ---- R6/C3：每策略影子状态文件 ----

def paper_state_file(live_dir: str, magic: int) -> str:
    """本策略的影子状态文件路径（paper/state-<magic>.json）。

    检测到旧版单文件 paper/state.json（多运行器互相覆盖的失效布局）时
    标红告警——旧文件不再读写，提示人工核对后归档。
    """
    state_file = paper_state_path(live_dir, magic)
    legacy = os.path.join(live_dir, PAPER_STATE_RELPATH)
    if os.path.exists(legacy):
        print_red(
            f"⚠ 检测到旧版影子状态 {legacy}（单文件多写者布局已停用，"
            "不再读写）——本策略状态改落 "
            f"{state_file}；旧文件请人工核对后归档/删除")
    return state_file


# ---- R10/T58（契约 C-4）：网关重连标记 ----

class GatewayReconnectWatch:
    """网关调用失败后的下一次成功调用记一行
    ``gateway_reconnected prev_failure=<kind>``（kind=失败类型，异常类名）。

    - 判定只在 live_runner.py 内：包在 ``gw.market`` 外层（quote/bars 是
      运行器每轮必经的网关调用），不改 src/mt5live/runner.py 与 mt5gw；
    - 只在"失败 → 成功"的跳变时打标（每次故障段恰一行）；幂等不设标记
      ——验收（demo_check）由 deals/orders 与决策日志推导（同 bar 同
      magic 无重复开仓请求）；
    - 离线模式（--offline）无网关，不装配本表。
    """

    def __init__(self):
        self.prev_failure: str | None = None

    def note_failure(self, exc: BaseException) -> None:
        self.prev_failure = type(exc).__name__

    def note_success(self) -> None:
        if self.prev_failure is None:
            return
        kind, self.prev_failure = self.prev_failure, None
        print(f"[{now_utc().isoformat()}] gateway_reconnected "
              f"prev_failure={kind}", flush=True)

    def wrap_market(self, market):
        """返回包了重连记账的行情服务替身（quote/bars 逐调用记账，
        其余属性透传原对象）。"""
        watch = self

        class _TrackedMarket:
            def quote(self, symbol):
                try:
                    q = market.quote(symbol)
                except Exception as exc:
                    watch.note_failure(exc)
                    raise
                watch.note_success()
                return q

            def bars(self, symbol, timeframe, count, *, end_pos=0):
                try:
                    rows = market.bars(symbol, timeframe, count,
                                       end_pos=end_pos)
                except Exception as exc:
                    watch.note_failure(exc)
                    raise
                watch.note_success()
                return rows

            def __getattr__(self, name):
                return getattr(market, name)

        return _TrackedMarket()


def build_executor_stack(trading, account, market, *, cfg: LiveConfig,
                         strategy, args, live_dir: str, specs_path: str,
                         gw, gate_account=None) -> LiveExecutor | None:
    """执行器 + 风控闸门 + 定手数装配（实跑与影子盘共用同一套接线，
    C1/M2/M10 全套；T34 起 trading/account 可为 PaperBroker）。

    gate_account：闸门的账户视图（缺省 = account）。R7/N-C2：--paper 装配
    传 paper 目录的 PaperAccountView 聚合视图——闸门看**账户整体**（跨
    magic 的持仓/成交/日亏，单次 snapshot 一致快照），执行器/定手数仍用
    本策略账户（每策略只执行自己的决策）。

    返回 None = 配置不可恢复（已打印并关闭网关），调用方以退出码 2 结束。
    """
    try:
        risk_cfg = load_risk_config(args.risk_config)
    except RiskConfigError as e:
        print(f"× 风控配置不合法：{e}")
        if gw is not None:
            gw.shutdown()
        return None
    from mt5live.riskproc import load_default_session_policy
    session_policy = load_default_session_policy(args.sessions_config)
    spread_model = None
    if (not args.no_spread_gate and args.spread_model
            and os.path.exists(args.spread_model)):
        try:
            spread_model = load_spread_model_for_live(args.spread_model,
                                                      cfg.symbol)
        except (OSError, ValueError) as e:
            print(f"× 点差模型加载失败：{e}")
            if gw is not None:
                gw.shutdown()
            return None
    process = RiskProcess(risk_cfg, live_dir=live_dir,
                          session_policy=session_policy)
    gate = RiskGate(process, account=(gate_account if gate_account is not None
                                      else account), market=market,
                    specs_path=specs_path, live_dir=live_dir,
                    spread_model=spread_model,
                    session_policy=session_policy)
    sizer = (None if cfg.volume_lots is not None else
             LiveSizer(RiskSizing(cfg.risk_fraction, basis="equity"),
                       account=account, specs_path=specs_path))
    adapter = ExecutionAdapter(
        trading, account, market,
        symbol=cfg.symbol, magic=cfg.magic, deviation=cfg.deviation)
    return LiveExecutor(
        adapter, symbol=cfg.symbol, magic=cfg.magic, live_dir=live_dir,
        rules=strategy.exit_rules, gate=gate, sizer=sizer)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="mt5live 信号运行器")
    p.add_argument("--config", required=True, help="live.toml 路径")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="跑一轮即退出")
    mode.add_argument("--loop", action="store_true", help="常驻轮询")
    p.add_argument("--dry-run", action="store_true",
                   help="不下单：无 --offline 时连真实终端只读行情")
    p.add_argument("--offline", action="store_true",
                   help="干跑离线模式：不连 MT5，K 线读 parquet（仅配合"
                        " --dry-run；C2 之后 parquet 只服务于此）")
    p.add_argument("--paper", action="store_true",
                   help="影子券商模式（T34/D26）：PaperBroker 本地模拟成交"
                        "（状态按策略落 {data_root}/live/paper/"
                        "state-<magic>.json——多运行器各写各的；风控/对账"
                        "进程用 --paper-state 指 paper 目录聚合读），闸门看"
                        "paper 目录聚合视图（账户整体，R7/N-C2）、定手数/"
                        "意图/对账/风控/全平协议全链路照常；绝不连"
                        "真实下单通道。行情源：真终端；配 --offline 用合成"
                        "报价（BarQuoteMarket）")
    p.add_argument("--paper-balance", type=float, default=10_000.0,
                   help="影子账户初始余额 USD（默认 10000；已有状态文件时"
                        "以文件为准）")
    p.add_argument("--paper-spread", type=float, default=0.2,
                   help="离线影子冒烟的合成点差（bid/ask = 最新 bar 收盘价"
                        "∓spread/2；默认 0.2）")
    p.add_argument("--poll-interval", type=float, default=2.0,
                   help="轮询间隔秒（默认 2.0）")
    p.add_argument("--gateway-config", default=None,
                   help="网关配置路径（默认找 config.json / gateway.json）")
    p.add_argument("--risk-config", default="config/risk.toml",
                   help="风控配置 TOML（RiskGate/RiskProcess 用）")
    p.add_argument("--sessions-config", default=None,
                   help="config/sessions.toml 路径（缺省用仓库默认）")
    p.add_argument("--spread-model", default="data/costs/spread_model.json",
                   help="点差模型文件（点差闸门；兼容 build_cost_model.py "
                        "的缓存包装与旧裸模型文件——R6/I5；实盘缺文件拒绝"
                        "启动，除非 --no-spread-gate）")
    p.add_argument("--no-spread-gate", action="store_true",
                   help="显式关闭点差闸门（T33/L3：实盘默认缺点差模型必须"
                        "拒启动；开启本开关会打红色警告）")
    p.add_argument("--stale-quote-s", type=float, default=120.0,
                   help="报价陈旧阈值秒（超过=休市静默跳过，默认 120）")
    p.add_argument("--max-wait-s", type=float, default=10.0,
                   help="等终端出新 bar 的重试预算秒（默认 10）")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.offline and not args.dry_run:
        print("× --offline 只能配合 --dry-run 使用（实盘必须有终端行情源）")
        return 2
    cfg: LiveConfig = load_config(args.config)
    strategy = load_strategy(cfg.strategy)()
    validate_live_strategy(strategy)          # M1：未实现的出场规则拒启动

    # T33/L3：实盘（会下真实单的模式）缺点差模型文件 → 拒绝启动——点差
    # 闸门不能静默关闭；确要关闭必须显式 --no-spread-gate（启动日志标红）。
    # 影子模式（--paper）不下真实单：缺模型只标红警告。检查放在连接网关
    # 之前（fail-fast，不占终端）。
    if not args.dry_run and not args.paper:
        if args.no_spread_gate:
            print_red(f"⚠ 点差闸门已用 --no-spread-gate 显式关闭"
                      f"（--spread-model={args.spread_model!r} 不加载）——"
                      "点差失控时不会拒单，风险自负")
        elif not args.spread_model or not os.path.exists(args.spread_model):
            print_red(
                f"× 实盘找不到点差模型文件：{args.spread_model!r}——点差闸门"
                "不能静默关闭（T33/L3）。重建 data/costs/spread_model.json"
                "（scripts/build_cost_model.py）或用 --spread-model 指向有效"
                "文件；确要关闭须显式 --no-spread-gate")
            return 2
    elif (args.paper and not args.no_spread_gate
            and (not args.spread_model
                 or not os.path.exists(args.spread_model))):
        print_red(
            f"⚠ 影子模式缺点差模型文件：{args.spread_model!r}——点差闸门将"
            "关闭（模拟成交无真实风险；全链路演练建议重建 data/costs/"
            "spread_model.json 或用 --spread-model 指向有效文件）")

    heartbeat = default_heartbeat_path(cfg)
    live_dir = os.path.join(cfg.data_root, "live")
    specs_path = os.path.join(cfg.data_root, "specs", "latest.json")

    # H-05：同账户/同 magic 单实例——会下单的模式（实跑/影子盘）启动即取
    # runner-<magic>.lock 进程锁，第二份运行器拿不到锁直接拒绝（rc=3），
    # 且拒绝发生在连接网关之前；两份运行器并行会重复下单、互写状态。
    # dry-run 不下单不持锁（只读观察可以并行）。
    lock = None
    if args.paper or not args.dry_run:
        lock = MagicInstanceLock(os.path.join(
            live_dir, f"runner-{cfg.magic}.lock"))
        if not lock.acquire():
            print(f"× magic={cfg.magic} 的运行器已在运行"
                  f"（{lock.path} 被持有）——第二份运行器拒绝启动"
                  "（H-05 同 magic 单实例；先停旧实例或换 magic）")
            return 3

    gw = None
    account_lock = None
    executor = None
    paper = None
    market = None
    quote_fn = None
    if args.offline:
        if args.paper:
            # 影子离线冒烟：报价由 parquet 最新 bar 收盘价合成（T34）
            market = BarQuoteMarket(cfg.data_root, symbol=cfg.symbol,
                                    timeframe=cfg.timeframe,
                                    spread=args.paper_spread)

            def quote_fn(symbol):        # noqa: F811 — 装配后的行情源
                q = market.quote(symbol)
                return q.bid, q.ask

        runner = SignalRunner(cfg, strategy, bars_loader=load_bars,
                              clock=now_utc, quote_fn=quote_fn)
    else:
        from mt5gw import MT5Gateway  # 延迟导入：离线干跑不触终端

        gw = MT5Gateway.from_config_file(
            find_gateway_config(args.gateway_config))
        gw.connect()
        # R10/T58（C-4）：行情服务包重连记账——quote/bars 每轮必经的网关
        # 调用失败后，下一次成功调用记 gateway_reconnected 日志行
        reconnect = GatewayReconnectWatch()
        market = reconnect.wrap_market(gw.market)

        def quote_fn(symbol):            # noqa: F811 — 装配后的行情源
            q = market.quote(symbol)
            return q.bid, q.ask

        source = TerminalBarsSource(
            market, stale_quote_s=args.stale_quote_s,
            max_wait_s=args.max_wait_s, clock=now_utc)
        runner = SignalRunner(
            cfg, strategy,
            bars_loader=terminal_bars_loader(
                source, n=default_compute_window_bars(cfg.lookback_bars)),
            clock=now_utc, quote_fn=quote_fn)

    if args.paper:
        # T34 影子券商（决策 D26）：PaperBroker 替换真实 trading/account
        # （同一对象充当两个角色），market 仍是上面的行情源；其余链路——
        # 闸门、定手数、意图、对账、风控、全平协议——全部照常，只有成交
        # 是模拟的。R6/C3：状态按策略隔离，经 fileio 原子落
        # {live_dir}/paper/state-<magic>.json（多运行器各写各的、票号
        # 区间按 magic 派生不撞号），重启恢复；风控/对账进程用
        # --paper-state 指向 paper 目录每轮聚合读取。
        try:
            contract_sizes = load_contract_sizes(specs_path)
        except (OSError, ValueError) as e:
            print(f"× 合约快照不可用：{e}——影子券商的盈亏换算需要 "
                  f"{specs_path}（先跑 scripts/snapshot_specs.py）")
            if gw is not None:
                gw.shutdown()
            return 2
        paper_state = paper_state_file(live_dir, cfg.magic)  # 旧布局检测在内
        paper = PaperBroker(
            market, state_path=paper_state,
            contract_sizes=contract_sizes, balance=args.paper_balance,
            magic=cfg.magic)
        # R7/N-C2：闸门看**账户整体**——paper 目录的聚合视图（全部
        # state-<magic>.json，单次 snapshot 一致快照；读失败显式
        # DataUnavailableError → 执行器 risk_gate_error fail-closed）。
        # B 策略的闸门能看到 A 的持仓（max_positions_per_symbol 等账户级
        # 规则跨 magic 生效）；执行器/定手数仍用本策略 PaperBroker——
        # 每策略只执行自己的决策。本策略状态文件已随 PaperBroker 构造
        # 落盘，聚合视图至少含本策略（构造不会因空目录失败）。
        gate_view = PaperAccountView(
            os.path.join(live_dir, PAPER_STATE_DIR_RELPATH),
            alert_dir=live_dir)
        executor = build_executor_stack(
            paper, paper, market, cfg=cfg, strategy=strategy, args=args,
            live_dir=live_dir, specs_path=specs_path, gw=gw,
            gate_account=gate_view)
        if executor is None:
            if lock is not None:
                lock.release()     # H-05：装配失败的早退同样释锁
            return 2
    elif not args.dry_run:
        # 实跑：执行器 + 风控闸门 + 定手数（C1/M2/M10 全套接线）
        #（market 用包了 C-4 重连记账的替身——闸门点差检查/执行器参考价
        #  的 quote 调用同样计入网关健康判定）
        executor = build_executor_stack(
            gw.trading, gw.account, market, cfg=cfg, strategy=strategy,
            args=args, live_dir=live_dir, specs_path=specs_path, gw=gw)
        if executor is None:
            if lock is not None:
                lock.release()     # H-05：装配失败的早退同样释锁
            return 2

    # H2-01：除 data_root 内的本地锁外，实盘再持有跨 data_root 的
    # account(server/login)+magic 锁；在首个决策/订单前完成互斥仲裁。
    if not args.paper and not args.dry_run and gw is not None:
        try:
            account_lock = MagicInstanceLock.for_account(
                gw.account.state(), cfg.magic)
        except Exception as exc:
            if lock is not None:
                lock.release()
            gw.shutdown()
            print(f"× 无法确定账户级单实例锁身份：{exc}")
            return 2
        try:
            acquired = account_lock.acquire()
        except OSError as exc:
            if lock is not None:
                lock.release()
            gw.shutdown()
            print(f"× 创建账户级 Runner 锁失败：{exc}")
            return 2
        if not acquired:
            account_state = gw.account.state()
            if lock is not None:
                lock.release()
            gw.shutdown()
            print(f"× MT5 account={account_state.server}/"
                  f"{account_state.login} magic={cfg.magic} 已有运行器"
                  "持锁——跨 data_root 第二份运行器拒绝启动（rc=3）")
            return 3

    sizing_desc = (f"固定 {cfg.volume_lots} 手" if cfg.volume_lots is not None
                   else f"按风险 {cfg.risk_fraction}")
    if args.paper:
        mode_desc = "[paper offline]" if args.offline else "[paper]"
    else:
        mode_desc = ("[dry-run offline]" if args.offline
                     else "[dry-run]" if args.dry_run else "[LIVE]")
    print(f"mt5live 启动：{cfg.symbol} {cfg.timeframe} magic={cfg.magic} "
          f"strategy={cfg.strategy} lookback={cfg.lookback_bars} "
          f"手数={sizing_desc} {mode_desc}")
    if paper is not None:
        st = paper.state()
        print(f"    影子账户：state={paper_state_path(live_dir, cfg.magic)}"
              f" balance={st.balance:.2f} equity={st.equity:.2f}"
              f" 持仓 {len(paper.positions())} 张"
              "（风控/对账进程：--paper-state 指向 paper 目录聚合读取）",
              flush=True)
    # T33/M1：心跳写入失败不退出（记日志下一轮重试，连续 3 次写告警）——
    # PermissionError 不再杀进程、随后心跳超时全局停机
    hb_writer = HeartbeatWriter(heartbeat, config=cfg, alert_dir=live_dir)
    try:
        skips_logged = runner.market_closed_skips
        while True:
            now = now_utc()
            decision = None
            error = None
            if paper is not None:
                # T34：每轮先按实时 bid/ask 检查 SL/TP（触发即平、成交带
                # reason），再进执行器轮——保护性出场同轮就被识别为
                # external_exit（意图=实际净持仓，对账不暂停）
                protective = paper.poll_protective(now)
                if protective:
                    print(f"[{now.isoformat()}] [paper] 保护性出场 "
                          f"{len(protective)} 笔：" + "；".join(
                              f"#{d.position_id} reason={d.reason} "
                              f"price={d.price} pnl={d.profit:+.2f}"
                              for d in protective), flush=True)
            try:
                decision = runner.decide()
            except MarketClosedError:
                pass   # 理论上 runner 已捕获；双保险不刷错误
            except Exception as exc:   # 决策侧异常：记录并继续（数据滞后等可自愈）
                error = f"{type(exc).__name__}: {exc}"
                print(f"[{now.isoformat()}] 决策异常：{error}", flush=True)
            if runner.market_closed_skips != skips_logged:
                skips_logged = runner.market_closed_skips
                print(f"[{now.isoformat()}] 休市跳过 {skips_logged}："
                      f"{getattr(runner, '_last_market_closed', '')}",
                      flush=True)
            if decision is not None:
                print(f"[{decision.decided_at.isoformat()}] 决策 "
                      f"{decision.decision_id}：signal={decision.signal} "
                      f"target={decision.target_position} "
                      f"volume={decision.volume_lots} "
                      f"bid={decision.quote_bid} ask={decision.quote_ask} "
                      f"atr={decision.atr} sl={decision.sl} tp={decision.tp}",
                      flush=True)
            if executor is not None:
                try:
                    rr = executor.poll(decision, now)
                except Exception as exc:
                    # 执行侧异常：原样上抛前先把状态写进心跳（风控要能看到）；
                    # 心跳本身写失败也不再掩盖原始异常（T33/M1）
                    hb_writer.write(decision=decision,
                                    error=f"执行异常 {type(exc).__name__}: "
                                          f"{exc}",
                                    dry_run=args.dry_run)
                    raise
                print(f"    执行 {rr.action}：net {rr.net_before} → "
                      f"{rr.net_after}，{len(rr.orders)} 笔请求"
                      + (f"（{rr.error}）" if rr.error else ""), flush=True)
                for o in rr.orders:
                    print(f"      {o.kind} ticket={o.ticket} "
                          f"vol={o.filled_volume}/{o.requested_volume} "
                          f"price={o.price}"
                          f"{' [部分成交]' if o.partial else ''}", flush=True)
                if rr.error:
                    error = rr.error
            elif decision is not None:
                print("    [dry-run] 不下单", flush=True)
            hb_writer.write(decision=decision, error=error,
                            dry_run=args.dry_run)
            if args.once:
                return 0
            time.sleep(max(args.poll_interval, 0.1))
    except KeyboardInterrupt:
        print("收到中断，退出", flush=True)
        return 0
    finally:
        if account_lock is not None:
            account_lock.release()    # H2-01：释放跨 data_root 账户级锁
        if lock is not None:
            lock.release()             # H-05：释放本地 paper/data_root 进程锁
        if gw is not None:
            gw.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
