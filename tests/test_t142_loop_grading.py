"""T142（第十六轮计划 §2，O16-3）：风控、对账进程的异常分级与取证。

- 主循环异常分两类：
  - 已知可恢复（数据暂缺 DataUnavailableError、文件被占用 PermissionError/
    锁等待 TimeoutError、断线 NotConnectedError）：记录后继续；
  - 连续 5 轮失败或任何未知异常：写退出标记 loop-exit-<component>.json
    （reason=unrecoverable_loop/<具体>）+ halt（fail-closed：监视者离开
    必须停交易）后以**专用退出码 79** 退出。
- 与 T141 的 900s/78 契约边界（钉死）：断线只走 DisconnectWatch 的 15 分钟
  时间窗——**不进 5 轮计数、断线降级轮也不清零计数**（统一计数会让
  5 轮 × interval 早于 900s 触发，破坏 T141 验收场景"断线 3 分钟恢复
  三进程不退出"）；只有完整成功的轮清零计数。
- 计划逐字 TDD 场景：
  1) 注入 PermissionError（11-01 签名：read_heartbeat_path 读取心跳时
     未捕获的 [Errno 13]）→ 风控不退出（可恢复类，记录后继续）；
  2) 连续 5 次 → 退出（专用码 79）并留下标记。

对账进程同口径：run_once 吞掉的 rc=2（意图文件缺失/不合法、持仓查询
失败——"数据暂缺"）按可恢复失败计数；rc=1（按规则写 pause）是动作不是
故障，视为成功轮；run_once 直接抛出的可恢复异常同风控分级。
"""
import time
from datetime import datetime
from pathlib import Path

from tests.test_t141_disconnect import (  # 复用 T141 已落地的测试脚手架
    RISK_TOML,
    SESSIONS_TOML,
    _DownGateway,
    _alerts_text,
    _fake_datetime,
    _load_script,
    _read_json,
    _write,
)
from tests.fakes_live import FakeBroker

REPO_ROOT = Path(__file__).resolve().parents[1]
MAGIC = 779
LOOP_EXIT_RC = 79
LOOP_FAILURE_LIMIT = 5


def _write_intent_ok(live: Path, *, volume: float = 0.0) -> None:
    """写一份已确认意图（volume=0 + 无持仓 → 对账一致；volume>0 → 差分）。"""
    from mt5live.reconcile import Intent, write_intent

    write_intent(str(live / f"expected-{MAGIC}.json"), Intent(
        symbol="XAUUSD", magic=MAGIC, side="buy", volume=volume,
        status="confirmed", cause="t142", ts=datetime(2026, 11, 9, 12, 0, 0)))


def _keep_runner_alive(live_dir, virt):
    from mt5live.monitor import write_heartbeat
    write_heartbeat(live_dir, True, ts=virt.wall)


class _PermHeartbeat:
    """11-01 签名注入器：read_heartbeat_path 在开关打开时抛 PermissionError。"""

    def __init__(self, monkeypatch):
        import mt5live.riskproc as riskproc

        self.flag = {"on": False}
        real = riskproc.read_heartbeat_path

        def fake(path):
            if self.flag["on"]:
                raise PermissionError(13, "Permission denied", path)
            return real(path)

        monkeypatch.setattr(riskproc, "read_heartbeat_path", fake)


def _run_risk_loop(tmp_path, monkeypatch, gw, virt, fake_sleep):
    mod = _load_script("risk_process.py")
    live = tmp_path / "live"
    risk_toml = _write(tmp_path / "risk.toml", RISK_TOML)
    monkeypatch.setattr(mod, "_open_gateway", lambda cfg: gw)
    monkeypatch.setattr(mod, "datetime", _fake_datetime(virt))
    monkeypatch.setattr(mod, "_mono", virt.mono_fn)
    monkeypatch.setattr(mod.time, "sleep", fake_sleep)
    _keep_runner_alive(str(live), virt)          # 建目录 + 新鲜心跳（第 1 轮前）
    return mod.main(["--config", risk_toml, "--data-dir", str(tmp_path),
                     "--loop", "--interval", "30",
                     "--sessions-config",
                     _write(tmp_path / "sessions.toml", SESSIONS_TOML)]), live


