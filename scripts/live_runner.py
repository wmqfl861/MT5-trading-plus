"""实盘信号运行器 CLI（T14a；风控/对账进程见 T14b）。

示例：
    # 干跑：不连 MT5、不下单，只打印决策 + 写心跳（验证配置与数据链路）
    .venv/Scripts/python.exe scripts/live_runner.py --config live.toml \
        --once --dry-run

    # 实跑（模拟盘）：bar 收盘后自动差分下单
    .venv/Scripts/python.exe scripts/live_runner.py --config live.toml --loop

约定：
- 数据：只读 {data_root} 下的 K 线 parquet（定时刷新进程是唯一写者，
  原子写 → 读写无冲突，R14 的"单写者"天然成立）；锁箱豁免见 mt5live.runner。
- dry-run：不初始化 MT5、不调执行器，只产出决策与心跳。
- 心跳每轮更新（含无决策的轮次），风控进程按 updated_at_utc 判活。
- 决策异常（数据滞后/窗口不足等）记入心跳 last_error 后继续轮询，
  绝不吞错后盲目下单；执行异常原样上抛并停机退出（等人工/风控处置）。
"""
from __future__ import annotations

import argparse
import importlib
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5data.store import load_bars  # noqa: E402
from mt5live.config import LiveConfig, load as load_config  # noqa: E402
from mt5live.executor import ExecutionAdapter  # noqa: E402
from mt5live.runner import (SignalRunner, default_heartbeat_path,  # noqa: E402
                            write_heartbeat)


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
                   help="不连 MT5、不下单，只打印决策与心跳")
    p.add_argument("--poll-interval", type=float, default=2.0,
                   help="轮询间隔秒（默认 2.0）")
    p.add_argument("--gateway-config", default=None,
                   help="网关配置路径（默认找 config.json / gateway.json）")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cfg: LiveConfig = load_config(args.config)
    strategy = load_strategy(cfg.strategy)()
    heartbeat = default_heartbeat_path(cfg)

    gw = None
    executor = None
    quote_fn = None
    if not args.dry_run:
        from mt5gw import MT5Gateway  # 延迟导入：dry-run 不触终端

        gw = MT5Gateway.from_config_file(find_gateway_config(args.gateway_config))
        gw.connect()

        def quote_fn(symbol):            # noqa: F811 — 装配后的行情源
            q = gw.market.quote(symbol)
            return q.bid, q.ask

        executor = ExecutionAdapter(
            gw.trading, gw.account, gw.market,
            symbol=cfg.symbol, magic=cfg.magic, deviation=cfg.deviation)
    # dry-run 无行情源：决策的报价与建议 SL/TP 为 None（runner 约定）
    runner = SignalRunner(cfg, strategy, bars_loader=load_bars, clock=now_utc,
                          quote_fn=quote_fn)

    print(f"mt5live 启动：{cfg.symbol} {cfg.timeframe} magic={cfg.magic} "
          f"strategy={cfg.strategy} lookback={cfg.lookback_bars} "
          f"{'[dry-run]' if args.dry_run else '[LIVE]'}")
    try:
        while True:
            decision = None
            error = None
            try:
                decision = runner.decide()
            except Exception as exc:   # 决策侧异常：记录并继续（数据滞后等可自愈）
                error = f"{type(exc).__name__}: {exc}"
                print(f"[{now_utc().isoformat()}] 决策异常：{error}", flush=True)
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
                        report = executor.execute(decision)
                    except Exception as exc:
                        # 执行侧异常：原样上抛前先把状态写进心跳（风控要能看到）
                        write_heartbeat(heartbeat, config=cfg, decision=decision,
                                        error=f"执行异常 {type(exc).__name__}: {exc}",
                                        dry_run=args.dry_run)
                        raise
                    print(f"    执行 {report.action}：net {report.net_before} → "
                          f"{report.net_after}，{len(report.orders)} 笔请求",
                          flush=True)
                    for o in report.orders:
                        print(f"      {o.kind} ticket={o.ticket} vol={o.filled_volume}"
                              f"/{o.requested_volume} price={o.price}"
                              f"{' [部分成交]' if o.partial else ''}", flush=True)
                    error = None
                else:
                    print("    [dry-run] 不下单", flush=True)
            write_heartbeat(heartbeat, config=cfg, decision=decision,
                            error=error, dry_run=args.dry_run)
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
