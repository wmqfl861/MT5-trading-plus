"""scripts/prob_calibration.py 的修正测试（T98 审核 + T104 批 2）。

全部合成数据：不触真实数据根、不连 MT5、不跑真实计数/标签引擎——
重依赖经 monkeypatch 替身注入（P-02 的标签器是**捕获式替身**，只记录
调用时刻）。覆盖：
- P-02/P2-06 成簇平移：内部洞/休市/末端丢弃（预写有限容差 = ±1 根
  H1 bar，绝不 nearest 吸附洞边或原始时刻）；周期性原始触发相撞丢弃；
  最终 disjoint；平移后 NY 小时/分钟/星期与 ATR 资格重证；匹配桶
  审计与预写拒绝规则（样本不足时仅审计）。
- P-03 样本单位：名义 n ≠ 实际日数记录；build_costs 传
  swap_ref_price=全序列最后收盘（与正式 P1 同口径）。
- P2-07 来源内容指纹：池内容精确有序 hash（日期平移/赢输时间结构
  反转/单标签/净 R 改动各自变指纹，行序重排不变）；run manifest v2
  绑定池内容 hash、来源行情快照、成本/政策版本、方法版本、依赖清单、
  cluster 冻结输入；pool meta 缺/坏/池被替换拒绝运行；cluster 相位读
  冻结产物不重读研究库；merge 校验全部绑定（旧 v1 manifest 明确拒绝，
  分片指纹必须与 manifest 绑定一致）。
- P-12 merge manifest：run 写完备集 manifest；merge 精确匹配（缺/
  多/重全拒绝）；pool.meta.json 不再被 merge 删除。
"""
import hashlib
import importlib.util
import json
import sys
import types
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "prob_calibration.py"