def _prep_reconcile(tmp_path, monkeypatch, gw, virt, fake_sleep):
    """加载对账脚本并注入替身；返回 (mod, live)。需要 patch mod 属性
    （如 write_pause）的测试用本函数拿同一个模块实例，避免二次加载。"""
    mod = _load_script("live_reconcile.py")
    live = tmp_path / "live"
    live.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mod, "_open_gateway", lambda cfg: gw)
    monkeypatch.setattr(mod, "datetime", _fake_datetime(virt))
    monkeypatch.setattr(mod, "_mono", virt.mono_fn)
    monkeypatch.setattr(mod.time, "sleep", fake_sleep)
    return mod, live


def _run_reconcile(mod, tmp_path):
    return mod.main(["--data-dir", str(tmp_path), "--loop", "--interval", "30"])


# ------------------------------------------------ 常量与码表（supervisor 汇总）

def test_t142_constants_and_code_table():
    """专用码 79、5 轮上限；与既有码 0/1/2/3/75/78 全部可区分。"""
    from mt5live.monitor import (
        DISCONNECT_EXIT_RC,
        LOOP_EXIT_RC,
        LOOP_FAILURE_LIMIT,
        LOOP_EXIT_FILE_FMT,
    )

    assert LOOP_EXIT_RC == 79
    assert LOOP_FAILURE_LIMIT == 5
    assert len({0, 1, 2, 3, 75, DISCONNECT_EXIT_RC, LOOP_EXIT_RC}) == 7
    assert LOOP_EXIT_FILE_FMT == "loop-exit-{component}.json"


# --------------------------------------------- LoopFailureWatch 单元（monitor）

def test_t142_watch_counts_resets_and_marks_streak_start(tmp_path):
    """连续失败计数：第 1 轮写 RECOVERABLE_FAILURE 告警（不刷屏）、恰好
    第 5 轮 expired；note_ok 清零；告警写失败不推翻计数。"""
    from mt5live.monitor import LoopFailureWatch

    watch = LoopFailureWatch(str(tmp_path), "risk")
    assert not watch.expired()
    watch.note_failure(PermissionError(13, "Permission denied", "x"))
    assert watch.consecutive_failures == 1
    watch.note_failure(PermissionError(13, "again"))
    assert watch.consecutive_failures == 2
    watch.note_ok()
    assert watch.consecutive_failures == 0
    for _ in range(5):
        watch.note_failure("PermissionError: 演练")
    assert watch.expired()
    alerts = _alerts_text(tmp_path)
    # 两个失败段（1+2 后清零、再 5 轮）各在段首告警一次，段内不刷屏
    assert alerts.count("RECOVERABLE_FAILURE") == 2


def test_t142_watch_alert_failure_does_not_break_counting(
        tmp_path, monkeypatch):
    """文件系统连告警都写不出（被占用）时：计数照常，退出标记仍尽力落盘。"""
    import mt5live.monitor as monitor

    def broken_append(*a, **k):
        raise PermissionError(13, "alerts 被占用")

    monkeypatch.setattr(monitor, "append_alert", broken_append)
    watch = monitor.LoopFailureWatch(str(tmp_path), "risk")
    for _ in range(5):
        watch.note_failure("PermissionError: 演练")
    rc = watch.exit_code(detail="PermissionError: 演练",
                         cause="consecutive_failures")
    assert rc == monitor.LOOP_EXIT_RC
    marker = _read_json(tmp_path / "loop-exit-risk.json")   # 标记仍写出
    assert marker["rc"] == monitor.LOOP_EXIT_RC


def test_t142_watch_exit_code_writes_marker_halt_and_returns_79(tmp_path):
    """exit_code：loop-exit-<component>.json（reason=unrecoverable_loop/<具体>、
    cause、轮数、上限）+ halt（fail-closed）+ LOOP_EXIT 告警，返回 79。"""
    from mt5live.monitor import LoopFailureWatch

    watch = LoopFailureWatch(str(tmp_path), "risk")
    for _ in range(5):
        watch.note_failure("PermissionError: [Errno 13] 演练")
    rc = watch.exit_code(detail="PermissionError: [Errno 13] 演练",
                         cause="consecutive_failures")
    assert rc == LOOP_EXIT_RC
    marker = _read_json(tmp_path / "loop-exit-risk.json")
    assert marker["component"] == "risk"
    assert marker["rc"] == LOOP_EXIT_RC
    assert marker["reason"].startswith("unrecoverable_loop/PermissionError")
    assert marker["cause"] == "consecutive_failures"
    assert marker["consecutive_failures"] == LOOP_FAILURE_LIMIT
    assert marker["limit_rounds"] == LOOP_FAILURE_LIMIT
    assert marker["first_failure_utc"]
    assert marker["ts"] and marker["pid"]
    halt = _read_json(tmp_path / "halt.json")
    assert "unrecoverable_loop" in halt["reason"]
    assert "LOOP_EXIT" in _alerts_text(tmp_path)


