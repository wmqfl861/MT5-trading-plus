"""Tick 历史采集与存储：按月分区 Parquet + 毫秒级水位。

布局（R3 起）：{root}/ticks/{SYMBOL}/{YYYY}/{MM}.parquet + meta.json
（rows/first_msc/last_msc/months/updated_at_utc/server/login）。
月份按 time_msc 所在的**服务器时间**月份划分（time_msc 是贴 UTC 标签的
服务器时间毫秒，直接取其日历月）。旧单文件布局（{SYMBOL}/data.parquet）
在读取/写入时抛 TicksLayoutError——先跑 scripts/migrate_ticks_layout.py。

约定与 bars 一致：时间为服务器时间贴 UTC 标签的 naive datetime；tick 是
已发生的事实，没有"未收盘"裁剪。写入一律原子（临时文件 + os.replace
带重试）；跨进程序列锁加在品种目录级，覆盖"重算旧水位 → 读旧月 →
整行去重合并 → 逐月写 → 重算并写 meta"整个序列（网络抓取在锁外，与
旧版一致，避免长回补占锁触发残锁接管）。

数据保护铁律（对齐 mt5data.downloader）：
- 回补按时间窗从 end 往回分块（copy_ticks_range + COPY_TICKS_ALL）；
  连续 3 个空窗视为历史尽头正常收尾，不算错误；
- 块内 time_msc 必须单调不倒退——真包同一毫秒可有多条 tick（bid/ask 更新），
  相邻相等是正常数据；只有出现倒退（后一条更早）才判历史损坏，拒绝写盘；
- 单块返回 None 按 last_error 如实上抛，绝不吞错落残缺数据；
- 刷新用"重叠证明"验证接续：抓回页必须含旧末条 time_msc 那一条；
  空抓回且静默不足 7 天视为周末/休市，返回 0；
- 回补/刷新只读写涉及月份的分区文件：窗口结果先收集进列表、最后一次
  concat（不逐块累加），按月分组后逐月"读旧月→合并→原子写"，绝不整库
  读改写；刷新只动 last_msc 所在月及之后的分区（更早月份一字节不动；
  from_s 按秒向下取整，末条恰在月初首秒时会多读上一月，属边界冗余）；
- 水位 rows/first_msc/last_msc/months 每次写盘后从各月 parquet 的元数据
  （num_rows 与 time_msc 列统计）重算，不做算术递增；读取枚举月份以磁盘
  为准，防异常中断留下陈旧 meta 漏月。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from mt5gw.errors import DataUnavailableError
from mt5data.store import atomic_replace, fsync_file, series_lock

logger = logging.getLogger(__name__)

# 贴真包 MetaTrader5.copy_ticks_range 结构化数组的字段（名字与顺序一致；
# 存储位宽：time_msc 毫秒 int64，bid/ask/last/volume float64，flags int64）
TICK_COLUMNS = ["time_msc", "bid", "ask", "last", "volume", "flags"]

_TICK_DTYPES = {
    "time_msc": "int64",
    "bid": "float64",
    "ask": "float64",
    "last": "float64",
    "volume": "float64",
    "flags": "int64",
}

_EMPTY_WINDOW_LIMIT = 3  # 连续空窗达到该块数 → 视为历史尽头正常收尾
_QUIET_DAYS = 7          # refresh 空抓回且静默不足该天数 → 视为休市，返回 0
_TO_MARGIN_S = 60        # refresh 上界放宽 60 秒，容下恰好落在 now 的 tick

_OLD_LAYOUT_FILENAME = "data.parquet"  # 旧单文件布局（R3 之前）

_EPOCH = datetime(1970, 1, 1)


class DataIntegrityError(DataUnavailableError):
    """tick 历史完整性异常（接续无法证明/块内乱序）——拒绝写盘，如实上抛。"""


class TicksLayoutError(DataUnavailableError):
    """检测到旧单文件布局（{SYMBOL}/data.parquet）——先跑迁移脚本再用。"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _epoch_s(dt: datetime) -> int:
    # 与 mt5gw.models._epoch_seconds 同一约定：只接受贴 UTC 标签的 naive
    if dt.tzinfo is not None:
        raise ValueError(
            "只接受贴 UTC 标签的 naive datetime（服务器时间），"
            f"收到 tzinfo={dt.tzinfo}"
        )
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