def load_pc():
    spec = importlib.util.spec_from_file_location("prob_calibration_script",
                                                  SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["prob_calibration_script"] = mod
    spec.loader.exec_module(mod)
    return mod


def synth_bars_at(times, *, seed=7, s0=1800.0):
    """给定时间索引的合成小时线（随机游走 OHLC）。"""
    rng = np.random.default_rng(seed)
    n = len(times)
    steps = rng.normal(0.0, 1.2, size=n)
    close = s0 + np.cumsum(steps)
    openc = close + rng.normal(0.0, 0.4, size=n)
    high = np.maximum(openc, close) + np.abs(rng.normal(0, 0.5, size=n))
    low = np.minimum(openc, close) - np.abs(rng.normal(0, 0.5, size=n))
    return pd.DataFrame({
        "time": pd.DatetimeIndex(times), "open": openc, "high": high,
        "low": low, "close": close,
        "tick_volume": 10, "spread": 5, "real_volume": 0})


def synth_bars(n_hours, *, seed=7, start="2021-01-04", s0=1800.0):
    """连续小时随机游走 K 线（start=周一）。"""
    return synth_bars_at(pd.date_range(start, periods=n_hours, freq="1h"),
                         seed=seed, s0=s0)


def _trig_at(times, level=100.0):
    times = pd.DatetimeIndex(times)
    return pd.DataFrame({"time": times, "situation": "S1", "action": "A3",
                         "side": 1, "level": level})


@pytest.fixture
def small_pc(monkeypatch):
    """缩小设计常量的脚本模块（manifest/merge 测试用）。"""
    pc = load_pc()
    monkeypatch.setattr(pc, "NEG_NS", (5, 6))   # neg-holm 用 NEG_NS[1]
    monkeypatch.setattr(pc, "NEG_SEEDS", 2)
    monkeypatch.setattr(pc, "POS_DELTAS", (0.02,))
    monkeypatch.setattr(pc, "POS_NS", (5,))
    monkeypatch.setattr(pc, "POS_SEEDS", 2)
    monkeypatch.setattr(pc, "CLUSTER_SHIFT_WEEKS", (3,))
    monkeypatch.setattr(pc, "CLUSTER_SEEDS", 2)
    monkeypatch.setattr(pc, "BRIER_SEEDS", 2)
    return pc


# ---------------------------------------------------------------- P2-06 平移

def test_shift_weeks_ny_preserves_local_hour_weekday_across_dst():
    pc = load_pc()
    # 2021-03-12 14:00 UTC = 周五 09:00 EST；+2 周跨 DST（03-14 拨快）→
    # 2021-03-26 09:00 EDT = 13:00 UTC（本地小时/星期保持）
    r = pc._shift_weeks_ny(pd.Series([pd.Timestamp("2021-03-12 14:00")]), 2)
    assert r.iloc[0] == pd.Timestamp("2021-03-26 13:00")
    assert r.iloc[0].dayofweek == pd.Timestamp("2021-03-12 14:00").dayofweek


def test_shift_weeks_ny_handles_nonexistent_and_ambiguous_local():
    pc = load_pc()
    # 不存在的本地时刻：2021-03-14 02:30 NY（春季 02:00→03:00 被跳过）
    # → 顺延到第一个存在时刻 03:00 EDT = 07:00 UTC
    r = pc._shift_weeks_ny(pd.Series([pd.Timestamp("2021-03-07 07:30")]), 1)
    assert r.iloc[0] == pd.Timestamp("2021-03-14 07:00")
    # 歧义本地时刻：2021-11-07 01:30 出现两次（秋季拨回）→ 取较早
    # （01:30 EDT = 05:30 UTC）
    r2 = pc._shift_weeks_ny(pd.Series([pd.Timestamp("2021-10-31 05:30")]), 1)
    assert r2.iloc[0] == pd.Timestamp("2021-11-07 05:30")


def test_shifted_triggers_drops_out_of_range_not_nearest():
    """平移后晚于末根的触发丢弃——绝不 nearest 堆到末端 bar（越界丢弃
    而非末端堆积是 P-02 的审核要求；前向平移下"早于首根"分支为防御）。"""
    pc = load_pc()
    idx = pd.Index(pd.date_range("2021-01-04", periods=24 * 7 * 12, freq="1h"))
    trig = pd.DataFrame({
        "time": [idx[-5], idx[10]],
        "situation": "S1", "action": "A3", "side": 1, "level": 100.0})
    out, audit = pc._shifted_triggers(trig, idx, weeks=3, perm_state=11)
    assert len(out) == 1                       # idx[-5]+3周越界 → 丢弃
    assert idx[-1] not in set(out["time"])     # 不堆到末根
    assert out["time"].iloc[0] == idx[10 + 3 * 168]   # 整周=168 小时精确落 bar
    assert audit["drops"]["gap"] == 1
    # 防御分支：平移后早于首根也丢弃（合成触发早于序列起点）
    trig_early = pd.DataFrame({
        "time": [pd.Timestamp("2020-12-14 01:00")],   # 序列前 3 周
        "situation": "S1", "action": "A3", "side": 1, "level": 100.0})
    out2, _ = pc._shifted_triggers(trig_early, idx, weeks=1, perm_state=11)
    assert len(out2) == 0


def test_shifted_triggers_internal_gap_dropped_not_snapped():
    """P2-06 核心：平移落入内部长洞 → 丢弃该触发，绝不 nearest 吸到
    洞边（更不能映回原始触发时刻——audit2 探针 cluster_shift 口径）。"""
    pc = load_pc()
    idx = pd.DatetimeIndex(["2024-01-01 15:00", "2024-03-01 15:00",
                            "2024-06-01 15:00"])
    trig = pd.DataFrame({"time": [idx[0]], "side": 1})
    out, audit = pc._shifted_triggers(trig, pd.Index(idx), weeks=3,
                                      perm_state=0)
    assert len(out) == 0                       # 1/22 落在 1/1 与 3/1 的洞里
    assert idx[0] not in set(out.get("time", []))
    assert audit["drops"]["gap"] == 1
    assert audit["n_kept"] == 0


def test_shifted_triggers_periodic_collision_dropped():
    """周期性原始触发：+3 周精确落在另一原始触发时刻 → 丢弃（两组的
    标签时刻都不得等于任何原始触发时刻——最终 disjoint）。"""
    pc = load_pc()
    idx = pd.Index(pd.date_range("2021-01-04", periods=24 * 7 * 20, freq="1h"))
    t0, t1 = idx[100], idx[100 + 3 * 168]      # 相隔恰 3 周
    trig = _trig_at([t0, t1], level=100.0)
    out, audit = pc._shifted_triggers(trig, idx, weeks=3, perm_state=1)
    # t0+3w == t1（原始时刻）→ 丢弃；t1+3w 正常存活
    assert set(out["time"]) == {idx[100 + 6 * 168]}
    assert audit["drops"]["orig_collision"] == 1
    assert not (set(out["time"]) & {t0, t1})   # 最终 disjoint


def test_shifted_triggers_calendar_mismatch_dropped():
    """容差内吸附但 NY 墙钟变化（分钟/小时）→ 丢弃：平移后重证本地
    小时/分钟/星期，不 silently 接受被吸附改变的时刻。"""
    pc = load_pc()
    idx = pd.Index(pd.date_range("2021-01-04 10:30", periods=24 * 7 * 8,
                                 freq="1h"))
    trig = _trig_at([pd.Timestamp("2021-01-04 10:00")])   # 不在 bar 网格上
    out, audit = pc._shifted_triggers(trig, idx, weeks=1, perm_state=3)
    # +1 周=10:00，最近 bar 10:30（30 分钟 ≤ ±1 bar 容差）→ 吸附改变
    # NY 分钟 → 重证失败丢弃
    assert len(out) == 0
    assert audit["drops"]["calendar"] == 1
    assert audit["drops"]["gap"] == 0


def test_shifted_triggers_dst_cross_exact_hit_kept_with_wall_preserved():
    """网格内精确命中且跨 DST → 存活，NY 小时/分钟/星期不变。"""
    pc = load_pc()
    idx = pd.Index(pd.date_range("2021-01-04", periods=24 * 7 * 20, freq="1h"))
    t = idx[50]
    out, audit = pc._shifted_triggers(_trig_at([t]), idx, weeks=12,
                                      perm_state=0)
    assert len(out) == 1 and audit["drops"]["gap"] == 0
    mapped = pd.Timestamp(out["time"].iloc[0])
    ny0 = t.tz_localize("UTC").tz_convert("America/New_York")
    ny1 = mapped.tz_localize("UTC").tz_convert("America/New_York")
    assert (ny0.hour, ny0.minute, ny0.weekday()) == \
        (ny1.hour, ny1.minute, ny1.weekday())


def test_shifted_triggers_atr_eligibility():
    """映射 bar 的 ATR 必须可算（回看不足/NaN → 丢弃该触发）。"""
    pc = load_pc()
    n = 24 * 7 * 20
    idx = pd.Index(pd.date_range("2021-01-04", periods=n, freq="1h"))
    atr = np.full(n, 10.0)
    atr[:20] = np.nan
    out, audit = pc._shifted_triggers(_trig_at([idx[100]]), idx, weeks=3,
                                      perm_state=0, atr=atr)
    assert len(out) == 1 and audit["drops"]["atr_na"] == 0
    out2, audit2 = pc._shifted_triggers(_trig_at([idx[5]]), idx, weeks=3,
                                        perm_state=0, atr=atr)
    assert len(out2) == 0 and audit2["drops"]["atr_na"] == 1


def test_shift_match_audit_rejects_degraded_transform():
    """桶匹配退化按预写规则拒绝变换：ATR 桶分布在样本充足
    （≥ MATCH_AUDIT_MIN_N）时 TV 超阈值 → 整组拒绝（空标签）；
    样本不足 → 仅审计不拒绝。"""
    pc = load_pc()
    n = 24 * 7 * 30
    idx = pd.Index(pd.date_range("2021-01-04", periods=n, freq="1h"))
    # ATR 沿序列线性抬升：前段低档、+20 周后高档 → 整组桶全变（TV≈1）
    atr = np.linspace(1.0, 50.0, n)
    n_big = pc.MATCH_AUDIT_MIN_N + 20
    trig = _trig_at(idx[:n_big], level=100.0)
    out, audit = pc._shifted_triggers(trig, idx, weeks=20, perm_state=0,
                                      atr=atr)
    assert audit["bucket_check"] == "enforced"
    assert audit["tv_atr"] > pc.ATR_TV_MAX
    assert audit["rejected"] is True
    assert audit["reject_reason"]
    assert len(out) == 0                       # 拒绝变换 → 整组丢弃
    # 同构造但样本 < MATCH_AUDIT_MIN_N → 只记录不拒绝
    out2, audit2 = pc._shifted_triggers(trig.iloc[:10], idx, weeks=20,
                                        perm_state=0, atr=atr)
    assert audit2["bucket_check"] == "audit_only_low_n"
    assert audit2["rejected"] is False
    assert len(out2) == 10


def _patch_cluster_heavy(monkeypatch, pc, bars, trig):
    """把计数/触发/标签/检验全部换成替身；标签器为捕获式。"""
    calls: list[list] = []

    def spy_label(bars_arg, trig_arg, costs, **kw):
        calls.append(list(pd.to_datetime(trig_arg["time"])))
        out = trig_arg.copy()
        rng = np.random.default_rng(1000 + len(trig_arg))
        out["outcome"] = np.where(rng.random(len(trig_arg)) < 0.4,
                                  "win", "loss")
        out["net_r"] = 1.9
        return out

    monkeypatch.setattr(pc, "label_triggers", spy_label)
    monkeypatch.setattr(pc, "_costs_and_sizes",
                        lambda swap_ref_price=None: (None, None, 1.0, "v"))
    monkeypatch.setattr(pc, "delta_p_test",
                        lambda real, null, *, block=None, sims, seed: {
                            "p_value": 0.5, "delta_p": 0.0,
                            "n_days_real": 4, "n_days_null": 4})
    monkeypatch.setattr(
        "mt5quant.prob.counting.level_rows_for_situation",
        lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr(
        "mt5quant.strategies.library.prob.situations.find_level_events",
        lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr(
        "mt5quant.strategies.library.prob.actions.action_triggers",
        lambda *a, **k: trig)
    stub_rb = types.SimpleNamespace(load_series=lambda *a, **k: bars)
    monkeypatch.setitem(sys.modules, "run_backtest", stub_rb)
    return calls


def test_phase_cluster_labels_only_at_shifted_times(monkeypatch):
    """捕获式标签器：real 与 null 两组的调用时刻均不含原始触发时刻。"""
    pc = load_pc()
    # 62 周小时线（≥ 最大平移 3+51=54 周 + 原始触发跨度 → 不整组越界）；
    # 原始触发集中在前 3 周 → 最小平移 3 周后必然落在全部原始时刻之后
    # （整周平移保 NY 小时/星期 → 时刻不可能重合）。
    bars = synth_bars(24 * 7 * 62)
    times = pd.to_datetime(bars["time"])
    trig = pd.DataFrame({
        "time": times.iloc[::11].iloc[:60],     # 前 660 小时（<3 周）
        "situation": "S1", "action": "A3", "side": 1, "level": 100.0})
    calls = _patch_cluster_heavy(monkeypatch, pc, bars, trig)
    monkeypatch.setattr(pc, "CLUSTER_SHIFT_WEEKS", (3,))
    monkeypatch.setattr(pc, "CLUSTER_SEEDS", 4)
    rows = pc.phase_cluster(0, 1, "unused-stamp")
    assert len(rows) == 4
    assert len(calls) == 8                      # 每 seed real+null 两次
    original = set(pd.to_datetime(trig["time"]))
    bar_set = set(times)
    for i, call_times in enumerate(calls):
        grp = "real" if i % 2 == 0 else "null"
        assert not (set(call_times) & original), (
            f"第 {grp} 组（第 {i // 2 + 1} 个 seed）出现了原始触发时刻")
        assert set(call_times) <= bar_set        # 全部映射到真实 bar
    # null 非原始时刻子集（旧实现的嵌套弱对照已删）：两组都是平移后
    # 时刻，且不同 seed 的独立平移产生不同时刻集
    pairs = [(calls[i], calls[i + 1]) for i in range(0, len(calls), 2)]
    assert any(set(a) != set(b) for a, b in pairs)
    assert all(r["kind"] == "cluster_neg" and "p" in r for r in rows)
    # P2-06：行内带覆盖损失与匹配桶审计（两组各自）
    for r in rows:
        for grp in ("audit_real", "audit_null"):
            a = r[grp]
            assert a["n_orig"] == len(trig)
            assert a["n_kept"] + sum(a["drops"].values()) >= a["n_orig"]
            assert a["bucket_check"] == "audit_only_low_n"   # 60 < 100
            assert a["rejected"] is False
        assert isinstance(r["n_overlap_real_null"], int)


def test_phase_cluster_internal_hole_triggers_dropped(monkeypatch):
    """内部 30 天洞：整组平移落入洞内 → 全部丢弃，标签器永远拿不到
    洞内或原始时刻；行内记录覆盖损失（gap 丢弃数）。"""
    pc = load_pc()
    head = pd.date_range("2021-01-04", periods=24 * 7 * 8, freq="1h")
    tail = pd.date_range(head[-1] + pd.Timedelta(days=30),
                         periods=24 * 7 * 8, freq="1h")
    bars = synth_bars_at(head.append(tail))
    times = pd.DatetimeIndex(bars["time"])
    hole_lo, hole_hi = head[-1], tail[0]
    # 触发在第 600..639 小时：+5 周（840h）→ 全部落入洞内
    trig = _trig_at(times[600:640], level=100.0)
    calls = _patch_cluster_heavy(monkeypatch, pc, bars, trig)
    monkeypatch.setattr(pc, "CLUSTER_SHIFT_WEEKS", (5,))
    monkeypatch.setattr(pc, "CLUSTER_SEEDS", 2)
    monkeypatch.setattr(pc, "CLUSTER_EXTRA_WEEKS_MAX", 0)
    rows = pc.phase_cluster(0, 1, "unused-stamp")
    assert len(rows) == 2
    assert all(len(c) == 0 for c in calls)      # 无任何标签调用时刻
    for r in rows:
        assert r["n_real"] == 0 and r["n_null"] == 0
        assert r["audit_real"]["drops"]["gap"] == 40
        assert r["audit_null"]["drops"]["gap"] == 40
    # 洞内时刻确实不是 bar（夹具自证）
    assert not any(hole_lo < t < hole_hi for t in times)


# ---------------------------------------------------------------- P-03 样本单位

def test_phase_neg_pos_record_actual_day_counts(monkeypatch):
    """名义 n ≠ 实际日数：记录 delta_p_test 返回的 n_days_real/null。"""
    pc = load_pc()
    monkeypatch.setattr(pc, "NEG_NS", (7,))
    monkeypatch.setattr(pc, "NEG_SEEDS", 1)
    monkeypatch.setattr(pc, "POS_DELTAS", (0.02,))
    monkeypatch.setattr(pc, "POS_NS", (7,))
    monkeypatch.setattr(pc, "POS_SEEDS", 1)
    monkeypatch.setattr(pc, "B_SIMS", 3)
    monkeypatch.setattr(pc, "delta_p_test",
                        lambda real, null, *, block=None, sims, seed: {
                            "p_value": 0.25, "delta_p": 0.01,
                            "n_days_real": 5, "n_days_null": 6})
    monkeypatch.setattr(pc, "plant_delta", lambda labels, d, seed: labels)
    pool_days = pd.DataFrame({
        "day": [pd.Timestamp("2021-01-04").date(),
                pd.Timestamp("2021-01-05").date()],
        "winrate": [0.4, 0.6]})
    neg = pc.phase_neg(pool_days, 0, 1, "t")
    assert neg[0]["n"] == 7                     # 名义 n
    assert neg[0]["n_days_real"] == 5           # 实际日数（≠名义）
    assert neg[0]["n_days_null"] == 6
    pos = pc.phase_pos(pool_days, 0, 1)
    assert pos[0]["n"] == 7
    assert pos[0]["n_days_real"] == 5
    assert pos[0]["n_days_null"] == 6


# ---------------------------------------------------------------- P2-07 指纹

def test_pool_content_fingerprint_exact_and_sensitive():
    """池内容指纹 = 精确有序内容 hash：同内容同 hash（行序重排不变），
    日期平移/赢输时间结构反转/单标签/净 R 改动各自变 hash。"""
    pc = load_pc()
    a = pd.DataFrame({"time": pd.date_range("2024-01-01", periods=4),
                      "outcome": ["win", "win", "loss", "loss"]})
    fpa = pc._pool_content_fingerprint(a)
    assert fpa == pc._pool_content_fingerprint(a.copy())
    # 行序重排（同一组 (time, outcome) 配对）→ 固定排序键下同 hash
    assert fpa == pc._pool_content_fingerprint(a.iloc[[2, 3, 0, 1]])
    # 日期整体后移 → 变（audit2 探针 calibration_identity 的日期分量）
    b = a.copy()
    b["time"] += pd.Timedelta(days=120)
    assert fpa != pc._pool_content_fingerprint(b)
    # 赢输时间结构反转（日期不动）→ 变
    c = a.copy()
    c["outcome"] = ["loss", "loss", "win", "win"]
    assert fpa != pc._pool_content_fingerprint(c)
    # 探针口径（日期 + 赢输结构同时变）→ 变
    d = a.copy()
    d["outcome"] = ["loss", "loss", "win", "win"]
    d["time"] += pd.Timedelta(days=120)
    assert fpa != pc._pool_content_fingerprint(d)
    # 单标签改动 → 变
    e = a.copy()
    e.loc[1, "outcome"] = "loss"
    assert fpa != pc._pool_content_fingerprint(e)
    # 成本口径经 net_r 入指纹 → 变
    g = a.assign(net_r=1.9)
    h = g.copy()
    h.loc[0, "net_r"] = 1.5
    assert pc._pool_content_fingerprint(g) != pc._pool_content_fingerprint(h)
    assert pc._pool_content_fingerprint(g) != fpa


def test_build_pool_writes_content_hash_and_freezes_cluster_inputs(
        monkeypatch, tmp_path):
    """build_pool：meta 记录内容 hash（重算池可复算一致）；S1 触发一次
    提取并冻结（bars+triggers 落盘 + 字节 hash）；swap_ref_price 口径。"""
    pc = load_pc()
    bars = synth_bars(240)
    monkeypatch.setattr(pc, "OUT_ROOT", tmp_path)
    monkeypatch.setattr(pc, "N_POOL", 40)
    monkeypatch.setattr(pc, "_load_bars", lambda: bars)
    captured: dict = {}

    def fake_costs(swap_ref_price=None):
        captured["swap_ref_price"] = swap_ref_price
        return (None, None, 1.0, "vtest")

    monkeypatch.setattr(pc, "_costs_and_sizes", fake_costs)

    def fake_label(bars_arg, trig_arg, costs, **kw):
        rng = np.random.default_rng(99)
        out = trig_arg.copy()
        out["outcome"] = np.where(rng.random(len(out)) < 0.5, "win", "loss")
        out["net_r"] = 1.9
        return out

    monkeypatch.setattr(pc, "label_triggers", fake_label)
    frozen_trig = _trig_at(bars["time"].iloc[[30, 80, 150]], level=100.0)
    monkeypatch.setattr(pc, "_extract_s1_triggers", lambda b: frozen_trig)
    pc.build_pool("st1")
    d = tmp_path / "st1"
    meta = json.loads((d / "pool.meta.json").read_text(encoding="utf-8"))
    pool = pd.read_parquet(d / "pool.parquet")
    assert meta["pool_content_sha256"] == pc._pool_content_fingerprint(pool)
    # 冻结的 cluster 输入：bars + triggers + 字节 hash
    cb = pd.read_parquet(d / "cluster-bars.parquet")
    ct = pd.read_parquet(d / "cluster-triggers.parquet")
    pd.testing.assert_frame_equal(cb, bars)
    assert list(ct["time"]) == list(frozen_trig["time"])
    cmeta = json.loads((d / "cluster-inputs.meta.json").read_text(
        encoding="utf-8"))
    assert cmeta["bars_sha256"] == hashlib.sha256(
        (d / "cluster-bars.parquet").read_bytes()).hexdigest()
    assert cmeta["triggers_sha256"] == hashlib.sha256(
        (d / "cluster-triggers.parquet").read_bytes()).hexdigest()
    assert cmeta["n_triggers"] == len(frozen_trig)
    # build_pool 成本口径：swap_ref_price = 全序列最后收盘（与 P1 一致）
    assert captured["swap_ref_price"] == float(bars["close"].iloc[-1])


def test_costs_and_sizes_passes_swap_ref_price(monkeypatch):
    """build_costs 收到 swap_ref_price（全序列最后收盘价，P1 同口径）。"""
    pc = load_pc()
    captured: dict = {}

    def fake_build_costs(data_root, symbol, spread, commission, **kw):
        captured.update(kw)
        return ("costs", "ver1")

    stub = types.SimpleNamespace(
        build_costs=fake_build_costs, SESSIONS_PATH=Path("sessions.toml"),
        entry_policy_for=lambda *a, **k: "pol",
        resolve_contract_size=lambda *a, **k: 100.0)
    monkeypatch.setitem(sys.modules, "run_backtest", stub)
    monkeypatch.setattr("mt5data.sessions.load_session_policy",
                        lambda p: "sess")
    costs, policy, cs, ver = pc._costs_and_sizes(swap_ref_price=2345.6)
    assert ver == "ver1"
    assert captured["swap_ref_price"] == 2345.6
    assert captured["research_store"] is True


# ------------------------------------------------------- P2-07 manifest/运行

def _mk_pool_frame(n=12):
    rng = np.random.default_rng(5)
    return pd.DataFrame({
        "time": pd.date_range("2021-01-04", periods=n, freq="37h"),
        "outcome": np.where(rng.random(n) < 0.5, "win", "loss"),
        "net_r": 1.9, "situation": "S1", "action": "A3", "side": 1,
        "level": 0.0})


def _mk_stamp_env(pc, tmp_path, *, stamp="st", pool=None, cost_ver="vtest",
                  cluster_bars=None, cluster_triggers=None):
    """构造一个可被 cmd_run/cmd_merge 校验通过的 stamp 目录（pool +
    meta + 冻结 cluster 输入）。"""
    d = tmp_path / stamp
    d.mkdir(parents=True, exist_ok=True)
    pool = _mk_pool_frame() if pool is None else pool
    pool.to_parquet(d / "pool.parquet", index=False)
    meta = {"pool_seed": pc.POOL_SEED, "n_pool": int(len(pool)),
            "cost_ver": cost_ver,
            "bracket": {"sl_atr": pc.BRACKET.sl_atr,
                        "tp_atr": pc.BRACKET.tp_atr,
                        "horizon_bars": pc.BRACKET.horizon_bars},
            "pool_content_sha256": pc._pool_content_fingerprint(pool)}
    (d / "pool.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    bars = cluster_bars if cluster_bars is not None else synth_bars(24 * 7 * 20)
    trig = cluster_triggers if cluster_triggers is not None else _trig_at(
        bars["time"].iloc[::200], level=100.0)
    bp, tp = d / "cluster-bars.parquet", d / "cluster-triggers.parquet"
    bars.to_parquet(bp, index=False)
    trig.to_parquet(tp, index=False)
    cmeta = {"bars_sha256": hashlib.sha256(bp.read_bytes()).hexdigest(),
             "triggers_sha256": hashlib.sha256(tp.read_bytes()).hexdigest(),
             "n_triggers": int(len(trig))}
    (d / "cluster-inputs.meta.json").write_text(json.dumps(cmeta),
                                                encoding="utf-8")
    return stamp


def test_manifest_binds_full_context_and_is_sensitive(small_pc, monkeypatch,
                                                      tmp_path):
    """manifest v2 绑定池内容/成本/政策/来源快照/方法/依赖/冻结输入；
    任一来源变化 → 指纹变化。"""
    pc = small_pc
    stamp = _mk_stamp_env(pc, tmp_path)
    d = tmp_path / stamp
    m1 = pc._build_manifest(d)
    assert m1["version"] == pc.MANIFEST_VERSION == 2
    b = m1["bindings"]
    for key in ("pool_content_sha256", "cost_ver", "sessions_sha256",
                "data_snapshot", "method", "deps", "cluster_inputs"):
        assert key in b, key
    assert b["method"]["block"] == pc.BLOCK
    assert "scripts/prob_calibration.py" in b["deps"]
    assert b["cluster_inputs"]["n_triggers"] > 0
    # 确定性：同输入重建完全一致（含 fingerprint）
    assert m1 == pc._build_manifest(d)

    def _fp_after():
        m = pc._build_manifest(d)
        assert "fingerprint" in m
        return m["fingerprint"]

    fp1 = m1["fingerprint"]
    # 池内容变化（parquet 与 meta 同步换）→ 变
    pool2 = _mk_pool_frame()
    pool2.loc[0, "outcome"] = "time"
    pool2.to_parquet(d / "pool.parquet", index=False)
    meta = json.loads((d / "pool.meta.json").read_text(encoding="utf-8"))
    meta["pool_content_sha256"] = pc._pool_content_fingerprint(pool2)
    (d / "pool.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    assert _fp_after() != fp1
    # 成本版本变化 → 变
    meta["cost_ver"] = "vother"
    (d / "pool.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    assert _fp_after() != fp1
    # sessions 政策变化 → 变
    monkeypatch.setattr(pc, "_sessions_sha", lambda: "0" * 64)
    assert _fp_after() != fp1
    # 依赖清单变化 → 变
    monkeypatch.setattr(pc, "_deps_hashes",
                        lambda: {"scripts/prob_calibration.py": "0" * 64})
    assert _fp_after() != fp1
    # 来源行情快照变化 → 变
    monkeypatch.setattr(pc, "_data_snapshot",
                        lambda: {"data_sha256": "0" * 64})
    assert _fp_after() != fp1
    # 方法/设计常量变化（brier 种子数 → job 集也变）→ 变
    monkeypatch.setattr(pc, "BRIER_SEEDS", pc.BRIER_SEEDS + 1)
    assert _fp_after() != fp1


def test_deps_hashes_and_data_snapshot_refuse_missing(small_pc, monkeypatch,
                                                      tmp_path):
    pc = small_pc
    hashes = pc._deps_hashes()
    assert len(hashes) == len(pc.DEP_FILES)
    assert all(len(v) == 64 for v in hashes.values())
    monkeypatch.setattr(pc, "DEP_FILES", ("scripts/__nope__.py",))
    with pytest.raises(RuntimeError):
        pc._deps_hashes()
    monkeypatch.setattr(pc, "REPO", tmp_path)     # 无 data_research meta
    with pytest.raises(RuntimeError):
        pc._data_snapshot()
    root = tmp_path / "data_research" / "bars" / pc.SYMBOL / pc.TF
    root.mkdir(parents=True)
    (root / "meta.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(RuntimeError):
        pc._data_snapshot()


def test_cmd_run_refuses_missing_or_stale_pool_meta(monkeypatch, tmp_path,
                                                    capsys):
    """P2-07：meta 缺/坏/内容对不上 → 拒绝运行（不退空串）。"""
    pc = load_pc()
    monkeypatch.setattr(pc, "OUT_ROOT", tmp_path)
    monkeypatch.setattr(pc, "BRIER_SEEDS", 2)
    stamp = _mk_stamp_env(pc, tmp_path)
    d = tmp_path / stamp
    args = types.SimpleNamespace(phase="brier", shard_i=0, shard_n=1,
                                 stamp=stamp)
    assert pc.cmd_run(args) == 0                 # 完整环境可运行
    (d / "manifest.json").unlink()
    (d / "pool.meta.json").unlink()
    assert pc.cmd_run(args) == 1
    assert "pool.meta.json" in capsys.readouterr().out
    (d / "pool.meta.json").write_text("{broken", encoding="utf-8")
    assert pc.cmd_run(args) == 1
    assert "pool.meta.json" in capsys.readouterr().out
    # 池被替换而 meta 未更新 → 内容 hash 对不上 → 拒绝
    _mk_stamp_env(pc, tmp_path, stamp=stamp)
    if (d / "manifest.json").is_file():
        (d / "manifest.json").unlink()
    pool2 = _mk_pool_frame()
    pool2.loc[0, "outcome"] = "time" if pool2.loc[0, "outcome"] != "time" \
        else "win"
    pool2.to_parquet(d / "pool.parquet", index=False)
    assert pc.cmd_run(args) == 1
    out = capsys.readouterr().out
    assert "不一致" in out


def test_cmd_run_cluster_reads_frozen_inputs_not_library(monkeypatch,
                                                         tmp_path):
    """cluster 相位读冻结产物：研究库装载与 S1 计数一旦被触到立即失败。"""
    pc = load_pc()
    monkeypatch.setattr(pc, "OUT_ROOT", tmp_path)
    bars = synth_bars(24 * 7 * 20)
    trig = _trig_at(bars["time"].iloc[[300, 700, 900]],
                    level=100.0)
    stamp = _mk_stamp_env(pc, tmp_path, cluster_bars=bars,
                          cluster_triggers=trig)

    def boom_load(*a, **k):
        raise AssertionError("cluster 相位重读了研究库")

    monkeypatch.setitem(sys.modules, "run_backtest",
                        types.SimpleNamespace(load_series=boom_load))

    def boom_count(*a, **k):
        raise AssertionError("cluster 相位重跑了 S1 触发计数")

    monkeypatch.setattr("mt5quant.prob.counting.level_rows_for_situation",
                        boom_count)
    calls: list = []

    def fake_label(bars_arg, trig_arg, costs, **kw):
        calls.append(list(pd.to_datetime(trig_arg["time"])))
        out = trig_arg.copy()
        out["outcome"] = "win"
        out["net_r"] = 1.0
        return out

    monkeypatch.setattr(pc, "label_triggers", fake_label)
    monkeypatch.setattr(pc, "_costs_and_sizes",
                        lambda swap_ref_price=None: (None, None, 1.0, "v"))
    monkeypatch.setattr(pc, "delta_p_test",
                        lambda real, null, *, block=None, sims, seed: {
                            "p_value": 0.5, "delta_p": 0.0,
                            "n_days_real": len(real),
                            "n_days_null": len(null)})
    monkeypatch.setattr(pc, "CLUSTER_SHIFT_WEEKS", (3,))
    monkeypatch.setattr(pc, "CLUSTER_SEEDS", 2)
    monkeypatch.setattr(pc, "CLUSTER_EXTRA_WEEKS_MAX", 2)
    args = types.SimpleNamespace(phase="cluster", shard_i=0, shard_n=1,
                                 stamp=stamp)
    assert pc.cmd_run(args) == 0
    shard = json.loads((tmp_path / stamp / "cluster-s0of1.json")
                       .read_text(encoding="utf-8"))
    assert len(shard["rows"]) == 2
    manifest = json.loads((tmp_path / stamp / "manifest.json")
                          .read_text(encoding="utf-8"))
    assert shard["fingerprint"] == manifest["fingerprint"]
    frozen_times = set(pd.DatetimeIndex(trig["time"]))
    bar_times = set(pd.DatetimeIndex(bars["time"]))
    assert calls and all(set(c) <= bar_times for c in calls)
    assert not any(set(c) & frozen_times for c in calls)


def test_cmd_run_cluster_rejects_tampered_frozen_inputs(monkeypatch,
                                                        tmp_path, capsys):
    pc = load_pc()
    monkeypatch.setattr(pc, "OUT_ROOT", tmp_path)
    bars = synth_bars(24 * 7 * 20)
    trig = _trig_at(bars["time"].iloc[[300, 700, 900]],
                    level=100.0)
    stamp = _mk_stamp_env(pc, tmp_path, cluster_bars=bars,
                          cluster_triggers=trig)
    d = tmp_path / stamp
    # 冻结 bars 被改动（meta hash 不再匹配实际文件）→ 拒绝
    tampered = bars.copy()
    tampered.loc[len(tampered) - 1, "close"] += 1.0
    tampered.to_parquet(d / "cluster-bars.parquet", index=False)
    monkeypatch.setattr(pc, "phase_cluster",
                        lambda *a, **k: pytest.fail("不应跑到 cluster 相位"))
    args = types.SimpleNamespace(phase="cluster", shard_i=0, shard_n=1,
                                 stamp=stamp)
    assert pc.cmd_run(args) == 1
    assert "cluster-inputs" in capsys.readouterr().out


def test_cmd_run_writes_idempotent_manifest(monkeypatch, tmp_path):
    pc = load_pc()
    monkeypatch.setattr(pc, "NEG_NS", (5, 6))
    monkeypatch.setattr(pc, "NEG_SEEDS", 2)
    monkeypatch.setattr(pc, "POS_DELTAS", (0.02,))
    monkeypatch.setattr(pc, "POS_NS", (5,))
    monkeypatch.setattr(pc, "POS_SEEDS", 2)
    monkeypatch.setattr(pc, "CLUSTER_SHIFT_WEEKS", (3,))
    monkeypatch.setattr(pc, "CLUSTER_SEEDS", 2)
    monkeypatch.setattr(pc, "BRIER_SEEDS", 2)
    monkeypatch.setattr(pc, "OUT_ROOT", tmp_path)
    stamp = _mk_stamp_env(pc, tmp_path, stamp="t1")
    got_frozen: list = []

    def fake_cluster(i, n, s, *, frozen=None):
        got_frozen.append(frozen)
        return [{"kind": "cluster_neg", "weeks": 3, "seed": 0,
                 "n_real": 1, "n_null": 1, "p": 0.5},
                {"kind": "cluster_neg", "weeks": 3, "seed": 1,
                 "n_real": 1, "n_null": 1, "p": 0.5}]

    monkeypatch.setattr(pc, "phase_cluster", fake_cluster)
    args = types.SimpleNamespace(phase="cluster", shard_i=0, shard_n=1,
                                 stamp=stamp)
    assert pc.cmd_run(args) == 0
    mpath = tmp_path / "t1" / "manifest.json"
    assert mpath.is_file()
    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    assert manifest == pc._build_manifest(tmp_path / "t1")
    # cluster 收到的是冻结输入（bars+triggers 元组，来自冻结产物）
    assert got_frozen and got_frozen[0] is not None
    bars_f, trig_f = got_frozen[0]
    assert len(trig_f) == 17 and len(bars_f) == 24 * 7 * 20
    # 重跑（manifest 一致）幂等通过；篡改 → 拒绝
    assert pc.cmd_run(args) == 0
    mpath.write_text(json.dumps({"version": 2, "jobs": ["x"]}),
                     encoding="utf-8")
    assert pc.cmd_run(args) == 1


# ---------------------------------------------------------------- P-12 manifest

def _mk_rows_from_manifest(pc):
    """按 manifest 键重建全量合法行（带 merge 汇总所需的结果字段）。"""
    rows = []
    for key in pc._job_manifest()["jobs"]:
        parts = key.split("|")
        kind = parts[0]
        if kind == "neg_single":
            rows.append({"kind": kind, "n": int(parts[1]),
                         "seed": int(parts[2]), "p": 0.5,
                         "n_days_real": 3, "n_days_null": 4})
        elif kind == "neg_holm":
            rows.append({"kind": kind, "n": 5, "seed": int(parts[1]),
                         "reject_any": False})
        elif kind == "pos":
            rows.append({"kind": kind, "delta": float(parts[1]),
                         "n": int(parts[2]), "seed": int(parts[3]),
                         "p": 0.5, "dp_hat": 0.0,
                         "n_days_real": 3, "n_days_null": 4})
        elif kind == "cluster_neg":
            rows.append({"kind": kind, "weeks": int(parts[1]),
                         "seed": int(parts[2]), "n_real": 10, "n_null": 10,
                         "p": 0.5})
        elif kind == "brier":
            rows.append({"kind": kind, "seed": int(parts[1]),
                         "s_uninformative": 0.0, "s_planted": 1.0})
        else:
            raise AssertionError(kind)
    return rows


def _write_stamp_dir(pc, tmp_path, rows_by_file, *, manifest="auto",
                     stamp="st", fp_override=None):
    """构造完整可合并目录：pool/meta/冻结输入 + manifest + 分片。"""
    _mk_stamp_env(pc, tmp_path, stamp=stamp)
    d = tmp_path / stamp
    m = pc._build_manifest(d) if manifest == "auto" else manifest
    (d / "manifest.json").write_text(
        json.dumps(m, ensure_ascii=False), encoding="utf-8")
    fp = m["fingerprint"] if fp_override is None else fp_override
    for name, rows in rows_by_file.items():
        (d / name).write_text(json.dumps(
            {"phase": name.split("-s")[0], "shard": [0, 1],
             "fingerprint": fp, "B": 3, "rows": rows}), encoding="utf-8")
    return stamp


def test_job_manifest_is_complete_product(small_pc):
    m = small_pc._job_manifest()
    assert m["version"] == small_pc.MANIFEST_VERSION
    # neg 2×2 + holm 2 + pos 1×1×2 + cluster 1×2 + brier 2 = 12，无重复
    assert len(m["jobs"]) == len(set(m["jobs"])) == 12


def test_merge_complete_ok_and_keeps_pool_meta(small_pc, monkeypatch,
                                               tmp_path):
    pc = small_pc
    monkeypatch.setattr(pc, "OUT_ROOT", tmp_path)
    rows = _mk_rows_from_manifest(pc)
    stamp = _write_stamp_dir(pc, tmp_path, {
        "neg-s0of1.json": [r for r in rows if r["kind"] == "neg_single"],
        "neg-holm-s0of1.json": [r for r in rows if r["kind"] == "neg_holm"],
        "pos-s0of1.json": [r for r in rows if r["kind"] == "pos"],
        "cluster-s0of1.json": [r for r in rows if r["kind"] == "cluster_neg"],
        "brier-s0of1.json": [r for r in rows if r["kind"] == "brier"]})
    pool_dir = tmp_path / stamp
    rc = pc.cmd_merge(types.SimpleNamespace(stamp=stamp, keep_pool=False))
    assert rc == 0
    assert (pool_dir / "summary.json").is_file()
    assert not (pool_dir / "pool.parquet").is_file()   # 标签池仍删
    # 来源证据保留：pool meta、manifest、冻结 cluster 输入（P-12/P2-07）
    assert (pool_dir / "pool.meta.json").is_file()
    assert (pool_dir / "manifest.json").is_file()
    assert (pool_dir / "cluster-inputs.meta.json").is_file()
    assert (pool_dir / "cluster-bars.parquet").is_file()
    summary = json.loads((pool_dir / "summary.json").read_text(
        encoding="utf-8"))
    manifest = json.loads((pool_dir / "manifest.json").read_text(
        encoding="utf-8"))
    assert summary["fingerprint"] == manifest["fingerprint"]


def test_merge_missing_job_rejected(small_pc, monkeypatch, tmp_path,
                                     capsys):
    pc = small_pc
    monkeypatch.setattr(pc, "OUT_ROOT", tmp_path)
    rows = _mk_rows_from_manifest(pc)
    victim = next(r for r in rows if r["kind"] == "pos"
                  and r["seed"] == 1)
    kept = [r for r in rows if r is not victim]
    stamp = _write_stamp_dir(pc, tmp_path, {"all-s0of1.json": kept})
    rc = pc.cmd_merge(types.SimpleNamespace(stamp=stamp, keep_pool=False))
    assert rc == 1
    out = capsys.readouterr().out
    assert "pos|0.02|5|1" in out                 # 报缺什么


def test_merge_duplicate_job_rejected(small_pc, monkeypatch, tmp_path,
                                       capsys):
    pc = small_pc
    monkeypatch.setattr(pc, "OUT_ROOT", tmp_path)
    rows = _mk_rows_from_manifest(pc)
    dup_row = dict(rows[0])                      # 复制分片 → 键重复
    stamp = _write_stamp_dir(pc, tmp_path, {
        "a-s0of1.json": rows,
        "a-s0of1-copy.json": [dup_row]})
    rc = pc.cmd_merge(types.SimpleNamespace(stamp=stamp, keep_pool=False))
    assert rc == 1
    assert "重复" in capsys.readouterr().out


def test_merge_extra_job_rejected(small_pc, monkeypatch, tmp_path, capsys):
    pc = small_pc
    monkeypatch.setattr(pc, "OUT_ROOT", tmp_path)
    rows = _mk_rows_from_manifest(pc)
    extra = {"kind": "brier", "seed": 99, "s_uninformative": 0.0,
             "s_planted": 1.0}
    stamp = _write_stamp_dir(pc, tmp_path,
                             {"all-s0of1.json": rows + [extra]})
    rc = pc.cmd_merge(types.SimpleNamespace(stamp=stamp, keep_pool=False))
    assert rc == 1
    assert "brier|99" in capsys.readouterr().out    # 报多什么


def test_merge_without_manifest_rejected(small_pc, monkeypatch, tmp_path):
    pc = small_pc
    monkeypatch.setattr(pc, "OUT_ROOT", tmp_path)
    rows = _mk_rows_from_manifest(pc)
    stamp = _write_stamp_dir(pc, tmp_path, {"all-s0of1.json": rows})
    (tmp_path / stamp / "manifest.json").unlink()
    rc = pc.cmd_merge(types.SimpleNamespace(stamp=stamp, keep_pool=False))
    assert rc == 1


def test_merge_rejects_legacy_v1_manifest(small_pc, monkeypatch, tmp_path,
                                          capsys):
    """旧 stamp（t75-formal-v2/v3 的 v1 manifest，无绑定字段）→ 明确
    拒绝合并，不静默通过。"""
    pc = small_pc
    monkeypatch.setattr(pc, "OUT_ROOT", tmp_path)
    rows = _mk_rows_from_manifest(pc)
    legacy = {"version": 1, "jobs": pc._job_manifest()["jobs"]}
    stamp = _write_stamp_dir(pc, tmp_path, {"all-s0of1.json": rows},
                             manifest=legacy, fp_override="FP")
    rc = pc.cmd_merge(types.SimpleNamespace(stamp=stamp, keep_pool=False))
    assert rc == 1
    out = capsys.readouterr().out
    assert "旧版本" in out and "不可" in out


def test_merge_rejects_shard_fingerprint_mismatch(small_pc, monkeypatch,
                                                  tmp_path, capsys):
    """分片指纹必须与 manifest 绑定一致（缺/重/混版本拒绝）。"""
    pc = small_pc
    monkeypatch.setattr(pc, "OUT_ROOT", tmp_path)
    rows = _mk_rows_from_manifest(pc)
    # 一个分片指纹与 manifest 绑定不符（另一来源/另一版本的产物混入）
    stamp = _write_stamp_dir(pc, tmp_path, {"a-s0of1.json": rows},
                             fp_override="FPX")
    rc = pc.cmd_merge(types.SimpleNamespace(stamp=stamp, keep_pool=False))
    assert rc == 1
    out = capsys.readouterr().out
    assert "指纹" in out and "manifest" in out


def test_merge_rejects_pool_replaced_after_run(small_pc, monkeypatch,
                                               tmp_path, capsys):
    """分片跑完后池被替换（meta 未更新）→ merge 复核实际内容与 manifest
    绑定不一致 → 拒绝。"""
    pc = small_pc
    monkeypatch.setattr(pc, "OUT_ROOT", tmp_path)
    rows = _mk_rows_from_manifest(pc)
    stamp = _write_stamp_dir(pc, tmp_path, {"all-s0of1.json": rows})
    pool2 = _mk_pool_frame()
    pool2.loc[0, "outcome"] = "win" if pool2.loc[0, "outcome"] == "loss" \
        else "loss"
    pool2.to_parquet(tmp_path / stamp / "pool.parquet", index=False)
    rc = pc.cmd_merge(types.SimpleNamespace(stamp=stamp, keep_pool=False))
    assert rc == 1
    assert "不一致" in capsys.readouterr().out
