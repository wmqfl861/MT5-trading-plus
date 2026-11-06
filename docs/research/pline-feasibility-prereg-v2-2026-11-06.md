# T121 P 线可行性筛查预注册 v2（待独立审核）

日期：2026-11-06。状态：**v2 送审稿，待独立审核；未批准、未计数、未计算真实成本或候选结局**。本稿在独立审核子代理的 docs/reviews 结论明确批准本文件的提交与文件 SHA 之前不生效；在此之前 v1 语义继续约束一切执行。开发者不自我批准。

## 0. 版本关系与变更范围

- v1＝[pline-feasibility-prereg-2026-11-02.md](pline-feasibility-prereg-2026-11-02.md)（A2 批准版，git-blob sha256 `c78a78d13ed81c611b5f9599c96235d7bbcefceac6fda54566b103f3e8cb3235`）。v2 不改动 v1 文件本身。
- v2 的变更范围**严格限于** 2026-11-06 独立范围裁定（[.superpowers/sdd/2026-11-06-p-t121-real/scope-review.md](../../.superpowers/sdd/2026-11-06-p-t121-real/scope-review.md)，本地证据路径）的 **B 条款（用户条款费用）与 E 条款（overshoot 情景）**，以及第 3 节的 D60 入池声明。该裁定决定性事实 3 明确：采用 spread 情景外推属用途规则变更（v1 C1 冻结拒绝该摘要参与 feasible/fail），须版本化预注册 v2，并以该裁定作为 v2 的独立批准；本稿即该版本化文件。对第 4 节"逐字不变"清单的任何偏离使裁定批准失效。
- 裁定决定性事实复述（约束本稿全部解释）：**(1) 样本门结构性不可达**——要求 ≥4521 UTC 触发日，发现段日历日上限 EURUSD [2007-01-02, 2016-05-01) ≈ 3,407 天、XAUUSD ≈ 850 天，任何行不可能 feasible，点差情景口径选择不可能翻转判定，只影响行级是否额外记成本门 fail；**(2) spread 2026→2007-2016 跨期外推方向未知**（裁定更正了请求中"方向可控"的表述）；可批准依据＝判定不变性＋强制情景标注＋仅限本次用途。
- 总体计算边界不变（v1 §1 原文语义）：只回答两个问题——登记的 Δp 是否足以覆盖三倍保守成本；发现段是否有足够独立触发日。不得计算任何触发后的收益、胜负、止盈/止损先后、到期价格、Δp 实测值、拟合、排名或策略绩效；不调用 `label_triggers`、`ProbTable.fit`、回测运行器、screen/gate/prob test；产物零结局字段。本稿仍未执行任何读取或计算。

## 1. 六成本组件登记表（B/E 条款；就本次一次性 release 取代 v1 §5.1 的 C1/C2 冻结与"逐品种未知"表）

登记对象：t121-release-v1（本裁定授权的唯一真实 release），品种 XAUUSD、EURUSD，账户范围＝Demo <demo-login> @ <demo-server>（USD）。所有组件 applies＝发现段内该品种合法覆盖区间（终点一律 2016-05-01 00:00 UTC 不含）。账户范围不外推到其他账户或真实账户；条款变更须重新核验。

