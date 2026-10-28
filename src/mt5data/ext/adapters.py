"""外部数据源适配器：原始字节 → 仓库 K 线（naive UTC）+ manifest（T67）。

已实测可达（2026-10-27，见 docs/research/ext-data-2026-10-27.md）：
- HistData：M1 **bid**，ASCII csv（; 分隔）；官方称标签为 EST 固定，实测
  （与 MT5/UTC 重叠期逐日收盘价对齐）为"美东偏移 + **欧盟 DST 日历**"
  （等效 EET−7h 管道），换算见 core.histdata_stamps_to_utc
  （converter_version=3）；
- CryptoDataDownload（Bitstamp）：BTC 现货日线，UTC 日期，成交价 OHLCV；
- FRED：NASDAQ100 指数**收盘价**（close-only，OHLC 退化为收盘）。

实测不可达（如实记录）：Stooq 的 CSV 端点对数据中心 IP 一律
"Access denied"（即便通过其浏览器质询）；Dukascopy datafeed TLS 无法建立。
两者的解析器保留（格式公开），下载函数会给出明确错误。

批准状态（T81 落地 D36/第十二轮审核；T87 落地第十三轮 §5，详见
mt5data.ext.approvals）：fred/cdd/histdata 批准入研究库（manifest 带
使用限定 usage_restriction——histdata 为第十三轮 §5 有条件批准：只作
个人研究、不再分发、低频下载、只启用逐年口径验证通过的年份）；yahoo
不批准（已从可选来源移除）。
"""
from __future__ import annotations

import hashlib
import http.cookiejar
import io
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

import pandas as pd

from mt5data.store import _COLUMNS
from mt5data.ext.approvals import (USAGE_RESTRICTION_CDD,
                                   USAGE_RESTRICTION_FRED,
                                   USAGE_RESTRICTION_HISTDATA)
from mt5data.ext.core import (ExtDataError, ExtSeries, histdata_stamps_to_utc,
                              sha256_bytes)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

# 使用条款摘要（批准结论见 mt5data.ext.approvals；第十三轮 §5）
TERMS_HISTDATA = (
    "histdata.com 免费外汇/黄金历史数据；站内无使用条款页（T81 核查，"
    "查找记录存本地 data/ext/terms/histdata-2026-10-27.txt）。第十三轮"
    " §5 有条件批准（2026-10-28）：FAQ 'Why Free?' 原文写明数据为交易者"
    "与开发者回测交易系统而免费分享、'Use the data at your own will "
    "and risk.'——只作个人研究、不再分发；按网站正常下载路径低频下载、"
    "不并发、不绕过限制；只启用逐年时间口径验证通过的年份（EURUSD 另加"
    "伦敦 16:00 定盘窗分钟波动尖峰锚点逐年核对）。")
TERMS_CDD = (
    "CryptoDataDownload 免费提供交易所公开行情的聚合 CSV；页面条款允许"
    "个人下载使用、限非商业用途。Bitstamp 数据为交易所公开成交。"
    "第十二轮审核批准用于研究（D36，2026-10-27），使用限定见 "
    "usage_restriction 字段。")
TERMS_FRED = (
    "FRED（圣路易斯联储）允许免费下载使用；NASDAQ100 指数值版权归纳斯达克"
    "（FRED 页面标注 Copyrighted: Pre-Approval Required）。第十二轮审核批准"
    "用于研究（D36，2026-10-27）：原始数据与逐日序列不入公开仓库、不再分发，"
    "使用限定见 usage_restriction 字段。")
TERMS_STOOQ = (
    "stooq.com 免费行情；条款注明数据仅供个人使用、不保证准确性。"
    "实测其批量 CSV 端点对数据中心 IP 拒绝（Access denied），不用。")


class ExtSourceUnreachable(RuntimeError):
    """网络层不可达或被拒（如实向调用方报告，不重试到天荒地老）。"""


def _http_get(opener, url: str, *, tries=4, timeout=60) -> bytes:
    last: Exception | None = None
    for i in range(tries):
        try:
            with opener.open(url, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001 —— 统一重试
            last = e
            time.sleep(2 + 2 * i)
    raise ExtSourceUnreachable(f"GET {url} 失败（重试 {tries} 次）：{last}")


def _http_post(opener, url: str, body: bytes, *, tries=4, timeout=180,
               headers: dict | None = None) -> bytes:
    last: Exception | None = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=body, headers=headers or {})
            with opener.open(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 + 2 * i)
    raise ExtSourceUnreachable(f"POST {url} 失败（重试 {tries} 次）：{last}")


