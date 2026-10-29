"""K 线存储：Parquet 文件 + 水位。

布局：{root}/bars/{SYMBOL}/{TF}/data.parquet + meta.json（水位）。
约定（与 mt5gw 一致）：time 为服务器时间贴 UTC 标签的 naive datetime。
写入一律原子（唯一临时文件 + os.replace 带重试），读者不会看到半截文件。

H-11 写入纪律：
- "读最新表→合并→校验→发布"的读改写复合操作走 update_bars（全段在
  序列写锁内，抓取留在锁外）——两个并发刷新不可能拿旧快照互覆；
- meta 重建只更新水位字段，审计字段（known_gaps）原样保留；
- 临时文件唯一命名（pid+uuid），两个写者不共用同一 tmp；
- 序列写锁为内核字节锁（LockFile/flock）：互斥由内核仲裁、崩溃自动
  释放、无按 mtime 接管（接管即双所有者竞态）；
- load_bars_checked 核对 parquet 与水位：发布中途失败过的撕裂快照
  明确拒绝（StoreConsistencyError），重跑一次保存即自愈。
"""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone

import pandas as pd

# 全部 21 个标准周期；MN1 按 31 天保守判定"月线收盘"
TIMEFRAME_SECONDS = {
    "M1": 60, "M2": 120, "M3": 180, "M4": 240, "M5": 300, "M6": 360,
    "M10": 600, "M12": 720, "M15": 900, "M20": 1200, "M30": 1800,
    "H1": 3600, "H2": 7200, "H3": 10800, "H4": 14400, "H6": 21600,
    "H8": 28800, "H12": 43200,
    "D1": 86400, "W1": 7 * 86400, "MN1": 31 * 86400,
}

_COLUMNS = ["time", "open", "high", "low", "close",
            "tick_volume", "spread", "real_volume"]


def series_dir(root, symbol: str, timeframe: str) -> str:
    # 品种名保留原大小写（XAUUSDm 等带后缀品种大小写敏感）；周期用规范大写
    return os.path.join(root, "bars", symbol, timeframe.upper())


def merge_bars(old: pd.DataFrame | None, new: pd.DataFrame) -> pd.DataFrame:
    """按 time 去重（保留 new 中的较新行），升序返回。"""
    if new is None or len(new) == 0:
        return old if old is not None else new
    if old is None or len(old) == 0:
        merged = new
    else:
        merged = pd.concat([old, new], ignore_index=True)
    merged = (
        merged.drop_duplicates(subset="time", keep="last")
        .sort_values("time")
        .reset_index(drop=True)
    )
    return merged[_COLUMNS]


def drop_unclosed(df: pd.DataFrame, timeframe: str, now_server: datetime) -> pd.DataFrame:
    """剔除未收盘的 K 线。

    规则：非末行的 bar 必已收盘（其后已有新 bar）；末行收盘当且仅当
    time+周期 <= now_server。now_server = 墙钟 UTC + 自校准券商时差
    （downloader.server_now，超限回退 offset=0 属墙钟近似而非完全解耦，
    方向见 README 时区约定）；无任何宽容秒数。
    """
    if df is None or len(df) == 0:
        return df
    tf = TIMEFRAME_SECONDS[timeframe.upper()]
    head = df.iloc[:-1]
    last = df.iloc[[-1]]
    last_closed = last["time"] + pd.to_timedelta(tf, unit="s") <= pd.Timestamp(now_server)
    return pd.concat([head, last[last_closed]]).reset_index(drop=True)


def _atomic_replace(tmp_path: str, final_path: str) -> None:
    for attempt in range(10):
        try:
            os.replace(tmp_path, final_path)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.3)  # Windows：读者占着句柄时稍候重试（总预算约 3 秒）


def _fsync_file(path: str) -> None:
    fd = os.open(path, os.O_RDWR)  # Windows：fsync 需写句柄
    try:
        os.fsync(fd)  # 落盘后再替换，掉电不至半截
    finally:
        os.close(fd)


