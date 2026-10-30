"""T4 锁箱测试：cutoff 计算、配置读写、research_bars 边界、状态统计。

语义钉死（Review Focus #4——锁箱旁路）：
- research_bars 只返回 time < cutoff 的行；
- 显式 end > cutoff → LockboxError，绝不静默截断；
- X-03：无任何声明边界且有数据的库（含无 meta 的手写 parquet）对正式
  研究读按"未初始化"拒绝；显式通道 allow_unbounded=True 保持旧的直读
  行为（测试/迁移专用）；完全无数据的序列保持 missing → None。

T2（R2）追加：v2 全局 cutoff——所有序列共用一个值（取最保守者），
一经写入只能更早不能更晚；v1 序列键配置继续兼容。
"""
import importlib.util
import json
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from mt5data.lockbox import (
    LockboxConfig,
    LockboxError,
    compute_cutoff,
    compute_global_cutoff,
    cutoff_for,
    load_config,
    lockbox_status,
    research_bars,
    write_config,
    write_global_cutoff,
)
from mt5data.schedule import SeriesRef
from mt5data.store import save_bars


def make_series(root, symbol="XAUUSD", tf="H1",
                start=datetime(2026, 1, 1), n=48):
    """n 根 H1：2026-01-01T00 .. 2026-01-02T23（供 cutoff=01-02T00 切半）。"""
    df = pd.DataFrame({
        "time": pd.to_datetime([start + timedelta(hours=i) for i in range(n)]),
        "open": 1.0, "high": 1.0, "low": 1.0,
        "close": [float(i) for i in range(n)],
        "tick_volume": 1, "spread": 1, "real_volume": 0,
    })
    save_bars(root, symbol, tf, df)
    return df


# ---- compute_cutoff：历史跨度决定回扣月数，截到日 00:00 ----

def test_cutoff_long_history_uses_12_months():
    # 2014 年起：跨度 >5 年 → now−12 个月，且截到日 00:00
    c = compute_cutoff(datetime(2014, 1, 1), datetime(2026, 10, 24, 15, 30))
    assert c == datetime(2025, 10, 24, 0, 0)


def test_cutoff_short_history_uses_6_months():
    # 2024 年中起：跨度 <5 年 → now−6 个月
    c = compute_cutoff(datetime(2024, 6, 1), datetime(2026, 10, 24, 15, 30))
    assert c == datetime(2026, 4, 24, 0, 0)


def test_cutoff_exactly_5_years_takes_6_months():
    first = datetime(2021, 10, 24, 9, 0)
    now = datetime(2026, 10, 24, 9, 0)
    assert compute_cutoff(first, now) == datetime(2026, 4, 24)  # 恰 5 年 → 6 个月
    assert compute_cutoff(first, now + timedelta(seconds=1)) == \
        datetime(2025, 10, 24)  # 晚 1 秒才越过边界 → 12 个月


def test_cutoff_feb29_first_year_boundary():
    # 闰日起步：5 年边界收进 02-28（2029 非闰年）
    first = datetime(2024, 2, 29)
    assert compute_cutoff(first, datetime(2029, 2, 28)) == datetime(2028, 8, 28)
    assert compute_cutoff(first, datetime(2029, 3, 1)) == datetime(2028, 3, 1)


def test_cutoff_month_end_clamps_day():
    # 3/31 −6 个月 → 9/30（不存在 9/31，收进当月最后一天）
    c = compute_cutoff(datetime(2025, 1, 1), datetime(2026, 3, 31, 12, 0))
    assert c == datetime(2025, 9, 30)


# ---- 配置读写 ----

def test_write_load_config_roundtrip(tmp_path):
    write_config(tmp_path, {"XAUUSD/H1": "2026-04-24T00:00:00",
                            "EURUSD/M1": "2026-04-24T00:00:00"})
    lb = load_config(tmp_path)
    assert isinstance(lb, LockboxConfig)
    assert lb.cutoffs == {
        "XAUUSD/H1": datetime(2026, 4, 24),
        "EURUSD/M1": datetime(2026, 4, 24),
    }
    assert lb.global_cutoff is None  # v1：没有全局值


def test_write_config_normalizes_keys_upper(tmp_path):
    write_config(tmp_path, {"xauusd/h1": "2026-04-24T00:00:00"})
    assert load_config(tmp_path).cutoffs == {"XAUUSD/H1": datetime(2026, 4, 24)}


