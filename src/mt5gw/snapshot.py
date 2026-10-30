"""品种参数快照：白名单 JSON 落盘 + 历史版本 + 差异比对（P0）。

合约参数（合约大小、库存费、冻结级别、保证金率…）可能被券商调整，
定期抓取写入 {root}/specs/latest.json 与 {root}/specs/history/<UTC戳>.json
（原子写：临时文件 + fsync + os.replace 带重试，模式对齐 mt5data.store），
diff_snapshots 给出两次快照间的逐字段变更，供成本模型审计与回测对账。

约定：
- raw 原始对象不进 JSON，只导出白名单标量字段；
- captured_at_utc 是抓取时刻（ISO UTC），不参与参数比对——每次抓取必然
  不同，比它会淹没真实变更。
- write_json_atomic 的临时文件名带 pid+uuid 唯一后缀（R6/I1：多个写者
  并发写同一目标文件时不再争用同一 tmp——固定 path+".tmp" 会让其中
  一个写者 FileNotFoundError）。
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from mt5gw.models import SymbolSpec

# 只导出可 JSON 化的标量字段（raw 不进 JSON）
_SPEC_JSON_FIELDS = (
    "name", "description", "digits", "point", "spread", "trade_mode",
    "currency_profit", "currency_margin", "currency_base", "contract_size",
    "volume_min", "volume_max", "volume_step", "filling_mode",
    "trade_stops_level",
    "swap_long", "swap_short", "trade_freeze_level",
    "tick_size", "tick_value", "trade_calc_mode",
    "margin_initial", "margin_maintenance",
    # T5（R4）：隔夜费计费口径与三倍日（券商改口径要被 diff 第一时间抓到）
    "swap_mode", "swap_rollover3days",
)
# 参与比对的字段：白名单去掉 name（即键名）与 captured_at_utc（抓取时间戳）
_DIFF_FIELDS = tuple(f for f in _SPEC_JSON_FIELDS if f != "name")


@dataclass(frozen=True)
class SpecChange:
    field: str
    symbol: str
    old: Any
    new: Any


def spec_to_json(spec: SymbolSpec) -> dict:
    """白名单标量字段 + 抓取时刻；raw 不进 JSON。"""
    out = {f: getattr(spec, f) for f in _SPEC_JSON_FIELDS}
    out["captured_at_utc"] = datetime.now(timezone.utc).isoformat()
    return out


def take_snapshot(catalog, symbols: list[str]) -> dict[str, dict]:
    """逐品种抓规格转 JSON dict（键=品种名）。取不到的品种由 catalog.get 如实抛错。"""
    return {sym: spec_to_json(catalog.get(sym)) for sym in symbols}


def save_snapshot(root_dir: str, snap: dict) -> str:
    """写 {root}/specs/latest.json + {root}/specs/history/<UTC戳>.json。

    先归档历史再刷新 latest（latest 永远指向已安全落盘的最新一份）；
    返回 latest.json 路径。
    """
    history_dir = os.path.join(root_dir, "specs", "history")
    os.makedirs(history_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    write_json_atomic(os.path.join(history_dir, f"{stamp}.json"), snap)
    latest = os.path.join(root_dir, "specs", "latest.json")
    write_json_atomic(latest, snap)
    return latest


def load_snapshot(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def diff_snapshots(old: dict, new: dict) -> list[SpecChange]:
    """逐品种逐字段比对（captured_at_utc 不比）。品种增减各记一条 field="symbol"。"""
    changes: list[SpecChange] = []
    for sym in sorted(set(old) | set(new)):
        if sym not in old:
            changes.append(SpecChange("symbol", sym, None, "<新增>"))
            continue
        if sym not in new:
            changes.append(SpecChange("symbol", sym, "<移除>", None))
            continue
        o, n = old[sym], new[sym]
        for f in _DIFF_FIELDS:
            if o.get(f) != n.get(f):
                changes.append(SpecChange(f, sym, o.get(f), n.get(f)))
    return changes


# ---- 原子写（模式对齐 mt5data.store：tmp + fsync + os.replace 带重试）----

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


def write_json_atomic(path: str, obj) -> None:
    """JSON 原子写：唯一临时文件 + fsync + os.replace 带重试（供脚本复用）。

    临时文件名带 pid+uuid 后缀（R6/I1）：两个写者并发写同一目标文件时
    各用各的 tmp，替换失败（重试耗尽等）时清理自己的 tmp 再上抛。
    """
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    _fsync_file(tmp)
    try:
        _atomic_replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)     # 自己的 tmp 自己清（唯一命名下不再被复用）
        except OSError:
            pass
        raise
