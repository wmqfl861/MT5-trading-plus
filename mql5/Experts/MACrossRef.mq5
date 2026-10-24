//+------------------------------------------------------------------+
//|                                                  MACrossRef.mq5  |
//|     T10 对账参考 EA：收盘价 SMA 快慢线交叉（参数写死 10/30）       |
//+------------------------------------------------------------------+
//
// 【用途】与 Python 侧的 MACross 策略逐行对应（P5 会把同名 Python 类加入
// mt5quant.strategies.library；本 EA 是先行参考实现），在 MT5 策略测试器
// （"基于真实 tick"模式）跑同一时段，把成交写成 CSV，供
// scripts/reconcile_mt5_tester.py 与 B 层（engine_b，逐 tick）逐笔比对。
//
// 【信号逻辑（与 Python 逐行对应；快慢线参数写死，禁止 input 改动）】
//   Python 等价（目标仓位语义，与 BuyHold 同一框架）：
//       fast = bars["close"].rolling(FAST_PERIOD).mean()    # SMA(10)
//       slow = bars["close"].rolling(SLOW_PERIOD).mean()    # SMA(30)
//       raw  = fast - slow
//       signal = np.where(raw.notna(), np.sign(raw), 0)     # +1 / 0 / -1
//   本 EA：SignalOnClosedBars() 在新 bar 的第一笔 tick（=上一根 bar 收盘后
//   第一笔 tick，与 engine_b 的成交时点一致）用 shift=1（最近一根已收盘
//   bar）的 iMA 值比较；已收盘 bar 不足 SLOW_PERIOD 根（rolling 有 NaN）
//   → 信号 0。SMA 只看最近 N 根收盘价，与 pandas rolling 完全同窗。
//
// 【编译与使用步骤（人工操作，MQL5 无法自动测试）】
//   1. 把本文件放入终端数据目录 MQL5\Experts\，MetaEditor 按 F7 编译
//      （或 metaeditor64.exe /compile:"MACrossRef.mq5" /log）。
//   2. 策略测试器：品种/周期与 Python 侧一致（如 XAUUSD H1），建模
//      "每次报价基于真实 tick"（Every tick based on real ticks），
//      日期范围 = 对账窗口；InpLots 必须等于 Python 侧 --volume-lots。
//   3. 运行结束后 CSV 写在公共目录（FILE_COMMON）：
//      <终端数据目录>\..\Common\Files\MACrossRef_trades.csv
//   4. 对账（Git Bash，示例；MACross 有 30 根预热，必须传 --start =
//      测试器起始时间，让 Python 侧 SMA 预热与 EA 一致；末笔若为 signal
//      平仓可加 --drop-b-trailing-end-of-data）：
//      .venv/Scripts/python.exe scripts/reconcile_mt5_tester.py \
//          --csv "<Common>\Files\MACrossRef_trades.csv" \
//          --strategy mt5quant.strategies.library.macross:MACross \
//          --symbol XAUUSD --timeframe H1 --params '{"fast":10,"slow":30}' \
//          --start <测试器起始时间> --end <测试器结束时间> \
//          --volume-lots <InpLots> --data-root data
//
// 【成交口径（与 engine_b 一致）】买入按触发 tick 的 Ask、卖出按 Bid；
//    目标仓位语义：交叉后反向先平后开（同一 tick）；magic 过滤本 EA 仓位。
//+------------------------------------------------------------------+
#property copyright "mt5quant T10 reconciliation reference"
#property version   "1.00"
#property description "SMA cross reference EA (hard-coded 10/30): writes fills to CSV for reconcile_mt5_tester.py"

#include <Trade\Trade.mqh>

// 与 scripts/reconcile_mt5_tester.py 的 CSV_HEADER 逐字一致（勿改列序）
#define CSV_HEADER "seq,side,volume_lots,entry_msc,entry_time,entry_price,exit_msc,exit_time,exit_price,gross,exit_reason"
#define MAGIC 20261026

#define FAST_PERIOD 10          // 快线 SMA 周期（写死：与 Python 默认参数一致）
#define SLOW_PERIOD 30          // 慢线 SMA 周期（写死）

input double InpLots = 0.1;     // 手数（必须等于 Python 侧 --volume-lots）

