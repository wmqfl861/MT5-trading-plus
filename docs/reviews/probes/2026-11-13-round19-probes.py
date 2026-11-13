"""第十九轮审核复现脚本（2026-11-13 下午，审核对象 main 7c069cb）。

用法（仓库根）：python -I docs/reviews/probes/2026-11-13-round19-probes.py

只读：用假 MT5 模块驱动真实网关代码（不 import MetaTrader5、不连终端），
复用 tests/test_demo_formal_window.py 的合成正式窗口（写临时目录）。

- P19-1 [High] 终端进程在线、只是与交易服务器断开时（terminal_info 有对象、
  connected=False），T141 重连路径仍调用 initialize；且 initialize 不带
  MT5 自身的 timeout 参数（缺省 60 秒）。11-10 03:35 三进程正是在这一步
  挂 66 秒撞 T110 的 30 秒期限 rc=75（exit-forensics §15.2）。
- P19-2 [Medium] 正式检查里没配上恢复的降级段一直计到检查时刻；段中途
  进程被重启（supervisor 或新部署）时不会截断，会把之后每天误判为
  "环境降级超限"。
- P19-3 [Info] 窗口内任何非空 .log.err 都判失败（含 Python warnings 模块
  的 FutureWarning 一类无害输出）；是否会发生取决于影子盘的 .err 内容。
"""
from __future__ import annotations

import importlib.util
import re
import sys
import tempfile
import types
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
RESULTS: list[tuple[str, str, str]] = []


def report(pid: str, status: str, detail: str) -> None:
    RESULTS.append((pid, status, detail))
    print(f"[{pid}] {status}: {detail}", flush=True)


class FakeMT5:
    """最小假 MT5：记录调用；ipc=False 表示终端 IPC 不通（terminal_info 为 None）。"""

    def __init__(self):
        self.calls = []
        self.ipc = True
        self.server_connected = False

    def initialize(self, *args, **kwargs):
        self.calls.append(("initialize", args, kwargs))
        return False if not self.ipc else True

    def terminal_info(self):
        self.calls.append(("terminal_info", (), {}))
        if not self.ipc:
            return None
        return types.SimpleNamespace(connected=self.server_connected)

    def last_error(self):
        return (-10004, "No IPC connection") if not self.ipc else (-10003, "IPC initialize failed")

    def shutdown(self):
        return True


def p19_1():
    from mt5gw.config import GatewayConfig
    from mt5gw.connection import MT5Connection
    from mt5gw.errors import NotConnectedError

    clock = [1000.0]
    fake = FakeMT5()
    conn = MT5Connection(GatewayConfig(), mt5_module=fake, monotonic=lambda: clock[0])
    conn._initialized = True            # 已在运行中：进程启动时 initialize 成功过
    fake.ipc, fake.server_connected = True, False   # 终端在线，交易服务器断开（11-10 形态）
    raised = False
    try:
        conn._ready_or_raise()
    except NotConnectedError:
        raised = True
    init_alive = [c for c in fake.calls if c[0] == "initialize"]

    # IPC 真断（终端进程不在）时允许 initialize，但必须带 MT5 timeout 且低于 T110 期限
    fake.calls.clear()
    fake.ipc = False
    conn._next_reconnect_at = 0.0
    try:
        conn._ready_or_raise()
    except NotConnectedError:
        pass
    init_dead = [c for c in fake.calls if c[0] == "initialize"]
    limit_ms = GatewayConfig().query_timeout_s * 1000
    has_timeout = bool(init_dead) and all(
        isinstance(c[2].get("timeout"), int) and c[2]["timeout"] <= limit_ms - 10_000 for c in init_dead)

    bug_alive = bool(init_alive)
    bug_timeout = bool(init_dead) and not has_timeout
    status = "REPRODUCED" if (bug_alive or bug_timeout) else "NOT-REPRODUCED"
    report("P19-1", status,
           f"终端在线但服务器断开：抛 NotConnectedError={raised}，initialize 调用 {len(init_alive)} 次"
           f"（应为 0）；IPC 不通时 initialize 调用 {len(init_dead)} 次，带 MT5 timeout 且 ≤ "
           f"{int(limit_ms - 10_000)} ms={has_timeout}（缺省 60000 ms > T110 的 {int(limit_ms)} ms 期限）")


def load_fixture():
    path = ROOT / "tests" / "test_demo_formal_window.py"
    spec = importlib.util.spec_from_file_location("r19_formal_fixture", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def p19_2():
    fx = load_fixture()
    root = Path(tempfile.mkdtemp(prefix="r19-p2-"))
    f = fx.build_evidence(root)
    t = f.start + timedelta(days=1, hours=3)
    with (f.live / "alerts.log").open("a", encoding="utf-8") as out:
        out.write(f"{t:%Y-%m-%dT%H:%M:%SZ}\tDISCONNECTED\trisk 进程与交易服务器断线（NotConnectedError: synthetic）"
                  "——降级继续\n")
    res = fx.formal(root)
    out = res.stdout + res.stderr
    over = "环境降级超限" in out
    src = (ROOT / "scripts" / "demo_check.py").read_text(encoding="utf-8")
    m = re.search(r"def _recorded_degradation\(.*?\n(?=def )", src, re.S)
    body = m.group(0) if m else ""
    truncates = bool(re.search(r"restart|started_utc|run_bindings|ops-restarts", body))
    status = "REPRODUCED" if (over and not truncates) else ("NOT-REPRODUCED" if truncates else "INFO")
    report("P19-2", status,
           f"窗口第 2 天一条未配对的 DISCONNECTED 段首 → 返回 {res.returncode}，环境降级超限={over}；"
           f"降级计算按重启/新运行截断={truncates}。段中途被 supervisor 重启或重新部署时，"
           "新进程不会补写恢复，此段会一直计到检查时刻")


def p19_3():
    src = (ROOT / "scripts" / "demo_check.py").read_text(encoding="utf-8")
    strict = "stderr 输出" in src and not re.search(r"Warning:", src)
    report("P19-3", "INFO",
           f"窗口内非空 .log.err 一律判失败（不区分 warnings 模块告警）={strict}；"
           "以影子盘三进程 .log.err 实际内容决定是否需要细分（第十九轮计划 T203）")


def main() -> int:
    p19_1()
    p19_2()
    p19_3()
    print("\n汇总：")
    for pid, status, _ in RESULTS:
        print(f"  {pid}: {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
