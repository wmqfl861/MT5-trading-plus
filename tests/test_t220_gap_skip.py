"""T220-S：换日停报段里缺的 bar 不再当"数据滞后"（收窄的方案 S）。

规格是 docs/reviews/2026-11-27-t220-design-codex-ruling.md 的"缺口跳过的边界"表和
"验收与文档"；现场与根因是 docs/ops/phase-f-log.md 2026-11-27 条的 §1、§3、§9。

这家券商的 XAUUSD 每天换日时刻（纽约 17:00）起约 60 分钟没有报价。行情恢复之后、
第一根新 bar 收盘之前，评估网格点的运行器要一根因为那段没有行情而根本不存在的 bar
（M15 是 22:45 那根，H1 是 22:00 那根，冬令时）。11-25 23:02 UTC 启动的运行器因此在
12 分钟里每轮报"数据滞后"、不写心跳，被 supervisor 连续重启 12 次，风控随即停机。

全部用假网关（tests/fakes_live.FakeBroker）和可注入的时钟，不连 MT5。主循环用例经
scripts/live_runner.py 的真实 main() 跑实跑模式（有执行器，不下真实单）：虚拟墙钟
由 sleep 推进，同一座钟上还跑着风控进程（每 10 秒一轮）和 supervisor 的健康判定
（每 60 秒一次）。取数源的重试预算在直接调用的用例里缩到 0.1 秒；主循环用例里
沿用 10 秒——sleep 是假的，不真等。

每条用例第一行标明它在父提交（修复之前）上应当是红还是绿（D118）：正向用例
【父提交上红】，反向回归【改前改后都绿】。所以这份文件在父提交上也要能原样运行：
父提交上还没有的名字用 getattr 取，取数源的换日配置只在它认这个参数时才传。
"""
from __future__ import annotations

import importlib.util
import inspect
import json
import os
from datetime import date, datetime, time as dtime, timedelta
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from mt5data.sessions import SessionPolicy, load_session_policy
from mt5data.store import load_bars, save_bars
from mt5gw.models import DEAL_REASON_SL, Bar
from mt5live import runner as runner_module
from mt5live.commands import write_flatten
from mt5live.config import LiveConfig
from mt5live.monitor import (
    health_timeout,
    heartbeat_status,
    parse_iso,
    read_alerts,
    read_heartbeat_path,
    write_role_heartbeat,
)
from mt5live.reconcile import Intent, write_intent
from mt5live.riskproc import AccountSnapshot, RiskProcess, load_risk_config
from mt5live.runner import (
    MarketClosedError,
    SignalRunner,
    TerminalBarsSource,
    terminal_bars_loader,
    write_heartbeat,
)
from tests.fakes_live import ClosingSignal, FakeBroker, mk_bars
from tests.test_t141_disconnect import RISK_TOML, _DownGateway, _write

REPO_ROOT = Path(__file__).resolve().parents[1]
MAGIC = 220610
SHA = "b" * 40
SYMBOL = "XAUUSD"
GAP_LINE = "换日停报段缺口跳过"

# 这份文件要能在父提交（修复之前）上原样运行：正向用例在那里失败，反向回归在那里
# 通过（D118）。父提交上还没有的名字在这里取不到，先用一个代替——取不到时用到
# 它们的用例本来就该红，而且是因为行为不对而红，不是因为导入失败。
RolloverGapSkipError = getattr(
    runner_module, "RolloverGapSkipError",
    type("_NoGapSkipOnThisCommit", (Exception,), {}))
ALERT_ROLLOVER_GAP_SKIP = getattr(runner_module, "ALERT_ROLLOVER_GAP_SKIP",
                                  "ROLLOVER_GAP_SKIP")

# 与仓库 config/sessions.toml 同形：纽约 17:00 换日，禁入窗前 5 分钟、后 60 分钟
SESSIONS_TOML = """
version = 1
[rollover]
local_time = "17:00"
tz = "America/New_York"
[forbidden]
pre_minutes = 5
post_minutes = 60
exempt_symbols = ["BTCUSD"]
[costs]
spread_gate_multiplier = 3.0
missing_hour_policy = "forbid"
"""

# 2026-11-25 是周三，美国已是冬令时：纽约 17:00 = 22:00 UTC，停报段 [22:00, 23:00)。
# 2026-07-15 是夏令时的周三：纽约 17:00 = 21:00 UTC，停报段 [21:00, 22:00)。
WINTER = date(2026, 11, 25)
SUMMER = date(2026, 7, 15)


def T(hour, minute=0, second=0, day=WINTER):
    return datetime.combine(day, dtime(hour, minute, int(second)))