class _SeriesLock:
    """同序列的跨进程写锁（H-11 起内核字节锁；结论同 mt5live.fileio N-I2）。

    旧实现（O_EXCL 建锁文件 + 按 mtime 超龄接管）有双所有者竞态：接管者
    A 读到旧 mtime 后暂停，B 删旧锁取新锁，A 恢复后删掉的已是 B 的新锁
    ——两个写者同时进入"读最新表→合并→发布"临界区，恰好制造 H-11 要
    防的丢更新。现改为锁住锁文件的 1 个字节（Windows LockFile /
    POSIX flock）：互斥由内核仲裁（同进程不同 fd 也冲突，线程间同样
    互斥），持锁进程崩溃内核自动释放（残锁不复存在，接管路径整体
    删除），释放只解自己 fd 上的锁、不存在删别人锁的可能。锁文件常驻
    磁盘（"释放即删"本身就是竞态源之一），内容为 pid+token+时间戳供
    排查。**不可重入**：持锁期间再进同一路径的锁会等待到超时。

    stale_after 兼容保留但被忽略：内核锁没有"超龄"语义，也不允许对
    活锁持有者接管。
    """

    def __init__(self, directory: str, stale_after: float = 600.0):
        self._path = os.path.join(directory, ".lock")
        self._stale = stale_after  # 兼容保留；内核锁不按 mtime 接管
        self._fd: int | None = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        fd = None
        for _ in range(50):
            fd = os.open(self._path,
                         os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0))
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                _kernel_lock(fd)  # 内核仲裁：他人持有 → OSError → 重试
                break
            except OSError:
                os.close(fd)
                fd = None
                time.sleep(0.2)
        if fd is None:
            raise TimeoutError(
                f"等待序列写锁超时：{self._path}（另一写者 50×0.2s 内未"
                "释放——内核锁不做超龄接管，fail-closed）")
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()} {uuid.uuid4().hex} "
                         f"{time.time():.3f}".encode())
        except OSError:
            pass  # 持有者排查信息写不进不影响互斥语义
        self._fd = fd
        return self

    def __exit__(self, *exc):
        if self._fd is None:
            return
        try:
            os.lseek(self._fd, 0, os.SEEK_SET)
            _kernel_unlock(self._fd)
        finally:
            os.close(self._fd)
            self._fd = None


# 内核字节锁原语（与 mt5live.fileio 同一套结论；mt5data 不能反向依赖
# mt5live，就地实现）：Windows msvcrt.locking / POSIX flock
try:
    import msvcrt as _msvcrt
except ImportError:            # pragma: no cover - 非 Windows 回退
    _msvcrt = None
    import fcntl as _fcntl


def _kernel_lock(fd: int) -> None:
    if _msvcrt is not None:
        _msvcrt.locking(fd, _msvcrt.LK_NBLCK, 1)
    else:                      # pragma: no cover - 非 Windows 回退
        _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)


def _kernel_unlock(fd: int) -> None:
    if _msvcrt is not None:
        _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)
    else:                      # pragma: no cover - 非 Windows 回退
        _fcntl.flock(fd, _fcntl.LOCK_UN)


def unique_tmp_path(final_path: str) -> str:
    """唯一临时文件名（H-11）：并发的两个写者各用各的 tmp。

    固定 path+".tmp" 时，写者 B 的 open(..., "w") 会截断写者 A 尚未
    replace 的半成品，或 B 先 replace 后 A 的 tmp 已消失（FileNotFoundError
    上抛）。pid+uuid 唯一命名后互不相干（模式同 mt5gw.snapshot /
    mt5live.fileio），仍以 .tmp 结尾便于"成功路径无残留"的巡检。
    """
    return f"{final_path}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"


def save_bars(root, symbol: str, timeframe: str, df: pd.DataFrame,
              *, server: str | None = None, login: int | None = None) -> None:
    """原子写入 data.parquet 与 meta.json（先数据后水位，跨进程序列锁）。"""
    d = series_dir(root, symbol, timeframe)
    os.makedirs(d, exist_ok=True)
    with _SeriesLock(d):
        _save_bars_locked(d, symbol, timeframe, df, server=server, login=login)


def update_bars(root, symbol: str, timeframe: str, fetched: pd.DataFrame, *,
                merge, server: str | None = None,
                login: int | None = None) -> pd.DataFrame:
    """H-11：读改写全段在同一序列写锁内——读最新表 → merge(latest) → 发布。

    供 bars 刷新/回填的"读取最新表→合并新数据→校验→发布"复合操作使用：
    抓取在锁外完成后把结果交给本函数，锁内**重读最新** data.parquet
    （可能已被并发写者推进），调 merge(latest, fetched) 完成校验与合并
    （merge 抛错即不发布、旧文件原样），再原子发布。两个并发写者因此
    不可能各自拿抓取前的旧快照覆盖对方（丢更新）。返回最终发布的
    DataFrame。merge 签名：merge(latest, fetched) -> df。
    """
    d = series_dir(root, symbol, timeframe)
    os.makedirs(d, exist_ok=True)
    with _SeriesLock(d):
        latest = load_bars(root, symbol, timeframe)
        target = merge(latest, fetched)
        _save_bars_locked(d, symbol, timeframe, target,
                          server=server, login=login)
        return target