def _opener_with_cookies():
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cj))
    opener.addheaders = [("User-Agent", _UA)]
    return opener


# ================================================================ HistData

def histdata_m1_from_bytes(zip_bytes: bytes, *, symbol: str,
                           downloaded_at_utc: str,
                           source_name: str | None = None,
                           label_rule: str = "eu_calendar",
                           ) -> ExtSeries:
    """HistData 年度 zip（DAT_ASCII_<SYM>_M1_<年>.csv）→ M1 bid K 线。

    格式：`YYYYMMDD HHMMSS;open;high;low;close;volume`，**无表头**；
    价格为 bid 小数（EURUSD 1.12345 六位）。

    时区——**标签规则逐年不同**，由调用方按逐年口径检测结果传入
    （consistency.yearly_label_calendar，T87）：

    - ``eu_calendar``（converter **v3**，实测 2020–2022 文件）：官方 FAQ
      称 EST 固定，实测为"美东偏移 + **欧盟 DST 日历**"（等效 EET−7h
      管道）：冬 −5/夏 −4，切换跟 3 月/10 月最后一个周日；秋季回拨日
      19:00–19:59 标签出现两次（先夏后冬）。实现于
      core.histdata_stamps_to_utc；
    - ``us_wall``（converter **v4**，实测 2013–2018 文件）：标签即美东
      墙钟（随美国 DST 调表），实现于 core.wall_clock_to_utc——早年
      文件与 2020+ 的管道不同，混用 v3 会把美欧错位窗（每年约 4 周）
      错一小时。

    其他规则（fixed_est）或判 inconsistent/undetermined 的年份**不由
    本适配器转换**——登记后不用（R13 §5 逐年启用条件）。
    """
    from mt5data.ext.core import wall_clock_to_utc
    if label_rule not in ("eu_calendar", "us_wall"):
        raise ExtDataError(
            f"不支持的 HistData 标签规则 {label_rule!r}（eu_calendar=v3 / "
            "us_wall=v4；其余规则的年份登记后不用）")
    z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    csvs = [n for n in z.namelist() if n.lower().endswith(".csv")]
    if not csvs:
        raise ExtDataError(f"HistData zip 内无 csv：{z.namelist()}")
    frames = []
    for name in csvs:
        text = z.read(name).decode("ascii", "replace")
        parts = [ln for ln in text.splitlines() if ln.strip()]
        if not parts:
            continue
        df = pd.read_csv(io.StringIO("\n".join(parts)), sep=";",
                         header=None,
                         names=["stamp", "open", "high", "low", "close",
                                "volume"])
        if label_rule == "us_wall":
            df["time"] = wall_clock_to_utc(df["stamp"])
        else:
            df["time"] = histdata_stamps_to_utc(df["stamp"])
        frames.append(df)
    df = pd.concat(frames, ignore_index=True).sort_values("time") \
        .reset_index(drop=True)
    out = pd.DataFrame({
        "time": df["time"].astype("datetime64[ns]"),
        "open": df["open"].astype("float64"),
        "high": df["high"].astype("float64"),
        "low": df["low"].astype("float64"),
        "close": df["close"].astype("float64"),
        "tick_volume": df["volume"].fillna(0).astype("int64"),
        "spread": 0, "real_volume": 0})
    name = source_name or csvs[0]
    manifest = {
        "source": "histdata",
        "source_url": "https://www.histdata.com/download-free-forex-historical-data/",
        "symbol": symbol.upper(),
        "timeframe": "M1",
        "price_side": "bid（HistData ASCII 只有买价；无 ask）",
        "original_timezone": (
            "美东偏移+美国 DST 墙钟（converter v4：冬 −5/夏 −4，切换跟"
            "美国日历）" if label_rule == "us_wall" else
            "美东偏移+欧盟 DST 日历（官方 FAQ 称 EST 固定；实测 2020–2022 "
            "等效 EET−7h 管道：冬 −5/夏 −4，切换按 3 月/10 月最后一个周日）"),
        "dst_rule": (
            "标签=美东墙钟：3 月第二个周日 02:00 → 11 月第一个周日 02:00"
            if label_rule == "us_wall" else
            "夏令时窗 = 3 月最后一个周日 19:00 标签 → 10 月最后"
            "一个周日 19:00 标签；秋季回拨日 19:00–19:59 标签重复"
            "（先夏令时侧后冬令时侧）"),
        "terms_note": TERMS_HISTDATA,
        "usage_restriction": USAGE_RESTRICTION_HISTDATA,
        "downloaded_at_utc": downloaded_at_utc,
        "source_files": [{"name": name, "sha256": sha256_bytes(zip_bytes)}],
        "label_rule": label_rule,
        "converter_version": 3 if label_rule == "eu_calendar" else 4,
        "rows": int(len(out)),
        "first_time": pd.Timestamp(out["time"].iloc[0]).isoformat(),
        "last_time": pd.Timestamp(out["time"].iloc[-1]).isoformat(),
    }
    return ExtSeries(df=out[_COLUMNS], manifest=manifest)


