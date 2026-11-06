# N1（Pre-FOMC 漂移，USTEC/D1）真实主段预筛装配器与运行（2026-11-06，provisional）

本文记录 `scripts/h_assemble_n1_precheck.py`（N1 装配器 + 一次性真实预筛 CLI）与 `tests/test_h_n1_precheck.py`，以及 2026-11-06 对主段 **[1986-01-02, 2021-05-17)**（主段起点 = 绑定 manifest 实测首日，与 FRED 序列一致）的唯一一次有效真实预筛运行。模式完全镜像刚交付的 C002 装配器（`scripts/h_assemble_real_precheck.py` + `src/mt5quant/h_readonly_precheck.py` real 模式，聚焦评审 `docs/reviews/2026-11-06-h-real-precheck-review.md` 的实现要求逐条落实）；既有装配器与冻结 CLI 本任务零改动。产物恒为 `research_status=not_evaluated`、`execution_status=not_released`，标注 **provisional**：聚焦独立评审通过前不交 A1。

## 绑定与资格门（打开任何价格工件之前完成）

六个工件按**路径 + SHA256 双绑定**、fail-closed、未绑定路径从不读取：研究库 `data_research/` 下四个（`ext/fred/USTEC/D1/{data.parquet, manifest.json}`、`costs/spread_model.json`、`specs/latest.json`，白名单 `N1_WHITELIST`）+ 研究库外两个钉死哈希（FOMC 日历 `.superpowers/h-new-candidates/n1-fomc-calendar/fomc_meetings.json`、候选 `.superpowers/h-new-candidates/n1-draft/C005.toml`，`CALENDAR_SHA256`/`CANDIDATE_SHA256`；命令行不暴露任何覆盖入口，`verify_hash`/`calendar_sha` 参数仅供测试绑定合成副本）。manifest 结构门（source=fred/USTEC/D1、首末日可解析、与 parquet 实测首末根逐根核对）→ 逐年 used 门（FRED 无逐年声明，登记 `no_yearly_verification_declared`；若声明了逐年表则按年强制 used=true）→ 之后才解码价格（pyarrow 仅投影 `time+close`、已校验字节单次读取、先过滤窗口再进一切计算）。**哈希钉在场外登记包独立审核（同日并行）后重绑一次**：日历 `a53577d9…`（默认事件集 258→257：2003-09-15 经审核自默认集剔除、条目保留供诊断）、候选 `d00adad5…`（expected_sharpe 0.53→0.52 重折算）；旧钉 `499abd64…` 的首次运行被本装配器自身的 fail-closed 检查拒绝（exit 2，证据 `run1-stale-calendar-pin-refused.txt`）。MT5/延长段（2021-05-17 后）不进任何输入。

## 预注册缺口规则与 σ 口径（写入产物 provenance 与 hashes.rules）

- **USTEC/D1 工作日网格**（与 C002 装配器 USTEC 信号侧同一规则代码模式）：首根至末根 delivered bar 间每个工作日为期望网格，缺失工作日极大连续段记 [首缺失日 00:00, 下一根既有 bar 00:00)；周末构造性排除，假日保守记缺失；无显式登记（FRED manifest 无回收排除月，2019 类年度处理 N/A）。实测主段内推导 **305 个洞**（首洞 1986-02-17 华盛顿诞辰等真实日历事实）。
- **σ**：window_bars=1（候选声明）的 D1 单根无条件波动（close[i+1]/close[i]−1，ddof=1），主段全体、双 close 界定主段界、窗口不跨声明缺口——即 `run_h_readonly_precheck._window_sigma` 的口径（已确认它就是单根口径）去掉其 H1 专属入场小时分支：FRED 是日期标签（fred_observation_date 语义，manifest `original_timezone` 同义），无纽约小时可言，冻结 `window_volatility` 规格本身规定 D1 单一起点不传该参数。实测 σ=**1.6662%**、窗口 **8611** 个（= 主段 8917 根的 8916 个相邻对 − 305 个跨洞对，自洽）。
- **覆盖年数双口径（登记包审核契约）**：功效年数自事件时代 **1994-09-27** 起算——同冻结 `runtime_coverage` 口径在时代限定 bar 上重算，实测 **24.386 年**（6708 根、237 段）；全主段口径 32.463 年（8917 根、306 段）一并登记（`years_full_main_segment`）但不进门。σ 仍按主段全体（品种性质口径）。

## 成本口径（provenance 逐项登记）

