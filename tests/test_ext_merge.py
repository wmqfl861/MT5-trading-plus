"""T97/M1：ext 合并取数管线 mt5data.ext.merge（合成数据，tmp_path 最小库）。

对照 docs/superpowers/plans/2026-10-29-p-line-t97-data-unlock.md（v2）M1
与先决 1/4：
- 排序去重 / 相同 time 冲突 MT5 为准（重叠期 MT5 胜出）；ext 只取 MT5
  first_time 之前（前向延长）；
- 只取 yearly_verification[*].used==true 的年份段（used=false 与未列
  年份都不进）；
- 裁 research_cutoff：ext 段铁律 19 严格 <（manifest.research_cutoff）；
  MT5 段另受 meta.last_closed_time 含端点约束——合并有效上界 = 两者
  更早者（口径见 merge.py docstring）；
- 月度周日开盘小时自检（仅 ext 段，MT5 段豁免）：正常月唯一；美国 DST
  切换月允许两个相邻小时；违例月记入 bad_months、从序列剔除且两端
  记入 gap_spans；
- gap_spans：>30 天相邻间隔，恰好 30 天不算（与
  mt5quant.research.candidates._GAP_SPLIT 口径一致，含直接对拍）；
- source_tags 与 bars 等长（"mt5" / "ext:histdata:2009" 式）。
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from mt5data.ext.approvals import USAGE_RESTRICTION_HISTDATA
from mt5data.ext.core import ExtSeries, save_ext_series
from mt5data.ext.merge import (MAX_GAP_DAYS, concat_mt5_priority,
                               merged_research_series)
from mt5data.store import _COLUMNS, save_bars

SYM, TF = "XAUUSD", "H1"


# ---------------------------------------------------------------- 夹具工具

def make_bars(times, close=900.0):
    """合成 H1 bar；close 恒定以区分来源（MT5=900，ext=100）。"""
    t = pd.DatetimeIndex(sorted(pd.to_datetime(list(times))))
    return pd.DataFrame({
        "time": t,
        "open": close, "high": close, "low": close, "close": close,
        "tick_volume": np.arange(1, len(t) + 1, dtype="int64"),
        "spread": np.zeros(len(t), dtype="int64"),
        "real_volume": np.zeros(len(t), dtype="int64"),
    })[_COLUMNS]


def mondays(start, periods):
    # pandas 3 周锚定别名为三字母（W-MON，旧式 W-MONDAY 已移除）
    return list(pd.date_range(start, periods=periods, freq="W-MON"))


def research_root(tmp_path):
    return str(tmp_path / "data_research")


def write_mt5(root, df, last_closed=None):
    """写 MT5 主库（save_bars 原子落盘）；last_closed 可改写水位边界。"""
    save_bars(root, SYM, TF, df)
    if last_closed is not None:
        p = Path(root) / "bars" / SYM / TF / "meta.json"
        meta = json.loads(p.read_text(encoding="utf-8"))
        meta["last_closed_time"] = pd.Timestamp(last_closed).isoformat()
        p.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                     encoding="utf-8")


def write_ext(root, df, *, research_cutoff="2099-01-01T00:00:00", yearly=None,
              source="histdata"):
    """写一份带 manifest 的 ext 序列（{root}/ext/...）。

    research_cutoff=None → manifest 不带该字段（P-01"来源 meta 缺失"场景）；
    source 可换其他批准来源（P-01"多来源冲突"场景）。
    """
    manifest = {
        "source": source,
        "source_url": "https://example.invalid/" + source,
        "symbol": SYM,
        "timeframe": TF,
        "price_side": "bid（合成测试）",
        "original_timezone": "合成 naive UTC",
        "dst_rule": "无（合成）",
        "terms_note": "合成测试序列，非真实来源",
        "downloaded_at_utc": "2026-10-29T00:00:00+00:00",
        "source_files": [{"name": "synthetic.zip", "sha256": "0" * 64}],
        "converter_version": 1,
        "usage_restriction": USAGE_RESTRICTION_HISTDATA,
        "yearly_verification": {} if yearly is None else yearly,
        "rows": int(len(df)),
        "first_time": pd.Timestamp(df["time"].iloc[0]).isoformat(),
        "last_time": pd.Timestamp(df["time"].iloc[-1]).isoformat(),
    }
    if research_cutoff is not None:
        manifest["research_cutoff"] = research_cutoff
    save_ext_series(str(Path(root) / "ext"),
                    ExtSeries(df=df, manifest=manifest))


def write_lockbox(root, global_cutoff):
    """写 v2 全局锁箱配置（主库场景；研究导出库没有此文件——H-01）。"""
    Path(root).mkdir(parents=True, exist_ok=True)
    (Path(root) / "lockbox.json").write_text(json.dumps({
        "version": 2,
        "global_cutoff": pd.Timestamp(global_cutoff).isoformat(),
        "note": "合成测试（P-01）",
    }, ensure_ascii=False), encoding="utf-8")


def sundays_2013():
    """2013 年每个周日一根 H1 bar，小时按月编排（自检的三类月）：

    - 正常月（1/2/6–12 月）恒 22:00（唯一，应放行）；
    - 3 月含美国 DST 切换（2013-03-10）：切换前周日 23:00、切换后 22:00
      （两个相邻小时，切换月应放行）；
    - 4 月不含切换但 22/23 混杂（应判坏）；5 月 0 点与 3 点（不相邻，
      应判坏）。
    """
    out = []
    for d in pd.date_range("2013-01-01", "2013-12-31", freq="D"):
        if d.dayofweek != 6:
            continue
        hour = {1: 22, 2: 22, 3: 23, 4: 22, 5: 0}.get(d.month, 22)
        if d.month == 3 and d.day >= 10:
            hour = 22
        if d.month == 4 and d.day >= 14:
            hour = 23
        if d.month == 5 and d.day >= 19:
            hour = 3
        out.append(d + pd.Timedelta(hours=hour))
    return out


# ================================================================ 去重与前向延长

class TestConcatDedup:
    def test_same_time_mt5_row_wins(self):
        """相同 time 的重复 bar 以 MT5 为准（先决 1：重叠期 MT5 胜出）。"""
        mt5 = make_bars(["2011-01-03 00:00", "2011-01-10 00:00"])
        ext = make_bars(["2010-12-27 00:00", "2011-01-03 00:00",
                         "2011-01-10 00:00"], close=100.0)
        merged = concat_mt5_priority(mt5, ext)
        assert list(merged["time"]) == [pd.Timestamp("2010-12-27"),
                                        pd.Timestamp("2011-01-03"),
                                        pd.Timestamp("2011-01-10")]
        for t in ("2011-01-03", "2011-01-10"):
            row = merged[merged["time"] == pd.Timestamp(t)]
            assert len(row) == 1 and row["close"].iloc[0] == 900.0


class TestForwardExtensionOnly:
    def test_ext_bars_at_or_after_mt5_first_are_dropped(self, tmp_path):
        """ext 只取 MT5 first_time 之前的 bar：同刻/之后的 ext bar 剔除。"""
        root = research_root(tmp_path)
        write_mt5(root, make_bars(["2011-01-03 00:00", "2011-01-10 00:00"]))
        write_ext(root, make_bars(["2010-12-27 00:00", "2011-01-03 00:00",
                                   "2011-01-10 00:00"], close=100.0),
                  yearly={"2010": {"used": True}, "2011": {"used": True}})
        bars, tags, gaps, bad = merged_research_series(root, SYM, TF)
        at_first = bars[bars["time"] == pd.Timestamp("2011-01-03")]
        assert len(at_first) == 1 and at_first["close"].iloc[0] == 900.0
        assert (bars["time"] == pd.Timestamp("2011-01-10")).sum() == 1
        assert tags == ["ext:histdata:2010", "mt5", "mt5"]


# ================================================================ 年份闸门

class TestYearGate:
    def test_used_false_and_unlisted_years_excluded(self, tmp_path):
        """只取 yearly_verification used==true 的年份：used=false 与未列
        年份的 bar 都不进合并序列。"""
        root = research_root(tmp_path)
        write_ext(root, make_bars(mondays("2009-01-05", 156), close=100.0),
                  yearly={"2009": {"used": False}, "2010": {"used": True}})
        write_mt5(root, make_bars(mondays("2012-01-02", 4)))
        bars, tags, gaps, bad = merged_research_series(root, SYM, TF)
        assert sorted(set(bars["time"].dt.year)) == [2010, 2012]
        assert set(tags) == {"ext:histdata:2010", "mt5"}
        # 2010 年末（被启用）到 2012 年初（MT5）之间 371 天 → 恰一个 gap
        assert len(gaps) == 1


# ================================================================ cutoff

class TestCutoff:
    def test_research_cutoff_trims_mt5_tail_strictly(self, tmp_path):
        """research_cutoff 对整条合并序列生效（有效上界=与 last_closed 的
        更早者）：cutoff 落在 MT5 段内时尾部严格 < 裁剪。"""
        root = research_root(tmp_path)
        write_ext(root, make_bars(mondays("2010-01-04", 13), close=100.0),
                  research_cutoff="2011-01-17T00:00:00",
                  yearly={"2010": {"used": True}})
        write_mt5(root, make_bars(mondays("2011-01-03", 9)))
        bars, tags, gaps, bad = merged_research_series(root, SYM, TF)
        assert bars["time"].max() == pd.Timestamp("2011-01-10 00:00")
        assert (bars["time"] >= pd.Timestamp("2011-01-17")).sum() == 0

    def test_research_cutoff_trims_ext_when_no_mt5(self, tmp_path):
        """ext 段铁律 19：严格 < research_cutoff（恰在 cutoff 上的 bar 剔除）。"""
        root = research_root(tmp_path)
        write_ext(root, make_bars(mondays("2010-01-04", 8), close=100.0),
                  research_cutoff="2010-01-26T00:00:00",
                  yearly={"2010": {"used": True}})
        bars, tags, gaps, bad = merged_research_series(root, SYM, TF)
        assert list(bars["time"]) == [pd.Timestamp(d) for d in
                                      ("2010-01-04", "2010-01-11",
                                       "2010-01-18", "2010-01-25")]
        assert tags == ["ext:histdata:2010"] * 4

    def test_last_closed_bound_inclusive_on_mt5(self, tmp_path):
        """MT5 水位语义为"最后一根已收盘 bar"：恰在 last_closed_time 的
        bar 保留（含端点），之后的剔除（防御性裁剪）。"""
        root = research_root(tmp_path)
        write_mt5(root, make_bars(mondays("2011-01-03", 4)),
                  last_closed="2011-01-17T00:00:00")
        bars, tags, gaps, bad = merged_research_series(root, SYM, TF)
        assert list(bars["time"]) == [pd.Timestamp("2011-01-03"),
                                      pd.Timestamp("2011-01-10"),
                                      pd.Timestamp("2011-01-17")]
        assert tags == ["mt5"] * 3


# ================================================================ 周日小时自检

class TestSundayMonthlyCheck:
    def _store(self, tmp_path):
        root = research_root(tmp_path)
        write_ext(root, make_bars(sundays_2013(), close=100.0),
                  yearly={"2013": {"used": True}})
        write_mt5(root, make_bars(mondays("2014-01-06", 4)))
        return root

    def test_bad_months_removed_and_ends_recorded_as_gap(self, tmp_path):
        root = self._store(tmp_path)
        bars, tags, gaps, bad = merged_research_series(root, SYM, TF)
        # 4 月（无 DST 切换却双小时）与 5 月（0/3 不相邻）判坏
        assert bad == ["2013-04", "2013-05"]
        months = bars["time"].dt.to_period("M")
        assert not months.isin([pd.Period("2013-04"),
                                pd.Period("2013-05")]).any()
        # 坏月剔除的两端（前末 bar、后首 bar）记入 gap_spans
        assert gaps == [(pd.Timestamp("2013-03-31 22:00"),
                         pd.Timestamp("2013-06-02 22:00"))]
        assert len(tags) == len(bars)

    def test_dst_switch_month_allows_two_adjacent_hours(self, tmp_path):
        """3 月含美国 DST 切换（2013-03-10）：23:00/22:00 两个相邻小时
        放行，整月保留。"""
        root = self._store(tmp_path)
        bars, tags, gaps, bad = merged_research_series(root, SYM, TF)
        assert "2013-03" not in bad
        n_march = (bars["time"].dt.to_period("M")
                   == pd.Period("2013-03")).sum()
        assert n_march == 5  # 3 月 5 个周日全在

    def test_normal_months_unique_hour_pass(self, tmp_path):
        """正常月（不含美国 DST 切换）周日首 bar 小时唯一 → 放行。"""
        root = self._store(tmp_path)
        bars, tags, gaps, bad = merged_research_series(root, SYM, TF)
        assert "2013-01" not in bad and "2013-02" not in bad
        assert "2013-11" not in bad and "2013-12" not in bad
        # 2013 全年 52 个周日，剔 4/5 两月各 4 个 → 44 根 ext + 4 根 MT5
        assert len(bars) == 48

    def test_mt5_segment_exempt_from_sunday_check(self, tmp_path):
        """MT5 段豁免：周日首 bar 小时混杂不判坏、不剔除。"""
        root = research_root(tmp_path)
        times = []
        for d in pd.date_range("2014-01-05", "2014-02-02", freq="W-SUN"):
            hour = 22 if (d.day // 7) % 2 == 0 else 23
            times.append(d + pd.Timedelta(hours=hour))
        write_mt5(root, make_bars(times))
        bars, tags, gaps, bad = merged_research_series(root, SYM, TF)
        assert bad == []
        assert len(bars) == 5


# ================================================================ gap_spans

class TestGapSpans:
    def test_exactly_30_days_is_not_a_gap(self, tmp_path):
        """恰 30 天的相邻间隔不算（与 _GAP_SPLIT 同口径）；>30 天才算。"""
        root = research_root(tmp_path)
        write_mt5(root, make_bars([
            "2011-01-03 00:00", "2011-02-02 00:00",   # 恰 30 天
            "2011-03-04 01:00",                        # 30 天 + 1 小时
            "2011-04-03 01:00",                        # 恰 30 天
        ]))
        bars, tags, gaps, bad = merged_research_series(root, SYM, TF)
        assert MAX_GAP_DAYS == 30
        assert gaps == [(pd.Timestamp("2011-02-02 00:00"),
                         pd.Timestamp("2011-03-04 01:00"))]
        assert tags == ["mt5"] * 4

    def test_boundary_parity_with_candidates_gap_split(self, tmp_path):
        """与 mt5quant.research.candidates._observed_gaps 直接对拍：同一条
        时间序列上的洞集合逐位一致（先决 4 引用的 _GAP_SPLIT 口径）。"""
        from mt5quant.research.candidates import _observed_gaps
        times = ["2011-01-03 00:00", "2011-02-02 00:00",
                 "2011-03-04 01:00", "2011-04-03 01:00",
                 "2011-05-04 01:00"]
        root = research_root(tmp_path)
        write_mt5(root, make_bars(times))
        _, _, gaps, _ = merged_research_series(root, SYM, TF)
        assert list(gaps) == list(_observed_gaps(
            pd.DatetimeIndex(pd.to_datetime(times))))


# ================================================================ source_tags

class TestSourceTags:
    def test_tags_equal_length_and_segment_labels(self, tmp_path):
        root = research_root(tmp_path)
        write_ext(root, make_bars(mondays("2010-01-04", 52), close=100.0),
                  yearly={"2010": {"used": True}})
        write_mt5(root, make_bars(mondays("2011-01-03", 52)))
        bars, tags, gaps, bad = merged_research_series(root, SYM, TF)
        assert len(tags) == len(bars) == 104
        assert tags[:52] == ["ext:histdata:2010"] * 52
        assert tags[52:] == ["mt5"] * 52
        assert bars["time"].is_monotonic_increasing
        assert bars["time"].is_unique
        assert gaps == [] and bad == []


class TestEmptyRoot:
    def test_no_data_returns_empty_quadruple(self, tmp_path):
        root = research_root(tmp_path)  # 目录不存在
        bars, tags, gaps, bad = merged_research_series(root, SYM, TF)
        assert len(bars) == 0
        assert list(bars.columns) == list(_COLUMNS)
        assert tags == [] and gaps == [] and bad == []


# ================================================================ P-01 统一上界

class TestP01UnifiedUpperBound:
    """T98/P-01：合并后全序列统一校验有效上界（取最严）——全局锁箱
    cutoff（data_root 下有 lockbox 配置才生效，研究导出库没有属既有设计、
    不因此拒绝——H-01）、ext manifest research_cutoff（ext 在库却缺该字段
    → 保守拒绝）、导出 meta source_cutoff、MT5 水位 last_closed_time（含
    端点）、显式 end（含端点；只收紧不放宽——比其他界晚则被支配）。"""

    def test_main_store_lockbox_cutoff_bounds_merged(self, tmp_path):
        """主库有 lockbox 配置时 cutoff 生效：合并序列严格 < 全局 cutoff
        （水位 last_closed 更晚也不能放出锁箱段——P-01 复现的反面）。"""
        root = research_root(tmp_path)
        write_mt5(root, make_bars(mondays("2011-01-03", 9)))  # 到 2011-03-07
        write_lockbox(root, "2011-02-01T00:00:00")
        bars, tags, gaps, bad = merged_research_series(root, SYM, TF)
        # 水位 last_closed=2011-03-07 晚于 cutoff → 生效的是 cutoff（严格 <）
        assert bars["time"].max() == pd.Timestamp("2011-01-31")
        assert (bars["time"] >= pd.Timestamp("2011-02-01")).sum() == 0
        assert len(bars) == 5  # 1 月的 5 个周一
        assert tags == ["mt5"] * 5

    def test_export_store_without_lockbox_not_rejected(self, tmp_path):
        """导出库无 lockbox.json 不拒绝：只用 ext manifest cutoff 与水位
        （data_research 既有设计——H-01 警告：不要求必须有 lockbox）。"""
        root = research_root(tmp_path)
        write_ext(root, make_bars(mondays("2010-01-04", 52), close=100.0),
                  yearly={"2010": {"used": True}})
        write_mt5(root, make_bars(mondays("2011-01-03", 4)))
        assert not (Path(root) / "lockbox.json").exists()
        bars, tags, gaps, bad = merged_research_series(root, SYM, TF)
        assert len(bars) == 56  # 52 ext + 4 MT5，无锁箱文件也正常合并
        assert bars["time"].max() == pd.Timestamp("2011-01-24")

    def test_ext_manifest_without_research_cutoff_rejected(self, tmp_path):
        """ext 数据在库但 manifest 缺 research_cutoff → 来源上界不明，
        保守拒绝（主库 ext 未经导出盖章即此形态——不静默放行）。"""
        root = research_root(tmp_path)
        write_ext(root, make_bars(mondays("2010-01-04", 13), close=100.0),
                  research_cutoff=None, yearly={"2010": {"used": True}})
        write_mt5(root, make_bars(mondays("2011-01-03", 4)))
        with pytest.raises(ValueError, match="research_cutoff"):
            merged_research_series(root, SYM, TF)

    def test_conflicting_ext_sources_rejected(self, tmp_path):
        """多个已批准 ext 来源同时提供同一序列 → 来源 meta 冲突，保守拒绝
        （不再按字母序静默选边）。"""
        root = research_root(tmp_path)
        write_ext(root, make_bars(mondays("2010-01-04", 13), close=100.0),
                  yearly={"2010": {"used": True}}, source="cdd")
        write_ext(root, make_bars(mondays("2010-01-04", 13), close=100.0),
                  yearly={"2010": {"used": True}}, source="histdata")
        write_mt5(root, make_bars(mondays("2011-01-03", 4)))
        with pytest.raises(ValueError, match="冲突"):
            merged_research_series(root, SYM, TF)

    def test_end_earlier_than_all_bounds_truncates(self, tmp_path):
        """end 比所有上界早时生效截断（含端点：恰在 end 上的 bar 保留，
        与 load_series/research_bars 的 end 语义一致）。"""
        root = research_root(tmp_path)
        write_ext(root, make_bars(mondays("2010-01-04", 52), close=100.0),
                  yearly={"2010": {"used": True}})
        write_mt5(root, make_bars(mondays("2011-01-03", 4)))
        bars, tags, gaps, bad = merged_research_series(
            root, SYM, TF, end="2011-01-17T00:00:00")
        assert bars["time"].max() == pd.Timestamp("2011-01-17")
        assert (bars["time"] > pd.Timestamp("2011-01-17")).sum() == 0
        assert len(bars) == 55  # 52 ext + 3 MT5（01-03/10/17）
        assert len(tags) == len(bars)

    def test_end_later_than_cutoff_dominated_by_cutoff(self, tmp_path):
        """end 比全局 cutoff 晚 → 被更严的 cutoff 支配（合并路径 end 不
        报"越界"，但绝不可能借 end 读出锁箱段——上界取最严）。"""
        root = research_root(tmp_path)
        write_mt5(root, make_bars(mondays("2011-01-03", 9)))
        write_lockbox(root, "2011-02-01T00:00:00")
        bars, _tags, _gaps, _bad = merged_research_series(
            root, SYM, TF, end=pd.Timestamp("2099-01-01T00:00:00"))
        assert bars["time"].max() == pd.Timestamp("2011-01-31")
        assert len(bars) == 5

    def test_end_none_strictest_bound_applies(self, tmp_path):
        """end=None 时多界并存取最严：lockbox=2011-03-01、ext manifest
        research_cutoff=2011-02-15、水位=2011-03-07 → 生效 02-15（严格 <）。"""
        root = research_root(tmp_path)
        write_ext(root, make_bars(mondays("2010-01-04", 13), close=100.0),
                  research_cutoff="2011-02-15T00:00:00",
                  yearly={"2010": {"used": True}})
        write_mt5(root, make_bars(mondays("2011-01-03", 9)))  # 到 03-07
        write_lockbox(root, "2011-03-01T00:00:00")
        bars, tags, gaps, bad = merged_research_series(root, SYM, TF)
        assert bars["time"].max() == pd.Timestamp("2011-02-14")
        assert (bars["time"] >= pd.Timestamp("2011-02-15")).sum() == 0
        # ext 13 根全在 2010（远早于全部界）；MT5 侧 01-03..02-14 共 7 根
        assert tags.count("mt5") == 7 and len(bars) == 20

    def test_distinct_bars_both_sides_of_cutoff_confined(self, tmp_path):
        """合成 cutoff 两侧插入可辨别 bar：ext（close=100）与 MT5（close=900）
        各有界两侧的 bar，统一裁剪后开放侧两段都在、锁箱侧零泄漏。"""
        root = research_root(tmp_path)
        # ext：2011 年周一 30 根（跨 cutoff 两侧）；MT5：2011-05-02 起 16 根
        write_ext(root, make_bars(mondays("2011-01-03", 30), close=100.0),
                  yearly={"2011": {"used": True}})
        write_mt5(root, make_bars(mondays("2011-05-02", 16)))
        write_lockbox(root, "2011-06-01T00:00:00")
        bars, tags, gaps, bad = merged_research_series(root, SYM, TF)
        assert bars["time"].max() == pd.Timestamp("2011-05-30")
        assert (bars["time"] >= pd.Timestamp("2011-06-01")).sum() == 0
        # 开放侧两段都在（可辨别 close 各自保留），锁箱侧全部消失
        closes = set(bars["close"].unique())
        assert closes == {100.0, 900.0}
        # ext 前向延长只留 2011-01..04（17 根周一），MT5 只留 5 月（5 根）
        assert tags.count("mt5") == 5 and len(bars) == 22
