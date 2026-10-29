"""T89/R14-L5：order_check 实测 MT5 comment 参数边界（不下单）。

背景（docs/reviews/2026-10-29-review-round14.md L5 + docs/ops/phase-f-log.md）：
- 2026-10-28 首战事故：comment="XAUUSD|M15|2026-10-28T06:30:00"（30 字符）被拒，
  last_error=(-2, 'Invalid "comment" argument')。
- 两份公开实测报告：MetaTrader5 Python 库对 ≥30 字符注释返回 -2，
  29 字符通过；'|' 与 ':' 是否单独致拒未证实。

本脚本在**真实模拟盘**上用 order_check（只读校验，绝不发真实单）逐组验证：
1. 29 字符纯字母数字（公开报告的通过边界）
2. 30 字符纯字母数字（公开报告的拒绝边界）
3. 短注释含 '|'（事故 comment 的嫌疑字符，长度 < 29 排除长度因素）

用法（终端可达时）：
    .venv/Scripts/python.exe scripts/order_check_comments.py [--symbol XAUUSD] [--config config.json]

实测结果（同时记录在 src/mt5gw/trading.py 的 MT5_COMMENT_MAX 注释处）：
- 2026-10-29 17:33 UTC，Demo 终端（只读 order_check，未发真实单，XAUUSD 最小手数）：
    29 字符纯字母数字 → check_retcode=0 "Done"，last_error=(1, 'Success')：**通过**
    30 字符纯字母数字 → order_check 无返回，last_error=(-2, 'Invalid "comment"
      argument')：**被拒**（客户端参数校验，请求未发出）
    短注释 'XAUUSD|M15'（10 字符，含 '|'）→ check_retcode=0 "Done"：**通过**
  结论：根因确认是**长度**（≥30 被拒、29 通过），'|' 单独不致拒——F 阶段
  "白名单字符"方向正确但根因记载（'|' 与 ':' 被拒）不成立；上限取 25 留余量。
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5gw import GatewayConfig, MT5Gateway, load  # noqa: E402
from mt5gw.trading import pick_filling  # noqa: E402

# 逐组探针（name, comment）：29/30 字符纯字母数字 + 短注释含 '|'
COMMENT_CASES = [
    ("29_alnum", "a" * 29),
    ("30_alnum", "a" * 30),
    ("short_pipe", "XAUUSD|M15"),
]


def build_probe_request(mt5, *, symbol: str, volume: float, price: float,
                        magic: int, comment: str, filling_bits: int) -> dict:
    """构造最小合法市价买单请求（仅供 order_check 校验，绝不 order_send）。"""
    return {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "type": mt5.ORDER_TYPE_BUY,
        "volume": volume,
        "price": price,
        "deviation": 20,
        "magic": magic,
        "comment": comment,
        "type_time": 0,
        "type_filling": pick_filling(filling_bits),
    }


def run_cases(conn, mt5, *, symbol: str, volume: float, price: float,
              magic: int, filling_bits: int) -> list[dict]:
    """对 COMMENT_CASES 逐组执行 order_check，返回 逐组 retcode/comment/last_error。

    conn 为 mt5gw.connection.MT5Connection（同锁串行）；测试注入 FakeMT5
    连接验证矩阵逻辑，真实终端结果由 main() 落盘打印。
    """
    results = []
    for name, comment in COMMENT_CASES:
        req = build_probe_request(
            mt5, symbol=symbol, volume=volume, price=price, magic=magic,
            comment=comment, filling_bits=filling_bits)
        res = conn.call(mt5.order_check, req)   # 只读校验：不 order_send
        results.append({
            "case": name,
            "comment": comment,
            "length": len(comment),
            "check_retcode": getattr(res, "retcode", None),
            "check_comment": getattr(res, "comment", None),
            "last_error": conn.last_error(),
        })
    return results


def main() -> int:
    ap = argparse.ArgumentParser(
        description="order_check 实测 MT5 comment 参数边界（不下单）")
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--config", default="config.json",
                    help="网关配置 JSON（不存在则用默认配置）")
    args = ap.parse_args()

    cfg = (load(args.config) if os.path.exists(args.config)
           else GatewayConfig())
    with MT5Gateway(cfg) as gw:
        spec = gw.symbols.get(args.symbol)      # 含 Market Watch 可见性保证
        quote = gw.market.quote(args.symbol)
        # order_check 不在门面职责内（网关只封装下单/查询服务）——探测脚本
        # 直接用连接层与底层模块句柄（同锁串行，与交易路径互不并发）
        results = run_cases(
            gw._conn, gw._mt5, symbol=args.symbol, volume=spec.volume_min,
            price=quote.ask, magic=cfg.magic, filling_bits=spec.filling_mode)

    print(f"order_check 实测（symbol={args.symbol}, volume=最小手数, "
          f"只读校验未发单）：")
    for r in results:
        print(f"  [{r['case']}] len={r['length']:>2} comment={r['comment']!r} → "
              f"check_retcode={r['check_retcode']} "
              f"check_comment={r['check_comment']!r} last_error={r['last_error']}")
    print("判读：check_retcode=0 且 last_error=Success → 通过；"
          "无返回/retcode 异常且 last_error=(-2, ...) → 客户端参数拒绝。")
    print("请把以上逐组结果回填到 src/mt5gw/trading.py 的 MT5_COMMENT_MAX 注释。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