def histdata_raw_stamps(zip_bytes: bytes) -> pd.Series:
    """HistData 年度 zip → 原始 ASCII 时间标签（naive，未做任何换算）。

    供逐年口径验证（consistency.yearly_label_calendar，T81/G5）用：
    标签口径的规则只存在于原始标签空间，换算后的 UTC 序列无法反推。
    """
    z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    frames = []
    for name in z.namelist():
        if not name.lower().endswith(".csv"):
            continue
        raw = z.read(name)
        df = pd.read_csv(io.BytesIO(raw), sep=";", header=None,
                         usecols=[0], names=["stamp"])
        frames.append(pd.to_datetime(df["stamp"].astype(str).str.strip(),
                                     format="%Y%m%d %H%M%S"))
    if not frames:
        raise ExtDataError(f"HistData zip 内无 csv：{z.namelist()}")
    return pd.concat(frames, ignore_index=True)


HISTDATA_TK_URL = ("https://www.histdata.com/download-free-forex-historical-data/"
                   "?/ascii/1-minute-bar-quotes/{pair_lower}/{year}")


def fetch_histdata_year(pair: str, year: int, *, opener=None
                        ) -> tuple[bytes, str]:
    """下载 HistData 一年 M1 zip：GET 表单页取 tk → POST /get.php。

    返回 (zip 字节, 文件名)。表单流为公开页面流程，无需登录。
    """
    op = opener or _opener_with_cookies()
    page = HISTDATA_TK_URL.format(pair_lower=pair.lower(), year=year)
    html = _http_get(op, page).decode("utf-8", "replace")
    m = re.search(r'name="tk"[^>]*value="([^"]+)"', html)
    if not m:
        raise ExtSourceUnreachable(
            f"HistData {pair} {year} 页面无下载表单（tk 缺失）——"
            "页面结构变了或该年无数据")
    form = urllib.parse.urlencode({
        "tk": m.group(1), "date": str(year), "datemonth": str(year),
        "platform": "ASCII", "timeframe": "M1", "fxpair": pair.upper(),
    }).encode()
    raw = _http_post(op, "https://www.histdata.com/get.php",
                     form, headers={"Referer": page,
                                    "Origin": "https://www.histdata.com"})
    if raw[:2] != b"PK":
        raise ExtSourceUnreachable(
            f"HistData {pair} {year} 返回的不是 zip（{len(raw)} 字节 HTML？）")
    name = f"DAT_ASCII_{pair.upper()}_M1_{year}.zip"
    return raw, name


# ================================================================ CDD

