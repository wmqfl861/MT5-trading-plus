"""端到端冒烟验收：品种目录 → 行情 → 账户 → 模拟盘真实下单链路。

用法：.venv/Scripts/python.exe scripts/smoke.py [--config config.json]
铁律（无论配置文件写了什么）：
- 测试单永远只允许模拟盘：保险丝被强制关闭（allow_live_trading=False），
  账户状态读不到（fail-closed）或非模拟盘时拒绝进入任何下单步骤；
- 下单前二次核身（账户号与类型必须与③一致）；
- 开仓成功后无论后续步骤成败，finally 里保证尝试平仓。
"""
import os
import random
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5gw import MT5Gateway, GatewayConfig, load  # noqa: E402
from mt5gw.errors import DataUnavailableError, GatewayError  # noqa: E402

FAILED = []


def step(ok, label, detail=""):
    mark = "✓" if ok else "✗"
    print(f"  {mark} {label}" + (f" — {detail}" if detail else ""), flush=True)
    if not ok:
        FAILED.append(label)


def utcnow_naive():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def main() -> int:
    config_path = "config.json"
    if "--config" in sys.argv:
        config_path = sys.argv[sys.argv.index("--config") + 1]

    # C2：配置可读则读，读不到用默认；无论来源，保险丝强制灭活
    if os.path.exists(config_path):
        cfg = replace(load(config_path), allow_live_trading=False)
    else:
        cfg = replace(GatewayConfig(), allow_live_trading=False)

    with MT5Gateway(cfg) as gw:
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
        symbol = spec = quote = bars = None
        try:
            symbol = "EURUSD"
            try:
                spec = gw.symbols.get(symbol)
            except DataUnavailableError:
                cands = [s for s in gw.symbols.all() if s.spread > 0 and s.volume_min <= 0.1]
                spec = cands[0]
                symbol = spec.name
            quote = gw.market.quote(symbol)
            step(quote.bid > 0 and quote.ask >= quote.bid, f"{symbol} 报价",
                 f"bid={quote.bid} ask={quote.ask} time={quote.time}")
            bars = gw.market.bars(symbol, "M1", 100)
            step(len(bars) > 0, f"M1 K线 × {len(bars)}",
                 f"最新：{bars[-1].time} close={bars[-1].close}" if bars else "空")
            ticks = gw.market.ticks(symbol, 50)
            step(len(ticks) > 0, f"tick × {len(ticks)}",
                 f"最新：{ticks[-1].time} bid={ticks[-1].bid}" if ticks else "空")
        except GatewayError as e:
            step(False, "行情", str(e))

        # ③ 账户 + 硬闸（fail-closed）
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

        # C2：读不到账户状态或非模拟盘 → 绝不进入下单步骤
        if st is None or st.trade_mode != 0:
            step(False, "冒烟拒绝下单",
                 "账户状态不可读（fail-closed）或非模拟盘。测试单永远不下在实盘。")
            return 1
        # C2：行情缺失则无从设定滑点/手数，同样不下单
        if spec is None or quote is None:
            step(False, "冒烟跳过下单", "行情未就绪，拒绝盲目下单")
            return 1

        # ④ 下单（最小手数×2 市价买入，为部分平仓留余量）+ 二次核身
        print("④ 下单（市价买入）")
        res = None
        try:
            again = gw.account.state()  # 二次核身：账户不得在间隙被切换
            if again.login != st.login or again.trade_mode != st.trade_mode:
                step(False, "下单前账户发生变化，中止",
                     f"{st.login}/{st.trade_mode} → {again.login}/{again.trade_mode}")
                return 1
            volume = round(spec.volume_min * 2, 8)
            res = gw.trading.market_order(symbol, "buy", volume, comment="smoke")
            if not res.confirmed:
                step(False, f"retcode={res.retcode} 成交未确认", str(res))
                res = None
            else:
                slip = round(abs(res.deal_price - quote.ask) / spec.point, 1)
                step(res.ok, f"retcode={res.retcode} 成交价={res.deal_price}",
                     f"下单时 ask={quote.ask} 滑点≈{slip}点 "
                     f"order={res.order_ticket} deal={res.deal_ticket}")
        except GatewayError as e:
            step(False, "下单", str(e))

        if res is None:
            print("⑤⑥⑦ 跳过 — ④ 未成交")
            return 1

        # 开了仓就必须尽力平掉：后续步骤全部包在 try/finally 里
        pos_ticket = None
        try:
            # ⑤ 改持仓 SL/TP（只改一侧，另一侧保持现值——真机验证 C1 修复）
            print("⑤ 改持仓（只设 TP，SL 应保持 0）")
            try:
                pos = gw.account.position(res.order_ticket)
                pos_ticket = pos.ticket
                guard = pos.volume * spec.point * 500  # 宽距离，避免 INVALID_STOPS
                far_tp = round(res.deal_price + guard, spec.digits)
                mod = gw.trading.modify_position(pos.ticket, tp=far_tp)
                pos2 = gw.account.position(pos.ticket)
                step(mod.ok and pos2.sl == 0.0 and pos2.tp == far_tp,
                     f"TP={pos2.tp} SL={pos2.sl}",
                     "只改 TP：SL 保持 0（未被清成 0 以外的假值），TP 已生效" if pos2.tp == far_tp
                     else f"期望 TP={far_tp} 实际 {pos2.tp}")
            except GatewayError as e:
                step(False, "改持仓", str(e))

            # ⑥ 部分平仓（一半）再全平
            print("⑥ 平仓（先半仓后全平）")
            try:
                pos = gw.account.position(pos_ticket or res.order_ticket)
                half = round(pos.volume / 2, 8)
                if half >= spec.volume_min and (pos.volume - half) >= spec.volume_min:
                    part = gw.trading.close_position(pos.ticket, half)
                    left = gw.account.position(pos.ticket)
                    step(part.ok and abs(left.volume - (pos.volume - half)) < 1e-9,
                         f"部分平仓 {half} 手",
                         f"余量 {left.volume} 手（原 {pos.volume}）")
                    close_target = left.ticket
                else:
                    close_target = pos.ticket  # 量太小，直接全平
                closed = gw.trading.close_position(close_target)
                step(closed.ok, f"全平 retcode={closed.retcode} 成交价={closed.deal_price}",
                     f"持仓 {close_target} 已平")
            except GatewayError as e:
                step(False, "平仓", str(e))
        finally:
            # C2：无论 ⑤⑥ 成败，兜底清掉可能残留的持仓
            try:
                remaining = gw.account.positions(symbol)
                for p in remaining:
                    if p.magic == gw.config.magic:
                        gw.trading.close_position(p.ticket)
                        print(f"  · 兜底平仓 {p.ticket}")
            except GatewayError as e:
                step(False, "兜底平仓失败（可能仍有残留持仓！）", str(e))

        # ⑦ 成交回查
        print("⑦ 成交回查")
        try:
            win_from = utcnow_naive() - timedelta(hours=12)
            win_to = utcnow_naive() + timedelta(hours=12)
            deals = gw.account.deals(win_from, win_to)
            mine = [d for d in deals if d.position_id == res.order_ticket or d.ticket == res.deal_ticket]
            if not mine:
                step(False, f"历史里找不到本单成交（order={res.order_ticket}）",
                     f"窗口内共 {len(deals)} 笔")
            else:
                fee_sum = sum(d.commission + d.swap + d.fee for d in mine)
                step(True, f"{len(mine)} 笔成交（开/平/部分平）",
                     f"手续费合计={round(fee_sum, 4)} "
                     f"entries={[d.entry for d in mine]}")
        except GatewayError as e:
            step(False, "成交回查", str(e))

    if FAILED:
        print(f"\n冒烟失败：{FAILED}")
        return 1
    print("\n冒烟全链路通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
