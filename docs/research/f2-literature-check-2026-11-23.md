# F2 文献核查：外汇、黄金、比特币月末效应的发表后证据（2026-11-23，研究线 D117）

任务定位：第二十三轮计划 §6 第 1 条（H23-1、D117），内容要求沿用第二十二轮增补 §5 第 2 条（D107）。本文件只做文献核查，供外部审核会话审核；不构成开轮、预注册或改判决定。

性质与边界：纯纸面核查。未下载任何行情数据，未连接交易终端，未计算任何策略收益或夏普，未运行任何回测、预筛、闸门或轮次脚本，未请求 G5，未改动仓库其他文件。

**核实口径**（每条文献都标明）：

- 【全文】我打开了原文或其工作稿、接受稿，读了与本题有关的章节；
- 【摘要】只读到摘要（出版社登记在 Crossref 的摘要、OpenAlex 索引的摘要或机构库摘要）；
- 【题录】只核实到题录（Crossref 的 DOI 注册记录等），结论未能核实；
- 【二手】只看到检索工具给出的摘要式描述。

出版社页面（Wiley、ScienceDirect、SSRN）多数被反爬拦截。题录改以 Crossref 的 DOI 注册记录和 OpenAlex 索引记录为准，逐条写明。

**标注约定**："文献："后面是文献里的结论（标版本和页码）；"评价："后面是我的评价。中文引号里是我对原文的翻译，英文引文才是原文。访问日期一律为 2026-11-23（UTC）。页码是我打开的 PDF 从封面起算的页码。

## 1. 结论摘要

- **外汇（月末定盘）**：定盘附近的价格模式在 1999–2019 年普遍存在，但"扣足点差后无利可图"只对**无条件、逐日**的定盘反转策略成立。按当月股市涨跌定方向的月末定盘前一小时效应，唯一直接证据的样本止于 2012-12-31；我没有找到 2013 年以后的已核实复现。
- **黄金**：月末效应没有可靠的正面证据。唯一读到的同行评审摘要（Kumar 2024）报告全样本中黄金不存在显著 TOM 效应。LBMA 定盘前后的价格行为，没有找到已核实的学术研究。
- **比特币**：2015–2021 年的样本里有正的 TOM 效应（三篇，都只读到摘要），都没有看到成本分析；2022 年以后没有找到已核实的复现。
- **Krohn、Mueller & Whelan（2024）**：题录、样本期与仓库一致。"1.37 / 1.23 / 2.01"出自央行工作稿表 IX 的**零成本**夏普；同一张表的全点差夏普是 0.15 / −0.36 / 0.05，仓库没有引。"扣足点差后大多消失"是对"交易利润大多消失"的转述，而且该文没有检验月末日子。
- **对 F3**：支持设置成本否决项，依据是 D76 条件 3 和上面这些量级证据；不支持把它写成"KMW 提出扣足点差后效应消失"。另外，Melvin & Prins 的 14bp/10% 来自十国去均值面板，直接套到"USTEC 涨幅 → EURUSD"是外推。
- **对 F2**：外汇、黄金、比特币三条腿都不满足"目标市场发表后证据为正"的文献前提。D79 清单的结论（F2 不满足 D76）不需要改，本文件只是把理由具体化。

## 2. Krohn、Mueller & Whelan（2024）的核实

### 2.1 题录与版本

| 项目 | 核实结果 |
| --- | --- |
| 引用 | Krohn, Ingomar; Mueller, Philippe; Whelan, Paul (2024). "Foreign Exchange Fixings and Returns around the Clock." *The Journal of Finance* 79(1): 541–578. |
| DOI 与日期 | 10.1111/jofi.13306；在线 2024-01-25，纸刊期 2024-02（Crossref 注册记录） |
| 我打开的版本 | 央行工作稿：Bank of Canada Staff Working Paper 2021-48（2021-10，DOI 10.34989/swp-2021-48）【全文】；作者接受稿（文内标注 "This Version: January 2023"，Warwick WRAP 机构库）【全文】 |
| 未能打开 | 期刊版正文（Wiley DOI 页停在机器人验证页）；SSRN 3521370（同样被拦）。下文的"工作稿"指央行 2021-10 版，"接受稿"指 2023-01 版 |
| 提醒 | 这篇不是 NBER 工作论文。NBER WP 31319 是另一位作者的另一篇论文（我打开核对过），不要误引 |

### 2.2 文献里的结论

1. **样本**：1999-01 至 2019-12，G9 货币对（约占现汇日成交量的 75%），日内高频收益（接受稿第 3 页；工作稿表 IX 注）。三个定盘：东京 9:55、法兰克福 14:15（ECB 定盘）、伦敦 16:00。
2. **毛模式**：美元在定盘前升值、定盘后贬值。投资于外币的美元组合在东京和欧洲定盘前"平均贬值约 2 个基点"，定盘后"升值同样的幅度"；t 值 4.5 到 11.7（接受稿第 3 页）。欧洲窗口扣费前的年化收益：欧元 15.6%、英镑 12.4%、瑞士法郎 12%（接受稿第 15 页）。
3. **成本，工作稿**（第 36–39 页，表 IX）：用路孚特的高频买卖报价当有效点差的代理，分别按 100%、50%、0% 的点差计算逐日"ECB 定盘前做多美元、伦敦定盘后做空美元"策略。欧洲窗口对 EUR 的结果（定盘前 / 定盘后 / 合计；样本 1999-01 至 2019-12）：

   | 点差口径 | 年化平均收益（%） | 夏普 |
   | --- | --- | --- |
   | 全额报价点差 | 2.02 / −0.50 / 1.52 | 0.15 / −0.36 / 0.05 |
   | 半点差 | 5.45 / 3.19 / 8.64 | 0.76 / 0.43 / 1.03 |
   | 零成本 | 8.88 / 6.88 / 15.75 | 1.37 / 1.23 / 2.01 |

   同一张表里，全点差夏普 GBP 是 −0.36 / −0.45 / −0.38，JPY 是 −1.43 / −3.03 / −2.86。作者的原话（工作稿第 38 页）：按指示性报价计入全额成本"使图景反转，多数窗口的平均收益转负"；夏普"在全额成本下为负，不计成本则为正且很高"；能拿到半点差的参与者"夏普在 0.12 到 1.03 之间"。