def cdd_daily_from_bytes(raw: bytes, *, symbol: str = "BTCUSD",
                         downloaded_at_utc: str,
                         source_name: str = "Bitstamp_BTCUSD_d.csv"
                         ) -> ExtSeries:
    """CryptoDataDownload 日线 csv → D1 K 线（UTC 日期）。

    首行是站点横幅（https://…），第二行表头
    `unix,date,symbol,open,high,low,close,Volume BTC,Volume USD`；
    date 已是 UTC 日期，直接作 naive UTC 的 bar 日期（日线 = 00:00 标签）。
    """
    text = raw.decode("utf-8", "replace")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if lines and lines[0].lower().startswith("http"):
        lines = lines[1:]
    df = pd.read_csv(io.StringIO("\n".join(lines)))
    need = {"date", "open", "high", "low", "close"}
    if not need <= set(df.columns):
        raise ExtDataError(f"CDD csv 缺列：{sorted(df.columns)}")
    out = pd.DataFrame({
        "time": pd.to_datetime(df["date"]).dt.tz_localize(None)
                  .astype("datetime64[ns]"),
        "open": df["open"].astype("float64"),
        "high": df["high"].astype("float64"),
        "low": df["low"].astype("float64"),
        "close": df["close"].astype("float64"),
        "tick_volume": df.get("Volume BTC",
                              pd.Series(0, index=df.index)).fillna(0)
                         .astype("int64"),
        "spread": 0, "real_volume": 0}).sort_values("time") \
        .reset_index(drop=True)
    manifest = {
        "source": "cdd",
        "source_url": "https://www.cryptodatadownload.com/",
        "symbol": symbol.upper(),
        "timeframe": "D1",
        "price_side": "成交价（exchange last）",
        "original_timezone": "UTC（日线日期）",
        "dst_rule": "无（UTC）",
        "terms_note": TERMS_CDD,
        "usage_restriction": USAGE_RESTRICTION_CDD,
        "downloaded_at_utc": downloaded_at_utc,
        "source_files": [{"name": source_name, "sha256": sha256_bytes(raw)}],
        "converter_version": 1,
        "rows": int(len(out)),
        "first_time": pd.Timestamp(out["time"].iloc[0]).isoformat(),
        "last_time": pd.Timestamp(out["time"].iloc[-1]).isoformat(),
    }
    return ExtSeries(df=out[_COLUMNS], manifest=manifest)


def fetch_cdd_csv(dataset: str = "Bitstamp_BTCUSD_d.csv") -> bytes:
    url = f"https://www.cryptodatadownload.com/cdd/{dataset}"
    return _http_get(_opener_with_cookies(), url)


# ================================================================ FRED

def fred_daily_from_bytes(raw: bytes, *, symbol: str = "USTEC",
                          downloaded_at_utc: str,
                          series_id: str = "NASDAQ100",
                          source_name: str | None = None
                          ) -> ExtSeries:
    """FRED fredgraph.csv（observation_date,<SERIES>）→ D1 close-only。

    只有收盘价：open/high/low 退化为 close，manifest 以 close_only=True
    如实声明——下游不得把退化 OHLC 当真实高低价用。
    """
    text = raw.decode("utf-8", "replace")
    df = pd.read_csv(io.StringIO(text))
    if "observation_date" not in df.columns or series_id not in df.columns:
        raise ExtDataError(f"FRED csv 缺列：{sorted(df.columns)}")
    df = df.dropna(subset=[series_id])
    close = df[series_id].astype("float64")
    out = pd.DataFrame({
        "time": pd.to_datetime(df["observation_date"]).dt.tz_localize(None)
                  .astype("datetime64[ns]"),
        "open": close, "high": close, "low": close, "close": close,
        "tick_volume": 0, "spread": 0, "real_volume": 0}) \
        .sort_values("time").reset_index(drop=True)
    manifest = {
        "source": "fred",
        "source_url": "https://fred.stlouisfed.org/series/" + series_id,
        "symbol": symbol.upper(),
        "timeframe": "D1",
        "price_side": "指数官方收盘价（close-only）",
        "original_timezone": "交易日历日期（贴 UTC 00:00 标签）",
        "dst_rule": "不适用（日线）",
        "terms_note": TERMS_FRED,
        "usage_restriction": USAGE_RESTRICTION_FRED,
        "downloaded_at_utc": downloaded_at_utc,
        "source_files": [{"name": source_name or f"{series_id}.csv",
                          "sha256": sha256_bytes(raw)}],
        "converter_version": 1,
        "rows": int(len(out)),
        "first_time": pd.Timestamp(out["time"].iloc[0]).isoformat(),
        "last_time": pd.Timestamp(out["time"].iloc[-1]).isoformat(),
        "close_only": True,
    }
    return ExtSeries(df=out[_COLUMNS], manifest=manifest)