def test_t142_watch_unknown_exception_exit(tmp_path):
    """未知异常路径：不等待 5 轮，首轮即退；cause=unknown_exception。"""
    from mt5live.monitor import LoopFailureWatch

    watch = LoopFailureWatch(str(tmp_path), "reconcile")
    rc = watch.exit_code(exc=ValueError("未知演练"),
                         cause="unknown_exception")
    assert rc == LOOP_EXIT_RC
    marker = _read_json(tmp_path / "loop-exit-reconcile.json")
    assert marker["cause"] == "unknown_exception"
    assert marker["reason"].startswith("unrecoverable_loop/ValueError")
    assert marker["consecutive_failures"] == 0
    assert (tmp_path / "halt.json").exists()


# ------------------------------------- 风控主循环（计划逐字 TDD 场景 1、2）

def test_t142_risk_permissionerror_survives(tmp_path, monkeypatch, capsys):
    """计划场景 1：注入 PermissionError（11-01 签名）4 轮 → 风控不退出
    （记录后继续），恢复后照常；无退出标记、无 halt。"""
    from tests.test_t141_disconnect import VirtClock

    perm = _PermHeartbeat(monkeypatch)
    perm.flag["on"] = True                       # 第 1 轮起文件被占用
    gw = _DownGateway(FakeBroker("XAUUSD"))
    virt = VirtClock(datetime(2026, 11, 9, 12, 0, 0))

    real_sleep = time.sleep
    step = {"n": 0}

    def fake_sleep(s):
        step["n"] += 1
        virt.advance(30)
        _keep_runner_alive(str(tmp_path / "live"), virt)
        if step["n"] == 4:                       # 4 轮失败后文件释放
            perm.flag["on"] = False
        elif step["n"] >= 7:                     # 恢复后跑满健康轮
            raise KeyboardInterrupt
        real_sleep(0)

    rc, live = _run_risk_loop(tmp_path, monkeypatch, gw, virt, fake_sleep)
    assert rc == 0                               # 不退出（手动停止收尾）
    assert not (live / "loop-exit-risk.json").exists()
    assert not (live / "halt.json").exists()
    assert (live / "heartbeat-risk.json").exists()   # 角色心跳持续（降级照写）
    assert "RECOVERABLE_FAILURE" in _alerts_text(live)
    out = capsys.readouterr().out
    assert out.count("可恢复") >= 1               # 每轮失败都有留痕


def test_t142_risk_5_consecutive_permissionerror_exits(tmp_path, monkeypatch,
                                                       capsys):
    """计划场景 2：PermissionError 连续 5 轮 → 专用码 79 退出 +
    loop-exit-risk.json（reason=unrecoverable_loop/…）+ halt（fail-closed）
    + exit-risk.json（guarded_main 落 rc=79）。"""
    from tests.test_t141_disconnect import VirtClock

    perm = _PermHeartbeat(monkeypatch)
    perm.flag["on"] = True                       # 一直占用
    gw = _DownGateway(FakeBroker("XAUUSD"))
    virt = VirtClock(datetime(2026, 11, 9, 12, 0, 0))

    real_sleep = time.sleep
    step = {"n": 0}

    def fake_sleep(s):
        step["n"] += 1
        virt.advance(30)
        _keep_runner_alive(str(tmp_path / "live"), virt)
        real_sleep(0)

    rc, live = _run_risk_loop(tmp_path, monkeypatch, gw, virt, fake_sleep)
    assert rc == LOOP_EXIT_RC
    marker = _read_json(live / "loop-exit-risk.json")
    assert marker["rc"] == LOOP_EXIT_RC
    assert marker["cause"] == "consecutive_failures"
    assert marker["consecutive_failures"] == LOOP_FAILURE_LIMIT
    assert marker["reason"].startswith("unrecoverable_loop/PermissionError")
    assert _read_json(live / "exit-risk.json")["rc"] == LOOP_EXIT_RC
    assert (live / "halt.json").exists()         # 监视者离开 → 停交易
    assert "LOOP_EXIT" in _alerts_text(live)


