"""T221（D117；F3 预注册草案 §3.3 第 1 条）：定盘锚点闸的币对清单。

`scripts/fetch_ext_data.py::_histdata_year_gate` 的锚点闸此前只对名为
EURUSD 的币对执行。T221 起按显式清单 `HISTDATA_FIX_ANCHOR_PAIRS` 执行：
EURUSD 加 F3 的四个新币对 GBPUSD、USDJPY、USDCHF、AUDUSD。闸门的其余
规则一字不动（T94/R14-L4、L9）：

- 只有锚点 inconsistent（月末定盘窗的尖峰在错的小时）才排除该年；
- undetermined 不排除：以标签规则检测为准入库，anchor_note 写进记录；
- 年判 inconsistent/undetermined 的年份先按月回收，回收子集再测锚点；
- 清单外的品种不执行锚点闸。

样本沿用 `tests/test_ext_data.py` 的合成年度 zip（同一套
_synth_weeks_utc / _labels_for_rule）：先合成真 UTC 的 M1 bar，再按候选
标签规则写回原始标签；波动尖峰按**真 UTC 对应的伦敦墙钟**分钟窗放置。
口径检测与转换正确时，尖峰落在预期的伦敦时刻。

样本抽稀（见 _bars）：闸门只读两类 bar——口径观测（周日首根、换日空档
后的首根，都在整点）和月末日下午搜索窗 [14:00, 17:00) 伦敦墙钟里的
bar。这两处保留 M1，其余抽成 15 分钟一根。闸门读到的 bar 因此与整年
M1 样本是同一组，判定相同（test_thinned_sample_matches_full_m1_record
钉住这一点）；一次闸门从十几秒降到一秒多。

在父提交 a122c78 上，凡是以新币对为参数的用例都失败：四个新币对的
inconsistent 年被启用（锚点闸没有执行），记录里没有 fix_anchor_month_end
和 anchor_note。EURUSD 和清单外品种的用例在父提交上本来就通过，留作
回归护栏。
"""
import functools
import io
import zipfile

import numpy as np
import pandas as pd
import pytest

from tests.test_ext_data import (REPO_ROOT, _labels_for_rule,
                                 _synth_weeks_utc, load_script)

NEW_PAIRS = ("GBPUSD", "USDJPY", "USDCHF", "AUDUSD")
ANCHOR_PAIRS = frozenset({"EURUSD", *NEW_PAIRS})
YEAR = 2021
DL_AT = "2026-11-28T00:00:00+00:00"

# 伦敦墙钟分钟窗，与 consistency.fix_window_vol_spike 的默认值一致：
# 定盘核心窗 [15:57, 16:03)、它的 −1 小时对照窗、搜索窗 [14:00, 17:00)
FIX_CORE = (15 * 60 + 57, 16 * 60 + 3)
FIX_MINUS_1H = (14 * 60 + 57, 15 * 60 + 3)
SEARCH_LO, SEARCH_HI = 14 * 60, 17 * 60
# 抽稀后其余时段的步长（分钟）。步长变长不产生新的空档：换日空档 60 分钟
# 仍不低于检测下限 max(30 分钟, 3 × 步长) = 45 分钟，其余间隔都不超过
# 15 分钟；周日 17:00 开市和 18:00 恢复都在 15 分钟的网格上
COARSE = 15

# 年判 inconsistent 的回收年里，月级闸门丢掉的月份（构造见 _labels）
UNSALVAGED_MONTHS = ("2021-07", "2021-08", "2021-09", "2021-10")


@functools.lru_cache(maxsize=None)
def _script():
    return load_script("fetch_ext_data_t221",
                       REPO_ROOT / "scripts" / "fetch_ext_data.py")


@functools.lru_cache(maxsize=None)
def _bars(coarse: int) -> pd.DatetimeIndex:
    """合成 2021 年的 bar 时刻（naive UTC），带外汇周结构和换日空档。
    coarse=1 是整年 M1；否则每个月在样本里最后一个伦敦日期的搜索窗
    [14:00, 17:00) 保留 M1（取月末的做法与 fix_window_vol_spike 相同），
    其余只留整点网格上每 coarse 分钟一根。"""
    full = _synth_weeks_utc(YEAR)
    local = full.tz_localize("UTC").tz_convert("Europe/London")
    minute = np.asarray(local.hour * 60 + local.minute)
    day = np.asarray(local.year) * 10000 + np.asarray(local.month) * 100 \
        + np.asarray(local.day)
    month = np.asarray(local.year) * 12 + np.asarray(local.month)
    month_last = pd.Series(day).groupby(month).transform("max").to_numpy()
    dense = (day == month_last) & (minute >= SEARCH_LO) \
        & (minute < SEARCH_HI)
    return full[dense | (full.minute % coarse == 0)]