| 组件 | observation_kind | observed | basis 与数值/规则 | evidence_refs | 证据等级 |
| --- | --- | --- | --- | --- | --- |
| spread | `measured` | 2026 水位观测区间：XAUUSD tick 水位 2026-01-01T23:05Z→2026-10-28T16:17Z；EURUSD 2026-01-02T00:00Z→2026-10-28T16:15Z | `relative`（costs/spread_model.json version2，relative/bar_open_m1，NY 小时桶 p50/p90/p99）；用途＝**p90 情景外推至发现段**（第 2 节规则 1），不是发现段实测 | costs/spread_model.json（模型 sha `abed3b8f26a089ef839f1f8ed436ee305fcd3ca5d82b00fe106f57d97f9eb54e`）；点差缓存文件 sha `c35550b90fbefef0d61b6c767e9e5365d6480cffaee232a68da696edb667ce85` | 2026 实测水位＋跨期情景外推（方向未知，强制情景标注） |
| slippage | `measured` | 同一 2026 水位观测区间（依赖同一 spread 工件） | `spread-fraction-v1`：0.1×p90 点差比例（项目标准规则，无候选结局拟合）；dependencies=spread | 同 spread 工件引用 | 2026 实测水位上的比例规则情景 |
| commission | `declared_terms` | **不适用**（observed=null；该账户 2007-2016 不存在，**禁止视为观测依据**） | 账户条款声明 2026-11-06＋实测佐证：用户声明"mt5的当前账户类型是没有佣金"＋EURUSD 实测 round_trip 0.0（8 进/12 出、共 0.13 手）＋同账户同条款（XAUUSD 无独立佣金字段可查，按账户类型推定）。数值＝两品种 0.0；dependencies=contract | `.superpowers/account-fee-terms-2026-11-06.md`（本地证据路径；登记时按 B 条款转录为 JSON 证据工件并绑定 SHA）＋costs/calibrated.json（sha `e2cae60cf76e70f0845933863ac552c1ec379373e1b73302625c883bfa267c4c`）实测记录 | 用户权威输入＋经验证词（非服务商书面条款） |
| swap | `declared_terms` | **不适用**（observed=null；禁止视为观测依据） | 账户条款声明 2026-11-06＋实测佐证：用户声明"mt5显示会有，但是服务商会返还"＋全部 20 笔实测成交 0 隔夜费（positions_with_swap=0）。实效＝两品种 0.0（沿用 v1 swap_basis=free 语义，依据从"第十五轮记录"升级为本声明＋实测）；dependencies=contract。swap 显示值两快照出入（−496.4/−490.0）**只披露、不进成本** | 同 commission（同一条款记录与实测工件） | 用户权威输入＋经验证词 |
| contract | `spec_snapshot` | **不适用**（observed=null；规格快照非观测区间） | `size-per-lot`：XAUUSD=100 oz、EURUSD=100000 base-unit（specs/latest.json，2026-10-28 快照）。哈希不证明数值适用性；适用性由本裁定＋本登记表绑定 | specs/latest.json（文件 sha256 `144bb2245479e3557c571c9f5f83ca92db9d269211da7845c9d5aa2467d3150f`） | 2026 规格快照外推（品种/账户规格假设） |
| overshoot | `scenario` | **null**（无实测证据：trade_stops_level=0） | `scenario_multiple_of_spread_p90`：overshoot_p90＝**1.0×同品种同 NY 小时 p90 相对点差**（**不叠加**外部来源 ×1.5）；multiplier=1.0；每触发全额计入、不按实际止损概率加权；逐行标注**"非期望成本上界"**（p90 以上仍有尾部，不能称为期望成本上界）＋另披露"不含越幅的已知分量小计" | 裁定 E 条款＋spread 组件同一工件引用 | 情景（E 方案 a；保守上界情景，非实测） |

补充约束（B 条款）：

- **证据等级标注强制**：运行产物须逐组件/逐行标注 observation_kind 与证据等级；未运行=null 与实际 0=0 严格区分。
- v1 §5.1 的 C1 拒绝（2026 点差摘要仅来源披露、对应成本未知、整行 waiting_data）**就本次一次性 release 被本节＋第 2 节版本化取代**；2026 摘要的其他用途拒绝维持不变。v1 §5.1 逐品种"commission/contract 未知、swap 仅 Demo 假定"表由本登记表取代。
- v1 §5.2 中"若获准越幅模型，必须为 version2、basis=stop_level"一句由本节 overshoot 情景组件取代（本次无实测越幅模型，不得默认零，也不冒充 stop_level 模型）；其余 §5.2 细则逐字沿用（见第 4 节第 5 条）。

## 2. 情景规则（B/E 条款）

1. **spread 2026 p90 情景外推仅限本次 T121-count-cost 一次性 release**：以第 1 节登记的 2026 水位 p90（relative/bar_open_m1、NY 小时桶）情景外推至发现段 2007-2016。跨期外推方向未知，不得表述为"方向可控"或"保守"；批准依据仅为：判定不变性（任何行不可能 feasible，口径选择不可能翻转判定，只影响行级是否额外记成本门 fail）＋强制情景标注＋仅限本次用途。一次性消费，不复用于 T122、其他轮次或任何其他用途。
2. 外部来源点差 ×1.5 规则沿用 v1 §5（外部来源触发的点差乘 1.5、MT5 乘 1）；overshoot 情景按 E 条款**不叠加**该 ×1.5。
3. **XAUUSD 发现段覆盖不足披露**：XAUUSD 发现段日历日上限 ≈ 850 天（2014-01-14 起→2016-05-01 不含），结构性 < 4521；EURUSD ≈ 3,407 天，同样 < 4521。运行报告必须逐行披露该结构性不足。
4. E 方案 (a) 生效条件：**v2 未落地（未获独立审核）则 overshoot 对应整行 waiting_data**；不得默认零，不得改用其他倍数或叠加 ×1.5。