def test_t142_risk_unknown_exception_immediate_exit(tmp_path, monkeypatch):
    """未知异常（非可恢复类）：首轮即写标记并以 79 退出（不再落到 rc=2）。"""
    import mt5live.riskproc as riskproc
    from tests.test_t141_disconnect import VirtClock

    def broken(path):
        raise ValueError("未知异常演练")

    monkeypatch.setattr(riskproc, "read_heartbeat_path", broken)
    gw = _DownGateway(FakeBroker("XAUUSD"))
    virt = VirtClock(datetime(2026, 11, 9, 12, 0, 0))

    def fake_sleep(s):
        virt.advance(30)
        _keep_runner_alive(str(tmp_path / "live"), virt)

    rc, live = _run_risk_loop(tmp_path, monkeypatch, gw, virt, fake_sleep)
    assert rc == LOOP_EXIT_RC
    marker = _read_json(live / "loop-exit-risk.json")
    assert marker["cause"] == "unknown_exception"
    assert marker["reason"].startswith("unrecoverable_loop/ValueError")
    assert (live / "halt.json").exists()


def test_t142_risk_disconnect_not_counted_boundary(tmp_path, monkeypatch,
                                                   capsys):
    """T141 边界钉死：断线轮既不进 5 轮计数也不清零；只有完整成功轮清零。

    序列：4×PermissionError（计数=4）→ 3×断线降级轮（计数不变，未满
    900s 不走 78）→ 1×完整成功（清零）→ 4×PermissionError（计数=4，
    不退出）→ 恢复 → 手动停止。"""
    from tests.test_t141_disconnect import VirtClock

    perm = _PermHeartbeat(monkeypatch)
    perm.flag["on"] = True                       # 第 1-4 轮：文件被占用
    gw = _DownGateway(FakeBroker("XAUUSD"))
    virt = VirtClock(datetime(2026, 11, 9, 12, 0, 0))

    real_sleep = time.sleep
    step = {"n": 0}

    def fake_sleep(s):
        step["n"] += 1
        virt.advance(30)
        _keep_runner_alive(str(tmp_path / "live"), virt)
        if step["n"] == 4:                       # 4 轮失败后转为断线 3 轮
            perm.flag["on"] = False
            gw.state["down"] = True
        elif step["n"] == 7:                     # 断线恢复 → 完整成功轮
            gw.state["down"] = False
        elif step["n"] == 8:                     # 成功后再 4 轮失败（<5）
            perm.flag["on"] = True
        elif step["n"] == 12:
            perm.flag["on"] = False
        elif step["n"] >= 14:
            raise KeyboardInterrupt
        real_sleep(0)

    rc, live = _run_risk_loop(tmp_path, monkeypatch, gw, virt, fake_sleep)
    assert rc == 0
    assert not (live / "loop-exit-risk.json").exists()   # 4+4 都未达 5
    assert not (live / "disconnect-exit-risk.json").exists()  # 90s < 900s
    alerts = _alerts_text(live)
    assert "DISCONNECTED" in alerts and "恢复" in alerts
    assert "RECOVERABLE_FAILURE" in alerts


# ------------------------------------------------ 对账主循环（同口径分级）

def test_t142_reconcile_rc2_streak_exits(tmp_path, monkeypatch):
    """对账：意图文件缺失（run_once rc=2，"数据暂缺"）连续 5 轮 →
    79 + loop-exit-reconcile.json + halt（此前是无限等待）。"""
    from tests.test_t141_disconnect import VirtClock

    gw = _DownGateway(FakeBroker("XAUUSD"))     # live 目录空 → rc=2 每轮
    virt = VirtClock(datetime(2026, 11, 9, 12, 0, 0))

    real_sleep = time.sleep
    step = {"n": 0}

    def fake_sleep(s):
        step["n"] += 1
        virt.advance(30)
        if step["n"] >= 8:                       # 兜底：现状是无限等待
            raise KeyboardInterrupt
        real_sleep(0)

    mod, live = _prep_reconcile(tmp_path, monkeypatch, gw, virt, fake_sleep)
    rc = _run_reconcile(mod, tmp_path)
    assert rc == LOOP_EXIT_RC
    marker = _read_json(live / "loop-exit-reconcile.json")
    assert marker["cause"] == "consecutive_failures"
    assert marker["consecutive_failures"] == LOOP_FAILURE_LIMIT
    assert marker["reason"].startswith("unrecoverable_loop/")
    assert marker["rc"] == LOOP_EXIT_RC
    assert _read_json(live / "exit-reconcile.json")["rc"] == LOOP_EXIT_RC
    assert (live / "halt.json").exists()
    assert "LOOP_EXIT" in _alerts_text(live)


