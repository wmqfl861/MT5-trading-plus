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
        idx = sys.argv.index("--config")
        if idx + 1 >= len(sys.argv):
            print("✗ --config 需要一个文件路径参数")
            return 1
        config_path = sys.argv[idx + 1]

    # C2：配置可读则读；显式指定的路径不存在直接报错（不静默用默认）
    if "--config" in sys.argv and not os.path.exists(config_path):
        print(f"✗ 指定的配置文件不存在：{config_path}")
        return 1
    if os.path.exists(config_path):
        cfg = replace(load(config_path), allow_live_trading=False,
                      magic=902424)  # 冒烟专属 magic，绝不误伤策略持仓
    else:
        print("· 未找到 config.json，使用安全默认（仅模拟盘）")
        cfg = replace(GatewayConfig(), allow_live_trading=False, magic=902424)

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

        res = None
        pos_ticket = None
        _pend_ticket = None
        try:
            # ④ 下单（最小手数×2 市价买入，为部分平仓留余量）+ 二次核身
            print("④ 下单（市价买入）")
            try:
                again = gw.account.state()  # 二次核身：账户不得在间隙被切换
                if again.login != st.login or again.trade_mode != st.trade_mode:
                    step(False, "下单前账户发生变化，中止",
                         f"{st.login}/{st.trade_mode} → {again.login}/{again.trade_mode}")
                    return 1
                volume = round(spec.volume_min * 2, 8)
                # 带非零 SL 开仓：⑤ 只改 TP 后 SL 必须原样保留（C1 的真证明）
                dist = max(spec.trade_stops_level + 20, 30) * spec.point
                open_sl = round(quote.ask - dist, spec.digits)
                open_tp = round(quote.ask + dist, spec.digits)
                res = gw.trading.market_order(symbol, "buy", volume,
                                              sl=open_sl, tp=open_tp, comment="smoke")
                if not res.filled:
                    step(False, f"retcode={res.retcode} 成交未确认", str(res))
                    res = None
                else:
                    slip = (round(abs(res.deal_price - quote.ask) / spec.point, 1)
                            if res.deal_price else None)
                    step(res.ok, f"retcode={res.retcode} 成交价={res.deal_price}",
                         f"下单时 ask={quote.ask} 滑点≈{slip}点 "
                         f"order={res.order_ticket} deal={res.deal_ticket}")
            except GatewayError as e:
                step(False, "下单", str(e))

            if res is None:
                print("④b⑤⑥ 跳过 — ④ 未成交")
                return 1

            # ④b 挂单 + 撤单（真机验收 cancel_order）
            print("④b 挂单+撤单（远端限价单）")
            try:
                far = round(quote.ask * 0.99, spec.digits)  # 距现价 1%，远超 stops level
                pend = gw.trading.pending_order(symbol, "buy", "limit",
                                                spec.volume_min, far, comment="smoke-pend")
                _pend_ticket = pend.order_ticket if pend.ok else None
                if not pend.ok:
                    step(False, f"挂单失败 retcode={pend.retcode}", pend.comment)
                else:
                    cancelled = gw.trading.cancel_order(pend.order_ticket)
                    gone = all(o.ticket != pend.order_ticket
                               for o in gw.account.pending_orders(symbol))
                    step(cancelled.ok and gone,
                         f"挂单 {pend.order_ticket} 已撤（retcode={cancelled.retcode}）",
                         "挂单列表中已不存在" if gone else "警告：挂单列表仍可见！")
            except GatewayError as e:
                step(False, "挂单/撤单", str(e))

            # ⑤ 改持仓 SL/TP（只改一侧，另一侧保持现值——C1 的真证明：
            #    开仓带非零 SL，只改 TP，断言 SL 仍是 open_sl 而非 0）
            print("⑤ 改持仓（只设 TP，非零 SL 必须原样保留）")
            try:
                pos = gw.account.position(res.order_ticket)
                pos_ticket = pos.ticket
                new_tp = round(pos.price_current + dist, spec.digits)
                mod = gw.trading.modify_position(pos.ticket, tp=new_tp)
                pos2 = gw.account.position(pos.ticket)
                ok5 = (mod.ok and pos2.sl == open_sl and pos2.tp == new_tp)
                step(ok5, f"TP={pos2.tp} SL={pos2.sl}",
                     f"只改 TP：SL 保持 {open_sl}（未被清 0），TP 已生效" if ok5
                     else f"期望 SL={open_sl} TP={new_tp}，实际 SL={pos2.sl} TP={pos2.tp}")
            except GatewayError as e:
                step(False, "改持仓", str(e))

            # ⑥ 平仓（一半）再全平
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
            # N3：兜底只清冒烟自己开的票（按 ticket 精确处理），
            # 任何异常（含 AttributeError 类）都不得逃出 finally
            own_positions = set()
            own_orders = set()
            if res is not None:
                own_positions.add(res.order_ticket)
            for t_ in (_pend_ticket and [_pend_ticket] or []):
                own_orders.add(t_)
            try:
                for t_ in own_positions:
                    try:
                        gw.trading.close_position(t_)
                        print(f"  · 兜底平仓 {t_}")
                    except GatewayError as e:
                        print(f"  · 兜底平仓 {t_} 未成（可能已平）：{e}")
                for t_ in own_orders:
                    try:
                        gw.trading.cancel_order(t_)
                        print(f"  · 兜底撤单 {t_}")
                    except GatewayError as e:
                        print(f"  · 兜底撤单 {t_} 未成（可能已撤）：{e}")
                # 终扫：同 magic（冒烟专属 902424）的残留如实报告
                for p_ in gw.account.positions(symbol):
                    if p_.magic == 902424:
                        step(False, f"残留持仓 {p_.ticket} 未清理", str(p_))
                for o_ in gw.account.pending_orders(symbol):
                    if o_.magic == 902424:
                        step(False, f"残留挂单 {o_.ticket} 未清理", str(o_))
            except Exception as e:  # noqa: BLE001 —— 兜底路径绝不抛出
                step(False, "兜底清理失败（可能仍有残留！）", f"{type(e).__name__}: {e}")

        if res is None:
            return 1

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
            # 委托历史（N2 的真机验证：volume_initial 字段映射不得崩）
            hist = gw.account.order_history(win_from, win_to)
            own_orders_hist = [h for h in hist if h.ticket in
                               ({res.order_ticket} | ({_pend_ticket} if _pend_ticket else set()))]
            step(len(hist) >= 1, f"委托历史 {len(hist)} 条（本单 {len(own_orders_hist)} 条）",
                 f"市价/挂单类型={[h.kind for h in own_orders_hist]}")
        except GatewayError as e:
            step(False, "成交回查", str(e))

    if FAILED:
        print(f"\n冒烟失败：{FAILED}")
        return 1
    print("\n冒烟全链路通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