## 3. USDJPY 入池声明（D60 用户同意 2026-11-06）

- D60 记录：`.superpowers/d60-consent-2026-11-06.md`（本地证据路径）——用户 2026-11-06 同意推进品种池扩展**评估阶段**（T115 口径：只读候选品种/规格/点差/深度等）；实际新增品种入池/入研究库仍按铁律另立任务并独立审核。
- 本节为该同意下的**品种池扩展声明**：候选 USDJPY **H1/H4**（MT5 原生数据 1971-01-03T22:00Z 起；H1 180,090 行、H4 52,080 行、D1 15,813 行）。数据深度使 EURUSD 量级的长发现段成为可能，样本门可达性须在入池评估中重新计算。
- USDJPY 六成本组件证据包：`.superpowers/mt5fetch/usdjpy_cost_bundle.json`（**本地证据路径，不入公开库**）：spread 组件工件 usdjpy_spread_component.json（sha256 `1419a0b418d1260793a5be11efce3e3a664e44dafbfbf9e1a8621a9a1ff25576`）；commission 0.0（账户类型零佣金声明 2026-11-06＋EURUSD 实测）；swap 实效 0.0（服务商返还声明＋全部成交 0 隔夜费；显示值 long 0.0/short −13.7 仅披露）；slippage 0.1×p90 点差（项目标准情景）；overshoot 1.0×p90 点差（本裁定 E 条款情景）；contract 100000（MT5 symbol_info 快照 2026-11-06，同账户）。
- **声明性质**：本节不改变本次 release 的品种池（仍为 XAUUSD/EURUSD、96 行）与第 4 节任何规则；USDJPY 正式入池时须按 production adapter 六条件**重新绑定**上述本地证据路径（转为正式工件＋SHA256＋独立审核记录），不得以本声明替代。

## 4. 其余规则：逐字沿用 v1（引用＋复述，语义不变）

以下各项语义与 v1 完全一致（引用节号，复述关键约束）；任何偏离须重新版本化并重新审核。