4. **成本，接受稿**（第 28–29 页，表 X 在第 42 页）：改为区分流动性需求方（客户，始终在客户—银行市场成交）和流动性供给方（交易商，在银行间市场按买价拿价）。样本 2006–2019，只有 AUD 和 GBP。结果（基点，括号内 t 值）：伦敦定盘前窗口，客户方 AUD −3.70（−4.10）、GBP −0.28（−0.34）；"定盘前 + 定盘后"，客户方 AUD −6.53（−5.86）、GBP −0.73（−0.74）；供给方对应为正，AUD 1.23 和 3.08，GBP 2.15 和 4.21。
5. **时间趋势**：反转收益"大部分年份为正，但样本末段有下行趋势"，原因"只能推测"（套利资本进场；2013 年定盘丑闻后的监管改革）。同时"没有清晰的证据把反转收益水平的变化与丑闻或其后的改革联系起来"，"一些最大的欧洲定盘反转收益出现在 2015 和 2016 年"（接受稿第 16 页）。工作稿第 33 页：欧洲定盘的反转收益危机后维持在约 10%/年，"样本最后两年降到 5% 以下"。
6. **与月末的关系**：工作稿全文 "month-end" 出现 0 次；接受稿出现 1 次，在相关文献段里转述 Melvin & Prins (2015)。工作稿第 33 页写的是反转收益在"每周各天、每月各周、各个月份"都为正。**这篇文章没有检验月末日子。**
7. **窗口**：接受稿第 14 页写明，欧洲窗口的反转"不包含 ECB 定盘与伦敦定盘之间的时段，因为表 I 显示这段没有清晰的方向性变动"。表 I 的数值在图里，我没有核对。

### 2.3 "扣足点差后效应大多消失"的准确含义与适用范围

- **对象是利润，不是价格效应。** 原句（工作稿第 8 页）："it is not very surprising that most of the trading profits disappear when transaction costs are incorporated"。毛价格模式本身在整段样本里普遍且显著。
- **策略是无条件的、逐日的、双腿的。** 每个交易日都做：ECB 定盘前做多美元，伦敦定盘后做空美元。不是只在月末做，不按任何信号定方向。
- **成本是客户端的报价点差。** 作者自己提示，这类指示性报价点差明显大于大交易者的实际有效点差，净收益因此"偏保守"，并引用文献建议可把指示性点差下调最多 75%（工作稿第 36 页）。
- **币种**：工作稿是 EUR、GBP、JPY；接受稿的成本表只有 AUD、GBP。
- **作者的总结**（接受稿第 6 页）："the unconditional return patterns cannot easily be arbitrage in the client market as transaction cost adjusted returns are significantly negative"，同时流动性供给方的收益为正。按表 X，"显著为负"对 AUD 成立；对 GBP，客户方各项为负，但 t 值在 −0.34 到 −1.19 之间，单看不显著，显著的是客户方与供给方之差。
- **适用范围**：它能说明"定盘相关的无条件日内漂移（约 2bp）不够覆盖客户端点差"。它**不能**说明"月末的、按信号定方向的、只在定盘前一小时持有的仓位"扣成本后不成立，这三种限定它都没有检验。

### 2.4 与仓库现有引用的对照

| 仓库表述（位置） | 核实结果（评价） |
| --- | --- |
| 作者、题名、JF 79(1): 541-578、2024（`C002.toml`、分诊表、D79 清单） | 一致 |
| "1999-01 至 2019-12（21 年）" | 一致 |
| "逐年存在定盘前持续升值、定盘后反转"（`C002.toml`、candidates-R1 两份、`H101.toml`、D79 清单、分诊表） | **偏强**。作者的表述是大部分年份为正、样本末段下行。逐年数值在图里，我无法核对 |
| "EUR 定盘周边策略免成本夏普（工作稿表 IX：1.37 / 1.23 / 2.01）"（`C002.toml`） | 数字一致，出处是央行工作稿表 IX Panel B 的零成本夏普。**有遗漏**：同表全点差夏普 0.15 / −0.36 / 0.05、半点差 0.76 / 0.43 / 1.03 没有引用。接受稿里没有夏普比，表 IX 是订单流回归表 |
| "扣足全额买卖价差后大多消失（hard to exploit after costs）"（`C002.toml` 反驳栏） | 前半句对应工作稿第 8 页的"交易利润大多消失"，不是价格效应消失。括号里的英文在两个版本里都检索不到，不是原文 |
| "定盘周边日内波动'扣足……后大多消失'"（同一处） | 不符：原文说的是收益，不是波动。定盘前后的高波动恰恰被报告为持续存在（Ito & Yamada；Osler & Turnbull 工作稿摘要） |
| "效应大多消失"（第二十二轮增补 §5）、"效应消失"（第二十二轮审核 §4.4） | 逐级加码，离原文更远 |
| "效应集中于月末"（D79 清单 F3 节） | KMW 没有这个结论。月末集中是 Melvin & Prins 的设定 |

## 3. 外汇

### 3.1 月末定盘

