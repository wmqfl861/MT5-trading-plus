"""隔夜费校准持仓（U0 已确认）：模拟盘小手数开/平，供 calibrate_costs 实测。

动作：
    --open  开两笔校准仓：EURUSD 0.01 多（跨周三计费时刻）、XAUUSD 0.01 多（跨周末）
    --close 平掉本脚本 magic 的全部持仓（按 ticket 精确，绝不误碰其他仓）
    --list  只列出当前校准仓

铁律：只在模拟盘执行（账户 trade_mode 必须 = 0 DEMO，否则拒绝退出）；
magic=20261025 专用；不设 SL/TP（校准 swap 与价位无关，0.01 手风险极小）。
"""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5gw import GatewayConfig, MT5Gateway  # noqa: E402

MAGIC = 20261025
CALIBRATION = [("EURUSD", 0.01), ("XAUUSD", 0.01)]


def main() -> int:
    ap = argparse.ArgumentParser(description="隔夜费校准持仓（仅模拟盘）")
    ap.add_argument("--action", choices=("open", "close", "list"), required=True)
    ap.add_argument("--config", default="config.json")
    args = ap.parse_args()

    cfg = GatewayConfig()  # 默认 allow_live=False，保险丝只放行 DEMO
    with MT5Gateway(cfg) as gw:
        st = gw.account.state()
        if st.trade_mode != 0:
            print(f"✗ 拒绝：当前账户 trade_mode={st.trade_mode}（仅模拟盘）")
            return 1
        print(f"· 模拟盘 {st.login}@{st.server} equity={st.equity:.2f}")

        if args.action == "list":
            for p in gw.account.positions():
                if p.magic == MAGIC:
                    print(f"  #{p.ticket} {p.symbol} {p.side} {p.volume} "
                          f"open={p.price_open} swap={p.swap} "
                          f"entry={p.time.isoformat()}")
            return 0

        if args.action == "open":
            for sym, vol in CALIBRATION:
                r = gw.trading.market_order(
                    sym, "buy", vol, magic=MAGIC,
                    comment="swap-calibration")
                print(f"✓ {sym} buy {vol} → order#{r.order_ticket} "
                      f"deal#{r.deal_ticket} @{r.deal_price} "
                      f"retcode={r.retcode}")
            print("持仓计划：EURUSD 跨周三计费时刻后平；XAUUSD 跨周末后平"
                  "（由计划任务执行，勿手动干预）")
            return 0

        # close：只平本 magic 的仓
        n = 0
        for p in gw.account.positions():
            if p.magic != MAGIC:
                continue
            r = gw.trading.close_position(p.ticket, magic=MAGIC)
            print(f"✓ 平 #{p.ticket} {p.symbol} {p.volume} → "
                  f"deal#{r.deal_ticket} @{r.deal_price}")
            n += 1
        print(f"共平 {n} 笔（{datetime.now(timezone.utc).isoformat()}）")
        return 0


if __name__ == "__main__":
    sys.exit(main())