def test_write_config_rejects_unparsable_value(tmp_path):
    with pytest.raises(ValueError):
        write_config(tmp_path, {"XAUUSD/H1": "不是时间"})


def test_load_config_missing_returns_none(tmp_path):
    assert load_config(tmp_path) is None


# ---- research_bars：每条读取路径一条"越过即报错/约束"测试 ----

def test_research_bars_no_config_rejected_as_uninitialized(tmp_path):
    """X-03：无任何声明边界的裸库对正式研究读按"未初始化"拒绝；
    显式测试/迁移通道 allow_unbounded=True 保持与 store.load_bars
    一致的直读。"""
    make_series(tmp_path)
    with pytest.raises(LockboxError, match="未初始化"):
        research_bars(tmp_path, "XAUUSD", "H1")
    assert len(research_bars(tmp_path, "XAUUSD", "H1",
                             allow_unbounded=True)) == 48


def test_research_bars_returns_only_rows_before_cutoff(tmp_path):
    make_series(tmp_path)
    write_config(tmp_path, {"XAUUSD/H1": "2026-01-02T00:00:00"})
    df = research_bars(tmp_path, "XAUUSD", "H1")
    assert len(df) == 24
    assert df["time"].max() < pd.Timestamp(2026, 1, 2)


def test_research_bars_end_beyond_cutoff_raises(tmp_path):
    make_series(tmp_path)
    write_config(tmp_path, {"XAUUSD/H1": "2026-01-02T00:00:00"})
    with pytest.raises(LockboxError, match="锁箱"):
        research_bars(tmp_path, "XAUUSD", "H1", end=datetime(2026, 1, 3))
    # 越界 1 秒也算越过——显式请求禁止静默截断
    with pytest.raises(LockboxError, match="锁箱"):
        research_bars(tmp_path, "XAUUSD", "H1",
                      end=datetime(2026, 1, 2, 0, 0, 1))


def test_research_bars_end_equals_cutoff_allowed_but_strict(tmp_path):
    make_series(tmp_path)
    write_config(tmp_path, {"XAUUSD/H1": "2026-01-02T00:00:00"})
    df = research_bars(tmp_path, "XAUUSD", "H1", end=datetime(2026, 1, 2))
    assert len(df) == 24  # 不报错，但结果仍严格 < cutoff


def test_research_bars_legal_end_applies_as_upper_bound(tmp_path):
    make_series(tmp_path)
    write_config(tmp_path, {"XAUUSD/H1": "2026-01-02T00:00:00"})
    df = research_bars(tmp_path, "XAUUSD", "H1", end=datetime(2026, 1, 1, 10, 0))
    assert len(df) == 11  # 00:00..10:00 两端含
    assert df["time"].max() == pd.Timestamp(2026, 1, 1, 10, 0)


def test_research_bars_end_without_config_filters_plainly(tmp_path):
    """X-03：显式通道下的 end 过滤（无声明边界时 end 单独生效，含端点）。"""
    make_series(tmp_path)
    df = research_bars(tmp_path, "XAUUSD", "H1", end=datetime(2026, 1, 1, 5, 0),
                       allow_unbounded=True)
    assert len(df) == 6


def test_research_bars_only_configured_series_locked(tmp_path):
    """v1 键按序列生效：被配置的序列受锁；未配置序列（有数据）按
    未初始化拒绝，显式通道恢复旧直读。"""
    make_series(tmp_path)
    write_config(tmp_path, {"EURUSD/M1": "2026-01-02T00:00:00"})
    with pytest.raises(LockboxError, match="未初始化"):
        research_bars(tmp_path, "XAUUSD", "H1")  # 键按序列生效
    assert len(research_bars(tmp_path, "XAUUSD", "H1",
                             allow_unbounded=True)) == 48


def test_research_bars_accepts_explicit_lockbox_dict(tmp_path):
    make_series(tmp_path)
    df = research_bars(tmp_path, "XAUUSD", "H1",
                       lockbox={"XAUUSD/H1": datetime(2026, 1, 2)})
    assert len(df) == 24  # 显式传入等价于读盘


def test_research_bars_missing_series_returns_none(tmp_path):
    assert research_bars(tmp_path, "NOAUD", "M1") is None  # 对齐 load_bars