- **[Fx1] Melvin, M. & Prins, J. (2015)**. "Equity hedging and exchange rates at the London 4p.m. fix." *Journal of Financial Markets* 22: 50–72. DOI 10.1016/j.finmar.2014.11.001.【全文】会议稿（ECB 第三届 FX Workshop，2013-11 版）＋【摘要】RePEc 条目页；期刊版正文未能打开。
  - 文献：摘要——"equity market appreciation over the month can be used to predict currency depreciation before the end-of-month fix"。会议稿：10 国（美、欧、日、英、加、澳、瑞典、挪威、瑞士、新西兰），5 分钟 TWAP 日内数据，2004-04-28 至 2012-12-31（8.66 年）。式 (3) 的应变量是"月末最后一个定盘日伦敦时间 15:00–16:00 的货币收益"，自变量是"当月首日到倒数第二日收盘的股市收益"。面板回归系数 0.0142（R²=0.03，F=25.52，p=0.00），"a 10% equity appreciation leads to 14 basis points of currency depreciation"（PDF 第 13 页）。作者自评"Though significant, this seems like quite a small effect"。面板里的货币收益和股市收益都按十国横截面均值去均值。作者称这次贬值"至少部分在定盘后一天内回归"，并用含月末虚拟变量交互项的回归检验月末的回归是否更强（式 5；提取出的公式符号不可辨，我没有引用它的数值）。非银行金融机构在月末日的成交量是非月末日的 1.39 到 2.36 倍（流量数据 2005-05-02 至 2010-03-12）。
  - 成本：没有对策略做交易成本分析。发表后样本：否，这是原始文献，样本止于 2012-12-31。
  - 评价：窗口（伦敦 15:00–16:00、月末最后定盘日）与 F3 一致，是唯一的直接条件证据。但它是样本内结果，R²=0.03 说明单次事件的噪声远大于信号，而且早于 2013 年丑闻和 2015-02-15 改革。
  - 评价（信号口径）：按会议稿写明的去均值设定可以推出，单一货币对（如 EURUSD）的预期定盘前变动对应**两地股市收益之差**，不是美股单边涨幅。这是我的推导，作者没有就单一货币对展开；单国回归的结果表不在我读到的页面里；出版版未能打开。
  - 与仓库登记的差异：`C002.toml` 写"2004-2013 八大流动货币……ECB 会议稿第 14 页"。我打开的会议稿是 10 国、样本到 2012-12-31，系数在 PDF 第 13 页。出版版是否不同，未能核实。
- **[Fx2] Ito, T. & Yamada, M. (2018)**. "Did the reform fix the London fix problem?" *Journal of International Money and Finance* 80: 75–95. DOI 10.1016/j.jimonfin.2017.10.004。工作稿：NBER WP 23327（2017-04），DOI 10.3386/w23327。【全文】NBER 工作稿（选读）；期刊版题录已核实，正文未能打开。
  - 文献：EBS 逐笔数据，2006-01-02 至 2016-06-30，8 个货币对。2015 年 2 月的改革把定盘窗口从 1 分钟（15:59:30–16:00:30）放宽到 5 分钟（15:57:30–16:02:30）。改革后"定盘窗口的成交量尖峰消失"，但"此前文献报告的价格动态异象仍然存在"，银行的被动交易策略"带来另一种可预测性"。结论列出改革后未变的三项：定盘后的收益反转、定盘前后时段的高波动、月末历日效应。
  - 口径：他们检验的是"按定盘前 10 分钟的涨跌方向、在定盘结束后反向持仓"的反转策略（持有 1、5、10 分钟；正文另有一处写 15 分钟）和波动尾部概率。不是 Melvin-Prins 的"按当月股市涨跌、在定盘前一小时顺向持仓"。
  - 成本：定盘后一段收益按成交方向取买价或卖价，含点差。改革前，该反转策略在月末样本的主要货币对上盈利，月内样本没有盈利；改革后"月末盈利仍在"，但作者写明改革后"因样本量不足，不如改革前清晰"。
  - 发表后样本：部分。改革后是 2015-02 至 2016-06，约 17 个月末。
  - 评价：说明改革后月末定盘附近仍有可利用的价格动态，但对应的是定盘后的反转，方向与 F3 相反；改革后样本很短；作者在脚注里提示，按交易日数年化的夏普对月度机会会夸大。
  - 同一研究的短版：Yamada, M. & Ito, T. (2017). "The forex fixing reform and its impact on cost and risk of forex trading banks." *Finance Research Letters* 21: 157–162. DOI 10.1016/j.frl.2016.12.004.【摘要】RePEc 条目页：窗口放宽没有降低银行成本，反而增加了使用预对冲的风险。
- **[Fx3] Krohn, Mueller & Whelan (2024)**：见 §2。
- **[Fx4] Evans, M. D. D. (2018)**. "Forex trading and the WMR Fix." *Journal of Banking & Finance* 87: 233–247. DOI 10.1016/j.jbankfin.2017.09.017。【全文】2014-08 首稿（MPRA 58151，选读摘要和表 12）；期刊版题录已核实，正文未能打开。
  - 文献：约十年、21 个货币对的逐笔数据。定盘前后的即期变动"波动异常高、强负序列相关，在每月最后一个交易日尤其明显"，21 个货币对在整个十年普遍存在。策略：月末定盘结束后，按定盘前若干分钟的反向持仓 1、5、15 分钟。计入"半个平均买卖价差"后，"许多货币对的平均收益转负"，但也有若干仍明显为正。EUR/USD（表 12）：15 分钟档年化平均收益 2.807%（夏普 1.335），5 分钟档 −0.673%（−0.279），1 分钟档 −1.090%（−0.489），平均点差 1.708bp。
  - 成本：是，半个平均点差，平仓时计。作者明说成本因交易场所而异，无法给出对每个参与者都适用的单一净绩效。发表后样本：否，改革前数据。
  - 评价：与 Ito-Yamada 同类（定盘后反转），不是 F3 的定盘前顺向仓。对 EURUSD，扣半点差后只有 15 分钟档为正。
- **[Fx5] Osler, C. & Turnbull, A. (2026)**. "Dealer misconduct and price dynamics at the fix." *Journal of Banking & Finance* 185: 107641. DOI 10.1016/j.jbankfin.2026.107641。【题录】期刊版（在线 2026-01-26，期 2026-04）；【摘要】早期工作稿 "Dealer Trading at the Fix"（Brandeis WP 101R，RePEc 条目页日期 2017/06）。期刊版摘要未能打开。
  - 文献（工作稿摘要）：给出做市商在伦敦 16:00 定盘的行为与不当行为模型，"有助于解释为何改革后定盘波动仍然很高"；"无串谋时定盘价也会异常波动"；统计检验在 2008 年前后检测到变化。成本：摘要未涉及。发表后样本：不适用。
  - 评价：期刊版内容未经核实，只作背景登记。
