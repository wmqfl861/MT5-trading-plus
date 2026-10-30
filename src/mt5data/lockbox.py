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
- X-03（audit2）：无任何声明边界**且有数据**的库（未初始化主库、无 meta
  的手写 parquet）对正式研究读按"未初始化"拒绝——"没有 lockbox"不再
  自动等于直读放行；显式测试/迁移通道 allow_unbounded=True 保持旧的
  直读行为；完全无数据的序列仍按 missing → None（与 load_bars 一致）；
  合法研究导出库（meta.source_cutoff）/纯 ext 库
  （manifest.research_cutoff）不属裸库，不须 opt-in；
- H-01 统一有效研究上界契约（effective_research_bound）：主库 lockbox
  cutoff、研究导出库 meta.source_cutoff、ext manifest.research_cutoff、
  显式 end、已收盘水位五类来源汇成**一个**严格上界；声明冲突/元数据
  缺损保守拒绝。research_bars 与 ext 合并读取（mt5data.ext.merge）消费
  同一契约——不存在"研究库没有 lockbox.json 就没有边界"的读法；
- X-04（audit2）：research_bars 的取数走 store.load_bars_consistent
  （内容指纹一致性读）——发布中断的撕裂快照明确拒绝，不配读混版；
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
from datetime import datetime, timedelta, timezone

import pandas as pd

from mt5data.schedule import REQUIRED_META_FIELDS
from mt5data.store import TIMEFRAME_SECONDS, load_bars, load_bars_consistent

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


# ---------------------------------------------------------------- H-01 契约

@dataclass(frozen=True)
class ResearchBound:
    """有效研究上界（H-01 统一契约的一次计算结果）。

    - declared：**声明**上界——主库 lockbox cutoff（v2 全局 / v1 序列）、
      研究导出库 bars meta 的 source_cutoff、ext manifest 的
      research_cutoff；多个声明并存且不一致在构造前即 LockboxError
      （fail-closed：分不清哪条是真边界）。None = 库内没有任何声明；
    - last_closed：bars meta 的已收盘水位（**含端点**：time <= last_closed）；
    - end：显式请求上界（含端点）；越 declared 界在构造前即 LockboxError；
    - strict：统一换算后的**严格**上界——合法研究行一律 time < strict
      （declared 原样；last_closed + 一个周期；end + 1µs——bar 时刻落在
      周期网格上，三种口径等价不漏）。全无 → None（X-03 起：declared
      为 None 且有数据 = 裸库未初始化，正式研究读在构造时即拒绝，
      仅 allow_unbounded=True 的显式测试/迁移通道能拿到 strict=None）；
    - sources：参与声明的来源标签（"lockbox" / "bars:source_cutoff" /
      "ext:{来源}"），诊断与审计用。
    """

    declared: datetime | None
    last_closed: datetime | None
    end: datetime | None
    strict: datetime | None
    sources: tuple[str, ...] = ()


def _parse_bound_iso(value, what: str) -> datetime:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as e:
        raise LockboxError(f"{what} 不可解析：{value!r}（fail-closed）") from e


def _read_bound_meta(root: str, symbol: str, tf: str) -> dict | None:
    """读 bars 序列 meta；缺文件 → None；坏 meta → LockboxError（fail-closed）。

    与 mt5data.schedule.series_inventory 同口径的必需字段校验——水位/
    来源字段缺损的 meta 不可信，保守拒绝而不是当作没有。
    """
    p = os.path.join(root, "bars", symbol, tf, "meta.json")
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            meta = json.load(f)
        if not isinstance(meta, dict):
            raise TypeError(f"meta 应为 JSON 对象，实际 {type(meta).__name__}")
        missing = [k for k in REQUIRED_META_FIELDS if k not in meta]
        if missing:
            raise KeyError(f"缺少字段 {','.join(missing)}")
    except (OSError, ValueError, TypeError, KeyError) as e:
        raise LockboxError(
            f"{symbol}/{tf} 的 meta.json 损坏（{type(e).__name__}: {e}）——"
            "研究边界的水位/来源不可信，保守拒绝（fail-closed）") from e
    return meta


