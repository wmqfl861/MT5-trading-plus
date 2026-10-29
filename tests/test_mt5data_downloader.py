from collections import namedtuple
from datetime import datetime, timedelta, timezone

import pytest

from mt5gw.connection import MT5Connection
from mt5gw.errors import DataUnavailableError
from mt5gw.market import MarketData
from mt5gw.symbols import SymbolCatalog
from mt5data import downloader
from mt5data.downloader import ALL_TIMEFRAMES, backfill_series, refresh_series
from mt5data.store import save_bars

from tests.fakes import FakeMT5

SymRow = namedtuple(
    "SymRow",
    "name description digits point spread trade_mode currency_profit currency_margin "
    "trade_contract_size volume_min volume_max volume_step filling_mode visible",
)
TickInfo = namedtuple("TickInfo", "time bid ask last volume")
RateRow = namedtuple("RateRow", "time open high low close tick_volume spread real_volume")

EUR = SymRow(
    name="EURUSD", description="", digits=5, point=0.00001, spread=12,
    trade_mode=4, currency_profit="USD", currency_margin="USD",
    trade_contract_size=100000.0, volume_min=0.01, volume_max=100.0,
    volume_step=0.01, filling_mode=1, visible=True,
)

BASE = datetime(2023, 10, 22, 12, 0)


MONDAY = datetime(2023, 10, 23, 12, 0)  # 周一基线：构造"交易日内的数据洞"


def make_gateway(total_history, now_min, now_sec=30, skip_positions=(), base=None):
    """伪造 total_history 根连续 M1 历史；'现在'在 now_min 分 now_sec 秒处。

    真实语义：pos 0 最新，越大越老；p 号 bar 时间 = now - p 分钟。
    state["wall"] 是可注入的墙钟（默认与 tick 同步），skip_positions 模拟空洞。
    """
    fake = FakeMT5()
    conn = MT5Connection(mt5_module=fake)
    anchor = base or BASE
    state = {
        "now": anchor + timedelta(minutes=now_min, seconds=now_sec),
        "wall": None,  # None → 跟随 tick（offset=0）
        "total": total_history,
        "fail_at_chunk": None,  # {chunk序号: 异常}
        "counter": {"n": 0},
        "skip": set(skip_positions),
    }

    def copy_rates(symbol, timeframe, start_pos, count):
        state["counter"]["n"] += 1
        if state["fail_at_chunk"] == state["counter"]["n"]:
            raise DataUnavailableError("模拟：第 %d 块失败" % state["counter"]["n"])
        rows = []
        for p in range(start_pos, start_pos + count):
            if p >= state["total"] or p in state["skip"]:
                continue
            t = state["now"] - timedelta(minutes=p)
            rows.append(RateRow(
                int(t.replace(tzinfo=timezone.utc).timestamp()),
                1.0, 1.0, 1.0, 1.0, 10, 1, 0))
        return tuple(rows)

    fake.set_behavior("copy_rates_from_pos", value=copy_rates)
    fake.set_behavior("symbol_info", value=EUR)
    fake.set_behavior("symbol_select", value=True)

    def now_epoch(symbol=None):
        return int(state["now"].replace(tzinfo=timezone.utc).timestamp())

    fake.set_behavior("symbol_info_tick", value=lambda symbol=None: TickInfo(
        now_epoch(), 1.1, 1.2, 1.15, 7))

    def fake_wall():
        return state["wall"] or state["now"]

    fake.state = state
    fake.anchor = anchor
    return fake, MarketData(conn, fake), SymbolCatalog(conn, fake), fake_wall


@pytest.fixture
def pinned_wall(monkeypatch):
    """把下载器的墙钟固定为各测试注入的值。"""
    holder = {"fn": lambda: BASE}

    def set_wall(dt):
        holder["fn"] = lambda: dt

    monkeypatch.setattr(downloader, "_utcnow", lambda: holder["fn"]())
    return set_wall


def test_all_timeframes_is_21_canonical_ordered():
    assert len(ALL_TIMEFRAMES) == 21
    assert ALL_TIMEFRAMES[:3] == ["M1", "M2", "M3"]
    assert ALL_TIMEFRAMES[-3:] == ["D1", "W1", "MN1"]