- **[Fx6] Ben Zeev, N. & Nathan, D. (2024)**. "Shorting the Dollar When Global Stock Markets Roar: The Equity Hedging Channel of Exchange Rate Determination." *The Review of Asset Pricing Studies* 14(4): 640–666. DOI 10.1093/rapstu/raae012。【摘要】Crossref 摘要＋【全文】工作稿（MPRA 115194）首页。
  - 文献：用以色列机构投资者 FX 远期流量与价格的日度数据（工作稿："约 13 年"）。外国股票市值冲击使机构投资者卖出美元远期，并伴随汇率显著变动；其远期流量供给增加一个标准差，远期汇率变动 0.53%（摘要原文 "appreciates IIs' forward rate by 0.53%"）。成本：摘要未涉及；工作稿全文检索 "month-end"、"London fix" 都是 0 处。
  - 评价：用真实机构流量支持"股市涨跌 → 对冲流 → 汇率"这条机制，但不涉及月末定盘时点，也不涉及 EURUSD。只能作机制层面的旁证。
- **[Fx7] FSB 外汇基准工作组 (2014)**. "Foreign Exchange Benchmarks: Consultative Document." 2014-07-15。【全文】（选读）。
  - 文献（PDF 第 19–20 页）：WMR 16:00 定盘的"成交量尖峰在月末和季末最大，很可能反映更大的组合再平衡需求"；定盘时的订单失衡"大小和方向从日到日没有明显的可预测模式，显著例外是：对多数货币，失衡和成交量一样，在月末和季末往往最大"。
  - 成本：不适用。发表后样本：否，改革前文件。
  - 评价：官方文件证实月末、季末的定盘流量集中，同时提示失衡方向不易预测。不涉及收益。
- **[Fx8] Benenchia, M., Galati, L. & Lepone, A. (2025)**. "To fix or not to fix, the Fix: Reassessing the effectiveness of the 4 pm Fix. A pre-registered study." *Pacific-Basin Finance Journal* 93: 102652. DOI 10.1016/j.pacfin.2024.102652。在线 2024-12-24，期 2025-10。【摘要】OpenAlex 索引记录。
  - 文献：用路孚特高频数据评估 2015 年修订后 WM/R 16:00 定盘的代表性、可达成性和稳健性；"现行 5 分钟窗口总体仍然有效"，加长窗口的改进有限，代价可能更大。
  - 成本：摘要未给数值。发表后样本：是（2015 年改革后的基准质量）。
  - 评价：讲的是基准质量，不是收益可预测性，只作背景。

### 3.2 月末、月初（TOM）日历效应

三篇都只读到 Crossref 登记的摘要，都没有看到成本分析，样本货币都不是 EURUSD 这类主要货币对。

- **[Ft1] Kumar, S. (2015)**. "Turn-of-month effect in the Indian currency market." *International Journal of Managerial Finance* 11(2): 232–243. DOI 10.1108/IJMF-05-2014-0068.【摘要】USD-INR、EUR-INR、GBP-INR、JPY-INR，1999-01 至 2014-04，日度，OLS。全样本里 USD 和 JPY 存在 TOM 效应，且 TOM 日收益显著**低于**非 TOM 日（摘要没有说明报价方向）；2008 年前四种货币都显著，2008 年后除 USD 外全部消失。评价：新兴市场货币，方向与 F2 的设定相反；后半段的结果与"效应随时间消失"一致。弱。
- **[Ft2] Villarreal-Samaniego, D. (2024)**. "Calendar anomalies' adaptiveness in exchange rates: evidence from the concordance coefficient and AR-GARCH tests." *Managerial Finance* 50(11): 1971–1990. DOI 10.1108/MF-06-2024-0430。【摘要】8 种对美元汇率（摘要没有列币种），日度，2004-01 至 2023-12，滚动窗口。日历异象随时间变化，新兴市场货币较普遍、发达货币较不明显；由异象推出的交易策略"总体不显著，唯 TOM 例外——呈统计显著的亏损"。评价：对外汇腿的可交易性是不利证据；摘要无数值、无币种清单，无法判断是否含 EURUSD。弱到中。
- **[Ft3] Gadhavi, M. Y. (2025)**. "Detecting anomalies in currency market of BRICS countries: insights into emerging economies." *Managerial Finance* 51(1): 111–125. DOI 10.1108/MF-06-2024-0438。【摘要】金砖五国货币对，约 23 年 4 个月的日度数据；TOM 效应只在中国人民币上出现。评价：不涉及主要货币对，结果零散。弱。

## 4. 黄金

- **[G1] Kumar, S. (2024)**. "On the (In)efficiency of gold and bitcoin: impact of COVID-19." *Managerial Finance* 50(8): 1480–1489. DOI 10.1108/MF-02-2024-0088.【摘要】TOM 日定义为当月最后一个交易日加下月前三个交易日；OLS（HAC 标准误）。全样本中"显著为正的 TOM 效应只出现在比特币上"；新冠期间"黄金出现 TOM 效应、比特币增强"；据此构造的策略在比特币上全样本和新冠期都胜过买入持有，在黄金上只有新冠期胜过。成本：摘要未提。评价：对黄金腿是不利证据；样本区间摘要未给。弱到中。
- **[G2] Jain, V. (2019)**. "Empirical Evidence on the Existence of Calendar Anomalies in the Market for Gold and Crude Oil." SSRN 3511451. DOI 10.2139/ssrn.3511451。【题录】Crossref。SSRN 被拦截，摘要与全文未能打开，结论未能核实。
- **[G3] Hauptfleisch, M., Putniņš, T. J. & Lucey, B. (2016)**. "Who Sets the Price of Gold? London or New York." *Journal of Futures Markets* 36(6): 564–586. DOI 10.1002/fut.21775.【摘要】17 年日内数据；伦敦现货与纽约期货都参与价格发现，平均而言纽约期货的作用更大。评价：研究的是价格发现，不是定盘前后或月末的可预测性。列在这里是为了说明：在索引到的学术文献里，"伦敦金定盘前后的价格行为"没有找到直接研究。
- **[G4] Speck, D. (2023)**. "How Does Gold Perform Around the Turn of the Month?" Seasonax，2023-04-12 发布，页面显示 2025-02-13 更新。【全文】网页正文（业界）。
  - 文献：图题为"1998 至 2023 年月初前后 10 个交易日金价的平均走势：此前两天上涨尤其明显"。正文："这些天的年化收益接近 32%，而金价本身年均涨幅不到 8%"；"该效应 2006 年前后才出现"；"效应存在，但不强"。
  - 成本：未涉及。评价：商业工具的推广文章，没有统计检验，图表数值无法从正文核对。弱。