@functools.lru_cache(maxsize=None)
def _labels(kind: str, coarse: int) -> tuple[pd.DatetimeIndex, pd.Series]:
    """真 UTC 的 bar 与它的原始标签。kind='us_wall'：整年美东墙钟；
    'mixed'：全年美东墙钟，7–9 月换成固定 EST——年级判 inconsistent，
    7–9 月不可回收、10 月只含单个错位日观测而判 undetermined。与
    test_ext_data.py 的月回收集成用例是同一个构造。"""
    utc = _bars(coarse)
    labels = pd.Series(_labels_for_rule(utc, "us_wall"))
    if kind == "mixed":
        est = pd.to_datetime(_labels_for_rule(utc, "fixed_est"))
        bad = np.isin(utc.month.to_numpy(), (7, 8, 9))
        labels[bad] = est[bad]
    return utc, labels


_ZIPS: dict = {}


def _year_zip(kind: str, spike, coarse: int = COARSE) -> bytes:
    """合成年度 zip，按 (kind, spike, coarse) 缓存。spike=(lo, hi) 时在
    真 UTC 对应的伦敦墙钟分钟窗 [lo, hi) 放 ×20 的波动尖峰，None 表示
    不放。zip 内 csv 的文件名不参与闸门：年份取自标签，源文件名由调用
    方的 source_name 给出。"""
    key = (kind, spike, coarse)
    if key not in _ZIPS:
        utc, labels = _labels(kind, coarse)
        local = utc.tz_localize("UTC").tz_convert("Europe/London")
        minute = local.hour * 60 + local.minute
        n = len(utc)
        amp = (np.where((minute >= spike[0]) & (minute < spike[1]), 20.0, 1.0)
               if spike else np.ones(n))
        ret = 1e-4 * amp * np.where(np.arange(n) % 2 == 0, 1.0, -1.0)
        px = 1.10 * np.cumprod(1.0 + ret)
        prev = np.concatenate([[1.10], px[:-1]])
        rows = [f"{t.strftime('%Y%m%d %H%M%S')};{o:.5f};{max(o, c):.5f};"
                f"{min(o, c):.5f};{c:.5f};0"
                for t, o, c in zip(labels, prev, px)]
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(f"DAT_ASCII_SYNTH_{YEAR}.csv", "\n".join(rows))
        _ZIPS[key] = buf.getvalue()
    return _ZIPS[key]


def _gate(raw: bytes, pair: str):
    return _script()._histdata_year_gate(
        raw, pair=pair, downloaded_at_utc=DL_AT,
        source_name=f"DAT_ASCII_{pair}_{YEAR}.zip")


# ---------------------------------------------------------------- 样本本身


def test_thinned_sample_matches_full_m1_record():
    """抽稀样本与整年 M1 样本给出同一个闸门判定：口径判定、三种规则的
    偏差数与容差、转换版本、锚点的结论、位置和观测数、年份用不用，逐项
    相等。本文件其余用例用抽稀样本，前提是这一条。

    锚点的实测均值不逐位相等：csv 里的价格写成 5 位小数，1.10 的价位上
    四舍五入带来的噪声有千分之几，两份样本的价格路径点数又不同，所以
    只断言相对差在 5% 以内。"""
    _, rec_m1 = _gate(_year_zip("us_wall", FIX_MINUS_1H, coarse=1), "EURUSD")
    _, rec_thin = _gate(_year_zip("us_wall", FIX_MINUS_1H), "EURUSD")
    assert rec_m1["calendar"] == "us_wall"
    for key in ("used", "calendar", "label_deviations", "label_tolerance",
                "converter_version", "reason"):
        assert rec_m1[key] == rec_thin[key], key
    a, b = rec_m1["fix_anchor_month_end"], rec_thin["fix_anchor_month_end"]
    for key in ("status", "location", "observations"):
        assert a[key] == b[key], key
    for key in ("v_fix", "v_minus_1h", "v_plus_1h", "margin"):
        assert b[key] == pytest.approx(a[key], rel=0.05), key


# ---------------------------------------------------------------- 清单本身


def test_anchor_pair_list_is_explicit():
    """清单恰好是 EURUSD 加四个新币对——是显式的名单，不是"所有外汇
    品种"。"""
    assert _script().HISTDATA_FIX_ANCHOR_PAIRS == ANCHOR_PAIRS


# ---------------------------------------------------------------- 新币对：整年过口径


@pytest.mark.parametrize("pair", NEW_PAIRS)
def test_inconsistent_anchor_year_excluded_for_new_pair(pair):
    """尖峰在定盘窗平移 −1 小时（伦敦 14:57–15:03）→ 锚点 inconsistent
    → 该年登记后不用。口径本身是通过的（us_wall）。"""
    s, rec = _gate(_year_zip("us_wall", FIX_MINUS_1H), pair)
    assert s is None and rec["used"] is False, rec
    assert rec["calendar"] == "us_wall"
    assert rec["converter_version"] == 4
    assert rec["fix_anchor_month_end"]["status"] == "inconsistent"
    assert rec["fix_anchor_month_end"]["location"] == "minus_1h"
    assert "inconsistent" in rec["reason"]