def test_research_bars_end_beyond_cutoff_raises_even_without_data(tmp_path):
    # 非法请求先于数据存在性判定（fail-fast）
    write_config(tmp_path, {"NOAUD/M1": "2026-01-02T00:00:00"})
    with pytest.raises(LockboxError):
        research_bars(tmp_path, "NOAUD", "M1", end=datetime(2026, 1, 3))


# ---- lockbox_status ----

def test_lockbox_status_counts_open_and_locked(tmp_path):
    make_series(tmp_path)
    write_config(tmp_path, {"XAUUSD/H1": "2026-01-02T00:00:00"})
    st = lockbox_status(tmp_path, "XAUUSD", "H1")
    assert st["cutoff"] == datetime(2026, 1, 2)
    assert st["open_rows"] == 24 and st["locked_rows"] == 24


def test_lockbox_status_without_config_all_open(tmp_path):
    make_series(tmp_path)
    st = lockbox_status(tmp_path, "XAUUSD", "H1")
    assert st["cutoff"] is None
    assert st["open_rows"] == 48 and st["locked_rows"] == 0


# ---- T2（R2）：全局锁箱 v2 —— compute_global_cutoff ----

def _ref(symbol: str, tf: str, first_time: str) -> SeriesRef:
    return SeriesRef(symbol=symbol, timeframe=tf,
                     watermark={"first_time": first_time})


def test_compute_global_cutoff_takes_earliest_series_cutoff():
    # XAUUSD 跨度 >5 年 → now−12 个月；BTCUSD 2024 年中起 → now−6 个月；
    # 全局取最早者（锁得最多 = 最保守），且截到日 00:00
    inv = [_ref("XAUUSD", "H1", "2014-01-01T00:00:00"),
           _ref("BTCUSD", "M1", "2024-06-01T00:00:00")]
    assert compute_global_cutoff(inv, datetime(2026, 10, 24, 15, 30)) == \
        datetime(2025, 10, 24, 0, 0)


def test_compute_global_cutoff_truncates_to_day_start():
    inv = [_ref("BTCUSD", "M1", "2024-06-01T00:00:00")]
    # 单序列：now−6 个月，now 带时分也不影响——结果截到日 00:00
    assert compute_global_cutoff(inv, datetime(2026, 10, 24, 23, 59)) == \
        datetime(2026, 4, 24, 0, 0)


def test_compute_global_cutoff_empty_inventory_raises():
    with pytest.raises(ValueError, match="inventory"):
        compute_global_cutoff([], datetime(2026, 10, 24))


# ---- T2（R2）：write_global_cutoff —— 只许更早不许更晚 ----

def test_write_global_cutoff_fresh_creates_v2(tmp_path):
    path = write_global_cutoff(tmp_path, datetime(2026, 1, 2), note="初始")
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    assert raw["version"] == 2
    assert raw["global_cutoff"] == "2026-01-02T00:00:00"
    assert raw["note"] == "初始"
    datetime.fromisoformat(raw["created_utc"])  # 必有且可解析
    lb = load_config(tmp_path)
    assert isinstance(lb, LockboxConfig)
    assert lb.global_cutoff == datetime(2026, 1, 2)
    assert cutoff_for(lb, "XAUUSD", "H1") == datetime(2026, 1, 2)


def test_v2_cutoff_applies_to_every_series(tmp_path):
    # R2 核心：全局 cutoff 对任意序列生效——包括从未单独配置过（甚至没有数据）的序列
    write_global_cutoff(tmp_path, datetime(2026, 1, 2), note="全局")
    lb = load_config(tmp_path)
    assert cutoff_for(lb, "BTCUSD", "M1") == datetime(2026, 1, 2)
    assert cutoff_for(lb, "ustec", "w1") == datetime(2026, 1, 2)  # 大小写归一


def test_write_global_cutoff_accepts_iso_string(tmp_path):
    write_global_cutoff(tmp_path, "2026-01-02T00:00:00", note="str")
    assert load_config(tmp_path).global_cutoff == datetime(2026, 1, 2)


def test_write_global_cutoff_rejects_unparsable(tmp_path):
    with pytest.raises(ValueError):
        write_global_cutoff(tmp_path, "不是时间", note="bad")