def _save_bars_locked(d, symbol, timeframe, df, *, server=None, login=None):
    # 旧 meta 一并读出：server/login 未提供时继承旧值（别用 null 覆盖掉
    # 数据来源）；known_gaps 是审计登记（quality.write_known_gaps 写入），
    # 不是水位——重建 meta 时必须原样保留（H-11：刷新不得清掉已登记缺口）
    old_meta: dict = {}
    old_meta_path = os.path.join(d, "meta.json")
    if os.path.exists(old_meta_path):
        try:
            with open(old_meta_path, encoding="utf-8") as _f:
                old_meta = json.load(_f)
        except (OSError, ValueError):
            old_meta = {}
    if server is None:
        server = old_meta.get("server")
    if login is None:
        login = old_meta.get("login")
    out = df.copy()
    out["time"] = pd.to_datetime(out["time"]).astype("datetime64[ns]")
    for c in ("open", "high", "low", "close"):
        out[c] = out[c].astype("float64")
    for c in ("tick_volume", "spread", "real_volume"):
        out[c] = out[c].astype("int64")
    out = out[_COLUMNS]

    parquet_tmp = unique_tmp_path(os.path.join(d, "data.parquet"))
    out.to_parquet(parquet_tmp, index=False)
    _fsync_file(parquet_tmp)
    _atomic_replace(parquet_tmp, os.path.join(d, "data.parquet"))

    wm = {
        "symbol": symbol.upper(),
        "timeframe": timeframe.upper(),
        "rows": int(len(out)),
        "first_time": out["time"].iloc[0].isoformat(),
        "last_closed_time": out["time"].iloc[-1].isoformat(),
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "server": server,
        "login": login,
    }
    if isinstance(old_meta.get("known_gaps"), list) and old_meta["known_gaps"]:
        wm["known_gaps"] = old_meta["known_gaps"]  # 审计字段：逐字保留
    meta_tmp = unique_tmp_path(os.path.join(d, "meta.json"))
    with open(meta_tmp, "w", encoding="utf-8") as f:
        json.dump(wm, f, ensure_ascii=False, indent=2)
    _atomic_replace(meta_tmp, os.path.join(d, "meta.json"))


def load_bars(root, symbol: str, timeframe: str) -> pd.DataFrame | None:
    p = os.path.join(series_dir(root, symbol, timeframe), "data.parquet")
    if not os.path.exists(p):
        return None
    return pd.read_parquet(p)


class StoreConsistencyError(RuntimeError):
    """data.parquet 与 meta.json 不是同一份快照（发布中途失败等）——拒绝配读。"""


def load_bars_checked(root, symbol: str,
                      timeframe: str) -> pd.DataFrame | None:
    """读 data.parquet 并核对 meta 水位（H-11：读者要么一致版本要么明确拒绝）。

    - 无 data.parquet → None（与 load_bars 一致）；
    - 有 data.parquet 无 meta.json、meta 坏、或 rows/first/last 与数据对
      不上 → StoreConsistencyError：data 与 meta 分两次原子发布，中途
      失败过的序列半新半旧，宁可明确拒绝也不给读者自相矛盾的版本
      （后续任一次正常保存即自愈）。
    """
    d = series_dir(root, symbol, timeframe)
    df = load_bars(root, symbol, timeframe)
    if df is None:
        return None
    wm = read_watermark(root, symbol, timeframe)
    if wm is None:
        raise StoreConsistencyError(
            f"{d}：有 data.parquet 无 meta.json——数据与水位不是同一份，"
            "拒绝读取（重跑一次保存可自愈）")
    try:
        declared = (int(wm["rows"]), pd.Timestamp(wm["first_time"]),
                    pd.Timestamp(wm["last_closed_time"]))
    except (KeyError, TypeError, ValueError) as e:
        raise StoreConsistencyError(
            f"{d}：meta 水位字段缺失或不可解析（{e!r}）——拒绝读取") from e
    actual = (len(df), pd.Timestamp(df["time"].iloc[0]),
              pd.Timestamp(df["time"].iloc[-1])) if len(df) else (0, None, None)
    if declared != actual:
        raise StoreConsistencyError(
            f"{d}：data.parquet 与 meta.json 不是同一份快照"
            f"（数据 {actual[0]} 根 [{actual[1]} → {actual[2]}]，"
            f"meta 声明 {declared[0]} 根 [{declared[1]} → {declared[2]}]）"
            "——发布中途失败过？拒绝读取（重跑一次保存可自愈）")
    return df


def read_watermark(root, symbol: str, timeframe: str) -> dict | None:
    p = os.path.join(series_dir(root, symbol, timeframe), "meta.json")
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


series_lock = _SeriesLock
atomic_replace = _atomic_replace
fsync_file = _fsync_file