def _ext_declared_cutoffs(root: str, symbol: str, tf: str
                          ) -> tuple[dict[str, datetime], list[str]]:
    """ext 段的声明上界（H-01）：{标签: research_cutoff} 与未打戳来源清单。

    - 只看**有 data.parquet** 的来源目录（无数据的 manifest 不构成边界）；
    - 有数据无 manifest → LockboxError（来源与版本不明，与
      mt5data.ext.core.load_ext_series 同口径 fail-fast）；
    - manifest 损坏 → LockboxError；缺 research_cutoff 记入未打戳清单，
    是否放行由调用方按"有无锁箱声明兜底"决定（主库源 manifest 本就不带
      该字段，lockbox 全局 cutoff 兜底属正常；研究库 ext 段必须带戳）。
    """
    declared: dict[str, datetime] = {}
    unstamped: list[str] = []
    ext_root = os.path.join(root, "ext")
    if not os.path.isdir(ext_root):
        return declared, unstamped
    for source in sorted(os.listdir(ext_root)):
        d = os.path.join(ext_root, source, symbol, tf)
        if not os.path.exists(os.path.join(d, "data.parquet")):
            continue
        mp = os.path.join(d, "manifest.json")
        if not os.path.exists(mp):
            raise LockboxError(
                f"ext {source}/{symbol}/{tf} 有 data.parquet 却没有 "
                "manifest.json——来源与边界不明，保守拒绝（fail-closed）")
        try:
            with open(mp, encoding="utf-8") as f:
                manifest = json.load(f)
            if not isinstance(manifest, dict):
                raise TypeError("manifest 应为 JSON 对象")
        except (OSError, ValueError, TypeError) as e:
            raise LockboxError(
                f"ext {source}/{symbol}/{tf} 的 manifest 损坏"
                f"（{type(e).__name__}: {e}）——保守拒绝（fail-closed）") from e
        if manifest.get("research_cutoff"):
            declared[f"ext:{source}"] = _parse_bound_iso(
                manifest["research_cutoff"],
                f"ext {source}/{symbol}/{tf} manifest.research_cutoff")
        else:
            unstamped.append(source)
    return declared, unstamped