- **[G5] In Gold We Trust Nugget (2024)**. "Calendar Anomalies and the Gold Market." 2024-05-09 发布。【全文】网页正文（业界）；数值在图片里，读不到。
  - 文献：称 GLD（起点 2004-11）、GDX（2006-05）、GDXJ（2009-11）在 TOM 期间"有清晰的正收益模式"，并提醒异象强度随时间变化。
  - 成本：只对万圣节效应说了成本可忽略，对 TOM 没有分析。评价：正文没有数值。弱。
- **[G6] Sytnyk, O. (2026)**. "The London Gold Fix Drift." Zenodo 预印本，2026-10-15，DOI 10.5281/zenodo.23399984。未评审，只读了仓库记录里的说明。
  - 文献（说明）：CME 黄金期货 2010–2026（4,061 个交易日），在 LBMA 上午定盘（伦敦 10:30）前 10:25–10:31 做空，日均 +1.6bp（t≈17）；英国银行假日没有定盘时消失；强弱随年份变化很大。
  - 评价：作者自述使用 AI 辅助写作和分析代码，未经评审；不作证据，只登记其存在。它讲的是上午定盘，不是月末。

## 5. 比特币

- **[B1] Kumar, S. (2022)**. "Turn-of-the-month effect in cryptocurrencies." *Managerial Finance* 48(5): 821–829. DOI 10.1108/MF-02-2022-0084.【摘要】BTC、ETH、LTC，2015-08 至 2021-08，日度，虚拟变量回归。TOM 日收益为正且显著高于非 TOM 日，不受星期几和一月效应驱动；据此构造的策略年化胜过买入持有 BTC 21.77%、LTC 47.10%；对 GARCH(1,1) 设定和 2018-01 的下跌稳健。成本：摘要未提。评价：样本止于 2021-08；摘要没有说明 ETH 的结果；仅据摘要无法判断策略是否扣成本。弱到中。
- **[B2] Vasileiou, E. (2023)**. "Is the turn of the month an anomaly on which an investment strategy could be based? Evidence from Bitcoin and Ethereum." *International Journal of Banking, Accounting and Finance* 13(3): 388 起（Crossref 只登记起始页）。DOI 10.1504/ijbaaf.2023.129336。【摘要】OpenAlex 索引记录。不预设 TOM 窗口，用优化算法检验多个"四天"窗口；最盈利的窗口集中在月末与下月初之间（含经典的 (−1, 3) 定义）；TOM"不一定总能带来高于买入持有的利润，但风险收益比更好"。成本：摘要未提。评价：在多个窗口里挑最优，有数据挖掘的风险（我的评价）；样本区间摘要未给。弱。
- **[B3] Kumar, S. & Pathak, R. (2025)**. "The impact of COVID-19 on the efficiency of exchange rate, stock and cryptocurrency markets." *Managerial Finance* 51(6): 916–926. DOI 10.1108/MF-10-2024-0754。在线 2024-12-31。【摘要】TOM 日定义为当月最后一个交易日加下月前三个交易日；TOM 日收益显著高于非 TOM 日；新冠期间股市的 TOM 增强，"汇率与以太坊市场减弱"。成本：摘要未提。评价：摘要没有单独给出 BTC 的分段结果。弱。
- **[B4] Susana, D., Sreejith, S. G. & Kavisanmathi, J. K. (2020)**. "A Study on Calendar Anomalies in the Cryptocurrency Market." *IFIP Advances in Information and Communication Technology*（TDIT 2020）: 166–177. DOI 10.1007/978-3-030-64849-7_16.【摘要】HAL 机构库记录。市值最大的五种加密货币，2017-07-23 至 2020-07-09，GARCH(1,1) 虚拟变量回归。摘要列出的证据是"星期四、三月份和四月份、年末"；TOM 在检验之列，但摘要没有把它列为有证据的异象。成本：摘要未提。评价：会议论文集；不构成比特币月末效应的正面证据。弱。
- **[B5] Ziegler, M. & Möbius, C. (2023)**. "Der Monatswechsel-Effekt bei Bitcoin: Eine empirische Analyse." *Zeitschrift für das gesamte Bank- und Börsenwesen* 71(4): 284 起（Crossref 只登记起始页）。DOI 10.47782/oeba202304028401。【题录】Crossref。结论未能核实。
- **[B6] Sytnyk, O. (2026)**. "Scheduled Flows in Crypto: Three Gold-Fix Mechanisms, None Tradable at Retail Cost." Zenodo 预印本，2026-10-15，DOI 10.5281/zenodo.23401076。未评审，只读了仓库记录里的说明。
  - 文献（说明）：测试永续合约的资金费率结算、美国现货比特币 ETF 的估值基准窗口（纽约 15:00–16:00）、Deribit 期权到期这三类"定时流量"（2020–2026）；"三者都真实存在，但没有一个能赚钱：波动 1 到 12bp，来回吃单成本 11bp"。
  - 评价：与月末无关，但与成本否决项同向；不作证据。

## 6. 汇总表

"成本"一栏写"摘要未提"的，只表示摘要里看不出来，不表示原文没有处理。强度是我的评价。

