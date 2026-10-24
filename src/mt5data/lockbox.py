"""锁箱：把最近一段历史隔离出研究可读范围，防未来数据泄进回测。

语义（钉死，禁止绕过）：
- 配置存 {root}/lockbox.json，两种版本（load_config 同时兼容）：
  - v1（旧）：键为 "SYMBOL/TF"（统一大写），值为 naive-UTC ISO 时间；
    不同序列可有不同 cutoff（历史跨度不同）；
  - v2（现行，R2 全局统一）：{"version": 2, "global_cutoff": ..., "created_utc": ...,
    "note": ...}——全部品种、全部周期共用同一个 cutoff（取各序列最保守者，
    见 compute_global_cutoff），一经写入只能更早不能更晚（write_global_cutoff
    强制，旧值保留进 history）；
- cutoff = 研究/回测可见上限：开放段 time < cutoff，锁箱段 time >= cutoff；
- 显式请求（end / since / until）越过 cutoff → LockboxError，绝不静默
  截断——静默截断会让研究者误以为看到了完整请求区间；
- 无配置 → 一切行为与没有锁箱时完全一致（回归兼容）；
- tick 库豁免：tick 只有最近数周（整段落在锁箱期内），仅用于成本统计
  （点差/滑点的按小时分布），不构成回测样本、不参与信号计算；对 tick
  施加锁箱会让成本校准无数据可用，而报价微观结构统计不泄露未来行情
  路径，故 tick 读取不受本模块约束（成本侧见 mt5quant.costs）。
"""
from __future__ import annotations

import calendar
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd

from mt5data.store import load_bars

CONFIG_NAME = "lockbox.json"

# v2 配置里允许出现的键；其余一律视为混入的序列键（语义不明，fail-fast）
_V2_KEYS = {"version", "global_cutoff", "created_utc", "note", "history"}


class LockboxError(RuntimeError):
    """显式请求越过锁箱边界，或试图把 cutoff 前移（防前视泄漏的关键防线）。"""


@dataclass(frozen=True)
class LockboxConfig:
    """锁箱配置视图（v1/v2 统一读取结果）。

    - v2：global_cutoff 对**所有**序列生效（含库里尚无数据的序列）；
    - v1：cutoffs 按 "SYMBOL/TF" 键逐序列生效，未配置的序列不受约束。
    实现 .get / __bool__ 与 dict 同形，既有调用方（如 mt5mcp.tools 的
    dict 式取值）无需修改即可兼容两种版本。
    """

    global_cutoff: datetime | None = None
    cutoffs: dict[str, datetime] = field(default_factory=dict)
    created_utc: datetime | None = None
    note: str = ""
    history: tuple = ()

    def get(self, key: str, default=None):
        """dict 式取值：v2 全局值优先（全局生效时序列键无意义）。"""
        if self.global_cutoff is not None:
            return self.global_cutoff
        return self.cutoffs.get(key, default)

    def __bool__(self) -> bool:
        return self.global_cutoff is not None or bool(self.cutoffs)


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


def compute_global_cutoff(inventory, now: datetime) -> datetime:
    """全局研究截止：对每个序列各算 compute_cutoff，取**最早**者，截到日 00:00。

    最早 = 锁得最多 = 最保守：任何一个序列的锁箱期都不得经由另一个
    未锁品种的行情被窥探（R2 跨品种泄露）。inventory 取
    mt5data.schedule.series_inventory 的返回值，各序列以水位里的
    first_time 起算（不重读 parquet）；空清单属调用错误（fail-fast）。
    compute_cutoff 的返回值已截到日 00:00，min 不破坏该性质。
    """
    firsts = []
    for ref in inventory:
        try:
            firsts.append(datetime.fromisoformat(ref.watermark["first_time"]))
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(
                f"序列 {getattr(ref, 'symbol', '?')}/"
                f"{getattr(ref, 'timeframe', '?')} 的水位缺可解析的 "
                f"first_time，无法计算全局 cutoff") from e
    if not firsts:
        raise ValueError("inventory 为空：库里没有序列，无法计算全局 cutoff")
    return min(compute_cutoff(f, now) for f in firsts)


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _load_raw(root) -> dict | None:
    """读原始配置 JSON；文件不存在 → None。版本/结构不对如实抛错。"""
    path = os.path.join(root, CONFIG_NAME)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    if "version" in raw:
        if raw["version"] != 2:
            raise ValueError(f"未知锁箱配置版本：{raw['version']!r}（只认 v1/v2）")
        if "global_cutoff" not in raw:
            raise ValueError("v2 锁箱配置缺 global_cutoff 字段")
        extra = set(raw) - _V2_KEYS
        if extra:
            raise ValueError(
                f"v2 锁箱配置混入了序列键 {sorted(extra)}——请用纯 v2（全局 "
                "cutoff）或纯 v1（SYMBOL/TF 键），不要混写")
        history = raw.get("history", [])
        if not isinstance(history, list):
            raise ValueError("v2 锁箱配置的 history 必须是列表")
    return raw


