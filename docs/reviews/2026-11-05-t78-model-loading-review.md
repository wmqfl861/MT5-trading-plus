# T78 冻结模型装载独立审核与修复复核（2026-11-05）

本记录由主会话转录独立审核者直接返回的结论。审核者未参与实现；本记录区分作者测试、审核者合成探针与尚未完成的联合验证，不构成主会话自我批准。

## 固定版本与结论

- 基点：`def8744d90f75519ed9486e8d1ee7efa79ba2e6a`。
- 首轮提交：`1219b468fa20c6832814c8433d5d5f781c9ab733`。
- fix1：`d62be547ace9fff37354f84c843ac17e4c719e13`。
- fix2：`5b5c5b54e2b1e2d7e54543d4c1e3a0e681be777c`，提交树 `d12dabf207b0c5b893907b27596cfef6bb9784f5`。
- 首轮独立结论：Spec FAIL、Quality NEEDS_FIXES，两个 Important。
- fix1 复核：R1 已解决；R2 原字符串反例已修，但数值溢出仍遗漏。
- 同一审核者 fix2 定点复核：**R2 溢出遗漏 ADDRESSED；R1 保持 ADDRESSED。最终 Spec PASS / Quality PASS，fix2 未发现新的 Important/Critical 问题。**

模块仅涉及 live 配置、运行器、新专项测试和[装载说明](../ops/t78-model-loading-2026-11-05.md)。冻结的模型工件、概率表与策略代码未改。

## 审核发现与闭合

**R1：模型概率表语义未检查。** 首轮只验证表的基本配置；审核者分别将合法弱模型的 `se_win` 改为 `-10`、`p_win` 改为 `2`，重新计算模型原始字节 SHA 和装配指纹后，两种非法输入仍被接纳，真实策略在同一合成行情上由空仓信号变成空头信号。另一个范围外局面探针使用强模型，证实越界行仍被接受；该探针没有证明同样的信号转换。

fix1 在 `with_model` 前新增 base、parent、cell 的结构和语义校验：严格有限数值类型、正整数样本数、概率范围与总和、非负标准误、有效局面/动作、特征、重复行与父子引用。反例重新计算模型原始字节 SHA 和装配指纹，确保检验的是语义校验而非旧哈希失配。合法三层模型仍由真实策略消费；cell 正测采用相反方向的回退估计，证明实际命中了 cell。审核者裁定 R1 ADDRESSED。

**R2：fittable 配置错误进入旧监控写入路径。** 首轮中，可解析 TOML 声明真实概率策略、缺模型组且 `lookback_bars` 为字符串时，配置异常丢失策略信息，运行器进入 `guarded_main`，按旧路径会写 HALT 和 exit 文件。fix1 保留解析后的策略引用，但 `volume_lots=10**400` 导致的 `OverflowError` 未被包装，仍有同类问题。

fix2 仅在 `config.py:236` 现有异常包装中加入 `OverflowError`，保留策略身份。专项测试使用缺整个模型组、临时 cwd 且不传 `--data-dir` 的真实 main 调用，确认返回2且未到达 guard、网关或运行状态副作用边界；旧非 fittable H 的同类错误继续保留监控记录。运行器及已闭合的 R1 校验没有在 fix2 再次修改。审核者裁定 R2 ADDRESSED。

## 验证证据

| 阶段 | 作者实际结果 |
| --- | --- |
| 首轮相关回归 | 236 passed，退出0 |
| fix1 定向 RED | 74 failed、4 passed，退出1 |
| fix1 定向 GREEN / 相关回归 | 78 passed / 313 passed，均退出0 |
| fix2 准确 RED | 1 failed、1 passed，退出1 |
| fix2 定向 GREEN | 2 passed，退出0 |
| 最终相关回归 | **315 passed，退出0**；受测树与最终提交树一致，测试期间任务文件字节不变 |

最终命令：

```text
python -m pytest tests/test_t78_model_loading.py tests/test_mt5live_config.py tests/test_mt5live_runner.py tests/test_mt5live_integration.py tests/test_t110_health.py -q --tb=short
```

首轮审核者执行有限内存探针，截住 guard，未实际写入 HALT 或重做 paper 成交。两轮修复复核读取 diff、源码及作者日志，没有重跑上述套件；fix1 另核实溢出反例，fix2 没有新增探针。各次审核均保持 checkout、HEAD 和索引不变。

## 实际能力与未放行范围

已接通 `main → load_live_strategy → with_model 返回的新实例 → SignalRunner → 真实策略 → PaperBroker`。相同合成行情中，两份有效冻结模型产生不同信号和0.01手成交/空仓结果；没有 mock 信号函数，`fit` 被禁止。

模型及卡相对路径按配置目录解析。模型原始字节哈希、完整范围字段和外部实验上下文证明离线装配一致性，不能证明研究批准；evaluator 未绑定和绝对闭包路径迁移后需重核验的限制公开保留。新 fittable 在线启动仍在读取模型/卡之前拒绝，正路径仅限合成 offline paper。旧 H 配置与错误监控、未支持出场规则的拒绝保持。

本结论限于模块技术审核。联合模型与分线风控验收、完整集成回归及对应 main CI 另行完成；真实研究资格、模型发布、阶段 F、维护窗口与部署未由本审核放行。
