"""T184（第十七轮计划 §T184，O17-4）：账户取数失败按时间窗计。

- `DataUnavailableError` 不再计入 T142 的 5 轮上限（风控 `--interval 10`
  下 5 轮 ≈ 50 秒，终端一抖就把交易 halt 到人工解除——O17-4/P17-4），
  改用独立计时：连续 `--account-unavailable-limit-s`（缺省 300 秒）才以
  79 退出并写 halt（halt 语义不变）；`PermissionError`/`TimeoutError`
  仍按 5 轮计；
- 首次失败写一条告警、恢复时再写一条、中间不刷屏（与 T141 DisconnectWatch
  同一做法）；对账进程的持仓查询失败同样处理；
- 三套计时互不干扰（边界钉死）：账户时间窗（300s/79+halt）、T141 断线
  15 分钟窗（900s/78 不写 halt）、T142 文件访问类 5 轮计数（79+halt）；
  断线轮对其他两套计时中性（不 note_ok 不 note_failure），只有完整成功
  的轮清零；
- 计划逐字 TDD 场景：
  1) 失败 60 秒后恢复 → 不退出，不写 halt；
  2) 连续 6 分钟失败 → 以 79 退出，写 halt；
  3) 断线仍只走 T141 的 15 分钟时间窗（不误触发本计时）；
  4) P17-4 变为 NOT-REPRODUCED。
"""
import importlib.util
import time
from datetime import datetime
from pathlib import Path

from mt5gw.errors import DataUnavailableError

from tests.fakes_live import FakeBroker
from tests.test_t141_disconnect import (  # 复用 T141/T142 已落地的测试脚手架
    DISCONNECT_EXIT_RC,
    RISK_TOML,
    SESSIONS_TOML,
    _DownGateway,
    _alerts_text,
    _fake_datetime,
    _load_script,
    _read_json,
    _write,
)
from tests.test_t142_loop_grading import _PermHeartbeat, _write_intent_ok

REPO_ROOT = Path(__file__).resolve().parents[1]
MAGIC = 779
LOOP_EXIT_RC = 79
ACCOUNT_UNAVAILABLE_LIMIT_S = 300.0


def _keep_runner_alive(live_dir, virt):
    from mt5live.monitor import write_heartbeat
    write_heartbeat(live_dir, True, ts=virt.wall)


class _AcctDownService:
    """account 服务包装：flag["acct_down"]=True 时一切查询抛
    DataUnavailableError（半死终端：连接着但取不到账户——O17-4 场景）。"""

    def __init__(self, inner, flag):
        self._inner = inner
        self._flag = flag

    def __getattr__(self, name):
        target = getattr(self._inner, name, None)
        if not callable(target):
            return target

        def call(*a, **k):
            if self._flag["acct_down"]:
                raise DataUnavailableError("account_info 返回空（演练）")
            return target(*a, **k)

        return call


class _AcctDownGateway(_DownGateway):
    """断线开关沿用 _DownGateway；叠加 acct_down 开关（二者独立翻转）。"""

    def __init__(self, broker):
        super().__init__(broker)
        self.state["acct_down"] = False
        self.account = _AcctDownService(self.account, self.state)


def _run_risk(tmp_path, monkeypatch, gw, virt, fake_sleep, *, interval=10.0):
    mod = _load_script("risk_process.py")
    live = tmp_path / "live"
    risk_toml = _write(tmp_path / "risk.toml", RISK_TOML)
    monkeypatch.setattr(mod, "_open_gateway", lambda cfg: gw)
    monkeypatch.setattr(mod, "datetime", _fake_datetime(virt))
    monkeypatch.setattr(mod, "_mono", virt.mono_fn)
    monkeypatch.setattr(mod.time, "sleep", fake_sleep)
    _keep_runner_alive(str(live), virt)
    return mod.main(["--config", risk_toml, "--data-dir", str(tmp_path),
                     "--loop", "--interval", str(interval),
                     "--sessions-config",
                     _write(tmp_path / "sessions.toml", SESSIONS_TOML)]), live