@pytest.mark.parametrize("pair", NEW_PAIRS)
def test_consistent_anchor_year_used_for_new_pair(pair):
    """尖峰在定盘窗（伦敦 15:57–16:03）→ 锚点 consistent → 入库。"""
    s, rec = _gate(_year_zip("us_wall", FIX_CORE), pair)
    assert s is not None and rec["used"] is True, rec
    assert rec["calendar"] == "us_wall"
    assert rec["converter_version"] == 4
    assert rec["fix_anchor_month_end"]["status"] == "consistent"
    assert "anchor_note" not in rec


@pytest.mark.parametrize("pair", NEW_PAIRS)
def test_undetermined_anchor_year_used_with_note_for_new_pair(pair):
    """没有尖峰 → 锚点分不出位置（undetermined）→ T94/R14-L4：不排除，
    以口径检测为准入库，anchor_note 写进记录。"""
    s, rec = _gate(_year_zip("us_wall", None), pair)
    assert s is not None and rec["used"] is True, rec
    assert rec["calendar"] == "us_wall"
    assert rec["fix_anchor_month_end"]["status"] == "undetermined"
    assert "undetermined" in rec["anchor_note"]
    assert "reason" not in rec


# ---------------------------------------------------------------- 新币对：回收年


@pytest.mark.parametrize("pair", NEW_PAIRS)
def test_salvaged_year_anchor_inconsistent_excluded_for_new_pair(pair):
    """年判 inconsistent 的年份先按月回收，回收子集的锚点 inconsistent
    → 整年登记后不用；年级判定和逐月回收的结果仍写进记录。"""
    s, rec = _gate(_year_zip("mixed", FIX_MINUS_1H), pair)
    assert s is None and rec["used"] is False, rec
    assert rec["calendar"] == "inconsistent"
    assert rec["salvage"] is True
    assert rec["salvaged_months"]
    assert not set(UNSALVAGED_MONTHS) & set(rec["salvaged_months"])
    assert rec["fix_anchor_month_end"]["status"] == "inconsistent"
    assert rec["fix_anchor_month_end"]["location"] == "minus_1h"
    assert "inconsistent" in rec["reason"]


@pytest.mark.parametrize("pair", NEW_PAIRS)
def test_salvaged_year_anchor_consistent_used_for_new_pair(pair):
    """回收子集的锚点 consistent → 回收月份入库，入库序列不含被月级
    闸门丢掉的月份。"""
    s, rec = _gate(_year_zip("mixed", FIX_CORE), pair)
    assert s is not None and rec["used"] is True, rec
    assert rec["calendar"] == "inconsistent"
    assert rec["salvage"] is True
    assert rec["fix_anchor_month_end"]["status"] == "consistent"
    months = pd.to_datetime(s.df["time"]).dt.strftime("%Y-%m")
    assert not months.isin(UNSALVAGED_MONTHS).any()


# ---------------------------------------------------------------- 与 EURUSD 同一条路


@pytest.mark.parametrize("pair", NEW_PAIRS)
def test_new_pair_anchor_record_equals_eurusd_on_same_sample(pair):
    """同一份样本，新币对的锚点记录与 EURUSD 的逐项相等：币对名只决定
    锚点闸跑不跑，不改变锚点函数和它的参数。"""
    raw = _year_zip("us_wall", FIX_MINUS_1H)
    _, rec_new = _gate(raw, pair)
    _, rec_eur = _gate(raw, "EURUSD")
    assert rec_new["fix_anchor_month_end"] == rec_eur["fix_anchor_month_end"]
    assert rec_new["reason"] == rec_eur["reason"]


@pytest.mark.parametrize("spike,used,status", [
    (FIX_MINUS_1H, False, "inconsistent"),
    (FIX_CORE, True, "consistent"),
    (None, True, "undetermined"),
])
def test_eurusd_behavior_unchanged(spike, used, status):
    """EURUSD 的三种锚点结果与改动前一致：inconsistent 排除；consistent
    入库；undetermined 入库并写 anchor_note。"""
    s, rec = _gate(_year_zip("us_wall", spike), "EURUSD")
    assert (s is not None) is used, rec
    assert rec["used"] is used, rec
    assert rec["calendar"] == "us_wall"
    assert rec["fix_anchor_month_end"]["status"] == status
    assert ("inconsistent" in rec.get("reason", "")) is not used
    assert ("undetermined" in rec.get("anchor_note", "")) \
        is (status == "undetermined")


# ---------------------------------------------------------------- 清单外品种


@pytest.mark.parametrize("pair", ("XAUUSD", "USDCAD"))
def test_pair_outside_list_skips_anchor_gate(pair):
    """清单外的品种不执行锚点闸：即使尖峰错位一小时，口径通过就入库，
    记录里没有锚点字段。USDCAD 是 D60 清单里给 F1 的币对，不在 F3 的
    名单里，这次不在清单内。"""
    s, rec = _gate(_year_zip("us_wall", FIX_MINUS_1H), pair)
    assert s is not None and rec["used"] is True, rec
    assert rec["calendar"] == "us_wall"
    assert "fix_anchor_month_end" not in rec
    assert "anchor_note" not in rec