spread = spread_model.json USTEC 小时桶（23 小时，17:00 ET 维护时段缺桶如实登记 `missing_hours=[17]`），16 点入场/出场桶 p90=6.7388e-05，来源层 ×1.5 = 冻结 `external-spread-x1.5-v1`（2026 tick 基准施于 1986-2021 属情景外推）；slippage = 项目口径 0.1×p90；commission = **0（declared_terms：账户持有人 2026-11-06 声明零佣账户类型，`.superpowers/account-fee-terms-2026-11-06.md`；calibrated.json 无 USTEC 条目，EURUSD 实测 0.0 佐证同账户类型）**；swap = **0（declared_terms_swap_free：服务商返还声明；charged 口径另检——specs/latest.json 点数模式快照最不利侧 −652 点×0.01×1 = 6.52 USD/手/夜，按参考价 13393.12 折 1 夜 ≈4.87e-04，登记为非门控参考）**；contract = 1（specs/latest.json USTEC contract_size，指数合约每手 1 指数单位）。实测来回成本 **1.1456e-04（1.146bp）**、参考价 13393.12（主段末根收盘）→ 事前成本倍数 **21.39×**（登记口径 B 毛 24.5bp；原作口径 A 49bp 对应 ≈42.8×）≥ 3 通过。

## 事件日历（仅登记，不进门、不触发事件收益）

预筛 = 无条件窗口波动 + 成本 + 覆盖年数功效；日历只做覆盖核对登记（`provenance.calendar`，事件数恒不 gate、事件收益恒不计算）：绑定日历 310 条目 / 默认事件集 257 / 主段窗 [1994-09-27, 2021-05-17) 内 **213** 个（首 1994-09-27、末 2021-04-28，与登记包审核后计数一致）；213 个事件全部有公告日 FRED bar、全部有入场锚（前一交易日收盘）；缺 bar/缺锚 fail-closed 丢事件并登记的分支为空集（合成测试单测覆盖）。K3 机械核对：公告日=该月最后一个 FRED 交易日 **15/213 ≈ 7%**（与日历月末最后工作日口径逐日相同），佐证 FOMC 日非月末流日。保守登记诊断（provenance）：事件有效年数 213/8 = 26.625、SR_req(50% 功效) 0.3272、该口径下功效 62.7%。

## 唯一一次有效真实运行（结果摘要）

命令（cwd=本工作树，既有 venv 解释器，PYTHONUTF8=1；运行史 4 次见 `.superpowers/sdd/2026-11-06-n1-precheck/RUN-LOG.txt`，前三次为 fail-closed 拒绝/被审核契约变更取代）：

```
python scripts/h_assemble_n1_precheck.py \
  --data-root <研究库 data_research 绝对路径（只读）> \
  --calendar <.superpowers/h-new-candidates/n1-fomc-calendar/fomc_meetings.json> \
  --candidate <.superpowers/h-new-candidates/n1-draft/C005.toml> \
  --output <独占输出目录>
```

产物 `result.json`（payload SHA256 `1a450369bebed3ebf812f0a915e2e9e00289ce82b8dce274cfa5cce22115a47e`；stdout 与文件逐字节一致、stderr 空、退出 0）：候选 C005（N1 草案，占位编号）；`diagnostic_status=engineering_waiting`，reasons 仅 `cumulative_n_unknown`（D33 组成待审核，cumulative_n 恒 None、α/N 功效 unknown）。文献/持仓门通过；成本门通过（21.39×）；功效门通过：σ=1.6662% → 隐含夏普 (0.00245−1.1456e-04)/0.016662×√8 = **0.39646** < 声明 0.52 → 取小者 0.39646，事件时代年数 24.386 → 单次检验力 **59.5%** ≥ 50%（同 sharpe 在全主段口径 32.463 年下的复核值为 70.2%，非门；min-detectable-Sharpe(80%) 0.5389 如实高于 sharpe_used，门为 50% 口径）。候选声明的 σ 量级参考 1.27% 被实测 1.67% 显著突破、隐含夏普跌破声明值——按装载器既有规则如实取小者，不回改声明。本产物为工程诊断：不构成研究结论、不构成生产输入放行、不触发任何执行。

## 测试与证据

新套件 `tests/test_h_n1_precheck.py`（15 项，合成 parquet/日历/候选副本，不绑真实路径与真实哈希）：白名单/日历/候选哈希不符与缺文件、键集不符 fail-closed、未绑定文件不被读、used=false 年在价格解析前拒绝（stub 非法 parquet 字节证明顺序）、manifest 首末日与 parquet 不符拒绝、窗口过滤与主段起点实测、工作日网格缺口规则单测（假日洞/跨周末连洞/周末排除）、D1 σ 单根口径/跨洞切窗/无小时分支/双 DST 季窗口全计、日历结构拒绝（meetings 非列表/缺字段/默认集 announcement≠end_date/缺 era_start/重复键）、事件计数与锚/缺锚/月末重叠登记、首根日事件无锚分支、成本 declared_terms 与 charged 参考/情景点差/合约登记、资格互串拒绝（synthetic_only 输入与非法资格值）、CLI 对合成根 fail-closed 与无覆盖旗标、钉与计划逐字一致。旧 `tests/test_h_real_precheck.py`（13）、`tests/test_h_readonly_precheck.py`（61）、`tests/test_h_main_scope.py`、`tests/test_h_time_audit.py` 合计 **315 passed**（既有装配器与冻结 CLI 零 diff）。RED（15 failed）→ GREEN（15 passed）、旧套件 315 passed、四次运行史与真实运行 stdout/退出码/产物哈希存本工作树 `.superpowers/sdd/2026-11-06-n1-precheck/`（不提交）。
