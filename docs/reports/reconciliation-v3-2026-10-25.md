# 多笔 A/B 对账报告 v3（2026-10-25，T40 / 审核H6 / 铁律第 14 条）

> 状态：**模板 + quick 样例**。四组全量对账（真实缓存成本模型）由主会话在
> tick 深回补、成本缓存重建（`scripts/build_cost_model.py --overshoot`）完成后
> 重跑并回填 §5–§7；占位处标 `【待主会话回填】`。

## 1. 目的与口径变更

第五轮审核 H6 最后一条：对账 v2 把容差放宽到 **120 秒、1 倍初始风险、
6 根 bar** 再复验"全部合规"——A/B 对账因此测不出止损逻辑里小于 1R 的错误
（例如 B 层用错触发侧、漏掉首笔穿价 tick）。本轮（T40）按铁律第 14 条
（**容差不因结果放宽**，只能依据机制设定）撤回上述常量，改为逐项按机制核对。

- 撤回的常量（`scripts/reconcile_ab.py` 中已删除，锚点测试防回归）：
  `TOL_ENTRY_TIME_MS=120_000`、`TOL_EXIT_TIME_SIGNAL_MS=120_000`、
  `NET_TOL_RISK_MULTIPLE=1.0`、`EXIT_TIME_PROTECTIVE_BARS=6.0`、
  `EXIT_TIME_OTHER_BARS=1.0`。
- 机制核对需要读 tick：CLI 经 `mt5data.ticks.load_ticks` 月分区按品种加载
  窗口内 tick（`TickProbe` 整月块缓存 + 小窗口切片），与 B 层引擎共用同一
  loader、同月分区语义。

## 2. 核对机制与容差（最终表）

| 检查 | 机制（两层各自"应该"的行为） | 容差 | 依据 |
| --- | --- | --- | --- |
| 入场时刻 | B 成交 = 就绪时刻（bar 边界 + latency_ms）后**第一笔 tick**；A = bar 开盘标签 | `\|B 成交 − 首笔 tick\| ≤ 1 ms`（`TOL_FILL_TIME_MS=1`） | B 实现语义：成交时刻即该 tick 的 `time_msc`，逐位一致；1ms 护毫秒舍入 |
| 静默边界 | 边界后 >2s 才有首笔 tick（换日窗实测 30–111s 无报价） | 单列 `entry_silent`/`exit_silent`，**不判违规** | 机制上 B 就该在该 tick 成交；差的是行情没有报价（v2 §5.1 的 17+6 笔即此类） |
| signal 出场时刻 | B 成交 = 就绪时刻后第一笔 tick；A = bar 开盘 | 同入场（1ms） | 同入场机制（先平后开同一笔 tick） |
| 保护性出场时刻 | B 成交 tick = **正确一侧（多头 Bid、空头 Ask）第一笔穿过价位的 tick**；成交价 = 该 tick 触发侧价 ± slippage；A = 价位（跳空时开盘价） | **零容差**（机制核对，无时间窗） | 引擎 B 逐 tick 判定语义；按错一侧触发 → `no_trigger_at_fill`，漏掉更早穿价 → `earlier_cross`，成交价异常 → `fill_price_mismatch` |
| 收盘价类出场时刻（time_stop/week_close/end_of_data） | B 成交 = 出场 bar 收盘边界（A 的 `exit_fill_time`）前**最后一笔 tick**，且落在出场 bar 区间内 | **零容差** | 引擎 B `_close_bar` 用 prev tick 的实现语义 |
| 净利差 | 拆解 `net_diff = 点差分量 + 止损越幅分量 + 其余`（见 §3） | **\|其余\| ≤ 1 个点差成本**（`NET_REST_TOL_SPREADS=1.0`） | 点差与越幅是机制已知差异；解释不掉的残差超 1 点差即分歧 |
| 配对 / 原因 | 同向且入场差 ≤ 1 根 bar 才配对（对齐手段，非容差）；原因一致（stop_gap↔stop 等价） | 超窗即 only_a/only_b 违规 | 正确实现的成交 tick 必在信号 bar 区间内 |

## 3. 净利差三分量拆解

- **点差分量（模型点差 vs 实际点差）**= `−(spread_cost_b − spread_cost_a)`：
  多头挂入场腿、空头挂出场腿，两侧字段即该腿点差成本。**例外**：空头非跳空
  stop / take_profit 取 0——A 层成交价"预减 spr 再加回"（engine_a
  `_protective_exit` 的 `base=sl−spr` → `_close` 再 `+spr`）、B 层按触发侧
  价格成交，实际点差不进净利（只影响触发早晚）；`spread_cost` 字段对这些行
  只是报告拆分。
- **止损越幅分量（T36/A-1.6）**= `(模型 p50 − B 实际越幅) × 手数 × 合约大小`，
  仅 `reason=stop` 的行。模型值查 `costs.stop_overshoot`（缓存 v2 的越幅表，
  `reconcile_ab.py` 自动装载并挂进 `Costs`）；实际越幅按 B 成交价回推触发侧价
  与重算价位之差。stop_gap 双方都按开盘/穿价 tick 成交、TP 不建模越幅，取 0
  （其成交差如实落进"其余"）。