def load_script(name):
    spec = importlib.util.spec_from_file_location(
        f"{name[:-3]}_t220", REPO_ROOT / "scripts" / name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def jsonl_rows(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(ln) for ln in f if ln.strip()]


def opens(last_open, n, tf_s):
    """末根开盘时刻 = last_open 的 n 根连续 bar 的开盘时刻（升序）。"""
    return [last_open - timedelta(seconds=tf_s * k) for k in range(n - 1, -1, -1)]


@lru_cache(maxsize=None)
def flat_bar(open_time):
    """一根等幅平盘 bar（开收高低都是 100）：买入持有基线不设止损止盈，不需要振幅。
    按时刻缓存：主循环重放里每个假 sleep 都要重新摆一遍终端的 bar。"""
    return Bar(time=open_time, open=100.0, high=100.0, low=100.0, close=100.0,
               tick_volume=0, spread=0, real_volume=0)


def flat_bars(open_times):
    return [flat_bar(t) for t in open_times]


TIMEFRAME_OF = {60: "M1", 900: "M15", 3600: "H1"}


# ================================================================ 主循环重放的装配

class Terminal:
    """假终端的行情剧本（11-25 的形态）。

    停报段里最后一根已收盘 bar 之后没有新 bar，报价停在停报之前；行情恢复后多出
    一根正在形成的 bar，要等它收盘终端才交出闭合的那一根。一分钟 bar 同理。
    """

    def __init__(self, broker, *, tf_s, last_closed_open, blackout_end,
                 stale_quote_time, resume_delay_s=6.0, last_tick_before=None,
                 symbol=SYMBOL):
        """last_tick_before：停报前最后一根一分钟 bar 的开盘时刻（缺省按停报段正好
        60 分钟、换日锚点起算：22:00 停报就是 21:59）。停报段里没有一分钟 bar。"""
        self.broker = broker
        self.symbol = symbol
        self.tf_s = tf_s
        self.timeframe = TIMEFRAME_OF[tf_s]
        self.blackout_end = blackout_end
        self.resume_at = blackout_end + timedelta(seconds=resume_delay_s)
        self.stale_quote_time = stale_quote_time
        self._closed = flat_bars(opens(last_closed_open, 40, tf_s))
        if last_tick_before is None:
            last_tick_before = blackout_end - timedelta(minutes=61)
        self._m1_before = flat_bars(opens(last_tick_before, 90, 60))
        self.first_next_bar_served_at = None

    def refresh(self, wall):
        rows, m1 = list(self._closed), list(self._m1_before)
        if wall >= self.resume_at:
            quote_time = max(wall - timedelta(seconds=1), self.resume_at)
            step = timedelta(seconds=self.tf_s)
            rows += flat_bars(self._grid(self.blackout_end, step, wall))
            m1 += flat_bars(self._grid(self.blackout_end, timedelta(minutes=1), wall))
            if (wall >= self.blackout_end + step
                    and self.first_next_bar_served_at is None):
                self.first_next_bar_served_at = wall
        else:
            quote_time = self.stale_quote_time
        self.broker.set_bars(self.symbol, self.timeframe, rows)
        self.broker.set_bars(self.symbol, "M1", m1)
        self.broker.quote_time = quote_time

    @staticmethod
    def _grid(first, step, until):
        out, t = [], first
        while t <= until:
            out.append(t)
            t += step
        return out


class World:
    """主循环脚本的替身：虚拟墙钟（sleep 即推进）和同时运行的另外两个角色。

    风控进程每 10 秒一轮：写自己的角色心跳，并照常检查运行器的心跳。supervisor
    启动后每满 60 秒一次：用 heartbeat_status + health_timeout 判运行器的
    heartbeat-<magic>.json，外加它的两条子条件——心跳里的 pid 是本进程、时刻不早于
    登记的启动时间。
    """

    def __init__(self, *, broker, terminal, live_dir, risk, magic, interval,
                 start, stop_at):
        self.broker = broker
        self.terminal = terminal
        self.live_dir = live_dir
        self.risk = risk
        self.magic = magic
        self.interval = interval
        self.wall = start
        self.started = start
        self.stop_at = stop_at
        self.gateway = _DownGateway(broker)
        self.risk_beats = []      # (墙钟, heartbeat_ok, 本轮是否写了 halt)
        self.supervisor = []      # 每分钟一次的判定
        self.orders_seen = []     # (墙钟, 到那一刻为止的策略订单数)
        self.ticks = []           # 每次 sleep 之后的墙钟
        self.heartbeat_writes = []   # 运行器每次写心跳时的墙钟（完整走完一轮才有）
        self.on_tick = None       # 测试在某个墙钟时刻注入的动作
        self._last_risk = None
        self._last_role_beat = None
        self._polls = 0
        terminal.refresh(start)

    def now(self):
        return self.wall

    def sleep(self, seconds):
        self.wall += timedelta(seconds=seconds)
        self.terminal.refresh(self.wall)
        self.ticks.append(self.wall)
        self._other_roles()
        self.orders_seen.append((self.wall, len(self.broker.market_orders)))
        if self.on_tick is not None:
            self.on_tick(self.wall)
        if self.wall >= self.stop_at:
            raise KeyboardInterrupt     # 经 main 的 KeyboardInterrupt 分支正常退出

    def _other_roles(self):
        # 风控进程的角色心跳每 30 秒写一次就够（闸门按 120 秒阈值读它）；每个假
        # sleep 都写一遍只是给磁盘添负载
        if (self._last_role_beat is None
                or (self.wall - self._last_role_beat).total_seconds() >= 30):
            self._last_role_beat = self.wall
            write_role_heartbeat(self.live_dir, "risk", interval=10.0, now=self.wall)
        if (self._last_risk is None
                or (self.wall - self._last_risk).total_seconds() >= 10):
            self._last_risk = self.wall
            res = self.risk.run_cycle(AccountSnapshot("USD", (), ()),
                                      now=self.wall, health_clock=lambda: self.wall)
            self.risk_beats.append((self.wall, res.heartbeat_ok, res.halt_written))
        elapsed = (self.wall - self.started).total_seconds()
        if int(elapsed // 60) > self._polls:
            self._polls = int(elapsed // 60)
            self.supervisor.append(self._supervisor_poll())

    def _supervisor_poll(self):
        path = os.path.join(self.live_dir, f"heartbeat-{self.magic}.json")
        hb = read_heartbeat_path(path)
        status = heartbeat_status(hb, self.wall,
                                  timeout_s=health_timeout(self.interval))
        data = read_json(path) if os.path.exists(path) else {}
        return SimpleNamespace(
            wall=self.wall, hb=hb, ok=status.ok, reason=status.reason,
            age_s=status.age_s, pid_ok=data.get("pid") == os.getpid(),
            sha_ok=data.get("deployment_sha") == SHA,
            not_before=hb is not None and self.started <= hb.ts)


def make_env(tmp_path, *, magic=MAGIC, timeframe="M15"):
    """实跑模式的全部输入：live.toml、risk.toml、sessions.toml、合约快照、网关配置。"""
    data = tmp_path / "data"
    (data / "live").mkdir(parents=True)
    (data / "specs").mkdir(parents=True)
    _write(data / "specs" / "latest.json", json.dumps({
        SYMBOL: {"contract_size": 100.0, "volume_min": 0.01,
                 "volume_step": 0.01, "volume_max": 100.0}}))
    config = _write(tmp_path / "live.toml", f"""
symbol = "{SYMBOL}"
timeframe = "{timeframe}"
strategy = "mt5quant.strategies.library.buy_hold:BuyHold"
magic = {magic}
lookback_bars = 30
volume_lots = 0.02
data_root = "{data.as_posix()}"
trigger_delay_s = 5.0
""")
    return dict(
        home=tmp_path, data=data, live=str(data / "live"), config=config,
        sessions=_write(tmp_path / "sessions.toml", SESSIONS_TOML),
        risk=_write(tmp_path / "risk.toml", RISK_TOML),
        gw=_write(tmp_path / "gw.json", "{}"), magic=magic, timeframe=timeframe)


def make_risk(env):
    return RiskProcess(load_risk_config(env["risk"]), live_dir=env["live"],
                       session_policy=load_session_policy(env["sessions"]))


def old_process_heartbeat(env, ts):
    """上一个运行器进程留下的心跳（被重启之前最后一次完整收尾时写的）：另一个 pid、
    没有 deployment SHA、时刻是 ts。实跑目录里总有这么一个文件——新进程第一轮要
    重试 10 秒才写心跳，风控进程在那之前查的是它。"""
    cfg = LiveConfig(symbol=SYMBOL, timeframe=env["timeframe"], strategy="x:X",
                     params={}, magic=env["magic"], lookback_bars=30,
                     volume_lots=0.02, data_root=str(env["data"]))
    write_heartbeat(os.path.join(env["live"], f"heartbeat-{env['magic']}.json"),
                    config=cfg, now=ts, pid=4242)


def run_runner(monkeypatch, world, env):
    """经 scripts/live_runner.py 的真实 main() 跑主循环，直到虚拟墙钟到点。

    三处注入：运行器的时钟（now_utc）、sleep（推进虚拟墙钟）、心跳写入器的时间戳
    （主循环写心跳时不传时刻，缺省读真实墙钟；这里让它读同一座虚拟钟）。
    """
    monkeypatch.setenv("LOCALAPPDATA", str(env["home"] / "lockshome"))
    monkeypatch.setenv("MT5_DEPLOYMENT_SHA", SHA)
    mod = load_script("live_runner.py")
    made = []
    real_runner = mod.SignalRunner
    monkeypatch.setattr(mod, "SignalRunner",
                        lambda *a, **k: made.append(real_runner(*a, **k)) or made[-1])
    monkeypatch.setattr(mod, "now_utc", world.now)
    real_write = mod.HeartbeatWriter.write

    def write(self, **kw):
        world.heartbeat_writes.append(world.wall)
        return real_write(self, now=world.wall, **kw)

    monkeypatch.setattr(mod.HeartbeatWriter, "write", write)
    monkeypatch.setattr("mt5gw.MT5Gateway.from_config_file",
                        staticmethod(lambda p: world.gateway))
    monkeypatch.setattr(mod.time, "sleep", world.sleep)
    rc = mod.main(["--config", env["config"], "--loop", "--no-spread-gate",
                   "--risk-config", env["risk"],
                   "--sessions-config", env["sessions"],
                   "--gateway-config", env["gw"],
                   "--poll-interval", str(world.interval)])
    return rc, made


def build(tmp_path, *, timeframe="M15", tf_s=900, last_closed_open,
          blackout_end=None, start, stop_at, interval, resume_delay_s=6.0,
          last_tick_before=None, old_heartbeat_at=None):
    """一次重放的现场：终端停在 blackout_end（缺省 23:00 UTC）之前，运行器在 start
    启动；live 目录里有上一个进程 2 秒前留下的心跳（old_heartbeat_at 可改）。"""
    env = make_env(tmp_path, timeframe=timeframe)
    broker = FakeBroker(SYMBOL)
    terminal = Terminal(broker, tf_s=tf_s, last_closed_open=last_closed_open,
                        blackout_end=blackout_end or T(23, 0),
                        stale_quote_time=T(21, 59, 50),
                        resume_delay_s=resume_delay_s,
                        last_tick_before=last_tick_before)
    world = World(broker=broker, terminal=terminal, live_dir=env["live"],
                  risk=make_risk(env), magic=env["magic"], interval=interval,
                  start=start, stop_at=stop_at)
    old_process_heartbeat(env, old_heartbeat_at or start - timedelta(seconds=2))
    return SimpleNamespace(env=env, broker=broker, terminal=terminal, world=world)


def replay(tmp_path, monkeypatch, capsys, **kw):
    run = build(tmp_path, **kw)
    rc, made = run_runner(monkeypatch, run.world, run.env)
    run.rc = rc
    run.out = capsys.readouterr().out
    run.runners = made
    return run


def assert_skipped_cleanly(run, *, grid, expected_open, latest, decision_at):
    """三处验收：那个网格不再出"数据滞后"、心跳恢复、下一根有效 bar 只决策一次。"""
    out, world, env = run.out, run.world, run.env
    live = env["live"]
    interval = world.interval
    # ① 那个网格：一条缺口记录，没有"数据滞后"（取数源和运行器两层都没有）
    assert run.rc == 0
    assert "数据滞后" not in out
    assert "决策异常" not in out
    gap_lines = [ln for ln in out.splitlines() if GAP_LINE in ln]
    assert len(gap_lines) == 1, gap_lines                 # 同一网格只记一次
    for text in (f"magic={env['magic']}", f"{SYMBOL} {env['timeframe']}",
                 f"收盘网格={grid.isoformat()}",
                 f"期望bar={expected_open.isoformat()}",
                 f"最新已收盘bar={latest.isoformat()}"):
        assert text in gap_lines[0], text
    alerts = read_alerts(live)
    gap_alerts = [a for a in alerts if a.kind == ALERT_ROLLOVER_GAP_SKIP]
    assert len(gap_alerts) == 1
    for text in (f"magic={env['magic']}", f"{SYMBOL} {env['timeframe']}",
                 f"收盘网格={grid.isoformat()}", f"最新已收盘bar=",
                 "报价时刻=", "停报段="):
        assert text in gap_alerts[0].message, text
    assert not {a.kind for a in alerts} & {
        "SUPERVISOR", "HEARTBEAT_TIMEOUT", "RISK_HALT", "RECONCILE_MISMATCH",
        "SUPERVISOR_REFUSED"}, [a.kind for a in alerts]
    # ② 心跳恢复：本进程的 PID、deployment SHA、完整走完一轮
    hb_path = os.path.join(live, f"heartbeat-{env['magic']}.json")
    hb = read_json(hb_path)
    assert hb["pid"] == os.getpid() and hb["deployment_sha"] == SHA
    assert hb["runner_alive"] is True and hb["last_error"] is None
    assert out.count("loop_completed") >= 10
    assert not os.path.exists(os.path.join(live, "halt.json"))
    assert not os.path.exists(os.path.join(live, "halt-risk-veto.json"))
    assert world.risk_beats
    assert all(ok and not written for _, ok, written in world.risk_beats)
    assert world.supervisor and all(p.pid_ok for p in world.supervisor), \
        "supervisor 的每次检查都应看到本进程的心跳"
    assert all(p.ok and p.sha_ok and p.not_before for p in world.supervisor)
    first = world.supervisor[0]
    timeout_s = health_timeout(interval)
    assert heartbeat_status(first.hb, first.wall, timeout_s=timeout_s).ok
    # 同一份心跳放到 120 秒之后仍然通过（恰好 120 秒不算过期，121 秒才算）
    assert heartbeat_status(first.hb, first.hb.ts + timedelta(seconds=120),
                            timeout_s=timeout_s).ok
    assert not heartbeat_status(first.hb, first.hb.ts + timedelta(seconds=121),
                                timeout_s=timeout_s).ok
    late = [p for p in world.supervisor
            if p.wall >= first.wall + timedelta(seconds=120)]
    assert late, "要跑够 120 秒以上，才看得出不会被 supervisor 再重启"
    # ③ 下一根有效 bar 只决策一次；之前零策略订单
    decision_id = f"{SYMBOL}|{env['timeframe']}|{decision_at.isoformat()}"
    decisions = [ln for ln in out.splitlines() if "] 决策 " in ln]
    assert len(decisions) == 1 and f"决策 {decision_id}：" in decisions[0]
    assert all(n == 0 for at, n in world.orders_seen if at < decision_at)
    assert len(run.broker.market_orders) == 1
    assert out.index(GAP_LINE) < out.index(f"决策 {decision_id}：")
    return gap_alerts[0], decisions[0]


# ================================================================ 正向：父提交上必须红
# 经真实主循环重放 11-25 的情形。

def test_m15_cold_start_skips_the_rollover_gap_grid_and_keeps_beating(
        tmp_path, monkeypatch, capsys):
    """【父提交上红】正向 1：23:02 UTC 启动（11-25 被重启的运行器），报价是新的，终端里最新已收盘
    bar 是 21:45，22:00–23:00 里一根 M1 也没有。缺的 22:45 那根按换日缺口跳过。"""
    run = replay(tmp_path, monkeypatch, capsys, timeframe="M15", tf_s=900,
                 last_closed_open=T(21, 45), start=T(23, 2, 12),
                 stop_at=T(23, 15, 40), interval=5)
    gap, _ = assert_skipped_cleanly(run, grid=T(23, 0), expected_open=T(22, 45),
                                    latest=T(21, 45), decision_at=T(23, 15))
    assert gap.time == T(23, 2, 12)                # 告警记在评估它的那一轮
    assert "休市跳过" not in run.out                # 报价是新的：不是休市分支
    # 取数源确实先照常取数并重试了 10 秒（20 次 0.5 秒的假 sleep），之后才跳过
    assert len(run.world.ticks) > 20
    assert run.world.ticks[0] == T(23, 2, 12) + timedelta(milliseconds=500)
    assert run.world.ticks[19] == T(23, 2, 22)
    # 新进程自己的第一份心跳在重试结束、执行器轮询之后才写（上一个进程的心跳在
    # build() 里是 2 秒前留下的）；父提交上要到 23:15 才会有第一份
    assert run.world.heartbeat_writes[0] == T(23, 2, 22)
    assert run.terminal.first_next_bar_served_at is not None
    assert T(23, 15, 0) <= run.terminal.first_next_bar_served_at <= T(23, 15, 5)


def test_h1_missing_exactly_one_bar_is_skipped_the_same_way(
        tmp_path, monkeypatch, capsys):
    """【父提交上红】正向 2：H1 只缺一根。同样的时刻，最新已收盘 bar 是 21:00，缺的是 22:00 那根；
    下一根要到 00:00 才收盘——中间近一个小时心跳一直在。"""
    tomorrow = WINTER + timedelta(days=1)
    midnight = datetime.combine(tomorrow, dtime(0, 0))
    run = replay(tmp_path, monkeypatch, capsys, timeframe="H1", tf_s=3600,
                 last_closed_open=T(21, 0), start=T(23, 2, 12),
                 stop_at=midnight + timedelta(seconds=40), interval=15)
    assert_skipped_cleanly(run, grid=T(23, 0), expected_open=T(22, 0),
                           latest=T(21, 0), decision_at=midnight)
    assert run.out.count("loop_completed") >= 200   # 23:02–00:00 每一轮都完整收尾


def test_hot_process_skips_when_its_first_look_at_the_grid_sees_a_new_quote(
        tmp_path, monkeypatch, capsys):
    """【父提交上红】正向 3：一直在跑的进程。22:40 起（报价停在 21:59:50，两个网格点走休市跳过），
    行情 23:00:06 恢复；23:00 这个网格点的第一次评估（23:00:07）见到的就是新报价。"""
    run = replay(tmp_path, monkeypatch, capsys, timeframe="M15", tf_s=900,
                 last_closed_open=T(21, 45), start=T(22, 40, 7),
                 stop_at=T(23, 15, 40), interval=10)
    gap, _ = assert_skipped_cleanly(run, grid=T(23, 0), expected_open=T(22, 45),
                                    latest=T(21, 45), decision_at=T(23, 15))
    assert gap.time == T(23, 0, 7)                  # 这个网格点第一次评估就跳过
    assert run.out.count("休市跳过") == 2            # 22:30、22:45 两个网格点照旧
    assert run.out.index("休市跳过 2") < run.out.index(GAP_LINE)


def test_retry_that_crosses_the_next_grid_does_not_use_the_next_bar(
        tmp_path, monkeypatch, capsys):
    """【父提交上红】正向 4：23:14:59 开始评估 B = 23:00，重试跨过 23:15:00——终端这时已经交出 23:00
    那根。它不属于这个网格：旧的网格按缺口跳过，23:00 那根只用于它自己的网格。"""
    run = replay(tmp_path, monkeypatch, capsys, timeframe="M15", tf_s=900,
                 last_closed_open=T(21, 45), start=T(23, 14, 59),
                 stop_at=T(23, 18, 10), interval=5)
    served = run.terminal.first_next_bar_served_at
    assert served is not None and T(23, 15, 0) <= served <= T(23, 15, 9)
    gap, decision = assert_skipped_cleanly(
        run, grid=T(23, 0), expected_open=T(22, 45), latest=T(21, 45),
        decision_at=T(23, 15))
    assert gap.time == T(23, 14, 59)                # 评估的是 B = 23:00 那一轮
    assert run.world.heartbeat_writes[0] == T(23, 15, 9)   # 重试跨过网格，那一轮照常收尾
    gap_line = next(ln for ln in run.out.splitlines() if GAP_LINE in ln)
    assert "最新已收盘bar=2026-11-25T21:45:00" in gap_line   # 没有拿 23:00 那根顶替
    assert decision.startswith("[2026-11-25T23:15:1")        # 下一轮才决策 23:15 的 bar
    assert "库内最新已收盘 bar" not in run.out                # 运行器上层也不再报错


def test_same_gap_grid_is_recorded_once_even_if_it_is_evaluated_again(
        tmp_path, monkeypatch, capsys):
    """【父提交上红】同一进程里同一网格只告警一次是必须的。墙钟回拨让已经消费过的网格点被重新
    评估时，运行器照旧只消费它，主循环不重复记日志和告警。

    从 23:02:12 跑到 23:15:20（其间 23:00 跳过、23:15 决策），把墙钟拨回 23:10:07：
    下一轮重新评估 B = 23:00（缺口照旧），之后的轮次落在 23:00 网格点自身，直到停止。
    """
    run = build(tmp_path, last_closed_open=T(21, 45), start=T(23, 2, 12),
                stop_at=T(23, 20), interval=5)
    rolled = []

    def roll_back(wall):
        if wall >= T(23, 15, 20) and not rolled:
            rolled.append(wall)
            run.world.wall = T(23, 10, 7)
            run.world.stop_at = T(23, 10, 40)        # 回拨之后跑到这里停
            run.terminal.refresh(run.world.wall)

    run.world.on_tick = roll_back
    rc, made = run_runner(monkeypatch, run.world, run.env)
    run.rc, run.out = rc, capsys.readouterr().out
    assert rc == 0 and rolled
    runner = made[0]
    decisions = [ln for ln in run.out.splitlines() if "] 决策 " in ln]
    assert len(decisions) == 1 and "23:15:00" in decisions[0]
    assert runner_gap_skips(runner) == 2              # 运行器确实又消费了一次 23:00
    assert last_gap(runner).close_time == T(23, 0)
    assert run.out.count(GAP_LINE) == 1             # 日志只有第一次那一条
    gap_alerts = [a for a in read_alerts(run.env["live"])
                  if a.kind == ALERT_ROLLOVER_GAP_SKIP]
    assert len(gap_alerts) == 1                     # 告警也只有第一次那一条
    assert not os.path.exists(os.path.join(run.env["live"], "halt.json"))


# ============================== 直接调用取数源和运行器：正向与反向回归
# 每条用例的第一行写明它在父提交上应当是红还是绿

POLICY = SessionPolicy(rollover_local="17:00", rollover_tz="America/New_York",
                       pre_minutes=5, post_minutes=60,
                       exempt_symbols=frozenset({"BTCUSD"}))


def recovery_broker(*, last_closed_open, blackout_end, m1_last_before, tf_s=900,
                    symbol=SYMBOL, extra_m1=()):
    """行情已恢复、下一根还没收盘的终端：收盘 bar 到 last_closed_open 为止，再加一根
    正在形成的 bar；M1 到 m1_last_before 为止（每条用例自己写明停报前最后一分钟），
    另加 extra_m1 和恢复后的第一分钟。"""
    broker = FakeBroker(symbol)
    broker.set_bars(symbol, TIMEFRAME_OF[tf_s],
                    flat_bars(opens(last_closed_open, 40, tf_s) + [blackout_end]))
    m1 = opens(m1_last_before, 90, 60) + list(extra_m1) + [blackout_end]
    broker.set_bars(symbol, "M1", flat_bars(m1))
    return broker


def source_at(broker, now, policy=None, **kw):
    """终端取数源，重试预算 0.1 秒，时钟冻结在 now。换日配置只在取数源认这个参数时
    才传（父提交上没有它，那里本来也没有缺口跳过）。"""
    if "session_policy" in inspect.signature(
            runner_module.TerminalBarsSource).parameters:
        kw["session_policy"] = policy
    kw.setdefault("max_wait_s", 0.1)
    kw.setdefault("poll_interval_s", 0.05)
    return runner_module.TerminalBarsSource(broker, clock=lambda: now, **kw)


def runner_at(broker, now, *, policy=POLICY, symbol=SYMBOL, timeframe="M15"):
    """运行器 + 装配了换日配置的终端取数源。"""
    cfg = LiveConfig(symbol=symbol, timeframe=timeframe, strategy="x:X", params={},
                     magic=MAGIC, lookback_bars=30, volume_lots=0.02,
                     trigger_delay_s=5.0)
    return SignalRunner(cfg, ClosingSignal(),
                        bars_loader=terminal_bars_loader(source_at(broker, now, policy),
                                                         n=40),
                        clock=lambda: now, quote_fn=lambda s: (broker.bid, broker.ask))


def runner_gap_skips(runner):
    """运行器到目前为止跳过的缺口网格点数（父提交上没有这个属性，就是 0）。"""
    return getattr(runner, "gap_skips", 0)


def last_gap(runner):
    """运行器最近一次的缺口记录（父提交上没有这个属性，就是 None）。"""
    return getattr(runner, "last_gap_skip", None)


def test_nothing_is_evaluated_before_the_trigger_delay():
    """【改前改后都绿】触发延迟之前不评估：连取数都没有，既不跳过也不报错。"""
    broker = recovery_broker(last_closed_open=T(21, 45), blackout_end=T(23, 0),
                             m1_last_before=T(21, 59))
    broker.quote_time = T(23, 0, 6)
    calls = []
    real_bars = broker.bars
    broker.bars = lambda *a, **k: calls.append(a[1]) or real_bars(*a, **k)
    runner = runner_at(broker, T(23, 0, 4))
    assert runner.decide(T(23, 0, 4)) is None            # 收盘后 4 秒 < 5 秒容差
    assert calls == []                                  # 没有到点：连取数都没有
    assert runner_gap_skips(runner) == 0 and runner.market_closed_skips == 0


def test_from_the_trigger_delay_on_the_gap_is_skipped_and_the_delay_is_not_added():
    """【父提交上红】触发延迟之后开始评估：满 5 秒就跳过这个网格点。延迟只决定何时开始评估，
    不加到 bar 或停报段上——网格是 23:00:00，不是 23:00:05。"""
    broker = recovery_broker(last_closed_open=T(21, 45), blackout_end=T(23, 0),
                             m1_last_before=T(21, 59))
    broker.quote_time = T(23, 0, 6)
    calls = []
    real_bars = broker.bars
    broker.bars = lambda *a, **k: calls.append(a[1]) or real_bars(*a, **k)
    runner = runner_at(broker, T(23, 0, 5))
    assert runner.decide(T(23, 0, 5)) is None              # 满 5 秒：评估，缺口跳过
    assert "M15" in calls and "M1" in calls             # 照常取数，并取了一分钟 bar
    assert runner_gap_skips(runner) == 1
    gap = last_gap(runner)
    assert gap.close_time == T(23, 0) and gap.expected_open == T(22, 45)
    assert gap.window == (T(22, 0), T(23, 0))
    assert runner.decide(T(23, 0, 17)) is None             # 同一网格点不再评估
    assert runner_gap_skips(runner) == 1


def test_a_quote_just_over_120s_old_takes_the_market_closed_branch_unchanged():
    """【改前改后都绿】报价超过 120 秒：走原来的休市分支——静默跳过、计一次休市，不取数、不报错。"""
    broker = recovery_broker(last_closed_open=T(21, 45), blackout_end=T(23, 0),
                             m1_last_before=T(21, 59))
    broker.quote_time = T(23, 0, 0)                     # 对 23:02:01 是 121 秒
    runner = runner_at(broker, T(23, 2, 1))
    assert runner.decide(T(23, 2, 1)) is None           # 休市分支：原样不动
    assert runner_gap_skips(runner) == 0 and runner.market_closed_skips == 1
    assert "休市" in runner._last_market_closed


def test_a_quote_exactly_120s_old_is_still_fresh_and_the_gap_is_skipped():
    """【父提交上红】报价年龄恰好 120 秒仍算新（沿用休市分支的口径）：照常取数，缺口跳过。"""
    broker = recovery_broker(last_closed_open=T(21, 45), blackout_end=T(23, 0),
                             m1_last_before=T(21, 59))
    broker.quote_time = T(23, 0, 0)                     # 对 23:02:00 恰好 120 秒
    runner = runner_at(broker, T(23, 2, 0))
    assert runner.decide(T(23, 2, 0)) is None
    assert runner_gap_skips(runner) == 1 and runner.market_closed_skips == 0


def test_a_bar_that_exists_inside_the_blackout_is_decided_normally():
    """【改前改后都绿】停报段里已经有那根 bar（那天这段其实有行情）：照常决策，不跳过。"""
    broker = recovery_broker(last_closed_open=T(22, 45), blackout_end=T(23, 0),
                             m1_last_before=T(22, 59))
    broker.quote_time = T(23, 2, 0)
    runner = runner_at(broker, T(23, 2, 0))
    decision = runner.decide(T(23, 2, 0))
    assert decision is not None and decision.bar_close_time == T(23, 0)
    assert runner_gap_skips(runner) == 0 and last_gap(runner) is None


def test_a_gap_that_starts_before_the_blackout_stays_a_lag_error():
    """【改前改后都绿】部分重叠：上一根可信 bar 是 21:30 那根，缺口从 21:45 就开始了，伸进了正常交易段。"""
    broker = recovery_broker(last_closed_open=T(21, 30), blackout_end=T(23, 0),
                             m1_last_before=T(21, 44))
    broker.quote_time = T(23, 2, 0)
    runner = runner_at(broker, T(23, 2, 0))
    with pytest.raises(ValueError, match="数据滞后") as caught:
        runner.decide(T(23, 2, 0))
    assert "触发网格期望 2026-11-25T22:45:00" in str(caught.value)
    assert runner_gap_skips(runner) == 0 and runner._last_close is None   # 网格点没有被消费


def test_the_winter_hours_are_not_a_blackout_in_summer():
    """【改前改后都绿】夏令时的那天按冬令时的小时数（22:00–23:00 UTC）摆一遍：那不是停报段，照旧滞后。"""
    later = T(23, 2, 0, day=SUMMER)
    broker = recovery_broker(last_closed_open=T(21, 45, day=SUMMER),
                             blackout_end=T(23, 0, day=SUMMER),
                             m1_last_before=T(21, 59, day=SUMMER))
    broker.quote_time = later
    runner = runner_at(broker, later)
    with pytest.raises(ValueError, match="数据滞后"):
        runner.decide(later)
    assert runner_gap_skips(runner) == 0


def test_in_summer_the_blackout_is_2100_2200_utc():
    """【父提交上红】夏令时：停报段是 21:00–22:00 UTC（同一个纽约 17:00 的换日锚点，逐日换算）。"""
    at = T(22, 2, 0, day=SUMMER)
    broker = recovery_broker(last_closed_open=T(20, 45, day=SUMMER),
                             blackout_end=T(22, 0, day=SUMMER),
                             m1_last_before=T(20, 59, day=SUMMER))
    broker.quote_time = at
    runner = runner_at(broker, at)
    assert runner.decide(at) is None
    assert runner_gap_skips(runner) == 1
    assert last_gap(runner).window == (T(21, 0, day=SUMMER), T(22, 0, day=SUMMER))


def test_a_late_reopen_after_the_blackout_stays_a_lag_error():
    """【改前改后都绿】窗外晚开：第一个新报价直到 23:15 才到，23:15 这个网格点缺的是 [23:00, 23:15)
    那根——在已知停报段之外，继续按滞后处理。"""
    broker = recovery_broker(last_closed_open=T(21, 45), blackout_end=T(23, 15),
                             m1_last_before=T(21, 59))
    broker.quote_time = T(23, 15, 6)
    runner = runner_at(broker, T(23, 15, 8))
    with pytest.raises(ValueError, match="数据滞后") as caught:
        runner.decide(T(23, 15, 8))
    assert "触发网格期望 2026-11-25T23:00:00" in str(caught.value)
    assert runner_gap_skips(runner) == 0


def test_a_missing_bar_in_normal_hours_stays_a_lag_error():
    """【改前改后都绿】重连之后在正常交易时段缺预期的 bar：原有的重试照走，仍缺就按滞后处理。"""
    broker = recovery_broker(last_closed_open=T(14, 30), blackout_end=T(15, 0),
                             m1_last_before=T(14, 44))
    broker.quote_time = T(15, 2, 0)
    runner = runner_at(broker, T(15, 2, 0))
    with pytest.raises(ValueError, match="数据滞后") as caught:
        runner.decide(T(15, 2, 0))
    assert "已重试 0.1s 仍无" in str(caught.value)
    assert runner_gap_skips(runner) == 0


def test_offline_parquet_lag_is_unchanged(tmp_path):
    """【改前改后都绿】离线 parquet 那一路不变：库内滞后照旧报"数据滞后"，缺口跳过只在终端取数这一路。"""
    root = tmp_path / "data"
    save_bars(str(root), SYMBOL, "M15",
              mk_bars([(t, 100.0, 101.0, 99.0, 100.0)
                       for t in opens(T(21, 45), 40, 900)]))
    cfg = LiveConfig(symbol=SYMBOL, timeframe="M15", strategy="x:X", params={},
                     magic=MAGIC, lookback_bars=30, volume_lots=0.02,
                     trigger_delay_s=5.0, data_root=str(root))
    runner = SignalRunner(cfg, ClosingSignal(), bars_loader=load_bars,
                          clock=lambda: T(23, 2, 0), quote_fn=None)
    with pytest.raises(ValueError, match="数据滞后") as caught:
        runner.decide(T(23, 2, 0))
    assert "库内最新已收盘 bar 2026-11-25T21:45:00" in str(caught.value)
    assert runner_gap_skips(runner) == 0


@pytest.mark.parametrize("minute", [0, 30, 59])
def test_an_m1_bar_inside_the_gap_is_counter_evidence(minute):
    """【改前改后都绿】所缺区间里有一根 M1（这里是被参数化的那一分钟）：那段其实有行情，继续按滞后处理。"""
    broker = recovery_broker(last_closed_open=T(21, 45), blackout_end=T(23, 0),
                             m1_last_before=T(21, 59), extra_m1=[T(22, minute)])
    broker.quote_time = T(23, 2, 0)
    runner = runner_at(broker, T(23, 2, 0))
    with pytest.raises(ValueError, match="数据滞后"):
        runner.decide(T(23, 2, 0))
    assert runner_gap_skips(runner) == 0


@pytest.mark.parametrize("how", ["raises", "empty", "stale"])
def test_unusable_m1_history_is_not_a_verification(how):
    """【改前改后都绿】一分钟 bar 取不到（接口失败、返回空、历史没有覆盖到上一根可信 bar）时不算核实。"""
    broker = recovery_broker(last_closed_open=T(21, 45), blackout_end=T(23, 0),
                             m1_last_before=T(21, 59))
    broker.quote_time = T(23, 2, 0)
    if how == "raises":
        real_bars = broker.bars

        def bars(symbol, timeframe, count, *, end_pos=0):
            if timeframe == "M1":
                raise ConnectionError("终端 IPC 断开")
            return real_bars(symbol, timeframe, count, end_pos=end_pos)

        broker.bars = bars
    elif how == "empty":
        broker.set_bars(SYMBOL, "M1", [])
    else:
        broker.set_bars(SYMBOL, "M1", flat_bars(opens(T(12, 0), 60, 60)))
    runner = runner_at(broker, T(23, 2, 0))
    with pytest.raises(ValueError, match="数据滞后"):
        runner.decide(T(23, 2, 0))
    assert runner_gap_skips(runner) == 0


def test_a_quote_inside_the_gap_is_evidence_of_trading():
    """【改前改后都绿】报价落在所缺区间里（22:59:30，年龄 30 秒是新的）：它本身就是有行情的证据。"""
    broker = recovery_broker(last_closed_open=T(21, 45), blackout_end=T(23, 0),
                             m1_last_before=T(21, 59))
    broker.quote_time = T(22, 59, 30)
    runner = runner_at(broker, T(23, 1, 0))
    with pytest.raises(ValueError, match="数据滞后"):
        runner.decide(T(23, 1, 0))
    assert runner_gap_skips(runner) == 0


def test_a_quote_that_cannot_be_read_at_judgement_time_is_not_a_verification():
    """【父提交上红】判定时刻重新读报价读不到（接口失败）：不算核实，照旧按滞后处理。"""
    broker = recovery_broker(last_closed_open=T(21, 45), blackout_end=T(23, 0),
                             m1_last_before=T(21, 59))
    broker.quote_time = T(23, 2, 0)
    real_quote = broker.quote
    reads = []

    def flaky_quote(symbol):
        reads.append(symbol)
        if len(reads) >= 2:                    # 重试结束后的那一次读不到
            raise ConnectionError("终端 IPC 断开")
        return real_quote(symbol)

    broker.quote = flaky_quote
    runner = runner_at(broker, T(23, 2, 0))
    with pytest.raises(ValueError, match="数据滞后"):
        runner.decide(T(23, 2, 0))
    assert runner_gap_skips(runner) == 0 and len(reads) == 2


def test_a_quote_far_in_the_future_stays_a_lag_error():
    """【改前改后都绿】报价超前墙钟 121 秒（超过同一个 120 秒阈值）不算"新的"：照旧按滞后处理。"""
    broker = recovery_broker(last_closed_open=T(21, 45), blackout_end=T(23, 0),
                             m1_last_before=T(21, 59))
    broker.quote_time = T(23, 4, 1)
    runner = runner_at(broker, T(23, 2, 0))
    with pytest.raises(ValueError, match="数据滞后"):
        runner.decide(T(23, 2, 0))
    assert runner_gap_skips(runner) == 0


def test_a_quote_slightly_ahead_of_the_wall_clock_is_tolerated():
    """【父提交上红】券商时钟比本机快 2 秒是常态：不算"来自未来"，照常跳过，记下的是判定时
    重新读到的那份报价。"""
    broker = recovery_broker(last_closed_open=T(21, 45), blackout_end=T(23, 0),
                             m1_last_before=T(21, 59))
    broker.quote_time = T(23, 2, 2)
    runner = runner_at(broker, T(23, 2, 0))
    assert runner.decide(T(23, 2, 0)) is None
    assert runner_gap_skips(runner) == 1 and last_gap(runner).quote_time == T(23, 2, 2)


@pytest.mark.parametrize("symbol", ["XAUUSDm", "EURUSD"])
def test_other_symbols_do_not_get_the_exception(symbol):
    """【改前改后都绿】别的品种（包括带后缀的同名品种）不适用，哪怕形态一模一样。"""
    broker = recovery_broker(symbol=symbol, last_closed_open=T(21, 45),
                             blackout_end=T(23, 0), m1_last_before=T(21, 59))
    broker.quote_time = T(23, 2, 0)
    runner = runner_at(broker, T(23, 2, 0), symbol=symbol)
    with pytest.raises(ValueError, match="数据滞后"):
        runner.decide(T(23, 2, 0))
    assert runner_gap_skips(runner) == 0


def test_empty_history_and_a_source_without_a_policy_keep_the_lag_error():
    """【改前改后都绿】空历史、没有装配换日配置的取数源：都保留原来的故障处理。"""
    broker = FakeBroker(SYMBOL)                             # 一根已收盘 bar 都没有
    broker.set_bars(SYMBOL, "M15", flat_bars([T(23, 0)]))   # 只有正在形成的那根
    broker.quote_time = T(23, 2, 0)
    runner = runner_at(broker, T(23, 2, 0))
    with pytest.raises(ValueError, match="数据滞后：XAUUSD M15 终端最新已收盘 bar 无"):
        runner.decide(T(23, 2, 0))
    assert runner_gap_skips(runner) == 0
    broker = recovery_broker(last_closed_open=T(21, 45), blackout_end=T(23, 0),
                             m1_last_before=T(21, 59))
    broker.quote_time = T(23, 2, 0)
    runner = runner_at(broker, T(23, 2, 0), policy=None)
    with pytest.raises(ValueError, match="数据滞后"):
        runner.decide(T(23, 2, 0))
    assert runner_gap_skips(runner) == 0


def crossed_grid_broker():
    """重试已经跨过 23:15 时的终端：23:00 那根闭合了，23:15 正在形成；取数源的时钟
    还停在重试开始的那一刻（23:14:59）。"""
    broker = recovery_broker(last_closed_open=T(21, 45), blackout_end=T(23, 0),
                             m1_last_before=T(21, 59))
    broker.set_bars(SYMBOL, "M15",
                    flat_bars(opens(T(21, 45), 40, 900) + [T(23, 0), T(23, 15)]))
    broker.quote_time = T(23, 14, 59)
    return broker


def test_the_source_called_without_the_grid_still_returns_the_next_bar():
    """【改前改后都绿】直接调用取数源（不给 B）沿用旧行为：按取数源自己的时钟选网格，非末行的 bar
    一律当已收盘——重试跨过网格之后，下一根 bar 就这样被交了出来。"""
    legacy = source_at(crossed_grid_broker(), T(23, 14, 59), POLICY).closed_bars(
        SYMBOL, "M15", 40)
    assert pd.Timestamp(legacy["time"].iloc[-1]) == pd.Timestamp(T(23, 0))


def test_with_the_grid_the_source_does_not_count_the_next_bar():
    """【父提交上红】给了 B = 23:00：23:00 那根不算数，按缺口跳过，记下的最新 bar 仍是 21:45。"""
    source = source_at(crossed_grid_broker(), T(23, 14, 59), POLICY)
    with pytest.raises(RolloverGapSkipError) as caught:
        source.closed_bars(SYMBOL, "M15", 40, close_time=T(23, 0))
    assert caught.value.gap.close_time == T(23, 0)
    assert caught.value.gap.latest_bar == T(21, 45)


def test_the_runner_hands_its_grid_to_loaders_that_accept_it():
    """【父提交上红】运行器把自己选定的 B 交给认这个参数的加载器；不认的照旧按三个位置参数调用。"""
    bars = mk_bars([(t, 100.0, 101.0, 99.0, 100.0) for t in opens(T(22, 45), 40, 900)])
    seen = []

    def named(root, symbol, timeframe, close_time=None):
        seen.append(close_time)
        return bars

    def star(root, symbol, timeframe, **kw):
        seen.append(kw)
        return bars

    def plain(root, symbol, timeframe):
        seen.append("plain")
        return bars

    cfg = LiveConfig(symbol=SYMBOL, timeframe="M15", strategy="x:X", params={},
                     magic=MAGIC, lookback_bars=30, volume_lots=0.02,
                     trigger_delay_s=5.0)
    for loader, expected in ((named, T(23, 0)),
                             (star, {"close_time": T(23, 0)})):
        runner = SignalRunner(cfg, ClosingSignal(), bars_loader=loader,
                              clock=lambda: T(23, 0, 7), quote_fn=None)
        assert runner.decide(T(23, 0, 7)) is not None
        assert seen[-1] == expected
    runner = SignalRunner(cfg, ClosingSignal(), bars_loader=plain,
                          clock=lambda: T(23, 0, 7), quote_fn=None)
    assert runner.decide(T(23, 0, 7)) is not None and seen[-1] == "plain"


def test_the_blackout_is_the_rollover_anchor_plus_post_minutes_only():
    """【父提交上红】停报段 = 换日锚点起 post_minutes 分钟；禁入窗的前几分钟、品种扩展窗、统计
    禁入小时都不扩大它。"""
    wide = SessionPolicy(rollover_local="17:00", rollover_tz="America/New_York",
                         pre_minutes=30, post_minutes=60,
                         exempt_symbols=frozenset({"BTCUSD"}),
                         extended_ends={"XAUUSD": "20:00"},
                         forbidden_hours_ny={"XAUUSD": frozenset({17, 18})})
    assert wide.rollover_blackout(WINTER) == (T(22, 0), T(23, 0))
    assert wide.rollover_blackout(SUMMER) == (T(21, 0, day=SUMMER), T(22, 0, day=SUMMER))
    assert wide.rollover_window(WINTER) == (T(21, 30), T(23, 0))   # 禁入窗含前 30 分钟
    assert not wide.entry_allowed(SYMBOL, T(23, 30))              # 扩展窗到 20:00 纽约
    # post_minutes 就是停报段的长度，不是硬编码的 60 分钟
    short = SessionPolicy(post_minutes=30)
    assert short.rollover_blackout(WINTER) == (T(22, 0), T(22, 30))
    broker = recovery_broker(last_closed_open=T(21, 45), blackout_end=T(22, 30),
                             m1_last_before=T(21, 59))
    broker.quote_time = T(22, 32, 0)
    runner = runner_at(broker, T(22, 32, 0), policy=short)
    assert runner.decide(T(22, 32, 0)) is None                    # B 等于窗尾也算
    assert last_gap(runner).window == (T(22, 0), T(22, 30))
    broker = recovery_broker(last_closed_open=T(21, 45), blackout_end=T(22, 45),
                             m1_last_before=T(21, 59))
    broker.quote_time = T(22, 47, 0)
    runner = runner_at(broker, T(22, 47, 0), policy=short)
    with pytest.raises(ValueError, match="数据滞后"):              # 窗外第一根缺失
        runner.decide(T(22, 47, 0))
    assert runner_gap_skips(runner) == 0


def test_market_closed_error_is_not_a_gap_skip():
    """【父提交上红】休市分支抛的是 MarketClosedError，与缺口跳过是两个异常；loader 只把 None 翻译成休市。"""
    market = SimpleNamespace(quote=lambda symbol: SimpleNamespace(
        time=T(22, 59, 50), bid=100.0, ask=100.2))
    source = TerminalBarsSource(market, clock=lambda: T(23, 2, 0))
    assert source.closed_bars(SYMBOL, "M15", 40, close_time=T(23, 0)) is None
    with pytest.raises(MarketClosedError):
        terminal_bars_loader(source, n=40)("root", SYMBOL, "M15", close_time=T(23, 0))
    assert not issubclass(RolloverGapSkipError, MarketClosedError)
    assert not issubclass(RolloverGapSkipError, ValueError)


# ================== 主循环上必须不变的东西（以及跳过的那一轮执行器照常运行）

def test_a_lag_outside_the_blackout_still_fails_closed_through_the_main_loop(
        tmp_path, monkeypatch, capsys):
    """【改前改后都绿】窗外晚开，经真实主循环：23:00 这个网格点评估时报价还是旧的（休市跳过、心跳照写）；
    行情 23:15:03 才恢复，23:15 这个网格点缺的是停报段之外的 [23:00, 23:15)。照旧每轮
    "数据滞后"、不写心跳、没有缺口告警，风控 120 秒后停机。"""
    run = replay(tmp_path, monkeypatch, capsys, last_closed_open=T(21, 45),
                 blackout_end=T(23, 15), start=T(23, 0, 7), stop_at=T(23, 17, 30),
                 interval=5, resume_delay_s=3.0, last_tick_before=T(21, 59))
    out, live = run.out, run.env["live"]
    assert run.rc == 0
    assert "休市跳过 1" in out                            # 23:00 那个网格点走休市分支
    assert GAP_LINE not in out
    assert "决策异常：ValueError: 数据滞后" in out
    assert "触发网格期望 2026-11-25T23:00:00" in out
    assert ALERT_ROLLOVER_GAP_SKIP not in [a.kind for a in read_alerts(live)]
    hb = read_json(os.path.join(live, f"heartbeat-{MAGIC}.json"))
    assert parse_iso(hb["ts"]) < T(23, 15, 5)             # 报"数据滞后"的轮次不写心跳
    assert run.world.heartbeat_writes[-1] < T(23, 15, 5)  # 最后一次写也在那之前
    assert max(run.world.ticks) >= T(23, 17, 29)         # 之后又空转了两分多钟
    assert read_json(os.path.join(live, "halt.json"))["reason"] == "heartbeat_timeout"
    assert "HEARTBEAT_TIMEOUT" in [a.kind for a in read_alerts(live)]
    assert run.broker.market_orders == []
    assert [written for _, _, written in run.world.risk_beats if written] == [True]
    assert run.world.risk_beats[-1][1] is False          # 之后每一轮心跳都不通过


def test_the_executor_still_runs_in_a_skip_cycle_flatten_command(
        tmp_path, monkeypatch, capsys):
    """【父提交上红】跳过的那一轮执行器照常 poll(None)：先于一切的全平指令被消费，心跳照写。"""
    run = build(tmp_path, last_closed_open=T(21, 45), start=T(23, 2, 12),
                stop_at=T(23, 15, 40), interval=5)
    run.broker.seed_position(magic=MAGIC)
    write_flatten(run.env["live"], reason="drill", now=T(22, 50))
    flattened_at = []

    def note(wall):
        if run.broker.close_calls and not flattened_at:
            flattened_at.append(wall)

    run.world.on_tick = note
    rc, _ = run_runner(monkeypatch, run.world, run.env)
    out = capsys.readouterr().out
    assert rc == 0 and len(run.broker.close_calls) == 1
    assert flattened_at and flattened_at[0] < T(23, 15)    # 在下一根有效 bar 之前
    assert out.index(GAP_LINE) < out.index("执行 flatten：")
    assert out.count(GAP_LINE) == 1
    decisions = [ln for ln in out.splitlines() if "] 决策 " in ln]
    assert len(decisions) == 1 and "23:15:00" in decisions[0]
    assert "数据滞后" not in out


def test_the_executor_still_runs_in_a_skip_cycle_external_exit(
        tmp_path, monkeypatch, capsys):
    """【父提交上红】跳过的那几轮里执行器照常做外部出场检测：持仓被止损平掉，在 23:15 之前就被识别。"""
    run = build(tmp_path, last_closed_open=T(21, 45), start=T(23, 2, 12),
                stop_at=T(23, 15, 40), interval=5)
    position = run.broker.seed_position(magic=MAGIC)
    live = run.env["live"]
    write_intent(os.path.join(live, f"expected-{MAGIC}.json"), Intent(
        symbol=SYMBOL, magic=MAGIC, side="buy", volume=0.01, status="confirmed",
        cause="test", ts=T(22, 50)))
    exited_at = []

    def stop_out(wall):
        if wall >= T(23, 5, 0) and not exited_at:
            exited_at.append(wall)
            run.broker.close_external(position.ticket, profit=-30.0, comment="[sl]",
                                      reason=DEAL_REASON_SL, time=wall)

    run.world.on_tick = stop_out
    cause_at_exit = []
    deals_log = os.path.join(live, f"deals-{MAGIC}.jsonl")

    def note_cause(wall):
        # 成交证据里一出现这条外部出场，就记下当时意图的来历（23:15 的决策会改写它）
        if not cause_at_exit and any(row["event"] == "external_exit"
                                     for row in jsonl_rows(deals_log)):
            cause_at_exit.append(read_json(
                os.path.join(live, f"expected-{MAGIC}.json"))["cause"])

    run.world.on_tick = lambda wall: (stop_out(wall), note_cause(wall))
    rc, _ = run_runner(monkeypatch, run.world, run.env)
    out = capsys.readouterr().out
    assert rc == 0 and exited_at
    exits = [row for row in jsonl_rows(deals_log)
             if row["event"] == "external_exit" and row["reason"] == "SL"]
    kinds = [a.kind for a in read_alerts(live)]
    assert out.count(GAP_LINE) == 1 and "数据滞后" not in out
    assert kinds.count(ALERT_ROLLOVER_GAP_SKIP) == 1
    assert not set(kinds) & {"HEARTBEAT_TIMEOUT", "RISK_HALT", "RISK_REJECT"}, kinds
    assert not os.path.exists(os.path.join(live, "halt.json"))
    # 出场之后的第一轮就识别了（那一轮的墙钟不早于出场时刻），远在 23:15 之前
    assert len(exits) == 1
    assert exited_at[0] <= parse_iso(exits[0]["ts"]) < T(23, 15)
    assert parse_iso(exits[0]["ts"]) - exited_at[0] <= timedelta(seconds=15)
    assert cause_at_exit and cause_at_exit[0].startswith("external_exit:")
    assert "EXTERNAL_EXIT" not in kinds
    decisions = [ln for ln in out.splitlines() if "] 决策 " in ln]
    assert len(decisions) == 1
    assert len(run.broker.market_orders) == 1             # 23:15 照常重新开仓
