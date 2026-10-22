"""端到端冒烟验收：品种目录 → 行情 → 账户 → 模拟盘真实开/平仓 → 成交回查。

用法：.venv/Scripts/python.exe scripts/smoke.py [--config config.json]
任一步失败 exit 1；④ 下单失败则跳过 ⑤⑥ 并注明。
"""
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5gw import MT5Gateway  # noqa: E402
from mt5gw.errors import DataUnavailableError, GatewayError  # noqa: E402

FAILED = []


def step(ok, label, detail=""):
    mark = "✓" if ok else "✗"
    print(f"  {mark} {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILED.append(label)


def pick_symbol(gw, wanted="EURUSD"):
    try:
        return wanted, gw.symbols.get(wanted)
    except DataUnavailableError:
        cands = [s for s in gw.symbols.all() if s.spread > 0 and s.volume_min <= 0.1]
        if not cands:
            raise DataUnavailableError("目录里没有可用候选品种")
        spec = cands[0]
        return spec.name, spec


def main() -> int:
    config_path = "config.json"
    if "--config" in sys.argv:
        config_path = sys.argv[sys.argv.index("--config") + 1]

    with MT5Gateway.from_config_file(config_path) as gw:
        # ① 品种目录
        print("① 品种目录")
        try:
            specs = gw.symbols.all()
            sample = ", ".join(random.sample([s.name for s in specs], k=min(5, len(specs))))
            step(len(specs) > 0, f"共 {len(specs)} 个品种", f"样例：{sample}")
        except GatewayError as e:
            step(False, "品种目录", str(e))

        # ② 行情
        print("② 行情")
        symbol = quote = bars = ticks = None
        try:
            symbol, spec = pick_symbol(gw)
            quote = gw.market.quote(symbol)
            step(quote.bid > 0 and quote.ask >= quote.bid, f"{symbol} 报价",
                 f"bid={quote.bid} ask={quote.ask} time={quote.time}")
            bars = gw.market.bars(symbol, "M1", 100)
            last = bars[-1]
            step(len(bars) > 0, f"M1 K线 × {len(bars)}",
                 f"最新：{last.time} close={last.close}")
            ticks = gw.market.ticks(symbol, 50)
            step(len(ticks) > 0, f"tick × {len(ticks)}",
                 f"最新：{ticks[-1].time} bid={ticks[-1].bid}")
        except GatewayError as e:
            step(False, "行情", str(e))

        # ③ 账户
        print("③ 账户")
        st = None
        try:
            st = gw.account.state()
            mode = {0: "模拟", 1: "竞赛", 2: "实盘"}.get(st.trade_mode, st.trade_mode)
            step(True, f"账户 {st.login}（{mode}）",
                 f"余额={st.balance} {st.currency} 净值={st.equity} 杠杆=1:{st.leverage} "
                 f"服务器={st.server}")
        except GatewayError as e:
            step(False, "账户", str(e))

        # 冒烟自己的硬闸：无论保险丝解没解锁，测试单只允许模拟盘（设计文档反目标）
        if st is not None and st.trade_mode != 0:
            step(False, "冒烟拒绝在非模拟盘账户下单",
                 f"trade_mode={st.trade_mode}（0=模拟）。测试单永远不下在实盘。")
            return 1

        # ④ 真实下单（模拟盘）
        print("④ 下单（最小手数市价买入）")
        res = None
        try:
            volume = spec.volume_min
            res = gw.trading.market_order(symbol, "buy", volume, comment="smoke")
            fill = res.deal_price
            expected = quote.ask
            slip_points = round(abs(fill - expected) / spec.point, 1)
            step(res.ok, f"retcode={res.retcode} 成交价={fill}",
                 f"下单时 ask={expected} 滑点≈{slip_points}点 "
                 f"order={res.order_ticket} deal={res.deal_ticket}")
        except GatewayError as e:
            step(False, "下单", str(e) + "（保险丝生效或被拒时如实失败，不伪造成功）")

        if res is None:
            print("⑤⑥ 跳过 — ④ 未成交")
            return 1

        # ⑤ 平仓
        print("⑤ 平仓")
        closed = None
        try:
            positions = gw.account.positions(symbol)
            pos = next((p for p in positions if p.ticket == res.order_ticket), None)
            if pos is None:  # 保险路径：按 magic 找最新持仓
                ours = [p for p in positions if p.magic == gw.config.magic]
                pos = max(ours, key=lambda p: p.time) if ours else None
            if pos is None:
                step(False, "找不到刚开的持仓", f"期望 ticket={res.order_ticket}")
            else:
                closed = gw.trading.close_position(pos.ticket)
                step(closed.ok, f"retcode={closed.retcode} 成交价={closed.deal_price}",
                     f"持仓 {pos.ticket} 已平（开仓价 {pos.price_open}，浮盈 {pos.profit}）")
        except GatewayError as e:
            step(False, "平仓", str(e))

        # ⑥ 成交回查
        print("⑥ 成交回查")
        try:
            window = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=12)
            deals = gw.account.deals(window, datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=12))
            mine = [d for d in deals if d.ticket == res.deal_ticket]
            d = mine[0] if mine else None
            if d is None:
                step(False, f"历史里找不到 deal {res.deal_ticket}",
                     f"窗口内共 {len(deals)} 笔成交")
            else:
                total_fee = d.commission + d.swap + d.fee
                step(True, f"deal {d.ticket}: {d.side} {d.volume} {d.symbol} @ {d.price}",
                     f"手续费={d.commission} 库存费={d.swap} 其他费={d.fee} "
                     f"合计={round(total_fee, 4)}")
        except GatewayError as e:
            step(False, "成交回查", str(e))

    if FAILED:
        print(f"\n冒烟失败：{FAILED}")
        return 1
    print("\n冒烟全链路通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