def test_write_global_cutoff_later_is_rejected(tmp_path):
    write_global_cutoff(tmp_path, datetime(2026, 1, 2), note="初始")
    with pytest.raises(LockboxError, match="只能更早不能更晚"):
        write_global_cutoff(tmp_path, datetime(2026, 1, 2, 0, 0, 1), note="前移 1 秒")
    with pytest.raises(LockboxError, match="只能更早不能更晚"):
        write_global_cutoff(tmp_path, datetime(2026, 2, 1), note="前移一天")
    # 拒绝后文件保持原值，没有被部分改写
    assert load_config(tmp_path).global_cutoff == datetime(2026, 1, 2)


def test_write_global_cutoff_earlier_allowed_and_keeps_history(tmp_path):
    write_global_cutoff(tmp_path, datetime(2026, 1, 2), note="初始")
    write_global_cutoff(tmp_path, datetime(2025, 12, 1), note="收紧")
    lb = load_config(tmp_path)
    assert lb.global_cutoff == datetime(2025, 12, 1)
    assert len(lb.history) == 1
    assert lb.history[0]["global_cutoff"] == "2026-01-02T00:00:00"
    assert lb.history[0]["note"] == "初始"
    assert datetime.fromisoformat(lb.history[0]["superseded_utc"])


def test_write_global_cutoff_equal_is_allowed(tmp_path):
    # 等值不算前移：允许重写（更新 note/created），值未变不产生 history 噪声
    write_global_cutoff(tmp_path, datetime(2026, 1, 2), note="初始")
    write_global_cutoff(tmp_path, datetime(2026, 1, 2), note="改备注")
    lb = load_config(tmp_path)
    assert lb.global_cutoff == datetime(2026, 1, 2)
    assert lb.note == "改备注"
    assert lb.history == ()


def test_write_global_cutoff_over_v1_requires_not_later(tmp_path):
    # v1 既有配置：等效旧值 = 各序列 cutoff 的最早者；新全局晚于它 = 放宽 → 拒绝
    write_config(tmp_path, {"XAUUSD/H1": "2026-01-02T00:00:00",
                            "EURUSD/M1": "2025-06-01T00:00:00"})
    with pytest.raises(LockboxError, match="只能更早不能更晚"):
        write_global_cutoff(tmp_path, datetime(2025, 7, 1), note="越权放宽")
    # 不晚于最早值 → 允许；v1 各键整体保留进 history
    write_global_cutoff(tmp_path, datetime(2025, 5, 1), note="统一收紧")
    lb = load_config(tmp_path)
    assert lb.global_cutoff == datetime(2025, 5, 1)
    assert cutoff_for(lb, "XAUUSD", "H1") == datetime(2025, 5, 1)  # v1 原值被更早的全局取代
    assert lb.history[0]["v1_cutoffs"] == {"XAUUSD/H1": "2026-01-02T00:00:00",
                                           "EURUSD/M1": "2025-06-01T00:00:00"}


def test_write_config_over_v2_refused(tmp_path):
    # v2 全局锁箱一经写入，禁止再用 v1 序列键覆盖（那是变相绕过"只能更早"）
    write_global_cutoff(tmp_path, datetime(2026, 1, 2), note="全局")
    with pytest.raises(LockboxError, match="v2"):
        write_config(tmp_path, {"XAUUSD/H1": "2025-01-01T00:00:00"})
    assert load_config(tmp_path).global_cutoff == datetime(2026, 1, 2)


def test_load_config_rejects_unknown_version(tmp_path):
    (tmp_path / "lockbox.json").write_text(
        json.dumps({"version": 3, "global_cutoff": "2026-01-02T00:00:00"}),
        encoding="utf-8")
    with pytest.raises(ValueError, match="版本"):
        load_config(tmp_path)


def test_load_config_rejects_mixed_v2_and_series_keys(tmp_path):
    # v2 与序列键混写 = 语义不明（序列键会被全局值忽略）→ fail-fast
    (tmp_path / "lockbox.json").write_text(
        json.dumps({"version": 2, "global_cutoff": "2026-01-02T00:00:00",
                    "XAUUSD/H1": "2025-01-01T00:00:00"}),
        encoding="utf-8")
    with pytest.raises(ValueError, match="序列键"):
        load_config(tmp_path)


# ---- T2（R2）：v2 生效下的读取路径 ----