- **其余** = 残差：滑点配置差（A 用模型滑点、B 用 `--slippage-price`）、
  成交 tick 价格与 bar 开/收盘之差（静默边界行）、跳空止盈的保守口径
  （A 按 TP 价位、B 按实际穿价 tick）等。

## 4. quick 样例（真实数据，2026-10-25）

命令（单组快验：`--quick` 把窗口裁到 tick 覆盖期末端 3 天）：

```bash
.venv/Scripts/python.exe scripts/reconcile_ab.py \
    --strategy mt5quant.backtest.reference:MaCrossATR \
    --symbol XAUUSD --timeframe H1 \
    --params '{"fast":10,"slow":30,"stop_loss_atr":2.0,"take_profit_atr":3.0}' \
    --data-root data --volume-lots 0.1 --allow-zero-swap --quick --ops-ack \
    --out data/reports/reconciliation-v3-quick
```

输出（摘）：窗口 2026-10-20 20:00 … 2026-10-23 19:00（69 根 H1），配对 4/4、
无 unmatched、无静默边界；时间类核对（首笔 tick / 首笔穿价 / 边界前末笔）
全过；净利"其余"超 1 点差 1 行：

| # | 方向/原因 | 机制核对 | 点差分量 | 越幅分量 | 其余 | 容差 | 判定 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | short take_profit | first_cross ✓ | 0 | 0 | +0.95 | 2.20 | 过 |
| 2 | short take_profit | first_cross ✓ | 0 | 0 | **+2.89** | 2.18 | **违规** |
| 3 | short stop | first_cross ✓ | 0 | −0.33 | +0.21 | 2.10 | 过 |
| 4 | short end_of_data | last_tick ✓ | +0.46 | 0 | +0.42 | 2.14 | 过 |

- **#2 的归因**（`data/reports/reconciliation-v3-quick/XAUUSD_H1_20261025T…/diff.csv`）：
  B 在首笔 Ask 穿过 TP 的 tick 成交（4005.785），比 TP 价位 4006.05 **有利
  0.267**（穿价距离 ×0.1 手×100 = +2.67），另有滑点配置差 +0.22（A 用模型
  滑点 ≈0.02、本次 B `--slippage-price` 缺省 0）→ 其余 +2.89 > 2.18。
  机制核对本身全对（`exit_note` 为空）：这正是收紧后的期望行为——**有利穿价
  距离没有机制分量可归**（计划三分量只含止损越幅），如实浮出供人工复核，
  不再用 1R 容差吞掉。
- **#3 的越幅分量**：缓存仍是 version 1（无越幅表，`attach_overshoot` 状态
  `cache-v1(no-overshoot)`，运行时有 ⚠ 提示），模型侧按 0——B 的实际越幅
  −0.33 全额计入越幅分量，"其余"仍只剩 +0.21。缓存 v2 重建后 A-1.6 的模型
  越幅会参与该分量。
- 四组全量对账在缓存 v2（含越幅表）重建后进行；届时建议 `--slippage-price`
  与模型滑点一致（或接受并单列该残差）。

## 5. 四组对账设置（【待主会话回填】）

| 运行 | 品种/周期 | fast/slow | SL/TP（×ATR14） | reentry | 手数 | 窗口 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | XAUUSD H1 | 10/30 | 2.0/3.0 | immediate | 0.1 | 【待回填】 |
| 2 | XAUUSD H1 | 10/30 | 2.0/3.0 | after_signal_change | 0.1 | 【待回填】 |
| 3 | USTEC M5 | 5/15 | 2.0/3.0 | immediate | 1.0 | 【待回填】 |
| 4 | USTEC M5 | 5/15 | 2.0/3.0 | after_signal_change | 1.0 | 【待回填】 |

## 6. 四组逐项结果（【待主会话回填：配对/原因一致率、时间类核对、
三分量分布、静默边界行清单、违规行逐条归因】）

## 7. 结论（【待主会话回填】）

## 8. 偏离与说明

- 越幅表缺失（缓存 v1）时 `attach_overshoot` 不报错、状态写进
  summary.meta（`overshoot: cache-v1(no-overshoot)`）并在运行时 ⚠ 提示；
  sha 校验失败则拒绝运行（缓存被改动）。主会话重跑前先重建缓存。
- TP（含跳空）的有利穿价距离与滑点配置差落在"其余"：前者若经用户确认可
  成为第四个机制分量（需按铁律 14 单独说明），本轮不加。
- 静默边界行的入场/出场**价格**与 bar 开盘标签之差同样落在"其余"（本 quick
  窗无静默行；四组全量若有，逐条列在 §6）。
- 本 quick 运行走运维豁免（深锁箱段，`--ops-ack`），台账
  `data/research/ledger.sqlite` 记 `actor=ops` 一行（card_id=OPS-RECONCILE-AB）。
- `missNY[17]`（点差模型缺纽约 17 点这一小时的统计）与 v2 相同：对账不装配
  SessionPolicy，两层同不受禁入限制，不影响口径。
