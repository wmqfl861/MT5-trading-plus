# 关卡校准草稿（2026-10-25，T13 / P5 基线策略）

> **状态：草稿（占位框架）。** G0/G1 结果与门槛建议由主会话在六张卡
> `git commit` 后依次跑 `bt screen` / `bt gate` 填写；本文件只预先钉死
> 跑法、口径与已知风险点。`config/gates.toml` 冻结中——校准结论出来前
> **不改**（改动需用户批准）。

## 1. 范围

六张卡 × 七个（品种×周期）组合，数据窗口 = 锁箱开放段
（`data/lockbox.json` v2，global_cutoff=2025-10-24T00:00 之前）：

| 卡 | 策略 | 品种 × 周期 | 网格 | 角色 |
| --- | --- | --- | --- | --- |
| H002 | `b1_donchian_breakout:DonchianBreakout` | XAUUSD、BTCUSD × H1 | 9 | 趋势基线（Moskowitz et al. 2012） |
| H003 | `b2_opening_range_breakout:NyOpeningRangeBreakout` | USTEC × M5 | 6 | 开盘区间突破基线 |
| H004 | `b3_london_breakout:LondonAsianBreakout` | EURUSD、XAUUSD × M15 | 6 | 伦敦破亚盘基线 |
| H005 | `b4_mean_reversion:TrendFilteredMeanReversion` | XAUUSD × M15 | 27 | 均值回归基线（Nagel 2012） |
| H006 | `b5_intraday_momentum:IntradayMomentumNy` | USTEC × M5 | 4 | 日内动量基线（Gao et al. 2018） |
| H007 | `b6_vol_target_long:VolTargetLong` | XAUUSD × H1 | 12 | 波动率管理基准（Moreira & Muir 2017） |

策略模块路径前缀 `mt5quant.strategies.library.`；网格合计 64 组。

## 2. 跑法（主会话执行；先 `git commit` 卡文件，再 screen/gate）

```bash
# 每张卡：先提交（先卡后测），再筛网格进台账，最后跑关卡
git add research/cards/H00X.toml && git commit -m "card(T13): H00X ..."
./.venv/Scripts/python.exe scripts/bt.py screen --card H002 \
    --strategy mt5quant.strategies.library.b1_donchian_breakout:DonchianBreakout
./.venv/Scripts/python.exe scripts/bt.py gate --card H002 \
    --strategy mt5quant.strategies.library.b1_donchian_breakout:DonchianBreakout \
    --gates G0,G1,G2,G3,G4 --out data/reports/gates/H002.md
```

G5（锁箱一次性）**不在本轮校准跑**：留给通过 G0–G4 的候选策略，
按蓝图铁律 6 一次卡一次机会。`--end` 不传即锁箱语义（`research_bars`
按 global_cutoff 钳制）。

## 3. G0 / G1 结果占位（主会话填写）

| 卡 | G0 截断/窗口/静态扫描 | G1 交易数 | G1 毛利/成本倍数 | G1 profit factor | 备注 |
| --- | --- | --- | --- | --- | --- |
| H002 | _待填_ | _待填_ | _待填_ | _待填_ | 单品种 6 年约 50–90 笔，**H1≥150 门槛可能不过**（卡内已如实预告；基线作对照线，是否豁免交易数门槛属门槛建议题） |
| H003 | _待填_ | _待填_ | _待填_ | _待填_ | M5 换手对成本 ×1.5 最敏感 |
| H004 | _待填_ | _待填_ | _待填_ | _待填_ | EURUSD 与 XAUUSD 分开看（G2 跨品种） |
| H005 | _待填_ | _待填_ | _待填_ | _待填_ | 27 组网格最大，DSR 试验次数按台账如实计 |
| H006 | _待填_ | _待填_ | _待填_ | _待填_ | 每日恰一笔；纽约 16:00 离场在冬季落在 21:00 UTC 换日窗首分钟，成本按实测小时点差如实计 |
| H007 | _待填_ | _待填_ | _待填_ | _待填_ | 对照线是 H001 BuyHold（G2 多头基准） |

## 4. 门槛建议（主会话跑完后填；在此之前 gates.toml 冻结不动）

- [ ] G1 交易次数门槛：H002/H007 这类低换手基线是否需要按策略族放宽
      （例如趋势类 H1 ≥ 60 笔），还是维持附录 B 原值并把不过关的基线
      如实记为"对照线"？
      建议：______________
- [ ] G1 毛利/成本倍数（3.0×）：M5 日内策略（H003/H006）实测分布
      是否支撑该值？建议：______________
- [ ] G2 成本放大（×1.5 通过 / ×2 备查）：建议：______________
- [ ] G2 参数邻域高原（70%）：网格 4 组的 H006 邻域只有 ±1 档共 2–3
      个邻居，分母小、方差大——是否对网格 <8 组的卡改用"全部其余组合"
      口径？建议：______________
- [ ] G3 walk-forward（样本外 ≥ 样本内 50%）：建议：______________
- [ ] G4 DSR（≥0.95，试验次数取卡家族台账数）：H005 的 27 组网格会
      抬高家族试验数、压低 DSR——按卡计还是按家族计？建议：______________
- [ ] 其他校准发现：______________

## 5. 已知风险点与口径备忘（跑关卡时对照）

1. **B1 交易数**：10–40 日通道换手低，G1 H1≥150 大概率不过——这是
   基线策略的真实属性，不是 bug；处理方式进 §4 第 1 条。
2. **reentry=immediate（全部六策略缺省）**：保护性出场后信号未翻转则
   下一根开盘重入。H002（趋势）与 H003/H004（日内突破）的重入会推高
   换手成本；卡内 rules 已如实声明。
3. **信号层时间止损 vs 引擎 time_stop_bars**：B1/B4 的"最多持有 N 根"
   在信号层实现（归零信号），因为引擎 time_stop 属保护性出场、
   immediate 模式下会立刻重入，语义相悖（策略模块 docstring 有论证）。
4. **bar 周期推导**：B2/B3/B5 的整点出入场依赖"时间索引最小正差 =
   bar 周期"。真实数据 M5/M15 网格规整（缺口只会让差变大）；若 G0
   窗口不变性在真实数据上报警，先查该假设。
5. **NY 16:00 离场 vs 换日窗**：H003 的 exit_hour 网格取 (14,15) 避开
   冬季 21:00 UTC 换日窗；H006 保持文献原始 15:30–16:00 窗口，冬季
   离场恰在 21:00 UTC——成本影响如实计入（falsified_if 已含成本 ×1.5）。
6. **H007 二元近似**：信号接口 {-1,0,1} 限制下波动率目标退化为风险
   开关；若对照 BuyHold 的形状改善显著，后续在引擎引入手数缩放后开新卡。
7. **窗口不变性声明**：六策略 lookback_bars 分别为 1704/320/128/320/
   320/128，均按"最大回看 + 最大持有 + 缓冲"推导（模块 docstring 有
   论证）；G0 窗口探针在真实数据上验证。

## 6. 结论（跑完后填）

- 通过 G0–G4 的卡：______________
- 证伪/搁置的卡与原因：______________
- gates.toml 冻结修订提案（需用户批准）：______________