def _prep_reconcile(tmp_path, monkeypatch, gw, virt, fake_sleep, *, interval=10.0):
    mod = _load_script("live_reconcile.py")
    live = tmp_path / "live"
    live.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mod, "_open_gateway", lambda cfg: gw)
    monkeypatch.setattr(mod, "datetime", _fake_datetime(virt))
    monkeypatch.setattr(mod, "_mono", virt.mono_fn)
    monkeypatch.setattr(mod.time, "sleep", fake_sleep)
    return mod, live


def _run_reconcile(mod, tmp_path, *, interval=10.0):
    return mod.main(["--data-dir", str(tmp_path), "--loop",
                     "--interval", str(interval)])


# ------------------------------------------------ 常量（supervisor/取证汇总）

def test_t184_constants():
    """缺省 300 秒时间窗、专用告警种类；退出码复用 T142 的 79（halt 语义
    不变——O17-4 只改触发条件）。"""
    from mt5live.monitor import (
        ACCOUNT_UNAVAILABLE_LIMIT_S,
        ALERT_ACCOUNT_UNAVAILABLE,
        LOOP_EXIT_RC,
    )

    assert ACCOUNT_UNAVAILABLE_LIMIT_S == 300.0
    assert ALERT_ACCOUNT_UNAVAILABLE == "ACCOUNT_UNAVAILABLE"
    assert LOOP_EXIT_RC == 79


# --------------------------------------------- AccountUnavailableWatch 单元

def test_t184_watch_segment_alert_once_recovery_no_spam(tmp_path):
    """段首一条告警、恢复一条、段内不刷屏；恢复清零后不再累计。"""
    from tests.test_t141_disconnect import VirtClock, _Mono
    from mt5live.monitor import AccountUnavailableWatch

    mono = _Mono()
    watch = AccountUnavailableWatch(str(tmp_path), "risk", clock=mono)
    assert not watch.expired()
    assert watch.unavailable_s == 0.0
    watch.note_failure(DataUnavailableError("account_info 返回空"))
    assert watch.unavailable_s == 0.0
    assert _alerts_text(tmp_path).count("ACCOUNT_UNAVAILABLE") == 1  # 段首一次
    watch.note_failure(DataUnavailableError("仍取不到"))
    assert _alerts_text(tmp_path).count("ACCOUNT_UNAVAILABLE") == 1  # 不刷屏
    mono.advance(299)
    assert not watch.expired()                       # 299s < 300s
    watch.note_ok()                                  # 恢复：告警 + 清零
    assert watch.unavailable_s == 0.0
    text = _alerts_text(tmp_path)
    assert text.count("ACCOUNT_UNAVAILABLE") == 2    # 段首 + 恢复
    assert "恢复" in text
    mono.advance(ACCOUNT_UNAVAILABLE_LIMIT_S + 1)
    assert not watch.expired()                       # 已清零，不再累计


def test_t184_watch_exit_code_writes_marker_halt_and_returns_79(tmp_path):
    """时间窗耗尽：loop-exit-<component>.json（cause=account_unavailable_window、
    reason=unrecoverable_loop/<具体>、时长/上限）+ halt（fail-closed，语义与
    T142 一致）+ LOOP_EXIT 告警，返回 79。"""
    from tests.test_t141_disconnect import _Mono
    from mt5live.monitor import AccountUnavailableWatch

    mono = _Mono()
    watch = AccountUnavailableWatch(str(tmp_path), "risk", clock=mono)
    watch.note_failure(DataUnavailableError("account_info 返回空"))
    mono.advance(ACCOUNT_UNAVAILABLE_LIMIT_S)
    assert watch.expired()
    rc = watch.exit_code()
    assert rc == LOOP_EXIT_RC
    marker = _read_json(tmp_path / "loop-exit-risk.json")
    assert marker["component"] == "risk"
    assert marker["rc"] == LOOP_EXIT_RC
    assert marker["cause"] == "account_unavailable_window"
    assert marker["reason"].startswith("unrecoverable_loop/DataUnavailableError")
    assert marker["duration_s"] >= ACCOUNT_UNAVAILABLE_LIMIT_S
    assert marker["limit_s"] == ACCOUNT_UNAVAILABLE_LIMIT_S
    assert marker["first_failure_utc"] and marker["ts"] and marker["pid"]
    assert (tmp_path / "halt.json").exists()         # halt 语义不变（fail-closed）
    assert "LOOP_EXIT" in _alerts_text(tmp_path)