def effective_research_bound(data_root, symbol: str, timeframe: str, *,
                             end=None,
                             lockbox: dict[str, datetime]
                             | LockboxConfig | None = None,
                             allow_unbounded: bool = False) -> ResearchBound:
    """统一有效研究上界（H-01/X-03）：research_bars 与 ext 合并读取
    （mt5data.ext.merge）共用的**唯一**边界契约。

    汇集四类来源（详见 ResearchBound docstring）：
    1. 主库 lockbox cutoff（v2 全局 / v1 序列）；
    2. 研究导出库 bars meta.source_cutoff（导出时记录——研究库没有
       lockbox.json 是既有设计，source_cutoff 就是它的边界）；
    3. ext manifest.research_cutoff（研究库 ext 段的边界戳）；
    4. 显式 end（含端点）：合法（≤ 声明上界）则生效为更紧上界；越界 →
       LockboxError（显式请求不做静默截断）；
    再与已收盘水位 last_closed_time（含端点）取 min，统一换算为严格上界
    strict（合法研究行一律 time < strict）。

    fail-closed（保守拒绝，绝不带着不明边界放行）：
    - 多个声明上界并存且不一致 → LockboxError（冲突，分不清哪条是真边界）；
    - bars meta 损坏（非法 JSON / 缺必需字段）→ LockboxError；
    - ext 有数据无 manifest / manifest 损坏 → LockboxError；
    - ext 段 manifest 缺 research_cutoff 且无锁箱声明兜底 → LockboxError
      （主库源 manifest 不带该字段属正常——lockbox 兜底放行）。

    X-03：未设任何声明边界**且有数据**的库（未初始化主库、无 meta 的
    手写 parquet）→ LockboxError（"未初始化"——正式研究必须能证明数据
    来源与研究上界，不能由"没有 lockbox"自动直读放行）。
    allow_unbounded=True 是显式测试/迁移通道：保持旧行为（declared=None、
    strict 只剩水位与 end）。完全无数据的序列不触发本拒绝（missing 语义
    由调用方按 None 处理）。
    """
    root = str(data_root)
    tf = timeframe.upper()

    lb = load_config(root) if lockbox is None else lockbox
    lb_cutoff = cutoff_for(lb, symbol, timeframe)

    candidates: dict[str, datetime] = {}
    if lb_cutoff is not None:
        candidates["lockbox"] = lb_cutoff

    meta = _read_bound_meta(root, symbol, tf)
    last_closed = None
    if meta is not None:
        if meta.get("last_closed_time"):
            last_closed = _parse_bound_iso(
                meta["last_closed_time"], f"{symbol}/{tf} meta.last_closed_time")
        if meta.get("source_cutoff"):
            candidates["bars:source_cutoff"] = _parse_bound_iso(
                meta["source_cutoff"], f"{symbol}/{tf} meta.source_cutoff")

    ext_declared, ext_unstamped = _ext_declared_cutoffs(root, symbol, tf)
    candidates.update(ext_declared)

    values = sorted(set(candidates.values()))
    if len(values) > 1:
        detail = "；".join(f"{k}={v.isoformat()}"
                           for k, v in sorted(candidates.items()))
        raise LockboxError(
            f"{symbol}/{tf} 的声明研究上界冲突（fail-closed）：{detail}"
            "——分不清哪条是真边界，请先对齐 lockbox/source_cutoff/"
            "ext research_cutoff")
    declared = values[0] if values else None

    if ext_unstamped and lb_cutoff is None:
        raise LockboxError(
            f"{symbol}/{tf} 的 ext 段（{ext_unstamped}）manifest 缺 "
            "research_cutoff，库内也没有锁箱声明兜底——外部数据没有边界，"
            "保守拒绝（fail-closed）")

    # X-03：裸库（无任何声明边界）且有数据 → 未初始化，正式研究读拒绝。
    # 完全无数据的序列不在此列（research_bars 对 missing 序列返回 None）。
    if declared is None and not allow_unbounded:
        has_series_data = (
            os.path.exists(os.path.join(root, "bars", symbol, tf,
                                        "data.parquet"))
            or bool(ext_declared))
        if has_series_data:
            raise LockboxError(
                f"{symbol}/{tf} 未初始化：库内没有任何已声明的研究上界"
                "（lockbox cutoff / 导出 meta source_cutoff / ext manifest "
                "research_cutoff 均缺）——正式研究读拒绝（X-03：裸库不再"
                "自动直读；测试/迁移场景显式传 allow_unbounded=True，"
                "或先跑 lockbox_init / 用 export_research_store 导出）")

    if declared is not None and end is not None \
            and pd.Timestamp(end) > pd.Timestamp(declared):
        raise LockboxError(
            f"锁箱：{symbol.upper()} {tf} 的研究截止为 "
            f"{declared.isoformat()}，请求 end={pd.Timestamp(end).isoformat()} "
            "已越过锁箱边界——显式请求不做静默截断，请改用截止内的区间"
        )

    strict_parts: list = []
    if declared is not None:
        strict_parts.append(pd.Timestamp(declared))
    if last_closed is not None:
        try:
            tf_seconds = TIMEFRAME_SECONDS[tf]
        except KeyError as e:
            raise LockboxError(
                f"未知周期 {tf!r}：无法换算已收盘水位的严格上界"
                "（fail-closed）") from e
        strict_parts.append(pd.Timestamp(last_closed)
                            + pd.Timedelta(seconds=tf_seconds))
    if end is not None:
        strict_parts.append(pd.Timestamp(end) + pd.Timedelta(microseconds=1))
    strict = min(strict_parts) if strict_parts else None
    return ResearchBound(declared=declared, last_closed=last_closed,
                         end=end, strict=strict,
                         sources=tuple(sorted(candidates)))


def research_bars(root, symbol: str, timeframe: str, *,
                  end: datetime | None = None,
                  lockbox: dict[str, datetime] | LockboxConfig | None = None,
                  allow_unbounded: bool = False) -> pd.DataFrame | None:
    """研究侧 K 线加载器：只返回有效研究上界内的行（锁箱段绝不外发）。

    上界统一由 effective_research_bound 契约决定（主库 lockbox cutoff /
    研究导出库 source_cutoff / ext research_cutoff 冲突 fail-closed、
    显式 end、已收盘水位取 min）：
    - 显式 end 越过声明上界 → LockboxError：显式请求禁止静默截断；
    - end == 声明上界合法，但结果仍严格 < 上界（上界时刻的 bar 属锁箱）；
    - X-03：无任何声明边界且有数据的库（裸库/手写 parquet）→
      LockboxError（"未初始化"）；allow_unbounded=True 为显式测试/迁移
      通道，保持旧的直读行为；missing 序列（无数据）返回 None；
    - X-04：取数走 load_bars_consistent——撕裂快照（发布中断/内容指纹
      不符）明确拒绝（StoreConsistencyError），绝不新 parquet 配旧 meta。
    """
    bound = effective_research_bound(root, symbol, timeframe,
                                     end=end, lockbox=lockbox,
                                     allow_unbounded=allow_unbounded)
    df = load_bars_consistent(str(root), symbol, timeframe,
                              allow_unverified=allow_unbounded)
    if df is None or len(df) == 0:
        return df
    if bound.strict is not None:
        df = df[df["time"] < bound.strict]
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
