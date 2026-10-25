"""实盘信号运行器 CLI（T14a + T24 实盘接线；风控/对账进程见 T14b）。

用法：
    # 干跑（离线合成数据）：不连 MT5、不下单，读 parquet + 写心跳
    .venv/Scripts/python.exe scripts/live_runner.py --config live.toml \
        --once --dry-run --offline

    # 干跑（真实终端）：连网关只读行情（TerminalBarsSource 取 bar），不下单
    .venv/Scripts/python.exe scripts/live_runner.py --config live.toml \
        --once --dry-run

    # 实跑（模拟盘）：bar 收盘后自动差分下单（先过 RiskGate 风控闸门）
    .venv/Scripts/python.exe scripts/live_runner.py --config live.toml --loop

约定（T24）：
- 数据来源（C2）：实盘与真终端干跑用 TerminalBarsSource（进程内直接向终端
  取最近 N 根已收盘 bar；休市=报价陈旧 → 静默跳过本轮并记日志；有行情但
  等不到预期 bar 在 max_wait_s 内重试，仍无才报"数据滞后"）。parquet 只用于
  `--dry-run --offline`。
- 执行（C1）：LiveExecutor 每轮先查 commands/flatten.json（全平+确认），
  开仓/加仓腿先过 RiskGate.check_open（拒绝 → 不下单，原因写心跳
  last_error 与 alerts.log），平仓不受限；每次执行后写
  expected-<magic>.json（对账进程读全部）。
- 手数（M2）：volume_lots 缺省时按 risk_fraction 定手数（LiveSizer，
  mt5quant.sizing.lots_for_risk——与回测同一函数）；风控只设上限。
- 启动校验（M1）：策略用到 time_stop_bars / week_close_exit 直接拒绝启动
  （实盘未实现，dry-run 同样校验）。
- 心跳：每策略一个 {data_root}/live/heartbeat-<magic>.json，每轮更新
  （含无决策轮次），风控进程按 ts 判活；决策异常记 last_error 后继续轮询，
  执行异常原样上抛并停机退出（等人工/风控处置）；**心跳写入失败不退出**
  （T33/M1：记日志下一轮重试，连续 3 次写告警——跨进程文件写入一律经
  mt5live.fileio 的原子写/追加）。
- 点差闸门（T33/L3）：实盘（非 dry-run）找不到 --spread-model 文件 →
  拒绝启动（不再静默关闸）；确要关闭须显式 --no-spread-gate（启动日志标红）。
"""
from __future__ import annotations

import argparse
import importlib
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
from mt5live.gate import LiveSizer, RiskGate  # noqa: E402
from mt5live.riskproc import RiskConfigError, RiskProcess, load_risk_config  # noqa: E402
from mt5live.runner import (  # noqa: E402
    HeartbeatWriter,
    MarketClosedError,
    SignalRunner,
    TerminalBarsSource,
    default_compute_window_bars,
    default_heartbeat_path,
    terminal_bars_loader,
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
    p.add_argument("--poll-interval", type=float, default=2.0,
                   help="轮询间隔秒（默认 2.0）")
    p.add_argument("--gateway-config", default=None,
                   help="网关配置路径（默认找 config.json / gateway.json）")
    p.add_argument("--risk-config", default="config/risk.toml",
                   help="风控配置 TOML（RiskGate/RiskProcess 用）")
    p.add_argument("--sessions-config", default=None,
                   help="config/sessions.toml 路径（缺省用仓库默认）")
    p.add_argument("--spread-model", default="data/costs/spread_model.json",
                   help="点差模型文件（点差闸门；实盘缺文件拒绝启动，"
                        "除非 --no-spread-gate）")
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

    # T33/L3：实盘（非 dry-run）缺点差模型文件 → 拒绝启动——点差闸门不再
    # 静默关闭；确要关闭必须显式 --no-spread-gate（启动日志标红）。检查放在
    # 连接网关之前（fail-fast，不占终端）。
    if not args.dry_run:
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

    heartbeat = default_heartbeat_path(cfg)
    live_dir = os.path.join(cfg.data_root, "live")
    specs_path = os.path.join(cfg.data_root, "specs", "latest.json")

    gw = None
    executor = None
    quote_fn = None
    if args.offline:
        runner = SignalRunner(cfg, strategy, bars_loader=load_bars,
                              clock=now_utc, quote_fn=None)
    else:
        from mt5gw import MT5Gateway  # 延迟导入：离线干跑不触终端

        gw = MT5Gateway.from_config_file(
            find_gateway_config(args.gateway_config))
        gw.connect()

        def quote_fn(symbol):            # noqa: F811 — 装配后的行情源
            q = gw.market.quote(symbol)
            return q.bid, q.ask

        source = TerminalBarsSource(
            gw.market, stale_quote_s=args.stale_quote_s,
            max_wait_s=args.max_wait_s, clock=now_utc)
        runner = SignalRunner(
            cfg, strategy,
            bars_loader=terminal_bars_loader(
                source, n=default_compute_window_bars(cfg.lookback_bars)),
            clock=now_utc, quote_fn=quote_fn)

        if not args.dry_run:
            # 实跑：执行器 + 风控闸门 + 定手数（C1/M2/M10 全套接线）
            try:
                risk_cfg = load_risk_config(args.risk_config)
            except RiskConfigError as e:
                print(f"× 风控配置不合法：{e}")
                gw.shutdown()
                return 2
            from mt5live.riskproc import load_default_session_policy
            from mt5quant.costs.spread import SpreadModel
            session_policy = load_default_session_policy(args.sessions_config)
            spread_model = None
            if (not args.no_spread_gate and args.spread_model
                    and os.path.exists(args.spread_model)):
                spread_model = SpreadModel.load(args.spread_model)
            process = RiskProcess(risk_cfg, live_dir=live_dir,
                                  session_policy=session_policy)
            gate = RiskGate(process, account=gw.account, market=gw.market,
                            specs_path=specs_path, live_dir=live_dir,
                            spread_model=spread_model,
                            session_policy=session_policy)
            sizer = (None if cfg.volume_lots is not None else
                     LiveSizer(RiskSizing(cfg.risk_fraction, basis="equity"),
                               account=gw.account, specs_path=specs_path))
            adapter = ExecutionAdapter(
                gw.trading, gw.account, gw.market,
                symbol=cfg.symbol, magic=cfg.magic, deviation=cfg.deviation)
            executor = LiveExecutor(
                adapter, symbol=cfg.symbol, magic=cfg.magic, live_dir=live_dir,
                rules=strategy.exit_rules, gate=gate, sizer=sizer)

    sizing_desc = (f"固定 {cfg.volume_lots} 手" if cfg.volume_lots is not None
                   else f"按风险 {cfg.risk_fraction}")
    mode_desc = ("[dry-run offline]" if args.offline
                 else "[dry-run]" if args.dry_run else "[LIVE]")
    print(f"mt5live 启动：{cfg.symbol} {cfg.timeframe} magic={cfg.magic} "
          f"strategy={cfg.strategy} lookback={cfg.lookback_bars} "
          f"手数={sizing_desc} {mode_desc}")
    # T33/M1：心跳写入失败不退出（记日志下一轮重试，连续 3 次写告警）——
    # PermissionError 不再杀进程、随后心跳超时全局停机
    hb_writer = HeartbeatWriter(heartbeat, config=cfg, alert_dir=live_dir)
    try:
        skips_logged = runner.market_closed_skips
        while True:
            now = now_utc()
            decision = None
            error = None
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
        if gw is not None:
            gw.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