CTrade trade;

struct TradeRec
  {
   long     entry_msc;      // epoch 毫秒（触发 tick 的 time_msc）
   long     exit_msc;
   double   volume_lots;
   double   entry_price;    // 实际成交价（买=Ask，卖=Bid）
   double   exit_price;
   int      dir;            // +1 long / -1 short
   string   reason;         // signal | end_of_data
  };

TradeRec g_trades[];
int      g_open_dir = 0;        // 当前持仓方向（0 = 空仓）
int      g_fast = INVALID_HANDLE;
int      g_slow = INVALID_HANDLE;

//+------------------------------------------------------------------+
int OnInit()
  {
   trade.SetExpertMagicNumber(MAGIC);
   trade.SetDeviationInPoints(10);
   g_fast = iMA(_Symbol, PERIOD_CURRENT, FAST_PERIOD, 0, MODE_SMA, PRICE_CLOSE);
   g_slow = iMA(_Symbol, PERIOD_CURRENT, SLOW_PERIOD, 0, MODE_SMA, PRICE_CLOSE);
   if(g_fast == INVALID_HANDLE || g_slow == INVALID_HANDLE)
     {
      Print("iMA 句柄创建失败：", GetLastError());
      return(INIT_FAILED);
     }
   return(INIT_SUCCEEDED);
  }
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   if(g_fast != INVALID_HANDLE)
      IndicatorRelease(g_fast);
   if(g_slow != INVALID_HANDLE)
      IndicatorRelease(g_slow);
  }
//+------------------------------------------------------------------+
//| 新 bar 的第一笔 tick = 上一根 bar 收盘后的第一笔 tick             |
//+------------------------------------------------------------------+
bool IsNewBar()
  {
   static datetime s_last = 0;
   datetime t = iTime(_Symbol, PERIOD_CURRENT, 0);
   if(t == s_last)
      return(false);
   s_last = t;
   return(true);
  }
//+------------------------------------------------------------------+
//| 信号（与 Python MACross 逐行对应）：                              |
//|   fast = close.rolling(10).mean(); slow = close.rolling(30).mean()|
//|   signal = sign(fast - slow)，rolling NaN（预热不足）→ 0          |
//+------------------------------------------------------------------+
int SignalOnClosedBars()
  {
   // 已收盘 bar 不足 SLOW_PERIOD 根 → slow 为 NaN → np.where(notna) 取 0
   if(Bars(_Symbol, PERIOD_CURRENT) < SLOW_PERIOD + 1)
      return(0);
   double f[1], s[1];
   if(CopyBuffer(g_fast, 0, 1, 1, f) != 1)   // shift=1：最近一根已收盘 bar
      return(0);
   if(CopyBuffer(g_slow, 0, 1, 1, s) != 1)
      return(0);
   if(f[0] > s[0])
      return(1);
   if(f[0] < s[0])
      return(-1);
   return(0);                                 // sign(0) 按 0 处理
  }
//+------------------------------------------------------------------+
void OnTick()
  {
   // 引擎语义：bar i 收盘信号 → bar i+1 开盘第一笔 tick 成交。
   // 测试开始的第一根 bar 尚无已收盘 bar（Bars < 2）→ 不动作。
   if(Bars(_Symbol, PERIOD_CURRENT) < 2)
      return;
   if(!IsNewBar())
      return;
   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick))
      return;
   AdjustToTarget(SignalOnClosedBars(), tick);
  }
//+------------------------------------------------------------------+
//| 目标仓位调仓：同向持仓不动；反向先平后开（同一 tick）             |
//+------------------------------------------------------------------+
void AdjustToTarget(const int target, const MqlTick &tick)
  {
   if(g_open_dir == target)
      return;
   if(g_open_dir != 0)
      ClosePosition(tick, "signal");
   if(target > 0)
      OpenPosition(1, tick);
   else if(target < 0)
      OpenPosition(-1, tick);
  }