# ------------------------------------------------------ 风控：计划逐字场景

def test_t184_risk_60s_unavailable_recovers_no_exit(tmp_path, monkeypatch):
    """计划场景 1：账户取数失败 60 秒后恢复 → 不退出、不写 halt；角色心跳
    照写；告警恰好两条（段首 + 恢复），中间不刷屏。"""
    from tests.test_t141_disconnect import VirtClock

    gw = _AcctDownGateway(FakeBroker("XAUUSD"))
    gw.state["acct_down"] = True
    virt = VirtClock(datetime(2026, 11, 10, 12, 0, 0))
    real_sleep = time.sleep
    step = {"n": 0}

    def fake_sleep(s):
        step["n"] += 1
        virt.advance(10)
        _keep_runner_alive(str(tmp_path / "live"), virt)
        if step["n"] == 6:                     # 6 轮失败（60 秒）后恢复
            gw.state["acct_down"] = False
        elif step["n"] >= 9:                   # 恢复后跑满健康轮
            raise KeyboardInterrupt
        real_sleep(0)

    rc, live = _run_risk(tmp_path, monkeypatch, gw, virt, fake_sleep)
    assert rc == 0                             # 不退出（手动停止收尾）
    assert not (live / "loop-exit-risk.json").exists()
    assert not (live / "halt.json").exists()
    assert not (live / "disconnect-exit-risk.json").exists()
    assert (live / "heartbeat-risk.json").exists()   # 失败轮照写角色心跳
    alerts = _alerts_text(live)
    assert alerts.count("ACCOUNT_UNAVAILABLE") == 2  # 段首 + 恢复，不刷屏


def test_t184_risk_6min_unavailable_exits_79_halt(tmp_path, monkeypatch):
    """计划场景 2：连续 6 分钟（360s ≥ 300s 窗）取不到账户 → 以 79 退出并
    写 halt + loop-exit-risk.json（cause=account_unavailable_window）。"""
    from tests.test_t141_disconnect import VirtClock

    gw = _AcctDownGateway(FakeBroker("XAUUSD"))
    gw.state["acct_down"] = True
    virt = VirtClock(datetime(2026, 11, 10, 12, 0, 0))
    real_sleep = time.sleep
    step = {"n": 0}

    def fake_sleep(s):
        step["n"] += 1
        virt.advance(10)
        _keep_runner_alive(str(tmp_path / "live"), virt)
        if step["n"] > 80:                     # 兜底：防回归时死循环
            raise KeyboardInterrupt
        real_sleep(0)

    rc, live = _run_risk(tmp_path, monkeypatch, gw, virt, fake_sleep)
    assert rc == LOOP_EXIT_RC                  # 79：时间窗（不是 5 轮）
    marker = _read_json(live / "loop-exit-risk.json")
    assert marker["rc"] == LOOP_EXIT_RC
    assert marker["cause"] == "account_unavailable_window"
    assert marker["reason"].startswith("unrecoverable_loop/DataUnavailableError")
    assert marker["duration_s"] >= ACCOUNT_UNAVAILABLE_LIMIT_S
    assert marker["duration_s"] <= 6 * 60 + 60
    assert _read_json(live / "exit-risk.json")["rc"] == LOOP_EXIT_RC
    assert (live / "halt.json").exists()       # halt 语义不变（fail-closed）
    alerts = _alerts_text(live)
    assert alerts.count("ACCOUNT_UNAVAILABLE") == 1   # 段首一次（退出走 LOOP_EXIT）
    assert "LOOP_EXIT" in alerts