def ticks_dir(root, symbol: str) -> str:
    # 品种名保留原大小写（XAUUSDm 等带后缀品种大小写敏感）
    return os.path.join(root, "ticks", symbol)


# ---- 月份分区工具：time_msc（服务器时间毫秒）→ 月序号（0 基，1970-01 起） ----


def _month_index_from_msc(msc: int) -> int:
    dt = _EPOCH + timedelta(milliseconds=int(msc))
    return (dt.year - 1970) * 12 + dt.month - 1


def _month_index_array(msc) -> np.ndarray:
    """整列 time_msc → 月序号数组（datetime64 直算，不逐行建对象）。"""
    t = np.asarray(msc, dtype="int64")
    return (t * 1_000_000).astype("datetime64[ns]").astype(
        "datetime64[M]").astype("int64")


def _month_str(m: int) -> str:
    return f"{1970 + m // 12:04d}-{m % 12 + 1:02d}"


def _month_file(d: str, m: int) -> str:
    return os.path.join(d, f"{1970 + m // 12:04d}", f"{m % 12 + 1:02d}.parquet")


def _scan_months(d: str) -> list[int]:
    """枚举磁盘上实际存在的月份分区（升序）。读取以磁盘为准，不信 meta。"""
    out: list[int] = []
    if not os.path.isdir(d):
        return out
    for name in os.listdir(d):
        if len(name) != 4 or not name.isdigit():
            continue
        year_dir = os.path.join(d, name)
        if not os.path.isdir(year_dir):
            continue
        for fn in os.listdir(year_dir):
            root, ext = os.path.splitext(fn)
            if ext == ".parquet" and len(root) == 2 and root.isdigit():
                out.append((int(name) - 1970) * 12 + int(root) - 1)
    return sorted(out)


def _read_month(d: str, m: int) -> pd.DataFrame | None:
    p = _month_file(d, m)
    if not os.path.exists(p):
        return None
    return pd.read_parquet(p)


def _write_month_atomic(d: str, m: int, df: pd.DataFrame) -> None:
    """单个月份分区原子写（临时文件 + fsync + os.replace 带重试）。"""
    if df is None or len(df) == 0:
        raise ValueError("拒绝写入空 tick 月分区（先查抓取结果再落盘）")
    out = _normalize(df)
    year_dir = os.path.join(d, f"{1970 + m // 12:04d}")
    os.makedirs(year_dir, exist_ok=True)
    final = os.path.join(year_dir, f"{m % 12 + 1:02d}.parquet")
    tmp = final + ".tmp"
    out.to_parquet(tmp, index=False)
    fsync_file(tmp)
    atomic_replace(tmp, final)


def _month_file_stats(path: str) -> tuple[int, int, int]:
    """月分区的 (rows, min time_msc, max time_msc)——只读 parquet 元数据。

    统计缺失时回退读该列（to_parquet 默认写列统计，回退只是防御）。
    """
    md = pq.ParquetFile(path).metadata
    idx = md.schema.names.index("time_msc")
    lo = hi = None
    for rg in range(md.num_row_groups):
        st = md.row_group(rg).column(idx).statistics
        if st is None or st.min is None or st.max is None:
            t = pd.read_parquet(path, columns=["time_msc"])["time_msc"]
            return int(md.num_rows), int(t.min()), int(t.max())
        lo = int(st.min) if lo is None else min(lo, int(st.min))
        hi = int(st.max) if hi is None else max(hi, int(st.max))
    if md.num_row_groups == 0 or lo is None:
        raise DataIntegrityError(f"{path} 无数据行（元数据 rows=0）")
    return int(md.num_rows), lo, hi


def _scan_stats(d: str, months: list[int]) -> tuple[int, int, int]:
    """全部月份分区的 (总行数, 首 time_msc, 末 time_msc)——只碰元数据。"""
    rows, first, last = 0, None, None
    for m in months:
        r, lo, hi = _month_file_stats(_month_file(d, m))
        rows += r
        first = lo if first is None else min(first, lo)
        last = hi if last is None else max(last, hi)
    if first is None:
        raise DataIntegrityError(f"{d} 下无有效月份分区可统计水位")
    return rows, first, last