//+------------------------------------------------------------------+
//| 多头按触发 tick 的 Ask 买入 / 空头按 Bid 卖出（engine_b 口径）    |
//+------------------------------------------------------------------+
void OpenPosition(const int dir, const MqlTick &tick)
  {
   bool ok = (dir > 0) ? trade.Buy(InpLots) : trade.Sell(InpLots);
   double price = trade.ResultPrice();
   if(price <= 0.0)
      price = (dir > 0) ? tick.ask : tick.bid;   // 回退到触发 tick 报价
   if(!ok && price <= 0.0)
     {
      Print("开仓失败：", trade.ResultRetcode(), " ", trade.ResultComment());
      return;
     }
   int n = ArraySize(g_trades);
   ArrayResize(g_trades, n + 1);
   g_trades[n].entry_msc = (long)tick.time_msc;
   g_trades[n].exit_msc = 0;
   g_trades[n].volume_lots = InpLots;
   g_trades[n].entry_price = price;
   g_trades[n].exit_price = 0.0;
   g_trades[n].dir = dir;
   g_trades[n].reason = "";
   g_open_dir = dir;
  }
//+------------------------------------------------------------------+
//| 多头按触发 tick 的 Bid 平仓 / 空头按 Ask 买回（engine_b 口径）    |
//+------------------------------------------------------------------+
void ClosePosition(const MqlTick &tick, const string reason)
  {
   if(g_open_dir == 0)
      return;
   bool ok = trade.PositionClose(_Symbol);
   double price = trade.ResultPrice();
   if(price <= 0.0)
      price = (g_open_dir > 0) ? tick.bid : tick.ask;
   for(int i = ArraySize(g_trades) - 1; i >= 0; i--)
     {
      if(g_trades[i].exit_msc != 0)
         continue;
      g_trades[i].exit_msc = (long)tick.time_msc;
      g_trades[i].exit_price = price;
      g_trades[i].reason = reason;
      break;
     }
   g_open_dir = 0;
   if(!ok)
      Print("平仓指令返回非 DONE（已按触发 tick 报价如实记录）：",
            trade.ResultRetcode(), " ", trade.ResultComment());
  }
//+------------------------------------------------------------------+
string MscToString(const long msc)
  {
   datetime sec = (datetime)(msc / 1000);
   return(StringFormat("%s.%03d",
                       TimeToString(sec, TIME_DATE | TIME_MINUTES | TIME_SECONDS),
                       (int)(msc % 1000)));
  }
//+------------------------------------------------------------------+
//| 测试结束：仍有持仓则强平（reason=end_of_data），随后落盘 CSV       |
//+------------------------------------------------------------------+
double OnTester()
  {
   if(g_open_dir != 0)
     {
      MqlTick tick;
      if(SymbolInfoTick(_Symbol, tick))
         ClosePosition(tick, "end_of_data");
     }
   WriteCsv();
   return(0.0);
  }
//+------------------------------------------------------------------+
//| 成交 CSV → 公共目录 Common\Files\<程序名>_trades.csv              |
//+------------------------------------------------------------------+
void WriteCsv()
  {
   string fname = MQLInfoString(MQL_PROGRAM_NAME) + "_trades.csv";
   int h = FileOpen(fname, FILE_WRITE | FILE_CSV | FILE_ANSI | FILE_COMMON, ',');
   if(h == INVALID_HANDLE)
     {
      Print("无法写成交 CSV（Common\\Files\\", fname, "）：", GetLastError());
      return;
     }
   FileWrite(h, CSV_HEADER);
   double cs = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_CONTRACT_SIZE);
   for(int i = 0; i < ArraySize(g_trades); i++)
     {
      double gross = (g_trades[i].exit_price - g_trades[i].entry_price)
                     * g_trades[i].dir * g_trades[i].volume_lots * cs;
      FileWrite(h,
                i + 1,
                (g_trades[i].dir > 0 ? "long" : "short"),
                DoubleToString(g_trades[i].volume_lots, 2),
                g_trades[i].entry_msc,
                MscToString(g_trades[i].entry_msc),
                DoubleToString(g_trades[i].entry_price, 8),
                g_trades[i].exit_msc,
                MscToString(g_trades[i].exit_msc),
                DoubleToString(g_trades[i].exit_price, 8),
                DoubleToString(gross, 2),
                g_trades[i].reason);
     }
   FileClose(h);
   Print("成交 CSV 已写入 Common\\Files\\", fname, "（共 ",
         ArraySize(g_trades), " 笔）");
  }
//+------------------------------------------------------------------+
