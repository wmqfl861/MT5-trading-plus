"""T209（第二十三轮施工计划 §2.2）：进程启动时交易终端可能还没起来；终端不允许算法交易时不开仓。

- wait_for_gateway：启动时连不上网关，按断线处理（T141）——每轮写心跳、段首一条
  DISCONNECTED 告警、连续 15 分钟以退出码 78 结束；不写 fatal halt。全灭之后终端
  由看护任务拉起，进程自己等到那一刻，不需要人工清 halt。
- AlgoTradingWatch：每个决策轮读一次 terminal_info()。终端不允许算法交易时本轮
  不开仓；状态变化时各发一条告警。
"""
from __future__ import annotations

import time
from datetime import datetime

from mt5gw.errors import NotConnectedError
from mt5live.monitor import DisconnectWatch, append_alert, iso_utc

ALERT_ALGO_TRADING_DISABLED = "ALGO_TRADING_DISABLED"     # 正式检查：异常类
ALERT_ALGO_TRADING_RESTORED = "ALGO_TRADING_RESTORED"     # 正式检查：记录类
STARTUP_RETRY_S = 5.0
ATTEMPT_MARGIN = 2                 # 次数上限比"上限时长 / 间隔"多出来的余量


def wait_for_gateway(gw, live_dir: str, component: str, heartbeat, *,
                     retry_s: float = STARTUP_RETRY_S, clock=None, sleep=None,
                     log=print) -> int | None:
    """连上网关。连不上就按断线处理（T141），不是 fatal。

    每次尝试之间写一次心跳（supervisor 据此知道进程还活着，不会把它重启）、
    打一行日志（supervisor 要求日志新鲜）；段首一条 DISCONNECTED 告警，连上时
    一条恢复告警。连续 DisconnectWatch 的上限（15 分钟）仍连不上，返回它的专用
    退出码（78，带退出标记），由调用方直接 return。已连上返回 None。

    只把 NotConnectedError 当作"终端还没起来"：其余异常照旧上抛，调用方的
    启动失败路径不变。第一次就连上时不写任何告警。
    除了时钟，还有次数上限（上限时长 / 间隔 + 2 次）：时钟不走时也会以同一个
    退出码停下来。生产时钟会走，到不了次数上限。
    sleep 缺省在调用时取 time.sleep（测试替换它才生效）。
    """
    sleep = sleep or time.sleep
    watch = None
    attempts = 0
    while True:
        attempts += 1
        try:
            gw.connect()
        except NotConnectedError as exc:
            if watch is None:
                watch = DisconnectWatch(live_dir, component, clock=clock) if clock else DisconnectWatch(live_dir, component)
            watch.note_failure(exc)
            try:
                heartbeat()
            except Exception as nested:         # 心跳写不进去不改变等待；告警已记录断线
                log(f"等待交易终端期间心跳写入失败（继续等待）：{type(nested).__name__}: {nested}")
            log(f"[{iso_utc(datetime.now())}] 等待交易终端（已 {watch.disconnected_s:.0f}/"
                f"{watch.limit_s:.0f} 秒）：{type(exc).__name__}: {exc}")
            if watch.expired():
                return watch.exit_code(last_error=f"{type(exc).__name__}: {exc}")
            if attempts > int(watch.limit_s / max(retry_s, 1e-3)) + ATTEMPT_MARGIN:
                return watch.exit_code(last_error=f"{type(exc).__name__}: {exc}（时钟不走，按次数上限停止）")
            sleep(retry_s)
        else:
            if watch is not None:
                watch.note_ok()
            return None


class AlgoTradingWatch:
    """每个决策轮读一次 terminal_info()。"""

    def __init__(self, gw, live_dir: str):
        self._gw = gw
        self._live_dir = live_dir
        self._allowed: bool | None = None      # None = 还没有读到过终端的状态

    def allowed(self, now: datetime) -> bool:
        """本决策轮是否允许开仓。False = 终端不允许算法交易。

        网关必须有 terminal_info()（MT5Gateway 有）。没有就是接错了网关，照旧抛
        AttributeError——不为没有这个方法的替身放行，否则方法一旦被改名或删掉，
        这道检查会无声地消失。
        终端 IPC 不通时抛 NotConnectedError：交给调用方的断线分支（本轮同样不开仓），
        不把它误报成"算法交易被关"。
        """
        try:
            info = self._gw.terminal_info()
        except (NotConnectedError, AttributeError):
            raise                                   # 断线交给调用方的断线分支；网关没有这个方法是接错了网关，照旧上抛
        except Exception as exc:                    # 其余异常也按 IPC 不通处理，带上原来的类名
            raise NotConnectedError(f"terminal_info 读取失败（{type(exc).__name__}）：终端 IPC 不通") from exc
        if info is None:
            raise NotConnectedError("terminal_info 无返回：终端 IPC 不通")
        allowed = (bool(getattr(info, "trade_allowed", False))
                   and not bool(getattr(info, "tradeapi_disabled", True)))
        if allowed != self._allowed:
            if not allowed:
                append_alert(self._live_dir, ALERT_ALGO_TRADING_DISABLED,
                             "终端不允许算法交易（trade_allowed=False 或 tradeapi_disabled=True）：本轮不开仓", now)
            elif self._allowed is False:       # 只在"观察到关闭之后"才报恢复；启动时本来就允许的不报
                append_alert(self._live_dir, ALERT_ALGO_TRADING_RESTORED,
                             "终端恢复允许算法交易（trade_allowed=True 且 tradeapi_disabled=False）", now)
            self._allowed = allowed
        return allowed