| 编号 | 文献 | 市场 | 样本与频率 | 核心发现（文献） | 成本 | 发表后样本 | 强度（评价） |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Fx1 | Melvin & Prins 2015 | 外汇·月末定盘 | 10 国，5 分钟，2004-04 至 2012-12 | 月末定盘前 1 小时：股市涨 10% → 货币跌 14bp（去均值面板，R²=0.03） | 无 | 否（原始文献） | 中：直接、窗口一致；样本内、止于 2012 |
| Fx2 | Ito & Yamada 2018 | 外汇·月末定盘 | EBS，8 对，2006-01 至 2016-06 | 2015 改革后成交量尖峰消失；定盘后反转、高波动、月末效应仍在 | 定盘后段按买价或卖价计 | 部分（约 17 个月末） | 中低：对应定盘后反转，样本短 |
| Fx3 | Krohn 等 2024 | 外汇·定盘（非月末） | G9，日内，1999-01 至 2019-12 | 美元定盘前升、后贬，约 2bp；全额点差下客户端为负 | 有（0、50、100% 点差；客户与交易商分开） | 是（对无条件模式） | 强（就其结论而言）；对 F3 不直接适用 |
| Fx4 | Evans 2018 | 外汇·月末定盘 | 21 对，逐笔，约十年 | 月末定盘前后波动与负序列相关尤强；定盘后反转扣半点差后多数转负 | 有（半点差） | 否 | 中低：定盘后反转 |
| Fx5 | Osler & Turnbull 2026 | 外汇·定盘 | 工作稿为理论模型 | 解释改革后定盘波动仍高 | 摘要未涉及 | 不适用 | 不评分（期刊版未核实） |
| Fx6 | Ben Zeev & Nathan 2024 | 外汇·机制 | 以色列机构，日度，约 13 年 | 股票市值冲击 → 卖美元远期 → 汇率变动 | 未涉及 | 不适用 | 中（仅机制旁证） |
| Fx7 | FSB 2014 | 外汇·定盘流量 | 官方文件 | 月末、季末的成交量和订单失衡最大；方向不易预测 | 不适用 | 否 | 中（官方，流量层面） |
| Fx8 | Benenchia 等 2025 | 外汇·基准质量 | 路孚特高频 | 5 分钟窗口总体有效 | 摘要未给 | 是 | 不评分（背景） |
| Ft1 | Kumar 2015 | 外汇·TOM | 4 个对 INR 的货币对，日度，1999-01 至 2014-04 | TOM 日收益更低；2008 年后除 USD 外消失 | 摘要未提 | 自身含 2008 年前后分段 | 弱 |
| Ft2 | Villarreal-Samaniego 2024 | 外汇·TOM | 8 个对美元汇率，日度，2004-01 至 2023-12 | 异象随时间变化；TOM 策略显著亏损 | 摘要未提 | 是 | 弱到中（仅摘要） |
| Ft3 | Gadhavi 2025 | 外汇·TOM | 金砖 5 对，约 23 年 | TOM 只在人民币上出现 | 摘要未提 | — | 弱 |
| G1 | Kumar 2024 | 黄金·TOM，兼比特币 | 日度，区间摘要未给 | 全样本黄金无显著 TOM；新冠期出现 | 摘要未提 | — | 弱到中（仅摘要） |
| G2 | Jain 2019 | 黄金·日历 | — | 未能核实 | — | — | 不评分 |
| G3 | Hauptfleisch 等 2016 | 黄金·价格发现 | 17 年，日内 | 纽约期货的作用更大 | 不适用 | — | 不适用 |
| G4 | Speck 2023（业界） | 黄金·TOM | 1998–2023 | 月初前两天偏强；"不强" | 无 | — | 弱 |
| G5 | In Gold We Trust 2024（业界） | 黄金·TOM | GLD 等，2004 起 | 称有清晰的正收益模式 | 无 | — | 弱 |
| G6 | Sytnyk 2026（未评审） | 黄金·上午定盘 | 2010–2026 | 定盘前做空日均 +1.6bp | 说明里未提 | — | 不采信 |
| B1 | Kumar 2022 | 比特币·TOM | BTC、ETH、LTC，2015-08 至 2021-08 | TOM 为正；策略胜过买入持有 | 摘要未提 | — | 弱到中（仅摘要） |
| B2 | Vasileiou 2023 | 比特币·TOM | 区间摘要未给 | 最盈利的四日窗口在月末月初 | 摘要未提 | — | 弱（仅摘要；窗口搜索） |
| B3 | Kumar & Pathak 2025 | 多市场·TOM | 区间摘要未给 | TOM 为正；新冠期以太坊减弱 | 摘要未提 | — | 弱（仅摘要） |
| B4 | Susana 等 2020 | 加密·日历 | 5 币，2017-07 至 2020-07 | 证据在星期四、三四月、年末；未列 TOM | 摘要未提 | — | 弱 |
| B5 | Ziegler & Möbius 2023 | 比特币·TOM | — | 未能核实 | — | — | 不评分 |
| B6 | Sytnyk 2026（未评审） | 加密·定时流量 | 2020–2026 | 三类机制"都不赚钱"（成本 11bp） | 有（说明里） | — | 不采信 |

## 7. 对 F3 预注册的含义

以下是给审核会话的输入，不是决定。

### 7.1 成本否决项

- **文献事实**：KMW 证明的是"无条件、逐日、双腿的定盘反转，在客户端按报价点差成交时利润被成本吃掉"；毛价格模式本身在 1999–2019 年普遍存在且显著。KMW 没有检验月末日子，也没有检验按股市涨跌定方向的定盘前一小时仓位。
- **支持的一面**：设成本否决项与 D76 条件 3 一致；KMW、Evans、Ito & Yamada 三篇都把成本作为结论的一部分。同一组毛收益在不同成本口径下结论相反（KMW 的 EUR：零成本夏普 1.37 / 1.23 / 2.01，全点差 0.15 / −0.36 / 0.05），所以否决项必须把点差情景写死，不能事后选择。
- **不支持的一面**：把否决项的出处写成"KMW 提出扣足点差后效应消失"是对文献的过度转述。外部审核若核对原文，会发现 KMW 的对象是交易利润，不是价格效应，也没有检验月末。
- **建议措辞**（供审核取舍）："否决项 V-cost：在预注册主检验段内，按 charged 口径（bid-only 数据按点差 ×1.5 情景）计算的每事件净收益均值 ≤ 0，或单侧检验未达预注册的 α/N，即否决。依据：D76 条件 3；KMW（2024）表明定盘相关的无条件日内漂移量级不足以覆盖客户端点差，可交易性只能来自月末条件效应本身。"
- **分段**（我的建议）：否决项应同时覆盖主检验段全段和 2015-02-15 之后的分段。理由：唯一直接的条件证据（Melvin & Prins）样本止于 2012-12-31；EURUSD 的 HistData 自 2007-01 起，2007–2012 与原文样本重叠，2013 年以后才是真正的发表后样本。第十三轮审核已有"以 2015-02-15 定盘规则修订为界，前后两段作为诊断分段"的安排，可以沿用。

