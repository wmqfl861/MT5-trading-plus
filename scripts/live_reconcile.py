"""对账 CLI（T14/P7 + T30/D19/D20）：意图 vs 账户实际持仓（按 magic 过滤）。

- 启动即全量对账一次；--loop 时每 --interval 秒轮询；
- 不一致：逐条打印差分，**只给有差分的 magic** 写 ``pause-<magic>.json``
  （paused=true）+ alerts.log 告警（loop 模式只在跳变时告警，不刷屏），
  exit 1；一致策略的 pause 文件写回 paused=false（自动解除）；
  全局 ``pause.json`` 只留给人工，本进程**绝不写**；
- pending 意图在 --grace-s（默认 60 秒）宽限内的 magic 本轮不判（下单
  在途不误报）；
- 意图文件缺失/不合法、持仓查询失败：exit 2（--loop 模式等待重试，
  运行器启动写出 expected-<magic>.json 后自愈）；
- T141（第十六轮计划）：断线（NotConnectedError）不退出——--loop 下降级
  继续（本轮跳过核对、角色心跳照写、网关按 5/10/30/60 秒退避重连），
  连续 15 分钟连不上才以专用退出码 78 退出交给 supervisor，并留
  data/live/disconnect-exit-reconcile.json 标记（不写 halt）。

用法：
    .venv/Scripts/python.exe scripts/live_reconcile.py --data-dir data --once
    .venv/Scripts/python.exe scripts/live_reconcile.py --data-dir data --loop --interval 30
    # 影子盘（T34）：actual 持仓读 paper 状态（目录=聚合全部
    # state-<magic>.json，每轮重读——R6/C3；单状态文件也兼容），
    # 不连真实终端
    .venv/Scripts/python.exe scripts/live_reconcile.py --data-dir data --once \
        --paper-state data/live/paper
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5gw import GatewayConfig, MT5Gateway  # noqa: E402
from mt5gw.errors import DataUnavailableError, NotConnectedError  # noqa: E402
from mt5live.monitor import (  # noqa: E402
    ALERT_DISCONNECTED,
    ALERT_RECONCILE_MISMATCH,
    DisconnectWatch,
    append_alert,
    naive_utc,
    guarded_main,
    reset_watchdog,
    write_role_heartbeat,
)
from mt5live.paper import PaperStateGateway  # noqa: E402（T34 --paper-state）
from mt5live.reconcile import (  # noqa: E402
    DEFAULT_GRACE_S,
    ExpectedStateError,
    ReconcileResult,
    diff_positions,
    load_intents,
    write_pause,
)

# T141：断线计时的单调钟接缝（测试注入虚拟钟；生产恒 time.monotonic）。
_mono = time.monotonic


def _open_gateway(gw_config_path: str) -> MT5Gateway:
    """连接 MT5 终端（只读持仓）；配置缺失用默认网关配置。"""
    if os.path.exists(gw_config_path):
        gw = MT5Gateway.from_config_file(gw_config_path)
    else:
        gw = MT5Gateway(GatewayConfig())
    gw.connect()
    return gw


def run_once(live_dir: str, gw, *,
             volume_tolerance: float, alert: bool = True,
             now: datetime | None = None,
             grace_s: float = DEFAULT_GRACE_S,
             ) -> tuple[int, ReconcileResult | None]:
    """单轮对账（T30：意图语义 + 按策略暂停）；返回 (exit_code, 结果|None)。

    gw 是"带 account 的网关对象"：真终端 MT5Gateway，或 T34 影子盘的
    PaperStateGateway（--paper-state：paper 目录聚合全部 state-<magic>.json
    或单状态文件，每查询重读——R6/C3、不碰真实账户）。

    有差分的 magic 写 pause-<magic>.json（paused=true），其余登记 magic
    写回 paused=false；全局 pause.json 只留给人工，本函数不写。
    """
    n = naive_utc(now if now is not None
                  else datetime.now(timezone.utc).replace(tzinfo=None))
    try:
        intents = load_intents(live_dir)   # T30：读全部 expected-<magic>.json
    except ExpectedStateError as e:
        print(f"× {e}")
        return 2, None
    try:
        actual = gw.account.positions()
    except DataUnavailableError as e:
        print(f"× 持仓查询失败：{e}")
        return 2, None

    res = diff_positions(intents, actual, now=n, grace_s=grace_s,
                         volume_tolerance=volume_tolerance)
    paused_magics = sorted({d.magic for d in res.diffs})
    for magic in sorted({i.magic for i in intents}):
        if magic in paused_magics:
            write_pause(live_dir, magic, paused=True,
                        reason="reconcile_mismatch",
                        diffs=[d for d in res.diffs if d.magic == magic],
                        now=n)
        else:
            write_pause(live_dir, magic, paused=False, reason=None, now=n)

    write_role_heartbeat(live_dir, "reconcile", interval=30.0,
                         reconcile_clean=res.ok and all(i.status == "confirmed" for i in intents))
    if res.ok:
        print(f"· {n:%Y-%m-%d %H:%M:%S} UTC 对账一致（{len(actual)} 个持仓）", flush=True)
        return 0, res
    for d in res.diffs:
        print(f"× {d.detail}")
    if alert:
        append_alert(live_dir, ALERT_RECONCILE_MISMATCH,
                     f"{len(res.diffs)} 处持仓不一致，已暂停 magic "
                     f"{paused_magics}（详见 pause-<magic>.json）", n)
    return 1, res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="持仓对账（意图 vs actual，按 magic 过滤、按策略暂停）")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--gw-config", default="config.json", help="网关配置 JSON")
    ap.add_argument("--paper-state", default=None,
                    help="影子券商状态（T34/R6）：paper 目录（每轮聚合读全部"
                         " state-<magic>.json——多运行器并行）或单个状态文件"
                         "（单策略兼容）；actual 持仓读它而非真实终端，"
                         "给了就不连网关")
    ap.add_argument("--once", action="store_true", help="单轮后退出（默认行为）")
    ap.add_argument("--loop", action="store_true", help="常驻轮询")
    ap.add_argument("--interval", type=float, default=30.0, help="轮询间隔（秒）")
    ap.add_argument("--volume-tolerance", type=float, default=1e-6,
                    help="净手数比较容差（手）")
    ap.add_argument("--grace-s", type=float, default=DEFAULT_GRACE_S,
                    help="pending 意图的宽限秒数（下单在途不判）")
    args = ap.parse_args(argv)
    return guarded_main("reconcile", os.path.join(args.data_dir, "live"), args.interval,
                        lambda: _run(args))


def _run(args) -> int:
    live_dir = os.path.join(args.data_dir, "live")
    try:
        if args.paper_state:
            # alert_dir：影子状态读失败（按空处理）时同步落 alerts.log
            gw = PaperStateGateway(args.paper_state, alert_dir=live_dir)
        else:
            gw = _open_gateway(args.gw_config)
    except Exception:
        raise  # guarded_main records the original traceback

    # T141：连续断线计时——降级继续，连续 15 分钟才以专用退出码 78 退出
    #（disconnect-exit-reconcile.json 留痕，供 T143 supervisor 演练与取证）。
    watch = DisconnectWatch(live_dir, "reconcile", clock=_mono)

    def _handle_disconnect(exc: NotConnectedError) -> int | None:
        """断线降级轮：本轮跳过核对（断线中持仓不可见也改不动）；角色
        心跳照写（supervisor 判活不受降级影响）；网关有界重连（到点才真
        试）。返回非 None = 容忍耗尽（调用方直接 return 专用码）。"""
        watch.note_failure(exc)
        if watch.expired():
            return watch.exit_code(last_error=f"{type(exc).__name__}: {exc}")
        print(f"× {datetime.now(timezone.utc):%H:%M:%S} 断线（降级继续，"
              f"本轮跳过核对）：{exc}", flush=True)
        write_role_heartbeat(live_dir, "reconcile", interval=args.interval)
        reconnect = getattr(gw, "reconnect", None)
        if callable(reconnect):
            reconnect()
        return None

    try:
        if args.loop:
            was_ok: bool | None = None  # 首轮与跳变时告警；持续不一致不重复告警
            while True:
                reset_watchdog(args.interval)
                try:
                    _, res = run_once(live_dir, gw,
                                      volume_tolerance=args.volume_tolerance,
                                      grace_s=args.grace_s,
                                      alert=was_ok is not False)
                except NotConnectedError as e:
                    rc = _handle_disconnect(e)      # T141：断线降级继续
                    if rc is not None:
                        return rc
                else:
                    watch.note_ok()                 # T141：恢复（段内才告警）
                    if res is not None:
                        was_ok = res.ok
                time.sleep(args.interval)
        try:
            rc, _ = run_once(live_dir, gw, volume_tolerance=args.volume_tolerance,
                             grace_s=args.grace_s)
        except NotConnectedError as e:
            # 单轮模式遇断线：与持仓查询失败同口径（exit 2，不 fatal）。
            print(f"× 断线：{e}")
            append_alert(live_dir, ALERT_DISCONNECTED, f"reconcile 单轮断线：{e}")
            return 2
        return rc
    except KeyboardInterrupt:
        print("· 手动停止")
        return 0
    finally:
        gw.shutdown()


if __name__ == "__main__":
    sys.exit(main())