def test_backfill_walks_chunks_and_drops_forming_bar(tmp_path, pinned_wall):
    fake, market, symbols, wall = make_gateway(total_history=250, now_min=249)
    pinned_wall(wall())  # 墙钟与 tick 同步（offset=0）→ now_server == tick now
    stats = backfill_series(tmp_path, market, "EURUSD", "M1", chunk=100)
    assert stats["rows"] == 249  # 最后一根（open==now）未收盘被剔除
    assert stats["chunks_fetched"] >= 3
    assert stats["first_time"].startswith("2023-10-22T12:00")
    assert stats["last_time"].startswith("2023-10-22T16:08")


def test_backfill_rerun_never_duplicates_or_shrinks(tmp_path, pinned_wall):
    fake, market, symbols, wall = make_gateway(total_history=120, now_min=119)
    pinned_wall(wall())
    stats = backfill_series(tmp_path, market, "EURUSD", "M1", chunk=100)
    assert stats["rows"] == 119
    stats2 = backfill_series(tmp_path, market, "EURUSD", "M1", chunk=100)
    assert stats2["rows"] == 119  # 与旧文件合并，不重复


def test_backfill_midway_error_propagates_and_keeps_old_file(tmp_path, pinned_wall):
    # C3：先落一份 120 根的完整历史
    fake, market, symbols, wall = make_gateway(total_history=120, now_min=119)
    pinned_wall(wall())
    backfill_series(tmp_path, market, "EURUSD", "M1", chunk=100)
    before = (tmp_path / "bars" / "EURUSD" / "M1" / "data.parquet").read_bytes()

    # 再回填时第 2 块失败 → 必须抛错，且旧文件一字节不动
    fake2, market2, _, wall2 = make_gateway(total_history=250, now_min=249)
    fake2.state["fail_at_chunk"] = 2
    pinned_wall(wall2())
    with pytest.raises(DataUnavailableError, match="第 2 块失败"):
        backfill_series(tmp_path, market2, "EURUSD", "M1", chunk=100)
    after = (tmp_path / "bars" / "EURUSD" / "M1" / "data.parquet").read_bytes()
    assert before == after


def test_backfill_zero_rows_is_failure(tmp_path, pinned_wall):
    fake, market, symbols, wall = make_gateway(total_history=0, now_min=10)
    pinned_wall(wall())
    with pytest.raises(DataUnavailableError, match="未取到任何数据"):
        backfill_series(tmp_path, market, "EURUSD", "M1")


def test_backfill_shrink_guard_rejects_force_unions_replace_backs_up(tmp_path, pinned_wall):
    import glob
    # 先存 300 根"深历史"
    fake, market, symbols, wall = make_gateway(total_history=300, now_min=299)
    pinned_wall(wall())
    backfill_series(tmp_path, market, "EURUSD", "M1", chunk=200)

    # 服务器现在只给得出 100 根（同步不完整）→ 拒绝写
    fake2, market2, _, wall2 = make_gateway(total_history=100, now_min=99)
    pinned_wall(wall2())
    with pytest.raises(DataUnavailableError, match="疑似截断"):
        backfill_series(tmp_path, market2, "EURUSD", "M1", chunk=100)
    # N4：force 是并集（不丢行），不是覆盖
    stats = backfill_series(tmp_path, market2, "EURUSD", "M1", chunk=100, force=True)
    assert stats["rows"] == 299  # 旧 300 - 形成中 1 根，并集保留深历史
    # 彻底替换走 replace，且自动备份
    stats2 = backfill_series(tmp_path, market2, "EURUSD", "M1", chunk=100, replace=True)
    assert stats2["rows"] == 99
    baks = glob.glob(str(tmp_path / "bars" / "EURUSD" / "M1" / "data.parquet.bak-*"))
    assert len(baks) == 1  # 旧数据已备份，可恢复