def write_config(root, cutoffs: dict[str, str]) -> str:
    """写 v1 序列键配置（原子替换）；键统一转大写，值须为可解析 ISO 时间。

    既有配置是 v2 时拒绝：用序列键覆盖全局锁箱等于变相绕过
    "只能更早不能更晚"（收紧请用 write_global_cutoff）。
    """
    if (existing := _load_raw(root)) is not None and "version" in existing:
        raise LockboxError(
            "已有 v2 全局锁箱配置，禁止用 v1 序列键覆盖——"
            "锁箱只能更早不能更晚，请改用 write_global_cutoff")
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


def _reject_later(old: datetime, new: datetime) -> None:
    if new > old:
        raise LockboxError(
            f"锁箱只能更早不能更晚：现有 cutoff {old.isoformat()}，"
            f"请求写入 {new.isoformat()}——前移会让已锁定的研究边界外泄")


def write_global_cutoff(root, cutoff: datetime | str, *, note: str) -> str:
    """写 v2 全局锁箱（原子替换）；锁箱只能更早不能更晚。

    - 无既有配置 → 直接写 v2；
    - 既有 v2：新 cutoff 晚于旧值 → LockboxError；早于 → 允许且旧值
      （global_cutoff/created_utc/note）进 history；等于 → 允许重写
      （更新 note/created），值未变不产生 history 记录；
    - 既有 v1：等效旧值取各序列 cutoff 的**最早**者，同一规则只许更早；
      成功转换后 v1 各键整体保留进 history。
    返回配置文件路径。
    """
    if isinstance(cutoff, str):
        try:
            cutoff = datetime.fromisoformat(cutoff)
        except ValueError as e:
            raise ValueError(f"全局 cutoff 不可解析：{cutoff!r}") from e
    path = os.path.join(root, CONFIG_NAME)
    os.makedirs(root, exist_ok=True)
    now_utc = _utcnow_naive()
    history: list[dict] = []
    if (raw := _load_raw(root)) is not None:
        if "version" in raw:  # 既有 v2
            old = datetime.fromisoformat(raw["global_cutoff"])
            _reject_later(old, cutoff)
            history = list(raw.get("history", []))
            if cutoff < old:
                history.append({
                    "global_cutoff": old.isoformat(),
                    "created_utc": raw.get("created_utc"),
                    "note": raw.get("note", ""),
                    "superseded_utc": now_utc.isoformat(),
                })
        else:  # 既有 v1：最早者是最保守的等效旧值，放宽它 = 放宽某序列
            old_values = {k: datetime.fromisoformat(v) for k, v in raw.items()}
            oldest = min(old_values.values())
            _reject_later(oldest, cutoff)
            if cutoff < oldest:
                history.append({
                    "v1_cutoffs": {k: v.isoformat() for k, v in old_values.items()},
                    "superseded_utc": now_utc.isoformat(),
                })
    payload = {
        "version": 2,
        "global_cutoff": cutoff.isoformat(),
        "created_utc": now_utc.isoformat(),
        "note": str(note),
    }
    if history:
        payload["history"] = history
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    _atomic_replace(tmp, path)
    return path


def load_config(root) -> LockboxConfig | None:
    """读锁箱配置（v1/v2 兼容）；文件不存在 → None（= 无锁箱，行为与旧一致）。

    坏文件（不可解析 / 未知版本 / v2 混序列键）如实抛错。
    """
    raw = _load_raw(root)
    if raw is None:
        return None
    if "version" in raw:  # v2：全局 cutoff，对所有序列生效
        created = raw.get("created_utc")
        return LockboxConfig(
            global_cutoff=datetime.fromisoformat(raw["global_cutoff"]),
            created_utc=datetime.fromisoformat(created) if created else None,
            note=str(raw.get("note", "")),
            history=tuple(raw.get("history", [])),
        )
    return LockboxConfig(
        cutoffs={k: datetime.fromisoformat(v) for k, v in raw.items()})


def cutoff_for(lockbox: dict[str, datetime] | LockboxConfig | None,
               symbol: str, timeframe: str) -> datetime | None:
    """从配置中取该序列的 cutoff（无配置/无该序列 → None）。

    接受 load_config 的返回值（LockboxConfig）或 v1 形态的裸 dict；
    v2 全局值对所有序列生效。
    """
    return (lockbox or {}).get(series_key(symbol, timeframe))


def research_bars(root, symbol: str, timeframe: str, *,
                  end: datetime | None = None,
                  lockbox: dict[str, datetime] | LockboxConfig | None = None
                  ) -> pd.DataFrame | None:
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