### 7.2 预注册需要正面处理的三个问题

1. **信号口径**：Melvin & Prins 的 0.0142 来自十国去均值面板。按其设定，EURUSD 的预期定盘前变动对应"美国股市涨幅减欧元区股市涨幅"，不是美股单边涨幅；推广到 GBP、JPY、CHF、AUD 对 USD 时同理，各对应"美国减当地"的股市涨幅差。C002 与 F3 草案用 USTEC 单一涨幅做信号，与原文口径不完全同构。这是我的推导；出版版未能打开，需要人工核对出版版有没有单货币对的结果。若维持 USTEC 信号，预注册里应写明这是对原文的外推。
2. **反转在定盘之后**：Melvin & Prins、Evans、Ito & Yamada 都报告定盘之后价格回归或反转。F3 在 16:00 定盘时平仓，取的是定盘前一段，这与文献一致；定盘之后没有文献支持的额外收益。
3. **发表后衰减**：KMW 报告样本末段的定盘反转收益有下行趋势，原因只能推测；同时 2015–2016 年又出现最大的欧洲定盘反转收益。月末条件效应在 2013 年以后是否仍在，没有已核实的文献能回答，只能由 F3 自己的预注册检验回答。

### 7.3 对 F2（月末 × 多品种）

- 三条腿都没有达到"目标市场发表后证据为正"的文献前提。D79 清单原话是"FX/金/BTC 腿的 TOM 证据未在本项目登记或检索"；本次核查的结果不支持推翻它的结论。具体是：黄金全样本无显著 TOM（Kumar 2024，仅摘要）；外汇 TOM 作为策略"显著亏损"（Villarreal-Samaniego 2024，仅摘要），或在 2008 年后消失（Kumar 2015，仅摘要）；比特币 2015–2021 年样本为正（Kumar 2022；Vasileiou 2023，仅摘要），但没有成本分析，也没有找到 2022 年以后的已核实复现。
- **窗口口径不一致**：仓库的月末窗是第 −2 日纽约 16:00 收盘到第 +3 日纽约 16:00 收盘（C001 的 K8 事件窗）；上述文献用的是"当月最后一个交易日加下月前三个交易日"或"四天窗口"。比较数字时要注意。
- **证据的独立性**：黄金、比特币、外汇 TOM 的正面证据集中出自同一位作者（Satish Kumar）的系列论文，刊物相同，而且我都只读到摘要；独立复现不足。

## 8. 证据不足以判断的清单

1. 月末伦敦 16:00 定盘前一小时、按股市涨跌定方向的效应，2013 年以后是否仍在。没有找到已核实的直接复现；最接近的可能是 Sizova 等（2025）的工作稿，但我只看到题录和检索摘要。
2. KMW 期刊版正文。Wiley 被拦截，表号和数值只能按工作稿和接受稿引用，期刊版可能与接受稿不同。
3. Melvin & Prins 期刊版：样本区间、货币数与会议稿是否一致（会议稿是 10 国、到 2012-12-31；仓库登记是 8 国、2004–2013）；有没有单货币对的结果。
4. Ito & Yamada 期刊版与 NBER 工作稿是否一致；Evans 期刊版与 2014 年首稿是否一致。
5. 黄金：LBMA 金价定盘前后的价格行为，没有找到已核实的学术研究；月末效应只有一篇仅摘要的论文和两篇业界文章。
6. 比特币：没有找到 2022 年以后样本的已核实研究；没有找到以"日历日"为对象的已核实研究；读到的几篇全部只有摘要。
7. 外汇 TOM：没有找到专门针对 EURUSD 等主要货币对的已核实研究。
8. 交易成本：除 KMW、Evans、Ito & Yamada 外，其余文献的成本处理在摘要里看不出来。
9. 定盘前的方向性流动：FSB（2014）只给出失衡大小在月末最大、方向不易预测；2015 年改革后的同类官方统计，我没有检索到。
10. 三篇只核实到题录的文献：Sizova、Syrstad & Sævareid (2025) "Rebalancing of Currency Hedging and the Impact on Exchange Rates"（SSRN，DOI 10.2139/ssrn.5143899）；Evans、O'Neill、Rime & Saakvitne (2018) "Fixing the Fix? Assessing the Effectiveness of the 4pm Fix Benchmark"（SSRN，DOI 10.2139/ssrn.3270844）；Baur、Cahill、Godfrey & Liu (2019) "Bitcoin time-of-day, day-of-week and month-of-year effects in returns and trading volume"（*Finance Research Letters* 31: 78–92，DOI 10.1016/j.frl.2019.04.023，题名显示研究的是时段、星期、月份）。
11. 仓库另一处引用：`C002.toml` 的检索记录写 "Evans-Marsh-Payne-Saar (2018, JFQA) 'Fixing the Fix?'（价格冲击 1-1.2bp）"。按 Crossref 的题名检索，这篇的作者是 Evans、O'Neill、Rime、Saakvitne，载体是 SSRN 工作论文，没有查到 JFQA 的记录；"1-1.2bp" 未能核实。

## 9. 来源

以下 URL 都由我在 2026-11-23（UTC）访问，括号内是访问结果。

