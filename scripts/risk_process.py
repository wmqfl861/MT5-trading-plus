"""风控主循环 CLI（T14/P7，独立于策略进程运行——蓝图 §4 第 10 条）。

每轮：
1. 读全部登记策略的心跳（data/live/heartbeat-<magic>.json，T24/M9；兼容
   旧版单文件 heartbeat.json）——任一超时/缺失 → 置该文件 runner_alive=false
   并写 data/live/halt.json（fail-closed：运行器未启动也拒绝交易。恢复 =
   人工删除 halt.json；建议启动顺序：先运行器、后风控进程）；
2. 检查 data/live/killSwitch——存在 → 写 halt.json（一键停机，不自动平仓）；
3. 轮询账户——当日（UTC 日）已实现+浮动 ≤ -daily_loss_cap_usd → 写
   data/live/commands/flatten.json（执行器全平后写 flatten.ack-<magic> 确认，
   T24）；所有登记 magic 都确认后把指令文件归档到 commands/archive/。

退出码：0 = 本轮完成（含"按规则写了 halt/flatten"——那是动作不是故障）；
1 = 账户轮询失败（--once 模式）；2 = 配置/连接等不可恢复错误；
78 = T141 断线专用：--loop 下连续 15 分钟连不上交易服务器，降级轮（文件
     级检查照常、角色心跳照写）耗尽容忍后以本码退出交给 supervisor，并留
     data/live/disconnect-exit-risk.json 标记（不写 halt——重启后果由
     supervisor 决定）；
79 = T142 主循环不可恢复：文件访问类可恢复异常（文件被占用/锁等待超时）
     连续 5 轮失败，或任何未知异常 → 写 data/live/loop-exit-risk.json
     （reason=unrecoverable_loop/<具体>）+ halt（fail-closed：监视者离开
     必须停交易）后退出。T184（O17-4）起账户取数失败（数据暂缺）不再计
     5 轮（--interval 10 下约 50 秒就退出，把终端抖动放大成整夜停机），
     改独立时间窗：连续 --account-unavailable-limit-s（缺省 300 秒）才以
     本码退出并写 halt（cause=account_unavailable_window）；断线仍只走 78
     的 15 分钟窗，三套计时互不干扰。完整码表见 src/mt5live/monitor.py
     的 T142 注释块（supervisor/取证统一汇总）。

用法：
    .venv/Scripts/python.exe scripts/risk_process.py --config config/risk.toml --once
    .venv/Scripts/python.exe scripts/risk_process.py --config config/risk.toml --loop --interval 10
    # 影子盘（T34）：账户快照读 paper 状态（目录=聚合全部 state-<magic>.json，
    # 每轮重读——多运行器并行、常驻风控看到最新成交与浮盈；R6/C3），
    # 不连真实终端；也可传单个状态文件（单策略兼容）
    .venv/Scripts/python.exe scripts/risk_process.py --config config/risk.toml --once \
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
    ALERT_ACCOUNT_POLL_ERROR,
    ALERT_DISCONNECTED,
    LOOP_EXIT_RC,
    AccountUnavailableWatch,
    DisconnectWatch,
    LoopFailureWatch,
    append_alert,
    guarded_main,
    reset_watchdog,
    write_role_heartbeat,
)
from mt5live.paper import PaperStateGateway  # noqa: E402（T34 --paper-state）
from mt5live.riskproc import (  # noqa: E402
    AccountSnapshot,
    CycleResult,
    RiskConfigError,
    RiskProcess,
    load_risk_config,
)

# T141：断线计时的单调钟接缝（测试注入虚拟钟；生产恒 time.monotonic）。
_mono = time.monotonic

# T142：主循环"已知可恢复"异常类（记录后继续、连续 5 轮才退出）——
# 文件被占用（PermissionError——11-01 签名）、跨进程文件锁等待超时
# （TimeoutError，fileio 锁预算耗尽）。
# 数据暂缺（DataUnavailableError）与断线（NotConnectedError）都不在此列：
# 断线只走 T141 的 15 分钟时间窗（78）；账户取数失败自 T184（O17-4）起
# 走 AccountUnavailableWatch 的独立时间窗（缺省 300 秒，同样以 79 退出），
# 不进 5 轮计数（时间契约优先，见 monitor 边界注释与
# tests/test_t184_account_window.py）。
_RECOVERABLE = (PermissionError, TimeoutError)


def _open_gateway(gw_config_path: str) -> MT5Gateway:
    """连接 MT5 终端（本进程只读账户，绝不下单）；配置缺失用默认网关配置。"""
    if os.path.exists(gw_config_path):
        gw = MT5Gateway.from_config_file(gw_config_path)
    else:
        gw = MT5Gateway(GatewayConfig())
    gw.connect()
    return gw


def build_snapshot(gw, now: datetime) -> AccountSnapshot:
    """轮询账户：状态 + 全部持仓 + 当日（UTC 00:00 起）全部成交流水。

    gw 是"带 account 的网关对象"：真终端 MT5Gateway，或 T34 影子盘的
    PaperStateGateway（--paper-state：paper 目录聚合全部 state-<magic>.json
    或单状态文件，每查询重读——R6/C3、不碰真实账户）。

    R7/N-C2：account 提供 snapshot()（PaperAccountView）→ **单次**调用取
    同一版本的持仓+当日成交+状态——两次独立查询之间落盘的平仓不会被同时
    计为浮亏与已实现；状态文件存在但读不出 → 经聚合视图抛
    DataUnavailableError（调用方按轮询失败告警/重试，绝不静默当日盈亏
    变 0）。真终端退回逐查询。
    """
    snap_fn = getattr(gw.account, "snapshot", None)
    snap = snap_fn() if callable(snap_fn) else None
    state = snap.state if snap is not None else gw.account.state()
    if state.currency != "USD":
        raise ValueError(
            f"账户货币须为 USD（美元暴露/日亏口径），收到 {state.currency!r}")
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if snap is not None:
        positions: tuple = snap.positions
        deals = tuple(d for d in snap.deals if day_start <= d.time <= now)
    else:
        positions = tuple(gw.account.positions())
        deals = tuple(gw.account.deals(day_start, now))
    return AccountSnapshot(currency=state.currency, positions=positions,
                           deals=deals)


def run_one_cycle(risk: RiskProcess, gw, *,
                  degraded_reason: str | None = None) -> CycleResult:
    """单轮（账户轮询失败如实抛 DataUnavailableError/NotConnectedError，
    由调用方决定重试/降级）。

    T141：degraded_reason 非 None = 断线降级轮——不查账户（snapshot=None，
    日亏/分线检查本轮跳过），文件级检查（心跳/killSwitch/flatten 归档）
    照常，角色心跳照写（supervisor 判活不受降级影响）。
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    snap = None if degraded_reason else build_snapshot(gw, now)
    res = risk.run_cycle(snap, now, degraded=degraded_reason is not None,
                         health_clock=lambda: datetime.now(timezone.utc))
    mode = ("paper" if isinstance(gw, PaperStateGateway) else "live")
    if degraded_reason:
        mode += "·断线降级"
    print(f"· {now:%Y-%m-%d %H:%M:%S} UTC [{mode}] 心跳={res.heartbeat_reason} "
          f"halt={'是' if res.halt_active else '否'} "
          f"flatten={'写入' if res.flatten_written else '—'} "
          f"日亏触发={'是' if res.daily_loss_breached else '否'}", flush=True)
    write_role_heartbeat(risk.live_dir, "risk", interval=getattr(risk, "interval_s", 10.0))
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="独立风控主循环（只读账户，不下单）")
    ap.add_argument("--config", default="config/risk.toml", help="风控配置 TOML")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--gw-config", default="config.json", help="网关配置 JSON")
    ap.add_argument("--sessions-config", default=None)
    ap.add_argument("--paper-state", default=None,
                    help="影子券商状态（T34/R6）：paper 目录（每轮聚合读全部"
                         " state-<magic>.json——多运行器并行）或单个状态文件"
                         "（单策略兼容）；账户快照读它而非真实终端，"
                         "给了就不连网关")
    ap.add_argument("--once", action="store_true", help="单轮后退出（默认行为）")
    ap.add_argument("--loop", action="store_true", help="常驻轮询")
    ap.add_argument("--interval", type=float, default=10.0, help="轮询间隔（秒）")
    ap.add_argument("--account-unavailable-limit-s", type=float,
                    default=ACCOUNT_UNAVAILABLE_LIMIT_S,
                    help="账户取数失败的连续时间窗上限（秒，T184/O17-4）——"
                         "连续这么久取不到账户才以 79 退出并写 halt；缺省 "
                         f"{ACCOUNT_UNAVAILABLE_LIMIT_S:.0f} 秒")
    ap.add_argument("--heartbeat-timeout", type=float, default=120.0,
                    help="心跳超时判定（秒）")
    args = ap.parse_args(argv)
    return guarded_main("risk", os.path.join(args.data_dir, "live"), args.interval,
                        lambda: _run(args))