def test_t184_risk_disconnect_stays_t141_window_only(tmp_path, monkeypatch):
    """计划场景 3：断线仍只走 T141 的 15 分钟时间窗——20 分钟断线 → 78
    （不是 79）、不写 halt；本计时从未被触发（无 ACCOUNT_UNAVAILABLE 告警）。"""
    from tests.test_t141_disconnect import VirtClock

    gw = _AcctDownGateway(FakeBroker("XAUUSD"))
    virt = VirtClock(datetime(2026, 11, 10, 12, 0, 0))
    real_sleep = time.sleep
    step = {"n": 0}

    def fake_sleep(s):
        step["n"] += 1
        virt.advance(30)
        _keep_runner_alive(str(tmp_path / "live"), virt)
        if step["n"] == 2:
            gw.state["down"] = True            # 之后一直断线（≥ 20 分钟）
        real_sleep(0)

    rc, live = _run_risk(tmp_path, monkeypatch, gw, virt, fake_sleep, interval=30)
    assert rc == DISCONNECT_EXIT_RC            # 78，不误触发本计时
    assert (live / "disconnect-exit-risk.json").exists()
    assert not (live / "loop-exit-risk.json").exists()
    assert not (live / "halt.json").exists()   # 78 不写 halt（supervisor 决定）
    assert "ACCOUNT_UNAVAILABLE" not in _alerts_text(live)


def test_t184_probe_p17_4_not_reproduced():
    """计划场景 4：P17-4 变为 NOT-REPRODUCED（直接复用审核探针本体断言）。"""
    spec = importlib.util.spec_from_file_location(
        "round17_probes_t184",
        REPO_ROOT / "docs" / "reviews" / "probes" / "2026-11-08-round17-probes.py")
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    probe.probe_risk_data_unavailable_halt(REPO_ROOT)
    pid, status, _detail = probe.RESULTS[-1]
    assert pid == "P17-4"
    assert status == "NOT-REPRODUCED"


# --------------------------------------------- 三套计时互不干扰（边界钉死）

def test_t184_risk_dua_not_counted_5rounds_perm_not_in_window(tmp_path,
                                                               monkeypatch):
    """边界 1：DataUnavailableError 轮不进 T142 的 5 轮计数；PermissionError
    轮不进账户时间窗。序列：4×Perm（计数 4）→ 20×DUA（190s < 300s，计数
    冻结）→ 完整成功（双清零）→ 4×Perm（< 5，不退出）→ 恢复手动停止。"""
    from tests.test_t141_disconnect import VirtClock

    perm = _PermHeartbeat(monkeypatch)
    perm.flag["on"] = True
    gw = _AcctDownGateway(FakeBroker("XAUUSD"))
    virt = VirtClock(datetime(2026, 11, 10, 12, 0, 0))
    real_sleep = time.sleep
    step = {"n": 0}

    def fake_sleep(s):
        step["n"] += 1
        virt.advance(10)
        _keep_runner_alive(str(tmp_path / "live"), virt)
        if step["n"] == 4:                     # 4×Perm 后转账户不可用
            perm.flag["on"] = False
            gw.state["acct_down"] = True
        elif step["n"] == 24:                  # 20×DUA 后恢复 → 完整成功双清零
            gw.state["acct_down"] = False
        elif step["n"] == 25:                  # 再 4×Perm（< 5 轮，不退出）
            perm.flag["on"] = True
        elif step["n"] == 29:
            perm.flag["on"] = False
        elif step["n"] >= 31:
            raise KeyboardInterrupt
        real_sleep(0)

    rc, live = _run_risk(tmp_path, monkeypatch, gw, virt, fake_sleep)
    assert rc == 0
    assert not (live / "loop-exit-risk.json").exists()   # 4+20+4 都未达上限
    assert not (live / "halt.json").exists()
    alerts = _alerts_text(live)
    assert alerts.count("RECOVERABLE_FAILURE") == 2      # 两个 Perm 失败段各一条
    assert alerts.count("ACCOUNT_UNAVAILABLE") == 2      # DUA 段首 + 恢复


