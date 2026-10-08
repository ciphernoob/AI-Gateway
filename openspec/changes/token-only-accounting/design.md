## Context

当前数据面在一次尝试开始时同时创建 Token quota 状态和金额 reservation，随后由共享异步队列执行两类结算。Redis 日/月 Token 键只按用户分组，模型只存在于事件元数据；管理配置和 UI 又要求模型价格、模型金额额度、Agent 金额额度和全局金额额度。详见 proposal.md 的变更动机。

OpenResty 的 `body_filter` 与 `log` 阶段不能执行 Redis cosocket I/O，所以响应观察仍需将标准化 Usage Event 放入 worker 内存队列，由 timer 回调结算。JSON 与 SSE 都已经能观察 Provider usage；本变更收紧消费规则，不改变代理协议。

## Goals / Non-Goals

**Goals:**

- 让 Provider 返回值成为唯一 Token 数值来源。
- 让日/月账本按用户和逻辑模型可独立查询，并保留用户汇总视图。
- 从请求阻断路径、配置、管理 API、UI、指标和审计关联中删除金额语义。
- 保持 attempt 级幂等、Fallback 多尝试记录以及 unknown/pending 的真实性。

**Non-Goals:**

- 不实现 Token 硬限额、预占或超限拒绝。
- 不实现本地 tokenizer、Token 估算或不同 Provider 字段的推断换算。
- 不迁移或删除 Redis 中历史金额键；它们停止使用并按运维策略自然退役。
- 不把审计正文作为 Token 账单来源。

## Decisions

### 1. 每个 attempt 绑定逻辑统计模型

尝试元数据持久化 `logical_model`，日/月键采用 `quota:usage:<user_id>:<logical_model>:<period>`。Fallback 候选即使使用不同实际模型，也以网关为该尝试选择的逻辑统计模型结算；若未来跨逻辑模型路由，路由决策必须显式更新 attempt 的逻辑统计模型。

选择这一结构而不是在单个用户 Hash 中创建动态模型字段，是为了保持现有三个 Token 字段和 pending/unknown 原子更新逻辑简单，并避免模型名参与字段名编码。

### 2. 用户汇总在查询时聚合模型账本

运行配置已提供有限模型注册表。`/v1/usage` 与管理查询对注册模型逐一读取同一窗口的 Hash，再求和生成 summary。这样只有模型维度一个事实来源，不需要双写“用户总计”和“用户模型明细”。

备选方案是每次结算同时更新用户总键与模型键，但会增加跨键原子计划、迁移和部分失败的复杂度，因此不采用。

### 3. 移除 Budget consumer，保留精简的 accounting consumer

删除请求前金额 reserve。尝试开始只创建 Token pending 状态，真正向上游发送前仍原子标记 dispatch，防止恢复器把已发送请求当作未发送。响应后的 timer 只执行 quota settle 和 Audit Store usage 关联。

Redis 脚本删除 reservation、global/agent/model budget 键参数与 budget operation。恢复器对未发送 attempt 直接关闭而不写零 Token；对可能已发送但无 Provider usage 的 attempt 写 unknown。

### 4. 严格接受 Provider 基础 usage

标准化器仅接受三个非负安全整数并要求 prompt + completion = total。有效响应标为 provider/known；缺失或非法响应标为 unknown，且数值保持 null。`system zero` 路径被移除，因为它不是后端返回值，也没有必要作为 Token 统计事实。

审计事件可继续记录 usage_source 和 usage_status，但删除 `cost`、`cost_micro_usd`、`price_version`。Prometheus 保留 Token 与 unknown/settlement 指标，删除费用和预算指标。

### 5. 管理配置采用破坏性的新 schema

校验器要求插件集合不再包含 `budget`，顶层不接受 `budget`，候选不接受 `input_rate`、`output_rate` 和 `price_version`。管理数据库中的旧草稿在服务启动或首次读取时通过确定性迁移删除这些字段，然后以新 schema 发布；版本历史保持不可变，回滚旧版本前先通过当前编译器迁移并校验。

管理 API 删除 `budget_limit`、`agent_budget`、`global_limit` 和 `agent_limits`。Vue 页面及文案同步删除金额字段。该设计选择显式拒绝新提交中的遗留字段，而不是静默忽略，以避免管理员误以为金额策略仍生效。

## Risks / Trade-offs

- [Provider 不返回 usage 时无法得到 Token 数值] → 将该尝试显示为 unknown，并要求 Provider/流式配置返回 usage；不以估算掩盖缺口。
- [按模型查询增加 Redis 读取数] → 首版模型数量有限，使用 pipeline 或 Lua 查询保持单次快照；设置模型数量和分页边界。
- [旧运行快照无法通过新 schema] → 启动导入和版本发布加入确定性去金额迁移；切换前完成校验，失败保留现有 worker。
- [内存结算队列在 worker 异常退出时仍可能丢失] → 保留 Redis pending 集合与恢复器；无法恢复准确 usage 时记 unknown，不从审计内容推算。
- [移除金额拒绝后调用成本不再受网关保护] → 文档明确这是产品选择，成本控制交由后端 Provider 或未来独立策略变更。

## Migration Plan

1. 更新配置编译器和管理草稿导入，生成不含金额字段的新快照。
2. 部署兼容读取旧 Token 总量但只向新模型维度键写入的版本；查询明确区分 legacy 总量，避免与新明细相加。
3. 发布新配置并平滑重载 OpenResty，新请求停止访问金额账本。
4. 更新管理页面、指标和文档，验证 JSON、SSE、Fallback、unknown、并发幂等及依赖故障。
5. 旧金额 Redis 键不主动删除；回滚程序版本时仍可读取原账本，回滚前需恢复带金额字段的历史配置快照。