1. KMW 的 Crossref 注册记录 https://api.crossref.org/works/10.1111/jofi.13306 （题录与摘要）；OpenAlex 索引记录 https://api.openalex.org/works/doi:10.1111/jofi.13306 （题录、摘要、各版本位置）。
2. KMW 央行工作稿页面 https://www.bankofcanada.ca/2021/10/staff-working-paper-2021-48 （已打开）；PDF https://www.bankofcanada.ca/wp-content/uploads/2021/10/swp2021-48.pdf （已读）。
3. KMW 接受稿 PDF https://wrap.warwick.ac.uk/177333/1/WRAP-foreign-exchange-fixings-returns-around-clock-Mueller-2023.pdf （已读）。
4. KMW 期刊版 https://doi.org/10.1111/jofi.13306 与 SSRN 页 https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3521370 （都停在机器人验证页，未打开）；NBER 页 https://www.nber.org/papers/w31319 （打开核对，确认不是 KMW 这篇）。
5. Melvin & Prins：RePEc 条目页 https://ideas.repec.org/a/eee/finmar/v22y2015icp50-72.html 与 https://econpapers.repec.org/RePEc:eee:finmar:v:22:y:2015:i:c:p:50-72 （摘要）；Crossref https://api.crossref.org/works/10.1016/j.finmar.2014.11.001 ；ECB 会议稿 PDF https://www.ecb.europa.eu/events/pdf/conferences/131216/Third_FX_Workshop_MELVIN_PRINS_Equity%20hedging%20and%20exchange%20rates%20Nov%202013.pdf （已读）；ScienceDirect 页 https://www.sciencedirect.com/science/article/pii/S1386418114000779 （网络错误，未打开）。
6. Ito & Yamada：NBER 工作稿 PDF https://www.nber.org/system/files/working_papers/w23327/w23327.pdf （已读）；Crossref https://api.crossref.org/works/10.1016/j.jimonfin.2017.10.004 与 https://api.crossref.org/works/10.3386/w23327 。
7. Yamada & Ito (2017)：RePEc 条目页 https://ideas.repec.org/a/eee/finlet/v21y2017icp157-162.html （摘要）。
8. Evans (2018)：Crossref https://api.crossref.org/works/10.1016/j.jbankfin.2017.09.017 ；2014-08 首稿 PDF https://mpra.ub.uni-muenchen.de/58151/7/MPRA_paper_58151.pdf （已读）。
9. Osler & Turnbull：Crossref https://api.crossref.org/works/10.1016/j.jbankfin.2026.107641 ；工作稿条目页 https://ideas.repec.org/p/brd/wpaper/101r.html 与 https://econpapers.repec.org/RePEc:brd:wpaper:101r （摘要与题录）；Brandeis 的工作稿 PDF 返回 403，未打开。
10. Ben Zeev & Nathan：Crossref https://api.crossref.org/works/10.1093/rapstu/raae012 （摘要）；工作稿 PDF https://mpra.ub.uni-muenchen.de/115194/1/MPRA_paper_112909.pdf （已读首页）。
11. FSB (2014)：PDF https://www.fsb.org/uploads/r_140715.pdf （已读相关页）。
12. Benenchia 等：Crossref https://api.crossref.org/works/10.1016/j.pacfin.2024.102652 ；OpenAlex https://api.openalex.org/works/doi:10.1016/j.pacfin.2024.102652 （摘要）。
13. 外汇 TOM 三篇的 Crossref 记录（都含摘要）：https://api.crossref.org/works/10.1108/ijmf-05-2014-0068 ；https://api.crossref.org/works/10.1108/mf-06-2024-0430 ；https://api.crossref.org/works/10.1108/mf-06-2024-0438 。
14. 黄金：Crossref https://api.crossref.org/works/10.1108/mf-02-2024-0088 （摘要）；https://api.crossref.org/works/10.2139/ssrn.3511451 （仅题录）；https://api.crossref.org/works/10.1002/fut.21775 （摘要）；Seasonax https://www.seasonax.com/how-does-gold-perform-around-the-turn-of-the-month （已读）；In Gold We Trust https://ingoldwetrust.report/nuggets/calendar-anomalies-and-the-gold-market/?lang=en （已读正文）；Zenodo 记录 https://zenodo.org/api/records/23398616 （说明）。
15. 比特币：Crossref https://api.crossref.org/works/10.1108/mf-02-2022-0084 （摘要）；OpenAlex https://api.openalex.org/works/doi:10.1504/ijbaaf.2023.129336 （摘要）；Crossref https://api.crossref.org/works/10.1108/mf-10-2024-0754 （摘要）；HAL https://api.archives-ouvertes.fr/search/?q=halId_s:hal-03701815 （摘要）与 Crossref https://api.crossref.org/works/10.1007/978-3-030-64849-7_16 ；Crossref https://api.crossref.org/works/10.47782/oeba202304028401 （仅题录）；Zenodo 记录 https://zenodo.org/api/records/23401075 （说明）。
16. 只核实到题录的三篇：OpenAlex https://api.openalex.org/works/doi:10.2139/ssrn.5143899 （Sizova 等；SSRN 页面被拦截）；Crossref 题名检索命中的 https://doi.org/10.2139/ssrn.3270844 （Evans 等）；Crossref https://api.crossref.org/works/10.1016/j.frl.2019.04.023 （Baur 等）。
17. 仓库内的对照材料（只读）：`research/candidates/C002.toml`、`research/cards/H101.toml`、`docs/research/d79-feasibility-2026-11-13.md`、`docs/research/candidate-triage-2026-11-13.md`、`docs/research/candidates-R1-2026-10-28.md`、`docs/research/candidates-R1-2026-10-29.md`、`docs/reviews/2026-10-28-review-round13.md`、`docs/reviews/2026-11-20-review-round22.md`、两份轮次计划。

## 10. 红线声明

本文件不含公网 IP 地址、本机绝对路径、账号、本项目服务器名、主机名或令牌；来源清单里出现的域名都是公开文献站点，是给出 URL 所必需的。日期一律 UTC。制作过程未下载任何行情数据、未连接任何终端、未计算任何策略收益、未开启或预筛任何轮次、未请求 G5、未改动仓库其他文件、未执行任何改变 git 状态的命令。文中所有"评价"和 §7 的建议都是我的判断，不是文献结论。
