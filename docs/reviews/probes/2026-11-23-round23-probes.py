"""第二十三轮审核复现脚本（2026-11-23，全项目；审核对象 main ded9407）。

用法（仓库根）：python -I docs/reviews/probes/2026-11-23-round23-probes.py

只读：不 import MetaTrader5、不连终端、不起任何真实进程。只依赖标准库与
仓库自身（tests/ 下的替身）。进程表全部是合成数据；写文件只写临时目录。

- P23-1 [High] 登记的启动器已退出、其 PID 被无关进程占用（Windows 的 PID
  复用很快）。verified_tree 先比参数、后比创建时间，于是报
  "process script/arguments mismatch" 并拒绝——不是判 process_missing 去
  重启。11-19 恢复单实测到这一形态（risk 启动器的 PID 被一个 11-18 启动的
  系统进程占用）。这条拒绝在占用者退出之前每分钟重复一次。
- P23-2 [High] 终端进程不在时，网关重连会调 mt5.initialize(path)；真实的
  MetaTrader5 包这时会拉起 terminal64.exe，终端成为调用它的 worker 的子进程
  （第二十二轮 O22-7 已在生产上见到）。verified_tree 要求每个后代都通过
  _identity，终端过不了，报同一句 mismatch，并且终端活多久就拒绝多久。
- P23-3 [Medium] O23-1（4609be1、ded9407）的 conhost 跳过：
  a) 基本形态（启动器下挂一个系统 conhost 叶子）通过——修复对这一种形态有效；
  b) 叶子判断没有创建时间过滤：只要有一个进程的 parent_pid 等于 conhost 的
     PID（哪怕它比 conhost 早启动，即陈旧的父链接），conhost 就被当成非叶子，
     仍然报 mismatch；
  c) 启动器已退出、conhost 还没来得及退出的瞬间，root 缺失分支不认识
     conhost，报 "missing launcher with surviving/duplicate worker"。
- P23-4 [High] verify_deployment 每分钟跑三条 git 命令，各 15 秒超时。超时
  即 SUPERVISOR_REFUSED，同时写 halt。正式检查把 SUPERVISOR_REFUSED 归为
  异常类，窗口内出现一条就判失败。11-20 烧机日 03:40 UTC 实际发生过一次
  （git 超时，随后自愈）。
- P23-5 [High] 测试用的进程形态与生产不同：tests/ 里没有任何测试经过
  WindowsProcesses.launch()（CREATE_NO_WINDOW）或 demo_run.ps1 的
  Start-Process -WindowStyle Hidden 去拉起进程再交给 verified_tree；
  tests/ 全目录没有 "conhost" 一词；O23-1 两个提交没有改任何测试。
- P23-6 [Medium] 开仓已成交、随后的 SL/TP 修正被服务器拒绝时，执行器按
  "确定态拒单（订单确定未成交）"记 journal=rejected、写 ORDER_REJECTED
  告警并计入连拒；成交不进 deals 证据流。第十六轮 T140 评审的 F-7 记过
  这一条，定为 Info，没有立任务。
- P23-7 [Info] CI 没有静态检查一道门（ruff / mypy 都不跑）。
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
RESULTS: list[tuple[str, str, str]] = []

MISMATCH = "process script/arguments mismatch"
T0 = datetime(2026, 11, 21, 1, 0, 0)
ARGV = ["C:/prod/venv/Scripts/python.exe", "-u", "C:/prod/deploy/scripts/risk_process.py",
        "--data-dir", "C:/prod/data", "--loop"]
BASE_PY = "C:/Python312/python.exe"


def iso(t: datetime) -> str:
    return t.isoformat()


def report(pid: str, status: str, detail: str) -> None:
    RESULTS.append((pid, status, detail))
    print(f"[{pid}] {status}: {detail}", flush=True)


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def supervisor():
    return load("r23_supervisor", ROOT / "scripts" / "live_supervisor.py")


def tree_error(sup, entry, table, argv=ARGV):
    try:
        return sup.verified_tree(entry, table, argv), None
    except ValueError as exc:
        return None, str(exc)


def healthy_table():
    """启动器（venv 重定向器）→ 解释器 worker，生产上的正常两层形态。"""
    return {
        100: {"pid": 100, "parent_pid": 1, "argv": list(ARGV), "executable": ARGV[0],
              "started_utc": iso(T0)},
        101: {"pid": 101, "parent_pid": 100, "argv": [BASE_PY, *ARGV[1:]],
              "executable": BASE_PY, "started_utc": iso(T0 + timedelta(seconds=1))},
    }


def p23_1() -> None:
    sup = supervisor()
    entry = {"pid": 100, "started_utc": iso(T0)}
    clean, err0 = tree_error(sup, entry, healthy_table())
    # 启动器与 worker 都已退出；PID 100 两天后被一个无关的系统进程占用。
    table = {100: {"pid": 100, "parent_pid": 4, "argv": ["C:/Windows/System32/ctfmon.exe"],
                   "executable": "C:/Windows/System32/ctfmon.exe",
                   "started_utc": iso(T0 + timedelta(days=2))}}
    tree, err = tree_error(sup, entry, table)
    hit = err0 is None and len(clean) == 2 and err == MISMATCH
    report("P23-1", "REPRODUCED" if hit else "NOT-REPRODUCED",
           f"健康树校验通过（{len(clean or [])} 个进程）；启动器退出、PID 被两天后启动的无关进程占用 → "
           f"返回={tree!r}，报错={err!r}（期望：视为 process_missing，返回 []，由 supervisor 重启）")


def p23_2() -> None:
    sup = supervisor()
    entry = {"pid": 100, "started_utc": iso(T0)}
    table = healthy_table()
    # worker 重连时 initialize 拉起了终端：终端成为 worker 的子进程。
    table[102] = {"pid": 102, "parent_pid": 101,
                  "argv": ["C:/Program Files/MetaTrader 5/terminal64.exe"],
                  "executable": "C:/Program Files/MetaTrader 5/terminal64.exe",
                  "started_utc": iso(T0 + timedelta(minutes=24, seconds=34))}
    _, err = tree_error(sup, entry, table)

    # 网关侧：IPC 不通（terminal_info 返回 None）时是否调用 initialize。
    from mt5gw.config import GatewayConfig
    from mt5gw.connection import MT5Connection
    from mt5gw.errors import NotConnectedError

    class Info:
        connected = True

    class FakeMT5:
        def __init__(self):
            self.initialize_calls = 0
            self.terminal_alive = True

        def initialize(self, path, timeout=None):
            self.initialize_calls += 1
            self.terminal_alive = True     # 真实包：终端不在时由这一调用拉起
            return True

        def terminal_info(self):
            return Info() if self.terminal_alive else None

        def last_error(self):
            return (1, "Success")

        def shutdown(self):
            return True

        def symbol_info_tick(self, symbol):
            return {"symbol": symbol}

    fake = FakeMT5()
    # T209 之后网关在 initialize 之前先查终端进程在不在
    # （mt5gw.terminal.terminal_running，可注入）。这里把探针接到替身的
    # terminal_alive 上：替身里终端"退出"后，探针必须报不在。T209 之前的
    # MT5Connection 没有这个参数，退回原来的构造，探针在旧代码上照常复现。
    try:
        conn = MT5Connection(GatewayConfig(), mt5_module=fake,
                             terminal_running=lambda path: fake.terminal_alive)
    except TypeError:
        conn = MT5Connection(GatewayConfig(), mt5_module=fake)
    try:
        conn.connect()
    except NotConnectedError:
        pass                               # T209 之后：探测不到终端进程时连 connect 也不调 initialize
    before = fake.initialize_calls
    fake.terminal_alive = False            # 终端进程退出：IPC 不通
    try:
        conn.call(fake.symbol_info_tick, "XAUUSD")
        outcome = "调用成功"
    except NotConnectedError as exc:
        outcome = f"NotConnectedError: {exc}"
    spawned = fake.initialize_calls - before
    hit = err == MISMATCH and spawned >= 1
    report("P23-2", "REPRODUCED" if hit else "NOT-REPRODUCED",
           f"终端进程退出后的下一次网关调用里 initialize(path) 被调 {spawned} 次（{outcome}）——真实 "
           f"MetaTrader5 包这一调用会拉起 terminal64.exe 作为调用进程的子进程；"
           f"verified_tree 遇到 worker 下的 terminal64.exe 报错={err!r}")


def p23_3() -> None:
    sup = supervisor()
    entry = {"pid": 100, "started_utc": iso(T0)}
    root = Path(tempfile.mkdtemp(prefix="r23-p3-"))
    (root / "System32").mkdir()
    conhost = root / "System32" / "conhost.exe"
    conhost.write_bytes(b"")
    old = os.environ.get("SystemRoot")
    os.environ["SystemRoot"] = str(root)   # 让 O23-1 的系统路径比较在任何平台都可执行
    try:
        table = healthy_table()
        table[102] = {"pid": 102, "parent_pid": 100, "argv": [str(conhost), "0x4"],
                      "executable": str(conhost), "started_utc": iso(T0)}
        tree_a, err_a = tree_error(sup, entry, table)
        ok_a = err_a is None and [p["pid"] for p in tree_a] == [101, 100]
        report("P23-3a", "NOT-REPRODUCED" if ok_a else "REPRODUCED",
               f"启动器下挂系统 conhost 叶子 → 返回 pid={[p['pid'] for p in (tree_a or [])]}，报错={err_a!r}"
               "（NOT-REPRODUCED = O23-1 对这一形态有效；conhost 不进终止清单）")

        # b) 陈旧父链接：进程 103 比 conhost 早一天启动，其记录的父 PID 恰好是 102。
        table_b = dict(table)
        table_b[103] = {"pid": 103, "parent_pid": 102, "argv": ["C:/Windows/System32/ctfmon.exe"],
                        "executable": "C:/Windows/System32/ctfmon.exe",
                        "started_utc": iso(T0 - timedelta(days=1))}
        _, err_b = tree_error(sup, entry, table_b)
        report("P23-3b", "REPRODUCED" if err_b == MISMATCH else "NOT-REPRODUCED",
               f"另有一个早一天启动的无关进程，其 parent_pid 等于 conhost 的 PID → 报错={err_b!r}"
               "（期望：按创建时间排除陈旧父链接，conhost 仍算叶子）")

        # c) 启动器与 worker 已退出，conhost 尚未退出的瞬间。
        table_c = {102: table[102]}
        tree_c, err_c = tree_error(sup, entry, table_c)
        hit_c = err_c == "missing launcher with surviving/duplicate worker"
        report("P23-3c", "REPRODUCED" if hit_c else "NOT-REPRODUCED",
               f"启动器已退出、只剩它的 conhost → 返回={tree_c!r}，报错={err_c!r}"
               "（期望：不把系统 conhost 当成存活 worker）")
    finally:
        if old is None:
            os.environ.pop("SystemRoot", None)
        else:
            os.environ["SystemRoot"] = old


def p23_4() -> None:
    fx = load("r23_supervisor_fixture", ROOT / "tests" / "test_t110_supervisor.py")
    tmp = Path(tempfile.mkdtemp(prefix="r23-p4-"))
    mod, cfg, processes, live, _ = fx.setup(tmp)

    def git_timeout(_cfg):
        raise subprocess.TimeoutExpired(cmd=["git", "-C", "<deploy>", "status"], timeout=15)

    result = mod.supervise(cfg, processes, now=fx.NOW, verify_deployment=git_timeout)
    halt = json.loads((live / "halt.json").read_text(encoding="utf-8")) if (live / "halt.json").exists() else {}
    alerts = (live / "alerts.log").read_text(encoding="utf-8") if (live / "alerts.log").exists() else ""
    check = load("r23_demo_check", ROOT / "scripts" / "demo_check.py")
    anomaly = "SUPERVISOR_REFUSED" in check.FORMAL_ANOMALY_ALERTS
    hit = (result.get("refused") is True and halt.get("reason") == "supervisor_refused"
           and "SUPERVISOR_REFUSED" in alerts and anomaly
           and not processes.started and not processes.killed)
    report("P23-4", "REPRODUCED" if hit else "NOT-REPRODUCED",
           f"四个角色全部健康，仅 verify_deployment 的 git 调用超时 → refused={result.get('refused')}，"
           f"reason={result.get('reason')!r}，halt.reason={halt.get('reason')!r}，"
           f"告警含 SUPERVISOR_REFUSED={'SUPERVISOR_REFUSED' in alerts}；"
           f"正式检查把 SUPERVISOR_REFUSED 归异常类={anomaly}（窗口内一条即判失败）")


def p23_5() -> None:
    tests = ROOT / "tests"
    sup_test = (tests / "test_t110_supervisor.py").read_text(encoding="utf-8")
    uses_launch = bool(re.search(r"WindowsProcesses\(\)\.launch\(|backend\.launch\(", sup_test))
    popen_direct = len(re.findall(r"subprocess\.Popen\(", sup_test))
    conhost_files = [p.name for p in sorted(tests.glob("*.py"))
                     if "conhost" in p.read_text(encoding="utf-8", errors="ignore").lower()]
    src = (ROOT / "scripts" / "live_supervisor.py").read_text(encoding="utf-8")
    ps1 = (ROOT / "scripts" / "demo_run.ps1").read_text(encoding="utf-8", errors="ignore")
    prod_paths = (bool(re.search(r"creationflags\s*=\s*subprocess\.CREATE_NO_WINDOW", src)),
                  "-WindowStyle Hidden" in ps1)
    try:
        stat = subprocess.run(["git", "-C", str(ROOT), "diff", "--stat", "70cd7eb", "ded9407"],
                              capture_output=True, text=True, timeout=30)
        changed = [ln.split("|")[0].strip() for ln in stat.stdout.splitlines() if "|" in ln]
        diff_note = f"70cd7eb..ded9407 改动文件={changed}" if stat.returncode == 0 else "历史不足，未核对提交改动面"
    except (OSError, subprocess.SubprocessError):
        diff_note = "git 不可用，未核对提交改动面"
    hit = (not uses_launch) and (not conhost_files) and all(prod_paths)
    report("P23-5", "REPRODUCED" if hit else "NOT-REPRODUCED",
           f"生产启动路径：supervisor 用 CREATE_NO_WINDOW={prod_paths[0]}、demo_run.ps1 用 "
           f"-WindowStyle Hidden={prod_paths[1]}；supervisor 测试经过 launch()={uses_launch}"
           f"（测试自己用 subprocess.Popen 起进程 {popen_direct} 处）；tests/ 含 conhost 的文件={conhost_files}；"
           f"{diff_note}")


def p23_6() -> None:
    from mt5gw.errors import TradeRejectedError
    from mt5quant.strategies.base import ExitRules
    from mt5live.executor import ExecutionAdapter, LiveExecutor
    from tests.fakes_live import FakeBroker, mk_decision

    magic = 777
    live = Path(tempfile.mkdtemp(prefix="r23-p6-"))
    b = FakeBroker()
    b.fill_price = b.ask + 0.5             # 成交价偏离参考价 → 触发 SL/TP 修正

    def reject_modify(ticket, *, sl=None, tp=None):
        raise TradeRejectedError(10016, "Invalid stops")

    b.modify_position = reject_modify
    ex = LiveExecutor(ExecutionAdapter(b, b, b, symbol=b.symbol, magic=magic),
                      symbol=b.symbol, magic=magic, live_dir=str(live),
                      rules=ExitRules(stop_loss_atr=1.5))
    d = mk_decision(decision_id="XAUUSD|M15|2026-10-20T18:00:00", signal=1, volume_lots=0.02)
    try:
        rr = ex.poll(d, datetime(2026, 10, 20, 18, 0))
        action, raised = rr.action, None
    except Exception as exc:               # 复现脚本如实报告
        action, raised = None, f"{type(exc).__name__}: {exc}"
    positions = [p for p in b.positions(b.symbol) if p.magic == magic]
    journal = live / f"execution-journal-{magic}.jsonl"
    statuses = [json.loads(ln).get("status") for ln in journal.read_text(encoding="utf-8").splitlines()
                if ln.strip()] if journal.exists() else []
    alerts = (live / "alerts.log").read_text(encoding="utf-8") if (live / "alerts.log").exists() else ""
    deals = live / f"deals-{magic}.jsonl"
    deal_rows = [ln for ln in deals.read_text(encoding="utf-8").splitlines() if ln.strip()] if deals.exists() else []
    hit = (raised is None and action == "rejected" and len(positions) == 1
           and statuses[-1:] == ["rejected"] and "ORDER_REJECTED" in alerts and not deal_rows)
    report("P23-6", "REPRODUCED" if hit else "NOT-REPRODUCED",
           f"开仓成交后 SL/TP 修正被拒（10016）→ 回报 action={action!r}（异常={raised!r}），"
           f"券商侧该 magic 持仓 {len(positions)} 笔，journal 状态序列={statuses}，"
           f"告警含 ORDER_REJECTED={'ORDER_REJECTED' in alerts}，deals 证据流 {len(deal_rows)} 行"
           "（期望：成交如实入 journal 与 deals，SL/TP 修正失败单独告警）")


def p23_7() -> None:
    wf = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    has_lint = bool(re.search(r"\bruff\b|\bflake8\b|\bmypy\b|\bpyright\b", wf))
    has_cfg = bool(re.search(r"\[tool\.(ruff|mypy)", pyproject))
    report("P23-7", "INFO",
           f"tests.yml 含静态检查步骤={has_lint}；pyproject.toml 含 ruff/mypy 配置={has_cfg}")


def main() -> int:
    for fn in (p23_1, p23_2, p23_3, p23_4, p23_5, p23_6, p23_7):
        try:
            fn()
        except Exception as exc:  # 复现脚本自身出错也如实报告
            report(fn.__name__.upper().replace("_", "-"), "ERROR", f"{type(exc).__name__}: {exc}")
    print("\n汇总：")
    for pid, status, _ in RESULTS:
        print(f"  {pid}: {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
