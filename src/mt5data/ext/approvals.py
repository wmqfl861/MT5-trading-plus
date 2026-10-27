"""外部数据来源批准状态与使用限定（T81 落地 D36 / 第十二轮审核）。

批准结论出自第十二轮审核报告的 D36 批准表（审核会话按蓝图 v4 §8 D49
给出，docs/reviews/2026-10-27-review-round12.md）：

- **fred**（NASDAQ100 指数收盘价）与 **cdd**（CryptoDataDownload/
  Bitstamp BTC 日线）批准用于研究；使用限定原文写入各自 manifest 的
  `usage_restriction` 字段，导出侧逐字核对；
- **histdata** 暂不批准：站内无使用条款页、未找到允许个人研究使用的
  明文（D36"条款不明的不用"）。已下载样本只留主库侧 data/ext/ 作
  一致性核对，不进研究库；查找记录存本地 data/ext/terms/
  histdata-2026-10-27.txt（data/ 不入库）；
- **stooq** 不用（服务拒访问）；**yahoo** 不批准（已从可选来源移除）；
- **dukascopy** 原则可用、当前网络不可达（接通后先条款核对与一致性
  核对，再交审核会话）。

原始数据不进公开仓库：data/ 与 data_research/ 均 gitignore，报告只放
汇总统计。
"""
from __future__ import annotations

from mt5data.ext.core import ExtDataError

REVIEW_REF = "docs/reviews/2026-10-27-review-round12.md"
APPROVED_AT = "2026-10-27"

# ---------------------------------------------------------------- 使用限定

USAGE_RESTRICTION_FRED = (
    "只作研究用途；不再分发；NASDAQ100 指数值版权归 Nasdaq（FRED 页面标注 "
    "Copyrighted: Pre-Approval Required），原始数据与逐日序列不入公开仓库"
    "（data/ 与 data_research/ 均 gitignore），报告只放汇总统计；只用于日线"
    "及以上现象检验——FRED 指数为纽约 16:00 收盘口径，与券商 USTEC 差价合约"
    "17:00 结算不同，事件窗口须按此写明（候选登记同）")
USAGE_RESTRICTION_CDD = (
    "只作研究用途；不再分发；不进入任何对外产品；原始数据不入公开仓库"
    "（data/ 与 data_research/ 均 gitignore），报告只放汇总统计")

# ---------------------------------------------------------------- 批准表

SOURCE_APPROVALS: dict[str, dict] = {
    "fred": {
        "approved": True,
        "usage_restriction": USAGE_RESTRICTION_FRED,
        "basis": ("FRED 条款允许免费下载使用；NASDAQ100 序列版权归 Nasdaq"
                  "（Copyrighted: Pre-Approval Required），故只作研究用途、"
                  "不再分发、不入公开仓库"),
        "review": REVIEW_REF,
        "approved_at": APPROVED_AT,
    },
    "cdd": {
        "approved": True,
        "usage_restriction": USAGE_RESTRICTION_CDD,
        "basis": ("CryptoDataDownload 使用条款：数据供研究起点使用、限非商业"
                  "用途；Bitstamp 行情为交易所公开成交。批准用于研究、不再分发"),
        "review": REVIEW_REF,
        "approved_at": APPROVED_AT,
    },
    "histdata": {
        "approved": False,
        "usage_restriction": None,
        "basis": ("暂不批准（D36：条款不明的不用）——histdata.com 无使用条款"
                  "页，未找到允许个人研究使用的明文；查找记录存本地 "
                  "data/ext/terms/histdata-2026-10-27.txt。已下载样本只作"
                  "一致性核对，不进研究库"),
        "review": REVIEW_REF,
        "approved_at": APPROVED_AT,
    },
    "stooq": {
        "approved": False,
        "usage_restriction": None,
        "basis": "不用：批量 CSV 端点对数据中心 IP 拒绝（Access denied）",
        "review": REVIEW_REF,
        "approved_at": APPROVED_AT,
    },
    "yahoo": {
        "approved": False,
        "usage_restriction": None,
        "basis": ("不批准（第十二轮 D36）：非官方接口，条款不明确允许程序化"
                  "下载——已从可选来源移除"),
        "review": REVIEW_REF,
        "approved_at": APPROVED_AT,
    },
}


def approved_sources() -> tuple[str, ...]:
    """--include-ext 白名单：批准用于研究库的来源，按字母序。"""
    return tuple(sorted(s for s, r in SOURCE_APPROVALS.items()
                        if r["approved"]))


def check_exportable(manifest: dict) -> None:
    """--include-ext 导出硬校验（T81/D36），违例抛 ExtDataError。

    - 来源必须在批准表内且 approved=True（白名单）；
    - 批准来源的 manifest 必须带 usage_restriction，且与批准表的限定
      **逐字一致**——缺限定或被改弱都拒绝（研究数据必须带着批准时的
      使用限定入库；批准文本变更须重新下载/重建 manifest）。
    """
    src = manifest.get("source")
    rec = SOURCE_APPROVALS.get(src)
    if rec is None or not rec["approved"]:
        raise ExtDataError(
            f"来源 {src!r} 不在 --include-ext 白名单（批准来源："
            f"{list(approved_sources())}；批准表见 {REVIEW_REF}）——"
            "未批准来源不得进入研究库")
    usage = manifest.get("usage_restriction")
    canonical = rec["usage_restriction"]
    if not isinstance(usage, str) or not usage:
        raise ExtDataError(
            f"{src}/{manifest.get('symbol')}/{manifest.get('timeframe')} 的 "
            "manifest 缺使用限定（usage_restriction 为空）——批准来源入库"
            "必须携带使用限定，拒绝导出（重新下载或补齐 manifest）")
    if usage != canonical:
        raise ExtDataError(
            f"{src}/{manifest.get('symbol')}/{manifest.get('timeframe')} 的 "
            "manifest 使用限定与批准表不一致（被改弱/改错？）——拒绝导出"
            f"（批准文本见 mt5data.ext.approvals.SOURCE_APPROVALS）")