1. **目的与计算边界**（v1 §1）：见第 0 节末段复述；批准后的计数允许读取已收盘信号的 OHLC/ATR、触发时刻、方向、动作、来源标签；未来持有窗资格只检查时间戳，不读该窗价格决定资格或成本。
2. **数据范围与分界**（v1 §2，仅品种池条目由第 3 节 D60 声明更新）：仅限已批准研究库；只用 used=true 年份；发现段截止 **2016-05-01 00:00 UTC（不含）**；H4 由合法 H1 按现行 UTC 重采样边界生成、仅完整 4 根 H1 构成、缺根不填充；来源标签规则（全 MT5=MT5、任一外部=external 点差 ×1.5、缺失/未知=该输入不可用→waiting_data）；每次获准运行记录 manifest 哈希/输入起止/分段/排除/缺口/成本缓存哈希/配置哈希/代码 SHA；缺合法输入不下载换源、输出 waiting_data。实际输入工件的逐项绑定（路径＋SHA256、gap 推导、source_tags 规则）按裁定 A 条款与前置条件 3 在 release manifest 层成文，不改本段语义。
3. **设计族与网格**（v1 §3）：F1=H1 1×ATR/2×ATR/24 根、F2=H1 2×/4×/24 根、F3=H4 1×/2×/12 根、F4=H4 2×/4×/12 根（止损/止盈/持有上限）；ATR=14 根 Wilder；每族 S1/S2/S3×A1–A4（A1/A2=follow>base，A3/A4=fade>base）；固定参数 approach.lookback_bars=24、min_distance_atr=1、touch_atr=.25、breakout_atr=.25、retest_window_bars=6、feature_warmup=2000、max_gap_days=30，H4 沿同样根数；**S1 网格**＝参考价 0.4%、1/2/5×10^k 最近值（XAUUSD 锚 1250→5 美元；EURUSD 锚 1.25→.0050），从零点整数倍起算，不赋予更高 Δp；S2＝纽约 17:00 会话极值、H1 输入计算、按会话完成时刻映射、跨 17:00 的 H4 不整根归单一会话、新会话极值次根才可用、缺口资格覆盖实际回看窗；S3＝固定根数通道（H1 20×24=480 根、H4 20×6=120 根、rolling 右移一根本根不入通道、bars_per_day 24/6、局面回看自 anchor 起算）。
4. **预期效应**（v1 §4）：Δp 固定 **0.02**（保守设计假定，非已测得优势；.05 不是文献直接估计值）；文献支持仅机制方向；McLean & Pontiff (2016) 折算参照；勘误（Neely & Weller 2003 非 Osler；Moskowitz et al. 2012 非"N 日高低点条件概率"）照旧；不给 H4/更宽括号/新网格加成，样本或成本不足不抬高 Δp。
5. **成本计算口径**（v1 §5，除被第 1/2 节取代的来源规则）：1R＝信号根 sl_atr×ATR；止盈 R=2、止损 R=1，ΔEV=.06R；需 .06R≥3×保守成本，即**成本≤.02R 等号允许**；单触发价格单位成本＝入/出场较不利绑定点差（每来回一次全幅）＋入场滑点＋合格小时最大出场滑点＋来回佣金/contract_size＋止损越幅 p90 情景（第 1 节 scenario 组件，每触发全额计入）＋非负隔夜费支出/contract_size，再除以 1R；隔夜费符号处理（Costs.overnight_fee≤0、取负转支出、非正校验）；未知出场小时取全部覆盖达标小时最大值；聚合＝同 UTC 日内均值→触发日等权，逐动作输出不平均掩盖失败；v1 §5.2 资格/缺测两行表与时段/收费细则逐字沿用（"必需成本分量缺测/未获准→整行 waiting_data"的**获准依据**现为第 1 节登记表；不删缺成本触发后以剩余样本通过；非法数值＝成本门 fail）。
6. **样本量与计数结构**（v1 §6）：p0=.40、Δp=.02、power=.50、Bonferroni α=.05/4→解析 **3014** 独立触发日；DEFF=**1.5**→**4521 日**，等号允许；UTC 日期去重/日聚合（bar 时间标签=开盘时刻）；S2 纽约会话仅定义价位不混作日边界；日数不足或无合格触发记 waiting_data；跨界未完整持有剔除仅时间戳。
7. **预写结论与停止规则**（v1 §7）：每族/品种/局面/动作行分别保留成本门、样本量门与全部原因，两门同时通过才 feasible；已知超限/非法保留成本门 fail；无 waiting_data 原因而任一门失败＝fail；全部 **4×2×3×4=96 行**＋8 汇总完整输出不选择性隐藏；可行→报告后停在 A3 由独立审核决定是否进入 T122；全部不可行→P 线休眠（重启条件含 D60 扩池获批）；无随机数/重跑种子，重跑规则照旧。
8. **执行与产物**（v1 §8＋裁定前置条件 5 的标注要求）：运行前确认审核结论绑定；指纹；独占输出目录拒绝覆盖；零收益/标签字段＋AST 依赖审计＋合成 tripwire；逐行情景标注/证据等级/账户范围/"p90 非期望成本上界"标注/XAUUSD 覆盖不足披露/未运行 null 与实际零区分；提交报告立即停在 A3。

## 5. 与 adapter 实现的关系（仅披露，不构成规则变更）

- 裁定前置条件 2 的 adapter 语义对齐小改（observation_kind 枚举 measured/declared_terms/spec_snapshot/scenario；非 measured 类 observed 可空但必填 basis/evidence_refs；scenario 必填正 multiplier；measured 保持发现段内观测区间要求）已按 RED/GREEN 实现；六条件设计、claim 锁、水位、时效与 synthetic 测试路径不变。
- adapter 的 `measured` 类仍要求观测区间终点 ≤2016-05-01（发现段内）。本稿第 1 节 spread/slippage 的 2026 水位观测区间在正式 release manifest 中的编码方式（observation_kind/basis/observed 字段取值）须在登记审核时按六条件显式裁定与绑定，不得隐式放宽任何一类校验。

**当前批准点：待独立审核。本文档经独立审核批准前不生效，v1 语义继续约束一切执行；本稿不授权任何计数、成本计算或 release 登记。**