def test_t184_risk_disconnect_rounds_neutral_to_account_window(tmp_path,
                                                                monkeypatch):
    """边界 2：断线轮对账户时间窗中性——不重置（无第二段告警）、不写恢复；
    断线段内的单调流逝如实累计（账户确实一直取不到）。序列：24×DUA（230s）
    → 6 轮断线（60s，未到 900s）→ 重连但账户仍取不到 → 首个失败轮即按真实
    累计时长 79（若被断线重置，则需再等 300s 且多一条段首告警）。"""
    from tests.test_t141_disconnect import VirtClock

    gw = _AcctDownGateway(FakeBroker("XAUUSD"))
    gw.state["acct_down"] = True
    virt = VirtClock(datetime(2026, 11, 10, 12, 0, 0))
    real_sleep = time.sleep
    step = {"n": 0}

    def fake_sleep(s):
        step["n"] += 1
        virt.advance(10)
        _keep_runner_alive(str(tmp_path / "live"), virt)
        if step["n"] == 24:                    # 24×DUA 后断线 6 轮
            gw.state["acct_down"] = False
            gw.state["down"] = True
        elif step["n"] == 30:                  # 重连，但账户随即又取不到
            gw.state["down"] = False
            gw.state["acct_down"] = True
        real_sleep(0)

    rc, live = _run_risk(tmp_path, monkeypatch, gw, virt, fake_sleep)
    assert rc == LOOP_EXIT_RC
    marker = _read_json(live / "loop-exit-risk.json")
    assert marker["cause"] == "account_unavailable_window"
    assert marker["duration_s"] >= ACCOUNT_UNAVAILABLE_LIMIT_S
    assert _alerts_text(live).count("ACCOUNT_UNAVAILABLE") == 1  # 未被重置


def test_t184_risk_degraded_round_dua_goes_to_window_not_unknown(
        tmp_path, monkeypatch):
    """边界 3：断线降级轮内的 DataUnavailableError（文件级检查读到坏状态等）
    同走账户时间窗，不落"未知异常"分支。断线 900s 窗未到，账户 300s 窗先到
    → 79/cause=account_unavailable_window（先到的窗口先裁决）。"""
    import mt5live.riskproc as riskproc
    from tests.test_t141_disconnect import VirtClock

    def dua_heartbeat(path):
        raise DataUnavailableError("状态文件读不出（演练）")

    monkeypatch.setattr(riskproc, "read_heartbeat_path", dua_heartbeat)
    gw = _AcctDownGateway(FakeBroker("XAUUSD"))
    gw.state["down"] = True                    # 一起就断线（降级轮）
    virt = VirtClock(datetime(2026, 11, 10, 12, 0, 0))
    real_sleep = time.sleep
    step = {"n": 0}

    def fake_sleep(s):
        step["n"] += 1
        virt.advance(10)
        if step["n"] > 80:
            raise KeyboardInterrupt
        real_sleep(0)

    rc, live = _run_risk(tmp_path, monkeypatch, gw, virt, fake_sleep)
    assert rc == LOOP_EXIT_RC
    marker = _read_json(live / "loop-exit-risk.json")
    assert marker["cause"] == "account_unavailable_window"
    assert marker["duration_s"] >= ACCOUNT_UNAVAILABLE_LIMIT_S
    assert not (live / "disconnect-exit-risk.json").exists()  # 300s < 900s


# ------------------------------------------------ 对账：持仓查询同口径

def test_t184_reconcile_positions_60s_recovers_no_exit(tmp_path, monkeypatch):
    """对账：持仓查询失败 60 秒后恢复 → 不退出、不写 halt；恢复后照常对账
    一致；告警恰好两条（段首 + 恢复）。"""
    from tests.test_t141_disconnect import VirtClock

    gw = _AcctDownGateway(FakeBroker("XAUUSD"))
    gw.state["acct_down"] = True
    virt = VirtClock(datetime(2026, 11, 10, 12, 0, 0))
    real_sleep = time.sleep
    step = {"n": 0}

    def fake_sleep(s):
        step["n"] += 1
        virt.advance(10)
        if step["n"] == 6:                     # 6 轮失败（60 秒）后恢复
            gw.state["acct_down"] = False
        elif step["n"] >= 9:
            raise KeyboardInterrupt
        real_sleep(0)

    mod, live = _prep_reconcile(tmp_path, monkeypatch, gw, virt, fake_sleep)
    _write_intent_ok(live)
    rc = _run_reconcile(mod, tmp_path)
    assert rc == 0
    assert not (live / "loop-exit-reconcile.json").exists()
    assert not (live / "halt.json").exists()
    assert (live / "heartbeat-reconcile.json").exists()
    alerts = _alerts_text(live)
    assert alerts.count("ACCOUNT_UNAVAILABLE") == 2
    assert _read_json(live / f"pause-{MAGIC}.json")["paused"] is False