def fetch_fred_csv(series_id: str = "NASDAQ100") -> bytes:
    """FRED fredgraph.csv。本网络实测 urllib 读取常超时（服务端对
    该客户端挑剔、curl 稳定），故 urllib 两次失败后回落系统 curl。"""
    url = ("https://fred.stlouisfed.org/graph/fredgraph.csv?id="
           + urllib.parse.quote(series_id))
    try:
        return _http_get(_opener_with_cookies(), url, tries=2, timeout=90)
    except ExtSourceUnreachable:
        import shutil
        import subprocess
        if shutil.which("curl"):
            out = subprocess.run(
                ["curl", "-sS", "-m", "120", "--fail", url],
                capture_output=True, check=True)
            return out.stdout
        raise


# ================================================================ Stooq

def stooq_daily_from_bytes(raw: bytes, *, symbol: str = "USTEC",
                           downloaded_at_utc: str,
                           source_name: str = "^ndx_d.csv"
                           ) -> ExtSeries:
    """Stooq 日线 csv（Date,Open,High,Low,Close,Volume）→ D1 K 线。"""
    text = raw.decode("utf-8", "replace")
    df = pd.read_csv(io.StringIO(text))
    need = {"Date", "Open", "High", "Low", "Close"}
    if not need <= set(df.columns):
        raise ExtDataError(f"Stooq csv 缺列：{sorted(df.columns)}")
    out = pd.DataFrame({
        "time": pd.to_datetime(df["Date"]).dt.tz_localize(None)
                  .astype("datetime64[ns]"),
        "open": df["Open"].astype("float64"),
        "high": df["High"].astype("float64"),
        "low": df["Low"].astype("float64"),
        "close": df["Close"].astype("float64"),
        "tick_volume": df.get("Volume",
                              pd.Series(0, index=df.index)).fillna(0)
                         .astype("int64"),
        "spread": 0, "real_volume": 0}).sort_values("time") \
        .reset_index(drop=True)
    manifest = {
        "source": "stooq",
        "source_url": "https://stooq.com/q/d/?s=%5Endx",
        "symbol": symbol.upper(),
        "timeframe": "D1",
        "price_side": "指数日线（交易所收盘口径）",
        "original_timezone": "交易日历日期（贴 UTC 00:00 标签）",
        "dst_rule": "不适用（日线）",
        "terms_note": TERMS_STOOQ,
        "downloaded_at_utc": downloaded_at_utc,
        "source_files": [{"name": source_name, "sha256": sha256_bytes(raw)}],
        "converter_version": 1,
        "rows": int(len(out)),
        "first_time": pd.Timestamp(out["time"].iloc[0]).isoformat(),
        "last_time": pd.Timestamp(out["time"].iloc[-1]).isoformat(),
    }
    return ExtSeries(df=out[_COLUMNS], manifest=manifest)


def fetch_stooq_csv(symbol: str = "^ndx") -> bytes:
    """Stooq 批量 CSV：需先过浏览器质询（SHA-256 工作量证明）。

    实测（2026-10-27）：质询可通过，但 CSV 端点对数据中心 IP 仍返回
    "Access denied" → 抛 ExtSourceUnreachable，如实记录、用替代源。
    """
    op = _opener_with_cookies()
    home = _http_get(op, "https://stooq.com/").decode("utf-8", "replace")
    m = re.search(r'const c="([^"]+)",d=(\d+)', home)
    if m:
        c, d = m.group(1), int(m.group(2))
        n = 0
        while not hashlib.sha256((c + str(n)).encode()).hexdigest() \
                .startswith("0" * d):
            n += 1
        _http_post(op, "https://stooq.com/__verify",
                   urllib.parse.urlencode({"c": c, "n": n}).encode())
    url = f"https://stooq.com/q/d/l/?s={urllib.parse.quote(symbol)}&i=d"
    raw = _http_get(op, url)
    text = raw.decode("utf-8", "replace")
    if text.startswith("Access denied") or "Date" not in text[:200]:
        raise ExtSourceUnreachable(
            f"Stooq {symbol} 拒绝批量下载（Access denied）——数据中心 IP "
            "被限制；改用 FRED 或用户网络下手工下载")
    return raw
