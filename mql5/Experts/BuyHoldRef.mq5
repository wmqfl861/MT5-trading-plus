//+------------------------------------------------------------------+
//|                                                   BuyHoldRef.mq5 |
//|          T10 对账参考 EA：BuyHold（买入持有基准，只做多）          |
//+------------------------------------------------------------------+
//
// 【用途】与 Python 侧 mt5quant.strategies.library.buy_hold:BuyHold 逐行对应，
// 在 MT5 策略测试器（"每次报价 / 基于真实 tick"模式）跑同一时段，把成交
// 写成 CSV，供 scripts/reconcile_mt5_tester.py 与 B 层（engine_b，逐 tick）
// 逐笔比对（时间/方向/价格/手数）。
//
// 【信号逻辑（与 Python 逐行对应）】
//   Python（mt5quant/strategies/library/buy_hold.py）：
//       def generate_signals(self, bars, params):
//           return pd.Series(1, index=bars.index, dtype="int64")
//   本 EA：目标仓位恒为多头（signal 恒 1）。引擎语义：bar i 收盘产生信号，
//   bar i+1 开盘的第一笔 tick 成交——对应下文 OnTick 里"新 bar 的第一笔
//   tick 才调仓"，且测试开始的第一根 bar（尚无已收盘 bar，Bars < 2）不动。
//   从第二根 bar 起买入并持有到数据末尾（OnTester 强平，reason=end_of_data）。
//
// 【编译与使用步骤（人工操作，MQL5 无法自动测试）】
//   1. 把本文件放入终端数据目录 MQL5\Experts\（或仓库 mql5\Experts\ 下用
//      MetaEditor 直接打开），按 F7 编译（或命令行：
//      metaeditor64.exe /compile:"BuyHoldRef.mq5" /log）。
//   2. 策略测试器：品种/周期与 Python 侧一致（如 XAUUSD H1），建模
//      "每次报价基于真实 tick"（Every tick based on real ticks），
//      日期范围 = 对账窗口；InpLots 必须等于 Python 侧 --volume-lots。
//   3. 运行结束后 CSV 写在公共目录（FILE_COMMON）：
//      <终端数据目录>\..\Common\Files\BuyHoldRef_trades.csv
//      （终端菜单：文件 → 打开数据目录 → 上一级 → Common\Files）。
//   4. 对账（Git Bash，示例）：
//      .venv/Scripts/python.exe scripts/reconcile_mt5_tester.py \
//          --csv "<Common>\Files\BuyHoldRef_trades.csv" \
//          --strategy mt5quant.strategies.library.buy_hold:BuyHold \
//          --symbol XAUUSD --timeframe H1 --params '{}' \
//          --volume-lots <InpLots> --end <测试器结束时间> --data-root data
//      （BuyHold 持仓到测试结束：必须传 --end = 测试器结束时间，
//        B 层末根强制平仓才会落在同一时刻。）
//
// 【成交口径（与 engine_b 一致）】买入按触发 tick 的 Ask、卖出按 Bid；
//    单一笔 netting 目标仓位，反向先平后开同一 tick；magic 过滤本 EA 仓位。
//+------------------------------------------------------------------+
#property copyright "mt5quant T10 reconciliation reference"
#property version   "1.00"
#property description "BuyHold reference EA: writes fills to CSV for reconcile_mt5_tester.py"

#include <Trade\Trade.mqh>

// 与 scripts/reconcile_mt5_tester.py 的 CSV_HEADER 逐字一致（勿改列序）
#define CSV_HEADER "seq,side,volume_lots,entry_msc,entry_time,entry_price,exit_msc,exit_time,exit_price,gross,exit_reason"
#define MAGIC 20261025

input double InpLots = 0.1;   // 手数（必须等于 Python 侧 --volume-lots）

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
int      g_open_dir = 0;    // 当前持仓方向（0 = 空仓）

//+------------------------------------------------------------------+
int OnInit()
  {
   trade.SetExpertMagicNumber(MAGIC);
   trade.SetDeviationInPoints(10);
   return(INIT_SUCCEEDED);
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
//| 信号（与 Python BuyHold.generate_signals 逐行对应）：恒 1          |
//+------------------------------------------------------------------+
int SignalOnClosedBars()
  {
   return(1);   // pd.Series(1, index=bars.index)
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
//| 多头按触发 tick 的 Ask 买入（engine_b：入场 ask）                 |
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
//| 多头按触发 tick 的 Bid 平仓（engine_b：出场 bid）                 |
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
