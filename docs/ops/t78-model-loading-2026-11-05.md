# T78 冻结模型装载（2026-11-05）

范围仅为离线工程装配。`live_runner.main` 在 `guarded_main`、网关、实例锁、心跳、意图和 paper 状态创建之前完成 fittable 模型预检；`artifact.load_model` 校验文件原始字节 SHA256 并解析同一批字节，`with_model` 返回的新实例原样传给 `_run` / `SignalRunner` / 原有执行器。启动路径不调用 `fit`，不重造 paper 框架，不改变冻结策略、概率表、artifact 或成交语义。

## 配置和装配身份

新增字段为一个完整可选组；旧无模型配置的字段默认值不变。

- `model_path` / `model_sha256` 成对；后者为 64 位十六进制 SHA256。
- `card_path` 为显式 P2 决策卡路径，`experiment_sha256` 为 64 位十六进制装配指纹。
- `[experiment]` 必须含非空 JSON 表 `bars_meta`、非空字符串 `cost_version` / `sessions_sha`；只另允许可选非空字符串 `evaluator_sha`。上下文不触发行情、成本、轮次或台账读取，不证明这些材料获准。
- 模型和卡的相对路径以**配置文件目录**解析，与进程 cwd、`data_root` 和 `--data-dir` 无关。其他旧路径语义不变。

现有模型不含 card/experiment 新字段。这里不新增模型 identity 或 modelID，而是复用 `mt5quant.research.gate.experiment_fingerprint` 的原有接口：

```python
experiment_fingerprint(
    strategy_ref=cfg.strategy,
    card=card.id,
    model_dict=model,  # 已通过 artifact 字节哈希校验的原模型
    bars_meta=cfg.experiment["bars_meta"],
    cost_version=cfg.experiment["cost_version"],
    sessions_sha=cfg.experiment["sessions_sha"],
    params={"strategy_params": cfg.params,
            "card_sha256": sha256(card_bytes).hexdigest()},
    features=model["features"],
    evaluator_sha=cfg.experiment.get("evaluator_sha"),
)["sha256"]
```

卡由同一批 `card_bytes` 解析及计算哈希。复算值必须等于 `experiment_sha256`。此封套表达 **T78 离线装配身份**，不是已有研究 run 的身份或批准；不能把其他调用方式产生的旧指纹直接拿来当它使用。`evaluator_sha` 缺省时既有组件明确为 `bound=False` / “未绑定”，不会冒充覆盖。

既有指纹会对策略引用、卡号、模型内容、参数、特征、上下文以及代码闭包文件哈希进行绑定。T78 额外拒绝闭包解析为空或不可得的 `files=None` 降级。闭包使用绝对路径，故此指纹绑定当前 checkout；移动目录或更换 checkout 后必须显式重新装配核验，不能静默复用。模型原始字节 SHA 与该装配指纹作用不同；两者匹配仍不代表研究/运行许可。

## 启动核验与兼容

当前仅支持 `LevelDecisionV1` 家族。关键模型字段必须完整：schema/prob_version、单品种/周期、fit_window 起止、purge、bracket、situations、features、policy、allowed_hypotheses。关键值与策略常量逐项核对；训练窗须起点早于终点且结束不晚于当前时间，purge 按现有拟合接线为 `horizon_bars + 2`。概率表的特征、prior_strength、min_cell 与策略/卡一致，成本键限定有效局面×动作且值为有限非负数。

卡须显式绑定相同策略、单品种和周期，采用 standard+validation；参数在卡网格内，bracket、网格、approach、breakout/retest、五个决策字段及有效假设集合须与策略一致。不接受冻结旧模型中省略关键身份字段的宽松路径，也不更改冻结策略本身的兼容行为。

- fittable 缺模型、模型/卡/实验错配、模型配置错误：直接返回 2，不进入监控包装、不更改原有运行状态。
- 非 fittable 携带模型配置：拒绝，不静默忽略。
- 旧非 fittable 策略装载/不支持的出场规则错误仍走 `guarded_main`，保留原 `exit-runner-*` / fatal 监控证据；`time_stop_bars` / `week_close_exit` 仍拒绝。
- TOML 语法/编码损坏且含新模型字段声明时，直接返回 2；不生成运行状态。不带这些声明的旧损坏配置继续由 `guarded_main` 记录错误，旧合法配置路径保持原行为。

## 运行许可与验证边界

所有 fittable 线上模式当前均在读取模型/卡之前拒绝（含普通运行、在线 dry-run、在线 paper）。正路径只接受 `--paper --dry-run --offline`，用于独立裁定允许的合成输入工程验证；参数组合本身不授权真实数据读取，也不将内容自动认证为合成。没有 `--approved` 开关；模型自填 `approved_by`、`release_id` 或 `synthetic` 不会获得线上许可。

真实 P 部署仍须研究/阶段 F/维护窗口等前置和独立放行。现有 Demo 保险丝和 0.01 手规则不变；本轮没有真实账户、MT5、真实数据/配置/状态或部署操作。

`tests/test_t78_model_loading.py` 使用手写合成行情和两份无需拟合的冻结模型，真实 `runner.main` → `LevelDecisionS1Demo.generate_signals` → 原有 `PaperBroker`：相同末根触发分别产生 `-1` / `0`，结果为 0.01 手 sell / 零持仓。未 mock `generate_signals`，`fit` 一旦被调用即失败，并检查 `with_model` 新实例传递。反例覆盖重算指纹后的范围错配、装配身份变化、闭包不可得、线上读取前拒绝、失败零状态副作用及旧路径兼容。全量回归和独立审核由集成主会话完成；合成通过不等于策略有效或真实 Demo 获准。
