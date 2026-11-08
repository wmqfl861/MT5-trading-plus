"""对账 CLI（T14/P7 + T30/D19/D20）：意图 vs 账户实际持仓（按 magic 过滤）。

- 启动即全量对账一次；--loop 时每 --interval 秒轮询；
- 不一致：逐条打印差分，**只给有差分的 magic** 写 ``pause-<magic>.json``
  （paused=true）+ alerts.log 告警（loop 模式只在跳变时告警，不刷屏），
  exit 1；一致策略的 pause 文件写回 paused=false（自动解除）；
  全局 ``pause.json`` 只留给人工，本进程**绝不写**；
- pending 意图在 --grace-s（默认 60 秒）宽限内的 magic 本轮不判（下单
  在途不误报）；
- 意图文件缺失/不合法：exit 2（--once 模式；--loop 模式原为无限等待
  重试——T142 起按"已知可恢复"计数：连续 5 轮以专用退出码 79 退出并留
  标记，运行器启动写出 expected-<magic>.json 后 supervisor 重启本进程
  即自愈）；
- 持仓查询失败（DataUnavailableError，数据暂缺）：--once 仍 exit 2（业务
  性失败，不写 halt）；--loop 下自 T184（O17-4）起不进 5 轮计数，改独立
  时间窗——段首/恢复各一条告警、段内不刷屏，连续
  --account-unavailable-limit-s（缺省 300 秒）才以 79 退出并写 halt
  （cause=account_unavailable_window）；
- T141（第十六轮计划）：断线（NotConnectedError）不退出——--loop 下降级
  继续（本轮跳过核对、角色心跳照写、网关按 5/10/30/60 秒退避重连），
  连续 15 分钟连不上才以专用退出码 78 退出交给 supervisor，并留
  data/live/disconnect-exit-reconcile.json 标记（不写 halt）；
- T142（第十六轮计划）：主循环异常分级——已知可恢复（run_once rc=2、
  文件被占用 PermissionError、锁等待 TimeoutError；断线走 78 不进计数）
  记录后继续；连续 5 轮或任何未知异常 → 写
  data/live/loop-exit-reconcile.json（reason=unrecoverable_loop/<具体>）
  + halt（fail-closed）后以专用退出码 79 退出。对账不一致（rc=1，按规则
  写 pause）是动作不是故障，视为成功轮。完整码表见
  src/mt5live/monitor.py 的 T142 注释块（supervisor/取证统一汇总）。

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
    ACCOUNT_UNAVAILABLE_LIMIT_S,
    ALERT_DISCONNECTED,
    ALERT_RECONCILE_MISMATCH,
    LOOP_EXIT_RC,
    AccountUnavailableWatch,
    DisconnectWatch,
    LoopFailureWatch,
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

# T142：主循环"已知可恢复"异常类（文件被占用/锁等待超时；意图文件问题
# 在本进程表现为 run_once 的 rc=2，不抛异常）。断线（NotConnectedError）
# 与持仓查询的数据暂缺（DataUnavailableError，T184/O17-4）都不在此列：
# 前者只走 T141 的 15 分钟时间窗（78），后者走 AccountUnavailableWatch 的
# 独立时间窗（缺省 300 秒，同样以 79 退出），均不进 5 轮计数
#（tests/test_t184_account_window.py 钉死三套计时互不干扰）。
_RECOVERABLE = (PermissionError, TimeoutError)


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

    持仓查询的 DataUnavailableError **上抛不吞**（T184/O17-4）：--once 由
    调用处按业务性 exit 2 处理；--loop 由主循环走账户取数失败的独立时间窗
    （缺省 300 秒），不再折成 rc=2 进 5 轮计数。
    """
    n = naive_utc(now if now is not None
                  else datetime.now(timezone.utc).replace(tzinfo=None))
    try:
        intents = load_intents(live_dir)   # T30：读全部 expected-<magic>.json
    except ExpectedStateError as e:
        print(f"× {e}")
        return 2, None
    actual = gw.account.positions()        # DUA 上抛（见 docstring，T184）

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
    ap.add_argument("--account-unavailable-limit-s", type=float,
                    default=ACCOUNT_UNAVAILABLE_LIMIT_S,
                    help="持仓查询失败的连续时间窗上限（秒，T184/O17-4）——"
                         "连续这么久取不到持仓才以 79 退出并写 halt；缺省 "
                         f"{ACCOUNT_UNAVAILABLE_LIMIT_S:.0f} 秒")
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

    # T184（O17-4）：持仓查询失败的连续时间窗——段首/恢复各一条告警、
    # 段内不刷屏，连续 --account-unavailable-limit-s（缺省 300 秒）才以
    # 79 退出并写 halt（cause=account_unavailable_window）。
    acct_watch = AccountUnavailableWatch(
        live_dir, "reconcile", limit_s=args.account_unavailable_limit_s,
        clock=_mono)

    # T142：已知可恢复失败的连续轮计数（run_once rc=2 或可恢复异常）——
    # 连续 5 轮或未知异常才以专用退出码 79 退出（loop-exit-reconcile.json
    # + halt 留痕）。rc=1（按规则写 pause）是动作不是故障，视为成功轮。
    failures = LoopFailureWatch(live_dir, "reconcile")

    def _account_failed(e: Exception) -> int | None:
        """T184：持仓查询失败（数据暂缺）——独立时间窗计，不进 5 轮。"""
        acct_watch.note_failure(e)       # 段首一条告警，段内不刷屏
        print(f"× {datetime.now(timezone.utc):%H:%M:%S} 持仓查询失败"
              f"（数据暂缺，T184 时间窗 {acct_watch.unavailable_s:.0f}/"
              f"{acct_watch.limit_s:.0f} 秒）：{e}", flush=True)
        try:   # 降级照写角色心跳（与 T141/T142 失败轮同口径：进程还活着）
            write_role_heartbeat(live_dir, "reconcile", interval=args.interval)
        except Exception as nested:
            print(f"× 失败轮心跳留痕异常（继续计时）：{nested}", flush=True)
        if acct_watch.expired():
            return acct_watch.exit_code(
                last_error=f"{type(e).__name__}: {e}")
        return None

    def _round_failed(detail: str) -> int | None:
        """T142：已知可恢复失败轮——留痕后继续；连续 5 轮 → 专用退出。"""
        failures.note_failure(detail)
        print(f"· 连续可恢复失败 {failures.consecutive_failures}/"
              f"{failures.limit} 轮（满 {failures.limit} 轮以退出码 "
              f"{LOOP_EXIT_RC} 退出，T142）", flush=True)
        try:   # 降级照写角色心跳（与 T141 断线降级轮同口径：进程还活着）
            write_role_heartbeat(live_dir, "reconcile", interval=args.interval)
        except Exception as nested:
            print(f"× 失败轮心跳留痕异常（继续计数）：{nested}", flush=True)
        if failures.expired():
            return failures.exit_code(detail=detail,
                                      cause="consecutive_failures")
        return None

    def _unknown_failed(e: Exception) -> int:
        """T142：未知异常——立即写退出标记并以专用码退出（不再落到 rc=2）。"""
        return failures.exit_code(exc=e, cause="unknown_exception")

    def _handle_disconnect(exc: NotConnectedError) -> int | None:
        """断线降级轮：本轮跳过核对（断线中持仓不可见也改不动）；角色
        心跳照写（supervisor 判活不受降级影响）；网关有界重连（到点才真
        试）。返回非 None = 容忍耗尽（调用方直接 return 专用码）。"""
        watch.note_failure(exc)
        if watch.expired():
            return watch.exit_code(last_error=f"{type(exc).__name__}: {exc}")
        print(f"× {datetime.now(timezone.utc):%H:%M:%S} 断线（降级继续，"
              f"本轮跳过核对）：{exc}", flush=True)
        try:
            write_role_heartbeat(live_dir, "reconcile", interval=args.interval)
        except _RECOVERABLE as e2:                 # T142：降级轮同样分级
            rc = _round_failed(f"{type(e2).__name__}: {e2}")
            if rc is not None:
                return rc
        except Exception as e2:
            return _unknown_failed(e2)
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
                    rc, res = run_once(live_dir, gw,
                                       volume_tolerance=args.volume_tolerance,
                                       grace_s=args.grace_s,
                                       alert=was_ok is not False)
                except NotConnectedError as e:
                    rc2 = _handle_disconnect(e)     # T141：断线降级继续
                    if rc2 is not None:             # （不进其他两套计时）
                        return rc2
                except DataUnavailableError as e:
                    rc2 = _account_failed(e)        # T184：时间窗计
                    if rc2 is not None:             # （不进 5 轮计数）
                        return rc2
                except _RECOVERABLE as e:
                    rc2 = _round_failed(f"{type(e).__name__}: {e}")
                    if rc2 is not None:
                        return rc2
                except Exception as e:
                    return _unknown_failed(e)       # T142：未知——专用退出
                else:
                    watch.note_ok()                 # T141：恢复（段内才告警）
                    if rc == 2:
                        # T142：意图文件缺失/不合法（run_once 已打印具体
                        # 原因并返回 rc=2；持仓查询失败自 T184 起上抛走
                        # 时间窗，不落这里）。
                        rc2 = _round_failed(
                            "run_once rc=2：意图文件缺失/不合法"
                            "（具体见本轮输出）")
                        if rc2 is not None:
                            return rc2
                    else:
                        acct_watch.note_ok()  # T184 F-1：只有 rc 0/1 的完整
                        failures.note_ok()    # 成功轮才清账户窗（rc=2 无网关
                                              # 接触，不得写无证据的恢复）
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
        except DataUnavailableError as e:
            # 单轮模式持仓查询失败：业务性 exit 2（不写 halt、不经
            # guarded_main 的 fatal 分支）——T184 起上抛，口径不变。
            print(f"× 持仓查询失败：{e}")
            return 2
        return rc
    except KeyboardInterrupt:
        print("· 手动停止")
        return 0
    finally:
        gw.shutdown()


if __name__ == "__main__":
    sys.exit(main())
