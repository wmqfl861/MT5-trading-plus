"""scripts/prob_calibration.py 的 P-02/P-03/P-12 修正测试（T98 审核）。

全部合成数据：不触真实数据根、不连 MT5、不跑真实计数/标签引擎——
重依赖经 monkeypatch 替身注入（P-02 的标签器是**捕获式替身**，只记录
调用时刻）。覆盖：
- P-02 成簇负对照：real 与 null 均只在平移后时刻打标（两组调用时刻均
  不含原始触发时刻）；整周平移保 NY 本地小时/星期（含 DST 不存在/
  歧义）；越界丢弃不做 nearest 末端堆积；null 非原始时刻子集。
- P-03 样本单位：名义 n ≠ 实际日数时记录 n_days_real/n_days_null；
  pool 源指纹入 meta 且入 run fingerprint；build_costs 传
  swap_ref_price=全序列最后收盘（与正式 P1 同口径）。
- P-12 merge manifest：run 写完备集 manifest；merge 精确匹配（缺/
  多/重全拒绝）；pool.meta.json 不再被 merge 删除。
"""
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


def synth_bars(n_hours, *, seed=7, start="2021-01-04", s0=1800.0):
    """连续小时随机游走 K 线（start=周一）。"""
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0, 1.2, size=n_hours)
    close = s0 + np.cumsum(steps)
    openc = close + rng.normal(0.0, 0.4, size=n_hours)
    high = np.maximum(openc, close) + np.abs(rng.normal(0, 0.5, size=n_hours))
    low = np.minimum(openc, close) - np.abs(rng.normal(0, 0.5, size=n_hours))
    return pd.DataFrame({
        "time": pd.date_range(start, periods=n_hours, freq="1h"),
        "open": openc, "high": high, "low": low, "close": close,
        "tick_volume": 10, "spread": 5, "real_volume": 0})


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


# ---------------------------------------------------------------- P-02 平移

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
    out = pc._shifted_triggers(trig, idx, weeks=3, perm_state=11)
    assert len(out) == 1                       # idx[-5]+3周越界 → 丢弃
    assert idx[-1] not in set(out["time"])     # 不堆到末根
    assert out["time"].iloc[0] == idx[10 + 3 * 168]   # 整周=168 小时精确落 bar
    # 防御分支：平移后早于首根也丢弃（合成触发早于序列起点）
    trig_early = pd.DataFrame({
        "time": [pd.Timestamp("2020-12-14 01:00")],   # 序列前 3 周
        "situation": "S1", "action": "A3", "side": 1, "level": 100.0})
    assert len(pc._shifted_triggers(trig_early, idx, weeks=1,
                                    perm_state=11)) == 0


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


def test_pool_source_fingerprint_in_meta_and_run_fingerprint(
        monkeypatch, tmp_path):
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
    pc.build_pool("st1")
    meta = json.loads((tmp_path / "st1" / "pool.meta.json")
                      .read_text(encoding="utf-8"))
    fp = meta.get("pool_source_fingerprint")
    assert isinstance(fp, str) and len(fp) >= 16      # 存在且非空
    # 同池重建 → 指纹确定（同 seed 同分布摘要）
    pc.build_pool("st2")
    meta2 = json.loads((tmp_path / "st2" / "pool.meta.json")
                       .read_text(encoding="utf-8"))
    assert meta2["pool_source_fingerprint"] == fp
    # 源指纹进入 run fingerprint（不同源 → 不同 run 指纹）
    assert pc._run_fingerprint("aaa") != pc._run_fingerprint("bbb")
    assert pc._run_fingerprint(fp) != pc._run_fingerprint("")
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


def _write_stamp_dir(pc, tmp_path, rows_by_file, *, manifest_jobs="auto"):
    stamp_dir = tmp_path / "st"
    stamp_dir.mkdir(parents=True, exist_ok=True)
    jobs = (pc._job_manifest()["jobs"] if manifest_jobs == "auto"
            else manifest_jobs)
    (stamp_dir / "manifest.json").write_text(
        json.dumps({"version": 1, "jobs": jobs}), encoding="utf-8")
    for name, rows in rows_by_file.items():
        (stamp_dir / name).write_text(json.dumps(
            {"phase": name.split("-s")[0], "shard": [0, 1],
             "fingerprint": "FP", "B": 3, "rows": rows}), encoding="utf-8")
    return "st"


def test_job_manifest_is_complete_product(small_pc):
    m = small_pc._job_manifest()
    assert m["version"] == 1
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
    (pool_dir / "pool.parquet").write_bytes(b"fake-pool")
    (pool_dir / "pool.meta.json").write_text(
        json.dumps({"pool_source_fingerprint": "x" * 64}), encoding="utf-8")
    rc = pc.cmd_merge(types.SimpleNamespace(stamp=stamp, keep_pool=False))
    assert rc == 0
    assert (pool_dir / "summary.json").is_file()
    assert not (pool_dir / "pool.parquet").is_file()   # 标签池仍删
    assert (pool_dir / "pool.meta.json").is_file()     # 来源指纹保留（P-12）


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
    stamp_dir = tmp_path / "st"
    stamp_dir.mkdir(parents=True)
    (stamp_dir / "all-s0of1.json").write_text(json.dumps(
        {"phase": "neg", "shard": [0, 1], "fingerprint": "FP", "B": 3,
         "rows": rows}), encoding="utf-8")
    rc = pc.cmd_merge(types.SimpleNamespace(stamp="st", keep_pool=False))
    assert rc == 1


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
    monkeypatch.setattr(pc, "phase_cluster", lambda i, n, stamp: [
        {"kind": "cluster_neg", "weeks": 3, "seed": 0,
         "n_real": 1, "n_null": 1, "p": 0.5},
        {"kind": "cluster_neg", "weeks": 3, "seed": 1,
         "n_real": 1, "n_null": 1, "p": 0.5}])
    args = types.SimpleNamespace(phase="cluster", shard_i=0, shard_n=1,
                                 stamp="t1")
    assert pc.cmd_run(args) == 0
    mpath = tmp_path / "t1" / "manifest.json"
    assert mpath.is_file()
    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    assert manifest == pc._job_manifest()
    # 重跑（manifest 一致）幂等通过；篡改 → 拒绝
    assert pc.cmd_run(args) == 0
    mpath.write_text(json.dumps({"version": 1, "jobs": ["x"]}),
                     encoding="utf-8")
    assert pc.cmd_run(args) == 1