def test_weekend_frozen_quote_keeps_last_closed_bar(tmp_path, pinned_wall):
    # 周五 23:59:30 行情冻结，墙钟已走到周日：offset 超限回退 → 周五最后一根已收盘保留
    fake, market, symbols, wall = make_gateway(total_history=100, now_min=99, now_sec=30)
    friday = BASE + timedelta(minutes=99, seconds=30)
    sunday = friday + timedelta(days=2)
    pinned_wall(sunday)
    stats = backfill_series(tmp_path, market, "EURUSD", "M1", chunk=200)
    assert stats["rows"] == 100  # 无一根被误删（含周五最后一根）
    assert stats["last_time"] == friday.replace(tzinfo=None).isoformat()


def test_refresh_appends_new_bars(tmp_path, pinned_wall):
    fake, market, symbols, wall = make_gateway(total_history=100, now_min=99)
    pinned_wall(wall())
    backfill_series(tmp_path, market, "EURUSD", "M1", chunk=100)
    # 时间前进 10 分钟、历史多 10 根（墙钟与 tick 同步推进）
    fake.state["now"] = BASE + timedelta(minutes=109, seconds=30)
    fake.state["total"] = 110
    pinned_wall(fake.state["now"])
    stats = refresh_series(tmp_path, market, "EURUSD", "M1", lookback=50)
    assert stats["rows"] == 109  # 110 根收盘（第 110 根形成中被剔除）
    assert stats["new_bars"] == 10


def test_refresh_requires_overlap_proof(tmp_path, pinned_wall):
    # 周一基线；旧末根对应 p11。把 p11 从服务端抹掉 → 翻页永远盖不到旧末根 → 报错
    fake, market, symbols, wall = make_gateway(total_history=100, now_min=99, base=MONDAY)
    pinned_wall(wall())
    backfill_series(tmp_path, market, "EURUSD", "M1", chunk=100)
    fake.state["now"] = MONDAY + timedelta(minutes=109, seconds=30)
    fake.state["total"] = 110
    fake.state["skip"] = {11}  # 旧末根那一根服务端缺失
    fake.state["counter"]["n"] = 0
    pinned_wall(fake.state["now"])
    with pytest.raises(DataUnavailableError, match="接续"):
        refresh_series(tmp_path, market, "EURUSD", "M1", lookback=50)


def test_refresh_exact_multiple_boundary_no_spurious_error(tmp_path, pinned_wall):
    # m5：历史根数恰为页大小整数倍（100 根、页 99+1=100）→ 不得因越界报错
    fake, market, symbols, wall = make_gateway(total_history=200, now_min=199, base=MONDAY)
    pinned_wall(wall())
    stats = backfill_series(tmp_path, market, "EURUSD", "M1", chunk=100)
    assert stats["rows"] == 199  # 200 根去形成中 1 根，边界无异常


def test_refresh_weekend_gap_is_allowed(tmp_path, pinned_wall):
    # 周日基线：休市造成的缺口按正常放行（新问题1 的回归测试）
    fake, market, symbols, wall = make_gateway(total_history=100, now_min=99)  # BASE=周日
    pinned_wall(wall())
    backfill_series(tmp_path, market, "EURUSD", "M1", chunk=100)
    fake.state["now"] = BASE + timedelta(minutes=109, seconds=30)
    fake.state["total"] = 110
    fake.state["skip"] = set(range(6, 11))
    fake.state["counter"]["n"] = 0
    pinned_wall(fake.state["now"])
    stats = refresh_series(tmp_path, market, "EURUSD", "M1", lookback=50)
    assert stats["rows"] == 104  # 旧 99 根 + 5 根新收盘（缺口 5 根休市不计）
    assert stats["new_bars"] == 5


def test_refresh_without_old_file_falls_back_to_backfill(tmp_path, pinned_wall):
    fake, market, symbols, wall = make_gateway(total_history=80, now_min=79)
    pinned_wall(wall())
    stats = refresh_series(tmp_path, market, "EURUSD", "M1", lookback=50)
    assert stats["rows"] == 79  # 全量回填而非只存 50 根


# ----------------------------------------------- H-11：写锁覆盖读改写过程