def test_v2_research_bars_locks_every_series(tmp_path):
    make_series(tmp_path, "XAUUSD", "H1", n=48)
    make_series(tmp_path, "EURUSD", "H1", n=48)
    write_global_cutoff(tmp_path, datetime(2026, 1, 2), note="全局")
    for sym in ("XAUUSD", "EURUSD"):
        df = research_bars(tmp_path, sym, "H1")
        assert len(df) == 24
        assert df["time"].max() < pd.Timestamp(2026, 1, 2)
        with pytest.raises(LockboxError, match="锁箱"):
            research_bars(tmp_path, sym, "H1", end=datetime(2026, 1, 3))


def test_v2_research_bars_raises_even_for_unknown_series(tmp_path):
    # 全局 cutoff 下不存在"没配置的序列"——防跨品种泄露（R2 的核心场景）
    write_global_cutoff(tmp_path, datetime(2026, 1, 2), note="全局")
    with pytest.raises(LockboxError):
        research_bars(tmp_path, "NOAUD", "M1", end=datetime(2026, 1, 3))


def test_lockbox_status_with_v2_global(tmp_path):
    make_series(tmp_path, n=48)
    write_global_cutoff(tmp_path, datetime(2026, 1, 2), note="全局")
    st = lockbox_status(tmp_path, "XAUUSD", "H1")
    assert st == {"symbol": "XAUUSD", "timeframe": "H1",
                  "cutoff": datetime(2026, 1, 2),
                  "open_rows": 24, "locked_rows": 24}


# ---- T2：scripts/lockbox_init.py（--cutoff / --auto）----

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "lockbox_init.py"


def _load_init_script():
    spec = importlib.util.spec_from_file_location("lockbox_init_script", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_lockbox_init_cutoff_writes_global_and_reports_rows(tmp_path, capsys):
    make_series(tmp_path, n=48)
    mod = _load_init_script()
    rc = mod.main(["--cutoff", "2026-01-02", "--data-root", str(tmp_path)])
    assert rc == 0
    assert load_config(tmp_path).global_cutoff == datetime(2026, 1, 2)
    out = capsys.readouterr().out
    assert "2026-01-02" in out
    assert "开放 24" in out and "锁定 24" in out  # 每序列开放/锁定行数


def test_lockbox_init_auto_computes_conservative_cutoff(tmp_path):
    make_series(tmp_path, n=48)  # 序列 2026-01-01 起，跨度短 → now−6 个月
    mod = _load_init_script()
    assert mod.main(["--auto", "--data-root", str(tmp_path)]) == 0
    from datetime import timezone
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    assert load_config(tmp_path).global_cutoff == \
        compute_cutoff(datetime(2026, 1, 1), now)


def test_lockbox_init_later_cutoff_fails(tmp_path, capsys):
    make_series(tmp_path, n=48)
    mod = _load_init_script()
    assert mod.main(["--cutoff", "2026-01-02", "--data-root", str(tmp_path)]) == 0
    rc = mod.main(["--cutoff", "2026-01-03", "--data-root", str(tmp_path)])
    assert rc == 1
    assert "只能更早" in capsys.readouterr().out


def test_lockbox_init_earlier_cutoff_succeeds(tmp_path):
    make_series(tmp_path, n=48)
    mod = _load_init_script()
    assert mod.main(["--cutoff", "2026-01-02", "--data-root", str(tmp_path)]) == 0
    assert mod.main(["--cutoff", "2025-12-01", "--data-root", str(tmp_path)]) == 0
    lb = load_config(tmp_path)
    assert lb.global_cutoff == datetime(2025, 12, 1)
    assert len(lb.history) == 1


def test_lockbox_init_rejects_bad_date(tmp_path, capsys):
    mod = _load_init_script()
    rc = mod.main(["--cutoff", "2026/01/02", "--data-root", str(tmp_path)])
    assert rc == 1
    assert load_config(tmp_path) is None  # 没有写入


def test_lockbox_init_auto_empty_inventory_fails(tmp_path, capsys):
    mod = _load_init_script()
    assert mod.main(["--auto", "--data-root", str(tmp_path)]) == 1


def test_lockbox_init_cutoff_and_auto_are_exclusive(tmp_path):
    mod = _load_init_script()
    with pytest.raises(SystemExit):  # argparse 互斥组直接报用法错误
        mod.main(["--cutoff", "2026-01-02", "--auto", "--data-root", str(tmp_path)])