def test_t142_reconcile_permissionerror_survives(tmp_path, monkeypatch):
    """对账：write_pause 抛 PermissionError（文件被占用）3 轮 → 不退出，
    恢复后照常写 pause；无标记、无 halt。"""
    from tests.test_t141_disconnect import VirtClock

    gw = _DownGateway(FakeBroker("XAUUSD"))
    virt = VirtClock(datetime(2026, 11, 9, 12, 0, 0))

    real_sleep = time.sleep
    step = {"n": 0}
    flag = {"on": True}

    def fake_sleep(s):
        step["n"] += 1
        virt.advance(30)
        if step["n"] == 3:
            flag["on"] = False
        elif step["n"] >= 6:
            raise KeyboardInterrupt
        real_sleep(0)

    mod, live = _prep_reconcile(tmp_path, monkeypatch, gw, virt, fake_sleep)
    _write_intent_ok(live)
    real_pause = mod.write_pause

    def fake_pause(*a, **k):
        if flag["on"]:
            raise PermissionError(13, "Permission denied", "pause")
        return real_pause(*a, **k)

    monkeypatch.setattr(mod, "write_pause", fake_pause)

    rc = _run_reconcile(mod, tmp_path)
    assert rc == 0
    assert not (live / "loop-exit-reconcile.json").exists()
    assert not (live / "halt.json").exists()
    assert _read_json(live / f"pause-{MAGIC}.json")["paused"] is False
    assert "RECOVERABLE_FAILURE" in _alerts_text(live)


def test_t142_reconcile_unknown_exception_exits(tmp_path, monkeypatch):
    """对账：未知异常（RuntimeError）→ 首轮即 79 + 标记 + halt。"""
    from tests.test_t141_disconnect import VirtClock

    gw = _DownGateway(FakeBroker("XAUUSD"))
    virt = VirtClock(datetime(2026, 11, 9, 12, 0, 0))

    real_sleep = time.sleep
    step = {"n": 0}

    def fake_sleep(s):
        step["n"] += 1
        virt.advance(30)
        if step["n"] >= 4:                       # 兜底：防回归时死循环
            raise KeyboardInterrupt
        real_sleep(0)

    mod, live = _prep_reconcile(tmp_path, monkeypatch, gw, virt, fake_sleep)
    _write_intent_ok(live)

    def broken_pause(*a, **k):
        raise RuntimeError("未知异常演练")

    monkeypatch.setattr(mod, "write_pause", broken_pause)

    rc = _run_reconcile(mod, tmp_path)
    assert rc == LOOP_EXIT_RC
    marker = _read_json(live / "loop-exit-reconcile.json")
    assert marker["cause"] == "unknown_exception"
    assert marker["reason"].startswith("unrecoverable_loop/RuntimeError")
    assert (live / "halt.json").exists()


def test_t142_reconcile_mismatch_round_is_action_not_failure(
        tmp_path, monkeypatch):
    """rc=1（对账不一致 → 按规则写 pause）是动作不是故障：连续 7 轮差分
    不触发 5 轮计数退出。"""
    from tests.test_t141_disconnect import VirtClock

    gw = _DownGateway(FakeBroker("XAUUSD"))
    virt = VirtClock(datetime(2026, 11, 9, 12, 0, 0))

    real_sleep = time.sleep
    step = {"n": 0}

    def fake_sleep(s):
        step["n"] += 1
        virt.advance(30)
        if step["n"] >= 7:
            raise KeyboardInterrupt
        real_sleep(0)

    mod, live = _prep_reconcile(tmp_path, monkeypatch, gw, virt, fake_sleep)
    _write_intent_ok(live, volume=0.01)          # 期望 0.01 手，实际无持仓

    rc = _run_reconcile(mod, tmp_path)
    assert rc == 0
    assert not (live / "loop-exit-reconcile.json").exists()
    assert _read_json(live / f"pause-{MAGIC}.json")["paused"] is True