def test_two_writers_disjoint_segments_both_survive(tmp_path, monkeypatch):
    """H-11 失败测试：两个写者分别新增不相交时间段，合并后两段都在。

    写者 A（深回填）在**抓取期间**，写者 B（向前刷新）完整跑完一轮：
    B 读到旧表、发布了靠后的新段。A 若拿抓取前的旧快照合并发布（只有
    save_bars 的写段在锁内），就会把 B 刚发布的段覆盖丢——丢更新。
    修复后"读最新表→合并→校验→发布"在序列写锁内，A 在锁内重读最新
    表再合并，深段（A）与近段（B）同时保留。
    """
    import pandas as pd

    from mt5data.store import load_bars

    real_fetch = downloader._fetch_pages
    fake_a, market_a, _, _ = make_gateway(total_history=100, now_min=99)
    fake_b, market_b, _, _ = make_gateway(total_history=110, now_min=109)
    walls = {"cur": fake_a.state["now"]}
    monkeypatch.setattr(downloader, "_utcnow", lambda: walls["cur"])
    armed = {"go": False}

    def seam(market, symbol, timeframe, chunk, max_chunks, stop_before):
        # A 的深回填抓取开始时，让 B 先完整刷新一轮（读旧表→发布新段）
        if armed["go"] and market is market_a:
            armed["go"] = False
            walls["cur"] = fake_b.state["now"]  # B 的墙钟（B 自校准用）
            refresh_series(str(tmp_path), market_b, "EURUSD", "M1", lookback=50)
            walls["cur"] = fake_a.state["now"]  # 换回 A 的墙钟
        return real_fetch(market, symbol, timeframe, chunk, max_chunks,
                          stop_before)

    monkeypatch.setattr(downloader, "_fetch_pages", seam)

    # 初始：99 根收盘 [T0+0:30 → T0+98:30]
    backfill_series(str(tmp_path), market_a, "EURUSD", "M1", chunk=200)
    # A：终端出现深历史（total 100→300，新增靠前的段）；抓取期间 B 已把
    # 库推进到 109 根（新增靠后的段 [T0+99:30 → T0+108:30]）
    fake_a.state["total"] = 300
    armed["go"] = True
    stats = backfill_series(str(tmp_path), market_a, "EURUSD", "M1", chunk=500)

    final = load_bars(str(tmp_path), "EURUSD", "M1")
    assert stats["rows"] == 309, f"B 的段被 A 的旧快照覆盖丢了（剩 {stats['rows']}）"
    assert len(final) == 309
    # 深段（A 新增，最老一根 now-299m）与近段（B 新增，最新一根 now-1m）都在
    assert final["time"].iloc[0] == pd.Timestamp(fake_a.state["now"]) \
        - pd.Timedelta(minutes=299)
    assert final["time"].iloc[-1] == pd.Timestamp(fake_b.state["now"]) \
        - pd.Timedelta(minutes=1)


def test_refresh_keeps_registered_known_gaps(tmp_path, pinned_wall):
    """H-11 失败测试：先登记 known_gaps → 再刷新 → 登记仍在（且水位前进）。"""
    from mt5data.quality import read_known_gaps, write_known_gaps
    from mt5data.store import read_watermark

    fake, market, symbols, wall = make_gateway(total_history=100, now_min=99)
    pinned_wall(wall())
    backfill_series(str(tmp_path), market, "EURUSD", "M1", chunk=200)

    write_known_gaps(str(tmp_path), "EURUSD", "M1", [
        {"start": "2021-05-17T14:00:00", "end": "2021-06-14T11:00:00",
         "reason": "终端无该段历史"}])

    fake.state["now"] = BASE + timedelta(minutes=109, seconds=30)
    fake.state["total"] = 110
    pinned_wall(fake.state["now"])
    stats = refresh_series(str(tmp_path), market, "EURUSD", "M1", lookback=50)

    assert stats["rows"] == 109  # 刷新确实发生
    assert read_watermark(str(tmp_path), "EURUSD", "M1")["rows"] == 109
    assert read_known_gaps(str(tmp_path), "EURUSD", "M1") == [
        (datetime(2021, 5, 17, 14, 0), datetime(2021, 6, 14, 11, 0))]