def test_t184_reconcile_positions_6min_exits_79_halt(tmp_path, monkeypatch):
    """对账：持仓查询连续 6 分钟失败 → 79 + halt + loop-exit-reconcile.json
    （cause=account_unavailable_window）。"""
    from tests.test_t141_disconnect import VirtClock

    gw = _AcctDownGateway(FakeBroker("XAUUSD"))
    gw.state["acct_down"] = True
    virt = VirtClock(datetime(2026, 11, 10, 12, 0, 0))
    real_sleep = time.sleep
    step = {"n": 0}

    def fake_sleep(s):
        step["n"] += 1
        virt.advance(10)
        if step["n"] > 80:
            raise KeyboardInterrupt
        real_sleep(0)

    mod, live = _prep_reconcile(tmp_path, monkeypatch, gw, virt, fake_sleep)
    _write_intent_ok(live)
    rc = _run_reconcile(mod, tmp_path)
    assert rc == LOOP_EXIT_RC
    marker = _read_json(live / "loop-exit-reconcile.json")
    assert marker["cause"] == "account_unavailable_window"
    assert marker["duration_s"] >= ACCOUNT_UNAVAILABLE_LIMIT_S
    assert _read_json(live / "exit-reconcile.json")["rc"] == LOOP_EXIT_RC
    assert (live / "halt.json").exists()


def test_t184_reconcile_once_positions_failure_exits_2_no_halt(
        tmp_path, monkeypatch, capsys):
    """--once 语义保持：持仓查询失败 exit 2（业务性失败，不写 halt、不经
    guarded_main 的 fatal 分支）。"""
    from tests.test_t141_disconnect import VirtClock

    gw = _AcctDownGateway(FakeBroker("XAUUSD"))
    gw.state["acct_down"] = True
    virt = VirtClock(datetime(2026, 11, 10, 12, 0, 0))
    mod, live = _prep_reconcile(tmp_path, monkeypatch, gw, virt, lambda s: None)
    _write_intent_ok(live)
    rc = mod.main(["--data-dir", str(tmp_path), "--once"])
    assert rc == 2
    assert "持仓查询失败" in capsys.readouterr().out
    assert not (live / "halt.json").exists()
    assert not (live / "loop-exit-reconcile.json").exists()


def test_t184_reconcile_intent_rc2_still_5rounds_not_window(tmp_path,
                                                             monkeypatch):
    """边界 4：意图文件问题（run_once rc=2）仍只走 T142 的 5 轮计数——
    4 轮后意图就位 → 完整成功清零，不退出；全程无 ACCOUNT_UNAVAILABLE
    告警（持仓从未被查询）。"""
    from tests.test_t141_disconnect import VirtClock

    gw = _AcctDownGateway(FakeBroker("XAUUSD"))
    virt = VirtClock(datetime(2026, 11, 10, 12, 0, 0))
    real_sleep = time.sleep
    step = {"n": 0}

    def fake_sleep(s):
        step["n"] += 1
        virt.advance(10)
        if step["n"] == 4:                     # 4 轮 rc=2 后意图就位
            _write_intent_ok(tmp_path / "live")
        elif step["n"] >= 7:
            raise KeyboardInterrupt
        real_sleep(0)

    mod, live = _prep_reconcile(tmp_path, monkeypatch, gw, virt, fake_sleep)
    rc = _run_reconcile(mod, tmp_path)
    assert rc == 0
    assert not (live / "loop-exit-reconcile.json").exists()
    assert not (live / "halt.json").exists()
    alerts = _alerts_text(live)
    assert "ACCOUNT_UNAVAILABLE" not in alerts
    assert alerts.count("RECOVERABLE_FAILURE") == 1   # rc=2 失败段首一条
