"""锁箱：把最近一段历史隔离出研究可读范围，防未来数据泄进回测。

语义（钉死，禁止绕过）：
- 配置存 {root}/lockbox.json，键为 "SYMBOL/TF"（统一大写），值为
  naive-UTC ISO 时间；不同序列可有不同 cutoff（历史跨度不同）；
- cutoff = 研究/回测可见上限：开放段 time < cutoff，锁箱段 time >= cutoff；
- 显式请求（end / since / until）越过 cutoff → LockboxError，绝不静默
  截断——静默截断会让研究者误以为看到了完整请求区间；
- 无配置 → 一切行为与没有锁箱时完全一致（回归兼容）。
"""
from __future__ import annotations

import calendar
import json
import os
import time
from datetime import datetime

import pandas as pd

from mt5data.store import load_bars

CONFIG_NAME = "lockbox.json"


class LockboxError(RuntimeError):
    """显式请求越过锁箱边界（防前视泄漏的关键防线）。"""


def series_key(symbol: str, timeframe: str) -> str:
    """配置键：SYMBOL/TF 统一大写（品种目录大小写敏感，配置键不敏感）。"""
    return f"{symbol.upper()}/{timeframe.upper()}"


def _atomic_replace(tmp_path: str, final_path: str) -> None:
    # 与 mt5data.store 同款原子替换（Windows 读者占句柄时稍候重试）
    for attempt in range(10):
        try:
            os.replace(tmp_path, final_path)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.3)


def _shift_years(dt: datetime, years: int) -> datetime:
    try:
        return dt.replace(year=dt.year + years)
    except ValueError:  # 2/29 落在平年 → 收进 2/28
        return dt.replace(year=dt.year + years, day=28)


def _minus_months(dt: datetime, months: int) -> datetime:
    total = dt.year * 12 + (dt.month - 1) - months
    year, month0 = divmod(total, 12)
    month = month0 + 1
    last_day = calendar.monthrange(year, month)[1]
    return dt.replace(year=year, month=month, day=min(dt.day, last_day))


def compute_cutoff(first_time: datetime, now: datetime) -> datetime:
    """研究截止：历史跨度 >5 年 → now−12 个月，否则 now−6 个月；截到日 00:00。

    长历史给回测留足样本（扣 12 个月），短历史少扣（6 个月）；
    恰满 5 年不算">5 年"，取 6 个月。输入输出均为 naive-UTC。
    """
    months = 12 if now > _shift_years(first_time, 5) else 6
    return _minus_months(now, months).replace(
        hour=0, minute=0, second=0, microsecond=0)


def write_config(root, cutoffs: dict[str, str]) -> str:
    """写 {root}/lockbox.json（原子替换）；键统一转大写，值须为可解析 ISO 时间。"""
    payload = {str(k).upper(): str(v) for k, v in cutoffs.items()}
    for key, value in payload.items():
        try:
            datetime.fromisoformat(value)
        except ValueError as e:
            raise ValueError(f"锁箱配置 {key} 的时间值不可解析：{value!r}") from e
    path = os.path.join(root, CONFIG_NAME)
    os.makedirs(root, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    _atomic_replace(tmp, path)
    return path


def load_config(root) -> dict[str, datetime] | None:
    """读锁箱配置；文件不存在 → None（= 无锁箱，行为与旧一致）。坏文件如实抛错。"""
    path = os.path.join(root, CONFIG_NAME)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    return {k: datetime.fromisoformat(v) for k, v in raw.items()}


def cutoff_for(lockbox: dict[str, datetime] | None,
               symbol: str, timeframe: str) -> datetime | None:
    """从配置中取该序列的 cutoff（无配置/无该序列 → None）。"""
    return (lockbox or {}).get(series_key(symbol, timeframe))


def research_bars(root, symbol: str, timeframe: str, *,
                  end: datetime | None = None,
                  lockbox: dict[str, datetime] | None = None) -> pd.DataFrame | None:
    """研究侧 K 线加载器：只返回 time < cutoff 的行（锁箱段绝不外发）。

    - 无该序列 cutoff → 与 store.load_bars 行为一致（end 仅做普通上界）；
    - 显式 end > cutoff → LockboxError：显式请求禁止静默截断；
    - end == cutoff 合法，但结果仍严格 < cutoff（cutoff 时刻的 bar 属锁箱）。
    """
    lb = load_config(root) if lockbox is None else lockbox
    cutoff = cutoff_for(lb, symbol, timeframe)
    if cutoff is not None and end is not None and end > cutoff:
        raise LockboxError(
            f"锁箱：{symbol.upper()} {timeframe.upper()} 的研究截止为 "
            f"{cutoff.isoformat()}，请求 end={end.isoformat()} 已越过锁箱边界——"
            "显式请求不做静默截断，请改用截止内的区间"
        )
    df = load_bars(root, symbol, timeframe)
    if df is None or len(df) == 0:
        return df
    if cutoff is not None:
        df = df[df["time"] < pd.Timestamp(cutoff)]
    if end is not None:
        df = df[df["time"] <= pd.Timestamp(end)]
    return df.reset_index(drop=True)


def lockbox_status(root, symbol: str, timeframe: str) -> dict:
    """序列锁箱状态：cutoff 与开放/锁定行数（开放 = time < cutoff 可研究）。"""
    cutoff = cutoff_for(load_config(root), symbol, timeframe)
    df = load_bars(root, symbol, timeframe)
    total = 0 if df is None else int(len(df))
    if cutoff is None or total == 0:
        return {"symbol": symbol.upper(), "timeframe": timeframe.upper(),
                "cutoff": cutoff, "open_rows": total, "locked_rows": 0}
    locked = df["time"] >= pd.Timestamp(cutoff)
    return {"symbol": symbol.upper(), "timeframe": timeframe.upper(),
            "cutoff": cutoff,
            "open_rows": int((~locked).sum()), "locked_rows": int(locked.sum())}