def _inherit_source(d: str, server, login):
    """meta 的 server/login 未提供时继承旧值（别用 null 覆盖数据来源）。"""
    old_meta_path = os.path.join(d, "meta.json")
    if (server is None or login is None) and os.path.exists(old_meta_path):
        try:
            with open(old_meta_path, encoding="utf-8") as _f:
                old = json.load(_f)
            server = old.get("server") if server is None else server
            login = old.get("login") if login is None else login
        except (OSError, ValueError):
            pass
    return server, login


def _write_meta_atomic(d: str, meta: dict) -> None:
    meta_tmp = os.path.join(d, "meta.json.tmp")
    with open(meta_tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    atomic_replace(meta_tmp, os.path.join(d, "meta.json"))


def _recompute_meta_locked(d: str, symbol: str, *, server, login) -> dict:
    """从磁盘月分区重算并原子写水位（先数据后水位；调用方须持品种锁）。"""
    months = _scan_months(d)
    rows, first, last = _scan_stats(d, months)
    meta = {
        "symbol": symbol.upper(),
        "rows": rows,
        "first_msc": first,
        "last_msc": last,
        "months": [_month_str(m) for m in months],
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "server": server,
        "login": login,
    }
    _write_meta_atomic(d, meta)
    return meta


def _raise_if_old_layout(d: str, symbol: str) -> None:
    p = os.path.join(d, _OLD_LAYOUT_FILENAME)
    if os.path.exists(p):
        raise TicksLayoutError(
            f"{symbol} 检测到旧单文件布局（{p}）：按月分区读写前先运行 "
            f"scripts/migrate_ticks_layout.py --symbols {symbol} 完成迁移"
        )


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    """统一列序与 dtype（真包 flags 为 int32 等，落盘一律按本库规范）。"""
    out = df.copy()
    for c in TICK_COLUMNS:
        out[c] = out[c].astype(_TICK_DTYPES[c])
    return out[TICK_COLUMNS]


def merge_ticks(old: pd.DataFrame | None, new: pd.DataFrame) -> pd.DataFrame:
    """合并去重：整行去重（同毫秒的多条 tick 是正常数据，绝不按 time_msc 去重）。

    排序按 time_msc 稳定排序（同毫秒内保持到达顺序），列序化返回。
    """
    if new is None or len(new) == 0:
        return old if old is not None else _normalize(new)
    if old is None or len(old) == 0:
        merged = new
    else:
        merged = pd.concat([old, new], ignore_index=True)
    merged = (
        merged.drop_duplicates(keep="last")
        .sort_values("time_msc", kind="stable")
        .reset_index(drop=True)
    )
    return merged[TICK_COLUMNS]


def _fetch_window(conn, mt5_module, symbol: str, from_s: int, to_s: int) -> pd.DataFrame:
    """取一个时间窗（epoch 秒，含两端）的 tick。

    None → 按 last_error 上抛；空窗 → 空表；返回前做块内不倒退校验。
    真包返回 numpy 结构化数组——直接 pd.DataFrame(arr) 转换（不 tolist），
    多余字段（如 volume_real）由列序化裁掉。
    """
    rows = conn.call(
        mt5_module.copy_ticks_range, symbol, from_s, to_s, mt5_module.COPY_TICKS_ALL
    )
    if rows is None:
        raise DataUnavailableError(
            f"{symbol} tick 窗口 [{from_s}, {to_s}] 不可用：{conn.last_error()}"
        )
    if len(rows) == 0:
        return pd.DataFrame(columns=TICK_COLUMNS)
    if isinstance(rows, np.ndarray) and rows.dtype.names is not None:
        df = pd.DataFrame(rows)  # 结构化数组直转
    else:  # 防御：列表形态（真包/假件均走上面的快路径）
        df = pd.DataFrame(list(rows), columns=TICK_COLUMNS)
    t = df["time_msc"].to_numpy(dtype="int64")
    bad = np.flatnonzero(np.diff(t) < 0)  # 相邻相等=同毫秒多条，合法；倒退才损坏
    if bad.size:
        i = int(bad[0])
        raise DataIntegrityError(
            f"{symbol} tick 窗口 [{from_s}, {to_s}] 第 {i} 行起 time_msc 倒退"
            f"（{t[i]} → {t[i + 1]}），历史损坏，拒绝写入"
        )
    return _normalize(df)


def _merge_and_write_months(d: str, symbol: str, fetched: pd.DataFrame,
                            *, meta_info: dict | None = None) -> int:
    """锁内：fetched 按月分组，逐月与旧月文件整行去重合并后原子写，
    重算水位，返回新增行数（写盘前后磁盘重算的行数差）。
    """
    months_before = _scan_months(d)
    rows_before = _scan_stats(d, months_before)[0] if months_before else 0
    midx = _month_index_array(fetched["time_msc"].to_numpy())
    for m in np.unique(midx):
        part = fetched[midx == m]
        old_part = _read_month(d, int(m))
        merged = merge_ticks(old_part, part)
        _write_month_atomic(d, int(m), merged)
    info = meta_info or {}
    server, login = _inherit_source(d, info.get("server"), info.get("login"))
    meta = _recompute_meta_locked(d, symbol, server=server, login=login)
    return int(meta["rows"]) - rows_before


def backfill_ticks(conn, mt5_module, root, symbol: str, *, start=None, end=None,
                   chunk_days: float = 7.0, on_progress=None,
                   meta_info: dict | None = None) -> int:
    """分块回补 tick 历史，返回本次新增行数（合并后 − 旧库）。

    从 end（缺省 = 墙钟 UTC）往回按 chunk_days 取窗，直到 start；连续
    _EMPTY_WINDOW_LIMIT 个空窗视为历史尽头正常收尾（服务器 tick 史比请求
    的 start 更短不是错误）。窗口结果先收集进列表、最后一次 concat，再
    按月分组逐月合并落盘——只读写涉及月份的分区文件，不整库读改写。
    on_progress(win_start, win_end, window_rows) 每窗回调一次。start 必填
    （增量刷新用 refresh_ticks）。
    """
    if start is None:
        raise ValueError(
            "backfill_ticks 需要 start（贴 UTC 标签的 naive datetime）；"
            "增量刷新用 refresh_ticks"
        )
    if start.tzinfo is not None or (end is not None and end.tzinfo is not None):
        raise ValueError("start/end 只接受贴 UTC 标签的 naive datetime")
    if end is None:
        end = _utcnow()
    if end <= start:
        raise ValueError(f"end {end} 必须晚于 start {start}")

    d = ticks_dir(root, symbol)
    _raise_if_old_layout(d, symbol)
    months_before = _scan_months(d)
    old_rows = _scan_stats(d, months_before)[0] if months_before else 0

    step = timedelta(days=float(chunk_days))
    pages: list[pd.DataFrame] = []
    empty_run = 0
    win_end = end
    while win_end > start:
        win_start = max(win_end - step, start)
        page = _fetch_window(conn, mt5_module, symbol,
                             _epoch_s(win_start), _epoch_s(win_end))
        if len(page) == 0:
            empty_run += 1
            if empty_run >= _EMPTY_WINDOW_LIMIT:
                logger.info("%s tick 连续 %d 个空窗，视为历史尽头，正常收尾",
                            symbol, empty_run)
                break
        else:
            empty_run = 0
            pages.append(page)
        if on_progress is not None:
            on_progress(win_start, win_end, len(page))
        win_end = win_start

    if not pages:
        if old_rows > 0:
            return 0  # 一窗数据都没抓到但旧库健在：无事发生
        raise DataUnavailableError(
            f"{symbol} tick 回补未取到任何数据（终端未同步该品种/无 tick 历史？）"
        )
    fetched = pages[0] if len(pages) == 1 else pd.concat(pages, ignore_index=True)
    fetched = (fetched.drop_duplicates(keep="last")
               .sort_values("time_msc", kind="stable")
               .reset_index(drop=True))
    os.makedirs(d, exist_ok=True)
    with series_lock(d):
        return _merge_and_write_months(d, symbol, fetched, meta_info=meta_info)


def refresh_ticks(conn, mt5_module, root, symbol: str, *, now_server,
                  meta_info: dict | None = None) -> int:
    """增量刷新：从旧末条 last_msc（含）取到 now_server，返回新增行数。

    只读写 last_msc 所在月份及之后的分区文件（更早月份一字节不动）。
    接续证明（对齐 bars 的 refresh 语义）：抓回页必须含 time_msc == last_msc
    的记录，否则抛 DataIntegrityError——接续点之后的缺口属于市场自身序列，
    不做算术推断。空抓回且 now − last_msc < 7 天 → 0（周末/无行情）。
    """
    if getattr(now_server, "tzinfo", None) is not None:
        raise ValueError("now_server 只接受贴 UTC 标签的 naive datetime")
    d = ticks_dir(root, symbol)
    _raise_if_old_layout(d, symbol)
    meta = read_ticks_watermark(root, symbol)
    months = _scan_months(d)
    if meta is None or not months:
        raise DataUnavailableError(
            f"{symbol} 无历史 tick（{d} 缺按月分区 parquet/meta.json），"
            "先跑回补（--mode backfill）"
        )
    # 水位以磁盘月分区重算为准（防上次异常中断留下陈旧 meta）
    _, _, last_msc = _scan_stats(d, months)
    m_last = _month_index_from_msc(last_msc)
    if _read_month(d, m_last) is None:
        raise DataIntegrityError(
            f"{symbol} 末月分区文件缺失（{_month_file(d, m_last)}），与水位 "
            f"last_msc={last_msc} 不一致——库损坏，请重跑回补/迁移"
        )
    last_dt = _EPOCH + timedelta(milliseconds=last_msc)
    from_s = last_msc // 1000  # 含边界（秒粒度向下取整，宁可多抓几条）
    to_s = _epoch_s(now_server) + _TO_MARGIN_S

    page = _fetch_window(conn, mt5_module, symbol, from_s, to_s)
    if len(page) == 0:
        quiet = now_server - last_dt
        if quiet < timedelta(days=_QUIET_DAYS):
            return 0  # 周末/无行情的正常静默
        raise DataIntegrityError(
            f"{symbol} tick 自 {last_dt} 起静默 {quiet} 且抓回为空，无法证明接续"
            "（停更过久或服务端缺段）——请重跑回补（backfill）。"
        )
    if not (page["time_msc"] == last_msc).any():
        raise DataIntegrityError(
            f"{symbol} tick 抓回页不含旧末条 time_msc={last_msc}，无法证明接续"
            "（停更过久或服务端缺段）——请重跑回补（backfill）。"
        )
    os.makedirs(d, exist_ok=True)
    with series_lock(d):
        return _merge_and_write_months(d, symbol, page, meta_info=meta_info)


def save_ticks(root, symbol: str, df: pd.DataFrame,
               *, server: str | None = None, login: int | None = None) -> None:
    """整表覆写：df 按月分区原子落盘，df 覆盖到的月份被替换、未覆盖到的
    既有月份分区一并删除，并重算 meta（旧单文件版即整表覆写语义）。

    生产路径用 backfill/refresh（增量合并、只动涉及月份）；本函数用于
    建库种子与工具场景。
    """
    d = ticks_dir(root, symbol)
    _raise_if_old_layout(d, symbol)
    os.makedirs(d, exist_ok=True)
    with series_lock(d):
        _save_ticks_locked(d, symbol, df, server=server, login=login)


def _save_ticks_locked(d, symbol, df, *, server=None, login=None):
    if df is None or len(df) == 0:
        raise ValueError("拒绝写入空 tick 表（先查抓取结果再落盘）")
    server, login = _inherit_source(d, server, login)
    out = _normalize(df)
    midx = _month_index_array(out["time_msc"].to_numpy())
    keep = {int(m) for m in np.unique(midx)}
    for m in sorted(keep):
        _write_month_atomic(d, m, out[midx == m])
    for m in _scan_months(d):  # 整表覆写：清掉 df 未覆盖的旧月份分区
        if m not in keep:
            try:
                os.remove(_month_file(d, m))
                os.rmdir(os.path.dirname(_month_file(d, m)))  # 空年份目录
            except OSError:
                pass
    _recompute_meta_locked(d, symbol, server=server, login=login)


def load_ticks(root, symbol: str, *, start_msc: int | None = None,
               end_msc: int | None = None) -> pd.DataFrame | None:
    """读取 tick 历史，只打开 [start_msc, end_msc] 涉及的月份分区文件。

    - start/end 均缺省 → 全部月份；
    - 行级过滤 time_msc ∈ [start_msc, end_msc]（含两端，毫秒）；
    - 库不存在 → None；库存在但区间不涉及任何月份 → 空 DataFrame（列齐）；
    - 检测到旧单文件布局 → TicksLayoutError（提示先跑迁移脚本）。
    """
    d = ticks_dir(root, symbol)
    _raise_if_old_layout(d, symbol)
    months = _scan_months(d)
    if not months:
        return None
    if start_msc is not None and end_msc is not None \
            and int(start_msc) > int(end_msc):
        raise ValueError(f"start_msc {start_msc} 不得晚于 end_msc {end_msc}")
    lo = (_month_index_from_msc(int(start_msc))
          if start_msc is not None else months[0])
    hi = (_month_index_from_msc(int(end_msc))
          if end_msc is not None else months[-1])
    frames = []
    for m in months:
        if lo <= m <= hi:
            part = _read_month(d, m)
            if part is not None and len(part) > 0:
                frames.append(part)
    if not frames:
        return pd.DataFrame(columns=TICK_COLUMNS)
    df = frames[0] if len(frames) == 1 else pd.concat(frames, ignore_index=True)
    if start_msc is not None:
        df = df[df["time_msc"] >= int(start_msc)]
    if end_msc is not None:
        df = df[df["time_msc"] <= int(end_msc)]
    return df.reset_index(drop=True)


def read_ticks_watermark(root, symbol: str) -> dict | None:
    p = os.path.join(ticks_dir(root, symbol), "meta.json")
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def migrate_old_layout(root, symbol: str) -> dict | None:
    """旧单文件 data.parquet → 按月分区迁移（T4/R3，一次性）；返回摘要。

    无旧布局可迁时返回 None。步骤（全程持品种写锁）：
    读旧文件 → 按月写分区 → 守恒校验（总行数与首末 time_msc 逐项相等，
    失败则旧文件不动、可直接重跑覆盖半成品）→ 旧文件改名
    data.parquet.migrated-<UTC戳>（保留原数据，不删除）→ 重算 meta.json
    （server/login 从旧 meta 继承）。
    """
    d = ticks_dir(root, symbol)
    old_path = os.path.join(d, _OLD_LAYOUT_FILENAME)
    if not os.path.exists(old_path):
        return None
    os.makedirs(d, exist_ok=True)
    with series_lock(d):
        old = pd.read_parquet(old_path)
        if old is None or len(old) == 0:
            raise DataIntegrityError(f"{symbol} 旧 {old_path} 为空，无可迁移数据")
        out = (_normalize(old)
               .sort_values("time_msc", kind="stable")
               .reset_index(drop=True))
        midx = _month_index_array(out["time_msc"].to_numpy())
        for m in np.unique(midx):
            _write_month_atomic(d, int(m), out[midx == m])
        rows2, first2, last2 = _scan_stats(d, _scan_months(d))
        want = (len(out), int(out["time_msc"].min()), int(out["time_msc"].max()))
        if (rows2, first2, last2) != want:
            raise DataIntegrityError(
                f"{symbol} 迁移守恒校验失败：行数 {rows2}≠{want[0]} 或首末 "
                f"time_msc ({first2}, {last2})≠({want[1]}, {want[2]})；"
                "旧文件未动，可重跑迁移"
            )
        backup = (_OLD_LAYOUT_FILENAME + ".migrated-"
                  + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
        os.replace(old_path, os.path.join(d, backup))
        server, login = _inherit_source(d, None, None)
        meta = _recompute_meta_locked(d, symbol, server=server, login=login)
    return {"symbol": symbol, "rows": meta["rows"],
            "first_msc": meta["first_msc"], "last_msc": meta["last_msc"],
            "months": meta["months"], "backup": backup,
            "server": server, "login": login}