def _run(args) -> int:
    try:
        config = load_risk_config(args.config)
    except RiskConfigError as e:
        print(f"× {e}")
        return 2
    from mt5live.riskproc import load_default_session_policy
    risk = RiskProcess(
        config, live_dir=os.path.join(args.data_dir, "live"),
        heartbeat_timeout_s=args.heartbeat_timeout,
        session_policy=load_default_session_policy(args.sessions_config))
    risk.interval_s = args.interval

    try:
        if args.paper_state:
            try:
                # alert_dir：影子状态读失败（fail-closed 数据不可用）时
                # 同步落 alerts.log
                gw = PaperStateGateway(args.paper_state,
                                       alert_dir=risk.live_dir)
            except DataUnavailableError as e:
                print(f"× 影子状态不可用：{e}")
                return 2
        else:
            gw = _open_gateway(args.gw_config)
    except Exception:
        raise  # guarded_main records CRITICAL traceback and durable exit evidence

    def _poll_failed(e: Exception) -> None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        append_alert(risk.live_dir, ALERT_ACCOUNT_POLL_ERROR, str(e), now)
        print(f"× {now:%H:%M:%S} 账户轮询失败：{e}")

    # T141：连续断线计时——降级继续，连续 15 分钟才以专用退出码 78 退出
    #（disconnect-exit-risk.json 留痕，供 T143 supervisor 演练与取证）。
    watch = DisconnectWatch(risk.live_dir, "risk", clock=_mono)

    # T184（O17-4）：账户取数失败的连续时间窗——段首/恢复各一条告警、
    # 段内不刷屏，连续 --account-unavailable-limit-s（缺省 300 秒）才以
    # 79 退出并写 halt（cause=account_unavailable_window）。
    acct_watch = AccountUnavailableWatch(risk.live_dir, "risk",
                                         limit_s=args.account_unavailable_limit_s,
                                         clock=_mono)

    # T142：已知可恢复异常（文件访问类）的连续失败轮计数——记录后继续，
    # 连续 5 轮或未知异常才以专用退出码 79 退出（loop-exit-risk.json +
    # halt 留痕）。
    failures = LoopFailureWatch(risk.live_dir, "risk")

    def _account_failed(e: Exception) -> int | None:
        """T184：账户取数失败（数据暂缺）——独立时间窗计，不进 5 轮；
        连续超窗才以 79 退出并写 halt。"""
        acct_watch.note_failure(e)       # 段首一条告警，段内不刷屏
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        print(f"× {now:%H:%M:%S} 账户取数失败（数据暂缺，T184 时间窗 "
              f"{acct_watch.unavailable_s:.0f}/{acct_watch.limit_s:.0f} 秒）："
              f"{e}", flush=True)
        try:   # 降级照写角色心跳（与 T141/T142 失败轮同口径：进程还活着）
            write_role_heartbeat(risk.live_dir, "risk",
                                 interval=args.interval)
        except Exception as nested:
            print(f"× 失败轮心跳留痕异常（继续计时）：{nested}", flush=True)
        if acct_watch.expired():
            return acct_watch.exit_code(
                last_error=f"{type(e).__name__}: {e}")
        return None

    def _round_failed(e: Exception) -> int | None:
        """T142：文件访问类可恢复异常——告警/打印后继续；连续 5 轮 → 专用退出。"""
        try:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            print(f"× {now:%H:%M:%S} 本轮失败（可恢复，文件访问类）：{e}",
                  flush=True)
            # 降级照写角色心跳（与 T141 断线降级轮同口径：进程还活着）
            write_role_heartbeat(risk.live_dir, "risk",
                                 interval=args.interval)
        except Exception as nested:      # 留痕本身失败不推翻分级
            print(f"× 失败轮留痕异常（继续计数）：{nested}", flush=True)
        failures.note_failure(e)
        print(f"· 连续可恢复失败 {failures.consecutive_failures}/"
              f"{failures.limit} 轮（满 {failures.limit} 轮以退出码 "
              f"{LOOP_EXIT_RC} 退出，T142）", flush=True)
        if failures.expired():
            return failures.exit_code(exc=e, cause="consecutive_failures")
        return None

    def _unknown_failed(e: Exception) -> int:
        """T142：未知异常——立即写退出标记并以专用码退出（不再落到 rc=2）。"""
        return failures.exit_code(exc=e, cause="unknown_exception")

    def _handle_disconnect(exc: NotConnectedError) -> int | None:
        """断线降级轮；返回非 None = 容忍耗尽（调用方直接 return 专用码）。"""
        watch.note_failure(exc)
        if watch.expired():
            return watch.exit_code(last_error=f"{type(exc).__name__}: {exc}")
        try:
            run_one_cycle(risk, gw, degraded_reason=str(exc))
        except DataUnavailableError as e2:     # T184：账户暂缺同窗计
            rc = _account_failed(e2)           #（不进 5 轮，也不算未知）
            if rc is not None:
                return rc
        except _RECOVERABLE as e2:             # T142：降级轮同样分级
            rc = _round_failed(e2)
            if rc is not None:
                return rc
        except Exception as e2:
            return _unknown_failed(e2)
        reconnect = getattr(gw, "reconnect", None)
        if callable(reconnect):
            # T141 有界重连：到点才真试（5/10/30/60 退避在网关层），
            # 未到点立即返回——不阻塞轮询节奏。
            reconnect()
        return None

    try:
        if args.loop:
            while True:
                reset_watchdog(args.interval)
                try:
                    run_one_cycle(risk, gw)
                except NotConnectedError as e:
                    rc = _handle_disconnect(e)      # T141：断线降级继续
                    if rc is not None:              # （不进其他两套计时）
                        return rc
                except DataUnavailableError as e:
                    rc = _account_failed(e)         # T184：时间窗计
                    if rc is not None:              # （不进 5 轮计数）
                        return rc
                except _RECOVERABLE as e:
                    rc = _round_failed(e)           # T142：可恢复——继续
                    if rc is not None:
                        return rc
                except Exception as e:
                    return _unknown_failed(e)       # T142：未知——专用退出
                else:
                    watch.note_ok()                 # T141：恢复（段内才告警）
                    acct_watch.note_ok()            # T184：恢复告警+清零
                    failures.note_ok()              # T142：完整成功轮清零
                time.sleep(args.interval)
        try:
            run_one_cycle(risk, gw)
        except DataUnavailableError as e:
            _poll_failed(e)
            return 1
        except NotConnectedError as e:
            # 单轮模式遇断线：与账户轮询失败同口径（告警 + rc 1），不 fatal。
            _poll_failed(e)
            append_alert(risk.live_dir, ALERT_DISCONNECTED,
                         f"risk 单轮断线：{e}")
            return 1
        return 0
    except KeyboardInterrupt:
        print("· 手动停止")
        return 0
    finally:
        gw.shutdown()


if __name__ == "__main__":
    sys.exit(main())
