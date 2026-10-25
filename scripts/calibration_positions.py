"""隔夜费校准持仓（U0 已确认）：模拟盘小手数开/平，供 calibrate_costs 实测。

动作：
    --open  开校准仓（手数一律取快照 volume_min，不写死）：
            EURUSD（跨周三计费时刻，外汇三倍日）、XAUUSD（跨周末，金属三倍日=周三）、
            BTCUSD（跨周末——"加密货币周末不收隔夜费"是最反直觉的一条，须实测）、
            USTEC（跨周五，指数三倍日=周五）
    --close 平掉本脚本 magic 的全部持仓（按 ticket 精确，绝不误碰其他仓）
    --list  只列出当前校准仓

铁律：只在模拟盘执行（账户 trade_mode 必须 = 0 DEMO，否则拒绝退出）；
magic=20261025 专用；不设 SL/TP（校准 swap 与价位无关，最小手数风险极小）。
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5gw import GatewayConfig, MT5Gateway  # noqa: E402

MAGIC = 20261025
# 四笔各有测量目的（T35 补 BTCUSD/USTEC）：
# - EURUSD/XAUUSD：H1 校准（跨周三计费时刻 + 跨周末）；
# - BTCUSD：验"加密货币周末不收隔夜费"（Exness 官方表，待实测）；
# - USTEC：验周五三倍（指数组）。
CALIBRATION_SYMBOLS = ("EURUSD", "XAUUSD", "BTCUSD", "USTEC")
DEFAULT_SPECS = "data/specs/latest.json"


def calibration_orders(specs: dict,
                       symbols: tuple[str, ...] = CALIBRATION_SYMBOLS
                       ) -> list[tuple[str, float]]:
    """快照 → [(品种, 手数)]；手数一律取快照 volume_min（最小手数），不写死。

    快照缺品种或缺 volume_min → KeyError（提示先跑 scripts/snapshot_specs.py）。
    """
    orders: list[tuple[str, float]] = []
    for sym in symbols:
        try:
            vol = specs[sym]["volume_min"]
        except (KeyError, TypeError) as e:
            raise KeyError(
                f"快照缺 {sym} 的 volume_min（先跑 scripts/snapshot_specs.py）") from e
        orders.append((sym, float(vol)))
    return orders


def main() -> int:
    ap = argparse.ArgumentParser(description="隔夜费校准持仓（仅模拟盘）")
    ap.add_argument("--action", choices=("open", "close", "list"), required=True)
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--specs", default=DEFAULT_SPECS,
                    help="品种参数快照（volume_min 的来源）")
    args = ap.parse_args()

    try:
        with open(args.specs, encoding="utf-8") as f:
            orders = calibration_orders(json.load(f))
    except FileNotFoundError:
        print(f"✗ 快照不存在：{args.specs}（先跑 scripts/snapshot_specs.py）")
        return 1
    except KeyError as e:
        print(f"✗ {e}")
        return 1

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
            for sym, vol in orders:
                r = gw.trading.market_order(
                    sym, "buy", vol, magic=MAGIC,
                    comment="swap-calibration")
                print(f"✓ {sym} buy {vol} → order#{r.order_ticket} "
                      f"deal#{r.deal_ticket} @{r.deal_price} "
                      f"retcode={r.retcode}")
            print("持仓计划：EURUSD 跨周三计费时刻后可平；XAUUSD/BTCUSD 跨周末后平"
                  "（验周末不计费）；USTEC 跨周五三倍日后平（10-30 之后）。"
                  "由管理员会话按计划平仓，无自动任务。")
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
